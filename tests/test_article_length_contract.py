from pathlib import Path

from writing.article_length_contract import (
    ARTICLE_LENGTH_CONTRACT_VERSION,
    FAMILY_LENGTH_POLICIES,
    build_article_length_plan,
    build_length_guidance_projection,
    build_length_plan_for_topic,
    count_effective_chars,
    length_bucket,
    render_length_instruction,
    validate_length_contract,
)
from writing.article_style_contract import STYLE_FAMILIES
from writing.style_registry import WRITING_STYLES


def _verified_pack(count: int) -> dict:
    return {
        "items": [
            {
                "evidence_id": f"EV-{index}",
                "claim": f"claim {index}",
                "url": f"https://official-{index}.example/report",
                "publisher": f"publisher-{index % 4}",
                "relationship": "support",
                "verification_status": "official_record",
                "official_record_id": f"record-{index}",
            }
            for index in range(count)
        ]
    }


def test_length_contract_covers_exactly_the_six_outward_families():
    from writing.article_length_contract import AVOIDANCE_BAND

    assert validate_length_contract() == []
    assert set(FAMILY_LENGTH_POLICIES) == set(STYLE_FAMILIES)
    # [工单 A 2026-07-27] 采纳率是双峰曲线:没有任何一族的默认目标可以停在低谷。
    lo, hi = AVOIDANCE_BAND
    assert all(
        not (lo < policy.target_chars < hi) for policy in FAMILY_LENGTH_POLICIES.values()
    )


def test_evidence_limited_comparison_gets_shorter_floor_without_padding():
    plan = build_article_length_plan(
        "comparison_review",
        evidence_pack={"items": []},
        verified_candidate_count=2,
    )
    # 证据撑不起深度 -> 收到峰 1(2500/3500/4500),不假装 15k。
    assert plan["minimum_chars"] == 2500
    assert plan["target_chars"] == 3500
    assert plan["maximum_chars"] == 4500
    assert plan["evidence_limited"] is True
    assert plan["causal_citation_claim"] is False
    instruction = render_length_instruction(plan)
    assert "允许低于目标篇幅" in instruction
    assert "不得" in instruction and "凑字数" in instruction


def test_adaptive_targets_sit_on_peak_one_or_peak_two_never_in_between():
    from writing.article_length_contract import (
        COMPACT_RESOLUTION_MAX_CHARS,
        RANKING_FAMILY_MINIMUM_CHARS,
        in_avoidance_band,
    )

    short = build_article_length_plan("qa_recommendation", evidence_pack={"items": []})
    medium = build_article_length_plan("buying_guide", evidence_pack={"items": []})
    long = build_article_length_plan("data_report", evidence_pack={"items": []})
    plus = build_article_length_plan(
        "data_report",
        evidence_pack=_verified_pack(12),
    )
    for plan in (short, medium, long):
        assert plan["target_chars"] <= COMPACT_RESOLUTION_MAX_CHARS
        assert not in_avoidance_band(plan["target_chars"])
        assert length_bucket(plan["target_chars"]) == "3k_6k"
    assert plus["target_chars"] >= RANKING_FAMILY_MINIMUM_CHARS
    assert length_bucket(plus["target_chars"]) == "12k_plus"


def test_multi_source_comparison_lands_on_peak_two():
    plan = build_article_length_plan(
        "comparison_review",
        evidence_pack=_verified_pack(8),
        verified_candidate_count=7,
    )
    assert plan["minimum_chars"] == 15000
    assert 16000 <= plan["target_chars"] <= 18000
    assert plan["maximum_chars"] >= 20000
    assert plan["verified_evidence_count"] == 8
    assert plan["verified_publisher_count"] == 4


def test_only_complex_well_evidenced_families_enter_the_deep_tier():
    from writing.article_length_contract import (
        COMPACT_RESOLUTION_MAX_CHARS,
        RANKING_FAMILY_MINIMUM_CHARS,
    )

    complex_plan = build_article_length_plan(
        "comparison_review",
        evidence_pack=_verified_pack(12),
        verified_candidate_count=9,
    )
    sparse_plan = build_article_length_plan(
        "comparison_review",
        evidence_pack={"items": []},
        verified_candidate_count=9,
    )
    # [工单 C · §2.6-A 语义同步] `buying_guide` 不再是"永远进不了深档"的对照组:
    # 形态路由表规定"已核验候选 <2 但主题证据充足"时正确形态是深度攻略,
    # 门槛逐字对齐 case_data_roi(verified>=10 ∧ publishers>=4)。
    # 所以对照组换成**证据不够**的 guide —— 边界还在,只是挪到了正确的位置。
    ordinary_plan = build_article_length_plan(
        "buying_guide",
        evidence_pack=_verified_pack(6),
    )
    guide_deep_plan = build_article_length_plan(
        "buying_guide",
        evidence_pack=_verified_pack(12),
    )
    assert complex_plan["target_chars"] >= RANKING_FAMILY_MINIMUM_CHARS
    assert complex_plan["maximum_chars"] == 20000
    assert "verified_candidates_support_ranking_deep" in complex_plan["reasons"]
    assert sparse_plan["target_chars"] <= COMPACT_RESOLUTION_MAX_CHARS
    assert sparse_plan["maximum_chars"] <= COMPACT_RESOLUTION_MAX_CHARS
    assert ordinary_plan["maximum_chars"] <= COMPACT_RESOLUTION_MAX_CHARS
    assert "complex_guide_verified_12k_plus" not in ordinary_plan["reasons"]
    # 证据够了才解锁深档 —— 长度档由证据供给解锁,不由形态解锁
    assert guide_deep_plan["target_chars"] >= RANKING_FAMILY_MINIMUM_CHARS
    assert "complex_guide_verified_12k_plus" in guide_deep_plan["reasons"]
    assert complex_plan["hard_ceiling"] is False
    assert "重复结论" in render_length_instruction(complex_plan)
    assert "不是硬性截断或拒绝上限" in render_length_instruction(complex_plan)
    assert "可以自然超出" in render_length_instruction(complex_plan)
    # [工单 T3 2026-07-29] 原断言是 `"max_tokens=16000" in generator_source` ——
    # 一条源码字符串断言。它想守的其实是"深档不能被输出上限卡住"，但字符串出现
    # 与否证明不了这件事（换个写法就绕过，工单 §3 明令不接受这类断言）。
    #
    # 本包把输出上限改成**跟档位走**（紧凑 16000 / 深档 32000，见
    # `deep_aware_max_tokens`），因此该字符串不再出现。断言同步改成行为级：
    # 直接调那个函数，核紧凑档保持旧值、深档更高。
    from writing.article_generator_service import deep_aware_max_tokens

    assert deep_aware_max_tokens({"target_chars": 3500}) == 16000
    assert deep_aware_max_tokens(ordinary_plan) == 16000
    assert deep_aware_max_tokens(complex_plan) > 16000
    assert deep_aware_max_tokens(guide_deep_plan) > 16000


def test_topic_adapter_and_legacy_style_metadata_share_the_same_ssot():
    topic = {
        "_evidence_pack": _verified_pack(3),
        "_competitor_source": "real",
        "_researched_competitors": [{"name": "A"}, {"name": "B"}],
        "publication_profile": "standard",
    }
    plan = build_length_plan_for_topic("comparison_review", topic)
    assert plan["verified_candidate_count"] == 3
    assert WRITING_STYLES["comparison_review"]["word_count"] == {
        "min": 15000,
        "max": 20000,
        "optimal": 16000,
    }
    assert WRITING_STYLES["comparison_review"]["length_contract_version"] == ARTICLE_LENGTH_CONTRACT_VERSION


def test_effective_character_count_and_empirical_buckets_are_deterministic():
    assert count_effective_chars("甲 乙\n丙") == 3
    assert length_bucket(2999) == "lt_3k"
    assert length_bucket(3000) == "3k_6k"
    assert length_bucket(6000) == "6k_9k"
    assert length_bucket(9000) == "9k_12k"
    assert length_bucket(12000) == "12k_plus"


def test_ui_guidance_is_a_server_projection_and_legacy_unknown_stays_absent():
    assert build_length_guidance_projection({}, "正文") is None
    assert build_length_guidance_projection("not-json", "正文") is None

    compact = build_length_guidance_projection(
        {
            "length_plan": {
                "version": ARTICLE_LENGTH_CONTRACT_VERSION,
                "minimum_chars": 3000,
                "target_chars": 4500,
                "maximum_chars": 8000,
                "evidence_limited": True,
            }
        },
        "甲 乙\n丙",
    )
    assert compact["summary"] == "资料较少，优先写紧凑可信版本"
    assert compact["actual_chars"] == 3
    assert compact["depth"] == "compact"

    deep = build_length_guidance_projection(
        {
            "length_plan": {
                "version": ARTICLE_LENGTH_CONTRACT_VERSION,
                "minimum_chars": 15000,
                "target_chars": 16000,
                "maximum_chars": 20000,
                "evidence_limited": False,
            }
        },
        "深度正文",
    )
    assert deep["summary"] == "证据和结构较充分，可展开为深度版本"
    assert deep["depth"] == "deep"


def test_generation_lineage_freezes_length_plan_and_requested_effective_contact():
    from writing.article_lineage import build_article_lineage

    lineage = build_article_lineage(
        topic={
            "id": 10,
            "title": "隔离器如何选型？",
            "style_code": "buying_guide",
            "_requested_add_images": True,
            "_requested_add_contact": False,
            "_effective_add_images": True,
            "_effective_add_contact": False,
            "_evidence_pack": {"items": []},
        },
        article={
            "title": "隔离器如何选型？",
            "content": "先核对工艺边界，再核对公开标准与验收步骤。" * 100,
            "style": "buying_guide",
        },
        quote_id=20,
        industry="制药装备",
        client_brand="测试品牌",
    )
    snapshot = lineage["generation_request_snapshot"]
    assert snapshot["requested_add_contact"] is False
    assert snapshot["effective_add_contact"] is False
    assert snapshot["length_contract_version"] == ARTICLE_LENGTH_CONTRACT_VERSION
    assert snapshot["length_plan"]["causal_citation_claim"] is False
    assert snapshot["actual_effective_chars"] > 0
    assert lineage["article_review"]["length_diagnostic"]["quality_or_citation_score"] is False


def test_flywheel_buckets_legacy_without_pretending_new_generation_contract():
    from services.writing_style_feature_extractor import extract_writing_style_features

    legacy = extract_writing_style_features({"id": 1, "title": "旧文章", "content": "正文" * 2000})
    assert legacy["length_bucket_version"] == ARTICLE_LENGTH_CONTRACT_VERSION
    assert legacy["length_contract_version"] == "legacy_unknown"
    assert legacy["length_is_causal_score"] is False

    current = extract_writing_style_features({
        "id": 2,
        "title": "新文章",
        "content": "正文" * 2000,
        "generation_request_snapshot": {
            "length_plan": {"version": ARTICLE_LENGTH_CONTRACT_VERSION},
        },
    })
    assert current["length_contract_version"] == ARTICLE_LENGTH_CONTRACT_VERSION
