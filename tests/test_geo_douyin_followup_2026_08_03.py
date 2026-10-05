"""收尾三件 · 锁(2026-08-03)

  ① RBAC 路由映射漏登记 —— 这是我上一包的真实事故:
     `auth/module_mapping.py` 里没有 `/api/geo-douyin/` 条目,
     middleware 逻辑是「admin 直接放行 / 未映射一律拒」,
     所以那个 390 算力的付费功能**上线后只有 admin 能用**。
     Deploy-CTO 在 QA 实测里撞到 UNMAPPED_ROUTE 403 才发现。
     🔴 补一个条目挡不住下一次 —— 本文件补的是**通用棘轮**:
        新出现的未映射路由会让测试转红。

  ② provider `n` 只能是 1(代码此前允许到 10)——**计费面口子**:
     提交响应只取 arr[0],放行 n>1 等于按 n 张计费只用 1 张。

  ③ 2k/4k 单张成本按 provider「定价中心」页订正(旧值两个都错)。

🔴 全部写成**行为锁**:直接调真函数喂输入断输出,不断言源码字符串。
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HTTP_METHODS = {"get", "post", "put", "delete", "patch"}


# ═════════════════════════════════════════════════════════════
# ① RBAC 路由映射棘轮
# ═════════════════════════════════════════════════════════════
# 这些前缀**按设计**就没有模块权限:token 换取 / 公开分享 / 落地页。
# 🔴 说清楚边界:本单**没有**逐条审计它们的鉴权是否正确 ——
#    它们是本单开工前就存在的,不是本单引入的。这里只是把它们冻成基线,
#    好让【新出现的】未映射路由立刻转红。别把这张表读成"这些都审过了"。
PUBLIC_BY_DESIGN_PREFIXES = (
    "/api/public/",     # 公开分享 / 落地页 / 外部 runner 回调
    "/api/identity/",   # 身份/邀请码换取
    "/api/m/",          # 营销确认短链
    "/api/s/",          # 选型短链
    "/api/sl/",         # 分享短链
)


def _collect_real_routes() -> list:
    """静态展开 api/*.py 里每个路由的**真实全路径**(router prefix + 装饰器路径)。

    🔴 不能只看 `APIRouter(prefix=...)`:有的 router prefix 就是 `/api`,
       真实路径写在装饰器上(`@router.post("/selection/xxx")`)。
       只按前缀判会把这类整批漏掉 —— 判据的粒度必须对得上被判的东西。
    """
    rows = []
    for f in sorted((ROOT / "api").glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        prefixes = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", "") == "APIRouter"):
                pre = ""
                for kw in node.value.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        pre = str(kw.value.value)
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        prefixes[t.id] = pre
        for node in ast.walk(tree):
            for dec in getattr(node, "decorator_list", []):
                fn = dec.func if isinstance(dec, ast.Call) else dec
                if not (isinstance(fn, ast.Attribute) and fn.attr in HTTP_METHODS
                        and isinstance(fn.value, ast.Name)):
                    continue
                if fn.value.id not in prefixes:
                    continue
                arg = ""
                if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                    arg = str(dec.args[0].value)
                full = prefixes[fn.value.id] + arg
                if full.startswith("/api"):
                    rows.append((full, f.name))
    return rows


def _unmapped(rows) -> list:
    from auth.module_mapping import resolve_permission
    out = []
    for path, fname in rows:
        if path.startswith(PUBLIC_BY_DESIGN_PREFIXES):
            continue
        if resolve_permission(path) == "__unmapped__":
            out.append((path, fname))
    return out


def test_route_scanner_actually_finds_routes():
    """前提面:扫描器得真扫到东西,否则下面那条锁是恒真的(空集合永远通过)。"""
    rows = _collect_real_routes()
    assert len(rows) > 500, f"只扫到 {len(rows)} 条路由,扫描器坏了"
    assert any(p.startswith("/api/geo-douyin") for p, _ in rows), \
        "连本单自己的路由都没扫到"


def test_mapping_checker_has_power():
    """必须不命中面:造一条没归属的路径,检查器必须判它未映射。
    否则上面那条锁可能是恒真的(比如 resolve_permission 永远返回非 __unmapped__)。"""
    from auth.module_mapping import resolve_permission
    assert resolve_permission("/api/__definitely_not_registered__/x") == "__unmapped__"
    # 反过来:已知有归属的必须不被误判
    assert resolve_permission("/api/geo-douyin/posts") != "__unmapped__"


def test_geo_douyin_is_mapped_to_writing():
    """本单事故的定点锁:GEO 图文归 writing(计费同族 → 权限同族)。"""
    from auth.module_mapping import resolve_permission
    for p in ("/api/geo-douyin/posts", "/api/geo-douyin/posts/1/task",
              "/api/geo-douyin/pricing", "/api/geo-douyin/styles"):
        assert resolve_permission(p) == "writing", f"{p} 没归到 writing"


# ═════════════════════════════════════════════════════════════
# ② provider n 只能是 1(计费面)
# ═════════════════════════════════════════════════════════════
@pytest.mark.parametrize("given,expect", [(1, 1), (2, 1), (10, 1), (0, 1), (-5, 1)])
def test_n_is_clamped_to_provider_limit(given, expect):
    from services.marketing.image_client import _clamp_n
    assert _clamp_n(given) == expect


def test_n_clamp_survives_garbage():
    """脏输入不该让生图整条挂掉(fail-soft),但也不能放大。"""
    from services.marketing.image_client import _clamp_n
    for junk in (None, "abc", 3.9, ""):
        assert _clamp_n(junk) == 1


def test_n_over_limit_is_logged_not_silent(caplog):
    """🔴 静默改用户的入参本身是一种隐瞒 —— 夹了就要留痕。"""
    import logging
    from services.marketing.image_client import _clamp_n
    with caplog.at_level(logging.WARNING, logger="GEO-Marketing-Image"):
        _clamp_n(5)
    # 🔴 用 getMessage():日志是惰性格式化的,r.message 里还是 "%s" 占位符,
    #    自己拼 `r.message % r.args` 会在参数个数对不上时抛 TypeError(实测踩到)。
    msgs = [r.getMessage() for r in caplog.records]
    assert any("5" in m for m in msgs), f"夹到 1 却一声不响: {msgs}"


def test_n_equal_one_is_not_logged(caplog):
    """必须不命中面:正常值不该刷日志(否则告警会被淹没 —— 非必要不警告)。"""
    import logging
    from services.marketing.image_client import _clamp_n
    with caplog.at_level(logging.WARNING, logger="GEO-Marketing-Image"):
        _clamp_n(1)
    assert not caplog.records


def test_submit_body_never_asks_for_more_than_one():
    """结构锁:两个提交点都必须走 _clamp_n,不能有第三种写法绕过去。

    🔴 用 AST 而不是字符串比对:tokenize 拼回来的串是**没有空白**的
       (`"n":_clamp_n(n)`),照原样写断言必然对不上 —— 实测踩到。
       结构断言天然不受排版影响,也不会被注释命中。
    """
    src = (ROOT / "services" / "marketing" / "image_client.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    n_values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "n":
                n_values.append(ast.unparse(v))
    assert len(n_values) == 2, f"提交体里 'n' 键不是两处: {n_values}"
    assert all(v == "_clamp_n(n)" for v in n_values), \
        f"有提交点绕过了 _clamp_n: {n_values}"


# ═════════════════════════════════════════════════════════════
# ③ 2k/4k 成本按定价中心订正
# ═════════════════════════════════════════════════════════════
def test_image_cost_matches_pricing_center():
    """Owner 2026-08-03 提供的 provider「定价中心」页(我们的价格那一列)。"""
    from services.marketing.image_client import IMAGE_COST_USD
    assert IMAGE_COST_USD["1k"] == pytest.approx(0.0085)
    assert IMAGE_COST_USD["2k"] == pytest.approx(0.014)
    assert IMAGE_COST_USD["4k"] == pytest.approx(0.021)


def test_old_unverified_costs_are_gone():
    """必须不命中面:旧的未核实值(0.012 / 0.024)不能还留着。"""
    from services.marketing.image_client import IMAGE_COST_USD
    assert IMAGE_COST_USD["2k"] != pytest.approx(0.012)
    assert IMAGE_COST_USD["4k"] != pytest.approx(0.024)


def test_cost_tiers_are_monotonic():
    """分辨率越高越贵 —— 这条能抓到"把 2k/4k 填反"这类手滑
    (旧值恰好就是 4k 偏高、2k 偏低,方向都错了)。"""
    from services.marketing.image_client import IMAGE_COST_USD
    assert IMAGE_COST_USD["1k"] < IMAGE_COST_USD["2k"] < IMAGE_COST_USD["4k"]


def test_cost_is_not_used_for_billing():
    """🔴 成本常量只做毛利复盘,**绝不参与扣费**(扣费走 feature_pricing)。
    这条锁的意义:哪天有人拿它去算该扣多少分,这里会红。"""
    import io
    import tokenize
    src = (ROOT / "services" / "marketing" / "image_client.py").read_text(encoding="utf-8")
    code = "".join(t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
                   if t.type != tokenize.COMMENT)
    for forbidden in ("freeze_points", "commit_freeze", "charge_on_success", "cost_points"):
        assert forbidden not in code, f"生图客户端里出现了计费符号 {forbidden}"
