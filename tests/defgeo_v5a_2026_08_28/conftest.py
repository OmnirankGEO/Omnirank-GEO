"""工单 V5-A(Codex fix-of-fix2 P1-3 / P1-4 / P1-5 / P2-1 / P2-2)判据底座。

真 PG16 一次性库。安全栓沿用本仓惯例:库名必须**同时**含 ``defgeo`` 与 ``test``。

🔴 DSN 默认值指向**自己**那把一次性库(55495),不是别人的、也不是
   ``TEST_DATABASE_URL`` 这种全局惯例名。上一轮 C 窗事故的形状就是
   「设了 TEST_DATABASE_URL 不代表这个包读它」—— 结果判据落到别的窗口的容器上
   还 DROP 了那边的 schema,而四个信号(rc/collect/pass/skip)全都正常。
   所以:**每个包一个专属变量名 + 默认值指向自己**。本包 = ``DEFGEO_V5A_TEST_DSN``。

两种库
------
① ``chain_db(tag)`` —— 从「生产 dump + 防御 GEO 班列迁移」模板 **CLONE** 出来的
   一次性库。每条判据一把物理独立的库,残留不跨判据累积。
② ``fresh_db(tag)`` —— 空库,给需要自己装迁移的判据用。

🔴 迁移一律从 ``db.migration_manifest.MIGRATIONS`` 现扫,不手抄第二份清单。
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
    "DEFGEO_V5A_TEST_DSN",
    "postgresql://geo_admin:v5apass@localhost:55495/postgres")

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
    name = "defgeo_v5a_%s_%s_test" % (tag, uuid.uuid4().hex[:6])
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
    sql = sql.replace(_SEARCH_PATH_POISON, "-- [v5a] search_path poison neutralised")
    stripped = 0
    lines = []
    for ln in sql.splitlines():
        if ln.lstrip().startswith(("\\restrict", "\\unrestrict")):
            stripped += 1
            lines.append("-- [v5a] psql 元命令已剥离(第 %d 条)" % stripped)
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
            for tbl in ("quotes", "brands", "keyword_selection_sessions",
                        "client_access_tokens"):
                cur.execute("SELECT to_regclass(%s)", ("public." + tbl,))
                assert cur.fetchone()[0] is not None, "%s 不在 —— 判据会因错误的原因绿" % tbl
    finally:
        conn.close()
    yield name
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
