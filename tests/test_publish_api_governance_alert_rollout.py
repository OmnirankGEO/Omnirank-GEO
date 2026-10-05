"""WP8 · §13 rollout · publish_api.py partial dead-ends → machine contracts.

The two media-proxy refusals (insufficient paid points 402; already-published
items need manual review 400) previously shipped as ``{code, message[, nums]}``
with no next-step action — a red code with no exit (accident #8). They now build
a §13 contract via ``services.governance_alerts`` while preserving the numeric
fields existing frontends read.
"""
import re
from pathlib import Path

import api.publish_api  # import sanity: inline builder import path must resolve
from services.governance_contract import is_alert_contract
from services.governance_alerts import (
    publish_insufficient_paid_points_alert,
    publish_needs_manual_review_alert,
)

_SRC = (Path(__file__).parent.parent / "api" / "publish_api.py").read_text(encoding="utf-8")
_CODE = "\n".join(l for l in _SRC.splitlines() if not l.lstrip().startswith("#"))


def test_call_sites_use_builders():
    assert "publish_insufficient_paid_points_alert(" in _CODE
    assert "publish_needs_manual_review_alert(" in _CODE
    # old partial literals (no actions) must be gone.
    assert '"code": "INSUFFICIENT_PAID_POINTS",' not in _CODE
    assert '"code": "NEED_MANUAL_REVIEW",' not in _CODE


def test_insufficient_paid_points_contract_and_numbers():
    p = publish_insufficient_paid_points_alert(required=500, available=100)
    assert is_alert_contract(p)
    assert p["code"] == "INSUFFICIENT_PAID_POINTS"
    assert p["required"] == 500 and p["available_paid"] == 100
    assert any(a["id"] == "recharge" for a in p["actions"])


def test_needs_manual_review_contract_and_count():
    p = publish_needs_manual_review_alert(submitted_count=3)
    assert is_alert_contract(p)
    assert p["code"] == "NEED_MANUAL_REVIEW"
    assert p["submitted_count"] == 3
    # must offer a human exit (联系有权限的人 · §13)
    assert any(a["id"] == "contact_support" for a in p["actions"])
