import copy
import json
import sys
import types

import pytest


def test_live_generation_shadow_recorder_is_noop_by_default(tmp_path):
    from writing.shadow_only_injection import record_live_generation_shadow_artifact

    result = record_live_generation_shadow_artifact(
        quote_id=123,
        brand_name="岱林生物",
        industry="制药装备",
        topic={"id": 456, "title": "采购指南", "style_code": "buying_guide"},
        article={"id": 789, "title": "采购指南", "content": "一篇内部 shadow 文章。", "style": "buying_guide"},
        output_root=tmp_path / "admin_shadow_runs",
    )

    assert result["status"] == "disabled"
    assert result["artifact_written"] is False
    assert not (tmp_path / "admin_shadow_runs").exists()


def test_live_generation_shadow_recorder_writes_admin_local_artifact_when_enabled(tmp_path):
    from writing.shadow_only_injection import load_shadow_injection_config, record_live_generation_shadow_artifact

    result = record_live_generation_shadow_artifact(
        quote_id=123,
        brand_name="岱林生物",
        industry="制药装备",
        topic={"id": 456, "title": "采购指南", "style_code": "buying_guide"},
        article={"id": 789, "title": "采购指南", "content": "一篇内部 shadow 文章。", "style": "buying_guide"},
        output_root=tmp_path / "admin_shadow_runs",
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
            }
        ),
    )

    assert result["status"] == "recorded"
    assert result["artifact_written"] is True
    assert result["customer_output_allowed"] is False
    assert result["run_id"].startswith("live_quote123_topic456_article789_")
    assert "guard_missing" in result["blockers"]
    assert "semantic_missing" in result["blockers"]

    manifest_path = tmp_path / "admin_shadow_runs" / result["run_id"] / "shadow_run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["customer_output_allowed"] is False
    assert manifest["case_id"] == "LIVE-Q123-T456-A789"
    assert manifest["source_summary"]["evidence_mode"] == "no_evidence"
    assert manifest["quality_summary"]["guard_decision"] == "missing"
    assert manifest["quality_summary"]["semantic_decision"] == "missing"


def test_live_generation_shadow_recorder_uses_unique_run_ids_for_regeneration(tmp_path):
    from writing.shadow_only_injection import load_shadow_injection_config, record_live_generation_shadow_artifact

    config = load_shadow_injection_config(
        env={
            "R6H_SHADOW_INJECTION_ENABLED": "true",
            "R6H_SHADOW_ONLY": "true",
        }
    )
    kwargs = {
        "quote_id": 123,
        "brand_name": "岱林生物",
        "industry": "制药装备",
        "topic": {"id": 456, "title": "采购指南", "style_code": "buying_guide"},
        "article": {"id": 789, "title": "采购指南", "content": "一篇内部 shadow 文章。", "style": "buying_guide"},
        "output_root": tmp_path / "admin_shadow_runs",
        "config": config,
    }

    first = record_live_generation_shadow_artifact(**kwargs)
    second = record_live_generation_shadow_artifact(**kwargs)

    assert first["status"] == "recorded"
    assert second["status"] == "recorded"
    assert first["run_id"] != second["run_id"]
    assert first["run_id"].startswith("live_quote123_topic456_article789_")
    assert second["run_id"].startswith("live_quote123_topic456_article789_")
    assert (tmp_path / "admin_shadow_runs" / first["run_id"] / "shadow_run_manifest.json").exists()
    assert (tmp_path / "admin_shadow_runs" / second["run_id"] / "shadow_run_manifest.json").exists()


def test_article_generator_shadow_hook_does_not_mutate_article_or_write_when_disabled(tmp_path, monkeypatch):
    from writing.article_generator_service import ArticleGeneratorService

    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(tmp_path / "admin_shadow_runs"))
    service = ArticleGeneratorService(quote_id=123, brand_name="岱林生物", industry="制药装备")
    topic = {"id": 456, "title": "采购指南", "style_code": "buying_guide"}
    article = {"id": 789, "title": "采购指南", "content": "一篇内部 shadow 文章。", "style": "buying_guide"}
    before = copy.deepcopy(article)

    result = service._record_shadow_only_article(topic, article)

    assert result["artifact_written"] is False
    assert article == before
    assert not (tmp_path / "admin_shadow_runs").exists()


def test_article_generator_shadow_hook_swallows_recorder_errors(monkeypatch):
    import writing.shadow_only_injection as shadow
    from writing.article_generator_service import ArticleGeneratorService

    def boom(**kwargs):
        raise RuntimeError("shadow boom")

    monkeypatch.setattr(shadow, "record_live_generation_shadow_artifact", boom)
    service = ArticleGeneratorService(quote_id=123, brand_name="岱林生物", industry="制药装备")
    article = {"id": 789, "title": "采购指南", "content": "一篇内部 shadow 文章。", "style": "buying_guide"}
    before = copy.deepcopy(article)

    result = service._record_shadow_only_article(
        {"id": 456, "title": "采购指南", "style_code": "buying_guide"},
        article,
    )

    assert result["status"] == "error"
    assert result["artifact_written"] is False
    assert result["customer_output_allowed"] is False
    assert "shadow boom" in result["error"]
    assert article == before


def test_article_generator_shadow_hook_records_admin_error_event_when_enabled(tmp_path, monkeypatch):
    import writing.shadow_only_injection as shadow
    from writing.article_generator_service import ArticleGeneratorService

    def boom(**kwargs):
        raise RuntimeError("shadow boom with sensitive body")

    monkeypatch.setenv("R6H_SHADOW_INJECTION_ENABLED", "true")
    monkeypatch.setenv("R6H_SHADOW_ONLY", "true")
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(tmp_path / "admin_shadow_runs"))
    monkeypatch.setattr(shadow, "record_live_generation_shadow_artifact", boom)
    service = ArticleGeneratorService(quote_id=123, brand_name="岱林生物", industry="制药装备")
    article = {"id": 789, "title": "采购指南", "content": "SENSITIVE_BODY", "style": "buying_guide"}

    result = service._record_shadow_only_article(
        {"id": 456, "title": "采购指南", "style_code": "buying_guide"},
        article,
    )

    assert result["status"] == "error"
    assert result["artifact_written"] is False
    assert result["customer_output_allowed"] is False
    assert result["error_event_written"] is True

    error_log = tmp_path / "admin_shadow_runs" / "recorder_errors.jsonl"
    assert error_log.exists()
    lines = error_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["quote_id"] == "123"
    assert event["topic_id"] == "456"
    assert event["article_id"] == "789"
    assert event["error_type"] == "RuntimeError"
    assert "error_message_sha256" in event
    assert event["error_message_length"] == len("shadow boom with sensitive body")
    assert "shadow boom" not in lines[0]
    assert "SENSITIVE_BODY" not in lines[0]

    listing = shadow.list_shadow_run_artifacts(tmp_path / "admin_shadow_runs")
    assert listing["observability"]["recorder_error_count"] == 1
    assert listing["observability"]["top_recorder_error_types"] == [
        {"label": "RuntimeError", "count": 1}
    ]


@pytest.mark.asyncio
async def test_generate_articles_records_shadow_after_successful_save_without_changing_result(monkeypatch):
    from writing.article_generator_service import ArticleGeneratorService

    shadow_calls = []
    generated_content = "这是一篇已经生成的文章。" * 40

    async def fake_generate(self, topic, api_url, api_key, model):
        return {
            "topic_id": topic["id"],
            "title": topic["title"],
            "content": generated_content,
            "word_count": len(generated_content),
            "style": "buying_guide",
        }

    async def fake_save(self, topic, article):
        return 789

    async def fake_update_status(self):
        return None

    def fake_record(self, topic, article):
        shadow_calls.append({"topic": copy.deepcopy(topic), "article": copy.deepcopy(article)})
        return {"status": "disabled", "artifact_written": False, "customer_output_allowed": False}

    class FakeCursor:
        def execute(self, *args, **kwargs):
            return None

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(ArticleGeneratorService, "_generate_validated_with_rewrite_once", fake_generate)
    monkeypatch.setattr(ArticleGeneratorService, "_save_article", fake_save)
    monkeypatch.setattr(ArticleGeneratorService, "_update_project_status", fake_update_status)
    monkeypatch.setattr(ArticleGeneratorService, "_record_shadow_only_article", fake_record)
    monkeypatch.setattr("writing.llm_utils.get_api_key_for_provider", lambda provider: "test-key")
    monkeypatch.setitem(
        sys.modules,
        "db.diagnosis_db",
        types.SimpleNamespace(get_connection=lambda: FakeConnection()),
    )

    service = ArticleGeneratorService(quote_id=123, brand_name="岱林生物", industry="制药装备")
    results = await service.generate_articles(
        [{"id": 456, "title": "采购指南", "style_code": "buying_guide"}],
        max_concurrent=1,
        llm_override={"provider": "dashscope", "model": "qwen-test"},
    )

    assert results == [
            {
                "topic_id": 456,
                "title": "采购指南",
                "content": generated_content,
                "word_count": len(generated_content),
                "style": "buying_guide",
                "id": 789,
        }
    ]
    assert shadow_calls == [
        {
            "topic": {
                "id": 456,
                "title": "采购指南",
                "style_code": "buying_guide",
                "publication_profile": "standard",
                "_requested_add_images": True,
                "_requested_add_contact": False,
                "_effective_add_images": True,
                "_effective_add_contact": False,
            },
            "article": {
                "topic_id": 456,
                "title": "采购指南",
                "content": generated_content,
                "word_count": len(generated_content),
                "style": "buying_guide",
                "id": 789,
            },
        }
    ]
