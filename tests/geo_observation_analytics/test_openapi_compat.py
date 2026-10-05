"""OpenAPI + frozen fixture + copy-contract compatibility.

Proves the AI-3 product DTOs are a business-semantic superset of every
frontend_race_fixture_v1.json case (so both frontend candidates can consume the
real OpenAPI), the router exposes the contracted paths, and the copy contract
covers every outcome / metric the API emits.
"""

from __future__ import annotations

import pytest

from schemas import geo_observation_product as dto
from services.geo_observation_analytics import contract as C

from _support import build_test_app, build_test_deps

pytestmark = pytest.mark.integration


def _fields(model) -> set:
    return set(model.model_fields.keys())


def _assert_keys_subset(fixture_obj: dict, model, ctx: str):
    missing = set(fixture_obj.keys()) - _fields(model)
    assert not missing, f"{ctx}: DTO {model.__name__} missing fixture keys {sorted(missing)}"


# ---------------------------------------------------------------------------
# OpenAPI exposes the contracted product + admin paths
# ---------------------------------------------------------------------------
def test_openapi_has_contracted_paths():
    app = build_test_app(build_test_deps())
    paths = set(app.openapi()["paths"].keys())
    expected = {
        "/api/geo-observation/brands/{brand_id}/summary",
        "/api/geo-observation/brands/{brand_id}/trend",
        "/api/geo-observation/brands/{brand_id}/platforms",
        "/api/geo-observation/brands/{brand_id}/questions",
        "/api/geo-observation/brands/{brand_id}/evidence/{observation_id}",
        "/api/geo-observation/brands/{brand_id}/opportunities",
        "/api/geo-observation/brands/{brand_id}/insights",
        "/api/geo-observation/brands/{brand_id}/insights/{job_id}",
        "/api/geo-observation/industries/{industry_key}/baseline",
        "/api/geo-observation/industries/{industry_key}/source-patterns",
        "/api/geo-observation/industries/{industry_key}/content-opportunities",
        "/api/admin/geo-observation/overview",
        "/api/admin/geo-observation/platform-health",
        "/api/admin/geo-observation/model-shifts",
        "/api/admin/geo-observation/aggregate-diff",
        "/api/admin/geo-observation/content-opportunities",
    }
    assert expected <= paths, f"missing paths: {sorted(expected - paths)}"


# ---------------------------------------------------------------------------
# DTOs cover every fixture case's business fields
# ---------------------------------------------------------------------------
def test_dtos_superset_of_fixture():
    fx = C.load_frozen_fixture()["cases"]

    bs = fx["brand_summary"]["response"]
    _assert_keys_subset(bs, dto.BrandSummaryDTO, "brand_summary")
    _assert_keys_subset(bs["brand"], dto.BrandRefDTO, "brand_summary.brand")
    _assert_keys_subset(bs["window"], dto.WindowDTO, "brand_summary.window")
    _assert_keys_subset(bs["summary"], dto.BrandSummaryMetricsDTO, "brand_summary.summary")
    _assert_keys_subset(bs["comparison"], dto.ComparisonDTO, "brand_summary.comparison")
    _assert_keys_subset(bs["outcomes"][0], dto.OutcomeCountDTO, "brand_summary.outcomes[]")
    _assert_keys_subset(bs["next_actions"][0], dto.NextActionDTO, "brand_summary.next_actions[]")

    pf = fx["platforms"]["response"]
    _assert_keys_subset(pf, dto.BrandPlatformsDTO, "platforms")
    _assert_keys_subset(pf["items"][0], dto.PlatformItemDTO, "platforms.items[]")
    _assert_keys_subset(pf["historical_platforms"][0], dto.HistoricalPlatformDTO, "platforms.historical[]")

    q = fx["questions"]["response"]
    _assert_keys_subset(q, dto.QuestionsPageDTO, "questions")
    _assert_keys_subset(q["items"][0], dto.QuestionItemDTO, "questions.items[]")

    ed = fx["evidence_detail"]["response"]
    _assert_keys_subset(ed, dto.EvidenceDetailDTO, "evidence_detail")
    _assert_keys_subset(ed["next_action"], dto.EvidenceNextActionDTO, "evidence_detail.next_action")
    _assert_keys_subset(ed["channel_disclosure"], dto.ChannelDisclosureDTO, "evidence_detail.channel_disclosure")

    op = fx["opportunities"]["response"]
    _assert_keys_subset(op, dto.OpportunitiesDTO, "opportunities")
    _assert_keys_subset(op["items"][0], dto.OpportunityItemDTO, "opportunities.items[]")

    sj = fx["semantic_insight_job"]
    _assert_keys_subset(sj["create"]["response"], dto.InsightJobStatusDTO, "semantic.create")
    _assert_keys_subset(sj["completed"]["response"], dto.InsightJobStatusDTO, "semantic.completed")

    ao = fx["admin_overview"]["response"]
    _assert_keys_subset(ao, dto.AdminOverviewDTO, "admin_overview")
    _assert_keys_subset(ao["counts"], dto.AdminCountsDTO, "admin_overview.counts")
    _assert_keys_subset(ao["platform_health"][0], dto.AdminPlatformHealthDTO, "admin_overview.platform_health[]")


# ---------------------------------------------------------------------------
# Fixture uses only the frozen enums
# ---------------------------------------------------------------------------
def test_fixture_outcomes_are_frozen_enums():
    fx = C.load_frozen_fixture()["cases"]
    outcomes = {o["outcome"] for o in fx["brand_summary"]["response"]["outcomes"]}
    outcomes |= {i["outcome"] for i in fx["questions"]["response"]["items"]}
    assert outcomes <= set(C.TARGET_OUTCOMES)


# ---------------------------------------------------------------------------
# Copy contract covers every outcome / metric the API emits
# ---------------------------------------------------------------------------
def test_copy_contract_covers_outcomes_and_metrics():
    copy = C.load_frozen_copy_contract()
    assert set(copy["outcome_labels"].keys()) == set(C.TARGET_OUTCOMES)
    # every rate DTO field the summary emits has a business label or is a known
    # structural field.
    labeled = set(copy["metric_labels"].keys())
    for key in ["presence_rate_bps", "explicit_recommendation_rate_bps",
                "conditional_recommendation_rate_bps", "criteria_only_rate_bps",
                "refusal_no_evidence_rate_bps", "refusal_risk_rate_bps",
                "citation_rate_bps", "evidence_coverage_rate_bps",
                "share_of_voice_bps", "valid_observations"]:
        assert key in labeled, f"copy contract missing label for {key}"
    # forbidden engineering terms are declared for the frontend scanner
    assert "bps" in copy["forbidden_unexplained_user_terms"]
    assert set(copy["required_states"]) >= {
        "loading", "empty", "error", "forbidden_403", "conflict_409",
        "env_override_423", "unavailable_503", "insufficient_samples",
        "model_shift", "semantic_insight_unavailable",
    }
