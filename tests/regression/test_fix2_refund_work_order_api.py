"""Regression locks for api/refund_work_order_api.py (fix batch 2).

Finding GEO-R10-CAN-016 (P3, refund-split-commit-state-lag) was SKIPPED:
a proper fix (single transactional orchestration / durable outbox with a
shared idempotency key spanning the *financial reversal*) is fund + concurrency
territory and would require editing out-of-scope files (api/wallet_api.py's
request_refund, db/refund_work_order_db.py). Per the fix-batch discipline that
must go to manual fund review rather than be hard-patched here.

These are source-inspection locks (no server.py import, no DB). They pin the
money-safety properties the skip relies on, so a future naive rewrite that
introduces a direct double-charge or drops the idempotent delegation fails here.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SRC = (
    Path(__file__).resolve().parents[2] / "api" / "refund_work_order_api.py"
).read_text(encoding="utf-8")


def _execute_body() -> str:
    """Return the body of execute_refund_work_order for focused assertions."""
    start = SRC.index("async def execute_refund_work_order")
    end = SRC.index("@router.post", start)
    return SRC[start:end]


def test_execute_delegates_to_idempotent_wallet_refund():
    """Money reversal must still go through wallet_api.request_refund, whose
    recharge_orders FOR UPDATE + refund_status guard is what prevents a double
    refund on retry. A rewrite that inlines a raw balance UPDATE would drop that
    guard -> this lock fails."""
    body = _execute_body()
    assert "request_refund" in body, "execute must delegate reversal to wallet request_refund"
    assert "from api.wallet_api import" in body, "must import the shared idempotent wallet refund path"
    # No naive direct money mutation inlined into execute.
    assert not re.search(r"UPDATE\s+user_wallets", body, re.IGNORECASE), (
        "execute must not inline a direct wallet balance mutation (bypasses idempotency guard)"
    )


def test_execute_advances_state_only_after_reversal():
    """save_execution_result (approved->completed/payout_pending CAS) must run
    AFTER request_refund, so retry re-enters via status='approved'. Reordering
    would break the convergence the finding relies on."""
    body = _execute_body()
    assert "save_execution_result" in body
    assert body.index("request_refund(") < body.index("save_execution_result("), (
        "reversal must precede the work-order state transition"
    )


def test_execute_still_gated_by_status_and_confirm():
    """execute must remain single-entry per approved work order: status guard +
    explicit confirm. These gates bound how often the (idempotent) reversal can
    be re-attempted."""
    body = _execute_body()
    assert 'work_order["status"] != "approved"' in body, "must keep approved-only status gate"
    assert "confirm_execute" in body, "must keep two-step confirm gate"


def test_execute_failure_is_not_swallowed():
    """On reversal failure the error is persisted and re-raised (never a silent
    default-success), so a stalled work order is observable for manual retry —
    the operational mitigation for the skipped durable-outbox gap."""
    body = _execute_body()
    assert "save_action_error" in body, "failures must be recorded"
    assert "raise" in body, "failures must propagate (no silent success)"
