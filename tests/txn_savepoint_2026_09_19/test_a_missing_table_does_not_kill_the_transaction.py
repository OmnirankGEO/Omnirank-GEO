# -*- coding: utf-8 -*-
"""WO_242 · 缺表 SELECT 不许再把整个事务吞掉。

生产事实(PG 容器日志,台账 09-19 13:20):
`services/quote_numeric_repair.py` 查不存在的 `quote_packages` → PG 把事务判 aborted
→ 紧接着的 `SELECT 1 FROM keyword_selection_sessions` 与
**`UPDATE quotes SET total_articles=…` 一起静默失败**(09-18 一天 7 次)
→ 外层 `except` 只 rollback + warning,页面照常 200
→ **修复路径三个月零写入**(quote 379 的 `total_articles` 至今 0)。

🔴 **本包必须跑在真 PostgreSQL 上。**
   假 cursor 复现不出「事务被判 aborted」——
   那正是本单要修的那个机制本身。用假 cursor 写的判据会全绿,
   而它证明的是「我的假对象按我想的那样工作」,不是「PG 里这事不再发生」。
   (本仓 an-unrealistic-fixture-hides-the-defect-the-poison-should-catch。)
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    """让被测模块自己把 schema 建起来 —— 用**它真实依赖的那套**,不是我手搓的。

    🔴 第一版我 `DROP TABLE quotes` 再自己 `CREATE`,结果:
       ① `db.diagnosis_db` 在 import 期就会跑 `init_db()` 把整套表建出来,
          我的手搓表和它打架(外键依赖导致 DROP 失败);
       ② 我还顺手 drop 了 `keyword_selection_sessions` —— 而**生产里那张表是有的**,
          它失败纯粹是因为事务已经被前一条打废。
       把它 drop 掉等于**把夹具构造成了另一个缺陷**,而那个缺陷不是本单要修的。
    """
    import db.diagnosis_db  # noqa: F401  —— 导入即建表(生产同一条路径)
    yield


@pytest.fixture()
def conn():
    if not DSN:
        pytest.fail("TEST_DATABASE_URL 未设置 —— 本包必须连真库,不许退化成假 cursor")
    c = psycopg2.connect(DSN)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    cur = c.cursor()
    # 🔴 唯一要复现的生产事实:`quote_packages` **不存在**(to_regclass = NULL)。
    #    其余表一律保持真实 schema。
    cur.execute("DROP TABLE IF EXISTS quote_packages")
    c.commit()
    yield c
    try:
        c.close()
    except Exception:
        pass


def test_the_missing_table_is_actually_missing(conn):
    """🔴 前提自检:`quote_packages` 真的不在库里。

    它要是被别的东西建出来了,本包每一条都会"通过"而什么都没验。
    """
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('quote_packages') AS t")
    assert cur.fetchone()["t"] is None, "quote_packages 居然存在 —— 本包失去了被测对象"


def test_the_fixture_really_reproduces_the_abort(conn):
    """🔴 仪器自检:先证明「不加保护时事务真的会被打废」。

    没有这一条,下面那条「UPDATE 成功了」可能只是因为**这个环境里根本不会 abort**
    (比如连接是 autocommit)—— 那样它证明不了任何事。
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM quote_packages")
    except Exception:
        pass
    with pytest.raises(Exception) as ei:
        cur.execute("SELECT 1")
    assert "aborted" in str(ei.value).lower(), (
        "这个环境里缺表 SELECT 没有把事务判废(实际报错:%s)—— "
        "那么本包下面的判据都失去了参照物" % ei.value)
    conn.rollback()


def test_a_missing_table_no_longer_kills_the_later_update(conn):
    """🔴 本单的全部意义:缺表查询之后,`UPDATE quotes` 必须真的写进去。"""
    cur = conn.cursor()
    cur.execute("DELETE FROM quotes WHERE id = %s", (379,))
    cur.execute(
        "INSERT INTO quotes (id, tier, monthly_price, total_articles, markdown)"
        " VALUES (%s,%s,%s,%s,%s)",
        (379, "standard", 0, 0, "标准版 ¥4800/月 共 12 篇文章"),
    )
    conn.commit()

    from services.quote_numeric_repair import repair_quote_numeric_fields
    result = repair_quote_numeric_fields(379)

    check = psycopg2.connect(DSN)
    check.cursor_factory = psycopg2.extras.RealDictCursor
    ck = check.cursor()
    ck.execute("SELECT monthly_price, total_articles FROM quotes WHERE id = 379")
    row = ck.fetchone()
    check.close()

    assert row["total_articles"] == 12, (
        "UPDATE 没落库(读回 %s)—— 缺表查询仍然打废了事务" % (row,))
    assert result["updated"] is True, result


def test_the_swallow_helper_leaves_the_connection_usable(conn):
    """🔴 helper 的核心不变式:里面失败了,**外面还能继续用**。

    与裸 `try/except` 的区别只有这一条,而那一条就是本单的全部。
    """
    from db.txn_guard import swallow
    cur = conn.cursor()
    with swallow(cur, "sp_probe", where="test"):
        cur.execute("SELECT * FROM 这张表不存在")
    cur.execute("SELECT 1 AS ok")           # 不抛即通过
    assert cur.fetchone()["ok"] == 1


def test_a_bare_try_does_not(conn):
    """🔴 反向对照:同样的失败,裸 `try/except` 之后连接是**废的**。

    没有这一条,上面那条可能只是"这个环境本来就不会坏"。
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM 这张表也不存在")
    except Exception:
        pass
    with pytest.raises(Exception):
        cur.execute("SELECT 1")
    conn.rollback()


def test_swallow_degrades_cleanly_under_autocommit():
    """🔴 autocommit 下 `SAVEPOINT` 无事务可依附会直接报错。

    池化连接可能被前序只读/DDL 调成 autocommit
    (`api/brand_api.py:615-616` 那条注释说的就是这件事),
    那时 helper 必须降级成裸 try,而不是自己 500。
    """
    if not DSN:
        pytest.fail("TEST_DATABASE_URL 未设置")
    from db.txn_guard import swallow
    c = psycopg2.connect(DSN)
    c.autocommit = True
    c.cursor_factory = psycopg2.extras.RealDictCursor
    cur = c.cursor()
    try:
        with swallow(cur, "sp_auto", where="test-autocommit"):
            cur.execute("SELECT * FROM 这张表不存在")
        cur.execute("SELECT 1 AS ok")
        assert cur.fetchone()["ok"] == 1
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════
# 重复实现登记 —— 这个 helper 仓内此前已有三份手写版
# ══════════════════════════════════════════════════════════════════

#: 🔴 先把事实说准(第一版这条锁写错了,当场被自己判红,已订正):
#: `SAVEPOINT` 在本仓是**既有常用手法** —— 41 个生产文件里都有,就地 execute
#: 完全正常,**不是**重复实现。本锁只管一种稀有形态:**把它包成上下文管理器的 helper**。
#: 改动前全仓只有一处(`settlement_adjudicator._savepoint`),本模块照抄它。
#: 登记 ≠ 豁免:它在资金路径上,收敛要单独评审;漏登记会红。
KNOWN_CM_HELPERS = {
    "services/settlement_adjudicator.py": "_savepoint()",
    "db/txn_guard.py": "savepoint() / swallow()(本单新增的规范出处)",
}


def _contextmanager_savepoint_helpers():
    """全仓找「同一个函数里既有 SAVEPOINT 又有 yield」的 helper。"""
    import ast
    found = {}
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", "scripts/")) or "node_modules" in rel:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"), rel)
        except Exception:
            continue
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            src = ast.unparse(fn)
            if "SAVEPOINT" in src.upper() and any(isinstance(n, ast.Yield)
                                                  for n in ast.walk(fn)):
                found.setdefault(rel, []).append(fn.name)
    return found


def test_the_registered_contextmanager_helper_still_exists():
    """🔴 登记项必须还在 —— 登记一条不存在的东西会让「有登记」变成空话。"""
    found = _contextmanager_savepoint_helpers()
    for rel in KNOWN_CM_HELPERS:
        assert rel in found, "登记了 %s 的 savepoint 上下文管理器,但它已经不在了 —— 该删登记" % rel


def test_no_fourth_contextmanager_savepoint_helper_appears():
    """🔴 分母锁:再包一份同形 helper 且未登记 ⇒ 红。

    ⚠️ 只管**上下文管理器形态**。就地 `cur.execute("SAVEPOINT …")` 是本仓既有
    常用手法(41 个文件),不在本锁范围 —— 第一版我把它一起收了,
    于是这条锁要求登记 37 个与本单无关的文件。**那不是锁,是噪声**,已收窄。
    """
    found = _contextmanager_savepoint_helpers()
    extra = sorted(set(found) - set(KNOWN_CM_HELPERS))
    assert not extra, (
        "出现了未登记的 savepoint 上下文管理器:%s —— 要么改用 db/txn_guard,"
        "要么登记并写明为什么不能用" % (extra,))


def test_savepoint_under_autocommit_reraises_the_original_error():
    """🔴 autocommit 下 `savepoint()` 必须抛**原来那个异常**,不是保存点机制的异常。

    注毒发现的缺口:把 autocommit 降级删掉(`nested` 恒 True)后,
    上面那条 `test_swallow_degrades_cleanly_under_autocommit` **仍然绿** ——
    因为 autocommit 下本来就没有事务可污染,「连接还能用」两种写法都成立,
    **那条判据对这个改动天生没有分辨力**。

    真正会变的是这里:`nested` 恒 True 时,except 分支会去跑
    `ROLLBACK TO SAVEPOINT`,而该保存点根本不存在 ⇒ 调用方收到的是
    `InvalidSavepointSpecification`,**原始错误被机制的错误顶掉了**。
    排查的人看到的是"保存点不存在",而真因是"那张表不存在"。
    """
    if not DSN:
        pytest.fail("TEST_DATABASE_URL 未设置")
    from db.txn_guard import savepoint
    c = psycopg2.connect(DSN)
    c.autocommit = True
    cur = c.cursor()
    try:
        with pytest.raises(Exception) as ei:
            with savepoint(cur, "sp_direct"):
                cur.execute("SELECT * FROM 这张表不存在")
        msg = str(ei.value).lower()
        assert "savepoint" not in msg, (
            "抛出来的是保存点机制的错误而不是原始错误:%s —— "
            "autocommit 降级没了,真因会被顶掉" % ei.value)
    finally:
        c.close()


# ══════════════════════════════════════════════════════════════════
# 会话探测 fail-closed(Review 2026-09-19 裁定)
# ══════════════════════════════════════════════════════════════════

def _seed_quote(conn, qid=381):
    cur = conn.cursor()
    cur.execute("DELETE FROM quotes WHERE id = %s", (qid,))
    cur.execute(
        "INSERT INTO quotes (id, tier, monthly_price, total_articles, markdown)"
        " VALUES (%s,%s,%s,%s,%s)",
        (qid, "standard", 0, 0, "标准版 ¥4800/月 共 12 篇文章"),
    )
    conn.commit()
    return qid


def _read_back(qid):
    c = psycopg2.connect(DSN)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    k = c.cursor()
    k.execute("SELECT monthly_price, total_articles FROM quotes WHERE id = %s", (qid,))
    row = k.fetchone()
    c.close()
    return row


def test_a_failing_session_probe_must_not_write(conn, monkeypatch):
    """🔴 探测语句**真的**报错 ⇒ 不落库(fail-closed)。

    🔴 第一版夹具是错的,注毒当场抓到:我包了 `swallow` 并在 `yield` **之前**
       执行失败语句 ⇒ `__enter__` 抛异常 ⇒ 整个函数被**外层** except 接走、
       提前返回,`UPDATE` 根本没走到。于是这条判据**无论 fail-open 还是
       fail-closed 都绿** —— 它证明的是"函数崩了",不是"它选择不写"。
       (毒「会话探测退回 fail-open」存活,就是这么被发现的。)

    改法:注入点下沉到**那条 SQL 本身** —— 把探测语句指向一张不存在的表,
    于是它是一次**真的 PG UndefinedTable**,事务真的被判废,
    SAVEPOINT 真的要把它救回来,然后 fail-closed 才决定写不写。
    """
    qid = _seed_quote(conn, 381)
    assert _read_back(qid)["total_articles"] == 0

    import db.diagnosis_db as ddb
    real_connect = ddb.get_connection

    class _Cur:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, k):
            return getattr(self._real, k)

        def execute(self, sql, params=None):
            if "keyword_selection_sessions" in sql:
                sql = sql.replace("keyword_selection_sessions", "表_不存在_探测失败")
            return self._real.execute(sql, params)

    class _Conn:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, k):
            return getattr(self._real, k)

        def cursor(self, *a, **kw):
            return _Cur(self._real.cursor(*a, **kw))

    monkeypatch.setattr(ddb, "get_connection", lambda *a, **kw: _Conn(real_connect()))
    from services.quote_numeric_repair import repair_quote_numeric_fields
    repair_quote_numeric_fields(qid)

    after = _read_back(qid)
    assert after["total_articles"] == 0, (
        "探测失败时仍然落库了(读回 %s)—— fail-closed 没生效" % (after,))


def test_a_healthy_probe_with_no_session_still_writes(conn):
    """🔴 反向对照:探测正常且无会话 ⇒ **照常写**。

    少了这一条,上面那条可以靠「永远不写」通过 —— 那等于把修复路径整个关掉。
    """
    qid = _seed_quote(conn, 382)
    from services.quote_numeric_repair import repair_quote_numeric_fields
    result = repair_quote_numeric_fields(qid)
    after = _read_back(qid)
    assert after["total_articles"] == 12, (
        "探测正常、无会话,却没写(读回 %s)—— 修复路径被关死了" % (after,))
    assert result["updated"] is True, result
