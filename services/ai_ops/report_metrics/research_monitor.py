"""
调研监测指标 · 表列已核实(db/diagnosis_db.py geo_research_round 建表):
  status CHECK: pending/running/partial_success/failed/cancelled/completed/failed_resumable
  triggered_by CHECK: cron/manual/missed_cron_recovery · created_at TIMESTAMPTZ
只读。子指标独立失败。
"""

from services.ai_ops.report_metrics import open_conn, sub_metric


def collect(report_date) -> dict:
    conn = open_conn()
    try:
        cur = conn.cursor()

        def rounds():
            cur.execute(
                """
                SELECT COUNT(*) FILTER (WHERE status = 'running') AS running,
                       COUNT(*) FILTER (WHERE status = 'failed_resumable') AS failed_resumable
                FROM geo_research_round
                """)
            now_row = dict(cur.fetchone() or {})
            cur.execute(
                """
                SELECT COUNT(*) AS today_rounds,
                       COUNT(*) FILTER (WHERE triggered_by = 'missed_cron_recovery') AS today_recovery
                FROM geo_research_round
                WHERE created_at::date = %s
                """, (report_date,))
            day_row = dict(cur.fetchone() or {})
            return {
                "running": int(now_row.get("running") or 0),
                "failed_resumable": int(now_row.get("failed_resumable") or 0),
                "today_rounds": int(day_row.get("today_rounds") or 0),
                "today_missed_cron_recovery": int(day_row.get("today_recovery") or 0),
            }

        return {"rounds": sub_metric(conn, rounds)}
    finally:
        conn.close()
