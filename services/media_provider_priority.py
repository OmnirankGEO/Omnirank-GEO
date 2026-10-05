"""多渠道出货顺序（媒介盒子 / 快易播）。

Owner 口径（2026-07-27）：**媒介盒子先用完，之后快易播成为主力。**

实现成**同分下的兜底排序**，不是首位排序键 —— 如果把 provider 放在第一位，
会直接压过价格、被引强度、GEO 覆盖这些真正的排序信号，等于把推荐掀翻。
所以它只在其他条件打平时决定谁先出。

切换不靠人工记着改代码：``media_provider_priority`` 设置项一改即生效。
"""
from __future__ import annotations

import logging
from typing import Final

logger = logging.getLogger("GEO-MediaProviderPriority")

PROVIDER_MHZ: Final = "mhz"
PROVIDER_KYB: Final = "kyb"

#: 默认顺序：媒介盒子在前（先用完存量）。
DEFAULT_PRIORITY: Final[tuple[str, ...]] = (PROVIDER_MHZ, PROVIDER_KYB)

VALID_PROVIDERS: Final[frozenset[str]] = frozenset({PROVIDER_MHZ, PROVIDER_KYB})


def get_priority() -> tuple[str, ...]:
    """当前出货优先级。配置缺失/非法时回落默认（媒介盒子优先）。"""
    try:
        from config.settings_manager import load_settings

        raw = getattr(load_settings(), "media_provider_priority", None)
    except Exception as exc:
        logger.warning("[provider-priority] settings 不可用，用默认顺序: %s", exc)
        return DEFAULT_PRIORITY
    if not raw:
        return DEFAULT_PRIORITY
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        return DEFAULT_PRIORITY
    cleaned = [str(p).strip().lower() for p in raw if str(p).strip().lower() in VALID_PROVIDERS]
    if not cleaned:
        return DEFAULT_PRIORITY
    # 补齐没写到的渠道，保证每个 provider 都有确定名次
    for p in DEFAULT_PRIORITY:
        if p not in cleaned:
            cleaned.append(p)
    return tuple(cleaned)


def provider_rank(provider: str | None) -> int:
    """provider -> 名次（越小越先出）。未知 provider 排最后。"""
    order = get_priority()
    value = str(provider or PROVIDER_MHZ).strip().lower()
    try:
        return order.index(value)
    except ValueError:
        return len(order)


def order_by_clause(column: str = "provider") -> str:
    """生成 SQL 兜底排序片段，**接在既有 ORDER BY 末尾**。

    例：``ORDER BY quality DESC, price ASC, {provider_tiebreak}``
    """
    order = get_priority()
    whens = " ".join(
        f"WHEN '{p}' THEN {i}" for i, p in enumerate(order) if p in VALID_PROVIDERS
    )
    return f"CASE {column} {whens} ELSE {len(order)} END ASC"


def sort_key(row: dict) -> int:
    """给 Python 侧排序用的兜底键。"""
    return provider_rank((row or {}).get("provider"))
