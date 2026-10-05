"""【窗 2 插单返修】``GET /capabilities`` 的 DLP 词表缺口(2026-08-20)。

## 现象与真因(自己跑出来的,不是照抄工单)

基线 ``a88533306`` 上,``GET /api/xiaobang/operations/capabilities`` 对**任何看得见
命令的身份**一律 500。真因是出参闸 fail-closed 抓到 17 处
``InternalTermLeak``,全部落在同一格::

    $.commands[0].confirmation_policy.bind[0..5]    ← writing_center(compute_only,6 项)
    $.commands[1].confirmation_policy.bind[0..10]   ← publish_center(external,11 项)

``confirmation_policy.bind`` 的字段名(``actor_id`` / ``payload_hash`` / ``expires_at`` …)
形如 ``[a-z]+(_[a-z0-9]+)+`` ⇒ 既有闸判「未翻译的内部枚举」。
``FIVE_PHASE_MACHINE_KEYS`` 里其实已有 ``payload_hash`` 等**键名**,但 bind 的枚举是
**列表值**(继承父键 ``bind``),键名白名单够不着它们 —— 这是这个洞能活到今天的原因。

工单说「admin/agent 100% 500」。**实测要加一句前提**:agent 只有在
``permissions`` 里带 ``writing:`` / ``publish:`` 前缀时 ``count`` 才 > 0
(``gap_operation_map.is_operation_allowed:435``)。没有权限的 agent 得到 count=0,
和 customer 一样打不到闸 —— 所以下面 agent 那一臂**显式给权限**,并断言 count>0,
否则这条判据会以「全绿」掩盖「这一臂压根没打到被测代码」。

## 判的是哪一路:登记豁免,不补翻译

消费方普查(坐标贴在这里,便于复核):

* ``grep -rn "confirmation_policy|capabilities" frontend/src`` → **零命中**
  (前端从来没调过这个端点,也没读过这个字段);
* 全仓 ``bind`` 的读取方只有两处,都不是显示面:
  ``services/xiaobang_command_contract.py:264``(注册期自校验)、
  ``tests/xiaobang_vnext_2026_08_18/test_command_contract.py``(判据);
* 五阶段 DTO 那一侧 ``api/xiaobang_operations_api.py:419`` 只下发
  ``confirmation_policy.mode``,**不带 bind**。

⇒ 纯机器契约,零显示面 ⇒ 按工单第二路:**字段级机器面豁免**
(``services/xiaobang_facade_dlp.MACHINE_FACE_EXEMPTIONS``),三重收窄
(端点 / 字段路径 / 取值集合),并自带下面这组锁证明豁免面没被放宽。

## 这一组判据的成对形状

每条「必须放行」都配一条「必须仍然判红」:

===============================  ==========================================
必须放行                          必须仍判红
===============================  ==========================================
三身份 capabilities 200          bind 里注一个**未登记**枚举 → 仍 500
bind 的 11 个已登记字段名        bind 里注一个**供应商名** → 仍 500
capabilities 这一个 where        默认 where(其余调用点)→ 仍判红
bind[i] 这一条路径               相邻路径 / 别的 where → 豁免不命中
===============================  ==========================================
"""
from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import gap_operation_map as omap
from services.gap_operation_labels import InternalTermLeak, known_status_codes
from services.xiaobang_command_contract import (
    COMPUTE_ONLY_MIN_BIND_FIELDS,
    EXTERNAL_BIND_FIELDS,
)
from services.xiaobang_facade_dlp import (
    CAPABILITIES_FACADE,
    MACHINE_FACE_EXEMPTIONS,
    is_machine_face_exempt,
    machine_face_exemption_inventory,
)

CAPS_URL = "/api/xiaobang/operations/capabilities"

#: 三身份。agent 的 permissions 是**产品前提**不是夹具糖:没有它 count 恒 0。
IDENTITIES: dict[str, dict] = {
    "admin": {"id": 9001, "username": "cap-admin", "is_admin": True, "agent_level": 0,
              "permissions": []},
    "agent": {"id": 9002, "username": "cap-agent", "is_admin": False, "agent_level": 1,
              "permissions": ["writing:read", "writing:write",
                              "publish:read", "publish:write"]},
    "customer": {"id": 9003, "username": "cap-customer", "is_admin": False,
                 "agent_level": 0, "permissions": []},
}


def _app(monkeypatch, *, identity_key, poison=None) -> TestClient:
    """真 HTTP 应用。

    🔴 ``identity_key=None`` 时**不设** ``request.state.user`` —— 于是走的是
       ``_require_user`` 的真 401,不是夹具伪造的 401。
    🔴 ``poison(commands)`` 在**真 producer 之后**改一格,其余全部保持真实产出;
       这样注毒判据打的是「闸还在不在」,而不是一个手搓的假 payload。
    """
    import api.xiaobang_operations_api as ops

    if poison is not None:
        real = omap.discover_commands

        def _poisoned(user, *, identity=None):
            commands = copy.deepcopy(real(user, identity=identity))
            poison(commands)
            return commands

        monkeypatch.setattr(ops.omap, "discover_commands", _poisoned)

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        if identity_key is not None:
            request.state.user = dict(IDENTITIES[identity_key])
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(ops.router)
    # 不吞异常,才能看到真正的 500(默认会把它 re-raise 到判据里)
    return TestClient(app, raise_server_exceptions=False)


# ══════════════════════════════════════════════════════════════════════════
# 判据 1 · 三身份各打一次(分母自证)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("identity_key", ["admin", "agent"])
def test_capabilities_returns_200_with_commands_for_identities_that_have_them(
        monkeypatch, identity_key):
    """admin / agent → 200 且 count>0。

    🔴 ``count>0`` 是**分母自证**:这个端点对 count=0 的身份根本走不到泄漏那一格,
       所以只断言 200 的话,一条恒绿的判据也能长成这样。
    """
    with _app(monkeypatch, identity_key=identity_key) as client:
        got = client.get(CAPS_URL)
    assert got.status_code == 200, got.text[:600]
    body = got.json()
    assert body["count"] > 0, body
    assert len(body["commands"]) == body["count"], body
    # 真的把那一格发出去了(不是"因为字段消失了所以不泄漏")
    binds = [c["confirmation_policy"]["bind"] for c in body["commands"]]
    assert all(binds), binds
    # 🔴 [WO-B2 2026-08-20] 原来写死 `== 17`。加一条合同(geo_content_center)
    #    之后它当场红 —— 而它要钉的是「那些值真的还在出参里」,不是"一共几个"。
    #    改成从**源码常量**按每条命令的档位推导:合同增减时自动跟随,
    #    但"值凭空少了"照样红。身份不同看到的命令数不同,这样也不用分两套期望。
    expected = sum(
        len(EXTERNAL_BIND_FIELDS) if c["side_effect"] == "external"
        else len(COMPUTE_ONLY_MIN_BIND_FIELDS)
        for c in body["commands"])
    assert expected > 0, body["commands"]                      # 分母自证
    assert sum(len(b) for b in binds) == expected, (binds, expected)


def test_capabilities_is_200_for_customer_but_that_arm_never_reaches_the_gate(monkeypatch):
    """customer → 200,**且 count==0**。

    工单里 customer 本来就是 200。这条把「为什么它是 200」说清楚:它一条命令都
    发现不到,压根走不到 bind 那一格 —— 也就是说 customer 这一臂**证明不了**闸被修好,
    只能作为「本次改动没有把它弄坏」的对照。写出来,免得三身份全绿被当成三份证据。
    """
    with _app(monkeypatch, identity_key="customer") as client:
        got = client.get(CAPS_URL)
    assert got.status_code == 200, got.text[:600]
    assert got.json()["count"] == 0, got.json()


# ══════════════════════════════════════════════════════════════════════════
# 判据 2 · 反向对照(500 不是"路由/鉴权本来就坏")
# ══════════════════════════════════════════════════════════════════════════

def test_no_token_is_401_not_500(monkeypatch):
    with _app(monkeypatch, identity_key=None) as client:
        got = client.get(CAPS_URL)
    assert got.status_code == 401, got.text[:300]


def test_unknown_path_under_the_same_prefix_is_404_not_500(monkeypatch):
    """同前缀不存在路径 → 404。

    🔴 走的是 ``/{operation_id}/...`` 那批的 ``_resolve_entry`` 404
       (无合同/不存在同一个 404),证明 500 不是"这个前缀整片坏了"。
    """
    with _app(monkeypatch, identity_key="admin") as client:
        got = client.post("/api/xiaobang/operations/no_such_operation/prepare",
                          json={"prepare_request_id": "cap-404-probe-0001",
                                "selection": {}})
    assert got.status_code == 404, got.text[:300]


# ══════════════════════════════════════════════════════════════════════════
# 判据 3 · 拆锁:闸没有被摘掉
# ══════════════════════════════════════════════════════════════════════════

def test_injecting_an_unregistered_bind_enum_still_trips_the_gate(monkeypatch):
    """🔴 往 bind 注一个**真未登记**的枚举 → 必须仍 500。

    这条是本次豁免的**承重判据**。如果豁免写成「bind 这条路径整片放行」,
    它会绿 —— 而那正是"把闸摘掉"。豁免按**取值集合**收窄,所以没登记的值照样判红。
    """
    poison_value = "totally_unregistered_enum"
    assert poison_value not in (set(EXTERNAL_BIND_FIELDS) | set(COMPUTE_ONLY_MIN_BIND_FIELDS))

    def _poison(commands):
        commands[0]["confirmation_policy"]["bind"].append(poison_value)

    with _app(monkeypatch, identity_key="admin", poison=_poison) as client:
        got = client.get(CAPS_URL)
    assert got.status_code == 500, (
        "未登记枚举被放行了 —— 豁免面被放宽成整条路径,闸等于摘掉了。"
        "实得 {0}".format(got.status_code))


def test_the_gate_goes_green_again_once_the_poison_is_removed(monkeypatch):
    """🔁 恢复后回绿 —— 证明上面那一红是注毒造成的,不是环境坏了。"""
    with _app(monkeypatch, identity_key="admin") as client:
        assert client.get(CAPS_URL).status_code == 200


def test_a_supplier_name_inside_bind_still_trips_the_gate(monkeypatch):
    """🔴 豁免只豁「未翻译枚举」这一条规则,不是这一格随便放什么都行。

    往同一个已豁免的路径里塞一个供应商名 → 必须仍 500。
    没有这一条,「字段级豁免」和「把这一格从 DLP 里删掉」在判据上没有区别。
    """
    def _poison(commands):
        commands[0]["confirmation_policy"]["bind"].append("deepseek 上游直连")

    with _app(monkeypatch, identity_key="admin", poison=_poison) as client:
        got = client.get(CAPS_URL)
    assert got.status_code == 500, got.text[:300]


def test_the_default_facade_where_is_untouched():
    """🔴 其余调用点(默认 ``where``)**没有**被这次豁免放宽。

    同一份 payload 用默认 where 过闸必须仍然判红 —— 证明豁免是按端点收窄的,
    不是给整个五阶段 façade 开了洞。
    """
    from api.xiaobang_operations_api import _clean

    payload = {
        "commands": [{"confirmation_policy": {"mode": "required_user_click",
                                              "bind": list(EXTERNAL_BIND_FIELDS)}}],
        "count": 1,
    }
    # 默认 where:判红
    with pytest.raises(InternalTermLeak):
        _clean(copy.deepcopy(payload))
    # capabilities 的 where:放行
    assert _clean(copy.deepcopy(payload), where=CAPABILITIES_FACADE) is not None


# ══════════════════════════════════════════════════════════════════════════
# 判据 4 · 分母机械枚举:bind 全集逐个「有翻译 或 在豁免登记」
# ══════════════════════════════════════════════════════════════════════════

def _bind_universe() -> set:
    """轴的作用域 = **bind 全集**,机械取,不手抄那 11 个。

    两个来源都要:

    * 源码常量(``EXTERNAL_BIND_FIELDS`` / ``COMPUTE_ONLY_MIN_BIND_FIELDS``)——
      声明了但暂时没有 operation 用到的字段也在轴上;
    * 注册表实际产出(``discover_commands``)—— 防"常量改了但注册表另有来源"。
    """
    universe = set(EXTERNAL_BIND_FIELDS) | set(COMPUTE_ONLY_MIN_BIND_FIELDS)
    for command in omap.discover_commands({"id": 1, "is_admin": True}, identity=None):
        universe |= set(command["confirmation_policy"]["bind"])
    return universe


def test_every_bind_enum_is_either_translated_or_exempted():
    """🔴 逐个断言,漏一个即红。分母来自源码,不是我记得几个。"""
    universe = _bind_universe()
    assert len(universe) >= 11, universe          # 空/瘪分母自证

    translated = known_status_codes()
    uncovered = []
    for value in sorted(universe):
        if value in translated:
            continue
        # 豁免必须在**它真实出现的那条路径**上成立,不是"名字在某张表里"
        path = "$.commands[0].confirmation_policy.bind[0]"
        if is_machine_face_exempt(CAPABILITIES_FACADE, path, value):
            continue
        uncovered.append(value)
    assert not uncovered, (
        "bind 全集里有 {0} 个既没翻译也没登记豁免:{1};"
        "它们会让 capabilities 对任何看得见命令的身份 500".format(len(uncovered), uncovered))


def test_the_exemption_surface_is_enumerable_and_narrow():
    """🔴 豁免面自身可枚举 + 不越界(证明它没被悄悄放宽)。"""
    inventory = machine_face_exemption_inventory()
    assert inventory, "豁免登记表是空的 —— 那上面那条判据恒真"
    assert len(MACHINE_FACE_EXEMPTIONS) == 1, inventory
    only = inventory[0]
    assert only["where"] == CAPABILITIES_FACADE, only
    assert only["value_count"] == len(_bind_universe()), only

    good = "$.commands[0].confirmation_policy.bind[3]"
    assert is_machine_face_exempt(CAPABILITIES_FACADE, good, "payload_hash")

    # 🔁 反向对照:相邻路径 / 别的 where / 未登记取值 —— 一律不命中
    for where, path, value in (
        (CAPABILITIES_FACADE, "$.commands[0].confirmation_policy.mode", "payload_hash"),
        (CAPABILITIES_FACADE, "$.commands[0].adapters[0]", "payload_hash"),
        (CAPABILITIES_FACADE, "$.commands[0].confirmation_policy.bindx[0]", "payload_hash"),
        (CAPABILITIES_FACADE, "$.bind[0]", "payload_hash"),
        ("xiaobang_operations", good, "payload_hash"),
        (CAPABILITIES_FACADE, good, "some_other_enum"),
    ):
        assert not is_machine_face_exempt(where, path, value), (where, path, value)
