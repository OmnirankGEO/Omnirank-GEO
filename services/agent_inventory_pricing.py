"""代理进货目录的纯整数计算与事务 adapter。

纯函数不访问 DB/缓存、不写入；cursor adapter 只负责解析现有 override 与渠道等级上下文。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional
from db.xact_lock_guard import require_xact_scope


CATALOG_VERSION_RE = re.compile(r"^agent-purchase-v([1-9][0-9]*)$")
OPTION_ID_RE = re.compile(r"^apo_[a-z0-9][a-z0-9_-]{5,63}$")
FREE_AMOUNT_OPTION_ID = "custom-amount"
# 现金订单仍落在 INTEGER amount_cents；留出数据库边界余量，同时允许既有大客户场景。
# 对固定档和自由金额使用同一业务上限，避免外部支付成功后才发现下游字段无法承载。
MAX_AGENT_PURCHASE_AMOUNT_CENTS = 2_000_000_000
# v2 binary 会在首次 activation 前创建 generation=2 订单，不能作为安全部署目标。
# v3 明确包含 Phase A 新订单停写门；部署/回滚脚本不得把 v2 当作等价能力。
INVENTORY_SNAPSHOT_RUNTIME_CAPABILITY = "agent-inventory-snapshot-v3"
QUOTE_SCHEMA_VERSION = 2
CALCULATOR_VERSION = "agent-inventory-pricing-v2"
ACTIVATION_MARKER_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?$"
)

QUOTE_FINGERPRINT_FIELDS = (
    "quote_schema_version", "calculator_version", "catalog_version",
    "option_id", "option_source", "amount_cents", "reward_eligible",
    "discount_source", "discount_numer", "discount_denom",
    "channel_tier_enabled", "tier_at_order", "tier_source",
    "tier_bonus_rate_bps", "tier_bonus_points",
    "founder_eligibility_source", "founder_seat_policy", "founder_cap_snapshot",
    "founder_min_first_order_yuan_snapshot", "founder_bonus_rate_bps_snapshot",
    "founder_bonus_points_if_eligible", "bonus_validity_months",
    "base_points", "bonus_rate_bps", "bonus_points", "total_points",
)


def stable_option_id(amount_cents: int) -> str:
    digest = hashlib.sha256(f"agent-purchase:{int(amount_cents)}".encode()).hexdigest()[:12]
    return f"apo_{digest}"


def normalize_catalog_options(rows: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """兼容旧行并稳定排序；保留停用行，不做默认合并。"""
    normalized: List[Dict[str, Any]] = []
    for index, raw in enumerate(rows or []):
        item = dict(raw or {})
        amount = int(item.get("amount_cents") or 0)
        option_id = str(item.get("option_id") or stable_option_id(amount))
        normalized.append({
            "option_id": option_id,
            "amount_cents": amount,
            "is_enabled": bool(item.get("is_enabled", True)),
            "sort_order": int(item.get("sort_order", index)),
            "reward_eligible": bool(item.get("reward_eligible", item.get("is_first_month_bonus", True))),
            # 兼容旧读取方；资金计算从不信任或消费这些历史展示字段。
            "base_points": int(item.get("base_points") or 0),
            "bonus_points": int(item.get("bonus_points") or 0),
            "label": str(item.get("label") or f"¥{amount // 100} 进货"),
            "is_first_month_bonus": bool(item.get("is_first_month_bonus", item.get("reward_eligible", True))),
        })
    return sorted(normalized, key=lambda row: (row["sort_order"], row["amount_cents"], row["option_id"]))


def next_catalog_version(current: str) -> str:
    match = CATALOG_VERSION_RE.fullmatch(str(current or ""))
    if not match:
        raise ValueError("catalog_version 非法")
    return f"agent-purchase-v{int(match.group(1)) + 1}"


def advance_catalog_version_cursor(cursor) -> tuple[str, int]:
    """推进覆盖全部进货快照输入的统一 revision，并在同一事务 bump epoch。"""
    from config.pricing_config import get_agent_purchase_catalog_version, merge_pricing_config
    from services.config_epoch import bump_epoch_cursor

    cursor.execute("SELECT value FROM system_settings WHERE key='pricing_config' FOR UPDATE")
    row = cursor.fetchone()
    raw = _row_value(row, "value", 0, {})
    if isinstance(raw, str):
        raw = json.loads(raw or "{}")
    raw = dict(raw) if isinstance(raw, dict) else {}
    current = get_agent_purchase_catalog_version(merge_pricing_config(raw, include_env=False))
    new_version = next_catalog_version(current)
    raw["agent_purchase_catalog_version"] = new_version
    cursor.execute(
        """
        INSERT INTO system_settings(key,value,value_type,description,updated_at)
        VALUES ('pricing_config',%s,'json','定价系数动态配置(D5)',NOW())
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,value_type='json',updated_at=NOW()
        """,
        (json.dumps(raw, ensure_ascii=False),),
    )
    return new_version, bump_epoch_cursor(cursor)


def decimal_rate_to_bps(value: Any) -> int:
    rate = Decimal(str(value or 0))
    return int((rate * Decimal(10000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def points_for_amount(amount_cents: int, discount_numer: int, discount_denom: int) -> int:
    amount, numer, denom = int(amount_cents), int(discount_numer), int(discount_denom)
    if amount <= 0 or numer <= 0 or denom <= 0:
        raise ValueError("金额与折扣分子分母必须大于 0")
    if amount > MAX_AGENT_PURCHASE_AMOUNT_CENTS:
        raise ValueError("单笔进货金额超过业务上限")
    return amount * denom // numer


def points_at_rate(base_points: int, rate_bps: int) -> int:
    base, bps = int(base_points), int(rate_bps)
    if base <= 0 or bps <= 0:
        return 0
    if bps > 10000:
        raise ValueError("奖励比例不能超过 100%")
    return (base * bps + 5000) // 10000


def calculate_purchase(
    *,
    amount_cents: int,
    discount_numer: int,
    discount_denom: int,
    reward_rate_bps: int,
    reward_eligible: bool,
) -> Dict[str, int]:
    """唯一最终资金计算：全程整数，统一 half-up 到整数算力。"""
    base = points_for_amount(amount_cents, discount_numer, discount_denom)
    bonus = points_at_rate(base, reward_rate_bps) if reward_eligible else 0
    return {"base_points": base, "bonus_points": bonus, "total_points": base + bonus}


def compute_quote_fingerprint(snapshot: Dict[str, Any], *, agent_user_id: int) -> str:
    """内容寻址的单笔报价版本；只哈希规范化资金输入和派生结果。"""
    missing = [key for key in QUOTE_FINGERPRINT_FIELDS if key not in snapshot]
    if missing:
        raise ValueError(f"报价指纹缺字段: {','.join(missing)}")
    payload = {key: snapshot[key] for key in QUOTE_FINGERPRINT_FIELDS}
    founder_min = Decimal(str(payload["founder_min_first_order_yuan_snapshot"]))
    if not founder_min.is_finite() or founder_min < 0:
        raise ValueError("创始席门槛快照非法")
    payload["founder_min_first_order_yuan_snapshot"] = format(founder_min.normalize(), "f")
    if not bool(payload["channel_tier_enabled"]):
        # 渠道关闭时这些字段不参与本单到账或有效期，统一为中性值以避免无资金变化的误拒绝。
        payload.update({
            "founder_cap_snapshot": 0,
            "founder_min_first_order_yuan_snapshot": "0",
            "founder_bonus_rate_bps_snapshot": 0,
            "founder_bonus_points_if_eligible": 0,
            "bonus_validity_months": 0,
        })
    payload["agent_user_id"] = int(agent_user_id)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def finalize_quote_snapshot(snapshot: Dict[str, Any], *, agent_user_id: int) -> Dict[str, Any]:
    result = dict(snapshot)
    result["quote_schema_version"] = QUOTE_SCHEMA_VERSION
    result["calculator_version"] = CALCULATOR_VERSION
    result["quote_fingerprint"] = compute_quote_fingerprint(result, agent_user_id=agent_user_id)
    return result


def insert_pricing_audit(
    cursor,
    *,
    admin_user_id: int,
    admin_username: Optional[str],
    request_id: str,
    module: str,
    summary: str,
    before_config: Dict[str, Any],
    after_config: Dict[str, Any],
    previous_catalog_version: str,
    catalog_version: str,
    ip_address: Optional[str],
    entity_type: str = "pricing_config",
    entity_id: int = 0,
    reason: Optional[str] = None,
) -> None:
    """在调用方价格事务内写既有 audit_logs；本函数不建表、不提交。

    [WP2 · 商业治理] request_id 与变更原因(reason)现在写入 audit_logs 的**一等列**
    (对齐 admin_user_governance_audits),不再仅埋在 before/after JSON 与 summary 文本里,
    便于按列检索与合规审计。JSON 快照与 summary 保持不变(向后兼容既有读取方)。
    `reason` 缺省回退到 `summary`,保证一等列 reason 始终有值。
    """
    reason_value = (reason or summary or "").strip() or summary
    before = {
        "request_id": request_id,
        "catalog_version": previous_catalog_version,
        "config": before_config,
    }
    after = {
        "request_id": request_id,
        "catalog_version": catalog_version,
        "config": after_config,
    }
    cursor.execute(
        """
        INSERT INTO audit_logs (
            user_id, username, action, module, entity_type, entity_id,
            summary, before_snapshot, after_snapshot, ip_address,
            request_id, reason, created_at
        ) VALUES (%s, %s, 'update', %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """,
        (
            int(admin_user_id), admin_username, module, entity_type, int(entity_id),
            f"{summary} {catalog_version} request_id={request_id}",
            json.dumps(before, ensure_ascii=False, default=str),
            json.dumps(after, ensure_ascii=False, default=str),
            ip_address,
            request_id, reason_value,
        ),
    )


def _row_value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    if not row:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[index]
    except Exception:
        return default


def inventory_snapshot_activation_state(cursor) -> Dict[str, Any]:
    """严格读取新进货订单写入门。

    Phase A 可继续展示报价和处理旧订单，但只有 activation 的
    marker、非 MVCC fence、已验证 constraint 和 trigger 同时就绪才能创建 gen2 订单。
    """
    cursor.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM system_settings
             WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT') AS marker_count,
            (SELECT value FROM system_settings
             WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT' LIMIT 1) AS marker_value,
            clock_timestamp()::timestamp AS database_now,
            COALESCE(
                pg_sequence_last_value(
                    to_regclass('agent_inventory_writer_generation_fence_seq')
                ),
                0
            )::INTEGER AS writer_generation,
            EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid=to_regclass('recharge_orders')
                  AND conname='check_agent_inventory_writer_generation_required'
                  AND convalidated
            ) AS constraint_ready,
            EXISTS (
                SELECT 1 FROM pg_trigger t
                JOIN pg_class c ON c.oid=t.tgrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=current_schema()
                  AND c.relname='recharge_orders'
                  AND t.tgname='trg_agent_inventory_snapshot_writer_generation'
                  AND NOT t.tgisinternal
                  AND t.tgenabled <> 'D'
            ) AS trigger_ready
        """
    )
    row = cursor.fetchone()
    marker_count = int(_row_value(row, "marker_count", 0, 0) or 0)
    marker_value = _row_value(row, "marker_value", 1)
    database_now = _row_value(row, "database_now", 2)
    marker_ready = False
    marker_text = str(marker_value or "").strip()
    if (
        marker_count == 1
        and ACTIVATION_MARKER_RE.fullmatch(marker_text)
        and isinstance(database_now, datetime)
    ):
        try:
            marker_at = datetime.fromisoformat(marker_text)
            marker_ready = marker_at.tzinfo is None and marker_at <= database_now
        except (TypeError, ValueError):
            marker_ready = False
    writer_generation = int(_row_value(row, "writer_generation", 3, 0) or 0)
    constraint_ready = bool(_row_value(row, "constraint_ready", 4, False))
    trigger_ready = bool(_row_value(row, "trigger_ready", 5, False))
    return {
        "marker_count": marker_count,
        "marker_ready": marker_ready,
        "writer_generation": writer_generation,
        "constraint_ready": constraint_ready,
        "trigger_ready": trigger_ready,
        "ready": (
            marker_ready
            and writer_generation == 2
            and constraint_ready
            and trigger_ready
        ),
    }


def _override_timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="microseconds")
    text = str(value).strip()
    return text or None


def resolve_procurement_discount_snapshot(
    cursor,
    agent_user_id: Optional[int],
    config: Dict[str, Any],
    *,
    published_catalog_version: str,
    require_override_schema: bool,
) -> Dict[str, Any]:
    """Lock one per-agent procurement discount snapshot in the caller transaction.

    The override writer owns the exclusive ``(920713, 1)`` transaction lock.  A
    shared lock here closes the missing-row/first-insert race before querying the
    optional row.  ``updated_at`` is the existing per-row version token: it changes
    on every admin save, so a changed/cleared/recreated override cannot make an old
    issued quote valid again merely by restoring the same ratio.
    """

    numer = int(config.get("wholesale_numer", 225))
    denom = int(config.get("wholesale_denom", 325))
    catalog_version = str(published_catalog_version or "").strip()
    if numer <= 0 or denom <= 0 or not catalog_version:
        raise ValueError("已发布进货折扣配置非法")

    require_xact_scope(cursor, where="agent_inventory_pricing.resolve_procurement_discount_snapshot")  # §1 硬闸:autocommit 下取事务锁=没锁
    cursor.execute("SELECT pg_advisory_xact_lock_shared(920713, 1)")
    cursor.execute("SELECT to_regclass('agent_pricing_overrides') AS table_name")
    table_row = cursor.fetchone()
    table_ready = bool(_row_value(table_row, "table_name", 0))
    if require_override_schema and not table_ready:
        raise ValueError("专属进货折扣表未就绪")

    source = "published_global"
    updated_at = None
    if agent_user_id and table_ready:
        cursor.execute(
            "SELECT wholesale_numer, wholesale_denom, updated_at "
            "FROM agent_pricing_overrides WHERE agent_user_id=%s FOR SHARE",
            (int(agent_user_id),),
        )
        row = cursor.fetchone()
        ov_n = _row_value(row, "wholesale_numer", 0)
        ov_d = _row_value(row, "wholesale_denom", 1)
        if (ov_n is None) != (ov_d is None):
            raise ValueError("专属进货折扣分子分母不完整")
        if ov_n is not None and ov_d is not None:
            numer, denom = int(ov_n), int(ov_d)
            updated_at = _override_timestamp(_row_value(row, "updated_at", 2))
            if (
                numer <= 0
                or denom <= 0
                or numer > denom
                or numer * 10 < denom * 3
                or not updated_at
            ):
                raise ValueError("专属进货折扣配置非法")
            source = "agent_override"

    version_payload = {
        "agent_user_id": int(agent_user_id or 0),
        "source": source,
        "numer": numer,
        "denom": denom,
        "override_updated_at": updated_at,
        "published_catalog_version": catalog_version,
        "published_global_numer": int(config.get("wholesale_numer", 225)),
        "published_global_denom": int(config.get("wholesale_denom", 325)),
    }
    version = hashlib.sha256(
        json.dumps(
            version_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {**version_payload, "discount_version": version}


def resolve_discount(cursor, agent_user_id: Optional[int], config: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy/admin adapter over the transaction-stable canonical resolver."""

    snapshot = resolve_procurement_discount_snapshot(
        cursor,
        agent_user_id,
        config,
        published_catalog_version=str(
            config.get("agent_purchase_catalog_version") or "legacy-runtime"
        ),
        require_override_schema=False,
    )
    return {
        "source": (
            "agent_override" if snapshot["source"] == "agent_override" else "global"
        ),
        "numer": int(snapshot["numer"]),
        "denom": int(snapshot["denom"]),
    }


def build_purchase_snapshot(
    cursor,
    *,
    config: Dict[str, Any],
    catalog_version: str,
    agent_user_id: Optional[int],
    amount_cents: int,
    option: Optional[Dict[str, Any]],
    locked_discount: Optional[Dict[str, Any]] = None,
    locked_base_points: Optional[int] = None,
    progress_amount_cents: Optional[int] = None,
) -> Dict[str, Any]:
    """用现有渠道服务解析上下文，再把纯值交给唯一计算器。"""
    if locked_discount is None:
        discount = resolve_discount(cursor, agent_user_id, config)
    else:
        discount = {
            "source": str(locked_discount.get("source") or "published_catalog"),
            "numer": int(locked_discount.get("numer") or 0),
            "denom": int(locked_discount.get("denom") or 0),
        }
        if discount["numer"] <= 0 or discount["denom"] <= 0:
            raise ValueError("已发布进货折扣快照非法")
    option_id = str((option or {}).get("option_id") or FREE_AMOUNT_OPTION_ID)
    reward_eligible = bool((option or {}).get("reward_eligible", False))
    computed_base = points_for_amount(amount_cents, discount["numer"], discount["denom"])
    if locked_base_points is None:
        base = computed_base
    else:
        base = int(locked_base_points)
        if base <= 0 or base != computed_base:
            raise ValueError("已发布进货基础算力与折扣快照不一致")

    from services.channel_tier import compute_purchase_bonus_projection, is_channel_tier_enabled

    channel_enabled = bool(agent_user_id and is_channel_tier_enabled(cursor))
    tier = "none"
    tier_source = "disabled"
    tier_rate_bps = 0
    rolling_before_snapshot = "0"
    projected_rolling_snapshot = "0"
    tier_override_until_snapshot = None
    benefit_floor_tier = None
    crosses_tier_threshold = False
    if channel_enabled:
        projection = compute_purchase_bonus_projection(
            cursor,
            int(agent_user_id),
            int(progress_amount_cents if progress_amount_cents is not None else amount_cents),
            pricing_config=config,
            base_points=base,
        )
        tier = str(projection.get("projected_tier") or "none")
        tier_source = str(projection.get("tier_source") or "automatic")
        tier_rate_bps = int(projection.get("bonus_rate_bps") or 0)
        rolling_before_snapshot = str(Decimal(str(projection.get("rolling_before_yuan") or 0)))
        projected_rolling_snapshot = str(Decimal(str(projection.get("projected_rolling_12m_yuan") or 0)))
        benefit_floor_tier = projection.get("benefit_floor_tier")
        crosses_tier_threshold = bool(projection.get("crosses_tier_threshold"))
        override_until = projection.get("tier_override_until")
        tier_override_until_snapshot = (
            override_until.isoformat() if hasattr(override_until, "isoformat")
            else (str(override_until) if override_until else None)
        )
        reward_eligible = True
        reward_rate_bps = tier_rate_bps
    else:
        reward_rate_bps = decimal_rate_to_bps(config.get("agent_purchase_bonus_rate", 0))

    calc = calculate_purchase(
        amount_cents=amount_cents,
        discount_numer=discount["numer"],
        discount_denom=discount["denom"],
        reward_rate_bps=reward_rate_bps,
        reward_eligible=reward_eligible,
    )
    tier_bonus = calc["bonus_points"] if channel_enabled else 0
    founder_cfg = dict(config.get("founding") or {})
    founder_cap = int(founder_cfg.get("cap", 10))
    if founder_cap < 0:
        raise ValueError("创始席位上限配置非法")
    validity_raw = config["bonus_validity_months"] if "bonus_validity_months" in config else 12
    bonus_validity_months = int(validity_raw)
    if bonus_validity_months < 1 or bonus_validity_months > 120:
        raise ValueError("奖励有效期配置非法")
    founder_rate_bps = decimal_rate_to_bps(founder_cfg.get("first_order_extra_bonus", 0)) if channel_enabled else 0
    founder_if_eligible = points_at_rate(calc["base_points"], founder_rate_bps)
    tier_label = {
        "certified": "认证渠道",
        "preferred": "优选渠道",
        "strategic": "战略渠道",
    }.get(tier, "当前渠道")
    tier_rule = ((config.get("agent_tier_config") or {}).get(tier) or {})
    tier_description = str(tier_rule.get("description") or "").strip()
    reward_description = (
        (tier_description or f"按{tier_label}奖励") if channel_enabled and tier_bonus > 0
        else ("适用进货奖励" if calc["bonus_points"] > 0 else "本档暂无固定奖励")
    )
    snapshot = {
        "catalog_version": catalog_version,
        "option_id": option_id,
        "option_source": "catalog" if option else "custom_amount",
        "amount_cents": int(amount_cents),
        "tier_progress_amount_cents": int(
            progress_amount_cents if progress_amount_cents is not None else amount_cents
        ),
        "base_points": calc["base_points"],
        "discount_source": discount["source"],
        "discount_numer": discount["numer"],
        "discount_denom": discount["denom"],
        "channel_tier_enabled": channel_enabled,
        "tier_at_order": tier,
        "tier_source": tier_source,
        "benefit_floor_tier": benefit_floor_tier,
        "crosses_tier_threshold": crosses_tier_threshold,
        "rolling_before_yuan_snapshot": rolling_before_snapshot,
        "projected_rolling_12m_yuan_snapshot": projected_rolling_snapshot,
        "tier_override_until_snapshot": tier_override_until_snapshot,
        "tier_bonus_rate_bps": tier_rate_bps,
        "tier_bonus_points": tier_bonus,
        "founder_eligibility_source": "existing_atomic_gate" if channel_enabled else "not_applicable",
        "founder_seat_policy": "payment_time_atomic_remaining_seat" if channel_enabled else "not_applicable",
        "founder_cap_snapshot": founder_cap,
        "founder_min_first_order_yuan_snapshot": str(founder_cfg.get("min_first_order_yuan", 500)),
        "founder_bonus_rate_bps_snapshot": founder_rate_bps,
        "founder_bonus_points_if_eligible": founder_if_eligible,
        "bonus_validity_months": bonus_validity_months,
        "bonus_points": calc["bonus_points"],
        "bonus_rate_bps": reward_rate_bps if reward_eligible else 0,
        "total_points": calc["total_points"],
        "reward_eligible": reward_eligible,
        "reward_description": reward_description,
    }
    return finalize_quote_snapshot(snapshot, agent_user_id=int(agent_user_id or 0))


def public_option(snapshot: Dict[str, Any], option: Dict[str, Any]) -> Dict[str, Any]:
    amount = int(snapshot["amount_cents"])
    bonus = int(snapshot["bonus_points"])
    label = f"¥{Decimal(amount) / Decimal(100):g} 进货 · 充值 {int(snapshot['base_points']):,}"
    if bonus:
        label += f" + 奖励 {bonus:,}"
    return {
        "option_id": option["option_id"],
        "amount_cents": amount,
        "base_points": int(snapshot["base_points"]),
        "bonus_points": bonus,
        "total_points": int(snapshot["total_points"]),
        "label": label,
        "is_first_month_bonus": bool(option.get("reward_eligible")),
        "reward_description": (
            "本档含赠送库存" if bonus > 0 else "本档暂无固定奖励"
        ),
        "quote_fingerprint": snapshot["quote_fingerprint"],
        "sort_order": int(option["sort_order"]),
    }
