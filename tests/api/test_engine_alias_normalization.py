"""
2026-06 老板复核 · placement_service.py 4 处 COUNT(DISTINCT engine) 必须归一别名
否则 raw.engine 历史脏数据 (doubao/豆包/kimi/Kimi/deepseek/DeepSeek/qwen/千问 共 8 种别名)
会让 engine_count 算出 5-8 · 但实际真引擎只有 4 个

锁两件事:
  1. (inspect) placement_service.py 不该再有裸 COUNT(DISTINCT engine)
  2. (functional) 插入 8 种别名 → 各 API 返 engine_count ≤ 4

跑: TEST_DATABASE_URL=postgresql://... pytest tests/api/test_engine_alias_normalization.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# Part 1 · inspect 锁
# ============================================================

class TestEngineNormalizeSourceLevel:
    """从源码层锁 · 不依赖 DB · 防归一被偷偷撤回"""

    def test_placement_service_has_engine_normalize_constant(self):
        src = (ROOT / "services" / "placement_service.py").read_text(encoding="utf-8")
        assert "_ENGINE_NORMALIZE_SQL" in src, \
            "placement_service.py 必须有 _ENGINE_NORMALIZE_SQL 常量(跟 citations_api 同口径)"
        # 必须含 8 个别名全覆盖
        for alias in ['doubao', '豆包', 'kimi', 'deepseek', 'qwen', '千问']:
            assert f"'{alias}'" in src, f"_ENGINE_NORMALIZE_SQL 必须含别名 {alias!r}"

    def test_placement_service_no_raw_count_distinct_engine(self):
        """所有 COUNT(DISTINCT engine) 必须用 CASE 归一 · 不能裸 engine 列名"""
        src = (ROOT / "services" / "placement_service.py").read_text(encoding="utf-8")
        import re
        # 跳过 Python 注释行(# 开头 · 去掉前导空白后)防误报自己留的"COUNT(DISTINCT engine)"备忘
        non_comment_src = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        bare = re.findall(r"COUNT\(DISTINCT\s+(?:r\.|p\.)?engine\)", non_comment_src, re.IGNORECASE)
        assert not bare, \
            f"placement_service.py 不应再有裸 COUNT(DISTINCT engine) · 必须 CASE 归一 · " \
            f"找到 {len(bare)} 处: {bare}"

    def test_placement_service_string_agg_engine_normalized(self):
        """STRING_AGG(DISTINCT engine, ',') 也要归一 · 防显示 'doubao,豆包,kimi,Kimi'"""
        src = (ROOT / "services" / "placement_service.py").read_text(encoding="utf-8")
        import re
        # 找裸 STRING_AGG(DISTINCT engine, ...)
        bare = re.findall(r"STRING_AGG\(DISTINCT\s+engine\s*,", src, re.IGNORECASE)
        assert not bare, \
            f"placement_service.py 不应再有裸 STRING_AGG(DISTINCT engine, ...) · " \
            f"必须 CASE 归一防 'doubao,豆包' 这种花式输出 · 找到 {bare}"


# ============================================================
# Part 2 · functional 锁 · 真插脏数据 + 验 API engine_count ≤ 4
# ============================================================

@pytest.fixture
def industry_with_alias_pollution():
    """造一个行业 + 1 个 query · 该 query 8 种引擎别名都有 raw 数据
    模拟历史脏数据状态 · 用于验证 API 归一后 engine_count = 4
    """
    import psycopg2
    import psycopg2.extras
    import os
    url = os.environ.get('TEST_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not url:
        pytest.skip("TEST_DATABASE_URL 未设")
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()

    ind_name = 'alias_normalize_test_industry'
    prompt_text = 'alias_normalize_test_query'

    # cleanup
    cur.execute("DELETE FROM geo_research_raw WHERE industry = %s", (ind_name,))
    cur.execute("""
        DELETE FROM geo_research_prompts
         WHERE industry_id IN (SELECT id FROM geo_research_industries WHERE name = %s)
    """, (ind_name,))
    cur.execute("DELETE FROM geo_research_industries WHERE name = %s", (ind_name,))

    # 建 industry + prompt
    cur.execute("""
        INSERT INTO geo_research_industries (name, slug, sort_order, active)
        VALUES (%s, 'alias_norm_test', 999, TRUE) RETURNING id
    """, (ind_name,))
    ind_id = cur.fetchone()['id']
    cur.execute("""
        INSERT INTO geo_research_prompts (industry_id, prompt_text, sort_order, active)
        VALUES (%s, %s, 0, TRUE)
    """, (ind_id, prompt_text))

    # 同 query · 8 种别名 raw 数据 · 全指向同一 URL
    # source_analysis 按 URL 聚合 · 这样测得到 cross_engine engine_count 归一效果
    aliases = ['doubao', '豆包', 'kimi', 'Kimi', 'deepseek', 'DeepSeek', 'qwen', '千问']
    shared_url = 'https://example.com/alias-normalize-test-shared'
    for i, alias in enumerate(aliases):
        cur.execute("""
            INSERT INTO geo_research_raw
                (industry, query, engine, cited_platform, cite_position,
                 cite_url, batch_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        """, (
            ind_name, prompt_text, alias, 'cnpiw.cn', i + 1,
            shared_url, 'batch_alias_norm_test',
        ))
    conn.commit()

    yield {'industry_name': ind_name, 'prompt_text': prompt_text, 'shared_url': shared_url}

    # teardown
    cur.execute("DELETE FROM geo_research_raw WHERE industry = %s", (ind_name,))
    cur.execute("""
        DELETE FROM geo_research_prompts
         WHERE industry_id IN (SELECT id FROM geo_research_industries WHERE name = %s)
    """, (ind_name,))
    cur.execute("DELETE FROM geo_research_industries WHERE name = %s", (ind_name,))
    conn.commit()
    conn.close()


class TestEngineNormalizeFunctional:
    """真插 8 别名 → 调 API → 验 engine_count ≤ 4"""

    def test_industry_prompts_preview_engine_count_le_4(self, industry_with_alias_pollution):
        """get_industry_prompts_preview (用户截图看到的 "X 引擎命中") · 归一后 = 4"""
        from services.placement_service import get_placement_service
        svc = get_placement_service()
        result = svc.get_industry_prompts_preview(industry_with_alias_pollution['industry_name'])
        items = result.get('items') or []
        assert items, "fixture 已建 prompts 表 prompt · API 应至少返 1 条"
        # 找到我们造的 prompt
        target = next(
            (i for i in items if i.get('prompt') == industry_with_alias_pollution['prompt_text']),
            None,
        )
        assert target is not None, "测试 prompt 必须出现在 items"
        assert target['engine_count'] == 4, \
            f"8 别名 · 归一后 engine_count 必须 = 4 · 实际 {target['engine_count']}"

    def test_research_summary_by_industry_engines_le_4(self, industry_with_alias_pollution):
        """get_research_summary 的 by_industry 列表 'engines' 字段要归一"""
        from services.placement_service import get_placement_service
        svc = get_placement_service()
        result = svc.get_research_summary()
        by_industry = result.get('by_industry') or []
        ind_row = next(
            (r for r in by_industry
             if r.get('industry') == industry_with_alias_pollution['industry_name']),
            None,
        )
        assert ind_row is not None, "测试行业必须出现在 by_industry 列表"
        assert ind_row.get('engines') <= 4, \
            f"engines 必须 ≤ 4 (归一后真引擎数) · 实际 {ind_row.get('engines')}"
        assert ind_row.get('engines') == 4, \
            f"8 别名全有 → engines 应该 = 4 · 实际 {ind_row.get('engines')}"

    def test_research_source_analysis_engine_count_eq_4(self, industry_with_alias_pollution):
        """source_analysis cross-engine 也要归一 · 包括 STRING_AGG"""
        from services.placement_service import get_placement_service
        svc = get_placement_service()
        ind = industry_with_alias_pollution
        result = svc.get_research_source_analysis(ind['industry_name'])
        cross = result.get('cross_engine_sources') or []
        target = next((s for s in cross if s.get('url') == ind['shared_url']), None)
        assert target is not None, \
            f"测试 URL {ind['shared_url']!r} 必须出现在 cross_engine_sources(8 别名 → 归一后 4 引擎共享)"
        assert target['engine_count'] == 4, \
            f"engine_count 必须 = 4(8 别名归一)· 实际 {target['engine_count']}"
        engines = target.get('engines') or []
        # engines 是 list (split by ',') · 不该同时出现 doubao 和 豆包 这种别名对
        engines_lower = {e.strip().lower() for e in engines}
        assert not ('doubao' in engines_lower and '豆包' in engines), \
            f"engines 不该同时出现 doubao 和 豆包(应归一)· 实际: {engines}"
        assert not ('kimi' in engines and 'Kimi' in engines), \
            f"engines 不该同时出现 kimi 和 Kimi(应归一)· 实际: {engines}"
        assert len(engines) == 4, \
            f"engines list 应该正好 4 项(归一后)· 实际 {len(engines)}: {engines}"
