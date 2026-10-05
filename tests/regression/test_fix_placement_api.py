"""Regression lock for GEO-R1-CAN-065: IDOR in patch_dismiss_kb_issue.

Source-inspection discriminative lock: the dismiss handler must resolve the
issue's owning quote_id and call require_quote_access BEFORE writing the DB.
Reverting the fix (removing the quote-access check) makes these assertions fail.

No DB / no server import required.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "api" / "placement_api.py").read_text(encoding="utf-8")


def _dismiss_handler_body() -> str:
    """Extract the patch_dismiss_kb_issue handler source region."""
    start = SRC.index("async def patch_dismiss_kb_issue")
    # next top-level decorator / function after this handler
    tail = SRC[start:]
    m = re.search(r"\n@router\.", tail[1:])
    end = (start + 1 + m.start()) if m else len(SRC)
    return SRC[start:end]


def test_dismiss_handler_enforces_quote_access():
    body = _dismiss_handler_body()
    # must resolve the issue's tenant-bound quote_id
    assert "SELECT quote_id FROM kb_check_issues" in body, \
        "dismiss handler must resolve owning quote_id from kb_check_issues"
    # must call the ownership helper (fail-closed for a write endpoint)
    assert "require_quote_access(" in body, \
        "dismiss handler must call require_quote_access"
    assert "allow_null=False" in body, \
        "dismiss handler must fail-closed with allow_null=False"


def test_access_check_precedes_write():
    body = _dismiss_handler_body()
    access_idx = body.index("require_quote_access(")
    write_idx = body.index("ok = dismiss_kb_issue(")
    assert access_idx < write_idx, \
        "require_quote_access must run before dismiss_kb_issue write"


def test_marker_present():
    assert "[GEO-R1-CAN-065]" in SRC
