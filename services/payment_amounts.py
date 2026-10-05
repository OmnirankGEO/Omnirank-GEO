"""支付渠道金额换算:元 ↔ 分,精确,不许截断(WO_299)。

为什么要有这个模块:`services/wechat_pay.py` 的 Native / H5 下单原来写 `int(total_yuan * 100)`。
浮点下 `1.15 * 100 == 114.99999999999999`,截断成 114 分 —— 客户少付 1 分,
而回调核「实付 == 应付」,判金额不一致、拒绝入账 = 付了钱没到账。同文件 JSAPI / 退款用的是 `round`,
对合法两位小数是对的,但会把「本来就不是两位小数」的金额悄悄抹平。

口径照 `services/refund_cash_execution._yuan_to_cents`:经 `str()` 转 Decimal(浮点取最短表示,`1.15` 就是 "1.15"),
**只收恰好两位以内小数、有限、非负**的金额,其余一律抛 `ValueError` —— 宁可下单失败,也不发出一个错的金额。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

_CENT = Decimal("0.01")


def yuan_to_fen(value: Any) -> int:
    """元 → 分。`bool` / 非有限 / 负数 / 超过两位小数 ⇒ ValueError。"""
    if isinstance(value, bool):
        raise ValueError(f"金额不是数值:{value!r}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"金额无法解析:{value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"金额不是有限数:{value!r}")
    if amount < 0:
        raise ValueError(f"金额不能为负:{value!r}")
    if amount != amount.quantize(_CENT):
        raise ValueError(f"金额超过两位小数:{value!r}")
    return int(amount * 100)


def fen_to_yuan_str(fen: int) -> str:
    """分 → 恰好两位小数的元字符串(给要求 `"12.34"` 形式的渠道参数用)。"""
    if isinstance(fen, bool) or not isinstance(fen, int) or fen < 0:
        raise ValueError(f"分必须是非负整数:{fen!r}")
    return f"{fen // 100}.{fen % 100:02d}"
