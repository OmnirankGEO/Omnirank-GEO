"""GEO 抖音图文 · 张数计价的【唯一计算处】

Owner 2026-08-03 拍板的套餐口径:
  - 套餐**含 4 张**(`CARD_COUNT_INCLUDED`);
  - 每多一张按 `feature_pricing` 里 `geo_douyin_image_post_extra_card` 的单价加价;
  - **少于 4 张不减价**(基础价买的是"一组",不是按张零售);
  - 重新生成(regen)**也按张数加价**,与首次制作共用同一个每张单价。

🔴 为什么单独一个模块:加价额有三个调用方(下单 / 重新生成 / `/pricing` 展示)。
   写三份就一定会漂 —— 前端显示 890、后端扣 790 这种事只要有两份实现就会发生。
   这里是唯一计算处,三方都调它。

🔴 价目一律从 `feature_pricing` 读(元指令 1:价目表 SSOT 唯一权威源)。
   本模块**不出现任何价格数字**,只出现"含几张"这个套餐规模。
   规模是产品口径不是价格,所以它在 config,不在这里也不在价目表。

🔴 fail-closed:价目读不到就抛,**绝不按 0 算**。按 0 算等于白送 ——
   一个读库瞬时故障就能变成资金漏洞,而"报错让用户重试一次"只是体验问题。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from services.geo_douyin.config import (
    CARD_COUNT_INCLUDED,
    CARD_COUNT_MAX,
    CARD_COUNT_MIN,
    FEATURE_CODE_IMAGE_POST_EXTRA_CARD,
)

logger = logging.getLogger("GEO-Douyin-Pricing")


class PricingUnavailable(RuntimeError):
    """价目读不到。调用方必须让这次操作失败,不许按 0 继续。"""


def clamp_card_count(card_count: Optional[int]) -> int:
    """把用户传的张数收进合法区间。区间外不报错,夹住即可(它不是资金量)。"""
    try:
        n = int(card_count or CARD_COUNT_INCLUDED)
    except (TypeError, ValueError):
        n = CARD_COUNT_INCLUDED
    return max(CARD_COUNT_MIN, min(n, CARD_COUNT_MAX))


def extra_cards(card_count: Optional[int]) -> int:
    """超出套餐的张数。

    🔴 `max(0, ...)` 就是"少于 4 张不减价"这条口径在代码里的样子 ——
       选 1 张时它返回 0(不是 -3),所以加价额是 0 而不是负数抵扣。
    """
    return max(0, clamp_card_count(card_count) - CARD_COUNT_INCLUDED)


def _read_unit_points_sync(feature_code: str) -> int:
    from db.wallet_db import get_feature_pricing

    row = get_feature_pricing(feature_code)
    if not row:
        raise PricingUnavailable(f"价目缺失: {feature_code}")
    points = int(row.get("cost_points") or 0)
    if points <= 0:
        # 0 或负数不是"免费",是价目没配好。免费要靠不调用本函数来表达。
        raise PricingUnavailable(f"价目异常: {feature_code} cost_points={points}")
    return points


async def read_unit_points(feature_code: str) -> int:
    """读某个 feature_code 的单价(积分)。同步 DB 调用必须 to_thread。"""
    try:
        return await asyncio.to_thread(_read_unit_points_sync, feature_code)
    except PricingUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - 统一成一种失败,调用方只需处理一类
        raise PricingUnavailable(f"价目读取失败 {feature_code}: {str(e)[:120]}") from e


async def extra_card_points(card_count: Optional[int]) -> int:
    """这一单的**加价额**(积分)。基础价不含在内 —— 那是 freeze_points 自己按
    feature_code 查的,本函数只算 `extra_cost` 那部分。

    🔴 加价 0 张时**不读价目**:选 4 张及以下的用户不该因为
       `extra_card` 那行价目没配好而下不了单。
    """
    n = extra_cards(card_count)
    if n <= 0:
        return 0
    unit = await read_unit_points(FEATURE_CODE_IMAGE_POST_EXTRA_CARD)
    return n * unit


# 🔴 这里原来还有一个 `describe_card_pricing()` —— 接线检查抓出它**零调用点**,
#    已删除。写它的初衷是"展示用价目也走唯一计算处",但 `/pricing` 端点
#    实际要一次性返回五个 feature_code(首次/重做/加张/重抽/资料补全),
#    自己用 asyncio.gather 拼了,于是这个函数从第一天起就没人调 ——
#    它本身反而成了那个"没人用的第二份实现"。
#
#    留下这段注释而不是静悄悄删掉:死函数能混进来,说明"新模块要进接线检查表"
#    这条(本文件旁边的 wiring_check_geovid.py 里写着)第三次被漏。
#    本包的五个新模块已补进那张表。
