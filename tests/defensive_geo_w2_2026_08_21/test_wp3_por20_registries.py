"""POR-20 判据 · 十张 registry 的 AST census + 跨 registry union exact。

POR-20 逐字:「keys 与 DTO producer/consumer/action/route union **exact**;
code-copy-primary action、intent/target/capability/effects……逐行相等;
**死/漏 row**、交换合法行或 generation 变化未失效 cache 均拒绝」。
"""

from __future__ import annotations

import pytest

from scripts.defgeo_census import registry_census as CEN
from services.defensive_geo.presentation import copy_registry as C
from services.defensive_geo.presentation import public_registries as PR
from services.defensive_geo.presentation import registries as R
from services.defensive_geo import work_admission as WA


# ═══════════════════════════ AST census 本身

def test_census_reports_exactly_ten_registries():
    result = CEN.census()
    assert len(result["registered_versions"]) == 10
    assert set(result["registered_versions"]) == set(PR.REGISTRY_VERSIONS)


def test_no_dead_and_no_missing_tables():
    """双向差集:源码有而未登记 = 死表;登记而源码无 = 漏表。"""
    result = CEN.census()
    assert result["dead_tables"] == [], result["dead_tables"]
    assert result["missing_tables"] == [], result["missing_tables"]


def test_declared_versions_match_registered_versions():
    result = CEN.census()
    assert result["version_not_registered"] == []
    assert result["registered_not_declared"] == []


def test_ast_and_runtime_agree():
    """AST 结论 × 运行时字典互校 —— 两边不等说明有一边在说谎。"""
    assert CEN.cross_check_runtime() == []


def test_census_selftest_passes():
    """census 自身的判别力自证(三类注毒 + 反向对照)必须通过。

    没有这条,上面四条"全空"可能只是因为 census 什么都不会报。
    """
    assert CEN._selftest() == 0


# ═══════════════════════════ 逐行形态

def test_every_row_has_human_copy_not_an_internal_enum():
    """POR-20「code-copy……逐行相等」+ U-1「内部枚举裸串上屏 = 红」。"""
    for version, table in PR.ALL_REGISTRIES.items():
        for code, row in table.items():
            assert row.copy, (version, code)
            assert not C.looks_like_internal_enum(row.copy), (version, code, row.copy)


def test_row_code_matches_its_dict_key():
    """交换合法行必须被发现:key 与 row.code 不一致即拒。"""
    for version, table in PR.ALL_REGISTRIES.items():
        for code, row in table.items():
            assert code == row.code, (version, code, row.code)


def test_no_duplicate_codes_within_a_registry():
    for version, table in PR.ALL_REGISTRIES.items():
        assert len(table) == len(set(table)), version


def test_effects_come_from_a_closed_set():
    allowed = {"mutation", "billing", "job"}
    assert PR.all_effects() <= allowed, PR.all_effects() - allowed


def test_billing_effect_always_accompanies_mutation():
    """会扣算力的行必然也改状态 —— 只 billing 不 mutation 说明分类写错了。"""
    for version, table in PR.ALL_REGISTRIES.items():
        for code, row in table.items():
            if "billing" in row.effects:
                assert "mutation" in row.effects, (version, code)


# ═══════════════════════════ 跨 registry union exact

def test_admission_registry_matches_work_admission_block_reasons():
    """🔴 POR-20「keys 与 DTO union exact」的一个可机械验的实例。

    ``work_admission`` 的 BlockReason 与
    ``admission_unavailable_reason_registry_v1`` 必须**双向**相等:
    多一个 = 死 row(永远不会被产出),少一个 = 漏 row(运行时拿不到文案,
    最好情况是把 code 本身显示出来 = 内部枚举裸串上屏)。
    """
    registry_codes = set(PR.ADMISSION_UNAVAILABLE_REASON)
    # 从 work_admission 的实际拒绝路径**机械收集**,不手抄
    produced = set()
    cases = [
        dict(milestone="customer_accepted", funding_policy="personal_wallet",
             balance_points=1, required_points=1),
        dict(milestone="service_activated", funding_policy="personal_wallet",
             balance_points=0, required_points=99),
        dict(milestone="service_activated", funding_policy="organization_budget",
             balance_points=0, required_points=99, approval_state="required"),
        dict(milestone="service_activated", funding_policy="personal_wallet",
             balance_points=10 ** 9, required_points=99, scope_remaining_points=1),
        dict(milestone="service_activated", funding_policy="personal_wallet",
             balance_points=10 ** 9, required_points=1, capability_available=False),
    ]
    for case in cases:
        v = WA.admit(work_kind="content_generation", **case)
        assert not v.admitted
        produced.add(v.reason)
    assert produced == registry_codes, {
        "only_produced": produced - registry_codes,
        "only_registry": registry_codes - produced,
    }


def test_admission_registry_copy_matches_no_internal_enum():
    for code, row in PR.ADMISSION_UNAVAILABLE_REASON.items():
        assert not C.looks_like_internal_enum(row.copy), code


def test_professional_section_registry_is_the_one_registries_module_names():
    """两处引用同一个版本名,不得各写各的。"""
    assert R.SECTION_REGISTRY_VERSION in PR.REGISTRY_VERSIONS


def test_priority_action_intents_match_the_spec_four():
    """§15.6 PriorityActionFactBasis 的四个 actionIntent。"""
    assert set(PR.PRIORITY_ACTION_POLICY) == {
        "content_or_publication_repair", "fact_collection",
        "identity_calibration", "comparable_retest",
    }


def test_z3_fourth_outlet_exists_and_is_not_business_available():
    """🔴 §0.5.6 Z-3.1:fact_collection 增加第四出口「AI 联网补齐」。

    同时保留「fact_collection 禁止 business_available」(MET-19/42 逐字)——
    这两条不矛盾:第四出口是 ai_autofill,不是 business_available。
    """
    availability = PR.PUBLIC_PRIORITY_AVAILABILITY_REASON
    assert "ai_autofill_available" in availability
    fact = PR.PRIORITY_ACTION_POLICY["fact_collection"]
    assert fact.primary_action == "ai_autofill"
    assert fact.primary_action != "business_available"
    # 它扣算力,所以必须带 billing 副作用(不能装成免费)
    assert "billing" in availability["ai_autofill_available"].effects


def test_every_action_kind_is_reachable_from_some_route_or_is_terminal():
    """无未注册 action:每个 primary_action 要么在 handoff route 表里,
    要么是本表自己的终端动作。悬空 action = 点了没反应。"""
    routes = set(PR.PROVIDER_HANDOFF_ROUTE)
    terminal = {
        "view_evidence", "view_raw_answers", "view_plan", "view_milestone",
        "view_approver", "generate_pdf", "generate_plan", "calibrate",
        "calibrate_identity", "start_retest", "retry_platform",
        "build_comparable_retest_plan", "ai_autofill", "contact_provider",
        "contact_admin", "raise_scope_cap", "skip_capability", "reduce_plan",
    }
    unknown = PR.all_action_kinds() - routes - terminal
    assert unknown == set(), unknown


# ═══════════════════════════ generation hash 与 cache 失效

def test_generation_hash_is_stable_for_unchanged_registries():
    assert PR.generation_hash() == PR.generation_hash()
    assert len(PR.generation_hash()) == 64


def test_generation_hash_changes_when_any_row_changes(monkeypatch):
    """POR-20「generation 变化未失效 cache 拒绝」的前提:hash 必须真的变。"""
    before = PR.generation_hash()
    patched = dict(PR.ADMISSION_UNAVAILABLE_REASON)
    patched["service_not_activated"] = patched["service_not_activated"]._replace(
        copy="改过的文案")
    monkeypatch.setitem(
        PR.ALL_REGISTRIES, "admission_unavailable_reason_registry_v1", patched)
    assert PR.generation_hash() != before


def test_generation_hash_changes_when_a_row_is_removed(monkeypatch):
    before = PR.generation_hash()
    patched = dict(PR.OBSERVATION_ERROR)
    patched.pop("timeout")
    monkeypatch.setitem(
        PR.ALL_REGISTRIES, "observation_error_registry_v1", patched)
    assert PR.generation_hash() != before


def test_unknown_registry_lookup_is_rejected():
    with pytest.raises(ValueError):
        PR.registry("no_such_registry_v1")


def test_row_count_is_reported_mechanically():
    result = CEN.census()
    runtime_rows = sum(len(t) for t in PR.ALL_REGISTRIES.values())
    assert result["total_rows"] == runtime_rows
