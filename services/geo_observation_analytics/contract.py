"""Frozen contract constants for GEO Observation Analytics (AI-3).

This module is the in-code SSOT for the enums, version strings, aggregate field
list and privacy allow/deny lists that AI-3 implements. Every value here is
asserted against the frozen machine contract
``contracts/observation_contract_v1.json`` (SHA256 65C1..D5AA) by
``tests/geo_observation_analytics/test_contract_lock.py`` so code and contract
can never silently drift.

Nothing in this module reaches the database or the network; it is pure data.
"""

from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Versions (must match the frozen fixture cases and machine contract exactly)
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "geo-observation-v1"
CONTRACT_VERSION = "geo-observation-contract-v1.4"
METRIC_VERSION = "geo-observation-metrics-v1"
AGGREGATION_VERSION = "geo-observation-aggregation-v1"
COPY_CONTRACT_VERSION = "geo-observation-frontend-race-v1"
FIXTURE_VERSION = "geo-observation-frontend-fixture-v1"

# ---------------------------------------------------------------------------
# Ten-class target outcome — the ONLY SSOT for outcome semantics.
# ---------------------------------------------------------------------------
TARGET_OUTCOMES: tuple[str, ...] = (
    "recommended",
    "conditionally_recommended",
    "candidate_only",
    "mentioned_only",
    "criteria_only",
    "refused_no_evidence",
    "refused_risk",
    "not_mentioned",
    "entity_ambiguous",
    "engine_error",
)

# Outcomes that are excluded from the valid-observation denominator entirely
# (invariant I5 + §3.3). entity_ambiguous == unresolved entity; engine_error ==
# timeout/parse/provider/collection failure. UNKNOWN never enters a denominator.
OUTCOMES_EXCLUDED_FROM_VALID: frozenset[str] = frozenset(
    {"entity_ambiguous", "engine_error"}
)

# The 8 valid product behaviors (denominator = sum of these).
VALID_OUTCOMES: tuple[str, ...] = tuple(
    o for o in TARGET_OUTCOMES if o not in OUTCOMES_EXCLUDED_FROM_VALID
)

# Outcomes where the target brand is actually present/mentioned in the answer.
PRESENCE_OUTCOMES: frozenset[str] = frozenset(
    {"recommended", "conditionally_recommended", "candidate_only", "mentioned_only"}
)

# The two refusal-to-name behaviors (kept separate; refusal_rate is display-only).
REFUSAL_OUTCOMES: frozenset[str] = frozenset(
    {"refused_no_evidence", "refused_risk"}
)

# ---------------------------------------------------------------------------
# Other frozen enums
# ---------------------------------------------------------------------------
SURFACE_KEYS: tuple[str, ...] = (
    "doubao_ark_api_search",
    "qwen_dashscope_search",
    "deepseek_native_no_search",
    "deepseek_native_with_search",
    "deepseek_metaso_proxy",
    "deepseek_dashscope_search_legacy",
    "yuanbao_app_verified",
    "tencent_wsa_search",
    "yuanbao_hy3_tokenhub",
    "manual_app_capture",
    "other_explicit",
)

RESPONSE_STATUSES: tuple[str, ...] = (
    "answered",
    "refused",
    "timeout",
    "error",
    "unknown",
    "budget_blocked",
)

EVENT_PROCESSING_STATES: tuple[str, ...] = (
    "pending",
    "processing",
    "pending_review",
    "promoted",
    "private_only",
    "rejected",
    "withdrawn",
    "error",
)

# The ONLY processing state eligible for aggregation.
ELIGIBLE_PROCESSING_STATE = "promoted"

STABILITY_STATES: tuple[str, ...] = ("stable", "watch", "insufficient", "shifted")

SCOPE_TYPES: tuple[str, ...] = ("private_brand", "public_industry", "admin_shadow")

BUCKET_GRANULARITIES: tuple[str, ...] = ("day", "week", "month")

SOURCE_TYPES: tuple[str, ...] = (
    "research_round",
    "paid_diagnosis",
    "recurring_monitoring",
)

SOURCE_KIND_TO_SOURCE_TYPE: dict[str, str] = {
    "research": "research_round",
    "paid_diagnosis": "paid_diagnosis",
    "monitoring": "recurring_monitoring",
}

PROMPT_INTENTS: tuple[str, ...] = (
    "awareness",
    "category_recommendation",
    "comparison",
    "evaluation",
    "transaction",
    "risk",
    "branded",
    "other",
)

# ---------------------------------------------------------------------------
# Aggregate table field names (contract order) — used to build DTOs and by the
# drift test to prove parity with aggregate_schema.fields.
# ---------------------------------------------------------------------------
AGGREGATE_FIELD_NAMES: tuple[str, ...] = (
    "aggregate_id",
    "aggregate_key",
    "contract_version",
    "aggregation_version",
    "metric_version",
    "policy_version",
    "scope_type",
    "owner_user_id",
    "brand_id",
    "industry_key",
    "bucket_granularity",
    "bucket_start",
    "bucket_end",
    "prompt_family_key",
    "prompt_intent",
    "is_branded_prompt",
    "platform_key",
    "surface_key",
    "model_revision",
    "search_enabled",
    "source_type",
    "search_query_theme",
    "valid_observations",
    "independent_user_buckets",
    "independent_brand_buckets",
    "independent_source_types",
    "weighted_denominator_micros",
    "recommended_count",
    "conditionally_recommended_count",
    "candidate_only_count",
    "mentioned_only_count",
    "criteria_only_count",
    "refused_no_evidence_count",
    "refused_risk_count",
    "not_mentioned_count",
    "presence_rate_bps",
    "explicit_recommendation_rate_bps",
    "conditional_recommendation_rate_bps",
    "candidate_rate_bps",
    "mentioned_only_rate_bps",
    "criteria_only_rate_bps",
    "refusal_no_evidence_rate_bps",
    "refusal_risk_rate_bps",
    "not_mentioned_rate_bps",
    "refusal_rate_bps",
    "citation_rate_bps",
    "source_visibility_rate_bps",
    "evidence_coverage_rate_bps",
    "rank_top1_count",
    "rank_top3_count",
    "rank_top5_count",
    "rank_not_listed_count",
    "avg_position_milli",
    "share_of_voice_bps",
    "volatility_bps",
    "confirmed_change",
    "trend_confidence_bps",
    "model_shift_index_bps",
    "stability_status",
    "intrinsic_memory_rate_bps",
    "retrieval_selection_rate_bps",
    "input_watermark",
    "computed_at",
    "created_at",
    "updated_at",
)

# ---------------------------------------------------------------------------
# Privacy: fields that must NEVER appear in a public/service-provider DTO.
# ---------------------------------------------------------------------------
# The minimal contract-forbidden set for public aggregates.
PUBLIC_DTO_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {"owner_user_id", "brand_id", "aggregate_key"}
)

# The broader cross-tenant / upstream / cost / trace deny-list (§5.4 + 04 §3.4)
# applied to ALL non-admin (customer + service-provider) DTOs, private or public.
# NOTE: brand_id is intentionally NOT here — a private-brand DTO legitimately
# shows the owner their own brand_id (the frozen fixture's brand_summary carries
# it). brand_id is forbidden only in PUBLIC industry DTOs (see below).
PRIVACY_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "owner_user_id",
        "agent_user_id",
        "upstream_user_id",
        "service_account_code",
        "channel_account_code",
        "source_account_code",
        "cost_multiplier",
        "internal_cost",
        "provider_cost",
        "internal_model_route",
        "provider_trace_id",
        "source_record_id",
        "event_id",
        "aggregate_key",
        "contributor_user_bucket",
        "contributor_brand_bucket",
        "price_multiplier_bps",
        "cost_multiplier_bps",
    }
)

# The full deny-list for PUBLIC industry DTOs = the general list plus brand_id
# (and owner_user_id/aggregate_key already present).
PUBLIC_DTO_ALL_FORBIDDEN_FIELDS: frozenset[str] = (
    PRIVACY_FORBIDDEN_FIELDS | PUBLIC_DTO_FORBIDDEN_FIELDS
)

# ---------------------------------------------------------------------------
# Product-layer platform display (§7.2). platform_key is already a product label
# ("doubao"/"qwen"/"deepseek"/"yuanbao"/"kimi"); surface_key is the technical
# surface, shown only in admin / as an honest business note to users.
# ---------------------------------------------------------------------------
PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "doubao": "豆包",
    "qwen": "千问",
    "deepseek": "DeepSeek",
    "yuanbao": "元宝",
    "kimi": "Kimi",
}

# Kimi is a historical (default-retired) platform.
HISTORICAL_PLATFORM_KEYS: frozenset[str] = frozenset({"kimi"})

# Honest surface notes for non-native / proxy surfaces shown to users (never the
# raw provider/model). Native surfaces have no note.
SURFACE_USER_NOTES: dict[str, str] = {
    "deepseek_metaso_proxy": "部分结果由 DeepSeek 模型配合秘塔检索代理获得",
    "deepseek_dashscope_search_legacy": "部分结果由 DeepSeek 模型配合外部检索获得",
    "tencent_wsa_search": "结果来自腾讯网页搜索能力",
}

# ---------------------------------------------------------------------------
# Semantic engine (official DeepSeek) — fixed identity (§6.1 / 03 §6.1).
# ---------------------------------------------------------------------------
SEMANTIC_PROVIDER = "deepseek"
SEMANTIC_BASE_URL = "https://api.deepseek.com"
SEMANTIC_MODEL = DEEPSEEK_OFFICIAL_FLASH
SEMANTIC_THINKING = "disabled"
SEMANTIC_PROMPT_VERSION = "geo-observation-insight-prompt-v1"
SEMANTIC_SCHEMA_VERSION = "geo-observation-insight-schema-v1"

# ---------------------------------------------------------------------------
# Default numeric gates (04 §7.5). Overridable by geo_observation_policy (AI-2),
# but never lowered silently.
# ---------------------------------------------------------------------------
PUBLIC_MIN_VALID_OBSERVATIONS = 10
PUBLIC_MIN_INDEPENDENT_USER_BUCKETS = 3
PUBLIC_MIN_INDEPENDENT_BRAND_BUCKETS = 3
PUBLIC_MIN_SOURCE_TYPES = 2

# Default source base weights (§8.3). policy SSOT authoritative at runtime.
DEFAULT_SOURCE_BASE_WEIGHTS_BPS: dict[str, int] = {
    "research_round": 10000,
    "recurring_monitoring": 7000,
    "paid_diagnosis": 4000,
}
DEFAULT_MAX_SINGLE_BRAND_SHARE_BPS = 1000
DEFAULT_ANOMALY_CONFIRMATION_NUMERATOR = 2
DEFAULT_ANOMALY_CONFIRMATION_DENOMINATOR = 3

# ---------------------------------------------------------------------------
# Frozen JSON loaders (used by tests / OpenAPI compatibility checks).
# ---------------------------------------------------------------------------
_CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"


@lru_cache(maxsize=None)
def load_frozen_contract() -> dict[str, Any]:
    """Return the vendored observation_contract_v1.json as a dict."""
    return json.loads(
        (_CONTRACTS_DIR / "observation_contract_v1.json").read_text(encoding="utf-8")
    )


@lru_cache(maxsize=None)
def load_frozen_copy_contract() -> dict[str, Any]:
    return json.loads(
        (_CONTRACTS_DIR / "frontend_copy_and_race_v1.json").read_text(encoding="utf-8")
    )


@lru_cache(maxsize=None)
def load_frozen_fixture() -> dict[str, Any]:
    return json.loads(
        (_CONTRACTS_DIR / "frontend_race_fixture_v1.json").read_text(encoding="utf-8")
    )


def contract_path(name: str) -> Path:
    """Absolute path to a vendored contract file (for hash-lock tests)."""
    return _CONTRACTS_DIR / name
