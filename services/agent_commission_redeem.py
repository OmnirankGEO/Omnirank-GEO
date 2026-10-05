"""服务商利润换算力(V3.5 v7 · 老板 A 拍板 2026-06-08)

服务商把【settled 利润】(agent_revenue_ledger · cents · T+3 后)按当前出厂折扣等价换成
paid_inventory 算力,等于"用税前利润主动进货 · 不过微信支付 · 不扣 fees/税"。

防双花 SSOT:利润换算力与提现共用 settled ledger。available 由 agent_revenue.get_agent_balance
统一计算(settled_total − 提现锁 − 换算力锁)· redeem 时 FIFO 锁 settled ledger 写 redemption_items。
不动 commission_points · 不动 user_wallets · 不扣 fees/税。

换算力公式(floor · 平台不亏):
    points = redeem_cents × wholesale_denom // wholesale_numer
  与代理预付进货 _calc_prepay_points 同口径(利润换算力 = 用利润进货)。
  (calc_factory_cents 进货是 ceil 让平台不漏成本 · 逆向换算力 floor 让平台不亏 · 二者对称)
"""
import logging
from typing import Dict, Any
from db.xact_lock_guard import require_xact_scope

logger = logging.getLogger("GEO-CommissionRedeem")


def calc_redeem_points(redeem_cents: int, numer: int, denom: int) -> int:
    """利润 cents → 可换 paid_inventory 算力(floor · 平台不亏)· 与 _calc_prepay_points 同公式。"""
    if redeem_cents <= 0:
        return 0
    return redeem_cents * denom // numer


def redeem_commission_to_inventory(cursor, agent_user_id: int, redeem_cents: int) -> Dict[str, Any]:
    """服务商利润换算力主逻辑(主事务内 · 任一步失败 raise 让 rollback · 不留半截)。

    1. check available_settled >= redeem_cents(get_agent_balance · 已含换算力锁 SSOT)
    2. 算可换算力(per-agent 出厂折扣 · floor · 锁定折扣到 redemption 记录追溯)
    3. 创建 redemption_request + FIFO 锁 settled ledger 写 redemption_items
       (每 ledger 可用 = settlement_cents − 提现锁(pending/approved/paid) − 换算力锁(redeemed))
    4. UPDATE paid_inventory_points += points + 写 inventory transaction(purchase_from_commission)
    5. 不动 commission_points / user_wallets / 不扣 fees / 税
    """
    if redeem_cents <= 0:
        raise ValueError("换算力金额必须 > 0")

    from services.agent_revenue import get_agent_balance
    from services.agent_pricing import get_agent_wholesale_ratio
    from services.agent_inventory import get_or_create_inventory_wallet, _insert_inventory_transaction

    # [BUG-P2] 串行化同一 agent 的换算力/提现:READ COMMITTED 下 FOR UPDATE 锁住 ledger 行后,
    # SELECT 列表里的 already_locked 子查询仍用语句开始旧快照(ledger 行未 UPDATE 无 EvalPlanQual 重查)
    # → 看不到并发 tx 刚插入的 redemption/settlement items → already_locked 偏小 → 双锁同一 settled ledger。
    # advisory xact lock(同 classid 与提现互斥 · 锁到主事务结束)把同 agent 的 redeem/withdraw 串行,
    # WORKERS>1 / sync 端点线程池 / 蓝绿双实例切流量窗口均安全。
    require_xact_scope(cursor, where="agent_commission_redeem.redeem_commission_to_inventory")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute("SELECT pg_advisory_xact_lock(920506, %s)", (agent_user_id,))

    # 1. 可用 settled 利润(get_agent_balance 已含换算力锁 · 防双花 SSOT)
    balance = get_agent_balance(cursor, agent_user_id)
    if balance["available_cents"] < redeem_cents:
        raise ValueError(
            f"可换算力的利润不足:需要 {redeem_cents / 100:.2f} 元 · "
            f"可用 {balance['available_cents'] / 100:.2f} 元"
        )

    # 2. 当前出厂折扣(per-agent override · 锁定到 redemption 记录追溯)· floor 换算力
    numer, denom = get_agent_wholesale_ratio(agent_user_id)
    points = calc_redeem_points(redeem_cents, numer, denom)
    if points <= 0:
        raise ValueError("换算力金额过小 · 不足换 1 算力")

    # 3a. 创建 redemption_request(即时完成 status='redeemed')
    cursor.execute(
        """
        INSERT INTO agent_commission_redemption_requests
            (agent_user_id, redeem_cents, inventory_points_granted, wholesale_numer, wholesale_denom, status)
        VALUES (%s, %s, %s, %s, %s, 'redeemed')
        RETURNING id
        """,
        (agent_user_id, redeem_cents, points, numer, denom),
    )
    rrow = cursor.fetchone()
    redemption_id = rrow["id"] if isinstance(rrow, dict) else rrow[0]

    # 3b. FIFO 锁 settled ledger(可用 = settlement_cents − 提现锁 − 换算力锁)
    remaining = redeem_cents
    cursor.execute(
        """
        SELECT l.id,
               l.agent_settlement_cents,
               COALESCE((
                   SELECT SUM(i.locked_amount_cents) FROM agent_settlement_request_items i
                   JOIN agent_settlement_requests r ON r.id = i.settlement_request_id
                   WHERE i.ledger_id = l.id AND r.status IN ('pending','approved','paid')
               ), 0)
               + COALESCE((
                   SELECT SUM(ri.locked_amount_cents) FROM agent_commission_redemption_items ri
                   JOIN agent_commission_redemption_requests rr ON rr.id = ri.redemption_request_id
                   WHERE ri.ledger_id = l.id AND rr.status = 'redeemed'
               ), 0) AS already_locked
        FROM agent_revenue_ledger l
        WHERE l.agent_user_id = %s AND l.status = 'settled' AND l.agent_settlement_cents > 0
        ORDER BY l.settled_at ASC, l.id ASC
        FOR UPDATE
        """,
        (agent_user_id,),
    )
    rows = cursor.fetchall()
    locked_items = []
    for row in rows:
        if remaining <= 0:
            break
        lid = row["id"] if isinstance(row, dict) else row[0]
        lamt = row["agent_settlement_cents"] if isinstance(row, dict) else row[1]
        already = row["already_locked"] if isinstance(row, dict) else row[2]
        available_in_ledger = lamt - already
        if available_in_ledger <= 0:
            continue
        take = min(available_in_ledger, remaining)
        cursor.execute(
            """
            INSERT INTO agent_commission_redemption_items
                (redemption_request_id, ledger_id, locked_amount_cents)
            VALUES (%s, %s, %s)
            """,
            (redemption_id, lid, take),
        )
        locked_items.append({"ledger_id": lid, "locked": take})
        remaining -= take

    if remaining > 0:
        # 可用额度 race 不一致(并发提现/换算力)→ raise 让主事务 rollback · 不留半截
        raise ValueError(
            f"利润 race 不一致 · 换算力 {redeem_cents / 100:.2f} 元 · "
            f"剩 {remaining / 100:.2f} 元无法锁定 · rollback"
        )

    # 4. 加 paid_inventory + 写库存流水(purchase_from_commission · 不动 commission_points/user_wallets)
    wallet = get_or_create_inventory_wallet(cursor, agent_user_id)
    new_paid = wallet["paid_inventory_points"] + points
    new_bonus = wallet["bonus_inventory_points"]
    cursor.execute(
        """
        UPDATE agent_inventory_wallets
        SET paid_inventory_points = %s,
            total_purchased_points = total_purchased_points + %s,
            updated_at = NOW()
        WHERE agent_user_id = %s
        """,
        (new_paid, points, agent_user_id),
    )
    _insert_inventory_transaction(
        cursor, agent_user_id, "purchase_from_commission", "paid", points, new_paid, new_bonus,
        description=f"利润换算力 · 用 ¥{redeem_cents / 100:.2f} 利润换 {points} 算力(出厂价等价进货 · 不扣 fees/税)",
    )

    logger.info(
        f"[redeem] agent={agent_user_id} 利润 {redeem_cents} cents → {points} 算力 · "
        f"redemption={redemption_id} · 折扣 {numer}/{denom}"
    )
    return {
        "redemption_id": redemption_id,
        "redeem_cents": redeem_cents,
        "inventory_points_granted": points,
        "new_paid_inventory_points": new_paid,
        "wholesale_numer": numer,
        "wholesale_denom": denom,
        "locked_items": locked_items,
    }
