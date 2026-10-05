"""Source-inspection regression locks for api/trial_pass_api.py GEO fixes.

Covers three verified findings:
  - GEO-R10-CAN-012: check-then-act race on apply -> wallet row FOR UPDATE lock
  - GEO-R10-CAN-013: reject refunds bonus-funded holds to paid -> refund to origin pool
  - GEO-R10-CAN-014: approve credits reward to acting admin -> credit true issuer

These assertions fail if a fix is reverted. They read the source text only,
so they do NOT touch the DB or import server.py.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "api" / "trial_pass_api.py").read_text(encoding="utf-8")


def _slice(marker_start: str, marker_end: str) -> str:
    """Return the source between the def line of a handler and the next end marker."""
    i = SRC.index(marker_start)
    j = SRC.index(marker_end, i)
    return SRC[i:j]


# ---------------------------------------------------------------------------
# GEO-R10-CAN-012: apply must serialize concurrent applies via a row lock
# ---------------------------------------------------------------------------

def test_can012_apply_locks_wallet_row_for_update():
    apply_src = _slice("async def apply_trial_pass", "async def approve_trial_pass")
    # A FOR UPDATE lock on the recipient's wallet row must exist before the
    # existing-trial check, serializing concurrent applies.
    assert re.search(
        r"FROM\s+user_wallets\s+WHERE\s+user_id\s*=\s*%s\s+FOR UPDATE",
        apply_src,
    ), "apply must SELECT ... user_wallets ... FOR UPDATE to close the race"
    assert "GEO-R10-CAN-012" in apply_src


def test_can012_lock_precedes_existing_trial_check():
    apply_src = _slice("async def apply_trial_pass", "async def approve_trial_pass")
    lock_pos = apply_src.index("FOR UPDATE")
    existing_pos = apply_src.index("status IN ('pending', 'approved', 'active')")
    assert lock_pos < existing_pos, "wallet lock must be acquired before existing-trial check"


# ---------------------------------------------------------------------------
# GEO-R10-CAN-013: reject refunds each pool to its exact origin
# ---------------------------------------------------------------------------

def test_can013_apply_tags_holds_with_trial_ref():
    apply_src = _slice("async def apply_trial_pass", "async def approve_trial_pass")
    assert '_hold_ref = f"trial:{trial_id}"' in apply_src, "apply must tag holds with trial ref"
    # both bonus and paid trial_hold rows carry order_id=_hold_ref
    assert apply_src.count("order_id=_hold_ref") >= 2, "both hold legs must carry the trial ref"
    assert "GEO-R10-CAN-013" in apply_src


def test_can013_reject_refunds_to_origin_pool_not_all_paid():
    reject_src = _slice("async def reject_trial_pass", "async def get_my_active_trial")
    # reconstruct split from the trial_hold ledger
    assert "type = 'trial_hold'" in reject_src, "reject must read the hold ledger to split refund"
    assert "bonus_refund" in reject_src and "paid_refund" in reject_src
    # bonus portion returns to bonus_points (not laundered into paid)
    assert re.search(r"bonus_points\s*=\s*bonus_points\s*\+\s*%s", reject_src), \
        "reject must credit bonus_refund back to bonus_points"
    assert 'insert_transaction(' in reject_src and '"trial_refund", "bonus"' in reject_src
    assert "GEO-R10-CAN-013" in reject_src


def test_can013_reject_no_longer_dumps_all_to_paid_unconditionally():
    reject_src = _slice("async def reject_trial_pass", "async def get_my_active_trial")
    # The old single unconditional 'refund -> paid_points only' UPDATE is gone;
    # the new UPDATE touches both pools.
    assert re.search(
        r"SET\s+bonus_points\s*=\s*bonus_points\s*\+\s*%s,\s*\n\s*paid_points\s*=\s*paid_points\s*\+\s*%s",
        reject_src,
    ), "reject UPDATE must credit both bonus and paid pools"


# ---------------------------------------------------------------------------
# GEO-R10-CAN-014: approve credits the true issuer, not the acting admin
# ---------------------------------------------------------------------------

def test_can014_reward_credited_to_true_issuer():
    approve_src = _slice("async def approve_trial_pass", "async def reject_trial_pass")
    assert 'reward_recipient_id = trial["issuer_user_id"]' in approve_src, \
        "reward recipient must be the trial's issuer, not the acting caller"
    # the wallet credit + ledger row must target reward_recipient_id
    assert re.search(r"WHERE\s+user_id\s*=\s*%s\s*\n\s*RETURNING paid_points", approve_src)
    assert "insert_transaction(\n            cursor, reward_recipient_id" in approve_src
    assert "GEO-R10-CAN-014" in approve_src


def test_can014_reward_no_longer_uses_acting_issuer_id():
    approve_src = _slice("async def approve_trial_pass", "async def reject_trial_pass")
    # The reward UPDATE/insert must NOT be keyed on the acting caller's issuer_id.
    # Extract the reward block (after the reward comment) and ensure issuer_id is
    # not the credited principal there.
    reward_block = approve_src[approve_src.index("GEO-R10-CAN-014"):]
    assert "(reward, issuer_id)" not in reward_block, \
        "reward must not be credited to the acting issuer_id"
    assert "cursor, issuer_id, \"trial_reward\"" not in reward_block, \
        "ledger row must not be recorded against the acting issuer_id"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
