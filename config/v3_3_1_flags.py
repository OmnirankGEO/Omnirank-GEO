"""
V3.3.1 身份模型 feature flags + 配置参数

读取顺序(优先级从高到低):
1. 环境变量(`V3_3_1_*` 直接 env 覆盖 · 紧急回滚)
2. system_settings 表(灰度期 admin 可调 · 不重启)
3. 模块默认值(代码硬编码兜底 · 全部 OFF)

设计原则:
- 默认全部 OFF · 不影响生产
- helper 函数容错 · DB 异常时 fallback 默认值 · 不阻塞核心扣费
- 缓存 30 秒 · 防高频读 DB

关联文档:
- docs/DECISION/身份模型V3_C端社媒_GEO邀请制_2026-05-11.md
- .planning/phases/07-social-studio-subscription/PRICING/IDENTITY_DECISIONS_LOCK.md

注意:
- 任何"是否启用 V3.3.1 新逻辑"判断都必须走 `is_enabled(...)` · 禁直接 import 默认值
"""

import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("GEO-V3.3.1-Flags")

# ============================================
# 默认值(硬编码兜底 · 全部 OFF 不影响生产)
# ============================================

_FLAG_DEFAULTS: Dict[str, Any] = {
    # 总开关
    "V3_3_1_ENABLED": False,
    # 行为开关
    "V3_3_1_DUAL_WRITE_OLD_TABLE": True,           # 灰度期双写老表
    "V3_3_1_NET_CASH_REVENUE_BASE": False,         # 净现金基数
    "V3_3_1_SERVICE_FEE_CONVERSION_ENABLED": False,
    "V3_3_1_WITHDRAWAL_ENABLED": False,
    "V3_3_1_RBAC_GATE_ENABLED": False,
    "V3_3_1_INVITE_CODE_REQUIRED": False,
    # [报价系数第二步·2026-06-07] 无 owner 的 L0 普通用户是否可自设报价覆盖系数
    #   默认 False:无 owner 的孤立 L0 报价回落系统默认(防历史残留 quote_markup_ratio 脏数据被读出生效)
    #   True(admin 后台 system_settings / env 开):无 owner L0 自设系数生效
    #   故意【不带 V3_3_1_ 前缀】→ 走 get_flag 读取(env>DB>default)· 与 V3.3.1 总开关解耦
    "L0_QUOTE_MARKUP_SELF_EDIT_ENABLED": False,
    # [渠道激励全量 · 2026-06-28] 客户 SKU 默认零售价兜底开关。
    # 默认 False:没有 agent override 时保持旧逻辑(零售价回落 wholesale_cents),零回归。
    # True:优先 suggested_retail_cents;缺建议价时按 pricing_config.k_default 与真实进货系数推导默认零售价。
    "V35_SKU_K_DEFAULT_ENABLED": False,
    # Codex 四审 P1-2:V3.3.1 启用前必须明确决定 3888 立即发还是分阶段
    # default true(总开关 OFF 时不生效)· 总开关 ON 时启用 → grant_trial_bonus skip 即时发
    # 等分阶段释放任务实施完(决策书 §5.5)· 此 flag 可改 false 恢复即时发
    "V3_3_1_DISABLE_INSTANT_TRIAL_BONUS": True,
    # Codex 五审 P0-1:cron ROLE gate 必须在 _FLAG_DEFAULTS · 否则 get_flag 走 unknown key 路径返 None
    # any → 所有容器都注册(默认 · 兼容老行为)
    # primary → 只 ROLE=primary 容器注册(蓝绿规范 · 防 V3.3.1 cron 双跑)
    "V3_3_1_CRON_ROLE_GATE": "any",
    # 参数
    "service_fee_rate": 0.22,
    "service_fee_settle_days": 3,
    "referral_bonus_rate": 0.15,
    "referral_bonus_settle_days": 7,
    "service_fee_conversion_bonus_rate": 0.20,
    "service_fee_conversion_quota_default": 5000.0,
    "service_fee_conversion_quota_premium": 20000.0,
    "service_fee_conversion_min_age_days": 7,
    "service_fee_withdrawal_min_amount": 100.0,
    "service_fee_withdrawal_max_per_week": 1,
    "service_fee_withdrawal_invoice_threshold": 800.0,
    "service_fee_withdrawal_dual_sign_threshold": 1000.0,
    "l1_monthly_invite_quota": 10,
}

# ============================================
# 缓存(30 秒 TTL · 防高频读 DB)
# ============================================

_cache: Dict[str, tuple[Any, float]] = {}
_CACHE_TTL_SECONDS = 30


def _coerce(value: Any, value_type: str) -> Any:
    """按 value_type 强转类型 · 兼容 DB 字符串"""
    if value is None:
        return None
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    return str(value)


def _read_from_env(key: str, default: Any) -> Optional[Any]:
    """从环境变量读 · 命名约定 `V3_3_1_*` 或 `SERVICE_FEE_*`"""
    env_key = key.upper()
    raw = os.environ.get(env_key)
    if raw is None:
        return None
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return None
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return None
    return raw


def _read_from_db(key: str) -> Optional[Any]:
    """从 system_settings 表读 · DB 异常 fallback None"""
    try:
        # 延迟 import 防循环依赖
        from db.connection import get_db
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value, value_type FROM system_settings WHERE key = %s",
                    (key,)
                )
                row = cur.fetchone()
                if not row:
                    return None
                value = row["value"] if isinstance(row, dict) else row[0]
                value_type = row["value_type"] if isinstance(row, dict) else row[1]
                return _coerce(value, value_type or "string")
    except Exception as exc:
        logger.warning("v3_3_1_flags: DB read failed for %s · fallback default · %s", key, exc)
        return None


def get_flag(key: str) -> Any:
    """读取 flag(env → DB → default)· 30 秒缓存"""
    now = time.time()
    cached = _cache.get(key)
    if cached and (now - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    default = _FLAG_DEFAULTS.get(key)
    if default is None:
        logger.warning("v3_3_1_flags: unknown key %s · returning None", key)
        return None

    # 环境变量优先
    env_value = _read_from_env(key, default)
    if env_value is not None:
        _cache[key] = (env_value, now)
        return env_value

    # DB
    db_value = _read_from_db(key)
    if db_value is not None:
        _cache[key] = (db_value, now)
        return db_value

    # default
    _cache[key] = (default, now)
    return default


def is_enabled(flag_key: str) -> bool:
    """语义化:某 flag 是否启用"""
    if not flag_key.startswith("V3_3_1_"):
        logger.warning("is_enabled: 仅支持 V3_3_1_* flag · got %s", flag_key)
    value = get_flag(flag_key)
    return bool(value) if value is not None else False


def is_v3_3_1_enabled() -> bool:
    """总开关 · 任何新逻辑入口必查此"""
    return is_enabled("V3_3_1_ENABLED")


def is_dual_write_enabled() -> bool:
    """灰度期双写老表(默认 True · 切流后 admin 改 false)"""
    if not is_v3_3_1_enabled():
        return False
    return is_enabled("V3_3_1_DUAL_WRITE_OLD_TABLE")


def is_net_cash_revenue_base() -> bool:
    """净现金收入基数(§3.1.1)"""
    if not is_v3_3_1_enabled():
        return False
    return is_enabled("V3_3_1_NET_CASH_REVENUE_BASE")


def is_service_fee_conversion_enabled() -> bool:
    """服务费转积分功能"""
    if not is_v3_3_1_enabled():
        return False
    return is_enabled("V3_3_1_SERVICE_FEE_CONVERSION_ENABLED")


def is_withdrawal_enabled() -> bool:
    """人工提现申请功能"""
    if not is_v3_3_1_enabled():
        return False
    return is_enabled("V3_3_1_WITHDRAWAL_ENABLED")


def is_rbac_gate_enabled() -> bool:
    """GEO RBAC L0/L1/L2 gate"""
    if not is_v3_3_1_enabled():
        return False
    return is_enabled("V3_3_1_RBAC_GATE_ENABLED")


def is_invite_code_required() -> bool:
    """L1/L2 必须邀请码"""
    if not is_v3_3_1_enabled():
        return False
    return is_enabled("V3_3_1_INVITE_CODE_REQUIRED")


def is_instant_trial_bonus_disabled() -> bool:
    """Codex 四审 P1-2:V3.3.1 启用时是否禁止注册立即发 3888

    True(default · 总开关 ON 时生效)→ 注册不发 3888 · 等分阶段释放(决策书 §5.5)
    False → 维持老 V3.1 即时发 3888(已实施分阶段任务后才改 false)
    总开关 OFF 时永远 False(老 V3.x 行为不变)
    """
    if not is_v3_3_1_enabled():
        return False
    return is_enabled("V3_3_1_DISABLE_INSTANT_TRIAL_BONUS")


# ============================================
# 参数读取
# ============================================

def get_service_fee_rate() -> float:
    return float(get_flag("service_fee_rate") or 0.22)


def get_service_fee_settle_days() -> int:
    return int(get_flag("service_fee_settle_days") or 3)


def get_referral_bonus_rate() -> float:
    return float(get_flag("referral_bonus_rate") or 0.15)


def get_referral_bonus_settle_days() -> int:
    return int(get_flag("referral_bonus_settle_days") or 7)


def get_conversion_bonus_rate() -> float:
    return float(get_flag("service_fee_conversion_bonus_rate") or 0.20)


def get_conversion_quota_yuan(agent_tier: str = "standard") -> float:
    """月度转换配额 · 按等级返不同上限 · strategic = 无上限(返大数)"""
    tier = (agent_tier or "standard").lower()
    if tier == "strategic":
        return 10_000_000.0  # 实际不限 · 战略代理走特批
    if tier == "premium":
        return float(get_flag("service_fee_conversion_quota_premium") or 20000.0)
    return float(get_flag("service_fee_conversion_quota_default") or 5000.0)


def get_conversion_min_age_days() -> int:
    return int(get_flag("service_fee_conversion_min_age_days") or 7)


def get_withdrawal_min_amount() -> float:
    return float(get_flag("service_fee_withdrawal_min_amount") or 100.0)


def get_withdrawal_max_per_week() -> int:
    return int(get_flag("service_fee_withdrawal_max_per_week") or 1)


def get_withdrawal_invoice_threshold() -> float:
    return float(get_flag("service_fee_withdrawal_invoice_threshold") or 800.0)


def get_withdrawal_dual_sign_threshold() -> float:
    return float(get_flag("service_fee_withdrawal_dual_sign_threshold") or 1000.0)


def get_l1_monthly_invite_quota() -> int:
    return int(get_flag("l1_monthly_invite_quota") or 10)


# ============================================
# Source 字段枚举(防套利铁律 · §3.6)
# ============================================

class PaymentSource:
    """订单/流水的 source 字段值 · §3.6 7 场景"""
    EXTERNAL_CASH = "external_cash_payment"        # 微信/支付宝现金充值 · 唯一触发返佣
    BALANCE_DEDUCTION = "balance_deduction"        # 钱包内消费(paid/bonus/granted)
    SERVICE_FEE_CONVERSION = "service_fee_conversion"  # 服务费转出的 paid/bonus
    EXTERNAL_MEDIA_PURCHASE = "external_media_purchase"  # 媒体外采(平台不留毛利)


VALID_PAYMENT_SOURCES = {
    PaymentSource.EXTERNAL_CASH,
    PaymentSource.BALANCE_DEDUCTION,
    PaymentSource.SERVICE_FEE_CONVERSION,
    PaymentSource.EXTERNAL_MEDIA_PURCHASE,
}


def is_revenue_triggering_source(source: Optional[str]) -> bool:
    """判断该 source 是否触发服务费/推荐奖励"""
    return source == PaymentSource.EXTERNAL_CASH


# ============================================
# 异常退款 4 类(Q33 · §3.5.6)
# ============================================

class AnomalousRefundReason:
    """异常退款 4 类 · 触发已转换服务费追索"""
    LEGAL_DISPUTE = "legal_dispute"
    PLATFORM_FORCE = "platform_force"
    FRAUD = "fraud"
    CHARGEBACK = "chargeback"


ANOMALOUS_REFUND_REASONS = {
    AnomalousRefundReason.LEGAL_DISPUTE,
    AnomalousRefundReason.PLATFORM_FORCE,
    AnomalousRefundReason.FRAUD,
    AnomalousRefundReason.CHARGEBACK,
}


def is_anomalous_refund(reason: Optional[str]) -> bool:
    """判断退款 reason 是否触发追索(§3.5.6)"""
    return reason in ANOMALOUS_REFUND_REASONS


# ============================================
# 服务费状态枚举(8 态 · §3.2)
# ============================================

class ServiceFeeStatus:
    PENDING = "pending"                # 退款期内(冻结)
    SETTLED = "settled"                # 已过退款期 · 可处理
    CONVERTED = "converted"            # 已转充值积分(不可撤销)
    WITHDRAW_REQUESTED = "withdraw_requested"  # 已申请提现 · 等审核
    WITHDRAWN = "withdrawn"            # 已结算
    CANCELLED = "cancelled"            # 退款期内撤销
    CLAWBACK = "clawback"              # 已 settled 后退款 / 异常追索
    REJECTED = "rejected"              # 提现申请被拒(余额回 settled)


VALID_SERVICE_FEE_STATUSES = {
    ServiceFeeStatus.PENDING,
    ServiceFeeStatus.SETTLED,
    ServiceFeeStatus.CONVERTED,
    ServiceFeeStatus.WITHDRAW_REQUESTED,
    ServiceFeeStatus.WITHDRAWN,
    ServiceFeeStatus.CANCELLED,
    ServiceFeeStatus.CLAWBACK,
    ServiceFeeStatus.REJECTED,
}


def clear_cache() -> None:
    """admin 改配置后调用清缓存(可选)"""
    _cache.clear()
    logger.info("v3_3_1_flags cache cleared")
