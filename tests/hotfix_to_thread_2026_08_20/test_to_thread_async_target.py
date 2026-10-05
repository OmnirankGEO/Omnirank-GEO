"""【热修 P0 2026-08-20】`asyncio.to_thread(X)` 里 X 是 `async def` —— 全仓 census + 锁。

## 这个形态为什么值得全仓扫

`asyncio.to_thread(f, *a)` 会在线程里**调用** `f`。`f` 是协程函数时,调用它拿到的是
**协程对象**,不是结果;`await to_thread(...)` 等到的也只是那个协程对象。

于是变量里躺着一个 coroutine,而且:

* 类型检查看不出来(没有注解约束);
* `if brand:` 这类判断也看不出来(协程对象是真值);
* 单测里只要 mock 掉那个函数就更看不出来;
* **一路到 JSON 序列化才炸** —— 表现是该端点 100% 500,
  外加一条谁也不会注意的 `RuntimeWarning: coroutine ... was never awaited`。

`api/geo_douyin_api.py` 的 delivery-plan 就是这么 500 的。既然踩了一次,就把
**同形态**在全仓扫干净并立锁。

## 判定口径:能机械判的判死,判不了的**显式登记**

* **`to_thread(名字)`**(裸函数引用)—— 能机械解析(本模块 def / `from X import Y`
  的 X 模块里的 def),**必须** 零命中;
* **`to_thread(对象.方法)`** —— 静态解析不出接收者的类型,不硬判。
  它们进 :data:`ATTRIBUTE_TARGETS_REVIEWED`,**逐条**写明人工核过的结论。
  默认放过 = census 恒绿,所以这里不许默认放过(与 WP7 caller census 同一条纪律)。

## 本次 census 的真实结果(2026-08-20,底 `9d359800d`)

扫到 4 处疑似,**逐条核完只有 1 处是真的**:

| 坐标 | 判定 |
|------|------|
| `api/geo_douyin_api.py:481` `to_thread(fetch_brand_display_name)` | **真命中**(本热修修的就是它) |
| `api/placement_api.py` `to_thread(service.check_knowledge_usage, …)` | 假阳性:接收者是 `PlacementService`,那个方法是 **sync**(`services/placement_service.py:2853`);同名的 `async def` 是本文件的**路由 handler** |
| `tests/test_writing_competitor_verification_pg.py` ×3 `to_thread(provider_release.wait, 10)` | 假阳性:`threading.Event.wait` |
| `services/ai_surface_monitoring/scheduler_wiring.py` `to_thread(_reservation_reaper)` | 假阳性:注入的是 `cost_policy.reap_stale_reservations`,**sync** |

**所以本热修只改了一处** —— 不为了"顺手多修几处"去动没坏的代码。
这份表写进判据,是为了让下一个人不用把这四处再核一遍。
"""
from __future__ import annotations

import ast
import collections
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
_SKIP = ("node_modules", ".git", "__pycache__", ".venv", "frontend/dist",
         "scripts/research/mutation_runner")

#: 属性形态(`obj.method`)的目标 —— 静态解析不出接收者类型,**逐条人工登记结论**。
#: 键是 `文件:行` 之外的稳定形态(文件 + 属性名),值是核完的结论。
#: 🔴 空字典 = 下面那条判据恒绿,所以它自己也要被断言非空。
ATTRIBUTE_TARGETS_REVIEWED: dict[tuple[str, str], str] = {
    ("api/placement_api.py", "check_knowledge_usage"):
        "接收者是 PlacementService;services/placement_service.py:2853 是 sync def。"
        "同名的 async def 是本文件的路由 handler,不是这里被传的那个。",
    ("tests/test_writing_competitor_verification_pg.py", "wait"):
        "threading.Event.wait —— 标准库同步阻塞调用,正是 to_thread 该包的东西。",
    ("services/geo_douyin/production_task.py", "close"):
        "接收者是 `db.connection.get_connection()` 返回的 psycopg2 连接,"
        "`conn.close()` 是同步的。同名的 async def 在 services/meijiehezi/client.py:409 "
        "与 services/meijiehezi_client.py:209(HTTP 客户端),与这里无关。",
}

#: 名字形态但解析不到定义(注入的可调用变量等)—— 同样逐条登记。
NAME_TARGETS_REVIEWED: dict[tuple[str, str], str] = {
    ("services/ai_surface_monitoring/scheduler_wiring.py", "_reservation_reaper"):
        "模块级注入变量;唯一注入方 services/geo_observation/integration.py:83 传的是 "
        "cost_policy.reap_stale_reservations,sync def。",
    ("api/finance_api.py", "fn"):
        "`async def _run(fn)` 的**形参** —— 通用包装器,目标由每个调用方各自传入。"
        "静态解析不到,已人工看过全部调用点均为同步 DB 函数。",
    ("services/research_monitor/round_runner.py", "fn"):
        "同上形态:通用 `_run(fn)` 包装器的形参。",
}


def _python_files() -> list[pathlib.Path]:
    return [p for p in REPO.rglob("*.py")
            if not any(s in p.as_posix() for s in _SKIP)]


def _index() -> tuple[dict, dict, dict]:
    """全仓:模块 → async def 名字 / sync def 名字;以及 path → AST。"""
    async_defs: dict = collections.defaultdict(set)
    sync_defs: dict = collections.defaultdict(set)
    trees: dict = {}
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        trees[path] = tree
        mod = path.relative_to(REPO).as_posix()[:-3].replace("/", ".")
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                async_defs[mod].add(node.name)
            elif isinstance(node, ast.FunctionDef):
                sync_defs[mod].add(node.name)
    return async_defs, sync_defs, trees


def _scan():
    """返回 (确定命中, 属性形态待判, 名字形态解析不到)。"""
    async_defs, sync_defs, trees = _index()
    definite, attribute, unresolved = [], [], []

    for path, tree in trees.items():
        rel = path.relative_to(REPO).as_posix()
        mod = rel[:-3].replace("/", ".")
        imported: dict = {}
        module_alias: dict = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    local = alias.asname or alias.name
                    imported[local] = (node.module, alias.name)
                    # `from db import geo_douyin_db as ddb` —— ddb 是**一个模块**,
                    # 于是 `ddb.get_post` 是可以机械解析的,不该落进人工登记表。
                    module_alias[local] = node.module + "." + alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module_alias[alias.asname or alias.name.split(".")[0]] = alias.name
        local_async = async_defs.get(mod, set())
        local_sync = sync_defs.get(mod, set())

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            if (getattr(fn, "attr", None) or getattr(fn, "id", None)) != "to_thread":
                continue
            target = node.args[0]
            if isinstance(target, ast.Attribute):
                # 接收者是**模块别名**时可以机械解析(绝大多数 `db.xxx` 都是这种)
                base = getattr(target.value, "id", None)
                src_mod = module_alias.get(base) if base else None
                if src_mod and (src_mod in async_defs or src_mod in sync_defs):
                    if (target.attr in async_defs.get(src_mod, set())
                            and target.attr not in sync_defs.get(src_mod, set())):
                        definite.append((rel, node.lineno, base + "." + target.attr,
                                         "async def @ " + src_mod))
                    continue
                # 接收者是**对象实例**(`service.foo()`)—— 静态判不了。
                # 🔴 但只有**这个方法名在全仓某处真的是 `async def`** 时才值得人工签字:
                #    名字在全仓从来没当过协程函数(`commit` / `rollback` / `post_order` …)
                #    的那些,物理上不可能是本 bug,让它们逐条进登记表是纯负担 ——
                #    而负担会让下一个人把整条锁删掉。
                #    收窄之后登记表只剩**真有歧义**的那几个,自证见
                #    `test_the_ambiguous_filter_is_not_vacuous`。
                if any(target.attr in names for names in async_defs.values()):
                    attribute.append((rel, node.lineno, target.attr))
                continue
            if not isinstance(target, ast.Name):
                continue                      # lambda / 下标等,不在本锁范围
            name = target.id
            if name in local_async and name not in local_sync:
                definite.append((rel, node.lineno, name, "本模块 async def"))
            elif name in local_sync:
                pass
            elif name in imported:
                src_mod, orig = imported[name]
                if (orig in async_defs.get(src_mod, set())
                        and orig not in sync_defs.get(src_mod, set())):
                    definite.append((rel, node.lineno, name, "async def @ " + src_mod))
            else:
                unresolved.append((rel, node.lineno, name))
    return definite, attribute, unresolved


# ══════════════════════════════════════════════════════════════════════════
# 分母自证:扫描器**真的**看得见东西
# ══════════════════════════════════════════════════════════════════════════

def test_the_scanner_has_a_denominator():
    """全仓真的有一堆 `to_thread` 调用 —— 否则下面几条恒绿。"""
    _, attribute, unresolved = _scan()
    total = len(attribute) + len(unresolved)
    # 光看这两类不够,再数一次全部 to_thread 调用点
    calls = 0
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and (getattr(node.func, "attr", None)
                         or getattr(node.func, "id", None)) == "to_thread"):
                calls += 1
    assert calls >= 30, calls
    assert total >= 1, (total, calls)


def test_the_scanner_can_actually_see_an_offender():
    """🔁 判别力自证:把一段**已知有问题**的源码喂给同一套判别逻辑 → 必须命中。

    没有这条,「零命中」可能只是 AST 遍历写错了、从来没命中过任何东西。
    毒串在这里现写,**不从被测代码里读** —— 负样本与被测逻辑共用真值 =
    两边一起错还一起绿。
    """
    src = (
        "import asyncio\n"
        "async def slow_thing(x):\n"
        "    return x\n"
        "async def caller():\n"
        "    return await asyncio.to_thread(slow_thing, 1)\n"
    )
    tree = ast.parse(src)
    local_async = {n.name for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef)}
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "to_thread"
            and n.args and isinstance(n.args[0], ast.Name)
            and n.args[0].id in local_async]
    assert len(hits) == 1, ast.dump(tree)


# ══════════════════════════════════════════════════════════════════════════
# 主锁:确定命中必须为零;判不了的必须**显式登记**
# ══════════════════════════════════════════════════════════════════════════

def test_the_ambiguous_filter_is_not_vacuous():
    """🔴 收窄自证:「attr 名在全仓当过 async def」这个筛子**真的筛出东西**。

    如果它筛不出任何东西,`test_every_attribute_target_is_explicitly_reviewed`
    就变成了对空集合的断言 —— 恒绿。
    """
    _, attribute, _ = _scan()
    assert attribute, "属性形态待判集合是空的 —— 那条登记判据恒绿了"
    assert {attr for _rel, _line, attr in attribute} & set(ATTRIBUTE_TARGETS_REVIEWED and
                                                          {k[1] for k in ATTRIBUTE_TARGETS_REVIEWED})


def test_no_to_thread_call_targets_a_coroutine_function():
    """🔴 `to_thread(名字)` 里那个名字**不许**是 `async def`。

    命中的后果不是"慢一点",是**那一行拿到的是协程对象**,
    静默到 JSON 序列化才炸(该端点 100% 500)。
    """
    definite, _, _ = _scan()
    assert not definite, (
        "这些 to_thread 传的是协程函数,拿到的是协程对象不是结果:\n  "
        + "\n  ".join("{0}:{1}  to_thread({2})   <- {3}".format(*d) for d in definite))


def test_every_attribute_target_is_explicitly_reviewed():
    """🔴 `to_thread(对象.方法)` 静态判不了 —— 但**不许默认放过**。

    每一个属性形态的目标都要在 `ATTRIBUTE_TARGETS_REVIEWED` 里有一条结论。
    (与 WP7 caller census 同一条纪律:判不了 ≠ 不用判,是要有人签字。)
    """
    _, attribute, _ = _scan()
    assert ATTRIBUTE_TARGETS_REVIEWED, "登记表是空的 —— 本条判据恒绿"
    missing = sorted({(rel, attr) for rel, _line, attr in attribute
                      if (rel, attr) not in ATTRIBUTE_TARGETS_REVIEWED})
    assert not missing, (
        "这些 to_thread 的目标是属性形态,静态判不了,必须显式登记结论:\n  "
        + "\n  ".join("{0}  to_thread(….{1})".format(*m) for m in missing))


def test_every_unresolved_name_target_is_explicitly_reviewed():
    """🔴 名字形态但解析不到定义(注入的可调用变量等)—— 同样必须登记。"""
    _, _, unresolved = _scan()
    assert NAME_TARGETS_REVIEWED, "登记表是空的 —— 本条判据恒绿"
    missing = sorted({(rel, name) for rel, _line, name in unresolved
                      if (rel, name) not in NAME_TARGETS_REVIEWED})
    assert not missing, (
        "这些 to_thread 目标解析不到定义,必须显式登记结论:\n  "
        + "\n  ".join("{0}  to_thread({1})".format(*m) for m in missing))


def test_the_review_ledger_does_not_outlive_reality():
    """🔁 反向对照:登记表里的条目必须**还真的存在**。

    census 不许比现实活得久 —— 留着已经删掉的条目,等于给"下次真出现同名的"
    预先发了一张通行证。
    """
    _, attribute, unresolved = _scan()
    live_attr = {(rel, attr) for rel, _l, attr in attribute}
    live_name = {(rel, name) for rel, _l, name in unresolved}
    stale = ([k for k in ATTRIBUTE_TARGETS_REVIEWED if k not in live_attr]
             + [k for k in NAME_TARGETS_REVIEWED if k not in live_name])
    assert not stale, ("登记表里这些条目已经不存在了:" + str(stale))


def test_the_delivery_plan_handler_awaits_the_coroutine_directly():
    """🔴 就地钉住本次修的那一行:delivery-plan 里 `fetch_brand_display_name`
    是**直接 await**,不是塞进 to_thread。

    上面那条通用锁已经覆盖它,这一条是**坐标锁**:通用锁哪天被改宽了,
    这一行仍然有人守。
    """
    src = (REPO / "api" / "geo_douyin_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "api_quote_delivery_plan")
    body = ast.unparse(fn)
    assert "await fetch_brand_display_name(" in body, body[:400]
    assert "to_thread(fetch_brand_display_name" not in body, body[:400]
    # 🔁 前提自证:它**确实**是个协程函数(不然这条锁在钉一件不存在的事)
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                  and n.name == "fetch_brand_display_name")
    assert isinstance(helper, ast.AsyncFunctionDef), type(helper)
