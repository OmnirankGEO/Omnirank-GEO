from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "backfill_answer_adoption_signals_2026_06_16.py"


def test_backfill_is_dry_run_by_default_and_requires_full_to_write():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "parser.add_argument(\"--full\"" in text
    assert "\"dry_run\": not full" in text
    assert "if full and updates:" in text
    assert "\"cap_hit\": bool(limit > 0 and len(rows) >= limit)" in text
    assert "0 means no SQL LIMIT" in text


def test_backfill_only_updates_raw_answer_adoption_fields():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "UPDATE geo_research_raw" in text
    assert "SET is_answer_cited = %s" in text
    assert "adoption_rank = %s" in text
    forbidden = ["DROP TABLE", "TRUNCATE", "DELETE FROM", "ALTER TABLE RENAME"]
    upper = text.upper()
    for token in forbidden:
        assert token not in upper
