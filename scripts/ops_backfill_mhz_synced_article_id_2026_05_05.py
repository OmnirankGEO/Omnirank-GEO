"""ops_backfill_mhz_synced_article_id_2026_05_05.py

回填 mhz_synced_orders.article_id 为 NULL 的脏数据。

背景:
  53 条 mhz_publish_order_items 记录在提交时 mhz_order_id 漏写
  (failed/cancelled/paused_admin_dedupe 状态)，导致 sync 任务从 MHZ
  拉回订单时反查不到 article_id，重发功能报"找不到原始文章"。

回填规则:
  按 user_id + article_title + 同一天创建日期 反查 mhz_publish_orders。
  仅回填唯一匹配的（避免歧义）。

用法（部署 CTO 在生产容器内执行）:
  docker exec -it omnirank-blue python /app/scripts/ops_backfill_mhz_synced_article_id_2026_05_05.py --dry-run
  docker exec -it omnirank-blue python /app/scripts/ops_backfill_mhz_synced_article_id_2026_05_05.py --apply

红线:
  - 默认 dry-run，必须显式 --apply 才执行 UPDATE
  - 仅 UPDATE article_id（其他字段不动）
  - 不影响 article_id 已有值的行
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import get_connection


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际执行 UPDATE（默认 dry-run）")
    args = parser.parse_args()

    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT s.id AS order_id, s.order_sn, s.title, s.user_id, s.created_at::date AS d
        FROM mhz_synced_orders s
        WHERE s.article_id IS NULL AND s.title IS NOT NULL AND s.user_id IS NOT NULL
        ORDER BY s.created_at DESC
    """)
    rows = c.fetchall()
    print(f"扫描到 article_id IS NULL 的订单: {len(rows)} 条")

    matched = 0
    ambiguous = 0
    no_match = 0
    updates = []

    for r in rows:
        c.execute("""
            SELECT article_id FROM mhz_publish_orders
            WHERE user_id = %s AND article_title = %s AND created_at::date = %s
        """, (r["user_id"], r["title"].strip() if r["title"] else "", r["d"]))
        candidates = c.fetchall()
        if len(candidates) == 1:
            matched += 1
            updates.append((candidates[0]["article_id"], r["order_id"]))
        elif len(candidates) > 1:
            ambiguous += 1
        else:
            no_match += 1

    print(f"  唯一匹配可回填: {matched}")
    print(f"  多个匹配（歧义跳过）: {ambiguous}")
    print(f"  无匹配（直发 MHZ 后台）: {no_match}")

    if not args.apply:
        print("\n[DRY RUN] 未执行任何 UPDATE。加 --apply 才会写入。")
        conn.close()
        return

    if matched == 0:
        print("\n无可回填记录。")
        conn.close()
        return

    print(f"\n执行 UPDATE 中...")
    for article_id, order_id in updates:
        c.execute("UPDATE mhz_synced_orders SET article_id = %s WHERE id = %s AND article_id IS NULL",
                  (article_id, order_id))
    conn.commit()
    print(f"完成: 回填 {matched} 条 article_id")
    conn.close()


if __name__ == "__main__":
    main()
