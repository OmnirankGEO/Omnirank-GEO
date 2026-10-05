"""W1 返工 · 顺手项:深档判了但交付不到下限时,必须有**明确的降档记录**。

实证:历史 4 篇深文里 2 篇未达自身下限(12,599 / 14,810,下限 15,000)。

缺的从来不是"检测" —— `length_below_deep_floor` 早就会报,重写也真的会跑一次。
缺的是**重写之后仍不达标时的显式结论**:合同上写着深档 target 16000、实际交付
12,599 字,而数据层没有任何一个字段说"这篇实际上是按紧凑档交的"。
交付/QA/报价谁想知道"这单到底按哪一档交的",都得自己去比对 findings 数组。

刻意**不**做二次重写:实证已经指出深档长度是被**证据供给**解锁的
(`verified_candidates_support_ranking_deep`),证据不够时再写一遍还是写不长,
只是多烧一次钱。所以这一项是"如实记录",不是"想办法凑到线上"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from writing.article_length_contract import (  # noqa: E402
    DEEP_TIER_MIN_CHARS,
    assess_length_compliance,
    deep_output_floor,
)

_DEEP_PLAN = {"target_chars": 16000, "minimum_chars": 15000, "maximum_chars": 20000}
_COMPACT_PLAN = {"target_chars": 3500, "minimum_chars": 2500, "maximum_chars": 4500}


def _record(chars: int, plan: dict) -> dict:
    return assess_length_compliance(
        "字" * chars, style_code="ranking_v2", plan=plan,
    )["tier_downgrade"]


def test_deep_plan_short_delivery_is_recorded_as_downgraded():
    """历史那两篇的真实数字。"""
    rec = _record(12599, _DEEP_PLAN)
    assert rec["downgraded"] is True
    assert rec["planned_tier"] == "deep"
    assert rec["delivered_tier"] == "compact"
    assert rec["deep_floor"] == deep_output_floor(16000)
    assert rec["actual_chars"] == 12599


def test_deep_plan_met_is_not_recorded_as_downgraded():
    """反向对照:达标的深文不许被记成降档,否则这个字段就是恒真的废话。"""
    rec = _record(DEEP_TIER_MIN_CHARS + 2000, _DEEP_PLAN)
    assert rec["downgraded"] is False
    assert rec["delivered_tier"] == "deep"


def test_compact_plan_is_never_downgraded():
    """紧凑档没有深档下限可言,不该被这条记录波及。"""
    rec = _record(3000, _COMPACT_PLAN)
    assert rec["downgraded"] is False
    assert rec["deep_floor"] is None
    assert rec["planned_tier"] == "compact"


def test_floor_is_the_contract_floor_not_a_second_number():
    """下限必须是 `deep_output_floor` 那一份,不是这里另写的一个数。"""
    for target in (14000, 16000, 18000, 20000):
        rec = _record(1000, {"target_chars": target, "minimum_chars": target})
        assert rec["deep_floor"] == deep_output_floor(target)


@pytest.mark.parametrize("chars, expected", [
    (1500, "below_compact"),
    (3000, "compact"),
    (16000, "deep"),
])
def test_delivered_tier_is_derived_from_actual_length(chars, expected):
    """交付档位按**实际字数**归桶,不猜、不沿用 plan 里那个值。"""
    assert _record(chars, _DEEP_PLAN)["delivered_tier"] == expected


def test_record_rides_along_into_quality_warning():
    """接线锁:这个字段必须跟着 `length_compliance` 整块落 `quality_warning`。

    `assess_length_compliance` 的返回值被整块塞进
    `article['quality_warning']['length_compliance']`,所以只要它在返回字典里,
    就一定落库 —— 这条锁钉的是"它确实在返回字典的顶层",而不是藏在 findings 里。
    """
    full = assess_length_compliance("字" * 12599, style_code="ranking_v2", plan=_DEEP_PLAN)
    assert "tier_downgrade" in full, "降档记录没进返回字典顶层 = 落不了库"
    src = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    assert '"length_compliance": length_v1' in src
    assert '"length_compliance": length_v2' in src
