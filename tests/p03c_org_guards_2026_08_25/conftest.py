"""P0-3c 判据地基 —— **真的把 server.py import 进来并驱动它**。

## 这个 conftest 存在的唯一理由

P0-3b 交了 92 条判据 + 27 发变异全绿,上线**每一单诊断必挂 NameError**
(`_dr` 未绑定,server.py:3329)。事后复盘的结构性真因只有一句:

    整个判据目录没有任何一条在运行时 import / 执行 `server.py`。

判据全打在 `services/` 的纯函数上 —— 而那些纯函数**正是为了"可判"才抽出来的**。
抽函数让逻辑可判了,**调用点反而没人管**。静态 AST 锁答得了"那行文本在不在",
答不了"那一行跑起来会不会抛"。

所以本包的地基就是:**把真 server 模块导进测试进程**,让判据从
`server.run_diagnosis_task` / `server._run_diagnosis_impl` 本体驱动。
一发「把符号改成未定义名」的执行毒必须能把判据打红 —— 那是本包的验收线。

## 为什么不用 TestClient 打真端点(R1-A 允许的 fallback,附实证)

`run_diagnosis_task` 是 `server.py:3736` 用 `asyncio.create_task(...)` **甩出去**的
游离任务,HTTP handler 不 await 它。TestClient 发完请求立刻返回,
结算分派在后台跑,判据**观察不到也 await 不到**。
所以按 R1-A 的 fallback 直接驱动 handler 本体 —— 这是有实证的选择,不是图省事。

## 库

真 PG16 + **生产 schema dump**(不是测试用的简化 schema:类型不同构会让判据
全绿而生产炸)。库名强制含 `test`;用完 DROP。
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

PROD_SCHEMA = pathlib.Path(os.getenv(
    "P03C_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
ADMIN_DSN = os.getenv(
    "P03C_TEST_DSN", "postgresql://geo_admin:p03pass@localhost:55435/p03c_test")


def _admin_dsn() -> str:
    return ADMIN_DSN.rsplit("/", 1)[0] + "/postgres"


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


#: 🔴 进程级单例。
#  pytest 的 fixture 按**名字 + 所在 conftest** 解析:另一个测试包若 re-export
#  这些 fixture,拿到的是**同名但不同**的 fixture 对象 —— session 作用域各算各的,
#  于是**建出两个库**。而 `import server`(带 `init_db`)只会对第一个库跑一次,
#  第二个库永远 `migration_ok=false` ⇒ org 就绪门 503,判据莫名其妙红。
#  实测:合跑 settlement_manual_ux + p03c 时 4 条 org 判据齐红,单跑各自全绿。
#  所以这里把库与 server 都做成**进程内只建一次**,让 re-export 安全。
#  ⚠️ 单例**不能**存模块级变量:pytest 可能把同一个 conftest.py 加载成**两个模块对象**
#     (conftest 的导入身份与 `import tests.xxx.conftest` 不一定是同一个),
#     那样会有两份模块级 dict、两个库,等于没做单例(我第一版就是这么失败的)。
#     存进 `os.environ` 才是真·进程级:模块加载几次都只有一份。
_DSN_ENV = "P03C_SESSION_DSN_INPROC"
_BUILD_COUNT_ENV = "P03C_SESSION_DB_BUILDS_INPROC"


@pytest.fixture(scope="session")
def live_dsn():
    """一次性建库 + 灌生产 schema。整个 session 共用一个库。

    共用不是偷懒:`import server` 要 40s 且会 init_db,一个进程只能绑一个 DSN。
    测试之间靠**每条各自 mint 新的 run_token / session_id / brand**隔离,
    不靠换库 —— 换库反而会让 server 持有的连接池指向旧库(那才是假绿的来源)。
    """
    _existing = os.environ.get(_DSN_ENV)
    if _existing:                           # 已经建过 —— 复用,绝不再建第二个库
        yield _existing
        return
    # 🔴 走到这里 = 本进程要**建一个会话库**。正常情况下整个 session 只该发生一次。
    #   发生第二次 = 单例失效、fixture 分裂(见上面那段注释)。
    #   计数放 os.environ:模块级变量会随 conftest 被重复加载而分身,计数就永远是 1,
    #   等于没有守卫(这条守卫我写坏过两版,一版查错了对象、一版被顺序绕过,
    #   都是"看起来在守、实际零区分力")。
    _builds = int(os.environ.get(_BUILD_COUNT_ENV, "0")) + 1
    os.environ[_BUILD_COUNT_ENV] = str(_builds)
    if _builds > 1:
        raise RuntimeError(
            "同一个进程内第 %d 次建会话库 —— fixture 分裂了。"
            "另一个测试包 re-export 了 live_dsn/live_server,而单例没生效;"
            "后果是 `import server` 的 init_db 只跑过先建的那个库,"
            "后建的那个 org 就绪门恒 503,判据会以「readiness 未就绪」的形式莫名其妙红。"
            % _builds)
    if not PROD_SCHEMA.is_file():
        pytest.skip("需要生产 schema 夹具 " + str(PROD_SCHEMA))
    name = "p03c_" + uuid.uuid4().hex[:8] + "_test"
    if "test" not in name:                       # 安全栓:库名必须含 test
        raise RuntimeError("unsafe test db name " + name)
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "' + name + '"')
    admin.close()
    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name

    conn = _conn(dsn)
    cur = conn.cursor()
    cur.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    # pg_dump 尾部把 search_path 设成 '' —— 不复位则后续所有无前缀 SQL 都找不到表。
    cur.execute("SET search_path = public")
    conn.close()
    os.environ[_DSN_ENV] = dsn
    try:
        yield dsn
    finally:
        os.environ.pop(_DSN_ENV, None)
        os.environ.pop(_BUILD_COUNT_ENV, None)
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
        admin.close()


@pytest.fixture(scope="session")
def live_server(live_dsn):
    """把 **真 server 模块** import 进来,绑到上面那个真库。

    `DATABASE_URL` 必须在 import 之前落进环境:server 在 import 期就连库 + init_db。
    """
    os.environ["DATABASE_URL"] = live_dsn
    os.environ["TEST_DATABASE_URL"] = live_dsn
    os.chdir(REPO)                       # server.py 用相对路径写 output/ cache/

    import db.connection as dbconn
    dbconn.DATABASE_URL = live_dsn
    dbconn._pool = None

    import server                        # noqa: PLC0415 —— 就是要在这里真 import

    # 配对的必须不命中:import 成功不代表拿到的是**这棵树**的 server。
    assert pathlib.Path(server.__file__).resolve() == (REPO / "server.py").resolve(), (
        "import 到的不是本工作树的 server.py:%s" % server.__file__)
    assert dbconn.DATABASE_URL == live_dsn, "server import 过程把 DSN 改掉了"
    return server


@pytest.fixture
def db(live_dsn):
    c = _conn(live_dsn)
    try:
        yield c
    finally:
        c.close()
