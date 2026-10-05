"""
提并发 + 4h 超时自动续跑批 判别测试
(CODEX_SPEC_RESEARCH_ROUND_CONCURRENCY_AUTORESUME_2026-07-16 §4 · 禁假绿)

分组:
- 组 1  并发结构判别: 纯 mock 驱动【真实 stage1_ai_fetch_all】(禁手写等价逻辑),
        用在飞计数器做确定性判别(旧组间串行模型全局在飞物理上限=4, >4 即真并发铁证),
        elapsed 只做宽松 sanity。
- 组 2  熔断在并发下判别生效: mock 连续失败, 断言累计口径触发 + 停发远早于总量。
- 组 3  超时自动续跑判别: 真 PG 驱动【真实 run_round + run_round_with_auto_resume +
        真实 round_state(含 advisory lock)】, 只 mock _run_pipeline 耗时与
        ROUND_TIMEOUT_SECONDS。断言自动重入/断点续/计数递增/达上限停。
- 组 4  预算熔断不被绕过: BudgetExhaustedError → failed_resumable 且无自动重入。
- 组 5  防重叠: 另有活跃轮时自动续跑放弃, 无第二个 running。
- 附   config 开关/上限边界 · seed 迁移幂等 2× · attempts 真实化(CAN-006 消费侧) ·
        infer_resume_from_stage 下沉后行为锁定。
"""
import asyncio
import time
from unittest.mock import patch, MagicMock

import pytest

from services.research_monitor import round_runner
from services.research_monitor.budget_guard import BudgetExhaustedError
from services.research_monitor.round_state import (
    create_round_with_snapshot,
    update_round_progress,
    get_round_status,
    infer_resume_from_stage,
)


# ==================== 组 1/2 共用: 内存版 DB 假件 ====================

def _new_state():
    return {
        'next_id': 0,
        'insert_calls': [],      # INSERT round_call 参数
        'success_updates': [],   # UPDATE ... status='success' 参数
        'failed_updates': [],    # UPDATE ... status='failed' 参数
    }


class _FakeCursor:
    def __init__(self, state):
        self.state = state
        self._fetch = None

    def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        if 'INSERT INTO geo_research_round_call' in s:
            self.state['insert_calls'].append(params)
            self.state['next_id'] += 1
            self._fetch = {'id': self.state['next_id']}
        elif 'SELECT 1 FROM geo_research_round_call' in s:
            self._fetch = None  # CAN-014: 无已成功记录 → 守卫不触发
        elif 'UPDATE geo_research_round_call' in s and "'success'" in s:
            self.state['success_updates'].append(params)
        elif 'UPDATE geo_research_round_call' in s and "'failed'" in s:
            self.state['failed_updates'].append(params)
        # 其余 (cost_log 汇总 SELECT / config SELECT) → fetchall 空

    def fetchone(self):
        return self._fetch

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _cfg_patch(overrides=None):
    """把 round_runner 的运行时配置读取钉死为默认值(或指定覆盖), 与测试 DB 解耦。"""
    o = dict(overrides or {})

    def read(key, default, cast):
        return o.get(key, default)

    return patch.object(round_runner, '_read_config_value', side_effect=read)


def _make_tracking_fetcher(platform, track, sleep_seconds):
    """假平台 fetcher: 维护全局/每平台在飞计数器(判并发结构的确定性证据)。"""
    async def fetcher(prompt_id, prompt):
        track['global'] += 1
        track['per'][platform] = track['per'].get(platform, 0) + 1
        track['max_global'] = max(track['max_global'], track['global'])
        track['max_per'][platform] = max(
            track['max_per'].get(platform, 0), track['per'][platform]
        )
        try:
            await asyncio.sleep(sleep_seconds)
        finally:
            track['global'] -= 1
            track['per'][platform] -= 1
        return {'answer': 'a', 'citations': []}
    return fetcher


def _stage1_patches(state, fetchers, cfg_overrides=None):
    """组 1/2 通用补丁组: 假 DB + 假平台 + 钉死配置 + 假 round 状态(running)。"""
    return [
        patch.object(round_runner, 'PLATFORM_FETCHERS', fetchers),
        patch.object(round_runner, 'get_connection',
                     side_effect=lambda: _FakeConn(state)),
        patch.object(round_runner, 'update_heartbeat', MagicMock()),
        patch.object(round_runner, 'update_round_progress', MagicMock()),
        patch.object(round_runner, 'get_round_status',
                     return_value={'status': 'running'}),
        _cfg_patch(cfg_overrides),
    ]


# ==================== 组 1 · 并发结构判别 ====================

class TestStage1InterGroupConcurrency:

    def test_true_inter_group_concurrency_default_8(self, monkeypatch):
        """旧组间串行模型全局在飞上限=4(每 prompt 一组 await gather(4 平台));
        新模型 max_global 必须 >4。测试驱动真实 stage1_ai_fetch_all。"""
        monkeypatch.delenv('RESEARCH_FETCH_CONCURRENCY', raising=False)
        state = _new_state()
        track = {'global': 0, 'max_global': 0, 'per': {}, 'max_per': {}}
        sleep = 0.05
        fetchers = {
            f'p{i}': _make_tracking_fetcher(f'p{i}', track, sleep) for i in range(4)
        }
        plan = [
            {'industry_id': 1, 'industry_name': '测试行业',
             'prompt_id': i, 'prompt_text': f'q{i}'}
            for i in range(12)
        ]

        patches = _stage1_patches(state, fetchers)
        t0 = time.monotonic()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            asyncio.run(round_runner.stage1_ai_fetch_all('round_conc_t1', plan))
        elapsed = time.monotonic() - t0

        # 调用总量一个不多一个不少 (SPEC 1.4)
        assert len(state['insert_calls']) == 12 * 4
        assert len(state['success_updates']) == 12 * 4
        assert len(state['failed_updates']) == 0
        # 组间真并发铁证: 全局在飞 > 4 (旧串行模型物理上限 = 4)
        assert track['max_global'] > 4, (
            f"max_global={track['max_global']} ≤ 4 → 仍是组间串行"
        )
        # 每平台并发被 Semaphore(8) 收束
        for p in fetchers:
            assert track['max_per'].get(p, 0) <= 8
        # 宽松 elapsed sanity(判别主力是上面的 max_global; 此处只挡"接近串行下界",
        # 阈值放 0.9 容忍慢 CI — 真串行必然 ≥ 12×sleep, 真并发实测 ≈0.15s)
        assert elapsed < 12 * sleep * 0.9, (
            f"elapsed={elapsed:.3f}s 接近串行下界 {12 * sleep}s"
        )

    def test_semaphore_bound_is_effective(self, monkeypatch):
        """并发不是无界: 配置 2/平台时, 每平台在飞 ≤2 且耗时 ≥ ceil(N/2)×sleep。"""
        monkeypatch.delenv('RESEARCH_FETCH_CONCURRENCY', raising=False)
        state = _new_state()
        track = {'global': 0, 'max_global': 0, 'per': {}, 'max_per': {}}
        sleep = 0.05
        fetchers = {
            f'p{i}': _make_tracking_fetcher(f'p{i}', track, sleep) for i in range(4)
        }
        plan = [
            {'industry_id': 1, 'industry_name': '测试行业',
             'prompt_id': i, 'prompt_text': f'q{i}'}
            for i in range(6)
        ]

        patches = _stage1_patches(
            state, fetchers, {'fetch_concurrency_per_platform': 2}
        )
        t0 = time.monotonic()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            asyncio.run(round_runner.stage1_ai_fetch_all('round_conc_t2', plan))
        elapsed = time.monotonic() - t0

        assert len(state['success_updates']) == 6 * 4
        for p in fetchers:
            assert track['max_per'].get(p, 0) <= 2, (
                f"{p} 在飞 {track['max_per'].get(p)} > 配置 2 → semaphore 未生效"
            )
        # 每平台 6 条 / 2 并发 = 3 批 × 0.05s = 0.15s 下界(留裕量断 0.12)
        assert elapsed >= 0.12, f"elapsed={elapsed:.3f}s < 并发上界收束下界"

    def test_attempts_written_from_real_retry_count(self, monkeypatch):
        """[GEO-R6-CAN-006 消费侧判别] 成功 UPDATE 的 attempts 参数 = 真实尝试次数
        (真 query_with_retry 首试成功 → 1), 不再是旧版硬编码 3。"""
        monkeypatch.delenv('RESEARCH_FETCH_CONCURRENCY', raising=False)
        state = _new_state()
        track = {'global': 0, 'max_global': 0, 'per': {}, 'max_per': {}}
        fetchers = {'p0': _make_tracking_fetcher('p0', track, 0)}
        plan = [{'industry_id': 1, 'industry_name': '测试行业',
                 'prompt_id': 1, 'prompt_text': 'q'}]

        patches = _stage1_patches(state, fetchers)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            asyncio.run(round_runner.stage1_ai_fetch_all('round_conc_t3', plan))

        assert len(state['success_updates']) == 1
        # UPDATE 参数 = (citations_count, attempts, call_id)
        _, attempts, _ = state['success_updates'][0]
        assert attempts == 1, f"attempts={attempts}, 期望真实次数 1 (旧硬编码为 3)"


# ==================== 组 2 · 熔断在并发下判别生效 ====================

class TestStage1CircuitBreakerUnderConcurrency:

    def test_breaker_trips_and_stops_dispatch(self, monkeypatch):
        monkeypatch.delenv('RESEARCH_FETCH_CONCURRENCY', raising=False)
        state = _new_state()
        fetchers = {f'p{i}': MagicMock() for i in range(4)}
        plan = [
            {'industry_id': 1, 'industry_name': '测试行业',
             'prompt_id': i, 'prompt_text': f'q{i}'}
            for i in range(50)
        ]
        total = 50 * 4

        async def failing_retry(fetcher, prompt_id, prompt, max_retries=3):
            raise RuntimeError('provider down')

        progress_mock = MagicMock()
        overrides = {
            'circuit_breaker_consecutive': 10,
            'circuit_breaker_min_processed': 10 ** 9,  # 关掉 rate 口径, 只测 consecutive
            'fetch_concurrency_per_platform': 4,
        }
        with patch.object(round_runner, 'PLATFORM_FETCHERS', fetchers), \
             patch.object(round_runner, 'get_connection',
                          side_effect=lambda: _FakeConn(state)), \
             patch.object(round_runner, 'query_with_retry',
                          side_effect=failing_retry), \
             patch.object(round_runner, 'update_heartbeat', MagicMock()), \
             patch.object(round_runner, 'update_round_progress', progress_mock), \
             patch.object(round_runner, 'get_round_status',
                          return_value={'status': 'running'}), \
             _cfg_patch(overrides):
            # 熔断是软停(与旧版一致): stage1 正常返回不抛, pipeline 继续
            asyncio.run(round_runner.stage1_ai_fetch_all('round_brk', plan))

        # 累计口径触发: 至少打满 consecutive 阈值
        assert len(state['failed_updates']) >= 10
        # 停发生效: 已派发调用远小于总量 (阈值 10 + 在飞窗口 4×4, 留裕量断 ≤60)
        assert len(state['insert_calls']) <= 60, (
            f"派发 {len(state['insert_calls'])}/{total} → 熔断未停发"
        )
        assert len(state['insert_calls']) < total
        # stage 收尾进度上报 breaker_tripped=True
        done_calls = [
            c for c in progress_mock.call_args_list
            if (c.kwargs.get('stage') or (c.args[1] if len(c.args) > 1 else '')) == 'stage_1_ai_fetch_done'
        ]
        assert done_calls, "未找到 stage_1_ai_fetch_done 进度上报"
        progress = done_calls[-1].kwargs.get('progress') or {}
        assert progress.get('breaker_tripped') is True


# ==================== 组 3/4/5 共用: 真 PG + 假 pipeline ====================

def _mk_round(triggered_by='manual'):
    return create_round_with_snapshot(
        triggered_by,
        industries=[{'id': 1, 'name': '测试行业', 'slug': 'test-ind'}],
        prompts_by_industry={1: [{'id': 1, 'text': '测试 prompt'}]},
    )


_SNAPSHOT = {
    'industries': [{'id': 1, 'name': '测试行业', 'slug': 'test-ind'}],
    'prompts_by_industry': {'1': [{'id': 1, 'text': '测试 prompt'}]},
}


# ==================== 组 3 · 超时自动续跑判别 ====================

@pytest.mark.usefixtures('clean_research_tables')
class TestAutoResumeOnTimeout:

    def test_auto_resume_until_max_then_stop(self):
        """极小 ROUND_TIMEOUT_SECONDS 注入 → 断言:
        自动重入 3 次(默认上限) / 从断点续(resume_from_stage 取中断 stage, 不回 stage_1
        从头全跑) / auto_resume_count 递增 / 达上限后不再重入保持 failed_resumable。"""
        round_id = _mk_round()
        # 模拟上次跑到 stage_3 中断: 自动续跑必须从 stage_3 续(断点不回零)
        update_round_progress(round_id, stage='stage_3_crawl',
                              progress={'processed': 100, 'total': 1636})

        pipeline_calls = []

        async def slow_pipeline(rid, snapshot, resume_from_stage):
            pipeline_calls.append(resume_from_stage)
            await asyncio.sleep(0.5)  # > 注入的 0.05s 超时 → 每次都超时

        with patch.object(round_runner, '_run_pipeline',
                          side_effect=slow_pipeline), \
             patch.object(round_runner, 'ROUND_TIMEOUT_SECONDS', 0.05), \
             _cfg_patch({'round_auto_resume_enabled': True}):
            final = asyncio.run(
                round_runner.run_round_with_auto_resume(round_id, _SNAPSHOT)
            )

        assert final == 'failed_resumable'
        # 首跑 + 3 次自动续跑 = 4; 第 4 次超时后 count==max → 不再重入
        assert len(pipeline_calls) == 4, f"pipeline_calls={pipeline_calls}"
        assert pipeline_calls[0] is None
        # 断点续判别: 第一次自动续跑从中断的 stage_3 续, 不是 stage_1 从头
        assert pipeline_calls[1] == 'stage_3'

        st = get_round_status(round_id)
        assert st['status'] == 'failed_resumable'
        summary = st['summary_json'] or {}
        assert summary.get('reason') == '4h_hard_timeout'
        assert summary.get('auto_resume_count') == 3
        history = summary.get('auto_resume_history') or []
        assert len(history) == 3
        assert [h['n'] for h in history] == [1, 2, 3]
        # 第一次续跑记录的断点前进度 = 中断时进度(processed 不回零的观测面)
        assert history[0]['progress_before']['processed'] == 100
        assert history[0]['resume_from_stage'] == 'stage_3'

    def test_auto_resume_disabled_by_config(self):
        round_id = _mk_round()
        pipeline_calls = []

        async def slow_pipeline(rid, snapshot, resume_from_stage):
            pipeline_calls.append(resume_from_stage)
            await asyncio.sleep(0.5)

        with patch.object(round_runner, '_run_pipeline',
                          side_effect=slow_pipeline), \
             patch.object(round_runner, 'ROUND_TIMEOUT_SECONDS', 0.05), \
             _cfg_patch({'round_auto_resume_enabled': False}):
            final = asyncio.run(
                round_runner.run_round_with_auto_resume(round_id, _SNAPSHOT)
            )

        assert final == 'failed_resumable'
        assert len(pipeline_calls) == 1  # 开关关 → 零自动重入
        summary = (get_round_status(round_id) or {}).get('summary_json') or {}
        assert 'auto_resume_count' not in summary

    def test_auto_resume_missing_config_defaults_off(self):
        """恢复库/新环境漏 seed 时也必须 fail-closed，不能自动开闸。"""
        round_id = _mk_round()
        pipeline_calls = []

        async def slow_pipeline(rid, snapshot, resume_from_stage):
            pipeline_calls.append(resume_from_stage)
            await asyncio.sleep(0.5)

        with patch.object(round_runner, '_run_pipeline',
                          side_effect=slow_pipeline), \
             patch.object(round_runner, 'ROUND_TIMEOUT_SECONDS', 0.05), \
             _cfg_patch():
            final = asyncio.run(
                round_runner.run_round_with_auto_resume(round_id, _SNAPSHOT)
            )

        assert final == 'failed_resumable'
        assert len(pipeline_calls) == 1
        summary = (get_round_status(round_id) or {}).get('summary_json') or {}
        assert 'auto_resume_count' not in summary

    def test_auto_resume_max_zero_means_off(self):
        round_id = _mk_round()
        pipeline_calls = []

        async def slow_pipeline(rid, snapshot, resume_from_stage):
            pipeline_calls.append(resume_from_stage)
            await asyncio.sleep(0.5)

        with patch.object(round_runner, '_run_pipeline',
                          side_effect=slow_pipeline), \
             patch.object(round_runner, 'ROUND_TIMEOUT_SECONDS', 0.05), \
             _cfg_patch({
                 'round_auto_resume_enabled': True,
                 'round_auto_resume_max': 0,
             }):
            final = asyncio.run(
                round_runner.run_round_with_auto_resume(round_id, _SNAPSHOT)
            )

        assert final == 'failed_resumable'
        assert len(pipeline_calls) == 1


# ==================== 组 4 · 预算熔断不被绕过 ====================

@pytest.mark.usefixtures('clean_research_tables')
class TestBudgetExhaustedNotAutoResumed:

    def test_budget_exhausted_no_reentry(self):
        round_id = _mk_round()
        pipeline_calls = []

        async def budget_pipeline(rid, snapshot, resume_from_stage):
            pipeline_calls.append(resume_from_stage)
            raise BudgetExhaustedError('round', 350.0, 350.0, '单轮预算超支')

        with patch.object(round_runner, '_run_pipeline',
                          side_effect=budget_pipeline), \
             _cfg_patch({'round_auto_resume_enabled': True}):
            final = asyncio.run(
                round_runner.run_round_with_auto_resume(round_id, _SNAPSHOT)
            )

        assert final == 'failed_resumable'
        assert len(pipeline_calls) == 1, "预算熔断被自动续跑绕过了!"
        st = get_round_status(round_id)
        summary = st['summary_json'] or {}
        assert summary.get('reason') == 'round_budget_exhausted'
        assert 'auto_resume_count' not in summary
        assert st['status'] == 'failed_resumable'


# ==================== 组 5 · 防重叠 ====================

@pytest.mark.usefixtures('clean_research_tables')
class TestAutoResumeNoOverlap:

    def test_abandon_when_another_round_active(self, pg_conn):
        """超时轮想自动续跑时已有另一 running 轮 → 放弃(不排队不重试),
        断言无第二个 running。驱动真实 mark_round_resume_requested(真 advisory
        lock + 单活跃轮守卫)。"""
        round_a = _mk_round()
        round_b = _mk_round()
        # B 推成 running (update_round_progress 自动 pending→running)
        update_round_progress(round_b, stage='stage_1_ai_fetch', progress={})

        pipeline_calls = []

        async def slow_pipeline(rid, snapshot, resume_from_stage):
            pipeline_calls.append(resume_from_stage)
            await asyncio.sleep(0.5)

        with patch.object(round_runner, '_run_pipeline',
                          side_effect=slow_pipeline), \
             patch.object(round_runner, 'ROUND_TIMEOUT_SECONDS', 0.05), \
             _cfg_patch({'round_auto_resume_enabled': True}):
            final = asyncio.run(
                round_runner.run_round_with_auto_resume(round_a, _SNAPSHOT)
            )

        assert final == 'failed_resumable'
        assert len(pipeline_calls) == 1  # 抢占被 B 挡下 → 放弃, 无重入

        st_a = get_round_status(round_a)
        assert st_a['status'] == 'failed_resumable'
        assert 'auto_resume_count' not in (st_a['summary_json'] or {})
        st_b = get_round_status(round_b)
        assert st_b['status'] == 'running'

        # 无第二个 running/pending(防重叠核心断言)
        cur = pg_conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM geo_research_round "
            "WHERE status IN ('pending', 'running')"
        )
        assert cur.fetchone()['n'] == 1


# ==================== 附 · seed 迁移幂等 + config 端到端 ====================


def test_research_auto_resume_seed_is_in_prestart_manifest_after_base_schema():
    """恢复库/新环境必须先建基础表和自助 schema，再 seed 灰度开关。"""
    from db.migration_manifest import MIGRATIONS

    base = 'scripts/migration_geo_research_monitor.sql'
    selfserve = 'scripts/migration_geo_research_selfserve_2026_07_05.sql'
    seed = 'scripts/migration_research_round_concurrency_autoresume_2026_07_16.sql'
    assert base in MIGRATIONS
    assert selfserve in MIGRATIONS
    assert seed in MIGRATIONS
    assert MIGRATIONS.index(base) < MIGRATIONS.index(selfserve) < MIGRATIONS.index(seed)


@pytest.mark.usefixtures('clean_research_tables')
class TestConfigSeedMigration:

    def test_seed_idempotent_and_readable(self, pg_conn):
        """seed SQL 跑 2 次无错(幂等); 三键落库后 round_runner 真实读取路径
        (_get_int_config/_get_bool_config → 真 get_connection)取到正确类型值。

        隔离: config 表不在 clean fixture 清单(有默认配置不能清), 本测试只
        DELETE 自己的三键再 seed, 消除跨 session 持久值干扰(DO NOTHING 保旧值)。"""
        with open(
            'scripts/migration_research_round_concurrency_autoresume_2026_07_16.sql',
            'r', encoding='utf-8',
        ) as f:
            seed_sql = f.read()
        cur = pg_conn.cursor()
        cur.execute(
            "DELETE FROM geo_research_config WHERE key IN "
            "('fetch_concurrency_per_platform', 'round_auto_resume_enabled', "
            "'round_auto_resume_max')"
        )
        cur.execute(seed_sql)
        cur.execute(seed_sql)  # 幂等 2×
        pg_conn.commit()

        cur.execute(
            "SELECT key, value_json FROM geo_research_config WHERE key IN "
            "('fetch_concurrency_per_platform', 'round_auto_resume_enabled', "
            "'round_auto_resume_max')"
        )
        rows = {r['key']: r['value_json'] for r in cur.fetchall()}
        assert rows['fetch_concurrency_per_platform'] == 8
        # 灰度口径(2026-07-16 部署前审核收紧): seed 首发 false, 并发验证过后 config API 翻 true
        assert rows['round_auto_resume_enabled'] is False
        assert rows['round_auto_resume_max'] == 3

        # 端到端: 不 patch 配置, 走真实 DB 读取
        # (enabled 的 default 传 True: 断言读到的是 seed 落库的 false 而非回落值)
        assert round_runner._get_int_config('fetch_concurrency_per_platform', 999) == 8
        assert round_runner._get_bool_config('round_auto_resume_enabled', True) is False
        assert round_runner._get_int_config('round_auto_resume_max', 999) == 3

    def test_get_stage1_concurrency_fallbacks(self, monkeypatch):
        """读不到/非法值回落 8; clamp 上限; env 覆盖可选保留。纯函数级, 不连 DB。"""
        monkeypatch.delenv('RESEARCH_FETCH_CONCURRENCY', raising=False)
        with _cfg_patch():  # 读不到 → 默认
            assert round_runner._get_stage1_concurrency() == 8
        with _cfg_patch({'fetch_concurrency_per_platform': 0}):
            # _get_int_config 对 ≤0 回落默认
            assert round_runner._get_stage1_concurrency() == 8
        with _cfg_patch({'fetch_concurrency_per_platform': 500}):
            assert round_runner._get_stage1_concurrency() == 64  # clamp 上限
        monkeypatch.setenv('RESEARCH_FETCH_CONCURRENCY', '12')
        with _cfg_patch({'fetch_concurrency_per_platform': 8}):
            assert round_runner._get_stage1_concurrency() == 12  # env 优先
        monkeypatch.setenv('RESEARCH_FETCH_CONCURRENCY', 'abc')
        with _cfg_patch():
            assert round_runner._get_stage1_concurrency() == 8  # env 非法 → 忽略


# ==================== 附 · infer_resume_from_stage 下沉后行为锁定 ====================

class TestInferResumeFromStageSink:
    """改规则必同步测试: 函数从 api 下沉 round_state, 行为必须逐字一致。"""

    @pytest.mark.parametrize('current_stage, expected', [
        (None, 'stage_1'),
        ('', 'stage_1'),
        ('legacy_imported', 'stage_1'),
        ('stage_1_ai_fetch', 'stage_1'),
        ('stage_1_ai_fetch_done', 'stage_2'),
        ('stage_3_crawl', 'stage_3'),
        ('stage_2_prefilter_done', 'stage_3'),
        ('stage_7_aggregate_done', 'stage_8'),
        ('stage_8_notify_done', 'stage_8'),  # n>8 兜底
    ])
    def test_behavior_locked(self, current_stage, expected):
        assert infer_resume_from_stage(current_stage) == expected

    def test_api_alias_delegates(self):
        from api.research_monitor_round_api import _infer_resume_from_stage
        assert _infer_resume_from_stage('stage_4_analyze_done') == 'stage_5'
        assert _infer_resume_from_stage(None) == 'stage_1'


# ==================== 出口审核修复判别(二轮) ====================

def _set_round(round_id, sql_set, params=()):
    """真 PG 直改 round 行(测试造场景专用)。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE geo_research_round SET {sql_set} WHERE round_id = %s",
            tuple(params) + (round_id,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.usefixtures('clean_research_tables')
class TestCancelNotResurrected:
    """出口审核 [复活缺陷 · 三维度 CONFIRMED]: admin cancel 与 4h 超时竞态下,
    自动续跑绝不可复活 cancelled 轮。双层防御分别判别。"""

    def test_admin_cancel_wins_over_timeout_no_resurrection(self):
        """完整链复刻(审核 [5] 实机复现): pipeline 内模拟 admin 在取消检查盲区
        cancel(DB 置 cancelled + 陈旧 reason='4h_hard_timeout' 残留), 随后本段
        4h 超时先触发 → run_round 收尾被 cancelled 守卫 no-op 仍返回
        failed_resumable(返回值与 DB 终态分歧本体)→ 外壳必须放弃, 零重入。"""
        round_id = _mk_round()
        pipeline_calls = []

        async def cancel_then_timeout_pipeline(rid, snapshot, resume_from_stage):
            pipeline_calls.append(resume_from_stage)
            # admin cancel 落在取消检查盲区: 直接写终态 + 模拟历史超时残留的陈旧 reason
            _set_round(
                rid,
                "status = 'cancelled', "
                "summary_json = COALESCE(summary_json, '{}'::jsonb) || "
                "'{\"reason\": \"4h_hard_timeout\", \"cancelled_by\": 1}'::jsonb",
            )
            await asyncio.sleep(0.5)  # 本段超时先于任何取消检查触发

        # [二轮覆审补] spy 判别第一层(快速路径)单独存在: 该场景下 mark 必须
        # 零调用 — 若快速路径被删, mark 会被调用(即使被 WHERE 拒)→ 本断言红。
        mark_spy = MagicMock(side_effect=round_runner.mark_round_resume_requested)
        with patch.object(round_runner, '_run_pipeline',
                          side_effect=cancel_then_timeout_pipeline), \
             patch.object(round_runner, 'ROUND_TIMEOUT_SECONDS', 0.05), \
             patch.object(round_runner, 'mark_round_resume_requested', mark_spy), \
             _cfg_patch({'round_auto_resume_enabled': True}):
            final = asyncio.run(
                round_runner.run_round_with_auto_resume(round_id, _SNAPSHOT)
            )

        # run_round 返回值与 DB 分歧是本缺陷的成立前提(收尾被 cancelled 守卫拦下)
        assert final == 'failed_resumable'
        assert len(pipeline_calls) == 1, "cancelled 轮被自动续跑复活了!"
        mark_spy.assert_not_called()  # 第一层快速路径生效的判别证据
        st = get_round_status(round_id)
        assert st['status'] == 'cancelled', "管理员终态被推翻!"
        assert 'auto_resume_count' not in (st['summary_json'] or {})

    def test_mark_allowed_statuses_where_layer_guard(self):
        """第二层(真闸): mark 的 WHERE 层原子窗口 — 自动路径拿不走 cancelled 轮;
        admin 人工按钮默认窗口(P14-v9 cancelled 可续)语义保持不变。"""
        from services.research_monitor.round_state import mark_round_resume_requested
        round_id = _mk_round()
        _set_round(round_id, "status = 'cancelled'")

        claimed_auto = mark_round_resume_requested(
            round_id, allowed_statuses=('failed_resumable',),
        )
        assert claimed_auto is False
        assert get_round_status(round_id)['status'] == 'cancelled'

        claimed_manual = mark_round_resume_requested(round_id)  # 默认窗口=人工语义
        assert claimed_manual is True
        assert get_round_status(round_id)['status'] == 'pending'

    def test_toctou_where_layer_blocks_after_stale_status_read(self):
        """[二轮覆审补] 第二层真闸【接线】单独判别(掉 wrapper 的 allowed_statuses
        kwarg 本测试必红): 伪造 TOCTOU — wrapper 读到陈旧 failed_resumable
        (patch get_round_status), 真 DB 已被 admin cancel → 快速路径被陈旧读
        骗过、真走到 mark, WHERE 层必须拒绝抢占: 零重入 + DB 保持 cancelled。"""
        round_id = _mk_round()
        _set_round(round_id, "status = 'cancelled'")
        pipeline_calls = []

        async def slow_pipeline(rid, snapshot, resume_from_stage):
            pipeline_calls.append(resume_from_stage)
            await asyncio.sleep(0.5)

        stale = {
            'status': 'failed_resumable',  # 陈旧读(真 DB 是 cancelled)
            'summary_json': {'reason': '4h_hard_timeout'},
            'progress_json': {},
            'current_stage': None,
        }
        mark_spy = MagicMock(side_effect=round_runner.mark_round_resume_requested)
        with patch.object(round_runner, '_run_pipeline',
                          side_effect=slow_pipeline), \
             patch.object(round_runner, 'ROUND_TIMEOUT_SECONDS', 0.05), \
             patch.object(round_runner, 'get_round_status', return_value=stale), \
             patch.object(round_runner, 'mark_round_resume_requested', mark_spy), \
             _cfg_patch({'round_auto_resume_enabled': True}):
            final = asyncio.run(
                round_runner.run_round_with_auto_resume(round_id, _SNAPSHOT)
            )

        assert final == 'failed_resumable'
        assert len(pipeline_calls) == 1, "TOCTOU 窗口内 cancelled 轮被复活!"
        # 快速路径被陈旧读骗过 → mark 真的被调用了(与上一测互补)
        mark_spy.assert_called_once()
        # 且调用点确实传了收窄窗口(真闸接线判别)
        assert mark_spy.call_args.kwargs.get('allowed_statuses') == ('failed_resumable',)
        # 真 DB 权威终态未被推翻
        from services.research_monitor.round_state import get_round_status as _real_get
        assert _real_get(round_id)['status'] == 'cancelled'


@pytest.mark.usefixtures('clean_research_tables')
class TestResumeClaimNotSweptAsZombie:
    """出口审核 [sweep 6h 窗口 · MED]: 抢占把 started_at 置 NOW()(不再清 NULL)后,
    老轮(created_at >6h)刚被抢占的活续跑不会被僵尸 sweep 的 hard_timeout 分支
    误判。只读驱动真实 find_zombie_rounds, 不动 restart_recovery 行为(SPEC 红线)。"""

    def test_claimed_old_round_not_zombie(self):
        from services.research_monitor.round_state import mark_round_resume_requested
        from services.research_monitor.restart_recovery import find_zombie_rounds
        round_id = _mk_round()
        # 造"续跑第 2/3 段"场景: created_at 8h 前 + failed_resumable
        _set_round(
            round_id,
            "status = 'failed_resumable', created_at = NOW() - INTERVAL '8 hours'",
        )

        claimed = mark_round_resume_requested(
            round_id, allowed_statuses=('failed_resumable',),
        )
        assert claimed is True
        st = get_round_status(round_id)
        assert st['started_at'] is not None, "抢占后 started_at 必须置 NOW(), 不再留 NULL 窗口"

        zombie_ids = [z['round_id'] for z in find_zombie_rounds()]
        assert round_id not in zombie_ids, (
            "刚抢占的活续跑轮被 6h hard_timeout 分支误判为僵尸(started_at NULL 窗口回归)"
        )


@pytest.mark.usefixtures('clean_research_tables')
class TestOrphanPendingCleanup:
    """出口审核 [孤儿 pending · LOW 顺手修]: 4h 超时切断的在飞任务遗留 pending
    round_call 行, 自动续跑抢占后应幂等归档为 failed(admin 详情页不再永久'在跑')。"""

    def test_auto_resume_marks_orphan_pending_failed(self, pg_conn):
        round_id = _mk_round()
        cur = pg_conn.cursor()
        # platform 必须过表 CHECK(真实平台名)
        cur.execute(
            """
            INSERT INTO geo_research_round_call
                (round_id, industry_id, prompt_id, prompt_text, platform,
                 status, attempts, started_at)
            VALUES (%s, 1, 1, 'q', 'qwen', 'pending', 0, NOW())
            """,
            (round_id,),
        )
        pg_conn.commit()

        pipeline_calls = []

        async def slow_pipeline(rid, snapshot, resume_from_stage):
            pipeline_calls.append(resume_from_stage)
            await asyncio.sleep(0.5)

        with patch.object(round_runner, '_run_pipeline',
                          side_effect=slow_pipeline), \
             patch.object(round_runner, 'ROUND_TIMEOUT_SECONDS', 0.05), \
             _cfg_patch({
                 'round_auto_resume_enabled': True,
                 'round_auto_resume_max': 1,
             }):
            asyncio.run(round_runner.run_round_with_auto_resume(round_id, _SNAPSHOT))

        assert len(pipeline_calls) == 2  # 首跑 + 1 次自动续跑(清理发生在续跑抢占后)
        cur.execute(
            "SELECT status, error_message FROM geo_research_round_call "
            "WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        assert row['status'] == 'failed'
        assert 'superseded_by_auto_resume' in (row['error_message'] or '')


@pytest.mark.usefixtures('clean_research_tables')
class TestCan014ResumeIdempotencyBehavior:
    """出口审核 [MED·必修]: CAN-014 resume 幂等守卫正路径行为判别(全套件此前
    零覆盖·守卫被逻辑弄死时静默重复付费调用)。真 PG 驱动真实 stage1_ai_fetch_all:
    守卫 neutered(如 MUTATION-3 `and False`)时本测试必红。"""

    def test_success_row_skips_provider_call_and_insert(self, monkeypatch):
        monkeypatch.delenv('RESEARCH_FETCH_CONCURRENCY', raising=False)
        from db.connection import get_connection
        round_id = _mk_round()
        conn = get_connection()
        try:
            cur = conn.cursor()
            # platform 必须过表 CHECK(真实平台名): qwen=已成功, kimi=未做
            cur.execute(
                """
                INSERT INTO geo_research_round_call
                    (round_id, industry_id, prompt_id, prompt_text, platform,
                     status, attempts, started_at, finished_at)
                VALUES (%s, 1, 1, 'q', 'qwen', 'success', 1, NOW(), NOW())
                """,
                (round_id,),
            )
            conn.commit()
        finally:
            conn.close()

        calls = []

        def make_counting_fetcher(platform):
            async def fetcher(prompt_id, prompt):
                calls.append((platform, prompt_id))
                return {'answer': 'a', 'citations': []}
            return fetcher

        fetchers = {'qwen': make_counting_fetcher('qwen'),
                    'kimi': make_counting_fetcher('kimi')}
        plan = [{'industry_id': 1, 'industry_name': '测试行业',
                 'prompt_id': 1, 'prompt_text': 'q'}]

        with patch.object(round_runner, 'PLATFORM_FETCHERS', fetchers), \
             _cfg_patch():
            asyncio.run(round_runner.stage1_ai_fetch_all(round_id, plan))

        # 已 success 的 (round, qwen, prompt 1): 零 provider 调用(不重复扣供应商成本)
        assert ('qwen', 1) not in calls, "CAN-014 守卫失效: 已成功调用被重复派发!"
        # 未做过的 kimi 正常派发
        assert ('kimi', 1) in calls

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT platform, COUNT(*) AS n FROM geo_research_round_call "
                "WHERE round_id = %s GROUP BY platform",
                (round_id,),
            )
            by_platform = {r['platform']: r['n'] for r in cur.fetchall()}
        finally:
            conn.close()
        assert by_platform.get('qwen') == 1, "qwen 不得新增 round_call 行(防重复计费行)"
        assert by_platform.get('kimi') == 1


class TestCallerWiring:
    """出口审核 [MED·必修]: 改动二两处生产调用点换外壳零锚定 — 若合并冲突解回
    裸 run_round, 自动续跑在 cron 与 API 两条路径同时静默失效而全套件保持绿。
    行为级(patch 外壳断言被调) + 源码锁(锚定派发 token 非注释)双保险。"""

    def test_scheduler_trigger_dispatches_wrapper(self):
        from services.research_monitor import scheduler_setup
        wrapper_mock = MagicMock()

        async def fake_wrapper(*args, **kwargs):
            wrapper_mock(*args, **kwargs)
            return 'completed'

        plan = {'industries': [{'id': 1, 'name': '行业', 'slug': 's'}],
                'prompts_by_industry': {1: [{'id': 1, 'text': 'q'}]}}
        with patch.object(scheduler_setup, 'get_active_plan', return_value=plan), \
             patch('services.research_monitor.round_state.create_round_with_snapshot',
                   return_value='round_wire_x'), \
             patch('services.research_monitor.round_runner.run_round_with_auto_resume',
                   fake_wrapper):
            scheduler_setup.trigger_research_round_sync()

        wrapper_mock.assert_called_once()
        assert wrapper_mock.call_args.args[0] == 'round_wire_x'

    def test_run_round_sync_dispatches_wrapper_with_kwargs(self):
        from api import research_monitor_round_api as rapi
        wrapper_mock = MagicMock()

        async def fake_wrapper(*args, **kwargs):
            wrapper_mock(*args, **kwargs)
            return 'completed'

        with patch.object(rapi, 'run_round_with_auto_resume', fake_wrapper):
            rapi._run_round_sync(
                'round_wire_y', {'industries': []}, 'stage_3',
                force_bridge=True, force_answer_entity=True, skip_month_budget=True,
            )

        wrapper_mock.assert_called_once()
        assert wrapper_mock.call_args.args[0] == 'round_wire_y'
        kw = wrapper_mock.call_args.kwargs
        assert kw['resume_from_stage'] == 'stage_3'
        assert kw['force_bridge'] is True
        assert kw['force_answer_entity'] is True
        assert kw['skip_month_budget'] is True

    def test_source_lock_no_bare_run_round_dispatch(self):
        """源码锁: 两个派发函数体内必须调用外壳, 且不存在裸 run_round( 调用
        (锚定真实派发 token; 'run_round_with_auto_resume(' 不会被裸调用正则命中)。"""
        import inspect
        import re
        from services.research_monitor import scheduler_setup
        from api import research_monitor_round_api as rapi

        src_sched = inspect.getsource(scheduler_setup.trigger_research_round_sync)
        assert 'run_round_with_auto_resume(' in src_sched
        assert re.search(r'(?<![\w_])run_round\(', src_sched) is None, (
            "scheduler 派发被解回裸 run_round( → 自动续跑在 cron 路径静默失效"
        )

        src_api = inspect.getsource(rapi._run_round_sync)
        assert 'run_round_with_auto_resume(' in src_api
        assert re.search(r'(?<![\w_])run_round\(', src_api) is None, (
            "API 派发被解回裸 run_round( → 自动续跑在手动/续跑路径静默失效"
        )

    def test_source_lock_wrapper_passes_narrowed_window(self):
        """[二轮覆审补] 源码锁: wrapper 体内 mark 调用必须带收窄窗口 kwarg
        (锚定 mark_round_resume_requested( 调用块内的真 token, 非注释/docstring)。"""
        import inspect
        src = inspect.getsource(round_runner.run_round_with_auto_resume)
        call_idx = src.find('mark_round_resume_requested(')
        assert call_idx != -1, "wrapper 内找不到 mark_round_resume_requested 调用"
        call_block = src[call_idx:call_idx + 800]
        assert "allowed_statuses=('failed_resumable',)" in call_block, (
            "wrapper 的 mark 调用丢失 allowed_statuses 收窄 → "
            "WHERE 层真闸静默退化为含 cancelled 的人工宽窗口(复活缺陷回归)"
        )
