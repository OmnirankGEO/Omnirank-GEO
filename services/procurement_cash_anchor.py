"""Canonical cash-anchored procurement calculator.

The buyer-entered/published cash amount never changes.  The current relationship
path changes only the maximum paid inventory points that can be delivered safely.
All arithmetic uses integer cents and exact rational products; no float or rounded
per-hop amount is fed back into the next hop.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping


CASH_ANCHOR_SEMANTIC_VERSION = "procurement-cash-anchor-v2"
MAX_PAYABLE_AMOUNT_CENTS = 2_000_000_000
MAX_INVENTORY_POINTS = 9_000_000_000_000_000_000


class CashAnchorError(ValueError):
    """The persisted pricing inputs cannot produce a safe cash-anchored quote."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


def _ceil_ratio(value: int, numerator: int, denominator: int) -> int:
    if value < 0 or numerator < 0 or denominator <= 0:
        raise CashAnchorError("CASH_ANCHOR_RATIO_INVALID", "采购换算比例非法")
    return (int(value) * int(numerator) + int(denominator) - 1) // int(denominator)


def _normalize_path(path: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for index, raw in enumerate(path):
        multiplier = int(raw.get("multiplier_bps") or raw.get("edge_multiplier_bps") or 0)
        discount = int(raw.get("promotion_discount_bps") or 10000)
        if multiplier < 10000 or not 1 <= discount <= 10000:
            raise CashAnchorError(
                "CASH_ANCHOR_PATH_INVALID",
                f"第 {index + 1} 跳采购关系或活动比例非法",
            )
        normalized.append({
            "seller_user_id": (
                int(raw["seller_user_id"]) if raw.get("seller_user_id") is not None else None
            ),
            "buyer_user_id": (
                int(raw["buyer_user_id"]) if raw.get("buyer_user_id") is not None else None
            ),
            "relationship_id": (
                int(raw["relationship_id"]) if raw.get("relationship_id") is not None else None
            ),
            "relationship_version": (
                str(raw["relationship_version"]) if raw.get("relationship_version") is not None else None
            ),
            "source_kind": str(raw.get("source_kind") or "relationship"),
            "multiplier_bps": multiplier,
            "promotion_discount_bps": discount,
        })
    return normalized


def calculate_cash_anchor(
    *,
    buyer_user_id: int,
    payable_amount_cents: int,
    platform_points_numer: int,
    platform_points_denom: int,
    catalog_version: str,
    relationship_path: Iterable[Mapping[str, Any]],
    platform_promotion_discount_bps: int = 10000,
    promotion_snapshot: Mapping[str, Any] | None = None,
    purchase_discount_snapshot: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return the maximum safe paid points and immutable internal cash path.

    The published root ratio is ``points = cash * platform_points_numer /
    platform_points_denom``.  Relationship multipliers are payable-cost factors,
    so they divide the delivered paid points.  Paid points are floored exactly once;
    each cumulative internal cash obligation is then ceiled independently from the
    same rational origin.  This prevents double multiplication and rounding drift.
    """

    buyer = int(buyer_user_id)
    payable = int(payable_amount_cents)
    points_numer = int(platform_points_numer)
    points_denom = int(platform_points_denom)
    platform_discount = int(platform_promotion_discount_bps)
    version = str(catalog_version or "").strip()
    if (
        buyer <= 0
        or payable <= 0
        or payable > MAX_PAYABLE_AMOUNT_CENTS
        or points_numer <= 0
        or points_denom <= 0
        or not version
        or not 1 <= platform_discount <= 10000
    ):
        raise CashAnchorError("CASH_ANCHOR_INPUT_INVALID", "采购金额或已发布换算快照非法")

    path = _normalize_path(relationship_path)
    raw_discount = dict(purchase_discount_snapshot or {})
    discount_snapshot = {
        "source": str(raw_discount.get("source") or "explicit_ratio"),
        "numer": int(raw_discount.get("numer") or points_denom),
        "denom": int(raw_discount.get("denom") or points_numer),
        "discount_version": str(
            raw_discount.get("discount_version")
            or hashlib.sha256(
                f"explicit:{points_denom}:{points_numer}:{version}".encode("utf-8")
            ).hexdigest()
        ),
        "override_updated_at": (
            str(raw_discount["override_updated_at"])
            if raw_discount.get("override_updated_at") is not None else None
        ),
        "published_catalog_version": str(
            raw_discount.get("published_catalog_version") or version
        ),
        "published_global_numer": int(
            raw_discount.get("published_global_numer") or points_denom
        ),
        "published_global_denom": int(
            raw_discount.get("published_global_denom") or points_numer
        ),
    }
    if (
        discount_snapshot["numer"] != points_denom
        or discount_snapshot["denom"] != points_numer
        or not discount_snapshot["discount_version"]
    ):
        raise CashAnchorError(
            "CASH_ANCHOR_DISCOUNT_INVALID",
            "采购折扣快照与根换算比例不一致",
        )
    # Root cost ratio in cents per point is points_denom / points_numer.
    cumulative_cost_numer = points_denom * platform_discount
    cumulative_cost_denom = points_numer * 10000
    for step in path:
        cumulative_cost_numer *= int(step["multiplier_bps"]) * int(step["promotion_discount_bps"])
        cumulative_cost_denom *= 10000 * 10000

    paid_points = payable * cumulative_cost_denom // cumulative_cost_numer
    if paid_points <= 0:
        raise CashAnchorError(
            "CASH_ANCHOR_POINTS_TOO_SMALL",
            "本次金额不足以换算 1 算力，请提高进货金额",
        )
    if paid_points > MAX_INVENTORY_POINTS:
        raise CashAnchorError("CASH_ANCHOR_POINTS_OVERFLOW", "本次换算算力超出系统上限")

    root_cost = _ceil_ratio(paid_points, points_denom * platform_discount, points_numer * 10000)
    if root_cost <= 0 or root_cost > payable:
        raise CashAnchorError("CASH_ANCHOR_ROOT_COST_INVALID", "根采购成本无法由本次实付覆盖")

    hop_costs: List[Dict[str, Any]] = []
    running_numer = points_denom * platform_discount
    running_denom = points_numer * 10000
    previous_cost = root_cost
    for index, step in enumerate(path):
        normal_numer = running_numer * int(step["multiplier_bps"])
        normal_denom = running_denom * 10000
        normal_sale = _ceil_ratio(paid_points, normal_numer, normal_denom)
        running_numer = normal_numer * int(step["promotion_discount_bps"])
        running_denom = normal_denom * 10000
        sale = _ceil_ratio(paid_points, running_numer, running_denom)
        if sale < previous_cost or sale > payable:
            raise CashAnchorError(
                "CASH_ANCHOR_HOP_COST_INVALID",
                f"第 {index + 1} 跳内部成本无法由实付金额安全覆盖",
            )
        hop_costs.append({
            **step,
            "standard_reference_cents": previous_cost,
            "normal_sale_reference_cents": normal_sale,
            "sale_reference_cents": sale,
        })
        previous_cost = sale

    covered_cost = int(hop_costs[-1]["sale_reference_cents"]) if hop_costs else root_cost
    if covered_cost > payable:
        raise CashAnchorError("CASH_ANCHOR_OVERSPEND", "内部采购成本超过本次实付")
    remainder = payable - covered_cost
    direct_seller_acquisition = (
        int(hop_costs[-2]["sale_reference_cents"])
        if len(hop_costs) > 1
        else root_cost
    )

    fingerprint_payload = {
        "semantic_version": CASH_ANCHOR_SEMANTIC_VERSION,
        "buyer_user_id": buyer,
        "payable_amount_cents": payable,
        "platform_points_numer": points_numer,
        "platform_points_denom": points_denom,
        "catalog_version": version,
        "platform_promotion_discount_bps": platform_discount,
        "promotion_snapshot": dict(promotion_snapshot or {}),
        "purchase_discount_snapshot": discount_snapshot,
        "relationship_path": path,
    }
    canonical = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        **fingerprint_payload,
        "cash_anchor_fingerprint": fingerprint,
        "paid_inventory_points": int(paid_points),
        "platform_reference_amount_cents": int(root_cost),
        "covered_path_cost_cents": int(covered_cost),
        "direct_seller_acquisition_cents": int(direct_seller_acquisition),
        "rounding_remainder_cents": int(remainder),
        "hop_costs": hop_costs,
    }


def public_cash_anchor(result: Mapping[str, Any]) -> Dict[str, int | str]:
    """Minimal provider-safe projection; never expose the internal path."""

    return {
        "semantic_version": str(result["semantic_version"]),
        "payable_amount_cents": int(result["payable_amount_cents"]),
        "paid_inventory_points": int(result["paid_inventory_points"]),
    }
