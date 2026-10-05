"""运行期(Python 侧)裸 ``DROP INDEX`` 的机械普查 + 模块级 import 闭包。

轴C 只改了迁移 SQL。**运行期 `db/*.py` 里还有一批裸 DROP**,而且它们删的正是本单刚
绑好表的那些索引名 —— 交付单 §9.3-(b) 把这件事交回 Owner 时,必须说清"谁在什么时候
会跑到它",而不是凭印象。R2 收口发现第一版的机制陈述是错的(以为 prestart 先 import
`db.*` 所以运行期先删),所以这里把三件事都做成**可执行的谓词**,一处实现、三方共用:

  · :func:`import_closure`      —— 模块级 import 的传递闭包(函数体内的 import 不算,
                                    它们在 import 时不执行);回答"prestart 那一侧到底
                                    会不会把带裸 DROP 的模块拉进来"。
  · :func:`python_naked_drops`  —— **可执行面**(排除 ``tests/**`` 与 ``docs/**``)``.py``
                                    里的裸 ``DROP INDEX``。
  · :func:`non_test_callers`    —— 某个符号在非测试 ``.py`` 里的调用点(定义行除外),
                                    用来分辨"每次容器起都跑"和"全仓零调用的死代码"。

判据、交付单取证脚本都从这里拿数,别再各写一份 —— 同一谓词写两处必有一处没人验。
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 7 处运行期裸 DROP 的宿主模块 → 宿主函数(交付单 §9.3-(b) 引用的就是这三条)
DROP_BEARING_MODULES = {
    "db.fund_recovery_db": "init_fund_recovery_tables",
    "db.monitoring_db": "init_monitoring_tables",
    "db.research_selfserve_db": "ensure_selfserve_tables",
}

#: 必须是**语句开头**的 DROP INDEX(串首,或分号之后)。
#: 不加这条,``defects.append(f"…→ DROP INDEX {x}")`` 这种**错误文案**也会被算进分母。
_NAKED_DROP = re.compile(r"(?:^|;)\s*DROP\s+INDEX\b", re.IGNORECASE | re.MULTILINE)
_NAKED_DROP_NAME = re.compile(
    r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?"
    r"(?:(?P<schema>\"[^\"]+\"|[A-Za-z_][A-Za-z_0-9$]*)\s*\.\s*)?"
    r"(?P<name>\"[^\"]+\"|[A-Za-z_][A-Za-z_0-9$]*)",
    re.IGNORECASE,
)


def drop_index_name(line: str) -> str | None:
    """从一行裸 ``DROP INDEX`` 里取出索引名(去掉 schema 限定与引号)。"""
    m = _NAKED_DROP_NAME.search(line)
    if not m:
        return None
    return m.group("name").strip('"')


#: 🔴 **不在可执行面**的目录前缀。作用域按**语义**定义,不是白名单。
#:
#:    `tests/**`  判据自己,本来就不算"生产会跑的代码";
#:    `docs/**`   说明与**取证产物**。R2 把三份取证脚本(toctou_repro.py /
#:                runtime_drop_callgraph.py / strict_site_unreachable.py)落盘进
#:                `docs/AI-CONTEXT/**` 并 track 了,它们为了**演示**这个病,自然带着
#:                真的 `cur.execute("DROP INDEX ...")` —— 于是分母从 15/7 涨到 22/10,
#:                冻结锁在最终 commit 的树上必红。这是"工具扫到自己身上"的**第三例**
#:                (前两例:结构锁扫到变异清单里的 DSN、死代码锁扫到变异夹具字符串)。
#:                修法同款:**收窄作用域,不加白名单** —— 白名单是"这几个文件豁免",
#:                收窄是"这一层根本不属于被测面",后者未来不会漏。
#:
#: 🔴 刻意**只排这两层**,不写成"只看 db/ + scripts/":今天命中的确实只有这两层,
#:    但把分母钉死在它们上面,将来 `api/**` / `tools/**` / `middleware/**` 里冒出一处
#:    裸 DROP 就没有任何判据会红 —— 手写分母漏掉的那一项不会让任何判据变红。
_OUT_OF_SCOPE_PREFIXES = ("tests/", "docs/")


def is_executable_surface(rel: str) -> bool:
    """`rel` 是否属于「生产/运维可执行面」(普查与调用点普查共用这一处作用域)。"""
    if any(rel.startswith(pfx) for pfx in _OUT_OF_SCOPE_PREFIXES):
        return False
    return "/tests/" not in rel


def _is_test_path(rel: str) -> bool:
    """保留旧名给外部调用方;语义已并入 :func:`is_executable_surface`。"""
    return not is_executable_surface(rel)


def module_level_imports(path: Path) -> set[str]:
    """只收**模块级**(不在任何 def/class 体内)的 import 目标模块名。

    函数体内的 ``from db.fund_recovery_db import …`` 在 import 时**不执行** ——
    把它算进闭包会得出"prestart 会拉进 fund_recovery"这种错结论。
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    out: set[str] = set()

    def walk(node: ast.AST, inside_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            nested = inside_func or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if not inside_func and isinstance(child, ast.Import):
                out.update(a.name for a in child.names)
            elif not inside_func and isinstance(child, ast.ImportFrom):
                if child.level == 0 and child.module:
                    out.add(child.module)
            walk(child, nested)

    walk(tree, False)
    return out


def _module_path(mod: str, root: Path) -> Path | None:
    p = root / (mod.replace(".", "/") + ".py")
    if p.exists():
        return p
    p = root / mod.replace(".", "/") / "__init__.py"
    return p if p.exists() else None


def import_closure(entry: str, root: Path | None = None) -> set[str]:
    """``entry`` 模块的模块级 import 传递闭包(只跟能在本仓落到文件的模块)。"""
    root = root or ROOT
    seen: set[str] = set()
    stack = [entry]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        p = _module_path(mod, root)
        if p is None:
            continue
        seen.add(mod)
        stack.extend(m for m in module_level_imports(p) if m not in seen)
    return seen


def _tracked_py(root: Path) -> list[str]:
    raw = subprocess.run(["git", "ls-files", "*.py"], cwd=root,
                         capture_output=True, text=True).stdout
    return [ln for ln in raw.splitlines() if ln]


def _call_string_args(tree: ast.AST):
    """产出 ``(行号, 字面量文本)`` —— 只要**被当作调用实参**的字符串。

    🔴 为什么不按行 grep:本仓这几个轴C 脚本自己的注释/docstring/变异夹具字典里
       写满了 ``DROP INDEX`` 字样(实测按行 grep 得 49 处 / 11 文件,其中三分之二
       是在讲这件事而不是在做这件事)。分母掺了这种东西,冻结集合就锁不住真东西。
       走 AST 只认"进了某个 call 的字符串",注释与 docstring 自然出局,
       而 ``{...}`` 插值的 f-string 保留(占位符替换成 ``{}``,名字本来就运行期才知道)。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield arg.lineno, arg.value
            elif isinstance(arg, ast.JoinedStr):
                buf = []
                for part in arg.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        buf.append(part.value)
                    else:
                        buf.append("{}")
                yield arg.lineno, "".join(buf)
            elif isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
                # ``"DROP INDEX " + name`` 这种拼串
                buf = []
                for side in (arg.left, arg.right):
                    buf.append(side.value if isinstance(side, ast.Constant)
                               and isinstance(side.value, str) else "{}")
                yield arg.lineno, "".join(buf)


def python_naked_drops(root: Path | None = None) -> list[tuple[str, int, str]]:
    """**可执行面**的 ``.py`` 里**真的会被执行**的裸 ``DROP INDEX``。

    返回 ``(相对路径, 行号, SQL 字面量)``,按路径行号排序。
    """
    root = root or ROOT
    out: list[tuple[str, int, str]] = []
    for rel in _tracked_py(root):
        if not is_executable_surface(rel):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:                                     # pragma: no cover
            continue
        if "DROP" not in text.upper():
            continue
        try:
            tree = ast.parse(text, rel)
        except SyntaxError:                                 # pragma: no cover
            continue
        for lineno, sql in _call_string_args(tree):
            if _NAKED_DROP.search(sql):
                out.append((rel, lineno, " ".join(sql.split())[:160]))
    return sorted(out)


def non_test_callers(symbol: str, root: Path | None = None) -> list[tuple[str, int, str]]:
    """``symbol`` 在**可执行面**的 ``.py`` 里被**当作名字引用**的地方(import / 调用 / 传递)。

    零结果 = 全仓没有任何地方引用它(死代码);非零 = 有人用,再看用在哪一层。

    🔴 只认 AST 里的名字节点(``Name`` / ``Attribute`` / ``ImportFrom``),**不认字符串
       里的字面量**。第一版按行 grep,于是撕锁 runner 的变异夹具字典里那句
       ``"to": "... ensure_selfserve_tables()"``(那是**被引用的代码文本**,不是代码)
       被算成一个调用点 —— 判据在基线上就红。工具扫到自己身上,本轮第二次。
    """
    root = root or ROOT
    out: set[tuple[str, int, str]] = set()
    for rel in _tracked_py(root):
        if not is_executable_surface(rel):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:                                     # pragma: no cover
            continue
        if symbol not in text:
            continue
        try:
            tree = ast.parse(text, rel)
        except SyntaxError:                                 # pragma: no cover
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == symbol:
                hit = True
            elif isinstance(node, ast.Attribute) and node.attr == symbol:
                hit = True
            elif isinstance(node, ast.ImportFrom):
                hit = any(a.name == symbol or a.asname == symbol for a in node.names)
            elif isinstance(node, ast.Import):
                hit = any(a.name.rsplit(".", 1)[-1] == symbol for a in node.names)
            else:
                hit = False
            if not hit:
                continue
            lineno = getattr(node, "lineno", 0)
            body = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            out.add((rel, lineno, body))
    return sorted(out)
