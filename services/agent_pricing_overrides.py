"""
per-agent 定价系数覆盖 (D3 · 2026-06-04)

平台后台给【单个服务商】单独设系数(老板:动态设置代理的系数·丰俭由人·有降有涨):
  - 平台→服务商:出厂进货折扣 override (wholesale_numer/denom · NULL 回全局 pricing_config)
  - 服务商→客户:报价系数 / 额度包零售系数 admin 强制 override
    优先级: admin override > 服务商自设(users.quote_markup_ratio/agent_sku_markup_ratio) > 全局默认

抄 agent_tax_profiles 范式(per-agent · NULL/无记录回全局)。建表幂等(CREATE TABLE IF NOT EXISTS · 懒建)。

红线:
  - 不碰 billing.py · 不改扣费主链
  - B2 缓存防污染:per-agent 系数只在【读取/渲染时】经 markup_override 套用,
    【绝不】写回全局共享 keyword_price_cache(否则 A 服务商系数泄漏污染 B 及所有客户)
  - 建表/读取失败一律 fallback None → 回落全局默认(上线零感知·不阻塞核心报价/扣费)

关联:
  - docs/AI-CONTEXT/PRICING_COEFFICIENT_DYNAMIC_PLAN_2026-06-04.md (D3)
  - 范本: services/agent_pricing.py:get_agent_tax_rate_bps + api/admin_factory_api.py tax-profiles UPSERT
"""

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("GEO-AgentPricingOverrides")

_table_ensured = False
_RETAIL_MARKUP_LOCK_NAMESPACE = 920512
_OVERRIDE_FIELDS = (
    "quote_markup_override", "sku_markup_override",
    "wholesale_numer", "wholesale_denom", "note",
)


class AgentPricingOverrideVersionConflict(ValueError):
    def __init__(self, current_catalog_version: str):
        super().__init__("agent purchase catalog version conflict")
        self.current_catalog_version = current_catalog_version


def _override_dict(row: Any) -> Dict[str, Any]:
    if not row:
        return {key: None for key in _OVERRIDE_FIELDS}
    if isinstance(row, dict):
        return {key: row.get(key) for key in _OVERRIDE_FIELDS}
    return {key: row[index] for index, key in enumerate(_OVERRIDE_FIELDS)}


def ensure_pricing_overrides_table() -> bool:
    """运行时只做 schema 自检；DDL 仅允许 prestart migration 执行。"""
    global _table_ensured
    if _table_ensured:
        return True
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('agent_pricing_overrides') AS table_name")
            row = cur.fetchone()
            table_name = row.get("table_name") if isinstance(row, dict) else (row[0] if row else None)
        finally:
            conn.close()
        if not table_name:
            logger.error("agent_pricing_overrides 缺失；必须先执行 prestart migration")
            return False
        _table_ensured = True
        return True
    except Exception as exc:
        logger.warning("agent_pricing_overrides schema 自检失败(读取回落全局): %s", exc)
        return False


def get_agent_pricing_override(agent_user_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """读 per-agent 定价覆盖 · 无记录/表不存在/异常 → None(回落全局)。"""
    if not agent_user_id:
        return None
    if not ensure_pricing_overrides_table():
        return None
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT quote_markup_override, sku_markup_override, wholesale_numer, wholesale_denom, note
                FROM agent_pricing_overrides WHERE agent_user_id = %s
            """, (agent_user_id,))
            row = cur.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return dict(row)
            return {
                "quote_markup_override": row[0],
                "sku_markup_override": row[1],
                "wholesale_numer": row[2],
                "wholesale_denom": row[3],
                "note": row[4],
            }
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("get_agent_pricing_override 失败 agent=%s(回落全局): %s", agent_user_id, exc)
        return None


def get_agent_quote_markup_override(agent_user_id: Optional[int]) -> Optional[float]:
    """admin 强制报价系数(NULL=不覆盖·回落服务商自设/全局)。"""
    ov = get_agent_pricing_override(agent_user_id)
    if ov and ov.get("quote_markup_override") is not None:
        try:
            return float(ov["quote_markup_override"])
        except (TypeError, ValueError):
            return None
    return None


def get_agent_sku_markup_override(agent_user_id: Optional[int]) -> Optional[float]:
    """admin 强制额度包零售系数(NULL=不覆盖·回落服务商自设/全局)。"""
    ov = get_agent_pricing_override(agent_user_id)
    if ov and ov.get("sku_markup_override") is not None:
        try:
            return float(ov["sku_markup_override"])
        except (TypeError, ValueError):
            return None
    return None


def get_agent_wholesale_override(agent_user_id: Optional[int]) -> Optional[Tuple[int, int]]:
    """per-agent 出厂折扣 (numer, denom) · 两者都非空才生效 · 否则 None(回落全局)。"""
    ov = get_agent_pricing_override(agent_user_id)
    if ov and ov.get("wholesale_numer") and ov.get("wholesale_denom"):
        try:
            return int(ov["wholesale_numer"]), int(ov["wholesale_denom"])
        except (TypeError, ValueError):
            return None
    return None


def write_agent_pricing_override_atomic(
    agent_user_id: int,
    *,
    quote_markup_override: Optional[float] = None,
    sku_markup_override: Optional[float] = None,
    wholesale_numer: Optional[int] = None,
    wholesale_denom: Optional[int] = None,
    note: Optional[str] = None,
    clear_fields: Optional[list[str]] = None,
    expected_catalog_version: Optional[str],
    admin_user_id: int,
    admin_username: Optional[str],
    request_id: str,
    ip_address: Optional[str],
    audit_module: str,
) -> Dict[str, Any]:
    """单一事务保存专属规则、统一价格版本/epoch 与持久审计。

    `None` 在 upsert 模式表示保持原值；清空必须通过 clear_fields 明确表达。
    只要有效进货折扣发生变化，就必须校验 expected version 并推进统一 revision。
    """
    if not ensure_pricing_overrides_table():
        raise RuntimeError("agent_pricing_overrides schema 未就绪")
    allowed_clear = {"quote_markup_override", "sku_markup_override", "wholesale_numer", "wholesale_denom"}
    clear_set = set(clear_fields or [])
    if not clear_set.issubset(allowed_clear):
        raise ValueError("存在不允许清空的专属规则字段")
    wholesale_clear = {"wholesale_numer", "wholesale_denom"}.intersection(clear_set)
    if wholesale_clear and wholesale_clear != {"wholesale_numer", "wholesale_denom"}:
        raise ValueError("专属进货折扣分子分母必须同时清空")
    if (wholesale_numer is None) != (wholesale_denom is None):
        raise ValueError("专属进货折扣分子分母必须成对提供")
    if wholesale_numer is not None and (
        int(wholesale_numer) <= 0
        or int(wholesale_denom) <= 0
        or int(wholesale_numer) > int(wholesale_denom)
        or int(wholesale_numer) * 10 < int(wholesale_denom) * 3
    ):
        raise ValueError("专属进货折扣必须在 30%-100% 之间")
    wholesale_requested = (
        wholesale_numer is not None
        or wholesale_denom is not None
        or bool(wholesale_clear)
    )
    if wholesale_requested and not expected_catalog_version:
        raise ValueError("修改专属进货折扣必须携带 expected_catalog_version")

    from config.pricing_config import (
        get_agent_purchase_catalog_version,
        invalidate_pricing_config_cache,
        merge_pricing_config,
    )
    from db.connection import get_db
    from services.agent_inventory_pricing import advance_catalog_version_cursor, insert_pricing_audit
    from services.config_epoch import (
        accept_committed_epoch,
        read_config_epoch_strict,
    )

    committed_epoch: Optional[int] = None
    current_version = ""
    new_version = ""
    before: Dict[str, Any] = {}
    after: Dict[str, Any] = {}
    version_changed = False
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT pg_advisory_xact_lock(920713, 1)")
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (_RETAIL_MARKUP_LOCK_NAMESPACE, int(agent_user_id)),
            )
            epoch_before = read_config_epoch_strict(cur)
            cur.execute("SELECT value FROM system_settings WHERE key='pricing_config' FOR UPDATE")
            config_row = cur.fetchone()
            config_raw = config_row.get("value") if isinstance(config_row, dict) else (config_row[0] if config_row else {})
            if isinstance(config_raw, str):
                import json
                config_raw = json.loads(config_raw or "{}")
            effective_config = merge_pricing_config(
                config_raw if isinstance(config_raw, dict) else {}, include_env=False
            )
            current_version = get_agent_purchase_catalog_version(effective_config)
            if wholesale_requested and expected_catalog_version != current_version:
                raise AgentPricingOverrideVersionConflict(current_version)

            cur.execute(
                "SELECT quote_markup_override, sku_markup_override, wholesale_numer, "
                "wholesale_denom, note FROM agent_pricing_overrides "
                "WHERE agent_user_id=%s FOR UPDATE",
                (int(agent_user_id),),
            )
            before = _override_dict(cur.fetchone())

            if clear_set:
                set_clause = ", ".join(f"{field}=NULL" for field in sorted(clear_set))
                cur.execute(
                    f"UPDATE agent_pricing_overrides SET {set_clause}, updated_by=%s, "
                    "updated_at=CURRENT_TIMESTAMP WHERE agent_user_id=%s",
                    (int(admin_user_id), int(agent_user_id)),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO agent_pricing_overrides
                        (agent_user_id, quote_markup_override, sku_markup_override,
                         wholesale_numer, wholesale_denom, note, updated_by, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (agent_user_id) DO UPDATE SET
                        quote_markup_override = COALESCE(EXCLUDED.quote_markup_override, agent_pricing_overrides.quote_markup_override),
                        sku_markup_override = COALESCE(EXCLUDED.sku_markup_override, agent_pricing_overrides.sku_markup_override),
                        wholesale_numer = COALESCE(EXCLUDED.wholesale_numer, agent_pricing_overrides.wholesale_numer),
                        wholesale_denom = COALESCE(EXCLUDED.wholesale_denom, agent_pricing_overrides.wholesale_denom),
                        note = COALESCE(EXCLUDED.note, agent_pricing_overrides.note),
                        updated_by = EXCLUDED.updated_by,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        int(agent_user_id), quote_markup_override, sku_markup_override,
                        wholesale_numer, wholesale_denom, note, int(admin_user_id),
                    ),
                )

            cur.execute(
                "SELECT quote_markup_override, sku_markup_override, wholesale_numer, "
                "wholesale_denom, note FROM agent_pricing_overrides WHERE agent_user_id=%s",
                (int(agent_user_id),),
            )
            after = _override_dict(cur.fetchone())
            wholesale_changed = (
                before.get("wholesale_numer"), before.get("wholesale_denom")
            ) != (
                after.get("wholesale_numer"), after.get("wholesale_denom")
            )
            if wholesale_changed:
                new_version, committed_epoch = advance_catalog_version_cursor(cur)
                version_changed = True
            else:
                new_version = current_version
                committed_epoch = epoch_before

            insert_pricing_audit(
                cur,
                admin_user_id=int(admin_user_id),
                admin_username=admin_username,
                request_id=request_id,
                module=audit_module,
                summary=f"更新服务商 {int(agent_user_id)} 专属定价规则",
                before_config=before,
                after_config=after,
                previous_catalog_version=current_version,
                catalog_version=new_version,
                ip_address=ip_address,
                entity_type="agent_pricing_override",
                entity_id=int(agent_user_id),
            )
            if read_config_epoch_strict(cur) != int(committed_epoch):
                raise RuntimeError("配置纪元回读不一致")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    if version_changed:
        accept_committed_epoch(int(committed_epoch))
        invalidate_pricing_config_cache()
    return {
        "before": before,
        "after": after,
        "previous_catalog_version": current_version,
        "catalog_version": new_version,
        "request_id": request_id,
    }


def upsert_agent_pricing_override(
    agent_user_id: int,
    *,
    quote_markup_override: Optional[float] = None,
    sku_markup_override: Optional[float] = None,
    wholesale_numer: Optional[int] = None,
    wholesale_denom: Optional[int] = None,
    note: Optional[str] = None,
    admin_user_id: Optional[int] = None,
) -> None:
    """admin UPSERT(COALESCE 只覆盖显式传入的非 None 字段 · 传 None 保持原值不动)。

    注意:本函数语义是"设值";若要清空某 override 回落全局,用 clear_agent_pricing_override。
    """
    if wholesale_numer is not None or wholesale_denom is not None:
        raise RuntimeError("专属进货折扣必须使用 write_agent_pricing_override_atomic")
    if not ensure_pricing_overrides_table():
        raise RuntimeError("agent_pricing_overrides schema 未就绪")
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(920713, 1)")
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (_RETAIL_MARKUP_LOCK_NAMESPACE, int(agent_user_id)),
        )
        cur.execute("""
            INSERT INTO agent_pricing_overrides
                (agent_user_id, quote_markup_override, sku_markup_override,
                 wholesale_numer, wholesale_denom, note, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (agent_user_id) DO UPDATE SET
                quote_markup_override = COALESCE(EXCLUDED.quote_markup_override, agent_pricing_overrides.quote_markup_override),
                sku_markup_override   = COALESCE(EXCLUDED.sku_markup_override, agent_pricing_overrides.sku_markup_override),
                wholesale_numer       = COALESCE(EXCLUDED.wholesale_numer, agent_pricing_overrides.wholesale_numer),
                wholesale_denom       = COALESCE(EXCLUDED.wholesale_denom, agent_pricing_overrides.wholesale_denom),
                note                  = COALESCE(EXCLUDED.note, agent_pricing_overrides.note),
                updated_by            = EXCLUDED.updated_by,
                updated_at            = CURRENT_TIMESTAMP
        """, (agent_user_id, quote_markup_override, sku_markup_override,
              wholesale_numer, wholesale_denom, note, admin_user_id))
        if wholesale_numer is not None or wholesale_denom is not None:
            from services.agent_inventory_pricing import advance_catalog_version_cursor
            advance_catalog_version_cursor(cur)
        conn.commit()


def clear_agent_pricing_override(agent_user_id: int, fields: Optional[list] = None) -> None:
    """清空指定 override 字段(回落全局)。fields=None 清全部覆盖字段;否则只清列出的。"""
    if not ensure_pricing_overrides_table():
        raise RuntimeError("agent_pricing_overrides schema 未就绪")
    allowed = {"quote_markup_override", "sku_markup_override", "wholesale_numer", "wholesale_denom"}
    cols = allowed if not fields else {f for f in fields if f in allowed}
    if {"wholesale_numer", "wholesale_denom"}.intersection(cols):
        raise RuntimeError("清空专属进货折扣必须使用 write_agent_pricing_override_atomic")
    if not cols:
        return
    set_clause = ", ".join(f"{c} = NULL" for c in cols)
    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(920713, 1)")
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (_RETAIL_MARKUP_LOCK_NAMESPACE, int(agent_user_id)),
        )
        cur.execute(
            f"UPDATE agent_pricing_overrides SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE agent_user_id = %s",
            (agent_user_id,),
        )
        if {"wholesale_numer", "wholesale_denom"}.intersection(cols):
            from services.agent_inventory_pricing import advance_catalog_version_cursor
            advance_catalog_version_cursor(cur)
        conn.commit()
