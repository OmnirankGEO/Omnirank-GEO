"""[v6 req2 · 老板决策 2026-07-13] dispute_hold 订单托管(escrow)· 订单↔dispute 关联 + 不可消费额度 + 原子裁决。

老板拍板:
- hold 期间【下单即冻结进 escrow】:complete_recharge 检出 dispute_hold → 把刚入账的 base/bonus【反扣 user_wallets】
  → 记入 dispute_escrow(status='held')· 客户在争议期【消费不到】这笔(不可绕过服务商结算的可消费平台余额)。
- admin 裁决【原子完成】结算或退款:
    keep_old → 结算给【原绑定服务商 X】;reassign → 结算给【新服务商 Y】;reject → 退款给客户。
- 只覆盖 v6 上线后的新订单(历史已结算 snapshot 不可变)。

三账守恒:
- hold:  user_wallets −(base+bonus) · escrow +(base+bonus)
- settle:escrow −(base+bonus) · customer_agent_credit_wallets +(base+bonus)(客户拿到服务商额度)· agent_revenue_ledger 记 A 收益
- refund:escrow −(base+bonus) · user_wallets +(base+bonus)(客户拿回可消费额度 · 谁都不结算)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger("GEO-DisputeEscrow")


def init_dispute_escrow_tables(cursor=None) -> None:
    owns = cursor is None
    conn = None
    if owns:
        from db.connection import get_connection
        conn = get_connection()
        cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dispute_escrow (
                id                  BIGSERIAL PRIMARY KEY,
                order_id            TEXT NOT NULL UNIQUE,
                dispute_id          BIGINT,
                customer_user_id    INTEGER NOT NULL,
                order_agent_user_id INTEGER NOT NULL,
                bound_agent_user_id INTEGER,
                amount_cents        INTEGER NOT NULL DEFAULT 0,
                base_points         INTEGER NOT NULL DEFAULT 0,
                bonus_points        INTEGER NOT NULL DEFAULT 0,
                pricing_snapshot    JSONB,
                status              TEXT NOT NULL DEFAULT 'held'
                                    CHECK (status IN ('held','settled','refunded')),
                resolved_agent_user_id INTEGER,
                resolved_by         INTEGER,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at         TIMESTAMPTZ
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dispute_escrow_dispute ON dispute_escrow(dispute_id) WHERE status='held'")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dispute_escrow_customer ON dispute_escrow(customer_user_id, status)")
        if owns:
            conn.commit()
        logger.info("[DisputeEscrow] dispute_escrow 表就绪")
    finally:
        if owns and conn:
            try:
                conn.close()
            except Exception:
                pass


def _g(row, k, default=None):
    if row is None:
        return default
    return row.get(k, default) if isinstance(row, dict) else default


def _freeze_tax(cursor, snapshot: Optional[dict], order_agent_user_id: int) -> dict:
    """🔴 [#74 P3] 把**下单时那个服务商**的税制冻进快照。

    裁决常在数月后发生,而 `get_agent_tax_*` 读的是**今天**的配置 ——
    拿今天的税制结算几个月前的订单,与「按原订单不可变快照反转」直接冲突。

    ⚠️ **只冻 order_agent 的**,并在键名里写明是谁的:
       `reassign` 裁决会结算给**另一个**服务商,把 A 的税率套到 B 身上是错的。
       结算侧据此分流:结给同一个 agent ⇒ 用冻结值;结给别人 ⇒ 只能现读那个人的,
       并在 ledger 里记明用的是哪一种(见 dispute_settlement)。
    """
    snap = dict(snapshot or {})
    # 🔴🔴 **必须 SAVEPOINT 隔离**:`except` 只吞得掉 Python 异常,
    #    吞不掉 PostgreSQL 的事务中毒状态 —— 一条失败的 SELECT 之后,
    #    同一事务里后续每一句都 InFailedSqlTransaction。
    #    本函数跑在**充值主事务**内 ⇒ 税表读不到会连累**整笔充值回滚**,
    #    而代码表面上「优雅降级了」。
    #    (本仓三行之外的 `_hold_for_dispute` 早就写着同一条教训:
    #     「表不存在时直接 SELECT 会 abort 整个主事务(PG 语义)」——
    #     注释传不出去,所以这里用 SAVEPOINT 这个**会自己生效的东西**,不是再写一条注释。)
    cursor.execute("SAVEPOINT dispute_freeze_tax")
    try:
        from services.agent_pricing import get_agent_tax_mode, get_agent_tax_rate_bps
        snap["order_agent_user_id"] = int(order_agent_user_id)
        snap["order_agent_tax_rate_bps"] = int(get_agent_tax_rate_bps(cursor, order_agent_user_id))
        snap["order_agent_tax_mode"] = get_agent_tax_mode(cursor, order_agent_user_id)
        cursor.execute("RELEASE SAVEPOINT dispute_freeze_tax")
    except Exception as err:
        cursor.execute("ROLLBACK TO SAVEPOINT dispute_freeze_tax")
        # 半填的键要清掉:留下 order_agent_user_id 而没有税率,
        # 结算侧的 `_use_frozen` 判定会读到一个**不完整的冻结面**。
        for _k in ("order_agent_user_id", "order_agent_tax_rate_bps", "order_agent_tax_mode"):
            snap.pop(_k, None)
        logger.warning("[DisputeEscrow] 冻结 order_agent=%s 税制失败(结算将现读):%s",
                       order_agent_user_id, err)
    return snap


def create_escrow(cursor, *, order_id: str, dispute_id: Optional[int], customer_user_id: int,
                  order_agent_user_id: int, bound_agent_user_id: Optional[int],
                  amount_cents: int, base_points: int, bonus_points: int,
                  pricing_snapshot: Optional[dict] = None) -> Optional[int]:
    """[req2] 在 complete_recharge 主事务内:反扣 user_wallets(不可消费)+ 记 escrow(held)。

    幂等:同 order_id 已有 escrow → no-op(ON CONFLICT DO NOTHING),不重复反扣。返回 escrow id 或 None(已存在)。
    """
    # 1. 反扣 user_wallets(客户争议期消费不到 · GREATEST 防负)· 只有真插入 escrow 时才反扣(见 2 的守卫)
    cursor.execute("""
        INSERT INTO dispute_escrow
            (order_id, dispute_id, customer_user_id, order_agent_user_id, bound_agent_user_id,
             amount_cents, base_points, bonus_points, pricing_snapshot, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'held')
        ON CONFLICT (order_id) DO NOTHING
        RETURNING id
    """, (order_id, dispute_id, customer_user_id, order_agent_user_id, bound_agent_user_id,
          amount_cents, base_points, bonus_points,
          json.dumps(_freeze_tax(cursor, pricing_snapshot, order_agent_user_id),
                     ensure_ascii=False)))
    row = cursor.fetchone()
    if not row:
        logger.warning(f"[DisputeEscrow] order={order_id} escrow 已存在 · 跳过反扣(幂等)")
        return None
    escrow_id = row["id"] if isinstance(row, dict) else row[0]
    # 2. 反扣 user_wallets(与插入 escrow 在同一主事务 · 原子)
    # 🔴🔴 [#74 ③ · Owner 拍板 2026-09-05] 精确扣,**不钳位**。
    #    原来是 `GREATEST(0, paid_points - base)`:余额不足时**静默少扣**,
    #    而 settle/refund 都按 escrow 记的**全额**回补 ⇒ 凭空造算力。
    #    它防御的是一件不该发生的事(hold 就在 complete_recharge 入账之后的同一事务里),
    #    而防御方式本身会造钱 —— 宁可卡住等人,不可悄悄多给。
    #    条件写进 WHERE:余额不足 ⇒ 0 行 ⇒ 下面 raise ⇒ 整笔充值回滚(客户钱不丢)。
    cursor.execute("""
        UPDATE user_wallets
           SET paid_points = paid_points - %s,
               bonus_points = bonus_points - %s,
               updated_at = NOW()
         WHERE user_id = %s AND paid_points >= %s AND bonus_points >= %s
     RETURNING paid_points, bonus_points
    """, (base_points, bonus_points, customer_user_id, base_points, bonus_points))
    if cursor.rowcount != 1:
        # 🔴 两种原因处置相同(都拒),但**报文要分得开** ——
        #    「钱包行不存在」与「余额不够」指向完全不同的排查方向。
        cursor.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id = %s",
                       (customer_user_id,))
        _bal = cursor.fetchone()
        _why = ("钱包行不存在" if not _bal
                else "余额不足(paid=%s bonus=%s,需 %s/%s)" % (
                    _bal["paid_points"], _bal["bonus_points"], base_points, bonus_points))
        raise RuntimeError(
            "[DisputeEscrow][CRITICAL] order=%s 入 escrow 反扣失败(%s)· 拒绝建 held"
            "(否则 settle/refund 会按全额回补 = 凭空造算力)· 整笔充值将回滚"
            % (order_id, _why))
    logger.warning(f"[DisputeEscrow] order={order_id} 入 escrow(held)· 反扣 user_wallets base={base_points} bonus={bonus_points} "
                   f"· customer={customer_user_id} order_agent={order_agent_user_id} bound_agent={bound_agent_user_id}")
    return int(escrow_id)


def list_held_for_dispute(cursor, dispute_id: int) -> list[dict]:
    cursor.execute("SELECT * FROM dispute_escrow WHERE dispute_id=%s AND status='held' FOR UPDATE", (dispute_id,))
    return [dict(r) for r in cursor.fetchall()]


def settle_escrow(cursor, escrow: dict, agent_user_id: int, admin_id: Optional[int]) -> bool:
    """[v7 finding1 · P0 修] 结算给 agent_user_id(keep_old→X / reassign→Y)。

    ⚠️ 旧 v6 实现【手写第二套资金算法】(硬编码出厂系数 · 忽略快照 wholesale_cents · 把 base+bonus 全塞 tool 池
       丢 publish 分轨 · 无库存双流水/额度分轨流水)→ 平台亏损 P0。已删除。
    现【委托】唯一 canonical 裁决结算服务(services/dispute_settlement.settle_escrow_to_agent),
    与充值主路径 record_v35_core_settlement 同一算法 · 全量继承订单快照 · 守恒。
    """
    from services.dispute_settlement import settle_escrow_to_agent
    return settle_escrow_to_agent(cursor, escrow, agent_user_id, admin_id)


def refund_escrow(cursor, escrow: dict, admin_id: Optional[int]) -> bool:
    """[req2] 退款给客户(reject)· escrow −(base+bonus) → user_wallets +(base+bonus)· 幂等(仅 held→refunded)。"""
    eid = escrow["id"]
    cursor.execute("UPDATE dispute_escrow SET status='refunded', resolved_by=%s, resolved_at=NOW() "
                   "WHERE id=%s AND status='held' RETURNING base_points, bonus_points, customer_user_id",
                   (admin_id, eid))
    row = cursor.fetchone()
    if not row:
        logger.warning(f"[DisputeEscrow] escrow={eid} 非 held(重复裁决?)· 跳过 refund")
        return False
    base = row["base_points"]; bonus = row["bonus_points"]; customer = row["customer_user_id"]
    cursor.execute("""
        UPDATE user_wallets SET paid_points = paid_points + %s, bonus_points = bonus_points + %s, updated_at = NOW()
         WHERE user_id = %s
    """, (base, bonus, customer))
    # [v6 对抗审 P2 修] 校验 rowcount:客户 wallet 行缺失(如 admin 硬删客户级联删钱包)→ UPDATE 0 行,
    #   钱既不在 escrow 也没回 user_wallets = 静默资金蒸发。raise → resolve_dispute 整笔裁决回滚(escrow 保持 held · 强制人工处理)。
    if cursor.rowcount != 1:
        raise RuntimeError(f"[DisputeEscrow][CRITICAL] escrow={eid} refund 目标 user_wallets(customer={customer})命中 "
                           f"{cursor.rowcount} 行(期望 1)· 拒绝坐实 refunded(防资金蒸发)· 裁决将回滚")
    logger.warning(f"[DisputeEscrow] escrow={eid} → refunded to customer={customer} · user_wallets +{int(base)+int(bonus)}")
    return True
