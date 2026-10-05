"""WO_204 c1 · 图文选题表 —— 判据包夹具。

库私有(`geo_c14_204b_test`),**从生产 schema dump 建,再套上本单的 059**。

🔴 为什么不手写 CREATE TABLE:手写会漏掉 CHECK / partial 唯一索引,
   而本包 C1 钉的正是那把 partial 唯一索引的幂等、C3/C4 钉的是状态机 CHECK。
   漏了约束,判据就在一个生产上不成立的世界里全绿。
🔴 顺带:conftest 应用 059 = 这条迁移每跑一次判据就在真 PG 上被执行一次。

🔴 库名带 b:`geo_c14_204_test` 是我手工冒烟用过的库(有残留、且没有生产 schema)。
   拿跑过的库当模板会把残留一起带走 —— Review 09-13 就这么撞出过 6 条假红。
"""
import io
import os
import subprocess
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

PROD_SCHEMA = Path("C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql")
MIGRATION = Path("db/migration_059_geo_douyin_topics_2026_09_13.sql")
PG_CONTAINER = "defgeo-c14-62-pg"
DB_NAME = "geo_c14_204b_test"
ADMIN_DSN = "postgresql://geo_admin:testpw@localhost:55492/postgres"
TEST_DSN = "postgresql://geo_admin:testpw@localhost:55492/%s" % DB_NAME

BRAND_ID = 920401
USER_ID = 920402
OTHER_BRAND_ID = 920403


def pytest_configure(config):
    os.environ.setdefault("TEST_DATABASE_URL", TEST_DSN)
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    # 🔴 [c1b] 图文管线有一道**总闸** `GEO_DOUYIN_PIPELINE_ENABLED`(默认关):
    #    关着时每个端点第一行就 `return _COMING_SOON`,什么都不做。
    #    原来那 42 条判据全是直接调数据层的,所以从没碰到这道闸 ——
    #    一开始驱动端点就整排 `coming_soon`。生产上它是开的(图文线在跑),
    #    所以这里也开,判据才在**同一个世界**里。
    #    闸本身另有一条判据钉(关着时不碰库),不是靠这里默默打开就算数。
    os.environ.setdefault("GEO_DOUYIN_PIPELINE_ENABLED", "1")


def conn():
    c = psycopg2.connect(TEST_DSN)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    c.autocommit = True
    return c


def _admin(sql, args=None):
    c = psycopg2.connect(ADMIN_DSN)
    c.autocommit = True
    try:
        c.cursor().execute(sql, args)
    finally:
        c.close()


def _db_exists():
    c = psycopg2.connect(ADMIN_DSN)
    c.autocommit = True
    try:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        return cur.fetchone() is not None
    finally:
        c.close()


def _psql_file(text):
    proc = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-v", "ON_ERROR_STOP=1",
         "-U", "geo_admin", "-d", DB_NAME],
        input=text.encode("utf-8"), capture_output=True)
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def _build():
    if not PROD_SCHEMA.exists():
        raise RuntimeError("生产 schema dump 不在: %s" % PROD_SCHEMA)
    _admin('CREATE DATABASE "%s"' % DB_NAME)
    sql = io.open(PROD_SCHEMA, encoding="utf-8").read()
    # psql 元命令行(反斜杠开头)psycopg2 不认,docker exec psql 也不需要
    sql = "\n".join(l for l in sql.splitlines() if not l.startswith(chr(92)))
    rc, err = _psql_file(sql)
    if rc != 0:
        raise RuntimeError("恢复生产 schema 失败: %s" % err[-1200:])
    # 🔴 本单的迁移**在这里真跑**。它失败就整包炸在 sessionstart,
    #    而不是让每条判据各自红在 UndefinedTable 上(那看起来像被测代码坏了)。
    rc, err = _psql_file(io.open(MIGRATION, encoding="utf-8").read())
    if rc != 0:
        raise RuntimeError("migration_059 在生产 schema 上跑不过: %s" % err[-1200:])


def _shape_ok():
    """库**在**不等于库是我要的那个世界。"""
    try:
        c = psycopg2.connect(TEST_DSN)
    except Exception:
        return False
    try:
        cur = c.cursor()   # 裸游标 -> 元组,按**位置**取(别按名:那是 RealDictCursor 才有的)
        cur.execute("SELECT to_regclass('public.geo_douyin_topics'),"
                    "       to_regclass('public.geo_douyin_posts')")
        row = cur.fetchone()
        return bool(row) and row[0] is not None and row[1] is not None
    finally:
        c.close()


def pytest_sessionstart(session):
    if _db_exists() and not _shape_ok():
        _admin("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
               (DB_NAME,))
        _admin('DROP DATABASE IF EXISTS "%s"' % DB_NAME)
    if not _db_exists():
        _build()


@pytest.fixture(autouse=True)
def clean_rows():
    """每条判据自己的世界。

    🔴 清理**不吞异常**:吞掉的话残留会串到下一条,而串味只表现为
       "偶尔红一条",最难查。
    🔴 顺序按外键来:`geo_douyin_posts` 被 revisions / tasks / artifacts /
       **本单的 topics** 四张表引用,先删子再删它。
       (这一族确实有外键 —— 我一开始 grep dump 得到"没有外键"的空结果,
        是因为 pg_dump 把 ALTER 与 ADD CONSTRAINT 写成两行。)
    """
    c = conn()
    try:
        cur = c.cursor()
        scope = "(SELECT id FROM geo_douyin_posts WHERE brand_id IN (%s,%s))"
        cur.execute("DELETE FROM geo_douyin_topics WHERE brand_id IN (%s,%s)",
                    (BRAND_ID, OTHER_BRAND_ID))
        for child, col in (("geo_douyin_post_revisions", "geo_post_id"),
                           ("geo_douyin_post_tasks", "post_id"),
                           ("geo_douyin_publish_artifacts", "geo_post_id")):
            cur.execute("DELETE FROM %s WHERE %s IN %s" % (child, col, scope),
                        (BRAND_ID, OTHER_BRAND_ID))
        cur.execute("DELETE FROM geo_douyin_posts WHERE brand_id IN (%s,%s)",
                    (BRAND_ID, OTHER_BRAND_ID))
        cur.execute("DELETE FROM brands WHERE id IN (%s,%s)",
                    (BRAND_ID, OTHER_BRAND_ID))
    finally:
        c.close()
    yield


def make_post(*, brand_id=BRAND_ID, status="generating", title="建行时的占位标题"):
    """种一条真作品。

    🔴 必须是**真行**:`geo_douyin_topics.post_id` 有外键指向它,
       编一个 777001 会被数据库拒 —— 而那正是这条外键存在的理由。
    """
    c = conn()
    try:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO geo_douyin_posts
                   (brand_id, created_by, industry_key, city, keyword, content_type,
                    title, body_text, hashtags, cards, oss_keys, cover_oss_key,
                    status, style_key, contact_enabled, aspect_ratio, generation_meta)
               VALUES (%s,%s,'zhuangxiu','杭州','装修公司','cards',
                       %s,'', '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '',
                       %s,'style_a', true, '3:4', '{}'::jsonb)
               RETURNING id""",
            (brand_id, USER_ID, title, status))
        return int(cur.fetchone()["id"])
    finally:
        c.close()


def fake_request(user_id=USER_ID, *, is_admin=False, client_brand_ids=None):
    """造一个只带登录态的 Request 替身(端点鉴权读的就是 `request.state.user`)。

    🔴 [c1b] 加它是因为 Review 的毒 P6 证明:原来 42 条判据**没有一条**
       拿别人的 topic 去调 PATCH/DELETE —— 把 `_topic_or_404` 里的
       `require_brand_access` 删掉,42 条照样全绿。锁钉住了 helper,
       调用点裸奔。
    """
    import types

    state = types.SimpleNamespace()
    state.user = None if user_id is None else {
        "user_id": int(user_id), "is_admin": bool(is_admin),
        "client_brand_ids": list(client_brand_ids or []),
    }
    state.organization_identity = None
    return types.SimpleNamespace(state=state)


def seed_brand(brand_id, owner_user_id):
    """种一个品牌(`require_brand_access` 的第 2 条路要读 `brands.owner_user_id`)。"""
    c = conn()
    try:
        c.cursor().execute(
            "INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s)"
            " ON CONFLICT (id) DO UPDATE SET owner_user_id = EXCLUDED.owner_user_id",
            (int(brand_id), "品牌%d" % brand_id, int(owner_user_id)))
    finally:
        c.close()


def seed_topic(*, title="原选题", status="pending", source="distilled",
               brand_id=BRAND_ID, task_id=None, index=None, post_id=None,
               keyword="装修公司", city="杭州", angle="", outline=None):
    import json as _json
    c = conn()
    try:
        cur = c.cursor()
        cur.execute(
            """INSERT INTO geo_douyin_topics
                   (brand_id, keyword, city, title, angle, card_outline,
                    source, status, post_id, distill_task_id, distill_index, created_by)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (brand_id, keyword, city, title, angle,
             _json.dumps(outline or [], ensure_ascii=False),
             source, status, post_id, task_id, index, USER_ID))
        return int(cur.fetchone()["id"])
    finally:
        c.close()


def topic_row(topic_id):
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT * FROM geo_douyin_topics WHERE id=%s", (int(topic_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        c.close()
