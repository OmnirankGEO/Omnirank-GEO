#!/usr/bin/env python3
"""漏斗 publish/monitor 历史回填 + mhz item brand_id 回填 · 2026-05-29 · CTO-15.23

背景(老板 2026-05-29 拍板 P0/P2):
  publish/monitor 埋点 2026-05-25 才补(4 天) vs 90 天窗口 → pipeline_stage_log 历史缺口
  (prod 实证 publish 埋点 2 brand / 真实 7 · monitor 5 / 真实 20)。
  另:mhz_publish_order_items.brand_id 108/108 全 NULL(代码从没填 · 仅靠 article→quote 间接链)。

本脚本三段(全幂等 · 可重复跑):
  1. publish stage_log 回填:已发布 item → order → article → quote.brand_id,
     补 (brand, published_at) 的 publish/complete 事件(meta.source='mhz_backfill' · 按 item_id 去重)。
  2. monitor stage_log 回填:monitoring_tasks.brand_id → 补 (brand, created_at) 的 monitor/complete
     事件(meta.source='monitor_backfill' · 按 task_id 去重)。
  3. P2 mhz_publish_order_items.brand_id 回填:NULL → 经 order→article→quote.brand_id 反填。

注意:漏斗 dashboard 已改读真实表(Q1),不再依赖本回填;本回填是为
  pipeline_stage_log 完整性(scheduler flow-drop 提醒 / 其他分析消费方)+ P2 防 article 删后断链。

用法:
  python scripts/backfill_funnel_publish_monitor_2026_05_29.py            # dry-run(BEGIN→DML→报行数→ROLLBACK · 0 写)
  python scripts/backfill_funnel_publish_monitor_2026_05_29.py --apply    # 真写(BEGIN→DML→COMMIT)

红线:不碰 billing/jwt/middleware/connection/geo_scope_scorer · 只 INSERT pipeline_stage_log + UPDATE mhz item brand_id。
"""
from __future__ import annotations

import argparse
import sys

# 1. publish stage_log 回填(已发布集 · 经 quote 链拿 brand)
SQL_PUBLISH_BACKFILL = """
INSERT INTO pipeline_stage_log (brand_id, stage_name, event, meta, actor_user_id, created_at)
SELECT q.brand_id, 'publish', 'complete',
       jsonb_build_object('source', 'mhz_backfill', 'item_id', i.id, 'media_name', i.media_name),
       i.user_id,
       COALESCE(i.published_at, i.created_at)
FROM mhz_publish_order_items i
JOIN mhz_publish_orders mo ON mo.id = i.order_id
JOIN articles a ON a.id = mo.article_id
JOIN quotes q ON q.id = a.quote_id
WHERE i.status = 'published'
  AND q.brand_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM pipeline_stage_log p
      WHERE p.stage_name = 'publish'
        AND p.meta->>'source' = 'mhz_backfill'
        AND p.meta->>'item_id' = i.id::text
  )
"""

# 2. monitor stage_log 回填(monitoring_tasks · 直接 brand_id)
SQL_MONITOR_BACKFILL = """
INSERT INTO pipeline_stage_log (brand_id, stage_name, event, meta, created_at)
SELECT mt.brand_id, 'monitor', 'complete',
       jsonb_build_object('source', 'monitor_backfill', 'task_id', mt.id),
       mt.created_at
FROM monitoring_tasks mt
WHERE mt.brand_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM pipeline_stage_log p
      WHERE p.stage_name = 'monitor'
        AND p.meta->>'source' = 'monitor_backfill'
        AND p.meta->>'task_id' = mt.id::text
  )
"""

# 3. P2 mhz item brand_id 回填(NULL → 经链反填)
SQL_ITEM_BRAND_BACKFILL = """
UPDATE mhz_publish_order_items i
SET brand_id = q.brand_id
FROM mhz_publish_orders mo, articles a, quotes q
WHERE i.order_id = mo.id
  AND mo.article_id = a.id
  AND a.quote_id = q.id
  AND i.brand_id IS NULL
  AND q.brand_id IS NOT NULL
"""

STEPS = [
    ("publish stage_log 回填", SQL_PUBLISH_BACKFILL),
    ("monitor stage_log 回填", SQL_MONITOR_BACKFILL),
    ("P2 mhz item.brand_id 回填", SQL_ITEM_BRAND_BACKFILL),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写(默认 dry-run · BEGIN→DML→ROLLBACK)")
    args = ap.parse_args()
    mode = "APPLY(真写)" if args.apply else "DRY-RUN(只数不写)"

    from db.connection import get_connection
    conn = get_connection()
    conn.autocommit = False
    cur = conn.cursor()

    print(f"=== 漏斗回填 {mode} · 2026-05-29 ===")
    results = []
    try:
        for label, sql in STEPS:
            cur.execute(sql)
            n = cur.rowcount
            results.append((label, n))
            print(f"  [{label}] 影响 {n} 行")
        if args.apply:
            conn.commit()
            print("=== 已 COMMIT ===")
        else:
            conn.rollback()
            print("=== DRY-RUN 已 ROLLBACK(0 写)· 确认行数合理后加 --apply ===")
    except Exception as e:
        conn.rollback()
        print(f"!!! 失败已 ROLLBACK: {e}", file=sys.stderr)
        return 1
    finally:
        cur.close()
        conn.close()

    print("--- 汇总 ---")
    for label, n in results:
        print(f"  {label}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
