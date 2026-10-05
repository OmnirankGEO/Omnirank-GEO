from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bridge_raw_answer_adoption_to_source_signals_2026_06_17.py"


def test_bridge_script_is_dry_run_by_default_and_requires_full_to_write():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "dry_run=not args.full" in source
    assert "parser.add_argument(\"--full\"" in source
    assert "\"production_takeover\": False" in source
    assert "\"shadow_only\": True" in source


def test_bridge_script_reads_raw_and_writes_only_source_signals():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "FROM geo_research_raw raw" in source
    assert "COUNT(*) OVER" in source
    assert "PARTITION BY raw.batch_id, raw.engine, raw.query" in source
    assert "upsert_source_signal" in source
    assert "geo_research_source_signals" in source
    assert "geo_answer_adoption_metrics" not in source
    assert "UPDATE geo_research_raw" not in source
    assert "DELETE FROM" not in source
    assert "TRUNCATE" not in source
