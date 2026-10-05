"""媒体平衡推荐 · 灰度开关（Owner 2026-07-29：「推荐还不成熟，先灰度」）。

沿用仓内既有范式（`report_writer_v2.is_report_v2_enabled`）：
全局开关默认关 + `{brand_ids, user_ids}` 白名单，settings 读不到就 fail-closed。

**关掉时推荐面必须逐位回到上线前**：

* 主干/垂类分组走老的 :data:`LEGACY_GENERIC_PLATFORMS` 名单（不是域族 role）；
* `_quality_score_v2f` 不传 ``success_factor``（该参数为 None 时公式逐位不变）；
* 不返回 ``ai_citation_trunk`` / ``combination_plan``，前端两块自然不渲染
  （渲染条件是 ``citationTrunk.length > 0`` 和 ``combinationPlan?.reason``）。

**不受本开关影响**（Owner 明示）：

* 严审媒体档 v2 + 下单前预审 —— 合规红线，且是把搜狐系 52.2% 拒稿拦在扣费前的，
  关掉等于把这笔钱放回去；
* T4 供给缺口表 / T5 发布→被引转化率 —— 只读报表，不影响代理决策路径；
* ``can_geo`` 死过滤器修复 —— 那是修 bug（全表 17712 行恒为 0，V1 库存池长期恒空），
  跟着灰度关回去等于主动把 bug 放回生产。
"""
from __future__ import annotations

import logging
from typing import Any, Final

logger = logging.getLogger("GEO-MediaBalanceGate")

MEDIA_BALANCE_GATE_VERSION: Final = "media-balance-gate-v1.0"

#: 上线前 `placement_service._GENERIC_PLATFORMS` 的原始名单，逐字保留。
#: 灰度关闭时必须用它分组，才谈得上"回到上线前口径"。
LEGACY_GENERIC_PLATFORMS: Final[frozenset[str]] = frozenset({
    '知乎', '搜狐', '今日头条', '抖音', '百度', '新浪网', '手机搜狐网',
    '微信公众平台', '新浪', '腾讯', '网易', '凤凰', '微博',
})


# ---- 判定理由（C-4）-------------------------------------------------------
# [2026-07-28 C-4] 原来「配置就是 false」和「settings 读不到」返回同一个 False。
# settings 服务抖一下 → 灰度悄悄全关 → 现象是"功能时有时无",排查会很久。
# 现在把两种 false 分开回带,接口侧一眼看出是配置关的还是读不到。
GATE_REASON_GLOBAL_ON: Final = "global_on"                   # 全局开关 True
GATE_REASON_WHITELIST_HIT: Final = "whitelist_hit"           # 全局关但命中白名单
GATE_REASON_NOT_IN_WHITELIST: Final = "not_in_whitelist"     # 全局关且没命中
GATE_REASON_CONFIGURED_OFF: Final = "configured_off"         # 明确配成关(白名单结构也不可用)
GATE_REASON_SETTINGS_UNREADABLE: Final = "settings_unreadable"  # ⚠️ 读不到 → fail-closed


def _norm_industry(value: Any) -> str:
    """行业键归一：去空白 + 转小写。配置侧和调用侧用同一把尺子，避免大小写/空格漏配。"""
    return str(value or "").strip().lower()


def evaluate_gate(
    *,
    user_id: int | None = None,
    brand_id: int | None = None,
    industry_key: Any = None,
    industry_keys: "list[Any] | tuple[Any, ...] | None" = None,
) -> dict[str, Any]:
    """**唯一判定入口**，返回 ``{"enabled": bool, "reason": str}``。

    三级控制（与 report v2 一致）：

    1. ``media_balance_enabled=True`` → 全量开；
    2. ``media_balance_enabled=False`` → 查白名单命中；
    3. 未配置 / settings 读不到 → **关**（保守，灰度期 fail-closed）。

    [2026-07-28 C-1] 放量维度改成 **industry_keys**：媒体平衡的效果按行业分化
    （被引主干在不同行业占比差很多）。按 brand 放量拿到的是一堆跨行业零散样本，
    组内方差大到读不出信号；按行业放量则同行业内新旧对照，几十条就能看出差异。

    ``user_ids`` 保留但**降级成调试后门**（强制把某账号拉进灰度好复现问题），不作放量维度。
    ``brand_ids`` 保留仅为兼容既有配置，**不推荐用于放量**。

    ``industry_keys`` 参数可一次传多个别名（如原始行业名 + 归一后的 matched_industry），
    任一命中即算命中 —— 调用侧不必猜配置里写的是哪个写法。
    """
    try:
        from config.settings_manager import load_settings

        settings = load_settings()
    except Exception as exc:  # pragma: no cover - settings 不可用时不该放行
        logger.warning("[media-balance-gate] settings 不可用，按关闭处理: %s", exc)
        return {"enabled": False, "reason": GATE_REASON_SETTINGS_UNREADABLE}

    flag = getattr(settings, "media_balance_enabled", None)
    if flag is True:
        return {"enabled": True, "reason": GATE_REASON_GLOBAL_ON}
    if flag is not False:
        # 字段整个没配出来（老 settings / 结构漂移）→ 与"读不到"同类，别装作是配置关的
        return {"enabled": False, "reason": GATE_REASON_SETTINGS_UNREADABLE}

    whitelist = getattr(settings, "media_balance_whitelist", None) or {}
    if not isinstance(whitelist, dict):
        logger.warning("[media-balance-gate] 白名单结构异常(%s)，按关闭处理", type(whitelist).__name__)
        return {"enabled": False, "reason": GATE_REASON_CONFIGURED_OFF}

    # (1) 放量维度：行业
    wanted = {_norm_industry(k) for k in (whitelist.get("industry_keys") or [])}
    wanted.discard("")
    if wanted:
        candidates = {_norm_industry(industry_key)}
        for k in (industry_keys or ()):
            candidates.add(_norm_industry(k))
        candidates.discard("")
        if candidates & wanted:
            return {"enabled": True, "reason": GATE_REASON_WHITELIST_HIT}

    # (2) 调试后门：把某个账号强制拉进灰度（不作放量维度）
    if user_id is not None and user_id in (whitelist.get("user_ids") or []):
        return {"enabled": True, "reason": GATE_REASON_WHITELIST_HIT}

    # (3) 兼容既有 brand 配置（不推荐用于放量：跨行业零散样本读不出信号）
    if brand_id is not None and brand_id in (whitelist.get("brand_ids") or []):
        return {"enabled": True, "reason": GATE_REASON_WHITELIST_HIT}

    return {"enabled": False, "reason": GATE_REASON_NOT_IN_WHITELIST}


def is_media_balance_enabled(
    *,
    user_id: int | None = None,
    brand_id: int | None = None,
    industry_key: Any = None,
    industry_keys: "list[Any] | tuple[Any, ...] | None" = None,
) -> bool:
    """新推荐口径对该代理/品牌/行业是否开启（`evaluate_gate` 的布尔投影）。

    签名向后兼容：只传 ``user_id`` / ``brand_id`` 的老调用行为不变。
    """
    return bool(evaluate_gate(
        user_id=user_id, brand_id=brand_id,
        industry_key=industry_key, industry_keys=industry_keys,
    )["enabled"])


def gate_status(
    *,
    user_id: int | None = None,
    brand_id: int | None = None,
    industry_key: Any = None,
    industry_keys: "list[Any] | tuple[Any, ...] | None" = None,
) -> dict[str, Any]:
    """给接口层带出灰度态 + **判定理由**，方便排查「为什么我看不到那两块」。"""
    verdict = evaluate_gate(
        user_id=user_id, brand_id=brand_id,
        industry_key=industry_key, industry_keys=industry_keys,
    )
    return {
        "media_balance_enabled": verdict["enabled"],
        "reason": verdict["reason"],
        "gate_version": MEDIA_BALANCE_GATE_VERSION,
    }
