"""R3 verification: missing invite polling policy creates a catalog version.

Published catalog entries are immutable. The onboarding migration must clone
an operational zero-price policy that lacks ``invite.delivery_status`` into a
new published version, archive the old version, and remain idempotent.
Malformed policies with no rate-limit provenance must fail closed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "organization_internal_seats"))

from verify_local import (  # noqa: E402
    BASE_SQL,
    MIGRATION_SQL,
    ONBOARDING_MIGRATION_SQL,
    connect,
    create_schema,
    execute_sql,
)


DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:test_org_wallet_pw@localhost:55499/test_org_wallet",
)


schema, dsn = create_schema(DSN, "org_r3_catalog_version")
execute_sql(dsn, BASE_SQL)
execute_sql(dsn, MIGRATION_SQL)
execute_sql(dsn, ONBOARDING_MIGRATION_SQL)

with connect(dsn) as conn:
    cursor = conn.cursor()
    cursor.execute(
        """SELECT v.id AS version_id,e.id AS entry_id,e.source_ref_jsonb
             FROM pricing_catalog_versions v
             JOIN pricing_catalog_entries e ON e.version_id=v.id
            WHERE v.catalog_type='feature_consumption'
              AND v.scope_key='ORGANIZATION_SEATS'
              AND v.status='published'
              AND v.effective_to IS NULL
              AND e.product_code='organization_internal_seats'"""
    )
    original = cursor.fetchone()
    assert original
    original_version_id = int(original["version_id"])
    original_entry_id = int(original["entry_id"])
    seeded = original["source_ref_jsonb"]
    assert (seeded.get("rate_limits") or {}).get("invite.delivery_status")
    cursor.execute(
        """UPDATE pricing_catalog_entries
              SET source_ref_jsonb=jsonb_set(
                    source_ref_jsonb,
                    '{rate_limits}',
                    (source_ref_jsonb->'rate_limits') - 'invite.delivery_status'
                  )
            WHERE id=%s""",
        (original_entry_id,),
    )
    cursor.execute(
        """
        CREATE FUNCTION test_published_catalog_entry_guard()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        DECLARE target_status TEXT;
        BEGIN
          SELECT status INTO target_status
            FROM pricing_catalog_versions
           WHERE id=COALESCE(NEW.version_id,OLD.version_id);
          IF target_status='published' THEN
            RAISE EXCEPTION 'published catalog entry is immutable';
          END IF;
          RETURN COALESCE(NEW,OLD);
        END $$;
        CREATE TRIGGER test_published_catalog_entry_guard
          BEFORE UPDATE OR DELETE ON pricing_catalog_entries
          FOR EACH ROW EXECUTE FUNCTION test_published_catalog_entry_guard()
        """
    )

execute_sql(dsn, ONBOARDING_MIGRATION_SQL)

with connect(dsn) as conn:
    cursor = conn.cursor()
    cursor.execute(
        """SELECT v.id AS version_id,v.version_code,v.status,e.id AS entry_id,
                  e.source_ref_jsonb
             FROM pricing_catalog_versions v
             JOIN pricing_catalog_entries e ON e.version_id=v.id
            WHERE v.catalog_type='feature_consumption'
              AND v.scope_key='ORGANIZATION_SEATS'
              AND e.product_code='organization_internal_seats'
            ORDER BY v.id"""
    )
    versions = cursor.fetchall()
    assert len(versions) == 2
    archived, current = versions
    assert int(archived["version_id"]) == original_version_id
    assert archived["status"] == "archived"
    assert "invite.delivery_status" not in archived["source_ref_jsonb"]["rate_limits"]
    assert current["status"] == "published"
    assert int(current["version_id"]) != original_version_id
    assert int(current["entry_id"]) != original_entry_id
    assert current["version_code"].endswith("-delivery-status-v1")
    expected_bucket = {
        "window_seconds": 3600,
        "organization": 100,
        "target": 60,
        "ip": 100,
    }
    assert current["source_ref_jsonb"]["rate_limits"]["invite.delivery_status"] == expected_bucket
    assert current["source_ref_jsonb"]["included_seats"] == seeded["included_seats"]
    cursor.execute(
        """SELECT COUNT(*) AS count
             FROM organization_product_config_publications
            WHERE product_catalog_version_id=%s
              AND product_catalog_entry_id=%s""",
        (current["version_id"], current["entry_id"]),
    )
    assert int(cursor.fetchone()["count"]) == 1
    current_version_id = int(current["version_id"])
    current_entry_id = int(current["entry_id"])
    current_config = current["source_ref_jsonb"]

execute_sql(dsn, ONBOARDING_MIGRATION_SQL)

with connect(dsn) as conn:
    cursor = conn.cursor()
    cursor.execute(
        """SELECT COUNT(*) AS versions
             FROM pricing_catalog_versions
            WHERE catalog_type='feature_consumption'
              AND scope_key='ORGANIZATION_SEATS'"""
    )
    assert int(cursor.fetchone()["versions"]) == 2
    cursor.execute(
        """SELECT v.id AS version_id,e.id AS entry_id,e.source_ref_jsonb
             FROM pricing_catalog_versions v
             JOIN pricing_catalog_entries e ON e.version_id=v.id
            WHERE v.catalog_type='feature_consumption'
              AND v.scope_key='ORGANIZATION_SEATS'
              AND v.status='published'
              AND v.effective_to IS NULL"""
    )
    replay = cursor.fetchone()
    assert int(replay["version_id"]) == current_version_id
    assert int(replay["entry_id"]) == current_entry_id
    assert replay["source_ref_jsonb"] == current_config

with connect(dsn) as conn:
    cursor = conn.cursor()
    cursor.execute(
        "ALTER TABLE pricing_catalog_entries DISABLE TRIGGER test_published_catalog_entry_guard"
    )
    cursor.execute(
        "UPDATE pricing_catalog_entries SET source_ref_jsonb=source_ref_jsonb-'rate_limits' WHERE id=%s",
        (current_entry_id,),
    )
    cursor.execute(
        "ALTER TABLE pricing_catalog_entries ENABLE TRIGGER test_published_catalog_entry_guard"
    )

try:
    execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
except Exception as exc:
    assert "rate-limit provenance is incomplete" in str(exc)
else:
    raise AssertionError("missing rate-limit provenance must fail closed")

print("VERSIONED CATALOG UPGRADE CHECK: ALL OK")
