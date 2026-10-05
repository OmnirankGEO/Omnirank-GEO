"""冻结的跨包信封契约 + adapter Protocol。

机器契约 SSOT:``contracts/observation_contract_v1.json``。本文件的枚举、字段名、
类型、可空性必须与该 JSON 逐字段一致(见 ``tests/ai_surface_monitoring/test_contract_alignment.py``
的自动比对)。任何 prose 与机器契约不一致都属于开工阻断。

设计约束(不引入任何 DB / 网络 / 重依赖,保证 ``import`` 干净、测试零副作用):
只依赖标准库 + pydantic。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 契约常量(与 observation_contract_v1.json 对齐)
# ---------------------------------------------------------------------------

CONTRACT_VERSION = "geo-observation-contract-v1.4"
SCHEMA_VERSION = "geo-observation-v1"

# source_kind(API 词汇) → source_type(DB 词汇)。API 与 DB 不得各自猜映射。
SOURCE_KIND_TO_SOURCE_TYPE: Dict[str, str] = {
    "research": "research_round",
    "paid_diagnosis": "paid_diagnosis",
    "monitoring": "recurring_monitoring",
}

SourceKind = Literal["research", "paid_diagnosis", "monitoring"]

# §3.2 表面枚举 · 不允许 api/enhanced/default 模糊值
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
SurfaceKey = Literal[
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
]

RESPONSE_STATUSES: tuple[str, ...] = (
    "answered",
    "refused",
    "timeout",
    "error",
    "unknown",
    "budget_blocked",
)
ResponseStatus = Literal["answered", "refused", "timeout", "error", "unknown", "budget_blocked"]

QUERY_KINDS: tuple[str, ...] = ("branded", "non_branded", "comparative", "verification")
QueryKind = Literal["branded", "non_branded", "comparative", "verification"]

SESSION_MODES: tuple[str, ...] = ("clean", "anonymous", "accounted", "unknown")
SessionMode = Literal["clean", "anonymous", "accounted", "unknown"]

HEALTH_STATUSES: tuple[str, ...] = ("healthy", "degraded", "unavailable", "unknown")
HealthStatus = Literal["healthy", "degraded", "unavailable", "unknown"]

# citation 与 source 概念分开(§5):citation = 答案明确引用/采纳;source = 访问过但未必引用。
SourceType = Literal["citation", "source"]


# ---------------------------------------------------------------------------
# 哈希 helper(§5:question_hash 用规范化后问题,但不覆盖原文;answer_hash 为原文哈希)
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_question(question_text: str) -> str:
    """规范化用于 hash / 幂等:去首尾空白 + 折叠内部连续空白 + NFKC。

    注意:这只用于 *哈希与幂等*,绝不用于替换实际发给 provider 的 question_text。
    """
    import unicodedata

    normalized = unicodedata.normalize("NFKC", question_text or "")
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def canonical_question_hash(question_text: str) -> str:
    """sha256(规范化 question_text)。"""
    return hashlib.sha256(normalize_question(question_text).encode("utf-8")).hexdigest()


def answer_text_hash(answer_text: str) -> str:
    """sha256(原始答案),不复制正文。空答返回空串的哈希(仍是稳定 64 hex)。"""
    return hashlib.sha256((answer_text or "").encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 证据子结构
# ---------------------------------------------------------------------------


class SearchQueryEvidence(BaseModel):
    """引擎真实返回的 grounding / fanout query。永不推断补写。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    kind: Optional[str] = None
    rank: Optional[int] = None
    # 契约固定为 literal True:只接收 provider 明确返回的真实子查询。
    provider_returned: Literal[True] = True


class CitationEvidence(BaseModel):
    """显式引用 / 访问来源。``source_type`` 区分 citation(采纳引用) 与 source(仅访问)。"""

    model_config = ConfigDict(extra="forbid")

    url: str
    title: Optional[str] = None
    source_type: SourceType = "citation"
    rank: Optional[int] = None


class UsageEvidence(BaseModel):
    """token 与整数 micro-cost。

    Codex 裁定(#6):成本未知写 ``null``,不能写假 0;``usage_is_estimated`` 标记成本
    是否为估算。金额统一整数 micros,禁止 float。
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost_micros: Optional[int] = None
    usage_is_estimated: bool = False


# ---------------------------------------------------------------------------
# ObservationEnvelopeV1 · 冻结跨包信封(§5 / envelope_fields)
# ---------------------------------------------------------------------------


class ObservationEnvelopeV1(BaseModel):
    """采集层向业务层(AI-2)返回的唯一信封。三个后端 AI 不得各造一版;两个前端候选禁止直接读取。

    ``extra='forbid'`` 落实契约"未知字段拒绝"。所有 adapter 都必须能产出本结构。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["geo-observation-v1"] = SCHEMA_VERSION
    request_id: str
    source_kind: SourceKind
    source_ref: str
    owner_user_id: Optional[int] = None
    brand_id: Optional[int] = None
    industry_key: Optional[str] = None
    # question_text 是用户可理解的规范问题(客户购买短句原文);question_hash 为其规范化哈希。
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
    # 调用时是否打开检索;provider 不披露则 None。
    search_enabled: Optional[bool] = None
    # 只接收 provider 明确返回的真实子查询;API 不提供时为 None(禁止 LLM 补造)。
    search_queries: Optional[List[SearchQueryEvidence]] = None
    country_code: str = "CN"
    region_key: Optional[str] = None
    session_mode: SessionMode
    run_index: int = 1
    # prompt_text = 实际发给 provider 的完整 prompt(含 system framing);与 question_text 不得互相覆盖。
    prompt_text: str
    answer_text: str
    answer_hash: str
    response_status: ResponseStatus
    citations: List[CitationEvidence] = Field(default_factory=list)
    usage: UsageEvidence = Field(default_factory=UsageEvidence)
    latency_ms: int = 0
    retry_of_request_id: Optional[str] = None
    provider_trace_id: Optional[str] = None
    observed_at: datetime

    # ------ 便捷构造/校验 ------
    def source_type(self) -> str:
        return SOURCE_KIND_TO_SOURCE_TYPE[self.source_kind]


# ---------------------------------------------------------------------------
# 采集请求 / 健康 / 血缘
# ---------------------------------------------------------------------------


class CollectionRequest(BaseModel):
    """一次采集请求。``question_text`` 是逐字 SSOT:有值时 adapter 必须原样发给 provider,
    不得替换为"关键词哪家好"或让 LLM 改写。``prompt_text`` 才承载 system framing。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    source_kind: SourceKind
    source_ref: str
    # 客户购买短句原文(逐字 SSOT)。禁止被模板/LLM 覆盖。
    question_text: str
    query_kind: QueryKind = "non_branded"
    surface_key: SurfaceKey
    owner_user_id: Optional[int] = None
    brand_id: Optional[int] = None
    industry_key: Optional[str] = None
    country_code: str = "CN"
    region_key: Optional[str] = None
    session_mode: SessionMode = "clean"
    run_index: int = 1
    retry_of_request_id: Optional[str] = None
    # 目标品牌/别名由上游品牌解析契约提供,adapter 不自行硬编码(用于下游实体判定,不由 adapter 判断)。
    target_brand: Optional[str] = None
    brand_display_names: Optional[List[str]] = None
    # provider system framing 只进入 prompt_text,不改变 question_text 意图。默认与 question_text 相同。
    prompt_text_override: Optional[str] = None
    search_mode: Optional[str] = None
    # 单次请求允许的**重试次数上限**(来自 policy sampling_budget.max_retry_calls_per_request;P1-4)。
    # None 表示由 adapter 默认;service 会在采集前按 policy 注入。
    max_retry_calls: Optional[int] = None
    # 传入即透传给成本护栏做归属;不参与扣费(计费红线归 middleware/billing.py)。
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def effective_prompt_text(self) -> str:
        """实际发给 provider 的 prompt。默认逐字等于 question_text(无 system framing 时)。"""
        return self.prompt_text_override if self.prompt_text_override is not None else self.question_text


class SurfaceHealth(BaseModel):
    """只读健康 DTO(机器契约 ``platform_health_fields``)。

    只反映真实 provider/model/surface 探针,禁止把健康状态写回 policy 冒充管理员配置,
    也禁止把 unavailable 手工改成 healthy。
    """

    model_config = ConfigDict(extra="forbid")

    platform_key: str
    provider_key: str
    model_key: str
    surface_key: SurfaceKey
    status: HealthStatus
    checked_at: datetime
    latency_ms: Optional[int] = None
    model_revision: Optional[str] = None
    error_code: Optional[str] = None


class ModelLineage(BaseModel):
    """血缘:每条结果必须能回答 §5.2 的八问(产品/供应商/表面/模型版本/是否联网/是否 fallback/采样时间/成本时延)。"""

    model_config = ConfigDict(extra="forbid")

    product_label: str          # 面向管理员的产品名(如 "元宝");面向客户另有话术层
    platform_key: str
    provider_key: str
    surface_key: SurfaceKey
    model_key: str
    model_revision: Optional[str] = None
    search_provider: Optional[str] = None
    search_enabled: Optional[bool] = None
    fallback_occurred: bool = False
    fallback_detail: Optional[str] = None


@runtime_checkable
class SurfaceAdapter(Protocol):
    """每个平台表面 adapter 的统一协议(§5)。"""

    surface_key: str

    async def probe(self) -> SurfaceHealth: ...

    async def collect(self, request: CollectionRequest) -> ObservationEnvelopeV1: ...

    def describe_lineage(self) -> ModelLineage: ...


# 类型别名:批量迭代
ObservationBatch = AsyncIterator[ObservationEnvelopeV1]
