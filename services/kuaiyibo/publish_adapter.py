"""第二发布渠道投递适配器(开源版空壳)。"""
from __future__ import annotations

from typing import Any

from services.meijiehezi.client import ChannelNotConfigured


class KuaiyiboPublishAdapter:
    def __init__(self, *args, **kwargs):
        raise ChannelNotConfigured()


def open_spend_summary() -> dict[str, Any]:
    """没有接入第二渠道,也就没有在这家的消耗。"""
    return {"billed_yuan": 0.0, "pending_yuan": 0.0, "refunded_yuan": 0.0, "orders": {}}
