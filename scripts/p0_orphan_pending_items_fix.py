"""[CTO-15.23 2026-05-19 P0 BUG · 孤儿 pending items 一次性根治]

老板报"今日看板用户已经被拒稿了 · 但是看板这里还是准备中"
SSH 调研根因:db/meijiehezi_db.py:try_lock_for_submit attempts>=max 时返 False 但不标 failed ·
孤儿永远卡 pending + mhz_order_id=NULL · 实际媒介盒子那边已经拒了用户但 DB 里永远显示"准备中"。

本 commit 已修 try_lock_for_submit 加 fail-safe(未来再 lock 时会自动转 failed + 退款) ·
本脚本一次性修历史已有的孤儿 items(SSH 实证 5 个 · 跨多个用户 · 跨 5-09 ~ 5-14)。

工作原理:
  - 扫 mhz_publish_order_items WHERE status='pending' AND mhz_order_id IS NULL
    AND submit_attempts >= max_attempts AND last_submit_at < NOW() - 1h(防误伤刚提交的)
  - 对每条 UPDATE status='failed' + reject_reason + 调 refund_for_publish_order 退款
  - dry-run 模式仅扫不写 · 必须 --apply 才真改

用法(Deploy-CTO SSH 上 prod 跑):
  # 1. 备份
  docker exec omnirank-db pg_dump -U geo_admin -t mhz_publish_order_items geo_agentscope \\
    | gzip > /backup/items_pre_orphan_fix_$(date +%Y%m%d_%H%M).sql.gz
  # 2. dry-run(看影响范围 + 退款额)
  docker cp scripts/p0_orphan_pending_items_fix.py omnirank-blue:/tmp/
  docker exec omnirank-blue python /tmp/p0_orphan_pending_items_fix.py
  # 3. 老板批后 apply
  docker exec omnirank-blue python /tmp/p0_orphan_pending_items_fix.py --apply

安全:
  - dry-run 默认 · 必须显式 --apply 才写 DB
  - WHERE 严格(status='pending' AND mhz_order_id IS NULL AND attempts>=3 AND last_submit_at 老于 1h)防误伤
  - 退款幂等键 'item:{id}' 跟 try_lock_for_submit fail-safe / release_submit_lock / scheduler 一致
    重复跑 / 跟其他路径并发也不会双重退款
"""

import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description="P0 孤儿 pending items 一次性修复")
    parser.add_argument("--apply", action="store_true", help="真改 DB(默认 dry-run)")
    parser.add_argument("--max-attempts", type=int, default=3, help="重试上限(默认 3)")
    parser.add_argument("--min-stale-minutes", type=int, default=60,
                       help="last_submit_at 至少多久之前(默认 60min · 防误伤刚提交的)")
    args = parser.parse_args()

    from datetime import datetime, timedelta
    from db.connection import get_connection
    from db.meijiehezi_db import refund_for_publish_order

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n{'='*70}")
    print(f"P0 孤儿 pending items 修复脚本 · 模式={mode}")
    print(f"  max_attempts={args.max_attempts} · min_stale_minutes={args.min_stale_minutes}")
    print(f"{'='*70}")

    conn = get_connection()
    try:
        cur = conn.cursor()

        # 1. 扫候选孤儿:status=pending + mhz_order_id NULL + attempts>=max + 老于 X 分钟
        # [P0 BUG 6 教训 2026-05-18] 用 Python 端算 cutoff datetime 防字符串字面量内 placeholder 不替换坑
        stale_cutoff = datetime.now() - timedelta(minutes=args.min_stale_minutes)
        candidate_sql = """
            SELECT id, user_id, order_id, media_name, cost_points, cost_yuan,
                   submit_attempts, last_submit_at, created_at, reject_reason
            FROM mhz_publish_order_items
            WHERE status = 'pending'
              AND mhz_order_id IS NULL
              AND COALESCE(submit_attempts, 0) >= %s
              AND (last_submit_at IS NULL OR last_submit_at < %s)
            ORDER BY id ASC
        """
        cur.execute(candidate_sql, (args.max_attempts, stale_cutoff))
        orphans = cur.fetchall()
        total = len(orphans)
        print(f"\n[扫描] 候选孤儿 {total} 条")

        if not orphans:
            print("[结束] 0 条孤儿 · 退出")
            return

        # 2. 抽样展示
        print(f"\n[抽样] 前 10 条孤儿详情(等长 list):")
        total_refund_points = 0
        for r in orphans[:10]:
            print(f"  item={r['id']} order={r['order_id']} user={r['user_id']} "
                  f"media={r['media_name'][:20]!r} cost={r['cost_points']} pts "
                  f"attempts={r['submit_attempts']} last_submit={r['last_submit_at']} "
                  f"created={r['created_at']}")

        # 3. 退款额汇总
        for r in orphans:
            total_refund_points += int(r['cost_points'] or 0)
        print(f"\n[汇总] 退款总额 = {total_refund_points} 积分(≈ ¥{total_refund_points/130:.2f})")
        print(f"        涉及 user {len(set(r['user_id'] for r in orphans))} 人 · "
              f"涉及 order {len(set(r['order_id'] for r in orphans))} 单")

        # 4. dry-run 或真改
        if not args.apply:
            print(f"\n[DRY-RUN] 跳过 UPDATE + 退款 · 将处理 {total} 条孤儿")
            print(f"            如需真改:加 --apply(必须先 pg_dump 备份)")
            return

        print(f"\n[APPLY] 即将处理 {total} 条孤儿 · 5 秒后开始...")
        import time
        time.sleep(5)

        # 5. 逐条处理(分别更新 + 退款 · 防全表锁)
        success_count = 0
        refund_success = 0
        refund_failed = 0
        for r in orphans:
            item_id = int(r['id'])
            user_id = int(r['user_id']) if r['user_id'] else 0
            cost = int(r['cost_points'] or 0)

            try:
                # 5a. UPDATE status → failed
                cur.execute("""
                    UPDATE mhz_publish_order_items
                    SET status = 'failed',
                        reject_reason = COALESCE(NULLIF(reject_reason, ''),
                                                  '提交次数耗尽 · P0 孤儿一次性修复脚本自动标')
                    WHERE id = %s
                      AND status = 'pending'
                      AND mhz_order_id IS NULL
                """, (item_id,))
                affected = cur.rowcount
                conn.commit()

                if affected > 0:
                    success_count += 1
                    # 5b. 退款(幂等键 item:{id} · 跟 try_lock_for_submit / release_submit_lock 一致)
                    if cost > 0 and user_id > 0:
                        try:
                            refund_for_publish_order(
                                user_id=user_id,
                                amount=cost,
                                refund_key=f"item:{item_id}",
                                reason="孤儿 pending items P0 修复:提交次数耗尽自动退款",
                            )
                            refund_success += 1
                        except Exception as e:
                            refund_failed += 1
                            print(f"  [退款失败] item={item_id} user={user_id} cost={cost}: {e}")
            except Exception as e:
                print(f"  [UPDATE 失败] item={item_id}: {e}")
                conn.rollback()

        print(f"\n[完成]")
        print(f"  status 标 failed:{success_count}/{total}")
        print(f"  退款成功:{refund_success}")
        print(f"  退款失败:{refund_failed}(可后续手动重试 refund_for_publish_order item:{{id}})")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
