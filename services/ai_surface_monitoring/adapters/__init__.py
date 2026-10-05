"""平台表面 adapter 集合。

每个 adapter 实现 ``SurfaceAdapter`` Protocol,把一次真实 provider 调用收敛成
``ObservationEnvelopeV1``。adapter **不做品牌判断**、**不输出 mentioned=false**、
**不做 silent fallback**(发生 fallback 必须如实记录真实执行者)。
"""

from services.ai_surface_monitoring.adapters.base import (  # noqa: F401
    BaseSurfaceAdapter,
    EngineResult,
    extract_openai_usage,
)
from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter  # noqa: F401
from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter  # noqa: F401

__all__ = [
    "BaseSurfaceAdapter",
    "EngineResult",
    "extract_openai_usage",
    "WrappedResearchAdapter",
    "OpenAICompatibleAdapter",
]
