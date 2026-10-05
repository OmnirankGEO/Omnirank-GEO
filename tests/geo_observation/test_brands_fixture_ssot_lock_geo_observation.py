"""[R5 ⑤] 本目录夹具的 brands 保真锁 —— 换回手搓即红。

原夹具在 conftest.BASE_SCHEMA 里手搓了 8 列的 brands(id/name/company_name/
brand_display_names/industry/industry_category/owner_user_id/status),
生产是 32 列。已改调 db.brands_schema.ensure_brands_schema()。
"""
import os

import psycopg2

from tests.brands_fixture_lock import (
    assert_brands_has_production_unique_index,
    assert_brands_is_production_shaped,
)


def test_fixture_brands_is_built_by_the_production_ssot_export(isolated_postgres_schema):
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    try:
        assert_brands_is_production_shaped(conn)
        assert_brands_has_production_unique_index(conn)
    finally:
        conn.close()
