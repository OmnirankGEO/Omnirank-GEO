"""[WO-KYB D0-b 方案 A] 媒体价格对外投影 —— 进货价与加价率都不出后端。

纯函数 + 一个读配置的 markup 取值口。放在 services 层是为了让
`api/meijiehezi_api.py`(目录接口)与 `api/publish_api.py`(AI 推荐链)
**共用同一口径** —— 两条链各写一套是这个 bug 当初能长出来的原因。

口径铁律:售价算力 = ceil(进货价 × markup × 130)。
这与 `api/meijiehezi_api.py::_recompute_publish_charge`(服务端权威扣费)
和前端历史 `yuanToPoints` 三处必须一致,否则显示价与真扣费对不上。
"""
from __future__ import annotations

import math
from typing import Any, Iterable

POINTS_PER_YUAN = 130

#: 成本侧字段全集 —— 这些一个都不许出后端。
#: 只留 our_price_*(售价侧)与本模块算出来的 price_points。
COST_SIDE_FIELDS: tuple[str, ...] = (
    "price", "price1", "price2",
    "price_normal", "price_vip", "price_svip",
    "video_price", "weitoutiao_price",
    "hepai_price", "hepai_price1", "hepai_price2",
    "cost_yuan", "wholesale_cents", "wholesale_points", "platform_cost_cents",
)

#: 售价侧字段(可以外露),按优先级 —— 有现成售价就别拿进货价乘。
_SELL_SIDE_YUAN_FIELDS = ("our_price_yuan",)
_SELL_SIDE_POINTS_FIELDS = ("our_price_points",)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def yuan_to_points(yuan: Any, markup: float) -> int:
    """进货价(元)→ 售价算力。ceil,与扣费口径逐位一致。"""
    v = _num(yuan, 0.0) * _num(markup, 0.0) * POINTS_PER_YUAN
    return int(math.ceil(v)) if v > 0 else 0


def points_to_yuan(points: Any, markup: float) -> float:
    """售价算力 → 进货价(元),供 DB 层内部比较用(DB 仍按进货价存)。"""
    denom = _num(markup, 0.0) * POINTS_PER_YUAN
    return (_num(points, 0.0) / denom) if denom > 0 else 0.0


def public_sort_key(sort_by: str) -> str:
    """对外排序键 price_points → 库内仍按 price 排。

    price_points = ceil(price × markup × 130) 对 price **单调不减**,
    所以两者排序结果等价,不必改 SQL 表达式。
    (markup > 0 是前提;markup <= 0 时全为 0,任何顺序都平凡等价。)
    """
    return "price" if sort_by in ("price_points", "price") else sort_by


def resolve_price_points(row: dict, markup: float) -> int:
    """算一行的对外售价算力。

    优先级:已有售价算力 > 售价元 × 130 > 进货价 × markup × 130。
    🔴 最后那档必须乘 markup —— 原 `services/publish_recommendation.py::_points`
    在回落到进货价时写的是 `price_yuan × 130`(漏了 markup),
    算出来的售价会少收 markup 倍。本函数是那处的统一口径。
    """
    for f in _SELL_SIDE_POINTS_FIELDS:
        v = _num(row.get(f), 0.0)
        if v > 0:
            return int(round(v))
    for f in _SELL_SIDE_YUAN_FIELDS:
        v = _num(row.get(f), 0.0)
        if v > 0:
            return int(math.ceil(v * POINTS_PER_YUAN))
    return yuan_to_points(row.get("price"), markup)


#: 「性价比之王」判据区间(**建立在进货价上**:¥30-60 那档有 58.8% 成功率的实证)。
#: 进货价不再出后端 → 这个判断必须跟着挪到服务端,前端只收布尔。
SWEET_SPOT_YUAN_RANGE = (30.0, 60.0)


def is_sweet_spot(row: dict) -> bool:
    """只判价格档;引擎覆盖数(geo_rank_platform)是公开字段,仍由前端合并判断。"""
    lo, hi = SWEET_SPOT_YUAN_RANGE
    p = _num(row.get("price"), -1.0)
    return lo <= p <= hi


def project_rows(rows: Iterable[dict], markup: float) -> list:
    """目录行投影:补 price_points 与 is_sweet_spot,删全部成本侧字段。

    🔴 顺序要紧:两个派生字段都依赖 price,**必须在 pop 之前算完**。
    """
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            row["price_points"] = resolve_price_points(row, markup)
            row["is_sweet_spot"] = is_sweet_spot(row)
            for f in COST_SIDE_FIELDS:
                row.pop(f, None)
        out.append(row)
    return out


def project_payload(obj: Any, markup: float) -> Any:
    """递归投影,给 AI 推荐链那种**嵌套且形状不固定**的 payload 用。

    判定「这是一行媒体」的依据 = 它带任何一个成本侧字段或售价侧字段。
    只按 `price` 判会漏掉那些只有 our_price_* 的行(它们也需要 price_points)。
    """
    if isinstance(obj, dict):
        looks_like_media = any(f in obj for f in COST_SIDE_FIELDS) or any(
            f in obj for f in (_SELL_SIDE_POINTS_FIELDS + _SELL_SIDE_YUAN_FIELDS)
        )
        if looks_like_media:
            obj["price_points"] = resolve_price_points(obj, markup)
            for f in COST_SIDE_FIELDS:
                obj.pop(f, None)
        for k in list(obj.keys()):
            obj[k] = project_payload(obj[k], markup)
        return obj
    if isinstance(obj, list):
        return [project_payload(x, markup) for x in obj]
    return obj


def get_markup() -> float:
    """媒介盒子加价比例 SSOT(mhz_config.markup_ratio),与扣费同源。"""
    try:
        from db.meijiehezi_db import get_config
        v = get_config("markup_ratio")
        if v:
            return float(v)
    except Exception:
        pass
    try:
        from config.pricing_config import get_media_markup_default
        return float(get_media_markup_default())
    except Exception:
        return 1.5
