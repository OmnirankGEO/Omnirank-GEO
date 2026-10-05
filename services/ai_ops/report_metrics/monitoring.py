"""
监测指标 · 表列已核实(db/monitoring_db.py 建表):
  - monitoring_results:tested_at 归日 · is_detected SMALLINT(1=检出)
"活跃监测客户"按 SSOT 不能单用 quotes.monitoring_enabled,组合口径未在本包核实 → 待接入。
只读。子指标独立失败。
"""

from services.ai_ops.report_metrics import open_conn, sub_metric, unavailable
from services.monitoring_identity_review import aggregate_eligible_sql


def collect(report_date) -> dict:
    conn = open_conn()
    try:
        cur = conn.cursor()

        def results():
            cur.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE is_detected = 1) AS detected,
                       COUNT(DISTINCT platform) AS platforms
                FROM monitoring_results
                WHERE tested_at::date = %s
                  AND {aggregate_eligible_sql()}
                """, (report_date,))
            row = dict(cur.fetchone() or {})
            return {
                "today_results": int(row.get("total") or 0),
                "today_detected": int(row.get("detected") or 0),
                "platforms": int(row.get("platforms") or 0),
            }

        return {
            "results": sub_metric(conn, results),
            # 活跃监测客户:SSOT 要求组合 paid/service_end/发文条件,不单用 monitoring_enabled;
            # 组合口径未核实,先如实待接入(reference_monitoring_service_anchor_ssot)
            "active_clients": unavailable("待接入 · 活跃监测客户需 SSOT 组合口径,不单用 quotes.monitoring_enabled"),
        }
    finally:
        conn.close()
