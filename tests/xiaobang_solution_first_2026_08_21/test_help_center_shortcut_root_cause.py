"""包 B② · 截图那三幕的**真因**判据。

## 真因是什么(我在底 3e149add6 上实测出来的,工单没写)

工单 §1 把「连续甩帮助中心」归因于三条:current_page 关闸、提示词甩锅、
低置信兜底。前两条**属实**,但都不是主因 —— 因为在它们之前还有一层短路:

    api/xiaobang_api.py::event_stream 的 `deterministic_direct` 分支

它由 `services.gap_operation_map.match_operation()` 决定,而 `match` 是
**裸子串**匹配(`gap_operation_map.py:398` `candidate in text`,取最长命中)。
`help_center` 当时挂着一条同义词 **`"怎么用"`**。

于是(实测 `match_operation` 返回值):

    「员工席位这个该怎么用」            → help_center   ← 截图第 2 幕
    「你除了引导我去帮助中心还能做什么」 → help_center   ← 截图第 3 幕

**用户明说「除了帮助中心」,系统还是把他送回帮助中心。**
这条短路先于 preset / canned / RAG / LLM 全部返回,所以只改提示词或只改
current_page 闸,截图里的行为一个字都不会变。

## 两处修

1. 摘掉 `"怎么用"` 这条同义词 —— 它表达的是「<某功能>怎么用」,
   不是「带我去帮助中心」;
2. 排除语气守卫 —— 命中短语前 12 字内出现「除了/不要/以外…」时放弃这条短路。

两处都配了反向对照:**真的在找帮助中心的问法必须照样命中**。
"""

from __future__ import annotations

import pytest

from services.gap_operation_map import match_operation
from services.xiaobang_question_intent import (
    is_contrastive_exclusion,
    should_suppress_operation_shortcut,
)


# ══════════════════════════════════════════════════════════════════════
# ① 同义词面:「怎么用」不再等于「去帮助中心」
# ══════════════════════════════════════════════════════════════════════

HOW_TO_QUESTIONS = [
    "员工席位这个该怎么用",          # 截图第 2 幕原话
    "员工席位怎么用",
    "这个页面怎么用",
    "监测怎么用",
    "发布投放怎么用",
    "算力怎么用",
]


@pytest.mark.parametrize("q", HOW_TO_QUESTIONS)
def test_how_to_questions_no_longer_route_to_the_help_center(q):
    """🔴 必须不命中:含「怎么用」的问题不该被判成「要去帮助中心」。

    把 `"怎么用"` 加回 `help_center` 的 synonyms,本条必红。
    """
    hit = match_operation(q)
    assert getattr(hit, "operation_id", None) != "help_center", (
        "%r 被路由到帮助中心 —— 这正是截图第 2 幕" % q
    )


REAL_HELP_CENTER_QUESTIONS = [
    "帮助中心在哪",
    "帮助文档怎么找",
    "使用说明在哪里",
    "找帮助",
]


@pytest.mark.parametrize("q", REAL_HELP_CENTER_QUESTIONS)
def test_genuine_help_center_questions_still_route_there(q):
    """反向对照:真的在找帮助中心的问法**必须**照样命中。

    只验「不再乱指帮助中心」挡不住「帮助中心整个不可达了」——
    那是把一个 bug 换成另一个。
    """
    hit = match_operation(q)
    assert getattr(hit, "operation_id", None) == "help_center", q


# ══════════════════════════════════════════════════════════════════════
# ② 排除语气面:「除了 X 还能…」不许被路由到 X
# ══════════════════════════════════════════════════════════════════════

def test_contrastive_exclusion_detects_the_screenshot_sentence():
    assert is_contrastive_exclusion("你除了引导我去帮助中心还能做什么", "帮助中心")


def test_contrastive_exclusion_does_not_fire_without_an_exclusion_marker():
    """反向对照:没有排除语气时不许乱判 —— 否则「帮助中心在哪」也会被压掉。"""
    assert not is_contrastive_exclusion("帮助中心在哪", "帮助中心")
    assert not is_contrastive_exclusion("帮我打开帮助中心", "帮助中心")


def test_contrastive_exclusion_respects_the_window():
    """排除语气离得太远就不算 —— 否则一句话里任何位置的「除了」都会污染全句。"""
    far = "除了" + "啊" * 40 + "帮助中心在哪"
    assert not is_contrastive_exclusion(far, "帮助中心")


SCREENSHOT_ACT_THREE = [
    "你除了引导我去帮助中心还能做什么",
    "你除了帮助中心还能做什么",
    "别再让我去帮助中心了,还能做什么",
]


@pytest.mark.parametrize("q", SCREENSHOT_ACT_THREE)
def test_screenshot_act_three_no_longer_short_circuits_to_help_center(q):
    """🔴 必须命中抑制:这三种问法都不许再走「确定性操作直答」。

    拆掉排除语气守卫(或拆掉自足问句判断),本条必红。
    """
    hit = match_operation(q)
    if hit is None:
        pytest.fail("分母自证失败:%r 现在压根匹配不到任何 operation,"
                    "这条判据就没在验抑制逻辑了" % q)
    assert should_suppress_operation_shortcut(
        q, (hit.display_name, *hit.synonyms)
    ), "%r 命中 %s 却没被抑制" % (q, hit.operation_id)


def test_a_genuine_navigation_request_is_not_suppressed():
    """反向对照:正常导航请求**不许**被抑制,否则确定性直答整体失效。"""
    hit = match_operation("帮助中心在哪")
    assert hit is not None
    assert not should_suppress_operation_shortcut(
        "帮助中心在哪", (hit.display_name, *hit.synonyms)
    )


# ══════════════════════════════════════════════════════════════════════
# ③ 同义词过宽审计(补验 §8 弱点 1)
#
# 交付单 §8 曾如实记「match_operation 其余同义词我没有逐条审」。
# 这一节把它补上,并固化成常驻判据。
#
# 判据不是「这个词看起来太泛」(那是人肉判断,会漂),而是**机械的**:
# 拿一批与任何具体 operation 都无关的通用问法去撞注册表,撞上了就要能解释。
# ══════════════════════════════════════════════════════════════════════

def _all_synonyms():
    from services.gap_operation_map import get_operation_registry
    return [(e.operation_id, s)
            for e in get_operation_registry()._entries
            for s in e.synonyms]


#: 与任何具体 operation 都无关的通用问法。它们**本不该**被路由走。
GENERIC_PROBES = [
    "员工席位怎么用", "监测怎么用", "算力怎么用", "这个功能怎么用",
    "报价怎么用", "文章怎么用", "我该怎么开始", "第一步做什么",
    "为什么失败了", "报错了怎么办", "这个多少钱", "怎么收费",
    "在哪里看", "怎么设置", "怎么修改", "怎么删除", "怎么导出",
    "GEO 图文无法使用", "点了没反应", "页面打不开", "数据不对",
]

#: 允许命中的例外 —— **用户自己点名了模块**,路由过去是对的。
#: 每加一条都必须在这里写清理由;不写理由就等于给锁开白名单。
JUSTIFIED_HITS = {
    # 用户点名了「发布投放」这个模块名本身
    "发布投放怎么用": "publish_center",
    # 退款确实住在钱包里,路由过去正确
    "能退款吗": "wallet",
}


def test_the_synonym_denominator_is_real():
    """🔴 分母自证:213 条量级的同义词表必须真取到,否则下面是「跟空比」。"""
    syns = _all_synonyms()
    assert len(syns) > 150, "同义词只取到 %d 条,取数坏了" % len(syns)


@pytest.mark.parametrize("q", GENERIC_PROBES)
def test_generic_questions_are_not_swallowed_by_an_over_broad_synonym(q):
    """通用问法**不许**被任何同义词吃掉。

    `"怎么用"` 是被这条抓出来的那一个(它当时挂在 help_center 上,
    于是「员工席位怎么用」→ 帮助中心 = 截图第 2 幕)。
    这条判据守的是:**不许再出现第二个这样的同义词**。
    """
    hit = match_operation(q)
    oid = getattr(hit, "operation_id", None)
    assert oid is None, (
        "%r 被同义词吃掉,路由到了 %s —— 若这是对的,请在 JUSTIFIED_HITS 里"
        "写明理由;若不是,请收窄那条同义词" % (q, oid)
    )


@pytest.mark.parametrize("q,expected", sorted(JUSTIFIED_HITS.items()))
def test_the_justified_hits_really_do_hit_what_we_said(q, expected):
    """反向对照:例外清单里的那两条**必须真的**还命中它声称的那个 operation。

    白名单最常见的烂法是:被豁免的东西后来行为变了,而白名单还挂着 ——
    于是白名单在替一个**已经不存在**的情况开口子。
    """
    hit = match_operation(q)
    assert getattr(hit, "operation_id", None) == expected, (q, hit)


def test_no_synonym_is_a_bare_generic_how_to_phrase():
    """结构锚:同义词里不许再出现「怎么用」这一类**纯疑问式**短语。

    它们不指向任何功能,只表达「我想知道怎么做」——挂在任何 operation 上都是错的。
    """
    banned = ("怎么用", "怎么做", "怎么办", "如何使用", "怎么弄", "怎么搞")
    offenders = [(oid, s) for oid, s in _all_synonyms() if s.strip() in banned]
    assert offenders == [], offenders
