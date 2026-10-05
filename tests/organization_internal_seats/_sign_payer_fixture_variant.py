import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_local import (
    BASE_SQL, MIGRATION_SQL, ONBOARDING_MIGRATION_SQL,
    create_schema, execute_sql, connect,
)

PAYER_SQL = (ROOT / "scripts" / "migration_organization_payer_policies_2026_07_23.sql").read_text(encoding="utf-8")

DSN = "postgresql://postgres:test_org_wallet_pw@localhost:55499/test_org_wallet"
schema, dsn = create_schema(DSN, "org_payer_sign_fixture")
production_upgrade_sql = BASE_SQL.replace(
    "CREATE TABLE publish_orders (id BIGSERIAL PRIMARY KEY);",
    "CREATE TABLE publish_orders (id BIGSERIAL PRIMARY KEY, brand_id INTEGER NOT NULL);",
)
execute_sql(dsn, production_upgrade_sql)
execute_sql(dsn, MIGRATION_SQL)
execute_sql(dsn, ONBOARDING_MIGRATION_SQL)
execute_sql(dsn, PAYER_SQL)
execute_sql(dsn, PAYER_SQL)

from services.organization_schema_contract import catalog_fingerprint
with connect(dsn) as conn:
    result = catalog_fingerprint(conn.cursor())
print(json.dumps(result, indent=2, ensure_ascii=False))
