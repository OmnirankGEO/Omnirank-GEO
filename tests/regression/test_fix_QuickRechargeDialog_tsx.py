"""
Regression lock for GEO-R5-CAN-014 — QuickRechargeDialog poll route/field drift.

Root cause: the poller hit a non-existent route GET /api/wallet/recharge/{oid}
(r.ok always false → early return every tick → always timed out) and read the
wrong field d.data.payment_status. The single typed contract used by every other
live poller (RechargePage / BuyCredit / InventoryCenter) is
GET /api/wallet/order-status/{id} returning {data:{status:'paid'}}.

This is a source-inspection discriminative lock: it reads the .tsx text and
asserts the fixed route + field are present and the buggy ones are gone. Reverting
the fix makes these assertions fail. No DB / no server import.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TSX = ROOT / "frontend" / "src" / "components" / "managed" / "QuickRechargeDialog.tsx"


def _src() -> str:
    return TSX.read_text(encoding="utf-8")


def _polling_block(src: str) -> str:
    # isolate the startPolling body where the poll fetch lives
    start = src.index("const startPolling")
    end = src.index("const handleRecharge")
    return src[start:end]


def test_target_file_exists():
    assert TSX.exists(), f"missing target file: {TSX}"


def test_poll_uses_order_status_route():
    # [GEO-R5-CAN-014] fixed route must be the single order-status contract
    block = _polling_block(_src())
    assert "/api/wallet/order-status/" in block, (
        "poll must target GET /api/wallet/order-status/{oid} (single typed contract)"
    )


def test_poll_no_longer_hits_nonexistent_recharge_get():
    # buggy route GET /api/wallet/recharge/${oid} must be gone from the poll block.
    # (POST /api/wallet/recharge in handleRecharge is legitimate and excluded.)
    block = _polling_block(_src())
    assert not re.search(r"/api/wallet/recharge/\$\{oid\}", block), (
        "poll must not hit the non-existent GET /api/wallet/recharge/{oid} route"
    )


def test_poll_reads_status_not_payment_status():
    # [GEO-R5-CAN-014] must read data.status === 'paid', not payment_status
    block = _polling_block(_src())
    assert "data?.status === 'paid'" in block, (
        "poll must read d.data.status === 'paid' (order-status contract field)"
    )
    # the buggy field-read expression must be gone (comment mentions are ignored)
    assert not re.search(r"\.payment_status\s*===", block), (
        "poll must not read the wrong d.data.payment_status field"
    )
