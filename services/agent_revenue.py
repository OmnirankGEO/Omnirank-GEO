"""
V3.5 代理收益台账服务(agent_revenue_ledger)

核心责任:
- 写入代理应结算款(线上支付路径才写 · 线下不写)
- 4 状态机:frozen → settled → pending_payout → paid
- T+3 settle daily cron(frozen → settled)
- clawback(退款追溯)
- balance view 计算(余额 SSOT)

财务语义:
- agent_revenue_ledger.amount_cents 是"应付代理款"(平台负债 · 不是 OmniRank 收入)
- OmniRank 实际记账收入 = factory_cents + settlement_service_fee_cents
- gateway_fee_cents 是支付渠道实扣(走渠道)
- tax_withholding_cents 是预扣税(进 platform_tax_pool)

关联:
- docs/AI-CONTEXT/V35_FACTORY_INVENTORY_MODEL_v6_2026-05-26.md §4/§12
- memory feedback_v35_factory_inventory_model_v6
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from db.xact_lock_guard import require_xact_scope

logger = logging.getLogger("GEO-V35-AgentRevenue")

# T+3 settle 周期(可后续配置化)
SETTLE_DELAY_DAYS = 3


# ============================================================
# 写入代理收益(complete_recharge 主事务内调)
# ============================================================

def insert_revenue_ledger(
    cursor,
    agent_user_id: int,
    recharge_order_id: str,
    customer_user_id: int,
    customer_paid_cents: int,
    factory_cents: int,
    gateway_fee_bps: int,
    gateway_fee_cents: int,
    settlement_service_fee_bps: int,
    settlement_service_fee_cents: int,
    agent_margin_before_tax_cents: int,
    tax_rate_bps: int,
    tax_mode: str,
    tax_withholding_cents: int,
    agent_settlement_cents: int,
    source: str = "recharge",
    settle_delay_days: int = SETTLE_DELAY_DAYS,
    manual_review_required: bool = False,
    note: Optional[str] = None,
) -> int:
    """
    写代理收益台账 · 主事务内 · 返回 ledger.id
    状态 frozen · settle_at = NOW() + settle_delay_days
    """
    settle_at = datetime.utcnow() + timedelta(days=settle_delay_days)

    cursor.execute("""
        INSERT INTO agent_revenue_ledger (
            agent_user_id, source, recharge_order_id, customer_user_id,
            customer_paid_cents, factory_cents,
            gateway_fee_bps, gateway_fee_cents,
            settlement_service_fee_bps, settlement_service_fee_cents,
            agent_margin_before_tax_cents,
            tax_rate_bps, tax_mode, tax_withholding_cents,
            agent_settlement_cents,
            status, settle_at, manual_review_required, note
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s,
            %s, %s, %s,
            %s,
            'frozen', %s, %s, %s
        ) RETURNING id
    """, (
        agent_user_id, source, recharge_order_id, customer_user_id,
        customer_paid_cents, factory_cents,
        gateway_fee_bps, gateway_fee_cents,
        settlement_service_fee_bps, settlement_service_fee_cents,
        agent_margin_before_tax_cents,
        tax_rate_bps, tax_mode, tax_withholding_cents,
        agent_settlement_cents,
        settle_at, manual_review_required, note,
    ))
    row = cursor.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


# ============================================================
# clawback(退款追溯 · 退款 4 状态机用)
# ============================================================

def insert_revenue_clawback(
    cursor,
    agent_user_id: int,
    original_ledger_id: int,
    clawback_amount_cents: int,
    note: str = "退款 clawback",
) -> int:
    """
    退款时 · 写负数 ledger 冲销原条目
    保留原 status(frozen/settled/paid)语义 · 不直接改原 ledger
    """
    if clawback_amount_cents <= 0:
        raise ValueError("clawback 金额必须 > 0")

    # 加载原 ledger 取信息
    cursor.execute("""
        SELECT recharge_order_id, customer_user_id, status,
               gateway_fee_bps, settlement_service_fee_bps,
               tax_rate_bps, tax_mode
        FROM agent_revenue_ledger
        WHERE id = %s FOR UPDATE
    """, (original_ledger_id,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"原 ledger {original_ledger_id} 不存在")

    settle_at = datetime.utcnow()  # clawback 不冻结 · 立即对账

    cursor.execute("""
        INSERT INTO agent_revenue_ledger (
            agent_user_id, source, recharge_order_id, customer_user_id,
            customer_paid_cents, factory_cents,
            gateway_fee_bps, gateway_fee_cents,
            settlement_service_fee_bps, settlement_service_fee_cents,
            agent_margin_before_tax_cents,
            tax_rate_bps, tax_mode, tax_withholding_cents,
            agent_settlement_cents,
            status, settle_at, note
        ) VALUES (
            %s, 'refund_clawback', %s, %s,
            0, 0,
            %s, 0,
            %s, 0,
            -%s,
            %s, %s, 0,
            -%s,
            'settled', %s, %s
        ) RETURNING id
    """, (
        agent_user_id,
        row["recharge_order_id"], row["customer_user_id"],
        row["gateway_fee_bps"], row["settlement_service_fee_bps"],
        clawback_amount_cents,
        row["tax_rate_bps"], row["tax_mode"],
        clawback_amount_cents,
        settle_at, note,
    ))
    clawback_id = cursor.fetchone()
    return clawback_id["id"] if isinstance(clawback_id, dict) else clawback_id[0]


# ============================================================
# T+3 settle(daily cron · frozen → settled)
# ============================================================

def settle_frozen_to_settled(cursor, batch_size: int = 1000) -> int:
    """
    跑 daily cron · 把 frozen 中 settle_at < NOW() 的 ledger 标 settled
    返回处理条数

    [Codex r2 P0-3 修正] 跳过 cancelled 状态 + 已 reversed 的 ledger
    防退款后 cron 重新 settle 已撤销条目
    """
    # Dealer -> consumer resale has a separate refund-aware maturity state
    # machine.  Exclude those rows from this legacy bulk updater; otherwise a
    # consumer refund request could race the blind frozen -> settled UPDATE.
    cursor.execute("SELECT to_regclass('public.dealer_consumer_sales') AS table_name")
    table_row = cursor.fetchone()
    consumer_table = (
        table_row.get("table_name") if isinstance(table_row, dict)
        else (table_row[0] if table_row else None)
    )
    consumer_exclusion = ""
    if consumer_table:
        consumer_exclusion = """
              AND NOT EXISTS (
                  SELECT 1 FROM dealer_consumer_sales dcs
                  WHERE dcs.order_id=agent_revenue_ledger.recharge_order_id
              )
        """
    cursor.execute(f"""
        UPDATE agent_revenue_ledger
        SET status = 'settled', settled_at = NOW()
        WHERE id IN (
            SELECT id FROM agent_revenue_ledger
            WHERE status = 'frozen' AND settle_at < NOW()
              AND NOT manual_review_required
              AND reversed_at IS NULL
              {consumer_exclusion}
            ORDER BY settle_at ASC
            LIMIT %s
        )
        RETURNING id
    """, (batch_size,))
    rows = cursor.fetchall()
    count = len(rows)
    logger.info(f"settle_frozen_to_settled: {count} 条")
    return count


def cancel_frozen_ledger(cursor, ledger_id: int, clawback_ledger_id: Optional[int] = None, reason: str = "refund") -> int:
    """
    [Codex r2 P0-3 + r3 P0 修正] T+3 内退款 · 把原 frozen ledger 标记 cancelled · 防 cron settle
    clawback_ledger_id 可空:
      - A 状态(全退):无 clawback · 仅 cancel · clawback_ledger_id=None
      - B 状态(部分消费):清原 frozen 后写正数 replacement · clawback_ledger_id=replacement_id
      - C/D 状态:不调本函数 · 直接 insert 负数 clawback
    返回更新行数
    """
    cursor.execute("""
        UPDATE agent_revenue_ledger
        SET status = 'cancelled',
            reversed_at = NOW(),
            reversed_by_ledger_id = %s,
            note = COALESCE(note, '') || %s
        WHERE id = %s AND status = 'frozen'
        RETURNING id
    """, (clawback_ledger_id, f" | cancelled@{reason}", ledger_id))
    rows = cursor.fetchall()
    return len(rows)


def insert_revenue_replacement(
    cursor,
    original_ledger_id: int,
    agent_user_id: int,
    recharge_order_id: str,
    customer_user_id: int,
    customer_paid_cents: int,
    factory_cents: int,
    gateway_fee_bps: int,                 # [Codex r4 P1-2] 从原 ledger 复制
    gateway_fee_cents: int,
    settlement_service_fee_bps: int,      # [Codex r4 P1-2] 从原 ledger 复制
    settlement_service_fee_cents: int,
    agent_margin_before_tax_cents: int,
    tax_rate_bps: int,
    tax_mode: str,                        # [Codex r4 P1-2] 从原 ledger 复制
    tax_withholding_cents: int,
    agent_settlement_cents: int,
    settle_at_orig,
    note: str = "退款部分消费 · replacement frozen ledger",
) -> int:
    """
    [Codex r3 P0 + r4 P1-2] T+3 内 B 状态(部分消费)退款
    主事务内写一条正数 replacement frozen ledger
    表示"已消费部分对应的收益"(保留代理本应得 · 不写负数 clawback)
    继承原 frozen 的 settle_at(T+3 时点不变 · 后续 cron 自然 settle)

    [P1-2 修正] bps + tax_mode 从原 ledger 复制 · 不再硬编码 90/190/withheld
    防原订单是 manual/alipay 或代理 tax_mode='invoice_provided' 时 replacement 审计错位
    """
    cursor.execute("""
        INSERT INTO agent_revenue_ledger (
            agent_user_id, source, recharge_order_id, customer_user_id,
            customer_paid_cents, factory_cents,
            gateway_fee_bps, gateway_fee_cents,
            settlement_service_fee_bps, settlement_service_fee_cents,
            agent_margin_before_tax_cents,
            tax_rate_bps, tax_mode, tax_withholding_cents,
            agent_settlement_cents,
            status, settle_at, note
        ) VALUES (
            %s, 'recharge', %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s,
            %s, %s, %s,
            %s,
            'frozen', %s, %s
        ) RETURNING id
    """, (
        agent_user_id, recharge_order_id, customer_user_id,
        customer_paid_cents, factory_cents,
        gateway_fee_bps, gateway_fee_cents,
        settlement_service_fee_bps, settlement_service_fee_cents,
        agent_margin_before_tax_cents,
        tax_rate_bps, tax_mode, tax_withholding_cents,
        agent_settlement_cents,
        settle_at_orig, f"{note} · replacement-of-ledger#{original_ledger_id}",
    ))
    row = cursor.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


# ============================================================
# 余额查询(代理钱包页 + 提现申请前 check)
# ============================================================

def get_agent_balance(cursor, agent_user_id: int) -> Dict[str, int]:
    """
    [Codex r2 P0-1 修正版] 返回代理 4 池余额 SSOT
    ledger.status 只用 frozen/settled/cancelled
    提现锁定/已打款 状态从 agent_settlement_request_items + agent_settlement_requests.status 推断

    四池:
        frozen_cents          (冻结中 · ledger.status='frozen' AND reversed_at IS NULL)
        settled_total_cents   (T+3 后总余额 · ledger.status='settled')
        pending_locked_cents  (提现申请中锁定 · items WHERE request.status='pending'/'approved')
        paid_locked_cents     (历史已打款 · items WHERE request.status='paid')
        available_cents       = settled_total - pending_locked - paid_locked
    """
    # 1. ledger 各状态求和
    cursor.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN status='frozen' AND reversed_at IS NULL
                              THEN agent_settlement_cents ELSE 0 END), 0) AS frozen,
            COALESCE(SUM(CASE WHEN status='settled'
                              THEN agent_settlement_cents ELSE 0 END), 0) AS settled_total
        FROM agent_revenue_ledger
        WHERE agent_user_id = %s
    """, (agent_user_id,))
    row = cursor.fetchone()
    frozen = row["frozen"] if isinstance(row, dict) else row[0]
    settled_total = row["settled_total"] if isinstance(row, dict) else row[1]

    # 2. 提现申请锁定(pending + approved · 未打款)+ 已打款(paid)
    # 分两类:防"申请中"和"已打款"在 available 计算中混淆
    cursor.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN r.status IN ('pending','approved')
                              THEN i.locked_amount_cents ELSE 0 END), 0) AS pending_locked,
            COALESCE(SUM(CASE WHEN r.status = 'paid'
                              THEN i.locked_amount_cents ELSE 0 END), 0) AS paid_locked
        FROM agent_settlement_request_items i
        JOIN agent_settlement_requests r ON r.id = i.settlement_request_id
        WHERE r.agent_user_id = %s
    """, (agent_user_id,))
    row2 = cursor.fetchone()
    pending_locked = row2["pending_locked"] if isinstance(row2, dict) else row2[0]
    paid_locked = row2["paid_locked"] if isinstance(row2, dict) else row2[1]

    # 2b. [V3.5 v7 批1C] 利润换算力锁定(redeemed · 即时永久消耗 settled 利润)
    #     利润换算力与提现【共用同一 settled ledger】· available 必须同时减此项防双花
    #     (否则同一笔 settled 利润会被提现锁一次 + 换算力锁一次 = 双花)
    #     ⚠️ 依赖 migration(agent_commission_redemption_*)先于代码部署(DB before code 铁律)·
    #        表不存在即暴露部署顺序错误(不静默降级 · 防 available 虚高致双花)
    cursor.execute("""
        SELECT COALESCE(SUM(ri.locked_amount_cents), 0) AS redeem_locked
        FROM agent_commission_redemption_items ri
        JOIN agent_commission_redemption_requests rr ON rr.id = ri.redemption_request_id
        WHERE rr.agent_user_id = %s AND rr.status = 'redeemed'
    """, (agent_user_id,))
    row3 = cursor.fetchone()
    redeem_locked = (row3["redeem_locked"] if isinstance(row3, dict) else row3[0]) or 0

    # 3. 可提/可换 = settled - 提现锁定 - 已打款锁定 - 利润换算力锁定
    # ⚠️ ledger.status='settled' 含"已提走 / 已换算力 / 未用"三类 · 已用部分全减防双算
    available = max(0, settled_total - pending_locked - paid_locked - redeem_locked)

    return {
        "frozen_cents": int(frozen),
        "settled_total_cents": int(settled_total),
        "pending_payout_cents": int(pending_locked),
        "paid_cents": int(paid_locked),
        "redeemed_cents": int(redeem_locked),
        "available_cents": int(available),
    }


# ============================================================
# 提现申请 · 锁定 ledger 行(防重复申请)
# ============================================================

def create_settlement_request(
    cursor,
    agent_user_id: int,
    request_amount_cents: int,
    bank_name: str,
    bank_account: str,
    account_holder: str,
    invoice_required: bool = False,
) -> Dict[str, Any]:
    """
    创建提现申请 · 主事务内 ·
    - check available 余额够
    - 选 ledger 行 FOR UPDATE 锁定
    - INSERT request + items
    - ledger.status 改 pending_payout
    """
    if request_amount_cents <= 0:
        raise ValueError("提现金额必须 > 0")

    balance = get_agent_balance(cursor, agent_user_id)
    if balance["available_cents"] < request_amount_cents:
        raise ValueError(
            f"可提现余额不足: 需要 {request_amount_cents/100:.2f} 元 · "
            f"可用 {balance['available_cents']/100:.2f} 元"
        )

    # [V3.5 v7 批1D] 提现 fees 三段(SSOT helper · 与 GET /settlement/withdrawal-quote 同口径)
    # gross(申请额 = settled margin)→ 扣通道+代收+代扣税 → net 到账 · 写 DB 审计列
    from services.agent_pricing import calc_withdrawal_fees
    fees = calc_withdrawal_fees(cursor, agent_user_id, request_amount_cents)
    if fees["net_cents"] <= 0:
        # 费率配置异常(总扣费 >= 提现额)· raise 让主事务 rollback · 不留半截申请
        raise ValueError(
            f"提现 fees 配置异常 · 提现 {request_amount_cents/100:.2f} 元 · "
            f"扣费 {fees['total_fee_cents']/100:.2f} 元 · 到账 <= 0 · 请联系平台"
        )

    # 创建 request(锁 gross = request_amount_cents 的 ledger · fees 分项 + net 写审计列)
    cursor.execute("""
        INSERT INTO agent_settlement_requests (
            agent_user_id, request_amount_cents, bank_name, bank_account, account_holder,
            invoice_required, status,
            gateway_fee_cents, settlement_fee_cents, tax_cents, net_amount_cents
        ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s)
        RETURNING id
    """, (agent_user_id, request_amount_cents, bank_name, bank_account, account_holder,
          invoice_required,
          fees["gateway_fee_cents"], fees["settlement_fee_cents"], fees["tax_cents"], fees["net_cents"]))
    req_id = cursor.fetchone()
    req_id = req_id["id"] if isinstance(req_id, dict) else req_id[0]

    # [Codex r2 P0-1 修正] 选 ledger 行锁部分金额(支持部分提现)
    # ⚠️ ledger.status 不再改 'pending_payout' · 锁定状态用 items 推断
    # 防"申请 ¥30 锁整条 ¥100" / "available 双算" 问题
    #
    # 算法 FIFO:
    #   1. 列出所有 settled 且未被 pending/approved 申请的 ledger
    #   2. 同时考虑同一 ledger 可能已被部分 paid 锁(取剩余可用部分)
    #   3. 按 settled_at ASC 排序选取 · 锁 min(remaining ledger 余量, request remaining)
    # [BUG-P2] 串行化同一 agent 的提现/换算力:防 READ COMMITTED 下 FOR UPDATE 子查询旧快照
    # 双锁同一 settled ledger(同 classid 920506 与利润换算力互斥 · 锁到主事务结束)。
    require_xact_scope(cursor, where="agent_revenue.create_settlement_request")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute("SELECT pg_advisory_xact_lock(920506, %s)", (agent_user_id,))

    remaining = request_amount_cents
    cursor.execute("""
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
        WHERE l.agent_user_id = %s AND l.status = 'settled'
          AND l.agent_settlement_cents > 0
        ORDER BY l.settled_at ASC, l.id ASC
        FOR UPDATE
    """, (agent_user_id,))
    rows = cursor.fetchall()
    locked_items = []
    for row in rows:
        if remaining <= 0:
            break
        lid = row["id"] if isinstance(row, dict) else row[0]
        lamt = row["agent_settlement_cents"] if isinstance(row, dict) else row[1]
        already = row["already_locked"] if isinstance(row, dict) else row[2]
        # 该 ledger 剩余可用 = 总额 - 已锁定(其他 pending/approved/paid 申请)
        available_in_ledger = lamt - already
        if available_in_ledger <= 0:
            continue
        take = min(available_in_ledger, remaining)
        cursor.execute("""
            INSERT INTO agent_settlement_request_items (
                settlement_request_id, ledger_id, locked_amount_cents
            ) VALUES (%s, %s, %s)
        """, (req_id, lid, take))
        # ⚠️ 不动 ledger.status · 状态仍是 'settled'
        # 锁定信息由 items 表 + request.status 推断
        locked_items.append({"ledger_id": lid, "locked": take})
        remaining -= take

    if remaining > 0:
        raise ValueError(
            f"余额 race 不一致 · 申请 {request_amount_cents/100:.2f} 元 · "
            f"剩余 {remaining/100:.2f} 元无法锁定 · 主事务 rollback"
        )

    return {
        "request_id": req_id,
        "locked_items": locked_items,
        "total_locked_cents": request_amount_cents,
        # [V3.5 v7 批1D] 提现透明三段(金额 · 不露 *_bps · W2 铁律)
        "request_amount_cents": request_amount_cents,
        "platform_fee_cents": fees["platform_fee_cents"],
        "tax_cents": fees["tax_cents"],
        "total_fee_cents": fees["total_fee_cents"],
        "net_cents": fees["net_cents"],
    }


def mark_settlement_paid(
    cursor,
    request_id: int,
    admin_user_id: int,
    transfer_proof_url: Optional[str] = None,
    admin_note: Optional[str] = None,
) -> int:
    """
    admin 财务标记打款成功

    [Codex r2 P0-1 修正] ledger.status 不再标 'paid'
    打款状态完全由 agent_settlement_requests.status='paid' 表达
    items 表的 locked_amount_cents 仍保留 · 用于 paid_locked 余额计算
    防"整条 ledger 误标 paid"(原 BUG:申请 ¥30 但整条 ¥100 都标 paid)
    """
    cursor.execute("""
        UPDATE agent_settlement_requests
        SET status='paid', paid_at=NOW(),
            paid_by_admin_user_id=%s,
            transfer_proof_url=%s, admin_note=%s,
            updated_at=NOW()
        WHERE id=%s AND status IN ('pending','approved')
        RETURNING agent_user_id, request_amount_cents
    """, (admin_user_id, transfer_proof_url, admin_note, request_id))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"settlement_request {request_id} 状态不可打款")

    # ⚠️ 不动 ledger.status · 仍是 'settled'
    # paid_locked 余额从 items + request.status='paid' 自然反映
    # 同 ledger 剩余未锁部分仍可被后续申请使用

    return request_id
