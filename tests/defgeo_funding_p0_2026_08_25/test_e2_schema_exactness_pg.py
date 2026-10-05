"""【E2-4 = Codex 二审 P2/门5】启动守卫要核的是「列**对不对**」,不是「列在不在」。

修之前:守卫只查 `information_schema.columns` 里 column_name 在不在。
于是一个**同名错类型**的列全程畅通:
  · `ADD COLUMN IF NOT EXISTS` 按**列名**判存 ⇒ 见到同名列直接 no-op,类型不纠正;
  · prestart 每次重放都"成功";
  · 守卫每次都放行;
  · 真正炸的地方在 `commit_freeze(user_id=...)` —— 那时钱已经在动了。

这与「CREATE INDEX IF NOT EXISTS 按名判存不绑表」是同一族:**IF NOT EXISTS 的
判存维度比你以为的窄**。

顺带钉住一条附加发现(Review 明令一并修):守卫原来两处
`information_schema.columns ... WHERE table_name='…'` **没带 `table_schema`**。
information_schema 是跨 schema 的,不带它等于问"任何 schema 里有没有一张同名表";
search_path 换个 schema,守卫就会拿别人的表给自己发通行证。
(同族:DROP INDEX 语法上不绑表,撞名会静默删掉无辜表的索引。)
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.defgeo_funding_p0_2026_08_25 import conftest as CT

pytestmark = pytest.mark.integration

TABLE = "diagnosis_runs"
COL = "payer_user_id"


def _run_guard(dsn):
    import os

    from services.startup_schema_guards import verify_diagnosis_schema_fail_closed

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        verify_diagnosis_schema_fail_closed()
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


@pytest.fixture()
def poisonable(migrated_dsn, live_server):
    """在**已迁移**的会话库上临时改一列,跑完原样改回去。

    在真库上改真列(不是造假 information_schema),因为要验的正是
    「PG 真的这么建了之后,守卫认不认得出来」。
    """
    _ = live_server
    conn = psycopg2.connect(migrated_dsn)
    conn.autocommit = True
    yield conn, migrated_dsn
    # 还原:类型 + 可空性都掰回去(顺序要紧 —— 先类型后可空)
    cur = conn.cursor()
    cur.execute("ALTER TABLE %s ALTER COLUMN %s TYPE integer USING %s::integer"
                % (TABLE, COL, COL))
    cur.execute("ALTER TABLE %s ALTER COLUMN %s DROP NOT NULL" % (TABLE, COL))
    cur.execute("SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
                (TABLE, COL))
    got = cur.fetchone()
    conn.close()
    assert got == ("integer", "YES"), "还原没到位(%r)—— 后面的判据会在脏库上跑" % (got,)


def test_e2_guard_rejects_a_same_name_column_of_the_wrong_type(poisonable):
    """🔴 工单点名的那一发毒:同名、错类型。

    把 `payer_user_id` 改成 text —— 列**还在**,所以旧守卫(只核列名)照样放行,
    而 `ADD COLUMN IF NOT EXISTS` 也不会把它纠正回来。
    """
    conn, dsn = poisonable
    conn.cursor().execute(
        "ALTER TABLE %s ALTER COLUMN %s TYPE text USING %s::text" % (TABLE, COL, COL))

    with pytest.raises(RuntimeError) as err:
        _run_guard(dsn)
    msg = str(err.value)
    assert COL in msg and "text" in msg, (
        "守卫抛了,但没点名是哪一列错成了什么类型:%s" % msg)


def test_e2_guard_rejects_the_payer_column_made_not_null(poisonable):
    """另一个方向:类型对、**可空性**错。

    本列的语义是「NULL = payer 就是 owner」,所以它要核的是**必须可空** ——
    与大多数列相反。建成 NOT NULL 会让存量每一行都需要一个本不该存在的值。
    """
    conn, dsn = poisonable
    cur = conn.cursor()
    cur.execute("UPDATE %s SET %s = owner_user_id WHERE %s IS NULL" % (TABLE, COL, COL))
    cur.execute("ALTER TABLE %s ALTER COLUMN %s SET NOT NULL" % (TABLE, COL))

    with pytest.raises(RuntimeError) as err:
        _run_guard(dsn)
    assert COL in str(err.value), str(err.value)


def test_e2_guard_still_lets_a_correctly_migrated_database_start(migrated_dsn, live_server):
    """🔴 配对的必须不命中:规格写窄了会让**正确的库**起不来。

    那比原来的洞更贵 —— 原来的洞是"错的能过",写窄了是"对的过不去",
    整个 fleet crash-loop。所以这一条与上面两条同等重要。
    """
    _ = live_server
    _run_guard(migrated_dsn)


def test_e2_every_schema_query_in_the_guard_is_schema_qualified():
    """机械枚举锁:守卫里每一条 `information_schema.columns` 查询都必须带
    `table_schema='public'`。

    分母是**从源码 AST 机械枚举**出来的,不是手抄的清单 ——
    将来新加一条查询忘了带,这里当场红(手写分母漏掉的那一项不会让任何判据变红)。

    谓词走 AST 而不是 grep:这些 SQL 是跨行的相邻字面量,
    Python 在解析期就把它们拼好了,grep 只能看到半截(我第一版就被这么骗过)。
    """
    import ast
    import inspect
    import textwrap

    import services.startup_schema_guards as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod)))
    queries = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "information_schema.columns" in n.value]
    assert len(queries) >= 3, (
        "只枚举到 %d 条 information_schema 查询 —— 分母塌了,这条锁在守空气" % len(queries))
    bad = [" ".join(q.split())[:80] for q in queries if "table_schema='public'" not in q]
    assert not bad, (
        "这些查询没带 table_schema='public' —— information_schema 是跨 schema 的,"
        "不带它等于问「任何 schema 里有没有同名表」:%r" % bad)


def test_e2_migration_050_refuses_a_same_name_column_of_the_wrong_type(migrated_dsn):
    """迁移 050 自己也要拒 —— 守卫拦的是启动,迁移拦的是"装都装不对"。

    两道各有各的窗口:守卫在 web/cron 起来时查,而 prestart 跑迁移**更早**。
    只有守卫的话,一个错类型库会先把迁移跑"成功"了再被守卫拦下 ——
    运维看到的是"迁移没问题、服务起不来",查错方向。
    """
    name = "defgeo_p0fix_mig050_" + uuid.uuid4().hex[:6] + "_test"
    assert "defgeo" in name and "test" in name, "unsafe test db name"
    admin = psycopg2.connect(CT._admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "%s"' % name)
    admin.close()
    dsn = CT.ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        # 造出「表在、同名列已经是错类型」的现场(手工建表 / 别的分支 / 回滚残留)
        cur.execute("CREATE TABLE diagnosis_runs (run_token text, payer_user_id text)")

        sql = open("db/migration_050_diagnosis_payer_identity_2026_08_25.sql",
                   encoding="utf-8").read()
        with pytest.raises(psycopg2.Error) as err:
            cur.execute(sql)
        assert "payer_user_id" in str(err.value), str(err.value)
        conn.close()
    finally:
        admin = psycopg2.connect(CT._admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
        admin.close()


def test_e2_migration_050_refuses_a_column_that_was_made_not_null(migrated_dsn):
    """迁移 050 也要拒 **NOT NULL** —— 类型对、可空性错的那一格。

    这一条是被撕锁逼出来的:把 050 里那句 `IF v_nullable <> 'YES'` 摘掉,
    全包 81 条**一条都不红**。我给守卫写了可空性判据,却忘了给迁移写 ——
    两道闸各有各的窗口(prestart 跑迁移比 web 起来更早),
    只守其中一道等于另一道从上线起没人验。
    """
    name = "defgeo_p0fix_mig050nn_" + uuid.uuid4().hex[:6] + "_test"
    assert "defgeo" in name and "test" in name, "unsafe test db name"
    admin = psycopg2.connect(CT._admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "%s"' % name)
    admin.close()
    dsn = CT.ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor()
        # 类型是对的(integer),错在**非空** —— 旧版自证只看类型就会放行。
        cur.execute("CREATE TABLE diagnosis_runs "
                    "(run_token text, payer_user_id integer NOT NULL)")
        sql = open("db/migration_050_diagnosis_payer_identity_2026_08_25.sql",
                   encoding="utf-8").read()
        with pytest.raises(psycopg2.Error) as err:
            cur.execute(sql)
        assert "NOT NULL" in str(err.value) or "payer_user_id" in str(err.value), str(err.value)
        conn.close()
    finally:
        admin = psycopg2.connect(CT._admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
        admin.close()


def test_e2_migration_050_is_idempotent_on_a_correct_column(migrated_dsn):
    """配对的必须不命中:列已经对的库上,050 必须能**反复重放**且不抛。

    prestart 每次部署无条件重放全部迁移 —— 自证块要是把正确的库也拒了,
    每一次部署都会卡在迁移这一步。
    """
    conn = psycopg2.connect(migrated_dsn)
    conn.autocommit = True
    sql = open("db/migration_050_diagnosis_payer_identity_2026_08_25.sql",
               encoding="utf-8").read()
    cur = conn.cursor()
    for _ in range(2):                    # 跑两遍,证明幂等不是"第一遍碰巧过"
        cur.execute(sql)
    conn.close()
