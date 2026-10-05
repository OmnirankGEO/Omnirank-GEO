"""两档确认制映射 + 社媒域负向枚举锁(规格 §8.1/§8.2 · 工单 §3.2)。

工单要的两条反向变异都在这里:

* 「给纯算力操作套确认门」→ 必须红;
* 「把 external 操作改成静默扣」→ 必须红。

社媒锁部分,判据范围**等于轴的作用域**:员工席位能看见/走得通的是两处
(能力注册表 + 员工路由白名单),所以两处都枚举。只锁一处 = 锁比轴小,
这个错本仓记过三次(``feedback_enumeration_lock_scope_equals_axis_scope``)。
"""

from __future__ import annotations

import pytest

from services import gap_operation_map as omap
from services.organization_route_contract import MEMBER_GEO_ROUTE_POLICIES
from services.xiaobang_command_contract import (
    ALL_PHASES,
    COMPUTE_ONLY_MIN_BIND_FIELDS,
    CONFIRM_REQUIRED_USER_CLICK,
    CONFIRM_SILENT_WITH_NOTICE,
    CommandContract,
    ConfirmationPolicy,
    ContractRegistrationError,
    EXTERNAL_BIND_FIELDS,
    SIDE_EFFECT_COMPUTE_ONLY,
    SIDE_EFFECT_EXTERNAL,
    SocialDomainLockError,
    assert_no_social_domain,
    compute_only_confirmation_policy,
    external_confirmation_policy,
    social_domain_hits,
    standard_idempotency,
)

_ADAPTERS = {"query": "x_domain_query", "prepare": "x_domain_prepare"}


def _contract(side_effect, policy, **kw):
    return CommandContract(
        required_capability=kw.pop("capability", "publish.execute"),
        resource_kind=kw.pop("resource_kind", "geo_image_post"),
        side_effect=side_effect,
        supported_phases=ALL_PHASES,
        adapters=dict(_ADAPTERS),
        confirmation_policy=policy,
        idempotency=kw.pop("idempotency", standard_idempotency()),
        **kw,
    )


# ── 两档映射 ──────────────────────────────────────────────────────────────
def test_external_maps_to_required_user_click():
    contract = _contract(SIDE_EFFECT_EXTERNAL, external_confirmation_policy())
    assert contract.confirmation_policy.mode == CONFIRM_REQUIRED_USER_CLICK
    assert contract.h0_gate_id == "XB-H0-EXTERNAL-CONFIRM"


def test_compute_only_maps_to_silent_with_notice():
    contract = _contract(
        SIDE_EFFECT_COMPUTE_ONLY, compute_only_confirmation_policy(),
        capability="writing.generate", resource_kind="article",
    )
    assert contract.confirmation_policy.mode == CONFIRM_SILENT_WITH_NOTICE
    assert contract.h0_gate_id == "XB-H0-SILENT-NOTICE"


def test_mutation_confirmation_gate_on_a_compute_only_operation_is_red():
    """🔴 工单 §3.2 指名的变异:给纯算力操作套确认门。

    这与已签发 DP-A6.1(静默扣 + 事后通知)正面冲突,也是 DP-A4 明令禁止的
    「给正常流程加确认弹窗」。注册期就必须拒绝,不能等到线上有人点。
    """
    with pytest.raises(ContractRegistrationError) as excinfo:
        _contract(
            SIDE_EFFECT_COMPUTE_ONLY, external_confirmation_policy(),
            capability="writing.generate", resource_kind="article",
        )
    assert "silent_with_notice" in str(excinfo.value)


def test_mutation_silent_charge_on_an_external_operation_is_red():
    """反方向同样要红:对外不可逆动作不许走静默扣。"""
    with pytest.raises(ContractRegistrationError) as excinfo:
        _contract(SIDE_EFFECT_EXTERNAL, compute_only_confirmation_policy())
    assert "required_user_click" in str(excinfo.value)


def test_external_bind_must_match_the_eleven_field_review_checklist():
    """§8.1 的 bind 与 §10.3 复核清单逐项对齐;少一项 = 少一条漂移判据。"""
    assert len(EXTERNAL_BIND_FIELDS) == 11
    short = ConfirmationPolicy(
        CONFIRM_REQUIRED_USER_CLICK,
        tuple(f for f in EXTERNAL_BIND_FIELDS if f != "compute_quote_hash"),
    )
    with pytest.raises(ContractRegistrationError) as excinfo:
        _contract(SIDE_EFFECT_EXTERNAL, short)
    assert "compute_quote_hash" in str(excinfo.value)


def test_compute_only_still_binds_identity_object_and_quote():
    """静默扣不等于不绑定 —— 少了 payer 绑定就等于开着"扣错人钱"那条路。"""
    short = ConfirmationPolicy(
        CONFIRM_SILENT_WITH_NOTICE,
        tuple(f for f in COMPUTE_ONLY_MIN_BIND_FIELDS if f != "payer_id"),
    )
    with pytest.raises(ContractRegistrationError) as excinfo:
        _contract(
            SIDE_EFFECT_COMPUTE_ONLY, short,
            capability="writing.generate", resource_kind="article",
        )
    assert "payer_id" in str(excinfo.value)


def test_idempotency_must_cover_all_four_phases_including_execute():
    from services.xiaobang_command_contract import IdempotencyContract

    partial = IdempotencyContract(
        scope="tenant_operation_request",
        required_phases=("prepare", "confirm", "cancel"),
        phase_contracts={"prepare": "a", "confirm": "b", "cancel": "c"},
    )
    with pytest.raises(ContractRegistrationError) as excinfo:
        _contract(SIDE_EFFECT_EXTERNAL, external_confirmation_policy(),
                  idempotency=partial)
    assert "execute" in str(excinfo.value)


def test_executable_true_without_execute_adapter_is_rejected():
    with pytest.raises(ContractRegistrationError):
        _contract(SIDE_EFFECT_EXTERNAL, external_confirmation_policy(), executable=True)


def test_public_dict_never_carries_adapter_or_internal_route():
    contract = _contract(SIDE_EFFECT_EXTERNAL, external_confirmation_policy())
    public = contract.as_public_dict()
    assert "adapters" not in public
    assert "idempotency" not in public
    assert all("x_domain" not in str(v) for v in public.values())


# ── 社媒域负向枚举锁 ──────────────────────────────────────────────────────
def test_social_lock_rejects_a_social_capability_contract():
    with pytest.raises(SocialDomainLockError):
        _contract(
            SIDE_EFFECT_COMPUTE_ONLY, compute_only_confirmation_policy(),
            capability="social.script_generate", resource_kind="script",
        )


def test_social_lock_rejects_a_social_adapter_module():
    with pytest.raises(SocialDomainLockError):
        CommandContract(
            required_capability="writing.generate",
            resource_kind="article",
            side_effect=SIDE_EFFECT_COMPUTE_ONLY,
            supported_phases=ALL_PHASES,
            adapters={"query": "tools.social_operator.content_workshop"},  # 模块已随开源 E3 B2 删;这里是注入的毒串,锁拦的是这个名字
            confirmation_policy=compute_only_confirmation_policy(),
            idempotency=standard_idempotency(),
        )


def test_social_lock_covers_the_whole_registry_axis():
    """轴一:能力注册表。现役 54 条一条都不许是社媒域。"""
    entries = omap.all_operations()
    assert len(entries) >= 50, "注册表条数异常,判据的分母是空的"
    for entry in entries:
        assert_no_social_domain(
            identifier=entry.operation_id,
            route=entry.route_template,
            capability=(
                entry.command_contract.required_capability
                if entry.command_contract is not None else ""
            ),
            modules=(entry.required_module or "",),
            where="registry-sweep",
        )


def test_social_lock_covers_the_member_route_whitelist_axis():
    """轴二:员工路由白名单。

    🔴 单锁注册表盖不住这条轴:一条社媒路由塞进 MEMBER_GEO_ROUTE_POLICIES,
       员工席位照样能走通,而注册表里一个字都没变。
    """
    assert len(MEMBER_GEO_ROUTE_POLICIES) >= 100, "路由表条数异常,分母是空的"
    for policy in MEMBER_GEO_ROUTE_POLICIES:
        assert_no_social_domain(
            identifier=policy.operation,
            route=policy.path_template,
            capability=policy.capability,
            where="member-route-sweep",
        )


def test_social_lock_is_alive_on_both_axes():
    """反向对照:两条轴各喂一个真社媒样本,必须命中。

    没有这一条,上面两条"全过"与"扫描器是空的"完全同形。
    """
    assert social_domain_hits(identifier="social_studio_script", route="/s/scripts")
    assert social_domain_hits(identifier="my_ip_center", route="/social")
    assert social_domain_hits(capability="social.publish")
    assert social_domain_hits(modules=("frontend/src/pages/SocialStudio/x.tsx",))  # 路径已退役、token 按设计仍在表里;验词段匹配


def test_social_lock_does_not_false_positive_on_lookalike_names():
    """🔴 词段匹配而不是子串匹配。

    第一版这里用 ``token in identifier``,当场把 ``personal_settings``
    判成社媒域(``persona`` ⊂ ``personal``)—— 锁拦的是无辜的人。
    """
    for identifier, route in (
        ("personal_settings", "/account/profile"),
        ("admin_settings", "/settings"),
        ("today_workspace", "/dashboard/today"),
        ("publish_center", "/publish"),
        ("geo_content_center", "/marketing-materials"),
        ("feedback", "/feedback"),
    ):
        assert social_domain_hits(identifier=identifier, route=route) == [], identifier
