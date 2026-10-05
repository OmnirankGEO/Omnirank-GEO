"""
Admin helpers for channel tier configuration, overrides, and default SKU packages.

The runtime incentive engine stays in services.channel_tier; this module is the
admin surface that validates mutable configuration before it is persisted.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from config.pricing_config import (
    get_agent_tier_config,
    get_bonus_validity_months,
    get_founding_config,
    get_k_default,
    get_margin_label_thresholds,
    get_pricing_config,
)


CHANNEL_TIERS = ("certified", "preferred", "strategic")
DEFAULT_TIER_DESCRIPTIONS = {
    "certified": "达到认证渠道门槛后，进货享受认证奖励",
    "preferred": "达到优选渠道门槛后，进货享受优选奖励",
    "strategic": "达到战略渠道门槛后，进货享受战略奖励",
}
TIER_COMPAT_DEFAULTS = {
    "certified": {"min_yuan": 500, "bonus_rate": "0.1", "is_enabled": True},
    "preferred": {"min_yuan": 3000, "bonus_rate": "0.15", "is_enabled": True},
    "strategic": {"min_yuan": 10000, "bonus_rate": "0.3", "is_enabled": True},
}
CHANNEL_COMPAT_DEFAULTS = {
    "agent_tier_config": copy.deepcopy(TIER_COMPAT_DEFAULTS),
    "founding": {"cap": 10, "first_order_extra_bonus": "0.1", "min_first_order_yuan": 500},
    "bonus_validity_months": 12,
    "k_default": "2",
    "margin_label_thresholds": {
        "loss_heavy_bps": -4000,
        "loss_light_bps": -1000,
        "healthy_bps": 2000,
        "profit_excellent_bps": 8000,
        "hard_block_bps": 250000,
    },
}
CHANNEL_CONFIG_KEYS = (
    "agent_tier_config",
    "founding",
    "bonus_validity_months",
    "k_default",
    "margin_label_thresholds",
)


class ChannelTierVersionConflict(ValueError):
    def __init__(self, current_catalog_version: str):
        super().__init__("渠道奖励配置已被其他管理员更新")
        self.current_catalog_version = current_catalog_version


def _as_int(value: Any, field: str, minimum: Optional[int] = None) -> int:
    try:
        result = int(value)
    except Exception as exc:
        raise ValueError(f"{field} 必须是整数") from exc
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} 必须 >= {minimum}")
    return result


def _as_decimal(
    value: Any,
    field: str,
    minimum: Optional[Decimal] = None,
    maximum: Optional[Decimal] = None,
) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if not result.is_finite():
        raise ValueError(f"{field} 必须是有限数字")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} 必须 >= {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field} 必须 <= {maximum}")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _default_channel_config() -> Dict[str, Any]:
    return {
        "agent_tier_config": copy.deepcopy(get_agent_tier_config()),
        "founding": copy.deepcopy(get_founding_config()),
        "bonus_validity_months": get_bonus_validity_months(),
        "k_default": get_k_default(),
        "margin_label_thresholds": copy.deepcopy(get_margin_label_thresholds()),
    }


def _merge_dict(base: Dict[str, Any], patch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    if not patch:
        return result
    for key, value in patch.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _normalize_tier_config(value: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for tier in CHANNEL_TIERS:
        row = _merge_dict(TIER_COMPAT_DEFAULTS[tier], value.get(tier) or {})
        description = str(row.get("description") or DEFAULT_TIER_DESCRIPTIONS[tier]).strip()
        if not description or len(description) > 120:
            raise ValueError(f"{tier}.description 必须为 1-120 个字符")
        result[tier] = {
            "min_yuan": _decimal_text(_as_decimal(row.get("min_yuan"), f"{tier}.min_yuan", Decimal("0"))),
            "bonus_rate": _decimal_text(_as_decimal(row.get("bonus_rate"), f"{tier}.bonus_rate", Decimal("0"), Decimal("1"))),
            "is_enabled": bool(row.get("is_enabled", True)),
            "description": description,
        }
    mins = [Decimal(result[tier]["min_yuan"]) for tier in CHANNEL_TIERS]
    if not (mins[0] < mins[1] < mins[2]):
        raise ValueError("渠道等级门槛必须单调递增: certified < preferred < strategic")
    return result


def _normalize_founding(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cap": _as_int(value.get("cap"), "founding.cap", 0),
        "first_order_extra_bonus": _decimal_text(_as_decimal(
            value.get("first_order_extra_bonus"),
            "founding.first_order_extra_bonus",
            Decimal("0"),
            Decimal("1"),
        )),
        "min_first_order_yuan": _decimal_text(_as_decimal(
            value.get("min_first_order_yuan"), "founding.min_first_order_yuan", Decimal("0")
        )),
    }


def _normalize_margin_thresholds(value: Dict[str, Any]) -> Dict[str, int]:
    result = {
        "loss_heavy_bps": _as_int(value.get("loss_heavy_bps"), "margin.loss_heavy_bps"),
        "loss_light_bps": _as_int(value.get("loss_light_bps"), "margin.loss_light_bps"),
        "healthy_bps": _as_int(value.get("healthy_bps"), "margin.healthy_bps"),
        "profit_excellent_bps": _as_int(value.get("profit_excellent_bps"), "margin.profit_excellent_bps"),
        "hard_block_bps": _as_int(value.get("hard_block_bps"), "margin.hard_block_bps"),
    }
    if not (
        result["loss_heavy_bps"]
        < result["loss_light_bps"]
        < 0
        < result["healthy_bps"]
        < result["profit_excellent_bps"]
        < result["hard_block_bps"]
    ):
        raise ValueError("毛利标签阈值顺序非法")
    return result


def normalize_channel_tier_config(
    payload: Optional[Dict[str, Any]],
    *,
    base_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate admin input and return a complete channel-tier config payload.

    system_settings.pricing_config uses shallow top-level merge. Returning a
    complete nested object prevents admin partial edits from dropping sibling
    tiers or purchase rows.
    """
    if payload and "agent_purchase_options" in payload:
        raise ValueError("进货价目表只能通过专用接口修改")
    if base_config is not None:
        current_channel = {
            key: copy.deepcopy(base_config.get(key))
            for key in CHANNEL_CONFIG_KEYS
            if base_config.get(key) is not None
        }
        # 旧配置可能只保存嵌套对象的一部分；锁内必须以静态兼容默认补齐，不能访问进程缓存。
        base = _merge_dict(CHANNEL_COMPAT_DEFAULTS, current_channel)
    else:
        base = _default_channel_config()
    merged = _merge_dict(base, payload or {})
    bonus_validity_months = _as_int(merged["bonus_validity_months"], "bonus_validity_months", 1)
    if bonus_validity_months > 120:
        raise ValueError("bonus_validity_months 必须 <= 120")
    return {
        "agent_tier_config": _normalize_tier_config(merged["agent_tier_config"]),
        "founding": _normalize_founding(merged["founding"]),
        "bonus_validity_months": bonus_validity_months,
        "k_default": _decimal_text(_as_decimal(
            merged["k_default"], "k_default", Decimal("0.1"), Decimal("20")
        )),
        "margin_label_thresholds": _normalize_margin_thresholds(merged["margin_label_thresholds"]),
    }


def channel_tier_config_snapshot(pricing_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if pricing_config is None:
        return normalize_channel_tier_config({})
    return normalize_channel_tier_config({}, base_config=pricing_config)


def write_channel_tier_config(
    cursor,
    payload: Dict[str, Any],
    *,
    expected_catalog_version: str,
) -> Dict[str, Any]:
    cursor.execute("SELECT pg_advisory_xact_lock(920713, 1)")
    # 只 patch 渠道字段到 DB 原值，绝不把默认值或 PRICING_CONFIG 环境覆盖中的
    # agent_purchase_options 写回数据库，避免停用档被旁路保存复活。
    cursor.execute("SELECT value FROM system_settings WHERE key=%s FOR UPDATE", ("pricing_config",))
    row = cursor.fetchone()
    raw = row.get("value") if isinstance(row, dict) else (row[0] if row else None)
    try:
        current = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        current = {}
    from config.pricing_config import get_agent_purchase_catalog_version, merge_pricing_config
    effective_current = merge_pricing_config(current, include_env=False)
    current_version = get_agent_purchase_catalog_version(effective_current)
    if expected_catalog_version != current_version:
        raise ChannelTierVersionConflict(current_version)
    # 必须在锁内以 DB 当前值补全；禁止使用进程缓存或旧页面值补全未修改字段。
    previous_config = normalize_channel_tier_config({}, base_config=effective_current)
    normalized = normalize_channel_tier_config(payload, base_config=effective_current)
    for key in CHANNEL_CONFIG_KEYS:
        if key in normalized:
            current[key] = normalized[key]
    cursor.execute(
        """
        INSERT INTO system_settings (key, value, description, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            description = EXCLUDED.description,
            updated_at = NOW()
        """,
        (
            "pricing_config",
            json.dumps(current, ensure_ascii=False, default=str),
            "Dynamic pricing configuration",
        ),
    )
    from services.agent_inventory_pricing import advance_catalog_version_cursor
    new_version, committed_epoch = advance_catalog_version_cursor(cursor)
    return {
        "config": normalized,
        "previous_config": previous_config,
        "catalog_version": new_version,
        "previous_catalog_version": current_version,
        "committed_epoch": committed_epoch,
    }


def default_channel_sku_packages() -> List[Dict[str, Any]]:
    """Service-value package defaults. Customer copy must not expose points/cost/margin."""
    return [
        {
            "template_code": "scenario_trial",
            "sku_type": "scenario_pack",
            "default_name": "试水包",
            "default_subtitle": "1 个真实获客词试一轮",
            "default_capability_pitch": "先用一个真实获客词跑完整链路,看 AI 搜索里有没有你的位置。",
            "points_granted": 7150,
            "suggested_retail_cents": 69900,
        },
        {
            "template_code": "scenario_validation",
            "sku_type": "scenario_pack",
            "default_name": "验证包",
            "default_subtitle": "推荐 · 3 个核心词验证方向",
            "default_capability_pitch": "3 个核心词同时做,一个月看出 AI 愿不愿意提你。",
            "points_granted": 26000,
            "suggested_retail_cents": 248000,
        },
        {
            "template_code": "scenario_launch",
            "sku_type": "scenario_pack",
            "default_name": "启动包",
            "default_subtitle": "本地品牌首月 5-6 个词",
            "default_capability_pitch": "起步标配:5-6 个词全面铺开,首月跑出声量。",
            "points_granted": 52000,
            "suggested_retail_cents": 498000,
        },
        {
            "template_code": "scenario_growth",
            "sku_type": "scenario_pack",
            "default_name": "经营包",
            "default_subtitle": "多词多区域持续经营",
            "default_capability_pitch": "多词多区域长期经营,把 AI 推荐位牢牢占住。",
            "points_granted": 104000,
            "suggested_retail_cents": 980000,
        },
        {
            "template_code": "addon_diagnosis",
            "sku_type": "addon_pack",
            "default_name": "诊断包",
            "default_subtitle": "单独补一项,随时加",
            "default_capability_pitch": "1 次全面 AI 可见度诊断,输出可读报告。",
            "points_granted": 1040,
            "suggested_retail_cents": 9900,
        },
        {
            "template_code": "addon_writing",
            "sku_type": "addon_pack",
            "default_name": "写作包",
            "default_subtitle": "单独补一项,随时加",
            "default_capability_pitch": "1 篇 AI 优化文章。",
            "points_granted": 390,
            "suggested_retail_cents": 5900,
        },
        {
            "template_code": "addon_monitor",
            "sku_type": "addon_pack",
            "default_name": "监测包",
            "default_subtitle": "单独补一项,随时加",
            "default_capability_pitch": "1 个月多引擎监测,约 5 个词并生成战报。",
            "points_granted": 19500,
            "suggested_retail_cents": 29900,
        },
    ]


def seed_default_channel_sku_packages(cursor) -> Dict[str, Any]:
    from services.agent_pricing import calc_factory_cents

    seeded: List[str] = []
    for row in default_channel_sku_packages():
        wholesale_cents = int(calc_factory_cents(int(row["points_granted"])))
        cursor.execute(
            """
            UPDATE sku_templates
               SET sku_type = %s,
                   default_name = %s,
                   default_subtitle = %s,
                   default_capability_pitch = %s,
                   points_granted = %s,
                   wholesale_cents = %s,
                   suggested_retail_cents = %s,
                   is_active = TRUE,
                   updated_at = NOW()
             WHERE template_code = %s
            """,
            (
                row["sku_type"],
                row["default_name"],
                row["default_subtitle"],
                row["default_capability_pitch"],
                row["points_granted"],
                wholesale_cents,
                row["suggested_retail_cents"],
                row["template_code"],
            ),
        )
        if cursor.rowcount == 0:
            cursor.execute(
                """
                INSERT INTO sku_templates (
                    template_code, sku_type, default_name, default_subtitle,
                    default_capability_pitch, points_granted, wholesale_cents,
                    suggested_retail_cents, is_active, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, NOW(), NOW())
                """,
                (
                    row["template_code"],
                    row["sku_type"],
                    row["default_name"],
                    row["default_subtitle"],
                    row["default_capability_pitch"],
                    row["points_granted"],
                    wholesale_cents,
                    row["suggested_retail_cents"],
                ),
            )
        seeded.append(row["template_code"])

    cursor.execute(
        """
        UPDATE sku_templates
           SET is_active = FALSE, updated_at = NOW()
         WHERE LOWER(COALESCE(template_code, '')) IN ('basic_experience', 'trial_basic')
            OR default_name = '基础体验包'
        """
    )
    disabled_count = int(cursor.rowcount or 0)
    return {"seeded": seeded, "disabled_basic_count": disabled_count}


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _plain_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def list_channel_tier_agents(cursor, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    from services.channel_revenue_lifecycle import refund_effective_sql

    effective_sql, effective_params = refund_effective_sql("ro")

    cursor.execute(
        f"""
        WITH rolling_net AS (
            SELECT ro.user_id,
                   COALESCE(SUM(GREATEST(
                       0,
                       amount_cents - CASE
                            WHEN {effective_sql}
                           THEN CASE
                               WHEN COALESCE(refunded_amount_cents, 0) > 0
                               THEN LEAST(amount_cents, refunded_amount_cents)
                               ELSE amount_cents
                           END
                           ELSE 0
                       END
                   )), 0) / 100.0 AS rolling_12m_yuan
              FROM recharge_orders ro
             WHERE ro.order_type = 'agent_inventory_purchase'
               AND ro.payment_status = 'paid'
               AND COALESCE(ro.paid_at, ro.created_at) >= NOW() - INTERVAL '12 months'
             GROUP BY ro.user_id
        )
        SELECT u.id AS agent_user_id,
               u.username,
               COALESCE(NULLIF(u.display_name, ''), u.username) AS display_name,
               COALESCE(w.agent_level, 0) AS agent_level,
               COALESCE(s.channel_tier, 'none') AS channel_tier,
               COALESCE(r.rolling_12m_yuan, 0) AS rolling_12m_yuan,
               s.tier_override,
               s.tier_override_until,
               s.tier_override_by,
               s.tier_override_note,
               COALESCE(s.is_founder, FALSE) AS is_founder,
               s.founder_rank,
               s.last_evaluated_at,
               s.updated_at
          FROM users u
          LEFT JOIN user_wallets w ON w.user_id = u.id
          LEFT JOIN agent_channel_tier_state s ON s.agent_user_id = u.id
          LEFT JOIN rolling_net r ON r.user_id = u.id
         WHERE COALESCE(u.is_active, 1) = 1
           AND (
                s.agent_user_id IS NOT NULL
                OR COALESCE(w.agent_level, 0) >= 1
           )
         ORDER BY COALESCE(r.rolling_12m_yuan, 0) DESC, u.id DESC
         LIMIT %s OFFSET %s
        """,
        (*effective_params, int(limit), int(offset)),
    )
    rows = cursor.fetchall() or []
    return [
        {
            **dict(row),
            "tier_override_until": _iso(row.get("tier_override_until")),
            "last_evaluated_at": _iso(row.get("last_evaluated_at")),
            "updated_at": _iso(row.get("updated_at")),
        }
        for row in rows
    ]


def founder_seat_summary(cursor) -> Dict[str, int]:
    cap_default = int((get_founding_config() or {}).get("cap", 10) or 10)
    cursor.execute("SELECT used, cap FROM founder_seats WHERE id = 1")
    row = cursor.fetchone() or {"used": 0, "cap": cap_default}
    return {"used": int(row["used"]), "cap": int(row["cap"]), "remaining": max(0, int(row["cap"]) - int(row["used"]))}


def channel_tier_history(cursor, agent_user_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
    params: List[Any] = []
    where = ""
    if agent_user_id:
        where = "WHERE agent_user_id = %s"
        params.append(int(agent_user_id))
    params.append(int(limit))
    cursor.execute(
        f"""
        SELECT *
          FROM agent_tier_change_log
          {where}
         ORDER BY created_at DESC, id DESC
         LIMIT %s
        """,
        tuple(params),
    )
    return [{**dict(row), "created_at": _iso(row.get("created_at"))} for row in (cursor.fetchall() or [])]


def set_agent_tier_override(
    cursor,
    agent_user_id: int,
    *,
    tier: Optional[str],
    admin_user_id: int,
    until: Optional[datetime] = None,
    note: Optional[str] = None,
    clear: bool = False,
) -> Dict[str, Any]:
    from services.channel_tier import TIER_RANK, compute_rolling_12m_yuan, determine_tier

    if tier is not None and tier not in CHANNEL_TIERS:
        raise ValueError("tier 必须是 certified/preferred/strategic 或清空")

    cursor.execute("SELECT pg_advisory_xact_lock(920713, 1)")

    rolling = compute_rolling_12m_yuan(cursor, agent_user_id)
    automatic_tier = determine_tier(rolling)
    cursor.execute(
        "SELECT channel_tier FROM agent_channel_tier_state WHERE agent_user_id=%s FOR UPDATE",
        (int(agent_user_id),),
    )
    before_row = cursor.fetchone()
    old_tier = (before_row["channel_tier"] if isinstance(before_row, dict) else before_row[0]) if before_row else "none"
    effective_tier = (
        automatic_tier
        if clear or tier is None
        else max((automatic_tier, tier), key=lambda value: TIER_RANK[value])
    )
    override_tier = None if clear else tier
    if until and until.tzinfo:
        until = until.astimezone(timezone.utc).replace(tzinfo=None)
    direction = "init"
    if old_tier != effective_tier:
        direction = "upgrade" if TIER_RANK[effective_tier] > TIER_RANK.get(old_tier, 0) else "downgrade"

    cursor.execute(
        """
        INSERT INTO agent_channel_tier_state (
            agent_user_id, channel_tier, rolling_12m_yuan, tier_override,
            tier_override_until, tier_override_by, tier_override_note,
            last_evaluated_at, tier_effective_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW())
        ON CONFLICT (agent_user_id) DO UPDATE SET
            channel_tier = EXCLUDED.channel_tier,
            rolling_12m_yuan = EXCLUDED.rolling_12m_yuan,
            tier_override = EXCLUDED.tier_override,
            tier_override_until = EXCLUDED.tier_override_until,
            tier_override_by = EXCLUDED.tier_override_by,
            tier_override_note = EXCLUDED.tier_override_note,
            last_evaluated_at = NOW(),
            tier_effective_at = CASE
                WHEN agent_channel_tier_state.channel_tier IS DISTINCT FROM EXCLUDED.channel_tier
                THEN NOW() ELSE agent_channel_tier_state.tier_effective_at END,
            updated_at = NOW()
        RETURNING *
        """,
        (
            int(agent_user_id),
            effective_tier,
            rolling,
            override_tier,
            until,
            None if clear else int(admin_user_id),
            None if clear else note,
        ),
    )
    state = dict(cursor.fetchone() or {})
    from services.agent_inventory_pricing import advance_catalog_version_cursor
    advance_catalog_version_cursor(cursor)
    idem_scope = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    cursor.execute(
        """
        INSERT INTO agent_tier_change_log (
            agent_user_id, from_tier, to_tier, direction, rolling_12m_yuan,
            trigger_source, related_order_id, idempotency_key, note
        ) VALUES (%s, %s, %s, %s, %s, 'admin_manual', NULL, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            int(agent_user_id),
            old_tier,
            effective_tier,
            direction,
            rolling,
            f"manual:{agent_user_id}:{effective_tier}:{admin_user_id}:{idem_scope}",
            note,
        ),
    )
    return {
        **{key: _plain_value(value) for key, value in state.items()},
        "automatic_tier": automatic_tier,
        "effective_tier": effective_tier,
        "tier_override_until": _iso(state.get("tier_override_until")),
        "last_evaluated_at": _iso(state.get("last_evaluated_at")),
        "updated_at": _iso(state.get("updated_at")),
    }
