"""Registry v3 + 组织路由登记(规格 §3.3/§8 · 工单 §3.1)。

两条最容易被漏的判据在这里:

* **两条漏项**(``/admin/agent-inventory`` / ``/admin/media-directory``):
  它们在生产尖上让 ``test_visible_sidebar_routes_are_covered_by_the_registry``
  **是红的**。本文件再钉一次,并且钉的是"侧栏可见集合 ⊆ 注册表",
  而不是"这两条在不在" —— 钉具体两条的话,下次漏第三条照样绿。
* **路由登记成对**:已登记路径对 member 放行、未登记路径 fail-closed。
  只测其中一半,等于没测(全放行和全拒绝各能骗过一半)。
"""

from __future__ import annotations

import re
from pathlib import Path

from services import gap_operation_map as omap
from services.organization_route_contract import match_member_geo_route


# ── 版本协商 ──────────────────────────────────────────────────────────────
def test_registry_version_is_v3_and_keeps_v2_in_the_compatible_table():
    assert omap.OPERATION_MAP_VERSION == "operation-registry-v3"
    assert omap.OPERATION_MAP_VERSION == omap.OPERATION_REGISTRY_VERSION
    # 只加不删:删掉旧值 = 缓存里的旧 bundle 当场全量失配。
    assert "operation-registry-v2" in omap.OPERATION_REGISTRY_COMPATIBLE_VERSIONS
    assert omap.OPERATION_REGISTRY_VERSION in omap.OPERATION_REGISTRY_COMPATIBLE_VERSIONS


def test_assistant_meta_publishes_the_compatible_table():
    """前端要能靠数据协商,而不是自己写死一个常量(债务 §4.1-7)。"""
    from services.gap_assistant import AssistantAnswer

    meta = AssistantAnswer(headline="x", reasons=[], actions=[]).as_meta()
    payload = meta["gap_assistant"]
    assert payload["operation_map_version"] == omap.OPERATION_REGISTRY_VERSION
    assert payload["operation_map_compatible_versions"] == list(
        omap.OPERATION_REGISTRY_COMPATIBLE_VERSIONS
    )


def test_frontend_supported_versions_intersect_the_server_table():
    """前端声明的可渲染版本必须与服务端兼容表有交集,且含当前版本。

    两边各自写一份是必然的(一个在 py 一个在 ts),所以判据打在**交集**上:
    交集为空 = 线上一个动作按钮都不会出现,而两边单看都"没错"。
    """
    source = Path("frontend/src/hooks/useXiaobangChat.ts").read_text(encoding="utf-8")
    block = re.search(
        r"SUPPORTED_OPERATION_REGISTRY_VERSIONS[^=]*=\s*\[(.*?)\]", source, re.S
    )
    assert block, "前端没有声明支持版本表"
    declared = set(re.findall(r"'([^']+)'", block.group(1)))
    assert omap.OPERATION_REGISTRY_VERSION in declared
    assert declared & set(omap.OPERATION_REGISTRY_COMPATIBLE_VERSIONS)


# ── §3.3 两条漏项 ─────────────────────────────────────────────────────────
def test_every_visible_sidebar_route_is_registered():
    """判据钉的是**集合关系**,不是那两条具体路径。

    生产尖 ecce9985 上这条是红的(缺 /admin/agent-inventory 与
    /admin/media-directory)。钉具体两条的话,下次漏第三条照样绿。
    """
    sidebar = Path("frontend/src/components/layout/AppSidebar.tsx").read_text(
        encoding="utf-8"
    )
    declared = set(re.findall(r"to:\s*['\"]([^'\"]+)", sidebar))
    # /admin/managed 是 Owner 要求保留的关闭态能力,不是当前可见入口。
    visible = declared - {"/admin/managed"}
    assert visible, "侧栏路由集合为空 —— 判据的分母没了"
    missing = visible - set(omap.registry_route_paths())
    assert not missing, "侧栏可见但注册表没登记:{0}".format(sorted(missing))


def test_the_two_newly_registered_operations_point_at_real_frontend_routes():
    declared = omap.declared_frontend_routes()
    for operation_id, route in (
        ("admin_agent_inventory", "/admin/agent-inventory"),
        ("admin_media_directory", "/admin/media-directory"),
    ):
        entry = omap.resolve_operation(operation_id)
        assert entry is not None, operation_id
        assert entry.route_template == route
        assert route in declared, "注册了一条前端并不存在的路由"


# ── 命令合同登记面 ────────────────────────────────────────────────────────
def test_only_the_declared_operations_carry_a_command_contract():
    """登记面**逐条列名**,不许悄悄铺满。

    🔴 [WO-B2 ② 2026-08-20 · Owner 批] 从两条变三条:
       `geo_content_center` 登记只读/低危合同,唯一目的是让 `/marketing-materials`
       的深链 intent 产生得出来(规格 §12.3)。

       断言仍然是**等号**而不是 `>=`:WP1 的原话是「不同时铺三张空协议」,
       等号才拦得住"顺手再挂一条"。要加就得改这一行,而改这一行会被人看见。
    """
    ids = sorted(e.operation_id for e in omap.commandable_operations())
    assert ids == ["geo_content_center", "publish_center", "writing_center"], ids


def test_the_low_risk_registration_declares_no_billing_point():
    """🔴 Owner 的前提:素材页登记**不新增扣费点**。

    `billing_feature=None` 是那一位的物理落点 —— 没有它,
    `_resolve_compute_quote` 会去查目录价,报价链就走起来了。
    配一条反向对照:另外两条**有** billing_feature,证明这一位不是恒 None。
    """
    contracts = {e.operation_id: e.command_contract for e in omap.commandable_operations()}
    assert contracts["geo_content_center"].billing_feature is None, contracts
    assert contracts["geo_content_center"].executable is False, contracts
    assert "execute" not in contracts["geo_content_center"].adapters, contracts
    # 🔁 反向对照:这一位跟着合同走,不是所有合同都恒 None
    others = [c.billing_feature for oid, c in contracts.items() if oid != "geo_content_center"]
    assert others and all(others), others


def test_executable_and_adapter_registration_agree_both_ways():
    """🔴 [WO-B ② 2026-08-20 · 搬家改断言,不退役]

    原命题是「领域执行未开放:每条合同 executable=False 且没有 execute adapter」。
    发布那一条现在**开了**(图文合同链随 34 班并车上线),所以旧断言必须改 ——
    但不能退役成"随便":真正要守的不变量比原来那句更强,而且**双向**:

      · ``executable=False`` ⇒ 不许登记 execute adapter
        (登记了却不可执行 = 一条看得见却走不通的路);
      · ``executable=True``  ⇒ 必须登记 execute adapter,而且那个名字必须
        在 ``api/xiaobang_operations_api._execute_adapters()`` 的派发表里认得
        —— 否则 ``executable=True`` 是一句空话,端点会在最后一刻 500。

    分母从注册表机械枚举,不手写 operation 名。
    """
    from api.xiaobang_operations_api import _execute_adapters

    dispatch = _execute_adapters()
    assert dispatch, "派发表是空的 —— 下面那一支恒真"

    seen_executable = 0
    entries = list(omap.commandable_operations())
    assert entries, "注册表零 commandable —— 判据没有分母"
    for entry in entries:
        contract = entry.command_contract
        if not contract.executable:
            assert "execute" not in contract.adapters, (
                entry.operation_id + ":不可执行却登记了 execute adapter")
            continue
        seen_executable += 1
        name = (contract.adapters or {}).get("execute")
        assert name, entry.operation_id + ":executable=True 却没有 execute adapter"
        assert name in dispatch, (
            entry.operation_id + ":execute adapter " + str(name)
            + " 不在派发表里 —— executable=True 是空话,端点会在最后一刻 500")
        # external 之外的档现在还没有可当幂等键的东西(compute_only 没有确认回执)。
        assert contract.side_effect == "external", (
            entry.operation_id + ":只有 external 档有确认回执可作幂等键")
    assert seen_executable >= 1, (
        "一条 executable 都没有 —— 那这条判据的主分支从没被走到过")


def test_the_two_confirmation_tiers_are_both_represented():
    modes = {
        e.command_contract.side_effect: e.command_contract.confirmation_policy.mode
        for e in omap.commandable_operations()
    }
    assert modes == {
        "external": "required_user_click",
        "compute_only": "silent_with_notice",
    }


def test_discovery_hides_operations_the_member_has_no_capability_for():
    from types import SimpleNamespace

    user = {"id": 9, "agent_level": 1}
    with_publish = SimpleNamespace(
        is_member=True, is_owner=False,
        capabilities=frozenset({"publish.execute", "publish.plan"}),
    )
    without = SimpleNamespace(
        is_member=True, is_owner=False, capabilities=frozenset({"clients.read_assigned"}),
    )
    visible = {c["operation_id"] for c in omap.discover_commands(user, identity=with_publish)}
    assert "publish_center" in visible
    hidden = {c["operation_id"] for c in omap.discover_commands(user, identity=without)}
    assert "publish_center" not in hidden
    # 公共目录不得含 adapter 名(§8.2)。
    for item in omap.discover_commands(user, identity=with_publish):
        assert "adapters" not in item


# ── 组织路由登记(成对判据)──────────────────────────────────────────────
_INTENT = "xint_AbCdEf1234567890"
_EXECUTION = "xexe_AbCdEf1234567890"
_OP = "publish_center"

_REGISTERED = (
    ("POST", "/api/xiaobang/operations/{op}/query"),
    ("POST", "/api/xiaobang/operations/{op}/prepare"),
    ("GET", "/api/xiaobang/operations/{op}/intents/{intent}"),
    ("POST", "/api/xiaobang/operations/{op}/intents/{intent}/confirm"),
    ("POST", "/api/xiaobang/operations/{op}/intents/{intent}/cancel"),
    ("POST", "/api/xiaobang/operations/{op}/intents/{intent}/execute"),
    ("GET", "/api/xiaobang/operations/{op}/executions/{execution}/status"),
)


def _fill(path: str) -> str:
    return path.format(op=_OP, intent=_INTENT, execution=_EXECUTION)


def test_all_five_phase_endpoints_are_registered_for_member_seats():
    for method, template in _REGISTERED:
        policy = match_member_geo_route(method, _fill(template))
        assert policy is not None, (method, template)
        assert policy.capability, (method, template)
        assert policy.resource_kind == "xiaobang_intent", (method, template)


def test_unregistered_variants_stay_fail_closed():
    """成对的另一半。只测"已登记的能过"会被"全放行"骗过去。"""
    negatives = (
        # 方法不对
        ("GET", "/api/xiaobang/operations/{op}/prepare"),
        ("DELETE", "/api/xiaobang/operations/{op}/intents/{intent}"),
        # 路径形状不对(多一段 / 少一段 / 尾斜杠)
        ("POST", "/api/xiaobang/operations/{op}/intents/{intent}/approve"),
        ("POST", "/api/xiaobang/operations/{op}/intents/{intent}/confirm/"),
        ("POST", "/api/xiaobang/operations/{op}/query/extra"),
        # 前缀相同但不是登记路径
        ("POST", "/api/xiaobang/operations"),
        # 管理端重建接口:从来不该对员工开放
        ("POST", "/api/admin/xiaobang/reindex"),
        ("POST", "/api/admin/xiaobang/reindex-system"),
    )
    for method, template in negatives:
        assert match_member_geo_route(method, _fill(template)) is None, (method, template)


def test_opaque_id_shapes_are_constrained_not_wildcards():
    """占位不是 ``.*``:太短、错前缀、带路径分隔符的都不该匹配。"""
    bad_intents = ("xint_short", "xexe_AbCdEf1234567890", "../../etc/passwd", "1")
    for value in bad_intents:
        path = "/api/xiaobang/operations/{0}/intents/{1}".format(_OP, value)
        assert match_member_geo_route("GET", path) is None, value


def test_execute_requires_a_stronger_capability_than_planning():
    plan = match_member_geo_route("POST", _fill("/api/xiaobang/operations/{op}/prepare"))
    execute = match_member_geo_route(
        "POST", _fill("/api/xiaobang/operations/{op}/intents/{intent}/execute")
    )
    assert plan.capability == "publish.plan"
    assert execute.capability == "publish.execute"


# ── router 注册(防"静默跳过整个 router")────────────────────────────────
def test_operations_router_module_imports_and_hides_every_route_from_openapi():
    """🔴 server.py 里 router 注册包在 try/except 里:import 出错会被**静默跳过**,
    线上表现只是"端点 404"。这条测试直接 import,import 不了就当场红。
    顺带把 include_in_schema=False 逐条钉住(§10 · P1-8)。
    """
    from api.xiaobang_operations_api import router

    assert router.routes, "router 没有任何路由"
    for route in router.routes:
        assert getattr(route, "include_in_schema", True) is False, route.path
        assert route.path.startswith("/api/xiaobang/operations")


def test_server_registers_the_operations_router():
    source = Path("server.py").read_text(encoding="utf-8")
    assert "from api.xiaobang_operations_api import router as xiaobang_operations_router" in source
    assert "app.include_router(xiaobang_operations_router)" in source
