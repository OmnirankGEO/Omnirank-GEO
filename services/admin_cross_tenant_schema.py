"""Exact PostgreSQL contract for ADMIN cross-tenant governance schema."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ColumnSpec:
    pg_type: str
    not_null: bool
    default: Optional[str] = None


def c(pg_type: str, not_null: bool, default: Optional[str] = None) -> ColumnSpec:
    return ColumnSpec(pg_type, not_null, default)


COLUMN_CONTRACTS = {
    "admin_governance_subject_versions": {
        "subject_kind": c("text", True), "subject_id": c("bigint", True),
        "capability": c("text", True), "version": c("bigint", True, "1"),
        "updated_at": c("timestamp with time zone", True, "now()"),
    },
    "admin_cross_tenant_audits": {
        "id": c("bigint", True, "sequence:admin_cross_tenant_audits_id_seq"),
        "actor_user_id": c("integer", True), "actor_username": c("text", False),
        "action": c("text", True), "subject_kind": c("text", True),
        "subject_id": c("bigint", False), "request_id": c("text", True),
        "event_hash": c("text", True), "reason": c("text", True),
        "before_snapshot": c("jsonb", True, "'{}'"),
        "after_snapshot": c("jsonb", True, "'{}'"),
        "ip_address": c("text", False),
        "created_at": c("timestamp with time zone", True, "now()"),
    },
    "organization_invite_accept_receipts": {
        "invite_id": c("bigint", True),
        "accepted_user_id": c("integer", True),
        "accepted_request_id": c("text", True),
        "accept_request_hash": c("text", True),
        "accepted_result": c("jsonb", True),
        "created_at": c("timestamp with time zone", True, "now()"),
    },
    "admin_demo_cases": {
        "case_id": c("uuid", True), "brand_id": c("bigint", True),
        "diagnosis_id": c("bigint", True), "status": c("text", True, "'active'"),
        "snapshot_version": c("bigint", True, "1"), "safe_snapshot": c("jsonb", True),
        "frozen_at": c("timestamp with time zone", True, "now()"),
        "created_by_user_id": c("integer", True), "created_reason": c("text", True),
        "created_request_id": c("text", True),
        "created_at": c("timestamp with time zone", True, "now()"),
    },
    "admin_demo_case_grants": {
        "id": c("bigint", True, "sequence:admin_demo_case_grants_id_seq"),
        "grantee_kind": c("text", True), "grantee_user_id": c("integer", False),
        "grantee_organization_id": c("bigint", False), "case_id": c("uuid", True),
        "brand_id": c("bigint", True),
        "capability": c("text", True, "'demo.customer.preview'"),
        "status": c("text", True, "'active'"),
        "valid_from": c("timestamp with time zone", True, "now()"),
        "expires_at": c("timestamp with time zone", True), "version": c("bigint", True, "1"),
        "created_by_user_id": c("integer", True), "created_reason": c("text", True),
        "note": c("text", True, "''"), "request_payload_hash": c("text", True),
        "created_request_id": c("text", True), "revoked_by_user_id": c("integer", False),
        "revoked_reason": c("text", False), "revoked_request_id": c("text", False),
        "revoked_at": c("timestamp with time zone", False),
        "expired_at": c("timestamp with time zone", False),
        "created_at": c("timestamp with time zone", True, "now()"),
        "updated_at": c("timestamp with time zone", True, "now()"),
    },
    "admin_demo_access_events": {
        "id": c("bigint", True, "sequence:admin_demo_access_events_id_seq"),
        "grant_id": c("bigint", True), "viewer_user_id": c("integer", True),
        "brand_id": c("bigint", True), "case_id": c("uuid", True),
        "access_mode": c("text", True, "'demo'"), "action": c("text", True),
        "request_id": c("text", True), "ip_address": c("text", False),
        "blocked_reason": c("text", False),
        "created_at": c("timestamp with time zone", True, "now()"),
    },
    "admin_provider_downgrade_plans": {
        "id": c("bigint", True, "sequence:admin_provider_downgrade_plans_id_seq"),
        "provider_user_id": c("integer", True), "strategy": c("text", True),
        "target_provider_user_id": c("integer", False), "status": c("text", True, "'draft'"),
        "dependency_snapshot": c("jsonb", True, "'{}'"),
        "next_steps": c("jsonb", True, "'[]'"),
        "expected_identity_version": c("bigint", True), "version": c("bigint", True, "1"),
        "created_by_user_id": c("integer", True), "created_reason": c("text", True),
        "created_request_id": c("text", True), "confirmed_by_user_id": c("integer", False),
        "confirmed_reason": c("text", False), "confirmed_request_id": c("text", False),
        "confirmed_at": c("timestamp with time zone", False),
        "created_at": c("timestamp with time zone", True, "now()"),
        "updated_at": c("timestamp with time zone", True, "now()"),
    },
}


@dataclass(frozen=True)
class KeySpec:
    table: str
    kind: str
    columns: tuple[str, ...]
    ref_table: Optional[str] = None
    ref_columns: tuple[str, ...] = ()
    update_action: str = "a"  # PostgreSQL pg_constraint code for NO ACTION
    delete_action: str = "a"


KEY_CONTRACTS = {
    "admin_governance_subject_versions_pkey": KeySpec("admin_governance_subject_versions", "p", ("subject_kind", "subject_id", "capability")),
    "admin_cross_tenant_audits_pkey": KeySpec("admin_cross_tenant_audits", "p", ("id",)),
    "admin_cross_tenant_audits_request_id_key": KeySpec("admin_cross_tenant_audits", "u", ("request_id",)),
    "admin_cross_tenant_audits_actor_user_id_fkey": KeySpec("admin_cross_tenant_audits", "f", ("actor_user_id",), "users", ("id",)),
    "organization_invite_accept_receipts_pkey": KeySpec("organization_invite_accept_receipts", "p", ("invite_id",)),
    "organization_invite_accept_receipts_accepted_request_id_key": KeySpec("organization_invite_accept_receipts", "u", ("accepted_request_id",)),
    "organization_invite_accept_receipts_invite_id_fkey": KeySpec("organization_invite_accept_receipts", "f", ("invite_id",), "organization_invites", ("id",)),
    "organization_invite_accept_receipts_accepted_user_id_fkey": KeySpec("organization_invite_accept_receipts", "f", ("accepted_user_id",), "users", ("id",)),
    "admin_demo_cases_pkey": KeySpec("admin_demo_cases", "p", ("case_id",)),
    "admin_demo_cases_brand_id_diagnosis_id_key": KeySpec("admin_demo_cases", "u", ("brand_id", "diagnosis_id")),
    "admin_demo_cases_case_id_brand_id_key": KeySpec("admin_demo_cases", "u", ("case_id", "brand_id")),
    "admin_demo_cases_created_request_id_key": KeySpec("admin_demo_cases", "u", ("created_request_id",)),
    "admin_demo_cases_created_by_user_id_fkey": KeySpec("admin_demo_cases", "f", ("created_by_user_id",), "users", ("id",)),
    "admin_demo_case_grants_pkey": KeySpec("admin_demo_case_grants", "p", ("id",)),
    "admin_demo_case_grants_created_request_id_key": KeySpec("admin_demo_case_grants", "u", ("created_request_id",)),
    "admin_demo_case_grants_revoked_request_id_key": KeySpec("admin_demo_case_grants", "u", ("revoked_request_id",)),
    "admin_demo_case_grants_grantee_user_id_fkey": KeySpec("admin_demo_case_grants", "f", ("grantee_user_id",), "users", ("id",)),
    "admin_demo_case_grants_grantee_organization_id_fkey": KeySpec("admin_demo_case_grants", "f", ("grantee_organization_id",), "organizations", ("id",)),
    "admin_demo_case_grants_created_by_user_id_fkey": KeySpec("admin_demo_case_grants", "f", ("created_by_user_id",), "users", ("id",)),
    "admin_demo_case_grants_revoked_by_user_id_fkey": KeySpec("admin_demo_case_grants", "f", ("revoked_by_user_id",), "users", ("id",)),
    "admin_demo_case_grants_case_fk": KeySpec("admin_demo_case_grants", "f", ("case_id", "brand_id"), "admin_demo_cases", ("case_id", "brand_id")),
    "admin_demo_access_events_pkey": KeySpec("admin_demo_access_events", "p", ("id",)),
    "admin_demo_access_events_idempotency_key": KeySpec("admin_demo_access_events", "u", ("grant_id", "viewer_user_id", "request_id", "action")),
    "admin_demo_access_events_grant_id_fkey": KeySpec("admin_demo_access_events", "f", ("grant_id",), "admin_demo_case_grants", ("id",)),
    "admin_demo_access_events_viewer_user_id_fkey": KeySpec("admin_demo_access_events", "f", ("viewer_user_id",), "users", ("id",)),
    "admin_demo_access_events_case_fk": KeySpec("admin_demo_access_events", "f", ("case_id", "brand_id"), "admin_demo_cases", ("case_id", "brand_id")),
    "admin_provider_downgrade_plans_pkey": KeySpec("admin_provider_downgrade_plans", "p", ("id",)),
    "admin_provider_downgrade_plans_created_request_id_key": KeySpec("admin_provider_downgrade_plans", "u", ("created_request_id",)),
    "admin_provider_downgrade_plans_confirmed_request_id_key": KeySpec("admin_provider_downgrade_plans", "u", ("confirmed_request_id",)),
    "admin_provider_downgrade_plans_provider_user_id_fkey": KeySpec("admin_provider_downgrade_plans", "f", ("provider_user_id",), "users", ("id",)),
    "admin_provider_downgrade_plans_target_provider_user_id_fkey": KeySpec("admin_provider_downgrade_plans", "f", ("target_provider_user_id",), "users", ("id",)),
    "admin_provider_downgrade_plans_created_by_user_id_fkey": KeySpec("admin_provider_downgrade_plans", "f", ("created_by_user_id",), "users", ("id",)),
    "admin_provider_downgrade_plans_confirmed_by_user_id_fkey": KeySpec("admin_provider_downgrade_plans", "f", ("confirmed_by_user_id",), "users", ("id",)),
}


@dataclass(frozen=True)
class CheckSpec:
    table: str
    columns: tuple[str, ...]
    literals: tuple[str, ...] = ()
    required: tuple[str, ...] = ()


CHECK_CONTRACTS = {
    "admin_governance_subject_versions_kind_check": CheckSpec("admin_governance_subject_versions", ("subject_kind",), ("user", "organization", "demo_grant", "provider_downgrade_plan"), ("subject_kind", "any", "array")),
    "admin_governance_subject_versions_subject_id_check": CheckSpec("admin_governance_subject_versions", ("subject_id",), (), ("subject_id", ">0")),
    "admin_governance_subject_versions_capability_check": CheckSpec("admin_governance_subject_versions", ("capability",), ("",), ("btrim", "capability", "<>''")),
    "admin_governance_subject_versions_version_check": CheckSpec("admin_governance_subject_versions", ("version",), (), ("version", ">=1")),
    "admin_cross_tenant_audits_action_check": CheckSpec("admin_cross_tenant_audits", ("action",), ("",), ("btrim", "action", "<>''")),
    "admin_cross_tenant_audits_subject_kind_check": CheckSpec("admin_cross_tenant_audits", ("subject_kind",), ("",), ("btrim", "subject_kind", "<>''")),
    "admin_cross_tenant_audits_request_id_check": CheckSpec("admin_cross_tenant_audits", ("request_id",), ("",), ("btrim", "request_id", "<>''")),
    "admin_cross_tenant_audits_event_hash_check": CheckSpec("admin_cross_tenant_audits", ("event_hash",), ("^[0-9a-f]{64}$",), ("event_hash", "~")),
    "admin_cross_tenant_audits_reason_check": CheckSpec("admin_cross_tenant_audits", ("reason",), (), ("char_length", "btrim", ">=2", "<=500")),
    "organization_invite_accept_receipts_request_id_check": CheckSpec("organization_invite_accept_receipts", ("accepted_request_id",), ("",), ("btrim", "accepted_request_id", "<>''")),
    "organization_invite_accept_receipts_hash_check": CheckSpec("organization_invite_accept_receipts", ("accept_request_hash",), ("^[0-9a-f]{64}$",), ("accept_request_hash", "~")),
    "organization_invite_accept_receipts_result_check": CheckSpec("organization_invite_accept_receipts", ("accepted_result",), ("object",), ("jsonb_typeof", "accepted_result", "='object'")),
    "admin_demo_cases_brand_id_check": CheckSpec("admin_demo_cases", ("brand_id",), (), ("brand_id", ">0")),
    "admin_demo_cases_diagnosis_id_check": CheckSpec("admin_demo_cases", ("diagnosis_id",), (), ("diagnosis_id", ">0")),
    "admin_demo_cases_status_check": CheckSpec("admin_demo_cases", ("status",), ("active", "retired"), ("status", "any", "array")),
    "admin_demo_cases_snapshot_version_check": CheckSpec("admin_demo_cases", ("snapshot_version",), (), ("snapshot_version", ">=1")),
    "admin_demo_cases_created_reason_check": CheckSpec("admin_demo_cases", ("created_reason",), (), ("char_length", "btrim", ">=2", "<=500")),
    "admin_demo_case_grants_grantee_kind_check": CheckSpec("admin_demo_case_grants", ("grantee_kind",), ("user", "organization"), ("grantee_kind", "any", "array")),
    "admin_demo_case_grants_brand_id_check": CheckSpec("admin_demo_case_grants", ("brand_id",), (), ("brand_id", ">0")),
    "admin_demo_case_grants_capability_check": CheckSpec("admin_demo_case_grants", ("capability",), ("demo.customer.preview",), ("capability", "=")),
    "admin_demo_case_grants_status_check": CheckSpec("admin_demo_case_grants", ("status",), ("active", "revoked", "expired"), ("status", "any", "array")),
    "admin_demo_case_grants_version_check": CheckSpec("admin_demo_case_grants", ("version",), (), ("version", ">=1")),
    "admin_demo_case_grants_created_reason_check": CheckSpec("admin_demo_case_grants", ("created_reason",), (), ("char_length", "btrim", ">=2", "<=500")),
    "admin_demo_case_grants_note_check": CheckSpec("admin_demo_case_grants", ("note",), (), ("char_length", "note", "<=500")),
    "admin_demo_case_grants_request_payload_hash_check": CheckSpec("admin_demo_case_grants", ("request_payload_hash",), ("^[0-9a-f]{64}$",), ("request_payload_hash", "~")),
    "admin_demo_case_grants_grantee_shape_check": CheckSpec("admin_demo_case_grants", ("grantee_kind", "grantee_user_id", "grantee_organization_id"), ("user", "organization"), ("isnotnull", "isnull", "or")),
    "admin_demo_case_grants_window_check": CheckSpec("admin_demo_case_grants", ("valid_from", "expires_at"), (), ("expires_at", ">valid_from")),
    "admin_demo_case_grants_revocation_shape_check": CheckSpec("admin_demo_case_grants", ("status", "revoked_by_user_id", "revoked_reason", "revoked_at", "expired_at"), ("active", "revoked", "expired"), ("char_length", ">=2", "<=500", "or")),
    "admin_demo_access_events_brand_id_check": CheckSpec("admin_demo_access_events", ("brand_id",), (), ("brand_id", ">0")),
    "admin_demo_access_events_access_mode_check": CheckSpec("admin_demo_access_events", ("access_mode",), ("demo",), ("access_mode", "=")),
    "admin_demo_access_events_action_check": CheckSpec("admin_demo_access_events", ("action",), ("",), ("btrim", "action", "<>''")),
    "admin_demo_access_events_request_id_check": CheckSpec("admin_demo_access_events", ("request_id",), ("",), ("btrim", "request_id", "<>''")),
    "admin_provider_downgrade_plans_strategy_check": CheckSpec("admin_provider_downgrade_plans", ("strategy",), ("transfer_upstream", "platform_managed", "settle_then_downgrade"), ("strategy", "any", "array")),
    "admin_provider_downgrade_plans_status_check": CheckSpec("admin_provider_downgrade_plans", ("status",), ("draft", "blocked", "ready", "completed", "cancelled"), ("status", "any", "array")),
    "admin_provider_downgrade_plans_expected_identity_version_check": CheckSpec("admin_provider_downgrade_plans", ("expected_identity_version",), (), ("expected_identity_version", ">=1")),
    "admin_provider_downgrade_plans_version_check": CheckSpec("admin_provider_downgrade_plans", ("version",), (), ("version", ">=1")),
    "admin_provider_downgrade_plans_created_reason_check": CheckSpec("admin_provider_downgrade_plans", ("created_reason",), (), ("char_length", "btrim", ">=2", "<=500")),
    "admin_provider_downgrade_plans_target_check": CheckSpec("admin_provider_downgrade_plans", ("provider_user_id", "strategy", "target_provider_user_id"), ("transfer_upstream", "platform_managed", "settle_then_downgrade"), ("isnotnull", "isnull", "<>provider_user_id", "or")),
    "admin_provider_downgrade_plans_completion_shape_check": CheckSpec("admin_provider_downgrade_plans", ("status", "confirmed_by_user_id", "confirmed_reason", "confirmed_at"), ("completed",), ("isnotnull", "char_length", ">=2", "<=500", "or")),
}


@dataclass(frozen=True)
class IndexSpec:
    table: str
    unique: bool
    keys: tuple[str, ...]
    predicate: str = ""
    options: tuple[int, ...] = ()


INDEX_CONTRACTS = {
    "idx_admin_cross_tenant_audits_subject": IndexSpec("admin_cross_tenant_audits", False, ("subject_kind", "subject_id", "created_at", "id"), options=(0, 0, 3, 3)),
    "idx_admin_cross_tenant_audits_actor": IndexSpec("admin_cross_tenant_audits", False, ("actor_user_id", "created_at", "id"), options=(0, 3, 3)),
    "idx_admin_demo_cases_brand": IndexSpec("admin_demo_cases", False, ("brand_id", "status", "frozen_at"), options=(0, 0, 3)),
    "ux_admin_demo_case_grants_active_user": IndexSpec("admin_demo_case_grants", True, ("grantee_user_id", "case_id", "capability"), "status='active'andgrantee_kind='user'"),
    "ux_admin_demo_case_grants_active_org": IndexSpec("admin_demo_case_grants", True, ("grantee_organization_id", "case_id", "capability"), "status='active'andgrantee_kind='organization'"),
    "idx_admin_demo_case_grants_case": IndexSpec("admin_demo_case_grants", False, ("case_id", "status", "expires_at", "id")),
    "idx_admin_demo_case_grants_user_live": IndexSpec("admin_demo_case_grants", False, ("grantee_user_id", "expires_at", "id"), "status='active'andgrantee_kind='user'"),
    "idx_admin_demo_case_grants_org_live": IndexSpec("admin_demo_case_grants", False, ("grantee_organization_id", "expires_at", "id"), "status='active'andgrantee_kind='organization'"),
    "idx_admin_demo_access_events_grant": IndexSpec("admin_demo_access_events", False, ("grant_id", "created_at", "id"), options=(0, 3, 3)),
    "idx_admin_demo_access_events_viewer": IndexSpec("admin_demo_access_events", False, ("viewer_user_id", "created_at", "id"), options=(0, 3, 3)),
    "ux_admin_provider_downgrade_plans_live": IndexSpec("admin_provider_downgrade_plans", True, ("provider_user_id",), "status=anyarray['draft','blocked','ready']"),
    "idx_admin_provider_downgrade_plans_provider": IndexSpec("admin_provider_downgrade_plans", False, ("provider_user_id", "created_at", "id"), options=(0, 3, 3)),
}


def _dict_rows(cur):
    rows = cur.fetchall()
    if not rows or isinstance(rows[0], dict):
        return rows
    names = [getattr(item, "name", None) or item[0] for item in cur.description]
    return [dict(zip(names, row)) for row in rows]


def _canonical(value: object) -> str:
    text = str(value or "").lower()
    text = text.replace('"', "").replace("public.", "")
    text = re.sub(r"::(?:text|bigint|integer|jsonb|uuid|timestamp with time zone)", "", text)
    text = re.sub(r"[\s()]", "", text)
    return text


def _default_signature(value: object, expected: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if expected and expected.startswith("sequence:"):
        sequence = expected.split(":", 1)[1]
        return expected if "nextval(" in text.lower() and sequence in text else _canonical(text)
    return _canonical(text)


def _constraint_columns(cur, oid: int, table_oid: int, key_field: str) -> tuple[str, ...]:
    cur.execute(
        f"""SELECT a.attname FROM unnest((SELECT {key_field} FROM pg_constraint WHERE oid=%s))
              WITH ORDINALITY AS k(attnum,ord)
              JOIN pg_attribute a ON a.attrelid=%s AND a.attnum=k.attnum
              ORDER BY k.ord""",
        (oid, table_oid),
    )
    return tuple(str(row["attname"]) for row in _dict_rows(cur))


def verify_admin_cross_tenant_schema(cur) -> None:
    errors: list[str] = []
    table_oids: dict[str, int] = {}
    for table, columns in COLUMN_CONTRACTS.items():
        cur.execute(
            """SELECT c.oid,c.relkind,n.nspname FROM pg_class c
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname=%s""",
            (table,),
        )
        relation_rows = _dict_rows(cur)
        relation = relation_rows[0] if relation_rows else None
        if not relation:
            errors.append(f"public.{table}: missing table")
            continue
        if relation["relkind"] != "r":
            errors.append(f"public.{table}: relation kind must be table")
            continue
        table_oids[table] = int(relation["oid"])
        cur.execute(
            """SELECT a.attname,format_type(a.atttypid,a.atttypmod) AS pg_type,
                      a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_expr
               FROM pg_attribute a
               LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
               WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped""",
            (table_oids[table],),
        )
        actual = {row["attname"]: row for row in _dict_rows(cur)}
        for name, spec in columns.items():
            row = actual.get(name)
            if not row:
                errors.append(f"public.{table}.{name}: missing column")
                continue
            if row["pg_type"] != spec.pg_type:
                errors.append(f"public.{table}.{name}: type {row['pg_type']} != {spec.pg_type}")
            if bool(row["attnotnull"]) != spec.not_null:
                errors.append(f"public.{table}.{name}: nullability mismatch")
            actual_default = _default_signature(row.get("default_expr"), spec.default)
            expected_default = (
                spec.default if spec.default and spec.default.startswith("sequence:")
                else (None if spec.default is None else _canonical(spec.default))
            )
            if actual_default != expected_default:
                errors.append(f"public.{table}.{name}: default mismatch")

    cur.execute(
        """SELECT c.oid,c.conname,c.contype,c.convalidated,c.conrelid,c.confrelid,
                  c.confupdtype,c.confdeltype,n.nspname,t.relname,
                  rn.nspname AS ref_schema,rt.relname AS ref_table,
                  pg_get_constraintdef(c.oid,TRUE) AS definition
           FROM pg_constraint c
           JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
           LEFT JOIN pg_class rt ON rt.oid=c.confrelid LEFT JOIN pg_namespace rn ON rn.oid=rt.relnamespace
           WHERE n.nspname='public' AND c.conname=ANY(%s)""",
        (list(KEY_CONTRACTS) + list(CHECK_CONTRACTS),),
    )
    constraint_rows = {row["conname"]: row for row in _dict_rows(cur)}
    for name, spec in KEY_CONTRACTS.items():
        row = constraint_rows.get(name)
        if not row:
            errors.append(f"missing constraint public.{name}")
            continue
        if row["relname"] != spec.table or row["contype"] != spec.kind or row["convalidated"] is not True:
            errors.append(f"{name}: wrong table/type or not validated")
            continue
        columns = _constraint_columns(cur, int(row["oid"]), int(row["conrelid"]), "conkey")
        if columns != spec.columns:
            errors.append(f"{name}: columns {columns} != {spec.columns}")
        if spec.kind == "f":
            ref_columns = _constraint_columns(cur, int(row["oid"]), int(row["confrelid"]), "confkey")
            if row.get("ref_schema") != "public" or row.get("ref_table") != spec.ref_table:
                errors.append(f"{name}: referenced table mismatch")
            if ref_columns != spec.ref_columns:
                errors.append(f"{name}: referenced columns mismatch")
            if row["confupdtype"] != spec.update_action or row["confdeltype"] != spec.delete_action:
                errors.append(f"{name}: FK action mismatch")

    for name, spec in CHECK_CONTRACTS.items():
        row = constraint_rows.get(name)
        if not row:
            errors.append(f"missing check public.{name}")
            continue
        if row["relname"] != spec.table or row["contype"] != "c" or row["convalidated"] is not True:
            errors.append(f"{name}: wrong table/type or not validated")
            continue
        columns = _constraint_columns(cur, int(row["oid"]), int(row["conrelid"]), "conkey")
        if set(columns) != set(spec.columns):
            errors.append(f"{name}: check columns {columns} != {spec.columns}")
        definition = str(row["definition"])
        canonical = _canonical(definition)
        literals = tuple(re.findall(r"'([^']*)'", re.sub(r"::text", "", definition.lower())))
        if tuple(sorted(literals)) != tuple(sorted(spec.literals)):
            errors.append(f"{name}: literal contract mismatch")
        for token in spec.required:
            if _canonical(token) not in canonical:
                errors.append(f"{name}: expression contract missing {token}")
        if any(marker in canonical for marker in ("true", "1=1", "0=0")):
            errors.append(f"{name}: tautological check forbidden")

    cur.execute(
        """SELECT i.indexrelid,i.indrelid,i.indisunique,i.indisvalid,i.indisready,i.indislive,
                  ci.relname AS index_name,ct.relname AS table_name,nt.nspname AS table_schema,
                   pg_get_expr(i.indpred,i.indrelid) AS predicate,i.indnkeyatts,
                   i.indoption::int2[] AS indoptions
           FROM pg_index i JOIN pg_class ci ON ci.oid=i.indexrelid
           JOIN pg_namespace ni ON ni.oid=ci.relnamespace
           JOIN pg_class ct ON ct.oid=i.indrelid JOIN pg_namespace nt ON nt.oid=ct.relnamespace
           WHERE ni.nspname='public' AND ci.relname=ANY(%s)""",
        (list(INDEX_CONTRACTS),),
    )
    indexes = {row["index_name"]: row for row in _dict_rows(cur)}
    for name, spec in INDEX_CONTRACTS.items():
        row = indexes.get(name)
        if not row:
            errors.append(f"missing index public.{name}")
            continue
        if row["table_schema"] != "public" or row["table_name"] != spec.table:
            errors.append(f"{name}: wrong table/schema")
        if bool(row["indisunique"]) != spec.unique:
            errors.append(f"{name}: uniqueness mismatch")
        if not (row["indisvalid"] and row["indisready"] and row["indislive"]):
            errors.append(f"{name}: index not valid/ready/live")
        keys: list[str] = []
        for position in range(1, int(row["indnkeyatts"]) + 1):
            cur.execute("SELECT pg_get_indexdef(%s,%s,TRUE) AS key", (int(row["indexrelid"]), position))
            key_rows = _dict_rows(cur)
            keys.append(_canonical(key_rows[0]["key"]))
        if tuple(keys) != spec.keys:
            errors.append(f"{name}: keys {tuple(keys)} != {spec.keys}")
        options = tuple(int(value) for value in (row.get("indoptions") or []))
        expected_options = spec.options or tuple(0 for _ in spec.keys)
        if options != expected_options:
            errors.append(f"{name}: key options {options} != {expected_options}")
        if _canonical(row.get("predicate")) != _canonical(spec.predicate):
            errors.append(f"{name}: predicate mismatch")

    if errors:
        raise RuntimeError("admin cross-tenant governance schema incomplete: " + "; ".join(errors))
