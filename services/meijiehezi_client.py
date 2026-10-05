"""旧版发布渠道客户端入口(开源版空壳)。

与 ``services.meijiehezi.client`` 共用同一套空壳:可以构造,任何渠道方法都抛 ``ChannelNotConfigured``。
"""
from __future__ import annotations

from services.meijiehezi.client import (  # noqa: F401  (保留旧导入路径)
    ChannelNotConfigured, ConfirmationRequiredError, MeiJieHeZiClient, PublishError, SessionExpiredError,
)


class StatusSyncer:
    """旧版状态同步器空壳。"""

    def __init__(self, client, *args, **kwargs):
        self.client = client

    async def sync(self):
        raise ChannelNotConfigured()
