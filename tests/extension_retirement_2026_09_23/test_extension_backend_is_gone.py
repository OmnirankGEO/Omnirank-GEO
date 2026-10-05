# -*- coding: utf-8 -*-
"""WO_273 ② · 浏览器插件后端「不许悄悄回来」三把锁,每把都配牙证(该红必红)与对照臂(没动过的输入必绿)。

  A. **路由缺席**(Review 09-23 裁定 ②):从真导入的 `server.app.routes` 取,不 grep 源码 ——
     不许有任何 path 以 `/api/extension` 开头,也不许有 `/ws/extension`。
     (`auth/middleware.py` 的 PUBLIC_PATHS 里还留着两条指向插件的死白名单;这把锁保证它们
      永远只是「放行一个 404」,不会悄悄变成一个真的公开端点。)
  B. **模块缺席**:插件后端三个模块与两个手工脚本不在库里;全仓 .py 对三个模块的导入为 0 ——
     import / from-import / cache 包内相对导入 / importlib.import_module 与 __import__ 的字符串,都算。
     🔴 Deploy 09-23 指出的坑:生产 /app/cache 整个挂的是运行时根,那里躺着一份 5 月的旧
        extension_state.py;release 的逐文件只读覆盖一撤,它就在容器里露出来。哪里残留一处导入,
        就会**静默加载四个月前的代码**,而不是 ImportError —— 所以要的是「导入为 0」,不是「导不进就算」。
  C. **非测试代码文本锁**(工单 §C.5):`api.extension_api|/api/extension` 在非测试代码里为 0。
     豁免**只有一条**:保护文件 `auth/middleware.py` 的两个 PUBLIC_PATHS 字面量,带机器可核断言(见 EXEMPT)。
     (已应用迁移 039 第 5 行注释原也点名插件后端 —— Review 裁定改措辞、不开豁免。)
     前端 `frontend/src` 归 A 的锁(同一模式计数为 0),两边合起来 = 全仓非测试代码。
  D. **对用户说的话**(A 09-23 核出 ②):帮助中心文档不再提插件(个人设置那篇原写「插件授权状态」)。

冷启动建表那一半见同包 `test_publish_records_ddl_moved_verbatim.py`。
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# 🔴 退役模块名**运行时拼接**:本文件自己也在 B 的扫描范围里,写成完整字面量会把锁自己扫成嫌疑人
#    (本仓写入方普查同一处置:收紧匹配面,而不是把自己加进豁免)。
_P = "extension"
RETIRED_MODULES = ("api." + _P + "_api", "cache." + _P + "_state", "db." + _P + "_db")
RETIRED_FILES = (
    "api/" + _P + "_api.py",
    "cache/" + _P + "_state.py",
    "db/" + _P + "_db.py",
    "scripts/manual/" + _P + "_state_fallback.py",
    "scripts/manual/" + _P + "_state_multiworker.py",
)
_DYNAMIC_IMPORTERS = {"import_module", "__import__", "find_spec"}


def _tracked(*pathspec: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *pathspec],
                         capture_output=True, check=True).stdout
    return [p.decode("utf-8") for p in out.split(b"\0") if p]


# ===========================================================================
# A · 路由缺席
# ===========================================================================

def _extension_routes(routes) -> list[str]:
    bad = []
    for r in routes:
        path = getattr(r, "path", "") or ""
        if path.startswith("/api/" + _P) or path == "/ws/" + _P:
            bad.append(path)
    return bad


def test_no_extension_route_is_registered_in_the_real_app():
    os.environ.setdefault("GEO_SKIP_STARTUP_TASKS", "1")
    import server

    routes = list(server.app.routes)
    assert len(routes) >= 500, f"分母自证:真 app 只注册了 {len(routes)} 条路由 —— server 可能没导全"
    assert _extension_routes(routes) == []


def test_route_scanner_has_teeth_and_spares_the_ws_wildcard():
    """牙证:合成 app 挂上插件那两种路由必被点名。对照臂:通配 WS 与相邻前缀不许误伤。"""
    from fastapi import APIRouter, FastAPI, WebSocket

    app = FastAPI()
    r = APIRouter()

    @r.get("/api/" + _P + "/status")
    def _status():
        return {}

    @r.websocket("/ws/" + _P)
    async def _ws(websocket: WebSocket):
        return None

    @r.get("/api/publish/records")
    def _ok():
        return {}

    @r.websocket("/ws/{session_id}")
    async def _wild(websocket: WebSocket, session_id: str):
        return None

    app.include_router(r)
    assert sorted(_extension_routes(app.routes)) == sorted(["/api/" + _P + "/status", "/ws/" + _P])


# ===========================================================================
# B · 模块缺席
# ===========================================================================

def _resolve_from(rel: str, node: ast.ImportFrom) -> str:
    """把 ImportFrom 还原成绝对模块名(相对导入按文件所在包算)。"""
    if not node.level:
        return node.module or ""
    pkg = rel.rsplit("/", 1)[0].split("/") if "/" in rel else []
    base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
    return ".".join(base + ([node.module] if node.module else []))


def _imports_of(rel: str, src: str, modules: tuple[str, ...]) -> list[str]:
    """一个 .py 文件里对 `modules` 的全部导入形状。退役判据与在役对照走**同一个**函数。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    def _hit(name: str) -> bool:
        return any(name == m or name.startswith(m + ".") for m in modules)

    hits: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if _hit(a.name):
                    hits.append(f"{rel}:{n.lineno} import {a.name}")
        elif isinstance(n, ast.ImportFrom):
            mod = _resolve_from(rel, n)
            for a in n.names:
                full = f"{mod}.{a.name}" if mod else a.name
                if _hit(mod) or _hit(full):
                    hits.append(f"{rel}:{n.lineno} from {mod} import {a.name}")
        elif isinstance(n, ast.Call) and n.args:
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            a0 = n.args[0]
            if name in _DYNAMIC_IMPORTERS and isinstance(a0, ast.Constant) and isinstance(a0.value, str) \
                    and _hit(a0.value):
                hits.append(f"{rel}:{n.lineno} {name}({a0.value!r})")
    return hits


def _retired_imports(rel: str, src: str) -> list[str]:
    return _imports_of(rel, src, RETIRED_MODULES)


def test_retired_files_are_not_in_the_tree():
    present = [p for p in RETIRED_FILES if _tracked(p) or (ROOT / p).exists()]
    assert present == [], present


def test_no_python_file_imports_a_retired_module():
    files = _tracked("*.py")
    assert len(files) >= 2000, f"分母自证:只枚举到 {len(files)} 个 .py"
    hits: list[str] = []
    live: list[str] = []
    for rel in files:
        try:
            src = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits += _retired_imports(rel, src)
        live += _imports_of(rel, src, ("cache.redis_client",))
    # 对照臂(同一函数、同一批文件):在役的 cache 模块导入必须看得见 —— 否则「零命中」可能只是扫描器瞎了
    assert len(live) >= 3, f"对照臂:只看见 {len(live)} 处在役 cache.redis_client 导入"
    assert hits == [], hits


@pytest.mark.parametrize("rel,src", [
    ("services/a.py", "import cache." + _P + "_state\n"),
    ("services/b.py", "import cache." + _P + "_state as es\n"),
    ("services/c.py", "from cache import " + _P + "_state\n"),
    ("services/d.py", "from cache." + _P + "_state import pubres_get\n"),
    ("cache/e.py", "from . import " + _P + "_state\n"),
    ("cache/f.py", "from ." + _P + "_state import pubres_set\n"),
    ("services/g.py", "import importlib\nimportlib.import_module('cache." + _P + "_state')\n"),
    ("services/h.py", "__import__('api." + _P + "_api')\n"),
    ("services/i.py", "from api." + _P + "_api import router\n"),
    ("services/j.py", "from db import " + _P + "_db\n"),
])
def test_import_scanner_catches_every_shape(rel, src):
    assert _retired_imports(rel, src), f"这种导入形状没被抓到:{src!r}"


@pytest.mark.parametrize("rel,src", [
    ("services/k.py", "from cache import redis_client, progress_bus\n"),
    ("cache/l.py", "from . import leader_lock\n"),
    ("services/m.py", "import importlib\nimportlib.import_module('cache.redis_client')\n"),
    ("services/n.py", "NAME = 'cache." + _P + "_state'  # 只是个字符串,不是导入\n"),
    ("services/o.py", "from services." + _P + "_platform_registry import url_is_within_platform_domain\n"),
])
def test_import_scanner_spares_live_modules_and_plain_strings(rel, src):
    assert _retired_imports(rel, src) == [], src


# ===========================================================================
# C · 非测试代码文本锁
# ===========================================================================

LOCK_RE = re.compile(r"api.extension_api|/api/extension")

#: 非测试代码 = 库里全部文件,减去下面四类(逐条写理由,不许顺手多排)。
def _is_out_of_domain(rel: str) -> str | None:
    if rel.startswith("tests/"):
        return "测试代码"
    if rel.startswith("scripts/") and rel.rsplit("/", 1)[-1].startswith("test_") and rel.endswith(".py"):
        return "测试代码(pytest.ini testpaths 含 scripts)"
    if rel.startswith("frontend/"):
        return "前端归 A 的锁(frontend/src 同一模式计数为 0);frontend/tests 是测试代码"
    if rel.startswith("docs/") or rel.endswith(".md"):
        return "文档 / 历史记录"
    if rel.startswith("agent-test-artifacts"):
        return "历史测试产物(接口响应存档)"
    return None


#: 豁免:文件 → (精确命中数, 机器可核断言, 理由)。数多一处、少一处都红 —— 豁免不许比它的理由活得久。
def _middleware_literals_only(text: str) -> bool:
    tree = ast.parse(text)
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PUBLIC_PATHS" for t in n.targets):
            vals = {e.value for e in getattr(n.value, "elts", []) if isinstance(e, ast.Constant)}
            return {v for v in vals if isinstance(v, str) and LOCK_RE.search(v)} == {
                "/api/" + _P + "/bindcode/verify", "/api/" + _P + "/config"}
    return False


#: 🔴 只此一条。已应用迁移 039 第 5 行那句 SQL 注释原来也点名插件后端;Review 09-23 裁定 A:
#:    改那一行的措辞(SQL 逐字节不变),不给仪器开豁免 —— 类目豁免 = 永久盲区。
EXEMPT = {
    "auth/middleware.py": (
        2, _middleware_literals_only,
        "保护文件零改动(Review 09-23 裁定 ② 选 A):PUBLIC_PATHS 两条死白名单,留给 auth/middleware.py 清理专单;"
        "上面 A 锁保证它们只能放行 404"),
}


def _lock_violations(files: dict[str, str]) -> list[str]:
    bad: list[str] = []
    for rel, text in files.items():
        if _is_out_of_domain(rel):
            continue
        n = len(LOCK_RE.findall(text))
        if rel in EXEMPT:
            want, check, _why = EXEMPT[rel]
            if n != want or not check(text):
                bad.append(f"{rel}: 豁免条件不再成立(命中 {n},登记 {want})")
        elif n:
            bad.append(f"{rel}: {n} 处")
    return bad


def test_non_test_code_has_no_extension_reference_beyond_the_middleware_exemption():
    files: dict[str, str] = {}
    for rel in _tracked():
        if _is_out_of_domain(rel):
            continue
        try:
            files[rel] = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # 二进制
    assert len(files) >= 1500, f"分母自证:域内只读到 {len(files)} 个文本文件"
    assert set(EXEMPT) <= set(files), f"豁免文件不在域内:{set(EXEMPT) - set(files)}"
    assert _lock_violations(files) == []


def test_text_lock_has_teeth_and_exemptions_are_exact():
    mw = (ROOT / "auth" / "middleware.py").read_text(encoding="utf-8")
    mig_rel = "scripts/migration_publish_records_url_verification_2026_08_19.sql"
    mig = (ROOT / mig_rel).read_text(encoding="utf-8")
    base = {"auth/middleware.py": mw, mig_rel: mig}
    assert _lock_violations(base) == [], "对照臂:没动过的豁免文件与改过措辞的迁移 039 必绿"
    # 牙证 ① 非豁免文件出现任一形态即红
    for text in ("x = '/api/" + _P + "/status'\n", "from api." + _P + "_api import router\n",
                 "curl $B/api/" + _P + "/profiles\n"):
        assert _lock_violations({"services/x.py": text, "scripts/y.sh": text}), text
    # 牙证 ② 豁免文件多一处即红(计数精确)
    assert _lock_violations({"auth/middleware.py": mw + "\n# /api/" + _P + "/x\n"})
    # 牙证 ③ 迁移 039 不在豁免里:那句注释改回原措辞(哪怕仍是 `--` 注释行)即红
    assert _lock_violations({mig_rel: mig + "\n-- (api/" + _P + "_api.py)\n"})
    # 对照臂:测试 / 文档 / 前端出现不算(归别的锁或本就是历史)
    assert _lock_violations({"tests/a.py": "/api/" + _P, "docs/b.md": "/api/" + _P,
                             "frontend/src/c.ts": "/api/" + _P}) == []
    # 对照臂:同名但在役的模块不许被误伤
    assert _lock_violations({"services/z.py": "from services." + _P + "_platform_registry import X\n"}) == []


# ===========================================================================
# D · 帮助中心不再提插件(A 09-23 核出 ②)
# ===========================================================================

HELP_DOCS = ROOT / "api" / "help_docs_content.json"


def _docs_mentioning_plugin(docs: dict) -> list[str]:
    return sorted(k for k, v in docs.items()
                  if isinstance(v, dict) and "插件" in (v.get("title", "") + v.get("body", "")))


def test_help_center_no_longer_mentions_the_plugin():
    docs = json.loads(HELP_DOCS.read_text(encoding="utf-8"))
    assert len(docs) >= 40, f"分母自证:只读到 {len(docs)} 篇帮助文档"
    assert _docs_mentioning_plugin(docs) == []
    # 对照臂:「个人设置」那篇还在,账户一节仍讲算力余额 —— 删的是插件那半句,不是整行 / 整篇
    profile = [v for v in docs.values() if isinstance(v, dict) and v.get("title") == "个人设置"]
    assert len(profile) == 1 and "**账户** — 算力余额" in profile[0]["body"], "对照臂:个人设置的账户一节被一起删了"


def test_help_scanner_has_teeth():
    assert _docs_mentioning_plugin({"a": {"title": "个人设置", "body": "算力余额 + 插件授权状态"},
                                    "b": {"title": "注册", "body": "手机号登录"}}) == ["a"]
    assert _docs_mentioning_plugin({"b": {"title": "注册", "body": "手机号登录"}}) == []
