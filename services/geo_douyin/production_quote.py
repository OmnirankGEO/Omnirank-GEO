"""GEO 抖音图文 · **服务端总价**与价格指纹。

## 为什么要有这个模块(#150 §3.1 · P0 资金规则)

今天有**两处前端算钱**,不是一处:

1. `DouyinImagePost.tsx`(该页 09-08 已随 #150 §4 删除)的 `batchPrice = totalPrice * batchTotal`;
2. `/pricing` 端点的**契约本身** —— 它返回 `base` / `extra_card` / `included_cards`,
   注释明写「前端拿 `base + max(0, n-included) × extra` 自己算总价」。

只删第 1 处的乘法不够:第 2 处把公式写进了接口,
「按张加价」这条规则仍有第二份实现活在 tsx 里。
08_billing §3.3 的口径是 **「价格只在后端计算,前端只显示」**。

## 规则只许一份 —— 这一份已经存在

`services.geo_douyin.pricing` 的 docstring 自己就写明了理由:
加价额有三个调用方,**写三份就一定会漂,表现是前端显示 890、后端扣 790**。
本模块**调它**,不新写第二份公式。

## 指纹绑什么:数据 **和** 规则

🔴 只 hash 行数据是不够的。本仓有同族教训:版本串只 hash 数据行内容 ⇒
   **改规则不改数据时版本不动**,所有靠版本的闸静默失效。
   所以指纹里既有行(词/城市/张数),也有**当次读到的单价与套餐规模**。
   Owner 一调价,旧指纹立刻对不上 ⇒ 409 让前端重新取价,
   而不是「她看到 A、系统扣 B」。

## 逐行指纹 + 整批指纹

前端可能一行一行地发 `POST /posts`,所以**每行有自己的指纹**;
整批另给一个,供 §3.3 的 `/posts/batch` 用。
只给整批指纹的话,单条下单永远对不上。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from services.geo_douyin.config import (
    CARD_COUNT_INCLUDED,
    FEATURE_CODE_IMAGE_POST,
    FEATURE_CODE_IMAGE_POST_EXTRA_CARD,
)
from services.geo_douyin.pricing import (
    PricingUnavailable,
    clamp_card_count,
    extra_cards,
    read_unit_points,
)

#: 指纹口径版本。改了算法/绑定轴就 +1 —— 老指纹会全部对不上,那是**期望行为**
#: (与其让新旧口径悄悄共存,不如让前端重取一次价)。
FINGERPRINT_SCHEMA = "gdq1"


def _line_axes(row: Any) -> dict:
    """一行里**影响价格**的轴。

    🔴 只放影响价格的:把风格/画幅这类不影响价的也绑进去,
       用户换个画幅就 409,而价其实没变 —— 那是把闸变成噪音。
    """
    get = row.get if isinstance(row, dict) else (lambda k, d=None: getattr(row, k, d))
    return {
        "keyword": str(get("keyword", "") or "").strip(),
        "city": str(get("city", "") or "").strip(),
        "card_count": clamp_card_count(get("card_count", None)),
    }


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def line_fingerprint(row: Any, *, rule: dict) -> str:
    """单行指纹 = 该行的计价轴 + **当次的规则**。"""
    return _digest({"v": FINGERPRINT_SCHEMA, "line": _line_axes(row), "rule": rule})


def batch_fingerprint(rows: Iterable[Any], *, rule: dict) -> str:
    """整批指纹。**保序** —— 换了顺序就是另一批(避免两批互相顶替)。"""
    return _digest({"v": FINGERPRINT_SCHEMA,
                    "lines": [_line_axes(r) for r in (rows or [])],
                    "rule": rule})


async def read_rule() -> dict:
    """当次的计价规则输入(单价 + 套餐规模)。

    🔴 fail-closed:价目读不到就让 `PricingUnavailable` 抛出去,
       **绝不兜成 0**。按 0 算等于白送 —— 一次读库抖动就是资金漏洞,
       而「报错让用户重试一次」只是体验问题。
       (这条口径来自 `pricing.py`,本模块不许把它兜掉。)
    """
    base = await read_unit_points(FEATURE_CODE_IMAGE_POST)
    try:
        extra = await read_unit_points(FEATURE_CODE_IMAGE_POST_EXTRA_CARD)
    except PricingUnavailable:
        # 加价那行价目缺失**只在真的要加价时**才是错误 —— 与 pricing.extra_card_points
        # 的口径一致(选 4 张及以下的用户不该因为它没配好而下不了单)。
        # 这里记成 None,下面按行判:要加价却是 None ⇒ 抛。
        extra = None
    return {"base_points": int(base),
            "extra_card_points": (None if extra is None else int(extra)),
            "included_cards": int(CARD_COUNT_INCLUDED)}


async def quote_production(rows: Iterable[Any]) -> dict:
    """逐行单价 + 总算力 + 指纹。**服务端唯一的总价出口**。"""
    rows = list(rows or [])
    rule = await read_rule()
    lines = []
    total = 0
    for row in rows:
        axes = _line_axes(row)
        n_extra = extra_cards(axes["card_count"])
        if n_extra > 0 and rule["extra_card_points"] is None:
            raise PricingUnavailable(
                "价目缺失: %s" % FEATURE_CODE_IMAGE_POST_EXTRA_CARD)
        extra_points = n_extra * int(rule["extra_card_points"] or 0)
        line_points = int(rule["base_points"]) + extra_points
        total += line_points
        lines.append({
            **axes,
            "extra_cards": n_extra,
            "base_points": int(rule["base_points"]),
            "extra_points": extra_points,
            "line_points": line_points,
            "price_fingerprint": line_fingerprint(row, rule=rule),
        })
    return {
        "lines": lines,
        "total_points": total,
        "price_fingerprint": batch_fingerprint(rows, rule=rule),
        "rule": rule,
    }


async def fingerprint_matches(row: Any, expected: str) -> bool:
    """下单时比对。`expected` 空 = 前端没带(老前端)⇒ 视为匹配,不拦。

    🔴 只在**带了**指纹时才校验:老前端零改动仍能下单,
       与今天逐字节同行为。新前端带上之后才享受这道闸。
    """
    if not expected:
        return True
    return line_fingerprint(row, rule=await read_rule()) == str(expected)
