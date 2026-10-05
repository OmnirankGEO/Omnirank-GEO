"""tests/test_whitelabel_id132_regression.py — 板块 C 白标作用域纠正 · ID 132 回归

合同: docs/AI-CONTEXT/BUILDER_CTO_NEXT_QUALITY_BATCH_DEBUG_PLAN_2026-07-22.md §4（C1-C9）
裁决: 同合同 §13.2 Review-CTO D1/D2/D3（Owner 2026-07-22 · 优先级最高）

覆盖断言：
  ID132-① 客户页面（surface=customer）显示其对外品牌（D1 自设免审批 · 行为不变）
  ID132-② 内部后台（surface=agent 未授权）显示 OmniRank（D2 fail-closed · 修复外溢）
  ID132-③ 其他服务商不受影响（跨账号不残留 / 不串号）
  D2-迁移  oem+active+unlocked_by_admin 既有授权映射后 backoffice 仍有效（不无差别收回）
  D2-总闸  WHITELABEL_BACKOFFICE_BRAND_ENABLED=false 时全量 fail-closed
  D3-暂停  emergency suspension → 快照展示层压制回落平台；locked/到期 → 冻结保留
  D1-校验  拒绝 javascript:/data:/http URL；HEAD 探测失败给 400；文本危险字符拒绝
  D1-审计  品牌字段变更写 whitelabel_audit（before/after/request_id/ip）+ brand_version+1
  C3      公开端点 surface gate：surface=agent/admin → 不下发 whitelabel 字段

全部 fake/注入，不连真实 DB（conftest 的 TEST_DATABASE_URL 只做名字安全校验）。
"""
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ============================================================
# fake DB 基础设施（脚本化行返回 · 捕获执行 SQL）
# ============================================================

class _Cur:
    def __init__(self, rows):
        self._rows = list(rows)
        self.executed: list[tuple[str, object]] = []

    def execute(self, query, params=None):
        self.executed.append((" ".join(str(query).split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _Conn:
    def __init__(self, rows):
        self._cur = _Cur(rows)

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def close(self):
        pass


def _patch_conn(monkeypatch, module, rows, *, attr="get_connection"):
    conn = _Conn(rows)
    monkeypatch.setattr(module, attr, lambda: conn)
    return conn


# ID 132 的白标行：客户页自设 external_only 已生效 · 无 backoffice 授权（D2 固定 customer-only）
ID132_ROW = {
    "company_name": "代理商132品牌",
    "product_name": "132营销云",
    "logo_url": "https://cdn.example/132.png",
    "favicon_url": None,
    "slogan": "本地增长服务商",
    "brand_color": None,
    "contact_name": None, "contact_phone": None, "contact_wechat": None, "contact_email": None,
    "whitelabel_mode": "external_only",
    "whitelabel_status": "active",
    "unlocked_by_admin": False,
    "backoffice_brand_unlocked": False,
}

# 另一服务商（跨账号隔离对照 · 既有 oem 已授权 → 迁移后 backoffice_brand_unlocked=TRUE）
OEM_ROW = {
    "company_name": "老牌OEM代理",
    "product_name": "OEM云",
    "logo_url": "https://cdn.example/oem.png",
    "favicon_url": None,
    "slogan": None,
    "brand_color": None,
    "contact_name": None, "contact_phone": None, "contact_wechat": None, "contact_email": None,
    "whitelabel_mode": "oem",
    "whitelabel_status": "active",
    "unlocked_by_admin": True,
    # 迁移脚本 UPDATE 后的状态（mode=oem ∧ status=active ∧ unlocked_by_admin → TRUE）
    "backoffice_brand_unlocked": True,
}


def _flag_on(monkeypatch):
    from services import public_whitelabel as pwl
    monkeypatch.setattr(pwl, "is_backoffice_brand_enabled", lambda: True)


def _flag_off(monkeypatch):
    from services import public_whitelabel as pwl
    monkeypatch.setattr(pwl, "is_backoffice_brand_enabled", lambda: False)


# ============================================================
# ID 132 三断言
# ============================================================

def test_id132_customer_surface_shows_agent_brand():
    """ID132-① 客户页面显示其对外品牌（D1 自设免审批 · customer surface 行为不变）。"""
    from services.public_whitelabel import public_branding_from_record

    out = public_branding_from_record(dict(ID132_ROW), surface="customer")
    assert out["display_scope"] == "approved_whitelabel"
    assert out["brand"]["company_name"] == "代理商132品牌"
    assert out["brand"]["logo_url"] == "https://cdn.example/132.png"


def test_id132_agent_surface_without_backoffice_shows_platform(monkeypatch):
    """ID132-② 内部后台未授权 → OmniRank（D2 fail-closed · 即使总闸开也不显示）。

    区分性：本断言在旧逻辑（agent 判定绑 mode='oem' 或无 gate）下会失败——
    external_only 行 surface=agent 的 display_scope 必须恒为 platform。
    """
    _flag_on(monkeypatch)  # 总闸打开也不救：132 无 backoffice_brand_unlocked
    from services.public_whitelabel import public_branding_from_record

    out = public_branding_from_record(dict(ID132_ROW), surface="agent")
    assert out["display_scope"] == "platform"
    assert out["brand"]["company_name"] == "OmniRank · 全域上榜"
    assert out["brand"]["logo_url"] == "/logo-192.png"


def test_id132_public_lookup_and_cross_account_isolation(monkeypatch):
    """ID132-③ 公开解析链：132 的客户拿 132 品牌；其他服务商行独立解析互不影响。"""
    from services import public_whitelabel as pwl

    # 132 的客户经 quote → owner=132 → 拿到 132 品牌（customer surface）
    _patch_conn(monkeypatch, pwl, [{"owner_user_id": 132}, dict(ID132_ROW)])
    out132 = pwl.get_public_whitelabel_data(quote_id=9001)
    assert out132["display_scope"] == "approved_whitelabel"
    assert out132["whitelabel"]["company_name"] == "代理商132品牌"

    # 另一服务商的客户经各自 quote → owner=456 → 拿各自品牌（不串 132）
    other_row = {**dict(ID132_ROW), "company_name": "服务商456品牌",
                 "product_name": "456云", "slogan": None,
                 "logo_url": "https://cdn.example/456.png"}
    _patch_conn(monkeypatch, pwl, [{"owner_user_id": 456}, other_row])
    out456 = pwl.get_public_whitelabel_data(quote_id=9002)
    assert out456["whitelabel"]["company_name"] == "服务商456品牌"
    assert "132" not in repr(out456)

    # 无白标账号 → 平台兜底（不含任何 132/456 品牌残留）
    _patch_conn(monkeypatch, pwl, [{"owner_user_id": 789}, None])
    out789 = pwl.get_public_whitelabel_data(quote_id=9003)
    assert out789["display_scope"] == "platform"
    assert out789["whitelabel"] is None
    assert "132" not in repr(out789) and "456" not in repr(out789)


# ============================================================
# D2 · 既有 oem 授权映射后 backoffice 仍有效（禁止无差别收回）
# ============================================================

def test_d2_migrated_oem_keeps_backoffice_branding(monkeypatch):
    """oem+active+unlocked_by_admin → 迁移置 backoffice_brand_unlocked=TRUE 后：

    总闸开 → agent surface 仍显示代理品牌（D2 不无差别收回真正已授权者）。"""
    _flag_on(monkeypatch)
    from services.public_whitelabel import public_branding_from_record

    out = public_branding_from_record(dict(OEM_ROW), surface="agent")
    assert out["display_scope"] == "approved_whitelabel"
    assert out["brand"]["company_name"] == "老牌OEM代理"
    # customer surface 不受影响（oem 客户侧本来就能显示）
    out_c = public_branding_from_record(dict(OEM_ROW), surface="customer")
    assert out_c["brand"]["company_name"] == "老牌OEM代理"


def test_d2_master_flag_off_fails_closed(monkeypatch):
    """D2/合同§7：总闸 WHITELABEL_BACKOFFICE_BRAND_ENABLED=false → 后台一律平台。"""
    _flag_off(monkeypatch)
    from services.public_whitelabel import public_branding_from_record

    out = public_branding_from_record(dict(OEM_ROW), surface="agent")
    assert out["display_scope"] == "platform"
    assert out["brand"]["company_name"] == "OmniRank · 全域上榜"


def test_d2_resolve_live_agent_surface_reads_backoffice_column(monkeypatch):
    """live 分支（resolve_branding_context surface=agent）只读 backoffice 授权位。"""
    _flag_on(monkeypatch)
    from services import public_whitelabel as pwl

    # oem+active+unlocked 但 backoffice_brand_unlocked=False（未被迁移命中的脏态）→ 平台
    dirty = {**dict(OEM_ROW), "backoffice_brand_unlocked": False}
    _patch_conn(monkeypatch, pwl, [dirty])
    out = pwl.resolve_branding_context(surface="agent", owner_user_id=456)
    assert out["display_scope"] == "platform"

    # backoffice_brand_unlocked=TRUE（迁移命中）→ 显示
    _patch_conn(monkeypatch, pwl, [dict(OEM_ROW)])
    out2 = pwl.resolve_branding_context(surface="agent", owner_user_id=456)
    assert out2["display_scope"] == "approved_whitelabel"
    assert out2["brand"]["company_name"] == "老牌OEM代理"


def test_d2_id132_live_agent_surface_platform(monkeypatch):
    """ID 132 live 复核：external_only active + backoffice=FALSE → agent surface 平台。"""
    _flag_on(monkeypatch)
    from services import public_whitelabel as pwl

    _patch_conn(monkeypatch, pwl, [dict(ID132_ROW)])
    out = pwl.resolve_branding_context(surface="agent", owner_user_id=132)
    assert out["display_scope"] == "platform"
    assert out["brand"]["company_name"] == "OmniRank · 全域上榜"


# ============================================================
# D3 · 快照：emergency suspension 压制 / 正常撤权冻结保留 / 新报告用当前授权
# ============================================================

def _approved_snapshot(**over):
    base = {
        "company_name": "冻结代理品牌",
        "logo_url": "https://cdn.example/frozen.png",
        "whitelabel_mode": "external_only",
        "whitelabel_status": "active",   # 签发时冻结为 active
        "unlocked_by_admin": True,
    }
    base.update(over)
    return base


def test_d3_emergency_suspension_suppresses_snapshot(monkeypatch):
    """D3：实时 whitelabel_status='suspended'（冒用/安全/违法）→ 快照展示层压制回落平台。"""
    from services import public_whitelabel as pwl

    _patch_conn(monkeypatch, pwl, [{
        "whitelabel_status": "suspended",
        "backoffice_brand_unlocked": False,
    }])
    out = pwl.resolve_branding_context(
        surface="customer", owner_user_id=132, snapshot=_approved_snapshot(),
    )
    assert out["display_scope"] == "platform"
    assert out["status"] == "suspended"
    assert out.get("suppression") == "emergency_suspension"
    assert out["brand"]["company_name"] == "OmniRank · 全域上榜"


def test_d3_normal_revocation_keeps_frozen_snapshot(monkeypatch):
    """D3：正常到期/撤权（locked/none）→ 历史快照保留签发时冻结 Logo（快照数据不动）。"""
    from services import public_whitelabel as pwl

    _patch_conn(monkeypatch, pwl, [{
        "whitelabel_status": "locked",
        "backoffice_brand_unlocked": False,
    }])
    out = pwl.resolve_branding_context(
        surface="customer", owner_user_id=132, snapshot=_approved_snapshot(),
    )
    assert out["display_scope"] == "approved_whitelabel"
    assert out["source"] == "agent_quote_snapshot"
    assert out["brand"]["company_name"] == "冻结代理品牌"


def test_d3_new_material_uses_current_authorization(monkeypatch):
    """D3：新报告永远用当前有效授权 —— live 分支 locked → 平台（不沿用旧快照逻辑）。"""
    from services import public_whitelabel as pwl

    locked_live = {**dict(ID132_ROW), "whitelabel_status": "locked"}
    _patch_conn(monkeypatch, pwl, [locked_live])
    out = pwl.resolve_branding_context(surface="customer", owner_user_id=132)
    assert out["display_scope"] == "platform"


def test_d3_snapshot_without_owner_clues_keeps_frozen_customer_brand():
    """快照分支无属主线索（无法实时核验）→ customer 保持冻结（D3 默认保留）。"""
    from services.public_whitelabel import resolve_branding_context

    out = resolve_branding_context(surface="customer", snapshot=_approved_snapshot())
    assert out["display_scope"] == "approved_whitelabel"
    assert out["brand"]["company_name"] == "冻结代理品牌"


# ============================================================
# D1 · 校验（URL 协议 / 域名 / 后缀 / HEAD 探测 / 文本危险字符）
# ============================================================

def test_d1_validation_rejects_script_and_http_urls(monkeypatch):
    """javascript:/data:/http:// 一律 400；https 合法 URL 过（探测注入）。"""
    import api.referral_api as ra
    monkeypatch.setattr(ra, "_probe_brand_image_url", lambda url, label: None)

    for bad in ("javascript:alert(1)", "data:image/png;base64,AAAA",
                "http://cdn.example/a.png", "ftp://cdn.example/a.png"):
        with pytest.raises(Exception) as exc_info:
            ra._validate_brand_image_url(bad, "Logo 地址")
        assert getattr(exc_info.value, "status_code", None) == 400, bad

    # 无后缀 / 非法后缀 / 非法域名
    for bad in ("https://cdn.example/image", "https://cdn.example/a.gif",
                "https://user:pass@cdn.example/a.png"):
        with pytest.raises(Exception) as exc_info:
            ra._validate_brand_image_url(bad, "Logo 地址")
        assert getattr(exc_info.value, "status_code", None) == 400, bad

    assert ra._validate_brand_image_url("https://cdn.example/a.PNG", "Logo 地址") == "https://cdn.example/a.PNG"
    assert ra._validate_brand_image_url("/uploads/whitelabel-logos/abc/logo_1_x.png", "Logo 地址")
    assert ra._validate_brand_image_url("", "Logo 地址") is None
    assert ra._validate_brand_image_url(None, "Logo 地址") is None


def test_d1_probe_failure_gives_400_human_message(monkeypatch):
    """HEAD 探测失败（非图片/网络异常）→ 400 统一人话（集中严审 R1 · P1-2：不回显对端状态/类型）。

    R4 · P1-3：传输层改为固定 IP 的 _pinned_https_roundtrip（防 DNS rebinding），
    mock 面相应从 requests.head 迁移到该 seam；断言口径不变。"""
    import socket

    import api.referral_api as ra

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    monkeypatch.setattr(
        ra,
        "_pinned_https_roundtrip",
        lambda host, pinned_ips, method, target, timeout=5: (200, {"content-type": "text/html"}),
    )
    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url("https://cdn.example/a.png", "Logo 地址")
    assert getattr(exc_info.value, "status_code", None) == 400
    detail = getattr(exc_info.value, "detail", "")
    assert "不可用或不支持" in detail
    assert "text/html" not in detail  # 不回显对端 Content-Type（防内网存活 oracle）

    def _boom(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(ra, "_pinned_https_roundtrip", _boom)
    with pytest.raises(Exception) as exc_info:
        ra._probe_brand_image_url("https://cdn.example/a.png", "Logo 地址")
    assert getattr(exc_info.value, "status_code", None) == 400
    assert "不可用或不支持" in getattr(exc_info.value, "detail", "")


def test_d1_text_and_color_validation():
    """company_name/product_name 长度+危险字符；slogan/联系方式同校验；brand_color 格式。"""
    from api.referral_api import _validate_brand_color, _validate_brand_text

    assert _validate_brand_text("正常公司名", "company_name") == "正常公司名"
    for bad in ("<script>x</script>", "a" * 101, "javascript:evil"):
        with pytest.raises(Exception) as exc_info:
            _validate_brand_text(bad, "company_name")
        assert getattr(exc_info.value, "status_code", None) == 400, bad
    with pytest.raises(Exception):
        _validate_brand_text("not-an-email", "contact_email")
    assert _validate_brand_color("#6CBE1E") == "#6CBE1E"
    with pytest.raises(Exception):
        _validate_brand_color("red")


# ============================================================
# D1 · 审计写入 + 版本化（update_whitelabel / admin grant 双写）
# ============================================================

def _client(user):
    from api.referral_api import router
    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request, call_next):
        request.state.user = user
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def test_d1_update_whitelabel_writes_audit_and_bumps_version(monkeypatch):
    """代理 PUT /whitelabel：品牌字段 before/after 写 whitelabel_audit（含 request_id/ip）+ brand_version+1。"""
    import api.referral_api as ra
    monkeypatch.setattr(ra, "_probe_brand_image_url", lambda url, label: None)

    before = {
        "user_id": 132, "company_name": "旧名", "logo_url": "https://cdn.example/old.png",
        "whitelabel_mode": "external_only", "whitelabel_status": "active",
        "unlocked_by_admin": False, "brand_terms_accepted_at": "2026-07-01",
        "brand_version": 7,
    }
    after = {
        **before, "company_name": "代理商132品牌", "logo_url": "https://cdn.example/132.png",
        "company_logo_url": "https://cdn.example/132.png",
    }
    conn = _Conn([before, after])
    monkeypatch.setattr(ra, "get_db", lambda: conn)

    r = _client({"user_id": 132, "is_admin": False}).put(
        "/api/referral/whitelabel",
        json={"company_name": "代理商132品牌", "logo_url": "https://cdn.example/132.png"},
        headers={"X-Request-ID": "req-id132-1", "X-Forwarded-For": "203.0.113.9"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["brand_version"] == 8
    sql = conn.cursor().executed
    audit = [(q, p) for q, p in sql if "INSERT INTO whitelabel_audit" in q]
    assert audit, "必须写 whitelabel_audit"
    fields = {p[3] for _q, p in audit}
    assert "company_name" in fields and "logo_url" in fields
    # company_name 行 before/after + actor + request_id + ip
    cn = next(p for _q, p in audit if p[3] == "company_name")
    assert cn[0] == 132 and cn[1] == 132 and cn[2] == "agent"
    assert cn[4] == "旧名" and cn[5] == "代理商132品牌"
    assert cn[6] == "req-id132-1" and cn[7] == "203.0.113.9"
    assert any("brand_version = COALESCE(brand_version, 1) + 1" in q for q, _p in sql)


def test_d1_admin_grant_backoffice_writes_audit(monkeypatch):
    """admin grant/revoke backoffice_brand：写审计（actor_role=admin）+ 授权痕迹列 + 版本+1。"""
    import api.referral_api as ra

    # get_connection 单独承担激活前置校验；get_db 序列仅 before SELECT → UPSERT RETURNING。
    rows = [
        {"whitelabel_mode": "none", "whitelabel_status": "locked",
         "unlocked_by_admin": False, "backoffice_brand_unlocked": False},
        {"user_id": 456, "whitelabel_mode": "oem", "whitelabel_status": "active",
         "unlocked_by_admin": True, "approved_by": 1, "approved_at": "2026-07-22",
         "brand_version": 12},
    ]
    conn = _Conn(rows)
    monkeypatch.setattr(ra, "get_db", lambda: conn)
    monkeypatch.setattr(ra, "get_connection", lambda: _Conn([
        {"company_name": "服务商品牌", "logo_url": "https://cdn.example/a.png"}
    ]))

    r = _client({"user_id": 1, "is_admin": True}).put(
        "/api/referral/admin/whitelabel/456",
        json={"whitelabel_mode": "oem", "whitelabel_status": "active",
              "backoffice_brand_unlocked": True, "reason": "OEM 合同已签"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["brand_version"] == 13
    sql = conn.cursor().executed
    assert any("SET backoffice_brand_unlocked = TRUE" in q and "backoffice_brand_granted_by" in q for q, _p in sql)
    audit = [(q, p) for q, p in sql if "INSERT INTO whitelabel_audit" in q]
    assert audit, "admin 授权必须写 whitelabel_audit"
    bo = next((p for _q, p in audit if p[3] == "backoffice_brand_unlocked"), None)
    assert bo is not None and bo[2] == "admin" and bo[5] == "True"
    assert any(p[8] == "OEM 合同已签" for _q, p in audit), "reason 必须落审计"
    assert any("brand_version = COALESCE(brand_version, 1) + 1" in q for q, _p in sql)


def test_d1_admin_brand_update_returns_bumped_version(monkeypatch):
    """admin 代填品牌：响应 brand_version 与同事务 bump 后 DB 值一致。"""
    import api.referral_api as ra

    monkeypatch.setattr(ra, "_probe_brand_image_url", lambda url, label: None)
    before = {
        "user_id": 456,
        "company_name": "旧服务商品牌",
        "logo_url": "https://cdn.example/old.png",
        "brand_version": 20,
    }
    after = {
        **before,
        "company_name": "新服务商品牌",
        "logo_url": "https://cdn.example/new.png",
        "company_logo_url": "https://cdn.example/new.png",
    }
    conn = _Conn([before, after])
    monkeypatch.setattr(ra, "get_db", lambda: conn)

    response = _client({"user_id": 1, "is_admin": True}).put(
        "/api/referral/admin/whitelabel/456/brand",
        json={
            "company_name": "新服务商品牌",
            "logo_url": "https://cdn.example/new.png",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["brand_version"] == 21
    assert any(
        "brand_version = COALESCE(brand_version, 1) + 1" in query
        for query, _params in conn.cursor().executed
    )


def test_d1_agent_cannot_write_backoffice_field():
    """代理请求体带 backoffice_brand_unlocked → hard 403（授权边界 · D1 后台换肤仅 admin）。"""
    c = _client({"user_id": 132, "is_admin": False})
    r = c.put("/api/referral/whitelabel",
              json={"company_name": "x", "backoffice_brand_unlocked": True})
    assert r.status_code == 403
    r2 = c.put("/api/referral/whitelabel", json={"brand_version": 99})
    assert r2.status_code == 403


# ============================================================
# C3 · 公开端点 surface gate（服务端写死）
# ============================================================

def test_c3_public_endpoint_surface_gate(monkeypatch):
    """GET /api/public/whitelabel?surface=agent|admin → 不下发 whitelabel 字段（平台默认）。"""
    import api.share_api as sa

    approved = {
        "display_scope": "approved_whitelabel",
        "brand": {"company_name": "代理商132品牌", "logo_url": "https://cdn.example/132.png"},
        "whitelabel": {"company_name": "代理商132品牌", "logo_url": "https://cdn.example/132.png"},
    }
    monkeypatch.setattr(sa, "get_public_whitelabel_data", lambda **kw: dict(approved))

    app = FastAPI()
    app.include_router(sa.router)
    c = TestClient(app)

    for surface in ("agent", "admin", "bogus"):
        r = c.get("/api/public/whitelabel", params={"quote_id": 9001, "surface": surface})
        assert r.status_code == 200
        body = r.json()
        assert body["display_scope"] == "platform", surface
        assert body["whitelabel"] is None, surface
        assert "132" not in repr(body), surface

    r2 = c.get("/api/public/whitelabel", params={"quote_id": 9001, "surface": "customer"})
    assert r2.json()["whitelabel"]["company_name"] == "代理商132品牌"


# ============================================================
# migration 静态断言（幂等 / D2 映射 / 审计表 / ID 132 固定 / 可连跑两次）
# ============================================================

def test_migration_file_idempotent_and_maps_existing_oem():
    root = Path(__file__).resolve().parents[1]
    sql = (root / "scripts" / "migration_whitelabel_backoffice_scope_2026_07_22.sql").read_text(encoding="utf-8")

    # 幂等标记：全部 ADD COLUMN / CREATE 带 IF NOT EXISTS → 可连跑两次
    assert sql.count("ADD COLUMN IF NOT EXISTS") >= 4
    # R6 复审 P2：对象引用全部 public.* 精确限定（诱饵 search_path 无法重定向）
    assert "SET LOCAL search_path = pg_catalog, public;" in sql
    assert "CREATE TABLE IF NOT EXISTS public.whitelabel_audit" in sql
    # [索引守卫加固二单 2026-08-24] 形态换成表绑定守卫;仍然全 public 限定。
    assert ("-- @index-guard idx_whitelabel_audit_user_created "
            "ON whitelabel_audit plain") in sql
    assert "CREATE INDEX idx_whitelabel_audit_user_created ON public.whitelabel_audit " in sql
    assert "ON public.whitelabel_audit (user_id, created_at DESC)" in sql
    assert "ALTER TABLE public.whitelabel_settings" in sql
    assert "UPDATE public.whitelabel_settings" in sql
    # 禁止残留未限定白标对象引用（注释除外）
    code_lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    code = "\n".join(code_lines)
    for bare in ("ALTER TABLE whitelabel_settings", "CREATE TABLE IF NOT EXISTS whitelabel_audit ",
                 "UPDATE whitelabel_settings ", "ON whitelabel_audit ("):
        assert bare not in code, f"存在未 public 限定的对象引用: {bare}"
    # 新列齐备
    for col in ("backoffice_brand_unlocked", "backoffice_brand_granted_by",
                "backoffice_brand_granted_at", "brand_version"):
        assert col in sql
    # D2 既有 oem 映射（保留真正已授权者 · 不无差别收回）
    assert "WHERE whitelabel_mode = 'oem'" in sql
    assert "whitelabel_status = 'active'" in sql
    assert "unlocked_by_admin IS TRUE" in sql
    # D2 · ID 132 固定 customer-only
    assert "WHERE user_id = 132" in sql
    # 审计表 append-only 注释 + 索引
    assert "APPEND-ONLY" in sql or "append-only" in sql
    # 无破坏性语句（仅看非注释行 · 注释里的"不 DROP/不 TRUNCATE"声明不算）
    code_lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    code = "\n".join(code_lines)
    for banned in ("DROP TABLE", "TRUNCATE", "RENAME COLUMN"):
        assert banned not in code


def test_startup_selfcheck_registers_new_columns():
    """启动自检（_WHITELABEL_BACKOFFICE_COLUMNS + init 建审计表）覆盖新列/新表。"""
    from api.referral_api import _WHITELABEL_BACKOFFICE_COLUMNS, _WHITELABEL_EXPECTED_COLUMNS

    cols = {c for c, _t in _WHITELABEL_BACKOFFICE_COLUMNS}
    assert cols == {"backoffice_brand_unlocked", "backoffice_brand_granted_by",
                    "backoffice_brand_granted_at", "brand_version"}
    assert cols <= _WHITELABEL_EXPECTED_COLUMNS
    # 7 基表列 + 14 v3.6 列（含 brand_terms_accepted_at）+ 4 板块C列 = 25
    assert len(_WHITELABEL_EXPECTED_COLUMNS) == 25

    import inspect
    from api import referral_api as ra
    src = inspect.getsource(ra.init_referral_tables)
    assert "CREATE TABLE IF NOT EXISTS whitelabel_audit" in src
    assert "idx_whitelabel_audit_user_created" in src


# ============================================================
# 前端静态断言（hook backoffice 判定 / portal localStorage 隔离 / admin 授权控件）
# ============================================================

def _frontend_src(rel: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "frontend" / "src" / rel).read_text(encoding="utf-8")


def test_frontend_hook_exposes_backoffice_brand_allowed_fail_closed():
    src = _frontend_src("hooks/useWhitelabel.ts")
    assert "backofficeBrandAllowed" in src
    # fail-closed 默认 false
    assert "backoffice_brand_allowed === true" in src


def test_frontend_internal_surfaces_gate_on_backoffice():
    """内部 8+ 消费点经 hook 判定层收口：product_name（OEM 字段）只能 backofficeBrandAllowed 出。"""
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] 原 hook 判定层(助手名 hook)的消费方全在旧对话 UI 里,随之成孤儿删除;在役两处入口照查
    sidebar = _frontend_src("components/layout/AppSidebar.tsx")
    assert "backofficeBrandAllowed" in sidebar
    layout = _frontend_src("components/layout/Layout.tsx")
    assert "backofficeBrandAllowed" in layout


def test_frontend_portal_owner_key_scoped_and_swept():
    """portal_owner_user_id 全局 key 已废除；登录/登出前缀清扫一切 portal_owner_user_id* 残留。"""
    login = _frontend_src("pages/Portal/PortalLogin.tsx")
    dash = _frontend_src("pages/Portal/PortalDashboard.tsx")
    for name, src in (("PortalLogin", login), ("PortalDashboard", dash)):
        assert "setItem('portal_owner_user_id'" not in src, name
        assert 'setItem("portal_owner_user_id"' not in src, name
        assert "getItem('portal_owner_user_id')" not in src, name  # 不再读全局 key（跨账号残留源）
    # 前缀清扫函数定义于 PortalDashboard 并在两侧调用（登出 clearPortalSession + 登录成功）
    assert "function sweepLegacyPortalOwnerKeys" in dash
    assert "sweepLegacyPortalOwnerKeys();" in dash
    assert "sweepLegacyPortalOwnerKeys" in login


def test_frontend_admin_grant_has_backoffice_control():
    src = _frontend_src("pages/Admin/WhitelabelGrant.tsx")
    assert "backoffice_brand_unlocked" in src
    assert "后台换肤" in src


def test_frontend_whitelabel_settings_shows_version_and_audit():
    src = _frontend_src("pages/Agent/WhitelabelSettings.tsx")
    assert "brand_version" in src
    assert "last_audit_at" in src
    # URL 校验 UI：协议/后缀人话提示
    assert "https" in src and "仅支持" in src


def test_frontend_no_lookbehind_in_changed_files():
    """板块 C 改动的前端文件禁止正则 lookbehind（旧 iOS/微信 WebView 会炸）。"""
    for rel in ("hooks/useWhitelabel.ts",
                "pages/Portal/PortalLogin.tsx", "pages/Portal/PortalDashboard.tsx",
                "pages/Admin/WhitelabelGrant.tsx", "pages/Agent/WhitelabelSettings.tsx"):
        src = _frontend_src(rel)
        assert "(?<!" not in src and "(?<=" not in src, rel


def test_discriminative_agent_surface_guard_removal_would_fail():
    """区分性证明：删掉 backoffice 守卫（backoffice_allowed 缺省 False）agent surface 必为平台。

    若有人把 _can_show_brand 的 agent 分支改回 mode=='oem' 即真，本测试转红。
    """
    from services.public_whitelabel import _can_show_brand

    assert _can_show_brand("agent", "oem") is False, "守卫被移除：oem 不再隐含后台换肤（D2）"
    assert _can_show_brand("agent", "oem", backoffice_allowed=True) is True


# ============================================================
# 集中严审 R3 · P2:whitelabel_audit append-only DB 层强制
# （对标 monitoring_identity_decision_events 既有触发器模式）
# ============================================================

def test_migration_enforces_whitelabel_audit_append_only_trigger():
    """migration 必须含 append-only 触发器定义（幂等可连跑）。"""
    root = Path(__file__).resolve().parents[1]
    sql = (root / "scripts" / "migration_whitelabel_backoffice_scope_2026_07_22.sql").read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION public.trg_whitelabel_audit_append_only()" in sql
    assert "RAISE EXCEPTION" in sql
    assert "DROP TRIGGER IF EXISTS trg_whitelabel_audit_append_only" in sql
    assert "CREATE TRIGGER trg_whitelabel_audit_append_only" in sql
    assert "BEFORE UPDATE OR DELETE ON public.whitelabel_audit" in sql
    assert "EXECUTE FUNCTION public.trg_whitelabel_audit_append_only();" in sql


def test_rollback_drops_trigger_and_function_before_archiving():
    """rollback 必须先在 RENAME 归档前清理触发器/函数（否则归档表仍挂触发器）。"""
    root = Path(__file__).resolve().parents[1]
    sql = (root / "scripts" / "rollback_whitelabel_backoffice_scope_2026_07_22.sql").read_text(encoding="utf-8")

    drop_trigger = "DROP TRIGGER IF EXISTS trg_whitelabel_audit_append_only ON public.whitelabel_audit;"
    drop_function = "DROP FUNCTION IF EXISTS public.trg_whitelabel_audit_append_only();"
    assert drop_trigger in sql
    assert drop_function in sql
    rename_pos = sql.index("RENAME TO whitelabel_audit_archived_20260722")
    assert sql.index(drop_trigger) < rename_pos
    assert sql.index(drop_function) < rename_pos


def test_startup_selfcheck_covers_append_only_trigger():
    """init_referral_tables 启动自检必须挂 append-only drift 检查（建审计表之后）。"""
    import inspect

    from api import referral_api as ra

    src = inspect.getsource(ra.init_referral_tables)
    create_pos = src.index("CREATE TABLE IF NOT EXISTS whitelabel_audit")
    check_pos = src.index("_ensure_whitelabel_audit_append_only(cursor)")
    assert create_pos < check_pos


class _TriggerFakeCursor:
    """pg_trigger 查询按 trigger_exists/self_heal_works 编排;其余语句记录日志。"""

    def __init__(self, *, trigger_exists: bool, self_heal_works: bool = True):
        self.trigger_exists = trigger_exists
        self.self_heal_works = self_heal_works
        self.executed: list[str] = []
        self._row = None

    @staticmethod
    def _norm(sql) -> str:
        return " ".join(str(sql).split()).lower()

    def execute(self, sql, params=None):
        text = self._norm(sql)
        self.executed.append(text)
        if text.startswith("select") and "pg_trigger" in text:
            healed = self.self_heal_works and any(
                stmt.startswith("create trigger") for stmt in self.executed
            )
            self._row = {"tgname": "trg_whitelabel_audit_append_only"} if (
                self.trigger_exists or healed
            ) else None
        else:
            self._row = None
        return self

    def fetchone(self):
        return self._row


def test_append_only_selfcheck_noop_when_trigger_present():
    """触发器存在 → no-op(不发任何 CREATE/DROP)。"""
    from api.referral_api import _ensure_whitelabel_audit_append_only

    cur = _TriggerFakeCursor(trigger_exists=True)
    _ensure_whitelabel_audit_append_only(cur)
    assert not any(stmt.startswith(("create", "drop")) for stmt in cur.executed)


def test_append_only_selfcheck_self_heals_when_missing():
    """触发器缺失 → 幂等自建(CREATE OR REPLACE FUNCTION + DROP IF EXISTS + CREATE TRIGGER)。"""
    from api.referral_api import _ensure_whitelabel_audit_append_only

    cur = _TriggerFakeCursor(trigger_exists=False)
    _ensure_whitelabel_audit_append_only(cur)
    assert any("create or replace function public.trg_whitelabel_audit_append_only()" in s for s in cur.executed)
    assert any(s.startswith("drop trigger if exists trg_whitelabel_audit_append_only") for s in cur.executed)
    assert any(
        s.startswith("create trigger trg_whitelabel_audit_append_only")
        and "before update or delete on public.whitelabel_audit" in s
        for s in cur.executed
    )


def test_append_only_selfcheck_fail_closed_when_self_heal_fails():
    """自建后复查仍缺 → RuntimeError(fail-closed,不静默降级)。"""
    from api.referral_api import _ensure_whitelabel_audit_append_only

    cur = _TriggerFakeCursor(trigger_exists=False, self_heal_works=False)
    with pytest.raises(RuntimeError, match="append-only"):
        _ensure_whitelabel_audit_append_only(cur)


def test_migration_manifest_registers_whitelabel_scope_migration():
    """板块 C 迁移必须在 manifest(否则 prestart 跳过 → 触发器/新列缺失 → 启动自检 fail)。"""
    from db.migration_manifest import MIGRATIONS

    assert "scripts/migration_whitelabel_backoffice_scope_2026_07_22.sql" in MIGRATIONS
