"""Scratch capture: dump actual pg_catalog definitions after the pinned payer migration.

Used once to author the frozen expected manifest inside
scripts/sign_production_payer_catalog_variant.py. Runs against the loopback
throwaway PG16 container only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from verify_local import (  # noqa: E402
    BASE_SQL,
    CROSS_TENANT_MIGRATION_SQL,
    MIGRATION_SQL,
    ONBOARDING_MIGRATION_SQL,
    PAYER_MIGRATION_SQL,
    connect,
    create_schema,
    execute_sql,
    isolate_public_qualified_sql,
)

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:test_org_wallet_pw@localhost:55499/test_org_wallet")


def main() -> None:
    schema, dsn = create_schema(DSN, "org_capture_defs")
    execute_sql(dsn, BASE_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    execute_sql(dsn, MIGRATION_SQL)
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
    cross = isolate_public_qualified_sql(CROSS_TENANT_MIGRATION_SQL, schema)
    execute_sql(dsn, cross)
    execute_sql(dsn, cross)
    execute_sql(dsn, PAYER_MIGRATION_SQL)
    execute_sql(dsn, PAYER_MIGRATION_SQL)

    out: dict = {}
    with connect(dsn) as conn:
        cur = conn.cursor()
        # Columns of the two new tables + 5 new columns on organization_charge_links
        cur.execute(
            """
            SELECT cl.relname AS table_name,a.attname AS column_name,a.attnum AS ordinal,
                   format_type(a.atttypid,a.atttypmod) AS data_type,a.attnotnull AS not_null,
                   a.attidentity AS identity,a.attgenerated AS generated,
                   pg_get_expr(ad.adbin,ad.adrelid,TRUE) AS default_expr
            FROM pg_attribute a
            JOIN pg_class cl ON cl.oid=a.attrelid
            JOIN pg_namespace ns ON ns.oid=cl.relnamespace
            LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum
            WHERE ns.nspname=current_schema() AND a.attnum>0 AND NOT a.attisdropped
              AND (cl.relname IN ('organization_payer_policies','organization_payer_policy_events')
                   OR (cl.relname='organization_charge_links' AND a.attname IN
                       ('payer_policy_version','owner_consent_snapshot','employee_limit_snapshot',
                        'within_limit_points','overage_points')))
            ORDER BY cl.relname,a.attnum
            """
        )
        out["columns"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT cl.relname AS table_name,co.conname AS name,co.contype AS type,
                   co.convalidated AS validated,pg_get_constraintdef(co.oid,TRUE) AS definition
            FROM pg_constraint co
            JOIN pg_class cl ON cl.oid=co.conrelid
            JOIN pg_namespace ns ON ns.oid=cl.relnamespace
            WHERE ns.nspname=current_schema()
              AND (cl.relname IN ('organization_payer_policies','organization_payer_policy_events')
                   OR (cl.relname='organization_charge_links' AND co.conname IN
                       ('organization_charge_payer_split_valid','organization_charge_overage_consent_required')))
            ORDER BY cl.relname,co.conname
            """
        )
        out["constraints"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT tc.relname AS table_name,ic.relname AS name,ix.indisunique AS is_unique,
                   ix.indisvalid AS is_valid,ix.indisready AS is_ready,
                   ix.indnkeyatts AS key_count,
                   replace(pg_get_indexdef(ix.indexrelid),format('%I.',current_schema()),'') AS definition,
                   pg_get_expr(ix.indpred,ix.indrelid,TRUE) AS predicate,
                   ARRAY(
                     SELECT opc.opcname
                     FROM unnest(ix.indclass::oid[]) WITH ORDINALITY v(opcoid,ord)
                     JOIN pg_opclass opc ON opc.oid=v.opcoid ORDER BY v.ord
                   ) AS opclasses
            FROM pg_index ix
            JOIN pg_class ic ON ic.oid=ix.indexrelid
            JOIN pg_class tc ON tc.oid=ix.indrelid
            JOIN pg_namespace ns ON ns.oid=tc.relnamespace
            WHERE ns.nspname=current_schema()
              AND (tc.relname IN ('organization_payer_policies','organization_payer_policy_events')
                   OR (tc.relname='organization_charge_links' AND ic.relname='idx_org_charge_overage_period'))
            ORDER BY tc.relname,ic.relname
            """
        )
        out["indexes"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT cl.relname AS table_name,tg.tgname AS name,
                   pg_get_triggerdef(tg.oid,TRUE) AS definition
            FROM pg_trigger tg
            JOIN pg_class cl ON cl.oid=tg.tgrelid
            JOIN pg_namespace ns ON ns.oid=cl.relnamespace
            WHERE ns.nspname=current_schema() AND NOT tg.tgisinternal
              AND cl.relname='organization_payer_policy_events'
            ORDER BY cl.relname,tg.tgname
            """
        )
        out["triggers"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT p.proname AS name,pg_get_function_identity_arguments(p.oid) AS args,
                   pg_get_functiondef(p.oid) AS definition
            FROM pg_proc p
            JOIN pg_namespace ns ON ns.oid=p.pronamespace
            WHERE ns.nspname=current_schema() AND p.prokind='f'
              AND p.proname='organization_payer_policy_events_append_only'
            """
        )
        out["functions"] = [dict(r) for r in cur.fetchall()]
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
