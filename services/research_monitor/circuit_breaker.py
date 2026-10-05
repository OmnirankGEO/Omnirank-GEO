"""
跑批级熔断保护

跑批 4 平台 x 25 prompts x 17 行业 = 1700 calls,
连续 50 次失败 OR 整体失败率超 50%(跑过 100 次后判)就熔断,
防止 30 分钟全失败浪费钱。

CircuitBreaker 实例化在 round_runner stage 1 开头,
每次 fetch 后调 record_success / record_failure,
循环开头调 is_tripped() 判断要不要 break。
"""
from typing import Optional, Tuple, Dict


class CircuitBreaker:
    """
    consecutive_threshold: 连续失败 N 次熔断 (默认 50)
    rate_threshold: 整体失败率超此比例熔断 (默认 0.5 = 50%)
    min_processed: 至少跑过 N 次才开始判 rate (默认 100, 防早期波动误熔断)
    """

    def __init__(
        self,
        consecutive_threshold: int = 50,
        rate_threshold: float = 0.5,
        min_processed: int = 100,
    ):
        self.consecutive_threshold = consecutive_threshold
        self.rate_threshold = rate_threshold
        self.min_processed = min_processed
        self.success_count = 0
        self.failure_count = 0
        self.consecutive_failures = 0

    def record_success(self) -> None:
        """记一次成功调用,重置 consecutive 计数"""
        self.success_count += 1
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        """记一次失败调用,累加 consecutive 计数"""
        self.failure_count += 1
        self.consecutive_failures += 1

    def is_tripped(self) -> Tuple[bool, Optional[str]]:
        """
        返回 (是否熔断, 原因)
        - (True, 'consecutive'): 连续失败超阈值
        - (True, 'rate'): 整体失败率超阈值
        - (False, None): 未熔断

        consecutive 优先于 rate 判断。
        """
        # 1. 连续失败优先判
        if self.consecutive_failures >= self.consecutive_threshold:
            return (True, 'consecutive')

        # 2. 整体失败率(必须跑够 min_processed 才判,防早期波动)
        total = self.success_count + self.failure_count
        if total >= self.min_processed:
            rate = self.failure_count / total
            if rate > self.rate_threshold:
                return (True, 'rate')

        return (False, None)

    def get_stats(self) -> Dict:
        """返回当前统计供日志/UI 显示"""
        total = self.success_count + self.failure_count
        tripped, reason = self.is_tripped()
        return {
            'success_count': self.success_count,
            'failure_count': self.failure_count,
            'total_processed': total,
            'consecutive_failures': self.consecutive_failures,
            'failure_rate': self.failure_count / total if total > 0 else 0.0,
            'tripped': tripped,
            'trip_reason': reason,
        }
