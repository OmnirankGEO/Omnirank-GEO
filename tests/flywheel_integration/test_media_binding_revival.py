"""[P2 + 补充指令 §6] media binding candidate deleted-revival on rebuild:
- a soft-deleted candidate whose match still holds is revived to 'candidate' and
  its human-review fields are reset; _revived_from_deleted flag is returned.
- 'rejected' / 'approved' candidates are NEVER revived (human decisions preserved).
- list_media_binding_candidates excludes 'deleted' by default; explicit status='deleted' still returns them.
"""
import pytest

from db.media_entity_flywheel_db import (
    init_media_entity_flywheel_tables,
    list_media_binding_candidates,
    upsert_media_binding_candidate,
)
from db.connection import get_connection

_EKEY = "me_test_revive"


def _candidate(inventory_id=778899):
    return {
        "candidate_key": f"ck-{_EKEY}-{inventory_id}",
        "entity_key": _EKEY,
        "industry_key": "general",
        "inventory": {
            "media_source": "mhz_media",
            "inventory_id": inventory_id,
            "media_name": "测试媒体",
            "url": "https://revive.example.com",
        },
        "match_method": "domain_exact",
        "match_confidence": 0.98,
        "can_approve": True,
        "risk_flags": [],
        "evidence": {"name_evidence": True},
    }


def _cleanup():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM geo_media_binding_candidates WHERE entity_key = %s", (_EKEY,))
        conn.commit()
    finally:
        conn.close()


def _force_status(candidate_id, **fields):
    conn = get_connection()
    try:
        cur = conn.cursor()
        sets = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(
            f"UPDATE geo_media_binding_candidates SET {sets} WHERE id = %s",
            (*fields.values(), candidate_id),
        )
        conn.commit()
    finally:
        conn.close()


def _row(candidate_id):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_media_binding_candidates WHERE id = %s", (candidate_id,))
        return dict(cur.fetchone())
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _tables():
    init_media_entity_flywheel_tables()
    _cleanup()
    yield
    _cleanup()


def test_deleted_candidate_is_revived_and_review_fields_reset():
    saved = upsert_media_binding_candidate(_candidate(), operator_id=None)
    cid = int(saved["id"])
    assert saved["status"] == "candidate"
    assert saved["_revived_from_deleted"] is False

    # simulate the out-of-band soft-delete + a prior human review trail
    _force_status(cid, status="deleted", reviewed_by=5, review_note="手工删",
                  approved_mapping_id=42)

    revived = upsert_media_binding_candidate(_candidate(), operator_id=None)
    assert int(revived["id"]) == cid
    assert revived["_revived_from_deleted"] is True
    row = _row(cid)
    assert row["status"] == "candidate", "deleted 应复活为 candidate"
    assert row["reviewed_by"] is None
    assert row["reviewed_at"] is None
    assert row["review_note"] is None
    assert row["approved_mapping_id"] is None
    assert row["active"] is True


def test_rejected_candidate_is_not_revived():
    saved = upsert_media_binding_candidate(_candidate(), operator_id=None)
    cid = int(saved["id"])
    _force_status(cid, status="rejected", reviewed_by=7, review_note="不合格")

    again = upsert_media_binding_candidate(_candidate(), operator_id=None)
    assert again["_revived_from_deleted"] is False
    row = _row(cid)
    assert row["status"] == "rejected", "rejected 是人工决策,禁止被 rebuild 复活"
    assert row["reviewed_by"] == 7


def test_approved_candidate_is_not_revived():
    saved = upsert_media_binding_candidate(_candidate(), operator_id=None)
    cid = int(saved["id"])
    _force_status(cid, status="approved", reviewed_by=9, approved_mapping_id=100)

    again = upsert_media_binding_candidate(_candidate(), operator_id=None)
    assert again["_revived_from_deleted"] is False
    row = _row(cid)
    assert row["status"] == "approved", "approved 是人工决策,禁止被 rebuild 复活"
    assert row["approved_mapping_id"] == 100


def test_list_excludes_deleted_by_default():
    saved = upsert_media_binding_candidate(_candidate(), operator_id=None)
    cid = int(saved["id"])
    _force_status(cid, status="deleted")

    default_list = list_media_binding_candidates(industry_key="general", limit=100)
    assert all(item["entity_key"] != _EKEY for item in default_list), "默认列表不应含 deleted"

    deleted_list = list_media_binding_candidates(industry_key="general", status="deleted", limit=100)
    assert any(int(item["id"]) == cid for item in deleted_list), "显式 status=deleted 仍可查"
