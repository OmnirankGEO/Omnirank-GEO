"""Public GEO report backend contract regression tests.

These tests use synthetic report rows only. They never read production reports,
tokens, customer answers, or production databases.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from starlette.requests import Request


@pytest.mark.parametrize(
    "dangerous_url",
    [
        "javascript:alert(document.domain)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "//example.com/relative",
        "https://user:secret@example.com/private",
    ],
)
def test_customer_decision_evidence_rejects_non_http_url_protocols(dangerous_url):
    from services.report_html_renderer import _render_evidence_card_v2

    html = _render_evidence_card_v2(
        {
            "evidence_level": "A",
            "source_label": "测试来源",
            "search_citations": [
                {"url": dangerous_url, "title": "保留为不可点击的来源文字"}
            ],
        }
    )

    assert dangerous_url not in html
    assert "保留为不可点击的来源文字" in html
    assert "cdv2-ev-cite-disabled" in html
    assert '<a href=' not in html


def test_customer_decision_evidence_allows_normalized_http_and_https_urls():
    from services.report_html_renderer import _render_evidence_card_v2

    html = _render_evidence_card_v2(
        {
            "evidence_level": "A",
            "source_label": "测试来源",
            "search_citations": [
                {
                    "url": "HTTPS://Example.COM/path?q=1#proof",
                    "title": "可信来源",
                },
                {"url": "http://example.org/other", "title": "另一来源"},
            ],
        }
    )

    assert 'href="https://example.com/path?q=1#proof"' in html
    assert 'href="http://example.org/other"' in html
    assert html.count('rel="noopener noreferrer"') == 2


def _modules(*, summary_score=12, summary_level="隐形级") -> dict:
    score_module = {
        "summary": {
            "total_score": summary_score,
            "max_score": 100,
            "level": summary_level,
            "level_meta": {"summary": "旧摘要不应覆盖 canonical 等级"},
        },
        "funnel": {
            "total_score": summary_score,
            "level": summary_level,
            "level_meta": {"summary": "旧漏斗摘要"},
            "layers": [],
        },
        "dimensions": [],
        "layers": [],
        "keyword_strata": [],
    }
    client_modules = {
        "0": {"groups": []},
        "1": {"conclusion_text": "当前有基础，但多数新客户问题还没有稳定提及品牌。"},
        "2": score_module,
        "3": {"evidences": [], "evidence_count": {"A": 0, "B": 0, "C": 0}, "evidence_total": 0},
        "3_raw": {"tests": []},
        "3_authority": {"available": False},
        "4": {"top_competitors": [], "co_citation_total": 0},
        "5": {},
        "6": {"todos": [], "personalized_actions": []},
        "7": {},
        "8": {},
    }
    return {
        "funnel": {
            "total_score": 45,
            "level": "边缘级",
            "level_meta": {"summary": "AI 偶尔提到品牌"},
            "layers": [],
        },
        "client": {"modules": client_modules},
        "internal": {"modules": client_modules},
    }


def _row() -> dict:
    return {
        "id": 987654,
        "brand_id": 876543,
        "brand_owner_user_id": 765432,
        "brand_name": "超长测试品牌名称（仅隔离夹具）",
        "industry": "企业服务",
        "total_score": 12,
        "level": "隐形级",
        "keywords": "测试词一,测试词二",
        "created_at": datetime(2026, 7, 17, 10, 46, 47, 81661),
        "report_v2_version": "v2",
        "report_v2_client_md": "客户报告内容",
        "report_v2_modules_jsonb": _modules(),
        "report_v2_generated_at": datetime(2026, 7, 17, 10, 50),
        "report_v2_error": "INTERNAL_PROVIDER_ERROR secret detail",
        "data_completeness_score": 31,
        "data_completeness_breakdown": {
            "level": "资料偏少",
            "groups": [],
            "impact_notes": [],
            "missing_summary": "部分资料尚未补充",
        },
        "raw_data_json": {},
        "result_visibility": "published",
    }


def _canonical_meta() -> dict:
    from services.report_v2_score import build_canonical_report_meta

    return build_canonical_report_meta(_row())


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


class _Cursor:
    def __init__(self, row: dict):
        self.row = row

    def execute(self, _sql, _params=None):
        return None

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row: dict):
        self.row = row

    def cursor(self):
        return _Cursor(self.row)

    def close(self):
        return None


def test_canonical_meta_prefers_v2_funnel_and_formats_public_date():
    meta = _canonical_meta()
    assert meta["total_score"] == 45
    assert meta["level"] == "边缘级"
    assert meta["score_source"] == "v2_funnel"
    assert meta["created_at"] == "2026年7月17日"


def test_public_report_token_gate_only_exact_false_disables(monkeypatch):
    import api.share_api as share_api

    monkeypatch.setattr(
        share_api,
        "get_connection",
        lambda: (_ for _ in ()).throw(AssertionError("no-token branch must not query DB")),
    )

    monkeypatch.delenv("PUBLIC_REPORT_REQUIRE_TOKEN", raising=False)
    assert share_api._verify_public_report_token(987654, None) is False

    for value in ("true", "TRUE", "1", "yes", "typo", "0", "off", "", " false ", "FALSE"):
        monkeypatch.setenv("PUBLIC_REPORT_REQUIRE_TOKEN", value)
        assert share_api._verify_public_report_token(987654, None) is False, value

    monkeypatch.setenv("PUBLIC_REPORT_REQUIRE_TOKEN", "false")
    assert share_api._verify_public_report_token(987654, None) is True


def test_public_report_route_rejects_no_token_under_misconfigured_values(monkeypatch):
    import api.share_api as share_api

    monkeypatch.setattr(
        share_api,
        "get_connection",
        lambda: (_ for _ in ()).throw(AssertionError("route must reject before DB access")),
    )

    for value in ("1", "yes", "typo"):
        monkeypatch.setenv("PUBLIC_REPORT_REQUIRE_TOKEN", value)
        try:
            asyncio.run(share_api.get_public_report(987654, st=None))
        except share_api.HTTPException as exc:
            assert exc.status_code == 404
            assert "链接已失效" in str(exc.detail)
        else:
            raise AssertionError(f"{value!r} unexpectedly disabled the public report token gate")


def test_customer_decision_html_uses_canonical_score_and_hides_internal_ids():
    from services.report_html_renderer import render_customer_decision_page_html

    meta = {
        **_canonical_meta(),
        "diagnosis_id": 987654,
        "id": 987654,
        "share_token": "synthetic-secret-token",
    }
    html = render_customer_decision_page_html(
        meta=meta,
        modules_jsonb=_modules(summary_score=12, summary_level="隐形级"),
        completeness={"score": 31, "level": "资料偏少", "groups": []},
        snapshot_id="snap_diag987654_v1",
    )

    assert "<title>超长测试品牌名称（仅隔离夹具） · 边缘级 · GEO 诊断报告</title>" in html
    assert '<span class="cdv2-score-num">45</span>' in html
    assert "边缘级" in html
    assert "资料完整度 31/100" in html
    assert "不是 GEO 综合评分" in html
    assert "起步级" not in html
    assert "2026年7月17日" in html
    assert "2026-07-17T10:46:47" not in html
    assert "Evidence A/B/C" not in html
    assert "报告编号" not in html
    assert "诊断 ID" not in html
    assert "987654" not in html
    assert "synthetic-secret-token" not in html
    assert "snap_diag" not in html

    forbidden = (
        "owner_user_id",
        "agent_user_id",
        "upstream_user_id",
        "resolved_user_id",
        "service_account_code",
        "channel_account_code",
        "relationship_version",
        "cost_multiplier",
        "秘塔代理",
    )
    assert not [term for term in forbidden if term in html]


def test_customer_decision_hides_unversioned_industry_comparison():
    from services.report_html_renderer import (
        _radar_chart_svg,
        render_customer_decision_page_html,
    )

    payload = _modules()
    dims = [
        {"label": "品牌基础", "score": 12, "max": 20, "percent": 60, "status": "成长"},
        {"label": "AI 推荐率", "score": 8, "max": 20, "percent": 40, "status": "待提升"},
        {"label": "权威背书", "score": 6, "max": 20, "percent": 30, "status": "待提升"},
    ]
    payload["client"]["modules"]["2"]["dimensions"] = dims
    html = render_customer_decision_page_html(
        meta={**_canonical_meta(), "industry_avg": 45, "industry_best": 82},
        modules_jsonb=payload,
        completeness={"score": 31, "groups": []},
    )
    radar = _radar_chart_svg(dims, size=340)

    assert "暂无行业基准" in html
    assert "行业平均" not in html
    assert "行业最佳" not in html
    assert 'stroke="#F59E0B"' not in radar
    assert 'stroke="#10B981"' not in radar


def test_competitor_without_real_denominator_shows_count_only():
    from services.report_html_renderer import render_customer_decision_page_html

    payload = _modules()
    payload["client"]["modules"]["4"] = {
        "top_competitors": [["真实长名称引用源", 3]],
        "co_citation_total": 0,
    }
    html = render_customer_decision_page_html(
        meta=_canonical_meta(),
        modules_jsonb=payload,
        completeness={"score": 31, "groups": []},
    )

    assert "真实长名称引用源" in html
    assert "3 次" in html
    assert "数据不足" in html
    assert "3 / 12" not in html
    assert "高频引用" not in html
    assert "中频引用" not in html


def test_competitor_inconsistent_count_does_not_calculate_frequency():
    from services.report_html_renderer import render_customer_decision_page_html

    payload = _modules()
    payload["client"]["modules"]["4"] = {
        "top_competitors": [["计数大于样本的异常引用源", 9], ["无效计数", True]],
        "co_citation_total": 3,
    }
    html = render_customer_decision_page_html(
        meta=_canonical_meta(),
        modules_jsonb=payload,
        completeness={"score": 31, "groups": []},
    )

    assert "9 次" in html
    assert "9 / 3" not in html
    assert "无效计数" in html
    assert ">数据不足</td>" in html


def test_competitor_boolean_denominator_is_not_treated_as_one():
    from services.report_html_renderer import render_customer_decision_page_html

    payload = _modules()
    payload["client"]["modules"]["4"] = {
        "top_competitors": [["布尔分母引用源", 1]],
        "co_citation_total": True,
    }
    html = render_customer_decision_page_html(
        meta=_canonical_meta(),
        modules_jsonb=payload,
        completeness={"score": 31, "groups": []},
    )

    assert "1 次" in html
    assert "1 / 1" not in html


def test_engine_scope_is_derived_from_this_report_results():
    from services.report_html_renderer import render_customer_decision_page_html

    payload = _modules()
    payload["client"]["modules"]["3_raw"]["tests"] = [
        {
            "question": "测试问题",
            "results": [
                {"engine": "deepseek", "engine_label": "DeepSeek", "brand_detected": True},
                {"engine": "kimi", "engine_label": "Kimi", "brand_detected": False},
                {"engine": "deepseek", "engine_label": "DeepSeek", "brand_detected": True},
            ],
        }
    ]
    html = render_customer_decision_page_html(
        meta=_canonical_meta(),
        modules_jsonb=payload,
        completeness={"score": 31, "groups": []},
    )

    assert "本次测试：DeepSeek、Kimi" in html
    assert "本次测试覆盖：DeepSeek、Kimi" in html
    assert "4 大" not in html
    assert "四大" not in html


def test_engine_scope_falls_back_to_generic_when_results_are_missing():
    from services.report_html_renderer import render_customer_decision_page_html

    html = render_customer_decision_page_html(
        meta=_canonical_meta(),
        modules_jsonb=_modules(),
        completeness={"score": 31, "groups": []},
    )

    assert "本次 AI 实测" in html
    assert "4 大" not in html
    assert "四大" not in html


def test_derived_engine_scope_is_html_escaped():
    from services.report_html_renderer import render_customer_decision_page_html

    payload = _modules()
    payload["client"]["modules"]["3_raw"]["tests"] = [
        {
            "question": "测试问题",
            "results": [
                {
                    "engine": "new-engine",
                    "engine_label": '新引擎\"><script>window.bad=1</script>',
                    "brand_detected": False,
                }
            ],
        }
    ]
    html = render_customer_decision_page_html(
        meta=_canonical_meta(),
        modules_jsonb=payload,
        completeness={"score": 31, "groups": []},
    )

    assert "<script>window.bad=1</script>" not in html
    assert "新引擎&quot;&gt;&lt;script&gt;window.bad=1&lt;/script&gt;" in html


def test_missing_canonical_score_is_not_rendered_as_zero():
    from services.report_html_renderer import (
        render_customer_decision_page_html,
        render_report_html,
    )

    meta = {
        "brand_name": "空数据测试品牌",
        "total_score": None,
        "level": None,
        "industry": "企业服务",
        "city": "",
        "created_at": "2026年7月17日",
    }
    modules = _modules(summary_score=0, summary_level="隐形级")
    decision_html = render_customer_decision_page_html(
        meta=meta,
        modules_jsonb=modules,
        completeness={"score": None, "groups": []},
    )
    print_html = render_report_html(
        meta=meta,
        modules_jsonb=modules,
        completeness={"score": None, "groups": []},
        audience="client",
    )

    assert '<span class="cdv2-score-num">—</span>' in decision_html
    assert "数据不足" in decision_html
    assert '<div class="cover-score-num">—</div>' in print_html
    assert '<div class="funnel-score-num"' in print_html and ">—</div>" in print_html


def test_print_renderer_uses_same_canonical_score_and_level_as_web():
    from services.report_html_renderer import render_report_html

    html = render_report_html(
        meta=_canonical_meta(),
        modules_jsonb=_modules(summary_score=12, summary_level="隐形级"),
        completeness={"score": 31, "level": "资料偏少", "groups": []},
        audience="client",
    )
    assert "· 边缘级 · GEO 诊断报告 2.0</title>" in html
    assert '<div class="cover-score-num">45</div>' in html
    assert "综合评分 <strong>45/100</strong>" in html
    assert "起步级" not in html


def test_public_json_dto_uses_canonical_score_and_omits_internal_fields(monkeypatch):
    import api.share_api as share_api
    import services.public_whitelabel as public_whitelabel
    import os

    row = _row()
    monkeypatch.setattr(share_api, "_verify_public_report_token", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(share_api, "get_connection", lambda: _Connection(row))
    monkeypatch.setattr(os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        public_whitelabel,
        "get_public_whitelabel_data",
        lambda **_kwargs: {"whitelabel": None, "display_scope": "platform"},
    )

    payload = asyncio.run(share_api.get_public_report(987654, include_html=1, st="synthetic-token"))
    report = payload["report"]
    encoded = json.dumps(report, ensure_ascii=False, default=str)
    assert report["score"] == 45
    assert report["level"] == "边缘级"
    assert "id" not in report
    assert "score_source" not in report
    assert "report_v2_error" not in report
    assert report["has_html"] is False
    assert report["html_content"] == ""
    assert report["presentation"]["contractVersion"] == "public-report-presentation/v1"
    assert report["presentation"]["identity"]["industry"] == "企业服务"
    forbidden = (
        "brand_owner_user_id",
        "brand_id",
        "agent_user_id",
        "upstream_user_id",
        "resolved_user_id",
        "service_account_code",
        "channel_account_code",
        "relationship_version",
        "cost_multiplier",
        "INTERNAL_PROVIDER_ERROR",
    )
    assert not [term for term in forbidden if term in encoded]


def test_public_presentation_uses_customer_evidence_without_inventing_recommendations():
    from services.public_report_presentation import build_public_report_presentation

    modules = {
        "funnel": {
            "total_score": 45,
            "level_meta": {"partial_sample": True, "level_capped": False},
            "layers": [
                {
                    "key": "brand",
                    "label": "品牌认知层",
                    "weight": 20,
                    "detected": 2,
                    "total": 2,
                    "rate_pct": 100,
                    "score": 25,
                    "data_sufficient": False,
                    "confidence": "low",
                },
                {
                    "key": "scenario",
                    "label": "场景转化层",
                    "weight": 40,
                    "detected": 0,
                    "total": 0,
                    "rate_pct": 0,
                    "score": 0,
                    "data_sufficient": False,
                    "confidence": "low",
                },
            ],
        },
        "client": {
            "modules": {
                "1": {"conclusion_text": "品牌已经被识别，但推荐证据仍需补齐。"},
                "1_interpretation": {
                    "headline": "品牌已进入部分回答，下一步应核对推荐依据。",
                    "business_translation": "提到品牌不等于明确推荐。",
                    "data_caveat": "部分层样本偏少。",
                },
                "3": {"evidence_total": 2, "evidence_shortfall": 3},
                "3_raw": {
                    "tests": [
                        {
                            "question": "企业知识库哪家值得推荐？",
                            "results": {
                                "metaso": {
                                    "platform_key": "deepseek_metaso_proxy",
                                    "status": "answered",
                                    "brand_detected": True,
                                    "target_outcome": "recommended",
                                    "full_response": "明确推荐测试品牌，并说明了依据。",
                                    "search_citations": [
                                        {"url": "https://Evidence.EXAMPLE.com/proof"},
                                        {"url": "javascript:alert(1)"},
                                    ],
                                }
                            },
                        },
                        {
                            "question": "测试品牌是做什么的？",
                            "results": {
                                "deepseek": {
                                    "platform_key": "deepseek_metaso_proxy",
                                    "status": "answered",
                                    "brand_detected": True,
                                    "full_response": "回答中提到了测试品牌，但没有做推荐。",
                                }
                            },
                        },
                        {
                            "question": "企业知识库有哪些候选？",
                            "results": {
                                "qwen": {
                                    "platform_key": "qwen_dashscope_search",
                                    "status": "answered",
                                    "brand_detected": True,
                                    "target_outcome": "candidate_only",
                                    "full_response": "测试品牌被列为候选之一。",
                                    "search_citations": [],
                                }
                            },
                        },
                    ],
                    "custom_tests": [
                        {
                            "question": "客户购买的原始短句是否被提到？",
                            "results": [
                                {
                                    "engine": "doubao",
                                    "status": "answered",
                                    "brand_detected": True,
                                    "full_response": "客户品牌在原始短句对应回答中被提到。",
                                    "tested_at": "not-a-date",
                                },
                                {
                                    "engine": "kimi",
                                    "engine_label": "Kimi",
                                    "status": "未提到品牌",
                                    "brand_detected": False,
                                    "target_outcome": "recommended",
                                    "full_response": "本引擎未返回可展示回答。",
                                },
                                {
                                    "engine": "yuanbao",
                                    "engine_label": "元宝",
                                    "status": "查询失败",
                                    "brand_detected": False,
                                    "target_outcome": "recommended",
                                    "full_response": "查询失败: C:\\private\\provider-token-sentinel",
                                    "search_citations": [{"url": "https://internal.example/trace"}],
                                }
                            ],
                        }
                    ],
                },
                "3_competition": {
                    "top_brands": [{"name": "真实竞品甲", "count": 2}],
                    "client_detected_count": 3,
                    "valid_total": 3,
                },
                "4": {"top_competitors": [{"name": "引用网页标题不应当竞品"}]},
                "6": {
                    "todos": [
                        {"priority": "P0", "issue": "推荐依据不足", "action": "补齐可核验案例"}
                    ]
                },
            }
        },
        "internal": {"modules": {"1": {"conclusion_text": "INTERNAL_MARGIN_SENTINEL"}}},
    }

    presentation = build_public_report_presentation(
        modules,
        industry="企业服务",
        canonical_score=45,
        generated_at=datetime(2026, 7, 19, 12, 0),
        keyword_count=3,
    )
    encoded = json.dumps(presentation, ensure_ascii=False)

    assert presentation["funnel"]["data"]["layers"][1]["total"] is None
    assert presentation["funnel"]["data"]["layers"][1]["ratePct"] is None
    deepseek = next(row for row in presentation["platforms"]["data"] if row["platformName"] == "DeepSeek")
    assert deepseek["mentionRatePct"] == 100
    assert deepseek["recommendRatePct"] == 50
    assert deepseek["citationCount"] is None
    qwen = next(row for row in presentation["platforms"]["data"] if row["platformName"] == "通义千问")
    assert qwen["citationCount"] == 0
    kimi = next(row for row in presentation["platforms"]["data"] if row["platformName"] == "Kimi")
    assert kimi["validSamples"] == 0
    assert kimi["detectionRatePct"] is None
    assert kimi["citationCount"] is None
    verdicts = [item["verdict"] for item in presentation["evidence"]["data"]["items"]]
    assert verdicts == ["recommended", "mentioned", "candidate", "mentioned", "no_answer", "engine_error"]
    assert presentation["evidence"]["data"]["items"][-3]["question"] == "客户购买的原始短句是否被提到？"
    assert presentation["evidence"]["data"]["items"][-3]["testedAt"] is None
    assert presentation["summary"]["validAnswerCount"] == 4
    assert presentation["evidence"]["data"]["items"][0]["citedDomains"] == ["evidence.example.com"]
    assert presentation["evidence"]["data"]["items"][-1]["answerExcerpt"] is None
    assert presentation["evidence"]["data"]["items"][-1]["citedDomains"] == []
    assert "provider-token-sentinel" not in encoded
    assert "internal.example" not in encoded
    assert presentation["competitive"]["data"]["competitors"] == [
        {"name": "真实竞品甲", "mentionCount": 2, "recommendCount": None}
    ]
    assert presentation["actions"]["data"][0]["priorityLabel"] == "优先处理"
    assert presentation["thirtyDayPlan"]["status"] == "unavailable"
    assert "INTERNAL_MARGIN_SENTINEL" not in encoded
    assert "引用网页标题不应当竞品" not in encoded
    assert "metaso" not in encoded.lower()


def test_public_presentation_matches_current_raw_module_producer_shape():
    from services.public_report_presentation import build_public_report_presentation
    from services.report_writer_v2 import build_module_3_raw_ai_appendix

    raw_module = build_module_3_raw_ai_appendix({
        "diagnosis_data": {
            "ai_visibility_data": {
                "engines_tested": ["deepseek", "kimi"],
                "detail_table": [
                    {
                        "question": "客户购买的原始问题",
                        "results": {
                            "deepseek": {
                                "brand_detected": True,
                                "full_response": "回答中真实提到了客户品牌。",
                                "search_citations": [],
                            }
                        },
                    }
                ],
            }
        }
    })
    presentation = build_public_report_presentation(
        {
            "funnel": {
                "total_score": 45,
                "layers": [{"key": "brand", "label": "品牌认知层", "detected": 1, "total": 1}],
            },
            "client": {
                "modules": {
                    "1": {"conclusion_text": "本次形成一条有效回答。"},
                    "3_raw": raw_module,
                }
            },
        },
        industry="企业服务",
        canonical_score=45,
        generated_at=datetime(2026, 7, 19, 12, 0),
        keyword_count=1,
    )

    rows = {row["platformName"]: row for row in presentation["platforms"]["data"]}
    assert presentation["summary"]["validAnswerCount"] == 1
    assert rows["DeepSeek"]["validSamples"] == 1
    # The client artifact now preserves an explicitly observed empty citation
    # array, so measured zero stays distinct from an uncollected field.
    assert rows["DeepSeek"]["citationCount"] == 0
    assert rows["Kimi"]["validSamples"] == 0
    assert rows["Kimi"]["detectionRatePct"] is None
    evidence = presentation["evidence"]["data"]["items"]
    assert [item["verdict"] for item in evidence] == ["mentioned", "no_answer"]


def test_public_presentation_derives_actions_for_legacy_empty_module_six():
    from services.public_report_presentation import build_public_report_presentation

    modules = _modules()
    modules["client"]["modules"]["2"]["funnel"]["layers"] = [
        {"key": "brand", "detected": 1, "total": 2, "rate": 0.5},
        {"key": "local", "detected": 0, "total": 3, "rate": 0.0},
        {"key": "scenario", "detected": 0, "total": 0, "rate": 0.0},
    ]

    presentation = build_public_report_presentation(
        modules,
        industry="企业服务",
        canonical_score=45,
        generated_at=datetime(2026, 7, 20, 12, 0),
        keyword_count=3,
    )

    assert presentation["actions"]["status"] == "ready"
    actions = presentation["actions"]["data"]
    assert [item["priorityLabel"] for item in actions] == ["优先处理", "优先处理", "接着处理"]
    assert actions[0]["impactScope"] == "决策获客"
    assert "0 个" in actions[0]["why"]
    assert actions[1]["impactScope"] == "场景转化"
    assert "收益" not in json.dumps(actions, ensure_ascii=False)


def test_shape_report_data_derives_structured_actions_from_real_funnel():
    from services.diagnosis_report_v2 import _shape_report_data

    shaped = _shape_report_data(
        {
            "data": {
                "ai_visibility": {
                    "dimension_stats": {
                        "brand_awareness": {"detected": 1, "total": 2},
                        "regional_industry": {"detected": 0, "total": 3},
                        "super_tier1": {"detected": 1, "total": 3},
                    }
                },
                "action_plan": {"action_plan_md": "旧 Markdown 不能作为结构化行动"},
            }
        },
        {"dimension_scores": {}, "total_score": 0},
        {"name": "测试品牌", "industry": "企业服务"},
        {},
        {"score": 50},
    )

    suggestions = shaped["suggestions"]
    assert suggestions["high_priority"][0]["weak_dimension"] == "决策获客"
    assert suggestions["medium_priority"]
    assert all(item.get("evidence_basis") for group in suggestions.values() for item in group)


def test_gate_configuration_failure_defaults_to_customer_decision_not_legacy(monkeypatch):
    import api.share_api as share_api
    import config.settings_manager as settings_manager
    import services.public_whitelabel as public_whitelabel

    row = _row()
    monkeypatch.setattr(share_api, "_verify_public_report_token", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(share_api, "get_connection", lambda: _Connection(row))
    monkeypatch.setattr(
        public_whitelabel,
        "get_public_whitelabel_data",
        lambda **_kwargs: {"whitelabel": None, "display_scope": "platform"},
    )
    monkeypatch.setattr(
        settings_manager,
        "load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic config failure")),
    )

    response = asyncio.run(
        share_api.get_public_report_v2_html(
            987654,
            _request("/api/public/report/987654/v2.html"),
            st="synthetic-token",
        )
    )
    html = response.body.decode("utf-8")
    assert response.status_code == 200
    assert 'class="cdv2-topbar"' in html
    assert 'class="report"' not in html


def test_v2_route_does_not_inject_placeholder_industry_benchmarks(monkeypatch):
    import api.share_api as share_api
    import config.settings_manager as settings_manager
    import services.public_whitelabel as public_whitelabel
    import services.report_html_renderer as report_html_renderer

    captured = {}
    monkeypatch.setattr(share_api, "_verify_public_report_token", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(share_api, "get_connection", lambda: _Connection(_row()))
    monkeypatch.setattr(
        public_whitelabel,
        "get_public_whitelabel_data",
        lambda **_kwargs: {"whitelabel": None, "display_scope": "platform"},
    )
    monkeypatch.setattr(
        settings_manager,
        "load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic config failure")),
    )

    def _capture_renderer(**kwargs):
        captured.update(kwargs["meta"])
        return "<!doctype html><html><body>customer-decision</body></html>"

    monkeypatch.setattr(
        report_html_renderer,
        "render_customer_decision_page_html",
        _capture_renderer,
    )

    response = asyncio.run(
        share_api.get_public_report_v2_html(
            987654,
            _request("/api/public/report/987654/v2.html"),
            st="synthetic-token",
        )
    )
    assert response.status_code == 200
    assert response.body
    assert "industry_avg" not in captured
    assert "industry_best" not in captured


def test_customer_decision_render_failure_returns_clear_503_without_legacy(monkeypatch):
    import api.share_api as share_api
    import config.settings_manager as settings_manager
    import services.public_whitelabel as public_whitelabel
    import services.report_html_renderer as report_html_renderer

    row = _row()
    monkeypatch.setattr(share_api, "_verify_public_report_token", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(share_api, "get_connection", lambda: _Connection(row))
    monkeypatch.setattr(
        public_whitelabel,
        "get_public_whitelabel_data",
        lambda **_kwargs: {"whitelabel": None, "display_scope": "platform"},
    )
    monkeypatch.setattr(
        settings_manager,
        "load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic config failure")),
    )
    monkeypatch.setattr(
        report_html_renderer,
        "render_customer_decision_page_html",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic render failure")),
    )

    response = asyncio.run(
        share_api.get_public_report_v2_html(
            987654,
            _request("/api/public/report/987654/v2.html"),
            st="synthetic-token",
        )
    )
    html = response.body.decode("utf-8")
    assert response.status_code == 503
    assert "报告生成暂时遇到问题" in html
    assert 'class="report"' not in html
    assert 'class="cdv2-topbar"' not in html


def test_v2_public_renderers_never_fallback_to_internal_modules():
    from services.report_html_renderer import (
        render_customer_decision_page_html,
        render_report_html,
    )

    sentinel = "INTERNAL_ONLY_MARGIN_37_PERCENT"
    internal_only = {
        "internal": {
            "modules": {
                "1": {"conclusion_text": sentinel, "rendered_md": sentinel},
                "6": {"todos": [sentinel]},
            }
        }
    }
    kwargs = {
        "meta": _canonical_meta(),
        "modules_jsonb": internal_only,
        "completeness": {"score": 31, "level": "资料偏少", "groups": []},
        "audience": "client",
    }

    customer_html = render_customer_decision_page_html(**kwargs)
    print_html = render_report_html(**kwargs)

    for rendered in (customer_html, print_html):
        assert "客户报告尚未就绪" in rendered
        assert "本次客户版数据不足" in rendered
        assert sentinel not in rendered


def test_v3_public_renderer_never_fallbacks_to_internal_modules():
    from services.report_html_renderer_v3 import render_report_v3_html

    sentinel = "INTERNAL_ONLY_MARGIN_37_PERCENT"
    html = render_report_v3_html(
        meta=_canonical_meta(),
        modules_jsonb={
            "internal": {
                "modules": {
                    "1": {"insight": sentinel, "rendered_md": sentinel},
                    "executive_summary": {"summary": sentinel},
                }
            }
        },
    )

    assert "客户报告尚未就绪" in html
    assert "本次客户版数据不足" in html
    assert sentinel not in html


@pytest.mark.parametrize(
    ("client_modules", "expected_ready"),
    [
        ({"3_raw": {"tests": [{"answer": "raw-only"}]}}, False),
        ({"rich_narrative": {"executive_summary": "legacy-only"}}, False),
        ({"1": {}}, False),
        ({"1": {"rendered_md": "REAL_CUSTOMER_MODULE_ONE"}}, True),
    ],
)
def test_all_customer_renderers_share_strict_module_one_readiness(
    client_modules, expected_ready
):
    from services.report_html_renderer import (
        is_client_report_ready,
        render_customer_decision_page_html,
        render_report_html,
    )
    from services.report_html_renderer_v3 import render_report_v3_html

    modules_jsonb = {"client": {"modules": client_modules}}
    kwargs = {
        "meta": _canonical_meta(),
        "modules_jsonb": modules_jsonb,
        "completeness": {"score": 31, "level": "资料偏少", "groups": []},
        "audience": "client",
    }
    rendered = (
        render_customer_decision_page_html(**kwargs),
        render_report_html(**kwargs),
        render_report_v3_html(meta=_canonical_meta(), modules_jsonb=modules_jsonb),
    )

    assert is_client_report_ready(modules_jsonb) is expected_ready
    for html in rendered:
        assert ("客户报告尚未就绪" not in html) is expected_ready


def test_public_html_route_rejects_internal_only_report_before_raw_augmentation(monkeypatch):
    import api.share_api as share_api

    sentinel = "INTERNAL_ONLY_MARGIN_37_PERCENT"
    row = _row()
    row["report_v2_modules_jsonb"] = {
        "internal": {
            "modules": {
                "1": {"conclusion_text": sentinel, "rendered_md": sentinel},
            }
        },
        "funnel": {"total_score": 45, "level": "边缘级"},
    }
    # If augmentation ran first, this payload could create client.modules and
    # mask the missing customer artifact.
    row["raw_data_json"] = {
        "data": {"ai_visibility": {"test_results": [{"answer": "safe evidence"}]}}
    }
    monkeypatch.setattr(share_api, "_verify_public_report_token", lambda *_a, **_k: True)
    monkeypatch.setattr(share_api, "get_connection", lambda: _Connection(row))

    response = asyncio.run(
        share_api.get_public_report_v2_html(
            987654,
            _request("/api/public/report/987654/v2.html"),
            st="synthetic-token",
        )
    )
    html = response.body.decode("utf-8")

    assert response.status_code == 503
    assert "客户报告尚未就绪" in html
    assert 'data-report-status="not_ready"' in html
    assert 'data-error-code="CLIENT_REPORT_NOT_READY"' in html
    assert sentinel not in html


def test_v3_empty_findings_are_data_state_not_fixed_brand_conclusions():
    from services.report_html_renderer_v3 import render_report_v3_html

    html = render_report_v3_html(
        meta=_canonical_meta(),
        modules_jsonb={
            "client": {
                "modules": {
                    "1": {"rendered_md": "本节只有可核验的诊断正文。"},
                    "executive_summary": {"key_findings": []},
                }
            }
        },
    )

    assert "证据不足，暂无可核验结论" in html
    assert "引用链路需要补强" not in html
    assert "高意图内容需要结构化" not in html
    assert "客户案例证据需要补齐" not in html


def test_v3_real_numeric_conclusion_is_not_overridden_by_stale_fallback():
    from services.report_html_renderer_v3 import render_report_v3_html

    real_conclusion = "真实诊断结论：品牌词覆盖稳定，场景词仍需补充证据。"
    stale_claim = "STALE_SYNTHETIC_CLAIM"
    html = render_report_v3_html(
        meta=_canonical_meta(),
        modules_jsonb={
            "client": {
                "modules": {
                    "1": {
                        "insight": real_conclusion,
                        "rendered_md": f"## 1 分钟结论\n\n{real_conclusion}",
                    },
                    "rich_narrative": {
                        "version": "v3_fallback_2026_05_13",
                        "executive_summary": "证据不足，暂无可核验结论。",
                        "key_findings": [stale_claim],
                        "sections": {},
                    },
                }
            }
        },
    )

    assert real_conclusion in html
    assert "证据不足，暂无可核验结论" not in html
    assert stale_claim not in html


def test_v2_score_level_is_derived_from_funnel_ssot():
    from services.report_v2_score import resolve_canonical_score

    result = resolve_canonical_score(
        {
            "report_v2_version": "v2",
            "report_v2_modules_jsonb": {
                "funnel": {"total_score": 45, "level": "主导级"}
            },
            "total_score": 45,
            "level": "主导级",
        }
    )

    assert result == {
        "total_score": 45,
        "level": "边缘级",
        "source": "v2_funnel",
    }


def test_v2_out_of_range_funnel_score_fails_closed_without_v1_fallback():
    from services.report_v2_score import resolve_canonical_score

    result = resolve_canonical_score(
        {
            "report_v2_version": "v2",
            "report_v2_modules_jsonb": {
                "funnel": {"total_score": 999, "level": "主导级"}
            },
            "total_score": 45,
            "level": "主导级",
        }
    )

    assert result == {"total_score": None, "level": None, "source": "missing"}


def test_v1_score_level_is_derived_from_legacy_ssot():
    from services.report_v2_score import resolve_canonical_score

    result = resolve_canonical_score(
        {"report_v2_version": "v1", "total_score": 45, "level": "主导级"}
    )

    assert result == {
        "total_score": 45,
        "level": "起步",
        "source": "v1_column",
    }


def test_invalid_score_shapes_fail_closed_instead_of_truncating_or_clamping():
    from services.report_v2_score import resolve_canonical_score

    for invalid in (-1, 101, 45.5, True, "NaN", "not-a-score"):
        result = resolve_canonical_score(
            {"report_v2_version": "v1", "total_score": invalid, "level": "主导级"}
        )
        assert result == {"total_score": None, "level": None, "source": "missing"}


def test_public_v2_json_does_not_fallback_to_legacy_markdown_when_client_md_missing(monkeypatch):
    import os

    import api.share_api as share_api
    import services.public_whitelabel as public_whitelabel

    row = _row()
    row["report_v2_client_md"] = ""
    row["report_md_path"] = "output/reports/internal-only.md"
    monkeypatch.setattr(share_api, "_verify_public_report_token", lambda *_a, **_k: True)
    monkeypatch.setattr(share_api, "get_connection", lambda: _Connection(row))
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("V2 public JSON must not inspect a legacy markdown file")
        ),
    )
    monkeypatch.setattr(
        public_whitelabel,
        "get_public_whitelabel_data",
        lambda **_kwargs: {"whitelabel": None, "display_scope": "platform"},
    )

    payload = asyncio.run(
        share_api.get_public_report(987654, include_html=1, st="synthetic-token")
    )

    assert payload["report"]["report_version"] == "v2"
    assert payload["report"]["content"] == ""
    assert payload["report"]["html_content"] == ""


def test_public_v2_json_inlines_only_admin_approved_whitelabel(monkeypatch):
    import api.share_api as share_api
    import services.public_whitelabel as public_whitelabel

    row = _row()
    seen = {}
    approved_brand = {
        "company_name": "远山增长顾问",
        "product_name": "远山 GEO",
        "logo_url": "https://cdn.example.com/opaque/logo.png",
        "slogan": "让证据说话",
        "brand_color": "#075985",
    }

    def _approved_lookup(**kwargs):
        seen.update(kwargs)
        return {"whitelabel": approved_brand, "display_scope": "approved_whitelabel"}

    monkeypatch.setattr(share_api, "_verify_public_report_token", lambda *_a, **_k: True)
    monkeypatch.setattr(share_api, "get_connection", lambda: _Connection(row))
    monkeypatch.setattr(public_whitelabel, "get_public_whitelabel_data", _approved_lookup)

    payload = asyncio.run(
        share_api.get_public_report(987654, include_html=0, st="synthetic-token")
    )

    assert seen == {
        "brand_owner_user_id": row["brand_owner_user_id"],
        "brand_id": row["brand_id"],
    }
    assert payload["report"]["branding_status"] == "approved_whitelabel"
    assert payload["report"]["whitelabel"] == approved_brand
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "brand_owner_user_id" not in encoded
    assert "owner_user_id" not in encoded


def test_public_v2_json_returns_structured_not_ready_without_partial_report(monkeypatch):
    import api.share_api as share_api
    import services.public_whitelabel as public_whitelabel

    sentinel = "INTERNAL_ONLY_MARGIN_37_PERCENT"
    row = _row()
    row["report_v2_modules_jsonb"] = {
        "internal": {"modules": {"1": {"rendered_md": sentinel}}},
        "funnel": {"total_score": 45, "level": "边缘级"},
    }
    monkeypatch.setattr(share_api, "_verify_public_report_token", lambda *_a, **_k: True)
    monkeypatch.setattr(share_api, "get_connection", lambda: _Connection(row))
    monkeypatch.setattr(
        public_whitelabel,
        "get_public_whitelabel_data",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("not-ready response must not resolve or expose branding")
        ),
    )

    payload = asyncio.run(
        share_api.get_public_report(987654, include_html=1, st="synthetic-token")
    )
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload == {
        "status": "not_ready",
        "not_ready": True,
        "code": "CLIENT_REPORT_NOT_READY",
        "message": "客户报告尚未就绪，本次客户版数据不足。请稍后刷新，或联系发给你链接的人。",
        "detail": {
            "code": "CLIENT_REPORT_NOT_READY",
            "message": "客户报告尚未就绪，本次客户版数据不足。请稍后刷新，或联系发给你链接的人。",
        },
    }
    assert "report" not in payload
    assert "total_score" not in encoded
    assert "level" not in encoded
    assert "report_version" not in encoded
    assert sentinel not in encoded
