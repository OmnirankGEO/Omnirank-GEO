"""V5 · 飞轮总汇总 insight(AI 原生「总结节点」)。

老板要点③:飞轮多个节点要 LLM 支撑 + 一个总汇总,才是 AI 原生工具。
本模块把全景五节点 + 进化看板计数 + 趋势 + 数据健康(**全部真实数字**)聚合成一份 facts,
交 LLM 产出 3-5 条人话 insight + 一句总评。

🔴 铁律:
- **数字零 LLM 产**:facts 全来自现有只读聚合(SQL/统计);prompt 明令「只引用给定数字,禁止推算新数字」。
- **空数据不调 LLM**:facts 无有效数据(采集/学习/应用/候选全 0)→ 直接返回「数据积累中」,不编故事。
- **缓存**:按 facts 数据指纹 + TTL 24h;手动[刷新]强制重跑。数据不变二次调用零 LLM。
- **复用不重造**:子聚合走 V7 flywheel_cache 缓存包装版(不裸跑 board 重活);不新增平行聚合端点。
- **fail-soft**:LLM 失败/守卫拦截 → insights=[] + 数字仍照常返回,绝不阻断看板。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("GEO-WritingFlywheel.Insight")

_INSIGHT_TTL = 24 * 3600  # 24h
_INSIGHT_NEG_TTL = 600  # [review fix] 生成失败/守卫全拦的负缓存:10 分钟内不重烧 LLM
# fp -> (expires_at, payload)
_INSIGHT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _safe_health(industry_key: str | None) -> dict[str, Any]:
    """GEO 文章真相链健康（只读）。

    [504 治理 2026-08-18] 与 writing_evolution_board 共用同一个缓存 key —— 这两处在同一次
    页面加载里都要它,而它生产实测 4.2s/次。共享后一次加载只算一次(数值零变化)。
    """
    try:
        from services.article_data_health import get_article_data_health
        from writing.flywheel_cache import SCOPE_ARTICLE_HEALTH, get_or_compute, make_key

        return get_or_compute(make_key(SCOPE_ARTICLE_HEALTH), 60.0, get_article_data_health)
    except Exception as exc:
        logger.debug("[insight] health fail-soft: %s", str(exc)[:150])
        return {}


def _collect_facts(industry_key: str | None) -> dict[str, Any]:
    """聚合真实数字(走 V7 缓存包装版,复用 board/panorama,不裸跑重活)。"""
    from writing.flywheel_cache import (
        SCOPE_BOARD,
        SCOPE_OUTCOME,
        SCOPE_PANORAMA,
        SCOPE_TRENDS,
        get_or_compute,
        make_key,
    )

    from services.flywheel_panorama import get_flywheel_panorama, get_flywheel_trends
    from services.writing_evolution_board import get_evolution_board
    from services.article_experiment_registry import summarize_strict_experiment_outcomes

    # 🔴 [行业口径 2026-08-18] key 与端点 flywheel_panorama() 逐字同构(都带行业),否则
    #   insight 的 facts 取全站数、飞轮环取行业数 → 同一屏两套口径,比不过滤更糟。
    panorama = get_or_compute(
        make_key(SCOPE_PANORAMA, industry_key or "all"), 60, lambda: get_flywheel_panorama(industry_key)
    )
    board = get_or_compute(make_key(SCOPE_BOARD, industry_key or "all"), 60, lambda: get_evolution_board(industry_key))
    trends = get_or_compute(make_key(SCOPE_TRENDS, 12), 600, lambda: get_flywheel_trends(12))
    outcome = get_or_compute(
        make_key(SCOPE_OUTCOME, 30, 30), 300,
        summarize_strict_experiment_outcomes,
    )
    health = _safe_health(industry_key)

    nodes = {n["key"]: n for n in (panorama.get("nodes") or [])}
    cards = board.get("cards") or []
    state_counts: dict[str, int] = {}
    for c in cards:
        st = c.get("board_state") or "unknown"
        state_counts[st] = state_counts.get(st, 0) + 1

    series = trends.get("series") or []
    first_rate = float(series[0]["citation_rate"]) if series else 0.0
    last_rate = float(series[-1]["citation_rate"]) if series else 0.0
    measures = outcome.get("measures") or []
    rollback_flags = sum(1 for m in measures if m.get("effect_decision") == "FAIL")
    # [review fix · P1] 引用率未知(cited 查询失败)时喂 null 给 LLM,而非把假 0 当真实 0% 编故事
    citation_known = bool(panorama.get("citation_rate_known", True))

    return {
        "industry_key": industry_key or "all",
        "collected": int((nodes.get("collect") or {}).get("value") or 0),
        "learned": int((nodes.get("learn") or {}).get("value") or 0),
        "pending_review": int(panorama.get("pending_total") or 0),
        "applied_versions": int((nodes.get("apply") or {}).get("value") or 0),
        "citation_rate": round(float(panorama.get("citation_rate") or 0), 4) if citation_known else None,
        "citation_rate_trend": round(last_rate - first_rate, 4),
        "trend_weeks": len(series),
        "has_candidate_count": int(board.get("has_candidate_count") or 0),
        "recommend_replace_count": int(board.get("recommend_replace_count") or 0),
        "board_state_counts": state_counts,
        "eligible_outcome_versions": int(outcome.get("eligible_versions") or 0),
        "rollback_flags": int(rollback_flags),
        "health_can_activate": bool(health.get("style_effect_decision_allowed")),
        "health_blockers": len(health.get("blockers") or []),
        "health_summary": str(health.get("summary") or "")[:200],
    }


def _has_meaningful_data(facts: dict[str, Any]) -> bool:
    return any([
        facts.get("collected"), facts.get("learned"), facts.get("applied_versions"),
        facts.get("has_candidate_count"), facts.get("eligible_outcome_versions"),
    ])


def _facts_fingerprint(facts: dict[str, Any]) -> str:
    from writing.flywheel_llm import fingerprint

    return fingerprint(
        facts.get("industry_key"), facts.get("collected"), facts.get("learned"),
        facts.get("pending_review"), facts.get("applied_versions"),
        facts.get("citation_rate"), facts.get("citation_rate_trend"),
        facts.get("has_candidate_count"), facts.get("recommend_replace_count"),
        facts.get("eligible_outcome_versions"), facts.get("rollback_flags"),
        facts.get("health_can_activate"), facts.get("health_blockers"),
        json.dumps(facts.get("board_state_counts") or {}, sort_keys=True),
    )


def _parse_insights(raw: str) -> tuple[list[str], str]:
    """解析 LLM JSON {insights:[...], summary:"..."};容错 json_repair;失败回退空。"""
    text = (raw or "").strip()
    if not text:
        return [], ""
    data: Any = None
    try:
        data = json.loads(text)
    except Exception:
        try:
            from json_repair import repair_json

            data = json.loads(repair_json(text))
        except Exception:
            return [], ""
    if not isinstance(data, dict):
        return [], ""
    insights = [str(x).strip() for x in (data.get("insights") or []) if str(x or "").strip()][:5]
    summary = str(data.get("summary") or "").strip()
    return insights, summary


def _generate_insight_llm(facts: dict[str, Any]) -> tuple[list[str], str]:
    """调 LLM 产出 insight + 总评;守卫拦截/失败 → ([], "")。"""
    try:
        from writing.flywheel_llm import call_flywheel_llm_sync, guard_output

        system = (
            "你是 GEO 写作飞轮运营分析助手。根据给定的真实运营数字,产出 3-5 条简短 insight(每条一句人话,"
            "点出值得关注的变化或待决策事项)+ 一句总评。只引用给定数字,禁止推算或编造任何新数字;"
            "字段值为 null 表示该指标数据暂缺,不要就其编造或下结论;"
            "禁止提及任何 AI 模型或服务商名称;禁止承诺、保证类话术。"
            '严格返回 JSON:{"insights":["...","..."],"summary":"..."}'
        )
        user = "本期飞轮运营真实数字(只依据这些):\n" + json.dumps(facts, ensure_ascii=False, indent=2)
        raw = call_flywheel_llm_sync("flywheel_insight", system, user, max_tokens=500, json_mode=True)
        insights, summary = _parse_insights(raw or "")
        # [review fix] 逐条过守卫而非整段:整段守卫会让 1 条违规拖死全部 5 条 insight;
        # 命中条目单独丢弃,其余保留;summary 命中则置空(调用方有纯数字回退文案)。
        kept: list[str] = []
        for item in insights:
            cleaned, blocked = guard_output(item)
            if blocked:
                logger.warning("[insight] 单条守卫拦截丢弃: %s", item[:80])
            else:
                kept.append(cleaned)
        s_cleaned, s_blocked = guard_output(summary or "")
        return kept, ("" if s_blocked else s_cleaned)
    except Exception as exc:
        logger.warning("[insight] 生成失败(忽略): %s", str(exc)[:200])
        return [], ""


def get_flywheel_insight(industry_key: str | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """飞轮总汇总。数字永远真实;insights/summary 是 LLM 解释层(空数据不调 / 缓存 24h / 手动刷新)。"""
    facts = _collect_facts(industry_key)
    data_available = _has_meaningful_data(facts)
    base = {
        "industry_key": industry_key or "all",
        "facts": facts,
        "data_available": data_available,
    }
    if not data_available:
        # 空数据不调 LLM,不编故事
        return {**base, "insights": [], "summary": "数据积累中:先跑调研采集与蒸馏,飞轮转起来后这里会自动出总结。",
                "generated": False, "cached": False, "updated_at": None}

    fp = _facts_fingerprint(facts)
    now = time.time()
    hit = _INSIGHT_CACHE.get(fp)
    if hit is not None and hit[0] > now and not refresh:
        return {**base, **hit[1], "cached": True}

    insights, summary = _generate_insight_llm(facts)
    payload = {
        "insights": insights,
        "summary": summary or ("本期共 %d 条采集、%d 个候选待决策。" % (
            facts.get("collected", 0), facts.get("has_candidate_count", 0))),
        "generated": bool(insights or summary),
        "updated_at": int(now),
    }
    if insights or summary:
        _INSIGHT_CACHE[fp] = (now + _INSIGHT_TTL, payload)
    else:
        # [review fix] 负缓存(短 TTL):守卫全拦/LLM 失败时,GET 冷缓存路径否则每次页面加载都重烧一次
        # LLM(「同数据零 LLM」承诺破防)。短期静默,手动 refresh=True 可随时强制重试。
        _INSIGHT_CACHE[fp] = (now + _INSIGHT_NEG_TTL, payload)
    return {**base, **payload, "cached": False}


def clear_insight_cache() -> None:
    _INSIGHT_CACHE.clear()
