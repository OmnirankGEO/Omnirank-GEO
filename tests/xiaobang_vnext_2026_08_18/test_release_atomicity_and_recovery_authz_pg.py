"""R3-P8 ①②③⑤ 真库判据。

四件事各有一条**最容易写成假绿**的形态,都在这里钉住:

* ① `form_prefill` 由 **producer 真写** —— R3-P7 只有夹具里有这个键,生产端一个字没写,
  于是浏览器那条"表被真填了"打的是**夹具自己造的字段**。本文件从真 `POST /prepare`
  起链,并**禁止**任何夹具注入生产端不写的键;
* ② 恢复类端点从**冻结对象**恢复 refs 再做现役对象授权 —— 原来一律传空 refs,
  也就是这些端点上从没问过「这个客户还归不归你」;
* ③ system page 重建单事务 —— 中途注毒必须让**旧库一行不少**;
* ⑤ 门形态:草稿可存 / violating release 禁激活 / 上一 release 保持 active,
  错误合同带 `rule_id` + `version`。
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from services import gap_operation_map as omap
from services.kb_terminology_gate import (
    RULING_RULE_ID,
    RULING_VERSION,
    KbTerminologyViolation,
)


# ══════════════════════════════════════════════════════════════════════════
# ① form_prefill 由 producer 真写(禁夹具注入生产端不写的字段)
# ══════════════════════════════════════════════════════════════════════════

def _producer_preview_keys() -> set[str]:
    """AST 取 ``operation_prepare`` 里 ``preview={...}`` 真正写了哪些键。

    🔴 用 AST 而不是跑一次看结果:跑一次只能证明"这次这几个键有值",
    证不了"生产端声明了这些键" —— 而漏的那种恰好是"生产端压根没这个键"。
    """
    import api.xiaobang_operations_api as ops

    tree = ast.parse(inspect.getsource(ops))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "preview" and isinstance(node.value, ast.Dict):
            return {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


def test_prepare_producer_really_writes_form_prefill():
    """🔴 R3-P7 的洞:生产端 `preview` 里**没有** form_prefill 这个键。"""
    keys = _producer_preview_keys()
    assert keys, "解析不到 producer 的 preview —— 判据本身失效了"
    assert "form_prefill" in keys, sorted(keys)
    assert "object_items" in keys, sorted(keys)


def _public_preview_keys() -> set[str]:
    """``_public_preview`` 投影出去的键(DTO 形状)。

    与 producer 存的键**不是同一组**:`scope` / `pending_manifest_inputs` 存在库里,
    但出门时被投影成 `pending_checks`(R3-P7 ③ 的存量缺陷修复)。
    所以夹具的合法参照有两组:**存的形状** 与 **出门的形状**,两组都是"真的"。
    """
    import api.xiaobang_operations_api as ops

    tree = ast.parse(inspect.getsource(ops))
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef) and node.name == "_public_preview"):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Dict):
                    return {k.value for k in inner.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


def test_no_fixture_injects_a_preview_key_the_producer_never_writes():
    """🔴 工单点名:**禁夹具注入生产端不写的字段**。

    分母 = 本包所有 Python 夹具 + 浏览器夹具里出现的 `preview` 键;
    要求它 ⊆ producer 真写的键集合。缺这条,判据可以永远对着一个
    生产端不存在的 DTO 全绿(R3-P7 就是这么过去的)。
    """
    allowed = _producer_preview_keys() | _public_preview_keys()
    assert _public_preview_keys(), "投影键取不到 —— 判据本身失效了"
    here = Path(__file__).parent
    suspects: dict[str, set[str]] = {}

    # 🔴 锁的范围是**intent 行形状**的夹具 —— 即同一个 dict 里既有 `intent_id`
    #    (或 `intent_state`)又有 `preview` 的那种。它们是 `build_prefill` /
    #    `_intent_dto` 的入参替身,所以它们对 preview 形状是有断言性的。
    #    `test_facade_dlp.py` 里那种 `{"preview": {"note": "毒串"}}` **不在范围内**:
    #    它只是拿 preview 当毒串载体,没有声称 prepare 会产出 `note` 这个键。
    #    把它一起锁进来会逼下一个人去改 DLP 的毒样本 —— 锁错了对象。
    for path in sorted(here.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            literal_keys = {k.value for k in node.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if not ({"intent_id", "intent_state"} & literal_keys):
                continue                     # 不是 intent 行形状 ⇒ 不在锁的范围
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "preview"
                        and isinstance(value, ast.Dict)):
                    found = {k.value for k in value.keys
                             if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                    if found:
                        suspects.setdefault(path.name, set()).update(found)

    assert suspects, "一个 intent 行形状的 preview 夹具都没扫到 —— 分母没了,断言会空即通过"
    for name, keys in suspects.items():
        extra = keys - allowed
        assert not extra, "{0} 注入了生产端不写的 preview 键: {1}".format(name, sorted(extra))


def _browser_variant_blocks() -> dict:
    """[R3-P11 ①] 解析浏览器夹具里**每个对象变体**的 form_prefill / channel。

    🔴 上一版这两条锁用的是「取第一个 form_prefill 块」的正则 —— **只取第一处**。
    夹具扩到三个变体之后,那等于"只对了 article 那一份账",另外两份写错了也不会红。
    与 R3-P9 ① 的教训同型:**分母不能是"我碰到的第一个"**。
    """
    import re

    text = Path("frontend/tests/xiaobang-prefill/prefill-harness.ts").read_text(
        encoding="utf-8")
    # 🔴 [WO-B ① 2026-08-20] 变体名**机械枚举**,不再手写那三个:
    #    手写的清单在夹具加第四个变体的那天会静默漏掉它 ——
    #    而漏掉的表现是"新变体写错了也不会红",正是本函数注释里说的那个坑再犯一次。
    names = tuple(dict.fromkeys(re.findall(r"^  (\w+): \{$", text, re.M)))
    assert len(names) >= 3, names          # 分母自证
    out: dict = {}
    for name in names:
        # 变体块 = `  <name>: {` 起,到下一个顶层 `  },` 止
        block = re.search(
            r"^  " + name + r": \{(.*?)^  \},", text, re.S | re.M)
        assert block, "解析不到变体 {0} —— 夹具结构变了,锁要跟着改".format(name)
        body = block.group(1)
        fp = re.search(r"form_prefill:\s*\{([^}]*)\}", body)
        ch = re.search(r"channel:\s*\{([^}]*)\}", body)
        assert fp, name
        out[name] = {
            "form_prefill": {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", fp.group(1))},
            # 🔴 只取**键**,不取字符串值里的冒号。裸 `(\w+):` 会把
            #    `channel_option_id: 'svideo:12'` 里的 `svideo` 也当成一个键 ——
            #    第四档变体加进来时当场误报。键只出现在 `{` 或 `,` 之后。
            "channel_keys": (set(re.findall(r"(?:^|,)\s*(\w+):", ch.group(1)))
                             if ch else set()),
        }
    return out


def test_browser_fixture_covers_more_than_one_object():
    """🔴 分母自证:浏览器夹具必须覆盖**多个**对象,不能只有文章。

    工单原话:「现在只测文章 #4201 = 单对象分母」。单对象分母的失效方式是沉默的 ——
    geo 图文与报价那两条链完全没接线时,只测文章的判据照样全绿。
    """
    blocks = _browser_variant_blocks()
    assert {"article", "geo_post", "quote"} <= set(blocks), sorted(blocks)
    # 变体的 form_prefill 必须**真的不一样**(全一样 = 抄了几份同样的东西,
    # 分母看着有几个、判别力还是 1)。至少三种不同形状。
    shapes = {name: tuple(sorted(info["form_prefill"])) for name, info in blocks.items()}
    assert len(set(shapes.values())) >= 3, shapes
    assert "geo_post_id" in blocks["geo_post"]["form_prefill"], blocks["geo_post"]


def test_browser_fixture_form_prefill_fields_match_the_producer_allowlist():
    """浏览器夹具里 `form_prefill` 的字段必须在 producer 的白名单内 —— **逐变体**。"""
    import api.xiaobang_operations_api as ops

    allowed = set(ops._FORM_PREFILL_FIELDS)
    for name, info in _browser_variant_blocks().items():
        fields = set(info["form_prefill"])
        assert fields, name
        assert fields <= allowed, (name, fields - allowed)


def test_browser_fixture_channel_keys_match_the_producer():
    """[R3-P11 ②] 夹具的 `channel` 键必须是 producer 真会写的那些。

    ``_frozen_channel`` 的产出键从 AST 取(不是我记得几个);
    夹具里出现的键必须 ⊆ 它。缺这条,前端就可能对着一个服务端永远不会返回的
    channel 形状全绿 —— 与 R3-P7 的 form_prefill 是同一个坑。
    """
    import api.xiaobang_operations_api as ops

    # 🔴 [WO-B ③ 2026-08-20 · producer 拆成两半,锚跟着拆]
    #    ``_frozen_channel`` 现在在拿到解析结果时**委托**给
    #    ``ChannelResolution.as_channel_dict``(渠道资格从"恒 pending"变成真值)。
    #    只扫前者的源码会漏掉 ``eligible`` / ``eligibility_note`` 两个新键 ——
    #    那样夹具写了它们反而判红,而真正该守的「夹具键 ⊆ producer 键」失效。
    from services.xiaobang_channel_eligibility import ChannelResolution

    src = (inspect.getsource(ops._frozen_channel)
           + inspect.getsource(ChannelResolution.as_channel_dict))
    produced = set(re.findall(r'out\["(\w+)"\]', src)) | set(
        re.findall(r'"(\w+)":\s', src))
    assert {"verified", "resource_kind", "channel_option_id",
            "eligible", "eligibility_note"} <= produced, produced
    for name, info in _browser_variant_blocks().items():
        extra = info["channel_keys"] - produced
        assert not extra, (name, extra)


def test_prefill_fixture_matches_a_real_prepare_output():
    """🔴 ① 的收口:浏览器夹具的 `form_prefill` 必须与**真跑一次 producer** 的产出逐字相等。

    上一条只验"字段在白名单里"——那还允许夹具少写一个字段、或写成另一个值。
    这条真调 ``_frozen_form_prefill``,拿它的输出去比夹具,
    所以「生产端改了产出而夹具没跟」会当场转红。
    """
    import json
    import re

    import api.xiaobang_operations_api as ops
    from services.xiaobang_intent import canonical_input

    class _Ctx:
        def public_context(self):
            return {"brand_id": 101, "brand_name": "甲品牌", "quote_id": 77}

    # [R3-P11 ①] 逐变体对账 —— 每个变体拿它自己的 selection 真跑一次 producer。
    variants = {
        "article": ({"brand_id": 101, "article_id": 4201},
                    {"brand_id": 101, "brand_name": "甲品牌", "quote_id": 77}),
        "geo_post": ({"brand_id": 101, "geo_post_id": 5150},
                     {"brand_id": 101, "brand_name": "甲品牌"}),
        "quote": ({"brand_id": 101, "quote_id": 77},
                  {"brand_id": 101, "brand_name": "甲品牌"}),
    }
    blocks = _browser_variant_blocks()
    for name, (selection, public) in variants.items():
        class _V:
            def public_context(self_inner, _p=public):
                return dict(_p)

        canonical = canonical_input(
            operation_id="publish_center", operation_version=1, request_schema_version="1",
            selection=selection, allowlist=ops._SELECTION_ALLOWLIST,
        )
        real = ops._frozen_form_prefill(_V(), canonical)
        assert real, "{0}: producer 产出为空 —— 比较会空即通过".format(name)
        assert blocks[name]["form_prefill"] == real, (
            "{0}: 夹具 {1} ≠ producer 真产出 {2}".format(
                name, blocks[name]["form_prefill"], real))


def test_frozen_form_prefill_only_takes_authorized_canonical_values():
    """客户端多塞的键不许进 form_prefill(canonical 已按 allowlist 重建过)。"""
    import api.xiaobang_operations_api as ops
    from services.xiaobang_intent import canonical_input

    class _Ctx:
        def public_context(self):
            return {"brand_id": 101, "brand_name": "甲品牌"}

    canonical = canonical_input(
        operation_id="publish_center", operation_version=1, request_schema_version="1",
        selection={"brand_id": 101, "article_id": 4201, "evil_admin": 1},
        allowlist=ops._SELECTION_ALLOWLIST,
    )
    frozen = ops._frozen_form_prefill(_Ctx(), canonical)
    assert frozen == {"brand_id": 101, "article_id": 4201}, frozen
    assert "evil_admin" not in frozen


# ══════════════════════════════════════════════════════════════════════════
# ② 恢复端点:从冻结对象恢复 refs → 逐次现役对象授权
# ══════════════════════════════════════════════════════════════════════════

# ── 分母:从**路由注册表**机械枚举,手写清单作废 ────────────────────────
#
# 🔴 [R3-P9 ① 制度化] 上一轮这里是**手写的五元组**,于是 ``operation_status``
#    ——第六个恢复端点——从来没进过分母,漏了整整一轮才被外审抓到。
#    「我列了五个」这种分母的失效方式是**沉默的**:漏掉的那个不会让任何判据变红。
#
#    所以分母改成从 ``ops.router.routes`` 取:凡路径模板里带**服务端冻结对象 id**
#    (``intent_id`` / ``execution_id``)的路由,就是「要从冻结对象恢复 refs 再逐次
#    授权」的那一类 —— 这是**路径形状**决定的,不是我记得几个决定的。
#    以后新增第七个这样的端点,它自动进分母;不接线就自己红。
#
#    为什么用「路径里有没有冻结对象 id」而不是「全部 /operations/* 端点」:
#    ``query`` / ``prepare`` 的对象来自**请求体**(调用方自己给 brand_id,授权当场做);
#    ``capabilities`` 根本不碰对象。它们不属于「恢复类」,硬塞进矩阵会得到
#    一条恒真的判据。分类规则本身也有判据:见
#    :func:`test_route_census_splits_into_frozen_object_and_request_scoped`。

_FROZEN_OBJECT_PARAMS = ("intent_id", "execution_id")


def _router_routes():
    import api.xiaobang_operations_api as ops

    out = []
    for route in ops.router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        endpoint = getattr(route, "endpoint", None)
        if not path or not methods or endpoint is None:
            continue
        out.append((tuple(sorted(m for m in methods if m != "HEAD")), path, endpoint.__name__))
    return sorted(out, key=lambda item: (item[1], item[0]))


def frozen_object_routes():
    """恢复类端点全集 = 路径模板含冻结对象 id 的路由(机械枚举)。"""
    return [item for item in _router_routes()
            if any("{" + p + "}" in item[1] for p in _FROZEN_OBJECT_PARAMS)]


def test_route_census_splits_into_frozen_object_and_request_scoped():
    """分类规则的自证:全集 = 恢复类 ⊎ 请求域,两边都不为空且无遗漏。

    没有这条,``frozen_object_routes`` 哪怕因为写错前缀而返回空集,
    下面的矩阵也会「全部通过」—— 空分母的绿灯。
    """
    everything = _router_routes()
    frozen = frozen_object_routes()
    request_scoped = [item for item in everything if item not in frozen]

    assert len(everything) == len(frozen) + len(request_scoped)
    # 空集合自证:两边都必须真的有东西。
    assert frozen, "恢复类端点枚举为空 —— 分母塌了,不是没有洞"
    assert request_scoped, "请求域端点枚举为空 —— 分类规则把所有路由都吞了"
    # 已知形态钉死:请求域这边只应有 capabilities / query / prepare。
    assert sorted(item[2] for item in request_scoped) == [
        "operation_capabilities", "operation_prepare", "operation_query",
    ], request_scoped
    # 恢复类必须包含 status —— 它就是上一轮手写分母漏掉的那一个。
    assert "operation_status" in [item[2] for item in frozen], frozen


@pytest.mark.parametrize("route", frozen_object_routes(), ids=lambda r: r[2])
def test_every_recovery_endpoint_reauthorizes_from_the_frozen_object(route):
    """🔴 覆盖面判据:每个恢复端点都必须**按冻结 refs**重验。

    原来它们一律 ``_authorized_context(request, {})`` —— 空 refs = 没有对象可校验
    = 「这个客户还归不归你」在这些端点上从没被问过。
    这条不数「我加了几处」,而是逐个函数体里找那次调用;分母来自路由表。
    """
    import api.xiaobang_operations_api as ops

    func_name = route[2]
    tree = ast.parse(inspect.getsource(ops))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            target = node
            break
    assert target is not None, func_name

    reverified = False
    for inner in ast.walk(target):
        if not isinstance(inner, ast.Call):
            continue
        name = getattr(inner.func, "attr", None) or getattr(inner.func, "id", None)
        if name != "_authorized_context":
            continue
        for arg in inner.args[1:]:
            if (isinstance(arg, ast.Call)
                    and (getattr(arg.func, "id", None) == "_refs_from_frozen_intent")):
                reverified = True
    assert reverified, "{0} 没有按冻结对象重验".format(func_name)


def test_a_new_frozen_object_route_without_reauth_is_caught():
    """🔁 反向对照:虚构第七个恢复端点(路径带 intent_id、handler 不重验)
    → 分母自动把它算进来,且 AST 判据判红。

    这条证明的是「新增端点漏接线会自己红」,也就是上一轮**没有**的那个性质。
    """
    fake_src = chr(10).join([
        "async def operation_fake_recover(operation_id, intent_id, request):",
        "    entry = _resolve_entry(operation_id)",
        "    context = _authorized_context(request, {})",
        "    return {}",
        "",
    ])
    tree = ast.parse(fake_src)
    target = [n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))][0]
    reverified = False
    for inner in ast.walk(target):
        if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "_authorized_context":
            for arg in inner.args[1:]:
                if (isinstance(arg, ast.Call)
                        and getattr(arg.func, "id", None) == "_refs_from_frozen_intent"):
                    reverified = True
    assert not reverified, "反向对照失效:没重验的 handler 竟然被判成已重验"

    # 分母侧同理:含 {intent_id} 的路径一定被 frozen_object_routes 的规则收进来。
    fake_path = "/{operation_id}/intents/{intent_id}/fake"
    assert any("{" + p + "}" in fake_path for p in _FROZEN_OBJECT_PARAMS)


def test_frozen_refs_come_from_the_server_side_freeze_only():
    """refs 只能来自冻结的 preview,不能来自请求。"""
    import api.xiaobang_operations_api as ops

    row = {"preview": {
        "form_prefill": {"brand_id": 101, "article_id": 4201, "evil": 9},
        "object_items": [{"resource_kind": "quote", "ref": 77}],
    }}
    refs = ops._refs_from_frozen_intent(row)
    assert refs == {"brand_id": 101, "article_id": 4201, "quote_id": 77}, refs
    assert "evil" not in refs
    # 空 preview → 空 refs(而不是编一个)
    assert ops._refs_from_frozen_intent({"preview": {}}) == {}


# ── HTTP 矩阵:分母同源,路由 → 具体调用由**规则**生成 ──────────────────

_INTENT_ID = "xint_r3p8000001"
_EXECUTION_ID = "xexe_r3p9000001"
_PATH_VALUES = {
    "operation_id": "publish_center",
    "intent_id": _INTENT_ID,
    "execution_id": _EXECUTION_ID,
}
#: POST 端点的最小合法 body,按**路径末段**索引 —— 末段就是动作名。
_BODIES = {
    "confirm": {"intent_revision": 1},
    "cancel": {"expected_intent_revision": 1, "cancel_request_id": "cancel-req-0001"},
    "execute": {"intent_revision": 1, "execution_request_id": "exec-req-0001"},
}


def _concrete_calls():
    """把枚举出来的路由变成可发的请求。**认不出来就报错,不静默跳过**。"""
    calls = []
    for methods, path, func_name in frozen_object_routes():
        url = path
        for name, value in _PATH_VALUES.items():
            url = url.replace("{" + name + "}", str(value))
        assert "{" not in url, (
            "路由 {0} 有没登记的路径参数 —— 请在 _PATH_VALUES 补一个取值,"
            "不要把它从矩阵里拿掉".format(path))
        for method in methods:
            body = None
            if method == "POST":
                tail = path.rstrip("/").rsplit("/", 1)[-1]
                assert tail in _BODIES, (
                    "POST 路由 {0} 没有登记 body —— 请在 _BODIES 补一条,"
                    "不要把它从矩阵里拿掉".format(path))
                body = _BODIES[tail]
            calls.append((method, url, body, func_name))
    return calls


def test_revoked_assignment_makes_every_recovery_endpoint_404(monkeypatch):
    """🔴 撤销客户分配后,**全部**恢复端点 404(HTTP 判据 · 分母来自路由表)。

    这里把 ``resolve_authorized_context`` 换成**真实现的等价替身**:
    refs 里的 brand 不在允许集合中就抛 404 —— 与现役函数同一语义
    (跨租户/已撤权/猜 id 统一 404,不回显名称或 id)。
    替身只替"哪些 brand 允许",不替"要不要校验" —— 后者正是被测的那件事。
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    import api.xiaobang_operations_api as ops

    allowed = {101}

    class _Ctx:
        owner_user_id = 601

        def public_context(self):
            return {"brand_id": 101, "brand_name": "甲品牌"}

    def _fake_ctx(request, refs, _page=""):
        brand = (refs or {}).get("brand_id")
        if brand is not None and int(brand) not in allowed:
            raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND"})
        return _Ctx()

    row = {
        "intent_id": _INTENT_ID, "intent_state": "prepared", "intent_revision": 1,
        "operation_id": "publish_center", "side_effect": "external",
        "confirmation_mode": "required_user_click",
        "registry_version": omap.OPERATION_REGISTRY_VERSION, "operation_version": 1,
        "payload_hash": "p" * 8, "object_manifest_hash": "m" * 8,
        "compute_quote_amount": 390, "compute_quote_unit": "算力",
        "compute_quote_expires_at": None, "approval_window_expires_at": None,
        "preview": {"customer_label": "甲品牌", "form_prefill": {"brand_id": 101}},
        "reason_facts": [], "domain_ref": {},
        "execution_id": _EXECUTION_ID, "execution_state": "queued",
    }

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {"id": 601, "is_admin": True}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(ops.router)
    monkeypatch.setattr(ops, "_authorized_context", _fake_ctx)
    monkeypatch.setattr(ops, "load_intent", lambda *a, **k: row)
    monkeypatch.setattr(ops, "cancel_intent", lambda *a, **k: row)

    class _Cur:
        """status 端点走裸 cursor 查 execution_id —— 给它一个会返回那一行的替身,
        否则它会以 500 收场,而 500 同样满足「不是 404」,矩阵就白打了。"""

        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return dict(row)

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("db.connection.get_db", lambda: _Conn())

    calls = _concrete_calls()
    # 分母自证:枚举出来的路由**逐个**都要有对应请求(不是"我发了几个")。
    assert {c[3] for c in calls} == {r[2] for r in frozen_object_routes()}
    assert "operation_status" in {c[3] for c in calls}

    with TestClient(app) as client:
        # 🔁 先证明"分配还在"时它们**不是** 404 —— 否则下面的全 404 可能只是路由不通。
        before = {}
        for method, url, body, func_name in calls:
            resp = client.request(method, url, json=body)
            before[func_name] = resp.status_code
        assert all(code != 404 for code in before.values()), before

        # 撤销分配
        allowed.clear()
        after = {}
        for method, url, body, func_name in calls:
            resp = client.request(method, url, json=body)
            after[func_name] = resp.status_code
    assert all(code == 404 for code in after.values()), after


# ══════════════════════════════════════════════════════════════════════════
# ③ system page 重建原子化
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def kb_tables():
    from db.kb_db import init_kb_tables

    init_kb_tables()
    yield


def _sys_chunk_snapshot() -> list[tuple]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT source_type, source_slug, content FROM kb_chunks "
            "WHERE source_type LIKE 'sys\\_%' ORDER BY source_slug, source_type, content"
        )
        return [tuple(r.values()) for r in cur.fetchall()]
    finally:
        conn.close()


def test_poisoned_page_aborts_the_rebuild_and_leaves_the_old_release_intact(kb_tables, tmp_path):
    """🔴 工单原话:「中途注毒 → 旧库一行不少」。

    做法:先用两页干净页建出一份 release(记快照)→ 再加一页含旧词的页 →
    重建必须抛异常,且快照**逐行相同**。
    """
    from tools.xiaobang_system_kb import reindex_system

    def page(slug: str, body: str) -> str:
        return (
            "---\n"
            "route: /{0}\n".format(slug)
            + "page_name: {0} 页\n".format(slug)
            + "visible_to: both\n"
            "---\n\n"
            "## 用途\n\n{0}\n".format(body)
        )

    (tmp_path / "alpha.md").write_text(page("alpha", "这一页讲算力怎么用。"), encoding="utf-8")
    (tmp_path / "beta.md").write_text(page("beta", "这一页讲算力上限。"), encoding="utf-8")
    glob_pat = str(tmp_path / "*.md")

    first = reindex_system(pages_glob=glob_pat, with_embeddings=False)
    assert first > 0, "第一次重建就没写进去 —— 后面的快照比较会空即通过"
    snapshot = _sys_chunk_snapshot()
    assert snapshot, "快照为空 —— 分母没了"

    (tmp_path / "gamma.md").write_text(
        page("gamma", "充进来的是充值积分,老板给你设团队额度。"), encoding="utf-8")
    with pytest.raises(KbTerminologyViolation):
        reindex_system(pages_glob=glob_pat, with_embeddings=False)
    assert _sys_chunk_snapshot() == snapshot, "旧 release 被动过了 —— 门开晚了"

    # 🔁 反向对照:把毒页改干净 → 重建成功且真的多了一页(否则上面是恒红)。
    (tmp_path / "gamma.md").write_text(page("gamma", "这一页讲算力上限怎么设。"), encoding="utf-8")
    third = reindex_system(pages_glob=glob_pat, with_embeddings=False)
    assert third > first
    assert _sys_chunk_snapshot() != snapshot


#: 🔴 三档 front-matter 缺陷各自成条。只用「两项都缺」那一档的话,把 route 检查
#:    单独拆掉也会被 page_name 检查兜住 ⇒ 判据对那发变异零判别力(实测存活)。
#:    多行用 chr(10) 拼,不在改写脚本里写转义序列 —— 本轮实测第三次:
#:    heredoc 会把两字符的转义吃成真换行,把注释行拦腰截断。
_NL = chr(10)
_BAD_PAGES = (
    ("both_missing", "这不是合法的 front-matter"),
    ("no_page_name", _NL.join(["---", "route: /has-route", "visible_to: both", "---",
                              "", "## 用途", "", "讲算力。"])),
    ("no_route", _NL.join(["---", "page_name: 有名字", "visible_to: both", "---",
                          "", "## 用途", "", "讲算力。"])),
)


@pytest.mark.parametrize("case_id, bad_body", _BAD_PAGES, ids=[c[0] for c in _BAD_PAGES])
def test_unparsable_page_aborts_instead_of_publishing_a_partial_release(
        kb_tables, tmp_path, case_id, bad_body):
    """解析失败不再**静默跳过**。

    老实现 `logger.exception(...)` 然后 `continue` ⇒ 发布一份缺页 release,
    而且没人收到通知。这条要求它抛 :class:`SystemKbReleaseAborted`。
    """
    from tools.xiaobang_system_kb import SystemKbReleaseAborted, reindex_system

    (tmp_path / "ok.md").write_text(
        "---\nroute: /ok\npage_name: 好页\nvisible_to: both\n---\n\n## 用途\n\n讲算力。\n",
        encoding="utf-8")
    # 🔴 三档各自成条:只用「两项都缺」那一档的话,把 route 检查单独拆掉
    #    也会被 page_name 检查兜住 ⇒ 判据对那发变异零判别力(实测 P13 因此存活)。
    (tmp_path / "broken.md").write_text(bad_body, encoding="utf-8")
    with pytest.raises(SystemKbReleaseAborted):
        reindex_system(pages_glob=str(tmp_path / "*.md"), with_embeddings=False)


def test_rebuild_goes_through_the_transactional_writer_not_per_row_insert():
    """形态判据:``reindex_system`` 必须调原子 writer,不许退回逐条 ``insert_chunk``。"""
    import tools.xiaobang_system_kb as sys_kb

    tree = ast.parse(inspect.getsource(sys_kb))
    target = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "reindex_system")
    called = {getattr(c.func, "attr", None) or getattr(c.func, "id", None)
              for c in ast.walk(target) if isinstance(c, ast.Call)}
    assert "replace_chunks_transactionally" in called, sorted(called)
    assert "insert_chunk" not in called, "还在逐条写 —— 中途失败会留下半份 release"
    assert "clear_system_chunks" not in called, "clear 与 insert 分开 = 非原子"


def test_every_sys_source_type_is_replaceable_in_one_transaction():
    """`REPLACEABLE_SOURCE_TYPES` 必须覆盖 builder 真产出的每个 source_type。

    漏一个 ⇒ 那类只能退回逐条写(而且是静默的)。分母从 AST 机械取。
    """
    from db.kb_db import REPLACEABLE_SOURCE_TYPES

    from . import kb_index_sources as idx

    emitted: set[str] = set()
    for source in idx.builder_sources().values():
        emitted |= idx.builder_facts(source).source_types
    assert emitted, "builder 一个 source_type 都没取到 —— 分母没了"
    assert emitted <= REPLACEABLE_SOURCE_TYPES, sorted(emitted - REPLACEABLE_SOURCE_TYPES)


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 门形态:草稿可存 / violating release 禁激活 / 错误合同带 rule_id+version
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def faq_http(kb_tables):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.faq_api import init_faq_tables, router

    init_faq_tables()
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {"id": 1, "is_admin": True}
        return await call_next(request)

    app.include_router(router)
    with TestClient(app) as client:
        yield client


_MARK = "[R3P8门形态]"
_POISON = "充进来的是**充值积分** · 扣 5% 手续费。"


@pytest.fixture(autouse=True)
def _purge():
    def purge():
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM faq_items WHERE question LIKE %s", ("%" + _MARK + "%",))
            conn.commit()
        finally:
            conn.close()

    purge()
    yield
    purge()


def test_a_violating_draft_can_be_saved(faq_http):
    """🔴 ⑤ 第一条:草稿可存。

    R3-P7 的形态是「任何含旧词的保存一律 400」—— 管理员改一条长 FAQ 改到一半
    想先存下来会被顶回去,而那种压力最后会落在把门关掉上。
    草稿不进索引(``build_faq_chunks`` 明写 ``WHERE is_published = TRUE``),存它对用户零影响。
    """
    response = faq_http.post("/api/admin/faq/items", json={
        "question": "怎么充值草稿" + _MARK, "category": "billing",
        "answer_md": _POISON, "is_published": False,
    })
    assert response.status_code == 201, response.text


def test_publishing_the_same_violating_text_is_refused(faq_http):
    """🔁 同一段文字,`is_published=True` 必须被拒 —— 否则上一条等于把门关了。"""
    response = faq_http.post("/api/admin/faq/items", json={
        "question": "怎么充值发布" + _MARK, "category": "billing",
        "answer_md": _POISON, "is_published": True,
    })
    assert response.status_code == 400, response.text


def test_error_contract_carries_rule_id_and_version(faq_http):
    """⑤ 错误合同必须指得出**哪条规则的哪个版本**。

    尤其这条规则本身刚出过勘误(②④ 域优先级),不带版本号就无从核对。
    """
    detail = faq_http.post("/api/admin/faq/items", json={
        "question": "怎么充值合同" + _MARK, "category": "billing",
        "answer_md": _POISON, "is_published": True,
    }).json()["detail"]
    assert detail["rule_id"] == RULING_RULE_ID == "RULING_TERMINOLOGY_EDU_2026-08-18"
    assert detail["version"] == RULING_VERSION
    assert detail["problems"] and detail["fixes"]
    # 文案要告诉他"可以先存草稿",否则他只知道被拒、不知道下一步。
    assert "草稿" in detail["message"], detail["message"]


def _assert_no_foreign_violating_published_row() -> None:
    """库里不许有**别人留下的**违规已发布行。

    分母自证:同时断言已发布行本身 > 0 —— 一行都没有的话"零违规"是空集合。
    """
    from db.connection import get_connection
    from services.kb_terminology_gate import kb_write_violations

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, question, answer_md FROM faq_items "
                    "WHERE is_published = TRUE")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    assert rows, "faq_items 没有已发布行 —— 分母塌了"
    dirty = [(r["id"], (r["question"] or "")[:20])
             for r in rows
             if kb_write_violations((r["question"] or "") + chr(10) + (r["answer_md"] or ""))]
    assert not dirty, (
        "开跑前库里就有违规的已发布行 {0} —— 这不是本用例的问题,"
        "是别的用例/别的树污染了共享测试库。先清掉再看这条。".format(dirty))


def test_a_published_violating_row_blocks_activation_and_keeps_the_last_release(faq_http):
    """🔴 ⑤ 第二三条:violating release **禁激活** + 上一 release **保持 active**。

    构造:建一行干净的 published FAQ → 重建 → **就地取快照** → 绕过 API 把这行改成
    含旧词(模拟"有人从别的路径改了库")→ 重建必须抛异常 → 快照逐行不变。

    🔴 快照**紧挨着**注毒前后取,不跨其他步骤:第一版把快照取在建行之前,
    于是"前后不等"里混进了「我自己新建的那一行」和其他用例留下的种子差异 ——
    那种判据时红时绿,而时红时绿比恒红更坏(它会被当成 flaky 关掉)。
    """
    from db.connection import get_connection
    from tools.xiaobang_kb_indexer import reindex_faq

    def faq_snapshot():
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT source_slug, content FROM kb_chunks "
                        "WHERE source_type = 'faq' ORDER BY source_slug, content")
            return [tuple(r.values()) for r in cur.fetchall()]
        finally:
            conn.close()

    # 🔴 [R3-P9] 前置条件自证:本用例要求**第一次 reindex_faq() 必须成功**
    #    (成功了才有快照可比)。而 reindex_faq() 现在是 fail-closed 的全表门:
    #    库里只要有**任何一条**违规的已发布行,它就抛 —— 于是本用例会在
    #    「取快照」那一步就死掉,报出来的却是一个跟被测对象无关的错。
    #    实测踩过:另一棵树的旧 seed 灌进同一个测试库,本条随机序下约 1/4 概率红。
    #    这里把它变成**指名道姓**的红,而不是神秘的红。
    _assert_no_foreign_violating_published_row()

    # 🔴 [R3-P9 返工二] 这一行**不能**经 API 建。
    #    `admin_create_faq` 结尾会 `asyncio.create_task(_run_faq_reindex_with_retry())`
    #    —— **fire-and-forget 的后台重建**,完成时刻与 HTTP 响应无序。
    #    于是它可能落在本用例的 `before` 快照与注毒后快照**之间**,
    #    把 kb_chunks 的 faq 行整批换掉 ⇒ 「旧 release 被动过了」判红,
    #    而动它的是我们自己的后台任务,不是被测的那件事。
    #    实测:随机序下约 1/13 概率红,抓到的现场就是 after 比 before 多出 11 行。
    #    本用例的题设本来就是「有人从别的路径改了库」,所以直接建行更贴题,
    #    而且把这条异步竞态从判据里彻底摘掉。
    faq_id = _insert_raw_faq("绕过 API 的行" + _MARK,
                             "这条本来是干净的算力说明。", published=True)
    try:
        reindex_faq()
        before = faq_snapshot()
        assert before, "faq release 为空 —— 分母没了"
        assert any("这条本来是干净的算力说明" in content for _slug, content in before), (
            "我建的那行没进 release —— 后面的比较打不到被测对象")

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE faq_items SET answer_md = %s WHERE id = %s", (_POISON, faq_id))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(KbTerminologyViolation):
            reindex_faq()
        assert faq_snapshot() == before, "旧 release 被动过了 —— 违规 release 抢到了 active"

        # 🔁 反向对照:把那行改干净 → 重建成功且内容真的换了(否则上面是恒红)。
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE faq_items SET answer_md = %s WHERE id = %s",
                        ("改好了,这里说算力。", faq_id))
            conn.commit()
        finally:
            conn.close()
        reindex_faq()
        after = faq_snapshot()
        assert after != before
        assert any("改好了,这里说算力" in content for _slug, content in after)
    finally:
        _delete_raw_faq(faq_id)                 # 直插直删,同样不触发后台重建
        # 收尾里的重建**不许覆盖本用例的判定**:它抛的话,pytest 会把
        # finally 的异常顶掉真正的断言失败,现场就丢了。
        try:
            reindex_faq()
        except KbTerminologyViolation as exc:          # pragma: no cover - 仅诊断用
            print("[清理阶段] reindex_faq 被术语门拒绝(不影响本用例判定):{0}"
                  .format(str(exc)[:200]))


# ══════════════════════════════════════════════════════════════════════════
# ① 从**真 POST /prepare** 起链(真 PG16 + 真 HTTP)
# ══════════════════════════════════════════════════════════════════════════

_CHAIN_REQUEST_IDS = ("r3p8-chain-0001", "r3p8-chain-0002",
                      # [R3-P11 ①] 三对象矩阵各自的 prepare_request_id ——
                      # 不清就会命中幂等,判据读到上一次跑留下的 preview。
                      "r3p11-article", "r3p11-geo_post", "r3p11-quote",
                      "r3p11-channel")


@pytest.fixture(autouse=True)
def _purge_chain_intents():
    """🔴 `prepare` 按 `prepare_request_id` **幂等** —— 同 id 第二次调用返回
    **第一次建的那一行**。不清的话,本文件的链判据读到的是上一次跑留下的
    preview,于是「拆掉 producer 的写入」这发变异**存活**(实测 P01/P02/P03
    三发都因此活着):变异后的代码根本没被执行到,判据读的是陈旧行。

    这就是「预灌库假绿」的一种 —— 幂等是对的,但判据必须自己保证
    「这一次真的走了一遍 producer」。
    """
    def purge():
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM xiaobang_operation_intents WHERE prepare_request_id = ANY(%s)",
                (list(_CHAIN_REQUEST_IDS),))
            conn.commit()
        finally:
            conn.close()

    purge()
    yield
    purge()


def _ops_app(monkeypatch, allowed_brands):
    """真 HTTP 应用。鉴权替身只替「哪个 brand 允许」,其余全走真的。

    🔴 ``prepare_intent`` / ``load_intent`` 都走**真库**(迁移 038 建的表),
       所以 preview 是真的落了库又真的读回来 —— 不是在内存里传一个 dict。
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    import api.xiaobang_operations_api as ops

    class _Ctx:
        owner_user_id = 601

        def public_context(self):
            return {"brand_id": 101, "brand_name": "甲品牌", "quote_id": 77,
                    "data_updated_at": None}

    def _fake_ctx(request, refs, _page=""):
        brand = (refs or {}).get("brand_id")
        if brand is not None and int(brand) not in allowed_brands:
            raise HTTPException(status_code=404, detail={"error_code": "NOT_FOUND"})
        return _Ctx()

    monkeypatch.setattr(ops, "_authorized_context", _fake_ctx)
    # 报价 resolver 会去查 feature_pricing;本条只关心 preview 冻结,给个空报价。
    monkeypatch.setattr(ops, "_resolve_compute_quote",
                        lambda cursor, contract: (None, {"quote_state": "not_applicable"}))

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {"id": 601, "is_admin": True}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(ops.router)
    return TestClient(app)


def test_real_prepare_freezes_form_prefill_and_prefill_returns_it(monkeypatch):
    """🔴 ① 主链:真 `POST /prepare` → 真库 → 真 `GET …/prefill` 拿回 form_prefill。

    链上每一跳都是真的:HTTP → handler → ``prepare_intent`` 落库 →
    ``load_intent`` 读回 → ``build_prefill`` 投影 → HTTP 出参。
    拆掉 producer 那一行写入,这条必红。
    """
    with _ops_app(monkeypatch, {101}) as client:
        prepared = client.post(
            "/api/xiaobang/operations/publish_center/prepare",
            json={"prepare_request_id": "r3p8-chain-0001",
                  "selection": {"brand_id": 101, "article_id": 4201}},
        )
        assert prepared.status_code == 200, prepared.text[:500]
        # 🔴 自证:这一次是**新建**,不是幂等命中上一次的行。
        #    `replayed=True` 意味着判据读的是历史 preview,而不是当前代码的产出。
        assert prepared.json().get("replayed") is False, (
            "幂等命中了旧行 —— 这条判据没有走到当前 producer")
        intent_id = prepared.json()["intent_id"]

        got = client.get("/api/xiaobang/operations/intents/{0}/prefill".format(intent_id))
        assert got.status_code == 200, got.text[:500]
        body = got.json()

    form = body["form_prefill"]
    assert form, "form_prefill 空 —— producer 没冻结,或投影没带出来"
    assert form.get("brand_id") == 101, form
    assert form.get("article_id") == 4201, form
    # 对象标识也必须一路带到出参(§12.3「当前对象」)。
    assert body["object"]["label"], body["object"]
    # rebind_hint 监听的字段就是 form_prefill 的键 —— 两者必须同源。
    assert set(body["rebind_hint"]["watched_fields"]) == set(form), body["rebind_hint"]


def test_the_chain_is_denied_when_the_brand_is_not_assigned(monkeypatch):
    """🔁 反向对照:brand 不在分配集合里 → prepare 就 404(链根本起不来)。

    没有这条,上面那条的 200 可能只是因为鉴权替身恒放行。
    """
    with _ops_app(monkeypatch, set()) as client:
        denied = client.post(
            "/api/xiaobang/operations/publish_center/prepare",
            json={"prepare_request_id": "r3p8-chain-0002",
                  "selection": {"brand_id": 101, "article_id": 4201}},
        )
    assert denied.status_code == 404, denied.text[:300]


def test_every_quote_state_value_survives_the_facade_dlp():
    """🔴 逐态枚举:`quote_state` 的**每一个**取值都要过得了出口 DLP。

    只验一个取值挡不住这类缺陷 —— 素树实测 5 个取值里只有 `"quoted"` 恰好放行
    (它是个普通英文单词),其余 4 个判红。也就是说那个 500 **只在"报不出价"的
    那几档触发**,平时点不出来。所以判据必须把取值枚举完,不能挑一个代表。

    取值集合从**生产代码**里 AST 取,不手写 —— 手写会漏掉以后新增的那一档。
    """
    import ast
    import inspect

    import api.xiaobang_operations_api as ops
    from services.xiaobang_facade_dlp import assert_facade_clean

    tree = ast.parse(inspect.getsource(ops))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "quote_state"
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    values.add(value.value)
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "quote_state"):
                    values.add(node.value.value)
    assert len(values) >= 4, sorted(values)      # 分母自证
    for value in sorted(values):
        assert_facade_clean({"quote_state": value}, where="quote_state:" + value)


# ══════════════════════════════════════════════════════════════════════════
# [R3-P9 ②] FAQ 术语判定进 canonical writer 同事务(行锁)+ 用户面读路径
# ══════════════════════════════════════════════════════════════════════════


def _insert_raw_faq(question: str, answer_md: str, *, published: bool) -> int:
    """绕过一切写入路径,**直插库** —— 模拟 DBA 直改 / 历史遗留行。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO faq_items (question, answer_md, category, sort_order, "
            "is_published, visible_to) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (question, answer_md, "billing", 0, published, "both"),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id
    finally:
        conn.close()


def _delete_raw_faq(faq_id: int) -> None:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM faq_items WHERE id = %s", (faq_id,))
        conn.commit()
    finally:
        conn.close()


def test_a_directly_inserted_violating_published_row_is_not_served(faq_http):
    """🔴 工单判据原话:「测试库直插违规已发布行 → 用户面 GET /api/faq/items 不得吐出」。

    writer 上的门管不住直改库,也管不住历史遗留行。而「库里有一条脏的」与
    「用户看得见一条脏的」是两件事 —— 后者才是伤害,所以读路径也要收。
    """
    dirty = _insert_raw_faq("充值怎么算" + _MARK, "充进来的是**充值积分**,可以花。", published=True)
    clean = _insert_raw_faq("算力怎么算" + _MARK, "充进来的是**充值算力**,可以花。", published=True)
    try:
        resp = faq_http.get("/api/faq/items")
        assert resp.status_code == 200, resp.text[:300]
        served = {item["id"] for item in resp.json()["items"]}
        # 🔁 正对照:干净的那条**必须**在 —— 否则「脏的不在」可能只是整个接口空了。
        assert clean in served, "干净行也没吐出来 —— 分母塌了,不是过滤起作用"
        assert dirty not in served, "违规的已发布行被吐给用户了"
    finally:
        _delete_raw_faq(dirty)
        _delete_raw_faq(clean)


def test_admin_list_still_shows_the_violating_row(faq_http):
    """反向:管理员视图**不过滤** —— 藏起来就永远没人去改它。"""
    from db.faq_db import list_faq_items

    dirty = _insert_raw_faq("充值怎么算" + _MARK, "充进来的是**充值积分**。", published=True)
    try:
        admin_rows = list_faq_items(include_unpublished=True, identity='admin')
        assert dirty in {r["id"] for r in admin_rows}, "管理员也看不见 —— 改不掉了"
        user_rows = list_faq_items(include_unpublished=False, identity='normal_user')
        assert dirty not in {r["id"] for r in user_rows}
    finally:
        _delete_raw_faq(dirty)


def test_update_writer_locks_the_row_before_judging():
    """🔴 结构判据:``update_faq_item`` 必须在**同一个事务**里先 ``FOR UPDATE``
    锁行、再合并、再判定。

    没有锁的话有一条真缝:PATCH-A 只改正文(读到未上架 → 放行),PATCH-B 只点上架
    (读到旧的干净正文 → 放行),两个各自合规的请求合起来产出一条**已上架的违规行**。
    这条打的是「判定与写在同一事务、且判定在 UPDATE 之前」这个顺序,不是「我加了校验」。
    """
    import db.faq_db as faq_db

    tree = ast.parse(inspect.getsource(faq_db))
    target = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "update_faq_item"][0]
    body_src = ast.unparse(target)
    assert "FOR UPDATE" in body_src, "没有行锁"
    assert "_assert_release_text_clean" in body_src, "writer 里没有判定"
    # 顺序:锁 → 判定 → UPDATE。三者的位置关系用源码偏移量比,不靠肉眼。
    pos_lock = body_src.index("FOR UPDATE")
    pos_gate = body_src.index("_assert_release_text_clean")
    pos_write = body_src.index("UPDATE faq_items SET")
    assert pos_lock < pos_gate < pos_write, (pos_lock, pos_gate, pos_write)


def test_writer_gate_and_front_door_share_one_implementation():
    """两道门必须是**同一份实现**。抄第二份必然各自漂移,而漂移方向一定是
    「运行时那份更松」。
    """
    import db.faq_db as faq_db
    import services.kb_terminology_gate as gate

    # writer 侧的 helper 直接调签发实现,不自己判词。
    helper_src = inspect.getsource(faq_db._assert_release_text_clean)
    assert "assert_kb_text_clean" in helper_src
    # 读路径侧同理。
    reader_src = inspect.getsource(faq_db._row_release_violations)
    assert "kb_write_violations" in reader_src
    assert callable(gate.assert_kb_text_clean) and callable(gate.kb_write_violations)


def test_create_writer_rejects_a_violating_publish_without_the_front_door(faq_http):
    """create 侧同样要过门 —— 两个 writer 只关一个 = 另一个是敞开的门。

    与 update 侧拆开是因为它们是两条独立的写入路径:拆掉 create 那道校验,
    只测 update 的用例照样绿。
    """
    from db.faq_db import create_faq_item, get_faq_item
    from services.kb_terminology_gate import KbTerminologyViolation

    with pytest.raises(KbTerminologyViolation):
        create_faq_item(question="怎么充值" + _MARK, category="billing",
                        answer_md="充进来的是**充值积分**。", is_published=True)
    # 🔁 正对照 A:草稿档同一段文本 —— 必须建得出来(拦的是"进 release",不是"这段字")
    draft_id = create_faq_item(question="怎么充值草稿" + _MARK, category="billing",
                               answer_md="充进来的是**充值积分**。", is_published=False)
    assert get_faq_item(draft_id) is not None
    # 🔁 正对照 B:干净文本 + published —— 必须建得出来
    ok_id = create_faq_item(question="怎么充算力" + _MARK, category="billing",
                            answer_md="充进来的是**充值算力**。", is_published=True)
    assert get_faq_item(ok_id)["is_published"] is True


def test_writer_rejects_a_violating_publish_even_without_the_front_door(faq_http):
    """真库判据:直接调 writer(不经 API 前门)把一条草稿标成 published → 必须被拒。

    这正是并发竞态落地时的形态 —— 前门放行过了,writer 是最后一道。
    """
    from db.faq_db import update_faq_item
    from services.kb_terminology_gate import KbTerminologyViolation

    draft = _insert_raw_faq("充值怎么算" + _MARK, "充进来的是**充值积分**。", published=False)
    try:
        with pytest.raises(KbTerminologyViolation):
            update_faq_item(draft, is_published=True)
        # 抛了还不够:那一行**不能**真的被标成 published(事务要回滚干净)。
        from db.faq_db import get_faq_item
        assert get_faq_item(draft)["is_published"] is False, "抛了异常但库里已经上架了"
        # 🔁 正对照:同一跳换成干净正文 → 必须成功,证明拦的是词不是「这条路不通」。
        update_faq_item(draft, answer_md="充进来的是**充值算力**。", is_published=True)
        assert get_faq_item(draft)["is_published"] is True
    finally:
        _delete_raw_faq(draft)


# ══════════════════════════════════════════════════════════════════════════
# [R3-P9 ④] reindex_system 零 glob 匹配 → abort,不留 fail-open 角
# ══════════════════════════════════════════════════════════════════════════


def test_zero_glob_match_aborts_instead_of_publishing_an_empty_release(kb_tables, tmp_path):
    """🔴 零匹配不是「没事发生」。

    原来是 warning + ``return 0``:调用方拿到 0,与「本来就 0 页」无法区分。
    真实触发形态是部署期 cwd 不对 / 页目录没进镜像 —— 那一刻正确行为是
    「什么都别动」,而不是安静地宣布重建成功。
    """
    from tools.xiaobang_system_kb import SystemKbReleaseAborted, reindex_system

    def page(slug: str, body: str) -> str:
        return (
            "---" + chr(10)
            + "route: /{0}".format(slug) + chr(10)
            + "page_name: {0} 页".format(slug) + chr(10)
            + "visible_to: both" + chr(10)
            + "---" + chr(10) + chr(10)
            + "## 用途" + chr(10) + chr(10) + body + chr(10)
        )

    good = tmp_path / 'live'
    good.mkdir()
    (good / 'alpha.md').write_text(page('alpha', '这一页讲算力怎么用。'), encoding='utf-8')
    assert reindex_system(pages_glob=str(good / '*.md'), with_embeddings=False) > 0
    snapshot = _sys_chunk_snapshot()
    assert snapshot, '快照为空 —— 分母没了'

    empty = tmp_path / 'gone'
    empty.mkdir()
    with pytest.raises(SystemKbReleaseAborted):
        reindex_system(pages_glob=str(empty / '*.md'), with_embeddings=False)
    assert _sys_chunk_snapshot() == snapshot, '旧 release 被动了'


def test_only_underscore_pages_also_aborts(kb_tables, tmp_path):
    """独立一档:目录里**有** md 文件、但全被 ``_*.md`` 规则排掉 —— 同样是零匹配。

    与上一条拆开是因为它们走的是不同的「变成空」的路径(glob 空 vs 过滤后空),
    合成一条的话拆掉其中一个会被另一个兜住。
    """
    from tools.xiaobang_system_kb import SystemKbReleaseAborted, reindex_system

    only_draft = tmp_path / 'drafts'
    only_draft.mkdir()
    (only_draft / '_example.md').write_text('---' + chr(10) + 'route: /x' + chr(10)
                                            + '---' + chr(10), encoding='utf-8')
    with pytest.raises(SystemKbReleaseAborted):
        reindex_system(pages_glob=str(only_draft / '*.md'), with_embeddings=False)


# ══════════════════════════════════════════════════════════════════════════
# [R3-P9 ③] 包内模块一律相对导入 —— 单测独立可跑,不靠全量跑的 import 顺序
# ══════════════════════════════════════════════════════════════════════════


def test_no_module_in_this_package_imports_itself_by_absolute_path():
    """🔴 常驻判据:本包任何模块都不许写 ``from tests.xiaobang_... import ...``。

    绝对路径只在「rootdir 恰好在 ``sys.path`` 上」时能解析。全量跑时
    conftest / 更早的模块已经把它放上去了,于是绝对导入**看起来是好的**;
    单文件独立跑就 ImportError。
    「判据的可运行性依赖别的判据先跑过」是一种沉默的耦合 ——
    它不会让任何东西变红,只会在你最需要单独跑一个文件的时候塌掉。

    这条是**常驻**的:以后谁再写一次绝对导入,它当场红。
    """
    here = Path(__file__).resolve().parent
    modules = sorted(here.glob('*.py'))
    # 分母自证:目录里真的有一堆模块(glob 写错 → 空集合 → 恒绿)。
    assert len(modules) >= 10, modules

    offenders = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and (node.module or '').startswith('tests.'):
                    offenders.append((path.name, node.module, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith('tests.'):
                        offenders.append((path.name, alias.name, node.lineno))
    assert not offenders, offenders


def test_the_absolute_import_lock_can_actually_see_an_offender():
    """🔁 反向对照:把一段带绝对导入的源码喂给同一套判别逻辑 → 必须命中。

    没有这条,上面那条「零违规」可能只是 AST 遍历写错了从来没命中过任何东西。
    """
    bad = ast.parse('from tests.xiaobang_vnext_2026_08_18 import kb_index_sources')
    hits = [n for n in ast.walk(bad)
            if isinstance(n, ast.ImportFrom)
            and n.level == 0 and (n.module or '').startswith('tests.')]
    assert hits, '判别逻辑连教科书式的违规都看不见'
    # 相对导入不该被误判
    good = ast.parse('from . import kb_index_sources')
    assert not [n for n in ast.walk(good)
                if isinstance(n, ast.ImportFrom)
                and n.level == 0 and (n.module or '').startswith('tests.')]


# ══════════════════════════════════════════════════════════════════════════
# [R3-P10 ①] seed 写径(init_faq_tables)过同款术语门 —— 第五条写径不许裸奔
# ══════════════════════════════════════════════════════════════════════════

_SEED_POISON = ("怎么充值", "billing", 0, 1, 0, "充进来的是**充值积分**,可以花。")
_SEED_CLEAN = ("怎么充值", "billing", 0, 1, 0, "充进来的是**充值算力**,可以花。")


def _faq_row_count() -> int:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS c FROM faq_items")
        return int(cur.fetchone()["c"])
    finally:
        conn.close()


def _count_answer_like(pattern: str) -> int:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS c FROM faq_items WHERE answer_md LIKE %s",
                    (pattern,))
        return int(cur.fetchone()["c"])
    finally:
        conn.close()


def test_poisoned_seed_is_refused_by_the_startup_sync(kb_tables, monkeypatch):
    """🔴 工单判据原话:「seed 注毒 → 启动同步必须拒」。

    这条打的是**老库同步**那一支(``UPDATE ... SET answer_md``,只覆盖
    ``updated_by IS NULL`` 的行)。它每次部署都跑,是 seed 文本进库的常态路径。
    """
    import db.faq_db as faq_db
    from api.faq_api import init_faq_tables
    from services.kb_terminology_gate import KbTerminologyViolation

    init_faq_tables()                       # 先保证是「老库」形态(表里有行)
    assert _faq_row_count() > 0, "faq_items 空 —— 走的是首部署分支,这条打不到同步那一支"

    monkeypatch.setattr(faq_db, "_INITIAL_SEED", [_SEED_POISON])
    with pytest.raises(KbTerminologyViolation):
        init_faq_tables()

    # 拒绝还不够:那段脏文本**不能**已经落库(门必须在写之前)。
    assert _count_answer_like("%充值积分%") == 0, "抛了,但脏 seed 已经同步进库了"

    # 🔁 正对照:同一跳换成干净 seed → 必须真的写进去(拦的是词,不是这条路)
    monkeypatch.setattr(faq_db, "_INITIAL_SEED", [_SEED_CLEAN])
    init_faq_tables()
    assert _count_answer_like("%充值算力%") > 0, "干净 seed 也没写进去 —— 拦的不是词"

    monkeypatch.undo()
    init_faq_tables()                       # 还原成真 seed


def test_poisoned_seed_is_refused_on_the_fresh_deploy_branch(kb_tables, monkeypatch):
    """首部署那一支(``INSERT``)单独一档。

    与上一条拆开:两支是**两条独立的写入路径**,只关一个 = 另一个是敞开的门。
    合成一条的话,拆掉 INSERT 支的保护会被 UPDATE 支的判据兜住。
    """
    import db.faq_db as faq_db
    from api.faq_api import init_faq_tables
    from db.connection import get_connection
    from services.kb_terminology_gate import KbTerminologyViolation

    # 造出「全新部署」形态:清空 faq_items
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM faq_votes")
        cur.execute("DELETE FROM faq_items")
        conn.commit()
    finally:
        conn.close()
    assert _faq_row_count() == 0

    monkeypatch.setattr(faq_db, "_INITIAL_SEED", [_SEED_POISON])
    try:
        with pytest.raises(KbTerminologyViolation):
            init_faq_tables()
        assert _faq_row_count() == 0, "抛了,但首部署 seed 已经插进去了"
    finally:
        monkeypatch.undo()
        init_faq_tables()                   # 用真 seed 还原,别污染后面
    assert _faq_row_count() > 0


def test_a_dirty_seed_still_leaves_the_tables_created(kb_tables, monkeypatch):
    """🔴 门的**位置**判据:脏 seed 只挡「写文本」,不许连表都建不出来。

    ``init_faq_tables()`` 在 ``server.py`` 里被 ``try/except Exception`` 包着 ——
    抛出去只记一条 warning,应用照跑。若把门放在 DDL 之前,一个脏 seed 就会让
    FAQ 模块拿到一个**表不存在**的库,那比脏 seed 坏得多。
    """
    import db.faq_db as faq_db
    from api.faq_api import init_faq_tables
    from db.connection import get_connection
    from services.kb_terminology_gate import KbTerminologyViolation

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS faq_votes")
        cur.execute("DROP TABLE IF EXISTS faq_feedback")
        cur.execute("DROP TABLE IF EXISTS faq_items")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(faq_db, "_INITIAL_SEED", [_SEED_POISON])
    try:
        with pytest.raises(KbTerminologyViolation):
            init_faq_tables()
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass(%s) AS t", ("public.faq_items",))
            assert cur.fetchone()["t"] is not None, "脏 seed 把建表也一起挡了 —— 门放早了"
        finally:
            conn.close()
        assert _faq_row_count() == 0
    finally:
        monkeypatch.undo()
        init_faq_tables()


# ══════════════════════════════════════════════════════════════════════════
# [R3-P10 ① 制度化] faq_items 写正文的路径 census —— 手写清单作废
# ══════════════════════════════════════════════════════════════════════════

_FAQ_TEXT_GATES = ("_assert_seed_text_clean", "_assert_release_text_clean")


def _faq_text_writer_functions() -> list:
    """机械枚举:``db/faq_db.py`` 里**写 faq_items 且沾正文**的函数。

    🔴 判「沾不沾正文」不能只看 SQL 字面量 —— ``update_faq_item`` 的 SQL 是
    f-string 拼出来的(``SET {fields}``),字面量里根本没有 ``question``/``answer_md``,
    按字面量判会把它误判成「不沾正文」从而豁免掉,那正好放走了本该守的那个。
    所以判据是:函数体里既有 faq_items 的写语句,**且**整段源码提到正文列名。
    宁可多要求一个门,不可少要求一个。
    """
    import db.faq_db as faq_db

    tree = ast.parse(inspect.getsource(faq_db))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.unparse(node)
        writes = ("INSERT INTO faq_items" in body) or ("UPDATE faq_items" in body)
        touches_text = ("question" in body) or ("answer_md" in body)
        if writes and touches_text:
            out.append(node.name)
    return sorted(out)


def test_every_faq_text_write_path_is_gated():
    """🔴 覆盖面:每一条写 faq_items 正文的路径都要过门。

    不数「我加了几处」,而是用结构锚取全集,再要求每个都出现门调用。
    新增第六条写径而忘了加门,这条转红 —— 上一轮 ① 的教训:
    **手写分母漏掉的那一项不会让任何判据变红**。
    """
    import db.faq_db as faq_db

    writers = _faq_text_writer_functions()
    # 分母自证:枚举必须非空,且已知的三条都在。
    assert writers, "写径枚举为空 —— 分母塌了,不是没有洞"
    for expected in ("init_faq_tables", "create_faq_item", "update_faq_item"):
        assert expected in writers, (expected, writers)

    tree = ast.parse(inspect.getsource(faq_db))
    naked = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in writers:
            continue
        body = ast.unparse(node)
        if not any(gate in body for gate in _FAQ_TEXT_GATES):
            naked.append(node.name)
    assert not naked, "这些写正文的路径没有过门:{0}".format(naked)


def test_the_write_path_census_can_see_a_naked_writer():
    """🔁 反向对照:虚构第六条写径(写正文、不过门)→ 判别逻辑必须判它红。

    没有这条,上面那条「零裸奔」可能只是枚举逻辑写错了从来没命中过任何函数。
    """
    fake = chr(10).join([
        "def sync_faq_from_somewhere_new(cur, rows):",
        "    for question, answer_md in rows:",
        "        cur.execute(_SQL, (answer_md, question))",
        "",
    ]).replace("_SQL", repr("UPDATE faq_items SET answer_md = %s WHERE question = %s"))
    tree = ast.parse(fake)
    node = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    body = ast.unparse(node)
    assert "UPDATE faq_items" in body and "answer_md" in body, "分类规则看不见它"
    assert not any(gate in body for gate in _FAQ_TEXT_GATES), (
        "反向对照失效:裸写径被判成已过门")


def test_non_text_write_paths_are_not_required_to_be_gated():
    """🔁 另一侧:只动计数/排序/可见性的写径**不该**被要求过门。

    否则这把锁会退化成「所有碰 faq_items 的函数都要过门」,
    下一个人只会给 ``_recalc_counts`` 加一句假调用把它糊过去。
    """
    writers = _faq_text_writer_functions()
    for counter_only in ("_recalc_counts", "_recalc_feedback_count",
                         "reorder_items", "delete_faq_item"):
        assert counter_only not in writers, counter_only


# ══════════════════════════════════════════════════════════════════════════
# [R3-P10 ③] vote/feedback 存在性 oracle —— **记录在案 · 威胁模型外 · 不修**
# ══════════════════════════════════════════════════════════════════════════


def test_documents_the_faq_existence_oracle_accepted_out_of_scope(faq_http):
    """📌 **这条不是"发现了洞",是把一个已知且被接受的行为钉住**(Review 裁定:
    威胁模型外,标注即可)。

    ## 行为

    ``POST /api/faq/vote`` 用 ``get_faq_item(faq_id)`` 取行 —— 那是
    ``SELECT * FROM faq_items WHERE id = %s``,**既不看 is_published,
    也不过 R3-P9 ② 的用户面过滤**。于是登录用户可以区分:

    | 目标 | 在 ``GET /api/faq/items`` 里 | ``POST /api/faq/vote`` |
    |------|------|------|
    | 违规的已发布行(R3-P9 ② 隐藏) | 看不到 | **200** |
    | 草稿(``is_published=False``,一直隐藏) | 看不到 | **200** |
    | 不存在的 id | 看不到 | **404** |

    ⇒ 「id=N 存在但对我隐藏」与「id=N 不存在」可区分 = 存在性 oracle。

    ## 为什么判它在威胁模型外

    * 泄露面 = **一个自增整数 id 是否对应一条 FAQ**。FAQ 是全站公共帮助文案,
      不是租户数据;隐藏的原因是「文案用了废弃说法」或「还没写完」,
      **不是**「这条属于别的客户」。对象级越权(客户/品牌/报价)走的是
      ``resolve_authorized_context``,与这里是两套东西。
    * 攻击者拿到的信息量 = 「后台大概有多少条 FAQ」。没有正文、没有标题、没有归属。

    ## 🔴 归因:这不是 R3-P9 造出来的

    草稿那一行证明 oracle **早就在**(``is_published=False`` 的隐藏从 FAQ 模块
    第一天就有,而 vote 从来不看它)。R3-P9 ② 只是往「隐藏行」这个集合里
    多加了一类(违规的已发布行),没有新增可区分性的**机制**。

    ## 如果以后决定修

    修法是让 ``api_vote`` / ``api_submit_feedback`` 复用用户面可见性
    (即 ``get_faq_item`` 之后再过一次和 ``list_faq_items`` 同源的过滤),
    对隐藏行一律 404。**那时请直接删掉本条判据** —— 它钉的是现状,不是不变式。
    """
    from db.connection import get_connection

    def _insert(question, answer, published):
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO faq_items (question, answer_md, category, sort_order, "
                "is_published, visible_to) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (question, answer, "billing", 0, published, "both"),
            )
            new_id = cur.fetchone()["id"]
            conn.commit()
            return new_id
        finally:
            conn.close()

    dirty = _insert("存在性oracle脏行" + _MARK, "充进来的是**充值积分**。", True)
    draft = _insert("存在性oracle草稿" + _MARK, "干净的算力说明。", False)
    try:
        listed = {item["id"] for item in faq_http.get("/api/faq/items").json()["items"]}
        # 前提:两条都确实对用户不可见(否则下面比的不是"隐藏 vs 不存在")
        assert dirty not in listed and draft not in listed, listed

        seen = faq_http.post("/api/faq/vote", json={"faq_id": dirty, "vote": "up"})
        drafted = faq_http.post("/api/faq/vote", json={"faq_id": draft, "vote": "up"})
        missing = faq_http.post("/api/faq/vote", json={"faq_id": 99999999, "vote": "up"})

        # 现状钉死:隐藏行 200、不存在 404 —— 两者可区分。
        assert seen.status_code == 200, seen.text[:200]
        assert drafted.status_code == 200, drafted.text[:200]
        assert missing.status_code == 404, missing.text[:200]
        # 🔴 归因锚:草稿那一档 200 证明 oracle 与 R3-P9 ② 无关(它一直是这样)。
        assert drafted.status_code == seen.status_code
    finally:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM faq_votes WHERE faq_id IN (%s, %s)", (dirty, draft))
            cur.execute("DELETE FROM faq_items WHERE id IN (%s, %s)", (dirty, draft))
            conn.commit()
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# [R3-P11 ①] 三对象 × (冻结 / 恢复 / 预填) 矩阵
# ══════════════════════════════════════════════════════════════════════════

#: 三个对象各自的 selection、期望冻结字段、期望对象类型。
#: 分母写在一处,下面几组判据共用 —— 免得「矩阵」变成「我记得测了几个」。
_OBJECT_MATRIX = (
    ("article", {"brand_id": 101, "article_id": 4201}, "article_id", "article"),
    ("geo_post", {"brand_id": 101, "geo_post_id": 5150}, "geo_post_id", "geo_image_post"),
    ("quote", {"brand_id": 101, "quote_id": 77}, "quote_id", "quote"),
)


def test_object_matrix_covers_every_prefillable_kind():
    """🔴 分母自证:矩阵必须覆盖 ``_FORM_PREFILL_FIELDS`` 里**每一个**对象字段。

    分母写死成「我列的三个」会重演 R3-P9 ① 那个坑,所以这里反过来:
    从生产端的字段表取全集,要求矩阵把它们都排到 ——
    以后 producer 多冻结一个字段,这条自己红。
    """
    import api.xiaobang_operations_api as ops

    covered = {field for _n, _s, field, _k in _OBJECT_MATRIX}
    expected = set(ops._FORM_PREFILL_FIELDS) - {"brand_id"}   # brand 是每组都带的底座
    assert covered == expected, (covered, expected)


def _frozen_row_preview(intent_id: str) -> dict:
    """直接读库里那一行的 preview —— 不看回包(回包是投影,可能把键改了名)。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT preview FROM xiaobang_operation_intents WHERE intent_id = %s",
                    (intent_id,))
        row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "库里没有这一行 —— 链断在 prepare"
    return dict(row["preview"] or {})


@pytest.mark.parametrize("name,selection,field,kind", _OBJECT_MATRIX,
                         ids=[m[0] for m in _OBJECT_MATRIX])
def test_freeze_recover_prefill_for_every_object(monkeypatch, name, selection, field, kind):
    """🔴 三对象 × (冻结 / 恢复 / 预填) 全打,真 HTTP + 真库。

    ① **冻结**:真 ``POST /prepare`` 之后,**库里那一行**的 preview 带这个对象;
    ② **恢复**:``_refs_from_frozen_intent`` 能从冻结快照把它还原成 refs
       —— 还不回来 = 恢复端点又在拿空 refs 做授权(R3-P8 那个洞的翻版:
       对象在快照里,只是没人拿它去问「还归不归你」);
    ③ **预填**:真 ``GET /prefill`` 把它下发到页面要填的字段里。
    """
    import api.xiaobang_operations_api as ops

    request_id = "r3p11-{0}".format(name)
    with _ops_app(monkeypatch, {101}) as client:
        prepared = client.post("/api/xiaobang/operations/publish_center/prepare", json={
            "selection": dict(selection), "prepare_request_id": request_id,
        })
        assert prepared.status_code == 200, prepared.text[:400]
        # 🔴 幂等回放陷阱:必须确认这一次真的新建了,否则读的是上一次跑留下的行,
        #    「拆掉 producer 写入」那发变异就会存活(R3-P8 实测三发全活)。
        assert prepared.json().get("replayed") is False, prepared.json()
        intent_id = prepared.json()["intent_id"]

        served = client.get(
            "/api/xiaobang/operations/intents/{0}/prefill".format(intent_id))

    # ① 冻结(读库)
    frozen = _frozen_row_preview(intent_id)
    assert frozen.get("form_prefill", {}).get(field) == selection[field], frozen
    kinds = {item.get("resource_kind") for item in frozen.get("object_items") or []}
    assert kind in kinds, (kind, kinds)

    # ② 恢复
    refs = ops._refs_from_frozen_intent({"preview": frozen})
    assert refs.get(field) == selection[field], refs

    # ③ 预填(真 HTTP 出参)
    assert served.status_code == 200, served.text[:400]
    body = served.json()
    assert body["form_prefill"].get(field) == selection[field], body["form_prefill"]
    assert kind in {item.get("resource_kind") for item in body["object"]["items"]}


def test_every_frozen_object_kind_can_be_recovered():
    """🔴 接线判据:``_display_object_items`` 能冻结的每一种 ``resource_kind``,
    ``_OBJECT_KIND_TO_REF_FIELD`` 都必须认得。

    冻结时写了一种对象、恢复时认不出它 —— 那个对象就**永远不会被拿去问
    「还归不归你」**。分母用 AST 从 producer 取,不是手写。
    """
    import api.xiaobang_operations_api as ops

    src = inspect.getsource(ops._display_object_items)
    emitted = set(re.findall(r'"resource_kind":\s*"(\w+)"', src))
    emitted |= set(re.findall(r'\(\s*"(\w+)",\s*"\w+_id"\s*\)', src))
    assert emitted, "取不到 producer 会冻结的对象类型 —— 判据失效"
    missing = emitted - set(ops._OBJECT_KIND_TO_REF_FIELD)
    assert not missing, "这些对象冻结得进去、恢复时认不出来:{0}".format(sorted(missing))
    # 反向:映射表里的字段必须都在 allowlist 内(否则恢复出来的 refs 会被丢掉)
    for kind, field in ops._OBJECT_KIND_TO_REF_FIELD.items():
        assert field in ops._SELECTION_ALLOWLIST, (kind, field)


def test_the_kind_mapping_lock_can_see_a_missing_kind():
    """🔁 反向对照:虚构一个 producer 冻结了、映射表里没有的对象类型 → 必须判红。"""
    import api.xiaobang_operations_api as ops

    emitted = {"brand", "quote", "article", "geo_image_post", "a_brand_new_kind"}
    missing = emitted - set(ops._OBJECT_KIND_TO_REF_FIELD)
    assert missing == {"a_brand_new_kind"}, missing


# ══════════════════════════════════════════════════════════════════════════
# [R3-P11 ①] geo_post 的**现役对象授权**:撤销位翻转必 404(真库真函数)
# ══════════════════════════════════════════════════════════════════════════

#: 列名/类型抄自生产 schema dump(tests/publish_dispatch_industry_2026_08_17/
#: prod_schema_2026-08-17.sql 的 geo_douyin_posts),不是手写猜的 ——
#: 「测试 schema 与生产不同构照样全绿」是本仓付过费的坑。
_GEO_POST_DDL = """
CREATE TABLE IF NOT EXISTS geo_douyin_posts (
    id bigint PRIMARY KEY,
    brand_id integer,
    created_by integer NOT NULL DEFAULT 1,
    content_type character varying(16) DEFAULT 'image_post' NOT NULL,
    title text,
    status character varying(24) DEFAULT 'draft' NOT NULL,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)
"""


@pytest.fixture()
def geo_post_rows():
    """真建表 + 真插行(一条在用、一条软删)。"""
    from db.connection import get_connection

    def _cleanup():
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM geo_douyin_posts WHERE id IN (7001, 7002)")
            conn.commit()
        finally:
            conn.close()

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_GEO_POST_DDL)
        conn.commit()
    finally:
        conn.close()
    _cleanup()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO geo_douyin_posts (id, brand_id, title) VALUES (%s,%s,%s)",
                    (7001, 101, "甲品牌图文"))
        cur.execute("INSERT INTO geo_douyin_posts (id, brand_id, title, deleted_at) "
                    "VALUES (%s,%s,%s, now())", (7002, 101, "已删图文"))
        conn.commit()
    finally:
        conn.close()
    yield {"live": 7001, "deleted": 7002}
    _cleanup()


def _resolve_with_brand_gate(monkeypatch, allowed_brands, refs):
    """真调 ``resolve_authorized_context``;只把「哪个 brand 允许」换成替身。

    🔴 替身只替「允许集合」,**不替「要不要校验」** —— 后者正是被测的那件事。
    ``_resource_hint`` 那一跳走**真库**,所以 geo_post → brand 的归属是真查出来的。
    brands 表那一条查询替成常量(测试库里未必有 101 这行),
    ``geo_douyin_posts`` 那条照旧走真库。
    """
    from fastapi import HTTPException

    import services.customer_operation_plan as plans

    original_fetch = plans._fetch_row

    def _gate(request, brand_id, allow_null=False):
        if brand_id is not None and int(brand_id) not in allowed_brands:
            raise HTTPException(status_code=404, detail="上下文不可用,请重新选择客户")

    def _fetch(sql, params):
        if "FROM brands" in sql:
            return {"id": params[0], "name": "甲品牌", "updated_at": None}
        return original_fetch(sql, params)

    monkeypatch.setattr(plans, "require_brand_access", _gate)
    monkeypatch.setattr(plans, "_fetch_row", _fetch)
    # 🔴 报价「按 brand 找最新一张」是**与本条无关的旁路**:它查 `quotes.deleted_at`,
    #    而测试库的 quotes 没有这一列(生产有)。被测的是
    #    geo_post → brand → require_brand_access 这一跳,不是报价兜底。
    #    这里替掉它,而**不是**放宽被测的那一跳 —— 替身只能替旁路。
    monkeypatch.setattr(plans, "_latest_quote_for_brand", lambda brand_id: None)

    class _Req:
        class state:
            user = {"id": 601, "is_admin": False}

    return plans.resolve_authorized_context(_Req(), refs, "/publish")


def test_geo_post_authorization_flips_to_404_when_the_client_is_revoked(
        monkeypatch, geo_post_rows):
    """🔴 工单原话:「恢复授权撤销位翻转必 404」。

    先证明**没撤销时不是 404**(否则「撤销后 404」可能只是这条路根本不通),
    再撤销,同一跳必须 404。
    """
    from fastapi import HTTPException

    live = geo_post_rows["live"]

    ctx = _resolve_with_brand_gate(monkeypatch, {101}, {"geo_post_id": live})
    assert ctx.geo_post_id == live, ctx
    assert ctx.brand_id == 101, ctx           # 真从库里反查出来的归属
    assert ctx.refs().get("geo_post_id") == live, ctx.refs()

    with pytest.raises(HTTPException) as caught:
        _resolve_with_brand_gate(monkeypatch, set(), {"geo_post_id": live})
    assert caught.value.status_code == 404


def test_soft_deleted_geo_post_is_404_like_any_other_unavailable_object(
        monkeypatch, geo_post_rows):
    """软删的图文走**同一个** 404 出口,不新增一种失败形态。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        _resolve_with_brand_gate(monkeypatch, {101}, {"geo_post_id": geo_post_rows["deleted"]})
    assert caught.value.status_code == 404


def test_geo_post_from_another_client_is_404(monkeypatch, geo_post_rows):
    """跨租户:图文属于 brand 101,当前身份只被允许 brand 999 → 404。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        _resolve_with_brand_gate(monkeypatch, {999}, {"geo_post_id": geo_post_rows["live"]})
    assert caught.value.status_code == 404


def test_geo_post_id_reaches_the_authorization_layer_at_all(monkeypatch, geo_post_rows):
    """🔁 接线自证:refs 里换成一个**不存在**的 geo_post_id → 也必须 404。

    没有这条,上面几条的 404 有可能是「这个字段压根没被读过、于是 brand 为空
    走了别的分支」。不存在的 id 必须走到「查不到行 → 404」那一支。
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        _resolve_with_brand_gate(monkeypatch, {101}, {"geo_post_id": 987654321})
    assert caught.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# [R3-P11 ②] 目标渠道/账号:producer 真写 + 出得了 DLP 的门
# ══════════════════════════════════════════════════════════════════════════


def test_prepare_freezes_a_real_channel(monkeypatch):
    """🔴 producer 真写 channel —— 不是"前端渲染一个恒空的字段"。

    ## 工单前提与实测不符,记在这里

    工单说「后端 prefill.channel 已返齐」。**基线 `8ee45c24` 上不是这样**:
    ``operation_prepare`` 写进 ``preview`` 的键只有 5 个
    (customer_label / scope / pending_manifest_inputs / object_items / form_prefill),
    根本没有 ``channel``;``build_prefill`` 那句 ``preview.get("channel") or {}``
    于是**恒返空字典**。只在前端加渲染,得到的是一个永远走降级文案的**死元素** ——
    与 R3-P7 那次「夹具里有、生产端一个字没写」是同一个形态。
    所以本包把 producer 这一半补上了,本条钉的就是它。

    真 HTTP 起链;另外这条也是 DLP 的守门判据 ——
    channel 里的机读值必须落在**已登记**的机读键上(键名叫 `option_id` 时
    `GET /prefill` 会直接 500:`未翻译的内部枚举 'douyin_main'`)。
    """
    import api.xiaobang_operations_api as ops

    request_id = "r3p11-channel"
    with _ops_app(monkeypatch, {101}) as client:
        prepared = client.post("/api/xiaobang/operations/publish_center/prepare", json={
            "selection": {"brand_id": 101, "geo_post_id": 5150,
                          "channel_option_id": "douyin_main"},
            "prepare_request_id": request_id,
        })
        assert prepared.status_code == 200, prepared.text[:400]
        assert prepared.json().get("replayed") is False, prepared.json()
        intent_id = prepared.json()["intent_id"]
        served = client.get(
            "/api/xiaobang/operations/intents/{0}/prefill".format(intent_id))

    # ① 真的冻进了库(读库,不看回包)
    frozen = _frozen_row_preview(intent_id)
    assert frozen.get("channel"), "preview 里没有 channel —— producer 没写"
    assert frozen["channel"].get("channel_option_id") == "douyin_main", frozen["channel"]

    # ② 真的出得了门(DLP 不拦 + 值原样到前端)
    assert served.status_code == 200, served.text[:400]
    channel = served.json().get("channel") or {}
    assert channel.get("channel_option_id") == "douyin_main", channel
    assert channel.get("resource_kind") == "geo_image_post", channel


def test_channel_reports_eligibility_as_unverified_while_it_is_pending():
    """🔴 渠道资格没核过就必须说没核 —— 不许显示成"已确认"。

    ``manifest["pending"]`` 里有 ``channel_eligibility`` 就意味着「投放账号资不资格」
    这件事**这一轮还算不准**(要等发布域 adapter)。那一刻页面上写一个看起来
    已经确认的账号,比什么都不写更坏。
    这条把 `verified` 与 manifest 的 pending 绑死,而不是写死成常量。
    """
    import api.xiaobang_operations_api as ops

    class _Ctx:
        # 🔴 [工单 V3-A · Codex 三审 P1-9 · 2026-08-28] 这里原来挂着一个
        #    ``owner_user_id = 601`` —— 而现役 AuthorizedAssistantContext 上
        #    **没有**这个字段(机械核过:出现 0 次)。夹具供了生产不会供的东西,
        #    正是这个洞能活到三审的原因之一。现在租户由调用方从
        #    ``_actor_binding`` 现算后**必传关键字**传进来,夹具不再替它作答。
        def public_context(self):
            return {"brand_id": 101, "brand_name": "甲品牌"}

    manifest = ops._object_manifest(_Ctx(), {"brand_id": 101, "geo_post_id": 5150},
                                    resource_kind="geo_image_post",
                                    tenant_owner_id=601)
    # 前提自证:此刻 channel_eligibility 确实还在 pending 里(不然这条恒真)
    assert "channel_eligibility" in manifest["pending"], manifest["pending"]

    channel = ops._frozen_channel(_Ctx(), {"selection": {"geo_post_id": 5150}}, manifest)
    assert channel["verified"] is False, channel

    # 🔁 反向对照:pending 里没有它时,verified 必须翻成 True ——
    #    证明这一位是**跟着 manifest 走**的,不是写死的 False。
    cleared = dict(manifest, pending=[p for p in manifest["pending"]
                                      if p != "channel_eligibility"])
    assert ops._frozen_channel(_Ctx(), {"selection": {}}, cleared)["verified"] is True


def test_channel_never_invents_an_account_name():
    """拿不到就**空字典**,绝不返回空串账号名。

    空串会在页面上渲染成「有这一栏、但它是空的」,比「还没选」更误导 ——
    前端那三档降级文案就是为此存在的(PW 侧逐字钉死)。
    """
    import api.xiaobang_operations_api as ops

    class _Ctx:
        def public_context(self):
            return {}

    channel = ops._frozen_channel(_Ctx(), {"selection": {}},
                                  {"pending": ["channel_eligibility"]})
    assert "channel_option_id" not in channel, channel
    assert all(value != "" for value in channel.values()), channel
