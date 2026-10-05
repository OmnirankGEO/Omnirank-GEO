"""[R5 ⑤] 本目录夹具的 brands 保真锁 —— 换回手搓即红。

原夹具手搓的 brands **只有 2 列**(id / owner_user_id NOT NULL REFERENCES users(id)),
生产 32 列且 owner_user_id 是 nullable 无 FK。
转换当场暴露了一条真问题:夹具那句 `INSERT INTO brands(id,owner_user_id) VALUES (1,10)`
在生产 schema 下会被 `name NOT NULL` 拒 —— 手搓窄表把它藏了不知道多久。
"""
import psycopg2

from tests.brands_fixture_lock import (
    assert_brands_has_production_unique_index,
    assert_brands_is_production_shaped,
)


def test_fixture_brands_is_built_by_the_production_ssot_export(notification_database_url):
    conn = psycopg2.connect(notification_database_url)
    try:
        assert_brands_is_production_shaped(conn)
        assert_brands_has_production_unique_index(conn)
    finally:
        conn.close()


def test_fixture_brands_row_would_be_accepted_by_production(notification_database_url):
    """夹具种下的那一行必须是**生产也收得下**的行(name 非空)。"""
    conn = psycopg2.connect(notification_database_url)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM brands ORDER BY id")
        rows = cur.fetchall()
        assert rows, "夹具没种出 brands 行 —— 零分母"
        for r in rows:
            name = r["name"] if isinstance(r, dict) else r[1]
            assert name, "夹具种了一行 name 为空的 brands,生产会拒:%s" % (r,)
    finally:
        conn.close()
