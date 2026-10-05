"""
Kimi 月烧 ¥1500 治理 · monitoring_interval_hours 分布审计脚本
[CTO-15.23 2026-05-09]

跑一次给老板报告:
1. monitoring_interval_hours 分布(看有多少客户被设成 8h/12h 频次过密)
2. monitoring_start_hour 分布(看 03:00 雪崩有多严重)
3. 最近 7 天 monitoring_token_usage 总量(看 76a17210 修复后真实扣费量)
4. monitoring_results 最近 7 天 4 引擎调用次数对比(看 Kimi 占比)

usage:
  cd /c/AI-Test/AgentsCope-07
  python scripts/audit_kimi_cost_2026_05_09.py

输出:scripts/kimi_cost_audit_2026_05_09.json
"""
import os
import sys
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_connection


def audit() -> dict:
    """跑 SQL 审计"""
    report = {"audit_time": datetime.now().isoformat()}

    with get_connection() as conn:
        cur = conn.cursor()

        # 1. monitoring_interval_hours 分布
        cur.execute("""
            SELECT monitoring_interval_hours, COUNT(*) AS clients
              FROM quotes
             WHERE monitoring_enabled = TRUE
             GROUP BY monitoring_interval_hours
             ORDER BY clients DESC
        """)
        report["interval_hours_distribution"] = [
            {"interval_hours": r["monitoring_interval_hours"], "clients": r["clients"]}
            for r in cur.fetchall()
        ]

        # 2. monitoring_start_hour 分布(看 03:00 雪崩程度)
        cur.execute("""
            SELECT monitoring_start_hour, COUNT(*) AS clients
              FROM quotes
             WHERE monitoring_enabled = TRUE
             GROUP BY monitoring_start_hour
             ORDER BY clients DESC
        """)
        report["start_hour_distribution"] = [
            {"start_hour": r["monitoring_start_hour"], "clients": r["clients"]}
            for r in cur.fetchall()
        ]

        # 3. 最近 7 天 monitoring_token_usage 总量(76a17210 修复后)
        try:
            cur.execute("""
                SELECT DATE(created_at) AS day,
                       COUNT(*) AS calls,
                       SUM(input_tokens) AS in_tokens,
                       SUM(output_tokens) AS out_tokens
                  FROM monitoring_token_usage
                 WHERE created_at >= NOW() - INTERVAL '7 days'
                 GROUP BY DATE(created_at)
                 ORDER BY day DESC
            """)
            report["token_usage_7d"] = [
                {
                    "day": r["day"].isoformat() if r["day"] else None,
                    "calls": r["calls"],
                    "in_tokens": r["in_tokens"] or 0,
                    "out_tokens": r["out_tokens"] or 0,
                }
                for r in cur.fetchall()
            ]
        except Exception as e:
            report["token_usage_7d_error"] = str(e)

        # 4. monitoring_results 最近 7 天 4 引擎调用次数对比
        try:
            cur.execute("""
                SELECT engine, COUNT(*) AS calls
                  FROM monitoring_results
                 WHERE created_at >= NOW() - INTERVAL '7 days'
                 GROUP BY engine
                 ORDER BY calls DESC
            """)
            report["engine_distribution_7d"] = [
                {"engine": r["engine"], "calls": r["calls"]}
                for r in cur.fetchall()
            ]
        except Exception as e:
            report["engine_distribution_7d_error"] = str(e)

        # 5. 03:00 客户列表(看哪些 brand 被设成 03:00)
        cur.execute("""
            SELECT brand_id, brand_name, monitoring_start_hour, monitoring_interval_hours
              FROM quotes
             WHERE monitoring_enabled = TRUE
               AND monitoring_start_hour = 3
             ORDER BY brand_id
             LIMIT 50
        """)
        report["clients_at_03_00"] = [
            {
                "brand_id": r["brand_id"],
                "brand_name": r["brand_name"],
                "start_hour": r["monitoring_start_hour"],
                "interval_hours": r["monitoring_interval_hours"],
            }
            for r in cur.fetchall()
        ]

    return report


def main():
    report = audit()
    out_path = Path(__file__).resolve().parent / "kimi_cost_audit_2026_05_09.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n=== Kimi 月烧治理审计 ===")
    print(f"\n1. interval_hours 分布:")
    for r in report.get("interval_hours_distribution", []):
        print(f"   {r['interval_hours']}h: {r['clients']} clients")

    print(f"\n2. start_hour 分布(看 03:00 雪崩):")
    for r in report.get("start_hour_distribution", []):
        print(f"   {r['start_hour']:02d}:00 - {r['clients']} clients")

    if "engine_distribution_7d" in report:
        print(f"\n3. 7 天 4 引擎调用占比:")
        for r in report["engine_distribution_7d"]:
            print(f"   {r['engine']}: {r['calls']} calls")

    print(f"\n4. 03:00 雪崩客户数:{len(report.get('clients_at_03_00', []))}")

    print(f"\n报告已保存:{out_path}")


if __name__ == "__main__":
    main()
