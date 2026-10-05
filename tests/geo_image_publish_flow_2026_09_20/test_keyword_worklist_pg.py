"""Existing paid/confirmed inputs projected without creation, fee or text dedup.

Every test uses its own rows and rolls back on the explicitly selected local PG16.
"""
import os
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from services.geo_douyin.keyword_worklist import read_keyword_worklist


@pytest.fixture
def worklist():
    dsn = os.environ.get("GEO_FLOW_TEST_DSN")
    if not dsn:
        pytest.skip("explicit local PostgreSQL test DSN required")
    assert urlparse(dsn).hostname in {"127.0.0.1", "localhost"}
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    try:
        cur.execute("SHOW server_version_num")
        assert int(cur.fetchone()["server_version_num"]) // 10000 == 16
        cur.execute("SELECT id FROM users WHERE username='codex_geo_flow'")
        actor = cur.fetchone()["id"]
        cur.execute("INSERT INTO brands(name,owner_user_id,is_test) VALUES('rollback keyword worklist',%s,TRUE) RETURNING id", (actor,))
        brand = cur.fetchone()["id"]

        def keyword(word="同一个业务词", status="paid", required=1, core=True):
            cur.execute("INSERT INTO quotes(brand_id,owner_user_id,status,city) VALUES(%s,%s,%s,'测试城市') RETURNING id", (brand, actor, status))
            quote = cur.fetchone()["id"]
            cur.execute("INSERT INTO confirmed_keywords(quote_id,keyword,required_articles,is_core) VALUES(%s,%s,%s,%s) RETURNING id", (quote, word, required, core))
            return cur.fetchone()["id"]

        def post(ck, status, tenant=None, creator=None):
            cur.execute("""INSERT INTO geo_douyin_posts(brand_id,created_by,tenant_owner_user_id,confirmed_keyword_id,status,title)
                           VALUES(%s,%s,%s,%s,%s,'标题可以与来源词不同') RETURNING id""",
                        (brand, creator or actor, tenant, ck, status))
            return cur.fetchone()["id"]

        def read(**kwargs):
            return read_keyword_worklist(cur, brand_id=brand, tenant_owner_user_id=kwargs.pop("tenant", actor), **kwargs)

        yield cur, keyword, post, read, actor, brand
    finally:
        conn.rollback()
        conn.close()


def test_same_text_across_quotes_and_statuses_keeps_both_identities(worklist):
    _, keyword, _, read, _, _ = worklist
    paid, confirmed = keyword(), keyword(status="confirmed")
    keyword(status="draft")
    keyword(core=False)
    page = read()
    assert page["total"] == 2
    assert [(r["confirmed_keyword_id"], r["quote_status"]) for r in page["keywords"]] == [(paid, "paid"), (confirmed, "confirmed")]
    assert len({r["quote_id"] for r in page["keywords"]}) == 2


def test_pagination_beyond_old_30_50_limits_never_drops_same_words(worklist):
    _, keyword, _, read, _, _ = worklist
    ids = [keyword() for _ in range(205)]
    first, second = read(limit=200), read(limit=200, offset=200)
    assert first["total"] == second["total"] == 205
    assert len(first["keywords"]) == 200 and len(second["keywords"]) == 5
    actual = [r["confirmed_keyword_id"] for r in first["keywords"] + second["keywords"]]
    assert actual == sorted(ids, reverse=True)


@pytest.mark.parametrize("status,projected,count", [
    ("ready", "done", 1), ("publishing", "done", 1), ("published", "done", 1),
    ("generating", "making", 0), ("completing", "making", 0),
    ("draft", "incomplete", 0), ("failed", "failed", 0), ("new_unknown", "unknown", 0),
])
def test_real_post_state_and_precise_work_reference(worklist, status, projected, count):
    _, keyword, post, read, _, _ = worklist
    ck, another = keyword(), keyword(status="confirmed")
    pid = post(ck, status)
    rows = {r["confirmed_keyword_id"]: r for r in read()["keywords"]}
    assert rows[ck]["production_status"] == projected
    assert rows[ck]["post_id"] == pid and rows[ck]["produced_count"] == count
    assert rows[another]["production_status"] == "pending" and rows[another]["post_id"] is None


def test_zero_allocated_content_is_preserved_not_defaulted_to_one(worklist):
    _, keyword, _, read, _, _ = worklist
    keyword(required=0)
    assert read()["keywords"][0]["required_articles"] == 0


def test_work_projection_matches_post_list_tenant_scope(worklist):
    _, keyword, post, read, actor, _ = worklist
    ck = keyword()
    foreign = post(ck, "generating", tenant=actor + 1000)
    assert read()["keywords"][0]["post_id"] is None
    own = post(ck, "ready", tenant=actor, creator=actor + 1000)
    row = read()["keywords"][0]
    assert row["post_id"] == own and row["produced_count"] == 1
    assert read(tenant=None)["keywords"][0]["post_id"] == foreign


def test_new_draft_does_not_hide_existing_usable_work(worklist):
    _, keyword, post, read, _, _ = worklist
    ck = keyword()
    ready = post(ck, "ready")
    post(ck, "draft")
    assert read()["keywords"][0]["post_id"] == ready


def test_queued_draft_has_real_task_and_pollable_making_state(worklist):
    cur, keyword, post, read, actor, _ = worklist
    ck = keyword()
    post(ck, "ready")
    queued = post(ck, "draft")
    cur.execute("INSERT INTO geo_douyin_post_tasks(post_id,user_id,task_ref,status) VALUES(%s,%s,%s,'pending')", (queued, actor, f"worklist-rollback-{queued}"))
    row = read()["keywords"][0]
    assert row["post_id"] == queued and row["production_status"] == "making"
    assert row["has_active_task"] is True and row["produced_count"] == 1


def test_pending_manual_angle_keeps_title_but_never_auto_writes(worklist):
    cur, keyword, _, read, actor, brand = worklist
    ck = keyword()
    cur.execute("""INSERT INTO geo_douyin_topics(brand_id,confirmed_keyword_id,keyword,title,created_by,source)
                   VALUES(%s,%s,'同一个业务词','新的标题角度',%s,'user') RETURNING id""", (brand, ck, actor))
    topic = cur.fetchone()["id"]
    class ReadOnlyCursor:
        def execute(self, sql, params):
            assert sql.lstrip().upper().startswith("SELECT"), "worklist reads must not mutate"
            return cur.execute(sql, params)

        def __getattr__(self, name):
            return getattr(cur, name)

    for _ in range(2):
        row = read_keyword_worklist(ReadOnlyCursor(), brand_id=brand, tenant_owner_user_id=actor)["keywords"][0]
        assert row["topic_id"] == topic and row["topic_title"] == "新的标题角度"
    cur.execute("SELECT count(*) AS n FROM geo_douyin_topics WHERE brand_id=%s", (brand,))
    assert cur.fetchone()["n"] == 1
    cur.execute("SELECT count(*) AS n FROM geo_douyin_posts WHERE brand_id=%s", (brand,))
    assert cur.fetchone()["n"] == 0


def test_archived_keywords_and_deleted_quotes_are_not_offered(worklist):
    cur, keyword, _, read, _, _ = worklist
    archived, deleted = keyword(), keyword()
    cur.execute("UPDATE confirmed_keywords SET archived_at=now() WHERE id=%s", (archived,))
    cur.execute("UPDATE quotes SET deleted_at=now() WHERE id=(SELECT quote_id FROM confirmed_keywords WHERE id=%s)", (deleted,))
    assert read()["keywords"] == []


def test_read_failure_propagates_instead_of_empty_success(worklist):
    cur, _, _, _, actor, _ = worklist
    cur.close()
    with pytest.raises(psycopg2.InterfaceError):
        read_keyword_worklist(cur, brand_id=1, tenant_owner_user_id=actor)
