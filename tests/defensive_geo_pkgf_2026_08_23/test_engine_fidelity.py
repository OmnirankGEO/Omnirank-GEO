"""包F ⑥ 保真锁 —— 被测千问引擎的模型名 / 端点 / 联网参数漂移即红。

🔴 这把锁存在的理由是一次**实证的**记账错位
------------------------------------------
2026-08-23 census 实测:``db/monitoring_db.py`` 按 ``qwen3.7-plus`` **计价**,
而 ``tools/ai_visibility/ai_tester.py`` 实际发的是 ``qwen3-max``;
``services/geo_observation/source_hooks.py`` 与 ``server.py`` 也标 3.7-plus。
也就是说我们**按一个模型收钱、用另一个模型干活**,而全仓没有一条判据会红。

所以本文件锁的不是"模型名对不对"(那是 Owner 的商业决定),而是
**四处说法必须同源**:请求体发的、lineage 记的、计价用的、常量声明的。

🔴 为什么锁"端点"和"search_options"而不只是模型名
------------------------------------------------
2026-08-15 那次探测把 3.7-plus 打在 ``text-generation`` 端点上,
得到「必 400 / 只能走 Responses API / 零角标」——**结论是端点选错造成的**。
2026-08-24 生产原地复探:原生 ``multimodal-generation`` 端点 200,
全套 search_options 活(1377 字 / 6 来源 / 30 个正文角标)。

这说明**端点与参数和模型名一样是保真度的一部分**:
· 端点错 ⇒ 400,监测当天全红;
· ``enable_citation`` 掉了 ⇒ 正文角标静默消失,证据链缩水而没人报错;
· ``content`` 形状退回字符串 ⇒ 多模态端点 400;
· 提取器不折平数组 ⇒ ``.strip()`` AttributeError 被外层 except 吞成
  "重试三次后 engine_error" ⇒ **换代的表现是监测全线静默失败**。
每一条都配一发变异(MUT-F17..F22)。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

from services.monitoring_lineage import (QWEN_ENGINE, QWEN_ENGINE_PREVIOUS_MODEL,
                                         QWEN_ENGINE_SWITCHED_ON,
                                         _PLATFORM_CONTRACT)

ROOT = Path(__file__).resolve().parents[2]

#: Owner 2026-08-24 终裁的被测千问模型。**逐字**。
OWNER_RULED_MODEL = "qwen3.7-plus"

#: Owner 给定的探测参数集 —— 逐键逐值。
#: 多一个键 / 少一个键 / 值变了都红:
#: 探测证明可用的是**这一组**,加一个没探过的键就是拿生产去试。
PROBED_SEARCH_OPTIONS = {
    "enable_source": True,
    "enable_citation": True,
    "citation_format": "[<number>]",
    "forced_search": True,
    "search_strategy": "turbo",
}

#: [R2 F-2] 探测证明的**基础参数**集 —— 逐键逐值。
#:
#: 🔴 ``enable_thinking: False`` 在这里不是"可有可无的优化开关",是
#: **资金 × 质量**两条线上的承重件。Review 2026-08-24 生产两臂
#: (同题同参,仅差这一个键):
#:
#: ==================  ======================  ====================
#: 指标                 省略该键                 ``false``
#: ==================  ======================  ====================
#: output_tokens       2876(reasoning 2031)   981
#: 正文 / 角标          1343 字 / 17            1701 字 / 26
#: ==================  ======================  ====================
#:
#: 省略 = thinking 默认开 ⇒ 输出 token 2.9 倍、答案反而更短更少角标。
#: 每一次监测调用都在流血,而 HTTP 两臂都是 200 —— 所以**没有任何
#: 运行时信号会报警**,只能靠这条判据。
PROBED_BASE_PARAMETERS = {
    "enable_thinking": False,
    "enable_search": True,
}

#: 原生多模态端点路径片段。
MULTIMODAL_PATH = "multimodal-generation/generation"
#: 旧文本端点 —— 3.7-plus 走它必 400。出现即红。
TEXT_PATH = "text-generation/generation"

TESTER = "tools/ai_visibility/ai_tester.py"


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8", newline="", errors="replace").read()


def _fn(rel: str, name: str):
    tree = ast.parse(_read(rel))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"{rel} 里找不到 {name} —— 分母塌了")


# ══════════════════════════════════════════════════════════════════════
# 1. 常量本体
# ══════════════════════════════════════════════════════════════════════

def test_qwen_model_is_the_owner_ruled_one():
    """模型名 = Owner 终裁值,且 lineage 合同与它**同源**(不是各写一份)。"""
    assert QWEN_ENGINE["model"] == OWNER_RULED_MODEL, (
        f"被测千问模型漂移:{QWEN_ENGINE['model']!r} != {OWNER_RULED_MODEL!r}")
    assert _PLATFORM_CONTRACT["dashscope"]["model"] == QWEN_ENGINE["model"], (
        "lineage 合同里的模型名与 QWEN_ENGINE 不同源 —— 这正是那个记账错位的形态")
    # 反向:换代前那个值不许再出现在合同里
    assert _PLATFORM_CONTRACT["dashscope"]["model"] != QWEN_ENGINE_PREVIOUS_MODEL


def test_engine_contract_has_no_heavy_imports():
    """🔴 引擎合同模块**只许** stdlib —— 它被事务内惰性 import 的代码引用。

    ``services/monitoring_lineage.py`` 引 ``question_evolution`` → ``db.connection``,
    而 import ``db.connection`` 会触发 init_db 抢 ACCESS EXCLUSIVE
    (2026-08-10 把生产打成 503 十六分钟的自死锁)。包F ① 的账本桥正是
    事务内惰性 import,所以它读的合同必须住在一个碰不到 db 的模块里。

    这条判据是那次拆分的**唯一**理由;没有它,下一个人会顺手在这里
    ``import db.something``,而不会有任何东西变红。
    """
    tree = ast.parse(_read("services/engine_contract.py"))
    offenders = []
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module]
        for m in mods:
            top = m.split(".")[0]
            if top not in ("__future__", "typing"):
                offenders.append(m)
    assert not offenders, (
        f"engine_contract 引了非 stdlib 模块 {offenders} —— "
        "它会被事务内惰性 import,闭包必须干净")


def test_monitoring_lineage_still_re_exports_the_contract():
    """反向对照:搬走之后 ``monitoring_lineage`` 的既有调用方**零变化**。

    没有这一条,"搬家"与"把合同从 monitoring_lineage 里删了"分不开。
    """
    from services import monitoring_lineage as ML
    from services import engine_contract as EC

    assert ML.QWEN_ENGINE is EC.QWEN_ENGINE, "re-export 断了"
    assert ML._PLATFORM_CONTRACT is EC.PLATFORM_CONTRACT, "re-export 断了"
    assert ML.QWEN_ENGINE_SWITCHED_ON == EC.QWEN_ENGINE_SWITCHED_ON


def test_the_other_engines_match_their_current_contract_values():
    """反向对照:一次只动该动的那一格,别的引擎一个字不动。

    没有这一条,「换了某一个引擎」与「把四个引擎全改了」分不开。

    🔴 [WO_221-c1'' 重冻] 原名 `test_deepseek_and_kimi_are_untouched`,
       钉的是**换千问那一笔当时**的 deepseek 值 `deepseek-v4-flash`。
       WO_221 ⑦b 把 `PLATFORM_CONTRACT["deepseek"]` 改成了
       `deepseek_official` + `deepseek-flash` —— 那一格是**写进账本的计划模型**,
       2026-07-27 起该平台已换官方原生检索,合同却一直写着百炼那套,
       于是账本里 actual_provider/actual_model 一直是错的(同类缺陷第 6 处)。

    🔴 所以这条判据**按新值重冻**,不是「为了让它绿而改回合同」——
       方向反了会把一个已修好的缺陷装回去。判据钉住缺陷的形状时,
       它会与正确的修法为敌(本仓 criterion-pinning-the-defect-fights-the-fix)。
       kimi / doubao 两行**保持原值**:它们才是本条的反向对照。
    """
    assert _PLATFORM_CONTRACT["deepseek"]["model"] == "deepseek-flash"
    assert _PLATFORM_CONTRACT["deepseek"]["provider"] == "deepseek_official", (
        "provider 也要一起钉 —— 只钉 model 的话,provider 被改回 dashscope 仍绿,"
        "而账本里那一列同样是错的")
    assert _PLATFORM_CONTRACT["kimi"]["model"] == "kimi-k2.6"
    assert _PLATFORM_CONTRACT["doubao"]["model"] == "doubao-seed-2-0-pro-260215"


def test_qwen_endpoint_is_the_multimodal_one():
    """端点必须是原生多模态,**且不是**旧文本端点。

    成对判:只判"是多模态"的话,一个既含两段路径的畸形串也会过。
    """
    ep = QWEN_ENGINE["endpoint"]
    assert MULTIMODAL_PATH in ep, f"端点不是原生多模态:{ep}"
    assert TEXT_PATH not in ep, (
        f"端点里出现了旧文本路径 —— 3.7-plus 走它必 400「url error」:{ep}")
    assert ep.startswith("https://dashscope.aliyuncs.com/"), ep


def test_search_options_are_exactly_the_probed_set():
    """search_options **逐键逐值** = 探测证明可用的那一组。

    多一个键也红:加一个没探过的键就是拿生产去试,而 400 会让整条
    监测链当天全红。
    """
    actual = dict(QWEN_ENGINE["search_options"])
    assert actual == PROBED_SEARCH_OPTIONS, (
        f"search_options 漂移:\n  多/改 = "
        f"{ {k: v for k, v in actual.items() if PROBED_SEARCH_OPTIONS.get(k) != v} }"
        f"\n  少   = {sorted(set(PROBED_SEARCH_OPTIONS) - set(actual))}")


def test_base_parameters_are_exactly_the_probed_set():
    """[R2 F-2] ``base_parameters`` 逐键逐值 = 探测证明可用的那一组。

    少一个键也红 —— R1 就是"少了一个键"翻的车。
    """
    actual = dict(QWEN_ENGINE["base_parameters"])
    assert actual == PROBED_BASE_PARAMETERS, (
        f"base_parameters 漂移:\n  多/改 = "
        f"{ {k: v for k, v in actual.items() if PROBED_BASE_PARAMETERS.get(k) != v} }"
        f"\n  少   = {sorted(set(PROBED_BASE_PARAMETERS) - set(actual))}")


def test_thinking_is_off_by_identity_not_by_falsiness():
    """[R2 F-2] ``enable_thinking`` 必须是 ``False`` 本尊,不是任何 falsy 值。

    分开写一条,是因为上面那条 dict 相等在 Python 里 ``False == 0``:
    有人把它写成 ``0`` / ``None`` 上面那条**照样绿**,而
    ``None`` 发到线上就是"这个键没给" = thinking 默认开 = 两臂表里
    的臂A。所以这里用 ``is``。
    """
    v = QWEN_ENGINE["base_parameters"]["enable_thinking"]
    assert v is False, f"enable_thinking 是 {v!r}({type(v).__name__}),必须是 False 本尊"
    assert QWEN_ENGINE["base_parameters"]["enable_search"] is True


def test_base_parameters_do_not_shadow_the_caller_budget():
    """``max_tokens`` 是调用方给的,不许被 SSOT 里的同名键盖掉。

    调用点写的是 ``{**base_parameters, "max_tokens": max_tokens, ...}``,
    真塞进去也是调用方赢 —— 但那样 SSOT 里就躺着一个**永远不生效**
    的键,下一个人会照着它调预算然后发现调不动。
    """
    assert "max_tokens" not in QWEN_ENGINE["base_parameters"]
    assert "search_options" not in QWEN_ENGINE["base_parameters"], (
        "search_options 有自己的位置,不许在 base_parameters 里再来一份")


def test_switch_date_is_recorded_and_history_is_not_recomputed():
    """⑤ 断点标注在,且**没有**任何重算历史的动作。

    §3.1 的取舍:换模型当天起新旧数据不可比(先导翻转率 16.7~60%),
    趋势图会出现人为断点。所以我们只**标注**,不重算 ——
    重算等于用新模型改写客户已经看过的历史结论。
    """
    assert QWEN_ENGINE_SWITCHED_ON == "2026-08-24", QWEN_ENGINE_SWITCHED_ON
    assert QWEN_ENGINE_PREVIOUS_MODEL == "qwen3-max", QWEN_ENGINE_PREVIOUS_MODEL
    # 负向锁:本次改动不许出现"回填/重算历史监测"的动作。
    # 结构锚打在 SQL 动词上,不裸匹配"重算"三个字(注释里就有,那是病历)。
    src = _read("services/engine_contract.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            upper = node.value.upper()
            assert not ("UPDATE " in upper and "MONITORING_RESULTS" in upper), (
                "lineage 模块里出现了改写 monitoring_results 的语句 —— "
                "换代不许重算历史")


# ══════════════════════════════════════════════════════════════════════
# 2. 请求体真的从 SSOT 取值(不是手写)
# ══════════════════════════════════════════════════════════════════════

def test_request_body_takes_the_model_from_the_ssot():
    """``query_dashscope_search`` 的请求体里,model **必须**是 ``_QWEN["model"]``。

    🔴 这条锁的是「手写字符串」这个**形态**,不是某个具体值。
       手写是那个记账错位的成因:手写的两处迟早分叉,而分叉的那天
       没有任何判据会红。
    """
    node = _fn(TESTER, "query_dashscope_search")
    found = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Dict):
            continue
        for k, v in zip(n.keys, n.values):
            if isinstance(k, ast.Constant) and k.value == "model":
                found.append(v)
    assert found, "请求体里找不到 model 键 —— 分母塌了"
    for v in found:
        # 允许 _QWEN["model"];不允许字面量
        assert not isinstance(v, ast.Constant), (
            f"model 是手写字面量 {v.value!r}(第 {v.lineno} 行)—— "
            "必须取 services.monitoring_lineage.QWEN_ENGINE,否则迟早与计价分叉")
        assert isinstance(v, ast.Subscript), ast.dump(v)


def test_request_uses_the_ssot_endpoint_and_options():
    """端点与 search_options 也必须取 SSOT,不许手写。"""
    src = _read(TESTER)
    assert '_QWEN["endpoint"]' in src, "端点没取 SSOT"
    assert 'dict(_QWEN["search_options"])' in src, "search_options 没取 SSOT"
    # 负向:文本端点那条 URL 不许再出现在千问那一段里
    node = _fn(TESTER, "query_dashscope_search")
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and TEXT_PATH in n.value:
            pytest.fail(f"千问请求里还留着旧文本端点(第 {n.lineno} 行)")


def test_request_parameters_all_come_from_the_ssot():
    """[R2 F-2] 请求体 ``parameters`` 里,除 ``max_tokens`` 外**不许有手写键**。

    🔴 这条锁的是 **形态**,不是 ``enable_thinking`` 这一个键。
       F-2 的本体是"一个参数漂出 SSOT 而全仓没有判据会红";只钉
       ``enable_thinking`` 的话,下一个被顺手删掉的参数会重演一模一样
       的事故。所以这里要求:``parameters`` 的键集合恰好是
       ``{**铺开, "max_tokens", "search_options"}``,铺开的那一项必须
       铺自 ``_QWEN``。
    """
    node = _fn(TESTER, "query_dashscope_search")
    params_dicts = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Dict):
            continue
        for k, v in zip(n.keys, n.values):
            if isinstance(k, ast.Constant) and k.value == "parameters" \
                    and isinstance(v, ast.Dict):
                params_dicts.append(v)
    assert params_dicts, "请求体里找不到 parameters 字典 —— 分母塌了"

    for d in params_dicts:
        spreads, literal_keys = [], []
        for k, v in zip(d.keys, d.values):
            if k is None:            # ``**something``
                spreads.append(v)
            elif isinstance(k, ast.Constant):
                literal_keys.append(k.value)
            else:
                pytest.fail(f"parameters 里有动态键(第 {d.lineno} 行),看不懂就不许过")

        assert len(spreads) == 1, (
            f"parameters 必须恰好铺开一次 SSOT,实际 {len(spreads)} 次"
            f"(第 {d.lineno} 行)")
        # 铺开的必须是 _QWEN["base_parameters"](允许外面包一层 dict())
        src_of_spread = ast.dump(spreads[0])
        assert "base_parameters" in src_of_spread and "_QWEN" in src_of_spread, (
            f'铺开的不是 _QWEN["base_parameters"]:{src_of_spread[:160]}')

        assert set(literal_keys) == {"max_tokens", "search_options"}, (
            f"parameters 里出现了手写键 {sorted(set(literal_keys) - {'max_tokens', 'search_options'})}"
            f" —— 手写的迟早漂出 SSOT,而漂出去的那天没有判据会红"
            f"(第 {d.lineno} 行)")


@pytest.mark.asyncio
async def test_assembled_request_body_carries_thinking_off(monkeypatch):
    """[R2 F-2] **真正组装出来的请求体**逐值 —— 不是 grep 源码。

    🔴 为什么要这条而不是只留上面那条 AST 锁:上面锁的是"源码长什么
       样",而线上收到的是"组装完的 dict"。二者之间还隔着一层
       ``**`` 铺开的求值 —— 铺开的对象要是空 dict、要是被别的键盖掉、
       要是 SSOT 里根本没有 ``base_parameters`` 这一项,AST 锁**照样绿**
       而线上 thinking 照样默认开。
       (本仓的老教训:判据要打组装后的东西,不要 grep 源码。)

    做法:把 ``_tracked_post`` 换成捕获器,让请求"发"到捕获器手里,
    不出网、不花钱、不碰 key。
    """
    import tools.ai_visibility.ai_tester as tester

    captured: dict = {}
    calls: list[int] = []

    class _FakeResponse:
        """一份**形状真实**的多模态 200 响应(含正文角标与 search_info)。"""

        status_code = 200

        @staticmethod
        def json():
            return {"output": {
                "choices": [{"message": {"content": [
                    {"text": "占位答案[1],详见 https://example.com/a 。"},
                ]}}],
                "search_info": {"search_results": [
                    {"index": 1, "title": "来源一",
                     "url": "https://example.com/a", "site_name": "example"},
                ]},
            }}

    async def _capture(client, url, **kwargs):
        calls.append(1)
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResponse()

    monkeypatch.setattr(tester, "_tracked_post", _capture)
    await tester.query_dashscope_search("测试问题", max_tokens=1234)

    assert captured, "_tracked_post 没被调用 —— 这条判据什么也没验到"

    # ══════════════════════════════════════════════════════════════════
    # 🔴 承重:一次调用**零重试**走完整条 handler。
    # ══════════════════════════════════════════════════════════════════
    # 这一行是本文件里唯一能抓到"响应解析路径上任何一处没吃多模态数组"
    # 的判据。那类缺陷的现场表现是:HTTP 200 → 解析抛 TypeError →
    # 外层 ``except Exception`` 吞掉 → 重试 3 次 → engine_error。
    # 也就是说**换代当天监测全线静默失败,而没有任何一条单元锁会红**
    # ——R1 就是这样带着一处漏折平的 ``_extract_dashscope_native_citations``
    # 通过了 70 条判据 + 22 发变异。
    #
    # 所以这里不判"内容对不对"(那是提取器自己的判据),只判
    # **"没有人在这条路上抛异常"**,用重试次数做探针。
    assert len(calls) == 1, (
        f"一次调用发了 {len(calls)} 次请求 —— 说明响应解析路径上有人抛了异常"
        f"被外层 except 吞成重试。这类缺陷在生产的表现是 200 却判 engine_error,"
        f"整条监测链静默失败。")
    body = captured["kwargs"]["json"]
    params = body["parameters"]

    # 🔴 承重的一条:线上真发出去的那份 parameters 里,thinking 是关的。
    assert params.get("enable_thinking") is False, (
        f"组装出来的 parameters 里 enable_thinking = "
        f"{params.get('enable_thinking')!r} —— 省略/None 就是 thinking 默认开,"
        f"= Review 两臂表里的臂A(贵 2.9 倍、答案更短)")
    assert params.get("enable_search") is True, params
    assert params["max_tokens"] == 1234, "调用方的预算被 SSOT 盖掉了"
    assert params["search_options"] == PROBED_SEARCH_OPTIONS, params["search_options"]
    assert set(params) == {"enable_thinking", "enable_search",
                           "max_tokens", "search_options"}, sorted(params)

    # 顺带把同一份组装结果上的其它保真项一起判了(同一次调用,零额外成本)
    assert body["model"] == OWNER_RULED_MODEL, body["model"]
    assert captured["url"] == QWEN_ENGINE["endpoint"], captured["url"]
    assert body["input"]["messages"][0]["content"] == [{"text": "测试问题"}], (
        "content 不是多模态数组形状 —— 退回字符串在多模态端点会 400")


def test_no_second_unflattened_reader_of_the_native_content():
    """[R2] 谁都不许再裸取 DashScope 原生 ``output`` 里的 ``message.content``。

    🔴 这条是 R1 那个漏网之鱼的**同类防线**,不是它本身的判据
       (它本身的判据是上面那条"零重试")。
       R1 折平了主链那一处、给它配了锁,然后**第二处**
       ``_extract_dashscope_native_citations`` 还在裸取 —— 于是
       "提取器会折平数组"是真的,"线上能跑"是假的。
       同一个缺口常有两层;单点判据只能证明单点。

    作用域说明(免得这条锁自己变成假绿):结构锚打的是"以 ``output``
    开头、读到 ``'content'`` 的调用链"。``output`` 是本文件里 DashScope
    原生响应体的固定命名;OpenAI 兼容那几条链读的是 ``data``,形状是
    字符串,不在本锁分母内 —— 它们各自有自己的端点锁。
    """
    tree = ast.parse(_read(TESTER))
    offenders: dict[int, tuple[str, str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name == "_dashscope_message_text":
            continue                      # 折平器本人,唯一豁免
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            try:
                text = ast.unparse(n)
            except Exception:             # pragma: no cover - 解析不了就不判
                continue
            if text.startswith("output.") and "'content'" in text:
                offenders[n.lineno] = (fn.name, text[:100])

    assert not offenders, (
        "以下位置裸取了原生 content(多模态下它是数组,喂进 re 就 TypeError,"
        "会被外层 except 吞成 engine_error):\n  " + "\n  ".join(
            f"第 {ln} 行 · {fname}() · {snip}"
            for ln, (fname, snip) in sorted(offenders.items())) +
        "\n改法:走 _dashscope_message_text(output)。")


def test_the_flattening_lock_can_actually_see_that_function():
    """上一条的活性自证:豁免名单之外确实存在被扫到的函数。

    没有这条,``_dashscope_message_text`` 哪天改名 / 文件哪天读空,
    上一条会因为**分母是 0** 而恒绿。
    """
    tree = ast.parse(_read(TESTER))
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_dashscope_message_text" in names, "折平器改名了,豁免名单已失效"
    assert "_extract_dashscope_native_citations" in names, (
        "R1 漏网的那个函数不在了 —— 上一条锁的分母可能已经塌了")
    # 扫描确实能看到 output.* 形态的调用(在折平器自己身上)
    fn = _fn(TESTER, "_dashscope_message_text")
    seen = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert any(s.startswith("output.") for s in seen), (
        "连折平器里都扫不到 output.* —— 结构锚的匹配面已经失效")


@pytest.mark.asyncio
async def test_native_citation_extractor_eats_the_multimodal_array():
    """[R2] 第二处消费者的**行为**判据:多模态数组进去,不抛且能取到正文 URL。"""
    import tools.ai_visibility.ai_tester as tester

    output = {
        "choices": [{"message": {"content": [
            {"text": "见 https://example.com/in-text 一文。"},
        ]}}],
        "search_info": {"search_results": [
            {"index": 1, "title": "来源", "url": "https://example.com/a"},
        ]},
    }
    citations = tester._extract_dashscope_native_citations(output)
    urls = {c["url"] for c in citations}
    assert "https://example.com/a" in urls, citations
    assert "https://example.com/in-text" in urls, (
        f"正文里的 URL 没被提出来 —— content 数组没折平就会走到这一步之前"
        f"抛 TypeError:{citations}")


def test_multimodal_content_is_an_array():
    """``input.messages[].content`` 必须是**数组**形状。

    多模态端点的入参约定;退回字符串会 400。
    """
    node = _fn(TESTER, "query_dashscope_search")
    src = ast.unparse(node)
    assert "{'role': 'user', 'content': [{'text': query}]}" in src.replace('"', "'"), (
        "千问请求体的 content 不是 [{'text': query}] 数组形状 —— 多模态端点会 400")


def test_deepseek_still_uses_the_text_endpoint():
    """反向对照:DeepSeek **仍走**文本端点。

    没有这一条,"只换千问"与"把 DashScope 两条链都换了"分不开。
    """
    node = _fn(TESTER, "query_dashscope_deepseek")
    src = ast.unparse(node)
    assert TEXT_PATH in src, "DeepSeek 被顺手换到多模态端点了"
    assert MULTIMODAL_PATH not in src, "DeepSeek 被顺手换到多模态端点了"
    assert "deepseek-v4-flash" in src, "DeepSeek 模型名被改了"


# ══════════════════════════════════════════════════════════════════════
# 3. 提取器 —— 换端点必须同改提取器
# ══════════════════════════════════════════════════════════════════════

def test_extractor_flattens_the_multimodal_array():
    """🔴 「换路径必须同改提取器」——本仓记过这一条。

    多模态端点的 ``message.content`` 是 ``[{"text": ...}]``。
    不折平的话 ``.strip()`` 当场 AttributeError,被外层 except 吞成
    "重试三次后 engine_error" ⇒ **换代的表现是监测全线静默失败**,
    而不是一个显眼的报错。
    """
    from tools.ai_visibility.ai_tester import _dashscope_message_text

    # 多模态形状
    mm = {"choices": [{"message": {"content": [{"text": "甲"}, {"text": "乙"}]}}]}
    assert _dashscope_message_text(mm) == "甲乙"
    # 文本形状(DeepSeek 那条链仍在用)—— 行为必须逐字节不变
    txt = {"choices": [{"message": {"content": "丙"}}]}
    assert _dashscope_message_text(txt) == "丙"
    # 非文本片段(图/视频)跳过,不许把 dict 的 repr 混进正文
    mixed = {"choices": [{"message": {"content": [
        {"image": "http://x"}, {"text": "丁"}]}}]}
    assert _dashscope_message_text(mixed) == "丁"
    # 空/畸形一律空串(调用方另有 engine_error fail-closed 处理,资金相关)
    assert _dashscope_message_text({}) == ""
    assert _dashscope_message_text({"choices": []}) == ""
    assert _dashscope_message_text({"choices": [None]}) == ""


def test_both_dashscope_chains_use_one_extractor():
    """两条 DashScope 链共用同一个折平函数。

    「同一谓词写两处 ⇒ 必有一处没人验」—— 本仓记过。
    """
    src = _read(TESTER)
    assert src.count("_dashscope_message_text(output)") >= 2, (
        "只有一条链在用折平函数 —— 另一条哪天换端点会静默失败")


# ══════════════════════════════════════════════════════════════════════
# 4. 记账与实调同源(那个存量错位的闭合证明)
# ══════════════════════════════════════════════════════════════════════

def test_cost_accounting_model_equals_the_called_model():
    """计价用的模型名 == 实际发出去的模型名。

    🔴 这是那个存量错位的**闭合判据**。换代前:计价 3.7-plus / 实调 3-max。
       现在两边都从 ``QWEN_ENGINE`` 取 —— 结构上不能再分叉。
       变异把任一边改成手写,这里立刻红。
    """
    node = _fn("db/monitoring_db.py", "_estimate_token_cost_placeholder")
    src = ast.unparse(node)
    assert "_QWEN_MODEL" in src, (
        "计价表里的千问模型名不是从 SSOT 取的 —— 记账错位会复发")
    # 负向:不许再出现任何写死的 qwen 模型字面量
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and n.value.startswith("qwen"):
            pytest.fail(f"计价表里又出现手写 qwen 模型名 {n.value!r}(第 {n.lineno} 行)")


def test_pricing_row_exists_for_the_called_model():
    """价目表里必须有**这个**模型的精确行 —— 否则成本按兜底算。"""
    from tools.llm_call_tracker import PRICING_TABLE

    key = ("dashscope", QWEN_ENGINE["model"])
    assert key in PRICING_TABLE, (
        f"价目表缺 {key} 的精确行 —— 单次成本会按兜底价算,"
        f"而 ¥0.29/4引擎 的审计价就失去依据。现有 dashscope 行:"
        f"{sorted(k[1] for k in PRICING_TABLE if k[0] == 'dashscope')}")
    row = PRICING_TABLE[key]
    assert row["input"] > 0 and row["output"] > 0, row


def test_lineage_labels_agree_with_the_called_model():
    """其余几处 lineage 标注也必须指向同一个模型。

    换代前这几处标 3.7-plus 而实调 3-max —— 现在它们**恰好**变成对的了,
    本条把这个一致性钉住,免得下次换代时它们又落单。
    """
    hooks = _read("services/geo_observation/source_hooks.py")
    assert QWEN_ENGINE["model"] in hooks, (
        f"source_hooks 里的千问模型名与实调不一致 —— 应为 {QWEN_ENGINE['model']}")
    assert QWEN_ENGINE_PREVIOUS_MODEL not in hooks.split("qwen_dashscope_search")[0], (
        "source_hooks 里还留着换代前的模型名")
