"""工单 E3 判据底座 —— 真 PG16 一次性库,**从真迁移文件建库**。

🔴 与本仓既有 defgeo 底座(pkgE/pkgF/W4)的关键差别,以及为什么必须不同
--------------------------------------------------------------------
那几个底座建 `monitoring_run_cells` 用的是 conftest 里**手写**的
``_LIVE_SCHEMA``。工单 E3-1 要判的恰恰是「注册表 / 夹具 / 桥读的键 与
**真 DDL** 对不对得上」—— 拿手写夹具当分母去判这件事,分母与被测对象同源,
判不出任何东西。本仓刚刚为此付了一次学费:

  ``tests/defensive_geo_pkgf_2026_08_23/conftest._LIVE_SCHEMA`` 里凭空多了一列
  ``billing_user_id``(生产 monitoring_run_cells 没有这一列)。而
  ``test_open_for_claim_only_reads_keys_the_live_cell_row_really_has``
  ——**专门为这个 bug 写的锁**—— 的分母正是那份夹具,于是它恒绿,
  桥在生产里恒落租户 0 一年无人知。

所以本底座:**先建最小前置表 → 再原样执行真迁移文件**。
`monitoring_run_cells` 的列/约束/索引全部由
``scripts/migration_monitoring_cell_retry_2026_07_21.sql`` +
``db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql`` 真的建出来,
``information_schema`` 因此是一个与所有被测对象都无关的独立分母。

🔴 前置表只建**真迁移点名要的那几张**(monitoring_tasks / quotes /
   monitoring_results / brands),而且只建 FK 需要的列 —— 它们是**输入**,
   不是被测对象;被测对象是迁移自己建的那张表。

🔴 安全栓:库名必须同时含 ``e3`` 与 ``test``。2026-08-15 实测过库名不含
   ``test`` 会让安全栓整个失效、双臂 0 junit 假绿。
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_THROWAWAY_URL = (
    "postgresql://geo_admin:testpw@localhost:55487/geo_e3_ledger_test"
)

_REQUIRED_DB_TOKENS = ("e3", "test")

#: 🔴 **真**迁移文件,按 manifest 里的相对次序。判据的被测对象之一就是
#:    "这几个文件跑完之后库里长什么样",所以这里不许有任何手写替身。
PACKAGE_MIGRATIONS = (
    "scripts/migration_monitoring_cell_retry_2026_07_21.sql",
    "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
    "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql",
)

LEDGER_TABLE = "defgeo_monitoring_attempts"

#: 真迁移点名依赖的前置表。**只**建 FK / 唯一键需要的列。
_PREREQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS public.brands (
    id            SERIAL PRIMARY KEY,
    name          TEXT,
    owner_user_id INTEGER
);

CREATE TABLE IF NOT EXISTS public.quotes (
    id        SERIAL PRIMARY KEY,
    brand_id  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS public.monitoring_tasks (
    id          SERIAL PRIMARY KEY,
    brand_id    INTEGER NOT NULL,
    total_tests INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.monitoring_results (
    id      SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL
);

-- 🔴 [工单 V3-C · C-1] 这两张表**不是**被测对象,是
--    `reject_monitoring_run_cell_terminal_overwrite` 触发器体里 EXISTS 子查询
--    点名的表。生产上它们当然存在;在本底座上不建,任何真的 UPDATE
--    `monitoring_run_cells` 的判据都会以 `UndefinedTable` 的形式**假红** ——
--    红的原因写着"表不存在",而被测的回填逻辑其实一行都没跑到。
--
-- 🔴 如实措辞:这是**列的子集**,只含触发器真正引用的那几列,
--    不是生产 DDL 的精确副本。它们只作输入存在;没有任何判据拿它们当分母。
CREATE TABLE IF NOT EXISTS public.point_freezes (
    id       BIGSERIAL PRIMARY KEY,
    task_ref VARCHAR(160)
);

CREATE TABLE IF NOT EXISTS public.organization_charge_links (
    id                 BIGSERIAL PRIMARY KEY,
    physical_backend   VARCHAR(64),
    physical_freeze_id TEXT,
    status             VARCHAR(32)
);
"""


def _url() -> str:
    url = (os.getenv("E3_TEST_DATABASE_URL")
           or os.getenv("TEST_DATABASE_URL")
           or DEFAULT_THROWAWAY_URL)
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in tail]
    if missing:
        raise RuntimeError(
            f"拒绝在库名 {tail!r} 上跑:安全栓要求库名同时含 {_REQUIRED_DB_TOKENS}。"
            f"缺 {missing} —— 本底座会 DROP SCHEMA public CASCADE。")
    return url


def _read_sql(rel: str) -> str:
    with io.open(ROOT / rel, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _assert_fixture_actually_ran(cur) -> None:
    """活性自证 —— 没有这一条,空库上跑的判据会全绿。"""
    required = (LEDGER_TABLE, "monitoring_run_cells",
                "monitoring_keyword_settlements", "monitoring_tasks", "brands")
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
        "AND tablename = ANY(%s)", (list(required),))
    got = {r["tablename"] for r in cur.fetchall()}
    missing = sorted(set(required) - got)
    if missing:
        raise RuntimeError(
            f"底座装完却查不到表 {missing} —— 判据没有被测对象。"
            "这一条炸了比后面一片绿有用。")
    # 本包的两条承重约束(工单 E3-1 的结构承重点)必须真的在。
    cur.execute(
        "SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
        ([
            "chk_monitoring_run_cells_tenant_owner_positive",
            "chk_defgeo_attempt_tenant_owner_positive",
        ],))
    have = {r["conname"] for r in cur.fetchall()}
    absent = sorted({
        "chk_monitoring_run_cells_tenant_owner_positive",
        "chk_defgeo_attempt_tenant_owner_positive",
    } - have)
    if absent:
        raise RuntimeError(
            f"禁 0 的两条 CHECK 缺 {absent} —— 「编造租户」的结构承重点没装上,"
            "关于它的判据会全部假绿")


@pytest.fixture(scope="session")
def e3_db_url() -> str:
    return _url()


@pytest.fixture(scope="session")
def e3_db(e3_db_url):
    conn = psycopg2.connect(e3_db_url,
                            cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute(_PREREQ_SCHEMA)
        for rel in PACKAGE_MIGRATIONS:
            cur.execute(_read_sql(rel))
        _assert_fixture_actually_ran(cur)
    yield conn
    conn.close()


@pytest.fixture
def cur(e3_db, e3_db_url):
    """每条判据一个干净事务面。

    🔴 autocommit=False 与生产同构 —— 桥的 ``guarded`` 用 SAVEPOINT,
       而 SAVEPOINT 只能在事务块内使用;autocommit 下它报错被吞,
       于是**每一个**桥函数变成空转、判据看起来在跑真链实际一行没写
       (pkgF 记过这一条,这里同规矩)。
    """
    ddl = e3_db.cursor()
    # 🔴 `monitoring_provider_review_events` 有 append-only 触发器
    #    (`reject_monitoring_provider_review_event_mutation`),TRUNCATE 会被它
    #    RAISE 掉。它是 CASCADE 顺带拉进来的,不是本包判据的对象 ——
    #    显式关掉再开,不用 CASCADE 悄悄绕过(绕不过,只会整条 fixture 报错)。
    ddl.execute("ALTER TABLE public.monitoring_provider_review_events "
                "DISABLE TRIGGER USER")
    try:
        ddl.execute(f"TRUNCATE {LEDGER_TABLE}")
        ddl.execute(
            "TRUNCATE monitoring_run_cells, monitoring_keyword_settlements, "
            "monitoring_cell_retry_requests, monitoring_provider_review_events, "
            "monitoring_results, monitoring_tasks, brands "
            "RESTART IDENTITY CASCADE")
    finally:
        ddl.execute("ALTER TABLE public.monitoring_provider_review_events "
                    "ENABLE TRIGGER USER")
    ddl.close()

    conn = psycopg2.connect(e3_db_url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    c = conn.cursor()
    try:
        yield c
        conn.commit()
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# E3-3 poison 底座 —— 真冷库 + 真迁移
# ══════════════════════════════════════════════════════════════════════
#
# poison 判据问的是:「库里先有一个**同名但定义错误**的对象,迁移会不会
# 当场 RAISE,还是静默采信」。要问出这句话,必须在一个**还没跑过那条迁移**
# 的库上预置那个坏对象。
#
# 🔴 用 database 而不是 schema:本仓迁移全部写死 ``public.`` 限定名,
#    换 ``search_path`` 骗不过它们(骗得过才可怕 —— 那意味着迁移会在
#    调用方当时的 search_path 上乱建东西)。
#
#: 模板库 = 生产 dump + 051/052/054 **之前**的全部依赖。每条 poison 判据从它
#: ``CREATE DATABASE ... TEMPLATE`` 出一个新库(秒级),poison 完再跑那一条迁移。
TEMPLATE_DB_NAME = "geo_defgeo_e3_tpl_test"
COLD_DB_NAME = "geo_defgeo_e3_cold_test"

#: 模板要装的前置迁移(**不含**被 poison 的 051/052/054)。
TEMPLATE_MIGRATIONS = (
    "db/migration_040_defgeo_question_plans_2026_08_21.sql",
    "db/migration_041_defgeo_run_previews_2026_08_21.sql",
    "db/migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql",
    "db/migration_043_defgeo_activation_outbox_2026_08_21.sql",
    "db/migration_044_defgeo_publish_decision_2026_08_21.sql",
    "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
    "db/migration_047_defgeo_publish_provider_ref_2026_08_24.sql",
)

#: 被 poison 的三条迁移各自的目标对象,判据用它当分母。
POISON_TARGETS = {
    "051": "db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql",
    "052": "db/migration_052_defgeo_activation_frozen_payer_2026_08_25.sql",
    "054": "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql",
}

_PROD_SCHEMA = ROOT / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"


def _base_url(url: str) -> str:
    return url.rpartition("/")[0]


def _load_prod_schema(conn) -> None:
    """复用窗C conftest 的装载器,不写第二份。

    🔴 第二份必有一处没人验 —— 而这份装载器里的三处"毒"(search_path 毒行 /
       psql 元命令 / CREATE EXTENSION 并发)每一处都是实测踩出来的。
    """
    import importlib.util

    # 🔴 窗C conftest 在**模块导入期**就跑安全栓(库名必须含 defgeo+test),
    #    所以导入之前必须把 TEST_DATABASE_URL 换成一个过得了栓的名字,
    #    导入完再还原 —— 否则本包的 e3 库名会让它在 import 期直接抛。
    #    (模板库名 geo_defgeo_e3_tpl_test 本来就同时含 defgeo / e3 / test。)
    saved = os.environ.get("TEST_DATABASE_URL")
    os.environ["TEST_DATABASE_URL"] = (
        f"{_base_url(DEFAULT_THROWAWAY_URL)}/{TEMPLATE_DB_NAME}")
    try:
        spec = importlib.util.spec_from_file_location(
            "_e3_w3conf", ROOT / "tests/defensive_geo_w3_2026_08_21/conftest.py")
        w3 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(w3)
    finally:
        if saved is None:
            os.environ.pop("TEST_DATABASE_URL", None)
        else:
            os.environ["TEST_DATABASE_URL"] = saved
    w3._load_prod_schema(conn)


@pytest.fixture(scope="session")
def poison_template(e3_db_url) -> str:
    if not _PROD_SCHEMA.exists():                      # pragma: no cover
        pytest.skip(f"缺生产 schema 快照:{_PROD_SCHEMA}")
    for token in _REQUIRED_DB_TOKENS:
        assert token in TEMPLATE_DB_NAME, f"模板库名少了安全栓 token {token!r}"

    admin = psycopg2.connect(e3_db_url)
    admin.autocommit = True
    with admin.cursor() as c:
        c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  "WHERE datname = ANY(%s) AND pid<>pg_backend_pid()",
                  ([TEMPLATE_DB_NAME, COLD_DB_NAME],))
        c.execute(f'DROP DATABASE IF EXISTS "{COLD_DB_NAME}"')
        c.execute(f'DROP DATABASE IF EXISTS "{TEMPLATE_DB_NAME}"')
        c.execute(f'CREATE DATABASE "{TEMPLATE_DB_NAME}"')
    admin.close()

    tpl_url = f"{_base_url(e3_db_url)}/{TEMPLATE_DB_NAME}"
    conn = psycopg2.connect(tpl_url)
    conn.autocommit = True
    _load_prod_schema(conn)
    conn.close()

    for rel in TEMPLATE_MIGRATIONS:
        c = psycopg2.connect(tpl_url)
        c.autocommit = True
        try:
            with c.cursor() as cur_:
                cur_.execute("SET search_path TO public")
                cur_.execute(_read_sql(rel))
        except psycopg2.Error as exc:                  # pragma: no cover
            raise RuntimeError(
                f"poison 模板装 {rel} 失败,判据没有被测对象:{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            c.close()

    # 活性自证:模板里必须**没有**被 poison 的那三个对象,否则 poison 无从预置。
    probe = psycopg2.connect(tpl_url, cursor_factory=psycopg2.extras.RealDictCursor)
    probe.autocommit = True
    with probe.cursor() as cur_:
        cur_.execute("SELECT to_regclass('public.uq_defgeo_pcmd_provider_order_ref') AS i")
        assert cur_.fetchone()["i"] is None, "模板里已经有 051 的索引 —— poison 无从预置"
        for name in ("defgeo_activation_outbox_payer_group",
                     "chk_monitoring_run_cells_tenant_owner_positive"):
            cur_.execute("SELECT 1 AS x FROM pg_constraint WHERE conname=%s", (name,))
            assert cur_.fetchone() is None, f"模板里已经有 {name} —— poison 无从预置"
        # 反向:被 poison 的宿主表必须**在**,否则迁移会红在"表不存在"上,
        # 而那与"同名弱定义被采信"是两件事。
        for tbl in ("defgeo_publish_commands", "defgeo_activation_outbox",
                    "monitoring_run_cells"):
            cur_.execute("SELECT to_regclass(%s) AS r", (f"public.{tbl}",))
            assert cur_.fetchone()["r"] is not None, f"模板里缺宿主表 {tbl}"
    probe.close()
    return tpl_url


@pytest.fixture
def cold_db(e3_db_url, poison_template):
    """从模板克隆一个全新冷库,yield (cursor, apply_migration)。"""
    admin = psycopg2.connect(e3_db_url)
    admin.autocommit = True
    with admin.cursor() as c:
        c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  "WHERE datname=%s AND pid<>pg_backend_pid()", (COLD_DB_NAME,))
        c.execute(f'DROP DATABASE IF EXISTS "{COLD_DB_NAME}"')
        c.execute(f'CREATE DATABASE "{COLD_DB_NAME}" TEMPLATE "{TEMPLATE_DB_NAME}"')
    admin.close()

    cold_url = f"{_base_url(e3_db_url)}/{COLD_DB_NAME}"
    conn = psycopg2.connect(cold_url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    cur_ = conn.cursor()

    def apply_migration(tag: str) -> None:
        """把真迁移文件原样喂给冷库(判据里不许手抄迁移片段)。"""
        cur_.execute("SET search_path TO public")
        cur_.execute(_read_sql(POISON_TARGETS[tag]))

    try:
        yield cur_, apply_migration
    finally:
        conn.close()
