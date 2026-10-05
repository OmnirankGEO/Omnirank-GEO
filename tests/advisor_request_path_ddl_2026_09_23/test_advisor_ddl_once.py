# -*- coding: utf-8 -*-
"""WO_285 · advisor 请求路径上的 DDL 只许每进程跑一次,且跑 DDL 的事务必须先 SET LOCAL lock_timeout。

病灶(WO_284 取证,Review 从 git 对象复核):`api/advisor_api.py::_ensure_advisor_identity_schema` 被六个 handler
在每次请求时调用,每次无条件执行 5 条 `ALTER TABLE advisors ADD COLUMN IF NOT EXISTS …`(同步 psycopg2)。
发车备份的 pg_dump 对每张表持 ACCESS SHARE,ALTER 即使列已存在也要 ACCESS EXCLUSIVE ⇒ 排在 dump 后面;
lock_timeout=0 ⇒ 一直等;同步调用卡住事件循环;WORKERS=1 ⇒ 全应用停(0913AA 上线 8 条 504)。
本机实测复现:会话 A 持 ACCESS SHARE,`ALTER … ADD COLUMN IF NOT EXISTS`(列已存在)1s 后 LockNotAvailable;
`CREATE INDEX IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS`(对象已存在)不被挡(读数见交付单)。

判据(不连库、不起服务):
  ① 行为:假连接记下每条语句。两处 ensure 同一进程调两次,DDL 只执行一次;顺序必须是
     `SET LOCAL lock_timeout` → DDL → COMMIT;锁超时(LockNotAvailable)⇒ 回滚、不抛、不置位,下次再试;
     autocommit 连接也要被放进一个事务里跑(SET LOCAL 在事务外不生效)。
  ② 结构(AST):`api/advisor_api.py` 里每个含 `ALTER TABLE` 的字符串,所在函数必须以 once-guard 开头
     (`global 旗标` + `if 旗标: return`,旗标在模块级初值 False)、成功后把旗标置 True、DDL 交给
     `_run_ddl_with_lock_timeout` 执行(不许直接 execute 一条 ALTER);`_run_ddl_with_lock_timeout` 的第一条
     execute 必须是 `SET LOCAL lock_timeout`。AST 找到的 ALTER 串数必须等于分词器数到的数(防扫描恒绿)。
  ③ 反臂(格内,对源码做文本替换后在临时模块里执行):去掉 once-guard ⇒ ① ② 都红;
     去掉 `SET LOCAL lock_timeout` 那一行 ⇒ ① ② 都红。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import pathlib
import re
import sys
import tokenize

import psycopg2.errors

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "api" / "advisor_api.py"
RUNNER = "_run_ddl_with_lock_timeout"
SET_LOCAL = "SET LOCAL lock_timeout"
_ALTER = re.compile(r"\bALTER\s+TABLE\b", re.I)
_DDL = re.compile(r"\b(ALTER\s+TABLE|CREATE\s+(UNIQUE\s+)?INDEX)\b", re.I)

GUARD = "    if _IDENTITY_SCHEMA_READY:\n        return True\n"
SET_LOCAL_LINE = "        cursor.execute(f\"SET LOCAL lock_timeout = '{_DDL_LOCK_TIMEOUT}'\")\n"


# ---------------------------------------------------------------------------
# 假连接:记下每条语句与当时的 autocommit;可在第 N 次碰到某串时抛错
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.conn.log.append(("EXEC", text, self.conn.autocommit))
        if self.conn.fail_on and self.conn.fail_on in text and self.conn.fail_times > 0:
            self.conn.fail_times -= 1
            raise psycopg2.errors.LockNotAvailable("canceling statement due to lock timeout")


class _FakeConn:
    def __init__(self, fail_on=None, fail_times=0, autocommit=False):
        self.log = []
        self.fail_on, self.fail_times = fail_on, fail_times
        self.autocommit = autocommit

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.log.append(("COMMIT",))

    def rollback(self):
        self.log.append(("ROLLBACK",))


def _ddl(log):
    return [e[1] for e in log if e[0] == "EXEC" and _DDL.search(e[1])]


# ---------------------------------------------------------------------------
# 模块:真的,或对源码做过文本替换的临时模块
# ---------------------------------------------------------------------------

def _real_module():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import api.advisor_api as mod
    assert pathlib.Path(mod.__file__).resolve() == SRC.resolve(), f"import 到的不是本工作树的文件:{mod.__file__}"
    return mod


def _module_from_source(source: str, name: str):
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(SRC)
    sys.modules[name] = mod
    try:
        exec(compile(source, str(SRC), "exec"), mod.__dict__)
    finally:
        sys.modules.pop(name, None)
    return mod


# ---------------------------------------------------------------------------
# ① 行为判据
# ---------------------------------------------------------------------------

_ENSURES = (  # (函数名, 旗标, 期望 DDL 条数)
    ("_ensure_advisor_identity_schema", "_IDENTITY_SCHEMA_READY", 5),
    ("_ensure_advisor_conversation_ownership_schema", "_OWNERSHIP_SCHEMA_READY", 2),
)


def once_problems(mod) -> list:
    out = []
    for fn_name, flag, n_ddl in _ENSURES:
        setattr(mod, flag, False)
        conn = _FakeConn()
        fn = getattr(mod, fn_name)
        fn(conn)
        first = list(conn.log)
        fn(conn)
        total = _ddl(conn.log)
        if len(total) != n_ddl:
            out.append(f"{fn_name}:同进程调两次,DDL 执行了 {len(total)} 条(应只有第一次的 {n_ddl} 条)")
        execs = [e for e in first if e[0] == "EXEC"]
        if not execs or SET_LOCAL not in execs[0][1]:
            out.append(f"{fn_name}:第一条语句不是 {SET_LOCAL}(实为 {execs[0][1] if execs else '无'!r})")
        if not first or first[-1] != ("COMMIT",):
            out.append(f"{fn_name}:DDL 之后没有 COMMIT(末条 {first[-1] if first else '无'!r})")
    return out


def test_ensures_run_ddl_once_behind_a_lock_timeout(monkeypatch):
    mod = _real_module()
    for _, flag, _ in _ENSURES:
        monkeypatch.setattr(mod, flag, False)
    assert once_problems(mod) == []


def test_lock_timeout_is_swallowed_logged_and_retried(monkeypatch, caplog):
    mod = _real_module()
    monkeypatch.setattr(mod, "_IDENTITY_SCHEMA_READY", False)
    conn = _FakeConn(fail_on="ALTER TABLE advisors", fail_times=1)
    with caplog.at_level("WARNING", logger="GEO-Advisor"):
        assert mod._ensure_advisor_identity_schema(conn) is False  # 不抛
    assert ("ROLLBACK",) in conn.log and ("COMMIT",) not in conn.log
    assert mod._IDENTITY_SCHEMA_READY is False, "失败不许置位(否则永远不再补列)"
    assert any("WO_285" in r.getMessage() and "LockNotAvailable" in r.getMessage() for r in caplog.records)
    assert mod._ensure_advisor_identity_schema(conn) is True  # 下次再试,成功
    assert mod._IDENTITY_SCHEMA_READY is True
    n = len(_ddl(conn.log))
    mod._ensure_advisor_identity_schema(conn)
    assert len(_ddl(conn.log)) == n, "成功之后不许再跑 DDL"


def test_autocommit_connection_still_runs_ddl_inside_a_transaction(monkeypatch):
    mod = _real_module()
    monkeypatch.setattr(mod, "_OWNERSHIP_SCHEMA_READY", False)
    conn = _FakeConn(autocommit=True)
    assert mod._ensure_advisor_conversation_ownership_schema(conn) is True
    execs = [e for e in conn.log if e[0] == "EXEC"]
    assert execs and all(e[2] is False for e in execs), f"DDL 与 SET LOCAL 必须跑在事务里:{execs}"
    assert conn.autocommit is True, "跑完要把调用方的 autocommit 还原"


# ---------------------------------------------------------------------------
# ② 结构判据(AST)
# ---------------------------------------------------------------------------

def _strings(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n


def _alter_token_count(source: str) -> int:
    kinds = {tokenize.STRING} | ({tokenize.FSTRING_MIDDLE} if hasattr(tokenize, "FSTRING_MIDDLE") else set())
    return sum(1 for t in tokenize.generate_tokens(io.StringIO(source).readline)
               if t.type in kinds and _ALTER.search(t.string))


def structure_problems(source: str) -> list:
    tree = ast.parse(source)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    false_flags = {t.id for n in tree.body if isinstance(n, ast.Assign)
                   and isinstance(n.value, ast.Constant) and n.value.value is False
                   for t in n.targets if isinstance(t, ast.Name)}
    out = []

    def enclosing(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    def executed_directly(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
                return True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False
        return False

    alters = [s for s in _strings(tree) if _ALTER.search(s.value)]
    if len(alters) != _alter_token_count(source) or not alters:
        out.append(f"AST 找到的 ALTER 串 {len(alters)} 个 ≠ 分词器数到的 {_alter_token_count(source)} 个(扫描不可信)")
    for s in alters:
        fn = enclosing(s)
        where = f"第 {s.lineno} 行的 ALTER"
        if fn is None:
            out.append(f"{where} 在模块级,不在任何函数里")
            continue
        body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                               and isinstance(getattr(fn.body[0], "value", None), ast.Constant)) else fn.body
        flag = None
        if len(body) >= 2 and isinstance(body[0], ast.Global) and isinstance(body[1], ast.If) \
                and isinstance(body[1].test, ast.Name) and body[1].test.id in body[0].names \
                and len(body[1].body) == 1 and isinstance(body[1].body[0], ast.Return):
            flag = body[1].test.id
        if flag is None or flag not in false_flags:
            out.append(f"{where}:所在函数 {fn.name} 没有以 once-guard 开头(global 旗标 + if 旗标: return,旗标模块级初值 False)")
            continue
        sets_true = any(isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and n.value.value is True
                        and any(isinstance(t, ast.Name) and t.id == flag for t in n.targets) for n in ast.walk(fn))
        if not sets_true:
            out.append(f"{where}:{fn.name} 从不把 {flag} 置 True —— 等于没有 guard")
        if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == RUNNER for n in ast.walk(fn)):
            out.append(f"{where}:{fn.name} 的 DDL 没交给 {RUNNER}(没有 lock_timeout)")
        if executed_directly(s):
            out.append(f"{where}:直接 execute 了 ALTER,绕过了 {RUNNER}")
    runners = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == RUNNER]
    if len(runners) != 1:
        out.append(f"找不到唯一的 {RUNNER}")
    else:
        calls = sorted((n for n in ast.walk(runners[0]) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"),
                       key=lambda n: (n.lineno, n.col_offset))
        first_text = "".join(s.value for s in _strings(calls[0])) if calls else ""
        if SET_LOCAL not in first_text:
            out.append(f"{RUNNER} 的第一条 execute 不是 {SET_LOCAL}(实为 {first_text[:40]!r})")
    return out


def test_every_alter_sits_in_a_once_guarded_function_behind_lock_timeout():
    assert structure_problems(SRC.read_text(encoding="utf-8")) == []


# ---------------------------------------------------------------------------
# ③ 反臂
# ---------------------------------------------------------------------------

def test_arms_removing_the_guard_or_the_lock_timeout_turn_red():
    source = SRC.read_text(encoding="utf-8")
    assert source.count(GUARD) == 1 and source.count(SET_LOCAL_LINE) == 1, "反臂没下成:锚点不是恰好 1 处"
    for label, broken in (("去掉 once-guard", source.replace(GUARD, "", 1)),
                          ("去掉 SET LOCAL lock_timeout", source.replace(SET_LOCAL_LINE, "", 1))):
        assert structure_problems(broken), f"{label}:结构判据没红"
        mod = _module_from_source(broken, "wo285_arm")
        assert once_problems(mod), f"{label}:行为判据没红"
    # 对照:同一套执行方式跑没改过的源码 ⇒ 两个判据都绿(证明红来自改动,不是临时模块本身)
    same = _module_from_source(source, "wo285_control")
    assert once_problems(same) == [] and structure_problems(source) == []


# ---------------------------------------------------------------------------
# ④ 启动钩子(WO_285b 补:Review 的毒「server.py 启动钩子不调 prewarm_advisor_schema ⇒ 5 格照样绿」)
# ---------------------------------------------------------------------------

def hook_problems(server_source: str) -> list:
    """server.py 里必须有一个 @app.on_event("startup") 处理函数,从 api.advisor_api 导入 prewarm_advisor_schema
    并真的调用它(直接调用,或作为实参交给 asyncio.to_thread 之类)。只 import 不调用 / 只在别的函数里调 ⇒ 不算。"""
    tree = ast.parse(server_source)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_startup = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "on_event"
                         and d.args and isinstance(d.args[0], ast.Constant) and d.args[0].value == "startup"
                         for d in fn.decorator_list)
        if not is_startup:
            continue
        imported = any(isinstance(n, ast.ImportFrom) and n.module == "api.advisor_api"
                       and any(a.name == "prewarm_advisor_schema" for a in n.names) for n in ast.walk(fn))
        invoked = any(isinstance(n, ast.Call) and any(isinstance(x, ast.Name) and x.id == "prewarm_advisor_schema"
                                                      for x in [n.func] + list(n.args)) for n in ast.walk(fn))
        if imported and invoked:
            return []
    return ["server.py 没有任何 startup 钩子从 api.advisor_api 导入并调用 prewarm_advisor_schema —— 首个请求又要付 DDL"]


def test_server_startup_hook_prewarms_advisor_schema():
    assert hook_problems((REPO / "server.py").read_text(encoding="utf-8")) == []


def test_hook_check_has_teeth():
    ok = """
@app.on_event("startup")
async def h():
    from api.advisor_api import prewarm_advisor_schema
    await asyncio.to_thread(prewarm_advisor_schema)
"""
    assert hook_problems(ok) == []
    for label, bad in (
        ("只 import 不调用", ok.replace("await asyncio.to_thread(prewarm_advisor_schema)", "pass")),
        ("不是 startup 钩子", ok.replace('@app.on_event("startup")', '@app.on_event("shutdown")')),
        ("调的是别的函数", ok.replace("to_thread(prewarm_advisor_schema)", "to_thread(something_else)")),
    ):
        assert hook_problems(bad), f"牙证失效:{label}"
