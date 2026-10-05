"""Publication list filters the full scoped denominator before pagination."""
import os
from urllib.parse import urlparse
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor, Json
import pytest

from db.geo_douyin_db import read_publication_page
from services.geo_douyin.publish_convergence import pending_confirm_display


@pytest.fixture
def publication_page():
    dsn = os.environ.get("GEO_FLOW_TEST_DSN")
    if not dsn:
        pytest.skip("explicit local PG16 DSN required")
    assert urlparse(dsn).hostname in {"127.0.0.1", "localhost"}
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    try:
        cur.execute("SHOW server_version_num")
        assert int(cur.fetchone()["server_version_num"]) // 10000 == 16
        cur.execute("SELECT id FROM users WHERE username='codex_geo_flow'")
        actor = cur.fetchone()["id"]
        cur.execute("INSERT INTO brands(name,owner_user_id,is_test) VALUES('rollback publication page',%s,TRUE) RETURNING id", (actor,))
        brand = cur.fetchone()["id"]

        def post(status="ready", publish="", revision=1, keys=None, tenant=None, deleted=False, other_brand=False):
            cur.execute("""INSERT INTO geo_douyin_posts(brand_id,created_by,tenant_owner_user_id,status,
                publish_status,active_revision_id,oss_keys,deleted_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN NOW() ELSE NULL END,NOW()-INTERVAL '30 hours') RETURNING id""",
                (None if other_brand else brand, actor, tenant, status, publish, revision,
                 Json(["synthetic/image.png"] if keys is None else keys), deleted))
            return cur.fetchone()["id"]

        def read(**kwargs):
            return read_publication_page(cur, brand_id=brand, tenant_owner_user_id=actor, **kwargs)
        yield post, read, actor, cur
    finally:
        conn.rollback()
        conn.close()


def test_later_ready_work_is_not_hidden_by_first_100_generating(publication_page):
    post, read, _, cur = publication_page
    ready = post()
    for _ in range(105):
        post(status="generating")
    for _ in range(24):
        post()
    a, b = read(bucket="unpublished", limit=20), read(bucket="unpublished", limit=20, offset=20)
    assert a["counts"]["all"] == 130 and a["counts"]["unpublished"] == 25
    assert a["counts"]["incomplete"] == 105
    assert len(a["posts"]) == 20 and len(b["posts"]) == 5
    assert a["has_more"] and not b["has_more"]
    ids = [r["id"] for r in a["posts"] + b["posts"]]
    assert len(set(ids)) == 25 and ready in ids
    cur.execute("SELECT COUNT(*) AS n FROM geo_douyin_posts WHERE id=ANY(%s)", (ids,))
    assert cur.fetchone()["n"] == 25  # reads did not create or replace works


@pytest.mark.parametrize("status,publish,revision,keys,bucket", [
    ("ready", "", 1, ["image"], "unpublished"),
    ("ready", None, 1, ["image"], "unpublished"),
    ("draft", "", 1, ["image"], "incomplete"),
    ("completing", "", 1, ["image"], "incomplete"),
    ("failed", "", 1, ["image"], "incomplete"),
    ("ready", "", None, ["image"], "incomplete"),
    ("ready", "", 1, [], "incomplete"),
    ("ready", "", 1, ["image", ""], "incomplete"),
    ("ready", "", 1, [None], "incomplete"),
    ("ready", "", 1, [False], "incomplete"),
    ("ready", "publishing", 1, ["image"], "inflight"),
    ("ready", "self_reported_unverified", 1, ["image"], "inflight"),
    ("ready", "published", 1, ["image"], "published"),
    ("ready", "measured", 1, ["image"], "published"),
    ("ready", "failed", 1, ["image"], "failed"),
    ("ready", "new_unknown", 1, ["image"], "unknown"),
])
def test_bucket_projection_never_relabels_inflight_or_missing_assets(publication_page, status, publish, revision, keys, bucket):
    post, read, _, _ = publication_page
    pid = post(status, publish, revision, keys)
    result = read(bucket=bucket)
    assert result["total"] == 1 and result["posts"][0]["id"] == pid
    assert result["posts"][0]["publication_bucket"] == bucket
    if bucket != "unpublished":
        assert read(bucket="unpublished")["total"] == 0


def test_counts_page_share_owner_brand_and_deleted_scope(publication_page):
    post, read, actor, _ = publication_page
    visible = post(tenant=actor)
    post(tenant=actor + 9999)
    post(deleted=True)
    post(other_brand=True)
    result = read(bucket="all")
    assert result["counts"]["all"] == 1
    assert [p["id"] for p in result["posts"]] == [visible]


def test_paged_timestamps_keep_existing_pending_confirm_contract(publication_page):
    post, read, _, _ = publication_page
    post(publish="publishing")
    row = read(bucket="inflight")["posts"][0]
    assert isinstance(row["updated_at"], datetime)
    assert pending_confirm_display(row) is True


def test_empty_page_retains_full_counts(publication_page):
    post, read, _, _ = publication_page
    post()
    result = read(bucket="unpublished", offset=100)
    assert result["posts"] == [] and result["total"] == 1 and not result["has_more"]


def test_invalid_filter_does_not_fall_back_to_unfiltered_read(publication_page):
    _, read, _, _ = publication_page
    with pytest.raises(ValueError, match="发布状态"):
        read(bucket="not-a-bucket")
