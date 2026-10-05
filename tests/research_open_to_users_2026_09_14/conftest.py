"""WO_211 c1 · 自助调研放开给普通用户 —— 判据包夹具。

库私有(`geo_c14_211_test`),从**生产 schema** 建:
品牌归属校验(`require_brand_access`)读真表,手写夹具会漏掉列与约束,
于是判据能证出生产上不成立的事。
"""
import io
import os
import subprocess
import types
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

PROD_SCHEMA = Path("C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql")
PG_CONTAINER = "defgeo-c14-62-pg"
DB_NAME = "geo_c14_211_test"
ADMIN_DSN = "postgresql://geo_admin:testpw@localhost:55492/postgres"
TEST_DSN = "postgresql://geo_admin:testpw@localhost:55492/%s" % DB_NAME

#: 普通用户(agent_level=0)· 服务商(agent_level=1)· 管理员 · 别人
PLAIN_USER = 921101
AGENT_USER = 921102
OTHER_USER = 921103
PLAIN_BRAND = 921201     # 归 PLAIN_USER
OTHER_BRAND = 921202     # 归 OTHER_USER


def pytest_configure(config):
    os.environ.setdefault("TEST_DATABASE_URL", TEST_DSN)
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]


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


def _build():
    if not PROD_SCHEMA.exists():
        raise RuntimeError("生产 schema dump 不在: %s" % PROD_SCHEMA)
    _admin('CREATE DATABASE "%s"' % DB_NAME)
    sql = io.open(PROD_SCHEMA, encoding="utf-8").read()
    sql = "\n".join(l for l in sql.splitlines() if not l.startswith(chr(92)))
    proc = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-v", "ON_ERROR_STOP=1",
         "-U", "geo_admin", "-d", DB_NAME],
        input=sql.encode("utf-8"), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("恢复生产 schema 失败: %s"
                           % proc.stderr.decode("utf-8", "replace")[-1200:])


def _shape_ok():
    try:
        c = psycopg2.connect(TEST_DSN)
    except Exception:
        return False
    try:
        cur = c.cursor()   # 裸游标返元组,按**位置**取
        cur.execute("SELECT to_regclass('public.brands'),"
                    "       to_regclass('public.user_wallets'),"
                    "       to_regclass('public.geo_research_selfserve_queue')")
        row = cur.fetchone()
        return bool(row) and all(x is not None for x in row)
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
def seeded():
    """两个用户 + 两个品牌 + 钱包档位。清理**不吞异常**。

    🔴 `agent_level` 显式写:本单撤掉的正是读它的那道闸,
       夹具靠列默认值凑出来的 0 是偶然成立的 —— 换一版 schema 默认值一变,
       "普通用户"这个前提就没了,而判据会红在被测代码上。
    """
    c = conn()
    try:
        cur = c.cursor()
        cur.execute("DELETE FROM geo_research_selfserve_queue WHERE user_id IN (%s,%s,%s)",
                    (PLAIN_USER, AGENT_USER, OTHER_USER))
        cur.execute("DELETE FROM brands WHERE id IN (%s,%s)", (PLAIN_BRAND, OTHER_BRAND))
        cur.execute("DELETE FROM user_wallets WHERE user_id IN (%s,%s,%s)",
                    (PLAIN_USER, AGENT_USER, OTHER_USER))
        # 🔴 `user_wallets.user_id` 有外键指向 `users`(pg_constraint 实测,不是 grep)。
        #    users 的必填列:username / password_hash / display_name。
        cur.execute("DELETE FROM users WHERE id IN (%s,%s,%s)",
                    (PLAIN_USER, AGENT_USER, OTHER_USER))
        for uid, uname in ((PLAIN_USER, "plain211"), (AGENT_USER, "agent211"),
                           (OTHER_USER, "other211")):
            cur.execute(
                "INSERT INTO users (id, username, password_hash, display_name)"
                " VALUES (%s,%s,'x',%s)", (uid, uname, uname))
        cur.execute("INSERT INTO user_wallets (user_id, agent_level) VALUES (%s,0)",
                    (PLAIN_USER,))
        cur.execute("INSERT INTO user_wallets (user_id, agent_level) VALUES (%s,1)",
                    (AGENT_USER,))
        cur.execute("INSERT INTO user_wallets (user_id, agent_level) VALUES (%s,0)",
                    (OTHER_USER,))
        cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s)",
                    (PLAIN_BRAND, "普通用户的客户", PLAIN_USER))
        cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s)",
                    (OTHER_BRAND, "别人的客户", OTHER_USER))
    finally:
        c.close()
    yield


def fake_request(user_id=None, *, is_admin=False, client_brand_ids=None):
    """造一个只带登录态的 Request 替身。

    🔴 被测的那道闸读的就是 `request.state.user` —— 这里给什么它看到什么,
       所以这是**它真正的输入**,不是一个绕过去的旁路。
       `user_id=None` = 未登录(portal / 公开 token 的形状:没有登录态)。
    """
    state = types.SimpleNamespace()
    state.user = None if user_id is None else {
        "user_id": int(user_id), "is_admin": bool(is_admin),
        "client_brand_ids": list(client_brand_ids or []),
    }
    state.organization_identity = None
    return types.SimpleNamespace(state=state)
