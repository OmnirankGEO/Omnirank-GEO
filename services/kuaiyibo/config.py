"""第二发布渠道配置(开源版空壳):没有接入,``is_configured()`` 恒为 False。

保留的只有应用其它部分引用的名字:渠道键、目录 id 偏移(本地主键区间的约定)与它的换算函数。
"""
from __future__ import annotations

from typing import Final

PROVIDER_KEY: Final = "kyb"
PROVIDER_LABEL: Final = "第二渠道"
LEGACY_PROVIDER_KEY: Final = "mhz"

#: 第二渠道的目录条目在本地主键里加这个偏移,与主渠道的 id 区间分开。
ID_OFFSET: Final = 100_000_000


def is_configured() -> bool:
    return False


def to_local_id(provider_media_id: int) -> int:
    return ID_OFFSET + int(provider_media_id)


def to_provider_id(local_id: int) -> int:
    return int(local_id) - ID_OFFSET


def is_kuaiyibo_local_id(local_id: int) -> bool:
    try:
        return int(local_id) > ID_OFFSET
    except (TypeError, ValueError):
        return False
