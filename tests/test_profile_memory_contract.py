import json

from db import profile_memory_db as memory_db
from scripts.product.backfill_profile_flywheel_memory import _events_from_session


def test_event_fingerprint_requires_concept_and_is_stable():
    a = memory_db.make_event_fingerprint("p1", "profile_flywheel", "detail", "target_customer", " 小B老板，怕没效果。 ")
    b = memory_db.make_event_fingerprint("p1", "profile_flywheel", "detail", "target_customer", "小B老板怕没效果")
    assert a == b
    assert memory_db.make_event_fingerprint("p1", "profile_flywheel", "detail", "", "小B老板") is None


def test_infer_canonical_concept_maps_user_facing_facts():
    assert memory_db.infer_canonical_concept(label="目标客户", text="宝妈和本地老板") == "target_customer"
    assert memory_db.infer_canonical_concept(label="案例", text="成交 12 单") == "offer_and_proof"
    assert memory_db.infer_canonical_concept(label="口头禅", text="就一句话") == "voice_style"
    assert memory_db.infer_canonical_concept(label="不要写", text="不要承诺疗效") == "guardrails"
    assert memory_db.infer_canonical_concept(label="主营业务", text="做本地家装") == "business_identity"


def test_rejected_memory_cannot_be_resurrected_by_review_helper_guard():
    assert memory_db.is_review_transition_allowed("rejected", "approved") is False
    assert memory_db.is_review_transition_allowed("rejected", "auto") is False
    assert memory_db.is_review_transition_allowed("rejected", "processing") is False
    assert memory_db.is_review_transition_allowed("pending", "approved") is True
    assert memory_db.is_review_transition_allowed("approved", "rejected") is True


def test_backfill_reads_collected_data_v2_path_and_writes_pending_events():
    state = {
        "profile_id": "profile-1",
        "collected_data": {
            "_v2": {
                "understanding_summary": "他做本地家装，客户最怕延期。",
                "accumulated_details": [{"label": "客户画像", "content": "改善型业主"}],
                "accumulated_quotes": [{"text": "我不接低价乱改的单", "usage_hint": "筛选客户"}],
            }
        },
    }
    events = _events_from_session({
        "session_id": "s1",
        "user_id": 7,
        "state_json": json.dumps(state, ensure_ascii=False),
    })
    assert [event["event_type"] for event in events] == ["detail", "quote", "persona_summary"]
    assert {event["review_status"] for event in events} == {"pending"}
    assert events[0]["source"] == "profile_flywheel"
    assert events[0]["canonical_concept"] == "target_customer"


# [开源 E3 · B2 · 2026-09-28] 社媒飞轮引擎 v2 随包删除,守它的 4 格退役。


