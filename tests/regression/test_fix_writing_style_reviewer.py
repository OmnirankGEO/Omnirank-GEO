"""Regression lock for GEO-R10-CAN-019 (services/writing_style_reviewer.py).

Long articles are truncated to REVIEW_ARTICLE_MAX_CHARS before being shown to
both review models. Without a guard, a candidate whose degraded/non-compliant
content lives in the unseen tail could still receive a `replace` verdict and be
promoted for the full version.

These are source-inspection locks: reverting the fix makes them fail. They do
not touch the DB or import server.py.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "services" / "writing_style_reviewer.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def test_marker_present():
    # The fix is tagged so a blanket revert removes the lock signal.
    assert "[GEO-R10-CAN-019]" in SRC


def test_tail_truncation_detected():
    # review_simulation must compute whether either arm exceeded the review cap.
    assert "tail_truncated" in SRC
    assert "current_chars > REVIEW_ARTICLE_MAX_CHARS" in SRC
    assert "candidate_chars > REVIEW_ARTICLE_MAX_CHARS" in SRC


def test_replace_downgraded_when_truncated():
    # A truncated whole-version verdict must not stay `replace`; downgrade to observe.
    assert 'if tail_truncated and verdict == "replace":' in SRC
    # The downgrade must land on observe (mirrors the polluted_pair guard).
    idx = SRC.index('if tail_truncated and verdict == "replace":')
    window = SRC[idx: idx + 400]
    assert 'verdict, reason = "observe"' in window


def test_coverage_provenance_persisted():
    # Coverage / provenance travels into review_summary so the board can show it.
    assert '"tail_truncated": tail_truncated' in SRC
    assert '"reviewed_max_chars": REVIEW_ARTICLE_MAX_CHARS' in SRC
    assert '"current_chars": current_chars' in SRC
    assert '"candidate_chars": candidate_chars' in SRC


def test_downgrade_is_before_verdict_persisted():
    # The downgrade must execute before update_simulation_review persists the verdict.
    down_idx = SRC.index('if tail_truncated and verdict == "replace":')
    persist_idx = SRC.index("update_simulation_review(simulation_id, review_summary)")
    assert down_idx < persist_idx
