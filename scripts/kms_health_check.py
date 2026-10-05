"""KMS Health Check · v1.6 2026-05-29

老板 P0 6 大任务 #6 · 扫 4 类 KMS 异常:
  1. active KMS 重复(同 keyword_id 多 active sub)— idx_kms_unique_active 部署后应为 0
  2. is_monitored=TRUE 但 KMS 无 active sub(ck 状态错位 · 老 brand 6/428 类)
  3. active KMS 但 quote.status != 'paid'(v1.2 paid-only 守护漏的脏数据)
  4. active KMS 但 ck.is_monitored=FALSE(状态机不一致 · 老 disable 漏清)

用法:
  python -X utf8 scripts/kms_health_check.py             # 输出 JSON 报告 stdout
  python -X utf8 scripts/kms_health_check.py --verbose   # 输出每条样本行

退出码:0 = clean / 1 = 有异常 / 2 = DB 连接失败

只读 · 不改任何数据 · 安全反复跑。
"""
from __future__ import annotations
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))


def _connect():
    from db.connection import get_connection
    return get_connection()


def check_dup_active(cur, verbose: bool = False) -> dict:
    cur.execute("""
        SELECT keyword_id, COUNT(*) AS dup_count, ARRAY_AGG(id ORDER BY id) AS sub_ids
        FROM keyword_monitor_subscriptions
        WHERE status = 'active'
        GROUP BY keyword_id
        HAVING COUNT(*) > 1
        ORDER BY dup_count DESC, keyword_id
        LIMIT 100
    """)
    rows = [dict(r) for r in cur.fetchall()]
    return {"count": len(rows), "samples": rows if verbose else rows[:5]}


def check_is_monitored_but_no_active_kms(cur, verbose: bool = False) -> dict:
    cur.execute("""
        SELECT ck.id AS keyword_id, ck.keyword, ck.quote_id, ck.brand_id,
               ck.monitoring_subscription_id
        FROM confirmed_keywords ck
        LEFT JOIN keyword_monitor_subscriptions s
          ON s.keyword_id = ck.id AND s.status = 'active'
        WHERE ck.is_monitored = TRUE
          AND COALESCE(ck.monitoring_status, 'active') = 'active'
          AND s.id IS NULL
        ORDER BY ck.id DESC
        LIMIT 500
    """)
    rows = [dict(r) for r in cur.fetchall()]
    return {"count": len(rows), "samples": rows if verbose else rows[:10]}


def check_active_kms_but_quote_not_paid(cur, verbose: bool = False) -> dict:
    cur.execute("""
        SELECT s.id AS sub_id, s.keyword_id, s.quote_id, s.user_id, s.brand_id,
               q.status AS quote_status
        FROM keyword_monitor_subscriptions s
        LEFT JOIN quotes q ON q.id = s.quote_id
        WHERE s.status = 'active'
          AND (q.status IS NULL OR q.status != 'paid')
        ORDER BY s.id DESC
        LIMIT 500
    """)
    rows = [dict(r) for r in cur.fetchall()]
    return {"count": len(rows), "samples": rows if verbose else rows[:10]}


def check_active_kms_but_ck_not_monitored(cur, verbose: bool = False) -> dict:
    cur.execute("""
        SELECT s.id AS sub_id, s.keyword_id, s.quote_id,
               ck.is_monitored, ck.monitoring_status
        FROM keyword_monitor_subscriptions s
        JOIN confirmed_keywords ck ON ck.id = s.keyword_id
        WHERE s.status = 'active'
          AND (ck.is_monitored IS DISTINCT FROM TRUE
               OR COALESCE(ck.monitoring_status, 'active') != 'active')
        ORDER BY s.id DESC
        LIMIT 500
    """)
    rows = [dict(r) for r in cur.fetchall()]
    return {"count": len(rows), "samples": rows if verbose else rows[:10]}


def run(verbose: bool = False) -> dict:
    try:
        conn = _connect()
    except Exception as e:
        return {"ok": False, "error": f"DB 连接失败: {e}"}

    try:
        cur = conn.cursor()
        report = {
            "ok": True,
            "anomalies": {
                "dup_active": check_dup_active(cur, verbose),
                "is_monitored_no_active_kms": check_is_monitored_but_no_active_kms(cur, verbose),
                "active_kms_not_paid": check_active_kms_but_quote_not_paid(cur, verbose),
                "active_kms_ck_not_monitored": check_active_kms_but_ck_not_monitored(cur, verbose),
            },
        }
        total = sum(a["count"] for a in report["anomalies"].values())
        report["total_anomaly_rows"] = total
        report["clean"] = total == 0
        return report
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="KMS 4 类异常扫描 · v1.6")
    ap.add_argument("--verbose", action="store_true", help="输出全部样本行")
    args = ap.parse_args()

    report = run(verbose=args.verbose)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    if not report.get("ok"):
        return 2
    return 0 if report.get("clean") else 1


if __name__ == "__main__":
    sys.exit(main())
