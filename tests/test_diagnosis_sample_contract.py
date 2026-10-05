"""[C组·V10/§12.1]最小有效样本合同判别 —— 版本化,不再用散落 >0.5 裸常量。

覆盖 Master §12.1 四条:
- 零可用 / payload 损坏 → H0 insufficient(不交付、可计费=0 → 全额释放);
- 部分 provider 失败但有有效样本 → degraded 可交付(**已有结果不整批抹掉**),
  失败平台排除分母、覆盖率可见、**未履约不计费**(按覆盖率部分计费);
- 全履约 → sufficient 全额计费;
- 老结构(无 total_planned)不误杀。
"""
import pytest

from services.diagnosis_sample_contract import (
    MIN_SUCCESSFUL_TESTS,
    OUTCOME_DEGRADED,
    OUTCOME_INSUFFICIENT,
    OUTCOME_SUFFICIENT,
    SAMPLE_CONTRACT_VERSION,
    billable_points,
    evaluate_sample,
)


def _av(planned=0, tests=0, failed=None, error=None, platforms=None, with_summary=True):
    """计数口径同真实产出方(ai_tester):tests = **已成功**数;failed 默认 = planned - tests。"""
    if failed is None:
        failed = max(0, planned - tests)
    av = {}
    if error is not None:
        av["error"] = error
    if with_summary:
        av["summary"] = {"total_planned": planned, "total_tests": tests, "total_failed": failed}
    if platforms is not None:
        av["platforms"] = platforms
    return av


def test_collection_error_without_summary_is_h0_and_bills_zero():
    v = evaluate_sample(_av(error="timeout", with_summary=False))
    assert v.outcome == OUTCOME_INSUFFICIENT and v.deliverable is False
    assert v.reason_code == "collection_error_no_summary"
    assert billable_points(v, 260) == 0          # 全额释放,不收一分
    assert v.version == SAMPLE_CONTRACT_VERSION  # 判定带版本(可追溯用哪版规则)


def test_zero_usable_result_is_h0_even_if_tests_ran():
    # 4 引擎都跑了但全失败 → 零可用 → H0
    v = evaluate_sample(_av(planned=8, tests=0))   # 8 次尝试全失败 → 成功数 0
    assert v.outcome == OUTCOME_INSUFFICIENT
    assert v.reason_code == "zero_usable_result"
    assert billable_points(v, 260) == 0


def test_majority_failure_now_degrades_instead_of_hard_blocking():
    # 旧行为:failed/planned>0.5 直接 H0 拦死。新合同:只要还有有效样本就降级交付。
    v = evaluate_sample(_av(planned=8, tests=2))   # 8 计划只成功 2 次(75% 失败),仍有有效样本
    assert v.outcome == OUTCOME_DEGRADED and v.deliverable is True
    assert v.succeeded == 2
    assert 0.24 < v.coverage_ratio < 0.26                    # 2/8
    # 未履约不计费:只按覆盖率收
    assert billable_points(v, 800) == 200


def test_partial_failure_message_shows_coverage_and_no_charge_for_unfulfilled():
    v = evaluate_sample(_av(planned=4, tests=3))
    assert v.outcome == OUTCOME_DEGRADED
    assert "未完成部分不计费" in v.message
    assert "3" in v.message and "4" in v.message              # 覆盖可见
    assert billable_points(v, 400) == 300


def test_full_coverage_is_sufficient_and_bills_full():
    v = evaluate_sample(_av(planned=4, tests=4))
    assert v.outcome == OUTCOME_SUFFICIENT
    assert v.coverage_ratio == 1.0
    assert billable_points(v, 260) == 260


def test_legacy_shape_without_planned_is_not_killed():
    v = evaluate_sample(_av(planned=0, tests=0, failed=0))
    assert v.outcome == OUTCOME_SUFFICIENT and v.reason_code == "legacy_shape_no_planned"
    assert billable_points(v, 130) == 130


def test_failed_platforms_are_excluded_from_denominator_and_listed(cur=None):
    v = evaluate_sample(_av(
        planned=4, tests=2,
        platforms={
            "qwen": {"tests": 1},
            "deepseek": {"tests": 1},
            "kimi": {"error": "timeout"},
            "doubao": {"error": "http 500"},
        },
    ))
    assert v.outcome == OUTCOME_DEGRADED
    assert set(v.delivered_platforms) == {"qwen", "deepseek"}
    assert set(v.failed_platforms) == {"kimi", "doubao"}      # 失败平台被点名,排除在交付分母外


def test_degraded_always_charges_at_least_one_point_but_never_more_than_frozen():
    v = evaluate_sample(_av(planned=100, tests=1))   # 100 计划只成功 1 次 → 1% 覆盖
    assert v.outcome == OUTCOME_DEGRADED
    assert billable_points(v, 50) == 1        # 确有交付 → 至少 1 点,不白送
    assert billable_points(v, 0) == 0         # 没冻结就没有可计费
    assert billable_points(v, 10) <= 10       # 绝不超过冻结额(资金守恒上界)


def test_min_successful_tests_is_a_versioned_constant_not_a_scattered_ratio():
    # 合同把"够不够"表达为可审计的常量 + 版本号,而不是散落的 >0.5
    assert MIN_SUCCESSFUL_TESTS >= 1
    assert SAMPLE_CONTRACT_VERSION.startswith("diagnosis-min-sample-")
    v = evaluate_sample(_av(planned=8, tests=1))
    assert v.succeeded == MIN_SUCCESSFUL_TESTS and v.outcome == OUTCOME_DEGRADED
