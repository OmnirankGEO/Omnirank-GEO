"""价格报价服务(§9.2 · 不可篡改快照 + 防重放 + 幂等)

标准流程:
  读价目表 → issue_quote()(后端算价·锁 price_quote_id + 有效期 + calculation_hash)
  → 用户确认 → 下单只传 price_quote_id → consume_quote()(重校验未过期 + 买方/金额一致 + 一单一报价)

安全不变量(§9.2):
  1. quote_id 是服务端保存的不可枚举随机 ID;真值以 DB 行为准,绝不只信前端回传 JSON/hash。
  2. 报价绑定买方/平台卖方/渠道关系/商品/数量/币种/用途;任一不一致 → 拒绝。
  3. 一个报价只能创建一个订单(used_order_id 唯一索引兜底 + 条件 UPDATE)。
  4. 报价过期不得续算;必须重新报价再确认。

红线:整数分;不碰 billing/connection/auth;无税。
"""

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from db.connection import get_db
from services import pricing_catalog

logger = logging.getLogger("GEO-PriceQuote")

DEFAULT_TTL_SECONDS = 15 * 60  # §5.2 示例 price_valid_until 00:15
_QUOTE_ALPHABET = "23456789abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
_PROCUREMENT_QUOTE_IDEMPOTENCY_LOCK_NAMESPACE = 920719
_RETAIL_CASH_QUOTE_IDEMPOTENCY_LOCK_NAMESPACE = 920720


def _gen_quote_id() -> str:
    return "pq_" + "".join(secrets.choice(_QUOTE_ALPHABET) for _ in range(24))


def compute_calculation_hash(payload: Dict[str, Any]) -> str:
    """对定价关键输入做 sha256(完整性校验 · 真值仍以 DB 行为准)。
    [审核 P3] 只用【已持久化到 price_quotes 行】的字段 → consume 时可从行原样重算比对(真校验·非装饰)。"""
    canon = json.dumps(
        {k: payload[k] for k in sorted(payload)}, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _json_dict(value: Any) -> Dict[str, Any]:
    """Normalize psycopg2 JSONB values without accepting non-object payloads."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _dual_enabled() -> bool:
    """Flag lookup isolated for legacy unit callers that exercise the service directly."""
    from config.pricing_ssot_flags import dual_ssot_enabled

    return bool(dual_ssot_enabled())


def _hash_inputs(*, quote_type, product_code, quantity, buyer_user_id, catalog_version,
                 base_price_cents, multiplier_bps, final_price_cents, points_granted,
                 channel_relationship_version, upstream_cost_basis_cents=None) -> Dict[str, Any]:
    """哈希输入 = price_quotes 行可复原的定价关键字段(issue 与 verify 共用同一构造)。"""
    return {
        "quote_type": quote_type, "product_code": product_code, "quantity": int(quantity),
        "buyer_user_id": int(buyer_user_id), "catalog_version": catalog_version,
        "base_price_cents": int(base_price_cents), "multiplier_bps": int(multiplier_bps) if multiplier_bps is not None else None,
        "final_price_cents": int(final_price_cents), "points_granted": int(points_granted),
        "channel_relationship_version": channel_relationship_version,
        "upstream_cost_basis_cents": int(upstream_cost_basis_cents) if upstream_cost_basis_cents is not None else None,
    }


def _hash_inputs_v2(q: Dict[str, Any]) -> Dict[str, Any]:
    """Hash the complete immutable quote snapshot introduced by the wiring cutover.

    `calculation_hash` is deliberately excluded from the embedded JSON to avoid a
    self-referential digest. Legacy rows without pricing_snapshot_jsonb continue to
    use the original scalar-field digest in `_hash_from_row`.
    """
    snapshot = _json_dict(q.get("pricing_snapshot_jsonb"))
    snapshot.pop("calculation_hash", None)
    return {"quote_snapshot": snapshot}


def _compute_v2_hash(payload: Dict[str, Any]) -> str:
    """Canonical recursive JSON hash; PostgreSQL JSONB may reorder nested keys."""
    canon = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _hash_from_row(q: Dict[str, Any]) -> str:
    if _json_dict(q.get("pricing_snapshot_jsonb")):
        return _compute_v2_hash(_hash_inputs_v2(q))
    return compute_calculation_hash(_hash_inputs(
        quote_type=q["quote_type"], product_code=q["product_code"], quantity=q["quantity"],
        buyer_user_id=q["buyer_user_id"], catalog_version=q["catalog_version"],
        base_price_cents=q["base_price_cents"], multiplier_bps=q["effective_multiplier_bps"],
        final_price_cents=q["final_price_cents"], points_granted=q["points_granted"],
        channel_relationship_version=q["channel_relationship_version"],
        upstream_cost_basis_cents=q.get("upstream_cost_basis_cents"),
    ))


def quote_snapshot(q: Dict[str, Any]) -> Dict[str, Any]:
    """Return the immutable quote JSON object (empty for pre-cutover rows)."""
    return _json_dict(q.get("pricing_snapshot_jsonb"))


def quote_source_ref(q: Dict[str, Any]) -> Dict[str, Any]:
    return _json_dict(quote_snapshot(q).get("source_ref"))


def quote_order_pricing_snapshot(q: Dict[str, Any], *, required: bool = False) -> Dict[str, Any]:
    """Extract the callback-safe order snapshot pinned inside a persisted quote."""
    snap = _json_dict(quote_snapshot(q).get("order_pricing_snapshot"))
    if required and not snap:
        raise QuoteError("报价缺少不可变订单快照 · 请重新报价")
    return snap


def public_procurement_reward_description(snapshot: Dict[str, Any]) -> str:
    """Provider-safe label derived only from the frozen bonus quantity.

    Administrative tier descriptions are not part of the public procurement
    contract: they may contain internal pricing vocabulary and must never be
    reflected into provider-visible DTOs.
    """

    return (
        "本档含赠送库存"
        if int(snapshot.get("bonus_points") or 0) > 0
        else "本档暂无固定奖励"
    )


def _retail_order_snapshot(
    *, source_ref: Dict[str, Any], product_code: str, final_cents: int,
    points_granted: int, bonus_points: int,
) -> Dict[str, Any]:
    """Materialize the existing SettlementOrchestrator contract from catalog provenance."""
    required = (
        "agent_user_id", "retail_sku_id", "retail_sku_version", "override_id",
        "wholesale_cents", "retail_cents", "points_granted",
    )
    missing = [key for key in required if source_ref.get(key) is None]
    if missing:
        if _dual_enabled():
            raise QuoteError(f"零售价目来源快照缺字段: {','.join(missing)}")
        # Direct service tests and genuinely pre-cutover rows remain consumable while
        # all cutover flags are off. They are never accepted by the dual wallet path.
        return {
            "sku_key": product_code,
            "points_granted": int(points_granted),
            "tool_points": int(points_granted),
            "publish_points": 0,
            "bonus_points": int(bonus_points),
            "retail_cents": int(final_cents),
            "customer_paid_cents": int(final_cents),
            "amount_source": "legacy_price_quote",
        }
    if int(source_ref["retail_cents"]) <= 0 or int(source_ref["points_granted"]) <= 0:
        raise QuoteError("零售价目来源快照非法")
    if int(source_ref["points_granted"]) != int(points_granted):
        raise QuoteError("零售价目与来源算力不一致")
    return {
        "retail_snapshot_version": 2,
        "agent_user_id": int(source_ref["agent_user_id"]),
        "retail_sku_id": str(source_ref["retail_sku_id"]),
        "retail_sku_version": int(source_ref["retail_sku_version"]),
        "sku_template_id": (
            int(source_ref["sku_template_id"])
            if source_ref.get("sku_template_id") is not None else None
        ),
        "source_template_id": (
            int(source_ref["source_template_id"])
            if source_ref.get("source_template_id") is not None else None
        ),
        "override_id": int(source_ref["override_id"]),
        "sku_key": str(product_code),
        "sku_type": "credit_pack",
        "points_granted": int(points_granted),
        "wholesale_cents": int(source_ref["wholesale_cents"]),
        "retail_cents": int(final_cents),
        "tool_points": int(source_ref.get("tool_points", points_granted)),
        "publish_points": int(source_ref.get("publish_points", 0)),
        "bonus_points": int(bonus_points),
        "customer_paid_cents": int(final_cents),
        # SettlementOrchestrator recognizes this as a locked SKU order even when
        # the intentionally template-free recharge_orders.sku_template_id is NULL.
        "amount_source": "sku_snapshot",
    }


def _retail_cash_order_snapshot(
    *,
    source_ref: Dict[str, Any],
    final_cents: int,
    points_granted: int,
) -> Dict[str, Any]:
    required = (
        "agent_user_id", "wholesale_cents", "retail_cents", "points_granted",
        "cash_anchor_fingerprint", "cash_anchor_semantic_version",
        "effective_cost_version", "markup_version",
    )
    missing = [key for key in required if source_ref.get(key) is None]
    if missing:
        raise QuoteError(f"自由充值报价快照缺字段: {','.join(missing)}")
    if int(source_ref["retail_cents"]) != int(final_cents):
        raise QuoteError("自由充值金额快照不一致")
    if int(source_ref["points_granted"]) != int(points_granted):
        raise QuoteError("自由充值算力快照不一致")
    return {
        "retail_snapshot_version": 3,
        "retail_pricing_mode": "custom_amount",
        "cash_anchor_source": "customer_entered_cash",
        "agent_user_id": int(source_ref["agent_user_id"]),
        "retail_sku_id": "RETAIL_CUSTOM_AMOUNT",
        "retail_sku_version": 1,
        "sku_template_id": None,
        "source_template_id": None,
        "override_id": None,
        "sku_key": "RETAIL_CUSTOM_AMOUNT",
        "sku_type": "credit_custom_amount",
        "points_granted": int(points_granted),
        "wholesale_cents": int(source_ref["wholesale_cents"]),
        "retail_cents": int(final_cents),
        "tool_points": int(points_granted),
        "publish_points": 0,
        "bonus_points": 0,
        "customer_paid_cents": int(final_cents),
        "cash_anchor_fingerprint": str(source_ref["cash_anchor_fingerprint"]),
        "cash_anchor_semantic_version": str(source_ref["cash_anchor_semantic_version"]),
        "effective_cost_version": str(source_ref["effective_cost_version"]),
        "markup_version": str(source_ref["markup_version"]),
        # Keep the established callback routing marker.  The additional pricing
        # mode distinguishes custom cash from a fixed provider package.
        "amount_source": "sku_snapshot",
    }


class QuoteError(Exception):
    """报价/消费阶段的可预期业务错误(调用方转 4xx)。"""


def _lock_retail_cash_quote_idempotency(
    cur, *, buyer_user_id: int, idempotency_key: Optional[str],
) -> None:
    if not idempotency_key:
        return
    material = f"retail-cash:{int(buyer_user_id)}:{str(idempotency_key)}"
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (_RETAIL_CASH_QUOTE_IDEMPOTENCY_LOCK_NAMESPACE, material),
    )


def _lock_procurement_quote_idempotency(
    cur, *, buyer_user_id: int, idempotency_key: Optional[str],
) -> None:
    """Serialize every use of one buyer-owned procurement idempotency key."""
    if not idempotency_key:
        return
    material = f"procurement:{int(buyer_user_id)}:{str(idempotency_key)}"
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (_PROCUREMENT_QUOTE_IDEMPOTENCY_LOCK_NAMESPACE, material),
    )


def _retail_cost_guard_required(cur, source_ref: Dict[str, Any]) -> bool:
    if str(source_ref.get("effective_cost_version") or "").strip():
        return True
    from config.pricing_ssot_flags import pricing_flags_snapshot

    return bool(pricing_flags_snapshot(cur).get("PRICING_DUAL_SSOT_ENABLED", False))


def assert_retail_catalog_entry_current(
    cur,
    *,
    source_ref: Dict[str, Any],
    final_cents: int,
) -> Dict[str, Any]:
    """Fail closed when a published retail entry no longer matches current cost.

    Only an opaque cost-basis version is compared. Error messages intentionally omit
    the platform root cost, relationship path, ids, multiplier and spread.
    """
    required = (
        "agent_user_id",
        "points_granted",
        "wholesale_cents",
        "effective_cost_version",
    )
    if any(source_ref.get(key) is None for key in required):
        raise QuoteError("服务方价目成本版本缺失 · 请服务方重新发布")
    from services.agent_pricing import resolve_agent_effective_retail_cost

    try:
        current = resolve_agent_effective_retail_cost(
            cur,
            agent_user_id=int(source_ref["agent_user_id"]),
            points_granted=int(source_ref["points_granted"]),
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise QuoteError("服务方当前成本不可核验 · 请稍后重试") from exc
    if (
        int(source_ref["wholesale_cents"]) != int(current["effective_cost_cents"])
        or str(source_ref["effective_cost_version"])
        != str(current["cost_basis_version"])
    ):
        raise QuoteError("服务方成本已变化 · 请服务方重新发布价目")
    if int(final_cents) <= int(current["effective_cost_cents"]):
        raise QuoteError("服务方价目不再满足发布底线 · 请服务方重新发布")
    return current


def assert_retail_cash_anchor_current(
    cur, *, quote: Dict[str, Any], source_ref: Dict[str, Any],
) -> Dict[str, Any]:
    """Recompute a still-unconsumed custom cash quote without leaking internals."""
    required = (
        "agent_user_id", "retail_cents", "points_granted", "wholesale_cents",
        "effective_cost_version", "markup_version", "cash_anchor_fingerprint",
    )
    if any(source_ref.get(key) is None for key in required):
        raise QuoteError("自由充值报价版本缺失 · 请重新报价")
    from services.agent_pricing import resolve_agent_retail_cash_anchor
    try:
        current = resolve_agent_retail_cash_anchor(
            cur,
            agent_user_id=int(source_ref["agent_user_id"]),
            amount_cents=int(source_ref["retail_cents"]),
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise QuoteError("当前价格配置暂不可用 · 请稍后重试") from exc
    comparisons = (
        ("points_granted", current["points_granted"]),
        ("wholesale_cents", current["effective_cost_cents"]),
        ("effective_cost_version", current["cost_basis_version"]),
        ("markup_version", current["markup_version"]),
        ("cash_anchor_fingerprint", current["cash_anchor_fingerprint"]),
    )
    if any(str(source_ref[key]) != str(value) for key, value in comparisons):
        raise QuoteError("当前价格配置已变化 · 请重新计算到账算力")
    if int(quote["final_price_cents"]) != int(source_ref["retail_cents"]):
        raise QuoteError("自由充值金额快照不一致 · 拒绝")
    return current


def preview_published_quote_entry(
    *,
    quote_type: str,
    entry: Dict[str, Any],
    product_code: str,
    quantity: int = 1,
) -> Dict[str, Any]:
    """Run the real quote arithmetic and retail snapshot gates without writing.

    Readiness checks call this same function used by ``issue_quote``.  It must not
    insert a ``price_quotes`` row or create/repair catalog state.
    """

    if quote_type not in ("procurement", "retail"):
        raise QuoteError(f"invalid quote_type {quote_type}")
    if int(quantity) < 1:
        raise QuoteError("quantity 必须 ≥ 1")
    try:
        unit_final = int(entry["final_price_cents"])
        unit_points = int(entry["paid_points"])
        unit_bonus = int(entry.get("bonus_points") or 0)
        base_cents = int(entry["base_price_cents"])
        mult_bps = int(entry["multiplier_bps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise QuoteError("价目字段不完整") from exc

    if unit_final <= 0 or unit_points <= 0:
        raise QuoteError("价目异常(价格/算力 ≤ 0)")
    floor = entry.get("cost_floor_cents")
    if quote_type == "retail" and floor is not None and unit_final <= int(floor):
        raise QuoteError("最终售价必须高于成本底线 · 拒绝报价(§11.1)")

    final_cents = unit_final * int(quantity)
    points_granted = unit_points * int(quantity)
    bonus_points = unit_bonus * int(quantity)
    if final_cents > 2_000_000_000 or base_cents > 2_000_000_000:
        raise QuoteError("报价金额超出单笔上限 · 请减少数量或联系客服")

    source_ref = _json_dict(entry.get("source_ref_jsonb"))
    order_snapshot = None
    if quote_type == "retail":
        if source_ref and int(source_ref.get("retail_cents") or 0) * int(quantity) != final_cents:
            raise QuoteError("零售价目来源金额与已发布价格不一致")
        order_snapshot = _retail_order_snapshot(
            source_ref=source_ref,
            product_code=product_code,
            final_cents=final_cents,
            points_granted=points_granted,
            bonus_points=bonus_points,
        )

    return {
        "unit_final_cents": unit_final,
        "unit_points": unit_points,
        "unit_bonus_points": unit_bonus,
        "base_price_cents": base_cents,
        "multiplier_bps": mult_bps,
        "final_price_cents": final_cents,
        "points_granted": points_granted,
        "bonus_points": bonus_points,
        "source_ref": source_ref,
        "order_pricing_snapshot": order_snapshot,
    }


def issue_quote(
    *,
    quote_type: str,               # 'procurement' | 'retail'
    scope_key: str,                # procurement: channel_account_code/'PLATFORM_BASE';retail: service_account_code
    product_code: str,
    buyer_user_id: int,
    quantity: int = 1,
    channel_account_code: Optional[str] = None,
    channel_beneficiary_user_id: Optional[int] = None,
    channel_relationship_version: Optional[str] = None,
    seller_policy_version: Optional[str] = None,
    commercial_service_source: Optional[str] = None,
    retail_seller_user_id: Optional[int] = None,
    consumer_policy_acknowledged: bool = False,
    purchase_terms_acceptance_id: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """从已发布价目表 SSOT 算一次报价并落库。价目缺失 → QuoteError(fail-closed 不回退硬编码)。"""
    if quote_type not in ("procurement", "retail"):
        raise QuoteError(f"invalid quote_type {quote_type}")
    if quantity < 1:
        raise QuoteError("quantity 必须 ≥ 1")

    with get_db() as conn:
        cur = conn.cursor()
        # 幂等:同买方同 idem + 同商品/数量 已有未消费未过期报价 → 直接返回它(§9.2.4)
        # [审核 P2/P1] 必须匹配 product_code+quantity(否则同 key 换商品会返回错商品报价);
        #   idem 唯一索引已改为非唯一(见 migration),key 复用/过期后落 INSERT 也不再 500。
        if idempotency_key:
            cur.execute(
                """SELECT * FROM price_quotes
                   WHERE buyer_user_id=%s AND quote_type=%s AND idempotency_key=%s
                     AND product_code=%s AND quantity=%s AND status='issued' AND expires_at > NOW()
                   ORDER BY created_at DESC LIMIT 1""",
                (buyer_user_id, quote_type, idempotency_key, product_code, quantity),
            )
            existing = cur.fetchone()
            if existing:
                existing_dict = dict(existing)
                if quote_type != "retail":
                    return existing_dict
                from config.dealer_inventory_resale_flags import enabled as dealer_resale_enabled
                resale_now = dealer_resale_enabled(cur)
                existing_order = quote_order_pricing_snapshot(existing_dict)
                existing_route = existing_order.get("commercial_service_source")
                if (
                    commercial_service_source is not None
                    and existing_route != commercial_service_source
                ):
                    raise QuoteError("该幂等键对应的商业服务路由已变化 · 请使用新幂等键重新报价")
                if purchase_terms_acceptance_id and (
                    existing_order.get("terms_acceptance_id") != purchase_terms_acceptance_id
                ):
                    raise QuoteError("该幂等键对应另一份购买协议确认 · 请使用新幂等键重新报价")
                existing_resale = existing_order.get("consumer_resale_mode") is True
                if resale_now == existing_resale:
                    existing_source = quote_source_ref(existing_dict)
                    if _retail_cost_guard_required(cur, existing_source):
                        assert_retail_catalog_entry_current(
                            cur,
                            source_ref=existing_source,
                            final_cents=int(existing_dict["final_price_cents"]),
                        )
                    return existing_dict
                raise QuoteError("该幂等键对应的报价与当前逐级转售开关状态不一致 · 请使用新幂等键重新报价")

        entry_pair = pricing_catalog.get_published_entry(quote_type, scope_key, product_code, cur=cur)
        entry, version = (entry_pair if entry_pair else (None, None))
        if not entry:
            raise QuoteError(f"无已发布价目({quote_type}/{scope_key}/{product_code})· 拒绝报价")

        preview = preview_published_quote_entry(
            quote_type=quote_type,
            entry=entry,
            product_code=product_code,
            quantity=quantity,
        )
        unit_final = int(preview["unit_final_cents"])
        base_cents = int(preview["base_price_cents"])
        mult_bps = int(preview["multiplier_bps"])
        final_cents = int(preview["final_price_cents"])
        points_granted = int(preview["points_granted"])
        bonus_points = int(preview["bonus_points"])
        source_ref = dict(preview["source_ref"])
        order_snapshot = preview["order_pricing_snapshot"]
        consumer_terms = None
        if quote_type == "retail":
            if _retail_cost_guard_required(cur, source_ref):
                assert_retail_catalog_entry_current(
                    cur,
                    source_ref=source_ref,
                    final_cents=final_cents,
                )
            purchase_acceptance = None
            if purchase_terms_acceptance_id:
                from services.legal_agreements import validate_purchase_acceptance
                try:
                    purchase_acceptance = validate_purchase_acceptance(
                        cur,
                        acceptance_id=purchase_terms_acceptance_id,
                        user_id=int(buyer_user_id),
                        expected_surface="customer-recharge",
                    )
                except ValueError as exc:
                    raise QuoteError(str(exc)) from exc
            if source_ref and int(source_ref.get("retail_cents") or 0) * int(quantity) != final_cents:
                raise QuoteError("零售价目来源金额与已发布价格不一致")
            from config.dealer_inventory_resale_flags import enabled as dealer_resale_enabled
            if dealer_resale_enabled(cur):
                if retail_seller_user_id is None:
                    raise QuoteError("零售报价缺少直属服务方")
                source_seller = source_ref.get("agent_user_id")
                if source_seller is None or int(source_seller) != int(retail_seller_user_id):
                    raise QuoteError("零售价目服务方与客户绑定不一致")
                wholesale_reference = int(source_ref.get("wholesale_cents") or 0)
                if wholesale_reference <= 0:
                    raise QuoteError("已发布零售价目缺少厂家标准参考价 · 拒绝报价")
                from services import dealer_inventory_resale
                try:
                    consumer_terms = dealer_inventory_resale.build_consumer_quote_terms(
                        cur,
                        seller_user_id=int(retail_seller_user_id),
                        consumer_user_id=int(buyer_user_id),
                        points=points_granted + bonus_points,
                        catalog_reference_amount_cents=final_cents,
                        pricing_version=str(version["version_code"]),
                        digital_goods_acknowledged=bool(consumer_policy_acknowledged),
                        product_code=str(product_code),
                        platform_reference_amount_cents=wholesale_reference * int(quantity),
                        catalog_version_id=str(version["id"]),
                        catalog_entry_id=(str(entry["id"]) if entry.get("id") else None),
                    )
                except dealer_inventory_resale.ResaleError as exc:
                    raise QuoteError(exc.message) from exc
                if consumer_terms:
                    final_cents = int(consumer_terms["sale_amount_cents"])
                    seller_policy_version = str(consumer_terms["seller_policy_version"])
            order_snapshot = _retail_order_snapshot(
                source_ref=source_ref,
                product_code=product_code,
                final_cents=final_cents,
                points_granted=points_granted,
                bonus_points=bonus_points,
            )
            if consumer_terms:
                order_snapshot.update(consumer_terms)
                order_snapshot["customer_paid_cents"] = final_cents
                order_snapshot["retail_cents"] = final_cents
            if purchase_acceptance:
                order_snapshot.update({
                    "terms_acceptance_id": purchase_acceptance["acceptance_id"],
                    "terms_version": purchase_acceptance["agreement_version"],
                    "terms_content_hash": purchase_acceptance["content_hash"],
                    "terms_accepted_at": purchase_acceptance["accepted_at"].isoformat(),
                    "terms_surface": purchase_acceptance["surface"],
                })

        total_catalog_cents = unit_final * quantity
        effective_mult_bps = mult_bps
        seller_legal_entity_id = "PLATFORM"
        if consumer_terms:
            effective_mult_bps = (final_cents * 10000 + total_catalog_cents // 2) // total_catalog_cents
            seller_legal_entity_id = f"USER:{int(consumer_terms['seller_user_id'])}"

        return _insert_quote(
            cur, quote_type=quote_type, scope_key=scope_key, product_code=product_code,
            buyer_user_id=buyer_user_id, quantity=quantity, catalog_version=version["version_code"],
            catalog_version_id=version["id"], base_cents=base_cents, mult_bps=effective_mult_bps,
            final_cents=final_cents, points_granted=points_granted, bonus_points=bonus_points,
            channel_account_code=channel_account_code, channel_beneficiary_user_id=channel_beneficiary_user_id,
            channel_relationship_version=channel_relationship_version, seller_policy_version=seller_policy_version,
            ttl_seconds=ttl_seconds, idempotency_key=idempotency_key, source_ref=source_ref,
            catalog_entry_id=entry.get("id"), order_pricing_snapshot=order_snapshot,
            commercial_service_source=commercial_service_source,
            seller_legal_entity_id=seller_legal_entity_id,
        )


def issue_retail_cash_quote(
    *,
    scope_key: str,
    buyer_user_id: int,
    retail_seller_user_id: int,
    amount_cents: int,
    commercial_service_source: str,
    consumer_policy_acknowledged: bool,
    purchase_terms_acceptance_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist an exact-cash C-end quote whose coefficient changes only points."""
    amount_cents = int(amount_cents)
    buyer_user_id = int(buyer_user_id)
    retail_seller_user_id = int(retail_seller_user_id)
    if consumer_policy_acknowledged is not True:
        raise QuoteError("请先确认数字商品交付与退款规则")
    if not scope_key or commercial_service_source not in ("explicit_binding", "platform_direct"):
        raise QuoteError("当前账户价格配置暂不可用")
    with get_db() as conn:
        cur = conn.cursor()
        _lock_retail_cash_quote_idempotency(
            cur, buyer_user_id=buyer_user_id, idempotency_key=idempotency_key,
        )
        if idempotency_key:
            cur.execute(
                """SELECT * FROM price_quotes
                     WHERE buyer_user_id=%s AND quote_type='retail'
                       AND idempotency_key=%s AND product_code='RETAIL_CUSTOM_AMOUNT'
                       AND status='issued' AND expires_at > NOW()
                     ORDER BY created_at DESC LIMIT 1""",
                (buyer_user_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                existing = dict(existing)
                existing_source = quote_source_ref(existing)
                existing_order = quote_order_pricing_snapshot(existing, required=True)
                if int(existing["final_price_cents"]) != amount_cents:
                    raise QuoteError("该请求编号已用于其他充值金额")
                if int(existing_source.get("agent_user_id") or 0) != retail_seller_user_id:
                    raise QuoteError("该请求编号对应的服务配置已变化")
                if existing_order.get("commercial_service_source") != commercial_service_source:
                    raise QuoteError("该请求编号对应的服务配置已变化")
                if existing_order.get("terms_acceptance_id") != purchase_terms_acceptance_id:
                    raise QuoteError("该请求编号对应另一份协议确认")
                assert_retail_cash_anchor_current(
                    cur, quote=existing, source_ref=existing_source,
                )
                return existing

        from services.agent_pricing import (
            RETAIL_CASH_ANCHOR_SEMANTIC_VERSION,
            resolve_agent_retail_cash_anchor,
        )
        try:
            anchor = resolve_agent_retail_cash_anchor(
                cur,
                agent_user_id=retail_seller_user_id,
                amount_cents=amount_cents,
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise QuoteError("当前价格配置暂不可用 · 请稍后重试") from exc
        version = pricing_catalog.get_published_version("retail", scope_key, cur=cur)
        if not version:
            raise QuoteError("当前账户价格配置暂不可用 · 请稍后重试")
        from services.legal_agreements import validate_purchase_acceptance
        try:
            purchase_acceptance = validate_purchase_acceptance(
                cur,
                acceptance_id=purchase_terms_acceptance_id,
                user_id=buyer_user_id,
                expected_surface="customer-recharge",
            )
        except ValueError as exc:
            raise QuoteError(str(exc)) from exc

        source_ref = {
            "source_kind": "retail_custom_amount",
            "agent_user_id": retail_seller_user_id,
            "retail_sku_id": None,
            "retail_sku_version": None,
            "sku_template_id": None,
            "source_template_id": None,
            "override_id": None,
            "points_granted": int(anchor["points_granted"]),
            "wholesale_cents": int(anchor["effective_cost_cents"]),
            "retail_cents": amount_cents,
            "effective_cost_version": str(anchor["cost_basis_version"]),
            "markup_version": str(anchor["markup_version"]),
            "cash_anchor_fingerprint": str(anchor["cash_anchor_fingerprint"]),
            "cash_anchor_semantic_version": RETAIL_CASH_ANCHOR_SEMANTIC_VERSION,
        }
        order_snapshot = _retail_cash_order_snapshot(
            source_ref=source_ref,
            final_cents=amount_cents,
            points_granted=int(anchor["points_granted"]),
        )
        consumer_terms = None
        seller_policy_version = str(anchor["markup_version"])
        seller_legal_entity_id = "PLATFORM"
        from config.dealer_inventory_resale_flags import enabled as dealer_resale_enabled
        if dealer_resale_enabled(cur):
            from services import dealer_inventory_resale
            try:
                consumer_terms = dealer_inventory_resale.build_consumer_quote_terms(
                    cur,
                    seller_user_id=retail_seller_user_id,
                    consumer_user_id=buyer_user_id,
                    points=int(anchor["points_granted"]),
                    catalog_reference_amount_cents=amount_cents,
                    pricing_version=RETAIL_CASH_ANCHOR_SEMANTIC_VERSION,
                    digital_goods_acknowledged=bool(consumer_policy_acknowledged),
                    product_code="RETAIL_CUSTOM_AMOUNT",
                    platform_reference_amount_cents=int(anchor["_platform_base_cents"]),
                )
            except dealer_inventory_resale.ResaleError as exc:
                raise QuoteError(exc.message) from exc
            if consumer_terms:
                if int(consumer_terms["sale_amount_cents"]) != amount_cents:
                    raise QuoteError("自由充值实付金额被意外改变 · 拒绝")
                order_snapshot.update(consumer_terms)
                seller_policy_version = str(consumer_terms["seller_policy_version"])
                seller_legal_entity_id = f"USER:{retail_seller_user_id}"
        order_snapshot.update({
            "customer_paid_cents": amount_cents,
            "retail_cents": amount_cents,
            "terms_acceptance_id": purchase_acceptance["acceptance_id"],
            "terms_version": purchase_acceptance["agreement_version"],
            "terms_content_hash": purchase_acceptance["content_hash"],
            "terms_accepted_at": purchase_acceptance["accepted_at"].isoformat(),
            "terms_surface": purchase_acceptance["surface"],
        })
        return _insert_quote(
            cur,
            quote_type="retail",
            scope_key=scope_key,
            product_code="RETAIL_CUSTOM_AMOUNT",
            buyer_user_id=buyer_user_id,
            quantity=1,
            catalog_version=str(version["version_code"]),
            catalog_version_id=int(version["id"]),
            catalog_entry_id=None,
            base_cents=int(anchor["effective_cost_cents"]),
            mult_bps=int(anchor["markup_bps"]),
            final_cents=amount_cents,
            points_granted=int(anchor["points_granted"]),
            bonus_points=0,
            seller_policy_version=seller_policy_version,
            ttl_seconds=ttl_seconds,
            idempotency_key=idempotency_key,
            source_ref=source_ref,
            order_pricing_snapshot=order_snapshot,
            commercial_service_source=commercial_service_source,
            seller_legal_entity_id=seller_legal_entity_id,
        )


def _insert_quote(cur, *, quote_type, scope_key, product_code, buyer_user_id, quantity,
                  catalog_version, catalog_version_id, base_cents, mult_bps, final_cents,
                  points_granted, bonus_points, channel_account_code=None,
                  channel_beneficiary_user_id=None, channel_relationship_version=None,
                  seller_policy_version=None, ttl_seconds=DEFAULT_TTL_SECONDS, idempotency_key=None,
                  upstream_cost_basis_cents=None, source_ref=None, catalog_entry_id=None,
                  order_pricing_snapshot=None, commercial_service_source=None,
                  seller_legal_entity_id="PLATFORM"):
    """落库一条报价(整数分 · 平台统一代收，seller 可为直属经销商)。money math 已在调用方完成。
    [审核残留①] upstream_cost_basis_cents:进货报价钉死直属上游有效成本 → 渠道收益 = final − 此值
      在报价那刻锁定,结算时直读不重算(消除 15min 窗口内改渠道系数的漂移)。retail 报价传 None。"""
    # [审核 P2] final_price_cents 是 INTEGER 列 · 超 int32 会 DB 溢出 500 → 提前 fail-closed
    if int(final_cents) > 2_000_000_000 or int(base_cents) > 2_000_000_000:
        raise QuoteError("报价金额超出单笔上限 · 请减少数量或联系客服")
    quote_id = _gen_quote_id()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    order_snapshot = dict(order_pricing_snapshot or {})
    order_snapshot.update({
        "price_quote_id": quote_id,
        "quote_type": quote_type,
        "catalog_version": catalog_version,
        "catalog_version_id": int(catalog_version_id) if catalog_version_id is not None else None,
        "catalog_scope_key": scope_key,
        "catalog_entry_id": int(catalog_entry_id) if catalog_entry_id is not None else None,
        "product_code": product_code,
        "quantity": int(quantity),
        "channel_account_code": channel_account_code,
        "channel_beneficiary_user_id": (
            int(channel_beneficiary_user_id) if channel_beneficiary_user_id is not None else None
        ),
        "channel_relationship_version": channel_relationship_version,
        "upstream_cost_basis_cents": (
            int(upstream_cost_basis_cents) if upstream_cost_basis_cents is not None else None
        ),
        "buyer_paid_cents": int(final_cents),
        "buyer_user_id": int(buyer_user_id),
        "commercial_service_source": commercial_service_source,
    })
    immutable_snapshot = {
        "schema_version": 2,
        "quote_id": quote_id,
        "quote_type": quote_type,
        "currency": "CNY",
        "catalog_version": catalog_version,
        "catalog_version_id": int(catalog_version_id) if catalog_version_id is not None else None,
        "catalog_scope_key": scope_key,
        "catalog_entry_id": int(catalog_entry_id) if catalog_entry_id is not None else None,
        "product_code": product_code,
        "source_ref": dict(source_ref or {}),
        "quantity": int(quantity),
        "seller_legal_entity_id": str(seller_legal_entity_id),
        "payment_collector": "PLATFORM",
        "buyer_user_id": int(buyer_user_id),
        "base_price_cents": int(base_cents),
        "effective_multiplier_bps": int(mult_bps),
        "final_price_cents": int(final_cents),
        "points_granted": int(points_granted),
        "bonus_points": int(bonus_points),
        "channel_account_code": channel_account_code,
        "channel_beneficiary_user_id": (
            int(channel_beneficiary_user_id) if channel_beneficiary_user_id is not None else None
        ),
        "channel_relationship_version": channel_relationship_version,
        "seller_policy_version": seller_policy_version,
        "upstream_cost_basis_cents": (
            int(upstream_cost_basis_cents) if upstream_cost_basis_cents is not None else None
        ),
        "commercial_service_source": commercial_service_source,
        "expires_at": expires_at.isoformat(),
        "order_pricing_snapshot": order_snapshot,
    }
    calc_hash = _compute_v2_hash({"quote_snapshot": immutable_snapshot})
    immutable_snapshot["calculation_hash"] = calc_hash
    cur.execute(
        """INSERT INTO price_quotes
           (quote_id, quote_type, currency, catalog_version, catalog_version_id,
            channel_relationship_version, seller_policy_version, product_code, quantity,
            seller_legal_entity_id, buyer_user_id, channel_account_code, channel_beneficiary_user_id,
            payment_collector, points_granted, bonus_points, base_price_cents, effective_multiplier_bps,
            final_price_cents, upstream_cost_basis_cents, expires_at, calculation_hash, status, idempotency_key,
            pricing_snapshot_jsonb)
           VALUES (%s,%s,'CNY',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLATFORM',%s,%s,%s,%s,%s,%s,%s,%s,'issued',%s,%s::jsonb)
           RETURNING *""",
        (quote_id, quote_type, catalog_version, catalog_version_id,
         channel_relationship_version, seller_policy_version, product_code, quantity,
         str(seller_legal_entity_id), buyer_user_id, channel_account_code, channel_beneficiary_user_id,
         points_granted, bonus_points, base_cents, mult_bps, final_cents,
         upstream_cost_basis_cents, expires_at, calc_hash, idempotency_key,
         json.dumps(immutable_snapshot, ensure_ascii=False, separators=(",", ":"))),
    )
    return dict(cur.fetchone())


def _neutral_procurement_snapshot(
    *, dealer_id: int, catalog_version: str, product_code: str,
    final_cents: int, points_granted: int, bonus_points: int,
) -> Dict[str, Any]:
    """Legacy-off fallback used only when dual SSOT is disabled.

    It still satisfies the existing `complete_recharge` snapshot verifier, so a
    directly exercised legacy service quote cannot create a callback-time repricing
    dependency.
    """
    if final_cents <= 0 or points_granted <= 0:
        raise QuoteError("进货订单快照金额/算力非法")
    reward_bps = 0
    if bonus_points:
        reward_bps = (int(bonus_points) * 10000 + int(points_granted) // 2) // int(points_granted)
        if reward_bps > 10000 or (int(points_granted) * reward_bps + 5000) // 10000 != int(bonus_points):
            raise QuoteError("进货目录奖励无法按现有结算公式复算")
    snapshot = {
        "catalog_version": catalog_version,
        "option_id": product_code,
        "option_source": "published_catalog",
        "amount_cents": int(final_cents),
        "base_points": int(points_granted),
        "discount_source": "published_price_quote",
        "discount_numer": int(final_cents),
        "discount_denom": int(points_granted),
        "channel_tier_enabled": False,
        "tier_at_order": "none",
        "tier_source": "disabled",
        "rolling_before_yuan_snapshot": "0",
        "projected_rolling_12m_yuan_snapshot": "0",
        "tier_override_until_snapshot": None,
        "tier_bonus_rate_bps": 0,
        "tier_bonus_points": 0,
        "founder_eligibility_source": "not_applicable",
        "founder_seat_policy": "not_applicable",
        "founder_cap_snapshot": 0,
        "founder_min_first_order_yuan_snapshot": "0",
        "founder_bonus_rate_bps_snapshot": 0,
        "founder_bonus_points_if_eligible": 0,
        "bonus_validity_months": 12,
        "bonus_points": int(bonus_points),
        "bonus_rate_bps": int(reward_bps),
        "total_points": int(points_granted) + int(bonus_points),
        "reward_eligible": bool(bonus_points),
        "reward_description": "已发布进货目录奖励" if bonus_points else "本档暂无固定奖励",
    }
    from services.agent_inventory_pricing import finalize_quote_snapshot
    return finalize_quote_snapshot(snapshot, agent_user_id=int(dealer_id))


def _procurement_order_snapshot(
    cur, *, dealer_id: int, version: Dict[str, Any], entry: Dict[str, Any],
    source_ref: Dict[str, Any], payable_amount_cents: int,
    paid_inventory_points: int, cash_anchor: Dict[str, Any],
    resale_terms: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the callback contract after cash and paid points are both immutable."""
    from services.agent_inventory_pricing import (
        FREE_AMOUNT_OPTION_ID,
        build_purchase_snapshot,
        finalize_quote_snapshot,
    )

    calc_meta = _json_dict(version.get("calc_meta_jsonb"))
    pricing_config = _json_dict(calc_meta.get("pricing_config_snapshot"))
    source_kind = str(source_ref.get("kind") or "")
    option = _json_dict(source_ref.get("option"))
    option_id = source_ref.get("option_id") or option.get("option_id")
    source_amount = source_ref.get("amount_cents")
    is_fixed_option = bool(
        source_kind == "agent_purchase_option" and option_id and option
    )
    is_custom_amount = bool(
        source_kind == "agent_purchase_custom_amount"
        and source_amount is not None
        and str(entry.get("product_code")) == FREE_AMOUNT_OPTION_ID
    )
    valid_source = bool(pricing_config and source_amount is not None and (
        is_fixed_option or is_custom_amount
    ))
    if not valid_source:
        if _dual_enabled():
            raise QuoteError("进货价目缺少已发布计算快照 · 请管理员重新发布")
        return _neutral_procurement_snapshot(
            dealer_id=dealer_id,
            catalog_version=str(version["version_code"]),
            product_code=str(entry["product_code"]),
            final_cents=payable_amount_cents,
            points_granted=paid_inventory_points,
            bonus_points=0,
        )
    if int(source_amount) * max(1, int(entry.get("_quote_quantity", 1))) != int(payable_amount_cents):
        raise QuoteError("进货目录来源金额与已发布价格不一致")
    paid_points = int(paid_inventory_points)
    payable = int(payable_amount_cents)
    discount_snapshot = _json_dict(cash_anchor.get("purchase_discount_snapshot"))
    if paid_points <= 0 or payable <= 0:
        raise QuoteError("金额锚定进货快照金额/算力非法")
    if (
        not discount_snapshot
        or int(discount_snapshot.get("numer") or 0) <= 0
        or int(discount_snapshot.get("denom") or 0) <= 0
        or not str(discount_snapshot.get("discount_version") or "")
    ):
        raise QuoteError("专属进货折扣快照不完整")
    selected_option = None
    if is_fixed_option:
        option["option_id"] = str(option_id)
        selected_option = option
    snapshot = build_purchase_snapshot(
        cur,
        config=pricing_config,
        catalog_version=str(version["version_code"]),
        agent_user_id=int(dealer_id),
        amount_cents=payable,
        option=selected_option,
        locked_discount={
            "source": "procurement_cash_anchor_v2_effective",
            "numer": payable,
            "denom": paid_points,
        },
        locked_base_points=paid_points,
        progress_amount_cents=payable,
    )
    if int(snapshot["base_points"]) != paid_points:
        raise QuoteError("金额锚定进货基础算力与计算快照不一致")
    snapshot["amount_cents"] = payable
    snapshot["discount_source"] = str(discount_snapshot["source"])
    # complete_recharge intentionally verifies these legacy top-level fields as
    # the final effective cash/paid-points ratio.  Keep that callback contract
    # unchanged; the root per-agent override values/version live in the explicit
    # purchase_discount_snapshot below.
    snapshot["discount_numer"] = payable
    snapshot["discount_denom"] = paid_points
    snapshot["purchase_discount_version"] = str(discount_snapshot["discount_version"])
    snapshot["purchase_discount_snapshot"] = discount_snapshot
    snapshot["cash_anchor_semantic_version"] = str(cash_anchor["semantic_version"])
    snapshot["cash_anchor_fingerprint"] = str(cash_anchor["cash_anchor_fingerprint"])
    snapshot["payable_amount_cents"] = payable
    snapshot["paid_inventory_points"] = paid_points
    snapshot["cash_anchor"] = dict(cash_anchor)
    if is_fixed_option:
        snapshot["option_id"] = str(option_id)
        snapshot["option_source"] = "published_catalog"
    else:
        snapshot["option_id"] = FREE_AMOUNT_OPTION_ID
        snapshot["option_source"] = "custom_amount"
    if resale_terms is not None:
        snapshot.update(dict(resale_terms))
        snapshot["amount_cents"] = payable
        snapshot["base_points"] = paid_points
        snapshot["channel_account_code"] = None
        snapshot["channel_beneficiary_user_id"] = None
        snapshot["upstream_cost_basis_cents"] = None
    snapshot["total_points"] = int(snapshot["base_points"]) + int(snapshot["bonus_points"])
    return finalize_quote_snapshot(snapshot, agent_user_id=int(dealer_id))


def _calculate_procurement_cash_anchor(
    cur, *, dealer_id: int, payable_amount_cents: int,
    pricing_config: Dict[str, Any], catalog_version: str, product_code: str,
    catalog_version_id: Optional[str], catalog_entry_id: Optional[str],
) -> Dict[str, Any]:
    """Resolve one relationship-stable cash anchor for fixed and custom quotes."""

    from config import pricing_ssot_flags
    from config.dealer_inventory_resale_flags import (
        DealerResaleFlagUnavailable,
        enabled as dealer_resale_enabled,
    )
    from services import channel_pricing, dealer_inventory_resale
    from services.procurement_cash_anchor import CashAnchorError, calculate_cash_anchor

    from services.agent_inventory_pricing import resolve_procurement_discount_snapshot

    try:
        discount_snapshot = resolve_procurement_discount_snapshot(
            cur,
            int(dealer_id),
            pricing_config,
            published_catalog_version=str(catalog_version),
            require_override_schema=True,
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise QuoteError("当前专属进货折扣无法确认") from exc
    discount_numer = int(discount_snapshot["numer"])
    discount_denom = int(discount_snapshot["denom"])
    try:
        resale_enabled = dealer_resale_enabled(cur)
    except DealerResaleFlagUnavailable as exc:
        raise QuoteError("当前采购规则状态无法确认") from exc
    if resale_enabled:
        try:
            terms = dealer_inventory_resale.build_cash_anchored_quote_terms(
                cur,
                buyer_user_id=int(dealer_id),
                payable_amount_cents=int(payable_amount_cents),
                platform_points_numer=discount_denom,
                platform_points_denom=discount_numer,
                pricing_version=str(catalog_version),
                product_code=str(product_code),
                catalog_version_id=catalog_version_id,
                catalog_entry_id=catalog_entry_id,
                purchase_discount_snapshot=discount_snapshot,
            )
        except (CashAnchorError, dealer_inventory_resale.ResaleError) as exc:
            raise QuoteError("当前采购规则无法形成金额锚定报价") from exc
        if terms is None:
            raise QuoteError("当前采购规则无法形成金额锚定报价")
        return {
            "cash_anchor": dict(terms["cash_anchor"]),
            "resale_terms": terms,
            "channel_account_code": None,
            "channel_beneficiary_user_id": None,
            "channel_relationship_version": terms.get("relationship_version"),
            "upstream_cost_basis_cents": None,
            "seller_policy_version": str(terms["seller_policy_version"]),
            "seller_legal_entity_id": f"USER:{int(terms['seller_user_id'])}",
            "purchase_discount_snapshot": discount_snapshot,
        }

    flags = pricing_ssot_flags.pricing_flags_snapshot(cur=cur)
    channel_enabled = bool(flags.get("CHANNEL_PRICING_ENABLED", False))
    try:
        chain = (
            channel_pricing.resolve_chain_snapshot(int(dealer_id), cur=cur)
            if channel_enabled else []
        )
    except channel_pricing.ChannelError as exc:
        raise QuoteError("当前采购关系无法形成金额锚定报价") from exc
    path = [
        {
            "seller_user_id": int(rel["upstream_channel_account_id"]),
            "buyer_user_id": int(rel["buyer_dealer_id"]),
            "relationship_id": int(rel["relationship_id"]),
            "relationship_version": str(rel["relationship_version"]),
            "source_kind": "channel_pricing",
            "multiplier_bps": int(rel["cost_multiplier_bps"]),
        }
        for rel in reversed(chain)
    ]
    try:
        anchor = calculate_cash_anchor(
            buyer_user_id=int(dealer_id),
            payable_amount_cents=int(payable_amount_cents),
            platform_points_numer=discount_denom,
            platform_points_denom=discount_numer,
            catalog_version=str(catalog_version),
            relationship_path=path,
            purchase_discount_snapshot=discount_snapshot,
        )
    except CashAnchorError as exc:
        raise QuoteError("当前采购规则无法形成金额锚定报价") from exc
    channel_code = None
    beneficiary = None
    relationship_version = None
    upstream_cost = None
    if chain:
        from services import account_codes

        immediate = chain[0]
        beneficiary = int(immediate["upstream_channel_account_id"])
        relationship_version = str(immediate["relationship_version"])
        channel_code = account_codes.get_channel_code(beneficiary, cur=cur)
        if not channel_code:
            raise QuoteError("渠道结算编号尚未准备 · 请管理员处理后重试")
        upstream_cost = int(anchor["direct_seller_acquisition_cents"])
    return {
        "cash_anchor": anchor,
        "resale_terms": None,
        "channel_account_code": channel_code,
        "channel_beneficiary_user_id": beneficiary,
        "channel_relationship_version": relationship_version,
        "upstream_cost_basis_cents": upstream_cost,
        "seller_policy_version": None,
        "seller_legal_entity_id": "PLATFORM",
        "purchase_discount_snapshot": discount_snapshot,
    }


def build_procurement_quote_preview(
    cur,
    *,
    dealer_id: int,
    entry: Dict[str, Any],
    version: Dict[str, Any],
    quantity: int = 1,
) -> Dict[str, Any]:
    """Persisted-catalog preview where the published cash amount never changes."""
    from services.agent_inventory_pricing import points_for_amount

    qty = int(quantity)
    if qty < 1:
        raise QuoteError("quantity 必须 ≥ 1")
    payable = int(entry["final_price_cents"]) * qty
    if payable <= 0 or int(entry["paid_points"]) <= 0:
        raise QuoteError("进货价目异常")
    source_ref = _json_dict(entry.get("source_ref_jsonb"))
    calc_meta = _json_dict(version.get("calc_meta_jsonb"))
    pricing_config = _json_dict(calc_meta.get("pricing_config_snapshot"))
    if not pricing_config:
        raise QuoteError("已发布进货版本缺少计算配置快照 · 请管理员重新发布")
    root_points = points_for_amount(
        payable,
        int(pricing_config.get("wholesale_numer") or 0),
        int(pricing_config.get("wholesale_denom") or 0),
    )
    if root_points != int(entry["paid_points"]) * qty:
        raise QuoteError("已发布进货目录金额与根算力换算快照不一致")

    entry_for_snapshot = dict(entry)
    entry_for_snapshot["_quote_quantity"] = qty
    anchored = _calculate_procurement_cash_anchor(
        cur,
        dealer_id=int(dealer_id),
        payable_amount_cents=payable,
        pricing_config=pricing_config,
        catalog_version=str(version["version_code"]),
        product_code=str(entry["product_code"]),
        catalog_version_id=(str(version["id"]) if version.get("id") is not None else None),
        catalog_entry_id=(str(entry["id"]) if entry.get("id") else None),
    )
    cash_anchor = dict(anchored["cash_anchor"])
    order_snapshot = _procurement_order_snapshot(
        cur,
        dealer_id=int(dealer_id),
        version=version,
        entry=entry_for_snapshot,
        source_ref=source_ref,
        payable_amount_cents=payable,
        paid_inventory_points=int(cash_anchor["paid_inventory_points"]),
        cash_anchor=cash_anchor,
        resale_terms=anchored["resale_terms"],
    )
    points_granted = int(order_snapshot["base_points"])
    bonus_points = int(order_snapshot["bonus_points"])
    return {
        "base_price_cents": payable,
        "final_price_cents": payable,
        "points_granted": points_granted,
        "bonus_points": bonus_points,
        "multiplier_bps": 10000,
        "channel_account_code": anchored["channel_account_code"],
        "channel_beneficiary_user_id": anchored["channel_beneficiary_user_id"],
        "channel_relationship_version": anchored["channel_relationship_version"],
        "seller_policy_version": anchored["seller_policy_version"],
        "seller_legal_entity_id": anchored["seller_legal_entity_id"],
        "upstream_cost_basis_cents": anchored["upstream_cost_basis_cents"],
        "source_ref": source_ref,
        "order_pricing_snapshot": order_snapshot,
        "resale_mode": anchored["resale_terms"] is not None,
    }


def issue_procurement_custom_amount_quote(
    *, dealer_id: int, requested_amount_cents: int,
    platform_scope: str = "PLATFORM_BASE", ttl_seconds: int = DEFAULT_TTL_SECONDS,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a free-amount quote whose requested cash is the final payable cash.

    Current relationships change only paid inventory points.  Cash, points, rewards,
    fulfillment and every callback input are pinned before an order can be created.
    """

    from config.dealer_inventory_resale_flags import enabled as dealer_resale_enabled
    from services import pricing_catalog
    from services.agent_inventory_pricing import (
        FREE_AMOUNT_OPTION_ID,
        MAX_AGENT_PURCHASE_AMOUNT_CENTS,
    )

    platform_base = int(requested_amount_cents)
    if platform_base <= 0 or platform_base > MAX_AGENT_PURCHASE_AMOUNT_CENTS:
        raise QuoteError("自由金额超出单笔允许范围")

    with get_db() as conn:
        cur = conn.cursor()
        _lock_procurement_quote_idempotency(
            cur,
            buyer_user_id=int(dealer_id),
            idempotency_key=idempotency_key,
        )
        if idempotency_key:
            cur.execute(
                """SELECT * FROM price_quotes
                   WHERE buyer_user_id=%s AND quote_type='procurement'
                     AND idempotency_key=%s AND status='issued' AND expires_at > NOW()
                   ORDER BY created_at DESC LIMIT 1""",
                (dealer_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                existing_dict = dict(existing)
                if (
                    str(existing_dict.get("product_code")) != FREE_AMOUNT_OPTION_ID
                    or int(existing_dict.get("quantity") or 0) != 1
                    or int(existing_dict.get("base_price_cents") or 0) != platform_base
                ):
                    raise QuoteError("幂等键已用于不同的进货报价请求")
                existing_order = quote_order_pricing_snapshot(existing_dict)
                resale_now = dealer_resale_enabled(cur)
                existing_resale = existing_order.get("resale_mode") is True
                if resale_now == existing_resale:
                    try:
                        _assert_procurement_cash_anchor_current(
                            cur, quote=existing_dict, order_snapshot=existing_order,
                        )
                    except QuoteError:
                        pass
                    else:
                        return existing_dict
                cur.execute(
                    "UPDATE price_quotes SET status='expired' "
                    "WHERE quote_id=%s AND status='issued'",
                    (existing_dict["quote_id"],),
                )

        version = pricing_catalog.get_published_version(
            "procurement", platform_scope, cur=cur
        )
        if not version:
            raise QuoteError(f"无已发布进货价目({platform_scope})· 拒绝报价")
        calc_meta = _json_dict(version.get("calc_meta_jsonb"))
        pricing_config = _json_dict(calc_meta.get("pricing_config_snapshot"))
        if not pricing_config:
            raise QuoteError("已发布进货版本缺少计算配置快照 · 请管理员重新发布")

        source_ref = {
            "kind": "agent_purchase_custom_amount",
            "amount_cents": platform_base,
        }
        synthetic_entry = {
            "id": None,
            "product_code": FREE_AMOUNT_OPTION_ID,
            "_quote_quantity": 1,
        }

        try:
            anchored = _calculate_procurement_cash_anchor(
                cur,
                dealer_id=int(dealer_id),
                payable_amount_cents=platform_base,
                pricing_config=pricing_config,
                catalog_version=str(version["version_code"]),
                product_code=FREE_AMOUNT_OPTION_ID,
                catalog_version_id=str(version["id"]),
                catalog_entry_id=None,
            )
            cash_anchor = dict(anchored["cash_anchor"])
            order_snapshot = _procurement_order_snapshot(
                cur,
                dealer_id=dealer_id,
                version=version,
                entry=synthetic_entry,
                source_ref=source_ref,
                payable_amount_cents=platform_base,
                paid_inventory_points=int(cash_anchor["paid_inventory_points"]),
                cash_anchor=cash_anchor,
                resale_terms=anchored["resale_terms"],
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise QuoteError(f"自由金额报价计算失败: {exc}") from exc

        points_granted = int(order_snapshot["base_points"])
        bonus_points = int(order_snapshot["bonus_points"])
        if points_granted <= 0:
            raise QuoteError("自由金额报价基础算力异常")
        return _insert_quote(
            cur,
            quote_type="procurement",
            scope_key=platform_scope,
            product_code=FREE_AMOUNT_OPTION_ID,
            buyer_user_id=dealer_id,
            quantity=1,
            catalog_version=str(version["version_code"]),
            catalog_version_id=version["id"],
            base_cents=platform_base,
            mult_bps=10000,
            final_cents=platform_base,
            points_granted=points_granted,
            bonus_points=bonus_points,
            channel_account_code=anchored["channel_account_code"],
            channel_beneficiary_user_id=anchored["channel_beneficiary_user_id"],
            channel_relationship_version=anchored["channel_relationship_version"],
            seller_policy_version=anchored["seller_policy_version"],
            ttl_seconds=ttl_seconds,
            idempotency_key=idempotency_key,
            upstream_cost_basis_cents=anchored["upstream_cost_basis_cents"],
            source_ref=source_ref,
            catalog_entry_id=None,
            order_pricing_snapshot=order_snapshot,
            seller_legal_entity_id=anchored["seller_legal_entity_id"],
        )


def issue_procurement_quote(
    *, dealer_id: int, product_code: str, quantity: int = 1,
    platform_scope: str = "PLATFORM_BASE", ttl_seconds: int = DEFAULT_TTL_SECONDS,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """固定档位进货报价：现金不变，当前采购链仅决定到账算力。"""
    from services import pricing_catalog
    from config.dealer_inventory_resale_flags import enabled as dealer_resale_enabled
    if quantity < 1:
        raise QuoteError("quantity 必须 ≥ 1")
    with get_db() as conn:
        cur = conn.cursor()
        _lock_procurement_quote_idempotency(
            cur,
            buyer_user_id=int(dealer_id),
            idempotency_key=idempotency_key,
        )
        if idempotency_key:
            cur.execute(
                """SELECT * FROM price_quotes
                   WHERE buyer_user_id=%s AND quote_type='procurement' AND idempotency_key=%s
                     AND status='issued' AND expires_at > NOW()
                   ORDER BY created_at DESC LIMIT 1""",
                (dealer_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                existing_dict = dict(existing)
                if (
                    str(existing_dict.get("product_code")) != str(product_code)
                    or int(existing_dict.get("quantity") or 0) != int(quantity)
                ):
                    raise QuoteError("幂等键已用于不同的进货报价请求")
                existing_order = quote_order_pricing_snapshot(existing_dict)
                resale_now = dealer_resale_enabled(cur)
                existing_resale = existing_order.get("resale_mode") is True
                if resale_now == existing_resale:
                    try:
                        _assert_procurement_cash_anchor_current(
                            cur, quote=existing_dict, order_snapshot=existing_order,
                        )
                    except QuoteError:
                        pass
                    else:
                        return existing_dict
                cur.execute(
                    "UPDATE price_quotes SET status='expired' "
                    "WHERE quote_id=%s AND status='issued'",
                    (existing_dict["quote_id"],),
                )
        entry_pair = pricing_catalog.get_published_entry("procurement", platform_scope, product_code, cur=cur)
        entry, version = (entry_pair if entry_pair else (None, None))
        if not entry:
            raise QuoteError(f"无已发布进货价目({platform_scope}/{product_code})· 拒绝报价")
        preview = build_procurement_quote_preview(
            cur,
            dealer_id=int(dealer_id),
            entry=dict(entry),
            version=dict(version),
            quantity=int(quantity),
        )
        return _insert_quote(
            cur, quote_type="procurement", scope_key=platform_scope, product_code=product_code,
            buyer_user_id=dealer_id, quantity=quantity, catalog_version=version["version_code"],
            catalog_version_id=version["id"], base_cents=preview["base_price_cents"],
            mult_bps=preview["multiplier_bps"], final_cents=preview["final_price_cents"],
            points_granted=preview["points_granted"], bonus_points=preview["bonus_points"],
            channel_account_code=preview["channel_account_code"],
            channel_beneficiary_user_id=preview["channel_beneficiary_user_id"],
            channel_relationship_version=preview["channel_relationship_version"],
            seller_policy_version=preview["seller_policy_version"],
            ttl_seconds=ttl_seconds, idempotency_key=idempotency_key,
            upstream_cost_basis_cents=preview["upstream_cost_basis_cents"],
            source_ref=preview["source_ref"], catalog_entry_id=entry.get("id"),
            order_pricing_snapshot=preview["order_pricing_snapshot"],
            seller_legal_entity_id=preview["seller_legal_entity_id"],
        )


def get_quote(quote_id: str, cur=None) -> Optional[Dict[str, Any]]:
    sql = "SELECT * FROM price_quotes WHERE quote_id = %s"
    if cur is not None:
        cur.execute(sql, (quote_id,))
        r = cur.fetchone()
        return dict(r) if r else None
    with get_db() as conn:
        c = conn.cursor()
        c.execute(sql, (quote_id,))
        r = c.fetchone()
        return dict(r) if r else None


def _assert_procurement_cash_anchor_current(
    cur, *, quote: Dict[str, Any], order_snapshot: Dict[str, Any],
) -> None:
    """Reject pre-anchor or relationship-stale issued procurement quotes.

    Historical orders never call this path.  Dealer resale quotes get a stronger
    locked FIFO/JIT plan revalidation during order reservation; the legacy channel
    mode is re-derived here under the channel graph transaction lock.
    """
    from services.procurement_cash_anchor import (
        CASH_ANCHOR_SEMANTIC_VERSION,
        CashAnchorError,
        calculate_cash_anchor,
    )

    if order_snapshot.get("cash_anchor_semantic_version") != CASH_ANCHOR_SEMANTIC_VERSION:
        raise QuoteError("旧版进货报价已失效 · 请重新报价")
    anchor = _json_dict(order_snapshot.get("cash_anchor"))
    payable = int(quote["final_price_cents"])
    paid_points = int(quote["points_granted"])
    if (
        not anchor
        or str(order_snapshot.get("cash_anchor_fingerprint") or "")
        != str(anchor.get("cash_anchor_fingerprint") or "")
        or int(order_snapshot.get("payable_amount_cents") or 0) != payable
        or int(order_snapshot.get("paid_inventory_points") or 0) != paid_points
        or int(anchor.get("payable_amount_cents") or 0) != payable
        or int(anchor.get("paid_inventory_points") or 0) != paid_points
    ):
        raise QuoteError("金额锚定报价快照不完整 · 拒绝下单")

    locked_discount = _json_dict(anchor.get("purchase_discount_snapshot"))
    if (
        not locked_discount
        or str(order_snapshot.get("purchase_discount_version") or "")
        != str(locked_discount.get("discount_version") or "")
        or _json_dict(order_snapshot.get("purchase_discount_snapshot")) != locked_discount
    ):
        raise QuoteError("专属进货折扣快照不完整 · 请重新报价")
    from services.agent_inventory_pricing import resolve_procurement_discount_snapshot

    published_config = {
        "wholesale_numer": int(locked_discount.get("published_global_numer") or 0),
        "wholesale_denom": int(locked_discount.get("published_global_denom") or 0),
    }
    try:
        current_discount = resolve_procurement_discount_snapshot(
            cur,
            int(quote["buyer_user_id"]),
            published_config,
            published_catalog_version=str(
                locked_discount.get("published_catalog_version")
                or anchor.get("catalog_version")
                or ""
            ),
            require_override_schema=True,
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise QuoteError("当前专属进货折扣无法确认 · 请重新报价") from exc
    discount_fields = (
        "source", "numer", "denom", "discount_version", "override_updated_at",
        "published_catalog_version", "published_global_numer", "published_global_denom",
    )
    if any(
        str(current_discount.get(key)) != str(locked_discount.get(key))
        for key in discount_fields
    ):
        raise QuoteError("专属进货折扣已变化 · 请重新报价")

    if order_snapshot.get("resale_mode") is True:
        # reserve_order() re-derives and locks the complete FIFO/JIT plan in the
        # same order transaction.  Do not independently model that path here.
        return

    from config import pricing_ssot_flags
    from services import channel_pricing

    flags = pricing_ssot_flags.pricing_flags_snapshot(cur=cur)
    chain = (
        channel_pricing.resolve_chain_snapshot(int(quote["buyer_user_id"]), cur=cur)
        if flags.get("CHANNEL_PRICING_ENABLED", False)
        else []
    )
    path = [
        {
            "seller_user_id": int(rel["upstream_channel_account_id"]),
            "buyer_user_id": int(rel["buyer_dealer_id"]),
            "relationship_id": int(rel["relationship_id"]),
            "relationship_version": str(rel["relationship_version"]),
            "source_kind": "channel_pricing",
            "multiplier_bps": int(rel["cost_multiplier_bps"]),
        }
        for rel in reversed(chain)
    ]
    try:
        current = calculate_cash_anchor(
            buyer_user_id=int(quote["buyer_user_id"]),
            payable_amount_cents=payable,
            platform_points_numer=int(anchor.get("platform_points_numer") or 0),
            platform_points_denom=int(anchor.get("platform_points_denom") or 0),
            catalog_version=str(anchor.get("catalog_version") or ""),
            relationship_path=path,
            platform_promotion_discount_bps=int(
                anchor.get("platform_promotion_discount_bps") or 10000
            ),
            promotion_snapshot=_json_dict(anchor.get("promotion_snapshot")),
            purchase_discount_snapshot=current_discount,
        )
    except (CashAnchorError, channel_pricing.ChannelError) as exc:
        raise QuoteError("当前采购关系无法验证 · 请重新报价") from exc
    if str(current["cash_anchor_fingerprint"]) != str(anchor["cash_anchor_fingerprint"]):
        raise QuoteError("采购关系已变化 · 请重新报价")


def lock_and_validate(cur, quote_id: str, *, buyer_user_id: int, quote_type: str,
                      expected_final_cents: Optional[int] = None,
                      expected_product_code: Optional[str] = None,
                      expected_points: Optional[int] = None,
                      expected_bonus_points: Optional[int] = None,
                      expected_catalog_version: Optional[str] = None) -> Dict[str, Any]:
    """在调用方事务内 FOR UPDATE 锁报价并做全量校验。抛 QuoteError 表示拒绝。
    [审核 #4] 除金额外锁定商品(product_code)与算力(points_granted);[审核 #5] 重算 calculation_hash 比对。
    不推进状态 —— 调用方拿到有效报价后自行 consume_quote(同事务)。"""
    cur.execute("SELECT * FROM price_quotes WHERE quote_id = %s FOR UPDATE", (quote_id,))
    q = cur.fetchone()
    if not q:
        raise QuoteError("报价不存在")
    q = dict(q)
    if q["status"] == "consumed":
        raise QuoteError("报价已被使用(一报价只能开一单)")
    if q["status"] in ("expired", "cancelled"):
        raise QuoteError(f"报价已{q['status']}")
    if q["expires_at"] <= datetime.now(timezone.utc):
        raise QuoteError("报价已过期 · 请重新获取报价并确认")
    if q["buyer_user_id"] != buyer_user_id:
        raise QuoteError("报价买方不一致")
    if q["quote_type"] != quote_type:
        raise QuoteError("报价用途不一致")
    if q["currency"] != "CNY":
        raise QuoteError("币种不一致")
    # [审核 #5] 完整性:重算哈希比对(检测报价行被篡改·纵深防御·真值仍以行为准)
    if _hash_from_row(q) != q["calculation_hash"]:
        raise QuoteError("报价完整性校验失败(疑被篡改)· 拒绝")
    embedded = quote_snapshot(q)
    if q["quote_type"] == "procurement" and not embedded:
        raise QuoteError("旧版进货报价已失效 · 请重新报价")
    if embedded:
        if embedded.get("calculation_hash") != q["calculation_hash"]:
            raise QuoteError("报价快照哈希与报价行不一致 · 拒绝")
        scalar_pairs = (
            ("quote_id", q["quote_id"]),
            ("quote_type", q["quote_type"]),
            ("currency", q["currency"]),
            ("catalog_version", q["catalog_version"]),
            ("catalog_version_id", q.get("catalog_version_id")),
            ("product_code", q["product_code"]),
            ("quantity", q["quantity"]),
            ("seller_legal_entity_id", q["seller_legal_entity_id"]),
            ("payment_collector", q["payment_collector"]),
            ("buyer_user_id", q["buyer_user_id"]),
            ("base_price_cents", q["base_price_cents"]),
            ("effective_multiplier_bps", q["effective_multiplier_bps"]),
            ("final_price_cents", q["final_price_cents"]),
            ("points_granted", q["points_granted"]),
            ("bonus_points", q["bonus_points"]),
            ("channel_account_code", q.get("channel_account_code")),
            ("channel_beneficiary_user_id", q.get("channel_beneficiary_user_id")),
            ("channel_relationship_version", q.get("channel_relationship_version")),
            ("seller_policy_version", q.get("seller_policy_version")),
            ("upstream_cost_basis_cents", q.get("upstream_cost_basis_cents")),
        )
        for key, scalar in scalar_pairs:
            value = embedded.get(key)
            if value is None and scalar is None:
                continue
            if str(value) != str(scalar):
                raise QuoteError(f"报价快照字段 {key} 与报价行不一致 · 拒绝")
        embedded_expires = embedded.get("expires_at")
        try:
            parsed_expires = datetime.fromisoformat(str(embedded_expires).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise QuoteError("报价快照失效时间非法 · 拒绝")
        if parsed_expires != q["expires_at"]:
            raise QuoteError("报价快照失效时间与报价行不一致 · 拒绝")
        order_snapshot = quote_order_pricing_snapshot(q, required=True)
        if int(order_snapshot.get("buyer_paid_cents", -1)) != int(q["final_price_cents"]):
            raise QuoteError("订单快照实付金额与报价不一致 · 拒绝")
        if order_snapshot.get("quote_type") != q["quote_type"]:
            raise QuoteError("订单快照用途与报价不一致 · 拒绝")
        if q["quote_type"] == "procurement":
            _assert_procurement_cash_anchor_current(
                cur, quote=q, order_snapshot=order_snapshot,
            )
    if q["quote_type"] == "retail":
        retail_source = quote_source_ref(q)
        if retail_source.get("source_kind") == "retail_custom_amount":
            assert_retail_cash_anchor_current(
                cur,
                quote=q,
                source_ref=retail_source,
            )
        elif _retail_cost_guard_required(cur, retail_source):
            assert_retail_catalog_entry_current(
                cur,
                source_ref=retail_source,
                final_cents=int(q["final_price_cents"]),
            )
    if expected_final_cents is not None and int(expected_final_cents) != int(q["final_price_cents"]):
        raise QuoteError("前端金额与后端报价不一致 · 拒绝(§11.1)")
    # [审核 #4] 锁定商品与算力,防"锁了付多少钱、没锁哪个商品/发多少算力"
    if expected_product_code is not None and expected_product_code != q["product_code"]:
        raise QuoteError("报价商品与订单不一致 · 拒绝")
    if expected_points is not None and int(expected_points) != int(q["points_granted"]):
        raise QuoteError("报价算力与订单不一致 · 拒绝")
    if expected_bonus_points is not None and int(expected_bonus_points) != int(q["bonus_points"]):
        raise QuoteError("报价奖励算力与订单不一致 · 拒绝")
    if expected_catalog_version is not None and str(expected_catalog_version) != str(q["catalog_version"]):
        raise QuoteError("报价目录版本与订单不一致 · 拒绝")
    return q


def consume_quote(cur, quote_id: str, order_id: str) -> bool:
    """Conditionally consume one quote and reserve any pinned seller inventory.

    Retail dealer-consumer quotes reserve the service provider's FIFO lots here,
    after the recharge order INSERT but before this caller transaction commits.
    A reservation failure therefore rolls back the order and quote consumption.
    """
    cur.execute(
        """UPDATE price_quotes
           SET status='consumed', used_order_id=%s, consumed_at=NOW()
           WHERE quote_id=%s AND status='issued'""",
        (order_id, quote_id),
    )
    if cur.rowcount != 1:
        return False
    cur.execute("SELECT pricing_snapshot_jsonb FROM price_quotes WHERE quote_id=%s", (quote_id,))
    row = cur.fetchone()
    raw = row.get("pricing_snapshot_jsonb") if isinstance(row, dict) else (row[0] if row else None)
    snapshot = _json_dict(raw)
    order_snapshot = _json_dict(snapshot.get("order_pricing_snapshot"))
    if order_snapshot.get("consumer_resale_mode") is True:
        from services import dealer_inventory_resale
        try:
            dealer_inventory_resale.reserve_consumer_sale(
                cur, order_id=str(order_id), snapshot=order_snapshot,
            )
        except dealer_inventory_resale.ResaleError as exc:
            raise QuoteError(exc.message) from exc
    return True


def expire_stale(limit: int = 1000) -> int:
    """维护:把过期未消费报价标 expired(不影响资金 · 幂等)。"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE price_quotes SET status='expired'
               WHERE status='issued' AND expires_at <= NOW()
                 AND quote_id IN (SELECT quote_id FROM price_quotes
                                  WHERE status='issued' AND expires_at <= NOW() LIMIT %s)""",
            (limit,),
        )
        return cur.rowcount
