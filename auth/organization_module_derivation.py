"""组织角色能力 → 平台权限模块 推导(单1 · WP6 集成缺口)。

## 事故背景

组织席位体系(`organization_*` 表)自成一套 capability 授权,但**从未接进平台的
路由权限层**。Owner 生产实测:操作员 user 151 拿到 13 项 org capability,而
`user_roles` 0 行 → `permissions == []` → `agent_level == 0`,于是:

- 前端每条 `requiredModule` 路由被 `hasModule()` 拒(`ProtectedRoute.tsx`);
- 后端每个受模块保护的 API 被 `auth/middleware.py` 拒;
- 4 条 `requiresAgent` 路由被拒。

员工被授权了、却什么都打不开。

## 本模块的定位

把"组织角色能力"**推导**成平台的 `module:level` 权限串。**不写 `user_roles` 表**
(推导制:授权真相只有一份 = 组织能力表;不产生第二份需要同步的副本)。

## 安全设计(逐条对应攻击面)

1. **admin 面永不入表** —— `users`/`roles`/`audit`/`settings`/`ai_agents` 在
   `FORBIDDEN_MODULES` 里,导入期断言映射表不含它们。组织角色再怎么配也拿不到
   后台管理面。
2. **只发 `read`/`write`,绝不发 `delete`** —— `auth/module_mapping.get_required_level`
   对 DELETE 要 `delete` 级,我们一律不发 → 员工走不了模块级删除路径。
3. **最小授权** —— 只映射到"该能力真正需要的那个模块",不做 `module:*` 泛授权;
   一个能力对应哪几个 `module:level` 逐条写死并附理由。
4. **deny > allow** —— 本模块只接收 `resolve_identity()` 已经算完的 **allow 集合**
   (`db/organization_db.py` 里成员级 override 覆盖角色级授权),deny 在上游就已生效。
5. **owner-only 能力天然进不来** —— `validate_capabilities()` 禁止把
   `OWNER_ONLY_CAPABILITIES`(含 `platform_admin.use`/`billing.manage`)配到角色上;
   本表也不给它们任何映射(见 `_assert_map_is_safe`)。
6. **不伪造 agent_level** —— 服务商身份是**资金语义**(提现/佣金/进货价系数),
   员工不是服务商。本模块一个字都不碰 `agent_level`。"员工能否进服务商工作台"
   由 `operating_for_agent`(= principal 是不是服务商)单独表达,见 `operator_context_for`。

## 版本化

`MODULE_DERIVATION_VERSION` 随映射表内容变化而变。改表必须同步改版本号——
它会进 `/api/auth/me` 的 `operator_context`,便于线上排查"这个员工是按哪版映射
算出来的"。
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, Optional, Tuple

# 改映射表 = 必须改这个版本号(会出现在 /api/auth/me 的 operator_context 里)
MODULE_DERIVATION_VERSION = "org-capability-module-map-v1"

# 这些模块**永远**不能由组织能力推导出来。
# - users/roles/audit:平台后台管理面(前端 ProtectedRoute 另有 is_admin 硬闸,
#   这里是第二道:让它连 permission 串都拿不到)
# - settings/ai_agents:平台级配置与 Agent 编排,不属于服务商员工的作业面
# - social:社媒板块由另一板块治理,组织能力不覆盖(板块边界铁律)
FORBIDDEN_MODULES: FrozenSet[str] = frozenset(
    {"users", "roles", "audit", "settings", "ai_agents", "social"}
)

# 组织能力 → 平台权限串。逐条列出理由,便于审计"为什么这个员工能进这个页面"。
#
# 模块 id 口径以**后端**为准(`auth/module_mapping.ROUTE_PREFIX_MAP`):
#   diagnosis / writing / monitoring / quote / reports / insights
# 另有 brands / history 只在前端 `requiredModule` 用(后端不校验),一并发放以打开页面。
# 发布(publish.*)在后端归 `writing` 模块(`/api/meijiehezi/` → writing)。
CAPABILITY_MODULE_GRANTS: Dict[str, Tuple[str, ...]] = {
    # —— 客户/品牌 ——
    "clients.read_assigned": ("brands:read",),
    "clients.profile_edit": ("brands:read", "brands:write"),
    # —— 诊断 ——
    "diagnosis.read_own": ("diagnosis:read", "history:read"),
    "diagnosis.run": ("diagnosis:read", "diagnosis:write"),
    "diagnosis.export": ("diagnosis:read", "reports:read"),
    # —— 报价 ——
    "quote.read_own": ("quote:read",),
    "quote.create": ("quote:read", "quote:write"),
    "quote.submit_for_approval": ("quote:read", "quote:write"),
    "quote.send_external": ("quote:read", "quote:write"),
    # —— 写作 ——
    "writing.read_own": ("writing:read",),
    "writing.generate": ("writing:read", "writing:write"),
    "writing.review": ("writing:read", "writing:write"),
    "writing.share_internal": ("writing:read",),
    # —— 发布(后端归 writing 模块)——
    "publish.plan": ("writing:read",),
    "publish.submit_for_approval": ("writing:read", "writing:write"),
    "publish.execute": ("writing:read", "writing:write"),
    # —— 监测 ——
    "monitoring.read_assigned": ("monitoring:read",),
    "monitoring.run": ("monitoring:read", "monitoring:write"),
    "monitoring.configure": ("monitoring:read", "monitoring:write"),
    "monitoring.retry": ("monitoring:read", "monitoring:write"),
    # —— 报告 ——
    "reports.read_own": ("reports:read",),
    "reports.generate": ("reports:read", "reports:write"),
    "reports.export": ("reports:read",),
    "reports.share_external": ("reports:read", "reports:write"),
    # —— 以下能力**刻意不映射任何平台模块** ——
    # materials.* / approvals.review / team.output_* 走的是 /api/organization/*
    # 与 /api/marketing/*,这些路由在 module_mapping 里是 None(端点内自有归属校验),
    # 不需要模块权限。给它们发模块串只会白白放大可见面。
    "materials.read_assigned": (),
    "materials.write": (),
    "approvals.review": (),
    "team.output_read": (),
    "team.output_handoff": (),
}


def _assert_map_is_safe() -> None:
    """导入期自检:映射表越权面为零。任一条违反直接炸,不留运行期惊喜。"""
    from services.organization_contract import (
        DELEGABLE_CAPABILITIES,
        OWNER_ONLY_CAPABILITIES,
    )

    keys = set(CAPABILITY_MODULE_GRANTS)

    unknown = keys - set(DELEGABLE_CAPABILITIES)
    if unknown:
        raise RuntimeError(f"[org-module-map] 映射了未知能力: {sorted(unknown)}")

    owner_only = keys & set(OWNER_ONLY_CAPABILITIES)
    if owner_only:
        raise RuntimeError(f"[org-module-map] owner-only 能力不得出现在映射表: {sorted(owner_only)}")

    # 覆盖完整性:新增可委派能力时必须显式表态(映射到模块,或显式空元组),
    # 防止"加了能力却忘了决定它给不给模块"。
    missing = set(DELEGABLE_CAPABILITIES) - keys
    if missing:
        raise RuntimeError(
            f"[org-module-map] 新增可委派能力未在映射表表态(要么给模块,要么显式空): {sorted(missing)}"
        )

    for capability, grants in CAPABILITY_MODULE_GRANTS.items():
        for grant in grants:
            if grant.count(":") != 1:
                raise RuntimeError(f"[org-module-map] 权限串格式非法: {capability} -> {grant}")
            module, level = grant.split(":", 1)
            if module in FORBIDDEN_MODULES:
                raise RuntimeError(f"[org-module-map] 禁止模块被映射: {capability} -> {grant}")
            if level not in ("read", "write"):
                # delete 级绝不发放(见模块 docstring 安全设计 §2)
                raise RuntimeError(f"[org-module-map] 只允许 read/write 级: {capability} -> {grant}")


_assert_map_is_safe()


def derive_module_permissions(capabilities: Iterable[str]) -> FrozenSet[str]:
    """把组织能力(**已经是 allow 集合**)推导成 `module:level` 权限串集合。

    未知能力一律忽略(不是报错):能力表可能先于本映射表上线,那时应当**少给**
    而不是崩掉登录。
    """
    derived: set[str] = set()
    for capability in capabilities or ():
        for grant in CAPABILITY_MODULE_GRANTS.get(str(capability), ()):  # 未知能力 → ()
            derived.add(grant)
    # 兜底:即便映射表被改错,禁止模块也绝不出门
    return frozenset(g for g in derived if g.split(":", 1)[0] not in FORBIDDEN_MODULES)


def derive_for_user(user_id: int, *, cursor=None) -> FrozenSet[str]:
    """按 user_id 解析组织身份并推导权限串。任何异常 → 空集(fail-closed:少给不多给)。

    只对**成员席位**(operator)推导;组织 owner 自己就是平台用户,权限走原有
    `user_roles` 体系,不需要也不应该被这里放大。
    """
    try:
        from db.organization_db import resolve_identity

        identity = resolve_identity(int(user_id), cursor=cursor)
        if identity is None or not identity.is_member:
            return frozenset()
        if identity.membership_status != "active" or identity.organization_status != "active":
            # 停用/离职中的席位不推导(fail-closed)
            return frozenset()
        return derive_module_permissions(identity.capabilities)
    except Exception:
        # 登录/鉴权主链上绝不能因为组织体系异常而崩;少给权限是安全方向。
        return frozenset()


def operator_context_for(user_id: int, *, cursor=None) -> Optional[dict]:
    """给 `/api/auth/me` 用的操作员上下文。非操作员返回 None。

    **只暴露该员工自己已经知道的事**(自己属于哪个组织、什么角色、代谁作业),
    不含其他成员、不含组织内部治理数据。
    """
    try:
        from db.organization_db import resolve_identity

        identity = resolve_identity(int(user_id), cursor=cursor)
        if identity is None or not identity.is_member:
            return None

        principal_agent_level = 0
        try:
            from db.wallet_db import get_wallet_balance

            wallet = get_wallet_balance(int(identity.principal_user_id)) or {}
            principal_agent_level = int(wallet.get("agent_level", 0) or 0)
        except Exception:
            principal_agent_level = 0

        return {
            "organization_id": identity.organization_id,
            "principal_user_id": identity.principal_user_id,
            "role_id": identity.role_id,
            "membership_status": identity.membership_status,
            "organization_status": identity.organization_status,
            "capabilities": sorted(identity.capabilities),
            # 员工自己**不是**服务商(agent_level 永远是他自己的真值 0);
            # 这个标记只说明"他代作业的商业主体是服务商",用于放行服务商作业面。
            "operating_for_agent": principal_agent_level >= 1,
            "derivation_version": MODULE_DERIVATION_VERSION,
        }
    except Exception:
        return None
