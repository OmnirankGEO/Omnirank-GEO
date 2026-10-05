"""[A4] AST 扫描器 · 找 "async def 端点内直接调同步重活"(= 占死单 worker 事件循环)。

生产实证(2026-08-02 03:22 nginx access log):`/admin/geo-placement-flywheel` 页 20 并发,
16 个飞轮端点 **全部在 ±0.06s 内同时完成**(10.45s → 13.73s → 22.83s → 60.0s 四次加载),
且完全无关的轻端点 `/api/wallet` / `/api/user/notifications` 一并被拖到 rt=60.0 → 504。
这是单 worker 事件循环被同步 DB 调用串行占死的指纹。

扫描口径(不是名字启发式,是**可达性实证**):
  1. 走 `ast`,天然剥掉注释/docstring —— 不整文件跑正则(此坑已踩两次);
  2. 先给全仓 `db/ services/ scripts/ writing/ tools/ agents/ workflows/` 的顶层函数建
     "是否可达阻塞原语" 索引(get_connection / get_db / psycopg2 / requests / pandas /
     time.sleep / subprocess),按调用图传递闭包(深度受限 + 记忆化);
  3. 对每个 `@router.<verb>` 装饰的 **async def** 端点,只看**直接在协程体里执行**的调用:
     - 嵌套的 `def` / `lambda` 体一律跳过(那是给 to_thread / run_rebuild_guarded 的闭包,
       跑在线程池里,不占 loop);
     - `asyncio.to_thread(...)` / `loop.run_in_executor(...)` / `run_rebuild_guarded(...)`
       的实参一律跳过(同理);
  4. 命中 = 该调用可达阻塞原语 → 报告。

用法:
    python scripts/scan_async_endpoint_blocking_2026_08_01.py            # 全仓清单
    python scripts/scan_async_endpoint_blocking_2026_08_01.py --json     # 机器可读
    python scripts/scan_async_endpoint_blocking_2026_08_01.py --selftest # 自证扫描器有判别力
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 被扫的端点文件所在目录
ENDPOINT_DIRS = ("api",)

# 建调用图索引的库目录(端点会调到的同步实现都在这里)
LIBRARY_DIRS = ("db", "services", "scripts", "writing", "tools", "agents", "workflows", "middleware", "auth")

# 阻塞原语:出现即认定该函数是"同步重活"
BLOCKING_PRIMITIVES = {
    "get_connection",       # db.connection 连接池(psycopg2 同步)
    "get_db",               # db.connection contextmanager
    "put_connection",
    "connect",              # psycopg2.connect / pymysql.connect
    "read_sql",             # pandas.read_sql
    "read_sql_query",
}
# 属性调用形式的阻塞原语:模块名 -> 方法集合(None = 该模块任意方法都算)
BLOCKING_ATTR_ROOTS = {
    "requests": None,
    "psycopg2": {"connect"},
    "pd": {"read_sql", "read_sql_query", "read_csv", "read_excel"},
    "pandas": {"read_sql", "read_sql_query", "read_csv", "read_excel"},
    "subprocess": {"run", "call", "check_output", "check_call"},
    "time": {"sleep"},
}

# 这些包裹器的实参跑在线程池里 → 跳过不算阻塞
THREADPOOL_WRAPPERS = {
    "to_thread",            # asyncio.to_thread
    "run_in_executor",
    "run_rebuild_guarded",  # services.flywheel_rebuild_lock(后台线程 + 互斥)
}

MAX_DEPTH = 4


# ---------------------------------------------------------------- 调用图索引


def _iter_py(dirs: tuple[str, ...]):
    for d in dirs:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class _Index:
    """qualified name -> FunctionDef;以及 module -> {local name: qualified name}。"""

    def __init__(self) -> None:
        self.funcs: dict[str, ast.AST] = {}
        self.aliases: dict[str, dict[str, str]] = {}

    def build(self) -> None:
        for path in _iter_py(LIBRARY_DIRS + ENDPOINT_DIRS):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            mod = _module_name(path)
            alias: dict[str, str] = {}
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.funcs[f"{mod}.{node.name}"] = node
                    alias[node.name] = f"{mod}.{node.name}"
                elif isinstance(node, ast.ClassDef):
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            self.funcs[f"{mod}.{node.name}.{sub.name}"] = sub
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    for a in node.names:
                        alias[a.asname or a.name] = f"{node.module}.{a.name}"
            self.aliases[mod] = alias

    def resolve(self, module: str, local_name: str) -> str | None:
        return self.aliases.get(module, {}).get(local_name)


def _local_import_aliases(node: ast.AST) -> dict[str, str]:
    """函数体内的 `from x import y`(仓里大量端点用函数内 import 规避循环依赖)。

    不收集这些别名 = 扫描器漏报;此前版本就漏掉了 engine-weight / answer-entities 那批。
    """
    alias: dict[str, str] = {}
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom) and child.module and child.level == 0:
            for a in child.names:
                alias[a.asname or a.name] = f"{child.module}.{a.name}"
    return alias


INDEX = _Index()


def _called_names(node: ast.AST, *, skip_nested_defs: bool, skip_wrapper_args: bool) -> list[tuple[str, int, str]]:
    """收集 node 体内直接执行的调用 (显示名, 行号, 形态)。

    skip_nested_defs=True  → 嵌套 def/lambda 体不算(它们跑在线程池)
    skip_wrapper_args=True → to_thread/run_in_executor/run_rebuild_guarded 的实参不算
    """
    found: list[tuple[str, int, str]] = []

    def visit(n: ast.AST, *, inside_body: bool) -> None:
        for child in ast.iter_child_nodes(n):
            if skip_nested_defs and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                fname = _call_display_name(child)
                if skip_wrapper_args and _short_name(fname) in THREADPOOL_WRAPPERS:
                    # 包裹器本身不算,其实参也整体跳过
                    continue
                if isinstance(child.func, ast.Name):
                    found.append((child.func.id, child.lineno, "name"))
                elif isinstance(child.func, ast.Attribute):
                    found.append((fname, child.lineno, "attr"))
            visit(child, inside_body=inside_body)

    visit(node, inside_body=True)
    return found


def _call_display_name(call: ast.Call) -> str:
    f = call.func
    parts: list[str] = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def _short_name(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _is_primitive(dotted: str) -> bool:
    short = _short_name(dotted)
    if "." in dotted:
        root = dotted.split(".", 1)[0]
        allowed = BLOCKING_ATTR_ROOTS.get(root, "__missing__")
        if allowed is None:
            return True
        if allowed != "__missing__" and short in allowed:
            return True
        return False
    return short in BLOCKING_PRIMITIVES


@lru_cache(maxsize=None)
def _reaches_blocking(qualname: str, depth: int) -> bool:
    """qualname 这个函数(传递地)是否会执行阻塞原语。"""
    if depth > MAX_DEPTH:
        return False
    node = INDEX.funcs.get(qualname)
    if node is None:
        return False
    module = qualname.rsplit(".", 1)[0]
    local_alias = _local_import_aliases(node)
    # 库函数内部的嵌套 def 也算它自己的重活(它是同步函数,整体在调用方栈上执行)
    for dotted, _lineno, _kind in _called_names(node, skip_nested_defs=False, skip_wrapper_args=False):
        if _is_primitive(dotted):
            return True
        if "." in dotted:
            continue
        target = local_alias.get(dotted) or INDEX.resolve(module, dotted)
        if target and target != qualname and _reaches_blocking(target, depth + 1):
            return True
    return False


# ---------------------------------------------------------------- 端点扫描


def _is_route_decorated(node: ast.AsyncFunctionDef | ast.FunctionDef) -> str | None:
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        name = _call_display_name(dec)
        head, _, verb = name.rpartition(".")
        if head in ("router", "app") and verb in ("get", "post", "put", "delete", "patch"):
            path = ""
            if dec.args and isinstance(dec.args[0], ast.Constant):
                path = str(dec.args[0].value)
            return f"{verb.upper()} {path}"
    return None


def scan() -> list[dict]:
    findings: list[dict] = []
    for path in _iter_py(ENDPOINT_DIRS):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        module = _module_name(path)
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            route = _is_route_decorated(node)
            if route is None:
                continue
            hits: list[dict] = []
            local_alias = _local_import_aliases(node)
            for dotted, lineno, _kind in _called_names(
                node, skip_nested_defs=True, skip_wrapper_args=True
            ):
                if _is_primitive(dotted):
                    hits.append({"call": dotted, "line": lineno, "via": "primitive"})
                    continue
                if "." in dotted:
                    continue
                target = local_alias.get(dotted) or INDEX.resolve(module, dotted)
                if target and _reaches_blocking(target, 1):
                    hits.append({"call": dotted, "line": lineno, "via": target})
            if hits:
                # 同一函数多次调用只留首次,避免清单噪音
                seen: set[str] = set()
                uniq = []
                for h in hits:
                    if h["call"] in seen:
                        continue
                    seen.add(h["call"])
                    uniq.append(h)
                findings.append(
                    {
                        "file": rel,
                        "endpoint": route,
                        "handler": node.name,
                        "line": node.lineno,
                        "blocking_calls": uniq,
                    }
                )
    findings.sort(key=lambda f: (f["file"], f["line"]))
    return findings


# ---------------------------------------------------------------- selftest


_SELFTEST_SRC = '''
from fastapi import APIRouter
import asyncio, requests
from db.connection import get_connection
router = APIRouter()

@router.get("/blocking")
async def blocking_ep():
    conn = get_connection()
    return conn

@router.get("/wrapped")
async def wrapped_ep():
    def _work():
        return get_connection()
    return await asyncio.to_thread(_work)

@router.get("/sync-def")
def sync_ep():
    return get_connection()

@router.get("/clean")
async def clean_ep():
    return {"ok": True}

@router.get("/attr")
async def attr_ep():
    return requests.get("http://x")

@router.get("/local-import")
async def local_import_ep():
    from db.connection import get_connection as _gc
    return _gc()
'''


def selftest() -> int:
    """自证扫描器有判别力:该抓的抓到、不该抓的不抓。"""
    import tempfile

    ok = True
    tree = ast.parse(_SELFTEST_SRC)
    module = "api.__selftest__"
    INDEX.aliases[module] = {"get_connection": "db.connection.get_connection"}
    got: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if _is_route_decorated(node) is None:
            continue
        hits = []
        local_alias = _local_import_aliases(node)
        for dotted, _lineno, _k in _called_names(node, skip_nested_defs=True, skip_wrapper_args=True):
            if _is_primitive(dotted):
                hits.append(dotted)
            elif "." not in dotted:
                target = local_alias.get(dotted) or INDEX.resolve(module, dotted)
                if target and _reaches_blocking(target, 1):
                    hits.append(dotted)
        got[node.name] = hits

    expect_hit = {
        "blocking_ep": "get_connection",
        "attr_ep": "requests.get",
        "local_import_ep": "_gc",  # 函数内 import 的别名也必须解析(漏了就是漏报)
    }
    expect_clean = ["wrapped_ep", "clean_ep"]
    for name, call in expect_hit.items():
        if call not in got.get(name, []):
            print(f"  [FAIL] 应命中但没命中: {name} 缺 {call} (实得 {got.get(name)})")
            ok = False
        else:
            print(f"  [OK]  命中 {name} -> {call}")
    for name in expect_clean:
        if got.get(name):
            print(f"  [FAIL] 应放行却误报: {name} -> {got[name]}")
            ok = False
        else:
            print(f"  [OK]  放行 {name}(线程池包裹 / 无重活)")
    # sync def 端点根本不进 async 扫描面
    if any(isinstance(n, ast.AsyncFunctionDef) and n.name == "sync_ep" for n in ast.walk(tree)):
        print("  [FAIL] sync_ep 不该是 AsyncFunctionDef")
        ok = False
    else:
        print("  [OK]  sync def 端点不进扫描面(FastAPI 自动送线程池)")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        INDEX.build()
        return selftest()

    INDEX.build()
    findings = scan()
    if args.json:
        print(json.dumps(findings, ensure_ascii=False, indent=2))
        return 0
    by_file: dict[str, list[dict]] = {}
    for f in findings:
        by_file.setdefault(f["file"], []).append(f)
    print(f"# async 端点内裸同步重活清单 · 共 {len(findings)} 处 / {len(by_file)} 个文件\n")
    for fname in sorted(by_file):
        print(f"## {fname}  ({len(by_file[fname])} 处)")
        for f in by_file[fname]:
            calls = ", ".join(h["call"] for h in f["blocking_calls"])
            print(f"  L{f['line']:<5} {f['endpoint']:<58} {f['handler']}")
            print(f"          阻塞调用: {calls}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
