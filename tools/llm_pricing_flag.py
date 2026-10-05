"""LLM-first 报价 feature flag 查询 · 30 秒内存缓存

老板 2026-05-12 拍板:默认 OFF · 白名单灰度 · 30 秒回滚

查询入口:
    from tools.llm_pricing_flag import is_llm_first_enabled
    if is_llm_first_enabled(agent_user_id=42):
        # 走 LLM-first 路径
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger("GEO-LLM-Flag")

_CACHE_TTL_SEC = 30.0
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.RLock()


def _get_setting(key: str, default: str = "") -> str:
    """从 system_settings 读 flag · 30 秒缓存

    cache miss / DB 异常 / 表不存在 → 返回 default(失败安全)
    """
    now = time.time()
    with _lock:
        if key in _cache:
            cached_at, val = _cache[key]
            if now - cached_at < _CACHE_TTL_SEC:
                return val

    val = default
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM system_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            if row:
                val = row["value"] if isinstance(row, dict) else row[0]
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("system_settings 查 %s 失败 · 用 default=%r · %s", key, default, exc)
        val = default

    with _lock:
        _cache[key] = (now, val)
    return val


def is_llm_first_enabled(agent_user_id: int | None = None) -> bool:
    """LLM-first 路径是否启用

    优先级:
    1. 总开关 LLM_FIRST_PRICING_ENABLED='true' · 全开
    2. 白名单 LLM_FIRST_PRICING_AGENT_WHITELIST 含 agent_user_id · 灰度
    3. 否则 false · 走老公式
    """
    global_on = _get_setting("LLM_FIRST_PRICING_ENABLED", "false").strip().lower() == "true"
    if global_on:
        return True

    if agent_user_id is None:
        return False

    raw = _get_setting("LLM_FIRST_PRICING_AGENT_WHITELIST", "[]")
    try:
        whitelist = json.loads(raw)
    except Exception:
        return False

    if not isinstance(whitelist, list):
        return False
    try:
        return int(agent_user_id) in [int(x) for x in whitelist]
    except Exception:
        return False


def is_fallback_to_formula_enabled() -> bool:
    """LLM 失败时是否降级公式 · 默认 true(不阻断业务)"""
    return _get_setting("LLM_FIRST_PRICING_FALLBACK_TO_FORMULA", "true").strip().lower() == "true"


# ============================================================
# [完整修复 P0-A/B/C 2026-06-13] 三杠杆灰度 flag · 默认全 false(=现状·0 变化)
#   绝不三个一起开(联合爆价)· 串行灰度 A→B→C · 切换需 clear_cache
# ============================================================

def is_cost_snapshot_enabled() -> bool:
    """P0-A:单篇媒体成本是否切到真实快照(media_cost_ssot × mhz_config.markup_ratio + overhead)。
    默认 false = 现状(MEDIA_TIER_COSTS 常量/B 档 ¥55)。"""
    return _get_setting("LLM_FIRST_PRICING_COST_SNAPSHOT_ENABLED", "false").strip().lower() == "true"


def is_value_evidence_gate_relaxed() -> bool:
    """P0-B:价值证据闸是否放行 vm 到 [1.0,1.5](保留 has_5118_evidence + 意图 cap 双 cap)。
    默认 false = 现状(无 5118 证据词 vm 钳 ≤1.0)。"""
    return _get_setting("LLM_FIRST_PRICING_VALUE_EVIDENCE_GATE_ENABLED", "false").strip().lower() == "true"


def is_national_unclamped_enabled() -> bool:
    """P0-C:全国词竞争是否放开交 LLM 推(national_unclamped)。
    默认 false = 现状(全国词钳 [实测×0.8,100] 紧)。"""
    return _get_setting("LLM_FIRST_PRICING_NATIONAL_UNCLAMPED_ENABLED", "false").strip().lower() == "true"


def get_soft_guard_range() -> tuple[int, int]:
    """获取标准版软护栏 [min_yuan, max_yuan] · 默认 (300, 8000)"""
    try:
        min_y = int(_get_setting("LLM_FIRST_PRICING_SOFT_GUARD_MIN_YUAN", "300"))
        max_y = int(_get_setting("LLM_FIRST_PRICING_SOFT_GUARD_MAX_YUAN", "8000"))
        return (min_y, max_y)
    except ValueError:
        return (300, 8000)


# ============================================================
# [P0-D 信任资产/引用难度因子 2026-06-14] 独立批 · 默认全 false(=现状·0 变化)
#   命名去 LLM_FIRST_ 前缀(P0-D 不属 LLM-first 流程·沿用非版本前缀先例
#   L0_QUOTE_MARKUP_SELF_EDIT_ENABLED / FORCE_XUNHUPAY)。
#   两 flag 分阶段(§8):先 COLLECT(D0 采集建数据)→ 再 ASSET(D2 报价消费)·绝不同批首开。
# ============================================================

def is_trust_collect_enabled() -> bool:
    """P0-D 采集闸(D0):诊断阶段是否顺手归并 search_citations 写 brand_trust_asset_snapshot。
    默认 false = 不采集(诊断 0 行为变化 / 0 额外 DB 写)。"""
    return _get_setting("PRICING_P0D_TRUST_COLLECT_ENABLED", "false").strip().lower() == "true"


def is_trust_asset_enabled() -> bool:
    """P0-D 报价消费闸(D2):报价是否读 trust 快照调难度侧篇数(true_competition)。
    默认 false = 报价 0 变化(不读快照 / 不调篇数 / 不进观测)。"""
    return _get_setting("PRICING_P0D_TRUST_ASSET_ENABLED", "false").strip().lower() == "true"


def clear_cache() -> None:
    """主动清缓存(测试用 / flag 立即生效)"""
    with _lock:
        _cache.clear()
