"""Publish recommendation scoring and package assembly for V2.3."""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

from services.article_type import (
    ARTICLE_TYPE_LABELS,
    ARTICLE_TYPE_WEIGHTS,
    ArticleType,
    normalize_article_type,
)
from writing.feature_switches import is_feature_enabled


ANALYSIS_VERSION = "v2.3"

# [E2] 归一后 shadow_score(/100 → 0..1)在排序键里的权重上限。
# 刻意取小值:只用于「同有效分」候选之间按飞轮有效分微重排,绝不主导排序(有效分本体量级 0-100+)。
ENTITY_RANK_BOOST_WEIGHT = 3.0
DISCLAIMER_VERSION = "publish-risk-v1"
TERMS_VERSION = "2026-05-08"
DAILY_AI_RECOMMENDATION_QUOTA = 50

NOISE_PATTERNS = (
    "套餐系列",
    "随机",
    "任选",
    "秒杀",
    "包收录",
    "十元专区",
    "十元",
    "小站",
    "低价",
    "包月套餐",
    "包月",
)

INDUSTRY_ANCHORS = {
    "房地产": ("房天下", "吉屋", "和讯网"),
    "汽车": ("懂车帝", "汽车之家", "太平洋汽车"),
    "GEO": ("CSDN", "IT之家", "IT 之家", "搜狐", "知乎"),
    "企业服务": ("CSDN", "IT之家", "IT 之家", "搜狐", "知乎"),
    "医疗": ("健康", "医疗", "医药", "政府", "人民网", "新华网"),
    "医疗健康": ("健康", "医疗", "医药", "政府", "人民网", "新华网"),
    "教育": ("中国教育在线", "环球网校", "新东方"),
    "教育培训": ("中国教育在线", "环球网校", "新东方"),
}

from services.monitoring_identity_review import aggregate_eligible_sql

MONITORING_OUTCOME_SQL = f"""
SELECT mr.platform AS engine,
       SUM(CASE WHEN COALESCE(mr.is_detected, 0) = 1 THEN 1 ELSE 0 END)::int AS ai_citations,
       ROUND(AVG(CASE WHEN COALESCE(mr.is_detected, 0) = 1 THEN 100 ELSE 0 END)::numeric, 2)
           AS monitoring_brand_score
  FROM public.monitoring_results mr
  JOIN public.monitoring_tasks mt ON mt.id = mr.task_id
 WHERE mt.brand_id = %s
   AND mr.keyword = ANY(%s)
   AND mr.platform IN ('dashscope','deepseek','kimi','doubao')
   AND mr.tested_at BETWEEN %s AND %s + INTERVAL '30 days'
   AND {aggregate_eligible_sql('mr')}
 GROUP BY mr.platform
"""


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm_name(row: dict[str, Any]) -> str:
    return str(
        row.get("platform_name")
        or row.get("media_name")
        or row.get("toutiao_name")
        or row.get("name")
        or ""
    ).strip()


def _resolve_price_yuan(row: dict[str, Any]) -> float | None:
    """价格解析。**取不到返回 None** —— "没录价"与"0 元"必须分得开。

    🔴 旧实现用 `_num(value, -1) >= 0` 判定"取到了",于是 `our_price_yuan = 0`
       会被当成一个**合法价格**直接返回,后面的 `price`/`price1` 回退根本走不到。
       而 replica 实测:`mhz_media` active 50974 条里 `our_price_yuan = 0` 有
       **16636 条(32.6%)**,真正落在 (0,5) 区间的只有 **129 条** ——
       **0 是「没录」不是「免费」**。
       把没录价当成 0 元,再用 `price < 5` 打 `price_too_low` 一票否决,
       等于把三成库存判成"太便宜不能推"。(2026-08-09 P0:315 条候选被这么误杀。)
    """
    for key in ("our_price_yuan", "price", "price1", "cost_yuan"):
        value = _num(row.get(key), -1)
        if value > 0:
            return value
    points = _num(row.get("our_price_points") or row.get("cost_points"), 0)
    return points / 130 if points > 0 else None


def _price_yuan(row: dict[str, Any]) -> float:
    """展示/积分换算用:签名不变,取不到时给 0。

    顺带修好一个展示 bug:`our_price_yuan = 0` 但 `price = 300` 的行,
    旧实现会一路返回 0(卡在第一个字段),现在会继续回退拿到 300。
    """
    value = _resolve_price_yuan(row)
    return value if value is not None else 0.0


def _points(row: dict[str, Any]) -> int:
    direct = _num(row.get("our_price_points") or row.get("cost_points"), 0)
    if direct > 0:
        return int(round(direct))
    return int(round(_price_yuan(row) * 130))


def _has_noise(name: str) -> bool:
    return any(pattern in name for pattern in NOISE_PATTERNS)


def _has_dual_signal(row: dict[str, Any]) -> bool:
    geo_rank = row.get("geo_rank")
    portal = str(row.get("portal_media") or "").strip()
    return bool(portal) or _num(geo_rank, 0) > 0


def _industry_anchor_score(name: str, industry: str) -> float:
    for key, anchors in INDUSTRY_ANCHORS.items():
        if key in (industry or ""):
            if any(anchor.replace(" ", "") in name.replace(" ", "") for anchor in anchors):
                return 16.0
    return 0.0


def history_feedback_weight(sample_count: int) -> float:
    """V2.1 sigmoid ramp: 0 below 500, 5% at 1000, almost 10% after 1500."""
    if sample_count <= 500:
        return 0.0
    return 0.10 * (1 / (1 + math.exp(-((sample_count - 1000) / 160))))


def score_media_candidate(
    row: dict[str, Any],
    *,
    sample_count: int = 0,
    median_price_yuan: float | None = None,
    industry: str | None = None,
) -> dict[str, Any]:
    name = _norm_name(row)
    resolved_price = _resolve_price_yuan(row)
    price_known = resolved_price is not None
    price = resolved_price if price_known else 0.0
    reasons: list[str] = []
    risk_tags: list[str] = []

    if not name:
        risk_tags.append("missing_name")
    if _has_noise(name):
        risk_tags.append("noise_name")
    # 🔴 只有**真取到价**才谈"太便宜"。没录价不是风险,是信息缺失 ——
    #    risk_tags 非空即一票否决,把缺失塞进来等于用缺失杀掉候选。
    if price_known and price < 5:
        risk_tags.append("price_too_low")
    if price > 100000:
        risk_tags.append("price_too_high")
    if not _has_dual_signal(row):
        risk_tags.append("missing_quality_signals")

    # 🔴🔴 证据分:行里已经带了就用,不再从 citation_rate 重算。
    #
    #   为什么:`media_effective_pool` **没有 citation_rate 列**,投影自然也带不出来
    #   (db/publish_db.py 的 _POOL_CANDIDATE_SQL 投的是 `p.*` + mhz 侧字段)。
    #   于是请求时重算恒 `citation_rate = 0` → `evidence_score = 0`,而它占 0.45 权重 ——
    #   蒸馏时算好并存进池的 evidence_score(实测 L1 0.8~100 / L2 81~100)被整个丢弃。
    #   2026-08-09 replica 实测:1290 条池内 L1/L2 喂进来,重算后 **0 条**过 50 分线,
    #   `build_recommendation_packages` 对**全部真实调用点恒返空**。
    #
    #   为什么不选"给池表补 citation_rate 列":那要迁移 + 蒸馏重跑才生效,而且
    #   `median_price_yuan` / `sample_count` 请求时依然拿不到,分数还是和蒸馏时不一样 ——
    #   修不干净。信任池内结论零迁移、当天生效,而且语义更对:蒸馏时的入参更全,
    #   请求时并没有比它更好的证据来源。
    #
    #   🔴 蒸馏侧调用(scripts/distill_media_effective_pool.py)喂的是**原始 mhz 行**,
    #   不带 evidence_score → 自动走下面的 citation_rate 原路径,行为逐字不变,
    #   也就不存在"拿自己的输出喂自己"的自我强化。
    pooled_evidence = row.get("evidence_score")
    if pooled_evidence is not None:
        evidence_score = min(100.0, max(0.0, _num(pooled_evidence, 0)))
    else:
        citation_rate = _num(row.get("citation_rate"), 0)
        if citation_rate <= 1:
            citation_rate *= 100
        evidence_score = min(100.0, max(0.0, citation_rate))
    if evidence_score:
        reasons.append(f"行业上榜证据 {evidence_score:.0f} 分")

    geo_rank = _num(row.get("geo_rank"), 0)
    inclusion = _num(row.get("inclusion_rate"), 0)
    authority = 18 if _num(row.get("authority_media"), 0) > 0 else 0
    portal_bonus = 12 if str(row.get("portal_media") or "").strip() else 0
    quality_score = min(100.0, authority + portal_bonus + min(35, geo_rank * 7) + min(35, inclusion / 2))

    platform_score = min(100.0, _num(row.get("pc_weight"), 0) * 8 + _num(row.get("m_weight"), 0) * 6)

    if median_price_yuan and median_price_yuan > 0 and price > 0:
        price_score = max(0.0, min(100.0, 100.0 * (median_price_yuan / price)))
    elif not price_known:
        # 🔴 没录价给**中性分**,不是满分。旧式 `100 - price/20` 在 price=0 时算出
        #    100 —— "没录价"反而拿价格满分,等于奖励数据缺失。
        price_score = 50.0
    else:
        price_score = max(0.0, min(100.0, 100.0 - price / 20))

    diversity_score = 70.0
    history_weight = history_feedback_weight(sample_count)
    evidence_weight = 0.45 - history_weight
    history_score = _num(row.get("history_score"), 0)
    anchor_bonus = _industry_anchor_score(name, industry or row.get("industry") or "")

    effective_score = (
        evidence_score * evidence_weight
        + quality_score * 0.20
        + platform_score * 0.15
        + price_score * 0.10
        + diversity_score * 0.10
        + history_score * history_weight
        + anchor_bonus
    )
    if "noise_name" in risk_tags:
        effective_score -= 30
    if "missing_quality_signals" in risk_tags:
        effective_score -= 20

    is_recommendable = not risk_tags and effective_score >= 50
    tier = "L0"
    if is_recommendable:
        tier = "L2" if effective_score >= 75 and evidence_score >= 30 else "L1"

    return {
        **row,
        "media_source": row.get("media_source") or row.get("_type") or "media",
        "media_id": int(row.get("media_id") or row.get("id") or 0),
        "media_name": name,
        "platform_name": row.get("platform_name") or name,
        "industry": row.get("industry") or industry or "",
        "quality_score": round(quality_score, 2),
        "evidence_score": round(evidence_score, 2),
        "price_score": round(price_score, 2),
        "noise_score": -30 if "noise_name" in risk_tags else 0,
        "effective_score": round(max(0.0, min(100.0, effective_score)), 2),
        "is_recommendable": is_recommendable,
        "tier": tier,
        "tags": {
            "portal_media": row.get("portal_media"),
            "authority_media": bool(_num(row.get("authority_media"), 0)),
            "geo_rank": row.get("geo_rank"),
        },
        "reasons": reasons or ["当前资源质量与价格处于可投放范围"],
        "risk_tags": risk_tags,
        "risk_note": "、".join(risk_tags) if risk_tags else "",
    }


def classify_media_tier(scored: dict[str, Any]) -> str:
    if not scored.get("is_recommendable"):
        return "L0"
    if _num(scored.get("effective_score"), 0) >= 75 and _num(scored.get("evidence_score"), 0) >= 30:
        return "L2"
    return "L1"


def _article_type_weight_bonus(scored: dict[str, Any], article_type: ArticleType) -> float:
    weights = ARTICLE_TYPE_WEIGHTS[article_type]
    portal = str(scored.get("portal_media") or scored.get("tags", {}).get("portal_media") or "")
    authority = bool(scored.get("tags", {}).get("authority_media")) or _num(scored.get("authority_media"), 0) > 0
    if authority:
        return weights["authority"] * 8
    if "垂直" in portal or "行业" in portal:
        return weights["vertical"] * 8
    return weights["general"] * 8


def _candidate_sort_key(scored: dict[str, Any], article_type: ArticleType) -> float:
    return _num(scored.get("effective_score"), 0) + _article_type_weight_bonus(scored, article_type)


# ============================================================
# [E2] 答案实体飞轮排序增强 + 证据徽章(flag-gated · flag 关=零取数/字节一致)。
#
# 🔴 死因一守卫:排序增强插在 build_recommendation_packages 真正到达组合包页面的排序键里,
#    不碰 _candidate_sort_key 的默认返回、不碰 placement_service._quality_score_v2f、
#    不复活 blend 参数签名。flag 关时 boost/evidence 均为空 → 走原生 _candidate_sort_key / 原生 evidence。
# ============================================================
def _boost_key(row: dict[str, Any]) -> tuple[str, int]:
    """候选 → 规范化 (media 源, 库存 id)。
    组合包候选源(media_effective_pool)media_source 为 'media'/'wemedia',
    绑定候选(geo_media_binding_candidates)media_source 为 'mhz_media'/'mhz_wemedia' + inventory_id;
    底层同为 mhz_media.id / mhz_wemedia.id → 去 'mhz_' 前缀后按 (source, id) 对齐。
    """
    src = str(row.get("media_source") or "media")
    if src.startswith("mhz_"):
        src = src[4:]
    try:
        mid = int(row.get("media_id") or row.get("inventory_id") or row.get("id") or 0)
    except (TypeError, ValueError):
        mid = 0
    return (src, mid)


def _normalize_industry_key_safe(industry: str) -> str:
    """归一行业键(与 E1 榜 / 绑定候选 industry_key 口径一致)· fail-soft。"""
    try:
        from services.media_entity_flywheel import is_all_industry_scope, normalize_industry_key
        if not industry or is_all_industry_scope(industry):
            return ""
        ik = normalize_industry_key(industry)
        return "" if is_all_industry_scope(ik) else ik
    except Exception:
        return industry or ""


def _load_approved_bindings(industry_key: str) -> list[dict[str, Any]]:
    """已审核通过的媒体绑定候选(含 entity_key + media_source + inventory_id)· fail-soft 惰性取数。"""
    try:
        from db.media_entity_flywheel_db import list_approved_media_binding_candidates
        # include_general=True:与 shadow 侧 industry-OR-general 口径对齐,否则默认存 general 的 approved
        # 绑定对具体行业文章恒被过滤 → boost silent no-op(死因一)。
        # limit=None:全量 membership,默认 LIMIT 100 会让排位后的绑定实体 boost/徽章漏加(假阴性)。
        return list_approved_media_binding_candidates(
            industry_key=industry_key, include_general=True, limit=None,
        ) or []
    except Exception:
        return []


def _load_shadow_scores(entity_keys: list[str], industry_key: str) -> dict[str, float]:
    """entity_key → 最新 shadow_score(媒体飞轮有效分)· 复用 SSOT · fail-soft。"""
    try:
        from db.media_entity_flywheel_db import get_shadow_scores_by_entity_keys
        return get_shadow_scores_by_entity_keys(entity_keys, industry_key=industry_key) or {}
    except Exception:
        return {}


def _load_answer_entity_mentions(industry_key: str) -> dict[str, int]:
    """entity_key → AI 点名推荐次数(答案实体 mention_count)· 复用 E1 主榜数据源 · fail-soft。"""
    try:
        from db.research_answer_entity_db import list_answer_entity_summary
        rows = list_answer_entity_summary(industry_key=industry_key, limit=200, since_days=90) or []
        out: dict[str, int] = {}
        for r in rows:
            ek = r.get("entity_key")
            if not ek:
                continue
            try:
                out[ek] = int(r.get("mention_count") or 0)
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return {}


def _prefetch_entity_rank_boost(
    industry: str,
) -> tuple[dict[tuple[str, int], float], dict[tuple[str, int], str]]:
    """按 approved 绑定把候选 (source,id) ↔ entity_key ↔ shadow/点名关联起来。

    返回 (boost_by_key, evidence_by_key):
      - boost_by_key: 规范化 (source,id) → 排序 boost 值(仅有 shadow_score 时才有,归一后 * 权重)。
      - evidence_by_key: 规范化 (source,id) → 证据徽章串(有真实飞轮数据才有,无数据不产生 → 不显假 0)。
    任一环节失败整体 fail-soft 返 ({}, {}),让发布推荐照常出。
    """
    try:
        industry_key = _normalize_industry_key_safe(industry)
        bindings = _load_approved_bindings(industry_key)
        if not bindings:
            return {}, {}

        key_to_entity: dict[tuple[str, int], str] = {}
        for b in bindings:
            ek = b.get("entity_key")
            if not ek:
                continue
            key = _boost_key(b)
            if not key[0] or not key[1]:
                continue
            key_to_entity.setdefault(key, ek)
        if not key_to_entity:
            return {}, {}

        entity_keys = sorted(set(key_to_entity.values()))
        shadow = _load_shadow_scores(entity_keys, industry_key)
        mentions = _load_answer_entity_mentions(industry_key)
        if not isinstance(shadow, dict):
            shadow = {}
        if not isinstance(mentions, dict):
            mentions = {}

        boost_by_key: dict[tuple[str, int], float] = {}
        evidence_by_key: dict[tuple[str, int], str] = {}
        for key, ek in key_to_entity.items():
            sc = shadow.get(ek)
            mc = mentions.get(ek)

            # 排序 boost:仅在有真实 shadow_score 时;归一 /100 后 * 权重上限。
            if sc is not None:
                try:
                    boost_by_key[key] = (float(sc) / 100.0) * ENTITY_RANK_BOOST_WEIGHT
                except (TypeError, ValueError):
                    pass

            # 证据徽章:只用真实数据拼串,无数据不 append(绝不显假 0/假证据)。
            # [review fix] 措辞人话化且与 E1 榜列名同源同名(「媒体有效分」= 同一 shadow 分,防同页两套叫法打架)。
            parts: list[str] = []
            try:
                if mc is not None and int(mc) > 0:
                    parts.append(f"近90天被AI点名 {int(mc)} 次")
            except (TypeError, ValueError):
                pass
            if sc is not None:
                try:
                    parts.append(f"媒体有效分 {float(sc):.0f}")
                except (TypeError, ValueError):
                    pass
            if parts:
                evidence_by_key[key] = "AI 高频推荐媒体 · " + " · ".join(parts)

        return boost_by_key, evidence_by_key
    except Exception:
        return {}, {}


def _dedupe_keep_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int]] = set()
    result = []
    for item in items:
        key = (str(item.get("media_source") or "media"), int(item.get("media_id") or 0))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _to_package_item(scored: dict[str, Any], flywheel_evidence: str | None = None) -> dict[str, Any]:
    fit_score = round(_num(scored.get("effective_score"), 0), 2)
    # flag 关 / 无飞轮数据(flywheel_evidence=None)时:evidence 表达式与改前字节一致。
    # 有真实飞轮证据串时:在原 evidence 尾部 append(前端 item.evidence[] 证据徽章自动显示)。
    if flywheel_evidence:
        evidence = list(scored.get("reasons") or ["符合当前文章投放目标"]) + [flywheel_evidence]
    else:
        evidence = scored.get("reasons") or ["符合当前文章投放目标"]
    return {
        "media_source": scored.get("media_source") or "media",
        "media_id": int(scored.get("media_id") or scored.get("id") or 0),
        "media_name": scored.get("media_name") or _norm_name(scored),
        "platform_name": scored.get("platform_name") or scored.get("media_name") or _norm_name(scored),
        "price_yuan": round(_price_yuan(scored), 2),
        "cost_points": _points(scored),
        "fit_score": fit_score,
        "evidence": evidence,
        "risk_note": scored.get("risk_note") or "",
        "tier": classify_media_tier(scored),
    }


def build_recommendation_packages(
    *,
    article_summary: dict[str, Any],
    candidates: list[dict[str, Any]],
    recommendation_level: int,
) -> dict[str, Any]:
    industry = str(article_summary.get("industry") or "")
    article_type = normalize_article_type(article_summary.get("article_type"))
    scored = [
        score_media_candidate(c, industry=industry)
        for c in candidates
    ]
    recommendable = [
        s for s in scored
        if s.get("is_recommendable") and classify_media_tier(s) in {"L1", "L2"}
    ]

    # [E2] flag-gated 飞轮排序增强 + 证据徽章:仅 flag 开启时预取一次(flag 关 → 不取数、零成本)。
    #      boost_by_key 空(flag 关 / 无 shadow 数据)→ 排序键字节级恒等 _candidate_sort_key(默认路径不变)。
    boost_by_key: dict[tuple[str, int], float] = {}
    evidence_by_key: dict[tuple[str, int], str] = {}
    if is_feature_enabled("entity_rank_boost"):
        boost_by_key, evidence_by_key = _prefetch_entity_rank_boost(industry)

    if boost_by_key:
        def _sort_key(item: dict[str, Any]) -> float:
            return _candidate_sort_key(item, article_type) + boost_by_key.get(_boost_key(item), 0.0)
    else:
        def _sort_key(item: dict[str, Any]) -> float:
            return _candidate_sort_key(item, article_type)

    def _pkg_item(item: dict[str, Any]) -> dict[str, Any]:
        return _to_package_item(item, evidence_by_key.get(_boost_key(item)) if evidence_by_key else None)

    recommendable.sort(key=_sort_key, reverse=True)
    recommendable = _dedupe_keep_order(recommendable)

    if not recommendable:
        packages = []
    else:
        trial = sorted(recommendable, key=lambda item: (_points(item), -_sort_key(item)))[:3]
        balanced = recommendable[:5]
        authority = sorted(
            recommendable,
            key=lambda item: (
                classify_media_tier(item) != "L2",
                -_sort_key(item),
                _points(item),
            ),
        )[:8]
        packages = [
            {
                "package_type": "trial",
                "title": "性价比试投",
                "estimated_points": sum(_points(item) for item in trial),
                "reason": "优先选择价格可控且具备基础质量信号的媒体。",
                "items": [_pkg_item(item) for item in trial],
            },
            {
                "package_type": "balanced",
                "title": "稳妥推荐",
                "estimated_points": sum(_points(item) for item in balanced),
                "reason": "平衡行业证据、媒体质量和预算，适合作为默认投放组合。",
                "items": [_pkg_item(item) for item in balanced],
            },
            {
                "package_type": "authority",
                "title": "权威增强",
                "estimated_points": sum(_points(item) for item in authority),
                "reason": "提高权威媒体和 L2 推荐池占比，用于增强品牌背书。",
                "items": [_pkg_item(item) for item in authority],
            },
        ]

    summary = {
        "article_type": article_type.value,
        "article_type_label": ARTICLE_TYPE_LABELS[article_type],
        "industry": industry,
        "semantic_keywords": article_summary.get("semantic_keywords") or [],
        "publish_goal": article_summary.get("publish_goal") or "",
        "recommendation_level": recommendation_level,
    }
    return {
        "article_summary": summary,
        "packages": packages,
        "disclaimer_version": DISCLAIMER_VERSION,
        "terms_version": TERMS_VERSION,
        "analysis_version": ANALYSIS_VERSION,
    }


def parse_semantic_keywords(title: str, content: str = "", keyword: str = "") -> list[str]:
    text = f"{keyword} {title} {content[:500]}"
    tokens = [
        t for t in re.split(r"[\s,，。；;、/|()（）:：]+", text)
        if len(t.strip()) >= 2
    ]
    deduped: list[str] = []
    for token in tokens:
        if token not in deduped:
            deduped.append(token)
        if len(deduped) >= 8:
            break
    return deduped


def infer_article_summary(
    *,
    title: str,
    content: str = "",
    keyword: str = "",
    industry: str = "",
    article_type: str | None = None,
    publish_goal: str = "",
) -> dict[str, Any]:
    normalized = normalize_article_type(article_type)
    if article_type is None:
        raw = f"{title} {content[:800]}"
        if any(k in raw for k in ("案例", "客户", "实践")):
            normalized = ArticleType.CASE_STORY
        elif any(k in raw for k in ("政策", "趋势", "新规")):
            normalized = ArticleType.POLICY_TREND
        elif any(k in raw for k in ("怎么", "如何", "问题", "FAQ", "问答")):
            normalized = ArticleType.QA_SOLVE
    return {
        "article_type": normalized.value,
        "article_type_label": ARTICLE_TYPE_LABELS[normalized],
        "industry": industry,
        "semantic_keywords": parse_semantic_keywords(title, content, keyword),
        "publish_goal": publish_goal or "提升 AI 搜索引用和品牌可信度",
    }


def is_publish_snapshot_required(payload: dict[str, Any]) -> bool:
    return not bool(payload.get("snapshot_id") or payload.get("decision_snapshot_id"))


def quota_key(user_id: int, day: date | None = None) -> str:
    day = day or date.today()
    return f"ai_rec_quota:{user_id}:{day.isoformat()}"
