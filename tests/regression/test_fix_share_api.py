"""Regression locks for api/share_api.py GEO fixes.

Source-inspection discriminative locks: assert repair markers exist in the
source text. Reverting a fix makes the corresponding assertion fail. No DB /
no server import required.

Covered findings:
- GEO-R1-CAN-121: ownership check in _build_report_poster (report poster)
- GEO-R2-CAN-023: bearer share_token no longer logged verbatim
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "api" / "share_api.py").read_text(encoding="utf-8")


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


# ==================== GEO-R1-CAN-121 ====================

def test_report_poster_imports_ownership_helper():
    assert "require_diagnosis_access" in SRC
    # imported from the shared helper, not re-implemented
    assert re.search(
        r"from\s+auth\.brand_access\s+import\s+require_diagnosis_access", SRC
    ), "must reuse auth.brand_access.require_diagnosis_access"


def test_report_poster_signature_takes_request():
    assert re.search(
        r"def\s+_build_report_poster\(user:\s*dict,\s*params:\s*dict,\s*origin:\s*str,\s*request:\s*Request\)",
        SRC,
    ), "_build_report_poster must accept a Request for ownership check"


def test_report_poster_dispatch_passes_request():
    assert re.search(
        r'"report":\s*lambda:\s*_build_report_poster\(user,\s*req\.params,\s*origin,\s*request\)',
        SRC,
    ), "generate_poster dispatch must pass request into report builder"


def test_report_poster_checks_ownership_before_reading_and_minting():
    body = _slice(SRC, "def _build_report_poster(", "# ==================== API 端点")
    # ownership check present, fail-closed
    m_check = re.search(r"require_diagnosis_access\(request,\s*int\(diagnosis_id\),\s*allow_null=False\)", body)
    assert m_check, "report poster must call require_diagnosis_access(..., allow_null=False)"
    # the check must run BEFORE reading the diagnosis row and BEFORE minting a short link
    pos_check = m_check.start()
    pos_select = body.index("FROM diagnosis_records WHERE id")
    pos_link = body.index("_create_short_link(")
    assert pos_check < pos_select, "ownership check must precede the diagnosis_records read"
    assert pos_check < pos_link, "ownership check must precede short-link minting"


# ==================== GEO-R2-CAN-023 ====================

def test_share_token_not_logged_verbatim():
    body = _slice(SRC, "def _verify_public_report_token(", "@router.get(\"/api/public/report/{diagnosis_id}\")")
    # the raw f-string interpolation of the full token in the warning must be gone
    assert "st={st}" not in body, "raw share_token must not be logged verbatim (CWE-532)"
    # even a prefix/length hint is a reusable correlation signal and must stay out of logs
    assert "_st_hint" not in body
    assert "type(e).__name__" in body, "log only the exception type for diagnosis"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
