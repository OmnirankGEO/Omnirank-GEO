# -*- coding: utf-8 -*-
"""WO_239-乙 · 「要人复核」的判定必须同时交出**可点的候选名**。

甲修的是「没有候选名时也能确认未提到」;乙修的是**上游为什么不给候选名**。
两件不同的事:甲救已落库的那批(候选早写死成空),乙防复发。

**分母**(AST 枚举 `return BrandDecision(...)`,不按 grep —— 注释里提到它的行一堆):
全集 15 处,其中 verdict=UNKNOWN 且零候选名的 **4 处**:

| 行 | reason | 生产诊断数 | 真客户+分享链接 | 处置 |
|---|---|---|---|---|
| 1733 | `registry_name_correction_requires_review` | 61 | 48 | **修** |
| 1620 | `local_evidence_requires_review` | 14 | 12 | **修** |
| 1540 | `identity_load_failed` | **0** | — | 显式排除 |
| 1551 | `identity_decision_conflict` | **0** | — | 显式排除 |

排除理由(写下来,不悄悄跳过):
· `identity_load_failed` —— identity 根本没加载成功,**我们不知道品牌是谁**,
  给不出候选也不该给。它真正的问题是「系统错误被塞进了待人复核态」,另立。
· `identity_decision_conflict` —— 名字在(trusted ∩ rejected),但**修法不是给候选**:
  那是别名配置自相矛盾,让客户点一个名字解决不了。另立。
· 两者生产**各 0 条**,排除零风险;同时也意味着谁都无法从生产验证它们 ——
  所以只作为设计观察记下,不动码。

🔴 `invalid_matched_text`(:1666)**不在待修之列**:它早就带 `near_miss_alias`
   (WO 2026-08-06 §1),还配了防幻觉的 span 守卫。代码里那段「561 实证零候选」
   的注释描述的是**被修之前**的状态 —— 把它读成现状会多修一处已经好了的地方。
"""
from __future__ import annotations

import ast
import io
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

RESOLVER = REPO / "services" / "brand_identity_resolver.py"

#: 本单修的两处 reason(卡片必须有可点候选)
MUST_CARRY_NAME = (
    "registry_name_correction_requires_review",
    "local_evidence_requires_review",
)
#: 显式排除的两处(生产 0 条;给候选解决不了它们的问题)
DELIBERATELY_WITHOUT_NAME = (
    "identity_load_failed",
    "identity_decision_conflict",
)


def _brand_decision_returns():
    """AST 枚举全部 `return BrandDecision(...)`,返回 {reason: kwargs 名集合}。"""
    tree = ast.parse(io.open(RESOLVER, encoding="utf-8").read(), str(RESOLVER))
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        fname = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        if fname != "BrandDecision" or len(call.args) < 2:
            continue
        if not isinstance(call.args[1], ast.Constant):
            continue            # reason 是表达式(透传上游)的返回点,不在本单范围
        verdict = ast.unparse(call.args[0])
        kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        out[call.args[1].value] = {"verdict": verdict, "kwargs": kwargs,
                                   "line": node.lineno}
    return out


@pytest.mark.parametrize("reason", MUST_CARRY_NAME)
def test_a_review_state_hands_out_a_clickable_name(reason):
    """🔴 主判据:这两个「要人复核」的判定必须交出候选名。"""
    returns = _brand_decision_returns()
    assert reason in returns, "找不到 %s 的返回点 —— 判据前提变了,要重写" % reason
    entry = returns[reason]
    assert "UNKNOWN" in entry["verdict"], entry
    assert entry["kwargs"].get("near_miss_alias"), (
        "%s(行 %d)仍不交候选名 ⇒ 待确认卡片零候选可点" % (reason, entry["line"]))


@pytest.mark.parametrize("reason", MUST_CARRY_NAME)
def test_the_name_comes_from_the_evidence_window_not_from_a_model(reason):
    """候选名取自**证据窗口**(答案里真实出现的写法),不是模型另给的串。

    `window_spans` 是从答案切出来的,所以不需要 :1671 那种防幻觉 span 守卫;
    但也正因如此,**取值来源必须是它** —— 换成别处的串就要重新论证幻觉风险。
    """
    entry = _brand_decision_returns()[reason]
    assert "window_spans" in entry["kwargs"]["near_miss_alias"], entry["kwargs"]


@pytest.mark.parametrize("reason", MUST_CARRY_NAME)
def test_it_is_near_miss_alias_and_never_matched_alias(reason):
    """🔴🔴 语义臂 —— 本包最重要的一格。

    `matched_alias` 意思是「本品牌就是以这个写法被提到的」,**YES 时下游据此算
    位置/推荐档**;混用会让待确认格**按位置自动升格成「明确推荐」**,
    那正是 08-05 城市别名 P0 的放大链路(本文件 :85-88 逐字写着不可混用)。
    这里 verdict 是 UNKNOWN(等人确认),只能用 `near_miss_alias`。

    没有这一格,把 `near_miss_alias` 改成 `matched_alias` 会让上面两格照样绿 ——
    而那一改就是一个 P0。
    """
    entry = _brand_decision_returns()[reason]
    assert "matched_alias" not in entry["kwargs"], (
        "%s 用了 matched_alias —— 待确认格会被下游按位置升格成「明确推荐」,"
        "这是 08-05 P0 的放大链路" % reason)


@pytest.mark.parametrize("reason", DELIBERATELY_WITHOUT_NAME)
def test_the_deliberately_excluded_ones_stay_without_a_name(reason):
    """🔴 反臂:显式排除的那两处**仍然**零候选,不许被顺手塞个名字。

    没有这一条,"给所有 UNKNOWN 都塞上候选"也会让主判据全绿 ——
    而 `identity_load_failed` 时我们**根本不知道品牌是谁**,塞进去的会是个假线索;
    `identity_decision_conflict` 给候选也解决不了别名配置自相矛盾。
    两者生产各 0 条,排除零风险,但排除必须是**有判据的排除**,不是忘了。
    """
    entry = _brand_decision_returns().get(reason)
    assert entry is not None, "%s 的返回点不见了 —— 排除清单比问题活得久" % reason
    assert "UNKNOWN" in entry["verdict"], entry
    assert not entry["kwargs"].get("near_miss_alias"), (
        "%s 被顺手塞了候选名 —— 它是%s,给候选解决不了" % (
            reason,
            "「系统错误被塞进复核态」" if reason == "identity_load_failed"
            else "「别名配置自相矛盾」"))
    assert not entry["kwargs"].get("matched_alias"), entry["kwargs"]


def test_the_already_fixed_path_is_not_touched():
    """`invalid_matched_text` 早就修好了(WO 2026-08-06 §1),本单不许动它。

    🔴 它的 span 守卫 `if source_span is not None` 必须还在 ——
    那是防「复核层编了一个原文没有的名字」,与本单两处的情况不同
    (本单取的是证据窗口,天然来自答案)。
    把它顺手"统一"掉会重新引入幻觉候选。
    """
    entry = _brand_decision_returns()["invalid_matched_text"]
    expr = entry["kwargs"].get("near_miss_alias") or ""
    assert "source_span" in expr, (
        "invalid_matched_text 的防幻觉 span 守卫没了:%r" % expr)


def test_the_enumeration_still_covers_every_review_state():
    """🔴 分母腿:UNKNOWN 且 reason 是字面量的返回点,**全部**在上面两张名单里。

    新增一个「要人复核」的判定而没人考虑候选名时,这一条会红 ——
    本单的教训正是"只点实例不扫类"(工单只点了 registry,枚举翻出 local_evidence)。
    """
    returns = _brand_decision_returns()
    review_states = {
        reason for reason, e in returns.items() if "UNKNOWN" in e["verdict"]
    }
    known = set(MUST_CARRY_NAME) | set(DELIBERATELY_WITHOUT_NAME) | {
        "invalid_matched_text"}
    unlisted = sorted(review_states - known)
    assert not unlisted, (
        "出现了名单外的「要人复核」判定,必须逐个决定给不给候选名:%s" % unlisted)
    stale = sorted(known - review_states)
    assert not stale, "名单里这些 reason 已不存在(名单比问题活得久):%s" % stale
