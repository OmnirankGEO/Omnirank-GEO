"""流水构造器 —— 按**生产真实语义**造 point_transactions。

🔴 关键:生产里 `balance_after` 才是钱包真实位移的权威,`amount` 不是 ——
冻结链会写两行(freeze 真扣 + commit 时的 consume 记账行,balance_after 不变)。
本构造器如实复刻这个形态,否则夹具会把「按 amount 求和」的错误实现放行(false-green)。

零生产数据:uid 用 9_2xx_xxx 段,与生产账户无交集。
"""
from __future__ import annotations

USER = 9_200_017
AGENT = 9_200_018


def wipe(cur, user_id=USER):
    for uid in (user_id, AGENT):
        cur.execute("DELETE FROM point_transactions WHERE user_id=%s", (uid,))
        cur.execute("DELETE FROM point_freezes WHERE user_id=%s", (uid,))
        cur.execute("DELETE FROM customer_credit_transactions WHERE customer_user_id=%s", (uid,))
        cur.execute("DELETE FROM customer_agent_credit_wallets WHERE customer_user_id=%s", (uid,))
    cur.execute("DELETE FROM agent_revenue_ledger WHERE customer_user_id=%s OR agent_user_id=%s",
                (user_id, AGENT))
    cur.execute("DELETE FROM recharge_orders WHERE user_id=%s", (user_id,))
    cur.execute("DELETE FROM user_wallets WHERE user_id = ANY(%s)", ([user_id, AGENT],))
    cur.execute("DELETE FROM users WHERE id = ANY(%s)", ([user_id, AGENT],))


class Ledger:
    """按时间序写流水,自己维护 paid/bonus 两轨的 balance_after。"""

    def __init__(self, cur, user_id=USER):
        self.cur = cur
        self.uid = user_id
        self.bal = {"paid": 0, "bonus": 0}
        self._t = 0

    def _next_ts(self):
        self._t += 1
        return f"2026-08-01 00:00:{self._t:02d}"

    def _row(self, typ, track, amount, order_id, balance_after=None):
        if balance_after is None:
            self.bal[track] += amount
            balance_after = self.bal[track]
        else:
            self.bal[track] = balance_after
        self.cur.execute(
            "INSERT INTO point_transactions "
            "(user_id, type, point_type, amount, balance_after, order_id, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::timestamp) RETURNING id",
            (self.uid, typ, track, amount, balance_after, order_id, self._next_ts()))
        return self.cur.fetchone()["id"]

    # ── 进 ──
    def recharge(self, order_id, paid=0, bonus=0):
        if paid:
            self._row("recharge", "paid", paid, order_id)
        if bonus:
            self._row("recharge", "bonus", bonus, order_id)
        return order_id

    def grant(self, order_id, paid=0, bonus=0):
        if paid:
            self._row("agent_grant", "paid", paid, order_id)
        if bonus:
            self._row("agent_grant", "bonus", bonus, order_id)
        return order_id

    # ── 出 ──
    def consume(self, amount, track="paid", order_id=None):
        """直接消费(无冻结)· 真扣余额。"""
        return self._row("consume", track, -amount, order_id or f"OR-direct-{self._t}")

    def freeze_commit(self, amount, track="paid", freeze_id=None):
        """冻结→commit 的**两行**形态:freeze 真扣 + consume 记账行(balance 不变)。

        🔴 这就是「按 amount 求和会算两遍」的那个形态。
        """
        fid = freeze_id if freeze_id is not None else (9_000_000 + self._t)
        tref = f"TASK-{fid}"
        self.cur.execute(
            "INSERT INTO point_freezes (id,user_id,feature_code,amount_total,amount_bonus,"
            "amount_paid,status,task_ref,amount_commission) "
            "VALUES (%s,%s,'t',%s,%s,%s,'committed',%s,0)",
            (fid, self.uid, amount, amount if track == "bonus" else 0,
             amount if track == "paid" else 0, tref))
        self._row("freeze", track, -amount, f"FRZ{fid}-{tref}")
        # commit 记账行:balance_after 与上一行**相同**
        self._row("consume", track, -amount, f"CMT{fid}-{tref}",
                  balance_after=self.bal[track])
        return fid

    def freeze_open(self, amount, track="paid", freeze_id=None):
        """**仍在冻结中**(status='frozen')· 只有 freeze 行,余额已扣、无 commit 行。"""
        fid = freeze_id if freeze_id is not None else (9_500_000 + self._t)
        tref = f"TASK-{fid}"
        self.cur.execute(
            "INSERT INTO point_freezes (id,user_id,feature_code,amount_total,amount_bonus,"
            "amount_paid,status,task_ref,amount_commission) "
            "VALUES (%s,%s,'t',%s,%s,%s,'frozen',%s,0)",
            (fid, self.uid, amount, amount if track == "bonus" else 0,
             amount if track == "paid" else 0, tref))
        self._row("freeze", track, -amount, f"FRZ{fid}-{tref}")
        return fid

    def release(self, freeze_id, amount, track="paid"):
        """释放冻结:point_freezes 翻 released + 写正行回补余额。"""
        self.cur.execute("UPDATE point_freezes SET status='released' WHERE id=%s", (freeze_id,))
        self._row("release", track, amount, f"RLS{freeze_id}-TASK-{freeze_id}")


def seed_user(cur, user_id=USER, paid=0, bonus=0):
    wipe(cur, user_id)
    cur.execute(
        "INSERT INTO users (id, username, password_hash, display_name) "
        "VALUES (%s,%s,'x',%s),(%s,%s,'x',%s)",
        (user_id, f"t{user_id}", f"t{user_id}", AGENT, f"t{AGENT}", f"t{AGENT}"))
    cur.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points) VALUES (%s,%s,%s)",
                (user_id, paid, bonus))
    cur.execute("INSERT INTO user_wallets (user_id, paid_points, bonus_points) VALUES (%s,0,0) "
                "ON CONFLICT (user_id) DO NOTHING", (AGENT,))
    lg = Ledger(cur, user_id)
    # 初始余额**不写流水**(模拟历史被截断/归档):Ledger 的 balance_after 从这里起算,
    # 但 FIFO 重放看不到对应的入账行 —— 这正是"消费超过已知入账"的真实成因。
    lg.bal["paid"] = paid
    lg.bal["bonus"] = bonus
    return lg


def sync_wallet(cur, ledger, user_id=USER):
    """把 user_wallets 对齐到流水终值(生产里两者本就一致)。"""
    cur.execute("UPDATE user_wallets SET paid_points=%s, bonus_points=%s WHERE user_id=%s",
                (ledger.bal["paid"], ledger.bal["bonus"], user_id))


def make_order(cur, order_id, user_id=USER, base_points=0, bonus_points=0, amount_cents=None):
    """建一张 v35 结算模式的 paid 订单(退款链的入口条件)。"""
    cur.execute(
        "INSERT INTO recharge_orders (id,user_id,agent_user_id,amount_cents,base_points,"
        "bonus_points,payment_status,settlement_mode,paid_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,'paid','v35_inventory_settlement',NOW())",
        (order_id, user_id, AGENT, amount_cents if amount_cents is not None else base_points,
         base_points, bonus_points))


def add_legacy_allocate(cur, order_id, user_id=USER, points=0, pool="tool"):
    """给订单造**老台账** allocate 历史 → 新旧分流应判它走老路。"""
    cur.execute(
        "INSERT INTO customer_agent_credit_wallets (customer_user_id, agent_user_id) "
        "VALUES (%s,%s) ON CONFLICT (customer_user_id) DO NOTHING", (user_id, AGENT))
    cur.execute(
        "INSERT INTO customer_credit_transactions "
        "(customer_user_id, agent_user_id, type, pool, points, balance_tool_after, "
        " balance_publish_after, balance_bonus_after, related_order_id, source, description) "
        "VALUES (%s,%s,'allocate',%s,%s,%s,0,0,%s,'online_payment','夹具:老台账历史')",
        (user_id, AGENT, pool, points, points, order_id))
