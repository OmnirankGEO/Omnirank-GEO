"""工单 V4-A(Codex fix-of-fix A-1/A-2/A-3)判据底座 —— 真 PG16 一次性库。

安全栓沿用本仓惯例:库名必须**同时**含 ``defgeo`` 与 ``test``。
2026-08-15 实测过库名不含 ``test`` 会让整套安全栓失效、双臂 0 junit 假绿。

两种库,两种用途
----------------
① ``migrated_dsn``(session)—— 生产 dump + manifest 里 040~054 全量重放。
   给「行为臂」用:触发器真的拦不拦得住、真实 writer 写不写得进去,
   都得在一个**装齐了的**库上打才算数。
② ``fresh_db``(function)—— 每条 poison 判据一把**空库**,自己按需装迁移。
   poison 的形态统一是:先干净跑一遍(必须过)→ 下毒 → 再跑一遍(必须 RAISE)。
   这正是 Codex 三审复现 P1-2 / P1-3 的那条路径,不是我另起的炉灶。

🔴 迁移一律从 ``db.migration_manifest.MIGRATIONS`` 现扫,不手抄第二份清单 ——
   漏登记 = 上线后永远不会跑,那要在这里就红。
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

#: 一次性 PG16 容器。库名安全栓在 `_new_db` 里再验一次。
ADMIN_DSN = os.getenv(
    "DEFGEO_V4A_TEST_DSN",
    "postgresql://geo_admin:v4apass@localhost:55494/postgres")

_REQUIRED_DB_TOKENS = ("defgeo", "test")

PROD_SCHEMA = REPO / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"

#: pg_dump 尾部把 search_path 设成 '' —— 不中和的话之后所有不带 schema 限定的
#: 语句都找不到表(本仓 2026-08-12 记过这个毒)。
_SEARCH_PATH_POISON = "SELECT pg_catalog.set_config('search_path', '', false);"

#: 本单要打的迁移区间 = 防御 GEO 班列。**从 manifest 现扫**,这里只给个范围谓词。
_RANGE = ("migration_04", "migration_05")


def defgeo_migrations() -> list[str]:
    from db.migration_manifest import MIGRATIONS

    out = [m for m in MIGRATIONS if any(tok in m for tok in _RANGE)]
    if not out:
        raise RuntimeError("manifest 里一条防御 GEO 迁移都没扫到 —— 分母塌了")
    return out


def admin_root() -> str:
    return ADMIN_DSN.rsplit("/", 1)[0]


def _new_db(tag: str) -> tuple[str, str]:
    name = "defgeo_v4a_%s_%s_test" % (tag, uuid.uuid4().hex[:6])
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in name]
    if missing:
        raise RuntimeError("unsafe test db name %r(缺 %s)" % (name, missing))
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "%s"' % name)
    admin.close()
    return name, admin_root() + "/" + name


def drop_db(name: str) -> None:
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
    admin.close()


def load_prod_schema(conn) -> None:
    if not PROD_SCHEMA.is_file():
        pytest.skip("缺生产 schema 夹具 " + str(PROD_SCHEMA))
    sql = PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    if _SEARCH_PATH_POISON not in sql:
        raise AssertionError(
            "生产 dump 里没找到预期的 search_path 毒行 —— dump 形态可能变了,"
            "中和逻辑是否仍然必要必须由人确认,不能静默放过。")
    sql = sql.replace(_SEARCH_PATH_POISON, "-- [v3a] search_path poison neutralised")
    stripped = 0
    lines = []
    for ln in sql.splitlines():
        if ln.lstrip().startswith(("\\restrict", "\\unrestrict")):
            stripped += 1
            lines.append("-- [v3a] psql 元命令已剥离(第 %d 条)" % stripped)
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


def build_migrated(conn, upto: str | None = None) -> list[str]:
    """dump + 按 manifest 顺序重放防御 GEO 班列。返回真正跑过的清单。"""
    load_prod_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SET search_path = public")
    ran = []
    for rel in defgeo_migrations():
        run_migration(conn, rel)
        ran.append(rel)
        if upto and upto in rel:
            break
    return ran


@pytest.fixture(scope="session")
def migrated_dsn():
    """装齐了的一次性库。行为臂吃它。"""
    name, dsn = _new_db("full")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        ran = build_migrated(conn)
        assert len(ran) >= 13, "只重放了 %d 份迁移 —— 分母塌了" % len(ran)
        # 配对的必须不命中:装完之后本单要打的对象**真的**在。
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_trigger "
                        "WHERE tgname='trg_defgeo_qplan_immutable' "
                        "  AND tgrelid=to_regclass('public.defgeo_question_plans')")
            assert cur.fetchone()[0] == 1, "040 的不可变触发器没装上 —— 判据会因为错误的原因绿"
    finally:
        conn.close()
    yield dsn
    drop_db(name)


@pytest.fixture(scope="session")
def _chain_template():
    """把「dump + 全班列迁移」建**一次**,之后每条 poison 用 ``TEMPLATE`` 克隆。

    不是为了省时间才这么做(虽然确实从 ~15s/条降到 ~1s/条):
    每条 poison 仍然拿到一把**物理独立**的库,残留不会跨判据累积 ——
    本仓 08-26 记过「批跑变异共用一个库 ⇒ 残留跨变异累积,溢出随批次位置单调增长」。
    克隆 = 既独立又便宜,比"共用一把库然后小心翼翼清理"结构上更稳。
    """
    name, dsn = _new_db("tmpl")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        ran = build_migrated(conn)
        assert len(ran) >= 13, "模板库只重放了 %d 份迁移 —— 分母塌了" % len(ran)
    finally:
        conn.close()
    yield name
    drop_db(name)


@pytest.fixture()
def chain_db(_chain_template):
    """一把**装齐了**的一次性库(克隆自模板)。工厂形态:``dsn = chain_db("tag")``。"""
    made: list[str] = []

    def _make(tag: str) -> str:
        name, dsn = None, None
        admin = psycopg2.connect(ADMIN_DSN)
        admin.autocommit = True
        try:
            name = "defgeo_v4a_%s_%s_test" % (tag, uuid.uuid4().hex[:6])
            assert all(t in name for t in _REQUIRED_DB_TOKENS), "unsafe test db name"
            admin.cursor().execute(
                'CREATE DATABASE "%s" TEMPLATE "%s"' % (name, _chain_template))
        finally:
            admin.close()
        made.append(name)
        return admin_root() + "/" + name

    yield _make
    for name in made:
        drop_db(name)


@pytest.fixture()
def fresh_db():
    """每条 poison 一把空库。工厂形态:``dsn = fresh_db("tag")``。"""
    made: list[str] = []

    def _make(tag: str):
        name, dsn = _new_db(tag)
        made.append(name)
        return dsn

    yield _make
    for name in made:
        drop_db(name)
