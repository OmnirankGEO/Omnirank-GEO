"""v2.10 文章方向配比器 · G27 测试套(阶段 1:helpers + 算法 + slot bucket)

阶段 1 覆盖 G27 子集:
  - G27-22 fixed legacy 4 标记不双扣(老板五审 1)
  - G27-23 existing > required configurable 不为负(老板五审 2)
  - G27-x  slot bucket 互斥不变量(Codex 四审 P0-1)
  - G27-x  regenerating 锁定(Codex 四审 P0-2)
  - G27-x  largest_remainder sum == total(Codex 二审 P1-7)
  - G27-x  style_code → user_choice 转换(Codex 二审 P1-6)
  - G27-x  user_choice_distribution 校验(Codex 二审 P0-2)
  - G27-x  ranking 不在 USER_CHOICE_OPTIONS(老板拍 A 不暴露)

阶段 2-6 测试在后续 commit 加入(endpoints / KTG / 前端 / 旧 endpoint 等)
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from writing.direction_distribution import (
    LOCKED_STATUSES,
    USER_CHOICE_OPTIONS,
    VALID_SOURCES,
    STYLE_CODE_TO_USER_CHOICE,
    is_fixed_company_topic,
    is_user_choice_option,
    is_source_valid,
    style_code_to_user_choice,
    largest_remainder,
    compute_slot_buckets,
    compute_recommended_user_choice_distribution,
    validate_user_choice_distribution,
    DistributionValidationError,
)


# ============================================================
# G27 常量 SSOT 守护
# ============================================================

def test_v210_locked_statuses_includes_regenerating():
    """G27 · LOCKED_STATUSES 必须含 regenerating(Codex 四审 P0-2 防竞态)"""
    assert "regenerating" in LOCKED_STATUSES
    assert "completed" in LOCKED_STATUSES
    assert "writing" in LOCKED_STATUSES


def test_v210_user_choice_options_six_families_no_ranking():
    """G27 · USER_CHOICE_OPTIONS 严守 auto + 六个 outward family。"""
    expected = {
        "auto", "evidence_qa", "multi_brand_comparison", "implementation_guide",
        "trend_policy_risk", "case_data_roi", "company_facts",
    }
    assert USER_CHOICE_OPTIONS == expected
    assert "ranking" not in USER_CHOICE_OPTIONS
    assert "company" not in USER_CHOICE_OPTIONS


def test_v210_valid_sources_3_items():
    """G27 · user_choice_source 取值域(老板 9 条 + Codex P0-2)"""
    assert VALID_SOURCES == {"manual", "batch_uniform", "batch_distribution"}


# ============================================================
# G27-22 · fixed legacy 4 标记不双扣(老板五审 1)
# ============================================================

def test_v210_is_fixed_company_topic_4_legacy_markers():
    """G27-22 · is_fixed_company_topic 兼容 4 种 fixed slot 标记 · 任一命中即 true"""
    # 1. is_fixed boolean
    assert is_fixed_company_topic({"is_fixed": True})
    # 2. style_code='company_profile'(英文 v2.7.1+)
    assert is_fixed_company_topic({"style_code": "company_profile"})
    # 3. article_style='公司深度报道'(中文)
    assert is_fixed_company_topic({"article_style": "公司深度报道"})
    # 4. 多标记并存
    assert is_fixed_company_topic({
        "is_fixed": True,
        "style_code": "company_profile",
        "article_style": "公司深度报道",
    })

    # 非 fixed
    assert not is_fixed_company_topic({"style_code": "price_roi"})
    assert not is_fixed_company_topic({"article_style": "价格解读"})
    assert not is_fixed_company_topic({})
    assert not is_fixed_company_topic({"is_fixed": False})


def test_v210_fixed_completed_no_double_count():
    """G27-22 · fixed slot 已 completed · 只算 fixed_count + 不进 active_locked_count(防双扣)"""
    confirmed_kws = [{"required_articles": 14}]
    existing_topics = [
        # fixed slot 已 completed · 不能既算 fixed 又算 active
        {"id": 1, "status": "completed", "style_code": "company_profile", "article_style": "公司深度报道"},
        # 普通 topic completed
        {"id": 2, "status": "completed", "style_code": "price_roi"},
        # 普通 topic writing
        {"id": 3, "status": "writing", "style_code": "data_report"},
    ]
    result = compute_slot_buckets(1, confirmed_kws, existing_topics)

    assert result["fixed_count"] == 1, "fixed completed 必须算 fixed · 不算 active"
    assert result["active_locked_count"] == 2, "其他 completed/writing 算 active_locked"
    # 不变量
    assert (result["fixed_count"] + result["active_locked_count"]
            + result["manual_locked_count"] + result["configurable_count"]) == result["total"]


def test_v210_manual_completed_no_double_count():
    """G27-22 · manual locked 且 completed → 只算 active_locked(优先级:active > manual)"""
    confirmed_kws = [{"required_articles": 10}]
    existing_topics = [
        # manual + completed · 优先级 active > manual · 算 active_locked
        {"id": 1, "status": "completed", "user_choice_source": "manual", "style_code": "price_roi"},
        # manual + pending · 算 manual_locked
        {"id": 2, "status": "pending", "user_choice_source": "manual", "style_code": "comparison_review"},
    ]
    result = compute_slot_buckets(1, confirmed_kws, existing_topics)

    assert result["active_locked_count"] == 1, "manual+completed 算 active"
    assert result["manual_locked_count"] == 1, "manual+pending 算 manual"
    # 不变量
    assert (result["fixed_count"] + result["active_locked_count"]
            + result["manual_locked_count"] + result["configurable_count"]) == result["total"]


# ============================================================
# G27-23 · existing > required · configurable 不为负(老板五审 2)
# ============================================================

def test_v210_existing_count_exceeds_required_configurable_not_negative():
    """G27-23 · existing topics > required_articles · total 取 max · configurable >= 0"""
    confirmed_kws = [{"required_articles": 10}]  # required_sum=10
    # existing 14 个(orphan / 重生成 / 加词补题场景)
    existing_topics = [
        {"id": i, "status": "pending", "style_code": "price_roi"}
        for i in range(1, 15)
    ]
    result = compute_slot_buckets(1, confirmed_kws, existing_topics)

    assert result["total"] == 14, "total 取 max(required, existing) = 14"
    assert result["drift_warning"] is True, "existing > required 触发 drift_warning"
    assert result["configurable_count"] >= 0, "configurable 不能负"

    # 不变量
    assert (result["fixed_count"] + result["active_locked_count"]
            + result["manual_locked_count"] + result["configurable_count"]) == result["total"]


def test_v210_normal_required_equals_existing_no_drift():
    """G27 · 正常场景 existing == required · 0 drift"""
    confirmed_kws = [{"required_articles": 14}]
    existing_topics = [
        {"id": i, "status": "pending", "style_code": "price_roi"}
        for i in range(1, 14)  # 13 个 existing(未满 14)
    ]
    result = compute_slot_buckets(1, confirmed_kws, existing_topics)

    assert result["total"] == 14, "total = required(existing < required)"
    assert result["drift_warning"] is False


def test_v210_slot_bucket_invariant_always_holds():
    """G27 · 不变量 fixed + active + manual + configurable == total(任何场景)"""
    test_cases = [
        # 空 quote
        ([{"required_articles": 14}], []),
        # 只有 fixed
        ([{"required_articles": 5}], [{"id": 1, "is_fixed": True, "status": "completed"}]),
        # 混合
        ([{"required_articles": 10}], [
            {"id": 1, "style_code": "company_profile", "status": "completed"},
            {"id": 2, "user_choice_source": "manual", "status": "pending"},
            {"id": 3, "status": "regenerating"},  # active(LOCKED_STATUSES 含 regenerating)
            {"id": 4, "status": "pending"},
        ]),
    ]
    for confirmed_kws, existing_topics in test_cases:
        result = compute_slot_buckets(1, confirmed_kws, existing_topics)
        total_check = (result["fixed_count"] + result["active_locked_count"]
                       + result["manual_locked_count"] + result["configurable_count"])
        assert total_check == result["total"], \
            f"不变量破:{total_check} != {result['total']} 测试用例={existing_topics}"


def test_v210_regenerating_status_locked_from_config():
    """G27 · regenerating status 必须进 active_locked(防竞态 Codex 四审 P0-2)"""
    confirmed_kws = [{"required_articles": 10}]
    existing_topics = [
        {"id": 1, "status": "regenerating", "style_code": "price_roi"},
    ]
    result = compute_slot_buckets(1, confirmed_kws, existing_topics)
    assert result["active_locked_count"] == 1, "regenerating 必须算 active_locked"


# ============================================================
# G27 · largest_remainder 算法守护(Codex 二审 P0-7)
# ============================================================

def test_v210_largest_remainder_sum_equals_total():
    """G27 · largest_remainder sum 必须 == total"""
    test_cases = [
        ({"a": 0.3, "b": 0.25, "c": 0.2, "d": 0.15, "e": 0.1}, 14),
        ({"a": 0.5, "b": 0.5}, 11),
        ({"a": 1.0}, 5),
        ({"a": 0.33, "b": 0.33, "c": 0.34}, 10),
    ]
    for weights, total in test_cases:
        result = largest_remainder(weights, total)
        assert sum(result.values()) == total, \
            f"largest_remainder 破 · sum({result})!={total} weights={weights}"


def test_v210_largest_remainder_zero_total():
    """G27 · total=0 → 全 0(不补)"""
    result = largest_remainder({"a": 0.5, "b": 0.5}, 0)
    assert all(v == 0 for v in result.values())


def test_v210_largest_remainder_zero_weights():
    """G27 · weights 全 0 → 全 0(医疗禁 ranking 后剩 0 时)"""
    result = largest_remainder({"a": 0, "b": 0}, 5)
    assert all(v == 0 for v in result.values())


def test_v210_largest_remainder_deterministic_tie_break():
    """G27 · 同小数部分按 key 字典序 tie-break · 保证确定性"""
    weights = {"b": 0.5, "a": 0.5}
    result1 = largest_remainder(weights, 1)
    result2 = largest_remainder(weights, 1)
    assert result1 == result2, "tie-break 必须确定性"


# ============================================================
# G27 · style_code → user_choice 转换层(Codex 二审 P1-6)
# ============================================================

def test_v210_style_to_user_choice_mapping():
    """G27 · maintained style codes collapse into exactly six families."""
    assert style_code_to_user_choice("price_roi") == "case_data_roi"
    assert style_code_to_user_choice("comparison_review") == "multi_brand_comparison"
    assert style_code_to_user_choice("recommendation_review") == "multi_brand_comparison"
    assert style_code_to_user_choice("risk_compliance") == "trend_policy_risk"
    assert style_code_to_user_choice("data_report") == "case_data_roi"
    assert style_code_to_user_choice("buying_guide") == "implementation_guide"
    assert style_code_to_user_choice("qa_recommendation") == "evidence_qa"
    assert style_code_to_user_choice("brand_softarticle") == "company_facts"


def test_v210_disabled_styles_and_fixed_company_excluded():
    """G27 · 只有**真退役**与**固定槽位**的 style_code 才没有家族归属。

    [命名 SSOT 2026-07-28 · P0-1] 原断言把 ranking_v2 / authority_ranking 也锁成
    None。WP12(D12) 复活它们并给了默认配比之后，这个 None 就成了缺陷本身：
    映射返 None 会让该文体的配比在选题/标题层静默蒸发（实测六类合计掉到 80%），
    表现就是"排名文放回来了却没有影子"。按 SSOT §1.4 改测试对齐已签发语义。
    """
    # 复活的榜单文体归入"选购与多品牌比较"家族（对用户仍只暴露 6 类）
    assert style_code_to_user_choice("ranking_v2") == "multi_brand_comparison"
    assert style_code_to_user_choice("authority_ranking") == "multi_brand_comparison"
    # 反向锁：真退役与固定槽位仍必须是 None
    assert style_code_to_user_choice("trojan_horse") is None
    assert style_code_to_user_choice("company_profile") is None


# ============================================================
# G27 · validate_user_choice_distribution(Codex 二审 P0-2)
# ============================================================

def test_v210_validate_distribution_reject_style_code():
    """G27 · distribution keys 必须是 user_choice · 不接 style_code 等工程词"""
    with pytest.raises(DistributionValidationError) as exc:
        validate_user_choice_distribution(
            {"price_roi": 4, "comparison_review": 3, "data_report": 3},  # style_code 名
            configurable_count=10,
        )
    assert "style_code" in str(exc.value) or "工程词" in str(exc.value) or "user_choice" in str(exc.value)


def test_v210_validate_distribution_reject_ranking():
    """G27 · ranking 不在 USER_CHOICE_OPTIONS · 必拒"""
    with pytest.raises(DistributionValidationError):
        validate_user_choice_distribution(
            {"case_data_roi": 4, "ranking": 3, "evidence_qa": 3},
            configurable_count=10,
        )


def test_v210_validate_distribution_reject_company():
    """G27 · company 不在 USER_CHOICE_OPTIONS(v2.4 P0)· 必拒"""
    with pytest.raises(DistributionValidationError):
        validate_user_choice_distribution(
            {"case_data_roi": 5, "company": 5},
            configurable_count=10,
        )


def test_v210_validate_distribution_reject_auto():
    """G27 · auto 不参与 distribution(auto 表示系统推荐 mode 本身)"""
    with pytest.raises(DistributionValidationError):
        validate_user_choice_distribution(
            {"case_data_roi": 5, "auto": 5},
            configurable_count=10,
        )


def test_v210_validate_distribution_sum_mismatch():
    """G27 · sum != configurable_count → 400"""
    with pytest.raises(DistributionValidationError) as exc:
        validate_user_choice_distribution(
            {"case_data_roi": 5, "multi_brand_comparison": 3},  # sum=8
            configurable_count=10,
        )
    assert "sum" in str(exc.value) or "合计" in str(exc.value)


def test_v210_validate_distribution_sum_equal_passes():
    """G27 · sum == configurable_count → PASS(无异常)"""
    validate_user_choice_distribution(
        {"case_data_roi": 4, "multi_brand_comparison": 3, "evidence_qa": 3},
        configurable_count=10,
    )


def test_v210_validate_distribution_negative_count_reject():
    """G27 · count 必须非负整数"""
    with pytest.raises(DistributionValidationError):
        validate_user_choice_distribution(
            {"case_data_roi": -1, "multi_brand_comparison": 11},
            configurable_count=10,
        )


def test_v210_validate_distribution_not_dict_reject():
    """G27 · distribution 非 dict 类型 → 400"""
    with pytest.raises(DistributionValidationError):
        validate_user_choice_distribution([1, 2, 3], configurable_count=6)  # type: ignore


# ============================================================
# G27 · 推荐配比 sum == configurable_count
# ============================================================

def test_v210_recommended_distribution_sum_equals_configurable():
    """G27 · compute_recommended_user_choice_distribution sum == configurable_count"""
    # 各 configurable_count 都测
    for n in (5, 10, 14, 20, 50):
        result = compute_recommended_user_choice_distribution(None, n)
        assert sum(result.values()) == n, \
            f"recommended sum != configurable · n={n} result={result}"


def test_v210_recommended_distribution_no_ranking_in_keys():
    """G27 · 推荐配比返 user_choice 维度 · 0 ranking/style_code key"""
    result = compute_recommended_user_choice_distribution(None, 14)
    for key in result.keys():
        assert key in USER_CHOICE_OPTIONS, f"key '{key}' 不在 USER_CHOICE_OPTIONS · 应是 user_choice"
        assert key != "auto", "auto 不进 distribution"
        # 反例 · 不应含 ranking/style_code
        assert key not in ("ranking", "ranking_v2", "authority_ranking", "company", "company_profile",
                            "price_roi", "comparison_review", "data_report")


def test_v210_recommended_distribution_zero_configurable():
    """G27 · configurable_count=0 → 全 0"""
    result = compute_recommended_user_choice_distribution(None, 0)
    assert all(v == 0 for v in result.values())


# ============================================================
# G27 · is_user_choice_option / is_source_valid 助手
# ============================================================

def test_v210_is_user_choice_option_helper():
    """G27 · is_user_choice_option NULL/合法/非法"""
    assert is_user_choice_option(None)  # NULL 合法
    assert is_user_choice_option("auto")
    assert is_user_choice_option("case_data_roi")
    assert is_user_choice_option("price")  # historical alias remains readable
    assert not is_user_choice_option("ranking")
    assert not is_user_choice_option("company")
    assert not is_user_choice_option("price_roi")  # style_code 不算
    assert not is_user_choice_option("invalid_key")


def test_v210_is_source_valid_helper():
    """G27 · is_source_valid NULL/合法/非法"""
    assert is_source_valid(None)
    assert is_source_valid("manual")
    assert is_source_valid("batch_uniform")
    assert is_source_valid("batch_distribution")
    assert not is_source_valid("invalid_source")
    assert not is_source_valid("")
