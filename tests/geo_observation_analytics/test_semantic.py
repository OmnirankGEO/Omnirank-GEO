"""Semantic insight engine: anonymized minimal fact pack (request-body
interception), strict official DeepSeek identity, DB idempotency + 20-concurrent
= 1 model call, failure => unavailable (no fallback), and output allowlisting."""

from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import psycopg2
import pytest

from services.geo_observation_analytics import contract as C
from services.geo_observation_analytics import aggregates as A
from services.geo_observation_analytics import explain
from services.geo_observation_analytics.contract import PRIVACY_FORBIDDEN_FIELDS

from _support import FakeTransport

pytestmark = pytest.mark.integration

URL = os.getenv("TEST_DATABASE_URL", "")


def _fact_pack():
    return explain.build_fact_pack(
        industry_key="enterprise-service",
        window={"start": "2026-07-01", "end": "2026-07-10", "label": "近期"},
        outcome_counts={"recommended": 40, "not_mentioned": 15, "refused_no_evidence": 12},
        metrics_bps={"presence": 6875, "explicit_recommendation": 3125,
                     "refusal_no_evidence": 1333, "citation": 4000, "evidence_coverage": 5000},
        sample_size=90, stability_status="shifted", model_shift=True,
        evidence_refs=["obs-uuid-1", "obs-uuid-2"],
        allowed_action_types=["add_evidence", "keep_observing"],
    )


def _snapshot(epoch: int = 0, sequence: int = 1):
    return explain.InsightSnapshot(
        policy_basis_hash="a" * 64, scope_type="private_brand",
        bucket_granularity="day", bucket_start=date(2026, 7, 10),
        bucket_epoch=epoch, promotion_sequence_watermark=sequence,
        aggregate_input_watermark=datetime(2026, 7, 10, 8, tzinfo=timezone.utc),
        aggregate_contract_version=C.CONTRACT_VERSION,
        aggregate_aggregation_version=C.AGGREGATION_VERSION,
        aggregate_metric_version=C.METRIC_VERSION,
    )


def _publish_snapshot(
    conn, snapshot: explain.InsightSnapshot, *, owner: int = 501, brand: int = 900001,
) -> None:
    """Publish the exact aggregate terminal required by the paid-call CAS."""
    aggregate_identity = {
        "contract_version": snapshot.aggregate_contract_version,
        "aggregation_version": snapshot.aggregate_aggregation_version,
        "metric_version": snapshot.aggregate_metric_version,
        "policy_version": "20",
        "policy_basis_hash": snapshot.policy_basis_hash,
        "eligibility_epoch": snapshot.bucket_epoch,
        "promotion_sequence_watermark": snapshot.promotion_sequence_watermark,
        "scope_type": snapshot.scope_type,
        "owner_user_id": owner,
        "brand_id": brand,
        "industry_key": "test",
        "bucket_granularity": snapshot.bucket_granularity,
        "bucket_start": snapshot.bucket_start,
        "bucket_end": snapshot.bucket_start,
        "prompt_family_key": None,
        "prompt_intent": None,
        "is_branded_prompt": None,
        "platform_key": None,
        "surface_key": None,
        "model_revision": None,
        "search_enabled": None,
        "source_type": None,
        "search_query_theme": None,
    }
    aggregate_key = A.compute_aggregate_key(aggregate_identity)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO public.geo_observation_aggregate_bucket_revision(
                   scope_type,bucket_granularity,bucket_start,epoch,dirty)
                 VALUES(%s,%s,%s,%s,FALSE)
                 ON CONFLICT(scope_type,bucket_granularity,bucket_start)
                 DO UPDATE SET epoch=EXCLUDED.epoch,dirty=FALSE,updated_at=NOW()""",
            (
                snapshot.scope_type, snapshot.bucket_granularity,
                snapshot.bucket_start, snapshot.bucket_epoch,
            ),
        )
        cur.execute(
            """DELETE FROM public.geo_observation_aggregates
                 WHERE policy_basis_hash=%s AND scope_type=%s
                   AND bucket_granularity=%s AND bucket_start=%s
                   AND owner_user_id=%s AND brand_id=%s""",
            (
                snapshot.policy_basis_hash, snapshot.scope_type,
                snapshot.bucket_granularity, snapshot.bucket_start, owner, brand,
            ),
        )
        cur.execute(
            """INSERT INTO public.geo_observation_aggregates(
                   aggregate_key,contract_version,aggregation_version,metric_version,
                   policy_version,policy_basis_hash,scope_type,owner_user_id,brand_id,
                   industry_key,bucket_granularity,bucket_start,bucket_end,
                   input_watermark,computed_at,eligibility_epoch,
                   promotion_sequence_watermark)
                 VALUES(%s,%s,%s,%s,'20',%s,%s,%s,%s,'test',%s,%s,%s,%s,
                        NOW(),%s,%s)
                 ON CONFLICT(aggregate_key) DO NOTHING""",
            (
                aggregate_key, snapshot.aggregate_contract_version,
                snapshot.aggregate_aggregation_version,
                snapshot.aggregate_metric_version, snapshot.policy_basis_hash,
                snapshot.scope_type, owner, brand, snapshot.bucket_granularity,
                snapshot.bucket_start, snapshot.bucket_start,
                snapshot.aggregate_input_watermark, snapshot.bucket_epoch,
                snapshot.promotion_sequence_watermark,
            ),
        )
        cur.execute(
            """INSERT INTO public.geo_observation_aggregate_refresh_manifest(
                   manifest_key,policy_basis_hash,policy_version,contract_version,
                   aggregation_version,metric_version,scope_type,bucket_granularity,
                   bucket_start,bucket_end,input_watermark,eligibility_epoch,
                   promotion_sequence_watermark,expected_scope_cell_count,
                   expected_scope_cell_fingerprint,aggregate_key_fingerprint,
                   eligible_observation_count,overall_cell_count,aggregate_row_count,
                   completed_at)
                 VALUES(%s,%s,'20',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,1,1,1,NOW())
                 ON CONFLICT(policy_basis_hash,contract_version,aggregation_version,
                             metric_version,scope_type,bucket_granularity,bucket_start)
                 DO UPDATE SET manifest_key=EXCLUDED.manifest_key,
                     input_watermark=EXCLUDED.input_watermark,
                     eligibility_epoch=EXCLUDED.eligibility_epoch,
                     promotion_sequence_watermark=EXCLUDED.promotion_sequence_watermark,
                     completed_at=NOW()""",
            (
                snapshot.snapshot_id(), snapshot.policy_basis_hash,
                snapshot.aggregate_contract_version,
                snapshot.aggregate_aggregation_version,
                snapshot.aggregate_metric_version, snapshot.scope_type,
                snapshot.bucket_granularity, snapshot.bucket_start,
                snapshot.bucket_start, snapshot.aggregate_input_watermark,
                snapshot.bucket_epoch, snapshot.promotion_sequence_watermark,
                "d" * 64, "e" * 64,
            ),
        )
        cur.execute(
            """UPDATE public.geo_observation_aggregate_bucket_revision
                  SET published_receipt=%s::jsonb,dirty=FALSE
                WHERE scope_type=%s AND bucket_granularity=%s AND bucket_start=%s""",
            (
                json.dumps({
                    "manifest_key": snapshot.snapshot_id(),
                    "policy_basis_hash": snapshot.policy_basis_hash,
                    "contract_version": snapshot.aggregate_contract_version,
                    "aggregation_version": snapshot.aggregate_aggregation_version,
                    "metric_version": snapshot.aggregate_metric_version,
                    "scope_type": snapshot.scope_type,
                    "bucket_granularity": snapshot.bucket_granularity,
                    "bucket_start": snapshot.bucket_start.isoformat(),
                    "bucket_end": snapshot.bucket_start.isoformat(),
                    "input_watermark": snapshot.aggregate_input_watermark.astimezone(
                        timezone.utc
                    ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    "eligibility_epoch": snapshot.bucket_epoch,
                    "promotion_sequence_watermark": snapshot.promotion_sequence_watermark,
                    "cells": [{
                        "owner_user_id": owner,
                        "brand_id": brand,
                        "industry_key": "test",
                        "aggregate_keys": [aggregate_key],
                    }],
                }),
                snapshot.scope_type, snapshot.bucket_granularity,
                snapshot.bucket_start,
            ),
        )
    conn.commit()


def _create_job(engine, conn, fact_pack, request_id, owner, brand, snapshot):
    _publish_snapshot(conn, snapshot, owner=owner, brand=brand)
    return engine.create_job(conn, fact_pack, request_id, owner, brand, snapshot)


# ---------------------------------------------------------------------------
# Request body carries ZERO forbidden fields / no tenant text
# ---------------------------------------------------------------------------
def test_request_body_is_anonymous():
    body = explain.build_request_body(_fact_pack())
    blob = json.dumps(body, ensure_ascii=False)
    for f in list(PRIVACY_FORBIDDEN_FIELDS) + [
        "brand_id", "owner_user_id", "question_text", "answer_text", "prompt_text",
        "full_response", "provider_trace_id",
    ]:
        assert f'"{f}"' not in blob, f"fact pack leaked forbidden key: {f}"
    # official identity is fixed in the body
    # 🔴 [WO_206 c1c 翻面 2026-09-14] 原来这里写死 "deepseek-v4-flash"。
    #    官方 2026-09-13 把 Flash 档改名 deepseek-flash,Deploy 206-d2 实打:
    #    官方 /models 只剩 deepseek-flash 与 deepseek-v4-pro。继续钉旧名,
    #    这条判据就会和**正确的修法互斥**。改成取常量:本判据守的那件事一个字没变,
    #    而「既定模型叫什么」只剩一个出处(config/deepseek_models)。
    assert body["model"] == C.SEMANTIC_MODEL == DEEPSEEK_OFFICIAL_FLASH
    assert body["thinking"] == {"type": "disabled"}


def test_non_anonymous_pack_refuses_to_build_body():
    bad = explain.FactPack(
        contract_version=C.CONTRACT_VERSION, metric_version=C.METRIC_VERSION,
        industry="x", window={"owner_user_id": 5}, sample_size=1, metrics_bps={},
        outcome_counts={}, stability_status="stable", model_shift=False,
        evidence_refs=[], allowed_action_types=[],
    )
    with pytest.raises(ValueError):
        explain.build_request_body(bad)


def test_official_transport_url_is_deepseek_official():
    assert explain.OfficialDeepSeekTransport.URL.startswith("https://api.deepseek.com")
    # no concrete DashScope artifacts and no silently-degrading fallback helper
    import inspect
    src = inspect.getsource(explain)
    assert "dashscope.aliyuncs.com" not in src.lower()   # DashScope endpoint
    assert "enable_thinking" not in src                  # DashScope thinking param
    assert "call_llm_with_fallback" not in src           # degrading fallback helper


# ---------------------------------------------------------------------------
# Output allowlisting: the model cannot invent refs or actions
# ---------------------------------------------------------------------------
def test_output_rejects_unknown_refs_or_actions():
    fp = _fact_pack()
    with pytest.raises(explain.InsightValidationError):
        explain.parse_and_validate(
            '{"summary":"x","evidence_refs":["obs-uuid-1","INVENTED"],"allowed_actions":[],"state":"ok"}',
            fp,
        )
    with pytest.raises(explain.InsightValidationError):
        explain.parse_and_validate(
            '{"summary":"x","evidence_refs":[],"allowed_actions":["auto_publish"],"state":"ok"}',
            fp,
        )


def test_output_rejects_non_json():
    with pytest.raises(explain.InsightValidationError):
        explain.parse_and_validate("not json at all", _fact_pack())


# ---------------------------------------------------------------------------
# DB idempotency + 20 concurrent = 1 model call
# ---------------------------------------------------------------------------
def test_idempotent_same_input_single_call(db_conn):
    transport = FakeTransport()
    engine = explain.InsightEngine(transport=transport)
    fp = _fact_pack()
    job1 = _create_job(engine, db_conn, fp, "req-1", 501, 900001, _snapshot())
    job2 = _create_job(engine, db_conn, fp, "req-2", 501, 900001, _snapshot())
    assert job1["job_id"] == job2["job_id"]  # same input hash -> same job
    assert job1["state"] == "completed"
    assert len(transport.calls) == 1


def test_20_concurrent_generates_single_task():
    transport = FakeTransport()
    engine = explain.InsightEngine(transport=transport)
    fp = _fact_pack()

    def worker(i):
        conn = psycopg2.connect(URL)
        try:
            return _create_job(
                engine, conn, fp, f"req-{i}", 501, 900001, _snapshot()
            )["job_id"]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=20) as ex:
        job_ids = list(ex.map(worker, range(20)))

    assert len(set(job_ids)) == 1          # exactly one task
    assert len(transport.calls) == 1       # exactly one model call


def test_failure_marks_unavailable_no_fallback(db_conn):
    transport = FakeTransport(fail=True)
    engine = explain.InsightEngine(transport=transport)
    job = _create_job(
        engine, db_conn, _fact_pack(), "req-1", 501, 900001, _snapshot()
    )
    assert job["state"] == "result_unknown"
    assert job["error_code"] == "SEMANTIC_INSIGHT_UNAVAILABLE"
    # deterministic facts unaffected; no retry loop -> one attempt only
    assert len(transport.calls) == 1


def test_paid_call_failure_is_unknown_and_never_repaid(db_conn):
    failing = FakeTransport(fail=True)
    engine = explain.InsightEngine(transport=failing)
    fp = _fact_pack()
    job1 = _create_job(engine, db_conn, fp, "req-1", 501, 900001, _snapshot())
    assert job1["state"] == "result_unknown"
    assert failing.calls and len(failing.calls) == 1
    # The provider request may have been charged. Same snapshot retry returns
    # the durable unknown row and never issues a second paid call.
    engine.transport = FakeTransport()
    job2 = _create_job(engine, db_conn, fp, "req-2", 501, 900001, _snapshot())
    assert job2["job_id"] == job1["job_id"]
    assert job2["state"] == "result_unknown"
    assert len(engine.transport.calls) == 0


def test_post_then_process_death_keeps_paid_anchor_and_never_retries(db_conn):
    class KillAfterPost:
        def __init__(self):
            self.calls = 0

        def chat(self, request_body):
            self.calls += 1
            raise SystemExit("simulated kill-9 after provider accepted POST")

    transport = KillAfterPost()
    engine = explain.InsightEngine(transport=transport)
    fp = _fact_pack()
    with pytest.raises(SystemExit):
        _create_job(engine, db_conn, fp, "req-kill", 501, 900001, _snapshot())
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT state,paid_call_started_at FROM geo_observation_insight_jobs"
        )
        state, started = cur.fetchone()
    assert state == "paid_call_started"
    assert started is not None

    replacement = FakeTransport()
    after_lease = datetime.now(timezone.utc) + timedelta(
        seconds=explain.INSIGHT_LEASE_SECONDS + 1
    )
    recovered = _create_job(
        explain.InsightEngine(transport=replacement, clock=lambda: after_lease),
        db_conn, fp, "req-retry", 501, 900001, _snapshot()
    )
    assert recovered["state"] == "result_unknown"
    assert replacement.calls == []


def test_owner_paid_budget_is_atomically_reserved_across_two_brands(db_conn):
    """49 owner reservations + two brand-concurrent attempts => exactly one POST."""
    owner = 777
    with db_conn.cursor() as cur:
        for i in range(explain.INSIGHT_DAILY_OWNER_PAID_CALL_LIMIT - 1):
            cur.execute(
                """INSERT INTO public.geo_observation_insight_jobs(
                       job_id,input_hash,owner_user_id,brand_id,state,model,
                       prompt_version,schema_version,snapshot_id,policy_basis_hash,
                       scope_type,bucket_granularity,bucket_start,bucket_epoch,
                       promotion_sequence_watermark,aggregate_input_watermark,
                       aggregate_contract_version,aggregate_aggregation_version,
                       aggregate_metric_version,paid_call_started_at,budget_reserved_at)
                     VALUES(%s,%s,%s,%s,'completed','m','p','s',%s,%s,
                            'private_brand','day','2026-07-10',0,%s,
                            '2026-07-10T08:00:00Z',%s,%s,%s,NOW(),NOW())""",
                (
                    f"budget-seed-{i}", f"{i:064x}", owner, 7000 + (i % 3),
                    f"{(i + 1000):064x}", "a" * 64, i + 1,
                    C.CONTRACT_VERSION, C.AGGREGATION_VERSION, C.METRIC_VERSION,
                ),
            )
    db_conn.commit()

    transport = FakeTransport()
    engine = explain.InsightEngine(transport=transport)
    barrier = __import__("threading").Barrier(2)

    def worker(index: int):
        conn = psycopg2.connect(URL)
        try:
            base = _snapshot(sequence=500 + index)
            snapshot = explain.InsightSnapshot(**{
                **base.__dict__,
                "bucket_start": date(2026, 7, 7 + index),
                "aggregate_input_watermark": datetime(
                    2026, 7, 7 + index, 8, tzinfo=timezone.utc,
                ),
            })
            _publish_snapshot(conn, snapshot, owner=owner, brand=8000 + index)
            barrier.wait()
            try:
                return engine.create_job(
                    conn, _fact_pack(), f"budget-race-{index}", owner,
                    8000 + index, snapshot,
                )["state"]
            except explain.InsightUnavailableError:
                return "unavailable"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(worker, (1, 2)))
    assert len(transport.calls) == 1
    assert sorted(states) == ["completed", "unavailable"]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM public.geo_observation_insight_jobs "
            "WHERE owner_user_id=%s AND budget_reserved_at>=date_trunc('day',NOW())",
            (owner,),
        )
        assert cur.fetchone()[0] == explain.INSIGHT_DAILY_OWNER_PAID_CALL_LIMIT


def test_daily_paid_call_cap_blocks_unbounded_new_snapshots(db_conn):
    transport = FakeTransport()
    engine = explain.InsightEngine(transport=transport)
    for sequence in range(1, explain.INSIGHT_DAILY_BRAND_PAID_CALL_LIMIT + 1):
        job = _create_job(
            engine, db_conn, _fact_pack(), f"req-cap-{sequence}", 501, 900001,
            _snapshot(sequence=sequence),
        )
        assert job["state"] == "completed"
    with pytest.raises(explain.InsightUnavailableError):
        _create_job(
            engine, db_conn, _fact_pack(), "req-cap-over", 501, 900001,
            _snapshot(sequence=999),
        )
    assert len(transport.calls) == explain.INSIGHT_DAILY_BRAND_PAID_CALL_LIMIT


def test_stuck_job_recovered_by_sweeper(db_conn):
    """P1#6: a lease-expired pending/running job is recovered by the sweeper to
    'failed' (so it is not a permanent pending) and then re-runnable."""
    engine = explain.InsightEngine(transport=FakeTransport())
    fp = _fact_pack()
    job = _create_job(engine, db_conn, fp, "req-1", 501, 900001, _snapshot())
    assert job["state"] == "completed"
    # simulate a crashed worker: force the row back to running with an expired lease
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE geo_observation_insight_jobs SET state='running', "
            "paid_call_started_at=NULL, budget_reserved_at=NULL, "
            "lease_token=gen_random_uuid(), "
            "lease_until = NOW() - interval '10 minutes' WHERE job_id=%s", (job["job_id"],)
        )
    db_conn.commit()
    recovered = engine.recover_stuck_jobs(db_conn)
    assert recovered == 1
    row = engine.get_job(db_conn, job["job_id"], 501, 900001)
    assert row["state"] == "failed"
    assert row["error_code"] == "SEMANTIC_INSIGHT_UNAVAILABLE"


def test_fencing_prevents_revived_old_worker_overwrite(db_conn):
    """P1#1: after a reclaim issues a new lease token, a revived OLD worker
    (holding the superseded token) cannot overwrite the reclaimer's result."""
    engine = explain.InsightEngine(transport=FakeTransport(
        response_content='{"summary":"NEW_WORKER","evidence_refs":[],'
                         '"allowed_actions":["add_evidence"],"state":"ok"}'))
    fp = _fact_pack()
    job = _create_job(engine, db_conn, fp, "req-1", 501, 900001, _snapshot())
    job_id = job["job_id"]
    old_token = "11111111-1111-4111-8111-111111111111"
    # simulate a crashed worker holding old_token: expire the lease
    with db_conn.cursor() as cur:
        cur.execute("UPDATE geo_observation_insight_jobs SET state='running', "
                    "paid_call_started_at=NULL, budget_reserved_at=NULL, "
                    "lease_token=%s, lease_until = NOW() - interval '10 minutes' WHERE job_id=%s",
                    (old_token, job_id))
    db_conn.commit()
    # a reclaimer takes over with a NEW token and completes with NEW_WORKER
    job2 = _create_job(engine, db_conn, fp, "req-2", 501, 900001, _snapshot())
    assert job2["job_id"] == job_id
    assert job2["lease_token"] != old_token
    assert job2["summary"] == "NEW_WORKER"
    # the revived OLD worker tries to complete with the superseded token -> fenced
    n = engine._complete(db_conn, job_id, old_token,
                         datetime(2026, 7, 10, 8, tzinfo=timezone.utc),
                         explain.InsightResult(summary="OLD_WORKER", evidence_refs=[],
                                               allowed_actions=[], state="ok"), {})
    assert n == 0
    final = engine.get_job(db_conn, job_id, 501, 900001)
    assert final["summary"] == "NEW_WORKER"  # not resurrected to OLD_WORKER


def test_sweeper_result_unknown_cannot_be_resurrected_by_expired_worker(db_conn):
    engine = explain.InsightEngine(transport=FakeTransport())
    job = _create_job(
        engine, db_conn, _fact_pack(), "req-race", 501, 900001, _snapshot()
    )
    old_token = "22222222-2222-4222-8222-222222222222"
    started_at = datetime(2026, 7, 10, 9, tzinfo=timezone.utc)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE geo_observation_insight_jobs SET state='paid_call_started',"
            "lease_token=%s,paid_call_started_at=%s,budget_reserved_at=%s,"
            "lease_until=NOW()-interval '10 minutes' WHERE job_id=%s",
            (old_token, started_at, started_at, job["job_id"]),
        )
    db_conn.commit()

    assert engine.recover_stuck_jobs(db_conn) == 1
    stale_write = engine._complete(
        db_conn, job["job_id"], old_token, started_at,
        explain.InsightResult(
            summary="STALE_COMPLETION", evidence_refs=[], allowed_actions=[], state="ok"
        ),
        {},
    )
    assert stale_write == 0
    row = engine.get_job(db_conn, job["job_id"], 501, 900001)
    assert row["state"] == "result_unknown"
    assert row["lease_token"] is None
    assert row["summary"] != "STALE_COMPLETION"


def test_snapshot_identity_includes_watermark_and_metric_lineage():
    base = _snapshot()
    variants = [
        explain.InsightSnapshot(**{
            **base.__dict__,
            "aggregate_input_watermark": datetime(2026, 7, 10, 9, tzinfo=timezone.utc),
        }),
        explain.InsightSnapshot(**{**base.__dict__, "aggregate_contract_version": "other"}),
        explain.InsightSnapshot(**{**base.__dict__, "aggregate_aggregation_version": "other"}),
        explain.InsightSnapshot(**{**base.__dict__, "aggregate_metric_version": "other"}),
    ]
    assert len({base.snapshot_id(), *(item.snapshot_id() for item in variants)}) == 5
    job = {
        "snapshot_id": base.snapshot_id(),
        "policy_basis_hash": base.policy_basis_hash,
        "scope_type": base.scope_type,
        "bucket_granularity": base.bucket_granularity,
        "bucket_start": base.bucket_start,
        "bucket_epoch": base.bucket_epoch,
        "promotion_sequence_watermark": base.promotion_sequence_watermark,
        "aggregate_input_watermark": base.aggregate_input_watermark,
        "aggregate_contract_version": base.aggregate_contract_version,
        "aggregate_aggregation_version": base.aggregate_aggregation_version,
        "aggregate_metric_version": base.aggregate_metric_version,
    }
    assert explain.job_matches_snapshot(job, base)
    assert all(not explain.job_matches_snapshot(job, variant) for variant in variants)


def test_snapshot_identity_canonicalizes_equal_instants_to_utc():
    base = _snapshot()
    same_in_shanghai = explain.InsightSnapshot(**{
        **base.__dict__,
        "aggregate_input_watermark": datetime(
            2026, 7, 10, 16, tzinfo=timezone(timedelta(hours=8))
        ),
    })
    later = explain.InsightSnapshot(**{
        **base.__dict__,
        "aggregate_input_watermark": datetime(
            2026, 7, 10, 8, 0, 0, 1, tzinfo=timezone.utc
        ),
    })
    assert same_in_shanghai.snapshot_id() == base.snapshot_id()
    assert later.snapshot_id() != base.snapshot_id()


def test_completed_result_only_uses_allowed_actions(db_conn):
    transport = FakeTransport(
        response_content='{"summary":"补证据后继续观察","evidence_refs":["obs-uuid-1"],'
                         '"allowed_actions":["add_evidence"],"state":"ok"}'
    )
    engine = explain.InsightEngine(transport=transport)
    job = _create_job(
        engine, db_conn, _fact_pack(), "req-1", 501, 900001, _snapshot()
    )
    assert job["state"] == "completed"
    assert job["allowed_actions"] == ["add_evidence"]
    assert job["evidence_refs"] == ["obs-uuid-1"]
