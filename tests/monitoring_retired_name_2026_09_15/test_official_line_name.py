# -*- coding: utf-8 -*-
"""WO_221-c1 · 监测线不再发出官方已退役的 `deepseek-v4-flash`。

事实(206-d2 实打 + 官方快照):旧名仍返 200,但**回显 `deepseek-flash`**。
对监测来说**模型身份就是被测量本身** —— 把 flash 的回答记成 v4-flash,
整列 DeepSeek 数据的引擎名就是错的,而没有任何东西会报错。

🔴 本包的范围与工单**不同**,差异有据(见 §证据):
   工单点了 3 处,其中 `lineage.py:191` 是 metaso 代理(不是 DeepSeek 官方直连);
   而工单漏了 `lineage.py:175`(active + default_enabled 的那一面)与
   `batch_monitor.py:154`(**写进观测账本的血缘标签本身**)。
   实际官方线 **5 处**,百炼 13 处 + 秘塔 1 处**一个字不动** ——
   百炼上 `deepseek-v4-flash` 是另一家的**活**模型 ID。
"""
from __future__ import annotations

import ast
import io
import pathlib

import pytest

from tests.model_line_flash_census_2026_09_15 import census as C
from tests.model_line_flash_census_2026_09_15 import classified as X

REPO = pathlib.Path(__file__).resolve().parents[2]
RETIRED = "deepseek-v4-flash"

#: 我改动的五处官方线(人读取证过,逐条写明是什么)
OFFICIAL_SITES = {
    "services/ai_surface_monitoring/lineage.py": "两个 native 面的 default_model_key",
    "services/research_monitor/platforms.py": "官方通道的模型兜底默认",
    "tools/ai_visibility/ai_tester.py": "DEEPSEEK_OFFICIAL_MODEL(Anthropic 兼容端点)",
    "tools/monitoring/batch_monitor.py": "血缘契约里写进观测账本的那个标签",
}


@pytest.fixture(scope="module")
def rows():
    return [r for r in C.census(str(REPO)) if r["scope"] == "monitored_engine"]


# ══════════════════════════════════════════════════════════════════
# 1. 分域:官方线归零,百炼线原样
# ══════════════════════════════════════════════════════════════════
def test_no_retired_name_left_on_the_official_line(rows):
    """🔴 官方线上退役名零处。

    归属用 `attribute_by_nearest_credential`(凭据/域名),**不用** census 的
    `gateway` 字段 —— 那个字段自己的文档写着「只是提示」,而它实测把
    `lineage.py:175`(provider_key=deepseek_official / env=DEEPSEEK_API_KEY /
    availability=active)判成了 dashscope,那正是唯一活着的污染源。
    """
    left = []
    for r in rows:
        if r["model"] != RETIRED:
            continue
        who, why, at = C.attribute_by_nearest_credential(r["path"], r["line"])
        if who == "deepseek_official":
            left.append("%s:%d(证据 %s @:%d)" % (r["path"], r["line"], why, at))
    assert not left, ("官方线上仍有退役名:%s" % left)


def test_the_dashscope_side_is_untouched(rows):
    """🔴 反向钉:百炼那 13 处**必须还在**。

    上一条是「零处」。**空集合与「判据根本没在数」在读数上同形** ——
    这一条证明数器是活的:同一个名字在百炼上是另一家的**活**模型 ID,
    DeepSeek 官方改名不改百炼(206 早写过这一刀)。
    少了说明有人顺手改了不该改的。
    """
    n = 0
    for r in rows:
        if r["model"] != RETIRED:
            continue
        who, _, _ = C.attribute_by_nearest_credential(r["path"], r["line"])
        if who == "dashscope":
            n += 1
    assert n == X.FROZEN_DASHSCOPE_SAME_NAME_SITES, (
        "百炼侧同名处数变了(冻结 %d,实测 %d)—— 要么有人改了不该改的,"
        "要么归属法坏了" % (X.FROZEN_DASHSCOPE_SAME_NAME_SITES, n))


@pytest.mark.parametrize("rel", sorted(OFFICIAL_SITES))
def test_the_five_official_sites_emit_the_constant_not_a_literal(rel):
    """五个官方线文件里不许再出现退役名**字面量**。

    用 AST 只看字符串字面量:注释里解释「旧名是什么、为什么退役」是允许的,
    而且那些注释正是下一个人需要的(本仓 a-wrong-comment-outlives-a-wrong-assertion
    的反面:对的注释值得留)。
    """
    tree = ast.parse(io.open(REPO / rel, encoding="utf-8").read())
    docs = {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    bad = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value == RETIRED and id(n) not in docs):
            who, _, _ = C.attribute_by_nearest_credential(str(REPO / rel),
                                                          getattr(n, "lineno", 0))
            if who == "deepseek_official":
                bad.append(getattr(n, "lineno", "?"))
    assert not bad, ("%s(%s)官方线一侧仍有退役名字面量,行 %s"
                     % (rel, OFFICIAL_SITES[rel], bad))


# ══════════════════════════════════════════════════════════════════
# 2. 回显锁单点
# ══════════════════════════════════════════════════════════════════
def test_echo_assert_passes_only_on_exact_match():
    from config.deepseek_models import (
        DEEPSEEK_OFFICIAL_FLASH, OfficialModelEchoMismatch, assert_official_echo)
    assert_official_echo(DEEPSEEK_OFFICIAL_FLASH, DEEPSEEK_OFFICIAL_FLASH)  # 不抛即通过
    with pytest.raises(OfficialModelEchoMismatch) as e1:
        assert_official_echo(DEEPSEEK_OFFICIAL_FLASH, "deepseek-v4-flash")
    assert "deepseek-v4-flash" in str(e1.value) and DEEPSEEK_OFFICIAL_FLASH in str(e1.value), (
        "错误信息没同时写清「回显的」和「请求的」—— 收到告警的人不知道被换成了什么")


def test_a_missing_echo_field_is_not_treated_as_pass():
    """🔴 `model` 字段缺失**不当通过**。

    官方响应带这个字段;没有它说明这根本不是我们以为的那条响应。
    「字段缺失」比「值不同」更可疑,而宽容处理正是「会返 200 的错答案」
    得以长期存活的方式。
    """
    from config.deepseek_models import (
        DEEPSEEK_OFFICIAL_FLASH, OfficialModelEchoMismatch, assert_official_echo)
    for missing in (None, "", "   "):
        with pytest.raises(OfficialModelEchoMismatch):
            assert_official_echo(DEEPSEEK_OFFICIAL_FLASH, missing)


@pytest.mark.parametrize("rel,fn", [
    ("services/research_monitor/platforms.py", "query_deepseek"),
    ("tools/ai_visibility/ai_tester.py", None),
])
def test_both_official_call_sites_actually_call_the_echo_assert(rel, fn):
    """🔴 回显锁**被接上**了,不只是存在。

    「返回值对不对,和它有没有被接上,是两件事」—— 这句在 WO_220 里
    连续两轮都被证明为真(platform 那次、ocr_qa 那次)。
    """
    src = io.open(REPO / rel, encoding="utf-8").read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "assert_official_echo"]
    assert calls, "%s 里没有任何一处调用 assert_official_echo" % rel


# ══════════════════════════════════════════════════════════════════
# 3. 血缘标签:写进观测账本的那一格
# ══════════════════════════════════════════════════════════════════
def test_the_ledger_lineage_label_is_the_live_name():
    """🔴 这一格是**写进观测账本的引擎名**,不是调用参数。

    它错了,整列 DeepSeek 监测数据的引擎名就是错的,而且不会报错。
    工单没点到它 —— 逐处取证时才找出来。
    """
    from tools.monitoring.batch_monitor import _resolve_runtime_lineage
    provider, model, surface, search_mode = _resolve_runtime_lineage("deepseek", "standard")
    assert provider == "deepseek_official"
    assert model == "deepseek-flash", "观测账本里 DeepSeek 那一列还写着 %s" % model
    assert search_mode == "deepseek_native"


def test_the_dashscope_fallback_lineage_keeps_the_same_name():
    """🔴 反向钉:百炼兜底那一格**保持 deepseek-v4-flash**。

    撤换东西时最容易顺手把挨着它的也带走。这一格改了才是错的 ——
    百炼上这个 ID 是另一家的活模型,而且兜底态的血缘必须如实落成 dashscope
    (红线禁止的「静默回落」正是报表写 DeepSeek 而数据来自阿里)。
    """
    from tools.monitoring.batch_monitor import _resolve_runtime_lineage
    provider, model, surface, search_mode = _resolve_runtime_lineage(
        "deepseek", "dashscope_fallback")
    assert provider == "dashscope", "兜底态的血缘不是 dashscope —— 那是静默回落"
    assert model == "deepseek-v4-flash", (
        "百炼兜底那一格被顺手改成了 %s —— 百炼上这个 ID 没退役" % model)


def test_the_active_surface_points_at_the_live_name():
    """`deepseek_native_with_search` 是 availability=active + default_enabled 的那一面,
    真正在跑的就是它 —— 工单漏了它。"""
    from services.ai_surface_monitoring.lineage import SURFACE_SPECS
    spec = SURFACE_SPECS["deepseek_native_with_search"]
    assert spec.availability == "active" and spec.default_enabled is True, (
        "前提变了:这一面不再是活的,本条判据的意义要重估")
    assert spec.default_model_key == "deepseek-flash"
    legacy = SURFACE_SPECS["deepseek_dashscope_search_legacy"]
    assert legacy.default_model_key == "deepseek-v4-flash", (
        "百炼那一面被顺手改了 —— 它的 provider_key 是 dashscope")


# ══════════════════════════════════════════════════════════════════
# 4. 成本身份(Review ③)—— 🔴 这一条是**注毒补出来的**:
#    我按 ③ 改了 db/monitoring_db.py 的代码,却没写锁,
#    毒「成本占位行退回百炼」当场读绿。**改了但没锁**,本单第三次。
# ══════════════════════════════════════════════════════════════════
def test_the_deepseek_cost_row_is_booked_to_the_official_line():
    """DeepSeek 监测平台的成本记在**官方线**名下,模型名是实际在发的那个。

    记错的后果不是报错,是**成本落到百炼名下**:账单总额不变,
    只有按 caller/platform 分层对账时才会得出反过来的结论(WO_214 同族)。

    🔴 两条腿都要:
      · 值必须是**字面量** `deepseek-flash` —— 只跟 PLATFORM_CONTRACT 比的话,
        毒把合同和取值一起改就两边同动、数值没钉住;
      · 又必须**来自** PLATFORM_CONTRACT —— 否则它会变成第二份手写清单,
        而同文件里 qwen 那格的注释写着那正是上次记错行的成因。
    """
    import db.monitoring_db as m
    from services.engine_contract import PLATFORM_CONTRACT
    platform, model = m._deepseek_cost_identity()
    assert platform == "deepseek", "计价 platform 不是价目表的行键:%r" % platform
    assert model == "deepseek-flash", "成本按 %r 计 —— 那不是我们实际在发的模型" % model
    assert model == PLATFORM_CONTRACT["deepseek"]["model"], (
        "成本身份与血缘合同分叉了 —— 它必须从合同取,不是手写第二份")


def test_monitoring_db_does_not_hardcode_a_deepseek_model_name():
    """结构臂:`db/monitoring_db.py` 里不许再出现 deepseek 模型名字面量。

    有字面量就意味着有第二份来源;两份不一致时**没有任何东西会报错** ——
    这个文件自己的注释里就记着上一次:写死 qwen3.7-plus 而实际发 qwen3-max,
    「按一个模型收钱、用另一个模型干活」。
    """
    import pathlib
    src = io.open(REPO / "db" / "monitoring_db.py", encoding="utf-8").read()
    tree = ast.parse(src)
    docs = {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    bad = [(getattr(n, 'lineno', '?'), n.value) for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and n.value.startswith("deepseek-") and id(n) not in docs]
    assert not bad, ("db/monitoring_db.py 又手写了 deepseek 模型名:%s" % bad)


def test_the_cost_row_exists_for_what_we_actually_send():
    """价目表里必须有那一行,否则成本静默落默认值。"""
    from tools.llm_call_tracker import PRICING_TABLE
    import db.monitoring_db as m
    platform, model = m._deepseek_cost_identity()
    assert (platform, model) in PRICING_TABLE, (
        "价目表里没有 (%s, %s) —— 这一路的成本会静默落默认值" % (platform, model))
