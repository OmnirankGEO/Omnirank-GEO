"""
V3.5 代理白标 SKU 定价服务

职责:
- 以 agent_sku_overrides 的 canonical 零售行计算客户看到的零售价
- sku_templates 只作为可选的一次性预填来源，不参与零售身份与结算
- 算 factory_cents / collection_fee / margin / 标签(亏本/健康/超额)
- 防代理通过加积分量套利(wholesale_per_point 全平台 SSOT)
- raw cost 严格 admin-only · 代理 API 永不外露(DTO 隔离)

关联:
- docs/AI-CONTEXT/V35_FACTORY_INVENTORY_MODEL_v6_2026-05-26.md §4 (结算公式)
- memory feedback_v35_factory_inventory_model_v6

红线:
- 不动 middleware/billing.py
- 不动 feature_pricing.cost_points(工具消费规则 SSOT 跨代理统一)
"""

import json
import hashlib
import logging
import math
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple, Dict, Any

from db.connection import get_db

logger = logging.getLogger("GEO-V35-AgentPricing")


# ============================================================
# 全局常量(铁律)
# ============================================================

# 1 元 = 130 积分 (底层历史换算 · 不变)
POINTS_PER_YUAN = 130

# GEO 出厂折扣(示例 9 折 · 代理拿货价 vs 平台标价)
# 1 积分 出厂 = 100 × 0.9 / 130 cents = 225/325 cents
# 用整数分子分母防浮点误差(Codex r2 P1-2)
WHOLESALE_NUMER = 225          # = 100 (cents/元) × 0.9 × 2.5
WHOLESALE_DENOM = 325          # = 130 (积分/元) × 2.5
# 仅 doc 用 · 实际计算用整数 ceil
WHOLESALE_CENTS_PER_POINT = WHOLESALE_NUMER / WHOLESALE_DENOM  # ≈ 0.6923

# 默认费率(配置化 · 实际从 system_settings.platform_fee_config 读)
DEFAULT_GATEWAY_FEE_BPS = 90        # 微信通道 0.9% · 启动锚定 · 对账回填
DEFAULT_SETTLEMENT_SERVICE_FEE_BPS = 190  # 平台代收服务费 1.9%
DEFAULT_TAX_RATE_BPS = 600          # 税预扣 6% · 按 margin 不按 R

# 亏损 / 超额阈值(协议 v2.1 第 3 条)
LOSS_TIER_LIGHT = -1000             # margin/factory · 单位 bps · -10%
LOSS_TIER_MEDIUM = -5000            # -50%
EXCESS_TIER_HIGH = 20000            # +200%
MAX_RETAIL_MULTIPLIER = 3.0         # 协议限 retail ≤ factory × 3
MAX_RETAIL_POINTS = 9_000_000_000_000_000
MAX_RETAIL_CENTS = 2_000_000_000
RETAIL_SKU_LOCK_NAMESPACE = 920718
RETAIL_MARKUP_LOCK_NAMESPACE = 920512
RETAIL_CASH_ANCHOR_SEMANTIC_VERSION = "retail-cash-anchor-v1"
_PRICING_CONFIG_LOCK = (920713, 1)


class RetailSKUVersionConflict(ValueError):
    """The caller edited a stale retail SKU version."""


class RetailSKUIdempotencyConflict(ValueError):
    """A client request id was reused with a different immutable payload."""


class RetailSKUNotFound(ValueError):
    """The retail SKU is absent, tombstoned for this action, or owned by another provider."""


# ============================================================
# 费率配置加载(从 system_settings)
# ============================================================

import time as _time
from db.xact_lock_guard import require_xact_scope

_fee_config_cache: Optional[Dict[str, Any]] = None
_fee_config_cache_at: float = 0.0
# [BUG-P3] 缓存 TTL · 蓝绿双实例下改 system_settings.platform_fee_config 费率后最多 60s 生效。
# 原无失效通道(reload_fee_config 全仓 0 调用方 · 无 admin 写端点)→ 进程级永不过期 →
# 提现三段扣费/结算费率长期用旧值,且蓝绿两容器各持一套(切流量时同一服务商两次报价不同)。
_FEE_CONFIG_TTL_SEC = 60.0


def get_platform_fee_config() -> Dict[str, Any]:
    """读 system_settings.platform_fee_config(JSONB)· 带 TTL 缓存(60s)"""
    global _fee_config_cache, _fee_config_cache_at
    if _fee_config_cache is not None and (_time.monotonic() - _fee_config_cache_at) < _FEE_CONFIG_TTL_SEC:
        return _fee_config_cache

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM system_settings WHERE key='platform_fee_config' LIMIT 1")
        row = cur.fetchone()
        if not row:
            logger.warning("platform_fee_config 缺失 · 用启动默认")
            _fee_config_cache = {
                "gateway_fee_bps": {"wechat_pay": DEFAULT_GATEWAY_FEE_BPS, "alipay": 60, "huipi": 90, "manual": 0},
                "settlement_service_fee_bps": DEFAULT_SETTLEMENT_SERVICE_FEE_BPS,
                "collection_fee_bps_display": 280,
                "tax_withholding_bps_default": DEFAULT_TAX_RATE_BPS,
            }
        else:
            val = row["value"] if isinstance(row, dict) else row[0]
            _fee_config_cache = json.loads(val) if isinstance(val, str) else val
    _fee_config_cache_at = _time.monotonic()
    return _fee_config_cache


def reload_fee_config():
    """配置改后 · 调用此函数清缓存"""
    global _fee_config_cache
    _fee_config_cache = None


def get_gateway_fee_bps(payment_method: str = "wechat_pay") -> int:
    """获取指定支付渠道实际费率(bps)"""
    config = get_platform_fee_config()
    rates = config.get("gateway_fee_bps", {})
    return int(rates.get(payment_method, DEFAULT_GATEWAY_FEE_BPS))


def get_settlement_service_fee_bps() -> int:
    config = get_platform_fee_config()
    return int(config.get("settlement_service_fee_bps", DEFAULT_SETTLEMENT_SERVICE_FEE_BPS))


def get_default_tax_rate_bps() -> int:
    config = get_platform_fee_config()
    return int(config.get("tax_withholding_bps_default", DEFAULT_TAX_RATE_BPS))


# ============================================================
# 代理税务身份(agent_tax_profiles 覆盖默认税率)
# ============================================================

def get_agent_tax_rate_bps(cursor, agent_user_id: int) -> int:
    """从 agent_tax_profiles 读 · 没配置回平台默认"""
    cursor.execute("""
        SELECT default_tax_rate_bps FROM agent_tax_profiles WHERE agent_user_id = %s
    """, (agent_user_id,))
    row = cursor.fetchone()
    if row and (row["default_tax_rate_bps"] if isinstance(row, dict) else row[0]) is not None:
        return int(row["default_tax_rate_bps"] if isinstance(row, dict) else row[0])
    return get_default_tax_rate_bps()


def get_agent_tax_mode(cursor, agent_user_id: int) -> str:
    cursor.execute("""
        SELECT default_tax_mode FROM agent_tax_profiles WHERE agent_user_id = %s
    """, (agent_user_id,))
    row = cursor.fetchone()
    if row:
        v = row["default_tax_mode"] if isinstance(row, dict) else row[0]
        if v: return str(v)
    return "withheld"  # 默认预扣


def calc_withdrawal_fees(cursor, agent_user_id: int, gross_cents: int) -> Dict[str, int]:
    """[V3.5 v7 批1D · 老板 B 2026-06-08] 提现 fees 三段拆分(SSOT · quote 端点 + create_settlement_request 共用)。

    服务商提现申请额(gross = settled margin)→ 扣【通道费 + 平台代收服务费 + 代扣税】→ net 到账。
    - 税率 per-agent(agent_tax_profiles override · 回落全局)· 通道/服务费全局 platform_fee_config。
    - fee 整数 floor(对服务商友好 · 差 <1 分)· net = gross - 三项 · 钱守恒。

    ⚠️ W2 铁律(schemas/v35_w2_dto.py docstring):Agent 路径【禁暴露 *_bps】。
       本函数【只返金额 cents · 绝不返 bps】。Agent API 进一步:gateway+settlement 合并成
       platform_fee_cents(不分项露平台费率结构)· tax_cents 单列(docstring 允许 · 报税知情权)。
       gateway_fee_cents/settlement_fee_cents 分项【仅供 DB 审计列(admin 对账)】· 不进 Agent API 响应。
    """
    keys = ("gross_cents", "gateway_fee_cents", "settlement_fee_cents",
            "platform_fee_cents", "tax_cents", "total_fee_cents", "net_cents")
    if gross_cents <= 0:
        return {k: 0 for k in keys}

    gateway_bps = get_gateway_fee_bps("wechat_pay")
    settlement_bps = get_settlement_service_fee_bps()
    tax_bps = get_agent_tax_rate_bps(cursor, agent_user_id)

    gateway_fee_cents = gross_cents * gateway_bps // 10000
    settlement_fee_cents = gross_cents * settlement_bps // 10000
    tax_cents = gross_cents * tax_bps // 10000
    platform_fee_cents = gateway_fee_cents + settlement_fee_cents
    total_fee_cents = platform_fee_cents + tax_cents
    net_cents = gross_cents - total_fee_cents
    return {
        "gross_cents": gross_cents,
        "gateway_fee_cents": gateway_fee_cents,      # DB 审计分项(不进 Agent API)
        "settlement_fee_cents": settlement_fee_cents,  # DB 审计分项(不进 Agent API)
        "platform_fee_cents": platform_fee_cents,    # Agent API 合并露
        "tax_cents": tax_cents,                      # Agent API 单列(金额 · 允许)
        "total_fee_cents": total_fee_cents,
        "net_cents": net_cents,
    }


# ============================================================
# 出厂折扣 per-agent override(D3 · 平台→服务商系数)
# ============================================================

def get_agent_wholesale_ratio(agent_user_id: Optional[int] = None) -> Tuple[int, int]:
    """出厂进货折扣 (numer, denom)。
    [D3] 优先 per-agent admin override(agent_pricing_overrides)· NULL/无记录回全局 pricing_config
    (默认 225/325 = 9 折)。平台可给单个服务商设更优/更高的进货折扣(丰俭由人)。
    """
    if agent_user_id:
        try:
            from services.agent_pricing_overrides import get_agent_wholesale_override
            ov = get_agent_wholesale_override(agent_user_id)
            if ov:
                return ov
        except Exception as exc:
            logger.warning("读取 agent 出厂折扣 override 失败 agent=%s(回落全局): %s", agent_user_id, exc)
    from config.pricing_config import get_wholesale_ratio
    return get_wholesale_ratio()


def calc_prepay_points(amount_cents: int, agent_user_id: Optional[int] = None) -> int:
    """代理预付进货积分:1 元按出厂折扣换算成出厂积分(示例 9 折 ≈ 144.4)。

    统一给 API 展示、渠道奖励和实扣链路复用,避免服务层反向 import API。
    """
    if amount_cents <= 0:
        return 0
    numer, denom = get_agent_wholesale_ratio(agent_user_id)
    return int(amount_cents) * int(denom) // int(numer)


# ============================================================
# SKU 计算公式(v6 §4)
# ============================================================

def calc_factory_cents(points_granted: int, numer: Optional[int] = None, denom: Optional[int] = None) -> int:
    """
    出厂成本(cents)· 整数 ceil 防累计误差(Codex r2 P1-2)
    factory_cents = ceil(N × numer / denom)  · 默认 225/325 = 9 折
    [D2 动态化] numer/denom 为 None 时从 pricing_config 读(全局后台可调);
      显式传入时用传入值(结算锁价:老订单用下单时锁定的系数·不被后台改系数重算)
    平台向上取整 · 保证不漏算成本(代理多担少量分位)
    """
    if points_granted <= 0:
        return 0
    if numer is None or denom is None:
        from config.pricing_config import get_wholesale_ratio
        numer, denom = get_wholesale_ratio()
    # ceil(a/b) = (a + b - 1) // b
    return (points_granted * numer + denom - 1) // denom


def _per_agent_factory_override(agent_user_id: Optional[int], points_granted: int) -> Optional[int]:
    """[BUG-P3] 有 per-agent 出厂折扣 override 才返回锁定的 factory_cents · 否则 None(用全局·零回归)。
    用于 margin 预览/校验:无 override(prod 全部)行为完全不变;有 override 的服务商 margin 标签按其真实
    出厂成本算(否则全局价架空 override → 亏本警示错,可能据此把售价定到实际亏本)。"""
    if not agent_user_id or points_granted <= 0:
        return None
    try:
        from services.agent_pricing_overrides import get_agent_wholesale_override
        ov = get_agent_wholesale_override(agent_user_id)
        if ov:
            return calc_factory_cents(points_granted, *ov)
    except Exception as exc:
        logger.warning("per-agent 出厂折扣读取失败 agent=%s(回落全局): %s", agent_user_id, exc)
    return None


def _load_current_pricing_config(cursor) -> Dict[str, Any]:
    """Read the transaction-stable procurement pricing source without process caches."""
    require_xact_scope(cursor, where="agent_pricing._load_current_pricing_config")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", _PRICING_CONFIG_LOCK)
    cursor.execute("SELECT value FROM system_settings WHERE key='pricing_config' FOR SHARE")
    row = cursor.fetchone()
    raw = row.get("value") if isinstance(row, dict) else (row[0] if row else {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("当前进货成本配置不可用") from exc
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("当前进货成本配置不可用")
    from config.pricing_config import merge_pricing_config

    return merge_pricing_config(raw or {}, include_env=False)


def resolve_agent_effective_retail_cost(
    cursor,
    *,
    agent_user_id: int,
    points_granted: int,
    pricing_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve the sole cost basis used by provider retail management and runtime.

    The platform procurement ratio supplies the root cost for the exact point count.
    Channel compounding is deliberately delegated to
    ``channel_pricing.resolve_effective_cost_basis``; this module never implements a
    second multiplier formula.  The returned version is opaque and internal-only so
    provider/customer DTOs cannot reveal the platform base, relationship path or bps.
    """
    agent_user_id = int(agent_user_id)
    if agent_user_id <= 0:
        raise ValueError("服务商账号非法")
    if type(points_granted) is not int or not 0 < points_granted <= MAX_RETAIL_POINTS:
        raise ValueError("包含算力必须是合法正整数")

    config = pricing_config or _load_current_pricing_config(cursor)
    from services.agent_inventory_pricing import resolve_discount
    from services import channel_pricing

    discount = resolve_discount(cursor, agent_user_id, config)
    platform_base_cents = calc_factory_cents(
        points_granted,
        int(discount["numer"]),
        int(discount["denom"]),
    )
    try:
        resolved = channel_pricing.resolve_effective_cost_basis(
            agent_user_id,
            platform_base_cents,
            cur=cursor,
        )
    except channel_pricing.ChannelError as exc:
        logger.warning(
            "服务商有效成本解析失败 agent=%s points=%s: %s",
            agent_user_id,
            points_granted,
            exc,
        )
        raise ValueError("当前有效成本不可用，请联系管理员") from exc

    effective_cost_cents = int(resolved["effective_cost_cents"])
    if platform_base_cents <= 0 or effective_cost_cents <= 0:
        raise ValueError("当前有效成本不可用，请联系管理员")
    relationship_path = [
        {
            "relationship_id": int(rel["id"]),
            "buyer_dealer_id": int(rel["buyer_dealer_id"]),
            "upstream_channel_account_id": int(rel["upstream_channel_account_id"]),
            "cost_multiplier_bps": int(rel["cost_multiplier_bps"]),
            "relationship_version": str(rel["relationship_version"]),
        }
        for rel in resolved.get("chain") or []
    ]
    version_payload = {
        "schema_version": 1,
        "agent_user_id": agent_user_id,
        "points_granted": points_granted,
        "procurement_ratio": {
            "source": str(discount["source"]),
            "numer": int(discount["numer"]),
            "denom": int(discount["denom"]),
        },
        "platform_base_cents": platform_base_cents,
        "effective_cost_cents": effective_cost_cents,
        "relationship_path": relationship_path,
    }
    cost_basis_version = hashlib.sha256(
        json.dumps(
            version_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "effective_cost_cents": effective_cost_cents,
        "cost_basis_version": cost_basis_version,
        # Internal publication evidence only. API DTO builders must never expose it.
        "_platform_base_cents": platform_base_cents,
        "_procurement_ratio": dict(discount),
    }


def resolve_agent_retail_markup(cursor, *, agent_user_id: int) -> Dict[str, Any]:
    """Read one provider's effective retail markup under the writer's lock.

    The administrative override wins over the provider preference.  The source is
    intentionally represented only by an opaque version outside this module.
    """
    agent_user_id = int(agent_user_id)
    if agent_user_id <= 0:
        raise ValueError("服务商账号非法")
    require_xact_scope(cursor, where="agent_pricing.resolve_agent_retail_markup")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute(
        "SELECT pg_advisory_xact_lock_shared(%s, %s)",
        (RETAIL_MARKUP_LOCK_NAMESPACE, agent_user_id),
    )
    cursor.execute(
        "SELECT agent_sku_markup_ratio FROM users WHERE id=%s FOR SHARE",
        (agent_user_id,),
    )
    user_row = cursor.fetchone()
    if not user_row:
        raise ValueError("服务商账号不存在")
    user_ratio = (
        user_row.get("agent_sku_markup_ratio")
        if isinstance(user_row, dict)
        else user_row[0]
    )
    cursor.execute(
        """SELECT sku_markup_override
             FROM agent_pricing_overrides
            WHERE agent_user_id=%s
            FOR SHARE""",
        (agent_user_id,),
    )
    override_row = cursor.fetchone()
    override_ratio = (
        override_row.get("sku_markup_override")
        if isinstance(override_row, dict) and override_row
        else override_row[0] if override_row else None
    )
    source = "admin_override" if override_ratio is not None else "provider_preference"
    raw_ratio = override_ratio if override_ratio is not None else user_ratio
    canonical = resolve_canonical_markup(raw_ratio if raw_ratio is not None else Decimal("1.00"))
    bps = int(canonical["bps"])
    if bps < 10000 or bps > int(MAX_RETAIL_MULTIPLIER * 10000):
        raise ValueError("当前客户售价配置不可用")
    version_payload = {
        "schema_version": 1,
        "agent_user_id": agent_user_id,
        "source": source,
        "markup_bps": bps,
    }
    return {
        "markup_bps": bps,
        "markup_version": hashlib.sha256(
            json.dumps(version_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def resolve_agent_retail_cash_anchor(
    cursor,
    *,
    agent_user_id: int,
    amount_cents: int,
) -> Dict[str, Any]:
    """Convert an exact customer cash amount to the maximum deliverable points.

    Cash never changes. Procurement/channel cost and the provider's effective
    markup only change delivered points. The existing cash-anchor calculator keeps
    the full relationship product exact and floors points once at the end.
    """
    if type(amount_cents) is not int or not 100 <= amount_cents <= 1_000_000:
        raise ValueError("自由充值金额须为 1 至 10000 元")
    # Global pricing writers take the pricing-config lock before retail mutation
    # locks. Keep that order here as well so an admin override cannot deadlock a
    # concurrent quote calculation.
    config = _load_current_pricing_config(cursor)
    markup = resolve_agent_retail_markup(cursor, agent_user_id=int(agent_user_id))
    from services.agent_inventory_pricing import resolve_procurement_discount_snapshot
    from services import channel_pricing
    from services.procurement_cash_anchor import CashAnchorError, calculate_cash_anchor

    discount = resolve_procurement_discount_snapshot(
        cursor,
        int(agent_user_id),
        config,
        published_catalog_version=str(
            config.get("agent_purchase_catalog_version") or "legacy-runtime"
        ),
        require_override_schema=False,
    )
    try:
        resolved = channel_pricing.resolve_effective_cost_basis(
            int(agent_user_id), 1, cur=cursor,
        )
    except channel_pricing.ChannelError as exc:
        raise ValueError("当前有效成本不可用，请联系管理员") from exc

    relationship_path = [
        {
            "seller_user_id": int(rel["upstream_channel_account_id"]),
            "buyer_user_id": int(rel["buyer_dealer_id"]),
            "relationship_id": int(rel["id"]),
            "relationship_version": str(rel["relationship_version"]),
            "source_kind": "channel_relationship",
            "multiplier_bps": int(rel["cost_multiplier_bps"]),
        }
        for rel in reversed(resolved.get("chain") or [])
    ]
    relationship_path.append({
        "seller_user_id": int(agent_user_id),
        "buyer_user_id": None,
        "relationship_id": None,
        "relationship_version": str(markup["markup_version"]),
        "source_kind": "provider_retail_markup",
        "multiplier_bps": int(markup["markup_bps"]),
    })
    try:
        anchored = calculate_cash_anchor(
            buyer_user_id=int(agent_user_id),
            payable_amount_cents=amount_cents,
            platform_points_numer=int(discount["denom"]),
            platform_points_denom=int(discount["numer"]),
            catalog_version=RETAIL_CASH_ANCHOR_SEMANTIC_VERSION,
            relationship_path=relationship_path,
            purchase_discount_snapshot=discount,
        )
    except CashAnchorError as exc:
        raise ValueError(str(exc)) from exc
    points_granted = int(anchored["paid_inventory_points"])
    effective_cost_cents = int(anchored["direct_seller_acquisition_cents"])
    cost_version_payload = {
        "discount_version": str(discount["discount_version"]),
        "relationship_path": relationship_path[:-1],
        "points_granted": points_granted,
        "effective_cost_cents": effective_cost_cents,
    }
    cost_basis_version = hashlib.sha256(
        json.dumps(
            cost_version_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    fingerprint_payload = {
        "semantic_version": RETAIL_CASH_ANCHOR_SEMANTIC_VERSION,
        "agent_user_id": int(agent_user_id),
        "amount_cents": amount_cents,
        "points_granted": points_granted,
        "effective_cost_cents": effective_cost_cents,
        "cost_basis_version": cost_basis_version,
        "markup_bps": int(markup["markup_bps"]),
        "markup_version": str(markup["markup_version"]),
    }
    return {
        **fingerprint_payload,
        "cash_anchor_fingerprint": hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "_platform_base_cents": int(anchored["platform_reference_amount_cents"]),
    }


def calc_settlement(
    customer_paid_cents: int,
    points_granted: int,
    payment_method: str = "wechat_pay",
    tax_rate_bps: Optional[int] = None,
    factory_cents_override: Optional[int] = None,
) -> Dict[str, int]:
    """
    [V3.5 v7 · 2026-06-08 老板拍板 A/B/C] 结算 = R - factory(纯 markup)· fees/税挪提现端。

    老板 C 财务铁律:平台代收 · 不入营收 · 全额转服务商 · 服务商再向平台进货(出厂价)。
    服务商每笔利润 = 客户付款 R − 出厂成本 factory(结算时【不扣】任何 fees/税)。
    渠道费 / 平台服务费 / 代扣个税 统一挪到【提现端】按提现额一次性透明扣除(见 withdrawal-quote)。

    ⚠️ 历史订单不重算(4B 选 A):本函数只算新结算;已写入 agent_revenue_ledger 的历史
       frozen/settled 笔保持原值(含老 fees/税)· 新旧公式并存属预期(老板拍板·历史铁律)。

    返回字段全保留(fees/税 = 0)向后兼容:finance_api / agent_finance_api / referral_api
    等下游读这些字段不 KeyError;新订单这些列 = 0,语义 = fees 已挪到提现端结算。

    旧 v6 §4 五层公式(R−factory−gateway−service_fee−tax)已废 · 见 git 历史。
    """
    R = customer_paid_cents
    # [D2] 结算锁价:有下单锁定的出厂价(override)则用它·否则用当前配置实时算(老订单不重算)
    factory_cents = factory_cents_override if factory_cents_override is not None else calc_factory_cents(points_granted)

    # [v7 老板 C] fees / 税 不在每笔结算扣 · 挪到提现端 · 字段保留 0 向后兼容
    # (payment_method / tax_rate_bps 入参保留以兼容调用方签名 · 结算端不再使用)
    gateway_fee_bps = 0
    settlement_service_fee_bps = 0
    tax_rate_bps = 0
    gateway_fee_cents = 0
    settlement_service_fee_cents = 0
    collection_fee_cents = 0
    tax_withholding_cents = 0

    agent_margin_before_tax = R - factory_cents
    agent_settlement_cents = agent_margin_before_tax  # = R − factory(纯 markup · 提现时才扣 fees/税)

    return {
        "customer_paid_cents": R,
        "factory_cents": factory_cents,
        "gateway_fee_bps": gateway_fee_bps,
        "gateway_fee_cents": gateway_fee_cents,
        "settlement_service_fee_bps": settlement_service_fee_bps,
        "settlement_service_fee_cents": settlement_service_fee_cents,
        "collection_fee_cents": collection_fee_cents,
        "agent_margin_before_tax_cents": agent_margin_before_tax,
        "tax_rate_bps": tax_rate_bps,
        "tax_withholding_cents": tax_withholding_cents,
        "agent_settlement_cents": agent_settlement_cents,
    }


# ============================================================
# 亏损 / 超额标签
# ============================================================

def label_margin(margin_before_tax_cents: int, factory_cents: int) -> Tuple[str, str]:
    """
    返回 (label, action)
        label: 'loss_heavy' / 'loss_light' / 'zero' / 'healthy' / 'profit_excellent' / 'margin_anomaly_blocked'
        action: 'allowed' / 'admin_approval_required' / 'rejected'
    """
    if factory_cents <= 0:
        return ("invalid_factory", "rejected")

    from config.pricing_config import get_margin_label_thresholds

    thresholds = get_margin_label_thresholds()
    loss_heavy = int(thresholds.get("loss_heavy_bps", LOSS_TIER_MEDIUM))
    loss_light = int(thresholds.get("loss_light_bps", LOSS_TIER_LIGHT))
    healthy = int(thresholds.get("healthy_bps", 2000))
    profit_excellent = int(thresholds.get("profit_excellent_bps", 8000))
    hard_block = int(thresholds.get("hard_block_bps", EXCESS_TIER_HIGH))

    ratio_bps = int(margin_before_tax_cents * 10000 / factory_cents)

    if ratio_bps < loss_heavy:
        return ("loss_heavy", "rejected")
    elif ratio_bps < loss_light:
        return ("loss_medium", "admin_approval_required")
    elif ratio_bps < 0:
        return ("loss_light", "allowed")  # UI 二次确认
    elif ratio_bps == 0:
        return ("zero", "allowed")
    elif ratio_bps < healthy:
        return ("healthy", "allowed")
    elif ratio_bps < profit_excellent:
        return ("profit_good", "allowed")
    elif ratio_bps < hard_block:
        return ("profit_excellent", "allowed")
    else:
        return ("margin_anomaly_blocked", "rejected")


# ============================================================
# SKU 查询(代理白标 · 给客户看)
# ============================================================

def get_sku_markup_with_default(
    agent_user_id: int,
    wholesale_cents: int,
    suggested_retail_cents: Optional[int] = None,
) -> int:
    """渠道激励默认 K 倍兜底价(分)。

    仅在 V35_SKU_K_DEFAULT_ENABLED 打开时由 get_sku_for_customer 调用:
    - 有平台建议零售价 → 直接用 suggested_retail_cents
    - 无建议价 → 按 SKU 批发价直接推导 retail = wholesale_cents * k_default
    """
    suggested = int(suggested_retail_cents or 0)
    if suggested > 0:
        return suggested
    wholesale = int(wholesale_cents or 0)
    if wholesale <= 0:
        return 0

    try:
        from config.pricing_config import get_k_default

        k_default = Decimal(str(get_k_default() or 2.0))
        return int((Decimal(wholesale) * k_default).to_integral_value(rounding=ROUND_HALF_UP))
    except Exception as exc:
        logger.warning(
            "SKU 默认 K 倍兜底价计算失败 agent=%s wholesale=%s suggested=%s · 回落 wholesale · %s",
            agent_user_id,
            wholesale_cents,
            suggested_retail_cents,
            exc,
        )
        return wholesale

def get_sku_for_customer(cursor, agent_user_id: int, sku_template_id: int) -> Optional[Dict[str, Any]]:
    """
    旧 template-only 调用的只读兼容视图。

    只允许唯一命中一条显式 canonical 零售行；不存在或命中多条均
    fail-closed，绝不从平台模板补价、补算力或随机选一条。
    返回:
        name (canonical custom_name)
        subtitle
        sales_pitch  (canonical custom_sales_pitch)
        retail_cents  (代理零售价 · 客户付的钱)
        points_granted
    严格 不返:
        factory_cents · platform_cost_cents · admin 后台才看

    双保险:过滤面议/0 元 SKU
    返回 None 表示 SKU 不可购 · 调用方应在 UI 引导客户联系商务
    """
    cursor.execute("""
        SELECT COALESCE(t.template_code, o.retail_sku_id) AS template_code,
               COALESCE(t.sku_type, 'credit_pack') AS sku_type,
               o.custom_name, o.custom_subtitle, o.custom_sales_pitch,
               o.retail_cents, o.points_granted, t.recommended_use_jsonb
        FROM agent_sku_overrides o
        LEFT JOIN sku_templates t ON t.id = o.sku_template_id
        WHERE o.agent_user_id = %s
          AND o.sku_template_id = %s
          AND o.is_active = TRUE AND o.deleted_at IS NULL
        ORDER BY o.id
        LIMIT 2
    """, (agent_user_id, sku_template_id))
    rows = cursor.fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]

    name = row["custom_name"]
    subtitle = row["custom_subtitle"]
    sales_pitch = row["custom_sales_pitch"] or ""
    retail_cents = row["retail_cents"]
    points_granted = int(row["points_granted"] or 0)
    retail_cents = int(retail_cents or 0)

    # [Codex r5 P1-2] 0 元/0 额度面议 SKU 对客户不可购
    # 防代理没改 retail · fallback 用 wholesale=0 误导客户"免费 0 积分"
    if points_granted <= 0 or retail_cents <= 0:
        logger.warning(
            f"[SKU 面议过滤] template_id={sku_template_id} agent={agent_user_id} · "
            f"points={points_granted} retail={retail_cents} · 不返客户(引导联系商务)"
        )
        return None

    return {
        "template_code": row["template_code"],
        "sku_type": row["sku_type"],
        "name": name,
        "subtitle": subtitle,
        "sales_pitch": sales_pitch,
        "retail_cents": retail_cents,
        "points_granted": points_granted,
        "recommended_use": row["recommended_use_jsonb"],
    }


def get_sku_for_agent(cursor, agent_user_id: int, sku_template_id: int) -> Optional[Dict[str, Any]]:
    """
    代理视角 · 完整定价信息(可看出厂价 · 但永远不看 platform_cost)
    """
    customer_view = get_sku_for_customer(cursor, agent_user_id, sku_template_id)
    if not customer_view:
        return None

    cost_context = resolve_agent_effective_retail_cost(
        cursor,
        agent_user_id=int(agent_user_id),
        points_granted=int(customer_view["points_granted"]),
    )
    breakdown = calc_settlement(
        customer_paid_cents=customer_view["retail_cents"],
        points_granted=customer_view["points_granted"],
        factory_cents_override=int(cost_context["effective_cost_cents"]),
    )
    label, action = label_margin(
        breakdown["agent_margin_before_tax_cents"],
        breakdown["factory_cents"],
    )

    return {
        **customer_view,
        "wholesale_cents": int(breakdown["factory_cents"]),
        "default_name": customer_view["name"],
        "default_capability_pitch": "",
        "breakdown": breakdown,
        "margin_label": label,
        "margin_action": action,
    }


def get_sku_for_admin(cursor, sku_template_id: int) -> Optional[Dict[str, Any]]:
    """
    Admin 视角 · 含 platform_cost · 仅 admin RBAC 调用
    """
    cursor.execute("""
        SELECT * FROM sku_templates WHERE id = %s
    """, (sku_template_id,))
    row = cursor.fetchone()
    if not row:
        return None
    return dict(row) if isinstance(row, dict) else row


# ============================================================
# SKU 上下架 / 自定义保存
# ============================================================

def _validate_and_calc_margin(
    cursor,
    sku_template_id: Optional[int],
    retail_cents: int,
    agent_user_id: Optional[int] = None,
    *,
    points_granted: Optional[int] = None,
    validate_source_template: bool = False,
    enforce_cost_floor: bool = True,
    pricing_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    公共 helper:校验零售价/独立算力并用 canonical 整数成本计算器计算 margin。
    返回 {wholesale_cents, points_granted, breakdown, margin_label, margin_action}
    · 零售价 <= 0 / 模板不存在(或已下架)→ raise ValueError
    · 预览可返回零利润/亏损结果，但创建、编辑和发布都必须严格高于
      当前服务商的有效继承成本；平台建议售价不再是可写零售来源。
    · label/action 仍由 calc_settlement/label_margin 统一派生，供预览如实展示。
    """
    if type(retail_cents) is not int or retail_cents <= 0 or retail_cents > MAX_RETAIL_CENTS:
        raise ValueError("零售价必须大于 0")
    tpl = None
    if sku_template_id is not None and (validate_source_template or points_granted is None):
        cursor.execute(
            """SELECT id, wholesale_cents, points_granted, default_name
                 FROM sku_templates WHERE id=%s AND is_active=TRUE""",
            (int(sku_template_id),),
        )
        tpl = cursor.fetchone()
        if not tpl:
            raise ValueError(f"推荐模板 {sku_template_id} 不存在 / 已下架")
    if points_granted is None:
        if not tpl:
            raise ValueError("包含算力必须由服务商明确填写")
        points_granted = int(tpl["points_granted"])
    if type(points_granted) is not int or not 0 < points_granted <= MAX_RETAIL_POINTS:
        raise ValueError("包含算力必须是合法正整数")
    if not agent_user_id:
        raise ValueError("服务商账号非法")
    cost_context = resolve_agent_effective_retail_cost(
        cursor,
        agent_user_id=int(agent_user_id),
        points_granted=int(points_granted),
        pricing_config=pricing_config,
    )
    effective_cost = int(cost_context["effective_cost_cents"])
    if enforce_cost_floor and int(retail_cents) <= effective_cost:
        raise ValueError("客户售价必须高于当前有效成本")
    breakdown = calc_settlement(
        customer_paid_cents=retail_cents,
        points_granted=points_granted,
        factory_cents_override=effective_cost,
    )
    label, action = label_margin(
        breakdown["agent_margin_before_tax_cents"],
        breakdown["factory_cents"],
    )
    return {
        "wholesale_cents": int(breakdown["factory_cents"]),
        "points_granted": points_granted,
        "source_default_name": (
            str(tpl["default_name"]) if tpl and tpl.get("default_name") else None
        ),
        "breakdown": breakdown,
        "margin_label": label,
        "margin_action": action,
        "cost_basis_version": str(cost_context["cost_basis_version"]),
    }


def preview_agent_retail_sku(
    cursor,
    *,
    agent_user_id: int,
    points_granted: int,
    retail_cents: int,
    _schema_checked: bool = False,
) -> Dict[str, Any]:
    """Return provider-visible economics without exposing upstream relationships.

    The response exposes only this provider's effective inherited cost. Platform
    base cost, relationship path, ids and multipliers remain internal.
    """
    from services.agent_retail_sku_schema import assert_schema_ready

    if not _schema_checked:
        assert_schema_ready(cursor)
    calc = _validate_and_calc_margin(
        cursor,
        None,
        retail_cents,
        agent_user_id=int(agent_user_id),
        points_granted=points_granted,
        enforce_cost_floor=False,
    )
    cost = int(calc["breakdown"]["factory_cents"])
    profit = int(retail_cents) - cost
    return {
        "points_granted": int(points_granted),
        "retail_cents": int(retail_cents),
        "estimated_cost_cents": cost,
        "estimated_profit_cents": profit,
        "margin_label": calc["margin_label"],
        "margin_action": calc["margin_action"],
        "is_loss": profit <= 0,
        "publishable": profit > 0,
        "estimate_basis": "current_effective_procurement_cost",
    }


def create_agent_sku_override(
    cursor,
    agent_user_id: int,
    sku_template_id: Optional[int] = None,
    custom_name: Optional[str] = None,
    custom_subtitle: Optional[str] = None,
    custom_sales_pitch: Optional[str] = None,
    custom_scene: Optional[str] = None,
    retail_cents: int = 0,
    is_active: Optional[bool] = True,
    sort_order: int = 0,
    *,
    points_granted: Optional[int] = None,
    source_template_id: Optional[int] = None,
    client_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    新建 canonical 服务商零售 SKU。平台模板仅作可选预填来源。

    旧调用方只传 sku_template_id 时仍可由模板预填 points，并把原字段保留为
    legacy identity；新 API 传 source_template_id + points_granted，持久行的
    sku_template_id 为 NULL，证明模板不再是创建/结算前提。
    """
    from services.agent_retail_sku_schema import assert_schema_ready

    assert_schema_ready(cursor)
    agent_user_id = int(agent_user_id)
    require_xact_scope(cursor, where="agent_pricing.create_agent_sku_override")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (RETAIL_SKU_LOCK_NAMESPACE, agent_user_id),
    )
    source_id = source_template_id if source_template_id is not None else sku_template_id
    client_request_id = str(client_request_id or "").strip() or None
    if client_request_id is not None and not 8 <= len(client_request_id) <= 80:
        raise ValueError("client_request_id 长度必须为 8..80")
    calc = _validate_and_calc_margin(
        cursor,
        source_id,
        retail_cents,
        agent_user_id=agent_user_id,
        points_granted=points_granted,
        validate_source_template=source_id is not None,
    )
    # Compatibility callers may still submit only the historical template id.
    # The template supplies a one-time default label, never the new SKU identity
    # or runtime points/cost.  The public API requires an explicit display_name.
    display_name = str(custom_name or calc.get("source_default_name") or "").strip()
    if not display_name or len(display_name) > 120:
        raise ValueError("算力包名称长度必须为 1..120")
    if len(str(custom_subtitle or "")) > 240:
        raise ValueError("副标题不能超过 240 字")
    if len(str(custom_sales_pitch or "")) > 1000 or len(str(custom_scene or "")) > 500:
        raise ValueError("卖点或适用场景过长")
    canonical_payload = {
        "source_template_id": int(source_id) if source_id is not None else None,
        "points_granted": int(calc["points_granted"]),
        "custom_name": display_name,
        "custom_subtitle": custom_subtitle,
        "custom_sales_pitch": custom_sales_pitch,
        "custom_scene": custom_scene,
        "retail_cents": int(retail_cents),
        "is_active": bool(is_active if is_active is not None else True),
        "sort_order": int(sort_order or 0),
    }
    if client_request_id:
        cursor.execute(
            """SELECT id, retail_sku_id, source_template_id, points_granted,
                      custom_name, custom_subtitle, custom_sales_pitch, custom_scene,
                      retail_cents, is_active, sort_order, version
                 FROM agent_sku_overrides
                WHERE agent_user_id=%s AND client_request_id=%s""",
            (agent_user_id, client_request_id),
        )
        existing = cursor.fetchone()
        if existing:
            existing_dict = dict(existing)
            if any(existing_dict.get(key) != value for key, value in canonical_payload.items()):
                raise RetailSKUIdempotencyConflict(
                    "client_request_id 已用于不同的零售 SKU 请求"
                )
            existing_dict.update({
                "margin_label": calc["margin_label"],
                "margin_action": calc["margin_action"],
                "breakdown": calc["breakdown"],
                "idempotent_replay": True,
            })
            return existing_dict
    retail_sku_id = "RSKU-" + uuid.uuid4().hex.upper()
    legacy_template_id = sku_template_id if source_template_id is None else None
    cursor.execute("""
        INSERT INTO agent_sku_overrides
            (agent_user_id, sku_template_id, source_template_id, retail_sku_id,
             points_granted, custom_name, custom_subtitle, custom_sales_pitch,
             custom_scene, retail_cents, is_active, sort_order, margin_warning,
             version, client_request_id, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,NOW())
        RETURNING id, retail_sku_id, version
    """, (
        agent_user_id, legacy_template_id, canonical_payload["source_template_id"],
        retail_sku_id, canonical_payload["points_granted"], display_name,
        custom_subtitle, custom_sales_pitch, custom_scene, int(retail_cents),
        bool(is_active if is_active is not None else True), int(sort_order or 0), calc["margin_action"], client_request_id,
    ))
    row = cursor.fetchone()
    return {
        "id": row["id"] if isinstance(row, dict) else row[0],
        "retail_sku_id": row["retail_sku_id"] if isinstance(row, dict) else row[1],
        "version": int(row["version"] if isinstance(row, dict) else row[2]),
        "points_granted": int(calc["points_granted"]),
        "margin_label": calc["margin_label"],
        "margin_action": calc["margin_action"],
        "breakdown": calc["breakdown"],
    }


def update_agent_sku_override(
    cursor,
    agent_user_id: int,
    override_id: int,
    custom_name: Optional[str] = None,
    custom_subtitle: Optional[str] = None,
    custom_sales_pitch: Optional[str] = None,
    custom_scene: Optional[str] = None,
    retail_cents: int = 0,
    is_active: Optional[bool] = None,
    sort_order: Optional[int] = None,
    *,
    points_granted: Optional[int] = None,
    expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    """
    更新已有服务商零售算力包(按 override_id · 归属校验 agent_user_id)。
    成本按当前有效进货比例和渠道继承关系重算；可选模板不参与成本。
    sort_order / custom_* / is_active 传 None 时 COALESCE 保留原值
    (供 apply-markup 一键定价只改 retail_cents · 不抹代理已设包装/上架态)。
    返回 {id, margin_label, margin_action, breakdown}
    """
    from services.agent_retail_sku_schema import assert_schema_ready

    assert_schema_ready(cursor)
    require_xact_scope(cursor, where="agent_pricing.update_agent_sku_override")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (RETAIL_SKU_LOCK_NAMESPACE, int(agent_user_id)),
    )
    # 归属校验 + 取 canonical 算力/可选来源(防越权改别的代理的包)
    # tombstone 只能由 restore_agent_sku_override 清除。这里锁住未删除行，
    # 与并发 delete 串行化，避免 SELECT 通过后 UPDATE 把已删除 SKU 复活。
    cursor.execute("""
        SELECT retail_sku_id, source_template_id, sku_template_id, points_granted,
               custom_name, custom_subtitle, custom_sales_pitch, custom_scene,
               retail_cents, is_active, sort_order, version
          FROM agent_sku_overrides
        WHERE id = %s AND agent_user_id = %s AND deleted_at IS NULL
        FOR UPDATE
    """, (override_id, agent_user_id))
    own = cursor.fetchone()
    if not own:
        raise RetailSKUNotFound("零售算力包不存在或无权操作")
    own = dict(own)
    current_version = int(own["version"])
    if expected_version is not None and int(expected_version) != current_version:
        raise RetailSKUVersionConflict(
            f"零售 SKU 已被更新(expected={expected_version}, current={current_version})"
        )
    next_name = str(custom_name if custom_name is not None else own["custom_name"]).strip()
    if not next_name or len(next_name) > 120:
        raise ValueError("算力包名称长度必须为 1..120")
    next_points = points_granted if points_granted is not None else own["points_granted"]
    calc = _validate_and_calc_margin(
        cursor,
        None,
        retail_cents,
        agent_user_id=agent_user_id,
        points_granted=next_points,
    )
    next_points = int(calc["points_granted"])

    if sort_order is None:
        cursor.execute("""
            UPDATE agent_sku_overrides SET
                custom_name = COALESCE(%s, custom_name),
                custom_subtitle = COALESCE(%s, custom_subtitle),
                custom_sales_pitch = COALESCE(%s, custom_sales_pitch),
                custom_scene = COALESCE(%s, custom_scene),
                retail_cents = %s,
                points_granted = %s,
                is_active = COALESCE(%s, is_active),
                margin_warning = %s,
                version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND agent_user_id = %s AND deleted_at IS NULL
            RETURNING version
        """, (custom_name, custom_subtitle, custom_sales_pitch, custom_scene,
              retail_cents, next_points, is_active, calc["margin_action"], override_id, agent_user_id))
    else:
        cursor.execute("""
            UPDATE agent_sku_overrides SET
                custom_name = COALESCE(%s, custom_name),
                custom_subtitle = COALESCE(%s, custom_subtitle),
                custom_sales_pitch = COALESCE(%s, custom_sales_pitch),
                custom_scene = COALESCE(%s, custom_scene),
                retail_cents = %s,
                points_granted = %s,
                is_active = COALESCE(%s, is_active),
                sort_order = %s,
                margin_warning = %s,
                version = version + 1,
                updated_at = NOW()
            WHERE id = %s AND agent_user_id = %s AND deleted_at IS NULL
            RETURNING version
        """, (custom_name, custom_subtitle, custom_sales_pitch, custom_scene,
              retail_cents, next_points, is_active, sort_order, calc["margin_action"],
              override_id, agent_user_id))
    updated_row = cursor.fetchone()
    if not updated_row:
        raise RetailSKUVersionConflict("零售 SKU 更新竞态")
    return {
        "id": override_id,
        "retail_sku_id": own["retail_sku_id"],
        "version": int(updated_row["version"]),
        "points_granted": next_points,
        "margin_label": calc["margin_label"],
        "margin_action": calc["margin_action"],
        "breakdown": calc["breakdown"],
    }


def resolve_canonical_markup(ratio) -> Dict[str, Any]:
    """[v11 F5] 把加价系数量化成【唯一 canonical 值】并由其派生 bps —— 单一口径,同时用于 bps / DB 存储 / 响应。

    量化到 2 位小数(与 users.agent_sku_markup_ratio NUMERIC(4,2) 及所有下游读取口径
    quote_pricing_preferences / admin_business_profile 一致);bps【由 canonical 派生】(非原始输入),
    消除"1.234 按 1.234 定价、其它链路按存储的 1.23 读取"= 同系数两套价。
    返回 {"ratio": Decimal(canonical, 2 位), "bps": int}。
    """
    from decimal import Decimal, ROUND_HALF_UP
    canonical = Decimal(str(ratio)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # 1.234 → 1.23
    bps = int((canonical * Decimal(10000)).to_integral_value(rounding=ROUND_HALF_UP))   # 1.23 → 12300
    return {"ratio": canonical, "bps": bps}


def apply_markup_for_agent(cursor, agent_user_id: int, bps: int) -> Dict[str, Any]:
    """按系数重定价服务商现有的 active canonical 零售包。

    下架/tombstone 不参与，平台模板不会被自动物化为零售包。每个包均按
    自身 points_granted 进入 canonical 成本计算器，再由
    apply_multiplier_cents 进行整数分计价。服务商 advisory 事务锁与逐行
    version CAS 保证重复点击、蓝绿并发不会产生第二套价格真相。
    返回 {"created": [...], "updated": [...], "skipped": [...]}(逐项含 sku/retail_cents/reason)。
    """
    from services.pricing_catalog import apply_multiplier_cents
    bps = int(bps)
    created, updated, skipped = [], [], []
    # 按服务商 advisory 事务锁(与 endpoint 同 classid · 直接测 core 时亦生效)
    require_xact_scope(cursor, where="agent_pricing.apply_markup_for_agent")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute("SELECT pg_advisory_xact_lock(920512, %s)", (agent_user_id,))

    # 只重定价已经存在的 canonical retail SKU；平台模板不再自动 materialize。
    cursor.execute("""
        SELECT o.id AS override_id, o.points_granted, o.version,
               o.custom_name AS sku_name
        FROM agent_sku_overrides o
        WHERE o.agent_user_id = %s AND o.is_active = TRUE AND o.deleted_at IS NULL
        ORDER BY o.id
    """, (agent_user_id,))
    for s in [dict(r) for r in cursor.fetchall()]:
        cost_context = resolve_agent_effective_retail_cost(
            cursor,
            agent_user_id=int(agent_user_id),
            points_granted=int(s["points_granted"]),
        )
        wholesale = int(cost_context["effective_cost_cents"])
        retail = apply_multiplier_cents(wholesale, bps)
        try:
            update_agent_sku_override(
                cursor, agent_user_id=agent_user_id, override_id=s["override_id"],
                custom_name=None, custom_subtitle=None, custom_sales_pitch=None, custom_scene=None,
                retail_cents=retail, is_active=None, sort_order=None,
                points_granted=int(s["points_granted"]), expected_version=int(s["version"]),
            )
            updated.append({"sku": s["sku_name"], "retail_cents": retail, "reason": "更新已有白标包售价"})
        except ValueError as e:
            skipped.append({"sku": s["sku_name"], "retail_cents": retail, "reason": str(e)})

    return {"created": created, "updated": updated, "skipped": skipped}


def delete_agent_sku_override(
    cursor, agent_user_id: int, override_id: int, *, expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    """
    删除白标算力包(按 override_id · 归属校验 agent_user_id)。
    [v12 item7] **一律 tombstone · 绝不硬删除**(is_active=FALSE + deleted_at=NOW()):
      物理删除会丢 tombstone → 一键应用系数(apply_markup loop B)可能【重新物化该 SKU 上架】= resurrect。
      保 tombstone 后 apply_markup 永不 resurrect;只有 restore_agent_sku_override(显式恢复)能清 deleted_at。
    - 归属校验失败 → raise ValueError("白标包不存在或无权操作")
    - 一律软删并打 tombstone · 返回 {"action": "soft_deleted", "referenced": bool}
    """
    from services.agent_retail_sku_schema import assert_schema_ready

    assert_schema_ready(cursor)
    require_xact_scope(cursor, where="agent_pricing.delete_agent_sku_override")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (RETAIL_SKU_LOCK_NAMESPACE, int(agent_user_id)),
    )
    # 归属校验:取到属于该代理的行才继续
    cursor.execute("""
        SELECT id, version FROM agent_sku_overrides
        WHERE id = %s AND agent_user_id = %s
        FOR UPDATE
    """, (override_id, agent_user_id))
    owned = cursor.fetchone()
    if owned is None:
        raise RetailSKUNotFound("零售算力包不存在或无权操作")
    if expected_version is not None and int(owned["version"]) != int(expected_version):
        raise RetailSKUVersionConflict("零售 SKU 已被更新 · 请刷新后重试")

    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM recharge_orders WHERE override_id = %s", (override_id,))
    cnt_row = cursor.fetchone()
    referenced = int((cnt_row["cnt"] if isinstance(cnt_row, dict) else cnt_row[0]) or 0) > 0

    # [v12 item7] 一律 tombstone(不硬删)· deleted_at 非空 → apply_markup loop A/B 永不 resurrect。
    #   COALESCE 保留首个 deleted_at(重复删除幂等 · 不刷新删除时间)。
    cursor.execute("""
        UPDATE agent_sku_overrides
        SET is_active = FALSE, deleted_at = COALESCE(deleted_at, NOW()),
            version = version + 1, updated_at = NOW()
        WHERE id = %s AND agent_user_id = %s
        RETURNING version
    """, (override_id, agent_user_id))
    return {
        "action": "soft_deleted", "referenced": referenced,
        "version": int(cursor.fetchone()["version"]),
    }


def restore_agent_sku_override(
    cursor, agent_user_id: int, override_id: int, *, expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    """[v12 item7] 显式【恢复/重新上架】一个 tombstone 白标包:清 deleted_at + 重新 active。

    只有本【显式动作】能清除 deleted_at(一键应用系数 apply_markup 绝不清·绝不 resurrect)。
    归属校验 · FOR UPDATE 锁行 · updated_at 记审计时点。返回 {"action": "restored", "was_tombstoned": bool}。
    """
    from services.agent_retail_sku_schema import assert_schema_ready

    assert_schema_ready(cursor)
    require_xact_scope(cursor, where="agent_pricing.restore_agent_sku_override")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (RETAIL_SKU_LOCK_NAMESPACE, int(agent_user_id)),
    )
    cursor.execute(
        "SELECT id, deleted_at, version FROM agent_sku_overrides WHERE id=%s AND agent_user_id=%s FOR UPDATE",
        (override_id, agent_user_id))
    row = cursor.fetchone()
    if row is None:
        raise RetailSKUNotFound("零售算力包不存在或无权操作")
    if expected_version is not None and int(row["version"]) != int(expected_version):
        raise RetailSKUVersionConflict("零售 SKU 已被更新 · 请刷新后重试")
    _was = (row["deleted_at"] if isinstance(row, dict) else row[1]) is not None
    cursor.execute("""
        UPDATE agent_sku_overrides
        SET deleted_at = NULL, is_active = TRUE, version = version + 1, updated_at = NOW()
        WHERE id = %s AND agent_user_id = %s
        RETURNING version
    """, (override_id, agent_user_id))
    return {
        "action": "restored", "was_tombstoned": _was,
        "version": int(cursor.fetchone()["version"]),
    }


# ============================================================
# DTO 严格隔离(Codex r1 P0 防 raw cost 泄露)
# ============================================================

def strip_admin_fields(sku_dict: Dict[str, Any]) -> Dict[str, Any]:
    """从 SKU dict 去除 admin-only 字段 · 用在代理 API 响应前"""
    return {k: v for k, v in sku_dict.items() if k not in (
        "platform_cost_cents", "platform_cost_yuan", "raw_cost", "internal_margin",
    )}
