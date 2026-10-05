"""
Ops · 监测误扣退款 + 通知 (CTO-15.23 2026-05-05 P0 配套)

背景:
  老板反馈用户没发文,但 _hourly_check 仍跑监测扣费(每词 38 积分/次)
  effe7942 已修守卫(未发文不进 to_run) · 但已扣的积分需要退还 + 通知用户

本脚本执行 3 个动作:
  1. 暂停所有"未发文 + monitoring_enabled=TRUE"的 quote(L3-2)
  2. 退还过去 30 天 scheduled_monitoring 误扣积分到 bonus_points(L3-3)
  3. 给每个受影响用户发 IMPORTANT 站内通知(标 wallet · 跳 /wallet)

安全:
  默认 --dry-run (只 print 不改 DB) · 必须显式加 --apply 才真执行
  --apply 时所有写操作放同一个 transaction · 任一步失败整体 rollback

运行:
  # 先看会改什么(dry-run)
  docker exec omnirank-ai python3 /app/scripts/ops_refund_monitoring_2026_05_05.py

  # 确认后真执行
  docker exec omnirank-ai python3 /app/scripts/ops_refund_monitoring_2026_05_05.py --apply

  # 仅退款不发通知(慎用)
  docker exec omnirank-ai python3 /app/scripts/ops_refund_monitoring_2026_05_05.py --apply --no-notify
"""
import argparse
import sys

sys.path.insert(0, '/app')

# [CTO-15.23 2026-05-05 fix] 用 db.connection 而非 db.diagnosis_db
# diagnosis_db.get_connection 是 legacy SQLite 时代的 wrapper(内部转发到 db.connection)
# 但导入 diagnosis_db 会触发模块级 SQLite path 残留(`DB_PATH = Path(__file__).parent / "geo_diagnosis.db"`)
# 直接用 db.connection 干净 · 避免无关副作用
from db.connection import get_connection


# ============================================
# Step 1 · 排查受影响 quote
# ============================================
def find_affected_quotes(cur) -> list[dict]:
    """找 monitoring_enabled=TRUE 但 0 篇 published article 的 quote"""
    cur.execute("""
        SELECT q.id AS quote_id, q.brand_id, q.brand_name,
               b.owner_user_id, q.monthly_price,
               (SELECT COUNT(*) FROM confirmed_keywords ck
                WHERE ck.quote_id = q.id AND ck.is_core IS NOT FALSE) AS kw_count
        FROM quotes q
        JOIN brands b ON b.id = q.brand_id
        WHERE q.monitoring_enabled = TRUE
          AND NOT EXISTS (
              SELECT 1 FROM articles a
              WHERE a.quote_id = q.id AND a.first_published_at IS NOT NULL
          )
        ORDER BY q.id
    """)
    return [dict(r) for r in cur.fetchall()]


# ============================================
# Step 2 · 排查待退款 transaction
# ============================================
def find_misCharge_transactions(cur, days: int = 30) -> list[dict]:
    """
    找过去 N 天 feature_code='scheduled_monitoring' 的扣费,
    但发生时该 brand 还没有任何 published article → 误扣
    """
    cur.execute("""
        SELECT pt.id, pt.user_id, pt.brand_id, pt.amount, pt.created_at,
               b.name AS brand_name
        FROM point_transactions pt
        LEFT JOIN brands b ON b.id = pt.brand_id
        WHERE pt.feature_code = 'scheduled_monitoring'
          AND pt.type = 'consume'
          AND pt.created_at >= NOW() - INTERVAL '%s days'
          AND NOT EXISTS (
              SELECT 1 FROM articles a
              JOIN quotes q ON q.id = a.quote_id
              WHERE q.brand_id = pt.brand_id
                AND a.first_published_at IS NOT NULL
                AND a.first_published_at < pt.created_at
          )
        ORDER BY pt.user_id, pt.created_at
    """, (days,))
    return [dict(r) for r in cur.fetchall()]


def aggregate_refund_by_user(transactions: list[dict]) -> dict[int, dict]:
    """按 user 聚合退款金额"""
    by_user: dict[int, dict] = {}
    for tx in transactions:
        uid = tx["user_id"]
        if uid not in by_user:
            by_user[uid] = {
                "user_id": uid,
                "total_amount": 0,
                "tx_count": 0,
                "brands": set(),
            }
        by_user[uid]["total_amount"] += abs(int(tx["amount"]))
        by_user[uid]["tx_count"] += 1
        if tx.get("brand_name"):
            by_user[uid]["brands"].add(tx["brand_name"])
    return by_user


# ============================================
# Step 3 · 执行 (apply 模式)
# ============================================
def pause_monitoring_for_quotes(cur, quote_ids: list[int]) -> int:
    """L3-2 · 暂停未发文 quote 的 monitoring_enabled"""
    if not quote_ids:
        return 0
    cur.execute("""
        UPDATE quotes
        SET monitoring_enabled = FALSE,
            monitoring_paused_reason = '系统检测到未发文,自动暂停 (CTO-15.23 2026-05-05 修)'
        WHERE id = ANY(%s)
    """, (quote_ids,))
    return cur.rowcount


def refund_user(cur, user_id: int, amount: int, reason: str) -> int:
    """
    L3-3 · 退款到 bonus_points + 写 point_transactions
    返回新 transaction id
    """
    # 1. 加到 bonus_points
    cur.execute("""
        UPDATE user_wallets
        SET bonus_points = bonus_points + %s,
            updated_at = NOW()
        WHERE user_id = %s
        RETURNING bonus_points + paid_points + commission_points AS total_after
    """, (amount, user_id))
    row = cur.fetchone()
    balance_after = int(row["total_after"]) if row else amount

    # 2. 写 point_transactions
    cur.execute("""
        INSERT INTO point_transactions
            (user_id, type, point_type, amount, balance_after,
             feature_code, description, created_at)
        VALUES (%s, 'refund', 'bonus_points', %s, %s,
                'scheduled_monitoring_refund', %s, NOW())
        RETURNING id
    """, (user_id, amount, balance_after, reason))
    return cur.fetchone()["id"]


def send_user_notification(user_id: int, refund_amount: int, brand_count: int):
    """L3-4 · 给受影响用户发站内通知(IMPORTANT · 跳 /wallet)"""
    from utils.notify import notify_user, LEVEL_IMPORTANT

    title = f"已退还误扣监测积分 {refund_amount}"
    if brand_count > 1:
        content = (
            f"因您 {brand_count} 个品牌尚未发布文章但监测被自动运行(系统问题),"
            f"已暂停监测开关并退还 {refund_amount} 积分到您的赠送积分。\n"
            f"修复后:发布第 1 篇文章后系统才会自动监测,在'监测设置'可手动关闭。"
        )
    else:
        content = (
            f"因您尚未发布文章但监测被自动运行(系统问题),"
            f"已暂停监测开关并退还 {refund_amount} 积分到您的赠送积分。\n"
            f"修复后:发布第 1 篇文章后系统才会自动监测,在'监测设置'可手动关闭。"
        )
    notify_user(
        user_id=user_id,
        title=title,
        content=content,
        level=LEVEL_IMPORTANT,
        type="wallet",
        link="/wallet",
    )


# ============================================
# Main
# ============================================
def main():
    parser = argparse.ArgumentParser(description="监测误扣退款 + 通知 ops 脚本")
    parser.add_argument("--apply", action="store_true",
                        help="真执行(否则只 dry-run)")
    parser.add_argument("--no-notify", action="store_true",
                        help="退款但不发通知(默认会发)")
    parser.add_argument("--days", type=int, default=30,
                        help="排查过去 N 天的扣费(默认 30)")
    args = parser.parse_args()

    print("=" * 80)
    print(f"监测误扣退款 + 通知 · {'APPLY 模式 (真改 DB)' if args.apply else 'DRY-RUN (只 print)'}")
    print(f"排查窗口: 过去 {args.days} 天")
    print("=" * 80)

    conn = get_connection()
    cur = conn.cursor()

    # ===== Step 1: 排查受影响 quote =====
    affected_quotes = find_affected_quotes(cur)
    print(f"\n[1/4] 受影响的 quote (monitoring=TRUE 但 0 篇 published):")
    print(f"  共 {len(affected_quotes)} 个 quote")
    if affected_quotes:
        print(f"  {'quote_id':<10} {'brand_id':<10} {'kw_count':<10} brand_name")
        for q in affected_quotes[:20]:
            print(f"  {q['quote_id']:<10} {q['brand_id']:<10} {q['kw_count']:<10} {q['brand_name'][:40]}")
        if len(affected_quotes) > 20:
            print(f"  ... 还有 {len(affected_quotes) - 20} 个")

    # ===== Step 2: 排查待退款 transaction =====
    misCharges = find_misCharge_transactions(cur, args.days)
    by_user = aggregate_refund_by_user(misCharges)
    total_refund_amount = sum(u["total_amount"] for u in by_user.values())
    total_users = len(by_user)
    total_tx = len(misCharges)

    print(f"\n[2/4] 待退款 transaction (过去 {args.days} 天):")
    print(f"  共 {total_tx} 笔扣费 · {total_users} 个用户 · 共 {total_refund_amount} 积分待退")
    if by_user:
        print(f"  {'user_id':<10} {'退款额':<10} {'笔数':<6} 品牌")
        for u in sorted(by_user.values(), key=lambda x: -x["total_amount"])[:20]:
            brands = ', '.join(list(u["brands"])[:3])
            if len(u["brands"]) > 3:
                brands += f" 等{len(u['brands'])}个"
            print(f"  {u['user_id']:<10} {u['total_amount']:<10} {u['tx_count']:<6} {brands}")
        if len(by_user) > 20:
            print(f"  ... 还有 {len(by_user) - 20} 个用户")

    # ===== Step 3: APPLY 时真执行 =====
    if not args.apply:
        print("\n[3/4] DRY-RUN · 不改 DB · 加 --apply 真执行")
        print("\n[4/4] 跳过通知(dry-run)")
        conn.close()
        print("\n" + "=" * 80)
        print(f"DRY-RUN 完成 · 真执行命令: docker exec omnirank-ai python3 /app/scripts/ops_refund_monitoring_2026_05_05.py --apply")
        print("=" * 80)
        return 0

    # 真执行 · 单事务
    print(f"\n[3/4] APPLY · 暂停 monitoring + 退款...")
    quote_ids = [q["quote_id"] for q in affected_quotes]

    # L3-2 暂停
    paused = pause_monitoring_for_quotes(cur, quote_ids)
    print(f"  ✅ 已暂停 {paused} 个 quote 的 monitoring_enabled")

    # L3-3 退款
    refund_count = 0
    for u in by_user.values():
        try:
            refund_user(
                cur,
                u["user_id"],
                u["total_amount"],
                f"监测误扣退款 ({u['tx_count']} 笔 · CTO-15.23 修复)",
            )
            refund_count += 1
        except Exception as e:
            print(f"  ⚠️  user_id={u['user_id']} 退款失败: {e}")
            conn.rollback()
            print("\n❌ ROLLBACK · 没改任何 DB · 修复脚本后重试")
            return 1

    conn.commit()
    print(f"  ✅ 已退款 {refund_count} 个用户 · 共 {total_refund_amount} 积分")

    # ===== Step 4: 发通知 =====
    if args.no_notify:
        print("\n[4/4] --no-notify · 跳过通知")
    else:
        print(f"\n[4/4] 发通知给 {total_users} 个用户...")
        notify_count = 0
        for u in by_user.values():
            try:
                send_user_notification(u["user_id"], u["total_amount"], len(u["brands"]))
                notify_count += 1
            except Exception as e:
                print(f"  ⚠️  user_id={u['user_id']} 通知失败(不影响退款): {e}")
        print(f"  ✅ 已发送 {notify_count}/{total_users} 个通知")

    conn.close()

    print("\n" + "=" * 80)
    print(f"APPLY 完成:")
    print(f"  · 暂停 monitoring: {paused} 个 quote")
    print(f"  · 退款: {refund_count} 个用户 · {total_refund_amount} 积分")
    print(f"  · 通知: {'跳过' if args.no_notify else f'{notify_count}/{total_users} 已发'}")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
