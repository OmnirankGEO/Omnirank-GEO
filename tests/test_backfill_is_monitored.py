from pathlib import Path

from scripts.backfill_is_monitored_2026_05_08 import build_quote_targets_from_sessions


ROOT = Path(__file__).resolve().parents[1]


def test_backfill_maps_final_keyword_ids_to_text_not_confirmed_keyword_ids():
    rows = [
        {
            "quote_id": 287,
            "status": "confirmed",
            "final_keyword_ids": "[1, 3]",
            "selected_keyword_ids": None,
            "keywords_snapshot": '[{"id":1,"keyword":"揭阳电梯维修"},{"id":2,"keyword":"揭阳别墅电梯"},{"id":3,"keyword":"揭阳家用电梯"}]',
            "pricing_data": None,
            "clusters_data": None,
        }
    ]

    result = build_quote_targets_from_sessions(rows)

    assert result["quote_to_keywords"] == {
        287: ["揭阳电梯维修", "揭阳家用电梯"],
    }
    assert result["selected_ids_total"] == 2
    assert result["selected_keywords_total"] == 2


def test_backfill_keeps_quoted_sessions_out_unless_explicitly_included():
    rows = [
        {
            "quote_id": 89,
            "status": "quoted",
            "final_keyword_ids": "[1]",
            "selected_keyword_ids": "[1]",
            "keywords_snapshot": '[{"id":1,"keyword":"揭阳电梯维修"}]',
            "pricing_data": None,
            "clusters_data": None,
        }
    ]

    default_result = build_quote_targets_from_sessions(rows)
    included_result = build_quote_targets_from_sessions(rows, include_quoted=True)

    assert default_result["quote_to_keywords"] == {}
    assert included_result["quote_to_keywords"] == {89: ["揭阳电梯维修"]}


def test_backfill_and_selection_mark_by_keyword_text_not_confirmed_keyword_id():
    backfill_text = (ROOT / "scripts/backfill_is_monitored_2026_05_08.py").read_text(encoding="utf-8")
    selection_text = (ROOT / "api/selection_api.py").read_text(encoding="utf-8")

    assert "WHERE quote_id = %s AND id = ANY(%s)" not in backfill_text
    assert "WHERE quote_id = %s AND id = ANY(%s)" not in selection_text
    assert "WHERE quote_id = %s AND keyword = ANY(%s)" in backfill_text
    assert "WHERE quote_id = %s AND keyword = ANY(%s)" in selection_text
