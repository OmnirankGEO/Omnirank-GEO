"""[WO-KYB D0-b] 接线锁 —— 光有投影函数不算数,三个端点得真调它。

判据走 AST 结构断言,不用源码串(源码串会被注释/改名骗过)。
断言前先剥 docstring —— 注释里出现的字段名会让判据恒真/恒假。
"""
import ast
import io
import os

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_MHZ = os.path.join(_ROOT, "api", "meijiehezi_api.py")
_PUB = os.path.join(_ROOT, "api", "publish_api.py")
_FE = os.path.join(_ROOT, "frontend", "src", "pages", "Publishing")

CATALOG_ENDPOINTS = ["api_list_media", "api_list_wemedia", "api_list_short_video"]


def _src(p):
    return io.open(p, encoding="utf-8", newline="").read()


def _fn(path, name):
    for n in ast.walk(ast.parse(_src(path))):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _calls(fn):
    return {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
            for c in ast.walk(fn) if isinstance(c, ast.Call)}


def _args(fn):
    return [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]


# ============ 三个目录端点都必须投影 ============

@pytest.mark.parametrize("name", CATALOG_ENDPOINTS)
def test_catalog_endpoint_projects(name):
    fn = _fn(_MHZ, name)
    assert fn is not None, f"找不到 {name}"
    assert "_project_catalog" in _calls(fn), f"{name} 没走投影,进货价会原样返回"


def test_projection_criterion_is_not_vacuous():
    """反向对照:换个不存在的函数名,上面那条必须测不到。"""
    fn = _fn(_MHZ, CATALOG_ENDPOINTS[0])
    assert "_project_catalog_RENAMED" not in _calls(fn)


# ============ 元口径筛选参数必须消失 ============

@pytest.mark.parametrize("name", CATALOG_ENDPOINTS)
def test_yuan_filters_removed_from_signature(name):
    """🔴 留着 price_min/price_max 本身就是泄漏信道:
    二分它看哪些媒体消失,就能把进货价试出来。所以必须删,不是并存。"""
    args = _args(_fn(_MHZ, name))
    assert "price_min" not in args and "price_max" not in args, \
        f"{name} 还收元口径筛选参数"
    assert "points_min" in args and "points_max" in args, f"{name} 缺算力口径参数"


@pytest.mark.parametrize("name", CATALOG_ENDPOINTS)
def test_points_filter_is_converted_not_passed_raw(name):
    """算力参数必须换算成元再交给 DB 层(DB 仍按进货价比较)。"""
    fn = _fn(_MHZ, name)
    assert "_points_to_yuan" in _calls(fn), f"{name} 没做算力→元换算"


@pytest.mark.parametrize("name", CATALOG_ENDPOINTS)
def test_sort_key_is_mapped(name):
    assert "_public_sort_key" in _calls(_fn(_MHZ, name)), f"{name} 没映射排序键"


# ============ /markup 收窄 ============

def test_markup_endpoint_is_admin_only():
    fn = _fn(_MHZ, "api_get_markup_public")
    assert fn is not None
    assert "_require_admin" in _calls(fn), "/markup 仍是公开端点,加价率照样外露"


def test_markup_admin_gate_criterion_can_fail():
    """反向对照:目录端点本来就不该有 admin 闸,判据不能恒真。"""
    assert "_require_admin" not in _calls(_fn(_MHZ, "api_list_media"))


# ============ 推荐链同样收口 ============

def test_recommendation_payload_is_projected():
    fn = _fn(_PUB, "_build_media_package_response")
    assert fn is not None
    assert "project_payload" in _calls(fn), \
        "AI 推荐链没投影 —— 设计稿只列了目录接口,只修那边是半修"


# ============ 死端点已删 ============

def test_dead_order_items_endpoint_removed():
    s = _src(_MHZ)
    assert '@router.get("/orders/{order_id}/items")' not in s, "零调用死端点还在"
    assert "get_publish_order_owner" not in s, "死端点删了但 import 没撤,留下孤儿符号"


def test_dead_endpoint_criterion_can_fail():
    """反向对照:同文件里别的路由必须仍在,证明判据不是"整个文件都搜不到"。"""
    assert '@router.get("/mhz-orders")' in _src(_MHZ)


# ============ 前端不再自己算价 ============

def _strip_ts_comments(src: str) -> str:
    """🔴 断言 TS 源码前必须先剥注释。

    第一版没剥,结果被**我自己写的说明注释**骗红了 —— 注释里提到
    `meijiehezi/markup` 就让"不许出现"的判据永远不成立。
    (Python 侧早有"断言先剥 # 注释与 docstring"这条,TS 侧同理。)
    """
    out, i, n = [], 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif two == "/*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif src[i] in "'\"`":
            q = src[i]
            j = i + 1
            while j < n and src[j] != q:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


@pytest.mark.parametrize("f", ["PublishCenter.tsx", "ShortVideoPanel.tsx"])
def test_frontend_no_longer_computes_points(f):
    code = _strip_ts_comments(_src(os.path.join(_FE, f)))
    assert "yuanToPoints" not in code, f"{f} 还在前端做乘法"
    assert "meijiehezi/markup" not in code, f"{f} 还在拉 markup(已 admin-only,会 403)"


def test_comment_stripper_actually_strips():
    """反向对照之一:剥注释这件事本身要能被证伪。"""
    src = 'const a = 1; // meijiehezi/markup\n/* yuanToPoints */ const b = 2;'
    code = _strip_ts_comments(src)
    assert "meijiehezi/markup" not in code and "yuanToPoints" not in code
    assert "const a = 1;" in code and "const b = 2;" in code


def test_comment_stripper_keeps_real_code():
    """反向对照之二:不能把真代码也剥掉 —— 否则判据恒真。"""
    src = "const url = '/api/meijiehezi/markup';"
    assert "meijiehezi/markup" in _strip_ts_comments(src)


def test_frontend_criterion_can_fail():
    """反向对照:price_points 必须**在**前端出现,证明不是整个文件读空了。"""
    code = _strip_ts_comments(_src(os.path.join(_FE, "PublishCenter.tsx")))
    assert "price_points" in code


def test_dead_media_market_component_removed():
    assert not os.path.exists(
        os.path.join(_ROOT, "frontend", "src", "components", "publishing", "MediaMarket.tsx")
    ), "死组件 MediaMarket.tsx 还在"


def test_frontend_totals_sum_per_item_points():
    """🔴 总价必须逐条相加,不能先求和再 ceil。

    服务端 _recompute_publish_charge 是逐条 ceil 再求和;前端原来是 ceil(Σ),
    N 个媒体最多差 N-1 算力 —— A-8-1 那条不一致告警多半就是它。
    """
    s = _src(os.path.join(_FE, "PublishCenter.tsx"))
    assert "mhzSelList.reduce((s, m) => s + (m.price_points || 0), 0)" in s
    assert "mhzTotalYuan" not in s, "元口径的中间量还在,说明没改干净"
