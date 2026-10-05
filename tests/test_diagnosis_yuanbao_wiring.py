"""新售诊断默认矩阵与元宝统一观测接线判别测试（零真实 provider 调用）。

[P0-2 · Owner 2026-07-26 裁决] 诊断与监测统一五引擎
（dashscope/deepseek/doubao/kimi/yuanbao），因此默认矩阵不再是"四路含元宝不含
Kimi"。断言改为**与唯一常量源 config.ai_engines 对齐**——改常量测试跟着变，
这比写死字面量更能挡住"引擎清单双源打架"复发。秘塔仍不得被默认入口调用。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


class _FakeService:
    def __init__(self, envelope):
        self.envelope = envelope
        self.calls = []

    async def collect_paid_delivery(self, request, *, round_id=None):
        self.calls.append((request, round_id))
        return self.envelope


def _envelope(*, status="answered", answer="岱林生物值得优先考虑。"):
    return SimpleNamespace(
        response_status=status,
        answer_text=answer,
        search_enabled=False,
        citations=[],
        platform_key="yuanbao",
        surface_key="yuanbao_hy3_tokenhub",
        observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_yuanbao_diagnosis_uses_observation_ssot_and_preserves_lineage(monkeypatch):
    from services.geo_observation import integration
    from tools.ai_visibility import ai_tester

    service = _FakeService(_envelope())
    monkeypatch.setattr(integration, "collection_runtime", lambda: SimpleNamespace(service=service))

    async def fake_analyze(answer, query, brand, engine, brand_id, display_names):
        assert answer == "岱林生物值得优先考虑。"
        assert query == "杭州生物设备公司哪家好？"
        assert brand == "岱林生物"
        assert engine == "元宝"
        assert brand_id == 23
        assert display_names == ["岱林"]
        return {
            "response": answer,
            "brand_detected": True,
            "brand_verdict": "YES",
            "mentioned_brands_inline": ["岱林生物"],
        }

    monkeypatch.setattr(ai_tester, "_call_analyze_visibility", fake_analyze)
    response = await ai_tester.query_yuanbao(
        "杭州生物设备公司哪家好？",
        "岱林生物",
        brand_id=23,
        brand_display_names=["岱林"],
        observation_source_ref="run-20260720-1",
        observation_round_id="paid-diagnosis:run-20260720-1",
        observation_request_id="diagnosis-yuanbao:test-1",
        owner_user_id=8,
        industry="生物设备",
    )

    request, round_id = service.calls[0]
    assert request.source_kind == "paid_diagnosis"
    assert request.source_ref == "run-20260720-1"
    assert request.surface_key == "yuanbao_hy3_tokenhub"
    assert request.owner_user_id == 8
    assert request.brand_id == 23
    assert request.question_text == "杭州生物设备公司哪家好？"
    assert round_id == "paid-diagnosis:run-20260720-1"

    payload = json.loads(response.content[0]["text"])
    assert payload["platform_key"] == "yuanbao"
    assert payload["surface_key"] == "yuanbao_hy3_tokenhub"
    assert payload["brand_detected"] is True


@pytest.mark.asyncio
async def test_yuanbao_failure_is_unknown_and_does_not_leak_internal_detail(monkeypatch):
    from services.geo_observation import integration
    from tools.ai_visibility import ai_tester

    secret = "SECRET_TOKEN https://internal.invalid/private"

    class FailingService:
        async def collect_paid_delivery(self, *_args, **_kwargs):
            raise RuntimeError(secret)

    monkeypatch.setattr(
        integration,
        "collection_runtime",
        lambda: SimpleNamespace(service=FailingService()),
    )
    response = await ai_tester.query_yuanbao(
        "测试问题",
        "测试品牌",
        observation_source_ref="run-failure",
    )
    serialized = response.content[0]["text"]
    payload = json.loads(serialized)
    assert payload["brand_verdict"] == "UNKNOWN"
    assert payload["engine_error"] is True
    assert payload["platform_key"] == "yuanbao"
    assert secret not in serialized
    assert "internal.invalid" not in serialized


@pytest.mark.asyncio
async def test_default_diagnosis_matrix_matches_single_engine_source(monkeypatch):
    from config.ai_engines import DIAGNOSIS_ENGINES
    from tools.ai_visibility import ai_tester

    called = []

    async def fake_query(*_args, **_kwargs):
        called.append(_kwargs.get("observation_source_ref", "legacy"))
        payload = {
            "response": "有效回答",
            "brand_detected": False,
            "brand_verdict": "NO",
            "mentioned_brands_inline": [],
        }
        return SimpleNamespace(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])

    monkeypatch.setattr(ai_tester, "query_dashscope_search", fake_query)
    monkeypatch.setattr(ai_tester, "query_dashscope_deepseek", fake_query)
    monkeypatch.setattr(ai_tester, "query_doubao_search", fake_query)
    monkeypatch.setattr(ai_tester, "query_yuanbao", fake_query)
    monkeypatch.setattr(ai_tester, "query_kimi_search", fake_query)
    monkeypatch.setattr(
        ai_tester,
        "query_metaso_search",
        lambda *_args, **_kwargs: pytest.fail("新售默认诊断不得调用秘塔"),
    )

    result = await ai_tester.detailed_ai_visibility_test(
        questions=["测试问题"],
        check_brand="测试品牌",
        observation_source_ref="run-default-matrix",
    )
    # 与唯一常量源对齐（不写死字面量：改常量两边同步变才是 P0-2 的修复目标）
    assert result["engines"] == list(DIAGNOSIS_ENGINES)
    assert set(result["detail_table"][0]["results"]) == set(DIAGNOSIS_ENGINES)
    assert called


@pytest.mark.asyncio
async def test_batch_and_longtail_defaults_use_same_new_sale_matrix(monkeypatch):
    from config.ai_engines import DIAGNOSIS_ENGINES
    from tools.ai_visibility import ai_tester

    called: list[str] = []

    def fake_for(engine: str):
        async def fake_query(*_args, **_kwargs):
            called.append(engine)
            payload = {
                "engine": engine,
                "response": "有效回答",
                "brand_detected": False,
                "brand_verdict": "NO",
                "mentioned_brands_inline": [],
            }
            return SimpleNamespace(content=[{
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }])
        return fake_query

    monkeypatch.setattr(ai_tester, "query_dashscope_search", fake_for("dashscope"))
    monkeypatch.setattr(ai_tester, "query_dashscope_deepseek", fake_for("deepseek"))
    monkeypatch.setattr(ai_tester, "query_deepseek", fake_for("deepseek"))
    monkeypatch.setattr(ai_tester, "query_doubao_search", fake_for("doubao"))
    monkeypatch.setattr(ai_tester, "query_yuanbao", fake_for("yuanbao"))
    monkeypatch.setattr(ai_tester, "query_kimi_search", fake_for("kimi"))
    monkeypatch.setattr(
        ai_tester,
        "query_metaso_search",
        lambda *_args, **_kwargs: pytest.fail("新售默认批量入口不得调用秘塔"),
    )

    await ai_tester.batch_query_ai_engines("测试问题", "测试品牌")
    await ai_tester.check_longtail_keywords(["关键词一", "关键词二"], "测试品牌")

    assert set(DIAGNOSIS_ENGINES) == set(called)
    # batch(1 题) + longtail(2 词) = 每个引擎 3 次；两个入口共用同一默认矩阵
    assert all(called.count(engine) == 3 for engine in set(called))


@pytest.mark.asyncio
async def test_diagnosis_employee_uses_new_sale_matrix(monkeypatch):
    from employees.diagnosis.ai_tester import AITester
    from tools.ai_visibility import ai_tester

    captured = {}

    async def fake_detailed(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(ai_tester, "detailed_ai_visibility_test", fake_detailed)
    result = await AITester()._test_ai_visibility("岱林生物", "生物设备")

    assert result == {"ok": True}
    from config.ai_engines import DIAGNOSIS_ENGINES

    assert captured["engines"] == list(DIAGNOSIS_ENGINES)


def test_v2_platform_distribution_groups_yuanbao_aliases_and_omits_absent_kimi(monkeypatch):
    from services import report_evidence
    from services.report_writer_v2 import build_module_3_evidence

    evidences = [
        {
            "keyword": "问题一",
            "response_snippet": "岱林生物值得考虑。",
            "source_label": "yuanbao_hy3_tokenhub",
            "evidence_level": "A",
        },
        {
            "keyword": "问题二",
            "response_snippet": "岱林生物进入候选。",
            "source_label": "元宝",
            "evidence_level": "B",
        },
    ]
    monkeypatch.setattr(report_evidence, "extract_report_evidence", lambda **_kwargs: evidences)

    result = build_module_3_evidence({"brand_name": "岱林生物"}, brand_id=23)

    assert result["rendered_md"].count("| 元宝 |") == 1
    assert "| Kimi |" not in result["rendered_md"]
    assert result["platform_breakdown"]["元宝"] == 2
    assert "Kimi" not in result["platform_breakdown"]


def test_v2_platform_distribution_keeps_real_historical_kimi(monkeypatch):
    from services import report_evidence
    from services.report_writer_v2 import build_module_3_evidence

    evidences = [{
        "keyword": "历史问题",
        "response_snippet": "岱林生物曾进入候选。",
        "source_label": "Kimi",
        "evidence_level": "B",
    }]
    monkeypatch.setattr(report_evidence, "extract_report_evidence", lambda **_kwargs: evidences)

    result = build_module_3_evidence({"brand_name": "岱林生物"}, brand_id=23)

    assert result["rendered_md"].count("| Kimi |") == 1
    assert result["platform_breakdown"]["Kimi"] == 1
