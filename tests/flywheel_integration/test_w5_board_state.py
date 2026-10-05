"""W5 写作进化看板 · 8 态矩阵逻辑单测(纯 · DB-free)。"""
from __future__ import annotations

import pytest

from services.writing_evolution_board import _board_state


def _card(**kw):
    base = {"candidate_version_id": None, "review_summary": {}, "latest_sim_status": None,
            "train_adopted": 0, "candidate_generating": False}
    base.update(kw)
    return base


def test_recommend_replace():
    s, _ = _board_state(_card(candidate_version_id="v1", review_summary={"verdict": "replace"}))
    assert s == "recommend_replace"


def test_keep_when_current_better():
    s, _ = _board_state(_card(candidate_version_id="v1", review_summary={"verdict": "keep"}))
    assert s == "keep"


def test_disagree_dual_model():
    s, _ = _board_state(_card(candidate_version_id="v1",
                              review_summary={"verdict": "observe", "dual_model": True, "reviewer_agreement": False}))
    assert s == "disagree"


def test_observe_single_model():
    s, _ = _board_state(_card(candidate_version_id="v1",
                              review_summary={"verdict": "observe", "dual_model": False}))
    assert s == "observe"


def test_candidate_unreviewed():
    s, _ = _board_state(_card(candidate_version_id="v1", review_summary={}))
    assert s == "candidate_unreviewed"


def test_observing_sample_progress():
    s, label = _board_state(_card(train_adopted=18))
    assert s == "observing" and "18/30" in label


def test_ready_pending_distill_when_samples_reached_without_candidate():
    s, label = _board_state(_card(train_adopted=477, train_control=8))
    assert s == "ready_pending_distill"
    assert "样本已够 477 篇" in label
    assert "对照样本 8/10" in label
    assert "暂无候选" not in label


def test_ready_pending_distill_mentions_distill_when_control_reached():
    s, label = _board_state(_card(train_adopted=40, train_control=12))
    assert s == "ready_pending_distill"
    assert "等待蒸馏候选" in label


def test_no_candidate():
    s, _ = _board_state(_card())
    assert s == "no_candidate"


def test_generating_and_failed_take_priority():
    assert _board_state(_card(candidate_generating=True))[0] == "generating"
    assert _board_state(_card(latest_sim_status="failed"))[0] == "failed"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
