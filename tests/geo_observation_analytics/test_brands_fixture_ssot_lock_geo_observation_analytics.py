"""[R5 ⑤] 本目录夹具的 brands 保真锁 —— 换回手搓即红。

原夹具在 conftest.BRANDS_SCHEMA 里手搓了 6 列的 brands
(id/name/owner_user_id/industry/status/is_deleted),生产 32 列。
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
