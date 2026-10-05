"""
Stage2 全量预筛 并发+心跳 返工批 判别测试(2026-07-16 · 禁假绿)

映射老板判别清单:
- 组 P  性能: 2,500 域(5,000 URL 含重复/慢站/超时站), 并发16 显著低于串行基线
        (老路径 should_pre_filter_url 逐 URL 串行采样外推)+ 明确绝对上界 +
        全局在飞 ≤16。
- 组 H  心跳: 主等待循环时间驱动(零完成也刷), 缩放判别 max gap ≤ 2×interval,
        600s 级饿死不可能; stage4 节流心跳 / stage7 to_thread 伴飞 ticker。
- 组 R  robots: 同域只抓一次(含 32 线程同域并发在飞去重, 无半成品),
        不同路径按 RobotFileParser 正确判定; 超时 3s 收紧 + 不可用放行语义。
- 组 S  SSRF: 私网/回环/元数据/保留/解析失败 → fail-closed; 恶意重定向拦截;
        并发与 run 级 DNS 裁定缓存不绕过校验。
- 组 O  顺序/上下文: 并发乱序完成下 kept 顺序与原始 raw_id 顺序一致, raw_id/
        rank/industry/prompt 逐字段稳定, 两次运行结果一致。
- 组 C  取消: 判停后不再派发新网络任务, 在飞安全收敛, stage2 抛 RoundCancelledError。
- 组 D  DB/Redis 瞬断: 心跳/取消探测失败只 log, 预筛完成不受影响, stage2 全程
        零终态写入(活轮不可能被误标 completed)。
"""
import asyncio
import socket
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request
from urllib.error import HTTPError
from unittest.mock import patch, MagicMock

import pytest

from services.research_monitor import crawler
from services.research_monitor import round_runner
from services.research_monitor.crawler import (
    PreFilterReason,
    prefilter_urls_concurrent,
    should_pre_filter_url,
    _get_robots_parser_cached,
    _SSRFSafeRedirectHandler,
)


# ==================== 公共假件 ====================

@pytest.fixture(autouse=True)
def _reset_robots_state():
    """模块级 robots 缓存 24h TTL 会跨测试泄漏, 每测清空。"""
    with crawler._ROBOTS_CACHE_LOCK:
        crawler._ROBOTS_CACHE.clear()
        crawler._ROBOTS_INFLIGHT.clear()
    yield
    with crawler._ROBOTS_CACHE_LOCK:
        crawler._ROBOTS_CACHE.clear()
        crawler._ROBOTS_INFLIGHT.clear()


class _Tracker:
    """线程安全计数器: 每域抓取次数 + 全局在飞并发峰值。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.robots_fetches = {}       # netloc -> count
        self.dns_resolves = {}         # host -> count
        self.inflight = 0
        self.max_inflight = 0
        self.timeout_kwargs = []       # 每次 opener.open 收到的 timeout

    def enter(self):
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)

    def leave(self):
        with self.lock:
            self.inflight -= 1


class _FakeResp:
    """有状态消费式 HTTPResponse 假件: read/read1 都读尽返回 b''。
    生产 _fetch_robots_parser 循环调 read1(单次 recv 语义), 假件无状态会无限重读。"""

    def __init__(self, body: bytes):
        self._buf = body

    def _take(self, n):
        if n == -1 or n >= len(self._buf):
            out, self._buf = self._buf, b''
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def read(self, n=-1):
        return self._take(n)

    def read1(self, n=-1):
        return self._take(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeRobotsOpener:
    """替身 _SSRF_SAFE_OPENER: 按域返回 robots 内容/延迟/超时。"""

    def __init__(self, tracker: _Tracker, bodies=None, latency=0.0,
                 slow_hosts=None, timeout_hosts=None, slow_latency=0.1):
        self.tracker = tracker
        self.bodies = bodies or {}
        self.latency = latency
        self.slow_hosts = slow_hosts or set()
        self.timeout_hosts = timeout_hosts or set()
        self.slow_latency = slow_latency

    def open(self, url, timeout=None):
        host = urlparse(url).netloc
        with self.tracker.lock:
            self.tracker.robots_fetches[host] = self.tracker.robots_fetches.get(host, 0) + 1
            self.tracker.timeout_kwargs.append(timeout)
        self.tracker.enter()
        try:
            time.sleep(self.slow_latency if host in self.slow_hosts else self.latency)
            if host in self.timeout_hosts:
                raise socket.timeout('robots fetch timed out')
            body = self.bodies.get(host, b"User-agent: *\nAllow: /\n")
            return _FakeResp(body)
        finally:
            self.tracker.leave()


def _fake_dns(tracker: _Tracker, mapping=None, default_ip='93.184.216.34',
              latency=0.0, fail_hosts=None):
    """替身 _resolve_host_ips。"""
    mapping = mapping or {}
    fail_hosts = fail_hosts or set()

    def resolve(host):
        with tracker.lock:
            tracker.dns_resolves[host] = tracker.dns_resolves.get(host, 0) + 1
        tracker.enter()
        try:
            time.sleep(latency)
            if host in fail_hosts:
                raise socket.gaierror(f'dns fail {host}')
            return mapping.get(host, [default_ip])
        finally:
            tracker.leave()

    return resolve


# ==================== 组 P · 性能前后对照 + 有界并发 ====================

class TestPrefilterPerformance:

    def test_2500_domains_concurrent_vs_serial_baseline(self):
        """2,500 独立域(5,000 URL 含重复)+ 5% 慢站 + 2% 超时站:
        新并发路径显著低于老串行路径外推基线(×0.3)+ 绝对上界 + 在飞 ≤16。"""
        n_domains = 2500
        dns_lat = 0.004
        robots_lat = 0.004
        domains = [f'd{i}.perf.test' for i in range(n_domains)]
        slow = {f'd{i}.perf.test' for i in range(0, n_domains, 20)}       # 5% 慢站
        timeouts = {f'd{i}.perf.test' for i in range(10, n_domains, 50)}  # 2% 超时站
        urls = [f'https://{d}/article/{j}' for d in domains for j in (1, 2)]  # 重复域 5000 URL

        # --- 串行基线(老路径 should_pre_filter_url 逐 URL): 采样 200 域外推 ---
        tracker_s = _Tracker()
        opener_s = _FakeRobotsOpener(tracker_s, latency=robots_lat,
                                     slow_hosts=slow, timeout_hosts=timeouts,
                                     slow_latency=0.05)
        sample = [f'https://{d}/article/1' for d in domains[:200]]
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker_s, latency=dns_lat)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener_s):
            t0 = time.monotonic()
            for u in sample:
                should_pre_filter_url(u, check_robots=True)
            serial_sample = time.monotonic() - t0
        serial_per_domain = serial_sample / 200
        serial_estimate_full = serial_per_domain * n_domains

        # 清缓存, 并发路径从零开始
        with crawler._ROBOTS_CACHE_LOCK:
            crawler._ROBOTS_CACHE.clear()
            crawler._ROBOTS_INFLIGHT.clear()

        # --- 新并发路径(全量 5000 URL / 2500 域 / 16 并发) ---
        tracker_c = _Tracker()
        opener_c = _FakeRobotsOpener(tracker_c, latency=robots_lat,
                                     slow_hosts=slow, timeout_hosts=timeouts,
                                     slow_latency=0.05)
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker_c, latency=dns_lat)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener_c):
            t0 = time.monotonic()
            verdicts, stopped = prefilter_urls_concurrent(
                urls, max_workers=16, heartbeat_interval_seconds=5.0,
            )
            concurrent_elapsed = time.monotonic() - t0

        assert stopped is False
        assert len(verdicts) == len(urls)
        # 全量通过(慢站/超时站 robots 不可用=放行语义)
        assert all(v is None for v in verdicts.values())
        # 同域 robots 只抓一次
        assert all(c == 1 for c in tracker_c.robots_fetches.values())
        assert len(tracker_c.robots_fetches) == n_domains
        # 有界并发: 全局在飞(DNS+robots 出站合计)≤ 16
        assert tracker_c.max_inflight <= 16, (
            f"max_inflight={tracker_c.max_inflight} > 16 → 并发无界"
        )
        # 性能判别: 显著低于串行外推基线 + 绝对上界
        assert concurrent_elapsed < serial_estimate_full * 0.3, (
            f"concurrent={concurrent_elapsed:.2f}s vs serial_est={serial_estimate_full:.2f}s"
        )
        assert concurrent_elapsed < 15.0, f"绝对上界超限: {concurrent_elapsed:.2f}s"
        print(
            f"\n[PERF] domains={n_domains} urls={len(urls)} | "
            f"serial_sample(200域)={serial_sample:.2f}s → 外推全量≈{serial_estimate_full:.1f}s | "
            f"concurrent16={concurrent_elapsed:.2f}s | 加速比≈{serial_estimate_full / max(concurrent_elapsed, 1e-6):.1f}x | "
            f"max_inflight={tracker_c.max_inflight}"
        )


# ==================== 组 H · 心跳 ====================

class TestHeartbeat:

    def test_wait_loop_time_driven_not_completion_driven(self):
        """零完成期间心跳照打: 4 origin 各睡 1.2s(workers=2 → 首批 1.2s 内零完成),
        interval=0.2 → 相邻心跳最大间隔 ≤ 0.6s(判别: 完成驱动的话首个 gap ≥1.2s)。"""
        tracker = _Tracker()
        opener = _FakeRobotsOpener(tracker, latency=1.2)
        beats = []
        urls = [f'https://hb{i}.test/a' for i in range(4)]
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            t0 = time.monotonic()
            verdicts, stopped = prefilter_urls_concurrent(
                urls, max_workers=2,
                heartbeat_cb=lambda: beats.append(time.monotonic() - t0),
                heartbeat_interval_seconds=0.2,
            )
        assert stopped is False and len(verdicts) == 4
        assert len(beats) >= 5, f"心跳次数过少: {beats}"
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        assert max(gaps) <= 0.6, (
            f"max gap={max(gaps):.2f}s > 3×interval → 心跳被完成驱动饿死"
        )

    def test_stage2_passes_30s_interval(self):
        """stage2 主循环用 30s 节拍常量(600s 饿死结构性不可能)。"""
        assert round_runner.STAGE2_HEARTBEAT_INTERVAL_SECONDS == 30.0
        import inspect
        src = inspect.getsource(round_runner.stage2_extract_and_prefilter_urls)
        assert 'heartbeat_interval_seconds=STAGE2_HEARTBEAT_INTERVAL_SECONDS' in src
        # best-effort 心跳回调接入并发预筛主等待循环(锚定真实传参 token)
        assert 'heartbeat_cb=_stage2_beat' in src
        assert 'update_heartbeat(round_id)' in src  # _stage2_beat 内真调心跳

    def test_stage4_heartbeat_throttled_tick(self):
        """stage4 修复判别: 处理期间按时间节流打心跳(旧版全程零心跳仅收尾 1 次)。"""
        arts = [
            {'id': i, 'url': f'https://a{i}.t/x', 'url_hash': f'h{i}',
             'oss_key_raw': f'raw/{i}.md', 'raw_char_count': 100, 'domain': f'a{i}.t'}
            for i in range(12)
        ]

        class _C:
            def cursor(self):
                return self

            def execute(self, *a, **k):
                pass

            def fetchall(self):
                return arts

            def commit(self):
                pass

            def close(self):
                pass

        hb = MagicMock()
        with patch.object(round_runner, 'get_connection', side_effect=lambda: _C()), \
             patch.object(round_runner, 'download_markdown',
                          side_effect=lambda k: (time.sleep(0.03) or 'content ' * 50)), \
             patch.object(round_runner, 'upload_markdown',
                          return_value={'ok': True}), \
             patch.object(round_runner, '_rule_clean_markdown', side_effect=lambda x: x), \
             patch.object(round_runner, '_rule_text_length', return_value=300), \
             patch.object(round_runner, 'generate_oss_key_for_article',
                          return_value='cleaned/x.md'), \
             patch.object(round_runner, 'update_heartbeat', hb), \
             patch.object(round_runner, 'update_round_progress', MagicMock()), \
             patch.object(round_runner, 'get_round_status',
                          return_value={'status': 'running'}), \
             patch.object(round_runner, 'HEARTBEAT_TICK_INTERVAL_SECONDS', 0.05), \
             patch.object(round_runner, 'CLEAN_CONCURRENCY', 2):
            asyncio.run(round_runner.stage4_clean_articles('round_hb4'))

        # 旧版: 全程 0 次 + 收尾 1 次; 新版: 处理期间节流 tick ≥2 次 + 收尾 1 次
        assert hb.call_count >= 3, f"stage4 心跳次数 {hb.call_count} < 3 → 节流 tick 未生效"

    def test_stage7_ticker_beats_during_to_thread(self):
        """stage7 修复判别: to_thread 阻塞期间伴飞心跳照打。"""
        hb = MagicMock()
        with patch.object(round_runner, 'update_heartbeat', hb):
            result = asyncio.run(
                round_runner._to_thread_with_heartbeat(
                    'round_hb7', time.sleep, 0.45, interval_seconds=0.1,
                )
            )
        assert result is None
        assert hb.call_count >= 3, f"伴飞心跳 {hb.call_count} < 3 → ticker 未生效"

    def test_heartbeat_ticker_survives_db_error(self):
        """伴飞心跳 DB 瞬断只 log 不死。"""
        calls = {'n': 0}

        def flaky(_rid):
            calls['n'] += 1
            raise OSError('db blip')

        with patch.object(round_runner, 'update_heartbeat', side_effect=flaky):
            asyncio.run(
                round_runner._to_thread_with_heartbeat(
                    'round_hb8', time.sleep, 0.25, interval_seconds=0.05,
                )
            )
        assert calls['n'] >= 2  # 抛错后仍继续下一拍


# ==================== 组 R · robots ====================

class TestRobots:

    def test_same_domain_fetched_once_paths_judged_by_parser(self):
        """3 域 × 各 40 URL: 每域恰 1 次抓取; /private/* 禁 /public/* 放。"""
        tracker = _Tracker()
        body = b"User-agent: *\nDisallow: /private/\n"
        opener = _FakeRobotsOpener(
            tracker, bodies={f'r{i}.test': body for i in range(3)},
        )
        urls = []
        for i in range(3):
            for j in range(20):
                urls.append(f'https://r{i}.test/public/{j}')
                urls.append(f'https://r{i}.test/private/{j}')
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            verdicts, stopped = prefilter_urls_concurrent(urls, max_workers=8)
        assert stopped is False
        assert tracker.robots_fetches == {f'r{i}.test': 1 for i in range(3)}
        for u, v in verdicts.items():
            if '/private/' in u:
                assert v == PreFilterReason.ROBOTS_DISALLOWED, u
            else:
                assert v is None, u

    def test_concurrent_same_domain_single_fetch_no_partial(self):
        """32 线程同域并发直打 _get_robots_parser_cached: 恰 1 次抓取,
        全员拿到同一份完整 parser(可正常 can_fetch, 无半成品)。
        [轮1审核修] Barrier 强制同刻起跑压真锁窗口(否则 Thread.start 串行
        错峰会让删锁变体碰巧也 =1 次抓取 → 假绿)。"""
        tracker = _Tracker()
        opener = _FakeRobotsOpener(
            tracker,
            bodies={'busy.test': b"User-agent: *\nDisallow: /no/\n"},
            latency=0.3,
        )
        results = []
        barrier = threading.Barrier(32)

        def _worker():
            barrier.wait(timeout=10)  # 32 线程同刻冲进 check-then-act 窗口
            results.append(
                _get_robots_parser_cached('https', 'busy.test', 'busy.test')
            )

        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            threads = [threading.Thread(target=_worker) for _ in range(32)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        assert tracker.robots_fetches.get('busy.test') == 1, (
            f"同域并发重复抓取: {tracker.robots_fetches}"
        )
        assert len(results) == 32
        assert all(r is not None for r in results)
        assert all(not r.can_fetch(crawler.ROBOTS_UA, 'https://busy.test/no/x')
                   for r in results)
        assert all(r.can_fetch(crawler.ROBOTS_UA, 'https://busy.test/ok')
                   for r in results)
        with crawler._ROBOTS_CACHE_LOCK:
            assert not crawler._ROBOTS_INFLIGHT  # in-flight 表无泄漏

    def test_robots_timeout_3s_and_unavailable_allows(self):
        """超时收紧至 3s(opener 收到 timeout=3.0)且超时→放行语义不变。"""
        assert crawler._ROBOTS_FETCH_TIMEOUT_SECONDS == 3.0
        tracker = _Tracker()
        opener = _FakeRobotsOpener(tracker, timeout_hosts={'t.test'})
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            verdicts, _ = prefilter_urls_concurrent(['https://t.test/a'])
        assert verdicts['https://t.test/a'] is None  # 不可用 → 放行
        assert tracker.timeout_kwargs == [3.0]


# ==================== 组 S · SSRF fail-closed ====================

class TestSSRF:

    def test_private_metadata_loopback_reserved_and_dnsfail_blocked(self):
        tracker = _Tracker()
        mapping = {
            'intra.test': ['10.0.0.5'],
            'meta.test': ['169.254.169.254'],
            'loop.test': ['::ffff:127.0.0.1'],
            'zero.test': ['0.0.0.0'],
            'ok.test': ['93.184.216.34'],
        }
        opener = _FakeRobotsOpener(tracker)
        urls = [f'https://{h}/x' for h in
                ('intra.test', 'meta.test', 'loop.test', 'zero.test', 'dead.test', 'ok.test')]
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker, mapping=mapping,
                                                fail_hosts={'dead.test'})), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            verdicts, _ = prefilter_urls_concurrent(urls, max_workers=6)
        for h in ('intra.test', 'meta.test', 'loop.test', 'zero.test', 'dead.test'):
            assert verdicts[f'https://{h}/x'] == PreFilterReason.PRIVATE_ADDRESS, h
        assert verdicts['https://ok.test/x'] is None
        # 不安全域绝不发 robots 请求
        for h in ('intra.test', 'meta.test', 'loop.test', 'zero.test', 'dead.test'):
            assert h not in tracker.robots_fetches

    def test_malicious_redirect_blocked(self):
        """robots 抓取重定向到元数据地址 → 逐跳新鲜校验拦截(不经任何缓存)。"""
        tracker = _Tracker()
        handler = _SSRFSafeRedirectHandler()
        req = Request('https://ok.test/robots.txt')
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(
                              tracker, mapping={'evil.internal': ['169.254.169.254'],
                                                'ok.test': ['93.184.216.34']})):
            with pytest.raises(HTTPError):
                handler.redirect_request(
                    req, None, 302, 'Found', {}, 'http://evil.internal/latest/meta-data'
                )
            # 公网目标正常放行
            out = handler.redirect_request(
                req, None, 302, 'Found', {}, 'https://ok.test/robots2.txt'
            )
            assert out is not None

    def test_dns_verdict_cache_does_not_bypass_block(self):
        """run 级 DNS 裁定缓存: 同 hostname 只解析一次, 但不安全裁定对
        http/https 两个 origin 都持续生效(缓存不绕过校验)。"""
        tracker = _Tracker()
        opener = _FakeRobotsOpener(tracker)
        urls = ['https://bad.test/a', 'http://bad.test/b', 'https://bad.test/c']
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker,
                                                mapping={'bad.test': ['192.168.1.9']})), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            verdicts, _ = prefilter_urls_concurrent(urls, max_workers=4)
        assert all(v == PreFilterReason.PRIVATE_ADDRESS for v in verdicts.values())
        assert tracker.dns_resolves.get('bad.test') == 1  # 合并解析
        assert 'bad.test' not in tracker.robots_fetches   # 零出站


# ==================== 组 O · 顺序与上下文稳定 ====================

class _Stage2FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return self

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self.rows

    def commit(self):
        pass

    def close(self):
        pass


def _run_stage2(rows, plan, tracker=None, opener=None, cancel_seq=None):
    tracker = tracker or _Tracker()
    opener = opener or _FakeRobotsOpener(tracker, latency=0.01)
    status_seq = list(cancel_seq or [])

    def _status(_rid):
        if status_seq:
            return {'status': status_seq.pop(0)}
        return {'status': 'running'}

    with patch.object(round_runner, 'get_connection',
                      side_effect=lambda: _Stage2FakeConn(rows)), \
         patch.object(round_runner, 'update_heartbeat', MagicMock()), \
         patch.object(round_runner, 'update_round_progress', MagicMock()), \
         patch.object(round_runner, 'get_round_status', side_effect=_status), \
         patch.object(crawler, '_resolve_host_ips',
                      side_effect=_fake_dns(tracker)), \
         patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
        return round_runner.stage2_extract_and_prefilter_urls(
            'round_o1', 'batch_round_o1', plan,
        )


class TestOrderAndContextStability:

    def _rows_and_plan(self):
        rows = []
        rid = 0
        # 交错 8 个域 × 6 条 + 完全重复 URL + 待过滤(pdf)混排
        for j in range(6):
            for i in range(8):
                rid += 1
                rows.append({
                    'raw_id': rid,
                    'cite_url': f'https://o{i}.test/art/{j}',
                    'industry': f'行业{i % 3}',
                    'query': f'问题{i % 4}',
                    'engine': 'qwen' if i % 2 else 'kimi',
                    'cite_position': j + 1,
                })
        rid += 1
        rows.append({'raw_id': rid, 'cite_url': 'https://o1.test/art/0',   # 重复
                     'industry': '行业1', 'query': '问题1', 'engine': 'qwen',
                     'cite_position': 9})
        rid += 1
        rows.append({'raw_id': rid, 'cite_url': 'https://o2.test/file.pdf',  # 形状过滤
                     'industry': '行业2', 'query': '问题2', 'engine': 'kimi',
                     'cite_position': 9})
        plan = [
            {'industry_id': 100 + k, 'industry_name': f'行业{k}',
             'prompt_id': 200 + k, 'prompt_text': f'问题{k}'}
            for k in range(4)
        ]
        return rows, plan

    def test_order_context_stable_across_runs(self):
        rows, plan = self._rows_and_plan()
        # 变速 opener 打乱完成顺序(慢域交错)
        tracker1 = _Tracker()
        opener1 = _FakeRobotsOpener(
            tracker1, latency=0.005,
            slow_hosts={'o0.test', 'o3.test', 'o6.test'}, slow_latency=0.08,
        )
        kept1 = _run_stage2(rows, plan, tracker1, opener1)

        with crawler._ROBOTS_CACHE_LOCK:
            crawler._ROBOTS_CACHE.clear()
            crawler._ROBOTS_INFLIGHT.clear()
        tracker2 = _Tracker()
        opener2 = _FakeRobotsOpener(
            tracker2, latency=0.005,
            slow_hosts={'o1.test', 'o5.test'}, slow_latency=0.08,
        )
        kept2 = _run_stage2(rows, plan, tracker2, opener2)

        assert kept1 == kept2, "两次运行(不同完成时序)结果不一致"
        # 顺序 = 原始 raw_id 首现顺序(前 48 条里的 8 个首现: raw_id 1..8)
        assert [k['raw_id'] for k in kept1][:8] == list(range(1, 9))
        assert [k['raw_id'] for k in kept1] == sorted(k['raw_id'] for k in kept1)
        # 上下文逐字段: 行业/prompt 反查 + rank 原样
        first = kept1[0]
        assert first['url'] == 'https://o0.test/art/0'
        assert first['industry_id'] == 100 and first['prompt_id'] == 200
        assert first['rank_in_response'] == 1
        assert first['platform'] == 'kimi'
        # 重复 URL(raw_id=49)与 pdf(raw_id=50)都不在 kept
        kept_ids = {k['raw_id'] for k in kept1}
        assert 49 not in kept_ids and 50 not in kept_ids
        # 8 域 × 6 条全通过
        assert len(kept1) == 48


# ==================== 组 C · 取消 ====================

class TestCancellation:

    def test_cancel_stops_dispatch_and_converges(self):
        """判停后: 已开跑在飞收敛, 未开跑不再发起新网络请求(抓取数远小于总量)。"""
        tracker = _Tracker()
        opener = _FakeRobotsOpener(tracker, latency=0.15)
        urls = [f'https://c{i}.test/a' for i in range(60)]
        stop_flag = {'v': False}
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            verdicts, stopped = prefilter_urls_concurrent(
                urls, max_workers=4,
                should_stop_cb=lambda: stop_flag['v'],
                heartbeat_cb=lambda: stop_flag.__setitem__('v', True),  # 第一片醒来即判停
                heartbeat_interval_seconds=0.1,
            )
        assert stopped is True
        started = sum(tracker.robots_fetches.values())
        # 4 workers × 少数几片 « 60: 判停后未开跑任务被撤, 不再发新请求
        assert started < 30, f"判停后仍大量派发: started={started}"
        # [轮1修复后语义] shutdown(wait=False): 在飞任务异步收敛(正常上界秒级),
        # 轮询等待其排干; 判停后 started 不再增长另行断言
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with crawler._ROBOTS_CACHE_LOCK:
                if not crawler._ROBOTS_INFLIGHT:
                    break
            time.sleep(0.05)
        with crawler._ROBOTS_CACHE_LOCK:
            assert not crawler._ROBOTS_INFLIGHT, "在飞任务未在上界内收敛"
        final_started = sum(tracker.robots_fetches.values())
        assert final_started == started or final_started <= started + 4, (
            "判停后仍有新派发(超出在飞收敛余量)"
        )

    def test_stage2_raises_round_cancelled(self):
        rows = [{'raw_id': i + 1, 'cite_url': f'https://cc{i}.test/a',
                 'industry': '行业0', 'query': '问题0', 'engine': 'qwen',
                 'cite_position': 1} for i in range(40)]
        tracker = _Tracker()
        opener = _FakeRobotsOpener(tracker, latency=0.2)
        with pytest.raises(round_runner.RoundCancelledError):
            _run_stage2(rows, None, tracker, opener,
                        cancel_seq=['cancelled'])  # 第一次探测即已取消


# ==================== 组 D · DB/Redis 瞬断 ====================

class TestTransientFailures:

    def test_heartbeat_and_cancel_probe_failures_do_not_break_prefilter(self):
        tracker = _Tracker()
        opener = _FakeRobotsOpener(tracker, latency=0.05)
        urls = [f'https://t{i}.test/a' for i in range(8)]

        def bad_hb():
            raise OSError('redis blip')

        def bad_stop():
            raise ConnectionError('db blip')

        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            verdicts, stopped = prefilter_urls_concurrent(
                urls, max_workers=4,
                heartbeat_cb=bad_hb, should_stop_cb=bad_stop,
                heartbeat_interval_seconds=0.05,
            )
        assert stopped is False
        assert len(verdicts) == 8 and all(v is None for v in verdicts.values())

    def test_stage2_transient_db_errors_never_write_terminal_state(self):
        """update_heartbeat 瞬断 + get_round_status 瞬断: stage2 正常完成,
        全程零 update_round_complete 调用(活轮不可能被 stage2 误标 completed)。"""
        rows = [{'raw_id': i + 1, 'cite_url': f'https://d{i}.test/a',
                 'industry': '行业0', 'query': '问题0', 'engine': 'qwen',
                 'cite_position': 1} for i in range(6)]
        tracker = _Tracker()
        opener = _FakeRobotsOpener(tracker, latency=0.02)
        hb_calls = {'n': 0}

        def flaky_hb(_rid):
            hb_calls['n'] += 1
            if hb_calls['n'] <= 2:
                raise OSError('db blip')

        def flaky_status(_rid):
            raise ConnectionError('db blip')

        complete_spy = MagicMock()
        with patch.object(round_runner, 'get_connection',
                          side_effect=lambda: _Stage2FakeConn(rows)), \
             patch.object(round_runner, 'update_heartbeat', side_effect=flaky_hb), \
             patch.object(round_runner, 'update_round_progress', MagicMock()), \
             patch.object(round_runner, 'get_round_status', side_effect=flaky_status), \
             patch.object(round_runner, 'update_round_complete', complete_spy), \
             patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            kept = round_runner.stage2_extract_and_prefilter_urls(
                'round_d1', 'batch_round_d1', None,
            )
        assert len(kept) == 6
        complete_spy.assert_not_called()


# ==================== 轮1审核修复判别 ====================

def _wait_inflight_drained(timeout=8.0):
    """轮询等被弃养/钉死的后台线程收敛(写完缓存+pop in-flight), 防跨测试污染。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with crawler._ROBOTS_CACHE_LOCK:
            if not crawler._ROBOTS_INFLIGHT:
                return
        time.sleep(0.05)


class TestTarpitDefenses:
    """[轮1审核 MED·必修] robots 3s 是 per-socket-op 超时非总上界(滴流服务器可把
    单次 read(256KB) 拖到天级, 且持续心跳会把 10min 僵尸 sweep 致盲): 分块读总
    预算 / 超龄弃养 / 提交段探测节流 三道护栏各自独立判别。"""

    def test_total_deadline_breaks_headdrip_deadlock(self):
        """[轮2 MED·必修 死循环判别] "worker 无法自解放"类滴流(头部滴流 open() 内
        阻塞 / chunked 正文滴流 read1 内 readline 阻塞——两者都够不到护栏1墙钟)钉满
        全部 worker → 排队 origin 永无 started_at 永不被单任务弃养 → 旧代码 while
        pending 死循环无限期阻塞。编排层总墙钟(guard-3)兜底: 无论如何有界返回,
        全 fail-closed。判别: 若 revert 总墙钟, 本测试会挂死(open sleep 远超断言)。
        (此处用 open() 阻塞代表该类; chunked 正文滴流机制同——见
        test_chunked_body_drip_falls_to_guard3。)"""
        tracker = _Tracker()
        # 2 worker 全被 open() 阻塞 3s(头部滴流类比: open 不返回, read1 护栏够不到);
        # 另 6 个排队 origin 永远等不到 worker
        opener = _FakeRobotsOpener(tracker, slow_hosts={f'h{i}.dl' for i in range(8)},
                                   slow_latency=3.0)
        urls = [f'https://h{i}.dl/a' for i in range(8)]
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener), \
             patch.object(crawler, '_ORIGIN_CHECK_ABANDON_SECONDS', 999.0), \
             patch.object(crawler, '_PREFILTER_MIN_TOTAL_DEADLINE_SECONDS', 0.3), \
             patch.object(crawler, '_PREFILTER_MAX_TOTAL_DEADLINE_SECONDS', 0.6), \
             patch.object(crawler, '_PREFILTER_PER_ORIGIN_BUDGET_SECONDS', 0.1):
            t0 = time.monotonic()
            verdicts, stopped = prefilter_urls_concurrent(
                urls, max_workers=2, heartbeat_interval_seconds=0.1,
            )
            elapsed = time.monotonic() - t0
        # 有界返回(不无限挂): 总墙钟 ≤0.6s + 一片 wait 0.1s + 裕量
        assert elapsed < 2.5, f"总墙钟未打破死循环, 被钉死: {elapsed:.1f}s"
        assert stopped is False
        assert len(verdicts) == 8
        # 全部 fail-closed(超时收尾)
        assert all(v == PreFilterReason.NETWORK_CHECK_TIMEOUT for v in verdicts.values())
        _wait_inflight_drained()

    def test_body_drip_bounded_by_total_read_deadline(self):
        """【非 chunked】正文滴流判别(轮2修·忠实假件): 生产走 read1()(对定长/
        identity 响应=单次 recv, 每次 ≤3s 返回已到达字节), 墙钟预算(patch 0.5s)
        在块间必命中 → None=放行。判别力: 若 revert 回 read(n)(阻塞凑满 n),
        read(总字节)一次阻塞到远超 0.5s 才返回 → elapsed 断言红(不挂死: 总字节有限)。
        [轮3边界] 本护栏【不】覆盖 chunked 正文滴流(read1 内 readline 读 chunk-size
        跨多 recv 仍可被拖住)——那类 worker 无法自解放, 由 guard-3 总墙钟兜底,
        判别见 test_total_deadline_breaks_headdrip_deadlock。"""
        class _DripResp:
            """忠实 HTTPResponse 滴流:
            - read1(n): 单次 recv 语义 — sleep 一次返回至多 1 字节(慢滴)。
            - read(n): 阻塞凑满 min(n, 剩余) 字节才返回(vulnerable 老写法调它)。
            总字节有限(200), 两条路径都终止, revert 后靠 elapsed 判红而非挂死。"""
            def __init__(self):
                self._remaining = 200

            def read1(self, n=-1):
                if self._remaining <= 0:
                    return b''
                time.sleep(0.05)
                self._remaining -= 1
                return b'x'

            def read(self, n=-1):
                want = self._remaining if n < 0 else min(n, self._remaining)
                out = b''
                while len(out) < want and self._remaining > 0:
                    time.sleep(0.05)
                    self._remaining -= 1
                    out += b'x'
                return out

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _DripOpener:
            def open(self, url, timeout=None):
                return _DripResp()

        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=lambda h: ['93.184.216.34']), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', _DripOpener()), \
             patch.object(crawler, '_ROBOTS_TOTAL_READ_DEADLINE_SECONDS', 0.5):
            t0 = time.monotonic()
            rp = crawler._fetch_robots_parser('https', 'drip.test', 'drip.test')
            elapsed = time.monotonic() - t0
        assert rp is None  # 放弃 → 放行语义
        # read1 逐块: ~0.5s 预算命中; revert 回 read: 一次阻塞 ~10s(200×0.05)才返回
        assert elapsed < 2.0, f"滴流未被总预算切断(疑似 read 阻塞至满未改 read1): {elapsed:.1f}s"

    def test_straggler_origin_abandoned_fail_closed(self):
        """超龄在飞任务弃养: 1 个 origin 挂 5s(abandon patch 0.5s)→ 函数 ≲3s
        返回, 该域 fail-closed 过滤, 其余照常 — 单恶意域钉不死整轮。"""
        tracker = _Tracker()
        opener = _FakeRobotsOpener(tracker, slow_hosts={'stuck.test'},
                                   slow_latency=5.0)
        urls = ['https://stuck.test/a'] + [f'https://ok{i}.test/a' for i in range(6)]
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener), \
             patch.object(crawler, '_ORIGIN_CHECK_ABANDON_SECONDS', 0.5):
            t0 = time.monotonic()
            verdicts, stopped = prefilter_urls_concurrent(
                urls, max_workers=4, heartbeat_interval_seconds=0.1,
            )
            elapsed = time.monotonic() - t0
        assert stopped is False
        assert elapsed < 3.0, f"被钉死域拖住整轮: {elapsed:.1f}s"
        # 轮2: 弃养/超时用独立原因码 NETWORK_CHECK_TIMEOUT(非 PRIVATE_ADDRESS·非私网)
        assert verdicts['https://stuck.test/a'] == PreFilterReason.NETWORK_CHECK_TIMEOUT
        for i in range(6):
            assert verdicts[f'https://ok{i}.test/a'] is None
        _wait_inflight_drained()

    def test_chunked_body_drip_falls_to_guard3(self):
        """[轮3 LOW·口径判别] chunked 正文滴流: 单次 read1() 也被拖住(模拟 read1
        内 readline 读 chunk-size 跨多 recv 不返回)→ 护栏1墙钟够不到 → worker
        无法自解放。断言: prefilter 仍由 guard-3 总墙钟有界返回 + 该域
        NETWORK_CHECK_TIMEOUT(证明兜底是 guard-3 非 read1 自解放)。"""
        # stop_event 让"钉死"线程在测试结束时能退出(测试卫生, 不削弱判别:
        # guard-3 在 0.6s 先触发, read1 阻塞 > 该值即证明非 read1 自解放)
        stop_event = threading.Event()

        class _ChunkedDripResp:
            # read1 本身阻塞不返回(chunk-size 行逐字节滴), 护栏1 的块间墙钟够不到
            def read1(self, n=-1):
                stop_event.wait(timeout=10.0)  # 上限防测试自身 bug 挂死
                return b'x'

            def read(self, n=-1):
                stop_event.wait(timeout=10.0)
                return b'x'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _ChunkedOpener:
            def open(self, url, timeout=None):
                return _ChunkedDripResp()

        try:
            with patch.object(crawler, '_resolve_host_ips',
                              side_effect=lambda h: ['93.184.216.34']), \
                 patch.object(crawler, '_SSRF_SAFE_OPENER', _ChunkedOpener()), \
                 patch.object(crawler, '_ORIGIN_CHECK_ABANDON_SECONDS', 999.0), \
                 patch.object(crawler, '_PREFILTER_MIN_TOTAL_DEADLINE_SECONDS', 0.3), \
                 patch.object(crawler, '_PREFILTER_MAX_TOTAL_DEADLINE_SECONDS', 0.6), \
                 patch.object(crawler, '_PREFILTER_PER_ORIGIN_BUDGET_SECONDS', 0.1):
                t0 = time.monotonic()
                verdicts, stopped = prefilter_urls_concurrent(
                    ['https://chunk0.dl/a', 'https://chunk1.dl/a', 'https://chunk2.dl/a'],
                    max_workers=2, heartbeat_interval_seconds=0.1,
                )
                elapsed = time.monotonic() - t0
            assert elapsed < 2.5, f"chunked 滴流未被 guard-3 兜底: {elapsed:.1f}s"
            assert stopped is False
            assert all(v == PreFilterReason.NETWORK_CHECK_TIMEOUT for v in verdicts.values())
        finally:
            stop_event.set()  # 放行钉死线程尽快退出
            _wait_inflight_drained()

    def test_submit_loop_stop_probe_throttled(self):
        """提交循环判停探测按时间节流: 200 origin 的探测次数 « 200
        (未节流实现 = 逐 origin 一次 = 200 条串行 DB 查询)。"""
        tracker = _Tracker()
        opener = _FakeRobotsOpener(tracker)
        probes = {'n': 0}

        def probe():
            probes['n'] += 1
            return False

        urls = [f'https://s{i}.test/a' for i in range(200)]
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(tracker)), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            verdicts, stopped = prefilter_urls_concurrent(
                urls, max_workers=8, should_stop_cb=probe,
                heartbeat_interval_seconds=0.5,
            )
        assert stopped is False and len(verdicts) == 200
        assert probes['n'] < 20, f"探测 {probes['n']} 次 ≈ 逐 origin 未节流"

    def test_stage2_wiring_source_lock(self):
        """[轮1审核 · 硬要求 1 接线锁] stage2 派发必须带
        STAGE2_PREFILTER_CONCURRENCY; 常量默认 16 且 clamp 上限 16(env 只许调小)。"""
        import inspect
        src = inspect.getsource(round_runner.stage2_extract_and_prefilter_urls)
        assert 'max_workers=STAGE2_PREFILTER_CONCURRENCY' in src
        assert round_runner.STAGE2_PREFILTER_CONCURRENCY <= 16
        mod_src = inspect.getsource(round_runner)
        assert (
            "_safe_int_env('RESEARCH_STAGE2_PREFILTER_CONCURRENCY', 16, minimum=1), 16,"
            in mod_src
        ), "并发常量默认 16/clamp 16 被改动(硬要求 1 字面上限)"


# ==================== 附 · 单 URL 路径(backfill)语义锁 ====================

class TestSingleUrlPathUnchanged:

    def test_should_pre_filter_url_semantics_locked(self):
        tracker = _Tracker()
        opener = _FakeRobotsOpener(
            tracker, bodies={'s.test': b"User-agent: *\nDisallow: /no/\n"},
        )
        with patch.object(crawler, '_resolve_host_ips',
                          side_effect=_fake_dns(
                              tracker, mapping={'s.test': ['93.184.216.34'],
                                                'p.test': ['10.1.1.1']})), \
             patch.object(crawler, '_SSRF_SAFE_OPENER', opener):
            assert should_pre_filter_url('https://s.test/ok', check_robots=True) is None
            assert should_pre_filter_url(
                'https://s.test/no/x', check_robots=True,
            ) == PreFilterReason.ROBOTS_DISALLOWED
            assert should_pre_filter_url(
                'https://p.test/x', check_robots=True,
            ) == PreFilterReason.PRIVATE_ADDRESS
            assert should_pre_filter_url(
                'https://s.test/file.pdf', check_robots=True,
            ) == PreFilterReason.UNSUPPORTED_EXTENSION
            assert should_pre_filter_url(
                'https://s.test/search?q=1', check_robots=True,
            ) == PreFilterReason.URL_PATTERN
            assert should_pre_filter_url('', check_robots=True) == PreFilterReason.URL_PATTERN
        # 同域二次调用走缓存: 抓取仍 1 次
        assert tracker.robots_fetches.get('s.test') == 1
