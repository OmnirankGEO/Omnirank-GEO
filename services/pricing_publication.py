"""管理定价源到运行时现金目录的单向发布适配器。

运行时订单只读取 ``pricing_catalog_versions/entries``、持久报价及订单快照；
``system_settings.pricing_config``、``agent_purchase_options`` 和活跃
``agent_sku_overrides`` 只是管理/兼容源。本模块只允许从前者单向物化到后者，
没有反向同步，也不在下单或支付回调时读取实时管理配置。

事务契约:
  * cursor 级 materialize 函数不 commit，供 admin 保存事务原子接线；
  * ``publish_all`` 在一个事务内先构建所有 scope，再整体发布；任一 scope
    不合法则全部回滚；
  * 全局配置 shared advisory lock + 每 scope exclusive advisory lock，支持
    WORKERS=4/蓝绿并发；相同 source_fingerprint 幂等返回；
  * 已发布目录内容与 fingerprint 不符时 fail-closed，不用新版本掩盖篡改。

本模块不读取或翻转 PRICING_* / CHANNEL_* flags。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

from config.pricing_config import (
    get_agent_purchase_catalog_version,
    merge_pricing_config,
)
from db.connection import get_db
from services.agent_inventory_pricing import (
    CATALOG_VERSION_RE,
    MAX_AGENT_PURCHASE_AMOUNT_CENTS,
    OPTION_ID_RE,
    decimal_rate_to_bps,
    normalize_catalog_options,
    points_at_rate,
    points_for_amount,
    resolve_discount,
)
from services.config_epoch import read_config_epoch_strict
from db.xact_lock_guard import require_xact_scope


MATERIALIZER_VERSION = "pricing-publication-v3-retail-effective-channel-cost"
PLATFORM_SCOPE = "PLATFORM_BASE"
_PRICING_CONFIG_LOCK = (920713, 1)
_PUBLICATION_LOCK_NAMESPACE = 920715
_RETAIL_MANAGEMENT_LOCK_SCOPE = "retail-management-source"
_MAX_CENTS = 2_000_000_000
_PUBLIC_SERVICE_CODE_RE = re.compile(r"^SV-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$")

_PURCHASE_SNAPSHOT_CONFIG_KEYS = (
    # build_purchase_snapshot cash inputs
    "wholesale_numer",
    "wholesale_denom",
    "agent_purchase_catalog_version",
    "agent_purchase_options",
    # build_purchase_snapshot reward inputs (including CHANNEL_TIER_ENABLED-on context)
    "agent_purchase_bonus_rate",
    "agent_tier_config",
    "founding",
    "bonus_validity_months",
)


class PricingPublicationError(ValueError):
    """管理源不可安全发布，或已发布目录完整性不成立。"""


class PublicationEpochConflict(PricingPublicationError):
    """admin 预览后配置纪元已变化，必须重新 dry-run。"""

    def __init__(self, expected_epoch: int, current_epoch: int):
        super().__init__(
            f"pricing_config_epoch 已变化(expected={expected_epoch}, current={current_epoch})"
        )
        self.expected_epoch = int(expected_epoch)
        self.current_epoch = int(current_epoch)


class PublicationReviewConflict(PricingPublicationError):
    """The source set no longer exactly matches the administrator's dry-run review."""


class EmptyRetailCatalogRejected(PricingPublicationError):
    """零条目零售目录被发布期守卫拒绝(工单 2026-07-29 §2.2.1)。

    2026-07-27 14:39:48 的事故:`agent_sku_overrides` id=228 被软删的同一微秒,
    平台直营 scope 物化出了 **items=0** 的 published 版本 —— 校验全过、零告警,
    下游 `retail_catalog`/`retail_quote` 随即 fail-closed,47 个无归属客户断供约 2 天。

    进货目录本来就有这条规则(`agent_purchase_options 为空，禁止发布空进货目录`),
    零售侧只是漏了。本类把同一条规则补齐:**零售目录零条目 = 断供,不是配置**。
    """

    def __init__(self, scope_key: str, catalog_type: str = "retail"):
        super().__init__(
            f"{catalog_type}/{scope_key} 物化结果为 0 个商品。"
            "发布它会让该服务方的客户完全买不了东西，已拒绝。"
            "若确需清空该服务方目录，请在发布请求里把该 scope 填入 confirmed_empty_retail_scopes 明确确认。"
        )
        self.scope_key = str(scope_key)
        self.catalog_type = str(catalog_type)


def _row_get(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    if not row:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return default


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_safe(value: Any) -> Any:
    """Deep copy into the exact JSON-compatible representation persisted in JSONB."""
    return json.loads(_canonical_json(value))


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _normalize_user_ids(values: Optional[Iterable[int]]) -> List[int]:
    if values is None:
        return []
    if isinstance(values, (int, str)):
        values = [int(values)]
    result = sorted({int(value) for value in values})
    if any(value <= 0 for value in result):
        raise PricingPublicationError("service_user_ids 必须是正整数")
    return result


def _review_scope_collection(plans: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    return sorted(
        [
            {
                "catalog_type": str(plan["catalog_type"]),
                "scope_key": str(plan["scope_key"]),
                "source_fingerprint": str(plan["source_fingerprint"]),
            }
            for plan in plans
        ],
        key=lambda item: (item["catalog_type"], item["scope_key"]),
    )


def _normalize_reviewed_scopes(values: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in values or []:
        item = {
            "catalog_type": str(raw.get("catalog_type") or "").strip(),
            "scope_key": str(raw.get("scope_key") or "").strip(),
            "source_fingerprint": str(raw.get("source_fingerprint") or "").strip().lower(),
        }
        identity = (item["catalog_type"], item["scope_key"])
        if (
            item["catalog_type"] not in ("procurement", "retail")
            or not item["scope_key"]
            or not re.fullmatch(r"[0-9a-f]{64}", item["source_fingerprint"])
        ):
            raise PublicationReviewConflict("发布审核目录指纹集合格式非法 · 请重新预览")
        if identity in seen:
            raise PublicationReviewConflict("发布审核目录指纹集合存在重复 scope · 请重新预览")
        seen.add(identity)
        normalized.append(item)
    return sorted(normalized, key=lambda item: (item["catalog_type"], item["scope_key"]))


def _load_context(cur, *, expected_epoch: Optional[int] = None) -> Dict[str, Any]:
    """Lock and read the management config without process cache/env overrides."""
    require_xact_scope(cur, where="pricing_publication._load_context")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock_shared(%s, %s)",
        _PRICING_CONFIG_LOCK,
    )
    epoch = int(read_config_epoch_strict(cur))
    if expected_epoch is not None and int(expected_epoch) != epoch:
        raise PublicationEpochConflict(int(expected_epoch), epoch)

    cur.execute("SELECT value FROM system_settings WHERE key='pricing_config' FOR SHARE")
    row = cur.fetchone()
    raw = _row_get(row, "value", 0, {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise PricingPublicationError("system_settings.pricing_config 不是合法 JSON") from exc
    if raw is not None and not isinstance(raw, dict):
        raise PricingPublicationError("system_settings.pricing_config 必须是 JSON object")
    config = merge_pricing_config(raw or {}, include_env=False)
    return {"config": config, "config_epoch": epoch}


def _purchase_config_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    """Capture every cash/reward field consumed by build_purchase_snapshot."""
    raw_revision = str(config.get("agent_purchase_catalog_version") or "")
    if not CATALOG_VERSION_RE.fullmatch(raw_revision):
        raise PricingPublicationError("agent_purchase_catalog_version 非法，拒绝兼容回落发布")
    snapshot = {
        key: copy.deepcopy(config.get(key))
        for key in _PURCHASE_SNAPSHOT_CONFIG_KEYS
    }
    snapshot["agent_purchase_catalog_version"] = get_agent_purchase_catalog_version(config)
    try:
        snapshot["agent_purchase_options"] = normalize_catalog_options(
            config.get("agent_purchase_options") or []
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise PricingPublicationError("agent_purchase_options 存在非法字段") from exc
    return _json_safe(snapshot)


def _build_procurement_plan(config: Dict[str, Any], config_epoch: int) -> Dict[str, Any]:
    snapshot = _purchase_config_snapshot(config)
    options = list(snapshot["agent_purchase_options"])
    if not options:
        raise PricingPublicationError("agent_purchase_options 为空，禁止发布空进货目录")

    seen: set[str] = set()
    enabled: List[Dict[str, Any]] = []
    for option in options:
        option_id = str(option.get("option_id") or "")
        if not OPTION_ID_RE.fullmatch(option_id):
            raise PricingPublicationError(f"进货档 option_id 非法: {option_id or '<empty>'}")
        if option_id in seen:
            raise PricingPublicationError(f"进货档 option_id 重复: {option_id}")
        seen.add(option_id)
        amount = int(option.get("amount_cents") or 0)
        if amount <= 0 or amount > MAX_AGENT_PURCHASE_AMOUNT_CENTS:
            raise PricingPublicationError(f"进货档 {option_id} 金额超出允许范围")
        if bool(option.get("is_enabled")):
            enabled.append(option)
    if not enabled:
        raise PricingPublicationError("agent_purchase_options 全部停用，禁止发布空进货目录")
    # Existing installations may still carry the historical six fixed tiers.
    # New admin writes are capped at three, but publication must remain able to
    # preserve that legacy catalog until an operator intentionally edits it.

    numer = int(snapshot.get("wholesale_numer") or 0)
    denom = int(snapshot.get("wholesale_denom") or 0)
    if numer <= 0 or denom <= 0:
        raise PricingPublicationError("全局进货折扣分子/分母必须大于 0")
    try:
        reward_bps = decimal_rate_to_bps(snapshot.get("agent_purchase_bonus_rate"))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise PricingPublicationError("agent_purchase_bonus_rate 非法") from exc
    if reward_bps < 0 or reward_bps > 10000:
        raise PricingPublicationError("agent_purchase_bonus_rate 必须在 0%..100%")

    entries: List[Dict[str, Any]] = []
    for option in enabled:
        option_id = str(option["option_id"])
        amount = int(option["amount_cents"])
        paid_points = points_for_amount(amount, numer, denom)
        bonus_points = (
            points_at_rate(paid_points, reward_bps)
            if bool(option.get("reward_eligible"))
            else 0
        )
        source_ref = {
            "kind": "agent_purchase_option",
            "option_id": option_id,
            "amount_cents": amount,
            "option": _json_safe(option),
        }
        entries.append({
            "product_code": option_id,
            "base_price_cents": amount,
            "multiplier_bps": 10000,
            "final_price_cents": amount,
            "paid_points": paid_points,
            "bonus_points": bonus_points,
            "cost_floor_cents": None,
            "usage_example_version": None,
            "source_ref_jsonb": source_ref,
        })

    source_payload = {
        "materializer_version": MATERIALIZER_VERSION,
        "catalog_type": "procurement",
        "scope_key": PLATFORM_SCOPE,
        "config_epoch": int(config_epoch),
        "pricing_config_snapshot": snapshot,
        "entries": entries,
    }
    source_fingerprint = _fingerprint(source_payload)
    calc_meta = {
        "schema_version": 1,
        "materializer_version": MATERIALIZER_VERSION,
        "source_kind": "system_settings.pricing_config.agent_purchase_options",
        "source_revision": snapshot["agent_purchase_catalog_version"],
        "source_fingerprint": source_fingerprint,
        "config_epoch": int(config_epoch),
        "pricing_config_snapshot": snapshot,
        "entry_count": len(entries),
    }
    return {
        "catalog_type": "procurement",
        "scope_key": PLATFORM_SCOPE,
        "version_code": f"proc-{source_fingerprint[:24]}",
        "source_fingerprint": source_fingerprint,
        "calc_meta": calc_meta,
        "entries": entries,
    }


def _target_service_user_ids(cur) -> List[int]:
    """Same explicit business target set as public-account-code governance."""
    cur.execute(
        """
        SELECT DISTINCT u.id
          FROM users u
          LEFT JOIN user_wallets w ON w.user_id = u.id
         WHERE COALESCE(u.is_active, 1) = 1
           AND (
                COALESCE(w.agent_level, 0) >= 1
                OR EXISTS (
                    SELECT 1 FROM customer_agent_bindings b
                     WHERE b.agent_user_id = u.id
                )
                OR EXISTS (
                    SELECT 1 FROM agent_sku_overrides o
                     WHERE o.agent_user_id = u.id
                       AND o.is_active = TRUE
                       AND o.deleted_at IS NULL
                )
           )
         ORDER BY u.id
        """
    )
    return [int(_row_get(row, "id", 0)) for row in cur.fetchall()]


def _lock_management_source_tables(cur) -> Dict[str, bool]:
    """Fence phantom INSERT/UPDATE/DELETE while a publication batch is materialized.

    Row-level FOR SHARE is insufficient for rows that do not exist yet.  SHARE table
    locks conflict with writers' ROW EXCLUSIVE locks but allow concurrent readers; they
    last only for the caller transaction.  The optional override table is absent in some
    fresh/minimal environments, where template wholesale remains the deterministic cost.
    """
    cur.execute("LOCK TABLE agent_sku_overrides IN SHARE MODE")
    cur.execute("SELECT to_regclass('agent_pricing_overrides') AS table_name")
    row = cur.fetchone()
    override_table_present = bool(_row_get(row, "table_name", 0))
    if override_table_present:
        cur.execute("LOCK TABLE agent_pricing_overrides IN SHARE MODE")
    return {"agent_pricing_overrides_table_present": override_table_present}


def lock_retail_management_sources(cur) -> Dict[str, bool]:
    """Take the one global writer fence before any retail management DML.

    The exclusive advisory lock serializes the otherwise incompatible
    ``SHARE -> ROW EXCLUSIVE`` upgrade. Callers that mutate and publish must call
    this before their first write; materializers call it again idempotently.
    """
    # Global lock order is pricing config -> retail management source.  Cost
    # validation needs both; taking them in the opposite order can deadlock an
    # admin pricing-config writer that is waiting to update an override row.
    require_xact_scope(cur, where="pricing_publication.lock_retail_management_sources")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", _PRICING_CONFIG_LOCK)
    require_xact_scope(cur, where="pricing_publication.lock_retail_management_sources")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (_PUBLICATION_LOCK_NAMESPACE, _RETAIL_MANAGEMENT_LOCK_SCOPE),
    )
    return _lock_management_source_tables(cur)


def _service_code(cur, agent_user_id: int) -> str:
    cur.execute("SELECT id, is_active FROM users WHERE id=%s FOR SHARE", (int(agent_user_id),))
    user = cur.fetchone()
    if not user:
        raise PricingPublicationError(f"服务商账号 {agent_user_id} 不存在")
    is_active = _row_get(user, "is_active", 1, 1)
    if is_active in (False, 0, "0"):
        raise PricingPublicationError(f"服务商账号 {agent_user_id} 已停用")

    cur.execute(
        "SELECT service_account_code FROM public_account_codes WHERE user_id=%s FOR SHARE",
        (int(agent_user_id),),
    )
    row = cur.fetchone()
    code = str(_row_get(row, "service_account_code", 0, "") or "")
    if not code:
        raise PricingPublicationError(f"服务商 {agent_user_id} 缺少已预分配 SV 编号")
    if not _PUBLIC_SERVICE_CODE_RE.fullmatch(code):
        raise PricingPublicationError(f"服务商 {agent_user_id} 的 SV 编号格式非法")
    return code


def _retail_product_code(
    agent_user_id: int,
    *,
    retail_sku_id: str,
) -> str:
    identity = str(retail_sku_id or "").strip()
    if not re.fullmatch(r"RSKU-[A-Z0-9-]{8,59}", identity):
        raise PricingPublicationError("retail product identity 缺失")
    digest = hashlib.sha256(
        f"retail:{int(agent_user_id)}:{identity}".encode("utf-8")
    ).hexdigest()[:24]
    return f"svsku_{digest}"


def _load_retail_source_rows(cur, agent_user_id: int) -> Dict[str, Any]:
    """Load the only writable retail management source, with no template fallback."""
    cur.execute(
        """
        SELECT o.id AS override_id,
               o.agent_user_id,
               o.sku_template_id,
               o.source_template_id,
               o.retail_sku_id,
               o.version AS retail_sku_version,
               o.points_granted,
               o.retail_cents,
               o.custom_name,
               o.custom_subtitle,
               o.custom_sales_pitch,
               o.custom_scene,
               o.sort_order,
               o.is_active AS override_active,
               o.deleted_at,
               o.updated_at AS override_updated_at
          FROM agent_sku_overrides o
         WHERE o.agent_user_id = %s
         ORDER BY o.id
         FOR SHARE OF o
        """,
        (int(agent_user_id),),
    )
    override_rows = [dict(row) for row in cur.fetchall()]
    sellable = [
        {**row, "source_kind": "agent_retail_sku"}
        for row in override_rows
        if bool(row.get("override_active")) and row.get("deleted_at") is None
    ]

    return {
        "sellable_rows": sorted(
            sellable,
            key=lambda row: (
                int(row.get("sort_order") or 0),
                int(row["override_id"]),
            ),
        ),
        # Include inactive/tombstoned rows in the fingerprint so every management
        # mutation yields a provable catalog generation even though no fallback exists.
        "override_state": _json_safe(override_rows),
    }


def _effective_retail_cost_context(
    cur,
    *,
    agent_user_id: int,
    points_granted: int,
    pricing_config: Dict[str, Any],
) -> Dict[str, Any]:
    from services.agent_pricing import resolve_agent_effective_retail_cost

    try:
        return resolve_agent_effective_retail_cost(
            cur,
            agent_user_id=int(agent_user_id),
            points_granted=int(points_granted),
            pricing_config=pricing_config,
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise PricingPublicationError("服务商当前有效成本不可用") from exc


def _build_retail_plan(
    cur,
    *,
    agent_user_id: int,
    config: Dict[str, Any],
    config_epoch: int,
) -> Dict[str, Any]:
    agent_user_id = int(agent_user_id)
    service_code = _service_code(cur, agent_user_id)
    cur.execute("SELECT to_regclass('agent_pricing_overrides') AS table_name")
    table_row = cur.fetchone()
    override_table_present = bool(_row_get(table_row, "table_name", 0))
    try:
        discount = resolve_discount(cur, agent_user_id, config)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise PricingPublicationError(f"服务商 {service_code} 有效出厂折扣非法") from exc
    discount_meta = {
        **discount,
        "agent_pricing_overrides_table_present": override_table_present,
        "effective_cost_source": (
            "agent_pricing_overrides.wholesale_ratio"
            if discount["source"] == "agent_override"
            else "pricing_config.wholesale_ratio"
        ),
    }
    source_state = _load_retail_source_rows(cur, agent_user_id)
    rows = source_state["sellable_rows"]
    entries: List[Dict[str, Any]] = []
    source_rows: List[Dict[str, Any]] = []
    seen_products: set[str] = set()
    for raw_row in rows:
        row = dict(raw_row)
        override_id = int(row.get("override_id") or 0)
        retail_sku_id = str(row.get("retail_sku_id") or "").strip()
        retail_sku_version = int(row.get("retail_sku_version") or 0)
        sku_template_id = (
            int(row["sku_template_id"]) if row.get("sku_template_id") is not None else None
        )
        source_template_id = (
            int(row["source_template_id"]) if row.get("source_template_id") is not None else None
        )
        points_granted = int(row.get("points_granted") or 0)
        retail_cents = int(row.get("retail_cents") or 0)
        cost_context = _effective_retail_cost_context(
            cur,
            agent_user_id=agent_user_id,
            points_granted=points_granted,
            pricing_config=config,
        )
        wholesale_cents = int(cost_context["effective_cost_cents"])
        source_kind = str(row.get("source_kind") or "")
        display_name = str(row.get("custom_name") or "").strip()
        if (
            source_kind != "agent_retail_sku"
            or override_id <= 0
            or not re.fullmatch(r"RSKU-[A-Z0-9-]{8,59}", retail_sku_id)
            or retail_sku_version <= 0
            or not display_name
        ):
            raise PricingPublicationError(f"服务商 {service_code} 存在不完整 SKU 来源行")
        if retail_cents <= 0 or retail_cents > _MAX_CENTS:
            raise PricingPublicationError(f"SKU {source_kind}/{override_id or sku_template_id} 零售价超出允许范围")
        if wholesale_cents <= 0 or wholesale_cents > _MAX_CENTS:
            raise PricingPublicationError(f"SKU {source_kind}/{override_id or sku_template_id} 出厂成本超出允许范围")
        if retail_cents <= wholesale_cents:
            raise PricingPublicationError(
                f"SKU {source_kind}/{override_id or sku_template_id} 售价必须高于当前有效成本"
            )

        product_code = _retail_product_code(
            agent_user_id,
            retail_sku_id=retail_sku_id,
        )
        if product_code in seen_products:
            raise PricingPublicationError(f"服务商 {service_code} 生成了重复 product_code")
        seen_products.add(product_code)
        source_ref = {
            "kind": source_kind,
            "retail_sku_id": retail_sku_id,
            "retail_sku_version": retail_sku_version,
            "sku_template_id": sku_template_id,
            "source_template_id": source_template_id,
            "override_id": override_id,
            "agent_user_id": agent_user_id,
            "template_code": None,
            "sku_type": "credit_pack",
            "display_name": display_name,
            "subtitle": row.get("custom_subtitle"),
            "sales_pitch": row.get("custom_sales_pitch"),
            "scene": row.get("custom_scene"),
            "wholesale_cents": wholesale_cents,
            "effective_cost_version": str(cost_context["cost_basis_version"]),
            "retail_cents": retail_cents,
            "points_granted": points_granted,
        }
        # Retail exact price is represented as base=retail, multiplier=1.0.  Effective
        # factory cost is separately immutable in cost_floor/source_ref.  This avoids
        # lossy bps reconstruction for arbitrary integer-cent package prices.
        entries.append({
            "product_code": product_code,
            "base_price_cents": retail_cents,
            "multiplier_bps": 10000,
            "final_price_cents": retail_cents,
            "paid_points": points_granted,
            "bonus_points": 0,
            "cost_floor_cents": wholesale_cents,
            "usage_example_version": None,
            "source_ref_jsonb": source_ref,
        })
        source_rows.append({
            **source_ref,
            "override_updated_at": row.get("override_updated_at"),
        })

    source_payload = {
        "materializer_version": MATERIALIZER_VERSION,
        "catalog_type": "retail",
        "scope_key": service_code,
        "config_epoch": int(config_epoch),
        "discount_snapshot": _json_safe(discount_meta),
        "retail_wholesale_policy": "canonical_points_then_channel_effective_cost",
        "override_state": source_state["override_state"],
        "source_rows": source_rows,
        "entries": entries,
    }
    source_fingerprint = _fingerprint(source_payload)
    calc_meta = {
        "schema_version": 2,
        "materializer_version": MATERIALIZER_VERSION,
        "source_kind": "active_agent_sku_overrides",
        "source_revision": source_fingerprint[:24],
        "source_fingerprint": source_fingerprint,
        "config_epoch": int(config_epoch),
        "agent_user_id": agent_user_id,
        "service_account_code": service_code,
        "discount_snapshot": _json_safe(discount_meta),
        "retail_wholesale_policy": "canonical_points_then_channel_effective_cost",
        "entry_count": len(entries),
    }
    return {
        "catalog_type": "retail",
        "scope_key": service_code,
        "version_code": f"retail-{source_fingerprint[:24]}",
        "source_fingerprint": source_fingerprint,
        "calc_meta": calc_meta,
        "entries": entries,
        "agent_user_id": agent_user_id,
    }


def _entry_contract(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "product_code": str(row["product_code"]),
        "base_price_cents": int(row["base_price_cents"]),
        "multiplier_bps": int(row["multiplier_bps"]),
        "final_price_cents": int(row["final_price_cents"]),
        "paid_points": int(row["paid_points"]),
        "bonus_points": int(row.get("bonus_points") or 0),
        "cost_floor_cents": (
            int(row["cost_floor_cents"])
            if row.get("cost_floor_cents") is not None
            else None
        ),
        "usage_example_version": row.get("usage_example_version"),
        "source_ref_jsonb": _json_object(row.get("source_ref_jsonb")),
    }


def _catalog_content_matches(cur, version_id: int, plan: Dict[str, Any]) -> bool:
    cur.execute(
        """
        SELECT product_code, base_price_cents, multiplier_bps, final_price_cents,
               paid_points, bonus_points, cost_floor_cents, usage_example_version,
               source_ref_jsonb
          FROM pricing_catalog_entries
         WHERE version_id=%s
         ORDER BY product_code
        """,
        (int(version_id),),
    )
    actual = sorted(
        (_entry_contract(dict(row)) for row in cur.fetchall()),
        key=lambda item: item["product_code"],
    )
    expected = sorted(
        (_entry_contract(dict(row)) for row in plan["entries"]),
        key=lambda item: item["product_code"],
    )
    return _canonical_json(actual) == _canonical_json(expected)


def _current_published(cur, catalog_type: str, scope_key: str, *, for_update: bool = False):
    cur.execute(
        f"""
        SELECT *
          FROM pricing_catalog_versions
         WHERE catalog_type=%s AND scope_key=%s
           AND status='published' AND effective_to IS NULL
         LIMIT 1
         {'FOR UPDATE' if for_update else ''}
        """,
        (catalog_type, scope_key),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _inspect_plan(cur, plan: Dict[str, Any]) -> Dict[str, Any]:
    current = _current_published(cur, plan["catalog_type"], plan["scope_key"])
    current_meta = _json_object(current.get("calc_meta_jsonb")) if current else {}
    same_fingerprint = bool(
        current
        and current_meta.get("source_fingerprint") == plan["source_fingerprint"]
    )
    content_matches = bool(
        current and same_fingerprint and _catalog_content_matches(cur, current["id"], plan)
    )
    blocker = None
    if same_fingerprint and not content_matches:
        blocker = (
            f"{plan['catalog_type']}/{plan['scope_key']} 已发布内容与 source_fingerprint 不一致"
        )
    return {
        "catalog_type": plan["catalog_type"],
        "scope_key": plan["scope_key"],
        "source_fingerprint": plan["source_fingerprint"],
        "entry_count": len(plan["entries"]),
        "current_version_id": current.get("id") if current else None,
        "current_version_code": current.get("version_code") if current else None,
        "current_source_fingerprint": current_meta.get("source_fingerprint"),
        "content_matches": content_matches,
        "action": "noop" if same_fingerprint and content_matches else "publish",
        "blocker": blocker,
    }


def _publish_plan(
    cur,
    plan: Dict[str, Any],
    *,
    actor_id: int,
    reason: str,
    confirmed_empty_retail_scopes: Sequence[str] = (),
) -> Dict[str, Any]:
    actor_id = int(actor_id)
    reason = str(reason or "").strip()
    if actor_id <= 0:
        raise PricingPublicationError("actor_id 必须是有效 admin user_id")
    if not reason:
        raise PricingPublicationError("发布 reason 不能为空")

    # 🔴 发布期守卫:零售目录零条目 = 该服务方全体客户断供。
    # 刻意放在幂等短路**之前** —— 已经空掉的 scope 每次发布都要重新被人确认一次,
    # 免得它像 07-27 那样在无人察觉中一直空着。
    if plan["catalog_type"] == "retail" and not plan["entries"]:
        confirmed = {str(item) for item in (confirmed_empty_retail_scopes or ())}
        if str(plan["scope_key"]) not in confirmed:
            raise EmptyRetailCatalogRejected(str(plan["scope_key"]))
        plan = dict(plan)
        plan["calc_meta"] = {
            **_json_object(plan.get("calc_meta")),
            "empty_retail_confirmed_by": actor_id,
            "empty_retail_confirmed_reason": reason,
        }

    scope_lock = f"catalog:{plan['catalog_type']}:{plan['scope_key']}"
    require_xact_scope(cur, where="pricing_publication._publish_plan")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (_PUBLICATION_LOCK_NAMESPACE, scope_lock),
    )
    current = _current_published(
        cur, plan["catalog_type"], plan["scope_key"], for_update=True
    )
    current_meta = _json_object(current.get("calc_meta_jsonb")) if current else {}
    if current and current_meta.get("source_fingerprint") == plan["source_fingerprint"]:
        if not _catalog_content_matches(cur, current["id"], plan):
            raise PricingPublicationError(
                f"{plan['catalog_type']}/{plan['scope_key']} 指纹命中但目录内容不一致，疑似篡改"
            )
        return {
            "changed": False,
            "catalog_type": plan["catalog_type"],
            "scope_key": plan["scope_key"],
            "version_id": int(current["id"]),
            "version_code": str(current["version_code"]),
            "source_fingerprint": plan["source_fingerprint"],
            "entry_count": len(plan["entries"]),
        }

    cur.execute(
        """
        SELECT id, status, calc_meta_jsonb
          FROM pricing_catalog_versions
         WHERE catalog_type=%s AND scope_key=%s AND version_code=%s
         FOR UPDATE
        """,
        (plan["catalog_type"], plan["scope_key"], plan["version_code"]),
    )
    collision = cur.fetchone()
    if collision:
        raise PricingPublicationError(
            f"版本码 {plan['version_code']} 已存在但不是当前幂等版本，拒绝覆盖"
        )

    cur.execute(
        """
        INSERT INTO pricing_catalog_versions
            (catalog_type, scope_key, version_code, status, reason, created_by,
             calc_meta_jsonb, created_at, updated_at)
        VALUES (%s,%s,%s,'draft',%s,%s,%s,NOW(),NOW())
        RETURNING id
        """,
        (
            plan["catalog_type"],
            plan["scope_key"],
            plan["version_code"],
            reason,
            actor_id,
            json.dumps(plan["calc_meta"], ensure_ascii=False, default=_json_default),
        ),
    )
    version_id = int(_row_get(cur.fetchone(), "id", 0))
    for entry in plan["entries"]:
        cur.execute(
            """
            INSERT INTO pricing_catalog_entries
                (version_id, product_code, base_price_cents, multiplier_bps,
                 final_price_cents, paid_points, bonus_points, cost_floor_cents,
                 usage_example_version, source_ref_jsonb)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                version_id,
                entry["product_code"],
                int(entry["base_price_cents"]),
                int(entry["multiplier_bps"]),
                int(entry["final_price_cents"]),
                int(entry["paid_points"]),
                int(entry.get("bonus_points") or 0),
                entry.get("cost_floor_cents"),
                entry.get("usage_example_version"),
                json.dumps(entry["source_ref_jsonb"], ensure_ascii=False, default=_json_default),
            ),
        )

    if current:
        cur.execute(
            """
            UPDATE pricing_catalog_versions
               SET status='archived', effective_to=NOW(), archived_at=NOW(), updated_at=NOW()
             WHERE id=%s AND status='published' AND effective_to IS NULL
            """,
            (int(current["id"]),),
        )
        if cur.rowcount != 1:
            raise PricingPublicationError("当前已发布目录归档竞态，发布已回滚")

    cur.execute(
        """
        UPDATE pricing_catalog_versions
           SET status='published', effective_from=NOW(), effective_to=NULL,
               approved_by=%s, approved_at=NOW(), published_at=NOW(), updated_at=NOW()
         WHERE id=%s AND status='draft'
         RETURNING version_code
        """,
        (actor_id, version_id),
    )
    published = cur.fetchone()
    if not published:
        raise PricingPublicationError("新目录发布状态推进失败")
    return {
        "changed": True,
        "catalog_type": plan["catalog_type"],
        "scope_key": plan["scope_key"],
        "version_id": version_id,
        "version_code": str(_row_get(published, "version_code", 0)),
        "previous_version_id": int(current["id"]) if current else None,
        "source_fingerprint": plan["source_fingerprint"],
        "entry_count": len(plan["entries"]),
    }


def materialize_procurement_catalog(
    cur,
    *,
    actor_id: int,
    reason: str,
    expected_epoch: Optional[int] = None,
    _context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cursor-level PLATFORM_BASE materializer; never commits."""
    context = _context or _load_context(cur, expected_epoch=expected_epoch)
    if expected_epoch is not None and int(expected_epoch) != int(context["config_epoch"]):
        raise PublicationEpochConflict(int(expected_epoch), int(context["config_epoch"]))
    plan = _build_procurement_plan(context["config"], context["config_epoch"])
    return _publish_plan(cur, plan, actor_id=int(actor_id), reason=str(reason).strip())


def materialize_retail_catalog(
    cur,
    *,
    agent_user_id: int,
    actor_id: int,
    reason: str,
    expected_epoch: Optional[int] = None,
    _context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cursor-level SV-* retail materializer; never commits or creates public codes."""
    context = _context or _load_context(cur, expected_epoch=expected_epoch)
    if expected_epoch is not None and int(expected_epoch) != int(context["config_epoch"]):
        raise PublicationEpochConflict(int(expected_epoch), int(context["config_epoch"]))
    lock_retail_management_sources(cur)
    plan = _build_retail_plan(
        cur,
        agent_user_id=int(agent_user_id),
        config=context["config"],
        config_epoch=context["config_epoch"],
    )
    return _publish_plan(cur, plan, actor_id=int(actor_id), reason=str(reason).strip())


def _dry_run_cursor(
    cur,
    *,
    service_user_ids: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    context = _load_context(cur)
    lock_retail_management_sources(cur)
    target_mode = "explicit" if service_user_ids is not None else "all_active"
    targets = (
        _normalize_user_ids(service_user_ids)
        if service_user_ids is not None
        else _target_service_user_ids(cur)
    )
    blockers: List[str] = []
    scopes: List[Dict[str, Any]] = []

    try:
        procurement = _build_procurement_plan(context["config"], context["config_epoch"])
        inspection = _inspect_plan(cur, procurement)
        scopes.append(inspection)
        if inspection["blocker"]:
            blockers.append(inspection["blocker"])
    except PricingPublicationError as exc:
        blockers.append(f"procurement/PLATFORM_BASE: {exc}")

    for agent_user_id in targets:
        try:
            plan = _build_retail_plan(
                cur,
                agent_user_id=agent_user_id,
                config=context["config"],
                config_epoch=context["config_epoch"],
            )
            inspection = _inspect_plan(cur, plan)
            inspection["agent_user_id"] = agent_user_id
            scopes.append(inspection)
            if inspection["blocker"]:
                blockers.append(inspection["blocker"])
        except PricingPublicationError as exc:
            blockers.append(f"retail/agent={agent_user_id}: {exc}")
            scopes.append({
                "catalog_type": "retail",
                "agent_user_id": agent_user_id,
                "scope_key": None,
                "action": "blocked",
                "blocker": str(exc),
            })

    needs_publication = sum(1 for scope in scopes if scope.get("action") == "publish")
    blocked_count = sum(1 for scope in scopes if scope.get("action") == "blocked" or scope.get("blocker"))
    reviewed_scopes = sorted(
        [
            {
                "catalog_type": str(scope["catalog_type"]),
                "scope_key": str(scope["scope_key"]),
                "source_fingerprint": str(scope["source_fingerprint"]),
            }
            for scope in scopes
            if scope.get("catalog_type")
            and scope.get("scope_key")
            and scope.get("source_fingerprint")
        ],
        key=lambda item: (item["catalog_type"], item["scope_key"]),
    )
    return {
        "config_epoch": int(context["config_epoch"]),
        "target_service_count": len(targets),
        "scopes": scopes,
        "needs_publication_count": needs_publication,
        "blocked_count": blocked_count,
        "publishable": not blockers,
        "ready": not blockers and needs_publication == 0,
        "blockers": blockers,
        "review_contract": {
            "config_epoch": int(context["config_epoch"]),
            "target_mode": target_mode,
            "service_user_ids": list(targets),
            "scopes": reviewed_scopes,
        },
        # NOT VALID is an intentional legacy state: readiness must inspect
        # pg_constraint.convalidated and keep ready=false until cleanup validates it.
        "constraint_policy": "all required constraints must exist and be convalidated",
    }


def dry_run_publication(scope_service_user_ids=None) -> Dict[str, Any]:
    """Admin read-only impact analysis; no catalogs, codes, relationships or flags are written."""
    with get_db() as conn:
        return _dry_run_cursor(conn.cursor(), service_user_ids=scope_service_user_ids)


def publish_all(
    actor_id,
    reason,
    *,
    expected_epoch,
    reviewed_target_mode,
    reviewed_service_user_ids,
    reviewed_scopes,
    confirmed_empty_retail_scopes: Sequence[str] = (),
) -> Dict[str, Any]:
    """Atomically publish PLATFORM_BASE and every selected/default SV-* retail scope."""
    actor_id = int(actor_id)
    reason = str(reason or "").strip()
    if actor_id <= 0:
        raise PricingPublicationError("actor_id 必须是有效 admin user_id")
    if not reason:
        raise PricingPublicationError("发布 reason 不能为空")
    if expected_epoch is None:
        raise PublicationReviewConflict("发布必须绑定预览 config_epoch")
    target_mode = str(reviewed_target_mode or "").strip()
    if target_mode not in ("all_active", "explicit"):
        raise PublicationReviewConflict("发布审核 target_mode 非法 · 请重新预览")
    expected_targets = _normalize_user_ids(reviewed_service_user_ids)
    expected_scope_contract = _normalize_reviewed_scopes(reviewed_scopes)

    with get_db() as conn:
        cur = conn.cursor()
        context = _load_context(cur, expected_epoch=expected_epoch)
        _lock_management_source_tables(cur)
        targets = (
            _target_service_user_ids(cur)
            if target_mode == "all_active"
            else list(expected_targets)
        )
        if targets != expected_targets:
            raise PublicationReviewConflict(
                "发布目标服务商集合已变化 · 请重新预览后再发布"
            )

        # Build every source plan before the first catalog write.  FOR SHARE locks keep
        # selected source rows stable; any invalid retail scope aborts the whole batch.
        plans = [_build_procurement_plan(context["config"], context["config_epoch"])]
        plans.extend(
            _build_retail_plan(
                cur,
                agent_user_id=agent_user_id,
                config=context["config"],
                config_epoch=context["config_epoch"],
            )
            for agent_user_id in targets
        )
        actual_scope_contract = _review_scope_collection(plans)
        if actual_scope_contract != expected_scope_contract:
            raise PublicationReviewConflict(
                "管理源或目录 scope 已在预览后变化 · 请重新预览后再发布"
            )
        results = [
            _publish_plan(
                cur,
                plan,
                actor_id=actor_id,
                reason=reason,
                confirmed_empty_retail_scopes=confirmed_empty_retail_scopes,
            )
            for plan in plans
        ]

    return {
        "config_epoch": int(context["config_epoch"]),
        "target_service_count": len(targets),
        "results": results,
        "published_count": sum(1 for result in results if result["changed"]),
        "unchanged_count": sum(1 for result in results if not result["changed"]),
    }


def publication_status(cur=None) -> Dict[str, Any]:
    """Read-only current-source vs published-catalog status for the readiness gate."""
    if cur is not None:
        return _dry_run_cursor(cur)
    with get_db() as conn:
        return _dry_run_cursor(conn.cursor())
