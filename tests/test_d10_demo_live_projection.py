"""D10 演示实时投影 —— 判别测试(SSOT v2.0 §8.2 · Owner 2026-07-25)。

D10 把演示从「冻结快照另做一套视图」改为「真实 handler 的受控实时只读投影」。
本文件锁死改造后的四条支柱，并逐条覆盖 Owner 预告的三个攻击面：

  攻击面 1 跨品牌越权   → TestCrossBrandIsolation / TestAuthorityLayerScope
  攻击面 2 副作用 GET 漏网 → TestSideEffectGetFence
  攻击面 3 隐私字段出站泄漏 → TestOutboundPrivacyScrub

判别方向以 **DENIED→ALLOWED**（读面从 404/快照变实时真数据）和
**ALLOWED→DENIED**（写/副作用面必须继续被拒）双向覆盖。
"""
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auth.brand_access import demo_readable_brand_id, require_brand_access, get_user_brand_filter
from services.demo_access import (
    DEMO_PRIVATE_KEY_ALLOWLIST,
    demo_preview_contract,
    resolve_demo_route_contract,
    scrub_demo_payload,
)
from services.governance_contract import is_alert_contract


AUTHORIZED_BRAND = 101
OTHER_BRAND_SAME_OWNER = 102     # 同一个 owner 名下的另一个品牌
FOREIGN_BRAND = 999              # 完全无关租户


def _req(method="GET", path="/api/monitoring/keywords", *, demo_brand=None,
         user=None, org=None):
    """构造一个最小 Request 替身（只用到 method / state / url.path）。"""
    state = SimpleNamespace()
    state.user = user if user is not None else {"user_id": 7, "username": "demo_viewer"}
    if demo_brand is not None:
        state.demo_access_context = SimpleNamespace(brand_id=demo_brand)
    state.organization_identity = org
    return SimpleNamespace(method=method, state=state, url=SimpleNamespace(path=path))


# ===========================================================================
# 支柱 2 / 攻击面 1：租户隔离由唯一授权层承担
# ===========================================================================
class TestAuthorityLayerScope:
    def test_demo_brand_readable_on_get(self):
        assert demo_readable_brand_id(_req("GET", demo_brand=AUTHORIZED_BRAND)) == AUTHORIZED_BRAND
        assert demo_readable_brand_id(_req("HEAD", demo_brand=AUTHORIZED_BRAND)) == AUTHORIZED_BRAND

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_write_methods_never_get_demo_authority(self, method):
        """🔴 只读硬约束：任何写方法都不得从演示上下文拿到品牌权限。"""
        assert demo_readable_brand_id(_req(method, demo_brand=AUTHORIZED_BRAND)) is None

    def test_no_demo_context_no_authority(self):
        assert demo_readable_brand_id(_req("GET")) is None

    def test_malformed_context_fails_closed(self):
        for bad in (0, -1, None, "abc"):
            assert demo_readable_brand_id(_req("GET", demo_brand=bad)) is None


class TestCrossBrandIsolation:
    """🔴 攻击面 1：演示只能读被授权的那一个品牌。"""

    def test_authorized_brand_passes(self):
        require_brand_access(_req("GET", demo_brand=AUTHORIZED_BRAND), AUTHORIZED_BRAND)

    def test_other_brand_of_same_owner_denied(self):
        """owner 名下多品牌：范围按**品牌**不按**用户**，同 owner 的别的品牌也不行。"""
        with pytest.raises(HTTPException) as e:
            require_brand_access(_req("GET", demo_brand=AUTHORIZED_BRAND), OTHER_BRAND_SAME_OWNER)
        assert e.value.status_code == 404

    def test_foreign_tenant_denied(self):
        with pytest.raises(HTTPException) as e:
            require_brand_access(_req("GET", demo_brand=AUTHORIZED_BRAND), FOREIGN_BRAND)
        assert e.value.status_code == 404

    def test_demo_authority_does_not_leak_into_write(self):
        """演示上下文在写方法上不得放行，即使品牌就是被授权那个。"""
        with pytest.raises(HTTPException) as e:
            require_brand_access(_req("POST", demo_brand=AUTHORIZED_BRAND), AUTHORIZED_BRAND)
        assert e.value.status_code == 404

    def test_list_filter_includes_only_authorized_brand(self):
        ids = get_user_brand_filter(_req("GET", demo_brand=AUTHORIZED_BRAND))
        assert AUTHORIZED_BRAND in ids
        assert OTHER_BRAND_SAME_OWNER not in ids
        assert FOREIGN_BRAND not in ids

    def test_list_filter_excludes_demo_brand_on_write(self):
        ids = get_user_brand_filter(_req("POST", demo_brand=AUTHORIZED_BRAND))
        assert AUTHORIZED_BRAND not in ids

    def test_org_member_keeps_demo_readability(self):
        """组织成员持有演示授权时不得被 org 分支提前 404。"""
        req = _req("GET", demo_brand=AUTHORIZED_BRAND, org=SimpleNamespace(user_id=7))
        import auth.brand_access as ba
        original = ba.__dict__.get("assigned_brand_ids")
        import db.organization_db as odb
        odb.assigned_brand_ids = lambda identity: [55]      # 组织只分配了 55
        try:
            require_brand_access(req, AUTHORIZED_BRAND)      # 演示品牌仍可读
            with pytest.raises(HTTPException):
                require_brand_access(req, FOREIGN_BRAND)     # 越界照旧拒
        finally:
            if original is not None:
                ba.assigned_brand_ids = original


# ===========================================================================
# 支柱 1：读 = 真实 handler（快照降级为 fallback）
# ===========================================================================
class TestLiveReadSurfaces:
    @pytest.mark.parametrize("path", [
        "/api/diagnosis/123", "/api/quotes/55", "/api/reports/9",
        "/api/monitoring/keywords", "/api/articles/7", "/api/publish/records",
        "/api/client-context/list",
    ])
    def test_read_surfaces_are_live_not_snapshot(self, path):
        c = resolve_demo_route_contract("GET", path)
        assert c is not None and c.disposition == "live", f"{path} 仍走快照/被拒"

    def test_unmapped_read_falls_through_to_real_handler(self):
        """D10「任何功能面」：未登记的只读面不再 404，默认进真 handler。"""
        assert resolve_demo_route_contract("GET", "/api/some/brand-new/surface") is None

    def test_portal_token_surface_stays_frozen(self):
        """🔴 门户凭证面必须继续走冻结快照，绝不能进真 handler 拿到真 token。"""
        c = resolve_demo_route_contract("GET", "/api/portal/tokens/by-brand/7")
        assert c is not None and c.disposition == "frozen_snapshot"


# ===========================================================================
# 支柱 4 / 攻击面 2：写闸 + 副作用 GET
# ===========================================================================
class TestSideEffectGetFence:
    """🔴 攻击面 2：只读默认放行后，有副作用的 GET 必须显式挡住。"""

    @pytest.mark.parametrize("path", [
        "/api/monitoring/run-stream", "/api/monitoring/run", "/api/diagnosis/1/rerun",
        "/api/tasks/9/retry", "/api/admin/clear-data", "/api/admin/restore-data",
        "/api/batch/rollback-batch", "/api/content/deep-analyze", "/api/reports/1/send",
        "/api/keywords/expand", "/api/diagnosis/1/distill", "/api/monitoring/stream",
        "/api/x/generate", "/api/x/refresh", "/api/x/sync", "/api/x/trigger",
        "/api/x/cancel", "/api/x/collect", "/api/x/autofill",
    ])
    def test_side_effect_gets_are_blocked(self, path):
        c = resolve_demo_route_contract("GET", path)
        assert c is not None, f"{path} 未被登记 → 会进真 handler 执行副作用"
        assert c.disposition == "preview", f"{path} 未被拒绝（disposition={c.disposition}）"

    @pytest.mark.parametrize("path", [
        "/api/brands/current", "/api/quotes/55", "/api/monitoring/keywords",
        "/api/reports/9", "/api/diagnosis/123",
    ])
    def test_plain_reads_not_false_positived(self, path):
        """副作用正则不得误伤正常只读面（否则演示又变回缩水页）。"""
        c = resolve_demo_route_contract("GET", path)
        assert c is None or c.disposition == "live", f"{path} 被副作用规则误伤"

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_all_write_methods_blocked(self, method):
        c = resolve_demo_route_contract(method, "/api/anything/at/all")
        assert c is not None and c.disposition == "preview"

    def test_credential_and_raw_export_still_blocked(self):
        for path in ("/api/quotes/1/export", "/api/x/share", "/api/y/download", "/api/z/report.pdf"):
            c = resolve_demo_route_contract("GET", path)
            assert c is not None and c.disposition == "preview", path


class TestZeroWriteZeroChargeContract:
    """演示零写入/零 provider/零资金合同必须原样保留，并升到 §13 七字段。"""

    def _contract(self, method="POST", path="/api/quotes"):
        req = _req(method, path, demo_brand=AUTHORIZED_BRAND)
        req.headers = {}
        req.client = None
        return demo_preview_contract(req, SimpleNamespace(brand_id=AUTHORIZED_BRAND),
                                     "DEMO_SIDE_EFFECT_BOUNDARY")

    def test_zero_effect_keys_preserved(self):
        c = self._contract()
        for key in ("saved", "charged", "provider_called", "external_service_called", "job_created"):
            assert c[key] is False, f"{key} 不再声明为 False"

    def test_is_section13_alert_contract(self):
        c = self._contract()
        assert is_alert_contract(c), "演示拒绝不是 §13 合同（红码无下一步 = 事故#8）"
        assert c["actions"], "无任何下一步动作"

    def test_message_says_it_is_a_demo(self):
        c = self._contract()
        assert "演示" in c["message"]
        assert "演示" in c["reason"]

    def test_impact_states_nothing_happened(self):
        c = self._contract()
        for phrase in ("没有保存", "没有", "费用"):
            assert phrase in c["impact"]

    def test_legacy_transport_keys_preserved(self):
        c = self._contract()
        for key in ("code", "access_mode", "action", "route_contract", "blocked_reason",
                    "refresh_discards_local_preview", "real_mode_effects", "request_id"):
            assert key in c, f"既有传输键 {key} 被破坏"
        assert c["code"] == "DEMO_ACTION_PREVIEW"
        assert c["access_mode"] == "demo"


# ===========================================================================
# 支柱 3 / 攻击面 3：出站脱敏
# ===========================================================================
class TestOutboundPrivacyScrub:
    """🔴 攻击面 3：真 handler 的 owner 视角 DTO 出网前必须剥掉 §2.4 硬边界字段。"""

    PRIVATE = {
        "upstream_agent_id": 42, "parent_agent_name": "上级A", "sv_code": "SV-7",
        "agent_code": "AG-1", "base_cost": 120, "platform_cost": 88, "unit_cost": 12,
        "purchase_cost": 200, "cost_multiplier_bps": 11000, "markup_rate": 2.0,
        "coefficient": 1.35, "gross_profit": 300, "margin": 0.4,
        "portal_token": "pt_live_abc", "share_token": "st_1", "token": "secret",
        "api_key": "sk-1", "secret_key": "s", "password": "p",
        "contact_phone": "13800138000", "email": "a@b.com", "wechat": "wx",
        "mobile": "13900139000", "id_card": "310...", "bank_account": "62...",
    }

    def test_all_private_keys_stripped(self):
        out = scrub_demo_payload(dict(self.PRIVATE, brand_name="某某"))
        leaked = [k for k in self.PRIVATE if k in out]
        assert leaked == [], f"§2.4 隐私字段出站泄漏: {leaked}"
        assert out["brand_name"] == "某某"

    def test_nested_and_list_scrubbed(self):
        payload = {"rows": [{"keyword": "装修哪家好", "purchase_cost": 200,
                             "detail": {"upstream_agent_id": 9, "rate": 0.7}}]}
        out = scrub_demo_payload(payload)
        row = out["rows"][0]
        assert "purchase_cost" not in row
        assert "upstream_agent_id" not in row["detail"]
        assert row["keyword"] == "装修哪家好" and row["detail"]["rate"] == 0.7

    def test_business_data_preserved(self):
        """脱敏不得把演示要展示的真实业务数据也剥掉（否则又成缩水页）。"""
        payload = {"detection_rate": 0.75, "keyword": "装修哪家好", "total_tests": 40,
                   "standard": {"price": 500, "articles": 3}, "level": "B",
                   "status": "active", "created_at": "2026-07-25"}
        out = scrub_demo_payload(payload)
        assert out == payload

    def test_inline_pii_redacted(self):
        out = scrub_demo_payload({"note": "联系 13900139000 或 x@y.com，token=abc123"})
        assert "13900139000" not in out["note"]
        assert "x@y.com" not in out["note"]
        assert "abc123" not in out["note"]

    def test_allowlist_kept(self):
        out = scrub_demo_payload({k: "v" for k in DEMO_PRIVATE_KEY_ALLOWLIST})
        for key in DEMO_PRIVATE_KEY_ALLOWLIST:
            assert key in out, f"白名单键 {key} 被误剥"

    def test_recursion_depth_guard(self):
        node = {"leaf": 1}
        for _ in range(60):
            node = {"child": node, "unit_cost": 9}
        out = scrub_demo_payload(node)          # 不得爆栈
        assert "unit_cost" not in out
