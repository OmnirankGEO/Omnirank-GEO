"""v35 退款单账本接线 · 夹具形状构造(与 conftest 分开,避免与 tests/conftest.py 撞名)

🔴 零生产数据:全部 uid 用 9_1xx_xxx 区段自造,与生产 uid 18/93/103/127/149 **无交集**。
   夹具**复刻的是形状**(paid v35 订单 + 停写表清零残行 + user_wallets 有余额),
   不是复制生产的人和钱。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="需 TEST_DATABASE_URL")

# 自造 uid 区段 —— 与生产真实账户零交集
CUSTOMER = 9_100_017
AGENT = 9_100_018
CUSTOMER_NOROW = 9_100_019   # 停写表【无】残行的对照客户
ORDER_ID = "TESTV35ORDER0817A"
ORDER_ID_SPENT = "TESTV35ORDER0817B"


def _wipe(cur):
    """🔴 删除顺序 = FK 依赖的逆拓扑序:先删引用 users 的子表,最后才删 users。
    (顺序写反会撞 recharge_orders_user_id_fkey —— 上一个用例 commit 过就必炸)"""
    uids = (CUSTOMER, AGENT, CUSTOMER_NOROW)
    # ① 先清引用 users 的业务表
    cur.execute("DELETE FROM agent_revenue_ledger WHERE recharge_order_id = ANY(%s) OR agent_user_id = ANY(%s) "
                "OR customer_user_id = ANY(%s)",
                ([ORDER_ID, ORDER_ID_SPENT], list(uids), list(uids)))
    cur.execute("DELETE FROM recharge_orders WHERE id = ANY(%s) OR user_id = ANY(%s)",
                ([ORDER_ID, ORDER_ID_SPENT], list(uids)))
    cur.execute("DELETE FROM agent_inventory_transactions WHERE agent_user_id = ANY(%s)", (list(uids),))
    cur.execute("DELETE FROM agent_inventory_wallets WHERE agent_user_id = ANY(%s)", (list(uids),))
    for uid in uids:
        cur.execute("DELETE FROM customer_credit_transactions WHERE customer_user_id=%s", (uid,))
        cur.execute("DELETE FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (uid,))
        cur.execute("DELETE FROM point_transactions WHERE user_id=%s", (uid,))
        cur.execute("DELETE FROM user_wallets WHERE user_id=%s", (uid,))
        cur.execute("DELETE FROM customer_agent_bindings WHERE customer_user_id=%s OR agent_user_id=%s", (uid, uid))
    # ② 最后删 users
    for uid in uids:
        cur.execute("DELETE FROM users WHERE id=%s", (uid,))


def seed_exposure_shape(
    cur,
    *,
    customer=CUSTOMER,
    order_id=ORDER_ID,
    allocated=4160,
    consumed=0,
    wallet_paid=None,
    wallet_bonus=0,
    with_dead_row=True,
    ledger_status="frozen",
):
    """复刻生产暴露面的**形状**:

    · recharge_orders 一张 paid / settlement_mode='v35_inventory_settlement' / 未退款
    · customer_agent_credit_wallets **残行且三池为 0**(= 迁移「清余额保留行」的结果)
    · customer_credit_transactions 有历史 allocate(+ 可选 consume)—— FIFO 依据
    · user_wallets 持有真实余额(= 迁移把钱并过去了)
    """
    _wipe(cur)
    # 生产真实 schema:users 的 NOT NULL 无默认列 = username / password_hash / display_name
    # (简化桩表会 false-green,故按 prod DDL 逐列补齐)
    cur.execute(
        "INSERT INTO users (id, username, password_hash, display_name) "
        "VALUES (%s,%s,'x',%s),(%s,%s,'x',%s)",
        (customer, f"t{customer}", f"t{customer}", AGENT, f"t{AGENT}", f"t{AGENT}"))
    if wallet_paid is None:
        wallet_paid = allocated - consumed
    cur.execute(
        "INSERT INTO user_wallets (user_id, paid_points, bonus_points) VALUES (%s,%s,%s)",
        (customer, wallet_paid, wallet_bonus))
    cur.execute(
        "INSERT INTO user_wallets (user_id, paid_points, bonus_points) VALUES (%s,0,0) "
        "ON CONFLICT (user_id) DO NOTHING", (AGENT,))

    if with_dead_row:
        # 🔴 关键形状:行在、三池为 0(迁移脚本 ⑥ 的产物)
        cur.execute(
            "INSERT INTO customer_agent_credit_wallets "
            "(customer_user_id, agent_user_id, tool_credit_points, publish_credit_points, "
            " bonus_credit_points, total_purchased_points, total_consumed_points) "
            "VALUES (%s,%s,0,0,0,%s,%s)",
            (customer, AGENT, allocated, consumed))

    # 历史信用流水:allocate(+consume)· compute_unspent_from_order 的输入
    cur.execute(
        "INSERT INTO customer_credit_transactions "
        "(customer_user_id, agent_user_id, type, pool, points, balance_tool_after, "
        " balance_publish_after, balance_bonus_after, related_order_id, source, description) "
        "VALUES (%s,%s,'allocate','tool',%s,%s,0,0,%s,'online_payment','夹具历史划拨')",
        (customer, AGENT, allocated, allocated, order_id))
    if consumed:
        cur.execute(
            "INSERT INTO customer_credit_transactions "
            "(customer_user_id, agent_user_id, type, pool, points, balance_tool_after, "
            " balance_publish_after, balance_bonus_after, related_order_id, source, description) "
            "VALUES (%s,%s,'consume','tool',%s,%s,0,0,%s,'tool_consume','夹具历史消费')",
            (customer, AGENT, -consumed, allocated - consumed, order_id))

    cur.execute(
        "INSERT INTO recharge_orders (id, user_id, agent_user_id, amount_cents, base_points, "
        " bonus_points, payment_status, settlement_mode, paid_at) "
        "VALUES (%s,%s,%s,%s,%s,0,'paid','v35_inventory_settlement', NOW())",
        (order_id, customer, AGENT, allocated, allocated))

    cur.execute(
        "INSERT INTO agent_revenue_ledger "
        "(agent_user_id, recharge_order_id, customer_user_id, customer_paid_cents, factory_cents, "
        " gateway_fee_bps, gateway_fee_cents, settlement_service_fee_bps, settlement_service_fee_cents, "
        " agent_margin_before_tax_cents, tax_rate_bps, tax_mode, tax_withholding_cents, "
        " agent_settlement_cents, status, source, settle_at) "
        # tax_mode 的 CHECK 白名单 = withheld / invoice_provided / exempt_manual('none' 非法)
        "VALUES (%s,%s,%s,%s,%s,0,0,0,0,%s,0,'exempt_manual',0,%s,%s,'recharge', NOW() + INTERVAL '3 days') "
        "RETURNING id",
        (AGENT, order_id, customer, allocated, allocated // 2,
         allocated // 2, allocated // 2, ledger_status))
    return cur.fetchone()["id"]


def credit_wallet_row_count(cur) -> int:
    cur.execute("SELECT count(*) AS c FROM customer_agent_credit_wallets")
    return int(cur.fetchone()["c"])


def credit_pools_total(cur) -> int:
    cur.execute("SELECT COALESCE(SUM(tool_credit_points+publish_credit_points+bonus_credit_points),0) AS s "
                "FROM customer_agent_credit_wallets")
    return int(cur.fetchone()["s"])


def wallet_of(cur, uid) -> dict:
    cur.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id=%s", (uid,))
    return dict(cur.fetchone() or {"paid_points": 0, "bonus_points": 0})
