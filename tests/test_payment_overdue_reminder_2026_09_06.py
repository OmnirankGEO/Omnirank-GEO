"""#120 · 待收款提醒:一个只喂日志的查询,把整个业务功能杀了三个月。

## 病灶

`scheduler.py::job_payment_overdue_check` 里原来是这样:

    try:
        conn2 = get_connection()
        brand_row = conn2.execute("SELECT name FROM brands ...").fetchone()   # ← 必抛
        brand_name = brand_row["name"] if brand_row else "客户"
        conn2.close()
        enqueue_brand_owner_notification_event_durable(...)                    # ← 从没执行过
        notified.add(d)
    except Exception as e:
        logger.exception("待收款提醒写入 outbox 失败: %s", e)

psycopg2 的**连接**对象没有 `.execute`(那是 cursor 的方法)⇒ 第二行必抛
`AttributeError` ⇒ 被 except 接住 ⇒ **enqueue 一次都没被调用过**,
`notified` 也从不增长 ⇒ 1/3/7/14/21/30 天的提醒每天重算、每天失败。

日志文案是「待收款提醒写入 outbox 失败」—— 而它**根本没走到 outbox**。
🔴 这就是「错的具体消息比笼统消息更坏」:它让人去查 outbox,而病在上面三行。

而且 `conn2.close()` 写在抛出点**之后**(不是 finally)⇒ 每次还漏一个连接:
N 个待收款会话 × 6 个提醒档 = 每天一轮泄漏。

## 本文件守什么

🔴 修法不是「把 execute 改对」就完了。`brand_name` **只喂下面那行 logger.info**,
   `enqueue` 根本不用它。一个只喂日志的查询不该有能力中止真正干活的那步 ——
   所以它被挪出 try 并自带兜底(`_brand_name_for_log`)。
   臂 2 守的就是这条结构保证:**取名炸了,提醒照发**。

⚠️ 坐标订正:工单写的是 `api/scheduler.py:1216`,实际在**仓库根** `scheduler.py:1216`。
   两个文件都有 1216 行,是巧合。根 `scheduler.py` 是活代码
   (`server.py:380 / 2166`、`api/dashboard_api.py:1819` 都 import 它 ——
   import 语句缩在函数体里,带 `^` 锚的 grep 看不见)。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ══════════════════════════════════════════════════════════════
# 夹具:最小可驱动世界(只替换**输入**与**观察点**,不替被测逻辑干活)
# ══════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, rows, raise_on_execute=None):
        self._rows, self._raise = rows, raise_on_execute
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raise is not None:
            raise self._raise

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows, raise_on_execute=None):
        self.cursor_obj = _FakeCursor(rows, raise_on_execute)
        self.closed = 0

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed += 1


def _session_row(days_ago: int):
    """一条待收款会话。`sales_confirmed_at` 必须是 ISO 串(生产就是这么存的)。"""
    from datetime import datetime, timedelta
    return {
        "token": "tok-overdue",
        "brand_id": 629,
        "quote_id": 4242,
        "sales_confirmed_at": (datetime.now() - timedelta(days=days_ago)).isoformat(),
        "payment_overdue_notified_days": "[]",
        "status": "pending_payment",
    }


@pytest.fixture
def driven(monkeypatch):
    """驱动 job_payment_overdue_check 一轮,把 enqueue 调用记下来。

    只替换三样:会话行的来源(输入)、enqueue(观察点)、update_session(观察点)。
    被测的循环、天数判定、以及 brand_name 的隔离,全部跑真的。
    """
    import db.diagnosis_db as ddb
    import services.notification_outbox as outbox
    import scheduler as sched

    state = {"enqueued": [], "sessions": [], "brand_conns": []}

    def _fake_get_connection():
        # 第一次调用 = 取会话列表;之后的调用 = _brand_name_for_log 取品牌名
        if not state["sessions"]:
            state["sessions"].append(1)
            return _FakeConn([_session_row(state["days_ago"])])
        conn = _FakeConn([{"name": "真品牌名"}], raise_on_execute=state.get("brand_boom"))
        state["brand_conns"].append(conn)
        return conn

    monkeypatch.setattr(ddb, "get_connection", _fake_get_connection)
    monkeypatch.setattr(ddb, "update_session",
                        lambda token, **kw: state.setdefault("updates", []).append((token, kw)))
    monkeypatch.setattr(
        outbox, "enqueue_brand_owner_notification_event_durable",
        lambda **kw: state["enqueued"].append(kw))
    # 🔴 驱动**函数体**,绕开 `@sched_claim`。
    #    claim 是 tick 级并发闸,要打真库(sched_job_runs);它在本环境里 fail-CLOSED,
    #    于是函数体一行都不会跑 —— 那样这些臂全是「跑了个 None」。
    #    claim 本身由 test_scheduler_jobs_keep_their_claim_decorator 单独守。
    #
    #    ⚠️ `__wrapped__` 存在与否**本身就是**装饰器还在不在的证据:
    #    我第一版把 helper 插进了装饰器与函数之间,那时这里会 AttributeError。
    assert hasattr(sched.job_payment_overdue_check, "__wrapped__"), (
        "job_payment_overdue_check 没有 __wrapped__ —— @sched_claim 掉了或套错了函数")
    state["run"] = sched.job_payment_overdue_check.__wrapped__
    return state


# ══════════════════════════════════════════════════════════════
# 臂 1 · 行为终态:提醒必须真的进 outbox
# ══════════════════════════════════════════════════════════════

def test_reminders_actually_reach_the_outbox(driven):
    """🔴 修前这条必红:enqueue 一次都不会被调用。

    断言打在**终态**(enqueue 收到了什么)上,不是「没抛异常」——
    原代码就是不抛异常的,它把异常吞了。
    """
    driven["days_ago"] = 8          # 跨过 1/3/7 三档
    driven["run"]()
    days = sorted(int(k["terminal_state"].rsplit("_", 1)[1]) for k in driven["enqueued"])
    assert days == [1, 3, 7], "应发 1/3/7 三档,实际 %r" % (days,)
    assert all(k["brand_id"] == 629 for k in driven["enqueued"])


def test_notified_days_are_persisted_so_it_stops_repeating(driven):
    """已发的档要落库,否则明天原样再发一遍(旧版就是这个死循环:永远是空集)。"""
    driven["days_ago"] = 8
    driven["run"]()
    updates = dict(driven["updates"])[("tok-overdue")] if False else driven["updates"]
    persisted = [kw for _, kw in updates if "payment_overdue_notified_days" in kw]
    assert persisted, "已通知天数没有落库"
    assert "[1, 3, 7]" in persisted[-1]["payment_overdue_notified_days"]


# ══════════════════════════════════════════════════════════════
# 臂 2 · 结构隔离:取名炸了,提醒照发(本次修复的核心保证)
# ══════════════════════════════════════════════════════════════

def test_a_failing_log_lookup_does_not_kill_the_reminder(driven):
    """🔴 本文件最重要的一条。

    让 `_brand_name_for_log` 内部炸掉(正是旧版每天都在发生的事),
    提醒**仍然必须**发出去。旧版这里是 0 条 —— 那就是三个月零提醒的全部原因。
    """
    driven["days_ago"] = 8
    driven["brand_boom"] = AttributeError(
        "'psycopg2.extensions.connection' object has no attribute 'execute'")
    driven["run"]()
    # 🔴 先证**毒下成了**:取名这一步必须真的被走到并真的炸了。
    #    否则「提醒发出去了」可能只是因为取名压根没执行 —— 那样这条臂什么都没验。
    #    (第一版就是这样空过的:它绿着,而隔离根本没被驱动。)
    assert driven["brand_conns"], "取名这一步没被执行 —— 本臂空过,不是通过"
    assert len(driven["enqueued"]) == 3, (
        "取品牌名失败把提醒一起吞了 —— 只喂日志的东西又拿到了中止业务的能力")


def test_the_log_lookup_closes_its_connection_even_when_it_blows_up(driven):
    """连接泄漏臂:旧版 close 写在抛出点之后 ⇒ 永远关不掉。"""
    driven["days_ago"] = 8
    driven["brand_boom"] = AttributeError("boom")
    driven["run"]()
    assert driven["brand_conns"], "根本没去取品牌名 —— 分母塌了,不是通过"
    unclosed = [c for c in driven["brand_conns"] if c.closed != 1]
    assert not unclosed, "%d 个连接没被关(或被关了多次)—— 泄漏回来了" % len(unclosed)


# ══════════════════════════════════════════════════════════════
# 臂 3 · 全仓普查:连接对象上不许直接 .execute(
# ══════════════════════════════════════════════════════════════

#: 冻结豁免。`data/` 下是一次性分析脚本(sqlite 风格),不在生产链路上。
#: 🔴 写成**冻结集**而不是「跳过整个目录」:新增第二个豁免必须显式改这里。
EXEMPT = frozenset({"data/analyze_responses.py"})

PRODUCTION_DIRS = ("api", "db", "services", "middleware", "workflows", "agents", "tools")


def _connection_named_execute_calls(path: Path) -> list[str]:
    """找「从 get_connection() 拿到的对象上直接 .execute(」。

    🔴 按**数据流**找,不按变量名叫不叫 conn:本仓里有的地方变量名叫 conn
       但其实是 cursor(那种是对的),按名字判会误报;也有叫 c2 的连接
       (按名字判会漏)。所以先找 `X = get_connection()` 的赋值,再看
       同一函数里有没有 `X.execute(`。
    """
    try:
        tree = ast.parse(io.open(path, encoding="utf-8", errors="replace").read())
    except SyntaxError:
        return []
    bad = []
    # 🔴 只遍历**函数作用域**,不把 Module 也当一个作用域走一遍 ——
    #    第一版两者都走,同一处调用被数了两次(自证臂当场报 2 != 1)。
    #    模块级语句单独处理,且跳过里面的函数体,避免同一节点进两次。
    scopes: list = [n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    module_level = ast.Module(
        body=[s for s in tree.body
              if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))],
        type_ignores=[])
    scopes.append(module_level)
    for func in scopes:
        conns = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                fn = node.value.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in {"get_connection", "get_db_connection"}:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            conns.add(t.id)
        if not conns:
            continue
        for node in ast.walk(func):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in conns):
                bad.append("%s:%d %s.execute(" % (path.name, node.lineno, node.func.value.id))
    return bad


def _census_paths() -> list[Path]:
    paths = [ROOT / "scheduler.py", ROOT / "server.py"]
    for d in PRODUCTION_DIRS:
        paths.extend(sorted((ROOT / d).rglob("*.py")))
    return [p for p in paths
            if p.exists() and str(p.relative_to(ROOT)).replace("\\", "/") not in EXEMPT]


def test_no_production_code_calls_execute_on_a_connection():
    """psycopg2 的 connection 没有 .execute —— 写了就是运行期 AttributeError。

    这类 bug 单靠人看看不出来(它长得跟 sqlite3 的合法写法一模一样),
    而它一旦被 except 吞掉就**完全没有症状**。所以立一条普查。
    """
    paths = _census_paths()
    assert len(paths) > 200, "普查分母只有 %d 个文件 —— 分母塌了,不是通过" % len(paths)
    offenders = []
    for p in paths:
        offenders.extend(_connection_named_execute_calls(p))
    assert not offenders, "连接对象上直接 .execute(:\n  " + "\n  ".join(offenders)


def test_the_census_probe_can_actually_see_one():
    """配对自证:喂一段**合成的**坏代码,探针必须抓到。

    否则上面那条可能只是因为 AST 匹配写错而恒绿。喂合成源码,不改真文件。
    """
    import tempfile
    src = (
        "def f():\n"
        "    conn2 = get_connection()\n"
        "    row = conn2.execute('SELECT 1').fetchone()\n"
        "    return row\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(src)
        tmp = Path(fh.name)
    try:
        hits = _connection_named_execute_calls(tmp)
        assert len(hits) == 1 and "conn2.execute(" in hits[0], hits
    finally:
        tmp.unlink(missing_ok=True)


def test_scheduler_jobs_keep_their_claim_decorator():
    """🔴 装饰器归属锁 —— 这条是一次**险些交付的事故**换来的。

    修 #120 时我把 `_brand_name_for_log` 插在了
    `@sched_claim("payment_overdue_check", 86400)` 与它修饰的函数**之间**。
    后果:claim 套到了新 helper 头上,而 `job_payment_overdue_check`
    **丢掉了并发保护**(money 相邻任务的 tick 级 claim)。

    可怕的是它有多安静:`ast.parse` 过、`import scheduler` 过、
    本文件 7 条判据里 5 条绿。只有「连接必须被关」那一条红了 ——
    而那条红的表面原因是「分母塌了」,跟装饰器毫无关系。

    所以立这把锁:**每个 job_* 函数都必须自己带着 sched_claim**,
    并且 sched_claim 不许套在非 job 函数上(反向臂:插错位置时那个
    helper 会背上 claim,正是本次的病形)。
    """
    src = io.open(ROOT / "scheduler.py", encoding="utf-8").read()
    tree = ast.parse(src)
    claimed, jobs = set(), set()
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        has = any(ast.unparse(d).startswith("sched_claim") for d in n.decorator_list)
        if has:
            claimed.add(n.name)
        if n.name.startswith("job_"):
            jobs.add(n.name)
    assert jobs, "scheduler.py 里一个 job_* 都没有 —— 分母塌了,不是通过"
    # 正向:本次涉及的那个 job 必须带 claim(它是 money 相邻的每日任务)
    assert "job_payment_overdue_check" in claimed, (
        "job_payment_overdue_check 丢了 @sched_claim —— 很可能有人把新定义插进了"
        "装饰器与函数之间")
    # 反向:claim 只许戴在 job_* 上。插错位置的症状就是某个非 job 函数背上了 claim。
    stray = sorted(n for n in claimed if not n.startswith("job_"))
    assert not stray, "@sched_claim 套到了非 job 函数上(插入位置错了?): %s" % stray


def test_the_census_does_not_flag_the_correct_shape():
    """反向对照:正确写法(取 cursor 再 execute)不许被判成违规。

    只证「抓得到坏的」证明不了「不会误伤好的」—— 误伤会逼后来人加白名单,
    而白名单一加就再没人回头看。
    """
    import tempfile
    src = (
        "def f():\n"
        "    conn = get_connection()\n"
        "    try:\n"
        "        cur = conn.cursor()\n"
        "        cur.execute('SELECT 1')\n"
        "        return cur.fetchone()\n"
        "    finally:\n"
        "        conn.close()\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(src)
        tmp = Path(fh.name)
    try:
        assert _connection_named_execute_calls(tmp) == []
    finally:
        tmp.unlink(missing_ok=True)
