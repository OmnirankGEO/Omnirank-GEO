"""B 单锁:空行业候选不得穿透行业过滤(db.publish_db.get_effective_pool_candidates)。

命题(用户可见那句):**给「餐饮」推荐时,不能出现汽车号。**

🔴 判别力设计:
   - 每条"必须命中"都配一条"必须不命中"(否则把过滤写死成 `WHERE FALSE` 也全绿);
   - 有一条**分布锁**:同一批断言里 industry_match 的 True 与 False 必须都出现过
     (否则把它写死成常量 TRUE/FALSE 照样全绿);
   - 有一条**兜底可达锁**:证明修法不是"一刀切排除空行业候选"——精确档为空时兜底
     必须真的顶上,不然面板会从"推错行业"变成"什么都没有",那是另一个 P0。

🔴 生产真数据在本条路径上是**零触发**的:2026-08-08 只读实测
   `media_effective_pool` 里 `COALESCE(industry,'')=''` 的行有 1691 条,但同时满足
   `is_recommendable AND tier IN ('L1','L2')` 的是 **0 条**。也就是说这个洞现在
   还没被真数据踩到,靠真库数据跑这条锁只会全绿——所以下面全部是**构造用例**。
"""

from __future__ import annotations

import pytest

from db.publish_db import get_effective_pool_candidates


def _names(rows):
    return sorted(str(r.get("platform_name") or r.get("media_name") or "") for r in rows)


def _industries(rows):
    return {str(r.get("industry") or "") for r in rows}


@pytest.fixture()
def mixed_pool(pool):
    """一池三类:精确命中(食品餐饮) / 不匹配(汽车网站) / 无行业标注(空)。"""
    pool.media(1, name="美食门户", industry="食品餐饮")
    pool.media(2, name="汽车之家分站", industry="汽车网站")
    pool.wemedia(3, name="汽车头条号", industry="汽车")
    pool.wemedia(4, name="无标注头条号", industry="")
    pool.media(5, name="无标注门户", industry="")
    pool.commit()
    return pool


# ══════════════════════════════════════════════════════════════
# 核心命题
# ══════════════════════════════════════════════════════════════

def test_empty_industry_candidates_do_not_leak_into_a_matched_industry_query(mixed_pool):
    """🔴 本单的修复本体:有精确命中时,空行业候选**一条都不许出现**。

    旧口径 `... OR COALESCE(p.industry,'') = '' OR ...` 会让 4/5 号无条件穿透。
    """
    rows = get_effective_pool_candidates(industry="餐饮", limit=80)
    assert _names(rows) == ["美食门户"], _names(rows)
    assert "" not in _industries(rows)


def test_a_restaurant_query_never_surfaces_car_media(mixed_pool):
    """用户可见那句话本身。走精确档时成立。"""
    rows = get_effective_pool_candidates(industry="餐饮", limit=80)
    assert not any("汽车" in str(r.get("industry") or "") for r in rows), _names(rows)


def test_a_restaurant_query_never_surfaces_car_media_on_the_fallback_path_either(pool):
    """🔴 同一句话在**兜底路径**上也必须成立。

    兜底档只放"无行业标注"的候选;绝不因为精确档为空就把整池放出来
    (那等于把修好的洞从后门再开一次)。
    """
    pool.media(2, name="汽车之家分站", industry="汽车网站")
    pool.wemedia(3, name="汽车头条号", industry="汽车")
    pool.wemedia(4, name="无标注头条号", industry="")
    pool.commit()

    rows = get_effective_pool_candidates(industry="餐饮", limit=80)
    assert _names(rows) == ["无标注头条号"], _names(rows)
    assert not any("汽车" in str(r.get("industry") or "") for r in rows)


def test_fallback_tier_is_reachable_and_marked(pool):
    """反向对照:精确档为空 → 兜底档必须真的顶上(不是一刀切排除),且标 False。"""
    pool.wemedia(4, name="无标注头条号", industry="")
    pool.media(5, name="无标注门户", industry="")
    pool.commit()

    rows = get_effective_pool_candidates(industry="全屋定制", limit=80)
    assert _names(rows) == ["无标注头条号", "无标注门户"], _names(rows)
    assert all(r["industry_match"] is False for r in rows)


def test_matched_tier_is_marked_true(mixed_pool):
    rows = get_effective_pool_candidates(industry="餐饮", limit=80)
    assert rows and all(r["industry_match"] is True for r in rows)


def test_industry_match_takes_both_values_across_the_suite(pool):
    """🔴 分布锁:True 与 False 必须都出现过。

    没有这一条,把 industry_match 写死成常量(任一侧)都能让上面几条各自全绿。
    """
    pool.media(1, name="美食门户", industry="食品餐饮")
    pool.wemedia(4, name="无标注头条号", industry="")
    pool.commit()

    matched = get_effective_pool_candidates(industry="餐饮", limit=80)
    fallback = get_effective_pool_candidates(industry="全屋定制", limit=80)
    seen = {r["industry_match"] for r in matched} | {r["industry_match"] for r in fallback}
    assert seen == {True, False}, seen


# ══════════════════════════════════════════════════════════════
# 既有行为不回归
# ══════════════════════════════════════════════════════════════

def test_no_industry_query_still_returns_the_whole_pool(mixed_pool):
    """调用方没给行业 → 整池,行为与旧版逐字一致(不是"没行业就啥也不给")。"""
    rows = get_effective_pool_candidates(industry="", limit=80)
    assert len(rows) == 5, _names(rows)
    assert all(r["industry_match"] is True for r in rows), "没有行业就谈不上不匹配"


def test_whitespace_only_industry_is_treated_as_no_industry(mixed_pool):
    assert len(get_effective_pool_candidates(industry="   ", limit=80)) == 5


def test_industry_is_stripped_before_matching(mixed_pool):
    """' 餐饮 ' 与 '餐饮' 必须同解 —— 前端传的行业串常带空格。"""
    assert _names(get_effective_pool_candidates(industry=" 餐饮 ", limit=80)) == ["美食门户"]


def test_include_wemedia_false_excludes_wemedia_on_both_tiers(pool):
    pool.media(1, name="美食门户", industry="食品餐饮")
    pool.wemedia(6, name="美食头条号", industry="食品餐饮")
    pool.wemedia(4, name="无标注头条号", industry="")
    pool.media(5, name="无标注门户", industry="")
    pool.commit()

    matched = get_effective_pool_candidates(industry="餐饮", limit=80, include_wemedia=False)
    assert _names(matched) == ["美食门户"]
    # 反向对照:开着 wemedia 时那条自媒体是**在的**(证明上面不是因为它根本没入池)
    assert "美食头条号" in _names(
        get_effective_pool_candidates(industry="餐饮", limit=80, include_wemedia=True)
    )

    fallback = get_effective_pool_candidates(industry="全屋定制", limit=80, include_wemedia=False)
    assert _names(fallback) == ["无标注门户"]


def test_limit_applies_to_both_tiers(pool):
    for i in range(6):
        pool.media(100 + i, name=f"美食门户{i}", industry="食品餐饮", score=50.0 + i)
    for i in range(6):
        pool.media(200 + i, name=f"无标注门户{i}", industry="", score=50.0 + i)
    pool.commit()

    assert len(get_effective_pool_candidates(industry="餐饮", limit=3)) == 3
    assert len(get_effective_pool_candidates(industry="全屋定制", limit=2)) == 2


def test_non_recommendable_and_l0_are_still_excluded(pool):
    """既有硬条件不因本次改动松动。"""
    pool.media(1, name="美食门户", industry="食品餐饮")
    pool.media(7, name="美食门户不推荐", industry="食品餐饮", recommendable=False)
    pool.media(8, name="美食门户L0", industry="食品餐饮", tier="L0")
    pool.commit()

    assert _names(get_effective_pool_candidates(industry="餐饮", limit=80)) == ["美食门户"]
