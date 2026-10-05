"""[T3] 明确引用口径 SSOT:metrics 与 rollup 同源(answer_adopted + cited_source),单点定义。"""
import hashlib

import pytest

from services.research_monitor.source_signal_weighting import EXPLICIT_CITED_TIERS
from services.research_monitor.answer_adoption_metrics import metric_flags
from db.geo_source_signals_db import (
    _EXPLICIT_CITED_SQL,
    init_geo_source_signal_tables,
    list_source_signal_rollup,
    upsert_source_signal,
)
from db.connection import get_connection


def test_ssot_definition():
    assert EXPLICIT_CITED_TIERS == frozenset({"answer_adopted", "cited_source"})
    assert _EXPLICIT_CITED_SQL == "signal_tier IN ('answer_adopted', 'cited_source')"


def test_metric_flags_uses_ssot():
    assert metric_flags("answer_adopted")["explicit_cited"] is True
    assert metric_flags("cited_source")["explicit_cited"] is True
    assert metric_flags("search_result_only")["explicit_cited"] is False
    assert metric_flags("crawled_reference_only")["explicit_cited"] is False


def _seed(domain, industry_key, tier, i):
    url = f"https://{domain}/{tier}/{i}"
    upsert_source_signal({
        "source_url": url,
        "url_hash": hashlib.sha1(url.encode()).hexdigest(),
        "domain": domain,
        "industry_key": industry_key,
        "engine": "deepseek",
        "prompt_id": f"p{tier}{i}",
        "signal_tier": tier,
        "source_position": 1,
        "total_sources_in_answer": 1,
        "balanced_weight": 1.0,
        "answer_mentioned_brand": False,
        "round_id": "batch_T3",
        "metadata": {},
    })


def test_rollup_explicit_cited_count_is_adopted_plus_cited():
    init_geo_source_signal_tables()
    industry_key = "t3_ssot_ind"
    domain = "t3ssot.example.com"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM geo_research_source_signals WHERE industry_key = %s", (industry_key,))
        conn.commit()
    finally:
        conn.close()

    for i in range(2):
        _seed(domain, industry_key, "answer_adopted", i)
    _seed(domain, industry_key, "cited_source", 0)
    for i in range(3):
        _seed(domain, industry_key, "search_result_only", i)

    rollup = list_source_signal_rollup(industry_key=industry_key, limit=50)
    row = next((r for r in rollup if r["domain"] == domain), None)
    assert row is not None
    assert int(row["answer_adopted_count"]) == 2
    assert int(row["cited_count"]) == 1               # 纯 cited_source(供评分,不变)
    assert int(row["explicit_cited_count"]) == 3      # SSOT = 采纳 + 引用 = 明确引用

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM geo_research_source_signals WHERE industry_key = %s", (industry_key,))
        conn.commit()
    finally:
        conn.close()
