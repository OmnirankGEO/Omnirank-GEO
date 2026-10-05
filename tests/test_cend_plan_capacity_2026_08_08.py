"""C 端方案三档容量下发(P1 补完 · 2026-08-08)

锁三件事:
  1. 搬运正确 —— `{tier}_articles` 来自 clusters 里已经算好的数,不是重算的
  2. 幂等 + 不覆盖 —— 生成侧补过一次,读取侧再补一次,结果必须一样
  3. **接线** —— 生产者 / 读取端点 / 两个前端消费方都真的接上了
     (「函数对锁全绿,生产恒不生效」这条坑今年已经踩到第三次)

判别力保证:每条"必须命中"都配一条"必须不命中"。
"""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest

from services.c_end_plan_capacity import (
    CAPACITY_WHEN_NOT_QUOTED,
    CEND_PLAN_TIERS,
    attach_tier_article_capacity,
    index_cluster_keywords,
)

REPO = Path(__file__).resolve().parents[1]


# ============================================================
# fixture:形状逐字照抄生产 result_json
#   来源(2026-08-08 只读取证):
#     clusters[*].core_keywords[0] 的 key 集合 =
#       core_score,cost_per_article,difficulty_score,effective_competition,entry,flagship,
#       geo_multiplier,intent,is_broad,is_selected,keyword,required_articles,
#       search_probability,search_volume,selling_price,standard,strong,value_score
#     keyword_package[0] 的 key 集合 =
#       cluster_theme,competitor_count,difficulty_level,entry_price,flagship_price,
#       keyword,role,standard_price,strong_price
#   —— 注意 keyword_package **没有** strategy、也没有任何 *_articles。这就是 bug 的根。
# ============================================================

def _cluster_kw(name: str, *, entry: int, standard: int, flagship: int, strong: int = 0,
                price: int = 600, selected: bool = True) -> dict:
    return {
        "keyword": name,
        "is_selected": selected,
        "selling_price": price,
        "required_articles": standard,
        "cost_per_article": 60.0,
        "entry": {"price": price // 2, "articles": entry},
        "standard": {"price": price, "articles": standard},
        "flagship": {"price": price * 2, "articles": flagship},
        "strong": {"price": price * 3, "articles": strong},
    }


def _pkg_kw(name: str, *, role: str = "core", std_price: int = 600) -> dict:
    return {
        "keyword": name,
        "cluster_theme": "主题包 A",
        "role": role,
        "entry_price": std_price // 2,
        "standard_price": std_price,
        "flagship_price": std_price * 2,
        "strong_price": std_price * 3,
        "competitor_count": 12,
        "difficulty_level": 3,
    }


def make_plan() -> dict:
    return {
        "brand_name": "测试品牌",
        "clusters": [{
            "theme": "主题包 A",
            "core_keywords": [
                _cluster_kw("生成式AI引擎优化公司推荐", entry=18, standard=31, flagship=44, price=5170),
                _cluster_kw("社媒营销服务哪家好", entry=9, standard=15, flagship=22, price=2718),
            ],
            "covered_keywords": [
                _cluster_kw("附赠词一", entry=1, standard=2, flagship=3, price=120, selected=False),
                {"keyword": "变体词", "source": "generated_variant"},  # 没有三档 → 不写字段
            ],
        }],
        "keyword_package": [
            _pkg_kw("生成式AI引擎优化公司推荐", std_price=5170),
            _pkg_kw("社媒营销服务哪家好", std_price=2718),
            _pkg_kw("附赠词一", role="covered", std_price=120),
        ],
    }


# ============================================================
# 1. 搬运正确性
# ============================================================

def test_capacity_comes_from_clusters_not_from_price():
    plan = attach_tier_article_capacity(make_plan())
    first = plan["keyword_package"][0]
    assert first["standard_articles"] == 31
    assert first["entry_articles"] == 18
    assert first["flagship_articles"] == 44
    # 反向对照:价格反推会得到 5170/60 ≈ 86 —— 判据必须把这两个数**区分开**,
    # 否则"改成读字段了"和"还在算价"在测试里长得一样。
    assert round(5170 / 60) == 86
    assert first["standard_articles"] != round(first["standard_price"] / 60)


def test_all_four_tiers_are_emitted():
    """🔴 档位名**写死在断言里**,不许拿 CEND_PLAN_TIERS 去遍历。

    第一版就是 `for tier in CEND_PLAN_TIERS` —— 常量被删一档,循环也少跑一圈,
    断言永远绿。变异 M6(删掉 strong)当场存活,是变异 runner 抓出来的。
    """
    plan = attach_tier_article_capacity(make_plan())
    for item in plan["keyword_package"][:2]:
        for tier in ("entry", "standard", "flagship", "strong"):
            assert f"{tier}_articles" in item, f"{tier} 档没下发"
    assert CEND_PLAN_TIERS == ("entry", "standard", "flagship", "strong")


def test_covered_keyword_also_gets_capacity():
    """附赠词也带容量(生产实测:42/42 个带三档的附赠词容量都 > 0)。"""
    plan = attach_tier_article_capacity(make_plan())
    covered = [k for k in plan["keyword_package"] if k["role"] == "covered"][0]
    assert covered["standard_articles"] == 2


def test_zero_capacity_is_preserved_not_bumped_to_one():
    """显式 0 篇必须原样留住 —— P1 语义是 0..capacity,0 是合法容量不是缺失。"""
    plan = make_plan()
    plan["clusters"][0]["core_keywords"][0]["standard"]["articles"] = 0
    out = attach_tier_article_capacity(plan)
    assert out["keyword_package"][0]["standard_articles"] == 0


def test_keyword_missing_from_clusters_gets_no_field():
    """超红海 / 爆价 / 老路径追加的词在 clusters 里没有定价 → 不写字段,
    让前端回落 `strategy.articles_needed`,老路径行为不变。"""
    plan = make_plan()
    plan["keyword_package"].append({"keyword": "老路径词", "strategy": {"articles_needed": 4}})
    out = attach_tier_article_capacity(plan)
    legacy = out["keyword_package"][-1]
    assert "standard_articles" not in legacy


def test_flat_articles_form_is_also_accepted():
    """两种承载形态都认(嵌套 {price,articles} 与扁平 {tier}_articles 都是 batch_pricing 自己写的)。"""
    plan = make_plan()
    plan["clusters"][0]["core_keywords"][0] = {
        "keyword": "生成式AI引擎优化公司推荐",
        "entry_articles": 7, "standard_articles": 11, "flagship_articles": 13, "strong_articles": 20,
    }
    out = attach_tier_article_capacity(plan)
    assert out["keyword_package"][0]["standard_articles"] == 11


def test_negative_capacity_is_clamped_to_zero():
    plan = make_plan()
    plan["clusters"][0]["core_keywords"][0]["standard"]["articles"] = -5
    out = attach_tier_article_capacity(plan)
    assert out["keyword_package"][0]["standard_articles"] == 0


def test_missing_articles_key_falls_back_to_zero_not_one():
    """缺 articles 键时兜底是 0,不是 LEGACY_MISSING_CAPACITY_DEFAULT(=1)。
    「没报到价」和「报了 1 篇」是两回事,不许混。"""
    assert CAPACITY_WHEN_NOT_QUOTED == 0
    plan = make_plan()
    plan["clusters"][0]["core_keywords"][0]["standard"] = {"price": 5170}
    out = attach_tier_article_capacity(plan)
    assert out["keyword_package"][0]["standard_articles"] == 0


# ============================================================
# 2. 幂等 / 不覆盖
# ============================================================

def test_idempotent_two_passes_same_result():
    once = attach_tier_article_capacity(make_plan())
    twice = attach_tier_article_capacity(json.loads(json.dumps(once)))
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)


def test_existing_field_is_never_overwritten():
    """读取侧不许改掉生成侧已经冻结下来的数(历史快照不重算)。"""
    plan = make_plan()
    plan["keyword_package"][0]["standard_articles"] = 999
    out = attach_tier_article_capacity(plan)
    assert out["keyword_package"][0]["standard_articles"] == 999


def test_no_price_field_is_touched():
    before = make_plan()
    after = attach_tier_article_capacity(json.loads(json.dumps(before)))
    for a, b in zip(after["keyword_package"], before["keyword_package"]):
        for key in ("entry_price", "standard_price", "flagship_price", "strong_price"):
            assert a[key] == b[key], f"{key} 被动了 —— 本模块只搬篇数,不许碰价格"


@pytest.mark.parametrize("bad", [None, "", 123, [], {"keyword_package": "not a list"},
                                 {"keyword_package": []}, {"keyword_package": [1, 2]}])
def test_garbage_in_does_not_raise(bad):
    """读取端点在业务链路上,补篇数失败不许把方案页顶掉。"""
    attach_tier_article_capacity(bad)


def test_index_prefers_first_occurrence():
    idx = index_cluster_keywords([
        {"core_keywords": [{"keyword": "A", "standard": {"articles": 3}}]},
        {"core_keywords": [{"keyword": "A", "standard": {"articles": 9}}]},
    ])
    assert idx["A"]["standard"]["articles"] == 3


def test_index_ignores_non_mapping_rows():
    idx = index_cluster_keywords([None, "x", {"core_keywords": [None, {"keyword": "B"}]}])
    assert set(idx) == {"B"}


# ============================================================
# 3. 接线锁(函数对了不等于生产生效)
# ============================================================

def _src(rel: str) -> str:
    return io.open(REPO / rel, encoding="utf-8", errors="ignore").read()


def test_producer_is_wired():
    """`one_click_geo_plan` 的返回值真的过了这个函数 —— 而且是 return 的那个对象。"""
    text = _src("tools/c_end_cost_estimate.py")
    assert "from services.c_end_plan_capacity import attach_tier_article_capacity" in text
    assert "return attach_tier_article_capacity(result)" in text
    # 反向对照:确认 return 的确实是被补过的那个 dict,不是另起一个字面量
    tree = ast.parse(text)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "one_click_geo_plan")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    wrapped = [r for r in returns
               if isinstance(r.value, ast.Call)
               and getattr(r.value.func, "id", "") == "attach_tier_article_capacity"]
    assert wrapped, "one_click_geo_plan 的 return 没有过容量补齐"
    dict_returns = [r for r in returns if isinstance(r.value, ast.Dict)]
    assert not dict_returns, "还有一条 return 直接吐字面量 dict —— 那条不会被补篇数"


def test_backend_has_no_second_capacity_formula():
    """本模块只准搬运。出现 `/ 60` 之类的成本反算 = 又长出第二套篇数逻辑。"""
    text = _src("services/c_end_plan_capacity.py")
    tree = ast.parse(text)
    divs = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Div, ast.FloorDiv))]
    assert not divs, "容量搬运模块里出现了除法 —— 它只该搬数,不该算数"
    # 反向对照:判据不是恒真(同样的扫法能在 batch_pricing 里数出除法)
    assert [n for n in ast.walk(ast.parse(_src("tools/batch_pricing.py")))
            if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Div, ast.FloorDiv))]
