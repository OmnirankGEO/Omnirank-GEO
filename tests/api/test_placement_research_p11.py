"""
GEO 发布参谋 P11 后端单测 [2026-05-26]

锁住:
  1. summary 必须含 last_updated_at + total_engines + by_industry[].last_round_at
  2. content-type-stats endpoint 注册 + WHERE 严格筛 review_status + expired
  3. prompts-preview endpoint 严禁返回 answer_text / cite_url / cite_excerpt (代理 UI 安全)
  4. prompts-preview limit clamp (1~200) 防 DOS
  5. 不破坏现有 /research/* 路由

依赖: 跟 conftest 一致需 TEST_DATABASE_URL · 但本文件全用 inspect (不真连 DB)
跑法: TEST_DATABASE_URL=postgresql://... pytest tests/api/test_placement_research_p11.py -v
"""
from __future__ import annotations

import inspect


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------

def test_p11_endpoints_registered():
    """P11 加的 2 个 endpoint 必须在 placement_api.router 里(注意 router prefix /api/placement)"""
    from api.placement_api import router
    paths = [r.path for r in router.routes]
    assert "/api/placement/research/industry/{industry}/content-type-stats" in paths, \
        "P11 缺 content-type-stats endpoint"
    assert "/api/placement/research/industry/{industry}/prompts-preview" in paths, \
        "P11 缺 prompts-preview endpoint"


def test_p11_legacy_endpoints_preserved():
    """P11 不能破坏现有路由 (兼容性 · 前端删入口但后端兼容老调用方)"""
    from api.placement_api import router
    paths = [r.path for r in router.routes]
    for p in [
        "/api/placement/research/summary",
        "/api/placement/research/industry/{industry}",
        "/api/placement/research/industry/{industry}/details",
        "/api/placement/research/industry/{industry}/sources",
        "/api/placement/research/template",
        "/api/placement/research/upload",
        "/api/placement/research/cleanup",
    ]:
        assert p in paths, f"P11 不能误删旧路由 {p}"


# ---------------------------------------------------------------------------
# summary 增强 (last_updated_at / total_engines / last_round_at)
# ---------------------------------------------------------------------------

def test_summary_returns_last_updated_at():
    """P11: summary 必须 SELECT last_updated_at (前端 4 卡片用)"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_research_summary)
    assert "last_updated_at" in src, "P11 summary 缺 last_updated_at 字段"
    # 实现应是 GREATEST(MAX(batches.completed_at), MAX(raw.created_at))
    assert "GREATEST" in src, "P11 last_updated_at 应取 batches/raw 时间最大值"


def test_summary_returns_total_engines():
    """P11: summary 必须返回 total_engines (4 卡片之一)"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_research_summary)
    assert "total_engines" in src, "P11 summary 缺 total_engines 字段"
    assert "DISTINCT engine" in src, "total_engines 应用 COUNT(DISTINCT engine)"


def test_summary_by_industry_has_last_round_at():
    """P11: by_industry 每项必须含 last_round_at (左侧行业新鲜度徽章)"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_research_summary)
    assert "MAX(created_at) as last_round_at" in src, \
        "P11 by_industry 缺 last_round_at"


# ---------------------------------------------------------------------------
# content-type-stats 口径
# ---------------------------------------------------------------------------

def test_content_type_stats_filters_in_library_only():
    """P11: content-type-stats 必须严格筛 review_status (审核中/拒绝/老 crawled 都排除)"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_industry_content_type_stats)
    assert "review_status IN ('in_library', 'imported_to_reference')" in src, \
        "P11 content-type-stats 必须只统计入库 + 已导入参考库的文章"


def test_content_type_stats_excludes_expired():
    """P11: content-type-stats 必须排除过期文章(老数据自然衰减)"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_industry_content_type_stats)
    assert "(expired = FALSE OR expired IS NULL)" in src, \
        "P11 content-type-stats expired 条件必须括号 + 兼容 NULL"


def test_content_type_stats_groups_by_content_type():
    """P11: content-type-stats 必须 GROUP BY content_type"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_industry_content_type_stats)
    assert "GROUP BY content_type" in src, "P11 必须按 content_type 分组"


def test_content_type_stats_separates_uncategorized():
    """P11: 老数据 content_type IS NULL 单独算 uncategorized_count · 不混进 by_type"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_industry_content_type_stats)
    assert "uncategorized_count" in src, \
        "P11 content-type-stats 必须把 NULL 类型单独算 uncategorized_count"


# ---------------------------------------------------------------------------
# prompts-preview 安全性 (P11 最关键点 · 不能泄 AI 原文)
# ---------------------------------------------------------------------------

def test_prompts_preview_never_returns_answer_text():
    """P11 安全红线: prompts-preview 严禁 SELECT answer_text / cite_url / cite_excerpt"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_industry_prompts_preview)
    # SELECT 子句不能含敏感字段
    forbidden = ['answer_text', 'cite_url', 'cite_excerpt', 'cite_title',
                 'inline_cleaned_content', 'oss_key_raw', 'oss_key_cleaned']
    for kw in forbidden:
        assert kw not in src, \
            f"P11 安全红线: prompts-preview 不能涉及 {kw} (会泄 AI 原文给代理)"


def test_prompts_preview_uses_prompts_table_first():
    """P11: prompts-preview 必须优先 geo_research_prompts 表 · raw 只做 fallback"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_industry_prompts_preview)
    assert "geo_research_prompts" in src, "P11 prompts-preview 必须用 prompts 表"
    assert "geo_research_industries" in src, "必须 JOIN industries 表按 name 关联"
    # raw fallback 也要有
    assert "geo_research_raw" in src, "P11 prompts-preview 必须有 raw fallback"


def test_prompts_preview_returns_metadata_only():
    """P11: prompts-preview 返回字段限定 prompt + engine_count + citation_count"""
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_industry_prompts_preview)
    assert "engine_count" in src, "缺 engine_count"
    assert "citation_count" in src, "缺 citation_count"


def test_prompts_preview_endpoint_clamps_limit():
    """P11 防御: limit 必须 clamp 到 [1, 200] · 防代理传大 limit 拖垮 DB"""
    from api.placement_api import get_industry_prompts_preview
    src = inspect.getsource(get_industry_prompts_preview)
    # 实现里要有 min/max 或类似 clamp 逻辑
    assert ("min(" in src and "max(" in src) or "clamp" in src.lower(), \
        "P11 prompts-preview limit 缺 clamp"
    # 至少有 200 上界
    assert "200" in src, "P11 prompts-preview limit 上界应是 200"


# ---------------------------------------------------------------------------
# P11 v2 fix (2026-05-26) · Review 给出的 High + Medium 风险锁
# ---------------------------------------------------------------------------

def test_source_analysis_matrix_shape_is_platform_outer():
    """[v2 锁] /sources 返回的 platform_engine_matrix shape 必须是 platform → engine → count

    防回归: 这是 placement_service.py:2732 的合同 · 前端依赖它。
    历史 bug: GeoResearchCenter 初版把 outer key 当 engine 用,导致 Top10/Matrix 全错位
    后续修法: 前端不再调 /sources · 但后端合同仍需锁住,防其他调用方踩同坑
    """
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_research_source_analysis)
    # 关键赋值字面: platform_matrix[plat][r['engine']] = r['cnt']
    assert "platform_matrix[plat][r['engine']]" in src, (
        "[v2] /sources matrix shape 改了 · 必须保持 platform → engine → count"
    )


def test_publication_advisor_uses_weighted_platform_scores():
    """[v2 锁 HIGH] 发布参谋 Top10/Matrix 必须走 platform_scores (geo_engine_stats 加权)

    旧实现 [HIGH bug]: 走 /sources 的 platform_engine_matrix (raw count · 没 month_weights 衰减)
    新实现: 走 get_industry_engine_scores → geo_engine_stats → 已加权
    锁住: 该函数 SQL 必须 FROM geo_engine_stats · 必须用 engine_weights × citation_rate
    """
    from services.placement_service import PlacementService
    src = inspect.getsource(PlacementService.get_industry_engine_scores)
    assert "FROM geo_engine_stats" in src, "[v2] platform_scores 必须从 geo_engine_stats 读 · 不能走 raw"
    assert "engine_weights" in src, "[v2] platform_scores score 必须乘 engine_weights"
    assert "citation_rate" in src, "[v2] score 公式必须含 citation_rate"


def test_frontend_publication_advisor_does_not_call_sources():
    """[v2 锁 MEDIUM] 发布参谋前端不许调 /research/industry/{id}/sources

    /sources 同时返回 cross_engine_sources / engine_unique_sources · 里有 url + title
    代理/销售只该看 加权矩阵 · 不该有原始引用 URL 可访问 (即使页面不渲染 · Network 也能看见)

    防御性测: 在前端文件里 grep · 命中即测试失败
    """
    import os
    fe_path = os.path.join(
        os.path.dirname(__file__), '..', '..',
        'frontend', 'src', 'pages', 'GeoResearch', 'GeoResearchCenter.tsx'
    )
    fe_path = os.path.normpath(fe_path)
    if not os.path.exists(fe_path):
        # CI 环境可能不带前端 · skip 而非 fail
        import pytest as _pytest
        _pytest.skip(f"前端文件不存在: {fe_path}")
    with open(fe_path, 'r', encoding='utf-8') as f:
        src = f.read()
    # 注释里出现是允许的 (说明性) · 实际 fetch 调用不允许
    # 检测: 寻找 authFetch / fetch + /sources URL 拼接
    forbidden_patterns = [
        "authFetch(`/api/placement/research/industry/${encodeURIComponent(industry)}/sources`)",
        "authFetch('/api/placement/research/industry/' + ",  # 字符串拼接形式
        "fetch(`/api/placement/research/industry/${",        # 万一不用 authFetch
    ]
    for pat in forbidden_patterns:
        if pat.endswith("/sources`)"):
            assert pat not in src, (
                "[v2 MEDIUM] GeoResearchCenter 不许调 /sources endpoint · 防 url/title 泄漏到 Network"
            )
    # 更通用的检测: tsx 里出现 `/sources` 作为 endpoint 路径
    # 排除注释里的说明 (注释行以 // 或 * 开头)
    code_lines = [
        ln for ln in src.splitlines()
        if '/sources' in ln
        and not ln.strip().startswith('//')
        and not ln.strip().startswith('*')
    ]
    assert len(code_lines) == 0, (
        f"[v2 MEDIUM] GeoResearchCenter 代码行(非注释)出现 /sources · 应只在注释说明里:\n"
        + "\n".join(code_lines[:5])
    )
