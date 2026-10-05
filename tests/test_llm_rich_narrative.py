import asyncio
import json


def test_build_fallback_narrative_preserves_only_existing_customer_evidence():
    from services.llm_rich_narrative import build_fallback_rich_narrative

    narrative = build_fallback_rich_narrative(
        {
            "executive_summary": {
                "summary": "品牌基础不错，但内容证据不足。",
                "key_findings": ["AI 引用不足", "转化路径不清晰"],
            }
        }
    )

    assert narrative["executive_summary"] == "品牌基础不错，但内容证据不足。"
    assert narrative["key_findings"] == ["AI 引用不足", "转化路径不清晰"]
    assert narrative["sections"] == {}


def test_build_fallback_narrative_does_not_invent_findings_without_evidence():
    from services.llm_rich_narrative import build_fallback_rich_narrative

    narrative = build_fallback_rich_narrative({})

    assert narrative is None


def test_build_fallback_narrative_reads_real_numeric_module_one():
    from services.llm_rich_narrative import build_fallback_rich_narrative

    real_conclusion = "真实诊断结论：品牌词已有覆盖，场景词证据仍需复核。"
    narrative = build_fallback_rich_narrative(
        {"1": {"insight": real_conclusion, "rendered_md": f"## 1 分钟结论\n\n{real_conclusion}"}}
    )

    assert narrative is not None
    assert narrative["source_module"] == "1"
    assert narrative["executive_summary"] == real_conclusion
    assert narrative["key_findings"] == []
    assert "证据不足，暂无可核验结论" not in str(narrative)


def test_numeric_module_one_overrides_stale_executive_summary_alias():
    from services.llm_rich_narrative import build_fallback_rich_narrative
    from services.report_html_renderer_v3 import render_report_v3_html

    stale = "STALE_EXECUTIVE_ALIAS"
    real = "REAL_NUMERIC_MODULE_CONCLUSION"
    modules_jsonb = {
        "client": {
            "modules": {
                "executive_summary": {"summary": stale, "key_findings": [stale]},
                "1": {"insight": real, "rendered_md": real},
                "rich_narrative": {
                    "version": "v3_evidence_only_2026_07_19_r2",
                    "source_module": "executive_summary",
                    "executive_summary": stale,
                    "key_findings": [stale],
                    "sections": {},
                },
            }
        }
    }

    narrative = build_fallback_rich_narrative(modules_jsonb["client"]["modules"])
    html = render_report_v3_html(
        meta={"brand_name": "测试品牌", "total_score": 45, "level": "边缘级"},
        modules_jsonb=modules_jsonb,
    )

    assert narrative["source_module"] == "1"
    assert narrative["executive_summary"] == real
    assert stale not in str(narrative)
    assert real in html
    assert stale not in html


def test_rich_narrative_module_loader_never_uses_internal_or_root_fallback():
    from services.llm_rich_narrative import _modules_from_jsonb

    sentinel = "INTERNAL_ONLY_MARGIN_37_PERCENT"
    assert _modules_from_jsonb(
        {"internal": {"modules": {"1": {"rendered_md": sentinel}}}}
    ) == {}
    assert _modules_from_jsonb({"modules": {"1": {"rendered_md": sentinel}}}) == {}


def test_enrichment_does_not_create_customer_modules_from_internal_report(monkeypatch):
    import services.llm_rich_narrative as narrative_service

    sentinel = "INTERNAL_ONLY_MARGIN_37_PERCENT"

    class _Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append((sql, params))

        def fetchone(self):
            return {
                "id": 77,
                "report_v2_version": "v2",
                "report_v2_modules_jsonb": {
                    "internal": {"modules": {"1": {"rendered_md": sentinel}}}
                },
            }

    class _Connection:
        def __init__(self):
            self.cur = _Cursor()
            self.commits = 0

        def cursor(self):
            return self.cur

        def commit(self):
            self.commits += 1

        def close(self):
            return None

    conn = _Connection()
    monkeypatch.setattr(narrative_service, "get_connection", lambda: conn)

    result = asyncio.run(narrative_service.enrich_diagnosis_narrative(77))
    executed = "\n".join(sql for sql, _params in conn.cur.statements)

    assert result["status"] == "failed_full"
    assert result["reason"] == "client_report_not_ready"
    assert "SET report_v2_modules_jsonb" not in executed
    assert "client_report_not_ready" in executed
    assert conn.commits == 1


def test_enrichment_persists_real_numeric_conclusion_not_not_ready_copy(monkeypatch):
    import services.llm_rich_narrative as narrative_service

    real_conclusion = "真实诊断结论：客户场景词已有直接证据。"

    class _Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append((sql, params))

        def fetchone(self):
            return {
                "id": 78,
                "report_v2_version": "v2",
                "report_v2_modules_jsonb": {
                    "client": {
                        "modules": {
                            "1": {
                                "insight": real_conclusion,
                                "rendered_md": real_conclusion,
                            }
                        }
                    }
                },
            }

    class _Connection:
        def __init__(self):
            self.cur = _Cursor()

        def cursor(self):
            return self.cur

        def commit(self):
            return None

        def close(self):
            return None

    conn = _Connection()
    monkeypatch.setattr(narrative_service, "get_connection", lambda: conn)

    result = asyncio.run(narrative_service.enrich_diagnosis_narrative(78))
    report_updates = [
        params[0]
        for sql, params in conn.cur.statements
        if "UPDATE diagnosis_records" in sql
    ]

    assert result["status"] == "succeeded"
    assert len(report_updates) == 1
    persisted = json.loads(report_updates[0])
    rich = persisted["client"]["modules"]["rich_narrative"]
    assert rich["executive_summary"] == real_conclusion
    assert "证据不足，暂无可核验结论" not in str(rich)


def test_enrichment_without_canonical_module_one_is_not_ready(monkeypatch):
    import services.llm_rich_narrative as narrative_service

    class _Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append((sql, params))

        def fetchone(self):
            return {
                "id": 79,
                "report_v2_version": "v2",
                "report_v2_modules_jsonb": {
                    "client": {"modules": {"2": {"dimensions": []}}}
                },
            }

    class _Connection:
        def __init__(self):
            self.cur = _Cursor()

        def cursor(self):
            return self.cur

        def commit(self):
            return None

        def close(self):
            return None

    conn = _Connection()
    monkeypatch.setattr(narrative_service, "get_connection", lambda: conn)

    result = asyncio.run(narrative_service.enrich_diagnosis_narrative(79))
    executed = "\n".join(sql for sql, _params in conn.cur.statements)

    assert result["status"] == "failed_full"
    assert result["reason"] == "client_report_not_ready"
    assert "SET report_v2_modules_jsonb" not in executed
    assert "client_report_not_ready" in executed


def test_classify_section_results_distinguishes_full_partial_failed():
    from services.llm_rich_narrative import classify_section_results

    assert classify_section_results([True] * 8) == "succeeded"
    assert classify_section_results([True, False] + [True] * 6) == "succeeded_partial"
    assert classify_section_results([False] * 8) == "failed_full"
