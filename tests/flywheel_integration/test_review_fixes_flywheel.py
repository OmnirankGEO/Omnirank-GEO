"""出口审核修复 · 回归单测(测试/审核 CTO · 2026-07-04)。

覆盖本轮审核修掉的 P1(DB-free · 全 mock):
  - panorama:learn 节点查真表 geo_research_source_signals;binding 计数传 min_confidence(否则被 fail-soft 吞成恒 0 假空态)。
  - 模拟 auto-pick:general/all 族归一为无过滤(否则默认 scope 下点「生成对比样文」静默死亡)。
  - 模拟真跑失败落 failed 行(看板 failed 态可达;dry_run 失败不落行)。
  - 蒸馏重复守卫:同组已有未审结 draft → 跳过(dry_run 与真跑同口径);审结后解锁。
  - W4 评审:双评审全失败不落 review(防 0 分假 reviewed);受污染样本对(两臂输入不一致)强制 observe。
"""
from __future__ import annotations

import asyncio

import pytest


# ---------------- panorama 取数正确性 ----------------
def test_panorama_learn_table_and_binding_min_confidence(monkeypatch):
    import services.flywheel_panorama as pano

    executed: list[str] = []

    class _Cur:
        def execute(self, sql, params=()):
            executed.append(" ".join(str(sql).split()))

        def fetchone(self):
            return {"count": 7}

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr(pano, "get_connection", lambda: _Conn())

    binding_args: list = []
    import db.media_entity_flywheel_db as medb
    monkeypatch.setattr(
        medb, "count_recommended_binding_candidates",
        lambda min_confidence: binding_args.append(min_confidence) or 3,
    )
    import writing.style_control as sc
    monkeypatch.setattr(
        sc, "load_control_state",
        lambda: {"versions": [], "active_by_style": {}, "stable_by_style": {}},
    )

    out = pano.get_flywheel_panorama()
    learn = next(n for n in out["nodes"] if n["key"] == "learn")
    assert learn["value"] == 7
    assert any("FROM geo_research_source_signals" in s for s in executed)
    # binding 计数必须显式传 min_confidence(与媒体飞轮 tab 同源 SSOT 0.90)
    assert binding_args == [0.90]
    review = next(n for n in out["nodes"] if n["key"] == "review")
    assert review["detail"]["binding_candidates"] == 3


# ---------------- 模拟 auto-pick general 归一 ----------------
def test_autopick_industry_normalization():
    from db.writing_style_simulation_db import _normalize_autopick_industry

    assert _normalize_autopick_industry("general") == ""
    assert _normalize_autopick_industry("General ") == ""
    assert _normalize_autopick_industry("all") == ""
    assert _normalize_autopick_industry("ALL_ARTICLES") == ""
    assert _normalize_autopick_industry("") == ""
    assert _normalize_autopick_industry("装修") == "装修"


# ---------------- 模拟真跑失败落 failed 行 ----------------
def test_failed_simulation_recorded(monkeypatch):
    import services.writing_style_simulation as sim

    recorded: list[dict] = []
    monkeypatch.setattr(sim, "insert_simulation", lambda **kw: recorded.append(kw) or 1)
    monkeypatch.setattr(sim, "get_demo_quote", lambda industry, quote_id=None: None)

    with pytest.raises(ValueError):
        asyncio.run(sim.run_style_simulation(
            style_code="ranking_v2", candidate_prompt="候选 prompt", industry_key="装修", dry_run=False,
        ))
    assert recorded and recorded[0]["status"] == "failed"
    assert recorded[0]["industry_key"] == "装修"
    assert recorded[0]["style_code"] == "ranking_v2"

    # dry_run 预检失败直接抛给调用方,不落 failed 行(避免预览动作污染看板)
    recorded.clear()
    with pytest.raises(ValueError):
        asyncio.run(sim.run_style_simulation(
            style_code="ranking_v2", candidate_prompt="候选 prompt", industry_key="装修", dry_run=True,
        ))
    assert not recorded


# ---------------- 蒸馏重复守卫 ----------------
def test_distill_skips_groups_with_pending_draft(monkeypatch):
    import services.writing_answer_distiller as wd

    def fake_plan(industry, limit=1000, min_chars=500, oss_backfill_cap=None):
        return {
            "industry_key": "general", "loaded": 100, "group_count": 1, "eligible_count": 1,
            "groups": [{
                "industry_key": "general", "style_code": "ranking_v2", "style_name": "榜单",
                "intents": ["ranking"], "train_adopted": 40, "train_control": 10,
                "holdout_adopted": 12, "holdout_control": 4, "eligible": True, "reason": "达标可蒸馏",
                "_rows": {},
            }],
        }

    monkeypatch.setattr(wd, "plan_distill_groups", fake_plan)
    import writing.style_control as sc
    pending_state = {"versions": [{
        "source": "answer_distiller", "status": "draft", "style_code": "ranking_v2",
        "evidence_chain": {"industry_key": "general"},
    }]}
    monkeypatch.setattr(sc, "load_control_state", lambda: pending_state)

    res = asyncio.run(wd.distill_candidates(dry_run=True))
    assert res["skipped_pending_review"] == 1
    assert res["eligible_count"] == 0 and res["will_distill"] == 0
    assert res["groups"][0]["already_pending"] is True

    # 审结(active)后同组解锁,可再蒸馏
    pending_state["versions"][0]["status"] = "active"
    res2 = asyncio.run(wd.distill_candidates(dry_run=True))
    assert res2["skipped_pending_review"] == 0 and res2["will_distill"] == 1

    # 老 draft 缺行业信息 → 按文体保守跳过(兜底集)
    pending_state["versions"][0]["status"] = "draft"
    pending_state["versions"][0]["evidence_chain"] = {}
    res3 = asyncio.run(wd.distill_candidates(dry_run=True))
    assert res3["skipped_pending_review"] == 1


# ---------------- W4 评审护栏 ----------------
def _review_row(um_identical: bool):
    return {
        "id": 1, "style_code": "ranking_v2", "industry_key": "general",
        "current_article": "旧样文", "candidate_article": "新样文",
        "user_message_identical": um_identical, "version_id": "v1", "status": "generated",
    }


def _two_reviewers():
    return [
        {"label": "评审A", "provider": "a", "model": "x", "api_key": "k", "api_url": "u"},
        {"label": "评审B", "provider": "b", "model": "y", "api_key": "k", "api_url": "u"},
    ]


def test_review_all_fail_does_not_persist(monkeypatch):
    import services.writing_style_reviewer as rv

    persisted: list = []
    monkeypatch.setattr(rv, "get_simulation", lambda sid: _review_row(True))
    monkeypatch.setattr(rv, "_resolve_reviewers", _two_reviewers)
    monkeypatch.setattr(rv, "compute_holdout_lift", lambda industry, style: [])
    monkeypatch.setattr(rv, "update_simulation_review", lambda sid, s: persisted.append(s) or True)

    async def boom(messages, reviewer):
        raise RuntimeError("llm down")

    monkeypatch.setattr(rv, "_call_review_llm", boom)
    res = asyncio.run(rv.review_simulation(1, dry_run=False, sync_judge=False))
    assert res["status"] == "review_failed"
    assert not persisted  # 不落 0 分假 reviewed,卡片保持待评审可重试


def test_review_polluted_pair_forced_observe(monkeypatch):
    import services.writing_style_reviewer as rv

    persisted: list = []
    monkeypatch.setattr(rv, "get_simulation", lambda sid: _review_row(False))  # 两臂输入不一致
    monkeypatch.setattr(rv, "_resolve_reviewers", _two_reviewers)
    monkeypatch.setattr(rv, "compute_holdout_lift", lambda industry, style: [])
    monkeypatch.setattr(rv, "update_simulation_review", lambda sid, s: persisted.append(s) or True)

    async def fake_llm(messages, reviewer):
        return "{}"

    monkeypatch.setattr(rv, "_call_review_llm", fake_llm)
    monkeypatch.setattr(
        rv, "_de_blind_result",
        lambda parsed, jia_is: {
            "better_side": "candidate", "current_score": 50.0, "candidate_score": 80.0,
            "diff_summary": ["更贴近采纳特征"],
        },
    )
    res = asyncio.run(rv.review_simulation(1, dry_run=False, sync_judge=False))
    # 双模型一致候选本应 replace,但样本对受污染 → 强制 observe
    assert res["verdict"] == "observe"
    assert res["polluted_pair"] is True
    assert persisted and persisted[0]["verdict"] == "observe"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
