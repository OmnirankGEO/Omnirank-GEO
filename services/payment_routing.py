"""Unified payment channel routing.

This module is deliberately small and side-effect free. Production payment
entrypoints should call it instead of re-implementing UA heuristics.

口径 SSOT(改本文件前先读):
  · 资金侧权威 = ``docs/SYSTEM_TRUTH/08_billing.md`` §4.1「支付渠道路由」
  · 速查       = ``docs/AI-CONTEXT/GEO_PROJECT_FULL_MANUAL/CURRENT_*.md`` §5.6
四格,没有第五格:手机微信→JSAPI · 桌面微信→Native · PC→Native · 手机外部浏览器→虎皮椒。
前三格都是微信官方(公账),只有第四格是私账 —— **改路由等于改钱进哪个账**,不是纯技术改动。
"""

from typing import Optional


WECHAT_JSAPI = "wechat_jsapi"
WECHAT_NATIVE = "wechat_native"
XUNHUPAY = "xunhupay"

SUPPORTED_CHANNELS = {WECHAT_JSAPI, WECHAT_NATIVE, XUNHUPAY}
MOBILE_UA_TOKENS = (
    "android",
    "iphone",
    "ipad",
    "ipod",
    "mobile",
    "opera mini",
    "windows phone",
)

# [WO_MOBILE_PAY_JUMP 2026-08-07 §2.2] 桌面版微信内置浏览器的 UA 也带 MicroMessenger,
# 但它**不是**手机微信:WeixinJSBridge 在 PC 端拉起不可靠,表现正是"点了没反应"。
#   生产实证(2026-08-07 案例 A):用户 154 在 Mac 微信里 /customer/recharge 被路由 wechat_jsapi,
#   jsapi/create 返 200 但 ¥138 一直 pending;3 分钟后换安卓微信,¥100 同一条 jsapi 路 11 秒付成。
#   → 判据不是"能不能下单"(下单一直是 200),是**付款层弹不弹得出来**。
# 🔴 只列两个 token 就够:UnifiedPCMacWechat / UnifiedPCWindowsWechat 是它们的超串
#   ("unifiedpcmacwechat".find("macwechat") >= 0),多列反而让人以为漏一个就会漏检。
DESKTOP_WECHAT_UA_TOKENS = (
    "windowswechat",
    "macwechat",
)


def _normalize_ua(user_agent: Optional[str]) -> str:
    return (user_agent or "").lower()


def is_wechat_browser(user_agent: Optional[str]) -> bool:
    """任意微信内置浏览器(手机 + 桌面)。判"能不能用 JSAPI"要用下面那个。"""
    return "micromessenger" in _normalize_ua(user_agent)


def is_desktop_wechat_browser(user_agent: Optional[str]) -> bool:
    """Windows / Mac 桌面版微信的内置浏览器。"""
    ua = _normalize_ua(user_agent)
    return any(token in ua for token in DESKTOP_WECHAT_UA_TOKENS)


def is_mobile_wechat_browser(user_agent: Optional[str]) -> bool:
    """手机微信内置浏览器 —— 唯一能可靠调起 JSAPI 付款层的形态。"""
    return is_wechat_browser(user_agent) and not is_desktop_wechat_browser(user_agent)


def is_mobile_browser(user_agent: Optional[str]) -> bool:
    ua = _normalize_ua(user_agent)
    return any(token in ua for token in MOBILE_UA_TOKENS)


def detect_payment_channel(user_agent: Optional[str]) -> str:
    """Return the default production channel for a browser UA.

    Boss-approved routing:
    - Mobile WeChat embedded browser: official WeChat JSAPI
    - Desktop WeChat embedded browser: official WeChat Native QR(2026-08-07 新增)
    - Mobile external browser: xunhupay
    - Desktop or empty UA: official WeChat Native QR
    """
    # 🔴 顺序要紧:桌面微信同时满足 is_wechat_browser,必须先被摘出去。
    if is_desktop_wechat_browser(user_agent):
        return WECHAT_NATIVE
    if is_wechat_browser(user_agent):
        return WECHAT_JSAPI
    if is_mobile_browser(user_agent):
        return XUNHUPAY
    return WECHAT_NATIVE


def resolve_payment_channel(
    requested_channel: Optional[str],
    user_agent: Optional[str],
    *,
    force_xunhupay: bool = False,
) -> str:
    """Resolve an API-requested channel to the only allowed production paths.

    `force_xunhupay` is the emergency rollback switch. Without it, WeChat
    embedded browser cannot be explicitly routed to xunhupay because third-party
    pay links are not an approved main path there.
    """
    if force_xunhupay:
        return XUNHUPAY

    requested = (requested_channel or "auto").strip().lower()
    if requested == "auto":
        return detect_payment_channel(user_agent)

    if requested == "wechat_h5":
        raise ValueError("wechat_h5 is disabled for this merchant category")
    if requested == "alipay":
        raise ValueError("alipay is not available")
    if requested not in SUPPORTED_CHANNELS:
        raise ValueError(f"unsupported payment channel: {requested}")
    # 🔴 这里**故意**仍用 is_wechat_browser(含桌面微信),不跟着 detect 改成 is_mobile_wechat_browser:
    #   这条闸挡的是"微信生态内不许显式点第三方支付链接",桌面微信同样在微信生态内。
    #   detect 把桌面微信当 PC 是因为 JSAPI 拉不起来,两件事的理由不同,不要顺手对齐。
    if requested == XUNHUPAY and is_wechat_browser(user_agent):
        raise ValueError("wechat embedded browser must use wechat_jsapi")
    return requested
