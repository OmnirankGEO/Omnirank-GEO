"""§7.3 组织审批门 —— **位置**比实现更重要(Codex P0-07)。

## 规格原话与实现的差距

规格:「approval gate 在**容量 / 冻结 / draft / order 之前**」。
实现:从幂等 claim 直接进资格 → 容量 → freeze → 建单,中间**没有这一道**。
仓内只有 `pending_approval` 这个 projector 常量和相关测试 —— 也就是说
「批次可以处于待审批」这件事在**投影层**是真的,在**执行层**从来没发生过。

## 为什么位置是本模块的全部意义

审批门放晚一格的后果不是"慢一点",是**语义反了**:

  · 放在冻结之后 ⇒ 提交时钱已经冻住,审批被拒还要再退一次 ——
    员工发起一次被拒的请求,owner 的余额被占用一段时间;
  · 放在建单之后 ⇒ 供应商侧可能已经收到单,审批变成事后追认;
  · 放在容量预占之后 ⇒ 一个不会执行的请求把账号当天的额度用掉了。

所以这道闸只有在**所有副作用之前**才叫闸,否则它只是一条日志。

## 当前可达性(必须写在这里,不能只写在交付单里)

审批是**组织**概念:`approval_required` 对 `identity.is_owner` 直接返回
False。而图文 lane 的组织计费尚未接通(见
`contract_funding.organization_charge_available` 的申报),组织成员在更早的
`resolve_settlement_authority` 那一步就拿到 §9 handoff 403。

⇒ **本闸目前对所有能走到这里的请求都返回"无需审批"**。这不是空实现:
   它有真实生产调用者、真实执行、真实返回值,并且在组织计费接通的那一天
   **不需要再动位置** —— 位置是这次返工要固定下来的东西。
   判据打的也是位置(它必须在 freeze / 建单之前被调用过),不是返回值。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

#: 图文制作与投放对应的审批动作类型。取现役枚举,不新造。
ACTION_PUBLISH = "publish"
ACTION_SPEND = "spend"


class ApprovalPending(RuntimeError):
    """需要审批且尚未获批。整批零副作用,批次状态 `pending_approval`。"""

    def __init__(self, message: str, *, approval_request_id: Optional[int] = None,
                 policy_version: Optional[int] = None):
        super().__init__(message)
        self.approval_request_id = approval_request_id
        self.policy_version = policy_version


def require_approval(cur, *, identity: Mapping[str, Any], action_type: str,
                     estimated_points: int, feature_code: str,
                     payload_hash: str) -> dict[str, Any]:
    """审批门。返回 `{required, approval_request_id, policy_version, payload_hash}`。

    🔴 **必须在容量预占 / 冻结 / 建 draft / 建 order 之前调用。**
       调用点位置由 `test_chain5_approval_gate.py` 用 AST
       断言(闸的调用语句必须早于那四类语句),而不是靠 code review 记得。

    🔴 非组织身份直接返回 `required=False`:审批是组织内部的授权关系,
       个人服务商对自己的钱不需要向谁申请。**这不是"跳过闸"**,
       是这条闸对该身份本来就无内容 —— 两者的区别在于:前者是漏洞,
       后者是定义。
    """
    organization_id = identity.get("organization_id")
    if not organization_id:
        return {"required": False, "approval_request_id": None,
                "policy_version": None, "payload_hash": str(payload_hash),
                "reason": "solo_owner_no_approval_scope"}

    org_identity = identity.get("_organization_identity")
    if org_identity is None:
        # 有 organization_id 但拿不到完整身份上下文 ⇒ **fail-closed**。
        # 反方向(当成不需要审批)会让一个上下文不完整的请求绕过整道闸。
        raise ApprovalPending("组织身份上下文不完整,无法判定审批要求")

    from services.organization_approvals import approval_required_with_default

    required, policy = approval_required_with_default(
        cur, org_identity, action_type=action_type,
        # 老板 2026-07-25 口径:发布类日常动作**默认员工直接做**,
        # 组织要审批就打开开关。沿用该默认,不在本 lane 另立一套。
        default_required=False,
        estimated_points=int(estimated_points), feature_code=feature_code)
    if not required:
        return {"required": False, "approval_request_id": None,
                "policy_version": policy.get("policy_version"),
                "payload_hash": str(payload_hash), "reason": "policy_not_required"}

    raise ApprovalPending(
        "这次操作需要团队负责人审批后才能执行",
        policy_version=policy.get("policy_version"))
