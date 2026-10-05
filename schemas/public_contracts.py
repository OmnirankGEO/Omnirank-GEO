"""Explicit public/admin DTO boundaries for commercial relationships and branding."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerServiceDTO(_StrictDTO):
    """Customer-safe service configuration; never includes the internal provider."""

    service_status: Literal["platform_managed"] = "platform_managed"
    configuration_status: Literal["ready", "review_required", "unavailable"]
    account_configured: bool
    dispute_pending: bool = False


class AgentServiceDTO(_StrictDTO):
    """Agent-safe procurement configuration; never includes an upstream identity."""

    service_status: Literal["platform_managed"] = "platform_managed"
    configuration_status: Literal["ready", "review_required", "unavailable"]


class AdminRelationshipDTO(_StrictDTO):
    """Full relationship evidence. This DTO is restricted to admin endpoints."""

    customer_user_id: int
    service_user_id: Optional[int] = None
    relationship_id: Optional[int] = None
    relationship_version: Optional[str] = None
    resolution: Literal["BOUND", "PLATFORM_DIRECT", "CONFLICT", "UNAVAILABLE"]
    dispute_status: Optional[str] = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class PublicBrandDTO(_StrictDTO):
    """Approved display brand only; contains no account or authorization metadata."""

    company_name: str
    product_name: Optional[str] = None
    logo_url: str
    favicon_url: Optional[str] = None
    slogan: Optional[str] = None
    brand_color: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_wechat: Optional[str] = None
    contact_email: Optional[str] = None


class PublicBrandingDTO(_StrictDTO):
    display_scope: Literal["platform", "approved_whitelabel"]
    brand: PublicBrandDTO
