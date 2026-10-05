"""#67 判据:真库 + 真函数(`api.brand_api.list_my_clients`),不手写第二份 SQL。

🔴 为什么不测「拼出来的 SQL 串里有没有 is_test」:那是**串锚**,
   证得了谓词在,证不了它接到了结果上。本单改的正是「哪一支用哪个 clause」,
   接错支时串锚照样绿 —— 必须让真 SQL 打真库,读回真行。
"""
from __future__ import annotations

import os
import pathlib
import sys

import psycopg2
import psycopg2.extras
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if "test" not in dsn:
        pytest.skip("需要一个名字里带 test 的库(避免误打生产)", allow_module_level=True)
    return dsn


@pytest.fixture(scope="session")
def live_dsn() -> str:
    return _dsn()


@pytest.fixture(scope="session")
def brand_api(live_dsn):
    """把 DSN 落进环境**再** import —— db.connection 在 import 期就读它。"""
    os.environ["DATABASE_URL"] = live_dsn
    os.environ["TEST_DATABASE_URL"] = live_dsn
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import db.connection as dbconn
    dbconn.DATABASE_URL = live_dsn
    dbconn._pool = None
    # 🔴 先 import server:它在 import 期跑 init_db(),把表建齐(client_profiles 等)。
    #    **不手写精简 schema** —— 手写就是第二套表定义,列一漏 CHECK 一少,
    #    判据全绿而生产照样炸(本仓 2026-08-09 记过)。schema 只能由真代码产出。
    import server  # noqa: F401

    # 🔴 `client_profiles` 不在 init_db() 的范围内(它是社媒域的表)。
    #    用**生产 dump 抽出来的那份 DDL**补上,不手写精简表 —— 见同目录 .sql 抬头。
    _ddl = (pathlib.Path(__file__).parent / "fixtures" / "client_profiles_from_prod_dump.sql").read_text(encoding="utf-8")
    _c = psycopg2.connect(live_dsn)
    try:
        _c.autocommit = True
        with _c.cursor() as cur:
            cur.execute(_ddl)
            cur.execute("SELECT to_regclass('public.client_profiles') IS NOT NULL")
            assert cur.fetchone()[0], "夹具表没建成 —— 后面每一条『看不见』都不可解读"
    finally:
        _c.close()

    import api.brand_api as mod
    # 配对的必须不命中:import 成功不代表拿到的是**这棵树**的模块。
    assert pathlib.Path(mod.__file__).resolve() == (REPO / "api" / "brand_api.py").resolve(), (
        "import 到的不是本工作树的 brand_api:%s" % mod.__file__)
    return mod


@pytest.fixture
def db(live_dsn):
    c = psycopg2.connect(live_dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield c
    finally:
        c.close()
