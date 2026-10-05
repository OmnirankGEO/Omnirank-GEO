"""商业主体(principal)身份解析 —— 员工代作业时的等级/额度判定单一入口(F-1)。

## 事故形态

组织员工(操作员席位)的 `user_wallets.agent_level` 恒为 0:**他不是服务商**,
服务商是他所在组织的 owner。而全仓大量闸门写的是"取当前登录用户的 agent_level,
< 1 就拦",于是服务商花钱雇的员工:

- 建第 2 个客户 → 402 UPGRADE_REQUIRED
- 进服务商工作台 → 403
- 建第 2 个档案 → 402

授权了却什么都干不了。

## 判定原则

**能力与额度属于商业主体,操作权属于人。** 员工代 owner 作业时,额度/等级看
owner;但员工自己的 `agent_level` 一个字都不改 —— 那是资金身份语义(提现、佣金、
进货价系数),不能因为"他在替谁干活"而改写。

## 边界(绝不适用)

以下场景**必须**继续看操作者本人的身份,不得调用本模块:

- **提现 / 结算**(`api/withdrawal_api.py`)—— 钱出账
- **佣金 / 分销**(`services/service_fee_*`、`api/referral_api.py`)
- **进货价与毛利系数**(`services/quota_pricing_preferences` 等定价链)
- **admin 校验面**(`api/admin_api.py` 等对目标用户的 `agent_level` 断言)

这些在组织能力模型里对应 `OWNER_ONLY_CAPABILITIES`(`withdrawals.manage` /
`settlements.manage` / `pricing.cost_and_margin_view` / `referral.manage` /
`platform_admin.use`),角色根本配不上去 —— 代作业不构成放行理由。

## 明确**在**范围内:对外品牌(白标)

白标是**商业主体的对外身份**,不是操作者的。团队长(组织 owner)设置的对外品牌
必须覆盖名下全部子账号 —— 员工代做的报价、报告、海报、代发署名,客户看到的
都应当是团队长的品牌,而不是员工的空设置(会落回平台默认 = 露馅)。

因此白标的**读取**按 principal(`resolve_branding_principal_user_id`),
但白标的**编辑**仍是 owner 专属(`whitelabel.manage` 在 OWNER_ONLY_CAPABILITIES):
员工"看得到、用得上、改不了"。
"""

from __future__ import annotations

from typing import Optional, Tuple


def resolve_principal_user_id(request, *, fallback_user_id: int) -> Tuple[int, bool]:
    """返回 `(principal_user_id, is_organization_member)`。

    非组织员工(含组织 owner 本人)一律返回 `fallback_user_id` —— 行为与改动前完全一致。
    """
    identity = getattr(getattr(request, "state", None), "organization_identity", None)
    if identity is not None and getattr(identity, "is_member", False):
        try:
            return int(identity.principal_user_id), True
        except (TypeError, ValueError):
            return int(fallback_user_id), False
    return int(fallback_user_id), False


def effective_agent_level(request, *, fallback_user_id: int) -> int:
    """员工代作业时取**商业主体**的服务商等级,否则取本人的。

    任何异常一律退化为 0(fail-closed:宁可拦住也不错放)。
    """
    principal_user_id, _ = resolve_principal_user_id(request, fallback_user_id=fallback_user_id)
    try:
        from db.wallet_db import get_wallet_balance

        wallet = get_wallet_balance(principal_user_id) or {}
        return int(wallet.get("agent_level", 0) or 0)
    except Exception:
        return 0


def operating_for_agent(request, *, fallback_user_id: int) -> bool:
    """当前请求是否是"员工代服务商作业"。

    注意与 `effective_agent_level` 的区别:本函数**只在确实是员工席位时**才可能为真,
    owner 本人是服务商时返回 False(他不是"代"谁作业)。用于需要区分二者的文案/入口。
    """
    principal_user_id, is_member = resolve_principal_user_id(request, fallback_user_id=fallback_user_id)
    if not is_member:
        return False
    try:
        from db.wallet_db import get_wallet_balance

        wallet = get_wallet_balance(principal_user_id) or {}
        return int(wallet.get("agent_level", 0) or 0) >= 1
    except Exception:
        return False


def principal_display_hint(request, *, fallback_user_id: int) -> Optional[str]:
    """给 §13 告警用的一句人话:说明这次判定是按谁的身份做的。取不到返回 None。"""
    _, is_member = resolve_principal_user_id(request, fallback_user_id=fallback_user_id)
    if not is_member:
        return None
    return "当前以员工身份代所属服务商作业,额度与等级按所属服务商计算。"


def resolve_branding_principal_user_id(request, *, fallback_user_id: int) -> int:
    """对外品牌(白标)应当归到哪个 user_id 名下。

    员工席位 → 其所属组织 owner;其他一切情况 → 本人。
    这是"团队长的对外品牌覆盖全部子账号"的单一判定点,凡是要取白标的地方都走它,
    不要再各处自己写 `getattr(request.state, "organization_identity", None)`。
    """
    principal_user_id, _ = resolve_principal_user_id(request, fallback_user_id=fallback_user_id)
    return principal_user_id


def is_organization_seat_member(user_id) -> bool:
    """该 user_id 是不是**组织员工席位**(而非老板本人 / 非组织用户)。

    用途:把「看进货成本与毛利」这类 owner-only 视图挡在员工之外
    (`pricing.cost_and_margin_view` 属 OWNER_ONLY,角色配不上去)。

    失败方向是**反的**:这里解析失败返回 True(按员工对待 → 走脱敏视图)。
    因为返回 False 会**放行**看成本 —— 宁可让老板本人偶尔看到脱敏视图,
    也不能让员工看到成本毛利。
    """
    if not user_id:
        return False
    try:
        from db.organization_db import resolve_identity

        identity = resolve_identity(int(user_id))
        if identity is None:
            return False          # 不属于任何组织 = 普通服务商本人,正常放行
        return bool(identity.is_member)
    except Exception:
        return True               # 解析不了就按最保守处理:脱敏
