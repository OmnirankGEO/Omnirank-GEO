from services.writing_strategy_service import build_strategy_candidate


def test_strategy_candidate_adds_structure_rules_only_when_sample_gate_passes():
    candidate = build_strategy_candidate(
        industry_key="旅游酒店",
        style_features=[{
            "style_family": "guide",
            "article_structure_analysis": {
                "adopted_group_count": 42,
                "explicit_cited_count": 35,
                "search_only_control_count": 40,
                "engine_count": 2,
                "sample_domain_count": 8,
                "feature_lift": [
                    {
                        "feature": "lead_answers_question",
                        "label": "开头先回答问题",
                        "adopted_share": 0.72,
                        "control_share": 0.31,
                        "lift": 2.32,
                    },
                    {
                        "feature": "has_checklist",
                        "label": "正文有选择清单",
                        "adopted_share": 0.58,
                        "control_share": 0.24,
                        "lift": 2.42,
                    },
                ],
            },
        }],
        source_signals=[{"balanced_weight": 1.0, "signal_tier": "answer_adopted"}],
        outcome_signals=[],
    )

    assert candidate["industry_key"] == "tourism_hotel"
    assert candidate["status"] == "shadow"
    assert candidate["requires_admin_review"] is True
    assert candidate["structure_status"] == "ready"
    assert len(candidate["structure_rules"]) >= 2
    assert any("开头先回答问题" in rule for rule in candidate["structure_rules"])
    assert "不得自动替换线上写作策略" in candidate["guardrails"]
    assert candidate["source_summary"]["adopted_group_count"] == 42
    assert candidate["production_takeover"] is False


def test_strategy_candidate_keeps_structure_observing_when_samples_are_insufficient():
    candidate = build_strategy_candidate(
        industry_key="旅游酒店",
        style_features=[{
            "style_family": "guide",
            "article_structure_analysis": {
                "adopted_group_count": 8,
                "explicit_cited_count": 3,
                "search_only_control_count": 20,
                "engine_count": 1,
                "sample_domain_count": 2,
                "feature_lift": [
                    {
                        "feature": "lead_answers_question",
                        "label": "开头先回答问题",
                        "adopted_share": 0.75,
                        "control_share": 0.25,
                        "lift": 3.0,
                    },
                ],
            },
        }],
        source_signals=[{"balanced_weight": 1.0, "signal_tier": "answer_adopted"}],
        outcome_signals=[],
    )

    assert candidate["structure_status"] == "observing"
    assert candidate["structure_rules"] == []
    assert any("样本观察中" in warning for warning in candidate["structure_warnings"])
    assert candidate["production_takeover"] is False
