"""Compatibility exports for the canonical commercial service routing SSOT.

New code must import :mod:`services.commercial_service_routing` directly.
"""

from services.commercial_service_routing import (  # noqa: F401
    CommercialRelationship,
    CommercialServiceRoutingError,
    PlatformDirectUnavailable,
    RelationshipConflict,
    RelationshipError,
    RelationshipResolution,
    classify_commercial_relationship,
    get_platform_direct_service_user_id,
    lock_commercial_binding_subject as lock_commercial_relationship,
    platform_direct_readiness,
    public_relationship_http_error,
    read_locked_commercial_binding,
    resolve_commercial_relationship,
    resolve_effective_service_provider,
)
