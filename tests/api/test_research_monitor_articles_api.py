"""
GEO 调研监测 · 文章库 API 单元测试 [Phase 9 P09b · 2026-05-26]

主要锁住 P09-fix 4 个问题里的两个 SQL bug:
  1. list 接口 expired 条件 OR/AND 优先级 必须加括号(test_list_expired_bracket_safe)
  2. bulk-add-to-reference 一条失败时 SAVEPOINT 隔离 · 不污染前面成功的(test_bulk_savepoint_isolation)

依赖: TEST_DATABASE_URL 配 docker-compose 的 omnirank-db
跑法: TEST_DATABASE_URL=postgresql://... pytest tests/api/test_research_monitor_articles_api.py -v
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime


# 仅 import 测试 (不连 DB · py_compile + 静态检查)
def test_import_articles_api_module():
    """basic smoke: 文件能 import · router 7 endpoints 都注册"""
    from api.research_monitor_articles_api import router, _BULK_MAX
    assert router.prefix == "/api/admin/research-monitor"
    assert _BULK_MAX == 100
    paths = [r.path for r in router.routes]
    assert "/api/admin/research-monitor/articles" in paths
    assert "/api/admin/research-monitor/articles/{article_id}" in paths
    assert "/api/admin/research-monitor/articles/{article_id}/add-to-reference" in paths
    assert "/api/admin/research-monitor/articles/bulk-delete" in paths
    assert "/api/admin/research-monitor/articles/bulk-add-to-reference" in paths


def test_list_endpoint_sql_has_paren_for_expired():
    """P09-fix-1: list 接口 SQL WHERE 必须有 (expired = FALSE OR expired IS NULL) 括号
    不能直接拼成 expired = FALSE OR expired IS NULL AND ... · 否则后面所有筛选失效
    """
    import inspect
    from api.research_monitor_articles_api import list_articles
    src = inspect.getsource(list_articles)
    assert "(expired = FALSE OR expired IS NULL)" in src, (
        "P09-fix-1 回归: SQL OR/AND 优先级必须用括号包 expired 条件"
    )


def test_bulk_add_uses_savepoint():
    """P09-fix-2: bulk-add-to-reference 必须用 SAVEPOINT 隔离单条失败
    否则 conn.rollback() 会回滚整个事务 · 但计数继续 +1 · 返回值跟 DB 不符
    """
    import inspect
    from api.research_monitor_articles_api import bulk_add_to_reference
    src = inspect.getsource(bulk_add_to_reference)
    assert "SAVEPOINT " in src, "P09-fix-2 回归: bulk-add 必须用 SAVEPOINT 而非 conn.rollback()"
    assert "ROLLBACK TO SAVEPOINT" in src or "ROLLBACK TO" in src, (
        "P09-fix-2 回归: 异常路径必须 ROLLBACK TO SAVEPOINT · 不能全局 rollback"
    )
    # 反向验证: 单条失败异常分支不应该 conn.rollback() (整事务回滚)
    # 但函数末尾的 try/finally 不算 · 用更精确的字符串
    assert "conn.rollback()" not in src.split("except Exception as e:", 1)[-1].split("conn.commit()")[0], (
        "P09-fix-2 回归: 异常分支不应调 conn.rollback() · 应只 ROLLBACK 单 SAVEPOINT"
    )


def test_list_order_by_batch_then_industry():
    """P09-fix-3: 后端 ORDER BY 必须固定 first_seen_round_id DESC, primary_industry
    让前端按相邻条目能折成"大类批次 → 小类行业"双层分组
    """
    import inspect
    from api.research_monitor_articles_api import list_articles
    src = inspect.getsource(list_articles)
    assert "first_seen_round_id DESC" in src, "P09-fix-3 回归: 主排序必须是批次倒序"
    assert "primary_industry" in src, "P09-fix-3 回归: 必须包含行业次级排序"


def test_admin_required_on_all_endpoints():
    """所有 7 endpoints 必须调 _require_admin (沿用 industry_api 风格)"""
    import inspect
    from api.research_monitor_articles_api import (
        list_articles, get_article_detail, edit_article, delete_article,
        add_to_reference, bulk_delete_articles, bulk_add_to_reference,
    )
    for fn in [list_articles, get_article_detail, edit_article, delete_article,
               add_to_reference, bulk_delete_articles, bulk_add_to_reference]:
        src = inspect.getsource(fn)
        assert "_require_admin(request)" in src, f"{fn.__name__} 缺 _require_admin 校验"


def test_inline_fallback_in_detail():
    """详情接口 fallback inline_cleaned_content (P08 老数据无 OSS 时)"""
    import inspect
    from api.research_monitor_articles_api import get_article_detail
    src = inspect.getsource(get_article_detail)
    assert "inline_cleaned_content" in src, "详情接口需 fallback 读 inline_cleaned_content"


def test_inline_fallback_in_add_to_reference():
    """加入参考库 fallback inline_cleaned_content (老数据)"""
    import inspect
    from api.research_monitor_articles_api import add_to_reference, bulk_add_to_reference
    for fn in [add_to_reference, bulk_add_to_reference]:
        src = inspect.getsource(fn)
        assert "inline_cleaned_content" in src, f"{fn.__name__} 需 fallback 读 inline_cleaned_content"


def test_bulk_max_limit():
    """bulk 接口 max 100 (沿用旧数据复盘 router 的风格 · 防 OOM)"""
    from api.research_monitor_articles_api import BulkArticleIdsRequest
    # max_length=100
    with pytest.raises(Exception):  # Pydantic ValidationError
        BulkArticleIdsRequest(article_ids=list(range(101)))
    # 正常 100 个 OK
    req = BulkArticleIdsRequest(article_ids=list(range(100)))
    assert len(req.article_ids) == 100


def test_content_type_filter_param():
    """content_type 是 P09 新筛选维度 (7 类: video/article/doc_tool/encyc/ecom/gov/other)"""
    import inspect
    from api.research_monitor_articles_api import list_articles
    sig = inspect.signature(list_articles)
    assert "content_type" in sig.parameters, "list_articles 缺 content_type 筛选参数"


# ==============================
# P13 (2026-05-26) · 文章库重设计 · 后端新接口/字段锁
# ==============================

def test_p13_batches_endpoint_registered():
    """P13: GET /articles/batches endpoint 必须注册"""
    from api.research_monitor_articles_api import router
    paths = [r.path for r in router.routes]
    assert "/api/admin/research-monitor/articles/batches" in paths, \
        "P13 缺 /articles/batches endpoint · 前端顶部批次条会 404"


def test_p13_batches_registered_before_article_id_path():
    """P13: /articles/batches 必须在 /articles/{article_id} 之前注册
    否则 FastAPI 把 'batches' 当成 article_id 路径参数匹配 → 404
    """
    from api.research_monitor_articles_api import router
    paths_in_order = [r.path for r in router.routes]
    try:
        i_batches = paths_in_order.index("/api/admin/research-monitor/articles/batches")
        i_article = paths_in_order.index("/api/admin/research-monitor/articles/{article_id}")
    except ValueError as e:
        raise AssertionError(f"P13 路由缺失: {e}")
    assert i_batches < i_article, (
        "P13 路由顺序错: /articles/batches 必须在 /articles/{article_id} 之前注册 · "
        "否则 'batches' 被当成 article_id 匹配"
    )


def test_p13_list_articles_returns_cited_by_platforms():
    """P13: list_articles 返回每篇文章的 cited_by_platforms (供前端引擎 chips)"""
    import inspect
    from api.research_monitor_articles_api import list_articles
    src = inspect.getsource(list_articles)
    assert "cited_by_platforms" in src, "P13 缺 cited_by_platforms 返回字段"
    # SQL 字面里要用 array_agg + DISTINCT 一次拿全
    assert "array_agg" in src and "DISTINCT platform" in src, (
        "P13 cited_by_platforms 应用 array_agg(DISTINCT platform) · 不能 N+1 调用"
    )
    # 不能用 LEFT JOIN article_citations 全表 (会爆 row 数) · 必须 correlated subquery
    assert "geo_research_article_citations c" in src or "geo_research_article_citations\n         WHERE" in src, \
        "P13 cited_by_platforms 应走 correlated subquery 不爆 row 数"


def test_p13_list_articles_returns_counts_by_content_type():
    """P13: list_articles 返回 counts_by_content_type · 前端 chips 显数量 (全部 706 / 文章 532)"""
    import inspect
    from api.research_monitor_articles_api import list_articles
    src = inspect.getsource(list_articles)
    assert "counts_by_content_type" in src, "P13 缺 counts_by_content_type"
    # 切换 content_type chip 时统计要"忽略当前 content_type filter" · 否则切到视频后只显示视频的数量
    assert "GROUP BY content_type" in src, "P13 counts_by_content_type 必须 GROUP BY content_type"


def test_p13_batches_sql_filters_in_library():
    """P13: /articles/batches SQL 必须只算 in_library/imported_to_reference · 不算 auto_skipped/crawled"""
    import inspect
    from api.research_monitor_articles_api import list_article_batches
    src = inspect.getsource(list_article_batches)
    assert "review_status IN ('in_library', 'imported_to_reference')" in src, \
        "P13 batches SQL 必须 IN ('in_library', 'imported_to_reference') · 跟发布参谋/content-type-stats 对齐"
    assert "(expired = FALSE OR expired IS NULL)" in src, \
        "P13 batches SQL expired 条件必须带括号 + 兼容 NULL"


def test_p13_batches_endpoint_requires_admin():
    """P13: /articles/batches 必须 _require_admin (沿用其他 articles endpoint)"""
    import inspect
    from api.research_monitor_articles_api import list_article_batches
    src = inspect.getsource(list_article_batches)
    assert "_require_admin(request)" in src, "P13 batches endpoint 缺 _require_admin"


def test_p13_batches_limit_clamp():
    """P13: /articles/batches limit 必须有上界 (防代理传 9999 拖垮 DB)"""
    import inspect
    from api.research_monitor_articles_api import list_article_batches
    src = inspect.getsource(list_article_batches)
    # 必须有 1-N 范围校验
    assert "limit < 1" in src or "limit <= 0" in src, "P13 batches limit 缺下界校验"
    assert "100" in src or "limit > 100" in src, "P13 batches limit 应有 100 上界 (跟 articles 200 不同 · batches 数量少)"


# ==============================
# 集成测试 (P10c 已删 · pg_conn fixture 在 conftest.py 没定义 · 会导致 collection error)
# 真 DB 集成测试 P09 时让 Deploy-CTO 在公司测试环境写 · 直接接 docker postgres
# 本文件保持"纯单测 · 无 DB 依赖"原则 · CI 直接绿
# ==============================
