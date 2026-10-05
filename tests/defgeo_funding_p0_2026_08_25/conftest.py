"""诊断资金链四修(WO_CODEX_P0_DIAG_FUNDING A-1/A-2/A-3/A-4)的判据地基。

这个包要同时打两层,所以底座必须**同时**满足两件事:

  ① **真 HTTP × 真 PG** 打 defgeo confirm 端点(A-1/A-2/A-4 都长在 confirm 那一刀上);
  ② **真的把 server.py import 进来并驱动它**(A-3 的 org / 非 org 两条臂只在
     ``server.run_diagnosis_task`` 跑起来的时候才存在 —— P0-3b 的教训是
     「判据目录零条在运行时执行 server.py」,于是 92 条全绿、上线每单必挂)。

库怎么来
--------
生产 pg_dump(在仓里、跟着 commit 走)+ ``import server``。
后者在 ROLE 未设的本地形态下会**重放 db/migration_manifest.MIGRATIONS 全量**
(server.py:636 那一支),于是 040~050 全部装上 —— 迁移清单是真相源,
这里**不手抄第二份清单**(手抄漏掉的那一条不会让任何判据变红)。

装完立刻做**配对的必须不命中**:defgeo 表与 ``diagnosis_runs.payer_user_id``
必须真的在。它们不在时,本包所有"零副作用""落列了吗"的判据都会以
UndefinedTable/None 的形式给出**与被测代码无关的红或绿**,那种绿最贵。

🔴 库是 throwaway:PG16 容器 ``defgeo-p0fix-throwaway-pg``(端口 55437),
   库名强制同时含 ``defgeo`` 与 ``test``,用完 DROP。
   安全栓不是洁癖:2026-08-15 实测过库名不含 ``test`` 会让整套安全栓失效。
"""
from __future__ import annotations

import os
import pathlib
import sys
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

#: 生产 schema 快照(在仓里 · 不是手写的精简 schema)。
#: 手写一份精简 schema 就是第二套表定义:列一漏、类型一错,判据全绿而生产照样炸。
PROD_SCHEMA = pathlib.Path(os.getenv(
    "DEFGEO_P0FIX_PROD_SCHEMA_SQL",
    str(REPO / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql")))

ADMIN_DSN = os.getenv(
    "DEFGEO_P0FIX_TEST_DSN",
    "postgresql://geo_admin:p0fixpass@localhost:55437/defgeo_p0fix_test")

_REQUIRED_DB_TOKENS = ("defgeo", "test")

#: pg_dump 尾部把 search_path 设成 '' —— 不中和的话之后所有不带 schema 限定的
#: 语句都找不到表(本仓 2026-08-12 记过这个毒)。
_SEARCH_PATH_POISON = "SELECT pg_catalog.set_config('search_path', '', false);"

#: 进程级单例(存 os.environ,不存模块级变量)。
#: 模块级变量会随 conftest 被 pytest 加载成两个模块对象而分身,等于没做单例
#: —— p03c 的 conftest 已经踩过这一脚,这里照它的形态来。
_DSN_ENV = "DEFGEO_P0FIX_SESSION_DSN_INPROC"
_BUILD_COUNT_ENV = "DEFGEO_P0FIX_SESSION_DB_BUILDS_INPROC"

#: 🔴 跨包复用:``tests/p03c_org_guards_2026_08_25`` 也建会话库、也 ``import server``。
#: 两个包合跑时,**一个进程只能有一个 server 模块、一个连接池**,却会建出两个库
#: —— 后建的那个从来没被 server 绑过,它上面的判据会以"莫名其妙"的形式红或绿。
#: 所以两边共用同一个进程级 DSN 槽:谁先建谁说了算,后来者复用。
#: (它们的库来自 08-17 dump、我的来自 08-19,但**迁移由 import server 全量重放**,
#:  两边最终都有 defgeo 040~050;下面 ``migrated_dsn`` 会逐项验收,验不过当场炸。)
_SHARED_DSN_ENVS = (_DSN_ENV, "P03C_SESSION_DSN_INPROC")


def _shared_existing_dsn():
    for key in _SHARED_DSN_ENVS:
        value = os.environ.get(key)
        if value:
            return value
    return None


def _admin_dsn() -> str:
    return ADMIN_DSN.rsplit("/", 1)[0] + "/postgres"


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _load_prod_schema(conn) -> None:
    sql = PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    if _SEARCH_PATH_POISON not in sql:
        raise AssertionError(
            "生产 dump 里没找到预期的 search_path 毒行 —— dump 形态可能变了,"
            "中和逻辑是否仍然必要必须由人确认,不能静默放过。")
    sql = sql.replace(_SEARCH_PATH_POISON, "-- [p0fix] search_path poison neutralised")

    # pg_dump 17+ 会写 psql **元命令** \restrict / \unrestrict;psycopg2 直送
    # 服务端会 syntax error,整份 dump 一行都装不上 —— 而"没装上"常常表现为
    # 后面某条判据莫名其妙红(甚至绿)。剥离后回头核一遍剥干净了没有。
    stripped = 0
    lines = []
    for ln in sql.splitlines():
        if ln.lstrip().startswith(("\\restrict", "\\unrestrict")):
            stripped += 1
            lines.append("-- [p0fix conftest] psql 元命令已剥离(第 %d 条)" % (stripped,))
        else:
            lines.append(ln)
    sql = "\n".join(lines)
    if "\\restrict" in sql or "\\unrestrict" in sql:
        raise AssertionError("psql 元命令没有被完全剥离 —— 剥离规则与 dump 形态不匹配")
    with conn.cursor() as cur:
        cur.execute(sql)


@pytest.fixture(scope="session")
def live_dsn():
    """一次性建库 + 灌生产 schema。整个 session 共用一个库。

    共用不是偷懒:``import server`` 慢且会 init_db/跑迁移,一个进程只能绑一个 DSN。
    判据之间靠**各自 mint 新的 run_token / session_id / brand / uid** 隔离,
    不靠换库 —— 换库反而会让 server 持有的连接池指向旧库(那才是假绿的来源)。
    """
    existing = _shared_existing_dsn()
    if existing:
        yield existing
        return
    builds = int(os.environ.get(_BUILD_COUNT_ENV, "0")) + 1
    os.environ[_BUILD_COUNT_ENV] = str(builds)
    if builds > 1:
        raise RuntimeError(
            "同一进程内第 %d 次建会话库 —— fixture 分裂了(单例没生效)。"
            "后果是 `import server` 的迁移只跑过先建的那个库,后建的那个缺表缺列,"
            "判据会以莫名其妙的形式红。" % builds)
    if not PROD_SCHEMA.is_file():
        pytest.skip("缺生产 schema 夹具 " + str(PROD_SCHEMA))

    name = "defgeo_p0fix_" + uuid.uuid4().hex[:8] + "_test"
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in name]
    if missing:
        raise RuntimeError("unsafe test db name %r(缺 %s)" % (name, missing))
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "' + name + '"')
    admin.close()
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name

    conn = _conn(dsn)
    _load_prod_schema(conn)
    conn.cursor().execute("SET search_path = public")
    conn.close()

    for key in _SHARED_DSN_ENVS:
        os.environ[key] = dsn
    try:
        yield dsn
    finally:
        for key in _SHARED_DSN_ENVS:
            os.environ.pop(key, None)
        os.environ.pop(_BUILD_COUNT_ENV, None)
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
        admin.close()


@pytest.fixture(scope="session")
def live_server(live_dsn):
    """把 **真 server 模块** import 进来,绑到上面那个真库。

    ``DATABASE_URL`` 必须在 import 之前落进环境:server 在 import 期就连库、
    并(ROLE 未设时)重放整份迁移清单。
    """
    os.environ["DATABASE_URL"] = live_dsn
    os.environ["TEST_DATABASE_URL"] = live_dsn
    os.chdir(REPO)                      # server.py 用相对路径写 output/ cache/

    import db.connection as dbconn
    dbconn.DATABASE_URL = live_dsn
    dbconn._pool = None

    import server                       # noqa: PLC0415 —— 就是要在这里真 import

    # 配对的必须不命中:import 成功不代表拿到的是**这棵树**的 server。
    assert pathlib.Path(server.__file__).resolve() == (REPO / "server.py").resolve(), (
        "import 到的不是本工作树的 server.py:%s" % server.__file__)
    assert dbconn.DATABASE_URL == live_dsn, "server import 过程把 DSN 改掉了"
    return server


@pytest.fixture(scope="session")
def migrated_dsn(live_dsn, live_server):
    """迁移已重放 **且经过验收** 的库。本包所有判据都吃这个,不吃 ``live_dsn``。

    🔴 为什么要单独一层:``import server`` 的迁移是 **非 fail-fast** 的
    (server.py:636 那一支单条失败只记日志)。所以"import 成功"证明不了
    "040~050 都装上了"。这里逐项验收本包真正依赖的那几样;缺任何一项就
    当场 fail,而不是让下游判据以 UndefinedColumn / None 的形式红得莫名其妙。
    """
    _ = live_server
    conn = _conn(live_dsn)
    try:
        cur = conn.cursor()
        for table in ("defgeo_question_plans", "defgeo_diagnosis_run_previews",
                      "diagnosis_runs", "point_freezes", "feature_pricing",
                      "organization_charge_links", "notification_outbox"):
            cur.execute("SELECT to_regclass(%s) AS r", ("public." + table,))
            assert cur.fetchone()["r"] is not None, (
                "迁移重放后 %s 仍不存在 —— import server 的迁移是非 fail-fast 的,"
                "它跳过了这一条。下游判据会以 UndefinedTable 的形式红,与被测代码无关。" % table)
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='diagnosis_runs'")
        have = {r["column_name"] for r in cur.fetchall()}
        for col in ("payer_user_id", "reserved_split_snapshot_jsonb"):
            assert col in have, (
                "diagnosis_runs 缺列 %s —— 迁移 %s 没跑上。"
                "缺列时本包 A-1/A-4 的判据会**恒绿**(读到 None 就当没落),"
                "那是最贵的一种绿。" % (col, "050" if col == "payer_user_id" else "048"))
    finally:
        conn.close()
    return live_dsn


@pytest.fixture
def db(migrated_dsn):
    c = _conn(migrated_dsn)
    try:
        yield c
    finally:
        c.close()
