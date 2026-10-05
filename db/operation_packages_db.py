"""报价经营包持久化(GEO 域)· per-服务商经营包 + 客户公开分享 token(2026-06-17)。

设计口径: docs/AI-CONTEXT/PRICING_OPERATION_PACKAGES_PLAN_2026-06-16.md
铁律(本模块严格遵守):
  - 经营包 = 算价 / 展示 / 建议售价 模板;**启用不扣费**,真实扣费仍走现有算力(完成才扣)。
  - 每个服务商账号拥有自己的经营包配置;默认模板按账号 seed-copy,不跨账号共享可变数据。
  - 只读默认模板(DEFAULT_PACKAGES)单点权威;用户启用即复制为本人行,各改各的。
  - 客户公开分享 token 绑定 owner;测试账号(is_test)不外露给真实客户公开链接。
  - 红线:仅新表 · 全 IF NOT EXISTS · 无 DROP/RENAME/TRUNCATE · owner_user_id 隔离每条 CRUD。

数据落点:
  - operation_packages              : per-服务商经营包行(模板快照 + 服务商对客售价 + 启用/排序/软删)
  - operation_package_share_tokens  : per-owner 客户公开分享 token(一个 active · is_test 标记)
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

logger = logging.getLogger("GEO-OperationPackages")

# 从只读默认模板 seed(tools 层纯数据 · 无 db 依赖 · 不构成 import 环)
# 列 = DEFAULT_PACKAGES 字段的快照(self-contained 行 · 模板演进不影响已 seed 账号)
_SEED_COLUMNS = (
    "template_key", "name", "scope", "search_item_min", "search_item_max",
    "fit_scene", "customer_copy",
    "base_cost_min", "base_cost_max",
    "base_suggested_price_min", "base_suggested_price_max",
    "base_margin_min", "base_margin_max",
    "sub_cost_min", "sub_cost_max",
    "sub_suggested_price_min", "sub_suggested_price_max",
    "sub_margin_min", "sub_margin_max",
)

# 服务商可编辑的字段(白名单 · 防止越权写内部成本/系数列)
EDITABLE_FIELDS = (
    "name", "scope", "fit_scene", "customer_copy",
    "search_item_min", "search_item_max",
    "customer_price_min", "customer_price_max",
    "enabled", "sort_order",
)
_INT_FIELDS = (
    "search_item_min", "search_item_max", "customer_price_min", "customer_price_max",
    "sort_order",
)


def _conn():
    """惰性取连接(避免 import 周期)。"""
    from db.connection import get_connection
    return get_connection()


def _get_db():
    from db.connection import get_db
    return get_db()


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column),
    )
    return cursor.fetchone() is not None


def _safe_add_column(cursor, table: str, column: str, col_type: str):
    if _column_exists(cursor, table, column):
        return
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except Exception:
        pass  # 并发 worker 已加


# ============================================================
# DDL · 建表(幂等 · fail-soft)
# ============================================================
def init_operation_packages_table():
    """建经营包 + 分享 token 两张表(全 IF NOT EXISTS · 可重复跑 · 失败只 warn)。"""
    try:
        conn = _conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS operation_packages (
                    id SERIAL PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    template_key VARCHAR(40),
                    name TEXT NOT NULL,
                    scope TEXT,
                    search_item_min INTEGER,
                    search_item_max INTEGER,
                    fit_scene TEXT,
                    customer_copy TEXT,
                    base_cost_min INTEGER,
                    base_cost_max INTEGER,
                    base_suggested_price_min INTEGER,
                    base_suggested_price_max INTEGER,
                    base_margin_min INTEGER,
                    base_margin_max INTEGER,
                    sub_cost_min INTEGER,
                    sub_cost_max INTEGER,
                    sub_suggested_price_min INTEGER,
                    sub_suggested_price_max INTEGER,
                    sub_margin_min INTEGER,
                    sub_margin_max INTEGER,
                    customer_price_min INTEGER,
                    customer_price_max INTEGER,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # 列回填(老表升级安全)
            for col, typ in [
                ("template_key", "VARCHAR(40)"),
                ("customer_price_min", "INTEGER"),
                ("customer_price_max", "INTEGER"),
                ("enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "VARCHAR(20) NOT NULL DEFAULT 'active'"),
            ]:
                _safe_add_column(cur, "operation_packages", col, typ)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_oppkg_owner_active "
                "ON operation_packages(owner_user_id) WHERE status='active'"
            )
            # 部分唯一:同一 owner 同一模板只 seed 一行(active)· 防并发重复 seed
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_oppkg_owner_template "
                "ON operation_packages(owner_user_id, template_key) "
                "WHERE template_key IS NOT NULL AND status='active'"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS operation_package_share_tokens (
                    id SERIAL PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    token VARCHAR(64) UNIQUE NOT NULL,
                    is_active SMALLINT NOT NULL DEFAULT 1,
                    is_test SMALLINT NOT NULL DEFAULT 0,
                    expires_at TIMESTAMP,
                    last_access_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_oppkg_token_owner_active "
                "ON operation_package_share_tokens(owner_user_id) WHERE is_active=1"
            )
            logger.info("✅ operation_packages 表初始化完成")
        finally:
            conn.close()
    except Exception as e:
        logger.warning("operation_packages 表初始化跳过: %s: %s", type(e).__name__, e)


# ============================================================
# Seed-copy · 首访按账号复制默认模板(幂等 · 并发安全)
# ============================================================
def ensure_seeded(owner_user_id: int) -> None:
    """若该 owner 无任何 active 行,则按 DEFAULT_PACKAGES 复制一套(ON CONFLICT DO NOTHING)。
    幂等:已 seed / 已有自定义行都不重复插。部分唯一索引兜并发。"""
    if not owner_user_id:
        return
    from tools.operation_packages import DEFAULT_PACKAGES
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM operation_packages WHERE owner_user_id=%s AND status='active' LIMIT 1",
                (owner_user_id,),
            )
            if cur.fetchone():
                return  # 已有行(seed 过或自定义过)→ 不动
            cols = ", ".join(("owner_user_id", "sort_order") + _SEED_COLUMNS)
            placeholders = ", ".join(["%s"] * (2 + len(_SEED_COLUMNS)))
            for idx, pkg in enumerate(DEFAULT_PACKAGES):
                values = [owner_user_id, idx] + [pkg.get(c if c != "template_key" else "key")
                                                 for c in _SEED_COLUMNS]
                cur.execute(
                    f"INSERT INTO operation_packages ({cols}) VALUES ({placeholders}) "
                    f"ON CONFLICT DO NOTHING",
                    values,
                )
    except Exception as e:
        logger.warning("ensure_seeded(owner=%s) 失败: %s", owner_user_id, e)


# ============================================================
# 读
# ============================================================
def list_packages(owner_user_id: int, enabled_only: bool = False) -> list[dict]:
    """列出 owner 的 active 经营包(按 sort_order, id)。enabled_only=True 只返已启用。"""
    if not owner_user_id:
        return []
    conn = _conn()
    try:
        cur = conn.cursor()
        sql = ("SELECT * FROM operation_packages "
               "WHERE owner_user_id=%s AND status='active'")
        if enabled_only:
            sql += " AND enabled=TRUE"
        sql += " ORDER BY sort_order ASC, id ASC"
        cur.execute(sql, (owner_user_id,))
        return [dict(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()


def get_package(pkg_id: int) -> Optional[dict]:
    """取单个 active 经营包(用于 API 层鉴权 owner 判定)。不存在 → None。"""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM operation_packages WHERE id=%s AND status='active'",
            (pkg_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ============================================================
# 写(API 层先做 owner/agent 鉴权 · 此处再以 owner_user_id 兜底 WHERE)
# ============================================================
def update_package(pkg_id: int, owner_user_id: int, fields: dict) -> Optional[dict]:
    """按白名单字段更新自己的经营包。返回更新后的行;无匹配行(不存在/非己/已删)→ None。"""
    sets = {}
    for k, v in (fields or {}).items():
        if k not in EDITABLE_FIELDS:
            continue
        if k in _INT_FIELDS and v is not None:
            try:
                v = int(round(float(v)))
            except (TypeError, ValueError):
                continue
        if k == "enabled" and v is not None:
            v = bool(v)
        sets[k] = v
    if not sets:
        return get_package(pkg_id)
    assignments = ", ".join(f"{k}=%s" for k in sets) + ", updated_at=CURRENT_TIMESTAMP"
    params = list(sets.values()) + [pkg_id, owner_user_id]
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE operation_packages SET {assignments} "
            f"WHERE id=%s AND owner_user_id=%s AND status='active' RETURNING *",
            params,
        )
        row = cur.fetchone()
        return dict(row) if row else None


def copy_package(pkg_id: int, owner_user_id: int) -> Optional[dict]:
    """复制自己的一个经营包为自定义新行(template_key=NULL · 名称加「副本」· 追加到末尾)。"""
    src = get_package(pkg_id)
    if not src or src.get("owner_user_id") != owner_user_id:
        return None
    copy_cols = (("name",) + tuple(c for c in _SEED_COLUMNS if c not in ("template_key", "name"))
                 + ("customer_price_min", "customer_price_max"))
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(MAX(sort_order), -1)+1 AS next_order FROM operation_packages "
            "WHERE owner_user_id=%s AND status='active'",
            (owner_user_id,),
        )
        next_order = (cur.fetchone() or {}).get("next_order") or 0
        col_list = ", ".join(("owner_user_id", "template_key", "sort_order") + copy_cols)
        placeholders = ", ".join(["%s"] * (3 + len(copy_cols)))
        values = [owner_user_id, None, next_order]
        for c in copy_cols:
            if c == "name":
                values.append(f"{src.get('name') or '经营包'}(副本)")
            else:
                values.append(src.get(c))
        cur.execute(
            f"INSERT INTO operation_packages ({col_list}) VALUES ({placeholders}) RETURNING *",
            values,
        )
        row = cur.fetchone()
        return dict(row) if row else None


def soft_delete_package(pkg_id: int, owner_user_id: int) -> bool:
    """软删自己的经营包(status='deleted')。返回是否删到行。"""
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE operation_packages SET status='deleted', updated_at=CURRENT_TIMESTAMP "
            "WHERE id=%s AND owner_user_id=%s AND status='active'",
            (pkg_id, owner_user_id),
        )
        return cur.rowcount > 0


def reorder_packages(owner_user_id: int, ordered_ids: list[int]) -> int:
    """按给定 id 顺序设 sort_order(只动自己的 active 行)。返回更新行数。"""
    if not ordered_ids:
        return 0
    updated = 0
    with _get_db() as conn:
        cur = conn.cursor()
        for idx, pid in enumerate(ordered_ids):
            try:
                pid_i = int(pid)
            except (TypeError, ValueError):
                continue
            cur.execute(
                "UPDATE operation_packages SET sort_order=%s, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=%s AND owner_user_id=%s AND status='active'",
                (idx, pid_i, owner_user_id),
            )
            updated += cur.rowcount
    return updated


# ============================================================
# 客户公开分享 token(per-owner 一个 active)
# ============================================================
def get_or_create_share_token(owner_user_id: int, is_test: bool = False) -> Optional[dict]:
    """取或建 owner 的 active 分享 token(幂等:已有 active 直接返回)。"""
    if not owner_user_id:
        return None
    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM operation_package_share_tokens "
            "WHERE owner_user_id=%s AND is_active=1 ORDER BY id DESC LIMIT 1",
            (owner_user_id,),
        )
        row = cur.fetchone()
        if row:
            # 同步 is_test(账号状态可能变化)
            if int(row.get("is_test") or 0) != (1 if is_test else 0):
                cur.execute(
                    "UPDATE operation_package_share_tokens SET is_test=%s WHERE id=%s",
                    (1 if is_test else 0, row["id"]),
                )
                row = dict(row)
                row["is_test"] = 1 if is_test else 0
            return dict(row)
        token = secrets.token_urlsafe(24)
        cur.execute(
            "INSERT INTO operation_package_share_tokens (owner_user_id, token, is_active, is_test) "
            "VALUES (%s, %s, 1, %s) ON CONFLICT DO NOTHING "
            "RETURNING *",
            (owner_user_id, token, 1 if is_test else 0),
        )
        ins = cur.fetchone()
        if ins:
            return dict(ins)
        # 并发竞争:别的请求已建 → 重查
        cur.execute(
            "SELECT * FROM operation_package_share_tokens "
            "WHERE owner_user_id=%s AND is_active=1 ORDER BY id DESC LIMIT 1",
            (owner_user_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def resolve_share_token(token: str) -> Optional[dict]:
    """解析客户公开 token → {owner_user_id, is_test, expired}。无效/停用 → None(API 层 404)。"""
    if not token:
        return None
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT owner_user_id, is_test, expires_at FROM operation_package_share_tokens "
            "WHERE token=%s AND is_active=1",
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return None
        expired = False
        exp = row.get("expires_at")
        if exp is not None:
            try:
                cur.execute("SELECT (%s < CURRENT_TIMESTAMP) AS ex", (exp,))
                expired = bool((cur.fetchone() or {}).get("ex"))
            except Exception:
                expired = False
        return {
            "owner_user_id": row.get("owner_user_id"),
            "is_test": bool(row.get("is_test")),
            "expired": expired,
        }
    finally:
        conn.close()
