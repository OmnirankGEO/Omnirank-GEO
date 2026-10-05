# -*- coding: utf-8 -*-
"""运行时按路径读取的**非 ASCII** 仓内路径 —— 机械枚举器(WO_272 · 2026-09-23)。

🔴 起因:`a02225e31`(07-12 anchor 快照)把仓里几乎所有中文文件名按 cp437 双重编码写坏了。
   代码按**正确名**读,镜像里只有**乱码名** ⇒ 服务商协议 404(WO_268)、GEO 知识库 4 个文件 +
   案例库**静默为空**(WO_272)—— 两个多月没有任何东西红。这把尺子把「代码按名字读的仓内路径」
   收成分母,逐条要求:在 HEAD 的树里、且进得了镜像构建上下文。

## 什么算「按路径读取」(只认这四种形状 —— 散文里的「产品/服务」一条都不收)
  ① **锚定表达式**:以 `__file__` 为根、经 `.parent` / `os.path.dirname` / `.resolve()` /
     `Path(...)` / `/` / `os.path.join(...)` 拼出来的路径里含非 ASCII 字面量
     (`Path(__file__).resolve().parent.parent / "docs" / "条款"`、
      `os.path.join(KNOWLEDGE_BASE_DIR, "案例库")`)。模块级 `NAME = <锚定表达式>` 会被记住,
     之后 `NAME / "…"`、`os.path.join(NAME, "…")` 都按锚定算。
  ② **锚定目录 + 动态尾巴**:同一模块里出现 `<锚定目录> / <非常量>`(或 join 的非常量参数)时,
     该模块**模块级容器**(dict / list / tuple / set 字面量)里「像相对路径」的非 ASCII 字符串
     都按「锚定目录/该串」算(`KNOWLEDGE_DIR / config["file"]` + `KNOWLEDGE_BASES` 的 "file" 值)。
     同函数里的 f-string 模板(`f"服务商申请协议_{version}.md"`)按通配展开到该目录下的实际文件。
  ③ **仓根相对常量**:非 ASCII 串以仓库**顶层目录名 + "/"** 开头(`"knowledge/案例库/v9_ranking_template.md"`)。
     容器工作目录是 /app = 仓根,所以它就是仓内路径。
  ④ **路径函数的直接参数**:`open/Path/os.path.exists/isfile/isdir/listdir/scandir/glob` 的非 ASCII
     常量参数,且形似路径(无空白)。

## 解析结果三类
  `file` 树里有这个文件 · `dir` 树里有这个目录(登记目录本身 + 它**直接**下属的每个跟踪文件 ——
  代码按目录读时,里面任何一个文件都可能被读到)· `missing` 树里没有(再看有没有 cp437 乱码形:
  `mojibake_twin=True` 就是本次事故那一类)。

## 覆盖不到的形态(自曝盲区,别当成「这里干净了」)
  · 非 .py 的运行时读取(nginx.conf、start.sh、JSON 配置里写的文件名);
  · 跨模块传递的路径(A 模块定义常量、B 模块拼接);只在函数体内赋值、再经多层变量传递的路径;
  · 前端 import 的非 ASCII 资源(Vite 构建期会炸,不会静默)。
"""
from __future__ import annotations

import ast
import fnmatch
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_LIST_REL = "config/runtime_required_paths.txt"

#: 不属于「运行时」的顶层目录:测试与一次性脚本不在镜像的常驻执行路径上。
NON_RUNTIME_PREFIXES = ("tests/", "scripts/")

_PATH_FUNCS = {"open", "Path", "PurePath", "exists", "isfile", "isdir", "listdir", "scandir",
               "glob", "iglob", "read_text", "read_bytes"}
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_WS_RE = re.compile(r"\s")
#: 散文标点 —— 真文件名里不会有(「（完整版）」的全角括号、「→」是文件名里真有的,不在此列)。
#: 首跑时容器规则把「你还记得第一个客户/第一份工作吗？」「AI/SaaS软件」当成了路径,就是缺这一条。
_PROSE_RE = re.compile(r"[，。？！、；：「」『』《》“”‘’…—?*:\"<>|]")


@dataclass(frozen=True)
class Finding:
    source: str          # 哪个 .py
    line: int
    literal: str         # 代码里写的那个非 ASCII 串(或 f-string 模板)
    shape: str           # anchored / container / repo_relative / call_arg / fstring / fstring_glob
    resolved: str        # 仓内相对路径(目录不带尾斜杠);多候选时 = 实际会被读到的那个
    kind: str            # file / dir / missing
    mojibake_twin: bool  # missing 时:树里是否只有它的 cp437 乱码形
    alternatives: Tuple[str, ...] = ()   # 「按顺序取第一个存在的」那组候选(f-string 元组)


# ══════════════════════════════════════════════════════════════════
# 树
# ══════════════════════════════════════════════════════════════════

class Tree:
    def __init__(self, files: Iterable[str]):
        self.files: Set[str] = set(files)
        self.dirs: Set[str] = set()
        for f in self.files:
            parts = f.split("/")
            for i in range(1, len(parts)):
                self.dirs.add("/".join(parts[:i]))
        self.top_dirs = {d for d in self.dirs if "/" not in d}

    def kind(self, p: str) -> str:
        if p in self.files:
            return "file"
        if p in self.dirs:
            return "dir"
        return "missing"

    def children(self, d: str) -> List[str]:
        pre = d + "/"
        return sorted(f for f in self.files if f.startswith(pre) and "/" not in f[len(pre):])


def cp437_twin(p: str) -> str:
    """正确名 → 07-12 那次写进树里的乱码名(逐段;ASCII 段不变)。"""
    out = []
    for seg in p.split("/"):
        if seg.isascii():
            out.append(seg)
            continue
        try:
            out.append(seg.encode("utf-8").decode("cp437"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            out.append(seg)
    return "/".join(out)


# ══════════════════════════════════════════════════════════════════
# 锚定表达式的符号求值(只认有限几种写法,认不出就返回 None)
# ══════════════════════════════════════════════════════════════════

_UNKNOWN = object()


def _norm(p: str) -> str:
    p = posixpath.normpath(p) if p else ""
    return "" if p == "." else p


class _Eval:
    def __init__(self, module_path: str, bases: Dict[str, str]):
        self.module_path = module_path
        self.bases = bases           # 模块级 NAME → 仓内相对路径

    def path(self, node) -> Optional[str]:
        """返回仓内相对路径;不是锚定表达式 ⇒ None。"""
        r = self._ev(node)
        return None if r is _UNKNOWN or r is None else _norm(r)

    def _const(self, node) -> Optional[str]:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def _ev(self, node):
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return self.module_path
            return self.bases.get(node.id, _UNKNOWN)
        if isinstance(node, ast.Attribute):
            if node.attr == "parent":
                v = self._ev(node.value)
                return _UNKNOWN if v is _UNKNOWN else posixpath.dirname(v)
            return _UNKNOWN
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                and node.value.attr == "parents" and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, int):
            v = self._ev(node.value.value)
            if v is _UNKNOWN:
                return _UNKNOWN
            for _ in range(node.slice.value + 1):
                v = posixpath.dirname(v)
            return v
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = self._ev(node.left)
            tail = self._const(node.right)
            if left is _UNKNOWN or tail is None:
                return _UNKNOWN
            return posixpath.join(left, tail)
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
            if name in ("resolve", "absolute") and isinstance(fn, ast.Attribute) and not node.args:
                return self._ev(fn.value)
            if name in ("Path", "PurePath", "abspath", "realpath") and len(node.args) >= 1:
                v = self._ev(node.args[0])
                for extra in node.args[1:]:
                    t = self._const(extra)
                    if v is _UNKNOWN or t is None:
                        return _UNKNOWN
                    v = posixpath.join(v, t)
                return v
            if name == "dirname" and len(node.args) == 1:
                v = self._ev(node.args[0])
                return _UNKNOWN if v is _UNKNOWN else posixpath.dirname(v)
            if name == "join" and node.args:
                v = self._ev(node.args[0])
                if v is _UNKNOWN:
                    return _UNKNOWN
                for extra in node.args[1:]:
                    t = self._const(extra)
                    if t is None:
                        return _UNKNOWN
                    v = posixpath.join(v, t)
                return v
            if name == "joinpath" and isinstance(fn, ast.Attribute):
                v = self._ev(fn.value)
                for extra in node.args:
                    t = self._const(extra)
                    if v is _UNKNOWN or t is None:
                        return _UNKNOWN
                    v = posixpath.join(v, t)
                return v
        return _UNKNOWN


def _has_non_ascii_const(node) -> Optional[str]:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and not n.value.isascii():
            return n.value
    return None


def _looks_like_rel_path(s: str) -> bool:
    return bool(s) and not _WS_RE.search(s) and not _PROSE_RE.search(s) \
        and ("/" in s or _EXT_RE.search(s) is not None) \
        and not s.startswith(("http://", "https://"))


def _looks_like_file_name(s: str) -> bool:
    """容器规则只收**文件名**:必须以扩展名结尾(表里的「说明」「选项」再短也不带扩展名)。"""
    return _looks_like_rel_path(s) and _EXT_RE.search(s) is not None


def _call_name(node: ast.Call) -> str:
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")


# ══════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════

def enumerate_sources(sources: Dict[str, str], tree: Tree) -> List[Finding]:
    """纯函数:给定源码与树,返回全部发现。真判据、牙证、对照臂都走它。"""
    raw: List[Tuple[str, int, str, str, str]] = []   # (source, line, literal, shape, resolved)
    for src_path, text in sorted(sources.items()):
        try:
            mod = ast.parse(text)
        except SyntaxError:
            continue
        # 模块级锚定常量
        bases: Dict[str, str] = {}
        ev = _Eval(src_path, bases)
        for stmt in mod.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                p = ev.path(stmt.value)
                if p is not None and any(isinstance(n, ast.Name) and n.id == "__file__"
                                         for n in ast.walk(stmt.value)):
                    bases[stmt.targets[0].id] = p
                    lit = _has_non_ascii_const(stmt.value)
                    if lit:
                        raw.append((src_path, stmt.lineno, lit, "anchored", p))

        dynamic_tail_dirs: Dict[str, List[ast.AST]] = {}   # 锚定目录 → 用了动态尾巴的那些函数/节点
        parents: Dict[ast.AST, ast.AST] = {}
        for node in ast.walk(mod):
            for ch in ast.iter_child_nodes(node):
                parents[ch] = node

        for node in ast.walk(mod):
            # ① 锚定表达式(函数体内的也算):BinOp(/) 或 join/joinpath 调用,且整式可求值、含非 ASCII 常量
            if isinstance(node, (ast.BinOp, ast.Call)) and not isinstance(parents.get(node), (ast.BinOp,)):
                p = ev.path(node)
                if p is not None and any(isinstance(n, ast.Name) and (n.id == "__file__" or n.id in bases)
                                         for n in ast.walk(node)):
                    lit = _has_non_ascii_const(node)
                    stmt = parents.get(node)
                    top_assign = isinstance(stmt, ast.Assign) and stmt in mod.body
                    if lit and not top_assign:
                        raw.append((src_path, node.lineno, lit, "anchored", p))
            # ② 动态尾巴:<锚定目录> / <非常量>、join(<锚定目录>, <非常量>)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) \
                    and not (isinstance(node.right, ast.Constant)):
                base = ev.path(node.left)
                if base is not None:
                    dynamic_tail_dirs.setdefault(base, []).append(node)
            if isinstance(node, ast.Call) and _call_name(node) == "join" and node.args:
                base = ev.path(node.args[0])
                if base is not None and any(not isinstance(a, ast.Constant) for a in node.args[1:]):
                    dynamic_tail_dirs.setdefault(base, []).append(node)
            # ③ 仓根相对常量  ④ 路径函数的直接参数
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and not node.value.isascii():
                s = node.value
                par = parents.get(node)
                if isinstance(par, ast.JoinedStr):
                    continue
                first = s.split("/", 1)[0]
                if "/" in s and first in tree.top_dirs and _looks_like_rel_path(s):
                    raw.append((src_path, node.lineno, s, "repo_relative", _norm(s)))
                elif isinstance(par, ast.Call) and node in par.args and _call_name(par) in _PATH_FUNCS \
                        and _looks_like_rel_path(s):
                    raw.append((src_path, node.lineno, s, "call_arg", _norm(s)))

        # ② 续:动态尾巴的锚定目录 × 本模块模块级容器里的非 ASCII **文件名**
        groups: List[Tuple[int, str, List[str], str]] = []   # (行, 字面, 候选[], shape)
        if dynamic_tail_dirs:
            container_strs: List[Tuple[int, str]] = []
            for stmt in mod.body:
                value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
                if isinstance(value, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
                    for n in ast.walk(value):
                        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                                and not n.value.isascii() and _looks_like_file_name(n.value):
                            container_strs.append((n.lineno, n.value))
            for base in sorted(dynamic_tail_dirs):
                for ln, s in container_strs:
                    raw.append((src_path, ln, s, "container", _norm(posixpath.join(base, s))))
            groups = _fstring_groups(mod, parents, sorted(dynamic_tail_dirs), tree)

        for ln, lit, cands, shape in groups:
            hit = next((c for c in cands if tree.kind(c) == "file"), None)
            raw.append((src_path, ln, lit, shape, hit or cands[0], tuple(cands) if len(cands) > 1 else ()))

    out: List[Finding] = []
    seen = set()
    for rec in raw:
        src_path, line, lit, shape, resolved = rec[:5]
        alts = rec[5] if len(rec) > 5 else ()
        key = (src_path, line, lit, resolved)
        if key in seen:
            continue
        seen.add(key)
        kind = tree.kind(resolved)
        twin = False
        if kind == "missing":
            twin = any(cp437_twin(c) != c and tree.kind(cp437_twin(c)) != "missing"
                       for c in (alts or (resolved,)))
        out.append(Finding(src_path, line, lit, shape, resolved, kind, twin, alts))
    return out


def _module_str_consts(mod: ast.Module) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for stmt in mod.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) \
                and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            out[stmt.targets[0].id] = stmt.value.value
    return out


def _fstring_groups(mod, parents, bases: List[str], tree: Tree) -> List[Tuple[int, str, List[str], str]]:
    """f-string 文件名模板 → 候选路径。

    · 占位是 Name 时,代入**同模块**里名字含该占位名(不分大小写)的模块级字符串常量 ——
      `f"服务商申请协议_{version}.md"` + `CURRENT_AGREEMENT_VERSION = "v2.3"` ⇒ `服务商申请协议_v2.3.md`。
      这是「代码当下会读的那个文件」:版本号改了而文件没跟上 ⇒ 候选全不存在 ⇒ 红(版本漂移)。
    · 同一个元组/列表里的几个模板 = **按顺序取第一个存在的**(`for _name in (A, B)` 回退写法),
      组内有一个存在就算满足;全不存在才红。
    · 代入不了(占位不是 Name / 同模块没有匹配常量)⇒ 退回通配,只展开树里**已存在**的匹配(`fstring_glob`)。
    """
    consts = _module_str_consts(mod)
    templates: List[Tuple[ast.JoinedStr, str, List[str]]] = []
    for node in ast.walk(mod):
        if not isinstance(node, ast.JoinedStr):
            continue
        consts_parts = [v.value for v in node.values if isinstance(v, ast.Constant)]
        glob = "".join(v.value if isinstance(v, ast.Constant) else "*" for v in node.values)
        if "".join(consts_parts).isascii() or not _EXT_RE.search(glob) or _WS_RE.search(glob) \
                or _PROSE_RE.search(glob.replace("*", "")) or "/" in glob:
            continue
        names = [v.value.id for v in node.values
                 if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name)]
        subs: List[str] = []
        if names and len(names) == sum(isinstance(v, ast.FormattedValue) for v in node.values):
            vals = [[c for k, c in consts.items() if n.lower() in k.lower()] for n in names]
            if all(len(v) == 1 for v in vals):
                it = iter([v[0] for v in vals])
                subs = ["".join(v.value if isinstance(v, ast.Constant) else next(it) for v in node.values)]
        templates.append((node, glob, subs))

    out: List[Tuple[int, str, List[str], str]] = []
    by_parent: Dict[int, List[Tuple[ast.JoinedStr, str, List[str]]]] = {}
    for t in templates:
        par = parents.get(t[0])
        key = id(par) if isinstance(par, (ast.Tuple, ast.List)) else id(t[0])
        by_parent.setdefault(key, []).append(t)
    for base in bases:
        if tree.kind(base) != "dir":
            continue
        for grp in by_parent.values():
            if all(subs for _, _, subs in grp):
                cands = [posixpath.join(base, s) for _, _, subs in grp for s in subs]
                lit = " | ".join(g for _, g, _ in grp)
                out.append((grp[0][0].lineno, lit, cands, "fstring"))
            for node, glob, _subs in grp:
                for f in tree.children(base):
                    if fnmatch.fnmatchcase(f.rsplit("/", 1)[1], glob):
                        out.append((node.lineno, glob, [f], "fstring_glob"))
    return out


def required_paths(findings: Sequence[Finding], tree: Tree) -> List[str]:
    """镜像里必须存在的仓内路径:文件本身;目录记为 `dir/`(镜像门要求存在且非空)。

    目录**不展开**成它下面的每个文件 —— 代码是按目录读的(`os.listdir`),它要的是「目录在、里面有东西」;
    具体文件名由 f-string / 容器规则各自登记。
    """
    req: Set[str] = set()
    for f in findings:
        if f.kind == "file":
            req.add(f.resolved)
        elif f.kind == "dir":
            req.add(f.resolved + "/")
    return sorted(req)


# ══════════════════════════════════════════════════════════════════
# 取数:HEAD 的树 / 进镜像的运行时 .py
# ══════════════════════════════════════════════════════════════════

def _git(*args: str, stdin: Optional[bytes] = None) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True,
                          input=stdin).stdout


def dockerignore_matcher():
    """WO_268 那把对拍过真 BuildKit 的匹配器。按**文件路径**加载 —— 不依赖 sys.path 里有没有仓根
    (`pytest` 与 `python -m pytest` 下 `tests` 能不能当包导入不一样)。"""
    import importlib.util
    name = "_wo268_dockerignore"
    if name in sys.modules:
        return sys.modules[name]
    path = ROOT / "tests" / "partner_agreement_files_2026_09_23" / "_dockerignore.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclass 建类时要从 sys.modules 取本模块
    spec.loader.exec_module(mod)
    return mod


def head_tree_blobs(rev: str = "HEAD") -> Dict[str, str]:
    """HEAD 的树:路径 → blob sha(不读工作区)。"""
    out: Dict[str, str] = {}
    for rec in _git("ls-tree", "-r", "-z", rev).split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            out[path.decode("utf-8")] = meta.split()[2].decode()
    return out


def head_tree_files(rev: str = "HEAD") -> List[str]:
    return sorted(head_tree_blobs(rev))


def _read_blobs(shas: Sequence[str]) -> Dict[str, bytes]:
    """一次 `git cat-file --batch` 读完全部 blob(逐个 `git show` 要 1254 个子进程、近 40 秒)。"""
    raw = _git("cat-file", "--batch", stdin=("\n".join(shas) + "\n").encode())
    out: Dict[str, bytes] = {}
    i = 0
    while i < len(raw):
        nl = raw.index(b"\n", i)
        head = raw[i:nl].split()
        sha, size = head[0].decode(), int(head[2])
        out[sha] = raw[nl + 1: nl + 1 + size]
        i = nl + 1 + size + 1
    return out


def runtime_sources(blobs: Dict[str, str], dockerignore_text: str) -> Dict[str, str]:
    ctx = dockerignore_matcher().build_context_files(list(blobs), dockerignore_text)
    paths = sorted(p for p in ctx if p.endswith(".py") and not p.startswith(NON_RUNTIME_PREFIXES))
    data = _read_blobs([blobs[p] for p in paths])
    return {p: data[blobs[p]].decode("utf-8", "replace") for p in paths}


def enumerate_head(rev: str = "HEAD"):
    blobs = head_tree_blobs(rev)
    files = sorted(blobs)
    dockerignore = _git("show", "%s:.dockerignore" % rev).decode("utf-8")
    tree = Tree(files)
    sources = runtime_sources(blobs, dockerignore)
    findings = enumerate_sources(sources, tree)
    return files, dockerignore, tree, sources, findings


def render_required_list(req: Sequence[str]) -> str:
    head = ("# 运行时必需的非 ASCII 仓内路径 —— 由 tests/runtime_nonascii_paths_2026_09_23/_enumerate.py 生成,勿手改\n"
            "# (WO_272)Deploy 镜像门在无挂载容器里逐条检查:文件 test -s(非空);目录(以 / 结尾)test -d 且非空\n"
            "# 重新生成:python -m tests.runtime_nonascii_paths_2026_09_23._enumerate --write\n")
    return head + "".join(p + "\n" for p in req)


def parse_required_list(text: str) -> List[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def main(argv: Sequence[str]) -> int:
    """三态:0 清单与枚举一致且无红 · 1 有红(路径缺失 / 乱码孪生)或清单与枚举不一致 · 3 没跑成。"""
    try:
        files, _di, tree, sources, findings = enumerate_head()
    except Exception as exc:                      # noqa: BLE001 —— 取不到树/源码 = 没跑成,不是绿
        print("[runtime-nonascii-paths] 没跑成(rc=3):%s" % exc)
        return 3
    if not sources or not findings:
        print("[runtime-nonascii-paths] 没跑成(rc=3):源码 %d 个 / 发现 %d 条 —— 空读数不是绿" %
              (len(sources), len(findings)))
        return 3
    req = required_paths(findings, tree)
    print("运行时 .py %d 个 · 按路径读取的非 ASCII 发现 %d 条 · 必需路径 %d 条" %
          (len(sources), len(findings), len(req)))
    for f in findings:
        mark = {"file": "OK ", "dir": "OK ", "missing": "RED"}[f.kind]
        print("  %s %-7s %-13s %s:%d  %r → %s%s" % (mark, f.kind, f.shape, f.source, f.line, f.literal,
                                                     f.resolved, "  (树里只有 cp437 乱码形)" if f.mojibake_twin else ""))
    target = ROOT / REQUIRED_LIST_REL
    if "--write" in argv:
        target.write_bytes(render_required_list(req).encode("utf-8"))
        print("已写 %s(%d 条)" % (REQUIRED_LIST_REL, len(req)))
    reds = [f for f in findings if f.kind == "missing"]
    rc = 1 if reds else 0
    if "--check" in argv:
        if not target.exists():
            print("[runtime-nonascii-paths] 没跑成(rc=3):%s 不存在" % REQUIRED_LIST_REL)
            return 3
        committed = parse_required_list(target.read_text(encoding="utf-8"))
        if not committed:
            print("[runtime-nonascii-paths] 没跑成(rc=3):%s 解析为空" % REQUIRED_LIST_REL)
            return 3
        if committed != req:
            print("清单与枚举不一致:多 %r 少 %r" % (sorted(set(committed) - set(req)),
                                              sorted(set(req) - set(committed))))
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
