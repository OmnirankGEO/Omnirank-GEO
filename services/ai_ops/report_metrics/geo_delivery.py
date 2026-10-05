"""
GEO 业务交付指标 · 表列已逐一核实(db/diagnosis_db.py 建表):
  - 诊断:diagnosis_records.created_at(当日新建;该表无状态列,不编"失败数")
  - 写作:article_generations.created_at + status('success'/'failed')
  - 品牌:brands.created_at(当日新增)
报价 selection session / 人工核数量:表口径未核实,标待接入,不拼 SQL(brief §5.3:没把握不要猜)。
只读。子指标独立失败。
"""

from services.ai_ops.report_metrics import open_conn, sub_metric, unavailable


def collect(report_date) -> dict:
    conn = open_conn()
    try:
        cur = conn.cursor()

        def diagnosis():
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM diagnosis_records WHERE created_at::date = %s",
                (report_date,))
            row = dict(cur.fetchone() or {})
            return {"today_created": int(row.get("cnt") or 0)}

        def articles():
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'failed') AS failed
                FROM article_generations
                WHERE created_at::date = %s
                """, (report_date,))
            row = dict(cur.fetchone() or {})
            return {
                "today_generated": int(row.get("total") or 0),
                "today_failed": int(row.get("failed") or 0),
            }

        def brands():
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM brands WHERE created_at::date = %s",
                (report_date,))
            row = dict(cur.fetchone() or {})
            return {"today_new": int(row.get("cnt") or 0)}

        return {
            "diagnosis": sub_metric(conn, diagnosis),
            "articles": sub_metric(conn, articles),
            "brands": sub_metric(conn, brands),
            # 报价/人工核:selection session 表口径未核实,先如实待接入
            "quotes": unavailable("待接入 · selection session/人工核口径未核实,不拼 SQL"),
        }
    finally:
        conn.close()
