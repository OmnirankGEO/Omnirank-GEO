# -*- coding: utf-8 -*-
"""WO_222-c1b 判据夹具:真库 + 真端点函数。

口径(WO_222 §9 · Owner 2026-09-15「做」):
    经营后台仍服务商专属;**其余全部放开**;普通账号登录默认落到与服务商相同的工作台。

🔴 本包的主判据都是**两臂对比**,不是「L0 能过」。
   「L0 能过」只证明我这一次没挡住他;**「L0 与服务商得到同一个结果」**才是
   Owner 那句话的判据。反过来经营后台那一臂要证明两者**不同** ——
   否则"全放开"这句话没有边界,我把经营后台也放开了照样全绿。

🔴 库:复用 222-c0 的私有库 `geo_c14_222_test`(schema 已齐)。
   不新建空库 —— 本仓 a-fresh-fixture-db-makes-import-time-migrations-deadlock-with-the-fixture:
   空库上 `import server` 的导入期迁移 DDL 会和夹具持开的事务互等锁,21 分钟零输出。
   代价是与 c0 包共库 ⇒ **两个包不能并跑**(harness 串行,见交付单 §5)。
"""
from __future__ import annotations

import os
import pathlib
import sys
import types

import psycopg2
import psycopg2.extras
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

L0_USER = 992251          # 普通账号 agent_level = 0
PROVIDER = 992252         # 服务商 agent_level = 1


def _dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if "test" not in dsn:
        pytest.skip("需要一个名字里带 test 的库(避免误打生产)", allow_module_level=True)
    return dsn


@pytest.fixture(scope="session")
def live_dsn() -> str:
    return _dsn()


@pytest.fixture(scope="session")
def mods(live_dsn):
    """落 DSN **再** import —— db.connection 在 import 期就读它。"""
    os.environ["DATABASE_URL"] = live_dsn
    os.environ["TEST_DATABASE_URL"] = live_dsn
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import db.connection as dbconn
    dbconn.DATABASE_URL = live_dsn
    dbconn._pool = None
    import server  # noqa: F401  (import 期 init_db() 把表建齐;不手抄精简 schema)

    import api.c_end_api as c_end
    import api.wallet_api as wallet
    import api.agent_workbench_api as workbench

    # 🔴 import 成功不代表拿到的是**这棵树**的模块
    #    (本仓 verified-correctly-against-the-wrong-referent)
    for mod, rel in ((c_end, "api/c_end_api.py"),
                     (wallet, "api/wallet_api.py"),
                     (workbench, "api/agent_workbench_api.py")):
        assert pathlib.Path(mod.__file__).resolve() == (REPO / rel).resolve(), (
            "import 到的不是本工作树的 %s:%s" % (rel, mod.__file__))
    return types.SimpleNamespace(c_end=c_end, wallet=wallet, workbench=workbench)


@pytest.fixture
def db(live_dsn):
    c = psycopg2.connect(live_dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c.autocommit = True
        yield c
    finally:
        c.close()


@pytest.fixture
def two_accounts(db):
    """一个普通账号 + 一个服务商,**除了 agent_level 之外完全一样**。

    🔴 两臂只允许差这一个变量。差第二个变量(角色、组织席位、余额……)的话,
       两臂结果不同时我就分不清是身份分流还是那个变量造成的
       —— 本仓 a-feature-both-groups-share-cannot-explain-their-difference 的反面。
    """
    with db.cursor() as cur:
        for uid, name in ((L0_USER, "wo222c1b_l0"), (PROVIDER, "wo222c1b_provider")):
            cur.execute(
                "INSERT INTO users (id, username, password_hash, display_name, is_active) "
                "VALUES (%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
                (uid, name, name))
        for uid, level in ((L0_USER, 0), (PROVIDER, 1)):
            cur.execute(
                "INSERT INTO user_wallets (user_id, agent_level) VALUES (%s,%s) "
                "ON CONFLICT (user_id) DO UPDATE SET agent_level = EXCLUDED.agent_level",
                (uid, level))
        # 夹具自证:两臂的 agent_level 真的不同。等号两边都是 0 的话,
        # 「两臂结果相同」就成了同义反复(本仓 a-reverse-control-with-a-known-false-red)。
        cur.execute("SELECT user_id, agent_level FROM user_wallets WHERE user_id IN (%s,%s)",
                    (L0_USER, PROVIDER))
        levels = {int(r["user_id"]): int(r["agent_level"] or 0) for r in cur.fetchall()}
    assert levels.get(L0_USER) == 0 and levels.get(PROVIDER) >= 1, (
        "夹具没造出真正的两臂:%r" % levels)
    yield types.SimpleNamespace(l0=L0_USER, provider=PROVIDER)
    with db.cursor() as cur:
        cur.execute("DELETE FROM user_action_logs WHERE user_id IN (%s,%s)", (L0_USER, PROVIDER))
        cur.execute("DELETE FROM user_settings WHERE user_id IN (%s,%s)", (L0_USER, PROVIDER))
        cur.execute("DELETE FROM user_wallets WHERE user_id IN (%s,%s)", (L0_USER, PROVIDER))
        cur.execute("DELETE FROM users WHERE id IN (%s,%s)", (L0_USER, PROVIDER))


def fake_request(user_id: int, *, is_admin: bool = False):
    """最小假 Request:这些端点只读 `request.state.user`。"""
    return types.SimpleNamespace(state=types.SimpleNamespace(
        user={"user_id": user_id, "is_admin": is_admin}))
