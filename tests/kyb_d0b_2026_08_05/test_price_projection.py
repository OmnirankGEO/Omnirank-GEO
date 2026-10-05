"""[WO-KYB D0-b] 投影口径 —— 纯函数,可穷举。

这是整包唯一的价格出口:目录接口与 AI 推荐链都走它。
"""
import math

import pytest

from _d0b_helpers import media_row


# ============ 成本侧字段一个都不许出后端 ============

#: 🔴 故意**写死**,不引用 proj.COST_SIDE_FIELDS。
#: 引用被测常量的话,判据两端就都来自被变异的那个源 ——
#: 从常量里删掉一列,测试也跟着不再检查那一列,漏出去反而是绿的。
#: (变异 M4 当场证过:第一版正是这么写的,没杀掉。)
_MUST_BE_STRIPPED = (
    "price", "price1", "price2",
    "price_normal", "price_vip", "price_svip",
    "video_price", "weitoutiao_price",
    "hepai_price", "hepai_price1", "hepai_price2",
)


def test_all_cost_side_fields_are_stripped(proj):
    row = media_row()
    proj.project_rows([row], 1.5)
    left = [f for f in _MUST_BE_STRIPPED if f in row]
    assert left == [], f"这些成本侧字段还在响应里: {left}"


def test_stripped_list_is_not_derived_from_the_module(proj):
    """反向对照:写死的清单必须真的覆盖模块常量里的成本列,
    否则"写死"就变成了"写少了"。"""
    missing = set(_MUST_BE_STRIPPED) - set(proj.COST_SIDE_FIELDS)
    assert missing == set(), f"模块常量里没有这些: {sorted(missing)}"


def test_strip_list_covers_every_cost_column_in_the_whitelists(proj):
    """🔴 反向对照式覆盖:D0-a 的三份公开列白名单里凡是价格列,
    除 our_price_* 外**必须**都在 COST_SIDE_FIELDS 里。
    漏一个,那一列就会继续外露 —— 这条锁的就是"我列漏了"。
    """
    import db.meijiehezi_db as dbm
    declared = set(dbm.MEDIA_PUBLIC_COLUMNS) | set(dbm.WEMEDIA_PUBLIC_COLUMNS) | set(dbm.SHORT_VIDEO_PUBLIC_COLUMNS)
    price_cols = {c for c in declared if "price" in c}
    sell_side = {"our_price_yuan", "our_price_points"}
    missed = price_cols - sell_side - set(proj.COST_SIDE_FIELDS)
    assert missed == set(), f"白名单里这些价格列没被剥离: {sorted(missed)}"


def test_price_points_added(proj):
    row = media_row(price=1.00)
    proj.project_rows([row], 1.5)
    # 生产实测:D1 真投递那单服务端重算 total_cost=195 = ceil(1.00×1.5×130)
    assert row["price_points"] == 195


def test_points_formula_matches_charge_formula(proj):
    """口径必须与 _recompute_publish_charge 逐位一致(ceil,不是 round/floor)。"""
    for yuan, markup in [(0.01, 1.5), (33.33, 1.5), (40, 2.0), (7.77, 1.3)]:
        assert proj.yuan_to_points(yuan, markup) == int(math.ceil(yuan * markup * 130))


def test_zero_and_dirty_price_do_not_crash(proj):
    for bad in (None, "", "abc", -5, [], {}):
        assert proj.yuan_to_points(bad, 1.5) == 0


# ============ 售价优先级 ============

def test_existing_sell_points_win(proj):
    """已有售价算力就直接用,不要拿进货价再乘一遍。"""
    row = media_row(price=40.0, our_price_points=9999)
    assert proj.resolve_price_points(row, 1.5) == 9999


def test_sell_yuan_beats_cost_price(proj):
    row = media_row(price=40.0, our_price_points=None, our_price_yuan=100.0)
    assert proj.resolve_price_points(row, 1.5) == 100 * 130


def test_cost_fallback_multiplies_markup(proj):
    """🔴 回落到进货价那档**必须乘 markup**。

    原 services/publish_recommendation.py::_points 写的是 price_yuan × 130(漏了 markup),
    算出来的售价少收 markup 倍。这条锁的就是那个缺陷不许复活。
    """
    row = media_row(price=40.0, our_price_points=None, our_price_yuan=None)
    assert proj.resolve_price_points(row, 1.5) == math.ceil(40.0 * 1.5 * 130)
    assert proj.resolve_price_points(row, 1.5) != math.ceil(40.0 * 130)


# ============ 排序等价(Review 点名要的锁) ============

def test_sort_key_maps_public_name_to_column(proj):
    assert proj.public_sort_key("price_points") == "price"
    assert proj.public_sort_key("price") == "price"
    assert proj.public_sort_key("fans_num") == "fans_num"


@pytest.mark.parametrize("markup", [1.0, 1.5, 2.0, 3.7])
def test_sort_by_price_equals_sort_by_points(proj, markup):
    """新旧排序结果等价 —— 这是"只改名不改 SQL 表达式"能成立的全部依据。

    用固定序列(不用随机)以便可复现。
    """
    prices = [0, 0.01, 4.99, 5, 29.99, 30, 40, 59.99, 60, 61, 100, 999.99, 1000]
    by_price = sorted(prices)
    by_points = sorted(prices, key=lambda p: proj.yuan_to_points(p, markup))
    assert [proj.yuan_to_points(p, markup) for p in by_price] == \
           [proj.yuan_to_points(p, markup) for p in by_points]


def test_sort_equivalence_criterion_can_fail(proj):
    """反向对照:换成**非单调**的键,上面那条判据必须能测出不等价。"""
    prices = [1.0, 2.0, 3.0]
    non_monotonic = sorted(prices, key=lambda p: -p)
    assert [proj.yuan_to_points(p, 1.5) for p in sorted(prices)] != \
           [proj.yuan_to_points(p, 1.5) for p in non_monotonic]


# ============ 筛选参数换算 ============

def test_points_filter_round_trips_to_yuan(proj):
    assert proj.points_to_yuan(195, 1.5) == pytest.approx(1.0, rel=1e-6)


def test_points_to_yuan_zero_markup_is_safe(proj):
    assert proj.points_to_yuan(195, 0) == 0.0


# ============ 性价比徽章:判据挪到服务端 ============

def test_sweet_spot_boundaries_inclusive(proj):
    for p, want in [(29.99, False), (30, True), (45, True), (60, True), (60.01, False)]:
        assert proj.is_sweet_spot(media_row(price=p)) is want, p


def test_sweet_spot_computed_before_price_is_stripped(proj):
    """🔴 顺序锁:两个派生字段都依赖 price,必须在 pop 之前算完。
    顺序写反 → is_sweet_spot 恒 False,徽章全站消失且不报错。"""
    row = media_row(price=40.0)
    proj.project_rows([row], 1.5)
    assert row["is_sweet_spot"] is True
    assert "price" not in row


# ============ 递归投影(AI 推荐链) ============

def test_nested_payload_is_projected(proj):
    payload = {"packages": [{"title": "基础版", "medias": [media_row(price=40.0)]}]}
    proj.project_payload(payload, 1.5)
    m = payload["packages"][0]["medias"][0]
    assert "price" not in m and m["price_points"] == math.ceil(40 * 1.5 * 130)


def test_row_with_only_sell_fields_still_gets_points(proj):
    """只有 our_price_* 的行也要补 price_points —— 只按 price 判会漏掉它们。"""
    payload = {"list": [{"our_price_points": 500, "media_name": "x"}]}
    proj.project_payload(payload, 1.5)
    assert payload["list"][0]["price_points"] == 500


def test_non_media_dicts_untouched(proj):
    payload = {"quota": {"key": "ai_rec_quota", "limit": 5, "used": 1}}
    proj.project_payload(payload, 1.5)
    assert payload["quota"] == {"key": "ai_rec_quota", "limit": 5, "used": 1}
    assert "price_points" not in payload["quota"]
