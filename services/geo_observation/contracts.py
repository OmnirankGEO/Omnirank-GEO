"""机器契约 v1.4 的 Python 镜像:枚举 / 信封 / policy / verifier 强类型 schema。

唯一真相是 contracts/observation_contract_v1.json;本文件逐字镜像其枚举与字段,
任何偏差都属开工阻断。所有对外写入 Pydantic model 均 extra='forbid'。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import SCHEMA_VERSION


# ────────────────────────────── 枚举(逐字镜像契约)──────────────────────────────
class SourceKind(str, Enum):
    research = "research"
    paid_diagnosis = "paid_diagnosis"
    monitoring = "monitoring"


class SourceType(str, Enum):
    research_round = "research_round"
    paid_diagnosis = "paid_diagnosis"
    recurring_monitoring = "recurring_monitoring"


# source_kind → source_type(固定映射;API 词汇与 DB 词汇不得各自猜)
SOURCE_KIND_TO_TYPE = {
    SourceKind.research: SourceType.research_round,
    SourceKind.paid_diagnosis: SourceType.paid_diagnosis,
    SourceKind.monitoring: SourceType.recurring_monitoring,
}


class SurfaceKey(str, Enum):
    doubao_ark_api_search = "doubao_ark_api_search"
    qwen_dashscope_search = "qwen_dashscope_search"
    deepseek_native_no_search = "deepseek_native_no_search"
    deepseek_native_with_search = "deepseek_native_with_search"
    deepseek_metaso_proxy = "deepseek_metaso_proxy"
    deepseek_dashscope_search_legacy = "deepseek_dashscope_search_legacy"
    yuanbao_app_verified = "yuanbao_app_verified"
    tencent_wsa_search = "tencent_wsa_search"
    yuanbao_hy3_tokenhub = "yuanbao_hy3_tokenhub"
    manual_app_capture = "manual_app_capture"
    other_explicit = "other_explicit"


class ResponseStatus(str, Enum):
    answered = "answered"
    refused = "refused"
    timeout = "timeout"
    error = "error"
    unknown = "unknown"
    budget_blocked = "budget_blocked"


class TargetOutcome(str, Enum):
    recommended = "recommended"
    conditionally_recommended = "conditionally_recommended"
    candidate_only = "candidate_only"
    mentioned_only = "mentioned_only"
    criteria_only = "criteria_only"
    refused_no_evidence = "refused_no_evidence"
    refused_risk = "refused_risk"
    not_mentioned = "not_mentioned"
    entity_ambiguous = "entity_ambiguous"
    engine_error = "engine_error"


class ProcessingState(str, Enum):
    pending = "pending"
    processing = "processing"
    pending_review = "pending_review"
    promoted = "promoted"
    private_only = "private_only"
    rejected = "rejected"
    withdrawn = "withdrawn"
    error = "error"


class EntityState(str, Enum):
    confirmed_mention = "confirmed_mention"
    confirmed_non_mention = "confirmed_non_mention"
    ambiguous = "ambiguous"
    unknown = "unknown"
    refused = "refused"
    provider_error = "provider_error"


class SessionMode(str, Enum):
    clean = "clean"
    anonymous = "anonymous"
    accounted = "accounted"
    unknown = "unknown"


class QueryKind(str, Enum):
    branded = "branded"
    non_branded = "non_branded"
    comparative = "comparative"
    verification = "verification"


class PromptIntent(str, Enum):
    awareness = "awareness"
    category_recommendation = "category_recommendation"
    comparison = "comparison"
    evaluation = "evaluation"
    transaction = "transaction"
    risk = "risk"
    branded = "branded"
    other = "other"


class Sentiment(str, Enum):
    positive = "positive"
    neutral = "neutral"
    negative = "negative"
    mixed = "mixed"
    unknown = "unknown"


class Stability(str, Enum):
    stable = "stable"
    watch = "watch"
    insufficient = "insufficient"
    shifted = "shifted"


class PromotionDecision(str, Enum):
    """晋升决策五态(spec §9.1)。"""
    pending_review = "pending_review"
    promoted = "promoted"
    private_only = "private_only"
    rejected = "rejected"
    withdrawn = "withdrawn"


# ────────────────────────────── 信封 v1(AI-1 → AI-2 冻结契约)──────────────────────────────
class SearchQueryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    kind: Optional[str] = None
    rank: Optional[int] = None
    provider_returned: bool = True

    @field_validator("provider_returned")
    @classmethod
    def _must_be_provider_returned(cls, v: bool) -> bool:
        # 契约 literal:true —— 只收 provider 明确返回的真实子查询,LLM 不得补造
        if v is not True:
            raise ValueError("search_queries 只接受 provider_returned=true 的真实子查询")
        return v


class CitationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    title: Optional[str] = None
    source_type: str = "citation"
    rank: Optional[int] = None


class UsageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost_micros: Optional[int] = None  # 整数 micros,禁 float
    usage_is_estimated: bool = False


class ObservationEnvelopeV1(BaseModel):
    """采集层向业务层返回的冻结信封。三个后端 AI 不得各造一版;前端禁读。

    owner_user_id/brand_id/question_text/prompt_text/answer_text 仅在私域链短暂传递,
    绝不写入公共 signal。金额一律整数 micros。未知值 null,禁猜模型版本。
    """
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    request_id: str
    source_kind: SourceKind
    source_ref: str
    owner_user_id: Optional[int] = None
    brand_id: Optional[int] = None
    industry_key: Optional[str] = None
    question_text: str
    question_hash: str
    query_kind: QueryKind
    platform_key: str
    provider_key: str
    model_key: str
    model_revision: Optional[str] = None
    search_provider: Optional[str] = None
    surface_key: SurfaceKey
    search_mode: Optional[str] = None
    search_enabled: Optional[bool] = None
    search_queries: Optional[list[SearchQueryEvidence]] = None
    country_code: str = "CN"
    region_key: Optional[str] = None
    session_mode: SessionMode
    run_index: int = 1
    prompt_text: str
    answer_text: str
    answer_hash: str
    response_status: ResponseStatus
    citations: list[CitationEvidence] = Field(default_factory=list)
    usage: UsageEvidence
    latency_ms: int = 0
    retry_of_request_id: Optional[str] = None
    provider_trace_id: Optional[str] = None
    observed_at: str  # RFC3339

    @field_validator("schema_version")
    @classmethod
    def _schema_version_fixed(cls, v: str) -> str:
        if v != SCHEMA_VERSION:
            raise ValueError(f"schema_version 必须为 {SCHEMA_VERSION}")
        return v


# ────────────────────────────── entity verifier 强类型输出(R5/Q2)──────────────────────────────
class VerifierOutput(BaseModel):
    """官方 DeepSeek verifier 的强类型输出。json_object 解析后必须过此校验;

    格式错/字段缺/位置不一致 → 视为 provider_error/UNKNOWN(R5),绝不自动晋升。
    """
    model_config = ConfigDict(extra="forbid")

    verdict: str  # 'YES' | 'NO' | 'UNKNOWN'
    reason: str = ""
    matched_text: Optional[str] = None
    window_index: Optional[int] = None
    matched_start: Optional[int] = None
    matched_end: Optional[int] = None

    @field_validator("verdict")
    @classmethod
    def _verdict_enum(cls, v: str) -> str:
        if v not in ("YES", "NO", "UNKNOWN"):
            raise ValueError("verdict 必须是 YES/NO/UNKNOWN")
        return v


# ────────────────────────────── policy_schema(AI-2 独占)──────────────────────────────
class PlatformPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_key: str
    enabled: bool
    base_weight_bps: int = Field(..., ge=0, le=10000)
    surface_key: Optional[SurfaceKey] = None  # enabled 时必填;legacy read-only 目录行可空
    legacy_read_only: bool = False

    @model_validator(mode="after")
    def _enabled_needs_surface(self) -> "PlatformPolicyV1":
        if self.enabled and self.surface_key is None:
            raise ValueError(f"enabled 平台 {self.platform_key} 必须有 surface_key")
        if self.enabled and self.base_weight_bps <= 0:
            raise ValueError(f"enabled 平台 {self.platform_key} base_weight_bps 必须为正")
        return self


class SourceBaseWeightsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    research: int = Field(..., ge=0, le=10000)
    paid_diagnosis: int = Field(..., ge=0, le=10000)
    monitoring: int = Field(..., ge=0, le=10000)


class SamplingBudgetV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_calls_per_round: int = Field(..., gt=0)
    max_calls_per_day: int = Field(..., gt=0)
    max_cost_micros_per_day: int = Field(..., gt=0)
    max_retry_calls_per_request: int = Field(..., ge=0)


class ObservationFeatureFlagsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ingest_enabled: bool
    promotion_enabled: bool
    aggregation_enabled: bool
    product_enabled: bool


class ObservationPolicyV1(BaseModel):
    """geo_observation_policy 的可写业务策略(runtime provider 健康不在此,由 AI-1 只读)。"""
    model_config = ConfigDict(extra="forbid")

    policy_version: str
    platforms: list[PlatformPolicyV1]
    source_base_weights_bps: SourceBaseWeightsV1
    sampling_budget: SamplingBudgetV1
    max_single_brand_share_bps: int = Field(..., ge=0, le=10000)
    public_min_independent_brands: int = Field(..., ge=3)
    public_min_source_types: int = Field(..., ge=2)
    retention_days: int = Field(..., gt=0)
    anomaly_confirmation_numerator: int = Field(..., gt=0)
    anomaly_confirmation_denominator: int = Field(..., gt=0)
    feature_flags: ObservationFeatureFlagsV1

    @model_validator(mode="after")
    def _policy_checks(self) -> "ObservationPolicyV1":
        # 契约 policy_schema.checks
        keys = [p.platform_key for p in self.platforms]
        if len(keys) != len(set(keys)):
            raise ValueError("platform_key 必须唯一")
        if not any(p.enabled and not p.legacy_read_only for p in self.platforms):
            raise ValueError("至少一个非 legacy 平台 enabled")
        total = sum(p.base_weight_bps for p in self.platforms)
        if total != 10000:
            raise ValueError(f"platform base weights 全目录合计必须=10000(当前 {total})")
        if self.anomaly_confirmation_denominator < self.anomaly_confirmation_numerator:
            raise ValueError("anomaly_confirmation_denominator 必须 >= numerator")
        return self
