# -*- coding: utf-8 -*-
"""[Review-CTO 2026-07-27] 发布中心文章列表轻量判别锁(Owner:每次加载都很慢)。

慢因三座山:列表 SELECT 全文(5-15k×N)+逐篇正则清洗+JSON 传输——而全仓
零消费方(前端映射不含 content,详情走单独接口)。锁住:列表路径永不带正文。
"""
import inspect


def test_completed_articles_list_never_selects_full_content():
    from services.placement_service import PlacementService

    src = inspect.getsource(PlacementService.get_completed_articles)
    assert "a.content" not in src, "列表 SELECT 又把全文背回来了——那是每次加载慢的头号原因"
    assert "clean_llm_article" not in src, "列表层清洗属重复劳动,清洗归详情/发布路径"
    # 列表仍必须给出发布资格与推荐信息(功能不减)
    assert "evaluate_publication_eligibility" in src
    assert "placement_recommendations" in src
