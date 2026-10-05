from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types

import pytest

from services.article_generation_reset import (
    ArticleResetConflict,
    reset_topics_for_full_regeneration,
)
from services.article_generation_status import refund_status_message
from writing.article_generation_failure import (
    ArticleProviderUnsupported,
    ArticleSaveFailed,
    classify_article_generation_failure,
)
from writing.content_cleaner import clean_llm_article
import writing.evidence_precision_policy as evidence_precision_policy


ROOT = Path(__file__).resolve().parents[1]


class _ResetCursor:
    def __init__(self, rows: list[dict]):
        self.rows = {int(row["id"]): dict(row) for row in rows}
        self.events: set[tuple[str, int]] = set()
        self._one = None
        self.rowcount = 0

    def execute(self, sql: str, params=()):
        normalized = " ".join(sql.split())
        self.rowcount = 0
        self._one = None
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            self._many = []
            return
        if normalized.startswith("SELECT quote_id,topic_id,previous_published"):
            request_id = str(params[0])
            self._many = [
                {
                    "quote_id": self.rows[topic_id]["quote_id"],
                    "topic_id": topic_id,
                    "previous_published": bool(
                        self.rows[topic_id].get("first_published_at")
                    ),
                }
                for event_request_id, topic_id in sorted(self.events)
                if event_request_id == request_id
            ]
            return
        if normalized.startswith("SELECT t.id,t.status"):
            quote_id, ids = params
            self._many = [
                dict(self.rows[int(topic_id)])
                for topic_id in ids
                if int(topic_id) in self.rows
                and int(self.rows[int(topic_id)]["quote_id"]) == int(quote_id)
            ]
            return
        if normalized.startswith("INSERT INTO article_generation_revision_events"):
            request_id, _quote_id, topic_id = params[:3]
            key = (str(request_id), int(topic_id))
            if key not in self.events:
                self.events.add(key)
                self._one = {"id": len(self.events)}
            return
        if normalized.startswith("UPDATE topics SET optimized_title=NULL"):
            revision, topic_id, quote_id = params
            row = self.rows[int(topic_id)]
            assert int(row["quote_id"]) == int(quote_id)
            row.update(
                optimized_title=None,
                article_id=None,
                status="pending",
                generation_revision=int(revision),
                generation_operation="full_reset",
            )
            self.rowcount = 1
            return
        raise AssertionError(normalized)

    def fetchall(self):
        return self._many

    def fetchone(self):
        return self._one


def _reset_row(topic_id: int, *, published: bool, status: str = "completed") -> dict:
    return {
        "id": topic_id,
        "quote_id": 9,
        "status": status,
        "optimized_title": f"旧标题 {topic_id}",
        "article_id": 100 + topic_id,
        "generation_revision": 1,
        "article_version": 2,
        "first_published_at": "2026-07-21T00:00:00Z" if published else None,
    }


def test_full_reset_preserves_published_and_unpublished_history_and_is_idempotent():
    cur = _ResetCursor([_reset_row(1, published=False), _reset_row(2, published=True)])
    result = reset_topics_for_full_regeneration(
        cur,
        quote_id=9,
        topic_ids=[1, 2],
        actor_user_id=7,
        request_id="reset-request-0001",
    )
    assert result["reset_topic_ids"] == [1, 2]
    assert result["published_topic_ids"] == [2]
    assert cur.rows[1]["optimized_title"] is None
    assert cur.rows[1]["article_id"] is None
    assert cur.rows[1]["generation_revision"] == 2
    assert cur.events == {("reset-request-0001", 1), ("reset-request-0001", 2)}

    replay = reset_topics_for_full_regeneration(
        cur,
        quote_id=9,
        topic_ids=[2, 1],
        actor_user_id=7,
        request_id="reset-request-0001",
    )
    assert replay["reset_topic_ids"] == []
    assert replay["replayed_topic_ids"] == [1, 2]
    assert cur.rows[1]["generation_revision"] == 2


def test_full_reset_rejects_inflight_topic():
    cur = _ResetCursor([_reset_row(1, published=False, status="writing")])
    with pytest.raises(ArticleResetConflict) as error:
        reset_topics_for_full_regeneration(
            cur,
            quote_id=9,
            topic_ids=[1],
            actor_user_id=7,
            request_id="reset-request-0002",
        )
    assert error.value.code == "ARTICLE_RESET_IN_PROGRESS"


def test_full_reset_rejects_same_request_id_with_changed_topic_scope():
    cur = _ResetCursor([_reset_row(1, published=False), _reset_row(2, published=False)])
    reset_topics_for_full_regeneration(
        cur,
        quote_id=9,
        topic_ids=[1],
        actor_user_id=7,
        request_id="reset-request-scope-0001",
    )
    with pytest.raises(ArticleResetConflict) as error:
        reset_topics_for_full_regeneration(
            cur,
            quote_id=9,
            topic_ids=[1, 2],
            actor_user_id=7,
            request_id="reset-request-scope-0001",
        )
    assert error.value.code == "ARTICLE_RESET_IDEMPOTENCY_CONFLICT"
    assert cur.rows[2]["article_id"] == 102


def test_different_request_does_not_create_empty_revision_for_already_pending_topic():
    cur = _ResetCursor([_reset_row(1, published=False)])
    first = reset_topics_for_full_regeneration(
        cur,
        quote_id=9,
        topic_ids=[1],
        actor_user_id=7,
        request_id="reset-request-empty-0001",
    )
    assert first["reset_topic_ids"] == [1]
    second = reset_topics_for_full_regeneration(
        cur,
        quote_id=9,
        topic_ids=[1],
        actor_user_id=7,
        request_id="reset-request-empty-0002",
    )
    assert second["reset_topic_ids"] == []
    assert second["already_pending_topic_ids"] == [1]
    assert cur.rows[1]["generation_revision"] == 2
    assert ("reset-request-empty-0002", 1) not in cur.events


def test_reset_route_only_rolls_quote_back_when_a_topic_was_actually_reset():
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    assert 'if result["reset_topic_ids"]:' in server
    assert '"already_pending_count": len(result["already_pending_topic_ids"])' in server


def test_article_task_bootstrap_and_manifest_share_canonical_nullable_defaults():
    bootstrap = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    migration = (
        ROOT / "scripts" / "migration_article_generation_task_state_2026_07_21.sql"
    ).read_text(encoding="utf-8")
    nullable_columns = (
        "generation_request_id",
        "generation_operation",
        "generation_error_code",
        "generation_error_message",
        "generation_retryable",
        "generation_failure_phase",
        "generation_refund_status",
    )
    for column in nullable_columns:
        bootstrap_line = next(
            line
            for line in bootstrap.splitlines()
            if '_safe_add_column(cursor, "topics"' in line and f'"{column}"' in line
        )
        assert "DEFAULT NULL" not in bootstrap_line
        assert f"ALTER COLUMN {column} DROP DEFAULT;" in migration


def test_failure_contract_never_exposes_raw_provider_or_database_text():
    provider = classify_article_generation_failure(
        TimeoutError("Bearer secret-token stack /internal/path"), phase="provider"
    )
    assert provider.code == "ARTICLE_PROVIDER_UNAVAILABLE"
    assert "secret-token" not in provider.message

    save = classify_article_generation_failure(
        ArticleSaveFailed(), phase="save"
    )
    assert save.code == "ARTICLE_SAVE_FAILED"
    assert save.retryable is True
    assert "database" not in save.message.lower()


def test_unknown_provider_is_rejected_before_any_topic_or_provider_work():
    from writing.article_generator_service import ArticleGeneratorService

    service = ArticleGeneratorService(quote_id=1, brand_name="Brand", industry="Industry")
    with pytest.raises(ArticleProviderUnsupported):
        asyncio.run(
            service.generate_articles(
                [],
                llm_override={"provider": "unknown-provider", "model": "model"},
            )
        )


def test_batch_rewrite_is_body_only(monkeypatch):
    from writing.article_generator_service import ArticleGeneratorService

    calls = []

    async def fake_rewrite(topic_id: int, **kwargs):
        calls.append((topic_id, kwargs))
        return {"id": 99, "topic_id": topic_id}

    service = ArticleGeneratorService(quote_id=1, brand_name="Brand", industry="Industry")
    monkeypatch.setattr(service, "rewrite_article", fake_rewrite)
    result = asyncio.run(service.batch_rewrite_articles([4]))
    assert result["success"] == 1
    assert calls == [(4, {"preserve_title": True})]


@pytest.mark.parametrize(
    "failure_kind,expected_code",
    [
        ("provider", "ARTICLE_PROVIDER_UNAVAILABLE"),
        ("evidence", "ARTICLE_EVIDENCE_ADVISORY_FAILED"),
        ("save", "ARTICLE_SAVE_FAILED"),
    ],
)
def test_worker_projects_stable_provider_evidence_and_save_failures(
    monkeypatch, failure_kind, expected_code
):
    import writing.article_generator_service as module
    import writing.llm_utils as llm_utils
    from writing.article_generator_service import ArticleGeneratorService

    class Cursor:
        rowcount = 1

        def execute(self, sql, params=()):
            self.last_sql = sql

        def fetchone(self):
            return {"writing_started_at": "lease-2"}

    class Connection:
        def __init__(self):
            self.cur = Cursor()

        def cursor(self):
            return self.cur

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    fake_db = types.ModuleType("db.diagnosis_db")
    fake_db.get_connection = Connection
    monkeypatch.setitem(sys.modules, "db.diagnosis_db", fake_db)
    monkeypatch.setattr(llm_utils, "get_api_key_for_provider", lambda provider: "test-key")
    monkeypatch.setattr(module, "get_fallback_llm_config", lambda: ("", "", "", ""))

    service = ArticleGeneratorService(quote_id=1, brand_name="Brand", industry="Industry")

    async def no_project_update():
        return None

    monkeypatch.setattr(service, "_update_project_status", no_project_update)
    calls = {"generate": 0}

    async def generate(*args, **kwargs):
        calls["generate"] += 1
        if failure_kind == "provider":
            raise TimeoutError("provider secret and stack")
        if failure_kind == "evidence":
            from writing.evidence_precision_policy import (
                EvidencePrecisionAssessment,
                EvidencePrecisionFinding,
                EvidencePrecisionViolation,
            )

            raise EvidencePrecisionViolation(
                EvidencePrecisionAssessment(
                    version="test",
                    hard=(EvidencePrecisionFinding("missing", "hard", "blocked"),),
                )
            )
        return {"title": "标题", "content": "正文" * 150, "style": "buying_guide"}

    async def save(*args, **kwargs):
        raise OSError("database password and stack")

    monkeypatch.setattr(service, "_generate_validated_with_rewrite_once", generate)
    monkeypatch.setattr(service, "_save_article", save)
    results = asyncio.run(
        service.generate_articles(
            [{"id": 8, "title": "标题", "keyword": "关键词", "_writing_started_at": "lease-1"}],
            llm_override={"provider": "dashscope", "model": "model"},
        )
    )
    assert results[0]["error"] == expected_code
    assert results[0]["failure"]["code"] == expected_code
    assert "secret" not in results[0]["failure"]["message"]
    assert calls["generate"] == 1


def test_model_self_reported_length_is_removed_and_ssot_is_recomputed():
    cleaned = clean_llm_article("# 标题\n\n可信正文。\n\n全文共 9,999 字。")
    assert "9,999" not in cleaned
    assert "可信正文" in cleaned

    service_source = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    assert "count_effective_chars(_content)" in service_source
    assert "SELECT COALESCE(MAX(version),0) AS max_version" in service_source


def test_precision_advisory_never_silently_prunes_markdown_table():
    """[返修 C16 2026-08-11 · §0 裁决一] 本锁原断言「pruner 恒不删」——
    那正是"看着有兜底实际零执行"的僵尸层。pruner 已整体拆除,本锁改为:
    ① 删除机器不存在(含换名复活探测);② 表格/商业内容过评估后一个字不动
    (裁决一:客户与检索事实不删不降级,评估只产 advisory)。"""
    assert not hasattr(evidence_precision_policy, "prune_unsupported_precision_blocks")

    content = """# 核验清单

这是一段有信息增益的安全说明，项目团队应按来源逐项确认。

| 项目 | 结论 |
| --- | --- |
| 周期 | 通常小于2小时 |
| 证据 | 待供应商确认 |
"""
    assessment = evidence_precision_policy.evaluate_evidence_precision(content, {})
    assert assessment.passed, "评估产生了硬门 —— advisory 化契约被破坏"
    # 评估是只读的:不存在任何返回"改写后正文"的删除通道
    assert not hasattr(evidence_precision_policy, "prune_unsupported_blocks")


def test_refund_states_are_human_readable_and_block_duplicate_charge():
    assert refund_status_message("refunded") == "退款已完成"
    assert "请勿重复提交" in refund_status_message("pending_recovery")

    server = (ROOT / "server.py").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx").read_text(encoding="utf-8")
    assert "ARTICLE_REFUND_PENDING" in server
    assert "generation_request_id=%s" in server
    assert "NOT IN ('refunded','released')" in server
    assert "['refunded', 'released'].includes" in frontend
    assert "安全重试" in frontend
    assert "selectedProjectRef.current?.id !== projectId" in frontend


def test_migration_is_additive_and_legacy_compatible():
    sql = (ROOT / "scripts" / "migration_article_generation_task_state_2026_07_21.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS generation_revision INTEGER NOT NULL DEFAULT 0" in sql
    assert "article_generation_revision_events" in sql
    assert "UNIQUE (request_id,topic_id)" in sql
    assert "pg_get_constraintdef(oid, TRUE)" in sql
    assert "VALIDATE CONSTRAINT topics_generation_revision_nonnegative_ck" in sql
    assert "article_generation_revision_events_immutable_guard" in sql
    assert "BEFORE UPDATE OR DELETE ON public.article_generation_revision_events" in sql
    assert "revision immutable trigger" in sql
    assert "feature_flags" not in sql
