"""geo-observation 管理治理后端 · 强类型请求/响应(extra='forbid')。

复用 schemas/admin_user_governance.py 的 StrictAdminModel + expected_version/reason 蓝本。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.geo_observation.contracts import ObservationPolicyV1


class StrictAdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Mutation(StrictAdminModel):
    reason: str = Field(..., min_length=2, max_length=500)
    request_id: str = Field(..., min_length=1, max_length=128)

    @field_validator("reason")
    @classmethod
    def _reason_meaningful(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("必须填写明确的操作原因")
        return v


class PutPolicyRequest(_Mutation):
    expected_policy_version: int = Field(..., ge=1)
    policy: ObservationPolicyV1


class PutPromotionGovernanceRequest(_Mutation):
    """法务晋升治理(CAS)。只批 promotion_legal_basis + consent_policy_version(法务可自主断言)。

    ⚠️ 不含 outcome_gold_gate_passed —— 金标准门由服务端从不可变评估证据派生(见 PostGoldEvaluationRequest),
       禁止直接提交可信布尔值(P1-1)。extra='forbid' 会拒绝任何残留的 outcome_gold_gate_passed 字段。
    """
    expected_policy_version: int = Field(..., ge=1)
    promotion_legal_basis: Optional[str] = Field(None, max_length=120)
    consent_policy_version: Optional[str] = Field(None, max_length=80)


class PostGoldEvaluationRequest(_Mutation):
    """提交一次金标准评估证据(P1-1)。服务端按契约 §601 阈值派生 outcome_gold_gate_passed,
    并写入 append-only 不可变评估记录。调用方只给证据,拿不到直接置门通过的通道。"""
    expected_policy_version: int = Field(..., ge=1)
    dataset_version: str = Field(..., min_length=1, max_length=80)
    sample_count: int = Field(..., ge=0)
    macro_f1_bps: int = Field(..., ge=0, le=10000)
    high_risk_false_reco: int = Field(..., ge=0)
    report_hash: str = Field(..., min_length=8, max_length=128)


class ReviewEventRequest(_Mutation):
    # 人工复核:仅可下调/撤回/重排;禁人工直接 promote(须走完整管线)
    decision: str = Field(..., description="private_only|rejected|requeue|withdraw")

    @field_validator("decision")
    @classmethod
    def _decision_enum(cls, v: str) -> str:
        if v not in ("private_only", "rejected", "requeue", "withdraw"):
            raise ValueError("decision 必须是 private_only/rejected/requeue/withdraw")
        return v


class WithdrawEventRequest(_Mutation):
    pass
