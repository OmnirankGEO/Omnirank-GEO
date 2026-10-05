"""round 状态 CRUD + snapshot + heartbeat 测试 (用真 PG 测试库)"""
import pytest
import json

pytestmark = pytest.mark.integration

from services.research_monitor.round_state import (
    create_round_with_snapshot,
    update_heartbeat,
    update_round_progress,
    update_round_complete,
    mark_round_resume_requested,
    get_round_snapshot,
    get_round_status,
    truncate_large_jsonb,
    RoundAlreadyRunningError,
)


class TestCreateRoundWithSnapshot:

    def test_create_round_basic(self, pg_conn, clean_research_tables, sample_industry, sample_prompts):
        """创建 round + 拿 round_id"""
        cur = pg_conn.cursor()
        cur.execute("SELECT id, name, slug FROM geo_research_industries WHERE id = %s", (sample_industry,))
        ind_row = cur.fetchone()
        industries = [{'id': ind_row['id'], 'name': ind_row['name'], 'slug': ind_row['slug']}]

        cur.execute("SELECT id, prompt_text FROM geo_research_prompts WHERE industry_id = %s", (sample_industry,))
        prompts = [{'id': r['id'], 'text': r['prompt_text']} for r in cur.fetchall()]
        prompts_by_industry = {sample_industry: prompts}

        round_id = create_round_with_snapshot(
            triggered_by='manual',
            industries=industries,
            prompts_by_industry=prompts_by_industry,
            triggered_user_id=1,
        )

        assert round_id.startswith('round_')
        cur.execute(
            "SELECT round_id, status, snapshot_json, triggered_by FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row['status'] == 'pending'
        assert row['triggered_by'] == 'manual'
        snapshot = row['snapshot_json']
        assert 'industries' in snapshot
        assert 'prompts_by_industry' in snapshot
        assert len(snapshot['industries']) == 1

    def test_create_round_id_unique(self, pg_conn, clean_research_tables, sample_industry, sample_prompts):
        """连续创建 2 个 round, round_id 不撞"""
        cur = pg_conn.cursor()
        cur.execute("SELECT id, name, slug FROM geo_research_industries WHERE id = %s", (sample_industry,))
        ind_row = cur.fetchone()
        industries = [{'id': ind_row['id'], 'name': ind_row['name'], 'slug': ind_row['slug']}]
        prompts_by_industry = {sample_industry: []}

        rid1 = create_round_with_snapshot(triggered_by='manual', industries=industries, prompts_by_industry=prompts_by_industry)
        rid2 = create_round_with_snapshot(triggered_by='manual', industries=industries, prompts_by_industry=prompts_by_industry)
        assert rid1 != rid2


class TestHeartbeat:

    def test_update_heartbeat_basic(self, pg_conn, clean_research_tables):
        """update_heartbeat 推进 last_heartbeat_at"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        result = update_heartbeat(round_id)
        assert result is True
        cur = pg_conn.cursor()
        cur.execute("SELECT last_heartbeat_at FROM geo_research_round WHERE round_id = %s", (round_id,))
        ts = cur.fetchone()['last_heartbeat_at']
        assert ts is not None

    def test_update_heartbeat_nonexistent_round(self, clean_research_tables):
        """update_heartbeat 不存在的 round 返 False"""
        result = update_heartbeat('round_nonexistent_xxx')
        assert result is False


class TestProgressAndComplete:

    def test_update_round_progress(self, pg_conn, clean_research_tables):
        """update_round_progress 更新 current_stage + progress_json"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        result = update_round_progress(
            round_id,
            stage='stage_1_ai_fetch',
            progress={'fetched': 1240, 'total': 1700, 'errors': 8},
        )
        assert result is True
        cur = pg_conn.cursor()
        cur.execute(
            "SELECT current_stage, progress_json, status FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        assert row['current_stage'] == 'stage_1_ai_fetch'
        assert row['progress_json']['fetched'] == 1240
        assert row['status'] == 'running'

    def test_update_round_complete_success(self, pg_conn, clean_research_tables):
        """update_round_complete 设 finished_at + summary"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        update_round_progress(round_id, stage='stage_1_ai_fetch', progress={})
        update_round_complete(round_id, status='completed', summary={'raw_inserted': 1700})
        cur = pg_conn.cursor()
        cur.execute(
            "SELECT status, finished_at, summary_json FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        assert row['status'] == 'completed'
        assert row['finished_at'] is not None
        assert row['summary_json']['raw_inserted'] == 1700

    def test_update_round_complete_does_not_overwrite_cancelled(self, pg_conn, clean_research_tables):
        """管理员取消后, runner 最终 completed/failed 收尾不能覆盖 cancelled。"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        update_round_progress(round_id, stage='stage_8_notify', progress={})

        cancelled = update_round_complete(
            round_id,
            status='cancelled',
            summary={'cancelled_reason': 'admin_cancelled'},
        )
        overwritten = update_round_complete(
            round_id,
            status='completed',
            summary={'raw_inserted': 1700},
        )

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT status, summary_json FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        assert cancelled is True
        assert overwritten is False
        assert row['status'] == 'cancelled'
        assert row['summary_json']['cancelled_reason'] == 'admin_cancelled'
        assert 'raw_inserted' not in row['summary_json']

    def test_mark_round_resume_requested_blocks_other_active_round(self, pg_conn, clean_research_tables):
        """续跑 failed_resumable 前也必须防止已有 pending/running 跑批并发。"""
        failed_round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        update_round_complete(
            failed_round_id,
            status='failed_resumable',
            summary={'reason': '4h_hard_timeout'},
        )
        active_round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )

        with pytest.raises(RoundAlreadyRunningError):
            mark_round_resume_requested(failed_round_id, requested_by=1)

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT status FROM geo_research_round WHERE round_id IN (%s, %s) ORDER BY round_id",
            (failed_round_id, active_round_id),
        )
        statuses = {row['status'] for row in cur.fetchall()}
        assert statuses == {'failed_resumable', 'pending'}

    def test_mark_round_resume_requested_claims_round_once(self, pg_conn, clean_research_tables):
        """同一个 failed_resumable round 只能被一个 resume 请求抢到。"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        update_round_complete(
            round_id,
            status='failed_resumable',
            summary={'reason': 'round_budget_exhausted'},
        )

        first = mark_round_resume_requested(round_id, requested_by=1)
        second = mark_round_resume_requested(round_id, requested_by=2)

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT status, finished_at, summary_json FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        assert first is True
        assert second is False
        assert row['status'] == 'pending'
        assert row['finished_at'] is None
        assert row['summary_json']['resume_requested_by'] == 1


class TestGetRoundSnapshot:

    def test_get_round_snapshot(self, pg_conn, clean_research_tables, sample_industry, sample_prompts):
        cur = pg_conn.cursor()
        cur.execute("SELECT id, name, slug FROM geo_research_industries WHERE id = %s", (sample_industry,))
        ind_row = cur.fetchone()
        industries = [{'id': ind_row['id'], 'name': ind_row['name'], 'slug': ind_row['slug']}]
        prompts_by_industry = {sample_industry: [{'id': pid, 'text': f'test {pid}'} for pid in sample_prompts]}

        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=industries, prompts_by_industry=prompts_by_industry,
        )

        snapshot = get_round_snapshot(round_id)
        assert snapshot is not None
        assert len(snapshot['industries']) == 1
        assert snapshot['industries'][0]['name'] == ind_row['name']
        # prompts_by_industry 的 key 序列化为 str
        assert str(sample_industry) in snapshot['prompts_by_industry'] or sample_industry in snapshot['prompts_by_industry']

    def test_get_round_status(self, pg_conn, clean_research_tables):
        """get_round_status 返回完整字段字典"""
        round_id = create_round_with_snapshot(
            triggered_by='manual', industries=[], prompts_by_industry={},
        )
        update_round_progress(round_id, stage='stage_1_ai_fetch', progress={'fetched': 5})
        status = get_round_status(round_id)
        assert status is not None
        assert status['round_id'] == round_id
        assert status['status'] == 'running'
        assert status['current_stage'] == 'stage_1_ai_fetch'
        assert status['progress_json']['fetched'] == 5

    def test_get_round_status_nonexistent(self, clean_research_tables):
        assert get_round_status('round_doesnotexist') is None


class TestTruncateJsonb:

    def test_small_jsonb_unchanged(self):
        """小 JSONB 不截断"""
        small = {'industries': [{'id': 1, 'name': 'test'}], 'prompts_by_industry': {}}
        result = truncate_large_jsonb(small, max_size_kb=50)
        assert result == small

    def test_large_jsonb_truncated(self):
        """超 50KB JSONB 截断为占位符 + original_size"""
        huge = {'industries': [{'id': i, 'name': 'x' * 1000} for i in range(80)]}
        result = truncate_large_jsonb(huge, max_size_kb=50)
        assert 'truncated' in result
        assert result['truncated'] is True
        assert 'original_size_bytes' in result
        assert result['original_size_bytes'] > 50 * 1024

    def test_empty_jsonb_unchanged(self):
        """空 dict / None 不截断"""
        assert truncate_large_jsonb({}) == {}
        assert truncate_large_jsonb(None) is None
