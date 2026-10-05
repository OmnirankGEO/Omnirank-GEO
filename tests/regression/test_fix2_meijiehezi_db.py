"""Regression guards for FIX batch round-2 · db/meijiehezi_db.py

Source-inspection based (no server.py import, no DB dependency). Each assertion
pins a specific fix marker so a revert of the fix fails the test.

Findings covered:
- GEO-R9-CAN-005 (FIXED): unmatched synced-order ownership reassignment.
- GEO-R2-CAN-024 (SKIPPED · needs-manual-fund-review): refund saga untouched.
- GEO-R7-CAN-004 (SKIPPED · needs-manual-concurrency-review): idempotency PK untouched.
"""
import re
from pathlib import Path

sys_path_root = str(Path(__file__).resolve().parents[2])
import sys
sys.path.insert(0, sys_path_root)

TARGET = Path(__file__).resolve().parents[2] / "db" / "meijiehezi_db.py"
SRC = TARGET.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GEO-R9-CAN-005 · FIXED — ownership reassignment (not blind COALESCE)
# ---------------------------------------------------------------------------

def test_r9_marker_present():
    assert "[GEO-R9-CAN-005]" in SRC, "fix marker missing → reassignment fix reverted"


def test_r9_coalesce_user_id_removed():
    # The old bug pattern (in the actual SQL) kept the non-null admin fallback
    # forever. Match the normalized SQL assignment, not prose in comments.
    norm = re.sub(r"\s+", " ", SRC)
    assert "user_id = COALESCE(user_id, %s)" not in norm, (
        "old SQL 'user_id = COALESCE(user_id, %s)' still present "
        "-> admin fallback never reassigned"
    )


def test_r9_case_reassignment_sql_present():
    # New SQL must reassign when owner is NULL or equals the admin fallback.
    norm = re.sub(r"\s+", " ", SRC)
    assert "user_id = CASE" in norm, "CASE-based ownership reassignment missing"
    assert "WHEN %s IS NOT NULL AND (user_id IS NULL OR user_id = %s)" in norm, (
        "reassignment guard (NULL or admin fallback) missing"
    )


def test_r9_reassign_flag_and_audit_log():
    assert "_reassign_owner" in SRC, "reassignment detection flag missing"
    assert "exists[\"user_id\"] == fallback_user_id" in SRC, (
        "reassign flag must compare existing owner to admin fallback"
    )
    # Audit trail requirement from the finding.
    assert "归属回收" in SRC, "audit-trail log for reassignment missing"


def test_r9_brand_article_still_coalesced():
    # brand_id/article_id are NULL at first insert; COALESCE fill is correct.
    norm = re.sub(r"\s+", " ", SRC)
    assert "brand_id = COALESCE(brand_id, %s)" in norm
    assert "article_id = COALESCE(article_id, %s)" in norm


# ---------------------------------------------------------------------------
# GEO-R2-CAN-024 · SKIPPED — refund saga left for manual fund review.
# Guard: the terminal-commit-then-refund shape is intentionally unchanged so
# this test documents the skip and flags if someone silently rewrites it.
# ---------------------------------------------------------------------------

def test_r2_refund_flow_still_item_keyed_and_commented():
    # refund still keyed by item id (fund logic untouched by this round).
    assert 'refund_key=f"item:{it[\'id\']}"' in SRC, (
        "refund key changed — fund logic must not be altered in this round"
    )


# ---------------------------------------------------------------------------
# GEO-R7-CAN-004 · SKIPPED — idempotency PK is a schema migration on a
# fund-guarding concurrency primitive; left for manual concurrency review.
# Guard: single-column PK + composite lookup remain (documents the skip).
# ---------------------------------------------------------------------------

def test_r7_idempotency_pk_unchanged():
    norm = re.sub(r"\s+", " ", SRC)
    assert "request_id TEXT PRIMARY KEY" in norm, (
        "idempotency PK changed — requires manual schema-migration review"
    )
    assert "ON CONFLICT (request_id) DO NOTHING" in norm, (
        "idempotency ON CONFLICT changed — requires manual concurrency review"
    )
