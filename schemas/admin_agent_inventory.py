"""服务商库存管理侧 DTO(工单 WO_INVENTORY_POINTS_DEADLOCK_2026-08-12 §P0-1 §P0-3)。

🔴 与 `schemas/admin_user_governance.py` 同规矩:`extra="forbid"` ——
   字段不在这里声明 = 整个接口 500,不是"字段看不见"。
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictInventoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InventoryWalletSnapshot(StrictInventoryModel):
    paid_inventory_points: int = 0
    bonus_inventory_points: int = 0
    frozen_inventory_points: int = 0


class AdminAdjustInventoryRequest(StrictInventoryModel):
    agent_user_id: int
    direction: Literal["increase", "decrease"]
    paid_points: int = Field(0, ge=0, le=10_000_000_000)
    bonus_points: int = Field(0, ge=0, le=10_000_000_000)
    # 🔴 增加方向必填:`inventory_minting_guard` 护栏 3「无订单号的孤儿铸造一律拒绝」。
    #    这条不是本工单新加的门槛,是 2026-07-29 按需铸造工单立的。
    related_order_id: Optional[str] = Field(None, max_length=128)
    reason: str = Field(..., min_length=2, max_length=500)


class AdminAllocateInventoryRequest(StrictInventoryModel):
    agent_user_id: int
    customer_user_id: int
    paid_points: int = Field(0, ge=0, le=10_000_000_000)
    bonus_points: int = Field(0, ge=0, le=10_000_000_000)
    # 客户尚无商业服务归属时,本次划拨会**同时建立归属**(走 change_commercial_binding)。
    # 必须显式带上 CAS 版本 —— 建归属是治理动作,不接受"顺手带过去"。
    binding_expected_version: Optional[int] = Field(None, ge=1)
    reason: str = Field(..., min_length=2, max_length=500)


class AdminInventoryMutationResponse(StrictInventoryModel):
    success: bool
    action: str
    action_id: int
    agent_user_id: int
    customer_user_id: Optional[int] = None
    request_id: str
    before: InventoryWalletSnapshot
    after: InventoryWalletSnapshot
    binding: Optional[Dict[str, Any]] = None
    customer_after: Optional[Dict[str, Any]] = None


class AgentSelfUseRequest(StrictInventoryModel):
    paid_points: int = Field(0, ge=0, le=10_000_000_000)
    bonus_points: int = Field(0, ge=0, le=10_000_000_000)
    reason: str = Field(..., min_length=2, max_length=500)


class AgentSelfUseResponse(StrictInventoryModel):
    success: bool
    action: str
    action_id: int
    agent_user_id: int
    request_id: str
    inventory_before: InventoryWalletSnapshot
    inventory_after: InventoryWalletSnapshot
    wallet_after: Dict[str, int]
    converted_paid: int
    converted_bonus: int


class LotDriftAgentRow(StrictInventoryModel):
    agent_user_id: int
    wallet_side: int
    lot_side: int
    drift_points: int


class LotDriftResponse(StrictInventoryModel):
    success: bool
    drift_agent_count: int
    drift_total_points: int
    drift_abs_points: int
    agents: List[LotDriftAgentRow]
