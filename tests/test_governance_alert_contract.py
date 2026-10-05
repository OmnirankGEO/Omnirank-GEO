"""WP8 · §13 · reusable alert machine-contract builder.

Locks the §13 contract (7 fields incl. impact + >=1 legal action) and the
accident-#8 invariant: an alert can never be constructed as a dead end.
"""
import pytest

from services.governance_contract import (
    CONTRACT_KEYS,
    AlertContractError,
    build_alert,
    is_alert_contract,
)


def test_build_alert_has_all_seven_keys_including_impact():
    a = build_alert(
        "DIAG_NO_VALID_SAMPLE", "本次诊断有效样本不足",
        reason="4 个引擎中过半未响应", impact="本次诊断未出报告",
        repair_hint="可重试诊断", actions=[{"id": "retry", "label": "重试诊断"}],
        rule_version="diagnosis-min-sample-v1",
    )
    assert set(a) == set(CONTRACT_KEYS)
    assert a["impact"] == "本次诊断未出报告"
    assert is_alert_contract(a)


def test_dead_end_alert_is_rejected_at_construction():
    # accident #8: a red code with no next-step action must never ship.
    with pytest.raises(AlertContractError):
        build_alert("X", "m", reason="r", impact="i", actions=[])
    with pytest.raises(AlertContractError):
        build_alert("X", "m", reason="r", impact="i", actions=None)


def test_missing_why_or_what_is_rejected():
    with pytest.raises(AlertContractError):
        build_alert("X", "m", reason="", impact="i", actions=[{"id": "a", "label": "b"}])
    with pytest.raises(AlertContractError):
        build_alert("X", "m", reason="r", impact="", actions=[{"id": "a", "label": "b"}])


def test_is_alert_contract_rejects_bare_error_and_partial():
    assert not is_alert_contract({"status": "error", "error": "boom"})
    assert not is_alert_contract("boom")
    assert not is_alert_contract({"code": "X", "message": "m"})  # missing keys/actions


def test_actions_are_normalized_and_malformed_dropped():
    a = build_alert(
        "X", "m", reason="r", impact="i",
        actions=[
            {"id": "good", "label": "L"},
            {"id": "", "label": "bad"},          # dropped (no id)
            "notdict",                             # dropped
            {"id": "t", "label": "T", "type": "nav"},
        ],
    )
    assert [x["id"] for x in a["actions"]] == ["good", "t"]
    assert a["actions"][1]["type"] == "nav"
