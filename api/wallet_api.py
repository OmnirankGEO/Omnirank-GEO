"""
积分钱包 API
- 余额查询
- 积分流水
- 充值包列表
- 创建充值订单
- 功能定价表
"""

import os
import json
import logging
import shortuuid
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from db.wallet_db import (
    get_wallet_balance, get_transactions, get_all_pricing,
    create_recharge_order, complete_recharge, get_or_create_wallet,
    get_recharge_order, count_transactions, _format_yuan_from_cents,
    INTERNAL_CUSTOMER_HIDDEN_TRANSACTION_TYPES,
)
from services.payment_routing import detect_payment_channel, resolve_payment_channel

logger = logging.getLogger("GEO-Wallet-API")

router = APIRouter(prefix="/api/wallet", tags=["积分钱包"])

# Readiness imports this only after the live `/customer/recharge` order endpoint
# supports persisted retail quotes without disabling the three payment channels.
PRICING_QUOTE_WIRING_CAPABILITY = "retail-price-quote-v1"
PRICING_QUOTE_WIRING_BEHAVIOR = {
    "contract_version": "geo-persisted-price-quote-v2",
    "entry_id": "customer-recharge",
    "active_route": "/customer/recharge",
    "quote_type": "retail",
    "quote_endpoint": "/api/pricing/retail/quote",
    "custom_amount_quote_endpoint": "/api/pricing/retail/custom-amount-quote",
    "supports_custom_amount": True,
    "cash_semantics": "payable_amount_immutable",
    "relationship_effect": "delivered_points_only",
    "order_endpoint": "/api/wallet/recharge",
    "required_order_field": "price_quote_id",
    "quote_persistence": "price_quotes",
    "callback_pricing_source": "order_snapshot",
    "quote_required_enforcement": "request_pre_writer",
}


def _pricing_quote_flags() -> tuple[bool, bool]:
    """Read rollout policy once; an unavailable policy cannot authorize a cash order."""
    from config.pricing_ssot_flags import PricingFlagsUnavailable, pricing_flags_snapshot

    try:
        flags = pricing_flags_snapshot()
    except PricingFlagsUnavailable as exc:
        raise HTTPException(
            503,
            detail={
                "code": "PRICING_POLICY_UNAVAILABLE",
                "message": "定价策略暂不可用 · 已暂停创建订单，请稍后重试",
            },
            headers={"Retry-After": "5"},
        ) from exc
    return (
        bool(flags.get("PRICING_DUAL_SSOT_ENABLED", False)),
        bool(flags.get("PRICING_QUOTE_REQUIRED", False)),
    )


def _fire_marketing_recharge_hook(result) -> None:
    """营销钩子(首充双倍/累充成长礼)· post-commit fail-open · 绝不影响支付路径。
    在 complete_recharge 调用方外层包装(不改其函数体 · 红线 db/wallet_db.py diff=0)。
    真实发放仅营销三闸全开时发生,否则内部自动 dry_run(彩排)。"""
    try:
        from services.marketing.executors.hooks import on_recharge_completed
        on_recharge_completed(result)
    except Exception as _e:  # noqa: BLE001
        logger.warning(f"[Wallet] 营销充值钩子异常(已吞·不影响支付): {_e}")


# ============================================================
# [BUG-P0-2] 退款未消耗积分计算 · 单点权威 helper
# ============================================================

def _compute_refund_unused_ratio(total_order_points: int, consumed_abs: int):
    """退款"未消耗积分 + 退款比例"的唯一计算入口(两处退款路径共用,防符号/上限漏改一处)。

    [BUG-P0-2 根因] point_transactions.consume 的 amount 恒为负(prod 953/953 全负)。
    调用方 SQL 必须用 SUM(ABS(amount)) 取真实消费总量(正);本函数再对结果夹紧:
      - unused 夹在 [0, total_order_points] → 即便上游 SQL 漏改 ABS(传入负 consumed),
        total-负=膨胀也会被 min(.., total) 兜回 total,绝不退超原单(fail-closed 兜底)。
      - ratio 夹在 [0, 1.0] → 退款金额绝不超过原单 100%。

    Args:
        total_order_points: 本次充值订单发放的总积分(base + bonus)
        consumed_abs:       支付后消费总量(应为 SUM(ABS(amount)) 的非负值)
    Returns:
        (unused_points:int, unused_ratio:float)
    """
    unused = min(max(0, total_order_points - consumed_abs), total_order_points)
    ratio = min(unused / total_order_points, 1.0) if total_order_points > 0 else 0.0
    return unused, ratio


def _decorate_wallet_transaction_for_display(tx: dict) -> dict:
    """Normalize customer-facing wallet transaction copy without mutating ledger rows."""
    if tx.get("type") == "recharge" and tx.get("recharge_amount_cents") is not None:
        tx["description"] = f"充值 ¥{_format_yuan_from_cents(tx.get('recharge_amount_cents'))}"
    elif tx.get("type") in INTERNAL_CUSTOMER_HIDDEN_TRANSACTION_TYPES:
        # [BUG-2 2026-07-27] 迁移流水不再对客户【整条消失】,改成客户能懂的人话。
        # 根因:原做法把这条藏掉(客户刚充 ¥165 就看到 -19,500 会以为被盗刷,隐藏的动机成立),
        # 但"流水藏了、余额也跟着藏了一半"→ 客户对不上账 → 投诉"钱少了"(生产 user 149 实证)。
        # 现在:余额端合并显示两个钱包(见 WalletContext),流水端把这条翻成"转入可用额度",
        # 金额记 0(既不吓人也不篡改真实账本 —— 后台 include_internal_migrations=True 仍是原始 -19,500)。
        from config.pricing_config import get_points_per_yuan
        _pts = abs(int(tx.get("amount") or 0))
        _yuan = _pts / max(1, get_points_per_yuan())
        _yuan_txt = str(int(_yuan)) if abs(_yuan - int(_yuan)) < 0.005 else f"{_yuan:.2f}"
        tx["description"] = f"充值 ¥{_yuan_txt} 已到账 · 转入可用额度（工具与发布均可用）"
        tx["amount"] = 0
        tx["display_kind"] = "credit_transfer"
        tx["transferred_points"] = _pts
    tx["feature_name"] = tx.get("description") or tx.get("feature_code") or None
    return tx


# ============================================================
# [#6] 支付回调一致性校验 helper（fix/security-audit-p0）
# ============================================================

def _verify_callback_consistency(
    order_id: str,
    callback_amount_cents: int,
    callback_label: str,
    extra_checks: dict = None,
) -> tuple:
    """校验支付回调金额跟本地订单金额一致，并校验额外字段（商户号/AppID 等）。

    Args:
        order_id: 商户订单号 (本地 recharge_orders.id)
        callback_amount_cents: 回调里携带的金额（单位：分）
        callback_label: 渠道标签 ("微信回调" / "虎皮椒回调")，用于日志
        extra_checks: dict { 字段名: (期望值, 回调值) }，任一不一致即拒绝

    Returns:
        (ok: bool, reason: str)
        ok=False 时 reason 是给日志和告警的描述
    """
    order = get_recharge_order(order_id)
    if not order:
        return False, f"{callback_label} 订单不存在: {order_id}"

    local_cents = order.get("amount_cents", 0)
    if int(local_cents) != int(callback_amount_cents):
        return False, (
            f"{callback_label} 金额不一致 "
            f"order={order_id} 本地={local_cents}分 回调={callback_amount_cents}分 "
            f"差额={callback_amount_cents - local_cents}分"
        )

    for field, pair in (extra_checks or {}).items():
        expected, actual = pair
        # 期望值/实际值任一为空就跳过（兼容回调可能没带某些字段）
        if not expected or not actual:
            continue
        if str(expected) != str(actual):
            return False, (
                f"{callback_label} {field} 不一致 "
                f"order={order_id} 期望={expected} 回调={actual}"
            )

    return True, "OK"


# ==================== 充值包配置 ====================

# 充值档位（2026-04-16 CTO-10 重构：赠送比例严格单调递增 + 降低 C 端门槛）
# [P0-13 2026-07-12] 删除死代码 RECHARGE_PACKAGES 硬编码字面量。
#   充值档 SSOT = config.pricing_config.get_recharge_packages()(已在下方 437/732 使用),
#   此处旧字面量全仓 0 引用(grep 证)· 保留会成第二真源 · 删除零行为变化。

# 自定义金额参数
CUSTOM_AMOUNT_MIN_YUAN = 1      # 最低 ¥1（虎皮椒 + 微信均无下限，¥0.01 太碎）
CUSTOM_AMOUNT_MAX_YUAN = 10000  # 风控上限
# [P0-13] 汇率 SSOT = config.pricing_config.get_points_per_yuan()(默认 130)· 不再本地 130 二源


def _calc_bonus_ratio(amount_yuan: float) -> float:
    """自定义金额按梯度计算赠送比例（和固定档位保持一致）
    [D2 动态化] 委托 pricing_config.calc_recharge_bonus_ratio(默认阶梯 10/15/20/25/30% 一致·后台可调/可关)"""
    from config.pricing_config import calc_recharge_bonus_ratio
    return calc_recharge_bonus_ratio(amount_yuan)


# ==================== 请求模型 ====================

class RechargeRequest(BaseModel):
    """充值请求 — package_id 和 custom_amount_yuan 二选一"""
    package_id: Optional[str] = None                 # 固定档位 id
    custom_amount_yuan: Optional[float] = None       # 自定义金额（元，范围 30-10000）
    payment_method: str = "wechat"                   # wechat / alipay
    # 2026-04-25 (CTO-15.9 wxpay 切微信直连) · 渠道选择
    # auto = UA 自动判断(微信内 jsapi · 手机非微信 xunhupay · PC native)
    # wechat_jsapi / wechat_native / xunhupay = 显式; wechat_h5 已关闭
    channel: str = "auto"
    sku_template_id: Optional[int] = None
    # [1:N 白标包 2026-06-05] 客户买代理某个具体白标算力包时传 override_id
    # 传则按 override_id + 绑定代理双重校验取价(retail 从 override · points 从 template)
    # 不传则走老 sku_template_id 逻辑(完全不变 · 向后兼容)
    override_id: Optional[int] = None
    # Legacy compatibility requests bind what the active page displayed. Older
    # clients may omit these fields, but provenance is never accepted as identity.
    retail_sku_id: Optional[str] = Field(None, min_length=8, max_length=64)
    retail_sku_version: Optional[int] = Field(None, ge=1)
    # [双价目表 SSOT 切流 2026-07-12] 后端报价引用 · PRICING_QUOTE_REQUIRED 开启后必传
    #   传入则订单金额/算力以 price_quote 为准(后端 SSOT),前端金额仅作校验;报价一单一用原子消费。
    price_quote_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    terms_acceptance_id: Optional[str] = Field(None, min_length=10, max_length=80)


class PaymentCallbackRequest(BaseModel):
    order_id: str
    payment_id: str


def _lock_legacy_template_sku(cur, *, agent_user_id: int, sku_template_id: int) -> dict:
    """Resolve one historical template identity without consulting provenance.

    A single locked SELECT replaces the former COUNT→SELECT race. New canonical
    rows have ``sku_template_id=NULL`` and therefore can never take over an old
    template link merely because they share ``source_template_id``.
    """
    cur.execute(
        """
        SELECT o.id AS override_id, o.retail_sku_id,
               o.version AS retail_sku_version,
               o.sku_template_id, o.source_template_id, o.points_granted,
               o.retail_cents AS override_retail,
               o.is_active AS override_active,
               COALESCE(t.template_code, o.retail_sku_id) AS template_code,
               COALESCE(t.sku_type, 'credit_pack') AS sku_type
          FROM agent_sku_overrides o
          LEFT JOIN sku_templates t ON t.id=o.sku_template_id
         WHERE o.agent_user_id=%s AND o.sku_template_id=%s
           AND o.is_active=TRUE AND o.deleted_at IS NULL
         ORDER BY o.id
         LIMIT 2
         FOR UPDATE OF o
        """,
        (int(agent_user_id), int(sku_template_id)),
    )
    rows = cur.fetchall()
    if len(rows) >= 2:
        raise HTTPException(
            400,
            detail={
                "code": "OVERRIDE_REQUIRED",
                "message": "该规格有多个算力包,请重新选择具体算力包",
            },
        )
    if not rows:
        raise HTTPException(400, detail="此商品已下架")
    return dict(rows[0]) if isinstance(rows[0], dict) else {}


def _assert_legacy_retail_snapshot_payable(
    *, agent_user_id: int, points_granted: int, retail_cents: int
) -> int:
    """Return canonical cost or reject a legacy order that cannot settle safely."""
    from services.agent_pricing import calc_factory_cents, get_agent_wholesale_ratio

    points = int(points_granted)
    retail = int(retail_cents)
    if points <= 0 or retail <= 0:
        raise HTTPException(409, detail="当前账户价格配置暂不可用，请稍后重试")
    locked_cost = calc_factory_cents(points, *get_agent_wholesale_ratio(int(agent_user_id)))
    if locked_cost > retail:
        raise HTTPException(
            409,
            detail={
                "code": "LEGACY_SKU_QUOTE_REQUIRED",
                "message": "该算力包需要重新发布并报价后才能购买",
            },
        )
    return int(locked_cost)


_PUBLIC_WALLET_FIELDS = {
    "paid_points", "commission_points", "bonus_points", "frozen_points", "total",
    "total_with_frozen", "agent_level", "total_recharged", "deduction_preference",
    "charge_notify_level", "active_subscription_id", "active_plan_id",
    "subscription_expires_at", "subscription_auto_renew", "pending_clawback_points",
    "customer_credit_status", "tool_credit_total", "publish_credit_total",
    "bonus_points_total", "paid_points_total", "service_fee",
    # [客户线上购买门控 2026-07-29] 单一判定源:前端只消费这个布尔,不自己拼
    # "客户级 + 主账号"两级逻辑 —— 否则前后端判定必然漂移。
    # 只下发布尔,绝不下发 provider_user_id / reason(那会把服务商身份泄给客户)。
    "can_purchase_online",
}
_PUBLIC_CUSTOMER_CREDIT_FIELDS = {
    "tool_credit_points", "publish_credit_points", "bonus_credit_points",
    "total_purchased_points", "total_consumed_points",
}


def _serialize_public_wallet(balance: dict) -> dict:
    """Explicit customer/agent allowlist; settlement principals remain server-side."""
    public = {key: balance.get(key) for key in _PUBLIC_WALLET_FIELDS if key in balance}
    raw_credit = balance.get("customer_credit")
    public["customer_credit"] = (
        {key: raw_credit.get(key) for key in _PUBLIC_CUSTOMER_CREDIT_FIELDS}
        if isinstance(raw_credit, dict)
        else None
    )
    return public


def _persist_payment_intent_urls(
    order_id: str,
    *,
    payment_url_mobile: Optional[str] = None,
    payment_url_qrcode: Optional[str] = None,
    code_url: Optional[str] = None,
) -> None:
    """记下这张单的支付出口,好让用户回来时还找得到它(#169 · 迁移 058)。

    🔴 只写"这次真拿到的"那几个:三个参数全 None 时直接返回,不发一条把
       已有值抹成 NULL 的 UPDATE —— 降级路径会二次调用本函数。

    🔴 失败**不抛**:URL 是恢复用的便利,不是资金证据。为了存一个跳转链接
       而让一张已经可以支付的订单下单失败,是把小问题换成大问题
       (与 `_persist_actual_payment_channel` 相反 —— 那个是退款依据)。
    """
    from db.connection import get_connection

    sets, vals = [], []
    for col, val in (
        ("payment_url_mobile", payment_url_mobile),
        ("payment_url_qrcode", payment_url_qrcode),
        ("code_url", code_url),
    ):
        if val:
            sets.append(col + "=%s")
            vals.append(str(val))
    if not sets:
        return

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE recharge_orders SET " + ", ".join(sets)
            + " WHERE id=%s AND payment_status='pending'",
            (*vals, str(order_id)),
        )
        conn.commit()
    except Exception as exc:            # noqa: BLE001 见 docstring:不抛
        if conn is not None:
            try:
                conn.rollback()
            except Exception:           # noqa: BLE001
                pass
        logger.warning("[#169] 支付出口 URL 未能留痕(不阻断下单)· order=%s: %s",
                       order_id, exc)
    finally:
        if conn is not None:
            conn.close()


def _load_payment_intent_urls(order_id: str) -> dict:
    """取回这张单建单时存下的支付出口(#169)。

    读不到 / 出错一律返回三个 None —— 与迁移 058 之前建的老单同形。
    **不编造**:宁可让前端说"这张单没有出口了",也不能返回一个不属于它的链接。
    """
    empty = {"payment_url_mobile": None, "payment_url_qrcode": None, "code_url": None}
    from db.connection import get_connection

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT payment_url_mobile, payment_url_qrcode, code_url
               FROM recharge_orders WHERE id=%s""",
            (str(order_id),),
        )
        row = cur.fetchone()
        if not row:
            return empty
        return {k: (row[k] or None) for k in empty}
    except Exception as exc:            # noqa: BLE001
        logger.warning("[#169] 读支付出口失败(按无出口处理)· order=%s: %s", order_id, exc)
        return empty
    finally:
        if conn is not None:
            conn.close()


def _persist_actual_payment_channel(order_id: str, channel: str) -> None:
    """Persist the refund route before crossing the external provider boundary."""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE recharge_orders SET actual_payment_channel=%s
               WHERE id=%s AND payment_status='pending'""",
            (str(channel), str(order_id)),
        )
        if cur.rowcount != 1:
            raise RuntimeError("充值订单实际支付通道未能持久化")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==================== 端点 ====================

@router.get("")
async def get_balance(request: Request):
    """获取当前用户余额

    V3.3.1(2026-05-12):总开关启用 + L2 时返 service_fee 概要(L1/L0 兼容不变)
    """
    user = request.state.user
    user_id = user["user_id"]

    # V3.3.1 hook · 启用时走扩展查询(加 service_fee 5 段统计 + L1/L2 隔离)
    balance = None
    try:
        from config.v3_3_1_flags import is_v3_3_1_enabled
        if is_v3_3_1_enabled():
            from db.wallet_db_v3_3_1 import get_wallet_balance_with_service_fee
            balance = get_wallet_balance_with_service_fee(user_id)
    except Exception as exc:
        # V3.3.1 扩展失败 · 退化老 wallet_balance · 不阻塞钱包页
        logger.warning("[V3.3.1] get_wallet_balance_with_service_fee failed · fallback: %s", exc)

    if balance is None:
        balance = get_wallet_balance(user_id)
    if balance.get("customer_credit_status") != "ready":
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CUSTOMER_CREDIT_UNAVAILABLE",
                "message": "当前功能可用额度暂时无法确认，请稍后重试；这不代表额度为 0",
            },
        )
    # [客户线上购买门控 2026-07-29] 可见性判定由后端下发最终结果,前端只消费。
    # 判定层自身 fail-open,读不到时是 True(不拦),与后端守卫同源同结论。
    from services.client_purchase_gate import can_purchase_online as _can_purchase_online
    balance = dict(balance)
    balance["can_purchase_online"] = _can_purchase_online(user_id)
    return {"success": True, "data": _serialize_public_wallet(balance)}


class PreferenceUpdateRequest(BaseModel):
    """扣费偏好切换请求（仅代理可用）"""
    deduction_preference: str  # 'default' | 'agent_friendly'


# 全站扣费提醒偏好合法值（P5a · 静默扣费 + 扣完通知 + 用户可配提醒强度）
CHARGE_NOTIFY_LEVELS = ("each", "quiet", "off")


class ChargeNotifyLevelRequest(BaseModel):
    """全站扣费提醒强度切换请求（所有用户可用 · 不限代理）

    - 'each'  : 每次扣费都提醒
    - 'quiet' : 安静一点（弱提醒）
    - 'off'   : 不主动提醒
    """
    level: str


class AutoMonitorPreferenceRequest(BaseModel):
    """A.9-B(CTO-15.9 session 3 · 2026-04-25 · 老板拍板)
    自动 monitor 24h after publish 开关 · 默认关
    · [WO_222-c1b] 原"仅代理可切",Owner 2026-09-15 直令放开,任何登录账号都可切
    """
    auto_monitor_after_publish: bool


@router.get("/auto-monitor-preference")
async def get_auto_monitor_preference(request: Request):
    """A.9-B · 查代理"自动 monitor 24h"开关状态"""
    user = request.state.user
    user_id = user["user_id"]
    wallet = get_wallet_balance(user_id)
    return {
        "success": True,
        "data": {
            "enabled": bool(wallet.get("auto_monitor_after_publish") or False),
            "agent_level": wallet.get("agent_level", 0),
        },
    }


@router.patch("/auto-monitor-preference")
async def update_auto_monitor_preference(req: AutoMonitorPreferenceRequest, request: Request):
    """A.9-B · 切换"自动 monitor 24h"开关 · 任何登录账号都可切

    开关开启后 · 本账号名下任一品牌文章首发 24h 后自动跑 1 次监测
    扣费 ¥0.29(charge_on_success · 完成才扣)
    元指令 11 遵守:明示同意才自动扣费 —— 这个开关本身就是那份明示同意。

    [WO_222-c1b] 原来卡 `agent_level >= 1`(403「仅服务方可切换自动监测偏好」)。
    监测在 Owner 2026-09-15 直令的**放开**清单里;扣的是本账号自己钱包里的算力、
    按同一份功能目录计价,不涉及任何身份语义 ⇒ 撤闸。连带撤掉那次
    `get_wallet_balance` —— 它在本函数里唯一的用途就是这道闸。
    """
    user = request.state.user
    user_id = user["user_id"]

    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_wallets
            SET auto_monitor_after_publish = %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (req.auto_monitor_after_publish, user_id))
        conn.commit()

        # 写 audit_log
        try:
            from db.auth_db import create_audit_log
            create_audit_log(
                user_id=user_id,
                username=user.get("username") or user.get("nickname"),
                action="auto_monitor_pref_toggle",
                module="wallet",
                summary=f"代理切换自动 monitor 24h: {'ON' if req.auto_monitor_after_publish else 'OFF'}",
                after={"enabled": req.auto_monitor_after_publish},
            )
        except Exception:
            pass

        return {"success": True, "data": {"auto_monitor_after_publish": req.auto_monitor_after_publish}}
    finally:
        conn.close()


@router.patch("/preference")
async def update_deduction_preference(req: PreferenceUpdateRequest, request: Request):
    """更新扣费偏好（仅 agent_level>=1 可切换）

    - 'default'（平台友好）: bonus → commission → paid
    - 'agent_friendly'（代理友好）: bonus → paid → commission
    """
    user = request.state.user
    user_id = user["user_id"]

    # 校验取值
    if req.deduction_preference not in ('default', 'agent_friendly'):
        raise HTTPException(status_code=400, detail="deduction_preference 只接受 default 或 agent_friendly")

    # 校验权限：仅代理可切
    wallet = get_wallet_balance(user_id)
    if wallet.get("agent_level", 0) < 1 and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅服务方可切换扣费偏好")

    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_wallets
            SET deduction_preference = %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (req.deduction_preference, user_id))
        conn.commit()
        return {"success": True, "data": {"deduction_preference": req.deduction_preference}}
    finally:
        conn.close()


# ==================== 全站扣费提醒偏好（P5a · 2026-06-03）====================
# 老板拍板：全站「前置确认扣费」→「静默扣费 + 扣完通知 + 用户可配提醒强度」
# 本组端点只做偏好的读 / 写存储 · 不改任何扣费逻辑（billing.py 不动）
# 与 deduction_preference（仅代理）不同 · charge_notify_level 面向所有用户

@router.get("/charge-notify-preference")
async def get_charge_notify_preference(request: Request):
    """查当前登录用户的扣费提醒强度

    level 为 None 表示用户尚未设置（前端首次引导用 · 区分"默认 quiet"与"未表态"）
    """
    user = request.state.user
    user_id = user["user_id"]
    wallet = get_wallet_balance(user_id)
    return {
        "success": True,
        "data": {
            # NULL = 未设置 · 不强制兜默认值（保留首次引导语义）
            "charge_notify_level": wallet.get("charge_notify_level"),
        },
    }


@router.patch("/charge-notify-preference")
async def update_charge_notify_preference(req: ChargeNotifyLevelRequest, request: Request):
    """切换当前登录用户的扣费提醒强度（所有用户可用）

    入参 level ∈ {'each','quiet','off'} · 非法值返 400
    仅写偏好 · 不触发任何扣费 / 退费
    """
    user = request.state.user
    user_id = user["user_id"]

    # 校验取值（非法返 400）
    if req.level not in CHARGE_NOTIFY_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"level 只接受 {'/'.join(CHARGE_NOTIFY_LEVELS)}",
        )

    # 确保钱包行存在（新用户首次设置时 UPDATE 不至于 0 行）· 复用现有 helper
    get_or_create_wallet(user_id)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_wallets
            SET charge_notify_level = %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
        """, (req.level, user_id))
        conn.commit()
        return {"success": True, "data": {"charge_notify_level": req.level}}
    finally:
        conn.close()


@router.get("/transactions")
async def list_transactions(request: Request, type: str = None, page: int = 1, limit: int = 20, offset: int = 0):
    """获取积分流水
    [P1-9 fix 2026-05-23] Codex 跨 AI 审计 · committed 的 freeze 流水标 freeze_status='committed' 前端可隐藏防双扣视觉"""
    user = request.state.user
    real_limit = min(limit, 100)
    real_offset = (page - 1) * real_limit if page > 1 else offset
    txs = get_transactions(
        user["user_id"],
        tx_type=type,
        limit=real_limit,
        offset=real_offset,
        # [BUG-2 2026-07-27] 客户也要看到这条 —— 但经 _decorate_wallet_transaction_for_display
        # 翻成"转入可用额度"的人话。原先整条隐藏导致客户对不上账(见该函数注释)。
        include_internal_migrations=True,
    )
    # 映射 feature_code → feature_name(用 description 字段,更可读)
    for tx in txs:
        _decorate_wallet_transaction_for_display(tx)
        if tx.get("created_at") and hasattr(tx["created_at"], "isoformat"):
            tx["created_at"] = tx["created_at"].isoformat()

    # P1-9: 为每条 freeze 流水反查 point_freezes.status · 给前端标记
    # 已 committed 的 freeze 流水跟同 feature/同 amount 的 consume 流水成对出现 · 前端可隐藏 freeze 防双扣视觉
    try:
        from db.connection import get_connection
        freeze_ids = [tx.get("order_id") for tx in txs if tx.get("type") == "freeze" and tx.get("order_id")]
        if freeze_ids:
            # order_id 形如 "FRZ82-OR20260523-b4h5gr" · 反向解析 freeze id
            import re
            freeze_id_map = {}
            for oid in freeze_ids:
                m = re.match(r"^FRZ(\d+)-", oid or "")
                if m:
                    freeze_id_map[oid] = int(m.group(1))
            if freeze_id_map:
                conn = get_connection()
                try:
                    c = conn.cursor()
                    ids = list(set(freeze_id_map.values()))
                    placeholders = ",".join(["%s"] * len(ids))
                    c.execute(f"SELECT id, status FROM point_freezes WHERE id IN ({placeholders})", tuple(ids))
                    status_by_id = {row["id"]: row["status"] for row in c.fetchall()}
                finally:
                    conn.close()
                for tx in txs:
                    if tx.get("type") == "freeze":
                        fid = freeze_id_map.get(tx.get("order_id"))
                        if fid:
                            tx["freeze_status"] = status_by_id.get(fid, "frozen")
    except Exception as e:
        # 防御性 · 不阻塞主流水返回
        import logging
        logging.getLogger("GEO-Wallet").warning(f"[P1-9] freeze status 反查失败(非阻塞): {e}")
    total = count_transactions(
        user["user_id"],
        tx_type=type,
        # [BUG-2] 计数口径必须与列表一致,否则分页总数对不上
        include_internal_migrations=True,
    )
    return {"items": txs, "total": total, "page": page, "limit": real_limit}


@router.get("/packages")
async def list_packages():
    """获取充值包列表"""
    # [D2 动态化] 充值档位从 pricing_config 读(默认 6 档·后台可调)
    from config.pricing_config import get_recharge_packages
    return {"success": True, "data": get_recharge_packages()}


PUBLIC_PRICING_FIELDS = {
    "feature_code",
    "feature_name",
    "cost_points",
    "cost_compute",
    "requires_paid_points",
    "is_active",
}


def _request_is_admin(request: Request) -> bool:
    user = getattr(request.state, "user", None) or {}
    return bool(user.get("is_admin"))


def serialize_pricing_for_user(pricing: list[dict], *, is_admin: bool) -> list[dict]:
    """Return full pricing to admins and customer-safe fields to everyone else."""
    if is_admin:
        return [dict(row) for row in pricing]
    safe_rows: list[dict] = []
    for row in pricing:
        item = dict(row)
        safe_rows.append({field: item.get(field) for field in PUBLIC_PRICING_FIELDS if field in item})
    return safe_rows


@router.get("/pricing")
async def list_pricing(request: Request):
    """获取全部功能定价"""
    pricing = get_all_pricing()
    return {
        "success": True,
        "data": serialize_pricing_for_user(pricing, is_admin=_request_is_admin(request)),
    }


def _detect_channel(user_agent: str) -> str:
    """2026-04-25 第 4 版(Deploy-CTO 切手机走 xunhupay · 老板知情拍板)

    实证产品权限(2026-04-25):
      ✅ JSAPI 支付   · 已开通 · 微信内付款层
      ✅ Native 支付  · 已开通 · 二维码 weixin://wxpay/...(PC 扫)
      ❌ H5 支付     · 申请被驳回(SaaS 类目微信不批)
      ⚠️  xunhupay   · 聚合支付 · 资金进私账 · 老板手动平账

    🚨 微信新策略(2026-04-25 实测):
      "为保障支付安全,暂不支持通过从相册识别二维码完成支付"
      → 手机非微信用户"长按保存→切微信→扫一扫→相册→选图"路径被微信主动阻断
      → wechat_native 在手机非微信场景下完全死路

    决策权衡(老板拍板):
      公账 + 用户 0 转化 vs 私账 + 用户能付款
      → 选后者 · 用户能付款 > 私账平账成本(月手续费 ~ ¥150/万 GMV)

    新分流策略:
      - 手机微信内 → wechat_jsapi(0 手续费 · 公账)
      - 桌面微信内 → wechat_native(2026-08-07 新增 · 桌面 WeixinJSBridge 拉不起付款层)
      - PC → wechat_native(0 手续费 · 公账 · 用户用手机微信扫桌面二维码)
      - 手机非微信 → xunhupay(1.5% · 私账 · 但用户能 1 点直跳支付)
      - 直连失败 → xunhupay 兜底

    判据全在 services/payment_routing.detect_payment_channel(唯一实现 · 本函数只是转发)。
    """
    return detect_payment_channel(user_agent)


@router.post("/recharge")
async def create_recharge(req: RechargeRequest, request: Request):
    """创建充值订单 + 调微信支付(2026-04-25 切直连 · 虎皮椒兜底)

    两种模式:
    1. 固定档位:传 package_id(starter/popular/pro/flagship/premium)
    2. 自定义金额:传 custom_amount_yuan(¥30-¥10000,按梯度赠送)

    渠道选择(channel 参数):
    - 'auto' (默认) · 后端 UA 自动判断:微信内→jsapi · 手机外部→xunhupay · PC→native
    - 'wechat_jsapi' / 'wechat_native' · 显式微信直连
    - 'xunhupay' · 显式走虎皮椒兜底(老路径)
    PC Native 失败 · 自动 fallback xunhupay(若 xunhupay 也失败 · 返 fallback_failed)
    """
    user = request.state.user

    # [客户线上购买门控 2026-07-29] 服务商可关掉名下客户的线上直购(玩法 B 线下收费)。
    # 必须拦在这里而不是只拦前端 —— 前端拦得住、API 绕得过就不算真拦。
    # 判定单点在 services/client_purchase_gate.py;放行态零行为变化。
    from services.client_purchase_gate import require_online_purchase_allowed
    require_online_purchase_allowed(user["user_id"])

    # ============================================================
    # [V3.5 W3 资金链 SSOT] 价格 / 积分 / 路由分两路径(boss r5 P0-3 修正):
    #
    # Path SKU(V3.5)· req.sku_template_id 存在:
    #   - amount_cents 必须来自 SKU snapshot retail_cents(忽略 custom_amount_yuan / package_id)
    #   - 客户端传 amount 与 SKU 不符 → 静默用 SKU(防 ¥30 买高额度 SKU 攻击)
    #   - agent_user_id 由当前客户绑定推导(若 client 传 agent · 必须与绑定一致)
    #   - 代理必须已签 v2.1
    #
    # Path Legacy(直营 + 老充值)· 无 sku_template_id:
    #   - 走原 package_id / custom_amount_yuan 解析
    # ============================================================
    v35_order_type = None
    v35_pricing_snapshot = None
    tier_label = ""
    bound_agent_id_for_v35 = None
    # [1:N 白标包 2026-06-05] 解析出的 sku_template_id(override 路径从 join 推导 · 否则 = req.sku_template_id)
    _resolved_sku_template_id = req.sku_template_id
    # 实际锁价命中的 override。旧 sku_template_id 链若唯一命中白标包，也必须
    # 写进订单和快照，供审计及退款使用。
    _resolved_override_id = req.override_id
    _quote_id_for_order = None
    _quote_type_for_order = None
    _catalog_version_for_order = None
    _expected_product_code = None
    # Commercial routing is server-derived. Public request hints never select the
    # settlement principal or create a relationship.
    _binding_source_for_order = None
    _dual_enabled, _require_quote = _pricing_quote_flags()
    from config.dealer_inventory_resale_flags import enabled as _dealer_resale_enabled
    _resale_enabled = _dealer_resale_enabled()
    if _require_quote and not _dual_enabled:
        raise HTTPException(
            503,
            detail={
                "code": "PRICING_FLAG_STATE_INVALID",
                "message": "报价强制闸已开启但双价目表未开启 · 暂停创建订单",
            },
        )
    if _require_quote and not req.price_quote_id:
        raise HTTPException(422, detail={"code": "QUOTE_REQUIRED", "message": "请先获取价格报价再下单"})
    if _resale_enabled and not _dual_enabled:
        raise HTTPException(
            503,
            detail={
                "code": "DEALER_RESALE_PRICING_PREREQUISITE_MISSING",
                "message": "逐级库存转售已开但持久化报价目录未开 · 暂停创建订单",
            },
        )
    if _resale_enabled and not req.price_quote_id:
        raise HTTPException(
            422,
            detail={"code": "DEALER_RESALE_QUOTE_REQUIRED", "message": "当前购买必须先获取平台服务报价"},
        )
    _use_retail_quote = bool((_dual_enabled or _resale_enabled) and req.price_quote_id)

    # override_id 或 sku_template_id 任一存在 → 走 SKU 资金路径
    if _use_retail_quote:
        # Quoted retail orders derive every monetary/source field from the persisted
        # quote. Client SKU/amount fields are ignored and never become a second truth.
        from db.connection import get_db as _get_db
        from services import price_quote as _pq_mod
        from services.agent_agreement import is_agent_signed, CURRENT_VERSION
        from services.commercial_service_routing import (
            RelationshipError,
            RelationshipResolution,
            public_relationship_http_error,
            resolve_commercial_relationship,
        )
        from services.settlement_orchestrator import is_v35_factory_enabled
        try:
            with _get_db() as _c:
                _cur = _c.cursor()
                _q = _pq_mod.lock_and_validate(
                    _cur,
                    str(req.price_quote_id),
                    buyer_user_id=int(user["user_id"]),
                    quote_type="retail",
                )
                _source_ref = _pq_mod.quote_source_ref(_q)
                _order_snapshot = _pq_mod.quote_order_pricing_snapshot(_q, required=True)
                _resale_now = _dealer_resale_enabled(_cur)
                if _resale_now and _order_snapshot.get("consumer_resale_mode") is not True:
                    raise _pq_mod.QuoteError(
                        "逐级库存转售已开启，旧零售报价不能创建新订单 · 请重新报价"
                    )
                if not _resale_now and _order_snapshot.get("consumer_resale_mode") is True:
                    raise _pq_mod.QuoteError(
                        "逐级库存转售已关闭，未下单的旧转售报价已作废 · 请重新报价"
                    )
                if _source_ref.get("source_kind") == "retail_custom_amount":
                    required_source = (
                        "agent_user_id", "wholesale_cents", "retail_cents",
                        "points_granted", "cash_anchor_fingerprint",
                        "effective_cost_version", "markup_version",
                    )
                else:
                    required_source = (
                        "agent_user_id", "retail_sku_id", "retail_sku_version", "override_id",
                        "wholesale_cents", "retail_cents", "points_granted",
                    )
                missing_source = [key for key in required_source if _source_ref.get(key) is None]
                if missing_source:
                    raise _pq_mod.QuoteError(
                        f"零售报价来源快照缺字段: {','.join(missing_source)}"
                )
                bound_agent_id_for_v35 = int(_source_ref["agent_user_id"])
                try:
                    _relationship = resolve_commercial_relationship(_cur, int(user["user_id"]))
                except RelationshipError as exc:
                    status, detail = public_relationship_http_error(exc)
                    raise HTTPException(status, detail=detail) from exc
                if _relationship.service_user_id != bound_agent_id_for_v35:
                    raise _pq_mod.QuoteError("retail quote relationship changed")
                _route_source = (
                    "explicit_binding"
                    if _relationship.resolution is RelationshipResolution.BOUND
                    else "platform_direct"
                )
                _quoted_route_source = _order_snapshot.get("commercial_service_source")
                if (
                    _quoted_route_source not in ("explicit_binding", "platform_direct")
                    or _quoted_route_source != _route_source
                ):
                    raise _pq_mod.QuoteError("retail quote relationship changed")
                _binding_source_for_order = str(_quoted_route_source)
                if not is_v35_factory_enabled(_cur):
                    raise HTTPException(
                        503,
                        detail={
                            "code": "V35_FACTORY_DISABLED",
                            "message": "服务暂未开启 · 额度包购买暂不可用 · 请联系平台",
                        },
                    )
                if (
                    _relationship.resolution is RelationshipResolution.BOUND
                    and not is_agent_signed(_cur, bound_agent_id_for_v35, CURRENT_VERSION)
                ):
                    raise HTTPException(
                        403,
                        detail={
                            "code": "ACCOUNT_CONFIGURATION_UNAVAILABLE",
                            "message": "当前账户配置暂不可用，请稍后重试",
                        },
                    )
                if int(_order_snapshot.get("points_granted", -1)) != int(_q["points_granted"]):
                    raise _pq_mod.QuoteError("零售订单快照算力与报价不一致")
                if int(_order_snapshot.get("bonus_points", -1)) != int(_q["bonus_points"]):
                    raise _pq_mod.QuoteError("零售订单快照奖励与报价不一致")
        except HTTPException:
            raise
        except _pq_mod.QuoteError as exc:
            logger.warning("retail quote validation failed type=%s", type(exc).__name__)
            raise HTTPException(
                422,
                detail={"code": "QUOTE_INVALID", "message": "报价已失效，请刷新后重试"},
            ) from exc

        amount_cents = int(_q["final_price_cents"])
        amount_yuan = Decimal(amount_cents) / Decimal(100)
        base_points = int(_q["points_granted"])
        bonus_points = int(_q["bonus_points"])
        tier_label = str(_q["product_code"])
        _resolved_sku_template_id = (
            int(_source_ref["sku_template_id"])
            if _source_ref.get("sku_template_id") is not None else None
        )
        _resolved_override_id = (
            int(_source_ref["override_id"])
            if _source_ref.get("override_id") is not None else None
        )
        v35_pricing_snapshot = _order_snapshot
        v35_pricing_snapshot["commercial_resolution"] = _relationship.resolution.value
        v35_order_type = "customer_recharge"
        _quote_id_for_order = str(req.price_quote_id)
        _quote_type_for_order = "retail"
        _catalog_version_for_order = str(_q["catalog_version"])
        _expected_product_code = str(_q["product_code"])
        if (
            req.override_id or req.sku_template_id or req.retail_sku_id
            or req.retail_sku_version is not None
            or req.custom_amount_yuan is not None or req.package_id
        ):
            logger.info(
                "[recharge][retail-quote] user=%s 提交了兼容字段 · 已忽略并使用 quote=%s",
                user["user_id"], req.price_quote_id,
            )
    elif req.override_id or req.sku_template_id:
        # === Path SKU(V3.5 资金 SSOT)===
        from db.connection import get_db as _get_db
        from services.agent_agreement import is_agent_signed, CURRENT_VERSION
        from services.commercial_service_routing import (
            RelationshipError,
            RelationshipResolution,
            public_relationship_http_error,
            resolve_commercial_relationship,
        )
        from services.settlement_orchestrator import is_v35_factory_enabled

        with _get_db() as _c:
            _cur = _c.cursor()
            # 0. [boss r6 P0] V3.5 总开关闸 · false 时拒绝下单 · 防付款后落 direct/v32 错账
            # 资金链铁律:SKU 下单 → 回调 complete_recharge → Orchestrator 路由
            # 若 V35_FACTORY_INVENTORY_ENABLED=false → 走 direct/v32_legacy 不写客户额度 + 不写代理收益
            # → 客户付款但拿不到工具额度 + 代理拿不到结算 → 必须拒绝下单
            if not is_v35_factory_enabled(_cur):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "V35_FACTORY_DISABLED",
                        "message": "服务暂未开启 · 额度包购买暂不可用 · 请联系平台",
                    },
                )

            try:
                _relationship = resolve_commercial_relationship(_cur, int(user["user_id"]))
            except RelationshipError as exc:
                status, detail = public_relationship_http_error(exc)
                raise HTTPException(status, detail=detail) from exc
            bound_agent_id_for_v35 = _relationship.service_user_id
            _binding_source_for_order = (
                "explicit_binding"
                if _relationship.resolution is RelationshipResolution.BOUND
                else "platform_direct"
            )

            # 2. 代理必须已签 v2.1
            if (
                _relationship.resolution is RelationshipResolution.BOUND
                and not is_agent_signed(_cur, bound_agent_id_for_v35, CURRENT_VERSION)
            ):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "ACCOUNT_CONFIGURATION_UNAVAILABLE",
                        "message": "当前账户配置暂不可用，请稍后重试",
                    },
                )

            # 3. SKU 快照取价(SSOT)
            # override_id 存在 → 按 canonical 零售行 + 直属服务方双重校验。
            # 旧 sku_template_id 链只能唯一命中显式零售行，永不再回落平台模板售价。
            if req.override_id:
                _cur.execute("""
                    SELECT o.id AS override_id, o.retail_cents AS override_retail,
                           o.is_active AS override_active,
                           o.retail_sku_id, o.version AS retail_sku_version,
                           o.sku_template_id, o.source_template_id, o.points_granted,
                           COALESCE(t.template_code, o.retail_sku_id) AS template_code,
                           COALESCE(t.sku_type, 'credit_pack') AS sku_type
                    FROM agent_sku_overrides o
                    LEFT JOIN sku_templates t ON t.id = o.source_template_id
                    WHERE o.id = %s AND o.agent_user_id = %s
                      AND o.is_active = TRUE AND o.deleted_at IS NULL
                """, (req.override_id, bound_agent_id_for_v35))
                _sku = _cur.fetchone()
                if not _sku:
                    raise HTTPException(404, detail="SKU 不存在或已下架")
                _sku_d = dict(_sku) if isinstance(_sku, dict) else {}
                if _sku_d.get("override_active") is False:
                    raise HTTPException(400, detail="此商品已下架")
                _retail = _sku_d.get("override_retail") or 0
                if not _retail or int(_retail) <= 0:
                    raise HTTPException(409, detail="当前账户价格配置暂不可用，请稍后重试")
                _resolved_sku_template_id = _sku_d.get("sku_template_id")
                _resolved_override_id = int(_sku_d["override_id"])
            else:
                _sku_d = _lock_legacy_template_sku(
                    _cur,
                    agent_user_id=int(bound_agent_id_for_v35),
                    sku_template_id=int(req.sku_template_id),
                )
                _retail = _sku_d.get("override_retail") or 0
                if not _retail or int(_retail) <= 0:
                    raise HTTPException(409, detail="当前账户价格配置暂不可用，请稍后重试")
                _resolved_sku_template_id = _sku_d.get("sku_template_id")
                _resolved_override_id = int(_sku_d["override_id"])

            # 4. 金额 / 积分 强制从 SKU 取(不接受 client custom_amount / package_id)
            amount_cents = int(_retail)
            amount_yuan = Decimal(amount_cents) / Decimal(100)
            base_points = int(_sku_d.get("points_granted", 0) or 0)
            bonus_points = 0
            tier_label = _sku_d.get("retail_sku_id", "v35_sku")

            if req.retail_sku_id and str(req.retail_sku_id) != str(_sku_d.get("retail_sku_id")):
                raise HTTPException(409, detail={"code": "SKU_CHANGED", "message": "算力包已变化，请刷新后重试"})
            if req.retail_sku_version and int(req.retail_sku_version) != int(_sku_d.get("retail_sku_version") or 0):
                raise HTTPException(409, detail={"code": "SKU_CHANGED", "message": "算力包已变化，请刷新后重试"})

            # [BUG-P3] 锁 per-agent 出厂折扣到 snapshot:SKU 模板 wholesale_cents 是【全局】出厂价,
            # 若该服务商有 per-agent 出厂折扣 override(agent_pricing_overrides),结算 factory_cents
            # 用全局会架空 override → margin 算偏。有 override 才按 per-agent 重算锁价;无 override
            # 用模板全局值(行为不变·零回归)。下单时锁,settlement 直接读(老订单不重算铁律)。
            try:
                _locked_wholesale_cents = _assert_legacy_retail_snapshot_payable(
                    agent_user_id=int(bound_agent_id_for_v35),
                    points_granted=base_points,
                    retail_cents=amount_cents,
                )
            except HTTPException:
                raise
            except Exception as _wexc:
                logger.error("[Wallet/V3.5] canonical 零售成本锁价失败", exc_info=True)
                raise HTTPException(503, detail="当前账户价格配置暂不可用，请稍后重试") from _wexc

            v35_pricing_snapshot = {
                "retail_snapshot_version": 2,
                "retail_sku_id": _sku_d.get("retail_sku_id"),
                "retail_sku_version": int(_sku_d.get("retail_sku_version") or 0),
                "sku_template_id": _resolved_sku_template_id,
                "source_template_id": _sku_d.get("source_template_id"),
                "override_id": _resolved_override_id,  # 1:N 白标包审计 · 含旧链接唯一命中
                "sku_key": _sku_d.get("template_code"),
                "sku_type": _sku_d.get("sku_type"),
                "points_granted": base_points,
                "wholesale_cents": _locked_wholesale_cents,
                "retail_cents": amount_cents,
                "tool_points": base_points,
                "publish_points": 0,
                "bonus_points": 0,
                "customer_paid_cents": amount_cents,
                "amount_source": "sku_snapshot",  # 资金链审计 · 显式标 SKU SSOT
                "commercial_resolution": _relationship.resolution.value,
                "commercial_service_source": _binding_source_for_order,
            }
        v35_order_type = "customer_recharge"
        # client 若同时传了 custom_amount_yuan / package_id 静默忽略 + log warn
        if req.custom_amount_yuan is not None or req.package_id:
            logger.warning(
                f"[recharge][V3.5-SKU] user={user['user_id']} sku={req.sku_template_id} "
                f"client 同时传 custom_amount_yuan={req.custom_amount_yuan} / package_id={req.package_id} "
                f"· 已忽略 · 使用 SKU retail_cents={amount_cents}"
            )
    else:
        # === Path Legacy(直营 · 老充值)===
        if req.custom_amount_yuan is not None:
            amount_yuan = round(float(req.custom_amount_yuan), 2)
            if amount_yuan < CUSTOM_AMOUNT_MIN_YUAN:
                raise HTTPException(400, detail=f"最低充值金额 ¥{CUSTOM_AMOUNT_MIN_YUAN}")
            if amount_yuan > CUSTOM_AMOUNT_MAX_YUAN:
                raise HTTPException(400, detail=f"单笔最高 ¥{CUSTOM_AMOUNT_MAX_YUAN},大额请分多次")
            amount_cents = int(round(amount_yuan * 100))
            from config.pricing_config import get_points_per_yuan
            base_points = int(amount_yuan * get_points_per_yuan())  # [P0-13] SSOT 汇率(默认130)
            bonus_ratio = _calc_bonus_ratio(amount_yuan)
            bonus_points = int(base_points * bonus_ratio)
            tier_label = f"自定义 ¥{amount_yuan:g}"
        elif req.package_id:
            from config.pricing_config import get_recharge_packages
            pkg = next((p for p in get_recharge_packages() if p["id"] == req.package_id), None)
            if not pkg:
                raise HTTPException(400, detail="无效的充值包")
            if pkg["amount"] == 0:
                raise HTTPException(400, detail="体验包在注册时自动发放")
            amount_cents = pkg["amount"]
            amount_yuan = amount_cents / 100
            base_points = pkg["base"]
            bonus_points = pkg["bonus"]
            tier_label = pkg["label"]
        else:
            raise HTTPException(400, detail="请选择档位或输入自定义金额")
        v35_order_type = "customer_recharge_direct"

    payment_method = (req.payment_method or "wechat").lower()
    if payment_method != "wechat":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ALIPAY_NOT_AVAILABLE",
                "message": "支付宝暂未接通,请先使用微信支付",
            },
        )

    requested_channel = (req.channel or "auto").lower()
    user_agent = request.headers.get("User-Agent", "")
    from services.xunhupay import is_force_xunhupay
    try:
        chosen_channel = resolve_payment_channel(
            requested_channel,
            user_agent,
            force_xunhupay=is_force_xunhupay(),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "PAYMENT_CHANNEL_UNAVAILABLE", "message": str(e)},
        )

    if not req.terms_acceptance_id:
        raise HTTPException(
            status_code=422,
            detail={"code": "PURCHASE_TERMS_REQUIRED", "message": "请先阅读并确认当前用户服务协议"},
        )
    from services.legal_agreements import validate_purchase_acceptance
    from db.connection import get_db as _agreement_get_db
    try:
        with _agreement_get_db() as _agreement_conn:
            _agreement_cur = _agreement_conn.cursor()
            _terms_evidence = validate_purchase_acceptance(
                _agreement_cur,
                acceptance_id=req.terms_acceptance_id,
                user_id=int(user["user_id"]),
                expected_surface="customer-recharge",
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "PURCHASE_TERMS_INVALID", "message": str(exc)},
        ) from exc

    _locked_terms_id = (
        v35_pricing_snapshot.get("terms_acceptance_id")
        if isinstance(v35_pricing_snapshot, dict)
        else None
    )
    if _locked_terms_id is not None and _locked_terms_id != req.terms_acceptance_id:
        raise HTTPException(
            status_code=422,
            detail={"code": "PURCHASE_TERMS_MISMATCH", "message": "报价与购买协议确认不一致 · 请重新报价"},
        )
    v35_pricing_snapshot = dict(v35_pricing_snapshot or {})
    v35_pricing_snapshot.update({
        "terms_acceptance_id": _terms_evidence["acceptance_id"],
        "terms_version": _terms_evidence["agreement_version"],
        "terms_content_hash": _terms_evidence["content_hash"],
        "terms_accepted_at": _terms_evidence["accepted_at"].isoformat(),
        "terms_surface": _terms_evidence["surface"],
    })

    order_id = shortuuid.uuid()

    # Internal settlement principal is derived server-side and never accepted from the client.
    _order_create_kwargs = {
        "user_id": user["user_id"],
        "order_id": order_id,
        "amount_cents": amount_cents,
        "base_points": base_points,
        "bonus_points": bonus_points,
        "payment_method": payment_method,
        "order_type": v35_order_type,
        "agent_user_id": bound_agent_id_for_v35,
        "sku_template_id": _resolved_sku_template_id,
        "override_id": _resolved_override_id,
        "binding_source": _binding_source_for_order,
        "source_token": None,
        "pricing_snapshot": v35_pricing_snapshot,
        "price_quote_id": _quote_id_for_order,
        "pricing_catalog_version": _catalog_version_for_order,
        "idempotency_key": req.idempotency_key,
        "quote_type": _quote_type_for_order,
        "expected_product_code": _expected_product_code,
    }
    try:
        if v35_order_type == "customer_recharge" and not _quote_id_for_order:
            # Legacy SKU orders have no quote row for create_recharge_order() to
            # lock.  Hold the commercial-subject advisory lock across the actual
            # INSERT so an admin rebind either wins first (and this request must
            # retry) or observes the newly pinned pending order and fails CAS.
            from services.commercial_service_routing import (
                RelationshipError as _RelationshipFenceError,
                execute_under_commercial_relationship_fence,
            )

            try:
                order = execute_under_commercial_relationship_fence(
                    customer_user_id=int(user["user_id"]),
                    expected_service_user_id=int(bound_agent_id_for_v35),
                    expected_source=str(_binding_source_for_order),
                    writer=lambda: create_recharge_order(**_order_create_kwargs),
                )
            except _RelationshipFenceError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "COMMERCIAL_SERVICE_CHANGED",
                        "message": "账户服务配置已更新，请刷新后重试",
                    },
                ) from exc
        else:
            order = create_recharge_order(**_order_create_kwargs)
    except Exception as exc:
        from services.price_quote import QuoteError
        if isinstance(exc, QuoteError):
            logger.warning("recharge quote consumption failed type=%s", type(exc).__name__)
            raise HTTPException(
                422,
                detail={"code": "QUOTE_INVALID", "message": "报价已失效，请刷新后重试"},
            ) from exc
        if getattr(getattr(exc, "diag", None), "constraint_name", None) in {
            "ux_recharge_terms_acceptance_once",
            "ux_purchase_acceptance_global_once",
        }:
            raise HTTPException(
                409,
                detail={"code": "PURCHASE_TERMS_REPLAYED", "message": "本次购买确认已用于其他订单 · 请重新确认"},
            ) from exc
        raise

    payment_url_qrcode: Optional[str] = None  # PC 扫码二维码 URL(Native)
    payment_url_mobile: Optional[str] = None  # 手机跳转(H5 / 虎皮椒)
    code_url: Optional[str] = None             # 微信 Native 原生 weixin:// URL · 前端可自己渲二维码
    h5_url: Optional[str] = None               # 微信 H5 跳转 URL
    needs_openid: bool = False                 # JSAPI 标记 · 前端跳 oauth 拿 openid 后调 jsapi/create
    actual_channel: str = chosen_channel
    fallback_to: Optional[str] = None

    # The provider route is refund evidence. Persist the intended route before
    # any provider can create a payable intent, so a process crash cannot leave
    # an externally payable order without a durable original-route record.
    try:
        _persist_actual_payment_channel(str(order["id"]), actual_channel)
    except Exception as exc:
        from services.dealer_inventory_resale import cancel_any_reservation
        cancel_any_reservation(str(order["id"]), "payment_route_persist_failed")
        raise HTTPException(
            503, detail="支付通道证据未能持久化 · 未向支付机构下单，请重新下单",
        ) from exc

    # ===== 微信支付 3 渠道(直连优先 · xunhupay 兜底)=====
    if chosen_channel == "wechat_jsapi":
        # JSAPI:本端点不下单 · 标记 needs_openid=True · 前端跳 oauth 拿 openid 后再调
        # /wallet/wechat-jsapi/create 真正下单
        needs_openid = True
        # 实际下单通过 jsapi/create endpoint · 这里仅返 order_id + 提示
    elif chosen_channel == "wechat_native":
        try:
            from services.wechat_pay import create_native_order
            result = await create_native_order(
                out_trade_no=order_id,
                total_yuan=amount_yuan,
                description=f"OmniRank AI 积分充值 ¥{amount_yuan:g}",
            )
            code_url = result.get("code_url")
            if not code_url:
                raise RuntimeError("Native 下单未返 code_url")
        except Exception as e:
            logger.error(f"微信 Native 下单失败 · 降级 xunhupay: {e}")
            actual_channel = "xunhupay"
            fallback_to = "xunhupay"
    elif chosen_channel == "xunhupay":
        actual_channel = "xunhupay"

    # 显式 xunhupay 或直连失败降级
    if actual_channel == "xunhupay":
        try:
            _persist_actual_payment_channel(str(order["id"]), "xunhupay")
        except Exception as exc:
            from services.dealer_inventory_resale import cancel_any_reservation
            cancel_any_reservation(str(order["id"]), "payment_route_fallback_persist_failed")
            raise HTTPException(
                503, detail="备用支付通道证据未能持久化 · 未向支付机构下单，请重新下单",
            ) from exc
        try:
            from services.xunhupay import create_xunhupay_order
            title = f"OmniRank AI 积分充值 ¥{amount_yuan:g}"
            result = await create_xunhupay_order(
                order_id=order_id,
                amount_yuan=amount_yuan,
                title=title,
                attach=str(user["user_id"]),
            )
            payment_url_qrcode = result.get("url_qrcode")
            payment_url_mobile = result.get("url")
        except Exception as e:
            logger.error(f"虎皮椒下单也失败: {e}")
            from services.dealer_inventory_resale import cancel_any_reservation
            try:
                cancel_any_reservation(str(order["id"]), "payment_intent_failed")
            except Exception as release_exc:
                logger.critical(
                    "消费者支付失败后的库存预占释放失败 · order=%s: %s",
                    order["id"], release_exc,
                )
            return {
                "success": False,
                "data": {
                    "order_id": order["id"],
                    "actual_channel": "xunhupay",
                    "error": "fallback_failed",
                    "fallback_to": fallback_to,
                },
                "detail": "支付下单失败(直连 + 兜底都失败)· 请稍后重试或联系客服",
            }

    # #169 · 把这次拿到的支付出口存下来(迁移 058),好让用户跳出去付款后
    #        回来还能恢复同一张单。不阻断:存不下不影响这次已经能付的流程。
    _persist_payment_intent_urls(
        str(order["id"]),
        payment_url_mobile=payment_url_mobile,
        payment_url_qrcode=payment_url_qrcode,
        code_url=code_url,
    )

    # [Deploy-CTO 2026-04-25] 临时诊断 log · 排查"支付链接生成失败"前端报错
    logger.info(
        "recharge response: order=%s chosen=%s actual=%s code_url_len=%d h5_url=%s qrcode=%s mobile=%s fallback=%s",
        order["id"], chosen_channel, actual_channel,
        len(code_url) if code_url else 0,
        bool(h5_url), bool(payment_url_qrcode), bool(payment_url_mobile),
        fallback_to,
    )
    return {
        "success": True,
        "data": {
            "order_id": order["id"],
            "amount_yuan": amount_yuan,
            "base_points": base_points,
            "bonus_points": bonus_points,
            "total_points": base_points + bonus_points,
            "tier_label": tier_label,
            "payment_method": payment_method,
            "channel_requested": chosen_channel,         # 用户选/UA 选的
            "actual_channel": actual_channel,            # 实际生效的(可能 fallback)
            "fallback_to": fallback_to,                  # 不为 None 即直连失败兜底
            "needs_openid": needs_openid,                # JSAPI 必走 oauth 后再调 jsapi/create
            "code_url": code_url,                        # Native(weixin://wxpay/...)· 前端 QR 渲
            "h5_url": h5_url,                            # H5(直接 location.href 跳)
            "payment_url_qrcode": payment_url_qrcode,    # 兼容老前端 · xunhupay 二维码图
            "payment_url_mobile": payment_url_mobile,    # 兼容老前端 · xunhupay 跳转 / H5
            "status": "pending",
            "expire_minutes": 5,
        },
    }


# ==================== 微信 JSAPI 支付 ====================

class _WxJsapiCreateBody(BaseModel):
    """微信 JSAPI 下单 body"""
    order_id: str  # 已 create 的充值订单 id
    openid: str    # 服务号网页授权拿的 openid


@router.post("/wechat-jsapi/create")
async def wechat_jsapi_create(body: _WxJsapiCreateBody, request: Request):
    """微信 JSAPI 下单 · 返前端 wx.requestPayment 调起参数

    流程:
      1. 前端调 POST /wallet/recharge 已建 order(payment_method='wechat')
      2. 用户在公众号网页授权 → 拿 openid → 调本端点
      3. 本端点调 wechat_pay.create_jsapi_order → 拿 prepay_id
      4. 本端点调 build_jsapi_invoke_params → 拿调起参数
      5. 前端 wx.requestPayment(调起参数)
      6. 用户支付 → 微信回调 /wallet/wechat-callback
    """
    # [2026-06-06 紧急] 全切 xunhupay 期间 · jsapi 单独 endpoint 拦截 · 引导走主 recharge(xunhupay)
    from services.xunhupay import is_force_xunhupay
    if is_force_xunhupay():
        raise HTTPException(
            status_code=503,
            detail={"code": "WECHAT_TEMP_DISABLED",
                    "message": "微信支付临时维护中,请通过虎皮椒扫码支付",
                    "fallback_endpoint": "/api/wallet/recharge",
                    "fallback_channel": "xunhupay"},
        )
    user = request.state.user
    user_id = user.get("user_id")

    # [客户线上购买门控 2026-07-29] JSAPI 调起是第二段支付发起 · 同样拦,
    # 否则关开关前建的 pending 单还能被继续付掉。
    from services.client_purchase_gate import require_online_purchase_allowed
    require_online_purchase_allowed(user_id)

    order = get_recharge_order(body.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if int(order.get("user_id") or 0) != int(user_id):
        raise HTTPException(status_code=403, detail="无权操作此订单")
    if order.get("payment_status") == "paid":
        raise HTTPException(status_code=400, detail="订单已支付")
    if order.get("actual_payment_channel") != "wechat_jsapi":
        raise HTTPException(status_code=409, detail="订单原支付路径不是微信 JSAPI，请重新发起购买")

    amount_cents = int(order.get("amount_cents") or 0)
    if amount_cents <= 0:
        raise HTTPException(status_code=400, detail="订单金额异常")

    order_type = order.get("order_type") or "customer_recharge"
    amount_yuan = amount_cents / 100.0
    if order_type == "agent_inventory_purchase":
        description = f"OmniRank 代理预付进货 ¥{amount_yuan:g}"
        attach = f"agent_inventory_purchase:{user_id}"
    else:
        description = f"OmniRank AI 积分充值 ¥{amount_yuan:g}"
        attach = str(user_id)

    try:
        from services.wechat_pay import create_jsapi_order, build_jsapi_invoke_params
        prepay_resp = await create_jsapi_order(
            out_trade_no=body.order_id,
            total_yuan=amount_yuan,
            openid=body.openid,
            description=description,
            attach=attach,
        )
        invoke = build_jsapi_invoke_params(prepay_resp["prepay_id"])
        return {
            "success": True,
            "data": {
                "order_id": body.order_id,
                "prepay_id": prepay_resp["prepay_id"],
                "invoke_params": invoke,  # 前端直接传 wx.requestPayment
            },
        }
    except (RuntimeError, ValueError) as e:
        logger.error(f"JSAPI 下单失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/wechat-jsapi/oauth-url")
async def wechat_jsapi_oauth_url(redirect_uri: str, state: str = ""):
    """获取服务号网页授权 URL · 前端跳转拿 openid

    Args:
        redirect_uri: 授权后跳回(必须在公众号"网页授权域名"白名单)
        state: 业务侧透传(回调原样返)
    """
    try:
        from services.wechat_pay import build_oauth_authorize_url
        url = build_oauth_authorize_url(redirect_uri, state=state)
        return {"success": True, "data": {"oauth_url": url}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class _WxOAuthCallbackBody(BaseModel):
    code: str  # 微信授权后回调的 code


@router.post("/wechat-jsapi/exchange-openid")
async def wechat_jsapi_exchange_openid(body: _WxOAuthCallbackBody):
    """用授权 code 换 openid(服务号 snsapi_base)· 前端授权回调后调"""
    try:
        from services.wechat_pay import fetch_openid_by_code
        result = await fetch_openid_by_code(body.code)
        return {
            "success": True,
            "data": {
                "openid": result.get("openid"),
            },
        }
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 微信支付退款(2026-04-25 配套 JSAPI) ====================

class _WxRefundBody(BaseModel):
    """微信退款 body"""
    order_id: str
    # Legacy orders still use this field. Dealer/direct-service orders ignore it
    # and load integer cents from their persisted refund intent/cash job.
    refund_yuan: Optional[float] = None
    reason: Optional[str] = "用户申请退款"


@router.post("/wechat-refund")
async def wechat_refund(body: _WxRefundBody, request: Request):
    """微信支付退款 · 两步流的第二步(发出真实微信退款请求)

    两步流(2026-04-26 Codex 复验加 guard):
      Step 1: 用户/admin 调 POST /api/wallet/refund 创建退款申请
              → 扣用户钱包未消耗积分 + clawback 代理 settled 佣金
              + 标记 referral_bonus_records.fraud_flag
              + recharge_orders.refund_status='pending'
      Step 2: admin 调本端点 POST /api/wallet/wechat-refund
              → 真正发出微信退款请求(钱真退到用户银行卡)
              → 同步 recharge_orders.refund_status 终态

    规则:
      - 必须 admin(防代理直接退自己单)
      - 必须 refund_status IN ('pending','approved') · 即 Step 1 已完成
      - 退款金额不得超过原单金额；消费者退款不预扣固定百分比费用
      - 退款单号自动生成 · 与 order_id 1:1 不重发

    禁止:admin 跨过 Step 1 直接调本端点退真钱(资金会真退给用户但平台
    钱包没扣 / 代理 settled 佣金没 clawback / referral_bonus 没标 fraud)。
    """
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="退款仅 admin 可操作 · 客户走客服流程")

    order = get_recharge_order(body.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.get("payment_status") != "paid":
        raise HTTPException(status_code=400, detail="订单未支付 · 不支持退款")

    # 2026-04-26 资金安全 guard(Codex 复验):
    # 仅允许 refund_status='pending' 或 'approved' (即用户/admin 已通过 /api/wallet/refund 创建退款申请)
    # NULL/rejected/failed/completed 一律拒绝 · 防 admin 跨步骤直接退真钱导致的资金/账务不一致
    refund_status = order.get("refund_status")
    if refund_status not in ("pending", "approved"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"refund_status={refund_status or 'NULL'} · 不允许直接微信退款。"
                "请先调 POST /api/wallet/refund 创建退款申请(扣用户钱包+clawback 代理佣金+标记 fraud)· "
                "本端点只负责发出真实微信退款请求 · 不能绕过钱包/佣金清算链路。"
                "admin 强制退款要另开完整链路 · 不能走本简化端点"
            ),
        )

    amount_cents = int(order.get("amount_cents") or 0)
    total_yuan = amount_cents / 100.0
    _is_dealer_resale_refund = False
    _is_direct_service_refund = False
    from db.connection import get_connection as _resale_get_conn
    _resale_conn = _resale_get_conn()
    try:
        from services import dealer_inventory_resale

        _resale_cur = _resale_conn.cursor()
        if dealer_inventory_resale.has_resale_order(_resale_cur, body.order_id):
            _is_dealer_resale_refund = True
        _is_direct_service_refund = dealer_inventory_resale.has_consumer_sale(
            _resale_cur, body.order_id
        )
    finally:
        _resale_conn.close()
    if _is_dealer_resale_refund or _is_direct_service_refund:
        from services.refund_cash_execution import (
            RefundExecutionError,
            execute_persisted_refund,
        )

        try:
            execution = await execute_persisted_refund(
                order_id=str(body.order_id), reason=body.reason or "用户申请退款",
            )
            return {"success": True, "data": execution}
        except RefundExecutionError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
    if body.refund_yuan is None:
        raise HTTPException(status_code=422, detail="旧退款订单必须提交 refund_yuan")
    if body.refund_yuan <= 0 or body.refund_yuan > total_yuan:
        raise HTTPException(status_code=400, detail=f"退款金额无效(原单 ¥{total_yuan:g})")

    out_refund_no = f"R{body.order_id}"  # 退款单号 = R + 原订单号(幂等 · 同单同号)

    try:
        from services.wechat_pay import refund_order
        result = await refund_order(
            out_trade_no=body.order_id,
            out_refund_no=out_refund_no,
            refund_yuan=body.refund_yuan,
            total_yuan=total_yuan,
            reason=body.reason or "用户申请退款",
        )

        # 2026-04-26 Codex 复验补丁: refund_order 拿到 result 后立即同步 recharge_orders
        # 防止"微信首次回调即 SUCCESS · 而 callback WHERE refund_status IN ('pending','approved') 更新 0 行"
        # 三态分流:
        #   SUCCESS → completed + refund_completed_at + refunded_amount_cents
        #   PROCESSING → pending + refund_requested_at(COALESCE 防覆盖) + refunded_amount_cents
        #   ABNORMAL/CLOSED 等失败态 → failed
        wxpay_status = result.get("status")
        refunded_amount_cents = int(round(body.refund_yuan * 100))
        try:
            from db.connection import get_connection as _get_conn
            _conn = _get_conn()
            try:
                _cur = _conn.cursor()
                if wxpay_status == "SUCCESS":
                    _provider_refund_id = str(result.get("refund_id") or out_refund_no)
                    _provider_evidence = json.dumps(
                        {
                            "refund_provider": "wechat",
                            "provider_refund_id": _provider_refund_id,
                            "refund_evidence_kind": "wechat_refund_api",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    _cur.execute("""
                        UPDATE recharge_orders
                        SET refund_status = 'completed',
                            refund_completed_at = NOW(),
                            refund_requested_at = COALESCE(refund_requested_at, NOW()),
                            refunded_amount_cents = %s,
                            settlement_snapshot_jsonb =
                                COALESCE(settlement_snapshot_jsonb, '{}'::jsonb) || %s::jsonb
                        WHERE id = %s
                    """, (refunded_amount_cents, _provider_evidence, body.order_id))
                elif wxpay_status == "PROCESSING":
                    _cur.execute("""
                        UPDATE recharge_orders
                        SET refund_status = 'pending',
                            refund_requested_at = COALESCE(refund_requested_at, NOW()),
                            refunded_amount_cents = %s
                        WHERE id = %s
                    """, (refunded_amount_cents, body.order_id))
                else:
                    # ABNORMAL / CLOSED / 其他失败态 → failed
                    _cur.execute("""
                        UPDATE recharge_orders
                        SET refund_status = 'failed',
                            refund_requested_at = COALESCE(refund_requested_at, NOW())
                        WHERE id = %s
                    """, (body.order_id,))
                # [v10 item6] 退款状态已落库 → canonical 冲销渠道收益(仅 completed 生效态才真冲·PROCESSING/failed no-op)·与状态原子提交。
                from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund
                reverse_channel_revenue_on_refund(_cur, body.order_id)
                if wxpay_status == "SUCCESS":
                    from services import dealer_inventory_resale

                    dealer_inventory_resale.sync_consumer_refund_from_recharge(
                        _cur, body.order_id
                    )
                _conn.commit()
            except Exception as _se:
                try:
                    _conn.rollback()
                except Exception:
                    pass
                logger.error(
                    f"wechat-refund 同步 recharge_orders 失败 order={body.order_id} "
                    f"wxpay_status={wxpay_status}: {_se}"
                )
                if _is_dealer_resale_refund or _is_direct_service_refund:
                    raise RuntimeError(
                        "现金退款已受理但直属库存/利润终态未能原子落库 · 拒绝返回成功"
                    ) from _se
            finally:
                try:
                    _conn.close()
                except Exception:
                    pass
        except Exception as _outer_e:
            logger.error(f"wechat-refund 状态同步连接异常: {_outer_e}")
            if _is_dealer_resale_refund or _is_direct_service_refund:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "DEALER_RESALE_REFUND_SYNC_FAILED",
                        "message": (
                            "退款通道已返回结果，但本跳库存/利润终态尚未可靠同步；"
                            "请按同一退款单号幂等重试，禁止人工重复退款"
                        ),
                    },
                ) from _outer_e

        # 写 audit_log
        try:
            from db.auth_db import create_audit_log
            create_audit_log(
                user_id=user.get("user_id"),
                username=user.get("username"),
                action="wechat_refund",
                module="wallet",
                entity_type="recharge_order",
                entity_id=int(body.order_id) if str(body.order_id).isdigit() else None,
                summary=f"申请退款 ¥{body.refund_yuan:g}(原单 ¥{total_yuan:g}) · 原因:{body.reason}",
                after={
                    "order_id": body.order_id,
                    "out_refund_no": out_refund_no,
                    "refund_yuan": body.refund_yuan,
                    "total_yuan": total_yuan,
                    "reason": body.reason,
                    "wxpay_status": wxpay_status,
                    "refund_id": result.get("refund_id"),
                },
            )
        except Exception:
            pass

        return {
            "success": True,
            "data": {
                "order_id": body.order_id,
                "out_refund_no": out_refund_no,
                "refund_id": result.get("refund_id"),
                "status": wxpay_status,  # SUCCESS / PROCESSING / ABNORMAL
                "amount_refund": result.get("amount", {}).get("refund"),
            },
        }
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/wechat-refund-callback")
async def wechat_refund_callback(request: Request):
    """微信退款异步回调(对账用)· 验签 + 解密 + 写 log"""
    body = await request.body()
    body_str = body.decode("utf-8")

    timestamp = request.headers.get("Wechatpay-Timestamp", "")
    nonce = request.headers.get("Wechatpay-Nonce", "")
    signature = request.headers.get("Wechatpay-Signature", "")

    from services.wechat_pay import verify_callback, decrypt_callback

    if not verify_callback(timestamp, nonce, body_str, signature):
        logger.warning("微信退款回调验签失败 · 可能伪造")
        return {"code": "FAIL", "message": "验签失败"}

    try:
        notify = json.loads(body_str)
        resource = notify.get("resource", {})
        decrypted = decrypt_callback(
            ciphertext=resource.get("ciphertext", ""),
            nonce=resource.get("nonce", ""),
            associated_data=resource.get("associated_data", ""),
        )
    except Exception as e:
        logger.error(f"退款回调解密失败: {e}")
        return {"code": "FAIL", "message": "解密失败"}

    out_refund_no = decrypted.get("out_refund_no")
    refund_status = decrypted.get("refund_status")
    out_trade_no = decrypted.get("out_trade_no")
    refund_id = decrypted.get("refund_id")
    callback_refund_cents = None
    try:
        callback_refund_cents = int((decrypted.get("amount") or {}).get("refund"))
    except (TypeError, ValueError):
        callback_refund_cents = None

    logger.info(
        f"微信退款回调: order={out_trade_no} refund_no={out_refund_no} status={refund_status}"
    )

    # 2026-04-26 P0/P1 修复: 同步更新 recharge_orders.refund_status (状态闭环)
    # 微信 V3 refund_status 取值: SUCCESS / PROCESSING / ABNORMAL / CLOSED
    # 仅修 admin/wechat refund 链路的状态闭环 · 不等于开放用户自动退款(见 /refund RFC)
    update_target_status: Optional[str] = None
    if refund_status == "SUCCESS":
        update_target_status = "completed"
    elif refund_status == "PROCESSING":
        update_target_status = "pending"
    elif refund_status in ("ABNORMAL", "CLOSED"):
        update_target_status = "failed"
    else:
        logger.warning(f"微信退款回调未知 refund_status: {refund_status} order={out_trade_no}")

    if out_trade_no and update_target_status:
        from db.connection import get_connection as _get_conn
        _conn = _get_conn()
        try:
            _cur = _conn.cursor()
            if update_target_status == "completed":
                if callback_refund_cents is None:
                    raise RuntimeError("微信退款成功回调缺少可核验退款金额")
                _provider_evidence = json.dumps(
                    {
                        "refund_provider": "wechat",
                        "provider_refund_id": str(refund_id or out_refund_no or ""),
                        "refund_evidence_kind": "signed_wechat_callback",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                # SUCCESS: 允许 NULL(legacy 老订单 + 微信首次回调即 SUCCESS 的场景) + pending/approved
                # Codex 复验补丁 2026-04-26: 不加 NULL 会导致首次回调更新 0 行 · DB 永远停在 pending
                _cur.execute("""
                    UPDATE recharge_orders
                    SET refund_status = 'completed',
                        refund_completed_at = NOW(),
                        refunded_amount_cents = %s,
                        settlement_snapshot_jsonb =
                            COALESCE(settlement_snapshot_jsonb, '{}'::jsonb) || %s::jsonb
                    WHERE id = %s
                      AND (refund_status IS NULL OR refund_status IN ('pending', 'approved', 'completed'))
                """, (callback_refund_cents, _provider_evidence, out_trade_no))
            elif update_target_status == "pending":
                # PROCESSING: 仅在 NULL 时升到 pending (已 pending 不变, 已 completed 不退化)
                _cur.execute("""
                    UPDATE recharge_orders
                    SET refund_status = 'pending'
                    WHERE id = %s AND refund_status IS NULL
                """, (out_trade_no,))
            else:  # failed
                # ABNORMAL/CLOSED: 仅在非 completed 时改为 failed (不覆盖成功终态)
                _cur.execute("""
                    UPDATE recharge_orders
                    SET refund_status = 'failed'
                    WHERE id = %s AND (refund_status IS NULL OR refund_status NOT IN ('completed'))
                """, (out_trade_no,))
            _updated = _cur.rowcount   # [v10 item6] 先捕获退款 UPDATE 行数(canonical 的 execute 会覆盖 _cur.rowcount)
            # [v10 item6] canonical 冲销渠道收益(仅 completed 生效态真冲·pending/failed no-op)· 与退款状态原子提交
            from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund
            reverse_channel_revenue_on_refund(_cur, out_trade_no)
            if update_target_status == "completed":
                from services import dealer_inventory_resale

                dealer_inventory_resale.sync_consumer_refund_from_recharge(
                    _cur, out_trade_no
                )
            _conn.commit()
            logger.info(
                f"微信退款回调更新 recharge_orders: order={out_trade_no} "
                f"target={update_target_status} updated_rows={_updated}"
            )
        except Exception as _e:
            try:
                _conn.rollback()
            except Exception:
                pass
            logger.error(
                f"微信退款回调更新 recharge_orders 失败 order={out_trade_no}: {_e}"
            )
            return {"code": "FAIL", "message": "退款终态暂不可确认，请重试"}
        finally:
            try:
                _conn.close()
            except Exception:
                pass

    # 写入 audit_log(退款最终结果)
    try:
        from db.auth_db import create_audit_log
        create_audit_log(
            action="wechat_refund_callback",
            module="wallet",
            entity_type="recharge_order",
            summary=f"微信退款回调 · {refund_status} · order={out_trade_no} refund={out_refund_no}",
            after=decrypted,
        )
    except Exception:
        pass

    return {"code": "SUCCESS", "message": "OK"}


@router.post("/wechat-callback")
async def wechat_payment_callback(request: Request):
    """
    微信支付异步通知回调
    微信会 POST 到这个 URL，通知支付结果
    """
    body = await request.body()
    body_str = body.decode("utf-8")

    timestamp = request.headers.get("Wechatpay-Timestamp", "")
    nonce = request.headers.get("Wechatpay-Nonce", "")
    signature = request.headers.get("Wechatpay-Signature", "")

    from services.wechat_pay import verify_callback, decrypt_callback

    if not verify_callback(timestamp, nonce, body_str, signature):
        logger.warning("微信回调验签失败！可能是伪造请求")
        return {"code": "FAIL", "message": "验签失败"}

    # [Deploy-CTO 2026-04-25] 在 try 外预 init · 防 except 引用未定义变量
    ciphertext = ""
    nonce_str = ""
    ad_str = ""
    try:
        notify = json.loads(body_str)
        resource = notify.get("resource", {})
        ciphertext = resource.get("ciphertext", "")
        nonce_str = resource.get("nonce", "")
        ad_str = resource.get("associated_data", "")
        decrypted = decrypt_callback(
            ciphertext=ciphertext,
            nonce=nonce_str,
            associated_data=ad_str,
        )
    except Exception as e:
        # [Deploy-CTO 2026-04-25] 诊断:type/repr + 关键长度,排查 InvalidTag(密钥不一致)
        import os as _os
        _api_key_len = len(_os.environ.get("WX_APIV3_KEY", ""))
        _err_type = type(e).__name__
        _err_repr = repr(e)
        logger.error(
            "解密回调数据失败: type=%s repr=%s key_len=%d ciphertext_b64_len=%d nonce_len=%d ad=%s",
            _err_type, _err_repr, _api_key_len, len(ciphertext), len(nonce_str), ad_str,
        )
        return {"code": "FAIL", "message": "解密失败"}

    out_trade_no = decrypted.get("out_trade_no")
    trade_state = decrypted.get("trade_state")
    transaction_id = decrypted.get("transaction_id", "")
    # [#6] 解出回调金额、商户号、AppID 用于一致性校验
    amount_total_cents = (decrypted.get("amount") or {}).get("total")
    callback_mchid = decrypted.get("mchid", "")
    callback_appid = decrypted.get("appid", "")

    logger.info(
        f"微信回调: order={out_trade_no}, state={trade_state}, txn={transaction_id}, "
        f"amount_total={amount_total_cents}, mchid={callback_mchid}, appid={callback_appid}"
    )

    if trade_state == "SUCCESS":
        # [#6] 三重一致性校验：金额 + 商户号 + AppID
        from services.wechat_pay import MCH_ID, APPID
        ok, reason = _verify_callback_consistency(
            order_id=out_trade_no,
            callback_amount_cents=int(amount_total_cents or 0),
            callback_label="微信回调",
            extra_checks={
                "mchid": (MCH_ID, callback_mchid),
                "appid": (APPID, callback_appid),
            },
        )
        if not ok:
            # 高危事件：金额/商户号/AppID 不一致 → 拒绝入账 + 高优先级日志（建议接入 Sentry）
            logger.error(f"[#6][高危] {reason} - 拒绝入账")
            # 返回 FAIL 让微信重试一次（可能是网络抖动重投），重投仍不一致就是真异常
            return {"code": "FAIL", "message": "回调字段一致性校验失败"}

        result = complete_recharge(out_trade_no, transaction_id)
        if result:
            logger.info(f"充值完成: order={out_trade_no}")
            _fire_marketing_recharge_hook(result)
        else:
            logger.warning(f"充值订单不存在: {out_trade_no}")

    return {"code": "SUCCESS", "message": "OK"}


def _trusted_xunhupay_refund_reference_cur(cur, order_id: str) -> str:
    """Load an out_refund_no previously persisted from a signed refund response.

    Xunhupay's payment callback ``transaction_id`` is the original payment
    transaction number.  It is never a refund number, so an asynchronous CD
    callback may only close the local cash leg when the refund endpoint has
    already persisted its signed ``out_refund_no`` for this order.
    """

    cur.execute(
        """SELECT provider_refund_id,provider_evidence_jsonb
             FROM service_refund_cash_jobs
            WHERE source_order_id=%s
            FOR UPDATE""",
        (str(order_id),),
    )
    row = cur.fetchone()
    if row:
        provider_ref = str(
            (row.get("provider_refund_id") if isinstance(row, dict) else row[0]) or ""
        ).strip()
        evidence = (
            row.get("provider_evidence_jsonb") if isinstance(row, dict) else row[1]
        ) or {}
        signed_refund_reference = (
            isinstance(evidence, dict)
            and evidence.get("provider") == "xunhupay"
            and evidence.get("refund_evidence_kind") == "signed_xunhupay_refund_response"
        )
        canonical_callback_reference = (
            isinstance(evidence, dict)
            and evidence.get("provider") == "xunhupay"
            and evidence.get("refund_evidence_kind") == "signed_xunhupay_callback"
            and evidence.get("provider_refund_reference_kind")
            == "signed_xunhupay_refund_response"
            and evidence.get("source") == "recharge_orders.settlement_snapshot_jsonb"
        )
        if (
            provider_ref
            and (signed_refund_reference or canonical_callback_reference)
            and str(evidence.get("provider_refund_id") or "").strip() == provider_ref
        ):
            return provider_ref

    cur.execute(
        """SELECT external_refund_id,audit_jsonb
             FROM dealer_resale_refunds
            WHERE order_id=%s
            FOR UPDATE""",
        (str(order_id),),
    )
    row = cur.fetchone()
    if row:
        provider_ref = str(
            (row.get("external_refund_id") if isinstance(row, dict) else row[0]) or ""
        ).strip()
        audit = (row.get("audit_jsonb") if isinstance(row, dict) else row[1]) or {}
        evidence = audit.get("provider_execution", {}) if isinstance(audit, dict) else {}
        if (
            provider_ref
            and isinstance(evidence, dict)
            and evidence.get("provider") == "xunhupay"
            and evidence.get("refund_evidence_kind") == "signed_xunhupay_refund_response"
            and str(evidence.get("provider_refund_id") or "").strip() == provider_ref
        ):
            return provider_ref
    return ""


def _flag_channel_refund(
    order_id: str,
    event: str,
    total_fee,
    *,
    channel: str,
    provider_refund_id: str = "",
    payment_transaction_id: str = "",
) -> bool:
    """[P0-14 · v11 F1/F2 · v12 item1] 渠道退款回调 RD/CD/UD 状态机驱动 · 推进 refund_status + 仅 CD 冲销收益。

    显式转移表 `channel_revenue_lifecycle.channel_refund_transition`(单一权威口径):
      - RD=退款处理中 → channel_refunding(IN_FLIGHT · 不冲收益·不退内账);
      - CD=现金退款成功 → pending_review(REFUND_EFFECTIVE)+ canonical 收益冲销(仅订单存在且状态迁移成功);
      - UD=退款失败 → 退出 channel_refunding 回 'failed'(保原收益+内账 · 允许人工重试)。
    fail-closed(返 False → 回调回非 success)覆盖:**未知订单 / 非 paid / 非法迁移或冲突(UD 撞已生效) /
      状态 UPDATE touched≠1(乱序竞态)/ 写库或冲销异常**。只有"状态已持久化"或"已处于幂等终态"才返 True。
    幂等:重复 RD/CD/UD 与乱序均由转移表判 noop/illegal · 不重复冲销(reverse WHERE status='recorded' 幂等)。
    遵守 refund-admin-ticket-only:只 flag+告警·绝不自动打款/静默扣负;已消费余额由 admin 工单按快照人工处理。
    """
    from services.channel_revenue_lifecycle import channel_refund_transition, reverse_channel_revenue_on_refund
    try:
        from db.connection import get_db  # 局部导入避免循环
        with get_db() as conn:
            cur = conn.cursor()
            # 1. FOR UPDATE 锁订单(未知订单/非 paid → fail-closed·不假成功 ACK)
            cur.execute(
                "SELECT id,refund_status,payment_status,payment_id "
                "FROM recharge_orders WHERE id=%s FOR UPDATE",
                (order_id,),
            )
            row = cur.fetchone()
            if not row:
                logger.error("[P0-14][fail-closed] 渠道退款回调未知订单 order=%s event=%s → 回非 success", order_id, event)
                return False
            _pay = row["payment_status"] if isinstance(row, dict) else row[2]
            if _pay != "paid":
                logger.error("[P0-14][fail-closed] 渠道退款回调订单非 paid order=%s pay=%s → 回非 success", order_id, _pay)
                return False
            cur_rs = row["refund_status"] if isinstance(row, dict) else row[1]
            persisted_payment_id = str(
                (row.get("payment_id") if isinstance(row, dict) else row[3]) or ""
            ).strip()
            callback_payment_id = str(payment_transaction_id or "").strip()
            if callback_payment_id and (
                not persisted_payment_id or callback_payment_id != persisted_payment_id
            ):
                raise RuntimeError("signed callback payment transaction does not match order")
            # 2. 显式转移表决策
            t = channel_refund_transition(cur_rs, event)
            if t["ack"] == "failclosed":
                logger.critical("[P0-14][fail-closed] 渠道退款非法/冲突 order=%s event=%s rs=%s → %s(回非 success·等人工)",
                                order_id, event, cur_rs, t["reason"])
                return False
            touched = None
            if t["action"] == "set":
                # 乐观锁 WHERE=读到的当前态(IS NOT DISTINCT FROM 兼容 NULL)· touched≠1 视为竞态 → fail-closed 整事务回滚
                if event.upper() == "CD":
                    provider_ref = str(provider_refund_id or "").strip()
                    if not provider_ref and channel == "xunhupay":
                        provider_ref = _trusted_xunhupay_refund_reference_cur(cur, order_id)
                    if not provider_ref:
                        raise RuntimeError("signed CD callback missing persisted provider refund id")
                    refund_cents = int(round(float(total_fee) * 100))
                    evidence_json = json.dumps(
                        {
                            "refund_provider": channel,
                            "provider_refund_id": provider_ref,
                            "payment_transaction_id": callback_payment_id,
                            "provider_refund_reference_kind": "signed_xunhupay_refund_response",
                            "refund_evidence_kind": "signed_xunhupay_callback",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    cur.execute(
                        """UPDATE recharge_orders
                           SET refund_status=%s,
                               refund_completed_at=COALESCE(refund_completed_at,NOW()),
                               refunded_amount_cents=%s,
                               settlement_snapshot_jsonb=
                                   COALESCE(settlement_snapshot_jsonb,'{}'::jsonb) || %s::jsonb
                           WHERE id=%s AND payment_status='paid'
                             AND refund_status IS NOT DISTINCT FROM %s""",
                        (t["target"], refund_cents, evidence_json, order_id, cur_rs),
                    )
                elif event.upper() == "RD":
                    cur.execute(
                        """UPDATE recharge_orders
                           SET refund_status=%s,
                               refund_requested_at=COALESCE(refund_requested_at,NOW())
                           WHERE id=%s AND payment_status='paid'
                             AND refund_status IS NOT DISTINCT FROM %s""",
                        (t["target"], order_id, cur_rs),
                    )
                else:
                    cur.execute(
                        """UPDATE recharge_orders SET refund_status=%s
                           WHERE id=%s AND payment_status='paid'
                             AND refund_status IS NOT DISTINCT FROM %s""",
                        (t["target"], order_id, cur_rs),
                    )
                touched = cur.rowcount
                if touched != 1:
                    raise RuntimeError(f"state transition touched={touched}!=1 (event={event} rs={cur_rs})")
            elif event.upper() == "CD" and t["action"] == "noop":
                # 重复 CD 仍可为旧 pending_review/终态补齐可信回调证据,不改变退款终态。
                provider_ref = str(provider_refund_id or "").strip()
                if not provider_ref and channel == "xunhupay":
                    provider_ref = _trusted_xunhupay_refund_reference_cur(cur, order_id)
                if not provider_ref:
                    raise RuntimeError("signed CD callback missing persisted provider refund id")
                refund_cents = int(round(float(total_fee) * 100))
                evidence_json = json.dumps(
                    {
                        "refund_provider": channel,
                        "provider_refund_id": provider_ref,
                        "payment_transaction_id": callback_payment_id,
                        "provider_refund_reference_kind": "signed_xunhupay_refund_response",
                        "refund_evidence_kind": "signed_xunhupay_callback",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                cur.execute(
                    """UPDATE recharge_orders
                       SET refund_completed_at=COALESCE(refund_completed_at,NOW()),
                           refunded_amount_cents=%s,
                           settlement_snapshot_jsonb=
                               COALESCE(settlement_snapshot_jsonb,'{}'::jsonb) || %s::jsonb
                       WHERE id=%s AND payment_status='paid'""",
                    (refund_cents, evidence_json, order_id),
                )
                touched = cur.rowcount
                if touched != 1:
                    raise RuntimeError(f"CD evidence touched={touched}!=1 (rs={cur_rs})")
            if t["reverse"]:
                # 仅 CD(target=pending_review 或已生效幂等)冲销 · 冲销内异常自登记耐久补偿;登记再失败传播→整事务回滚→False
                reverse_channel_revenue_on_refund(cur, order_id)
                from services import dealer_inventory_resale

                dealer_inventory_resale.sync_consumer_refund_from_recharge(cur, order_id)
        logger.error(
            "[P0-14][高危·需人工退款工单] 渠道=%s 订单=%s event=%s total_fee=%s → action=%s target=%s reverse=%s"
            "(touched=%s · %s)· 请核对原订单快照人工冲账,勿自动白退",
            channel, order_id, event, total_fee, t["action"], t.get("target"), t["reverse"], touched, t["reason"],
        )
        return True
    except Exception as _e:  # noqa: BLE001
        # fail-closed:不吞 → 返 False · 回调据此回非 success 让渠道重试(状态机 + 冲销幂等 · 整事务已回滚不留半态)
        logger.error("[P0-14][fail-closed] refund flag 失败(回非 success 让渠道重试) order=%s event=%s: %s", order_id, event, _e)
        return False


@router.post("/xunhupay-callback")
async def xunhupay_payment_callback(request: Request):
    """虎皮椒聚合支付异步通知回调

    - 虎皮椒 POST form 表单到这里
    - 用 MD5 签名验签
    - 成功必须返纯文本 "success"（不是 JSON），否则虎皮椒会重试 6 次
    - status='OD' 表示已支付，'CD' 已退款，'RD' 退款中，'UD' 退款失败
    """
    from fastapi.responses import PlainTextResponse
    from services.xunhupay import verify_xunhupay_callback, STATUS_PAID

    try:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
    except Exception as e:
        logger.error(f"虎皮椒回调解析 form 失败: {e}")
        return PlainTextResponse("error", status_code=400)

    logger.info(
        "虎皮椒回调: order_id=%s, status=%s, txn=%s, fee=%s",
        params.get("trade_order_id"),
        params.get("status"),
        params.get("transaction_id"),
        params.get("total_fee"),
    )

    # 签名校验
    if not verify_xunhupay_callback(params):
        logger.warning("虎皮椒回调验签失败，可能伪造")
        return PlainTextResponse("fail", status_code=400)

    status = params.get("status", "")
    order_id = params.get("trade_order_id", "")
    transaction_id = params.get("transaction_id", "")

    if status in (STATUS_PAID, "CD", "RD", "UD"):
        # 支付与退款状态都必须先核对原订单金额。CD/RD/UD 若金额不一致，不能成为整单退款证据。
        try:
            callback_fee_yuan = float(params.get("total_fee", "0"))
            callback_cents = int(round(callback_fee_yuan * 100))
        except (ValueError, TypeError):
            logger.error(f"[#6][高危] 虎皮椒 total_fee 解析失败: {params.get('total_fee')}")
            return PlainTextResponse("fail", status_code=400)

        ok, reason = _verify_callback_consistency(
            order_id=order_id,
            callback_amount_cents=callback_cents,
            callback_label="虎皮椒回调",
        )
        if not ok:
            logger.error(f"[#6][高危] {reason} - 拒绝处理回调")
            return PlainTextResponse("fail", status_code=400)

    if status == STATUS_PAID:
        # 幂等：complete_recharge 内部检查了 payment_status='paid' 直接 return
        result = complete_recharge(order_id, transaction_id)
        if result:
            logger.info(f"充值完成(虎皮椒): order={order_id}")
            _fire_marketing_recharge_hook(result)
        else:
            logger.warning(f"充值订单不存在或已处理: {order_id}")
    elif status in ("CD", "RD", "UD"):
        # [P0-14 · v11 · v12 item1] 退款回调 RD(退款中)/CD(退款成功)/UD(退款失败)由状态机统一处理:
        #   RD→channel_refunding(不冲) · CD→pending_review+冲销 · UD→failed(保收益·允许人工重试)。
        #   遵守 refund-admin-ticket-only 铁律:不自动冲账、不静默扣成负、不白退现金。
        _ok = _flag_channel_refund(
            order_id,
            status,
            params.get("total_fee"),
            channel="xunhupay",
            payment_transaction_id=transaction_id,
        )
        if not _ok:
            # fail-closed:未知订单/非法迁移/冲突/未耐久落库 → 回非 success 让虎皮椒重试(最多 6 次)/等人工。
            #   禁 fail-open:否则"现金已退、平台无退款记录、算力未追回、渠道收益未冲销"。契约:非 "success" 即重试
            #   (services/xunhupay.py:9 + 上方同级失败分支 1487/1500/1509 均 "fail"/400)· 不自造格式。
            logger.error("[P0-14][fail-closed] 虎皮椒退款回调未收口(fail-closed) → 返 fail 让渠道重试 order=%s status=%s",
                         order_id, status)
            return PlainTextResponse("fail", status_code=400)
    else:
        logger.warning(f"虎皮椒回调未知状态: order={order_id}, status={status}")
        return PlainTextResponse("fail", status_code=400)

    # 必须返 "success" 纯文本，否则虎皮椒会重试 6 次
    return PlainTextResponse("success")


@router.get("/order-status/{order_id}")
async def get_order_status(order_id: str, request: Request):
    """查询充值订单状态（前端轮询）

    [Deploy-CTO 2026-04-25] 加微信主动 query 兜底:
    - 若订单 wechat 渠道 + pending + 距创建 > 15 秒
    - 主动调 wechat_pay.query_order 问微信端真实状态
    - 微信返 SUCCESS → 立刻 complete_recharge → 入账 + 返 paid
    - 兜底原因:WX_APIV3_KEY 配错时 callback 解密失败 InvalidTag,
      但商户私钥签名(query_order)仍工作,可绕开 callback 链路
    """
    user = request.state.user

    order = get_recharge_order(order_id)

    if not order:
        raise HTTPException(404, detail="订单不存在")
    if order["user_id"] != user["user_id"] and not user.get("is_admin"):
        # #169 · 改 403 → 404:403 等于告诉对方"这张单存在,只是不归你",
        #        订单号可枚举时那是一条信息泄露。对非所有者,"存在"本身就不该回答。
        #        Review 2026-09-10 §2.2.2 裁定:404 维持(工单首版写的 403 是笔误,
        #        已由 Review 订正)。admin 旁路保留不变。
        raise HTTPException(404, detail="订单不存在")

    # [兜底] wechat 渠道 pending 超 5 秒 → 主动 query 微信
    # 5s 阈值理由:callback 永远失败的现状 · 越早 query 越好
    # 不设 0s 是因为:用户刚扫码还没付款 · query 返 NOTPAY 浪费一次往返
    if order["payment_status"] == "pending" and order.get("payment_method") == "wechat":
        try:
            from datetime import datetime as _dt
            created = order.get("created_at")
            elapsed = (_dt.now() - created).total_seconds() if created else 999
            if elapsed > 5:
                from services.wechat_pay import query_order as _wx_query
                wx_result = await _wx_query(order_id)
                wx_state = wx_result.get("trade_state")
                if wx_state == "SUCCESS":
                    txn = wx_result.get("transaction_id", "")
                    rescued = complete_recharge(order_id, txn or "QUERY_RESCUE")
                    if rescued:
                        logger.info(f"order-status 主动 query 救援成功: order={order_id} txn={txn}")
                        _fire_marketing_recharge_hook(rescued)
                        order = get_recharge_order(order_id)  # 重读拿最新 paid 状态
        except Exception as _e:
            logger.warning(f"order-status query 兜底失败(不阻断): order={order_id} err={_e}")

    # #169 · 恢复支付出口:把建单时存下的字段一并回给前端,
    #        让"跳出去付款没成、回来"的用户能接着付**同一张单**。
    # 🔴 全部是**重取已存的行**,不向虎皮椒/微信发任何新的**下单**请求。
    #    (上面那段 wechat query 兜底是既有的**查询**救援,逐字未动。)
    # 🔴 已 paid 的单不带支付出口:钱已经付了,再给一个能付款的链接就是诱导重复支付。
    data = {
        "order_id": order["id"],
        "status": order["payment_status"],
        "paid_at": str(order.get("paid_at", "")) if order.get("paid_at") else None,
    }
    if order["payment_status"] == "pending":
        from decimal import Decimal
        _cents = int(order.get("amount_cents") or 0)
        _channel = order.get("actual_payment_channel")
        data.update({
            "amount_yuan": float(Decimal(_cents) / Decimal(100)),
            "total_points": int(order.get("base_points") or 0) + int(order.get("bonus_points") or 0),
            "base_points": int(order.get("base_points") or 0),
            "bonus_points": int(order.get("bonus_points") or 0),
            "actual_channel": _channel,
            "payment_url_mobile": order.get("payment_url_mobile") or None,
            "payment_url_qrcode": order.get("payment_url_qrcode") or None,
            "code_url": order.get("code_url") or None,
            # needs_openid 由通道推出:JSAPI 必须先拿 openid 才能调起。
            "needs_openid": _channel == "wechat_jsapi",
        })

    return {"success": True, "data": data}


PAYMENT_CALLBACK_SECRET = os.environ.get("PAYMENT_CALLBACK_SECRET", "")


@router.post("/recharge/callback")
async def payment_callback(req: PaymentCallbackRequest, request: Request):
    """
    [已下线 · BUG-P3] 遗留通用支付回调。

    原仅校验共享密钥 X-Callback-Secret 即 complete_recharge 入账,无金额/订单态/真实支付凭证
    校验 → 密钥泄露即可把任意 pending 订单刷成已支付并凭空入账(免费入账后门)。
    真实支付已走专用回调 /wechat-callback、/xunhupay-callback(均经 _verify_callback_consistency
    三重校验金额+商户号+AppID)。prod PAYMENT_CALLBACK_SECRET 空已恒 500 不可用 → 本端点恒拒绝下线,
    消除后门(防未来误配 SECRET 重新打开)。
    [营销 cherry-pick 冲突解 · 2026-07-05] 保安全下线版;营销 recharge hook 不挂死端点,
    需营销 CTO 改挂真实回调 /wechat-callback、/xunhupay-callback(follow-up)。
    """
    raise HTTPException(
        status_code=410,
        detail="该回调入口已下线 · 真实支付请走微信/虎皮椒专用回调(经金额一致性校验)",
    )


@router.post("/manual-recharge")
async def manual_recharge(req: PaymentCallbackRequest, request: Request):
    """
    手动充值确认（管理员后台操作，支付审核期间使用）
    """
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, detail="仅管理员可操作")

    result = complete_recharge(req.order_id, req.payment_id or "MANUAL")
    if not result:
        raise HTTPException(404, detail="订单不存在")
    _fire_marketing_recharge_hook(result)
    return {"success": True, "data": {"order_id": req.order_id, "status": "paid"}}


# ==================== 扣费端点（前端 DeductDialog 调用） ====================

class DeductRequest(BaseModel):
    feature_code: str


@router.post("/deduct")
async def deduct(req: DeductRequest, request: Request):
    """
    前端扣费端点 — DeductDialog 确认后调用
    管理员免费使用所有功能
    """
    user = request.state.user

    # 管理员免扣费
    if user.get("is_admin"):
        wallet = get_wallet_balance(user["user_id"])
        return {
            "success": True,
            "deducted": 0,
            "free": True,
            "remaining_paid": wallet.get("paid_points", 0),
            "remaining_bonus": wallet.get("bonus_points", 0),
        }

    from middleware.billing import deduct_points
    try:
        result = await deduct_points(user["user_id"], req.feature_code)
    except ValueError as exc:
        # 🔴 [商业边界裁决 2026-08-10] 下架商品必须被**明确拒绝**,不能是 500。
        #   链路:`get_feature_pricing` 带 `WHERE is_active = TRUE`,查不到就
        #   `raise ValueError("未知的功能编码: …")`;`deduct_points` 不捕它,
        #   于是一路穿到 FastAPI 变成 **500**——用户看到的是"服务器错误",
        #   既不知道商品下架了,也没有下一步(总册 §13.5:拦截必须带出口)。
        #   这里把它翻成 400 + 人话 + 出口。
        #   ⚠️ 改在本文件而不是 `middleware/billing.py` —— 后者是七保护文件。
        #   ⚠️ 未知 code 与已下架 code 在这一层**不可区分**(都是同一个 ValueError),
        #      故文案两种情况都要成立;不回显 `feature_code` 原文,避免把内部编码抛给用户。
        logger.info("[Wallet] 扣费被拒(商品不可用): code=%s user=%s err=%s",
                    req.feature_code, user.get("user_id"), exc)
        raise HTTPException(
            status_code=400,
            detail="该功能商品当前不可用（已下架或不存在），本次未扣任何算力。"
                   "如需该能力，请在价目表中选择在售功能，或联系平台。",
        ) from exc
    wallet = get_wallet_balance(user["user_id"])
    return {
        "success": True,
        "deducted": result.get("deducted", 0),
        "remaining_paid": wallet.get("paid_points", 0),
        "remaining_bonus": wallet.get("bonus_points", 0),
    }


# ==================== v3.2 退款 ====================

class RefundRequest(BaseModel):
    order_id: str
    reason: str = ""
    reason_category: str = "negotiated_other"
    evidence: dict = Field(default_factory=dict)


def _require_external_refund_evidence_cur(cursor, order: dict) -> None:
    """Require trusted cash-refund evidence before admin reverses platform ledgers."""
    status = (order.get("refund_status") or "").strip().lower()
    if status not in ("pending_review", "channel_refunding"):
        return
    from db.refund_work_order_db import assert_external_refund_evidence_cur
    try:
        assert_external_refund_evidence_cur(
            cursor,
            str(order["id"]),
            status,
            order.get("refund_completed_at"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _db_now():
    """DB current time for legal-window and evidence calculations.
    原 datetime.utcnow()(naive UTC)比 DB 本地 paid_at(timestamp without time zone)慢 8h →
    退款窗口被放宽 ~8 小时,与对外'3 自然日'口径不一致。统一走 DB now() 不依赖容器时区。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT now()::timestamp AS now")
        row = cur.fetchone()
        return row["now"] if isinstance(row, dict) else row[0]
    finally:
        conn.close()


def _get_recharge_settlement_mode(order_id: str) -> str:
    """[2A-2 · Codex r2 P1] 查充值订单结算模式(退款分流用)· DB 异常 fail-closed 冒泡(不静默回落 direct 误路由)。
    订单不存在 → 404;DB 抖动 → 异常自然冒泡 → 调用方 500(admin 重试)· 不把 V3.5 单错分流到 legacy 被拒。
    """
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT settlement_mode FROM recharge_orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="订单不存在")
        return (row["settlement_mode"] if isinstance(row, dict) else row[0]) or "direct"
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def _v35_admin_refund(
    req: RefundRequest, user: dict, request: Request, *, dealer_consumer_resale: bool = False,
) -> dict:
    """[2A-2 方案A 修正版 2026-06-08 · 对抗审 wmlgiobzc 修 2 P0] V3.5 客户充值退款 admin 工单 → on_recharge_refund 平台内账反向。

    退平台内账(customer_credit 额度 + ledger 冲销/clawback + inventory 撤回)· 真款退付走人工(微信商户后台 / 线下转账 ·
    老板拍 A 纯人工 · /wallet/wechat-refund 状态机不兼容 V3.5 · 不调)· admin 核对后调 POST /api/wallet/refund/{order_id}/complete 标完成。
    _handle_v35_factory_refund 成功后【自标 refund_status='processed'】(referral_api:1238)· 即平台内账反向完成态;
    admin 核对真款退付后走【标完成端点】置 'completed'(工单制 · 人工把关 · 留证据链)。

    幂等(修对抗审 2 P0):
      - 【不预占位 pending】· 防"占位后崩溃 → 卡 pending 反向未做 → 重试被拒永久卡死"(P0-2)。崩溃则 refund_status 仍 NULL · 可重试 · _handle 单事务原子。
      - gate:refund_status 非 NULL 且非 rejected/failed 一律拒(含 'processed' · 防 C/D 二次提交穿透守卫重入双 clawback · P0-1)。
      - _handle_v35_factory_refund 入口幂等闸(referral_api · 配合 FOR UPDATE ledger)对 A/B/C/D 全防重入/并发。
      - 信号聚合(C/D clawback 不设 reversed_at)· platform_credit_reversed = 任一 reversed_at OR 存在 refund_clawback。
    """
    from db.connection import get_connection

    # 1. 校验(不占位 · gate 拒任何已发起退款态防重入)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, agent_user_id, amount_cents, payment_status, paid_at,
                   refund_status, refund_completed_at, settlement_mode,
                   now()::timestamp AS db_now
            FROM recharge_orders WHERE id = %s FOR UPDATE
        """, (req.order_id,))
        order = cursor.fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        if order["payment_status"] != "paid":
            raise HTTPException(status_code=400, detail="订单未支付,无需退款")
        # gate(P0-1):任何已发起/已处理退款态拒(含 'processed'/'pending'/'approved'/'completed')· 仅 NULL/rejected/failed 可发起
        # [P0-14 死锁修 2026-07-12] 'pending_review' 语义="渠道已退现金·待人工冲内账",应视为【可发起内账反向】
        #   而非"已发起退款"。不加白名单 → 虎皮椒 CD/RD 单进 pending_review 后工单永久卡 400(FORCE_XUNHUPAY 现状可达)。
        #   _v35_admin_refund 只反向平台内账(不退现金·现金渠道已退),故此处放行不会双退现金。
        # [v12 item2] pending_review/channel_refunding 均须先有可信 CD 或管理员实际退款凭证。
        #   channel_refunding 只有人工凭证可越过；pending_review 不能仅凭状态字符串猜现金已退。
        _require_external_refund_evidence_cur(cursor, order)
        if order["refund_status"] is not None and order["refund_status"] not in (
            "rejected", "failed", "pending_review", "channel_refunding"
        ):
            raise HTTPException(status_code=400, detail=f"该订单已有退款记录(状态 {order['refund_status']})· 不可重复发起")
        paid_at = order["paid_at"]
        if not paid_at:
            raise HTTPException(status_code=400, detail="订单支付时间异常")
        if dealer_consumer_resale:
            if order.get("settlement_mode") != "dealer_consumer_resale":
                raise HTTPException(409, detail="消费者逐级转售退款路由与订单结算模式不一致")
            cursor.execute(
                """SELECT c.status,c.consumer_user_id,c.responsible_service_user_id
                   FROM consumer_refund_cases c
                   JOIN dealer_consumer_sales s ON s.order_id=c.source_order_id
                   WHERE c.source_order_id=%s
                     AND c.consumer_user_id=s.consumer_user_id
                     AND c.responsible_service_user_id=s.seller_user_id
                   FOR UPDATE OF c""",
                (str(req.order_id),),
            )
            consumer_case = cursor.fetchone()
            if (
                not consumer_case
                or consumer_case["status"] != "platform_execution"
                or int(consumer_case["consumer_user_id"]) != int(order["user_id"])
                or int(consumer_case["responsible_service_user_id"])
                   != int(order.get("agent_user_id") or 0)
            ):
                raise HTTPException(
                    409,
                    detail={
                        "code": "CONSUMER_REFUND_SERVICE_REVIEW_REQUIRED",
                        "message": "协商退款必须完成本单售后审核，不能只提交 signoff 布尔值",
                    },
                )
        target_user_id = order["user_id"]
        amount_cents = order["amount_cents"]
        conn.commit()  # 释放 FOR UPDATE(校验完 · 不占位 · 防并发靠 _handle FOR UPDATE ledger + 入口幂等闸)
    finally:
        conn.close()

    # 2. 直接调 _handle_v35_factory_refund 平台内账反向(Codex r2 P0:跳过 on_recharge_refund 的吞异常包装 ·
    #    V3.5 路径本不跑 V3.2/V3.3.1 hook)· 异常冒泡 raise 500 防 fail-open(否则 _handle 抛=额度没退但返 success)。
    #    自管事务 · 4 状态机 · 入口幂等闸防重入/并发 · 成功自标 refund_status='processed'。
    try:
        from api.referral_api import _handle_v35_factory_refund
        _handle_v35_factory_refund(req.order_id, refund_reason=req.reason or "admin_v35_refund")
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"[Refund 2A-2] V3.5 平台内账反向失败 order={req.order_id}: {e}")
        raise HTTPException(status_code=500, detail=f"平台内账反向失败 · 请查日志: {str(e)[:200]}")

    # 3. 聚合查 ledger 实况 + refund_status 真值(C/D clawback 不设 reversed_at → reversed OR clawback)
    platform_reversed = False
    manual_review = False
    final_status = "unknown"
    conn2 = get_connection()
    try:
        cur2 = conn2.cursor()
        cur2.execute("""
            SELECT BOOL_OR(reversed_at IS NOT NULL) AS any_reversed,
                   BOOL_OR(source = 'refund_clawback') AS has_clawback,
                   BOOL_OR(manual_review_required) AS any_manual
            FROM agent_revenue_ledger WHERE recharge_order_id = %s
        """, (req.order_id,))
        lr = cur2.fetchone()
        if lr:
            platform_reversed = bool(lr["any_reversed"]) or bool(lr["has_clawback"])
            manual_review = bool(lr["any_manual"])
        cur2.execute("SELECT refund_status FROM recharge_orders WHERE id = %s", (req.order_id,))
        sr = cur2.fetchone()
        if sr and sr["refund_status"]:
            final_status = sr["refund_status"]
    except Exception as e:
        logger.warning(f"[Refund 2A-2] 查 ledger/status 实况失败 order={req.order_id}: {e}")
    finally:
        conn2.close()

    # 3.5 后置断言(Codex r2 P0):平台内账未达 processed/未反向 → raise 500 · 不写 success audit
    #     防 _handle 写半截(ledger 没真改)或查询异常被吞 → admin 误信 success 真款退付后白嫖。
    if final_status != "processed" or not platform_reversed:
        logger.error(f"[Refund 2A-2] 后置断言失败 order={req.order_id} status={final_status} reversed={platform_reversed}")
        raise HTTPException(status_code=500, detail=(
            f"平台内账未达 processed 态(status={final_status} reversed={platform_reversed})· "
            "查 agent_revenue_ledger 实况"
        ))

    # 4. audit 留痕(操作人 + 退款依据 + 反向实况 · 真 IP)· 断言通过后才写 success
    try:
        from db.auth_db import create_audit_log
        _ip = None
        try:
            _ip = request.client.host if (request and getattr(request, "client", None)) else None
        except Exception:
            pass
        create_audit_log(
            user_id=user.get("user_id"), username=user.get("username"),
            action="admin_refund_v35", module="wallet", entity_type="recharge_order",
            entity_id=int(req.order_id) if str(req.order_id).isdigit() else None,
            summary=(f"V3.5 admin 退款工单 order={req.order_id} target={target_user_id} "
                     f"¥{amount_cents / 100:.2f} status={final_status} reversed={platform_reversed} "
                     f"manual_review={manual_review} reason_category={req.reason_category}"),
            after={"order_id": str(req.order_id), "target_user_id": target_user_id,
                   "refund_status": final_status, "platform_credit_reversed": platform_reversed,
                   "manual_review": manual_review, "reason": req.reason,
                   "reason_category": req.reason_category, "evidence": req.evidence},
            ip_address=_ip,
        )
    except Exception as e:
        logger.error(f"[Refund 2A-2] audit log 写入失败 order={req.order_id}: {e}")

    logger.info(f"[Refund 2A-2] V3.5 admin refund order={req.order_id} status={final_status} "
                f"reversed={platform_reversed} manual_review={manual_review}")
    return {
        "success": True, "order_id": req.order_id, "v35_refund": True, "status": final_status,
        "platform_credit_reversed": platform_reversed, "manual_review": manual_review,
        "message": (("平台内额度/收益已冲销 · 真款退付 admin 另办后标完成"
                     if platform_reversed else "已受理 · 平台内账冲销待核对(请查台账)")
                    + (" · ⚠️ 已部分消费/已结算需人工复核冲销比例" if manual_review else "")),
    }


class RefundCompleteRequest(BaseModel):
    note: str = ""


@router.post("/refund/{order_id}/complete")
async def admin_complete_refund(order_id: str, req: RefundCompleteRequest, request: Request):
    """[2A-2 对抗审 must_fix#1 闭环] admin 标记 V3.5 退款工单完成(真款退付已线下/微信办理后)。
    refund_status 'processed'(平台内账已反向)→ 'completed' · 仅 admin · 留 audit 证据链(老板"人工标完成")。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if not user.get("is_admin", False):
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    from db.connection import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, refund_status FROM recharge_orders WHERE id = %s FOR UPDATE", (order_id,))
        order = cursor.fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        if order["refund_status"] != "processed":
            raise HTTPException(status_code=400,
                detail=f"仅平台内账已冲销(processed)的退款可标完成 · 当前状态 {order['refund_status']}")
        # [审核 P1 · P0-2 快照不可变] 退款元信息写 settlement_snapshot_jsonb,不再改 pricing_snapshot_jsonb
        #   (原始定价证据 write-once · §9.3)。settlement_snapshot 承载所有下单后证据(结算/退款)。
        cursor.execute("""
            UPDATE recharge_orders
            SET refund_status = 'completed',
                settlement_snapshot_jsonb = COALESCE(settlement_snapshot_jsonb, '{}'::jsonb)
                    || jsonb_build_object('refund_completed_at', NOW()::text, 'refund_complete_note', %s)
            WHERE id = %s
        """, (req.note or "", order_id))
        # [v10 item6] processed→completed:canonical 冲销渠道收益(V3.5 内账路径通常已在 processed 阶段冲销·此处幂等兜底)·原子提交
        from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund
        reverse_channel_revenue_on_refund(cursor, order_id)
        conn.commit()
    finally:
        conn.close()
    try:
        from db.auth_db import create_audit_log
        _ip = None
        try:
            _ip = request.client.host if (request and getattr(request, "client", None)) else None
        except Exception:
            pass
        create_audit_log(
            user_id=user.get("user_id"), username=user.get("username"),
            action="admin_refund_v35_complete", module="wallet", entity_type="recharge_order",
            entity_id=int(order_id) if str(order_id).isdigit() else None,
            summary=f"V3.5 退款工单标完成 order={order_id}(真款退付已办)note={req.note!r}",
            after={"order_id": str(order_id), "note": req.note}, ip_address=_ip,
        )
    except Exception as e:
        logger.error(f"[Refund 2A-2] complete audit log 写入失败 order={order_id}: {e}")
    logger.info(f"[Refund 2A-2] order={order_id} 标完成(completed)by admin={user.get('user_id')}")
    return {"success": True, "order_id": order_id, "status": "completed"}


@router.post("/refund")
async def request_refund(req: RefundRequest, request: Request):
    """Create a refund request against the original order and direct seller.

    Mandatory consumer reasons bypass service rejection. Negotiated requests
    require the direct service provider's approval. Cash completion is proven
    separately by the original payment route; this endpoint never reports a
    queued provider refund as completed.
    """
    from datetime import datetime, timedelta
    from db.connection import get_connection
    from db.wallet_db import insert_transaction

    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    user_id = user["user_id"]
    is_admin = user.get("is_admin", False)

    # Dealer-resale inventory has its own whole-lot/one-hop refund state machine.
    # Never let it fall through to the legacy agent_inventory_prepay clawback,
    # which has no lot provenance and would not restore the direct seller's FIFO.
    _resale_probe_conn = get_connection()
    try:
        from services import dealer_inventory_resale

        if dealer_inventory_resale.has_resale_order(_resale_probe_conn.cursor(), req.order_id):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "DEALER_RESALE_REFUND_ROUTE_REQUIRED",
                    "message": (
                        "逐级库存转售订单必须使用 /api/dealer-resale/orders/{order_id}/refund "
                        "冻结本跳完整批次；禁止走旧进货退款链"
                    ),
                },
            )
    finally:
        _resale_probe_conn.close()

    # [2A-2 2026-06-08] V3.5 客户充值退款 → on_recharge_refund 反向链(平台内账)· 前置分流
    # 现状:V3.5 客户 user_wallets 余额 0 → 下方 legacy 逻辑必 400 拒退(safe-fail)· 这里前置走对链路。
    _v35_mode = _get_recharge_settlement_mode(req.order_id)
    if _v35_mode == "dealer_consumer_resale":
        from services import dealer_inventory_resale as _resale
        _conn = get_connection()
        try:
            _cur = _conn.cursor()
            _cur.execute("SELECT user_id FROM recharge_orders WHERE id=%s FOR UPDATE", (req.order_id,))
            _owner = _cur.fetchone()
            if not _owner:
                raise HTTPException(404, "订单不存在")
            if not is_admin and int(_owner["user_id"]) != int(user_id):
                raise HTTPException(403, "只能申请自己的原订单退款")
            _case = _resale.request_consumer_refund_case(
                _cur, order_id=req.order_id, consumer_user_id=int(_owner["user_id"]),
                reason=req.reason, reason_category=req.reason_category, evidence=req.evidence,
            )
            if _case.get("status") == "platform_execution":
                _case = _resale.settle_consumer_refund_case(_cur, case_id=str(_case["case_id"]))
            _visible = _resale.get_consumer_refund_visible(
                _cur, case_id=str(_case["case_id"]), actor_user_id=int(_owner["user_id"]),
                is_admin=is_admin,
            )
            _conn.commit()
            return {"success": True, "refund": _visible, "cash_completed": False}
        except _resale.ResaleError as exc:
            _conn.rollback()
            if is_admin:
                detail = {"code": exc.code, "message": exc.message}
            else:
                detail = {
                    "code": "REFUND_REQUEST_NOT_AVAILABLE",
                    "message": "当前退款申请暂不能处理，请联系平台服务",
                }
            raise HTTPException(409, detail=detail) from exc
        finally:
            _conn.close()
    if _v35_mode in ("v35_inventory_settlement", "v35_platform_direct_settlement"):
        if not is_admin:
            # Legacy orders lack immutable direct-sale lots. Preserve the request
            # and evidence for platform handling instead of silently using the
            # old three-day/fixed-fee path or pretending cash moved.
            from db.refund_work_order_db import build_refund_order_preview, create_refund_work_order
            preview = build_refund_order_preview(req.order_id)
            impact = preview.get("impact") or {}
            payload = {
                "source_order_id": req.order_id,
                "customer_user_id": user_id,
                "agent_user_id": (preview.get("order") or {}).get("agent_user_id"),
                "refund_reason_category": req.reason_category,
                "refund_reason_detail": req.reason,
                "refund_method": "original_route_pending",
                "requested_refund_cents": int(impact.get("estimated_refund_cents") or 0),
                "estimated_refund_cents": int(impact.get("estimated_refund_cents") or 0),
                "refundable_power": int(impact.get("refundable_power") or 0),
                "customer_requested_at": None,
                "agent_confirmed_at": None,
                "impact_snapshot": {**preview, "customer_evidence": req.evidence},
            }
            work_order = create_refund_work_order(payload, user_id, status="submitted")
            return {"success": True, "work_order_id": work_order["id"], "status": "submitted", "cash_completed": False}
        return await _v35_admin_refund(
            req, user, request,
            dealer_consumer_resale=False,
        )
    if _v35_mode == "v35_platform_direct_settlement":
        if not is_admin:
            raise HTTPException(
                status_code=403,
                detail="平台不提供自助退款 · 未消耗算力之退还请联系客服核验办理",
            )
        return await _v35_admin_refund(req, user, request)

    # [BUG-P1] 预付进货(agent_inventory_prepay)退款:专用库存反向链,不走下方 legacy 误扣 user_wallets(退错池)
    if _v35_mode == "agent_inventory_prepay":
        if not is_admin:
            raise HTTPException(status_code=403, detail="平台不提供自助退款 · 请联系客服核验办理")
        from services.agent_inventory import refund_inventory_prepay
        _conn = get_connection()
        try:
            _cur = _conn.cursor()
            _cur.execute("""
                SELECT id, user_id, base_points, bonus_points, payment_status, paid_at,
                       refund_status, refund_completed_at
                FROM recharge_orders WHERE id = %s FOR UPDATE
            """, (req.order_id,))
            _o = _cur.fetchone()
            if not _o:
                raise HTTPException(status_code=404, detail="订单不存在")
            if _o["payment_status"] != "paid":
                raise HTTPException(status_code=400, detail="订单未支付，无需退款")
            _require_external_refund_evidence_cur(_cur, _o)
            if _o["refund_status"] in ("pending", "approved", "completed"):
                raise HTTPException(status_code=400, detail="该订单已有退款申请")
            if not _o["paid_at"]:
                raise HTTPException(status_code=400, detail="订单支付时间异常")
            _r = refund_inventory_prepay(
                _cur, agent_user_id=_o["user_id"],
                paid_points=_o["base_points"] or 0, bonus_points=_o["bonus_points"] or 0,
                related_order_id=req.order_id, description=f"预付进货退款扣回 order={req.order_id}",
            )
            if not _r.get("success"):
                _conn.commit()  # 库存不足(已划拨)→ 不改订单状态,返回 manual_review 供 admin 人工核对
                return {"success": False, "manual_review": True, "order_id": req.order_id,
                        "message": _r.get("reason")}
            _cur.execute(
                "UPDATE recharge_orders SET refund_status='pending', refund_requested_at=NOW() WHERE id=%s",
                (req.order_id,))
            _conn.commit()
            return {"success": True, "order_id": req.order_id,
                    "refunded_paid": _r["refunded_paid"], "refunded_bonus": _r["refunded_bonus"],
                    "message": "已从代理库存扣回未划拨进货积分(真款退付走人工)"}
        except HTTPException:
            _conn.rollback(); raise
        except Exception:
            _conn.rollback(); logger.exception(f"[Wallet] 预付进货退款失败 order={req.order_id}"); raise
        finally:
            _conn.close()

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 1. 查订单（FOR UPDATE 防并发重复退款）
        cursor.execute("""
            SELECT id, user_id, amount_cents, base_points, bonus_points,
                   payment_status, paid_at, refund_status, refund_deadline,
                   commission_version, refund_completed_at
            FROM recharge_orders
            WHERE id = %s
            FOR UPDATE
        """, (req.order_id,))
        order = cursor.fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")

        if not is_admin and int(order["user_id"]) != int(user_id):
            raise HTTPException(status_code=403, detail="只能申请自己的原订单退款")

        if order["payment_status"] != "paid":
            raise HTTPException(status_code=400, detail="订单未支付，无需退款")

        if not is_admin:
            # Legacy orders do not have the immutable direct-sale lot evidence
            # needed for an automatic reversal. Accept the customer's request,
            # preserve it durably, and move no money/credits until reviewed.
            from db.refund_work_order_db import build_refund_order_preview, create_refund_work_order
            preview = build_refund_order_preview(req.order_id)
            impact = preview.get("impact") or {}
            payload = {
                "source_order_id": req.order_id,
                "customer_user_id": user_id,
                "agent_user_id": (preview.get("order") or {}).get("agent_user_id"),
                "refund_reason_category": req.reason_category,
                "refund_reason_detail": req.reason,
                "refund_method": "original_route_pending",
                "requested_refund_cents": int(impact.get("estimated_refund_cents") or 0),
                "estimated_refund_cents": int(impact.get("estimated_refund_cents") or 0),
                "refundable_power": int(impact.get("refundable_power") or 0),
                "customer_requested_at": _db_now(),
                "agent_confirmed_at": None,
                "impact_snapshot": {**preview, "customer_evidence": req.evidence},
            }
            try:
                work_order = create_refund_work_order(payload, user_id, status="submitted")
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {
                "success": True, "work_order_id": work_order["id"],
                "status": "submitted", "cash_completed": False,
            }

        _require_external_refund_evidence_cur(cursor, order)
        if order["refund_status"] in ("pending", "approved", "completed"):
            raise HTTPException(status_code=400, detail="该订单已有退款申请")

        if order["commission_version"] == "legacy":
            raise HTTPException(status_code=400, detail="老订单不支持自助退款，请联系客服")

        # 2. DB clock anchors evidence. Mandatory reasons are not refused by a
        # fixed product window; negotiated cases must have been approved first.
        paid_at = order["paid_at"]
        if not paid_at:
            raise HTTPException(status_code=400, detail="订单支付时间异常")

        # 3. 计算未消耗积分（简化版：支付后消费总和）
        total_order_points = (order["base_points"] or 0) + (order["bonus_points"] or 0)
        # [BUG-P0-2] consume 流水 amount 恒负(prod 实证 953/953 全负),原 SUM(amount) 得负数,
        # total_order_points - 负数 = 加法 → unused 随消费膨胀(消费越多退越多,可超原单)。
        # 修:SUM(ABS(amount)) 取真实消费总量(正);unused 再加订单总额上限(fail-closed 双保险)。
        cursor.execute("""
            SELECT COALESCE(SUM(ABS(amount)), 0)::bigint AS consumed
            FROM point_transactions
            WHERE user_id = %s AND type = 'consume'
              AND created_at > %s
        """, (order["user_id"], paid_at))
        consumed_row = cursor.fetchone()
        consumed_since = int(consumed_row["consumed"] or 0)

        # [BUG-P0-2] 单点权威计算(unused 夹 [0,total] · ratio 夹 [0,1])
        unused_points, unused_ratio = _compute_refund_unused_ratio(total_order_points, consumed_since)

        if unused_points == 0:
            raise HTTPException(status_code=400, detail="该订单积分已全部使用，不可退款")

        # 4. 按比例计算退款金额(unused_ratio 已夹紧 ≤1.0)
        refundable_cents = int(order["amount_cents"] * unused_ratio)
        refund_amount_cents = refundable_cents
        refund_amount_yuan = refund_amount_cents / 100

        # 5. 检查用户余额是否够扣（避免负余额）
        cursor.execute("""
            SELECT paid_points, bonus_points FROM user_wallets WHERE user_id = %s FOR UPDATE
        """, (order["user_id"],))
        wallet = cursor.fetchone()
        total_balance = (wallet["paid_points"] or 0) + (wallet["bonus_points"] or 0)

        if total_balance < unused_points:
            raise HTTPException(
                status_code=400,
                detail=f"当前余额 {total_balance} 不足以扣除未使用积分 {unused_points}，请联系客服"
            )

        # 6. 扣代理 pending 佣金
        cursor.execute("""
            UPDATE pending_commissions
            SET status = 'refunded_cancelled', settled_at = NOW()
            WHERE order_id = %s AND status = 'pending'
            RETURNING id, user_id, amount_yuan, level
        """, (req.order_id,))
        cancelled_pending = cursor.fetchall()

        # 7. 扣代理已 settled 的佣金（如存在）
        cursor.execute("""
            SELECT id, user_id, amount_yuan, level FROM pending_commissions
            WHERE order_id = %s AND status = 'settled'
        """, (req.order_id,))
        settled_to_clawback = cursor.fetchall()

        clawback_failed = []
        for comm in settled_to_clawback:
            from config.pricing_config import get_points_per_yuan as _ppy
            points_to_deduct = int(float(comm["amount_yuan"]) * _ppy())  # [P0-13] SSOT 汇率(默认130·非硬编码二源)
            # 尝试从代理 paid_points 扣回
            cursor.execute("""
                UPDATE user_wallets
                SET paid_points = paid_points - %s, updated_at = NOW()
                WHERE user_id = %s AND paid_points >= %s
                RETURNING paid_points
            """, (points_to_deduct, comm["user_id"], points_to_deduct))
            row = cursor.fetchone()

            if row:
                # 扣回成功
                cursor.execute("""
                    UPDATE pending_commissions
                    SET status = 'refund_clawback', settled_at = NOW()
                    WHERE id = %s
                """, (comm["id"],))
                insert_transaction(
                    cursor, comm["user_id"], "commission_clawback", "paid",
                    -points_to_deduct, row["paid_points"],
                    description=f"订单 {req.order_id} 退款追回 L{comm['level']} 佣金"
                )
            else:
                # 代理余额不足，平台承担损失
                clawback_failed.append(comm["id"])
                cursor.execute("""
                    UPDATE pending_commissions
                    SET status = 'refund_clawback', settled_at = NOW(),
                        frozen_reason = 'clawback_failed_insufficient_balance'
                    WHERE id = %s
                """, (comm["id"],))
                logger.warning(
                    f"[Refund] 代理 {comm['user_id']} 余额不足，平台承担佣金损失 ¥{comm['amount_yuan']}"
                )

        # 8. 扣用户未使用积分（优先扣 paid，不够扣 bonus）
        remaining_to_deduct = unused_points
        paid_deduct = min(wallet["paid_points"] or 0, remaining_to_deduct)
        bonus_deduct = remaining_to_deduct - paid_deduct

        cursor.execute("""
            UPDATE user_wallets
            SET paid_points = paid_points - %s,
                bonus_points = bonus_points - %s,
                updated_at = NOW()
            WHERE user_id = %s
            RETURNING paid_points, bonus_points
        """, (paid_deduct, bonus_deduct, order["user_id"]))
        new_wallet = cursor.fetchone()

        insert_transaction(
            cursor, order["user_id"], "refund_deduction", "paid",
            -paid_deduct, new_wallet["paid_points"],
            order_id=req.order_id,
            description=f"退款扣除未使用积分 ¥{refund_amount_yuan:.2f}"
        )
        if bonus_deduct > 0:
            insert_transaction(
                cursor, order["user_id"], "refund_deduction", "bonus",
                -bonus_deduct, new_wallet["bonus_points"],
                order_id=req.order_id,
                description=f"退款扣除未使用赠送积分 {bonus_deduct}"
            )

        # 9. 更新订单状态（实际退款在后端异步调微信接口）
        cursor.execute("""
            UPDATE recharge_orders
            SET refund_status = 'pending',
                refund_requested_at = NOW(),
                refunded_amount_cents = %s
            WHERE id = %s
        """, (refund_amount_cents, req.order_id))

        # 2026-04-18 v3.4: 触发 referral_bonus fraud_flag（不追回，只打标记给运营）
        try:
            cursor.execute("""
                UPDATE referral_bonus_records
                SET fraud_flag = TRUE,
                    refund_linked_order_id = %s
                WHERE order_id = %s
            """, (req.order_id, req.order_id))
            _flagged = cursor.rowcount
            if _flagged:
                logger.warning(
                    f"[Refund→ReferralBonus] order={req.order_id} 触发 {_flagged} "
                    f"笔 referral_bonus fraud_flag（不追回，仅标记）"
                )
        except Exception as e:
            logger.warning(f"[Refund→ReferralBonus] 标记失败（忽略）: {e}")

        conn.commit()

        # TODO: 异步调微信/支付宝退款 API
        # 目前返回 pending，需运营审核后人工 refund_status = 'completed'
        logger.info(
            f"[Refund] admin={user_id} order={req.order_id} target_user={order['user_id']} "
            f"amount=¥{refund_amount_yuan:.2f} unused={unused_points} "
            f"cancelled_pending={len(cancelled_pending)} "
            f"clawback={len(settled_to_clawback)} clawback_failed={len(clawback_failed)} "
            f"reason_category={req.reason_category}"
        )

        # 2026-06-06 服务商模型 P0:admin 退款留痕(操作人 + 服务商 sign-off + 同系数原路实付)
        try:
            from db.auth_db import create_audit_log
            create_audit_log(
                user_id=user.get("user_id"),
                username=user.get("username"),
                action="admin_refund",
                module="wallet",
                entity_type="recharge_order",
                entity_id=int(req.order_id) if str(req.order_id).isdigit() else None,
                summary=(
                    f"退款 ¥{refund_amount_yuan:.2f}(未消耗 {unused_points} · 原订单原路) · "
                    f"目标用户 {order['user_id']} · reason={req.reason_category}"
                ),
                after={
                    "order_id": req.order_id,
                    "target_user_id": order["user_id"],
                    "refund_amount_yuan": refund_amount_yuan,
                    "unused_points": unused_points,
                    "reason_category": req.reason_category,
                    "evidence": req.evidence,
                    "reason": req.reason,
                },
            )
        except Exception:
            pass

        return {
            "success": True,
            "order_id": req.order_id,
            "refund_amount_yuan": refund_amount_yuan,
            "refund_amount_cents": refund_amount_cents,
            "unused_points": unused_points,
            "unused_ratio": round(unused_ratio, 4),
            "cancelled_pending_commissions": len(cancelled_pending),
            "clawback_settled_commissions": len(settled_to_clawback),
            "clawback_failed": len(clawback_failed),
            "status": "pending_wechat_refund",
            "message": f"退款申请已受理，¥{refund_amount_yuan:.2f} 将在 3-7 工作日内到账",
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logger.exception(f"[Refund] 处理失败: {e}")
        raise HTTPException(status_code=500, detail=f"退款处理失败：{e}")
    finally:
        conn.close()


@router.get("/refund/eligibility/{order_id}")
async def check_refund_eligibility(order_id: str, request: Request):
    """Read refund facts for the order owner or admin; it never moves funds."""
    from datetime import datetime, timedelta
    from db.connection import get_connection

    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, amount_cents, base_points, bonus_points,
                   payment_status, paid_at, refund_status, commission_version, refund_completed_at
            FROM recharge_orders WHERE id = %s
        """, (order_id,))
        order = cursor.fetchone()

        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        if not user.get("is_admin", False) and int(order["user_id"]) != int(user["user_id"]):
            raise HTTPException(status_code=403, detail="只能查看自己的原订单退款资格")

        if order["payment_status"] != "paid":
            return {"eligible": False, "reason": "订单未支付"}

        if order["refund_status"] in ("pending_review", "channel_refunding"):
            try:
                _require_external_refund_evidence_cur(cursor, order)
            except HTTPException as exc:
                return {"eligible": False, "reason": str(exc.detail)}
        elif order["refund_status"]:
            return {"eligible": False, "reason": f"订单已申请退款（{order['refund_status']}）"}

        if order["commission_version"] == "legacy":
            return {"eligible": False, "reason": "老订单不支持自助退款"}

        paid_at = order["paid_at"]
        seven_day_deadline = paid_at + timedelta(days=7)
        now = _db_now()

        # 估算未使用积分(admin 核验任意订单 → 按订单归属用户 order["user_id"],非操作的 admin)
        total_order_points = (order["base_points"] or 0) + (order["bonus_points"] or 0)
        # [BUG-P0-2] 同 request_refund:consume amount 恒负 → SUM(ABS) 取真实消费量 + unused 上限
        cursor.execute("""
            SELECT COALESCE(SUM(ABS(amount)), 0)::bigint AS consumed
            FROM point_transactions
            WHERE user_id = %s AND type = 'consume' AND created_at > %s
        """, (order["user_id"], paid_at))
        consumed = int(cursor.fetchone()["consumed"] or 0)
        # [BUG-P0-2] 单点权威计算(与 request_refund 同口径)
        unused, unused_ratio = _compute_refund_unused_ratio(total_order_points, consumed)

        if unused == 0:
            return {"eligible": False, "reason": "积分已全部使用"}

        estimated_refund_cents = int(order["amount_cents"] * unused_ratio)

        return {
            "eligible": True,
            "total_order_points": total_order_points,
            "unused_points": unused,
            "unused_ratio": round(unused_ratio, 4),
            "estimated_refund_yuan": estimated_refund_cents / 100,
            "within_seven_day_prepayment_window": now <= seven_day_deadline,
            "seven_day_deadline": seven_day_deadline.isoformat(),
            "seconds_remaining": max(0, int((seven_day_deadline - now).total_seconds())),
            "fixed_consumer_fee_bps": 0,
        }
    finally:
        conn.close()


# ==================== v3.2 佣金查询 ====================

@router.get("/commissions")
async def list_commissions(
    request: Request,
    status: str = "pending",
    page: int = 1,
    limit: int = 20,
):
    """分页查佣金列表"""
    from db.connection import get_connection

    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    user_id = user["user_id"]

    if status not in ("pending", "settled", "frozen", "refunded_cancelled", "refund_clawback", "all"):
        raise HTTPException(status_code=400, detail="无效的 status 参数")

    offset = (max(page, 1) - 1) * limit

    conn = get_connection()
    try:
        cursor = conn.cursor()
        if status == "all":
            cursor.execute("""
                SELECT pc.id, pc.source_user_id, pc.order_id, pc.amount_yuan,
                       pc.level, pc.status, pc.created_at, pc.available_at,
                       pc.settled_at, pc.frozen_reason,
                       u.username AS source_username, u.display_name AS source_display
                FROM pending_commissions pc
                LEFT JOIN users u ON u.id = pc.source_user_id
                WHERE pc.user_id = %s
                ORDER BY pc.created_at DESC
                LIMIT %s OFFSET %s
            """, (user_id, limit, offset))
        else:
            cursor.execute("""
                SELECT pc.id, pc.source_user_id, pc.order_id, pc.amount_yuan,
                       pc.level, pc.status, pc.created_at, pc.available_at,
                       pc.settled_at, pc.frozen_reason,
                       u.username AS source_username, u.display_name AS source_display
                FROM pending_commissions pc
                LEFT JOIN users u ON u.id = pc.source_user_id
                WHERE pc.user_id = %s AND pc.status = %s
                ORDER BY pc.created_at DESC
                LIMIT %s OFFSET %s
            """, (user_id, status, limit, offset))

        rows = cursor.fetchall()
        records = []
        for r in rows:
            # 脱敏：来源用户名显示为"用户 ***xxx"
            src_name = r.get("source_display") or r.get("source_username") or ""
            if len(src_name) > 3:
                src_name = f"***{src_name[-2:]}"
            records.append({
                "id": r["id"],
                "source_user_name": src_name,
                "order_id": r["order_id"],
                "amount_yuan": float(r["amount_yuan"]),
                "level": r["level"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "available_at": r["available_at"].isoformat() if r["available_at"] else None,
                "settled_at": r["settled_at"].isoformat() if r["settled_at"] else None,
                "frozen_reason": r["frozen_reason"],
            })

        return {"records": records, "page": page, "limit": limit}
    finally:
        conn.close()
