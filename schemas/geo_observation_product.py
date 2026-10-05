"""Product DTOs for the GEO observation analytics API.

Audience-separated, strict (``extra='forbid'``) so a forbidden field can never
be silently attached. Field names and business semantics are compatible with
the frozen ``frontend_race_fixture_v1.json``; the frontend maps ``*_bps`` and
enum values to the standard business labels in
``frontend_copy_and_race_v1.json`` (JSON keys are machine fields, not
user-facing text).

Privacy by construction:
  * PRIVATE DTOs may carry ``brand_id`` (the owner's own brand) but never
    ``owner_user_id`` / ``aggregate_key`` / upstream / cost / trace.
  * PUBLIC DTOs carry no ``owner_user_id`` / ``brand_id`` / ``aggregate_key``.
  * ADMIN DTOs may be traceable (event ids, policy versions) but never carry
    secrets, other-customer raw content, or provider cost/trace.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

OutcomeLiteral = Literal[
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
]
StabilityLiteral = Literal["stable", "watch", "insufficient", "shifted"]


class _StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ===========================================================================
# Shared value objects
# ===========================================================================
class WindowDTO(_StrictDTO):
    label: str
    start: str  # ISO date
    end: str


class OutcomeCountDTO(_StrictDTO):
    outcome: OutcomeLiteral
    count: int = Field(ge=0)


class NextActionDTO(_StrictDTO):
    action: str
    title: str
    reason: str
    requires_confirmation: bool
    may_charge: bool


class ErrorDTO(_StrictDTO):
    code: str
    message: str
    retryable: Optional[bool] = None


# ===========================================================================
# Private brand — summary
# ===========================================================================
class BrandRefDTO(_StrictDTO):
    brand_id: int
    display_name: str


class BrandSummaryMetricsDTO(_StrictDTO):
    # rate fields are nullable: with no data they are null (NOT a fabricated 0%,
    # which would read as "0% presence"). valid_observations=0 + stability
    # 'insufficient' say "no data". "null != 0" invariant.
    valid_observations: int = Field(ge=0)
    presence_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    explicit_recommendation_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    conditional_recommendation_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    candidate_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    criteria_only_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    refusal_no_evidence_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    refusal_risk_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    not_mentioned_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    citation_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    evidence_coverage_rate_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    share_of_voice_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    stability_status: StabilityLiteral
    stability_explanation: str


class ComparisonDTO(_StrictDTO):
    presence_change_bps: Optional[int] = None
    recommendation_change_bps: Optional[int] = None
    comparison_allowed: bool
    reason: Optional[str] = None


class BrandSummaryDTO(_StrictDTO):
    brand: BrandRefDTO
    window: WindowDTO
    data_updated_at: Optional[str] = None
    metric_version: str
    summary: BrandSummaryMetricsDTO
    comparison: ComparisonDTO
    outcomes: list[OutcomeCountDTO]
    next_actions: list[NextActionDTO]


# ===========================================================================
# Private brand — trend
# ===========================================================================
class TrendPointDTO(_StrictDTO):
    bucket_start: str
    bucket_end: str
    valid_observations: int = Field(ge=0)
    presence_rate_bps: int = Field(ge=0, le=10000)
    explicit_recommendation_rate_bps: int = Field(ge=0, le=10000)
    stability_status: StabilityLiteral
    model_shift_marker: bool
    confirmed_change: bool


class BrandTrendDTO(_StrictDTO):
    brand: BrandRefDTO
    window: WindowDTO
    metric_version: str
    granularity: Literal["day", "week", "month"]
    points: list[TrendPointDTO]
    comparison_note: Optional[str] = None


# ===========================================================================
# Private brand — platforms
# ===========================================================================
class PlatformItemDTO(_StrictDTO):
    platform_key: str
    display_name: str
    surface_note: Optional[str] = None
    valid_observations: int = Field(ge=0)
    presence_rate_bps: int = Field(ge=0, le=10000)
    explicit_recommendation_rate_bps: int = Field(ge=0, le=10000)
    stability_status: StabilityLiteral
    updated_at: Optional[str] = None


class HistoricalPlatformDTO(_StrictDTO):
    platform_key: str
    display_name: str
    status: Literal["historical"]
    last_observed_at: Optional[str] = None


class BrandPlatformsDTO(_StrictDTO):
    items: list[PlatformItemDTO]
    historical_platforms: list[HistoricalPlatformDTO]


# ===========================================================================
# Private brand — questions + evidence
# ===========================================================================
class QuestionItemDTO(_StrictDTO):
    observation_id: str
    question: str
    outcome: OutcomeLiteral
    matched_text: Optional[str] = None
    position: Optional[int] = Field(default=None, ge=1)
    citations: int = Field(ge=0)
    changed: bool
    evidence_available: bool


class QuestionsPageDTO(_StrictDTO):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)
    items: list[QuestionItemDTO]


class CitationDTO(_StrictDTO):
    domain: str
    source_type: Literal["citation", "source"]
    rank: Optional[int] = Field(default=None, ge=1)


class ChannelDisclosureDTO(_StrictDTO):
    platform: str
    note: Optional[str] = None


class EvidenceNextActionDTO(_StrictDTO):
    action: str
    title: str
    may_charge: bool


class EvidenceDetailDTO(_StrictDTO):
    observation_id: str
    question: str
    outcome: OutcomeLiteral
    answer_excerpt: Optional[str] = None
    matched_text: Optional[str] = None
    citations: list[CitationDTO]
    evidence_gap: list[str]
    next_action: Optional[EvidenceNextActionDTO] = None
    channel_disclosure: ChannelDisclosureDTO


# ===========================================================================
# Opportunities (private + public + admin)
# ===========================================================================
class OpportunityItemDTO(_StrictDTO):
    opportunity_id: str
    priority: int = Field(ge=1)
    topic: str
    recommended_content_type: Literal[
        "case_study", "comparison_method", "qualification_evidence", "faq", "data_report"
    ]
    recommended_evidence: list[str]
    recommended_media_pattern: list[str] = Field(default_factory=list)
    reason: str
    sample_size: int = Field(ge=0)
    stability: Literal["stable", "watch", "insufficient"]
    auto_action_allowed: Literal[False] = False


class OpportunitiesDTO(_StrictDTO):
    items: list[OpportunityItemDTO]


# ===========================================================================
# Public industry
# ===========================================================================
class IndustryBaselineDTO(_StrictDTO):
    industry_key: str
    window: WindowDTO
    metric_version: str
    valid_observations: int = Field(ge=0)
    presence_rate_bps: int = Field(ge=0, le=10000)
    explicit_recommendation_rate_bps: int = Field(ge=0, le=10000)
    conditional_recommendation_rate_bps: int = Field(ge=0, le=10000)
    criteria_only_rate_bps: int = Field(ge=0, le=10000)
    refusal_no_evidence_rate_bps: int = Field(ge=0, le=10000)
    refusal_risk_rate_bps: int = Field(ge=0, le=10000)
    citation_rate_bps: int = Field(ge=0, le=10000)
    evidence_coverage_rate_bps: int = Field(ge=0, le=10000)
    stability_status: StabilityLiteral
    sample_scope: str  # human-readable scope disclosure (never falls back silently)


class SourcePatternItemDTO(_StrictDTO):
    domain: str
    appearance_count: int = Field(ge=0)
    platforms: int = Field(ge=0)
    source_type: Literal["citation", "source"]


class IndustrySourcePatternsDTO(_StrictDTO):
    industry_key: str
    window: WindowDTO
    metric_version: str
    items: list[SourcePatternItemDTO]
    sample_scope: str


class PublicBaselineResponseDTO(_StrictDTO):
    """Public baseline never silently falls back; it explicitly reports whether
    the k-anonymity gate is met and the ACTUAL scope used."""

    status: Literal["ok", "insufficient_samples"]
    industry_key: str
    sample_scope: str
    message: Optional[str] = None
    baseline: Optional[IndustryBaselineDTO] = None


class PublicSourcePatternsResponseDTO(_StrictDTO):
    status: Literal["ok", "insufficient_samples"]
    industry_key: str
    sample_scope: str
    message: Optional[str] = None
    patterns: Optional[IndustrySourcePatternsDTO] = None


class PublicOpportunitiesResponseDTO(_StrictDTO):
    """Public content opportunities distinguish 'no opportunities' (status ok,
    empty items) from 'privacy-blocked' (status insufficient_samples)."""

    status: Literal["ok", "insufficient_samples"]
    industry_key: str
    sample_scope: str
    message: Optional[str] = None
    items: list[OpportunityItemDTO] = Field(default_factory=list)


# ===========================================================================
# Semantic insight job
# ===========================================================================
class InsightJobCreatedDTO(_StrictDTO):
    job_id: str
    state: Literal["pending", "running", "completed", "failed"]
    request_id: str


class InsightJobStatusDTO(_StrictDTO):
    job_id: str
    state: Literal["pending", "running", "completed", "failed"]
    request_id: Optional[str] = None
    progress_percent: Optional[int] = Field(default=None, ge=0, le=100)
    summary: Optional[str] = None
    evidence_refs: Optional[list[str]] = None
    allowed_actions: Optional[list[str]] = None


# ===========================================================================
# Admin
# ===========================================================================
class AdminPlatformHealthDTO(_StrictDTO):
    platform: str
    enabled_by_policy: bool
    runtime_health: Literal["healthy", "degraded", "unavailable", "unknown"]
    # exact surface/provider/model are ADMIN-ONLY (never in customer/service-
    # provider DTOs); users only ever see the product platform name.
    surface_key: Optional[str] = None
    provider_key: Optional[str] = None
    model_key: Optional[str] = None
    model_revision: Optional[str] = None


class AdminCountsDTO(_StrictDTO):
    pending_review: int = Field(ge=0)
    private_only: int = Field(ge=0)
    rejected: int = Field(ge=0)
    withdrawn: int = Field(ge=0)
    stuck_claims: int = Field(ge=0)


class CollectionReadinessDTO(_StrictDTO):
    """Mode-aware, auditable collection readiness for the admin control plane."""
    mode: Literal["existing_collectors_reconciled", "native_sampling_driver"]
    status: Literal["ready", "attention_required", "unavailable"]
    problems: list[str] = Field(default_factory=list)
    reconciler_last_success_at: Optional[str]
    reconciler_backlog: int = Field(ge=0)
    source_watermarks: dict[str, Optional[str]]
    duplicate_collection_jobs: list[str] = Field(default_factory=list)
    policy_version: Optional[int] = None


class AdminOverviewDTO(_StrictDTO):
    readiness: Literal["ready", "attention_required", "unavailable"]
    counts: AdminCountsDTO
    reconciler_delay_seconds: int = Field(ge=0)
    aggregate_updated_at: Optional[str] = None
    policy_version: Optional[int] = None
    platform_health: list[AdminPlatformHealthDTO]
    collection_readiness: CollectionReadinessDTO


class AdminModelShiftDTO(_StrictDTO):
    industry_key: str
    platform_key: Optional[str] = None
    model_revision: Optional[str] = None
    bucket_granularity: Literal["day", "week", "month"]
    bucket_start: str
    model_shift_index_bps: int = Field(ge=0, le=10000)
    stability_status: StabilityLiteral


class AdminModelShiftsDTO(_StrictDTO):
    items: list[AdminModelShiftDTO]


class AdminAggregateDiffItemDTO(_StrictDTO):
    industry_key: str
    metric: str
    shadow_bps: int
    legacy_bps: Optional[int] = None
    delta_bps: Optional[int] = None
    diff_reason: Optional[str] = None


class AdminAggregateDiffDTO(_StrictDTO):
    items: list[AdminAggregateDiffItemDTO]
    note: Optional[str] = None
