"""[答案实体] db 层:建表幂等 + answer 级去重(engine/batch/answer_hash)+ 重抽不增行 + citation 折叠。"""
import db.diagnosis_db  # noqa: F401  bootstrap geo_research_raw

import pytest

from db.research_answer_entity_db import (
    init_research_answer_entity_tables,
    upsert_answer_fact_with_entities,
    list_answer_entity_summary,
    list_answer_entity_examples,
    count_extractable_answers,
)


def _clean(conn):
    c = conn.cursor()
    c.execute("DELETE FROM geo_research_answer_entities")
    c.execute("DELETE FROM geo_research_answer_facts")
    conn.commit()


def _fact(engine="DeepSeek", batch_id="b1", answer_hash="hA", industry_key="education", raw_id=1001):
    return {"raw_id": raw_id, "industry": "教育培训", "industry_key": industry_key,
            "query": "教育机构哪家好", "engine": engine, "batch_id": batch_id,
            "answer_hash": answer_hash, "raw_ids": [raw_id], "citation_urls": ["http://x.com/a"],
            "answer_excerpt": "摘要", "quality_flag": "auto"}


def _ent(name, key, rank=1):
    return {"entity_name": name, "entity_key": key, "entity_type": "brand",
            "recommendation_rank": rank, "mention_rank": rank,
            "recommendation_reasons": ["师资强"], "evidence_phrases": ["名师"],
            "source_urls": ["http://x.com/a"], "confidence": 0.9, "llm_model": "m"}


def _count(conn, table):
    c = conn.cursor()
    c.execute(f"SELECT COUNT(*) AS c FROM {table}")
    return c.fetchone()["c"]


def test_init_is_idempotent(db_with_clean_research):
    init_research_answer_entity_tables()
    init_research_answer_entity_tables()  # 重复调用不报错
    conn = db_with_clean_research
    _clean(conn)
    assert _count(conn, "geo_research_answer_facts") == 0


def test_dedup_by_answer_key_and_reextract_no_row_increase(db_with_clean_research):
    """去重键 = (engine, batch_id, answer_hash)。同键重抽 → fact 不增行;实体 delete+insert 不增/无残留。"""
    init_research_answer_entity_tables()
    conn = db_with_clean_research
    _clean(conn)

    r1 = upsert_answer_fact_with_entities(_fact(answer_hash="hA"), [_ent("阿里云", "me_a"), _ent("腾讯云", "me_b")])
    assert r1["entity_written"] == 2
    assert _count(conn, "geo_research_answer_facts") == 1
    assert _count(conn, "geo_research_answer_entities") == 2

    # 同 (engine,batch,answer_hash) 重抽 → upsert,不增行
    upsert_answer_fact_with_entities(_fact(answer_hash="hA"), [_ent("阿里云", "me_a"), _ent("腾讯云", "me_b")])
    assert _count(conn, "geo_research_answer_facts") == 1, "同 answer 键重抽不应新增 fact"
    assert _count(conn, "geo_research_answer_entities") == 2, "重抽不应新增实体(delete+insert 幂等)"

    # 实体减少 → 无残留
    upsert_answer_fact_with_entities(_fact(answer_hash="hA"), [_ent("阿里云", "me_a")])
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS c FROM geo_research_answer_entities WHERE answer_fact_id=%s", (r1["fact_id"],))
    assert c.fetchone()["c"] == 1, "重抽实体减少后旧实体应被清掉"


def test_summary_rollup_counts_answers_not_citation_rows(db_with_clean_research):
    init_research_answer_entity_tables()
    conn = db_with_clean_research
    _clean(conn)
    # 同一实体在两个引擎的两条答案里 → engine_count=2, mention_count=2
    upsert_answer_fact_with_entities(_fact(engine="DeepSeek", answer_hash="hA"), [_ent("阿里云", "me_ali", rank=1)])
    upsert_answer_fact_with_entities(_fact(engine="Kimi", answer_hash="hB"), [_ent("阿里云", "me_ali", rank=3)])

    summary = list_answer_entity_summary(industry_key="education", limit=10)
    ali = [s for s in summary if s["entity_key"] == "me_ali"]
    assert ali and ali[0]["mention_count"] == 2 and ali[0]["engine_count"] == 2
    assert ali[0]["avg_recommendation_rank"] == pytest.approx(2.0)

    examples = list_answer_entity_examples(industry_key="education", limit=10)
    assert examples and all("answer_text" not in row for row in examples), "examples 绝不含完整 answer_text"


@pytest.mark.asyncio
async def test_answer_level_dedup_collapses_citation_rows(db_with_clean_research, monkeypatch):
    """[review fix P1-1] 同一答案的 N 条 citation 行折叠成 1 个 answer fact,抽 1 次、计 1 次(非 N 次)。"""
    from services.research_monitor import answer_entity_extractor as ext
    import tools.distillation.distiller as dz
    init_research_answer_entity_tables()
    conn = db_with_clean_research
    _clean(conn)
    c = conn.cursor()
    ans = "综合来看推荐阿里云,稳定可靠。" * 20
    for url in ("http://a.com/1", "http://b.com/2", "http://c.com/3"):  # 同答案 3 条 citation 行
        c.execute("""INSERT INTO geo_research_raw (industry, query, engine, cited_platform, cite_url, answer_text, batch_id)
                     VALUES ('科技数码','云服务哪家好','deepseek','x',%s,%s,'b1')""", (url, ans))
    conn.commit()

    calls = {"n": 0}

    async def fake_ok(**kw):
        calls["n"] += 1
        return dz.DistillationResult(keyword="k", brands=[
            dz.BrandInfo(name="阿里云", ranks={"DeepSeek": 1}, recommendation_reasons=["稳定"])]), 10, "auto"
    monkeypatch.setattr(dz, "distill_keyword", fake_ok)

    r = await ext.rebuild_answer_entities(industry="科技数码", limit=50, dry_run=False)
    assert calls["n"] == 1, "3 条 citation 属同一答案 → 只应抽 1 次(不 3 倍 LLM)"
    assert r["facts_written"] == 1
    c.execute("SELECT raw_ids FROM geo_research_answer_facts")
    facts = c.fetchall()
    assert len(facts) == 1 and len(facts[0]["raw_ids"]) == 3, "1 个 answer fact 聚合 3 条 citation raw"
    summary = list_answer_entity_summary(industry_key="technology")
    ali = [s for s in summary if s["entity_key"] == ext.build_answer_entity_key("阿里云")]
    assert ali and ali[0]["mention_count"] == 1, "同一答案推荐一次 = 计 1 次(非 3 次)"


@pytest.mark.asyncio
async def test_degraded_answer_persisted_and_converges(db_with_clean_research, monkeypatch):
    """[review fix medium] LLM degraded 答案落 fact → only_pending 下轮排除(不再无限重抽)。"""
    from services.research_monitor import answer_entity_extractor as ext
    import tools.distillation.distiller as dz
    init_research_answer_entity_tables()
    conn = db_with_clean_research
    _clean(conn)
    c = conn.cursor()
    c.execute("""INSERT INTO geo_research_raw (industry, query, engine, cited_platform, cite_url, answer_text, batch_id)
                 VALUES ('教育培训','q','deepseek','x','http://a','""" + "推荐某某机构。" * 40 + "','b')")
    conn.commit()

    async def fake_degraded(**kw):
        return dz.DistillationResult(keyword="k", brands=[]), 0, "degraded"
    monkeypatch.setattr(dz, "distill_keyword", fake_degraded)

    assert count_extractable_answers(["教育培训"])["pending_extract"] == 1
    r1 = await ext.rebuild_answer_entities(industry="教育培训", limit=50, dry_run=False)
    assert r1["degraded"] == 1
    c.execute("SELECT quality_flag FROM geo_research_answer_facts")
    rows = c.fetchall()
    assert len(rows) == 1 and rows[0]["quality_flag"] == "degraded", "degraded 应落 fact 标记"
    assert count_extractable_answers(["教育培训"])["pending_extract"] == 0, "落标记后 only_pending 应收敛到 0"
