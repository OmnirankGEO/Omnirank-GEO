"""僵尸单盘点 · audit #9 返修 · 2026-06-10(只读)

prod 实证 327/329 类僵尸单:quotes.status='paid' + service_status='active' 但
confirmed_keywords 0 词(客户付款/已激活服务期却无任何交付内容)。根因:offline_mark_paid /
agent_activate_service 旧版先激活后 COUNT、0 词不拒绝(本批已前置守卫根治写入侧)。

本脚本只读盘点存量,供 Deploy-CTO 决策(补关键词 or 回退 service_status)。不改库。

用法:
  python -X utf8 scripts/zombie_paid_active_no_keywords_2026_06_10.py

退出码:0 = 完成 / 2 = DB 错
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))


_SQL = """
    SELECT q.id AS quote_id, q.brand_id, q.brand_name, q.status, q.service_status,
           q.monthly_price, q.paid_at, q.service_start_date, q.service_end_date,
           (SELECT COUNT(*) FROM confirmed_keywords ck WHERE ck.quote_id = q.id) AS kw_count
    FROM quotes q
    WHERE q.status = 'paid'
      AND q.service_status = 'active'
      AND COALESCE(q.deleted_at, NULL) IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM confirmed_keywords ck WHERE ck.quote_id = q.id
      )
    ORDER BY q.id
"""


def run() -> dict:
    try:
        from db.connection import get_connection
        conn = get_connection()
    except Exception as e:
        return {"ok": False, "error": f"DB 连接失败: {e}"}
    try:
        cur = conn.cursor()
        cur.execute(_SQL)
        rows = [dict(r) for r in cur.fetchall()]
        total_amount = sum(int(r.get("monthly_price") or 0) for r in rows)
        return {
            "ok": True,
            "zombie_total": len(rows),
            "monthly_price_sum": total_amount,
            "zombies": rows[:100],
        }
    except Exception as e:
        return {"ok": False, "error": f"查询失败: {e}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if report.get("ok") else 2)
