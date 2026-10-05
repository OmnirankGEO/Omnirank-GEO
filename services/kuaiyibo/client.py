"""第二发布渠道客户端(开源版空壳):构造即抛 ``KuaiyiboError``,不含任何端点或鉴权。"""
from services.publish_channel_notice import CHANNEL_NOT_CONFIGURED


class KuaiyiboError(RuntimeError):
    pass


class KuaiyiboClient:
    def __init__(self, *args, **kwargs):
        raise KuaiyiboError(CHANNEL_NOT_CONFIGURED)
