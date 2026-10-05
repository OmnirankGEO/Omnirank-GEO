from pathlib import Path

import pytest
from pydantic import ValidationError

from schemas.admin_user_governance import ChangeCommercialBindingRequest


ROOT = Path(__file__).resolve().parents[2]


def test_strong_request_rejects_missing_reason_bad_version_and_extra_fields():
    with pytest.raises(ValidationError):
        ChangeCommercialBindingRequest(expected_version=0, reason="有效原因", provider_user_id=28)
    with pytest.raises(ValidationError):
        ChangeCommercialBindingRequest(expected_version=1, reason=" ", provider_user_id=28)
    with pytest.raises(ValidationError):
        ChangeCommercialBindingRequest(
            expected_version=1, reason="有效原因", provider_user_id=28, upstream_user_id=102
        )


def test_public_pricing_ui_contains_no_upstream_identity_or_account_codes():
    public_files = [
        ROOT / "frontend" / "src" / "pages" / "Wallet" / "BuyCreditsSSOT.tsx",
        ROOT / "frontend" / "src" / "pages" / "Agent" / "ProcurementSSOT.tsx",
        ROOT / "frontend" / "src" / "pages" / "Customer" / "BuyCredit.tsx",
        ROOT / "frontend" / "src" / "pages" / "Agent" / "InventoryCenter.tsx",
        ROOT / "frontend" / "src" / "hooks" / "usePricingSSOT.ts",
    ]
    forbidden = (
        "service_account_code", "channel_account_code", "直属渠道", "结算渠道编号",
        "已绑定服务账号", "NO_CHANNEL_ACCOUNT",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in public_files)
    for token in forbidden:
        assert token not in combined


def test_registration_no_longer_assigns_deprecated_geo_writer():
    source = (ROOT / "api" / "auth_api.py").read_text(encoding="utf-8")
    registration_slice = source[
        source.index("仅分配普通用户兼容角色"):
        source.index("# 4. 记录注册邀请归属")
    ]
    assert "name = 'social_ops'" in registration_slice
    assert "name IN ('social_ops', 'geo_writer')" not in registration_slice
    assert "set_user_roles(user_id, role_ids)" in registration_slice


def test_legacy_role_and_ambiguous_relation_writers_are_retired():
    source = (ROOT / "api" / "admin_api.py").read_text(encoding="utf-8")
    assert "LEGACY_ROLE_WRITER_RETIRED" in source
    assert "LEGACY_AGENT_LEVEL_WRITER_RETIRED" in source
    assert "AMBIGUOUS_RELATIONSHIP_WRITER_RETIRED" in source
    assert "MODULE_OVERRIDES_RETIRED" in source
    ambiguous_slice = source[
        source.index('async def admin_set_upstream_service_provider'):
        source.index('def _assert_service_provider')
    ]
    assert "DELETE FROM referral_links" not in ambiguous_slice
    assert "customer_agent_bindings" not in ambiguous_slice


def test_legacy_password_reset_is_retired_without_secret_generation():
    source = (ROOT / "api" / "admin_api.py").read_text(encoding="utf-8")
    start = source.index("async def admin_reset_password")
    reset_slice = source[start:source.index("\n@router.", start)]

    assert "PASSWORD_RESET_ENDPOINT_RETIRED" in reset_slice
    assert "status_code=410" in reset_slice
    assert "_require_admin(request)" in reset_slice
    assert "generate" not in reset_slice.lower()
    assert "password_hash" not in reset_slice
    assert "new_password" not in reset_slice
    assert "temporary_password" not in reset_slice


def test_legacy_wallet_mutations_are_retired_without_ledger_writes():
    source = (ROOT / "api" / "admin_api.py").read_text(encoding="utf-8")
    start = source.index("def _retired_wallet_mutation")
    retired_slice = source[start:source.index("async def admin_refund_order", start)]

    assert "WALLET_MUTATION_ENDPOINT_RETIRED" in retired_slice
    assert "status_code=410" in retired_slice
    assert "_require_admin(request)" in retired_slice
    assert "UPDATE user_wallets" not in retired_slice
    assert "INSERT INTO point_transactions" not in retired_slice
    assert "add_points" not in retired_slice
    assert "deduct_points" not in retired_slice


def test_user_creation_cannot_bypass_platform_access_governance():
    source = (ROOT / "api" / "admin_api.py").read_text(encoding="utf-8")
    start = source.index("async def admin_create_user")
    create_slice = source[
        start:
        source.index("\n@router.", start)
    ]
    assert "ROLE_ASSIGNMENT_REQUIRES_GOVERNANCE_API" in create_slice
    assert "if req.role_ids:" in create_slice
    assert "effective_role_ids = [int(compatibility_role" in create_slice


def test_deprecated_role_navigation_is_hidden_but_route_component_remains():
    navigation_files = [
        ROOT / "frontend" / "src" / "components" / "layout" / "AppSidebar.tsx",
        ROOT / "frontend" / "src" / "components" / "layout" / "Sidebar.tsx",
        ROOT / "frontend" / "src" / "components" / "layout" / "MobileTabBar.tsx",
    ]
    assert all("/admin/roles" not in path.read_text(encoding="utf-8") for path in navigation_files)
    assert (ROOT / "frontend" / "src" / "pages" / "Admin" / "RoleManagement.tsx").exists()


def test_admin_dto_is_not_imported_by_public_api_modules():
    offenders = []
    for path in (ROOT / "api").glob("*_api.py"):
        if path.name == "admin_user_governance_api.py":
            continue
        if "schemas.admin_user_governance" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []


def test_user_management_has_no_direct_wallet_or_legacy_role_controls():
    source = (ROOT / "frontend" / "src" / "pages" / "Admin" / "UserManagement.tsx").read_text(encoding="utf-8")
    assert "/add-credits" not in source
    assert "/deduct-credits" not in source
    assert "/set-wallet" not in source
    assert "/module-overrides" not in source
    assert "/roles" not in source
    assert "历史兼容信息" in source
    assert "只修改当前商业服务绑定" in source
    commercial_title = source.index('title="当前商业服务归属"')
    relationship_section = source[
        source.rindex("detail.overview.business_identity === 'service_provider' ?", 0, commercial_title):
        source.index('title="平台直营准备状态"')
    ]
    assert "detail.overview.business_identity === 'service_provider' ?" in relationship_section
    # 🔴 [#140 2026-09-08] 这里原来断言页面上必须有
    #     「请先在“服务关系”中单独结束当前商业服务归属」
    # 已删除,**不是**换个串重锚。三态定性 = **锚过期**,而且是与一次刻意修复**互斥**的锚:
    #
    #   `b1b2075f3`(2026-07-25,`fix(admin): remove client-side promotion pre-block
    #   missed by all review rounds`)把这句话连同它背后的**客户端升级预拦**一起删了 ——
    #   上一版让「有商业归属的用户」在前端就被挡住,后端的原子转换永远收不到请求。
    #   同一笔加了 `tests/test_frontend_no_upgrade_preblock.py:24`:
    #     assert "单独结束当前商业服务归属" not in src
    #
    # ⇒ 两条锁读**同一个文件**、要求**相反**,永远不可能同时通过。
    #   SSOT 是 P0 那条(它保的是「别把前端预拦放回来」这个**行为**);
    #   本条保的是那句**提示文案**,而文案的前提已随预拦一起退役。
    #
    # 🔴 归属订正(Review 考古):我第一版把这笔记到了 `fedcf3373`(08-13)头上。
    #   实测 `git log -S`:`6ec4b8890`(07-15)同时加进页面与本判据;
    #   `b1b2075f3`(07-25)删句 + 加 preblock 锁;`fedcf3373` 的**父提交**里
    #   这句话已出现 **0** 次。⇒ 本断言**自 07-25 起就红**,不是 08-13。
    #   「具体但错误的归属」比笼统更糟:它会把下一个人引到 08-13 去找一个不存在的改动。
    # 🔴 顺带一个值得记的事实:它红了约六周没人发现 ——
    #   说明 `tests/admin_user_governance/` 不在任何常跑的分母里。
    # 🔴 特别记一笔:#140 的工单说本句「被改成了『请先在“概览”里…』」——
    #   那是**另一句话**(:448,说的是配定价前要先把用户调成服务商),主题都不同。
    #   照工单把锚重钉到那句上,会得到一把**看起来正常、实际什么都不保**的锁。
    #   本函数其余 10 条断言实测全部通过,过期的只有这一条。


def test_web_and_cron_only_verify_schema_and_do_not_add_request_time_ddl():
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    api = (ROOT / "api" / "admin_user_governance_api.py").read_text(encoding="utf-8")
    service = (ROOT / "services" / "admin_user_governance.py").read_text(encoding="utf-8")
    assert 'elif _SCHED_ROLE in ("web", "cron")' in server
    # [守卫单一来源 2026-07-30] 六道守卫的实现与顺序搬到 services/startup_schema_guards.py,
    # web/cron 分支改为调用那份唯一清单。诊断守卫必须仍在清单里 → 覆盖面不因搬家丢失。
    assert "run_fleet_schema_guards(log=logger)" in server
    from services.startup_schema_guards import FLEET_SCHEMA_GUARDS

    assert ("diagnosis", "verify_diagnosis_schema_fail_closed") in FLEET_SCHEMA_GUARDS
    assert "CREATE TABLE" not in api
    assert "ALTER TABLE" not in api
    assert "CREATE TABLE" not in service
    assert "ALTER TABLE" not in service


def test_non_admin_paths_cannot_arbitrarily_create_binding_and_registration_uses_verified_invitation():
    customer_binding = (ROOT / "services" / "customer_binding.py").read_text(encoding="utf-8")
    allocation_api = (ROOT / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    identity_service = (ROOT / "services" / "identity_service.py").read_text(encoding="utf-8")
    referral_api = (ROOT / "api" / "referral_api.py").read_text(encoding="utf-8")

    assert "CommercialBindingSubjectError" in customer_binding
    assert "SELECT agent_level FROM user_wallets" in customer_binding
    allocation = allocation_api.split("async def agent_allocate_offline", 1)[1].split(
        "async def agent_revoke_offline", 1
    )[0]
    assert "_require_customer_owned_by_agent" in allocation
    assert "COMMERCIAL_BINDING_REQUIRED" in allocation_api
    assert "upsert_customer_agent_binding" not in allocation
    assert 'binding_source="admin_manual"' not in allocation
    assert "solidify_service_provider_invitation(" in identity_service
    assert "solidify_service_provider_invitation(" in referral_api
    assert "lock_commercial_provider_for_assignment" in (
        ROOT / "services" / "commercial_service_routing.py"
    ).read_text(encoding="utf-8")
