from __future__ import annotations

from pathlib import Path
import asyncio
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_PUBLIC_KEYS = {
    "agent_user_id",
    "upstream_user_id",
    "service_account_code",
    "channel_account_code",
    "resolved_user_id",
    "relationship_id",
    "relationship_version",
    "cost_multiplier",
    "cost_multiplier_bps",
    "seller_user_id",
    "responsible_service_user_id",
    "seller_account_code",
    "responsible_service_account_code",
    "counterparty_account_code",
    "seller_cost_basis_cents",
}


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def test_customer_and_agent_service_dtos_exclude_relationship_identity():
    from schemas.public_contracts import AgentServiceDTO, CustomerServiceDTO

    customer = CustomerServiceDTO(
        service_status="platform_managed",
        configuration_status="ready",
        account_configured=True,
        dispute_pending=False,
    ).model_dump()
    agent = AgentServiceDTO(
        service_status="platform_managed",
        configuration_status="ready",
    ).model_dump()

    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(set(_walk_keys(customer)))
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(set(_walk_keys(agent)))
    assert customer["service_status"] == "platform_managed"


def test_admin_relationship_dto_preserves_audit_trace():
    from schemas.public_contracts import AdminRelationshipDTO

    dto = AdminRelationshipDTO(
        customer_user_id=40,
        service_user_id=17,
        relationship_id=9,
        relationship_version="rel-v3",
        resolution="BOUND",
        dispute_status=None,
    ).model_dump()

    assert dto["service_user_id"] == 17
    assert dto["relationship_id"] == 9
    assert dto["relationship_version"] == "rel-v3"


def test_public_whitelabel_selfserve_customer_brand_and_governed_oem():
    from services.public_whitelabel import public_branding_from_record

    base = {
        "company_name": "已批准品牌",
        "logo_url": "https://static.example/brand.png",
        "whitelabel_mode": "external_only",
        "whitelabel_status": "active",
        "unlocked_by_admin": False,
    }
    approved = public_branding_from_record(base, surface="customer")
    assert approved["display_scope"] == "approved_whitelabel"
    assert approved["brand"]["company_name"] == "已批准品牌"
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(set(_walk_keys(approved)))

    assert public_branding_from_record(
        {**base, "whitelabel_mode": "oem", "unlocked_by_admin": False}, surface="customer"
    )["display_scope"] == "platform"
    assert public_branding_from_record(
        {**base, "whitelabel_mode": "oem", "unlocked_by_admin": True}, surface="customer"
    )["display_scope"] == "approved_whitelabel"
    assert public_branding_from_record(
        {**base, "whitelabel_status": "suspended"}, surface="customer"
    )["display_scope"] == "platform"
    assert public_branding_from_record(
        {**base, "logo_url": None}, surface="customer"
    )["display_scope"] == "platform"


def test_legacy_account_id_logo_cannot_activate_public_whitelabel():
    from services.public_whitelabel import public_branding_from_record

    result = public_branding_from_record(
        {
            "company_name": "历史品牌",
            "logo_url": "/uploads/whitelabel-logos/173/logo.png",
            "whitelabel_mode": "oem",
            "whitelabel_status": "active",
            "unlocked_by_admin": True,
        },
        surface="customer",
    )

    assert result["display_scope"] == "platform"
    assert "173" not in result["brand"]["logo_url"]


def test_agent_whitelabel_serializer_hides_legacy_account_id_logo():
    from api.referral_api import _serialize_agent_whitelabel_config

    result = _serialize_agent_whitelabel_config(
        {
            "company_name": "历史品牌",
            "logo_url": "/uploads/whitelabel-logos/173/logo.png",
            "company_logo_url": "/uploads/whitelabel-logos/173/logo.png",
            "whitelabel_mode": "oem",
            "whitelabel_status": "active",
            "unlocked_by_admin": True,
        }
    )

    assert result["logo_url"] is None
    assert result["company_logo_url"] is None
    assert result["display_scope"] == "platform"
    assert result["configuration_status"] == "draft"


def test_new_whitelabel_logo_path_is_opaque(monkeypatch, tmp_path):
    import inspect

    from api.referral_api import _save_whitelabel_logo_for_user

    monkeypatch.chdir(tmp_path)
    url = _save_whitelabel_logo_for_user(173, b"\x89PNG\r\n\x1a\n" + b"x" * 20)

    assert re.fullmatch(r"/uploads/whitelabel-logos/[A-Za-z0-9]+/logo_\d+_[a-f0-9]+\.png", url)
    opaque_bucket = url.split("/")[3]
    assert opaque_bucket != "173"
    source = inspect.getsource(_save_whitelabel_logo_for_user)
    path_logic = source[source.index("opaque_bucket ="):]
    assert "str(target_user_id)" not in path_logic
    assert "f\"{target_user_id}" not in path_logic
    assert (tmp_path / url.lstrip("/")).is_file()


def test_legacy_snapshot_without_approval_marker_falls_back_to_platform():
    from services.public_whitelabel import resolve_branding_context

    result = resolve_branding_context(
        surface="customer",
        snapshot={
            "company_name": "旧快照品牌",
            "logo_url": "https://static.example/legacy.png",
        },
    )

    assert result["display_scope"] == "platform"
    assert result["brand"]["company_name"].startswith("OmniRank")


def test_anonymous_quote_api_read_time_allowlist_strips_historical_internal_fields(monkeypatch):
    from api import referral_api

    class Cursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return {
                "services": [{
                    "name": "平台优化服务", "quantity": 1, "unit_price": 100,
                    "subtotal": 100, "seller_user_id": 28,
                    "service_account_code": "SV-SECRET", "upstream_cost": 1,
                }],
                "total_price": 100,
                "whitelabel": {
                    "company_name": "已批准品牌",
                    "logo_url": "https://static.example/brand.png",
                    "slogan": "品牌标语",
                    "contact_name": "真实联系人", "phone": "13800000000",
                    "wechat": "secret-wx", "whitelabel_mode": "oem",
                    "unlocked_by_admin": True, "admin_approved": True,
                },
                "created_at": "2026-07-15T00:00:00Z",
            }

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    monkeypatch.setattr(referral_api, "get_connection", lambda: Connection())
    response = asyncio.run(referral_api.get_public_quote("historical-dirty"))
    quote = response["quote"]
    assert quote["services"] == [{
        "name": "平台优化服务", "quantity": 1.0,
        "unit_price": 100.0, "subtotal": 100.0,
    }]
    assert set(quote["whitelabel"]) == {"company_name", "logo_url", "slogan"}
    serialized = repr(response)
    for secret in ("seller_user_id", "SV-SECRET", "真实联系人", "13800000000", "secret-wx", "whitelabel_mode"):
        assert secret not in serialized


def test_all_non_admin_frontend_surfaces_have_no_upstream_contract_fields():
    """Scan public UI trees broadly; adding a new page cannot evade this gate."""
    frontend = ROOT / "frontend" / "src"
    roots = [
        frontend / "pages",
        frontend / "components",
        frontend / "hooks",
        frontend / "context",
        frontend / "services",
    ]
    candidates = set()
    for root in roots:
        candidates.update(root.rglob("*.ts"))
        candidates.update(root.rglob("*.tsx"))

    public_files = sorted(
        path for path in candidates
        if "Admin" not in path.relative_to(frontend).parts
    )
    assert public_files
    forbidden = (
        "service_account_code",
        "channel_account_code",
        "agent_display_name",
        "agent_referral_code",
        "resolved_user_id",
        "bound_agent_user_id",
        "直属渠道",
        "绑定服务方",
        "联系服务方",
        "由服务方提供",
        "上级服务商",
        "尚未绑定服务方",
        "价格和包装由服务方设置",
        "seller_account_code",
        "responsible_service_account_code",
        "counterparty_account_code",
        "sellerAccountCode",
        "直属卖方",
        "公开编号待补齐",
    )
    violations = []
    for path in public_files:
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                violations.append(f"{path.relative_to(ROOT)}: {token}")
    assert violations == []


def test_production_build_runs_public_privacy_bundle_guard():
    package = (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    guard = ROOT / "frontend" / "scripts" / "verify-public-privacy-bundle.mjs"

    assert "verify-public-privacy-bundle.mjs" in package
    source = guard.read_text(encoding="utf-8")
    for token in (
        "omnirank_locked_ref",
        "portal_owner_user_id",
        "尚未绑定服务方",
        "价格和包装由服务方设置",
        "referral query URL",
        "upstream seller account key",
        "responsible service account key",
        "counterparty account key",
        "upstream seller copy",
    ):
        assert token in source


def test_public_pdf_and_share_url_do_not_encode_relationship_identity():
    footer = (ROOT / "services/report_v3_pdf_footer.py").read_text(encoding="utf-8")
    public_hook = (ROOT / "frontend/src/hooks/useWhitelabel.ts").read_text(encoding="utf-8")

    assert "?shared_by=" not in footer
    assert "/api/public/whitelabel/${" not in public_hook


def test_wallet_serializer_drops_settlement_principal_and_nested_private_fields():
    from api.wallet_api import _serialize_public_wallet

    result = _serialize_public_wallet({
        "paid_points": 100,
        "agent_user_id": 17,
        "bound_agent_user_id": 17,
        "customer_credit": {
            "tool_credit_points": 80,
            "publish_credit_points": 20,
            "agent_user_id": 17,
            "upstream_user_id": 9,
        },
    })

    assert result["paid_points"] == 100
    assert result["customer_credit"]["tool_credit_points"] == 80
    assert result["customer_credit"]["publish_credit_points"] == 20
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(set(_walk_keys(result)))


def test_shared_m3_responses_do_not_serialize_brand_owner():
    source = (ROOT / "api/m3_api.py").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend/src/services/m3/api.ts").read_text(encoding="utf-8")

    assert '"owner_user_id": r.get("owner_user_id")' not in source
    assert '"owner_user_id": brand.get("owner_user_id")' not in source
    assert "owner_user_id?: number" not in frontend


def test_public_brand_status_response_does_not_return_owner_id():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    marker = "async def api_update_brand_status"
    block = source[source.index(marker):]
    block = block[:block.index('@app.get("/api/brands/{brand_id}/materials")')]

    assert '"owner_user_id": row.get("owner_user_id")' not in block


def test_register_posters_do_not_return_inviter_identity():
    source = (ROOT / "api/share_api.py").read_text(encoding="utf-8")
    start = source.index("def _build_register_poster")
    end = source.index("def _build_interview_poster")
    block = source[start:end]

    assert "sharer_name" not in block
    assert "sharer_code" not in block
    assert "display_name" not in block
    assert 'target_url += f"?ref=' not in block
    assert '"omnirank_invite_attribution"' in source


def test_agent_promotion_uses_opaque_short_link_not_referral_query():
    source = (ROOT / "api/agent_workbench_api.py").read_text(encoding="utf-8")
    start = source.index("async def agent_promotion_qrcode")
    end = source.index('@router.get("/promotion/customers"')
    block = source[start:end]

    assert '"register"' in block
    assert 'ref_link = f"{public_origin}/api/sl/{short_code}"' in block
    assert "/invite?ref=" not in block


def test_login_and_invite_pages_do_not_persist_referral_relationship():
    paths = [
        ROOT / "frontend/src/pages/Invite/InvitePage.tsx",
        ROOT / "frontend/src/pages/Login/LoginPage.tsx",
        ROOT / "frontend/src/pages/Login/RegisterPage.tsx",
    ]
    sources = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "setItem('omnirank_locked_ref'" not in sources
    assert 'setItem("omnirank_locked_ref"' not in sources


def test_public_registration_copy_describes_attribution_not_commercial_binding():
    source = (ROOT / "api/auth_api.py").read_text(encoding="utf-8")
    registration = source[
        source.index("# 4. 记录注册邀请归属"):
        source.index("# 5. 签发 JWT Token")
    ]

    assert "建立商业关系" not in registration
    assert "商业绑定事务" not in registration
    assert "邀请归因不会重复写入" in registration


def test_procurement_quote_error_is_generic_on_agent_surface():
    from api.agent_workbench_api import _public_procurement_quote_error

    private_detail = (
        "channel_account_code=CH-00017 upstream_user_id=9 "
        "upstream_cost_basis_cents=123 relationship_version=secret-v4"
    )
    error = _public_procurement_quote_error(RuntimeError(private_detail), "test")

    assert error.status_code == 422
    assert error.detail == {
        "code": "PROCUREMENT_QUOTE_INVALID",
        "message": "当前进货报价已失效或不可用，请刷新后重试",
    }
    assert not [token for token in FORBIDDEN_PUBLIC_KEYS if token in str(error.detail)]
    assert "upstream" not in str(error.detail).lower()


def test_dealer_resale_public_serializers_hide_upstream_identity_and_cost():
    from services.dealer_inventory_resale import _serialize_order

    row = {
        "order_id": "ORDER-PRIVATE-UPSTREAM",
        "seller_user_id": 17,
        "buyer_user_id": 40,
        "refund_responsible_user_id": 17,
        "points": 100,
        "sale_amount_cents": 1200,
        "seller_cost_basis_cents": 1000,
        "margin_cents": 200,
        "downstream_markup_bps": 12000,
        "pricing_version": "retail-v1",
        "quote_id": "quote-v1",
        "state": "paid",
        "paid_at": None,
        "refund_deadline": None,
        "seller_lot_allocations": [],
        "relationship_version": "private-rel-v9",
        "source_kind": "direct_resale",
    }

    buyer = _serialize_order(None, row, actor_user_id=40, is_admin=False)
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(set(_walk_keys(buyer)))
    assert "refund_responsible_party" not in buyer
    assert buyer["refund_handling"] == "PLATFORM_MANAGED"

    seller = _serialize_order(None, row, actor_user_id=17, is_admin=False)
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(set(_walk_keys(seller)))
    assert seller["direction"] == "sale"

    admin = _serialize_order(None, row, actor_user_id=1, is_admin=True)
    assert admin["seller_user_id"] == 17
    assert admin["relationship_version"] == "private-rel-v9"


def test_brand_list_and_detail_use_non_admin_serializer_and_generic_errors():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start = source.index('@app.get("/api/brands")')
    end = source.index('@app.get("/api/brands/{brand_id}/latest-diagnosis-params")')
    block = source[start:end]

    assert "_serialize_brand_record_for_request(brand, request)" in block
    assert 'return {"error": str(e)}' not in block
    assert "品牌列表暂时无法加载" in block
    assert "品牌信息暂时无法加载" in block


def test_public_portal_renewal_never_returns_internal_exception_text():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start = source.index("def api_portal_renew_request")
    end = source.index('@app.get("/api/quotes/{quote_id}")')
    block = source[start:end]

    assert "detail=str(e)" not in block
    assert "续费申请暂时无法提交，请稍后重试" in block


@pytest.mark.parametrize(
    "relative_path",
    [
        "knowledge/system_kb/pages/customer-wallet.md",
        "knowledge/system_kb/pages/customer-recharge.md",
        "knowledge/system_kb/pages/agent-promotion.md",
    ],
)
def test_customer_help_content_does_not_describe_commercial_binding(relative_path: str):
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    forbidden = (
        "绑定服务方",
        "上级服务商",
        "直属渠道",
        "总部账号",
        "服务商绑定",
        "受谁邀请",
    )
    assert not [token for token in forbidden if token in source]


def test_public_report_shell_has_no_platform_brand_fallback():
    top_nav = (
        ROOT
        / "frontend/src/features/publicReportPremium/components/sections/TopNav.tsx"
    ).read_text(encoding="utf-8")
    footer = (
        ROOT
        / "frontend/src/features/publicReportPremium/components/sections/ReportFooter.tsx"
    ).read_text(encoding="utf-8")

    for source in (top_nav, footer):
        assert "OmniRank" not in source
        assert "全域上榜" not in source
    assert "GEO 诊断报告" in top_nav


def test_customer_brand_settings_do_not_require_platform_review_copy():
    source = (ROOT / "frontend/src/pages/Agent/WhitelabelSettings.tsx").read_text(
        encoding="utf-8"
    )

    assert "平台审核" not in source
    assert "待平台批准" not in source
    assert "保存后客户页面立即使用你的品牌" in source
