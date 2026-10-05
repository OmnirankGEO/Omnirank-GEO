"""Refund work order center contract guards.

The admin refund page must behave like a financial work-order center:
creating/submitting a work order never performs a refund, and the existing
real refund endpoint is only reused behind an explicit execute action.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_refund_console_is_work_order_center_not_direct_refund_form():
    src = _read("frontend/src/pages/Admin/RefundConsole.tsx")

    assert "充值退款工单中心" in src
    assert "提交退款审核" in src
    assert "退款影响预览" in src
    assert "工单时间线" in src
    assert "/api/wallet/refund-work-orders" in src
    assert "authFetch('/api/wallet/refund'" not in src
    assert 'authFetch("/api/wallet/refund"' not in src


def test_refund_console_hides_internal_words_and_points_wording():
    src = _read("frontend/src/pages/Admin/RefundConsole.tsx")
    forbidden = [
        "积分",
        "链A",
        "链 A",
        "admin经营服",
        "recharge_orders.id",
        "服务商客户",
        "进货差",
    ]

    for word in forbidden:
        assert word not in src


def test_refund_work_order_api_submit_does_not_execute_real_refund():
    src = _read("api/refund_work_order_api.py")
    submit_segment = src[src.index("async def submit_refund_work_order"):src.index("async def approve_refund_work_order")]
    execute_segment = src[src.index("async def execute_refund_work_order"):src.index("async def upload_refund_work_order_attachment")]

    assert "_require_admin(request)" in submit_segment
    assert "request_refund(" not in submit_segment
    assert "confirm_execute" in execute_segment
    assert "request_refund(" in execute_segment


def test_refund_work_order_db_is_idempotent_and_prevents_open_duplicates():
    src = _read("db/refund_work_order_db.py")
    migration = _read("scripts/migration_refund_work_orders_2026_06_09.sql")
    combined = src + "\n" + migration

    assert "CREATE TABLE IF NOT EXISTS refund_work_orders" in combined
    assert "CREATE TABLE IF NOT EXISTS refund_work_order_attachments" in combined
    assert "CREATE TABLE IF NOT EXISTS refund_work_order_events" in combined
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uniq_refund_work_orders_open_order" in combined
    assert "WHERE status IN ('draft','submitted','approved','payout_pending')" in combined
    assert "DROP " not in migration.upper()
    assert "TRUNCATE " not in migration.upper()


def test_refund_work_order_completion_requires_payout_proof_when_needed():
    src = _read("api/refund_work_order_api.py")
    complete_segment = src[src.index("async def complete_refund_work_order"):]

    assert "requires_payout_proof" in complete_segment
    assert "payout_proof" in complete_segment
    assert "仅系统冲账不打款" in complete_segment


def test_refund_work_order_system_only_does_not_require_payout_and_auto_completes():
    api_src = _read("api/refund_work_order_api.py")
    db_src = _read("db/refund_work_order_db.py")
    ui_src = _read("frontend/src/pages/Admin/RefundConsole.tsx")

    assert "_preview_for_refund_method" in api_src
    assert '"needs_manual_payout": needs_manual_payout' in api_src
    assert "applyRefundMethodToPreview" in ui_src
    assert "无需人工打款凭证" in ui_src
    assert "completed_at = CURRENT_TIMESTAMP" in db_src
    assert "refund_status = 'completed'" in db_src


def test_refund_work_order_cannot_reject_after_system_reconciliation():
    src = _read("api/refund_work_order_api.py")
    reject_segment = src[src.index("async def reject_refund_work_order"):src.index("async def execute_refund_work_order")]

    assert 'not in ("draft", "submitted", "approved")' in reject_segment
    assert "已执行系统冲账或已结束的工单不能驳回" in reject_segment
