# -*- coding: utf-8 -*-
"""Writing batch concurrency regression tests.

These tests lock the June 23 production incident chain:
- duplicate "start writing" clicks must not refresh an existing fresh writing round;
- generated work must be tied to the batch lease that started it;
- crash cleanup must not leak a DB transaction and must read COUNT(*) AS cnt correctly.
"""
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_start_articles_filters_fresh_duplicate_writing_batch():
    src = _read("server.py")
    fn_start = src.find("async def api_start_articles")
    fn = src[fn_start:fn_start + 12000]

    assert "NOW() - INTERVAL '90 minutes'" in fn
    assert "status='writing'" in fn
    assert "RETURNING id, writing_started_at" in fn
    assert "duplicate_writing_ids" in fn
    assert "_writing_started_at" in fn
    assert "article_id IS NULL" in fn
    assert "选中的标题正在生成中，无需重复开始" in fn
    assert "skipped_topic_ids" in fn


def test_start_articles_reset_path_uses_cnt_and_always_closes_connection():
    src = _read("server.py")
    fn_start = src.find("def run_in_thread")
    fn = src[fn_start:fn_start + 4500]

    assert "COUNT(*) as cnt" in fn
    assert "row['cnt']" in fn
    assert "row['count']" not in fn
    assert "reset_conn = None" in fn
    assert "lease_by_topic_copy" in fn
    assert "AND writing_started_at=%s" in fn
    assert "reset_conn.rollback()" in fn
    assert "reset_conn.close()" in fn
    assert "finally:" in fn


def test_article_generator_refresh_and_save_are_batch_lease_guarded():
    src = _read("writing/article_generator_service.py")

    assert "_writing_started_at" in src
    assert "RETURNING writing_started_at" in src
    assert "skipped_taken_over" in src
    assert "AND writing_started_at=%s" in src
    assert "article_id IS NULL" in src


def test_start_articles_all_taken_over_skips_are_not_failed_or_refunded():
    src = _read("server.py")
    fn_start = src.find("async def api_start_articles")
    fn = src[fn_start:fn_start + 18000]

    assert "_takeover_skip_reasons" in fn
    assert "skipped_taken_over" in fn and "stale_batch" in fn
    assert "_takeover_skipped > 0" in fn
    assert "_takeover_skipped == len(gen_results or [])" in fn
    assert "全部 topic 已被其他写作批次接管" in fn
    assert "return" in fn[fn.find("_takeover_skipped == len(gen_results or [])"):fn.find("_takeover_skipped == len(gen_results or [])") + 900]


def test_failed_topic_updates_never_touch_existing_articles():
    src = _read("server.py") + "\n" + _read("writing/article_generator_service.py")
    assert "WHERE id=%s AND status=%s AND article_id IS NULL" in src
    assert "WHERE id=%s\n                              AND status='writing'\n                              AND article_id IS NULL" in src


@pytest.mark.asyncio
async def test_stale_batch_skips_before_llm_and_does_not_spend_tokens(monkeypatch):
    from writing.article_generator_service import ArticleGeneratorService

    executed_sql = []

    class FakeCursor:
        def execute(self, sql, params=None):
            executed_sql.append((sql, params))

        def fetchone(self):
            # First lease refresh returns no row: another batch already owns this topic.
            return None

    class FakeConnection:
        def __init__(self):
            self.closed = False
            self.rolled_back = False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    fake_conn = FakeConnection()

    async def must_not_call_llm(self, topic, api_url, api_key, model):
        raise AssertionError("stale batch should skip before calling the LLM")

    async def fake_update_project_status(self):
        return None

    monkeypatch.setattr(ArticleGeneratorService, "_generate_validated_with_rewrite_once", must_not_call_llm)
    monkeypatch.setattr(ArticleGeneratorService, "_update_project_status", fake_update_project_status)
    monkeypatch.setattr("writing.llm_utils.get_api_key_for_provider", lambda provider: "test-key")
    monkeypatch.setitem(
        sys.modules,
        "db.diagnosis_db",
        types.SimpleNamespace(get_connection=lambda: fake_conn),
    )

    service = ArticleGeneratorService(quote_id=123, brand_name="测试品牌", industry="测试行业")
    results = await service.generate_articles(
        [
            {
                "id": 456,
                "title": "测试标题",
                "style_code": "buying_guide",
                "_writing_started_at": "2026-06-23T12:00:00",
            }
        ],
        max_concurrent=1,
        llm_override={"provider": "dashscope", "model": "qwen-test"},
    )

    assert results == [{"skipped": True, "topic_id": 456, "reason": "skipped_taken_over"}]
    assert any("writing_started_at=%s" in sql for sql, _ in executed_sql)
    assert fake_conn.closed is True
