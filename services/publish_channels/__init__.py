"""发布渠道接入(开源版)。

选哪条渠道只看配置,没有任何默认值:

  PUBLISH_CHANNEL 不设 / 为空  → 没有接入:会走到渠道的入口一律 503(api/publish_channel_gate.py),
                                 不扣算力、不建任务行。
  PUBLISH_CHANNEL=dry_run      → 模拟发布:下单、状态、结果整条流程走完,不访问任何外部地址。
                                 订单号以 DRY- 开头,结果链接是 about:blank#dry-run-…,一看就知道是模拟。
  PUBLISH_CHANNEL=api          → 发布渠道 API 客户端:按公开契约 v1 调用一个第三方发布渠道。
                                 必须同时配 PUBLISH_CHANNEL_API_BASE(服务地址)与 PUBLISH_CHANNEL_API_KEY;
                                 缺一个就当作没接入。服务地址不提供默认值。

计费沿用应用现有的代发链:下单时扣算力,没发出去 / 撤单 / 渠道拒稿时原额退回。本包不碰计费。
客户端实现的方法与应用一直在调用的那一组相同(publish / publish_wemedia / 目录拉取 / 撤单 …),
所以应用里的代发路由、目录同步、状态回流都不用改。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("GEO-PublishChannel")

MODE_ENV = "PUBLISH_CHANNEL"
BASE_ENV = "PUBLISH_CHANNEL_API_BASE"
KEY_ENV = "PUBLISH_CHANNEL_API_KEY"

DRY_RUN = "dry_run"
API = "api"


# 读配置一律写字面量键名、不给默认值:开源导出按字面量把这几个键补进 .env.example(注释行),
# 导出闸「发布渠道包格」核这里没有任何默认地址 / 默认值。
def _mode() -> str:
    return (os.environ.get("PUBLISH_CHANNEL") or "").strip().lower()


def _base() -> str:
    return (os.environ.get("PUBLISH_CHANNEL_API_BASE") or "").strip()


def _key() -> str:
    return (os.environ.get("PUBLISH_CHANNEL_API_KEY") or "").strip()


def configured_mode() -> str | None:
    """当前接入的渠道:``dry_run`` / ``api`` / ``None``(没接入)。配了认不出的值 ⇒ 告警并按没接入处理。"""
    mode = _mode()
    if not mode:
        return None
    if mode == DRY_RUN:
        return DRY_RUN
    if mode == API:
        if _base() and _key():
            return API
        logger.warning("[publish-channel] PUBLISH_CHANNEL=api 但 %s / %s 没配齐 ⇒ 按没接入处理", BASE_ENV, KEY_ENV)
        return None
    logger.warning("[publish-channel] PUBLISH_CHANNEL=%r 认不出 ⇒ 按没接入处理", mode)
    return None


def api_settings() -> tuple[str, str]:
    """(服务地址, Key)。只在 configured_mode() == "api" 时调用。"""
    return _base().rstrip("/"), _key()


def get_client():
    """按配置返回渠道客户端;没接入 ⇒ 抛 ChannelNotConfigured(文字取唯一出处那一句)。"""
    mode = configured_mode()
    if mode == DRY_RUN:
        from .dry_run import DryRunClient

        return DryRunClient()
    if mode == API:
        from .api_client import ApiChannelClient

        base, key = api_settings()
        return ApiChannelClient(base, key)
    from services.meijiehezi.client import ChannelNotConfigured

    raise ChannelNotConfigured()
