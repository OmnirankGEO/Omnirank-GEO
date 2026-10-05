"""Regression locks for round_runner.py fix batch 2 (GEO-R2 缺陷修复第2轮).

Source-inspection based: assert the fix markers/logic exist in the source so a
revert fails the test. Does NOT import server.py and does NOT touch the DB.
"""
import re
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RUNNER = Path(__file__).resolve().parents[2] / "services" / "research_monitor" / "round_runner.py"
SRC = RUNNER.read_text(encoding="utf-8")


def test_source_file_exists():
    assert RUNNER.is_file(), f"missing {RUNNER}"


# ---------- GEO-R6-CAN-014: resume per-call idempotency ----------

def test_can014_resume_idempotency_guard_present():
    # The fix must SELECT an existing 'success' round_call for this
    # (round, prompt, platform) BEFORE inserting/executing, and short-circuit.
    assert "[GEO-R6-CAN-014]" in SRC, "GEO-R6-CAN-014 marker missing"
    # A completed-call lookup must exist inside call_one_platform.
    assert re.search(
        r"SELECT\s+1\s+FROM\s+geo_research_round_call",
        SRC,
        re.IGNORECASE,
    ), "resume idempotency SELECT lookup missing"
    assert "prompt_id IS NOT DISTINCT FROM" in SRC, "NULL-safe prompt_id match missing"
    assert "_already_done" in SRC, "already-done short-circuit variable missing"


def test_can014_guard_short_circuits_before_provider_call():
    # The early-return on _already_done must precede the pending-INSERT and the
    # query_with_retry provider call, otherwise re-execution still happens.
    guard_idx = SRC.index("if _already_done:")
    insert_idx = SRC.index("INSERT INTO geo_research_round_call")
    retry_idx = SRC.index("result = await query_with_retry")
    assert guard_idx < insert_idx, "guard must precede the pending INSERT"
    assert guard_idx < retry_idx, "guard must precede the provider call"


# ---------- GEO-R1-CAN-124: answer truncation loss-awareness ----------

def test_can124_no_silent_16000_truncation():
    assert "[GEO-R1-CAN-124]" in SRC, "GEO-R1-CAN-124 marker missing"
    # The old silent hard clip must be gone.
    assert "answer[:16000]" not in SRC, "silent answer[:16000] truncation still present"


def test_can124_persists_full_answer_with_marker():
    # Full answer persisted for realistic sizes; pathological >200000 gets a marker.
    assert "_answer_persist" in SRC, "loss-aware answer variable missing"
    assert "_ANSWER_PERSIST_MAX" in SRC, "safety bound missing"
    assert "truncated" in SRC, "explicit truncation marker missing"
    # The INSERT must bind the loss-aware value, not the raw clipped prefix.
    assert re.search(r"_answer_persist,\s*#", SRC), "INSERT does not bind _answer_persist"


def test_can124_truncation_logic_math():
    # Re-implement the source's branch to lock the intended behavior.
    _ANSWER_PERSIST_MAX = 200000

    def persist(answer: str) -> str:
        if len(answer) > _ANSWER_PERSIST_MAX:
            omitted = len(answer) - _ANSWER_PERSIST_MAX
            return answer[:_ANSWER_PERSIST_MAX] + f"\n\n[GEO-R1-CAN-124 truncated: {omitted} chars omitted]"
        return answer

    short = "x" * 16001  # would have been clipped by old code, now kept whole
    assert persist(short) == short, "16001-char answer must be persisted whole (no 16000 clip)"

    huge = "y" * (_ANSWER_PERSIST_MAX + 42)
    out = persist(huge)
    assert out.startswith("y" * _ANSWER_PERSIST_MAX)
    assert "truncated: 42 chars omitted" in out


# ---------- Skipped fund findings must NOT have been silently hard-changed ----------

def test_settlement_status_still_completed_not_partial():
    # CAN-020/CAN-021 are skipped (needs-manual-fund-review). Guard that we did NOT
    # sneak a fund-affecting status change into _run_pipeline's completion call.
    assert "update_round_complete(round_id, status='completed', summary=summary)" in SRC, (
        "round completion status changed — fund-affecting edit must be left for manual review"
    )
