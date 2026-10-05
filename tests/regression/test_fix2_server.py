"""Regression locks for Fix-Batch-2 server.py security/consistency fixes.

Source-inspection only: assert the fix markers/patterns are present in server.py.
Each locked candidate has >=1 discriminating assertion that FAILS if the fix is reverted.
Does NOT import server.py and does NOT touch the DB.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SERVER = Path(__file__).resolve().parents[2] / "server.py"
SRC = SERVER.read_text(encoding="utf-8")


def _slice_between(anchor: str, *, length: int = 4000) -> str:
    """Return SRC window starting at `anchor` (first occurrence)."""
    idx = SRC.find(anchor)
    assert idx != -1, f"anchor not found: {anchor!r}"
    return SRC[idx: idx + length]


# ---- GEO-R4-CAN-014: manual publication quote-access fail-closed ----
def test_r4_can_014_manual_publication_fail_closed():
    win = _slice_between("async def api_record_manual_publication")
    assert "[GEO-R4-CAN-014]" in win
    # the bare `except Exception:\n            pass` swallow must be gone: fail-closed 403
    assert 'detail="无法校验报价归属权限"' in win
    assert "raise HTTPException(status_code=403" in win


# ---- GEO-R4-CAN-009: get-quote-for-diagnosis access fail-closed ----
def test_r4_can_009_get_quote_fail_closed():
    win = _slice_between("async def api_get_quote_for_diagnosis")
    assert "[GEO-R4-CAN-009]" in win
    assert 'detail="无法校验诊断归属权限"' in win


# ---- GEO-R1-CAN-034: monitoring-config GET requires quote access ----
def test_r1_can_034_monitoring_config_get_authz():
    win = _slice_between("async def get_client_monitoring_config", length=1200)
    assert "[GEO-R1-CAN-034" in win
    # require_quote_access must appear BEFORE the try (guard outside try body)
    guard = win.find("require_quote_access(request, quote_id)")
    trybody = win.find("\n    try:")
    assert guard != -1 and trybody != -1 and guard < trybody


# ---- GEO-R1-CAN-068: in-memory session status ownership ----
def test_r1_can_068_session_status_ownership():
    win = _slice_between("def get_diagnosis_session_status", length=8000)
    # WORKERS=4 后轮询与 WebSocket 共用同一归属判定；必须先鉴权再读缓存。
    guard = win.find("if not _ws_authorize_session(_poll_user, session_id):")
    cache_read = win.find("manager.get_task_status(session_id)")
    assert guard != -1 and cache_read != -1 and guard < cache_read
    assert "raise HTTPException(status_code=403" in win[guard:cache_read]


# ---- GEO-R1-CAN-018: /api/articles/plan IDOR ----
def test_r1_can_018_plan_articles_idor():
    win = _slice_between("async def plan_articles", length=1600)
    assert "http_request: Request" in win
    assert "require_diagnosis_access(http_request, request.diagnosis_id)" in win


# ---- GEO-R1-CAN-087: forged operation-log attribution ----
def test_r1_can_087_operation_log_attribution():
    win = _slice_between("def record_operation_log", length=1400)
    assert "[GEO-R1-CAN-087" in win
    assert "http_request: Request" in win
    # operator_id must be forced to the authenticated identity, not the body value
    assert "request.operator_id = str(user_id)" in win


# ---- GEO-R1-CAN-060: compliance/check admin guard ----
def test_r1_can_060_compliance_admin_guard():
    win = _slice_between("def run_compliance_check_now", length=900)
    assert "[GEO-R1-CAN-060" in win
    guard = win.find("_require_admin_for_global_monitoring(request)")
    sink = win.find("run_daily_compliance_check()")
    assert guard != -1 and sink != -1 and guard < sink


# ---- GEO-R1-CAN-026: batch-delete path containment (sep boundary) ----
def test_r1_can_026_batch_delete_containment():
    win = _slice_between("def batch_delete_articles", length=2600)
    assert "[GEO-R1-CAN-026" in win
    # proper boundary check with os.sep; the old sep-less lexical prefix code must be gone
    assert "real_path.startswith(output_dir + os.sep)" in win
    assert "os.remove(real_path)" in win
    assert "os.remove(abs_path)" not in win


# ---- GEO-R1-CAN-149: WS auth revocation/freshness ----
def test_r1_can_149_ws_revocation_check():
    assert "def _ws_check_user_active(user: dict) -> bool:" in SRC
    assert "[GEO-R1-CAN-149" in SRC
    # both ws endpoints must call the freshness gate
    assert SRC.count("_ws_check_user_active(user)") >= 2


# ---- GEO-R10-CAN-018: PDF upload real extraction (no placeholder) ----
def test_r10_can_018_pdf_upload_extraction():
    win = _slice_between("async def upload_file", length=2200)
    assert "[GEO-R10-CAN-018]" in win
    assert "pdf_reader.page_texts(" in win  # OSS_25 #4:PyMuPDF(AGPL)换成 services.pdf_reader
    # the success-shaped placeholder assignment must be gone from the whole module
    assert 'content = f"[PDF File Uploaded: {file.filename}]"' not in SRC
    assert "status_code=422" in win  # scanned/encrypted PDF explicit error


# ---- GEO-R1-CAN-042: materials extract size ceiling (DoS) ----
def test_r1_can_042_extract_size_limit():
    win = _slice_between("async def api_extract_materials", length=1200)
    assert "[GEO-R1-CAN-042" in win
    assert "MAX_MATERIAL_SIZE" in win
    assert "status_code=413" in win
    # HTTPException must propagate (not be swallowed into a 200 error body)
    tail = _slice_between("def api_extract_materials", length=6000)
    assert "except HTTPException:\n        raise" in tail


# ---- GEO-R3-CAN-008: glob brand_name wildcard injection ----
def test_r3_can_008_glob_escape():
    win = _slice_between("def list_articles", length=3200)
    assert "[GEO-R3-CAN-008" in win
    assert "glob.escape(brand_name)" in win
    # raw brand_name must no longer be interpolated into glob patterns
    assert "output/articles/{brand_name}*.md" not in win


# ---- GEO-R2-CAN-003: article PUT re-derives title from H1 ----
def test_r2_can_003_title_h1_sync():
    win = _slice_between("def api_update_article", length=1600)
    assert "[GEO-R2-CAN-003" in win
    assert "SET content=%s, word_count=%s, title=%s" in win
    assert "^#\\s+(.+?)\\s*$" in win


# ---- GEO-R7-CAN-010: direction endpoints admit assigned agents ----
def test_r7_can_010_assigned_agent_access():
    # all three endpoints replaced owner-only with require_quote_access
    assert SRC.count("[GEO-R7-CAN-010]") >= 3
    for fn in ("api_get_direction_plan", "api_apply_distribution", "api_reset_to_recommended"):
        win = _slice_between(f"def {fn}", length=6000)
        assert "require_quote_access as _rqa" in win, fn
        assert "_rqa(http_request, quote_id, allow_null=False)" in win, fn


# ---- GEO-R1-CAN-145: report PDF render SSRF interception ----
def test_r1_can_145_pdf_render_ssrf():
    assert "[GEO-R1-CAN-145" in SRC
    assert "_block_external_subresource" in SRC
    win = _slice_between("_block_external_subresource")
    assert 'page.route("**/*"' in _slice_between("def _block_external_subresource", length=1200) or \
           'page.route("**/*", _block_external_subresource)' in SRC
    # route interception must be installed BEFORE set_content
    route_idx = SRC.find('page.route("**/*", _block_external_subresource)')
    setc_idx = SRC.find('page.set_content(html_content, wait_until="networkidle")')
    assert route_idx != -1 and setc_idx != -1 and route_idx < setc_idx
