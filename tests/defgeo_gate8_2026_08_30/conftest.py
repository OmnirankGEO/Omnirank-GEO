"""门八第三发现(confirm 之后到执行器领取之间的那段窗口)判据底座。

真 PG16 一次性库。安全栓沿用本仓惯例:库名必须**同时**含 ``defgeo`` 与 ``test``。

🔴 DSN 默认值指向**自己**那把一次性库(55496),不是 ``TEST_DATABASE_URL``
   这种全局惯例名 —— 「设了 TEST_DATABASE_URL 不代表这个包读它」是本仓记过的事故形状。
   本包 = ``DEFGEO_GATE8_TEST_DSN``。

结构与 ``tests/defgeo_v5a_2026_08_28/conftest.py`` 同源(模板库 CLONE 出一次性库),
只换了变量名 / 端口 / 库名前缀。
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

#: 🔴 专属变量名 + 默认值指向本包自己的一次性容器(端口 55495)。
ADMIN_DSN = os.getenv(
    "DEFGEO_GATE8_TEST_DSN",
    "postgresql://geo_admin:gate8pass@localhost:55496/postgres")

#: 🔴 app 侧不少模块在 **import 期**就 `init_db()`(`db/diagnosis_db.py` 文件末尾那句)。
#: 所以在任何 app 模块被导入之前先把 DATABASE_URL 指到本包自己的容器上,
#: 每条判据再用 `_bind_db` 重指到那把一次性库。不这么做,autouse fixture 里
#: 一句 `import auth.brand_access` 就会在 fixture 顺序里先炸(实测)。
os.environ.setdefault("DATABASE_URL", ADMIN_DSN)

_REQUIRED_DB_TOKENS = ("defgeo", "test")

PROD_SCHEMA = REPO / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"

#: pg_dump 尾部把 search_path 设成 '' —— 不中和的话之后所有不带 schema 限定的
#: 语句都找不到表(本仓 2026-08-12 记过这个毒)。
_SEARCH_PATH_POISON = "SELECT pg_catalog.set_config('search_path', '', false);"

_RANGE = ("migration_04", "migration_05")


def defgeo_migrations() -> list[str]:
    from db.migration_manifest import MIGRATIONS

    out = [m for m in MIGRATIONS if any(tok in m for tok in _RANGE)]
    if not out:
        raise RuntimeError("manifest 里一条防御 GEO 迁移都没扫到 —— 分母塌了")
    return out


def admin_root() -> str:
    return ADMIN_DSN.rsplit("/", 1)[0]


def _safe_name(tag: str) -> str:
    name = "defgeo_gate8_%s_%s_test" % (tag, uuid.uuid4().hex[:6])
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in name]
    if missing:
        raise RuntimeError("unsafe test db name %r(缺 %s)" % (name, missing))
    return name


def _new_db(tag: str, template: str | None = None) -> tuple[str, str]:
    name = _safe_name(tag)
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    try:
        if template:
            admin.cursor().execute('CREATE DATABASE "%s" TEMPLATE "%s"' % (name, template))
        else:
            admin.cursor().execute('CREATE DATABASE "%s"' % name)
    finally:
        admin.close()
    return name, admin_root() + "/" + name


def drop_db(name: str) -> None:
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    try:
        admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
    finally:
        admin.close()


def load_prod_schema(conn) -> None:
    if not PROD_SCHEMA.is_file():
        pytest.skip("缺生产 schema 夹具 " + str(PROD_SCHEMA))
    sql = PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    if _SEARCH_PATH_POISON not in sql:
        raise AssertionError(
            "生产 dump 里没找到预期的 search_path 毒行 —— dump 形态可能变了,"
            "中和逻辑是否仍然必要必须由人确认,不能静默放过。")
    sql = sql.replace(_SEARCH_PATH_POISON, "-- [gate8] search_path poison neutralised")
    stripped = 0
    lines = []
    for ln in sql.splitlines():
        if ln.lstrip().startswith(("\\restrict", "\\unrestrict")):
            stripped += 1
            lines.append("-- [gate8] psql 元命令已剥离(第 %d 条)" % stripped)
        else:
            lines.append(ln)
    if stripped == 0:
        raise AssertionError("生产 dump 里一条 psql 元命令都没有 —— 剥离规则可能已过时")
    sql = "\n".join(lines)
    if "\\restrict" in sql or "\\unrestrict" in sql:
        raise AssertionError("psql 元命令没有被完全剥离")
    sql = sql.replace("CREATE EXTENSION IF NOT EXISTS", "CREATE EXTENSION")
    sql = sql.replace("CREATE EXTENSION ", "CREATE EXTENSION IF NOT EXISTS ")
    assert "CREATE EXTENSION IF NOT EXISTS IF NOT EXISTS" not in sql, "归一写重了"
    with conn.cursor() as cur:
        cur.execute(sql)


def run_migration(conn, rel: str) -> None:
    """跑**一份**迁移。失败原样抛 —— 「跑不起来」必须当场炸,不许静默跳过。"""
    sql = (REPO / rel).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)


def build_migrated(conn) -> list[str]:
    load_prod_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SET search_path = public")
    ran = []
    for rel in defgeo_migrations():
        run_migration(conn, rel)
        ran.append(rel)
    return ran


@pytest.fixture(scope="session")
def _chain_template():
    """建**一次**模板库,之后每条判据 ``TEMPLATE`` 克隆一把物理独立的库。"""
    name, dsn = _new_db("tmpl")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        ran = build_migrated(conn)
        assert len(ran) >= 13, "模板库只重放了 %d 份迁移 —— 分母塌了" % len(ran)
        # 配对的必须不命中:本包要打的表真的在,判据不会因为"表不存在"而绿。
        with conn.cursor() as cur:
            for tbl in ("diagnosis_runs", "brands", "point_freezes",
                        "notification_outbox"):
                cur.execute("SELECT to_regclass(%s)", ("public." + tbl,))
                assert cur.fetchone()[0] is not None, "%s 不在 —— 判据会因错误的原因绿" % tbl
    finally:
        conn.close()
    yield name
    drop_db(name)


@pytest.fixture(scope="session", autouse=True)
def _bind_app_db(_chain_template):
    """把 ``DATABASE_URL`` 指到**装齐了的模板库**,并在这里把 ``server`` 导进来一次。

    🔴 为什么必须在这儿:``server.py`` 在 **import 期**就跑 startup 迁移 + 注册 cron,
       指着空库会直接 ``RuntimeError: cron 调度器注册失败,拒绝启动``。
       而被测的生产代码本身就有一句 ``from server import mark_session_queued`` ——
       所以"能不能 import server"不是判据的方便问题,是被测路径的一部分。

    🔴 顺序也是判据的一部分:server 的 import 期迁移会**改模板库**。
       放在这里(克隆之前)⇒ 每把一次性库形态一致;放到某条判据里 ⇒
       第一把和后面几把不同构,而那种不同构不会有任何信号。
    """
    # 🔴 **不能**直接用模板库:import server 会一直握着一条连接,
    #    之后 `CREATE DATABASE … TEMPLATE` 就会 ObjectInUse(实测)。
    #    所以先从模板克隆一把"给 app 用的库",server 指着它;
    #    模板本身保持零连接,每条判据照常克隆。
    name, dsn = _new_db("app", template=_chain_template)
    os.environ["DATABASE_URL"] = dsn
    import db.connection as dbconn

    dbconn.DATABASE_URL = dsn
    dbconn._pool = None
    import server  # noqa: F401  —— 只为触发那一次 import 期初始化
    yield
    # server 的连接池还握着它 —— FORCE 断开再删。
    drop_db(name)


@pytest.fixture()
def chain_db(_chain_template):
    """一把装齐了的一次性库(克隆自模板)。工厂形态:``dsn = chain_db("tag")``。"""
    made: list[str] = []

    def _make(tag: str) -> str:
        name, dsn = _new_db(tag, template=_chain_template)
        made.append(name)
        return dsn

    yield _make
    for name in made:
        drop_db(name)


@pytest.fixture()
def fresh_db():
    made: list[str] = []

    def _make(tag: str) -> str:
        name, dsn = _new_db(tag)
        made.append(name)
        return dsn

    yield _make
    for name in made:
        drop_db(name)

# ══════════════════════════════════════════════════════════════════════
# 共享:真 HTTP 客户端 + 真链 confirm
#
# 🔴 放 conftest 不是为了好看:两个判据文件都要"真的走一遍 confirm"。
#    各写一份的话,改了一处忘了另一处,而漂掉的那一份会静默退化成
#    "根本没 confirm",它后面的断言全变成零分母。
# ══════════════════════════════════════════════════════════════════════
PLAN_PATH = "/api/defensive-geo/question-plans/preview"
PREVIEW_PATH = "/api/defensive-geo/run-previews"


def seed_owner_brand_pricing(cur, tenant, prefix):
    """一个用户 + 一个品牌 + 一条 0 算力价目行(0 算力是刻意的:本包不测计价)。"""
    import uuid as _uuid

    cur.execute(
        "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
        "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
        (tenant, prefix + "_owner", prefix, prefix + "@example.com"))
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                (prefix.upper() + "_" + _uuid.uuid4().hex[:6], tenant))
    brand_id = int(cur.fetchone()["id"])
    cur.execute(
        "INSERT INTO feature_pricing (feature_code, feature_name, cost_points, "
        "  requires_paid_points, is_active) "
        "VALUES ('geo_diagnosis','GEO 体检',0,false,true) "
        "ON CONFLICT (feature_code) DO UPDATE SET cost_points=0, "
        "  requires_paid_points=false, is_active=true")
    return brand_id


def make_client(tenant):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.defensive_geo_api import router

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):        # noqa: ANN001
        request.state.user = {"user_id": tenant, "is_admin": False}
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def confirm_once(client, brand_id, tenant):
    """真 HTTP:题单 → preview → confirm。返回 confirm 的 Response(不替调用方断言状态码)。"""
    import uuid as _uuid

    h = {"X-Test-Tenant": str(tenant)}
    plan = client.post(PLAN_PATH, json={
        "clientRequestId": "creq-" + _uuid.uuid4().hex[:10],
        "brandId": brand_id, "profileRevisionId": "prof-1", "mode": "defensive",
        "questions": [{"text": "这个牌子靠谱吗", "modeSide": "defensive",
                       "familyKey": "identity_check", "brandExposure": "named"}],
    }, headers=h)
    assert plan.status_code == 200, "题单没签出来:" + plan.text
    plan = plan.json()

    prev = client.post(PREVIEW_PATH, json={
        "questionPlanId": plan["planId"], "questionPlanRevision": plan["planRevision"],
        "profileRevisionId": "prof-1", "platformKeys": ["deepseek"],
    }, headers=dict(h, **{"Idempotency-Key": "idem-" + _uuid.uuid4().hex[:10]}))
    assert prev.status_code == 200, "preview 没签出来:" + prev.text
    prev = prev.json()

    return client.post(
        PREVIEW_PATH + "/" + prev["previewId"] + "/confirm",
        json={"expectedHash": prev["canonicalHash"]},
        headers=dict(h, **{"Idempotency-Key": "idem-" + _uuid.uuid4().hex[:10]}))

# ══════════════════════════════════════════════════════════════════════
# 真 Redis 目标:本包**统一**在这里绑定/复位
#
# 🔴 为什么放 conftest:两个判据文件都要控制 Redis 在/不在。各自绑各自的话,
#    一个文件把 progress_bus 指到死端口,泄漏给下一个文件 —— 实测踩过:
#    另一个文件的主锁因此"快照是空"而红,红因与被测行为无关。
#    autouse 复位把这条泄漏路径整个关掉。
# ══════════════════════════════════════════════════════════════════════
from tests._shared.progress_redis_guard import (   # noqa: E402
    bind_progress_bus, resolve_target, verify_ephemeral_container,
)

REDIS_DSN_ENV = "DEFGEO_GATE8_TEST_REDIS_DSN"
REDIS_CONTAINER_ENV = "DEFGEO_GATE8_TEST_REDIS_CONTAINER"
REDIS_DSN_DEFAULT = "redis://127.0.0.1:55599/1"
REDIS_CONTAINER_DEFAULT = "omnirank-progress-test-gate8"
#: "失联"= 一个确定连不上的端口 —— 真 connect 失败,不是打桩。
DEAD_REDIS_PORT = 1


def redis_target():
    return resolve_target(REDIS_DSN_ENV, REDIS_CONTAINER_ENV,
                          dsn_default=REDIS_DSN_DEFAULT,
                          container_default=REDIS_CONTAINER_DEFAULT)


def redis_up():
    import cache.progress_bus as pb

    target = redis_target()
    verify_ephemeral_container(target)
    bind_progress_bus(pb, target)
    return pb


def redis_down():
    """真连接失败(指死端口),**不 monkeypatch `_get_sync_redis`** ——
    打桩会把"只有这一扇门"变成假设而不是判据。"""
    import cache.progress_bus as pb

    bind_progress_bus(pb, redis_target(), port=DEAD_REDIS_PORT)
    return pb


@pytest.fixture(autouse=True)
def _redis_known_state():
    """每条判据开跑前把 progress_bus 指回目标,跑完再指回去 —— **只绑定,不体检**。

    为什么不在这里 fail:纯静态的锁(比如"守卫只此一份")根本不需要 Redis,
    让它们因为 Redis 不在而 error,就是把判据的失败原因和被测行为解耦。
    要 Redis 的判据显式 `live_redis`。
    """
    import cache.progress_bus as pb

    bind_progress_bus(pb, redis_target())
    yield
    bind_progress_bus(pb, redis_target())


@pytest.fixture()
def live_redis():
    """要真 Redis 的判据显式请它:连不上/容器不可信就 **fail**(判据不可用),**不 skip**。"""
    pb = redis_up()
    if pb._get_sync_redis() is None:
        pytest.fail(
            "判据不可用:%r 上没有可连的 Redis。起一把贴了一次性测试标签的容器"
            "(命名/标签/端口映射约定见 tests/_shared/progress_redis_guard.py),"
            "收尾 docker rm -f -v。" % (redis_target(),))
    return pb


# ══════════════════════════════════════════════════════════════════════
# 共享:一次性库 env + 造 run 行 + 打真 /status 端点
#
# 🔴 放 conftest 的理由与上面那段一样:两个判据文件都要"造一条真 run 行再打真端点"。
#    各写一份 ⇒ 改了一处忘了另一处,而漂掉的那一份会静默变成测别的东西。
# ══════════════════════════════════════════════════════════════════════
TENANT = 8481
OTHER = 8482


class StatusRequest:
    """够 `get_diagnosis_session_status` 用的最小 Request 替身。

    只喂它真正读的两样:`state.user` / `state.organization_identity`。
    归属**不 monkeypatch** —— 那正是要验的东西。
    """

    def __init__(self, user):
        class _S:
            pass

        self.state = _S()
        self.state.user = user
        self.state.organization_identity = None


def poll_status(sid, user_id=TENANT, admin=False):
    from server import get_diagnosis_session_status

    return get_diagnosis_session_status(
        sid, StatusRequest({"user_id": user_id, "is_admin": admin}))


def insert_run(conn, brand_id, *, run_status="running", alive=True, tenant=TENANT):
    """库里造一条真 run 行(归属 = tenant),返回它的 session_id。

    `alive=True` ⇒ `finished_at IS NULL`(第三支的可达前提)。
    freeze_id/freeze_backend/freeze_task_ref 不是装饰:`chk_freeze_handle` 是库级不变式,
    少给夹具当场 CheckViolation(那不是"判据红了",是"夹具坏了")。
    """
    import uuid as _uuid

    sid = "defgeo_run_" + _uuid.uuid4().hex[:10]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO diagnosis_runs (run_token, session_id, owner_user_id, brand_id,"
            " client_request_id, billing_mode, run_status, freeze_task_ref, finished_at,"
            " freeze_id, freeze_backend) "
            "VALUES (%s,%s,%s,%s,%s,'paid',%s,%s,%s,1,'legacy')",
            (sid.replace("defgeo_", ""), sid, tenant, brand_id,
             "creq-" + _uuid.uuid4().hex[:8], run_status,
             "diag_" + sid, None if alive else "2026-01-01 00:00:00"))
    return sid
