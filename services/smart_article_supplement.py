"""Server-authoritative planning for monitoring-driven article supplements."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from writing.direction_distribution import (
    STYLE_CODE_TO_USER_CHOICE,
    USER_CHOICE_OPTIONS,
    largest_remainder,
    style_code_to_user_choice,
)


MIN_RATE_GAP_ARTICLES = 1
MAX_RATE_GAP_ARTICLES = 10
PLAN_VERSION = "smart-supplement-v1"
TIER_TARGET_RATES = {"entry": 50.0, "standard": 65.0, "premium": 75.0, "flagship": 75.0}
VALID_EXISTING_STATUSES = frozenset({
    "draft", "pending", "titles_ready", "regenerating", "failed", "writing", "completed", "published",
})


class SupplementPricingUnavailable(RuntimeError):
    """The supplement price SSOT could not provide a trustworthy point cost."""


def _load_article_point_cost() -> int:
    """Read article pricing without turning an outage into a fake free preview."""
    try:
        from db.wallet_db import get_feature_pricing

        pricing = get_feature_pricing("article_gen")
        if not isinstance(pricing, Mapping) or "cost_points" not in pricing:
            raise ValueError("article_gen cost_points missing")
        raw_cost = pricing["cost_points"]
        if raw_cost is None or isinstance(raw_cost, bool):
            raise ValueError("article_gen cost_points invalid")
        point_cost = int(raw_cost)
        if point_cost < 0:
            raise ValueError("article_gen cost_points negative")
        return point_cost
    except SupplementPricingUnavailable:
        raise
    except Exception as exc:
        raise SupplementPricingUnavailable("article_gen pricing unavailable") from exc


@dataclass(frozen=True)
class SupplementInputs:
    keyword_id: int
    keyword: str
    quote_id: int
    industry: str
    required_articles: int
    valid_existing_articles: int
    target_rate: float
    recent_rate: float
    existing_style_counts: Mapping[str, int]
    target_style_ratios: Mapping[str, float]
    point_cost: int


@dataclass(frozen=True)
class SupplementPreview:
    keyword_id: int
    keyword: str
    quote_id: int
    suggested_articles: int
    reason_code: str
    reason_text: str
    style_plan: Mapping[str, int]
    estimated_points: int
    plan_version: str
    plan_hash: str
    target_rate: float
    recent_rate: float

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_counts(values: Mapping[str, int]) -> dict[str, int]:
    return {
        str(key): max(0, int(value or 0))
        for key, value in values.items()
        if str(key) and int(value or 0) >= 0
    }


def _clean_ratios(values: Mapping[str, float]) -> dict[str, float]:
    return {
        str(key): max(0.0, float(value or 0.0))
        for key, value in values.items()
        if str(key)
    }


def allocate_supplement_styles(
    *,
    supplement_count: int,
    target_ratios: Mapping[str, float],
    locked_existing: Mapping[str, int],
) -> dict[str, int]:
    """Allocate new articles so the post-supplement aggregate is closest to target.

    ``locked_existing`` represents already written/in-progress/manual/fixed topics.
    Those rows are never changed; only the new supplement slots are allocated.
    ``largest_remainder`` remains the integer allocation SSOT.
    """
    count = max(0, int(supplement_count or 0))
    ratios = _clean_ratios(target_ratios)
    existing = _clean_counts(locked_existing)
    all_styles = sorted(set(ratios) | set(existing))
    if count == 0:
        return {style: 0 for style in all_styles}
    positive_ratios = {style: ratio for style, ratio in ratios.items() if ratio > 0}
    if not positive_ratios:
        # Existing distribution helper uses equal fallback when runtime settings
        # are unavailable.  A genuinely all-zero industry config gets a stable
        # default rather than inventing a forbidden zero-ratio style.
        positive_ratios = {"guide": 1.0}
        all_styles = sorted(set(all_styles) | {"guide"})

    final_total = sum(existing.values()) + count
    desired_final = largest_remainder(positive_ratios, final_total)
    deficits = {
        style: max(0, desired_final.get(style, 0) - existing.get(style, 0))
        for style in positive_ratios
    }
    deficit_total = sum(deficits.values())

    if deficit_total >= count:
        allocated = largest_remainder(deficits, count)
    else:
        allocated = dict(deficits)
        remainder = count - deficit_total
        tail = largest_remainder(positive_ratios, remainder)
        for style, amount in tail.items():
            allocated[style] = allocated.get(style, 0) + amount

    result = {style: int(allocated.get(style, 0)) for style in sorted(set(all_styles) | set(allocated))}
    assert sum(result.values()) == count, f"supplement style sum mismatch: {result} != {count}"
    for style, ratio in ratios.items():
        if ratio == 0:
            assert result.get(style, 0) == 0, f"zero-ratio style received slots: {style}"
    return result


def _canonical_plan_payload(inputs: SupplementInputs, count: int, style_plan: Mapping[str, int]) -> dict:
    return {
        "keyword_id": int(inputs.keyword_id),
        "quote_id": int(inputs.quote_id),
        "required_articles": max(0, int(inputs.required_articles or 0)),
        "valid_existing_articles": max(0, int(inputs.valid_existing_articles or 0)),
        "target_rate": round(float(inputs.target_rate or 0), 4),
        "recent_rate": round(float(inputs.recent_rate or 0), 4),
        "existing_style_counts": _clean_counts(inputs.existing_style_counts),
        "target_style_ratios": _clean_ratios(inputs.target_style_ratios),
        "suggested_articles": int(count),
        "style_plan": _clean_counts(style_plan),
        "point_cost": max(0, int(inputs.point_cost or 0)),
    }


def build_supplement_preview(inputs: SupplementInputs) -> SupplementPreview:
    required = max(0, int(inputs.required_articles or 0))
    existing = max(0, int(inputs.valid_existing_articles or 0))
    plan_gap = max(0, required - existing)

    if plan_gap > 0:
        suggested = plan_gap
        reason_code = "plan_gap"
        reason_text = f"原计划还差 {plan_gap} 篇，先补齐已确认交付计划"
    elif float(inputs.recent_rate or 0) < float(inputs.target_rate or 0):
        target = max(1.0, float(inputs.target_rate or 0))
        gap_ratio = min(1.0, max(0.0, (target - float(inputs.recent_rate or 0)) / target))
        basis = max(1, required, existing)
        suggested = min(
            MAX_RATE_GAP_ARTICLES,
            max(MIN_RATE_GAP_ARTICLES, int(math.ceil(basis * gap_ratio))),
        )
        reason_code = "rate_gap"
        reason_text = (
            f"原计划已完成，但近 7 天出现率 {float(inputs.recent_rate or 0):g}% "
            f"低于目标 {float(inputs.target_rate or 0):g}%"
        )
    else:
        suggested = 0
        reason_code = "on_target"
        reason_text = "原计划已完成且近 7 天出现率已达标，暂不建议补发"

    style_plan = allocate_supplement_styles(
        supplement_count=suggested,
        target_ratios=inputs.target_style_ratios,
        locked_existing=inputs.existing_style_counts,
    )
    payload = _canonical_plan_payload(inputs, suggested, style_plan)
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SupplementPreview(
        keyword_id=int(inputs.keyword_id),
        keyword=inputs.keyword,
        quote_id=int(inputs.quote_id),
        suggested_articles=suggested,
        reason_code=reason_code,
        reason_text=reason_text,
        style_plan=style_plan,
        estimated_points=suggested * max(0, int(inputs.point_cost or 0)),
        plan_version=PLAN_VERSION,
        plan_hash=digest,
        target_rate=float(inputs.target_rate or 0),
        recent_rate=float(inputs.recent_rate or 0),
    )


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _topic_user_choice(topic: Mapping[str, Any]) -> str | None:
    """Translate every trusted historical topic style through the existing registries."""
    direct = str(topic.get("user_choice") or "").strip()
    if direct in USER_CHOICE_OPTIONS and direct != "auto":
        return direct

    style_code = str(topic.get("style_code") or "").strip()
    choice = style_code_to_user_choice(style_code)
    if choice:
        return choice

    try:
        from writing.style_registry import USER_CHOICE_TO_STYLE, normalize_style_code

        normalized = normalize_style_code(topic.get("article_style"))
        choice = style_code_to_user_choice(normalized or "")
        if choice:
            return choice
        # Some historical registry styles are intentionally not exposed by the
        # distribution helper.  Preserve a user's existing locked choice where
        # the style registry has an unambiguous inverse.
        inverse = {
            code: user_choice
            for user_choice, code in USER_CHOICE_TO_STYLE.items()
            if code and user_choice != "auto"
        }
        return inverse.get(normalized or style_code)
    except Exception:
        return None


def _target_choice_ratios(industry: str) -> dict[str, float]:
    """Reuse runtime industry ratios and the direction-distribution conversion SSOT."""
    try:
        from config.settings_manager import get_effective_style_ratios

        style_ratios = get_effective_style_ratios(industry, unit="fraction")
    except Exception:
        style_ratios = {}

    result: dict[str, float] = {}
    for style_code, ratio in (style_ratios or {}).items():
        choice = STYLE_CODE_TO_USER_CHOICE.get(style_code)
        if choice:
            result[choice] = result.get(choice, 0.0) + max(0.0, float(ratio or 0.0))
    if not any(result.values()):
        result = {"guide": 1.0}
    return result


def load_supplement_inputs(conn: Any, keyword_id: int, *, for_update: bool = False) -> SupplementInputs:
    """Read a keyword's authoritative plan inputs using an existing transaction."""
    cur = conn.cursor()
    lock = " FOR UPDATE OF ck" if for_update else ""
    cur.execute(
        f"""
        SELECT ck.id, ck.keyword, ck.quote_id, COALESCE(ck.required_articles, 1) AS required_articles,
               COALESCE(q.industry, '') AS industry, COALESCE(q.tier, 'standard') AS tier
        FROM confirmed_keywords ck
        JOIN quotes q ON q.id = ck.quote_id
        WHERE ck.id = %s AND COALESCE(q.deleted_at IS NULL, TRUE)
        {lock}
        """,
        (int(keyword_id),),
    )
    keyword = _row_to_dict(cur.fetchone())
    if not keyword:
        raise LookupError("关键词不存在或已失效")

    cur.execute(
        """
        SELECT user_choice, style_code, article_style, status, is_fixed, user_choice_source
        FROM topics
        WHERE keyword_id = %s AND status = ANY(%s)
        ORDER BY id
        """,
        (int(keyword_id), list(VALID_EXISTING_STATUSES)),
    )
    topics = [_row_to_dict(row) for row in (cur.fetchall() or [])]
    style_counts: dict[str, int] = {}
    for topic in topics:
        choice = _topic_user_choice(topic)
        if choice:
            style_counts[choice] = style_counts.get(choice, 0) + 1

    cur.execute(
        """
        SELECT AVG(COALESCE(effective_rate, detection_rate)) AS recent_rate
        FROM keyword_compliance_log
        WHERE keyword_id = %s AND keyword_source = 'confirmed'
          AND quote_id = %s AND check_date >= CURRENT_DATE - INTERVAL '6 days'
          AND check_date <= CURRENT_DATE
        """,
        (int(keyword_id), int(keyword["quote_id"])),
    )
    rates = _row_to_dict(cur.fetchone())
    recent_rate = float(rates.get("recent_rate") or 0.0)
    # Current quote tier is the target SSOT. Historical log rows may retain a
    # former tier's target until a backfill runs and must not distort a new plan.
    target_rate = float(TIER_TARGET_RATES.get(str(keyword.get("tier") or "standard"), 65.0))

    point_cost = _load_article_point_cost()

    return SupplementInputs(
        keyword_id=int(keyword["id"]),
        keyword=str(keyword["keyword"]),
        quote_id=int(keyword["quote_id"]),
        industry=str(keyword.get("industry") or ""),
        required_articles=max(0, int(keyword.get("required_articles") or 0)),
        valid_existing_articles=len(topics),
        target_rate=target_rate,
        recent_rate=recent_rate,
        existing_style_counts=style_counts,
        target_style_ratios=_target_choice_ratios(str(keyword.get("industry") or "")),
        point_cost=point_cost,
    )


def build_supplement_preview_from_db(
    conn: Any,
    keyword_id: int,
    *,
    for_update: bool = False,
) -> SupplementPreview:
    return build_supplement_preview(load_supplement_inputs(conn, keyword_id, for_update=for_update))


def expand_style_plan(preview: SupplementPreview) -> list[str]:
    """Expand the deterministic count map into stable per-topic user choices."""
    slots: list[str] = []
    for choice in sorted(preview.style_plan):
        slots.extend([choice] * max(0, int(preview.style_plan[choice] or 0)))
    if len(slots) != preview.suggested_articles:
        raise ValueError("文体计划与建议篇数不一致")
    return slots
