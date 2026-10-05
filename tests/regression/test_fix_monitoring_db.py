"""Regression lock for db/monitoring_db.py GEO fixes.

Source-inspection discriminative locks (no DB / no server import):
- GEO-R2-CAN-004: auxiliary monitoring_token_usage insert is isolated with a
  SAVEPOINT + ROLLBACK TO SAVEPOINT so an aux failure cannot abort the primary
  monitoring_results transaction (COMMIT downgraded to ROLLBACK -> ghost row).
- GEO-R2-CAN-016: full_response / search_citations no longer application-truncated
  to [:8000] / [:16000], so stored content matches response_char_count.

Reverting either fix makes the corresponding assertions fail.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "db" / "monitoring_db.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _slice(marker_start: str, marker_end: str) -> str:
    i = SRC.index(marker_start)
    j = SRC.index(marker_end, i)
    return SRC[i:j]


# ---------------------------------------------------------------------------
# GEO-R2-CAN-004  savepoint isolation of aux token-usage insert
# ---------------------------------------------------------------------------

def test_can004_savepoint_established():
    assert 'SAVEPOINT sp_token_usage' in SRC, \
        "GEO-R2-CAN-004: expected a SAVEPOINT around the token-usage insert"


def test_can004_rollback_to_savepoint_on_error():
    # region between SAVEPOINT creation and conn.commit() in save_monitoring_result
    region = _slice('SAVEPOINT sp_token_usage', 'conn.commit()')
    assert 'ROLLBACK TO SAVEPOINT sp_token_usage' in region, \
        "GEO-R2-CAN-004: except branch must ROLLBACK TO SAVEPOINT to preserve primary row"
    assert 'RELEASE SAVEPOINT sp_token_usage' in region, \
        "GEO-R2-CAN-004: success path should RELEASE the savepoint"


def test_can004_rollback_precedes_commit():
    # The rollback-to-savepoint must appear before the primary commit so the
    # primary monitoring_results row survives an aux failure.
    idx_rollback = SRC.index('ROLLBACK TO SAVEPOINT sp_token_usage')
    idx_commit = SRC.index('conn.commit()', SRC.index('SAVEPOINT sp_token_usage'))
    assert idx_rollback < idx_commit, \
        "GEO-R2-CAN-004: ROLLBACK TO SAVEPOINT must precede conn.commit()"


# ---------------------------------------------------------------------------
# GEO-R2-CAN-016  no silent truncation of full_response / citations
# ---------------------------------------------------------------------------

def test_can016_no_full_response_8000_truncation():
    assert 'full_response[:8000]' not in SRC, \
        "GEO-R2-CAN-016: full_response must not be truncated to [:8000]"
    assert 'full_resp[:8000]' not in SRC, \
        "GEO-R2-CAN-016: batch full_resp must not be truncated to [:8000]"


def test_can016_no_citations_16000_truncation():
    assert 'search_citations[:16000]' not in SRC, \
        "GEO-R2-CAN-016: search_citations must not be truncated to [:16000]"


def test_can016_snippet_still_capped():
    # response_snippet is a summary field by design and should stay capped.
    assert 'response_snippet[:500]' in SRC and 'response_snippet", "")[:500]' in SRC, \
        "GEO-R2-CAN-016: response_snippet cap [:500] should be preserved (snippet semantics)"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
