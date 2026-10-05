"""[答案实体] admin API:_require_admin 门禁 + examples 不返完整 answer_text + rebuild 默认 dry_run 不调 LLM。

[A1 2026-08-01] answer_entities_examples/summary 两个只读端点已改同步 def(事件循环阻塞治理),
本文件直调 handler 处随之去掉 await —— 线上行为不变(FastAPI 对同步 handler 自动 run_in_threadpool)。
"""
import db.diagnosis_db  # noqa: F401

import pytest
from fastapi import HTTPException

from api.media_entity_flywheel_api import (
    answer_entities_examples_endpoint,
    answer_entities_rebuild_endpoint,
    AnswerEntityRebuildRequest,
)
from db.research_answer_entity_db import init_research_answer_entity_tables, upsert_answer_fact_with_entities


class _State:
    def __init__(self, user):
        self.user = user


class _Req:
    def __init__(self, user):
        self.state = _State(user)


@pytest.mark.asyncio
async def test_require_admin_blocks_anonymous_and_nonadmin():
    with pytest.raises(HTTPException) as e1:
        answer_entities_examples_endpoint(_Req(None))
    assert e1.value.status_code == 401
    with pytest.raises(HTTPException) as e2:
        answer_entities_examples_endpoint(_Req({"is_admin": False}))
    assert e2.value.status_code == 403


@pytest.mark.asyncio
async def test_examples_never_returns_full_answer_text(db_with_clean_research):
    init_research_answer_entity_tables()
    conn = db_with_clean_research
    c = conn.cursor()
    c.execute("DELETE FROM geo_research_answer_entities")
    c.execute("DELETE FROM geo_research_answer_facts")
    conn.commit()
    upsert_answer_fact_with_entities(
        {"raw_id": 3001, "industry": "科技数码", "industry_key": "technology", "query": "q",
         "engine": "DeepSeek", "answer_excerpt": "短摘要", "quality_flag": "auto"},
        [{"entity_name": "阿里云", "entity_key": "me_x", "recommendation_reasons": ["稳定"],
          "evidence_phrases": ["云"], "confidence": 0.9}],
    )
    resp = answer_entities_examples_endpoint(_Req({"is_admin": True}), industry="科技数码")
    assert resp["status"] == "success"
    assert resp["examples"], "应有样例"
    for row in resp["examples"]:
        assert "answer_text" not in row, "🔴 examples 绝不返完整 answer_text"


@pytest.mark.asyncio
async def test_rebuild_default_dry_run_does_not_call_llm(db_with_clean_research, monkeypatch):
    init_research_answer_entity_tables()
    # 若 dry_run 误调 LLM,这个 fake 会抛 → 测试红
    import tools.distillation.distiller as dz

    async def boom(**kw):
        raise AssertionError("dry_run 不应调用 LLM")

    monkeypatch.setattr(dz, "distill_keyword", boom)

    req = AnswerEntityRebuildRequest(industry="", limit=10, dry_run=True)
    resp = await answer_entities_rebuild_endpoint(req, _Req({"is_admin": True}))
    assert resp["status"] == "success"
    assert resp["llm_called"] is False
    assert resp["mode"] == "dry_run"


@pytest.mark.asyncio
async def test_summary_engine_filter_normalizes_alias(db_with_clean_research):
    """[review fix low] 读侧 engine 过滤归一:?engine=qwen 应命中存储的 canonical '千问'。"""
    from api.media_entity_flywheel_api import answer_entities_summary_endpoint
    init_research_answer_entity_tables()
    conn = db_with_clean_research
    c = conn.cursor()
    c.execute("DELETE FROM geo_research_answer_entities")
    c.execute("DELETE FROM geo_research_answer_facts")
    conn.commit()
    upsert_answer_fact_with_entities(
        {"raw_id": 4001, "industry": "科技数码", "industry_key": "technology", "query": "q",
         "engine": "千问", "quality_flag": "auto"},
        [{"entity_name": "阿里云", "entity_key": "me_ali", "confidence": 0.9}],
    )
    resp = answer_entities_summary_endpoint(_Req({"is_admin": True}), industry="科技数码", engine="qwen")
    assert "me_ali" in [e["entity_key"] for e in resp["entities"]], "engine=qwen 应归一到 千问 命中"
