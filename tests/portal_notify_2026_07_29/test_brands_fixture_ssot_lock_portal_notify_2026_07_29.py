"""[R5 ⑤] 本目录夹具的 brands 保真锁 —— 换回手搓即红。

原夹具在 conftest.SCHEMA_SQL 里手搓了 8 列的 brands,且 `name` 是**可空**的
(生产是 NOT NULL)。已改调 db.brands_schema.ensure_brands_schema()。
"""
import psycopg2

from tests.brands_fixture_lock import (
    assert_brands_has_production_unique_index,
    assert_brands_is_production_shaped,
)

from conftest import _connect  # 本目录不是 package,pytest 把 conftest 放在 sys.path 上


def test_fixture_brands_is_built_by_the_production_ssot_export(_schema):
    conn = _connect()
    try:
        assert_brands_is_production_shaped(conn)
        assert_brands_has_production_unique_index(conn)
    finally:
        conn.close()


def test_fixture_brands_name_is_not_nullable_like_production(_schema):
    """手搓版 `name TEXT`(可空)与生产 `name TEXT NOT NULL` 的差已被消除。

    可空的 name 让夹具能插进生产会拒的行 —— 这类行上跑出来的绿不算数。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT is_nullable FROM information_schema.columns"
                    " WHERE table_schema='public' AND table_name='brands'"
                    " AND column_name='name'")
        row = cur.fetchone()
        assert row, "brands 没有 name 列"
        val = row["is_nullable"] if isinstance(row, dict) else row[0]
        assert val == "NO", "brands.name 可空,与生产(NOT NULL)不一致"
    finally:
        conn.close()
