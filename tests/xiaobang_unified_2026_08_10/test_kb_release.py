from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from db import kb_db
from tools import xiaobang_kb_indexer as indexer


TEST_URL = os.environ["TEST_DATABASE_URL"]
assert "geo_test" in TEST_URL and "xiaobang_test_only" in TEST_URL


def _connection():
    conn = psycopg2.connect(TEST_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    cur = conn.cursor()
    cur.execute("SET search_path TO xiaobang_unified_test")
    return conn


@pytest.fixture(scope="module", autouse=True)
def kb_schema():
    conn = psycopg2.connect(TEST_URL)
    try:
        cur = conn.cursor()
        cur.execute("CREATE SCHEMA IF NOT EXISTS xiaobang_unified_test")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS xiaobang_unified_test.kb_chunks (
              id BIGSERIAL PRIMARY KEY,
              source_type VARCHAR(16) NOT NULL,
              source_slug VARCHAR(128) NOT NULL,
              source_title TEXT NOT NULL,
              section_title TEXT,
              content TEXT NOT NULL,
              route VARCHAR(256),
              route_label VARCHAR(64),
              category VARCHAR(32) NOT NULL DEFAULT 'general',
              is_admin_only BOOLEAN NOT NULL DEFAULT FALSE,
              token_keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
              embedding JSONB,
              origin VARCHAR(8) NOT NULL DEFAULT 'manual',
              visible_to VARCHAR(16) NOT NULL DEFAULT 'both',
              created_at TIMESTAMP DEFAULT NOW(),
              updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("DELETE FROM xiaobang_unified_test.kb_chunks")
        conn.commit()
    finally:
        conn.close()


def _row(slug: str, title: str = "现役帮助"):
    return {
        "source_type": "doc",
        "source_slug": slug,
        "source_title": title,
        "content": "现役主流程说明",
        "route": "/diagnosis/new",
        "route_label": "去品牌体检",
        "category": "onboarding",
        "token_keywords": ["品牌体检"],
    }


def test_release_rebuild_is_atomic_idempotent_and_carries_manifest(monkeypatch):
    monkeypatch.setattr(kb_db, "_get_conn", _connection)
    conn = _connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO kb_chunks (source_type, source_slug, source_title, content) "
            "VALUES ('faq','faq_old','旧导航','在左边栏销售分组里点品牌体检')"
        )
        conn.commit()
    finally:
        conn.close()

    manifest = {
        "content_version": "xiaobang-kb-v2",
        "release_sha": "a" * 40,
        "operation_registry_version": "operation-registry-v2",
        "source_hashes": {"doc": "hash-doc", "faq": "hash-faq", "preset": "hash-preset"},
    }
    for _ in range(2):
        result = kb_db.replace_chunks_transactionally(
            source_types=("doc", "faq", "preset"), chunks=[_row("doc_new")], manifest=manifest
        )
        assert result["inserted"] == 1

    conn = _connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT source_slug, content FROM kb_chunks ORDER BY source_slug")
        rows = cur.fetchall()
    finally:
        conn.close()
    by_slug = {row["source_slug"]: row["content"] for row in rows}
    assert set(by_slug) == {kb_db.KB_RELEASE_MANIFEST_SLUG, "doc_new"}
    assert "release_sha" in by_slug[kb_db.KB_RELEASE_MANIFEST_SLUG]
    assert all("销售分组" not in row["content"] for row in rows)


def test_failed_replacement_rolls_back_the_old_release(monkeypatch):
    monkeypatch.setattr(kb_db, "_get_conn", _connection)
    with pytest.raises(ValueError):
        kb_db.replace_chunks_transactionally(
            source_types=("doc",),
            chunks=[_row("would_be_rolled_back"), {**_row("wrong"), "source_type": "faq"}],
        )
    conn = _connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT source_slug FROM kb_chunks WHERE source_type = 'doc'")
        slugs = {row["source_slug"] for row in cur.fetchall()}
    finally:
        conn.close()
    assert slugs == {"doc_new"}


def test_old_faq_cannot_override_registry_route_even_if_source_still_exists():
    old_answer = "在左边栏销售分组里点品牌体检"
    assert indexer.is_stale_knowledge("品牌体检在哪里", old_answer)
    route, _label = indexer._faq_keyword_route("品牌体检在哪里", old_answer)
    assert route == "/diagnosis/new"


def test_runtime_stale_chunk_filter_rejects_retired_ia():
    from api.xiaobang_api import _is_stale_knowledge_chunk

    assert _is_stale_knowledge_chunk({
        "source_title": "旧 FAQ", "content": "制作 → 发布管理", "section_title": "",
    })
    assert not _is_stale_knowledge_chunk({
        "source_title": "现役", "content": "品牌体检 → 报价 → AI 创作 → 发布投放", "section_title": "",
    })


def test_runtime_knowledge_sources_have_no_retired_grouping_strings():
    root = Path(__file__).resolve().parents[2]
    paths = [
        root / "agents" / "xiaobang_canned_faq.py",
        root / "agents" / "xiaobang_presets.py",
    ]
    retired = ("销售分组", "运营分组", "制作 → 发布管理", "运营 → 排名监测")
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not any(term in text for term in retired), path


def test_release_sha_uses_image_file_when_git_metadata_is_absent(monkeypatch):
    release_sha = "b" * 40
    original_read_text = Path.read_text
    monkeypatch.delenv("RELEASE_SHA", raising=False)

    def image_release(self, *args, **kwargs):
        if self == Path("/app/RELEASE_SHA"):
            return release_sha
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", image_release)
    monkeypatch.setattr(
        indexer.subprocess,
        "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("no git")),
    )
    assert indexer._current_release_sha() == release_sha


def test_release_sha_fails_before_replacement_instead_of_writing_unknown(monkeypatch):
    monkeypatch.delenv("RELEASE_SHA", raising=False)
    original_read_text = Path.read_text

    def no_image_release(self, *args, **kwargs):
        if self == Path("/app/RELEASE_SHA"):
            raise FileNotFoundError("no release file")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_image_release)
    monkeypatch.setattr(
        indexer.subprocess,
        "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("no git")),
    )
    with pytest.raises(RuntimeError, match="release SHA"):
        indexer._current_release_sha()


def test_deploy_command_passes_sha_and_success_marker_matches_indexer():
    root = Path(__file__).resolve().parents[2]
    deploy = (root / "scripts" / "deploy-blue-green.sh").read_text(encoding="utf-8")
    indexer_source = (root / "tools" / "xiaobang_kb_indexer.py").read_text(encoding="utf-8")
    assert '--release-sha \"$DEPLOY_SHA\"' in deploy
    assert "索引完成" in deploy
    assert "索引完成 · release 切换完成" in indexer_source
