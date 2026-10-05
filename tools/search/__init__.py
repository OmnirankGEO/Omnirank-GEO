# Search Tools Package
from .metaso_search import (
    metaso_search,
    search_web_for_geo,
    search_scholar_for_geo,
)

# [搜索 Provider 适配层 2026-07-28] 场景键路由 + 豆包直用入口(D 级新场景)。
# 报价链 7 文件禁止 import 这些符号(工单 §1-A 底线锁)。
from .provider_router import (
    search_scenario,
    resolve_provider,
    citation_search,
    scholar_evidence_search,
)
from .doubao_search import (
    doubao_search_webpages,
    multi_angle_search,
    is_doubao_search_configured,
)

__all__ = [
    "metaso_search",
    "search_web_for_geo",
    "search_scholar_for_geo",
    "search_scenario",
    "resolve_provider",
    "citation_search",
    "scholar_evidence_search",
    "doubao_search_webpages",
    "multi_angle_search",
    "is_doubao_search_configured",
]
