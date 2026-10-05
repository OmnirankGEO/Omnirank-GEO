"""Text edit → immutable publishing revision, exercised on a throwaway PostgreSQL 16.

Set GEO_FLOW_TEST_DSN explicitly. Every fixture rolls back; no provider or wallet calls.
The seed post is read only, and each case owns a newly inserted post inside its transaction.
"""
import os
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from services.geo_douyin.post_revisions import RevisionConflict, save_text_revision, stage_revision


@pytest.fixture
def fixture():
    dsn = os.environ.get("GEO_FLOW_TEST_DSN")
    if not dsn:
        pytest.skip("explicit local PostgreSQL test DSN required")
    assert urlparse(dsn).hostname in {"127.0.0.1", "localhost"}, "local disposable database only"
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute("SHOW server_version_num")
        assert int(cur.fetchone()["server_version_num"]) // 10000 == 16
        cur.execute("SELECT id FROM users WHERE username='codex_geo_flow'")
        actor = cur.fetchone()["id"]
        cur.execute("""INSERT INTO geo_douyin_posts(created_by,title,body_text,hashtags,oss_keys,cover_oss_key,status)
                       VALUES(%s,'old title','old body','["old"]'::jsonb,'["fixture/a.png"]'::jsonb,'fixture/a.png','ready') RETURNING id""", (actor,))
        post = cur.fetchone()["id"]
        revision = stage_revision(cur, geo_post_id=post, created_by=actor, operation_kind="create",
            title="old title", body="old body", hashtags=["old"],
            cards_snapshot=[{"asset": "fixture/a.png", "role": "cover"}],
            asset_manifest={"oss_keys": ["fixture/a.png"], "cover_oss_key": "fixture/a.png", "card_count": 1},
            topic_snapshot_hash="a" * 64, render_input_hash="b" * 64, style_catalog_version="fixture-v1")
        rid = revision["post_revision_id"]
        cur.execute("UPDATE geo_douyin_post_revisions SET status='active',activated_at=now() WHERE post_revision_id=%s", (rid,))
        cur.execute("UPDATE geo_douyin_posts SET active_revision_id=%s WHERE id=%s", (rid, post))
        yield cur, post, actor, rid
    finally:
        conn.rollback()
        conn.close()


def read(cur, post):
    cur.execute("""SELECT p.title,p.body_text,p.hashtags,p.oss_keys,p.active_revision_id,r.*
                   FROM geo_douyin_posts p LEFT JOIN geo_douyin_post_revisions r ON r.post_revision_id=p.active_revision_id
                   WHERE p.id=%s""", (post,))
    return dict(cur.fetchone())


def save(f, **kwargs):
    cur, post, actor, _ = f
    return save_text_revision(cur, geo_post_id=post, created_by=actor, **kwargs)


def test_saved_copy_is_the_version_publish_reads(fixture):
    cur, post, _, before_id = fixture
    new_id = save(fixture, title="new title", body="new body", hashtags=["new"], expected_revision_id=before_id, check_revision=True)
    row = read(cur, post)
    assert new_id != before_id
    assert row["operation_kind"] == "edit" and row["base_revision_id"] == before_id
    assert row["title"] == "new title" and row["body"] == row["body_text"] == "new body"
    assert row["hashtags"] == ["new"]
    assert row["asset_manifest"]["oss_keys"] == row["oss_keys"] == ["fixture/a.png"]
    assert row["cards_snapshot"] == [{"asset": "fixture/a.png", "role": "cover"}]
    assert row["topic_snapshot_hash"] == "a" * 64 and row["render_input_hash"] == "b" * 64
    cur.execute("SELECT title,body,hashtags,status FROM geo_douyin_post_revisions WHERE post_revision_id=%s", (before_id,))
    assert dict(cur.fetchone()) == {"title": "old title", "body": "old body", "hashtags": ["old"], "status": "superseded"}


def test_idempotent_retry_does_not_create_another_version(fixture):
    rid = save(fixture, title="new", expected_revision_id=fixture[3], check_revision=True)
    assert save(fixture, title="new", expected_revision_id=fixture[3], check_revision=True) == rid
    fixture[0].execute("SELECT count(*) AS n FROM geo_douyin_post_revisions WHERE geo_post_id=%s", (fixture[1],))
    assert fixture[0].fetchone()["n"] == 2


def test_stale_second_editor_does_not_overwrite(fixture):
    save(fixture, title="editor A")
    with pytest.raises(RevisionConflict, match="已有更新"):
        save(fixture, title="editor B", expected_revision_id=fixture[3], check_revision=True)
    assert read(fixture[0], fixture[1])["title"] == "editor A"


def test_explicit_null_is_cas_not_omitted(fixture):
    with pytest.raises(RevisionConflict, match="已有更新"):
        save(fixture, title="new", expected_revision_id=None, check_revision=True)


def test_partial_edit_preserves_other_fields(fixture):
    save(fixture, title="only title")
    row = read(fixture[0], fixture[1])
    assert row["body"] == row["body_text"] == "old body" and row["hashtags"] == ["old"]


def test_intentional_empty_body_and_tags_are_not_restored(fixture):
    save(fixture, body="", hashtags=[])
    row = read(fixture[0], fixture[1])
    assert row["body"] == row["body_text"] == "" and row["hashtags"] == []


def test_preexisting_post_revision_drift_is_repaired(fixture):
    cur, post, _, _ = fixture
    cur.execute("UPDATE geo_douyin_posts SET title='already saved but not frozen' WHERE id=%s", (post,))
    save(fixture, title="already saved but not frozen")
    cur.execute("SELECT title FROM geo_douyin_post_revisions WHERE post_revision_id=(SELECT active_revision_id FROM geo_douyin_posts WHERE id=%s)", (post,))
    assert cur.fetchone()["title"] == "already saved but not frozen"


@pytest.mark.parametrize("status", ["pending", "running"])
def test_active_generation_preserves_inputs_by_refusing_overwrite(fixture, status):
    cur, post, actor, _ = fixture
    cur.execute("INSERT INTO geo_douyin_post_tasks(post_id,user_id,task_ref,status) VALUES(%s,%s,%s,%s)", (post, actor, f"edit-test-{post}", status))
    with pytest.raises(RevisionConflict, match="图片仍在生成"):
        save(fixture, title="new")
    assert read(cur, post)["title"] == "old title"


def test_legacy_no_revision_can_save_without_inventing_asset_lineage(fixture):
    cur, post, _, _ = fixture
    cur.execute("UPDATE geo_douyin_posts SET active_revision_id=NULL WHERE id=%s", (post,))
    assert save(fixture, title="legacy saved", expected_revision_id=None, check_revision=True) is None
    cur.execute("SELECT title,active_revision_id FROM geo_douyin_posts WHERE id=%s", (post,))
    assert dict(cur.fetchone()) == {"title": "legacy saved", "active_revision_id": None}


def test_published_post_stays_immutable(fixture):
    cur, post, _, _ = fixture
    cur.execute("UPDATE geo_douyin_posts SET status='published' WHERE id=%s", (post,))
    with pytest.raises(RevisionConflict, match="已经发布"):
        save(fixture, title="new")
    assert read(cur, post)["title"] == "old title"
