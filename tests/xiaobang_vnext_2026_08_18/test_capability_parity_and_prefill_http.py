"""capability 奇偶 + 预填真 HTTP(R3-P7 ③④)。

## ④ 奇偶判据是什么

能力发现按 ``required_capability`` 过滤后**下发**给员工的 operation 集合,
必须与外层组织守卫在五阶段路径上**放行**的集合逐项一致。

不一致的那一格长什么样:``writing_center`` 要 ``writing.generate``,
而外层守卫在 ``/{operation_id}/prepare`` 上写死 ``publish.plan`` ⇒
一个只有 ``writing.generate`` 的员工会**看见**这个能力、点下去 **403**。
两边单看都"对",用户看到的是功能不存在。工单点名它是首个正样本。

🔴 放行 ≠ 授权:外层守卫只判「这条路径对这个身份是否可达」,
精确校验在 handler 的 ``_authorize()``。所以奇偶判据必须**同时**验:
可达(不 403)**且** 无权的 operation 仍被 handler 挡住。
"""

from __future__ import annotations

import types

import pytest
from fastapi import HTTPException

from services import gap_operation_map as omap
from services.organization_contract import OrganizationError
from services.organization_route_contract import (
    MEMBER_GEO_ROUTE_POLICIES,
    five_phase_any_of,
    match_member_geo_route,
)

_FIVE_PHASE_PATHS = tuple(
    policy for policy in MEMBER_GEO_ROUTE_POLICIES
    if policy.path_template.startswith("/api/xiaobang/operations/")
)


class _Identity:
    """员工身份替身。``require`` / ``require_any`` 直接用真实现,不另写一套。"""

    def __init__(self, capabilities):
        self.capabilities = frozenset(capabilities)
        self.is_member = True
        self.is_owner = False
        self.organization_id = 91
        self.payer_user_id = 302
        self.membership_version = "m1"
        self.assignment_authority_version = "a1"
        self.approval_policy_version = "p1"
        self.actor_kind = "member"
        self.organization_status = "active"

    # ``require`` / ``require_any`` 由 :func:`_member` 绑定**真实现**
    # —— 替身自己实现一份判断,等于用替身验替身,而被测的恰好是真实现。


def _member(capabilities) -> _Identity:
    """把真 ``IdentityContext`` 的两个方法绑到替身上。

    🔴 不重写 ``require`` 的逻辑 —— 重写就等于用替身的实现验替身,
    而被测的恰好是真实现的判断。
    """
    from services.organization_contract import IdentityContext

    identity = _Identity(capabilities)
    identity.require = types.MethodType(IdentityContext.require, identity)
    identity.require_any = types.MethodType(IdentityContext.require_any, identity)
    return identity


def _guard_admits(identity, policy) -> bool:
    """复现 ``middleware/organization_guard.py`` 里那段判断(同一套谓词)。"""
    try:
        if policy.capability_any_of:
            identity.require_any(policy.capability_any_of)
        else:
            identity.require(policy.capability)
        return True
    except OrganizationError:
        return False


# ══════════════════════════════════════════════════════════════════════════
# ④ 奇偶
# ══════════════════════════════════════════════════════════════════════════

def test_five_phase_paths_all_carry_the_any_of_set():
    """9 条通用路由每条都要带 any-of;少一条就是那条路径上的奇偶洞。"""
    assert len(_FIVE_PHASE_PATHS) == 9, [p.path_template for p in _FIVE_PHASE_PATHS]
    for policy in _FIVE_PHASE_PATHS:
        assert policy.capability_any_of, policy.path_template


def test_any_of_set_is_derived_from_the_registry_not_hand_typed():
    """any-of 必须**从注册表取**。

    手写会在新增一个 command_contract 时静默漂移,而漂移的表现正是本项要修的
    那种奇偶不一致。这条把注册表里每个 required_capability 都要求在集合里。
    """
    declared = set(five_phase_any_of())
    for entry in omap.commandable_operations():
        assert entry.command_contract.required_capability in declared, entry.operation_id


@pytest.mark.parametrize(
    "entry", omap.commandable_operations(), ids=lambda e: e.operation_id,
)
def test_discovery_and_guard_agree_for_every_commandable_operation(entry):
    """🔴 奇偶本体:**能力发现下发** ∩ **守卫放行** 逐项一致。

    对每个可命令 operation,构造「只持有它那一项 capability」的员工:
      · 能力发现必须下发它;
      · 五阶段 9 条路径必须全部放行(不 403)。
    ``writing_center``(``writing.generate``)就是工单点名的首个正样本 ——
    R3-P6 的单 capability 写法在它上面必红。
    """
    capability = entry.command_contract.required_capability
    # 🔴 N18 变异存活暴露的缺口:第一版给身份附赠了 `clients.read_assigned`,
    #    而它本身就在 any-of 集合里 ⇒ 无论 operation 自己的 capability 有没有
    #    被登记进 any-of,守卫都放行。判据对"漏登记 writing.generate"这件事
    #    **零判别力**。这里只给它那一项。
    identity = _member({capability})
    user = {"id": 501, "is_admin": False}

    discovered = {item["operation_id"] for item in omap.discover_commands(user, identity=identity)}
    assert entry.operation_id in discovered, (entry.operation_id, capability, discovered)

    blocked = [p.path_template for p in _FIVE_PHASE_PATHS if not _guard_admits(identity, p)]
    assert not blocked, (
        "能力发现下发了 {0} 却被守卫拦在这些路径上(奇偶不一致): {1}".format(
            entry.operation_id, blocked)
    )


def test_a_member_with_no_relevant_capability_is_still_blocked_at_the_guard():
    """🔁 反向对照:any-of **不是**放开。

    只有无关 capability 的员工必须仍然被外层守卫挡住 ——
    否则「降到最小 capability」就退化成「不设门」。
    """
    identity = _member({"monitoring.read_assigned"})
    admitted = [p.path_template for p in _FIVE_PHASE_PATHS if _guard_admits(identity, p)]
    assert not admitted, admitted
    # 而且能力发现对他也是空的 —— 两边同时为空,奇偶仍然一致。
    assert omap.discover_commands({"id": 502, "is_admin": False}, identity=identity) == []


def test_guard_passing_does_not_mean_the_handler_authorizes():
    """🔴 放行 ≠ 授权。

    持 ``writing.generate`` 的员工在 ``publish_center`` 的路径上**可达**
    (外层放行),但 handler 的 ``_authorize()`` 必须按该 operation 的
    ``required_capability``(``publish.execute``)把他挡住。
    少了这条,any-of 就等于把精确授权也一起放宽了。
    """
    from api.xiaobang_operations_api import _authorize

    # 🔴 N20 变异存活暴露的缺口:第一版用「只有 writing.generate」的员工去打
    #    publish_center —— 他在**更早**那道粗筛(is_operation_allowed 按
    #    required_module 前缀 `publish.` 筛)就已经被挡住了,所以拆掉精确
    #    capability 检查这条判据照样绿 = 对被测的那一行零判别力。
    #    正确构造:持 `publish.plan` —— 前缀 `publish.` 过粗筛,
    #    但不是 publish_center 要的 `publish.execute` ⇒ 只有精确检查能挡他。
    identity = _member({"publish.plan"})
    request = types.SimpleNamespace(state=types.SimpleNamespace(
        user={"id": 503, "is_admin": False}, organization_identity=identity,
    ))
    publish_entry = omap.resolve_operation("publish_center")
    # 先证明他确实过得了粗筛(否则下面的 404 又是被更早那道门吃掉的)。
    assert omap.is_operation_allowed(
        publish_entry, {"id": 503, "is_admin": False}, identity=identity)
    with pytest.raises(HTTPException) as excinfo:
        _authorize(request, publish_entry)
    assert excinfo.value.status_code == 404          # 不回显存在性(§8.2)

    # 🔁 反向对照:持精确 capability 的员工必须过 —— 否则上面是恒红。
    ok_request = types.SimpleNamespace(state=types.SimpleNamespace(
        user={"id": 504, "is_admin": False},
        organization_identity=_member({"publish.execute"}),
    ))
    _authorize(ok_request, publish_entry)


def test_the_old_single_capability_behaviour_is_unchanged_for_normal_routes():
    """追加谓词,不改写默认路径:``any_of`` 为空的路由仍走原来的 ``require``。

    (2026-08 有一次把默认路径改写成三元式,结果没传参数的老路也换了语法。)
    """
    context = match_member_geo_route("POST", "/api/xiaobang/context")
    assert context is not None and context.capability_any_of == ()
    assert _guard_admits(_member({"clients.read_assigned"}), context)
    assert not _guard_admits(_member({"monitoring.read_assigned"}), context)


# ══════════════════════════════════════════════════════════════════════════
# ③ 预填真 HTTP + 与浏览器夹具同形
# ══════════════════════════════════════════════════════════════════════════

def _prefill_over_http(monkeypatch, row, entry_id="publish_center"):
    """真 HTTP 打预填端点。鉴权与取行替身化,**投影逻辑与出口 DLP 走真的**。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.xiaobang_operations_api as ops

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {"id": 601, "is_admin": True}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(ops.router)

    class _Ctx:
        owner_user_id = 601

        def public_context(self):
            return {"brand_id": 101, "brand_name": "甲品牌", "quote_id": 77}

    monkeypatch.setattr(ops, "_authorized_context", lambda *a, **k: _Ctx())
    monkeypatch.setattr(ops, "load_intent", lambda *a, **k: row)

    class _Conn:
        def cursor(self):
            return None

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("db.connection.get_db", lambda: _Conn())
    with TestClient(app) as client:
        return client.get(
            "/api/xiaobang/operations/intents/{0}/prefill".format(row["intent_id"])
        )


def _row():
    return {
        "intent_id": "xint_pw0000000001",
        "intent_state": "prepared",
        "intent_revision": 1,
        "operation_id": "publish_center",
        "side_effect": "external",
        "confirmation_mode": "required_user_click",
        "registry_version": omap.OPERATION_REGISTRY_VERSION,
        "operation_version": 1,
        "payload_hash": "p" * 8,
        "object_manifest_hash": "m" * 8,
        "compute_quote_amount": 390,
        "compute_quote_unit": "算力",
        "compute_quote_expires_at": None,
        "approval_window_expires_at": None,
        "preview": {
            "customer_label": "甲品牌",
            "scope": "wp1_identity_and_object_refs",
            "pending_manifest_inputs": ["channel_eligibility"],
            "object_items": [
                {"resource_kind": "brand", "ref": 101, "label": "甲品牌"},
                {"resource_kind": "article", "ref": 4201, "label": None},
            ],
            "form_prefill": {"brand_id": 101, "article_id": 4201},
        },
        "reason_facts": [{"reason_code": "low_coverage", "explanation": "这个词还没被推荐过,先补一篇"}],
        "domain_ref": {},
    }


def test_prefill_endpoint_answers_over_real_http(monkeypatch):
    """真 HTTP:200 + 合同版本 + §12.3 那批字段齐。"""
    response = _prefill_over_http(monkeypatch, _row())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract_version"] == "xiaobang-page-prefill-v1"
    for key in ("customer", "object", "recommendation", "external_actions",
                "payer", "compute", "primary_action", "cancel_action",
                "secondary_actions", "form_prefill", "rebind_hint", "status"):
        assert key in body, key


def test_object_dto_carries_a_displayable_identity(monkeypatch):
    """[R3-P7 ③] ``object`` 必须能显示「在动哪个东西」。

    R3-P6 只有 ``scope``/``pending_manifest_inputs`` —— 页面上显示不出对象。
    拿不到名字时退回「文章 #4201」,**不编名字**。
    """
    body = _prefill_over_http(monkeypatch, _row()).json()
    obj = body["object"]
    # 🔴 内部工程标记不许出门(它同时是 DLP 会判红的形态)。
    assert "scope" not in obj, obj
    assert obj["pending_checks"] == ["投放账号资格还没核"], obj
    assert obj["label"] == "甲品牌 · 文章 #4201", obj
    kinds = [item["resource_kind"] for item in obj["items"]]
    assert kinds == ["brand", "article"], obj
    # 反向对照:没有 object_items 时给 None,而不是编一个标签。
    row = _row()
    row["preview"].pop("object_items")
    empty = _prefill_over_http(monkeypatch, row).json()["object"]
    assert empty["label"] is None and empty["items"] == []


def test_prefill_dto_shape_matches_the_browser_fixture():
    """🔴 浏览器判据的 mock 形状必须与服务端**同形**。

    否则 PW 那 5 条会对着一个服务端根本不会返回的 DTO 全绿 ——
    「两边跑的不是同一个后端」这种假绿本仓付过费。
    这条从 TS 夹具里解析出顶层键,与服务端 ``build_prefill`` 的键集合求差。
    """
    import re

    from services.xiaobang_page_prefill import build_prefill

    fixture = (
        __import__("pathlib").Path("frontend/tests/xiaobang-prefill/prefill-harness.ts")
        .read_text(encoding="utf-8")
    )
    block = re.search(r"export function prefillFixture[\s\S]*?\n  return \{([\s\S]*?)\n  \}\n\}",
                      fixture)
    assert block, "解析不到 prefillFixture —— 判据本身失效了"
    fixture_keys = set(re.findall(r"^    ([a-z_]+):", block.group(1), re.M))

    server = build_prefill(_row(), entry=omap.resolve_operation("publish_center"),
                          projection={"settlement_projection": {"state": "none"},
                                      "external_projection": {"state": "not_started"}},
                          quote_state="quoted")
    server_keys = set(server) | {"status", "help_target"}   # status 由 handler 补
    missing = fixture_keys - server_keys
    assert not missing, "夹具有服务端不返回的键: {0}".format(sorted(missing))
    # 反向:§12.3 要求同屏可见的那批,夹具一个都不能少。
    required = {"customer", "object", "recommendation", "external_actions",
                "payer", "compute", "primary_action", "cancel_action", "form_prefill"}
    assert required <= fixture_keys, sorted(required - fixture_keys)


def _get_intent_over_http(monkeypatch, row):
    """GET intent 走的是 ``_intent_dto``(**不是** build_prefill)。

    🔴 N13 变异存活暴露的缺口:我把 ``_intent_dto`` 的 preview 透传改成人话投影,
       却只给**预填**端点写了判据 —— 而预填走 build_prefill。也就是说那处修复
       身上一条判据都没有。这个 helper 补的就是那条。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.xiaobang_operations_api as ops

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {"id": 601, "is_admin": True}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(ops.router)

    class _Ctx:
        owner_user_id = 601

        def public_context(self):
            return {"brand_id": 101, "brand_name": "甲品牌", "quote_id": 77}

    monkeypatch.setattr(ops, "_authorized_context", lambda *a, **k: _Ctx())
    monkeypatch.setattr(ops, "load_intent", lambda *a, **k: row)

    class _Conn:
        def cursor(self):
            return None

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("db.connection.get_db", lambda: _Conn())
    with TestClient(app) as client:
        return client.get(
            "/api/xiaobang/operations/publish_center/intents/{0}".format(row["intent_id"])
        )


def test_get_intent_does_not_leak_internal_enums_over_real_http(monkeypatch):
    """🔴 存量缺陷的判据(素树同形):preview 原样透传 → DLP 抛异常 → 真 HTTP 500。

    这条打的是 ``_intent_dto``。它与预填那条**不能互相冒充** ——
       两个端点走两套投影函数。
    """
    response = _get_intent_over_http(monkeypatch, _row())
    assert response.status_code == 200, response.text[:400]
    preview = response.json()["preview"]
    assert "scope" not in preview, preview
    assert "pending_manifest_inputs" not in preview, preview
    assert preview["pending_checks"] == ["投放账号资格还没核"], preview
    assert preview["customer_label"] == "甲品牌"


def test_the_get_intent_leak_criterion_has_discriminating_power(monkeypatch):
    """活性自证:把原样 preview 直接过一遍 DLP,必须判红。

    否则上面那条的 200 可能只是因为 DLP 根本不看这些键。
    """
    import pytest as _pytest

    from services.xiaobang_facade_dlp import assert_facade_clean

    with _pytest.raises(Exception):
        assert_facade_clean({"preview": _row()["preview"]}, where="neg")