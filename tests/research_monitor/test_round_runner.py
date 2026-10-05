"""
A.5.3.c Stage 7 / Stage 8 / run_round 主调度纯 mock 测试。

不连真 PG, 全靠 unittest.mock.patch 拦截 IO。
真 PG 集成测试见 test_round_runner_stages.py。
"""
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from services.research_monitor import round_runner


# ==================== Stage 1 · C-1 cited_platform 修复 ====================

class TestStage1CitedPlatform:
    """C-1 修复:Stage 1 写 geo_research_raw 时 cited_platform 必须是 domain
    (zhihu.com / 36kr.com),不是完整 URL。
    """

    def test_stage1_cited_platform_is_domain_not_full_url(self):
        captured_inserts: list = []

        # mock connection / cursor:抓 INSERT geo_research_raw 的参数
        class FakeCursor:
            def __init__(self):
                self._fetch_data = None

            def execute(self, sql, params=()):
                if 'INSERT INTO geo_research_raw' in sql:
                    captured_inserts.append(params)
                elif 'INSERT INTO geo_research_round_call' in sql:
                    self._fetch_data = {'id': 1}

            def fetchone(self):
                return self._fetch_data

            def fetchall(self):
                return []

            def close(self):
                pass

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

            def close(self):
                pass

        async def fake_retry(fetcher, prompt_id, prompt, max_retries=3):
            return {
                'platform': 'doubao',
                'prompt_id': prompt_id,
                'prompt': prompt,
                'answer': 'a',
                'citations': [
                    {'url': 'https://www.zhihu.com/question/12345', 'title': 'T1', 'rank': 1},
                    {'url': 'https://36kr.com/p/abc', 'title': 'T2', 'rank': 2},
                    {'url': '', 'title': 'T3', 'rank': 3},  # url 为空 → domain 应为空
                    {'url': 'not a url', 'title': 'T4', 'rank': 4},  # 解析失败 → domain 空
                ],
                'ok': True,
                'error': None,
                'raw': {},
            }

        plan = [{
            'industry_id': 1,
            'industry_name': '测试行业',
            'prompt_id': 100,
            'prompt_text': '测试 prompt',
        }]

        with patch.object(round_runner, 'get_connection', return_value=FakeConn()), \
             patch.object(round_runner, 'query_with_retry', side_effect=fake_retry), \
             patch.object(round_runner, '_log_cost'), \
             patch.object(round_runner, 'update_heartbeat'), \
             patch.object(round_runner, 'update_round_progress'):
            asyncio.run(round_runner.stage1_ai_fetch_all('round_c1', plan))

        # 4 平台 × 4 citations = 16 行
        assert len(captured_inserts) == 16, (
            f"期望 16 行 INSERT geo_research_raw,实际 {len(captured_inserts)}"
        )

        # 列顺序: industry, query, engine, cited_platform, cite_position, cite_url, ...
        # 即 params[3] = cited_platform, params[5] = cite_url
        for params in captured_inserts:
            cited_platform = params[3]
            cite_url = params[5]
            # cited_platform 严禁等于完整 url(原 bug 行为)
            if cite_url:
                assert cited_platform != cite_url, (
                    f"C-1 回归:cited_platform 不能填完整 url · cited_platform={cited_platform!r} cite_url={cite_url!r}"
                )

        # 抽样验证具体 4 条 citation 的 domain
        # 每平台都跑 4 条 → 取第 1 平台的 4 条
        first_platform_inserts = captured_inserts[:4]
        cited_platforms = [p[3] for p in first_platform_inserts]
        # zhihu.com → www.zhihu.com / 36kr.com → 36kr.com / 空 url → '' / 'not a url' → ''
        assert 'www.zhihu.com' in cited_platforms, f"期望 www.zhihu.com, 实际 {cited_platforms}"
        assert '36kr.com' in cited_platforms, f"期望 36kr.com, 实际 {cited_platforms}"
        # 空 url + 非法 url 应解析为空字符串
        empty_count = sum(1 for cp in cited_platforms if cp == '')
        assert empty_count == 2, f"期望 2 个空 cited_platform(空 url + 非法 url),实际 {empty_count}"

    def test_stage1_geo_raw_insert_shape_matches_params(self):
        """Stage1 真执行 raw INSERT 时,列数/VALUES/params 必须完全对齐。"""
        checked_inserts: list[tuple[int, int, int, int]] = []

        class FakeCursor:
            def __init__(self):
                self._fetch_data = None

            def execute(self, sql, params=()):
                if 'INSERT INTO geo_research_raw' in sql:
                    columns_part = sql.split('(', 1)[1].split(')', 1)[0]
                    value_part = sql.split('VALUES', 1)[1].split(')', 1)[0]
                    target_count = len([c.strip() for c in columns_part.split(',') if c.strip()])
                    placeholder_count = value_part.count('%s')
                    literal_count = value_part.count("''")
                    param_count = len(params or ())
                    checked_inserts.append(
                        (target_count, placeholder_count, literal_count, param_count)
                    )
                    assert target_count == placeholder_count + literal_count
                    assert param_count == placeholder_count
                elif 'INSERT INTO geo_research_round_call' in sql:
                    self._fetch_data = {'id': 1}

            def fetchone(self):
                return self._fetch_data

            def fetchall(self):
                return []

            def close(self):
                pass

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

            def close(self):
                pass

        async def fake_retry(fetcher, prompt_id, prompt, max_retries=3):
            return {
                'platform': 'deepseek',
                'prompt_id': prompt_id,
                'prompt': prompt,
                'answer': '答案明确引用第 1 条来源 [1]',
                'citations': [{
                    'url': 'https://example.com/article',
                    'title': '示例来源',
                    'rank': 1,
                    'is_answer_cited': True,
                    'adoption_rank': 1,
                }],
                'ok': True,
                'error': None,
                'raw': {},
            }

        plan = [{
            'industry_id': 1,
            'industry_name': '测试行业',
            'prompt_id': 100,
            'prompt_text': '测试 prompt',
        }]

        with patch.object(round_runner, 'PLATFORM_FETCHERS', {'deepseek': object()}), \
             patch.object(round_runner, 'get_connection', return_value=FakeConn()), \
             patch.object(round_runner, 'query_with_retry', side_effect=fake_retry), \
             patch.object(round_runner, '_log_cost'), \
             patch.object(round_runner, 'update_heartbeat'), \
             patch.object(round_runner, 'update_round_progress'):
            asyncio.run(round_runner.stage1_ai_fetch_all('round_shape', plan))

        # GEO article v1.4 appends provider/model/surface lineage and the exact
        # prompt snapshot while preserving one literal researcher value.
        assert checked_inserts == [(19, 18, 1, 18)]


# ==================== 成本日志 · 预算 fail-closed ====================

class TestCostLogFailClosed:
    """外部调用已发生后, cost_log 写失败必须停跑, 防止后续预算低估。"""

    def test_log_cost_write_failure_raises_cost_log_error(self):
        class FakeCursor:
            def execute(self, sql, params=()):
                raise RuntimeError('cost log table locked')

            def close(self):
                pass

        class FakeConn:
            def __init__(self):
                self.rolled_back = False
                self.closed = False

            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        fake_conn = FakeConn()

        with patch.object(round_runner, 'get_connection', return_value=fake_conn):
            with pytest.raises(round_runner.CostLogWriteError):
                round_runner._log_cost('round_cost', 'doubao', 2, 0.075)

        assert fake_conn.rolled_back is True
        assert fake_conn.closed is True

    def test_stage1_cost_log_failure_raises_cost_log_error(self):
        class FakeCursor:
            def __init__(self):
                self._fetch_data = None

            def execute(self, sql, params=()):
                if 'INSERT INTO geo_research_round_call' in sql:
                    self._fetch_data = {'id': 1}
                elif 'SELECT platform, COUNT(*) AS cnt' in sql:
                    self._fetch_data = [{'platform': 'doubao', 'cnt': 1}]
                else:
                    self._fetch_data = None

            def fetchone(self):
                if isinstance(self._fetch_data, dict):
                    return self._fetch_data
                return None

            def fetchall(self):
                if isinstance(self._fetch_data, list):
                    return self._fetch_data
                return []

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

            def close(self):
                pass

        async def fake_retry(fetcher, prompt_id, prompt, max_retries=3):
            return {'ok': True, 'answer': 'a', 'citations': [], 'raw': {}}

        plan = [{
            'industry_id': 1,
            'industry_name': '测试行业',
            'prompt_id': 100,
            'prompt_text': '测试 prompt',
        }]

        with patch.object(round_runner, 'PLATFORM_FETCHERS', {'doubao': object()}), \
             patch.object(round_runner, 'get_connection', return_value=FakeConn()), \
             patch.object(round_runner, 'query_with_retry', side_effect=fake_retry), \
             patch.object(round_runner, '_log_cost',
                          side_effect=round_runner.CostLogWriteError('stage1 cost failed')), \
             patch.object(round_runner, 'update_heartbeat'), \
             patch.object(round_runner, 'update_round_progress'):
            with pytest.raises(round_runner.CostLogWriteError):
                asyncio.run(round_runner.stage1_ai_fetch_all('round_stage1', plan))

    def test_run_round_cost_log_failure_returns_failed_resumable(self):
        snapshot = {
            'industries': [{'id': 1, 'name': '行业A', 'slug': 'a'}],
            'prompts_by_industry': {'1': [{'id': 10, 'text': 'q1'}]},
            'snapshotted_at': '2026-05-07T00:00:00',
        }

        async def fail_cost_log(round_id, snapshot, resume_from_stage):
            raise round_runner.CostLogWriteError('cost log insert failed')

        with patch.object(
            round_runner, 'check_month_budget',
            return_value={'ok': True, 'spent': 0, 'limit': 1000.0,
                          'remaining': 1000.0, 'reason': None},
        ), \
             patch.object(round_runner, '_run_pipeline', side_effect=fail_cost_log), \
             patch.object(round_runner, '_build_summary', return_value={
                 'total_articles_seen': 10,
                 'pending_review_count': 0,
                 'auto_skipped_count': 0,
                 'total_cost_yuan': 0,
                 'finished_at': '2026-05-07T01:00:00',
             }), \
             patch.object(round_runner, 'update_round_complete') as m_complete:

            status = asyncio.run(round_runner.run_round('round_cost', snapshot))

        assert status == 'failed_resumable'
        assert m_complete.call_count == 1
        call = m_complete.call_args
        kw_status = call.kwargs.get('status') or (call.args[1] if len(call.args) >= 2 else None)
        assert kw_status == 'failed_resumable'
        summary = call.kwargs.get('summary') or (call.args[2] if len(call.args) >= 3 else None)
        assert summary is not None
        assert summary.get('reason') == 'cost_log_write_failed'
        assert 'cost log insert failed' in summary.get('cost_log_error', '')


# ==================== Stage 7 ====================

class TestStage7Aggregate:

    def test_stage7_calls_placement_service_with_industries(self):
        """
        get_round_snapshot mock 回 fake snapshot,
        PlacementService().aggregate_research_stats 应被 ['行业A','行业B'] 调用,
        update_round_progress 应记 stage_7_aggregate_done + rows_written=5。
        """
        fake_snapshot = {
            'industries': [
                {'id': 1, 'name': '行业A', 'slug': 'a'},
                {'id': 2, 'name': '行业B', 'slug': 'b'},
            ],
            'prompts_by_industry': {'1': [], '2': []},
        }

        # mock PlacementService 类: 实例化后 aggregate_research_stats 返 5
        mock_instance = MagicMock()
        mock_instance.aggregate_research_stats = MagicMock(return_value=5)
        mock_ps_cls = MagicMock(return_value=mock_instance)

        with patch.object(round_runner, 'get_round_snapshot', return_value=fake_snapshot), \
             patch.object(round_runner, 'PlacementService', mock_ps_cls), \
             patch.object(round_runner, 'update_round_progress') as m_progress, \
             patch.object(round_runner, 'update_heartbeat'):

            asyncio.run(round_runner.stage7_aggregate_stats('round_xxx'))

        # PlacementService 被实例化 1 次
        assert mock_ps_cls.call_count == 1
        # aggregate_research_stats 被以 industries 名字列表调用
        mock_instance.aggregate_research_stats.assert_called_once_with(['行业A', '行业B'])

        # update_round_progress 至少有一次调用是 stage_7_aggregate_done + rows_written=5
        success_calls = [
            c for c in m_progress.call_args_list
            if c.kwargs.get('stage') == 'stage_7_aggregate_done'
            or (len(c.args) >= 2 and c.args[1] == 'stage_7_aggregate_done')
        ]
        assert len(success_calls) >= 1, f"未找到 stage_7_aggregate_done 调用: {m_progress.call_args_list}"

        # 拿其中一次, 拆 progress 字段验证 rows_written=5
        c = success_calls[0]
        progress = c.kwargs.get('progress')
        if progress is None and len(c.args) >= 3:
            progress = c.args[2]
        # 兼容 stage 关键字调用
        if progress is None:
            progress = c.kwargs.get('progress')
        assert progress is not None
        assert progress.get('rows_written') == 5
        assert progress.get('industries_count') == 2

    def test_stage7_aggregate_failure_is_not_swallowed(self):
        """推荐统计是飞轮输出层, 聚合失败必须让 round 可见失败, 不能静默 completed。"""
        fake_snapshot = {
            'industries': [{'id': 1, 'name': '行业A', 'slug': 'a'}],
            'prompts_by_industry': {'1': []},
        }

        mock_instance = MagicMock()
        mock_instance.aggregate_research_stats = MagicMock(
            side_effect=RuntimeError('stats write failed')
        )
        mock_ps_cls = MagicMock(return_value=mock_instance)

        with patch.object(round_runner, 'get_round_snapshot', return_value=fake_snapshot), \
             patch.object(round_runner, 'PlacementService', mock_ps_cls), \
             patch.object(round_runner, 'update_round_progress') as m_progress, \
             patch.object(round_runner, 'update_heartbeat'):

            with pytest.raises(round_runner.Stage7AggregateError):
                asyncio.run(round_runner.stage7_aggregate_stats('round_xxx'))

        failed_calls = [
            c for c in m_progress.call_args_list
            if c.kwargs.get('stage') == 'stage_7_aggregate_failed'
            or (len(c.args) >= 2 and c.args[1] == 'stage_7_aggregate_failed')
        ]
        assert len(failed_calls) == 1

    def test_run_round_marks_stage7_failure_resumable(self):
        """Stage 7 聚合失败时整轮应 failed_resumable, 避免推荐数据没写却显示 completed。"""
        snapshot = {
            'industries': [{'id': 1, 'name': '行业A', 'slug': 'a'}],
            'prompts_by_industry': {'1': [{'id': 10, 'text': 'q1'}]},
            'snapshotted_at': '2026-05-07T00:00:00',
        }

        async def fail_stage7(round_id):
            raise round_runner.Stage7AggregateError('stats write failed')

        with patch.object(round_runner, 'check_month_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 1000,
                                        'remaining': 1000, 'reason': None}), \
             patch.object(round_runner, 'check_round_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 350,
                                        'remaining': 350, 'reason': None}), \
             patch.object(round_runner, 'stage1_ai_fetch_all', new_callable=AsyncMock), \
             patch.object(
                 round_runner,
                 'stage2_extract_and_prefilter_urls',
                 return_value=[{'url': 'https://example.test/a', 'normalized_url': 'https://example.test/a'}],
             ), \
             patch.object(
                 round_runner,
                 'ensure_stage3_url_budget',
                 return_value=[{'url': 'https://example.test/a', 'normalized_url': 'https://example.test/a'}],
             ), \
             patch.object(round_runner, '_count_stage3_paid_urls', return_value=1), \
             patch.object(round_runner, 'stage3_crawl_articles', new_callable=AsyncMock), \
             patch.object(round_runner, 'stage4_clean_articles', new_callable=AsyncMock), \
             patch.object(round_runner, 'stage45_classify_article_intents', new_callable=AsyncMock), \
             patch.object(round_runner, 'stage5_filter_by_char_count'), \
             patch.object(round_runner, 'stage7_aggregate_stats', side_effect=fail_stage7), \
             patch.object(round_runner, 'stage8_notify_admins') as m_stage8, \
             patch.object(round_runner, 'get_round_status', return_value={'status': 'running'}), \
             patch.object(round_runner, '_get_batch_id', return_value='batch_round_xxx'), \
             patch.object(round_runner, '_build_summary', return_value={'total_articles_seen': 1}), \
             patch.object(round_runner, 'update_round_complete') as m_complete, \
             patch.object(round_runner, 'update_heartbeat'):

            status = asyncio.run(round_runner.run_round('round_xxx', snapshot))

        assert status == 'failed_resumable'
        m_stage8.assert_not_called()
        assert m_complete.call_count == 1
        summary = m_complete.call_args.kwargs.get('summary') or {}
        assert summary.get('reason') == 'stage_7_aggregate_failed'
        assert 'stats write failed' in summary.get('stage_7_error', '')


# ==================== Stage 8 ====================

class TestStage8Notify:

    def test_stage8_defers_notification_to_terminal_outbox(self):
        """Stage 8 records a checkpoint; terminal commit owns the outbox write."""
        with patch.object(round_runner, 'update_round_progress') as m_progress, \
             patch.object(round_runner, 'update_heartbeat'):

            round_runner.stage8_notify_admins(
                'round_xxx',
                # Phase 9: 新 summary 字段 · 老 pending_review_count 保留兼容
                {'in_library_count': 7, 'auto_skipped_count': 3, 'total_cost_yuan': 12.34},
            )

        # The checkpoint must explicitly prove that notification delivery is
        # deferred until update_round_complete commits its durable outbox row.
        progress_calls = [
            c for c in m_progress.call_args_list
            if c.kwargs.get('stage') == 'stage_8_notify_done'
        ]
        assert len(progress_calls) >= 1
        progress = progress_calls[0].kwargs.get('progress') or {}
        assert progress == {'notification_deferred_to_terminal_outbox': True}


# ==================== run_round 主调度 ====================

class TestRunRound:

    def _make_snapshot(self):
        return {
            'industries': [
                {'id': 1, 'name': '行业A', 'slug': 'a'},
            ],
            'prompts_by_industry': {
                '1': [{'id': 10, 'text': 'q1'}],
            },
            'snapshotted_at': '2026-05-07T00:00:00',
        }

    def test_flatten_plan_accepts_scheduler_prompt_text_shape(self):
        """cron/scheduler snapshot 使用 prompt_text 字段, runner 不能打空 prompt。"""
        snapshot = {
            'industries': [{'id': 1, 'name': '行业A', 'slug': 'a'}],
            'prompts_by_industry': {
                1: [{'id': 10, 'prompt_text': '来自 scheduler 的问题'}],
            },
        }

        plan = round_runner._flatten_plan(snapshot)

        assert len(plan) == 1
        assert plan[0]['prompt_text'] == '来自 scheduler 的问题'

    def test_run_round_preserves_cancelled_status_at_stage_boundary(self):
        """管理员取消后 runner 应在 stage 边界退出, 不能最后覆盖成 completed。"""
        call_order: list = []

        async def s1(round_id, plan):
            call_order.append('s1')

        with patch.object(round_runner, 'stage1_ai_fetch_all', side_effect=s1), \
             patch.object(round_runner, 'stage2_extract_and_prefilter_urls') as m_s2, \
             patch.object(round_runner, 'check_month_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 1000, 'remaining': 1000, 'reason': None}), \
             patch.object(round_runner, 'check_round_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 350, 'remaining': 350, 'reason': None}), \
             patch.object(round_runner, 'get_round_status',
                          side_effect=[
                              {'status': 'running'},
                              {'status': 'cancelled'},
                          ]), \
             patch.object(round_runner, '_get_batch_id', return_value='batch_round_xxx'), \
             patch.object(round_runner, 'update_round_complete') as m_complete, \
             patch.object(round_runner, 'update_heartbeat'):

            status = asyncio.run(round_runner.run_round('round_xxx', self._make_snapshot()))

        assert status == 'cancelled'
        assert call_order == ['s1']
        m_s2.assert_not_called()
        m_complete.assert_not_called()

    def test_run_round_rejects_truncated_snapshot_before_external_calls(self):
        """snapshot 若已被截断, 必须 fail-fast, 不能跑空计划并标 completed。"""
        snapshot = {'truncated': True, 'original_size_bytes': 999999, 'preview': '{}'}

        with patch.object(round_runner, 'check_month_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 1000, 'remaining': 1000, 'reason': None}), \
             patch.object(round_runner, 'stage1_ai_fetch_all', new_callable=AsyncMock) as m_s1, \
             patch.object(round_runner, 'update_round_complete') as m_complete:

            status = asyncio.run(round_runner.run_round('round_xxx', snapshot))

        assert status == 'failed'
        m_s1.assert_not_called()
        assert m_complete.call_count == 1
        summary = m_complete.call_args.kwargs.get('summary')
        assert summary['reason'] == 'snapshot_truncated'

    def test_run_round_happy_path(self):
        """所有 stage mock 立即返, 验证调用顺序 + final status='completed'。"""
        call_order: list = []

        async def s1(round_id, plan):
            call_order.append('s1')

        def s2(round_id, batch_id, plan=None):
            # P14: stage2 signature 加 plan 参数 (citation 上下文用 · Optional)
            call_order.append('s2')
            return [{'url': 'https://x.com', 'normalized_url': 'https://x.com'}]

        async def s3(round_id, urls):
            call_order.append('s3')

        async def s4(round_id):
            call_order.append('s4')

        async def s45(round_id):
            call_order.append('s45')

        def s5(round_id):
            call_order.append('s5')

        # Phase 9: stage 6 已物理删 · 不再注册 s6
        async def s7(round_id):
            call_order.append('s7')

        def s8(round_id, summary):
            call_order.append('s8')

        with patch.object(round_runner, 'stage1_ai_fetch_all', side_effect=s1), \
             patch.object(round_runner, 'stage2_extract_and_prefilter_urls', side_effect=s2), \
             patch.object(
                 round_runner,
                 'ensure_stage3_url_budget',
                 side_effect=lambda _round_id, _batch_id, urls: urls,
             ), \
             patch.object(round_runner, '_count_stage3_paid_urls', return_value=1), \
             patch.object(round_runner, 'stage3_crawl_articles', side_effect=s3), \
             patch.object(round_runner, 'stage4_clean_articles', side_effect=s4), \
             patch.object(round_runner, 'stage45_classify_article_intents', side_effect=s45), \
             patch.object(round_runner, 'stage5_filter_by_char_count', side_effect=s5), \
             patch.object(round_runner, 'stage7_aggregate_stats', side_effect=s7), \
             patch.object(round_runner, 'stage8_notify_admins', side_effect=s8), \
             patch.object(round_runner, 'check_month_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 1000,
                                        'remaining': 1000, 'reason': None}), \
             patch.object(round_runner, 'check_round_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 350,
                                        'remaining': 350, 'reason': None}), \
             patch.object(round_runner, 'get_round_status', return_value={'status': 'running'}), \
             patch.object(round_runner, '_build_summary', return_value={
                 'total_articles_seen': 10,
                 'pending_review_count': 5,
                 'auto_skipped_count': 5,
                 'total_cost_yuan': 1.23,
                 'finished_at': '2026-05-07T01:00:00',
             }), \
             patch.object(round_runner, '_get_batch_id', return_value='batch_round_xxx'), \
             patch.object(round_runner, 'update_round_complete') as m_complete, \
             patch.object(round_runner, 'update_heartbeat'):

            status = asyncio.run(round_runner.run_round('round_xxx', self._make_snapshot()))

        assert status == 'completed'
        # 严格顺序 s1→s8
        # Phase 9: stage 6 已删 · stage 4.5 保留文章意图分类
        assert call_order == ['s1', 's2', 's3', 's4', 's45', 's5', 's7', 's8']

        # update_round_complete 用 status='completed' 调用
        assert m_complete.call_count == 1
        complete_call = m_complete.call_args
        kwargs_status = complete_call.kwargs.get('status')
        if kwargs_status is None and len(complete_call.args) >= 2:
            kwargs_status = complete_call.args[1]
        assert kwargs_status == 'completed'

    def test_run_round_fails_when_stage2_has_no_citation_urls(self):
        """Stage 1/2 没有采到可爬引用时, 不能继续跑空流程并标 completed。"""
        call_order: list = []

        async def s1(round_id, plan):
            call_order.append('s1')

        def s2(round_id, batch_id, plan=None):
            call_order.append('s2')
            return []

        with patch.object(round_runner, 'stage1_ai_fetch_all', side_effect=s1), \
             patch.object(round_runner, 'stage2_extract_and_prefilter_urls', side_effect=s2), \
             patch.object(round_runner, 'stage3_crawl_articles', new_callable=AsyncMock) as m_s3, \
             patch.object(round_runner, 'stage4_clean_articles', new_callable=AsyncMock) as m_s4, \
             patch.object(round_runner, 'stage45_classify_article_intents', new_callable=AsyncMock) as m_s45, \
             patch.object(round_runner, 'stage5_filter_by_char_count') as m_s5, \
             patch.object(round_runner, 'stage7_aggregate_stats', new_callable=AsyncMock) as m_s7, \
             patch.object(round_runner, 'stage8_notify_admins') as m_s8, \
             patch.object(round_runner, 'check_month_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 1000,
                                        'remaining': 1000, 'reason': None}), \
             patch.object(round_runner, 'check_round_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 350,
                                        'remaining': 350, 'reason': None}), \
             patch.object(round_runner, 'get_round_status', return_value={'status': 'running'}), \
             patch.object(round_runner, '_build_summary', return_value={
                 'total_articles_seen': 0,
                 'pending_review_count': 0,
                 'auto_skipped_count': 0,
                 'total_cost_yuan': 1.5,
                 'finished_at': '2026-05-07T01:00:00',
             }), \
             patch.object(round_runner, '_get_batch_id', return_value='batch_round_xxx'), \
             patch.object(round_runner, 'update_round_complete') as m_complete, \
             patch.object(round_runner, 'update_heartbeat'):

            status = asyncio.run(round_runner.run_round('round_xxx', self._make_snapshot()))

        assert status == 'failed'
        assert call_order == ['s1', 's2']
        m_s3.assert_not_called()
        m_s4.assert_not_called()
        m_s45.assert_not_called()
        m_s5.assert_not_called()
        m_s7.assert_not_called()
        m_s8.assert_not_called()
        assert m_complete.call_count == 1
        complete_call = m_complete.call_args
        kwargs_status = complete_call.kwargs.get('status')
        if kwargs_status is None and len(complete_call.args) >= 2:
            kwargs_status = complete_call.args[1]
        assert kwargs_status == 'failed'
        summary = complete_call.kwargs.get('summary') or (
            complete_call.args[2] if len(complete_call.args) >= 3 else None
        )
        assert summary['reason'] == 'empty_citation_urls'

    def test_run_round_resume_fails_when_summary_has_zero_articles(self):
        """续跑跳过 stage 3 时, 最终 0 原文仍必须 fail-closed。"""
        call_order: list = []

        def s2(round_id, batch_id, plan=None):
            call_order.append('s2')
            return [{'url': 'https://x.com/a', 'normalized_url': 'https://x.com/a'}]

        async def s4(round_id):
            call_order.append('s4')

        async def s45(round_id):
            call_order.append('s45')

        def s5(round_id):
            call_order.append('s5')

        async def s7(round_id):
            call_order.append('s7')

        with patch.object(round_runner, 'stage1_ai_fetch_all', new_callable=AsyncMock) as m_s1, \
             patch.object(round_runner, 'stage2_extract_and_prefilter_urls', side_effect=s2), \
             patch.object(round_runner, 'stage3_crawl_articles', new_callable=AsyncMock) as m_s3, \
             patch.object(round_runner, 'stage4_clean_articles', side_effect=s4), \
             patch.object(round_runner, 'stage45_classify_article_intents', side_effect=s45), \
             patch.object(round_runner, 'stage5_filter_by_char_count', side_effect=s5), \
             patch.object(round_runner, 'stage7_aggregate_stats', side_effect=s7), \
             patch.object(round_runner, 'stage8_notify_admins') as m_s8, \
             patch.object(round_runner, 'check_month_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 1000,
                                        'remaining': 1000, 'reason': None}), \
             patch.object(round_runner, 'check_round_budget',
                          return_value={'ok': True, 'spent': 0, 'limit': 350,
                                        'remaining': 350, 'reason': None}), \
             patch.object(round_runner, 'get_round_status', return_value={'status': 'running'}), \
             patch.object(round_runner, '_build_summary', return_value={
                 'total_articles_seen': 0,
                 'pending_review_count': 0,
                 'auto_skipped_count': 0,
                 'total_cost_yuan': 0.5,
                 'finished_at': '2026-05-07T01:00:00',
             }), \
             patch.object(round_runner, '_get_batch_id', return_value='batch_round_xxx'), \
             patch.object(round_runner, 'update_round_complete') as m_complete, \
             patch.object(round_runner, 'update_heartbeat'):

            status = asyncio.run(
                round_runner.run_round(
                    'round_xxx',
                    self._make_snapshot(),
                    resume_from_stage='stage_4',
                )
            )

        assert status == 'failed'
        assert call_order == ['s2', 's4', 's45', 's5', 's7']
        m_s1.assert_not_called()
        m_s3.assert_not_called()
        m_s8.assert_not_called()
        assert m_complete.call_count == 1
        complete_call = m_complete.call_args
        kwargs_status = complete_call.kwargs.get('status')
        if kwargs_status is None and len(complete_call.args) >= 2:
            kwargs_status = complete_call.args[1]
        assert kwargs_status == 'failed'
        summary = complete_call.kwargs.get('summary') or (
            complete_call.args[2] if len(complete_call.args) >= 3 else None
        )
        assert summary['reason'] == 'empty_citation_urls'

    def test_run_round_timeout(self):
        """stage1 sleep 5s + ROUND_TIMEOUT_SECONDS=0.1 → 'failed_resumable'"""
        async def slow_s1(round_id, plan):
            await asyncio.sleep(5)

        original_timeout = round_runner.ROUND_TIMEOUT_SECONDS
        try:
            round_runner.ROUND_TIMEOUT_SECONDS = 0.1

            with patch.object(round_runner, 'stage1_ai_fetch_all', side_effect=slow_s1), \
                 patch.object(round_runner, 'stage2_extract_and_prefilter_urls', return_value=[]), \
                 patch.object(round_runner, 'stage3_crawl_articles', new_callable=AsyncMock), \
                 patch.object(round_runner, 'stage4_clean_articles', new_callable=AsyncMock), \
                 patch.object(round_runner, 'stage5_filter_by_char_count'), \
                 patch.object(round_runner, 'stage7_aggregate_stats', new_callable=AsyncMock), \
                 patch.object(round_runner, 'stage8_notify_admins'), \
                 patch.object(round_runner, 'check_month_budget',
                              return_value={'ok': True, 'spent': 0, 'limit': 1000,
                                            'remaining': 1000, 'reason': None}), \
                 patch.object(round_runner, 'check_round_budget',
                              return_value={'ok': True, 'spent': 0, 'limit': 350,
                                            'remaining': 350, 'reason': None}), \
                 patch.object(round_runner, 'get_round_status', return_value={'status': 'running'}), \
                 patch.object(round_runner, '_build_summary', return_value={
                     'total_articles_seen': 0, 'pending_review_count': 0,
                     'auto_skipped_count': 0, 'total_cost_yuan': 0,
                     'finished_at': '2026-05-07T01:00:00',
                 }), \
                 patch.object(round_runner, '_get_batch_id', return_value='batch_round_xxx'), \
                 patch.object(round_runner, 'update_round_complete') as m_complete, \
                 patch.object(round_runner, 'update_heartbeat'):

                status = asyncio.run(round_runner.run_round('round_xxx', self._make_snapshot()))

            assert status == 'failed_resumable'

            # update_round_complete 用 status='failed_resumable' + summary.reason='4h_hard_timeout'
            assert m_complete.call_count == 1
            call = m_complete.call_args
            kw_status = call.kwargs.get('status') or (call.args[1] if len(call.args) >= 2 else None)
            assert kw_status == 'failed_resumable'
            summary = call.kwargs.get('summary') or (call.args[2] if len(call.args) >= 3 else None)
            assert summary is not None
            assert summary.get('reason') == '4h_hard_timeout'
        finally:
            round_runner.ROUND_TIMEOUT_SECONDS = original_timeout


# ==================== A.5.4 预算熔断 ====================

class TestRunRoundMonthBudgetExhausted:
    """月度预算超 1000 → 不进 stage 1, 直接 cancelled"""

    def _make_snapshot(self):
        return {
            'industries': [{'id': 1, 'name': '行业A', 'slug': 'a'}],
            'prompts_by_industry': {'1': [{'id': 10, 'text': 'q1'}]},
            'snapshotted_at': '2026-05-07T00:00:00',
        }

    def test_month_budget_exhausted_returns_cancelled(self):
        with patch.object(
            round_runner, 'check_month_budget',
            return_value={
                'ok': False,
                'spent': 1100.0,
                'limit': 1000.0,
                'remaining': -100.0,
                'reason': '月度预算超支 ¥1100.00/¥1000.00',
            },
        ), \
             patch.object(round_runner, 'stage1_ai_fetch_all', new_callable=AsyncMock) as m_s1, \
             patch.object(round_runner, 'update_round_complete') as m_complete:

            status = asyncio.run(round_runner.run_round('round_xxx', self._make_snapshot()))

        assert status == 'cancelled'
        # stage 1 必须没被调
        m_s1.assert_not_called()

        # update_round_complete status='cancelled', summary.cancelled_reason='month_budget_exhausted'
        assert m_complete.call_count == 1
        call = m_complete.call_args
        kw_status = call.kwargs.get('status') or (call.args[1] if len(call.args) >= 2 else None)
        assert kw_status == 'cancelled'
        summary = call.kwargs.get('summary') or (call.args[2] if len(call.args) >= 3 else None)
        assert summary is not None
        assert summary.get('cancelled_reason') == 'month_budget_exhausted'
        assert summary.get('month_spent_yuan') == 1100.0
        assert summary.get('month_limit_yuan') == 1000.0


class TestRunRoundRoundBudgetExhausted:
    """单轮预算在外部成本 stage 之前超 350 → BudgetExhaustedError → failed_resumable

    Phase 9 (P09 d549a0fe) 之后:
      - stage 4 改规则清洗 0 LLM 成本 · stage 4 之前的 budget check 已删
      - 现在 budget check 只在 stage 1 (AI fetch · 主成本) 和 stage 3 (Jina crawl · 次成本) 之前
      - 测试改成 "stage 3 前熔断" · 锁实际触发位置
    """

    def _make_snapshot(self):
        return {
            'industries': [{'id': 1, 'name': '行业A', 'slug': 'a'}],
            'prompts_by_industry': {'1': [{'id': 10, 'text': 'q1'}]},
            'snapshotted_at': '2026-05-07T00:00:00',
        }

    def test_round_budget_exhausted_at_stage3_returns_failed_resumable(self):
        # P12-fix-v4 (2026-05-26) 改: stage4 已无 budget check · 改测试期望 stage3 前熔断
        # check_round_budget 调用顺序: stage_1 (start), stage_3 (before crawl)
        # 前 1 次 ok · 第 2 次超
        budget_calls = []

        def fake_check_round_budget(round_id):
            budget_calls.append(round_id)
            if len(budget_calls) <= 1:
                return {'ok': True, 'spent': 100.0, 'limit': 350.0,
                        'remaining': 250.0, 'reason': None}
            return {'ok': False, 'spent': 360.0, 'limit': 350.0,
                    'remaining': -10.0, 'reason': '单轮预算超支 ¥360.00/¥350.00'}

        with patch.object(
            round_runner, 'check_month_budget',
            return_value={'ok': True, 'spent': 0, 'limit': 1000.0,
                          'remaining': 1000.0, 'reason': None},
        ), \
             patch.object(round_runner, 'check_round_budget', side_effect=fake_check_round_budget), \
             patch.object(round_runner, 'stage1_ai_fetch_all', new_callable=AsyncMock), \
             patch.object(round_runner, 'stage2_extract_and_prefilter_urls', return_value=['http://x']), \
             patch.object(
                 round_runner,
                 'ensure_stage3_url_budget',
                 return_value=[{'url': 'http://x', 'normalized_url': 'http://x'}],
             ), \
             patch.object(round_runner, '_count_stage3_paid_urls', return_value=1), \
             patch.object(round_runner, 'stage3_crawl_articles', new_callable=AsyncMock) as m_s3, \
             patch.object(round_runner, 'stage4_clean_articles', new_callable=AsyncMock) as m_s4, \
             patch.object(round_runner, '_build_summary', return_value={
                 'total_articles_seen': 0, 'in_library_count': 0,
                 'auto_skipped_count': 0, 'total_cost_yuan': 360.0,
                 'finished_at': '2026-05-07T01:00:00',
             }), \
             patch.object(round_runner, '_get_batch_id', return_value='batch_round_xxx'), \
             patch.object(round_runner, 'update_round_complete') as m_complete, \
             patch.object(round_runner, 'update_heartbeat'):

            status = asyncio.run(round_runner.run_round('round_xxx', self._make_snapshot()))

        assert status == 'failed_resumable'
        # stage 3 + stage 4 都不应被调 (在 stage 3 前就熔断了)
        m_s3.assert_not_called()
        m_s4.assert_not_called()

        # update_round_complete status='failed_resumable', reason='round_budget_exhausted'
        assert m_complete.call_count == 1
        call = m_complete.call_args
        kw_status = call.kwargs.get('status') or (call.args[1] if len(call.args) >= 2 else None)
        assert kw_status == 'failed_resumable'
        summary = call.kwargs.get('summary') or (call.args[2] if len(call.args) >= 3 else None)
        assert summary is not None
        assert summary.get('reason') == 'round_budget_exhausted'
        assert summary.get('budget_level') == 'round'
        assert summary.get('budget_spent_yuan') == 360.0
        assert summary.get('budget_limit_yuan') == 350.0
