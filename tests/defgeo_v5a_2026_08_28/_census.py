"""V5-A 判据共用的**机械枚举器**。

三条分母都在这里生成,判据只消费,不各自再抄一份扫描逻辑
(同一个谓词写两处 ⇒ 必有一处没人验)。

① :func:`quotes_write_sites` —— 全仓生产 ``.py`` 里对 ``quotes`` 的
   ``UPDATE`` / ``INSERT`` 站点,并标出 SET 触没触及 canonical 谓词列。
② :func:`token_write_routes` —— ``api/selection_api.py`` 里以 ``{token}``
   为路径参数的**写方法**路由,以及每个 handler 有没有接归属校验。
③ :func:`customer_link_exception_classes` —— ``customer_links`` 模块定义的
   自定义异常类全集。

🔴 三条都只看**字符串字面量 / AST 节点**,不做整文件裸串 grep。
   理由是本仓踩过的那两条:
     · 裸串锁会被**注释里引用规则原文**误触发(我自己撞过);
     · 行内注释里的 ``UPDATE quotes`` 会被当成 SQL(第一版扫描器就多报了
       ``server.py`` 的三处 docstring 散文)。
   走 ``ast`` 取字符串常量既排除了注释,也自然拿到了正确的行号。
"""
from __future__ import annotations

import ast
import pathlib
import re
from typing import Iterable, NamedTuple

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 扫描分母:**生产** Python。测试/脚本/前端/文档不在内 —— 它们不写生产库。
_SKIP_TOP = {
    "tests", "scripts", "frontend", "node_modules", ".git", "qa", "docs",
    "data", "output", "cache", "logs", "backups", "agent-test-artifacts",
}

#: ``canonical_quote_id()`` 的谓词与排序键。任何 SET 触及它们的写者,
#: 都能改变「这个品牌当前那一份报价是哪一份」这个答案。
#:
#: 逐字对照 ``services.defensive_geo.customer_links.canonical_quote_id``::
#:
#:     WHERE brand_id = %s AND deleted_at IS NULL
#:     ORDER BY created_at DESC, id DESC LIMIT 1
#:
#: ``id`` 不在集合里:它是主键,生产没有也不该有 ``SET id=``;
#: 真出现了会被下面的 :data:`PREDICATE_COLUMNS` 之外的路径接住吗?不会 ——
#: 所以显式把它也列进来,宁可多红一次。
PREDICATE_COLUMNS = frozenset({"deleted_at", "brand_id", "created_at", "id"})

#: 拿品牌级序列化点的那个原语的名字。census 找的就是它的调用。
LOCK_FN = "lock_brand_quote_serialization_point"


class SqlSite(NamedTuple):
    rel: str
    lineno: int
    verb: str                 # "UPDATE" | "INSERT"
    columns: tuple[str, ...]  # UPDATE 才有;INSERT 恒空
    dynamic: bool             # SET 子句里有 f-string 插值
    has_set: bool             # UPDATE 后面根本没有 SET(= 散文,不是 SQL)
    func: str | None          # 所在函数(module 级为 None)


def production_py() -> list[pathlib.Path]:
    out = []
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel.split("/")[0] in _SKIP_TOP:
            continue
        out.append(p)
    return out


def _string_nodes(tree: ast.AST) -> Iterable[tuple[int, str, bool]]:
    """(lineno, 文本, 是否含插值)。f-string 的插值洞统一记成 ``{__DYNAMIC__}``。"""
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n.lineno, n.value, False
        elif isinstance(n, ast.JoinedStr):
            parts = []
            for v in n.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    parts.append("{__DYNAMIC__}")
            yield n.lineno, "".join(parts), True


def _enclosing_functions(tree: ast.AST) -> list[tuple[int, int, str]]:
    """(起行, 止行, 函数名),按范围从小到大排 —— 取第一个命中的即最内层。"""
    spans = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(n, "end_lineno", None) or n.lineno
            spans.append((n.lineno, end, n.name))
    spans.sort(key=lambda s: s[1] - s[0])
    return spans


def _func_at(spans, line: int) -> str | None:
    for lo, hi, name in spans:
        if lo <= line <= hi:
            return name
    return None


_SET_HEAD = re.compile(r"\s+(?:AS\s+)?[A-Za-z_][A-Za-z_0-9]*\s+SET\b|\s+SET\b", re.I)
_STOP = re.compile(r"\b(WHERE|RETURNING|FROM)\b", re.I)
_ASSIGN = re.compile(r"(?:^|,)\s*([A-Za-z_][A-Za-z_0-9]*)\s*=")


def _set_columns(text: str, start: int) -> tuple[tuple[str, ...], bool, bool]:
    """从 ``UPDATE quotes`` 之后切出 SET 子句,返回 (列, 动态, 有没有 SET)。

    括号深度感知:``SET total_keywords = (SELECT … WHERE …)`` 里那个 WHERE
    在深度 1,不能当断句点 —— 否则它后面的赋值列会被**漏掉**(漏 = 假绿)。
    """
    m = _SET_HEAD.match(text, start)
    if not m:
        return (), False, False
    j = m.end()
    depth = 0
    k = j
    buf = []
    while k < len(text):
        c = text[k]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                break
        if depth == 0 and _STOP.match(text, k) and (k == j or not text[k - 1].isalnum()):
            break
        buf.append(c)
        k += 1
    seg = "".join(buf)
    cols = tuple(sorted({a.group(1) for a in _ASSIGN.finditer(seg)}))
    return cols, "__DYNAMIC__" in seg, True


def scan_source(rel: str, src: str) -> list[SqlSite]:
    """扫**一份源码文本**。抽出来是为了让判别力自证能拿合成源码打扫描器本身 ——
    扫描器也是代码,「自己出题自己批改」的另一面是「工具从没被验过」。"""
    sites: list[SqlSite] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return sites
    spans = _enclosing_functions(tree)
    for lineno, s, dyn in _string_nodes(tree):
        for m in re.finditer(r"UPDATE\s+quotes\b", s, re.I):
            off = lineno + s[:m.start()].count("\n")
            cols, d2, has_set = _set_columns(s, m.end())
            sites.append(SqlSite(rel, off, "UPDATE", cols, dyn or d2, has_set,
                                 _func_at(spans, off)))
        for m in re.finditer(r"INSERT\s+INTO\s+quotes\b", s, re.I):
            off = lineno + s[:m.start()].count("\n")
            sites.append(SqlSite(rel, off, "INSERT", (), False, True,
                                 _func_at(spans, off)))
    return sites


def quotes_write_sites() -> list[SqlSite]:
    """全仓生产 ``.py`` 里所有写 ``quotes`` 的 SQL 字面量站点。"""
    sites: list[SqlSite] = []
    for p in production_py():
        sites.extend(scan_source(p.relative_to(REPO).as_posix(),
                                 p.read_text(encoding="utf-8", errors="replace")))
    return sites


def predicate_writers(sites: Iterable[SqlSite]) -> list[SqlSite]:
    """SET 触及 canonical 谓词列的 UPDATE 站点。"""
    return [s for s in sites
            if s.verb == "UPDATE" and s.has_set
            and set(s.columns) & PREDICATE_COLUMNS]


def functions_reaching_lock(rel: str) -> set[str]:
    """``rel`` 里所有**能到达**锁原语的模块级函数名(同模块内取传递闭包)。

    🔴 为什么不是"函数体内直接出现调用"
    ---------------------------------
    ``services/artifact_archive.py`` 把「读 brand_id + 拿锁」抽成了一个
    小 helper(两处归档/恢复共用)。只认直接调用的话,这两处会被判成漏锁 ——
    而它们其实拿了。这就是本仓记过的
    「抽纯函数之后调用点没人守 / 抽常量把锚点移出边界」的镜像面:
    **重构改变了谓词该打在哪一层**,判据得跟着走,而不是逼代码为判据写重复。

    闭包**只在同一个模块内**求,不跨文件:跨文件传递会把谓词稀释成
    "这个文件里有人调过锁",那就没有约束力了。
    """
    src = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    calls: dict[str, set[str]] = {}
    direct: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names: set[str] = set()
        for sub in ast.walk(n):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    names.add(f.id)
                elif isinstance(f, ast.Attribute):
                    names.add(f.attr)
        calls[n.name] = names
        if LOCK_FN in names:
            direct.add(n.name)
    reach = set(direct)
    changed = True
    while changed:
        changed = False
        for fn, names in calls.items():
            if fn in reach:
                continue
            if names & reach:
                reach.add(fn)
                changed = True
    return reach


def function_calls_lock(rel: str, func: str) -> bool:
    """``rel`` 里名为 ``func`` 的函数体内**有没有**调用锁原语(AST,不是裸串)。

    🔴 用 AST 不用 ``in src``:注释里写一句「这里应该拿
       lock_brand_quote_serialization_point」也会让裸串锁变绿。
       我自己在 V3-A 那轮就被同款问题咬过一次。
    """
    src = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func:
            for sub in ast.walk(n):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name) and f.id == LOCK_FN:
                        return True
                    if isinstance(f, ast.Attribute) and f.attr == LOCK_FN:
                        return True
    return False


def lock_call_line(rel: str, func: str) -> int | None:
    src = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func:
            best = None
            for sub in ast.walk(n):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    ok = ((isinstance(f, ast.Name) and f.id == LOCK_FN)
                          or (isinstance(f, ast.Attribute) and f.attr == LOCK_FN))
                    if ok and (best is None or sub.lineno < best):
                        best = sub.lineno
            return best
    return None


def lock_acquisition_line(rel: str, func: str) -> int | None:
    """``func`` 里**拿到品牌锁**那一刻的行号 —— 直接调用或经同模块 helper 都算。

    比 :func:`lock_call_line` 宽一层,是为了让「锁必须在被保护的那条语句之前」
    这个断言对**全部**站点都成立,而不是对其中一两处成立、其余全 skip。
    6/7 skip 的判据等于没验 —— 本仓把那个记成「零分母恒绿」。
    """
    reach = functions_reaching_lock(rel)
    src = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or n.name != func:
            continue
        best = None
        for sub in ast.walk(n):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name == LOCK_FN or (name in reach and name != func):
                if best is None or sub.lineno < best:
                    best = sub.lineno
        return best
    return None


def first_row_lock_line(rel: str, func: str) -> int | None:
    """函数体内第一处 ``FOR UPDATE`` / ``FOR SHARE`` 字面量的行号。"""
    src = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func:
            best = None
            for lineno, s, _dyn in _string_nodes(n):
                m = re.search(r"FOR\s+(UPDATE|SHARE)\b", s, re.I)
                if m:
                    at = lineno + s[:m.start()].count("\n")
                    if best is None or at < best:
                        best = at
            return best
    return None


# ══════════════════════════════════════════════════════════════════════════
# ② selection_api 的 {token} 写路由
# ══════════════════════════════════════════════════════════════════════════
OWNER_GUARD = "_require_session_owner_access"
WRITE_METHODS = ("post", "put", "patch", "delete")


class Route(NamedTuple):
    method: str
    path: str
    handler: str
    lineno: int
    guarded: bool


def _guard_in_function(node: ast.AST) -> bool:
    """handler 体内有没有真的**调用**归属校验。

    两种合法形态都算(两种都在现役里真的出现):
      · 直接调用 ``_require_session_owner_access(request, token)``;
      · ``asyncio.to_thread(_require_session_owner_access, request, token)``
        —— 这里 guard 是**实参**不是 callee,只认 ``Call.func`` 会把
        ``approve_quote`` 误判成漏接(它是现役里唯一用这个形态的)。

    🔴 只认这两种。第三种形态(比如塞进变量再调)会红 —— 那是对的:
       新形态该由人看一眼,而不是被一条宽松的谓词默默放过。
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Name) and f.id == OWNER_GUARD:
            return True
        if isinstance(f, ast.Attribute) and f.attr == OWNER_GUARD:
            return True
        # to_thread(guard, …) / run_in_executor(None, guard, …)
        for arg in sub.args:
            if isinstance(arg, ast.Name) and arg.id == OWNER_GUARD:
                return True
    return False


def token_write_routes(rel: str = "api/selection_api.py") -> list[Route]:
    src = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    out: list[Route] = []
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in n.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            f = dec.func
            if not (isinstance(f, ast.Attribute) and f.attr in WRITE_METHODS):
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            path = str(dec.args[0].value)
            if "{token}" not in path:
                continue
            out.append(Route(f.attr.upper(), path, n.name, n.lineno,
                             _guard_in_function(n)))
    out.sort(key=lambda r: r.lineno)
    return out


# ══════════════════════════════════════════════════════════════════════════
# ③ customer_links 的自定义异常类全集
# ══════════════════════════════════════════════════════════════════════════
def customer_link_exception_classes(
        rel: str = "services/defensive_geo/customer_links.py") -> list[str]:
    src = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.ClassDef):
            continue
        for base in n.bases:
            name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
            if name.endswith("Error") or name.endswith("Exception"):
                out.append(n.name)
                break
    return sorted(out)


def names_caught_by(rel: str, func: str) -> set[str]:
    """``rel`` 的 ``func`` 里所有 ``except`` 子句捕获的异常名(含元组形态)。"""
    src = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    got: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or n.name != func:
            continue
        for sub in ast.walk(n):
            if not isinstance(sub, ast.ExceptHandler) or sub.type is None:
                continue
            targets = (sub.type.elts if isinstance(sub.type, ast.Tuple) else [sub.type])
            for tnode in targets:
                if isinstance(tnode, ast.Name):
                    got.add(tnode.id)
                elif isinstance(tnode, ast.Attribute):
                    got.add(tnode.attr)
    return got
