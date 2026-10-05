"""MET-43 判据 · 可比性 3×3 逐态闭合 + Z-2.3 无死路。"""

from __future__ import annotations

import itertools

import pytest

from services.defensive_geo.presentation import comparability as CP

ALL_SAME = {k: True for k in CP.DIMENSION_KEYS}
CMP_OK = {"comparable": True, "comparisonKey": "k1"}
B, C = "2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z"


def proj(**over):
    kw = dict(baseline_snapshot_ref="snapB", current_snapshot_ref="snapC",
              baseline_as_of=B, current_as_of=C, dimensions=dict(ALL_SAME),
              matched_cells=9, baseline_only_cells=0, current_only_cells=0,
              metric_comparisons=[CMP_OK])
    kw.update(over)
    return CP.project(**kw)


# ═══════════════════════════ registry 形态

def test_seven_dimensions_map_bijectively_to_reasons():
    assert len(CP.DIMENSION_KEYS) == 7
    assert set(CP.DIMENSION_TO_REASON) == set(CP.DIMENSION_KEYS)
    assert len(set(CP.DIMENSION_TO_REASON.values())) == 7


def test_precedence_is_finite_total_and_covers_every_reason():
    assert set(CP.REASON_PRECEDENCE) == set(CP.REASON_CODES)
    values = list(CP.REASON_PRECEDENCE.values())
    assert len(set(values)) == len(values)      # 互不相同 ⇒ 主 reason 唯一


def test_partial_forbidden_reasons_are_exactly_the_spec_exclusion():
    assert CP.REASONS_FORBIDDEN_IN_PARTIAL == {
        "no_baseline", "entity_identity_changed", "mode_changed"}


# ═══════════════════════════ level = full

def test_full_requires_all_seven_same_and_zero_side_cells():
    p = proj()
    assert p.level == "full" and p.reason is None
    assert p.baseline_only_cells == 0 and p.current_only_cells == 0


def test_full_with_zero_delta_is_still_ready():
    """MET-43 逐字「full 含 zero delta 固定 ready」。"""
    p = proj(metric_comparisons=[{**CMP_OK, "serverComputedDelta": {"value": 0}}])
    assert CP.section_state_for(p) == "ready"


def test_empty_full_is_rejected():
    with pytest.raises(CP.ComparabilityError):
        proj(metric_comparisons=[])


def test_full_rejects_non_comparable_entries():
    with pytest.raises(CP.ComparabilityError):
        proj(metric_comparisons=[{"comparable": False}])


# ═══════════════════════════ level = none

def test_no_baseline_shape_is_exact():
    p = CP.project(baseline_snapshot_ref=None, current_snapshot_ref="snapC",
                   baseline_as_of=None, current_as_of=C, dimensions=None,
                   matched_cells=0, baseline_only_cells=0, current_only_cells=4)
    assert p.level == "none" and p.reason.code == "no_baseline"
    assert p.dimensions is None and p.baseline_as_of is None
    assert p.metric_comparisons == ()


def test_no_baseline_rejects_dimensions_or_comparisons():
    with pytest.raises(CP.ComparabilityError):
        CP.project(baseline_snapshot_ref=None, current_snapshot_ref="c",
                   baseline_as_of=None, current_as_of=C, dimensions=dict(ALL_SAME),
                   matched_cells=0, baseline_only_cells=0, current_only_cells=0)


@pytest.mark.parametrize("fatal", sorted(CP.FATAL_DIMENSIONS))
def test_fatal_dimension_forces_none_not_partial(fatal):
    """🔴 换主体/换模式两次测的不是同一件事,并列展示都算误导。"""
    dims = dict(ALL_SAME); dims[fatal] = False
    p = proj(dimensions=dims, matched_cells=0, metric_comparisons=[])
    assert p.level == "none"
    assert p.reason.code in ("entity_identity_changed", "mode_changed")


@pytest.mark.parametrize("fatal", sorted(CP.FATAL_DIMENSIONS))
def test_fatal_dimension_cannot_carry_comparisons(fatal):
    dims = dict(ALL_SAME); dims[fatal] = False
    with pytest.raises(CP.ComparabilityError):
        proj(dimensions=dims, matched_cells=3, metric_comparisons=[CMP_OK])


def test_none_with_comparison_is_rejected():
    dims = dict(ALL_SAME); dims["samePlatformSet"] = False
    with pytest.raises(CP.ComparabilityError):
        proj(dimensions=dims, matched_cells=0, metric_comparisons=[CMP_OK])


# ═══════════════════════════ level = partial

def test_partial_requires_real_mismatch():
    dims = dict(ALL_SAME); dims["samePlatformSet"] = False
    p = proj(dimensions=dims, matched_cells=5, baseline_only_cells=2)
    assert p.level == "partial" and p.reason.code == "platform_set_changed"


def test_partial_from_overlap_only():
    p = proj(baseline_only_cells=3, current_only_cells=1)
    assert p.level == "partial" and p.reason.code == "partial_overlap"


def test_partial_without_mismatch_is_rejected():
    """七维全同 + 零单侧 cell 却报 partial = 无 mismatch,拒。"""
    with pytest.raises(CP.ComparabilityError):
        CP.project(baseline_snapshot_ref="b", current_snapshot_ref="c",
                   baseline_as_of=B, current_as_of=C, dimensions=dict(ALL_SAME),
                   matched_cells=5, baseline_only_cells=0, current_only_cells=0,
                   metric_comparisons=[{"comparable": False}])


def test_empty_partial_is_rejected():
    dims = dict(ALL_SAME); dims["samePlatformSet"] = False
    with pytest.raises(CP.ComparabilityError):
        proj(dimensions=dims, matched_cells=5, metric_comparisons=[])


def test_primary_reason_follows_precedence_when_several_dimensions_fail():
    dims = dict(ALL_SAME)
    dims["sameResolverAndClassifierVersions"] = False   # precedence 70
    dims["samePlatformSet"] = False                     # precedence 40 —— 更优先
    assert CP.primary_reason(dims) == "platform_set_changed"


# ═══════════════════════════ 时间与维度完整性

def test_baseline_must_strictly_precede_current():
    with pytest.raises(CP.ComparabilityError):
        proj(baseline_as_of=C, current_as_of=B)
    with pytest.raises(CP.ComparabilityError):
        proj(baseline_as_of=C, current_as_of=C)


def test_missing_or_unknown_dimension_is_rejected():
    partial_dims = {k: True for k in CP.DIMENSION_KEYS[:-1]}
    with pytest.raises(CP.ComparabilityError):
        proj(dimensions=partial_dims)
    with pytest.raises(CP.ComparabilityError):
        proj(dimensions={**ALL_SAME, "sameWeather": True})


def test_missing_current_root_is_rejected():
    with pytest.raises(CP.ComparabilityError):
        proj(current_snapshot_ref="")


# ═══════════════════════════ Z-2.3:每一档都有出口

def test_every_level_has_a_non_empty_next_action():
    """§0.5.6 Z-2.3:no-baseline 分支不得是 nextAction: null。"""
    no_baseline = CP.project(
        baseline_snapshot_ref=None, current_snapshot_ref="c", baseline_as_of=None,
        current_as_of=C, dimensions=None, matched_cells=0,
        baseline_only_cells=0, current_only_cells=1)
    dims = dict(ALL_SAME); dims["samePlatformSet"] = False
    for p in (proj(), proj(baseline_only_cells=2), no_baseline,
              proj(dimensions=dims, matched_cells=0, metric_comparisons=[])):
        actions = CP.section_actions(p)
        assert actions, p.level
        assert "build_comparable_retest_plan" in actions or p.level == "full"


def test_no_baseline_gets_retest_plan_cta():
    p = CP.project(baseline_snapshot_ref=None, current_snapshot_ref="c",
                   baseline_as_of=None, current_as_of=C, dimensions=None,
                   matched_cells=0, baseline_only_cells=0, current_only_cells=1)
    assert "build_comparable_retest_plan" in CP.section_actions(p)


def test_baseline_not_comparable_must_offer_contact_provider():
    """MET-43 逐字「后者必须 contact-provider」。"""
    dims = dict(ALL_SAME); dims["samePlatformSet"] = False
    p = proj(dimensions=dims, matched_cells=0, metric_comparisons=[])
    assert p.level == "none"
    assert "contact_provider" in CP.section_actions(p)


def test_no_baseline_does_not_offer_contact_provider():
    """反向对照:没基线只是还没测过,不该把用户推给服务商。"""
    p = CP.project(baseline_snapshot_ref=None, current_snapshot_ref="c",
                   baseline_as_of=None, current_as_of=C, dimensions=None,
                   matched_cells=0, baseline_only_cells=0, current_only_cells=1)
    assert "contact_provider" not in CP.section_actions(p)


# ═══════════════════════════ 3×3 aggregate

def test_single_side_states_cover_all_three_levels():
    dims = dict(ALL_SAME); dims["samePlatformSet"] = False
    assert CP.section_state_for(proj()) == "ready"
    assert CP.section_state_for(proj(baseline_only_cells=2)) == "partial_observation"
    assert CP.section_state_for(
        proj(dimensions=dims, matched_cells=0, metric_comparisons=[])) == "no_conclusion"


def test_hybrid_aggregate_takes_the_worse_side():
    """🔴 MET-43「一侧全绿另一侧 all-error 时顶层 partial」。"""
    dims = dict(ALL_SAME); dims["samePlatformSet"] = False
    ready = proj()
    dead = proj(dimensions=dims, matched_cells=0, metric_comparisons=[])
    assert CP.aggregate_hybrid({"defensive": ready, "offensive": dead}) == \
        "partial_observation"
    assert CP.aggregate_hybrid({"defensive": ready, "offensive": ready}) == "ready"
    assert CP.aggregate_hybrid({"defensive": dead, "offensive": dead}) == "no_conclusion"


def test_hybrid_three_by_three_matrix_is_total():
    """3 档 × 3 档 = 9 组合,每组都必须有确定聚合态(逐态闭合)。"""
    dims = dict(ALL_SAME); dims["samePlatformSet"] = False
    catalog = {
        "ready": proj(),
        "partial": proj(baseline_only_cells=2),
        "none": proj(dimensions=dims, matched_cells=0, metric_comparisons=[]),
    }
    seen = set()
    for a, b in itertools.product(catalog, repeat=2):
        state = CP.aggregate_hybrid({"defensive": catalog[a], "offensive": catalog[b]})
        assert state in ("ready", "partial_observation", "no_conclusion")
        seen.add((a, b))
    assert len(seen) == 9


def test_empty_hybrid_is_rejected():
    with pytest.raises(CP.ComparabilityError):
        CP.aggregate_hybrid({})


def test_census_is_mechanical():
    c = CP.registry_census()
    assert c["dimensions"] == 7 and c["reason_codes"] == 10
    assert c["precedence_entries"] == 10


def test_fatal_dimension_can_never_reach_partial():
    """🔴 承重不变式(穷举 2^7 维度组合 × 4 种 cell 形态)。

    这条是 MUT-14 存活后补的。原来的两条"致命维度"判据都被**第二把锁**兜住了
    (matched_cells=0 独立导向 none;带 comparison 时 partial 的 forbidden-reason
    检查独立抛错),所以摘掉 fatal 分支它们照样绿 —— 两把锁叠在同一条路径上,
    变异存活是唯一信号(本仓 2026-08-21 记过这个形态)。

    这条改为直接钉**不变式本身**:任一致命维度失配时,``level`` 永不为 partial。
    它对"谁在承重"不敏感,所以无论 fatal 分支在不在,不变式被破就会转红。
    """
    for combo in itertools.product([True, False], repeat=len(CP.DIMENSION_KEYS)):
        dims = dict(zip(CP.DIMENSION_KEYS, combo))
        if all(dims[k] for k in CP.FATAL_DIMENSIONS):
            continue
        for mc, bo, co, cmps in ((5, 2, 1, [CMP_OK]), (5, 0, 0, [CMP_OK]),
                                 (0, 1, 1, []), (3, 0, 0, [{"comparable": False}])):
            try:
                p = CP.project(
                    baseline_snapshot_ref="b", current_snapshot_ref="c",
                    baseline_as_of=B, current_as_of=C, dimensions=dims,
                    matched_cells=mc, baseline_only_cells=bo,
                    current_only_cells=co, metric_comparisons=cmps)
            except CP.ComparabilityError:
                continue                      # 拒绝也是合法结局
            assert p.level != "partial", (combo, mc, bo, co, p.reason)
