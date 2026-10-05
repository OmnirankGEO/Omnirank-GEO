"""跑批级熔断保护测试"""
import pytest
from services.research_monitor.circuit_breaker import CircuitBreaker


class TestCircuitBreaker:

    def test_initial_state_not_tripped(self):
        cb = CircuitBreaker()
        tripped, reason = cb.is_tripped()
        assert tripped is False
        assert reason is None

    def test_consecutive_failures_trip(self):
        """连续失败 50 次熔断"""
        cb = CircuitBreaker(consecutive_threshold=50, rate_threshold=0.5, min_processed=100)
        for _ in range(50):
            cb.record_failure()
        tripped, reason = cb.is_tripped()
        assert tripped is True
        assert reason == 'consecutive'

    def test_consecutive_threshold_minus_one_not_trip(self):
        """连续失败 49 次不熔断"""
        cb = CircuitBreaker(consecutive_threshold=50)
        for _ in range(49):
            cb.record_failure()
        tripped, _ = cb.is_tripped()
        assert tripped is False

    def test_success_resets_consecutive_count(self):
        """成功重置连续失败计数"""
        cb = CircuitBreaker(consecutive_threshold=50, min_processed=1000)  # 高 min 防 rate 触发
        for _ in range(49):
            cb.record_failure()
        cb.record_success()  # 重置
        for _ in range(49):
            cb.record_failure()  # 又 49,但前面被重置了
        tripped, _ = cb.is_tripped()
        assert tripped is False

    def test_rate_threshold_trips_when_over_min_processed(self):
        """跑了 100 次, 失败率超 50% 熔断"""
        cb = CircuitBreaker(consecutive_threshold=10000, rate_threshold=0.5, min_processed=100)
        # 用 record_success / record_failure 凑 60 失败 + 40 成功 = 60% > 50%
        for _ in range(40):
            cb.record_success()
        for i in range(60):
            cb.record_failure()
            if i == 9:  # 防连续 10 次熔断, 中间穿插一次成功
                cb.record_success()
        # 实际: 41 success, 60 failure = 60/101 = 59.4% > 50%, 但这里 consecutive_threshold=10000 不会触发 consecutive
        # 而 rate_threshold=0.5 应该触发
        tripped, reason = cb.is_tripped()
        assert tripped is True
        assert reason == 'rate'

    def test_rate_high_but_under_min_processed_no_trip(self):
        """跑了不到 min_processed 次, 即使失败率高也不熔断(早期波动)"""
        cb = CircuitBreaker(consecutive_threshold=10000, rate_threshold=0.5, min_processed=100)
        # 跑 50 次, 全失败, rate=100%
        for i in range(50):
            cb.record_failure()
            # 防 consecutive 触发
            if i == 9 or i == 19 or i == 29 or i == 39 or i == 49:
                cb.record_success()
        # 共 5 success + 50 failure = 55,< min_processed=100
        # rate=50/55=90% > 50%,但 processed < 100 不触发
        tripped, _ = cb.is_tripped()
        assert tripped is False

    def test_consecutive_takes_priority_over_rate(self):
        """连续 + 整体都超时, 报 'consecutive' 优先"""
        cb = CircuitBreaker(consecutive_threshold=50, rate_threshold=0.5, min_processed=100)
        # 直接连续 50 失败, rate=100%
        for _ in range(50):
            cb.record_failure()
        tripped, reason = cb.is_tripped()
        assert tripped is True
        assert reason == 'consecutive'

    def test_get_stats(self):
        """get_stats 返回当前统计(供日志/UI 显示)"""
        cb = CircuitBreaker(consecutive_threshold=50, rate_threshold=0.5, min_processed=100)
        cb.record_success()
        cb.record_success()
        cb.record_failure()
        stats = cb.get_stats()
        assert stats['success_count'] == 2
        assert stats['failure_count'] == 1
        assert stats['total_processed'] == 3
        assert stats['consecutive_failures'] == 1
