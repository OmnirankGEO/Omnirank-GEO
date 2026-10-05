"""正式诊断 preview / confirm 的资金投影校验(规格 §15.3 / §18.3 DIA-FIN-13、14、15)。

这一层是纯函数,不碰库、不碰钱 —— 它只回答一件事:
**「这份投影自洽吗?」** 不自洽就不签 preview / 不返回 confirm。

为什么要单独成层
----------------
§15.3 的资金约束是一张**笛卡尔积矩阵**,不是几条 if:
``fundingPolicy × principalKind × sponsorPolicyRef × approvalState × billingModeProjection × fundingState``。
散在端点里写 if 必然漏格,而漏掉的那一格不会让任何判据变红 ——
所以这里把矩阵写成**数据**,判据拿数据当分母逐格核对(本仓 2026-08-14
「语义判别必须笛卡尔积矩阵验」)。

🔴 三条最贵的规则(逐条对应真实损失方向):

1. **组织的 ``exempt`` 不是免费**。现役 ``billing_mode`` 只有 ``paid|exempt`` 两值
   (``chk_diag_runs_billing_mode`` 实测),组织预算只能借 ``exempt`` 这个字符串走
   兼容 adapter —— 但它必须带**真实** organization reservation + approval ref,
   且对外 ``fundingState`` 仍是 ``frozen``。把它当免费 = 组织白跑不扣预算。

   🔴 [A-1 · 2026-08-25] **平台承担两格已经不再借 exempt 了**(改投 ``paid``)。
      理由与组织格正好相反:组织的钱不走 ``point_freezes``(走 charge link),
      所以 legacy 状态机不该对它调 billing;而平台腿的钱**就在 point_freezes 里**,
      不走物理结算 = 交付完成后成本归零。Codex 终审 P0-1。
      ``billing_mode`` 的语义在这里被钉死为:**「这一单有没有必须收敛的物理冻结」**,
      而不是「谁付钱」。谁付钱由 ``diagnosis_runs.payer_user_id`` 单独记。
2. **admin / sponsor 平台账不许下发个人钱包充值**。付款方是平台成本中心,
   给用户一个"去充值"的按钮等于让他为平台的账掏钱。
3. **算术必须守恒**:``exactTotalPoints == basePoints + extraPoints`` 且三者非负整数。
   逐项价格对不代表整包守恒(§3.4 原文)。
"""

from __future__ import annotations

from typing import Literal, NamedTuple

#: 本矩阵版本。改任一格必须升版。
FUNDING_MATRIX_VERSION = "defensive-geo-diagnosis-funding-matrix-v1"

FundingPolicy = Literal[
    "personal_wallet", "organization_budget",
    "admin_platform_ledger", "sponsor_platform_ledger",
]
PrincipalKind = Literal["personal", "organization", "platform_cost_center"]
ApprovalState = Literal["not_required", "required", "approved", "rejected"]
BillingModeProjection = Literal["paid", "exempt"]


class FundingCell(NamedTuple):
    """一条 fundingPolicy 的全部合法投影。四条 policy 各一格,没有第五格。"""

    principal_kind: PrincipalKind
    #: ``sponsorPolicyRef`` 必须为空 / 必须非空。
    sponsor_ref_required: bool
    #: confirm 后投影到现役 ``diagnosis_runs.billing_mode`` 的值。
    billing_mode_projection: BillingModeProjection
    #: confirm 后对外公开的 fundingState。
    confirm_funding_state: Literal["frozen", "exempt_recorded"]
    #: confirm 后 fundingHandle 的 kind。
    handle_kind: str
    #: 该 handle 是否必须额外带 approvalRef。
    handle_requires_approval_ref: bool
    #: 资金不足时允许下发的动作 —— 谁付钱谁被引导,绝不串格。
    insufficient_actions: tuple[str, ...]


#: 🔴 §15.3 的四格矩阵,写成数据。
#: **不许有第五格,也不许有兜底 else** —— 兜底会让「漏一格」和「写对了」长得一样。
_MATRIX: dict[FundingPolicy, FundingCell] = {
    "personal_wallet": FundingCell(
        principal_kind="personal",
        sponsor_ref_required=False,
        billing_mode_projection="paid",
        confirm_funding_state="frozen",
        handle_kind="wallet_freeze",
        handle_requires_approval_ref=False,
        # 个人钱包不足:只能充值或缩题单。给"找 owner 审批"是串格(个人没有 owner)。
        insufficient_actions=("top_up", "reduce_plan"),
    ),
    "organization_budget": FundingCell(
        principal_kind="organization",
        sponsor_ref_required=False,
        # 借现役 exempt 走兼容 adapter —— 但下面三项保证它不等于免费。
        billing_mode_projection="exempt",
        confirm_funding_state="frozen",
        handle_kind="organization_reservation",
        handle_requires_approval_ref=True,
        # 组织预算不足:追加审批或缩题单。绝不给个人钱包充值 —— 那是让成员替组织掏钱。
        insufficient_actions=("request_budget_approval", "reduce_plan"),
    ),
    "admin_platform_ledger": FundingCell(
        principal_kind="platform_cost_center",
        sponsor_ref_required=False,
        # 🔴 [A-1 · Codex P0-1 · 2026-08-25] 这一格从 ``exempt`` 改成 ``paid``。
        #    平台承担腿在 confirm 里走的是**真** ``freeze_points(platform_uid, …)``,
        #    ``point_freezes`` 里真有一行。而 ``exempt`` 是 ``commit_run/release_run``
        #    的**短路口令**:见 exempt 就直接落 completed_exempt/failed_exempt,
        #    零 billing 调用 —— 于是平台交付完成后成本回退为零,
        #    冻结再被通用 freeze_sweeper 当僵尸释放掉。
        #    ``paid`` 才是「有真实冻结、必须走到物理 commit/release」的那个模式,
        #    平台腿正是这个形状(付款方由 run 行的 payer_user_id 记,不靠 billing_mode 区分)。
        #    ⚠️ 刻意**不新增第三个枚举值**:``chk_diag_runs_billing_mode`` 是这一类的
        #       边界,加值等于点燃十几处「假设只有 paid|exempt」的读取
        #       (两条 sweeper 查询在内),漏掉的那一处不会让任何判据变红,
        #       只会让平台的钱静静挂着。详见迁移 050 头部与交付文 §A-1。
        billing_mode_projection="paid",
        # 对外仍是 exempt_recorded:**用户**的钱包确实一分没动。
        # billingModeProjection 说的是「这一单要不要走物理结算」,
        # fundingState 说的是「你要不要付钱」—— 两件事,不许互相翻译。
        confirm_funding_state="exempt_recorded",
        handle_kind="platform_cost_ledger",
        handle_requires_approval_ref=False,
        # 平台账不存在"余额不足",只可能是 policy 不可用 → 转支持。
        insufficient_actions=(),
    ),
    "sponsor_platform_ledger": FundingCell(
        principal_kind="platform_cost_center",
        sponsor_ref_required=True,      # 唯一必须带已签 sponsor policy ref 的一格
        billing_mode_projection="paid",     # 同 admin 格,理由见上
        confirm_funding_state="exempt_recorded",
        handle_kind="platform_cost_ledger",
        handle_requires_approval_ref=False,
        insufficient_actions=(),
    ),
}


def funding_policies() -> tuple[FundingPolicy, ...]:
    """合法 policy 全集。判据拿它当分母,不手抄。"""
    return tuple(_MATRIX)


def cell(policy: str) -> FundingCell:
    try:
        return _MATRIX[policy]  # type: ignore[index]
    except KeyError:
        raise ValueError(
            f"未知 fundingPolicy {policy!r};合法值 = {sorted(_MATRIX)}。"
            "不设兜底:兜底会让漏一格和写对了长得一样。"
        ) from None


class ProjectionError(ValueError):
    """投影不自洽。**不签 preview / 不返回 confirm**,而不是修一修凑合发出去。"""


def validate_preview_funding(
    *,
    funding_policy: str,
    principal_kind: str,
    sponsor_policy_ref: str | None,
    approval_requirement: str,
    base_points: object,
    extra_points: object,
    exact_total_points: object,
) -> None:
    """preview 侧四格 + 算术守恒(DIA-FIN-14)。任一项不合法 → 抛,零 preview。"""
    spec = cell(funding_policy)

    if principal_kind != spec.principal_kind:
        raise ProjectionError(
            f"{funding_policy} 的 principalKind 必须是 {spec.principal_kind},实得 {principal_kind!r} —— 错 payer"
        )

    has_ref = bool(sponsor_policy_ref and str(sponsor_policy_ref).strip())
    if spec.sponsor_ref_required and not has_ref:
        raise ProjectionError(
            f"{funding_policy} 必须带已签 sponsorPolicyRef;缺 ref 时它与 admin_platform_ledger 无法区分"
        )
    if not spec.sponsor_ref_required and has_ref:
        raise ProjectionError(
            f"{funding_policy} 不得带 sponsorPolicyRef(实得 {sponsor_policy_ref!r})—— 冒充 sponsor 档"
        )

    if approval_requirement not in ("not_required", "required"):
        raise ProjectionError(f"approvalRequirement 只能是 not_required|required,实得 {approval_requirement!r}")

    # 算术:三者都必须是**真整数**且非负。bool 是 int 的子类,必须显式挡掉 ——
    # True 会静默当成 1,让 "1+0=1" 通过而金额其实是个布尔。
    values = {"basePoints": base_points, "extraPoints": extra_points, "exactTotalPoints": exact_total_points}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProjectionError(f"{name} 必须是整数,实得 {type(value).__name__}({value!r})")
        if value < 0:
            raise ProjectionError(f"{name} 不得为负,实得 {value}")
    if exact_total_points != base_points + extra_points:  # type: ignore[operator]
        raise ProjectionError(
            f"算术不守恒:{base_points} + {extra_points} != {exact_total_points}。"
            "逐项价格正确不代表整包守恒"
        )


def validate_confirm_funding(
    *,
    funding_policy: str,
    principal_kind: str,
    billing_mode_projection: str,
    funding_state: str,
    handle_kind: str,
    handle_approval_ref: str | None,
    sponsor_policy_ref: str | None,
) -> None:
    """confirm 侧四格逐值(DIA-FIN-15)。

    §19 变异 148 点名的三个方向,逐个都在这里被挡:
      · 组织 exempt 被当成免费(丢 organization handle)
      · admin/sponsor 不写平台成本账仍返回成功
      · 输出错 fundingState
    """
    spec = cell(funding_policy)

    if principal_kind != spec.principal_kind:
        raise ProjectionError(f"{funding_policy} 的 principalKind 必须是 {spec.principal_kind}")

    if billing_mode_projection != spec.billing_mode_projection:
        raise ProjectionError(
            f"{funding_policy} 的 billingModeProjection 必须是 {spec.billing_mode_projection},"
            f"实得 {billing_mode_projection!r}"
        )
    if billing_mode_projection not in ("paid", "exempt"):
        raise ProjectionError(
            "billingModeProjection 只能投影成现役 paid|exempt —— "
            "不得把 runKind/fundingPolicy 写进该 enum"
        )

    if funding_state != spec.confirm_funding_state:
        raise ProjectionError(
            f"{funding_policy} 的 fundingState 必须是 {spec.confirm_funding_state},实得 {funding_state!r}。"
            "组织预算虽走 exempt adapter,对外仍必须是 frozen —— 报成 exempt_recorded 等于说这次不花钱"
        )

    if handle_kind != spec.handle_kind:
        raise ProjectionError(
            f"{funding_policy} 的 fundingHandle.kind 必须是 {spec.handle_kind},实得 {handle_kind!r}。"
            "平台账成功响应伪装成 wallet freeze 是 §19 变异 157 点名的形态"
        )

    has_approval = bool(handle_approval_ref and str(handle_approval_ref).strip())
    if spec.handle_requires_approval_ref and not has_approval:
        raise ProjectionError(
            f"{funding_policy} 的 handle 必须带真实 approvalRef —— "
            "没有 reservation/approval 凭据的 exempt 就是「白跑不扣预算」"
        )
    if not spec.handle_requires_approval_ref and has_approval:
        raise ProjectionError(f"{funding_policy} 的 handle 不该带 approvalRef")

    has_sponsor = bool(sponsor_policy_ref and str(sponsor_policy_ref).strip())
    if spec.sponsor_ref_required != has_sponsor:
        raise ProjectionError(
            f"{funding_policy} 的 sponsorPolicyRef 存在性错配"
            f"(需要={spec.sponsor_ref_required} 实得={has_sponsor})"
        )


def allowed_insufficient_actions(funding_policy: str) -> tuple[str, ...]:
    """资金不足时该 policy 允许下发的动作。

    🔴 平台账两格返回空元组:平台成本中心不存在「余额不足」。
       给它任何一个动作都是在替平台向用户要钱。
    """
    return cell(funding_policy).insufficient_actions


def validate_insufficient_action(funding_policy: str, action_kind: str) -> None:
    """必须不命中的那一半:动作串格当场拒。"""
    allowed = allowed_insufficient_actions(funding_policy)
    if action_kind not in allowed:
        raise ProjectionError(
            f"{funding_policy} 不得下发 {action_kind!r};允许 = {list(allowed) or '(无 · 平台账不存在余额不足)'}。"
            "个人/组织/平台三条钱腿的动作不许互串"
        )
