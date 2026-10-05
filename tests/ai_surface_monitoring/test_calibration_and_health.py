"""校准 harness(§7)+ 只读健康 DTO(§8 / platform_health_fields)。"""

from __future__ import annotations

import pytest

from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter
from services.ai_surface_monitoring.calibration import CalibrationHarness
from services.ai_surface_monitoring.contracts import CollectionRequest, SurfaceHealth, utcnow
from services.ai_surface_monitoring.policy import FakeObservationPolicy
from services.ai_surface_monitoring.registry import AdapterRegistry, _build_default_adapters
from .conftest import make_engine_fetch


async def test_calibration_harness_produces_deterministic_metrics():
    async def collect(surface_key, question, run_index):
        adapter = WrappedResearchAdapter(surface_key, fetch=make_engine_fetch(
            answer="答", citations=[{"url": "http://x.com", "title": "X", "rank": 1, "is_answer_cited": True}]))
        req = CollectionRequest(request_id=f"{surface_key}-{question}-{run_index}",
                                source_kind="research", source_ref="s", question_text=question,
                                query_kind="non_branded", surface_key=surface_key)
        return await adapter.collect(req)

    harness = CalibrationHarness(collect, repeats=2, judge_version="jv1", question_set_version="qs1")
    report = await harness.run(["doubao_ark_api_search", "qwen_dashscope_search"], ["q1", "q2"])
    assert report.judge_version == "jv1" and report.question_set_version == "qs1"
    doubao = report.per_surface["doubao_ark_api_search"]
    assert doubao["total"] == 4               # 2 questions × 2 repeats
    assert doubao["answer_rate_bps"] == 10000  # 全 answered
    assert doubao["citation_rate_bps"] == 10000
    # 同域名 → jaccard 1.0
    assert report.domain_overlap_jaccard["doubao_ark_api_search|qwen_dashscope_search"] == 1.0


async def test_calibration_cost_null_not_counted_as_zero():
    async def collect(surface_key, question, run_index):
        # 无 usage → hy3 成本 None(不当 0)
        from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter
        from .conftest import make_http_post
        adapter = OpenAICompatibleAdapter(surface_key, http_post=make_http_post(
            {"id": "x", "model": "hy3", "choices": [{"message": {"content": "答"}}]}),
            api_key_getter=lambda n: "K")
        req = CollectionRequest(request_id=f"{surface_key}-{run_index}", source_kind="research",
                                source_ref="s", question_text=question, query_kind="non_branded", surface_key=surface_key)
        return await adapter.collect(req)

    harness = CalibrationHarness(collect, repeats=1)
    report = await harness.run(["yuanbao_hy3_tokenhub"], ["q1"])
    yb = report.per_surface["yuanbao_hy3_tokenhub"]
    assert yb["avg_cost_micros_known"] is None   # 成本未知 → None,不算 0
    assert yb["cost_known_ratio_bps"] == 0


async def test_adapter_health_dto_shape_and_readonly():
    """健康 DTO 只读反映真实探针;结构保留但无自动可用性的表面 → unavailable。"""
    from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter
    adapters = _build_default_adapters()
    # 强制 openai adapter 空 key → 探针短路 unavailable(绝不发网络,与 env 无关)
    adapters["yuanbao_hy3_tokenhub"] = OpenAICompatibleAdapter(
        "yuanbao_hy3_tokenhub", api_key_getter=lambda n: "")
    adapters["deepseek_native_no_search"] = OpenAICompatibleAdapter(
        "deepseek_native_no_search", api_key_getter=lambda n: "", verify_model_echo=False)
    reg = AdapterRegistry(FakeObservationPolicy(), adapters=adapters)
    # 只探针 unavailable 占位表面(不发网络)
    healths = await reg.get_adapter_health(only_active=False)
    by_key = {h.surface_key: h for h in healths}
    assert isinstance(by_key["manual_app_capture"], SurfaceHealth)
    assert by_key["manual_app_capture"].status == "unavailable"       # manual_only
    # [2026-07-27] deepseek_native_with_search 已实测转 active(不再是占位表面),
    # 这里改用仍然 unavailable 的 metaso 代理表面来验"占位表面探针不发网络"。
    assert by_key["deepseek_metaso_proxy"].status == "unavailable"
    # 健康 DTO 不含策略可写字段(只读反映,不写回 policy)
    for h in healths:
        assert set(h.model_dump().keys()) == {
            "platform_key", "provider_key", "model_key", "surface_key",
            "status", "checked_at", "latency_ms", "model_revision", "error_code"}


def test_health_status_enum_matches_contract():
    from services.ai_surface_monitoring.contracts import HEALTH_STATUSES
    assert HEALTH_STATUSES == ("healthy", "degraded", "unavailable", "unknown")


# ---- P2-7: repeat stability + admission thresholds + entity-judge hook ----
def _env(surface, status="answered", citations=None):
    from services.ai_surface_monitoring.contracts import (
        ObservationEnvelopeV1, CitationEvidence, utcnow)
    return ObservationEnvelopeV1(
        request_id="r", source_kind="research", source_ref="s", question_text="q", question_hash="h",
        query_kind="non_branded", platform_key="p", provider_key="pr", model_key="m",
        surface_key=surface, session_mode="clean", prompt_text="q", answer_text="a", answer_hash="h",
        response_status=status, citations=[CitationEvidence(url=u, source_type="citation") for u in (citations or [])],
        observed_at=utcnow())


async def test_repeat_stability_metric():
    from services.ai_surface_monitoring.calibration import CalibrationHarness
    # 全 answered → 稳定度 10000;answered/unknown 交替 → 每 question 2 次众数 1/2 → 5000
    calls = {"n": 0}

    async def stable(surface, q, run):
        return _env(surface, "answered")

    async def flapping(surface, q, run):
        return _env(surface, "answered" if run == 1 else "unknown")

    rs = (await CalibrationHarness(stable, repeats=3).run(["doubao_ark_api_search"], ["q1"])).per_surface
    assert rs["doubao_ark_api_search"]["repeat_stability_bps"] == 10000
    rf = (await CalibrationHarness(flapping, repeats=2).run(["doubao_ark_api_search"], ["q1"])).per_surface
    assert rf["doubao_ark_api_search"]["repeat_stability_bps"] == 5000


async def test_admission_threshold_fails_low_answer_rate():
    from services.ai_surface_monitoring.calibration import CalibrationHarness

    async def all_unknown(surface, q, run):
        return _env(surface, "unknown")
    report = await CalibrationHarness(all_unknown, repeats=2).run(["qwen_dashscope_search"], ["q1", "q2"])
    adm = report.admission["qwen_dashscope_search"]
    assert adm["passes"] is False and any("answer_rate" in r for r in adm["reasons"])


async def test_entity_judge_metrics_only_with_hook():
    from services.ai_surface_monitoring.calibration import CalibrationHarness

    async def collect(surface, q, run):
        return _env(surface, "answered")

    def judge(env):
        return {"mentioned": True, "recommended": True, "brands": ["A", "B"]}

    # 无 judge → 品牌/推荐指标 None(不臆造)
    no_judge = (await CalibrationHarness(collect, repeats=2).run(["doubao_ark_api_search"], ["q1"])).per_surface
    assert no_judge["doubao_ark_api_search"]["brand_mention_rate_bps"] is None
    assert no_judge["doubao_ark_api_search"]["recommendation_jaccard"] is None
    # 有 judge → 提及率 10000;重复推荐集相同 → jaccard 1.0
    with_judge = (await CalibrationHarness(collect, repeats=2, entity_judge=judge).run(
        ["doubao_ark_api_search"], ["q1"])).per_surface
    assert with_judge["doubao_ark_api_search"]["brand_mention_rate_bps"] == 10000
    assert with_judge["doubao_ark_api_search"]["recommendation_jaccard"] == 1.0
