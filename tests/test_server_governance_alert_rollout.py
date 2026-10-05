"""WP8 · §13 rollout · server.py user-facing dead-ends → machine contracts.

``server.py`` is too heavy to import in a unit test (full app startup), so this
is a source-lock guard: it asserts the four rewired failure paths now emit a
§13 alert via ``services.governance_alerts`` and that the old bare-string
dead-ends (accident #8) are gone. Anchors are real code tokens, not comments.

It also invokes the builders those call sites use and asserts the resulting
payloads are §13 contracts with >=1 action — so a regression in either the wiring
or the builder is caught.
"""
import re
from pathlib import Path

from services.governance_contract import is_alert_contract
from services.governance_alerts import (
    diagnosis_failed_alert,
    autofill_all_providers_failed_alert,
    monitoring_run_failed_alert,
    monitoring_stream_failed_alert,
)

_SRC = (Path(__file__).parent.parent / "server.py").read_text(encoding="utf-8")


def _code_lines():
    # strip full-line comments so anchors can't be satisfied by a comment.
    return "\n".join(
        ln for ln in _SRC.splitlines() if not ln.lstrip().startswith("#")
    )


def test_server_imports_and_calls_the_four_builders():
    code = _code_lines()
    for builder in (
        "diagnosis_failed_alert",
        "autofill_all_providers_failed_alert",
        "monitoring_run_failed_alert",
        "monitoring_stream_failed_alert",
    ):
        assert f"import {builder}" in code or builder + "(" in code, builder
        # must be actually invoked, not just imported
        assert builder + "(" in code, f"{builder} imported but never called"


def test_diagnosis_terminal_error_is_no_longer_bare_str_e():
    # the old dead-end: error_status = {..., "message": str(e), ...} with no code/actions.
    code = _code_lines()
    # the diagnosis error_status block must now spread the builder result.
    assert "**diagnosis_failed_alert(str(e))" in code
    # and the old '"message": str(e),' literal inside that terminal block is gone.
    assert '"message": str(e),\n            "done": True,' not in _SRC


def _region(anchor: str, span: int = 12) -> str:
    """The `span` lines starting at the line containing `anchor` (scoped guard —
    server.py has ~148 legitimate bare-error returns in unrelated routes)."""
    lines = _SRC.splitlines()
    for i, ln in enumerate(lines):
        if anchor in ln:
            return "\n".join(lines[i : i + span])
    raise AssertionError(f"anchor not found: {anchor}")


def test_monitoring_stream_error_preserves_transport_and_contract():
    region = _region("Run monitoring stream error")
    # within THIS route: old bare dead-end gone, merged contract present.
    assert 'return {"status": "error", "error": str(e)}' not in region
    assert "monitoring_stream_failed_alert(str(e))" in region
    assert '"error": _alert["message"], **_alert' in region


def test_builders_used_by_server_are_valid_contracts():
    for payload in (
        diagnosis_failed_alert("boom"),
        autofill_all_providers_failed_alert("TimeoutError: doubao"),
        monitoring_run_failed_alert("boom"),
        monitoring_stream_failed_alert("boom"),
    ):
        assert is_alert_contract(payload)
        assert len(payload["actions"]) >= 1


def test_merged_diagnosis_envelope_keeps_sse_transport_keys():
    # simulate the exact merge server.py performs so transport keys survive.
    merged = {
        "type": "error", "done": True, "stage": "error", "progress": 100,
        "error": True, **diagnosis_failed_alert("boom"),
    }
    assert merged["type"] == "error" and merged["error"] is True
    assert merged["done"] is True and merged["progress"] == 100
    assert is_alert_contract(merged)  # contract keys survived the merge
    assert merged["actions"]
