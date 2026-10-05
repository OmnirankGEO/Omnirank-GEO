"""WP8 · §13 rollout · advisor_api / content_api (+ 已随 E3 删除的团队模块) dead-ends → contracts.

These three domains shipped user-facing failures as bare ``str(e)`` / ``str(exc)``
(SSE ``{'type':'error','text':...}`` for content, ``event:error {error:...}`` for
advisor, ``detail=f"...{str(e)}"`` for team) — accident #8. They now build §13
contracts via ``services.governance_alerts`` while preserving each domain's
transport keys (``text``/``session_id`` for content SSE; ``error`` for advisor SSE)
and never leaking the raw exception to the user-facing surface.

Source-lock guards (these modules are heavy / import many deps) + builder-contract
assertions. Import sanity confirms each inline builder import path resolves.
"""
from pathlib import Path

import api.advisor_api  # import sanity
from services.governance_contract import is_alert_contract
from services.governance_alerts import (
    advisor_chat_failed_alert,
    advisor_not_found_alert,
    content_stream_failed_alert,
    team_operation_failed_alert,
)

_ROOT = Path(__file__).parent.parent


def test_builders_are_contracts_and_hide_stack():
    payloads = [
        advisor_chat_failed_alert("KeyError at line 5"),
        content_stream_failed_alert("topics", "Traceback boom"),
        content_stream_failed_alert("write", "boom"),
        team_operation_failed_alert("diagnose", "boom"),
        team_operation_failed_alert("weekly_report", "boom"),
    ]
    for p in payloads:
        assert is_alert_contract(p)
        assert len(p["actions"]) >= 1
        surface = " ".join(str(p.get(k) or "") for k in ("message", "reason", "impact", "repair_hint")).lower()
        for banned in ("keyerror", "traceback", "line 5"):
            assert banned not in surface


def test_content_stream_kind_maps_to_distinct_impact():
    # write vs topics vs corpus must not collapse into one impact string.
    impacts = {content_stream_failed_alert(k)["impact"] for k in ("write", "topics", "corpus")}
    assert len(impacts) == 3
