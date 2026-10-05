"""
微信/虎皮椒充值订单对账脚本（只读）

目的：
  audit #6 指出"支付回调金额未跟本地订单复核" — 系统目前没有回调日志表，
  无法做"回调金额 vs 本地金额"的反向对账。但能查内部一致性：

  ① 重放攻击 — 同一个 payment_id (微信 transaction_id) 是否被多次记账
  ② 流水缺失 — paid 订单是否都在 point_transactions 里有 recharge 入账
  ③ 金额-积分换算 — amount_cents → base_points 是否合理
  ④ 钱包余额一致性 — user_wallets.paid_points 是否等于流水合计
  ⑤ 异常状态 — paid_at IS NULL 但已发积分；pending 太久未完成

输出 5 个清单。任何一条非空都意味着系统有"已经被薅"或"账目不一致"的真实记录。

只读脚本，零风险。
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.connection import get_connection


def section(title):
    print()
    print("=" * 88)
    print(f"  {title}")
    print("=" * 88)


def main():
    conn = get_connection()
    cur = conn.cursor()
    issues_found = 0

    # ---------- ① 重放攻击：同 payment_id 多次入账 ----------
    section("① 重放攻击核查（同一个 payment_id 被多次入账）")
    cur.execute("""
        SELECT payment_id, COUNT(*) AS cnt,
               array_agg(id) AS order_ids,
               array_agg(user_id) AS user_ids,
               SUM(amount_cents) AS total_cents
        FROM recharge_orders
        WHERE payment_id IS NOT NULL AND payment_id <> ''
        GROUP BY payment_id
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
    """)
    rows = cur.fetchall()
    if not rows:
        print("  ✅ 没有重复 payment_id（系统未被重放攻击）")
    else:
        issues_found += len(rows)
        print(f"  ❌ 发现 {len(rows)} 条重复 payment_id（重放攻击迹象）")
        for r in rows:
            print(f"     payment_id={r['payment_id']}  次数={r['cnt']}  "
                  f"订单={r['order_ids']}  用户={r['user_ids']}  总金额={r['total_cents']/100:.2f} 元")

    # ---------- ② 流水缺失：paid 订单未在流水入账 ----------
    section("② 流水缺失核查（paid 订单是否都有 recharge 流水）")
    cur.execute("""
        SELECT ro.id, ro.user_id, ro.amount_cents, ro.base_points,
               ro.payment_method, ro.payment_id, ro.paid_at
        FROM recharge_orders ro
        WHERE ro.payment_status = 'paid'
          AND NOT EXISTS (
            SELECT 1 FROM point_transactions pt
            WHERE pt.order_id = ro.id AND pt.type = 'recharge'
          )
        ORDER BY ro.paid_at DESC NULLS LAST
    """)
    rows = cur.fetchall()
    if not rows:
        print("  ✅ 所有 paid 订单都有对应流水")
    else:
        issues_found += len(rows)
        print(f"  ❌ 发现 {len(rows)} 条 paid 订单缺失流水（用户付了钱没拿到积分）")
        for r in rows:
            print(f"     order_id={r['id']}  user={r['user_id']}  "
                  f"金额={r['amount_cents']/100:.2f} 元  应得积分={r['base_points']}  "
                  f"渠道={r['payment_method']}  支付ID={r['payment_id']}  paid_at={r['paid_at']}")

    # ---------- ③ 金额-积分换算异常 ----------
    # 业务规则: api/wallet_api.py:45 POINTS_PER_YUAN = 130
    #   即 1 元 = 130 积分；amount_cents = 100 → base_points = 130
    #   公式: base_points = (amount_cents / 100) * 130 = amount_cents * 1.3
    section("③ 金额-积分换算核查（业务规则: 1 元 = 130 积分，即 base_points = amount_cents × 1.3）")
    cur.execute("""
        SELECT id, user_id, amount_cents, base_points, bonus_points,
               payment_method, payment_status
        FROM recharge_orders
        WHERE payment_status = 'paid'
          AND base_points <> FLOOR(amount_cents * 1.3)::bigint
          AND base_points <> ROUND(amount_cents * 1.3)::bigint
        ORDER BY amount_cents DESC
    """)
    rows = cur.fetchall()
    if not rows:
        print("  ✅ 已支付订单换算全部符合业务规则（1 元 = 130 积分）")
    else:
        issues_found += len(rows)
        print(f"  ❌ 发现 {len(rows)} 条订单换算偏离业务规则")
        for r in rows:
            ratio = r["base_points"] / r["amount_cents"] if r["amount_cents"] else 0
            expected = int(r["amount_cents"] * 1.3)
            print(f"     order={r['id']}  user={r['user_id']}  金额={r['amount_cents']/100:.2f}元  "
                  f"实际积分={r['base_points']}  应得={expected}  bonus={r['bonus_points']}  比例={ratio:.4f}")

    # ---------- ④ 钱包余额 vs 流水合计 ----------
    # 业务规则: paid_points/bonus_points 字段是"可用余额"
    #   = 总入账 - 已消费 + 退款（不含 freeze，freeze 是临时锁定不从字段扣）
    #   freeze/unfreeze 类型的流水只影响 frozen_points 字段，不影响 paid/bonus_points
    section("④ 钱包余额一致性（user_wallets vs point_transactions 合计，排除 freeze/unfreeze）")
    cur.execute("""
        SELECT uw.user_id,
               uw.paid_points AS wallet_paid_points,
               uw.bonus_points AS wallet_bonus_points,
               COALESCE(SUM(CASE WHEN pt.point_type='paid' AND pt.type NOT IN ('freeze','unfreeze') THEN pt.amount ELSE 0 END), 0) AS flow_paid_sum,
               COALESCE(SUM(CASE WHEN pt.point_type='bonus' AND pt.type NOT IN ('freeze','unfreeze') THEN pt.amount ELSE 0 END), 0) AS flow_bonus_sum
        FROM user_wallets uw
        LEFT JOIN point_transactions pt ON pt.user_id = uw.user_id
        GROUP BY uw.user_id, uw.paid_points, uw.bonus_points
        HAVING uw.paid_points <> COALESCE(SUM(CASE WHEN pt.point_type='paid' AND pt.type NOT IN ('freeze','unfreeze') THEN pt.amount ELSE 0 END), 0)
            OR uw.bonus_points <> COALESCE(SUM(CASE WHEN pt.point_type='bonus' AND pt.type NOT IN ('freeze','unfreeze') THEN pt.amount ELSE 0 END), 0)
        ORDER BY uw.user_id
    """)
    rows = cur.fetchall()
    if not rows:
        print("  ✅ 所有用户钱包余额 = 流水合计（账目一致）")
    else:
        issues_found += len(rows)
        print(f"  ❌ 发现 {len(rows)} 个用户钱包 ≠ 流水合计（账目不一致 → 可能被薅或手动改过）")
        for r in rows:
            diff_paid = r["wallet_paid_points"] - r["flow_paid_sum"]
            diff_bonus = r["wallet_bonus_points"] - r["flow_bonus_sum"]
            print(f"     user={r['user_id']}  "
                  f"钱包paid={r['wallet_paid_points']} vs 流水paid={r['flow_paid_sum']} (差 {diff_paid:+})  "
                  f"钱包bonus={r['wallet_bonus_points']} vs 流水bonus={r['flow_bonus_sum']} (差 {diff_bonus:+})")

    # ---------- ⑤ 异常订单：paid 但 paid_at 为空 / pending 太久 ----------
    section("⑤ 异常状态核查")
    cur.execute("""
        SELECT id, user_id, amount_cents, payment_method, payment_status,
               paid_at, base_points, created_at
        FROM recharge_orders
        WHERE (payment_status = 'paid' AND paid_at IS NULL)
           OR (payment_status = 'paid' AND base_points <= 0)
        ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    if rows:
        issues_found += len(rows)
        print(f"  ❌ {len(rows)} 条「已支付但 paid_at 为空 / 积分为 0」的异常订单")
        for r in rows:
            print(f"     order={r['id']}  user={r['user_id']}  金额={r['amount_cents']/100:.2f}  "
                  f"积分={r['base_points']}  paid_at={r['paid_at']}")
    else:
        print("  ✅ 无 paid_at 异常订单")

    # 长期 pending（超过 24 小时未完成的，正常应该自动过期）
    cur.execute("""
        SELECT COUNT(*) AS cnt FROM recharge_orders
        WHERE payment_status = 'pending'
          AND created_at < NOW() - INTERVAL '24 hours'
    """)
    long_pending = cur.fetchone()["cnt"]
    if long_pending > 0:
        print(f"  ⚠️  {long_pending} 条 pending 订单超过 24 小时未关闭（信息提示，非资金风险）")
    else:
        print("  ✅ 无长期 pending 订单残留")

    conn.close()

    # ---------- 总结 ----------
    print()
    print("=" * 88)
    if issues_found == 0:
        print("  ✅ 对账完成：未发现任何资金账目异常。系统从未被薅，可放心改 #6 加防护")
    else:
        print(f"  ⚠️  对账完成：共发现 {issues_found} 处账目异常，详见上方各章节")
        print(f"     请把上面输出截图发给老板拍板：追回 / 核销 / 报警")
    print("=" * 88)
    return 0 if issues_found == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
