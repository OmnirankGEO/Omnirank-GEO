"""机器契约 ↔ Pydantic 逐字段对齐(§10.2 / 04 §3.2)。

自动比对 observation_contract_v1.json 与本包模型:枚举、source 映射、信封字段名+可空性、
platform_health_fields、policy_schema 各子字段。任一漂移 → 红(开工阻断的守卫)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args, get_origin, Union

import pytest

from services.ai_surface_monitoring import contracts as C
from services.ai_surface_monitoring.contracts import (
    ObservationEnvelopeV1,
    SearchQueryEvidence,
    SurfaceHealth,
    UsageEvidence,
)
from services.ai_surface_monitoring.policy import (
    ObservationFeatureFlagsV1,
    ObservationPolicySnapshot,
    PlatformPolicyV1,
    SamplingBudgetV1,
    SourceBaseWeightsV1,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs/AI-CONTEXT/GEO_OBSERVATION_FLYWHEEL_VNEXT_2026-07-17/contracts/observation_contract_v1.json"
)


@pytest.fixture(scope="module")
def contract() -> dict:
    assert _CONTRACT_PATH.exists(), f"机器契约缺失: {_CONTRACT_PATH}"
    return json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def _allows_none(model, field_name: str) -> bool:
    ann = model.model_fields[field_name].annotation
    if get_origin(ann) is Union:
        return type(None) in get_args(ann)
    return ann is type(None)


def test_schema_version(contract):
    assert contract["schema_version"] == C.SCHEMA_VERSION


def test_surface_keys_match(contract):
    assert tuple(contract["surface_keys"]) == C.SURFACE_KEYS


def test_response_status_match(contract):
    assert tuple(contract["response_status"]) == C.RESPONSE_STATUSES


def test_source_kind_mapping_match(contract):
    assert contract["source_kind_to_source_type"] == C.SOURCE_KIND_TO_SOURCE_TYPE


def test_envelope_field_names_match(contract):
    contract_fields = {f["name"] for f in contract["envelope_fields"]}
    model_fields = set(ObservationEnvelopeV1.model_fields.keys())
    assert contract_fields == model_fields, (
        f"missing in model: {contract_fields - model_fields}; extra in model: {model_fields - contract_fields}"
    )


def test_envelope_field_nullability_match(contract):
    mismatches = []
    for f in contract["envelope_fields"]:
        name, nullable = f["name"], f["nullable"]
        model_nullable = _allows_none(ObservationEnvelopeV1, name)
        if bool(nullable) != bool(model_nullable):
            mismatches.append((name, nullable, model_nullable))
    assert not mismatches, f"nullability drift: {mismatches}"


def test_search_query_evidence_fields(contract):
    names = {f["name"] for f in contract["search_query_evidence_fields"]}
    assert names == set(SearchQueryEvidence.model_fields.keys())


def test_platform_health_fields_match(contract):
    names = {f["name"] for f in contract["platform_health_fields"]}
    assert names == set(SurfaceHealth.model_fields.keys())
    for f in contract["platform_health_fields"]:
        assert bool(f["nullable"]) == _allows_none(SurfaceHealth, f["name"]), f["name"]


def test_usage_evidence_fields(contract):
    # UsageEvidence 四字段(Codex #6)
    expected = {"input_tokens", "output_tokens", "estimated_cost_micros", "usage_is_estimated"}
    assert set(UsageEvidence.model_fields.keys()) == expected


def test_policy_top_level_writable_fields_match(contract):
    """复审 P1-6:ObservationPolicySnapshot 必须逐项覆盖 policy_schema.writable_fields(11 项)。"""
    contract_fields = {f["name"] for f in contract["policy_schema"]["writable_fields"]}
    model_fields = set(ObservationPolicySnapshot.model_fields.keys())
    assert contract_fields == model_fields, (
        f"missing in snapshot: {contract_fields - model_fields}; extra: {model_fields - contract_fields}")


def test_full_ai2_policy_validates_without_extra_forbidden(contract):
    """复审 P1-6:AI-2 的完整合法 policy(含 6 治理字段)输入本模型不得 extra_forbidden。"""
    snap = ObservationPolicySnapshot(
        policy_version="ai2-real-v1",
        # 契约 check④:全目录合计恰 10000(单启用平台 → 10000)
        platforms=[PlatformPolicyV1(platform_key="doubao", enabled=True, base_weight_bps=10000,
                                    surface_key="doubao_ark_api_search", legacy_read_only=False)],
        source_base_weights_bps=SourceBaseWeightsV1(),
        sampling_budget=SamplingBudgetV1(),
        max_single_brand_share_bps=1000,
        public_min_independent_brands=3,
        public_min_source_types=2,
        retention_days=180,
        anomaly_confirmation_numerator=2,
        anomaly_confirmation_denominator=3,
        feature_flags=ObservationFeatureFlagsV1(),
    )
    assert snap.policy_version == "ai2-real-v1" and snap.max_single_brand_share_bps == 1000


def test_policy_platform_fields_match(contract):
    names = {f["name"] for f in contract["policy_schema"]["platform_policy_fields"]}
    assert names == set(PlatformPolicyV1.model_fields.keys())


def test_policy_feature_flag_fields_match(contract):
    names = {f["name"] for f in contract["policy_schema"]["feature_flag_fields"]}
    assert names == set(ObservationFeatureFlagsV1.model_fields.keys())


def test_policy_sampling_budget_fields_match(contract):
    names = {f["name"] for f in contract["policy_schema"]["sampling_budget_fields"]}
    assert names == set(SamplingBudgetV1.model_fields.keys())


def test_policy_source_base_weight_fields_match(contract):
    names = {f["name"] for f in contract["policy_schema"]["source_base_weight_fields"]}
    assert names == set(SourceBaseWeightsV1.model_fields.keys())


def test_query_kind_enum_used_by_envelope(contract):
    # branded|non_branded|comparative|verification 是 query_kind 域
    for f in contract["envelope_fields"]:
        if f["name"] == "query_kind":
            assert "branded" in f["type"] and "verification" in f["type"]
            break
    else:
        pytest.fail("query_kind not in envelope_fields")
    assert C.QUERY_KINDS == ("branded", "non_branded", "comparative", "verification")
