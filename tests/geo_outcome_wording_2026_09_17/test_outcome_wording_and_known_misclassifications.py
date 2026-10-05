# -*- coding: utf-8 -*-
"""WO_233-c2 · 推荐判定:对客先改称谓,六个已知误判固化成验收基线。

字段:`target_outcome`(`services/geo_observation/contracts.TargetOutcome`,十态)。
对客文案单点:`services/defensive_geo/presentation/copy_registry.TARGET_OUTCOME_LABELS`。

🔴 本单**不重写语义判定**。`classify_outcome` 在确认提及之后只看
   「回答里任意位置有没有正向词」——不绑定推荐对象、不识别否定。
   重写它要标注集 + 影子计算,是包二。本单做两件:
     ① 对客不再把这两档叫「推荐」,改叫「提及」;
     ② 把六个已知误判**钉成 xfail(strict)**,给包二当验收基线。

🔴 为什么用 `xfail(strict=True)` 而不是注释掉或跳过:
   strict 的意思是「**修好了要报错**」。包二把语义判定做对之后,这几条会从
   xfail 变成 XPASS ⇒ 整包红 ⇒ 有人被迫回来把 xfail 摘掉并写进交付单。
   注释掉或 skip 的话,修好了没有任何人会知道
   (本仓:白名单/豁免不许比问题活得久)。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.defensive_geo.presentation.copy_registry import (      # noqa: E402
    TARGET_OUTCOME_LABELS,
)
from services.geo_observation.contracts import (                      # noqa: E402
    EntityState, ResponseStatus, TargetOutcome,
)
from services.geo_observation.entity_review import classify_outcome   # noqa: E402

BRAND = "甲公司"


def _outcome(answer: str) -> TargetOutcome:
    return classify_outcome(
        ResponseStatus.answered, EntityState.confirmed_mention, True, answer)


# ── ① 对客称谓:两档不再自称「推荐」 ────────────────────────────────

@pytest.mark.parametrize("value", ["recommended", "conditionally_recommended"])
def test_the_customer_facing_label_no_longer_claims_a_recommendation(value):
    """🔴 这两档的对客文案里不许出现「推荐」。

    判定会把「目前无法推荐甲公司」读成 recommended(见下方 xfail 那几条),
    拿它对客户说「AI 明确推荐了你」是正面承诺一个没发生的事。
    """
    label = TARGET_OUTCOME_LABELS[value]
    assert "推荐" not in label, "%s 的对客文案仍自称推荐:%r" % (value, label)
    assert "提及" in label or "提到" in label, label


def test_every_outcome_value_has_a_customer_facing_label():
    """分母:十态一个不漏都有对客文案 —— 漏一个前端就会显示原始英文枚举。"""
    missing = [v.value for v in TargetOutcome if v.value not in TARGET_OUTCOME_LABELS]
    assert not missing, missing
    extra = [k for k in TARGET_OUTCOME_LABELS if k not in {v.value for v in TargetOutcome}]
    assert not extra, "文案表里有枚举里没有的键(名单比问题活得久):%s" % extra


def test_the_two_reworded_labels_are_still_distinguishable():
    """反向对照:改称谓之后两档**仍然互不相同**。

    偷懒把两个都写成「提及」会让这一条红 —— 那等于把「有条件」这层信息丢了,
    对客户是另一种不准确。
    """
    assert (TARGET_OUTCOME_LABELS["recommended"]
            != TARGET_OUTCOME_LABELS["conditionally_recommended"])


def test_the_negative_outcomes_are_untouched():
    """本单只动那两档。其余八态文案逐字不动 —— 顺手改会扩大对客变更面。"""
    assert TARGET_OUTCOME_LABELS["not_mentioned"] == "没提到"
    assert TARGET_OUTCOME_LABELS["mentioned_only"] == "只是提到"
    assert TARGET_OUTCOME_LABELS["criteria_only"] == "只讲了怎么选没点名"
    assert TARGET_OUTCOME_LABELS["entity_ambiguous"] == "没认准是哪一家"


# ── ② 六个已知误判:现在是期望红,包二修好后会 XPASS ──────────────────

#: (回答原文, 当前判定, 应为, 说明)
KNOWN_MISCLASSIFICATIONS = [
    ("甲公司资料不足,无法评价。推荐乙公司,服务更适合你的需求。",
     TargetOutcome.mentioned_only, "推荐的是乙公司,不是甲公司 —— 判定不绑定推荐对象"),
    ("甲公司是一家专业从事软件开发的企业。",
     TargetOutcome.mentioned_only, "这是描述,不是推荐"),
    ("目前无法推荐甲公司,资料太少。",
     TargetOutcome.mentioned_only, "🔴 明确否定,却被判成 recommended"),
    ("推荐甲公司,因为它提供你需要的本地实施服务。",
     TargetOutcome.recommended, "「本地」是理由不是条件,不该降成有条件推荐"),
]


@pytest.mark.parametrize("answer,expected,why", KNOWN_MISCLASSIFICATIONS,
                         ids=[str(i) for i in range(len(KNOWN_MISCLASSIFICATIONS))])
@pytest.mark.xfail(strict=True, reason="WO_233-c2:本单不修语义判定,包二修。修好后本条会 XPASS ⇒ 整包红,提醒摘 xfail")
def test_the_outcome_matches_what_the_answer_actually_says(answer, expected, why):
    assert _outcome(answer) is expected, why


#: 正样本臂 + 可接受臂:这两条**现在就该绿**。
#: 🔴 没有它们,上面四条 xfail 可能只是因为判定器整个坏掉/永远返回同一个值 ——
#:    那样"期望红"就不是在描述一个具体缺陷,而是在描述一台死掉的仪器。
WORKING_TODAY = [
    ("我推荐甲公司,它在这个领域做得很好。", TargetOutcome.recommended, "正样本"),
    ("甲公司不推荐,服务态度差。", TargetOutcome.mentioned_only, "可接受(工单原话)"),
]


@pytest.mark.parametrize("answer,expected,why", WORKING_TODAY)
def test_the_classifier_still_gets_these_right_today(answer, expected, why):
    """🔴 正样本臂:判定器**不是**全坏,它在这两条上是对的。"""
    assert _outcome(answer) is expected, (
        "%s:期望 %s,实测 %s" % (why, expected, _outcome(answer)))


def test_the_six_cases_are_all_here():
    """六例齐 —— 少一条就少一格验收基线,而少了没人会发现。"""
    assert len(KNOWN_MISCLASSIFICATIONS) + len(WORKING_TODAY) == 6
