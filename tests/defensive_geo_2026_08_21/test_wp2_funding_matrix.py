"""DIA-FIN-14 / DIA-FIN-15:正式诊断资金四格矩阵(规格 §15.3 / §18.3)。

判据形态 = **笛卡尔积**,不是抽样。
本仓 2026-08-14 记过「语义判别必须笛卡尔积矩阵验」:
四条 policy × 三种 principalKind × 有/无 sponsor ref,一共 24 组合,
合法的只有 4 组 —— 逐组都跑,而不是挑几个例子。

分母来自 ``funding_policies()``,不手抄:新加一条 policy 而忘了配矩阵,
下面的 `test_matrix_covers_every_policy` 必红。
"""

from __future__ import annotations

import itertools

import pytest

from services.defensive_geo.funding_projection import (
    FUNDING_MATRIX_VERSION,
    ProjectionError,
    allowed_insufficient_actions,
    cell,
    funding_policies,
    validate_confirm_funding,
    validate_insufficient_action,
    validate_preview_funding,
)

POLICIES = funding_policies()
PRINCIPALS = ("personal", "organization", "platform_cost_center")


def test_matrix_version_is_pinned():
    assert FUNDING_MATRIX_VERSION == "defensive-geo-diagnosis-funding-matrix-v1"


def test_matrix_covers_exactly_the_four_spec_policies():
    """分母守卫:少一格 / 多一格都必红。"""
    assert set(POLICIES) == {
        "personal_wallet", "organization_budget",
        "admin_platform_ledger", "sponsor_platform_ledger",
    }, f"矩阵 policy 集合 = {sorted(POLICIES)}"


def test_unknown_policy_is_refused_not_defaulted():
    """必须不命中:没有兜底 else。兜底会让「漏一格」和「写对了」长得一样。"""
    with pytest.raises(ValueError):
        cell("some_new_policy")


# ══════════════════════ preview 侧:四格 × payer × sponsor ref ══════════════
def _preview(policy, principal, sponsor_ref, base=100, extra=20, total=120):
    validate_preview_funding(
        funding_policy=policy, principal_kind=principal,
        sponsor_policy_ref=sponsor_ref, approval_requirement="not_required",
        base_points=base, extra_points=extra, exact_total_points=total,
    )


@pytest.mark.parametrize("policy", POLICIES)
def test_each_policy_accepts_exactly_its_own_legal_cell(policy):
    """必须命中:每条 policy 的合法格必须通过。

    没有这条,下面「非法格必拒」可能只是因为校验器**恒拒** —— 那是零判别力。
    """
    spec = cell(policy)
    _preview(policy, spec.principal_kind, "sponsor-policy-ref-1" if spec.sponsor_ref_required else None)


@pytest.mark.parametrize(
    "policy,principal,sponsor",
    [
        (p, pk, s)
        for p, pk, s in itertools.product(POLICIES, PRINCIPALS, (None, "sponsor-ref"))
        # 排除该 policy 自己的那一格,剩下的**全部**必须被拒
        if not (pk == cell(p).principal_kind and bool(s) == cell(p).sponsor_ref_required)
    ],
)
def test_every_illegal_cell_is_refused(policy, principal, sponsor):
    """🔴 主锁:24 个组合里除 4 个合法格外,逐个必拒。

    这条覆盖了 §19 变异 134/148 点名的「错 payer、空/多 sponsor ref」。
    """
    with pytest.raises(ProjectionError):
        _preview(policy, principal, sponsor)


def test_admin_and_sponsor_cannot_impersonate_each_other():
    """两条平台账 policy 的唯一区别就是 sponsorPolicyRef —— 不许互相冒充。"""
    with pytest.raises(ProjectionError):
        _preview("admin_platform_ledger", "platform_cost_center", "sponsor-ref")
    with pytest.raises(ProjectionError):
        _preview("sponsor_platform_ledger", "platform_cost_center", None)


# ══════════════════════ 算术守恒 ══════════════════════════════════════════
@pytest.mark.parametrize(
    "base,extra,total",
    [
        (100, 20, 121),      # 差 1
        (100, 20, 119),
        (-1, 0, -1),         # 负数
        (0, -5, -5),
        (100, 20, 0),
    ],
)
def test_arithmetic_must_be_conserved(base, extra, total):
    with pytest.raises(ProjectionError):
        _preview("personal_wallet", "personal", None, base=base, extra=extra, total=total)


def test_zero_cost_is_legal_but_boolean_is_not():
    """反向对照:0 是合法金额(不能因为「防负数」把 0 也挡了)。"""
    _preview("personal_wallet", "personal", None, base=0, extra=0, total=0)


@pytest.mark.parametrize("bad", [True, False, 1.0, "100", None])
def test_non_integer_points_are_refused(bad):
    """必须不命中:``True`` 是 int 子类,``True + 0 == 1`` 会静默通过 —— 显式挡掉。"""
    with pytest.raises(ProjectionError):
        _preview("personal_wallet", "personal", None, base=bad, extra=0, total=bad)


# ══════════════════════ confirm 侧:DIA-FIN-15 四格逐值 ═════════════════════
_LEGAL_CONFIRM = {
    "personal_wallet": dict(
        principal_kind="personal", billing_mode_projection="paid", funding_state="frozen",
        handle_kind="wallet_freeze", handle_approval_ref=None, sponsor_policy_ref=None),
    "organization_budget": dict(
        principal_kind="organization", billing_mode_projection="exempt", funding_state="frozen",
        handle_kind="organization_reservation", handle_approval_ref="appr-1", sponsor_policy_ref=None),
    # [A-1 · Codex P0-1 · 2026-08-25] 平台两格的 billingModeProjection 从 exempt 改成 paid。
    #   exempt 是 commit_run/release_run 的短路口令(零 billing 调用),而平台腿在
    #   point_freezes 里真有一行冻结 —— 借 exempt 等于交付完成后平台成本归零。
    #   本表是矩阵的**期望值分母**,改矩阵必须同步改它;下面
    #   `test_billing_mode_projection_stays_within_live_enum` 与
    #   `test_billing_mode_cannot_be_widened_to_shadow_or_qa` 两把锁**一字未削**
    #   (paid 本来就在现役 enum 内,本次没有扩宽 chk_diag_runs_billing_mode)。
    "admin_platform_ledger": dict(
        principal_kind="platform_cost_center", billing_mode_projection="paid",
        funding_state="exempt_recorded", handle_kind="platform_cost_ledger",
        handle_approval_ref=None, sponsor_policy_ref=None),
    "sponsor_platform_ledger": dict(
        principal_kind="platform_cost_center", billing_mode_projection="paid",
        funding_state="exempt_recorded", handle_kind="platform_cost_ledger",
        handle_approval_ref=None, sponsor_policy_ref="sponsor-policy-1"),
}


def test_legal_confirm_table_covers_every_policy():
    """判据自己的期望表也要盖满分母,否则下面会静默少验几格。"""
    assert sorted(_LEGAL_CONFIRM) == sorted(POLICIES)


@pytest.mark.parametrize("policy", POLICIES)
def test_legal_confirm_projection_passes(policy):
    validate_confirm_funding(funding_policy=policy, **_LEGAL_CONFIRM[policy])


def test_organization_exempt_is_not_free():
    """🔴 最贵的一条:组织借 exempt 走 adapter,但绝不等于免费。

    §19 变异 148「组织 exempt 被当成免费、丢 organization handle」。
    """
    bad = dict(_LEGAL_CONFIRM["organization_budget"])
    # ① 丢掉 approval handle → 没有 reservation 凭据的 exempt 就是白跑
    bad_no_ref = {**bad, "handle_approval_ref": None}
    with pytest.raises(ProjectionError):
        validate_confirm_funding(funding_policy="organization_budget", **bad_no_ref)
    # ② 对外报成 exempt_recorded → 等于说这次不花钱
    bad_state = {**bad, "funding_state": "exempt_recorded"}
    with pytest.raises(ProjectionError):
        validate_confirm_funding(funding_policy="organization_budget", **bad_state)
    # ③ handle 伪装成个人钱包冻结
    bad_handle = {**bad, "handle_kind": "wallet_freeze"}
    with pytest.raises(ProjectionError):
        validate_confirm_funding(funding_policy="organization_budget", **bad_handle)


def test_platform_ledger_cannot_masquerade_as_wallet_freeze():
    """§19 变异 157:平台账成功响应伪装成 wallet freeze。"""
    for policy in ("admin_platform_ledger", "sponsor_platform_ledger"):
        bad = {**_LEGAL_CONFIRM[policy], "handle_kind": "wallet_freeze", "funding_state": "frozen"}
        with pytest.raises(ProjectionError):
            validate_confirm_funding(funding_policy=policy, **bad)


@pytest.mark.parametrize("policy", POLICIES)
def test_billing_mode_projection_stays_within_live_enum(policy):
    """现役 ``chk_diag_runs_billing_mode`` 只允许 paid|exempt(生产 schema 实测)。

    把 runKind/fundingPolicy 写进该 enum 会当场 IntegrityError。
    """
    assert cell(policy).billing_mode_projection in ("paid", "exempt")


@pytest.mark.parametrize("policy", POLICIES)
def test_billing_mode_cannot_be_widened_to_shadow_or_qa(policy):
    """§3.4 原文:不得把现役只允许 paid|exempt 的 billing_mode 擅自扩成 shadow/qa。"""
    for forbidden in ("shadow", "qa", "sponsor", "formal"):
        bad = {**_LEGAL_CONFIRM[policy], "billing_mode_projection": forbidden}
        with pytest.raises(ProjectionError):
            validate_confirm_funding(funding_policy=policy, **bad)


# ══════════════════════ 资金不足动作不许串格 ══════════════════════════════
def test_personal_gets_topup_organization_gets_approval():
    """必须命中:各自拿到自己那条路。"""
    assert allowed_insufficient_actions("personal_wallet") == ("top_up", "reduce_plan")
    assert allowed_insufficient_actions("organization_budget") == ("request_budget_approval", "reduce_plan")


def test_platform_ledger_has_no_insufficient_action_at_all():
    """平台成本中心不存在「余额不足」—— 给任何动作都是替平台向用户要钱。"""
    for policy in ("admin_platform_ledger", "sponsor_platform_ledger"):
        assert allowed_insufficient_actions(policy) == ()


@pytest.mark.parametrize(
    "policy,action",
    [
        ("personal_wallet", "request_budget_approval"),   # 个人没有 owner 可批
        ("organization_budget", "top_up"),                # 让成员替组织掏钱
        ("admin_platform_ledger", "top_up"),              # §19 变异 149 点名
        ("sponsor_platform_ledger", "top_up"),
        ("admin_platform_ledger", "reduce_plan"),
    ],
)
def test_cross_payer_actions_are_refused(policy, action):
    """🔴 §19 变异 149:「组织/平台下发个人 wallet top-up」必红。"""
    with pytest.raises(ProjectionError):
        validate_insufficient_action(policy, action)


@pytest.mark.parametrize(
    "policy,action",
    [("personal_wallet", "top_up"), ("personal_wallet", "reduce_plan"),
     ("organization_budget", "request_budget_approval"), ("organization_budget", "reduce_plan")],
)
def test_own_lane_actions_are_allowed(policy, action):
    """反向对照:合法动作必须放行,否则上面那组可能只是因为**恒拒**。"""
    validate_insufficient_action(policy, action)
