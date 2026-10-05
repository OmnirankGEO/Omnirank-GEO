"""Strict contracts for dedicated ADMIN cross-tenant governance actions."""

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictGovernanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReasonedMutation(StrictGovernanceModel):
    reason: str = Field(..., min_length=2, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("必须填写明确的治理原因")
        return value


class ChangeAccountStatusRequest(ReasonedMutation):
    active: bool
    expected_version: int = Field(..., ge=1)
    confirmation: Literal["CHANGE_ACCOUNT_STATUS"]


class DemoCaseSelection(StrictGovernanceModel):
    brand_id: int = Field(..., ge=1)
    diagnosis_id: int = Field(..., ge=1)
    case_id: UUID


class CreateDemoCaseGrantRequest(ReasonedMutation):
    grantee_kind: Literal["user", "organization"]
    grantee_user_id: Optional[int] = Field(default=None, ge=1)
    grantee_organization_id: Optional[int] = Field(default=None, ge=1)
    selections: list[DemoCaseSelection] = Field(..., min_length=1, max_length=100)
    capability: Literal["demo.customer.preview"] = "demo.customer.preview"
    note: str = Field(default="", max_length=500)
    expires_at: datetime
    confirmation: Literal["GRANT_DEMO_CUSTOMER_PREVIEW"]

    @model_validator(mode="after")
    def validate_shape(self):
        if self.grantee_kind == "user":
            valid = self.grantee_user_id is not None and self.grantee_organization_id is None
        else:
            valid = self.grantee_organization_id is not None and self.grantee_user_id is None
        if not valid:
            raise ValueError("授权对象必须与 grantee_kind 严格匹配")
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise ValueError("授权有效期必须晚于当前时间")
        if (expires_at - datetime.now(timezone.utc)).days > 366:
            raise ValueError("单次演示授权最长 366 天")
        return self


class RevokeDemoCaseGrantRequest(ReasonedMutation):
    expected_version: int = Field(..., ge=1)
    confirmation: Literal["REVOKE_DEMO_ACCESS"]


class DemoGrantVersion(StrictGovernanceModel):
    grant_id: int = Field(..., ge=1)
    expected_version: int = Field(..., ge=1)


class BatchRevokeDemoCaseGrantsRequest(ReasonedMutation):
    grants: list[DemoGrantVersion] = Field(..., min_length=1, max_length=100)
    confirmation: Literal["REVOKE_DEMO_ACCESS"]


class CreateProviderDowngradePlanRequest(ReasonedMutation):
    strategy: Literal["transfer_upstream", "platform_managed", "settle_then_downgrade"]
    target_provider_user_id: Optional[int] = Field(default=None, ge=1)
    expected_identity_version: int = Field(..., ge=1)
    confirmation: Literal["CREATE_PROVIDER_DOWNGRADE_PLAN"]

    @model_validator(mode="after")
    def validate_target(self):
        if self.strategy == "transfer_upstream" and self.target_provider_user_id is None:
            raise ValueError("转交上级必须指定承接服务商")
        if self.strategy != "transfer_upstream" and self.target_provider_user_id is not None:
            raise ValueError("仅转交上级策略可以指定承接服务商")
        return self


class ConfirmProviderDowngradePlanRequest(ReasonedMutation):
    expected_plan_version: int = Field(..., ge=1)
    confirmation: Literal["CONFIRM_PROVIDER_DOWNGRADE"]
