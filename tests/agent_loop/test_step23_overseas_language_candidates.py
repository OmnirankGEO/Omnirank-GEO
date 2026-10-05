import asyncio


# [开源 E3 · B2 · 2026-09-28] 社媒平台语言 / 意图引擎随包删除,守它们的 3 格退役。


def test_step23_industry_adapter_returns_candidate_answers_and_no_guess_policy(monkeypatch):
    from tools.agent_loop.adapters import industry

    def fake_get_industry_knowledge(industry_name, level="industry", category=None):
        assert industry_name == "教育咨询行业"
        return {
            "target_audience": {
                "primary": "初高中升学家长",
                "decision_factors": ["录取政策", "师资可信度"],
            },
            "pain_points": ["政策变化看不懂", "不知道如何择校"],
            "typical_products": ["升学规划", "志愿填报"],
        }

    monkeypatch.setattr(industry, "get_industry_knowledge", fake_get_industry_knowledge)
    result = asyncio.run(industry.query("教育咨询行业", "给用户候选答案", country="德国", limit=3))

    assert result["source"] == "industry_knowledge"
    assert result["country"] == "德国"
    assert result["candidate_answers"]
    assert result["fallback_policy"] == "ask_or_offer_choices_before_guessing"
    assert "录取政策" in " ".join(result["candidate_answers"])

