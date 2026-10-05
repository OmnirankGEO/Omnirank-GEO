"""Four-dimensional schema verification for admin user governance.

Web/cron workers call this read-only verifier.  It checks column names, types,
nullability, constraints, primary keys, uniqueness and operational indexes; DDL
remains prestart-only in the migration script.
"""

from __future__ import annotations

from typing import Dict, Iterable, Tuple


EXPECTED_COLUMNS: Dict[str, Dict[str, Tuple[str, bool]]] = {
    "admin_user_governance_versions": {
        "subject_user_id": ("integer", False), "scope": ("text", False),
        "version": ("bigint", False), "updated_at": ("timestamp with time zone", False),
    },
    "admin_user_governance_audits": {
        "id": ("bigint", False), "subject_user_id": ("integer", False),
        "scope": ("text", False), "operator_user_id": ("integer", False),
        "operator_username": ("text", True), "request_id": ("text", False),
        "reason": ("text", False), "before_snapshot": ("jsonb", False),
        "after_snapshot": ("jsonb", False), "evidence_jsonb": ("jsonb", False),
        "version_before": ("bigint", False), "version_after": ("bigint", False),
        "ip_address": ("text", True), "created_at": ("timestamp with time zone", False),
    },
    "customer_agent_binding_history": {
        "id": ("bigint", False), "customer_user_id": ("integer", False),
        "provider_user_id": ("integer", True), "relationship_state": ("text", False),
        "source_binding_id": ("integer", True), "binding_source": ("text", True),
        "source_token": ("text", True), "effective_from": ("timestamp with time zone", False),
        "effective_to": ("timestamp with time zone", True), "dispute_status": ("text", True),
        "dispute_note": ("text", True), "created_by_operator_user_id": ("integer", True),
        "created_reason": ("text", True), "created_request_id": ("text", True),
        "ended_by_operator_user_id": ("integer", True), "ended_reason": ("text", True),
        "ended_request_id": ("text", True), "evidence_jsonb": ("jsonb", False),
        "created_at": ("timestamp with time zone", False),
    },
}

REQUIRED_CHECKS = {
    "admin_user_governance_versions": {
        "admin_user_governance_versions_scope_check": (
            "business_identity", "commercial_binding", "channel_relationship",
            "platform_access", "password_security", "wallet_adjustment",
        ),
        "admin_user_governance_versions_version_check": ("version", ">= 1"),
    },
    "admin_user_governance_audits": {
        "admin_user_governance_audits_scope_check": (
            "business_identity", "commercial_binding", "channel_relationship",
            "platform_access", "password_security", "wallet_adjustment",
        ),
        "admin_user_governance_audits_reason_check": ("btrim(reason)", ">= 2", "<= 500"),
        "admin_user_governance_audits_version_before_check": ("version_before", ">= 1"),
        "admin_user_governance_audits_version_after_check": (
            "version_after", "version_before", "+ 1",
        ),
    },
    "customer_agent_binding_history": {
        "customer_agent_binding_history_relationship_state_check": (
            "service_provider", "platform_direct",
        ),
        "customer_agent_binding_history_provider_state_check": (
            "relationship_state", "provider_user_id", "is not null", "is null",
        ),
        "customer_agent_binding_history_effective_range_check": (
            "effective_to", "effective_from", ">=",
        ),
    },
}

REQUIRED_INDEXES = {
    "idx_admin_user_governance_audit_subject": ("subject_user_id", "created_at desc"),
    "idx_admin_user_governance_audit_operator": ("operator_user_id", "created_at desc"),
    "uq_admin_user_governance_audits_request_id": ("unique index", "request_id"),
    "uq_customer_agent_binding_history_active": (
        "unique index", "customer_user_id", "effective_to is null",
    ),
    "uq_customer_agent_binding_history_created_request": (
        "unique index", "created_request_id", "created_request_id is not null",
    ),
    "idx_customer_agent_binding_history_timeline": (
        "customer_user_id", "effective_from desc", "id desc",
    ),
}


def _rows_by_name(rows: Iterable[dict], key: str) -> Dict[str, dict]:
    return {str(row[key]): row for row in rows}


def _fetch_dicts(cur):
    rows = cur.fetchall()
    if not rows or isinstance(rows[0], dict):
        return rows
    names = [getattr(item, "name", None) or item[0] for item in cur.description]
    return [dict(zip(names, row)) for row in rows]


def verify_admin_user_governance_schema(cur) -> None:
    errors = []
    for table, expected in EXPECTED_COLUMNS.items():
        cur.execute("SELECT to_regclass(%s) AS table_name", (table,))
        row = cur.fetchone()
        exists = row.get("table_name") if isinstance(row, dict) else row[0]
        if exists is None:
            errors.append(f"{table}: missing table")
            continue
        cur.execute(
            """SELECT column_name,data_type,is_nullable
               FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name=%s""",
            (table,),
        )
        columns = _rows_by_name(_fetch_dicts(cur), "column_name")
        for name, (data_type, nullable) in expected.items():
            actual = columns.get(name)
            if not actual:
                errors.append(f"{table}.{name}: missing column")
                continue
            if actual["data_type"] != data_type:
                errors.append(f"{table}.{name}: type {actual['data_type']} != {data_type}")
            actual_nullable = actual["is_nullable"] == "YES"
            if actual_nullable != nullable:
                errors.append(f"{table}.{name}: nullable={actual_nullable} != {nullable}")

        cur.execute(
            """SELECT c.conname,c.contype,c.convalidated,
                      pg_get_constraintdef(c.oid) AS definition,
                      array_agg(a.attname ORDER BY u.ordinality) AS columns
               FROM pg_constraint c
               JOIN pg_class t ON t.oid=c.conrelid
               JOIN pg_namespace n ON n.oid=t.relnamespace
               LEFT JOIN unnest(c.conkey) WITH ORDINALITY u(attnum,ordinality) ON TRUE
               LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=u.attnum
               WHERE n.nspname=current_schema() AND t.relname=%s
               GROUP BY c.oid,c.conname,c.contype""",
            (table,),
        )
        constraints = _rows_by_name(_fetch_dicts(cur), "conname")
        missing_checks = set(REQUIRED_CHECKS[table]) - set(constraints)
        for name in sorted(missing_checks):
            errors.append(f"{table}: missing check {name}")
        for name, fragments in REQUIRED_CHECKS[table].items():
            if name not in constraints:
                continue
            if constraints[name]["convalidated"] is not True:
                errors.append(f"{table}: check {name} not validated")
            definition = " ".join(str(constraints[name]["definition"]).lower().split())
            for fragment in fragments:
                if fragment not in definition:
                    errors.append(f"{table}: malformed check {name} missing {fragment}")
        primary_keys = [row for row in constraints.values() if row["contype"] == "p"]
        expected_pk = ["subject_user_id", "scope"] if table.endswith("versions") else ["id"]
        if len(primary_keys) != 1 or list(primary_keys[0]["columns"] or []) != expected_pk:
            errors.append(f"{table}: primary key != {expected_pk}")

    cur.execute(
        """SELECT indexname,indexdef FROM pg_indexes
           WHERE schemaname=current_schema()
             AND tablename IN (
               'admin_user_governance_audits','customer_agent_binding_history'
             )"""
    )
    index_rows = _fetch_dicts(cur)
    indexes = _rows_by_name(index_rows, "indexname")
    for name in sorted(set(REQUIRED_INDEXES) - set(indexes)):
        errors.append(f"missing index {name}")
    for name, fragments in REQUIRED_INDEXES.items():
        if name not in indexes:
            continue
        definition = " ".join(str(indexes[name]["indexdef"]).lower().replace("(", " ").replace(")", " ").split())
        for fragment in fragments:
            if fragment not in definition:
                errors.append(f"malformed index {name} missing {fragment}")
    if errors:
        raise RuntimeError("admin user governance schema incomplete: " + "; ".join(errors))
