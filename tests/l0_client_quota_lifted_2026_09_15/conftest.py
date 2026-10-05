# -*- coding: utf-8 -*-
"""WO_222-c0 判据夹具:真库 + 真函数(`api.brand_api.add_client` / `api.profile_api.api_create_profile`)。

🔴 为什么不测「源码里还有没有 402 那段字符串」:那是**串锚**。
   本单改的是「走不走那一支」,而串锚在**支还在但没人走**和**支已经删了**两种情况下
   读数一样。所以主判据让真函数打真库,建第 2、3 个客户,读回真行。
   字面量腿只作**补充**(证明那两处不会再发 UPGRADE_REQUIRED),不单独当证据。
"""
from __future__ import annotations

import os
import pathlib
import sys

import psycopg2
import psycopg2.extras
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 🔴 `client_profiles` 的 DDL **引用**既有那份,不另抄一份。
#:   本仓 `feedback_one_predicate_one_place_or_half_goes_unverified`:
#:   同一份表定义抄两处,迟早有一处跟不上生产,而跟不上的那处判据照样绿。
_CLIENT_PROFILES_DDL = (
    REPO / "tests" / "brand_test_visibility_2026_09_05" / "fixtures"
    / "client_profiles_from_prod_dump.sql"
)


def _dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if "test" not in dsn:
        pytest.skip("需要一个名字里带 test 的库(避免误打生产)", allow_module_level=True)
    return dsn


@pytest.fixture(scope="session")
def live_dsn() -> str:
    return _dsn()


@pytest.fixture(scope="session")
def apis(live_dsn):
    """把 DSN 落进环境**再** import —— db.connection 在 import 期就读它。"""
    os.environ["DATABASE_URL"] = live_dsn
    os.environ["TEST_DATABASE_URL"] = live_dsn
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import db.connection as dbconn
    dbconn.DATABASE_URL = live_dsn
    dbconn._pool = None
    # 🔴 先 import server:它在 import 期跑 init_db() 把表建齐。不手写精简 schema ——
    #    手写就是第二套表定义,列一漏 CHECK 一少,判据全绿而生产照样炸。
    import server  # noqa: F401

    assert _CLIENT_PROFILES_DDL.exists(), (
        "缺 client_profiles 的 DDL 夹具:%s —— 它由 brand_test_visibility 包提供,"
        "本包**引用**而非复制" % _CLIENT_PROFILES_DDL)
    _c = psycopg2.connect(live_dsn)
    try:
        _c.autocommit = True
        with _c.cursor() as cur:
            cur.execute(_CLIENT_PROFILES_DDL.read_text(encoding="utf-8"))
            cur.execute("SELECT to_regclass('public.client_profiles') IS NOT NULL")
            assert cur.fetchone()[0], "夹具表没建成 —— 后面每一条读数都不可解读"
    finally:
        _c.close()

    import api.brand_api as brand_mod
    import api.profile_api as profile_mod
    # 🔴 import 成功不代表拿到的是**这棵树**的模块(本仓 verified-correctly-against-the-wrong-referent)
    for mod, rel in ((brand_mod, "api/brand_api.py"), (profile_mod, "api/profile_api.py")):
        assert pathlib.Path(mod.__file__).resolve() == (REPO / rel).resolve(), (
            "import 到的不是本工作树的 %s:%s" % (rel, mod.__file__))
    return brand_mod, profile_mod


@pytest.fixture
def db(live_dsn):
    c = psycopg2.connect(live_dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c.autocommit = True
        yield c
    finally:
        c.close()
