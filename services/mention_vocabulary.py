"""提及/推荐档位词表 SSOT（P0-3 · 2026-07-26）。

生产实证（修复前）：``monitoring_results.mention_type`` 全库只有
``direct`` / ``none`` / ``llm_verified_fallback`` / ``pending_identity`` 四个值，
**没有任何「推荐」档**。于是：

- 报告里的「推荐率」永远算成 0%；
- UI 同时摆出「仅提到 / 推荐」两个标签，后者永远取不到值 = 假标签。

Owner 2026-07-26 裁决（口径放宽）：判定扩为
``recommended``（出现在推荐列表／被列为候选／被正面描述都算）/ ``mentioned`` /
``none`` / ``pending_identity``。

本模块只做**词表与映射**，不做判定。真正的判定统一走
``services/geo_observation/entity_review.classify_outcome``（十分类 target_outcome），
这里负责把它压成对客户可读的四档，并把历史值归一化。

--------------------------------------------------------------------------
存量映射铁律
--------------------------------------------------------------------------
``direct`` → ``mentioned``，**绝不映射成 ``recommended``**。
旧值只证明「品牌出现在回答里」，不证明 AI 推荐了它。把它升成推荐就是伪造业绩。
报告需注明口径变更日期（``VOCABULARY_CHANGED_ON``），让新旧报告的推荐率可对比。
"""

from __future__ import annotations

from typing import Final

# 口径变更日期（报告注明用；不是 feature flag）
VOCABULARY_CHANGED_ON: Final[str] = "2026-07-26"

# 对客户呈现的四档
MENTION_RECOMMENDED: Final[str] = "recommended"
MENTION_MENTIONED: Final[str] = "mentioned"
MENTION_NONE: Final[str] = "none"
MENTION_PENDING_IDENTITY: Final[str] = "pending_identity"

PUBLIC_MENTION_TYPES: Final[tuple[str, ...]] = (
    MENTION_RECOMMENDED,
    MENTION_MENTIONED,
    MENTION_NONE,
    MENTION_PENDING_IDENTITY,
)

MENTION_TYPE_LABELS: Final[dict[str, str]] = {
    MENTION_RECOMMENDED: "被推荐",
    MENTION_MENTIONED: "仅提到",
    MENTION_NONE: "未提到",
    MENTION_PENDING_IDENTITY: "疑似提到待确认",
}

# 观测 SSOT 的十分类 target_outcome → 对客户四档。
# Owner 放宽口径：candidate_only（进候选/名单）也算被推荐。
_OUTCOME_TO_MENTION: Final[dict[str, str]] = {
    "recommended": MENTION_RECOMMENDED,
    "conditionally_recommended": MENTION_RECOMMENDED,
    "candidate_only": MENTION_RECOMMENDED,
    "mentioned_only": MENTION_MENTIONED,
    "criteria_only": MENTION_NONE,
    "not_mentioned": MENTION_NONE,
    "refused_no_evidence": MENTION_NONE,
    "refused_risk": MENTION_NONE,
    # 身份不明/引擎异常绝不算「未提到」（SSOT §10.2：PENDING/UNKNOWN 不等于 0）
    "entity_ambiguous": MENTION_PENDING_IDENTITY,
    "engine_error": MENTION_PENDING_IDENTITY,
    "legacy_unknown": MENTION_PENDING_IDENTITY,
}

# 历史 mention_type → 新词表。direct 只到 mentioned，绝不升为 recommended。
_LEGACY_TO_MENTION: Final[dict[str, str]] = {
    "direct": MENTION_MENTIONED,
    "exact_mention": MENTION_MENTIONED,
    "brand_mention": MENTION_MENTIONED,
    "llm_verified_fallback": MENTION_MENTIONED,
    "category_mention": MENTION_NONE,
    "partial": MENTION_NONE,
    "reverify_corrected": MENTION_NONE,
    "none": MENTION_NONE,
    "pending_identity": MENTION_PENDING_IDENTITY,
    # 新词表本身幂等
    MENTION_RECOMMENDED: MENTION_RECOMMENDED,
    MENTION_MENTIONED: MENTION_MENTIONED,
}

# 供 evidence 分级等旧消费点使用：这些值都表示「品牌确实出现在回答里」。
BRAND_PRESENT_MENTION_TYPES: Final[frozenset[str]] = frozenset({
    MENTION_RECOMMENDED,
    MENTION_MENTIONED,
    "direct",
    "exact_mention",
    "brand_mention",
    "llm_verified_fallback",
})


def normalize_mention_type(value: object) -> str:
    """把任意历史/新 mention_type 归一到四档；未知值按 pending 处理（不当 0）。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return MENTION_NONE
    return _LEGACY_TO_MENTION.get(raw, MENTION_PENDING_IDENTITY)


def mention_type_from_outcome(target_outcome: object, *, is_detected: object = None) -> str:
    """target_outcome → 对客户四档；缺 outcome 时退回 is_detected 的保守判定。"""
    raw = str(target_outcome or "").strip().lower()
    mapped = _OUTCOME_TO_MENTION.get(raw)
    if mapped:
        return mapped
    if is_detected is True:
        return MENTION_MENTIONED  # 只知道"出现了"，不敢说被推荐
    if is_detected is False:
        return MENTION_NONE
    return MENTION_PENDING_IDENTITY


def is_recommended_mention(value: object) -> bool:
    return normalize_mention_type(value) == MENTION_RECOMMENDED


def is_brand_present(value: object) -> bool:
    """该值是否表示品牌出现在回答里（被推荐或被提到）。"""
    return normalize_mention_type(value) in {MENTION_RECOMMENDED, MENTION_MENTIONED}


def mention_type_label(value: object) -> str:
    return MENTION_TYPE_LABELS.get(normalize_mention_type(value), "未提到")
