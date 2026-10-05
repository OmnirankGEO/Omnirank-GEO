"""[WO-PUB-ZOMBIE-2026-08-04] 真库 fixture。

刻意用 `db.meijiehezi_db.init_mhz_tables()` 建 mhz 那几张表,**不手搓 DDL**:
手搓的表一旦和真 DDL 漂移,针对 SQL 的断言就变成在测一张不存在的表
(仓内有过"fixture 手搓建表把真 init 的 IF NOT EXISTS 变成 no-op"的前科)。
只有 mhz 之外的周边表(通知 outbox / 角色)才手写,且只建被读到的那几列。
"""
import pytest

from db.connection import get_connection

_SIDE_TABLES = """
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER,
    role_id INTEGER
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id SERIAL PRIMARY KEY,
    event_key TEXT UNIQUE,
    event_type TEXT,
    business_id TEXT,
    terminal_state TEXT,
    recipient_user_id INTEGER,
    recipient_kind TEXT,
    level TEXT,
    title TEXT,
    content TEXT,
    route TEXT,
    privacy_policy TEXT,
    payload JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
"""


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from pathlib import Path

    from db.meijiehezi_db import init_mhz_tables

    init_mhz_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(_SIDE_TABLES)
        # routed_provider/routed_media_id/routed_cost_yuan 不在 init_mhz_tables 里,
        # 它们由这份已登记在 db/migration_manifest.py 的迁移加。清扫器的候选 SQL
        # 读 routed_media_id,不跑这份迁移的话测的就不是生产那张表。
        repo = Path(__file__).resolve().parents[2]
        cur.execute((repo / "scripts" / "migration_media_provider_routing_2026_08_02.sql")
                    .read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db():
    """每个用例前清空本包会用到的表,避免用例互相污染。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("TRUNCATE mhz_publish_order_items, mhz_publish_orders, "
                    "mhz_synced_orders, mhz_config, notification_outbox RESTART IDENTITY CASCADE")
        conn.commit()
        yield conn
    finally:
        conn.close()


def make_order(conn, *, order_id: int, user_id: int, article_id: int,
               title: str, age_hours: float = 0.0, deducted=None):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO mhz_publish_orders
             (id, user_id, article_id, article_title, total_cost_points,
              actually_deducted_points, created_at)
           VALUES (%s,%s,%s,%s,0,%s, NOW() - make_interval(mins => %s))""",
        (order_id, user_id, article_id, title, deducted, int(age_hours * 60)),
    )
    conn.commit()
    return order_id


def make_item(conn, *, item_id: int, order_id: int, user_id: int, media_id: int,
              media_name: str, status: str, mhz_order_id=None,
              cost_points: int = 780, media_type: str = "mhz",
              routed_media_id=None):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO mhz_publish_order_items
             (id, order_id, user_id, media_id, media_name, status, mhz_order_id,
              cost_points, media_type, routed_media_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (item_id, order_id, user_id, media_id, media_name, status, mhz_order_id,
         cost_points, media_type, routed_media_id),
    )
    conn.commit()
    return item_id


def make_synced(conn, *, sid: str, order_sn: str, title: str,
                media_name: str = "", resource_id: int = 0, status: int = 2):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO mhz_synced_orders (id, order_sn, title, media_name, resource_id, status)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (sid, order_sn, title, media_name, resource_id, status),
    )
    conn.commit()


def item_row(conn, item_id: int) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM mhz_publish_order_items WHERE id = %s", (item_id,))
    return dict(cur.fetchone())
