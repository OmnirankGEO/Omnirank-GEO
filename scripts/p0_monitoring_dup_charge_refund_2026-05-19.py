"""
[CTO-15.23 2026-05-19 P0] monitoring_keyword_daily 重复扣费退款 backfill

【现象】
老板 2026-05-19 截图实证:同 1 keyword 同时间被扣 4 次 130 积分(09:12-09:13)
客户原话"只监控了一个词 · 但是每天扣 4 次费用 · 在同一个时间段"

【根因】
- 蓝绿双 container(omnirank-blue + green)各跑 1 个根 scheduler.py BackgroundScheduler
- 加上 api/scheduler.py 第二个 BackgroundScheduler · 各 container 内可能 register 多次
- 09:00 cron 同时触发 daily_monitoring · 同 1 subscription 在 race 内被 charge N 次
- daily_monitoring 写 feature_code='monitoring_keyword_daily' · description='关键词每日监测'

【治本(已修)】
- scheduler.py _process_keyword 加 claim_subscription_for_today guard
- DB 层 INTERVAL '23 hours' atomic UPDATE WHERE 防 race
- 不管几个 instance/cron 同时触发 · 23h 内只 1 个 claim 成功 · 其他跳过

【退款(本脚本)】
- 扫 point_transactions feature_code='monitoring_keyword_daily' type='consume'
- 按 (user_id, charge_date_beijing) group · COUNT 多于当前 active subscription 数 = 多扣
- 退多扣的(实际 - 期望)× 130 积分 到 bonus_points(非 paid · 误扣不算用户主动消费)

【注意】
- 期望 charge 数取 *当前* active subscription · 不够精确(用户中途加/删订阅)
- 真精确要扫 enabled_at/cancelled_at 历史 · 这里保守取 max 多扣
- dry-run 默认 · --apply 才真退

【用法】
  # SSH 上 prod blue/green 任一容器
  docker exec -it omnirank-blue python /app/scripts/p0_monitoring_dup_charge_refund_2026-05-19.py
  docker exec -it omnirank-blue python /app/scripts/p0_monitoring_dup_charge_refund_2026-05-19.py --apply
"""

import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

# allow import db/* from sibling
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.monitoring_db import get_connection


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='真退分(否则 dry-run)')
    parser.add_argument('--since', default='2026-05-09', help='扫描起始日期 (default 2026-05-09 · daily 上线日)')
    parser.add_argument('--until', default=None, help='扫描截止日期(默认今天)')
    args = parser.parse_args()

    until = args.until or datetime.now().strftime('%Y-%m-%d')

    conn = get_connection()
    cur = conn.cursor()

    # Step 1: 扫每日多次扣费的 user
    cur.execute("""
        SELECT user_id,
               DATE(created_at AT TIME ZONE 'Asia/Shanghai') AS charge_date,
               COUNT(*) AS actual_charges,
               SUM(ABS(amount)) AS total_amount
        FROM point_transactions
        WHERE feature_code = 'monitoring_keyword_daily'
          AND type = 'consume'
          AND created_at >= %s::date
          AND created_at < (%s::date + INTERVAL '1 day')
        GROUP BY user_id, DATE(created_at AT TIME ZONE 'Asia/Shanghai')
        HAVING COUNT(*) > 0
        ORDER BY user_id, charge_date
    """, (args.since, until))

    rows = cur.fetchall()
    if not rows:
        print(f"[INFO] {args.since} ~ {until} 期间无 monitoring_keyword_daily 扣费记录")
        conn.close()
        return

    # Step 2: 每 user 期望 charge 数 = 当前 active subscription count
    cur.execute("""
        SELECT user_id, COUNT(*) AS active_kw_count
        FROM keyword_monitor_subscriptions
        WHERE status IN ('active', 'paused_low_balance')
        GROUP BY user_id
    """)
    user_active = {r['user_id']: r['active_kw_count'] for r in cur.fetchall()}

    total_refund_amount = 0
    affected_users = set()
    rows_to_refund = []

    print(f"\n{'user_id':>8} {'date':>12} {'expected':>9} {'actual':>7} {'extra':>6} {'refund':>8}")
    print("-" * 60)

    for row in rows:
        uid = row['user_id']
        date = row['charge_date']
        actual = row['actual_charges']
        expected = user_active.get(uid, 1)
        if actual > expected:
            extra = actual - expected
            refund = extra * 130
            total_refund_amount += refund
            affected_users.add(uid)
            rows_to_refund.append({
                'user_id': uid, 'date': date, 'expected': expected,
                'actual': actual, 'extra': extra, 'refund': refund,
            })
            print(f"{uid:>8} {date.strftime('%Y-%m-%d'):>12} {expected:>9} {actual:>7} {extra:>6} {refund:>8}")

    print("-" * 60)
    print(f"\n总影响 user: {len(affected_users)}")
    print(f"总退款积分: {total_refund_amount}(¥{total_refund_amount/130:.2f})\n")

    if not args.apply:
        print("[DRY-RUN] 未真退分 · 复检 OK 后加 --apply 真退")
        conn.close()
        return

    # Step 3: --apply 真退分到 bonus_points
    print(f"[APPLY] 开始退款 {len(rows_to_refund)} 条记录...")
    for entry in rows_to_refund:
        cur.execute("""
            UPDATE user_wallets
            SET bonus_points = COALESCE(bonus_points, 0) + %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            RETURNING bonus_points
        """, (entry['refund'], entry['user_id']))
        wallet_row = cur.fetchone()
        if not wallet_row:
            print(f"  ⚠️ user#{entry['user_id']} 无 wallet · 跳过")
            continue
        new_bal = wallet_row['bonus_points']

        cur.execute("""
            INSERT INTO point_transactions
                (user_id, type, point_type, amount, balance_after, feature_code, description, created_at)
            VALUES (%s, 'refund', 'bonus', %s, %s, 'monitoring_keyword_daily', %s, CURRENT_TIMESTAMP)
        """, (
            entry['user_id'],
            entry['refund'],
            new_bal,
            f"P0 退款 · 蓝绿+多scheduler 重复扣 {entry['date']} 多 {entry['extra']} 次 ({entry['refund']}/¥{entry['refund']/130:.2f})"
        ))
        conn.commit()
        print(f"  退 user#{entry['user_id']} {entry['refund']}积分 · 新 bonus = {new_bal}")

    print(f"\n✅ 退款完成 · 总 {total_refund_amount} 积分(¥{total_refund_amount/130:.2f}) 已退到 bonus_points")
    conn.close()


if __name__ == '__main__':
    main()
