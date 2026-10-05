from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_FILE = ROOT / "db" / "writing_style_flywheel_db.py"


def test_style_feature_query_deduplicates_source_signals_before_article_rollup():
    text = DB_FILE.read_text(encoding="utf-8")
    assert "WITH source_signal_by_url AS" in text
    assert "GROUP BY source_url, industry_key" in text
    assert "LEFT JOIN source_signal_by_url sig ON sig.source_url = raw.cite_url" in text
    assert "GROUP BY citation.article_id, raw.cite_url" in text


def test_style_feature_query_prioritizes_answer_adoption_before_article_length():
    text = DB_FILE.read_text(encoding="utf-8")
    adopted_pos = text.index("ORDER BY adopted_count DESC")
    length_pos = text.index("article.cleaned_char_count DESC")
    assert adopted_pos < length_pos
    assert '"adopted_count": row.get("adopted_count") or 0' in text
    assert '"source_weight": float(row.get("source_weight") or 0)' in text
