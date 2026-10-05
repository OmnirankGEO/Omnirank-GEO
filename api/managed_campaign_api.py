"""
v3.3 / v3.4 GEO 全自动托管 API

端点清单（14 个）:
  v3.3 单词托管:
    POST  /api/managed/estimate              估算（不扣费）
    POST  /api/managed/confirm-recharge      充值并启动套餐（扣算力）
    GET   /api/managed/{id}                  套餐详情
    GET   /api/managed/                      用户套餐列表
    PATCH /api/managed/{id}/adjust           对话式调方案
    POST  /api/managed/{id}/confirm-adjust   确认调方案（再次扣费）
    POST  /api/managed/{id}/pause            暂停
    POST  /api/managed/{id}/resume           恢复（含报价过期重评估）
    POST  /api/managed/{id}/top-up           加充
    POST  /api/managed/{id}/withdraw-pending 24h 撤回未发文章

  v3.4 全品牌托管:
    POST  /api/managed/brand/estimate        多关键词品牌套餐估算
    POST  /api/managed/brand/confirm-recharge 创建品牌套餐
    GET   /api/managed/brand/{brand_id}/dashboard

  待审 / 撤回:
    POST  /api/managed/review/{review_id}/approve
    POST  /api/managed/review/{review_id}/reject
    GET   /api/managed/reviews/pending       用户所有套餐的待审列表

  管理员:
    POST  /api/admin/managed/{id}/inspect
    POST  /api/admin/managed/dormancy-scan

约定:
  - 严格遵守 v2 决策（充值即消费不退款，报价 3 天有效期，同词唯一）
  - 所有用户操作落 user_action_logs（管理员可审计）
  - 每个端点都校验 estimate_valid_until


[R3-P9 ⑧ · 小榜线越域改动 · 共享主人翁制]
本文件属 GEO AI 独占域。小榜线只改了**文案**:「充值额度 / 工具额度 / 积分」
→ 算力口径(资金 SSOT `docs/SYSTEM_TRUTH/08_billing.md`:全站统一「算力」,
禁「积分 / 额度」;四语义域裁定 `docs/xiaobang/vnext/RULING_TERMINOLOGY_EDU_2026-08-18.md`
② 域池名「永远是错的,不看上下文」)。零逻辑改动:字段名 / 变量 / 状态码 / 计算全部未动,
改的只有 message 字符串与同段 docstring/日志措辞。
已在 `docs/AI-CONTEXT/AI_COORDINATION_LOG.md` 通告 GEO 线。
"""

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("GEO-Managed-API")
router = APIRouter(prefix="/api/managed", tags=["GEO 托管"])
admin_router = APIRouter(prefix="/api/admin/managed", tags=["GEO 托管(管理员)"])


# ============================================================
# 工具函数
# ============================================================

def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _get_admin(request: Request) -> dict:
    user = _get_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP（X-Forwarded-For 优先）"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _log_user_action(
    user_id: int,
    action_type: str,
    campaign_id: Optional[int] = None,
    before_state: Optional[dict] = None,
    after_state: Optional[dict] = None,
    ip: str = "unknown",
    user_agent: str = "",
):
    """落用户操作日志（user_action_logs）

    注：现有 user_action_logs schema (migration_v3_2_c_end.sql:47):
      user_id / action_type / action_detail JSONB / ai_response /
      user_confirmed_at / ip_address / user_agent / created_at
    我们把 campaign_id / before_state / after_state 合并到 action_detail
    """
    from db.connection import get_db
    detail = {
        "campaign_id": campaign_id,
        "before_state": before_state,
        "after_state": after_state,
    }
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_action_logs (
                  user_id, action_type, action_detail,
                  user_confirmed_at, ip_address, user_agent
                )
                VALUES (%s, %s, %s, NOW(), %s, %s)
            """, (
                user_id, action_type,
                json.dumps(detail, default=str),
                ip, user_agent,
            ))
    except Exception as e:
        # 日志失败不阻塞主流程
        logger.warning(f"[Managed] 操作日志写入失败: {e}")


def _is_estimate_valid(campaign: dict) -> bool:
    """检查套餐报价是否还在 3 天有效期内"""
    valid_until = campaign.get("estimate_valid_until")
    if not valid_until:
        return False
    if isinstance(valid_until, str):
        valid_until = datetime.fromisoformat(valid_until)
    return datetime.now() < valid_until.replace(tzinfo=None) if valid_until.tzinfo else datetime.now() < valid_until


def _check_balance_and_deduct_paid_points(
    user_id: int,
    amount_yuan: float,
    feature_code: str,
    description: str,
    order_id: Optional[str] = None,
) -> tuple[int, dict, Optional[int]]:
    """
    托管充值：扣 user_wallets.paid_points（不走 bonus，避免赠送算力被吞）

    Returns:
        (deducted_points, new_wallet, charge_tx_id)
        # [GEO-R2-CAN-039 v3] 精确退款透传：charge_tx_id 为本笔 consume 流水的不可变主键
        # （insert_transaction RETURNING id），供 refund_points(charge_tx_id=...) 按 order_id 组精确退款。

    Raises:
        HTTPException 402: 充值算力不足
    """
    from db.connection import get_db
    from db.wallet_db import get_or_create_wallet, insert_transaction

    points_per_yuan = 130
    deduct_points = int(amount_yuan * points_per_yuan)

    wallet = get_or_create_wallet(user_id)
    if wallet["paid_points"] < deduct_points:
        raise HTTPException(status_code=402, detail={
            "code": "INSUFFICIENT_PAID_POINTS",
            "message": f"充值算力不足，需要 {deduct_points} 算力",
            "required": deduct_points,
            "available_paid": wallet["paid_points"],
        })

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT paid_points FROM user_wallets WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        row = cursor.fetchone()
        if not row or row["paid_points"] < deduct_points:
            raise HTTPException(status_code=402, detail={
                "code": "INSUFFICIENT_PAID_POINTS",
                "message": "充值算力不足（并发竞争）",
            })

        cursor.execute("""
            UPDATE user_wallets
            SET paid_points = paid_points - %s, updated_at = NOW()
            WHERE user_id = %s
            RETURNING paid_points, bonus_points
        """, (deduct_points, user_id))
        new_wallet = cursor.fetchone()

        # [GEO-R2-CAN-039 v3] 精确退款透传：捕获本笔 consume 流水不可变主键作为 charge_tx_id
        _charge_tx_id = insert_transaction(
            cursor,
            user_id=user_id,
            tx_type="consume",
            point_type="paid",
            amount=-deduct_points,
            balance_after=new_wallet["paid_points"],
            feature_code=feature_code,
            description=description,
            order_id=order_id,
        )

    return deduct_points, dict(new_wallet), _charge_tx_id


async def _refund_managed_campaign_failure(
    user_id: int,
    feature_code: str,
    failure_reason: str,
    operation_label: str,
    charge_tx_id: Optional[int] = None,  # [GEO-R2-CAN-039 v3] 精确退款透传
) -> dict:
    """
    [#40] 托管套餐扣费成功但后续步骤失败时自动退款 + 站内通知。

    Args:
        user_id: 当前用户
        feature_code: 跟 _check_balance_and_deduct_paid_points 时传的一致
                      ('managed_campaign_recharge' / 'managed_brand_recharge')
        failure_reason: 失败原因（给日志和通知用）
        operation_label: 给用户看的中文操作名（"GEO 托管套餐充值" 等）
        charge_tx_id: [GEO-R2-CAN-039 v3] 本次扣费 consume 流水的不可变主键
                      （由 _check_balance_and_deduct_paid_points 返回）。传入后 refund_points
                      精确退该笔的 order_id 组，避免 newest-by-feature 并发退错笔。None 则退回旧兜底逻辑。

    Returns:
        refund 结果 dict（含 success / reason / refunded_amount 字段）
    """
    import hashlib
    from services.notification_events import (
        NotificationEventType,
        RecipientKind,
        RefundNotificationContext,
    )

    notification = None
    public_ref = ""
    if charge_tx_id is not None:
        public_ref = hashlib.sha256(
            f"managed-refund:{int(user_id)}:{int(charge_tx_id)}".encode("utf-8")
        ).hexdigest()[:10].upper()
        notification = RefundNotificationContext(
            event_type=NotificationEventType.MANAGED_CAMPAIGN_REFUNDED,
            business_id=f"managed_refund:{int(charge_tx_id)}",
            terminal_state="refunded",
            recipient_kind=RecipientKind.USER,
            business_no=f"GEO-{public_ref}",
            status="已退回",
            summary="托管任务费用已按真实退款流水退回。",
        )
    refund_result = {"success": False, "reason": "未尝试退款"}
    try:
        from middleware.billing import refund_points
        # [GEO-R2-CAN-039 v3] 精确退款透传 charge_tx_id
        refund_result = await refund_points(
            user_id, feature_code, reason=failure_reason, charge_tx_id=charge_tx_id,
            notification=notification,
        )
        if refund_result.get("success"):
            logger.info(
                f"[Managed/refund] {feature_code} 自动退款成功: "
                f"user_id={user_id} 退={refund_result.get('refunded_amount')}"
            )
        else:
            logger.warning(
                f"[Managed/refund] {feature_code} 自动退款未成功: "
                f"user_id={user_id} reason={refund_result.get('reason')}"
            )
    except Exception as exc:
        logger.exception(f"[Managed/refund] {feature_code} 退款抛异常: {exc}")
        refund_result = {"success": False, "reason": str(exc)}

    if not refund_result.get("success"):
        try:
            from datetime import timezone
            from db.connection import get_db
            from services.notification_outbox import (
                enqueue_admin_notification_events,
                enqueue_notification_event,
            )

            identity = int(charge_tx_id) if charge_tx_id is not None else hashlib.sha256(
                f"{int(user_id)}:{feature_code}:{operation_label}".encode("utf-8")
            ).hexdigest()[:16]
            if not public_ref:
                public_ref = hashlib.sha256(
                    f"managed-manual:{int(user_id)}:{identity}".encode("utf-8")
                ).hexdigest()[:10].upper()
            facts = {
                "business_no": f"GEO-{public_ref}",
                "status": "需要人工处理",
                "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "summary": "托管任务未完成，退款结果尚未确认，请勿重复提交。",
            }
            with get_db() as conn:
                cursor = conn.cursor()
                enqueue_notification_event(
                    cursor,
                    event_type=NotificationEventType.MANAGED_CAMPAIGN_MANUAL_REQUIRED,
                    business_id=f"managed_refund:{identity}",
                    terminal_state="manual_required",
                    recipient_user_id=int(user_id),
                    recipient_kind=RecipientKind.USER,
                    facts=facts,
                )
                enqueue_admin_notification_events(
                    cursor,
                    event_type=NotificationEventType.MANAGED_CAMPAIGN_MANUAL_REQUIRED,
                    business_id=f"managed_refund:{identity}",
                    terminal_state="manual_required",
                    facts=facts,
                )
        except Exception as exc:
            logger.exception("[Managed/notify] 持久人工处理通知写入失败: %s", type(exc).__name__)

    return refund_result


async def _refund_managed_campaign_partial(
    user_id: int,
    refund_yuan: float,
    feature_code: str,
    failed_keywords: list,
    operation_label: str,
    charge_tx_id: Optional[int] = None,
) -> dict:
    """[A-14-1 / #40-followup 2026-05-29] 全品牌托管多词「部分」失败 → 按比例退还失败词的钱 + 站内信。

    退款额由调用方按 (失败词成本 / 总成本) × 实扣总额 算好(refund_yuan)。
    扣的是 paid_points → 原路补 paid_points + 落 refund 流水(口径同 _check_balance_and_deduct_paid_points 反向)。
    不复用 middleware.billing.refund_points:那是按 feature_code 全额原路退,不支持部分额。
    """
    from db.connection import get_db
    from db.wallet_db import insert_transaction
    points_per_yuan = 130
    refund_points_int = int(round(refund_yuan * points_per_yuan))
    result = {"success": False, "reason": "退款额为 0", "refunded_points": 0}
    if refund_points_int <= 0:
        return result
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE user_wallets
                SET paid_points = paid_points + %s, updated_at = NOW()
                WHERE user_id = %s
                RETURNING paid_points
                """,
                (refund_points_int, user_id),
            )
            row = cursor.fetchone()
            balance_after = row["paid_points"] if row else 0
            refund_tx_id = insert_transaction(
                cursor,
                user_id=user_id,
                tx_type="refund",
                point_type="paid",
                amount=refund_points_int,
                balance_after=balance_after,
                feature_code=feature_code,
                description=f"部分关键词创建失败按比例退款（{len(failed_keywords)} 词）",
                source="balance_deduction",
            )
            import hashlib
            from datetime import timezone
            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import enqueue_notification_event

            public_ref = hashlib.sha256(
                f"managed-partial-refund:{int(user_id)}:{int(refund_tx_id)}".encode("utf-8")
            ).hexdigest()[:10].upper()
            enqueue_notification_event(
                cursor,
                event_type=NotificationEventType.MANAGED_CAMPAIGN_REFUNDED,
                business_id=f"managed_partial_refund:{int(refund_tx_id)}",
                terminal_state="refunded",
                recipient_user_id=int(user_id),
                recipient_kind=RecipientKind.USER,
                facts={
                    "business_no": f"GEO-{public_ref}",
                    "points": str(refund_points_int),
                    "status": "部分费用已退回",
                    "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "summary": "部分未完成任务的费用已按真实退款流水退回。",
                },
            )
        result = {"success": True, "refunded_points": refund_points_int, "refunded_yuan": round(refund_yuan, 2)}
        logger.info(
            f"[Managed/partial-refund] user={user_id} 退 {refund_points_int} 算力(¥{refund_yuan:.2f}) · 失败词={failed_keywords}"
        )
    except Exception as exc:
        logger.exception(f"[Managed/partial-refund] 退款抛异常: {exc}")
        result = {"success": False, "reason": str(exc), "refunded_points": 0}

    if not result.get("success"):
        try:
            import hashlib
            from datetime import timezone
            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import (
                enqueue_admin_notification_events,
                enqueue_notification_event,
            )

            identity = int(charge_tx_id) if charge_tx_id is not None else hashlib.sha256(
                f"{int(user_id)}:{feature_code}:{operation_label}".encode("utf-8")
            ).hexdigest()[:16]
            public_ref = hashlib.sha256(
                f"managed-partial-manual:{int(user_id)}:{identity}".encode("utf-8")
            ).hexdigest()[:10].upper()
            facts = {
                "business_no": f"GEO-{public_ref}",
                "status": "需要人工处理",
                "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "summary": "托管任务部分未完成，退款结果尚未确认，请勿重复提交。",
            }
            with get_db() as conn:
                cursor = conn.cursor()
                enqueue_notification_event(
                    cursor,
                    event_type=NotificationEventType.MANAGED_CAMPAIGN_MANUAL_REQUIRED,
                    business_id=f"managed_partial_refund:{identity}",
                    terminal_state="manual_required",
                    recipient_user_id=int(user_id),
                    recipient_kind=RecipientKind.USER,
                    facts=facts,
                )
                enqueue_admin_notification_events(
                    cursor,
                    event_type=NotificationEventType.MANAGED_CAMPAIGN_MANUAL_REQUIRED,
                    business_id=f"managed_partial_refund:{identity}",
                    terminal_state="manual_required",
                    facts=facts,
                )
        except Exception as exc:
            logger.exception("[Managed/partial-refund] 人工处理通知写入失败: %s", type(exc).__name__)

    return result


# ============================================================
# Pydantic 请求模型
# ============================================================

class EstimateRequest(BaseModel):
    keyword: str = Field(..., min_length=2, max_length=100)
    city: Optional[str] = Field(None, max_length=50)
    industry: Optional[str] = Field(None, max_length=50)
    mode: str = Field("by_target_sov", pattern="^(by_target_sov|by_budget)$")
    target_sov_pct: Optional[int] = Field(None, ge=5, le=50)
    budget_yuan: Optional[float] = Field(None, gt=0, le=100000)
    max_per_article_yuan: float = Field(200, gt=0, le=2000)
    check_frequency_per_day: int = Field(1, ge=1, le=6)


class ConfirmRechargeRequest(BaseModel):
    keyword: str = Field(..., min_length=2, max_length=100)
    brand_id: int
    target_sov_pct: int = Field(..., ge=5, le=50)
    target_display_label: str = Field(..., max_length=50)
    tier_label: str = Field(..., pattern="^(entry|standard|flagship|strong|custom)$")
    selected_amount_yuan: float = Field(..., gt=0, le=100000)
    mode: str = Field("semi_auto", pattern="^(semi_auto|full_auto)$")
    max_per_article_yuan: float = Field(200, gt=0, le=2000)
    check_frequency_per_day: int = Field(1, ge=1, le=6)
    estimate_quoted_at: str  # ISO 时间戳，用于校验报价是否过期
    current_plan: Optional[dict] = None
    agreement_consent: bool
    brand_voice_consent: bool
    agreement_version: str = Field("v3.3-2026-04", max_length=20)


class AdjustRequest(BaseModel):
    adjustment_text: Optional[str] = Field(None, max_length=500)
    new_target_sov_pct: Optional[int] = Field(None, ge=5, le=50)
    new_check_frequency_per_day: Optional[int] = Field(None, ge=1, le=6)
    new_max_per_article_yuan: Optional[float] = Field(None, gt=0, le=2000)
    additional_recharge_yuan: Optional[float] = Field(None, gt=0, le=100000)
    preferred_platforms: Optional[list[str]] = None


class TopUpRequest(BaseModel):
    amount_yuan: float = Field(..., gt=0, le=100000)


class WithdrawPendingRequest(BaseModel):
    article_ids: list[int] = Field(..., min_length=1, max_length=20)


class BrandEstimateRequest(BaseModel):
    keywords: list[str] = Field(..., min_length=1, max_length=20)
    brand_id: int
    city: Optional[str] = None
    industry: Optional[str] = None
    uniform_target_sov_pct: int = Field(25, ge=5, le=50)
    per_keyword_sov: Optional[dict[str, int]] = None
    total_budget_yuan: Optional[float] = Field(None, gt=0, le=500000)
    max_per_article_yuan: float = Field(200, gt=0, le=2000)
    check_frequency_per_day: int = Field(1, ge=1, le=6)


class BrandConfirmRechargeRequest(BaseModel):
    brand_id: int
    final_total_yuan: float = Field(..., gt=0, le=500000)
    raw_total_yuan: float = Field(..., gt=0)
    markup_factor: float = Field(1.2, ge=1.0, le=2.5)
    subcampaigns: list[dict]  # [{keyword, sov_pct, articles, cost_yuan}]
    estimate_quoted_at: str
    mode: str = Field("semi_auto", pattern="^(semi_auto|full_auto)$")
    agreement_consent: bool
    brand_voice_consent: bool
    agreement_version: str = Field("v3.4-2026-04", max_length=20)


class ReviewActionRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=300)


# ============================================================
# v3.3 单词托管端点
# ============================================================

@router.post("/estimate")
async def post_estimate(req: EstimateRequest, request: Request):
    """估算套餐方案（不扣费，仅返回 plan_card）"""
    from tools.geo_managed import estimate_word_plan

    user = _get_user(request)
    try:
        plan = await estimate_word_plan(
            keyword=req.keyword,
            city=req.city,
            industry=req.industry,
            mode=req.mode,
            target_sov_pct=req.target_sov_pct,
            budget_yuan=req.budget_yuan,
            max_per_article_yuan=req.max_per_article_yuan,
            check_frequency_per_day=req.check_frequency_per_day,
        )
        plan["user_id"] = user["user_id"]
        return plan
    except Exception as e:
        logger.exception(f"[Managed/estimate] 失败: {e}")
        raise HTTPException(status_code=500, detail=f"估算失败: {e}")


@router.post("/confirm-recharge")
async def post_confirm_recharge(req: ConfirmRechargeRequest, request: Request):
    """充值并启动套餐（扣 paid_points + 创建 managed_campaigns + 落操作日志）"""
    from db.managed_campaign_db import (
        create_campaign, find_active_campaign_by_keyword,
    )
    import psycopg2

    user = _get_user(request)
    user_id = user["user_id"]
    _recharge_charge_txid: Optional[int] = None  # [GEO-R2-CAN-039 v3] 精确退款透传·先绑定防未扣分支引用

    # RBAC: 校验品牌归属（防越权拿别人 brand_id 充值/启动套餐扣自己钱）
    from auth.brand_access import require_brand_access
    require_brand_access(request, req.brand_id)

    if not req.agreement_consent or not req.brand_voice_consent:
        raise HTTPException(status_code=400, detail={
            "code": "AGREEMENT_REQUIRED",
            "message": "必须同意服务协议和品牌口吻授权才能启动",
        })

    # 1. 校验报价 3 天有效期
    try:
        quoted_at = datetime.fromisoformat(req.estimate_quoted_at.replace("Z", "+00:00"))
        # 去 tz
        quoted_at = quoted_at.replace(tzinfo=None) if quoted_at.tzinfo else quoted_at
        if (datetime.now() - quoted_at).total_seconds() > 3 * 86400:
            raise HTTPException(status_code=400, detail={
                "code": "ESTIMATE_EXPIRED",
                "message": "此报价已超过 3 天有效期，市场可能已变化，请刷新重新评估",
            })
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="estimate_quoted_at 格式错误")

    # 2. 同品牌+同关键词唯一约束
    existing = find_active_campaign_by_keyword(req.brand_id, req.keyword)
    if existing:
        raise HTTPException(status_code=409, detail={
            "code": "DUPLICATE_CAMPAIGN",
            "message": f"已有该词的活跃套餐 (id={existing['id']})，请先暂停或选其他词",
            "existing_campaign_id": existing["id"],
        })

    # 3. 扣 paid_points
    try:
        deducted_points, new_wallet, _recharge_charge_txid = _check_balance_and_deduct_paid_points(
            user_id=user_id,
            amount_yuan=req.selected_amount_yuan,
            feature_code="managed_campaign_recharge",
            description=f'GEO 托管套餐 "{req.keyword}" 充值',
            # [对抗审核订正 R1-CAN-069] 加请求级 nonce:原 order_id 秒级,两并发同关键词
            # 同一秒 → order_id 相同 → refund_points 的 _select_split_refund_txs 按 order_id 并组
            # 会把胜出方的 consume 一并退回(charge≠fund 平台净亏)。nonce 保证每请求 order_id 唯一。
            order_id=f"managed:{req.keyword}:{int(datetime.now().timestamp())}:{__import__('shortuuid').uuid()[:8]}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[Managed/confirm] 扣费失败: {e}")
        raise HTTPException(status_code=500, detail=f"扣费失败: {e}")

    # 4. 创建套餐
    try:
        campaign = create_campaign(
            user_id=user_id,
            brand_id=req.brand_id,
            keyword=req.keyword,
            target_sov_pct=req.target_sov_pct,
            target_display_label=req.target_display_label,
            tier_label=req.tier_label,
            initial_recharge_yuan=req.selected_amount_yuan,
            mode=req.mode,
            max_per_article_yuan=req.max_per_article_yuan,
            check_frequency_per_day=req.check_frequency_per_day,
            estimate_quoted_at=quoted_at,
            current_plan=req.current_plan,
            authorized_ip=_get_client_ip(request),
            agreement_version=req.agreement_version,
        )
    except psycopg2.errors.UniqueViolation:
        # 并发竞争触发了 UNIQUE INDEX
        # [GEO-R1-CAN-069] 之前此分支直接抛 409 而不退款,导致并发失败方被扣
        # selected_amount_yuan 却没有套餐、也没有自动退款(与下方 generic 分支不一致)。
        # 这里复用与 generic 分支完全相同的补偿路径,保证扣费方在同词竞争失败后净扣为 0。
        refund_res = await _refund_managed_campaign_failure(
            user_id=user_id,
            feature_code="managed_campaign_recharge",
            failure_reason="创建套餐失败: 同品牌+同关键词并发冲突(UniqueViolation)",
            operation_label=f'GEO 托管套餐充值（关键词"{req.keyword}"）',
            charge_tx_id=_recharge_charge_txid,  # [GEO-R2-CAN-039 v3] 精确退款透传
        )
        if refund_res.get("success"):
            raise HTTPException(status_code=409, detail={
                "code": "DUPLICATE_CAMPAIGN_REFUNDED",
                "message": "已有该词的活跃套餐（并发冲突），已自动退款",
                "existing_campaign_id": None,
            })
        raise HTTPException(status_code=409, detail={
            "code": "DUPLICATE_CAMPAIGN_REFUND_PENDING",
            "message": "已有该词的活跃套餐（并发冲突），自动退款未成功，请联系客服",
        })
    except Exception as e:
        logger.exception(f"[Managed/confirm] 创建套餐失败: {e}")
        # [#40] 自动退款 + 站内通知（替代旧 TODO 客服兜底）
        refund_res = await _refund_managed_campaign_failure(
            user_id=user_id,
            feature_code="managed_campaign_recharge",
            failure_reason=f"创建套餐失败: {e}",
            operation_label=f'GEO 托管套餐充值（关键词"{req.keyword}"）',
            charge_tx_id=_recharge_charge_txid,  # [GEO-R2-CAN-039 v3] 精确退款透传
        )
        if refund_res.get("success"):
            raise HTTPException(status_code=500, detail={
                "code": "CAMPAIGN_CREATE_FAILED_REFUNDED",
                "message": f"创建套餐失败：{e}（已自动退款）",
            })
        raise HTTPException(status_code=500, detail={
            "code": "CAMPAIGN_CREATE_FAILED_REFUND_PENDING",
            "message": f"创建套餐失败：{e}（自动退款也未成功，请联系客服）",
        })

    # 5. 落操作日志
    _log_user_action(
        user_id=user_id,
        action_type="managed_confirm_recharge",
        campaign_id=campaign["id"],
        after_state={
            "keyword": req.keyword,
            "tier": req.tier_label,
            "amount_yuan": req.selected_amount_yuan,
            "mode": req.mode,
        },
        ip=_get_client_ip(request),
        user_agent=request.headers.get("User-Agent", ""),
    )

    logger.info(f"[Managed] user={user_id} 创建套餐 id={campaign['id']} keyword={req.keyword} amount=¥{req.selected_amount_yuan}")

    return {
        "success": True,
        "campaign_id": campaign["id"],
        "status": "active",
        "current_balance_yuan": req.selected_amount_yuan,
        "deducted_points": deducted_points,
        "new_wallet": new_wallet,
    }


@router.get("/{campaign_id}")
async def get_campaign_detail(campaign_id: int, request: Request):
    """套餐详情 + 最近操作日志"""
    from db.managed_campaign_db import get_campaign, get_recent_actions, get_pending_reviews_for_campaign

    user = _get_user(request)
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="套餐不存在")
    # 权限：本人或管理员
    if campaign["user_id"] != user["user_id"] and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="非套餐拥有者")

    actions = get_recent_actions(campaign_id, limit=20)
    pending = get_pending_reviews_for_campaign(campaign_id, limit=20)

    return {
        **campaign,
        "recent_actions": actions,
        "pending_reviews": pending,
    }


@router.get("/")
async def list_user_campaigns(request: Request, status: Optional[str] = None, brand_id: Optional[int] = None):
    """用户的套餐列表"""
    from db.managed_campaign_db import get_user_campaigns

    user = _get_user(request)
    campaigns = get_user_campaigns(
        user_id=user["user_id"],
        status=status,
        brand_id=brand_id,
        limit=50,
    )
    return {"campaigns": campaigns, "total": len(campaigns)}


@router.patch("/{campaign_id}/adjust")
async def patch_adjust(campaign_id: int, req: AdjustRequest, request: Request):
    """对话式调方案 — 不立即扣费，返回新估算 + 等用户确认"""
    from db.managed_campaign_db import get_campaign
    from tools.geo_managed import estimate_word_plan, reverse_calc_from_budget

    user = _get_user(request)
    campaign = get_campaign(campaign_id)
    if not campaign or campaign["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="套餐不存在或无权访问")

    if campaign["status"] not in ("active", "paused"):
        raise HTTPException(status_code=400, detail=f"套餐当前状态 {campaign['status']}，不可调整")

    # 重新估算
    try:
        new_target_sov = req.new_target_sov_pct or int(campaign.get("target_sov_pct", 25))
        new_freq = req.new_check_frequency_per_day or int(campaign.get("check_frequency_per_day", 1))
        new_max_per = req.new_max_per_article_yuan or float(campaign.get("max_per_article_yuan", 200))

        new_plan = await estimate_word_plan(
            keyword=campaign["keyword"],
            city=None,
            industry=None,
            mode="by_target_sov",
            target_sov_pct=new_target_sov,
            max_per_article_yuan=new_max_per,
            check_frequency_per_day=new_freq,
        )
    except Exception as e:
        logger.exception(f"[Managed/adjust] 重算失败: {e}")
        raise HTTPException(status_code=500, detail=f"重算失败: {e}")

    # 落日志（仅记录调方案动作，不扣费）
    _log_user_action(
        user_id=user["user_id"],
        action_type="managed_adjust_plan",
        campaign_id=campaign_id,
        before_state={
            "target_sov_pct": campaign.get("target_sov_pct"),
            "check_frequency_per_day": campaign.get("check_frequency_per_day"),
        },
        after_state={
            "target_sov_pct": new_target_sov,
            "check_frequency_per_day": new_freq,
            "additional_recharge_yuan": req.additional_recharge_yuan,
        },
        ip=_get_client_ip(request),
    )

    # 如果用户要求加充，提示需走 /top-up + /confirm-adjust
    return {
        "new_estimate": new_plan,
        "diff_summary": _diff_summary(campaign, new_target_sov, new_freq, new_max_per, req.additional_recharge_yuan),
        "requires_user_confirm": True,
        "next_step": "如确认调方案：调用 PATCH /api/managed/{id}/confirm-adjust 携带新参数",
    }


def _diff_summary(campaign, new_sov, new_freq, new_max_per, addtl_recharge) -> str:
    diffs = []
    old_sov = int(campaign.get("target_sov_pct", 25))
    if new_sov != old_sov:
        diffs.append(f"目标 SOV 从 {old_sov}% → {new_sov}%")
    old_freq = int(campaign.get("check_frequency_per_day", 1))
    if new_freq != old_freq:
        diffs.append(f"监测频次从 {old_freq}/天 → {new_freq}/天")
    old_max_per = float(campaign.get("max_per_article_yuan", 200))
    if new_max_per != old_max_per:
        diffs.append(f"单篇上限从 ¥{old_max_per:.0f} → ¥{new_max_per:.0f}")
    if addtl_recharge:
        diffs.append(f"加充 ¥{addtl_recharge}")
    return " / ".join(diffs) if diffs else "无变化"


@router.post("/{campaign_id}/confirm-adjust")
async def post_confirm_adjust(campaign_id: int, req: AdjustRequest, request: Request):
    """确认调方案（如有 additional_recharge_yuan 则扣费）"""
    from db.managed_campaign_db import (
        get_campaign, top_up_campaign, update_current_plan,
    )
    from db.connection import get_db

    _adjust_charge_txid: Optional[int] = None  # [GEO-R2-CAN-039 v3] 精确退款透传·先绑定防未扣分支引用
    user = _get_user(request)
    campaign = get_campaign(campaign_id)
    if not campaign or campaign["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="套餐不存在或无权访问")

    new_target_sov = req.new_target_sov_pct or int(campaign.get("target_sov_pct", 25))
    new_freq = req.new_check_frequency_per_day or int(campaign.get("check_frequency_per_day", 1))
    new_max_per = req.new_max_per_article_yuan or float(campaign.get("max_per_article_yuan", 200))

    # 如有加充，先扣费
    if req.additional_recharge_yuan and req.additional_recharge_yuan > 0:
        # 第一步：扣费（HTTPException 直接抛，余额不足等业务错误不触发退款）
        try:
            # [GEO-R2-CAN-039 v3] 精确退款透传：捕获 charge_tx_id
            _, _, _adjust_charge_txid = _check_balance_and_deduct_paid_points(
                user_id=user["user_id"],
                amount_yuan=req.additional_recharge_yuan,
                feature_code="managed_campaign_recharge",
                description=f'托管套餐 #{campaign_id} 加充',
                order_id=f"top_up:{campaign_id}:{int(datetime.now().timestamp())}:{__import__('shortuuid').uuid()[:8]}",
            )
        except HTTPException:
            raise
        # 第二步：加充入账（失败需退款 — #40）
        try:
            top_up_campaign(campaign_id, req.additional_recharge_yuan)
        except Exception as e:
            logger.exception(f"[Managed/adjust] top_up_campaign 失败: {e}")
            refund_res = await _refund_managed_campaign_failure(
                user_id=user["user_id"],
                feature_code="managed_campaign_recharge",
                failure_reason=f"调方案加充失败: {e}",
                operation_label=f"GEO 托管套餐 #{campaign_id} 调方案加充",
                charge_tx_id=_adjust_charge_txid,  # [GEO-R2-CAN-039 v3] 精确退款透传
            )
            if refund_res.get("success"):
                raise HTTPException(status_code=500, detail={
                    "code": "ADJUST_FAILED_REFUNDED",
                    "message": f"加充失败：{e}（已自动退款）",
                })
            raise HTTPException(status_code=500, detail={
                "code": "ADJUST_FAILED_REFUND_PENDING",
                "message": f"加充失败：{e}（自动退款未成功，请联系客服）",
            })

    # 更新套餐参数
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE managed_campaigns
            SET target_sov_pct = %s,
                check_frequency_per_day = %s,
                max_per_article_yuan = %s,
                last_active_at = NOW()
            WHERE id = %s
        """, (new_target_sov, new_freq, new_max_per, campaign_id))

    # 更新 current_plan + 刷新报价窗口
    new_plan_data = {
        "target_sov_pct": new_target_sov,
        "platform_mix": (campaign.get("current_plan") or {}).get("platform_mix"),
        "check_frequency_per_day": new_freq,
        "preferred_platforms": req.preferred_platforms,
    }
    update_current_plan(campaign_id, new_plan_data)

    return {"success": True, "campaign_id": campaign_id, "applied": True}


@router.post("/{campaign_id}/pause")
async def post_pause(campaign_id: int, request: Request):
    """暂停套餐（已扣不退，余额永久保留）"""
    from db.managed_campaign_db import get_campaign, update_status

    user = _get_user(request)
    campaign = get_campaign(campaign_id)
    if not campaign or campaign["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="套餐不存在或无权访问")
    if campaign["status"] != "active":
        raise HTTPException(status_code=409, detail=f"套餐当前状态 {campaign['status']}，无需暂停")

    updated = update_status(campaign_id, "paused", reason="user_paused")
    _log_user_action(
        user_id=user["user_id"],
        action_type="managed_pause",
        campaign_id=campaign_id,
        before_state={"status": "active"},
        after_state={"status": "paused"},
        ip=_get_client_ip(request),
    )
    return {"success": True, "status": "paused", "balance_yuan": updated.get("total_recharged_yuan", 0) - updated.get("total_consumed_yuan", 0)}


@router.post("/{campaign_id}/resume")
async def post_resume(campaign_id: int, request: Request):
    """
    恢复暂停的套餐
    逻辑:
      - 暂停 > 30 天 或 报价过期 → 强制重新评估
      - 否则直接恢复
    """
    from db.managed_campaign_db import get_campaign, update_status, update_estimate_window
    from tools.geo_managed.estimate_engine import re_estimate_for_resume

    user = _get_user(request)
    campaign = get_campaign(campaign_id)
    if not campaign or campaign["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="套餐不存在或无权访问")
    if campaign["status"] != "paused":
        raise HTTPException(status_code=409, detail=f"套餐当前状态 {campaign['status']}，无法恢复")

    # 检查是否需要重新评估
    paused_at = campaign.get("paused_at")
    paused_too_long = False
    if paused_at:
        if isinstance(paused_at, str):
            paused_at = datetime.fromisoformat(paused_at)
        days_paused = (datetime.now() - paused_at.replace(tzinfo=None)).days
        if days_paused > 30:
            paused_too_long = True

    estimate_expired = not _is_estimate_valid(campaign)

    if paused_too_long or estimate_expired:
        # 强制重新评估，不立即恢复 active
        try:
            new_estimate = await re_estimate_for_resume(campaign)
        except Exception as e:
            logger.exception(f"[Managed/resume] 重评估失败: {e}")
            raise HTTPException(status_code=500, detail=f"重评估失败: {e}")
        # 仅刷新报价窗口，状态保持 paused 等用户确认
        update_estimate_window(campaign_id)
        return {
            "requires_re_estimate": True,
            "reason": "暂停 > 30 天或报价已过期，市场可能已变化",
            "new_estimate": new_estimate,
            "next_step": "确认新方案：调用 POST /api/managed/{id}/confirm-resume",
        }

    # 直接恢复
    update_status(campaign_id, "active", reason="user_resumed")
    _log_user_action(
        user_id=user["user_id"],
        action_type="managed_resume",
        campaign_id=campaign_id,
        ip=_get_client_ip(request),
    )
    return {"success": True, "status": "active"}


@router.post("/{campaign_id}/confirm-resume")
async def post_confirm_resume(campaign_id: int, request: Request):
    """重评估后确认恢复"""
    from db.managed_campaign_db import get_campaign, update_status

    user = _get_user(request)
    campaign = get_campaign(campaign_id)
    if not campaign or campaign["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="套餐不存在或无权访问")

    # 检查报价是否最新
    if not _is_estimate_valid(campaign):
        raise HTTPException(status_code=400, detail={
            "code": "ESTIMATE_EXPIRED",
            "message": "报价已过期，请重新拉取 estimate",
        })

    update_status(campaign_id, "active", reason="user_confirmed_resume")
    _log_user_action(
        user_id=user["user_id"],
        action_type="managed_resume",
        campaign_id=campaign_id,
        after_state={"re_estimated": True},
        ip=_get_client_ip(request),
    )
    return {"success": True, "status": "active"}


@router.post("/{campaign_id}/top-up")
async def post_top_up(campaign_id: int, req: TopUpRequest, request: Request):
    """加充"""
    from db.managed_campaign_db import get_campaign, top_up_campaign

    _topup_charge_txid: Optional[int] = None  # [GEO-R2-CAN-039 v3] 精确退款透传·先绑定防未扣分支引用
    user = _get_user(request)
    campaign = get_campaign(campaign_id)
    if not campaign or campaign["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="套餐不存在或无权访问")

    # 扣 paid_points
    try:
        # [GEO-R2-CAN-039 v3] 精确退款透传：捕获 charge_tx_id
        _, _, _topup_charge_txid = _check_balance_and_deduct_paid_points(
            user_id=user["user_id"],
            amount_yuan=req.amount_yuan,
            feature_code="managed_campaign_recharge",
            description=f'托管套餐 #{campaign_id} 加充',
            order_id=f"top_up:{campaign_id}:{int(datetime.now().timestamp())}:{__import__('shortuuid').uuid()[:8]}",
        )
    except HTTPException:
        raise

    # [#40] top_up_campaign 失败需退款（之前裸调，钱扣了套餐没加）
    try:
        updated = top_up_campaign(campaign_id, req.amount_yuan)
    except Exception as e:
        logger.exception(f"[Managed/top-up] top_up_campaign 失败: {e}")
        refund_res = await _refund_managed_campaign_failure(
            user_id=user["user_id"],
            feature_code="managed_campaign_recharge",
            failure_reason=f"加充套餐失败: {e}",
            operation_label=f"GEO 托管套餐 #{campaign_id} 加充",
            charge_tx_id=_topup_charge_txid,  # [GEO-R2-CAN-039 v3] 精确退款透传
        )
        if refund_res.get("success"):
            raise HTTPException(status_code=500, detail={
                "code": "TOP_UP_FAILED_REFUNDED",
                "message": f"加充失败：{e}（已自动退款）",
            })
        raise HTTPException(status_code=500, detail={
            "code": "TOP_UP_FAILED_REFUND_PENDING",
            "message": f"加充失败：{e}（自动退款未成功，请联系客服）",
        })

    _log_user_action(
        user_id=user["user_id"],
        action_type="managed_top_up",
        campaign_id=campaign_id,
        after_state={"top_up_yuan": req.amount_yuan},
        ip=_get_client_ip(request),
    )

    return {
        "success": True,
        "status": updated.get("status", "active"),
        "new_balance": float(updated.get("total_recharged_yuan", 0)) - float(updated.get("total_consumed_yuan", 0)),
    }


@router.post("/{campaign_id}/withdraw-pending")
async def post_withdraw_pending(campaign_id: int, req: WithdrawPendingRequest, request: Request):
    """24h 内撤回未发文章（品牌口吻授权的反悔窗口）"""
    from db.managed_campaign_db import (
        get_campaign, get_recent_pending_for_withdraw, update_review_status,
        consume_balance, log_action,
    )
    from db.connection import get_db

    user = _get_user(request)
    campaign = get_campaign(campaign_id)
    if not campaign or campaign["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="套餐不存在或无权访问")

    # 取可撤回的文章
    candidates = get_recent_pending_for_withdraw(campaign_id, req.article_ids, within_hours=24)
    if not candidates:
        raise HTTPException(status_code=410, detail={
            "code": "WITHDRAW_WINDOW_EXPIRED",
            "message": "没有可撤回的文章（已超 24h 或不在 pending 队列）",
        })

    refunded = 0.0
    withdrawn_count = 0
    for c in candidates:
        rid = c["id"]
        # 退还写作费 ¥10（在套餐内退）
        # 注意：套餐"退还"= total_consumed_yuan 减少（不是 user_wallets 退）
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE managed_campaigns
                    SET total_consumed_yuan = GREATEST(0, total_consumed_yuan - 10),
                        last_active_at = NOW()
                    WHERE id = %s
                """, (campaign_id,))
            update_review_status(rid, "withdrawn", reviewed_by_user_id=user["user_id"], review_note="用户 24h 内撤回")
            log_action(campaign_id, "user_withdrew",
                       action_detail={"review_id": rid}, cost_yuan=-10, result="success")
            refunded += 10
            withdrawn_count += 1
        except Exception as e:
            logger.warning(f"[Managed/withdraw] 撤回 {rid} 失败: {e}")

    _log_user_action(
        user_id=user["user_id"],
        action_type="managed_withdraw_pending",
        campaign_id=campaign_id,
        after_state={"withdrawn_count": withdrawn_count, "refunded_yuan": refunded},
        ip=_get_client_ip(request),
    )
    return {"withdrawn_count": withdrawn_count, "refunded_yuan": refunded}


# ============================================================
# 待审 / 撤回端点
# ============================================================

@router.get("/reviews/pending")
async def get_user_pending_reviews_endpoint(request: Request):
    """用户所有套餐的待审列表"""
    from db.managed_campaign_db import get_user_pending_reviews

    user = _get_user(request)
    reviews = get_user_pending_reviews(user["user_id"], limit=50)
    return {"reviews": reviews, "total": len(reviews)}


@router.post("/review/{review_id}/approve")
async def post_approve_review(review_id: int, req: ReviewActionRequest, request: Request):
    """半自动模式：审核通过 → 立即发布"""
    from db.managed_campaign_db import (
        get_review_by_id, update_review_status,
        get_campaign, log_action,
    )
    from tools.geo_managed.campaign_tick import _publish_article

    user = _get_user(request)
    review = get_review_by_id(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="待审记录不存在")
    if review["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"该记录状态 {review['status']}，无法操作")

    campaign = get_campaign(review["campaign_id"])
    if not campaign or campaign["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作")

    # 立即发布
    article = {"title": review["title"], "content": review["full_content"]}
    platforms = [{"platform": p, "our_price_yuan": 0} for p in review.get("platforms_to_publish", [])]
    try:
        await _publish_article(campaign, article, platforms)
        update_review_status(review_id, "approved", reviewed_by_user_id=user["user_id"], review_note=req.note)
        log_action(campaign["id"], "user_approved",
                   action_detail={"review_id": review_id, "title": review["title"]})
    except Exception as e:
        logger.exception(f"[Managed/approve] 发布失败: {e}")
        raise HTTPException(status_code=500, detail=f"发布失败: {e}")

    _log_user_action(
        user_id=user["user_id"],
        action_type="managed_review_approve",
        campaign_id=campaign["id"],
        after_state={"review_id": review_id},
        ip=_get_client_ip(request),
    )
    return {"success": True, "review_id": review_id, "status": "approved"}


@router.post("/review/{review_id}/reject")
async def post_reject_review(review_id: int, req: ReviewActionRequest, request: Request):
    """半自动模式：审核拒绝 → AI 重写（这里只标记，重写由 tick 重跑）"""
    from db.managed_campaign_db import (
        get_review_by_id, update_review_status, get_campaign, log_action,
    )

    user = _get_user(request)
    review = get_review_by_id(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="待审记录不存在")
    if review["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"该记录状态 {review['status']}")

    campaign = get_campaign(review["campaign_id"])
    if not campaign or campaign["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作")

    update_review_status(review_id, "rejected", reviewed_by_user_id=user["user_id"], review_note=req.note)
    log_action(campaign["id"], "user_rejected",
               action_detail={"review_id": review_id, "reason": req.note})

    _log_user_action(
        user_id=user["user_id"],
        action_type="managed_review_reject",
        campaign_id=campaign["id"],
        after_state={"review_id": review_id, "reason": req.note},
        ip=_get_client_ip(request),
    )
    return {"success": True, "review_id": review_id, "status": "rejected", "note": "AI 将在下次 tick 重写"}


# ============================================================
# v3.4 全品牌托管端点
# ============================================================

@router.post("/brand/estimate")
async def post_brand_estimate(req: BrandEstimateRequest, request: Request):
    """v3.4 全品牌套餐估算"""
    from tools.geo_managed import estimate_brand_plan
    from auth.brand_access import require_brand_access

    user = _get_user(request)
    # 2026-04-17 (P1-3): 校验 brand_id 属于当前用户（admin/分配/owner 三选一）
    require_brand_access(request, req.brand_id)
    try:
        plan = await estimate_brand_plan(
            keywords=req.keywords,
            brand_id=req.brand_id,
            user_id=user["user_id"],
            city=req.city,
            industry=req.industry,
            uniform_target_sov_pct=req.uniform_target_sov_pct,
            per_keyword_sov=req.per_keyword_sov,
            total_budget_yuan=req.total_budget_yuan,
            max_per_article_yuan=req.max_per_article_yuan,
            check_frequency_per_day=req.check_frequency_per_day,
        )
        return plan
    except Exception as e:
        logger.exception(f"[Managed/brand/estimate] 失败: {e}")
        raise HTTPException(status_code=500, detail=f"估算失败: {e}")


@router.post("/brand/confirm-recharge")
async def post_brand_confirm_recharge(req: BrandConfirmRechargeRequest, request: Request):
    """创建全品牌套餐（多 keyword 合并 + markup 1.2x 已内嵌在 final_total_yuan）"""
    from db.managed_campaign_db import create_campaign, create_brand_package
    from auth.brand_access import require_brand_access
    import psycopg2

    user = _get_user(request)
    user_id = user["user_id"]
    _brand_charge_txid: Optional[int] = None  # [GEO-R2-CAN-039 v3] 精确退款透传·先绑定防未扣分支引用

    # 2026-04-17 (P1-3): 校验 brand_id 属于当前用户
    # 否则攻击者可扣自己钱但创建套餐挂到别人 brand
    require_brand_access(request, req.brand_id)

    if not req.agreement_consent or not req.brand_voice_consent:
        raise HTTPException(status_code=400, detail={"code": "AGREEMENT_REQUIRED"})

    # 校验报价 3 天有效
    try:
        quoted_at = datetime.fromisoformat(req.estimate_quoted_at.replace("Z", "+00:00"))
        quoted_at = quoted_at.replace(tzinfo=None) if quoted_at.tzinfo else quoted_at
        if (datetime.now() - quoted_at).total_seconds() > 3 * 86400:
            raise HTTPException(status_code=400, detail={"code": "ESTIMATE_EXPIRED"})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="estimate_quoted_at 格式错误")

    # [GEO-R6-CAN-013 P1] 防客户端伪造定价:req.final_total_yuan(扣费额)与子套餐
    # initial_recharge_yuan=cost_yuan*markup_factor(充值额)均来自客户端,原无服务端
    # 一致性校验 → 可构造 final_total_yuan 少扣、cost_yuan 多充(charge<fund)套利,或反向多扣。
    # 强制 扣费额 == 子套餐充值额之和(容差 1 分),否则拒。使 charge==fund 守恒。
    # (注:cost_yuan 基价本身仍取客户端,全量服务端重算依赖报价快照,留后续;本闸已闭合 charge≠fund 背离)
    # [对抗审核订正] 报价引擎 estimate 产出 final_total=round(raw_total*markup)(取整到整元),
    # 故服务端合计也须整元取整,容差放宽到 0.5,否则误杀平台自己生成的合法整元报价。
    _server_pkg_total = round(
        sum(float(s.get("cost_yuan", 0) or 0) for s in req.subcampaigns) * float(req.markup_factor))
    if abs(float(req.final_total_yuan) - _server_pkg_total) > 0.5:
        raise HTTPException(status_code=400, detail={
            "code": "PRICING_MISMATCH",
            "message": f"套餐总价与子项充值额不符(扣费={req.final_total_yuan} 子项合计={_server_pkg_total})· 请重新报价",
        })

    # 扣 paid_points (final_total_yuan 已含 markup)
    try:
        # [GEO-R2-CAN-039 v3] 精确退款透传：捕获 charge_tx_id
        _, _, _brand_charge_txid = _check_balance_and_deduct_paid_points(
            user_id=user_id,
            amount_yuan=req.final_total_yuan,
            feature_code="managed_brand_recharge",
            description=f'全品牌托管套餐（含 {len(req.subcampaigns)} 词）',
            order_id=f"brand_pkg:{req.brand_id}:{int(datetime.now().timestamp())}:{__import__('shortuuid').uuid()[:8]}",
        )
    except HTTPException:
        raise

    # 为每个 keyword 创建子 campaign
    campaign_ids: list[int] = []
    failed_subs: list = []  # [A-14-1] 创建失败 / 重复词 · 用于部分失败按比例退款
    for sub in req.subcampaigns:
        try:
            tier_label_map = {15: "entry", 25: "standard", 33: "flagship", 50: "strong"}
            tier = tier_label_map.get(sub.get("sov_pct", 25), "custom")
            label_map = {15: "偶尔被推荐", 25: "经常被推荐", 33: "优先推荐", 50: "频繁被推荐"}
            campaign = create_campaign(
                user_id=user_id,
                brand_id=req.brand_id,
                keyword=sub["keyword"],
                target_sov_pct=sub.get("sov_pct", 25),
                target_display_label=label_map.get(sub.get("sov_pct", 25), "经常被推荐"),
                tier_label=tier,
                initial_recharge_yuan=float(sub["cost_yuan"]) * req.markup_factor,
                mode=req.mode,
                authorized_ip=_get_client_ip(request),
                agreement_version=req.agreement_version,
            )
            campaign_ids.append(campaign["id"])
        except psycopg2.errors.UniqueViolation:
            logger.warning(f"[Brand] 跳过已存在的关键词 {sub['keyword']}")
            failed_subs.append(sub)  # [A-14-1] 重复词本次未新建 · 用户已为它付费 → 计入按比例退款
        except Exception as e:
            logger.error(f"[Brand] 创建子套餐失败 {sub.get('keyword')}: {e}")
            failed_subs.append(sub)  # [A-14-1] 失败词计入按比例退款

    # [A-14-1 P2 对账] 记录部分退款额 + 失败词 · 供收入/复盘/客服对账(避免只记全额报价导致收入虚高)
    _partial_refunded_yuan = 0.0
    _partial_failed_keywords: list = []

    # [#40] 全部子套餐都失败时全额退款（部分成功的 N:1 比例退款属于复杂场景，单独立项处理）
    if not campaign_ids and req.subcampaigns:
        logger.error(f"[Brand] 全部 {len(req.subcampaigns)} 个子套餐创建失败，触发全额退款")
        refund_res = await _refund_managed_campaign_failure(
            user_id=user_id,
            feature_code="managed_brand_recharge",
            failure_reason=f"全部 {len(req.subcampaigns)} 个子套餐创建失败",
            operation_label=f"GEO 全品牌托管套餐（{len(req.subcampaigns)} 个关键词）",
            charge_tx_id=_brand_charge_txid,  # [GEO-R2-CAN-039 v3] 精确退款透传
        )
        if refund_res.get("success"):
            raise HTTPException(status_code=500, detail={
                "code": "BRAND_PKG_ALL_FAILED_REFUNDED",
                "message": "全部子套餐创建失败（已自动退款）",
            })
        raise HTTPException(status_code=500, detail={
            "code": "BRAND_PKG_ALL_FAILED_REFUND_PENDING",
            "message": "全部子套餐创建失败（自动退款未成功，请联系客服）",
        })
    # [A-14-1 / #40-followup 2026-05-29] 部分子套餐失败 → 按 (失败词成本 / 总成本) × 实扣总额 比例退款
    elif failed_subs:
        def _sub_cost(s) -> float:
            try:
                return float(s.get("cost_yuan") or 0)
            except (TypeError, ValueError):
                return 0.0
        total_cost = sum(_sub_cost(s) for s in req.subcampaigns)
        failed_cost = sum(_sub_cost(s) for s in failed_subs)
        failed_keywords = [str(s.get("keyword") or "?") for s in failed_subs]
        _partial_failed_keywords = failed_keywords
        if total_cost > 0 and failed_cost > 0:
            # 按实扣总额(final_total_yuan · 已含 markup)等比退 · markup 随之等比退还
            refund_yuan = round(req.final_total_yuan * (failed_cost / total_cost), 2)
            logger.warning(
                f"[Brand] 部分失败 {len(failed_subs)}/{len(req.subcampaigns)} 词 · "
                f"按比例退 ¥{refund_yuan:.2f}(失败成本 {failed_cost}/{total_cost}) · 词={failed_keywords}"
            )
            _pr = await _refund_managed_campaign_partial(
                user_id=user_id,
                refund_yuan=refund_yuan,
                feature_code="managed_brand_recharge",
                failed_keywords=failed_keywords,
                operation_label=f"GEO 全品牌托管套餐（{len(req.subcampaigns)} 个关键词）",
                charge_tx_id=_brand_charge_txid,
            )
            if _pr.get("success"):
                _partial_refunded_yuan = float(_pr.get("refunded_yuan") or 0.0)
        else:
            # 成本数据缺失无法按比例 · 不静默吞 · 站内信提示联系客服
            logger.error(
                f"[Brand] 部分失败但成本数据缺失,无法按比例退款 · 词={failed_keywords} "
                f"total_cost={total_cost} failed_cost={failed_cost}"
            )
            try:
                import hashlib
                from datetime import timezone
                from services.notification_events import NotificationEventType, RecipientKind
                from services.notification_outbox import (
                    enqueue_admin_notification_events,
                    enqueue_notification_event,
                )
                identity = int(_brand_charge_txid) if _brand_charge_txid is not None else hashlib.sha256(
                    f"{int(user_id)}:managed_brand_recharge:{req.brand_id}".encode("utf-8")
                ).hexdigest()[:16]
                public_ref = hashlib.sha256(
                    f"managed-brand-manual:{int(user_id)}:{identity}".encode("utf-8")
                ).hexdigest()[:10].upper()
                facts = {
                    "business_no": f"GEO-{public_ref}",
                    "status": "需要人工处理",
                    "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "summary": "部分任务未完成且退款金额暂时无法确认，请勿重复提交。",
                }
                from db.connection import get_db
                with get_db() as conn:
                    cursor = conn.cursor()
                    enqueue_notification_event(
                        cursor,
                        event_type=NotificationEventType.MANAGED_CAMPAIGN_MANUAL_REQUIRED,
                        business_id=f"managed_partial_refund:{identity}",
                        terminal_state="manual_required",
                        recipient_user_id=int(user_id),
                        recipient_kind=RecipientKind.USER,
                        facts=facts,
                    )
                    enqueue_admin_notification_events(
                        cursor,
                        event_type=NotificationEventType.MANAGED_CAMPAIGN_MANUAL_REQUIRED,
                        business_id=f"managed_partial_refund:{identity}",
                        terminal_state="manual_required",
                        facts=facts,
                    )
            except Exception as _ne:
                logger.exception("[Brand] 部分失败持久通知写入失败: %s", type(_ne).__name__)

    # 创建主品牌套餐记录
    pkg = create_brand_package(
        user_id=user_id,
        brand_id=req.brand_id,
        total_price_yuan=req.final_total_yuan,
        raw_cost_yuan=req.raw_total_yuan,
        campaign_ids=campaign_ids,
        markup_factor=req.markup_factor,
        authorized_ip=_get_client_ip(request),
        agreement_version=req.agreement_version,
    )

    _log_user_action(
        user_id=user_id,
        action_type="managed_brand_confirm_recharge",
        after_state={
            "brand_id": req.brand_id,
            "package_id": pkg["id"],
            "amount_yuan": req.final_total_yuan,                 # 原始报价额(gross)
            "refunded_yuan": _partial_refunded_yuan,             # [A-14-1 对账] 部分失败已退
            "net_charged_yuan": round(req.final_total_yuan - _partial_refunded_yuan, 2),
            "failed_keywords": _partial_failed_keywords,
            "campaign_count": len(campaign_ids),
        },
        ip=_get_client_ip(request),
    )

    return {
        "success": True,
        "brand_package_id": pkg["id"],
        "campaign_ids": campaign_ids,
        "total_price_yuan": req.final_total_yuan,
        "refunded_yuan": _partial_refunded_yuan,                 # [A-14-1 对账] 部分失败按比例退款额
        "net_charged_yuan": round(req.final_total_yuan - _partial_refunded_yuan, 2),
        "failed_keywords": _partial_failed_keywords,
    }


@router.get("/brand/{brand_id}/dashboard")
async def get_brand_dashboard(brand_id: int, request: Request):
    """品牌套餐看板（含 5 引擎日志 + 子套餐汇总）"""
    from db.managed_campaign_db import (
        get_brand_package, get_user_campaigns, get_brand_strategy,
    )

    user = _get_user(request)
    pkg = get_brand_package(brand_id)
    if not pkg or pkg.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=404, detail="该品牌无活跃托管套餐")

    subcampaigns = get_user_campaigns(user_id=user["user_id"], brand_id=brand_id)
    strategy = get_brand_strategy(brand_id)

    total_balance = sum(c.get("balance_yuan", 0) for c in subcampaigns)
    total_delivered = sum(c.get("delivered_articles", 0) for c in subcampaigns)

    # 本周 AI 洞察（从 brand_strategies）
    insights = ""
    if strategy and strategy.get("shareable_patterns"):
        sp = strategy["shareable_patterns"]
        if isinstance(sp, dict):
            insights = sp.get("industry_insight", "") or sp.get("platform_preference", "")

    return {
        "brand_id": brand_id,
        "brand_package": pkg,
        "subcampaigns": subcampaigns,
        "total_balance_yuan": total_balance,
        "delivered_articles": total_delivered,
        "this_week_insights": insights,
        "brand_strategy": strategy,
    }


# ============================================================
# 管理员端点
# ============================================================

@admin_router.post("/{campaign_id}/inspect")
async def admin_inspect_campaign(campaign_id: int, request: Request):
    """管理员查看任意套餐 + 完整动作日志"""
    from db.managed_campaign_db import get_campaign, get_recent_actions

    _get_admin(request)
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="套餐不存在")
    actions = get_recent_actions(campaign_id, limit=200)
    return {**campaign, "all_actions": actions}


@admin_router.post("/dormancy-scan")
async def admin_trigger_dormancy_scan(request: Request):
    """管理员手动触发 12 个月休眠扫描"""
    from tools.geo_managed.campaign_tick import dormancy_scan
    _get_admin(request)
    try:
        dormancy_scan()
        _log_user_action(
            user_id=request.state.user["user_id"],
            action_type="admin_dormancy_scan",
            ip=_get_client_ip(request),
        )
        return {"success": True, "message": "已触发休眠扫描，详情见日志"}
    except Exception as e:
        logger.exception(f"[Admin/dormancy-scan] 失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
