"""发布渠道客户端(开源版空壳)。

开源版没有接入任何外部发布渠道:本模块只保留应用其它部分引用的类名,不含端点、登录、令牌或抓取。
构造客户端不会出错;调用它的任何渠道方法都抛 :class:`ChannelNotConfigured`(是 ``SessionExpiredError``
的子类 —— 调用方本来就把「会话不可用」当成外调前失败处理:不扣钱、不改状态)。
接入自己的渠道:实现同名方法,或在 ``services/defensive_geo/publish/provider_transport.py`` 登记一个适配器。
"""
from __future__ import annotations

from dataclasses import dataclass

from services.publish_channel_notice import CHANNEL_NOT_CONFIGURED


class SessionExpiredError(Exception):
    """渠道会话不可用。"""


class ChannelNotConfigured(SessionExpiredError):
    """开源版:没有接入发布渠道。"""

    def __init__(self, message: str = CHANNEL_NOT_CONFIGURED):
        super().__init__(message)


class PublishError(Exception):
    def __init__(self, code: int, msg: str):
        self.code, self.msg = code, msg
        super().__init__(f"发布失败 [{code}]: {msg}")


class ConfirmationRequiredError(Exception):
    def __init__(self, code: int, msg: str, field: str, prior_confirms: dict | None = None):
        self.code, self.msg, self.field = code, msg or "", field
        self.prior_confirms = prior_confirms or {}
        super().__init__(f"需要确认 [{code}] {field}: {msg}")


class AmbiguousResponseError(Exception):
    def __init__(self, code: int, msg: str, raw_data=None):
        self.code, self.msg, self.raw_data = code, msg or "", raw_data
        super().__init__(f"结果不明 [{code}]: {msg}")


class PartialSuccessError(Exception):
    def __init__(self, code: int, msg: str, selected_num: int, success_count: int, raw_data=None):
        self.code, self.msg, self.raw_data = code, msg or "", raw_data
        self.selected_num, self.success_count = int(selected_num or 0), int(success_count or 0)
        super().__init__(f"部分接单 [{code}]: {msg}")


@dataclass
class PublishResult:
    success: bool
    code: int
    msg: str
    selected_num: int = 0
    success_count: int = 0
    order_sn: str = ""
    raw_data: dict = None
    order_sn_map: dict = None


class MeiJieHeZiClient:
    """渠道客户端空壳:可以构造、可以关闭;其余任何方法调用都抛 ChannelNotConfigured。"""

    def __init__(self, session_id: str = "", base_url: str = ""):
        self.base_url = ""

    async def close(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        async def _unavailable(*_args, **_kwargs):
            raise ChannelNotConfigured()

        return _unavailable
