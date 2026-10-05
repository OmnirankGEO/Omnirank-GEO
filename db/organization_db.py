"""Organization schema readiness and fail-closed identity resolution."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from db.connection import get_db
from services.organization_contract import (
    CONTRACT_FREEZE_SHA,
    IdentityContext,
    OrganizationError,
    SCHEMA_VERSION,
    feature_flags,
)
from services.organization_schema_contract import catalog_fingerprint


EXPECTED_TABLES = frozenset(
    {
        "organizations",
        "organization_schema_migrations",
        "organization_seat_entitlements",
        "organization_roles",
        "organization_memberships",
        "organization_invites",
        "organization_invite_accept_receipts",
        "organization_security_rate_events",
        "organization_role_capabilities",
        "organization_member_capability_overrides",
        "organization_brand_assignments",
        "organization_artifact_shares",
        "organization_spend_limits",
        "organization_automatic_plans",
        "organization_plan_occurrences",
        "organization_charge_links",
        "organization_charge_limit_links",
        "organization_approval_policies",
        "organization_approval_requests",
        "organization_work_outbox",
        "organization_audit_events",
        "organization_product_config_publications",
        "organization_member_legacy_access_snapshots",
        "organization_operator_accounts",
        "organization_invite_verification_challenges",
        "organization_invite_delivery_outbox",
        "organization_payer_policies",
        "organization_payer_policy_events",
    }
)

EXPECTED_COLUMN_TYPES = {
    ("organizations", "id"): "bigint",
    ("organizations", "owner_user_id"): "integer",
    ("organization_memberships", "user_id"): "integer",
    ("organization_memberships", "organization_id"): "bigint",
    ("organization_invites", "target_hmac"): "text",
    ("organization_invites", "token_hash"): "text",
    ("organization_security_rate_events", "source_ip_hmac"): "text",
    ("organization_charge_links", "reserved_ceiling_points"): "bigint",
    ("organization_charge_links", "status"): "text",
    ("organization_spend_limits", "period_start"): "timestamp with time zone",
    ("organization_approval_requests", "payload_hash"): "text",
    ("organization_approval_requests", "feature_code"): "text",
    ("organization_approval_policies", "role_id"): "bigint",
    ("organization_approval_policies", "min_points"): "bigint",
    ("organization_approval_policies", "max_points"): "bigint",
    ("organization_invites", "token_derivation_id"): "uuid",
    ("organization_product_config_publications", "operational"): "boolean",
    ("organization_member_legacy_access_snapshots", "brand_ids"): "jsonb",
    ("organization_operator_accounts", "principal_user_id"): "integer",
    ("organization_invite_verification_challenges", "receipt_derivation_id"): "uuid",
    ("organization_invite_delivery_outbox", "status"): "text",
    ("organization_payer_policies", "organization_id"): "bigint",
    ("organization_payer_policies", "shared_payer_enabled"): "boolean",
    ("organization_payer_policies", "overage_enabled"): "boolean",
    ("organization_payer_policies", "per_action_limit_points"): "bigint",
    ("organization_payer_policies", "policy_version"): "integer",
    ("organization_payer_policy_events", "actor_kind"): "text",
    ("organization_payer_policy_events", "new_snapshot"): "jsonb",
    ("organization_charge_links", "payer_policy_version"): "integer",
    ("organization_charge_links", "owner_consent_snapshot"): "jsonb",
    ("organization_charge_links", "within_limit_points"): "bigint",
    ("organization_charge_links", "overage_points"): "bigint",
}


def _scalar(row: Any, key: str = "value") -> Any:
    if row is None:
        return None
    if hasattr(row, "get"):
        return row.get(key)
    return row[0]


def readiness(*, cursor=None) -> dict[str, Any]:
    """Validate actual object kind, current schema, constraints, and indexes.

    ``to_regclass`` alone is deliberately insufficient: a view or same-named
    object in another schema must not satisfy readiness.
    """

    def inspect(cur) -> dict[str, Any]:
        cur.execute(
            """
            SELECT c.relname, c.relkind
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relname = ANY(%s)
            """,
            (list(EXPECTED_TABLES),),
        )
        actual = {row["relname"]: row["relkind"] for row in cur.fetchall()}
        missing_tables = sorted(EXPECTED_TABLES - actual.keys())
        wrong_kinds = sorted(name for name, kind in actual.items() if kind not in {"r", "p"})

        cur.execute(
            """
            SELECT table_name,column_name,data_type
            FROM information_schema.columns
            WHERE table_schema=current_schema()
              AND (table_name,column_name) IN (
                SELECT * FROM unnest(%s::text[],%s::text[])
              )
            """,
            (
                [table for table, _ in EXPECTED_COLUMN_TYPES],
                [column for _, column in EXPECTED_COLUMN_TYPES],
            ),
        )
        actual_columns = {(row["table_name"], row["column_name"]): row["data_type"] for row in cur.fetchall()}
        missing_columns = sorted(f"{table}.{column}" for table, column in EXPECTED_COLUMN_TYPES if (table, column) not in actual_columns)
        wrong_column_types = sorted(
            f"{table}.{column}:{actual_columns[(table, column)]}!={expected}"
            for (table, column), expected in EXPECTED_COLUMN_TYPES.items()
            if (table, column) in actual_columns and actual_columns[(table, column)] != expected
        )

        # Full PostgreSQL 16 catalog fingerprint. This is an exact comparison,
        # not a substring/fragment heuristic: every frozen column, constraint and
        # index is bound with types/null/defaults, FK targets, validation,
        # ordered expressions, predicates, uniqueness and opclasses.
        schema_contract = catalog_fingerprint(cur)
        missing_constraints: list[str] = []
        missing_indexes: list[str] = []

        migration = None
        if actual.get("organization_schema_migrations") in {"r", "p"}:
            cur.execute(
                """
                SELECT contract_freeze_sha, status
                FROM organization_schema_migrations WHERE version=%s
                """,
                (SCHEMA_VERSION,),
            )
            migration = cur.fetchone()
        migration_ok = bool(
            migration
            and migration["status"] == "applied"
            and migration["contract_freeze_sha"] == CONTRACT_FREEZE_SHA
        )
        payer_policy = _payer_policy_readiness(cur, actual)
        ok = (
            not (missing_tables or wrong_kinds or missing_columns or wrong_column_types)
            and migration_ok
            and schema_contract["matches"]
            and payer_policy["ready"]
        )
        return {
            "ready": ok,
            "schema": _current_schema(cur),
            "missing_tables": missing_tables,
            "wrong_object_kinds": wrong_kinds,
            "missing_columns": missing_columns,
            "wrong_column_types": wrong_column_types,
            "missing_constraints": missing_constraints,
            "missing_indexes": missing_indexes,
            "schema_contract": schema_contract,
            "migration_ok": migration_ok,
            "payer_policy": payer_policy,
            "schema_version": SCHEMA_VERSION,
            "contract_freeze_sha": CONTRACT_FREEZE_SHA,
        }

    if cursor is not None:
        return inspect(cursor)
    try:
        with get_db() as conn:
            return inspect(conn.cursor())
    except Exception as exc:
        return {
            "ready": False,
            "schema": None,
            "missing_tables": sorted(EXPECTED_TABLES),
            "wrong_object_kinds": [],
            "missing_columns": sorted(f"{table}.{column}" for table, column in EXPECTED_COLUMN_TYPES),
            "wrong_column_types": [],
            "missing_constraints": [],
            "missing_indexes": [],
            "schema_contract": {
                "matches": False,
                "actual_fingerprint": None,
                "actual_counts": {"columns": 0, "constraints": 0, "indexes": 0},
            },
            "migration_ok": False,
            "payer_policy": {"ready": False, "error_type": type(exc).__name__},
            "schema_version": SCHEMA_VERSION,
            "contract_freeze_sha": CONTRACT_FREEZE_SHA,
            "error_type": type(exc).__name__,
        }


def _payer_policy_readiness(cursor, actual: dict[str, str]) -> dict[str, Any]:
    """Payer-policy schema validation plus flag/policy layering surface.

    Layering invariant: the global ORGANIZATION_SHARED_PAYER_ENABLED flag and
    the owner policy are independent layers; member payer work is effective
    only when both allow it. Readiness asserts the schema can never hold an
    enabled policy with missing hard caps, and reports both layers so the
    startup gate fails closed on any drift.
    """
    flags = feature_flags()
    base = {
        "ready": True,
        "global_shared_payer_flag": bool(flags["ORGANIZATION_SHARED_PAYER_ENABLED"]),
        "enabled_policy_count": 0,
        "invalid_enabled_policies": 0,
        "effective_layering": "global_flag AND owner_policy",
    }
    if actual.get("organization_payer_policies") not in {"r", "p"}:
        return {**base, "ready": False, "error": "organization_payer_policies missing"}
    cursor.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE shared_payer_enabled OR overage_enabled) AS enabled_count,
          COUNT(*) FILTER (WHERE (shared_payer_enabled OR overage_enabled) AND (
            per_action_limit_points IS NULL OR daily_limit_points IS NULL OR monthly_limit_points IS NULL
            OR per_action_limit_points <= 0 OR daily_limit_points <= 0 OR monthly_limit_points <= 0
          )) AS invalid_enabled
        FROM organization_payer_policies
        """
    )
    row = cursor.fetchone()
    enabled = int(row["enabled_count"])
    invalid = int(row["invalid_enabled"])
    return {
        **base,
        "ready": invalid == 0,
        "enabled_policy_count": enabled,
        "invalid_enabled_policies": invalid,
    }


def _current_schema(cursor) -> Optional[str]:
    cursor.execute("SELECT current_schema() AS value")
    return _scalar(cursor.fetchone())


def assert_ready(cursor) -> None:
    result = readiness(cursor=cursor)
    if not result["ready"]:
        raise OrganizationError(
            "ORG_SCHEMA_NOT_READY",
            "组织席位数据库尚未就绪",
            http_status=503,
            safe_details={key: value for key, value in result.items() if key not in {"schema"}},
        )


def resolve_identity(
    authenticated_user_id: int,
    *,
    request_id: Optional[str] = None,
    cursor=None,
) -> Optional[IdentityContext]:
    """Resolve payer/principal exclusively from server-side membership rows."""

    def resolve(cur) -> Optional[IdentityContext]:
        cur.execute(
            """
            SELECT m.id AS membership_id, m.user_id, m.status AS membership_status,
                   m.is_owner, m.version AS membership_version,
                   m.capability_version, m.assignment_version,
                   o.id AS organization_id, o.owner_user_id, o.status AS organization_status,
                   o.version AS organization_version, o.authority_version,
                   r.id AS role_id
            FROM organization_memberships m
            JOIN organizations o ON o.id=m.organization_id
            JOIN organization_roles r ON r.id=m.role_id AND r.organization_id=o.id
            WHERE m.user_id=%s AND m.status IN ('active','suspended','leaving')
            ORDER BY m.id DESC LIMIT 2
            """,
            (int(authenticated_user_id),),
        )
        rows = list(cur.fetchall())
        if not rows:
            return None
        if len(rows) != 1:
            raise OrganizationError("ORG_MEMBERSHIP_AMBIGUOUS", "员工组织身份冲突", http_status=503)
        row = rows[0]
        if not bool(row["is_owner"]) and (
            row["organization_status"] != "active" or row["membership_status"] != "active"
        ):
            raise OrganizationError("ORG_MEMBERSHIP_INACTIVE", "组织席位当前不可用", http_status=403)
        if bool(row["is_owner"]) and row["membership_status"] not in {"active", "suspended"}:
            raise OrganizationError("ORG_MEMBERSHIP_INACTIVE", "老板组织身份当前不可用", http_status=403)

        cur.execute(
            """
            SELECT capability,effect,0 AS precedence
            FROM organization_role_capabilities WHERE role_id=%s
            UNION ALL
            SELECT capability,effect,1 AS precedence
            FROM organization_member_capability_overrides WHERE membership_id=%s
            ORDER BY capability,precedence
            """,
            (row["role_id"], row["membership_id"]),
        )
        effects: dict[str, str] = {}
        for item in cur.fetchall():
            effects[item["capability"]] = item["effect"]
        capabilities = frozenset(name for name, effect in effects.items() if effect == "allow")
        actor_kind = "owner" if row["is_owner"] else "member"
        return IdentityContext(
            request_id=request_id or str(uuid.uuid4()),
            authenticated_user_id=int(authenticated_user_id),
            principal_user_id=int(row["owner_user_id"]),
            payer_user_id=int(row["owner_user_id"]),
            actor_kind=actor_kind,
            organization_id=int(row["organization_id"]),
            organization_status=str(row["organization_status"]),
            membership_status=str(row["membership_status"]),
            actor_user_id=int(authenticated_user_id),
            membership_id=int(row["membership_id"]),
            role_id=int(row["role_id"]),
            membership_version=int(row["membership_version"]),
            capability_version=int(row["capability_version"]),
            assignment_version=int(row["assignment_version"]),
            organization_version=int(row["organization_version"]),
            organization_authority_version=int(row["authority_version"]),
            capabilities=capabilities,
        )

    if cursor is not None:
        return resolve(cursor)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            assert_ready(cur)
            return resolve(cur)
    except OrganizationError:
        raise
    except Exception as exc:
        raise OrganizationError("ORG_IDENTITY_LOOKUP_FAILED", "组织身份解析失败", http_status=503) from exc


def assigned_brand_ids(identity: IdentityContext, *, cursor=None) -> list[int]:
    if identity.is_owner:
        sql = """
            SELECT id FROM brands
            WHERE owner_user_id=%s AND (is_deleted IS NULL OR is_deleted=FALSE)
            ORDER BY id
        """
        params = (identity.principal_user_id,)
    else:
        sql = """
            SELECT a.brand_id
            FROM organization_brand_assignments a
            JOIN brands b ON b.id=a.brand_id
            WHERE a.organization_id=%s AND a.membership_id=%s AND a.status='active'
              AND b.owner_user_id=%s AND (b.is_deleted IS NULL OR b.is_deleted=FALSE)
            ORDER BY a.brand_id
        """
        params = (identity.organization_id, identity.membership_id, identity.principal_user_id)

    def query(cur) -> list[int]:
        cur.execute(sql, params)
        return [int(row.get("id", row.get("brand_id"))) for row in cur.fetchall()]

    if cursor is not None:
        return query(cursor)
    with get_db() as conn:
        return query(conn.cursor())
