from __future__ import annotations

from datetime import datetime, timezone
import json

from writing.article_rewrite_jobs import (
    BatchRewriteJobStore,
    batch_rewrite_failure_message,
    public_batch_rewrite_errors,
)
from writing.brand_fact_snapshot import build_brand_fact_snapshot


def test_brand_fact_snapshot_normalizes_database_datetimes_before_json_persistence():
    confirmed_at = datetime(2026, 7, 20, 4, 46, 56, tzinfo=timezone.utc)
    snapshot = build_brand_fact_snapshot(
        brand_id=52,
        brand_name="测试品牌",
        industry="测试行业",
        client_materials={
            "_confirmed_at": confirmed_at,
            "case_studies": [{"confirmed_at": confirmed_at, "summary": "已核验案例"}],
        },
    )

    encoded = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["source_updated_at"] == confirmed_at.isoformat()
    assert snapshot["claims"][0]["value"][0]["confirmed_at"] == confirmed_at.isoformat()
    assert "已核验案例" in encoded


def test_batch_rewrite_failure_copy_distinguishes_trust_gate_without_leaking_details():
    errors = [
        {"topic_id": 5979, "error": "EVIDENCE_FIRST_BLOCKED:ordered_brand_candidates"},
        {"topic_id": 5981, "error": "EVIDENCE_FIRST_BLOCKED:anonymous_authority"},
    ]

    public = public_batch_rewrite_errors(errors)

    assert public == [
        {"topic_id": 5979, "reason": "content_trust_check_failed"},
        {"topic_id": 5981, "reason": "content_trust_check_failed"},
    ]
    assert batch_rewrite_failure_message(errors) == "所选文章未通过内容可信校验，原稿已保留，本次未收费"
    assert "ordered_brand_candidates" not in json.dumps(public)


def test_batch_rewrite_failure_copy_identifies_json_processing_failure():
    errors = [{"topic_id": 5983, "error": "Object of type datetime is not JSON serializable"}]
    assert batch_rewrite_failure_message(errors) == "文章资料处理异常，原稿已保留，本次未收费"


def test_job_store_uses_safe_failure_message_and_preserves_no_charge_state():
    store = BatchRewriteJobStore(ref_factory=lambda: "batch-safe")
    job, _ = store.get_or_create(
        actor_key="user:125",
        quote_id=366,
        topic_ids=[5979, 5981],
    )
    message = "所选文章未通过内容可信校验，原稿已保留，本次未收费"
    store.mark_failed(job.batch_ref, error=message, message=message)

    public = store.get(job.batch_ref).to_public_dict()
    assert public["status"] == "failed"
    assert public["charged_count"] == 0
    assert public["charged_points"] == 0
    assert public["message"] == message
