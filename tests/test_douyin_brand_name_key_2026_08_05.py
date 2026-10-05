"""锁:geo_douyin 取客户名不许再读一个不存在的列。

背景(生产实证 2026-08-05):
  `db.diagnosis_db.get_brand_by_id` 是 `SELECT * FROM brands`,
  brands 表的列叫 `name`,**没有 `brand_name` 这一列**。
  而 api/geo_douyin_api.py 四处都读 `.get("brand_name")` → 恒 None → 客户名恒空。

  这是老 bug,以前**静默**:内容照生成,只是从不点名客户
  (post 14 标题「深圳深圳全屋定制哪家好…」而该客户是"全域上榜科技")。
  geovid v4 加了「不点名客户一律判废」的 P0 闸后,它从静默变成 **100% 失败**
  (post 15 / post 16 两个不同品牌都 `copy: brand_name_missing`)。

每条「必须命中」都配一条成对的「必须不命中」——
没有反向对照的锁只是在陈述现状,证不了"以后写错还会被抓到"。
"""
from __future__ import annotations

import ast
import io
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
API = REPO / "api" / "geo_douyin_api.py"


def _src() -> str:
    return io.open(API, encoding="utf-8").read()


# ────────────────────────── 行为:helper 本身 ──────────────────────────

def _helper():
    import sys

    sys.path.insert(0, str(REPO))
    from api.geo_douyin_api import _brand_display_name  # noqa: PLC0415

    return _brand_display_name


def test_A1_real_brands_row_shape_yields_name__must_hit():
    """真实 brands 行的形状(只有 name,没有 brand_name)必须取得到名字。

    这就是生产上 100% 失败的那个入参形状。
    """
    f = _helper()
    row = {"id": 629, "name": "全域上榜（深圳）科技有限公司", "industry": "AI搜索优化/GEO服务"}
    assert f(row) == "全域上榜（深圳）科技有限公司"
    assert "brand_name" not in row, "本用例的价值就在于入参【没有】brand_name 这个键"


def test_A2_mapped_shape_still_works__must_hit():
    """别处传进来的、已经映射成 brand_name 的 dict 也要认(兜底那一半)。"""
    f = _helper()
    assert f({"brand_name": "某某科技"}) == "某某科技"


def test_A3_name_wins_over_brand_name__must_hit():
    """两个键都在时以 name 为准(与 report_metrics.py:433 同序,不另立口径)。"""
    f = _helper()
    assert f({"name": "甲", "brand_name": "乙"}) == "甲"


def test_B1_empty_inputs_return_empty__must_not_hit():
    """🔴 反向对照:真取不到时必须回空串,不许编一个名字出来。

    编名字 = 我们替客户写了一句他没说过的推荐语,比失败更糟。
    """
    f = _helper()
    for bad in (None, {}, {"name": None}, {"name": ""}, {"name": "   "}):
        assert f(bad) == "", f"{bad!r} 应回空串"


# ────────────────────────── 静态:不许再写回去 ──────────────────────────

_BARE = re.compile(r'\(\s*brand\s*or\s*\{\}\s*\)\s*\.get\(\s*["\']brand_name["\']\s*\)')


def test_C1_no_bare_brand_name_read_from_brand_row__must_not_hit():
    """🔴 从 `get_brand_by_id` 结果里裸读 brand_name 的写法必须绝迹。"""
    hits = _BARE.findall(_src())
    assert not hits, (
        f"又出现了 {len(hits)} 处 `(brand or {{}}).get(\"brand_name\")` —— "
        "brands 表没有这个列,读它恒空。走 _brand_display_name。"
    )


def test_C2_the_regex_would_catch_the_old_code__must_hit():
    """🔴 判别力自证:同一条正则打在【修复前的原文】上必须命中。

    没有这一条,上面那条"零命中"可能只是正则写废了。
    """
    old = '            brand_name = (brand or {}).get("brand_name") or ""'
    assert _BARE.findall(old), "正则连修复前的原文都抓不到 —— 判据是废的"


def test_C3_contact_dict_brand_name_not_touched__must_hit():
    """🔴 误伤自证:`contact`/`info` 那两处的 brand_name 是它们自造字典的合法键,必须还在。

    第 700 行附近 `contact = {..., "brand_name": ""}` —— 一刀切全局替换会把它们也改掉。
    """
    src = _src()
    assert 'contact.get("brand_name")' in src
    assert 'info.get("brand_name")' in src


def test_D1_helper_is_single_point__must_hit():
    """四个业务入口全走同一条取名路径 —— 同一事实不写四份。

    🔴 [2026-08-06 收敛后改写] 原判据数的是 `_brand_display_name` 被**直接调用**
       ≥4 次。合并 geovid v5 之后,四个入口改走异步入口 `fetch_brand_display_name`,
       由它**唯一一次**调用 `_brand_display_name` —— 直接调用数变成 1,原判据会红。

       这里改的是**判据形态,不是严格度**:锁的仍然是"四个入口一条路径",
       而且比原来更严 —— 原判据只数了调用次数,现在同时要求
       「读 brands 行的地方全仓只有一处」(下面第二段)。
       **不是为了让自己过就把 >=4 改成 >=1。**
    """
    tree = ast.parse(_src())
    entries = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "fetch_brand_display_name"
    )
    assert entries >= 4, \
        f"取名入口只被调用 {entries} 次,应 ≥4(蒸馏/创建/重做/发布预填)"

    # 更严的一半:整份文件里读 brands 行的函数只能有一个,
    # 且它必须就是那个异步入口 —— 谁再自己去 get_brand_by_id 都会红。
    readers = [
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(x, ast.Name) and x.id == "get_brand_by_id"
                for x in ast.walk(fn))
    ]
    assert readers == ["fetch_brand_display_name"], \
        f"读 brands 行的地方不止一处:{readers}"

    # 反向对照:合并后**不许**还剩第二个同用途的单点函数(死函数正是本次返工的起因)
    names = {fn.name for fn in ast.walk(tree)
             if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "brand_display_name" not in names, \
        "又出现了第二个同用途的取名函数 —— 其中一个必然变成零调用死函数"
