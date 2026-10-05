"""W2 标准答案蒸馏层 · 单测(DB-free · mock LLM + mock 加载 · temp 控制文件)。

覆盖:
  - holdout_side 稳定切分(同 id 同侧 · ~70/30 · 空 id 归 train)。
  - plan_distill_groups 三轴分组 + train/holdout 计数 + 门槛判定。
  - distill_one_group 只读 train 侧、解析候选包三件套、缺 candidate_prompt 抛错。
  - create_distilled_draft:守卫拦内部字段泄漏 → blocked;干净 prompt → draft。
"""
from __future__ import annotations

import asyncio
import json

import pytest


def test_holdout_side_stable_and_distributed():
    from services.writing_answer_distiller import holdout_side

    assert holdout_side(123) == holdout_side(123) == holdout_side("123")
    sides = [holdout_side(i) for i in range(3000)]
    train_ratio = sides.count("train") / len(sides)
    assert 0.60 < train_ratio < 0.80  # ~70%
    assert holdout_side(None) == "train"
    assert holdout_side("") == "train"


def _synthetic_rows(n_adopted: int, n_control: int, intent: str = "ranking"):
    feats_true = {"lead_answers_question": True, "has_faq_block": True, "has_data_block": True}
    feats_false = {"lead_answers_question": False, "has_faq_block": False, "has_data_block": False}
    rows = []
    idx = 1
    for _ in range(n_adopted):
        rows.append({
            "id": idx, "intent_type": intent, "group_key": "adopted_group",
            "title": f"采纳{idx}", "domain": "a.com", "source_weight": 1.0,
            "body": "开头先给答案。小标题一。FAQ。数据。" * 30, "features": dict(feats_true),
        })
        idx += 1
    for _ in range(n_control):
        rows.append({
            "id": idx, "intent_type": intent, "group_key": "search_only_control_group",
            "title": f"对照{idx}", "domain": "b.com", "source_weight": 0.3,
            "body": "泛泛而谈没有重点。" * 30, "features": dict(feats_false),
        })
        idx += 1
    return rows


def test_plan_groups_splits_and_thresholds(monkeypatch):
    import services.writing_answer_distiller as d

    rows = _synthetic_rows(50, 25, "ranking")
    monkeypatch.setattr(d, "load_labeled_article_rows", lambda *a, **k: ("general", rows))
    plan = d.plan_distill_groups("general", limit=1000)
    assert plan["group_count"] == 1
    g = plan["groups"][0]
    assert g["style_code"] == "comparison_review"
    # train/holdout 计数守恒
    assert g["train_adopted"] + g["holdout_adopted"] == 50
    assert g["train_control"] + g["holdout_control"] == 25
    # 门槛:train 采纳 ≥30 且对照 ≥10 → eligible
    assert g["eligible"] is True
    assert plan["eligible_count"] == 1


def test_plan_groups_below_threshold_not_eligible(monkeypatch):
    import services.writing_answer_distiller as d

    rows = _synthetic_rows(10, 25, "ranking")  # 采纳太少
    monkeypatch.setattr(d, "load_labeled_article_rows", lambda *a, **k: ("general", rows))
    plan = d.plan_distill_groups("general", limit=1000)
    assert plan["eligible_count"] == 0
    assert plan["groups"][0]["eligible"] is False


def test_row_style_code_uses_family_axis_and_empty_intent_fallback():
    import services.writing_answer_distiller as d

    assert d._row_style_code({"intent_type": "ranking", "style_family": "guide"}) == "buying_guide"
    assert d._row_style_code({"intent_type": "ranking", "style_family": "case"}) == "recommendation_review"
    assert d._row_style_code({"intent_type": "", "style_family": "guide"}) == "buying_guide"
    assert d._row_style_code({"intent_type": None, "style_family": "case"}) == "brand_softarticle"


def test_plan_groups_uses_style_family_axis(monkeypatch):
    import services.writing_answer_distiller as d

    rows = _synthetic_rows(8, 4, "ranking")
    for i, row in enumerate(rows):
        row["style_family"] = "guide" if i % 2 == 0 else "case"
    monkeypatch.setattr(d, "load_labeled_article_rows", lambda *a, **k: ("general", rows))
    plan = d.plan_distill_groups("general", limit=1000)
    styles = {g["style_code"] for g in plan["groups"]}
    assert "buying_guide" in styles
    assert "recommendation_review" in styles
    assert not styles.intersection({"ranking_v2", "authority_ranking", "trojan_horse"})


def test_distill_one_group_parses_and_uses_only_train(monkeypatch):
    import services.writing_answer_distiller as d

    rows = _synthetic_rows(50, 25, "ranking")
    train_adopted = [r for r in rows if r["group_key"] == "adopted_group" and d.holdout_side(r["id"]) == "train"]
    train_control = [r for r in rows if r["group_key"] == "search_only_control_group" and d.holdout_side(r["id"]) == "train"]
    holdout_adopted = [r for r in rows if r["group_key"] == "adopted_group" and d.holdout_side(r["id"]) == "holdout"]
    holdout_control = [r for r in rows if r["group_key"] == "search_only_control_group" and d.holdout_side(r["id"]) == "holdout"]
    group = {
        "style_code": "ranking_v2", "style_name": "排行榜单", "industry_key": "general",
        "intents": ["ranking"],
        "_rows": {
            "train_adopted": train_adopted, "train_control": train_control,
            "holdout_adopted": holdout_adopted, "holdout_control": holdout_control,
        },
    }

    captured = {}

    async def fake_llm(messages, **kw):
        captured["messages"] = messages
        return json.dumps({
            "answer_template": "先给答案 + 3 个小标题 + FAQ + 数据",
            "candidate_prompt": "你是排行榜写手。" + "严格结构约束。" * 20,
            "key_changes": ["开头先给答案", "加 FAQ", "加数据引用"],
        }, ensure_ascii=False)

    monkeypatch.setattr(d, "_call_distill_llm", fake_llm)
    monkeypatch.setattr(d, "get_prompt_for_style", lambda code: "当前 prompt 基线内容")
    pkg = asyncio.run(d.distill_one_group(group))
    assert pkg["candidate_prompt"]
    assert pkg["answer_template"]
    assert len(pkg["key_changes"]) == 3
    # 证据里 holdout 计数如实记录(供 W4 评审用),但样文 id 只来自 train 侧
    assert pkg["evidence"]["sample"]["train_adopted"] == len(train_adopted)
    assert pkg["evidence"]["sample"]["holdout_adopted"] == len(holdout_adopted)
    train_ids = {r["id"] for r in train_adopted}
    assert all(i in train_ids for i in pkg["evidence"]["adopted_sample_ids"])
    # 蒸馏输入的范文节选不得含 holdout 样本
    holdout_ids = {r["id"] for r in holdout_adopted}
    assert not (set(pkg["evidence"]["adopted_sample_ids"]) & holdout_ids)


def test_distill_missing_candidate_prompt_raises(monkeypatch):
    import services.writing_answer_distiller as d

    group = {
        "style_code": "ranking_v2", "style_name": "排行榜单", "industry_key": "general", "intents": ["ranking"],
        "_rows": {"train_adopted": _synthetic_rows(30, 0)[:30], "train_control": _synthetic_rows(0, 12)[:12],
                  "holdout_adopted": [], "holdout_control": []},
    }

    async def fake_llm(messages, **kw):
        return json.dumps({"answer_template": "x", "candidate_prompt": "", "key_changes": []})

    monkeypatch.setattr(d, "_call_distill_llm", fake_llm)
    monkeypatch.setattr(d, "get_prompt_for_style", lambda code: "base")
    with pytest.raises(ValueError):
        asyncio.run(d.distill_one_group(group))


def _temp_control(monkeypatch, tmp_path):
    monkeypatch.setenv("WRITING_STYLE_CONTROL_FILE", str(tmp_path / "ctrl.json"))
    monkeypatch.setenv("WRITING_STYLE_AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))


def test_create_distilled_draft_guard_blocks_internal_leak(monkeypatch, tmp_path):
    _temp_control(monkeypatch, tmp_path)
    from writing.style_control import create_distilled_draft

    leaky = "写作时请按毛利 markup ratio 成本 来组织内容。" + "补充说明。" * 20
    res = create_distilled_draft(
        style_code="ranking_v2", industry_key="general", prompt_text=leaky,
        template_doc="x", sample_count=40, control_sample_count=15,
    )
    v = res["version"]
    assert v["status"] == "blocked"  # 内部字段泄漏 → 守卫 block
    assert v["rollout_recommendation"]["may_activate"] is False


def test_create_distilled_draft_clean_prompt_is_draft(monkeypatch, tmp_path):
    _temp_control(monkeypatch, tmp_path)
    from writing.style_control import create_distilled_draft

    clean = "你是资深排行榜写手。开头先直接给出答案,再用 3-5 个小标题分点展开,末尾加 FAQ 和数据引用。" * 5
    res = create_distilled_draft(
        style_code="ranking_v2", industry_key="general", prompt_text=clean,
        template_doc="先给答案+小标题+FAQ", sample_count=40, control_sample_count=15,
    )
    v = res["version"]
    assert v["status"] == "draft"
    assert v["answer_template_doc"] == "先给答案+小标题+FAQ"
    assert v["source"] == "answer_distiller"
    assert v["prompt_sha256"]


def test_distill_cost_monotonic():
    from services.writing_answer_distiller import estimate_distill_cost

    assert estimate_distill_cost(0)["est_cost_cny"] == 0
    assert estimate_distill_cost(10)["est_cost_cny"] > estimate_distill_cost(1)["est_cost_cny"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
