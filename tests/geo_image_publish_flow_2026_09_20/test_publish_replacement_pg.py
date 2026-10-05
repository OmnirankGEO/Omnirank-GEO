"""Released failed delivery → another account, on real PG16 and the real coordinator.

All created rows roll back. No worker/provider/wallet is called here: the injected
freeze records its exact call count; independent HTTP integration verifies billing.
"""
import os
import asyncio
import uuid
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from test_saved_copy_revision_pg import fixture  # shared localhost-only rollback fixture
from test_publish_conflict_mapping import api_module
from services.geo_douyin.contract_pricing import publish_fingerprint
from services.geo_douyin.contract_funding import AUTHORITY_DIRECT
from services.geo_douyin.publish_command import PublishAttemptConflict, is_retryable
from services.geo_douyin.publish_coordinator import materialize_command, _lock_previous_attempts


def inputs(f, media_id=991011):
    _, post_id, user_id, revision_id = f
    item = {
        "item_request_id": str(uuid.uuid4()), "geo_post_id": post_id,
        "post_revision_id": revision_id, "media_id": media_id,
        "prepared_artifact_id": "1", "manifest_hash": "m" * 64,
        "title": "Saved title", "body_text": "Saved body", "image_urls": "fixture/a.png",
        "expected_price_fingerprint": publish_fingerprint(
            feature_code="media_proxy_publish", media_id=media_id, final_price_points=260,
            markup_version="fixture", resolver_version="fixture", catalog_version="fixture"),
    }
    return item, {
        "command_request_id": str(uuid.uuid4()),
        "identity": {"tenant_owner_user_id": user_id, "payer_user_id": user_id,
                     "actor_user_id": user_id, "payer_policy_snapshot": {}},
        "items": [item],
        "resolved_prices": {item["item_request_id"]: {
            "final_price_points": 260, "publish_price_fingerprint": item["expected_price_fingerprint"],
            "price_snapshot": {"final_price_points": 260}}},
        "artifacts": {item["item_request_id"]: {
            "prepared_artifact_id": 1, "post_revision_id": revision_id,
            "manifest_hash": item["manifest_hash"], "state": "ready"}},
        "account_names": {media_id: "local account"}, "daily_limit": 100,
        "settlement": {"authority": AUTHORITY_DIRECT, "payer_user_id": user_id},
    }


def materialize(f, media_id=991011, calls=None, freeze_fn=None):
    _, kwargs = inputs(f, media_id)
    calls = calls if calls is not None else []

    def freeze(**args):
        calls.append(args)
        return {**args, "freeze_id": 900000 + len(calls), "freeze_table": "legacy",
                "reserved_amount": args["amount"], "physical_split_snapshot": {"paid": args["amount"]}}

    return materialize_command(f[0], **kwargs, freeze_fn=freeze_fn or freeze)["items"][0]


def row(f, item_id):
    f[0].execute("SELECT * FROM mhz_publish_order_items WHERE id=%s", (item_id,))
    return dict(f[0].fetchone())


def failed_parent(f, state="failed", settled="released"):
    item_id = materialize(f)["order_item_id"]
    # Exact old worker output: terminal/availability omitted, index still occupied.
    f[0].execute("""UPDATE mhz_publish_order_items
                       SET status=%s, settlement_status=%s, capacity_state='released'
                     WHERE id=%s""", (state, settled, item_id))
    return item_id


@pytest.mark.parametrize("state", ["failed", "rejected"])
def test_legacy_released_failure_can_use_new_account_same_revision(fixture, state):
    parent_id = failed_parent(fixture, state)
    before = row(fixture, parent_id)
    assert before["terminal_at"] is None and before["availability"] is None
    calls = []
    child_id = materialize(fixture, 991012, calls=calls)["order_item_id"]
    parent, child = row(fixture, parent_id), row(fixture, child_id)
    assert parent["status"] == state and parent["settlement_status"] == "released"
    assert parent["terminal_at"] is not None and parent["availability"] == "replaced"
    assert parent["replaced_by_source"] == "mhz_publish_order_items"
    assert parent["replaced_by_source_id"] == str(child_id)
    assert child["retry_of_item_id"] == parent_id
    assert child["attempt_root_id"] == parent["attempt_root_id"]
    assert child["attempt_no"] == 2 and child["source_post_revision_id"] == fixture[3]
    assert child["media_id"] == 991012 and child["status"] == "queued"
    assert child["settlement_status"] == "frozen"
    assert len(calls) == 1 and calls[0]["amount"] == 260
    assert child["task_ref"] != parent["task_ref"]
    assert not is_retryable({**parent, "state": state})
    fixture[0].execute("SELECT indexdef FROM pg_indexes WHERE indexname='uq_mhz_item_live_revision_root'")
    assert "UNIQUE INDEX" in fixture[0].fetchone()["indexdef"]
    fixture[0].execute("SELECT publish_item_ids FROM geo_douyin_posts WHERE id=%s", (fixture[1],))
    assert fixture[0].fetchone()["publish_item_ids"] == [child_id]


@pytest.mark.parametrize("state,settled", [
    ("submitted", "frozen"), ("awaiting_sync", "frozen"), ("awaiting_sync", "released"),
    ("needs_action", "manual"), ("failed", "frozen"), ("failed", "quarantined"),
    ("published", "committed"), ("queued", "frozen"),
])
def test_live_or_unsettled_attempt_never_creates_another_freeze(fixture, state, settled):
    parent_id = failed_parent(fixture, state, settled)
    before = row(fixture, parent_id)
    calls = []
    with pytest.raises(PublishAttemptConflict) as error:
        materialize(fixture, 991012, calls=calls)
    assert error.value.command_id == "pubcmd_" + before["command_request_id"]
    assert calls == [] and row(fixture, parent_id) == before
    fixture[0].execute("SELECT count(*) AS n FROM mhz_publish_order_items WHERE source_post_revision_id=%s", (fixture[3],))
    assert fixture[0].fetchone()["n"] == 1


def test_replacement_failure_rolls_back_old_record_and_new_order(fixture, monkeypatch):
    parent_id = failed_parent(fixture)
    before = row(fixture, parent_id)
    cur = fixture[0]
    cur.execute("SAVEPOINT replacement")
    # Fail after the new item and replacement backlink have both been written.
    from db import geo_douyin_db
    def fail_writeback(*args, **kwargs):
        raise RuntimeError("fixture final writeback failure")
    monkeypatch.setattr(geo_douyin_db, "bind_publish_submission", fail_writeback)
    with pytest.raises(RuntimeError, match="fixture final"):
        materialize(fixture, 991012)
    cur.execute("ROLLBACK TO SAVEPOINT replacement")
    assert row(fixture, parent_id) == before
    cur.execute("SELECT count(*) AS n FROM mhz_publish_order_items WHERE source_post_revision_id=%s", (fixture[3],))
    assert cur.fetchone()["n"] == 1


def test_third_attempt_continues_root_not_another_independent_chain(fixture):
    parent_id = failed_parent(fixture)
    second_id = materialize(fixture, 991012)["order_item_id"]
    fixture[0].execute("UPDATE mhz_publish_order_items SET status='rejected',settlement_status='released' WHERE id=%s", (second_id,))
    third = row(fixture, materialize(fixture, 991013)["order_item_id"])
    assert third["attempt_root_id"] == row(fixture, parent_id)["attempt_root_id"]
    assert third["attempt_no"] == 3 and third["retry_of_item_id"] == second_id


def test_second_submit_sees_queued_child_before_freeze(fixture):
    failed_parent(fixture)
    child = materialize(fixture, 991012)
    calls = []
    with pytest.raises(PublishAttemptConflict):
        materialize(fixture, 991013, calls=calls)
    assert calls == []
    assert row(fixture, child["order_item_id"])["status"] == "queued"


def test_old_command_reads_current_child_and_preserves_owner_boundary(fixture, api_module):
    parent_id = failed_parent(fixture)
    original = row(fixture, parent_id)
    child = materialize(fixture, 991012)
    loaded = api_module._load_command(fixture[0], command_request_id=original["command_request_id"],
                                      owner_user_id=fixture[2])
    assert len(loaded) == 1 and loaded[0]["id"] == child["order_item_id"]
    assert loaded[0]["state"] == "queued" and loaded[0]["media_id"] == 991012
    assert api_module._load_command(fixture[0], command_request_id=original["command_request_id"],
                                   owner_user_id=fixture[2] + 99999) == []
    # Even if a malformed chain pointed at another payer's order, do not disclose it.
    fixture[0].execute("UPDATE mhz_publish_orders SET user_id=%s WHERE id=%s", (fixture[2] + 99999, child["order_id"]))
    loaded = api_module._load_command(fixture[0], command_request_id=original["command_request_id"],
                                      owner_user_id=fixture[2])
    assert loaded[0]["id"] == parent_id


def test_command_name_prefers_snapshot_then_live_directory_without_writes(fixture, api_module):
    cur = fixture[0]
    cur.execute("SELECT COUNT(*) AS n FROM mhz_short_video WHERE id=1900999")
    assert cur.fetchone()["n"] == 0
    cur.execute("INSERT INTO mhz_short_video(id,media_name,platform,can_tuwen) VALUES(1900999,'rollback公开账号名','抖音',1)")
    made = materialize(fixture, 1900999)
    original = row(fixture, made["order_item_id"])
    def load():
        return api_module._load_command(cur, command_request_id=original["command_request_id"], owner_user_id=fixture[2])
    assert load()[0]["media_name"] == "local account"  # immutable order name wins
    cur.execute("UPDATE mhz_publish_order_items SET media_name='' WHERE id=%s", (made["order_item_id"],))
    before = row(fixture, made["order_item_id"])
    assert load()[0]["media_name"] == "rollback公开账号名"
    assert row(fixture, made["order_item_id"]) == before  # lookup never repairs/mutates old orders
    assert api_module._load_command(cur, command_request_id=original["command_request_id"], owner_user_id=fixture[2] + 9999) == []
    cur.execute("UPDATE mhz_short_video SET media_name='' WHERE id=1900999")
    assert load()[0]["media_name"] == ""


@pytest.mark.parametrize("race", [False, True])
def test_old_command_retry_marker_targets_child_and_reports_written_count(fixture, api_module, monkeypatch, race):
    from fastapi import HTTPException
    from db import connection
    parent_id = failed_parent(fixture)
    child = materialize(fixture, 991012)
    fixture[0].execute("UPDATE mhz_publish_order_items SET status='failed',settlement_status='released' WHERE id=%s", (child["order_item_id"],))
    events = []
    # The endpoint gets the real cursor; fixture owns the final rollback, not a commit.
    conn = SimpleNamespace(cursor=lambda: fixture[0], commit=lambda: events.append("commit"),
                           rollback=lambda: events.append("rollback"), close=lambda: None)
    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    monkeypatch.setattr(api_module, "_user", lambda request: {"id": fixture[2]})
    monkeypatch.setattr(api_module, "_guard", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_module, "_resolve_identity", lambda request: {"tenant_owner_user_id": fixture[2]})
    if race:
        original_load = api_module._load_command
        def changed_after_read(*args, **kwargs):
            found = original_load(*args, **kwargs)
            fixture[0].execute("UPDATE mhz_publish_order_items SET settlement_status='manual' WHERE id=%s", (child["order_item_id"],))
            return found
        monkeypatch.setattr(api_module, "_load_command", changed_after_read)
    req = api_module.CommandRetryRequest(request_id=str(uuid.uuid4()), item_request_ids=[child["item_request_id"]])
    invocation = api_module.api_retry_publish_command(
        "pubcmd_" + row(fixture, parent_id)["command_request_id"], req, SimpleNamespace())
    if race:
        with pytest.raises(HTTPException) as error:
            asyncio.run(invocation)
        assert error.value.status_code == 409 and events == ["rollback"]
        assert row(fixture, child["order_item_id"])["retry_claim_state"] is None
    else:
        result = asyncio.run(invocation)
        assert result["marked"] == 1 and events == ["commit"]
        assert row(fixture, child["order_item_id"])["retry_claim_state"] == "requested"
    assert row(fixture, parent_id)["retry_claim_state"] is None


def test_concurrent_other_account_waits_on_same_revision_before_freeze(fixture):
    """Another connection cannot see uncommitted rows; only the version lock can fence it."""
    failed_parent(fixture)
    child = materialize(fixture, 991012)

    def other_connection():
        conn = psycopg2.connect(os.environ["GEO_FLOW_TEST_DSN"], cursor_factory=RealDictCursor)
        try:
            cur = conn.cursor()
            cur.execute("SET LOCAL lock_timeout='300ms'")
            item, _ = inputs(fixture, 991013)
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                _lock_previous_attempts(cur, [item])
        finally:
            conn.rollback()
            conn.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(other_connection).result(timeout=3)
    assert row(fixture, child["order_item_id"])["status"] == "queued"
