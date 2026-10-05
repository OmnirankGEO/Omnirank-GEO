import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_local import (
    BASE_SQL, MIGRATION_SQL, ONBOARDING_MIGRATION_SQL, CROSS_TENANT_MIGRATION_SQL,
    create_schema, execute_sql, isolate_public_qualified_sql, connect,
)

PAYER_SQL = (ROOT / "scripts" / "migration_organization_payer_policies_2026_07_23.sql").read_text(encoding="utf-8")

DSN = "postgresql://postgres:test_org_wallet_pw@localhost:55499/test_org_wallet"
schema, dsn = create_schema(DSN, "org_payer_sign")
execute_sql(dsn, BASE_SQL)
execute_sql(dsn, MIGRATION_SQL)
execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
execute_sql(dsn, MIGRATION_SQL)
execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
cross = isolate_public_qualified_sql(CROSS_TENANT_MIGRATION_SQL, schema)
execute_sql(dsn, cross)
execute_sql(dsn, cross)
execute_sql(dsn, PAYER_SQL)
execute_sql(dsn, PAYER_SQL)

from services.organization_schema_contract import catalog_fingerprint
with connect(dsn) as conn:
    result = catalog_fingerprint(conn.cursor())
print(json.dumps(result, indent=2, ensure_ascii=False))
