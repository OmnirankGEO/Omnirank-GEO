"""窗D 判据底座 —— 真 PG16 一次性库。

沿用窗A/B/C 的安全栓:库名必须**同时**含 ``defgeo`` 与 ``test``。
2026-08-15 实测过库名不含 ``test`` 会让安全栓整个失效、双臂 0 junit 假绿。

🔴 本包迁移**不许跳过**
----------------------
跑不起来 = 判据没有被测对象。宁可当场炸,也不 skip ——
skip 掉的那条判据在报告里和"通过了"长得一模一样。

🔴 冷库自检(判据需要冷库时必须明说)
------------------------------------
本包的 PG16 判据全部打在**结构约束**上(唯一约束 / 触发器 / CHECK),
不依赖任何存量业务数据。所以这里建的是**空库 + 只装 046**:

* 装生产 dump 反而有害 —— 046 的五张表对既有表零 FK,装了 dump 只会
  让"这条 CHECK 到底是被谁挡住的"变得不唯一;
* 每次会话 ``DROP SCHEMA public CASCADE`` 重建,保证**冷库** ——
  上一轮变异留下的残留行会污染后续判据(本仓记过)。

:func:`_assert_migration_actually_ran` 是活性自证:装完必须能查到五张表,
查不到就当场炸,而不是让后面的判据在空库上"全绿"。
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_THROWAWAY_URL = (
    "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_w4_test"
)

_REQUIRED_DB_TOKENS = ("defgeo", "test")

#: 🔴 **本包拥有的**迁移。轴的作用域 = 本包的作用域。
#:    046 的五张表对既有表零 FK,所以它在**空库**上就装得起来 ——
#:    这一点由 :func:`test_migration_046_applies_on_an_empty_database` 亲自证。
PACKAGE_OWNED_MIGRATIONS = (
    "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
)

EXPECTED_TABLES = (
    "defgeo_monitoring_attempts",
    "defgeo_monitoring_defensive_projections",
    "defgeo_report_snapshots",
    "defgeo_renewal_recommendations",
    "defgeo_xiaobang_frozen_reasons",
)


def _url() -> str:
    url = os.getenv("DEFGEO_W4_TEST_DB_URL") or DEFAULT_THROWAWAY_URL
    dbname = url.rsplit("/", 1)[-1].lower()
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in dbname]
    if missing:
        raise RuntimeError(
            f"判据库名 {dbname!r} 缺 {missing} —— 安全栓要求库名同时含 "
            "'defgeo' 与 'test'。2026-08-15 实测:库名不含 test 时安全栓整个失效,"
            "双臂 0 junit 也会显示成绿。"
        )
    return url


def _read_sql(rel: str) -> str:
    with io.open(ROOT / rel, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _assert_migration_actually_ran(cur) -> None:
    """活性自证:装完必须真的有那五张表。

    没有这一条,任何"在空库上跑的判据"都会全绿 —— 因为它们要打的对象
    压根不存在,而 ``CREATE TABLE IF NOT EXISTS`` 不会告诉你它没跑。
    """
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
        "AND tablename = ANY(%s)", (list(EXPECTED_TABLES),))
    got = {r["tablename"] for r in cur.fetchall()}
    missing = sorted(set(EXPECTED_TABLES) - got)
    if missing:
        raise RuntimeError(
            f"迁移 046 装完却查不到表 {missing} —— 判据没有被测对象。"
            "这一条炸了比后面一片绿有用。"
        )


#: 🔴 [工单 V3-C · C-4] 现役表,**不是**本包的被测对象。
#:
#:   ``card_producer.occurrence_rate`` 走账本的 ``monitoring_result_id``
#:   回连现役 ``monitoring_results``(§13.1「保留现役 raw result」)。
#:   不建它的话,出现率那条链在本底座上永远走 ``guarded()`` 的留白分支 ——
#:   于是"绑没绑冻结 raw_result_ids"这件事在本包**测不到**,判据恒绿。
#:
#: 🔴 如实措辞:这是**列的子集**(只含被测链真正读写的那三列),
#:   不是生产 DDL 的精确副本;没有任何判据拿它当分母。
_PREREQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS public.monitoring_results (
    id          SERIAL PRIMARY KEY,
    task_id     INTEGER,
    is_detected INTEGER
);
"""


@pytest.fixture(scope="session")
def w4_db():
    conn = psycopg2.connect(_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        # 冷库:上一轮的残留行会污染后续判据(本仓记过)。
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        for rel in PACKAGE_OWNED_MIGRATIONS:
            cur.execute(_read_sql(rel))
        cur.execute(_PREREQ_SCHEMA)
        _assert_migration_actually_ran(cur)
    yield conn
    conn.close()


@pytest.fixture
def cur(w4_db):
    with w4_db.cursor() as c:
        yield c


#: 迁移体内 DML 普查用的正则。**结构锚**:匹配语句开头的动词,
#: 不裸匹配单词 —— 注释里出现 "INSERT" 三个字母不该算违规。
DML_STATEMENT = re.compile(
    r"(?im)^\s*(INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|TRUNCATE|COPY\s+\w)")


def strip_sql_comments(sql: str) -> str:
    """去掉 ``--`` 行注释与 ``/* */`` 块注释,再做 DML 普查。

    不去注释的话,迁移里那句「体内零 DML」的说明本身会被判成违规 ——
    本仓记过「引用裁决原文会让裸串结构锚判红」。
    """
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    return re.sub(r"--[^\n]*", "", sql)
