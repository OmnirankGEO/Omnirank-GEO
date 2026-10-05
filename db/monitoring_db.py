"""
客户词条监测数据库模块 v2.0 (brand_id 统一化)
功能：
- 客户词条管理 (confirmed_keywords + extra_keywords)
- 监测任务管理 (monitoring_tasks)
- 监测结果存储 (monitoring_results)
- 报告记录管理 (monitoring_reports)
- 监测配置管理 (monitoring_config)

✅ v2.0 统一化：所有函数优先使用 brand_id: int 作为业务主键。
   保留 client_id: str 参数做 fallback（兼容期）。
   写入时双写 brand_id + client_id。
"""

from db.schema_guard import add_column_if_missing
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

# ================================================================
# AI平台市占率权重（基于 QuestMobile 2026年2月 MAU 数据）
#
# [P0-2 · 2026-07-26 Owner 裁决] 诊断与监测统一五引擎，引擎清单唯一常量源
# = config/ai_engines.py。修复前诊断走 dashscope/deepseek/doubao/yuanbao、
# 监测走 dashscope/deepseek/kimi/doubao → 同一客户两处答案不同。
#
# 已售商品不可无痕重写（SSOT §16）：新售矩阵 = monitoring-unified5-v1；
# classic4 版本行与老词已持久化的 platforms 快照保持原样，按购买时口径继续跑。
# ================================================================
from config.ai_engines import (  # noqa: E402  (常量源必须在模块顶层可见)
    MONITORING_ENGINES,
    MONITORING_PLATFORMS_CLASSIC4_CSV,
    MONITORING_PLATFORMS_CSV,
    MONITORING_PRODUCT_VERSION_CLASSIC4,
    MONITORING_PRODUCT_VERSION_UNIFIED5,
    MONITORING_RUN_CELL_PLATFORMS,
)
# [服务期 SSOT 2026-08-06] 服务期(日历)与履约配额(达标天数)两个概念的唯一权威源。
#   本文件历来把 `service_days` 既当配额又当日历天用 —— 这条 import 是把二者拆开的抓手。
from services.service_period import (  # noqa: E402
    compliance_target_days,
    read_calendar_period,
)

DEFAULT_MONITORING_PRODUCT_VERSION = MONITORING_PRODUCT_VERSION_UNIFIED5
DEFAULT_MONITORING_PLATFORMS = MONITORING_PLATFORMS_CSV

# 历史商品版本（老词外键 + 快照解析仍要用；不得删行）
LEGACY_MONITORING_PRODUCT_VERSION = MONITORING_PRODUCT_VERSION_CLASSIC4
LEGACY_MONITORING_PLATFORMS = MONITORING_PLATFORMS_CLASSIC4_CSV

DEFAULT_PLATFORM_WEIGHTS = {
    platform: round(1.0 / len(MONITORING_ENGINES), 4) for platform in MONITORING_ENGINES
}
PLATFORM_WEIGHTS = dict(DEFAULT_PLATFORM_WEIGHTS)

# 权重/展示的稳定顺序（豆包 MAU 最高，保持原有「豆包优先」的运营口径）
PLATFORM_CANONICAL_ORDER = ("doubao", "dashscope", "deepseek", "kimi", "yuanbao")
MONITORING_UNIFIED5_ORDER = tuple(MONITORING_ENGINES)
# 历史四路顺序（classic4 老词展示/回归用，不再是新售默认）
# ⚠️ [2026-08-04] 这是 **classic4 这个商品版本**的顺序,不是「账本能存哪些平台」。
# create_monitoring_run_cells 曾经拿它当后者用 —— 数值恰好相同掩盖了语义错位,
# 直到 unified5 上线才暴露。落 cell / 裁 eligible 一律用
# config.ai_engines.MONITORING_RUN_CELL_PLATFORMS。
MONITORING_CLASSIC4_ORDER = ("dashscope", "deepseek", "kimi", "doubao")


def assert_monitoring_product_matrix_ready(cursor) -> None:
    """Read-only, definition-level guard for purchased monitoring rights."""
    def row_value(row, key, index=0):
        if isinstance(row, dict):
            return row.get(key)
        return row[index] if row else None

    cursor.execute(
        """
        SELECT c.oid, c.relkind, c.relpersistence
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relname = 'monitoring_product_platform_matrices'
        """
    )
    table_row = cursor.fetchone()
    if (
        not table_row
        or row_value(table_row, "relkind", 1) != "r"
        or row_value(table_row, "relpersistence", 2) != "p"
    ):
        raise RuntimeError("public monitoring product matrix is not an ordinary permanent table")
    matrix_oid = row_value(table_row, "oid")

    cursor.execute(
        """
        SELECT a.attname,
               pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attnotnull,
               pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS column_default
          FROM pg_catalog.pg_attribute a
          LEFT JOIN pg_catalog.pg_attrdef d
            ON d.adrelid = a.attrelid
           AND d.adnum = a.attnum
         WHERE a.attrelid = %s
           AND a.attnum > 0
           AND NOT a.attisdropped
         ORDER BY a.attnum
        """,
        (matrix_oid,),
    )
    actual_columns = {
        row_value(column_row, "attname"): (
            row_value(column_row, "data_type", 1),
            row_value(column_row, "attnotnull", 2),
            row_value(column_row, "column_default", 3),
        )
        for column_row in cursor.fetchall()
    }
    expected_columns = {
        "version": ("text", True, None),
        "platforms": ("text", True, None),
        "created_at": ("timestamp with time zone", True, "now()"),
    }
    if actual_columns != expected_columns:
        raise RuntimeError(f"monitoring product matrix columns drifted: {actual_columns!r}")

    cursor.execute(
        """
        SELECT c.conname, c.convalidated,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(c.conkey) WITH ORDINALITY AS key(attnum, ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid = c.conrelid
                      AND a.attnum = key.attnum
                    ORDER BY key.ord
               ) AS columns
          FROM pg_catalog.pg_constraint c
         WHERE c.conrelid = %s
           AND c.contype = 'p'
        """,
        (matrix_oid,),
    )
    primary_keys = cursor.fetchall()
    if (
        len(primary_keys) != 1
        or row_value(primary_keys[0], "convalidated", 1) is not True
        or list(row_value(primary_keys[0], "columns", 2) or []) != ["version"]
    ):
        raise RuntimeError(f"monitoring product matrix PK drifted: {primary_keys!r}")

    cursor.execute(
        """
        SELECT platforms
          FROM public.monitoring_product_platform_matrices
         WHERE version = %s
        """,
        (DEFAULT_MONITORING_PRODUCT_VERSION,),
    )
    row = cursor.fetchone()
    actual_platforms = row.get("platforms") if isinstance(row, dict) else (row[0] if row else None)
    if actual_platforms != DEFAULT_MONITORING_PLATFORMS:
        raise RuntimeError(
            "monitoring product matrix missing or drifted: "
            f"{DEFAULT_MONITORING_PRODUCT_VERSION}={actual_platforms!r}"
        )

    expected_defaults = {
        ("client_keywords", "platforms"): f"'{DEFAULT_MONITORING_PLATFORMS}'::text",
        ("extra_keywords", "platforms"): f"'{DEFAULT_MONITORING_PLATFORMS}'::text",
        ("monitoring_config", "default_platforms"): f"'{DEFAULT_MONITORING_PLATFORMS}'::text",
        ("monitoring_tasks", "platform_count"): "0",
    }
    for (table_name, column_name), expected_default in expected_defaults.items():
        cursor.execute(
            """
            SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS column_default
              FROM pg_catalog.pg_attrdef d
              JOIN pg_catalog.pg_attribute a
                ON a.attrelid = d.adrelid
               AND a.attnum = d.adnum
             WHERE d.adrelid = pg_catalog.to_regclass(%s)
               AND a.attname = %s
            """,
            (f"public.{table_name}", column_name),
        )
        default_row = cursor.fetchone()
        actual_default = (
            default_row.get("column_default")
            if isinstance(default_row, dict)
            else (default_row[0] if default_row else None)
        )
        if actual_default != expected_default:
            raise RuntimeError(
                "monitoring default drift: "
                f"{table_name}.{column_name}={actual_default!r}"
            )

    cursor.execute(
        """
        SELECT pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attnotnull,
               pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS column_default
          FROM pg_catalog.pg_attribute a
          LEFT JOIN pg_catalog.pg_attrdef d
            ON d.adrelid = a.attrelid
           AND d.adnum = a.attnum
         WHERE a.attrelid = 'public.confirmed_keywords'::pg_catalog.regclass
           AND a.attname = 'monitoring_product_version'
           AND NOT a.attisdropped
        """
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeError("confirmed_keywords.monitoring_product_version is missing")
    data_type = row_value(row, "data_type")
    not_null = row_value(row, "attnotnull", 1)
    column_default = row_value(row, "column_default", 2)
    if (
        data_type != "text"
        or not_null is not True
        or column_default != f"'{DEFAULT_MONITORING_PRODUCT_VERSION}'::text"
    ):
        raise RuntimeError(
            "confirmed monitoring product column is weak: "
            f"type={data_type!r}, not_null={not_null}, default={column_default!r}"
        )

    cursor.execute(
        """
        SELECT c.convalidated, c.condeferrable, c.condeferred,
               c.confupdtype, c.confdeltype, c.confmatchtype, c.confrelid,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(c.conkey) WITH ORDINALITY AS key(attnum, ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid = c.conrelid
                      AND a.attnum = key.attnum
                    ORDER BY key.ord
               ) AS source_columns,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(c.confkey) WITH ORDINALITY AS key(attnum, ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid = c.confrelid
                      AND a.attnum = key.attnum
                    ORDER BY key.ord
               ) AS target_columns
          FROM pg_catalog.pg_constraint c
         WHERE c.conrelid = 'public.confirmed_keywords'::pg_catalog.regclass
           AND c.conname = 'fk_confirmed_keywords_monitoring_product_version'
           AND c.contype = 'f'
        """
    )
    row = cursor.fetchone()
    fk_valid = bool(row) and (
        row_value(row, "convalidated") is True
        and row_value(row, "condeferrable", 1) is False
        and row_value(row, "condeferred", 2) is False
        and row_value(row, "confupdtype", 3) == "a"
        and row_value(row, "confdeltype", 4) == "a"
        and row_value(row, "confmatchtype", 5) == "s"
        and row_value(row, "confrelid", 6) == matrix_oid
        and list(row_value(row, "source_columns", 7) or [])
        == ["monitoring_product_version"]
        and list(row_value(row, "target_columns", 8) or []) == ["version"]
    )
    if not fk_valid:
        raise RuntimeError(f"monitoring product FK drift: {row!r}")

    expected_function_sources = {
        "reject_monitoring_product_matrix_mutation": (
            "BEGIN RAISE EXCEPTION 'monitoring product matrices are append-only'; END;"
        ),
        "reject_confirmed_monitoring_product_rebind": (
            "BEGIN IF OLD.monitoring_product_version IS DISTINCT FROM "
            "NEW.monitoring_product_version THEN RAISE EXCEPTION "
            "'confirmed monitoring product version is immutable'; END IF; RETURN NEW; END;"
        ),
    }
    function_oids = {}
    for function_name, expected_source in expected_function_sources.items():
        cursor.execute(
            """
            SELECT p.oid, l.lanname,
                   pg_catalog.pg_get_function_result(p.oid) AS result_type,
                   p.prokind, p.provolatile, p.prosecdef, p.proleakproof, p.proconfig,
                   btrim(regexp_replace(p.prosrc, '\\s+', ' ', 'g')) AS normalized_source
              FROM pg_catalog.pg_proc p
              JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
              JOIN pg_catalog.pg_language l ON l.oid = p.prolang
             WHERE n.nspname = 'public'
               AND p.proname = %s
               AND pg_catalog.pg_get_function_identity_arguments(p.oid) = ''
            """,
            (function_name,),
        )
        function_rows = cursor.fetchall()
        if len(function_rows) != 1:
            raise RuntimeError(f"public monitoring function identity drift: {function_name}")
        function_row = function_rows[0]
        if not (
            row_value(function_row, "lanname", 1) == "plpgsql"
            and row_value(function_row, "result_type", 2) == "trigger"
            and row_value(function_row, "prokind", 3) == "f"
            and row_value(function_row, "provolatile", 4) == "v"
            and row_value(function_row, "prosecdef", 5) is False
            and row_value(function_row, "proleakproof", 6) is False
            and row_value(function_row, "proconfig", 7) is None
            and row_value(function_row, "normalized_source", 8) == expected_source
        ):
            raise RuntimeError(f"monitoring product function drift: {function_name}")
        function_oids[function_name] = row_value(function_row, "oid")

    cursor.execute("SELECT 'public.confirmed_keywords'::pg_catalog.regclass::oid")
    confirmed_oid = row_value(cursor.fetchone(), "oid")
    expected_triggers = {
        "trg_monitoring_product_matrix_append_only": (
            matrix_oid,
            function_oids["reject_monitoring_product_matrix_mutation"],
            27,
            [],
        ),
        "trg_confirmed_monitoring_product_immutable": (
            confirmed_oid,
            function_oids["reject_confirmed_monitoring_product_rebind"],
            19,
            ["monitoring_product_version"],
        ),
    }
    cursor.execute(
        """
        SELECT t.tgname, t.tgenabled, t.tgrelid, t.tgfoid, t.tgtype,
               t.tgqual IS NULL AS has_no_when, t.tgnargs,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(t.tgattr::smallint[]) WITH ORDINALITY AS key(attnum, ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid = t.tgrelid
                      AND a.attnum = key.attnum
                    ORDER BY key.ord
               ) AS update_columns
          FROM pg_catalog.pg_trigger t
         WHERE NOT t.tgisinternal
           AND (
               (t.tgname = 'trg_monitoring_product_matrix_append_only'
                AND t.tgrelid = %s)
               OR
               (t.tgname = 'trg_confirmed_monitoring_product_immutable'
                AND t.tgrelid = %s)
           )
        """,
        (matrix_oid, confirmed_oid),
    )
    trigger_rows = {}
    for trigger_row in cursor.fetchall():
        if isinstance(trigger_row, dict):
            trigger_rows[trigger_row["tgname"]] = trigger_row
        else:
            trigger_rows[trigger_row[0]] = {
                "tgname": trigger_row[0],
                "tgenabled": trigger_row[1],
                "tgrelid": trigger_row[2],
                "tgfoid": trigger_row[3],
                "tgtype": trigger_row[4],
                "has_no_when": trigger_row[5],
                "tgnargs": trigger_row[6],
                "update_columns": trigger_row[7],
            }
    for trigger_name, expected in expected_triggers.items():
        trigger_row = trigger_rows.get(trigger_name)
        expected_relid, expected_foid, expected_type, expected_columns = expected
        if (
            not trigger_row
            or trigger_row["tgenabled"] != "O"
            or trigger_row["tgrelid"] != expected_relid
            or trigger_row["tgfoid"] != expected_foid
            or trigger_row["tgtype"] != expected_type
            or trigger_row["has_no_when"] is not True
            or trigger_row["tgnargs"] != 0
            or list(trigger_row["update_columns"] or []) != expected_columns
        ):
            raise RuntimeError(
                f"monitoring product trigger missing, disabled or drifted: {trigger_name}"
            )

    cursor.execute("SELECT current_setting('session_replication_role') AS role")
    role_row = cursor.fetchone()
    replication_role = (
        role_row.get("role") if isinstance(role_row, dict) else role_row[0]
    )
    if replication_role != "origin":
        raise RuntimeError(
            f"session_replication_role must be origin, got {replication_role!r}"
        )

    cursor.execute(
        """
        SELECT source_table, COUNT(*) AS ambiguous_count
          FROM (
                SELECT 'client_keywords'::text AS source_table, target.id
                  FROM public.client_keywords target
                 WHERE target.platforms = 'dashscope,deepseek,doubao,yuanbao'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM public.monitoring_platform_matrix_backup_20260720 backup
                        WHERE backup.source_table = 'client_keywords'
                          AND backup.source_id = target.id
                          AND backup.old_value = 'dashscope,deepseek,kimi,doubao'
                   )
                UNION ALL
                SELECT 'extra_keywords'::text, target.id
                  FROM public.extra_keywords target
                 WHERE target.platforms = 'dashscope,deepseek,doubao,yuanbao'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM public.monitoring_platform_matrix_backup_20260720 backup
                        WHERE backup.source_table = 'extra_keywords'
                          AND backup.source_id = target.id
                          AND backup.old_value = 'dashscope,deepseek,kimi,doubao'
                   )
                UNION ALL
                SELECT 'monitoring_config'::text, target.id
                  FROM public.monitoring_config target
                 WHERE target.default_platforms = 'dashscope,deepseek,doubao,yuanbao'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM public.monitoring_platform_matrix_backup_20260720 backup
                        WHERE backup.source_table = 'monitoring_config'
                          AND backup.source_id = target.id
                          AND backup.old_value = 'dashscope,deepseek,kimi,doubao'
                   )
          ) ambiguous
         GROUP BY source_table
         ORDER BY source_table
        """
    )
    ambiguous_rows = cursor.fetchall()
    if ambiguous_rows:
        details = ", ".join(
            f"{row.get('source_table') if isinstance(row, dict) else row[0]}="
            f"{row.get('ambiguous_count') if isinstance(row, dict) else row[1]}"
            for row in ambiguous_rows
        )
        raise RuntimeError(
            "ambiguous retired monitoring defaults require production review: " + details
        )


def assert_monitoring_identity_review_ready(cursor) -> None:
    """Fail closed unless the durable UNKNOWN review contract is exact."""

    def value(row, key, index=0):
        if isinstance(row, dict):
            return row.get(key)
        return row[index] if row else None

    def normalize_sql(definition):
        return re.sub(r"\s+", " ", str(definition or "")).strip()

    cursor.execute(
        """
        SELECT c.oid, c.relname, c.relkind, c.relpersistence
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relname = ANY(%s)
        """,
        ([
            "monitoring_results",
            "monitoring_identity_name_decisions",
            "monitoring_identity_decision_events",
        ],),
    )
    relations = {
        value(row, "relname", 1): {
            "oid": value(row, "oid", 0),
            "relkind": value(row, "relkind", 2),
            "persistence": value(row, "relpersistence", 3),
        }
        for row in cursor.fetchall()
    }
    if set(relations) != {
        "monitoring_results",
        "monitoring_identity_name_decisions",
        "monitoring_identity_decision_events",
    } or any(
        item["relkind"] != "r" or item["persistence"] != "p"
        for item in relations.values()
    ):
        raise RuntimeError("monitoring identity tables are missing or not permanent public tables")

    expected_columns = {
        "monitoring_results": {
            "identity_review_state": ("character varying(24)", True, "'not_required'::character varying", ""),
            "identity_brand_id": ("integer", False, None, ""),
            "identity_candidates": ("jsonb", True, "'[]'::jsonb", ""),
            "identity_evidence_snippet": ("text", False, None, ""),
            "identity_evidence_hash": ("character(64)", False, None, ""),
            "identity_decision_version": ("bigint", True, "0", ""),
            "identity_resolved_at": ("timestamp with time zone", False, None, ""),
            "identity_resolved_by": ("bigint", False, None, ""),
        },
        "monitoring_identity_name_decisions": {
            "brand_id": ("integer", True, None, ""),
            "normalized_name": ("text", True, None, ""),
            "display_name": ("text", True, None, ""),
            "decision": ("character varying(16)", True, None, ""),
            "decision_version": ("bigint", True, "1", ""),
            "evidence_hash": ("character(64)", True, None, ""),
            "decided_by": ("bigint", True, None, ""),
            "request_id": ("uuid", True, None, ""),
            "created_at": ("timestamp with time zone", True, "now()", ""),
            "updated_at": ("timestamp with time zone", True, "now()", ""),
        },
        "monitoring_identity_decision_events": {
            "event_id": ("bigint", True, None, "d"),
            # [2026-07-22 板块A D6] additive 泛化:result_id 放宽可空(诊断事件无监测行),
            # 新增 source_kind/source_result_id/tenant_id/ip/reason;详见
            # scripts/migration_diagnosis_identity_review_2026_07_22.sql
            "result_id": ("integer", False, None, ""),
            "brand_id": ("integer", True, None, ""),
            "action": ("character varying(16)", True, None, ""),
            "selected_name": ("text", True, None, ""),
            "normalized_name": ("text", True, None, ""),
            "evidence_hash": ("character(64)", True, None, ""),
            "result_version_before": ("bigint", True, None, ""),
            "result_version_after": ("bigint", True, None, ""),
            "actor_user_id": ("bigint", True, None, ""),
            "request_id": ("uuid", True, None, ""),
            "metadata": ("jsonb", True, "'{}'::jsonb", ""),
            "decided_at": ("timestamp with time zone", True, "now()", ""),
            "source_kind": ("text", True, "'monitoring'::text", ""),
            "source_result_id": ("bigint", False, None, ""),
            "tenant_id": ("bigint", False, None, ""),
            "ip": ("text", False, None, ""),
            "reason": ("text", False, None, ""),
        },
    }
    for table_name, expected in expected_columns.items():
        cursor.execute(
            """
            SELECT a.attname,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                   a.attnotnull,
                   pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS column_default,
                   a.attidentity
              FROM pg_catalog.pg_attribute a
              LEFT JOIN pg_catalog.pg_attrdef d
                ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = %s
               AND a.attnum > 0 AND NOT a.attisdropped
             ORDER BY a.attnum
            """,
            (relations[table_name]["oid"],),
        )
        actual = {
            value(row, "attname", 0): (
                value(row, "data_type", 1),
                value(row, "attnotnull", 2),
                value(row, "column_default", 3),
                value(row, "attidentity", 4),
            )
            for row in cursor.fetchall()
        }
        if table_name == "monitoring_results":
            actual = {name: actual.get(name) for name in expected}
        if actual != expected:
            raise RuntimeError(f"monitoring identity column contract drift: {table_name}")

    cursor.execute(
        """
        SELECT c.conrelid, c.conname, c.contype, c.convalidated,
               c.condeferrable, c.condeferred, c.confrelid,
               c.confupdtype, c.confdeltype, c.confmatchtype,
               pg_catalog.pg_get_constraintdef(c.oid, TRUE) AS definition,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(c.conkey) WITH ORDINALITY key(attnum, ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid = c.conrelid AND a.attnum = key.attnum
                    ORDER BY key.ord
               ) AS source_columns,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(c.confkey) WITH ORDINALITY key(attnum, ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid = c.confrelid AND a.attnum = key.attnum
                    ORDER BY key.ord
               ) AS target_columns
          FROM pg_catalog.pg_constraint c
         WHERE c.conrelid = ANY(%s)
        """,
        ([item["oid"] for item in relations.values()],),
    )
    constraints = {
        value(row, "conname", 1): {
            "relid": value(row, "conrelid", 0),
            "type": value(row, "contype", 2),
            "valid": value(row, "convalidated", 3),
            "deferrable": value(row, "condeferrable", 4),
            "deferred": value(row, "condeferred", 5),
            "target": value(row, "confrelid", 6),
            "update": value(row, "confupdtype", 7),
            "delete": value(row, "confdeltype", 8),
            "match": value(row, "confmatchtype", 9),
            "definition": normalize_sql(value(row, "definition", 10)),
            "source_columns": list(value(row, "source_columns", 11) or []),
            "target_columns": list(value(row, "target_columns", 12) or []),
        }
        for row in cursor.fetchall()
    }
    exact_keys = {
        "pk_monitoring_identity_name_decisions": ("p", ["brand_id", "normalized_name"]),
        "uq_monitoring_identity_name_decisions_request": ("u", ["request_id"]),
        "pk_monitoring_identity_decision_events": ("p", ["event_id"]),
        "uq_monitoring_identity_decision_events_request": ("u", ["request_id"]),
    }
    for name, (kind, columns) in exact_keys.items():
        contract = constraints.get(name)
        if not contract or not (
            contract["type"] == kind
            and contract["valid"] is True
            and contract["deferrable"] is False
            and contract["deferred"] is False
            and contract["source_columns"] == columns
        ):
            raise RuntimeError(f"monitoring identity key contract drift: {name}")

    exact_fks = {
        "fk_monitoring_results_identity_brand": (
            relations["monitoring_results"]["oid"], ["identity_brand_id"],
            "brands", ["id"],
        ),
        "fk_monitoring_identity_name_decisions_brand": (
            relations["monitoring_identity_name_decisions"]["oid"], ["brand_id"],
            "brands", ["id"],
        ),
        "fk_monitoring_identity_decision_events_result": (
            relations["monitoring_identity_decision_events"]["oid"], ["result_id"],
            "monitoring_results", ["id"],
        ),
        "fk_monitoring_identity_decision_events_brand": (
            relations["monitoring_identity_decision_events"]["oid"], ["brand_id"],
            "brands", ["id"],
        ),
    }
    cursor.execute("SELECT 'public.brands'::pg_catalog.regclass::oid AS oid")
    brands_oid = value(cursor.fetchone(), "oid")
    target_oids = {"brands": brands_oid, **{name: item["oid"] for name, item in relations.items()}}
    for name, (relid, source_columns, target_name, target_columns) in exact_fks.items():
        contract = constraints.get(name)
        if not contract or not (
            contract["relid"] == relid
            and contract["type"] == "f"
            and contract["valid"] is True
            and contract["deferrable"] is False
            and contract["deferred"] is False
            and contract["target"] == target_oids[target_name]
            and contract["update"] == "a"
            and contract["delete"] == "r"
            and contract["match"] == "s"
            and contract["source_columns"] == source_columns
            and contract["target_columns"] == target_columns
        ):
            raise RuntimeError(f"monitoring identity FK contract drift: {name}")

    exact_checks = {
        "chk_monitoring_results_identity_review_state":
            "CHECK (identity_review_state::text = ANY (ARRAY['not_required'::character varying, 'pending'::character varying, 'confirmed'::character varying, 'rejected'::character varying]::text[]))",
        "chk_monitoring_results_identity_candidates":
            "CHECK (jsonb_typeof(identity_candidates) = 'array'::text)",
        "chk_monitoring_results_identity_pending":
            "CHECK (identity_review_state::text <> 'pending'::text OR response_status::text = 'brand_identity_unresolved'::text AND mention_type = 'pending_identity'::text AND identity_brand_id IS NOT NULL AND identity_evidence_hash IS NOT NULL AND identity_decision_version = 0)",
        "chk_monitoring_results_identity_markers":
            "CHECK ((identity_review_state::text = 'pending'::text) = (response_status::text = 'brand_identity_unresolved'::text OR mention_type = 'pending_identity'::text))",
        "chk_monitoring_results_identity_version":
            "CHECK (identity_decision_version >= 0)",
        "chk_monitoring_identity_name_decision":
            "CHECK (decision::text = ANY (ARRAY['positive'::character varying, 'negative'::character varying]::text[]))",
        "chk_monitoring_identity_name_version":
            "CHECK (decision_version > 0)",
        "chk_monitoring_identity_event_action":
            "CHECK (action::text = ANY (ARRAY['yes'::character varying, 'no'::character varying, 'custom'::character varying]::text[]))",
        "chk_monitoring_identity_event_before":
            "CHECK (result_version_before >= 0)",
        "chk_monitoring_identity_event_after":
            "CHECK (result_version_after > result_version_before)",
        "chk_monitoring_identity_event_metadata":
            "CHECK (jsonb_typeof(metadata) = 'object'::text)",
        # [2026-07-22 板块A D6] 泛化 CHECK(migration_diagnosis_identity_review_2026_07_22.sql)
        "chk_monitoring_identity_event_source_kind":
            "CHECK (source_kind = ANY (ARRAY['monitoring'::text, 'diagnosis'::text]))",
        # PG16 canonical(16.14 实证):pg_get_constraintdef(oid, TRUE) 重脱水后
        # AND 组不保留书写括号;与 migration 尾部核验使用同一份定义。
        "chk_monitoring_identity_event_source":
            "CHECK (source_kind = 'monitoring'::text AND result_id IS NOT NULL OR source_kind = 'diagnosis'::text AND source_result_id IS NOT NULL)",
    }
    restored_check_equivalents = {
        "chk_monitoring_results_identity_review_state":
            "CHECK (identity_review_state::text = ANY (ARRAY['not_required'::character varying::text, 'pending'::character varying::text, 'confirmed'::character varying::text, 'rejected'::character varying::text]))",
        "chk_monitoring_identity_name_decision":
            "CHECK (decision::text = ANY (ARRAY['positive'::character varying::text, 'negative'::character varying::text]))",
        "chk_monitoring_identity_event_action":
            "CHECK (action::text = ANY (ARRAY['yes'::character varying::text, 'no'::character varying::text, 'custom'::character varying::text]))",
    }
    for name, expected_definition in exact_checks.items():
        contract = constraints.get(name)
        definition = contract["definition"] if contract else ""
        if not contract or not (
            contract["type"] == "c"
            and contract["valid"] is True
            and contract["deferrable"] is False
            and contract["deferred"] is False
            and definition in {
                expected_definition,
                restored_check_equivalents.get(name, expected_definition),
            }
        ):
            raise RuntimeError(f"monitoring identity CHECK contract drift: {name}")

    result_oid = relations["monitoring_results"]["oid"]
    cursor.execute(
        """
        SELECT i.indisunique, i.indisvalid, i.indisready,
               i.indnatts, i.indnkeyatts, am.amname,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(i.indkey) WITH ORDINALITY key(attnum, ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid = i.indrelid AND a.attnum = key.attnum
                    ORDER BY key.ord
               ) AS columns,
               i.indoption::smallint[] AS options,
               pg_catalog.pg_get_expr(i.indpred, i.indrelid) AS predicate
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class idx ON idx.oid = i.indexrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = idx.relnamespace
          JOIN pg_catalog.pg_am am ON am.oid = idx.relam
         WHERE n.nspname = 'public'
           AND idx.relname = 'idx_monitoring_results_identity_pending'
           AND i.indrelid = %s
        """,
        (result_oid,),
    )
    index_row = cursor.fetchone()
    if not index_row or not (
        value(index_row, "indisunique") is False
        and value(index_row, "indisvalid", 1) is True
        and value(index_row, "indisready", 2) is True
        and value(index_row, "indnatts", 3) == 3
        and value(index_row, "indnkeyatts", 4) == 3
        and value(index_row, "amname", 5) == "btree"
        and list(value(index_row, "columns", 6) or []) == ["identity_brand_id", "tested_at", "id"]
        and list(value(index_row, "options", 7) or []) == [0, 3, 3]
        and normalize_sql(value(index_row, "predicate", 8))
        == "(((identity_review_state)::text = 'pending'::text) AND ((response_status)::text = 'brand_identity_unresolved'::text))"
    ):
        raise RuntimeError("monitoring identity pending index contract drift")

    cursor.execute(
        """
        SELECT p.oid, l.lanname, pg_catalog.pg_get_function_result(p.oid) AS result_type,
               p.prokind, p.provolatile, p.prosecdef, p.proleakproof, p.proconfig,
               btrim(pg_catalog.regexp_replace(p.prosrc, '\\s+', ' ', 'g')) AS source
          FROM pg_catalog.pg_proc p
          JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_catalog.pg_language l ON l.oid = p.prolang
         WHERE n.nspname = 'public'
           AND p.proname = 'reject_monitoring_identity_event_mutation'
           AND pg_catalog.pg_get_function_identity_arguments(p.oid) = ''
        """
    )
    function_rows = cursor.fetchall()
    if len(function_rows) != 1:
        raise RuntimeError("monitoring identity append-only function identity drift")
    function_row = function_rows[0]
    expected_source = "BEGIN RAISE EXCEPTION 'monitoring identity decision events are append-only'; END;"
    if not (
        value(function_row, "lanname", 1) == "plpgsql"
        and value(function_row, "result_type", 2) == "trigger"
        and value(function_row, "prokind", 3) == "f"
        and value(function_row, "provolatile", 4) == "v"
        and value(function_row, "prosecdef", 5) is False
        and value(function_row, "proleakproof", 6) is False
        and value(function_row, "proconfig", 7) is None
        and value(function_row, "source", 8) == expected_source
    ):
        raise RuntimeError("monitoring identity append-only function drift")

    cursor.execute(
        """
        SELECT t.tgenabled, t.tgfoid, t.tgtype, t.tgqual, t.tgnargs,
               pg_catalog.pg_get_triggerdef(t.oid, TRUE) AS definition
          FROM pg_catalog.pg_trigger t
         WHERE t.tgrelid = %s
           AND t.tgname = 'trg_monitoring_identity_events_append_only'
           AND NOT t.tgisinternal
        """,
        (relations["monitoring_identity_decision_events"]["oid"],),
    )
    trigger_rows = cursor.fetchall()
    if len(trigger_rows) != 1 or not (
        value(trigger_rows[0], "tgenabled") == "O"
        and value(trigger_rows[0], "tgfoid", 1) == value(function_row, "oid")
        and value(trigger_rows[0], "tgtype", 2) == 27
        and value(trigger_rows[0], "tgqual", 3) is None
        and value(trigger_rows[0], "tgnargs", 4) == 0
        and normalize_sql(value(trigger_rows[0], "definition", 5))
        == "CREATE TRIGGER trg_monitoring_identity_events_append_only BEFORE DELETE OR UPDATE ON monitoring_identity_decision_events FOR EACH ROW EXECUTE FUNCTION reject_monitoring_identity_event_mutation()"
    ):
        raise RuntimeError("monitoring identity append-only trigger drift")

    cursor.execute("SELECT current_setting('session_replication_role') AS role")
    if value(cursor.fetchone(), "role") != "origin":
        raise RuntimeError("monitoring identity append-only trigger requires origin role")


def _normalize_pg16_constraint_definition(definition: object) -> str:
    normalized = re.sub(r"\s+", " ", str(definition or "")).strip()
    # A PG16 pg_dump/pg_restore round trip can make the implicit text coercion
    # on varchar array literals explicit.  Both forms resolve to the same
    # pg_catalog expression tree, so keep the exact-shape guard while folding
    # only this server-generated, semantics-preserving spelling difference.
    return (
        normalized
        .replace("::character varying::text", "::character varying")
        .replace("]::text[]", "]")
    )


def assert_monitoring_cell_retry_ready(cursor) -> None:
    """Read-only startup guard for the durable execution/retry ledger."""
    def value(row, key, index=0):
        if isinstance(row, dict):
            return row.get(key)
        return row[index] if row else None

    expected = {
        "monitoring_run_cells": {
            "id", "task_id", "brand_id", "keyword_id", "keyword_source", "quote_id",
            "keyword_snapshot", "question_snapshot", "target_brand_snapshot", "platform",
            "is_planned", "state", "entitlement_snapshot", "order_snapshot",
            "fulfillment_credential", "fulfillment_state", "settlement_reference",
            "plan_hash", "attempt_count",
            "retry_count", "claim_token", "claim_until", "provider_dispatched_at",
            "provider_request_id", "result_id", "error_code", "error_message",
            "started_at", "completed_at", "created_at", "updated_at",
            # [工单 E3-1 · 迁移 054] 建格时冻下来的租户归属。
            "tenant_owner_user_id",
        },
        "monitoring_cell_retry_requests": {
            "request_id", "cell_id", "task_id", "brand_id", "plan_hash", "params_hash",
            "status", "claim_token", "response_snapshot", "created_at", "completed_at",
        },
        "monitoring_keyword_settlements": {
            "settlement_reference", "task_id", "brand_id", "keyword_id",
            "keyword_source", "subscription_id", "billing_user_id", "feature_code",
            "claim_token", "previous_claim_at", "freeze_id", "freeze_table",
            "frozen_amount", "state", "charge_recorded", "created_at", "updated_at",
        },
        "monitoring_provider_review_events": {
            "request_id", "cell_id", "task_id", "brand_id", "plan_hash", "action",
            "actor_user_id", "note", "state_before", "state_after", "created_at",
        },
    }
    # Defaults and NOT NULL are executable parts of the contract, not cosmetic
    # metadata.  CREATE TABLE IF NOT EXISTS cannot repair a half-built table.
    required_column_shape = {
        "monitoring_run_cells": {
            "id": ("bigint", True, "d", None),
            "task_id": ("integer", True, "", None),
            "brand_id": ("integer", True, "", None),
            "keyword_id": ("bigint", True, "", None),
            "keyword_source": ("character varying(16)", True, "", None),
            "keyword_snapshot": ("text", True, "", None),
            "question_snapshot": ("text", True, "", None),
            "target_brand_snapshot": ("text", True, "", None),
            "platform": ("character varying(32)", True, "", None),
            "is_planned": ("boolean", True, "", None),
            "state": ("character varying(40)", True, "", "'queued'::character varying"),
            "entitlement_snapshot": ("jsonb", True, "", None),
            "order_snapshot": ("jsonb", True, "", None),
            "fulfillment_credential": ("uuid", True, "", None),
            "fulfillment_state": (
                "character varying(24)", True, "", "'reserved'::character varying"
            ),
            "plan_hash": ("character(64)", True, "", None),
            "attempt_count": ("integer", True, "", "0"),
            "retry_count": ("integer", True, "", "0"),
            "created_at": ("timestamp with time zone", True, "", "now()"),
            "updated_at": ("timestamp with time zone", True, "", "now()"),
            # [工单 E3-1 · 迁移 054] **可空**是有意的:NULL = 租户未知。
            # 加上 NOT NULL 就等于逼调用方编一个,而编出来的那个正是 0。
            "tenant_owner_user_id": ("integer", False, "", None),
        },
        "monitoring_cell_retry_requests": {
            "request_id": ("uuid", True, "", None),
            "cell_id": ("bigint", True, "", None),
            "task_id": ("integer", True, "", None),
            "brand_id": ("integer", True, "", None),
            "plan_hash": ("character(64)", True, "", None),
            "params_hash": ("character(64)", True, "", None),
            "status": ("character varying(24)", True, "", "'accepted'::character varying"),
            "response_snapshot": ("jsonb", True, "", "'{}'::jsonb"),
            "created_at": ("timestamp with time zone", True, "", "now()"),
        },
        "monitoring_keyword_settlements": {
            "settlement_reference": ("character varying(160)", True, "", None),
            "task_id": ("integer", True, "", None),
            "brand_id": ("integer", True, "", None),
            "keyword_id": ("bigint", True, "", None),
            "keyword_source": ("character varying(24)", True, "", None),
            "feature_code": ("character varying(80)", True, "", None),
            "frozen_amount": ("bigint", True, "", "0"),
            "state": ("character varying(32)", True, "", "'reserved'::character varying"),
            "charge_recorded": ("boolean", True, "", "false"),
            "created_at": ("timestamp with time zone", True, "", "now()"),
            "updated_at": ("timestamp with time zone", True, "", "now()"),
        },
        "monitoring_provider_review_events": {
            "request_id": ("uuid", True, "", None),
            "cell_id": ("bigint", True, "", None),
            "task_id": ("integer", True, "", None),
            "brand_id": ("integer", True, "", None),
            "plan_hash": ("character(64)", True, "", None),
            "action": ("character varying(32)", True, "", None),
            "actor_user_id": ("integer", True, "", None),
            "note": ("text", True, "", None),
            "state_before": ("character varying(40)", True, "", None),
            "state_after": ("character varying(40)", True, "", None),
            "created_at": ("timestamp with time zone", True, "", "now()"),
        },
    }
    relation_oids = {}
    for table_name, expected_columns in expected.items():
        cursor.execute(
            """
            SELECT c.oid, c.relkind, c.relpersistence
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relname=%s
            """,
            (table_name,),
        )
        relation = cursor.fetchone()
        if not relation or value(relation, "relkind", 1) != "r" or value(relation, "relpersistence", 2) != "p":
            raise RuntimeError(f"public.{table_name} is not an ordinary permanent table")
        relation_oids[table_name] = value(relation, "oid")
        cursor.execute(
            """
            SELECT a.attname
              FROM pg_catalog.pg_attribute a
             WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped
            """,
            (relation_oids[table_name],),
        )
        actual_columns = {value(row, "attname") for row in cursor.fetchall()}
        if actual_columns != expected_columns:
            raise RuntimeError(f"monitoring cell retry columns drifted: {table_name}")
        cursor.execute(
            """
            SELECT a.attname,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                   a.attnotnull, a.attidentity,
                   pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_expr
              FROM pg_catalog.pg_attribute a
              LEFT JOIN pg_catalog.pg_attrdef d
                ON d.adrelid=a.attrelid AND d.adnum=a.attnum
             WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped
            """,
            (relation_oids[table_name],),
        )
        column_rows = {value(row, "attname"): row for row in cursor.fetchall()}
        drifted_columns = []
        for column_name, shape in required_column_shape[table_name].items():
            row = column_rows.get(column_name)
            actual_shape = (
                str(value(row, "data_type", 1) or ""),
                value(row, "attnotnull", 2) is True,
                str(value(row, "attidentity", 3) or ""),
                value(row, "default_expr", 4),
            )
            if not row or actual_shape != shape:
                drifted_columns.append(column_name)
        if drifted_columns:
            raise RuntimeError(
                f"monitoring cell retry column definitions drifted: "
                f"{table_name}:{sorted(drifted_columns)}"
            )

    required_constraints = {
        "monitoring_run_cells_pkey", "uq_monitoring_run_cells_plan",
        "uq_monitoring_run_cells_retry_scope", "fk_monitoring_run_cells_task",
        "fk_monitoring_run_cells_task_brand",
        "fk_monitoring_run_cells_brand", "fk_monitoring_run_cells_quote_brand",
        "fk_monitoring_run_cells_result_task", "chk_monitoring_run_cells_keyword_source",
        "chk_monitoring_run_cells_platform", "chk_monitoring_run_cells_state",
        "chk_monitoring_run_cells_fulfillment_state", "chk_monitoring_run_cells_counts",
        "chk_monitoring_run_cells_json", "chk_monitoring_run_cells_plan_shape",
        "chk_monitoring_run_cells_result_terminal", "chk_monitoring_run_cells_claim",
        # [工单 E3-1 · 迁移 054] 禁 0 —— 0 号用户不存在,那是被本单消灭的编造租户。
        "chk_monitoring_run_cells_tenant_owner_positive",
        "monitoring_cell_retry_requests_pkey", "fk_monitoring_cell_retry_requests_cell",
        "fk_monitoring_cell_retry_requests_task", "fk_monitoring_cell_retry_requests_brand",
        "fk_monitoring_cell_retry_requests_scope",
        "chk_monitoring_cell_retry_requests_status", "chk_monitoring_cell_retry_requests_response",
        "chk_monitoring_cell_retry_requests_lifecycle",
        "monitoring_keyword_settlements_pkey", "uq_monitoring_keyword_settlement_scope",
        "fk_monitoring_keyword_settlement_task_brand",
        "chk_monitoring_keyword_settlement_source", "chk_monitoring_keyword_settlement_state",
        "chk_monitoring_keyword_settlement_freeze",
        "monitoring_provider_review_events_pkey", "fk_monitoring_provider_review_scope",
        "chk_monitoring_provider_review_action",
        "chk_monitoring_provider_review_states",
    }
    cursor.execute(
        """
        SELECT conname, contype, convalidated,
               pg_catalog.pg_get_constraintdef(oid, TRUE) AS definition,
               condeferrable, condeferred
          FROM pg_catalog.pg_constraint
         WHERE conrelid = ANY(%s)
        """,
        (list(relation_oids.values()),),
    )
    constraint_rows = {value(row, "conname"): row for row in cursor.fetchall()}
    constraints = {
        name: value(row, "convalidated", 2) for name, row in constraint_rows.items()
    }
    missing = sorted(name for name in required_constraints if constraints.get(name) is not True)
    if missing:
        raise RuntimeError(f"monitoring cell retry constraints missing or weak: {missing}")

    expected_constraint_shape = {
        "monitoring_run_cells_pkey": ("p", "PRIMARY KEY (id)"),
        "uq_monitoring_run_cells_plan": (
            "u", "UNIQUE (task_id, keyword_source, keyword_id, platform)"
        ),
        "uq_monitoring_run_cells_retry_scope": (
            "u", "UNIQUE (id, task_id, brand_id, plan_hash)"
        ),
        "fk_monitoring_run_cells_task": (
            "f", "FOREIGN KEY (task_id) REFERENCES monitoring_tasks(id) ON DELETE RESTRICT"
        ),
        "fk_monitoring_run_cells_task_brand": (
            "f", "FOREIGN KEY (task_id, brand_id) REFERENCES monitoring_tasks(id, brand_id) ON DELETE RESTRICT"
        ),
        "fk_monitoring_run_cells_brand": (
            "f", "FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE RESTRICT"
        ),
        "fk_monitoring_run_cells_quote_brand": (
            "f", "FOREIGN KEY (quote_id, brand_id) REFERENCES quotes(id, brand_id) ON DELETE RESTRICT"
        ),
        "fk_monitoring_run_cells_result_task": (
            "f", "FOREIGN KEY (result_id, task_id) REFERENCES monitoring_results(id, task_id) ON DELETE RESTRICT"
        ),
        "chk_monitoring_run_cells_keyword_source": (
            "c", "CHECK (keyword_source::text = ANY (ARRAY['confirmed'::character varying, 'contract'::character varying, 'extra'::character varying]::text[]))"
        ),
        "chk_monitoring_run_cells_platform": (
            "c", "CHECK (platform::text = ANY (ARRAY['dashscope'::character varying, 'deepseek'::character varying, 'kimi'::character varying, 'doubao'::character varying]::text[]))"
        ),
        "chk_monitoring_run_cells_state": (
            "c", "CHECK (state::text = ANY (ARRAY['queued'::character varying, 'running'::character varying, 'succeeded'::character varying, 'failed'::character varying, 'pending_identity'::character varying, 'unavailable'::character varying, 'pending_provider_confirmation'::character varying]::text[]))"
        ),
        "chk_monitoring_run_cells_fulfillment_state": (
            "c", "CHECK (fulfillment_state::text = ANY (ARRAY['reserved'::character varying, 'covered'::character varying, 'admin_covered'::character varying, 'coverage_unknown'::character varying, 'released'::character varying]::text[]))"
        ),
        "chk_monitoring_run_cells_counts": (
            "c", "CHECK (attempt_count >= 0 AND retry_count >= 0 AND retry_count <= attempt_count)"
        ),
        "chk_monitoring_run_cells_json": (
            "c", "CHECK (jsonb_typeof(entitlement_snapshot) = 'object'::text AND jsonb_typeof(order_snapshot) = 'object'::text)"
        ),
        "chk_monitoring_run_cells_plan_shape": (
            "c", "CHECK (is_planned OR NOT is_planned AND state::text = 'unavailable'::text)"
        ),
        "chk_monitoring_run_cells_result_terminal": (
            "c", "CHECK ((state::text = ANY (ARRAY['succeeded'::character varying, 'pending_identity'::character varying]::text[])) AND result_id IS NOT NULL OR (state::text <> ALL (ARRAY['succeeded'::character varying, 'pending_identity'::character varying]::text[])) AND result_id IS NULL)"
        ),
        "chk_monitoring_run_cells_claim": (
            "c", "CHECK (state::text = 'running'::text AND claim_token IS NOT NULL AND claim_until IS NOT NULL OR state::text <> 'running'::text AND claim_token IS NULL AND claim_until IS NULL)"
        ),
        # [工单 E3-1 · 迁移 054] 逐字定义锁 —— 只核名字挡不住"同名弱 CHECK"
        # (本轮 051/052 被 Codex 打穿的正是那一格)。
        "chk_monitoring_run_cells_tenant_owner_positive": (
            "c", "CHECK (tenant_owner_user_id IS NULL OR tenant_owner_user_id > 0)"
        ),
        "monitoring_cell_retry_requests_pkey": ("p", "PRIMARY KEY (request_id)"),
        "fk_monitoring_cell_retry_requests_cell": (
            "f", "FOREIGN KEY (cell_id) REFERENCES monitoring_run_cells(id) ON DELETE RESTRICT"
        ),
        "fk_monitoring_cell_retry_requests_task": (
            "f", "FOREIGN KEY (task_id) REFERENCES monitoring_tasks(id) ON DELETE RESTRICT"
        ),
        "fk_monitoring_cell_retry_requests_brand": (
            "f", "FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE RESTRICT"
        ),
        "fk_monitoring_cell_retry_requests_scope": (
            "f", "FOREIGN KEY (cell_id, task_id, brand_id, plan_hash) REFERENCES monitoring_run_cells(id, task_id, brand_id, plan_hash) ON DELETE RESTRICT"
        ),
        "chk_monitoring_cell_retry_requests_status": (
            "c", "CHECK (status::text = ANY (ARRAY['accepted'::character varying, 'running'::character varying, 'succeeded'::character varying, 'failed'::character varying, 'blocked'::character varying]::text[]))"
        ),
        "chk_monitoring_cell_retry_requests_response": (
            "c", "CHECK (jsonb_typeof(response_snapshot) = 'object'::text)"
        ),
        "chk_monitoring_cell_retry_requests_lifecycle": (
            "c", "CHECK (status::text = 'accepted'::text AND claim_token IS NULL AND completed_at IS NULL OR status::text = 'running'::text AND claim_token IS NOT NULL AND completed_at IS NULL OR (status::text = ANY (ARRAY['succeeded'::character varying, 'failed'::character varying, 'blocked'::character varying]::text[])) AND claim_token IS NOT NULL AND completed_at IS NOT NULL)"
        ),
        "monitoring_keyword_settlements_pkey": (
            "p", "PRIMARY KEY (settlement_reference)"
        ),
        "uq_monitoring_keyword_settlement_scope": (
            "u", "UNIQUE (task_id, keyword_source, keyword_id)"
        ),
        "fk_monitoring_keyword_settlement_task_brand": (
            "f", "FOREIGN KEY (task_id, brand_id) REFERENCES monitoring_tasks(id, brand_id) ON DELETE RESTRICT"
        ),
        "chk_monitoring_keyword_settlement_source": (
            "c", "CHECK (keyword_source::text = ANY (ARRAY['confirmed'::character varying, 'contract'::character varying, 'extra'::character varying]::text[]))"
        ),
        "chk_monitoring_keyword_settlement_state": (
            "c", "CHECK (state::text = ANY (ARRAY['reserved'::character varying, 'frozen'::character varying, 'coverage_unknown'::character varying, 'committed'::character varying, 'released'::character varying, 'admin_covered'::character varying]::text[]))"
        ),
        "chk_monitoring_keyword_settlement_freeze": (
            "c", "CHECK (frozen_amount >= 0 AND freeze_table IS NULL OR frozen_amount >= 0 AND (freeze_table::text = ANY (ARRAY['legacy'::character varying, 'v35'::character varying]::text[])))"
        ),
        "monitoring_provider_review_events_pkey": ("p", "PRIMARY KEY (request_id)"),
        "fk_monitoring_provider_review_scope": (
            "f", "FOREIGN KEY (cell_id, task_id, brand_id, plan_hash) REFERENCES monitoring_run_cells(id, task_id, brand_id, plan_hash) ON DELETE RESTRICT"
        ),
        "chk_monitoring_provider_review_action": (
            "c", "CHECK (action::text = ANY (ARRAY['confirm_failed'::character varying, 'confirm_unavailable'::character varying]::text[]))"
        ),
        "chk_monitoring_provider_review_states": (
            "c", "CHECK (state_before::text = 'pending_provider_confirmation'::text AND (state_after::text = ANY (ARRAY['failed'::character varying, 'unavailable'::character varying]::text[])))"
        ),
    }
    drifted = []
    for name, (expected_type, expected_definition) in expected_constraint_shape.items():
        row = constraint_rows.get(name)
        definition = _normalize_pg16_constraint_definition(
            value(row, "definition", 3)
        )
        if not row or not (
            value(row, "contype", 1) == expected_type
            and value(row, "convalidated", 2) is True
            and value(row, "condeferrable", 4) is False
            and value(row, "condeferred", 5) is False
            and definition == _normalize_pg16_constraint_definition(
                expected_definition
            )
        ):
            drifted.append(name)
    if drifted:
        raise RuntimeError(f"monitoring cell retry constraint definitions drifted: {sorted(drifted)}")

    authority_uniques = (
        ("public.monitoring_tasks", "uq_monitoring_tasks_id_brand", ["id", "brand_id"]),
        (
            "public.monitoring_results", "uq_monitoring_results_id_task",
            ["id", "task_id"],
        ),
    )
    for relation_name, constraint_name, expected_key_columns in authority_uniques:
        cursor.execute(
            """
            SELECT c.contype, c.convalidated, c.condeferrable, c.condeferred,
                   ARRAY(
                       SELECT a.attname
                         FROM unnest(c.conkey) WITH ORDINALITY key(attnum, ord)
                         JOIN pg_catalog.pg_attribute a
                           ON a.attrelid=c.conrelid AND a.attnum=key.attnum
                        ORDER BY key.ord
                   ) AS key_columns
              FROM pg_catalog.pg_constraint c
             WHERE c.conrelid=%s::pg_catalog.regclass AND c.conname=%s
            """,
            (relation_name, constraint_name),
        )
        authority_unique = cursor.fetchone()
        if not authority_unique or not (
            value(authority_unique, "contype") == "u"
            and value(authority_unique, "convalidated", 1) is True
            and value(authority_unique, "condeferrable", 2) is False
            and value(authority_unique, "condeferred", 3) is False
            and list(value(authority_unique, "key_columns", 4) or [])
            == expected_key_columns
        ):
            raise RuntimeError(
                f"monitoring composite authority constraint drifted: {constraint_name}"
            )

    cursor.execute(
        """
        SELECT c.contype,c.convalidated,c.condeferrable,c.condeferred,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(c.conkey) WITH ORDINALITY key(attnum,ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid=c.conrelid AND a.attnum=key.attnum
                    ORDER BY key.ord
               ) AS key_columns
          FROM pg_catalog.pg_constraint c
         WHERE c.conrelid='public.quotes'::pg_catalog.regclass
           AND c.conname='uq_quotes_id_brand'
        """
    )
    quote_constraint = cursor.fetchone()
    quote_constraint_ok = bool(quote_constraint) and (
        value(quote_constraint, "contype") == "u"
        and value(quote_constraint, "convalidated", 1) is True
        and value(quote_constraint, "condeferrable", 2) is False
        and value(quote_constraint, "condeferred", 3) is False
        and list(value(quote_constraint, "key_columns", 4) or []) == ["id", "brand_id"]
    )

    # The unified manifest's quote snapshot migration runs first and owns this
    # exact composite authority as a unique index. PostgreSQL accepts a valid,
    # non-partial unique index as a referenced key, so requiring a second
    # UNIQUE constraint would only duplicate a production table scan and lock.
    cursor.execute(
        """
        SELECT i.indisunique,i.indisvalid,i.indisready,i.indislive,
               i.indpred IS NULL AS unfiltered,
               i.indexprs IS NULL AS plain_columns,
               i.indnkeyatts,i.indnatts,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(i.indkey) WITH ORDINALITY key(attnum,ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid=i.indrelid AND a.attnum=key.attnum
                    WHERE key.ord <= i.indnkeyatts
                    ORDER BY key.ord
               ) AS key_columns
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class idx ON idx.oid=i.indexrelid
          JOIN pg_catalog.pg_namespace n ON n.oid=idx.relnamespace
         WHERE n.nspname='public' AND idx.relname='ux_quotes_id_brand'
           AND i.indrelid='public.quotes'::pg_catalog.regclass
        """
    )
    quote_authority = cursor.fetchone()
    quote_index_ok = bool(quote_authority) and (
        value(quote_authority, "indisunique") is True
        and value(quote_authority, "indisvalid", 1) is True
        and value(quote_authority, "indisready", 2) is True
        and value(quote_authority, "indislive", 3) is True
        and value(quote_authority, "unfiltered", 4) is True
        and value(quote_authority, "plain_columns", 5) is True
        and int(value(quote_authority, "indnkeyatts", 6) or 0) == 2
        and int(value(quote_authority, "indnatts", 7) or 0) == 2
        and list(value(quote_authority, "key_columns", 8) or []) == ["id", "brand_id"]
    )
    if not quote_constraint_ok and not quote_index_ok:
        raise RuntimeError(
            "monitoring composite authority drifted: "
            "expected uq_quotes_id_brand or ux_quotes_id_brand"
        )

    cursor.execute(
        """
        SELECT i.indisunique, i.indisvalid, i.indisready,
               ARRAY(
                   SELECT a.attname
                     FROM unnest(i.indkey) WITH ORDINALITY key(attnum, ord)
                     JOIN pg_catalog.pg_attribute a
                       ON a.attrelid=i.indrelid AND a.attnum=key.attnum
                    ORDER BY key.ord
               ) AS key_columns,
               pg_catalog.pg_get_expr(i.indpred, i.indrelid) AS predicate
          FROM pg_catalog.pg_index i
          JOIN pg_catalog.pg_class idx ON idx.oid=i.indexrelid
          JOIN pg_catalog.pg_namespace n ON n.oid=idx.relnamespace
         WHERE n.nspname='public' AND idx.relname='ux_monitoring_run_cells_result'
           AND i.indrelid=%s
        """,
        (relation_oids["monitoring_run_cells"],),
    )
    result_index = cursor.fetchone()
    result_predicate = re.sub(
        r"\s+", " ", str(value(result_index, "predicate", 4) or "").strip()
    )
    if not result_index or not (
        value(result_index, "indisunique") is True
        and value(result_index, "indisvalid", 1) is True
        and value(result_index, "indisready", 2) is True
        and list(value(result_index, "key_columns", 3) or []) == ["result_id"]
        and result_predicate == "(result_id IS NOT NULL)"
    ):
        raise RuntimeError("monitoring cell result unique index drifted")

    cursor.execute(
        """
        SELECT t.tgenabled, p.proname,
               pg_catalog.pg_get_triggerdef(t.oid, TRUE) AS trigger_definition,
               p.prosrc AS function_source
          FROM pg_catalog.pg_trigger t
          JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
         WHERE t.tgrelid=%s
           AND t.tgname='trg_monitoring_run_cell_terminal_overwrite'
           AND NOT t.tgisinternal
        """,
        (relation_oids["monitoring_run_cells"],),
    )
    trigger = cursor.fetchone()
    trigger_definition = re.sub(
        r"\s+", " ", str(value(trigger, "trigger_definition", 2) or "")
    ).strip().lower()
    function_source = re.sub(
        r"\s+", " ", str(value(trigger, "function_source", 3) or "")
    ).strip().lower()
    expected_trigger_definition = (
        "create trigger trg_monitoring_run_cell_terminal_overwrite before update "
        "on monitoring_run_cells for each row execute function "
        "reject_monitoring_run_cell_terminal_overwrite()"
    )
    expected_function_source = re.sub(r"\s+", " ", """
        BEGIN
            IF OLD.fulfillment_state IS DISTINCT FROM NEW.fulfillment_state
               AND NOT (
                   (OLD.fulfillment_state = 'reserved'
                    AND NEW.fulfillment_state IN ('covered', 'admin_covered', 'coverage_unknown', 'released'))
                   OR (OLD.fulfillment_state = 'coverage_unknown'
                       AND NEW.fulfillment_state IN ('covered', 'released'))
                   OR (OLD.fulfillment_state = 'covered'
                       AND NEW.fulfillment_state = 'released'
                       AND EXISTS (
                           SELECT 1
                             FROM public.point_freezes pf
                             JOIN public.organization_charge_links charge
                               ON charge.physical_backend='legacy_user_wallet'
                              AND charge.physical_freeze_id=pf.id::text
                            WHERE pf.task_ref=OLD.settlement_reference
                              AND charge.status='refunded'
                       ))
               ) THEN
                RAISE EXCEPTION 'monitoring cell fulfillment transition is invalid';
            END IF;
            IF OLD.state = 'succeeded'
               AND ROW(OLD.state, OLD.result_id, OLD.plan_hash, OLD.entitlement_snapshot,
                       OLD.order_snapshot, OLD.fulfillment_credential)
                   IS DISTINCT FROM
                   ROW(NEW.state, NEW.result_id, NEW.plan_hash, NEW.entitlement_snapshot,
                       NEW.order_snapshot, NEW.fulfillment_credential) THEN
                RAISE EXCEPTION 'successful monitoring cells are immutable';
            END IF;
            IF OLD.state = 'pending_identity'
               AND (
                   NEW.state NOT IN ('pending_identity', 'succeeded')
                   OR OLD.result_id IS DISTINCT FROM NEW.result_id
               ) THEN
                RAISE EXCEPTION 'pending identity monitoring cell transition is invalid';
            END IF;
            IF ROW(OLD.task_id, OLD.brand_id, OLD.keyword_id, OLD.keyword_source,
                   OLD.quote_id, OLD.keyword_snapshot, OLD.question_snapshot,
                   OLD.target_brand_snapshot, OLD.platform, OLD.is_planned,
                   OLD.entitlement_snapshot, OLD.order_snapshot,
                   OLD.fulfillment_credential, OLD.settlement_reference, OLD.plan_hash)
               IS DISTINCT FROM
               ROW(NEW.task_id, NEW.brand_id, NEW.keyword_id, NEW.keyword_source,
                   NEW.quote_id, NEW.keyword_snapshot, NEW.question_snapshot,
                   NEW.target_brand_snapshot, NEW.platform, NEW.is_planned,
                   NEW.entitlement_snapshot, NEW.order_snapshot,
                   NEW.fulfillment_credential, NEW.settlement_reference, NEW.plan_hash) THEN
                RAISE EXCEPTION 'monitoring cell plan is immutable';
            END IF;
            RETURN NEW;
        END;
    """).strip().lower()
    if not trigger or not (
        value(trigger, "tgenabled") == "O"
        and value(trigger, "proname", 1) == "reject_monitoring_run_cell_terminal_overwrite"
        and trigger_definition == expected_trigger_definition
        and function_source == expected_function_source
    ):
        raise RuntimeError("monitoring successful-cell immutability trigger missing or disabled")

    cursor.execute(
        """
        SELECT t.tgenabled, p.proname,
               pg_catalog.pg_get_triggerdef(t.oid, TRUE) AS trigger_definition,
               p.prosrc AS function_source
          FROM pg_catalog.pg_trigger t
          JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
         WHERE t.tgrelid=%s
           AND t.tgname='trg_monitoring_provider_review_events_append_only'
           AND NOT t.tgisinternal
        """,
        (relation_oids["monitoring_provider_review_events"],),
    )
    review_trigger = cursor.fetchone()
    review_definition = re.sub(
        r"\s+", " ", str(value(review_trigger, "trigger_definition", 2) or "")
    ).lower()
    review_source = re.sub(
        r"\s+", " ", str(value(review_trigger, "function_source", 3) or "")
    ).strip().lower()
    if not review_trigger or not (
        value(review_trigger, "tgenabled") == "O"
        and value(review_trigger, "proname", 1)
        == "reject_monitoring_provider_review_event_mutation"
        and review_definition
        == "create trigger trg_monitoring_provider_review_events_append_only before delete or update on monitoring_provider_review_events for each row execute function reject_monitoring_provider_review_event_mutation()"
        and review_source
        == "begin raise exception 'monitoring provider review events are append-only'; end;"
    ):
        raise RuntimeError("monitoring provider review append-only trigger drifted")
    cursor.execute(
        """
        SELECT t.tgenabled, p.proname,
               pg_catalog.pg_get_triggerdef(t.oid, TRUE) AS trigger_definition
          FROM pg_catalog.pg_trigger t
          JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
         WHERE t.tgrelid=%s
           AND t.tgname='trg_monitoring_provider_review_events_no_truncate'
           AND NOT t.tgisinternal
        """,
        (relation_oids["monitoring_provider_review_events"],),
    )
    truncate_trigger = cursor.fetchone()
    truncate_definition = re.sub(
        r"\s+", " ", str(value(truncate_trigger, "trigger_definition", 2) or "")
    ).strip().lower()
    if not truncate_trigger or not (
        value(truncate_trigger, "tgenabled") == "O"
        and value(truncate_trigger, "proname", 1)
        == "reject_monitoring_provider_review_event_mutation"
        and truncate_definition
        == "create trigger trg_monitoring_provider_review_events_no_truncate before truncate on monitoring_provider_review_events for each statement execute function reject_monitoring_provider_review_event_mutation()"
    ):
        raise RuntimeError("monitoring provider review truncate guard drifted")
    cursor.execute("SELECT current_setting('session_replication_role') AS role")
    if value(cursor.fetchone(), "role") != "origin":
        raise RuntimeError("monitoring cell retry triggers require origin role")


def normalize_monitoring_platform(platform: str) -> str:
    """把平台别名归一到权重配置使用的内部键。"""
    raw = str(platform or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"[\s_\-·/\.]+", "", raw.lower())

    if "豆包" in raw or "doubao" in compact or "bytedance" in compact or "volcengine" in compact:
        return "doubao"
    if (
        "通义" in raw
        or "千问" in raw
        or "qwen" in compact
        or "dashscope" in compact
        or "tongyi" in compact
        or "aliyun" in compact
    ):
        return "dashscope"
    if "deepseek" in compact:
        return "deepseek"
    if "元宝" in raw or "yuanbao" in compact or "hunyuan" in compact or "hy3" in compact:
        return "yuanbao"
    if "kimi" in compact or "moonshot" in compact:
        return "kimi"
    return compact or raw.lower()


def get_platform_weight(platform: str) -> float:
    """获取平台权重，未知平台返回等权"""
    platform_key = normalize_monitoring_platform(platform)
    return PLATFORM_WEIGHTS.get(platform_key, 1.0 / max(len(PLATFORM_WEIGHTS), 1))


def _normalize_platform_weights(weights: Dict[str, Any]) -> Dict[str, float]:
    normalized: Dict[str, float] = {}
    for platform, weight in (weights or {}).items():
        platform_key = normalize_monitoring_platform(platform)
        if not platform_key:
            continue
        try:
            normalized[platform_key] = float(weight)
        except (TypeError, ValueError):
            continue
    return normalized


def normalize_active_platform_weights(weights: Dict[str, Any]) -> Dict[str, float]:
    """Return a complete normalized map for the monitoring product matrix.

    [P0-2 · 2026-07-26] 统一五引擎后 kimi 与 yuanbao 都是一等平台，删掉旧的
    "kimi 缺失时拿 yuanbao 的权重顶上" 别名兜底 —— 那是四路时代为避免整表
    失效的过渡逻辑，现在会把两个真实平台的权重混成一个。缺失平台一律按
    ``DEFAULT_PLATFORM_WEIGHTS`` 补齐后重归一，不再互相冒名。
    """
    normalized = _normalize_platform_weights(weights)
    active = {
        platform: max(float(normalized.get(platform, DEFAULT_PLATFORM_WEIGHTS[platform])), 0.0)
        for platform in PLATFORM_CANONICAL_ORDER
    }
    total = sum(active.values())
    if total <= 0:
        return dict(DEFAULT_PLATFORM_WEIGHTS)
    return {platform: weight / total for platform, weight in active.items()}


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def quote_unfulfilled_compliance_condition_sql(alias: str = "q") -> str:
    """SQL condition: paid quote still has at least one keyword not fully fulfilled.

    Service fulfillment is based on compliant days, not calendar expiry.
    Used as part of the monitoring service-anchor SSOT.
    """
    a = alias
    return f"""
        (
            -- compliant_days = COUNT(keyword_compliance_log.is_compliant = TRUE)
            EXISTS (
                SELECT 1
                FROM confirmed_keywords ck
                WHERE ck.quote_id = {a}.id
                  AND (ck.is_core IS NOT FALSE)
                  AND COALESCE(ck.super_red_ocean, FALSE) = FALSE
                  AND COALESCE((
                      SELECT COUNT(*) FILTER (WHERE kcl.is_compliant = TRUE)
                      FROM keyword_compliance_log kcl
                      WHERE kcl.keyword_id = ck.id
                        AND kcl.keyword_source = 'confirmed'
                        AND kcl.check_date >= COALESCE({a}.service_start_date, {a}.paid_at::date)
                  ), 0) < {a}.service_days
            )
            OR EXISTS (
                SELECT 1
                FROM extra_keywords ek
                WHERE ek.quote_id = {a}.id
                  AND ek.status = 'active'
                  AND COALESCE((
                      SELECT COUNT(*) FILTER (WHERE kcl.is_compliant = TRUE)
                      FROM keyword_compliance_log kcl
                      WHERE kcl.keyword_id = ek.id
                        AND kcl.keyword_source = 'extra'
                        AND kcl.check_date >= COALESCE({a}.service_start_date, {a}.paid_at::date)
                  ), 0) < {a}.service_days
            )
        )
    """


def quote_service_anchor_condition_sql(alias: str = "q") -> str:
    """Monitoring service-anchor SSOT.

    Paid quotes are monitorable unless manually stopped/cancelled. Calendar
    expired quotes remain monitorable while any purchased keyword has not yet
    accumulated service_days compliant days from keyword_compliance_log
    (compliant_days < service_days · migration_028 已置 NOT NULL,不再有 365 兜底).
    Confirmed quotes
    still require a service anchor, matching the existing offline-payment path.
    """
    a = alias
    unfulfilled = quote_unfulfilled_compliance_condition_sql(a)
    return f"""
        (
            (
                {a}.status = 'paid'
                AND COALESCE({a}.service_status, 'active') NOT IN ('cancelled', 'inactive')
                AND (
                    COALESCE({a}.service_status, 'active') <> 'expired'
                    OR {unfulfilled}
                )
            )
            OR (
                {a}.status = 'confirmed'
                AND COALESCE({a}.service_start_date, {a}.paid_at) IS NOT NULL
            )
        )
    """


def _platform_weight_values_sql() -> str:
    normalized: Dict[str, float] = {}
    for platform in PLATFORM_CANONICAL_ORDER:
        if platform in PLATFORM_WEIGHTS:
            normalized[platform] = float(PLATFORM_WEIGHTS[platform])
    normalized.update(_normalize_platform_weights(PLATFORM_WEIGHTS))
    if not normalized:
        fallback = 1.0 / max(len(PLATFORM_CANONICAL_ORDER), 1)
        normalized = {platform: fallback for platform in PLATFORM_CANONICAL_ORDER}
    return ", ".join(
        f"({_sql_literal(platform)}, {float(weight)})"
        for platform, weight in normalized.items()
    )


def _platform_key_sql_expr(column: str) -> str:
    normalized = (
        f"LOWER(REGEXP_REPLACE(TRIM(COALESCE({column}, '')), "
        "'[[:space:]_\\-·/\\.]+', '', 'g'))"
    )
    # P0 hotfix 2026-06-23:本片段会被嵌入 get_client_keywords 的 cursor.execute(query, params)。
    # psycopg2 在传 params 时把 SQL 里的 '%' 当参数占位符,LIKE '%xx%' 的字面 '%' 会撑爆参数计数
    # → IndexError: tuple index out of range（监测列表全空)。字面 '%' 必须写成 '%%'(psycopg2 还原为单个 '%')。
    return f"""CASE
                WHEN {normalized} LIKE '%%doubao%%' OR {normalized} LIKE '%%豆包%%' OR {normalized} LIKE '%%bytedance%%' OR {normalized} LIKE '%%volcengine%%' THEN 'doubao'
                WHEN {normalized} LIKE '%%qwen%%' OR {normalized} LIKE '%%dashscope%%' OR {normalized} LIKE '%%tongyi%%' OR {normalized} LIKE '%%通义%%' OR {normalized} LIKE '%%千问%%' OR {normalized} LIKE '%%aliyun%%' THEN 'dashscope'
                WHEN {normalized} LIKE '%%deepseek%%' THEN 'deepseek'
                WHEN {normalized} LIKE '%%kimi%%' OR {normalized} LIKE '%%moonshot%%' THEN 'kimi'
                ELSE {normalized}
            END"""


def get_connection():
    """获取数据库连接"""
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


# ================================================================
# 平台权重 DB 持久化（支持动态更新）
# ================================================================

def get_saved_platform_weights() -> Optional[Dict[str, Any]]:
    """从 monitoring_config 表读取已保存的平台权重"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT config_value FROM monitoring_config WHERE client_id = '_platform_weights_'"
        )
        row = cursor.fetchone()
        conn.close()
        if row and row.get("config_value"):
            return json.loads(row["config_value"])
    except Exception:
        pass
    return None


def save_platform_weights(weights: Dict[str, float], mau_data: Dict[str, str] = None, source: str = "manual") -> bool:
    """
    保存平台权重到 DB 并更新内存中的 PLATFORM_WEIGHTS。
    weights: {"doubao": 0.35, "dashscope": 0.30, ...}
    mau_data: {"doubao": "2.27亿", ...}  (可选, 记录MAU原始数据)
    """
    global PLATFORM_WEIGHTS
    normalized_weights = normalize_active_platform_weights(weights)
    config_value = json.dumps({
        "weights": normalized_weights,
        "mau_data": mau_data or {},
        "source": source,
        "updated_at": datetime.now().isoformat()
    }, ensure_ascii=False)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO monitoring_config (client_id, default_concurrency, config_value)
            VALUES ('_platform_weights_', 0, %s)
            ON CONFLICT (client_id) DO UPDATE
            SET config_value = EXCLUDED.config_value, updated_at = NOW()
        """, (config_value,))
        conn.commit()
        # 更新内存中的权重
        PLATFORM_WEIGHTS.clear()
        PLATFORM_WEIGHTS.update(normalized_weights)
        return True
    except Exception as e:
        conn.rollback()
        print(f"[save_platform_weights] 保存失败: {e}")
        return False
    finally:
        conn.close()


def load_platform_weights_from_db():
    """启动时从 DB 加载权重到内存（如果有保存的话）"""
    global PLATFORM_WEIGHTS
    saved = get_saved_platform_weights()
    if saved and saved.get("weights"):
        PLATFORM_WEIGHTS.clear()
        PLATFORM_WEIGHTS.update(normalize_active_platform_weights(saved["weights"]))
        print(f"[Platform Weights] 从DB加载: {PLATFORM_WEIGHTS}")
    else:
        print(f"[Platform Weights] 使用默认: {PLATFORM_WEIGHTS}")


def _resolve_id(brand_id: int = None, client_id: str = None) -> tuple:
    """
    统一 ID 解析辅助函数（兼容期）。

    Returns: (brand_id: int|None, client_id_compat: str)
    - 如果传入 brand_id → 反查 quote_id 作为 client_id_compat
    - 如果只传入 client_id → 尝试反查 brand_id，client_id_compat = client_id
    """
    if brand_id is not None:
        # 反查对应的 quote_id,使 client_id 字段可被客户端门户查询
        # [Deploy-CTO 2026-05-26 根治 A] 老板报"加 keyword 提示已添加但列表无"真因:
        #   原 SQL "LIMIT 1" 没 ORDER BY · PostgreSQL undefined behavior 随机挑 quote ·
        #   同 brand 多 quote(brand 387 有 13 个)时 add 跟 list 命中不同 quote · 数据漂移
        # 修:ORDER BY 优先 paid/confirmed > created_at DESC · 稳定挑最新活跃 quote
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM quotes
                WHERE brand_id = %s
                ORDER BY
                  CASE WHEN status IN ('paid','confirmed') THEN 0 ELSE 1 END,
                  created_at DESC
                LIMIT 1
            """, (brand_id,))
            row = cursor.fetchone()
            conn.close()
            quote_id = str(row["id"]) if row else str(brand_id)
            return brand_id, quote_id
        except Exception:
            return brand_id, str(brand_id)
    elif client_id is not None:
        # 兼容旧路径：client_id 实为 quote_id，反查 brand_id
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT brand_id FROM quotes WHERE id = %s", (int(client_id),))
            row = cursor.fetchone()
            conn.close()
            bid = row["brand_id"] if row else None
            return bid, client_id
        except Exception:
            return None, client_id
    else:
        return None, ""


def init_monitoring_tables():
    """初始化监测相关表"""
    conn = get_connection()
    try:
        conn.autocommit = True
        cursor = conn.cursor()

        # 额外词条表（手动补充的词条，区别于confirmed_keywords报价词条）
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS extra_keywords (
                id SERIAL PRIMARY KEY,
                quote_id INTEGER,
                client_id TEXT NOT NULL,
                brand_id INTEGER,
                keyword TEXT NOT NULL,
                target_brand TEXT NOT NULL,
                difficulty TEXT DEFAULT '中等',
                target_rate INTEGER DEFAULT 60,
                platforms TEXT DEFAULT '{DEFAULT_MONITORING_PLATFORMS}',
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active',
                UNIQUE(quote_id, keyword),
                FOREIGN KEY(quote_id) REFERENCES quotes(id)
            )
        """)

        # 客户词条表（用于add_keyword等API）
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS client_keywords (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL,
                brand_id INTEGER,
                keyword TEXT NOT NULL,
                target_brand TEXT NOT NULL,
                difficulty TEXT DEFAULT '中等',
                target_rate INTEGER DEFAULT 60,
                platforms TEXT DEFAULT '{DEFAULT_MONITORING_PLATFORMS}',
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active',
                UNIQUE(client_id, keyword)
            )
        """)

        # 监测任务表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_tasks (
                id SERIAL PRIMARY KEY,
                quote_id INTEGER,
                client_id TEXT NOT NULL,
                brand_id INTEGER,
                task_name TEXT,
                keyword_count INTEGER DEFAULT 0,
                platform_count INTEGER DEFAULT 0,
                total_tests INTEGER DEFAULT 0,
                completed_tests INTEGER DEFAULT 0,
                concurrency INTEGER DEFAULT 10,
                test_rounds INTEGER DEFAULT 3,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                result_summary TEXT,
                FOREIGN KEY(quote_id) REFERENCES quotes(id)
            )
        """)

        # 监测结果表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_results (
                id SERIAL PRIMARY KEY,
                task_id INTEGER,
                keyword_id INTEGER,
                confirmed_keyword_id INTEGER,
                keyword TEXT NOT NULL,
                platform TEXT NOT NULL,
                round_number INTEGER DEFAULT 1,
                is_detected SMALLINT DEFAULT 0,
                mention_type TEXT DEFAULT 'none',
                response_snippet TEXT,
                full_response TEXT,
                tested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES monitoring_tasks(id),
                FOREIGN KEY(keyword_id) REFERENCES extra_keywords(id),
                FOREIGN KEY(confirmed_keyword_id) REFERENCES confirmed_keywords(id)
            )
        """)

        # 报告记录表（支持审核流程）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_reports (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL,
                brand_id INTEGER,
                report_type TEXT NOT NULL,
                period_start DATE,
                period_end DATE,
                summary_data TEXT,
                content TEXT,
                status TEXT DEFAULT 'draft',
                reviewed_by TEXT,
                reviewed_at TIMESTAMP,
                sent_at TIMESTAMP,
                excel_path TEXT,
                pdf_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 监测配置表（含并发数）
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS monitoring_config (
                id SERIAL PRIMARY KEY,
                client_id TEXT UNIQUE,
                brand_id INTEGER,
                default_concurrency INTEGER DEFAULT 10,
                default_platforms TEXT DEFAULT '{DEFAULT_MONITORING_PLATFORMS}',
                auto_monitor_enabled SMALLINT DEFAULT 0,
                auto_monitor_cron TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    
        # 插入全局默认配置
        cursor.execute("""
            INSERT INTO monitoring_config (client_id, default_concurrency)
            VALUES ('_global_', 20)
            ON CONFLICT (client_id) DO UPDATE SET default_concurrency = 20
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS public.monitoring_product_platform_matrices (
                version TEXT PRIMARY KEY,
                platforms TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # 新售矩阵 + 历史 classic4 矩阵都必须在（老词外键指向 classic4，不得因新版本落地而消失）
        cursor.executemany("""
            INSERT INTO public.monitoring_product_platform_matrices (version, platforms)
            VALUES (%s, %s)
            ON CONFLICT (version) DO NOTHING
        """, [
            (LEGACY_MONITORING_PRODUCT_VERSION, LEGACY_MONITORING_PLATFORMS),
            (DEFAULT_MONITORING_PRODUCT_VERSION, DEFAULT_MONITORING_PLATFORMS),
        ])
        cursor.execute(f"""
            ALTER TABLE public.confirmed_keywords
            ADD COLUMN IF NOT EXISTS monitoring_product_version TEXT
            DEFAULT '{DEFAULT_MONITORING_PRODUCT_VERSION}'
        """)
    
        # ========== 新增：趋势统计表 ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keyword_trend_stats (
                id SERIAL PRIMARY KEY,
                keyword_id INTEGER,
                keyword_source TEXT DEFAULT 'confirmed',
                period_type TEXT NOT NULL,
                period_date DATE NOT NULL,
                test_count INTEGER DEFAULT 0,
                detected_count INTEGER DEFAULT 0,
                detection_rate REAL DEFAULT 0,
                rate_change REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(keyword_id, keyword_source, period_type, period_date)
            )
        """)
    
        # ========== 新增：Token消耗表 ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_token_usage (
                id SERIAL PRIMARY KEY,
                task_id INTEGER,
                platform TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                estimated_cost REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES monitoring_tasks(id)
            )
        """)
    
        # ========== 新增：客户访问令牌表 ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS client_access_tokens (
                id SERIAL PRIMARY KEY,
                quote_id INTEGER NOT NULL,
                brand_name TEXT,
                token TEXT UNIQUE NOT NULL,
                is_active SMALLINT DEFAULT 1,
                expires_at DATE,
                last_access_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(quote_id) REFERENCES quotes(id)
            )
        """)
    
        # ========== 新增：媒体投放记录表 ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS media_publications (
                id SERIAL PRIMARY KEY,
                quote_id INTEGER NOT NULL,
                article_id INTEGER,
                platform_name TEXT NOT NULL,
                platform_url TEXT,
                article_title TEXT,
                publish_date DATE,
                operator_id TEXT,
                screenshot_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(quote_id) REFERENCES quotes(id)
            )
        """)
    
        # ========== 新增：操作日志表 ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operation_logs (
                id SERIAL PRIMARY KEY,
                operator_id TEXT NOT NULL,
                brand_id INTEGER,
                action TEXT NOT NULL,
                target_type TEXT,
                target_id INTEGER,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    
        # ========== 新增：通知中心表 ==========
        # 注意：已移除对 monitoring_clients 的 FK（该表不存在）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                client_id INTEGER,
                brand_id INTEGER,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT,
                related_id INTEGER,
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    
        # ========== 新增：数据归档表（用于清除后恢复） ==========
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_data_archives (
                id SERIAL PRIMARY KEY,
                archive_key TEXT UNIQUE NOT NULL,
                brand_id INTEGER NOT NULL,
                quote_id INTEGER,
                operator TEXT,
                reason TEXT,
                task_ids TEXT,
                result_count INTEGER DEFAULT 0,
                trend_count INTEGER DEFAULT 0,
                archived_tasks TEXT,
                archived_results TEXT,
                archived_trends TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # CTO-15.23 2026-05-09 · 全量 LLM 调用日志(admin dashboard 数据源)
        # 覆盖 monitoring/autofill/竞品/写文章/advisor/fallback_chain/keyword_expand 等所有 callsite
        # 老板要求:跟 Moonshot/火山/阿里云/DeepSeek 控制台对账误差 < 5%
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_call_log (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                caller TEXT NOT NULL,
                platform TEXT NOT NULL,
                model TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cached_tokens INTEGER DEFAULT 0,
                estimated_cost NUMERIC(10,6) DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                brand_id INTEGER,
                quote_id INTEGER,
                user_id INTEGER,
                success BOOLEAN DEFAULT TRUE,
                error_msg TEXT,
                metadata JSONB
            )
        """)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_log_created_at ON llm_call_log(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_log_caller ON llm_call_log(caller)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_log_platform ON llm_call_log(platform)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_log_brand ON llm_call_log(brand_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_log_user ON llm_call_log(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_log_success ON llm_call_log(success) WHERE success=FALSE")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_log_caller_platform ON llm_call_log(caller, platform)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_log_created_caller ON llm_call_log(created_at, caller)")
        except Exception:
            pass

        # CTO-15.23 2026-05-09 · 代理自助监测订阅(消费一次扣一次模型)
        # · enable    创建订阅 · is_monitored=TRUE · 不扣分(只是订阅意向)
        # · daily 03:00 跑 · charge_on_success 每个 keyword 跑完即扣 130 积分
        # · 余额不足 → status='paused_low_balance' · is_monitored=FALSE
        # · 充值后 02:30 cron 检查 → 恢复 active + is_monitored=TRUE
        # · disable   status='cancelled' · 不退分(A 类完成才扣模型)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keyword_monitor_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                keyword_id INTEGER NOT NULL,
                quote_id INTEGER,
                brand_id INTEGER,
                -- [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-1] keyword_id 指向哪张表:
                --   confirmed_keywords 还是 extra_keywords(两表 id 各自独立)。
                --   虚拟新库靠这里建出来,存量库靠 036 迁移 ADD COLUMN,两条路同口径。
                keyword_source TEXT NOT NULL DEFAULT 'confirmed',
                -- 🔴 [R8 · Codex 2026-08-17] 上一版这里**零 billing_mode** ⇒ 全新库建出来的表
                --   缺这一列,而计费主体解析与建订阅 INSERT 都显式读写它 → UndefinedColumn。
                --   这正是 036 那颗 init-DDL 漂移雷的同型 —— 那次接住了,这次没有。
                --   ⇒ 建表 DDL 与迁移(037)必须同口径,由 test_r8_init_ddl_matches_migration 钉住。
                billing_mode TEXT NOT NULL DEFAULT 'brand_owner',
                status TEXT DEFAULT 'active',
                daily_points INTEGER DEFAULT 130,
                feature_code TEXT DEFAULT 'monitoring_keyword_daily',
                enabled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_charged_at TIMESTAMP,
                last_charge_amount INTEGER DEFAULT 0,
                total_charged INTEGER DEFAULT 0,
                paused_reason TEXT,
                paused_at TIMESTAMP,
                cancelled_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_kms_user_id ON keyword_monitor_subscriptions(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_kms_keyword_id ON keyword_monitor_subscriptions(keyword_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_kms_quote_id ON keyword_monitor_subscriptions(quote_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_kms_status ON keyword_monitor_subscriptions(status)")
            # [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-1b] 🔴 自愈 DDL 会撤销迁移
            #   病史(本轮由 P0-1 串词用例实测抓到,不是 review 看出来的):
            #   036 迁移 DROP 掉只按 keyword_id 的 uniq_kms_keyword_active,
            #   而这里 init_db 每次应用启动**无条件重建**它 → 迁移被静默撤销。
            #   引爆条件:两表 id 号段重叠后,给手动词建订阅会撞上同 id 合同词的订阅
            #   → UniqueViolation → P0-3 的"开开关"整条路径报错。
            #   ⇒ 自愈 DDL 必须与迁移同口径:带 keyword_source 维度。
            #   老库(还没跑 036、没有该列)走原来的写法 —— 不能直接跳过:
            #   跳过 = 一个唯一索引都没有,建订阅的并发兜底(UniqueViolation)随之失效。
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'keyword_monitor_subscriptions'
                   AND column_name = 'keyword_source'
            """)
            if cursor.fetchone():
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_kms_keyword_source_active
                    ON keyword_monitor_subscriptions(keyword_id, keyword_source)
                    WHERE status IN ('active', 'paused_low_balance')
                """)
                cursor.execute("DROP INDEX IF EXISTS uniq_kms_keyword_active")
            else:
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_kms_keyword_active
                    ON keyword_monitor_subscriptions(keyword_id)
                    WHERE status IN ('active', 'paused_low_balance')
                """)
        except Exception:
            pass

        # ========== 表迁移：为已有表添加新字段 ==========
        migration_columns = [
            ("monitoring_reports", "content", "TEXT"),
            ("monitoring_reports", "status", "TEXT DEFAULT 'draft'"),
            ("monitoring_reports", "reviewed_by", "TEXT"),
            ("monitoring_reports", "reviewed_at", "TIMESTAMP"),
            ("monitoring_reports", "sent_at", "TIMESTAMP"),
            # DDS Phase 0: 记录 full_response 原文长度（解除截断后追踪用）
            ("monitoring_results", "response_char_count", "INTEGER DEFAULT 0"),
            # DDS Phase 3: 保存 AI 搜索引用数据（JSON格式）
            ("monitoring_results", "search_citations", "TEXT"),
            # P1 竞品共现: 监测时 LLM 已抽取的共现品牌/公司列表(JSONB · 默认空 · 不混入 search_citations)
            ("monitoring_results", "competitors_mentioned", "JSONB DEFAULT '[]'::jsonb"),
            # GEO article v1.4 A9: monitoring_results is the sole business-fact SSOT.
            ("monitoring_results", "sent_question_snapshot", "TEXT"),
            ("monitoring_results", "keyword_source", "VARCHAR(32) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "keyword_type", "VARCHAR(32) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "question_family", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "question_family_version", "VARCHAR(80) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "keyword_source_id", "BIGINT"),
            ("monitoring_results", "keyword_resolver_status", "VARCHAR(32) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "provider", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "model", "VARCHAR(128) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "model_revision", "VARCHAR(128) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "model_revision_unknown_reason", "VARCHAR(64)"),
            ("monitoring_results", "surface", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "search_mode", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "response_status", "VARCHAR(32) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "target_brand_snapshot", "TEXT"),
            ("monitoring_results", "target_entity_snapshot", "TEXT"),
            ("monitoring_results", "target_outcome", "VARCHAR(40) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "outcome_resolver_version", "VARCHAR(80) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "outcome_resolver_confidence", "NUMERIC(5,4)"),
            ("monitoring_results", "lineage_version", "VARCHAR(80) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "lineage_status", "VARCHAR(32) DEFAULT 'legacy_unknown'"),
            ("monitoring_results", "lineage_error_reason", "TEXT"),
            ("monitoring_results", "provider_request_id", "TEXT"),
            ("monitoring_results", "sent_at", "TIMESTAMPTZ"),
            # ✅ brand_id 统一化迁移（为已有库补加 brand_id 列）
            ("extra_keywords", "brand_id", "INTEGER"),
            ("client_keywords", "brand_id", "INTEGER"),
            ("monitoring_tasks", "brand_id", "INTEGER"),
            ("monitoring_reports", "brand_id", "INTEGER"),
            ("monitoring_config", "brand_id", "INTEGER"),
            ("notifications", "brand_id", "INTEGER"),
            # 2026-06-26: 操作日志按客户隔离，避免同一代理名下不同品牌日志串台
            ("operation_logs", "brand_id", "INTEGER"),
            # 区分手动/自动监测 + 趋势同步状态
            ("monitoring_tasks", "trigger_type", "TEXT DEFAULT 'manual'"),
            ("monitoring_tasks", "trend_synced", "SMALLINT DEFAULT 0"),
            # 平台权重持久化（存储JSON: {weights, mau_data, source, updated_at}）
            ("monitoring_config", "config_value", "TEXT"),
            # P0.8 (CTO-15.9 2026-04-25): 关键词独立监测问题 · 解耦"{keyword}哪家好？推荐一下"硬拼接
            # 关键词意图分类 + 人工 override 后 · 这里存真实要发的 query · fallback build_question(keyword)
            ("confirmed_keywords", "monitoring_query", "TEXT"),
            ("extra_keywords", "monitoring_query", "TEXT"),
            ("extra_keywords", "archive_reason", "TEXT"),
            ("extra_keywords", "archived_at", "TIMESTAMP"),
            # [WO_MONITORING_OPTIN 2026-08-15 P0-A.1] 手动词的监测开关 · 默认关。
            #   与 scripts/migration_monitoring_extra_optin_2026_08_15.sql 同形(启动自检双保险 ·
            #   防 migration 未跑环境 SELECT ek.is_monitored 报 UndefinedColumn)。
            #   🔴 默认只能 FALSE:一加词就跑就扣是本单要消灭的形态,不许给 TRUE。
            ("extra_keywords", "is_monitored", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("extra_keywords", "monitoring_enabled_at", "TIMESTAMP"),
            ("extra_keywords", "monitoring_enabled_by", "INTEGER"),
            # B.6 (CTO-15.9 session 3 · 2026-04-25): 报告 2.0 schema
            # version='v1' 老 markdown · 'v2' M2 8 模块装配版
            # evidence_count: 报告含 response_snippet 数 · M2 KR 之一
            # modules_jsonb: v2 8 模块结构化数据(标题/内容/置信度/源/Evidence 级)
            ("monitoring_reports", "version", "TEXT DEFAULT 'v1'"),
            ("monitoring_reports", "evidence_count", "INTEGER DEFAULT 0"),
            ("monitoring_reports", "modules_jsonb", "JSONB"),
            # CTO-15.23 2026-05-09 · 代理自助监测订阅 关联字段
            ("confirmed_keywords", "monitoring_subscription_id", "INTEGER"),
            ("confirmed_keywords", "monitoring_enabled_at", "TIMESTAMP"),
        ]
    
        for table, column, col_type in migration_columns:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
                (table, column)
            )
            if not cursor.fetchone():
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                except Exception:
                    pass  # 并发竞态

        # 2026-04-29: quote 定时监测从 1/2/3 次每天升级为任意 N 小时间隔。
        # 这里不用 migration_columns 的 DEFAULT 直接加列，避免老数据全被默认 24 覆盖后无法按
        # monitoring_frequency=2/3 回填 12/8 小时。
        cursor.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            ("quotes", "monitoring_interval_hours"),
        )
        if not cursor.fetchone():
            try:
                cursor.execute("ALTER TABLE quotes ADD COLUMN monitoring_interval_hours INTEGER")
            except Exception:
                pass

        cursor.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            ("quotes", "monitoring_last_run_at"),
        )
        if not cursor.fetchone():
            try:
                cursor.execute("ALTER TABLE quotes ADD COLUMN monitoring_last_run_at TIMESTAMP")
            except Exception:
                pass

        try:
            cursor.execute("""
                UPDATE quotes
                SET monitoring_interval_hours = CASE monitoring_frequency
                    WHEN 1 THEN 24
                    WHEN 2 THEN 12
                    WHEN 3 THEN 8
                    ELSE 24
                END
                WHERE monitoring_interval_hours IS NULL
            """)
            cursor.execute("ALTER TABLE quotes ALTER COLUMN monitoring_interval_hours SET DEFAULT 24")
        except Exception:
            pass

        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_logs_brand ON operation_logs(brand_id)")
        except Exception:
            pass
    
        conn.close()
        print("[Monitoring DB] 监测表初始化完成")
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 监测配置操作
# ==========================================

def get_monitoring_config(
    brand_id: int = None,
    client_id: str = "_global_"
) -> Dict[str, Any]:
    """获取监测配置（优先 brand_id，fallback client_id）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 优先用 brand_id 查询
        if brand_id is not None:
            cursor.execute(
                "SELECT * FROM monitoring_config WHERE brand_id = %s",
                (brand_id,)
            )
            row = cursor.fetchone()
            if row:
                conn.close()
                return dict(row)

        # fallback: client_id 查询
        cursor.execute(
            "SELECT * FROM monitoring_config WHERE client_id = %s",
            (client_id,)
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            return dict(row)
        return {
            "client_id": client_id,
            "brand_id": brand_id,
            "default_concurrency": 10,
            "default_platforms": DEFAULT_MONITORING_PLATFORMS
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def update_monitoring_config(
    brand_id: int = None,
    client_id: str = "_global_",
    default_concurrency: int = None,
    default_platforms: str = None
) -> bool:
    """更新监测配置（双写 brand_id + client_id）"""
    _, client_id_compat = _resolve_id(brand_id, client_id)
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        updates = []
        params = []
    
        if default_concurrency is not None:
            updates.append("default_concurrency = %s")
            params.append(default_concurrency)

        if default_platforms is not None:
            updates.append("default_platforms = %s")
            params.append(default_platforms)

        if not updates:
            return False

        updates.append("updated_at = %s")
        params.append(datetime.now().isoformat())
        params.append(client_id_compat)

        cursor.execute(f"""
            INSERT INTO monitoring_config (client_id, brand_id, default_concurrency, default_platforms, updated_at)
            VALUES (%s, %s, 10, '{DEFAULT_MONITORING_PLATFORMS}', %s)
            ON CONFLICT(client_id) DO UPDATE SET {', '.join(updates[:-1])}, updated_at = excluded.updated_at
        """, (client_id_compat, brand_id, datetime.now().isoformat()) + tuple(params[:-1]))
    
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 客户词条操作
# ==========================================

def resolve_extra_keyword_platforms(
    *, brand_id: int = None, client_id: str = None
) -> Optional[str]:
    """手动词该继承的平台授权口径 —— 与合同词同源,不再抄产品默认。

    [R1 根因修 · 2026-08-04]
    原实现写 ``get_monitoring_config().default_platforms``。那是**产品默认**:
    ``extra_keywords.platforms`` 的列默认值在 2026-07-26 的 unified5 收口迁移里被推
    到五引擎,而 ``get_monitoring_config`` 在该 brand 没有配置行时也返回同一个五引擎
    常量。它和「这个词挂靠的合同到底买了什么」**毫无关系**。

    后果(生产实证):同一个 quote 386 下,8 条合同词按购买时的 classic4 矩阵跑、手动词
    id=23 按产品默认的五引擎跑;两者在 ``create_monitoring_run_cells`` 汇合时
    ``executable ⊄ entitlement`` → 整批炸(monitoring_tasks 1543/1544 双 failed,
    total_tests=29 = 6×4 + 1×5 正是这个混合矩阵的指纹)。

    新口径(与 ``get_keywords_for_monitoring`` 里 confirmed 词那条 JOIN 同表同键):
      1. 能核实归属的 quote → 读该 quote 已确认词的商品版本,到
         ``monitoring_product_platform_matrices`` 取矩阵,**逐字返回**(不重排不裁剪,
         这样新加的手动词和它的 quote 矩阵字面相等)。
         · 该 quote 上有多个商品版本(续单/混合)→ 取各矩阵**交集**。fail-closed:
           绝不让手动词拿到比任何一条合同词更多的平台。
         · 交集为空 → 返 None(交由调用方走 brand 口径),不写一个空授权。
      2. 该 quote 一条确认词都没有 / 无 quote / 核不实归属 → 返 None,调用方落
         brand 生效口径(代理自助订阅是合法场景,没有合同可继承)。

    🔴 quote 归属必须走 ``_resolve_delivery_quote_id`` 核实,不能直接信
    ``_resolve_id`` 吐出的 ``client_id``:该品牌没有 quote 时它会回落成
    ``str(brand_id)``,而 brand_id 与 quote_id 是两套自增序列、数值必然会撞 ——
    直接当 quote_id 用会**继承到别人 quote 的授权矩阵**。

    本函数只做「取口径」,拿不准一律返 None,绝不阻断加词。
    """
    verified_quote_id = _resolve_delivery_quote_id(
        client_id=client_id, brand_id=brand_id
    )
    if verified_quote_id is None:
        return None

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT m.version, m.platforms
              FROM public.confirmed_keywords k
              JOIN public.monitoring_product_platform_matrices m
                ON m.version = k.monitoring_product_version
             WHERE k.quote_id = %s
             ORDER BY m.version
            """,
            (int(verified_quote_id),),
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception as err:
        print(f"[Monitoring DB] 手动词授权口径解析失败(回落 brand 口径) quote={verified_quote_id}: {err}")
        return None
    finally:
        try:
            conn.close()
        except Exception: pass

    matrices = [str(row["platforms"] or "") for row in rows if row.get("platforms")]
    if not matrices:
        return None
    if len(matrices) == 1:
        # 单一商品版本(绝大多数):逐字返回,和合同词的矩阵字面相等。
        return matrices[0]

    # 多版本 → 交集,保留第一条矩阵的顺序(version ASC 已定序,结果确定)。
    common = None
    for raw in matrices:
        items = {item.strip().lower() for item in raw.split(",") if item.strip()}
        common = items if common is None else (common & items)
    if not common:
        return None
    first = [item.strip().lower() for item in matrices[0].split(",") if item.strip()]
    return ",".join([item for item in first if item in common])


def add_keyword(
    brand_id: int = None,
    client_id: str = None,
    keyword: str = "",
    target_brand: str = "",
    difficulty: str = "中等",
    target_rate: int = 60,
    platforms: str = None,
    monitoring_query: str = None,  # [CTO-15.9 Codex bug 4] P0.8 · 人工 override query
) -> int:
    """添加客户词条（写入extra_keywords表，双写 brand_id + client_id）

    P0.8(CTO-15.9 Codex bug 4 修):
      - 新增 monitoring_query 可选参数 · 写入 extra_keywords.monitoring_query
      - None 时不写 · 兼容老调用方 + 依靠 resolve_monitoring_query fallback build_question

    [R1 · 2026-08-04] platforms 缺省时先继承挂靠 quote 的授权矩阵(见
    ``resolve_extra_keyword_platforms``),继承不到才落 brand 生效口径。
    显式传入的 platforms 仍然原样尊重(调用方明示意图优先)。
    """
    resolved_brand_id, client_id_compat = _resolve_id(brand_id, client_id)

    # 先获取配置（避免嵌套连接导致database locked）
    if platforms is None:
        platforms = resolve_extra_keyword_platforms(
            brand_id=resolved_brand_id, client_id=client_id_compat
        )
    if platforms is None:
        config = get_monitoring_config(brand_id=resolved_brand_id, client_id=client_id_compat)
        platforms = config.get("default_platforms", DEFAULT_MONITORING_PLATFORMS)

    # 然后再打开连接写入数据
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # quote_id: 兼容期从 client_id 解析
        quote_id = int(client_id_compat) if client_id_compat and client_id_compat.isdigit() else None

        # 写入extra_keywords表（双写 brand_id + P0.8 monitoring_query）
        cursor.execute("""
            INSERT INTO extra_keywords
            (quote_id, client_id, brand_id, keyword, target_brand, difficulty, target_rate,
             platforms, status, monitoring_query)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
            RETURNING id
        """, (quote_id, client_id_compat, resolved_brand_id, keyword, target_brand, difficulty,
              target_rate, platforms,
              (monitoring_query or None)))

        keyword_id = cursor.fetchone()["id"]

        # CTO-15.23 2026-05-25 · 老板拍 C 全干 · 监测自定义词回流 brands.seed_keywords 全局池
        # · 同 brand 新建 campaign / 诊断 / 写作可读 brand.seed_keywords 预填(后续消费侧改造)
        # · 去重(name strip)· 不覆盖现有 · merge 模式 · 失败不阻塞主流程
        # · brands.seed_keywords JSONB 字段(diagnosis_db.py:178 _safe_add_column 实证)
        if resolved_brand_id and keyword and keyword.strip():
            try:
                cursor.execute("SELECT seed_keywords FROM brands WHERE id = %s", (resolved_brand_id,))
                _brand_row = cursor.fetchone()
                _seed_list = []
                if _brand_row and _brand_row.get("seed_keywords"):
                    _seed_raw = _brand_row["seed_keywords"]
                    if isinstance(_seed_raw, str):
                        try:
                            import json as _json_lib
                            _seed_list = _json_lib.loads(_seed_raw) or []
                        except Exception:
                            _seed_list = []
                    elif isinstance(_seed_raw, list):
                        _seed_list = list(_seed_raw)
                _kw_stripped = keyword.strip()
                _existing_set = {
                    (s.strip() if isinstance(s, str) else (s.get("keyword", "").strip() if isinstance(s, dict) else ""))
                    for s in _seed_list
                }
                if _kw_stripped not in _existing_set:
                    _seed_list.append(_kw_stripped)
                    import json as _json_lib2
                    cursor.execute(
                        "UPDATE brands SET seed_keywords = %s WHERE id = %s",
                        (_json_lib2.dumps(_seed_list, ensure_ascii=False), resolved_brand_id),
                    )
            except Exception as _seed_err:
                print(f"[Monitoring DB] seed_keywords 回流失败(非阻塞) brand={resolved_brand_id}: {_seed_err}")

        conn.commit()
        conn.close()
        return keyword_id
    finally:
        try:
            conn.close()
        except Exception: pass


def batch_add_keywords(
    brand_id: int = None,
    client_id: str = None,
    keywords: List[Dict[str, Any]] = None
) -> int:
    """批量添加词条"""
    if keywords is None:
        keywords = []
    count = 0
    for kw in keywords:
        try:
            add_keyword(
                brand_id=brand_id,
                client_id=client_id,
                keyword=kw.get("keyword", ""),
                target_brand=kw.get("target_brand", ""),
                difficulty=kw.get("difficulty", "中等"),
                target_rate=kw.get("target_rate", 60),
                platforms=kw.get("platforms"),
                monitoring_query=kw.get("monitoring_query"),  # P0.8 CTO-15.9 bug 3 二审修
            )
            count += 1
        except Exception as e:
            print(f"[Monitoring DB] 添加词条失败: {e}")
    return count


def get_keywords(
    brand_id: int = None,
    client_id: str = None,
    status: str = "active"
) -> List[Dict[str, Any]]:
    """获取客户词条列表（优先 brand_id 查询）

    FIX GEO-BUG-3: add_keyword() writes to extra_keywords, but this function
    was only reading client_keywords. Now queries both tables with UNION ALL.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Shared column list for UNION (both tables have these columns)
        _cols = "id, client_id, brand_id, keyword, target_brand, difficulty, target_rate, platforms, note, created_at, status"

        # 优先用 brand_id 查询 — UNION both tables to catch keywords from either
        if brand_id is not None:
            if status:
                cursor.execute(f"""
                    SELECT {_cols} FROM client_keywords WHERE brand_id = %s AND status = %s
                    UNION ALL
                    SELECT {_cols} FROM extra_keywords WHERE brand_id = %s AND status = %s
                    ORDER BY created_at DESC
                """, (brand_id, status, brand_id, status))
            else:
                cursor.execute(f"""
                    SELECT {_cols} FROM client_keywords WHERE brand_id = %s
                    UNION ALL
                    SELECT {_cols} FROM extra_keywords WHERE brand_id = %s
                    ORDER BY created_at DESC
                """, (brand_id, brand_id))
        elif client_id is not None:
            # fallback: 旧路径
            if status:
                cursor.execute(f"""
                    SELECT {_cols} FROM client_keywords WHERE client_id = %s AND status = %s
                    UNION ALL
                    SELECT {_cols} FROM extra_keywords WHERE client_id = %s AND status = %s
                    ORDER BY created_at DESC
                """, (client_id, status, client_id, status))
            else:
                cursor.execute(f"""
                    SELECT {_cols} FROM client_keywords WHERE client_id = %s
                    UNION ALL
                    SELECT {_cols} FROM extra_keywords WHERE client_id = %s
                    ORDER BY created_at DESC
                """, (client_id, client_id))
        else:
            conn.close()
            return []

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_keyword_by_id(keyword_id: int) -> Optional[Dict[str, Any]]:
    """获取单个词条（查 client_keywords，未找到则 fallback 到 extra_keywords）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM client_keywords WHERE id = %s", (keyword_id,))
        row = cursor.fetchone()
        if not row:
            # FIX GEO-BUG-3: add_keyword writes to extra_keywords, fallback query
            cursor.execute("SELECT * FROM extra_keywords WHERE id = %s", (keyword_id,))
            row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def update_keyword_status(keyword_id: int, status: str, source: str = "client") -> bool:
    """更新词条状态

    [A-5-2 安全修 2026-05-29] 加 source · 必须更新与上游 RBAC 校验同一张表。
    原写死 client_keywords:当上游按 source='extra' 校验 extra_keywords 通过后仍只改 client_keywords,
    若两表 id 碰撞即"用自己 extra 词鉴权改别人 client 词状态"(跨表 IDOR)。
    source=='extra' → extra_keywords · 否则 client_keywords。
    """
    tbl = "extra_keywords" if source == "extra" else "client_keywords"
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE {tbl} SET status = %s WHERE id = %s",
            (status, keyword_id)
        )
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_keyword(keyword_id: int) -> bool:
    """删除词条（尝试 client_keywords，未找到则尝试 extra_keywords）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM client_keywords WHERE id = %s", (keyword_id,))
        affected = cursor.rowcount
        if affected == 0:
            # FIX GEO-BUG-3: add_keyword writes to extra_keywords, fallback delete
            cursor.execute("DELETE FROM extra_keywords WHERE id = %s", (keyword_id,))
            affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 监测任务操作
# ==========================================

def create_monitoring_task(
    brand_id: int = None,
    client_id: str = None,
    keyword_ids: List[int] = None,
    concurrency: int = None,
    task_name: str = None,
    trigger_type: str = "unknown",
    planned_test_count: int = None,
    planned_platform_count: int = None,
) -> int:
    """创建监测任务（双写 brand_id + client_id）

    Args:
        trigger_type: 显式分类 · 不留 default · 防漏归类(2026-05-07 P0-2)
            - 'manual_user'   · 用户在监测中心手点 /api/monitoring/run
            - 'manual_sse'    · 前端 SSE 启动 /api/monitoring/run-stream
            - 'scheduled'     · scheduler.py cron 自动跑
            - 'auto_publish'  · _m1a_auto_monitor_after_publish 24h 自动监测
            - 'unknown'       · 没显式传 · 触发 logger.warning 排查调用源
        Deploy-CTO 调查发现 prod 30 天 manual=98% scheduled=2% · 但有规律 06:04/07:50
        触发 不像用户手点 · 嫌疑某 cron/SDK/前端 polling 调入口未传 trigger_type
    """
    if trigger_type == "unknown":
        import logging as _lg
        _lg.getLogger("GEO-Monitoring").warning(
            f"[create_monitoring_task] trigger_type=unknown · brand={brand_id} client={client_id} "
            f"调用栈未显式分类 · 见 TODO 排查"
        )
    resolved_brand_id, client_id_compat = _resolve_id(brand_id, client_id)

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 获取配置
        if concurrency is None:
            config = get_monitoring_config(brand_id=resolved_brand_id, client_id=client_id_compat)
            concurrency = config.get("default_concurrency", 10)

        # 获取词条数量
        if keyword_ids:
            keyword_count = len(keyword_ids)
        else:
            keywords = get_keywords(brand_id=resolved_brand_id, client_id=client_id_compat)
            keyword_count = len(keywords)

        if planned_test_count is None:
            raise ValueError("planned_test_count is required for monitoring task creation")
        total_tests = int(planned_test_count)
        if total_tests <= 0:
            raise ValueError("planned_test_count must be positive")
        platform_count = int(planned_platform_count or 0)
        if platform_count <= 0:
            raise ValueError("planned_platform_count must be positive")

        if not task_name:
            task_name = f"监测任务_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        cursor.execute("""
            INSERT INTO monitoring_tasks
            (client_id, brand_id, task_name, keyword_count, platform_count, total_tests, concurrency, status, trigger_type, trend_synced)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, 0)
            RETURNING id
        """, (client_id_compat, resolved_brand_id, task_name, keyword_count, platform_count, total_tests, concurrency, trigger_type))

        task_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return task_id
    finally:
        try:
            conn.close()
        except Exception: pass


# ══════════════════════════════════════════════════════════════════════
# 完成信号必须锚**结果**(WO_224-c1 §3.1 · Deploy 224-d1 实证)
# ══════════════════════════════════════════════════════════════════════
# 🔴 生产实证:**47 个 task(14.4%)零结果行,却全部 `status='completed'`**,
#    且各有 4 个 platform 格子。任何锚在「派了几个引擎」或任务状态字的判据
#    都判它们健康 —— 而 `monitoring_results` 里一行都没有。
#    这是本仓记过的第十一件同形:**完成信号由做事方发出**(建了格子 = 我干过了),
#    而真判据必须取自**被服务方**(结果落库了没有)。
#
# 🔴 引擎集合**不取任何常量**。仓里有三个今天数值都等于 4 的常量,语义三分:
#      `MONITORING_ENGINES`            = 授权面(unified5 = **5**,含元宝)
#      `MONITORING_RUN_CELL_PLATFORMS` = 执行面(DB CHECK 钉死 = 4)
#      `engine_contract.PLATFORM_CONTRACT` = provider/model 合同(= 4)
#    `config/ai_engines.py:66-75` 自己就写着「数值恰好相同掩盖了语义错位」。
#    拿其中任何一个当「这单派了几个引擎」,unified5 的单少落元宝都会被判健康。
#    ⇒ 取**这单自己记的**:四条建 task 的路径都写 `planned_platform_count`;
#      有派发台账(`monitoring_run_cells`)的路径再比**集合**,更严。

def _completion_evidence(cursor, task_id: int) -> dict:
    """完成准入的证据。全部读被服务方,不读派发计数器、不读日志。

    返回 `{ok, reason, seen_platforms, missing_platforms, expected, source}`。
    · ① `monitoring_results` 行数 > 0 —— 覆盖**全部四条**完成路径;
    · ② 有 `monitoring_run_cells` ⇒ 比**集合**:每个 `is_planned` 的平台名
         都要在结果里出现(能说出**缺哪个引擎**,不只是缺几个);
    · ③ 没格子(batch_monitor 那条路径不建格子)⇒ 退到**计数**:
         distinct platform ≥ 该 task 自记的 `platform_count`。

    🔴 ③ 里 `platform_count <= 0` 时这条退化成恒真。**必须出声**,
       不能静默放行 —— 否则「宽度没记下来」和「宽度满足了」在数据上分不开。
    """
    out = {
        "ok": False, "reason": "", "seen_platforms": [],
        "missing_platforms": [], "expected": None, "source": "",
    }
    cursor.execute(
        "SELECT platform, COUNT(*) AS n FROM monitoring_results "
        " WHERE task_id = %s GROUP BY platform", (int(task_id),))
    rows = [dict(r) for r in (cursor.fetchall() or [])]
    seen = sorted({str(r.get("platform")) for r in rows if r.get("platform")})
    out["seen_platforms"] = seen
    if not rows:
        out["reason"] = "no_result_rows"
        return out

    # 派发台账优先:它记着**这一单实际派了哪些平台**
    cursor.execute(
        "SELECT DISTINCT platform FROM monitoring_run_cells "
        " WHERE task_id = %s AND is_planned IS TRUE", (int(task_id),))
    planned = sorted({
        str(dict(r).get("platform")) for r in (cursor.fetchall() or [])
        if dict(r).get("platform")})
    if planned:
        out["source"] = "run_cells"
        out["expected"] = planned
        missing = [p for p in planned if p not in set(seen)]
        out["missing_platforms"] = missing
        if missing:
            out["reason"] = "missing_planned_platforms"
            return out
        out["ok"] = True
        return out

    # 没格子 ⇒ 退到这单自记的派发宽度
    cursor.execute("SELECT platform_count FROM monitoring_tasks WHERE id = %s",
                   (int(task_id),))
    row = cursor.fetchone()
    width = int((dict(row) if row else {}).get("platform_count") or 0)
    out["source"] = "task_platform_count"
    out["expected"] = width
    if width <= 0:
        # 🔴 出声:宽度没记下来,这一档等于没判。放行,但留痕。
        out["ok"] = True
        out["reason"] = "width_unknown"
        import logging as _lg
        _lg.getLogger("GEO-Monitoring").warning(
            "[monitoring] task=%s 没有派发台账、platform_count 也没记(=%s),"
            "完成准入的宽度判据这一档**没生效**;只校了「结果非空」。",
            task_id, width)
        return out
    if len(seen) < width:
        out["reason"] = "fewer_platforms_than_dispatched"
        return out
    out["ok"] = True
    return out


def update_task_status(
    task_id: int,
    status: str,
    completed_tests: int = None,
    result_summary: dict = None
) -> bool:
    """更新任务状态"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        updates = ["status = %s"]
        params = [status]

        if status == "running":
            updates.append("started_at = %s")
            params.append(datetime.now().isoformat())

        if status in ["completed", "failed"]:
            updates.append("completed_at = %s")
            params.append(datetime.now().isoformat())

        if completed_tests is not None:
            updates.append("completed_tests = %s")
            params.append(completed_tests)

        if result_summary is not None:
            updates.append("result_summary = %s")
            params.append(json.dumps(result_summary, ensure_ascii=False))

        # ── [WO_224-c1 §3.1] 完成准入:锚结果,不锚「派了几个引擎」──────
        # 🔴 放在**这里**(库级单点)而不是四个调用点:`completed` 由
        #    api/monitoring_api(×2)· api/scheduler · tools/monitoring/batch_monitor
        #    四处写,逐处加等于四份口径,将来第五个调用点还会漏。
        #    本函数是 `monitoring_tasks.status` 的唯一写入口。
        # 🔴 零结果落**现役枚举里的 `failed`**,不新造状态字:
        #    生产在写的只有 pending/running/completed/failed/rolled_back 五个
        #    (机械枚举 update_task_status 的调用点 + 两处直写)。
        # 🔴 **不回改历史 47 单**:本闸只在有人试图写 completed 时起作用,
        #    不扫库、不改旧行(旧账留给对账;真要回改属不可逆生产 → Owner)。
        _completion = None
        if status == "completed":
            try:
                _completion = _completion_evidence(cursor, task_id)
            except Exception as _ce:  # noqa: BLE001
                # 🔴 读不出证据**不放行**也不静默:准入是 fail-closed 的一侧。
                #    读不到就当没证据 —— 与「证明它完成了」相反的那一侧才安全。
                _completion = {"ok": False, "reason": "evidence_unavailable:%s"
                               % type(_ce).__name__,
                               "seen_platforms": [], "missing_platforms": [],
                               "expected": None, "source": "error"}
            if not _completion.get("ok"):
                import logging as _lg
                _lg.getLogger("GEO-Monitoring").warning(
                    "[monitoring] task=%s 请求置 completed,但结果侧不支持(%s;"
                    "已落 %s;缺 %s);按现役枚举落 failed。",
                    task_id, _completion.get("reason"),
                    _completion.get("seen_platforms"),
                    _completion.get("missing_platforms"))
                status = "failed"
                params[0] = status
                # 🔴 把**缺哪个引擎**写进任务行,不只是「缺几个」——
                #    每班 after 查要能直接读出是哪条引擎没落,而不是再去翻日志。
                #    `updates` 与 `params` 在 `params.append(task_id)` 之前是逐项对齐的,
                #    所以已经 append 过 result_summary 时**就地覆盖**那一项,不再 append 第二条。
                _merged = dict(result_summary) if isinstance(result_summary, dict) else {}
                _merged["completion_blocked"] = {
                    "reason": _completion.get("reason"),
                    "seen_platforms": _completion.get("seen_platforms"),
                    "missing_platforms": _completion.get("missing_platforms"),
                    "expected": _completion.get("expected"),
                    "expected_source": _completion.get("source"),
                }
                _blob = json.dumps(_merged, ensure_ascii=False)
                try:
                    _idx = updates.index("result_summary = %s")
                    params[_idx] = _blob
                except ValueError:
                    updates.append("result_summary = %s")
                    params.append(_blob)

        params.append(task_id)

        # [工单 2026-07-29 T3 §3.4 根因锁 · 状态机不变量]
        # 已到终态 completed 的任务**不许**再被写成 failed。
        # 这是 MONITOR-1507「11:37:24 已完成 → 11:37:34 未完成」的写入侧闸门:
        # 调用侧(api/monitoring_api SSE except 分支)已按 `_sse_completed` 修过根因,
        # 这里再补一道库级不变量,免得将来又有新调用点在终态之后补刀。
        # 反向(failed → completed)不拦:重试跑成功理应能改回来。
        # 现有调用点全部是"先 failed 后 completed"或已带守卫,零行为变化。
        guard_sql = ""
        if status == "failed":
            guard_sql = " AND status IS DISTINCT FROM 'completed'"

        cursor.execute(
            f"UPDATE monitoring_tasks SET {', '.join(updates)} WHERE id = %s{guard_sql} "
            "RETURNING id,brand_id,quote_id,client_id,total_tests,completed_tests,"
            "completed_at,trigger_type",
            tuple(params)
        )
        changed_row = cursor.fetchone()
        if changed_row is None and status == "failed":
            # [Deploy-CTO 2026-07-29 部署前小修] 守卫**不许无声**。
            #
            # 本包修的就是静默失败;守卫默默吞掉一次 completed→failed,等于用一个新的
            # 静默失败替换旧的 —— 而且更难查,因为它长得像"正常工作"。
            # 所以拦截必须留痕,且必须带够定位信息:
            #   task_id · 尝试写入的状态 · 当前实际状态 · 调用点
            #
            # 🔴 先把"当前实际状态"读出来再判:UPDATE 没命中有两种可能 ——
            #    ①被终态守卫拦下(actual='completed'),②task_id 根本不存在。
            #    不分清就把②也记成"被守卫拦下",是在制造假线索。
            import logging as _lg
            import traceback as _tb

            cursor.execute("SELECT status FROM monitoring_tasks WHERE id = %s", (task_id,))
            _cur_row = cursor.fetchone()
            _actual = (_cur_row or {}).get("status") if _cur_row else None
            # 调用点取本函数之上那一帧(倒数第二帧即调用者)。
            _stack = _tb.extract_stack()
            _caller = "<unknown>"
            if len(_stack) >= 2:
                _f = _stack[-2]
                _caller = f"{_f.filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:{_f.lineno}:{_f.name}"
            _log = _lg.getLogger("GEO-Monitoring")
            if _actual == "completed":
                _log.warning(
                    f"[MonitoringTask][终态守卫拦截] 已完成的监测任务被尝试降级为 failed,已拒绝写入 · "
                    f"task_id={task_id} · 尝试写入状态=failed · 当前实际状态={_actual} · 调用点={_caller} "
                    f"· 任务状态未被改动(仍为 completed)· 工单 2026-07-29 T3 §3.4"
                )
            elif _cur_row is None:
                # 🔴 这条**不许**出现"终态守卫拦截"字样(哪怕是"不是…拦截"这种否定说法)——
                #    运维 grep 日志找拦截记录时会误命中,把"任务不存在"当成"被守卫拦下"去查。
                _log.warning(
                    f"[MonitoringTask] update_task_status 未命中任何行:task_id={task_id} "
                    f"在 monitoring_tasks 中不存在 · 尝试写入状态=failed · 调用点={_caller}"
                )
            else:
                _log.warning(
                    f"[MonitoringTask] update_task_status 未命中任何行 · task_id={task_id} · "
                    f"尝试写入状态=failed · 当前实际状态={_actual} · 调用点={_caller}"
                )
        if changed_row and status in ("completed", "failed") and changed_row.get("brand_id") is not None:
            cursor.execute("SELECT owner_user_id FROM brands WHERE id=%s", (changed_row["brand_id"],))
            owner_row = cursor.fetchone()
            if owner_row and owner_row.get("owner_user_id") is not None:
                from services.notification_events import NotificationEventType, RecipientKind
                from services.notification_outbox import (
                    enqueue_admin_notification_events,
                    enqueue_notification_event,
                )

                is_partial = (
                    status == "completed"
                    and int(changed_row.get("completed_tests") or 0)
                    < int(changed_row.get("total_tests") or 0)
                )
                if status == "failed":
                    event_type = NotificationEventType.MONITORING_FAILED
                    terminal_state = "failed"
                    status_text = "监测未完成"
                elif is_partial:
                    event_type = NotificationEventType.MONITORING_PARTIAL
                    terminal_state = "partial_success"
                    status_text = "监测部分完成"
                else:
                    event_type = NotificationEventType.MONITORING_COMPLETED
                    terminal_state = "completed"
                    status_text = "监测已完成"
                terminal_at = changed_row.get("completed_at") or datetime.now()
                facts = {
                    "business_no": f"MONITOR-{task_id}",
                    "status": status_text,
                    "occurred_at": terminal_at.isoformat(timespec="seconds"),
                    "summary": "请在效果监测页面查看结果。",
                }
                enqueue_notification_event(
                    cursor,
                    event_type=event_type,
                    business_id=str(task_id),
                    terminal_state=terminal_state,
                    recipient_user_id=int(owner_row["owner_user_id"]),
                    recipient_kind=RecipientKind.USER,
                    facts=facts,
                )
                if status == "failed" and changed_row.get("trigger_type") in {"scheduled", "auto_publish"}:
                    enqueue_admin_notification_events(
                        cursor,
                        event_type=event_type,
                        business_id=str(task_id),
                        terminal_state=terminal_state,
                        facts=facts,
                    )
        conn.commit()
        affected = int(changed_row is not None)
        conn.close()

        # [工单 2026-07-29 T1] 交付成功事件 ①「监测轮次完成」→ 客户门户 token 续期。
        # 放在 commit + close 之后:独立连接、独立事务,续期失败不回滚监测终态
        # (renew_client_token_safe 内部吞异常 · 工单 §1.3.5)。
        if changed_row is not None and status == "completed":
            _renew_quote_id = _resolve_delivery_quote_id(
                quote_id=changed_row.get("quote_id"),
                client_id=changed_row.get("client_id"),
                brand_id=changed_row.get("brand_id"),
            )
            renew_client_token_safe(
                _renew_quote_id,
                trigger="monitoring_round_completed",
                reason=f"监测轮次完成自动续期 task_id={task_id}",
            )
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def _resolve_delivery_quote_id(
    *, quote_id=None, client_id=None, brand_id=None
) -> Optional[int]:
    """把交付事件上的 id 收敛成**确定归属该品牌**的 quote_id,拿不准就返 None。

    🔴 client_id 在本仓是「quote_id 的字符串兼容位」,但 _resolve_id 在该品牌没有
    quote 时会回落成 str(brand_id) —— 直接拿它当 quote_id 会**串到别人的 quote**
    (brand_id 与 quote_id 是两套自增序列,数值必然会撞)。所以只认能被
    `quotes.id=? AND quotes.brand_id=?` 反证过的值;证不了就不续期(fail-closed)。
    """
    candidates = []
    for raw in (quote_id, client_id):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in candidates:
            candidates.append(value)
    if not candidates:
        return None
    if brand_id is None:
        # 无品牌可核时只信 quotes 表里真实存在的 quote_id 列(FK 已保证)。
        return candidates[0] if quote_id is not None else None
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM quotes WHERE id = ANY(%s) AND brand_id = %s ORDER BY id LIMIT 1",
            (candidates, int(brand_id)),
        )
        row = cursor.fetchone()
        conn.close()
        return int(row["id"]) if row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    """获取任务详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitoring_tasks WHERE id = %s", (task_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            task = dict(row)
            if task.get("result_summary"):
                try:
                    task["result_summary"] = json.loads(task["result_summary"])
                except:
                    pass
            return task
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_tasks(
    brand_id: int = None,
    client_id: str = None,
    limit: int = 20,
    before_created_at=None,
    before_id: int = None,
) -> List[Dict[str, Any]]:
    """获取任务列表（优先 brand_id 查询）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        conditions = []
        params = []
        if brand_id is not None:
            conditions.append("brand_id=%s")
            params.append(brand_id)
        elif client_id is not None:
            conditions.append("client_id=%s")
            params.append(client_id)
        if before_created_at is not None:
            if before_id is None:
                raise ValueError("monitoring task keyset id is required")
            conditions.append("(created_at,id) < (%s,%s)")
            params.extend((before_created_at, int(before_id)))
        where_sql = " WHERE " + " AND ".join(conditions) if conditions else ""
        cursor.execute(
            f"SELECT * FROM monitoring_tasks{where_sql} "
            "ORDER BY created_at DESC,id DESC LIMIT %s",
            (*params, max(1, min(int(limit), 200))),
        )

        rows = cursor.fetchall()
        conn.close()

        tasks = []
        for row in rows:
            task = dict(row)
            if task.get("result_summary"):
                try:
                    task["result_summary"] = json.loads(task["result_summary"])
                except:
                    pass
            tasks.append(task)
        return tasks
    finally:
        try:
            conn.close()
        except Exception: pass


class MonitoringCellConflict(RuntimeError):
    pass


class MonitoringCellNotFound(LookupError):
    pass


def _monitoring_snapshot_value(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _monitoring_plan_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_monitoring_snapshot_value,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_monitoring_run_cells(
    *,
    task_id: int,
    brand_id: int,
    keywords: List[Dict[str, Any]],
    search_mode: str,
    fulfillment_credential: str,
    fulfillment_state: str,
    retry_coverage: Optional[Dict[str, Any]] = None,
    settlement_reference: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Persist the complete classic-four matrix before any provider starts.

    Missing/unentitled platforms are explicit unavailable cells. Executable cells
    retain immutable entitlement, order and question snapshots for covered retry.
    """
    from psycopg2.extras import Json
    from tools.monitoring.batch_monitor import resolve_monitoring_query

    credential = str(uuid.UUID(str(fulfillment_credential)))
    normalized_settlement_reference = (
        str(settlement_reference).strip()[:160] if settlement_reference else None
    )
    if settlement_reference and not normalized_settlement_reference:
        raise ValueError("invalid monitoring settlement reference")
    if fulfillment_state not in {"reserved", "covered", "admin_covered"}:
        raise ValueError("invalid initial monitoring fulfillment state")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, brand_id, total_tests FROM public.monitoring_tasks WHERE id=%s FOR UPDATE",
            (int(task_id),),
        )
        task = cur.fetchone()
        if not task or int(task.get("brand_id") or 0) != int(brand_id):
            raise MonitoringCellNotFound("monitoring task scope not found")

        # [工单 E3-1 · 2026-08-26 · 迁移 054] 建格这一刻把租户归属**冻**在格上。
        #
        # 为什么冻而不是每次现读:`brands.owner_user_id` 是可变列。品牌在
        # 「跑这一轮」与「收口这一轮」之间被转移(真实业务动作),现读就会让
        # 同一次运行的历史归属跟着改 —— attempt 账本回答的是"这次运行是谁的",
        # 那件事在运行开始时就已经确定了。
        #
        # 为什么不是 `monitoring_keyword_settlements.billing_user_id`:那一列
        # 回答的是"谁付钱",admin_covered(平台垫付)时恒 NULL,而租户仍然是
        # 品牌所有者。身份只决定进哪个钱包,不决定归属。
        #
        # 🔴 NULL 放行:品牌确实可能没有 owner(`brands.owner_user_id` 可空)。
        #    这里**不**编一个 0 —— 0 号用户不存在,编出来的租户比缺失贵得多。
        #    落 NULL 之后账本侧会拒绝落账并告警(转人工),现役监测照跑
        #    (§13.1「监测老链一行不改语义」)。
        cur.execute(
            "SELECT owner_user_id FROM public.brands WHERE id=%s", (int(brand_id),))
        _owner_row = cur.fetchone()
        _raw_owner = (_owner_row or {}).get("owner_user_id") if _owner_row else None
        frozen_tenant_owner = int(_raw_owner) if _raw_owner else None
        if frozen_tenant_owner is not None and frozen_tenant_owner <= 0:
            frozen_tenant_owner = None
        if frozen_tenant_owner is None:
            import logging as _logging

            _logging.getLogger("GEO-Monitoring").error(
                "[monitoring-cells] brand=%s 没有 owner_user_id —— 本批格的租户归属"
                "落 NULL(**不编 0**);attempt 账本会拒绝落账并告警",
                brand_id)

        quote_ids = sorted({
            int(keyword["quote_id"])
            for keyword in keywords
            if keyword.get("quote_id") is not None
        })
        quote_snapshots: Dict[int, Dict[str, Any]] = {}
        if quote_ids:
            cur.execute(
                """
                SELECT q.id, pg_catalog.to_jsonb(q) AS snapshot
                  FROM public.quotes q
                 WHERE id = ANY(%s) AND q.brand_id=%s
                """,
                (quote_ids, int(brand_id)),
            )
            for row in cur.fetchall():
                source_snapshot = dict(row.get("snapshot") or {})
                permitted_fields = (
                    "id", "brand_id", "status", "service_status", "service_start_date",
                    "paid_at", "service_days", "confirmed_at", "created_at",
                )
                quote_snapshots[int(row["id"])] = {
                    "schema_version": "monitoring-order-snapshot-v1",
                    **{
                        key: _monitoring_snapshot_value(source_snapshot.get(key))
                        for key in permitted_fields if key in source_snapshot
                    },
                }
        missing_quotes = set(quote_ids) - set(quote_snapshots)
        if missing_quotes:
            raise MonitoringCellConflict("monitoring order snapshot is incomplete")

        cells: List[Dict[str, Any]] = []
        executable_count = 0
        for keyword in keywords:
            source = str(keyword.get("source") or "").strip().lower()
            if source not in {"confirmed", "contract", "extra"}:
                raise ValueError("monitoring keyword source is not retry-safe")
            keyword_id = int(keyword["id"])
            quote_id = int(keyword["quote_id"]) if keyword.get("quote_id") is not None else None
            if source in {"confirmed", "contract"}:
                if quote_id is None:
                    raise MonitoringCellConflict("confirmed monitoring keyword requires an order")
                cur.execute(
                    "SELECT keyword FROM public.confirmed_keywords WHERE id=%s AND quote_id=%s",
                    (keyword_id, quote_id),
                )
                keyword_authority = cur.fetchone()
            else:
                cur.execute(
                    """
                    SELECT keyword, quote_id, brand_id
                     FROM public.extra_keywords
                     WHERE id=%s
                       AND ((%s IS NOT NULL AND quote_id=%s
                             AND (brand_id IS NULL OR brand_id=%s))
                            OR (%s IS NULL AND quote_id IS NULL AND brand_id=%s))
                    """,
                    (
                        keyword_id, quote_id, quote_id, int(brand_id),
                        quote_id, int(brand_id),
                    ),
                )
                keyword_authority = cur.fetchone()
            if (
                not keyword_authority
                or str(keyword_authority.get("keyword") or "")
                != str(keyword.get("keyword") or "")
            ):
                raise MonitoringCellConflict(
                    "monitoring keyword does not belong to the order and brand scope"
                )
            entitlement = []
            raw_entitlement = keyword.get("entitlement_platforms") or []
            if isinstance(raw_entitlement, str):
                raw_entitlement = raw_entitlement.split(",")
            for raw in raw_entitlement:
                platform = str(raw).strip().lower()
                if platform in MONITORING_RUN_CELL_PLATFORMS and platform not in entitlement:
                    entitlement.append(platform)
            executable = set(keyword.get("_eligible_monitoring_platforms") or [])
            # 守卫保留 fail-closed(资金/授权闸,不许放宽)。2026-08-04 之后它在正常
            # 路径上永远不该触发:裁 eligible 的 PlatformAdapter 与这里读的是同一个
            # MONITORING_RUN_CELL_PLATFORMS,所以 executable 天然 ⊆ entitlement。
            # 真触发了 = 有第三个地方在自己造平台清单 → 错误必须点名到「哪个词的哪个
            # 平台」,否则排查只能拿到一句笼统文案(1543/1544 当时就是这样)。
            excess = sorted(executable - set(entitlement))
            if excess:
                raise MonitoringCellConflict(
                    "runtime plan exceeds purchased entitlement snapshot: "
                    f"keyword_id={keyword_id} source={source} "
                    f"keyword={str(keyword.get('keyword') or '')[:40]!r} "
                    f"excess_platforms={','.join(excess)} "
                    f"entitlement={','.join(entitlement) or '-'}"
                )

            keyword_credential = str(uuid.UUID(str(
                keyword.get("_fulfillment_credential") or credential
            )))
            keyword_settlement_reference = (
                str(keyword.get("_settlement_reference") or normalized_settlement_reference or "")
                .strip()[:160]
                or None
            )
            keyword_fulfillment_state = str(
                keyword.get("_fulfillment_state") or fulfillment_state
            )
            if keyword_fulfillment_state not in {"reserved", "covered", "admin_covered"}:
                raise ValueError("invalid keyword monitoring fulfillment state")
            raw_retry_coverage = keyword.get("_retry_coverage") or retry_coverage or {}
            retry_policy = {
                "policy_version": str(
                    raw_retry_coverage.get("policy_version") or "monitoring-retry-v1"
                ),
                "coverage": str(raw_retry_coverage.get("coverage") or "unknown"),
                "max_attempts": int(raw_retry_coverage.get("max_attempts") or 0),
            }
            if (
                retry_policy["coverage"] not in {"included", "requires_new_charge", "unknown"}
                or retry_policy["max_attempts"] < 0
                or retry_policy["max_attempts"] > 10
            ):
                raise ValueError("invalid monitoring retry coverage snapshot")
            entitlement_snapshot = {
                "schema_version": "monitoring-entitlement-snapshot-v1",
                "source": source,
                "platforms": entitlement,
                "search_mode": str(search_mode or "enhanced"),
                "monitoring_product_version": (
                    str(keyword.get("monitoring_product_version"))
                    if keyword.get("monitoring_product_version")
                    else (
                        DEFAULT_MONITORING_PRODUCT_VERSION
                        if source in {"confirmed", "contract"} else None
                    )
                ),
                "retry_coverage": retry_policy,
            }
            order_snapshot = quote_snapshots.get(quote_id) or {
                "schema_version": "monitoring-order-snapshot-v1",
                "source": "brand-extra-keyword",
                "brand_id": int(brand_id),
                "quote_id": None,
            }
            question = resolve_monitoring_query(keyword)
            for platform in MONITORING_RUN_CELL_PLATFORMS:
                is_planned = platform in executable
                state = "queued" if is_planned else "unavailable"
                executable_count += int(is_planned)
                plan_payload = {
                    "schema_version": "monitoring-cell-plan-v1",
                    "task_id": int(task_id),
                    "brand_id": int(brand_id),
                    "keyword_id": keyword_id,
                    "keyword_source": source,
                    "quote_id": quote_id,
                    "keyword": str(keyword.get("keyword") or ""),
                    "question": question,
                    "target_brand": str(keyword.get("target_brand") or ""),
                    "platform": platform,
                    "is_planned": is_planned,
                    "search_mode": str(search_mode or "enhanced"),
                    "entitlement": entitlement_snapshot,
                    "order": order_snapshot,
                    "fulfillment_credential": keyword_credential,
                    "settlement_reference": keyword_settlement_reference,
                }
                plan_hash = _monitoring_plan_hash(plan_payload)
                cur.execute(
                    """
                    INSERT INTO public.monitoring_run_cells
                        (task_id, brand_id, keyword_id, keyword_source, quote_id,
                         keyword_snapshot, question_snapshot, target_brand_snapshot,
                         platform, is_planned, state, entitlement_snapshot,
                         order_snapshot, fulfillment_credential, fulfillment_state,
                         settlement_reference, plan_hash, tenant_owner_user_id,
                         error_code, error_message, completed_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            CASE WHEN %s THEN NULL ELSE NOW() END)
                    ON CONFLICT (task_id, keyword_source, keyword_id, platform)
                    DO NOTHING
                    RETURNING *
                    """,
                    (
                        int(task_id), int(brand_id), keyword_id, source, quote_id,
                        plan_payload["keyword"], question, plan_payload["target_brand"],
                        platform, is_planned, state, Json(entitlement_snapshot),
                        Json(order_snapshot), keyword_credential, keyword_fulfillment_state,
                        keyword_settlement_reference, plan_hash, frozen_tenant_owner,
                        None if is_planned else "not_in_purchased_run_plan",
                        None if is_planned else "当前履约计划不包含该平台",
                        is_planned,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise MonitoringCellConflict("monitoring task plan already exists")
                cells.append(dict(row))
                # [防御型 GEO 包F ① · 2026-08-23] 接线点④:未购平台 = policy skip。
                #
                # 🔴 为什么这一格必须进账本:MON-11 逐字「skipped 无 provider
                #    attempt、**计 terminal 不计 attempted**」。不记它,五卡
                #    summary 的 terminal 永远到不了 planned ⇒
                #    assert_terminal_conservation 永远判"数据坏了",
                #    而真相是"这几个平台客户没买"。
                # 🔴 它**不进**进度端点的分母:那条链按 is_planned=TRUE 取格
                #    (一期 P1-C)。两个分母不同是有意的 ——
                #    进度回答"我买的这些做到哪了",五卡回答"这次一共看了哪些格"。
                #    本函数内部由 record_skip_for_plan 自己判 is_planned,
                #    已计划的格在这里零写入。
                from services.defensive_geo.monitoring.run_ledger_bridge import (
                    record_skip_for_plan,
                )
                record_skip_for_plan(cur, dict(row))

        if executable_count != int(task.get("total_tests") or 0):
            raise MonitoringCellConflict(
                "planned_test_count does not match durable monitoring cells"
            )
        conn.commit()
        return cells
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _recover_expired_monitoring_cells(cur, *, task_id: int = None, cell_id: int = None) -> int:
    scope_sql = "task_id=%s" if task_id is not None else "id=%s"
    scope_value = int(task_id if task_id is not None else cell_id)
    cur.execute(
        f"""
        UPDATE public.monitoring_run_cells
           SET state = CASE
                   WHEN provider_dispatched_at IS NULL THEN 'failed'
                   ELSE 'pending_provider_confirmation'
               END,
               error_code = CASE
                   WHEN provider_dispatched_at IS NULL THEN 'worker_lost_before_dispatch'
                   ELSE 'provider_outcome_unknown'
               END,
               error_message = CASE
                   WHEN provider_dispatched_at IS NULL THEN '执行进程在请求发送前中断，可安全重试'
                   ELSE '请求已发送但结果未确认，禁止自动再次调用'
               END,
               claim_token=NULL, claim_until=NULL, completed_at=NOW(), updated_at=NOW()
         WHERE {scope_sql}
           AND state='running' AND claim_until < NOW()
        """,
        (scope_value,),
    )
    recovered = int(cur.rowcount)
    # [防御型 GEO 包F ① · 2026-08-23] 崩溃恢复的**账本那一半**。
    #
    # 🔴 这里是现役自己的崩溃恢复 chokepoint(claim / list / retry 三条路都过它)。
    #    上面那条 UPDATE 刚把租约过期的 running 格终态化 —— 于是它们对应的
    #    在飞 attempt **已被证明**不在飞了,必须一起收口。
    # 🔴 不收的后果是一期审计点名的那个雷:uq_defgeo_attempt_single_inflight
    #    是 partial unique,那条在飞行会**永久**占着位子,该格从此
    #    再也 open 不进第二个 attempt(重试永远不进账本)。
    # 🔴 reclaim 判"已不在飞"优先用 cell.state(**事实**)而不是超时(**猜**),
    #    所以此处顺序是有意的:先终态化 cell,再 reclaim ——
    #    反过来的话 cell 还是 running,reclaim 会一条都收不到。
    from services.defensive_geo.monitoring.run_ledger_bridge import reclaim_orphans
    if cell_id is not None:
        reclaim_orphans(cur, monitoring_cell_ids=(int(cell_id),))
    else:
        cur.execute(
            "SELECT id FROM public.monitoring_run_cells WHERE task_id=%s",
            (int(task_id),))
        _ids = [int(r["id"]) for r in (cur.fetchall() or [])]
        if _ids:
            reclaim_orphans(cur, monitoring_cell_ids=_ids)
    return recovered


def recover_abandoned_monitoring_task_execution(task_id: int) -> int:
    """Terminalize every uncompleted cell after the whole task is proven abandoned."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET state = CASE
                       WHEN state='running' AND provider_dispatched_at IS NOT NULL
                       THEN 'pending_provider_confirmation'
                       ELSE 'failed'
                   END,
                   error_code = CASE
                       WHEN state='running' AND provider_dispatched_at IS NOT NULL
                       THEN 'provider_outcome_unknown'
                       ELSE 'worker_lost_before_dispatch'
                   END,
                   error_message = CASE
                       WHEN state='running' AND provider_dispatched_at IS NOT NULL
                       THEN '请求已发送但结果未确认，禁止自动再次调用'
                       ELSE '执行进程在请求发送前中断，可安全重试'
                   END,
                   claim_token=NULL, claim_until=NULL,
                   completed_at=NOW(), updated_at=NOW()
             WHERE task_id=%s AND is_planned
               AND (
                    state='queued'
                    OR (state='running' AND claim_until < NOW())
               )
         RETURNING id
            """,
            (int(task_id),),
        )
        reaped = [int(r["id"]) for r in (cur.fetchall() or [])]
        changed = len(reaped)
        # [防御型 GEO 包F R2 · F-3a · 2026-08-24] 接线点⑥:sweeper 收割也要收账本。
        #
        # 🔴 这条路径**每小时无条件跑**(api/scheduler.py 的 freeze_sweeper_hourly
        #    → services/freeze_sweeper.py 五处调用),而它把 cell 打到终态时
        #    绕过了账本 —— 于是这些格的在飞 attempt 行**永久悬挂**:
        #    cell 早已 failed / pending_provider_confirmation,账本却一直说
        #    "还在跑"。五卡的分母据此算,MON-11 的"attempt 数 = 真实调用数"
        #    也就永远对不上,而**没有任何东西会报错**。
        #
        # 🔴 放在 UPDATE 之后、commit 之前:``reclaim_orphans`` 的权威分支判的是
        #    ``c.state <> 'running'``,此刻在同一个事务里恰好为真 ——
        #    这正是它被设计出来要管的场景。放到 commit 之后就变成两个事务,
        #    中间崩一下就又漏了。
        # 🔴 fail-soft:桥函数自己带 SAVEPOINT + never_raises,账本故障不许
        #    阻断现役的恢复动作(一期 legacy_bridge 立的标杆)。
        if reaped:
            from services.defensive_geo.monitoring.run_ledger_bridge import (
                UNATTEMPTED_ABANDONED_REASON,
                close_unattempted_for_cells,
                reclaim_orphans,
            )
            reclaim_orphans(cur, monitoring_cell_ids=reaped)
            # [工单 C-3 · Codex 终审 P1-10] 接线点⑧:**从未形成 attempt** 的
            # queued 格也要闭合账本。
            #
            # 🔴 ``reclaim_orphans`` 只动 ``terminal_state IS NULL`` 的**在飞**行,
            #    而 queued 格在账本里一行都没有(``open_for_claim`` 只在 claim 成
            #    running 时才写)⇒ 上面那一跳对它命中零行,却看起来干完了。
            #    这些格于是停在"计划了、没尝试、也没有任何终态记录":
            #    既没被扣(没交付)、也没被退(整任务的 release 只看
            #    fulfillment_state)、更没有人管 —— 资金侧的第三态。
            # 🔴 两个函数作用域**不重叠**(一个要求账本有在飞行、一个要求账本零行),
            #    所以不存在"两把锁叠同一条路径互相吞变异"。
            close_unattempted_for_cells(
                cur, monitoring_cell_ids=reaped,
                reason_code=UNATTEMPTED_ABANDONED_REASON)
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_monitoring_run_cells(task_id: int, *, brand_id: int = None) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        _recover_expired_monitoring_cells(cur, task_id=int(task_id))
        params: List[Any] = [int(task_id)]
        scope = "task_id=%s"
        if brand_id is not None:
            scope += " AND brand_id=%s"
            params.append(int(brand_id))
        cur.execute(
            f"""
            SELECT id, task_id, brand_id, keyword_id, keyword_source, quote_id,
                   keyword_snapshot, platform, is_planned, state, plan_hash,
                   fulfillment_state, attempt_count, retry_count, result_id,
                   entitlement_snapshot->'retry_coverage'->>'coverage' AS retry_coverage,
                   CASE
                     WHEN entitlement_snapshot->'retry_coverage'->>'max_attempts' ~ '^[0-9]+$'
                     THEN (entitlement_snapshot->'retry_coverage'->>'max_attempts')::INTEGER
                     ELSE 0
                   END AS retry_max_attempts,
                   error_code, error_message, started_at, completed_at, updated_at
              FROM public.monitoring_run_cells
             WHERE {scope}
             ORDER BY keyword_snapshot, keyword_source, keyword_id,
                      array_position(ARRAY['dashscope','deepseek','kimi','doubao'], platform)
            """,
            tuple(params),
        )
        rows = [dict(row) for row in cur.fetchall()]
        conn.commit()
        return rows
    finally:
        conn.close()


def claim_monitoring_run_cell(
    *, cell_id: int, task_id: int, brand_id: int, allowed_state: str = "queued",
    lease_seconds: int = 300,
) -> Dict[str, Any]:
    if allowed_state != "queued":
        raise ValueError("invalid monitoring cell claim state")
    token = str(uuid.uuid4())
    conn = get_connection()
    try:
        cur = conn.cursor()
        _recover_expired_monitoring_cells(cur, cell_id=int(cell_id))
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET state='running', claim_token=%s,
                   claim_until=NOW() + (%s || ' seconds')::interval,
                   attempt_count=attempt_count+1,
                   started_at=COALESCE(started_at, NOW()), completed_at=NULL,
                   error_code=NULL, error_message=NULL, updated_at=NOW()
             WHERE id=%s AND task_id=%s AND brand_id=%s
               AND is_planned AND state=%s
             RETURNING *
            """,
            (token, max(30, min(int(lease_seconds), 1800)), int(cell_id), int(task_id),
             int(brand_id), allowed_state),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "SELECT state FROM public.monitoring_run_cells WHERE id=%s AND task_id=%s AND brand_id=%s",
                (int(cell_id), int(task_id), int(brand_id)),
            )
            current = cur.fetchone()
            if not current:
                raise MonitoringCellNotFound("monitoring cell not found")
            raise MonitoringCellConflict(f"monitoring cell cannot be claimed from {current['state']}")
        # [防御型 GEO 包F ① · 2026-08-23] 接线点①:派发**前**登记一条在飞 attempt。
        #
        # 🔴 位置是有意的 —— 在这条 claim 之后、provider 调用之前。一期
        #    ``attempt_ledger.open_attempt`` 的 docstring 逐字写了为什么不能等结果:
        #    「拿到结果后才记,进程在 provider 调用与落库之间崩掉的那些 attempt
        #    会整条消失,attemptRecords 与真实调用数就对不上(MON-11)」。
        # 🔴 接在这里而不是四个调用方(api/monitoring_api.py ×2 / api/scheduler.py /
        #    scheduler.py):同一个谓词写四处必有一处没人验。
        # 🔴 fail-soft 由 run_ledger_bridge.guarded 的 SAVEPOINT 保证 ——
        #    账本写崩**不会**把这条 claim 的事务打废(一期 legacy_bridge 的裸
        #    try/except 就会,已一并修掉)。
        from services.defensive_geo.monitoring.run_ledger_bridge import open_for_claim
        open_for_claim(cur, dict(row))
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_monitoring_cell_dispatched(*, cell_id: int, claim_token: str) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET provider_dispatched_at=NOW(),
                   retry_count=retry_count + CASE WHEN EXISTS (
                       SELECT 1
                         FROM public.monitoring_cell_retry_requests request
                        WHERE request.cell_id=monitoring_run_cells.id
                          AND request.claim_token=%s
                          AND request.status='running'
                   ) THEN 1 ELSE 0 END,
                   updated_at=NOW()
             WHERE id=%s AND state='running' AND claim_token=%s
               AND provider_dispatched_at IS NULL
            """,
            (str(claim_token), int(cell_id), str(claim_token)),
        )
        if cur.rowcount != 1:
            raise MonitoringCellConflict("monitoring cell dispatch lease lost")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_monitoring_cell_error(
    *, cell_id: int, claim_token: str, state: str, error_code: str, error_message: str,
) -> Dict[str, Any]:
    if state not in {"failed", "unavailable", "pending_provider_confirmation"}:
        raise ValueError("invalid monitoring cell error state")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET state=%s, error_code=%s, error_message=%s,
                   claim_token=NULL, claim_until=NULL, completed_at=NOW(), updated_at=NOW()
             WHERE id=%s AND state='running' AND claim_token=%s
             RETURNING *
            """,
            (state, str(error_code or "platform_unavailable")[:80],
             str(error_message or "")[:2000], int(cell_id), str(claim_token)),
        )
        row = cur.fetchone()
        if not row:
            raise MonitoringCellConflict("monitoring cell completion lease lost")
        # [防御型 GEO 包F ① · 2026-08-23] 接线点③:失败收口。
        #
        # 三种现役错误态(failed / unavailable / pending_provider_confirmation)
        # **全部**落 engine_error —— 它们的共同事实是"这一次没拿到可用回答"。
        # 🔴 MON-02 逐字禁止把它们压成 absent:压成"未提及"会让客户看到一份
        #    "品牌提及率下降"的报告,而真相是我们没拿到回答。
        from services.defensive_geo.monitoring.run_ledger_bridge import close_for_error
        close_for_error(
            cur,
            plan_cell_id=str(row.get("plan_hash") or ""),
            error_code=str(row.get("error_code") or ""),
            error_message=str(row.get("error_message") or ""),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_monitoring_task_fulfillment_state(task_id: int, state: str) -> int:
    if state not in {"covered", "admin_covered", "coverage_unknown", "released"}:
        raise ValueError("invalid monitoring fulfillment state")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET fulfillment_state=%s, updated_at=NOW()
             WHERE task_id=%s AND fulfillment_state NOT IN ('covered','admin_covered')
            """,
            (state, int(task_id)),
        )
        changed = int(cur.rowcount)
        conn.commit()
        return changed
    finally:
        conn.close()


def revoke_monitoring_task_coverage_for_organization_refund(
    task_id: int, organization_charge_id: int,
) -> int:
    """Revoke retry coverage only from a physically linked, fully refunded org charge."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM public.monitoring_run_cells WHERE task_id=%s FOR UPDATE",
            (int(task_id),),
        )
        cells = cur.fetchall()
        if not cells:
            raise MonitoringCellConflict("monitoring task has no durable cells")
        cur.execute(
            """
            SELECT COUNT(*) AS proven
              FROM public.monitoring_run_cells c
              JOIN public.point_freezes pf
                ON pf.task_ref=c.settlement_reference
              JOIN public.organization_charge_links charge
                ON charge.physical_backend='legacy_user_wallet'
               AND charge.physical_freeze_id=pf.id::text
             WHERE c.task_id=%s AND charge.id=%s AND charge.status='refunded'
            """,
            (int(task_id), int(organization_charge_id)),
        )
        if int(cur.fetchone().get("proven") or 0) != len(cells):
            raise MonitoringCellConflict(
                "organization refund does not prove every monitoring cell"
            )
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET fulfillment_state='released',
                   state=CASE
                       WHEN state='running' AND provider_dispatched_at IS NOT NULL
                       THEN 'pending_provider_confirmation'
                       WHEN state IN ('queued','running') THEN 'failed'
                       ELSE state
                   END,
                   error_code=CASE
                       WHEN state='running' AND provider_dispatched_at IS NOT NULL
                       THEN 'provider_outcome_unknown'
                       WHEN state IN ('queued','running') THEN 'fulfillment_refunded'
                       ELSE error_code
                   END,
                   error_message=CASE
                       WHEN state='running' AND provider_dispatched_at IS NOT NULL
                       THEN '请求已发送但结果未确认，原履约已退款，禁止再次调用'
                       WHEN state IN ('queued','running')
                       THEN '原履约已全额退款，未派发单格已停止'
                       ELSE error_message
                   END,
                   claim_token=CASE WHEN state IN ('queued','running') THEN NULL ELSE claim_token END,
                   claim_until=CASE WHEN state IN ('queued','running') THEN NULL ELSE claim_until END,
                   completed_at=CASE
                       WHEN state IN ('queued','running') THEN COALESCE(completed_at,NOW())
                       ELSE completed_at
                   END,
                   updated_at=NOW()
             WHERE task_id=%s
               AND fulfillment_state IN ('reserved','coverage_unknown','covered')
         RETURNING id
            """,
            (int(task_id),),
        )
        released = [int(r["id"]) for r in (cur.fetchall() or [])]
        changed = len(released)
        # [防御型 GEO 包F R2 · F-3b · 2026-08-24] 接线点⑦:退款释放也要收账本。
        #
        # 与 F-3a 同一个 cron 链(services/freeze_sweeper.py:207),同一个后果:
        # 退款把格停了,账本里的在飞 attempt 却永久悬挂。
        # 🔴 这里的 ``released`` 含**本来就已终态**的格(WHERE 只筛
        #    fulfillment_state,CASE 才筛 state)。传给 reclaim_orphans 是安全的:
        #    它只动 ``terminal_state IS NULL`` 的账本行,已收口的一行不碰。
        #    宁可分母给宽也不给窄 —— 给窄了漏掉的那一格不会让任何判据变红。
        if released:
            from services.defensive_geo.monitoring.run_ledger_bridge import (
                UNATTEMPTED_REFUNDED_REASON,
                close_unattempted_for_cells,
                reclaim_orphans,
            )
            reclaim_orphans(cur, monitoring_cell_ids=released)
            # [工单 C-3 · Codex 终审 P1-10] 接线点⑧(退款那一半)。
            #
            # 🔴 退款路径比 abandon 路径更需要这一跳:钱**确实退了**,
            #    而账本里这几个 queued 格连"被取消"这件事都没记下。
            #    对账时它们看起来像"还欠客户一次测量",而实际上那笔钱已经
            #    还回去了 —— 两边的故事对不上,且没有任何东西会报错。
            # 🔴 理由码逐字沿用现役写进 cell 的 ``fulfillment_refunded``,
            #    不另起名字:账本与 cell 必须是同一个故事。
            close_unattempted_for_cells(
                cur, monitoring_cell_ids=released,
                reason_code=UNATTEMPTED_REFUNDED_REASON)
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_monitoring_keyword_fulfillment_state(
    task_id: int, keyword_id: int, keyword_source: str, state: str
) -> int:
    """Settle only one independently billed keyword inside a grouped task."""
    if state not in {"covered", "admin_covered", "coverage_unknown", "released"}:
        raise ValueError("invalid monitoring fulfillment state")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET fulfillment_state=%s, updated_at=NOW()
             WHERE task_id=%s AND keyword_id=%s AND keyword_source=%s
               AND fulfillment_state NOT IN ('covered','admin_covered')
            """,
            (state, int(task_id), int(keyword_id), str(keyword_source)),
        )
        changed = int(cur.rowcount)
        conn.commit()
        return changed
    finally:
        conn.close()


def create_monitoring_keyword_settlements(
    *, task_id: int, brand_id: int, keywords: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Persist one independently billed daily-keyword settlement before dispatch."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        created = []
        for keyword in keywords:
            reference = str(keyword.get("_settlement_reference") or "").strip()[:160]
            if not reference:
                raise ValueError("daily monitoring settlement reference is required")
            source = str(keyword.get("source") or "").strip().lower()
            user_id = keyword.get("user_id")
            initial_state = "reserved" if user_id else "admin_covered"
            cur.execute(
                """
                INSERT INTO public.monitoring_keyword_settlements
                    (settlement_reference, task_id, brand_id, keyword_id,
                     keyword_source, subscription_id, billing_user_id,
                     feature_code, state, charge_recorded)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    reference, int(task_id), int(brand_id), int(keyword["id"]), source,
                    int(keyword["subscription_id"]) if keyword.get("subscription_id") else None,
                    int(user_id) if user_id else None,
                    str(keyword.get("feature_code") or "monitoring_keyword_daily"),
                    initial_state, not bool(user_id),
                ),
            )
            created.append(dict(cur.fetchone()))
        conn.commit()
        return created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_monitoring_keyword_settlement_claim(
    settlement_reference: str, claim_state: Dict[str, Any],
) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_keyword_settlements
               SET claim_token=%s, previous_claim_at=%s, updated_at=NOW()
             WHERE settlement_reference=%s AND state='reserved'
               AND claim_token IS NULL
            """,
            (
                claim_state.get("claim_token"), claim_state.get("previous_value"),
                str(settlement_reference),
            ),
        )
        if cur.rowcount != 1:
            raise MonitoringCellConflict("daily monitoring settlement claim conflict")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_monitoring_keyword_settlement_freeze(
    settlement_reference: str, freeze_handle: Dict[str, Any],
) -> None:
    freeze_id = freeze_handle.get("freeze_id")
    freeze_table = freeze_handle.get("freeze_table")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_keyword_settlements
               SET freeze_id=%s, freeze_table=%s, frozen_amount=%s,
                   state=%s, updated_at=NOW()
             WHERE settlement_reference=%s AND state='reserved'
            """,
            (
                int(freeze_id) if freeze_id else None,
                str(freeze_table) if freeze_table else None,
                int(freeze_handle.get("amount") or 0),
                "frozen" if freeze_id else "admin_covered",
                str(settlement_reference),
            ),
        )
        if cur.rowcount != 1:
            raise MonitoringCellConflict("daily monitoring freeze binding conflict")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_monitoring_keyword_settlement_dispatched(settlement_reference: str) -> None:
    """Durably fail closed the whole paid keyword as soon as one provider may run."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_keyword_settlements
               SET state=CASE WHEN state='admin_covered' THEN state ELSE 'coverage_unknown' END,
                   updated_at=NOW()
             WHERE settlement_reference=%s
               AND state IN ('reserved','frozen','coverage_unknown','admin_covered')
            """,
            (str(settlement_reference),),
        )
        if cur.rowcount != 1:
            raise MonitoringCellConflict("daily monitoring dispatch settlement conflict")
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET fulfillment_state='coverage_unknown', updated_at=NOW()
             WHERE settlement_reference=%s
               AND fulfillment_state IN ('reserved','coverage_unknown')
            """,
            (str(settlement_reference),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def settle_monitoring_keyword_reference(settlement_reference: str, state: str) -> None:
    """Atomically settle durable keyword ledger and every cell under its credential."""
    if state not in {"committed", "released", "admin_covered"}:
        raise ValueError("invalid daily monitoring settlement state")
    fulfillment_state = {
        "committed": "covered", "released": "released", "admin_covered": "admin_covered",
    }[state]
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_keyword_settlements
               SET state=%s, updated_at=NOW()
             WHERE settlement_reference=%s
               AND state NOT IN ('committed','released')
            """,
            (state, str(settlement_reference)),
        )
        if cur.rowcount == 0:
            cur.execute(
                "SELECT state FROM public.monitoring_keyword_settlements WHERE settlement_reference=%s",
                (str(settlement_reference),),
            )
            prior = cur.fetchone()
            if not prior or prior.get("state") != state:
                raise MonitoringCellConflict("daily monitoring settlement terminal conflict")
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET fulfillment_state=%s, updated_at=NOW()
             WHERE settlement_reference=%s
               AND fulfillment_state NOT IN ('covered','admin_covered','released')
            """,
            (fulfillment_state, str(settlement_reference)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_monitoring_subscription_charge_for_settlement(settlement_reference: str) -> bool:
    """Idempotently project a committed freeze into subscription charge totals."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM public.monitoring_keyword_settlements
             WHERE settlement_reference=%s FOR UPDATE
            """,
            (str(settlement_reference),),
        )
        row = cur.fetchone()
        if not row:
            raise MonitoringCellNotFound("daily monitoring settlement not found")
        if row.get("charge_recorded"):
            conn.commit()
            return False
        if row.get("state") not in {"committed", "admin_covered"}:
            raise MonitoringCellConflict("uncommitted monitoring charge cannot be recorded")
        subscription_id = row.get("subscription_id")
        if subscription_id:
            cur.execute(
                """
                UPDATE public.keyword_monitor_subscriptions
                   SET last_charged_at=CURRENT_TIMESTAMP,
                       last_charge_amount=%s,
                       total_charged=COALESCE(total_charged,0)+%s,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE id=%s
                """,
                (int(row.get("frozen_amount") or 0), int(row.get("frozen_amount") or 0),
                 int(subscription_id)),
            )
            if cur.rowcount != 1:
                raise MonitoringCellConflict("monitoring subscription charge target missing")
        cur.execute(
            """
            UPDATE public.monitoring_keyword_settlements
               SET charge_recorded=TRUE, updated_at=NOW()
             WHERE settlement_reference=%s AND charge_recorded=FALSE
            """,
            (str(settlement_reference),),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_stale_monitoring_keyword_settlements(stale_hours: int) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.*,
                   COALESCE(BOOL_OR(c.provider_dispatched_at IS NOT NULL), FALSE)
                       AS provider_dispatched
              FROM public.monitoring_keyword_settlements s
              LEFT JOIN public.monitoring_run_cells c
                ON c.settlement_reference=s.settlement_reference
             WHERE (s.state IN ('reserved','frozen','coverage_unknown')
                    OR (s.state IN ('committed','admin_covered') AND NOT s.charge_recorded))
               AND s.updated_at < NOW() - (%s || ' hours')::interval
             GROUP BY s.settlement_reference
             ORDER BY s.updated_at, s.settlement_reference
            """,
            (max(1, int(stale_hours)),),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def find_monitoring_freeze_for_settlement(
    settlement_reference: str, billing_user_id: int,
) -> Optional[Dict[str, Any]]:
    """Find a freeze even if the worker died before copying its handle to the ledger."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        matches = []
        cur.execute(
            """
            SELECT id, 'legacy'::text AS freeze_table, status, amount_total
              FROM public.point_freezes
             WHERE task_ref=%s AND user_id=%s
            """,
            (str(settlement_reference), int(billing_user_id)),
        )
        matches.extend(dict(row) for row in cur.fetchall())
        cur.execute(
            "SELECT pg_catalog.to_regclass('public.customer_credit_freezes') AS relation"
        )
        if cur.fetchone().get("relation") is not None:
            cur.execute(
                """
                SELECT id, 'v35'::text AS freeze_table, status, amount_total
                  FROM public.customer_credit_freezes
                 WHERE task_ref=%s AND customer_user_id=%s
                """,
                (str(settlement_reference), int(billing_user_id)),
            )
            matches.extend(dict(row) for row in cur.fetchall())
        if len(matches) > 1:
            raise MonitoringCellConflict("multiple freezes share one monitoring settlement")
        return matches[0] if matches else None
    finally:
        conn.close()


def list_stale_monitoring_task_settlements(stale_hours: int) -> List[Dict[str, Any]]:
    """Return non-daily task settlements whose worker can no longer be trusted."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH candidate_references AS (
                SELECT c.settlement_reference
                  FROM public.monitoring_run_cells c
                  LEFT JOIN public.monitoring_keyword_settlements s
                    ON s.settlement_reference=c.settlement_reference
                 WHERE c.settlement_reference IS NOT NULL
                   AND s.settlement_reference IS NULL
                 GROUP BY c.settlement_reference
                HAVING BOOL_OR(
                           c.fulfillment_state IN ('reserved','coverage_unknown')
                           OR (c.is_planned AND c.state IN ('queued','running'))
                       )
                   AND MAX(c.updated_at) FILTER (
                           WHERE c.fulfillment_state IN ('reserved','coverage_unknown')
                              OR (c.is_planned AND c.state IN ('queued','running'))
                       ) < NOW() - (%s || ' hours')::interval
            )
            SELECT c.settlement_reference,
                   MIN(c.task_id) AS task_id,
                   MIN(c.brand_id) AS brand_id,
                   COUNT(DISTINCT c.task_id) AS task_count,
                   BOOL_OR(c.provider_dispatched_at IS NOT NULL) AS provider_dispatched,
                   BOOL_OR(c.is_planned AND c.state IN ('queued','running')) AS execution_abandoned,
                   BOOL_AND(c.fulfillment_state='admin_covered')
                       FILTER (WHERE c.is_planned) AS all_admin_covered
              FROM public.monitoring_run_cells c
              JOIN candidate_references candidate
                ON candidate.settlement_reference=c.settlement_reference
             GROUP BY c.settlement_reference
             ORDER BY MIN(c.updated_at), c.settlement_reference
            """,
            (max(1, int(stale_hours)),),
        )
        rows_by_reference = {
            str(row["settlement_reference"]): dict(row) for row in cur.fetchall()
        }
        cur.execute(
            "SELECT pg_catalog.to_regclass('public.organization_charge_links') AS relation"
        )
        if cur.fetchone().get("relation") is not None:
            if rows_by_reference:
                cur.execute(
                    """
                    SELECT pf.task_ref AS settlement_reference,
                           CASE WHEN COUNT(charge.id)=1 THEN MIN(charge.status)
                                ELSE 'conflict' END AS organization_status_hint
                      FROM public.point_freezes pf
                      JOIN public.organization_charge_links charge
                        ON charge.physical_backend='legacy_user_wallet'
                       AND charge.physical_freeze_id=pf.id::text
                     WHERE pf.task_ref=ANY(%s)
                     GROUP BY pf.task_ref
                    """,
                    (list(rows_by_reference),),
                )
                for hint in cur.fetchall():
                    reference = str(hint["settlement_reference"])
                    if reference in rows_by_reference:
                        rows_by_reference[reference]["organization_status_hint"] = str(
                            hint["organization_status_hint"]
                        )
            # A later full refund revokes already-covered retry entitlement and
            # must be projected even though the original task is no longer stale.
            cur.execute(
                """
                SELECT c.settlement_reference,
                       MIN(c.task_id) AS task_id,
                       MIN(c.brand_id) AS brand_id,
                       COUNT(DISTINCT c.task_id) AS task_count,
                       BOOL_OR(c.provider_dispatched_at IS NOT NULL) AS provider_dispatched,
                       BOOL_OR(c.is_planned AND c.state IN ('queued','running')) AS execution_abandoned,
                       FALSE AS all_admin_covered,
                       TRUE AS organization_refunded,
                       'refunded'::text AS organization_status_hint
                  FROM public.monitoring_run_cells c
                  JOIN public.point_freezes pf
                    ON pf.task_ref=c.settlement_reference
                  JOIN public.organization_charge_links charge
                    ON charge.physical_backend='legacy_user_wallet'
                   AND charge.physical_freeze_id=pf.id::text
                  LEFT JOIN public.monitoring_keyword_settlements s
                    ON s.settlement_reference=c.settlement_reference
                 WHERE c.settlement_reference IS NOT NULL
                   AND s.settlement_reference IS NULL
                   AND charge.status='refunded'
                   AND c.fulfillment_state='covered'
                 GROUP BY c.settlement_reference
                """
            )
            for refunded_row in cur.fetchall():
                reference = str(refunded_row["settlement_reference"])
                merged = rows_by_reference.get(reference, {})
                merged.update(dict(refunded_row))
                rows_by_reference[reference] = merged
        quarantine_states = {"reserved", "unknown", "refund_pending", "refunded_pending"}

        def priority(row):
            status = str(row.get("organization_status_hint") or "")
            if bool(row.get("organization_refunded")) or status == "refunded":
                return 0
            if status in quarantine_states:
                return 2
            return 1

        return sorted(rows_by_reference.values(), key=priority)
    finally:
        conn.close()


def find_monitoring_task_freeze(settlement_reference: str) -> Optional[Dict[str, Any]]:
    """Resolve the exact physical freeze owner/table for a task settlement."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        matches = []
        cur.execute(
            """
            SELECT id, user_id AS billing_user_id, 'legacy'::text AS freeze_table,
                   status, amount_total
              FROM public.point_freezes
             WHERE task_ref=%s
            """,
            (str(settlement_reference),),
        )
        matches.extend(dict(row) for row in cur.fetchall())
        cur.execute(
            "SELECT pg_catalog.to_regclass('public.customer_credit_freezes') AS relation"
        )
        if cur.fetchone().get("relation") is not None:
            cur.execute(
                """
                SELECT id, customer_user_id AS billing_user_id,
                       'v35'::text AS freeze_table, status, amount_total
                  FROM public.customer_credit_freezes
                 WHERE task_ref=%s
                """,
                (str(settlement_reference),),
            )
            matches.extend(dict(row) for row in cur.fetchall())
        if len(matches) > 1:
            raise MonitoringCellConflict("multiple freezes share one monitoring task reference")
        if not matches:
            return None
        match = matches[0]
        match["organization_linked"] = False
        if match["freeze_table"] == "legacy":
            cur.execute(
                "SELECT pg_catalog.to_regclass('public.organization_charge_links') AS relation"
            )
            if cur.fetchone().get("relation") is not None:
                # Organization billing owns the physical legacy freeze through a
                # stricter ledger/state machine.  Never settle that leg directly,
                # even when the organization row is already terminal.
                cur.execute(
                    """
                    SELECT id, status
                      FROM public.organization_charge_links
                     WHERE physical_backend='legacy_user_wallet'
                       AND physical_freeze_id=%s
                    """,
                    (str(match["id"]),),
                )
                organization_links = [dict(row) for row in cur.fetchall()]
                if len(organization_links) > 1:
                    raise MonitoringCellConflict(
                        "multiple organization charges share one physical freeze"
                    )
                if organization_links:
                    match["organization_linked"] = True
                    match["organization_charge_id"] = int(organization_links[0]["id"])
                    match["organization_status"] = str(organization_links[0]["status"])
        return match
    finally:
        conn.close()


def reserve_monitoring_cell_retry(
    *, task_id: int, cell_id: int, brand_id: int, request_id: str,
    expected_plan_hash: str, lease_seconds: int = 300,
) -> Dict[str, Any]:
    """Idempotently reserve a failed cell under its original covered fulfillment."""
    from psycopg2.extras import Json

    request_uuid = str(uuid.UUID(str(request_id)))
    params_hash = _monitoring_plan_hash({
        "action": "retry-monitoring-cell-v1",
        "task_id": int(task_id),
        "cell_id": int(cell_id),
        "brand_id": int(brand_id),
        "plan_hash": str(expected_plan_hash),
    })
    claim_token = str(uuid.uuid4())
    conn = get_connection()
    try:
        cur = conn.cursor()
        # Serialize a request UUID independently of cell scope. Without this,
        # the same key racing across two cells can surface as a raw PK violation
        # instead of a deterministic idempotency conflict.
        cur.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
            (request_uuid,),
        )
        _recover_expired_monitoring_cells(cur, cell_id=int(cell_id))
        cur.execute(
            "SELECT * FROM public.monitoring_run_cells WHERE id=%s AND task_id=%s AND brand_id=%s FOR UPDATE",
            (int(cell_id), int(task_id), int(brand_id)),
        )
        cell = cur.fetchone()
        if not cell:
            raise MonitoringCellNotFound("monitoring cell not found")
        cur.execute(
            "SELECT * FROM public.monitoring_cell_retry_requests WHERE request_id=%s FOR UPDATE",
            (request_uuid,),
        )
        prior = cur.fetchone()
        if prior:
            if (
                int(prior["cell_id"]) != int(cell_id)
                or int(prior["task_id"]) != int(task_id)
                or int(prior["brand_id"]) != int(brand_id)
                or str(prior["params_hash"]) != params_hash
            ):
                raise MonitoringCellConflict("same retry key used with different parameters")
            repair_status = {
                "succeeded": "succeeded",
                "pending_identity": "succeeded",
                "failed": "failed",
                "unavailable": "failed",
                "pending_provider_confirmation": "blocked",
            }.get(str(cell["state"]))
            if repair_status and prior["status"] == "running":
                response_snapshot = {
                    "state": cell["state"],
                    "result_id": cell["result_id"],
                    "error_code": cell["error_code"],
                }
                cur.execute(
                    """
                    UPDATE public.monitoring_cell_retry_requests
                       SET status=%s, response_snapshot=%s, completed_at=NOW()
                     WHERE request_id=%s
                    """,
                    (repair_status, Json(response_snapshot), request_uuid),
                )
                prior = {
                    **dict(prior),
                    "status": repair_status,
                    "response_snapshot": response_snapshot,
                }
            conn.commit()
            return {"replay": True, "request": dict(prior), "cell": dict(cell)}

        # The cell projection can lag an organization refund by one sweeper
        # cycle. Lock the authoritative charge before trusting retry coverage.
        # Existing request IDs replay above and never create a second call.
        cur.execute(
            "SELECT pg_catalog.to_regclass('public.organization_charge_links') AS relation"
        )
        if cur.fetchone().get("relation") is not None:
            cur.execute(
                """
                SELECT charge.id, charge.status
                  FROM public.point_freezes pf
                  JOIN public.organization_charge_links charge
                    ON charge.physical_backend='legacy_user_wallet'
                   AND charge.physical_freeze_id=pf.id::text
                 WHERE pf.task_ref=%s
                 FOR UPDATE OF charge
                """,
                (str(cell.get("settlement_reference") or ""),),
            )
            organization_charges = [dict(row) for row in cur.fetchall()]
            if len(organization_charges) > 1:
                raise MonitoringCellConflict(
                    "multiple organization charges share monitoring fulfillment"
                )
            if organization_charges and organization_charges[0]["status"] != "committed":
                raise MonitoringCellConflict(
                    "original organization fulfillment is no longer retryable"
                )

        if str(cell["plan_hash"]) != str(expected_plan_hash):
            raise MonitoringCellConflict("monitoring cell plan hash changed")
        if cell["state"] in {"succeeded", "pending_identity"}:
            raise MonitoringCellConflict("successful monitoring cell cannot be retried")
        if cell["state"] == "pending_provider_confirmation":
            raise MonitoringCellConflict("provider outcome requires confirmation before retry")
        if cell["state"] != "failed":
            raise MonitoringCellConflict(f"monitoring cell cannot be retried from {cell['state']}")
        if cell["fulfillment_state"] not in {"covered", "admin_covered"}:
            raise MonitoringCellConflict("original fulfillment does not confirm retry coverage")
        retry_policy = dict((cell.get("entitlement_snapshot") or {}).get("retry_coverage") or {})
        if (
            retry_policy.get("policy_version") != "monitoring-retry-v1"
            or retry_policy.get("coverage") != "included"
        ):
            raise MonitoringCellConflict(
                "original fulfillment contract does not include a no-charge retry"
            )
        try:
            max_retry_attempts = int(retry_policy.get("max_attempts") or 0)
        except (TypeError, ValueError):
            max_retry_attempts = 0
        if max_retry_attempts <= 0 or int(cell.get("retry_count") or 0) >= max_retry_attempts:
            raise MonitoringCellConflict("original fulfillment retry coverage is exhausted")

        # [防御型 GEO WP7 · 2026-08-22] 在下面那条 UPDATE 把 error_code/error_message
        # NULL 掉**之前**,先把这一次失败落进追加式 attempt 账本。
        #
        # 🔴 现役这条重试是原地覆盖:``SET ... error_code=NULL, error_message=NULL``。
        #    跑完之后"第一次为什么失败"就没有了,而 MON-03「重试建 child attempt,
        #    不覆盖原始失败」与 MON-10「保留 attempt error」要的正是那条信息。
        # 🔴 legacy 语义**零变化**:迁移 046 没上时 ledger_is_available 返 False,
        #    这一跳什么都不做(MIG-04);写失败也只记 warning 不阻断重试 ——
        #    一条观测账本不该有能力把用户的重试挡下来。
        from services.defensive_geo.monitoring.legacy_bridge import (
            capture_before_retry_overwrite,
        )
        capture_before_retry_overwrite(cur, dict(cell))

        cur.execute(
            """
            INSERT INTO public.monitoring_cell_retry_requests
                (request_id, cell_id, task_id, brand_id, plan_hash, params_hash,
                 status, claim_token, response_snapshot)
            VALUES (%s,%s,%s,%s,%s,%s,'running',%s,%s)
            """,
            (request_uuid, int(cell_id), int(task_id), int(brand_id),
             str(expected_plan_hash), params_hash, claim_token, Json({})),
        )
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET state='running', claim_token=%s,
                   claim_until=NOW() + (%s || ' seconds')::interval,
                   attempt_count=attempt_count+1,
                   provider_dispatched_at=NULL, provider_request_id=NULL,
                   error_code=NULL, error_message=NULL, completed_at=NULL, updated_at=NOW()
             WHERE id=%s AND state='failed'
             RETURNING *
            """,
            (claim_token, max(30, min(int(lease_seconds), 1800)), int(cell_id)),
        )
        claimed = cur.fetchone()
        if not claimed:
            raise MonitoringCellConflict("monitoring cell retry claim raced")
        # [防御型 GEO 包F ① · 2026-08-23] 接线点①的**重试臂**。
        #
        # 上面那条 UPDATE 已经把这一格重新置成 running 并把 error_code NULL 掉,
        # 下一步就是真的再打一次 provider。所以这里必须像正常 claim 一样
        # 登记一条**新的**在飞 attempt(ordinal = MAX+1)——
        # 🔴 不登记的后果:重试那一次调用在账本里根本不存在,
        #    attemptRecords 少一次,而 MON-11 要求它与真实调用数一致;
        #    并且这一格从此没有在飞行,后续 close_for_result/close_for_error
        #    都会收不到东西(rowcount 0)。
        # 🔴 与上面那句 capture_before_retry_overwrite 不重复:那一跳是
        #    **存量 backfill**(只在这一格账本里一行都没有时才写),
        #    这一跳登记的是"接下来要发生的这一次"。
        from services.defensive_geo.monitoring.run_ledger_bridge import open_for_claim
        open_for_claim(cur, dict(claimed))
        conn.commit()
        return {
            "replay": False,
            "request": {"request_id": request_uuid, "status": "running", "claim_token": claim_token},
            "cell": dict(claimed),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_monitoring_cell_retry_request(
    *, request_id: str, claim_token: str, status: str, response_snapshot: Dict[str, Any],
) -> None:
    if status not in {"succeeded", "failed", "blocked"}:
        raise ValueError("invalid monitoring retry request terminal state")
    from psycopg2.extras import Json
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE public.monitoring_cell_retry_requests
               SET status=%s, response_snapshot=%s, completed_at=NOW()
             WHERE request_id=%s AND claim_token=%s AND status='running'
            """,
            (status, Json(dict(response_snapshot or {})), str(uuid.UUID(str(request_id))), str(claim_token)),
        )
        if cur.rowcount != 1:
            cur.execute(
                "SELECT status FROM public.monitoring_cell_retry_requests WHERE request_id=%s",
                (str(uuid.UUID(str(request_id))),),
            )
            prior = cur.fetchone()
            if not prior or prior["status"] != status:
                raise MonitoringCellConflict("monitoring retry request completion conflict")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_pending_monitoring_provider_reviews(*, limit: int = 100) -> List[Dict[str, Any]]:
    """Admin reconciliation queue for provider calls with an unknown outcome."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT cell.id AS cell_id, cell.task_id, cell.brand_id, cell.keyword_id,
                   cell.keyword_source, cell.keyword_snapshot, cell.platform,
                   cell.plan_hash, cell.provider_request_id, cell.provider_dispatched_at,
                   cell.fulfillment_state, cell.error_code, cell.error_message,
                   cell.updated_at
              FROM public.monitoring_run_cells cell
             WHERE cell.state='pending_provider_confirmation'
             ORDER BY cell.updated_at, cell.id
             LIMIT %s
            """,
            (max(1, min(int(limit), 200)),),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def decide_monitoring_provider_review(
    *, task_id: int, cell_id: int, brand_id: int, actor_user_id: int,
    action: str, note: str, request_id: str, expected_plan_hash: str,
) -> Dict[str, Any]:
    """Resolve UNKNOWN without calling a provider or changing any price/funds."""
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"confirm_failed", "confirm_unavailable"}:
        raise ValueError("invalid provider review action")
    normalized_note = str(note or "").strip()
    if not normalized_note:
        raise ValueError("provider review requires a note")
    normalized_note = normalized_note[:2000]
    request_uuid = str(uuid.UUID(str(request_id)))
    next_state = "failed" if normalized_action == "confirm_failed" else "unavailable"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
            (request_uuid,),
        )
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM public.user_roles ur
                  JOIN public.roles r ON r.id=ur.role_id
                 WHERE ur.user_id=%s AND r.name='admin'
            ) AS is_admin
            """,
            (int(actor_user_id),),
        )
        if not bool(cur.fetchone().get("is_admin")):
            raise PermissionError("provider outcome review requires a live admin role")
        cur.execute(
            "SELECT * FROM public.monitoring_provider_review_events WHERE request_id=%s FOR UPDATE",
            (request_uuid,),
        )
        prior = cur.fetchone()
        if prior:
            if (
                int(prior["cell_id"]) != int(cell_id)
                or int(prior["task_id"]) != int(task_id)
                or int(prior["brand_id"]) != int(brand_id)
                or str(prior["plan_hash"]) != str(expected_plan_hash)
                or str(prior["action"]) != normalized_action
                or str(prior["note"]) != normalized_note
            ):
                raise MonitoringCellConflict(
                    "same provider review key used with different parameters"
                )
            conn.commit()
            return {"status": "idempotent", **dict(prior)}

        cur.execute(
            """
            SELECT * FROM public.monitoring_run_cells
             WHERE id=%s AND task_id=%s AND brand_id=%s
             FOR UPDATE
            """,
            (int(cell_id), int(task_id), int(brand_id)),
        )
        cell = cur.fetchone()
        if not cell:
            raise MonitoringCellNotFound("monitoring cell not found")
        if str(cell["plan_hash"]) != str(expected_plan_hash):
            raise MonitoringCellConflict("monitoring cell plan hash changed")
        if cell["state"] != "pending_provider_confirmation":
            raise MonitoringCellConflict(
                f"monitoring provider review cannot resolve {cell['state']}"
            )
        cur.execute(
            """
            UPDATE public.monitoring_run_cells
               SET state=%s, error_code=%s, error_message=%s,
                   completed_at=NOW(), updated_at=NOW()
             WHERE id=%s AND state='pending_provider_confirmation'
            """,
            (
                next_state,
                (
                    "provider_outcome_manually_confirmed_failed"
                    if next_state == "failed" else "provider_outcome_manually_closed"
                ),
                normalized_note,
                int(cell_id),
            ),
        )
        if cur.rowcount != 1:
            raise MonitoringCellConflict("monitoring provider review raced")
        cur.execute(
            """
            INSERT INTO public.monitoring_provider_review_events
                (request_id,cell_id,task_id,brand_id,plan_hash,action,
                 actor_user_id,note,state_before,state_after)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending_provider_confirmation',%s)
            RETURNING *
            """,
            (
                request_uuid, int(cell_id), int(task_id), int(brand_id),
                str(expected_plan_hash), normalized_action, int(actor_user_id),
                normalized_note, next_state,
            ),
        )
        event = dict(cur.fetchone())
        conn.commit()
        try:
            refresh_monitoring_task_from_cells(int(task_id))
        except Exception:
            pass
        return {"status": "resolved", **event}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def refresh_monitoring_task_from_cells(task_id: int) -> Dict[str, Any]:
    """Recompute task progress from durable planned cells without changing its plan."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE is_planned) AS planned,
                   COUNT(*) FILTER (WHERE is_planned AND state='succeeded') AS succeeded,
                   COUNT(*) FILTER (WHERE is_planned AND state='pending_identity') AS pending_identity,
                   COUNT(*) FILTER (WHERE is_planned AND state='failed') AS failed,
                   COUNT(*) FILTER (WHERE is_planned AND state='pending_provider_confirmation') AS provider_unknown,
                   COUNT(*) FILTER (WHERE is_planned AND state='running') AS running,
                   COUNT(*) FILTER (WHERE is_planned AND state='queued') AS queued
              FROM public.monitoring_run_cells
             WHERE task_id=%s
            """,
            (int(task_id),),
        )
        counts = dict(cur.fetchone())
        planned = int(counts.get("planned") or 0)
        completed = int(counts.get("succeeded") or 0) + int(counts.get("pending_identity") or 0)
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE mr.is_detected=1) AS detected,
                   COUNT(*) AS eligible
              FROM public.monitoring_run_cells cell
              JOIN public.monitoring_results mr ON mr.id=cell.result_id
             WHERE cell.task_id=%s AND cell.state='succeeded'
            """,
            (int(task_id),),
        )
        result_counts = dict(cur.fetchone())
        eligible = int(result_counts.get("eligible") or 0)
        detected = int(result_counts.get("detected") or 0)
        summary = {
            "task_id": int(task_id),
            "attempted_tests": planned,
            "total_tests": eligible,
            "error_count": int(counts.get("failed") or 0) + int(counts.get("provider_unknown") or 0),
            "pending_identity_count": int(counts.get("pending_identity") or 0),
            "detected_count": detected,
            "detection_rate": round(detected / eligible * 100, 1) if eligible else 0,
            "cell_states": counts,
            "completed_at": datetime.now().isoformat(),
        }
        cur.execute(
            """
            UPDATE public.monitoring_tasks
               SET completed_tests=%s, result_summary=%s,
                   status=CASE WHEN %s > 0 OR %s > 0 THEN 'running' ELSE 'completed' END,
                   completed_at=CASE WHEN %s > 0 OR %s > 0 THEN NULL ELSE COALESCE(completed_at, NOW()) END
             WHERE id=%s AND total_tests=%s
            """,
            (
                completed, json.dumps(summary, ensure_ascii=False),
                int(counts.get("running") or 0), int(counts.get("queued") or 0),
                int(counts.get("running") or 0), int(counts.get("queued") or 0),
                int(task_id), planned,
            ),
        )
        if cur.rowcount != 1:
            raise MonitoringCellConflict("monitoring task planned_test_count changed")
        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==========================================
# 监测结果操作
# ==========================================

# [CTO-15.23 2026-05-07 P0-3] token cost placeholder · save_monitoring_result 内部调
# 后续可改:让 query_kimi/query_dashscope 等返 LLM usage 字段(input_tokens/output_tokens) ·
# PlatformAdapter 透传 · 这里直接拿 LLM 真值不再估算
# 当前 placeholder 的目的:从 0 行变有数据 · 月度对账先有量级



def _deepseek_cost_identity():
    """DeepSeek 监测平台的 (计价 platform, 模型名) —— 从 `PLATFORM_CONTRACT` 取。

    🔴 与上面 qwen 那一格**同法**:`_QWEN_MODEL = QWEN_ENGINE["model"]`。
       手写第二份的下场这个文件里就有前例:换代前这里写死 qwen3.7-plus 而
       ai_tester 实际发 qwen3-max,「按一个模型收钱、用另一个模型干活」,
       且没有任何判据会红。

    🔴 计价 platform 用 `deepseek` 而不是合同里的 `deepseek_official`:
       前者是**价目表的行键**,后者是**血缘的供货线名**,两套命名空间。
       写错会让成本落到一个价目表里不存在的行上。
    """
    from services.engine_contract import PLATFORM_CONTRACT as _PC
    cell = _PC.get("deepseek") or {}
    return ("deepseek", str(cell.get("model") or ""))
def _estimate_token_cost_placeholder(platform: str, keyword: str, full_response: str) -> tuple[int, int, float]:
    """估算 token + ¥cost · placeholder 版(2026-05-07 P0-3)

    估算方法:
      input_tokens ≈ 100(问题模板平均长度 · 后续接 LLM 真值)
      output_tokens ≈ len(full_response) // 3(中英文混 ~3 char/token)
      cost 走 tools.llm_call_tracker 统一价格表

    对 doubao 用 flat rate(¥0.20/次 ai_search) · 不按 token
    """
    from tools.llm_call_tracker import estimate_cost
    # [包F ⑥] 被测千问模型名的唯一来源 —— 见 QWEN_ENGINE 的 docstring。
    from services.engine_contract import QWEN_ENGINE as _QE
    _QWEN_MODEL = _QE["model"]

    input_tokens = 100  # 问题模板大致 100 token · 真值需 LLM usage 字段
    output_tokens = max(1, len(full_response or "") // 3)
    provider_platform, model = {
        # [包F ⑥ · 2026-08-24] 逐值取被测引擎 SSOT。
        # 🔴 这一行是那个存量记账错位的**现场**:换代前它写死 qwen3.7-plus,
        #    而 ai_tester 实际发的是 qwen3-max ⇒ 按一个模型收钱、用另一个
        #    模型干活,且没有任何判据会红。换代后两边同源,错位自然闭合;
        #    从常量取值而不是再手写一遍,是让它**不能**重新分叉。
        "dashscope": ("dashscope", _QWEN_MODEL),
        #: 🔴 [WO_221-c1' · Review 发现] 这一行是 DeepSeek 监测平台的**成本占位**
        #:   (monitoring_token_usage -> api_costs -> 财务/看板)。
        #:   2026-07-27 起该平台已换**官方原生检索**,而这里仍写着 dashscope +
        #:   退役名 ⇒ 成本记到百炼名下、模型名也是退役的(WO_214 同族)。
        #:   从血缘单点取,不再手写第二份 —— 上面那段注释说的正是「不能重新分叉」。
        "deepseek": _deepseek_cost_identity(),
        "kimi": ("kimi", "kimi-k2.6"),
        "doubao": ("doubao", "ai_search"),
        # [2026-07-27] 元宝换 hy3-preview(联网版)· 这是工单只点了 3 处之外的**第 4 处血缘**,
        # 漏改会让成本估算按旧模型算。
        # [2026-08-03] 改回 `hy3`:preview 8/31 下线且免费包耗尽未开后付费(402/401008)。
        # 价目表两条同价并存,历史行仍按当时模型算价。见 lineage.py 同日订正。
        "yuanbao": ("tencent_tokenhub", "hy3"),
    }.get(platform, (platform, None))
    cost = estimate_cost(provider_platform, model, input_tokens, output_tokens)
    return input_tokens, output_tokens, round(cost, 4)


def save_monitoring_result(
    task_id: int,
    keyword_id: int,
    keyword: str,
    platform: str,
    is_detected: bool,
    mention_type: str = "none",
    response_snippet: str = "",
    full_response: str = "",
    search_citations: str = "",
    competitors_mentioned=None,
    lineage: Dict[str, Any] = None,
    identity_brand_id: int | None = None,
    identity_candidates=None,
    identity_evidence_snippet: str = "",
    identity_review_state: str = "not_required",
    cell_id: int | None = None,
    cell_claim_token: str | None = None,
) -> int:
    """保存单条监测结果

    [CTO-15.23 2026-05-07 P0-3] 同步写 monitoring_token_usage 一行(placeholder 估算 token+cost)
    · Deploy-CTO 调查:monitoring_token_usage 表 30 天 0 写入 · save_token_usage 0 调用方
    · 此前 monitoring 走完不写对账表 · 月度成本核算永远 0
    · 修法:在每条 monitoring_result 落库后 · 估算 token + cost 一并写 monitoring_token_usage
    · 后续 v2:让 query_kimi 等返 LLM usage 真值 · 不再估算
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if cell_id is not None:
            if not cell_claim_token:
                raise ValueError("monitoring cell result requires a claim token")
            cursor.execute(
                """
                SELECT id, task_id, brand_id, keyword_id, keyword_source, quote_id,
                       keyword_snapshot, question_snapshot, target_brand_snapshot,
                       platform, state, claim_token
                  FROM public.monitoring_run_cells
                 WHERE id=%s
                 FOR UPDATE
                """,
                (int(cell_id),),
            )
            cell = cursor.fetchone()
            if not cell:
                raise MonitoringCellNotFound("monitoring cell not found")
            if (
                int(cell["task_id"]) != int(task_id)
                or int(cell["keyword_id"]) != int(keyword_id)
                or str(cell["platform"]) != str(platform)
                or cell["state"] != "running"
                or str(cell["claim_token"]) != str(cell_claim_token)
            ):
                raise MonitoringCellConflict("monitoring cell result lease or scope mismatch")

        response_char_count = len(full_response) if full_response else 0
        from services.monitoring_lineage import LINEAGE_VERSION, normalize_lineage_payload
        lineage_payload = normalize_lineage_payload(
            lineage,
            platform=platform,
            keyword=keyword,
            full_response=full_response,
            is_detected=is_detected,
            mention_type=mention_type,
            search_citations=search_citations,
        )
        if not lineage:
            lineage_payload["lineage_status"] = "explicit_unknown"
            lineage_payload["lineage_error_reason"] = "legacy_caller_no_provider_boundary_snapshot"
        if cell_id is not None:
            lineage_mismatches = []
            if str(keyword) != str(cell["keyword_snapshot"]):
                lineage_mismatches.append("keyword")
            if str(lineage_payload.get("keyword_source") or "") != str(cell["keyword_source"]):
                lineage_mismatches.append("keyword_source")
            if str(lineage_payload.get("sent_question_snapshot") or "") != str(cell["question_snapshot"]):
                lineage_mismatches.append("question")
            if str(lineage_payload.get("target_brand") or "") != str(cell["target_brand_snapshot"]):
                lineage_mismatches.append("target_brand")
            if identity_brand_id is None:
                identity_brand_id = int(cell["brand_id"])
            elif int(identity_brand_id) != int(cell["brand_id"]):
                lineage_mismatches.append("identity_brand")
            if lineage_mismatches:
                raise MonitoringCellConflict(
                    "monitoring cell result lineage mismatch: " + ",".join(lineage_mismatches)
                )
            confirmed_source = str(cell["keyword_source"]) in {"confirmed", "contract"}
        else:
            confirmed_source = lineage_payload.get("keyword_source") in {"confirmed", "contract"}
        stored_keyword_id = None if confirmed_source else keyword_id
        stored_confirmed_keyword_id = keyword_id if confirmed_source else None
        from psycopg2.extras import Json
        from services.monitoring_identity_review import (
            PENDING_MENTION_TYPE,
            PENDING_RESPONSE_STATUS,
            PENDING_REVIEW_STATE,
            bounded_identity_candidates,
            build_evidence_hash,
            is_pending_identity_result,
        )
        pending_identity = is_pending_identity_result({
            "identity_review_state": identity_review_state,
            "response_status": lineage_payload.get("response_status"),
            "mention_type": mention_type,
        })
        identity_review_state = PENDING_REVIEW_STATE if pending_identity else identity_review_state
        candidates = bounded_identity_candidates(identity_candidates)
        evidence_snippet = str(identity_evidence_snippet or "")[:2000]
        evidence_hash = None
        if pending_identity:
            if not identity_brand_id:
                raise ValueError("pending identity result requires identity_brand_id")
            lineage_payload["response_status"] = PENDING_RESPONSE_STATUS
            lineage_payload["target_outcome"] = "entity_ambiguous"
            mention_type = PENDING_MENTION_TYPE
            is_detected = False
            evidence_hash = build_evidence_hash(
                brand_id=int(identity_brand_id),
                platform=platform,
                keyword=keyword,
                candidates=candidates,
                evidence_snippet=evidence_snippet,
                full_response=full_response,
            )
        # [GEO-R2-CAN-016] full_response/search_citations 列是 TEXT(无长度限制)· 不再应用层截断
        # · 此前只存前 8000 字却把 response_char_count 设为原始长度 → 下游读到截断证据但
        #   char_count 仍广告原始大小 · 超长尾部证据静默丢失 · 现整存使二者一致
        # · response_snippet 保留 [:500](本就是"摘要"语义 · 非完整证据)
        cursor.execute("""
            INSERT INTO monitoring_results
            (task_id, keyword_id, confirmed_keyword_id, keyword, platform, is_detected, mention_type,
             response_snippet, full_response, response_char_count, search_citations,
             sent_question_snapshot, keyword_source, keyword_type,
             question_family, question_family_version, keyword_source_id,
             keyword_resolver_status, provider, model, model_revision,
             model_revision_unknown_reason, surface, search_mode, response_status,
             target_brand_snapshot, target_entity_snapshot, target_outcome,
             outcome_resolver_version, outcome_resolver_confidence, lineage_version,
             lineage_status, lineage_error_reason, provider_request_id, sent_at,
             identity_review_state, identity_brand_id, identity_candidates,
             identity_evidence_snippet, identity_evidence_hash,
             competitors_mentioned)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            task_id, stored_keyword_id, stored_confirmed_keyword_id,
            keyword, platform, int(is_detected), mention_type,
            response_snippet[:500], full_response, response_char_count,
            search_citations if search_citations else "",
            lineage_payload["sent_question_snapshot"], lineage_payload["keyword_source"],
            lineage_payload["keyword_type"], lineage_payload["question_family"],
            lineage_payload["question_family_version"], keyword_id,
            lineage_payload["keyword_resolver_status"], lineage_payload["provider"],
            lineage_payload["model"], lineage_payload["model_revision"],
            lineage_payload["model_revision_unknown_reason"], lineage_payload["surface"],
            lineage_payload["search_mode"], lineage_payload["response_status"],
            lineage_payload["target_brand"], lineage_payload["target_entity"],
            lineage_payload["target_outcome"], lineage_payload["resolver_version"],
            lineage_payload["resolver_confidence"], LINEAGE_VERSION,
            lineage_payload["lineage_status"], lineage_payload["lineage_error_reason"],
            lineage_payload["request_id"] or None, lineage_payload["sent_at"] or None,
            identity_review_state, identity_brand_id,
            Json(list(candidates)), evidence_snippet or None, evidence_hash,
            Json(competitors_mentioned if isinstance(competitors_mentioned, list) else []),
        ))

        result_id = cursor.fetchone()["id"]

        if cell_id is not None:
            cell_state = "pending_identity" if pending_identity else "succeeded"
            cursor.execute(
                """
                UPDATE public.monitoring_run_cells
                   SET state=%s, result_id=%s, provider_request_id=%s,
                       claim_token=NULL, claim_until=NULL,
                       error_code=NULL, error_message=NULL,
                       completed_at=NOW(), updated_at=NOW()
                 WHERE id=%s AND state='running' AND claim_token=%s
                """,
                (
                    cell_state, int(result_id), lineage_payload["request_id"] or None,
                    int(cell_id), str(cell_claim_token),
                ),
            )
            if cursor.rowcount != 1:
                raise MonitoringCellConflict("monitoring cell result completion raced")

            # [防御型 GEO 包F ① · 2026-08-23] 接线点②:成功收口。
            #
            # 映射**逐值**对应,不是"差不多":
            #   succeeded        → answered
            #   pending_identity → entity_ambiguous
            # 🔴 pending_identity 既不能压成 not_mentioned(§6.3),也不能抬成
            #    answered —— 抬上去会让"AI 说的可能不是你"在客户卡上消失。
            # 🔴 observed_* 是**收口时才知道**的真 lineage(尤其 model_revision:
            #    平台只在回答里带版本,派发前拿不到)。这几列不参与 attempt 身份,
            #    所以在这里写入不改变任何唯一性;不写的话账本永远只有
            #    "计划发哪个模型",拿不到"实际跑的是哪个版本" —— 而后者才是
            #    保真度对账(包F ⑥ 的保真锁)要的那一位。
            from services.defensive_geo.monitoring.run_ledger_bridge import (
                close_for_result,
            )
            cursor.execute(
                "SELECT plan_hash FROM public.monitoring_run_cells WHERE id=%s",
                (int(cell_id),))
            _plan_row = cursor.fetchone()
            close_for_result(
                cursor,
                plan_cell_id=str((_plan_row or {}).get("plan_hash") or ""),
                cell_state=cell_state,
                monitoring_result_id=int(result_id),
                observed_provider=lineage_payload["provider"],
                # [工单 E3-4 · P1-9b · 2026-08-26] **只有真回显**才当 observed 落库。
                #
                # 这一格原来无条件把 lineage 的 model 传成 observed_model,而那个值
                # 在生产上恒是**计划值**(provider adapter 还没把响应里的回显带回来)。
                # 于是账本里 `actual_model` 被一个计划值"确认"成了实际值 ——
                # 而它同时是保真度对账与计价的取数口。
                #
                # 传 None 时 `attempt_ledger.close_attempt` 走
                # `COALESCE(NULLIF(%s,''), actual_model)` ⇒ 保留 open 时写下的
                # 那个计划值,**不假装它被证实过**。少一次覆盖,不是少一条记录。
                observed_model=(
                    lineage_payload["model"]
                    if lineage_payload.get("model_source") == "provider_echo"
                    else None),
                observed_model_revision=lineage_payload["model_revision"],
                observed_surface=lineage_payload["surface"],
                observed_search_mode=lineage_payload["search_mode"],
            )

        # P0-3 · 同事务写 token usage placeholder · 失败不阻断 monitoring 主流程
        # [GEO-R2-CAN-004] 用 SAVEPOINT 隔离辅助写入 · 失败仅 ROLLBACK TO savepoint
        # · 否则 aux INSERT 异常会 abort 整个事务 → 后续 conn.commit() 被降级为 ROLLBACK
        # · 会丢弃已 RETURNING 的主 monitoring_results 行 · 但 result_id 仍被当成功返回(幽灵行)
        cursor.execute("SAVEPOINT sp_token_usage")
        try:
            in_t, out_t, cost = _estimate_token_cost_placeholder(platform, keyword, full_response)
            cursor.execute("""
                INSERT INTO monitoring_token_usage
                (task_id, platform, input_tokens, output_tokens, estimated_cost)
                VALUES (%s, %s, %s, %s, %s)
            """, (task_id, platform, in_t, out_t, cost))
            cursor.execute("RELEASE SAVEPOINT sp_token_usage")
        except Exception as _tex:
            # [GEO-R2-CAN-004] 回滚到 savepoint · 保住主 monitoring_results 行 · 只 log 不阻断
            cursor.execute("ROLLBACK TO SAVEPOINT sp_token_usage")
            cursor.execute("RELEASE SAVEPOINT sp_token_usage")
            print(f"[TokenUsage-Placeholder] task_id={task_id} platform={platform} 写入异常(非阻塞): {_tex}")

        conn.commit()
        conn.close()

        return result_id
    finally:
        try:
            conn.close()
        except Exception: pass


def batch_save_results(results: List[Dict[str, Any]]) -> int:
    """批量保存结果"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        for r in results:
            full_resp = r.get("full_response", "")
            response_char_count = len(full_resp) if full_resp else 0
            from services.monitoring_lineage import LINEAGE_VERSION, normalize_lineage_payload
            lineage_payload = normalize_lineage_payload(
                r.get("lineage") or r,
                platform=r.get("platform"),
                keyword=r.get("keyword"),
                full_response=full_resp,
                is_detected=r.get("is_detected", False),
                mention_type=r.get("mention_type", "none"),
                search_citations=r.get("search_citations", ""),
            )
            boundary = r.get("lineage") if isinstance(r.get("lineage"), dict) else r
            boundary_complete = all(
                str(boundary.get(key) or "").strip()
                for key in ("sent_question_snapshot", "provider", "model", "surface")
            )
            if not boundary_complete:
                lineage_payload["lineage_status"] = "explicit_unknown"
                lineage_payload["lineage_error_reason"] = (
                    "legacy_batch_caller_no_provider_boundary_snapshot"
                )
            from psycopg2.extras import Json
            from services.monitoring_identity_review import (
                PENDING_MENTION_TYPE,
                PENDING_RESPONSE_STATUS,
                PENDING_REVIEW_STATE,
                bounded_identity_candidates,
                build_evidence_hash,
                is_pending_identity_result,
            )
            pending_identity = is_pending_identity_result({**r, **lineage_payload})
            identity_review_state = (
                PENDING_REVIEW_STATE if pending_identity
                else (r.get("identity_review_state") or "not_required")
            )
            identity_brand_id = r.get("identity_brand_id") or r.get("brand_id")
            candidates = bounded_identity_candidates(r.get("identity_candidates"))
            evidence_snippet = str(r.get("identity_evidence_snippet") or "")[:2000]
            evidence_hash = None
            if pending_identity:
                if not identity_brand_id:
                    raise ValueError("pending identity result requires identity_brand_id")
                lineage_payload["response_status"] = PENDING_RESPONSE_STATUS
                lineage_payload["target_outcome"] = "entity_ambiguous"
                r["mention_type"] = PENDING_MENTION_TYPE
                r["is_detected"] = False
                evidence_hash = build_evidence_hash(
                    brand_id=int(identity_brand_id),
                    platform=r.get("platform", ""),
                    keyword=r.get("keyword", ""),
                    candidates=candidates,
                    evidence_snippet=evidence_snippet,
                    full_response=full_resp,
                )
            # [GEO-R2-CAN-016] full_response 列是 TEXT · 不再截断前 8000 字 · 与单条写入路径一致
            # · 保证存储内容与 response_char_count 一致 · 避免尾部证据静默丢失
            cursor.execute("""
                INSERT INTO monitoring_results
                (task_id, keyword_id, keyword, platform, is_detected, mention_type,
                 response_snippet, full_response, response_char_count,
                 search_citations,
                 sent_question_snapshot, keyword_source, keyword_type,
                 question_family, question_family_version, keyword_source_id,
                 keyword_resolver_status, provider, model, model_revision,
                 model_revision_unknown_reason, surface, search_mode, response_status,
                 target_brand_snapshot, target_entity_snapshot, target_outcome,
                 outcome_resolver_version, outcome_resolver_confidence, lineage_version,
                 lineage_status, lineage_error_reason, provider_request_id, sent_at,
                 identity_review_state, identity_brand_id, identity_candidates,
                 identity_evidence_snippet, identity_evidence_hash,
                 competitors_mentioned)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                r.get("task_id"),
                r.get("keyword_id"),
                r.get("keyword", ""),
                r.get("platform", ""),
                int(r.get("is_detected", False)),
                r.get("mention_type", "none"),
                r.get("response_snippet", "")[:500],
                full_resp,
                response_char_count,
                r.get("search_citations", ""),
                lineage_payload["sent_question_snapshot"], lineage_payload["keyword_source"],
                lineage_payload["keyword_type"], lineage_payload["question_family"],
                lineage_payload["question_family_version"], r.get("keyword_id"),
                lineage_payload["keyword_resolver_status"], lineage_payload["provider"],
                lineage_payload["model"], lineage_payload["model_revision"],
                lineage_payload["model_revision_unknown_reason"], lineage_payload["surface"],
                lineage_payload["search_mode"], lineage_payload["response_status"],
                lineage_payload["target_brand"], lineage_payload["target_entity"],
                lineage_payload["target_outcome"], lineage_payload["resolver_version"],
                lineage_payload["resolver_confidence"], LINEAGE_VERSION,
                lineage_payload["lineage_status"], lineage_payload["lineage_error_reason"],
                lineage_payload["request_id"] or None, lineage_payload["sent_at"] or None,
                identity_review_state, identity_brand_id,
                Json(list(candidates)), evidence_snippet or None, evidence_hash,
                Json(r.get("competitors_mentioned") or []),
            ))

            cursor.execute("SAVEPOINT sp_batch_token_usage")
            try:
                in_t, out_t, cost = _estimate_token_cost_placeholder(
                    r.get("platform", ""), r.get("keyword", ""), full_resp
                )
                cursor.execute(
                    """
                    INSERT INTO public.monitoring_token_usage
                        (task_id, platform, input_tokens, output_tokens, estimated_cost)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (r.get("task_id"), r.get("platform", ""), in_t, out_t, cost),
                )
                cursor.execute("RELEASE SAVEPOINT sp_batch_token_usage")
            except Exception:
                cursor.execute("ROLLBACK TO SAVEPOINT sp_batch_token_usage")
                cursor.execute("RELEASE SAVEPOINT sp_batch_token_usage")
    
        conn.commit()
        count = len(results)
        conn.close()
        return count
    finally:
        try:
            conn.close()
        except Exception: pass


def get_task_results(task_id: int) -> List[Dict[str, Any]]:
    """获取任务的所有结果"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM monitoring_results WHERE task_id = %s ORDER BY tested_at",
            (task_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_aggregate_task_results(task_id: int) -> List[Dict[str, Any]]:
    """Return only result cells eligible for customer metrics and downstream learning."""
    from services.monitoring_identity_review import aggregate_eligible_sql

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT *
              FROM public.monitoring_results mr
             WHERE mr.task_id = %s
               AND {aggregate_eligible_sql('mr')}
             ORDER BY mr.tested_at
            """,
            (int(task_id),),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


class MonitoringIdentityReviewConflict(RuntimeError):
    pass


class MonitoringIdentityReviewNotFound(LookupError):
    pass


def get_monitoring_identity_review_answer(result_id: int) -> Optional[Dict[str, Any]]:
    """Fetch one pending review's **full** answer plus the anchors needed to locate it.

    [工单 2026-08-03 ①] 卡片上只显示得到 ``response_snippet``(实测恒为全文前 500 字的
    逐字前缀),全文躺在 ``full_response`` 里没有入口。本函数是「查看完整回答」的唯一数据源:
    **按 result_id 单独拉**,不塞进列表接口 —— 列表一次最多 200 条,每条全文 700~1700 字,
    塞进去等于把一个从不被读的大字段挂在每次轮询上(面板 30s 轮询一次)。

    🔴 定位锚点是**算出来的,不是猜的**。生产实测(2026-08-03,613 行全表):
    ``identity_evidence_snippet`` **恒为 NULL** —— ±420 证据窗口从来没落过库。
    所以这里不能只认证据窗口,否则真实数据上永远定位不到。两级锚点:
      1. 证据窗口有值且能在全文里逐字找到 → 用它的真实偏移(未来窗口落库后自动生效);
      2. 否则退到 ``response_snippet`` 的边界 —— 即"卡片里已经看过的部分"到此为止,
         让用户直接跳到**之前看不到的那段**。这才是他点开全文的目的。
    两者都定位不了时返回 ``anchor='none'``,前端照常展示全文、只是不高亮(不阻断)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, task_id, identity_brand_id, identity_review_state,
                   full_response, response_snippet, identity_evidence_snippet
              FROM public.monitoring_results
             WHERE id = %s
            """,
            (int(result_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def build_identity_answer_anchor(
    full_response: Optional[str],
    response_snippet: Optional[str],
    evidence_snippet: Optional[str],
) -> Dict[str, Any]:
    """Compute the highlight span inside ``full_response``.  Pure function, unit-testable."""
    full = str(full_response or "")
    if not full:
        return {"anchor": "none", "start": None, "end": None}

    window = str(evidence_snippet or "")
    if window:
        at = full.find(window)
        if at >= 0:
            return {"anchor": "evidence_window", "start": at, "end": at + len(window)}

    seen = str(response_snippet or "")
    # 只有当片段确实是全文前缀时,"已看过的部分到此为止"才成立;
    # 不是前缀就别硬套一个边界出来 —— 那会把用户定位到一个没有意义的位置。
    if seen and full.startswith(seen) and len(seen) < len(full):
        return {"anchor": "seen_prefix", "start": len(seen), "end": len(full)}

    return {"anchor": "none", "start": None, "end": None}


def list_pending_monitoring_identity_reviews(
    brand_id: int,
    *,
    limit: int = 100,
    before_tested_at=None,
    before_id: int = None,
) -> List[Dict[str, Any]]:
    """Return durable unresolved identity cells for one authorized brand."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, task_id, keyword_id, keyword, platform,
                   response_snippet, identity_candidates,
                   identity_evidence_snippet, identity_evidence_hash,
                   identity_decision_version, tested_at
              FROM public.monitoring_results
             WHERE identity_brand_id = %s
               AND identity_review_state = 'pending'
               AND response_status = 'brand_identity_unresolved'
               AND (%s::timestamptz IS NULL OR (tested_at,id) < (%s::timestamptz,%s))
             ORDER BY tested_at DESC, id DESC
             LIMIT %s
            """,
            (
                int(brand_id), before_tested_at, before_tested_at,
                int(before_id) if before_id is not None else 0,
                max(1, min(int(limit), 200)),
            ),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def decide_monitoring_identity_review(
    *,
    result_id: int,
    brand_id: int,
    actor_user_id: int,
    action: str,
    selected_name: str,
    expected_version: int,
    evidence_hash: str,
    request_id: str,
    actor_is_admin: bool = False,
) -> Dict[str, Any]:
    """Apply one human decision atomically without provider or billing calls."""
    from psycopg2.extras import Json
    from services.brand_identity_resolver import (
        normalize_brand_name,
        normalize_confirmed_display_names,
        parse_brand_display_names,
    )

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"yes", "no", "custom"}:
        raise ValueError("无效的确认动作")
    normalized_name = normalize_brand_name(selected_name)
    answer_absent = normalized_action == "no" and not normalized_name
    if not normalized_name and not answer_absent:
        raise ValueError("请选择候选名称或填写正确名称")
    if answer_absent:
        selected_name = ""

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, identity_brand_id, identity_candidates,
                   identity_evidence_hash, identity_review_state,
                   identity_decision_version, full_response, response_snippet
              FROM public.monitoring_results
             WHERE id = %s
             FOR UPDATE
            """,
            (int(result_id),),
        )
        row = cur.fetchone()
        if not row or int(row.get("identity_brand_id") or 0) != int(brand_id):
            raise MonitoringIdentityReviewNotFound("待确认记录不存在")

        cur.execute(
            "SELECT id, owner_user_id, name, company_name, brand_display_names FROM public.brands "
            "WHERE id = %s AND (is_deleted IS NULL OR is_deleted = FALSE) FOR UPDATE",
            (int(brand_id),),
        )
        brand = cur.fetchone()
        if not brand:
            raise MonitoringIdentityReviewNotFound("品牌不存在")
        cur.execute(
            """
            SELECT 1
              FROM public.user_roles ur
              JOIN public.roles r ON r.id = ur.role_id
             WHERE ur.user_id = %s AND r.name = 'admin'
             LIMIT 1
            """,
            (int(actor_user_id),),
        )
        live_is_admin = bool(cur.fetchone())
        if not live_is_admin and int(brand.get("owner_user_id") or 0) != int(actor_user_id):
            cur.execute(
                "SELECT 1 FROM public.user_clients WHERE user_id = %s AND brand_id = %s",
                (int(actor_user_id), int(brand_id)),
            )
            live_assignment = cur.fetchone()
            if not live_assignment:
                cur.execute(
                    """
                    SELECT pg_catalog.to_regclass('public.organization_memberships') IS NOT NULL
                           AND pg_catalog.to_regclass('public.organization_brand_assignments') IS NOT NULL
                           AS ready
                    """
                )
                organization_ready = bool(cur.fetchone()["ready"])
                if organization_ready:
                    cur.execute(
                        """
                        SELECT 1
                          FROM public.organization_memberships membership
                          JOIN public.organization_brand_assignments assignment
                            ON assignment.membership_id = membership.id
                           AND assignment.organization_id = membership.organization_id
                         WHERE membership.user_id = %s
                           AND membership.status = 'active'
                           AND assignment.brand_id = %s
                           AND assignment.status = 'active'
                         LIMIT 1
                        """,
                        (int(actor_user_id), int(brand_id)),
                    )
                    live_assignment = cur.fetchone()
            if not live_assignment:
                raise PermissionError("品牌访问权已变化，请刷新后重试")

        cur.execute(
            """
            SELECT event_id, result_id, brand_id, action, selected_name,
                   normalized_name, evidence_hash, result_version_before,
                   result_version_after
              FROM public.monitoring_identity_decision_events
             WHERE request_id = %s::uuid
            """,
            (request_id,),
        )
        prior = cur.fetchone()
        if prior:
            same_request = (
                int(prior["result_id"]) == int(result_id)
                and int(prior["brand_id"]) == int(brand_id)
                and prior["action"] == normalized_action
                and normalize_brand_name(prior["selected_name"]) == normalized_name
                and prior["evidence_hash"] == evidence_hash
                and int(prior["result_version_before"]) == int(expected_version)
            )
            if not same_request:
                raise MonitoringIdentityReviewConflict("请求编号已用于其他确认")
            conn.rollback()
            return {"status": "idempotent", **dict(prior)}
        if row.get("identity_review_state") != "pending":
            cur.execute(
                """
                SELECT event_id, result_id, action, selected_name, result_version_after
                  FROM public.monitoring_identity_decision_events
                 WHERE request_id = %s::uuid
                """,
                (request_id,),
            )
            prior = cur.fetchone()
            if prior and int(prior["result_id"]) == int(result_id):
                conn.rollback()
                return {"status": "idempotent", **dict(prior)}
            raise MonitoringIdentityReviewConflict("该记录已经处理")
        if row.get("identity_evidence_hash") != evidence_hash:
            raise MonitoringIdentityReviewConflict("证据已变化，请刷新后重试")
        if int(row.get("identity_decision_version") or 0) != int(expected_version):
            raise MonitoringIdentityReviewConflict("确认状态已变化，请刷新后重试")

        candidates = tuple(
            str(value).strip()
            for value in (row.get("identity_candidates") or [])
            if str(value).strip()
        )
        candidate_map = {normalize_brand_name(value): value for value in candidates}
        if normalized_action == "yes" or (normalized_action == "no" and not answer_absent):
            if normalized_name not in candidate_map:
                raise MonitoringIdentityReviewConflict("候选名称已变化，请刷新后重试")
            selected_name = candidate_map[normalized_name]
        elif normalized_action == "custom":
            normalized_custom_names = normalize_confirmed_display_names([selected_name])
            if not normalized_custom_names:
                raise ValueError("请填写有效的品牌名称")
            selected_name = normalized_custom_names[0]
            normalized_name = normalize_brand_name(selected_name)

        decision = "negative" if normalized_action == "no" else "positive"
        if answer_absent:
            decision_names = ()
            decision_scope = "full_answer_absent"
        elif decision == "negative":
            decision_names = tuple(candidate_map.items())
            decision_scope = "candidate_set_rejected"
        else:
            decision_names = ((normalized_name, selected_name),)
            decision_scope = (
                "custom_name_confirmed"
                if normalized_action == "custom"
                else "candidate_confirmed"
            )
        trusted_names = {
            normalize_brand_name(value)
            for value in (
                brand.get("name") or "",
                brand.get("company_name") or "",
                *parse_brand_display_names(brand.get("brand_display_names")),
            )
            if normalize_brand_name(value)
        }
        cur.execute(
            """
            SELECT canonical_name, alias
              FROM public.brand_aliases
             WHERE brand_id = %s AND source = 'manual'
            """,
            (int(brand_id),),
        )
        manual_alias_names: list[str] = []
        for alias_row in cur.fetchall():
            for value in (
                alias_row.get("canonical_name") or "",
                alias_row.get("alias") or "",
            ):
                if normalize_brand_name(value):
                    trusted_names.add(normalize_brand_name(value))
                    manual_alias_names.append(str(value))
        # [R1 修复 2026-08-12] 守卫的判别位必须落在**这张卡上真实存在的候选**
        # (candidate_map),不能落在 decision_names 上:absent 分支把 decision_names
        # 置空了,`any()` 对空集恒 False ⇒ 守卫在 absent 路径结构性失效 —— 同一个
        # 运营意图走 `no + 带名` 被拦、走 `no + 空名`(「没有出现」)却能一键把
        # 「回答里明确提到本品牌可信名」的结果写成"未提及"并计进客户可见出现率,
        # 且该写入无撤销路径。
        # 🔴 这不会把「整段全在讲同行」的死锁修回来:那时候选是同行名,与
        #    trusted_names 无交集 ⇒ 守卫不触发,「没有出现」照常可用。被拦的只有
        #    「候选里含品牌自己的可信名」这一个可疑格。
        guarded_name_keys = (
            tuple(candidate_map.keys())
            if answer_absent
            else tuple(name_key for name_key, _ in decision_names)
        )
        if decision == "negative" and any(
            name_key in trusted_names for name_key in guarded_name_keys
        ):
            raise MonitoringIdentityReviewConflict(
                # absent 那条必须告诉运营下一步怎么走:界面上并没有"整组"这个动作,
                # 只回旧文案他不知道该干嘛。
                "候选中包含当前品牌的可信名称，不能直接记为未出现；请逐条判断该候选"
                if answer_absent
                else "候选中包含当前品牌的可信名称，不能整组标记为不是"
            )
        existing_decisions: dict[str, str] = {}
        if decision_names:
            cur.execute(
                """
                SELECT normalized_name, decision
                  FROM public.monitoring_identity_name_decisions
                 WHERE brand_id = %s AND normalized_name = ANY(%s)
                 FOR UPDATE
                """,
                (int(brand_id), [name_key for name_key, _ in decision_names]),
            )
            existing_decisions = {
                existing_row["normalized_name"]: existing_row["decision"]
                for existing_row in cur.fetchall()
            }
        if any(
            existing_decisions.get(name_key) not in (None, decision)
            for name_key, _ in decision_names
        ):
            raise MonitoringIdentityReviewConflict("该名称已有相反确认，无法覆盖")

        request_namespace = uuid.UUID(request_id)
        for index, (decision_name_key, decision_display_name) in enumerate(decision_names):
            projection_request_id = (
                request_id
                if index == 0
                else str(uuid.uuid5(request_namespace, decision_name_key))
            )
            cur.execute(
                """
                INSERT INTO public.monitoring_identity_name_decisions
                    (brand_id, normalized_name, display_name, decision, decision_version,
                     evidence_hash, decided_by, request_id)
                VALUES (%s, %s, %s, %s, 1, %s, %s, %s::uuid)
                ON CONFLICT (brand_id, normalized_name) DO UPDATE
                   SET display_name = EXCLUDED.display_name,
                       evidence_hash = EXCLUDED.evidence_hash,
                       decided_by = EXCLUDED.decided_by,
                       request_id = EXCLUDED.request_id,
                       updated_at = CURRENT_TIMESTAMP,
                       decision_version = public.monitoring_identity_name_decisions.decision_version + 1
                 WHERE public.monitoring_identity_name_decisions.decision = EXCLUDED.decision
                """,
                (
                    int(brand_id), decision_name_key, decision_display_name, decision,
                    evidence_hash, int(actor_user_id), projection_request_id,
                ),
            )
            if cur.rowcount != 1:
                raise MonitoringIdentityReviewConflict("名称确认发生冲突")

        updated_display_names = list(parse_brand_display_names(brand.get("brand_display_names")))
        if decision == "positive":
            display_names = normalize_confirmed_display_names([*updated_display_names, selected_name])
            updated_display_names = list(display_names)
            payload = json.dumps(list(display_names), ensure_ascii=False)
            cur.execute(
                """
                UPDATE public.brands
                   SET brand_display_names = %s, updated_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                """,
                (payload, int(brand_id)),
            )
            cur.execute(
                """
                UPDATE public.client_profiles
                   SET brand_display_names = %s, updated_at = CURRENT_TIMESTAMP
                 WHERE brand_id = %s
                   AND (is_deleted = 0 OR is_deleted IS NULL)
                """,
                (payload, int(brand_id)),
            )

        next_version = int(expected_version) + 1
        review_state = "rejected" if decision == "negative" else "confirmed"

        # [工单 M-1 ② 2026-07-28] 确认后用**已确认口径**对本条原文重新本地判定再计入,
        # 不再按动作一刀切。resolve_local 是确定性判定、物理零 provider 调用(零算力,
        # 既有锁 test_rejudge_is_provider_free 同一口径);身份在事务内手工装配,
        # 本次新确认/新否定的名字立即生效:
        #   YES → 计入检出;NO → 按未提及计入;UNKNOWN → 仅 action=yes(人工背书
        #   "这段文字就是本品牌")回落计入 —— custom 名不在本条原文时不得凭空计入。
        from services.brand_identity_resolver import (
            BrandIdentity,
            BrandIdentityResolver,
            BrandVerdict,
        )

        cur.execute(
            """
            SELECT display_name, decision
              FROM public.monitoring_identity_name_decisions
             WHERE brand_id = %s
            """,
            (int(brand_id),),
        )
        name_decision_rows = cur.fetchall()
        rejudge_identity = BrandIdentity(
            brand_id=int(brand_id),
            canonical_names=tuple(
                str(value) for value in (brand.get("name"), brand.get("company_name"))
                if str(value or "").strip()
            ),
            trusted_aliases=tuple(
                str(value) for value in (
                    *updated_display_names,
                    *manual_alias_names,
                    *(
                        nd_row.get("display_name") or ""
                        for nd_row in name_decision_rows
                        if nd_row.get("decision") == "positive"
                    ),
                )
                if str(value or "").strip()
            ),
            rejected_aliases=tuple(
                str(nd_row.get("display_name") or "")
                for nd_row in name_decision_rows
                if nd_row.get("decision") == "negative"
                and str(nd_row.get("display_name") or "").strip()
            ),
        )
        answer_text = str(row.get("full_response") or row.get("response_snippet") or "")
        if answer_absent:
            # 用户看过整段回答并明确确认客户品牌未出现。这是本条结果的
            # 人工终判，不是对某个候选别名作负面沉淀，因此不再让本地
            # 别名解析器覆盖该结论，也不会污染后续回答的品牌词典。
            rejudge = None
            rejudge_verdict = "human_confirmed_absent"
            rejudge_reason = "full_answer_reviewed_absent"
            rejudge_method = "human_full_answer_absence"
            is_detected = False
        else:
            rejudge = (
                BrandIdentityResolver(rejudge_identity).resolve_local(answer_text)
                if answer_text.strip()
                else None
            )
            rejudge_verdict = rejudge.verdict.value if rejudge else "skipped_empty_answer"
            rejudge_reason = rejudge.reason if rejudge else ""
            rejudge_method = rejudge.method if rejudge else ""
            if rejudge is not None and rejudge.verdict is BrandVerdict.YES:
                is_detected = True
            elif rejudge is not None and rejudge.verdict is BrandVerdict.NO:
                is_detected = False
            else:
                # UNKNOWN 兜底。⚠️ resolve_local 的短名/上下文护栏是给**自动推断**用的
                # (它会把"回答中提到了 X 的案例"判成 UNKNOWN),不能拿它推翻人工确认:
                #   · action=yes  → 候选本就是从这条回答里抽出来的,人已背书 → 计入;
                #   · action=custom → 人手填的名字**字面出现在本条原文**才计入
                #     (归一化后子串命中,绕开自动推断护栏);不在原文则不得凭空计入。
                literal_present = bool(
                    normalized_name
                    and normalized_name in normalize_brand_name(answer_text)
                )
                is_detected = normalized_action == "yes" or (
                    normalized_action == "custom" and literal_present
                )
        # [P0-3 · 2026-07-26 保持] 人工确认链最高只到 mentioned:重判只裁"有没有提到",
        # 不得顺手升档 recommended。
        from services.mention_vocabulary import MENTION_MENTIONED, MENTION_NONE

        mention_type = MENTION_MENTIONED if is_detected else MENTION_NONE
        target_outcome = "mentioned_only" if is_detected else "not_mentioned"
        cur.execute(
            """
            UPDATE public.monitoring_results
               SET identity_review_state = %s,
                   identity_decision_version = %s,
                   identity_resolved_at = CURRENT_TIMESTAMP,
                   identity_resolved_by = %s,
                   response_status = 'success',
                   is_detected = %s,
                   mention_type = %s,
                   target_outcome = %s
             WHERE id = %s
               AND identity_review_state = 'pending'
               AND identity_decision_version = %s
               AND identity_evidence_hash = %s
            """,
            (
                review_state, next_version, int(actor_user_id), int(is_detected),
                mention_type, target_outcome, int(result_id), int(expected_version), evidence_hash,
            ),
        )
        if cur.rowcount != 1:
            raise MonitoringIdentityReviewConflict("确认状态并发变化")

        # A human identity decision is also the durable terminal transition of
        # its execution cell. Keep the same immutable result_id; only the
        # pending_identity -> succeeded state edge is allowed by the DB trigger.
        cur.execute(
            "SELECT pg_catalog.to_regclass('public.monitoring_run_cells') AS relation"
        )
        if cur.fetchone()["relation"] is not None:
            cur.execute(
                """
                UPDATE public.monitoring_run_cells
                   SET state='succeeded', completed_at=COALESCE(completed_at, NOW()),
                       updated_at=NOW()
                 WHERE result_id=%s AND brand_id=%s AND state='pending_identity'
                 RETURNING task_id
                """,
                (int(result_id), int(brand_id)),
            )
            _resolved_cells = cur.fetchall()
            resolved_task_ids = [int(cell_row["task_id"]) for cell_row in _resolved_cells]

            # [防御型 GEO 包F ① · 2026-08-23] 接线点⑤(工单四点之外的第五点)。
            #
            # 人工把 pending_identity 确认成 succeeded 时,**追加**一条 answered
            # attempt —— 不改写②那一行。
            # 🔴 为什么必须接:②已经把这一格收成 entity_ambiguous,而终态行由
            #    trg_defgeo_attempt_terminal_immutable 结构性锁死(MON-03
            #    「不覆盖原始失败」)。不接这一跳的后果是**真实的数据错误**:
            #    客户已经人工确认过"这就是我家",而五卡上永远写着"身份待确认"。
            # 🔴 为什么是追加而不是改写:人工确认是一条**新事实**,不是
            #    "上一条判错了要改"。原始那条身份待定的观测必须原样留着。
            #    §6.1 canonical selection 里 answered(rank 0)优于
            #    entity_ambiguous(rank 1),所以卡上自然显示"认出来了"。
            # 🔴 provider_called=False:这一跳零 provider 调用(上面那段
            #    resolve_local 是确定性判定,零算力,既有锁
            #    test_rejudge_is_provider_free 同一口径)。于是它不进 attempted,
            #    而 attempted 仍由②那条 provider attempt 提供 —— 两个计数
            #    各自都对得上真实调用数(MON-11)。
            if _resolved_cells:
                from services.defensive_geo.monitoring.run_ledger_bridge import (
                    close_for_human_resolution,
                )
                # [工单 E3-1 · 2026-08-26] 多取一列 tenant_owner_user_id:
                # 这一跳过去把**操作者** actor_user_id 写进账本的
                # tenant_owner_user_id —— 管理员替客户确认身份时,那一行
                # attempt 就归到管理员名下了。actor 仍然进 request_hash。
                cur.execute(
                    "SELECT plan_hash, task_id, tenant_owner_user_id "
                    "  FROM public.monitoring_run_cells "
                    " WHERE result_id=%s AND brand_id=%s",
                    (int(result_id), int(brand_id)))
                for _cell_row in (cur.fetchall() or []):
                    close_for_human_resolution(
                        cur,
                        plan_cell_id=str(_cell_row.get("plan_hash") or ""),
                        monitoring_result_id=int(result_id),
                        actor_user_id=int(actor_user_id),
                        brand_id=int(brand_id),
                        run_authority_id=str(_cell_row.get("task_id") or ""),
                        tenant_owner_user_id=_cell_row.get("tenant_owner_user_id"),
                    )
        else:
            resolved_task_ids = []

        cur.execute(
            """
            INSERT INTO public.monitoring_identity_decision_events
                (result_id, brand_id, action, selected_name, normalized_name,
                 evidence_hash, result_version_before, result_version_after,
                 actor_user_id, request_id, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s)
            RETURNING event_id, decided_at
            """,
            (
                int(result_id), int(brand_id), normalized_action, selected_name,
                normalized_name, evidence_hash, int(expected_version), next_version,
                int(actor_user_id), request_id, Json({
                    "source": "monitoring_human_review",
                    "decision_scope": decision_scope,
                    "decision_name_count": len(decision_names),
                    "decision_names": [
                        display_name for _, display_name in decision_names
                    ],
                    # [M-1 ②] 本地重判审计面(零 provider 调用)
                    "rejudge_verdict": rejudge_verdict,
                    "rejudge_reason": rejudge_reason,
                    "rejudge_method": rejudge_method,
                    "counted_is_detected": bool(is_detected),
                }),
            ),
        )
        event = dict(cur.fetchone())
        conn.commit()
        for resolved_task_id in resolved_task_ids:
            try:
                refresh_monitoring_task_from_cells(resolved_task_id)
            except Exception:
                # The cell/result decision is already atomic and durable. A
                # subsequent task hydration also recomputes this projection.
                pass
        return {
            "status": "resolved",
            "result_id": int(result_id),
            "review_state": review_state,
            "decision_version": next_version,
            # [M-1 ①②] 前端回写用:确认后该条的计入结论(重判产物)
            "is_detected": bool(is_detected),
            "mention_type": mention_type,
            "rejudge_verdict": rejudge_verdict,
            **event,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_keyword_history(
    keyword_id: int,
    limit: int = 50
) -> List[Dict[str, Any]]:
    """获取词条的历史检测记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM monitoring_results WHERE COALESCE(confirmed_keyword_id, keyword_id) = %s ORDER BY tested_at DESC LIMIT %s",
            (keyword_id, limit)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_keyword_stats(keyword_id: int) -> Dict[str, Any]:
    """获取词条统计数据"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 总检测次数和命中次数
        from services.monitoring_identity_review import aggregate_eligible_sql
        eligible = aggregate_eligible_sql()
        cursor.execute(f"""
            SELECT
                COUNT(*) as total_tests,
                SUM(CASE WHEN is_detected = 1 THEN 1 ELSE 0 END) as detected_count
            FROM monitoring_results
            WHERE COALESCE(confirmed_keyword_id, keyword_id) = %s AND {eligible}
        """, (keyword_id,))
        row = cursor.fetchone()

        total = row["total_tests"] or 0
        detected = row["detected_count"] or 0

        # 各平台统计
        cursor.execute(f"""
            SELECT
                platform,
                COUNT(*) as tests,
                SUM(CASE WHEN is_detected = 1 THEN 1 ELSE 0 END) as detected
            FROM monitoring_results
            WHERE COALESCE(confirmed_keyword_id, keyword_id) = %s AND {eligible}
            GROUP BY platform
        """, (keyword_id,))
        platform_stats = {}
        for prow in cursor.fetchall():
            p_total = prow["tests"] or 0
            p_detected = prow["detected"] or 0
            platform_stats[prow["platform"]] = {
                "tests": p_total,
                "detected": p_detected,
                "rate": round(p_detected / p_total * 100, 1) if p_total > 0 else 0
            }
    
        conn.close()
    
        return {
            "total_tests": total,
            "detected_count": detected,
            "detection_rate": round(detected / total * 100, 1) if total > 0 else 0,
            "platform_stats": platform_stats
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 报告操作
# ==========================================

def save_report(
    brand_id: int = None,
    client_id: str = None,
    report_type: str = "",
    period_start: str = "",
    period_end: str = "",
    summary_data: dict = None,
    content: str = None,
    status: str = "draft",
    excel_path: str = None,
    pdf_path: str = None,
    # B.6 (CTO-15.9 session 3 · 2026-04-25): v2 报告 schema
    version: str = "v1",
    evidence_count: int = 0,
    modules_jsonb: dict = None,
    organization_identity=None,
) -> int:
    """保存报告记录（双写 brand_id + client_id）"""
    resolved_brand_id, client_id_compat = _resolve_id(brand_id, client_id)
    if summary_data is None:
        summary_data = {}

    modules_payload = json.dumps(modules_jsonb, ensure_ascii=False) if modules_jsonb else None

    conn = get_connection()
    try:
        cursor = conn.cursor()

        if organization_identity is not None:
            from services.organization_service import _lock_identity
            organization_identity = _lock_identity(cursor, organization_identity)
            organization_identity.require_active_organization()
            if organization_identity.is_member:
                organization_identity.require("reports.generate")
                cursor.execute(
                    """
                    SELECT 1 FROM organization_brand_assignments
                    WHERE organization_id=%s AND membership_id=%s
                      AND brand_id=%s AND status='active'
                    """,
                    (
                        organization_identity.organization_id,
                        organization_identity.membership_id,
                        resolved_brand_id,
                    ),
                )
                if not cursor.fetchone():
                    from services.organization_contract import OrganizationError
                    raise OrganizationError(
                        "ORG_BRAND_NOT_ASSIGNED",
                        "该客户未分配给当前员工",
                        http_status=403,
                    )
            operation_key = (
                f"organization-report:{organization_identity.organization_id}:"
                f"{organization_identity.membership_id or 0}:{resolved_brand_id}:"
                f"{client_id_compat}:{report_type}:{period_start}:{period_end}"
            )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (operation_key,),
            )
            cursor.execute(
                """
                SELECT id FROM monitoring_reports
                WHERE organization_id=%s
                  AND COALESCE(created_by_membership_id,0)=COALESCE(%s,0)
                  AND brand_id=%s AND COALESCE(client_id,'')=COALESCE(%s,'')
                  AND report_type=%s AND period_start=%s AND period_end=%s
                ORDER BY id DESC LIMIT 1
                """,
                (
                    organization_identity.organization_id,
                    organization_identity.membership_id,
                    resolved_brand_id,
                    client_id_compat,
                    report_type,
                    period_start,
                    period_end,
                ),
            )
            replay = cursor.fetchone()
            if replay:
                return int(replay["id"])

        cursor.execute("""
            INSERT INTO monitoring_reports
            (client_id, brand_id, report_type, period_start, period_end, summary_data,
             content, status, excel_path, pdf_path,
             version, evidence_count, modules_jsonb)
            VALUES (%s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s)
            RETURNING id
        """, (
            client_id_compat, resolved_brand_id, report_type, period_start, period_end,
            json.dumps(summary_data, ensure_ascii=False),
            content, status, excel_path, pdf_path,
            version, evidence_count, modules_payload,
        ))

        report_id = cursor.fetchone()["id"]
        if organization_identity is not None:
            from services.organization_artifacts import stamp_artifact
            stamp_artifact(
                cursor,
                organization_identity,
                artifact_type="monitoring_report",
                artifact_id=report_id,
                brand_id=resolved_brand_id,
                visibility="private",
            )
        if resolved_brand_id is not None:
            cursor.execute("SELECT owner_user_id FROM brands WHERE id=%s", (resolved_brand_id,))
            owner_row = cursor.fetchone()
            if owner_row and owner_row.get("owner_user_id") is not None:
                from services.notification_events import NotificationEventType, RecipientKind
                from services.notification_outbox import enqueue_notification_event

                enqueue_notification_event(
                    cursor,
                    event_type=NotificationEventType.REPORT_COMPLETED,
                    business_id=str(report_id),
                    terminal_state="completed",
                    recipient_user_id=int(owner_row["owner_user_id"]),
                    recipient_kind=RecipientKind.USER,
                    facts={
                        "business_no": f"REPORT-{report_id}",
                        "status": "报告已生成",
                        "occurred_at": datetime.now().isoformat(timespec="seconds"),
                        "summary": "请在报告管理页面查看或下载。",
                    },
                )
        conn.commit()
        conn.close()

        # [工单 2026-07-29 T1] 交付成功事件 ②「效果报告生成成功」→ 客户门户 token 续期。
        # 同上:commit 之后独立事务做,续期失败不影响报告已生成这一事实。
        renew_client_token_safe(
            _resolve_delivery_quote_id(
                client_id=client_id_compat, brand_id=resolved_brand_id,
            ),
            trigger="report_generated",
            reason=f"效果报告生成成功自动续期 report_id={report_id}",
        )
        return report_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_reports(
    brand_id: int = None,
    client_id: str = None,
    report_type: str = None,
    limit: int = 20
) -> List[Dict[str, Any]]:
    """获取报告列表（优先 brand_id 查询）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 确定查询条件
        if brand_id is not None:
            id_col, id_val = "brand_id", brand_id
        elif client_id is not None:
            id_col, id_val = "client_id", client_id
        else:
            conn.close()
            return []
    
        if report_type:
            cursor.execute(
                f"SELECT * FROM monitoring_reports WHERE {id_col} = %s AND report_type = %s ORDER BY created_at DESC LIMIT %s",
                (id_val, report_type, limit)
            )
        else:
            cursor.execute(
                f"SELECT * FROM monitoring_reports WHERE {id_col} = %s ORDER BY created_at DESC LIMIT %s",
                (id_val, limit)
            )
    
        rows = cursor.fetchall()
        conn.close()
    
        reports = []
        for row in rows:
            report = dict(row)
            if report.get("summary_data"):
                try:
                    report["summary_data"] = json.loads(report["summary_data"])
                except:
                    pass
            reports.append(report)
        return reports
    finally:
        try:
            conn.close()
        except Exception: pass


def get_report_by_id(report_id: int) -> Optional[Dict[str, Any]]:
    """获取单个报告详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitoring_reports WHERE id = %s", (report_id,))
        row = cursor.fetchone()
        conn.close()
    
        if row:
            report = dict(row)
            if report.get("summary_data"):
                try:
                    report["summary_data"] = json.loads(report["summary_data"])
                except:
                    pass
            return report
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


def update_report_content(
    report_id: int,
    content: str,
    # B1.1 (CTO-15.9 session 3 · 2026-04-25) · v2 灰度可观测 · 修原 1 列 update bug
    version: Optional[str] = None,
    evidence_count: Optional[int] = None,
    modules_jsonb: Optional[dict] = None,
) -> bool:
    """更新报告内容(编辑)

    B1.1 修复:旧版只 UPDATE content · 即使灰度走 v2 · DB 仍 version='v1' · 灰度审计/统计废
    新版可选传 version/evidence_count/modules_jsonb · v2 路径必须传以解锁可观测性
    向后兼容 · 老 v1 调用方零改动
    """
    set_parts = ["content = %s"]
    params: list = [content]

    if version is not None:
        set_parts.append("version = %s")
        params.append(version)
    if evidence_count is not None:
        set_parts.append("evidence_count = %s")
        params.append(int(evidence_count))
    if modules_jsonb is not None:
        set_parts.append("modules_jsonb = %s")
        params.append(json.dumps(modules_jsonb, ensure_ascii=False))

    params.append(report_id)
    sql = f"UPDATE monitoring_reports SET {', '.join(set_parts)} WHERE id = %s"

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def update_report_status(
    report_id: int,
    status: str,
    reviewed_by: str = None
) -> bool:
    """更新报告状态（审核流程）"""
    from datetime import datetime
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        if status == "reviewed":
            cursor.execute(
                "UPDATE monitoring_reports SET status = %s, reviewed_by = %s, reviewed_at = %s WHERE id = %s",
                (status, reviewed_by, datetime.now().isoformat(), report_id)
            )
        elif status == "sent":
            cursor.execute(
                "UPDATE monitoring_reports SET status = %s, sent_at = %s WHERE id = %s",
                (status, datetime.now().isoformat(), report_id)
            )
        else:
            cursor.execute(
                "UPDATE monitoring_reports SET status = %s WHERE id = %s",
                (status, report_id)
            )
    
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def get_all_reports(
    status: str = None,
    report_type: str = None,
    limit: int = 50,
    brand_id: int = None
) -> List[Dict[str, Any]]:
    """获取所有报告（管理后台用）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        query = "SELECT * FROM monitoring_reports WHERE 1=1"
        params = []

        if brand_id:
            query += " AND brand_id = %s"
            params.append(brand_id)

        if status:
            query += " AND status = %s"
            params.append(status)

        if report_type:
            query += " AND report_type = %s"
            params.append(report_type)

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
    
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        conn.close()
    
        reports = []
        for row in rows:
            report = dict(row)
            if report.get("summary_data"):
                try:
                    report["summary_data"] = json.loads(report["summary_data"])
                except:
                    pass
            reports.append(report)
        return reports
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 付费客户操作（读取quotes表）
# ==========================================

def get_paid_clients(limit: int = 100) -> List[Dict[str, Any]]:
    """获取付费客户列表（返回 brand_id）

    ── [WO_251 §4 2026-09-20] 行上两个「品牌现值」字段的契约 ───────────────
    🔴 这段是 `/api/monitoring/clients` 返回行的**唯一权威**描述。前端(窗口 A)按它实现,
       不要在各自的消费点再写一遍回落 —— 同一条规则写两处,迟早有一处跟不上,
       而两边各自看都"对",只有客户看到的那个名字是旧的。

    字段(**本函数的每一行一律附带**,不按调用方分支;已知消费方
    `frontend/src/pages/Monitoring/index.tsx` 与 `frontend/src/services/m3/deliveryApi.ts:136`):

      · ``brand_current_name``     品牌现名,取不到则报价快照名,都没有则 ``None``
      · ``brand_current_industry`` 品牌现行业,取不到则报价快照行业,都没有则 ``None``

    🔴 **三态,不是两态**(2026-09-20 A 在前端摸出来,我原先写成「对所有消费方一律附带」
       —— 轴划错了:我数的是**消费方**,而缺口在**另一个生产方**):
      · **键不存在(undefined)** = 这条链**不提供**现值 ⇒ 消费方按快照显示,**不要**落到显示默认值
      · ``None``              = 两级都查了、都空 ⇒ 用下面的显示默认值
      · 有值                   = 直接显示
    同一条 `/api/monitoring/clients` 路径上,**客户条的生产方共三处**(演示态整条路径被接管):
      ① 本函数(实时) · ② ``services/demo_access.py::_demo_clients`` 兜底分支自拼行
      ③ ``services/admin_cross_tenant_governance.py::_monitoring_snapshot`` 产出的 ``clients``
         (落成快照后由 ①② 之外的演示态读回)
    🔴 ②③ 都按「值为 None 就不放这个键」拼行 ⇒ **它们结构上表达不了 ``None`` 这一态**:
       将来谁给这两条链补上这两个字段,只要那次算出来是 ``None``,键就又消失了,
       三态在那条链上**塌回两态**,而且不报错、读数与"这条链不提供现值"完全一样。
       要补,必须先**绕开那个过滤**(显式允许 None)。判据里有一格专盯这一点
       (`test_a_registered_producer_cannot_emit_these_keys_through_a_none_filter`),
       今天两条链都不产这两个键 ⇒ 它静默;谁去补,谁当场被逼着处理。
       (2026-09-20 A 在核第三个生产方时指出;绕开过滤的正确写法判据会放行,不是路障。)
    且 ③ **已落盘的存量快照里根本没有这两个键** ——
    所以"键不存在"在演示态是**结构性不可消除**的,契约必须为它定义行为,而不是要求它消失。
    判据 `tests/monitoring_client_current_values_2026_09_20` 里有一格按这个形状枚举生产方:
    **再出现第四个生产方就会红**,逼它在这里登记并说明给不给这两个键。

    与既有的 ``brand_name`` / ``industry`` 是**两件事**:那两个是下单那一刻的快照,
    对账用,本单一个字没动。客户改了名或当初选错分类,快照都不会变
    (Owner 截图:报价 287 显示旧名 +「餐饮食品」,而品牌 19 现值是
     「揭阳阁揭阳中路雅栖酒店 · 酒店住宿…」——同一个客户两个名字两个行业)。

    「空」的定义(三种都算空,后端已统一处理):``NULL`` / ``""`` / 仅空白。
    所以 ``NULLIF(TRIM(...), '')``:**空值不算现值**——拿空的覆盖过去,
    是把"显示了旧的"换成"什么都不显示",那更糟。两级都空时**返 ``None``,不返空串**,
    让"没有值"和"值是空字符串"在前端不必再分辨一次。

    🔴 两级都空(``None``,**不含"键不存在"**)时的显示定论
       (前端照此实现,**收在一个共用 helper 里**,两个消费点不各写一份):
      · 名字 → ``品牌 #<brand_id>``(``brand_id`` 也为空时 → ``未命名客户``)
      · 行业 → ``未填写``
    这一段是**显示**默认值,不是数据回落:数据层如实说"没有",由前端决定怎么写给人看。
    ──────────────────────────────────────────────────────────────
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT
                q.id as quote_id,
                q.brand_id,
                q.brand_name,
                q.industry,
                q.city,
                q.status,
                q.tier,
                q.paid_amount,
                q.total_keywords,
                q.created_at,
                q.service_days,
                q.service_start_date,
                (SELECT COUNT(*) FROM confirmed_keywords ck WHERE ck.quote_id = q.id AND (ck.is_core IS NOT FALSE) AND COALESCE(ck.super_red_ocean, FALSE) = FALSE) as keyword_count,
                -- [WO_251 §4 2026-09-20] 客户条上显示的名字/行业来自**报价快照**,
                --   而快照记的是"下单那一刻前端选了什么"。客户改了名或当初选错了,
                --   快照都不会变 ⇒ 监测中心那一条与顶部品牌卡**两个名字、两个行业**
                --   (Owner 截图:报价 287 显示旧名 +「餐饮食品」,品牌 19 现值是
                --    「揭阳阁揭阳中路雅栖酒店 · 酒店住宿…」)。
                -- 🔴 两级都套 `NULLIF(TRIM(...), '')`:「空」= NULL / "" / 仅空白三种。
                --   品牌现值**为空不算现值**(空值覆盖过去 = 把"显示了旧的"换成"什么都不显示");
                --   快照也空时整列**返 NULL 而不是空串**,让前端不必再分辨一次"没有值 vs 空字符串"。
                --   回落只写这一处,前端不再写第二遍 —— 详见本函数 docstring 里的字段契约。
                COALESCE(NULLIF(TRIM(b.name), ''), NULLIF(TRIM(q.brand_name), '')) AS brand_current_name,
                COALESCE(NULLIF(TRIM(b.industry), ''), NULLIF(TRIM(q.industry), '')) AS brand_current_industry
            FROM quotes q
            LEFT JOIN brands b ON b.id = q.brand_id
            WHERE q.status IN ('paid', 'confirmed')
            ORDER BY q.created_at DESC
            LIMIT %s
        """, (limit,))

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def resolve_service_anchored_quote_ids_for_brand(brand_id: int) -> List[int]:
    """[2026-06-07 老板复审 · 监测 quote 范围 SSOT] 返回某 brand 下「已进入服务期」的 quote_id 列表。

    口径(唯一权威 · 与 get_client_keywords 本文件 ~1681 / api.monitoring_api._quote_rows_for_brand 完全一致):
        status = 'paid'
        OR (status = 'confirmed' AND COALESCE(service_start_date, paid_at) IS NOT NULL)

    🔴 任何监测路径禁止再自己写 `SELECT id FROM quotes WHERE brand_id = %s`(会漏服务锚过滤 →
       未付/未启动服务的 quote 偷跑监测扣算力 · 历史脏草稿 quote 误挡整个品牌监测)。统一走本 helper。
    """
    if not brand_id:
        return []
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT q.id FROM quotes q
            WHERE q.brand_id = %s
              AND {quote_service_anchor_condition_sql("q")}
            ORDER BY q.id DESC
            """,
            (brand_id,),
        )
        return [int(row["id"]) if not isinstance(row, tuple) else int(row[0]) for row in cursor.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def brand_has_confirmed_or_paid_quote(brand_id: int) -> bool:
    """[2026-06-07 老板复审] brand 是否存在任意 confirmed/paid quote(不论是否有有效服务锚)。

    用于区分两种"helper 返空"的情形,fallback 行为不同:
      - 【无任何 quote】→ 可 fallback 到 brand 级 extra_keywords(代理自助监测订阅·合法)
      - 【有 quote 但无有效服务锚】(脏草稿 / paid 已过期)→ 必须返回无可监测词条·不进引擎(禁 fallback)
    """
    if not brand_id:
        return False
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM quotes WHERE brand_id = %s AND status IN ('confirmed', 'paid') LIMIT 1",
            (brand_id,),
        )
        return cursor.fetchone() is not None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def is_quote_service_anchored(quote_id) -> bool:
    """[2026-06-07 老板复审 · 单 quote 服务锚口径 SSOT] 某 quote 是否在【有效服务期】可监测/可扣费。

    口径与 resolve_service_anchored_quote_ids_for_brand / _quote_rows_for_brand / get_client_keywords(~1681)完全一致:
        (status = 'paid'      AND service_status 非 cancelled/expired/inactive)
        OR (status = 'confirmed' AND COALESCE(service_start_date, paid_at) IS NOT NULL)

    所有"单 quote_id"入口(_resolve_quote_scope client_id 兼容路径 / run-stream gate)统一调本函数·禁裸用 quote_id 不校验。
    """
    if not quote_id:
        return False
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT 1
            FROM quotes q
            WHERE q.id = %s
              AND {quote_service_anchor_condition_sql("q")}
            LIMIT 1
            """,
            (int(quote_id),),
        )
        return cursor.fetchone() is not None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_client_keywords(quote_id: int) -> List[Dict[str, Any]]:
    """
    获取客户的所有监测词条（合并confirmed_keywords和extra_keywords）

    [Deploy-CTO 2026-05-26 根治 C] 同 brand 多 quote 聚合视图
    ─────────────────────────────────────────
    老板真根因:一个 brand 累积多个 quote(报价历史 · 重报价 · 续费)
      → 老板加 keyword 到 quote A · 但 UI selectedClient 是 quote B
      → 看不到 row · "提示已添加但列表中没有"
    修法:WHERE 反查 brand_id → 聚合 brand 下所有 quote 的 keyword
      用户视角:一个 brand 看所有 keyword(跨所有 quote · 跟用户 mental 一致)
      返字段加 quote_id · 前端可显示"来自 quote XXX"标注

    出现率算法（v2 - 滚动窗口 + 生效起点）：
    ─────────────────────────────────────────
    1. 只统计「首次检出日期」之后的数据（自动排除铺量期）
    2. 在此基础上，只取最近 7 天的监测记录计算出现率
    3. 如果从未检出过，显示 0% 并标记为"铺量中"
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 获取客户 brand_name
        cursor.execute("SELECT brand_name FROM quotes WHERE id = %s", (quote_id,))
        brand_row = cursor.fetchone()
        default_brand = brand_row["brand_name"] if brand_row else ""

        # 合并两个来源的词条，使用滚动窗口计算出现率
        # 核心逻辑：
        #   first_detected = 该关键词首次被检出的日期（铺量期结束标志）
        #   统计范围 = MAX(first_detected, 7天前) ~ 今天
        #   出现率 = 该范围内的 detected / total
        # [根治 C] WHERE quote_id IN (同 brand 所有 quote) 替代 WHERE quote_id = 单值
        pw_values = _platform_weight_values_sql()
        platform_key_expr = _platform_key_sql_expr("mr.platform")
        fallback_weight = 1.0 / max(len(PLATFORM_WEIGHTS), 1)
        from services.monitoring_identity_review import aggregate_eligible_sql
        eligible_result = aggregate_eligible_sql()
        eligible_mr = aggregate_eligible_sql("mr")
        cursor.execute(f"""
            WITH brand_quotes AS (
                -- 反查同 brand 所有 quote(根治 C 聚合视图)
                -- [Deploy-CTO 2026-05-26 P0 修过头] 必须加 status 过滤
                -- 之前 buggy:brand 下所有 9 个 quote(1 paid + 8 draft)的 keyword 全聚合 → 86 条
                -- 实际只有 paid quote 94 的 4 个 keyword 是用户付款监测的
                -- draft 状态的 quote 是历史报价试算 · 不该显示给监测页(用户没确认 / 没付款)
                -- [v1.3 2026-05-29 paid-only 收紧] 老板 P0 拍板:confirmed 未付不展示给客户
                -- [v17.1 2026-05-29 Deploy-CTO P0 救场] 老板真机 P0 事故:
                --   confirmed + paid_at 有值 = 已签合同+财务对账中(揭阳雅栖 / 岱林 / QZQZ 等 10 真客户)
                --   只过滤 confirmed + paid_at NULL 的草稿态(brand 6/428 类未付脏数据 case 仍隐藏)
                --   原:status = 'paid' · 全平台 20 brand / 28 quote / 21 monitored ck 从客户视图消失
                --   现:status = 'paid' OR (status = 'confirmed' AND paid_at IS NOT NULL)
                SELECT q.id, q.brand_id FROM quotes q
                WHERE q.brand_id = (SELECT brand_id FROM quotes WHERE id = %s)
                  -- [2026-06-23 续费履约口径] 可见性 gate 统一走服务锚 SSOT:
                  --   paid 日历过期但达标天数未满仍继续服务; cancelled/inactive 或已完成才排除。
                  AND {quote_service_anchor_condition_sql("q")}
            ),
            keyword_base AS (
                SELECT
                    id,
                    quote_id,
                    keyword,
                    'confirmed' as source,
                    category as difficulty,
                    status,
                    COALESCE(is_core, TRUE) as is_core,
                    cluster_id,
                    -- [CTO-15.23 2026-05-09] 代理自助监测订阅模型 · 返回 is_monitored 给前端 toggle
                    -- [WO_ORPHAN_MONITOR_FIX 2026-08-10 ②] 开关显示**派生自订阅真值**,
                    --   不再读 confirmed_keywords.is_monitored 那一列。
                    --   理由:每日 cron 只认 keyword_monitor_subscriptions(list_active_subscriptions),
                    --   而这一列可以在没有订阅的情况下为 TRUE(C 端确认报价的历史路径就是这么产的,
                    --   存量 147 条)。读列 = 界面说"开着"、实际永远不跑 —— 从此"看着开 = 真的跑"恒等。
                    --   口径与调度器同形:active / paused_low_balance 都算"开着"
                    --   (余额不足是暂停不是关闭,充值后自己恢复,UI 不该显示成关)。
                    EXISTS (
                        SELECT 1 FROM keyword_monitor_subscriptions kms
                         WHERE kms.keyword_id = confirmed_keywords.id
                           AND kms.status IN ('active', 'paused_low_balance')
                    ) as is_monitored,
                    monitoring_subscription_id,
                    (
                        SELECT m.platforms
                        FROM public.monitoring_product_platform_matrices m
                        WHERE m.version = confirmed_keywords.monitoring_product_version
                    ) as entitlement_platforms,
                    COALESCE(super_red_ocean, FALSE) as super_red_ocean
                FROM confirmed_keywords
                WHERE quote_id IN (SELECT id FROM brand_quotes)
                  -- [CTO-15.23 2026-05-09 监测自助开通]
                  -- 不再按 is_monitored 过滤(否则代理 disable 后该词消失就没法再 enable)
                  -- daily 跑监测改走 keyword_monitor_subscriptions.status='active'(list_active_subscriptions)
                  -- 监测页面显示 quote 下所有 confirmed keyword · 每行带 toggle
                  -- 归档机制:只过滤 archived/deleted(monitoring_status='active' 仍 active)
                  AND COALESCE(monitoring_status, 'active') = 'active'

                UNION ALL

                SELECT
                    id,
                    quote_id,
                    keyword,
                    'extra' as source,
                    difficulty,
                    status,
                    TRUE as is_core,
                    NULL::INTEGER as cluster_id,
                    -- [WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15 P0-A.3] 去掉硬编码 FALSE。
                    --   旧值是写死的常量 → 前端那个 checked 绑定永远拿到 false、画不出开关,
                    --   而后台照跑照扣(唯一闸是 status,库默认就是 'active')= 界面与真相分裂。
                    --   现在读真列,与 confirmed 分支同口径:**看着开 = 真的跑**(2026-08-10 立的恒等式)。
                    --   口径差异说明:confirmed 派生自逐词订阅(有 keyword_monitor_subscriptions 那层),
                    --   extra 没有订阅模型(它搭品牌批次跑、按 monitor_single 130/词/次 结算),
                    --   所以它的真值就在自己这一列上。
                    --   🔴 **必须写全表名限定**(2026-08-15 A/B 实测倒逼):
                    --   2026-08-10 那批锁把"不带表名限定的那种 COALESCE 读法"当红线,
                    --   用来防 confirmed 分支退回直读自己那一列。裸写会与它逐字相撞 ——
                    --   而那两条锁是对的(它守的是 confirmed 分支),该让路的是本包不是它。
                    --   (连这段注释也不能把那个字面量抄进来:文本锁不看上下文,注释同样会命中。)
                    -- [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-3/P0-8] K1 之后手动词也有逐词订阅了,
                    --   所以这里与 confirmed 分支同口径:**开着 = 列开着 且 有在跑的订阅**。
                    --   两个都要:取数的 extra 臂正是 `s.status='active' AND ek.is_monitored=TRUE`,
                    --   少判一个就会出现「界面说开着、cron 不跑」或反过来 —— 那正是
                    --   2026-08-10 立"看着开 = 真的跑"恒等式要消灭的形态。
                    (
                        COALESCE(extra_keywords.is_monitored, FALSE)
                        AND EXISTS (
                            SELECT 1 FROM keyword_monitor_subscriptions kms
                             WHERE kms.keyword_id = extra_keywords.id
                               AND kms.keyword_source = 'extra'
                               AND kms.status IN ('active', 'paused_low_balance')
                        )
                    ) as is_monitored,
                    NULL::INTEGER as monitoring_subscription_id,
                    platforms as entitlement_platforms,
                    FALSE as super_red_ocean
                FROM extra_keywords
                WHERE quote_id IN (SELECT id FROM brand_quotes)
                  AND COALESCE(status, 'active') = 'active'
            ),
            -- 找到每个关键词首次被检出的日期（铺量期结束点）
            first_detection AS (
                SELECT
                    COALESCE(confirmed_keyword_id, keyword_id) AS keyword_id,
                    MIN(DATE(tested_at)) as first_detected_date
                FROM monitoring_results
                WHERE is_detected = 1 AND {eligible_result}
                GROUP BY COALESCE(confirmed_keyword_id, keyword_id)
            ),
            -- 每个(keyword, platform)只取最新一条结果，避免重复监测拉低出现率
            latest_per_platform AS (
                SELECT DISTINCT ON (COALESCE(mr.confirmed_keyword_id, mr.keyword_id), {platform_key_expr})
                    COALESCE(mr.confirmed_keyword_id, mr.keyword_id) AS keyword_id,
                    mr.platform,
                    {platform_key_expr} as platform_key,
                    mr.is_detected,
                    mr.tested_at
                FROM monitoring_results mr
                INNER JOIN first_detection fd
                    ON COALESCE(mr.confirmed_keyword_id, mr.keyword_id) = fd.keyword_id
                WHERE {eligible_mr}
                  AND DATE(mr.tested_at) >= GREATEST(
                    fd.first_detected_date,
                    CURRENT_DATE - INTERVAL '7 days'
                )
                ORDER BY COALESCE(mr.confirmed_keyword_id, mr.keyword_id), {platform_key_expr}, mr.tested_at DESC
            ),
            -- 平台权重映射（基于 QuestMobile 2026年2月 MAU）
            platform_weight AS (
                SELECT * FROM (VALUES
                    {pw_values}
                ) AS pw(platform, weight)
            ),
            -- 滚动窗口统计：加权出现率
            keyword_stats AS (
                SELECT
                    lpp.keyword_id,
                    COUNT(*) as total_tests,
                    SUM(CASE WHEN lpp.is_detected = 1 THEN 1 ELSE 0 END) as detected_count,
                    MAX(lpp.tested_at) as last_tested,
                    -- [2026-05-29 Q1 老板拍] 原始检出率(头条口径 · 不被低 MAU 引擎压低 · 全链单一口径含达标)
                    SUM(CASE WHEN lpp.is_detected = 1 THEN 1 ELSE 0 END)::numeric
                        / NULLIF(COUNT(*), 0) * 100 as raw_rate,
                    -- 市占率加权出现率(主显示口径 · 平台别名先归一化再匹配权重)
                    SUM(CASE WHEN lpp.is_detected = 1 THEN COALESCE(pw.weight, {fallback_weight}) ELSE 0 END)
                        / NULLIF(SUM(COALESCE(pw.weight, {fallback_weight})), 0) * 100 as weighted_rate
                FROM latest_per_platform lpp
                LEFT JOIN platform_weight pw ON lpp.platform_key = pw.platform
                GROUP BY lpp.keyword_id
            ),
            -- 全量统计（用于显示总测试次数）
            keyword_all_stats AS (
                SELECT
                    COALESCE(confirmed_keyword_id, keyword_id) AS keyword_id,
                    COUNT(*) as all_total_tests,
                    SUM(CASE WHEN is_detected = 1 THEN 1 ELSE 0 END) as all_detected_count,
                    MAX(tested_at) as last_tested
                FROM monitoring_results
                WHERE {eligible_result}
                GROUP BY COALESCE(confirmed_keyword_id, keyword_id)
            ),
            -- [2026-05-29 Q1-C] 每引擎本期检出(latest per platform · 与原始检出率头条同基)
            per_engine AS (
                SELECT keyword_id, jsonb_object_agg(platform_key, is_detected) as engines
                FROM latest_per_platform
                GROUP BY keyword_id
            )
            SELECT
                kb.id,
                kb.quote_id,  -- [根治 C] 返 quote_id 让前端显示"来自 quote XXX"
                kb.keyword,
                kb.source,
                kb.difficulty,
                kb.status,
                kb.is_core,
                kb.cluster_id,
                kc.cluster_name,
                COALESCE(kas.all_total_tests, 0) as total_tests,
                COALESCE(kas.all_detected_count, 0) as detected_count,
                COALESCE(ks.total_tests, 0) as window_tests,
                COALESCE(ks.detected_count, 0) as window_detected,
                CASE
                    WHEN COALESCE(ks.total_tests, 0) > 0
                    THEN ROUND(CAST(ks.raw_rate AS NUMERIC), 1)
                    ELSE 0
                END as detection_rate,   -- 原始检出率(API 层会同时保留 raw_detection_rate 并将主显示切到 weighted_rate)
                CASE
                    WHEN COALESCE(ks.total_tests, 0) > 0
                    THEN ROUND(CAST(ks.weighted_rate AS NUMERIC), 1)
                    ELSE 0
                END as weighted_rate,    -- 市占率加权主显示口径
                pe.engines as per_engine_detection,  -- [Q1-C] 每引擎本期检出 platform=0/1(注释勿用花括号·此 SQL 是 f-string)
                fd.first_detected_date,
                COALESCE(kas.last_tested, ks.last_tested) as last_tested,
                kb.is_monitored,
                kb.monitoring_subscription_id,
                kb.entitlement_platforms,
                kb.super_red_ocean
            FROM keyword_base kb
            LEFT JOIN keyword_stats ks ON kb.id = ks.keyword_id
            LEFT JOIN keyword_all_stats kas ON kb.id = kas.keyword_id
            LEFT JOIN first_detection fd ON kb.id = fd.keyword_id
            LEFT JOIN per_engine pe ON kb.id = pe.keyword_id
            LEFT JOIN keyword_clusters kc ON kb.cluster_id = kc.id
            ORDER BY kb.cluster_id NULLS LAST, kb.keyword
        """, (quote_id,))

        rows = cursor.fetchall()

        result = []
        for row in rows:
            kw = dict(row)
            kw_id = kw["id"]
            source = kw["source"]

            kw["target_brand"] = default_brand

            # 标记生命周期阶段
            # 🔴 [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-8] 必须**同时**看历史与开关。
            #   Owner 2026-08-16 截图报:同一行绿色徽章写「监测中」、紧挨着的开关写「已关闭」。
            #   根因就是这段:旧版只按历史推导(曾检出→monitoring / 跑过没检出→deploying),
            #   完全不看开关。「监测中」「铺量中」是**现在进行时**的词,描述的却是过去发生过的事,
            #   关掉开关之后照旧显示。
            #   新口径:开关关着时一律落到 stopped_* —— 前端据此渲染「已停(曾检出)」/
            #   「已停(铺量未完成)」,**不许出现任何"在跑"字样**。
            #   从没跑过 + 关着 仍是 pending(「待检测」不是现在进行时,没有误导)。
            #   confirmed 与 extra 两支共用这段代码 ⇒ K1 要求的"两者行为一致"由此保证。
            _switch_on = bool(kw.get("is_monitored"))
            if kw.get("first_detected_date"):
                kw["lifecycle"] = "monitoring" if _switch_on else "stopped_detected"
            elif kw.get("total_tests", 0) > 0:
                kw["lifecycle"] = "deploying" if _switch_on else "stopped_deploying"
            else:
                kw["lifecycle"] = "pending"     # 尚未测试(开关无论开关都是"待检测")

            # 获取趋势变化：当前实时出现率 - 上一期（非今天）的趋势出现率
            cursor.execute("""
                SELECT detection_rate FROM keyword_trend_stats
                WHERE keyword_id = %s AND keyword_source = %s
                  AND period_date < CURRENT_DATE
                ORDER BY period_date DESC
                LIMIT 1
            """, (kw_id, source))
            prev_row = cursor.fetchone()
            if prev_row is not None and kw.get("detection_rate") is not None:
                kw["rate_change"] = round(float(kw["detection_rate"]) - float(prev_row["detection_rate"] or 0), 1)
            else:
                kw["rate_change"] = None

            result.append(kw)

        conn.close()
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def get_keyword_detection_details(keyword_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    获取每个关键词的最近一轮监测结果详情（按平台分组，含引用信息）
    返回 {keyword_id: [{platform, is_detected, mention_type, tested_at, citations: [...]}]}
    """
    if not keyword_ids:
        return {}

    conn = get_connection()
    try:
        cursor = conn.cursor()

        placeholders = ','.join(['%s'] * len(keyword_ids))

        # 取每个 keyword_id / platform 的最新一条结果。
        # 旧实现按 DATE(max_tested) 取整天数据，同一天多跑几轮时会把旧检出和新未检出混在一起，
        # 客户看到"本轮未提到我"，表格却仍显示检出/达标。
        from services.monitoring_identity_review import aggregate_eligible_sql
        eligible_mr = aggregate_eligible_sql("mr")
        cursor.execute(f"""
            SELECT DISTINCT ON (COALESCE(mr.confirmed_keyword_id, mr.keyword_id), mr.platform)
                COALESCE(mr.confirmed_keyword_id, mr.keyword_id) AS keyword_id,
                mr.platform,
                mr.is_detected,
                mr.mention_type,
                mr.tested_at,
                mr.search_citations,
                mr.response_snippet,
                mr.full_response
            FROM monitoring_results mr
            WHERE COALESCE(mr.confirmed_keyword_id, mr.keyword_id) IN ({placeholders})
              AND {eligible_mr}
            ORDER BY COALESCE(mr.confirmed_keyword_id, mr.keyword_id), mr.platform, mr.tested_at DESC
        """, keyword_ids)

        rows = cursor.fetchall()
        conn.close()

        import json
        result: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            kw_id = row["keyword_id"]
            citations = []
            if row["search_citations"]:
                try:
                    citations = json.loads(row["search_citations"])
                except (json.JSONDecodeError, TypeError):
                    pass

            detail = {
                "platform": row["platform"],
                "is_detected": bool(row["is_detected"]),
                "mention_type": row["mention_type"] or "none",
                "tested_at": row["tested_at"],
                "citations": citations,
                "snippet": row["full_response"] or row["response_snippet"] or ""
            }

            if kw_id not in result:
                result[kw_id] = []
            result[kw_id].append(detail)

        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def _legacy_get_keyword_detection_details_by_day(keyword_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """保留旧版按天聚合实现仅供回溯，不再用于线上展示。"""
    if not keyword_ids:
        return {}

    conn = get_connection()
    try:
        cursor = conn.cursor()

        placeholders = ','.join(['%s'] * len(keyword_ids))

        from services.monitoring_identity_review import aggregate_eligible_sql
        eligible = aggregate_eligible_sql()
        eligible_mr = aggregate_eligible_sql("mr")
        cursor.execute(f"""
            WITH latest_round AS (
                SELECT keyword_id, MAX(tested_at) as max_tested
                FROM monitoring_results
                WHERE keyword_id IN ({placeholders})
                  AND {eligible}
                GROUP BY keyword_id
            )
            SELECT
                mr.keyword_id,
                mr.platform,
                mr.is_detected,
                mr.mention_type,
                mr.tested_at,
                mr.search_citations,
                mr.response_snippet,
                mr.full_response
            FROM monitoring_results mr
            INNER JOIN latest_round lr
                ON mr.keyword_id = lr.keyword_id
                AND DATE(mr.tested_at) = DATE(lr.max_tested)
            WHERE mr.keyword_id IN ({placeholders})
              AND {eligible_mr}
            ORDER BY mr.keyword_id, mr.platform
        """, keyword_ids + keyword_ids)

        rows = cursor.fetchall()
        conn.close()

        import json
        result: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            kw_id = row["keyword_id"]
            citations = []
            if row["search_citations"]:
                try:
                    citations = json.loads(row["search_citations"])
                except (json.JSONDecodeError, TypeError):
                    pass

            detail = {
                "platform": row["platform"],
                "is_detected": bool(row["is_detected"]),
                "mention_type": row["mention_type"] or "none",
                "tested_at": row["tested_at"],
                "citations": citations,
                "snippet": row["full_response"] or row["response_snippet"] or ""
            }

            if kw_id not in result:
                result[kw_id] = []
            result[kw_id].append(detail)

        return result
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 趋势统计操作
# ==========================================

def save_trend_stat(
    keyword_id: int,
    keyword_source: str,
    period_type: str,
    period_date: str,
    test_count: int,
    detected_count: int,
    rate_change: float = 0
) -> int:
    """保存趋势统计 · 内部聚合 monitoring_results 真值(SSOT 防 ON CONFLICT 覆盖)

    [Deploy-CTO 2026-05-27 P0 修 · 出现率 vs 趋势报表数据不统一根治]
    之前 ON CONFLICT DO UPDATE 用 caller 传值覆盖 · 同 keyword+period_date 多 task 跑
    会互相覆盖(自动 cron 覆盖手动 SSE):
      早 自动 cron task#A: test=4/det=1 → INSERT 25%
      中 手动 SSE task#B:  test=20/det=6 → ON CONFLICT 覆盖 30%
      晚 自动 cron task#C: test=4/det=0 → ON CONFLICT 覆盖 0% ← 留下错值
    监测中心(实时 SUM)显示 25% · 趋势报表(覆盖后)显示 0% · 数据不统一

    修法:函数内部不用 caller 传入 test_count/detected_count(忽略)
    直接 SELECT monitoring_results WHERE keyword_id + DATE(tested_at)=period_date
    AGGREGATE COUNT/SUM · 用真值写入(SSOT · 涵盖所有 trigger_type:manual_sse/unknown/scheduled)
    caller signature 不变(backward compat)· 仅 period_type='daily' 走聚合 · 其他不变
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 仅 daily period_type 走聚合(weekly/monthly 等其他粒度走老逻辑 · 保 backward compat)
        if period_type == "daily":
            from services.monitoring_identity_review import aggregate_eligible_sql
            cursor.execute(f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN is_detected = 1 THEN 1 ELSE 0 END) AS detected
                FROM monitoring_results
                WHERE COALESCE(confirmed_keyword_id, keyword_id) = %s
                  AND (
                       keyword_source = %s
                       OR (%s = 'confirmed' AND keyword_source = 'contract')
                       OR keyword_source IS NULL
                       OR keyword_source = 'legacy_unknown'
                  )
                  AND DATE(tested_at) = %s
                  AND {aggregate_eligible_sql()}
            """, (keyword_id, keyword_source, keyword_source, period_date))
            row = cursor.fetchone()
            real_total = (row["total"] if isinstance(row, dict) else row[0]) or 0
            real_detected = (row["detected"] if isinstance(row, dict) else row[1]) or 0
            test_count = int(real_total)
            detected_count = int(real_detected)

        detection_rate = round(detected_count / test_count * 100, 1) if test_count > 0 else 0

        cursor.execute("""
            INSERT INTO keyword_trend_stats
            (keyword_id, keyword_source, period_type, period_date, test_count, detected_count, detection_rate, rate_change)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (keyword_id, keyword_source, period_type, period_date)
            DO UPDATE SET test_count = EXCLUDED.test_count, detected_count = EXCLUDED.detected_count,
                          detection_rate = EXCLUDED.detection_rate, rate_change = EXCLUDED.rate_change
            RETURNING id
        """, (keyword_id, keyword_source, period_type, period_date, test_count, detected_count, detection_rate, rate_change))

        stat_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return stat_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_keyword_trend(
    keyword_id: int,
    keyword_source: str = "confirmed",
    period_type: str = "daily",
    limit: int = 30
) -> List[Dict[str, Any]]:
    """获取关键词趋势数据"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT * FROM keyword_trend_stats
            WHERE keyword_id = %s AND keyword_source = %s AND period_type = %s
            ORDER BY period_date DESC
            LIMIT %s
        """, (keyword_id, keyword_source, period_type, limit))
    
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def calculate_rate_change(
    keyword_id: int,
    keyword_source: str,
    current_rate: float,
    period_type: str
) -> float:
    """计算出现率变化（与上一期对比）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT detection_rate FROM keyword_trend_stats
            WHERE keyword_id = %s AND keyword_source = %s AND period_type = %s
            ORDER BY period_date DESC
            LIMIT 1 OFFSET 0
        """, (keyword_id, keyword_source, period_type))
    
        row = cursor.fetchone()
        conn.close()
    
        if row:
            previous_rate = row["detection_rate"] or 0
            return round(current_rate - previous_rate, 1)
        return 0
    finally:
        try:
            conn.close()
        except Exception: pass


def sync_task_trends(task_id: int) -> Dict[str, Any]:
    """
    手动同步监测任务的趋势数据。

    从 monitoring_results 聚合指定任务的检测结果，写入 keyword_trend_stats。
    同时标记任务的 trend_synced = 1。

    Returns:
        {success, synced_keywords, task_id}
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 获取任务信息
        cursor.execute("SELECT id, created_at, trigger_type, trend_synced FROM monitoring_tasks WHERE id = %s", (task_id,))
        task = cursor.fetchone()
        if not task:
            conn.close()
            return {"success": False, "error": "任务不存在"}

        cursor.execute("SELECT pg_catalog.to_regclass('public.monitoring_identity_decision_events') AS rel")
        if cursor.fetchone().get("rel"):
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt
                  FROM public.monitoring_identity_decision_events e
                  JOIN public.monitoring_results mr ON mr.id = e.result_id
                 WHERE mr.task_id = %s
                """,
                (int(task_id),),
            )
            if int(cursor.fetchone()["cnt"] or 0) > 0:
                conn.rollback()
                return {
                    "success": False,
                    "error": "该任务包含不可删除的人工核验审计，请保留任务并重新运行监测",
                }

        task_date = task["created_at"].strftime("%Y-%m-%d") if task["created_at"] else datetime.now().strftime("%Y-%m-%d")

        # 按 keyword_id + platform 聚合结果（加权出现率）
        from services.monitoring_identity_review import aggregate_eligible_sql
        cursor.execute(f"""
            SELECT COALESCE(confirmed_keyword_id, keyword_id) AS keyword_id,
                   CASE
                       WHEN keyword_source IN ('confirmed', 'contract') THEN 'confirmed'
                       WHEN keyword_source = 'extra' THEN 'extra'
                       ELSE 'confirmed'
                   END AS keyword_source,
                   platform,
                   CASE WHEN is_detected = 1 THEN 1 ELSE 0 END as detected
            FROM monitoring_results
            WHERE task_id = %s
              AND {aggregate_eligible_sql()}
        """, (task_id,))
        result_rows = cursor.fetchall()

        # 按 keyword_id 分组，用加权计算
        from collections import defaultdict
        kw_results = defaultdict(list)
        for r in result_rows:
            kw_results[(r["keyword_id"], r["keyword_source"])].append(r)

        synced = 0
        for (kid, keyword_source), records in kw_results.items():
            tests = len(records)
            detected = sum(1 for r in records if r["detected"])
            if tests > 0:
                # 加权出现率
                w_detected = sum(get_platform_weight(r["platform"]) for r in records if r["detected"])
                w_total = sum(get_platform_weight(r["platform"]) for r in records)
                rate = round(w_detected / w_total * 100, 1) if w_total > 0 else 0
                # 计算与上期的变化
                prev_change = calculate_rate_change(kid, keyword_source, rate, "daily")
                save_trend_stat(
                    kid, keyword_source, "daily", task_date, tests, detected, prev_change
                )
                synced += 1

        # 标记任务已同步
        cursor.execute("UPDATE monitoring_tasks SET trend_synced = 1 WHERE id = %s", (task_id,))
        conn.commit()
        conn.close()

        return {"success": True, "synced_keywords": synced, "task_id": task_id}
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 数据回退操作
# ==========================================

def get_rollback_tasks(
    brand_id: int, limit: int = 20, before_created_at=None, before_id: int = None
) -> List[Dict[str, Any]]:
    """获取可回退的监测任务列表（按时间倒序，含结果统计）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        from services.monitoring_identity_review import aggregate_eligible_sql
        eligible_mr = aggregate_eligible_sql("mr")
        cursor.execute(f"""
            SELECT
                mt.id,
                mt.task_name,
                mt.status,
                mt.total_tests,
                mt.completed_tests,
                mt.created_at,
                mt.completed_at,
                mt.trigger_type,
                mt.trend_synced,
                COUNT(mr.id) FILTER (WHERE {eligible_mr}) as result_count,
                SUM(CASE WHEN mr.is_detected = 1 AND {eligible_mr} THEN 1 ELSE 0 END) as detected_count,
                COUNT(mr.id) FILTER (WHERE NOT ({eligible_mr})) as pending_identity_count
            FROM monitoring_tasks mt
            LEFT JOIN monitoring_results mr ON mt.id = mr.task_id
            WHERE mt.brand_id = %s
              AND (%s::timestamptz IS NULL OR (mt.created_at,mt.id) < (%s::timestamptz,%s))
            GROUP BY mt.id, mt.task_name, mt.status, mt.total_tests, mt.completed_tests,
                     mt.created_at, mt.completed_at, mt.trigger_type, mt.trend_synced
            ORDER BY mt.created_at DESC,mt.id DESC
            LIMIT %s
        """, (
            brand_id, before_created_at, before_created_at,
            int(before_id) if before_id is not None else 0,
            max(1, min(int(limit), 200)),
        ))

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def rollback_task(task_id: int) -> Dict[str, Any]:
    """
    回退指定监测任务：删除该任务的所有结果，并删除该任务对应日期的趋势统计。
    任务本身标记为 rolled_back 而非删除（保留审计痕迹）。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 获取任务信息
        cursor.execute("SELECT * FROM monitoring_tasks WHERE id = %s", (task_id,))
        task = cursor.fetchone()
        if not task:
            conn.close()
            return {"success": False, "error": "任务不存在"}

        created_at = task.get("created_at")
        task_date = (
            created_at.strftime("%Y-%m-%d")
            if hasattr(created_at, "strftime")
            else str(created_at)[:10]
            if created_at
            else None
        )

        cursor.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM public.monitoring_results
             WHERE task_id = %s
               AND (
                    identity_review_state <> 'not_required'
                    OR response_status = 'brand_identity_unresolved'
                    OR mention_type = 'pending_identity'
               )
            """,
            (int(task_id),),
        )
        if int(cursor.fetchone()["cnt"] or 0) > 0:
            conn.rollback()
            return {
                "success": False,
                "error": "该任务包含待核验或已核验的身份记录，不能回退",
            }

        cursor.execute(
            "SELECT pg_catalog.to_regclass('public.monitoring_identity_decision_events') AS rel"
        )
        if cursor.fetchone().get("rel"):
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt
                  FROM public.monitoring_identity_decision_events e
                  JOIN public.monitoring_results mr ON mr.id = e.result_id
                 WHERE mr.task_id = %s
                """,
                (int(task_id),),
            )
            if int(cursor.fetchone()["cnt"] or 0) > 0:
                conn.rollback()
                return {
                    "success": False,
                    "error": "该任务包含不可删除的人工核验审计，不能回退",
                }

        # 统计即将删除的数据
        cursor.execute("SELECT COUNT(*) as cnt FROM monitoring_results WHERE task_id = %s", (task_id,))
        result_count = cursor.fetchone()["cnt"]

        # [audit P1 2026-06-10 红线批·老板已批] 删 results 前先取本任务词集(monitoring_tasks 不存 keyword_ids)
        # 旧版 trends 按 period_date 全局删 → 一次回退抹掉当天【全平台所有租户】趋势统计
        # (prod 单日最多 438 行/366 词·可被逐日刷成全表损毁)。缩到本任务词集维度。
        cursor.execute(
            "SELECT DISTINCT COALESCE(confirmed_keyword_id, keyword_id) AS keyword_id "
            "FROM monitoring_results WHERE task_id = %s "
            "AND COALESCE(confirmed_keyword_id, keyword_id) IS NOT NULL",
            (task_id,),
        )
        _task_kw_ids = [r["keyword_id"] for r in cursor.fetchall()]

        # 删除该任务的所有监测结果
        cursor.execute("DELETE FROM monitoring_results WHERE task_id = %s", (task_id,))
        deleted_results = cursor.rowcount

        # 删除该任务对应日期 + 本任务词集的趋势统计(避免残留脏数据 · 不再全局删)
        deleted_trends = 0
        if task_date and _task_kw_ids:
            cursor.execute(
                "DELETE FROM keyword_trend_stats WHERE period_date = %s AND keyword_id = ANY(%s)",
                (task_date, _task_kw_ids),
            )
            deleted_trends = cursor.rowcount
        # 词集为空(结果已被清/老任务无 keyword_id)→ fail-closed 不删 trends(宁留待重算,不全局抹)

        # 标记任务为已回退
        cursor.execute("""
            UPDATE monitoring_tasks SET status = 'rolled_back'
            WHERE id = %s
        """, (task_id,))

        conn.commit()
        conn.close()

        return {
            "success": True,
            "task_id": task_id,
            "deleted_results": deleted_results,
            "deleted_trends": deleted_trends,
            "task_date": task_date,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def rollback_tasks_batch(task_ids: List[int]) -> Dict[str, Any]:
    """批量回退多个监测任务"""
    total_results = 0
    total_trends = 0
    for tid in task_ids:
        r = rollback_task(tid)
        if r.get("success"):
            total_results += r.get("deleted_results", 0)
            total_trends += r.get("deleted_trends", 0)
    return {
        "success": True,
        "rolled_back_tasks": len(task_ids),
        "deleted_results": total_results,
        "deleted_trends": total_trends,
    }


# ==========================================
# 数据清除与恢复
# ==========================================

def clear_monitoring_data(brand_id: int, quote_id: int, operator: str = "", reason: str = "") -> Dict[str, Any]:
    """
    清除指定客户的全部监测数据（归档后删除，可恢复）

    归档内容：monitoring_tasks, monitoring_results, keyword_trend_stats, keyword_compliance_log

    [CTO-15.23 2026-05-14 BUG fix] 老板报"清除后再监测老数据又回来"根因:
      老版漏清 keyword_compliance_log 表 → effective_rate 历史还在 → v2/v3 算法读历史 →
      老 32.7% 持续显示。本 fix 加 keyword_compliance_log 归档 + DELETE · quote_id 维度对齐。
    """
    import uuid as _uuid
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # [CTO-15.23 2026-05-14] 兜底:确保 archived_compliance 列存在
        # 老 schema 没此列 · 不能假设 prod 已 ALTER · 用 PG IF NOT EXISTS 幂等 + try
        try:
            add_column_if_missing(cursor, "monitoring_data_archives", "archived_compliance", "TEXT")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        except Exception:
            pass  # 列已存在或权限问题 · 后续 INSERT 走兜底分支

        archive_key = f"archive-{_uuid.uuid4().hex[:12]}"

        # 1. 收集该客户的所有任务ID（client_id 实际存的是 brand_id）
        cursor.execute(
            "SELECT id FROM monitoring_tasks WHERE (brand_id = %s OR client_id = %s) AND status != 'rolled_back'",
            (brand_id, str(brand_id))
        )
        task_ids = [row["id"] for row in cursor.fetchall()]

        if not task_ids:
            conn.close()
            return {"success": False, "error": "该客户没有监测数据可清除"}

        cursor.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM public.monitoring_results
             WHERE task_id = ANY(%s)
               AND (
                    identity_review_state <> 'not_required'
                    OR response_status = 'brand_identity_unresolved'
                    OR mention_type = 'pending_identity'
               )
            """,
            (task_ids,),
        )
        if int(cursor.fetchone()["cnt"] or 0) > 0:
            conn.rollback()
            return {
                "success": False,
                "error": "该客户包含待核验或已核验的身份记录，不能执行历史数据清除",
            }

        cursor.execute("SELECT pg_catalog.to_regclass('public.monitoring_identity_decision_events') AS rel")
        if cursor.fetchone().get("rel"):
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt
                  FROM public.monitoring_identity_decision_events e
                  JOIN public.monitoring_results mr ON mr.id = e.result_id
                 WHERE mr.task_id = ANY(%s)
                """,
                (task_ids,),
            )
            if int(cursor.fetchone()["cnt"] or 0) > 0:
                conn.rollback()
                return {
                    "success": False,
                    "error": "该客户包含不可删除的人工核验审计，不能执行历史数据清除",
                }

        placeholders = ",".join(["%s"] * len(task_ids))

        # 2. 归档 tasks
        cursor.execute(f"SELECT * FROM monitoring_tasks WHERE id IN ({placeholders})", tuple(task_ids))
        archived_tasks = json.dumps([dict(r) for r in cursor.fetchall()], ensure_ascii=False, default=str)

        # 3. 归档 results
        cursor.execute(f"SELECT * FROM monitoring_results WHERE task_id IN ({placeholders})", tuple(task_ids))
        results_rows = cursor.fetchall()
        result_count = len(results_rows)
        archived_results = json.dumps([dict(r) for r in results_rows], ensure_ascii=False, default=str)

        # 4. 归档 keyword_trend_stats（按该客户的词条）
        cursor.execute(
            "SELECT id FROM confirmed_keywords WHERE quote_id = %s", (quote_id,)
        )
        kw_ids = [row["id"] for row in cursor.fetchall()]

        trend_count = 0
        archived_trends = "[]"
        if kw_ids:
            kw_placeholders = ",".join(["%s"] * len(kw_ids))
            cursor.execute(
                f"SELECT * FROM keyword_trend_stats WHERE keyword_id IN ({kw_placeholders})",
                tuple(kw_ids)
            )
            trends_rows = cursor.fetchall()
            trend_count = len(trends_rows)
            archived_trends = json.dumps([dict(r) for r in trends_rows], ensure_ascii=False, default=str)

        # 4b. 归档 keyword_compliance_log(按 quote_id · 跟 compute_compliance 写入维度一致)
        # [CTO-15.23 2026-05-14] 老版漏归档 → restore 不完整 + 重监测时老数据复活
        cursor.execute(
            "SELECT * FROM keyword_compliance_log WHERE quote_id = %s",
            (quote_id,)
        )
        compliance_rows = cursor.fetchall()
        compliance_count = len(compliance_rows)
        archived_compliance = json.dumps([dict(r) for r in compliance_rows], ensure_ascii=False, default=str)

        # 5. 写入归档记录(兜底:archived_compliance 列可能未 ALTER 成功 · try 兼容老 schema)
        try:
            cursor.execute("""
                INSERT INTO monitoring_data_archives
                (archive_key, brand_id, quote_id, operator, reason, task_ids,
                 result_count, trend_count, archived_tasks, archived_results, archived_trends, archived_compliance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                archive_key, brand_id, quote_id, operator, reason,
                json.dumps(task_ids), result_count, trend_count,
                archived_tasks, archived_results, archived_trends, archived_compliance
            ))
        except Exception as _archive_err:
            # 老 schema 没 archived_compliance 列 · 退回老 INSERT(compliance 数据只能 log 不归档)
            import logging as _logging
            _logging.getLogger("GEO-Monitoring").warning(
                f"[clear-data] archived_compliance 列不存在 · 退回老 schema · compliance_count={compliance_count} 数据将仅 DELETE 不归档: {_archive_err}"
            )
            cursor.execute("""
                INSERT INTO monitoring_data_archives
                (archive_key, brand_id, quote_id, operator, reason, task_ids,
                 result_count, trend_count, archived_tasks, archived_results, archived_trends)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                archive_key, brand_id, quote_id, operator, reason,
                json.dumps(task_ids), result_count, trend_count,
                archived_tasks, archived_results, archived_trends
            ))

        # 6. 删除原数据（先 results → trends → tasks → compliance_log）
        cursor.execute(f"DELETE FROM monitoring_results WHERE task_id IN ({placeholders})", tuple(task_ids))
        deleted_results = cursor.rowcount

        if kw_ids:
            cursor.execute(
                f"DELETE FROM keyword_trend_stats WHERE keyword_id IN ({kw_placeholders})",
                tuple(kw_ids)
            )

        cursor.execute(f"DELETE FROM monitoring_tasks WHERE id IN ({placeholders})", tuple(task_ids))
        deleted_tasks = cursor.rowcount

        # [CTO-15.23 2026-05-14] DELETE keyword_compliance_log(老板报 BUG 真根因)
        cursor.execute(
            "DELETE FROM keyword_compliance_log WHERE quote_id = %s",
            (quote_id,)
        )
        deleted_compliance = cursor.rowcount

        conn.commit()
        conn.close()

        return {
            "success": True,
            "archive_key": archive_key,
            "deleted_tasks": deleted_tasks,
            "deleted_results": deleted_results,
            "deleted_trends": trend_count,
            "deleted_compliance": deleted_compliance,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def restore_monitoring_data(archive_key: str) -> Dict[str, Any]:
    """从归档恢复监测数据"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM monitoring_data_archives WHERE archive_key = %s",
            (archive_key,)
        )
        archive = cursor.fetchone()
        if not archive:
            conn.close()
            return {"success": False, "error": "归档记录不存在"}

        restored_tasks = 0
        restored_results = 0
        restored_trends = 0

        # 恢复 tasks
        tasks = json.loads(archive["archived_tasks"])
        for t in tasks:
            try:
                cursor.execute("""
                    INSERT INTO monitoring_tasks
                    (id, quote_id, client_id, brand_id, task_name, keyword_count, platform_count,
                     total_tests, completed_tests, concurrency, test_rounds, status,
                     created_at, started_at, completed_at, result_summary)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    t["id"], t.get("quote_id"), t.get("client_id", ""), t.get("brand_id"),
                    t.get("task_name"), t.get("keyword_count", 0), t.get("platform_count", 4),
                    t.get("total_tests", 0), t.get("completed_tests", 0),
                    t.get("concurrency", 10), t.get("test_rounds", 3), t.get("status", "completed"),
                    t.get("created_at"), t.get("started_at"), t.get("completed_at"),
                    t.get("result_summary")
                ))
                restored_tasks += 1
            except Exception as e:
                print(f"[Restore] task {t['id']} failed: {e}")

        # 恢复 results
        results = json.loads(archive["archived_results"])
        for r in results:
            try:
                cursor.execute("""
                    INSERT INTO monitoring_results
                    (id, task_id, keyword_id, keyword, platform, is_detected,
                     mention_type, response_snippet, full_response, tested_at,
                     response_char_count, search_citations)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    r["id"], r["task_id"], r.get("keyword_id"), r.get("keyword", ""),
                    r.get("platform", ""), int(r.get("is_detected", 0)),
                    r.get("mention_type", "none"), r.get("response_snippet", ""),
                    r.get("full_response", ""), r.get("tested_at"),
                    r.get("response_char_count", 0), r.get("search_citations", "")
                ))
                restored_results += 1
            except Exception as e:
                print(f"[Restore] result {r['id']} failed: {e}")

        # 恢复 trends
        trends = json.loads(archive["archived_trends"])
        for tr in trends:
            try:
                cursor.execute("""
                    INSERT INTO keyword_trend_stats
                    (keyword_id, keyword_source, period_type, period_date,
                     test_count, detected_count, detection_rate, rate_change)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (keyword_id, keyword_source, period_type, period_date) DO NOTHING
                """, (
                    tr["keyword_id"], tr.get("keyword_source", "confirmed"),
                    tr["period_type"], tr["period_date"],
                    tr.get("test_count", 0), tr.get("detected_count", 0),
                    tr.get("detection_rate", 0), tr.get("rate_change", 0)
                ))
                restored_trends += 1
            except Exception as e:
                print(f"[Restore] trend failed: {e}")

        # [CTO-15.23 2026-05-14] 恢复 keyword_compliance_log(对称 clear_monitoring_data 新增的归档)
        # 兼容老归档:archived_compliance 字段不存在或 None 时跳过(老 schema 误清的不能恢复)
        restored_compliance = 0
        try:
            compliance_archived = archive.get("archived_compliance") if isinstance(archive, dict) else None
        except Exception:
            compliance_archived = None
        if compliance_archived:
            try:
                compliance_rows = json.loads(compliance_archived)
                for cr in compliance_rows:
                    try:
                        cursor.execute("""
                            INSERT INTO keyword_compliance_log
                            (keyword_id, keyword_source, quote_id, check_date,
                             detection_rate, effective_rate, target_rate, is_compliant, is_stable)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (keyword_id, keyword_source, check_date) DO NOTHING
                        """, (
                            cr["keyword_id"], cr.get("keyword_source", "confirmed"),
                            cr["quote_id"], cr["check_date"],
                            cr.get("detection_rate"), cr.get("effective_rate"),
                            cr["target_rate"], bool(cr.get("is_compliant", False)),
                            bool(cr.get("is_stable", False)) if cr.get("is_stable") is not None else None,
                        ))
                        restored_compliance += 1
                    except Exception as e:
                        print(f"[Restore] compliance row failed: {e}")
            except Exception as e:
                print(f"[Restore] parse archived_compliance failed: {e}")

        # 删除归档记录
        cursor.execute("DELETE FROM monitoring_data_archives WHERE archive_key = %s", (archive_key,))

        conn.commit()
        conn.close()

        return {
            "success": True,
            "restored_tasks": restored_tasks,
            "restored_results": restored_results,
            "restored_trends": restored_trends,
            "restored_compliance": restored_compliance,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def list_archives(brand_id: int = None) -> List[Dict[str, Any]]:
    """列出归档记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if brand_id:
            cursor.execute(
                "SELECT id, archive_key, brand_id, quote_id, operator, reason, result_count, trend_count, created_at "
                "FROM monitoring_data_archives WHERE brand_id = %s ORDER BY created_at DESC",
                (brand_id,)
            )
        else:
            cursor.execute(
                "SELECT id, archive_key, brand_id, quote_id, operator, reason, result_count, trend_count, created_at "
                "FROM monitoring_data_archives ORDER BY created_at DESC"
            )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# Token消耗操作
# ==========================================

def save_token_usage(
    task_id: int,
    platform: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost: float = 0
) -> int:
    """保存Token消耗记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO monitoring_token_usage
            (task_id, platform, input_tokens, output_tokens, estimated_cost)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (task_id, platform, input_tokens, output_tokens, estimated_cost))

        usage_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return usage_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_token_usage_summary(
    quote_id: int = None,
    start_date: str = None,
    end_date: str = None
) -> Dict[str, Any]:
    """获取Token消耗汇总"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        query = """
            SELECT 
                SUM(input_tokens) as total_input,
                SUM(output_tokens) as total_output,
                SUM(estimated_cost) as total_cost,
                COUNT(*) as record_count
            FROM monitoring_token_usage tu
            JOIN monitoring_tasks mt ON tu.task_id = mt.id
            WHERE 1=1
        """
        params = []
    
        if quote_id:
            query += " AND mt.quote_id = %s"
            params.append(quote_id)

        if start_date:
            query += " AND tu.created_at >= %s"
            params.append(start_date)

        if end_date:
            query += " AND tu.created_at <= %s"
            params.append(end_date)
    
        cursor.execute(query, tuple(params))
        row = cursor.fetchone()
        conn.close()
    
        return {
            "total_input_tokens": row["total_input"] or 0,
            "total_output_tokens": row["total_output"] or 0,
            "total_tokens": (row["total_input"] or 0) + (row["total_output"] or 0),
            "total_cost": row["total_cost"] or 0,
            "record_count": row["record_count"] or 0
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# Token管理
# ==========================================

import secrets

def generate_client_token(
    quote_id: int, brand_name: str = None, days_valid: int = 30,
    *,
    actor_user_id: Optional[int] = None, actor_username: Optional[str] = None,
    request_id: Optional[str] = None, ip_address: Optional[str] = None,
    authority: str = "commercial_owner", reason: Optional[str] = None,
    in_tx_guard=None,
) -> Dict[str, Any]:
    """生成客户访问Token(轮换:失效旧 token + 签发新 token)。

    [WP7 #4] 当调用方(server.py 门户路由)传入 actor 上下文时,在**同一事务内**为本次
    轮换写一条 audit_logs 记录(request_id/reason 进 WP2 一等列),以对齐 admin 路径
    (require_portal_token_authority → record_admin_read)已有的审计,补上此前 owner /
    已授权成员 / 老服务商自助轮换门户 token **无审计**的缺口。actor 缺省时行为不变。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 🔴 [工单 V3-A · Codex 三审 P1-8] 事务内 CAS 钩子。
        #    调用方(现役唯一:defgeo 的「同对象重签」)在这里**用本事务的 cursor**
        #    再确认一次「用户看到的那个对象仍然是当前对象」;不符就抛,
        #    此时**一个 token 都还没失效**。
        #    为什么钩子而不是把 quote_id 比一遍:真正要 CAS 的命题是
        #    「这个品牌的 canonical 对象没漂」,那个谓词长在 defgeo 那一侧;
        #    搬进来会让 db 层反向依赖 services 层,而且会变成同一谓词的第二份实现。
        #    ``in_tx_guard=None`` 时逐字节回到旧行为。
        if in_tx_guard is not None:
            in_tx_guard(cursor)

        # 失效旧Token
        cursor.execute("""
            UPDATE client_access_tokens
            SET is_active = 0
            WHERE quote_id = %s AND is_active = 1
        """, (quote_id,))
        deactivated_count = cursor.rowcount  # 本次轮换失效的旧 token 数(审计用)

        # 生成新Token (12位)
        token = secrets.token_urlsafe(9)[:12].upper()
        expires_at = (datetime.now() + timedelta(days=days_valid)).strftime("%Y-%m-%d")

        cursor.execute("""
            INSERT INTO client_access_tokens (quote_id, brand_name, token, expires_at)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (quote_id, brand_name, token, expires_at))

        token_id = cursor.fetchone()["id"]

        # [WP7 #4] 同事务审计非 admin(owner/成员/老服务商)门户 token 轮换。
        if actor_user_id:
            _record_portal_token_rotation_audit(
                cursor,
                quote_id=quote_id, brand_name=brand_name, new_token_id=token_id,
                deactivated_count=deactivated_count, actor_user_id=actor_user_id,
                actor_username=actor_username, request_id=request_id,
                ip_address=ip_address, authority=authority, reason=reason,
            )

        conn.commit()
        conn.close()

        return {
            "id": token_id,
            "quote_id": quote_id,
            "token": token,
            "expires_at": expires_at
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def _record_portal_token_rotation_audit(
    cursor, *, quote_id: int, brand_name: Optional[str], new_token_id: int,
    deactivated_count: int, actor_user_id: int, actor_username: Optional[str],
    request_id: Optional[str], ip_address: Optional[str], authority: str,
    reason: Optional[str],
) -> None:
    """在调用方事务内写一条门户 token 轮换审计(不提交)。request_id/reason 进一等列。"""
    reason_value = (reason or "").strip() or "客户商业所有者/授权成员轮换客户门户访问 token"
    summary = (
        f"客户门户 token 轮换 quote_id={quote_id} "
        f"authority={authority} request_id={request_id or ''}"
    )
    before = {
        "quote_id": quote_id,
        "brand_name": brand_name,
        "previous_active_tokens": int(deactivated_count or 0),
    }
    after = {
        "authority": authority,
        "new_token_id": int(new_token_id),
    }
    cursor.execute(
        """
        INSERT INTO audit_logs (
            user_id, username, action, module, entity_type, entity_id,
            summary, before_snapshot, after_snapshot, ip_address,
            request_id, reason, created_at
        ) VALUES (%s, %s, 'portal.token.generate', 'portal_token',
                  'customer_portal_credential', %s, %s, %s, %s, %s, %s, %s, NOW())
        """,
        (
            int(actor_user_id), actor_username, int(quote_id), summary,
            json.dumps(before, ensure_ascii=False, default=str),
            json.dumps(after, ensure_ascii=False, default=str),
            ip_address, request_id, reason_value,
        ),
    )


# ==========================================
# [工单 2026-07-29 T1] 客户门户 token 续期(renew) —— 与轮换(rotate)严格分开
#
# 事故根因:token 默认 30 天固定有效期,而服务期常见 90/180/365 天
#   → 客户拿到的链接一个月后打不开,服务却还有几个月(岱林生物:服务期 180 剩 151)。
#
# 🔴 renew ≠ rotate:
#   - rotate = generate_client_token:先 is_active=0 掉旧 token 再签发新值
#     → 客户收藏夹/聊天记录里的旧 URL 立刻作废,比过期更糟。仅用于安全事件/手动重置。
#   - renew  = 本段:**只 UPDATE expires_at**,token 值不变 / is_active 不动 / 不发新链接。
#
# 续期规则(工单 §1.3):
#   new_expires_at = min(服务期结束日 + 宽限期, 今天 + 单次续期上限)
#   · 服务期已结束 → 不续期(维持原到期日)
#   · 只延长不缩短:new > old 才写(new <= old 直接跳过 → 重复事件零副作用)
#   · 已过期但仍在服务期内 → 允许复活(token 值不变,客户原链接继续可用)
# ==========================================

# 服务期结束后仍可访问的宽限天数(工单建议 30 天,可配置)
PORTAL_TOKEN_RENEW_GRACE_DAYS = 30
# 单次续期上限:一次交付成功最多把有效期推到「今天 + 该天数」
PORTAL_TOKEN_RENEW_MAX_DAYS = 90


def _portal_renew_config() -> tuple:
    """读环境覆盖(缺省 = 上面两个常量)。非法值一律回落常量,不让配置错误放大有效期。"""
    import os

    def _read(name: str, default: int) -> int:
        try:
            value = int(str(os.environ.get(name, "")).strip() or default)
        except (TypeError, ValueError):
            return default
        return value if 0 <= value <= 3650 else default

    return (
        _read("PORTAL_TOKEN_RENEW_GRACE_DAYS", PORTAL_TOKEN_RENEW_GRACE_DAYS),
        _read("PORTAL_TOKEN_RENEW_MAX_DAYS", PORTAL_TOKEN_RENEW_MAX_DAYS),
    )


def compute_portal_token_renew_date(
    cursor, quote_id: int, *, today=None, grace_days: int = None, max_days: int = None
):
    """算这张 quote 的门户 token 应该续到哪天。

    服务期结束日与 portal「剩余 N 天」用**同一口径**(api/monitoring_api.py
    api_get_client_keywords_merged / db._get_effective_window):
        service_end = quotes.service_end_date —— 服务期唯一 SSOT

    🔴 [服务期 SSOT 2026-08-06 §1.3] 旧口径是 `contract_start + COALESCE(service_days, 365)`。
       那把**履约达标天数配额**当成日历天用,于是门户 token 一路续到"起始日 + 365 天",
       而轮换资格闸一个月就关了 —— 客户还能进门户看"服务充足",里面早就不再更新。
       现在没有 service_end_date 就没有续期依据(fail-closed),不再拿配额糊一个出来。

    返回 (new_expires_at | None, service_end | None, reason)
      reason ∈ {ok, quote_not_found, no_service_anchor, service_ended}
    """
    from datetime import date as _date
    from services.service_period import read_calendar_period as _sp_read_period

    _grace, _max = _portal_renew_config()
    grace_days = _grace if grace_days is None else int(grace_days)
    max_days = _max if max_days is None else int(max_days)
    today = today or _date.today()

    cursor.execute(
        "SELECT service_start_date, service_end_date, paid_at::date AS paid_date "
        "FROM quotes WHERE id = %s",
        (int(quote_id),),
    )
    row = cursor.fetchone()
    if not row:
        return (None, None, "quote_not_found")

    _sp_start, service_end = _sp_read_period(row)
    contract_start = _sp_start or row.get("paid_date")
    if not contract_start or not service_end:
        # 未付款 / 未激活服务期 —— 没有服务期就没有续期依据,fail-closed 不动 token。
        return (None, None, "no_service_anchor")

    if service_end < today:
        # 服务期已结束 → 不再续期(工单 §1.3.2),维持原到期日。
        return (None, service_end, "service_ended")

    capped_by_service = service_end + timedelta(days=grace_days)
    capped_by_single_renew = today + timedelta(days=max_days)
    new_expires_at = min(capped_by_service, capped_by_single_renew)
    return (new_expires_at, service_end, "ok")


def _record_portal_token_renew_audit(
    cursor, *, quote_id: int, renewed: list, new_expires_at, service_end,
    trigger: str, actor_user_id: Optional[int], actor_username: Optional[str],
    request_id: Optional[str], ip_address: Optional[str], reason: Optional[str],
) -> None:
    """在调用方事务内写一条门户 token **续期** 审计(不提交)。

    action 用 `portal.token.renew`,与轮换的 `portal.token.generate` 分开 ——
    以后要区分"系统自动续的"和"人工重置的"直接按 action 过滤即可。
    系统触发无 actor:user_id 允许 NULL(audit_logs.user_id 本就 nullable)。
    """
    reason_value = (reason or "").strip() or f"交付成功自动续期客户门户访问 token(trigger={trigger})"
    summary = (
        f"客户门户 token 续期 quote_id={quote_id} trigger={trigger} "
        f"renewed={len(renewed)} request_id={request_id or ''}"
    )
    before = {
        "quote_id": quote_id,
        "tokens": [
            {"token_id": item["id"], "expires_at": str(item["old_expires_at"])}
            for item in renewed
        ],
    }
    after = {
        "trigger": trigger,
        "new_expires_at": str(new_expires_at),
        "service_end": str(service_end) if service_end else None,
        "renewed_token_ids": [item["id"] for item in renewed],
        "rotated": False,  # 续期永不换 token 值,客户原链接继续可用
    }
    cursor.execute(
        """
        INSERT INTO audit_logs (
            user_id, username, action, module, entity_type, entity_id,
            summary, before_snapshot, after_snapshot, ip_address,
            request_id, reason, created_at
        ) VALUES (%s, %s, 'portal.token.renew', 'portal_token',
                  'customer_portal_credential', %s, %s, %s, %s, %s, %s, %s, NOW())
        """,
        (
            int(actor_user_id) if actor_user_id else None,
            actor_username or "system",
            int(quote_id), summary,
            json.dumps(before, ensure_ascii=False, default=str),
            json.dumps(after, ensure_ascii=False, default=str),
            ip_address, request_id, reason_value,
        ),
    )


def renew_client_token(
    quote_id: int, *, trigger: str = "delivery_success",
    actor_user_id: Optional[int] = None, actor_username: Optional[str] = None,
    request_id: Optional[str] = None, ip_address: Optional[str] = None,
    reason: Optional[str] = None, today=None,
    grace_days: int = None, max_days: int = None,
) -> Dict[str, Any]:
    """交付成功后给客户门户 token 续期 —— **只延长 expires_at**。

    🔴 绝不调用 generate_client_token:那是轮换,会把客户手上的旧链接立刻作废。
       这里 token 值不变、is_active 不动、不签发新链接。

    幂等:new_expires_at <= 现有 expires_at 的 token 一行不写(重复事件零副作用)。
    复活:已过期但服务期内的 token 也会被延长(不复活等于续期机制白做)。
    NULL expires_at(永不过期)不动 —— 写日期只会**缩短**它。

    返回 {"status": renewed|noop|skipped, "reason": ..., "renewed": n, ...}
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        new_expires_at, service_end, why = compute_portal_token_renew_date(
            cursor, quote_id, today=today, grace_days=grace_days, max_days=max_days,
        )
        if new_expires_at is None:
            conn.close()
            return {
                "status": "skipped", "reason": why, "quote_id": int(quote_id),
                "renewed": 0, "service_end": str(service_end) if service_end else None,
            }

        cursor.execute(
            """
            SELECT id, expires_at FROM client_access_tokens
            WHERE quote_id = %s AND is_active = 1
            FOR UPDATE
            """,
            (int(quote_id),),
        )
        candidates = cursor.fetchall() or []
        renewed = [
            {"id": int(r["id"]), "old_expires_at": r["expires_at"]}
            for r in candidates
            # 只延长不缩短 —— 这一条就是幂等锁:new <= old 直接落空集,不写库。
            if r["expires_at"] is not None and r["expires_at"] < new_expires_at
        ]
        if not renewed:
            conn.commit()
            conn.close()
            return {
                "status": "noop", "reason": "already_covered", "quote_id": int(quote_id),
                "renewed": 0, "new_expires_at": str(new_expires_at),
                "service_end": str(service_end) if service_end else None,
            }

        cursor.execute(
            """
            UPDATE client_access_tokens
            SET expires_at = %s
            WHERE id = ANY(%s)
            """,
            (new_expires_at, [item["id"] for item in renewed]),
        )
        _record_portal_token_renew_audit(
            cursor, quote_id=int(quote_id), renewed=renewed,
            new_expires_at=new_expires_at, service_end=service_end, trigger=trigger,
            actor_user_id=actor_user_id, actor_username=actor_username,
            request_id=request_id, ip_address=ip_address, reason=reason,
        )
        conn.commit()
        conn.close()
        return {
            "status": "renewed", "reason": "ok", "quote_id": int(quote_id),
            "renewed": len(renewed), "new_expires_at": str(new_expires_at),
            "service_end": str(service_end) if service_end else None,
            "token_ids": [item["id"] for item in renewed],
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def renew_client_token_safe(quote_id: Optional[int], *, trigger: str, **kwargs) -> Dict[str, Any]:
    """交付主链专用包装:**续期失败绝不阻断交付**(工单 §1.3.5)。

    报告生成成功了就是成功了 —— 续期异常只记日志,不回滚、不报错给用户。
    """
    if not quote_id:
        return {"status": "skipped", "reason": "no_quote_id", "renewed": 0}
    try:
        result = renew_client_token(int(quote_id), trigger=trigger, **kwargs)
        print(f"[PortalTokenRenew] trigger={trigger} quote_id={quote_id} -> {result}")
        return result
    except Exception as exc:  # noqa: BLE001 —— 交付主链不可因续期失败而失败
        print(f"[PortalTokenRenew] 续期失败(不影响交付) trigger={trigger} quote_id={quote_id}: {exc}")
        return {"status": "error", "reason": str(exc)[:200], "renewed": 0}


def verify_client_token(token: str) -> Optional[int]:
    """验证Token，返回quote_id"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT quote_id, expires_at FROM client_access_tokens
            WHERE token = %s AND is_active = 1
        """, (token,))

        row = cursor.fetchone()
        if not row:
            conn.close()
            return None

        # 检查过期
        if row["expires_at"] and str(row["expires_at"]) < datetime.now().strftime("%Y-%m-%d"):
            conn.close()
            return None

        # 更新最后访问时间
        cursor.execute("""
            UPDATE client_access_tokens SET last_access_at = CURRENT_TIMESTAMP
            WHERE token = %s
        """, (token,))
        conn.commit()
        conn.close()
    
        return row["quote_id"]
    finally:
        try:
            conn.close()
        except Exception: pass


def verify_portal_token(token: str) -> Optional[Dict[str, Any]]:
    """验证门户Token，返回 {valid, quote_id, brand_name}（供中间件使用）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT quote_id, brand_name, expires_at
            FROM client_access_tokens
            WHERE token = %s AND is_active = 1
        """, (token,))

        row = cursor.fetchone()
        if not row:
            conn.close()
            return None

        if row["expires_at"] and str(row["expires_at"]) < datetime.now().strftime("%Y-%m-%d"):
            conn.close()
            return None

        conn.close()
        return {"valid": True, "quote_id": row["quote_id"], "brand_name": row["brand_name"] or ""}
    finally:
        try:
            conn.close()
        except Exception: pass


def get_client_token(quote_id: int) -> Optional[Dict[str, Any]]:
    """获取客户当前有效Token"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT * FROM client_access_tokens
            WHERE quote_id = %s AND is_active = 1
            ORDER BY created_at DESC LIMIT 1
        """, (quote_id,))
    
        row = cursor.fetchone()
        conn.close()
    
        if row:
            return dict(row)
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 媒体投放记录
# ==========================================

def add_publication(
    quote_id: int,
    platform_name: str,
    platform_url: str = None,
    article_title: str = None,
    publish_date: str = None,
    operator_id: str = None,
    article_id: int = None,       # P0.4 (CTO-15.7 2026-04-24) · 关联 articles 表
    screenshot_path: str = None,  # P0.4 · 代理手动投放证据截图
) -> int:
    """添加媒体投放记录

    P0.4 扩展:加 article_id / screenshot_path 参数
    (media_publications 表 schema 已有这 2 字段 · 仅函数层未暴露)
    用于承接"代理手动确认已发布 · 贴 URL + 可选截图"业务场景。
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO media_publications
            (quote_id, platform_name, platform_url, article_title, publish_date, operator_id,
             article_id, screenshot_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (quote_id, platform_name, platform_url, article_title, publish_date, operator_id,
              article_id, screenshot_path))

        pub_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return pub_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_publications(quote_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    """获取客户的媒体投放记录"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM media_publications
            WHERE quote_id = %s
            ORDER BY publish_date DESC, created_at DESC
            LIMIT %s
        """, (quote_id, limit))

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_publication_stats(quote_id: int) -> Dict[str, Any]:
    """获取客户投放统计"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT
                COUNT(*) as total_count,
                COUNT(DISTINCT platform_name) as platform_count
            FROM media_publications
            WHERE quote_id = %s
        """, (quote_id,))

        row = cursor.fetchone()

        # 按平台统计
        cursor.execute("""
            SELECT platform_name, COUNT(*) as count
            FROM media_publications
            WHERE quote_id = %s
            GROUP BY platform_name
        """, (quote_id,))
    
        platform_stats = {r["platform_name"]: r["count"] for r in cursor.fetchall()}
        conn.close()
    
        return {
            "total_count": row["total_count"],
            "platform_count": row["platform_count"],
            "by_platform": platform_stats
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 操作日志
# ==========================================

def log_operation(
    operator_id: str,
    action: str,
    target_type: str = None,
    target_id: int = None,
    details: dict = None,
    brand_id: int = None,
) -> int:
    """记录操作日志"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            INSERT INTO operation_logs (operator_id, brand_id, action, target_type, target_id, details)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (operator_id, brand_id, action, target_type, target_id, json.dumps(details) if details else None))

        log_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return log_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_operation_logs(
    operator_id: str = None,
    target_type: str = None,
    brand_id: int = None,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """查询操作日志(单 operator_id · 老接口保留 · 新 GET endpoint 走 get_operation_logs_multi)"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        query = "SELECT * FROM operation_logs WHERE 1=1"
        params = []

        if operator_id:
            query += " AND operator_id = %s"
            params.append(operator_id)

        if target_type:
            query += " AND target_type = %s"
            params.append(target_type)

        if brand_id is not None:
            query += " AND brand_id = %s"
            params.append(brand_id)

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_operation_logs_multi(
    operator_ids: List[str],
    target_type: str = None,
    brand_id: int = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """查询操作日志(多 operator_id 池过滤)

    CTO-15.23 2026-05-25:历史 5 处写入(monitoring_api.py:262/289/365/655)operator_id 传 brand_id 或 'system'
    · GET endpoint 非 admin 强制按 user_id 注入 operator_id 过滤 → 永不匹配 → "暂无操作日志"
    · 池过滤 = (user_id ∪ 该 user 拥有的所有 brand_id) · 兼容历史 brand_id 写入 + 未来真 user_id 写入
    """
    if not operator_ids:
        return []
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholders = ",".join(["%s"] * len(operator_ids))
        query = f"SELECT * FROM operation_logs WHERE operator_id IN ({placeholders})"
        params: list = list(operator_ids)

        if target_type:
            query += " AND target_type = %s"
            params.append(target_type)

        if brand_id is not None:
            query += " AND brand_id = %s"
            params.append(brand_id)

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 调度器辅助函数
# ==========================================

# [WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15 · Review 追加 🔴] `get_active_client_keywords()` 已删除。
#
# 它是"为监测取 extra 词"的**第 5 个出口**,而且**没有开关闸**。复审(2026-08-15)用结构锚
# 逐个映射 `FROM extra_keywords` 的全部出现点时抓到它 —— 本包原来的语义签名扫不到:
# 那个签名要求窗口内同时出现 entitlement_platforms + monitoring_query,而这函数两个都不 SELECT。
# 「全仓只有 monitoring_db.py 有取词路径」那句结论,当时是在一个**有洞的签名**下得的。
#
# 为什么删而不是加闸(三条,缺一条我就选加闸):
#   1. 它是死函数:全仓零非测试调用方(反向对照:同法查 get_keywords_for_monitoring 得 39 处);
#      `scheduler.py:146` 只在 docstring 里说"数据源已从它迁到 list_active_subscriptions";
#      `tests/test_daily_monitoring_charge.py` 那条锁只要求调度器**用新模型**,不要求它存在。
#   2. 它没有被 __all__ / star import 再导出,删掉不改变任何模块的对外面。
#   3. 🔴 决定性的一条:它缺的**不止**本包这个闸 —— 还缺服务锚
#      (`quote_service_anchor_condition_sql`)、缺 `exclude_keyword_subscription_owned`
#      (会与逐词订阅双跑双扣)、缺 `monitoring_status` 过滤。只补我这一个闸,会让一个
#      **仍然错三处**的函数看起来是现役的、安全的 —— 那是把陷阱粉刷一遍,比留着更危险。
#
# 要恢复这个能力,走唯一漏斗 `get_keywords_for_monitoring()`(它带全部四道口径)。
# 防复发:`tests/monitoring_optin_2026_08_15/test_optin_wiring.py::test_W1_*` 已改成
# **结构锚**口径 —— 枚举全仓每一处 `FROM extra_keywords` 并要求逐个归类,
# 新增未归类的出现点当场红(配成对反向对照,证明它抓得到一个"无闸取词函数")。


def get_expiring_tokens(days: int = 7) -> List[Dict[str, Any]]:
    """获取即将在N天内过期的Token"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT t.*, q.brand_name
            FROM client_access_tokens t
            LEFT JOIN quotes q ON t.quote_id = q.id
            WHERE t.is_active = 1
            AND t.expires_at::timestamp <= NOW() + (%s || ' days')::interval
            AND t.expires_at::timestamp > NOW()
        """, (days,))
    
        rows = cursor.fetchall()
        conn.close()
    
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def deactivate_expired_tokens() -> int:
    """停用所有已过期的Token，返回停用数量"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            UPDATE client_access_tokens
            SET is_active = 0
            WHERE is_active = 1 AND expires_at::timestamp <= NOW()
        """)
    
        count = cursor.rowcount
        conn.commit()
        conn.close()
    
        return count
    finally:
        try:
            conn.close()
        except Exception: pass

# ========== 监测专用统一适配层 ==========

def get_keywords_for_monitoring(
    quote_id: int = None,
    quote_ids: List[int] = None,
    keyword_keys: List[str] = None,
    brand_id: int = None,
    exclude_keyword_subscription_owned: bool = False,
    for_dispatch: bool = True,
) -> List[Dict[str, Any]]:
    """
    【监测专用】统一获取客户的所有待监测词条

    这是监测流程的单一数据源，所有监测代码都应调用此函数。
    自动合并 confirmed_keywords + extra_keywords，并正确获取 target_brand。

    Args:
        quote_id: 单个客户报价ID（对应quotes表的id）
        quote_ids: 多个报价ID列表（brand_id → 多 quote 场景）
        keyword_keys: 可选，指定词条key列表（格式："confirmed-123" 或 "extra-456"）
        brand_id: 品牌ID，当无 confirmed quote 时直接按 brand_id 查 extra_keywords
        exclude_keyword_subscription_owned: 旧报价调度专用；排除已有 active/paused
            逐词订阅所有权的 confirmed keyword，避免同一短句双跑双扣
        for_dispatch: 这批词是不是**要拿去跑并扣费**。默认 True = 加闸(fail-safe)。
            只有"渲染历史监测结果"的读路径才允许传 False，见下方 [WO_MONITORING_OPTIN]。

    Returns:
        统一格式的词条列表

    [WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15 · P0-A.2] extra 词默认不进监测
    ─────────────────────────────────────────────────────────────────────
    本函数是【所有】监测执行路径的唯一取词漏斗(2026-08-15 全仓枚举实证):
      · api/scheduler.py  _run_clients_with_billing / _async_run_brand   → 定时(scheduled_monitoring)
      · server.py         /api/monitoring/run-stream                     → 手动 SSE(monitor_single)
      · api/monitoring_api.py                                            → 同上 + 报表/导出
      · services/organization_worker.py                                  → 组织员工自动计划
      · tools/monitoring/batch_monitor.run_client_monitoring             → 上面几条的下游
    因此闸加在这里 = 一处堵住全部执行路径。扣费在**取词之后**按词数 freeze
    (server.py:9337 n_keywords → freeze_points),所以闸一收紧,冻结金额自动跟着降 —— 闸与钱同源。

    两条刻意的例外(都不是漏,是 Owner 2026-08-15 拍板):
      1) **显式点名放行**:keyword_keys 明确指定了某个 extra 词 = 代理在界面上勾了它并确认扣费,
         按元指令 #2「按钮级确认扣费」视为已确认,不再要求开关也开着。
         "跑全部"(不传 keyword_keys)一律按开关过滤。
      2) **for_dispatch=False 放行**:周报/CSV 导出这类【渲染历史结果】的读路径。
         把开关闸加到报表上会让"关掉的词"连同它已经跑出来、客户已经付过钱的历史数据
         一起从报告里消失 —— 那是数据丢失,不是省钱。默认 True 保证新调用方自动被闸住。

    🔴 服务锚 quote_unfulfilled_compliance_condition_sql 本单**刻意不动**(Owner 2026-08-15 定):
       它是履约/服务期语义,不是取词闸;给它加 is_monitored 会缩短服务期,且 get_client_keywords
       复用同一锚 → 可能让整张报价的词从界面消失。作为遗留项单独立单。
    """
    # 闸只作用于"取全部"的两条分支;显式 keyword_keys 分支按例外 1 放行。
    extra_dispatch_gate_sql = (
        "AND COALESCE(e.is_monitored, FALSE) = TRUE" if for_dispatch else ""
    )
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        results = []
    
        # 合并 quote_id 和 quote_ids
        all_quote_ids = []
        if quote_ids:
            all_quote_ids = list(quote_ids)
        if quote_id is not None and quote_id not in all_quote_ids:
            all_quote_ids.append(quote_id)

        subscription_ownership_filter = ""
        if exclude_keyword_subscription_owned:
            subscription_ownership_filter = """
                AND NOT EXISTS (
                    SELECT 1
                    FROM keyword_monitor_subscriptions kms
                    WHERE kms.keyword_id = k.id
                )
            """
    
        if keyword_keys:
            # 按指定的key列表获取
            for key in keyword_keys:
                parts = key.rsplit('-', 1)
                if len(parts) != 2:
                    continue
                source, kid = parts[0], int(parts[1])
            
                # [2026-06-07 老板复审 P1] keyword_keys 不能裸按 k.id 查 —— 必须受 quote scope + 服务锚口径限制,
                #   否则"已解析的合法 quote scope"与"实际跑的 keyword id"脱钩(可指向非 scope/脏 quote 的词)。
                if source == 'confirmed':
                    if not all_quote_ids:
                        # 无 quote scope(brand-only fallback)→ confirmed key 无服务锚依据 · 禁裸查 · 不返回
                        continue
                    cursor.execute(f"""
                        SELECT
                            k.id,
                            k.quote_id,
                            k.keyword,
                            k.monitoring_query,
                            k.monitoring_product_version,
                            m.platforms AS entitlement_platforms,
                            q.brand_name as target_brand,
                            k.category as difficulty,
                            'confirmed' as source
                        FROM confirmed_keywords k
                        JOIN public.monitoring_product_platform_matrices m
                          ON m.version = k.monitoring_product_version
                        JOIN quotes q ON k.quote_id = q.id
                        WHERE k.id = %s
                          AND k.quote_id = ANY(%s)
                          -- [2026-06-07 解冲突 · 批C×hotfix 叠加] 超红海词此处【不排除】——
                          --   批C §4.2 反转:超红海重新进监测·不隐藏(达标仍在 daily compliance ~3564 独立 skip);
                          --   hotfix:仅按 scope(quote_id IN 已解析 scope)+ 服务锚口径限制·防 keyword_keys 指向非 scope/未锚 quote。
                          AND {quote_service_anchor_condition_sql("q")}
                          {subscription_ownership_filter}
                    """, (kid, all_quote_ids))
                else:
                    if all_quote_ids:
                        cursor.execute(f"""
                            SELECT
                                e.id,
                                e.quote_id,
                                e.keyword,
                                e.monitoring_query,
                                NULL::TEXT AS monitoring_product_version,
                                e.platforms AS entitlement_platforms,
                                COALESCE(e.target_brand, q.brand_name) as target_brand,
                                e.difficulty,
                                'extra' as source
                            FROM extra_keywords e
                            JOIN quotes q ON e.quote_id = q.id
                            WHERE e.id = %s
                              AND e.quote_id = ANY(%s)
                              AND e.status = 'active'
                              -- [2026-06-07 老板复审 fix3] SSOT 自保护:extra keyword_keys 也 JOIN quote 套服务锚(不只依赖上游 scope)
                              AND {quote_service_anchor_condition_sql("q")}
                        """, (kid, all_quote_ids))
                    elif brand_id:
                        # brand-only fallback(无任何 quote)→ 仅允许该 brand 的 active extra(无 quote 绑定场景)
                        cursor.execute("""
                            SELECT
                                e.id,
                                e.quote_id,
                                e.keyword,
                                e.monitoring_query,
                                NULL::TEXT AS monitoring_product_version,
                                e.platforms AS entitlement_platforms,
                                COALESCE(e.target_brand, b.name) as target_brand,
                                e.difficulty,
                                'extra' as source
                            FROM extra_keywords e
                            LEFT JOIN brands b ON e.brand_id = b.id
                            WHERE e.id = %s
                              AND e.brand_id = %s
                              AND e.status = 'active'
                        """, (kid, brand_id))
                    else:
                        continue

                row = cursor.fetchone()
                if row:
                    results.append(dict(row))
        else:
            # 获取该客户的所有关键词（支持多 quote_id）
            if not all_quote_ids:
                # 没有 quote 但有 brand_id → 直接查该品牌的 extra_keywords
                if brand_id:
                    # [WO_MONITORING_OPTIN 2026-08-15] brand 级"跑全部" · 加开关闸
                    cursor.execute(f"""
                        SELECT
                            e.id,
                            e.quote_id,
                            e.keyword,
                            e.monitoring_query,
                            NULL::TEXT AS monitoring_product_version,
                            e.platforms AS entitlement_platforms,
                            COALESCE(e.target_brand, b.name) as target_brand,
                            e.difficulty,
                            'extra' as source
                        FROM extra_keywords e
                        LEFT JOIN brands b ON e.brand_id = b.id
                        WHERE e.brand_id = %s AND e.status = 'active'
                          {extra_dispatch_gate_sql}
                    """, (brand_id,))
                    results.extend([dict(row) for row in cursor.fetchall()])
                    conn.close()
                    return results
                conn.close()
                return []
        
            placeholders = ','.join(['%s' for _ in all_quote_ids])
        
            # 1. confirmed_keywords(只取已确认/已付款报价单的词条 · B2.1 修复 'paid' 漏)
            cursor.execute(f"""
                SELECT
                    k.id,
                    k.quote_id,
                    k.keyword,
                    k.monitoring_query,
                    k.monitoring_product_version,
                    m.platforms AS entitlement_platforms,
                    q.brand_name as target_brand,
                    k.category as difficulty,
                    'confirmed' as source
                FROM confirmed_keywords k
                JOIN public.monitoring_product_platform_matrices m
                  ON m.version = k.monitoring_product_version
                JOIN quotes q ON k.quote_id = q.id
                WHERE k.quote_id IN ({placeholders})
                  AND (k.is_core IS NOT FALSE)
                  -- [2026-06-07 老板复审 fix3] SSOT 自保护:普通 confirmed 分支也套服务锚口径(不只依赖上游传入干净 scope)
                  AND {quote_service_anchor_condition_sql("q")}
                  {subscription_ownership_filter}
                  -- [2026-06-06 反转 §4.2 · 监测不隐藏] 超红海词重新进监测任务(有逐引擎数据)· 达标仍在 daily compliance ~3564 独立排除
            """, all_quote_ids)
            results.extend([dict(row) for row in cursor.fetchall()])

            # 2. extra_keywords(同上修)
            cursor.execute(f"""
                SELECT
                    e.id,
                    e.quote_id,
                    e.keyword,
                    e.monitoring_query,
                    NULL::TEXT AS monitoring_product_version,
                    e.platforms AS entitlement_platforms,
                    COALESCE(e.target_brand, q.brand_name) as target_brand,
                    e.difficulty,
                    'extra' as source
                FROM extra_keywords e
                JOIN quotes q ON e.quote_id = q.id
                WHERE e.quote_id IN ({placeholders}) AND e.status = 'active'
                  -- [2026-06-07 老板复审 fix3] SSOT 自保护:普通 extra 分支也套服务锚口径
                  AND {quote_service_anchor_condition_sql("q")}
                  -- [WO_MONITORING_OPTIN 2026-08-15] quote 级"跑全部" · 加开关闸(默认关)
                  {extra_dispatch_gate_sql}
            """, all_quote_ids)
            results.extend([dict(row) for row in cursor.fetchall()])
    
        conn.close()
        return results
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 关键词达标倒计时
# ==========================================

# 套餐 → 目标检出率
_TIER_TARGET_MAP = {
    "entry": 50,
    "standard": 65,
    "premium": 75,
    "flagship": 75,
}


# ==========================================
# [CTO-15.23 2026-05-13] v3 公平算法 helper
# ==========================================
# 老板痛点(2026-05-13 截图):4/4 全检出 100% · effective_rate 显示 32.7% · 永远拉不到 target=50
# 根因:v2 算法用"首次达标至今"全期窗口 · 分母 N+1 永远稀释 · 1 天 100% 后掉 N 天 →
#       再打 100% 拉不回 target(老板原话"倒反天罡 · 永远还债")
# v3 修法:
#   1. 用近 N 天滚动窗口替换全期窗口(默认 N=7)
#   2. effective_rate = max(detection_rate, rolling_avg) — 打到了立刻承认 · 不被历史拖累
#   3. 连续 M 天 is_compliant=FALSE → 切瞬时 detection_rate(暴露懒政 · 防代理偷懒)
# 灰度:settings.compliance_algorithm_v3_enabled / compliance_v3_whitelist.quote_ids
def _is_v3_algorithm_active(quote_id: int) -> bool:
    """检查 v3 算法对当前 quote 是否生效(全局开关 OR 白名单命中)"""
    try:
        from config.settings_manager import load_settings
        s = load_settings()
        if getattr(s, "compliance_algorithm_v3_enabled", False):
            return True
        wl = getattr(s, "compliance_v3_whitelist", {}) or {}
        return int(quote_id) in (wl.get("quote_ids") or [])
    except Exception:
        return False


def _get_effective_window(cur, keyword_id: int, quote_id: int, allow_confirmed: bool = False,
                          keyword_source: str = 'confirmed'):
    """v1.7 · 服务窗口完整起止 = (start, end) tuple · 替代 v1.4 _get_effective_start_date

    [Deploy-CTO 2026-05-30] allow_confirmed 参数:默认 False = paid-only(所有现有调用点行为不变)·
      仅 run_daily_compliance_check 显式传 True · 放行玩法B confirmed 已交付真客户算履约 ·
      confirmed 无服务锚(service_start_date/paid_at 全 NULL)仍 fail-closed(contract_start 兜底)

    根因(老板 v1.7 复审 P0):v1.4 只过滤起点 effective_start · 服务到期后仍计履约
      → 璧山 paid 30 天已过期的 quote · 只要 status='paid' KMS active 仍跑 + 计 compliant_days
      → portal 显示"剩余 X 天"错位(实际服务已结束)

    修法:helper 返完整窗口
      effective_start = MAX(quotes.paid_at::DATE, kms.enabled_at::DATE)
      effective_end   = MAX(quotes.service_end_date, 今天) —— 见下方 🔴 两条裁定的调和
        contract_start 优先 service_start_date · 兜底 paid_at::date(2026-05-29 D2 锚翻转 · 单一服务锚)

    🔴 [服务期 SSOT 2026-08-06 §1.3] 旧 effective_end = `contract_start + service_days`,
       service_days 取不到还兜底 365。那是把**履约达标天数配额**当日历天用 ——
       出现率算法的历史窗口因此比合同期长出近一年(11/13 张 paid 报价中招)。

    🔴 为什么是 MAX(service_end_date, 今天) 而不是直接 service_end_date:
       这里有**两条 Owner 裁定打架**,而假钟一直把矛盾掩着:
         · v1.7(2026-05-29)加了自然日历上界,理由"防过期后日志计履约";
         · A 方案(2026-06-04 / 06-23)推翻它:"不设自然日历封顶 · 过了日历窗口仍继续
           累计达标天数,直到 compliant_days 满 service_days"。
       代码里 v1.7 那个上界从没被删,只是被假钟推到了 ~2027 年,所以**从未真正生效过**。
       如果这次"老老实实"把它接到 service_end_date 上,等于**激活**一条 Owner 已经废除的封顶:
       生产实测 23 张有履约日志的 quote 里会有 9 张当场被截断(#178/273/282/287/294/295/315/317/366),
       它们的出现率会从"历史平滑"掉成"当期实时"。
       所以取 MAX(合同结束日, 今天):口径只读 SSOT(`+ service_days` 派生彻底消失),
       又与 SQL 里既有的 `check_date < CURRENT_DATE` 叠加成**与当前生产逐行等价**(零截断实测)。
       真要不要重新设封顶 = 商业决策,挂 OWNER_DECISION_BRIEF 第 3 项,本包不替 Owner 拍。

    ⚠️ 本 helper 只界定"出现率 averaging 窗口",**不是交付停止条件**:
       交付完成仍由 compliant_days >= service_days 决定(Owner A 方案),那条口径本包不动。

    compliance log 查询全部加 `AND check_date BETWEEN effective_start AND effective_end`
    scheduler list_active 也用 effective_end 做 CURRENT_DATE < effective_end 过滤

    fail-closed:
      - quote.paid_at IS NULL 且 service_start_date IS NULL → 返 (None, None)
      - quote.status != 'paid' → 返 (None, None)
      - service_end_date IS NULL(未激活服务期)→ 返 (None, None)

    返:Tuple[Optional[date], Optional[date]]
    """
    cur.execute("""
        SELECT
            q.paid_at::date AS paid_start,
            q.service_start_date AS service_start,
            q.service_end_date AS service_end,
            (
                -- [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-4] 必须带 keyword_source。
                --   本单之前 extra 没有订阅,只按 keyword_id 查不会撞;现在两种词都有订阅了,
                --   两表 id 撞车时会把**另一种词**的 enabled_at 当成本词的服务窗口起点,
                --   算出来的达标天数记到错的词/错的客户头上。
                SELECT MAX(s.enabled_at::date)
                FROM keyword_monitor_subscriptions s
                WHERE s.keyword_id = %s
                  AND s.keyword_source = %s
                  AND s.quote_id = %s
                  AND s.status IN ('active', 'paused_low_balance')
            ) AS kms_start
        FROM quotes q
        -- [Deploy-CTO 2026-05-30] allow_confirmed=True 时也放行 confirmed(玩法B 已交付真客户)·
        -- 默认 False 保持 paid-only(其他调用点 0 行为变化)· paid 分支字符串仍在(不破 v1.4 测试守护)
        WHERE q.id = %s AND (q.status = 'paid' OR (%s AND q.status = 'confirmed'))
    """, (keyword_id, str(keyword_source or 'confirmed').strip().lower(),
          quote_id, quote_id, allow_confirmed))
    row = cur.fetchone()
    if not row:
        return (None, None)

    # [2026-05-29 D2 锚翻转] service_start_date 优先(权威服务锚)· paid_at 兜底(财务/老 quote)
    contract_start = row.get("service_start") or row.get("paid_start")
    if not contract_start:
        return (None, None)

    kms = row.get("kms_start")
    effective_start = max(contract_start, kms) if kms else contract_start

    # [服务期 SSOT 2026-08-06] 上界只从 service_end_date 来(不拿配额/365 兜),
    #   再按 A 方案抬到不早于今天(理由见 docstring 里那两条打架的裁定)。
    from datetime import date as _date_ew
    from services.service_period import coerce_date as _sp_coerce_date

    service_end = _sp_coerce_date(row.get("service_end"))
    if service_end is None:
        # 没有服务期 = 没有服务关系,fail-closed(不给一个凭空的窗口)
        return (None, None)
    effective_end = max(service_end, _date_ew.today())

    return (effective_start, effective_end)


def _get_effective_start_date(cur, keyword_id: int, quote_id: int):
    """v1.4 helper · v1.7 后保留作 backward compat alias · 仅返起点

    新代码请用 _get_effective_window(返 start, end tuple)· 履约上下界都过滤
    """
    start, _end = _get_effective_window(cur, keyword_id, quote_id)
    return start


def _compute_effective_rate_v3(
    cur,
    kw_id: int,
    kw_source: str,
    quote_id: int,
    detection_rate: float,
    rolling_window_days: int = 7,
    lazy_threshold_days: int = 7,
    effective_start = None,
    effective_end = None,  # v1.7 服务窗口上界
) -> tuple[float, int, int]:
    """
    v3 公平算法核心计算

    返回 (effective_rate, hist_days, consec_below_days):
      - effective_rate: 算法输出的"显示出现率"
      - hist_days: 窗口内有数据的天数(给 logging 用)
      - consec_below_days: 连续不达标天数(给 logging / debug 用)

    算法步骤:
      1. 查近 N 天历史(不含今日) → hist_avg / hist_days
      2. rolling_with_today = (hist_avg × hist_days + detection_rate) / (hist_days + 1)
      3. 从最近一天往前数连续 is_compliant=FALSE → consec_below
      4. if consec_below >= lazy_threshold: effective = detection_rate(暴露真实)
         else: effective = max(detection_rate, rolling_with_today)(打到了承认)
    """
    # [v1.4 2026-05-29 / v1.7 2026-05-29 老板复审 P0]
    # 服务窗口过滤 · 仅算 check_date BETWEEN effective_start AND effective_end 的历史
    #   start = MAX(quote.paid_at, kms.enabled_at) · 防未付期 / KMS 未启用前日志入算
    #   end   = contract_start + service_days · 防过期后日志计履约(老板 v1.7 P0 复审补)
    if effective_start is None or effective_end is None:
        s, e = _get_effective_window(cur, kw_id, quote_id)
        if effective_start is None:
            effective_start = s
        if effective_end is None:
            effective_end = e
    # 仍可能 None(quote 未付 / paid_at NULL)· 此时返回早期 detection_rate(不抛错 · 调用方决定)

    # 1. 近 N 天历史 avg(不含今日)· v1.7 加上下界
    cur.execute(f"""
        SELECT AVG(detection_rate) AS avg_rate, COUNT(*) AS days
        FROM keyword_compliance_log
        WHERE keyword_id = %s AND keyword_source = %s AND quote_id = %s
          AND check_date >= CURRENT_DATE - INTERVAL '{int(rolling_window_days)} days'
          AND check_date < CURRENT_DATE
          AND (%s IS NULL OR check_date >= %s)   -- v1.4 服务窗口下限(含)
          AND (%s IS NULL OR check_date < %s)    -- v1.8 服务窗口上限(排他 · 跟 scheduler 口径一致)
    """, (kw_id, kw_source, quote_id, effective_start, effective_start, effective_end, effective_end))
    row = cur.fetchone() or {}
    hist_avg = float(row.get("avg_rate")) if row.get("avg_rate") is not None else 0.0
    hist_days = int(row.get("days") or 0)

    # 2. rolling 含今日
    if hist_days > 0:
        rolling = (hist_avg * hist_days + detection_rate) / (hist_days + 1)
    else:
        rolling = detection_rate

    # 3. 从最近往前连续不达标天数(扫近 lazy_threshold 天历史)· v1.7 加上下界
    cur.execute(f"""
        SELECT check_date, is_compliant
        FROM keyword_compliance_log
        WHERE keyword_id = %s AND keyword_source = %s AND quote_id = %s
          AND check_date >= CURRENT_DATE - INTERVAL '{int(lazy_threshold_days)} days'
          AND check_date < CURRENT_DATE
          AND (%s IS NULL OR check_date >= %s)   -- v1.4 服务窗口下限(含)
          AND (%s IS NULL OR check_date < %s)    -- v1.8 服务窗口上限(排他)
        ORDER BY check_date DESC
    """, (kw_id, kw_source, quote_id, effective_start, effective_start, effective_end, effective_end))
    consec_below = 0
    for r in cur.fetchall():
        if r.get("is_compliant"):
            break
        consec_below += 1

    # 4. 决策
    if consec_below >= lazy_threshold_days:
        # 懒政模式:暴露真实 · 让代理看到 detection_rate 立刻补素材
        effective_rate = detection_rate
    else:
        # 常态:max 保护 · 打到了立刻承认 · 不被历史拖累
        effective_rate = max(detection_rate, rolling)

    return round(effective_rate, 1), hist_days, consec_below


def compute_live_effective_rates(quote_id: int, realtime_map: dict) -> dict:
    """[2026-06-30 kou-jing tong-yi] read-time recompute per-keyword canonical effective (incl today live detection).
    realtime_map: {keyword_id: (keyword_source, realtime_detection_rate_float)}
    return: {keyword_id: live_effective_rate_float}. single conn, read-only, fail-open.
    """
    if not realtime_map:
        return {}
    conn = get_connection()
    out = {}
    try:
        cur = conn.cursor()
        for kw_id, (kw_source, rt_rate) in realtime_map.items():
            try:
                eff, _hd, _cb = _compute_effective_rate_v3(
                    cur, kw_id, kw_source or "confirmed", quote_id, float(rt_rate or 0.0),
                )
                out[kw_id] = float(eff)
            except Exception:
                out[kw_id] = float(rt_rate or 0.0)
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_unified_appearance(quote_id: int, keywords: list = None) -> dict:
    """[2026-06-30 kou-jing tong-yi single authority] shared by 3 views.
    return {'per_keyword': {kw_id: live_effective}, 'client_avg': float|None}.
    client_avg only over monitoring-lifecycle (first_detected_date not null) keyword set, round 1.
    """
    if keywords is None:
        keywords = get_client_keywords(quote_id) or []
    realtime_map = {}
    monitoring_ids = []
    for kw in keywords:
        if kw.get("super_red_ocean") or kw.get("is_core") is False:
            continue
        try:
            rt = float(kw.get("detection_rate", 0) or 0)
        except (TypeError, ValueError):
            rt = 0.0
        realtime_map[kw["id"]] = (kw.get("source", "confirmed"), rt)
        if kw.get("first_detected_date"):
            monitoring_ids.append(kw["id"])
    live = compute_live_effective_rates(quote_id, realtime_map)
    vals = [live[k] for k in monitoring_ids if k in live]
    client_avg = round(sum(vals) / len(vals), 1) if vals else None
    return {"per_keyword": live, "client_avg": client_avg}


def run_daily_compliance_check():
    """
    每日达标判定（v2 - 历史平滑算法）：

    核心逻辑：
    ─────────────────────────────────────────────────
    ▸ 达标前：用当天实时检出率判定（纯4平台加权，无历史包袱）
    ▸ 达标后：用「首次达标以来的累计平均出现率」判定
      - 避免达标前的低分数拖慢累计
      - 避免达标后偶尔一天波动导致不达标

    示例：目标50%，连续10天100%后某天降到40%
    - 旧算法：当天40% < 50%，不达标 ✗
    - 新算法：(100×10 + 40) / 11 ≈ 94.5% >= 50%，仍达标 ✓
    """
    import logging
    logger = logging.getLogger("GEO-Compliance")

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # [v1.4 2026-05-29] 严格 paid-only · 对齐 list_active_subscriptions / get_client_keywords
        # 旧版 status IN ('confirmed','paid') 会把 brand 6/428 类 confirmed 未付的 quote 也跑 compliance
        # → 写入 keyword_compliance_log 错位行 → portal 显示"剩余 -N 天"等
        cursor.execute(f"""
            SELECT id, tier, service_days, service_start_date
            FROM quotes
            WHERE {quote_service_anchor_condition_sql("quotes")}
              AND (
                   status = 'paid'
               -- [Deploy-CTO 2026-05-30] 也覆盖玩法B已交付 confirmed 真客户(有服务锚 + 有 active 监测订阅)
               -- 不增成本:监测引擎(写 monitoring_results)已在跑 · 这里只把现成数据算成 effective/达标到今天
               -- 纯 confirmed brand(无 paid quote · 如雅栖)否则永不进遍历 → 达标冻结在 backfill 日
               -- 无锚 confirmed(草稿态)由 COALESCE NULL + _get_effective_window fail-closed 自然排除
               OR (status = 'confirmed'
                   AND COALESCE(service_start_date, paid_at) IS NOT NULL
                   AND EXISTS (SELECT 1 FROM keyword_monitor_subscriptions s
                               WHERE s.quote_id = quotes.id
                                 AND s.status IN ('active', 'paused_low_balance')))
              )
        """)
        # 🔴 [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-4] 手动词怎么进这个循环 —— 三种情况说清楚,
        #    不留"以后再说":
        #
        #    (a) 手动词挂在**有服务锚的** quote 上(生产 14 条 extra 里 quote 311/354/386 属此类):
        #        本单 P0-3 让开开关时建 active 订阅,上面那个 EXISTS 自然命中 ⇒ 整张单进循环,
        #        循环体的 get_client_keywords(quote_id) 本来就返回 extra 支(source='extra'),
        #        且 extra 支 is_core 恒 TRUE ⇒ 不会被 is_core 那道 skip 拦掉 ⇒ 达标日志开始写。
        #        这就是 QZQZ(brand 662 / quote 386)「8/15 刚跑过却显示 0/365 天」的根治点:
        #        不是没跑,是没订阅所以整张单不进循环。
        #
        #    (b) 手动词挂的 quote **没有服务锚**(draft / 无 paid_at 且无 service_start_date;
        #        生产实例 quote 249 是 draft、quote 31/328 无 paid_at):
        #        本查询的 quote_service_anchor_condition_sql 会把它挡在外面 ⇒ 不写达标日志。
        #        🔴 这是 K3 的**必然结果,不是漏**:达标 = 对服务期的履约度量,而 K3 明确
        #        手动词不挂报价单生命周期、没有服务期承诺。它照常**跑和扣**(取数 extra 臂
        #        不套服务锚),只是不累计"达标天"。与 P0-5 不自动归档同一条推论。
        #
        #    (c) 手动词 **quote_id 为空**(brand 级):本循环按 quote 遍历,永远进不来。
        #        同 (b) 的理由 —— 没有 quote 就没有 service_days,"达标 N/365" 这个量本身不存在。
        #        ⇒ 方案 = **显式不累计达标**,而不是编一个默认服务期把它塞进来。
        #        生产今天此类 0 条(14 条 extra 全部 quote_id 非空,2026-08-16 实测),
        #        但列可空,所以取数 extra 臂用 LEFT JOIN quotes 让它照样能跑;
        #        这里则明确它不进达标统计。两处口径一致:**能跑,不计达标**。
        quotes = [dict(r) for r in cursor.fetchall()]
        conn.close()

        total_checked = 0
        total_compliant = 0
        skipped_no_effective_start = 0  # v1.4 监控指标
        skipped_outside_service_window = 0  # v1.8 监控指标(过期 / 未开始)
        skipped_not_core = 0  # [审计 2026-06-07] 非核心词(is_core=False)跳过达标计数 · 可追溯
        skipped_super_red_ocean = 0  # [审计 2026-06-07] 超红海词(super_red_ocean=True)跳过达标计数 · 可追溯

        for q in quotes:
            quote_id = q["id"]
            tier = q.get("tier") or "standard"
            target_rate = _TIER_TARGET_MAP.get(tier, 65)

            keywords = get_client_keywords(quote_id)

            conn2 = get_connection()
            cur2 = conn2.cursor()
            for kw in keywords:
                kw_id = kw["id"]
                kw_source = kw.get("source", "confirmed")
                # [Deploy-CTO 2026-05-30] 只处理本 quote 真实归属的词 · 根治 quote_id 飘移
                # get_client_keywords 按 brand 聚合会带出同 brand 别 quote 的词(罗平 280 遍历会带出 282 的 2300)
                # 旧逻辑用外层 quote_id 写 → 2300 被写到 paid quote 280 名下 → 展示层按 282 查漏看 → effective 卡旧值
                # 现在每词只被它真实归属 quote 处理一次(282 遍历时处理 2300)· 用真实 quote 算窗口/tier/写入 · 不飘移
                if kw.get("quote_id") is not None and kw.get("quote_id") != quote_id:
                    continue
                # [C1 2026-06-05] 达标统计只覆盖「客户已选付费核心词」(is_core)· 与按勾选核心词计价口径对齐
                #   相关搜索/覆盖词(confirmed is_core=False · 不监控/不承诺达标)不计达标统计 · 防计价 vs 达标口径打架
                #   confirmed 核心词(is_core=True · 含玩法B真客户)+ extra 监控词(is_core=True)仍计 · 不破坏 feedback_confirmed_counts_compliance
                if kw.get("is_core") is False:
                    skipped_not_core += 1
                    continue
                # [§4.2 2026-06-06] 超红海词(竞争极度饱和·尽力价不保证)不进达标计数 ——
                #   不写 keyword_compliance_log·不计 total_checked/total_compliant·不污染出现率/趋势/徽章。
                #   v3 托底算法不动(只在 loop 前置 skip)· 与 feedback_confirmed_counts_compliance 不冲突。
                if kw.get("super_red_ocean") is True:
                    skipped_super_red_ocean += 1
                    continue
                detection_rate = float(kw.get("detection_rate", 0) or 0)

                # [v1.4 2026-05-29 / v1.7 2026-05-29 / 续费履约 2026-06-23]
                # 服务窗口 = (effective_start, effective_end)
                #   start = MAX(quote.paid_at, kms.enabled_at)
                #   end   = contract_start + service_days(仅供 v2/v3 出现率算法窗口使用,不再作为交付停止条件)
                # None = quote 未付款 / paid_at NULL(legacy 脏数据)→ 跳过(fail-closed)
                #
                # 续费履约口径:在 INSERT keyword_compliance_log 前只判未开始 / 已累计达标完成。
                #   日历窗口结束但达标天数未满 → 继续写当天 log,继续服务。
                # [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-4] 传真实来源(kw_source 上面已取)。
                #   get_client_keywords 两支返回的 id 分别来自 confirmed_keywords / extra_keywords,
                #   不传来源就会在 id 撞车时取到另一种词的订阅起点。
                effective_start, effective_end = _get_effective_window(
                    cur2, kw_id, quote_id, allow_confirmed=True,
                    keyword_source=('extra' if kw_source == 'extra' else 'confirmed'),
                )
                if effective_start is None or effective_end is None:
                    skipped_no_effective_start += 1
                    continue
                from datetime import date as _date_v18
                _today_v18 = _date_v18.today()
                # [履约口径 2026-06-04] A 方案纯履约 · 取代 v1.8 自然日历过期 skip
                #   旧:自然日历到期会 skip 不写 log
                #   新:① 仍 skip 未开始(today < start)· ② 去掉"自然日历到期"skip(不设日历封顶)·
                #       ③ 新增完成 skip:compliant_days >= service_days(达标天数满 = 服务完成 → 停写)
                #   ⚠️ effective_end 仍传给出现率算法(_compute_effective_rate_v3 / v2 窗口)· 出现率算法不动。
                # [累计达标30天 2026-06-05] 交付完成 criterion = 累计达标天数(下方 :3582 从 effective_start 数
                #   is_compliant 天数 >= service_days · 无自然日历封顶)→ 已与「自然月服务期」拆开。
                #   产品口径「累计达标30天」对应 service_days=30(累计达标日数·非连续·非日历天)。
                #   effective_end 在此【仅】界定出现率averaging窗口(v2/v3 rate 算法内·非交付criterion)·
                #   memory feedback_confirmed_counts_compliance「v3 托底算法别动」→ 不碰 v3。
                #   🔜 Phase 2(累计达标30天状态机):rate averaging 窗口与服务期彻底解耦 + 客户面 N/30 进度展示。
                if _today_v18 < effective_start:
                    skipped_outside_service_window += 1
                    continue
                # 完成判定:本词在服务窗口下界之后累计达标天数 >= service_days → 服务完成 · 不再写当天 log
                # [服务期 SSOT 2026-08-06] 配额读取统一走 compliance_target_days(无 365 兜底)
                _svc_days_int = compliance_target_days(q)
                if _svc_days_int:
                    cur2.execute("""
                        SELECT COUNT(*) FILTER (WHERE is_compliant = TRUE) AS done_days
                        FROM keyword_compliance_log
                        WHERE keyword_id = %s AND keyword_source = %s AND quote_id = %s
                          AND check_date >= %s
                    """, (kw_id, kw_source, quote_id, effective_start))
                    _done = int((cur2.fetchone() or {}).get("done_days") or 0)
                    if _done >= _svc_days_int:
                        skipped_outside_service_window += 1
                        continue

                # ── 查询该关键词是否曾经达标过（取首次达标日期）── v1.7 加服务期上下界
                cur2.execute("""
                    SELECT MIN(check_date) as first_compliant_date
                    FROM keyword_compliance_log
                    WHERE keyword_id = %s AND keyword_source = %s AND quote_id = %s
                      AND is_compliant = TRUE
                      AND check_date >= %s  -- v1.4 服务窗口下限(含)
                      AND check_date < %s   -- v1.8 服务窗口上限(排他 · 过期当天不计达标)
                """, (kw_id, kw_source, quote_id, effective_start, effective_end))
                fcd_row = cur2.fetchone()
                first_compliant_date = fcd_row["first_compliant_date"] if fcd_row else None

                # [CTO-15.23 2026-05-13] v3 公平算法切换 · 修"32.7% 倒反天罡"
                # v3 启用条件:全局开关 OR quote_id 在白名单 · 见 _is_v3_algorithm_active
                # v2 兜底:flag 关 或 quote 不在白名单 走老算法(0 regression)
                use_v3 = _is_v3_algorithm_active(quote_id)
                if use_v3:
                    try:
                        from config.settings_manager import load_settings as _ls
                        _s = _ls()
                        _window = int(getattr(_s, "compliance_v3_rolling_window_days", 7) or 7)
                        _lazy = int(getattr(_s, "compliance_v3_lazy_threshold_days", 7) or 7)
                    except Exception:
                        _window, _lazy = 7, 7
                    effective_rate, hist_days, consec_below = _compute_effective_rate_v3(
                        cur2, kw_id, kw_source, quote_id, detection_rate,
                        rolling_window_days=_window, lazy_threshold_days=_lazy,
                        effective_start=effective_start,  # v1.4 复用 caller 算出的窗口起点
                        effective_end=effective_end,      # v1.7 复用 caller 算出的窗口终点
                    )
                elif first_compliant_date:
                    # ── v2 达标后：累计平均出现率（含今天的实时值）── v1.7 加上下界
                    cur2.execute("""
                        SELECT AVG(detection_rate) as avg_rate, COUNT(*) as days
                        FROM keyword_compliance_log
                        WHERE keyword_id = %s AND keyword_source = %s AND quote_id = %s
                          AND check_date >= GREATEST(%s, %s)
                          AND check_date < CURRENT_DATE
                          AND check_date < %s  -- v1.8 服务窗口上限(排他)
                    """, (kw_id, kw_source, quote_id, first_compliant_date, effective_start, effective_end))
                    hist = cur2.fetchone()
                    hist_avg = float(hist["avg_rate"]) if hist and hist["avg_rate"] else 0
                    hist_days = int(hist["days"]) if hist and hist["days"] else 0

                    # 把今天的实时值也纳入平均
                    effective_rate = round((hist_avg * hist_days + detection_rate) / (hist_days + 1), 1)
                else:
                    # ── v2 达标前：纯实时检出率 ──
                    effective_rate = detection_rate

                is_compliant = effective_rate >= target_rate

                # [CTO-15.23 2026-05-10] 老板拍板 A · is_stable 标志区分"刚达标 vs 稳定达标"
                # 稳定定义: 最近 7 天(前 6 天历史 + 今天本次判定)≥ 5 天 is_compliant=TRUE
                # 不影响 compliant_days 累积语义 · 续费/合同/服务期 0 触碰
                cur2.execute("""
                    SELECT COUNT(*) AS prev_6d_compliant
                    FROM keyword_compliance_log
                    WHERE keyword_id = %s AND keyword_source = %s AND quote_id = %s
                      AND check_date BETWEEN (CURRENT_DATE - INTERVAL '6 days')
                                         AND (CURRENT_DATE - INTERVAL '1 day')
                      AND check_date >= %s  -- v1.4 服务窗口下限(含 · 防 KMS enabled 前的天数算入 stable)
                      AND check_date < %s   -- v1.8 服务窗口上限(排他 · 过期当天不算 stable)
                      AND is_compliant = TRUE
                """, (kw_id, kw_source, quote_id, effective_start, effective_end))
                prev_6d = int((cur2.fetchone() or {}).get("prev_6d_compliant") or 0)
                recent_7d_compliant = prev_6d + (1 if is_compliant else 0)
                is_stable = recent_7d_compliant >= 5

                cur2.execute("""
                    INSERT INTO keyword_compliance_log
                        (keyword_id, keyword_source, quote_id, check_date,
                         detection_rate, effective_rate, target_rate, is_compliant, is_stable)
                    VALUES (%s, %s, %s, CURRENT_DATE, %s, %s, %s, %s, %s)
                    ON CONFLICT (keyword_id, keyword_source, check_date)
                    DO UPDATE SET detection_rate = EXCLUDED.detection_rate,
                                 effective_rate = EXCLUDED.effective_rate,
                                 target_rate = EXCLUDED.target_rate,
                                 is_compliant = EXCLUDED.is_compliant,
                                 is_stable = EXCLUDED.is_stable
                """, (kw_id, kw_source, quote_id,
                      detection_rate, effective_rate, target_rate,
                      is_compliant, is_stable))

                total_checked += 1
                if is_compliant:
                    total_compliant += 1

                if first_compliant_date:
                    logger.debug(f"[Compliance] {kw['keyword']}: 实时={detection_rate}%, "
                                 f"历史均值={effective_rate}% (自{first_compliant_date}起{hist_days+1}天), "
                                 f"达标={'✓' if is_compliant else '✗'}")

            conn2.commit()
            conn2.close()

        logger.info(
            f"[Compliance] 每日判定完成: {total_checked} 词条, {total_compliant} 达标, "
            f"{skipped_no_effective_start} 跳过(v1.4 paid_at NULL / quote 未付), "
            f"{skipped_outside_service_window} 跳过(v1.8 过期/未开始 · 不写当天 log), "
            f"{skipped_not_core} 跳过(非核心词 is_core=False), "
            f"{skipped_super_red_ocean} 跳过(超红海词 不计达标)"
        )
        return {
            "checked": total_checked,
            "compliant": total_compliant,
            "skipped_no_effective_start": skipped_no_effective_start,  # v1.4
            "skipped_outside_service_window": skipped_outside_service_window,  # v1.8
            "skipped_not_core": skipped_not_core,  # [审计 2026-06-07]
            "skipped_super_red_ocean": skipped_super_red_ocean,  # [审计 2026-06-07]
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def backfill_compliance_log_v3(
    days: int = 7,
    quote_id: Optional[int] = None,
    force_today: bool = True,
    only_paid_with_monitoring: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    [P0 事故 2026-05-23 老板拍 路径 B]
    用 v3 公平算法重算近 N 天 keyword_compliance_log
    修复 v2"倒反天罡"导致的 detection=100% 但 is_compliant=False 历史数据

    场景:
      - 客户铺量期 N 天 detection_rate=0% · 写入 log
      - 今天素材投放 detection_rate=100%
      - v2: effective_rate=(0×6+100)/7=14.3% < target=50 → is_compliant=False(错)
      - v3: effective_rate=max(100%, 14.3%)=100% → is_compliant=True(对)

    算法(r9 修后):
      1. 拉所有 paid/confirmed/active quote(或指定 quote_id)
      2. 对每个 quote 拉所有 keyword(confirmed + extra)
      3. 按时序(早→近)对近 N 天每天重算:
         - 拉当天 log row 的 detection_rate(已存真实值)
         - 算 rolling_avg(从前 N 天 DETECTION_RATE · 跟 v3 daily SQL 路径 line 3166-3172 对齐)
         - computed_effective = max(detection, rolling)
         - 单调硬约束:new_effective = max(old_effective, computed_effective)
         - is_compliant 跟 effective 同步 + 已达标永不变不达标
         - UPDATE log row · 仅当值真变化
      4. 重算后再跑一次 today 的 daily check 让 cron 状态对齐

    单调改善承诺(r9 修):
      - effective_rate 永不下降 (max(old, computed))
      - 已达标 row 永不变不达标(old_compliant=True → 强制保 True 即使 target 上调)
      - 跟 backfill_compliance_log_with_current_tier 不同 · 后者按当前 target 严格重判

    [r10 2026-05-23] 老板"回溯一直以来 已付费已开自动监测的客户":
      only_paid_with_monitoring=True(默认):用 get_monitoring_enabled_clients() 精确范围
        - status='paid' + monitoring_enabled=TRUE + service_end_date >= today
        - service_status NOT expired/paused + EXISTS articles(first_published_at)
      dry_run=True:返回会改的 row 数 + per-quote 明细 · 不真 UPDATE(老板预审用)
    """
    import logging as _logging
    _logger = _logging.getLogger("GEO-Compliance-Backfill")
    conn = get_connection()
    try:
        cur = conn.cursor()
        if quote_id is not None:
            cur.execute("SELECT id, tier FROM quotes WHERE id = %s", (quote_id,))
            quotes = [dict(r) for r in cur.fetchall()]
        elif only_paid_with_monitoring:
            # 精确范围:付费 + 自动监测开 + 服务期内 + 有发文
            from services.publication_stage_sources import published_occurrence_predicate

            _occ_backfill = published_occurrence_predicate("q.id")
            cur.execute(f"""
                SELECT q.id, q.tier
                FROM quotes q
                WHERE q.status = 'paid'
                  AND q.monitoring_enabled = TRUE
                  AND q.service_end_date IS NOT NULL
                  AND q.service_end_date >= CURRENT_DATE
                  AND COALESCE(q.service_status, 'active') NOT IN ('expired', 'paused')
                  -- [WP7] 与 get_monitoring_enabled_clients 用**同一个** occurrence 谓词。
                  -- 两处口径不同会造出"哪个名单都不在"的 quote,而且不报错。
                  AND ({_occ_backfill})
                ORDER BY q.id
            """)
            quotes = [dict(r) for r in cur.fetchall()]
            _logger.info(f"[Backfill-V3] 精确范围(paid+auto-monitor)拉到 {len(quotes)} quote")
        else:
            cur.execute("SELECT id, tier FROM quotes WHERE status IN ('confirmed','paid','active')")
            quotes = [dict(r) for r in cur.fetchall()]
            _logger.info(f"[Backfill-V3] 宽松范围(confirmed/paid/active)拉到 {len(quotes)} quote")

        total_keywords = 0
        total_log_rows = 0
        total_fixed = 0
        # [r10] dry_run 时的 per-quote 明细
        per_quote_detail = [] if dry_run else None

        for q in quotes:
            qid = q["id"]
            tier = q.get("tier") or "standard"
            target_rate = _TIER_TARGET_MAP.get(tier, 65)

            # 拉该 quote 所有 (keyword_id, keyword_source) pair
            # 注:INTERVAL '<param> days' 不能用 %s 参数化 · psycopg2 不解 string literal
            # days 已是 int 经端点 max(1,min(30,...)) 净化 · 安全 f-string 拼接
            _safe_days = int(days)
            cur.execute(f"""
                SELECT DISTINCT keyword_id, keyword_source
                FROM keyword_compliance_log
                WHERE quote_id = %s
                  AND check_date >= CURRENT_DATE - INTERVAL '{_safe_days} days'
            """, (qid,))
            kw_pairs = [(r["keyword_id"], r["keyword_source"]) for r in cur.fetchall()]
            total_keywords += len(kw_pairs)

            for kw_id, kw_source in kw_pairs:
                # 拉该 keyword 近 N 天所有 log row · 按日期升序
                cur.execute(f"""
                    SELECT id, check_date, detection_rate, effective_rate,
                           target_rate, is_compliant, is_stable
                    FROM keyword_compliance_log
                    WHERE keyword_id = %s AND keyword_source = %s AND quote_id = %s
                      AND check_date >= CURRENT_DATE - INTERVAL '{_safe_days} days'
                    ORDER BY check_date ASC
                """, (kw_id, kw_source, qid))
                rows = [dict(r) for r in cur.fetchall()]
                total_log_rows += len(rows)

                # 按时序回填:每天用前 N 天(已重算的)effective_rate avg + 当天 detection
                # 用 max(detection, rolling_avg) v3 公式
                # [self-review r8 2026-05-23] BUG 修:之前用 rows[idx-N:idx] 切片按行数取
                #   若 cron 漏跑导致 row 不连续 · 窗口可跨 > N 天 · 跟 v3 真 SQL 路径
                #   (line 3166-3170 按 check_date >= CURRENT_DATE - INTERVAL 'N days')语义不一致
                # 修法:按 check_date 过滤前 N 天 · 跟 v3 SQL 一致
                from datetime import timedelta as _td
                for idx, r in enumerate(rows):
                    det = float(r.get("detection_rate") or 0)
                    cur_date = r.get("check_date")
                    # 前 N 天 effective_rate avg(按 check_date 过滤 · 不按行数)
                    if cur_date is not None:
                        _cutoff = cur_date - _td(days=int(days))
                        hist_window = [
                            h for h in rows[:idx]
                            if h.get("check_date") is not None
                               and h["check_date"] > _cutoff
                               and h["check_date"] < cur_date
                        ]
                    else:
                        hist_window = rows[max(0, idx-int(days)):idx]
                    # [self-review r9 2026-05-23] Deploy-CTO 复审复现"75.5 → 71.4"BUG
                    # 根因 1:rolling 用 hist_avg_eff(链式)· 跟 daily v3 真算法用 hist_avg_det 不一致
                    # 根因 2:即使 hist_avg_det 也只能保证 v3 >= v2_pure · 但当前 row 的 v2 旧值
                    #         可能是不同 tier(target_rate)下算的 · 重算可能下降
                    # 修法:强制单调改善 · new_effective = max(old_effective, computed)
                    #      effective_rate 永不下降 · is_compliant 跟 effective 同步 · 永不让原达标变不达标
                    old_effective = float(r.get("effective_rate") or 0)
                    old_compliant = bool(r.get("is_compliant"))

                    if hist_window:
                        # rolling 用 detection_rate(跟 v3 真 SQL 路径 line 3166-3172 对齐)
                        # 不用 effective_rate 防止链式回填污染
                        hist_avg = sum(float(h.get("detection_rate") or 0) for h in hist_window) / len(hist_window)
                        rolling = (hist_avg * len(hist_window) + det) / (len(hist_window) + 1)
                    else:
                        rolling = det
                    computed_effective = max(det, rolling)
                    # 单调改善硬约束:effective 不下降
                    new_effective = round(max(old_effective, computed_effective), 1)
                    new_compliant = new_effective >= target_rate
                    # 单调改善硬约束:已达标永不变不达标(即使 target 上调)
                    if old_compliant and not new_compliant:
                        new_compliant = True

                    needs_update = (
                        abs(new_effective - old_effective) > 0.05
                        or new_compliant != old_compliant
                        or float(r.get("target_rate") or 0) != target_rate
                    )
                    if needs_update:
                        if not dry_run:
                            cur.execute("""
                                UPDATE keyword_compliance_log
                                   SET effective_rate = %s,
                                       target_rate = %s,
                                       is_compliant = %s
                                 WHERE id = %s
                            """, (new_effective, target_rate, new_compliant, r["id"]))
                            # 仅真改时更新本地 rows 副本让后续日期 rolling 用新值
                            r["effective_rate"] = new_effective
                            r["target_rate"] = target_rate
                            r["is_compliant"] = new_compliant
                        total_fixed += 1
                        if per_quote_detail is not None and len(per_quote_detail) < 200:
                            # dry_run 时收集前 200 个明细 · 老板预审用 · 防 response 过大
                            per_quote_detail.append({
                                "quote_id": qid,
                                "keyword_id": kw_id,
                                "check_date": str(r.get("check_date") or ""),
                                "old_effective": old_effective,
                                "new_effective": new_effective,
                                "old_compliant": old_compliant,
                                "new_compliant": new_compliant,
                                "target_rate": target_rate,
                            })

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        result = {
            "dry_run": dry_run,
            "quotes_scanned": len(quotes),
            "keywords_scanned": total_keywords,
            "log_rows_scanned": total_log_rows,
            "log_rows_fixed": total_fixed,
            "days": int(days),
            "quote_id_filter": quote_id,
            "scope": "paid+auto-monitor" if (only_paid_with_monitoring and quote_id is None) else ("single_quote" if quote_id else "wide"),
        }
        if per_quote_detail is not None:
            result["sample_changes"] = per_quote_detail
            result["sample_truncated"] = (len(per_quote_detail) == 200)
        _logger.info(f"[Backfill-V3] 完成 · {result}")

        # force_today: 顺手跑一次今天的 daily check(用 v3 算法 · 如果 flag 已开)
        # dry_run 时不跑(避免副作用)
        if force_today and not dry_run:
            try:
                today_result = run_daily_compliance_check()
                result["today_rerun"] = today_result
            except Exception as e:
                _logger.warning(f"[Backfill-V3] today rerun 失败(非阻塞): {e}")
                result["today_rerun_error"] = str(e)

        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def backfill_compliance_log_with_current_tier(quote_id: Optional[int] = None) -> dict:
    """
    [CTO-15.23 2026-05-12 BUG fix · 全面修复 P1]
    用当前 quote.tier 重算 keyword_compliance_log 所有历史 row 的 target_rate + is_compliant

    场景:
      - cron 历史跑时 quote.tier='premium'(target=75)· effective_rate=72 · is_compliant=False · log 写入
      - 之后老板把 tier 改回 'entry'(target=50)· 但 log 旧 row target_rate=75 / is_compliant=False
      - UI 显示 effective_rate=72(从 log)+ is_compliant=False · 跟当前 target=50 不一致
    修法:
      - 用当前 quote.tier 重算每个 log row 的 target_rate + is_compliant
      - 只 UPDATE 真正变化的 row(WHERE 子句过滤)· 减少写放大

    Args:
        quote_id: 仅刷指定 quote 的 log · None 刷全部(status=confirmed/paid/active)

    Returns:
        {processed, updated, quotes}
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        if quote_id is not None:
            cur.execute("SELECT id, tier FROM quotes WHERE id = %s", (quote_id,))
        else:
            cur.execute(
                "SELECT id, tier FROM quotes WHERE status IN ('confirmed','paid','active')"
            )
        quotes = cur.fetchall()

        total_processed = 0
        total_updated = 0

        for q in quotes:
            qid = q["id"]
            tier = q.get("tier") or "standard"
            current_target = _TIER_TARGET_MAP.get(tier, 65)

            # 重算该 quote 的所有 log row · 仅 UPDATE 真正变的
            cur.execute(
                """
                UPDATE keyword_compliance_log
                SET target_rate = %s,
                    is_compliant = (effective_rate >= %s)
                WHERE quote_id = %s
                  AND (target_rate != %s OR is_compliant IS DISTINCT FROM (effective_rate >= %s))
                """,
                (current_target, current_target, qid, current_target, current_target),
            )
            updated = cur.rowcount or 0
            total_updated += updated

            cur.execute(
                "SELECT COUNT(*) AS c FROM keyword_compliance_log WHERE quote_id = %s",
                (qid,),
            )
            total_processed += cur.fetchone()["c"]

        conn.commit()
        return {
            "processed": total_processed,
            "updated": total_updated,
            "quotes": len(quotes),
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_keyword_compliance_summary(quote_id: int) -> Dict[str, Any]:
    """
    获取合同下每个关键词的达标汇总。
    返回 {keyword_id: {compliant_days, service_days, remaining_compliant, remaining_days, is_today_compliant, ...}}

    [履约口径 2026-06-04 · A 方案纯履约]
      - 已服务 = compliant_days(达标天数)
      - 服务完成 = compliant_days >= service_days(不是自然日历到期)
      - 还需 = remaining_compliant = max(0, service_days − compliant_days)
      - 不设自然日历封顶:过自然日历窗口后仍累计达标天数(见上方 filtered CTE 去掉 effective_end 上界)
      - remaining_days 保留为 remaining_compliant 的别名(向后兼容旧前端)
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 统计每个关键词的达标天数 + 历史平滑信息
        # 修复：当今日未运行达标判定时，使用最近一次判定结果，避免误显示"未达标"
        # [CTO-15.23 2026-05-10] 加 is_stable 字段(老板拍板 A 方案)
        #   - is_today_stable: 今日记录的 is_stable 值
        #   - is_latest_stable: 最近一次记录的 is_stable 值(今日未判定时兜底)
        # [v1.4 2026-05-29 / 续费履约 2026-06-23]
        # 服务窗口只保留下界 effective_start:
        #   start = MAX(quote.paid_at, kms.enabled_at)
        #   - 防 KMS 启用前 / 未付期日志计入 compliant_days(v1.4)
        #   - 不设自然日历上界;过日历窗口后仍累计达标天数,直到 compliant_days 满 service_days。
        # quote 未付款 / 未进入服务锚 → kw_eff_window 为空 → 整行不计(fail-closed)
        cursor.execute(f"""
            WITH seed_quote AS (
                SELECT brand_id FROM quotes WHERE id = %s
            ),
            scoped_quotes AS (
                SELECT
                    q.id,
                    q.service_days AS service_days,
                    COALESCE(q.tier, 'standard') AS tier,
                    COALESCE(q.service_start_date, q.paid_at::date) AS service_start
                FROM quotes q
                JOIN seed_quote sq ON sq.brand_id = q.brand_id
                WHERE {quote_service_anchor_condition_sql("q")}
            ),
            kw_eff_window AS (
                -- 每个 (keyword_id, keyword_source) 算服务窗口 start; 续费场景按 brand 下所有服务锚 quote 取词
                SELECT
                    kcl.keyword_id,
                    kcl.keyword_source,
                    kcl.quote_id,
                    q.service_days,
                    q.tier,
                    GREATEST(
                        q.service_start,
                        COALESCE(
                            (SELECT MAX(s.enabled_at::date)
                             FROM keyword_monitor_subscriptions s
                             WHERE s.keyword_id = kcl.keyword_id
                               AND s.quote_id = kcl.quote_id
                               AND s.status IN ('active', 'paused_low_balance')),
                            q.service_start  -- 无 KMS sub → 用服务锚(允许 backfill)
                        )
                    ) AS effective_start
                FROM (SELECT DISTINCT keyword_id, keyword_source, quote_id
                      FROM keyword_compliance_log) kcl
                JOIN scoped_quotes q ON q.id = kcl.quote_id
            ),
            filtered AS (
                SELECT kcl.*, kew.service_days, kew.tier
                FROM keyword_compliance_log kcl
                JOIN kw_eff_window kew
                  ON kew.keyword_id = kcl.keyword_id
                 AND kew.keyword_source = kcl.keyword_source
                -- [Deploy-CTO 2026-05-30] 去掉 kcl.quote_id 过滤 · 按 keyword_id 取该词【全部 quote_id 的行】
                -- 根因:唯一键(keyword_id,keyword_source,check_date)不含 quote_id · daily 用外层 paid quote 写
                --   → 同词的行 quote_id 在同 brand 多 quote 间飘移(罗平2300: 5/30→280 / 5/27-28→268 / 5/19-26→282)
                --   旧 WHERE kcl.quote_id=本quote 只取本 quote 名下 → 漏 5/27-30 达标行 → effective 卡 5/26=75
                --   kw_eff_window 已用本 quote(入参)限定词集 + 本 quote 服务锚算窗口 · 这里按 keyword_id 取全部行
                --   keyword_id 全局唯一(confirmed_keywords.id)· 一词只属一 quote · 不会混入别客户的词
                WHERE kcl.check_date >= kew.effective_start
                  -- [履约口径 2026-06-04] A 方案纯履约:不设自然日历封顶
                  --   服务完成 = compliant_days >= service_days(达标天数),而非自然日历到期。
                  --   故去掉自然日历上界 ·
                  --   过了自然日历窗口仍继续累计达标天数,直到 compliant_days 满 service_days。
                  --   下界 effective_start 保留(不计服务/KMS 启用前的脏行)。
                  --   ⚠️ 这只改"完成判定/累计达标天数",不动出现率算法(_compute_effective_rate_v3)。
            )
            SELECT
                keyword_id,
                keyword_source,
                service_days,
                tier,
                COUNT(*) FILTER (WHERE is_compliant = TRUE) as compliant_days,
                BOOL_OR(check_date = CURRENT_DATE) as has_today_check,
                BOOL_OR(check_date = CURRENT_DATE AND is_compliant = TRUE) as is_today_compliant,
                BOOL_OR(check_date = CURRENT_DATE AND is_stable = TRUE) as is_today_stable,
                -- 最近一次达标判定结果（兜底：今日未判定时使用）
                (SELECT is_compliant FROM filtered k3
                 WHERE k3.keyword_id = filtered.keyword_id AND k3.keyword_source = filtered.keyword_source
                 ORDER BY k3.check_date DESC LIMIT 1) as is_latest_compliant,
                (SELECT is_stable FROM filtered k5
                 WHERE k5.keyword_id = filtered.keyword_id AND k5.keyword_source = filtered.keyword_source
                 ORDER BY k5.check_date DESC LIMIT 1) as is_latest_stable,
                MIN(check_date) FILTER (WHERE is_compliant = TRUE) as first_compliant_date,
                -- 今日的 effective_rate（历史平滑后的出现率），无则取最近一次
                COALESCE(
                    (SELECT effective_rate FROM filtered k2
                     WHERE k2.keyword_id = filtered.keyword_id AND k2.keyword_source = filtered.keyword_source
                       AND k2.check_date = CURRENT_DATE
                     LIMIT 1),
                    (SELECT effective_rate FROM filtered k4
                     WHERE k4.keyword_id = filtered.keyword_id AND k4.keyword_source = filtered.keyword_source
                     ORDER BY k4.check_date DESC LIMIT 1)
                ) as today_effective_rate,
                -- [Deploy-CTO 2026-05-30] is_stable 读时动态:最近 7 个已记录监测日中达标天数
                -- 不信 keyword_compliance_log.is_stable 冻结列(confirmed quote 不再跑日常检查 → 旧 is_stable 卡死显"刚达标")
                (SELECT COUNT(*) FILTER (WHERE r7.is_compliant)
                 FROM (SELECT is_compliant FROM filtered kk
                       WHERE kk.keyword_id = filtered.keyword_id AND kk.keyword_source = filtered.keyword_source
                       ORDER BY kk.check_date DESC LIMIT 7) r7) as recent7_compliant
            FROM filtered
            GROUP BY keyword_id, keyword_source, service_days, tier
        """, (quote_id,))

        rows = cursor.fetchall()
        conn.close()

        result = {}
        for row in rows:
            r = dict(row)
            kw_id = r["keyword_id"]
            compliant_days = r["compliant_days"] or 0
            # [服务期 SSOT 2026-08-06 §1.2] `or 365` 拔掉。service_days 是履约达标天数配额,
            #   migration_028 已置 NOT NULL → 这里正常永远拿得到值。真拿不到就返 None,
            #   让前端显"配额未设"并给出口,而不是糊成 365(那正是假钟的来源)。
            service_days = compliance_target_days(r)
            tier = r.get("tier") or "standard"
            target_rate = _TIER_TARGET_MAP.get(tier, 65)
            # [履约口径 2026-06-04] A 方案:还需 = service_days − compliant_days(还需达标天数)
            remaining = max(0, service_days - compliant_days) if service_days else None
            progress = round(compliant_days / service_days * 100, 1) if service_days else 0

            # 修复：今日未运行达标判定时，使用最近一次判定结果
            has_today = bool(r.get("has_today_check"))
            if has_today:
                compliant_status = bool(r.get("is_today_compliant"))
            else:
                compliant_status = bool(r.get("is_latest_compliant"))
            # [Deploy-CTO 2026-05-30] is_stable 改读时动态重算:最近 7 个已记录监测日 ≥5 天达标 → 稳定达标。
            # 不信冻结的 is_stable 列(confirmed quote 不再跑日常检查会卡死旧值 · 老板报"刚达标"标签不动态)
            stable_status = (r.get("recent7_compliant") or 0) >= 5

            result[kw_id] = {
                "keyword_id": kw_id,
                "keyword_source": r["keyword_source"],
                "target_rate": target_rate,
                "compliant_days": compliant_days,
                "service_days": service_days,
                "remaining_days": remaining,  # 向后兼容旧字段名(=remaining_compliant)
                # [履约口径 2026-06-04] A 方案统一字段名 · 前端按此读
                #   compliant_days = 已服务(达标天数)· service_days = 服务总天数
                #   remaining_compliant = 还需达标天数 = max(0, service_days − compliant_days)
                "remaining_compliant": remaining,
                "is_today_compliant": compliant_status,
                "is_stable": stable_status,
                "compliance_progress": progress,
                "first_compliant_date": str(r["first_compliant_date"]) if r.get("first_compliant_date") else None,
                "effective_rate": float(r["today_effective_rate"]) if r.get("today_effective_rate") is not None else None,
            }

        return result
    finally:
        try:
            conn.close()
        except Exception: pass


# 初始化
if __name__ == "__main__":
    init_monitoring_tables()
    print("监测数据库初始化完成")


# ================================================================
# 客户级监测配置（quotes 表新字段）
# ================================================================

# 2026-04-17 P0-O: 重命名为 _quote 后缀版本
# 原名 get_monitoring_config / update_monitoring_config 与 L431/L468 重名
# Python 后定义覆盖 → api/monitoring_api.py 调旧版 brand_id 签名报
# TypeError: got an unexpected keyword argument 'brand_id'
# → 代理端排名监测页 500 全断
def get_quote_monitoring_config(quote_id: int) -> dict:
    """获取报价单的监测配置"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT monitoring_enabled, monitoring_frequency, monitoring_start_hour,
                   monitoring_interval_hours, monitoring_last_run_at,
                   monitoring_started_at, monitoring_paused_reason
            FROM quotes WHERE id = %s
        """, (quote_id,))
        row = cur.fetchone()
        conn.close()
        data = dict(row) if row else {}
        if data and not data.get("monitoring_interval_hours"):
            freq = data.get("monitoring_frequency") or 1
            data["monitoring_interval_hours"] = {1: 24, 2: 12, 3: 8}.get(freq, 24)
        return data
    finally:
        try:
            conn.close()
        except Exception: pass


def _legacy_frequency_from_interval(interval_hours: int) -> int:
    """保留老 monitoring_frequency 字段:精确老档位映射,其余仅作兼容近似。"""
    if interval_hours == 8:
        return 3
    if interval_hours == 12:
        return 2
    if interval_hours < 24:
        return max(1, round(24 / interval_hours))
    return 1


def update_quote_monitoring_config(
    quote_id: int,
    enabled: bool,
    frequency: int = 1,
    start_hour: int = 8,
    interval_hours: Optional[int] = None,
):
    """更新报价单的监测配置"""
    if interval_hours is None:
        interval_hours = {1: 24, 2: 12, 3: 8}.get(frequency, 24)
    interval_hours = max(1, min(int(interval_hours), 720))
    legacy_frequency = _legacy_frequency_from_interval(interval_hours)

    conn = get_connection()
    try:
        cur = conn.cursor()
        if enabled:
            cur.execute("""
                UPDATE quotes
                SET monitoring_enabled = TRUE,
                    monitoring_frequency = %s,
                    monitoring_interval_hours = %s,
                    monitoring_start_hour = %s,
                    monitoring_started_at = COALESCE(monitoring_started_at, CURRENT_TIMESTAMP),
                    monitoring_paused_reason = NULL
                WHERE id = %s
            """, (legacy_frequency, interval_hours, start_hour, quote_id))
        else:
            cur.execute("""
                UPDATE quotes
                SET monitoring_enabled = FALSE,
                    monitoring_paused_reason = '手动关闭'
                WHERE id = %s
            """, (quote_id,))
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def pause_monitoring(quote_id: int, reason: str):
    """暂停监测（余额不足时自动调用）"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE quotes
            SET monitoring_enabled = FALSE,
                monitoring_paused_reason = %s
            WHERE id = %s AND monitoring_enabled = TRUE
            RETURNING id,brand_id
        """, (reason, quote_id))
        changed = cur.fetchone()
        if changed and changed.get("brand_id") is not None:
            cur.execute("SELECT owner_user_id FROM brands WHERE id=%s", (changed["brand_id"],))
            owner = cur.fetchone()
            if owner and owner.get("owner_user_id") is not None:
                from services.notification_events import NotificationEventType, RecipientKind
                from services.notification_outbox import enqueue_notification_event

                enqueue_notification_event(
                    cur,
                    event_type=NotificationEventType.MONITORING_PAUSED,
                    business_id=f"quote:{int(quote_id)}",
                    terminal_state="paused",
                    recipient_user_id=int(owner["owner_user_id"]),
                    recipient_kind=RecipientKind.USER,
                    facts={
                        "business_no": f"QUOTE-{int(quote_id)}",
                        "status": "效果监测已暂停",
                        "occurred_at": datetime.now().isoformat(timespec="seconds"),
                        "summary": "请在效果监测页面查看原因和恢复方式。",
                    },
                )
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def get_monitoring_enabled_clients() -> list:
    """获取仍有短句归旧报价级调度负责的监测客户。

    所有权按短句而不是整张报价划分。曾建立逐词订阅的短句永久由该订阅状态机
    管理：active 履约，paused/cancelled 都不回落；同报价中从未建立逐词订阅的
    历史短句仍按原 6/12/24 小时配置履约。

    [CTO-15.23 2026-05-05 P0 fix] 加 "已发文" 守卫:
    老板反馈 · 用户没发文但被自动监测扣费(每词 38 积分/次)
    · monitoring_enabled = TRUE 可能来自历史脏数据 / admin 直改 / 前端 update_monitoring_config
      调用错位 (server.py:5810 调错函数,本次一并修)
    · 即使 monitoring_enabled = TRUE,未发文 → 监测的"AI 排名变化"无意义 → 不该扣费
    · 加 EXISTS articles.first_published_at IS NOT NULL 铁底兜
    """
    # [WP7 cutover 2026-08-17] "已发文"守卫改走统一的 occurrence 谓词。
    # 旧写法只认 `articles.first_published_at`,而那一列只有人工登记链回写 ——
    # 只走代发/插件发布的客户会被判成"从没发过文",监测**静默不跑**。
    from services.publication_stage_sources import published_occurrence_predicate

    _occurrence = published_occurrence_predicate("q.id")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT q.id as quote_id, q.brand_id, q.brand_name,
                   q.monitoring_frequency, q.monitoring_start_hour,
                   COALESCE(q.monitoring_interval_hours, CASE q.monitoring_frequency
                       WHEN 1 THEN 24
                       WHEN 2 THEN 12
                       WHEN 3 THEN 8
                       ELSE 24
                   END) AS monitoring_interval_hours,
                   q.monitoring_last_run_at,
                   q.service_start_date, q.service_end_date,
                   (SELECT COUNT(*) FROM confirmed_keywords ck
                    WHERE ck.quote_id = q.id
                      AND (ck.is_core IS NOT FALSE)
                      AND COALESCE(ck.super_red_ocean, FALSE) = FALSE
                      AND NOT EXISTS (
                          SELECT 1 FROM keyword_monitor_subscriptions kms
                          WHERE kms.keyword_id = ck.id
                      )) as kw_count
            FROM quotes q
            WHERE q.status = 'paid'
              AND q.monitoring_enabled = TRUE
              AND q.service_end_date IS NOT NULL
              AND q.service_end_date >= CURRENT_DATE
              AND COALESCE(q.service_status, 'active') NOT IN ('expired', 'paused')
              AND EXISTS (
                  SELECT 1 FROM confirmed_keywords ck
                  WHERE ck.quote_id = q.id
                    AND (ck.is_core IS NOT FALSE)
                    AND COALESCE(ck.super_red_ocean, FALSE) = FALSE
                    AND NOT EXISTS (
                        SELECT 1 FROM keyword_monitor_subscriptions kms
                        WHERE kms.keyword_id = ck.id
                    )
              )
              AND ({_occurrence})
            ORDER BY q.id
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    finally:
        try:
            conn.close()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# [服务期 SSOT 2026-08-06 §1.5] 到期不静默
# ─────────────────────────────────────────────────────────────────────────────
def get_service_period_blocked_clients():
    """服务期已到、却还被当成在服务的付费客户 —— 内部必须看得见的那一批。

    工单 §1.5:服务期到点摘除资格时,必须写一条**对内可见**的状态
    (经营后台/工作台标"服务期已到 · 待续费"),不通知客户端(静默纪律),
    但代理/内部必须看得见 —— "静默停服务"这一类不再发生。

    判据(2026-08-06 生产实测后定稿):
      付费 ✓ · 自动监测开关还开着 ✓ · 有付费核心词 ✓ · 有已发布文章 ✓ ·
      service_status 非 expired/paused ✓ · 服务期 ✗(为空 或 早于今天)

    🔴 为什么**不**照抄 `get_monitoring_enabled_clients()` 的全部条件:
       那个闸还有一条 `NOT EXISTS keyword_monitor_subscriptions`(意思是"核心词还没建监测
       订阅"),它是**入池条件**不是**服务条件**。生产实测:#94 与 #286 两张付费+自动监测开
       的单子,`core_kw_without_sub` 都是 0 —— 也就是说照抄全部条件的话这个函数恒返 0 行,
       一个恒空的告警等于没做。按"服务期"这个维度取,#286(晨光富士)会被正确点名。

    🔴 同一次取证还推翻了工单 §0 对晨光富士的因果判断,见交付说明 §影响面:
       #286 的监测停在 2026-07-01,直接原因是它 5 个核心词全部 `is_monitored=false /
       monitoring_status='archived'`(KMS 订阅仍是 active、最后扣费日 07-01 与最后一条
       履约日志同日),**不是**服务期闸把它摘掉的。两钟分裂真实存在、危害也真实
       (门户 token 续期 / 续费桶 / 倒计时全被假钟带偏),但"静默停轮换"这一条
       在 #286 身上不成立。

    返回每行带 `blocked_reason` / `overdue_days`,给内部面板直接显示,不用再各自推算。
    """
    from services.publication_stage_sources import published_occurrence_predicate

    _occ_blocked = published_occurrence_predicate("q.id")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT q.id as quote_id, q.brand_id, q.brand_name,
                   q.service_start_date, q.service_end_date, q.service_status,
                   q.paid_at,
                   CASE WHEN q.service_end_date IS NULL THEN 'service_period_not_set'
                        ELSE 'service_period_ended' END AS blocked_reason,
                   CASE WHEN q.service_end_date IS NULL THEN NULL
                        ELSE (CURRENT_DATE - q.service_end_date) END AS overdue_days
            FROM quotes q
            WHERE q.status = 'paid'
              AND q.monitoring_enabled = TRUE
              AND COALESCE(q.service_status, 'active') NOT IN ('expired', 'paused')
              -- 服务期这一条取反
              AND (q.service_end_date IS NULL OR q.service_end_date < CURRENT_DATE)
              AND EXISTS (
                  SELECT 1 FROM confirmed_keywords ck
                  WHERE ck.quote_id = q.id
                    AND (ck.is_core IS NOT FALSE)
                    AND COALESCE(ck.super_red_ocean, FALSE) = FALSE
              )
              -- [WP7] 同上:阻塞名单必须与在跑名单用同一个 occurrence 谓词。
              AND ({_occ_blocked})
            ORDER BY q.service_end_date NULLS FIRST, q.id
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    finally:
        try:
            conn.close()
        except Exception: pass


def mark_quote_monitoring_last_run(quote_id: int):
    """记录 quote 最近一次定时监测成功时间。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        # quotes.monitoring_last_run_at 是 TIMESTAMP WITHOUT TIME ZONE；
        # 写入北京时间 naive 值,与 scheduler 的 BEIJING_TZ localize 逻辑保持一致。
        beijing_now = datetime.utcnow() + timedelta(hours=8)
        cur.execute(
            "UPDATE quotes SET monitoring_last_run_at = %s WHERE id = %s",
            (beijing_now, quote_id),
        )
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def claim_quote_for_monitoring_with_token(
    quote_id: int,
    interval_hours: int = 24,
) -> Optional[Dict[str, Any]]:
    """
    Atomic claim quote 监测扣费 slot · 防蓝绿+多 scheduler race 重复 freeze_points

    [CTO-15.23 2026-05-19 P0 补漏 · 跟 claim_subscription_for_today 同款模式]
    api/scheduler.py _hourly_check + _run_clients_with_billing 用
    feature_code='scheduled_monitoring' freeze_points → 同样有蓝绿 race 重复扣风险
    monitoring_keyword_daily 那条 path 已加 claim_subscription_for_today
    这条 path 用 quotes.monitoring_last_run_at + Python 算 cutoff atomic UPDATE

    用 Python 算 cutoff timestamp(不依赖 DB INTERVAL 字符串拼接 · 防 SQL injection):
    - cutoff = 北京 now - max(1, interval_hours - 1) hours (grace 1h 防 interval=24 race 卡死)
    - WHERE monitoring_last_run_at IS NULL OR monitoring_last_run_at < cutoff
    - 多 instance 同时 UPDATE · 只 1 个 RETURNING 拿到 row · 其他 RETURNING 空 = 跳过

    Returns a CAS token plus the previous successful timestamp. Callers may
    restore the previous value only when the run produced no result and no
    charge. A late worker cannot clear a newer successful run.
    """
    grace_hours = max(1, interval_hours - 1)
    beijing_now = datetime.utcnow() + timedelta(hours=8)
    cutoff = beijing_now - timedelta(hours=grace_hours)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT monitoring_last_run_at FROM quotes WHERE id = %s FOR UPDATE",
            (quote_id,),
        )
        previous_row = cur.fetchone()
        if not previous_row:
            conn.rollback()
            return None
        previous_value = previous_row.get("monitoring_last_run_at")
        cur.execute(
            """
            UPDATE quotes
            SET monitoring_last_run_at = %s
            WHERE id = %s
              AND (monitoring_last_run_at IS NULL OR monitoring_last_run_at < %s)
            RETURNING id, monitoring_last_run_at
            """,
            (beijing_now, quote_id, cutoff),
        )
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return None
        return {
            "claim_token": row.get("monitoring_last_run_at"),
            "previous_value": previous_value,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def claim_quote_for_monitoring(quote_id: int, interval_hours: int = 24) -> bool:
    """Compatibility boolean wrapper for older explicit/manual callers."""
    return claim_quote_for_monitoring_with_token(quote_id, interval_hours) is not None


def release_quote_monitoring_claim(
    quote_id: int,
    claim_token: object,
    previous_value: object,
) -> bool:
    """Restore a failed zero-result quote claim only if its token still owns it."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE quotes
            SET monitoring_last_run_at = %s
            WHERE id = %s
              AND monitoring_last_run_at IS NOT DISTINCT FROM %s
            """,
            (previous_value, quote_id, claim_token),
        )
        affected = cur.rowcount
        conn.commit()
        return affected == 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ================================================================
# 关键词监测订阅 helpers (CTO-15.23 2026-05-09)
# 老板拍板设计:消费一次扣一次(charge_on_success A 类) · 不用月 freeze
# enable 不扣 · disable 不退 · daily 03:00 跑完每个 active keyword 即扣 130
# ================================================================

def create_keyword_monitor_subscription(
    user_id: int,
    keyword_id: int,
    quote_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    daily_points: int = 130,
    feature_code: str = 'monitoring_keyword_daily',
    keyword_source: str = 'confirmed',
    billing_mode: str = 'brand_owner',
) -> int:
    """创建监测订阅 · 幂等 · 返回 subscription_id

    [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-3] keyword_source 透传给实现体
    ('confirmed' 默认 → 全部现有调用方行为逐字不变;'extra' 是手动词)。

    [v2026-05-29 P0 hotfix] 幂等改造(老板拍 · KMS 重复创建是 prod BUG):
      1. 先 SELECT 已有 active/paused_low_balance sub · 有则直接返(不再 INSERT)
      2. 没有才 INSERT 新 sub
      3. 同一 conn 串行 · 配合 uniq_kms_keyword_active unique index 防并发重复

    [v1.7 2026-05-29 老板复审 P2 · 并发 unique 冲突兜底]
      问题:先 SELECT 再 INSERT 仍有并发窗口 · 第二个请求 INSERT 撞 unique index → 500
      修法:catch psycopg2.errors.UniqueViolation → 回滚 + re-SELECT 已存在 active sub → 返其 id
      兼容性:UniqueViolation 不可达时(unique index 未部署)旧 SELECT-then-INSERT 仍工作

    旧行为:直接 INSERT → 重复扣费 P0 BUG(5/11→19 brand 250/63/17/19/289/10/316 等)
    新行为:幂等 · 调用方仍要 update_keyword_monitor_state(ck.is_monitored=TRUE · sub_id 回填)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        sub_id = create_keyword_monitor_subscription_with_cursor(
            cur,
            user_id=user_id,
            keyword_id=keyword_id,
            quote_id=quote_id,
            brand_id=brand_id,
            daily_points=daily_points,
            feature_code=feature_code,
            keyword_source=keyword_source,
            billing_mode=billing_mode,
        )
        conn.commit()
        return sub_id
    finally:
        try:
            conn.close()
        except Exception:
            pass


def create_keyword_monitor_subscription_with_cursor(
    cur,
    *,
    user_id: int,
    keyword_id: int,
    quote_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    daily_points: int = 130,
    feature_code: str = 'monitoring_keyword_daily',
    keyword_source: str = 'confirmed',
    billing_mode: str = 'brand_owner',
) -> int:
    """同上,但用**调用方的 cursor**,让"业务写入 + 建订阅"能落在同一个事务里。

    [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-1/P0-3] 加 keyword_source 维度。
    🔴 默认 'confirmed' —— 全部现有调用方逐字不用改,行为逐字不变。
    🔴 幂等查重 / 并发兜底 re-SELECT **都必须带上 keyword_source**:
      只按 keyword_id 查会让"手动词 id == 某个合同词 id"时互相认成对方的订阅,
      结果是开手动词开关拿到合同词的订阅 id、或反过来把对方的订阅当成自己的复用掉。
      (今天 extra 2–24 / confirmed 526–3074 零重叠是侥幸,两表独立自增。)

    [WO_ORPHAN_MONITOR_FIX 2026-08-10 ①] C 端确认报价必须"标 is_monitored 与建订阅"同生共死,
    否则就是本工单要消灭的那种孤儿(UI 开着、cron 永不跑)。自开连接的老函数做不到这件事,
    所以把实现挪到这里,老函数改成"开连接 + 调它 + commit"的薄壳 —— **不是重写,是同一份实现**。

    🔴 本函数**不 commit、不 rollback 整个事务**:提交与回滚都归调用方。
    🔴 并发 unique 冲突的兜底改用 **SAVEPOINT**:老版在自己的连接里 `conn.rollback()`,
      放进别人的事务里那一下会把调用方**整笔业务写入一起回滚掉**
      (本仓有过"try/except 包 SQL 无 SAVEPOINT = 打废调用方事务"的前科)。
    """

    # [WO_MONITORING_PLATFORM_COVERED 2026-08-16 §2] 付款方维度。
    # 🔴 默认 'brand_owner' ⇒ 全部现有调用方逐字不用改、行为逐字不变(加法不是减法)。
    # 取值在这里就校验:与迁移里的 CHECK 同口径,非法值当场炸出调用方,
    # 而不是写到库门口再被 CHECK 拒(那时错误信息里已经没有"谁传的"了)。
    _billing_mode = (billing_mode or 'brand_owner').strip()
    if _billing_mode not in ('brand_owner', 'platform'):
        raise ValueError(f"不支持的 billing_mode: {billing_mode!r}(只允许 brand_owner / platform)")
    _source = str(keyword_source or 'confirmed').strip().lower()
    if _source not in ('confirmed', 'extra'):
        # 响亮失败:静默回落 'confirmed' 会把手动词订阅写成合同词订阅,
        # 然后被合同臂取数取走、套上 quote 服务锚 —— 正是本单要消灭的串词形态。
        raise ValueError(f"unsupported keyword_source: {keyword_source!r}")

    # 1. 查已有 active/paused_low_balance sub(幂等)· 必须同来源
    cur.execute("""
        SELECT id FROM keyword_monitor_subscriptions
        WHERE keyword_id = %s
          AND keyword_source = %s
          AND status IN ('active', 'paused_low_balance')
        ORDER BY id DESC LIMIT 1
    """, (keyword_id, _source))
    row = cur.fetchone()
    if row:
        return row['id']

    cur.execute("SAVEPOINT kms_create")
    try:
        cur.execute("""
            INSERT INTO keyword_monitor_subscriptions
                (user_id, keyword_id, keyword_source, quote_id, brand_id, status,
                 daily_points, feature_code, billing_mode, enabled_at)
            VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, CURRENT_TIMESTAMP)
            RETURNING id
        """, (user_id, keyword_id, _source, quote_id, brand_id, daily_points,
              feature_code, _billing_mode))
        row = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT kms_create")
        return row['id'] if row else None
    except Exception as _insert_err:
        _err_name = type(_insert_err).__name__
        _is_unique = (
            _err_name in ("UniqueViolation", "IntegrityError")
            or "uniq" in str(_insert_err).lower()
            or "duplicate" in str(_insert_err).lower()
        )
        cur.execute("ROLLBACK TO SAVEPOINT kms_create")
        if not _is_unique:
            raise
        cur.execute("""
            SELECT id FROM keyword_monitor_subscriptions
            WHERE keyword_id = %s
              AND keyword_source = %s
              AND status IN ('active', 'paused_low_balance')
            ORDER BY id DESC LIMIT 1
        """, (keyword_id, _source))
        row2 = cur.fetchone()
        if row2:
            return row2['id']
        raise


def cancel_keyword_monitor_subscription(subscription_id: int) -> bool:
    """关闭订阅 · status='cancelled' · 不退分(已扣的不退)"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE keyword_monitor_subscriptions
            SET status = 'cancelled',
                cancelled_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND status IN ('active', 'paused_low_balance')
        """, (subscription_id,))
        affected = cur.rowcount
        conn.commit()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cancel_keyword_monitor_subscriptions_for_keyword_with_cursor(
    cur, keyword_id: int, keyword_source: str = 'confirmed'
) -> int:
    """把某个词**所有**在跑的订阅置 cancelled,用调用方的 cursor,返回受影响行数。

    [WO_ORPHAN_MONITOR_FIX 2026-08-10 ③] 归档词联动用。
    病史:归档只改 confirmed_keywords(monitoring_status='archived' / is_monitored=FALSE),
    订阅表那行**留在 active** —— 产出"UI 关着、订阅表僵尸 active"的镜像态
    (实例:富士 ck2339。它不扣费,因为 list_active_subscriptions 还 JOIN 了 is_monitored,
    但数据是脏的,任何按订阅表统计的口径都会把它算成在跑)。

    🔴 按 keyword_id 取消**全部**在跑行,不是只取一条:并发/历史脏数据下同一个词
      可能有多行 active(unique index 是后加的),只 cancel 一条等于没清干净。
    🔴 不 commit:提交与回滚归调用方(与归档 UPDATE 同事务,要么都改要么都不改)。

    [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-3] 加 keyword_source(默认 'confirmed',
    现有调用方行为逐字不变)。🔴 不带来源地按 keyword_id 取消,会在两表 id 撞车时
    **把另一个客户的合同词订阅一起 cancel 掉** —— 归档一个手动词顺手停掉别人的合同监测。
    """
    cur.execute("""
        UPDATE keyword_monitor_subscriptions
           SET status = 'cancelled',
               cancelled_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
         WHERE keyword_id = %s
           AND keyword_source = %s
           AND status IN ('active', 'paused_low_balance')
    """, (keyword_id, str(keyword_source or 'confirmed').strip().lower()))
    return cur.rowcount


def pause_keyword_monitor_subscription(subscription_id: int, reason: str = 'insufficient_balance') -> bool:
    """暂停订阅(余额不足等) · 不取消 · 充值后可恢复"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE keyword_monitor_subscriptions
            SET status = 'paused_low_balance',
                paused_reason = %s,
                paused_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'active'
        """, (reason, subscription_id))
        affected = cur.rowcount
        conn.commit()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def resume_keyword_monitor_subscription(subscription_id: int) -> bool:
    """恢复订阅(02:30 cron 余额足够时调用)"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE keyword_monitor_subscriptions
            SET status = 'active',
                paused_reason = NULL,
                paused_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'paused_low_balance'
        """, (subscription_id,))
        affected = cur.rowcount
        conn.commit()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_subscription_by_keyword(
    keyword_id: int, keyword_source: str = 'confirmed'
) -> Optional[Dict[str, Any]]:
    """按 (keyword_id, keyword_source) 取最新非 cancelled 订阅 · enable 查重 + daily 跑前过滤

    [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-3] 加来源维度。
    🔴 只按 keyword_id 查会在两表 id 撞车时把**另一种词**的订阅认成自己的
      (开手动词开关 → 复用到合同词订阅 → 手动词永远没有自己的订阅、永远不跑)。
    🔴 默认 'confirmed':全部现有调用方行为逐字不变。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, user_id, keyword_id, keyword_source, quote_id, brand_id, status,
                   daily_points, feature_code, enabled_at, last_charged_at,
                   total_charged, paused_reason
            FROM keyword_monitor_subscriptions
            WHERE keyword_id = %s
              AND keyword_source = %s
              AND status IN ('active', 'paused_low_balance')
            ORDER BY id DESC
            LIMIT 1
        """, (keyword_id, str(keyword_source or 'confirmed').strip().lower()))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_active_subscriptions() -> List[Dict[str, Any]]:
    """列出所有 active 订阅 · daily 03:00 跑前用

    [Deploy-CTO 2026-05-26 P0 根因 · 自动监测 detected=0/176]
    旧版 SELECT 没拿 brand_name → scheduler.py:253 target_brand="" 永远空
      → LLM 检测 比对 target_brand="" 永远 False → 自动监测 100% is_detected=0
    手动监测走 SSE endpoint 拿 brand_name OK · 所以手动 93.8% / 自动 0% 巨大差异
    实证:7 天 176 行自动 task 全 is_detected=0 · 即使 LLM 回复明确提到 brand 名
    修法:JOIN quotes 拿 brand_name 字段

    [v1.2 2026-05-29 paid-only scheduler 守护]
    根因:brand 6/428 类 confirmed 未付 KMS 仍被 scheduler 跑 → 重复扣费
    旧 LEFT JOIN quotes 不过滤 status · daily job 跑所有 active sub(不管付款状态)
    修法:LEFT JOIN → INNER JOIN + q.status='paid' filter
      · confirmed 未付 / draft / cancelled 全部不进 daily 跑监测
      · KMS 仍 active 但 quote 撤销 paid 也会被自动剔除(下一次 scheduler run 起效)

    [履约口径 2026-06-04 · A 方案纯履约 · 取代 v1.7 自然日历过期过滤]
    老板拍板 A:服务完成 = compliant_days >= service_days(达标天数),不是自然日历到期。
    旧 v1.7:WHERE CURRENT_DATE < contract_start + service_days(自然日历到期就停)
      → 问题:还没达标满 service_days 但自然日历到了就停 = 没履约完就停服务(违 A)
    新:履约未满才 active = compliant_days < service_days
      - 达标满 service_days → 不再 active(服务完成要停)
      - 不设自然日历封顶:过了自然日历但还没达标满 → 继续监测直到达标满
      - 代理手动关自动监测(归档/暂停)→ is_monitored/monitoring_status 任一不满足 → 不 active
      - compliant_days 统计:check_date >= 服务锚(下界防脏行) · 无上界(纯履约)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT s.id, s.user_id, s.keyword_id, s.quote_id, s.brand_id,
                   s.daily_points, s.feature_code, s.last_charged_at,
                   s.billing_mode,
                   'confirmed'::text AS keyword_source,
                   ck.keyword, ck.monitoring_query,
                   -- 🔴 刻意不 SELECT ck.monitoring_product_version:现役这里就没取,
                   --   scheduler 的 sub.get("monitoring_product_version") 因此恒 None、
                   --   entitlement 快照回落 DEFAULT_MONITORING_PRODUCT_VERSION。
                   --   补上它会改变合同臂**已落库的审计快照内容**,那是本单范围外的行为变更。
                   --   已作为独立发现上报,不在本包顺手改。
                   m.platforms AS entitlement_platforms,
                   q.brand_name
            FROM keyword_monitor_subscriptions s
            JOIN confirmed_keywords ck ON ck.id = s.keyword_id
            JOIN public.monitoring_product_platform_matrices m
              ON m.version = ck.monitoring_product_version
            JOIN quotes q ON q.id = s.quote_id
            WHERE s.status = 'active'
              -- [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-1/P0-2] 合同臂只认 confirmed 订阅。
              --   没有这一条,手动词的订阅会 JOIN 到 id 相同的**另一个客户的合同词**上
              --   去跑、去扣(今天 extra 2–24 / confirmed 526–3074 零重叠是侥幸,
              --   两张表各自独立自增,手动词再加约 500 个就进入合同词区间)。
              AND s.keyword_source = 'confirmed'
              -- [履约口径 2026-06-04] 自动监测 toggle 开 = is_monitored + monitoring_status='active'
              --   代理手动关自动监测(归档/暂停)→ 这两条任一不满足 → 不 active(已停)
              AND ck.is_monitored = TRUE
              AND COALESCE(ck.monitoring_status, 'active') = 'active'
              -- [audit P1 2026-06-10 红线批·老板已批] v1.2 paid-only 与玩法B服务锚 SSOT 矛盾修复:
              --   confirmed 真客户(代理线下收款·paid_at 可 NULL 但 service_start_date 有值)的 active 订阅
              --   自 v1.2(2026-05-29)起被 q.status='paid' 滤死 → 僵尸态(toggle 显示开着·永不跑·永不扣·无通知),
              --   compliance 7 天窗排空 → 出现率/达标被不公平打 0(prod 实证 quote 282/287/109 冻在 2026-05-28)。
              --   对齐服务锚 SSOT(本文件 resolve_service_anchored_quote_ids_for_brand ~:1630 /
              --   run_daily_compliance_check ~:3663 同一口径):
              AND {quote_service_anchor_condition_sql("q")}
              -- [履约口径 2026-06-04] A 方案纯履约 · 取代 v1.7 自然日历过期过滤
              --   旧:CURRENT_DATE < contract_start + service_days(自然日历到期就停)
              --   新:履约未满才 active = compliant_days < service_days(达标天数未满服务总天数)
              --   服务完成 = compliant_days >= service_days → 不再 active(达标满 service_days 要停)
              --   不设自然日历封顶:过了自然日历但还没达标满 → 继续监测,直到达标天数满。
              --   compliant_days 统计窗口下界用服务锚(防服务前脏行);无上界(纯履约)。
              AND (
                  COALESCE((
                      SELECT COUNT(*) FILTER (WHERE kcl.is_compliant = TRUE)
                      FROM keyword_compliance_log kcl
                      WHERE kcl.keyword_id = s.keyword_id
                        AND kcl.quote_id = s.quote_id
                        AND kcl.check_date >= COALESCE(q.service_start_date, q.paid_at::date)
                  ), 0) < q.service_days
              )

            UNION ALL

            -- ── 手动词臂(K1:功能一模一样;K3:不受报价单约束)──────────────────
            -- [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-2]
            -- Owner 2026-08-16:「监测扣的是别人钱包里面的算力,他手动添加扣的是钱包,
            --   和前面的报价单没什么关系,每天跑每天扣,完全和合同的统一就行了」
            --
            -- 与合同臂**刻意不同**的三处(K3 的落点 · 不许"顺手统一"):
            --   1. 不套 quote 服务锚 quote_service_anchor_condition_sql —— 手动词花的是钱包算力,
            --      由代理自己开关决定,不挂报价单生命周期;
            --   2. 不套 compliant_days < service_days —— 手动词没有服务期承诺,
            --      跑到代理手动关、或余额不足暂停为止(P0-5 的"不自动归档"是同一条推论);
            --   3. quotes 用 LEFT JOIN —— 合同臂那个 INNER JOIN 会让 quote_id 为空的
            --      brand 级手动词直接消失。生产今天 14 条 extra 全部 quote_id 非空,
            --      但列可空且建词路径不保证,靠"现在没有"上线与 P0-1 的 id 侥幸同型。
            --
            -- 🔴 brand_name 不许为空:2026-05-26 那个 P0 的根因就是它空了 →
            --    target_brand="" → 自动监测 100% is_detected=0。合同臂从 quotes 拿,
            --    手动词允许没有 quote,所以回落 brands.name;两个都空则 **不进跑批**
            --    (WHERE 末尾那条 <> '' ),宁可不跑也不跑出一批必然全 0 的结果。
            SELECT s.id, s.user_id, s.keyword_id, s.quote_id, s.brand_id,
                   s.daily_points, s.feature_code, s.last_charged_at,
                   s.billing_mode,
                   'extra'::text AS keyword_source,
                   ek.keyword, ek.monitoring_query,
                   ek.platforms AS entitlement_platforms,
                   COALESCE(NULLIF(q.brand_name, ''), b.name) AS brand_name
            FROM keyword_monitor_subscriptions s
            JOIN extra_keywords ek ON ek.id = s.keyword_id
            LEFT JOIN quotes q ON q.id = s.quote_id
            LEFT JOIN brands b ON b.id = COALESCE(s.brand_id, ek.brand_id)
            WHERE s.status = 'active'
              AND s.keyword_source = 'extra'
              -- 开关(035 加的列)· 与合同臂的 ck.is_monitored 同位
              AND ek.is_monitored = TRUE
              -- 词条本身可用 · 与合同臂的 monitoring_status='active' 同位
              AND COALESCE(ek.status, 'active') = 'active'
              AND COALESCE(NULLIF(q.brand_name, ''), b.name, '') <> ''
            ORDER BY 2, 3
        """)
        return [dict(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def check_hidden_real_customers() -> List[Dict[str, Any]]:
    """[CTO-15.23 2026-05-29 D6 哨兵] 真客户被"服务锚缺失"误隐藏的回归探针。

    confirmed + 两锚(service_start_date / paid_at)均 NULL + 有 active KMS
    = 在跑监测却无服务锚 → 会被 portal 可见性 filter 隐藏(翠玉/QZQZ 同根 P0)。
    返列表(空 = 健康)· daily compliance cron 调用 · >0 则 admin alert。
    """
    import logging as _lg
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT q.id AS quote_id, q.brand_id, b.name AS brand_name,
                   (SELECT COUNT(*) FROM keyword_monitor_subscriptions kms
                    WHERE kms.quote_id = q.id AND kms.status = 'active') AS active_kms
            FROM quotes q JOIN brands b ON b.id = q.brand_id
            WHERE q.status = 'confirmed'
              AND q.paid_at IS NULL AND q.service_start_date IS NULL
              AND EXISTS (SELECT 1 FROM keyword_monitor_subscriptions kms
                          WHERE kms.quote_id = q.id AND kms.status = 'active')
            ORDER BY q.id
        """)
        return [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:
        _lg.getLogger("GEO-Monitoring").warning(f"[sentinel] check_hidden_real_customers 失败: {e}")
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_paused_subscriptions_for_resume() -> List[Dict[str, Any]]:
    """列出 paused_low_balance 订阅 · 02:30 cron 检查余额 → 恢复用

    [audit P0-4 2026-06-10 · 返修] SELECT 必须含 brand_id —— scheduler 恢复段按 brand owner 解析
    计费主体(_load_brand_owner_map 取 {s['brand_id']})。旧版漏 brand_id → owner_map 恒空 →
    恢复预检回落订阅原主体(admin 免扣恒过)→ 充值后振荡照旧。

    [WO_MONITORING_PLATFORM_COVERED 2026-08-16 §2] 同理必须含 billing_mode:
    恢复预检与主扣费链**必须看同一个钱包**。只在主链认 billing_mode、恢复段不认,
    会出现「按平台余额判恢复、按服务商余额扣钱」(或反过来)的振荡 ——
    与上面那条病史是同一个形态的镜像。
    🔴 本次只加 SELECT 列,WHERE / ORDER BY 一字未动 ⇒ **取到哪些行完全不变**。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, user_id, keyword_id, brand_id, daily_points, feature_code,
                   billing_mode
            FROM keyword_monitor_subscriptions
            WHERE status = 'paused_low_balance'
            ORDER BY user_id, keyword_id
        """)
        return [dict(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_subscriptions_by_quote(quote_id: int) -> List[Dict[str, Any]]:
    """按 quote 取所有非 cancelled 订阅 · update_quote_status 联动用"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, user_id, keyword_id, quote_id, status
            FROM keyword_monitor_subscriptions
            WHERE quote_id = %s
              AND status IN ('active', 'paused_low_balance')
        """, (quote_id,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_subscriptions_by_user(
    user_id: int,
    status: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """代理自己列出订阅 · 监测页用"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        if status:
            cur.execute("""
                SELECT s.*, ck.keyword
                FROM keyword_monitor_subscriptions s
                LEFT JOIN confirmed_keywords ck ON ck.id = s.keyword_id
                WHERE s.user_id = %s AND s.status = %s
                ORDER BY s.id DESC LIMIT %s
            """, (user_id, status, limit))
        else:
            cur.execute("""
                SELECT s.*, ck.keyword
                FROM keyword_monitor_subscriptions s
                LEFT JOIN confirmed_keywords ck ON ck.id = s.keyword_id
                WHERE s.user_id = %s
                ORDER BY s.id DESC LIMIT %s
            """, (user_id, limit))
        return [dict(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_keyword_monitor_state(
    keyword_id: int,
    is_monitored: bool,
    subscription_id: Optional[int] = None,
) -> bool:
    """同步 confirmed_keywords.is_monitored + monitoring_subscription_id

    [v2026-05-29 P0 hotfix] disable 时必须清 monitoring_subscription_id=NULL
      根因:旧实现 disable 只 SET is_monitored=FALSE · 不清 sub_id
            → 状态错位:is_monitored=FALSE 但 monitoring_subscription_id 仍指向 cancelled sub
            → 健康检查跑不出"幽灵 sub 残留"· 后续 toggle 误判
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        if is_monitored:
            cur.execute("""
                UPDATE confirmed_keywords
                SET is_monitored = TRUE,
                    monitoring_subscription_id = %s,
                    monitoring_enabled_at = CURRENT_TIMESTAMP,
                    monitoring_status = 'active'
                WHERE id = %s
            """, (subscription_id, keyword_id))
        else:
            # disable:同步清 sub_id(防状态错位)· 保留 monitoring_enabled_at 历史(供审计 / compliance 起算)
            # [2026-06-02 Deploy-CTO P0 修 · 老板选 A] 关闭自动监测后词条必须留在监测列表(开关关掉·可重开),
            #   不是移到归档。get_client_keywords 按 monitoring_status='active' 过滤(不按 is_monitored),
            #   正常 disable 态 = monitoring_status='active' + is_monitored=false(全库 1710 个词都这样)。
            #   原 bug:此处误改 monitoring_status('inactive')→ 偏离 'active' → 被列表过滤掉 = 词条凭空消失
            #   (老板关「重庆璧山万家装饰」发现)。修:disable 不动 monitoring_status(保持 'active'),
            #   只置 is_monitored=FALSE + 清 sub_id。daily 跑监测靠 keyword_monitor_subscriptions.status,
            #   sub 已 cancel → 不会再扣;重开走 enable 分支重建 sub。
            cur.execute("""
                UPDATE confirmed_keywords
                SET is_monitored = FALSE,
                    monitoring_subscription_id = NULL
                WHERE id = %s
            """, (keyword_id,))
        affected = cur.rowcount
        conn.commit()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def set_extra_keyword_monitored(
    keyword_id: int,
    is_monitored: bool,
    operator_user_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """[WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15 P0-A.4] 开/关手动词(extra)的监测。

    🔴🔴 [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-3 · 口径已翻转,以下是新口径]
    Owner 2026-08-16 拍板 K1/K2:**手动词 = 合同词**,唯一区别是列表上那个「合同/手动」标签;
    计费统一为逐词日订阅 monitoring_keyword_daily(130 算力/词/**天**)。
    ⇒ 本函数**降级为"只翻列"的内部原语**,不再是开关的完整实现。
      开关的完整语义现在是:**开 = 建 active 订阅 + 置 is_monitored=TRUE;关 = 销订阅 + 置 FALSE**,
      由 server.py 的 enable/disable 端点组合完成(与 confirmed 侧同路径)。

    ⚠️ 上一版这里写着「extra **没有订阅模型**…**绝不**给 extra 建订阅 —— 建了就会被
    list_active_subscriptions 的 JOIN confirmed_keywords 漏掉」。那句话在当时是对的,
    它成立的前提是**订阅表分不清两种词**;本单 P0-1 给订阅表加了 keyword_source、
    P0-2 把取数改成两臂,前提已不存在。保留这段是为了让下一个人知道口径是**被显式翻转的**,
    不是有人忘了那条注释。

    审计(工单红线 4):开启时记 monitoring_enabled_at + monitoring_enabled_by。
    关闭**保留**这两个值(历史痕迹,与 confirmed 侧 disable 保留 enabled_at 同规矩)。

    返回被改行的 {id, keyword, brand_id, quote_id, is_monitored};找不到返 None。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        if is_monitored:
            cur.execute("""
                UPDATE extra_keywords
                   SET is_monitored = TRUE,
                       monitoring_enabled_at = CURRENT_TIMESTAMP,
                       monitoring_enabled_by = %s
                 WHERE id = %s
                   AND COALESCE(status, 'active') = 'active'
                RETURNING id, keyword, brand_id, quote_id, is_monitored
            """, (operator_user_id, keyword_id))
        else:
            cur.execute("""
                UPDATE extra_keywords
                   SET is_monitored = FALSE
                 WHERE id = %s
                RETURNING id, keyword, brand_id, quote_id, is_monitored
            """, (keyword_id,))
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_subscription_billing_mode(subscription_id: int, billing_mode: str) -> bool:
    """[WO_MONITORING_PLATFORM_COVERED R5 2026-08-17] 改写订阅的**付款方**。

    为什么必须有:enable 的幂等复用分支原来只修 P0-4 的计费主体 user_id、不碰 billing_mode
    ⇒ 管理员在**已有订阅**上选"记平台账",订阅仍是 brand_owner,照旧扣服务商的钱。
    引爆条件:任何一条已存在 active/paused 订阅上再点一次开通并选平台账。

    🔴 取值白名单与迁移 CHECK / 创建函数同口径 —— 三处一致,非法值当场炸出调用方。
    🔴 只按 id 改这一列,不碰主体/达标/扣费。
    """
    _mode = (billing_mode or 'brand_owner').strip()
    if _mode not in ('brand_owner', 'platform'):
        raise ValueError(f"不支持的 billing_mode: {billing_mode!r}(只允许 brand_owner / platform)")
    if not subscription_id:
        return False
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE keyword_monitor_subscriptions SET billing_mode = %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s",
            (_mode, int(subscription_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_subscription_billing_user(subscription_id: int, user_id: int) -> bool:
    """[audit P0-4 2026-06-10 · 返修] 改写订阅计费主体 user_id(锚 brand owner · 修存量错位订阅)。

    仅 server.py enable 幂等复用分支调用:admin 帮客户开通后订阅挂 admin 名下,
    重复点开通时把 user_id 修回 brand owner(否则存量错位永远修不了)。

    🔴 红线授权:本函数含 UPDATE keyword_monitor_subscriptions SET user_id(改计费主体列),是
    db/monitoring_db.py(红线)的写函数,超出"#3 resume 加列"授权范围 → 老板 2026-06-10 经
    Fable 重核点名后【单独授权】(surgical · 只按 id 改错位订阅 user_id · 不碰达标/扣费/检测查询)。
    """
    if not subscription_id or not user_id:
        return False
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE keyword_monitor_subscriptions SET user_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (int(user_id), int(subscription_id)),
        )
        affected = cur.rowcount
        conn.commit()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def record_subscription_charge(
    subscription_id: int,
    amount: int,
) -> bool:
    """daily charge_on_success 完成后写流水(last_charged_at + total_charged)"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE keyword_monitor_subscriptions
            SET last_charged_at = CURRENT_TIMESTAMP,
                last_charge_amount = %s,
                total_charged = COALESCE(total_charged, 0) + %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (amount, amount, subscription_id))
        affected = cur.rowcount
        conn.commit()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def claim_subscription_for_today_with_token(
    subscription_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Atomic claim 当天监测扣费 slot · 防蓝绿双 instance + 多 scheduler race 重复扣

    [CTO-15.23 2026-05-19 P0 fix]
    现象:同 1 keyword 同时间被扣 4 次 130(老板截图实证)
    根因:蓝绿双 container(2x) + 根 scheduler.py + api/scheduler.py 双 BackgroundScheduler(2x)
         09:00 cron 同时触发 daily_monitoring · 同 1 subscription 被 charge 4 次
    修法:DB 层 atomic UPDATE · INTERVAL '23 hours' 窗口 · 23h 内已扣过的 row 不再 claim 成功
         调用方收 False 必须跳过 charge_on_success + LLM 调用

    用 INTERVAL '23 hours' 不依赖 DB 时区设置 · 比 DATE() 比较更稳:
    - 第一次 cron 触发(09:00)· 多个 instance 同时 race · 只 1 个 atomic UPDATE 拿到 row
    - 其他 instance 看到 last_charged_at < 23h 内 → claim 失败 → return False
    - 第二天 09:00 cron 距上次扣费 ~24h > 23h → 自然可 claim 新一天 slot

    Returns a CAS token plus the previous successful timestamp. The token may
    be released only before charge commit and before any result is persisted.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT last_charged_at
            FROM keyword_monitor_subscriptions
            WHERE id = %s
            FOR UPDATE
            """,
            (subscription_id,),
        )
        previous_row = cur.fetchone()
        if not previous_row:
            conn.rollback()
            return None
        previous_value = previous_row.get("last_charged_at")
        # Atomic UPDATE · WHERE 条件保证 race 安全
        cur.execute("""
            UPDATE keyword_monitor_subscriptions
            SET last_charged_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND status = 'active'
              AND (last_charged_at IS NULL
                   OR last_charged_at < CURRENT_TIMESTAMP - INTERVAL '23 hours')
            RETURNING id, last_charged_at
        """, (subscription_id,))
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return None
        return {
            "claim_token": row.get("last_charged_at"),
            "previous_value": previous_value,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def claim_subscription_for_today_with_settlement(
    subscription_id: int, settlement_reference: str,
) -> Optional[Dict[str, Any]]:
    """Atomically claim the daily slot and persist its recovery fence."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT last_charged_at
              FROM public.keyword_monitor_subscriptions
             WHERE id=%s FOR UPDATE
            """,
            (int(subscription_id),),
        )
        previous_row = cur.fetchone()
        if not previous_row:
            conn.rollback()
            return None
        previous_value = previous_row.get("last_charged_at")
        cur.execute(
            """
            UPDATE public.keyword_monitor_subscriptions
               SET last_charged_at=CURRENT_TIMESTAMP
             WHERE id=%s AND status='active'
               AND (last_charged_at IS NULL
                    OR last_charged_at < CURRENT_TIMESTAMP - INTERVAL '23 hours')
             RETURNING last_charged_at
            """,
            (int(subscription_id),),
        )
        claimed = cur.fetchone()
        if not claimed:
            conn.rollback()
            return None
        claim_token = claimed.get("last_charged_at")
        cur.execute(
            """
            UPDATE public.monitoring_keyword_settlements
               SET claim_token=%s, previous_claim_at=%s, updated_at=NOW()
             WHERE settlement_reference=%s AND subscription_id=%s
               AND state='reserved' AND claim_token IS NULL
            """,
            (claim_token, previous_value, str(settlement_reference), int(subscription_id)),
        )
        if cur.rowcount != 1:
            raise MonitoringCellConflict("daily monitoring settlement claim conflict")
        conn.commit()
        return {"claim_token": claim_token, "previous_value": previous_value}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_subscription_for_today(subscription_id: int) -> bool:
    """Compatibility boolean wrapper for older explicit/manual callers."""
    return claim_subscription_for_today_with_token(subscription_id) is not None


def release_subscription_claim(
    subscription_id: int,
    claim_token: object,
    previous_value: object,
) -> bool:
    """Restore a zero-result claim without overwriting a newer charge/run."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE keyword_monitor_subscriptions
            SET last_charged_at = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND last_charged_at IS NOT DISTINCT FROM %s
            """,
            (previous_value, subscription_id, claim_token),
        )
        affected = cur.rowcount
        if affected == 0:
            # Recovery can die after restoring the subscription but before
            # terminalizing its durable settlement. The already-restored value
            # is the same successful CAS; a newer claim still fails closed.
            cur.execute(
                """
                SELECT last_charged_at IS NOT DISTINCT FROM %s AS restored
                  FROM keyword_monitor_subscriptions
                 WHERE id=%s
                """,
                (previous_value, int(subscription_id)),
            )
            current = cur.fetchone()
            affected = 1 if current and bool(current.get("restored")) else 0
        conn.commit()
        return affected == 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_subscription(subscription_id: int) -> Optional[Dict[str, Any]]:
    """按 ID 取订阅"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM keyword_monitor_subscriptions WHERE id = %s
        """, (subscription_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_confirmed_keyword_with_brand(keyword_id: int) -> Optional[Dict[str, Any]]:
    """取 confirmed_keywords + 关联 quote.brand_id + brands.owner_user_id · 用于鉴权"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ck.id, ck.quote_id, ck.keyword, ck.is_monitored,
                   ck.monitoring_status, ck.monitoring_subscription_id,
                   q.brand_id, b.owner_user_id
            FROM confirmed_keywords ck
            LEFT JOIN quotes q ON q.id = ck.quote_id
            LEFT JOIN brands b ON b.id = q.brand_id
            WHERE ck.id = %s
        """, (keyword_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cancel_subscriptions_by_quote(quote_id: int, reason: str = 'quote_status_change') -> int:
    """按 quote 批量 cancel 订阅 · update_quote_status archived/cancelled/refunded 时用"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 先查待 cancel 的 keyword_ids
        cur.execute("""
            SELECT id, keyword_id FROM keyword_monitor_subscriptions
            WHERE quote_id = %s AND status IN ('active', 'paused_low_balance')
        """, (quote_id,))
        rows = cur.fetchall()
        keyword_ids = [r['keyword_id'] for r in rows]

        # 批量 cancel
        cur.execute("""
            UPDATE keyword_monitor_subscriptions
            SET status = 'cancelled',
                paused_reason = %s,
                cancelled_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE quote_id = %s AND status IN ('active', 'paused_low_balance')
        """, (reason, quote_id))
        cancelled = cur.rowcount

        # 同步 confirmed_keywords.is_monitored=FALSE
        if keyword_ids:
            cur.execute("""
                UPDATE confirmed_keywords
                SET is_monitored = FALSE
                WHERE id = ANY(%s)
            """, (keyword_ids,))

        conn.commit()
        return cancelled
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cancel_subscriptions_by_brand(brand_id: int, reason: str = 'brand_soft_deleted') -> int:
    """按 brand 批量 cancel 订阅 · 客户软删时用"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, keyword_id FROM keyword_monitor_subscriptions
            WHERE brand_id = %s AND status IN ('active', 'paused_low_balance')
        """, (brand_id,))
        rows = cur.fetchall()
        keyword_ids = [r['keyword_id'] for r in rows]

        cur.execute("""
            UPDATE keyword_monitor_subscriptions
            SET status = 'cancelled',
                paused_reason = %s,
                cancelled_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE brand_id = %s AND status IN ('active', 'paused_low_balance')
        """, (reason, brand_id))
        cancelled = cur.rowcount

        if keyword_ids:
            cur.execute("""
                UPDATE confirmed_keywords
                SET is_monitored = FALSE
                WHERE id = ANY(%s)
            """, (keyword_ids,))

        conn.commit()
        return cancelled
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==========================================
# [P0-A 2026-05-25] reverify 历史假阳数据(同后缀不同前缀)
# ==========================================
async def reverify_keyword_results(
    keyword_id: int,
    keyword_source: str = "confirmed",
    days: int = 30,
    dry_run: bool = True,
) -> dict:
    """Use the deterministic resolver to re-evaluate eligible historical hits.

    This maintenance path is physically provider-free. Ambiguous rows remain
    pending for the durable human-review workflow instead of spending again.
    """
    from services.brand_identity_resolver import BrandIdentityResolver, BrandVerdict

    conn = get_connection()
    try:
        cur = conn.cursor()
        if keyword_source == "confirmed":
            cur.execute(
                """
                SELECT q.brand_name, q.brand_id FROM confirmed_keywords ck
                JOIN quotes q ON q.id = ck.quote_id
                WHERE ck.id = %s
                """,
                (keyword_id,),
            )
        else:
            cur.execute(
                """
                SELECT q.brand_name, q.brand_id FROM extra_keywords ek
                JOIN quotes q ON q.id = ek.quote_id
                WHERE ek.id = %s
                """,
                (keyword_id,),
            )
        row = cur.fetchone()
        if not row:
            return {"error": "keyword not found"}
        target_brand = row["brand_name"] if isinstance(row, dict) else row[0]
        target_brand_id = row.get("brand_id") if isinstance(row, dict) else row[1]
        resolver = BrandIdentityResolver.for_brand(
            target_brand_id,
            fallback_name=target_brand,
        )

        from services.monitoring_identity_review import aggregate_eligible_sql

        cur.execute(
            f"""
            SELECT id, full_response, response_snippet
            FROM public.monitoring_results mr
            WHERE mr.keyword_id = %s AND mr.is_detected = 1
              AND {aggregate_eligible_sql('mr')}
              AND tested_at >= (NOW() AT TIME ZONE 'Asia/Shanghai')::date - INTERVAL '{int(days)} days'
            """,
            (keyword_id,),
        )
        rows = cur.fetchall()

        fixed = 0
        samples = []
        for r in rows:
            rec = r if isinstance(r, dict) else {"id": r[0], "full_response": r[1], "response_snippet": r[2]}
            text = rec.get("full_response") or rec.get("response_snippet") or ""
            if len(text.strip()) < 50:
                continue
            decision = resolver.resolve_local(text)
            if decision.verdict is BrandVerdict.UNKNOWN:
                continue
            if decision.verdict is BrandVerdict.NO:
                fixed += 1
                if len(samples) < 50:
                    samples.append({"row_id": rec["id"], "reason": decision.reason})
                if not dry_run:
                    cur.execute(
                        "UPDATE monitoring_results SET is_detected=0, mention_type='reverify_corrected' WHERE id=%s",
                        (rec["id"],),
                    )

        if not dry_run:
            conn.commit()
        else:
            conn.rollback()

        return {
            "dry_run": dry_run,
            "scanned": len(rows),
            "fixed": fixed,
            "samples": samples,
            "target_brand": target_brand,
            "keyword_id": keyword_id,
            "keyword_source": keyword_source,
            "days": int(days),
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass
