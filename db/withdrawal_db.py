"""
提现功能数据库模块
- bank_cards: 银行卡管理（加密存储）
- withdrawal_requests: 提现申请与审批
- user_wallets KYC 扩展字段
"""

import logging
from decimal import Decimal
from fastapi import HTTPException
from db.connection import get_connection, get_db
from db.wallet_db import insert_transaction
from services.kyc_crypto import encrypt_id_card, hmac_id_card

logger = logging.getLogger("GEO-Withdrawal")

# ==================== 常量 ====================

POINTS_PER_YUAN = 130
FEE_RATE = Decimal("0.01")
MIN_WITHDRAWAL_YUAN = Decimal("100")
MAX_PENDING_COUNT = 5


def _enqueue_legacy_withdrawal_terminal(cursor, withdrawal: dict, state: str, reason: str = "") -> None:
    """Write old-chain notification events with the surrounding money transaction."""
    from datetime import datetime, timezone

    from services.notification_events import NotificationEventType, RecipientKind
    from services.notification_outbox import (
        enqueue_admin_notification_events,
        enqueue_notification_event,
    )

    event_by_state = {
        "submitted": NotificationEventType.AGENT_SETTLEMENT_SUBMITTED,
        "approved": NotificationEventType.AGENT_SETTLEMENT_APPROVED,
        "rejected": NotificationEventType.AGENT_SETTLEMENT_REJECTED,
        "paid": NotificationEventType.AGENT_SETTLEMENT_PAID,
    }
    event_type = event_by_state[state]
    withdrawal_id = int(withdrawal["id"])
    business_id = f"legacy-withdrawal-{withdrawal_id}"
    occurred = (
        withdrawal.get("paid_at")
        or withdrawal.get("reviewed_at")
        or withdrawal.get("created_at")
        or datetime.now(timezone.utc)
    )
    occurred_at = occurred.isoformat(timespec="seconds") if hasattr(occurred, "isoformat") else str(occurred)
    gross = Decimal(str(withdrawal.get("amount_yuan") or 0))
    facts = {
        "business_no": f"WITHDRAWAL-{withdrawal_id}",
        "amount": f"{gross:.2f} 元",
        "status": {
            "submitted": "已提交，等待审核",
            "approved": "审核通过，等待打款",
            "rejected": "审核未通过",
            "paid": "已打款",
        }[state],
        "occurred_at": occurred_at,
    }
    if state == "rejected":
        facts["reason"] = reason or withdrawal.get("reject_reason") or "请在提现页面查看处理建议。"
    elif state == "paid":
        facts["fee_amount"] = f"{Decimal(str(withdrawal.get('fee_yuan') or 0)):.2f} 元"
        facts["net_amount"] = f"{Decimal(str(withdrawal.get('actual_yuan') or 0)):.2f} 元"

    enqueue_notification_event(
        cursor,
        event_type=event_type,
        business_id=business_id,
        terminal_state=state,
        recipient_user_id=int(withdrawal["user_id"]),
        recipient_kind=RecipientKind.AGENT,
        facts=facts,
    )
    if state == "submitted":
        enqueue_admin_notification_events(
            cursor,
            event_type=event_type,
            business_id=business_id,
            terminal_state=state,
            facts=facts,
        )


# ==================== 初始化 ====================

def init_withdrawal_tables():
    """创建提现相关表（幂等，可重复调用）"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bank_cards (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                card_holder TEXT NOT NULL,
                bank_name TEXT NOT NULL,
                card_number_encrypted TEXT NOT NULL,
                card_number_hmac TEXT NOT NULL,
                card_number_mask TEXT NOT NULL,
                phone TEXT NOT NULL,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_bank_cards_user ON bank_cards(user_id)
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_bank_cards_hmac_active
            ON bank_cards(card_number_hmac) WHERE status = 'active'
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                bank_card_id INTEGER NOT NULL REFERENCES bank_cards(id) ON DELETE RESTRICT,
                amount_yuan NUMERIC(10,2) NOT NULL,
                fee_yuan NUMERIC(10,2) NOT NULL,
                actual_yuan NUMERIC(10,2) NOT NULL,
                points_deducted BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reject_reason TEXT,
                reviewed_by INTEGER REFERENCES users(id),
                reviewed_at TIMESTAMP,
                paid_at TIMESTAMP,
                external_tx_ref TEXT,
                idempotency_key UUID NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_wr_user ON withdrawal_requests(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_wr_status ON withdrawal_requests(status)
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_wr_idempotency
            ON withdrawal_requests(idempotency_key)
        """)

        # KYC 扩展字段
        cursor.execute("""
            ALTER TABLE user_wallets
            ADD COLUMN IF NOT EXISTS withdrawal_kyc_verified BOOLEAN DEFAULT FALSE
        """)
        cursor.execute("""
            ALTER TABLE user_wallets
            ADD COLUMN IF NOT EXISTS withdrawal_kyc_real_name TEXT
        """)

        logger.info("[Withdrawal] 提现表初始化完成")


# ==================== 工具函数 ====================

def luhn_check(card_number: str) -> bool:
    """Luhn 算法校验银行卡号（16-19 位数字）"""
    if not card_number or not card_number.isdigit():
        return False
    n = len(card_number)
    if n < 16 or n > 19:
        return False

    total = 0
    for i, ch in enumerate(reversed(card_number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_card_number(card_number: str) -> str:
    """银行卡号脱敏: ****XXXX（最后 4 位）"""
    if not card_number or len(card_number) < 4:
        return "****"
    return f"****{card_number[-4:]}"


# ==================== 银行卡 CRUD ====================

def add_bank_card(user_id: int, card_holder: str, bank_name: str,
                  card_number: str, phone: str) -> dict:
    """添加银行卡（加密存储，第一张卡自动设为默认）"""
    if not luhn_check(card_number):
        raise HTTPException(status_code=400, detail="银行卡号格式不正确")

    encrypted = encrypt_id_card(card_number)
    card_hmac = hmac_id_card(card_number)
    mask = mask_card_number(card_number)

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 检查是否已存在相同卡号（active 状态）
        cursor.execute(
            "SELECT id FROM bank_cards WHERE card_number_hmac = %s AND status = 'active'",
            (card_hmac,)
        )
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="该银行卡已绑定")

        # 检查是否是该用户的第一张卡
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM bank_cards WHERE user_id = %s AND status = 'active'",
            (user_id,)
        )
        count = cursor.fetchone()["cnt"]
        is_default = count == 0

        cursor.execute("""
            INSERT INTO bank_cards
                (user_id, card_holder, bank_name, card_number_encrypted,
                 card_number_hmac, card_number_mask, phone, is_default)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, user_id, card_holder, bank_name, card_number_mask,
                      phone, is_default, status, created_at
        """, (user_id, card_holder, bank_name, encrypted, card_hmac, mask, phone, is_default))
        conn.commit()
        return dict(cursor.fetchone())
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"[Withdrawal] 添加银行卡失败 user_id={user_id}: {e}")
        raise
    finally:
        conn.close()


def list_bank_cards(user_id: int) -> list:
    """列出用户所有有效银行卡（不返回加密数据）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, card_holder, bank_name, card_number_mask,
                   phone, is_default, status, created_at
            FROM bank_cards
            WHERE user_id = %s AND status = 'active'
            ORDER BY is_default DESC, created_at DESC
        """, (user_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def delete_bank_card(user_id: int, card_id: int) -> bool:
    """软删除银行卡（status='disabled'），有 pending/approved 提现的卡不能删"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 验证卡属于该用户且为 active
        cursor.execute(
            "SELECT id, is_default FROM bank_cards WHERE id = %s AND user_id = %s AND status = 'active'",
            (card_id, user_id)
        )
        card = cursor.fetchone()
        if not card:
            raise HTTPException(status_code=404, detail="银行卡不存在或已删除")

        # 检查是否有 pending/approved 提现
        cursor.execute("""
            SELECT COUNT(*) AS cnt FROM withdrawal_requests
            WHERE bank_card_id = %s AND status IN ('pending', 'approved')
        """, (card_id,))
        if cursor.fetchone()["cnt"] > 0:
            raise HTTPException(
                status_code=409,
                detail="该银行卡有进行中的提现申请，无法删除"
            )

        cursor.execute("""
            UPDATE bank_cards
            SET status = 'disabled', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND user_id = %s
        """, (card_id, user_id))

        # 如果删的是默认卡，把最早创建的 active 卡设为默认
        if card["is_default"]:
            cursor.execute("""
                UPDATE bank_cards
                SET is_default = TRUE, updated_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT id FROM bank_cards
                    WHERE user_id = %s AND status = 'active'
                    ORDER BY created_at ASC
                    LIMIT 1
                )
            """, (user_id,))

        conn.commit()
        return True
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"[Withdrawal] 删除银行卡失败 user_id={user_id} card_id={card_id}: {e}")
        raise
    finally:
        conn.close()


def set_default_card(user_id: int, card_id: int) -> bool:
    """设置默认银行卡（取消所有其他默认，设当前卡为默认）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 验证卡属于该用户且为 active
        cursor.execute(
            "SELECT id FROM bank_cards WHERE id = %s AND user_id = %s AND status = 'active'",
            (card_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="银行卡不存在或已删除")

        cursor.execute("""
            UPDATE bank_cards SET is_default = FALSE, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND status = 'active'
        """, (user_id,))

        cursor.execute("""
            UPDATE bank_cards SET is_default = TRUE, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND user_id = %s
        """, (card_id, user_id))

        conn.commit()
        return True
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"[Withdrawal] 设置默认卡失败 user_id={user_id} card_id={card_id}: {e}")
        raise
    finally:
        conn.close()


# ==================== 提现申请 ====================

def create_withdrawal(user_id: int, amount_yuan: Decimal, bank_card_id: int,
                      idempotency_key: str) -> dict:
    """创建提现申请（核心事务）

    流程:
    1. 幂等检查（相同 idempotency_key 直接返回已有记录）
    2. 校验银行卡归属 + 有效
    3. 校验 pending/approved 数量 < MAX_PENDING_COUNT
    4. SELECT ... FOR UPDATE 锁定钱包
    5. 校验 commission_points >= amount_yuan * POINTS_PER_YUAN
    6. 冻结: commission_points -= X, frozen_points += X
    7. 写 withdrawal_freeze 流水
    8. 插入 withdrawal_requests
    9. 返回结果
    """
    amount_yuan = Decimal(str(amount_yuan))
    if amount_yuan < MIN_WITHDRAWAL_YUAN:
        raise HTTPException(
            status_code=400,
            detail=f"最低提现金额 {MIN_WITHDRAWAL_YUAN} 元"
        )

    fee_yuan = (amount_yuan * FEE_RATE).quantize(Decimal("0.01"))
    actual_yuan = amount_yuan - fee_yuan
    points_deducted = int(amount_yuan * POINTS_PER_YUAN)

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 1. 幂等检查
        cursor.execute(
            "SELECT * FROM withdrawal_requests WHERE idempotency_key = %s",
            (idempotency_key,)
        )
        existing = cursor.fetchone()
        if existing:
            existing = dict(existing)
            _enqueue_legacy_withdrawal_terminal(cursor, existing, "submitted")
            conn.commit()
            return existing

        # 2. 校验银行卡
        cursor.execute(
            "SELECT id FROM bank_cards WHERE id = %s AND user_id = %s AND status = 'active'",
            (bank_card_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="银行卡不存在或已失效")

        # 3. 校验 pending/approved 数量
        cursor.execute("""
            SELECT COUNT(*) AS cnt FROM withdrawal_requests
            WHERE user_id = %s AND status IN ('pending', 'approved')
        """, (user_id,))
        pending_count = cursor.fetchone()["cnt"]
        if pending_count >= MAX_PENDING_COUNT:
            raise HTTPException(
                status_code=429,
                detail=f"最多同时有 {MAX_PENDING_COUNT} 笔提现申请"
            )

        # 4. 锁定钱包
        cursor.execute(
            "SELECT * FROM user_wallets WHERE user_id = %s FOR UPDATE",
            (user_id,)
        )
        wallet = cursor.fetchone()
        if not wallet:
            raise HTTPException(status_code=404, detail="钱包不存在")

        # 5. 校验余额
        commission = wallet["commission_points"] or 0
        if commission < points_deducted:
            raise HTTPException(
                status_code=400,
                detail=f"佣金积分不足（需要 {points_deducted}，当前 {commission}）"
            )

        # 6. 冻结积分
        cursor.execute("""
            UPDATE user_wallets
            SET commission_points = commission_points - %s,
                frozen_points = frozen_points + %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            RETURNING commission_points, frozen_points
        """, (points_deducted, points_deducted, user_id))
        updated_wallet = cursor.fetchone()

        # 7. 先插提现申请拿到 ID
        cursor.execute("""
            INSERT INTO withdrawal_requests
                (user_id, bank_card_id, amount_yuan, fee_yuan, actual_yuan,
                 points_deducted, idempotency_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (user_id, bank_card_id, amount_yuan, fee_yuan, actual_yuan,
              points_deducted, idempotency_key))
        result = dict(cursor.fetchone())

        # 8. 写提现流水（用 order_id 关联，打款时更新这条）
        insert_transaction(
            cursor, user_id,
            tx_type="withdrawal_freeze",
            point_type="commission",
            amount=-points_deducted,
            balance_after=updated_wallet["commission_points"],
            description=f"提现申请 ¥{amount_yuan}（手续费 ¥{fee_yuan}，到账 ¥{actual_yuan}，审核中）",
            order_id=f"withdrawal_{result['id']}"
        )
        _enqueue_legacy_withdrawal_terminal(cursor, result, "submitted")
        conn.commit()
        return result

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"[Withdrawal] 创建提现失败 user_id={user_id}: {e}")
        raise
    finally:
        conn.close()


# ==================== 提现查询 ====================

def list_withdrawals(user_id: int, status_filter: str = None,
                     limit: int = 20, offset: int = 0) -> list:
    """查询用户提现记录（join bank_cards 获取展示信息）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        sql = """
            SELECT wr.*, bc.bank_name, bc.card_number_mask, bc.card_holder
            FROM withdrawal_requests wr
            JOIN bank_cards bc ON bc.id = wr.bank_card_id
            WHERE wr.user_id = %s
        """
        params = [user_id]

        if status_filter:
            sql += " AND wr.status = %s"
            params.append(status_filter)

        sql += " ORDER BY wr.created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cursor.execute(sql, params)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


# ==================== 管理端 ====================

def admin_list_withdrawals(status_filter: str = None,
                           limit: int = 20, offset: int = 0) -> dict:
    """管理端提现列表（含 pending_count + month_paid_total + 用户信息）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # pending 计数
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM withdrawal_requests WHERE status = 'pending'"
        )
        pending_count = cursor.fetchone()["cnt"]

        # 本月已打款总额
        cursor.execute("""
            SELECT COALESCE(SUM(actual_yuan), 0) AS total
            FROM withdrawal_requests
            WHERE status = 'paid'
              AND paid_at >= date_trunc('month', CURRENT_TIMESTAMP)
        """)
        month_paid_total = cursor.fetchone()["total"]

        # 列表
        sql = """
            SELECT wr.*, bc.bank_name, bc.card_number_mask, bc.card_holder,
                   u.username, u.phone AS user_phone
            FROM withdrawal_requests wr
            JOIN bank_cards bc ON bc.id = wr.bank_card_id
            JOIN users u ON u.id = wr.user_id
            WHERE 1=1
        """
        params = []

        if status_filter:
            sql += " AND wr.status = %s"
            params.append(status_filter)

        sql += " ORDER BY wr.created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cursor.execute(sql, params)
        items = [dict(r) for r in cursor.fetchall()]

        return {
            "pending_count": pending_count,
            "month_paid_total": float(month_paid_total),
            "items": items,
        }
    finally:
        conn.close()


def admin_approve(withdrawal_id: int, admin_user_id: int) -> dict:
    """管理端审批通过（pending -> approved）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM withdrawal_requests WHERE id = %s FOR UPDATE",
            (withdrawal_id,)
        )
        wr = cursor.fetchone()
        if not wr:
            raise HTTPException(status_code=404, detail="提现申请不存在")

        if wr["status"] == "approved":
            _enqueue_legacy_withdrawal_terminal(cursor, dict(wr), "approved")
            conn.commit()
            return dict(wr)  # 幂等

        if wr["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"当前状态 {wr['status']}，无法审批通过"
            )

        cursor.execute("""
            UPDATE withdrawal_requests
            SET status = 'approved',
                reviewed_by = %s,
                reviewed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING *
        """, (admin_user_id, withdrawal_id))

        result = dict(cursor.fetchone())
        _enqueue_legacy_withdrawal_terminal(cursor, result, "approved")
        conn.commit()
        return result
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"[Withdrawal] admin_approve 失败 id={withdrawal_id}: {e}")
        raise
    finally:
        conn.close()


def admin_reject(withdrawal_id: int, admin_user_id: int, reason: str) -> dict:
    """管理端驳回（pending -> rejected，解冻佣金积分）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM withdrawal_requests WHERE id = %s FOR UPDATE",
            (withdrawal_id,)
        )
        wr = cursor.fetchone()
        if not wr:
            raise HTTPException(status_code=404, detail="提现申请不存在")

        if wr["status"] == "rejected":
            _enqueue_legacy_withdrawal_terminal(
                cursor, dict(wr), "rejected", str(wr.get("reject_reason") or "")
            )
            conn.commit()
            return dict(wr)  # 幂等

        if wr["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"当前状态 {wr['status']}，无法驳回"
            )

        user_id = wr["user_id"]
        points = wr["points_deducted"]

        # 解冻: frozen_points -= X, commission_points += X
        cursor.execute("""
            UPDATE user_wallets
            SET frozen_points = frozen_points - %s,
                commission_points = commission_points + %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            RETURNING commission_points
        """, (points, points, user_id))
        updated_wallet = cursor.fetchone()

        # 更新原来的冻结流水：改类型 + 改金额（退回）+ 改描述
        cursor.execute("""
            UPDATE point_transactions
            SET type = 'withdrawal_reject',
                amount = %s,
                balance_after = %s,
                description = %s
            WHERE user_id = %s AND order_id = %s AND type = 'withdrawal_freeze'
        """, (
            points,
            updated_wallet["commission_points"],
            f"提现已退回 ¥{wr['amount_yuan']}（原因: {reason}）",
            user_id,
            f"withdrawal_{withdrawal_id}"
        ))

        # 更新提现状态
        cursor.execute("""
            UPDATE withdrawal_requests
            SET status = 'rejected',
                reject_reason = %s,
                reviewed_by = %s,
                reviewed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING *
        """, (reason, admin_user_id, withdrawal_id))

        result = dict(cursor.fetchone())
        _enqueue_legacy_withdrawal_terminal(cursor, result, "rejected", reason)
        conn.commit()
        return result
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"[Withdrawal] admin_reject 失败 id={withdrawal_id}: {e}")
        raise
    finally:
        conn.close()


def admin_mark_paid(withdrawal_id: int, admin_user_id: int,
                    external_tx_ref: str) -> dict:
    """管理端确认打款（approved -> paid，扣减 frozen_points）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM withdrawal_requests WHERE id = %s FOR UPDATE",
            (withdrawal_id,)
        )
        wr = cursor.fetchone()
        if not wr:
            raise HTTPException(status_code=404, detail="提现申请不存在")

        if wr["status"] == "paid":
            _enqueue_legacy_withdrawal_terminal(cursor, dict(wr), "paid")
            conn.commit()
            return dict(wr)  # 幂等

        if wr["status"] != "approved":
            raise HTTPException(
                status_code=409,
                detail=f"当前状态 {wr['status']}，需先审批通过才能打款"
            )

        user_id = wr["user_id"]
        points = wr["points_deducted"]

        # 扣减冻结积分（真正消耗）
        cursor.execute("""
            UPDATE user_wallets
            SET frozen_points = frozen_points - %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
            RETURNING frozen_points
        """, (points, user_id))
        updated_wallet = cursor.fetchone()

        # 更新原来的冻结流水：改类型 + 改描述（合并为一条记录）
        cursor.execute("""
            UPDATE point_transactions
            SET type = 'withdrawal_paid',
                description = %s
            WHERE user_id = %s AND order_id = %s AND type = 'withdrawal_freeze'
        """, (
            f"提现到账 ¥{wr['actual_yuan']}（流水号: {external_tx_ref}）",
            user_id,
            f"withdrawal_{withdrawal_id}"
        ))

        # 更新提现状态
        cursor.execute("""
            UPDATE withdrawal_requests
            SET status = 'paid',
                external_tx_ref = %s,
                reviewed_by = %s,
                paid_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING *
        """, (external_tx_ref, admin_user_id, withdrawal_id))

        result = dict(cursor.fetchone())
        _enqueue_legacy_withdrawal_terminal(cursor, result, "paid")
        conn.commit()
        return result
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"[Withdrawal] admin_mark_paid 失败 id={withdrawal_id}: {e}")
        raise
    finally:
        conn.close()


# ==================== KYC ====================

def get_withdrawal_kyc_status(user_id: int) -> dict:
    """获取提现 KYC 状态

    优先从 agent_applications 取已审核通过的实名信息，
    其次看 user_wallets.withdrawal_kyc_* 字段。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 优先: agent_applications 已审核通过
        cursor.execute("""
            SELECT real_name, status FROM agent_applications
            WHERE user_id = %s AND status = 'approved'
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,))
        app = cursor.fetchone()
        if app:
            return {
                "kyc_verified": True,
                "real_name": app["real_name"],
                "source": "agent_application",
            }

        # 其次: user_wallets.withdrawal_kyc_*
        cursor.execute("""
            SELECT withdrawal_kyc_verified, withdrawal_kyc_real_name
            FROM user_wallets WHERE user_id = %s
        """, (user_id,))
        wallet = cursor.fetchone()
        if wallet and wallet.get("withdrawal_kyc_verified"):
            return {
                "kyc_verified": True,
                "real_name": wallet["withdrawal_kyc_real_name"],
                "source": "withdrawal_kyc",
            }

        return {
            "kyc_verified": False,
            "real_name": None,
            "source": None,
        }
    finally:
        conn.close()


def save_withdrawal_kyc(user_id: int, real_name: str):
    """保存提现 KYC 实名信息"""
    if not real_name or not real_name.strip():
        raise HTTPException(status_code=400, detail="真实姓名不能为空")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_wallets
            SET withdrawal_kyc_verified = TRUE,
                withdrawal_kyc_real_name = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (real_name.strip(), user_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[Withdrawal] save_withdrawal_kyc 失败 user_id={user_id}: {e}")
        raise
    finally:
        conn.close()
