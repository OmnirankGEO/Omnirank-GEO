"""Test-only builders for immutable pricing catalogs and flag states."""

from __future__ import annotations

import json
from typing import Any

from config import pricing_ssot_flags
from db.connection import get_db
from services import pricing_catalog


PLATFORM_PRODUCT = "credit_basic"


def set_flags(*, dual: bool, quote_required: bool, channel: bool) -> None:
    values = {
        "PRICING_DUAL_SSOT_ENABLED": dual,
        "PRICING_QUOTE_REQUIRED": quote_required,
        "CHANNEL_PRICING_ENABLED": channel,
    }
    with get_db() as conn:
        cur = conn.cursor()
        for key, value in values.items():
            cur.execute(
                "UPDATE system_settings SET value=%s, updated_at=NOW() WHERE key=%s",
                ("true" if value else "false", key),
            )
        cur.execute(
            """UPDATE system_settings
               SET value=(COALESCE(NULLIF(value,''),'0')::bigint + 1)::text
               WHERE key='pricing_config_epoch'"""
        )
    pricing_ssot_flags.invalidate()


def publish_retail(
    scope_key: str,
    *,
    version_code: str = "retail-v1",
    retail_cents: int = 180000,
    points: int = 195000,
    agent_user_id: int = 200,
) -> int:
    from services.agent_pricing import resolve_agent_effective_retail_cost

    with get_db() as conn:
        cost_context = resolve_agent_effective_retail_cost(
            conn.cursor(),
            agent_user_id=int(agent_user_id),
            points_granted=int(points),
        )
    effective_cost = int(cost_context["effective_cost_cents"])
    source_ref = {
        "kind": "agent_retail_sku",
        "agent_user_id": agent_user_id,
        "retail_sku_id": f"RSKU-TEST-{agent_user_id}",
        "retail_sku_version": 1,
        "sku_template_id": 1,
        "override_id": 701,
        "template_code": PLATFORM_PRODUCT,
        "sku_type": "credit_pack",
        "wholesale_cents": effective_cost,
        "effective_cost_version": str(cost_context["cost_basis_version"]),
        "retail_cents": retail_cents,
        "points_granted": points,
        "tool_points": points,
        "publish_points": 0,
    }
    version_id = pricing_catalog.create_draft_version(
        catalog_type="retail",
        scope_key=scope_key,
        version_code=version_code,
        entries=[{
            "product_code": PLATFORM_PRODUCT,
            "base_price_cents": retail_cents,
            "multiplier_bps": 10000,
            "paid_points": points,
            "bonus_points": 0,
            "cost_floor_cents": effective_cost,
        }],
        calc_meta={
            "source_fingerprint": f"retail-source-{version_code}",
            "config_epoch": int(version_code.rsplit("v", 1)[-1])
            if version_code.rsplit("v", 1)[-1].isdigit() else 1,
        },
    )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pricing_catalog_entries SET source_ref_jsonb=%s::jsonb WHERE version_id=%s",
            (json.dumps(source_ref), version_id),
        )
    pricing_catalog.publish_version(version_id, approved_by=900)
    return version_id


def publish_procurement(
    *,
    version_code: str = "proc-v1",
    amount_cents: int = 120000,
    points: int = 195000,
    bonus_points: int = 0,
    reward_rate: float = 0,
) -> int:
    pricing_config: dict[str, Any] = {
        "wholesale_numer": amount_cents,
        "wholesale_denom": points,
        "agent_purchase_bonus_rate": reward_rate,
        "bonus_validity_months": 12,
        "founding": {
            "cap": 10,
            "min_first_order_yuan": 500,
            "first_order_extra_bonus": 0,
        },
        "agent_tier_config": {},
    }
    option = {
        "option_id": PLATFORM_PRODUCT,
        "amount_cents": amount_cents,
        "reward_eligible": bool(bonus_points),
    }
    source_ref = {
        "kind": "agent_purchase_option",
        "option_id": PLATFORM_PRODUCT,
        "amount_cents": amount_cents,
        "option": option,
    }
    version_id = pricing_catalog.create_draft_version(
        catalog_type="procurement",
        scope_key="PLATFORM_BASE",
        version_code=version_code,
        entries=[{
            "product_code": PLATFORM_PRODUCT,
            "base_price_cents": amount_cents,
            "multiplier_bps": 10000,
            "paid_points": points,
            "bonus_points": bonus_points,
        }],
        calc_meta={
            "source_fingerprint": f"proc-source-{version_code}",
            "config_epoch": int(version_code.rsplit("v", 1)[-1])
            if version_code.rsplit("v", 1)[-1].isdigit() else 1,
            "pricing_config_snapshot": pricing_config,
        },
    )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pricing_catalog_entries SET source_ref_jsonb=%s::jsonb WHERE version_id=%s",
            (json.dumps(source_ref), version_id),
        )
    pricing_catalog.publish_version(version_id, approved_by=900)
    return version_id


def scalar(sql: str, params=()):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return None
        return next(iter(row.values()))
