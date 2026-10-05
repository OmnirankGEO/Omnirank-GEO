"""双价目表 SSOT 总闸读取(默认关 · migration seed 'false')

三总闸(system_settings):
  - PRICING_DUAL_SSOT_ENABLED : 双价目表报价/目录读取入口是否启用
  - CHANNEL_PRICING_ENABLED   : 直属渠道分级收益是否启用(关=扁平只认第一层=现状)
  - PRICING_QUOTE_REQUIRED    : 下单是否强制 price_quote_id(灰度切流后翻)

默认全关 → 现有链路零行为变化(宽进严出)。资金开关每次权威直读，蓝绿无进程缓存陈旧窗。
"""

import logging

from db.connection import get_db

logger = logging.getLogger("GEO-PricingSSOTFlags")

_FLAG_KEYS = ("PRICING_DUAL_SSOT_ENABLED", "CHANNEL_PRICING_ENABLED", "PRICING_QUOTE_REQUIRED")


class PricingFlagsUnavailable(RuntimeError):
    """The rollout policy could not be read authoritatively; financial writes must stop."""


def _load_flags(cur=None) -> dict:
    out = {k: False for k in _FLAG_KEYS}
    try:
        if cur is not None:
            cur.execute(
                "SELECT key, value FROM system_settings WHERE key = ANY(%s)", (list(_FLAG_KEYS),)
            )
            for row in cur.fetchall():
                out[row["key"]] = str(row["value"]).strip().lower() in ("true", "1", "yes", "on")
        else:
            with get_db() as conn:
                db_cur = conn.cursor()
                db_cur.execute(
                    "SELECT key, value FROM system_settings WHERE key = ANY(%s)",
                    (list(_FLAG_KEYS),),
                )
                for row in db_cur.fetchall():
                    out[row["key"]] = str(row["value"]).strip().lower() in (
                        "true", "1", "yes", "on"
                    )
    except Exception as exc:  # noqa: BLE001
        logger.error("pricing_ssot_flags 权威读取失败 · 拒绝推断为全部关闭 · %s", exc)
        raise PricingFlagsUnavailable("定价开关状态暂不可用") from exc
    return out


def pricing_flags_snapshot(cur=None) -> dict:
    """Return one authoritative snapshot; financial gates never use stale cache state."""
    return _load_flags(cur=cur)


def dual_ssot_enabled() -> bool:
    return bool(pricing_flags_snapshot().get("PRICING_DUAL_SSOT_ENABLED", False))


def channel_pricing_enabled() -> bool:
    return bool(pricing_flags_snapshot().get("CHANNEL_PRICING_ENABLED", False))


def quote_required() -> bool:
    return bool(pricing_flags_snapshot().get("PRICING_QUOTE_REQUIRED", False))


def invalidate() -> None:
    """Compatibility hook retained for callers; strict reads have no process cache."""
    return None
