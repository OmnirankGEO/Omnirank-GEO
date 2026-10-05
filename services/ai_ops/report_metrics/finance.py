"""
财务资金链指标 · 口径复用 SSOT(api/finance_api.py + 各 db helper 建表,已逐列核实):
  - 已支付充值:recharge_orders.payment_status='paid' · COALESCE(paid_at,created_at) 归日/归月 · amount_cents
  - 退款工单:refund_work_orders.status,在途口径同 db/refund_work_order_db.py:122
    (draft/submitted/approved/payout_pending)· requested_refund_cents
  - 提现:withdrawal_requests.status='pending' · amount_yuan(注意:该表存元,不是 cents)
  - 服务商结算:agent_settlement_requests.status='pending' · request_amount_cents
  - 冲销:agent_revenue_ledger.reversed_at(当日)
只读。子指标独立失败。
"""

from services.ai_ops.report_metrics import open_conn, sub_metric


def collect(report_date) -> dict:
    conn = open_conn()
    try:
        cur = conn.cursor()

        def recharge():
            cur.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_cents), 0) AS amount_cents
                FROM recharge_orders
                WHERE payment_status = 'paid'
                  AND COALESCE(paid_at, created_at)::date = %s
                """, (report_date,))
            day = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_cents), 0) AS amount_cents
                FROM recharge_orders
                WHERE payment_status = 'paid'
                  AND date_trunc('month', COALESCE(paid_at, created_at)) = date_trunc('month', %s::date)
                """, (report_date,))
            month = dict(cur.fetchone() or {})
            return {
                "day_paid_count": int(day.get("cnt") or 0),
                "day_paid_amount_cents": int(day.get("amount_cents") or 0),
                "month_paid_count": int(month.get("cnt") or 0),
                "month_paid_amount_cents": int(month.get("amount_cents") or 0),
            }

        def refund():
            # 在途口径同 db/refund_work_order_db.py(draft/submitted/approved/payout_pending)
            cur.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(SUM(requested_refund_cents), 0) AS req_cents
                FROM refund_work_orders
                WHERE status IN ('draft', 'submitted', 'approved', 'payout_pending')
                """)
            open_row = dict(cur.fetchone() or {})
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM refund_work_orders WHERE created_at::date = %s",
                (report_date,))
            today = dict(cur.fetchone() or {})
            return {
                "open_count": int(open_row.get("cnt") or 0),
                "open_requested_cents": int(open_row.get("req_cents") or 0),
                "today_new": int(today.get("cnt") or 0),
            }

        def withdrawal():
            # 该表金额列是 amount_yuan(元),不做 cents 换算
            cur.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_yuan), 0) AS amount_yuan
                FROM withdrawal_requests
                WHERE status = 'pending'
                """)
            row = dict(cur.fetchone() or {})
            return {
                "pending_count": int(row.get("cnt") or 0),
                "pending_amount_yuan": float(row.get("amount_yuan") or 0),
            }

        def settlement():
            cur.execute(
                """
                SELECT COUNT(*) AS cnt, COALESCE(SUM(request_amount_cents), 0) AS amount_cents
                FROM agent_settlement_requests
                WHERE status = 'pending'
                """)
            row = dict(cur.fetchone() or {})
            return {
                "pending_count": int(row.get("cnt") or 0),
                "pending_amount_cents": int(row.get("amount_cents") or 0),
            }

        def ledger():
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM agent_revenue_ledger WHERE reversed_at::date = %s",
                (report_date,))
            row = dict(cur.fetchone() or {})
            return {"reversed_today": int(row.get("cnt") or 0)}

        return {
            "recharge": sub_metric(conn, recharge),
            "refund": sub_metric(conn, refund),
            "withdrawal": sub_metric(conn, withdrawal),
            "settlement": sub_metric(conn, settlement),
            "ledger": sub_metric(conn, ledger),
        }
    finally:
        conn.close()
