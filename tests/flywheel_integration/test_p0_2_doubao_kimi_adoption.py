"""[P0-2 + 补充指令 §2] Doubao/Kimi answer-adoption 口径修复:
- mark_answer_cited_sources is now wired into query_doubao AND query_kimi (forward fix).
- cite_url alone is NEVER adoption; only an in-answer [n]/【n】 marker matching the rank is.
- backfill recomputes historical rows from answer_text markers only; reports determinable
  vs still-undeterminable + per-engine, and never over-counts.
"""
import inspect

import pytest

import db.diagnosis_db  # noqa: F401  建 geo_research_raw
from db.connection import get_connection
from services.research_monitor import platforms
from services.research_monitor.platforms import mark_answer_cited_sources


def test_mark_wired_into_doubao_and_kimi():
    assert "mark_answer_cited_sources" in inspect.getsource(platforms.query_doubao)
    assert "mark_answer_cited_sources" in inspect.getsource(platforms.query_kimi)


def test_cite_url_alone_is_not_adoption():
    # search-result cite_url with no in-answer marker → NOT adopted
    out = mark_answer_cited_sources("这是一段答案，没有任何引用标记。", [{"url": "https://a.com", "rank": 1}])
    assert out[0]["is_answer_cited"] is False
    assert out[0]["adoption_rank"] is None


def test_in_answer_marker_is_adoption():
    out = mark_answer_cited_sources("根据资料[1]的说法……", [{"url": "https://a.com", "rank": 1}])
    assert out[0]["is_answer_cited"] is True
    assert out[0]["adoption_rank"] == 1


def _seed(conn, rows):
    """rows: (engine, cite_position, answer_text)."""
    c = conn.cursor()
    for (engine, pos, ans) in rows:
        c.execute(
            """
            INSERT INTO geo_research_raw
                (industry, query, engine, cited_platform, cite_position, cite_url,
                 cite_title, cite_excerpt, answer_text, is_answer_cited, adoption_rank,
                 batch_id, researcher)
            VALUES ('教育培训', 'q', %s, '', %s, 'https://x.com/p', '', '', %s, FALSE, NULL, 'batch_X', '')
            """,
            (engine, pos, ans),
        )
    conn.commit()


def test_backfill_reports_determinable_and_does_not_overcount(db_with_clean_research):
    from scripts.backfill_doubao_kimi_answer_adoption_2026_07_03 import backfill
    conn = db_with_clean_research
    _seed(conn, [
        ("doubao", 1, "参考[1]和[2]"),   # determinable (pos 1 in {1,2})
        ("doubao", 2, "参考[1]和[2]"),   # determinable
        ("doubao", 3, "参考[1]和[2]"),   # undeterminable (pos 3 not in {1,2})
        ("kimi", 1, "回答里没有任何标记"),  # undeterminable (no marker)
    ])

    dry = backfill(days=0, dry_run=True)
    assert dry["scanned_rows"] == 4
    assert dry["determinable_adoption_rows"] == 2
    assert dry["still_undeterminable_rows"] == 2
    assert dry["by_engine"]["doubao"]["determinable"] == 2
    assert dry["by_engine"]["kimi"]["determinable"] == 0
    assert dry["written"] == 0  # dry-run writes nothing

    # real write flips only the determinable rows
    real = backfill(days=0, dry_run=False)
    assert real["written"] == 2
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS c FROM geo_research_raw WHERE is_answer_cited = TRUE")
    assert int(c.fetchone()["c"]) == 2
    # kimi (no marker) stays search_result_only
    c.execute("SELECT is_answer_cited FROM geo_research_raw WHERE engine='kimi'")
    assert c.fetchone()["is_answer_cited"] is False
