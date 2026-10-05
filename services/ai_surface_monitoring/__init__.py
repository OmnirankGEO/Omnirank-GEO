"""AI-1 平台采集包 · GEO 统一观测飞轮 vNext

采集头部:把不同平台/模型/搜索表面/采样策略收敛成同一 ``ObservationEnvelopeV1``,
供 AI-2 写入观测事件与隐私治理链。本包只做采集/血缘/采样/成本护栏,以及只读的
provider/model/surface 健康 DTO;不拥有业务策略配置(``geo_observation_policy`` 归 AI-2)、
不做聚合(AI-3)、不做前端、不改资金/RBAC/调度红线文件。

对外稳定入口(见 ``01_AI1_COLLECTION_PLATFORM_SAMPLING.md`` §9):
    - ``collect_observation(request) -> ObservationEnvelopeV1``
    - ``collect_batch(requests) -> AsyncIterator[ObservationEnvelopeV1]``
    - ``get_surface_matrix() -> SurfaceMatrix``
    - ``get_adapter_health() -> list[SurfaceHealth]``
    - ``register_observation_collection_jobs(scheduler) -> dict``  (纯函数,由集成者接线)

契约冻结源:``docs/AI-CONTEXT/GEO_OBSERVATION_FLYWHEEL_VNEXT_2026-07-17/contracts/observation_contract_v1.json``
"""

from services.ai_surface_monitoring.contracts import (  # noqa: F401
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    SURFACE_KEYS,
    RESPONSE_STATUSES,
    QUERY_KINDS,
    SESSION_MODES,
    HEALTH_STATUSES,
    SOURCE_KIND_TO_SOURCE_TYPE,
    CitationEvidence,
    SearchQueryEvidence,
    UsageEvidence,
    ObservationEnvelopeV1,
    CollectionRequest,
    SurfaceHealth,
    ModelLineage,
    SurfaceAdapter,
    canonical_question_hash,
    answer_text_hash,
)
from services.ai_surface_monitoring.registry import (  # noqa: F401
    AdapterRegistry,
    OrderPlatformEntitlement,
    SurfaceMatrix,
)
from services.ai_surface_monitoring.policy import (  # noqa: F401
    ObservationPolicyReader,
    ObservationPolicySnapshot,
    FakeObservationPolicy,
    renormalize_to_10000,
)
from services.ai_surface_monitoring.service import (  # noqa: F401
    CollectionService,
    SurfaceNotAllowedError,
    build_default_service,
    configure_default_service,
    collect_observation,
    collect_batch,
    get_surface_matrix,
    get_adapter_health,
)
from services.ai_surface_monitoring.scheduler_wiring import (  # noqa: F401
    register_observation_collection_jobs,
    set_sampling_driver,
)

__all__ = [
    "CONTRACT_VERSION",
    "SCHEMA_VERSION",
    "SURFACE_KEYS",
    "RESPONSE_STATUSES",
    "QUERY_KINDS",
    "SESSION_MODES",
    "HEALTH_STATUSES",
    "SOURCE_KIND_TO_SOURCE_TYPE",
    "CitationEvidence",
    "SearchQueryEvidence",
    "UsageEvidence",
    "ObservationEnvelopeV1",
    "CollectionRequest",
    "SurfaceHealth",
    "ModelLineage",
    "SurfaceAdapter",
    "canonical_question_hash",
    "answer_text_hash",
    # registry / policy
    "AdapterRegistry",
    "OrderPlatformEntitlement",
    "SurfaceMatrix",
    "ObservationPolicyReader",
    "ObservationPolicySnapshot",
    "FakeObservationPolicy",
    "renormalize_to_10000",
    # service (§9.1)
    "CollectionService",
    "SurfaceNotAllowedError",
    "build_default_service",
    "configure_default_service",
    "collect_observation",
    "collect_batch",
    "get_surface_matrix",
    "get_adapter_health",
    # scheduler wiring (纯函数,集成者接线)
    "register_observation_collection_jobs",
    "set_sampling_driver",
]
