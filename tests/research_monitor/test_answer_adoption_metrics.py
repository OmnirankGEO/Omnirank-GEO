from pathlib import Path

from services.research_monitor.answer_adoption_metrics import (
    build_metric_payload,
    metric_flags,
    normalized_metric_credit,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "rebuild_answer_adoption_metrics_2026_06_17.py"
MIGRATION = ROOT / "scripts" / "migration_geo_answer_adoption_metrics_2026_06_17.sql"
API = ROOT / "api" / "media_entity_flywheel_api.py"
PAGE = ROOT / "frontend" / "src" / "pages" / "Admin" / "GeoPlacementFlywheel.tsx"
REPORT_WRITER = ROOT / "services" / "report_writer_v2.py"
RICH_NARRATIVE = ROOT / "services" / "llm_rich_narrative.py"


def test_metric_flags_do_not_promote_search_exposure_to_adoption():
    assert metric_flags("answer_adopted") == {
        "answer_adopted": True,
        "explicit_cited": True,
        "search_exposed": False,
        "reference_only": False,
        "rejected_noise": False,
    }
    assert metric_flags("cited_source")["explicit_cited"] is True
    assert metric_flags("cited_source")["answer_adopted"] is False
    assert metric_flags("search_result_only")["search_exposed"] is True
    assert metric_flags("search_result_only")["answer_adopted"] is False
    assert metric_flags("crawled_reference_only")["reference_only"] is True
    assert metric_flags("rejected_noise")["rejected_noise"] is True


def test_metric_credit_prefers_existing_balanced_weight_and_has_safe_fallback():
    assert normalized_metric_credit({"balanced_weight": 0.4567894}) == 0.456789
    fallback = normalized_metric_credit({
        "signal_tier": "cited_source",
        "total_sources_in_answer": 16,
        "source_position": 4,
    })
    assert 0 < fallback < 0.78


def test_metric_payload_maps_source_signal_to_readable_fields():
    payload = build_metric_payload({
        "id": 123,
        "source_url": "https://example.com/a",
        "domain": "example.com",
        "industry_key": "tourism_hotel",
        "engine": "deepseek",
        "prompt_id": "p1",
        "round_id": "r1",
        "signal_tier": "answer_adopted",
        "source_position": 2,
        "total_sources_in_answer": 9,
        "balanced_weight": 0.31,
    })

    assert payload["answer_adopted"] is True
    assert payload["explicit_cited"] is True
    assert payload["search_exposed"] is False
    assert payload["metadata"]["source_signal_id"] == 123
    assert payload["normalized_credit"] == 0.31


def test_rebuild_script_is_dry_run_by_default_and_reads_source_signals_only():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "parser.add_argument(\"--full\"" in text
    assert "dry_run=not args.full" in text
    assert "\"cap_hit\": bool(limit > 0 and len(rows) >= limit)" in text
    assert "0 means no SQL LIMIT" in text
    assert "FROM geo_research_source_signals" in text
    assert "FROM geo_research_raw" not in text
    assert "source_table\": \"geo_research_source_signals\"" in text


def test_answer_metric_migration_is_additive_only():
    text = MIGRATION.read_text(encoding="utf-8")
    upper = text.upper()
    assert "CREATE TABLE IF NOT EXISTS geo_answer_adoption_metrics" in text
    assert "CREATE INDEX IF NOT EXISTS idx_geo_answer_metrics_industry" in text
    for forbidden in ["DROP TABLE", "TRUNCATE", "ALTER TABLE RENAME", "DELETE FROM"]:
        assert forbidden not in upper


def test_admin_api_and_frontend_surface_shadow_only_answer_metrics():
    api_text = API.read_text(encoding="utf-8")
    page_text = PAGE.read_text(encoding="utf-8")

    assert "@router.get(\"/answer-adoption/summary\")" in api_text
    assert "@router.post(\"/answer-adoption/rebuild\")" in api_text
    assert "\"production_takeover\": False" in api_text
    assert "_require_admin(request)" in api_text
    assert "真实答案采纳指标" in page_text
    assert "预览真实采纳指标" in page_text
    assert "保存真实采纳指标" in page_text
    assert "搜索曝光不会被当成答案采纳" in page_text


def test_customer_facing_copy_distinguishes_visibility_from_answer_adoption():
    report_text = REPORT_WRITER.read_text(encoding="utf-8")
    rich_text = RICH_NARRATIVE.read_text(encoding="utf-8")

    assert "AI 搜索可见度与答案采纳变化区间" in report_text
    assert "AI 引用率变化区间" not in report_text
    assert "搜索曝光、答案采纳" in rich_text
    assert "提及率、引用率" not in rich_text
