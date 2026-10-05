"""WP8 · §13 rollout · monitoring_api.py SSE dead-ends → machine contracts.

The streaming-monitor generator emitted five ``{'type':'error','error': '...'}``
SSE events (no next-step action, some no code) — accident #8 over SSE. They now
merge a §13 contract from ``services.governance_alerts`` while preserving the SSE
transport keys (``type``/``error`` string) and the existing stable codes
(``requested_platform_not_entitled`` / ``no_eligible_monitoring_platforms`` /
``INSUFFICIENT_POINTS``) so no other consumer breaks.
"""
from pathlib import Path

import api.monitoring_api  # import sanity: module-level builder import must resolve
from services.governance_contract import is_alert_contract
from services.governance_alerts import (
    monitoring_no_monitorable_keywords_alert,
    monitoring_platform_not_entitled_alert,
    monitoring_no_eligible_platforms_alert,
    monitoring_insufficient_points_alert,
    monitoring_billing_error_alert,
)

_SRC = (Path(__file__).parent.parent / "api" / "monitoring_api.py").read_text(encoding="utf-8")
_CODE = "\n".join(l for l in _SRC.splitlines() if not l.lstrip().startswith("#"))


def test_all_five_sse_sites_use_builders():
    for builder in (
        "monitoring_no_monitorable_keywords_alert",
        "monitoring_platform_not_entitled_alert",
        "monitoring_no_eligible_platforms_alert",
        "monitoring_insufficient_points_alert",
        "monitoring_billing_error_alert",
    ):
        assert f"_gov_alerts.{builder}(" in _CODE, builder


def test_old_bare_sse_error_literals_gone():
    # the raw dead-end strings must no longer be emitted by these sites.
    assert "'error': '没有可监测的词条'" not in _CODE
    assert "'error': '请求包含所选词条未购买或不可用的监测引擎'" not in _CODE
    assert "'error': '所选词条没有可执行的已购监测引擎'" not in _CODE
    assert "f'计费异常: {str(fex)[:200]}'" not in _CODE


def test_existing_stable_codes_are_preserved():
    # other consumers (server.py:9409/9418, batch_monitor, non-SSE routes) key on
    # these exact codes — the builders must not have renamed them.
    assert monitoring_platform_not_entitled_alert()["code"] == "requested_platform_not_entitled"
    assert monitoring_no_eligible_platforms_alert()["code"] == "no_eligible_monitoring_platforms"
    assert monitoring_insufficient_points_alert()["code"] == "INSUFFICIENT_POINTS"


def test_sse_builders_are_contracts_with_action():
    for payload in (
        monitoring_no_monitorable_keywords_alert(),
        monitoring_platform_not_entitled_alert(),
        monitoring_no_eligible_platforms_alert(),
        monitoring_insufficient_points_alert(required=260, available=10),
        monitoring_billing_error_alert("db down"),
    ):
        assert is_alert_contract(payload)
        assert len(payload["actions"]) >= 1


def test_billing_error_does_not_leak_stack():
    p = monitoring_billing_error_alert("KeyError: 'freeze_id' at line 42 traceback")
    surface = " ".join(str(p.get(k) or "") for k in ("message", "reason", "impact", "repair_hint")).lower()
    for banned in ("keyerror", "traceback", "line 42"):
        assert banned not in surface
