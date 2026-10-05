"""
Regression source-inspection lock for api/dashboard_api.py GEO fixes.

GEO-R1-CAN-106 (predictable-tv-fallback-token):
  /api/tv/dashboard must fail closed when no tv_dashboard_token is configured,
  and must NOT fall back to a derivable constant (md5("omnirank_tv_2026")[:16]).
"""
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "api" / "dashboard_api.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _tv_handler_region() -> str:
    """Extract the tv_dashboard handler body for targeted assertions."""
    start = SRC.index('@router.get("/api/tv/dashboard")')
    end = SRC.index("def _compute_tv_dashboard", start)
    return SRC[start:end]


def test_no_derivable_fallback_token():
    # The predictable seed must no longer be used to derive a valid token.
    assert b"omnirank_tv_2026" not in SRC.encode("utf-8"), \
        "derivable md5 fallback seed still present"
    assert 'hashlib.md5(b"omnirank_tv_2026")' not in SRC


def test_derived_token_would_not_grant_access():
    # Prove the old bypass value is not silently accepted by construction:
    # it must not appear as an assigned valid_token fallback anywhere.
    derived = hashlib.md5(b"omnirank_tv_2026").hexdigest()[:16]
    # The literal derived value must not be hardcoded as an accepted secret.
    assert derived not in SRC


def test_fail_closed_when_unconfigured():
    region = _tv_handler_region()
    # After computing valid_token, an unconfigured (falsy) token must raise 403.
    assert "if not valid_token:" in region
    # The branch immediately following `if not valid_token:` must raise, not assign.
    m = re.search(r"if not valid_token:\s*\n\s*(.+)", region)
    assert m, "could not locate `if not valid_token:` branch"
    assert "raise HTTPException(403" in m.group(1), \
        "unconfigured token branch must fail closed with 403"


def test_marker_present():
    assert "[GEO-R1-CAN-106]" in _tv_handler_region()


def test_constant_time_compare():
    # Token comparison should be constant-time (hmac.compare_digest).
    region = _tv_handler_region()
    assert "compare_digest" in region
