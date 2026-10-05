"""
Backfill quotes.status / confirmed_at / paid_at / paid_amount

修复:富士电梯 quote 286 + quote 294 等写作大厅消失 bug(CTO-15.23 · 2026-05-09)

症状:
  selection_session.status = 'active' + payment_received_at 已写
  + confirmed_keywords 已生成
  + quotes.service_status = 'active'
  但 quotes.status 仍 'draft'(confirmed_at / paid_at / paid_amount 全 NULL)
  → get_writing_projects WHERE status IN ('confirmed','paid') 把它过滤
  → 写作大厅待办看不到该订单

根因:api/selection_api.py mark_paid 把 update_quote_status('confirmed')
     嵌在 keyword 同步大 try 块里 · 前置任意一步抛异常都让它跳过

代码 root cause 修复见同 commit 的 api/selection_api.py 改动
此脚本是 backfill 历史数据(漏推 quote.status 的全部修齐)

用法:
    docker exec omnirank-blue python /app/scripts/backfill_quote_status_2026_05_09.py --dry-run
    docker exec omnirank-blue python /app/scripts/backfill_quote_status_2026_05_09.py --execute
"""
import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("backfill_quote_status")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import get_connection


def find_orphan_quotes() -> list[dict]:
    """找出所有 selection_session.status='active' 但 quotes.status='draft' 的 quote"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT q.id AS quote_id,
                   q.brand_id,
                   q.brand_name,
                   q.status AS q_status,
                   q.confirmed_at AS q_confirmed_at,
                   q.paid_at AS q_paid_at,
                   q.paid_amount AS q_paid_amount,
                   s.status AS s_status,
                   s.sales_confirmed_at,
                   s.payment_received_at,
                   s.confirmed_total_price,
                   s.final_keyword_ids
              FROM quotes q
              JOIN keyword_selection_sessions s ON s.quote_id = q.id
             WHERE s.status = 'active'
               AND q.status = 'draft'
               AND s.payment_received_at IS NOT NULL
             ORDER BY q.id DESC
        """)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def backfill(execute: bool) -> int:
    rows = find_orphan_quotes()
    if not rows:
        logger.info("✅ 没有 orphan quote(全部 selection active 的 quote 已正确推到 confirmed/paid)")
        return 0

    logger.info(f"🔍 找到 {len(rows)} 个 orphan quote(selection active 但 quote draft):")
    for r in rows:
        logger.info(
            f"  quote_id={r['quote_id']} brand_id={r['brand_id']} brand={r['brand_name']!r} "
            f"sales_confirmed={r['sales_confirmed_at']} payment_received={r['payment_received_at']} "
            f"price={r['confirmed_total_price']}"
        )

    if not execute:
        logger.info("📋 dry-run 模式 · 未执行 UPDATE · 加 --execute 真改")
        return len(rows)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE quotes q
               SET status = 'paid',
                   confirmed_at = COALESCE(q.confirmed_at, s.sales_confirmed_at::timestamp),
                   paid_at = COALESCE(q.paid_at, s.payment_received_at::timestamp),
                   paid_amount = COALESCE(q.paid_amount, s.confirmed_total_price)
              FROM keyword_selection_sessions s
             WHERE s.quote_id = q.id
               AND s.status = 'active'
               AND q.status = 'draft'
               AND s.payment_received_at IS NOT NULL
        """)
        affected = cur.rowcount
        conn.commit()
        logger.info(f"✅ backfill 完成 · 影响 {affected} 行")
        cur.close()

        # 验证 · 应该 0 行 orphan 了
        verify = find_orphan_quotes()
        if verify:
            logger.warning(f"⚠️  还有 {len(verify)} 个 orphan · 可能 selection_session.payment_received_at 是 NULL · 需人工核查")
            for r in verify:
                logger.warning(f"  quote_id={r['quote_id']} payment_received={r['payment_received_at']}")
        else:
            logger.info("✅ 验证通过 · 全部 orphan 已修齐")
        return affected
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="dry-run · 只打印不真改")
    ap.add_argument("--execute", action="store_true", help="真执行 UPDATE")
    args = ap.parse_args()
    if not args.dry_run and not args.execute:
        ap.error("必须指定 --dry-run 或 --execute")
    if args.dry_run and args.execute:
        ap.error("--dry-run 和 --execute 互斥")

    affected = backfill(execute=args.execute)
    logger.info(f"affected_rows={affected}")
    sys.exit(0)


if __name__ == "__main__":
    main()
