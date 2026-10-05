"""
services/marketing/case_schema.py — 建议案件 10 字段强 schema(pydantic 强校验)

总设计 §9.4:marketing_cases 必填 10 字段,拒绝自由文本建议。
"建议你发优惠券"这种一句话产物直接校验失败。
无论产出来自"规则确定性草稿"还是"LLM 撰写",都过这个 model 再落库。
"""
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from services.marketing.guards import check_no_promise


class AudienceSpec(BaseModel):
    definition: str = Field(..., min_length=2, description="目标人群定义")
    count: int = Field(..., ge=0, description="人数")
    segment: str = Field(default="end", description="end(终端) / provider(服务商)")


class BudgetSpec(BaseModel):
    points: int = Field(default=0, ge=0, description="预算算力上限")
    cost_estimate_yuan: float = Field(default=0.0, ge=0, description="真实成本估(元)")
    note: str = Field(default="")


class MarketingCaseDraft(BaseModel):
    """军师建议案件草稿(10 字段 + 元信息)。model_dump() 后喂 marketing_db.create_case。"""

    # —— 元信息 ——
    rule_key: str = Field(..., min_length=1)
    fingerprint: str = Field(default="")
    awareness_stage: str = Field(default="unknown")  # 施瓦茨觉醒 5 阶段
    owner_scope: str = Field(default="platform")
    skill_packs: list[str] = Field(default_factory=list)

    # —— 10 字段强 schema ——
    trigger_reason: str = Field(..., min_length=4, description="① 触发原因")
    evidence: dict[str, Any] = Field(..., description="② 证据数据(查询+快照)")
    audience: AudienceSpec = Field(..., description="③ 目标人群定义+人数")
    expected_impact: str = Field(..., min_length=4, description="④ 预计影响(相关转化口径)")
    budget_cost: BudgetSpec = Field(..., description="⑤ 预算")
    risk_level: str = Field(..., description="⑥ 风险等级 low/med/high")
    touch_copy: dict[str, str] = Field(..., description="⑦ 触达文案成品(分渠道)")
    execution_plan: dict[str, Any] = Field(..., description="⑧ 执行计划")
    rollback_plan: str = Field(..., min_length=4, description="⑨ 回滚方案(触达不可撤回须显式标注)")
    observation_window: int = Field(default=7, ge=1, le=90, description="⑩ 观察窗口 N 天")

    @field_validator("risk_level")
    @classmethod
    def _risk(cls, v: str) -> str:
        if v not in ("low", "med", "high"):
            raise ValueError("risk_level 必须是 low/med/high")
        return v

    @field_validator("owner_scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        if v not in ("platform", "user"):
            raise ValueError("owner_scope 必须是 platform/user")
        return v

    @field_validator("evidence")
    @classmethod
    def _evidence_nonempty(cls, v: dict) -> dict:
        if not v:
            raise ValueError("evidence 不能为空(必须带查询证据/快照)")
        return v

    @field_validator("execution_plan")
    @classmethod
    def _plan_nonempty(cls, v: dict) -> dict:
        if not v:
            raise ValueError("execution_plan 不能为空(自由文本一句话建议直接失败)")
        return v

    @field_validator("touch_copy")
    @classmethod
    def _copy_nonempty(cls, v: dict) -> dict:
        if not v or not any((t or "").strip() for t in v.values()):
            raise ValueError("touch_copy 至少一个渠道文案非空")
        return v

    @model_validator(mode="after")
    def _no_promise_in_copy(self) -> "MarketingCaseDraft":
        # 禁承诺守卫:任一渠道文案含承诺词 → 校验失败(与出口守卫双保险)
        for channel, text in (self.touch_copy or {}).items():
            hits = check_no_promise(text or "")
            if hits:
                raise ValueError(f"touch_copy[{channel}] 含承诺词 {hits},禁承诺守卫拦截")
            if "积分" in (text or ""):
                raise ValueError(f"touch_copy[{channel}] 含'积分'(对客必须'算力')")
        return self

    def to_db_kwargs(self, *, case_key: Optional[str] = None, status: str = "pending",
                     llm_model: str = "", created_by: Optional[int] = None,
                     expires_at=None) -> dict:
        """转成 marketing_db.create_case 的 kwargs。"""
        return dict(
            rule_key=self.rule_key,
            fingerprint=self.fingerprint,
            case_key=case_key,
            owner_scope=self.owner_scope,
            awareness_stage=self.awareness_stage,
            trigger_reason=self.trigger_reason,
            evidence=self.evidence,
            audience=self.audience.model_dump(),
            expected_impact=self.expected_impact,
            budget_cost=self.budget_cost.model_dump(),
            risk_level=self.risk_level,
            touch_copy=self.touch_copy,
            execution_plan=self.execution_plan,
            rollback_plan=self.rollback_plan,
            observation_window_days=self.observation_window,
            skill_packs=self.skill_packs,
            status=status,
            llm_model=llm_model,
            created_by=created_by,
            expires_at=expires_at,
        )
