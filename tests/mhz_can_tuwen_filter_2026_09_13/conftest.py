"""#192 c1 · 账号列表 can_tuwen 过滤 —— 判据包夹具。

本包要真库:被测的是 `_short_video_where` 生成的 SQL 在真表上的行为。
库私有,建表从生产 schema 取那一张。
"""
import io
import os

_DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_192_test"


def pytest_configure(config):
    os.environ.setdefault("TEST_DATABASE_URL", _DSN)
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]


import subprocess
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

PROD_SCHEMA = Path("C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql")
PG_CONTAINER = "defgeo-c14-62-pg"
DB_NAME = "geo_c14_192_test"
ADMIN_DSN = "postgresql://geo_admin:testpw@localhost:55492/postgres"


def _admin(sql, args=None):
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    try:
        conn.cursor().execute(sql, args)
    finally:
        conn.close()


def _conn():
    conn = psycopg2.connect(_DSN)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    conn.autocommit = True
    return conn


def _db_exists():
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _build():
    _admin('CREATE DATABASE "%s"' % DB_NAME)
    sql = io.open(PROD_SCHEMA, encoding="utf-8").read()
    sql = "\n".join(l for l in sql.splitlines() if not l.startswith(chr(92)))
    proc = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-v", "ON_ERROR_STOP=0",
         "-U", "geo_admin", "-d", DB_NAME],
        input=sql.encode("utf-8"), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1500:])


def _shape_ok():
    """库**在**不等于库是我要的那个世界。

    上一次建库若炸在恢复 schema 之前,库壳会留下,`_db_exists()` 照样为真,
    于是判据红在 UndefinedTable —— 那看起来像被测对象坏了。
    """
    try:
        conn = psycopg2.connect(_DSN)
    except Exception:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.mhz_short_video') AS t")
        row = cur.fetchone()
        return (row["t"] if isinstance(row, dict) else row[0]) is not None
    finally:
        conn.close()


def pytest_sessionstart(session):
    if _db_exists() and not _shape_ok():
        _admin("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
               "WHERE datname=%s", (DB_NAME,))
        _admin('DROP DATABASE IF EXISTS "%s"' % DB_NAME)
    if not _db_exists():
        _build()


@pytest.fixture(autouse=True)
def clean_media():
    conn = _conn()
    try:
        conn.cursor().execute("DELETE FROM mhz_short_video")
    finally:
        conn.close()
    yield


def seed_media(media_id, *, can_tuwen, name=None, status=1, is_active=True):
    """种一条媒体。

    🔴 `is_active` **显式写**,不吃列默认值:`_short_video_where` 的基础谓词是
       `is_active = TRUE`(db/meijiehezi_db.py:1560)——那是被测谓词的**前提**。
       靠默认值满足前提的夹具是偶然成立的:换一个库、换一版 schema,
       默认值一变就整包全红,而那种红看起来像被测代码坏了。
    """
    conn = _conn()
    try:
        conn.cursor().execute(
            """INSERT INTO mhz_short_video (id, media_name, platform, price,
                   can_tuwen, status, fans_num, is_active)
               VALUES (%s,%s,'douyin',100,%s,%s,1000,%s)""",
            (int(media_id), name or ("号%d" % media_id), int(can_tuwen),
             int(status), bool(is_active)))
    finally:
        conn.close()


@pytest.fixture(autouse=True, scope="session")
def _fixture_can_actually_be_seen():
    """夹具自检:种一条、用**被测的那条查询路径**读回来。

    读不回来就**当场炸并说清楚**,而不是让每条判据各自红在自己的断言上 ——
    「库在但夹具不可见」与「被测代码坏了」在读数上完全同形
    (Review 09-13 在另一个库上跑本包就是 3/3 全红,第一反应是代码坏了)。
    """
    from db.meijiehezi_db import list_short_video

    probe_id = 987654321
    conn = _conn()
    try:
        conn.cursor().execute("DELETE FROM mhz_short_video WHERE id=%s", (probe_id,))
    finally:
        conn.close()
    seed_media(probe_id, can_tuwen=1, name="夹具自检")
    try:
        out = list_short_video(1, 50, can_tuwen=1)
        hit = [m for m in out["media"] if int(m["id"]) == probe_id]
        assert hit, (
            "夹具种进去的行**读不回来** —— 不是被测代码的问题。"
            "常见两因:① 库是空壳/schema 不对;② 有别的进程在同一个库上跑本包"
            "(本包库名 %s,请各自用私有库名)。" % DB_NAME)
    finally:
        conn = _conn()
        try:
            conn.cursor().execute("DELETE FROM mhz_short_video WHERE id=%s", (probe_id,))
        finally:
            conn.close()
    yield
