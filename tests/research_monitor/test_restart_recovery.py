"""
restart_recovery 僵尸 round sweep 单测(纯 mock · 不连真 PG)

验证:
1. heartbeat_stale: 心跳超 10 分钟标死
2. hard_timeout: 总时长超 6 小时标死(即便心跳还在)
3. 空集合: 没有僵尸正常返
4. mark_zombie_failed_resumable: SQL 含 'failed_resumable' + rowcount=1 → True
5. sweep happy path: 2 zombie 全标
6. sweep DB 异常: 不抛 · 返 error 字段
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ========================================================================
# helpers · 构造 mock connection / cursor
# ========================================================================

def _make_mock_conn(fetchall_rows=None, rowcount=1):
    """build a mock conn with cursor returning given fetchall rows."""
    cur = MagicMock()
    cur.fetchall.return_value = fetchall_rows or []
    cur.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.close = MagicMock()
    conn.commit = MagicMock()
    return conn, cur


# ========================================================================
# TestFindZombieRoundsHeartbeatStale
# ========================================================================

class TestFindZombieRoundsHeartbeatStale:

    def test_heartbeat_stale_15_minutes_marked_zombie(self):
        """心跳 15 分钟前 · 总时长 1 小时 · 应判 heartbeat_stale"""
        from services.research_monitor import restart_recovery

        now = datetime.now(timezone.utc)
        rows = [{
            'round_id': 'round_test_hb_stale',
            'status': 'running',
            'current_stage': 'stage_3_collect',
            'started_at': now - timedelta(hours=1),
            'last_heartbeat_at': now - timedelta(minutes=15),
            'heartbeat_age': timedelta(minutes=15),
            'run_age': timedelta(hours=1),
        }]
        conn, cur = _make_mock_conn(fetchall_rows=rows)

        with patch.object(restart_recovery, 'get_connection', return_value=conn):
            zombies = restart_recovery.find_zombie_rounds()

        assert len(zombies) == 1
        z = zombies[0]
        assert z['round_id'] == 'round_test_hb_stale'
        assert 'heartbeat' in z['reason'].lower()
        assert z['current_stage'] == 'stage_3_collect'

        # 验证 SQL 参数化(传了 HEARTBEAT_STALE_MINUTES + ROUND_HARD_TIMEOUT_HOURS)
        called_args = cur.execute.call_args
        assert called_args is not None
        sql = called_args[0][0]
        assert 'status = %s' not in sql  # 用字面量列表 · 不参数化 status
        # P14-v15 (review HIGH#6): find_zombie_rounds 已扩到 IN ('running', 'pending')
        # 老 "status = 'running'" 单字面量已不再适用
        assert "status IN ('running', 'pending')" in sql
        assert 'last_heartbeat_at' in sql
        assert 'started_at' in sql


# ========================================================================
# TestFindZombieRoundsHardTimeout
# ========================================================================

class TestFindZombieRoundsHardTimeout:

    def test_hard_timeout_7_hours_marked_zombie(self):
        """started_at 7 小时前 + 心跳 9 分钟前(还活)· 应判 hard_timeout"""
        from services.research_monitor import restart_recovery

        now = datetime.now(timezone.utc)
        rows = [{
            'round_id': 'round_test_hard_to',
            'status': 'running',
            'current_stage': 'stage_5_score',
            'started_at': now - timedelta(hours=7),
            'last_heartbeat_at': now - timedelta(minutes=9),
            'heartbeat_age': timedelta(minutes=9),
            'run_age': timedelta(hours=7),
        }]
        conn, _ = _make_mock_conn(fetchall_rows=rows)

        with patch.object(restart_recovery, 'get_connection', return_value=conn):
            zombies = restart_recovery.find_zombie_rounds()

        assert len(zombies) == 1
        z = zombies[0]
        assert z['round_id'] == 'round_test_hard_to'
        # 总时长超时优先于心跳(更严重)
        reason_lower = z['reason'].lower()
        assert 'hard_timeout' in reason_lower or '总时长' in z['reason']


# ========================================================================
# TestFindZombieRoundsEmpty
# ========================================================================

class TestFindZombieRoundsEmpty:

    def test_no_zombie_returns_empty_list(self):
        """fetchall 返空 · 应返 []"""
        from services.research_monitor import restart_recovery

        conn, _ = _make_mock_conn(fetchall_rows=[])

        with patch.object(restart_recovery, 'get_connection', return_value=conn):
            zombies = restart_recovery.find_zombie_rounds()

        assert zombies == []


# ========================================================================
# TestMarkZombieFailedResumable
# ========================================================================

class TestMarkZombieFailedResumable:

    def test_mark_failed_resumable_returns_true_when_rowcount_1(self):
        """rowcount=1 · 返 True · SQL 含 'failed_resumable'"""
        from services.research_monitor import restart_recovery

        conn, cur = _make_mock_conn(rowcount=1)

        with patch.object(restart_recovery, 'get_connection', return_value=conn):
            ok = restart_recovery.mark_zombie_failed_resumable(
                round_id='round_xxx',
                reason='heartbeat_stale: 心跳超过 10 分钟未更新',
                last_stage='stage_4_clean',
            )

        assert ok is True
        # 验证 SQL 含 failed_resumable + jsonb_build_object
        sql = cur.execute.call_args[0][0]
        assert 'failed_resumable' in sql
        assert 'jsonb_build_object' in sql
        # 验证 SQL 参数化(reason / last_stage / detected_at / round_id 4 个 %s)
        params = cur.execute.call_args[0][1]
        assert params[0] == 'heartbeat_stale: 心跳超过 10 分钟未更新'
        assert params[1] == 'stage_4_clean'
        assert params[3] == 'round_xxx'
        # 验证防并发条件 (C9 post-review fix · UPDATE WHERE 必须扩到 pending · 跟 find 对齐)
        # 旧 status = 'running' 单字面量会让 pending 僵尸被检测但 UPDATE 0 行
        assert "status IN ('running', 'pending')" in sql
        # commit 调过
        conn.commit.assert_called_once()

    def test_mark_returns_false_when_rowcount_0(self):
        """rowcount=0(被并发改过)· 返 False"""
        from services.research_monitor import restart_recovery

        conn, _ = _make_mock_conn(rowcount=0)

        with patch.object(restart_recovery, 'get_connection', return_value=conn):
            ok = restart_recovery.mark_zombie_failed_resumable(
                'round_yyy', 'hard_timeout', 'stage_2'
            )

        assert ok is False


# ========================================================================
# TestSweepZombieRoundsHappy
# ========================================================================

class TestSweepZombieRoundsHappy:

    def test_sweep_2_zombies_all_marked(self):
        """patch 2 个 zombie 全标成功 · detected=2 marked=2"""
        from services.research_monitor import restart_recovery

        fake_zombies = [
            {
                'round_id': 'r1',
                'reason': 'heartbeat_stale: 心跳超时',
                'current_stage': 'stage_3',
                'status': 'running',
                'started_at': None,
                'last_heartbeat_at': None,
            },
            {
                'round_id': 'r2',
                'reason': 'hard_timeout: 总时长超时',
                'current_stage': 'stage_5',
                'status': 'running',
                'started_at': None,
                'last_heartbeat_at': None,
            },
        ]

        with patch.object(restart_recovery, 'find_zombie_rounds', return_value=fake_zombies), \
             patch.object(restart_recovery, 'mark_zombie_failed_resumable', return_value=True) as mock_mark:
            result = restart_recovery.sweep_zombie_rounds()

        assert result['detected'] == 2
        assert result['marked'] == 2
        assert len(result['rounds']) == 2
        assert result['rounds'][0]['round_id'] == 'r1'
        assert result['rounds'][0]['marked_ok'] is True
        assert result['rounds'][1]['round_id'] == 'r2'
        assert result['rounds'][1]['marked_ok'] is True
        # mark 被调 2 次
        assert mock_mark.call_count == 2

    def test_sweep_partial_mark_some_already_changed(self):
        """1 标成功 + 1 被并发改过(rowcount=0 → False)· detected=2 marked=1"""
        from services.research_monitor import restart_recovery

        fake_zombies = [
            {'round_id': 'r1', 'reason': 'heartbeat_stale', 'current_stage': 'stage_2'},
            {'round_id': 'r2', 'reason': 'hard_timeout', 'current_stage': 'stage_4'},
        ]

        with patch.object(restart_recovery, 'find_zombie_rounds', return_value=fake_zombies), \
             patch.object(restart_recovery, 'mark_zombie_failed_resumable', side_effect=[True, False]):
            result = restart_recovery.sweep_zombie_rounds()

        assert result['detected'] == 2
        assert result['marked'] == 1


# ========================================================================
# TestSweepZombieRoundsDbError
# ========================================================================

class TestSweepZombieRoundsDbError:

    def test_sweep_db_error_does_not_raise(self):
        """find_zombie_rounds 抛 RuntimeError · sweep 不抛 · 返 error 字段"""
        from services.research_monitor import restart_recovery

        with patch.object(
            restart_recovery,
            'find_zombie_rounds',
            side_effect=RuntimeError('DB connection refused'),
        ):
            result = restart_recovery.sweep_zombie_rounds()

        assert result['detected'] == 0
        assert result['marked'] == 0
        assert 'error' in result
        assert 'RuntimeError' in result['error']
        assert 'DB connection refused' in result['error']

    def test_sweep_mark_exception_continues_other_rows(self):
        """单行 mark 抛异常 · 其他行继续处理 · 不阻断"""
        from services.research_monitor import restart_recovery

        fake_zombies = [
            {'round_id': 'r1', 'reason': 'heartbeat_stale', 'current_stage': 'stage_1'},
            {'round_id': 'r2', 'reason': 'hard_timeout', 'current_stage': 'stage_2'},
            {'round_id': 'r3', 'reason': 'heartbeat_stale', 'current_stage': 'stage_3'},
        ]

        # r2 抛异常
        def mark_side_effect(round_id, reason, last_stage):
            if round_id == 'r2':
                raise RuntimeError('UPDATE failed')
            return True

        with patch.object(restart_recovery, 'find_zombie_rounds', return_value=fake_zombies), \
             patch.object(restart_recovery, 'mark_zombie_failed_resumable', side_effect=mark_side_effect):
            result = restart_recovery.sweep_zombie_rounds()

        assert result['detected'] == 3
        assert result['marked'] == 2  # r1 + r3 标成功 · r2 异常跳过
        assert len(result['rounds']) == 3
        # r2 marked_ok=False
        r2 = next(r for r in result['rounds'] if r['round_id'] == 'r2')
        assert r2['marked_ok'] is False
