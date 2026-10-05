# -*- coding: utf-8 -*-
"""#101 · `request.state.user` 上裸读 `["id"]` / `.get("id")` 的**数据流溯源**普查。

## 为什么不能按名字匹配

C 试过四版:99 / 50 / 1 处两版**漏掉已知真阳**(手写 helper 名单静默漏项),
第四版「所在函数带 request 参数」候选 225 处但假阳率未知
(很多 `user` 是端点内的 DB 行,确实有 id 列)。

本器不维护任何 helper 名单 —— **从 `request.state.user` 出发做不动点推导**:

    第 0 轮:直接读 `request.state.user` / `getattr(request.state, "user", …)` 的表达式
    第 n 轮:若某函数的**所有** return 都追溯到已知 user 源 ⇒ 它自己也是 user 源
    收敛后:凡变量赋值自 user 源 ⇒ 该变量是 user 变量;它上面的 ["id"] / .get("id") 就是读点

跨模块用 `ImportFrom` 的 **asname 映射**解析(`import X as Y` 后按 Y 匹配),
不按原名 —— A 实测按原名匹配得空集、差点报「未接线」,而假信号坏在**坏消息方向**。

## 三态

    HIT         变量确证来自 user 源,且裸读了 id
    UNRESOLVED  变量来源追不到(跨函数传参 / 从容器取出 / 动态)—— 单列计数,不进 HIT 也不进 PASS
    (其余)     不是 user 变量,不在分母里
"""
from __future__ import annotations

import ast
import io
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN = ["api", "services", "db", "middleware", "auth", "workflows", "tools"]


def valid_keys() -> set[str]:
    """`request.state.user` 上**实际存在**的键 —— 机械取自它的两处构造,不手写。

    两条来源都产出**规范化**的 dict:
      · `auth/jwt_utils.py` `create_access_token` 里的 `payload = {...}`
      · `auth/middleware.py` perm_version 软刷新分支的 `user_data_source = {...}`
        (它显式写 `"user_id": fresh_user["id"]` —— 把 DB 行的 `id` **改名**成了 `user_id`)
    ⇒ 两条路都**没有 `id` 键**。读 `user["id"]` 恒为 KeyError / `.get("id")` 恒 None。

    再并上后续 `payload["k"] = ...` 形式的追加键(如 team_context)。
    """
    keys: set[str] = set()
    for rel in ("auth/jwt_utils.py", "auth/middleware.py"):
        p = ROOT / rel
        if not p.exists():
            continue
        t = ast.parse(io.open(p, encoding="utf-8", errors="replace").read())
        for n in ast.walk(t):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                    and isinstance(n.targets[0], ast.Name) \
                    and n.targets[0].id in ("payload", "user_data_source") \
                    and isinstance(n.value, ast.Dict):
                for k in n.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
            # payload["team_context"] = ...
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Subscript):
                tgt = n.targets[0]
                if isinstance(tgt.value, ast.Name) and tgt.value.id in ("payload", "user_data_source") \
                        and isinstance(tgt.slice, ast.Constant) and isinstance(tgt.slice.value, str):
                    keys.add(tgt.slice.value)
    return keys


VALID_KEYS = valid_keys()

#: 由 `run()` 在推导 user 源函数时填充:那些函数体内注入过的键(如 `user["id"] = ...`)。
#: 注入过的键随返回对象传给调用方 ⇒ 下游读它是安全的。
INJECTED_BY_SOURCE: set[str] = set()


def _files() -> list[pathlib.Path]:
    out = []
    for d in SCAN:
        base = ROOT / d
        if base.exists():
            out.extend(p for p in base.rglob("*.py"))
    return sorted(out)


def _is_request_state_user(node: ast.AST) -> bool:
    """`request.state.user` 或 `getattr(request.state, "user", …)`。"""
    if isinstance(node, ast.Attribute) and node.attr == "user":
        v = node.value
        if isinstance(v, ast.Attribute) and v.attr == "state":
            return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "user":
            a0 = node.args[0]
            if isinstance(a0, ast.Attribute) and a0.attr == "state":
                return True
    return False


def _alias_map(tree: ast.AST) -> dict[str, str]:
    """本地名 → 原始符号名(A 的 ① 规格:先解析别名再比对)。"""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out[a.asname or a.name] = a.name
    return out


def run(verbose: bool = False) -> dict:
    files = _files()
    trees: dict[pathlib.Path, ast.AST] = {}
    for p in files:
        try:
            trees[p] = ast.parse(io.open(p, encoding="utf-8", errors="replace").read())
        except SyntaxError:
            continue

    # ── 不动点:推导「返回 user 的函数」——**按文件**,不是全局裸名 ──
    #    🔴 第一版用全局裸名集合:A 模块的 `_get_user` 返回 request.state.user,
    #       就让 B 模块里同名但其实是 DB 取行的 `_get_user` 也被算成 user 源;
    #       注入键也随之全局化,导致正样本反而抓不到。
    #       这些 helper 几乎都是**模块内私有**(所以才有 19 个不同名字)——
    #       按文件解析既正确又够用;跨文件只走显式 ImportFrom。
    per_file_sources: dict[pathlib.Path, set[str]] = {p: set() for p in trees}
    per_file_injected: dict[pathlib.Path, dict[str, set[str]]] = {p: {} for p in trees}
    for _round in range(6):
        grew = False
        for p, t in trees.items():
            alias = _alias_map(t)
            sources = per_file_sources[p]
            for fn in ast.walk(t):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name in sources:
                    continue
                rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
                if not rets:
                    continue
                # 本函数内 变量 → 是否 user 来源
                uv: set[str] = set()
                for st in ast.walk(fn):
                    if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                        if _expr_is_user(st.value, uv, sources, alias):
                            uv.add(st.targets[0].id)
                if all(_expr_is_user(r.value, uv, sources, alias) for r in rets):
                    sources.add(fn.name)
                    # 源函数体内注入的键,随它返回的对象传给**本文件**的调用方
                    per_file_injected[p][fn.name] = _injected_keys(fn, uv)
                    grew = True
        if not grew:
            break

    # ── 读点普查 ──
    hits, unres = [], []
    for p, t in trees.items():
        alias = _alias_map(t)
        rel = p.relative_to(ROOT).as_posix()
        sources = per_file_sources[p]
        inj_map = per_file_injected[p]
        for fn in ast.walk(t):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            uv: set[str] = set()
            from_src: set[str] = set()     # 本函数里用到的 user 源函数名
            for st in ast.walk(fn):
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    if _expr_is_user(st.value, uv, sources, alias):
                        uv.add(st.targets[0].id)
                        for c in ast.walk(st.value):
                            if isinstance(c, ast.Call):
                                f = c.func
                                nm = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                                if nm in sources:
                                    from_src.add(nm)
            # 合法键 = 全局键集 ∪ 本函数注入 ∪ **本文件**那几个源函数注入的
            injected = _injected_keys(fn, uv)
            for nm in from_src:
                injected |= inj_map.get(nm, set())
            for n in ast.walk(fn):
                var, key, kind = _read_of_id(n, injected)
                if var is None:
                    continue
                if var in uv:
                    hits.append({"f": rel, "l": n.lineno, "var": var, "key": key, "kind": kind,
                                 "src": ast.unparse(n)[:90]})
                elif var == "user":
                    # 名字叫 user 但来源追不到 —— 不判 HIT,也不判无关
                    unres.append({"f": rel, "l": n.lineno, "src": ast.unparse(n)[:90]})
    res = {"sources": sorted(set().union(*per_file_sources.values()) if per_file_sources else set()), "hits": hits, "unresolved": unres, "files": len(trees)}
    if verbose:
        print(f"  扫描 {len(trees)} 个 .py · 推导出返回 user 的函数 {len(res['sources'])} 个(按文件解析)")
        print(f"  合法键集({len(VALID_KEYS)},机械取自构造处):{sorted(VALID_KEYS)}")
        print(f"  🔴 HIT(确证来自 user 源且裸读 id):{len(hits)}")
        for h in hits:
            print(f"      {h['f']}:{h['l']}  {h['kind']}  {h['src']}")
        print(f"  UNRESOLVED(名叫 user 但来源追不到):{len(unres)}")
        for u in unres[:8]:
            print(f"      {u['f']}:{u['l']}  {u['src']}")
    return res


def _expr_is_user(node: ast.AST, local_uservars: set[str], sources: set[str], alias: dict[str, str]) -> bool:
    if node is None:
        return False
    if _is_request_state_user(node):
        return True
    if isinstance(node, ast.Name) and node.id in local_uservars:
        return True
    if isinstance(node, ast.Call):
        f = node.func
        nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if nm and (nm in sources or alias.get(nm, nm) in sources):
            return True
    # `A or B` / 三元:两支都得是 user 源才算(短路写法不许蒙混)
    if isinstance(node, ast.BoolOp):
        return all(_expr_is_user(v, local_uservars, sources, alias) for v in node.values)
    if isinstance(node, ast.IfExp):
        return _expr_is_user(node.body, local_uservars, sources, alias) and \
               _expr_is_user(node.orelse, local_uservars, sources, alias)
    return False


def _injected_keys(fn: ast.AST, uservars: set[str]) -> set[str]:
    """本函数里对 user 变量做过 `uv["k"] = ...` 写入的键 —— 写过之后读它是安全的。

    实测 `api/admin_withdrawal_api.py:34` 的 `_get_admin` 正是:
        user = getattr(request.state, "user", None)
        ...
        user["id"] = user.get("user_id") or user.get("id")   # ← 补键
        return user
    该模块下游全部 `user["id"]` 因此合法。不算这一层会造出一整片假阳。
    """
    out = set()
    for st in ast.walk(fn):
        if isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                        and t.value.id in uservars and isinstance(t.slice, ast.Constant) \
                        and isinstance(t.slice.value, str):
                    out.add(t.slice.value)
    return out


def _read_of_id(n: ast.AST, injected: frozenset | set = frozenset()):
    """`x[k]` / `x.get(k)`,且 **k 既不在 VALID_KEYS 也不在本地注入集** ⇒ (var, k, kind)。

    分母是**实际键集**(机械取自两处构造),不是我手写的「哪些键是坏的」。

    🔴 Subscript 必须是 **Load**:`user["id"] = ...` 是**写**不是读。
       第一版没判 ctx,把补键那一行数成了缺陷读点。
    """
    if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
            and isinstance(getattr(n, "ctx", None), ast.Load):
        s = n.slice
        if isinstance(s, ast.Constant) and isinstance(s.value, str) \
                and s.value not in VALID_KEYS and s.value not in injected:
            return n.value.id, s.value, "subscript"
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" \
            and isinstance(n.func.value, ast.Name) and n.args:
        a = n.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                and a.value not in VALID_KEYS and a.value not in injected:
            return n.func.value.id, a.value, "get"
    return None, None, None


if __name__ == "__main__":
    run(verbose=True)
