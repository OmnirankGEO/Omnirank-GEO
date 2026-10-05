"""代理工作台试用通行证 API (v3.2 Phase 3)

L0 用户想体验代理工作台 → 申请 → 代理审批 → 24 小时试用
规则:
  - 费用 260 积分（从 L0 用户自己的积分扣）
  - 代理批准后：
    · 100 积分给审批代理（带看佣金）
    · 160 积分归平台
  - 代理拒绝：全额退还 L0 用户
  - 24 小时后自动过期

端点:
  POST /api/trial-pass/apply        L0 用户申请（冻结积分）
  POST /api/trial-pass/{id}/approve 代理批准（扣积分 + 发奖 + 激活）
  POST /api/trial-pass/{id}/reject  代理拒绝（退积分）
  GET  /api/trial-pass/my-active    L0 查自己当前试用
  GET  /api/trial-pass/pending-review 代理查待审批列表
"""

import logging
import hashlib
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

logger = logging.getLogger("GEO-TrialPass-API")
router = APIRouter(prefix="/api/trial-pass", tags=["代理试用"])

TRIAL_COST_POINTS = 260
ISSUER_REWARD_POINTS = 100
TRIAL_DURATION_HOURS = 24


def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _enqueue_trial_event(
    cursor,
    *,
    event_type,
    trial_id: int,
    terminal_state: str,
    recipient_user_id: int,
    recipient_kind,
    status: str,
    summary: str = "",
) -> None:
    """Persist one trial terminal/action event in the caller's transaction."""
    from services.notification_outbox import enqueue_notification_event

    public_ref = hashlib.sha256(f"trial:{int(trial_id)}".encode("utf-8")).hexdigest()[:10].upper()
    enqueue_notification_event(
        cursor,
        event_type=event_type,
        business_id=str(int(trial_id)),
        terminal_state=terminal_state,
        recipient_user_id=int(recipient_user_id),
        recipient_kind=recipient_kind,
        facts={
            "business_no": f"TRY-{public_ref}",
            "status": status,
            "occurred_at": datetime.utcnow().isoformat(timespec="seconds"),
            "summary": summary,
        },
    )


# ==================== 请求模型 ====================

class ApplyRequest(BaseModel):
    # 目前不需要额外参数，自动查用户的直推代理
    reason: Optional[str] = Field(None, max_length=200)


class RejectRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=200)


# ==================== 端点 ====================

@router.post("/apply")
async def apply_trial_pass(req: ApplyRequest, request: Request):
    """L0 用户申请 1 天代理工作台试用

    流程:
      1. 检查用户是 L0（已经是代理的不能申请）
      2. 查直推代理（必须有推荐人）
      3. 冻结用户 260 积分
      4. 创建 pending 试用记录
      5. 通知代理（站内信/push）
    """
    from db.connection import get_connection
    from db.wallet_db import get_wallet_balance, insert_transaction

    user = _get_user(request)
    user_id = user["user_id"]

    # 检查用户等级
    wallet = get_wallet_balance(user_id) or {}
    if wallet.get("agent_level", 0) >= 1:
        raise HTTPException(status_code=400, detail="您已是服务商，无需申请试用")

    # 检查余额
    total = wallet.get("paid_points", 0) + wallet.get("bonus_points", 0)
    if total < TRIAL_COST_POINTS:
        raise HTTPException(
            status_code=402,
            detail=f"积分不足（当前 {total}，需要 {TRIAL_COST_POINTS}），请先充值"
        )

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # [GEO-R10-CAN-012] 锁定用户钱包行,串行化同一用户的并发 apply。
        # 原逻辑"查已有 pending → 扣积分 → 插 pending"是 check-then-act,两个并发请求
        # 会都通过 existing 检查各插一条 pending(idx_trial_passes_recipient 非唯一,DB 不兜)。
        # 钱包行是每次 apply 都要 UPDATE 的行,拿它做串行点:第二个请求阻塞到第一个提交后,
        # existing 检查即可看到已插入的 pending 而正确拒绝。
        cursor.execute(
            "SELECT user_id FROM user_wallets WHERE user_id = %s FOR UPDATE",
            (user_id,)
        )

        # 查直推代理
        cursor.execute("""
            SELECT referrer_id FROM referral_links
            WHERE referred_id = %s AND level = 1
        """, (user_id,))
        ref = cursor.fetchone()
        if not ref:
            raise HTTPException(
                status_code=400,
                detail="您未绑定推荐人，无法申请试用。请联系客服。"
            )

        issuer_user_id = ref["referrer_id"]

        # 检查是否已有 pending / active 试用
        cursor.execute("""
            SELECT id, status FROM trial_passes
            WHERE recipient_user_id = %s
              AND status IN ('pending', 'approved', 'active')
            LIMIT 1
        """, (user_id,))
        existing = cursor.fetchone()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"已有进行中的试用申请（状态：{existing['status']}），请勿重复申请"
            )

        # 冻结积分（优先扣 bonus，不够扣 paid）
        bonus_deduct = min(wallet.get("bonus_points", 0), TRIAL_COST_POINTS)
        paid_deduct = TRIAL_COST_POINTS - bonus_deduct

        # [GEO-R10-CAN-013] 先建试用记录拿 trial_id,再冻结积分,并把 bonus/paid 拆分
        # 通过 point_transactions.order_id='trial:{id}' 标记落库(现有 trial_passes schema
        # 无 held_bonus/held_paid 拆分列,借 order_id 作 hold ledger,与 recharge 的 join 不冲突)。
        # reject 时按此原样退回各自资金池,防止不可退现的 bonus 被洗成可原路退款/可提现的 paid。
        cursor.execute("""
            INSERT INTO trial_passes
                (recipient_user_id, issuer_user_id, cost_points,
                 issuer_reward_points, platform_keep_points, points_held, status)
            VALUES (%s, %s, %s, %s, %s, TRUE, 'pending')
            RETURNING id
        """, (user_id, issuer_user_id, TRIAL_COST_POINTS,
              ISSUER_REWARD_POINTS, TRIAL_COST_POINTS - ISSUER_REWARD_POINTS))
        trial_id = cursor.fetchone()["id"]
        _hold_ref = f"trial:{trial_id}"

        cursor.execute("""
            UPDATE user_wallets
            SET bonus_points = bonus_points - %s,
                paid_points = paid_points - %s,
                updated_at = NOW()
            WHERE user_id = %s
            RETURNING paid_points, bonus_points
        """, (bonus_deduct, paid_deduct, user_id))
        new_wallet = cursor.fetchone()

        # 流水（标记为冻结，不是真正消费；order_id 标 trial:{id} 便于 reject 精确退回来源池）
        insert_transaction(
            cursor, user_id, "trial_hold", "bonus",
            -bonus_deduct, new_wallet["bonus_points"],
            order_id=_hold_ref,
            description=f"申请工作台试用冻结 {TRIAL_COST_POINTS} 积分"
        )
        if paid_deduct > 0:
            insert_transaction(
                cursor, user_id, "trial_hold", "paid",
                -paid_deduct, new_wallet["paid_points"],
                order_id=_hold_ref,
                description=f"申请工作台试用冻结 {paid_deduct} 充值积分"
            )

        from services.notification_events import NotificationEventType, RecipientKind
        _enqueue_trial_event(
            cursor,
            event_type=NotificationEventType.TRIAL_REVIEW_REQUIRED,
            trial_id=trial_id,
            terminal_state="pending_review",
            recipient_user_id=issuer_user_id,
            recipient_kind=RecipientKind.AGENT,
            status="等待审批",
            summary="有新的工作台试用申请需要处理。",
        )

        conn.commit()

        logger.info(
            f"[TrialPass] user={user_id} 申请试用 -> issuer={issuer_user_id} "
            f"trial_id={trial_id}"
        )

        return {
            "success": True,
            "trial_id": trial_id,
            "status": "pending",
            "message": "申请已提交，等待服务方审批（一般 24 小时内）",
            "cost_points": TRIAL_COST_POINTS,
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.exception(f"[TrialPass/apply] 失败: {e}")
        raise HTTPException(status_code=500, detail=f"申请失败: {e}")
    finally:
        conn.close()


@router.post("/{trial_id}/approve")
async def approve_trial_pass(trial_id: int, request: Request):
    """代理批准试用

    流程:
      1. 校验当前用户是 issuer
      2. 更新 status='active'，设 expires_at = now + 24h
      3. 100 积分发给 issuer（带看奖）
      4. 160 积分归平台
    """
    from db.connection import get_connection
    from db.wallet_db import insert_transaction

    user = _get_user(request)
    issuer_id = user["user_id"]

    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, recipient_user_id, issuer_user_id, cost_points,
                   issuer_reward_points, status
            FROM trial_passes WHERE id = %s FOR UPDATE
        """, (trial_id,))
        trial = cursor.fetchone()

        if not trial:
            raise HTTPException(status_code=404, detail="试用记录不存在")

        if trial["issuer_user_id"] != issuer_id and not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="您无权审批该申请")

        if trial["status"] != "pending":
            raise HTTPException(status_code=400, detail=f"该申请已处理（{trial['status']}）")

        now = datetime.utcnow()
        expires_at = now + timedelta(hours=TRIAL_DURATION_HOURS)

        # 更新试用状态
        cursor.execute("""
            UPDATE trial_passes
            SET status = 'active', reviewed_at = %s, activated_at = %s, expires_at = %s
            WHERE id = %s
        """, (now, now, expires_at, trial_id))

        # [GEO-R10-CAN-014] 带看奖必须发给真正的发起代理(trial.issuer_user_id),
        # 而不是当前操作人(issuer_id)。授权处允许 admin 代其他 issuer 审批,若按 issuer_id
        # 发奖,admin 代批时 100 积分会被错记到 admin 头上,佣金归属被污染。
        reward = trial["issuer_reward_points"]
        reward_recipient_id = trial["issuer_user_id"]
        # 确保真 issuer 的钱包行存在(admin 代批时该代理可能从未初始化钱包 → RETURNING 空会崩)
        cursor.execute(
            "INSERT INTO user_wallets (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (reward_recipient_id,)
        )
        cursor.execute("""
            UPDATE user_wallets
            SET paid_points = paid_points + %s, updated_at = NOW()
            WHERE user_id = %s
            RETURNING paid_points
        """, (reward, reward_recipient_id))
        agent_wallet = cursor.fetchone()

        insert_transaction(
            cursor, reward_recipient_id, "trial_reward", "paid",
            reward, agent_wallet["paid_points"],
            description=(
                f"批准用户试用带看奖 trial_id={trial_id}"
                + (f"（由管理员 {issuer_id} 代审）" if reward_recipient_id != issuer_id else "")
            )
        )

        from services.notification_events import NotificationEventType, RecipientKind
        _enqueue_trial_event(
            cursor,
            event_type=NotificationEventType.TRIAL_ACTIVATED,
            trial_id=trial_id,
            terminal_state="active",
            recipient_user_id=trial["recipient_user_id"],
            recipient_kind=RecipientKind.USER,
            status="试用已开通",
            summary=f"工作台可使用至 {expires_at.strftime('%m-%d %H:%M')}。",
        )

        conn.commit()

        logger.info(
            f"[TrialPass/approve] trial_id={trial_id} issuer={issuer_id} "
            f"recipient={trial['recipient_user_id']} reward={reward}"
        )

        return {
            "success": True,
            "trial_id": trial_id,
            "status": "active",
            "expires_at": expires_at.isoformat(),
            "reward_points": reward,
            "message": f"试用已激活，将于 {expires_at.strftime('%m-%d %H:%M')} 过期",
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.exception(f"[TrialPass/approve] 失败: {e}")
        raise HTTPException(status_code=500, detail=f"批准失败: {e}")
    finally:
        conn.close()


@router.post("/{trial_id}/reject")
async def reject_trial_pass(trial_id: int, req: RejectRequest, request: Request):
    """代理拒绝 — 全额退还 L0 用户积分"""
    from db.connection import get_connection
    from db.wallet_db import insert_transaction

    user = _get_user(request)
    issuer_id = user["user_id"]

    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, recipient_user_id, issuer_user_id, cost_points, status
            FROM trial_passes WHERE id = %s FOR UPDATE
        """, (trial_id,))
        trial = cursor.fetchone()

        if not trial:
            raise HTTPException(status_code=404, detail="试用记录不存在")

        if trial["issuer_user_id"] != issuer_id and not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="您无权操作该申请")

        if trial["status"] != "pending":
            raise HTTPException(status_code=400, detail=f"该申请已处理（{trial['status']}）")

        # [GEO-R10-CAN-013] 退还积分给 recipient — 按申请时冻结的资金池原样退回
        # (bonus→bonus_points, paid→paid_points),不再全额退 paid。全退 paid 会把
        # 不可退现的赠送积分洗成可原路退款/可提现的充值积分,凭空创造退现权。
        # 拆分来源:apply 时用 order_id='trial:{id}' 标记的 trial_hold 流水。
        refund = trial["cost_points"]
        cursor.execute("""
            SELECT point_type, COALESCE(SUM(-amount), 0) AS held
            FROM point_transactions
            WHERE user_id = %s AND type = 'trial_hold' AND order_id = %s
            GROUP BY point_type
        """, (trial["recipient_user_id"], f"trial:{trial_id}"))
        held = {r["point_type"]: int(r["held"] or 0) for r in cursor.fetchall()}
        bonus_refund = held.get("bonus", 0)
        paid_refund = held.get("paid", 0)
        # 兼容本次修复前创建的旧试用(无 trial:{id} 拆分标记)→ 保底全额退 paid(旧行为)
        if bonus_refund + paid_refund != refund:
            bonus_refund = 0
            paid_refund = refund

        cursor.execute("""
            UPDATE user_wallets
            SET bonus_points = bonus_points + %s,
                paid_points = paid_points + %s,
                updated_at = NOW()
            WHERE user_id = %s
            RETURNING paid_points, bonus_points
        """, (bonus_refund, paid_refund, trial["recipient_user_id"]))
        recipient_wallet = cursor.fetchone()

        if bonus_refund > 0:
            insert_transaction(
                cursor, trial["recipient_user_id"], "trial_refund", "bonus",
                bonus_refund, recipient_wallet["bonus_points"],
                order_id=f"trial:{trial_id}",
                description=f"试用申请被拒绝，退还 {bonus_refund} 赠送积分"
            )
        if paid_refund > 0:
            insert_transaction(
                cursor, trial["recipient_user_id"], "trial_refund", "paid",
                paid_refund, recipient_wallet["paid_points"],
                order_id=f"trial:{trial_id}",
                description=f"试用申请被拒绝，退还 {paid_refund} 积分"
            )

        # 更新状态
        cursor.execute("""
            UPDATE trial_passes
            SET status = 'rejected', reviewed_at = NOW(), reject_reason = %s
            WHERE id = %s
        """, (req.reason or "", trial_id))

        from services.notification_events import NotificationEventType, RecipientKind
        _enqueue_trial_event(
            cursor,
            event_type=NotificationEventType.TRIAL_REJECTED,
            trial_id=trial_id,
            terminal_state="rejected",
            recipient_user_id=trial["recipient_user_id"],
            recipient_kind=RecipientKind.USER,
            status="申请未通过，算力已退回",
            summary=f"已退回 {refund} 算力。{req.reason or ''}",
        )

        conn.commit()

        logger.info(
            f"[TrialPass/reject] trial_id={trial_id} refund={refund} to={trial['recipient_user_id']}"
        )

        return {
            "success": True,
            "trial_id": trial_id,
            "status": "rejected",
            "refunded_points": refund,
            "message": "已拒绝申请，积分已全额退还",
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.exception(f"[TrialPass/reject] 失败: {e}")
        raise HTTPException(status_code=500, detail=f"操作失败: {e}")
    finally:
        conn.close()


@router.get("/my-active")
async def get_my_active_trial(request: Request):
    """L0 查询自己当前的试用状态（前端顶部倒计时 banner 用）"""
    from db.connection import get_connection

    user = _get_user(request)
    user_id = user["user_id"]

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, status, activated_at, expires_at
            FROM trial_passes
            WHERE recipient_user_id = %s
              AND status IN ('active', 'pending', 'approved')
            ORDER BY applied_at DESC
            LIMIT 1
        """, (user_id,))
        row = cursor.fetchone()

        if not row:
            return {"has_active": False}

        return {
            "has_active": True,
            "trial_id": row["id"],
            "status": row["status"],
            "activated_at": row["activated_at"].isoformat() if row["activated_at"] else None,
            "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        }
    finally:
        conn.close()


@router.get("/pending-review")
async def list_pending_review(request: Request):
    """代理查待审批的试用申请列表"""
    from db.connection import get_connection

    user = _get_user(request)
    issuer_id = user["user_id"]

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT tp.id, tp.recipient_user_id, tp.cost_points,
                   tp.issuer_reward_points, tp.applied_at,
                   u.username, u.display_name, u.created_at AS recipient_registered_at,
                   uw.paid_points + uw.bonus_points AS recipient_balance
            FROM trial_passes tp
            JOIN users u ON u.id = tp.recipient_user_id
            LEFT JOIN user_wallets uw ON uw.user_id = tp.recipient_user_id
            WHERE tp.issuer_user_id = %s AND tp.status = 'pending'
            ORDER BY tp.applied_at DESC
        """, (issuer_id,))
        rows = cursor.fetchall()

        records = []
        for r in rows:
            records.append({
                "trial_id": r["id"],
                "recipient_user_id": r["recipient_user_id"],
                "recipient_name": r["display_name"] or r["username"],
                "recipient_registered_at": r["recipient_registered_at"].isoformat() if r.get("recipient_registered_at") else None,
                "recipient_balance": r["recipient_balance"] or 0,
                "cost_points": r["cost_points"],
                "reward_points": r["issuer_reward_points"],
                "applied_at": r["applied_at"].isoformat() if r["applied_at"] else None,
            })

        return {"records": records, "count": len(records)}
    finally:
        conn.close()


# ==================== 定时任务：清理过期试用 ====================

def expire_active_trials():
    """清理过期试用 — 由 scheduler 每 5 分钟调用

    所有 status='active' 且 expires_at <= NOW() 的记录
    → 自动转 status='expired'
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE trial_passes
            SET status = 'expired'
            WHERE status = 'active' AND expires_at <= NOW()
            RETURNING id, recipient_user_id
        """)
        expired = cursor.fetchall()
        if expired:
            from services.notification_events import NotificationEventType, RecipientKind
            for row in expired:
                _enqueue_trial_event(
                    cursor,
                    event_type=NotificationEventType.TRIAL_EXPIRED,
                    trial_id=row["id"],
                    terminal_state="expired",
                    recipient_user_id=row["recipient_user_id"],
                    recipient_kind=RecipientKind.USER,
                    status="试用已结束",
                    summary="如需继续使用，请在套餐页面查看可用方案。",
                )
        conn.commit()

        if expired:
            logger.info(f"[TrialPass/expire] 清理了 {len(expired)} 条过期试用")
        return len(expired)
    except Exception as e:
        conn.rollback()
        logger.error(f"[TrialPass/expire] 失败: {e}")
        return 0
    finally:
        conn.close()
