"""
LLM 成本指标 · 表列已核实(db/monitoring_db.py llm_call_log 建表):
  created_at · estimated_cost NUMERIC(单位=元,tools/llm_call_tracker.py 计价含 flat_cost_yuan)
  success BOOLEAN · platform/model
只读。子指标独立失败。
"""

from services.ai_ops.report_metrics import open_conn, sub_metric


def collect(report_date) -> dict:
    conn = open_conn()
    try:
        cur = conn.cursor()

        def today():
            cur.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(estimated_cost), 0) AS cost_yuan,
                       COUNT(*) FILTER (WHERE success = FALSE) AS fails
                FROM llm_call_log
                WHERE created_at::date = %s
                """, (report_date,))
            row = dict(cur.fetchone() or {})
            return {
                "calls": int(row.get("calls") or 0),
                "cost_yuan": round(float(row.get("cost_yuan") or 0), 2),
                "fails": int(row.get("fails") or 0),
            }

        def month():
            cur.execute(
                """
                SELECT COALESCE(SUM(estimated_cost), 0) AS cost_yuan
                FROM llm_call_log
                WHERE date_trunc('month', created_at) = date_trunc('month', %s::date)
                """, (report_date,))
            row = dict(cur.fetchone() or {})
            return {"cost_yuan": round(float(row.get("cost_yuan") or 0), 2)}

        def top_platform():
            cur.execute(
                """
                SELECT platform, COUNT(*) AS calls
                FROM llm_call_log
                WHERE created_at::date = %s
                GROUP BY platform ORDER BY calls DESC LIMIT 3
                """, (report_date,))
            rows = [dict(r) for r in cur.fetchall()]
            return {"top": [{"platform": r["platform"], "calls": int(r["calls"])} for r in rows]}

        return {
            "today": sub_metric(conn, today),
            "month": sub_metric(conn, month),
            "top_platform": sub_metric(conn, top_platform),
        }
    finally:
        conn.close()
