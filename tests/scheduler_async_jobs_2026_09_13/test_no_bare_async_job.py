"""#197 c2 —— 白名单锁:每个 `add_job` 的第一实参都必须是**同步可调用**。

🔴 这是防复发的承重件,所以写成**白名单**而不是黑名单:
   「不许出现 run_tick」对**新增**的裸 async job 结构性失明 —— 而新增正是它存在的理由。
   这里的规则是「每一个 add_job 都要能解析到定义,且 async 的必须被包装」,
   解析不到**也算红**(解析不到 = 我不知道它是什么,不能当它没问题)。

🔴 分母要打印:`add_job` 总数 / 解析成功数。分母塌了(比如 AST 锚过期、
   某个文件没被扫到)时,一个「全绿」什么都不说明。
"""
import ast
import io
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 两份调度器都要扫 —— 09-13 我自己就因为 grep 只列了 api/ 而漏掉根 scheduler.py,
#: 把一个每小时真在跑的 job 报成了"零调用方"。
SCHEDULER_FILES = ("api/scheduler.py", "scheduler.py")

#: 认得出的包装器。包装器的作用是把 async 变成同步 callable。
WRAPPERS = {
    "run_async_in_scheduler",     # [#197] 本单新加的模块级包装器
    "_run_async",                 # `_setup_mhz_jobs` 里的同名闭包(委托给上面那只)
    "run_async",
    "scheduler_sync_callable",    # services/sched_claim.py:45 —— 另一只现役包装器
}


def _module_async_names(tree) -> set:
    """模块里所有 `async def` 的名字(含嵌套)。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            out.add(node.name)
    return out


def _module_sync_names(tree) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            out.add(node.name)
    return out


def _import_sources(tree) -> dict:
    """`from M import N [as A]` → {本地名: (模块, 原名)},含函数体内的局部 import。"""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = (node.module, alias.name)
    return out


def _is_wrapped(call_arg) -> bool:
    """第一实参是不是「包装过的」。

    认三种形态:`W(f)`、`deco(...)(W(f))`、以及 lambda / 同步 def。
    """
    node = call_arg
    for _ in range(6):                      # 有界深度,防畸形 AST 打转
        if not isinstance(node, ast.Call):
            return False
        fn = node.func
        if isinstance(fn, ast.Call):
            # `sched_claim("x", 600)(inner)` —— **装饰器套用**:
            #   外层 Call 的 func 本身是 Call,真正的被包装对象在 args[0]。
            #   我第一版在这儿走错了分支(把 "x" 当成了被包装对象)。
            node = node.args[0] if node.args else None
            continue
        name = (fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute) else None)
        if name in WRAPPERS:
            return True
        node = node.args[0] if node.args else None
    return False


def _assigned_values(tree) -> dict:
    """`name = <expr>` 的绑定(含函数体内)。用于 `x = _run_async(f)` 这类中转。"""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                out[t.id] = node.value
    return out


def _param_names(tree) -> set:
    """所有函数的形参名。形参 = **间接派发**:这一处的 add_job 收的是调用方给的东西,
    在本文件里判不了;由调用方那一处负责(它自己也会被本判据扫到)。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for a in list(node.args.args) + list(node.args.kwonlyargs):
                out.add(a.arg)
    return out


def _innermost_name(node):
    """剥掉装饰器套用,取最里层那个裸名字。

    `sched_claim("k", 600)(managed_campaign_tick)` → `managed_campaign_tick`。
    没有裸名字(lambda / 动态取属性)时返回 None。
    """
    for _ in range(6):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Call):
            node = node.args[0] if node.args else None
            continue
        return None
    return None


def _resolve_kind(name, tree, cache):
    """判断一个裸名字指向 async / sync / 间接 / 解析不到。"""
    if name in _module_async_names(tree):
        return "async"
    if name in _module_sync_names(tree):
        return "sync"
    assigned = _assigned_values(tree)
    if name in assigned:
        val = assigned[name]
        if _is_wrapped(val) or isinstance(val, ast.Lambda):
            return "sync"
        if isinstance(val, ast.Name):
            return _resolve_kind(val.id, tree, cache)
    if name in _param_names(tree):
        return "indirect"
    imports = _import_sources(tree)
    if name in imports:
        mod, orig = imports[name]
        path = REPO / (mod.replace(".", "/") + ".py")
        if path.exists():
            if path not in cache:
                cache[path] = ast.parse(io.open(path, encoding="utf-8").read())
            t2 = cache[path]
            if orig in _module_async_names(t2):
                return "async"
            if orig in _module_sync_names(t2):
                return "sync"
            # 被导入的模块里也可能是**模块级赋值**包了一层:
            #   `run_flywheel_watchdog_job = flywheel_job(k)(run_flywheel_watchdog)`
            #   —— 剥到最里层再判,别把"包过的同步函数"当成解析不到。
            assigned2 = _assigned_values(t2)
            if orig in assigned2:
                inner2 = _innermost_name(assigned2[orig])
                if inner2:
                    if inner2 in _module_async_names(t2):
                        return "async"
                    if inner2 in _module_sync_names(t2):
                        return "sync"
        return "unresolved"
    return "unresolved"


def test_every_scheduled_job_is_a_sync_callable():
    total = resolved = 0
    offenders = []
    unresolved = []
    cache = {}

    for rel in SCHEDULER_FILES:
        path = REPO / rel
        assert path.exists(), "调度器文件不在:%s —— 分母塌了" % rel
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_job"
                    and node.args):
                continue
            total += 1
            arg = node.args[0]
            if _is_wrapped(arg):
                resolved += 1
                continue
            if isinstance(arg, ast.Lambda):
                resolved += 1
                continue
            inner = _innermost_name(arg)
            if inner is not None:
                kind = _resolve_kind(inner, tree, cache)
                if kind == "async":
                    offenders.append("%s:%d %s" % (rel, node.lineno, inner))
                    resolved += 1
                elif kind in ("sync", "indirect"):
                    resolved += 1
                else:
                    unresolved.append("%s:%d %s" % (rel, node.lineno, inner))
                continue
            if isinstance(arg, ast.Attribute):
                resolved += 1          # obj.method(...) —— 本仓无此形态,留着不算漏
                continue
            unresolved.append("%s:%d <%s>" % (rel, node.lineno, type(arg).__name__))

    print("[#197 c2] add_job 总数 %d · 解析成功 %d · 解析不到 %d · 裸 async %d"
          % (total, resolved, len(unresolved), len(offenders)))

    assert total >= 90, "只扫到 %d 个 add_job —— 分母塌了,后面的绿没有意义" % total
    assert not unresolved, (
        "这些 add_job 的第一实参解析不到定义(解析不到 = 我不知道它是什么,"
        "不能当它没问题):\n  " + "\n  ".join(unresolved))
    assert not offenders, (
        "这些 job 把 async def **裸传**给了调度器 —— 线程里只会造一个 coroutine "
        "就返回,调度器记 executed successfully 而函数体一行没跑:\n  "
        + "\n  ".join(offenders))
