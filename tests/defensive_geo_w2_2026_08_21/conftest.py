"""窗B(WP3+WP4)判据底座 —— 真 PG16 一次性库。

安全栓沿用窗A(``tests/defensive_geo_2026_08_21/conftest.py``)的形态:
库名必须**同时**含 ``defgeo`` 与 ``test``。理由不是洁癖 ——
2026-08-15 实测过库名不含 ``test`` 会让安全栓整个失效,双臂 0 junit 假绿;
双树 A/B 又恰恰要给两臂各一个独立库,所以不能写死单个 URL。

schema 从哪来 —— **生产 pg_dump,不手写**
------------------------------------------
本包判据要打的是 ``keyword_selection_sessions`` × ``quote_pricing_snapshots``
的**复合外键**。手写一份精简 schema 就会漏掉
``quote_pricing_snapshots_owner_unique UNIQUE (id, quote_id)`` ——
而那条唯一约束正是复合 FK 能不能建起来的前提。漏了它判据会以
「FK 建不了」的形式假红,或更糟:改成单列 FK 后**假绿**。

🔴 pg_dump 的 ``search_path=''`` 是毒
   (本仓 2026-08-12 记过「pgdump fixture search_path 毒死后续迁移」):
   dump 第 15 行 ``set_config('search_path','',false)`` 会让**之后**所有
   不带 schema 限定的语句找不到表。本 conftest 装载前把该行中和掉,
   并在连接上显式 ``SET search_path``。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_THROWAWAY_URL = (
    "postgresql://geo_admin:testpw@localhost:55473/geo_defgeo_w2_test"
)

_REQUIRED_DB_TOKENS = ("defgeo", "test")

#: 生产 schema dump。**不是本包生成的**,是既有的真库快照。
PROD_SCHEMA = ROOT / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"

#: 本包引入的迁移。判据要证明它 additive、可重放、且真的挡住了跨 quote 错指。
MIGRATION_042 = (
    ROOT / "db" / "migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql"
)
MIGRATION_043 = ROOT / "db" / "migration_043_defgeo_activation_outbox_2026_08_21.sql"
#: 窗A 的两张表(040/041)。接线包的 CUR-01 判据要跨它们查链,所以一并装。
MIGRATION_040 = ROOT / "db" / "migration_040_defgeo_question_plans_2026_08_21.sql"
MIGRATION_041 = ROOT / "db" / "migration_041_defgeo_run_previews_2026_08_21.sql"

#: 🔴 [工单C 052 · 2026-08-25] **运行期前置**,不是本包拥有的迁移。
#:
#: 工单C 把 `activation_outbox.enqueue_activation` 改成在客户确认事务里
#: **冻结付款人身份**(payer_user_id / payer_funding_policy / payer_principal_kind
#: 三列,由 052 加)。本包的 `test_wp4_confirm_quote_wiring_pg` 走的正是那条
#: 生产确认路径 ⇒ 不装 052 就是 `UndefinedColumn` ⇒ 12 条红。
#: 那是"夹具没装起来",不是被测代码坏了(与 047/051 那两次同形)。
MIGRATION_052 = (
    ROOT / "db" / "migration_052_defgeo_activation_frozen_payer_2026_08_25.sql"
)

#: 判据依赖的迁移全集(含窗A 两张)。**按编号顺序**,041 依赖 040、052 依赖 043。
PACKAGE_MIGRATIONS = (MIGRATION_040, MIGRATION_041, MIGRATION_042, MIGRATION_043,
                      MIGRATION_052)

_SEARCH_PATH_POISON = "SELECT pg_catalog.set_config('search_path', '', false);"


def _resolve_url() -> str:
    configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
    dbname = configured.rsplit("/", 1)[-1].lower()
    missing = [t for t in _REQUIRED_DB_TOKENS if t not in dbname]
    if not configured or missing:
        raise RuntimeError(
            "窗B 判据锁死在本包一次性库上:库名必须同时含 {0};实得 {1!r}(缺 {2})。"
            "单跑用 {3}".format(
                _REQUIRED_DB_TOKENS, configured, missing, DEFAULT_THROWAWAY_URL
            )
        )
    return configured


EXACT_THROWAWAY_URL = _resolve_url()
os.environ["DATABASE_URL"] = EXACT_THROWAWAY_URL


def _load_prod_schema(conn) -> None:
    if not PROD_SCHEMA.exists():          # pragma: no cover - 环境问题,不是判据失败
        pytest.skip(f"缺生产 schema 快照:{PROD_SCHEMA}")
    sql = PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    if _SEARCH_PATH_POISON not in sql:
        # 形态变了要当场知道 —— 静默放过就等于把毒又吃回去。
        raise AssertionError(
            "生产 dump 里没找到预期的 search_path 毒行;dump 形态可能变了,"
            "请重新确认中和逻辑是否仍然必要。"
        )
    sql = sql.replace(_SEARCH_PATH_POISON, "-- [w2] search_path poison neutralised")

    # 🔴 [fix-of-fix ④ 2026-08-23] 第二处毒:pg_dump 17+ 会在文件头尾写 psql
    #    **元命令** ``\restrict <token>`` / ``\unrestrict <token>``。它们不是 SQL,
    #    psycopg2 直送服务端会得到 `syntax error at or near "\"`,整份 dump 一行都不装。
    #
    #    为什么本包一直没暴露:装载分支被 ``to_regclass(...) is not None`` 短路了 ——
    #    库已经预装好时根本走不到这里。**在干净库上一跑就是 29 errors**
    #    (`relation "public.quote_pricing_snapshots" does not exist`,
    #     因为 dump 没装成,迁移 042 的复合 FK 无表可指)。
    #    这正是「夹具没跑起来的绿不算绿」:绿的是"没跑",不是"跑通了"。
    #    剥离逻辑与窗C 的 ``w3c conftest`` 同源 —— 那边先修好并留了注释说本包缺这段。
    stripped = 0
    lines: list[str] = []
    for ln in sql.splitlines():
        if ln.lstrip().startswith(("\\restrict", "\\unrestrict")):
            stripped += 1
            lines.append("-- [w2 conftest] psql 元命令已剥离(第 %d 条)" % (stripped,))
        else:
            lines.append(ln)
    if stripped == 0:
        # 形态变了要当场知道:dump 换了版本、不再写元命令,这条剥离就成了死代码。
        raise AssertionError(
            f"生产 dump 里一条 psql 元命令都没有 —— 剥离规则可能已经过时:{PROD_SCHEMA}"
        )
    sql = "\n".join(lines)
    if "\\restrict" in sql or "\\unrestrict" in sql:
        raise AssertionError("psql 元命令没有被完全剥离 —— 剥离规则与 dump 形态不匹配")

    with conn.cursor() as cur:
        cur.execute(sql)


@pytest.fixture(scope="session")
def pg_url() -> str:
    return EXACT_THROWAWAY_URL


@pytest.fixture(scope="session")
def schema_loaded(pg_url: str) -> str:
    """把生产 schema + 迁移 042 装进一次性库。整个 session 只做一次。"""
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.keyword_selection_sessions')")
            already = cur.fetchone()[0] is not None
        if not already:
            _load_prod_schema(conn)
        with conn.cursor() as cur:
            for migration in PACKAGE_MIGRATIONS:
                cur.execute(migration.read_text(encoding="utf-8"))
    finally:
        conn.close()
    return pg_url


@pytest.fixture()
def conn(schema_loaded: str):
    """每条判据一个**回滚**连接 —— 判据之间零残留。

    2026-08-11 记过:变异留库残留会污染后续判据。所以这里恒 rollback,
    不给任何一条判据留下写进库里的东西。
    """
    c = psycopg2.connect(schema_loaded)
    # 🔴 与生产同构:``db/connection.py:57`` 把 cursor_factory 固定成
    #    RealDictCursor(行是 dict 不是 tuple)。夹具用裸 tuple cursor 时,
    #    按下标取值的代码在判据里全绿、真 HTTP 一打就 KeyError: 0 → 500。
    #    2026-08-21 实测踩到:report_binding 第一版就是这么挂的。
    #    夹具与生产不同构 = 判据恒绿,本仓早记过这个形态。
    c.cursor_factory = psycopg2.extras.RealDictCursor
    c.autocommit = False
    try:
        with c.cursor() as cur:
            cur.execute("SET search_path TO public")
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture()
def probe_rows(conn):
    """最小可用的 quote/session/snapshot 三元组 × 2 套(用于跨 quote 判据)。

    NOT NULL 列不是猜的:由 information_schema 现查后逐列填
    (``keywords_snapshot`` / ``expires_at`` 这两列就是这么发现的 ——
    手写夹具第一次跑漏了它们,整段判据以 aborted transaction 的形式假红)。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id,username,password_hash,display_name)"
            " VALUES (9001,'w2probe','x','W2') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO brands (id,name,owner_user_id)"
            " VALUES (9001,'W2ProbeBrand',9001) ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO quotes (id,brand_id) VALUES (9001,9001),(9002,9001)"
            " ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO keyword_selection_sessions"
            " (id,token,quote_id,brand_id,keywords_snapshot,expires_at) VALUES"
            " (9001,'w2tok1',9001,9001,'[]','2099-01-01'),"
            " (9002,'w2tok2',9002,9001,'[]','2099-01-01') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO quote_pricing_snapshots"
            " (id,quote_id,brand_id,selection_session_id,version,reason,"
            "  calculation_version,pricing_snapshot,snapshot_hash) VALUES"
            " (9001,9001,9001,9001,1,'probe','v1','{}'::jsonb, repeat('a',64)),"
            " (9002,9002,9001,9002,1,'probe','v1','{}'::jsonb, repeat('b',64))"
            " ON CONFLICT DO NOTHING"
        )
    return {"session_a": 9001, "session_b": 9002, "snap_a": 9001, "snap_b": 9002}


@pytest.fixture()
def probe_rows_committed(schema_loaded):
    """**已提交**的 quote/session/snapshot —— 并发判据要跨连接可见。

    与 :func:`probe_rows` 分开而不是合并:合并就得让那个 fixture 也 commit,
    于是每条判据都会往库里留残留(本仓 2026-08-11 记过残留污染后续判据)。
    这里自己负责收尾:测完把本组行删干净。
    """
    snap_id, quote_id, brand_id = 9101, 9101, 9101
    c = psycopg2.connect(schema_loaded)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("INSERT INTO users (id,username,password_hash,display_name)"
                        " VALUES (9101,'w2conc','x','W2C') ON CONFLICT DO NOTHING")
            cur.execute("INSERT INTO brands (id,name,owner_user_id)"
                        " VALUES (%s,'W2ConcBrand',9101) ON CONFLICT DO NOTHING",
                        (brand_id,))
            cur.execute("INSERT INTO quotes (id,brand_id) VALUES (%s,%s)"
                        " ON CONFLICT DO NOTHING", (quote_id, brand_id))
            cur.execute(
                "INSERT INTO keyword_selection_sessions"
                " (id,token,quote_id,brand_id,keywords_snapshot,expires_at)"
                " VALUES (9101,'w2conctok',%s,%s,'[]','2099-01-01')"
                " ON CONFLICT DO NOTHING", (quote_id, brand_id))
            cur.execute(
                "INSERT INTO quote_pricing_snapshots"
                " (id,quote_id,brand_id,selection_session_id,version,reason,"
                "  calculation_version,pricing_snapshot,snapshot_hash)"
                " VALUES (%s,%s,%s,9101,1,'probe','v1','{}'::jsonb, repeat('a',64))"
                " ON CONFLICT DO NOTHING", (snap_id, quote_id, brand_id))
        yield (snap_id, quote_id, brand_id)
    finally:
        # 🔴 只清 outbox。**不删** quote_pricing_snapshots ——
        #    生产装了 BEFORE UPDATE OR DELETE 不可变触发器
        #    (reject_quote_pricing_snapshot_mutation),DELETE 会被它拒绝。
        #    那是真不变式,判据不该为了收尾去跟它打架。
        #    其余脚手架行留着无害:一次性库 + 全部 ON CONFLICT DO NOTHING,
        #    重跑幂等;真正会污染下一条判据的只有 outbox 行。
        with c.cursor() as cur:
            cur.execute("DELETE FROM defgeo_activation_outbox WHERE quote_id=%s",
                        (quote_id,))
        c.close()
