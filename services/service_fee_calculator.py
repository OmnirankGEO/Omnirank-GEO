"""
V3.3.1 服务费 / 推荐奖励写入核心

设计:
- 不污染老 api/referral_api.py(V3.1 已部 · 灰度期共存)
- 由 referral_api.process_recharge_rewards() 在 V3.3.1 总开关启用时调用本模块
- 双写老 pending_commissions + 新 service_fee_records(由 feature flag 控制)
- UNIQUE 3 列 防回调重试
- net_cash 基数 防毛利穿透
- L0/L1 → bonus 15% / L2 → service_fee(比例见 v3_3_1_flags)· 单级直推 · 取消间推

入口:
- record_v3_3_1_rewards(order_dict)  · 由 referral_api 调用

关联:
- 决策书 §3.1 / §3.6 / §9.1
- RED_LINES.md R1
- IDENTITY_DECISIONS_LOCK Q6 / Q7 / Q23-Q25 / Q27 / Q31-Q32
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any

from db.connection import get_db
from config.v3_3_1_flags import (
    is_v3_3_1_enabled,
    is_dual_write_enabled,
    is_net_cash_revenue_base,
    is_revenue_triggering_source,
    get_service_fee_rate,
    get_service_fee_settle_days,
    get_referral_bonus_rate,
    get_referral_bonus_settle_days,
    PaymentSource,
    ServiceFeeStatus,
)
from services.net_cash_revenue import (
    compute_net_cash,
    compute_from_order,
    compute_service_fee,
    compute_referral_bonus_points,
)

logger = logging.getLogger("GEO-ServiceFee-Calc")


# ============================================
# 邀请链查找
# ============================================

def find_direct_inviter(user_id: int) -> Optional[dict]:
    """查直接邀请人(level=1 of referral_links 老 V3.2 表)· 返 None 表示无邀请人

    V3.3.1 修复:复用已存在的 referral_links 表(非造新 referral_relations 表)
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rl.referrer_id, uw.agent_level, uw.agent_tier
                      FROM referral_links rl
                      LEFT JOIN user_wallets uw ON uw.user_id = rl.referrer_id
                     WHERE rl.referred_id = %s AND rl.level = 1
                     LIMIT 1
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "user_id": row["referrer_id"] if isinstance(row, dict) else row[0],
                    "agent_level": (row["agent_level"] if isinstance(row, dict) else row[1]) or 0,
                    "agent_tier": (row["agent_tier"] if isinstance(row, dict) else row[2]) or "standard",
                }
    except Exception as exc:
        logger.warning("find_direct_inviter: referral_links query failed · %s", exc)
        return None


def find_upstream_l2(user_id: int, max_hops: int = 10) -> Optional[dict]:
    """沿邀请链上溯找最近 L2(agent_level >= 2)· 决策书 §3.1 单级直推穿透

    优先级:
      1. 直接邀请人若为 L2 → 用它
      2. referral_links level=2(老 V3.2 已存的间接关系)
      3. 沿 level=1 上溯查 max_hops 跳

    "单级直推"指 L2 直推率(无间推)· 不是邀请链层数限制。
    """
    # 直接邀请人若已 L2 · 不上溯
    direct = find_direct_inviter(user_id)
    if direct and direct.get("agent_level", 0) >= 2:
        return direct

    # 老 V3.2 level=2 间接邀请关系
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rl.referrer_id, uw.agent_level, uw.agent_tier
                      FROM referral_links rl
                      LEFT JOIN user_wallets uw ON uw.user_id = rl.referrer_id
                     WHERE rl.referred_id = %s AND rl.level = 2
                       AND COALESCE(uw.agent_level, 0) >= 2
                     LIMIT 1
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if row:
                    return {
                        "user_id": row["referrer_id"] if isinstance(row, dict) else row[0],
                        "agent_level": (row["agent_level"] if isinstance(row, dict) else row[1]) or 0,
                        "agent_tier": (row["agent_tier"] if isinstance(row, dict) else row[2]) or "standard",
                    }
    except Exception as exc:
        logger.warning("find_upstream_l2: level=2 query failed · %s", exc)

    # 沿 level=1 链路上溯(老数据无 level=2 时兜底)
    current = direct
    hops = 0
    while current and hops < max_hops:
        if current.get("agent_level", 0) >= 2:
            return current
        current = find_direct_inviter(current["user_id"])
        hops += 1
    return None


# ============================================
# 自推检测(Q11 · §3.6.2 防套利)
# ============================================

def _is_self_referral(payer_id: int, beneficiary_id: int) -> bool:
    """自己邀请自己充值 → 0 bonus 0 service_fee"""
    return payer_id == beneficiary_id


# ============================================
# 是否首充(Q7 · bonus 仅首充)
# ============================================

def is_first_recharge(user_id: int, exclude_order_id: Optional[str] = None) -> bool:
    """判断该用户是否首次充值

    Args:
        exclude_order_id: 排除当前订单 · 防递归判断把自己算进去
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                # recharge_orders 实际字段 payment_status(非 status)· schema 真相
                if exclude_order_id:
                    cur.execute(
                        """
                        SELECT COUNT(*) AS c FROM recharge_orders
                         WHERE user_id = %s
                           AND payment_status = 'paid'
                           AND id != %s
                        """,
                        (user_id, exclude_order_id),
                    )
                else:
                    cur.execute(
                        "SELECT COUNT(*) AS c FROM recharge_orders WHERE user_id = %s AND payment_status = 'paid'",
                        (user_id,),
                    )
                row = cur.fetchone()
                count = row["c"] if isinstance(row, dict) else row[0]
                return count == 0
    except Exception as exc:
        logger.warning("is_first_recharge: query failed · fallback True · %s", exc)
        return True


# ============================================
# V3.3.1 主入口
# ============================================

def _order_paid_cents(order: Dict[str, Any]) -> int:
    """订单实付金额(分)。[WO_299b] 原来先把元转成浮点再乘 100 取整:浮点截断,1.15 元记成 114 分。

    两个调用方(`api/referral_api.py`、`services/service_fee_cron.py`)都同时传了整数 `paid_amount_cents`
    与 `paid_amount_yuan = cents / 100` ⇒ 有整数分就直接用;没有才把元精确换成分(非法金额抛错,不猜)。
    """
    from services.payment_amounts import yuan_to_fen

    cents = order.get("paid_amount_cents")
    if cents is not None:
        if isinstance(cents, bool) or int(cents) != cents or cents < 0:
            raise ValueError(f"paid_amount_cents 不是非负整数:{cents!r}")
        return int(cents)
    return yuan_to_fen(order.get("paid_amount_yuan") or 0)


def record_v3_3_1_rewards(order: Dict[str, Any]) -> Dict[str, Any]:
    """V3.3.1 服务费 + 推荐奖励主入口(由 referral_api 调用)

    Args:
        order: 充值/订阅订单 dict · 必含:
            - id (str)                    订单号
            - user_id (int)               付款用户
            - source (str)                §3.6 7 场景之一 · 仅 external_cash_payment 触发
            - order_type (str)            recharge / subscription
            - paid_amount_yuan (float)    支付到账金额
            可选:
            - refund_amount_yuan
            - media_cost_yuan
            - coupon_yuan
            - bonus_points_used
            - granted_points_deducted_yuan
            - gateway_fee_yuan
            - payment_method (wechat/alipay)

    Returns:
        {
            "skipped": bool · 是否跳过(总开关关 / source 不对 / net_cash<=0 / 自推)
            "skip_reason": str
            "bonus_record_id": Optional[int]      pending_bonus_records.id
            "service_fee_record_id": Optional[int] service_fee_records.id
            "net_cash_revenue_yuan": float
            "service_fee_amount_yuan": float
            "bonus_points": int
        }
    """
    result = {
        "skipped": False,
        "skip_reason": None,
        "bonus_record_id": None,
        "service_fee_record_id": None,
        "net_cash_revenue_yuan": 0.0,
        "service_fee_amount_yuan": 0.0,
        "bonus_points": 0,
    }

    # 1. V3.3.1 总开关
    if not is_v3_3_1_enabled():
        result["skipped"] = True
        result["skip_reason"] = "v3_3_1_disabled"
        return result

    # 2. source 校验 · §3.6 仅 external_cash_payment 触发
    # Codex 反馈:默认值不可 fall-open 到 external_cash_payment(反而触发返佣)
    # source 缺失 / 未知值 → 默认安全 = balance_deduction(不触发)· 强制调用方显式标 external_cash_payment
    raw_source = order.get("source")
    if raw_source is None:
        logger.warning("order %s · source field missing · fail-close to balance_deduction(safer)",
                       order.get("id"))
        source = PaymentSource.BALANCE_DEDUCTION
    else:
        source = raw_source
    if not is_revenue_triggering_source(source):
        logger.info("order %s source=%s · 跳过服务费/推荐奖励(V3.3 §3.6)", order.get("id"), source)
        result["skipped"] = True
        result["skip_reason"] = f"source_not_external_cash({source})"
        return result

    user_id = order.get("user_id")
    order_id = order.get("id")
    order_type = order.get("order_type") or "recharge"
    if not user_id or not order_id:
        result["skipped"] = True
        result["skip_reason"] = "missing_user_id_or_order_id"
        return result

    # 3. 净现金计算(§3.1.1)· 若 flag 关 → 退化为 gross
    if is_net_cash_revenue_base():
        breakdown = compute_from_order(order)
    else:
        breakdown = compute_net_cash(
            order.get("paid_amount_yuan") or 0,
            auto_gateway_fee=False,  # flag 未开 · 走简化基数
        )
    result["net_cash_revenue_yuan"] = float(breakdown.net_cash_revenue_yuan)

    if not breakdown.is_positive():
        logger.info(
            "order %s net_cash=%s ≤ 0 · 跳过服务费/推荐奖励",
            order_id, breakdown.net_cash_revenue_yuan,
        )
        result["skipped"] = True
        result["skip_reason"] = f"net_cash_non_positive({breakdown.net_cash_revenue_yuan})"
        return result

    # 4. 找邀请人
    direct_inviter = find_direct_inviter(user_id)
    if not direct_inviter:
        logger.info("order %s · no inviter · skip", order_id)
        result["skipped"] = True
        result["skip_reason"] = "no_inviter"
        return result

    if _is_self_referral(user_id, direct_inviter["user_id"]):
        logger.info("order %s · self_referral · skip", order_id)
        result["skipped"] = True
        result["skip_reason"] = "self_referral"
        return result

    # 5. 处理 L0/L1 直接邀请人 → 推荐奖励 15% bonus(首充)
    if direct_inviter["agent_level"] < 2:
        if is_first_recharge(user_id, exclude_order_id=order_id):
            bonus_pts = compute_referral_bonus_points(breakdown)
            result["bonus_points"] = bonus_pts
            record_id = _insert_pending_bonus(
                referrer_id=direct_inviter["user_id"],
                referred_id=user_id,
                bonus_points=bonus_pts,
                rate=get_referral_bonus_rate(),
                recharge_order_id=order_id,
                charger_user_id=user_id,
                recharge_amount_cents=_order_paid_cents(order),
                net_cash_revenue_yuan=float(breakdown.net_cash_revenue_yuan),
                source=source,
                settle_days=get_referral_bonus_settle_days(),
            )
            result["bonus_record_id"] = record_id
        else:
            logger.info("order %s · not first recharge · skip bonus", order_id)

    # 6. 找上游 L2 → 服务费(按服务费率)
    l2_inviter = (
        direct_inviter
        if direct_inviter["agent_level"] >= 2
        else find_upstream_l2(user_id)
    )
    if l2_inviter and not _is_self_referral(user_id, l2_inviter["user_id"]):
        service_fee_yuan = compute_service_fee(breakdown)
        result["service_fee_amount_yuan"] = float(service_fee_yuan)
        record_id = _insert_service_fee_record(
            user_id=l2_inviter["user_id"],
            source_user_id=user_id,
            source_order_id=order_id,
            source_order_type=order_type,
            source_payment_method=order.get("payment_method"),
            source_payment_source=source,
            breakdown=breakdown,
            amount_yuan=service_fee_yuan,
            service_fee_rate=Decimal(str(get_service_fee_rate())),
            settle_days=get_service_fee_settle_days(),
        )
        result["service_fee_record_id"] = record_id

        # 7. 双写老 pending_commissions(灰度期 · feature flag 控制)
        if is_dual_write_enabled():
            _dual_write_old_commissions(
                user_id=l2_inviter["user_id"],
                source_user_id=user_id,
                order_id=order_id,
                amount_yuan=float(service_fee_yuan),
                service_fee_rate=get_service_fee_rate(),
                settle_days=get_service_fee_settle_days(),
            )
    else:
        logger.info("order %s · no upstream L2 · skip service_fee", order_id)

    return result


# ============================================
# 内部:写入新表
# ============================================

def _insert_pending_bonus(
    *,
    referrer_id: int,
    referred_id: int,
    bonus_points: int,
    rate: float,
    recharge_order_id: str,
    charger_user_id: int,
    recharge_amount_cents: int,
    net_cash_revenue_yuan: float,
    source: str,
    settle_days: int,
) -> Optional[int]:
    """写 pending_bonus_records · UNIQUE 防回调重试

    异常处理(Codex 反馈 · 不静默):
    - UniqueViolation(回调重试) → 返 None + log info(正常 skip)
    - 其他错误(undefined_column / undefined_table / 网络等)→ raise · 让上层 referral_api hook 退化 V3.2 老逻辑
    """
    from psycopg2.errors import UniqueViolation

    settle_at = datetime.now() + timedelta(days=settle_days)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pending_bonus_records (
                        referrer_id, referred_id, bonus_points, rate, status,
                        settle_at, is_first_recharge,
                        recharge_order_id, charger_user_id, recharge_amount_cents,
                        net_cash_revenue_yuan, source
                    ) VALUES (
                        %s, %s, %s, %s, 'pending',
                        %s, true,
                        %s, %s, %s,
                        %s, %s
                    )
                    ON CONFLICT (recharge_order_id, referrer_id)
                    WHERE recharge_order_id IS NOT NULL
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        referrer_id, referred_id, bonus_points, rate,
                        settle_at,
                        recharge_order_id, charger_user_id, recharge_amount_cents,
                        net_cash_revenue_yuan, source,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                if row:
                    return row["id"] if isinstance(row, dict) else row[0]
                logger.info("pending_bonus order=%s referrer=%s · already exists · skip(回调重试)",
                            recharge_order_id, referrer_id)
                return None
    except UniqueViolation:
        # ON CONFLICT 已 DO NOTHING 兜底 · 这里几乎不会到达 · 防御 INSERT 路径变了
        logger.info("pending_bonus order=%s referrer=%s · UniqueViolation · skip",
                    recharge_order_id, referrer_id)
        return None
    except Exception:
        # schema 错位 / 字段缺失 等 · 不静默 · raise 让 caller 报警 + 退化老逻辑
        logger.exception("_insert_pending_bonus FAILED (non-recoverable) · raise")
        raise


def _insert_service_fee_record(
    *,
    user_id: int,
    source_user_id: int,
    source_order_id: str,
    source_order_type: str,
    source_payment_method: Optional[str],
    source_payment_source: str,
    breakdown,
    amount_yuan: Decimal,
    service_fee_rate: Decimal,
    settle_days: int,
) -> Optional[int]:
    """写 service_fee_records · UNIQUE 3 列防回调重试

    异常处理(Codex 反馈 · 不静默):
    - UniqueViolation(回调重试) → 返 None + log info
    - 其他错误 → raise · 让上层 referral_api hook 退化 V3.2 老逻辑
    """
    from psycopg2.errors import UniqueViolation

    available_at = datetime.now() + timedelta(days=settle_days)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO service_fee_records (
                        user_id, source_user_id, source_order_id, source_order_type,
                        source_payment_method, source_payment_source,
                        gross_amount_yuan, refund_amount_yuan, media_cost_yuan,
                        coupon_yuan, bonus_deducted_yuan, granted_points_deducted_yuan,
                        gateway_fee_yuan, net_cash_revenue_yuan,
                        amount_yuan, service_fee_rate,
                        status, available_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s,
                        %s, %s
                    )
                    ON CONFLICT ON CONSTRAINT uniq_service_fee_order_type_user
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        user_id, source_user_id, source_order_id, source_order_type,
                        source_payment_method, source_payment_source,
                        float(breakdown.gross_amount_yuan),
                        float(breakdown.refund_amount_yuan),
                        float(breakdown.media_cost_yuan),
                        float(breakdown.coupon_yuan),
                        float(breakdown.bonus_deducted_yuan),
                        float(breakdown.granted_points_deducted_yuan),
                        float(breakdown.gateway_fee_yuan),
                        float(breakdown.net_cash_revenue_yuan),
                        float(amount_yuan), float(service_fee_rate),
                        ServiceFeeStatus.PENDING, available_at,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                if row:
                    return row["id"] if isinstance(row, dict) else row[0]
                logger.info("service_fee order=%s · already exists · skip(回调重试)",
                            source_order_id)
                return None
    except UniqueViolation:
        logger.info("service_fee order=%s · UniqueViolation · skip",
                    source_order_id)
        return None
    except Exception:
        logger.exception("_insert_service_fee_record FAILED (non-recoverable) · raise")
        raise


def _dual_write_old_commissions(
    *,
    user_id: int,
    source_user_id: int,
    order_id: str,
    amount_yuan: float,
    service_fee_rate: float,
    settle_days: int,
) -> None:
    """灰度期双写 pending_commissions(V3.1/V3.2 老表 · feature flag 控制)

    V3.3.1 修复:老表实际字段 = amount_yuan(非 amount_points) + commission_rate + frozen_reason
    UNIQUE(order_id, user_id, level)由 migration_007 第 8 段补
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                available_at = datetime.now() + timedelta(days=settle_days)
                cur.execute(
                    """
                    INSERT INTO pending_commissions (
                        user_id, source_user_id, order_id, amount_yuan,
                        level, commission_rate, status, available_at, frozen_reason
                    ) VALUES (
                        %s, %s, %s, %s,
                        1, %s, 'pending', %s, %s
                    )
                    ON CONFLICT (order_id, user_id, level) DO NOTHING
                    """,
                    (user_id, source_user_id, order_id, float(amount_yuan),
                     float(service_fee_rate), available_at,
                     "v3_3_1_dual_write"),
                )
                conn.commit()
    except Exception as exc:
        # 老表不存在或字段不匹配时 · 不阻塞新表写入
        logger.warning("_dual_write_old_commissions failed (V3.1 老表可能不存在 · 容错): %s", exc)
