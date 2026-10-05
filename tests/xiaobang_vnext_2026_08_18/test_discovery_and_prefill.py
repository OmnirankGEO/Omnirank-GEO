"""能力发现端点 + 页面预填(规格 §8.2 / §12.3 · XO-03 · 工单 R3-P6 ③)。

## 本轮最容易写成假绿的三条,都在这里

1. **路由被吞**。``/api/xiaobang/operations/intents/{intent_id}/prefill`` 与
   ``/{operation_id}/intents/{intent_id}`` 形状相近,FastAPI 按声明顺序匹配。
   顺序错了**不报错**,只是静默走进另一个 handler。所以判据打真实路由解析
   (``app.router`` 的实际匹配结果),不打「我读代码觉得不会撞」。
2. **死函数**。``discover_commands`` 在 R3 只有测试在调 —— 测试全绿、
   线上没有这个能力。所以本文件带一条**调用方 census**:非测试调用方必须存在。
3. **过滤方向**。能力发现的判据必须成对:有权的**看得见** + 无权的**看不见**。
   只测一半的话,「全放行」和「全拒绝」各能骗过一半。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from services import gap_operation_map as omap
from services import xiaobang_page_prefill as prefill
from services.organization_route_contract import match_member_geo_route
from services.xiaobang_intent import (
    STATE_APPROVAL_PENDING,
    STATE_AWAITING_CONFIRMATION,
    STATE_CANCELLED,
    STATE_EXECUTION_LINKED,
    STATE_PREPARED,
)

CAPABILITIES_PATH = "/api/xiaobang/operations/capabilities"
PREFILL_PATH = "/api/xiaobang/operations/intents/{0}/prefill"
SAMPLE_INTENT = "xint_abcdefgh1234"


class _Identity:
    def __init__(self, capabilities=(), *, is_member=True, is_owner=False,
                 organization_id=7, payer_user_id=101):
        self.capabilities = tuple(capabilities)
        self.is_member = is_member
        self.is_owner = is_owner
        self.organization_id = organization_id
        self.payer_user_id = payer_user_id
        self.membership_version = "m1"
        self.assignment_authority_version = "a1"
        self.approval_policy_version = "p1"


# ══════════════════════════════════════════════════════════════════════════
# 一、路由真解析(不是"我读代码觉得不会撞")
# ══════════════════════════════════════════════════════════════════════════

def _resolve(method: str, path: str):
    """按 FastAPI 的真实顺序解析,返回命中的 endpoint 函数名。"""
    from api.xiaobang_operations_api import router

    for route in router.routes:
        if method not in getattr(route, "methods", ()):
            continue
        match = route.path_regex.match(path)
        if match:
            return route.endpoint.__name__, match.groupdict()
    return None, {}


def test_prefill_route_is_not_swallowed_by_the_operation_id_pattern():
    """🔴 本轮头号静默失败模式。

    ``/{operation_id}/intents/{intent_id}`` 声明在前的话,
    ``operations/intents/xint_xxx/prefill`` 会被解析成
    ``operation_id='intents'`` 走进 GET intent —— 不报错,只是答非所问。
    """
    name, params = _resolve("GET", PREFILL_PATH.format(SAMPLE_INTENT))
    assert name == "operation_intent_prefill", (name, params)
    assert params.get("intent_id") == SAMPLE_INTENT


def test_capabilities_route_is_not_swallowed_either():
    name, _ = _resolve("GET", CAPABILITIES_PATH)
    assert name == "operation_capabilities", name


def test_the_neighbouring_get_intent_route_still_resolves():
    """反向对照:新路由没有把旧路由挡掉。

    只验新路由通,挡掉旧路由的改动照样绿 —— 那是把一个 bug 换成另一个。
    """
    name, params = _resolve(
        "GET", "/api/xiaobang/operations/publish_center/intents/{0}".format(SAMPLE_INTENT)
    )
    assert name == "operation_get_intent", name
    assert params.get("operation_id") == "publish_center"


def test_new_endpoints_stay_out_of_the_public_openapi():
    """§10 P1-8:默认 ``/docs`` 会枚举内部路由,本网关全部 ``include_in_schema=False``。"""
    from api.xiaobang_operations_api import router

    for route in router.routes:
        assert getattr(route, "include_in_schema", True) is False, route.path


# ══════════════════════════════════════════════════════════════════════════
# 二、组织路由逐条登记(成对:登记的放行 + 没登记的 fail-closed)
# ══════════════════════════════════════════════════════════════════════════

def test_both_new_routes_are_registered_with_method_capability_and_resource():
    caps = match_member_geo_route("GET", CAPABILITIES_PATH)
    assert caps is not None, "能力发现没登记 → 员工席位一律 403"
    assert caps.capability == "clients.read_assigned"
    assert caps.resource_kind == "xiaobang_intent"

    pre = match_member_geo_route("GET", PREFILL_PATH.format(SAMPLE_INTENT))
    assert pre is not None, "预填没登记 → 员工点开 deep link 就是 403"
    assert pre.capability == "publish.plan"
    assert pre.resource_kind == "xiaobang_intent"


def test_the_registration_is_per_route_not_a_prefix():
    """🔴 工单原话「不放 prefix」。

    判据:同 namespace 下一条**没登记**的路径必须匹配不上。
    放了 prefix 的话这条会绿 —— 而 prefix 正是这张表存在的理由要防的东西。
    """
    # 🔴 M20 变异存活暴露的缺口:前三条探针的路径都带连字符或是空段,
    #    而这张表把未知占位编译成**数字主键**,所以「把某条改成 {operation_id}」
    #    这种真正的 prefix 化改动,它们一条都打不到。补一条**纯小写、无连字符**
    #    的未登记路径 —— 它正好落在 {operation_id} 的字符集里,prefix 化当场转红。
    assert match_member_geo_route("GET", "/api/xiaobang/operations/registry") is None
    assert match_member_geo_route("GET", "/api/xiaobang/operations/admin-rebuild") is None
    assert match_member_geo_route("POST", CAPABILITIES_PATH) is None      # 方法也逐条
    assert match_member_geo_route("GET", "/api/xiaobang/operations") is None
    # 现役但**未登记**的真实端点:员工侧必须 fail-closed。
    assert match_member_geo_route("GET", "/api/xiaobang/health") is None


def test_prefill_registration_only_admits_opaque_intent_ids():
    """路径正则必须只认 ``xint_`` 前缀的不可猜 id。

    兜底正则会把占位当数字主键 —— 那样员工侧永远匹配不上,
    失败形态与「没登记」一模一样(静默 403,查不出来)。
    """
    assert match_member_geo_route("GET", PREFILL_PATH.format("xint_abcdefgh")) is not None
    assert match_member_geo_route("GET", PREFILL_PATH.format("9")) is None
    assert match_member_geo_route("GET", PREFILL_PATH.format("../../etc")) is None


# ══════════════════════════════════════════════════════════════════════════
# 三、能力发现:调用方 census + 过滤成对 + 社媒负向锁
# ══════════════════════════════════════════════════════════════════════════

def test_discover_commands_has_a_non_test_caller():
    """🔴 死函数 census。

    R3 里 ``discover_commands`` 只有测试在调 —— 判据全绿而线上根本没有
    这个能力。所以这条数的是**非 tests/ 目录**的调用方。
    """
    root = Path(__file__).resolve().parents[2]
    callers = []
    for path in list(root.glob("api/*.py")) + list(root.glob("services/*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        if path.name == "gap_operation_map.py":
            continue                       # 定义处不算调用方
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == "discover_commands":
                    callers.append(path.name)
    assert callers, "discover_commands 仍然只有测试在调 = 死函数"


def test_capability_discovery_filters_both_directions():
    """成对判据:有该 capability 的看得见,没有的**连名字都看不到**。"""
    user = {"id": 101, "is_admin": False}
    contracts = omap.commandable_operations()
    assert contracts, "候选集为空 —— 后面两个断言会空即通过"
    needed = contracts[0].command_contract.required_capability

    visible = {c["operation_id"] for c in omap.discover_commands(
        user, identity=_Identity((needed, "clients.read_assigned")))}
    hidden = {c["operation_id"] for c in omap.discover_commands(
        user, identity=_Identity(("clients.read_assigned",)))}
    assert contracts[0].operation_id in visible
    assert contracts[0].operation_id not in hidden


def test_discovery_payload_carries_no_adapter_or_internal_url():
    """§8.2:adapter 名、内部 URL、HTTP method、供应商信息不进公共 DTO。"""
    user = {"id": 1, "is_admin": True}
    payloads = omap.discover_commands(user, identity=None)
    assert payloads
    blob = repr(payloads)
    for banned in ("adapter", "http_method", "POST /api", "endpoint_url"):
        assert banned not in blob, banned


def test_discovery_never_lists_a_social_domain_operation():
    """员工席位负向枚举锁(Owner 2026-08-17:社媒永久剔除)。"""
    user = {"id": 1, "is_admin": True}
    for item in omap.discover_commands(user, identity=None):
        blob = "{0}{1}".format(item.get("operation_id"), item.get("display_name"))
        for token in ("social", "社媒", "短视频"):  # 小写比对下 social 已覆盖社媒工作台英文名
            assert token.lower() not in blob.lower(), item


# ══════════════════════════════════════════════════════════════════════════
# 四、预填投影:主按钮两档 + 取消文案跟真实边界一致
# ══════════════════════════════════════════════════════════════════════════

def _row(state=STATE_PREPARED, *, side_effect="external", amount=None, preview=None):
    return {
        "intent_id": SAMPLE_INTENT,
        "intent_state": state,
        "intent_revision": 1,
        "side_effect": side_effect,
        "compute_quote_amount": amount,
        "compute_quote_unit": "算力",
        "compute_quote_expires_at": None,
        "preview": preview if preview is not None else {"customer_label": "测试客户"},
        "reason_facts": [{"reason_code": "low_coverage", "explanation": "这个词还没被推荐过"}],
        "domain_ref": {},
    }


def _projection(external="not_started", state=STATE_PREPARED):
    return {
        "intent_state": state,
        "intent_revision": 1,
        "domain_projection": {"state": "not_started", "reference": None},
        "settlement_projection": {"state": "none", "amount": None, "unit": "算力"},
        "external_projection": {"state": external},
    }


def test_primary_button_shows_the_number_only_when_it_is_a_real_quote():
    quoted = prefill.primary_action(
        _row(amount=390), needs_approval=False,
        compute={"state": "quoted", "amount": 390, "unit": "算力"})
    assert quoted["next_action_id"] == "confirm_then_run"
    assert "390" in quoted["label"] and "算力" in quoted["label"]

    unquoted = prefill.primary_action(
        _row(), needs_approval=False,
        compute={"state": "pending_domain_adapter", "amount": None, "unit": "算力"})
    assert unquoted["next_action_id"] == "confirm_then_run"
    # 🔴 报不出价就绝不写一个数字:用户会按那个数字做决定。
    assert not re.search(r"\d", unquoted["label"]), unquoted


def test_primary_button_switches_to_the_approval_wording():
    action = prefill.primary_action(
        _row(), needs_approval=True,
        compute={"state": "quoted", "amount": 390, "unit": "算力"})
    assert action["next_action_id"] == "submit_for_approval"
    assert "暂不使用算力" in action["label"]
    # 需审批档**不许**出现算力数字 —— 点它不花钱。
    assert "390" not in action["label"]


@pytest.mark.parametrize(
    "state, external, expected_kind",
    [
        (STATE_PREPARED, "not_started", "cancel_free"),
        (STATE_AWAITING_CONFIRMATION, "not_started", "cancel_free"),
        (STATE_APPROVAL_PENDING, "not_started", "cancel_free"),
        (STATE_EXECUTION_LINKED, "started", "view_progress"),
        (STATE_EXECUTION_LINKED, "unknown", "manual_reconcile"),
        (STATE_CANCELLED, "not_started", "none"),
    ],
)
def test_cancel_wording_matches_the_real_boundary(state, external, expected_kind):
    action = prefill.cancel_action(_row(state), _projection(external, state))
    assert action["next_action_id"] == expected_kind, action


def test_unknown_external_state_never_promises_cancelled_or_refunded():
    """🔴 §12.3 原话:external unknown 时不能承诺已取消、已退款或可安全重试。

    判据故意用**裸子串**而不是"承诺句式":在这一支里,这几个词无论以什么
    句式出现都是风险 —— 用户只会记住那四个字。第一版文案写的是
    「先别当成已取消或已退款」,语义正确却仍然把这四个字摆在了用户眼前,
    被这条当场判红,已改成不含这些词的说法。
    """
    action = prefill.cancel_action(_row(STATE_EXECUTION_LINKED), _projection("unknown"))
    blob = "{0}{1}".format(action.get("label"), action.get("note"))
    for promise in ("已取消", "已退款", "可以重试", "安全重试"):
        assert promise not in blob, blob


def test_compute_only_declares_an_empty_external_action_list_not_none():
    """空列表和「还没算出来」必须能区分 —— 都返回 ``None`` 的话前端只能猜。"""
    payload = prefill.build_prefill(
        _row(side_effect="compute_only"), entry=omap.resolve_operation("publish_center"),
        projection=_projection())
    assert payload["external_actions"] == []

    external = prefill.build_prefill(
        _row(side_effect="external"), entry=omap.resolve_operation("publish_center"),
        projection=_projection())
    assert external["external_actions"], "external 档必须列出或说明对外动作"


def test_prefill_covers_every_field_12_3_requires_above_the_main_button():
    """§12.3 的同屏可见清单 —— 逐项都要在合同里,缺一项就是页面显示不出来。"""
    payload = prefill.build_prefill(
        _row(amount=390), entry=omap.resolve_operation("publish_center"),
        projection=_projection(), quote_state="quoted")
    for key in ("customer", "object", "recommendation", "external_actions",
                "channel", "visibility", "payer", "compute",
                "primary_action", "cancel_action", "secondary_actions",
                "form_prefill", "rebind_hint"):
        assert key in payload, key
    assert payload["recommendation"]["reasons"], "推荐原因丢了 = XO-05 落不了地"
    assert payload["rebind_hint"]["primary_label"] == "重新计算并核对"


def test_action_ids_do_not_collide_with_the_adapter_naming_convention():
    """🔴 DLP 把 ``<domain>_<object>_<phase>``(phase ∈ 五阶段)当内部 adapter 名。

    第一版主按钮 id 写作 ``confirm_and_execute``,正好撞上 ``..._execute``,
    被 DLP 当场判成 adapter 泄漏。处置是**改自己的名字**,不是放宽那条共享规则 ——
    放宽一次,真的 adapter 名以后就跟着漏出去。这条把该决定钉住。
    """
    payload = prefill.build_prefill(
        _row(amount=390), entry=omap.resolve_operation("publish_center"),
        projection=_projection(), quote_state="quoted")
    ids = [payload["primary_action"]["next_action_id"],
           payload["cancel_action"]["next_action_id"]]
    ids += [a["next_action_id"] for a in payload["secondary_actions"]]
    banned_suffixes = ("_query", "_prepare", "_confirm", "_cancel", "_execute", "_status")
    for action_id in ids:
        assert not action_id.endswith(banned_suffixes), action_id
    # 反向对照:撞名的那个确实会被 DLP 打红(否则这条是空断言)。
    from services.xiaobang_facade_dlp import assert_facade_clean
    with pytest.raises(Exception):
        assert_facade_clean({"next_action_id": "confirm_and_execute"}, where="neg")


def test_prefill_payload_passes_the_facade_dlp():
    """出口必须过 DLP-A;顺带证明 DLP 对本 DTO 不是恒绿。"""
    from services.xiaobang_facade_dlp import assert_facade_clean

    payload = prefill.build_prefill(
        _row(amount=390), entry=omap.resolve_operation("publish_center"),
        projection=_projection(), quote_state="quoted")
    assert_facade_clean(payload, where="test_prefill")

    # 🔴 负样本必须选**这套扫描器真的会命中**的形态,否则「没抛」会被读成
    #    「DTO 很干净」。实测:成本词 + 金额会命中;而供应商名「快易播」、
    #    「采购价 12 元」、上游单号 `KYB20260819001`、值里的 provider_route
    #    **都不命中** —— 那是 DLP-A 现役扫描器的存量洞,已在交付单单列上报,
    #    不在这里靠改判据掩盖。
    poisoned = dict(payload)
    poisoned["channel"] = {"label": "成本 3.9 元"}
    with pytest.raises(Exception):
        assert_facade_clean(poisoned, where="test_prefill_negative")
