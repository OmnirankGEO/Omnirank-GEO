"""找「长得像守卫、但体内没有任何鉴权原语」的函数(同名冒牌货扫描)。

起因:`api/material_api.py` 与另两个社媒 router(运营 / 人设,已随 E3 删)三处各有一份
本地 `_get_profile_safe`,docstring 自写「宽松版,不做 brand 权限校验」——
它只判档案存不存在。按**函数名**扫 IDOR 的判据会把这三个文件整个判成"有守卫",
于是 13 个调用点从来没进过候选名单。

所以判据必须改成:**看守卫体内有没有真的鉴权原语**,不看它叫什么名字。

判据改了三版,每一版都是被自己的误报逼出来的:
  v1 一层判定           → 19 命中,绝大多数是"自己不判、转调真守卫"的包装函数
  v2 跨函数追踪(递归)  → 10 命中,全是 `_require_user(request)` 这类**登录检查**
  v3 再要求"收了资源 id" → 1 命中,是按 `organization_id` 判租户的真守卫(原语名单漏了组织维度)
  v4 补组织维度         → 0 命中

🔴 每一版都拿【改前的树】做反向对照:必须仍然抓到那 3 个已知冒牌货。
   只断言"改后干净"是没有判别力的 —— 一个恒不命中的扫描也能做到"改后干净"。
"""
import ast
import os
import sys

# 真正的鉴权原语 = auth/brand_access.py 的全部 require_*/filter_* 导出 + admin 闸。
# 🔴 这份名单不许凭印象写 —— 是照 `grep '^def ' auth/brand_access.py` 抄下来的,
#    漏一个就会把真守卫误判成冒牌货(第一版就漏了 require_quote_access,
#    当场把 meijiehezi_api._require_article_access 误报了)。
AUTHZ_PRIMITIVES = {
    "require_brand_access", "require_profile_access", "require_project_access",
    "require_diagnosis_access", "require_client_material_access",
    "require_report_access", "require_quote_access",
    "_require_organization_artifact", "filter_organization_artifact_rows",
    "get_user_brand_filter", "demo_readable_brand_id", "_is_brand_owner",
    "_owned_brand_ids",
    "require_admin", "_require_admin", "_get_admin", "_admin",
}
# 手写归属判定也算(形如 row["user_id"] != user["user_id"] / is_admin)
# 组织维度也是归属维度 —— 第一版漏了它,把 marketing_material_api
# `_require_frozen_actor_scope`(按 organization_id 判租户边界并 404 防枚举)误报成冒牌货。
AUTHZ_TEXT_MARKERS = (
    "user_id", "is_admin", "owner_user_id",
    "organization_id", "organization_identity",
)

# 名字长得像守卫
GUARDISH = ("safe", "access", "owner", "permission", "perm", "auth", "guard", "require")


def looks_like_guard(fn: ast.AST) -> bool:
    """只认【资源归属守卫】,不认【登录检查】。

    区别特征是入参:
      - `_require_user(request)`            → 只判"登录了没",**不是**本扫描的对象
      - `_get_profile_safe(request, pid)`   → 收了资源 id,才是"这东西是不是你的"
    第一版没做这个区分,10 个 `_require_user` 之类混进命中里 ——
    噪声一多,真冒牌货就淹掉了。判据宁可窄而准,也不要宽而吵。
    """
    name = fn.name.lower()
    if not any(k in name for k in GUARDISH):
        return False
    args = [a.arg for a in fn.args.args if a.arg not in ("self", "cls")]
    if not any(a in ("request", "req", "user") for a in args):
        return False
    # 除调用者之外,必须还收至少一个入参(即被判定的那个资源)
    return len([a for a in args if a not in ("request", "req", "user")]) >= 1


def _strip_comments(seg: str) -> str:
    """剥掉 # 注释 —— 否则说明文字里出现 'user_id' 就能把断言骗过去。"""
    return "\n".join(line.split("#", 1)[0] for line in seg.split("\n"))


def _called_names(fn: ast.AST):
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            nm = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if nm:
                yield nm


def reaches_authz(fn, local_defs, src, seen=None, depth=0) -> bool:
    """🔴 跨函数追踪:守卫自己不判、但转调了另一个真守卫,也算数。

    一层判定会把这类包装函数全部误报(实测 19 命中里绝大多数是这种),
    而噪声一多,真冒牌货就藏在里面看不见了 —— 所以必须递归。
    """
    if seen is None:
        seen = set()
    if depth > 6 or fn.name in seen:
        return False
    seen.add(fn.name)

    for nm in _called_names(fn):
        if nm in AUTHZ_PRIMITIVES:
            return True
    body = _strip_comments(ast.get_source_segment(src, fn) or "")
    if any(m in body for m in AUTHZ_TEXT_MARKERS):
        return True
    for nm in _called_names(fn):
        callee = local_defs.get(nm)
        if callee is not None and reaches_authz(callee, local_defs, src, seen, depth + 1):
            return True
    return False


def scan(root, api_dir="api"):
    hits, checked = [], 0
    for dirpath, _d, files in os.walk(os.path.join(root, api_dir)):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            src = open(path, "r", encoding="utf-8", errors="replace").read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            local_defs = {
                n.name: n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for node in local_defs.values():
                if not looks_like_guard(node):
                    continue
                checked += 1
                if not reaches_authz(node, local_defs, src):
                    rel = os.path.relpath(path, root).replace("\\", "/")
                    hits.append((rel, node.lineno, node.name))
    return hits, checked


if __name__ == "__main__":
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    hits, checked = scan(root)
    print(f"检查了 {checked} 个「名字像守卫且能拿到调用者」的函数")
    if not hits:
        print("✅ 没有发现冒牌货")
    for rel, line, name in sorted(hits):
        print(f"🔴 {rel}:{line}  {name}  —— 体内没有任何鉴权原语")
    sys.exit(0)
