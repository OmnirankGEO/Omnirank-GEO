"""第二发布渠道订单状态回流(开源版空壳):没有接入,没有在飞订单,各入口返回空汇总。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.meijiehezi.client import ChannelNotConfigured


@dataclass
class StatusSyncResult:
    checked: int = 0
    published: int = 0
    failed: int = 0
    still_processing: int = 0
    refunded_points: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"已发布{self.published} 已失败{self.failed} 处理中{self.still_processing} "
                f"退还{self.refunded_points}算力 错误{len(self.errors)}")


def fail_and_refund_unsubmitted(items: list[dict[str, Any]], submitted_media_ids: set[int],
                                reasons_by_media: dict[Any, str] | None = None) -> int:
    # 只会在「向第二渠道下单失败」之后被调用;开源版没有这条路,走到这里说明分流出了错 —— 大声失败,不静默吞掉
    raise ChannelNotConfigured()


def refund_stale_orders(*, max_age_hours: int = 24) -> StatusSyncResult:
    return StatusSyncResult()


def sweep_unrefunded_failed(*, max_age_days: int = 7) -> StatusSyncResult:
    return StatusSyncResult()


def sync_open_orders(client: Any | None = None) -> StatusSyncResult:
    return StatusSyncResult()


def apply_callback_batch(rows: list[dict[str, Any]]) -> StatusSyncResult:
    return StatusSyncResult()
