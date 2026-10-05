# -*- coding: utf-8 -*-
"""WO_285b · 路由可达的函数里,不许有无条件执行的 `ALTER TABLE …`。

病灶(WO_284 / WO_285):`ALTER TABLE … ADD COLUMN IF NOT EXISTS` 先拿 ACCESS EXCLUSIVE 再判断 IF NOT EXISTS,
列早已存在也要锁;发车 pg_dump 对每张表持 ACCESS SHARE ⇒ 请求路径上的这类语句排在 dump 后面、lock_timeout=0 一直等、
同步调用卡住事件循环、WORKERS=1 全应用停。WO_285 修了 advisor 那一处;本包管全仓。

判据:
  ① 结构(静态调用图,ddl_graph.py):从每个路由处理函数出发、不穿过 once-guard 函数能走到的函数里,
     每个含 ALTER TABLE 的字面量都必须是「有条件的」——
       · 在 Python `if` 分支里(先查目录),或同函数前面有 `if …: return` 的提前返回(如 `if column_exists(...): return`);
       · 或是 `DO $$ … IF … THEN ALTER … $$` 这种库内判断;
     否则红。图是**下界**(不解析 getattr / 字符串派发 / 类方法);自证格保证它确实解析出了已知的链。
  ② 牙证 / 对照:临时目录里造一个迷你仓 —— 路由经模块别名调到无条件 ALTER ⇒ 必须被抓;
     if 包住 / 提前返回 / once-guard / DO-IF 四种 ⇒ 必须不报。
  ③ 行为(真 PG,测试库):db/schema_guard 的三个函数在「对象已存在」时**不执行任何 DDL** ——
     另一会话像 pg_dump 一样持表的 ACCESS SHARE,本会话 lock_timeout=1s 调它们,必须立即返回;
     对象缺失 / 不一致时才真的执行(同样调用,在无锁时生效)。
"""
from __future__ import annotations

import os
import pathlib
import sys
import textwrap
import time
import uuid

import pytest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import ddl_graph  # noqa: E402

ALLOWED = {}  # 「函数键:行号」→ 原因。目前一条都没有;要加必须写原因。


@pytest.fixture(scope="module")
def graph():
    return ddl_graph.build(REPO)


def test_graph_resolves_known_chains(graph):
    # [开源 E3 · 2026-09-28] 下限 1500 → 1300:E3(E3a / B1b / B3a-c / B4)删了几百条端点,本图实测
    #   E3 前 308330514 = 1865 → 9ae1c8ac7 = 1433(B2 不再变)。这格验的是「图没坏」不是「路由数不许少」:
    #   留约一成余量,删端点的片不必每次来调;真掉到 1300 以下先查图有没有解析坏,再核是不是又删了一批。
    assert graph["routes"] > 1300, f"只认出 {graph['routes']} 个路由处理函数,图坏了"
    assert graph["edges"] > 10000, f"只解析出 {graph['edges']} 条调用边,图坏了"
    reached = {f"{m}.{n}" for m, n in graph["reached"]}
    for must in ("db.fund_recovery_db.init_fund_recovery_tables",       # server.api_start_articles → create_recovery_order → …
                 "db.diagnosis_db.init_db",                              # server.list_employee_configs → …
                 "services.organization_short_code.ensure_schema"):      # api.organization_api.get_short_code → …
        assert must in reached, f"已知的请求路径链没解析出来:{must}"
    assert any(h["conditional"] for h in graph["hot"]), "一条有条件的 ALTER 都没看见 —— 扫描没覆盖"


def test_no_unconditional_alter_on_a_request_path(graph):
    bad = [h for h in graph["hot"] if not h["conditional"] and f"{h['key']}:{h['line']}" not in ALLOWED]
    assert not bad, ("以下 ALTER 在路由可达、无 once-guard 的函数里无条件执行 —— 发车 pg_dump 期间会把整个应用卡住。"
                     "改用 db/schema_guard 的函数(先查目录),或放进 once-guard 函数 / 迁移:\n  "
                     + "\n  ".join(f"{h['key']}:{h['line']}  {h['sql']}  ← {h['route']}" for h in bad))


_MINI = {
    "app_api.py": '''
        from fastapi import APIRouter
        import mini_db as mdb
        from mini_db import guarded_init, if_init, early_init, do_init
        router = APIRouter()

        @router.get("/a")
        def a():
            mdb.bad_init()        # 经模块别名 —— 必须被解析、被抓

        @router.get("/b")
        def b():
            guarded_init(); if_init(); early_init(); do_init(); mdb.const_init(); mdb.comment_init()
            mdb.positional_init("CREATE x")
    ''',
    "mini_db.py": '''
        _READY = False
        _BAD_CONST = ("CREATE INDEX IF NOT EXISTS i ON t(c)", "ALTER TABLE t DROP CONSTRAINT IF EXISTS k")
        _COMMENT_ONLY = """CREATE TABLE IF NOT EXISTS t (c TEXT) -- 以前这里用 ALTER TABLE 补过列"""

        def const_init():
            for s in _BAD_CONST:          # 模块级常量里的 ALTER,在循环里无条件执行 —— 必须被抓
                cur.execute(s)

        def comment_init():
            cur.execute(_COMMENT_ONLY)    # 只在 SQL 注释里提到 ALTER —— 不算

        def positional_init(sql):
            if sql.startswith("CREATE"):  # 按位置的 if,不是在问目录 —— 必须被抓
                cur.execute("ALTER TABLE t DROP CONSTRAINT IF EXISTS p")

        def bad_init():
            cur.execute("ALTER TABLE t ADD COLUMN IF NOT EXISTS c TEXT")

        def guarded_init():
            global _READY
            if _READY:
                return
            cur.execute("ALTER TABLE t ADD COLUMN IF NOT EXISTS g TEXT")
            _READY = True

        def if_init():
            if not exists():
                cur.execute("ALTER TABLE t ADD COLUMN i TEXT")

        def early_init():
            if exists():
                return
            cur.execute("ALTER TABLE t ADD COLUMN e TEXT")

        def do_init():
            cur.execute("""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='k') THEN
                ALTER TABLE t ADD CONSTRAINT k CHECK (c <> ''); END IF; END $$;""")
    ''',
}


def test_teeth_and_controls_on_a_mini_repo(tmp_path):
    for name, src in _MINI.items():
        (tmp_path / name).write_text(textwrap.dedent(src), encoding="utf-8")
    g = ddl_graph.build(tmp_path)
    flagged = sorted(h["key"] for h in g["hot"] if not h["conditional"])
    quiet = sorted(h["key"] for h in g["hot"] if h["conditional"])
    assert flagged == ["mini_db.bad_init", "mini_db.const_init", "mini_db.positional_init"], \
        f"牙证:该抓 bad_init / const_init / positional_init,实抓 {flagged}"
    assert quiet == ["mini_db.do_init", "mini_db.early_init", "mini_db.if_init"], f"对照:{quiet}"
    keys = {h["key"] for h in g["hot"]}
    assert "mini_db.guarded_init" not in keys, "once-guard 函数不该进「每请求」集合"
    assert "mini_db.comment_init" not in keys, "只在 SQL 注释里提到 ALTER 的不该算"


# ---------------------------------------------------------------------------
# ③ 真 PG:schema_guard 在对象已存在时不执行 DDL(持锁时立即返回)
# ---------------------------------------------------------------------------

@pytest.fixture
def pg():
    dsn = os.environ.get("TEST_DATABASE_URL") or ""
    if "test" not in dsn:
        pytest.skip("需要名字里带 test 的测试库")
    import psycopg2

    table = f"wo285b_probe_{uuid.uuid4().hex[:8]}"
    setup = psycopg2.connect(dsn)
    setup.autocommit = True
    with setup.cursor() as c:
        c.execute(f"CREATE TABLE {table} (id INT, status TEXT, name VARCHAR(20), "
                  f"CONSTRAINT {table}_status_check CHECK (status IN ('a','b')))")
    try:
        yield dsn, table
    finally:
        with setup.cursor() as c:
            c.execute(f"DROP TABLE IF EXISTS {table}")
        setup.close()


def _held(dsn, table, fn):
    """另一会话持 ACCESS SHARE(pg_dump 那把),本会话 lock_timeout=1s 调 fn(cur);返回 (耗时, 结果或异常名)。"""
    import psycopg2

    a, b = psycopg2.connect(dsn), psycopg2.connect(dsn)
    try:
        with a.cursor() as ca:
            ca.execute(f"LOCK TABLE {table} IN ACCESS SHARE MODE")
        t0 = time.time()
        with b.cursor() as cb:
            cb.execute("SET LOCAL lock_timeout = '1s'")
            try:
                out = fn(cb)
            except psycopg2.errors.LockNotAvailable:
                out = "LockNotAvailable"
        return time.time() - t0, out
    finally:
        b.rollback()
        a.rollback()
        a.close()
        b.close()


def test_schema_guard_runs_no_ddl_when_the_object_already_matches(pg):
    dsn, table = pg
    sys.path.insert(0, str(REPO))
    from db import schema_guard as sg

    cases = [
        ("add_column_if_missing(已有列)", lambda c: sg.add_column_if_missing(c, table, "status", "TEXT")),
        ("replace_in_list_check_if_changed(集合相同)",
         lambda c: sg.replace_in_list_check_if_changed(c, table, f"{table}_status_check", "status", ["b", "a"])),
        ("alter_column_type_if_changed(类型相同)",
         lambda c: sg.alter_column_type_if_changed(c, table, "name", "VARCHAR(20)", "character varying(20)")),
    ]
    for label, fn in cases:
        secs, out = _held(dsn, table, fn)
        assert out is False and secs < 0.5, f"{label}:持锁时应立即返回 False(不执行 DDL),实为 {out!r} / {secs:.2f}s"
    # 对照:原来那种无条件写法在同样持锁下会被挡(证明「持锁」这把尺子是真的)
    secs, out = _held(dsn, table, lambda c: c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS status TEXT"))
    assert out == "LockNotAvailable", f"对照失效:无条件 ALTER 持锁时应被挡,实为 {out!r}"


def test_schema_guard_applies_ddl_when_the_object_is_missing_or_different(pg):
    import psycopg2

    dsn, table = pg
    sys.path.insert(0, str(REPO))
    from db import schema_guard as sg

    c = psycopg2.connect(dsn)
    c.autocommit = True
    try:
        with c.cursor() as cur:
            assert sg.add_column_if_missing(cur, table, "extra", "TEXT") is True
            assert sg.column_exists(cur, table, "extra")
            assert sg.replace_in_list_check_if_changed(cur, table, f"{table}_status_check", "status", ["a", "b", "c"]) is True
            assert set(__import__("re").findall(r"'([^']*)'", sg.constraint_def(cur, table, f"{table}_status_check"))) == {"a", "b", "c"}
            assert sg.alter_column_type_if_changed(cur, table, "name", "VARCHAR(40)", "character varying(40)") is True
            assert sg.column_type(cur, table, "name") == "character varying(40)"
            # 再调一次 ⇒ 都已一致 ⇒ 都不执行
            assert sg.add_column_if_missing(cur, table, "extra", "TEXT") is False
            assert sg.replace_in_list_check_if_changed(cur, table, f"{table}_status_check", "status", ["c", "b", "a"]) is False
            assert sg.alter_column_type_if_changed(cur, table, "name", "VARCHAR(40)", "character varying(40)") is False
    finally:
        c.close()


def _missing_cases(sg, table):
    """三个 helper 各一种「对象缺失 / 不一致 ⇒ 真要跑 DDL」的调用。"""
    return [
        ("add_column_if_missing(缺列)", lambda c: sg.add_column_if_missing(c, table, "extra", "TEXT"),
         lambda c: not sg.column_exists(c, table, "extra")),
        ("replace_in_list_check_if_changed(值集不同)",
         lambda c: sg.replace_in_list_check_if_changed(c, table, f"{table}_status_check", "status", ["a", "b", "z"]),
         lambda c: "'z'" not in (sg.constraint_def(c, table, f"{table}_status_check") or "")),
        ("alter_column_type_if_changed(类型不同)",
         lambda c: sg.alter_column_type_if_changed(c, table, "name", "VARCHAR(40)", "character varying(40)"),
         lambda c: sg.column_type(c, table, "name") == "character varying(20)"),
    ]


def test_schema_guard_ddl_gives_up_within_its_own_lock_timeout_when_the_table_is_held(pg, monkeypatch):
    """[WO_285b 补 · Review 🟡①] 对象真缺失、恰好赶上备份窗口时,helper 自己的 DDL 也不许无限排队。

    调用方自己的 lock_timeout 设成 10s(比 helper 的 2s 长):helper 必须在 5s 内放弃、返回 False、不抛,
    对象保持原样;调用方的事务仍可用,lock_timeout 恢复成调用方原来的 10s。事务连接与 autocommit 连接各验一遍。
    """
    import psycopg2

    dsn, table = pg
    sys.path.insert(0, str(REPO))
    from db import schema_guard as sg

    # [ONESHOT 提速] 6 次调用原先每次都等满 helper 的真 2s ≈ 12s。第 1 次保留生产值 2s,并加下限断言
    #   (确实等了 helper 自己的时限,不是 0 也不是调用方的 10s);其余 5 次把 helper 时限临时调成 200ms,
    #   验的仍是「helper 用自己的时限放弃、不继承调用方的 10s」,只是等得短。
    assert sg.DDL_LOCK_TIMEOUT == "2s", "生产 helper 时限变了 —— 下面第 1 次的上下限要跟着改"
    first = True
    for autocommit in (False, True):
        for label, call, unchanged in _missing_cases(sg, table):
            real = first
            first = False
            monkeypatch.setattr(sg, "DDL_LOCK_TIMEOUT", "2s" if real else "200ms")
            holder, caller = psycopg2.connect(dsn), psycopg2.connect(dsn)
            caller.autocommit = autocommit
            try:
                with holder.cursor() as h:
                    h.execute(f"LOCK TABLE {table} IN ACCESS SHARE MODE")
                with caller.cursor() as c:
                    c.execute("SET lock_timeout = '10s'" if autocommit else "SET LOCAL lock_timeout = '10s'")
                    t0 = time.time()
                    out = call(c)
                    secs = time.time() - t0
                    where = f"{label} · {'autocommit' if autocommit else '事务内'}"
                    assert out is False and secs < 5, f"{where}:持锁时应在 helper 自己的时限内放弃,实为 {out!r} / {secs:.2f}s"
                    if real:
                        assert secs >= 1.5, f"{where}:生产时限 2s 下 {secs:.2f}s 就返回 —— 没在等 helper 自己的锁"
                    else:
                        assert secs < 1.5, f"{where}:helper 时限调成 200ms 仍等了 {secs:.2f}s —— 没用 helper 自己的时限"
                    c.execute("SHOW lock_timeout")
                    assert c.fetchone()[0] == "10s", f"{where}:调用方的 lock_timeout 没恢复"
                    c.execute("SELECT 1")
                    assert c.fetchone()[0] == 1, f"{where}:调用方的事务不可用了"
                    assert unchanged(c), f"{where}:放弃了却留下半截改动"
            finally:
                caller.rollback()
                holder.rollback()
                caller.close()
                holder.close()


def test_schema_guard_restores_the_callers_lock_timeout_after_a_successful_ddl(pg):
    """成功路径:DDL 在调用方事务里生效,之后调用方后续语句的等锁上限回到它自己设的值,不被 helper 的 2s 覆盖。"""
    import psycopg2

    dsn, table = pg
    sys.path.insert(0, str(REPO))
    from db import schema_guard as sg

    for label, call, unchanged in _missing_cases(sg, table):
        c = psycopg2.connect(dsn)
        try:
            with c.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '10s'")
                assert call(cur) is True, f"{label}:无锁时应真的执行"
                assert not unchanged(cur), f"{label}:返回 True 却没改到"
                cur.execute("SHOW lock_timeout")
                assert cur.fetchone()[0] == "10s", f"{label}:成功后调用方的 lock_timeout 没恢复"
        finally:
            c.rollback()
            c.close()


def test_schema_guard_other_errors_still_raise_and_leave_the_callers_transaction_usable(pg):
    import psycopg2

    dsn, table = pg
    sys.path.insert(0, str(REPO))
    from db import schema_guard as sg

    c = psycopg2.connect(dsn)
    try:
        with c.cursor() as cur:
            cur.execute(f"INSERT INTO {table} (id, status) VALUES (1, 'a')")
            with pytest.raises(psycopg2.errors.UndefinedColumn):
                sg.replace_in_list_check_if_changed(cur, table, f"{table}_nope_check", "no_such_col", ["x"])
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 1, "非锁错误退回保存点后,调用方此前的写应仍在"
    finally:
        c.rollback()
        c.close()
