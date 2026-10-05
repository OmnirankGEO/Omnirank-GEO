"""Media entity normalization and shadow scoring for GEO placement flywheel.

This module is deliberately pure-Python.  It does not replace live media
recommendation; it produces auditable shadow scores that can be reviewed before
any production recommendation path consumes them.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any
from urllib.parse import urlparse


FLYWHEEL_VERSION = "geo_media_flywheel_v1_2026-06-12"

# [E1 SSOT · 媒体有效性综合分权重] 原为 compute_media_entity_shadow_score 内联字面量,抽为命名常量单点,
# 供 E1「行业媒体有效性榜」复用同一口径(禁第二份权重漂移)。改这里 = 全局改综合有效分口径。
EVIDENCE_BALANCED_WEIGHT_MULT = 35.0        # balanced_weight > 0 时 evidence = min(100, bw × 35)
EVIDENCE_ADOPTION_WEIGHT = 55.0             # 无 balanced_weight 时:答案采纳率 × 55(最高权重信号)
EVIDENCE_CITATION_WEIGHT = 30.0            # + 明确引用率 × 30
EVIDENCE_ENGINE_COVERAGE_WEIGHT = 15.0      # + 引擎覆盖率 × 15
SHADOW_BLEND_EVIDENCE = 0.52               # 5 维合成:证据主导
SHADOW_BLEND_QUALITY = 0.18
SHADOW_BLEND_INVENTORY = 0.20
SHADOW_BLEND_OUTCOME = 0.10

_MOBILE_PREFIXES = ("www.", "m.", "wap.", "amp.")
_JINA_PREFIX_RE = re.compile(r"^https?://r\.jina\.ai/http[s]?://", re.I)
_NOISE_NAME_PATTERNS = ("套餐", "随机", "任选", "秒杀", "包收录", "十元", "低价", "组合包")

_INDUSTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "tourism_hotel": ("tourism-hotel", "tourism_hotel", "travel", "hotel", "旅游酒店", "酒店旅游", "文旅", "旅行", "景区", "民宿", "出行"),
    "real_estate": ("real-estate", "real_estate", "property", "房地产", "房产", "地产", "楼盘", "置业"),
    "auto": ("auto-mobility", "auto", "汽车", "新能源车", "出行服务", "租车", "车队", "驾培"),
    "home_improvement": ("home-decor", "home_improvement", "decoration", "装修建材", "家装", "装修", "家居", "建材", "家具"),
    "education": ("education-training", "education", "教育培训", "教育", "培训", "留学", "考研", "少儿"),
    "medical": ("medical-health", "healthcare", "medical", "beauty-medical", "医疗健康", "医疗", "健康", "口腔", "医美", "美业医美", "康复"),
    "finance": ("finance-insurance", "finance", "金融理财", "金融", "理财", "保险", "证券", "基金"),
    "technology": ("technology-digital", "technology", "tech-digital", "科技数码", "科技", "软件", "互联网", "人工智能", "云计算"),
    "business_service": ("business-service", "business_service", "商务服务", "企业服务", "营销", "咨询", "SaaS"),
    "retail_ecommerce": ("retail-ecommerce", "retail_ecommerce", "电商零售", "零售", "电商", "连锁", "消费"),
    "food": ("food", "food-beverage", "食品餐饮", "餐饮", "食品", "茶饮", "咖啡"),
    "legal": ("legal-service", "legal", "law", "法律", "法律服务", "律所", "律师"),
    "manufacturing": ("manufacturing-industrial", "manufacturing", "industrial", "industry-equipment", "制造业", "工业设备", "专用设备", "制药装备"),
}

_ALL_INDUSTRY_SCOPES = {"", "general", "all", "all_articles", "通用", "全部行业", "通用/全部行业"}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_domain(value: str) -> str:
    """Normalize a URL or domain into a stable registrable-ish domain key.

    The project often stores Jina reader URLs.  Those are unwrapped here so the
    media entity key points at the original source instead of r.jina.ai.
    """
    raw = _clean_text(value)
    if not raw:
        return ""
    raw = _JINA_PREFIX_RE.sub("https://", raw)
    if raw.startswith("r.jina.ai/http://"):
        raw = raw.replace("r.jina.ai/http://", "http://", 1)
    if raw.startswith("r.jina.ai/https://"):
        raw = raw.replace("r.jina.ai/https://", "https://", 1)
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path).split("/")[0].split("@")[-1].split(":")[0].lower()
    for prefix in _MOBILE_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def canonicalize_media_name(name: str) -> str:
    cleaned = re.sub(r"\s+", "", _clean_text(name))
    cleaned = cleaned.replace("（", "(").replace("）", ")")
    return cleaned


def normalize_industry_key(industry: str) -> str:
    raw = _clean_text(industry)
    if not raw:
        return "general"
    for key, aliases in _INDUSTRY_ALIASES.items():
        if raw == key or any(alias in raw or raw in alias for alias in aliases):
            return key
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", raw).strip("_").lower()
    return slug or "general"


def is_all_industry_scope(industry: str) -> bool:
    raw = _clean_text(industry).replace("／", "/").lower()
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff/]+", "_", raw).strip("_")
    return raw in _ALL_INDUSTRY_SCOPES or normalized in _ALL_INDUSTRY_SCOPES


def industry_filter_values(industry: str) -> list[str]:
    """Return raw industry labels that may map to the same normalized key.

    Research raw rows usually store Chinese display names while shadow tables
    store normalized keys.  Admin tools can accept either form, so rebuild jobs
    should search all known aliases instead of silently returning zero rows.
    """
    raw = _clean_text(industry)
    if is_all_industry_scope(raw):
        return []
    key = normalize_industry_key(raw)
    values = [raw, key]
    values.extend(_INDUSTRY_ALIASES.get(key, ()))
    return list(dict.fromkeys(v for v in values if v))


def build_entity_key(name: str, domain: str = "") -> str:
    domain_key = normalize_domain(domain)
    name_key = canonicalize_media_name(name).lower()
    base = domain_key or name_key
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12] if base else ""
    return f"me_{digest}" if digest else ""


def build_media_entity_seed(seed: dict[str, Any]) -> dict[str, Any]:
    name = _clean_text(seed.get("name") or seed.get("canonical_name") or seed.get("media_name"))
    domain = normalize_domain(seed.get("domain") or seed.get("url") or seed.get("home_url") or "")
    aliases = seed.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [x.strip() for x in re.split(r"[,，\n]", aliases) if x.strip()]
    entity_key = _clean_text(seed.get("entity_key")) or build_entity_key(name, domain)
    return {
        "entity_key": entity_key,
        "canonical_name": name,
        "domain": domain,
        "aliases": list(dict.fromkeys([canonicalize_media_name(x) for x in aliases if _clean_text(x)])),
        "industry_key": normalize_industry_key(seed.get("industry") or seed.get("industry_key") or ""),
        "entity_type": seed.get("entity_type") or "media_site",
        "home_url": seed.get("home_url") or (f"https://{domain}" if domain else ""),
        "tags": seed.get("tags") or {},
        "confidence": float(seed.get("confidence") or 0.7),
    }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_inventory_purchasable(item: dict[str, Any]) -> bool:
    if item.get("is_active") is False:
        return False
    name = canonicalize_media_name(item.get("media_name") or item.get("platform_name") or "")
    if any(pattern in name for pattern in _NOISE_NAME_PATTERNS):
        return False
    price = _num(item.get("price_yuan") or item.get("our_price_yuan") or item.get("price"), 0)
    points = _num(item.get("price_points") or item.get("our_price_points"), 0)
    return price > 0 or points > 0


def match_inventory_to_entity(entity: dict[str, Any], inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match media-box SKUs to a canonical entity without relying on one SKU as the entity."""
    domain = normalize_domain(entity.get("domain") or "")
    names = {canonicalize_media_name(entity.get("canonical_name") or "")}
    names.update(canonicalize_media_name(x) for x in entity.get("aliases") or [])
    names = {n for n in names if n}
    matches: list[dict[str, Any]] = []
    for row in inventory:
        row_domain = normalize_domain(row.get("domain") or row.get("url") or row.get("entrance_link") or "")
        row_name = canonicalize_media_name(
            row.get("media_name") or row.get("platform_name") or row.get("toutiao_name") or ""
        )
        method = ""
        confidence = 0.0
        if domain and row_domain and domain == row_domain:
            method = "domain_exact"
            confidence = 0.98
        elif names and any(n and (n in row_name or row_name in n) for n in names if len(n) >= 2):
            method = "name_alias"
            confidence = 0.86
        if not method:
            continue
        matches.append({
            **row,
            "media_source": row.get("media_source") or row.get("_type") or "media",
            "inventory_id": int(row.get("inventory_id") or row.get("media_id") or row.get("id") or 0),
            "match_method": method,
            "match_confidence": max(confidence, _num(row.get("match_confidence"), 0)),
            "is_purchasable": _is_inventory_purchasable(row),
        })
    return matches


def outcome_score_from_rollup(outcome_rollup: dict[str, Any] | None) -> float:
    """[B5-1 SSOT] outcome_rollup(published_count / citation_lift_30d)→ outcome_score(0-100)。
    与 compute_media_entity_shadow_score 同一公式,供效果回流任务复用(单点,防漂移)。"""
    o = outcome_rollup or {}
    published_count = _num(o.get("published_count"), 0)
    citation_lift = _num(o.get("citation_lift_30d"), 0)
    return min(100.0, published_count * 8 + max(0.0, citation_lift) * 12)


def blend_shadow_score(evidence_score: float, quality_score: float,
                       inventory_score: float, outcome_score: float) -> float:
    """[B5-1 SSOT] 5 维权重合成 shadow_score(evidence .52 / quality .18 / inventory .20 / outcome .10)。单点。"""
    return (evidence_score * SHADOW_BLEND_EVIDENCE + quality_score * SHADOW_BLEND_QUALITY
            + inventory_score * SHADOW_BLEND_INVENTORY + outcome_score * SHADOW_BLEND_OUTCOME)


def compute_media_entity_shadow_score(
    *,
    entity: dict[str, Any],
    citation_rollup: dict[str, Any],
    inventory_matches: list[dict[str, Any]],
    outcome_rollup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a shadow media score.

    Weighting policy:
    - answer adoption and explicit citations dominate evidence score;
    - inventory only decides whether a cited source is actually purchasable;
    - outcome feedback can improve a score, but cannot auto-approve production.
    """
    outcome_rollup = outcome_rollup or {}
    answer_adopted = _num(citation_rollup.get("answer_adopted_count"), 0)
    cited = _num(citation_rollup.get("cited_count"), 0)
    prompts = max(1.0, _num(citation_rollup.get("prompt_count"), 1))
    engine_count = min(4.0, _num(citation_rollup.get("engine_count"), 0))
    balanced_weight = _num(citation_rollup.get("balanced_weight"), 0)

    if balanced_weight > 0:
        evidence_score = min(100.0, balanced_weight * EVIDENCE_BALANCED_WEIGHT_MULT)
    else:
        adoption_rate = min(1.0, answer_adopted / prompts)
        citation_rate = min(1.0, cited / prompts)
        engine_coverage = engine_count / 4.0
        evidence_score = min(100.0, (adoption_rate * EVIDENCE_ADOPTION_WEIGHT)
                             + (citation_rate * EVIDENCE_CITATION_WEIGHT)
                             + (engine_coverage * EVIDENCE_ENGINE_COVERAGE_WEIGHT))

    purchasable_matches = [m for m in inventory_matches if m.get("is_purchasable", False)]
    best_match_conf = max([_num(m.get("match_confidence"), 0) for m in purchasable_matches] or [0])
    inventory_score = min(100.0, best_match_conf * 80 + min(20, len(purchasable_matches) * 5))

    outcome_score = outcome_score_from_rollup(outcome_rollup)

    explicit_quality = _num(citation_rollup.get("quality_score"), 0)
    if explicit_quality > 0:
        quality_score = min(100.0, explicit_quality)
    else:
        # entity["confidence"] 可能是 DB NUMERIC → Decimal;Decimal 与下方 float 系数
        # (0.18 等)相乘会 TypeError,导致 approve 每次 500。用 _num 统一转 float。
        quality_score = min(100.0, _num(entity.get("confidence"), 0.7) * 100)
    shadow_score = blend_shadow_score(evidence_score, quality_score, inventory_score, outcome_score)

    is_purchasable = bool(purchasable_matches)
    reference_status = "purchasable" if is_purchasable else "reference_only"
    reasons: list[str] = []
    if answer_adopted:
        reasons.append(f"答案采纳 {int(answer_adopted)} 次，是最高权重信号")
    if cited:
        reasons.append(f"被 AI 明确引用 {int(cited)} 次")
    if engine_count:
        reasons.append(f"覆盖 {int(engine_count)} 个引擎")
    if is_purchasable:
        reasons.append(f"已匹配 {len(purchasable_matches)} 个可采购资源")
    else:
        reasons.append("暂无可采购资源，只作为写作参考或媒体拓展线索")

    return {
        "entity_key": entity.get("entity_key"),
        "canonical_name": entity.get("canonical_name"),
        "industry_key": entity.get("industry_key") or normalize_industry_key(entity.get("industry") or ""),
        "score_version": FLYWHEEL_VERSION,
        "shadow_score": round(max(0.0, min(100.0, shadow_score)), 2),
        "evidence_score": round(evidence_score, 2),
        "quality_score": round(quality_score, 2),
        "inventory_score": round(inventory_score, 2),
        "outcome_score": round(outcome_score, 2),
        "reference_status": reference_status,
        "is_purchasable": is_purchasable,
        "reasons": reasons,
        "evidence": {
            "citation_rollup": citation_rollup,
            "inventory_match_count": len(inventory_matches),
            "purchasable_match_count": len(purchasable_matches),
            "outcome_rollup": outcome_rollup,
        },
    }


def rank_shadow_entities(rows: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: (
            bool(r.get("is_purchasable")),
            _num(r.get("shadow_score"), 0),
            _num(r.get("evidence_score"), 0),
        ),
        reverse=True,
    )[: max(1, int(limit or 20))]


def source_list_fairness_credit(total_sources_in_answer: int) -> float:
    """A single AI answer has one source-credit budget.

    Doubao may return 35 native-search sources while other engines return 7-10.
    This function prevents a longer source list from counting as 5 independent
    votes; each source receives a smaller share of the answer's credit.
    """
    count = max(1, int(total_sources_in_answer or 1))
    return 1.0 / math.sqrt(count)
