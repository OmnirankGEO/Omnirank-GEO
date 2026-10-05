# -*- coding: utf-8 -*-
"""引擎与名次同行取数锁 · 返工单 §2

上一版 SQL 把 `ARRAY_AGG(DISTINCT engine)` 与 `MIN(recommendation_rank)` **各自独立
聚合**,再在 Python 里拿 `engines[0]` 配 `best_rank` —— 两者**可能来自不同行**。
于是「在 DeepSeek 回答『X』时列第 2」这句可以是**假的**:真实可能是
DeepSeek 第 5、Kimi 第 2,而 `engines[0]` 恰好按字典序是 DeepSeek。

这句话正是 P1-2 允许在七要素不齐时对外说的**唯一一句**,它假了整条就废了。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from services.geo_douyin import ranking_payload as rp


REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = REPO / "services" / "geo_douyin" / "ranking_source.py"


# ===========================================================================
# 形态:SQL 必须同行取
# ===========================================================================

def test_sql_uses_distinct_on_for_the_row_level_tuple():
    src = SOURCE.read_text(encoding="utf-8")
    assert "DISTINCT ON (e.entity_key)" in src, "没有用 DISTINCT ON 取同一行"
    assert "best_engine" in src and "best_rank_raw" in src and "best_query" in src


def test_query_is_joined_from_answer_facts():
    """`query` 不在实体表上 —— 四元组要完整必须 join facts。"""
    src = SOURCE.read_text(encoding="utf-8")
    assert "geo_research_answer_facts" in src
    assert "f.query" in src


def test_engine_no_longer_taken_from_the_aggregated_list():
    """🔴 反向对照:`source["engine"]` 不得再从聚合出来的 engines 列表取。"""
    src = SOURCE.read_text(encoding="utf-8")
    i = src.find('"source": {')
    assert i > 0
    block = src[i: i + 900]
    assert '"engine": str(r.get("best_engine")' in block, "engine 不是同行取的"
    assert 'or [""])[0]' not in block, "又退回 engines[0] 了"


def test_aggregate_fields_are_kept_but_clearly_separated():
    """聚合面(engines / entity_row_ids)可以留,但**不能被当成同一行的事实**。"""
    src = SOURCE.read_text(encoding="utf-8")
    i = src.find('"source": {')
    block = src[i: i + 900]
    assert '"engines"' in block          # 聚合面还在
    assert "同行" in block or "聚合面" in block, "没有把两类分开标注"


# ===========================================================================
# 行为:四元组齐 / 缺元
# ===========================================================================

def _item(**src):
    return rp.RankingItem(rank=1, display_name="X", source=src)


def test_statement_uses_the_row_tuple_not_the_contract_query():
    """🔴 不完整分支只许引用同行四元组 —— 拿聚合层的 query 去拼就是造句。"""
    it = _item(engine="Kimi", recommendation_rank=2,
               query="深圳载货电梯哪家好", observed_at="2026-08-06T00:00:00")
    got = rp.rank_statement(it, rp.AggregationContract(query="完全不同的另一个问题"))
    assert "深圳载货电梯哪家好" in got, f"用了聚合层的 query:{got}"
    assert "完全不同的另一个问题" not in got
    assert "Kimi" in got and "第 2" in got


@pytest.mark.parametrize("missing", ["engine", "query", "observed_at"])
def test_incomplete_tuple_makes_no_engine_or_rank_claim(missing):
    """四元组缺任何一元 → **不作任何引擎/名次断言**。"""
    src = {"engine": "Kimi", "recommendation_rank": 2,
           "query": "Q", "observed_at": "2026-08-06T00:00:00"}
    src[missing] = ""
    got = rp.rank_statement(_item(**src), rp.AggregationContract(query="Q"))
    assert "第 2" not in got, f"缺 {missing} 仍断言了名次:{got}"
    assert "Kimi" not in got, f"缺 {missing} 仍断言了引擎:{got}"


def test_no_rank_but_full_context_says_mentioned_only():
    got = rp.rank_statement(
        _item(engine="Kimi", recommendation_rank=None, query="Q",
              observed_at="2026-08-06T00:00:00"),
        rp.AggregationContract(query="Q"))
    assert "被提到" in got and "第" not in got


def test_rank_statement_tuple_constant_is_the_four():
    assert tuple(rp.RANK_STATEMENT_TUPLE) == (
        "engine", "recommendation_rank", "query", "observed_at")


# ===========================================================================
# 真库判别:引擎 A 第 5、引擎 B 第 2 → 必须说 B
# ===========================================================================

_FIXTURE_IND = "samerow_probe_ind"


@pytest.fixture(scope="module")
def seeded():
    """造两行:A 第 5、B 第 2(同一实体)。判据:表述必须指向 B。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 🔴 `(engine, batch_id, answer_hash)` 上有唯一约束 —— fixture 必须给
        #    自己独有的 batch_id/answer_hash,否则会和别的用例/调试残留撞。
        # 🔴 `answer_hash` 是 **character(32)** —— 长一位就 StringDataRightTruncation
        #    (SQL 4 维核验的 data_type 那条,这里踩过一次)。
        cur.execute("""
            INSERT INTO geo_research_answer_facts
                (id, raw_id, industry_key, query, engine, batch_id, answer_hash,
                 extractor_version)
            VALUES (990101, 1, %s, '深圳载货电梯哪家好', 'A',
                    'samerow_probe', %s, 'v1'),
                   (990102, 2, %s, '深圳载货电梯哪家好', 'B',
                    'samerow_probe', %s, 'v1')
            ON CONFLICT DO NOTHING
        """, (_FIXTURE_IND, "a" * 32, _FIXTURE_IND, "b" * 32))
        # 🔴 **必须幂等** —— `(answer_fact_id, entity_key)` 上有唯一约束。
        #    不加 ON CONFLICT 的话这个 fixture 只能在全新库上跑一次,
        #    第二次(以及变异 runner 反复跑)当场 UniqueViolation,
        #    表现为"基线红",而基线红会让整轮变异结论作废。
        cur.execute("""
            INSERT INTO geo_research_answer_entities
                (answer_fact_id, raw_id, industry_key, engine, entity_name, entity_key,
                 entity_type, recommendation_rank, evidence_phrases,
                 recommendation_reasons, confidence, llm_model, extractor_version)
            VALUES (990101, 1, %s, 'A', '同行探针甲', 'me_sr1', 'brand', 5,
                    '[]', '[]', 1.0, 'm', 'v1'),
                   (990102, 2, %s, 'B', '同行探针甲', 'me_sr1', 'brand', 2,
                    '[]', '[]', 1.0, 'm', 'v1')
            ON CONFLICT (answer_fact_id, entity_key) DO NOTHING
        """, (_FIXTURE_IND, _FIXTURE_IND))
        conn.commit()
    finally:
        conn.close()
    yield


def test_best_engine_comes_from_the_best_ranked_row(seeded):
    """🔴 返工单点名的判据:A 第 5 / B 第 2 → 必须是「在 B …第 2」,
    出现「在 A …第 2」即红。"""
    from services.geo_douyin.ranking_source import fetch_ranking_candidates
    pool = fetch_ranking_candidates(_FIXTURE_IND)
    assert pool, "探针数据没取到"
    c = next(x for x in pool if x["entity_name"] == "同行探针甲")
    assert c["best_rank"] == 2
    assert c["source"]["engine"] == "B", (
        f"engine 与 rank 不同行:engine={c['source']['engine']} rank={c['best_rank']}")
    assert c["source"]["query"] == "深圳载货电梯哪家好"

    it = rp.RankingItem(rank=1, display_name=c["entity_name"], source=c["source"])
    said = rp.rank_statement(it, rp.AggregationContract(query="深圳载货电梯哪家好"))
    assert "在 B" in said and "第 2" in said, said
    assert "在 A" not in said, f"说成了 A:{said}"


def test_aggregate_still_reports_both_engines(seeded):
    """反向对照:聚合面仍要看得到 A 和 B(共识度靠它),只是不许拿它配名次。"""
    from services.geo_douyin.ranking_source import fetch_ranking_candidates
    c = next(x for x in fetch_ranking_candidates(_FIXTURE_IND)
             if x["entity_name"] == "同行探针甲")
    assert set(c["engines"]) == {"A", "B"}
    assert c["engine_count"] == 2
