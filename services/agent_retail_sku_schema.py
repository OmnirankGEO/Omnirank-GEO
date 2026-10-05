"""Read-only schema contract for canonical service-provider retail SKUs.

DDL belongs exclusively to the prestart migration manifest.  Web/cron and every
retail management writer call this module only to fail closed on missing or
tampered columns, constraints and indexes.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable


TABLE = "agent_sku_overrides"


def _row_dict(row: Any, cur=None) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    description = getattr(cur, "description", None) or []
    if isinstance(row, (tuple, list)) and len(description) == len(row):
        return {
            str(getattr(column, "name", None) or column[0]): value
            for column, value in zip(description, row)
        }
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


EXPECTED_COLUMNS = {
    "retail_sku_id": ("text", False),
    "points_granted": ("bigint", False),
    "source_template_id": ("integer", True),
    "version": ("integer", False),
    "client_request_id": ("text", True),
    "sku_template_id": ("integer", True),
    "custom_name": ("text", False),
    "custom_subtitle": ("text", True),
    "custom_sales_pitch": ("text", True),
    "custom_scene": ("text", True),
    "retail_cents": ("integer", False),
    "is_active": ("boolean", True),
    "sort_order": ("integer", False),
    "deleted_at": ("timestamp with time zone", True),
}

EXPECTED_DEFAULTS = {
    "version": ("1", "1::integer"),
    "sort_order": ("0", "0::integer"),
}

EXPECTED_CONSTRAINTS = {
    "fk_agent_retail_sku_source_template": (
        "foreignkey(source_template_id)referencessku_templates(id)",
    ),
    "chk_agent_retail_sku_identity": (
        "check(retail_sku_id~'^rsku-[a-z0-9-]{8,59}$'::text)",
    ),
    "chk_agent_retail_sku_points": (
        "check(points_granted>0andpoints_granted<='9000000000000000'::bigint)",
    ),
    "chk_agent_retail_sku_money": (
        "check(retail_cents>0andretail_cents<=2000000000)",
    ),
    "chk_agent_retail_sku_version": ("check(version>=1)",),
    "chk_agent_retail_sku_name": (
        "check(char_length(btrim(custom_name))>=1andchar_length(btrim(custom_name))<=120)",
    ),
    "chk_agent_retail_sku_client_request": (
        "check(client_request_idisnullorchar_length(btrim(client_request_id))>=8andchar_length(btrim(client_request_id))<=80)",
    ),
    "chk_agent_retail_sku_copy_lengths": (
        "check((custom_subtitleisnullorchar_length(custom_subtitle)<=240)and(custom_sales_pitchisnullorchar_length(custom_sales_pitch)<=1000)and(custom_sceneisnullorchar_length(custom_scene)<=500))",
    ),
    "chk_agent_retail_sku_tombstone": (
        "check(deleted_atisnulloris_active=false)",
    ),
}

EXPECTED_INDEXES = {
    "ux_agent_retail_sku_id": {
        "unique": True,
        "columns": ["retail_sku_id"],
        "predicate": "",
    },
    "ux_agent_retail_sku_client_request": {
        "unique": True,
        "columns": ["agent_user_id", "client_request_id"],
        "predicate": "(client_request_idisnotnull)",
    },
    "idx_agent_retail_sku_catalog": {
        "unique": False,
        "columns": ["agent_user_id", "is_active", "sort_order", "id"],
        "predicate": "(deleted_atisnull)",
    },
    "idx_agent_retail_sku_source_template": {
        "unique": False,
        "columns": ["source_template_id"],
        "predicate": "(source_template_idisnotnull)",
    },
}

EXPECTED_TRIGGER_DEFINITIONS = (
    "CREATE TRIGGER trg_agent_retail_sku_legacy_fill BEFORE INSERT ON agent_sku_overrides "
    "FOR EACH ROW EXECUTE FUNCTION agent_retail_sku_legacy_fill()",
)

EXPECTED_LEGACY_FILL_FUNCTION_FRAGMENTS = (
    "IF NEW.sku_template_id IS NULL THEN RETURN NEW; END IF;",
    "FROM sku_templates WHERE id = NEW.sku_template_id;",
    "IF NEW.retail_sku_id IS NULL THEN NEW.retail_sku_id := 'RSKU-LEG-'",
    "NEW.points_granted := COALESCE(NEW.points_granted, template_points);",
    "NEW.source_template_id := COALESCE(NEW.source_template_id, NEW.sku_template_id);",
    "NEW.custom_name := COALESCE(NULLIF(BTRIM(template_name), ''), template_code_value);",
    "NEW.version := COALESCE(NEW.version, 1);",
    "NEW.sort_order := COALESCE(NEW.sort_order, 0);",
)


def _one_of(actual: str, expected: Iterable[str]) -> bool:
    normalized = _norm(actual)
    return any(normalized == _norm(item) for item in expected)


def schema_status(cur) -> Dict[str, Any]:
    blockers: list[str] = []
    cur.execute("SELECT to_regclass(%s) AS table_name", (TABLE,))
    table_row = _row_dict(cur.fetchone(), cur)
    if not table_row.get("table_name"):
        return {"ready": False, "blockers": [f"{TABLE} 表不存在"]}

    cur.execute(
        """
        SELECT a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               NOT a.attnotnull AS is_nullable,
               pg_get_expr(d.adbin, d.adrelid) AS column_default
          FROM pg_attribute a
          LEFT JOIN pg_attrdef d
            ON d.adrelid=a.attrelid AND d.adnum=a.attnum
         WHERE a.attrelid=%s::regclass
           AND a.attnum > 0 AND NOT a.attisdropped
        """,
        (TABLE,),
    )
    columns = {
        _row_dict(row, cur).get("column_name"): _row_dict(row, cur)
        for row in cur.fetchall()
    }
    for name, (data_type, nullable) in EXPECTED_COLUMNS.items():
        row = columns.get(name)
        if not row:
            blockers.append(f"{TABLE}.{name} 缺失")
            continue
        if str(row.get("data_type")) != data_type:
            blockers.append(f"{TABLE}.{name} 类型应为 {data_type}")
        actual_nullable = row.get("is_nullable") is True
        if actual_nullable != nullable:
            blockers.append(
                f"{TABLE}.{name} 可空性应为 {'NULL' if nullable else 'NOT NULL'}"
            )
    for name, expected in EXPECTED_DEFAULTS.items():
        if name in columns and not _one_of(columns[name].get("column_default"), expected):
            blockers.append(f"{TABLE}.{name} 默认值定义不匹配")

    cur.execute(
        """
        SELECT c.conname, c.convalidated, c.conrelid::regclass::text AS table_name,
               pg_get_constraintdef(c.oid, TRUE) AS definition
          FROM pg_constraint c
         WHERE c.conname = ANY(%s)
           AND c.conrelid = %s::regclass
        """,
        (list(EXPECTED_CONSTRAINTS), TABLE),
    )
    constraints = {
        _row_dict(row, cur).get("conname"): _row_dict(row, cur)
        for row in cur.fetchall()
    }
    for name, definitions in EXPECTED_CONSTRAINTS.items():
        row = constraints.get(name)
        if not row:
            blockers.append(f"约束 {name} 缺失")
            continue
        if str(row.get("table_name", "")).split(".")[-1] != TABLE:
            blockers.append(f"约束 {name} 所属表错误")
        if row.get("convalidated") is not True:
            blockers.append(f"约束 {name} 未验证")
        if not _one_of(str(row.get("definition") or ""), definitions):
            blockers.append(f"约束 {name} 定义不匹配")

    cur.execute(
        """
        SELECT ci.relname AS index_name, ct.relname AS table_name,
               i.indisunique, i.indisvalid, i.indisready,
               ARRAY(
                   SELECT pg_get_indexdef(i.indexrelid, pos, TRUE)
                     FROM generate_series(1, i.indnkeyatts) AS pos
                    ORDER BY pos
               ) AS key_columns,
               COALESCE(pg_get_expr(i.indpred, i.indrelid), '') AS predicate
          FROM pg_index i
         JOIN pg_class ci ON ci.oid=i.indexrelid
         JOIN pg_class ct ON ct.oid=i.indrelid
         WHERE ci.relname = ANY(%s)
           AND i.indrelid = %s::regclass
        """,
        (list(EXPECTED_INDEXES), TABLE),
    )
    indexes = {
        _row_dict(row, cur).get("index_name"): _row_dict(row, cur)
        for row in cur.fetchall()
    }
    for name, expected in EXPECTED_INDEXES.items():
        row = indexes.get(name)
        if not row:
            blockers.append(f"索引 {name} 缺失")
            continue
        if str(row.get("table_name")) != TABLE:
            blockers.append(f"索引 {name} 所属表错误")
        if bool(row.get("indisunique")) != bool(expected["unique"]):
            blockers.append(f"索引 {name} 唯一性错误")
        if row.get("indisvalid") is not True or row.get("indisready") is not True:
            blockers.append(f"索引 {name} 未 ready/valid")
        if [_norm(value) for value in (row.get("key_columns") or [])] != [
            _norm(value) for value in expected["columns"]
        ]:
            blockers.append(f"索引 {name} 列或顺序错误")
        if _norm(row.get("predicate")) != _norm(expected["predicate"]):
            blockers.append(f"索引 {name} predicate 错误")

    cur.execute(
        """
        SELECT t.tgname, t.tgenabled, c.relname AS table_name,
               table_ns.nspname AS table_schema,
               p.proname AS function_name, function_ns.nspname AS function_schema,
               pg_get_triggerdef(t.oid, TRUE) AS definition,
               pg_get_functiondef(t.tgfoid) AS function_definition
          FROM pg_trigger t
          JOIN pg_class c ON c.oid=t.tgrelid
          JOIN pg_namespace table_ns ON table_ns.oid=c.relnamespace
          JOIN pg_proc p ON p.oid=t.tgfoid
          JOIN pg_namespace function_ns ON function_ns.oid=p.pronamespace
         WHERE t.tgname='trg_agent_retail_sku_legacy_fill'
           AND t.tgrelid=%s::regclass AND NOT t.tgisinternal
        """,
        (TABLE,),
    )
    trigger = _row_dict(cur.fetchone(), cur)
    if not trigger:
        blockers.append("触发器 trg_agent_retail_sku_legacy_fill 缺失")
    else:
        if trigger.get("table_name") != TABLE:
            blockers.append("触发器 trg_agent_retail_sku_legacy_fill 所属表错误")
        if trigger.get("tgenabled") != "O":
            blockers.append("触发器 trg_agent_retail_sku_legacy_fill 未启用")
        if trigger.get("function_name") != "agent_retail_sku_legacy_fill":
            blockers.append("触发器 trg_agent_retail_sku_legacy_fill 执行函数错误")
        if trigger.get("function_schema") != trigger.get("table_schema"):
            blockers.append("触发器 trg_agent_retail_sku_legacy_fill 执行函数 schema 错误")
        if not _one_of(trigger.get("definition"), EXPECTED_TRIGGER_DEFINITIONS):
            blockers.append("触发器 trg_agent_retail_sku_legacy_fill 定义不匹配")
        function_definition = _norm(trigger.get("function_definition"))
        if any(
            _norm(fragment) not in function_definition
            for fragment in EXPECTED_LEGACY_FILL_FUNCTION_FRAGMENTS
        ):
            blockers.append("函数 agent_retail_sku_legacy_fill 定义不匹配")

    if not blockers:
        cur.execute(
            """
            SELECT COUNT(*) AS invalid_count
              FROM agent_sku_overrides
             WHERE retail_sku_id IS NULL
                OR points_granted IS NULL OR points_granted <= 0
                OR points_granted > 9000000000000000
                OR retail_cents <= 0 OR retail_cents > 2000000000
                OR is_active IS NULL
                OR version < 1 OR NULLIF(BTRIM(custom_name), '') IS NULL
            """
        )
        row = _row_dict(cur.fetchone(), cur)
        if int(row.get("invalid_count") or 0):
            blockers.append("agent_sku_overrides 存在非法 canonical 零售 SKU")

    return {"ready": not blockers, "blockers": blockers}


def assert_schema_ready(cur) -> None:
    status = schema_status(cur)
    if not status["ready"]:
        raise RuntimeError("零售 SKU schema 未就绪：" + "；".join(status["blockers"]))
