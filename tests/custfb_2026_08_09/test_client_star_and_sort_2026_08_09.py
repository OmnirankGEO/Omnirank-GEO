"""客户反馈③ · 星标置顶 + 排序换口径 —— 真 PostgreSQL 行为锁。

判据全部打在**真跑一遍 `api.brand_api.list_my_clients` / `set_client_star`** 上:
真库、真 SQL、真迁移(migration_030 逐字执行),不做源码字符串断言。

  锁1  迁移 030 真能把两列加上(幂等,连跑两次)
  锁2  排序:星标恒在最前
  锁3  排序:未星标之间按"最近服务活跃"(最近报价/文章 created_at),**不看 updated_at**
       —— 反向对照:把 updated_at 顶到最新的那一行**不会**因此上浮
  锁4  刚建档、零服务动作的新客户不会被沉底(GREATEST 里带 b.created_at 的理由)
  锁5  星标端点:改的就是被授权的那一行,返回值与库内一致
  锁6  RBAC:非 owner 拿不到别人的品牌 → 404,且**库里那一行没被改**
       (核验 target = 写 target:光看 404 不够,要证明它真的没写)
  锁7  admin 可以跨 owner 操作(放宽写在同一条 UPDATE 里,不另开路径)
  锁8  取消星标把 starred_at 置回 NULL

环境:TEST_DATABASE_URL 指向一次性 loopback 测试库(与仓内既有 PG 矩阵同纪律)。
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
MIGRATION_030 = ROOT / "db" / "migration_030_brand_star_2026_08_09.sql"

# 只建 list_my_clients / set_client_star 这两条 SQL 真正碰到的表与列。
BASE_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY, username TEXT, display_name TEXT
);
-- brands: 见 db() 夹具里的 ensure_brands_schema()（生产 SSOT 出口），不在这里手搓。
CREATE TABLE IF NOT EXISTS quotes (
    id SERIAL PRIMARY KEY, brand_id INTEGER, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS articles (
    id SERIAL PRIMARY KEY, brand_id INTEGER, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS diagnosis_records (
    id SERIAL PRIMARY KEY, brand_id INTEGER, total_score INTEGER,
    result_visibility TEXT, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS client_profiles (
    id TEXT PRIMARY KEY, brand_id INTEGER, is_deleted INTEGER DEFAULT 0
);
-- migration_030 也会给它加 order_remark;这里只需要表在。
CREATE TABLE IF NOT EXISTS mhz_publish_orders (
    id SERIAL PRIMARY KEY, user_id INTEGER
);
"""

OWNER_ID = 7001
OTHER_ID = 7002


def _skip_unless_throwaway_pg():
    if not PG_URL:
        pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)")
    parsed = urlsplit(PG_URL)
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        pytest.skip(f"只允许 loopback 一次性容器 DSN,拒绝疑似生产 host:{parsed.hostname}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"基础库名必须含 test 且不含 prod:{base_db}")


@pytest.fixture(scope="module")
def db():
    """一次性 throwaway 库 + 建表 + 跑两遍 migration_030(顺带证明幂等)。"""
    _skip_unless_throwaway_pg()
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"custfb_star_test_{uuid.uuid4().hex[:10]}"
    assert "test" in name and "prod" not in name
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        # [R5 ⑤ 批2 2026-08-21] brands 走生产 SSOT 出口（手搓版比生产窄）。只换 brands 这一张表的 DDL 来源，
        #   本文件其余业务表一概不动 —— 批 1 实测证伪过「夹具改跑整个 init_db」：
        #   把 150 张表拖进只要十来张表的夹具，174 passed/0 failed 变 138 passed/34 failed。
        #   手搓版 12 列（含 is_test/brand_type 这些真实列），生产 32 列。
        ensure_brands_schema(cur)
        cur.execute(BASE_DDL)
        forward = MIGRATION_030.read_text(encoding="utf-8")
        cur.execute(forward)
        cur.execute(forward)  # 锁1:连跑两次不炸 = 幂等
    try:
        yield type("DB", (), {
            "url": url,
            "connect": staticmethod(
                lambda: psycopg2.connect(url, cursor_factory=RealDictCursor)),
            "conn": conn,
        })
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.close()


def test_migration_adds_columns(db):
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='brands' AND column_name IN ('is_starred','starred_at')")
        cols = {r["column_name"] for r in cur.fetchall()}
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='mhz_publish_orders' AND column_name='order_remark'")
        remark = cur.fetchall()
    assert cols == {"is_starred", "starred_at"}
    assert len(remark) == 1, "migration_030 的 ② 那半没生效(确认后重发拿不回备注)"


# ────────────────────────────────────────────────────────────────────────
# 真调用 endpoint(不经 ASGI,但走真代码 + 真库)
# ────────────────────────────────────────────────────────────────────────
class _FakeState:
    def __init__(self, user):
        self.user = user
        self.organization_identity = None
        self.organization_brand_ids = []


class _FakeRequest:
    def __init__(self, user):
        self.state = _FakeState(user)
        self.client = None


def _patch_conn(monkeypatch, db):
    import db.connection as conn_mod

    monkeypatch.setattr(conn_mod, "get_connection", db.connect)


def _seed(db):
    """三个客户,故意让 updated_at 与"最近服务活跃"给出**相反**的顺序。"""
    with db.conn.cursor() as cur:
        cur.execute("DELETE FROM articles"); cur.execute("DELETE FROM quotes")
        cur.execute("DELETE FROM brands"); cur.execute("DELETE FROM users")
        cur.execute("INSERT INTO users (id, username) VALUES (%s,'owner'), (%s,'other')",
                    (OWNER_ID, OTHER_ID))
        cur.execute(
            """
            INSERT INTO brands (id, name, brand_type, owner_user_id, created_at, updated_at)
            VALUES
              (1, '被批量顶起来的老客户', 'client', %s, '2026-01-01', '2026-08-08'),
              (2, '真正在服务的客户',     'client', %s, '2026-01-02', '2026-01-02'),
              (3, '今天刚建档的新客户',   'client', %s, '2026-08-09', '2026-08-09')
            """,
            (OWNER_ID, OWNER_ID, OWNER_ID),
        )
        # 只有 2 号有真实服务动作(一张报价 + 一篇文章)
        cur.execute("INSERT INTO quotes (brand_id, created_at) VALUES (2, '2026-08-05')")
        cur.execute("INSERT INTO articles (brand_id, created_at) VALUES (2, '2026-08-06')")


def _list_ids(db, monkeypatch, user=None):
    _patch_conn(monkeypatch, db)
    from api.brand_api import list_my_clients

    req = _FakeRequest(user or {"user_id": OWNER_ID, "is_admin": False})
    res = asyncio.run(list_my_clients(req))
    return [c["id"] for c in res["clients"]]


def test_sort_by_real_service_activity_not_updated_at(db, monkeypatch):
    """锁3 + 锁4:2 号(有服务动作)与 3 号(今天建档)必须压过 1 号。

    🔴 反向对照就长在夹具里:1 号的 `updated_at` 是三者中**最新的**(2026-08-08)。
      旧口径 `ORDER BY updated_at DESC` 会把它排第一 —— 它排第一就说明没改成。
    """
    _seed(db)
    ids = _list_ids(db, monkeypatch)
    assert ids[0] in (2, 3) and ids[1] in (2, 3), f"实际次序 {ids}"
    assert ids[-1] == 1, f"updated_at 最新的空壳客户仍排在最前 → 排序没换口径:{ids}"


def test_starred_goes_first(db, monkeypatch):
    """锁2:给最"不活跃"的 1 号打星标,它必须立刻跳到第一。"""
    _seed(db)
    with db.conn.cursor() as cur:
        cur.execute("UPDATE brands SET is_starred=TRUE, starred_at=NOW() WHERE id=1")
    ids = _list_ids(db, monkeypatch)
    assert ids[0] == 1, f"星标没置顶:{ids}"
    with db.conn.cursor() as cur:  # 复原,别污染后面的用例
        cur.execute("UPDATE brands SET is_starred=FALSE, starred_at=NULL")


def test_star_endpoint_writes_the_authorized_row(db, monkeypatch):
    """锁5 + 锁8:标上 → 库里真是 TRUE 且有 starred_at;取消 → 回 FALSE 且 starred_at 为 NULL。"""
    _seed(db)
    _patch_conn(monkeypatch, db)
    from api.brand_api import ClientStarRequest, set_client_star

    req = _FakeRequest({"user_id": OWNER_ID, "is_admin": False})
    got = asyncio.run(set_client_star(2, ClientStarRequest(starred=True), req))
    assert got["is_starred"] is True and got["starred_at"]
    with db.conn.cursor() as cur:
        cur.execute("SELECT is_starred, starred_at FROM brands WHERE id=2")
        row = cur.fetchone()
    assert row["is_starred"] is True and row["starred_at"] is not None

    got_off = asyncio.run(set_client_star(2, ClientStarRequest(starred=False), req))
    assert got_off["is_starred"] is False and got_off["starred_at"] is None
    with db.conn.cursor() as cur:
        cur.execute("SELECT is_starred, starred_at FROM brands WHERE id=2")
        row = cur.fetchone()
    assert row["is_starred"] is False and row["starred_at"] is None


def test_star_endpoint_rbac_other_owner_cannot_write(db, monkeypatch):
    """锁6:别人的品牌 → 404,而且**库里那一行一个字没动**。

    🔴 只断 404 是不够的:一个"先写后判"的实现照样能返 404 而数据已经改了。
      所以这里读回来看真值 —— 核验 target = 写 target 的实测形态。
    """
    _seed(db)
    _patch_conn(monkeypatch, db)
    from fastapi import HTTPException

    from api.brand_api import ClientStarRequest, set_client_star

    req = _FakeRequest({"user_id": OTHER_ID, "is_admin": False})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(set_client_star(2, ClientStarRequest(starred=True), req))
    assert exc.value.status_code == 404
    with db.conn.cursor() as cur:
        cur.execute("SELECT is_starred FROM brands WHERE id=2")
        assert cur.fetchone()["is_starred"] is False, "RBAC 说不行,数据却被改了"


def test_order_remark_round_trip(db, monkeypatch):
    """② 备注落库 → 回读:确认后重投那一次必须还拿得到当初填的地区备注。

    🔴 反向对照两条:没落过备注的订单回读必须是空串(不是 None、不是异常);
      落库函数对空备注**不写**(空备注不该把已有值抹掉)。
    """
    _patch_conn(monkeypatch, db)
    from api.meijiehezi_api import _persist_order_remark, _read_order_remark

    with db.conn.cursor() as cur:
        cur.execute("DELETE FROM mhz_publish_orders")
        cur.execute("INSERT INTO mhz_publish_orders (id, user_id) VALUES (11, 1), (12, 1)")

    assert _read_order_remark(11) == "", "没落过备注的订单必须回读空串"
    _persist_order_remark(11, "发广东省")
    assert _read_order_remark(11) == "发广东省"
    _persist_order_remark(11, "")          # 空备注不该抹掉已有值
    assert _read_order_remark(11) == "发广东省"
    assert _read_order_remark(12) == "", "串号了:别的订单不该被写到"


def test_star_endpoint_admin_can_cross_owner(db, monkeypatch):
    """锁7:admin 放宽必须真的生效(否则上一条锁可能只是恒 404,零判别力)。"""
    _seed(db)
    _patch_conn(monkeypatch, db)
    from api.brand_api import ClientStarRequest, set_client_star

    req = _FakeRequest({"user_id": OTHER_ID, "is_admin": True})
    got = asyncio.run(set_client_star(2, ClientStarRequest(starred=True), req))
    assert got["is_starred"] is True
    with db.conn.cursor() as cur:
        cur.execute("UPDATE brands SET is_starred=FALSE, starred_at=NULL")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
