"""WO_220-c2 · 写作大厅基础资料表落库 —— 判据包夹具。

库私有(`geo_c14_220_test`),从**生产 schema** 建表再叠本单迁移:
手写夹具会漏掉 CHECK / UNIQUE / NOT NULL / DEFAULT,于是判据能证出生产上
不可能的事。本包尤其要用真 schema —— `client_profiles` 有 80 列,
其中 `products` / `success_cases` 的存在正是本单不复用它们的理由。

🔴 库名锁死在本包上:统一导出 `TEST_DATABASE_URL` 会把它顶掉
   (`setdefault` 在已导出时不生效),整包连错库只会红得看不懂。
   实测过一次:跨包判别实验里我统一覆盖,整包 27 errors。
"""
import io
import os
import subprocess
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DB_NAME = "geo_c14_220_test"
_DSN = "postgresql://geo_admin:testpw@localhost:55492/%s" % DB_NAME
ADMIN_DSN = "postgresql://geo_admin:testpw@localhost:55492/postgres"
PG_CONTAINER = "defgeo-c14-62-pg"
PROD_SCHEMA = Path("C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql")
REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "db" / "migration_062_client_profile_basic_info_fields_2026_09_16.sql"


def pytest_configure(config):
    got = os.environ.get("TEST_DATABASE_URL")
    if got and DB_NAME not in got:
        raise RuntimeError(
            "writing_basics 判据锁死在本包一次性库上:库名必须含 '%s';"
            "实得 '%s'。单跑用 %s" % (DB_NAME, got, _DSN))
    os.environ.setdefault("TEST_DATABASE_URL", _DSN)
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]


def _admin(sql, args=None):
    c = psycopg2.connect(ADMIN_DSN)
    c.autocommit = True
    try:
        c.cursor().execute(sql, args)
    finally:
        c.close()


def conn():
    c = psycopg2.connect(_DSN)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    c.autocommit = True
    return c


def _db_exists():
    c = psycopg2.connect(ADMIN_DSN)
    c.autocommit = True
    try:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        return cur.fetchone() is not None
    finally:
        c.close()


def _psql(sql_text):
    proc = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-v", "ON_ERROR_STOP=0",
         "-U", "geo_admin", "-d", DB_NAME],
        input=sql_text.encode("utf-8"), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1500:])


def _build():
    _admin('CREATE DATABASE "%s"' % DB_NAME)
    sql = io.open(PROD_SCHEMA, encoding="utf-8").read()
    sql = "\n".join(l for l in sql.splitlines() if not l.startswith(chr(92)))
    _psql(sql)
    # 🔴 迁移**从文件读**,不在这里手抄一份 DDL。
    #    抄一份就是这张表的第五套 schema 定义,迟早与迁移对不上而不报错。
    if not MIGRATION.exists():
        raise RuntimeError("迁移文件不在:%s —— 拒建库" % MIGRATION)
    _psql(io.open(MIGRATION, encoding="utf-8").read())


def _shape_ok():
    """库**在**不等于库是我要的那个世界。

    上一次建库若炸在恢复 schema 之前,库壳会留下、`_db_exists()` 照样为真,
    判据就红在 UndefinedTable —— 那看起来像被测对象坏了。
    这里连**本单那一列**一起验:少了它说明迁移那一步没跑成。
    """
    try:
        c = psycopg2.connect(_DSN)
    except Exception:
        return False
    try:
        # 🔴 按**位置**取,不按键名:这里是普通游标(不是 RealDictCursor),
        #    `dict(row)` 会拿元组去当键值对,报 "sequence element #0 has length 15"。
        #    第一次跑库不存在 ⇒ `_db_exists()` 短路,这个函数根本没被调用 ⇒ 18 条全绿;
        #    第二次跑才炸。**一次绿不算绿**,判据包交出去前必须连跑两次。
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT to_regclass('public.client_profiles') AS t,
                   (SELECT 1 FROM information_schema.columns
                     WHERE table_name='client_profiles'
                       AND column_name='basic_info_fields') AS c
        """)
        row = cur.fetchone()
        return row["t"] is not None and row["c"] is not None
    finally:
        c.close()


def pytest_sessionstart(session):
    if _db_exists() and not _shape_ok():
        _admin("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
               (DB_NAME,))
        _admin('DROP DATABASE IF EXISTS "%s"' % DB_NAME)
    if not _db_exists():
        _build()


#: 本包自己的档案 id 前缀,清理只认它 —— 别人的行一律不碰。
PID_PREFIX = "wb220_"


@pytest.fixture(autouse=True)
def clean_rows():
    """每条判据自己的世界。清理**不吞异常**:吞掉的话下一条会读到上一条的残留,
    而那种串味只表现为「偶尔红一条」,最难查。"""
    c = conn()
    try:
        c.cursor().execute("DELETE FROM client_profiles WHERE id LIKE %s", (PID_PREFIX + "%",))
    finally:
        c.close()
    yield
    c = conn()
    try:
        c.cursor().execute("DELETE FROM client_profiles WHERE id LIKE %s", (PID_PREFIX + "%",))
    finally:
        c.close()
