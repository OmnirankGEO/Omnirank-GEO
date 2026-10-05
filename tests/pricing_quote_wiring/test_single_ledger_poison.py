"""单账本三条不变量的**牙齿自证**(#118 1-8)。

🔴 为什么要有这个文件:上面八条判据是从「双账本」改成「单账本」的,
   而改判据最容易改出的病是**把期望值改成实现现在输出的那个值** ——
   那样它对任何回归都不再红。所以按 Review 点名的两发毒各打一枪:

     毒 1 同一订单多记一笔入账 -> 不变量 ③ 必红
     毒 2 往退役表写一行       -> 不变量 ② 必红(双账本复活的否定臂)

🔴 两发都在**真库真行**上施加,不是构造 facts 字典 —— 构造字典只能证
   `assert` 会抛,证不了 `read_settlement_facts` 读的是那张表那一列。
   毒 2 尤其要真写:退役表若被谁 DROP 了,读数恒 0、不变量 ② 恒绿,
   而「恒绿」与「守住了」在退出码上同形。

🔴 本文件用**自己的客户**(POISON_USER),不走 `_retail_quote()`。
   第一版走了,结果**单跑绿、整包红**:同包别的用例把 agent 200 的进货成本
   抬到 195000,`publish_retail` 默认零售 180000 就撞 §11.1 成本下限守卫。
   —— 判据的绿不该取决于谁先跑。

   这一条腿证的是:helper **读对了表和列、并且能区分**。
   它**不证**「helper 读的正是生产写的那些列」—— 那条腿由同包八条
   真结算判据承担(它们跑的是 `complete_recharge` 真链路)。两条腿都要有。
"""

import pytest

from db.connection import get_db
from tests.pricing_quote_wiring._single_ledger import (
    assert_single_ledger_settlement,
    read_settlement_facts,
)

POISON_USER = 409
BASE_POINTS = 12345
BONUS_POINTS = 67


def _seed(order_id: str):
    """造一个**已结算**的干净局面:钱包 == 订单快照,入账恰 1 行,退役账本 0 行。

    列名按各表建表源逐项核过(`db/wallet_db.py:62` point_transactions /
    `scripts/migration_v35_factory_inventory_2026_05_26.sql:163` 退役表)。
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users(id, username, is_active) VALUES (%s, %s, 1)"
            " ON CONFLICT (id) DO NOTHING",
            (POISON_USER, "poison-customer-%d" % POISON_USER),
        )
        cur.execute(
            "INSERT INTO user_wallets(user_id, agent_level, paid_points, bonus_points)"
            " VALUES (%s, 0, %s, %s)"
            " ON CONFLICT (user_id) DO UPDATE SET paid_points=EXCLUDED.paid_points,"
            "   bonus_points=EXCLUDED.bonus_points",
            (POISON_USER, BASE_POINTS, BONUS_POINTS),
        )
        cur.execute(
            "INSERT INTO recharge_orders(id, user_id, amount_cents, base_points,"
            "  bonus_points, payment_method, payment_status)"
            " VALUES (%s, %s, 1, %s, %s, 'wechat', 'paid')",
            (order_id, POISON_USER, BASE_POINTS, BONUS_POINTS),
        )
        cur.execute(
            "INSERT INTO point_transactions(user_id, type, point_type, amount,"
            "  balance_after, order_id)"
            " VALUES (%s, 'recharge', 'paid', %s, %s, %s)",
            (POISON_USER, BASE_POINTS, BASE_POINTS, order_id),
        )
        conn.commit()
        facts = read_settlement_facts(
            conn.cursor(), order_id=order_id, customer_user_id=POISON_USER)
    # 施毒**前**必须是绿的 —— 否则后面的红说明不了是毒造成的
    assert_single_ledger_settlement(facts)
    return facts


def test_poison_extra_point_transaction_must_go_red():
    """毒 1:同一订单多记一笔入账 -> 不变量 ③ 必红。"""
    order_id = "poison-extra-point-tx"
    before = _seed(order_id)
    with get_db() as conn:
        cur = conn.cursor()
        # 复制**那条真行**而不是自己拼一行:所有 CHECK/NOT NULL 天然满足,
        # 且毒的形状与真行同形 —— 「重放多记一笔」就长这样。
        cur.execute(
            "INSERT INTO point_transactions"
            " (user_id, type, point_type, amount, balance_after,"
            "  feature_code, description, order_id)"
            " SELECT user_id, type, point_type, amount, balance_after,"
            "        feature_code, description, order_id"
            " FROM point_transactions WHERE order_id=%s",
            (order_id,),
        )
        conn.commit()
        after = read_settlement_facts(
            conn.cursor(), order_id=order_id, customer_user_id=POISON_USER)
    # 毒自证:行数**真的**从 1 变 2。「锁没牙」与「毒没下成」方向相反、信号同形。
    assert (before["point_tx"], after["point_tx"]) == (1, 2), (before, after)
    with pytest.raises(AssertionError, match="入账行"):
        assert_single_ledger_settlement(after)


def test_poison_retired_ledger_row_must_go_red():
    """毒 2:往退役账本写一行 -> 不变量 ② 必红(双账本复活)。"""
    order_id = "poison-retired-ledger-row"
    before = _seed(order_id)
    with get_db() as conn:
        cur = conn.cursor()
        # type CHECK IN (allocate,consume,refund,revoke) · pool CHECK IN (tool,publish,bonus)
        # · source CHECK IN (online_payment,...) · 三个 balance_*_after 均 NOT NULL
        cur.execute(
            "INSERT INTO customer_credit_transactions"
            " (customer_user_id, agent_user_id, type, pool, points,"
            "  balance_tool_after, balance_publish_after, balance_bonus_after,"
            "  related_order_id, source)"
            " VALUES (%s, 200, 'allocate', 'tool', 1, 1, 0, 0, %s, 'online_payment')",
            (POISON_USER, order_id),
        )
        conn.commit()
        after = read_settlement_facts(
            conn.cursor(), order_id=order_id, customer_user_id=POISON_USER)
    assert (before["retired_tx_rows"], after["retired_tx_rows"]) == (0, 1), (before, after)
    with pytest.raises(AssertionError, match="两本账复活"):
        assert_single_ledger_settlement(after)
