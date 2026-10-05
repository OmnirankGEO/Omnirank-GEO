"""Stage 1-3 测试 (mock 4 平台 / Jina / OSS)"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.integration

from services.research_monitor.round_runner import (
    stage1_ai_fetch_all,
    stage2_extract_and_prefilter_urls,
    stage3_crawl_articles,
    stage45_classify_article_intents,
)
from services.research_monitor.article_intent_classifier import IntentClassification
from services.research_monitor.round_state import create_round_with_snapshot


def _mock_platform_response(platform: str = 'doubao', url: str = 'https://x.com/p/1'):
    """通用 mock platform 返回结构 (跟 platforms.py 对齐)"""
    return {
        'platform': platform,
        'prompt_id': 1,
        'prompt': '测试',
        'answer': '测试回答',
        'citations': [{'url': url, 'title': 'T', 'rank': 1}],
        'ok': True,
        'error': None,
        'raw': {},
    }


# ==================== Stage 1 ====================

class TestStage1AIFetch:

    @pytest.mark.asyncio
    async def test_stage1_writes_round_call_per_prompt_platform(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """3 prompts × 4 平台 = 12 行 round_call, 每行单独 commit"""
        cur = pg_conn.cursor()
        cur.execute(
            "SELECT name FROM geo_research_industries WHERE id = %s",
            (sample_industry,),
        )
        ind_name = cur.fetchone()['name']

        round_id = create_round_with_snapshot(
            triggered_by='manual',
            industries=[{'id': sample_industry, 'name': ind_name, 'slug': 'test'}],
            prompts_by_industry={
                sample_industry: [{'id': pid, 'text': '测试'} for pid in sample_prompts]
            },
        )

        plan = [
            {
                'industry_id': sample_industry,
                'industry_name': ind_name,
                'prompt_id': pid,
                'prompt_text': '测试',
            }
            for pid in sample_prompts
        ]

        with patch(
            'services.research_monitor.round_runner.query_with_retry',
            new_callable=AsyncMock,
        ) as m_retry:
            # 让 query_with_retry 直接返回 mock 平台响应
            async def fake_retry(fetcher, prompt_id, prompt, max_retries=3):
                # fetcher 是 PLATFORM_FETCHERS 的某个 (query_doubao/etc)
                platform_label = fetcher.__name__.replace('query_', '')
                return _mock_platform_response(platform_label)

            m_retry.side_effect = fake_retry

            await stage1_ai_fetch_all(round_id, plan)

        # 验证: 12 行 round_call, 全部 success
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM geo_research_round_call WHERE round_id = %s AND status = 'success'",
            (round_id,),
        )
        assert cur.fetchone()['cnt'] == 12  # 3 prompts × 4 platforms

    @pytest.mark.asyncio
    async def test_stage1_failed_call_marked_failed(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """单条调用失败不阻塞后续, status='failed' 入 round_call"""
        round_id = create_round_with_snapshot(
            triggered_by='manual',
            industries=[],
            prompts_by_industry={},
        )
        plan = [
            {
                'industry_id': sample_industry,
                'industry_name': 'x',
                'prompt_id': sample_prompts[0],
                'prompt_text': '测试',
            }
        ]

        with patch(
            'services.research_monitor.round_runner.query_with_retry',
            new_callable=AsyncMock,
        ) as m_retry:
            async def fake_retry(fetcher, prompt_id, prompt, max_retries=3):
                platform_label = fetcher.__name__.replace('query_', '')
                if platform_label == 'doubao':
                    raise RuntimeError("test fail")
                return _mock_platform_response(platform_label)

            m_retry.side_effect = fake_retry

            await stage1_ai_fetch_all(round_id, plan)

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT platform, status FROM geo_research_round_call WHERE round_id = %s ORDER BY platform",
            (round_id,),
        )
        rows = cur.fetchall()
        statuses = {r['platform']: r['status'] for r in rows}
        # 4 个 platform 全部 insert (即使 doubao 失败)
        assert len(rows) == 4
        # doubao failed, 其他 3 个 success
        assert statuses.get('doubao') == 'failed'
        for p in ['deepseek', 'qwen', 'kimi']:
            assert statuses.get(p) == 'success'

    @pytest.mark.asyncio
    async def test_stage1_circuit_breaker_trips_on_consecutive_failures(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """连续 N 次失败触发熔断, 后续 prompt 不跑"""
        round_id = create_round_with_snapshot(
            triggered_by='manual',
            industries=[],
            prompts_by_industry={},
        )
        # 50 prompts × 4 平台 = 200 calls 全失败 → 熔断后停, 总数应 < 200
        plan = [
            {
                'industry_id': sample_industry,
                'industry_name': 'x',
                'prompt_id': sample_prompts[0],
                'prompt_text': f'测试 {i}',
            }
            for i in range(50)
        ]

        with patch(
            'services.research_monitor.round_runner.query_with_retry',
            new_callable=AsyncMock,
        ) as m_retry:
            m_retry.side_effect = RuntimeError("all fail")

            await stage1_ai_fetch_all(round_id, plan)

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM geo_research_round_call WHERE round_id = %s",
            (round_id,),
        )
        cnt = cur.fetchone()['cnt']
        assert cnt < 200, f"熔断未生效, 跑了 {cnt} 次"


# ==================== Stage 2 ====================

class TestStage2PreFilter:

    def test_stage2_extracts_urls_from_round_call(
        self, pg_conn, sample_industry, clean_research_tables,
    ):
        """Stage 1 写完后, Stage 2 从 geo_research_raw 提取 + 预过滤"""
        round_id = create_round_with_snapshot(
            triggered_by='manual',
            industries=[],
            prompts_by_industry={},
        )

        cur = pg_conn.cursor()
        for url in [
            'https://www.digiwin.com/p/13752.html',  # 通过
            'https://douyin.com/video/123',          # 黑名单
            'https://example.com/file.pdf',          # 后缀
            'https://example.com/search?q=x',        # pattern
            'https://www.36kr.com/p/2026',           # 通过
        ]:
            cur.execute(
                """
                INSERT INTO geo_research_raw
                    (industry, query, engine, cited_platform, cite_position,
                     cite_url, batch_id, researcher)
                VALUES (%s, %s, 'doubao', %s, 1, %s, %s, '')
                """,
                ('test', '测试', url, url, f'batch_{round_id}'),
            )
        pg_conn.commit()

        urls = stage2_extract_and_prefilter_urls(round_id, batch_id=f'batch_{round_id}')

        # 应保留 2 个通过的 (digiwin / 36kr)
        assert len(urls) == 2
        urls_only = [u['normalized_url'] for u in urls]
        assert 'https://digiwin.com/p/13752.html' in urls_only
        assert 'https://36kr.com/p/2026' in urls_only


# ==================== Stage 3 ====================

class TestStage3Crawl:

    @pytest.mark.asyncio
    async def test_stage3_writes_articles_and_citations(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """爬完写 articles + citations"""
        round_id = create_round_with_snapshot(
            triggered_by='manual',
            industries=[],
            prompts_by_industry={},
        )
        urls_to_crawl = [
            {
                'url': 'https://www.digiwin.com/p/13752.html',
                'normalized_url': 'https://digiwin.com/p/13752.html',
                'industry_id': sample_industry,
                'industry_name': '测试',
                'prompt_id': sample_prompts[0],
                'platform': 'doubao',
            }
        ]

        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl, patch(
            'services.research_monitor.round_runner.upload_markdown'
        ) as m_upload:
            m_crawl.return_value = {
                'url': 'https://digiwin.com/p/13752.html',
                'title': 'ERP 排行榜',
                'content': '# ERP 排行榜\n\n2026 年综合实力榜...' * 100,
                'char_count': 4000,
                'fetched_at': '2026-05-06T10:00:00',
                'ok': True,
                'error': None,
            }
            m_upload.return_value = {
                'ok': True,
                'oss_key': 'raw/2026-05/digiwin.com/abc.md',
                'error': None,
            }

            await stage3_crawl_articles(round_id, urls_to_crawl)

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM geo_research_articles WHERE first_seen_round_id = %s",
            (round_id,),
        )
        assert cur.fetchone()['cnt'] == 1

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM geo_research_article_citations WHERE round_id = %s",
            (round_id,),
        )
        assert cur.fetchone()['cnt'] == 1

    @pytest.mark.asyncio
    async def test_stage3_dedup_by_url(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """同 URL 已存在不重爬, 只插 citation"""
        round_id_1 = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        round_id_2 = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )

        urls = [
            {
                'url': 'https://digiwin.com/p/13752.html',
                'normalized_url': 'https://digiwin.com/p/13752.html',
                'industry_id': sample_industry,
                'industry_name': '测试',
                'prompt_id': sample_prompts[0],
                'platform': 'doubao',
            }
        ]

        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl, patch(
            'services.research_monitor.round_runner.upload_markdown'
        ) as m_upload:
            m_crawl.return_value = {
                'url': 'https://digiwin.com/p/13752.html',
                'title': 'ERP',
                'content': 'a' * 1000,
                'char_count': 1000,
                'fetched_at': '2026-05-06T10:00',
                'ok': True,
                'error': None,
            }
            m_upload.return_value = {'ok': True, 'oss_key': 'raw/x.md', 'error': None}

            await stage3_crawl_articles(round_id_1, urls)
            # 第二轮同 URL, 不应重爬
            await stage3_crawl_articles(round_id_2, urls)

        cur = pg_conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM geo_research_articles")
        assert cur.fetchone()['cnt'] == 1  # 只 1 篇 article (url_hash UNIQUE)

        cur.execute(
            """
            SELECT COUNT(*) AS cnt FROM geo_research_article_citations
             WHERE round_id IN (%s, %s)
            """,
            (round_id_1, round_id_2),
        )
        assert cur.fetchone()['cnt'] == 2  # 2 条 citation (各轮 1 条)


# ==================== Stage 3 P14.1 · 全路径心跳 + skip reason 分类 ====================
#
# 背景: 老板真实跑批 1 prompt (round_20260528_2216 ...) 暴露:
#   - 旧 stage3 只在 "成功写文章" 路径才 flush · existing/jina_fail/short/oss_fail 4 个
#     return 路径不刷 · UI 看着两分钟无心跳直到全跑完一刷新就完
#   - summary_json 无 skip reason 分类 · 18 次 Jina 0 篇新文章但看不出原因
#
# 本测试组锁:
#   1. 4 种 skip path 都进 reason 计数器
#   2. 每条 URL 处理后必触发 _flush_progress (节流 5/10s)
#   3. progress_json 暴露 6 类 reasons + processed + total

class TestStage3PathCoverage:

    @staticmethod
    def _select_round_progress(pg_conn, round_id: str) -> dict:
        cur = pg_conn.cursor()
        cur.execute(
            "SELECT progress_json FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        return (row or {}).get('progress_json') or {}

    @pytest.mark.asyncio
    async def test_stage3_jina_failed_increments_skipped_jina(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """Jina ok=False · reasons.skipped_jina_failed=1 · 心跳更新"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [{
            'url': 'https://x.com/fail',
            'normalized_url': 'https://x.com/fail',
            'industry_id': sample_industry,
            'industry_name': '测试',
            'prompt_id': sample_prompts[0],
            'platform': 'doubao',
        }]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl:
            m_crawl.return_value = {'ok': False, 'error': 'timeout', 'char_count': 0}
            await stage3_crawl_articles(round_id, urls)

        progress = self._select_round_progress(pg_conn, round_id)
        assert progress.get('processed') == 1
        assert progress.get('total') == 1
        reasons = progress.get('reasons') or {}
        assert reasons.get('skipped_jina_failed') == 1
        assert reasons.get('crawled_new') == 0
        # 兼容字段 (旧前端): skipped 计 5 个 non-crawled 之和
        assert progress.get('crawled') == 0
        assert progress.get('skipped') == 1

    @pytest.mark.asyncio
    async def test_stage3_short_content_increments_skipped_short(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """Jina ok 但 char_count < MIN_ARTICLE_CHARS · reasons.skipped_short=1"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [{
            'url': 'https://x.com/short',
            'normalized_url': 'https://x.com/short',
            'industry_id': sample_industry,
            'industry_name': '测试',
            'prompt_id': sample_prompts[0],
            'platform': 'doubao',
        }]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl:
            m_crawl.return_value = {
                'ok': True, 'content': 'short', 'char_count': 50, 'title': 'T',
            }
            await stage3_crawl_articles(round_id, urls)

        reasons = (self._select_round_progress(pg_conn, round_id).get('reasons') or {})
        assert reasons.get('skipped_short') == 1
        assert reasons.get('crawled_new') == 0
        assert reasons.get('skipped_jina_failed') == 0

    @pytest.mark.asyncio
    async def test_stage3_oss_failed_increments_skipped_oss(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """Jina ok + 长 · OSS upload 失败 · reasons.skipped_oss_failed=1"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [{
            'url': 'https://x.com/oss-fail',
            'normalized_url': 'https://x.com/oss-fail',
            'industry_id': sample_industry,
            'industry_name': '测试',
            'prompt_id': sample_prompts[0],
            'platform': 'doubao',
        }]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl, patch(
            'services.research_monitor.round_runner.upload_markdown'
        ) as m_upload:
            m_crawl.return_value = {
                'ok': True, 'content': 'a' * 2000, 'char_count': 2000, 'title': 'T',
            }
            m_upload.return_value = {'ok': False, 'error': 'OSS auth fail'}
            await stage3_crawl_articles(round_id, urls)

        reasons = (self._select_round_progress(pg_conn, round_id).get('reasons') or {})
        assert reasons.get('skipped_oss_failed') == 1
        assert reasons.get('crawled_new') == 0

    @pytest.mark.asyncio
    async def test_stage3_reuse_existing_increments_reused(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """url_hash 已存在 · 不重爬 · reasons.reused_existing=1"""
        # Round 1: 先爬一篇进 DB
        round_id_1 = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        url = 'https://x.com/reuse'
        urls = [{
            'url': url, 'normalized_url': url,
            'industry_id': sample_industry,
            'industry_name': '测试',
            'prompt_id': sample_prompts[0],
            'platform': 'doubao',
        }]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl, patch(
            'services.research_monitor.round_runner.upload_markdown'
        ) as m_upload:
            m_crawl.return_value = {
                'ok': True, 'content': 'a' * 2000, 'char_count': 2000, 'title': 'T',
            }
            m_upload.return_value = {'ok': True, 'oss_key': 'k.md'}
            await stage3_crawl_articles(round_id_1, urls)

        # Round 2: 同 URL · 应走 reused_existing 路径
        round_id_2 = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl_2:
            # crawl_article 不该被调 · 但加 mock 防意外
            m_crawl_2.return_value = {'ok': False}
            await stage3_crawl_articles(round_id_2, urls)
            assert m_crawl_2.call_count == 0, "reused_existing 路径不应再调 Jina"

        reasons = (self._select_round_progress(pg_conn, round_id_2).get('reasons') or {})
        assert reasons.get('reused_existing') == 1
        assert reasons.get('crawled_new') == 0

    @pytest.mark.asyncio
    async def test_stage3_progress_payload_exposes_reasons_and_processed(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """终点 progress_json 必须含 reasons (6 类) + processed + total"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [
            {'url': f'https://x.com/f{i}', 'normalized_url': f'https://x.com/f{i}',
             'industry_id': sample_industry, 'industry_name': '测试',
             'prompt_id': sample_prompts[0], 'platform': 'doubao'}
            for i in range(3)
        ]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl:
            m_crawl.return_value = {'ok': False, 'error': 'timeout', 'char_count': 0}
            await stage3_crawl_articles(round_id, urls)

        progress = self._select_round_progress(pg_conn, round_id)
        assert set(['crawled', 'skipped', 'total', 'processed', 'reasons']).issubset(progress.keys())
        reasons = progress['reasons']
        # P14.2 C1: 7 类 reasons (加 crawled_dup 跨行业复用)
        expected_keys = {
            'crawled_new', 'crawled_dup', 'reused_existing', 'skipped_short',
            'skipped_jina_failed', 'skipped_oss_failed', 'failed_unknown',
        }
        assert set(reasons.keys()) == expected_keys, \
            f"reasons 必须含全 7 类 · 实际 {set(reasons.keys())}"
        assert progress['processed'] == 3
        assert progress['total'] == 3
        assert reasons['skipped_jina_failed'] == 3

    @pytest.mark.asyncio
    async def test_stage3_flush_called_for_all_skip_paths(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """老板 1-prompt 跑批反馈核心 · 4 个 skip path 都进 flush · update_heartbeat 多次调

        老 bug: 只有成功 path 才 % 5 flush · 4 skip path 都 return 不 flush
        新设计: 入口 force + 每条 URL finally 进 _flush_progress (节流 5/10s) + 终点 force

        本测试用 5 个 URL 全 jina_failed · 必须有 ≥ 2 次 update_heartbeat (入口 + 5 整除/终点)
        """
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [
            {'url': f'https://x.com/h{i}', 'normalized_url': f'https://x.com/h{i}',
             'industry_id': sample_industry, 'industry_name': '测试',
             'prompt_id': sample_prompts[0], 'platform': 'doubao'}
            for i in range(5)
        ]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl, patch(
            'services.research_monitor.round_runner.update_heartbeat'
        ) as m_hb:
            m_crawl.return_value = {'ok': False, 'error': 't', 'char_count': 0}
            await stage3_crawl_articles(round_id, urls)

        # 入口 force flush + 5 个 URL 中至少触发一次 5-整除 + 终点 force flush
        # 节流逻辑允许有的 URL flush 被 skip · 但总次数至少 2 (入口 + 终点)
        assert m_hb.call_count >= 2, \
            f"心跳必须至少 2 次(入口+终点)· 实际 {m_hb.call_count}"


# ==================== Stage 3 P14.1 C3 · reasons 镜像 summary_json 防 overwrite ====================
#
# 背景: round_20260528_232257 暴露 progress_json 被 stage 4/5/7/8 后续覆盖 ·
#   完成后历史视图只剩 {notified: 0} · stage 3 reasons 丢
# 修: stage 3 终点 jsonb merge 写入 summary_json.stage_3 · _build_summary 读回合并 ·
#   不被 update_round_complete overwrite 整段 summary

class TestStage3SummaryMirror:

    @pytest.mark.asyncio
    async def test_stage3_mirrors_reasons_to_summary_json_stage_3(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """stage 3 终点必须把 reasons + processed + total + jina_requests 镜像进 summary_json.stage_3"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [
            {'url': f'https://x.com/m{i}', 'normalized_url': f'https://x.com/m{i}',
             'industry_id': sample_industry, 'industry_name': '测试行业',
             'prompt_id': sample_prompts[0], 'platform': 'doubao'}
            for i in range(3)
        ]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl, patch(
            'services.research_monitor.round_runner.asyncio.sleep', new_callable=AsyncMock,
        ):  # P14.3 C3: retry 间隔 patch 防测试拖时长
            m_crawl.return_value = {'ok': False, 'error': 'timeout', 'char_count': 0}
            await stage3_crawl_articles(round_id, urls)

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT summary_json FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        s = (cur.fetchone() or {}).get('summary_json') or {}
        assert 'stage_3' in s, \
            "stage 3 终点必须把 reasons 镜像进 summary_json.stage_3 (防 stage 8 overwrite)"
        s3 = s['stage_3']
        assert s3['processed'] == 3
        assert s3['total_urls'] == 3
        assert s3['skipped_jina_failed'] == 3
        assert s3['crawled_new'] == 0
        assert s3['reused_existing'] == 0
        # P14.3 C3: jina_requests = 真实发出的 Jina 请求数 · 每 URL retry 1 次 → 3 × 2 = 6
        # (existing 路径不算 · 但 retry 算 · 用于 cost log 正确计费)
        assert s3['jina_requests'] == 6, \
            f"3 URL × 2 attempts (P14.3 C3 retry) = 6 jina_requests · 实际 {s3['jina_requests']}"
        # 7 类 reasons 全在 (P14.2 C1 加 crawled_dup)
        expected_keys = {
            'crawled_new', 'crawled_dup', 'reused_existing', 'skipped_short',
            'skipped_jina_failed', 'skipped_oss_failed', 'failed_unknown',
        }
        assert expected_keys.issubset(s3.keys()), \
            f"summary.stage_3 必须含全 7 类 reasons · 实际 {set(s3.keys())}"

    @pytest.mark.asyncio
    async def test_build_summary_preserves_stage_3_after_call(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """_build_summary 必须从 DB 读回 stage_3 子字段合并到新 summary
        (否则 update_round_complete overwrite 整 summary 会丢 stage 3 reasons)
        """
        from services.research_monitor.round_runner import _build_summary

        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [{
            'url': 'https://x.com/c3', 'normalized_url': 'https://x.com/c3',
            'industry_id': sample_industry, 'industry_name': '测试行业',
            'prompt_id': sample_prompts[0], 'platform': 'doubao',
        }]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl:
            m_crawl.return_value = {'ok': False, 'error': 'timeout'}
            await stage3_crawl_articles(round_id, urls)

        # 模拟 stage 7/8 调 _build_summary 构造新 summary
        summary = _build_summary(round_id)
        assert 'stage_3' in summary, \
            "_build_summary 必须把 DB 已有 summary_json.stage_3 合并进返回 summary"
        assert summary['stage_3']['skipped_jina_failed'] == 1
        assert summary['stage_3']['processed'] == 1

    @pytest.mark.asyncio
    async def test_stage3_jina_failed_records_error_sample(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """skipped_jina_failed 时必须把前 5 条 error 样本写入 summary_json.stage_3.error_samples"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [{
            'url': f'https://x.com/err{i}', 'normalized_url': f'https://x.com/err{i}',
            'industry_id': sample_industry, 'industry_name': '测试行业',
            'prompt_id': sample_prompts[0], 'platform': 'doubao',
        } for i in range(3)]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl:
            m_crawl.return_value = {
                'ok': False, 'error': 'httpx.ReadTimeout: Timed out reading response',
                'char_count': 0,
            }
            await stage3_crawl_articles(round_id, urls)

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT summary_json FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        s3 = ((cur.fetchone() or {}).get('summary_json') or {}).get('stage_3') or {}
        samples = s3.get('error_samples') or []
        assert len(samples) == 3, f"3 个 URL 全 jina_failed · 应记 3 条 error_sample · 实际 {len(samples)}"
        e0 = samples[0]
        assert e0['url'].startswith('https://x.com/err')
        assert e0['reason'] == 'skipped_jina_failed'
        assert e0['error_type'] == 'timeout', \
            f"error 含 'timeout' 字样 · error_type 必须分类为 'timeout' · 实际 {e0['error_type']}"
        assert 'timeout' in e0['error_message'].lower()

    @pytest.mark.asyncio
    async def test_stage3_error_samples_capped_at_5(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """error_samples 必须限 5 条 · 防大轮次打爆 summary_json"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [{
            'url': f'https://x.com/cap{i}', 'normalized_url': f'https://x.com/cap{i}',
            'industry_id': sample_industry, 'industry_name': '测试行业',
            'prompt_id': sample_prompts[0], 'platform': 'doubao',
        } for i in range(10)]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl:
            m_crawl.return_value = {'ok': False, 'error': 'connect refused'}
            await stage3_crawl_articles(round_id, urls)

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT summary_json FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        s3 = ((cur.fetchone() or {}).get('summary_json') or {}).get('stage_3') or {}
        samples = s3.get('error_samples') or []
        # 但 counters.skipped_jina_failed 应该 = 10 (全部都计数 · 只是 sample 限 5)
        assert s3.get('skipped_jina_failed') == 10
        assert len(samples) == 5, f"error_samples 必须 ≤ 5 · 实际 {len(samples)}"
        # error_type 分类为 connect
        assert all(s['error_type'] == 'connect' for s in samples), \
            "全部 'connect refused' · error_type 必须分类为 'connect'"


# ==================== Stage 2/3 P14.1 C2 · citation 上下文 + existing reuse 计数 ====================
#
# 背景: 老板真实跑批暴露 article_citations 三字段全 NULL (industry_id/prompt_id/raw_id)
#   · 引用明细/矩阵丢上下文 · 测试行业引用了文章但文章库看不到
#   · 复用旧文章时 total_citation_count 不增 · last_seen_at 不刷
# 本测试组锁:
#   1. Stage 2 SELECT raw 必含 id (as raw_id) + cite_position (as rank_in_response)
#   2. Stage 2 用 plan map 反查 industry_id + prompt_id (raw 表无这俩列)
#   3. Stage 3 两处 citation INSERT 必写 4 字段
#   4. existing reuse 时 total_citation_count + 1 + last_seen_at 更新
#   5. 新爬路径 total_citation_count 也 + 1 (并发 url_hash 冲突场景仍按 citation rowcount 判)

class TestStage2CitationContext:

    def test_stage2_large_slow_prefilter_refreshes_heartbeat_by_elapsed_time(self):
        """大量慢 robots 检查期间必须持续心跳，不能被 10 分钟收尸器误判。

        [2026-07-16 Stage2 并发预筛返工 · 测试随架构迁移, 语义不变]
        旧版 patch should_pre_filter_url + 假时钟驱动逐 URL 串行循环; 新架构
        网络检查在 prefilter_urls_concurrent 线程池内(真线程与假时钟不相容),
        心跳由主等待循环按 STAGE2_HEARTBEAT_INTERVAL_SECONDS 时间驱动切片刷新。
        本版改真时间 + 缩放节拍(0.1s): patch crawler 网络原语造慢 robots
        (0.15s/域 × 40 域), 断言全量通过 + 心跳最大间隔 ≤ 5×缩放节拍。
        [轮1审核措辞订正] 本测试是时间驱动语义的回归锚(其负载形状下完成驱动
        变体也可能过); 强判别(零完成期间心跳照打)由
        test_stage2_prefilter_concurrency 组H 承担, 生产 30s 节拍由其源码锁锚定。
        """
        import time as _time
        from services.research_monitor import crawler as _crawler

        n_hosts = 40
        raw_urls = [
            {
                'raw_id': i,
                'cite_url': f'https://host-{i % n_hosts}.hb2.example/article/{i}',
                'industry': '测试行业',
                'query': '测试问题',
                'engine': 'doubao',
                'cite_position': 1,
            }
            for i in range(2000)
        ]
        cursor = MagicMock()
        cursor.fetchall.return_value = raw_urls
        conn = MagicMock()
        conn.cursor.return_value = cursor

        class _Resp:
            """消费式 read(与真实 HTTPResponse 一致): 分块读改造后 read 会被循环
            调用, 无状态假件会变成 16 线程纯 Python 自旋(不释放 GIL)饿死主线程心跳。"""

            def __init__(self):
                self._buf = b"User-agent: *\nAllow: /\n"

            def read(self, n=-1):
                out, self._buf = self._buf, b''
                return out

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _SlowOpener:
            def open(self, url, timeout=None):
                _time.sleep(0.15)
                return _Resp()

        with _crawler._ROBOTS_CACHE_LOCK:
            _crawler._ROBOTS_CACHE.clear()
            _crawler._ROBOTS_INFLIGHT.clear()

        heartbeat_times = []
        with patch(
            'services.research_monitor.round_runner.get_connection',
            return_value=conn,
        ), patch(
            'services.research_monitor.round_runner.get_round_status',
            return_value={'status': 'running'},
        ), patch(
            'services.research_monitor.round_runner.update_heartbeat',
            side_effect=lambda *_args, **_kwargs: heartbeat_times.append(_time.monotonic()),
        ), patch(
            'services.research_monitor.round_runner.update_round_progress',
        ), patch(
            'services.research_monitor.round_runner.STAGE2_HEARTBEAT_INTERVAL_SECONDS',
            0.1,
        ), patch.object(
            _crawler, '_resolve_host_ips',
            side_effect=lambda h: ['93.184.216.34'],
        ), patch.object(
            _crawler, '_SSRF_SAFE_OPENER', _SlowOpener(),
        ):
            urls = stage2_extract_and_prefilter_urls('round-heartbeat', 'batch-heartbeat')

        try:
            assert len(urls) == 2000
            assert len(heartbeat_times) >= 4
            gaps = [
                later - earlier
                for earlier, later in zip(heartbeat_times, heartbeat_times[1:])
            ]
            assert max(gaps) <= 0.5, (
                f"心跳最大间隔 {max(gaps):.2f}s > 5×缩放节拍 → 时间驱动失效"
            )
        finally:
            # [轮1审核修] 跑后同样清缓存: 不向 session 泄漏 40 条 24h TTL 条目
            with _crawler._ROBOTS_CACHE_LOCK:
                _crawler._ROBOTS_CACHE.clear()
                _crawler._ROBOTS_INFLIGHT.clear()

    def test_stage2_returns_raw_id_and_cite_position(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """url_item 必含 raw_id + rank_in_response (Stage 2 SELECT 多拉的 raw 字段)

        注: geo_research_raw 不在 clean_research_tables 清单 · batch_id 用 round_id 防累积干扰
        """
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        batch_id = f'batch_{round_id}'
        cur = pg_conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_research_raw
                (industry, query, engine, cited_platform, cite_position, cite_url, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            ('测试行业', '测试问题 1', 'doubao', 'zhihu.com', 3,
             'https://zhihu.com/q/p14-c2-1', batch_id),
        )
        raw_id = cur.fetchone()['id']
        pg_conn.commit()

        # [2026-07-16 迁移] 新架构走 prefilter_urls_concurrent, 旧 patch 落空会真跑网络
        with patch(
            'services.research_monitor.round_runner.prefilter_urls_concurrent',
            side_effect=lambda urls, **kw: ({u: None for u in urls}, False),  # 不预过滤
        ):
            urls = stage2_extract_and_prefilter_urls(round_id, batch_id, plan=None)
        assert len(urls) == 1
        item = urls[0]
        assert item['raw_id'] == raw_id, "raw_id 必须从 raw.id 透传"
        assert item['rank_in_response'] == 3, "rank_in_response 必须从 raw.cite_position 透传"
        # 无 plan 时 industry_id/prompt_id 为 None (符合预期)
        assert item['industry_id'] is None
        assert item['prompt_id'] is None

    def test_stage2_resolves_industry_id_prompt_id_via_plan(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """传 plan 时 stage2 必反查出 industry_id + prompt_id"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        batch_id = f'batch_{round_id}'
        cur = pg_conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_research_raw
                (industry, query, engine, cited_platform, cite_position, cite_url, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            ('测试行业', '测试问题 1', 'doubao', 'x.com', 1,
             'https://x.com/p14-c2-plan', batch_id),
        )
        pg_conn.commit()

        plan = [{
            'industry_name': '测试行业',
            'industry_id': sample_industry,
            'prompt_text': '测试问题 1',
            'prompt_id': sample_prompts[0],
        }]
        # [2026-07-16 迁移] 同上: 直通裁定绕过网络
        with patch(
            'services.research_monitor.round_runner.prefilter_urls_concurrent',
            side_effect=lambda urls, **kw: ({u: None for u in urls}, False),
        ):
            urls = stage2_extract_and_prefilter_urls(round_id, batch_id, plan=plan)
        assert len(urls) == 1
        item = urls[0]
        assert item['industry_id'] == sample_industry
        assert item['prompt_id'] == sample_prompts[0]
        assert item['industry_name'] == '测试行业'

    def test_stage2_plan_lookup_misses_remain_none(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """raw.industry/query 不在 plan map 时 · industry_id/prompt_id 留 None 不报错"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        batch_id = f'batch_{round_id}'
        cur = pg_conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_research_raw
                (industry, query, engine, cited_platform, cite_url, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            ('未知行业', '未知问题', 'doubao', 'x.com',
             'https://x.com/p14-c2-miss', batch_id),
        )
        pg_conn.commit()
        plan = [{
            'industry_name': '测试行业', 'industry_id': sample_industry,
            'prompt_text': '测试问题 1', 'prompt_id': sample_prompts[0],
        }]
        # [2026-07-16 迁移] 同上: 直通裁定绕过网络
        with patch(
            'services.research_monitor.round_runner.prefilter_urls_concurrent',
            side_effect=lambda urls, **kw: ({u: None for u in urls}, False),
        ):
            urls = stage2_extract_and_prefilter_urls(round_id, batch_id, plan=plan)
        assert len(urls) == 1
        assert urls[0]['industry_id'] is None
        assert urls[0]['prompt_id'] is None


class TestStage3CitationContext:

    @pytest.mark.asyncio
    async def test_stage3_citation_includes_raw_id_rank_industry_prompt(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """新爬路径 citation INSERT 必含 4 字段非 NULL"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls = [{
            'url': 'https://x.com/c2-1',
            'normalized_url': 'https://x.com/c2-1',
            'industry_id': sample_industry,
            'industry_name': '测试行业',
            'prompt_id': sample_prompts[0],
            'prompt_text': '测试问题 1',
            'platform': 'doubao',
            'raw_id': 999,
            'rank_in_response': 2,
        }]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl, patch(
            'services.research_monitor.round_runner.upload_markdown'
        ) as m_upload:
            m_crawl.return_value = {
                'ok': True, 'content': 'a' * 2000, 'char_count': 2000, 'title': 'T',
            }
            m_upload.return_value = {'ok': True, 'oss_key': 'k.md'}
            await stage3_crawl_articles(round_id, urls)

        cur = pg_conn.cursor()
        cur.execute(
            """
            SELECT industry_id, prompt_id, raw_id, rank_in_response, platform
              FROM geo_research_article_citations
             WHERE round_id = %s
            """,
            (round_id,),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        c = rows[0]
        assert c['industry_id'] == sample_industry, "industry_id 必须非 NULL"
        assert c['prompt_id'] == sample_prompts[0], "prompt_id 必须非 NULL"
        assert c['raw_id'] == 999, "raw_id 必须从 url_item 透传"
        assert c['rank_in_response'] == 2, "rank_in_response 必须从 url_item 透传"

    @pytest.mark.asyncio
    async def test_stage3_existing_reuse_increments_total_citation_count(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """复用已有文章时 article.total_citation_count + 1 + last_seen_at 更新"""
        # Round 1: 爬一篇
        round_id_1 = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls_1 = [{
            'url': 'https://x.com/c2-reuse',
            'normalized_url': 'https://x.com/c2-reuse',
            'industry_id': sample_industry,
            'industry_name': '测试行业',
            'prompt_id': sample_prompts[0],
            'platform': 'doubao',
            'raw_id': 1001,
            'rank_in_response': 1,
        }]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl, patch(
            'services.research_monitor.round_runner.upload_markdown'
        ) as m_upload:
            m_crawl.return_value = {
                'ok': True, 'content': 'a' * 2000, 'char_count': 2000, 'title': 'T',
            }
            m_upload.return_value = {'ok': True, 'oss_key': 'k.md'}
            await stage3_crawl_articles(round_id_1, urls_1)

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT id, total_citation_count, last_seen_at FROM geo_research_articles "
            "WHERE url_hash = (SELECT url_hash FROM geo_research_articles ORDER BY id DESC LIMIT 1)"
        )
        a_before = cur.fetchone()
        article_id = a_before['id']
        assert a_before['total_citation_count'] == 1, \
            f"首次爬 + 1 citation 后 total_citation_count 应=1 · 实际 {a_before['total_citation_count']}"

        # Round 2: 同 URL 不同 prompt_id (避免 UNIQUE 冲突 article_id+round+prompt+platform)
        round_id_2 = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        urls_2 = [{
            **urls_1[0],
            'prompt_id': sample_prompts[1],  # 换 prompt 避免 UNIQUE 冲突
            'raw_id': 1002,
        }]
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl_2:
            m_crawl_2.return_value = {'ok': False}  # 不该被调
            await stage3_crawl_articles(round_id_2, urls_2)

        cur.execute(
            "SELECT total_citation_count, last_seen_at FROM geo_research_articles WHERE id = %s",
            (article_id,),
        )
        a_after = cur.fetchone()
        assert a_after['total_citation_count'] == 2, \
            f"reused_existing 路径必须 total_citation_count + 1 · 实际 {a_after['total_citation_count']}"
        assert a_after['last_seen_at'] >= a_before['last_seen_at'], \
            "reused_existing 时 last_seen_at 必须更新"

    @pytest.mark.asyncio
    async def test_stage3_cross_industry_dup_creates_new_article(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """P14.2 C1 · 跨行业首次引用同 URL · 必须新插 article 行 (is_duplicate=true) · 不重爬 Jina"""
        url = 'https://x.com/cross-dup'
        url_item_A = {
            'url': url, 'normalized_url': url,
            'industry_name': '行业A', 'industry_id': sample_industry,
            'prompt_id': sample_prompts[0], 'platform': 'doubao',
            'raw_id': 5001, 'rank_in_response': 1,
        }
        url_item_B = {**url_item_A, 'industry_name': '行业B', 'raw_id': 5002}

        round_id_A = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        # Round A: 真爬 + 写 article(primary='行业A')
        with patch('services.research_monitor.round_runner.crawl_article',
                   new_callable=AsyncMock) as m_crawl, \
             patch('services.research_monitor.round_runner.upload_markdown') as m_upload:
            m_crawl.return_value = {
                'ok': True, 'content': 'a' * 2000, 'char_count': 2000, 'title': 'TITLE-A',
            }
            m_upload.return_value = {'ok': True, 'oss_key': 'raw/y/x/abc.md'}
            await stage3_crawl_articles(round_id_A, [url_item_A])

        cur = pg_conn.cursor()
        cur.execute("SELECT id, primary_industry, is_duplicate FROM geo_research_articles "
                    "WHERE primary_industry='行业A'")
        a_rows = cur.fetchall()
        assert len(a_rows) == 1
        primary_id = a_rows[0]['id']
        assert a_rows[0]['is_duplicate'] is False

        # Round B: 同 URL · 不同行业 · crawl_article 不该被调
        round_id_B = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        with patch('services.research_monitor.round_runner.crawl_article',
                   new_callable=AsyncMock) as m_crawl_B:
            m_crawl_B.return_value = {'ok': False}  # 不该被调
            await stage3_crawl_articles(round_id_B, [url_item_B])
            assert m_crawl_B.call_count == 0, "跨行业 dup 不应再调 Jina"

        # 现在应该有 2 条 article (行业A primary + 行业B dup)
        cur.execute("""
            SELECT id, primary_industry, is_duplicate, primary_article_id
              FROM geo_research_articles WHERE url_hash IS NOT NULL
             ORDER BY id ASC
        """)
        all_rows = cur.fetchall()
        assert len(all_rows) == 2
        # 第二行 dup 必须 is_duplicate=true + primary_article_id 指向首篇
        dup = next(r for r in all_rows if r['primary_industry'] == '行业B')
        assert dup['is_duplicate'] is True
        assert dup['primary_article_id'] == primary_id

        # progress_json.reasons.crawled_dup = 1
        cur.execute("SELECT progress_json FROM geo_research_round WHERE round_id=%s",
                    (round_id_B,))
        progress = (cur.fetchone() or {}).get('progress_json') or {}
        assert (progress.get('reasons') or {}).get('crawled_dup') == 1
        assert (progress.get('reasons') or {}).get('crawled_new') == 0

    @pytest.mark.asyncio
    async def test_stage3_cross_industry_dup_copies_content_fields(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """P14.2 C1 老板规格 A · dup 复制 14 内容字段 · 不复制 5 个权限/审核字段"""
        url = 'https://x.com/copy-fields'
        url_item = {
            'url': url, 'normalized_url': url,
            'industry_name': '行业A',
            'prompt_id': sample_prompts[0], 'platform': 'doubao',
        }

        round_id_A = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        with patch('services.research_monitor.round_runner.crawl_article',
                   new_callable=AsyncMock) as m_crawl, \
             patch('services.research_monitor.round_runner.upload_markdown') as m_upload:
            m_crawl.return_value = {
                'ok': True, 'content': 'a' * 3000, 'char_count': 3000,
                'title': 'TITLE-COPY-TEST',
            }
            m_upload.return_value = {'ok': True, 'oss_key': 'raw/y/copy/abc.md'}
            await stage3_crawl_articles(round_id_A, [url_item])

        # 手动给原 article 加 locked_by 和 reviewed_by · 验证 dup 不复制
        cur = pg_conn.cursor()
        cur.execute("""
            UPDATE geo_research_articles
               SET locked_by = 'tester_locked',
                   reviewed_by = 'tester_reviewed',
                   inline_cleaned_content = 'cleaned-text-for-A'
             WHERE primary_industry = '行业A'
            RETURNING id, url, url_hash, domain, title, oss_key_raw, raw_char_count,
                      content_hash, domain_tier, content_type, clean_status, review_status,
                      locked_by, reviewed_by, inline_cleaned_content
        """)
        primary = cur.fetchone()
        pg_conn.commit()

        # Round B: 跨行业 dup
        url_item_B = {**url_item, 'industry_name': '行业B'}
        round_id_B = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        with patch('services.research_monitor.round_runner.crawl_article',
                   new_callable=AsyncMock):
            await stage3_crawl_articles(round_id_B, [url_item_B])

        cur.execute("""
            SELECT id, url, url_hash, domain, title, oss_key_raw, raw_char_count,
                   content_hash, domain_tier, content_type, clean_status, review_status,
                   inline_cleaned_content,
                   primary_industry, first_seen_round_id, is_duplicate, primary_article_id,
                   total_citation_count,
                   locked_by, locked_at, reviewed_by, reviewed_at, reference_article_id
              FROM geo_research_articles WHERE primary_industry = '行业B'
        """)
        dup = cur.fetchone()
        assert dup is not None, "跨行业 dup article 必须存在"

        # 14 复制字段必须等
        for f in ['url', 'url_hash', 'domain', 'title', 'oss_key_raw',
                  'raw_char_count', 'content_hash', 'domain_tier', 'content_type',
                  'clean_status', 'review_status', 'inline_cleaned_content']:
            assert dup[f] == primary[f], f"复制字段 {f!r} 必须等首篇 · A={primary[f]!r} B={dup[f]!r}"

        # 7 重写字段必须按规格
        assert dup['primary_industry'] == '行业B'
        assert dup['first_seen_round_id'] == round_id_B
        assert dup['is_duplicate'] is True
        assert dup['primary_article_id'] == primary['id']
        # total_citation_count=0 (INSERT 时) → +1 (citation INSERT 后 UPDATE)
        assert dup['total_citation_count'] == 1

        # 5 不复制字段必须 NULL (污染防护)
        assert dup['locked_by'] is None, "locked_by 不复制 · 防权限污染"
        assert dup['reviewed_by'] is None, "reviewed_by 不复制 · 防审核状态污染"
        assert dup['locked_at'] is None
        assert dup['reviewed_at'] is None
        assert dup['reference_article_id'] is None

    @pytest.mark.asyncio
    async def test_stage3_same_industry_after_dup_still_reuses(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """跨行业 dup 之后 · 同行业 (行业B) 再来一次 · 必须 reused_existing 不再新建"""
        url = 'https://x.com/reuse-after-dup'
        url_item_A = {'url': url, 'normalized_url': url, 'industry_name': '行业A',
                      'prompt_id': sample_prompts[0], 'platform': 'doubao'}
        # Round A: 首爬
        rA = create_round_with_snapshot(triggered_by='manual', industries=[], prompts_by_industry={})
        with patch('services.research_monitor.round_runner.crawl_article',
                   new_callable=AsyncMock) as m_crawl, \
             patch('services.research_monitor.round_runner.upload_markdown') as m_upload:
            m_crawl.return_value = {'ok': True, 'content': 'x' * 2000, 'char_count': 2000, 'title': 'T'}
            m_upload.return_value = {'ok': True, 'oss_key': 'k.md'}
            await stage3_crawl_articles(rA, [url_item_A])

        # Round B: 跨行业 dup
        rB = create_round_with_snapshot(triggered_by='manual', industries=[], prompts_by_industry={})
        url_item_B = {**url_item_A, 'industry_name': '行业B', 'prompt_id': sample_prompts[1]}
        await stage3_crawl_articles(rB, [url_item_B])

        # Round B 再来一次同 URL 同行业B · 应该 reused_existing 不再 dup
        rB2 = create_round_with_snapshot(triggered_by='manual', industries=[], prompts_by_industry={})
        url_item_B2 = {**url_item_B, 'prompt_id': sample_prompts[2]}  # 换 prompt 防 citation UNIQUE
        await stage3_crawl_articles(rB2, [url_item_B2])

        cur = pg_conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM geo_research_articles "
                    "WHERE primary_industry='行业B'")
        assert cur.fetchone()['cnt'] == 1, "行业B 只该有 1 个 article · 不该重复"

        # Round B2 reasons
        cur.execute("SELECT progress_json FROM geo_research_round WHERE round_id=%s", (rB2,))
        progress = (cur.fetchone() or {}).get('progress_json') or {}
        assert (progress.get('reasons') or {}).get('reused_existing') == 1
        assert (progress.get('reasons') or {}).get('crawled_dup') == 0
        assert (progress.get('reasons') or {}).get('crawled_new') == 0

    @pytest.mark.asyncio
    async def test_stage3_jina_retry_first_fail_second_success_crawls_article(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """P14.3 C3 · 第 1 次 Jina fail · 第 2 次 success → crawled_new=1 · jina_request_count=2"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        url_item = {
            'url': 'https://x.com/retry-success', 'normalized_url': 'https://x.com/retry-success',
            'industry_id': sample_industry, 'industry_name': '行业A',
            'prompt_id': sample_prompts[0], 'platform': 'doubao',
        }
        with patch('services.research_monitor.round_runner.crawl_article',
                   new_callable=AsyncMock) as m_crawl, \
             patch('services.research_monitor.round_runner.upload_markdown') as m_upload, \
             patch('services.research_monitor.round_runner.asyncio.sleep',
                   new_callable=AsyncMock) as m_sleep:
            # side_effect: 第 1 次 fail · 第 2 次 success
            m_crawl.side_effect = [
                {'ok': False, 'error': 'ConnectError: refused', 'char_count': 0},
                {'ok': True, 'content': 'a' * 2000, 'char_count': 2000, 'title': 'T'},
            ]
            m_upload.return_value = {'ok': True, 'oss_key': 'k.md'}
            await stage3_crawl_articles(round_id, [url_item])

        cur = pg_conn.cursor()
        # 文章被成功爬到 (crawled_new=1)
        cur.execute("SELECT COUNT(*) AS c FROM geo_research_articles WHERE first_seen_round_id=%s",
                    (round_id,))
        assert cur.fetchone()['c'] == 1, "retry 成功后必须新增 article"

        # reasons.crawled_new = 1 · skipped_jina_failed = 0
        cur.execute("SELECT progress_json FROM geo_research_round WHERE round_id=%s", (round_id,))
        reasons = ((cur.fetchone() or {}).get('progress_json') or {}).get('reasons') or {}
        assert reasons.get('crawled_new') == 1
        assert reasons.get('skipped_jina_failed') == 0
        # crawl_article 被调 2 次 (1 fail + 1 success)
        assert m_crawl.call_count == 2, f"必须 retry 1 次 · crawl_article 共 2 次调 · 实际 {m_crawl.call_count}"
        # sleep 在 2 次 attempt 之间被调 1 次 (retry 间隔)
        assert m_sleep.call_count >= 1, "retry 间隔必须 await asyncio.sleep 至少 1 次"

    @pytest.mark.asyncio
    async def test_stage3_jina_retry_both_attempts_fail_keeps_attempt_summary(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """P14.3 C3 · 两次 attempt 都 fail · error_samples 必须含 'attempt 1' + 'attempt 2' 摘要"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        url_item = {
            'url': 'https://x.com/retry-fail', 'normalized_url': 'https://x.com/retry-fail',
            'industry_id': sample_industry, 'industry_name': '行业A',
            'prompt_id': sample_prompts[0], 'platform': 'doubao',
        }
        with patch('services.research_monitor.round_runner.crawl_article',
                   new_callable=AsyncMock) as m_crawl, \
             patch('services.research_monitor.round_runner.asyncio.sleep',
                   new_callable=AsyncMock):
            # 两次都 fail · 第 2 次错误信息不一样 (验证保留每 attempt 摘要)
            m_crawl.side_effect = [
                {'ok': False, 'error': 'ConnectError: refused', 'char_count': 0},
                {'ok': False, 'error': 'ReadTimeout: 60s', 'char_count': 0},
            ]
            await stage3_crawl_articles(round_id, [url_item])

        cur = pg_conn.cursor()
        cur.execute("SELECT summary_json FROM geo_research_round WHERE round_id=%s", (round_id,))
        s3 = ((cur.fetchone() or {}).get('summary_json') or {}).get('stage_3') or {}
        assert s3.get('skipped_jina_failed') == 1
        samples = s3.get('error_samples') or []
        assert len(samples) == 1
        msg = samples[0].get('error_message') or ''
        # 必须保留两次 attempt 的摘要
        assert 'attempt 1' in msg, f"error_message 必须含 'attempt 1' · 实际 {msg!r}"
        assert 'attempt 2' in msg, f"error_message 必须含 'attempt 2' · 实际 {msg!r}"
        assert 'ConnectError' in msg, "attempt 1 错误必须保留"
        assert 'ReadTimeout' in msg, "attempt 2 错误必须保留"
        # crawl_article 被调 2 次
        assert m_crawl.call_count == 2

    def test_jina_retry_clamps_misconfigured_env_to_safe_minimum(self, monkeypatch):
        """P14.3 C3.1 防呆 (老板复核): env JINA_RETRY_MAX_ATTEMPTS=0 / DELAY_SECONDS=负数 时
        模块加载必须 clamp 到安全下限 · 否则跑批一次 Jina 都不请求 · 所有 URL 假死成 jina_failed
        """
        import importlib
        from services.research_monitor import round_runner

        # case 1: MAX_ATTEMPTS=0 误配 · 必须 clamp 到 1 (至少 1 次 attempt)
        monkeypatch.setenv('JINA_RETRY_MAX_ATTEMPTS', '0')
        monkeypatch.setenv('JINA_RETRY_DELAY_SECONDS', '-5.0')
        importlib.reload(round_runner)
        assert round_runner.JINA_RETRY_MAX_ATTEMPTS == 1, \
            f"env=0 必须 clamp 到 1 · 实际 {round_runner.JINA_RETRY_MAX_ATTEMPTS}"
        assert round_runner.JINA_RETRY_DELAY_SECONDS == 0.0, \
            f"env=-5.0 必须 clamp 到 0.0 · 实际 {round_runner.JINA_RETRY_DELAY_SECONDS}"

        # case 2: 负 MAX_ATTEMPTS 也必须 clamp
        monkeypatch.setenv('JINA_RETRY_MAX_ATTEMPTS', '-3')
        importlib.reload(round_runner)
        assert round_runner.JINA_RETRY_MAX_ATTEMPTS == 1, \
            f"env=-3 必须 clamp 到 1 · 实际 {round_runner.JINA_RETRY_MAX_ATTEMPTS}"

        # case 3: 正常值不受影响
        monkeypatch.setenv('JINA_RETRY_MAX_ATTEMPTS', '3')
        monkeypatch.setenv('JINA_RETRY_DELAY_SECONDS', '1.5')
        importlib.reload(round_runner)
        assert round_runner.JINA_RETRY_MAX_ATTEMPTS == 3
        assert round_runner.JINA_RETRY_DELAY_SECONDS == 1.5

        # 恢复默认 (monkeypatch 会自动撤 env · 再 reload 拿回默认)
        monkeypatch.delenv('JINA_RETRY_MAX_ATTEMPTS', raising=False)
        monkeypatch.delenv('JINA_RETRY_DELAY_SECONDS', raising=False)
        importlib.reload(round_runner)
        assert round_runner.JINA_RETRY_MAX_ATTEMPTS == 2, \
            "默认 env unset 时回到 2 · 不被本测试副作用污染"

    @pytest.mark.asyncio
    async def test_stage3_existing_reuse_conflict_does_not_double_increment(
        self, pg_conn, clean_research_tables, sample_industry, sample_prompts,
    ):
        """同 (article_id, round_id, prompt_id, platform) ON CONFLICT 跳过时 · count 不重复 +1"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        url_item = {
            'url': 'https://x.com/c2-conflict',
            'normalized_url': 'https://x.com/c2-conflict',
            'industry_id': sample_industry,
            'industry_name': '测试行业',
            'prompt_id': sample_prompts[0],
            'platform': 'doubao',
            'raw_id': 2001,
            'rank_in_response': 1,
        }
        # 第一次爬
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl, patch(
            'services.research_monitor.round_runner.upload_markdown'
        ) as m_upload:
            m_crawl.return_value = {
                'ok': True, 'content': 'a' * 2000, 'char_count': 2000, 'title': 'T',
            }
            m_upload.return_value = {'ok': True, 'oss_key': 'k.md'}
            await stage3_crawl_articles(round_id, [url_item])

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT id, total_citation_count FROM geo_research_articles "
            "WHERE url_hash = (SELECT url_hash FROM geo_research_articles ORDER BY id DESC LIMIT 1)"
        )
        a1 = cur.fetchone()
        assert a1['total_citation_count'] == 1
        article_id = a1['id']

        # 同 round 同 url_item 再爬一次 · 现在走 reused_existing 路径但
        # citation INSERT ON CONFLICT (article_id, round_id, prompt_id, platform) 命中 · 跳过
        with patch(
            'services.research_monitor.round_runner.crawl_article',
            new_callable=AsyncMock,
        ) as m_crawl_2:
            m_crawl_2.return_value = {'ok': False}
            await stage3_crawl_articles(round_id, [url_item])

        cur.execute(
            "SELECT total_citation_count FROM geo_research_articles WHERE id = %s",
            (article_id,),
        )
        a2 = cur.fetchone()
        assert a2['total_citation_count'] == 1, \
            ("ON CONFLICT DO NOTHING 时 total_citation_count 不该再 +1 · "
             f"实际 {a2['total_citation_count']}")


# ==================== Stage 4-6 ====================

from services.research_monitor.round_runner import (
    stage4_clean_articles,
    stage5_filter_by_char_count,
    # Phase 9: stage6_score_articles 已物理删 (LLM 评分功能下线)
)


class TestStage4Clean:
    """Phase 9 (2026-05-25) · stage 4 改用规则清洗(clean_articles_rule)替代 LLM(cleaner.py)"""

    @pytest.mark.asyncio
    async def test_stage4_cleans_pending_articles(self, pg_conn):
        """clean_status='pending' 的文章被规则清洗 · 不再走 LLM"""
        from services.research_monitor.round_state import create_round_with_snapshot
        round_id = create_round_with_snapshot(triggered_by='manual', industries=[], prompts_by_industry={})

        cur = pg_conn.cursor()
        import hashlib
        url_hash = hashlib.sha1('https://test.com/p/4'.encode()).hexdigest()
        cur.execute("""
            INSERT INTO geo_research_articles
            (url, url_hash, domain, title, primary_industry,
             oss_key_raw, raw_char_count, content_hash, domain_tier,
             clean_status, review_status, first_seen_round_id, fetched_at)
            VALUES ('https://test.com/p/4', %s, 'test.com', 'test', '测试',
                    'raw/2026-05/test.com/abc.md', 5000, %s, 'gray',
                    'pending', 'crawled', %s, NOW())
            RETURNING id
        """, (url_hash, 'h' * 64, round_id))
        article_id = cur.fetchone()['id']
        pg_conn.commit()

        # 给 download_markdown 返一段长 markdown(规则清洗就用它)
        # 规则清洗是纯函数, 不 mock 它(直接跑真函数验证)
        long_raw = "# 主标题\n\n" + "这是有意义的正文段落,讲一些技术内容。" * 30
        with patch('services.research_monitor.round_runner.download_markdown') as m_dl, \
             patch('services.research_monitor.round_runner.upload_markdown') as m_up:
            m_dl.return_value = long_raw
            m_up.return_value = {'ok': True, 'oss_key': 'cleaned/2026-05/test.com/abc.md', 'error': None}

            await stage4_clean_articles(round_id)

        cur.execute(
            "SELECT clean_status, oss_key_cleaned, cleaned_char_count, clean_model "
            "FROM geo_research_articles WHERE id = %s",
            (article_id,),
        )
        row = cur.fetchone()
        assert row['clean_status'] == 'cleaned'
        assert row['oss_key_cleaned'] is not None
        assert row['cleaned_char_count'] > 0
        assert row['clean_model'] == 'rule_v1'  # Phase 9 标识

    @pytest.mark.asyncio
    async def test_stage4_oss_upload_failure_marks_failed(self, pg_conn):
        """Phase 9 · OSS 上传失败 → status='failed' (不再骗人写 cleaned)"""
        from services.research_monitor.round_state import create_round_with_snapshot
        round_id = create_round_with_snapshot(triggered_by='manual', industries=[], prompts_by_industry={})

        cur = pg_conn.cursor()
        import hashlib
        url_hash = hashlib.sha1('https://test.com/p/4f'.encode()).hexdigest()
        cur.execute("""
            INSERT INTO geo_research_articles
            (url, url_hash, domain, title, primary_industry,
             oss_key_raw, raw_char_count, content_hash, domain_tier,
             clean_status, review_status, first_seen_round_id, fetched_at)
            VALUES ('https://test.com/p/4f', %s, 'test.com', 't', 'i',
                    'raw/x.md', 5000, %s, 'gray',
                    'pending', 'crawled', %s, NOW())
            RETURNING id
        """, (url_hash, 'h' * 64, round_id))
        article_id = cur.fetchone()['id']
        pg_conn.commit()

        with patch('services.research_monitor.round_runner.download_markdown') as m_dl, \
             patch('services.research_monitor.round_runner.upload_markdown') as m_up:
            m_dl.return_value = "# 长内容" + "正文段落 " * 100
            m_up.return_value = {'ok': False, 'error': 'OSS 503'}  # 模拟 OSS 失败

            await stage4_clean_articles(round_id)

        cur.execute(
            "SELECT clean_status, clean_attempts, oss_key_cleaned FROM geo_research_articles WHERE id = %s",
            (article_id,),
        )
        row = cur.fetchone()
        assert row['clean_status'] == 'failed'  # Phase 9: OSS 失败标 failed 不骗人
        assert row['clean_attempts'] >= 1
        assert row['oss_key_cleaned'] is None  # Phase 9: 不写 fallback raw key


class TestStage45IntentClassify:
    """P15 · stage 4.5 用 DeepSeek 给清洗后文章打 8 类写作意图标签。"""

    @pytest.mark.asyncio
    async def test_stage45_writes_intent_fields_for_cleaned_article(self, pg_conn):
        """cleaned 文章被分类后写入 intent_type / confidence / reason / model。"""
        from services.research_monitor.round_state import create_round_with_snapshot
        import hashlib

        round_id = create_round_with_snapshot(triggered_by='manual', industries=[], prompts_by_industry={})
        url = 'https://test.com/p/intent-ranking'
        cur = pg_conn.cursor()
        cur.execute("""
            INSERT INTO geo_research_articles
            (url, url_hash, domain, title, primary_industry,
             oss_key_cleaned, inline_cleaned_content,
             cleaned_char_count, raw_char_count, content_hash, domain_tier,
             clean_status, review_status, first_seen_round_id, fetched_at)
            VALUES (%s, %s, 'test.com', '2026 年深圳装修公司排名 TOP10', '测试',
                    'cleaned/intent-ranking.md', NULL,
                    1800, 1900, %s, 'gray',
                    'cleaned', 'crawled', %s, NOW())
            RETURNING id
        """, (
            url,
            hashlib.sha1(url.encode()).hexdigest(),
            hashlib.sha256(url.encode()).hexdigest(),
            round_id,
        ))
        article_id = cur.fetchone()['id']
        pg_conn.commit()

        with patch('services.research_monitor.round_runner.download_markdown') as m_dl, \
             patch('services.research_monitor.round_runner.classify_article_intent', new_callable=AsyncMock) as m_cls:
            m_dl.return_value = '# 深圳装修公司排名\n\n' + '本文整理十家服务商榜单。' * 80
            m_cls.return_value = IntentClassification(
                intent_type='ranking',
                confidence=0.93,
                reason='标题和正文主体是服务商榜单推荐',
                model='deepseek-v4-flash',
            )

            await stage45_classify_article_intents(round_id)

        cur.execute(
            """
            SELECT intent_type, intent_confidence, intent_reason, intent_model, intent_classified_at
              FROM geo_research_articles
             WHERE id = %s
            """,
            (article_id,),
        )
        row = cur.fetchone()
        assert row['intent_type'] == 'ranking'
        assert float(row['intent_confidence']) == pytest.approx(0.93)
        assert row['intent_reason'] == '标题和正文主体是服务商榜单推荐'
        assert row['intent_model'] == 'deepseek-v4-flash'
        assert row['intent_classified_at'] is not None
        assert m_cls.await_count == 1

    @pytest.mark.asyncio
    async def test_stage45_soft_fails_when_classifier_errors(self, pg_conn):
        """分类服务失败不阻塞流水线, 文章 intent 字段保持 NULL。"""
        from services.research_monitor.round_state import create_round_with_snapshot
        import hashlib

        round_id = create_round_with_snapshot(triggered_by='manual', industries=[], prompts_by_industry={})
        url = 'https://test.com/p/intent-fail'
        cur = pg_conn.cursor()
        cur.execute("""
            INSERT INTO geo_research_articles
            (url, url_hash, domain, title, primary_industry,
             oss_key_cleaned, inline_cleaned_content,
             cleaned_char_count, raw_char_count, content_hash, domain_tier,
             clean_status, review_status, first_seen_round_id, fetched_at)
            VALUES (%s, %s, 'test.com', '分类失败也要继续入库', '测试',
                    'cleaned/intent-fail.md', NULL,
                    1500, 1600, %s, 'gray',
                    'cleaned', 'crawled', %s, NOW())
            RETURNING id
        """, (
            url,
            hashlib.sha1(url.encode()).hexdigest(),
            hashlib.sha256(url.encode()).hexdigest(),
            round_id,
        ))
        article_id = cur.fetchone()['id']
        pg_conn.commit()

        with patch('services.research_monitor.round_runner.download_markdown') as m_dl, \
             patch('services.research_monitor.round_runner.classify_article_intent', new_callable=AsyncMock) as m_cls:
            m_dl.return_value = '# 长文章\n\n' + '正文内容。' * 200
            m_cls.side_effect = RuntimeError('deepseek unavailable')

            await stage45_classify_article_intents(round_id)

        cur.execute(
            "SELECT intent_type, intent_confidence, intent_reason, intent_model FROM geo_research_articles WHERE id = %s",
            (article_id,),
        )
        row = cur.fetchone()
        assert row['intent_type'] is None
        assert row['intent_confidence'] is None
        assert row['intent_reason'] is None
        assert row['intent_model'] is None

        cur.execute(
            "SELECT current_stage, progress_json FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        round_row = cur.fetchone()
        assert round_row['current_stage'] == 'stage_4_5_intent_done'
        assert round_row['progress_json']['failed'] == 1
        assert round_row['progress_json']['total'] == 1


class TestStage5Filter:

    def test_stage5_long_to_in_library(self, pg_conn):
        """Phase 9 (老板拍板 100 字门槛 · P09 d549a0fe):
        cleaned_char_count >= min_chars → in_library (替代旧 pending_review)
        cleaned_char_count <  min_chars → auto_skipped

        默认 min_chars=100 (db seed) · 但代码默认 ARTICLE_MIN_CHARS_FOR_REVIEW=3000
        本测试不依赖 DB seed · 测试范围用 [5000, 4000, 50, 10] 跨 100/3000 两个阈值
        都正确分到 in_library / auto_skipped (5000/4000 ≥ 任何门槛 / 50,10 < 任何门槛)
        """
        from services.research_monitor.round_state import create_round_with_snapshot
        round_id = create_round_with_snapshot(triggered_by='manual', industries=[], prompts_by_industry={})

        cur = pg_conn.cursor()
        import hashlib
        for i, char_count in enumerate([5000, 4000, 50, 10]):
            url = f'https://test.com/p/5{i}'
            cur.execute("""
                INSERT INTO geo_research_articles
                (url, url_hash, domain, title, primary_industry,
                 cleaned_char_count, content_hash, domain_tier, clean_status, review_status, first_seen_round_id)
                VALUES (%s, %s, 'test.com', 't', 'i', %s, %s, 'gray', 'cleaned', 'crawled', %s)
            """, (
                url,
                hashlib.sha1(url.encode()).hexdigest(),
                char_count,
                hashlib.sha256(f'{i}'.encode()).hexdigest(),
                round_id,
            ))
        pg_conn.commit()

        stage5_filter_by_char_count(round_id)

        cur.execute(
            "SELECT cleaned_char_count, review_status FROM geo_research_articles "
            "WHERE first_seen_round_id = %s ORDER BY cleaned_char_count DESC",
            (round_id,),
        )
        rows = cur.fetchall()
        # P12-fix-v4: 锁新契约 in_library · 不能再期望 pending_review
        assert rows[0]['review_status'] == 'in_library'   # 5000 ≥ 100/3000 都过
        assert rows[1]['review_status'] == 'in_library'   # 4000 ≥ 100/3000 都过
        assert rows[2]['review_status'] == 'auto_skipped'  # 50 < 100/3000 都不过
        assert rows[3]['review_status'] == 'auto_skipped'  # 10 同上


# Phase 9 (2026-05-25) · TestStage6Score 整段删除
#   原因: scorer.py 物理删 · stage6_score_articles 不再存在
#   文章库排序改用 total_citation_count (citation 数), 不用 LLM 评分


def test_stage45_intent_summary_mirrors_to_summary_json():
    """P15 · 文章意图分类完成后必须镜像 summary_json.stage_4_5, 防后续 stage 覆盖 progress_json。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "services" / "research_monitor" / "round_runner.py").read_text(encoding="utf-8")
    assert "'stage_4_5'" in src, "stage 4.5 摘要必须写入 summary_json.stage_4_5"
    assert "intent 分类摘要" in src or "stage 4.5 摘要" in src, "代码注释必须说明这是分类摘要持久化"
    assert "classified_count" in src and "failed_count" in src and "DEFAULT_INTENT_MODEL" in src, \
        "summary_json.stage_4_5 必须包含 classified/failed/total/model"
