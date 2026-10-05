"""[R5 ⑤] 本目录夹具的 brands 保真锁 —— 换回手搓即红。

原 base_fixture.sql 手搓了 7 列的 brands
(id / owner_user_id NOT NULL REFERENCES users(id) / name / is_deleted /
 deleted_at TIMESTAMPTZ / deleted_reason / created_at TIMESTAMPTZ),生产 32 列,
且生产 deleted_at/created_at 是 `timestamp without time zone`、owner_user_id 无 FK。

本目录的夹具是**一整段 SQL 字符串**(还要被 .replace() 做变体),
所以走 SSOT 的纯 SQL 形态 `brands_schema_sql()`,拼在 BASE_SQL 最前面。
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.brands_fixture_lock import (  # noqa: E402
    assert_brands_has_production_unique_index,
    assert_brands_is_production_shaped,
)
from verify_local import BASE_SQL, create_schema, execute_sql  # noqa: E402

DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:test_org_wallet_pw@localhost:55499/test_org_wallet",
)
if (urlsplit(DSN).hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
    raise RuntimeError("brands fixture lock only accepts a loopback throwaway PostgreSQL DSN")


@pytest.fixture(scope="module")
def base_schema_conn():
    _schema, dsn = create_schema(DSN, "org_seats_brands_lock")
    execute_sql(dsn, BASE_SQL)
    conn = psycopg2.connect(dsn)
    yield conn
    conn.close()


def test_base_fixture_brands_is_built_by_the_production_ssot_export(base_schema_conn):
    assert_brands_is_production_shaped(base_schema_conn)
    assert_brands_has_production_unique_index(base_schema_conn)


def test_base_fixture_sql_does_not_hand_author_brands():
    """结构锁:base_fixture.sql 里不许再出现 `CREATE TABLE brands (`。

    列锁挡的是"建出来的形状不对";这一条挡的是"有人又在 .sql 里手搓一份"
    —— 两把锁堵的是同一个口子的两端。
    """
    sql_text = (Path(__file__).with_name("base_fixture.sql")).read_text(encoding="utf-8")
    assert "CREATE TABLE brands" not in sql_text, (
        "base_fixture.sql 又手搓 brands 了 —— 应当由 brands_schema_sql() 提供")
    assert "CREATE TABLE IF NOT EXISTS brands" in BASE_SQL, (
        "BASE_SQL 里没有 SSOT 出口拼进来的 brands 建表 —— 拼接被拆了?")
