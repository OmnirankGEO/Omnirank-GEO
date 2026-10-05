"""W4 AI 评审层 · 单测(纯逻辑 + mock LLM,DB-free)。

覆盖:5 维加权口径、双盲去盲映射、双模型一致才 replace(单模型/不一致→observe 防自嗨)、
采纳 rubric 来自 holdout lift、review_simulation 双盲→去盲全流程。
"""
from __future__ import annotations

import asyncio
import json

import pytest


def test_weighted_total_adoption_is_30pct():
    from services.writing_style_reviewer import _weighted_total

    allz = {"ai_trust": 0, "platform_compliance": 0, "brand_value": 0, "user_value": 0, "adoption_fit": 0}
    assert _weighted_total({**allz, "adoption_fit": 100}) == 30.0   # 采纳维度独占 30%
    perfect = {k: 100 for k in ["ai_trust", "platform_compliance", "brand_value", "user_value", "adoption_fit"]}
    assert _weighted_total(perfect) == 100.0
    # 4 维满分 + 采纳 0 → 70
    base_only = {k: 100 for k in ["ai_trust", "platform_compliance", "brand_value", "user_value"]}
    base_only["adoption_fit"] = 0
    assert _weighted_total(base_only) == 70.0


def test_norm_scores_clamps():
    from services.writing_style_reviewer import _norm_scores

    s = _norm_scores({"ai_trust": 150, "platform_compliance": -5, "brand_value": "80"})
    assert s["ai_trust"] == 100 and s["platform_compliance"] == 0 and s["brand_value"] == 80
    assert s["user_value"] == 0 and s["adoption_fit"] == 0  # 缺失补 0


def test_de_blind_maps_jia_to_actual_side():
    from services.writing_style_reviewer import _de_blind_result

    parsed = {
        "jia": {"ai_trust": 90, "platform_compliance": 90, "brand_value": 90, "user_value": 90, "adoption_fit": 90},
        "yi": {"ai_trust": 50, "platform_compliance": 50, "brand_value": 50, "user_value": 50, "adoption_fit": 50},
        "better": "jia",
    }
    # 甲 = candidate → better_side = candidate,candidate 高分
    d = _de_blind_result(parsed, jia_is="candidate")
    assert d["better_side"] == "candidate"
    assert d["candidate_score"] > d["current_score"]
    # 甲 = current → better_side = current
    d2 = _de_blind_result(parsed, jia_is="current")
    assert d2["better_side"] == "current"
    assert d2["current_score"] > d2["candidate_score"]


def test_aggregate_dual_model_consistency():
    from services.writing_style_reviewer import _aggregate

    cand = {"better_side": "candidate"}
    cur = {"better_side": "current"}
    # 双模型一致候选 → replace
    assert _aggregate([dict(cand), dict(cand)], 2)[0] == "replace"
    # 双模型一致当前 → keep
    assert _aggregate([dict(cur), dict(cur)], 2)[0] == "keep"
    # 双模型不一致 → observe
    assert _aggregate([dict(cand), dict(cur)], 2)[0] == "observe"
    # 单模型候选 → observe(永不单模型 replace)
    assert _aggregate([dict(cand)], 1)[0] == "observe"
    # 单模型当前 → keep
    assert _aggregate([dict(cur)], 1)[0] == "keep"
    # 全失败 → observe
    assert _aggregate([{"error": "x"}], 2)[0] == "observe"


def test_render_adoption_rubric_from_lift():
    from services.writing_style_reviewer import _render_adoption_rubric

    lift = [{"label": "开头先给答案", "adopted_share": 0.83, "control_share": 0.2, "lift": 4.1, "recommended": True}]
    txt = _render_adoption_rubric(lift)
    assert "开头先给答案" in txt and "83%" in txt
    assert "暂无显著" in _render_adoption_rubric([])  # 空 → 兜底文案


def _make_review_flow(monkeypatch, reviewer_labels, better_marker="CANDMARK"):
    """mock get_simulation + reviewers + LLM(按 marker 判断候选在甲/乙,一致偏好候选)。"""
    import services.writing_style_reviewer as r

    monkeypatch.setattr(r, "get_simulation", lambda sid: {
        "id": sid, "style_code": "ranking_v2", "industry_key": "法律", "version_id": None,
        "current_article": "当前版正文 CURRENTMARK 内容。", "candidate_article": "候选版正文 CANDMARK 内容。",
    })
    monkeypatch.setattr(r, "update_simulation_review", lambda sid, s: True)
    monkeypatch.setattr(r, "compute_holdout_lift", lambda ind, sc: [])
    monkeypatch.setattr(r, "_resolve_reviewers", lambda: [
        {"label": lb, "provider": "p", "model": "m", "url": "u", "key": "k"} for lb in reviewer_labels
    ])

    async def fake_llm(messages, reviewer):
        user = messages[1]["content"]
        jia_part = user.split("【文章乙】")[0]
        cand_in_jia = better_marker in jia_part
        hi = {"ai_trust": 90, "platform_compliance": 90, "brand_value": 90, "user_value": 90, "adoption_fit": 90}
        lo = {"ai_trust": 50, "platform_compliance": 50, "brand_value": 50, "user_value": 50, "adoption_fit": 50}
        # 偏好候选:候选那一侧给高分
        return json.dumps({
            "jia": hi if cand_in_jia else lo,
            "yi": lo if cand_in_jia else hi,
            "better": "jia" if cand_in_jia else "yi",
            "diff_summary": ["候选开头先给答案", "候选加了 FAQ"],
        })

    monkeypatch.setattr(r, "_call_review_llm", fake_llm)
    return r


def test_review_simulation_dual_consistent_replace(monkeypatch):
    r = _make_review_flow(monkeypatch, ["评审A", "评审B"])
    for seed in (0, 1, 2, 3):  # 无论双盲怎么 swap,都应去盲映射到 candidate → replace
        res = asyncio.run(r.review_simulation(1, dry_run=False, seed=seed, sync_judge=False))
        assert res["verdict"] == "replace", f"seed={seed}"
        assert res["dual_model"] is True
        assert res["avg_candidate_score"] > res["avg_current_score"]
        assert res["diff_summary"]


def test_review_simulation_single_model_never_replace(monkeypatch):
    r = _make_review_flow(monkeypatch, ["评审A"])  # 只有 1 个评审模型
    res = asyncio.run(r.review_simulation(1, dry_run=False, seed=0, sync_judge=False))
    assert res["verdict"] == "observe"  # 单模型即便偏好候选也不 replace
    assert res["dual_model"] is False


def test_review_dry_run_no_llm(monkeypatch):
    import services.writing_style_reviewer as r

    monkeypatch.setattr(r, "get_simulation", lambda sid: {"id": sid})
    monkeypatch.setattr(r, "_resolve_reviewers", lambda: [{"label": "评审A"}, {"label": "评审B"}])
    res = asyncio.run(r.review_simulation(5, dry_run=True))
    assert res["mode"] == "dry_run" and res["dual_model_available"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
