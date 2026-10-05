"""白标 backoffice 作用域 readiness 合同 · 单一权威调用方（统一 R3 §七）
====================================================================
合同口径只存在一份：数据库内函数
``public.whitelabel_backoffice_schema_blockers(boolean)``
（由 ``scripts/migration_whitelabel_backoffice_scope_2026_07_22.sql`` §6 创建，
rollback 同批移除）。覆盖：whitelabel_settings 4 新列 type/null/default、
whitelabel_audit 完整 11 列 + PK + 永久表属性、idx_whitelabel_audit_user_created
归属 + 列序 + 定义 + indisvalid/indisready/indislive 三状态、append-only
trigger/function 定义归属、ID 132 customer-only 不变式；函数本体
``SET search_path = pg_catalog, public`` 且全限定 public.*，免疫诱饵
search_path / pg_temp 影子表；半成品索引等任何 drift → blocker（fail-closed）。

四处调用点跑的是**同一个函数**，本模块只是薄调用方（不含任何 schema 期望值，
禁写第二份口径）：

1. migration 自验块（migration SQL §7，同事务 fail-closed 中止）；
2. ``scripts/prestart.py``（fleet 起前单飞预检，``require_settings=False``：
   fresh 库基表由运行时 init_referral_tables 兜底，缺失不算 blocker；
   表一旦存在即全量核验）；
3. runtime 启动自检（``api/referral_api.py::_migrate_whitelabel_settings_v36``
   收敛后调用，``require_settings=True``）；
4. 统一 release readiness（``scripts/verify_unified_release_readiness.py``，
   ``require_settings=True``）。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONTRACT_FUNCTION_SIGNATURE = "public.whitelabel_backoffice_schema_blockers(boolean)"
CONTRACT_FUNCTION_CALL = "public.whitelabel_backoffice_schema_blockers"
NOT_READY_PREFIX = "WHITELABEL_BACKOFFICE_SCHEMA_NOT_READY"


def _value(row: Any, key: str, index: int = 0) -> Any:
    """同时兼容 psycopg2 tuple 行与 RealDictCursor 行。"""
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index] if row else None


def whitelabel_backoffice_schema_blockers(cur, require_settings: bool = True) -> list[str]:
    """返回合同 blocker 列表（空 = 就绪）。合同函数缺失本身即 blocker（fail-closed）。"""
    cur.execute("SELECT pg_catalog.to_regprocedure(%s) AS fn", (CONTRACT_FUNCTION_SIGNATURE,))
    if _value(cur.fetchone(), "fn") is None:
        return [
            "missing_contract_function:" + CONTRACT_FUNCTION_SIGNATURE
            + "（migration_whitelabel_backoffice_scope_2026_07_22.sql 未应用或被回滚）"
        ]
    cur.execute(
        f"SELECT blocker FROM {CONTRACT_FUNCTION_CALL}(%s) ORDER BY blocker",
        (require_settings,),
    )
    return [str(_value(row, "blocker")) for row in cur.fetchall()]


def assert_whitelabel_backoffice_schema_ready(cur, require_settings: bool = True) -> None:
    """fail-closed：任一 blocker → RuntimeError（稳定前缀 + 逐条 blocker）。"""
    blockers = whitelabel_backoffice_schema_blockers(cur, require_settings=require_settings)
    if blockers:
        raise RuntimeError(NOT_READY_PREFIX + "|" + "|".join(blockers))
