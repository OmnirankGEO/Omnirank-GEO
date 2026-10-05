"""
V3.3.1 服务费状态机 + 转积分 + clawback 双层 + 异常退款追索

8 态状态机(§3.2):
    pending → settled → (converted / withdraw_requested / cancelled / clawback / rejected)
    withdraw_requested → withdrawn / rejected → settled

转积分(§3.5):
    - 1元=130积分 + 赠送 N% bonus(默认 20% · 后台可调)
    - 月度配额(L2 普通 ¥5K / 优质 ¥20K / 战略无限)
    - 前置 T+7 退款/投诉期(只能转纳入退款期已过的 settled records)
    - 不可撤销 · 转换后不可再提现
    - 转换产生的 paid/bonus 标 source='service_fee_conversion' · 不触发返佣

提现(§3.3):
    - KYC + 实名 + 1 次/周 + ≥¥100 + 月累计 > ¥800 需发票 + > ¥1000 双签
    - 平台可拒绝/延迟/部分结算

Clawback 双层(§3.4.1):
    LAYER 1: settled 余额扣
    LAYER 2: 不足 → 写 service_fee_clawback_pending · 后续抵扣

异常退款追索(§3.5.6):
    4 类(legal_dispute / platform_force / fraud / chargeback)→ 转积分后追索

关联:
- 决策书 §3.2 §3.3 §3.4 §3.5 §3.6
- IDENTITY_DECISIONS_LOCK Q12-Q22 / Q33-Q34
- RED_LINES R2 R3
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List

from db.connection import get_db
from config.v3_3_1_flags import (
    is_v3_3_1_enabled,
    is_service_fee_conversion_enabled,
    is_withdrawal_enabled,
    get_conversion_bonus_rate,
    get_conversion_quota_yuan,
    get_conversion_min_age_days,
    get_withdrawal_min_amount,
    get_withdrawal_max_per_week,
    get_withdrawal_invoice_threshold,
    get_withdrawal_dual_sign_threshold,
    PaymentSource,
    ServiceFeeStatus,
    is_anomalous_refund,
)

logger = logging.getLogger("GEO-ServiceFee-Engine")

POINTS_PER_YUAN = Decimal("130")


# ============================================
# 异常基类
# ============================================

class ServiceFeeError(Exception):
    """V3.3.1 服务费业务异常基类"""
    code: str = "service_fee_error"
    http_status: int = 400

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


class FeatureDisabledError(ServiceFeeError):
    code = "feature_disabled"
    http_status = 503


class NotL2AgentError(ServiceFeeError):
    code = "not_l2_agent"
    http_status = 403


class InsufficientBalanceError(ServiceFeeError):
    code = "insufficient_balance"
    http_status = 400


class SettledRecordsWithinRefundWindowError(ServiceFeeError):
    """转换前置 T+7 退款期未过"""
    code = "settled_records_within_refund_window"
    http_status = 425  # too early


class MonthlyQuotaExceededError(ServiceFeeError):
    code = "monthly_quota_exceeded"
    http_status = 402


class WithdrawalPreconditionError(ServiceFeeError):
    code = "withdrawal_precondition_failed"
    http_status = 400


class WithdrawalRateLimitError(ServiceFeeError):
    code = "withdrawal_rate_limit"
    http_status = 429


# ============================================
# 通用查询
# ============================================

def get_user_wallet(user_id: int) -> Optional[dict]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, agent_level, agent_tier,
                       is_kyc_passed, bank_account_verified,
                       paid_points, bonus_points
                  FROM user_wallets
                 WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return dict(row) if isinstance(row, dict) else {
                "user_id": row[0], "agent_level": row[1], "agent_tier": row[2],
                "is_kyc_passed": row[3], "bank_account_verified": row[4],
                "paid_points": row[5], "bonus_points": row[6],
            }


def is_l2_agent(user_id: int) -> bool:
    """L2 代理判定(agent_level >= 2)"""
    wallet = get_user_wallet(user_id)
    return bool(wallet and (wallet.get("agent_level") or 0) >= 2)


def get_settled_balance_yuan(user_id: int) -> Decimal:
    """L2 可处理余额(status=settled · 未冻未转未提现)"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(amount_yuan), 0) AS total
                  FROM service_fee_records
                 WHERE user_id = %s AND status = %s
                """,
                (user_id, ServiceFeeStatus.SETTLED),
            )
            row = cur.fetchone()
            value = row["total"] if isinstance(row, dict) else row[0]
            return Decimal(str(value or 0))


def get_pending_balance_yuan(user_id: int) -> Decimal:
    """退款期内(冻结)余额"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(amount_yuan), 0) AS total
                  FROM service_fee_records
                 WHERE user_id = %s AND status = %s
                """,
                (user_id, ServiceFeeStatus.PENDING),
            )
            row = cur.fetchone()
            value = row["total"] if isinstance(row, dict) else row[0]
            return Decimal(str(value or 0))


def get_monthly_used_yuan(user_id: int, yyyymm: Optional[int] = None) -> Decimal:
    """月度已用转换额度"""
    yyyymm = yyyymm or int(datetime.now().strftime("%Y%m"))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT used_yuan FROM service_fee_conversion_quota WHERE user_id = %s AND yyyymm = %s",
                (user_id, yyyymm),
            )
            row = cur.fetchone()
            if not row:
                return Decimal("0")
            value = row["used_yuan"] if isinstance(row, dict) else row[0]
            return Decimal(str(value or 0))


# ============================================
# T+3 / T+7 状态机推进(cron 调用)
# ============================================

def settle_due_service_fees(dry_run: bool = False) -> dict:
    """T+3 cron · 把 pending 转 settled(已过 available_at + 无退款)"""
    if not is_v3_3_1_enabled():
        return {"skipped": True, "reason": "v3_3_1_disabled"}

    affected = 0
    skipped_refunded = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, source_order_id, amount_yuan, net_cash_revenue_yuan
                  FROM service_fee_records
                 WHERE status = %s
                   AND available_at <= NOW()
                """,
                (ServiceFeeStatus.PENDING,),
            )
            rows = cur.fetchall()

            for row in rows:
                rid = row["id"] if isinstance(row, dict) else row[0]
                source_order_id = row["source_order_id"] if isinstance(row, dict) else row[2]

                # 二次核对源订单是否有退款(决策书 §3.1.1 T+3 settle 前再核 net_cash)
                # recharge_orders 实际字段:payment_status('refunded' / 'partially_refunded' / 'paid')
                # 退款金额从 refund_records 表(若存在)/ on_recharge_refund 调用时反映在 service_fee_records.clawback
                cur.execute(
                    """
                    SELECT COALESCE(payment_status, '') AS s
                      FROM recharge_orders
                     WHERE id = %s
                    """,
                    (source_order_id,),
                )
                src = cur.fetchone()
                if src:
                    status = src["s"] if isinstance(src, dict) else src[0]
                    if status in ("refunded", "partially_refunded"):
                        if not dry_run:
                            cur.execute(
                                """
                                UPDATE service_fee_records
                                   SET status = %s, clawback_reason = 'auto:refunded_before_settle'
                                 WHERE id = %s
                                """,
                                (ServiceFeeStatus.CANCELLED, rid),
                            )
                        skipped_refunded += 1
                        continue

                if not dry_run:
                    cur.execute(
                        """
                        UPDATE service_fee_records
                           SET status = %s, settled_at = NOW()
                         WHERE id = %s AND status = %s
                        """,
                        (ServiceFeeStatus.SETTLED, rid, ServiceFeeStatus.PENDING),
                    )
                affected += 1

            if not dry_run:
                conn.commit()

    logger.info("settle_due_service_fees: pending→settled=%d cancelled=%d dry=%s",
                affected, skipped_refunded, dry_run)
    return {
        "skipped": False,
        "settled": affected,
        "cancelled_due_refund": skipped_refunded,
        "dry_run": dry_run,
    }


# ============================================
# 服务费转充值积分(§3.5)
# ============================================

def _ensure_quota_row(user_id: int, agent_tier: str, yyyymm: int, cur) -> Decimal:
    """确保 service_fee_conversion_quota 当月行存在 · 返回 quota_yuan"""
    quota = Decimal(str(get_conversion_quota_yuan(agent_tier)))
    cur.execute(
        """
        INSERT INTO service_fee_conversion_quota (user_id, yyyymm, quota_yuan, used_yuan, tier)
        VALUES (%s, %s, %s, 0, %s)
        ON CONFLICT (user_id, yyyymm) DO NOTHING
        """,
        (user_id, yyyymm, float(quota), agent_tier),
    )
    return quota


def convert_service_fee_to_points(
    user_id: int,
    amount_yuan: float,
    *,
    allow_review_path: bool = True,
    skip_quota_check: bool = False,
) -> Dict[str, Any]:
    """L2 服务费转充值积分

    Returns:
        {
            "conversion_order_id": str,
            "paid_points_granted": int,
            "bonus_points_granted": int,
            "bonus_rate": float,
            "bonus_expires_at": iso str,
            "used_records": [int],
        }

    Raises:
        FeatureDisabledError
        NotL2AgentError
        InsufficientBalanceError
        SettledRecordsWithinRefundWindowError
        MonthlyQuotaExceededError
    """
    if not is_v3_3_1_enabled() or not is_service_fee_conversion_enabled():
        raise FeatureDisabledError("V3.3.1 服务费转积分功能未启用")

    if not is_l2_agent(user_id):
        raise NotL2AgentError("仅 L2 代理可转换服务费")

    amount = Decimal(str(amount_yuan)).quantize(Decimal("0.01"))
    if amount <= 0:
        raise ServiceFeeError("转换金额必须 > 0")

    wallet = get_user_wallet(user_id)
    agent_tier = wallet.get("agent_tier", "standard") if wallet else "standard"
    yyyymm = int(datetime.now().strftime("%Y%m"))
    min_age_days = get_conversion_min_age_days()
    available_cutoff = datetime.now() - timedelta(days=min_age_days)
    bonus_rate = Decimal(str(get_conversion_bonus_rate()))

    with get_db() as conn:
        with conn.cursor() as cur:
            # 1. 检查月度配额(Codex 三审 P1-2:skip_quota_check=True 时跳过 · 用于大额审批后入账)
            quota = _ensure_quota_row(user_id, agent_tier, yyyymm, cur)
            cur.execute(
                "SELECT used_yuan FROM service_fee_conversion_quota WHERE user_id=%s AND yyyymm=%s FOR UPDATE",
                (user_id, yyyymm),
            )
            row = cur.fetchone()
            used = Decimal(str((row["used_yuan"] if isinstance(row, dict) else row[0]) or 0))
            if not skip_quota_check and used + amount > quota:
                if allow_review_path:
                    # 留 conversion_review_pending 入口 · 由 service_fee_api 调用 convert-large
                    raise MonthlyQuotaExceededError(
                        "月度转换额度已用尽",
                        used_yuan=float(used),
                        quota_yuan=float(quota),
                        available_yuan=float(quota - used),
                        large_convert_endpoint="/api/service-fee/convert-large",
                    )
                raise MonthlyQuotaExceededError("月度转换额度已用尽")

            # 2. 锁定符合条件的 settled records(过 T+7 退款期 · FIFO 顺序)
            cur.execute(
                """
                SELECT id, amount_yuan, available_at
                  FROM service_fee_records
                 WHERE user_id = %s
                   AND status = %s
                   AND available_at <= %s
                 ORDER BY available_at ASC
                 FOR UPDATE
                """,
                (user_id, ServiceFeeStatus.SETTLED, available_cutoff),
            )
            ready_rows = cur.fetchall()

            ready_total = Decimal("0")
            for r in ready_rows:
                ready_total += Decimal(str(r["amount_yuan"] if isinstance(r, dict) else r[1]))

            if ready_total < amount:
                # 探明:是否有 settled 但未过退款期的余额
                cur.execute(
                    """
                    SELECT COUNT(*) AS c, COALESCE(SUM(amount_yuan), 0) AS s
                      FROM service_fee_records
                     WHERE user_id = %s AND status = %s AND available_at > %s
                    """,
                    (user_id, ServiceFeeStatus.SETTLED, available_cutoff),
                )
                in_win = cur.fetchone()
                window_count = (in_win["c"] if isinstance(in_win, dict) else in_win[0]) or 0
                window_amount = (in_win["s"] if isinstance(in_win, dict) else in_win[1]) or 0
                if window_count and (ready_total + Decimal(str(window_amount))) >= amount:
                    raise SettledRecordsWithinRefundWindowError(
                        "部分服务费未过退款/投诉观察期 · 暂不可转换",
                        ready_amount_yuan=float(ready_total),
                        records_in_window=int(window_count),
                        records_window_amount_yuan=float(window_amount),
                        min_age_days=min_age_days,
                    )
                raise InsufficientBalanceError(
                    f"可结算余额不足 · 可用 ¥{float(ready_total)} 申请 ¥{float(amount)}",
                    ready_amount_yuan=float(ready_total),
                )

            # 3. 按 FIFO 标记 converted
            used_record_ids: List[int] = []
            remaining = amount
            for r in ready_rows:
                if remaining <= 0:
                    break
                rid = r["id"] if isinstance(r, dict) else r[0]
                ramount = Decimal(str(r["amount_yuan"] if isinstance(r, dict) else r[1]))
                if ramount <= remaining:
                    used_record_ids.append(rid)
                    cur.execute(
                        """
                        UPDATE service_fee_records
                           SET status = %s, converted_at = NOW()
                         WHERE id = %s AND status = %s
                        """,
                        (ServiceFeeStatus.CONVERTED, rid, ServiceFeeStatus.SETTLED),
                    )
                    remaining -= ramount
                else:
                    # 部分转换 · 拆出一行 converted · 剩余保留 settled
                    # 拆分行 source_order_id 加 '_split_<rid>_<timestamp_ms>' · 防 UNIQUE 撞键
                    cur.execute(
                        """
                        INSERT INTO service_fee_records (
                            user_id, source_user_id, source_order_id, source_order_type,
                            source_payment_method, source_payment_source,
                            gross_amount_yuan, refund_amount_yuan, media_cost_yuan,
                            coupon_yuan, bonus_deducted_yuan, granted_points_deducted_yuan,
                            gateway_fee_yuan, net_cash_revenue_yuan,
                            amount_yuan, service_fee_rate, status,
                            available_at, settled_at, converted_at,
                            review_note
                        )
                        SELECT user_id, source_user_id,
                               source_order_id || '_split_' || %s::text || '_' || EXTRACT(EPOCH FROM NOW())::bigint::text,
                               source_order_type,
                               source_payment_method, source_payment_source,
                               0, 0, 0, 0, 0, 0, 0, 0,
                               %s, service_fee_rate, %s,
                               available_at, settled_at, NOW(),
                               'split_from:' || %s::text
                          FROM service_fee_records
                         WHERE id = %s
                         RETURNING id
                        """,
                        (rid, float(remaining), ServiceFeeStatus.CONVERTED, rid, rid),
                    )
                    split_id = cur.fetchone()
                    if split_id:
                        used_record_ids.append(
                            split_id["id"] if isinstance(split_id, dict) else split_id[0]
                        )
                    cur.execute(
                        "UPDATE service_fee_records SET amount_yuan = amount_yuan - %s WHERE id = %s",
                        (float(remaining), rid),
                    )
                    remaining = Decimal("0")

            # 4. 计算积分
            paid_points = int((amount * POINTS_PER_YUAN).quantize(Decimal("1")))
            bonus_points = int((amount * POINTS_PER_YUAN * bonus_rate).quantize(Decimal("1")))
            bonus_expires_at = datetime.now() + timedelta(days=90)

            # 5. 写转换订单
            cur.execute(
                """
                INSERT INTO service_fee_conversion_orders (
                    service_fee_record_ids, user_id, amount_yuan,
                    paid_points_granted, bonus_points_granted, bonus_rate, bonus_expires_at,
                    status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'completed')
                RETURNING id
                """,
                (used_record_ids, user_id, float(amount),
                 paid_points, bonus_points, float(bonus_rate), bonus_expires_at),
            )
            row = cur.fetchone()
            conversion_id = row["id"] if isinstance(row, dict) else row[0]

            # 6. 入账 paid_points + bonus_points · source='service_fee_conversion'
            cur.execute(
                """
                UPDATE user_wallets
                   SET paid_points = COALESCE(paid_points, 0) + %s,
                       bonus_points = COALESCE(bonus_points, 0) + %s
                 WHERE user_id = %s
                """,
                (paid_points, bonus_points, user_id),
            )

            # 流水(point_transactions 实际表 · source 字段由 migration_007 加)
            cur.execute(
                """
                INSERT INTO point_transactions (
                    user_id, type, point_type, amount, balance_after,
                    description, order_id, source, created_at
                )
                SELECT %s, 'service_fee_conversion', 'paid', %s,
                       COALESCE(paid_points, 0),
                       %s, %s, %s, NOW()
                  FROM user_wallets WHERE user_id = %s
                """,
                (user_id, paid_points,
                 f"服务费转积分 #{conversion_id} +paid",
                 f"SFC-{conversion_id}",
                 PaymentSource.SERVICE_FEE_CONVERSION, user_id),
            )
            cur.execute(
                """
                INSERT INTO point_transactions (
                    user_id, type, point_type, amount, balance_after,
                    description, order_id, source, created_at
                )
                SELECT %s, 'service_fee_conversion', 'bonus', %s,
                       COALESCE(bonus_points, 0),
                       %s, %s, %s, NOW()
                  FROM user_wallets WHERE user_id = %s
                """,
                (user_id, bonus_points,
                 f"服务费转积分 #{conversion_id} +bonus({float(bonus_rate)*100:.0f}%)",
                 f"SFC-{conversion_id}",
                 PaymentSource.SERVICE_FEE_CONVERSION, user_id),
            )

            # 7. 月度配额累加
            cur.execute(
                """
                UPDATE service_fee_conversion_quota
                   SET used_yuan = used_yuan + %s
                 WHERE user_id = %s AND yyyymm = %s
                """,
                (float(amount), user_id, yyyymm),
            )

            conn.commit()

            return {
                "conversion_order_id": f"SFC-{conversion_id}",
                "conversion_id_int": conversion_id,
                "paid_points_granted": paid_points,
                "bonus_points_granted": bonus_points,
                "bonus_rate": float(bonus_rate),
                "bonus_expires_at": bonus_expires_at.isoformat(),
                "used_records": used_record_ids,
                "amount_yuan": float(amount),
            }


def request_large_conversion(user_id: int, amount_yuan: float, reason: str = "") -> Dict[str, Any]:
    """超月度额度的大额转换申请 · 写入 conversion_orders status='pending_review'(单订单闭环)

    Codex 三审 P1-3 修复:不再写"占位单"
    - 申请时:写 1 条 record · status='pending_review' / paid_points=0 / records=[] / requires_review=true
    - 审批通过:同一条 record 由 service_fee_review_api 调 approve_large_conversion 完成入账
    - 不再产生第二条 conversion order · 审计单订单闭环
    """
    if not is_v3_3_1_enabled() or not is_service_fee_conversion_enabled():
        raise FeatureDisabledError("V3.3.1 服务费转积分功能未启用")
    if not is_l2_agent(user_id):
        raise NotL2AgentError("仅 L2 代理可申请大额转换")

    amount = Decimal(str(amount_yuan)).quantize(Decimal("0.01"))
    if amount <= 0:
        raise ServiceFeeError("申请金额必须 > 0")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO service_fee_conversion_orders (
                    service_fee_record_ids, user_id, amount_yuan,
                    paid_points_granted, bonus_points_granted, bonus_rate,
                    bonus_expires_at, status,
                    requires_review, review_status
                ) VALUES (
                    ARRAY[]::BIGINT[], %s, %s,
                    0, 0, %s,
                    NOW() + INTERVAL '90 days',
                    'pending_review',
                    TRUE, 'pending'
                )
                RETURNING id
                """,
                (user_id, float(amount), float(get_conversion_bonus_rate())),
            )
            row = cur.fetchone()
            conn.commit()
            review_id = row["id"] if isinstance(row, dict) else row[0]
            return {
                "review_id": f"SFCREV-{review_id}",
                "review_id_int": review_id,
                "status": "pending",
                "amount_yuan": float(amount),
                "sla_days": "3-5",
                "reason": reason,
            }


def approve_large_conversion(review_id: int, reviewer_id: int) -> Dict[str, Any]:
    """审批通过大额转换 · 同一条 conversion_orders record 完成入账(单订单闭环)

    Codex 三审 P1-3 修复:
    - 不调 convert_service_fee_to_points 生成第二条 record
    - 同一条 review record 原地更新:status='completed' + 填实际 records/points
    - 同时锁 settled records + 入账 paid/bonus + 推进月度配额(skip_quota_check=True)

    Raises:
        ServiceFeeError 如果该 review 不存在 / 已审批 / 状态错
        InsufficientBalanceError / SettledRecordsWithinRefundWindowError
    """
    if not is_v3_3_1_enabled() or not is_service_fee_conversion_enabled():
        raise FeatureDisabledError("V3.3.1 服务费转积分功能未启用")

    bonus_rate_global = Decimal(str(get_conversion_bonus_rate()))

    with get_db() as conn:
        with conn.cursor() as cur:
            # 1. 锁定 review record
            cur.execute(
                """
                SELECT * FROM service_fee_conversion_orders
                 WHERE id = %s AND requires_review = TRUE AND review_status = 'pending'
                 FOR UPDATE
                """,
                (review_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ServiceFeeError(f"大额转换审批 {review_id} 不存在或非 pending 状态")
            r = dict(row) if isinstance(row, dict) else row
            user_id = r["user_id"]
            amount = Decimal(str(r["amount_yuan"]))

            wallet = get_user_wallet(user_id)
            agent_tier = wallet.get("agent_tier", "standard") if wallet else "standard"
            yyyymm = int(datetime.now().strftime("%Y%m"))
            min_age_days = get_conversion_min_age_days()
            available_cutoff = datetime.now() - timedelta(days=min_age_days)

            # 2. 锁定符合条件的 settled records(过 T+7 退款期 · FIFO)
            cur.execute(
                """
                SELECT id, amount_yuan FROM service_fee_records
                 WHERE user_id = %s AND status = %s AND available_at <= %s
                 ORDER BY available_at ASC
                 FOR UPDATE
                """,
                (user_id, ServiceFeeStatus.SETTLED, available_cutoff),
            )
            ready_rows = cur.fetchall()
            ready_total = sum(
                (Decimal(str(rr["amount_yuan"] if isinstance(rr, dict) else rr[1])) for rr in ready_rows),
                Decimal("0"),
            )
            if ready_total < amount:
                raise InsufficientBalanceError(
                    f"审批入账失败 · settled 余额 ¥{float(ready_total)} 不足 ¥{float(amount)}",
                    ready_amount_yuan=float(ready_total),
                )

            # 3. FIFO 消化 amount · 标记 used_record_ids
            used_record_ids: List[int] = []
            remaining = amount
            for rr in ready_rows:
                if remaining <= 0:
                    break
                rid = rr["id"] if isinstance(rr, dict) else rr[0]
                ramount = Decimal(str(rr["amount_yuan"] if isinstance(rr, dict) else rr[1]))
                if ramount <= remaining:
                    used_record_ids.append(rid)
                    cur.execute(
                        """
                        UPDATE service_fee_records
                           SET status = %s, converted_at = NOW()
                         WHERE id = %s AND status = %s
                        """,
                        (ServiceFeeStatus.CONVERTED, rid, ServiceFeeStatus.SETTLED),
                    )
                    remaining -= ramount
                else:
                    # 拆分 record · 部分 converted
                    cur.execute(
                        """
                        INSERT INTO service_fee_records (
                            user_id, source_user_id, source_order_id, source_order_type,
                            source_payment_method, source_payment_source,
                            gross_amount_yuan, refund_amount_yuan, media_cost_yuan,
                            coupon_yuan, bonus_deducted_yuan, granted_points_deducted_yuan,
                            gateway_fee_yuan, net_cash_revenue_yuan,
                            amount_yuan, service_fee_rate, status,
                            available_at, settled_at, converted_at,
                            review_note
                        )
                        SELECT user_id, source_user_id,
                               source_order_id || '_split_large_' || %s::text || '_' || EXTRACT(EPOCH FROM NOW())::bigint::text,
                               source_order_type,
                               source_payment_method, source_payment_source,
                               0, 0, 0, 0, 0, 0, 0, 0,
                               %s, service_fee_rate, %s,
                               available_at, settled_at, NOW(),
                               'large_conv_split:r' || %s::text || ':rev' || %s::text
                          FROM service_fee_records WHERE id = %s
                         RETURNING id
                        """,
                        (rid, float(remaining), ServiceFeeStatus.CONVERTED, rid, review_id, rid),
                    )
                    split = cur.fetchone()
                    if split:
                        used_record_ids.append(split["id"] if isinstance(split, dict) else split[0])
                    cur.execute(
                        "UPDATE service_fee_records SET amount_yuan = amount_yuan - %s WHERE id = %s",
                        (float(remaining), rid),
                    )
                    remaining = Decimal("0")

            if remaining > 0:
                raise InsufficientBalanceError(
                    "大额审批锁定失败 · 拆分异常",
                    locked_yuan=float(amount - remaining),
                )

            # 4. 计算积分
            paid_points = int((amount * POINTS_PER_YUAN).quantize(Decimal("1")))
            bonus_points = int((amount * POINTS_PER_YUAN * bonus_rate_global).quantize(Decimal("1")))
            bonus_expires_at = datetime.now() + timedelta(days=90)

            # 5. 原地更新 review record · 不创建新 conversion_order
            cur.execute(
                """
                UPDATE service_fee_conversion_orders
                   SET service_fee_record_ids = %s,
                       paid_points_granted = %s,
                       bonus_points_granted = %s,
                       bonus_rate = %s,
                       bonus_expires_at = %s,
                       status = 'completed',
                       review_status = 'approved',
                       reviewed_by = %s,
                       reviewed_at = NOW()
                 WHERE id = %s
                """,
                (used_record_ids, paid_points, bonus_points,
                 float(bonus_rate_global), bonus_expires_at,
                 reviewer_id, review_id),
            )

            # 6. 入账 paid + bonus
            cur.execute(
                """
                UPDATE user_wallets
                   SET paid_points = COALESCE(paid_points, 0) + %s,
                       bonus_points = COALESCE(bonus_points, 0) + %s
                 WHERE user_id = %s
                """,
                (paid_points, bonus_points, user_id),
            )

            # 流水(point_transactions source='service_fee_conversion')
            cur.execute(
                """
                INSERT INTO point_transactions (
                    user_id, type, point_type, amount, balance_after,
                    description, order_id, source, created_at
                )
                SELECT %s, 'service_fee_conversion', 'paid', %s,
                       COALESCE(paid_points, 0),
                       %s, %s, %s, NOW()
                  FROM user_wallets WHERE user_id = %s
                """,
                (user_id, paid_points,
                 f"大额转换审批入账 #{review_id} +paid",
                 f"SFCREV-{review_id}",
                 PaymentSource.SERVICE_FEE_CONVERSION, user_id),
            )
            cur.execute(
                """
                INSERT INTO point_transactions (
                    user_id, type, point_type, amount, balance_after,
                    description, order_id, source, created_at
                )
                SELECT %s, 'service_fee_conversion', 'bonus', %s,
                       COALESCE(bonus_points, 0),
                       %s, %s, %s, NOW()
                  FROM user_wallets WHERE user_id = %s
                """,
                (user_id, bonus_points,
                 f"大额转换审批入账 #{review_id} +bonus({float(bonus_rate_global)*100:.0f}%)",
                 f"SFCREV-{review_id}",
                 PaymentSource.SERVICE_FEE_CONVERSION, user_id),
            )

            # 7. 月度配额累加(审批通过的也算月度用量 · 防同月再绕)
            _ensure_quota_row(user_id, agent_tier, yyyymm, cur)
            cur.execute(
                """
                UPDATE service_fee_conversion_quota
                   SET used_yuan = used_yuan + %s
                 WHERE user_id = %s AND yyyymm = %s
                """,
                (float(amount), user_id, yyyymm),
            )

            conn.commit()

            return {
                "review_id": f"SFCREV-{review_id}",
                "review_id_int": review_id,
                "status": "completed",
                "paid_points_granted": paid_points,
                "bonus_points_granted": bonus_points,
                "bonus_rate": float(bonus_rate_global),
                "bonus_expires_at": bonus_expires_at.isoformat(),
                "used_records": used_record_ids,
                "amount_yuan": float(amount),
            }


def reject_large_conversion(review_id: int, reviewer_id: int, reason: str = "") -> Dict[str, Any]:
    """拒绝大额转换 · 单订单状态机:pending → rejected · 不入账"""
    if not is_v3_3_1_enabled():
        raise FeatureDisabledError("V3.3.1 未启用")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE service_fee_conversion_orders
                   SET status = 'cancelled',
                       review_status = 'rejected',
                       reviewed_by = %s, reviewed_at = NOW()
                 WHERE id = %s AND requires_review = TRUE AND review_status = 'pending'
                 RETURNING id
                """,
                (reviewer_id, review_id),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                raise ServiceFeeError(f"大额转换审批 {review_id} 不存在或非 pending")
            return {"review_id": f"SFCREV-{review_id}", "status": "rejected", "reason": reason}


# ============================================
# 提现申请(§3.3)
# ============================================

def get_weekly_pending_withdrawal_count(user_id: int) -> int:
    """本周已有 pending 提现申请数"""
    week_start = datetime.now() - timedelta(days=7)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM service_fee_settlements
                 WHERE user_id = %s
                   AND settlement_type = 'withdrawal'
                   AND status = 'pending'
                   AND created_at >= %s
                """,
                (user_id, week_start),
            )
            row = cur.fetchone()
            return int(row["c"] if isinstance(row, dict) else row[0])


def get_monthly_withdrawal_yuan(user_id: int, yyyymm: Optional[int] = None) -> Decimal:
    yyyymm = yyyymm or int(datetime.now().strftime("%Y%m"))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(amount_yuan), 0) AS s
                  FROM service_fee_settlements
                 WHERE user_id = %s
                   AND settlement_type = 'withdrawal'
                   AND status IN ('approved', 'completed', 'partial')
                   AND TO_CHAR(completed_at, 'YYYYMM') = %s
                """,
                (user_id, str(yyyymm)),
            )
            row = cur.fetchone()
            value = row["s"] if isinstance(row, dict) else row[0]
            return Decimal(str(value or 0))


def request_withdrawal(
    user_id: int,
    amount_yuan: float,
    *,
    bank_account_masked: str,
) -> Dict[str, Any]:
    """L2 申请人工提现"""
    if not is_v3_3_1_enabled() or not is_withdrawal_enabled():
        raise FeatureDisabledError("V3.3.1 提现功能未启用")

    if not is_l2_agent(user_id):
        raise NotL2AgentError("仅 L2 代理可申请提现")

    amount = Decimal(str(amount_yuan)).quantize(Decimal("0.01"))
    min_amount = Decimal(str(get_withdrawal_min_amount()))
    if amount < min_amount:
        raise WithdrawalPreconditionError(
            f"单笔提现 ≥ ¥{float(min_amount)}",
            min_amount_yuan=float(min_amount),
        )

    wallet = get_user_wallet(user_id)
    if not wallet:
        raise WithdrawalPreconditionError("用户钱包不存在")
    if not wallet.get("is_kyc_passed"):
        raise WithdrawalPreconditionError("请先完成实名认证(KYC)")
    if not wallet.get("bank_account_verified"):
        raise WithdrawalPreconditionError("请先绑定实名收款账户")

    # 发票门槛 / 双签门槛(基于当前已 settled 状态预读 · 实际值在事务内核对)
    invoice_threshold = Decimal(str(get_withdrawal_invoice_threshold()))
    dual_sign_threshold = Decimal(str(get_withdrawal_dual_sign_threshold()))
    requires_dual_sign = amount > dual_sign_threshold

    # Codex 二审 P0-6 + 三审 P1-5:并发账务洞修复
    # - PG advisory lock(user-level)防同用户两笔并发提现都过频次锁
    # - 频次锁 / 余额检查 / 锁记录 / 写 settlement 全部在同一事务 + FOR UPDATE 锁内
    # - remaining > 0 → 报错(不允许部分申请 · 用户必须重发更小金额)
    # - used_ids 总和 == amount 才允许写 settlement
    # advisory lock 命名空间 0xC0DEAF · 第二参数 user_id · 同 user 互斥 · 事务结束自动释放
    ADVISORY_LOCK_NAMESPACE = 0xC0DEAF
    with get_db() as conn:
        with conn.cursor() as cur:
            # Step 0:user-level advisory lock(同 user 提现请求串行 · 防 ToCToU 频次缝)
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                        (ADVISORY_LOCK_NAMESPACE, int(user_id)))

            # 频次锁(事务内 · 防并发同时申请)
            week_start = datetime.now() - timedelta(days=7)
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM service_fee_settlements
                 WHERE user_id = %s AND settlement_type = 'withdrawal'
                   AND status = 'pending' AND created_at >= %s
                """,
                (user_id, week_start),
            )
            row_c = cur.fetchone()
            weekly_pending = int((row_c["c"] if isinstance(row_c, dict) else row_c[0]) or 0)
            if weekly_pending >= get_withdrawal_max_per_week():
                raise WithdrawalRateLimitError(
                    "本周已有提现申请待审核 · 请等待",
                    weekly_max=get_withdrawal_max_per_week(),
                    weekly_pending=weekly_pending,
                )

            # 月累计已提现(事务内查 · 防发票门槛误判)
            yyyymm = datetime.now().strftime("%Y%m")
            cur.execute(
                """
                SELECT COALESCE(SUM(COALESCE(partial_amount_yuan, amount_yuan)), 0) AS s
                  FROM service_fee_settlements
                 WHERE user_id = %s AND settlement_type = 'withdrawal'
                   AND status IN ('approved', 'completed', 'partial')
                   AND TO_CHAR(completed_at, 'YYYYMM') = %s
                """,
                (user_id, yyyymm),
            )
            row_m = cur.fetchone()
            monthly_withdrawn = Decimal(str((row_m["s"] if isinstance(row_m, dict) else row_m[0]) or 0))
            invoice_required = (monthly_withdrawn + amount) > invoice_threshold

            # 锁定 settled records · 状态 → withdraw_requested(FIFO)
            cur.execute(
                """
                SELECT id, amount_yuan FROM service_fee_records
                 WHERE user_id = %s AND status = %s
                 ORDER BY available_at ASC
                 FOR UPDATE
                """,
                (user_id, ServiceFeeStatus.SETTLED),
            )
            ready_rows = cur.fetchall()

            # 事务内余额校验(防 ToCToU 竞态)
            ready_total = sum(
                (Decimal(str(r["amount_yuan"] if isinstance(r, dict) else r[1])) for r in ready_rows),
                Decimal("0"),
            )
            if ready_total < amount:
                raise InsufficientBalanceError(
                    f"可结算余额 ¥{float(ready_total)} 不足 ¥{float(amount)}",
                    settled_balance_yuan=float(ready_total),
                )

            used_ids: List[int] = []
            locked_total = Decimal("0")
            remaining = amount
            for r in ready_rows:
                if remaining <= 0:
                    break
                rid = r["id"] if isinstance(r, dict) else r[0]
                ramount = Decimal(str(r["amount_yuan"] if isinstance(r, dict) else r[1]))
                if ramount <= remaining:
                    used_ids.append(rid)
                    cur.execute(
                        """
                        UPDATE service_fee_records
                           SET status = %s, withdraw_requested_at = NOW()
                         WHERE id = %s AND status = %s
                        """,
                        (ServiceFeeStatus.WITHDRAW_REQUESTED, rid, ServiceFeeStatus.SETTLED),
                    )
                    locked_total += ramount
                    remaining -= ramount
                else:
                    # 拆分 settled 行 · 一部分进入提现锁 · source_order_id 加唯一后缀防 UNIQUE 撞键
                    cur.execute(
                        """
                        INSERT INTO service_fee_records (
                            user_id, source_user_id, source_order_id, source_order_type,
                            source_payment_method, source_payment_source,
                            net_cash_revenue_yuan,
                            amount_yuan, service_fee_rate, status,
                            available_at, settled_at, withdraw_requested_at,
                            review_note
                        )
                        SELECT user_id, source_user_id,
                               source_order_id || '_split_' || %s::text || '_' || EXTRACT(EPOCH FROM NOW())::bigint::text,
                               source_order_type,
                               source_payment_method, source_payment_source,
                               0,
                               %s, service_fee_rate, %s,
                               available_at, settled_at, NOW(),
                               'split_from:' || %s::text
                          FROM service_fee_records
                         WHERE id = %s
                         RETURNING id
                        """,
                        (rid, float(remaining), ServiceFeeStatus.WITHDRAW_REQUESTED, rid, rid),
                    )
                    split = cur.fetchone()
                    if split:
                        used_ids.append(split["id"] if isinstance(split, dict) else split[0])
                    cur.execute(
                        "UPDATE service_fee_records SET amount_yuan = amount_yuan - %s WHERE id = %s",
                        (float(remaining), rid),
                    )
                    locked_total += remaining
                    remaining = Decimal("0")

            # Codex P0-6 校验:remaining 必须 == 0 + locked_total 必须 == amount
            if remaining > 0 or locked_total != amount:
                conn.rollback()
                raise InsufficientBalanceError(
                    f"提现锁定失败 · 锁定 ¥{float(locked_total)} 缺 ¥{float(remaining)}(并发冲突或拆分异常)",
                    locked_yuan=float(locked_total),
                    remaining_yuan=float(remaining),
                )

            cur.execute(
                """
                INSERT INTO service_fee_settlements (
                    user_id, settlement_type, related_record_ids,
                    amount_yuan, status, bank_account_masked,
                    invoice_required, requires_dual_sign
                ) VALUES (
                    %s, 'withdrawal', %s,
                    %s, 'pending', %s,
                    %s, %s
                )
                RETURNING id
                """,
                (user_id, used_ids, float(amount), bank_account_masked,
                 invoice_required, requires_dual_sign),
            )
            row = cur.fetchone()
            conn.commit()
            settlement_id = row["id"] if isinstance(row, dict) else row[0]

            return {
                "settlement_id": settlement_id,
                "settlement_code": f"WD-{settlement_id}",
                "status": "pending",
                "review_sla_days": "5-10",
                "invoice_required": invoice_required,
                "requires_dual_sign": requires_dual_sign,
                "monthly_withdrawn_yuan": float(monthly_withdrawn),
                "amount_yuan": float(amount),
            }


# ============================================
# Clawback 双层(§3.4.1)
# ============================================

def clawback_bonus(recharge_order_id: str, reason: str) -> Dict[str, Any]:
    """已 settled 后退款 → bonus_clawback 双层

    LAYER 1:乐观扣 settled bonus_points
    LAYER 2:不足 → 写 bonus_clawback_pending(消费时抵扣)
    """
    if not is_v3_3_1_enabled():
        return {"skipped": True, "reason": "v3_3_1_disabled"}

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, referrer_id, bonus_points, status
                  FROM pending_bonus_records
                 WHERE recharge_order_id = %s AND status = 'settled'
                 ORDER BY id ASC
                 FOR UPDATE
                """,
                (recharge_order_id,),
            )
            records = cur.fetchall()
            if not records:
                return {"skipped": True, "reason": "no_settled_bonus_for_order"}

            results = []
            for r in records:
                rid = r["id"] if isinstance(r, dict) else r[0]
                referrer_id = r["referrer_id"] if isinstance(r, dict) else r[1]
                bonus = int(r["bonus_points"] if isinstance(r, dict) else r[2])

                # LAYER 1: 扣 bonus_points 余额
                cur.execute(
                    "SELECT COALESCE(bonus_points,0) AS b FROM user_wallets WHERE user_id=%s FOR UPDATE",
                    (referrer_id,),
                )
                w = cur.fetchone()
                current = int((w["b"] if isinstance(w, dict) else w[0]) or 0) if w else 0

                if current >= bonus:
                    cur.execute(
                        "UPDATE user_wallets SET bonus_points = bonus_points - %s WHERE user_id=%s",
                        (bonus, referrer_id),
                    )
                    cur.execute(
                        "UPDATE pending_bonus_records SET status='clawback', cancel_reason=%s WHERE id=%s",
                        (f"clawback:{reason}", rid),
                    )
                    results.append({
                        "record_id": rid, "user_id": referrer_id,
                        "clawback_yuan": bonus / float(POINTS_PER_YUAN),
                        "layer": 1, "fully_settled": True,
                    })
                else:
                    remaining = bonus - current
                    if current > 0:
                        cur.execute(
                            "UPDATE user_wallets SET bonus_points = 0 WHERE user_id=%s",
                            (referrer_id,),
                        )
                    # LAYER 2: 债务表
                    cur.execute(
                        """
                        INSERT INTO bonus_clawback_pending (
                            user_id, recharge_order_id, amount_due,
                            amount_settled, status, reason
                        ) VALUES (%s, %s, %s, %s, 'pending', %s)
                        ON CONFLICT (user_id, recharge_order_id) DO UPDATE
                          SET amount_due = bonus_clawback_pending.amount_due + EXCLUDED.amount_due
                        """,
                        (referrer_id, recharge_order_id, remaining, current, f"clawback:{reason}"),
                    )
                    cur.execute(
                        "UPDATE pending_bonus_records SET status='clawback', cancel_reason=%s WHERE id=%s",
                        (f"clawback_partial:{reason}", rid),
                    )
                    results.append({
                        "record_id": rid, "user_id": referrer_id,
                        "clawback_yuan": bonus / float(POINTS_PER_YUAN),
                        "layer": 2,
                        "settled_now_points": current,
                        "pending_debt_points": remaining,
                    })

            conn.commit()
            return {"records": results}


def clawback_service_fee(source_order_id: str, reason: str) -> Dict[str, Any]:
    """已 settled 服务费退款 → 双层 clawback"""
    if not is_v3_3_1_enabled():
        return {"skipped": True, "reason": "v3_3_1_disabled"}

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, amount_yuan, status
                  FROM service_fee_records
                 WHERE source_order_id = %s AND status = %s
                 FOR UPDATE
                """,
                (source_order_id, ServiceFeeStatus.SETTLED),
            )
            records = cur.fetchall()
            if not records:
                return {"skipped": True, "reason": "no_settled_service_fee_for_order"}

            results = []
            for r in records:
                rid = r["id"] if isinstance(r, dict) else r[0]
                user_id = r["user_id"] if isinstance(r, dict) else r[1]
                amount = Decimal(str(r["amount_yuan"] if isinstance(r, dict) else r[2]))

                # LAYER 1: 直接 clawback 本条 settled record(已 settled 退款触发)
                cur.execute(
                    """
                    UPDATE service_fee_records
                       SET status = %s, clawback_amount = %s,
                           clawback_reason = %s
                     WHERE id = %s
                    """,
                    (ServiceFeeStatus.CLAWBACK, float(amount), f"clawback:{reason}", rid),
                )
                results.append({
                    "record_id": rid, "user_id": user_id,
                    "clawback_yuan": float(amount), "layer": 1,
                })
                # 后续异常追索走 clawback_after_conversion(§3.5.6)处理

            conn.commit()
            return {"records": results}


def clawback_after_conversion(
    user_id: int,
    source_order_id: str,
    refund_amount_yuan: float,
    refund_reason: str,
) -> Dict[str, Any]:
    """异常退款追索(§3.5.6)· 4 类 reason 才触发

    抵扣顺序:
        1. 后续 settled service_fee
        2. bonus_points 余额
        3. paid_points 余额
        4. 长期挂账 service_fee_clawback_pending(90 天后转法务)
    """
    if not is_v3_3_1_enabled():
        return {"skipped": True, "reason": "v3_3_1_disabled"}

    if not is_anomalous_refund(refund_reason):
        return {"skipped": True, "reason": f"refund_reason_not_anomalous({refund_reason})"}

    refund = Decimal(str(refund_amount_yuan)).quantize(Decimal("0.01"))
    remaining = refund

    with get_db() as conn:
        with conn.cursor() as cur:
            # 1. 查关联的 conversion order(确认是已转换的)
            cur.execute(
                """
                SELECT id FROM service_fee_conversion_orders
                 WHERE user_id = %s
                   AND %s = ANY(
                       SELECT source_order_id FROM service_fee_records
                        WHERE id = ANY(service_fee_record_ids)
                   )
                """,
                (user_id, source_order_id),
            )
            converted_check = cur.fetchall()
            if not converted_check:
                return {"skipped": True, "reason": "no_conversion_for_order"}

            steps: List[dict] = []

            # 2. 扣后续 settled service_fee
            cur.execute(
                """
                SELECT id, amount_yuan FROM service_fee_records
                 WHERE user_id = %s AND status = %s
                 ORDER BY available_at ASC
                 FOR UPDATE
                """,
                (user_id, ServiceFeeStatus.SETTLED),
            )
            settled_rows = cur.fetchall()
            for r in settled_rows:
                if remaining <= 0:
                    break
                rid = r["id"] if isinstance(r, dict) else r[0]
                amt = Decimal(str(r["amount_yuan"] if isinstance(r, dict) else r[1]))
                deduct = min(remaining, amt)
                if deduct >= amt:
                    cur.execute(
                        "UPDATE service_fee_records SET status=%s, clawback_amount=%s, clawback_reason=%s WHERE id=%s",
                        (ServiceFeeStatus.CLAWBACK, float(amt),
                         f"anomaly_clawback:{refund_reason}", rid),
                    )
                else:
                    cur.execute(
                        "UPDATE service_fee_records SET amount_yuan = amount_yuan - %s WHERE id=%s",
                        (float(deduct), rid),
                    )
                steps.append({"layer": "settled_service_fee", "yuan": float(deduct), "record_id": rid})
                remaining -= deduct

            # 3. 扣 bonus_points
            if remaining > 0:
                cur.execute(
                    "SELECT COALESCE(bonus_points,0) AS b FROM user_wallets WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                w = cur.fetchone()
                bonus_pts = int((w["b"] if isinstance(w, dict) else w[0]) or 0) if w else 0
                bonus_yuan = Decimal(bonus_pts) / POINTS_PER_YUAN
                deduct = min(remaining, bonus_yuan)
                deduct_pts = int((deduct * POINTS_PER_YUAN).quantize(Decimal("1")))
                if deduct_pts > 0:
                    cur.execute(
                        "UPDATE user_wallets SET bonus_points = bonus_points - %s WHERE user_id=%s",
                        (deduct_pts, user_id),
                    )
                    steps.append({"layer": "bonus_points", "yuan": float(deduct), "points": deduct_pts})
                    remaining -= deduct

            # 4. 扣 paid_points
            if remaining > 0:
                cur.execute(
                    "SELECT COALESCE(paid_points,0) AS p FROM user_wallets WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                w = cur.fetchone()
                paid_pts = int((w["p"] if isinstance(w, dict) else w[0]) or 0) if w else 0
                paid_yuan = Decimal(paid_pts) / POINTS_PER_YUAN
                deduct = min(remaining, paid_yuan)
                deduct_pts = int((deduct * POINTS_PER_YUAN).quantize(Decimal("1")))
                if deduct_pts > 0:
                    cur.execute(
                        "UPDATE user_wallets SET paid_points = paid_points - %s WHERE user_id=%s",
                        (deduct_pts, user_id),
                    )
                    steps.append({"layer": "paid_points", "yuan": float(deduct), "points": deduct_pts})
                    remaining -= deduct

            # 5. 仍剩余 → 写债务表(长期挂账)
            if remaining > 0:
                cur.execute(
                    """
                    INSERT INTO service_fee_clawback_pending (
                        user_id, source_order_id, amount_due,
                        status, reason, refund_category
                    ) VALUES (%s, %s, %s, 'pending', %s, %s)
                    ON CONFLICT (user_id, source_order_id) DO UPDATE
                      SET amount_due = service_fee_clawback_pending.amount_due + EXCLUDED.amount_due
                    """,
                    (user_id, source_order_id, float(remaining),
                     f"anomaly_long_term:{refund_reason}", refund_reason),
                )
                steps.append({"layer": "long_term_debt", "yuan": float(remaining)})

            conn.commit()
            return {
                "user_id": user_id,
                "source_order_id": source_order_id,
                "refund_reason": refund_reason,
                "refund_amount_yuan": float(refund),
                "remaining_yuan": float(remaining),
                "steps": steps,
            }


# ============================================
# 退款三段(§3.4)
# ============================================

def process_recharge_refund(
    recharge_order_id: str,
    *,
    refund_reason: str = "user_request",
) -> Dict[str, Any]:
    """充值退款触发 · 按 T+3 / T+7 三段处理 + 异常追索

    pending → cancelled
    settled → clawback 双层
    """
    if not is_v3_3_1_enabled():
        return {"skipped": True, "reason": "v3_3_1_disabled"}

    results = {"bonus": [], "service_fee": [], "anomaly_clawback": None}

    with get_db() as conn:
        with conn.cursor() as cur:
            # pending bonus → cancelled
            cur.execute(
                """
                UPDATE pending_bonus_records SET status='cancelled', cancel_reason=%s
                 WHERE recharge_order_id = %s AND status = 'pending'
                 RETURNING id, referrer_id, bonus_points
                """,
                (f"refund:{refund_reason}", recharge_order_id),
            )
            for r in cur.fetchall():
                results["bonus"].append({
                    "record_id": r["id"] if isinstance(r, dict) else r[0],
                    "action": "cancelled_pending",
                })

            # pending service_fee → cancelled
            cur.execute(
                """
                UPDATE service_fee_records
                   SET status = %s, clawback_reason = %s
                 WHERE source_order_id = %s AND status = %s
                 RETURNING id, user_id, amount_yuan
                """,
                (ServiceFeeStatus.CANCELLED, f"refund:{refund_reason}",
                 recharge_order_id, ServiceFeeStatus.PENDING),
            )
            for r in cur.fetchall():
                results["service_fee"].append({
                    "record_id": r["id"] if isinstance(r, dict) else r[0],
                    "action": "cancelled_pending",
                })
            conn.commit()

    # settled 的走 clawback 双层
    bonus_clawback = clawback_bonus(recharge_order_id, refund_reason)
    sf_clawback = clawback_service_fee(recharge_order_id, refund_reason)
    if bonus_clawback.get("records"):
        results["bonus"].extend([{**x, "action": "clawback"} for x in bonus_clawback["records"]])
    if sf_clawback.get("records"):
        results["service_fee"].extend([{**x, "action": "clawback"} for x in sf_clawback["records"]])

    # 异常退款 4 类 → 追索已转换的服务费
    if is_anomalous_refund(refund_reason):
        # 找 conversion order 的 user_id · 用 JOIN(可读性 > 嵌套 ANY)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT co.user_id
                      FROM service_fee_conversion_orders co
                      JOIN service_fee_records sfr ON sfr.id = ANY(co.service_fee_record_ids)
                     WHERE sfr.source_order_id = %s
                    """,
                    (recharge_order_id,),
                )
                rows = cur.fetchall()
                # 查源订单 amount_cents(recharge_orders 实际字段)
                cur.execute(
                    "SELECT amount_cents FROM recharge_orders WHERE id = %s",
                    (recharge_order_id,),
                )
                o = cur.fetchone()
                cents = (o["amount_cents"] if isinstance(o, dict) else o[0]) if o else 0
                refund_yuan = (cents or 0) / 100.0

                for row in rows:
                    uid = row["user_id"] if isinstance(row, dict) else row[0]
                    results["anomaly_clawback"] = clawback_after_conversion(
                        uid, recharge_order_id, refund_yuan, refund_reason
                    )

    return results
