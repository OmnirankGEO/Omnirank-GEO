"""单账本结算的三条不变量 —— **一处实现**(#118 1-8)。

继任 `2aef3b59b`(2026-07-27 单账本收敛):Owner 定「不能有两本账」,
`record_v35_core_settlement` 自那时起**不再调 `allocate_credit`**,
客户的算力**留在 `user_wallets`**,不再复制一份进 `customer_agent_credit_wallets`。

本包有八条判据钉的是**收敛之前**的双账本模型(三个文件最后改动 2026-07-18/20,
收敛后 0 笔改动),因此在 2026-09-06 全部红在
`SELECT … FROM customer_agent_credit_wallets` 返 None 上。

🔴 **不把它们改成「user_wallets 里有钱」就完事** —— 那是让判据跟着实现走,
   下次再换账本模型还得再改一次。改成守**不变量**:

   ① **守恒且精确**:客户 `user_wallets` 的 paid/bonus == 订单快照的应给拆分。
      用 `==` 不用 `>=` —— `>=` 会放过「多给了」,而多给正是资金缺陷的一半。
   ② **双账本不复活**(否定臂):退役账本该客户新行 == 0。
      没有这一臂,「守恒」可以被**两本账各记一半**满足。
   ③ **入账恰一次且幂等**:`point_transactions` 该订单入账行恰 1;
      重放一次 `complete_recharge` 之后 ① ③ 读数**不变**。

⚠️ 三条缺一不可,而且**必须写在一处**:八个调用点各写一遍,迟早有几处漂 ——
   而漂了不会有任何东西报错。
"""

from __future__ import annotations

RETIRED_LEDGERS = ("customer_agent_credit_wallets", "customer_credit_transactions")


def read_settlement_facts(cur, *, order_id: str, customer_user_id: int) -> dict:
    """把三条不变量要用的读数一次取齐(取数与断言分开,便于重放前后比对)。"""
    cur.execute(
        "SELECT paid_points, bonus_points FROM user_wallets WHERE user_id=%s",
        (int(customer_user_id),),
    )
    wallet = cur.fetchone()
    cur.execute(
        "SELECT base_points, bonus_points FROM recharge_orders WHERE id=%s",
        (order_id,),
    )
    order = cur.fetchone()
    cur.execute(
        "SELECT COUNT(*) AS c FROM point_transactions WHERE order_id=%s",
        (order_id,),
    )
    point_tx = int((cur.fetchone() or {}).get("c") or 0)
    cur.execute(
        "SELECT COUNT(*) AS c FROM customer_agent_credit_wallets WHERE customer_user_id=%s",
        (int(customer_user_id),),
    )
    retired_wallet_rows = int((cur.fetchone() or {}).get("c") or 0)
    cur.execute(
        "SELECT COUNT(*) AS c FROM customer_credit_transactions WHERE related_order_id=%s",
        (order_id,),
    )
    retired_tx_rows = int((cur.fetchone() or {}).get("c") or 0)
    return {
        "wallet_paid": int((wallet or {}).get("paid_points") or 0),
        "wallet_bonus": int((wallet or {}).get("bonus_points") or 0),
        "order_base": int((order or {}).get("base_points") or 0),
        "order_bonus": int((order or {}).get("bonus_points") or 0),
        "point_tx": point_tx,
        "retired_wallet_rows": retired_wallet_rows,
        "retired_tx_rows": retired_tx_rows,
        "order_row_present": order is not None,
    }


def assert_single_ledger_settlement(facts: dict) -> None:
    """三条不变量。`facts` 由 `read_settlement_facts` 取。"""
    assert facts["order_row_present"], "订单行不存在 —— 分母塌了,不是通过"
    # ① 守恒且**精确**
    assert (facts["wallet_paid"], facts["wallet_bonus"]) == (
        facts["order_base"], facts["order_bonus"]
    ), (
        "客户 user_wallets 与订单应给拆分对不上:钱包 paid=%s bonus=%s,"
        "订单 base=%s bonus=%s" % (
            facts["wallet_paid"], facts["wallet_bonus"],
            facts["order_base"], facts["order_bonus"])
    )
    # ② 双账本不复活
    assert facts["retired_wallet_rows"] == 0, (
        "退役账本 customer_agent_credit_wallets 又长出 %d 行 —— 两本账复活了"
        % facts["retired_wallet_rows"])
    assert facts["retired_tx_rows"] == 0, (
        "退役账本 customer_credit_transactions 又长出 %d 行 —— 两本账复活了"
        % facts["retired_tx_rows"])
    # ③ 入账恰一次
    assert facts["point_tx"] == 1, (
        "point_transactions 该订单入账行 %d 条(应恰 1)" % facts["point_tx"])
