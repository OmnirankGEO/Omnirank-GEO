from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from services import gap_operation_map as registry


REQUIRED_OPERATIONS = {
    "diagnosis_new": "/diagnosis/new",
    "quote_center": "/pricing",
    "writing_center": "/writing",
    "geo_content_center": "/marketing-materials",
    "publish_center": "/publish",
    "monitoring_center": "/monitoring",
    "client_list": "/my-clients",
    "brand_center": "/my-brand",
    "team_seats": "/organization/team",
    "wallet": "/wallet",
    "help_center": "/help",
}


def test_required_operations_have_complete_versioned_contract_and_real_routes():
    declared = registry.declared_frontend_routes()
    for operation_id, route in REQUIRED_OPERATIONS.items():
        entry = registry.resolve_operation(operation_id)
        assert entry is not None, operation_id
        assert entry.route_template == route
        assert route in declared
        assert entry.display_name and entry.synonyms and entry.allowed_roles
        assert entry.permissions and entry.help_target and entry.prerequisites
        assert entry.action_type and entry.version == registry.OPERATION_REGISTRY_VERSION


def test_two_diagnosis_phrasings_resolve_to_one_operation():
    first = registry.match_operation("帮我给我的品牌做一次AI搜索诊断")
    second = registry.match_operation("品牌体检在哪里")
    assert first is not None and second is not None
    assert first.operation_id == second.operation_id == "diagnosis_new"
    assert first.route_template == "/diagnosis/new"


def test_role_and_permission_filtering_covers_three_identities():
    admin = {"id": 1, "is_admin": True, "permissions": []}
    agent = {
        "id": 2, "agent_level": 1,
        "permissions": ["diagnosis:read", "quote:read", "writing:read", "monitoring:read"],
    }
    normal = {
        "id": 3, "agent_level": 0,
        "permissions": ["diagnosis:read", "quote:read", "writing:read", "monitoring:read"],
    }
    diagnosis = registry.resolve_operation("diagnosis_new")
    admin_only = registry.resolve_operation("admin_audit")
    assert all(registry.is_operation_allowed(diagnosis, user) for user in (admin, agent, normal))
    assert registry.is_operation_allowed(admin_only, admin)
    assert not registry.is_operation_allowed(admin_only, agent)
    assert not registry.is_operation_allowed(admin_only, normal)
    assert not registry.is_operation_allowed(diagnosis, {"id": 4, "permissions": []})


def test_employee_identity_comes_from_organization_guard_and_hides_commercial_entries():
    employee = SimpleNamespace(
        is_member=True,
        is_owner=False,
        capabilities=frozenset({
            "clients.read_assigned", "diagnosis.run", "quote.read_own",
            "writing.read_own", "publish.plan", "monitoring.read_assigned",
        }),
    )
    user = {"id": 9, "agent_level": 1, "operator_context": {"kind": "legacy"}}
    assert registry.actor_role(user, identity=employee) == registry.ROLE_MEMBER
    for operation_id in (
        "client_list", "diagnosis_new", "quote_center", "writing_center",
        "publish_center", "monitoring_center",
    ):
        assert registry.is_operation_allowed(
            registry.resolve_operation(operation_id), user, identity=employee
        ), operation_id
    for operation_id in ("wallet", "feature_pricing", "team_seats", "whitelabel", "agent_overview"):
        assert not registry.is_operation_allowed(
            registry.resolve_operation(operation_id), user, identity=employee
        ), operation_id


def test_legacy_operator_context_cannot_forge_employee_identity():
    forged = {"id": 10, "agent_level": 0, "operator_context": {"kind": "member"}}
    assert registry.actor_role(forged, identity=None) == registry.ROLE_NORMAL


def test_employee_assistant_routes_are_exactly_classified():
    from services.organization_route_contract import match_member_geo_route

    assert match_member_geo_route("POST", "/api/xiaobang/context").capability == "clients.read_assigned"
    assert match_member_geo_route("POST", "/api/xiaobang/chat").capability == "clients.read_assigned"
    assert match_member_geo_route("POST", "/api/admin/xiaobang/reindex") is None


def test_registry_mutant_without_diagnosis_is_killed():
    entries = tuple(
        entry for entry in registry.all_operations() if entry.operation_id != "diagnosis_new"
    )
    mutant = registry.OperationRegistry(entries)
    matched = mutant.match("帮我给我的品牌做一次AI搜索诊断")
    assert matched is None or matched.operation_id != "diagnosis_new"
    assert registry.match_operation("帮我给我的品牌做一次AI搜索诊断") is not None


def test_all_route_targets_follow_frontend_binding_contract():
    # 现行 P4 已拆为 GapPlanExecution 容器 + View；特殊锚点在四个真实渲染态。
    gap_plan_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("frontend/src/components/gapPlan").glob("*.tsx")
    )
    for entry in registry.all_operations():
        if entry.help_target == "gap-plan-section":
            assert 'data-help-target="gap-plan-section"' in gap_plan_source
            continue
        assert entry.help_target == registry.help_target_for_route(entry.route_template)


def test_visible_sidebar_routes_are_covered_by_the_registry():
    sidebar = Path("frontend/src/components/layout/AppSidebar.tsx").read_text(encoding="utf-8")
    declared = set(re.findall(r"to:\s*['\"]([^'\"]+)", sidebar))
    # /admin/managed 仍是 Owner 要求保留的关闭态能力，不是当前可见入口。
    visible = declared - {"/admin/managed"}
    assert visible <= registry.registry_route_paths()


def test_every_registered_route_resolves_to_a_current_frontend_page():
    declared = registry.declared_frontend_routes()
    for entry in registry.all_operations():
        assert urlsplit(entry.route_template).path in declared, entry.operation_id


def test_unknown_operation_and_url_are_never_signed():
    assert registry.resolve_operation("model_invented_operation") is None
    assert registry.match_operation("打开 https://evil.example/secret") is None
    assert registry.get_operation_registry().render_route(
        registry.resolve_operation("help_center"), {"brand_id": 1}
    ) == "/help"
