"""Destructive-only-to-throwaway PostgreSQL 16 verification for organization seats.

Run through ``scripts/verify_organization_internal_seats.ps1``.  The script
creates its own tmpfs-backed container, and this module creates unique schemas;
it never accepts a production-looking DSN and never sends external messages.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import sys
import re
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2 import sql
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# [R5 ⑤ 2026-08-20] brands 走生产 SSOT 出口(手搓版 7 列 vs 生产 32 列)。
#   本目录的夹具是**一整段 SQL 字符串**(还要被 .replace() 做 legacy / production_upgrade
#   变体),没有 cursor 可交,所以用 SSOT 的纯 SQL 形态拼在最前面 —— 常量同一份,
#   两种发射方式同形由 tests/db_bootstrap_r5 的 test_sql_emission_matches_the_cursor_emission 钉死。
#   顺带丢掉手搓版的 `owner_user_id NOT NULL REFERENCES users(id)`:生产实查该列是
#   integer / nullable / 无 FK,手搓版比生产**严**(另一种假绿)。
from db.brands_schema import brands_schema_sql  # noqa: E402  (须在 sys.path 注入之后)

BASE_SQL = brands_schema_sql() + (
    Path(__file__).with_name("base_fixture.sql")).read_text(encoding="utf-8")
MIGRATION_SQL = (ROOT / "scripts" / "migration_organization_internal_seats_2026_07_20.sql").read_text(encoding="utf-8")
ONBOARDING_MIGRATION_SQL = (
    ROOT / "scripts" / "migration_organization_all_accounts_onboarding_2026_07_22.sql"
).read_text(encoding="utf-8")
CROSS_TENANT_MIGRATION_SQL = (
    ROOT / "scripts" / "migration_admin_cross_tenant_governance_2026_07_21.sql"
).read_text(encoding="utf-8")
PAYER_MIGRATION_SQL = (
    ROOT / "scripts" / "migration_organization_payer_policies_2026_07_23.sql"
).read_text(encoding="utf-8")


def enable_payer_policy_seed(dsn: str, organization_id: int, *, owner_user_id: int = 1) -> None:
    """Test-only seed: explicit owner consent so legacy member-billing flows run.

    The migration itself never enables payer policies; verification fixtures
    opt in exactly one organization with generous hard caps.
    """
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO organization_payer_policies(
              organization_id,shared_payer_enabled,overage_enabled,
              per_action_limit_points,daily_limit_points,monthly_limit_points,
              policy_version,updated_by_owner_user_id,enabled_at,reason
            ) VALUES (%s,TRUE,TRUE,100000000,100000000,100000000,1,%s,NOW(),
                      'verification seed: owner consented shared wallet')
            ON CONFLICT(organization_id) DO NOTHING
            """,
            (int(organization_id), int(owner_user_id)),
        )


class Verification:
    def __init__(self) -> None:
        self.passed: list[str] = []

    def check(self, condition: object, name: str, details: object = None) -> None:
        if not condition:
            raise AssertionError(f"{name}: {details!r}")
        self.passed.append(name)
        print(json.dumps({"test": name, "status": "passed"}, ensure_ascii=False), flush=True)


def schema_dsn(dsn: str, schema: str) -> str:
    parsed = urlsplit(dsn)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def connect(dsn: str, *, autocommit: bool = False):
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = autocommit
    with conn.cursor() as cursor:
        cursor.execute("SET lock_timeout='5s'; SET statement_timeout='30s'")
    return conn


def create_schema(dsn: str, prefix: str) -> tuple[str, str]:
    schema = f"{prefix}_{secrets.token_hex(5)}"
    with connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
    return schema, schema_dsn(dsn, schema)


def execute_sql(dsn: str, sql: str) -> None:
    with connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)


def isolate_public_qualified_sql(sql_text: str, schema: str) -> str:
    """Retarget a production-public migration into this runner's throwaway schema."""
    if not re.fullmatch(r"[a-z0-9_]+", schema):
        raise RuntimeError("unsafe verification schema name")
    return sql_text.replace("public.", f'"{schema}".')


def install_public_charge_link_test_view(dsn: str, schema: str) -> None:
    """Point the production-qualified sweeper read at this throwaway schema."""
    if not re.fullmatch(r"[a-z0-9_]+", schema):
        raise RuntimeError("unsafe verification schema name")
    with connect(dsn, autocommit=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT c.relkind
               FROM pg_catalog.pg_class c
               JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname='organization_charge_links'"""
        )
        relation = cursor.fetchone()
        if relation and relation["relkind"] != "v":
            raise RuntimeError(
                "verification refuses to replace non-view public.organization_charge_links"
            )
        cursor.execute(
            sql.SQL("CREATE OR REPLACE VIEW public.organization_charge_links AS SELECT * FROM {}.organization_charge_links")
            .format(sql.Identifier(schema))
        )


def force_plan_due(dsn: str, plan_id: int) -> None:
    """Use the database clock so scheduler tests cannot race host clock skew."""
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_automatic_plans SET next_occurrence_at=NOW()+INTERVAL '1 hour' WHERE status='active' AND id<>%s",
            (int(plan_id),),
        )
        cursor.execute(
            """
            UPDATE organization_plan_occurrences x
            SET next_attempt_at=NOW()+INTERVAL '1 hour'
            FROM organization_automatic_plans p
            WHERE p.id=x.plan_id AND p.status='active' AND p.id<>%s
              AND x.status='retry_wait' AND x.charge_link_id IS NULL
            """,
            (int(plan_id),),
        )
        cursor.execute(
            "UPDATE organization_automatic_plans SET next_occurrence_at=NOW()-INTERVAL '1 second' WHERE id=%s",
            (int(plan_id),),
        )


def seed_runtime(dsn: str) -> None:
    metadata = json.dumps(
        {
            "included_seats": 3,
            "extra_seat_price_cents": 0,
            "high_cost_approval_threshold_points": 1000,
            "invite_ttl_hours": 24,
            "approval_ttl_hours": 24,
            "verification_ttl_minutes": 10,
            "verification_max_attempts": 5,
            "operational": True,
            "paid_extra_seats_enabled": False,
            "rate_limits": {
                "invite.create": {
                    "window_seconds": 3600,
                    "organization": 100,
                    "actor": 100,
                    "target": 3,
                    "ip": 1000,
                },
                "invite.resend": {
                    "window_seconds": 3600,
                    "organization": 100,
                    "actor": 100,
                    "target": 5,
                    "ip": 1000,
                },
                "invite.accept": {
                    "window_seconds": 3600,
                    "organization": 200,
                    "actor": 10,
                    "target": 5,
                    "ip": 1000,
                },
                "invite.verify": {
                    "window_seconds": 3600,
                    "organization": 200,
                    "target": 20,
                    "ip": 1000,
                },
                "invite.delivery_status": {
                    "window_seconds": 3600,
                    "organization": 200,
                    "target": 60,
                    "ip": 1000,
                },
            },
        },
        ensure_ascii=False,
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO roles(name,display_name,is_system) VALUES ('user','普通用户',TRUE),('admin','管理员',TRUE)")
        users = []
        for user_id in range(1, 81):
            phone = f"139{user_id:08d}"
            users.append((user_id, f"user{user_id}", f"用户{user_id}", phone, user_id != 79))
        cursor.executemany(
            """
            INSERT INTO users(id,username,display_name,phone,phone_verified,email,email_verified,email_verified_for)
            VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s)
            """,
            [
                (
                    uid,
                    username,
                    display,
                    phone,
                    verified,
                    f"user{uid}@example.test",
                    f"user{uid}@example.test",
                )
                for uid, username, display, phone, verified in users
            ],
        )
        cursor.execute("SELECT setval(pg_get_serial_sequence('users','id'),100,TRUE)")
        cursor.execute("INSERT INTO user_roles(user_id,role_id) SELECT id,1 FROM users")
        cursor.execute("INSERT INTO user_roles(user_id,role_id) VALUES (80,2)")
        cursor.execute("INSERT INTO brands(id,owner_user_id,name) VALUES (10,1,'超长品牌名称用于员工席位隔离验证有限公司')")
        cursor.executemany(
            "INSERT INTO brands(id,owner_user_id,name) VALUES (%s,%s,%s)",
            [
                (70, 70, "旧账号本人品牌"),
                (71, 71, "旧账号合法服务客户品牌"),
                (72, 72, "跨租户诱饵品牌"),
            ],
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER,
                username TEXT,
                action TEXT NOT NULL,
                module TEXT,
                entity_type TEXT,
                entity_id INTEGER,
                summary TEXT,
                before_snapshot TEXT,
                after_snapshot TEXT,
                ip_address TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_agent_bindings (
                id BIGSERIAL PRIMARY KEY,
                customer_user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
                agent_user_id INTEGER NOT NULL REFERENCES users(id),
                binding_source TEXT NOT NULL DEFAULT 'admin_manual',
                dispute_status TEXT,
                bound_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO customer_agent_bindings(customer_user_id,agent_user_id,binding_source)
            VALUES (71,70,'admin_manual')
            """
        )
        cursor.execute(
            """
            INSERT INTO whitelabel_settings(
              user_id,company_name,logo_url,slogan,contact_name,contact_phone,
              whitelabel_mode,whitelabel_status,unlocked_by_admin
            ) VALUES (1,'老板白标品牌','/uploads/whitelabel-logos/opaque-owner/logo.png',
                      '老板品牌口号','老板联系人','13900000001','external_only','active',FALSE)
            """
        )
        cursor.execute("INSERT INTO user_wallets(user_id,paid_points,total_recharged,agent_level) VALUES (1,2000000,2000000,1)")
        cursor.execute(
            """
            UPDATE pricing_catalog_versions
            SET status='archived',effective_to=NOW(),updated_at=NOW()
            WHERE catalog_type='feature_consumption' AND scope_key='ORGANIZATION_SEATS'
              AND status='published' AND effective_to IS NULL
            """
        )
        cursor.execute(
            """
            INSERT INTO pricing_catalog_versions(catalog_type,scope_key,version_code,status,effective_from)
            VALUES ('feature_consumption','ORGANIZATION_SEATS','org-seat-test-v2','published',NOW())
            RETURNING id
            """
        )
        version_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO pricing_catalog_entries(version_id,product_code,final_price_cents,source_ref_jsonb)
            VALUES (%s,'organization_internal_seats',0,%s::jsonb)
            """,
            (version_id, metadata),
        )
        cursor.executemany(
            """
            INSERT INTO feature_pricing(feature_code,feature_name,cost_points,cost_compute,requires_paid_points)
            VALUES (%s,%s,%s,0,FALSE)
            """,
            [
                ("geo_diagnosis", "GEO诊断", 50),
                ("monitor_single", "单次监测", 30),
                ("article_gen", "文章生成", 60),
                ("quote_generate", "方案报价", 40),
                ("topic_gen", "标题生成", 35),
            ],
        )


def install_environment(dsn: str) -> None:
    os.environ["DATABASE_URL"] = dsn
    os.environ["PGOPTIONS"] = "-c lock_timeout=5s -c statement_timeout=30s"
    os.environ["ORGANIZATION_SEATS_ENABLED"] = "true"
    os.environ["ORGANIZATION_SHARED_PAYER_ENABLED"] = "true"
    os.environ["ORGANIZATION_EXTERNAL_ACTIONS_ENABLED"] = "false"
    encoded = base64.urlsafe_b64encode(b"organization-test-key-material!!").decode("ascii").rstrip("=")
    for name in (
        "ORGANIZATION_INVITE_HMAC_KEYS",
        "ORGANIZATION_INVITE_ENCRYPTION_KEYS",
        "ORGANIZATION_TOKEN_HMAC_KEYS",
    ):
        os.environ[name] = f"v1:{encoded}"
        os.environ[f"{name}_ACTIVE_VERSION"] = "v1"


def sql_value(dsn: str, sql: str, params=()):
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        row = cursor.fetchone()
        return next(iter(row.values())) if row else None


def assert_full_schema_mutation_rejected(
    verify: Verification,
    root_dsn: str,
    readiness_fn,
    *,
    name: str,
    mutation_sql: str,
) -> None:
    """Build a complete schema, apply one catalog decoy, and require red."""
    _, mutation_dsn = create_schema(root_dsn, f"org_contract_{name[:18]}")
    execute_sql(mutation_dsn, BASE_SQL)
    execute_sql(mutation_dsn, MIGRATION_SQL)
    execute_sql(mutation_dsn, ONBOARDING_MIGRATION_SQL)
    execute_sql(mutation_dsn, PAYER_MIGRATION_SQL)
    execute_sql(mutation_dsn, mutation_sql)
    with connect(mutation_dsn) as conn:
        result = readiness_fn(cursor=conn.cursor())
    verify.check(
        not result["ready"] and not result["schema_contract"]["matches"],
        f"schema_contract_rejects_{name}",
        result,
    )


def source_route_census() -> set[tuple[str, str]]:
    """Collect static FastAPI decorator routes without importing the app."""
    routes: set[tuple[str, str]] = set()
    method_names = {"get", "post", "put", "patch", "delete", "options", "head"}
    source_files = [ROOT / "server.py", *(ROOT / "api").glob("*.py")]
    for source_file in source_files:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        prefixes: dict[str, str] = {"app": ""}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "APIRouter":
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target = targets[0] if targets else None
            if not isinstance(target, ast.Name):
                continue
            prefix = ""
            for keyword in value.keywords:
                if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    prefix = keyword.value.value
            prefixes[target.id] = prefix
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr not in method_names or not isinstance(decorator.func.value, ast.Name):
                    continue
                router_name = decorator.func.value.id
                if router_name not in prefixes or not decorator.args:
                    continue
                path_node = decorator.args[0]
                if isinstance(path_node, ast.Constant) and isinstance(path_node.value, str):
                    routes.add((decorator.func.attr.upper(), prefixes[router_name] + path_node.value))
    return routes


async def run_async_calls(callables):
    return await asyncio.gather(*(callable_() for callable_ in callables))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    args = parser.parse_args()
    if (urlsplit(args.dsn).hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("verification only accepts a loopback throwaway PostgreSQL DSN")
    verify = Verification()

    # Production before the organization release already had users.email but
    # did not necessarily have auth_db's later email verification columns.
    # The prestart migration must be self-contained instead of relying on a
    # wider test fixture or an import-time ALTER from db.auth_db.
    legacy_schema, legacy_dsn = create_schema(args.dsn, "org_seats_legacy_users")
    legacy_base_sql = BASE_SQL.replace(
        "    email_verified BOOLEAN NOT NULL DEFAULT FALSE,\n",
        "",
    )
    execute_sql(legacy_dsn, legacy_base_sql)
    execute_sql(legacy_dsn, MIGRATION_SQL)
    execute_sql(legacy_dsn, ONBOARDING_MIGRATION_SQL)
    execute_sql(legacy_dsn, MIGRATION_SQL)
    execute_sql(legacy_dsn, ONBOARDING_MIGRATION_SQL)
    legacy_cross_tenant_sql = isolate_public_qualified_sql(CROSS_TENANT_MIGRATION_SQL, legacy_schema)
    execute_sql(legacy_dsn, legacy_cross_tenant_sql)
    execute_sql(legacy_dsn, legacy_cross_tenant_sql)
    legacy_email_columns = sql_value(
        legacy_dsn,
        """
        SELECT json_object_agg(column_name, json_build_object(
            'type', data_type,
            'nullable', is_nullable,
            'default', column_default
        ))
        FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='users'
          AND column_name IN ('email','email_verified','email_verified_for')
        """,
    )
    verify.check(
        set(legacy_email_columns or {}) == {"email", "email_verified", "email_verified_for"}
        and legacy_email_columns["email_verified"]["type"] == "boolean"
        and legacy_email_columns["email_verified"]["nullable"] == "NO"
        and "false" in str(legacy_email_columns["email_verified"]["default"]).lower(),
        "migration_repairs_production_users_without_email_verification_columns",
        legacy_email_columns,
    )

    schema, dsn = create_schema(args.dsn, "org_seats_verify")
    execute_sql(dsn, BASE_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    cross_tenant_sql = isolate_public_qualified_sql(CROSS_TENANT_MIGRATION_SQL, schema)
    execute_sql(dsn, cross_tenant_sql)
    execute_sql(dsn, cross_tenant_sql)
    execute_sql(dsn, PAYER_MIGRATION_SQL)
    execute_sql(dsn, PAYER_MIGRATION_SQL)
    install_public_charge_link_test_view(args.dsn, schema)
    seed_runtime(dsn)
    install_environment(dsn)

    # The production freeze sweeper deliberately pins charge protection to
    # public.  Expose the unique throwaway schema through a read-only view so
    # this isolated run exercises that exact query without creating a second
    # charge ledger or weakening the production qualification.
    with connect(dsn) as public_fixture_conn:
        public_fixture_cursor = public_fixture_conn.cursor()
        public_fixture_cursor.execute(
            sql.SQL("CREATE OR REPLACE VIEW public.organization_charge_links AS SELECT * FROM {}.organization_charge_links").format(sql.Identifier(schema))
        )

    # Imports occur only after DATABASE_URL points to the isolated schema.
    from db import connection as db_connection
    from db.organization_db import readiness, resolve_identity
    from middleware.billing import commit_freeze, freeze_points, release_freeze
    from services.organization_approvals import decide_approval, list_approvals, submit_approval
    from services.organization_artifacts import (
        cache_partition_key,
        isolation_cursor,
        issue_public_token,
        list_artifacts,
        parse_isolation_cursor,
        revoke_share,
        share_artifact_internal,
        stamp_artifact,
        validate_public_token,
    )
    from services.organization_billing import (
        billing_consistency,
        claim_live_charge,
        mark_external_side_effect_started,
        refund_charge,
        release_charge,
        reserve_charge,
        settle_charge,
    )
    from services.organization_contract import OrganizationError, feature_flags, require_feature_flag
    from services.admin_client_scope import (
        AdminClientScopeError,
        get_admin_client_scope,
        replace_admin_client_scope,
    )
    from services.organization_limits import configure_limit
    from services.organization_plans import create_plan, schedule_one_due_occurrence, set_plan_status
    from services.organization_route_contract import (
        MEMBER_GEO_ROUTE_POLICIES,
        match_member_geo_route,
    )
    from services.organization_service import (
        accept_invite,
        assign_brands,
        create_invite,
        create_organization,
        inspect_invite,
        list_invites,
        revoke_invite,
        set_member_overrides,
        set_member_status,
    )
    from services.freeze_sweeper import sweep_zombie_freezes
    from services.organization_worker import (
        claim_work,
        dispatch_one_automatic_work,
        recover_stale_work,
        start_work,
    )

    actual_routes = source_route_census()
    contracted_routes = {(policy.method, policy.path_template) for policy in MEMBER_GEO_ROUTE_POLICIES}
    canonical_actual_routes = {
        (method, re.sub(r"\{[^{}]+\}", "{}", path)) for method, path in actual_routes
    }
    missing_contract_routes = sorted(
        (method, path)
        for method, path in contracted_routes
        if (method, re.sub(r"\{[^{}]+\}", "{}", path)) not in canonical_actual_routes
    )
    verify.check(not missing_contract_routes, "route_contract_every_member_route_has_live_decorator", missing_contract_routes)
    verify.check(
        len(contracted_routes) == len(MEMBER_GEO_ROUTE_POLICIES),
        "route_contract_has_no_method_path_duplicates",
    )
    billable_contract = {
        (policy.method, policy.path_template, policy.billing_feature)
        for policy in MEMBER_GEO_ROUTE_POLICIES
        if policy.billing_feature
    }
    verify.check(
        billable_contract
        == {
            ("POST", "/api/diagnosis", "geo_diagnosis"),
            ("POST", "/api/diagnosis/start", "geo_diagnosis"),
            ("POST", "/api/diagnosis/{diagnosis_id}/generate-quote", "quote_generate"),
            ("POST", "/api/keyword-selection/{token}/generate-quote", "quote_generate"),
            ("POST", "/api/writing/generate-titles", "topic_gen"),
            ("POST", "/api/writing/start-articles", "article_gen"),
            ("POST", "/api/monitoring/run", "monitor_single"),
        },
        "route_contract_billable_core_matrix_is_exact",
        billable_contract,
    )
    verify.check(
        match_member_geo_route("POST", "/api/monitoring/run-stream") is None
        and match_member_geo_route("POST", "/api/monitoring/run/../config") is None
        and match_member_geo_route("GET", "/api/quotes/1/export") is None
        and match_member_geo_route("POST", "/api/reports/send") is None,
        "route_contract_unwired_external_and_lookalike_paths_fail_closed",
    )

    ready = readiness()
    verify.check(ready["ready"], "migration_fresh_twice_and_half_table_repair", ready)
    _, production_upgrade_dsn = create_schema(args.dsn, "org_seats_production_upgrade")
    production_upgrade_sql = BASE_SQL.replace(
        "CREATE TABLE publish_orders (id BIGSERIAL PRIMARY KEY);",
        "CREATE TABLE publish_orders (id BIGSERIAL PRIMARY KEY, brand_id INTEGER NOT NULL);",
    )
    execute_sql(production_upgrade_dsn, production_upgrade_sql)
    execute_sql(production_upgrade_dsn, MIGRATION_SQL)
    execute_sql(production_upgrade_dsn, ONBOARDING_MIGRATION_SQL)
    execute_sql(production_upgrade_dsn, PAYER_MIGRATION_SQL)
    execute_sql(production_upgrade_dsn, PAYER_MIGRATION_SQL)
    with connect(production_upgrade_dsn) as production_upgrade_conn:
        production_upgrade_ready = readiness(cursor=production_upgrade_conn.cursor())
    verify.check(
        production_upgrade_ready["ready"]
        and production_upgrade_ready["schema_contract"]["matched_variant"]
        == "production_fixture_reanchor_payer_policies_v1",
        "migration_upgrade_preserves_exact_production_actor_shape",
        production_upgrade_ready,
    )

    # Current application archive-preview SQL relies on this pre-existing
    # business column. Add it after the frozen organization catalog has laid
    # down its isolation columns so their signed ordinals remain unchanged.
    execute_sql(
        dsn,
        """
        ALTER TABLE articles ADD COLUMN IF NOT EXISTS quote_id BIGINT REFERENCES quotes(id);
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS is_deleted INTEGER DEFAULT 0;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS archived_with_brand_at TIMESTAMPTZ;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS status TEXT;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS status_before_archive TEXT;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS archive_reason TEXT;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS archived_by_user_id INTEGER;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS archived_with_brand_at TIMESTAMPTZ;
        ALTER TABLE keyword_selection_sessions ADD COLUMN IF NOT EXISTS quote_id INTEGER;
        ALTER TABLE keyword_selection_sessions ADD COLUMN IF NOT EXISTS status TEXT;
        ALTER TABLE keyword_selection_sessions ADD COLUMN IF NOT EXISTS status_before_archive TEXT;
        ALTER TABLE keyword_selection_sessions ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
        ALTER TABLE keyword_selection_sessions ADD COLUMN IF NOT EXISTS archived_by_user_id INTEGER;
        ALTER TABLE keyword_selection_sessions ADD COLUMN IF NOT EXISTS archived_with_brand_at TIMESTAMPTZ;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS archived_with_brand_at TIMESTAMPTZ;
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS deleted_reason TEXT;
        """,
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) AS count FROM pg_constraint co
            JOIN pg_class c ON c.oid=co.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND co.conname LIKE 'org_%' AND NOT co.convalidated
            """
        )
        verify.check(int(cursor.fetchone()["count"]) == 0, "migration_constraints_all_validated")
        cursor.execute(
            """
            SELECT COUNT(*) AS count FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='article_generations'
              AND column_name IN ('brand_id','organization_id','created_by_membership_id','created_by_actor_kind','artifact_visibility')
            """
        )
        verify.check(int(cursor.fetchone()["count"]) == 5, "migration_repairs_partial_artifact_columns")

    # A current-schema decoy view, wrong type, weak named constraint, and wrong
    # named index must not satisfy readiness even if another schema has valid objects.
    bad_schema, bad_dsn = create_schema(args.dsn, "org_seats_bad")
    execute_sql(
        bad_dsn,
        """
        CREATE VIEW organizations AS SELECT 'wrong'::text AS id,'wrong'::text AS owner_user_id;
        CREATE TABLE organization_approval_requests(
          id BIGINT,requested_by_user_id INTEGER,approved_by_user_id INTEGER,
          payload_hash INTEGER,feature_code INTEGER,
          CONSTRAINT organization_approval_no_self CHECK (TRUE)
        );
        CREATE TABLE organization_memberships(id BIGINT,user_id INTEGER,status TEXT);
        CREATE UNIQUE INDEX ux_org_membership_live_user ON organization_memberships(id);
        """,
    )
    with connect(bad_dsn) as conn:
        bad = readiness(cursor=conn.cursor())
    verify.check(not bad["ready"] and "organizations" in bad["wrong_object_kinds"], "readiness_rejects_same_name_view_decoy", bad)
    verify.check(not bad["schema_contract"]["matches"], "readiness_rejects_weak_named_constraint", bad)
    verify.check(not bad["schema_contract"]["matches"], "readiness_rejects_wrong_named_index", bad)
    verify.check(any("payload_hash" in item for item in bad["wrong_column_types"]), "readiness_rejects_wrong_column_type", bad)

    schema_mutations = {
        "same_name_weak_check": """
            ALTER TABLE organization_charge_links
              DROP CONSTRAINT organization_charge_actor_shape;
            ALTER TABLE organization_charge_links
              ADD CONSTRAINT organization_charge_actor_shape
              CHECK (actor_kind IS NULL OR membership_id IS NULL OR payer_user_id IS NULL);
        """,
        "true_or_check": """
            ALTER TABLE organization_automatic_plans
              DROP CONSTRAINT organization_automatic_plan_budget_valid;
            ALTER TABLE organization_automatic_plans
              ADD CONSTRAINT organization_automatic_plan_budget_valid
              CHECK (TRUE OR reserved_budget_points+consumed_budget_points-refunded_budget_points<=total_budget_points);
        """,
        "unique_extra_column": """
            DROP INDEX ux_org_membership_live_user;
            CREATE UNIQUE INDEX ux_org_membership_live_user
              ON organization_memberships(user_id,id)
              WHERE status IN ('active','suspended','leaving');
        """,
        "wrong_index_predicate": """
            DROP INDEX ux_org_membership_live_user;
            CREATE UNIQUE INDEX ux_org_membership_live_user
              ON organization_memberships(user_id) WHERE status='active';
        """,
        "not_valid_constraint": """
            ALTER TABLE organization_charge_links
              DROP CONSTRAINT organization_charge_actor_shape;
            ALTER TABLE organization_charge_links
              ADD CONSTRAINT organization_charge_actor_shape CHECK (
                (actor_kind='member' AND membership_id IS NOT NULL AND actor_user_id IS NOT NULL)
                OR (actor_kind='owner' AND membership_id IS NOT NULL AND actor_user_id=payer_user_id)
                OR (actor_kind='system' AND membership_id IS NULL AND actor_user_id IS NULL)
                OR actor_kind='legacy_owner_backfill'
              ) NOT VALID;
        """,
        "foreign_key_wrong_target": """
            ALTER TABLE organization_plan_occurrences
              DROP CONSTRAINT organization_plan_occurrence_charge_fk;
            ALTER TABLE organization_plan_occurrences
              ADD CONSTRAINT organization_plan_occurrence_charge_fk
              FOREIGN KEY (charge_link_id) REFERENCES organization_plan_occurrences(id) NOT VALID;
        """,
        "bigint_downgraded_to_integer": """
            ALTER TABLE organization_charge_links
              ALTER COLUMN reserved_ceiling_points TYPE INTEGER;
        """,
        "dropped_not_null_default": """
            ALTER TABLE organization_work_outbox ALTER COLUMN status DROP NOT NULL;
            ALTER TABLE organization_work_outbox ALTER COLUMN status DROP DEFAULT;
        """,
    }
    for mutation_name, mutation_sql in schema_mutations.items():
        assert_full_schema_mutation_rejected(
            verify,
            args.dsn,
            readiness,
            name=mutation_name,
            mutation_sql=mutation_sql,
        )

    # Functional rollback is the frozen contract: flags off reject new work,
    # forward re-enable restores admission without deleting schema or history.
    os.environ["ORGANIZATION_SHARED_PAYER_ENABLED"] = "false"
    try:
        require_feature_flag("ORGANIZATION_SHARED_PAYER_ENABLED")
        flag_rejected = False
    except OrganizationError as exc:
        flag_rejected = exc.code == "ORG_SHARED_PAYER_DISABLED"
    verify.check(flag_rejected, "rollback_flag_disables_new_shared_payer_work")
    os.environ["ORGANIZATION_SHARED_PAYER_ENABLED"] = "true"
    verify.check(feature_flags()["ORGANIZATION_SHARED_PAYER_ENABLED"], "rollback_forward_reenables_admission")

    overview = create_organization(owner_user_id=1, name="服务商内部员工席位完整验证团队", request_id="org-create-verification-0001")
    organization_id = int(overview["id"])
    enable_payer_policy_seed(dsn, organization_id, owner_user_id=1)
    owner = resolve_identity(1, request_id="owner-verification")
    verify.check(owner is not None and owner.is_owner and owner.payer_user_id == 1, "owner_is_principal_and_payer")
    role_id = int(sql_value(dsn, "SELECT id FROM organization_roles WHERE organization_id=%s AND code='sales'", (organization_id,)))

    # Twenty simultaneous invitation attempts must never oversubscribe three
    # employee seats; owner is excluded and pending invitations occupy seats.
    def invite_attempt(user_id: int):
        try:
            result = create_invite(
                owner,
                target_kind="phone",
                target=f"139{user_id:08d}",
                role_id=role_id,
                request_id=f"seat-concurrency-{user_id:04d}",
                source_ip="192.0.2.10",
            )
            return ("ok", user_id, result)
        except OrganizationError as exc:
            return (exc.code, user_id, None)

    with ThreadPoolExecutor(max_workers=20) as executor:
        seat_results = list(executor.map(invite_attempt, range(2, 22)))
    successes = [item for item in seat_results if item[0] == "ok"]
    rejected = [item for item in seat_results if item[0] == "ORG_SEAT_LIMIT_REACHED"]
    verify.check(len(successes) == 3 and len(rejected) == 17, "seat_concurrency_20_no_oversubscription", seat_results)
    verify.check(
        int(sql_value(dsn, "SELECT COUNT(*) FROM organization_invites WHERE organization_id=%s AND status='pending'", (organization_id,))) == 3,
        "pending_invites_count_as_seats",
    )

    first = successes[0]
    first_user = int(first[1])
    first_token = first[2]["delivery_token"]
    inspected = inspect_invite(
        authenticated_user_id=first_user, token=first_token,
        request_id="inspect-first-member", source_ip="192.0.2.111",
    )
    verify.check(
        inspected["status"] == "pending" and inspected["can_accept"]
        and inspected["account_matched"] and inspected["target_verified"],
        "invite_accept_page_is_account_matched_and_actionable",
        inspected,
    )
    accepted = accept_invite(
        authenticated_user_id=first_user, token=first_token,
        request_id="accept-first-member", source_ip="192.0.2.11",
    )
    member_a_id = int(accepted["membership_id"])
    recovered_accept = accept_invite(
        authenticated_user_id=first_user, token=first_token,
        request_id="accept-first-member", source_ip="192.0.2.11",
    )
    verify.check(
        recovered_accept["membership_id"] == accepted["membership_id"]
        and recovered_accept["organization_id"] == accepted["organization_id"]
        and recovered_accept["replayed"] is True,
        "invite_accept_commit_response_loss_replays_original_success",
        recovered_accept,
    )
    idempotency_conflict = None
    try:
        accept_invite(
            authenticated_user_id=first_user, token=first_token,
            request_id="accept-first-member-different", source_ip="192.0.2.11",
        )
    except OrganizationError as exc:
        idempotency_conflict = exc.code
    verify.check(
        idempotency_conflict == "ORG_INVITE_IDEMPOTENCY_CONFLICT",
        "invite_accept_different_request_cannot_claim_prior_success",
        idempotency_conflict,
    )
    accepted_inspection = inspect_invite(
        authenticated_user_id=first_user, token=first_token,
        request_id="inspect-accepted-first-member", source_ip="192.0.2.112",
    )
    verify.check(
        accepted_inspection["status"] == "accepted"
        and not accepted_inspection["can_accept"]
        and accepted_inspection["blocker_code"] == "ORG_INVITE_ACCEPTED",
        "accepted_invite_inspection_blocks_replay",
        accepted_inspection,
    )
    second_invite = successes[1][2]["invite"]
    revoke_invite(owner, invite_id=int(second_invite["id"]), reason="并发席位验证后撤销")
    revoked_inspection = inspect_invite(
        authenticated_user_id=int(successes[1][1]),
        token=successes[1][2]["delivery_token"],
        request_id="inspect-revoked-member", source_ip="192.0.2.113",
    )
    verify.check(
        revoked_inspection["status"] == "revoked"
        and not revoked_inspection["can_accept"]
        and revoked_inspection["blocker_code"] == "ORG_INVITE_REVOKED",
        "revoked_invite_inspection_is_terminal",
        revoked_inspection,
    )
    third_invite_id = int(successes[2][2]["invite"]["id"])
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_invites SET created_at=NOW()-INTERVAL '2 days',expires_at=NOW()-INTERVAL '1 day' WHERE id=%s",
            (third_invite_id,),
        )
    list_invites(owner)
    verify.check(
        str(sql_value(dsn, "SELECT status FROM organization_invites WHERE id=%s", (third_invite_id,))) == "expired",
        "invite_expiry_releases_seat",
    )
    expired_inspection = inspect_invite(
        authenticated_user_id=int(successes[2][1]),
        token=successes[2][2]["delivery_token"],
        request_id="inspect-expired-member", source_ip="192.0.2.114",
    )
    verify.check(
        expired_inspection["status"] == "expired"
        and not expired_inspection["can_accept"]
        and expired_inspection["blocker_code"] == "ORG_INVITE_EXPIRED",
        "expired_invite_inspection_is_terminal",
        expired_inspection,
    )

    # Verified-target enforcement, admin conversion rejection, then a second
    # valid member used as an independent reviewer.
    unverified = create_invite(
        owner, target_kind="phone", target="13900000079", role_id=role_id,
        request_id="invite-unverified-79", source_ip="192.0.2.12",
    )
    try:
        accept_invite(
            authenticated_user_id=79, token=unverified["delivery_token"],
            request_id="accept-unverified-79", source_ip="192.0.2.13",
        )
        unverified_rejected = False
    except OrganizationError as exc:
        unverified_rejected = exc.code == "ORG_INVITEE_TARGET_UNVERIFIED"
    verify.check(unverified_rejected, "invite_accept_requires_verified_target")
    revoke_invite(owner, invite_id=int(unverified["invite"]["id"]), reason="验证失败后撤销")

    admin_invite = create_invite(
        owner, target_kind="phone", target="13900000080", role_id=role_id,
        request_id="invite-admin-80", source_ip="192.0.2.14",
    )
    try:
        accept_invite(
            authenticated_user_id=80, token=admin_invite["delivery_token"],
            request_id="accept-admin-80", source_ip="192.0.2.15",
        )
        admin_rejected = False
    except OrganizationError as exc:
        admin_rejected = exc.code == "ORG_ACCOUNT_CONVERSION_FORBIDDEN"
    verify.check(admin_rejected, "admin_account_conversion_rejected")
    revoke_invite(owner, invite_id=int(admin_invite["invite"]["id"]), reason="管理员转换验证后撤销")

    # A sticky generic flag is not proof of possession of a newly edited
    # email. The value-bound evidence must match the exact normalized target.
    email_target = "seat79-new@example.test"
    email_invite = create_invite(
        owner,
        target_kind="email",
        target=email_target,
        role_id=role_id,
        request_id="invite-email-rebind-79",
        source_ip="192.0.2.18",
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET email=%s,email_verified=TRUE WHERE id=79",
            (email_target,),
        )
    try:
        accept_invite(
            authenticated_user_id=79,
            token=email_invite["delivery_token"],
            request_id="accept-email-rebind-79",
            source_ip="192.0.2.19",
        )
        email_rebind_rejected = False
    except OrganizationError as exc:
        email_rebind_rejected = exc.code == "ORG_INVITEE_TARGET_UNVERIFIED"
    verify.check(
        email_rebind_rejected,
        "invite_email_verification_is_bound_to_exact_value",
    )
    revoke_invite(owner, invite_id=int(email_invite["invite"]["id"]), reason="邮箱换绑验证完成")

    rate_code = None
    for attempt in range(11):
        try:
            accept_invite(
                authenticated_user_id=40,
                token="invalid-invite-token-with-sufficient-length",
                request_id=f"accept-rate-{attempt:02d}",
                source_ip="192.0.2.30",
            )
        except OrganizationError as exc:
            rate_code = exc.code
    verify.check(rate_code == "ORG_RATE_LIMITED", "invite_accept_actor_rate_limit_persists_failures", rate_code)
    verify.check(
        int(sql_value(
            dsn,
            "SELECT COUNT(*) FROM organization_security_rate_events WHERE action='invite.accept' AND actor_user_id=40",
        )) == 10,
        "invite_rate_attempts_are_committed_independently",
    )
    verify.check(
        int(sql_value(
            dsn,
            "SELECT COUNT(*) FROM organization_security_rate_events WHERE source_ip_hmac=%s OR length(source_ip_hmac)<>64",
            ("192.0.2.30",),
        )) == 0,
        "invite_rate_ip_is_hmac_only",
    )

    member_b_user = 30
    member_b_invite = create_invite(
        owner, target_kind="phone", target=f"139{member_b_user:08d}", role_id=role_id,
        request_id="invite-reviewer-30", source_ip="192.0.2.16",
    )
    member_b_id = int(accept_invite(
        authenticated_user_id=member_b_user, token=member_b_invite["delivery_token"],
        request_id="accept-reviewer-30", source_ip="192.0.2.17",
    )["membership_id"])
    owner = resolve_identity(1, request_id="owner-after-members")
    assign_brands(owner, membership_id=member_a_id, brand_ids=[10], request_id="assign-member-a", reason="验证客户隔离")
    assign_brands(owner, membership_id=member_b_id, brand_ids=[10], request_id="assign-member-b", reason="验证审核客户范围")

    member_a_version = int(sql_value(dsn, "SELECT version FROM organization_memberships WHERE id=%s", (member_a_id,)))
    set_member_overrides(
        owner,
        membership_id=member_a_id,
        expected_membership_version=member_a_version,
        overrides={"approvals.review": "allow", "writing.share_internal": "allow", "writing.generate": "allow", "materials.write": "allow", "reports.generate": "allow"},
        reason="验证自批禁止和内部共享",
        high_risk_confirmed=True,
        high_risk_reason="测试显式跨域授权",
    )
    member_b_version = int(sql_value(dsn, "SELECT version FROM organization_memberships WHERE id=%s", (member_b_id,)))
    set_member_overrides(
        owner,
        membership_id=member_b_id,
        expected_membership_version=member_b_version,
        overrides={"approvals.review": "allow"},
        reason="授予独立审核能力",
        high_risk_confirmed=True,
        high_risk_reason="测试显式独立审核授权",
    )
    member_a = resolve_identity(first_user, request_id="member-a-live")
    member_b = resolve_identity(member_b_user, request_id="member-b-live")
    verify.check(member_a.payer_user_id == 1 and member_a.actor_user_id == first_user, "member_actor_owner_payer_split")

    # Real FastAPI/HTTP discrimination through the production organization
    # guard and production routers. The auth shim only injects the already
    # authenticated user id; all membership, assignment and capability state
    # is still resolved from this throwaway PostgreSQL schema on every request.
    execute_sql(
        dsn,
        """
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS industry TEXT;
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS company_name TEXT;
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS cities TEXT;
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS brand_type TEXT NOT NULL DEFAULT 'client';
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS agent_payment_note JSONB;
        UPDATE brands SET agent_payment_note='{"received_amount":88000,"note":"老板私有收款备注"}'::jsonb
        WHERE id=10;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS is_deleted INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS total_score INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS result_visibility TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS diagnosis_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS latest_score INTEGER;
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS latest_diagnosis_id BIGINT;
        """,
    )
    from fastapi import FastAPI, HTTPException, WebSocket
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware
    from api.admin_api import router as admin_router
    from api.brand_api import router as brand_router
    from api.organization_api import public_router as organization_public_router
    from api.organization_api import router as organization_router
    from middleware.organization_guard import setup_organization_guard

    class _ThrowawayAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            raw_user_id = request.headers.get("x-throwaway-user-id")
            if raw_user_id:
                request.state.user = {
                    "user_id": int(raw_user_id),
                    "id": int(raw_user_id),
                    "username": f"user{int(raw_user_id)}",
                    "is_admin": int(raw_user_id) == 80,
                }
            return await call_next(request)

    admin_app = FastAPI()
    admin_app.include_router(admin_router)
    setup_organization_guard(admin_app)
    admin_app.add_middleware(_ThrowawayAuthMiddleware)
    admin_headers = {
        "x-throwaway-user-id": "80",
        "x-request-id": "admin-client-scope-http-0001",
    }
    with TestClient(admin_app, raise_server_exceptions=False) as admin_client:
        raw_legacy_writer = admin_client.put(
            f"/api/admin/users/{first_user}/clients",
            headers=admin_headers,
            json={"brand_ids": [10]},
        )
        verify.check(
            raw_legacy_writer.status_code == 422,
            "admin_client_scope_rejects_brand_ids_only_legacy_writer",
            raw_legacy_writer.json(),
        )

        owner_scope_response = admin_client.get("/api/admin/users/1/clients", headers=admin_headers)
        owner_scope = owner_scope_response.json()
        verify.check(
            owner_scope_response.status_code == 200
            and owner_scope["scope_kind"] == "organization_assigned"
            and owner_scope["subject_kind"] == "organization_owner"
            and owner_scope["editable"] is False
            and owner_scope["brand_ids"] == []
            and owner_scope["effective_brand_ids"] == [10]
            and owner_scope["empty_semantics"] == "owner_identity_all",
            "admin_client_scope_owner_uses_identity_not_empty_assignment",
            owner_scope,
        )
        owner_mutation = admin_client.put(
            "/api/admin/users/1/clients",
            headers=admin_headers,
            json={
                "scope_kind": "organization_assigned",
                "expected_scope_kind": owner_scope["scope_kind"],
                "expected_version": owner_scope["version"],
                "etag": owner_scope["etag"],
                "brand_ids": [],
                "request_id": "admin-owner-scope-immutable-0001",
                "reason": "老板范围不可改",
            },
        )
        verify.check(
            owner_mutation.status_code == 409,
            "admin_client_scope_owner_assignment_is_immutable",
            owner_mutation.json(),
        )

        member_scope_response = admin_client.get(
            f"/api/admin/users/{first_user}/clients", headers=admin_headers
        )
        member_scope = member_scope_response.json()
        verify.check(
            member_scope_response.status_code == 200
            and member_scope["scope_kind"] == "organization_assigned"
            and member_scope["subject_kind"] == "organization_member"
            and member_scope["brand_ids"] == [10]
            and member_scope["empty_semantics"] == "zero_clients"
            and member_scope_response.headers.get("etag") == f'"{member_scope["etag"]}"',
            "admin_client_scope_member_contract_and_etag",
            member_scope,
        )
        member_wrong_mode = admin_client.put(
            f"/api/admin/users/{first_user}/clients",
            headers=admin_headers,
            json={
                "scope_kind": "legacy_unrestricted",
                "expected_scope_kind": member_scope["scope_kind"],
                "expected_version": member_scope["version"],
                "etag": member_scope["etag"],
                "brand_ids": [],
                "request_id": "admin-member-wrong-mode-0001",
                "reason": "不得降级绕过组织范围",
            },
        )
        verify.check(
            member_wrong_mode.status_code == 409,
            "admin_client_scope_member_cross_mode_fails_closed",
            member_wrong_mode.json(),
        )
        before_member_permission = int(
            sql_value(dsn, "SELECT permission_version FROM users WHERE id=%s", (first_user,))
        )
        member_empty_payload = {
            "scope_kind": "organization_assigned",
            "expected_scope_kind": member_scope["scope_kind"],
            "expected_version": member_scope["version"],
            "etag": member_scope["etag"],
            "brand_ids": [],
            "request_id": "admin-member-empty-0001",
            "reason": "验证员工空分配等于零客户",
        }
        member_empty_response = admin_client.put(
            f"/api/admin/users/{first_user}/clients",
            headers=admin_headers,
            json=member_empty_payload,
        )
        member_empty = member_empty_response.json()
        verify.check(
            member_empty_response.status_code == 200
            and member_empty["brand_ids"] == []
            and member_empty["effective_brand_ids"] == []
            and member_empty["permission_version"] == before_member_permission + 1
            and sql_value(
                dsn,
                "SELECT COUNT(*) FROM organization_brand_assignments WHERE membership_id=%s AND status='active'",
                (member_a_id,),
            ) == 0
            and sql_value(dsn, "SELECT COUNT(*) FROM user_clients WHERE user_id=%s", (first_user,)) == 0,
            "admin_client_scope_member_empty_is_zero_and_bumps_generations",
            member_empty,
        )
        member_replay = admin_client.put(
            f"/api/admin/users/{first_user}/clients",
            headers=admin_headers,
            json=member_empty_payload,
        )
        verify.check(
            member_replay.status_code == 200
            and member_replay.json().get("replayed") is True
            and member_replay.json()["brand_ids"] == [],
            "admin_client_scope_member_retry_replays_durably",
            member_replay.json(),
        )
        replay_tamper = admin_client.put(
            f"/api/admin/users/{first_user}/clients",
            headers=admin_headers,
            json={**member_empty_payload, "brand_ids": [10]},
        )
        verify.check(
            replay_tamper.status_code == 409,
            "admin_client_scope_same_request_different_payload_rejected",
            replay_tamper.json(),
        )
        current_member_scope = admin_client.get(
            f"/api/admin/users/{first_user}/clients", headers=admin_headers
        ).json()
        member_restore = admin_client.put(
            f"/api/admin/users/{first_user}/clients",
            headers=admin_headers,
            json={
                "scope_kind": "organization_assigned",
                "expected_scope_kind": current_member_scope["scope_kind"],
                "expected_version": current_member_scope["version"],
                "etag": current_member_scope["etag"],
                "brand_ids": [10],
                "request_id": "admin-member-restore-0001",
                "reason": "恢复后续验证客户",
            },
        )
        verify.check(
            member_restore.status_code == 200 and member_restore.json()["brand_ids"] == [10],
            "admin_client_scope_member_restore_uses_org_ssot",
            member_restore.json(),
        )
        replay_superseded = admin_client.put(
            f"/api/admin/users/{first_user}/clients",
            headers=admin_headers,
            json=member_empty_payload,
        )
        verify.check(
            replay_superseded.status_code == 409
            and replay_superseded.json().get("detail", {}).get("code")
                == "ADMIN_CLIENT_SCOPE_REPLAY_SUPERSEDED"
            and replay_superseded.json().get("brand_ids") != [10],
            "admin_client_scope_a_then_b_then_replay_a_is_not_current_b_success",
            replay_superseded.json(),
        )
        restored_scope = member_restore.json()
        org_cross_tenant = admin_client.put(
            f"/api/admin/users/{first_user}/clients",
            headers=admin_headers,
            json={
                "scope_kind": "organization_assigned",
                "expected_scope_kind": restored_scope["scope_kind"],
                "expected_version": restored_scope["version"],
                "etag": restored_scope["etag"],
                "brand_ids": [72],
                "request_id": "admin-member-cross-tenant-0001",
                "reason": "跨租户必须隐藏",
            },
        )
        verify.check(
            org_cross_tenant.status_code == 404,
            "admin_client_scope_member_cross_tenant_brand_is_404",
            org_cross_tenant.json(),
        )

        legacy_scope_response = admin_client.get("/api/admin/users/70/clients", headers=admin_headers)
        legacy_scope = legacy_scope_response.json()
        verify.check(
            legacy_scope_response.status_code == 200
            and legacy_scope["scope_kind"] == "legacy_unrestricted"
            and legacy_scope["brand_ids"] == []
            and legacy_scope["empty_semantics"] == "legacy_owner_role_fallback"
            and {row["id"] for row in legacy_scope["available_brands"]} == {70, 71}
            and 72 not in {row["id"] for row in legacy_scope["available_brands"]},
            "admin_client_scope_legacy_unrestricted_lawful_range",
            legacy_scope,
        )
        legacy_selected_response = admin_client.put(
            "/api/admin/users/70/clients",
            headers=admin_headers,
            json={
                "scope_kind": "legacy_selected",
                "expected_scope_kind": legacy_scope["scope_kind"],
                "expected_version": legacy_scope["version"],
                "etag": legacy_scope["etag"],
                "brand_ids": [70, 71],
                "request_id": "admin-legacy-selected-0001",
                "reason": "验证旧账号指定客户",
            },
        )
        legacy_selected = legacy_selected_response.json()
        verify.check(
            legacy_selected_response.status_code == 200
            and legacy_selected["scope_kind"] == "legacy_selected"
            and legacy_selected["brand_ids"] == [70, 71]
            and legacy_selected["effective_brand_ids"] == [70, 71],
            "admin_client_scope_legacy_selected_roundtrip",
            legacy_selected,
        )
        legacy_cross_tenant = admin_client.put(
            "/api/admin/users/70/clients",
            headers=admin_headers,
            json={
                "scope_kind": "legacy_selected",
                "expected_scope_kind": legacy_selected["scope_kind"],
                "expected_version": legacy_selected["version"],
                "etag": legacy_selected["etag"],
                "brand_ids": [72],
                "request_id": "admin-legacy-cross-tenant-0001",
                "reason": "跨租户必须隐藏",
            },
        )
        verify.check(
            legacy_cross_tenant.status_code == 404,
            "admin_client_scope_legacy_cross_tenant_brand_is_404",
            legacy_cross_tenant.json(),
        )
        legacy_wrong_mode = admin_client.put(
            "/api/admin/users/70/clients",
            headers=admin_headers,
            json={
                "scope_kind": "organization_assigned",
                "expected_scope_kind": legacy_selected["scope_kind"],
                "expected_version": legacy_selected["version"],
                "etag": legacy_selected["etag"],
                "brand_ids": [],
                "request_id": "admin-legacy-wrong-mode-0001",
                "reason": "不得伪造组织模式",
            },
        )
        verify.check(
            legacy_wrong_mode.status_code == 409,
            "admin_client_scope_legacy_cross_mode_fails_closed",
            legacy_wrong_mode.json(),
        )

    concurrent_baseline = get_admin_client_scope(70)

    def replace_legacy_concurrently(ids, suffix):
        try:
            result = replace_admin_client_scope(
                admin_user_id=80,
                target_user_id=70,
                scope_kind="legacy_selected",
                expected_scope_kind=concurrent_baseline["scope_kind"],
                expected_version=concurrent_baseline["version"],
                expected_etag=concurrent_baseline["etag"],
                brand_ids=ids,
                request_id=f"admin-legacy-concurrent-{suffix}",
                reason="验证并发 CAS",
            )
            return ("success", result["brand_ids"])
        except AdminClientScopeError as exc:
            return (exc.code, None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_results = list(executor.map(
            lambda item: replace_legacy_concurrently(*item),
            [([70], "a"), ([71], "b")],
        ))
    verify.check(
        [row[0] for row in concurrent_results].count("success") == 1
        and [row[0] for row in concurrent_results].count("ADMIN_CLIENT_SCOPE_VERSION_CONFLICT") == 1,
        "admin_client_scope_legacy_concurrent_cas_one_winner",
        concurrent_results,
    )
    with TestClient(admin_app, raise_server_exceptions=False) as admin_client:
        current_legacy = admin_client.get("/api/admin/users/70/clients", headers=admin_headers).json()
        legacy_empty_response = admin_client.put(
            "/api/admin/users/70/clients",
            headers=admin_headers,
            json={
                "scope_kind": "legacy_unrestricted",
                "expected_scope_kind": current_legacy["scope_kind"],
                "expected_version": current_legacy["version"],
                "etag": current_legacy["etag"],
                "brand_ids": [],
                "request_id": "admin-legacy-empty-0001",
                "reason": "验证旧账号兼容回退",
            },
        )
        legacy_empty = legacy_empty_response.json()
        verify.check(
            legacy_empty_response.status_code == 200
            and legacy_empty["scope_kind"] == "legacy_unrestricted"
            and legacy_empty["brand_ids"] == []
            and legacy_empty["effective_brand_ids"] == [70],
            "admin_client_scope_legacy_empty_is_not_global_access",
            legacy_empty,
        )
        legacy_for_soft_delete = admin_client.put(
            "/api/admin/users/70/clients",
            headers=admin_headers,
            json={
                "scope_kind": "legacy_selected",
                "expected_scope_kind": legacy_empty["scope_kind"],
                "expected_version": legacy_empty["version"],
                "etag": legacy_empty["etag"],
                "brand_ids": [70, 71],
                "request_id": "admin-legacy-soft-delete-setup-0001",
                "reason": "准备软删撤权验证",
            },
        )
        verify.check(
            legacy_for_soft_delete.status_code == 200,
            "admin_client_scope_legacy_soft_delete_setup",
            legacy_for_soft_delete.json(),
        )

    http_app = FastAPI()
    http_app.include_router(brand_router)
    http_app.include_router(organization_router)

    @http_app.get("/api/monitoring/config")
    def _unclassified_monitoring_config():
        return {"unsafe": True}

    unclassified_ws_provider_calls = {"count": 0}

    @http_app.websocket("/api/interview/voice-stream")
    async def _unclassified_voice_stream(websocket: WebSocket):
        await websocket.accept()
        unclassified_ws_provider_calls["count"] += 1
        await websocket.send_json({"unsafe": True})

    setup_organization_guard(http_app)
    # Starlette executes the most recently-added middleware first, so this
    # test auth shim runs before the production organization guard.
    http_app.add_middleware(_ThrowawayAuthMiddleware)
    http_headers = {"x-throwaway-user-id": str(first_user), "x-request-id": "http-member-route-0001"}
    import auth.jwt_utils as organization_ws_jwt
    original_decode_jwt = organization_ws_jwt.decode_jwt
    member_permission_version = int(
        sql_value(dsn, "SELECT permission_version FROM users WHERE id=%s", (first_user,))
    )
    organization_ws_jwt.decode_jwt = lambda _token: {
        "user_id": first_user,
        "perm_version": member_permission_version,
    }
    with TestClient(http_app, raise_server_exceptions=False) as client:
        unclassified_ws_code = None
        try:
            with client.websocket_connect(
                "/api/interview/voice-stream?token=throwaway-member-token-0001"
            ):
                pass
        except Exception as exc:
            unclassified_ws_code = getattr(exc, "code", None)
        verify.check(
            unclassified_ws_code == 4403 and unclassified_ws_provider_calls["count"] == 0,
            "unclassified_websocket_member_rejected_before_provider",
            {"code": unclassified_ws_code, "provider_calls": unclassified_ws_provider_calls},
        )
        clients_response = client.get("/api/my-clients", headers=http_headers)
        clients_payload = clients_response.json()
        verify.check(
            clients_response.status_code == 200
            and [int(row["id"]) for row in clients_payload.get("clients", [])] == [10]
            and "owner_user_id" not in clients_payload["clients"][0]
            and "agent_payment_note" not in clients_payload["clients"][0],
            "http_member_reads_only_assigned_clients_without_owner_fields",
            clients_payload,
        )
        with client.websocket_connect(
            "/api/organization/ws",
            subprotocols=["omnirank-auth", "throwaway-member-token-0001"],
        ) as websocket:
            first_heartbeat = websocket.receive_json()
            with connect(dsn) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE organization_memberships
                    SET status='suspended',capability_version=capability_version+1,
                        version=version+1,updated_at=NOW()
                    WHERE id=%s
                    """,
                    (member_a_id,),
                )
                cursor.execute(
                    "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
                    (organization_id,),
                )
            revoked_event = websocket.receive_json()
            verify.check(
                first_heartbeat.get("type") == "heartbeat"
                and revoked_event.get("type") == "permission-revoked",
                "websocket_open_connection_revoked_after_seat_suspension",
                {"heartbeat": first_heartbeat, "revoked": revoked_event},
            )
        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE organization_memberships
                SET status='active',capability_version=capability_version+1,
                    version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (member_a_id,),
            )
            cursor.execute(
                "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
                (organization_id,),
            )
        unclassified_response = client.get("/api/monitoring/config", headers=http_headers)
        verify.check(
            unclassified_response.status_code == 403
            and unclassified_response.json()["detail"]["code"] == "ORG_ROUTE_NOT_CLASSIFIED",
            "http_unclassified_live_route_fails_closed",
            unclassified_response.json(),
        )
        direct_reservation = client.post(
            "/api/organization/work/reservations",
            headers=http_headers,
            json={
                "execution_id": "http-direct-reservation-0001",
                "feature_code": "geo_diagnosis",
                "work_kind": "diagnosis.run",
                "payload": {"brand_id": 10},
                "brand_id": 10,
            },
        )
        verify.check(
            direct_reservation.status_code == 403
            and direct_reservation.json()["detail"]["code"] == "ORG_DIRECT_RESERVATION_FORBIDDEN",
            "http_generic_reservation_cannot_bypass_live_handler",
            direct_reservation.json(),
        )
        forged_public = client.post(
            "/api/organization/artifacts/shares/public",
            headers=http_headers,
            json={
                "artifact_type": "quote",
                "artifact_id": "1",
                "purpose": "quote",
                "request_id": "http-forged-whitelabel-0001",
                "expires_in_seconds": 300,
                "whitelabel_snapshot": {
                    "brand": {"company_name": "员工伪造品牌", "logo_url": "https://evil.invalid/logo"},
                    "contact": {"phone": "10086"},
                },
            },
        )
        verify.check(
            forged_public.status_code == 422,
            "http_public_token_request_rejects_client_whitelabel_field",
            forged_public.json(),
        )

        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE organization_brand_assignments
                SET status='revoked',version=version+1,revoked_at=NOW()
                WHERE organization_id=%s AND membership_id=%s AND brand_id=10
                """,
                (organization_id, member_b_id),
            )
            cursor.execute(
                """
                UPDATE organization_memberships
                SET assignment_version=assignment_version+1,version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (member_b_id,),
            )
            cursor.execute(
                "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
                (organization_id,),
            )
        unassigned_response = client.get(
            "/api/my-clients/10",
            headers={"x-throwaway-user-id": str(member_b_user), "x-request-id": "http-unassigned-0001"},
        )
        verify.check(
            unassigned_response.status_code in {403, 404},
            "http_unassigned_member_cannot_read_client_detail",
            unassigned_response.json(),
        )
        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE organization_memberships
                SET status='suspended',capability_version=capability_version+1,version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (member_a_id,),
            )
            cursor.execute(
                "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
                (organization_id,),
            )
        suspended_response = client.get("/api/my-clients", headers=http_headers)
        verify.check(
            suspended_response.status_code == 403
            and suspended_response.json()["detail"]["code"] == "ORG_MEMBERSHIP_INACTIVE",
            "http_suspended_seat_is_rejected_immediately",
            suspended_response.json(),
        )
    organization_ws_jwt.decode_jwt = original_decode_jwt

    # Restore both members for the remaining billing/approval scenarios.
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE organization_brand_assignments
            SET status='active',version=version+1,revoked_at=NULL
            WHERE organization_id=%s AND membership_id=%s AND brand_id=10
            """,
            (organization_id, member_b_id),
        )
        cursor.execute(
            """
            UPDATE organization_memberships
            SET status='active',assignment_version=assignment_version+1,
                capability_version=capability_version+1,version=version+1,updated_at=NOW()
            WHERE id IN (%s,%s)
            """,
            (member_a_id, member_b_id),
        )
        cursor.execute(
            "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
            (organization_id,),
        )
    member_a = resolve_identity(first_user, request_id="member-a-after-http-revocation")
    member_b = resolve_identity(member_b_user, request_id="member-b-after-http-revocation")

    for membership_id in (member_a_id, member_b_id):
        configure_limit(owner, membership_id=membership_id, limit_kind="daily_total", limit_points=1000000, reason="并发验证日上限")
        configure_limit(owner, membership_id=membership_id, limit_kind="monthly_total", limit_points=1000000, reason="并发验证月上限")
        for feature_code in ("geo_diagnosis", "quote_generate", "topic_gen", "monitor_single", "article_gen"):
            configure_limit(owner, membership_id=membership_id, limit_kind="daily_feature", feature_code=feature_code, limit_points=800000, reason="并发验证日功能上限")
            configure_limit(owner, membership_id=membership_id, limit_kind="monthly_feature", feature_code=feature_code, limit_points=800000, reason="并发验证月功能上限")

    # Live authority must be re-read at both execution boundaries.  These four
    # real PostgreSQL races cover diagnosis, quote, writing and monitoring and
    # leave every definitely-unstarted reservation fully released.
    capability_race = asyncio.run(
        reserve_charge(
            member_a,
            execution_id="revocation-race-capability-before-claim",
            feature_code="geo_diagnosis",
            work_kind="diagnosis.run",
            payload={"brand_id": 10, "race": "capability-before-claim"},
            brand_id=10,
        )
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        current_version = int(sql_value(dsn, "SELECT version FROM organization_memberships WHERE id=%s", (member_a_id,)))
        cursor.execute(
            """
            INSERT INTO organization_member_capability_overrides(
              membership_id,capability,effect,expected_membership_version,reason,created_by_user_id
            ) VALUES (%s,'diagnosis.run','deny',%s,'race regression',1)
            ON CONFLICT(membership_id,capability) DO UPDATE SET effect='deny'
            """,
            (member_a_id, current_version),
        )
        cursor.execute(
            "UPDATE organization_memberships SET version=version+1,capability_version=capability_version+1 WHERE id=%s",
            (member_a_id,),
        )
        cursor.execute(
            "UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s",
            (organization_id,),
        )
    try:
        claim_live_charge(member_a, charge_link_id=int(capability_race["id"]))
        capability_stopped = False
    except OrganizationError as exc:
        capability_stopped = exc.code == "ORG_CAPABILITY_DENIED"
    verify.check(capability_stopped, "revoked_capability_between_reserve_and_claim_stops_diagnosis")
    asyncio.run(release_charge(charge_link_id=int(capability_race["id"]), reason="revoked before claim"))
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM organization_member_capability_overrides WHERE membership_id=%s AND capability='diagnosis.run'",
            (member_a_id,),
        )
        cursor.execute(
            "UPDATE organization_memberships SET version=version+1,capability_version=capability_version+1 WHERE id=%s",
            (member_a_id,),
        )
        cursor.execute("UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s", (organization_id,))

    assignment_race = asyncio.run(
        reserve_charge(
            member_a,
            execution_id="revocation-race-assignment-before-claim",
            feature_code="quote_generate",
            work_kind="quote.create",
            payload={"brand_id": 10, "race": "assignment-before-claim"},
            brand_id=10,
        )
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_brand_assignments SET status='revoked',revoked_at=NOW(),version=version+1 WHERE organization_id=%s AND membership_id=%s AND brand_id=10 AND status='active'",
            (organization_id, member_a_id),
        )
        cursor.execute("UPDATE organization_memberships SET version=version+1,assignment_version=assignment_version+1 WHERE id=%s", (member_a_id,))
        cursor.execute("UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s", (organization_id,))
    try:
        claim_live_charge(member_a, charge_link_id=int(assignment_race["id"]))
        assignment_stopped = False
    except OrganizationError as exc:
        assignment_stopped = exc.code in {"ORG_BRAND_NOT_ASSIGNED", "ORG_CHARGE_AUTHORITY_CHANGED"}
    verify.check(assignment_stopped, "revoked_assignment_between_reserve_and_claim_stops_quote")
    asyncio.run(release_charge(charge_link_id=int(assignment_race["id"]), reason="brand unassigned before claim"))
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE organization_brand_assignments
            SET status='active',revoked_at=NULL,version=version+1
            WHERE id=(
                SELECT id FROM organization_brand_assignments
                WHERE organization_id=%s AND membership_id=%s AND brand_id=10
                ORDER BY id DESC LIMIT 1
            )
            """,
            (organization_id, member_a_id),
        )
        cursor.execute("UPDATE organization_memberships SET version=version+1,assignment_version=assignment_version+1 WHERE id=%s", (member_a_id,))
        cursor.execute("UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s", (organization_id,))

    member_a = resolve_identity(first_user, request_id="member-a-before-suspend-race")
    suspend_race = asyncio.run(
        reserve_charge(
            member_a,
            execution_id="revocation-race-suspend-before-external",
            feature_code="article_gen",
            work_kind="writing.generate",
            payload={"brand_id": 10, "race": "suspend-before-external"},
            brand_id=10,
        )
    )
    suspend_claim = claim_live_charge(member_a, charge_link_id=int(suspend_race["id"]))
    owner = resolve_identity(1, request_id="owner-suspend-race")
    set_member_status(owner, membership_id=member_a_id, action="suspend", reason="外调前暂停竞态验证")
    try:
        mark_external_side_effect_started(
            charge_link_id=int(suspend_race["id"]),
            claim_token=str(suspend_claim["claim_token"]),
        )
        suspension_stopped = False
    except OrganizationError as exc:
        suspension_stopped = exc.code in {"ORG_MEMBERSHIP_INACTIVE", "ORG_IDENTITY_CHANGED"}
    verify.check(suspension_stopped, "suspended_membership_between_claim_and_external_stops_writing")
    asyncio.run(release_charge(charge_link_id=int(suspend_race["id"]), reason="seat suspended before provider"))
    owner = resolve_identity(1, request_id="owner-resume-race")
    set_member_status(owner, membership_id=member_a_id, action="resume", reason="竞态验证后恢复")

    # A revoke followed by re-grant must not resurrect a claim created under
    # the old authority generation, even though the final live capability and
    # assignment shape looks allowed again.
    member_a = resolve_identity(first_user, request_id="member-a-before-regrant-race")
    regrant_race = asyncio.run(
        reserve_charge(
            member_a,
            execution_id="revocation-race-regrant-before-external",
            feature_code="article_gen",
            work_kind="writing.generate",
            payload={"brand_id": 10, "race": "revoke-then-regrant"},
            brand_id=10,
        )
    )
    regrant_claim = claim_live_charge(member_a, charge_link_id=int(regrant_race["id"]))
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_brand_assignments SET status='revoked',revoked_at=NOW(),version=version+1 WHERE organization_id=%s AND membership_id=%s AND brand_id=10 AND status='active'",
            (organization_id, member_a_id),
        )
        cursor.execute(
            """
            UPDATE organization_brand_assignments
            SET status='active',revoked_at=NULL,version=version+1
            WHERE id=(
                SELECT id FROM organization_brand_assignments
                WHERE organization_id=%s AND membership_id=%s AND brand_id=10
                ORDER BY id DESC LIMIT 1
            )
            """,
            (organization_id, member_a_id),
        )
        cursor.execute(
            "UPDATE organization_memberships SET version=version+2,assignment_version=assignment_version+2 WHERE id=%s",
            (member_a_id,),
        )
        cursor.execute(
            "UPDATE organizations SET authority_version=authority_version+2 WHERE id=%s",
            (organization_id,),
        )
    try:
        mark_external_side_effect_started(
            charge_link_id=int(regrant_race["id"]),
            claim_token=str(regrant_claim["claim_token"]),
        )
        regrant_stopped = False
    except OrganizationError as exc:
        regrant_stopped = exc.code == "ORG_CHARGE_AUTHORITY_CHANGED"
    verify.check(
        regrant_stopped
        and sql_value(
            dsn,
            "SELECT external_side_effect_started_at FROM organization_charge_links WHERE id=%s",
            (regrant_race["id"],),
        ) is None,
        "revoked_then_regranted_assignment_cannot_resurrect_old_claim",
    )
    asyncio.run(release_charge(charge_link_id=int(regrant_race["id"]), reason="authority generation changed"))

    # The generation evidence is bound by the outbox hash.  A same-row JSONB
    # mutation without the matching digest must fail closed before claim.
    member_a = resolve_identity(first_user, request_id="member-a-before-evidence-tamper")
    evidence_tamper_charge = asyncio.run(
        reserve_charge(
            member_a,
            execution_id="authority-evidence-tamper-regression",
            feature_code="geo_diagnosis",
            work_kind="diagnosis.run",
            payload={"brand_id": 10, "race": "evidence-tamper"},
            brand_id=10,
        )
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payload_snapshot FROM organization_work_outbox WHERE charge_link_id=%s",
            (evidence_tamper_charge["id"],),
        )
        original_snapshot = cursor.fetchone()["payload_snapshot"]
        tampered_snapshot = json.loads(json.dumps(original_snapshot))
        tampered_snapshot["authority_generation"]["membership_version"] += 1
        cursor.execute(
            "UPDATE organization_work_outbox SET payload_snapshot=%s::jsonb WHERE charge_link_id=%s",
            (json.dumps(tampered_snapshot), evidence_tamper_charge["id"]),
        )
    try:
        claim_live_charge(member_a, charge_link_id=int(evidence_tamper_charge["id"]))
        evidence_tamper_stopped = False
    except OrganizationError as exc:
        evidence_tamper_stopped = exc.code == "ORG_CHARGE_AUTHORITY_EVIDENCE_CORRUPT"
    verify.check(evidence_tamper_stopped, "outbox_authority_generation_tamper_fails_closed")
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_work_outbox SET payload_snapshot=%s::jsonb WHERE charge_link_id=%s",
            (json.dumps(original_snapshot), evidence_tamper_charge["id"]),
        )
    asyncio.run(
        release_charge(
            charge_link_id=int(evidence_tamper_charge["id"]),
            reason="authority evidence tamper regression restored",
        )
    )

    removable_user = 32
    removable_invite = create_invite(
        owner,
        target_kind="phone",
        target=f"139{removable_user:08d}",
        role_id=role_id,
        request_id="invite-removable-race-member-32",
        source_ip="192.0.2.132",
    )
    removable_member_id = int(
        accept_invite(
            authenticated_user_id=removable_user,
            token=removable_invite["delivery_token"],
            request_id="accept-removable-race-member-32",
            source_ip="192.0.2.133",
        )["membership_id"]
    )
    owner = resolve_identity(1, request_id="owner-assign-removable-race")
    assign_brands(
        owner,
        membership_id=removable_member_id,
        brand_ids=[10],
        request_id="assign-removable-race-member-32",
        reason="移除竞态验证",
    )
    for limit_kind in ("daily_total", "monthly_total"):
        configure_limit(owner, membership_id=removable_member_id, limit_kind=limit_kind, limit_points=1000, reason="移除竞态额度")
    for limit_kind in ("daily_feature", "monthly_feature"):
        configure_limit(owner, membership_id=removable_member_id, limit_kind=limit_kind, feature_code="monitor_single", limit_points=1000, reason="移除竞态功能额度")
    removable_identity = resolve_identity(removable_user, request_id="removable-race-live")
    remove_race = asyncio.run(
        reserve_charge(
            removable_identity,
            execution_id="revocation-race-remove-before-external",
            feature_code="monitor_single",
            work_kind="monitoring.run",
            payload={"brand_id": 10, "race": "remove-before-external"},
            brand_id=10,
        )
    )
    remove_claim = claim_live_charge(removable_identity, charge_link_id=int(remove_race["id"]))
    owner = resolve_identity(1, request_id="owner-remove-race")
    set_member_status(owner, membership_id=removable_member_id, action="remove", reason="外调前移除竞态验证")
    try:
        mark_external_side_effect_started(
            charge_link_id=int(remove_race["id"]),
            claim_token=str(remove_claim["claim_token"]),
        )
        removal_stopped = False
    except OrganizationError as exc:
        removal_stopped = exc.code in {"ORG_MEMBERSHIP_INACTIVE", "ORG_IDENTITY_CHANGED"}
    verify.check(removal_stopped, "removed_membership_between_claim_and_external_stops_monitoring")
    asyncio.run(release_charge(charge_link_id=int(remove_race["id"]), reason="seat removed before provider"))
    verify.check(
        all(
            sql_value(dsn, "SELECT external_side_effect_started_at FROM organization_charge_links WHERE id=%s", (charge_id,)) is None
            for charge_id in (capability_race["id"], assignment_race["id"], suspend_race["id"], remove_race["id"])
        )
        and all(
            sql_value(dsn, "SELECT status FROM organization_charge_links WHERE id=%s", (charge_id,)) == "released"
            for charge_id in (capability_race["id"], assignment_race["id"], suspend_race["id"], remove_race["id"])
        )
        and billing_consistency()["ready"],
        "all_four_revocation_races_release_wallet_and_limit_legs_without_provider_marker",
        billing_consistency(),
    )
    member_a = resolve_identity(first_user, request_id="member-a-after-authority-races")

    # Mount the production GEO handler functions themselves into a throwaway
    # FastAPI app. Provider/LLM implementations are deterministic local fakes,
    # while the real route, organization guard, brand access, reservation,
    # settlement, artifact stamping and PostgreSQL isolation code all execute.
    execute_sql(
        dsn,
        """
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS brand_name TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS industry TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS industry_category TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS city TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS keywords JSONB;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS raw_data_json JSONB;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS level TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS brand_recognition_level TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS session_id TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS web_search_score INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS platform_score INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS content_quality_score INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS authority_score INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS brand_ownership_score INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS ai_visibility_score INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS ai_citation_score INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS update_frequency_score INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS ai_total_tests INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS ai_detected_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS ai_mention_rate REAL;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS ai_engines_tested TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS douyin_video_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS douyin_brand_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS xhs_note_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS xhs_brand_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS web_result_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS web_brand_direct_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS web_authority_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS scholar_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS has_brand_presence SMALLINT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS brand_account_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS competitor_count INTEGER;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS data_anomaly BOOLEAN;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS anomaly_reason TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS report_md_path TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS report_json_path TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS diagnosis_type TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS custom_questions TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS total_questions_tested INTEGER;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_verify_diagnosis_session_id
          ON diagnosis_records(session_id);
        ALTER TABLE brands ADD COLUMN IF NOT EXISTS industry_category TEXT;
        ALTER TABLE article_generations ADD COLUMN IF NOT EXISTS diagnosis_id BIGINT;
        ALTER TABLE article_generations ADD COLUMN IF NOT EXISTS task_id TEXT;
        ALTER TABLE article_generations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS diagnosis_id BIGINT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS company_intro TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS founding_year INTEGER;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS team_size TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS service_area TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS core_selling_points TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS unique_value TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS methodology TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS case_studies TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS pricing_tiers TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS testimonials TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS credentials TEXT;
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE client_materials ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS name TEXT;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS industry TEXT;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS company_intro TEXT;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS core_value TEXT;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS selling_points TEXT;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS success_cases TEXT;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS testimonials TEXT;
        ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS structured_knowledge TEXT;
        ALTER TABLE keyword_selection_sessions ADD COLUMN IF NOT EXISTS token TEXT;
        ALTER TABLE keyword_selection_sessions ADD COLUMN IF NOT EXISTS quote_id BIGINT;
        ALTER TABLE keyword_selection_sessions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'selecting';
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS diagnosis_id BIGINT;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS brand_name TEXT;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS industry TEXT;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS city TEXT;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS tier TEXT;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS target_share NUMERIC(8,4);
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS total_keywords INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS total_articles INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS monthly_price BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft';
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS markdown TEXT;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
        ALTER TABLE quotes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        CREATE TABLE IF NOT EXISTS topics(
          id BIGSERIAL PRIMARY KEY,
          quote_id BIGINT NOT NULL,
          optimized_title TEXT NOT NULL,
          original_keyword TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE topics ADD COLUMN IF NOT EXISTS keyword_id BIGINT;
        ALTER TABLE topics ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft';
        INSERT INTO client_profiles(id,brand_id,name,industry,is_deleted)
        VALUES (100,10,'席位资料同步档案','企业服务',0)
        ON CONFLICT(id) DO NOTHING;
        """,
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO diagnosis_records(brand_id,brand_name,industry,city,keywords,result_visibility)
            VALUES (10,'超长品牌名称用于员工席位隔离验证有限公司','企业服务','深圳',
                    %s::jsonb,'published') RETURNING id
            """,
            (json.dumps(["GEO优化", "AI搜索优化"], ensure_ascii=False),),
        )
        live_diagnosis_id = int(cursor.fetchone()["id"])
        stamp_artifact(
            cursor,
            member_a,
            artifact_type="diagnosis",
            artifact_id=live_diagnosis_id,
            brand_id=10,
        )

    os.environ["ROLE"] = ""
    # Import platform-sensitive binary dependencies before the temporary
    # scheduler-role platform projection below.
    import numpy  # noqa: F401
    _real_platform = sys.platform
    try:
        # ROLE unset exercises the repository's backward-compatible local
        # startup path. Present Linux only during import so Windows dev-mode
        # does not elect itself cron leader and start unrelated schedulers.
        sys.platform = "linux"
        import server as live_server
    finally:
        sys.platform = _real_platform
    import db.diagnosis_db as live_diagnosis_db
    import db.monitoring_db as live_monitoring_db
    import api.m3_material_confirm_api as live_m3_material_api
    import api.monitoring_api as live_monitoring_api
    import services.quote_pricing_preferences as live_quote_preferences
    import tools.batch_pricing as live_batch_pricing
    import writing.keyword_topic_generator as live_topic_generator

    decoy_schema = f"{schema}_column_decoy"
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(decoy_schema)))
        cursor.execute("CREATE TABLE organization_column_probe(id INTEGER)")
        cursor.execute(
            sql.SQL("CREATE TABLE {}.organization_column_probe(id INTEGER, owner_only TEXT)").format(
                sql.Identifier(decoy_schema)
            )
        )
        cursor.execute(
            sql.SQL("SET LOCAL search_path TO {}, {}").format(
                sql.Identifier(schema), sql.Identifier(decoy_schema)
            )
        )
        verify.check(
            not live_diagnosis_db._column_exists(cursor, "organization_column_probe", "owner_only"),
            "startup_column_probe_rejects_same_name_schema_decoy",
        )

    # Real diagnosis persistence for both product variants and both identity
    # modes.  This executes the production INSERT/UPSERT, brand link, artifact
    # stamp and pool-return path rather than replacing the DB function.
    def _diagnosis_results(session_id: str, diagnosis_type: str, *, trusted_brand_id=None, creator_user_id=None):
        return {
            "brand": "超长品牌名称用于员工席位隔离验证有限公司" if trusted_brand_id else f"旧单人保存验证-{session_id}",
            "industry": "企业服务",
            "keywords": ["GEO优化", "AI搜索优化"],
            "session_id": session_id,
            "brand_id": trusted_brand_id,
            "creator_user_id": creator_user_id,
            "data": {"diagnosis_type": diagnosis_type, "ai_visibility": {"test_questions": ["问题1"]}},
            "scores": {"total_score": 76, "dimension_scores": {"ai_visibility_score": 11}},
            "input_params": {"custom_questions": []},
            "report": "本地保存链验证",
        }

    sales_org_diagnosis_id = int(
        live_diagnosis_db.save_diagnosis(
            _diagnosis_results(
                "verify-save-sales-member-0001",
                "sales_lite",
                trusted_brand_id=10,
                creator_user_id=first_user,
            ),
            organization_identity=member_a,
        )
    )
    full_org_diagnosis_id = int(
        live_diagnosis_db.save_diagnosis(
            _diagnosis_results(
                "verify-save-full-member-0001",
                "technical_full",
                trusted_brand_id=10,
                creator_user_id=first_user,
            ),
            organization_identity=member_a,
        )
    )
    legacy_diagnosis_id = int(
        live_diagnosis_db.save_diagnosis(
            _diagnosis_results(
                "verify-save-legacy-0001",
                "technical_full",
                creator_user_id=1,
            )
        )
    )
    diagnosis_stamp_rows = int(
        sql_value(
            dsn,
            """
            SELECT COUNT(*) FROM diagnosis_records
            WHERE id IN (%s,%s) AND organization_id=%s
              AND created_by_membership_id=%s AND created_by_actor_kind='member'
              AND brand_id=10
            """,
            (sales_org_diagnosis_id, full_org_diagnosis_id, organization_id, member_a_id),
        )
    )
    pool_used_after_diagnosis = len(getattr(db_connection._pool, "_used", {}))
    verify.check(
        diagnosis_stamp_rows == 2
        and sql_value(dsn, "SELECT organization_id FROM diagnosis_records WHERE id=%s", (legacy_diagnosis_id,)) is None
        and pool_used_after_diagnosis == 0,
        "sales_and_full_diagnosis_real_save_member_and_legacy_stamp_and_return_connection",
        {"org_rows": diagnosis_stamp_rows, "pool_used": pool_used_after_diagnosis},
    )

    legacy_brand_id = int(
        sql_value(dsn, "SELECT brand_id FROM diagnosis_records WHERE id=%s", (legacy_diagnosis_id,))
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO client_profiles(id,brand_id,name,industry,is_deleted)
            VALUES (101,%s,'旧单人资料档案','企业服务',0)
            ON CONFLICT(id) DO NOTHING
            """,
            (legacy_brand_id,),
        )
        cursor.execute(
            "UPDATE client_profiles SET structured_knowledge='{\"differentiation\":{\"usp\":\"老板已确认价值\"}}' WHERE id=100"
        )

    base_materials = {
        "company_intro": "员工首次写入资料",
        "unique_value": "可信价值",
        "core_selling_points": [{"point": "真实卖点", "evidence": "可验证证据"}],
        "case_studies": [{"client": "案例客户", "result": "提升"}],
        "testimonials": [{"name": "客户", "quote": "可信"}],
        "structured_knowledge": {"differentiation": {"usp": "可信价值"}},
    }
    material_record_id = int(
        live_diagnosis_db.save_client_materials(
            sales_org_diagnosis_id,
            base_materials,
            organization_identity=member_a,
        )
    )
    duplicate_material_id = int(
        live_diagnosis_db.save_client_materials(
            sales_org_diagnosis_id,
            {**base_materials, "company_intro": "员工幂等更新资料"},
            organization_identity=member_a,
        )
    )
    verify.check(
        material_record_id == duplicate_material_id
        and int(
            sql_value(
                dsn,
                """
                SELECT COUNT(*) FROM client_materials
                WHERE id=%s AND organization_id=%s AND created_by_membership_id=%s
                  AND company_intro='员工幂等更新资料'
                """,
                (material_record_id, organization_id, member_a_id),
            )
        ) == 1
        and int(
            sql_value(
                dsn,
                """
                SELECT COUNT(*) FROM client_profiles
                WHERE id=100 AND organization_id=%s AND company_intro='员工幂等更新资料'
                """,
                (organization_id,),
            )
        ) == 1
        and sql_value(dsn, "SELECT (structured_knowledge::jsonb)->'differentiation'->>'usp' FROM client_profiles WHERE id=100") == "老板已确认价值",
        "member_material_insert_update_duplicate_is_atomic_and_stamped",
    )

    live_diagnosis_db.save_client_materials(
        legacy_diagnosis_id,
        {**base_materials, "company_intro": "旧单人资料仍兼容"},
    )
    owner_live = resolve_identity(1, request_id="owner-material-update")
    live_diagnosis_db.save_client_materials(
        full_org_diagnosis_id,
        {**base_materials, "company_intro": "老板资料更新"},
        organization_identity=owner_live,
    )
    verify.check(
        sql_value(dsn, "SELECT company_intro FROM client_profiles WHERE id=101") == "旧单人资料仍兼容"
        and sql_value(dsn, "SELECT company_intro FROM client_profiles WHERE id=100") == "老板资料更新",
        "legacy_and_owner_material_profile_sync_remain_supported",
    )

    with connect(dsn) as conn:
        cursor = conn.cursor()
        version = int(sql_value(dsn, "SELECT version FROM organization_memberships WHERE id=%s", (member_a_id,)))
        cursor.execute(
            """
            INSERT INTO organization_member_capability_overrides(
              membership_id,capability,effect,expected_membership_version,reason,created_by_user_id
            ) VALUES (%s,'clients.profile_edit','deny',%s,'material sync policy regression',1)
            ON CONFLICT(membership_id,capability) DO UPDATE SET effect='deny'
            """,
            (member_a_id, version),
        )
        cursor.execute("UPDATE organization_memberships SET version=version+1,capability_version=capability_version+1 WHERE id=%s", (member_a_id,))
        cursor.execute("UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s", (organization_id,))
        cursor.execute("INSERT INTO brands(id,owner_user_id,name,industry) VALUES (23,1,'空行业权限验证客户',NULL)")
    owner = resolve_identity(1, request_id="owner-assign-empty-industry-brand")
    assign_brands(
        owner,
        membership_id=member_a_id,
        brand_ids=[10, 23],
        request_id="assign-empty-industry-brand-23",
        reason="诊断不得越权回写行业",
    )
    member_without_profile_edit = resolve_identity(first_user, request_id="member-diagnosis-no-profile-edit")
    no_profile_edit_diagnosis_id = live_diagnosis_db.save_diagnosis(
        _diagnosis_results(
            "verify-save-no-profile-edit-0001",
            "sales_lite",
            trusted_brand_id=23,
            creator_user_id=first_user,
        ),
        organization_identity=member_without_profile_edit,
    )
    verify.check(
        int(no_profile_edit_diagnosis_id) > 0
        and sql_value(dsn, "SELECT industry FROM brands WHERE id=23") is None,
        "member_with_diagnosis_run_without_profile_edit_cannot_backfill_brand_industry",
    )
    profile_before_denied_sync = sql_value(dsn, "SELECT company_intro FROM client_profiles WHERE id=100")
    live_diagnosis_db.save_client_materials(
        sales_org_diagnosis_id,
        {**base_materials, "company_intro": "仅材料可写不可改档案"},
        organization_identity=member_a,
    )
    verify.check(
        sql_value(dsn, "SELECT company_intro FROM client_materials WHERE id=%s", (material_record_id,)) == "仅材料可写不可改档案"
        and sql_value(dsn, "SELECT company_intro FROM client_profiles WHERE id=100") == profile_before_denied_sync,
        "member_without_profile_edit_writes_material_but_cannot_sync_profile",
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM organization_member_capability_overrides WHERE membership_id=%s AND capability='clients.profile_edit'",
            (member_a_id,),
        )
        cursor.execute("UPDATE organization_memberships SET version=version+1,capability_version=capability_version+1 WHERE id=%s", (member_a_id,))
        cursor.execute("UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s", (organization_id,))
    member_a = resolve_identity(first_user, request_id="member-a-after-material-policy")

    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO brands(id,owner_user_id,name) VALUES (22,2,'其他租户资料诱饵')")
        cursor.execute(
            """
            INSERT INTO client_profiles(id,brand_id,name,industry,is_deleted,structured_knowledge)
            VALUES (102,22,'受害租户档案','企业服务',0,'{\"protected\":true}')
            """
        )
        cursor.execute("INSERT INTO diagnosis_records(brand_id,brand_name,industry) VALUES (22,'其他租户资料诱饵','企业服务') RETURNING id")
        cross_tenant_diagnosis_id = int(cursor.fetchone()["id"])
    try:
        live_diagnosis_db.save_client_materials(
            cross_tenant_diagnosis_id,
            {**base_materials, "company_intro": "越权写入"},
            organization_identity=member_a,
        )
        cross_tenant_rejected = False
    except OrganizationError as exc:
        cross_tenant_rejected = exc.code == "ORG_ARTIFACT_NOT_FOUND"
    verify.check(
        cross_tenant_rejected
        and int(sql_value(dsn, "SELECT COUNT(*) FROM client_materials WHERE diagnosis_id=%s", (cross_tenant_diagnosis_id,))) == 0,
        "material_write_cross_tenant_is_rejected_without_partial_row",
    )
    try:
        live_diagnosis_db.save_client_materials(
            cross_tenant_diagnosis_id,
            {**base_materials, "company_intro": "旧单人M3越权写入"},
            expected_brand_id=10,
        )
        legacy_expected_brand_rejected = False
    except OrganizationError as exc:
        legacy_expected_brand_rejected = exc.code == "ORG_ARTIFACT_NOT_FOUND"
    verify.check(
        legacy_expected_brand_rejected
        and int(sql_value(dsn, "SELECT COUNT(*) FROM client_materials WHERE diagnosis_id=%s", (cross_tenant_diagnosis_id,))) == 0,
        "legacy_material_sink_binds_authorized_brand_to_diagnosis_id",
    )
    original_m3_brand_access = live_m3_material_api.require_brand_access
    original_m3_rate_limit = live_m3_material_api._enforce_clean_rate_limit
    original_m3_brand_info = live_m3_material_api._get_brand_info
    original_m3_profile = live_m3_material_api._get_profile_for_brand
    original_m3_materials = live_m3_material_api._get_materials_for_brand
    original_m3_knowledge = live_m3_material_api._retrieve_client_knowledge_context
    original_m3_cleaner = live_m3_material_api._ai_clean_materials

    async def _fake_m3_knowledge(*_args, **_kwargs):
        return ""

    async def _fake_m3_cleaner(*_args, **_kwargs):
        return {**base_materials, "_llm_error": False}

    live_m3_material_api.require_brand_access = lambda _request, checked_brand_id: (
        True
        if int(checked_brand_id) == 10
        else (_ for _ in ()).throw(HTTPException(status_code=403, detail="brand denied"))
    )
    live_m3_material_api._enforce_clean_rate_limit = lambda *_args, **_kwargs: None
    live_m3_material_api._get_brand_info = lambda checked_brand_id: {"id": int(checked_brand_id), "name": "授权诱饵客户"}
    live_m3_material_api._get_profile_for_brand = lambda _brand_id: None
    live_m3_material_api._get_materials_for_brand = lambda _brand_id: None
    live_m3_material_api._retrieve_client_knowledge_context = _fake_m3_knowledge
    live_m3_material_api._ai_clean_materials = _fake_m3_cleaner
    m3_http_app = FastAPI()
    m3_http_app.add_exception_handler(OrganizationError, live_server._organization_error_handler)

    @m3_http_app.middleware("http")
    async def _m3_throwaway_user(request, call_next):
        request.state.user = {"id": 1, "user_id": 1, "is_admin": False}
        return await call_next(request)

    m3_http_app.include_router(live_m3_material_api.router)
    try:
        with TestClient(m3_http_app, raise_server_exceptions=False) as m3_client:
            m3_cross_brand_response = m3_client.post(
                "/api/m3/material-confirm/clean",
                json={
                    "brand_id": 10,
                    "diagnosis_id": cross_tenant_diagnosis_id,
                    "raw_text": "已授权诱饵客户的真实资料，不能写入另一个租户的诊断。",
                },
            )
    finally:
        live_m3_material_api.require_brand_access = original_m3_brand_access
        live_m3_material_api._enforce_clean_rate_limit = original_m3_rate_limit
        live_m3_material_api._get_brand_info = original_m3_brand_info
        live_m3_material_api._get_profile_for_brand = original_m3_profile
        live_m3_material_api._get_materials_for_brand = original_m3_materials
        live_m3_material_api._retrieve_client_knowledge_context = original_m3_knowledge
        live_m3_material_api._ai_clean_materials = original_m3_cleaner
    verify.check(
        m3_cross_brand_response.status_code == 404
        and m3_cross_brand_response.json().get("detail", {}).get("code") == "ORG_ARTIFACT_NOT_FOUND"
        and int(sql_value(dsn, "SELECT COUNT(*) FROM client_materials WHERE diagnosis_id=%s", (cross_tenant_diagnosis_id,))) == 0
        and sql_value(dsn, "SELECT (structured_knowledge::jsonb)->>'protected' FROM client_profiles WHERE id=102") == "true",
        "m3_http_authorized_decoy_brand_cannot_write_independent_victim_diagnosis",
        m3_cross_brand_response.json(),
    )

    material_before_forced_failure = sql_value(
        dsn,
        "SELECT company_intro FROM client_materials WHERE id=%s",
        (material_record_id,),
    )
    execute_sql(
        dsn,
        """
        CREATE OR REPLACE FUNCTION verify_reject_profile_sync() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.company_intro='FORCED_PROFILE_SYNC_FAILURE' THEN
            RAISE EXCEPTION 'ROLLBACK_SENTINEL';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER verify_reject_profile_sync_trigger
          BEFORE UPDATE ON client_profiles
          FOR EACH ROW EXECUTE FUNCTION verify_reject_profile_sync();
        """,
    )
    try:
        live_diagnosis_db.save_client_materials(
            sales_org_diagnosis_id,
            {**base_materials, "company_intro": "FORCED_PROFILE_SYNC_FAILURE"},
            organization_identity=member_a,
        )
        forced_failure_rolled_back = False
    except Exception as exc:
        forced_failure_rolled_back = "ROLLBACK_SENTINEL" in str(exc)
    verify.check(
        forced_failure_rolled_back
        and sql_value(dsn, "SELECT company_intro FROM client_materials WHERE id=%s", (material_record_id,))
        == material_before_forced_failure
        and len(getattr(db_connection._pool, "_used", {})) == 0,
        "profile_sync_failure_rolls_back_material_and_returns_connection",
    )
    execute_sql(dsn, "DROP TRIGGER verify_reject_profile_sync_trigger ON client_profiles; DROP FUNCTION verify_reject_profile_sync()")

    material_call_ledger = []
    for relative in ("server.py", "workflows/diagnosis_workflow.py", "api/m3_material_confirm_api.py"):
        source_path = ROOT / relative
        source_tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(source_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "save_client_materials":
                material_call_ledger.append(
                    (relative, node.lineno, any(keyword.arg == "organization_identity" for keyword in node.keywords))
                )
    verify.check(
        len(material_call_ledger) == 4 and all(entry[2] for entry in material_call_ledger),
        "every_save_client_materials_callsite_explicitly_carries_identity_policy",
        material_call_ledger,
    )
    diagnosis_source = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    server_source = (ROOT / "server.py").read_text(encoding="utf-8")
    workflow_source = (ROOT / "workflows" / "diagnosis_workflow.py").read_text(encoding="utf-8")
    m3_source = (ROOT / "api" / "m3_material_confirm_api.py").read_text(encoding="utf-8")
    verify.check(
        "def save_diagnosis(results: dict, *, organization_identity=None)" in diagnosis_source
        and "sync_profiles_allowed = organization_identity is None" in diagnosis_source
        and "organization_identity=organization_identity" in server_source
        and workflow_source.count("organization_identity=organization_identity") >= 3,
        "identity_plumbing_and_new_local_variables_static_regression_gate",
    )
    verify.check(
        "owner_user_id=creator_user_id" not in workflow_source
        and workflow_source.count("owner_user_id=effective_owner_user_id") >= 8
        and workflow_source.count("if organization_identity is not None\n                    else get_or_create_brand") >= 1
        and "expected_brand_id=brand_id" in m3_source,
        "organization_workflow_uses_owner_principal_namespace_and_m3_binds_diagnosis_brand",
    )

    async def _fake_generate_batch_quote(**kwargs):
        keyword_rows = [{"keyword": value} for value in kwargs.get("keywords", [])]
        return (
            {
                "keywords": keyword_rows,
                "total_keywords": len(keyword_rows),
                "total_articles": len(keyword_rows) * 2,
                "final_price": 8800,
                "unavailable_keywords": [],
            },
            "# 本地验证报价\n\n服务端确定性产物",
        )

    live_batch_pricing.generate_batch_quote = _fake_generate_batch_quote
    live_quote_preferences.get_quote_markup_for_quote_viewer = lambda _user_id: 1.0
    live_quote_preferences.get_cost_per_article_for_quote_viewer = lambda _user_id: None
    live_quote_preferences.get_procurement_cost_multiplier_for_quote_viewer = lambda _user_id: 1.0
    live_quote_preferences.get_cumulative_media_procurement_multiplier = lambda _user_id: 1.0
    live_quote_preferences.is_default_quote_pricing = lambda _user_id: True

    def _fake_writing_detail(quote_id: int):
        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM quotes WHERE id=%s", (quote_id,))
            quote = cursor.fetchone()
        if not quote:
            return None
        return {
            "quote": dict(quote),
            "keywords": [{"id": 9001, "keyword": "GEO优化", "required_articles": 1}],
            "topics": [],
            "articles": [],
        }

    def _fake_save_topics(quote_id: int, topics: list[dict], *, cursor=None):
        if cursor is None:
            with connect(dsn) as conn:
                return _fake_save_topics(quote_id, topics, cursor=conn.cursor())
        ids = []
        for topic in topics:
            cursor.execute(
                """
                INSERT INTO topics(quote_id,optimized_title,original_keyword)
                VALUES (%s,%s,%s) RETURNING id
                """,
                (quote_id, topic["optimized_title"], topic["original_keyword"]),
            )
            ids.append(int(cursor.fetchone()["id"]))
        return ids

    live_diagnosis_db.get_writing_project_detail = _fake_writing_detail
    live_diagnosis_db.save_topics_batch = _fake_save_topics
    live_diagnosis_db.update_writing_status = lambda *_args, **_kwargs: True

    class _FakeTopicGenerator:
        def __init__(self, **_kwargs):
            pass

        async def generate(self):
            return [
                {
                    "keyword_id": 9001,
                    "optimized_title": "企业如何建立可验证的 GEO 增长闭环",
                    "original_keyword": "GEO优化",
                }
            ]

    live_topic_generator.KeywordTopicGenerator = _FakeTopicGenerator
    live_monitoring_api._resolve_brand_and_quotes = lambda _request: (10, [])
    live_monitoring_db.brand_has_confirmed_or_paid_quote = lambda _brand_id: False
    live_monitoring_db.get_keywords_for_monitoring = lambda **_kwargs: [
        {
            "id": 7001,
            "keyword": "GEO优化",
            "source": "extra",
            "target_brand": "本地验证品牌",
            "entitlement_platforms": "dashscope,deepseek,kimi,doubao",
        }
    ]

    monitoring_provider_calls = {"count": 0}

    async def _fake_run_monitoring(
        _request,
        *,
        organization_identity=None,
        task_created_callback=None,
        before_first_provider=None,
        **_kwargs,
    ):
        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO monitoring_tasks(brand_id) VALUES (10) RETURNING id")
            task_id = int(cursor.fetchone()["id"])
        if task_created_callback is not None:
            callback_result = task_created_callback(task_id)
            if asyncio.iscoroutine(callback_result):
                await callback_result
        if before_first_provider is not None:
            boundary_result = before_first_provider()
            if asyncio.iscoroutine(boundary_result):
                await boundary_result
        monitoring_provider_calls["count"] += 1
        return {"status": "success", "task_id": task_id, "total_tests": 1}

    live_server.api_run_monitoring = _fake_run_monitoring

    def _fake_generate_report(*, brand_id=None, client_id=None, report_type="weekly", organization_identity=None):
        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO monitoring_reports(brand_id) VALUES (%s) RETURNING id", (brand_id,))
            report_id = int(cursor.fetchone()["id"])
            stamp_artifact(
                cursor,
                organization_identity,
                artifact_type="monitoring_report",
                artifact_id=report_id,
                brand_id=brand_id,
            )
        return {"status": "success", "report_id": report_id, "report_type": report_type}

    live_server.api_generate_report = _fake_generate_report

    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO diagnosis_records(brand_id,brand_name,industry,city,keywords,result_visibility)
            VALUES (10,'共享客户资料验证','企业服务','深圳',%s::jsonb,'published')
            RETURNING id
            """,
            (json.dumps(["客户共享资料"], ensure_ascii=False),),
        )
        shared_material_diagnosis_id = int(cursor.fetchone()["id"])
        stamp_artifact(
            cursor,
            member_b,
            artifact_type="diagnosis",
            artifact_id=shared_material_diagnosis_id,
            brand_id=10,
        )

    live_diagnosis_db.get_client_materials = lambda diagnosis_id: (
        {"diagnosis_id": int(diagnosis_id), "company_intro": "服务端客户资料"}
        if int(diagnosis_id) == shared_material_diagnosis_id
        else None
    )

    def _fake_list_monitoring_keywords(*, brand_id=None, client_id=None, status="active"):
        rows = [
            {"id": 7101, "source": "extra", "brand_id": 10, "keyword": "已分配客户词"},
            {"id": 7201, "source": "extra", "brand_id": 20, "keyword": "其他租户诱饵词"},
        ]
        if brand_id is not None:
            rows = [row for row in rows if int(row["brand_id"]) == int(brand_id)]
        return {"status": "success", "keywords": rows, "count": len(rows)}

    live_server.api_get_keywords = _fake_list_monitoring_keywords
    live_server.api_get_client_keywords_merged = lambda quote_id: {
        "status": "success",
        "quote_id": int(quote_id),
        "keywords": [{"keyword": "服务范围共享词"}],
    }
    live_server.api_get_keyword = lambda keyword_id: {
        "status": "success",
        "keyword": {"id": int(keyword_id), "keyword": "详情授权词"},
    }

    live_app = FastAPI()
    live_app.add_exception_handler(OrganizationError, live_server._organization_error_handler)

    @live_app.get("/api/organization/_verify-live-context")
    async def _verify_live_context(request: Request):
        identity = getattr(request.state, "organization_identity", None)
        return {
            "actor_kind": getattr(identity, "actor_kind", None),
            "actor_user_id": getattr(identity, "actor_user_id", None),
            "seats_enabled": feature_flags()["ORGANIZATION_SEATS_ENABLED"],
        }

    live_app.add_api_route(
        "/api/diagnosis/{diagnosis_id}/generate-quote",
        live_server.api_generate_quote_from_diagnosis,
        methods=["POST"],
    )
    live_app.add_api_route(
        "/api/quotes/{quote_id}",
        live_server.api_get_quote,
        methods=["GET"],
    )
    live_app.add_api_route(
        "/api/writing/generate-titles",
        live_server.api_generate_titles,
        methods=["POST"],
    )
    live_app.add_api_route(
        "/api/monitoring/run",
        live_server.run_monitoring,
        methods=["POST"],
    )
    live_app.add_api_route(
        "/api/reports/generate",
        live_server.generate_report_endpoint,
        methods=["POST"],
    )
    live_app.add_api_route(
        "/api/materials/{diagnosis_id}",
        live_server.api_get_materials,
        methods=["GET"],
    )
    live_app.add_api_route(
        "/api/materials/{diagnosis_id}",
        live_server.api_save_materials,
        methods=["POST", "PUT"],
    )
    from api.brand_api import delete_client as live_delete_client, restore_deleted_client as live_restore_client
    live_app.add_api_route(
        "/api/my-clients/{brand_id}",
        live_delete_client,
        methods=["DELETE"],
    )
    live_app.add_api_route(
        "/api/my-clients/{brand_id}/restore",
        live_restore_client,
        methods=["POST"],
    )
    live_app.add_api_route(
        "/api/monitoring/keywords",
        live_server.get_monitoring_keywords,
        methods=["GET"],
    )
    live_app.add_api_route(
        "/api/monitoring/keywords/{keyword_id}",
        live_server.get_monitoring_keyword,
        methods=["GET"],
    )
    live_app.add_api_route(
        "/api/monitoring/clients/{quote_id}/keywords",
        live_server.get_client_keywords_list,
        methods=["GET"],
    )
    live_app.add_api_websocket_route(
        "/ws/progress/{session_id}",
        live_server.websocket_endpoint,
    )
    setup_organization_guard(live_app)
    live_app.add_middleware(_ThrowawayAuthMiddleware)
    member_a_http = {
        "x-throwaway-user-id": str(first_user),
        "x-request-id": "http-live-geo-core-0001",
    }
    member_b_http = {
        "x-throwaway-user-id": str(member_b_user),
        "x-request-id": "http-live-geo-other-member-0001",
    }
    original_live_decode_jwt = organization_ws_jwt.decode_jwt
    original_ws_session_authorizer = live_server._ws_authorize_session
    original_ws_organization_authorizer = live_server._ws_authorize_organization_session
    original_ws_user_checker = live_server._ws_check_user_active
    organization_ws_jwt.decode_jwt = lambda _token: {
        "user_id": first_user,
        "perm_version": member_permission_version,
    }
    live_server._ws_authorize_session = lambda _user, _session_id: True
    def _throwaway_ws_organization_authorizer(user, _session_id):
        try:
            return resolve_identity(int(user["user_id"]), request_id="live-ws-recheck") is not None
        except OrganizationError:
            return False
    live_server._ws_authorize_organization_session = _throwaway_ws_organization_authorizer
    live_server._ws_check_user_active = lambda _user, **_kwargs: True
    before_live_wallet = int(sql_value(dsn, "SELECT paid_points FROM user_wallets WHERE user_id=1"))
    with TestClient(live_app, raise_server_exceptions=False) as client:
        legacy_permission_before_delete = int(
            sql_value(dsn, "SELECT permission_version FROM users WHERE id=70")
        )
        legacy_soft_delete = client.delete(
            "/api/my-clients/71?reason=管理员范围软删撤权验证",
            headers={
                "x-throwaway-user-id": "71",
                "x-request-id": "legacy-soft-delete-revoke-0001",
            },
        )
        legacy_scope_after_delete = get_admin_client_scope(70)
        verify.check(
            legacy_soft_delete.status_code == 200
            and sql_value(dsn, "SELECT COUNT(*) FROM user_clients WHERE user_id=70 AND brand_id=71") == 0
            and int(sql_value(dsn, "SELECT permission_version FROM users WHERE id=70"))
                == legacy_permission_before_delete + 1
            and 71 not in legacy_scope_after_delete["effective_brand_ids"]
            and 71 not in {row["id"] for row in legacy_scope_after_delete["available_brands"]},
            "admin_client_scope_soft_delete_revokes_legacy_access_same_transaction",
            {"response": legacy_soft_delete.json(), "scope": legacy_scope_after_delete},
        )

        context_response = client.get(
            "/api/organization/_verify-live-context",
            headers=member_a_http,
        )
        verify.check(
            context_response.status_code == 200
            and context_response.json() == {
                "actor_kind": "member",
                "actor_user_id": first_user,
                "seats_enabled": True,
            },
            "http_live_guard_injects_member_identity_before_core_work",
            context_response.json(),
        )
        query_token_close_code = None
        try:
            with client.websocket_connect(
                "/ws/progress/http-live-diagnosis-0001?token=throwaway-live-ws-token-0001"
            ):
                pass
        except Exception as exc:
            query_token_close_code = getattr(exc, "code", None)
        verify.check(
            query_token_close_code == 4403,
            "diagnosis_websocket_member_query_token_is_forbidden",
            query_token_close_code,
        )
        ws_close_code = None
        try:
            with client.websocket_connect(
                "/ws/progress/http-live-diagnosis-0001",
                subprotocols=["omnirank-auth", "throwaway-live-ws-token-0001"],
            ) as websocket:
                with connect(dsn) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        UPDATE organization_memberships
                        SET status='suspended',capability_version=capability_version+1,
                            version=version+1,updated_at=NOW()
                        WHERE id=%s
                        """,
                        (member_a_id,),
                    )
                    cursor.execute(
                        "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
                        (organization_id,),
                    )
                try:
                    websocket.receive_json()
                except Exception as exc:
                    ws_close_code = getattr(exc, "code", None)
        finally:
            with connect(dsn) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE organization_memberships
                    SET status='active',capability_version=capability_version+1,
                        version=version+1,updated_at=NOW()
                    WHERE id=%s
                    """,
                    (member_a_id,),
                )
                cursor.execute(
                    "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
                    (organization_id,),
                )
        verify.check(
            ws_close_code == 4003,
            "diagnosis_websocket_rechecks_open_connection_after_seat_suspension",
            ws_close_code,
        )
        quote_response = client.post(
            f"/api/diagnosis/{live_diagnosis_id}/generate-quote",
            headers=member_a_http,
        )
        quote_payload = quote_response.json()
        verify.check(
            quote_response.status_code == 200 and quote_payload.get("success") is True,
            "http_live_quote_handler_member_authorized",
            quote_payload,
        )
        live_quote_id = int(quote_payload["quote_id"])
        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO extra_keywords(quote_id,client_id,brand_id,keyword,target_brand)
                VALUES (%s,%s,10,'已分配详情词','本地验证品牌') RETURNING id
                """,
                (live_quote_id, str(live_quote_id)),
            )
            assigned_keyword_id = int(cursor.fetchone()["id"])
            cursor.execute(
                "INSERT INTO brands(id,owner_user_id,name) VALUES (20,1,'未分配客户诱饵')"
            )
            cursor.execute(
                "INSERT INTO quotes(brand_id,brand_name) VALUES (20,'未分配客户诱饵') RETURNING id"
            )
            unassigned_quote_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO extra_keywords(quote_id,client_id,brand_id,keyword,target_brand)
                VALUES (%s,%s,20,'未分配详情词','未分配客户诱饵') RETURNING id
                """,
                (unassigned_quote_id, str(unassigned_quote_id)),
            )
            unassigned_keyword_id = int(cursor.fetchone()["id"])
        quote_charge_count = int(
            sql_value(
                dsn,
                "SELECT COUNT(*) FROM organization_charge_links c JOIN organization_work_outbox o ON o.charge_link_id=c.id WHERE o.work_kind='quote.create' AND c.actor_user_id=%s AND c.status='committed' AND c.task_ref=%s",
                (first_user, f"diagnosis:{live_diagnosis_id}"),
            )
        )
        verify.check(
            quote_charge_count == 1
            and int(sql_value(dsn, "SELECT paid_points FROM user_wallets WHERE user_id=1")) == before_live_wallet - 40
            and int(sql_value(dsn, "SELECT COUNT(*) FROM organization_charge_limit_links j JOIN organization_charge_links c ON c.id=j.charge_link_id JOIN organization_work_outbox o ON o.charge_link_id=c.id WHERE o.work_kind='quote.create' AND j.consumed_points=40")) == 4,
            "http_live_quote_charges_owner_once_and_advances_four_member_buckets",
        )
        quote_replay = client.post(
            f"/api/diagnosis/{live_diagnosis_id}/generate-quote",
            headers=member_a_http,
        )
        verify.check(
            quote_replay.status_code == 200
            and quote_replay.json().get("existing") is True
            and int(sql_value(dsn, "SELECT COUNT(*) FROM organization_charge_links c JOIN organization_work_outbox o ON o.charge_link_id=c.id WHERE o.work_kind='quote.create' AND c.status='committed' AND c.task_ref=%s", (f"diagnosis:{live_diagnosis_id}",))) == 1,
            "http_live_quote_retry_does_not_double_charge",
            quote_replay.json(),
        )
        other_quote = client.get(f"/api/quotes/{live_quote_id}", headers=member_b_http)
        verify.check(
            other_quote.status_code in {403, 404},
            "http_other_member_quote_detail_is_not_visible",
            other_quote.json(),
        )
        other_write = client.post(
            "/api/writing/generate-titles",
            headers=member_b_http,
            json={"quote_id": live_quote_id},
        )
        verify.check(
            other_write.status_code in {403, 404}
            and int(sql_value(dsn, "SELECT COUNT(*) FROM organization_charge_links c JOIN organization_work_outbox o ON o.charge_link_id=c.id WHERE o.work_kind='writing.generate_titles'")) == 0,
            "http_other_member_cannot_write_or_charge_against_private_quote",
            other_write.json(),
        )
        titles_response = client.post(
            "/api/writing/generate-titles",
            headers={**member_a_http, "x-request-id": "http-live-titles-0001"},
            json={"quote_id": live_quote_id},
        )
        verify.check(
            titles_response.status_code == 200
            and titles_response.json().get("success") is True
            and int(sql_value(dsn, "SELECT COUNT(*) FROM organization_charge_links c JOIN organization_work_outbox o ON o.charge_link_id=c.id WHERE o.work_kind='writing.generate_titles' AND c.status='committed'")) == 1,
            "http_live_writing_handler_reserves_settles_and_persists",
            titles_response.json(),
        )
        shared_client_keywords = client.get(
            f"/api/monitoring/clients/{live_quote_id}/keywords",
            headers={**member_b_http, "x-request-id": "http-live-client-keywords-shared-0001"},
        )
        unassigned_client_keywords = client.get(
            f"/api/monitoring/clients/{unassigned_quote_id}/keywords",
            headers={**member_a_http, "x-request-id": "http-live-client-keywords-denied-0001"},
        )
        assigned_keyword_detail = client.get(
            f"/api/monitoring/keywords/{assigned_keyword_id}?source=extra",
            headers={**member_a_http, "x-request-id": "http-live-keyword-detail-0001"},
        )
        unassigned_keyword_detail = client.get(
            f"/api/monitoring/keywords/{unassigned_keyword_id}?source=extra",
            headers={**member_a_http, "x-request-id": "http-live-keyword-detail-denied-0001"},
        )
        verify.check(
            shared_client_keywords.status_code == 200
            and shared_client_keywords.json().get("keywords", [{}])[0].get("keyword")
            == "服务范围共享词"
            and unassigned_client_keywords.status_code in {403, 404}
            and assigned_keyword_detail.status_code == 200
            and unassigned_keyword_detail.status_code in {403, 404},
            "http_monitoring_client_scope_and_keyword_detail_enforce_assignment",
            {
                "shared": shared_client_keywords.json(),
                "unassigned_client": unassigned_client_keywords.json(),
                "assigned_detail": assigned_keyword_detail.json(),
                "unassigned_detail": unassigned_keyword_detail.json(),
            },
        )
        monitoring_response = client.post(
            "/api/monitoring/run",
            headers={**member_a_http, "x-request-id": "http-live-monitor-0001"},
            json={"brand_id": 10, "platforms": ["dashscope"]},
        )
        monitoring_payload = monitoring_response.json()
        monitoring_wallet_after_first = sql_value(
            dsn,
            "SELECT json_build_object('paid',paid_points,'bonus',bonus_points,'frozen',frozen_points) FROM user_wallets WHERE user_id=1",
        )
        with connect(dsn) as conn:
            conn.cursor().execute(
                "UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s",
                (organization_id,),
            )
        original_monitoring_keyword_resolver = live_monitoring_db.get_keywords_for_monitoring
        live_monitoring_db.get_keywords_for_monitoring = lambda **_kwargs: []
        try:
            monitoring_replay_response = client.post(
                "/api/monitoring/run",
                headers={**member_a_http, "x-request-id": "http-live-monitor-0001"},
                json={"brand_id": 10, "platforms": ["dashscope"]},
            )
            monitoring_conflict_response = client.post(
                "/api/monitoring/run",
                headers={**member_a_http, "x-request-id": "http-live-monitor-0001"},
                json={"brand_id": 10, "platforms": ["deepseek"]},
            )
        finally:
            live_monitoring_db.get_keywords_for_monitoring = original_monitoring_keyword_resolver
        monitoring_charge_id = int(
            sql_value(
                dsn,
                "SELECT c.id FROM organization_charge_links c JOIN organization_work_outbox o ON o.charge_link_id=c.id WHERE o.work_kind='monitoring.run' AND c.status='committed' ORDER BY c.id DESC LIMIT 1",
            )
        )
        from services.organization_billing import get_committed_charge_result
        replay_refund_identity = resolve_identity(first_user, request_id="monitor-replay-refund-race")

        def _replay_or_zero_refund(worker_no: int):
            try:
                if worker_no % 2 == 0:
                    return get_committed_charge_result(
                        replay_refund_identity,
                        charge_link_id=monitoring_charge_id,
                    )
                return asyncio.run(
                    refund_charge(
                        charge_link_id=monitoring_charge_id,
                        cumulative_refund_target=0,
                        refund_request_id=f"monitor-zero-refund-race-{worker_no:02d}",
                        reason="监测重放与退款固定锁序验证",
                        expected_organization_id=organization_id,
                    )
                )
            except Exception as exc:
                return exc

        with ThreadPoolExecutor(max_workers=20) as executor:
            replay_refund_results = list(executor.map(_replay_or_zero_refund, range(20)))
        verify.check(
            monitoring_response.status_code == 200
            and monitoring_payload.get("status") == "success"
            and monitoring_replay_response.status_code == 200
            and monitoring_replay_response.json().get("task_id") == monitoring_payload.get("task_id")
            and monitoring_provider_calls["count"] == 1
            and not any(isinstance(item, Exception) for item in replay_refund_results)
            and not any("deadlock" in str(item).lower() for item in replay_refund_results)
            and monitoring_conflict_response.status_code == 409
            and monitoring_conflict_response.json().get("detail", {}).get("code") == "ORG_IDEMPOTENCY_CONFLICT"
            and sql_value(
                dsn,
                "SELECT json_build_object('paid',paid_points,'bonus',bonus_points,'frozen',frozen_points) FROM user_wallets WHERE user_id=1",
            ) == monitoring_wallet_after_first
            and int(sql_value(dsn, "SELECT COUNT(*) FROM organization_charge_links c JOIN organization_work_outbox o ON o.charge_link_id=c.id WHERE o.work_kind='monitoring.run' AND c.status='committed'")) == 1
            and int(sql_value(dsn, "SELECT COUNT(*) FROM monitoring_tasks WHERE id=%s AND organization_id=%s AND created_by_membership_id=%s", (monitoring_payload.get("task_id"), organization_id, member_a_id))) == 1,
            "http_live_monitoring_handler_reserves_settles_and_stamps",
            {
                "first": monitoring_payload,
                "replay": monitoring_replay_response.json(),
                "conflict": monitoring_conflict_response.json(),
                "provider_calls": monitoring_provider_calls["count"],
            },
        )

        # Real route race: revoke the assignment after the production handler
        # has claimed its reservation but before it marks the provider boundary.
        import services.organization_billing as live_organization_billing
        original_claim_live_charge = live_organization_billing.claim_live_charge
        original_monitoring_provider = live_server.api_run_monitoring
        route_race_provider_calls = {"count": 0}

        def _claim_then_revoke_assignment(identity, **kwargs):
            claimed = original_claim_live_charge(identity, **kwargs)
            with connect(dsn) as race_conn:
                race_cursor = race_conn.cursor()
                race_cursor.execute(
                    "UPDATE organization_brand_assignments SET status='revoked',revoked_at=NOW(),version=version+1 WHERE organization_id=%s AND membership_id=%s AND brand_id=10 AND status='active'",
                    (organization_id, member_a_id),
                )
                race_cursor.execute("UPDATE organization_memberships SET version=version+1,assignment_version=assignment_version+1 WHERE id=%s", (member_a_id,))
                race_cursor.execute("UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s", (organization_id,))
            return claimed

        async def _provider_must_not_run_after_revoke(
            _request, *, organization_identity=None, before_first_provider=None, **_kwargs,
        ):
            if before_first_provider is not None:
                boundary_result = before_first_provider()
                if asyncio.iscoroutine(boundary_result):
                    await boundary_result
            route_race_provider_calls["count"] += 1
            return {"status": "success", "task_id": 909090}

        frozen_before_route_race = int(sql_value(dsn, "SELECT frozen_points FROM user_wallets WHERE user_id=1"))
        limit_reserved_before_route_race = int(
            sql_value(dsn, "SELECT COALESCE(SUM(reserved_points),0) FROM organization_spend_limits WHERE membership_id=%s", (member_a_id,))
        )
        live_organization_billing.claim_live_charge = _claim_then_revoke_assignment
        live_server.api_run_monitoring = _provider_must_not_run_after_revoke
        try:
            route_race_response = client.post(
                "/api/monitoring/run",
                headers={**member_a_http, "x-request-id": "http-live-monitor-revoke-after-claim-0001"},
                json={"brand_id": 10, "platforms": ["deepseek"]},
            )
        finally:
            live_organization_billing.claim_live_charge = original_claim_live_charge
            live_server.api_run_monitoring = original_monitoring_provider
            with connect(dsn) as restore_conn:
                restore_cursor = restore_conn.cursor()
                restore_cursor.execute(
                    """
                    UPDATE organization_brand_assignments
                    SET status='active',revoked_at=NULL,version=version+1
                    WHERE id=(
                        SELECT id FROM organization_brand_assignments
                        WHERE organization_id=%s AND membership_id=%s AND brand_id=10
                        ORDER BY id DESC LIMIT 1
                    )
                    """,
                    (organization_id, member_a_id),
                )
                restore_cursor.execute("UPDATE organization_memberships SET version=version+1,assignment_version=assignment_version+1 WHERE id=%s", (member_a_id,))
                restore_cursor.execute("UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s", (organization_id,))
        route_race_charge = sql_value(
            dsn,
            """
            SELECT json_build_object(
              'status',c.status,'external',c.external_side_effect_started_at,
              'outbox_external',o.external_side_effect_started_at
            )
            FROM organization_charge_links c
            JOIN organization_work_outbox o ON o.charge_link_id=c.id
            WHERE o.work_kind='monitoring.run' AND c.actor_user_id=%s
            ORDER BY c.id DESC LIMIT 1
            """,
            (first_user,),
        )
        verify.check(
            route_race_response.status_code in {403, 409}
            and route_race_response.json().get("detail", {}).get("code")
            in {"ORG_BRAND_NOT_ASSIGNED", "ORG_CHARGE_AUTHORITY_CHANGED"}
            and route_race_provider_calls["count"] == 0
            and route_race_charge["status"] == "released"
            and route_race_charge["external"] is None
            and route_race_charge["outbox_external"] is None
            and int(sql_value(dsn, "SELECT frozen_points FROM user_wallets WHERE user_id=1")) == frozen_before_route_race
            and int(sql_value(dsn, "SELECT COALESCE(SUM(reserved_points),0) FROM organization_spend_limits WHERE membership_id=%s", (member_a_id,))) == limit_reserved_before_route_race,
            "http_monitoring_assignment_revoke_after_claim_stops_provider_and_releases_both_legs",
            {"response": route_race_response.json(), "charge": route_race_charge},
        )
        report_response = client.post(
            "/api/reports/generate?brand_id=10&report_type=weekly",
            headers={**member_a_http, "x-request-id": "http-live-report-0001"},
        )
        report_payload = report_response.json()
        verify.check(
            report_response.status_code == 200
            and report_payload.get("status") == "success"
            and int(sql_value(dsn, "SELECT COUNT(*) FROM monitoring_reports WHERE id=%s AND organization_id=%s AND created_by_membership_id=%s", (report_payload.get("report_id"), organization_id, member_a_id))) == 1,
            "http_live_report_handler_enforces_assignment_and_stamps",
            report_payload,
        )
        shared_material_response = client.get(
            f"/api/materials/{shared_material_diagnosis_id}",
            headers={**member_a_http, "x-request-id": "http-live-shared-material-0001"},
        )
        verify.check(
            shared_material_response.status_code == 200
            and shared_material_response.json().get("data", {}).get("company_intro")
            == "服务端客户资料",
            "http_assigned_member_reads_customer_material_from_other_creator",
            shared_material_response.json(),
        )
        material_http_response = client.post(
            f"/api/materials/{sales_org_diagnosis_id}",
            headers={**member_a_http, "x-request-id": "http-live-material-update-0001"},
            json={
                "company_intro": "HTTP 员工资料更新",
                "unique_value": "HTTP 原子价值",
                "core_selling_points": [{"point": "HTTP卖点", "evidence": "HTTP证据"}],
            },
        )
        verify.check(
            material_http_response.status_code == 200
            and material_http_response.json().get("success") is True
            and sql_value(dsn, "SELECT company_intro FROM client_materials WHERE id=%s", (material_record_id,)) == "HTTP 员工资料更新"
            and sql_value(dsn, "SELECT company_intro FROM client_profiles WHERE id=100") == "HTTP 员工资料更新",
            "http_member_material_response_matches_atomic_material_and_profile_commit",
            material_http_response.json(),
        )
        execute_sql(
            dsn,
            """
            CREATE OR REPLACE FUNCTION verify_reject_profile_sync() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.company_intro='HTTP_FORCED_PROFILE_FAILURE' THEN
                RAISE EXCEPTION 'ROLLBACK_SENTINEL';
              END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER verify_reject_profile_sync_trigger
              BEFORE UPDATE ON client_profiles
              FOR EACH ROW EXECUTE FUNCTION verify_reject_profile_sync();
            """,
        )
        material_before_http_failure = sql_value(dsn, "SELECT company_intro FROM client_materials WHERE id=%s", (material_record_id,))
        try:
            material_http_failure = client.put(
                f"/api/materials/{sales_org_diagnosis_id}",
                headers={**member_a_http, "x-request-id": "http-live-material-failure-0001"},
                json={"company_intro": "HTTP_FORCED_PROFILE_FAILURE"},
            )
        finally:
            execute_sql(dsn, "DROP TRIGGER verify_reject_profile_sync_trigger ON client_profiles; DROP FUNCTION verify_reject_profile_sync()")
        material_http_failure_body = material_http_failure.json()
        verify.check(
            material_http_failure.status_code == 500
            and material_http_failure_body.get("detail", {}).get("code") == "MATERIAL_SAVE_FAILED"
            and "ROLLBACK_SENTINEL" not in json.dumps(material_http_failure_body, ensure_ascii=False)
            and sql_value(dsn, "SELECT company_intro FROM client_materials WHERE id=%s", (material_record_id,)) == material_before_http_failure,
            "http_material_failure_is_non_2xx_safe_and_has_no_committed_write",
            material_http_failure_body,
        )

        # Brand tombstone, automatic plan cancellation and wallet recovery are
        # one durable state machine.  Force the eager release handoff to fail so
        # the outbox recovery proof covers the commit-after-delete crash window.
        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO brands(id,owner_user_id,name,industry) VALUES (21,1,'软删自动计划客户','企业服务')")
            cursor.execute(
                """
                INSERT INTO extra_keywords(quote_id,client_id,brand_id,keyword,target_brand)
                VALUES (NULL,'soft-delete-plan-21',21,'软删计划额外词','软删自动计划客户')
                """
            )
        soft_delete_now = datetime.now(timezone.utc)
        soft_delete_plan = create_plan(
            resolve_identity(1, request_id="owner-soft-delete-plan"),
            request_id="automatic-plan-brand-soft-delete-0001",
            feature_code="monitor_single",
            work_kind="monitoring.run",
            payload={"brand_id": 21},
            cadence_seconds=60,
            max_occurrences=1,
            total_budget_points=30,
            max_occurrence_points=30,
            starts_at=soft_delete_now - timedelta(seconds=1),
            ends_at=soft_delete_now + timedelta(minutes=10),
            brand_id=21,
        )
        force_plan_due(dsn, int(soft_delete_plan["id"]))
        frozen_before_soft_delete_schedule = int(sql_value(dsn, "SELECT frozen_points FROM user_wallets WHERE user_id=1"))
        soft_delete_occurrence = asyncio.run(schedule_one_due_occurrence())
        import services.organization_billing as soft_delete_billing_module
        original_soft_delete_release = soft_delete_billing_module.release_charge

        async def _simulate_release_handoff_crash(**_kwargs):
            raise RuntimeError("simulated release handoff crash")

        soft_delete_billing_module.release_charge = _simulate_release_handoff_crash
        try:
            soft_delete_response = client.delete(
                "/api/my-clients/21?reason=自动计划软删竞态验证",
                headers={"x-throwaway-user-id": "1", "x-request-id": "http-brand-soft-delete-plan-0001"},
            )
        finally:
            soft_delete_billing_module.release_charge = original_soft_delete_release
        from services.organization_worker import _prepare_automatic_monitoring
        try:
            _prepare_automatic_monitoring({"brand_id": 21}, 21)
            deleted_extra_keywords_blocked = False
        except Exception:
            deleted_extra_keywords_blocked = True
        soft_delete_provider_calls = {"count": 0}

        async def _deleted_brand_provider_must_not_run(
            _request, *, organization_identity=None, **_kwargs,
        ):
            soft_delete_provider_calls["count"] += 1
            return {"status": "success", "task_id": 212121}

        original_deleted_brand_provider = live_monitoring_api.api_run_monitoring
        live_monitoring_api.api_run_monitoring = _deleted_brand_provider_must_not_run
        try:
            deleted_dispatch = asyncio.run(dispatch_one_automatic_work(worker_id="deleted-brand-worker"))
            deleted_recovery = asyncio.run(recover_stale_work())
        finally:
            live_monitoring_api.api_run_monitoring = original_deleted_brand_provider
        soft_delete_state = sql_value(
            dsn,
            """
            SELECT json_build_object(
              'brand_deleted',b.is_deleted,'plan_status',p.status,
              'scheduled',p.scheduled_occurrences,'reserved_budget',p.reserved_budget_points,
              'occurrence_status',x.status,'charge_status',c.status,'outbox_status',o.status
            )
            FROM brands b
            JOIN organization_automatic_plans p ON p.brand_id=b.id
            JOIN organization_plan_occurrences x ON x.plan_id=p.id
            JOIN organization_charge_links c ON c.id=x.charge_link_id
            JOIN organization_work_outbox o ON o.charge_link_id=c.id
            WHERE p.id=%s
            """,
            (soft_delete_plan["id"],),
        )
        verify.check(
            soft_delete_response.status_code == 200
            and int(soft_delete_occurrence["charge"]["id"])
            in set(soft_delete_response.json().get("organization_release_pending") or [])
            and deleted_extra_keywords_blocked
            and deleted_dispatch is None
            and soft_delete_provider_calls["count"] == 0
            and int(soft_delete_occurrence["charge"]["id"]) in deleted_recovery["released"]
            and soft_delete_state == {
                "brand_deleted": True,
                "plan_status": "cancelled",
                "scheduled": 0,
                "reserved_budget": 0,
                "occurrence_status": "released",
                "charge_status": "released",
                "outbox_status": "cancelled",
            }
            and int(sql_value(dsn, "SELECT frozen_points FROM user_wallets WHERE user_id=1")) == frozen_before_soft_delete_schedule,
            "brand_soft_delete_cancels_plan_blocks_extra_keywords_provider_and_recovers_budget_wallet",
            {"response": soft_delete_response.json(), "state": soft_delete_state, "recovery": deleted_recovery},
        )
        restore_soft_delete_response = client.post(
            "/api/my-clients/21/restore",
            headers={"x-throwaway-user-id": "1", "x-request-id": "http-brand-restore-plan-0001"},
        )
        verify.check(
            restore_soft_delete_response.status_code == 200
            and sql_value(dsn, "SELECT status FROM organization_automatic_plans WHERE id=%s", (soft_delete_plan["id"],)) == "cancelled"
            and asyncio.run(schedule_one_due_occurrence()) is None,
            "brand_restore_does_not_restart_cancelled_automatic_plan",
            restore_soft_delete_response.json(),
        )
        with connect(dsn) as conn:
            conn.cursor().execute(
                "INSERT INTO brands(id,owner_user_id,name,industry) VALUES (24,1,'无员工分配撤权客户','企业服务')"
            )
        owner_predelete_identity = resolve_identity(1, request_id="owner-claim-before-delete-24")
        owner_predelete_charge = asyncio.run(
            reserve_charge(
                owner_predelete_identity,
                execution_id="owner-claim-delete-restore-generation-24",
                feature_code="monitor_single",
                work_kind="monitoring.run",
                payload={"brand_id": 24, "race": "delete-restore"},
                brand_id=24,
            )
        )
        owner_predelete_claim = claim_live_charge(
            owner_predelete_identity,
            charge_link_id=int(owner_predelete_charge["id"]),
        )
        owner_delete_response = client.delete(
            "/api/my-clients/24?reason=老板旧claim撤权验证",
            headers={"x-throwaway-user-id": "1", "x-request-id": "http-owner-delete-claim-24"},
        )
        owner_restore_response = client.post(
            "/api/my-clients/24/restore",
            headers={"x-throwaway-user-id": "1", "x-request-id": "http-owner-restore-claim-24"},
        )
        try:
            mark_external_side_effect_started(
                charge_link_id=int(owner_predelete_charge["id"]),
                claim_token=str(owner_predelete_claim["claim_token"]),
            )
            owner_old_claim_rejected = False
        except OrganizationError as exc:
            owner_old_claim_rejected = exc.code == "ORG_CHARGE_AUTHORITY_CHANGED"
        asyncio.run(
            release_charge(
                charge_link_id=int(owner_predelete_charge["id"]),
                reason="delete restore invalidated pre-delete owner claim",
            )
        )
        verify.check(
            owner_delete_response.status_code == 200
            and owner_restore_response.status_code == 200
            and owner_old_claim_rejected
            and sql_value(
                dsn,
                "SELECT status FROM organization_charge_links WHERE id=%s",
                (owner_predelete_charge["id"],),
            ) == "released",
            "brand_delete_restore_invalidates_predelete_owner_claim_without_assignments",
        )
        scoped_keywords_response = client.get(
            "/api/monitoring/keywords",
            headers={**member_a_http, "x-request-id": "http-live-keywords-scope-0001"},
        )
        scoped_keywords = scoped_keywords_response.json().get("keywords", [])
        unassigned_keywords_response = client.get(
            "/api/monitoring/keywords?brand_id=20",
            headers={**member_a_http, "x-request-id": "http-live-keywords-unassigned-0001"},
        )
        verify.check(
            scoped_keywords_response.status_code == 200
            and {int(row["brand_id"]) for row in scoped_keywords} == {10}
            and unassigned_keywords_response.status_code in {403, 404},
            "http_monitoring_keywords_without_brand_aggregates_only_assignments",
            {
                "scoped": scoped_keywords_response.json(),
                "unassigned": unassigned_keywords_response.json(),
            },
        )

        async def _ambiguous_monitoring_failure(
            _request,
            *,
            organization_identity=None,
            before_first_provider=None,
            **_kwargs,
        ):
            if before_first_provider is not None:
                boundary_result = before_first_provider()
                if asyncio.iscoroutine(boundary_result):
                    await boundary_result
            raise TimeoutError("provider response lost after external request")

        before_unknown_frozen = int(
            sql_value(dsn, "SELECT frozen_points FROM user_wallets WHERE user_id=1")
        )
        live_server.api_run_monitoring = _ambiguous_monitoring_failure
        try:
            ambiguous_monitoring_response = client.post(
                "/api/monitoring/run",
                headers={**member_a_http, "x-request-id": "http-live-monitor-unknown-0001"},
                json={"brand_id": 10, "platforms": ["dashscope"]},
            )
        finally:
            live_server.api_run_monitoring = _fake_run_monitoring
        ambiguous_monitor_charge = sql_value(
            dsn,
            """
            SELECT json_build_object(
              'id',c.id,'charge_status',c.status,'outbox_status',o.status,
              'reserved_limit_legs',(
                SELECT COUNT(*) FROM organization_charge_limit_links j
                WHERE j.charge_link_id=c.id AND j.reserved_points=c.reserved_ceiling_points
              )
            )
            FROM organization_charge_links c
            JOIN organization_work_outbox o ON o.charge_link_id=c.id
            WHERE c.organization_id=%s AND o.work_kind='monitoring.run'
              AND c.status='unknown'
            ORDER BY c.id DESC LIMIT 1
            """,
            (organization_id,),
        )
        ambiguous_retry_response = client.post(
            "/api/monitoring/run",
            headers={**member_a_http, "x-request-id": "http-live-monitor-unknown-retry-0002"},
            json={"brand_id": 10, "platforms": ["dashscope"]},
        )
        verify.check(
            ambiguous_monitoring_response.status_code == 500
            and ambiguous_retry_response.status_code == 409
            and ambiguous_retry_response.json().get("detail", {}).get("code") == "ORG_MONITOR_ALREADY_RUNNING"
            and monitoring_provider_calls["count"] == 1
            and ambiguous_monitor_charge["charge_status"] == "unknown"
            and ambiguous_monitor_charge["outbox_status"] == "unknown"
            and int(ambiguous_monitor_charge["reserved_limit_legs"]) == 4
            and int(sql_value(dsn, "SELECT frozen_points FROM user_wallets WHERE user_id=1"))
            == before_unknown_frozen + 30,
            "http_external_timeout_quarantines_wallet_and_member_limit_legs",
            {
                "response": ambiguous_monitoring_response.json(),
                "charge": ambiguous_monitor_charge,
                "retry": ambiguous_retry_response.json(),
            },
        )

        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE organization_memberships SET status='suspended',version=version+1,capability_version=capability_version+1,updated_at=NOW() WHERE id=%s",
                (member_a_id,),
            )
            cursor.execute(
                "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
                (organization_id,),
            )
        suspended_write = client.post(
            "/api/monitoring/run",
            headers={**member_a_http, "x-request-id": "http-live-suspended-write-0001"},
            json={"brand_id": 10},
        )
        verify.check(
            suspended_write.status_code == 403
            and suspended_write.json()["detail"]["code"] == "ORG_MEMBERSHIP_INACTIVE",
            "http_suspended_seat_immediately_rejects_paid_write",
            suspended_write.json(),
        )

    organization_ws_jwt.decode_jwt = original_live_decode_jwt
    live_server._ws_authorize_session = original_ws_session_authorizer
    live_server._ws_authorize_organization_session = original_ws_organization_authorizer
    live_server._ws_check_user_active = original_ws_user_checker
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_memberships SET status='active',version=version+1,capability_version=capability_version+1,updated_at=NOW() WHERE id=%s",
            (member_a_id,),
        )
        cursor.execute(
            "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
            (organization_id,),
        )
    member_a = resolve_identity(first_user, request_id="member-a-after-live-http")

    # Delegated reviewers must not enumerate approval metadata for customers
    # outside their current assignment. Their own requests remain visible.
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO brands(id,owner_user_id,name) VALUES (11,1,'审核隔离客户B')")
    owner = resolve_identity(1, request_id="owner-before-review-list-scope")
    assign_brands(
        owner,
        membership_id=member_a_id,
        brand_ids=[10, 11],
        request_id="assign-member-a-review-scope",
        reason="验证审批列表客户隔离",
    )
    member_a = resolve_identity(first_user, request_id="member-a-review-list-scope")
    member_b = resolve_identity(member_b_user, request_id="member-b-review-list-scope")
    hidden_approval = submit_approval(
        member_a,
        action_type="billing.execute_high_cost",
        payload={"brand_id": 11, "operation": "hidden-review-scope", "ceiling": 1050},
        request_id="approval-hidden-brand-11",
        brand_id=11,
        estimated_points=1050,
        feature_code="geo_diagnosis",
    )
    reviewer_visible_ids = {int(item["id"]) for item in list_approvals(member_b)}
    verify.check(
        int(hidden_approval["id"]) not in reviewer_visible_ids,
        "approval_list_reviewer_cannot_enumerate_unassigned_brand",
        reviewer_visible_ids,
    )
    decide_approval(
        owner,
        approval_id=int(hidden_approval["id"]),
        decision="reject",
        expected_version=int(hidden_approval["version"]),
        reason="客户隔离验证完成",
    )

    approval_payload = {"brand_id": 10, "operation": "high-cost-diagnosis", "ceiling": 1050}
    approval = submit_approval(
        member_a,
        action_type="billing.execute_high_cost",
        payload=approval_payload,
        request_id="approval-high-cost-0001",
        brand_id=10,
        estimated_points=1050,
        feature_code="geo_diagnosis",
    )
    try:
        decide_approval(member_a, approval_id=approval["id"], decision="approve", expected_version=approval["version"], reason="不得自批")
        self_rejected = False
    except OrganizationError as exc:
        self_rejected = exc.code == "ORG_APPROVAL_SELF_FORBIDDEN"
    verify.check(self_rejected, "approval_self_approval_forbidden")
    approved = decide_approval(member_b, approval_id=approval["id"], decision="approve", expected_version=approval["version"], reason="独立审核人批准")
    verify.check(approved["status"] == "approved", "approval_independent_reviewer_with_brand_scope")

    async def reserve_high_cost(payload):
        return await reserve_charge(
            member_a,
            execution_id="high-cost-execution-0001",
            feature_code="geo_diagnosis",
            work_kind="diagnosis.run",
            payload=payload,
            brand_id=10,
            approval_request_id=approval["id"],
            dynamic_ceiling_extra_points=1000,
        )

    try:
        asyncio.run(reserve_high_cost({**approval_payload, "ceiling": 1051}))
        tamper_rejected = False
    except OrganizationError as exc:
        tamper_rejected = exc.code == "ORG_APPROVAL_PAYLOAD_CHANGED"
    verify.check(tamper_rejected, "approval_payload_tamper_rejected_before_reservation")
    try:
        asyncio.run(
            reserve_charge(
                member_a,
                execution_id="high-cost-estimate-tamper-0001",
                feature_code="geo_diagnosis",
                work_kind="diagnosis.run",
                payload=approval_payload,
                brand_id=10,
                approval_request_id=approval["id"],
                dynamic_ceiling_extra_points=999,
            )
        )
        estimate_tamper_rejected = False
    except OrganizationError as exc:
        estimate_tamper_rejected = exc.code == "ORG_APPROVAL_ESTIMATE_CHANGED"
    verify.check(
        estimate_tamper_rejected,
        "approval_estimated_points_tamper_rejected_before_reservation",
    )
    high_charge = asyncio.run(reserve_high_cost(approval_payload))
    asyncio.run(release_charge(charge_link_id=high_charge["id"], reason="高额审批验证完成"))

    # Approval authority is bound to one immutable membership generation. A
    # removed user who rejoins with the same account/role/brand must reapply.
    generation_user = 31
    generation_invite = create_invite(
        owner,
        target_kind="phone",
        target=f"139{generation_user:08d}",
        role_id=role_id,
        request_id="invite-approval-generation-31-a",
        source_ip="192.0.2.41",
    )
    generation_membership_1 = int(
        accept_invite(
            authenticated_user_id=generation_user,
            token=generation_invite["delivery_token"],
            request_id="accept-approval-generation-31-a",
            source_ip="192.0.2.42",
        )["membership_id"]
    )
    owner = resolve_identity(1, request_id="owner-approval-generation-a")
    assign_brands(
        owner,
        membership_id=generation_membership_1,
        brand_ids=[10],
        request_id="assign-approval-generation-31-a",
        reason="审批代际验证",
    )
    generation_identity_1 = resolve_identity(generation_user, request_id="member-approval-generation-a")
    generation_payload = {"brand_id": 10, "operation": "membership-generation", "ceiling": 1050}
    generation_approval = submit_approval(
        generation_identity_1,
        action_type="billing.execute_high_cost",
        payload=generation_payload,
        request_id="approval-membership-generation-31",
        brand_id=10,
        estimated_points=1050,
        feature_code="geo_diagnosis",
    )
    member_b = resolve_identity(member_b_user, request_id="reviewer-approval-generation")
    decide_approval(
        member_b,
        approval_id=int(generation_approval["id"]),
        decision="approve",
        expected_version=int(generation_approval["version"]),
        reason="批准旧席位代际请求",
    )
    owner = resolve_identity(1, request_id="owner-remove-generation-a")
    set_member_status(
        owner,
        membership_id=generation_membership_1,
        action="remove",
        reason="验证移除后审批失效",
    )
    generation_reinvite = create_invite(
        owner,
        target_kind="phone",
        target=f"139{generation_user:08d}",
        role_id=role_id,
        request_id="invite-approval-generation-31-b",
        source_ip="192.0.2.43",
    )
    generation_membership_2 = int(
        accept_invite(
            authenticated_user_id=generation_user,
            token=generation_reinvite["delivery_token"],
            request_id="accept-approval-generation-31-b",
            source_ip="192.0.2.44",
        )["membership_id"]
    )
    owner = resolve_identity(1, request_id="owner-approval-generation-b")
    assign_brands(
        owner,
        membership_id=generation_membership_2,
        brand_ids=[10],
        request_id="assign-approval-generation-31-b",
        reason="验证重入席位不得复用旧审批",
    )
    generation_identity_2 = resolve_identity(generation_user, request_id="member-approval-generation-b")
    try:
        asyncio.run(
            reserve_charge(
                generation_identity_2,
                execution_id="approval-membership-generation-execution-31",
                feature_code="geo_diagnosis",
                work_kind="diagnosis.run",
                payload=generation_payload,
                brand_id=10,
                approval_request_id=int(generation_approval["id"]),
                dynamic_ceiling_extra_points=1000,
            )
        )
        approval_generation_rejected = False
    except OrganizationError as exc:
        approval_generation_rejected = exc.code == "ORG_APPROVAL_MEMBERSHIP_CHANGED"
    verify.check(
        approval_generation_rejected,
        "approval_cannot_cross_membership_remove_and_rejoin_generation",
    )
    set_member_status(
        owner,
        membership_id=generation_membership_2,
        action="remove",
        reason="审批代际验证清理",
    )

    # Use a test-only larger pool so 50 simultaneous application calls hold 50
    # distinct real PostgreSQL sessions; production pool configuration is not changed.
    db_connection.close_pool()
    db_connection._pool = psycopg2.pool.ThreadedConnectionPool(5, 60, dsn=dsn)

    def reserve_sync(execution_id: str, payload: dict):
        return asyncio.run(
            reserve_charge(
                member_a,
                execution_id=execution_id,
                feature_code="geo_diagnosis",
                work_kind="diagnosis.run",
                payload=payload,
                brand_id=10,
            )
        )

    same_payload = {"brand_id": 10, "case": "same-key-20"}
    with ThreadPoolExecutor(max_workers=20) as executor:
        same_results = list(executor.map(lambda _: reserve_sync("same-key-concurrency-20", same_payload), range(20)))
    same_ids = {item["id"] for item in same_results}
    verify.check(len(same_ids) == 1 and sum(bool(item["replayed"]) for item in same_results) == 19, "billing_idempotency_concurrency_20")
    asyncio.run(release_charge(charge_link_id=next(iter(same_ids)), reason="同键并发验证完成"))

    live_claim_charge = reserve_sync(
        "live-claim-cannot-be-stolen-0001",
        {"brand_id": 10, "case": "live-claim-lease"},
    )
    first_live_claim = claim_live_charge(
        member_a,
        charge_link_id=int(live_claim_charge["id"]),
        lease_seconds=300,
    )
    second_live_claim = claim_live_charge(
        member_a,
        charge_link_id=int(live_claim_charge["id"]),
        lease_seconds=300,
    )
    persisted_claim_token = sql_value(
        dsn,
        "SELECT claim_token FROM organization_work_outbox WHERE charge_link_id=%s",
        (live_claim_charge["id"],),
    )
    verify.check(
        bool(first_live_claim.get("claim_token"))
        and second_live_claim.get("in_progress") is True
        and "claim_token" not in second_live_claim
        and persisted_claim_token == first_live_claim["claim_token"],
        "billing_unexpired_live_claim_cannot_be_stolen",
        {"first": first_live_claim, "second": second_live_claim},
    )
    asyncio.run(release_charge(charge_link_id=live_claim_charge["id"], reason="活跃 claim 防抢占验证完成"))

    with ThreadPoolExecutor(max_workers=50) as executor:
        distinct_results = list(
            executor.map(
                lambda n: reserve_sync(f"distinct-concurrency-50-{n:02d}", {"brand_id": 10, "case": n}),
                range(50),
            )
        )
    verify.check(len({item["id"] for item in distinct_results}) == 50, "billing_wallet_and_four_limit_buckets_concurrency_50")
    with ThreadPoolExecutor(max_workers=50) as executor:
        list(executor.map(lambda item: asyncio.run(release_charge(charge_link_id=item["id"], reason="五十路释放")), distinct_results))
    verify.check(billing_consistency()["ready"], "billing_dual_leg_consistency_after_50_release", billing_consistency())

    # Old single-user and organization reservations share the same physical
    # wallet concurrently without changing the legacy algorithm or pool order.
    with ThreadPoolExecutor(max_workers=2) as executor:
        legacy_future = executor.submit(lambda: asyncio.run(freeze_points(1, "geo_diagnosis", task_ref="legacy-parity-concurrent")))
        org_future = executor.submit(lambda: reserve_sync("org-legacy-concurrent", {"brand_id": 10, "case": "mixed"}))
        legacy_freeze = legacy_future.result()
        mixed_charge = org_future.result()
    verify.check(int(legacy_freeze["amount"]) == 50 and int(mixed_charge["reserved_ceiling_points"]) == 50, "legacy_and_organization_shared_wallet_concurrency")
    asyncio.run(release_freeze(freeze_id=legacy_freeze["freeze_id"], user_id=1, freeze_table="legacy", reason="旧路径兼容验证"))
    asyncio.run(release_charge(charge_link_id=mixed_charge["id"], reason="混合并发验证完成"))

    # Dynamic settle releases the unused ceiling, refund follows immutable
    # physical split and original period junction, and duplicate refund is safe.
    dynamic = asyncio.run(
        reserve_charge(
            member_a,
            execution_id="dynamic-settle-refund-0001",
            feature_code="geo_diagnosis",
            work_kind="diagnosis.run",
            payload={"brand_id": 10, "dynamic": True},
            brand_id=10,
            dynamic_ceiling_extra_points=100,
        )
    )
    dynamic_claim = claim_live_charge(member_a, charge_link_id=dynamic["id"])
    mark_external_side_effect_started(
        charge_link_id=dynamic["id"],
        claim_token=str(dynamic_claim["claim_token"]),
    )
    settled = asyncio.run(
        settle_charge(
            charge_link_id=dynamic["id"],
            actual_points=90,
            result_payload={"ok": True},
            claim_token=str(dynamic_claim["claim_token"]),
        )
    )
    verify.check(settled["status"] == "committed" and settled["actual_points"] == 90, "dynamic_ceiling_settle_releases_difference")
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE organization_spend_limits SET status='closed'
            WHERE id IN (SELECT spend_limit_id FROM organization_charge_limit_links WHERE charge_link_id=%s)
            """,
            (dynamic["id"],),
        )
    with ThreadPoolExecutor(max_workers=20) as executor:
        duplicate_refunds = list(
            executor.map(
                lambda _: asyncio.run(
                    refund_charge(
                        charge_link_id=dynamic["id"],
                        cumulative_refund_target=40,
                        refund_request_id="duplicate-refund-request-0001",
                        reason="跨周期重复退款验证",
                        expected_organization_id=organization_id,
                    )
                ),
                range(20),
            )
        )
    verify.check(all(item["refunded_points"] == 40 for item in duplicate_refunds), "cross_period_duplicate_refund_concurrency_20")
    verify.check(billing_consistency()["ready"], "refund_restores_original_physical_and_limit_legs", billing_consistency())
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE organization_spend_limits SET status='open'
            WHERE id IN (SELECT spend_limit_id FROM organization_charge_limit_links WHERE charge_link_id=%s)
            """,
            (dynamic["id"],),
        )

    # Crash window 1: kill-like connection loss before transaction commit rolls
    # back both wallet and evidence. psycopg backend termination is not needed;
    # closing an uncommitted session exercises the same PostgreSQL guarantee.
    before_wallet = int(sql_value(dsn, "SELECT paid_points+bonus_points+commission_points+frozen_points FROM user_wallets WHERE user_id=1"))
    crash_conn = connect(dsn)
    crash_cursor = crash_conn.cursor()
    crash_cursor.execute("UPDATE user_wallets SET paid_points=paid_points-7,frozen_points=frozen_points+7 WHERE user_id=1")
    crash_conn.close()
    after_wallet = int(sql_value(dsn, "SELECT paid_points+bonus_points+commission_points+frozen_points FROM user_wallets WHERE user_id=1"))
    verify.check(before_wallet == after_wallet, "kill_window_before_commit_rolls_back")

    stale = reserve_sync("recovery-before-start-0001", {"brand_id": 10, "recovery": "before"})
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE organization_work_outbox SET dispatch_deadline=NOW()-INTERVAL '1 second' WHERE charge_link_id=%s", (stale["id"],))
    recovered = asyncio.run(recover_stale_work())
    verify.check(stale["id"] in recovered["released"], "kill_window_reserved_before_execution_releases")

    ambiguous = reserve_sync("recovery-after-external-0001", {"brand_id": 10, "recovery": "after"})
    claimed = claim_work(worker_id="verification-worker", lease_seconds=30)
    # Earlier pending rows may exist only for this charge at this point.
    verify.check(claimed is not None and int(claimed["charge_link_id"]) == int(ambiguous["id"]), "worker_claims_reserved_actor_without_system_rewrite", claimed)
    start_work(outbox_id=int(claimed["id"]), claim_token=claimed["claim_token"])
    mark_external_side_effect_started(charge_link_id=ambiguous["id"], claim_token=claimed["claim_token"])
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE organization_work_outbox SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (claimed["id"],))
    recovered_after = asyncio.run(recover_stale_work())
    verify.check(ambiguous["id"] in recovered_after["unknown"], "kill_window_after_external_side_effect_quarantines_unknown")
    protected_freeze_id = int(
        sql_value(
            dsn,
            "SELECT physical_freeze_id FROM organization_charge_links WHERE id=%s",
            (ambiguous["id"],),
        )
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE point_freezes SET created_at=NOW()-INTERVAL '13 hours' WHERE id=%s",
            (protected_freeze_id,),
        )
    sweep_preview = asyncio.run(sweep_zombie_freezes(stale_hours=12, dry_run=True))
    verify.check(
        protected_freeze_id not in set(sweep_preview.get("would_release_ids") or [])
        and sql_value(dsn, "SELECT status FROM point_freezes WHERE id=%s", (protected_freeze_id,)) == "frozen",
        "generic_freeze_sweeper_preserves_unknown_organization_hold",
        sweep_preview,
    )

    # Artifact actor/org stamping, own/team/share isolation, cache/cursor
    # authority versioning, and public-token immediate revocation.
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO quotes(brand_id) VALUES (10) RETURNING id")
        quote_a = int(cursor.fetchone()["id"])
        stamp_artifact(cursor, member_a, artifact_type="quote", artifact_id=quote_a, brand_id=10)
        cursor.execute("INSERT INTO quotes(brand_id) VALUES (10) RETURNING id")
        quote_b = int(cursor.fetchone()["id"])
        stamp_artifact(cursor, member_b, artifact_type="quote", artifact_id=quote_b, brand_id=10)
    own_a = list_artifacts(member_a, artifact_type="quote")
    verify.check(
        {int(item["id"]) for item in own_a} == {live_quote_id, quote_a},
        "artifact_default_member_reads_only_own",
    )

    # External approval and issuance bind a server-resolved owner white-label
    # snapshot. Client attempts to provide a logo/name/contact are rejected.
    member_a_version = int(sql_value(dsn, "SELECT version FROM organization_memberships WHERE id=%s", (member_a_id,)))
    set_member_overrides(
        owner,
        membership_id=member_a_id,
        expected_membership_version=member_a_version,
        overrides={
            "approvals.review": "allow",
            "writing.share_internal": "allow",
            "quote.submit_for_approval": "allow",
        },
        reason="验证公开报价审批与服务端白标快照",
        high_risk_confirmed=True,
        high_risk_reason="测试公开报价跨域授权",
    )
    member_b_version = int(sql_value(dsn, "SELECT version FROM organization_memberships WHERE id=%s", (member_b_id,)))
    set_member_overrides(
        owner,
        membership_id=member_b_id,
        expected_membership_version=member_b_version,
        overrides={"approvals.review": "allow", "quote.submit_for_approval": "allow"},
        reason="验证公开报价独立审核",
        high_risk_confirmed=True,
        high_risk_reason="测试独立审核跨域授权",
    )
    member_a = resolve_identity(first_user, request_id="member-a-public-approval")
    member_b = resolve_identity(member_b_user, request_id="member-b-public-review")
    try:
        submit_approval(
            member_a,
            action_type="quote.send_external",
            payload={
                "expires_in_seconds": 300,
                "whitelabel_snapshot": {
                    "brand": {"company_name": "员工伪造品牌", "logo_url": "https://evil.invalid/logo"},
                    "contact": {"phone": "10086"},
                },
            },
            request_id="public-approval-forgery-0001",
            brand_id=10,
            artifact_type="quote",
            artifact_id=str(quote_a),
            public_scope="quote",
        )
        forged_whitelabel_rejected = False
    except OrganizationError as exc:
        forged_whitelabel_rejected = exc.code == "ORG_WHITELABEL_CLIENT_CONTROL_FORBIDDEN"
    verify.check(forged_whitelabel_rejected, "public_approval_rejects_employee_whitelabel_forgery")

    public_approval = submit_approval(
        member_a,
        action_type="quote.send_external",
        payload={"expires_in_seconds": 300},
        request_id="public-approval-quote-a-0001",
        brand_id=10,
        artifact_type="quote",
        artifact_id=str(quote_a),
        public_scope="quote",
    )
    public_approval = decide_approval(
        member_b,
        approval_id=int(public_approval["id"]),
        decision="approve",
        expected_version=int(public_approval["version"]),
        reason="独立审核公开报价",
    )
    os.environ["ORGANIZATION_EXTERNAL_ACTIONS_ENABLED"] = "true"

    def issue_same_public_token(_: int):
        return issue_public_token(
            member_a,
            artifact_type="quote",
            artifact_id=str(quote_a),
            purpose="quote",
            request_id="public-token-member-quote-a-0001",
            approval_request_id=int(public_approval["id"]),
            expires_in_seconds=300,
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        issued_tokens = list(executor.map(issue_same_public_token, range(20)))
    stable_tokens = {item["token"] for item in issued_tokens}
    verify.check(
        len(stable_tokens) == 1
        and int(sql_value(dsn, "SELECT COUNT(*) FROM organization_artifact_shares WHERE request_id=%s", ("public-token-member-quote-a-0001",))) == 1
        and sql_value(dsn, "SELECT status FROM organization_approval_requests WHERE id=%s", (public_approval["id"],)) == "executed",
        "public_token_concurrency_response_loss_replays_stable_token_and_finishes_approval",
        issued_tokens,
    )
    try:
        issue_public_token(
            owner,
            artifact_type="quote",
            artifact_id=str(quote_a),
            purpose="quote",
            request_id="public-token-member-quote-a-0001",
            approval_request_id=int(public_approval["id"]),
            expires_in_seconds=300,
        )
        public_token_cross_actor_replay_rejected = False
    except OrganizationError as exc:
        public_token_cross_actor_replay_rejected = exc.code == "ORG_IDEMPOTENCY_CONFLICT"
    verify.check(
        public_token_cross_actor_replay_rejected,
        "public_token_idempotency_replay_is_bound_to_issuing_actor",
    )
    member_public_view = validate_public_token(purpose="quote", token=next(iter(stable_tokens)))
    verify.check(
        member_public_view["whitelabel_snapshot"]["brand"]["company_name"] == "老板白标品牌"
        and "员工伪造品牌" not in json.dumps(member_public_view, ensure_ascii=False, default=str),
        "public_token_never_exposes_employee_forged_whitelabel",
        member_public_view,
    )
    try:
        issue_public_token(
            member_a,
            artifact_type="quote",
            artifact_id=str(quote_a),
            purpose="quote",
            request_id="public-token-member-quote-a-0001",
            approval_request_id=int(public_approval["id"]),
            expires_in_seconds=600,
        )
        public_token_payload_conflict = False
    except OrganizationError as exc:
        public_token_payload_conflict = exc.code == "ORG_IDEMPOTENCY_CONFLICT"
    verify.check(public_token_payload_conflict, "public_token_same_key_different_payload_rejected")
    os.environ["ORGANIZATION_EXTERNAL_ACTIONS_ENABLED"] = "false"

    share_artifact_internal(
        member_a,
        artifact_type="quote",
        artifact_id=str(quote_a),
        permission="read",
        subject_membership_id=member_b_id,
        subject_role_id=None,
        request_id="internal-share-quote-a",
    )
    try:
        share_artifact_internal(
            member_a,
            artifact_type="quote",
            artifact_id=str(quote_a),
            permission="edit",
            subject_membership_id=member_b_id,
            subject_role_id=None,
            request_id="internal-share-quote-a",
        )
        internal_share_conflict_rejected = False
    except OrganizationError as exc:
        internal_share_conflict_rejected = exc.code == "ORG_IDEMPOTENCY_CONFLICT"
    verify.check(
        internal_share_conflict_rejected,
        "internal_share_same_key_different_permission_rejected",
    )
    member_b = resolve_identity(member_b_user, request_id="member-b-after-share")
    shared_b = list_artifacts(member_b, artifact_type="quote")
    verify.check({int(item["id"]) for item in shared_b} == {quote_a, quote_b}, "artifact_explicit_member_share_is_effective")
    cursor_token = isolation_cursor(member_b, last_id=quote_b, secret="throwaway-cursor-secret")
    old_cache_key = cache_partition_key(member_b, "rag", "brand-10")
    current_version = int(sql_value(dsn, "SELECT version FROM organization_memberships WHERE id=%s", (member_b_id,)))
    set_member_overrides(owner, membership_id=member_b_id, expected_membership_version=current_version, overrides={"approvals.review": "allow", "team.output_read": "allow"}, reason="权限版本轮换验证", high_risk_confirmed=True, high_risk_reason="测试权限版本跨域轮换")
    member_b_new = resolve_identity(member_b_user, request_id="member-b-version-rotated")
    try:
        parse_isolation_cursor(member_b_new, token=cursor_token, secret="throwaway-cursor-secret")
        cursor_rejected = False
    except OrganizationError as exc:
        cursor_rejected = exc.code == "ORG_CURSOR_AUTHORITY_CHANGED"
    verify.check(cursor_rejected and cache_partition_key(member_b_new, "rag", "brand-10") != old_cache_key, "cursor_cache_rag_partition_revoked_on_authority_change")

    os.environ["ORGANIZATION_EXTERNAL_ACTIONS_ENABLED"] = "true"
    owner = resolve_identity(1, request_id="owner-public-token")
    public = issue_public_token(
        owner,
        artifact_type="quote",
        artifact_id=str(quote_a),
        purpose="quote",
        request_id="public-token-quote-a",
        approval_request_id=None,
        expires_in_seconds=300,
    )
    public_view = validate_public_token(purpose="quote", token=public["token"])
    verify.check(
        public_view["principal_user_id"] == 1
        and public_view["whitelabel_snapshot"]["brand"]["company_name"] == "老板白标品牌"
        and "employee" not in json.dumps(public_view, ensure_ascii=False, default=str).lower(),
        "public_token_uses_owner_whitelabel_principal",
    )
    public_http_app = FastAPI()
    public_http_app.include_router(organization_public_router)
    with TestClient(public_http_app, raise_server_exceptions=False) as public_client:
        public_http_response = public_client.post(
            "/api/public/organization/links/validate",
            json={"purpose": "quote", "token": public["token"]},
        )
        legacy_uri_response = public_client.get(
            f"/api/public/organization/links/quote/{public['token']}"
        )
    verify.check(
        public_http_response.status_code == 200
        and public_http_response.headers.get("cache-control") == "private, no-store, max-age=0"
        and public_http_response.headers.get("pragma") == "no-cache"
        and legacy_uri_response.status_code == 404,
        "public_capability_token_uses_post_body_and_real_no_store_headers",
        {
            "headers": dict(public_http_response.headers),
            "legacy_status": legacy_uri_response.status_code,
        },
    )
    revoke_share(owner, share_id=int(public["share"]["id"]), reason="公开链接撤销验证")
    try:
        validate_public_token(purpose="quote", token=public["token"])
        revoked_rejected = False
    except OrganizationError as exc:
        revoked_rejected = exc.code == "ORG_PUBLIC_TOKEN_REVOKED"
    verify.check(revoked_rejected, "public_token_revocation_is_immediate")

    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO brands(id,owner_user_id,name) VALUES (12,1,'待删除客户C')")
        cursor.execute("INSERT INTO quotes(brand_id) VALUES (12) RETURNING id")
        deleted_brand_quote = int(cursor.fetchone()["id"])
        stamp_artifact(
            cursor,
            owner,
            artifact_type="quote",
            artifact_id=deleted_brand_quote,
            brand_id=12,
        )
    deleted_brand_token = issue_public_token(
        owner,
        artifact_type="quote",
        artifact_id=str(deleted_brand_quote),
        purpose="quote",
        request_id="public-token-deleted-brand-quote",
        approval_request_id=None,
        expires_in_seconds=300,
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE brands SET is_deleted=TRUE WHERE id=12")
    try:
        validate_public_token(purpose="quote", token=deleted_brand_token["token"])
        deleted_brand_token_rejected = False
    except OrganizationError as exc:
        deleted_brand_token_rejected = exc.code == "ORG_PUBLIC_TOKEN_REVOKED"
    try:
        issue_public_token(
            owner,
            artifact_type="quote",
            artifact_id=str(deleted_brand_quote),
            purpose="quote",
            request_id="public-token-deleted-brand-quote-new",
            approval_request_id=None,
            expires_in_seconds=300,
        )
        deleted_brand_new_issue_rejected = False
    except OrganizationError as exc:
        deleted_brand_new_issue_rejected = exc.code == "ORG_ARTIFACT_NOT_FOUND"
    verify.check(
        deleted_brand_token_rejected
        and deleted_brand_new_issue_rejected
        and deleted_brand_quote not in {
            int(item["id"]) for item in list_artifacts(owner, artifact_type="quote")
        },
        "deleted_brand_revokes_artifact_queries_and_public_tokens",
    )
    os.environ["ORGANIZATION_EXTERNAL_ACTIONS_ENABLED"] = "false"

    # Owner-approved automatic occurrence keeps actor_kind=system, has a
    # stable occurrence idempotency key, and never creates member limit legs.
    now = datetime.now(timezone.utc)
    plan = create_plan(
        owner,
        request_id="automatic-plan-verification-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10},
        cadence_seconds=60,
        max_occurrences=2,
        total_budget_points=200,
        max_occurrence_points=50,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(plan["id"]))
    occurrence_charge = asyncio.run(schedule_one_due_occurrence())
    system_charge = occurrence_charge["charge"] if occurrence_charge else None
    verify.check(system_charge is not None and system_charge["actor_kind"] == "system", "automatic_occurrence_preserves_system_actor")
    limit_links = int(sql_value(dsn, "SELECT COUNT(*) FROM organization_charge_limit_links WHERE charge_link_id=%s", (system_charge["id"],)))
    verify.check(limit_links == 0, "system_occurrence_never_consumes_member_limits")
    first_occurrence_id = int(occurrence_charge["id"])
    asyncio.run(release_charge(charge_link_id=system_charge["id"], reason="自动计划验证重试"))
    released_occurrence = sql_value(
        dsn,
        "SELECT json_build_object('status',status,'charge_link_id',charge_link_id) FROM organization_plan_occurrences WHERE id=%s",
        (first_occurrence_id,),
    )
    verify.check(
        released_occurrence["status"] == "retry_wait"
        and released_occurrence["charge_link_id"] is None,
        "automatic_occurrence_release_becomes_retryable_not_lost",
        released_occurrence,
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_plan_occurrences SET next_attempt_at=NOW()-INTERVAL '1 second' WHERE id=%s",
            (first_occurrence_id,),
        )
    retried_occurrence = asyncio.run(schedule_one_due_occurrence())
    verify.check(
        retried_occurrence is not None
        and int(retried_occurrence["id"]) == first_occurrence_id
        and int(retried_occurrence["charge"]["id"]) != int(system_charge["id"]),
        "automatic_retry_reuses_occurrence_with_new_charge_attempt",
        retried_occurrence,
    )
    asyncio.run(
        release_charge(
            charge_link_id=int(retried_occurrence["charge"]["id"]),
            reason="自动计划重试执行验证完成",
        )
    )

    # A permanent reservation failure releases the plan-level hold/count and
    # max_occurrences=1 cannot become completed without a settled delivery.
    single_plan = create_plan(
        owner,
        request_id="automatic-plan-single-verification-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=50,
        max_occurrence_points=50,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(single_plan["id"]))
    # Pause the earlier retryable plan so this assertion targets single_plan.
    asyncio.run(
        set_plan_status(
            owner,
            plan_id=int(plan["id"]),
            status="paused",
            expected_version=int(sql_value(dsn, "SELECT version FROM organization_automatic_plans WHERE id=%s", (plan["id"],))),
        )
    )
    os.environ["ORGANIZATION_SHARED_PAYER_ENABLED"] = "false"
    try:
        asyncio.run(schedule_one_due_occurrence())
        reserve_failed = False
    except OrganizationError:
        reserve_failed = True
    os.environ["ORGANIZATION_SHARED_PAYER_ENABLED"] = "true"
    single_state = sql_value(
        dsn,
        "SELECT json_build_object('status',status,'scheduled',scheduled_occurrences,'reserved',reserved_budget_points,'settled',settled_occurrences) FROM organization_automatic_plans WHERE id=%s",
        (single_plan["id"],),
    )
    verify.check(
        reserve_failed
        and single_state["status"] == "active"
        and int(single_state["scheduled"]) == 0
        and int(single_state["reserved"]) == 0
        and int(single_state["settled"]) == 0,
        "automatic_plan_reserve_failure_releases_count_budget_and_not_complete",
        single_state,
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_plan_occurrences SET next_attempt_at=NOW()-INTERVAL '1 second' WHERE plan_id=%s AND status='retry_wait'",
            (single_plan["id"],),
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_occurrences = list(
            executor.map(lambda _: asyncio.run(schedule_one_due_occurrence()), range(2))
        )
    occurrence_rows = int(
        sql_value(dsn, "SELECT COUNT(*) FROM organization_plan_occurrences WHERE plan_id=%s", (single_plan["id"],))
    )
    charge_rows = int(
        sql_value(
            dsn,
            "SELECT COUNT(DISTINCT charge_link_id) FROM organization_plan_occurrences WHERE plan_id=%s AND charge_link_id IS NOT NULL",
            (single_plan["id"],),
        )
    )
    single_charge_id = int(
        sql_value(dsn, "SELECT charge_link_id FROM organization_plan_occurrences WHERE plan_id=%s", (single_plan["id"],))
    )
    verify.check(
        occurrence_rows == 1 and charge_rows == 1,
        "automatic_plan_double_scheduler_reuses_one_occurrence_and_charge",
        concurrent_occurrences,
    )
    # simulate the automatic worker's durable claim, then cross the provider
    # boundary through the real authority-checked entry before settling.
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE organization_work_outbox
            SET status='claimed',claim_token='verify-automatic-worker',claimed_at=NOW(),
                lease_until=NOW()+INTERVAL '1 hour',attempts=attempts+1,updated_at=NOW()
            WHERE charge_link_id=%s
            """,
            (single_charge_id,),
        )
        cursor.execute(
            """
            UPDATE organization_charge_links
            SET attempt_token='verify-automatic-worker',lease_until=NOW()+INTERVAL '1 hour',updated_at=NOW()
            WHERE id=%s
            """,
            (single_charge_id,),
        )
        cursor.execute(
            "UPDATE organization_plan_occurrences SET status='running',updated_at=NOW() WHERE charge_link_id=%s AND status='reserved'",
            (single_charge_id,),
        )
    mark_external_side_effect_started(
        charge_link_id=single_charge_id,
        claim_token="verify-automatic-worker",
    )
    asyncio.run(
        settle_charge(
            charge_link_id=single_charge_id,
            actual_points=30,
            result_payload={"delivery": "verified"},
            claim_token="verify-automatic-worker",
        )
    )
    single_terminal = sql_value(
        dsn,
        "SELECT json_build_object('status',status,'scheduled',scheduled_occurrences,'settled',settled_occurrences,'reserved',reserved_budget_points,'consumed',consumed_budget_points) FROM organization_automatic_plans WHERE id=%s",
        (single_plan["id"],),
    )
    verify.check(
        single_terminal["status"] == "completed"
        and int(single_terminal["settled"]) == 1
        and int(single_terminal["reserved"]) == 0
        and int(single_terminal["consumed"]) == 30,
        "automatic_plan_max_one_completes_only_after_settled_delivery",
        single_terminal,
    )

    cancel_plan = create_plan(
        owner,
        request_id="automatic-plan-cancel-verification-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=50,
        max_occurrence_points=50,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(cancel_plan["id"]))
    cancel_occurrence = asyncio.run(schedule_one_due_occurrence())
    cancelled = asyncio.run(
        set_plan_status(
            owner,
            plan_id=int(cancel_plan["id"]),
            status="cancelled",
            expected_version=int(sql_value(dsn, "SELECT version FROM organization_automatic_plans WHERE id=%s", (cancel_plan["id"],))),
        )
    )
    verify.check(
        cancel_occurrence is not None
        and cancelled["status"] == "cancelled"
        and int(cancelled["reserved_budget_points"]) == 0
        and int(cancelled["scheduled_occurrences"]) == 0
        and sql_value(dsn, "SELECT status FROM organization_plan_occurrences WHERE plan_id=%s", (cancel_plan["id"],)) == "released",
        "automatic_plan_cancel_releases_bound_occurrence_and_budget",
        cancelled,
    )

    expired_dispatch_plan = create_plan(
        owner,
        request_id="automatic-plan-expired-dispatch-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=30,
        max_occurrence_points=30,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(expired_dispatch_plan["id"]))
    expired_dispatch_occurrence = asyncio.run(schedule_one_due_occurrence())
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_work_outbox SET dispatch_deadline=NOW()-INTERVAL '1 second' WHERE charge_link_id=%s",
            (expired_dispatch_occurrence["charge"]["id"],),
        )
        cursor.execute(
            "UPDATE organization_automatic_plans SET ends_at=NOW()-INTERVAL '1 second' WHERE id=%s",
            (expired_dispatch_plan["id"],),
        )
    expired_claim = claim_work(
        worker_id="automatic-expired-claim-must-not-run",
        automatic_system_only=True,
    )
    expired_recovery = asyncio.run(recover_stale_work())
    verify.check(
        expired_claim is None
        and int(expired_dispatch_occurrence["charge"]["id"]) in expired_recovery["released"],
        "automatic_expired_dispatch_cannot_be_claimed_and_is_released",
        {"claim": expired_claim, "recovery": expired_recovery},
    )

    expiry_after_claim_plan = create_plan(
        owner,
        request_id="automatic-plan-expiry-after-claim-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=30,
        max_occurrence_points=30,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(expiry_after_claim_plan["id"]))
    expiry_after_claim_occurrence = asyncio.run(schedule_one_due_occurrence())
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_automatic_plans SET ends_at=NOW()-INTERVAL '1 second' WHERE id=%s",
            (expiry_after_claim_plan["id"],),
        )
    expiry_provider_calls = {"count": 0}

    async def _expired_plan_provider_must_not_run(
        _request, *, organization_identity=None, **_kwargs,
    ):
        expiry_provider_calls["count"] += 1
        return {"status": "success", "task_id": 999998}

    live_monitoring_api.api_run_monitoring = _expired_plan_provider_must_not_run
    expiry_after_claim_result = asyncio.run(
        dispatch_one_automatic_work(worker_id="automatic-expiry-after-claim-worker")
    )
    verify.check(
        expiry_after_claim_result["status"] == "retry_wait"
        and expiry_after_claim_result["error_code"] == "ORG_PLAN_INACTIVE"
        and expiry_provider_calls["count"] == 0
        and sql_value(
            dsn,
            "SELECT status FROM organization_charge_links WHERE id=%s",
            (expiry_after_claim_occurrence["charge"]["id"],),
        ) == "released",
        "automatic_plan_expiry_after_claim_stops_before_provider",
        expiry_after_claim_result,
    )

    envelope_tamper_plan = create_plan(
        owner,
        request_id="automatic-plan-envelope-tamper-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10, "platforms": ["dashscope"]},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=30,
        max_occurrence_points=30,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_automatic_plans SET next_occurrence_at=NOW()+INTERVAL '1 hour' WHERE status='active' AND id<>%s",
            (envelope_tamper_plan["id"],),
        )
        cursor.execute(
            "UPDATE organization_automatic_plans SET next_occurrence_at=NOW()-INTERVAL '1 second' WHERE id=%s",
            (envelope_tamper_plan["id"],),
        )
    envelope_tamper_occurrence = asyncio.run(schedule_one_due_occurrence())
    with connect(dsn) as conn:
        conn.cursor().execute(
            """
            UPDATE organization_work_outbox
            SET payload_snapshot=jsonb_set(payload_snapshot,'{request_payload,platforms}','[\"deepseek\"]'::jsonb)
            WHERE charge_link_id=%s
            """,
            (envelope_tamper_occurrence["charge"]["id"],),
        )
    envelope_tamper_provider_calls = {"count": 0}

    async def _envelope_tamper_provider_must_not_run(
        _request, *, organization_identity=None, **_kwargs,
    ):
        envelope_tamper_provider_calls["count"] += 1
        return {"status": "success", "task_id": 101010}

    original_envelope_tamper_provider = live_monitoring_api.api_run_monitoring
    live_monitoring_api.api_run_monitoring = _envelope_tamper_provider_must_not_run
    try:
        envelope_tamper_dispatch = asyncio.run(
            dispatch_one_automatic_work(worker_id="automatic-envelope-tamper-worker")
        )
    finally:
        live_monitoring_api.api_run_monitoring = original_envelope_tamper_provider
    verify.check(
        envelope_tamper_dispatch["status"] == "retry_wait"
        and envelope_tamper_dispatch["error_code"] == "ORG_CHARGE_AUTHORITY_EVIDENCE_CORRUPT"
        and envelope_tamper_provider_calls["count"] == 0
        and sql_value(
            dsn,
            "SELECT status FROM organization_charge_links WHERE id=%s",
            (envelope_tamper_occurrence["charge"]["id"],),
        ) == "released",
        "automatic_worker_rejects_tampered_v2_envelope_before_provider_and_releases",
        envelope_tamper_dispatch,
    )
    awaitable_pause_version = int(
        sql_value(
            dsn,
            "SELECT version FROM organization_automatic_plans WHERE id=%s",
            (envelope_tamper_plan["id"],),
        )
    )
    asyncio.run(
        set_plan_status(
            owner,
            plan_id=int(envelope_tamper_plan["id"]),
            status="paused",
            expected_version=awaitable_pause_version,
        )
    )

    lock_order_plan = create_plan(
        owner,
        request_id="automatic-plan-lock-order-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=30,
        max_occurrence_points=30,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(lock_order_plan["id"]))
    lock_order_occurrence = asyncio.run(schedule_one_due_occurrence())
    lock_order_claim = claim_work(
        worker_id="automatic-lock-order-claim",
        lease_seconds=60,
        automatic_system_only=True,
    )
    lock_order_barrier = threading.Barrier(2)

    def _race_start_work():
        lock_order_barrier.wait()
        try:
            return start_work(
                outbox_id=int(lock_order_claim["id"]),
                claim_token=str(lock_order_claim["claim_token"]),
            )
        except Exception as exc:
            return exc

    def _race_release_work():
        lock_order_barrier.wait()
        try:
            return asyncio.run(
                release_charge(
                    charge_link_id=int(lock_order_occurrence["charge"]["id"]),
                    reason="charge-outbox lock order race verification",
                )
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        start_future = executor.submit(_race_start_work)
        release_future = executor.submit(_race_release_work)
        lock_order_results = [start_future.result(), release_future.result()]
    verify.check(
        not any("deadlock" in str(item).lower() for item in lock_order_results)
        and sql_value(
            dsn,
            "SELECT status FROM organization_charge_links WHERE id=%s",
            (lock_order_occurrence["charge"]["id"],),
        ) == "released",
        "worker_start_and_release_share_charge_then_outbox_lock_order",
        [str(item) for item in lock_order_results],
    )
    asyncio.run(
        set_plan_status(
            owner,
            plan_id=int(lock_order_plan["id"]),
            status="paused",
            expected_version=int(
                sql_value(
                    dsn,
                    "SELECT version FROM organization_automatic_plans WHERE id=%s",
                    (lock_order_plan["id"],),
                )
            ),
        )
    )

    try:
        create_plan(
            owner,
            request_id="automatic-plan-cancel-verification-0001",
            feature_code="monitor_single",
            work_kind="monitoring.run",
            payload={"brand_id": 10},
            cadence_seconds=120,
            max_occurrences=1,
            total_budget_points=50,
            max_occurrence_points=50,
            starts_at=now - timedelta(seconds=1),
            ends_at=now + timedelta(minutes=5),
            brand_id=10,
        )
        plan_idempotency_rejected = False
    except OrganizationError as exc:
        plan_idempotency_rejected = exc.code == "ORG_IDEMPOTENCY_CONFLICT"
    verify.check(
        plan_idempotency_rejected,
        "automatic_plan_same_key_different_schedule_rejected",
    )

    # The cron worker now dispatches the live monitoring handler instead of
    # leaving a wallet freeze/outbox with no business executor. Twenty workers
    # race one occurrence; exactly one produces and settles one system artifact.
    dispatch_plan = create_plan(
        owner,
        request_id="automatic-plan-dispatch-verification-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10, "platforms": ["dashscope"]},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=30,
        max_occurrence_points=30,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(dispatch_plan["id"]))
    dispatched_occurrence = asyncio.run(schedule_one_due_occurrence())

    automatic_dispatch_scope = {}

    async def _fake_automatic_monitoring(
        _request, *, organization_identity=None, **_kwargs,
    ):
        automatic_dispatch_scope["platforms"] = list(_request.platforms or [])
        automatic_dispatch_scope["keyword_keys"] = list(_request.keyword_keys or [])
        with connect(dsn) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO monitoring_tasks(brand_id) VALUES (10) RETURNING id")
            automatic_task_id = int(cursor.fetchone()["id"])
        return {"status": "success", "task_id": automatic_task_id, "total_tests": 1}

    live_monitoring_api.api_run_monitoring = _fake_automatic_monitoring
    with ThreadPoolExecutor(max_workers=20) as executor:
        dispatch_results = list(
            executor.map(
                lambda worker_no: asyncio.run(
                    dispatch_one_automatic_work(worker_id=f"automatic-race-{worker_no:02d}")
                ),
                range(20),
            )
        )
    successful_dispatches = [
        result for result in dispatch_results if result and result.get("status") == "succeeded"
    ]
    dispatched_task_id = int(successful_dispatches[0]["result"]["task_id"]) if successful_dispatches else 0
    verify.check(
        dispatched_occurrence is not None
        and len(successful_dispatches) == 1
        and int(sql_value(dsn, "SELECT COUNT(*) FROM organization_plan_occurrences WHERE plan_id=%s", (dispatch_plan["id"],))) == 1
        and sql_value(dsn, "SELECT status FROM organization_automatic_plans WHERE id=%s", (dispatch_plan["id"],)) == "completed"
        and automatic_dispatch_scope["platforms"] == ["dashscope"]
        and int(sql_value(dsn, "SELECT COUNT(*) FROM monitoring_tasks WHERE id=%s AND organization_id=%s AND created_by_actor_kind='system' AND created_by_user_id=%s AND responsible_user_id=%s AND created_by_membership_id IS NULL", (dispatched_task_id, organization_id, owner.authenticated_user_id, owner.authenticated_user_id))) == 1
        and int(sql_value(dsn, "SELECT COUNT(*) FROM organization_charge_limit_links WHERE charge_link_id=%s", (dispatched_occurrence["charge"]["id"],))) == 0,
        "automatic_worker_20_way_race_dispatches_one_system_occurrence_once",
        {"results": dispatch_results, "scope": automatic_dispatch_scope},
    )

    # Once the provider boundary is crossed, a lost response must not release
    # the owner wallet/plan hold or make the occurrence runnable again.
    unknown_plan = create_plan(
        owner,
        request_id="automatic-plan-unknown-verification-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=30,
        max_occurrence_points=30,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(unknown_plan["id"]))
    unknown_occurrence = asyncio.run(schedule_one_due_occurrence())
    unknown_provider_calls = {"count": 0}

    async def _lost_automatic_provider_response(
        _request, *, organization_identity=None, **_kwargs,
    ):
        unknown_provider_calls["count"] += 1
        raise TimeoutError("automatic provider response lost")

    live_monitoring_api.api_run_monitoring = _lost_automatic_provider_response
    unknown_dispatch = asyncio.run(
        dispatch_one_automatic_work(worker_id="automatic-unknown-worker")
    )
    unknown_replay = asyncio.run(
        dispatch_one_automatic_work(worker_id="automatic-unknown-replay-worker")
    )
    unknown_state = sql_value(
        dsn,
        """
        SELECT json_build_object(
          'charge',c.status,'outbox',w.status,'occurrence',x.status,
          'plan_status',p.status,'scheduled',p.scheduled_occurrences,
          'settled',p.settled_occurrences,'reserved',p.reserved_budget_points,
          'consumed',p.consumed_budget_points
        )
        FROM organization_plan_occurrences x
        JOIN organization_automatic_plans p ON p.id=x.plan_id
        JOIN organization_charge_links c ON c.id=x.charge_link_id
        JOIN organization_work_outbox w ON w.charge_link_id=c.id
        WHERE x.id=%s
        """,
        (unknown_occurrence["id"],),
    )
    verify.check(
        unknown_dispatch["status"] == "unknown"
        and unknown_replay is None
        and unknown_provider_calls["count"] == 1
        and unknown_state["charge"] == "unknown"
        and unknown_state["outbox"] == "unknown"
        and unknown_state["occurrence"] == "unknown"
        and unknown_state["plan_status"] == "active"
        and int(unknown_state["scheduled"]) == 1
        and int(unknown_state["settled"]) == 0
        and int(unknown_state["reserved"]) == 30
        and int(unknown_state["consumed"]) == 0,
        "automatic_worker_lost_response_quarantines_without_retry_or_budget_release",
        {"dispatch": unknown_dispatch, "state": unknown_state},
    )

    # A scope that grows beyond the immutable ceiling is stopped before the
    # provider call and releases both the occurrence hold and wallet freeze.
    ceiling_plan = create_plan(
        owner,
        request_id="automatic-plan-ceiling-verification-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=30,
        max_occurrence_points=30,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(ceiling_plan["id"]))
    ceiling_occurrence = asyncio.run(schedule_one_due_occurrence())
    import services.organization_worker as organization_worker_module

    original_prepare_automatic = organization_worker_module._prepare_automatic_monitoring
    organization_worker_module._prepare_automatic_monitoring = lambda _payload, _brand_id: (object(), 60)
    provider_calls = {"count": 0}

    async def _must_not_call_provider(
        _request, *, organization_identity=None, **_kwargs,
    ):
        provider_calls["count"] += 1
        return {"status": "success", "task_id": 999999}

    live_monitoring_api.api_run_monitoring = _must_not_call_provider
    try:
        ceiling_result = asyncio.run(
            dispatch_one_automatic_work(worker_id="automatic-ceiling-worker")
        )
    finally:
        organization_worker_module._prepare_automatic_monitoring = original_prepare_automatic
    verify.check(
        ceiling_occurrence is not None
        and ceiling_result["status"] == "retry_wait"
        and ceiling_result["error_code"] == "ORG_ACTUAL_EXCEEDS_CEILING"
        and provider_calls["count"] == 0
        and sql_value(dsn, "SELECT status FROM organization_charge_links WHERE id=%s", (ceiling_occurrence["charge"]["id"],)) == "released"
        and int(sql_value(dsn, "SELECT reserved_budget_points FROM organization_automatic_plans WHERE id=%s", (ceiling_plan["id"],))) == 0,
        "automatic_worker_ceiling_stops_before_provider_and_releases_hold",
        ceiling_result,
    )

    # Crash after worker start but before the provider marker is unambiguous:
    # recovery releases it rather than quarantining it as an external unknown.
    pre_provider_plan = create_plan(
        owner,
        request_id="automatic-plan-pre-provider-crash-0001",
        feature_code="monitor_single",
        work_kind="monitoring.run",
        payload={"brand_id": 10},
        cadence_seconds=60,
        max_occurrences=1,
        total_budget_points=30,
        max_occurrence_points=30,
        starts_at=now - timedelta(seconds=1),
        ends_at=now + timedelta(minutes=5),
        brand_id=10,
    )
    force_plan_due(dsn, int(pre_provider_plan["id"]))
    pre_provider_occurrence = asyncio.run(schedule_one_due_occurrence())
    verify.check(
        pre_provider_occurrence is not None
        and int(
            sql_value(
                dsn,
                "SELECT plan_id FROM organization_plan_occurrences WHERE id=%s",
                (pre_provider_occurrence["id"],),
            )
        ) == int(pre_provider_plan["id"]),
        "automatic_retry_wait_plan_does_not_starve_other_due_plan",
        pre_provider_occurrence,
    )
    pre_provider_claim = claim_work(
        worker_id="automatic-pre-provider-crash",
        lease_seconds=30,
        automatic_system_only=True,
    )
    start_work(
        outbox_id=int(pre_provider_claim["id"]),
        claim_token=str(pre_provider_claim["claim_token"]),
    )
    with connect(dsn) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE organization_work_outbox SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s",
            (pre_provider_claim["id"],),
        )
    pre_provider_recovery = asyncio.run(recover_stale_work())
    verify.check(
        pre_provider_occurrence is not None
        and int(pre_provider_occurrence["charge"]["id"]) in pre_provider_recovery["released"]
        and int(pre_provider_occurrence["charge"]["id"]) not in pre_provider_recovery["unknown"]
        and sql_value(dsn, "SELECT status FROM organization_plan_occurrences WHERE plan_id=%s", (pre_provider_plan["id"],)) == "retry_wait",
        "automatic_worker_kill_before_provider_is_released_and_retryable",
        pre_provider_recovery,
    )

    final_consistency = billing_consistency()
    verify.check(final_consistency["ready"], "final_billing_consistency", final_consistency)
    summary = {
        "status": "passed",
        "tests": len(verify.passed),
        "failed": 0,
        "skipped": 0,
        "schema": schema,
        "postgres_version": sql_value(dsn, "SHOW server_version"),
        "cases": verify.passed,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    db_connection.close_pool()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "failed": 1, "skipped": 0, "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise
