"""接线锁:兜底档必须**走到用户面前**,而不是只在 DB 层多了一个字段。

🔴 为什么单独立一条:本仓刚踩过两次"判据打在函数上、生产那条线没接"。
   `industry_match` 只是个 bool;它要有意义,得有人读。读它的是
   `api.publish_api._build_media_package_response`,它把兜底档翻成
   `fallback_notice` —— 而 `fallback_notice` 已经有前端出口
   (frontend/src/pages/Publishing/PublishCenter.tsx:3712-3713 渲染这一串)。

🔴 另一条同样重要:SQL 里的 ORDER BY **活不过下游**。
   services/publish_recommendation.build_recommendation_packages 会按
   effective_score 重排(:462),所以"把无行业候选排到后面"这种修法在
   用户那一端等于没改。下面 test_ordering_does_not_survive_downstream 用
   真链路把这件事钉死,免得将来有人把集合分档"优化"回排序降权。
"""

from __future__ import annotations

import asyncio

import pytest

from api.publish_api import _build_media_package_response


def _run(**kwargs):
    return asyncio.run(_build_media_package_response(**kwargs))


def _summary(industry: str) -> dict:
    return {
        "industry": industry,
        "article_type": "qa_solve",
        "semantic_keywords": ["装修", "餐厅"],
        "publish_goal": "提升 AI 搜索引用",
    }


@pytest.fixture()
def seeded(pool):
    pool.media(1, name="美食门户", industry="食品餐饮")
    pool.media(2, name="汽车之家分站", industry="汽车网站")
    pool.media(4, name="无标注门户", industry="")
    pool.commit()
    return pool


def test_fallback_tier_reaches_the_user_as_a_notice(seeded):
    """精确档为空 → 用户拿到一句说明,而不是一串莫名其妙的媒体。"""
    payload = _run(
        article_summary=_summary("全屋定制"),
        recommendation_level=2,
        include_wemedia=True,
        quota_used=0,
    )
    notice = payload.get("fallback_notice") or ""
    assert "全屋定制" in notice, notice
    # 出参不许泄内部口径:不出现 industry_match / 兜底档 / 有效池 这类工程词
    for jargon in ("industry_match", "兜底", "有效池", "media_effective_pool"):
        assert jargon not in notice, notice


def test_matched_tier_says_nothing(seeded):
    """🔴 反向对照:精确命中时**不许**出现这句提示。

    没有这一条,把提示写成无条件下发照样绿 —— 而那违反「非必要不警告」。
    """
    payload = _run(
        article_summary=_summary("餐饮"),
        recommendation_level=2,
        include_wemedia=True,
        quota_used=0,
    )
    assert not payload.get("fallback_notice"), payload.get("fallback_notice")


def test_existing_notice_is_not_clobbered(seeded):
    """降级通知信息量更大,不许被兜底提示顶掉。"""
    payload = _run(
        article_summary=_summary("全屋定制"),
        recommendation_level=3,
        include_wemedia=True,
        quota_used=0,
        fallback_notice="实时 AI 暂不可用，已使用历史推荐池",
    )
    assert payload["fallback_notice"] == "实时 AI 暂不可用，已使用历史推荐池"


def test_no_industry_query_says_nothing(seeded):
    """调用方压根没给行业 → 无所谓"不匹配",不该提示。"""
    payload = _run(
        article_summary=_summary(""),
        recommendation_level=2,
        include_wemedia=True,
        quota_used=0,
    )
    assert not payload.get("fallback_notice")


def test_car_media_never_reaches_the_packages_for_a_restaurant_article(seeded):
    """端到端那句话:组合包里不许出现汽车号。"""
    payload = _run(
        article_summary=_summary("餐饮"),
        recommendation_level=2,
        include_wemedia=True,
        quota_used=0,
    )
    names = [
        item.get("media_name") or item.get("platform_name") or ""
        for pkg in payload.get("packages") or []
        for item in pkg.get("items") or []
    ]
    assert names, "组合包空了 —— 这条锁会退化成恒真,先修数据再谈判别力"
    assert not any("汽车" in n for n in names), names


def test_ordering_does_not_survive_downstream(seeded, pool):
    """🔴 元判据:证明"靠 SQL 排序给兜底档降权"这种修法是**死排序**。

    构造:无行业候选的 effective_score 高于精确命中。若靠排序降权,
    下游按 effective_score 重排后它照样排到前面 —— 所以必须集合层分档。
    这条锁直接盯住"分档"这一性质:精确档存在时,结果里一条无行业候选都没有,
    **与分数高低无关**。
    """
    pool.media(9, name="超高分无标注门户", industry="", score=999.0)
    pool.commit()

    payload = _run(
        article_summary=_summary("餐饮"),
        recommendation_level=2,
        include_wemedia=True,
        quota_used=0,
    )
    names = [
        item.get("media_name") or item.get("platform_name") or ""
        for pkg in payload.get("packages") or []
        for item in pkg.get("items") or []
    ]
    assert names
    assert "超高分无标注门户" not in names, names
