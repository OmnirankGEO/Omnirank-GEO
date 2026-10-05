from services import writing_structure_guidance as guidance_service
from services.writing_structure_guidance import (
    build_bounded_structure_instruction,
    build_guidance_payload,
    build_structure_guidance_for_quote,
    infer_default_article_type,
)


def test_guidance_without_active_strategy_uses_safe_system_default():
    payload = build_guidance_payload(None, quote_id=101, industry="旅游酒店")

    assert payload["success"] is True
    assert payload["available"] is True
    assert payload["can_apply"] is False
    assert payload["default_enabled"] is True
    assert payload["production_takeover"] is False
    assert payload["status_label"] == "系统默认写法"
    assert "保守默认结构" in payload["evidence_note"]
    instruction = build_bounded_structure_instruction(payload, title="酒店预订平台哪个好")
    assert "对比测评类" in instruction
    assert "不是效果承诺" in instruction


def test_article_type_inference_uses_title_and_user_choice():
    assert infer_default_article_type("2026装修公司十大品牌排行榜") == "ranking_recommendation"
    assert infer_default_article_type("装修公司哪个好？优缺点对比") == "comparison_review"
    assert infer_default_article_type("装修报价多少钱？预算避坑指南") == "price_budget"
    assert infer_default_article_type("旧房翻新怎么选", user_choice="guide") == "buying_guide"
    assert infer_default_article_type("装修报价多少钱", style_family="ranking") == "price_budget"


def test_guidance_requires_ready_sample_counts():
    row = {
        "id": 7,
        "industry_key": "tourism_hotel",
        "guidance": "开头先回答问题，正文用清单组织。",
        "common_elements": ["开头先回答问题", "正文用清单组织"],
        "guardrails": ["不得承诺固定效果"],
        "source_summary": {
            "adopted_group_count": 4,
            "explicit_cited_count": 3,
            "search_only_control_count": 30,
            "engine_count": 2,
            "sample_domain_count": 5,
            "qualified_structure_lift_count": 2,
        },
        "confidence": 0.82,
    }

    payload = build_guidance_payload(row, quote_id=101, industry="旅游酒店")

    assert payload["available"] is True
    assert payload["can_apply"] is False
    assert payload["default_enabled"] is True
    assert payload["sample_status"] == "observing"
    assert "系统推荐" in payload["status_label"]
    assert "范文结构统计" in payload["evidence_note"]


def test_reference_template_can_build_instruction_without_claiming_adoption():
    row = {
        "id": 17,
        "industry_key": "home_improvement",
        "style_family": "ranking",
        "guidance": "适合回答哪家好类问题。",
        "common_elements": ["先给筛选标准", "再分层推荐"],
        "guardrails": ["不得承诺固定排名"],
        "source_summary": {
            "style_feature_count": 119,
            "search_only_control_count": 119,
            "engine_count": 1,
            "sample_domain_count": 8,
        },
        "confidence": 0.7,
    }

    payload = build_guidance_payload(row, quote_id=102, industry="装修")
    instruction = build_bounded_structure_instruction(
        payload,
        title="2026环保涂料十大品牌权威榜单",
        user_choice="auto",
    )

    assert payload["can_apply"] is False
    assert payload["default_enabled"] is True
    assert payload["article_type_label"] == "榜单推荐类"
    assert "系统推荐写法" in instruction
    assert "筛选标准" in instruction
    assert "不是效果承诺" in instruction


def test_quote_guidance_uses_r6_baseline_when_no_active_strategy(monkeypatch):
    monkeypatch.setattr(guidance_service, "get_active_strategy_version", lambda industry_key: None)
    monkeypatch.setattr(
        guidance_service,
        "load_structure_baseline_summary",
        lambda industry_key: {
            "industry_key": industry_key,
            "style_family": "comparison",
            "sample_count": 274,
            "style_distribution": {"comparison": 120, "guide": 80},
            "source_summary": {
                "style_feature_count": 274,
                "search_only_control_count": 274,
                "sample_domain_count": 30,
                "source_signal_count": 274,
            },
        },
    )

    payload = build_structure_guidance_for_quote({"id": 103, "industry": "GEO 优化服务"})
    instruction = build_bounded_structure_instruction(payload, title="GEO服务公司哪个好对比")

    assert payload["sample_status"] == "baseline"
    assert payload["default_enabled"] is True
    assert payload["baseline"]["sample_count"] == 274
    assert payload["article_type_label"] == "对比测评类"
    assert "行业范文结构基线" in payload["status_detail"]
    assert "对比测评类" in instruction


def test_ready_guidance_builds_bounded_instruction():
    row = {
        "id": 8,
        "industry_key": "tourism_hotel",
        "guidance": "本行业被答案采纳的文章常见结构：开头先回答问题；正文用清单、步骤或维度组织；加入避坑和风险提示。",
        "common_elements": ["开头先回答问题", "正文用清单、步骤或维度组织", "加入避坑和风险提示"],
        "guardrails": ["不得承诺固定排名或固定 30 天效果", "必须保留客户知识库事实核查"],
        "source_summary": {
            "adopted_group_count": 42,
            "explicit_cited_count": 36,
            "search_only_control_count": 80,
            "engine_count": 3,
            "sample_domain_count": 12,
            "qualified_structure_lift_count": 3,
        },
        "confidence": 0.91,
    }

    payload = build_guidance_payload(row, quote_id=101, industry="旅游酒店")
    instruction = build_bounded_structure_instruction(payload)

    assert payload["can_apply"] is True
    assert payload["default_enabled"] is True
    assert payload["sample_status"] == "ready"
    assert payload["production_takeover"] is False
    assert "只作为本项目结构参考" in instruction
    assert "开头先回答问题" in instruction
    assert "不得承诺固定排名" in instruction
    assert len(instruction) <= 700
