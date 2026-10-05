"""
A.5.6 scheduler_setup 单元测试 (纯 mock, 不连真 PG / 真 scheduler)。

覆盖:
- get_active_plan: 正常拼装 + 行业为空时返空
- get_last_completed_round_at: 透传 fetchone['max']
- should_run_missed_cron: 从未跑过 / 最近跑过 / 距上次预定 cron 久远
- schedule_research_monitor_jobs: 注册主 cron / 触发补跑时多注册一个 date trigger
"""
import sys
import types
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta
from pathlib import Path

import pytz


# 在导入被测模块前 stub 掉 api.scheduler, 避免 import api/__init__.py
# 触发 employee_api → diagnosis_db → 真 PG 连接(测试环境无可用 DB)。
# 注意: scheduler_setup 内部用 `from api.scheduler import get_scheduler` 延迟导入,
# stub 仅需在 schedule_research_monitor_jobs 被调用前就绪。
def _ensure_api_scheduler_stub() -> tuple[bool, bool]:
    created_api = False
    created_scheduler = False
    if 'api' not in sys.modules:
        api_pkg = types.ModuleType('api')
        api_pkg.__path__ = [str(Path(__file__).resolve().parents[2] / 'api')]
        sys.modules['api'] = api_pkg
        created_api = True
    if 'api.scheduler' not in sys.modules:
        api_sched = types.ModuleType('api.scheduler')
        api_sched.get_scheduler = lambda: None  # 默认值, 测试用 patch 覆盖
        sys.modules['api.scheduler'] = api_sched
        created_scheduler = True
    return created_api, created_scheduler


@pytest.fixture(autouse=True)
def _isolated_api_scheduler_stub():
    """Install the lazy scheduler stub per test and remove only what we create.

    A module-level stub polluted pytest collection and made unrelated scheduler
    contract tests import an empty ``api.scheduler``.  The production module is
    still not imported by this unit-test file, while combined suites now remain
    order-independent.
    """
    created_api, created_scheduler = _ensure_api_scheduler_stub()
    try:
        yield
    finally:
        if created_scheduler:
            sys.modules.pop('api.scheduler', None)
        if created_api:
            sys.modules.pop('api', None)

from services.research_monitor import scheduler_setup
from services.research_monitor.scheduler_setup import (
    get_active_plan,
    get_last_completed_round_at,
    should_run_missed_cron,
    schedule_research_monitor_jobs,
    JOB_ID_BIMONTHLY_ROUND,
    JOB_ID_MISSED_RECOVERY,
    BEIJING_TZ,
)


def _make_mock_conn(fetchall_results=None, fetchone_result=None):
    """构造支持 fetchall/fetchone 序列调用的 mock conn。"""
    conn = MagicMock()
    cur = MagicMock()
    if fetchall_results is not None:
        cur.fetchall.side_effect = list(fetchall_results)
    if fetchone_result is not None:
        cur.fetchone.return_value = fetchone_result
    conn.cursor.return_value = cur
    return conn


# ==================== get_active_plan ====================

class TestGetActivePlan:

    def test_normal_returns_grouped_prompts(self):
        industries_rows = [
            {'id': 1, 'name': '美妆', 'slug': 'beauty'},
            {'id': 2, 'name': '母婴', 'slug': 'mother-baby'},
        ]
        prompt_rows = [
            {'id': 11, 'industry_id': 1, 'prompt_text': 'p1', 'sort_order': 0,
             'is_sensitive': False, 'source': 'manual'},
            {'id': 12, 'industry_id': 1, 'prompt_text': 'p2', 'sort_order': 1,
             'is_sensitive': True, 'source': 'manual'},
            {'id': 21, 'industry_id': 2, 'prompt_text': 'p3', 'sort_order': 0,
             'is_sensitive': False, 'source': 'ai_generated'},
        ]
        conn = _make_mock_conn(fetchall_results=[industries_rows, prompt_rows])
        with patch.object(scheduler_setup, 'get_connection', return_value=conn):
            result = get_active_plan()

        assert 'industries' in result
        assert 'prompts_by_industry' in result
        assert len(result['industries']) == 2
        assert result['industries'][0]['slug'] == 'beauty'

        pbi = result['prompts_by_industry']
        assert sorted(pbi.keys()) == [1, 2]
        assert len(pbi[1]) == 2
        assert pbi[1][0]['prompt_text'] == 'p1'
        assert pbi[1][1]['is_sensitive'] is True
        assert len(pbi[2]) == 1
        assert pbi[2][0]['source'] == 'ai_generated'

    def test_orphan_prompt_industry_skipped(self):
        """prompt 指向已软删行业时被跳过, 不抛异常"""
        industries_rows = [{'id': 1, 'name': '美妆', 'slug': 'beauty'}]
        prompt_rows = [
            {'id': 11, 'industry_id': 1, 'prompt_text': 'p1', 'sort_order': 0,
             'is_sensitive': False, 'source': 'manual'},
            {'id': 99, 'industry_id': 999, 'prompt_text': 'orphan', 'sort_order': 0,
             'is_sensitive': False, 'source': 'manual'},
        ]
        conn = _make_mock_conn(fetchall_results=[industries_rows, prompt_rows])
        with patch.object(scheduler_setup, 'get_connection', return_value=conn):
            result = get_active_plan()
        assert list(result['prompts_by_industry'].keys()) == [1]
        assert len(result['prompts_by_industry'][1]) == 1


class TestGetActivePlanEmpty:

    def test_empty_industries_returns_empty_dict(self):
        conn = _make_mock_conn(fetchall_results=[[], []])
        with patch.object(scheduler_setup, 'get_connection', return_value=conn):
            result = get_active_plan()
        assert result == {'industries': [], 'prompts_by_industry': {}}


# ==================== get_last_completed_round_at ====================

class TestGetLastCompletedRoundAt:

    def test_returns_max_finished_at(self):
        ts = datetime(2026, 5, 1, 2, 0, 0, tzinfo=BEIJING_TZ)
        conn = _make_mock_conn(fetchone_result={'max': ts})
        with patch.object(scheduler_setup, 'get_connection', return_value=conn):
            result = get_last_completed_round_at()
        assert result == ts

    def test_no_rows_returns_none(self):
        conn = _make_mock_conn(fetchone_result={'max': None})
        with patch.object(scheduler_setup, 'get_connection', return_value=conn):
            result = get_last_completed_round_at()
        assert result is None


# ==================== should_run_missed_cron ====================

class TestShouldRunMissedCronNeverRan:

    def test_never_ran_does_not_trigger_recovery_by_default(self, monkeypatch):
        monkeypatch.delenv('RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY', raising=False)
        with patch.object(scheduler_setup, 'get_last_completed_round_at', return_value=None):
            assert should_run_missed_cron() is False

    def test_never_ran_triggers_recovery_only_when_explicitly_enabled(self, monkeypatch):
        monkeypatch.setenv('RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY', 'true')
        with patch.object(
            scheduler_setup, '_load_cron_config_from_db',
            return_value={'enabled': True, 'available': True, 'days': '1,16', 'hour': 2},
        ), patch.object(scheduler_setup, 'get_last_completed_round_at', return_value=None):
            assert should_run_missed_cron() is True


class TestShouldRunMissedCronRecent:

    def test_recent_completion_no_recovery(self, monkeypatch):
        monkeypatch.setenv('RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY', 'true')
        # 5 分钟前完成, 最近预定 cron 时刻 (本月 1/16 号 02:00) 一定 < 5 分钟前
        # 所以 last_completed > last_scheduled → 不补跑
        recent = datetime.now(BEIJING_TZ) - timedelta(minutes=5)
        with patch.object(scheduler_setup, 'get_last_completed_round_at', return_value=recent):
            assert should_run_missed_cron() is False


class TestShouldRunMissedCronStale:

    def test_stale_completion_triggers_recovery(self, monkeypatch):
        monkeypatch.setenv('RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY', 'true')
        # 假设 now=2026-05-07 → 最近预定 cron = 2026-05-01 02:00
        # last_completed = 10 天前 = 2026-04-27 → 早于 5-1, 应补跑
        fake_now = BEIJING_TZ.localize(datetime(2026, 5, 7, 10, 0, 0))
        last_completed = fake_now - timedelta(days=10)

        # 让 _last_scheduled_cron_time 用 fake_now 算
        original_fn = scheduler_setup._last_scheduled_cron_time
        with patch.object(
            scheduler_setup, '_load_cron_config_from_db',
            return_value={'enabled': True, 'available': True, 'days': '1,16', 'hour': 2},
        ), patch.object(
            scheduler_setup, '_last_scheduled_cron_time',
            side_effect=lambda now=None, days=None, hour=None: original_fn(
                fake_now,
                days=days,
                hour=hour,
            ),
        ):
            with patch.object(
                scheduler_setup, 'get_last_completed_round_at',
                return_value=last_completed,
            ):
                assert should_run_missed_cron() is True

    def test_missed_recovery_uses_db_cron_config(self, monkeypatch):
        """missed recovery 必须跟随 DB 里的 cron_days/hour, 不能写死 1/16 号 2 点。"""
        monkeypatch.setenv('RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY', 'true')
        calls = []

        def fake_last_scheduled(now=None, days=None, hour=None):
            calls.append({'days': days, 'hour': hour})
            return BEIJING_TZ.localize(datetime(2026, 5, 20, 7, 0))

        with patch.object(
            scheduler_setup, '_load_cron_config_from_db',
            return_value={'enabled': True, 'days': '5,20', 'hour': 7},
        ), patch.object(
            scheduler_setup,
            'get_active_plan',
            return_value={'industry_count': 1, 'prompt_count': 1, 'industries': []},
        ), patch.object(
            scheduler_setup,
            'get_last_completed_round_at',
            return_value=BEIJING_TZ.localize(datetime(2026, 5, 19, 10, 0)),
        ), patch.object(
            scheduler_setup,
            '_last_scheduled_cron_time',
            side_effect=fake_last_scheduled,
        ):
            assert should_run_missed_cron() is True

        assert calls == [{'days': '5,20', 'hour': 7}]

    def test_last_scheduled_cron_time_respects_configured_days_and_hour(self):
        now = BEIJING_TZ.localize(datetime(2026, 5, 20, 10, 0))

        last = scheduler_setup._last_scheduled_cron_time(
            now=now,
            days='5,20',
            hour=7,
        )

        assert last == BEIJING_TZ.localize(datetime(2026, 5, 20, 7, 0))

    def test_last_scheduled_cron_time_skips_months_without_configured_day(self):
        """配置 31 号时, 2 月没有候选, 应回看 1 月 31 号而不是掉到 1970。"""
        now = BEIJING_TZ.localize(datetime(2026, 3, 1, 10, 0))

        last = scheduler_setup._last_scheduled_cron_time(
            now=now,
            days='31',
            hour=7,
        )

        assert last == BEIJING_TZ.localize(datetime(2026, 1, 31, 7, 0))


# ==================== schedule_research_monitor_jobs ====================

class TestScheduleResearchMonitorJobsRegisters:

    def test_cron_default_enabled_registers_main_cron(self, monkeypatch):
        """P14-v10: cron 配置走 DB · 默认 enabled=True/days='1,16'/hour=2 · 注册 cron job

        旧测试 test_cron_disabled_by_default_registers_nothing 已不再适用 ·
        P14-v10 后 cron 默认开启 (geo_research_config seed) · 必须主动 disable 才不注册.
        """
        monkeypatch.delenv('RESEARCH_MONITOR_CRON_ENABLED', raising=False)
        monkeypatch.delenv('ROLE', raising=False)
        fake_scheduler = MagicMock()
        with patch.object(
            scheduler_setup, '_load_cron_config_from_db',
            return_value={'enabled': True, 'days': '1,16', 'hour': 2},
        ), patch('api.scheduler.get_scheduler', return_value=fake_scheduler), \
             patch.object(scheduler_setup, 'should_run_missed_cron', return_value=False):
            result = schedule_research_monitor_jobs()

        assert result['registered'] is True
        assert result['main_job_id'] == JOB_ID_BIMONTHLY_ROUND
        assert fake_scheduler.add_job.call_count == 1
        first_call_kwargs = fake_scheduler.add_job.call_args_list[0].kwargs
        assert first_call_kwargs['id'] == JOB_ID_BIMONTHLY_ROUND
        assert first_call_kwargs['replace_existing'] is True
        # P14-v10: 默认 days='1,16' + hour=2 · 通过 job name 验证 (CronTrigger 字段不便直检)
        assert '1,16' in first_call_kwargs['name']
        assert '02:00' in first_call_kwargs['name']

    def test_cron_db_disabled_registers_nothing(self, monkeypatch):
        """P14-v10: DB cron_enabled=False (admin GUI 关掉) · 不注册"""
        monkeypatch.delenv('RESEARCH_MONITOR_CRON_ENABLED', raising=False)
        monkeypatch.delenv('ROLE', raising=False)
        fake_scheduler = MagicMock()
        with patch.object(
            scheduler_setup, '_load_cron_config_from_db',
            return_value={'enabled': False, 'days': '1,16', 'hour': 2},
        ), patch('api.scheduler.get_scheduler', return_value=fake_scheduler):
            result = schedule_research_monitor_jobs()

        assert result['registered'] is False
        assert result['main_job_id'] == JOB_ID_BIMONTHLY_ROUND
        assert fake_scheduler.add_job.call_count == 0

    def test_env_override_false_disables_even_when_db_enabled(self, monkeypatch):
        """P14-v10: env RESEARCH_MONITOR_CRON_ENABLED=false 是急停 override · DB true 也不注册"""
        monkeypatch.delenv('ROLE', raising=False)
        monkeypatch.setenv('RESEARCH_MONITOR_CRON_ENABLED', 'false')
        fake_scheduler = MagicMock()
        with patch.object(
            scheduler_setup, '_load_cron_config_from_db',
            return_value={'enabled': True, 'days': '1,16', 'hour': 2},
        ), patch('api.scheduler.get_scheduler', return_value=fake_scheduler):
            result = schedule_research_monitor_jobs()

        assert result['registered'] is False
        assert result.get('reason') == 'env_override_false'
        assert fake_scheduler.add_job.call_count == 0

    def test_backup_role_registers_nothing_even_when_enabled(self, monkeypatch):
        """ROLE=backup 短路 · 在 DB 读 + env 检查之前就跳过"""
        monkeypatch.setenv('ROLE', 'backup')
        # 即便 env 设了开启 · backup 仍然跳过
        monkeypatch.setenv('RESEARCH_MONITOR_CRON_ENABLED', 'true')
        with patch('api.scheduler.get_scheduler') as m_get_scheduler:
            result = schedule_research_monitor_jobs()

        assert result['registered'] is False
        assert result['skipped_reason'] == 'backup_role'
        m_get_scheduler.assert_not_called()


class TestScheduleWithMissedCronRecovery:

    def test_registers_main_and_recovery_when_enabled_and_missed(self, monkeypatch):
        monkeypatch.delenv('RESEARCH_MONITOR_CRON_ENABLED', raising=False)
        monkeypatch.delenv('ROLE', raising=False)
        monkeypatch.setenv('RESEARCH_MONITOR_ENABLE_MISSED_RECOVERY', 'true')
        fake_scheduler = MagicMock()
        with patch.object(
            scheduler_setup, '_load_cron_config_from_db',
            return_value={'enabled': True, 'days': '1,16', 'hour': 2},
        ), patch('api.scheduler.get_scheduler', return_value=fake_scheduler), \
             patch.object(scheduler_setup, 'should_run_missed_cron', return_value=True):
            result = schedule_research_monitor_jobs()

        assert result['registered'] is True
        assert result['missed_cron_recovery_scheduled'] is True
        assert fake_scheduler.add_job.call_count == 2

        ids = [c.kwargs['id'] for c in fake_scheduler.add_job.call_args_list]
        assert JOB_ID_BIMONTHLY_ROUND in ids
        assert JOB_ID_MISSED_RECOVERY in ids

    def test_recovery_check_exception_does_not_break_registration(self, monkeypatch):
        """补跑判定异常不应破坏主 cron 注册"""
        monkeypatch.delenv('RESEARCH_MONITOR_CRON_ENABLED', raising=False)
        monkeypatch.delenv('ROLE', raising=False)
        fake_scheduler = MagicMock()
        with patch.object(
            scheduler_setup, '_load_cron_config_from_db',
            return_value={'enabled': True, 'days': '1,16', 'hour': 2},
        ), patch('api.scheduler.get_scheduler', return_value=fake_scheduler), \
             patch.object(
                 scheduler_setup, 'should_run_missed_cron',
                 side_effect=RuntimeError('db down'),
             ):
            result = schedule_research_monitor_jobs()

        assert result['registered'] is True
        assert result['missed_cron_recovery_scheduled'] is False
        # 主 cron 仍然注册了
        assert fake_scheduler.add_job.call_count == 1


class TestAutomaticRoundCircuitBreaker:
    def test_circuit_removes_only_research_auto_jobs(self):
        scheduler = MagicMock()
        with patch('api.scheduler.get_scheduler', return_value=scheduler):
            scheduler_setup._remove_auto_round_jobs_best_effort()
        assert [item.args for item in scheduler.remove_job.call_args_list] == [
            (scheduler_setup.JOB_ID_BIMONTHLY_ROUND,),
            (scheduler_setup.JOB_ID_MISSED_RECOVERY,),
        ]

    def test_config_read_failure_is_fail_closed(self):
        with patch.object(
            scheduler_setup, 'get_connection', side_effect=RuntimeError('db unavailable')
        ):
            cfg = scheduler_setup._load_cron_config_from_db()
        assert cfg['enabled'] is False
        assert cfg['available'] is False

    def test_preflight_db_failure_does_not_start_or_mutate_gate(self):
        with patch.object(
            scheduler_setup, 'get_connection', side_effect=RuntimeError('db unavailable')
        ), patch.object(scheduler_setup, 'get_active_plan') as get_plan, patch.object(
            scheduler_setup, '_trip_auto_round_circuit'
        ) as trip:
            scheduler_setup.trigger_research_round_sync()
        get_plan.assert_not_called()
        trip.assert_not_called()

    def test_preflight_previous_failed_auto_round_opens_circuit(self):
        conn = MagicMock()
        cur = conn.cursor.return_value
        rearmed_at = datetime(2026, 7, 18, tzinfo=BEIJING_TZ)
        cur.fetchone.side_effect = [
            {'value_json': True, 'updated_at': rearmed_at},
            {'round_id': 'round_failed', 'status': 'failed_resumable',
             'reason': 'stage3_timeout'},
        ]
        with patch.object(scheduler_setup, 'get_connection', return_value=conn), \
             patch.object(scheduler_setup, '_trip_auto_round_circuit') as trip:
            assert scheduler_setup._preflight_auto_round_allowed() is None
        trip.assert_called_once_with(
            round_id='round_failed', status='failed_resumable', reason='stage3_timeout',
            expected_gate_updated_at=rearmed_at,
        )

    def test_preflight_success_returns_rearm_token(self):
        conn = MagicMock()
        cur = conn.cursor.return_value
        rearmed_at = datetime(2026, 7, 18, tzinfo=BEIJING_TZ)
        cur.fetchone.side_effect = [
            {'value_json': True, 'updated_at': rearmed_at},
            None,
        ]
        with patch.object(scheduler_setup, 'get_connection', return_value=conn):
            assert scheduler_setup._preflight_auto_round_allowed() == rearmed_at

    def test_stale_round_cannot_override_new_admin_gate_decision(self):
        conn = MagicMock()
        cur = conn.cursor.return_value
        old_token = datetime(2026, 7, 18, 2, tzinfo=BEIJING_TZ)
        new_token = datetime(2026, 7, 18, 3, tzinfo=BEIJING_TZ)
        cur.fetchone.return_value = {'value_json': True, 'updated_at': new_token}
        with patch.object(scheduler_setup, 'get_connection', return_value=conn), \
             patch(
                 'services.notification_outbox.enqueue_admin_notification_events'
             ) as enqueue, patch.object(
                 scheduler_setup, '_remove_auto_round_jobs_best_effort'
             ) as remove_jobs:
            result = scheduler_setup._trip_auto_round_circuit(
                round_id='round_old',
                status='failed',
                reason='old run failed after admin rearmed',
                expected_gate_updated_at=old_token,
            )

        assert result['stale_gate'] is True
        assert result['paused'] is False
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        enqueue.assert_not_called()
        remove_jobs.assert_not_called()

    def test_trip_atomically_disables_gate_and_enqueues_admin_alert(self):
        conn = MagicMock()
        cur = conn.cursor.return_value
        cur.fetchone.side_effect = [{'value_json': True}, {'key': 'cron_enabled'}]
        with patch.object(scheduler_setup, 'get_connection', return_value=conn), \
             patch(
                 'services.notification_outbox.enqueue_admin_notification_events'
             ) as enqueue, patch.object(
                 scheduler_setup, '_remove_auto_round_jobs_best_effort'
             ) as remove_jobs:
            result = scheduler_setup._trip_auto_round_circuit(
                round_id='round_failed',
                status='failed_resumable',
                reason='stage3_timeout',
            )

        assert result['paused'] is True
        assert result['changed'] is True
        conn.commit.assert_called_once()
        enqueue.assert_called_once()
        assert enqueue.call_args.kwargs['business_id'] == 'auto-circuit:round_failed'
        assert enqueue.call_args.kwargs['terminal_state'] == 'paused:failed_resumable'
        remove_jobs.assert_called_once()

    def test_disabled_gate_never_reads_plan_or_starts_round(self):
        with patch.object(
            scheduler_setup, '_preflight_auto_round_allowed', return_value=None
        ), patch.object(scheduler_setup, 'get_active_plan') as get_plan:
            scheduler_setup.trigger_research_round_sync()
        get_plan.assert_not_called()

    def test_failed_auto_round_pauses_next_trigger_and_alerts(self):
        gate_token = datetime(2026, 7, 18, tzinfo=BEIJING_TZ)
        plan = {
            'industries': [{'id': 1, 'name': '测试', 'slug': 'test'}],
            'prompts_by_industry': {1: [{'id': 11, 'prompt_text': '测试问题'}]},
        }
        with patch.object(
            scheduler_setup, '_preflight_auto_round_allowed', return_value=gate_token
        ), patch.object(
            scheduler_setup, 'get_active_plan', return_value=plan
        ), patch(
            'services.research_monitor.round_state.create_round_with_snapshot',
            return_value='round_failed',
        ), patch(
            'services.research_monitor.round_state.get_round_status',
            return_value={
                'status': 'failed_resumable',
                'summary_json': {'reason': 'round_budget_exhausted'},
            },
        ), patch(
            'services.research_monitor.round_runner.run_round_with_auto_resume',
            new=AsyncMock(return_value='failed_resumable'),
        ), patch.object(scheduler_setup, '_trip_auto_round_circuit') as trip:
            scheduler_setup.trigger_research_round_sync()

        trip.assert_called_once_with(
            round_id='round_failed',
            status='failed_resumable',
            reason='round_budget_exhausted',
            expected_gate_updated_at=gate_token,
        )

    def test_successful_auto_round_does_not_open_circuit(self):
        gate_token = datetime(2026, 7, 18, tzinfo=BEIJING_TZ)
        plan = {
            'industries': [{'id': 1, 'name': '测试', 'slug': 'test'}],
            'prompts_by_industry': {1: [{'id': 11, 'prompt_text': '测试问题'}]},
        }
        with patch.object(
            scheduler_setup, '_preflight_auto_round_allowed', return_value=gate_token
        ), patch.object(
            scheduler_setup, 'get_active_plan', return_value=plan
        ), patch(
            'services.research_monitor.round_state.create_round_with_snapshot',
            return_value='round_ok',
        ), patch(
            'services.research_monitor.round_runner.run_round_with_auto_resume',
            new=AsyncMock(return_value='completed'),
        ), patch.object(scheduler_setup, '_trip_auto_round_circuit') as trip:
            scheduler_setup.trigger_research_round_sync()

        trip.assert_not_called()
