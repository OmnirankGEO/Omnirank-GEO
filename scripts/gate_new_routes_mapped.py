#!/usr/bin/env python3
"""新增路由归属门(preflight 5-e · 静态 · 三态):本包**新增**的每条非公开 /api 路由,
都必须在待发树的 `auth/module_mapping.py` 里有归属。

## 为什么要这一把
全局 RBAC 中间件对未映射路由 fail-closed:管理员 200,服务商 / 客户一律 403 UNMAPPED_ROUTE。
0913AB 的 `/api/industry-taxonomy`(WO_267 新增、没登记)就是这么上线的 —— 三笔交付、
Review 分支核验、MUST_RUN + ONESHOT 全绿、preflight PASS,**全都没看见**,上线后真测才抓到(WO_286)。
C 的 WO_287 结构门起真 app 遍历 app.routes(随 ONESHOT 跑、要库);本门是它的**静态前哨**:
不起 app、不要库,preflight 每班都能跑,只管「本包新加了什么」。

## 怎么算(只读 git 对象,不读工作树)
  · 路由 = 每个非测试 .py 里 `X = APIRouter(prefix=...)` 的前缀 + `@X.<method>(path)` / `@app.<method>(path)`;
    09-24 全仓实测:没有带 prefix 的 include_router、没有 router 套 router、APIRouter 的 prefix 全是常量
    ⇒ 完整路径 = 前缀 + 装饰器路径。算不出来的(非常量前缀 / 路径、认不出的装饰器对象)单独记下,
    **它若是本包新增的 ⇒ rc=3**(判不了 ≠ 没问题)。
  · [AG 补] 同文件里 `-> APIRouter` 的工厂函数,其调用结果也算路由器;文件有模块级 `MOUNT_PATH = "<字面量>"`
    (独立子应用由最外层分流器挂在那里)⇒ 该文件路由器完整路径前面再加 MOUNT_PATH。
    工厂路由器所在文件没声明 MOUNT_PATH / MOUNT_PATH 非字面量 / 工厂体内自带 prefix / MOUNT_PATH 文件里有 @app 路由 ⇒ 算不出。
  · 新增 = 待发树的路由集合 − 基准(待发树与 --base 的 merge-base)的路由集合,按完整路径比。
  · 公开判定逐字镜像 `auth/middleware.py`:PUBLIC_PATHS 精确匹配;PUBLIC_PREFIXES 前缀但
    PORTAL_PROTECTED_PREFIXES 除外;PUBLIC_SUFFIXES 只对纯 GET/HEAD 路由放行。
  · 归属用**待发树**的 `resolve_permission`(模块只 import typing,原样 exec);`{参数}` 段填占位再解析。
  · 刻意只给 admin 的,写进 C 的允许名单 `tests/route_permission_mapping_2026_09_24/admin_only_allowlist.tsv`
    ⇒ 本门打 ⚠️ 点名放行(那张表的纪律由 WO_287 管)。

## 退出码(三态)
  0 = 本包新增的非公开 /api 路由都有归属(或 0 条新增)
  1 = 有新增路由落到 __unmapped__(上线即 403 UNMAPPED_ROUTE)
  3 = 没跑成:取不到树 / 解析不了 / 映射表或白名单读不出 / 分母塌了 / 新增路由里有算不出完整路径的
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

MAPPING_FILE = "auth/module_mapping.py"
MIDDLEWARE_FILE = "auth/middleware.py"
ALLOW_FILE = "tests/route_permission_mapping_2026_09_24/admin_only_allowlist.tsv"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
SKIP_DIRS = ("tests/", "scripts/", "docs/", "frontend/", "agent-test-artifacts")
PROBE_UNMAPPED = "/api/__gate_new_routes_probe_unregistered__"

REPO = ""


class CantRun(Exception):
    pass


def git(*args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", errors="replace")


def show(ref: str, path: str) -> str | None:
    rc, out = git("show", f"{ref}:{path}")
    return out if rc == 0 else None


def route_files(ref: str) -> list[str]:
    # include_router 也要进候选:前提锁(带 prefix / 嵌套)得先扫得到那个文件
    rc, out = git("grep", "-l", "-E", r"APIRouter\(|@app\.|include_router\(", ref, "--", "*.py")
    if rc not in (0, 1):
        raise CantRun(f"git grep 在 {ref} 上失败")
    files = []
    for line in out.splitlines():
        p = line.split(":", 1)[1] if ":" in line else line
        if not p.startswith(SKIP_DIRS) and "/tests/" not in p:
            files.append(p)
    return sorted(files)


def _const_str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def extract(ref: str) -> tuple[dict[str, set[str]], list[str]]:
    """返回 ({完整路径: {方法}}, [算不出的条目])。"""
    routes: dict[str, set[str]] = {}
    unresolved: list[str] = []
    for f in route_files(ref):
        src = show(ref, f)
        if src is None:
            raise CantRun(f"{ref}:{f} 读不到")
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            raise CantRun(f"{ref}:{f} 解析不了:{e}")
        prefixes: dict[str, str | None] = {"app": ""}
        # [AG · 工厂路由器 + 子应用挂载点] ① 同文件里返回注解为 APIRouter 的工厂函数,其调用结果也算路由器
        #   (前缀取调用里的 prefix,没有就 "";工厂体内自己的 APIRouter(...) 带 prefix ⇒ 看不见 ⇒ 算不出)。
        #   ② 文件有模块级 `MOUNT_PATH = "<字面量>"` ⇒ 该文件所有路由器完整路径 = MOUNT_PATH + 前缀 + 装饰器路径
        #   (最外层 ASGI 分流器把 MOUNT_PATH 下的请求去前缀交给子应用)。
        #   ③ 工厂路由器所在文件没声明 MOUNT_PATH ⇒ 算不出(新增的 ⇒ rc 3,不猜挂在哪)。
        factories: dict[str, bool] = {}  # 工厂名 → 工厂体内的 APIRouter 是否自带 prefix
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
                r = node.returns
                rname = r.id if isinstance(r, ast.Name) else r.attr if isinstance(r, ast.Attribute) else \
                    r.value if isinstance(r, ast.Constant) and isinstance(r.value, str) else ""
                if rname == "APIRouter":
                    factories[node.name] = any(
                        isinstance(c, ast.Call) and (getattr(c.func, "id", None) == "APIRouter"
                                                     or getattr(c.func, "attr", None) == "APIRouter")
                        and any(kw.arg == "prefix" for kw in c.keywords)
                        for c in ast.walk(node))
        mount: str | None | bool = False  # False = 没声明;None = 声明了但不是字面量
        for node in tree.body:
            tgt = (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else
                   node.target if isinstance(node, ast.AnnAssign) else None)
            if isinstance(tgt, ast.Name) and tgt.id == "MOUNT_PATH" and node.value is not None:
                mount = _const_str(node.value)
        factory_routers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                fn = node.value.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                if name == "APIRouter" or (isinstance(fn, ast.Name) and name in factories):
                    pre = ""
                    for kw in node.value.keywords:
                        if kw.arg == "prefix":
                            pre = _const_str(kw.value)
                    if name != "APIRouter" and factories[name]:
                        pre = None
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            prefixes[t.id] = pre
                            if name != "APIRouter":
                                factory_routers.add(t.id)
        if mount is not False:
            for k in list(prefixes):
                if k == "app":
                    continue
                prefixes[k] = None if (mount is None or prefixes[k] is None) else mount + prefixes[k]
        for node in ast.walk(tree):
            # 静态完整路径成立的前提:include_router 不带 prefix、只挂在 app 上(09-24 全仓 0 处例外)。
            # 本包若引入例外,完整路径就算错了 ⇒ 记成算不出(新增的 ⇒ rc 3),不静默少算。
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "include_router"):
                owner = node.func.value.id if isinstance(node.func.value, ast.Name) else "?"
                if owner != "app" or any(kw.arg == "prefix" for kw in node.keywords):
                    unresolved.append(f"{f}:{node.lineno} {owner}.include_router(…prefix/嵌套)"
                                      " —— 静态完整路径的前提被打破")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                attr = dec.func.attr
                if attr not in HTTP_METHODS and attr != "api_route":
                    continue
                owner = dec.func.value.id if isinstance(dec.func.value, ast.Name) else None
                path = _const_str(dec.args[0]) if dec.args else None
                for kw in dec.keywords:
                    if kw.arg == "path":
                        path = _const_str(kw.value)
                methods: set[str] = {attr.upper()} if attr != "api_route" else set()
                if attr == "api_route":
                    for kw in dec.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
                            methods = {m.upper() for m in (_const_str(e) for e in kw.value.elts) if m}
                    methods = methods or {"GET"}
                if owner is None or owner not in prefixes:
                    # 认不出的装饰器对象:只在「看起来像路由」(路径以 / 开头)时记
                    if path and path.startswith("/"):
                        unresolved.append(f"{f}:{node.lineno} @{owner or '?'}.{attr}({path!r}) —— 装饰器对象不是本文件的 APIRouter/app")
                    continue
                if owner in factory_routers and mount is False:
                    unresolved.append(f"{f}:{node.lineno} @{owner}.{attr}({path!r}) —— 工厂路由器所在文件没声明"
                                      " MOUNT_PATH,不知道挂在哪")
                    continue
                if owner == "app" and mount is not False:
                    unresolved.append(f"{f}:{node.lineno} @app.{attr}({path!r}) —— 文件声明了 MOUNT_PATH,"
                                      "分不清 app 是宿主还是子应用")
                    continue
                pre = prefixes[owner]
                if pre is None or path is None:
                    unresolved.append(f"{f}:{node.lineno} @{owner}.{attr} —— 前缀或路径不是常量")
                    continue
                routes.setdefault(pre + path, set()).update(methods)
    return routes, unresolved


def load_mapping(ref: str):
    src = show(ref, MAPPING_FILE)
    if src is None:
        raise CantRun(f"{ref}:{MAPPING_FILE} 读不到")
    ns: dict = {}
    try:
        exec(compile(src, MAPPING_FILE, "exec"), ns)
    except Exception as e:
        raise CantRun(f"{MAPPING_FILE} 执行不了:{e}")
    fn = ns.get("resolve_permission")
    if not callable(fn):
        raise CantRun(f"{MAPPING_FILE} 里没有 resolve_permission")
    return fn, ns.get("ROUTE_PREFIX_MAP") or []


def load_public(ref: str) -> dict:
    src = show(ref, MIDDLEWARE_FILE)
    if src is None:
        raise CantRun(f"{ref}:{MIDDLEWARE_FILE} 读不到")
    want = {"PUBLIC_PATHS", "PUBLIC_PREFIXES", "PUBLIC_SUFFIXES", "PORTAL_PROTECTED_PREFIXES"}
    got: dict = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            n = node.targets[0].id
            if n in want:
                try:
                    got[n] = ast.literal_eval(node.value)
                except ValueError:
                    raise CantRun(f"{MIDDLEWARE_FILE} 的 {n} 不是字面量,镜像不了")
    if want - set(got):
        raise CantRun(f"{MIDDLEWARE_FILE} 缺 {sorted(want - set(got))} —— 公开判定镜像不了")
    return got


def is_public(pub: dict, probe: str, methods: set[str]) -> bool:
    if probe in pub["PUBLIC_PATHS"]:
        return True
    portal_protected = any(probe == p or probe.startswith(p + "/") for p in pub["PORTAL_PROTECTED_PREFIXES"])
    if probe.startswith(tuple(pub["PUBLIC_PREFIXES"])) and not portal_protected:
        return True
    return bool(methods) and methods <= {"GET", "HEAD"} and any(probe.endswith(s) for s in pub["PUBLIC_SUFFIXES"])


def allowlist(ref: str) -> set[str]:
    src = show(ref, ALLOW_FILE)
    if src is None:
        return set()
    rows = set()
    for line in src.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            cols = line.split("\t")
            if len(cols) != 3:
                raise CantRun(f"{ALLOW_FILE} 不是三栏:{line!r}")
            rows.add(cols[0])
    return rows


def main(argv: list[str] | None = None) -> int:
    global REPO
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", required=True, help="待发树")
    ap.add_argument("--base", default="github/main", help="基准(实际比的是它与 --ref 的 merge-base)")
    ap.add_argument("--min-routes", type=int, default=800, help="分母哨兵:基准上至少要抽到这么多条 /api 路由")
    a = ap.parse_args(argv)
    REPO = a.repo
    try:
        rc, head = git("rev-parse", "--verify", "-q", f"{a.ref}^{{commit}}")
        if rc:
            raise CantRun(f"--ref {a.ref} 解析不了")
        rc, base = git("merge-base", a.ref, a.base)
        if rc:
            raise CantRun(f"{a.ref} 与 {a.base} 没有共同祖先 / --base 解析不了")
        head, base = head.strip(), base.strip()

        resolve, prefix_map = load_mapping(head)
        # 自证:必然未映射的探针必须回 __unmapped__;映射表第一条前缀必须能解析成非 __unmapped__
        if resolve(PROBE_UNMAPPED) != "__unmapped__":
            raise CantRun("自证不过:探针路径没回 __unmapped__ ⇒ resolve_permission 的语义变了,本门判不了")
        if not prefix_map or resolve(prefix_map[0][0] + "x") == "__unmapped__":
            raise CantRun("自证不过:映射表第一条前缀自己都解析成 __unmapped__")
        pub = load_public(head)
        allow = allowlist(head)

        b_routes, b_unres = extract(base)
        h_routes, h_unres = extract(head)
        n_api_base = sum(1 for p in b_routes if p.startswith("/api/"))
        if n_api_base < a.min_routes:
            raise CantRun(f"基准上只抽到 {n_api_base} 条 /api 路由(< {a.min_routes})—— 分母塌了,读数不可信")
    except CantRun as e:
        print(f"  🔴 本门【没跑成·rc=3】:{e} —— 没跑 != 没违规")
        return 3

    new = sorted(set(h_routes) - set(b_routes))
    new_unres = sorted(set(h_unres) - set(_strip_lines(b_unres, h_unres)))
    new_api = [p for p in new if p.startswith("/api/")]
    print(f"  待发 {head[:9]} vs 基准 {base[:9]}(merge-base):/api 路由 基准 {n_api_base} · 待发 "
          f"{sum(1 for p in h_routes if p.startswith('/api/'))} · 本包新增 {len(new_api)}(非 /api 新增 {len(new) - len(new_api)})")
    red, warn, ok_lines = [], [], []
    for p in new_api:
        probe = re.sub(r"\{[^}]+\}", "x", p)
        methods = h_routes[p]
        if is_public(pub, probe, methods):
            ok_lines.append(f"⚪ {p}  公开路由(中间件白名单),不要求映射")
            continue
        mod = resolve(probe)
        if mod == "__unmapped__":
            if p in allow:
                warn.append(f"⚠️  {p}  未映射 = 只 admin 可用,已写进 admin_only_allowlist.tsv(刻意)—— 签字条点名")
            else:
                red.append(f"{p} [{','.join(sorted(methods))}] 没有模块归属 ⇒ 上线后服务商 / 客户 403 UNMAPPED_ROUTE"
                           f" —— 去 {MAPPING_FILE} 映射;真要只给 admin 写进 {ALLOW_FILE}")
        else:
            ok_lines.append(f"✅ {p} → {mod if mod is not None else 'None(仅需认证)'}")
    for x in ok_lines + warn:
        print(f"    {x}")
    if new_unres:
        for x in new_unres:
            print(f"  🔴 本门【没跑成·rc=3】:本包新增的路由算不出完整路径:{x}")
        return 3
    for x in red:
        print(f"  🔴 {x}")
    if not new_api:
        print("  ⚪ 本包没有新增 /api 路由(无对象)")
    elif not red:
        print(f"  ✅ 本包新增 {len(new_api)} 条 /api 路由都有归属或公开")
    return 1 if red else 0


def _strip_lines(base_unres: list[str], head_unres: list[str]) -> list[str]:
    """算不出的条目带行号,行号随编辑漂移 ⇒ 去掉「:行号」再比,基准里已有的不算本包新增。"""
    key = lambda s: re.sub(r":\d+ ", " ", s, count=1)
    bk = {key(s) for s in base_unres}
    return [s for s in head_unres if key(s) in bk]


if __name__ == "__main__":
    sys.exit(main())
