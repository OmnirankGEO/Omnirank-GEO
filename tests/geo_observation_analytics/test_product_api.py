"""Product API contract tests: fixture-compatible DTOs, brand-owner RBAC,
privacy (no owner/upstream leak), public k-anonymity gate, error envelope,
and the semantic insight lifecycle. Real PostgreSQL, real router."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest
import psycopg2
from fastapi.testclient import TestClient

from api import geo_observation_product_api as product_api
from services.geo_observation_analytics import aggregates as A, contract as C
from services.geo_observation_analytics import repository as repo
from services.geo_observation_analytics.privacy import PRIVACY_FORBIDDEN_FIELDS

from _support import (
    DEFAULT_TEST_POLICY,
    FakeTransport,
    auth_headers,
    build_test_app,
    build_test_deps,
    seed_monitoring,
)

pytestmark = pytest.mark.integration

OWNER = 501
BRAND = 900001
DAY = date(2026, 7, 10)
CT = datetime(2026, 7, 11, tzinfo=timezone.utc)


@pytest.fixture()
def private_data(seeder, db_conn):
    seeder.brand(BRAND, OWNER, name="华南智能制造技术服务示例品牌")
    # a realistic private mix
    for _ in range(40):
        seeder.observation(outcome="recommended", processing_state="promoted",
                           owner_user_id=OWNER, brand_id=BRAND, target_position=2,
                           citation_count=2, evidence_verifiable=True)
    for _ in range(23):
        seeder.observation(outcome="conditionally_recommended", processing_state="promoted",
                           owner_user_id=OWNER, brand_id=BRAND)
    for _ in range(12):
        seeder.observation(outcome="refused_no_evidence", processing_state="promoted",
                           owner_user_id=OWNER, brand_id=BRAND)
    for _ in range(15):
        seeder.observation(outcome="not_mentioned", processing_state="promoted",
                           owner_user_id=OWNER, brand_id=BRAND)
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()


def _client(**kw) -> TestClient:
    return TestClient(build_test_app(build_test_deps(**kw)))


def _collection(status="ready", *, ready=None, problems=None, **overrides):
    payload = {
        "mode": "existing_collectors_reconciled",
        "status": status,
        "problems": list(problems or []),
        "reconciler_last_success_at": "2026-07-20T00:00:00+00:00",
        "reconciler_backlog": 0,
        "source_watermarks": {
            "paid_diagnosis": "2026-07-20T00:00:00+00:00",
            "recurring_monitoring": "2026-07-20T00:00:00+00:00",
            "research_round": "2026-07-20T00:00:00+00:00",
        },
        "duplicate_collection_jobs": [],
        "policy_version": 20,
    }
    if ready is not None:
        payload["ready"] = ready
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Error envelope matches the frozen fixture
# ---------------------------------------------------------------------------
def test_error_envelope_matches_fixture():
    from services.geo_observation_analytics.errors import ERROR_SPECS
    fixture = C.load_frozen_fixture()["error_cases"]
    assert ERROR_SPECS["FORBIDDEN"][1] == fixture["403"]["message"]
    assert ERROR_SPECS["VERSION_CONFLICT"][1] == fixture["409"]["message"]
    assert ERROR_SPECS["ENV_OVERRIDE_ACTIVE"][1] == fixture["423"]["message"]
    assert ERROR_SPECS["OBSERVATION_UNAVAILABLE"][1] == fixture["503"]["message"]
    assert ERROR_SPECS["INSUFFICIENT_SAMPLES"][1] == fixture["insufficient_samples"]["message"]
    assert ERROR_SPECS["FORBIDDEN"][0] == 403
    assert ERROR_SPECS["VERSION_CONFLICT"][0] == 409
    assert ERROR_SPECS["ENV_OVERRIDE_ACTIVE"][0] == 423
    assert ERROR_SPECS["OBSERVATION_UNAVAILABLE"][0] == 503


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def test_brand_summary_business_fields(private_data):
    c = _client()
    r = c.get(f"/api/geo-observation/brands/{BRAND}/summary",
              headers=auth_headers(OWNER, [BRAND]))
    assert r.status_code == 200
    body = r.json()
    # fixture-compatible keys present
    assert body["brand"] == {"brand_id": BRAND, "display_name": "华南智能制造技术服务示例品牌"}
    assert body["metric_version"] == C.METRIC_VERSION
    s = body["summary"]
    assert s["valid_observations"] == 90  # 40+23+12+15
    assert s["presence_rate_bps"] == round(63 / 90 * 10000)  # 40 rec + 23 cond present
    assert s["explicit_recommendation_rate_bps"] == round(40 / 90 * 10000)
    assert s["stability_status"] in C.STABILITY_STATES
    assert isinstance(body["outcomes"], list)
    assert {"outcome": "recommended", "count": 40} in body["outcomes"]
    assert body["next_actions"]  # at least one action
    # no privacy leak
    _assert_no_leak(body)


def test_summary_rbac_cross_tenant_forbidden(private_data):
    c = _client()
    # a different user with no access to this brand
    r = c.get(f"/api/geo-observation/brands/{BRAND}/summary",
              headers=auth_headers(999, [777]))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "FORBIDDEN"


def test_summary_requires_auth(private_data):
    c = _client()
    r = c.get(f"/api/geo-observation/brands/{BRAND}/summary")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Platforms — display names + historical Kimi folded
# ---------------------------------------------------------------------------
def test_platforms_display_and_historical(seeder, db_conn):
    seeder.brand(BRAND, OWNER)
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=OWNER, brand_id=BRAND, platform_key="doubao",
                       surface_key="doubao_ark_api_search")
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=OWNER, brand_id=BRAND, platform_key="deepseek",
                       surface_key="deepseek_metaso_proxy")
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=OWNER, brand_id=BRAND, platform_key="kimi",
                       surface_key="other_explicit")
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()

    c = _client()
    r = c.get(f"/api/geo-observation/brands/{BRAND}/platforms", headers=auth_headers(OWNER, [BRAND]))
    assert r.status_code == 200
    body = r.json()
    plats = {p["platform_key"]: p for p in body["items"]}
    assert "kimi" not in plats  # historical, not in default items
    assert plats["doubao"]["display_name"] == "豆包"
    assert plats["doubao"]["surface_note"] is None
    assert plats["deepseek"]["display_name"] == "DeepSeek"
    # proxy channel is admin-only now: never disclosed to customers/service providers
    assert plats["deepseek"]["surface_note"] is None
    assert "秘塔" not in json.dumps(body, ensure_ascii=False)
    hist = {h["platform_key"] for h in body["historical_platforms"]}
    assert "kimi" in hist
    _assert_no_leak(body)


# ---------------------------------------------------------------------------
# Questions + evidence
# ---------------------------------------------------------------------------
def test_questions_pagination_and_changed(seeder, db_conn):
    seeder.brand(BRAND, OWNER)
    # same family, outcome flips -> changed=True on the second
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=OWNER, brand_id=BRAND, prompt_family_key="fam-x",
                       observed_at=datetime(2026, 7, 10, 8, tzinfo=timezone.utc))
    seeder.observation(outcome="not_mentioned", processing_state="promoted",
                       owner_user_id=OWNER, brand_id=BRAND, prompt_family_key="fam-x",
                       observed_at=datetime(2026, 7, 10, 9, tzinfo=timezone.utc))
    A.refresh_scope(
        db_conn, scope_type="private_brand", granularity="day",
        day=DAY, computed_at=CT,
    )
    db_conn.commit()

    c = _client()
    r = c.get(f"/api/geo-observation/brands/{BRAND}/questions?page=1&page_size=20",
              headers=auth_headers(OWNER, [BRAND]))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    # newest first (not_mentioned) changed vs prior recommended
    assert body["items"][0]["outcome"] == "not_mentioned"
    assert body["items"][0]["changed"] is True
    assert body["items"][0]["question"].startswith("示例问题")  # from fake evidence source
    _assert_no_leak(body)


def test_questions_and_direct_evidence_are_bound_to_private_aggregate_snapshot(
    seeder, db_conn,
):
    """Promoted rows after the active aggregate terminal stay invisible until refresh."""
    seeder.brand(BRAND, OWNER)
    old_event_id = seeder.observation(
        outcome="recommended", processing_state="promoted",
        owner_user_id=OWNER, brand_id=BRAND, prompt_family_key="old-family",
        observed_at=datetime(2026, 7, 10, 8, tzinfo=timezone.utc),
    )
    A.refresh_scope(
        db_conn, scope_type="private_brand", granularity="day",
        day=DAY, computed_at=CT,
    )
    db_conn.commit()

    new_event_id = seeder.observation(
        outcome="not_mentioned", processing_state="promoted",
        owner_user_id=OWNER, brand_id=BRAND, prompt_family_key="new-family",
        # Backdated before the published watermark; promotion_seq is the only
        # safe inclusion boundary for this late-promoted event.
        observed_at=datetime(2026, 7, 10, 7, tzinfo=timezone.utc),
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, event_uuid::text FROM geo_observation_events WHERE id IN (%s,%s)",
            (old_event_id, new_event_id),
        )
        ids = {int(row[0]): row[1] for row in cur.fetchall()}
    old_uuid = ids[old_event_id]
    new_uuid = ids[new_event_id]

    c = _client()
    headers = auth_headers(OWNER, [BRAND])
    summary_before = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary", headers=headers
    )
    assert summary_before.status_code == 200
    assert summary_before.json()["summary"]["valid_observations"] == 1
    before = c.get(
        f"/api/geo-observation/brands/{BRAND}/questions?page=1&page_size=20",
        headers=headers,
    )
    assert before.status_code == 200
    assert before.json()["total"] == 1
    assert [row["observation_id"] for row in before.json()["items"]] == [old_uuid]
    assert c.get(
        f"/api/geo-observation/brands/{BRAND}/evidence/{old_uuid}",
        headers=headers,
    ).status_code == 200
    hidden = c.get(
        f"/api/geo-observation/brands/{BRAND}/evidence/{new_uuid}",
        headers=headers,
    )
    assert hidden.status_code == 403

    A.refresh_scope(
        db_conn, scope_type="private_brand", granularity="day",
        day=DAY, computed_at=CT,
    )
    db_conn.commit()
    after = c.get(
        f"/api/geo-observation/brands/{BRAND}/questions?page=1&page_size=20",
        headers=headers,
    )
    assert after.status_code == 200
    assert after.json()["total"] == 2
    assert new_uuid in {row["observation_id"] for row in after.json()["items"]}
    summary_after = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary", headers=headers
    )
    assert summary_after.status_code == 200
    assert summary_after.json()["summary"]["valid_observations"] == 2
    assert c.get(
        f"/api/geo-observation/brands/{BRAND}/evidence/{new_uuid}",
        headers=headers,
    ).status_code == 200


def test_expired_promoted_input_is_excluded_before_retention_reconciler(
    private_data, db_conn,
):
    latest = repo.latest_overall_aggregate(
        db_conn, scope_type="private_brand", granularity="day",
        owner_user_id=OWNER, brand_id=BRAND,
        policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH,
    )
    assert latest is not None
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id,event_uuid::text FROM geo_observation_events "
            "WHERE owner_user_id=%s AND brand_id=%s AND processing_state='promoted' "
            "ORDER BY id LIMIT 1",
            (OWNER, BRAND),
        )
        event_id, observation_id = cur.fetchone()
        cur.execute(
            "UPDATE geo_observation_events SET retention_until=NOW()-interval '1 second' "
            "WHERE id=%s", (event_id,),
        )
    db_conn.commit()

    assert repo.count_brand_questions(
        db_conn, OWNER, BRAND,
        through_watermark=latest["input_watermark"],
        through_promotion_sequence=int(latest["promotion_sequence_watermark"]),
    ) == 89
    assert repo.get_observation_meta(
        db_conn, OWNER, BRAND, observation_id,
        through_watermark=latest["input_watermark"],
        through_promotion_sequence=int(latest["promotion_sequence_watermark"]),
    ) is None
    assert len(A.fetch_observations(
        db_conn, "private_brand", DAY, DAY,
        promotion_sequence_watermark=int(latest["promotion_sequence_watermark"]),
    )) == 89


def test_retention_expiry_after_readiness_returns_unavailable_and_calls_no_provider(
    private_data,
):
    """Deterministic two-connection barrier: readiness succeeds, then the
    aggregate provider expires live input before the handler opens its read
    connection. Product data and paid insight must both fail closed.
    """
    expired = False

    def readiness_then_expire(_basis):
        nonlocal expired
        if not expired:
            conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.geo_observation_events "
                        "SET retention_until=clock_timestamp()-interval '1 second' "
                        "WHERE processing_state='promoted'"
                    )
                conn.commit()
            finally:
                conn.close()
            expired = True
        return {"status": "ready", "problems": []}

    transport = FakeTransport()
    c = _client(
        transport=transport,
        aggregate_readiness=readiness_then_expire,
    )
    headers = auth_headers(OWNER, [BRAND])
    summary = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary", headers=headers
    )
    assert summary.status_code == 503
    assert summary.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"
    insight = c.post(
        f"/api/geo-observation/brands/{BRAND}/insights", headers=headers
    )
    assert insight.status_code == 503
    assert transport.calls == []


def test_other_tenant_overdue_retention_does_not_block_private_product(
    private_data, seeder, db_conn,
):
    """Tenant B's overdue row is not a global outage switch for tenant A."""
    seeder.brand(900002, 502, name="B tenant")
    event_id = seeder.observation(
        outcome="recommended", processing_state="promoted",
        owner_user_id=502, brand_id=900002,
    )
    A.refresh_scope(
        db_conn, scope_type="private_brand", granularity="day", day=DAY,
        computed_at=CT + timedelta(minutes=1),
    )
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE public.geo_observation_events "
            "SET retention_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (event_id,),
        )
    db_conn.commit()

    transport = FakeTransport()
    c = _client(transport=transport)
    headers = auth_headers(OWNER, [BRAND])
    assert c.get(
        f"/api/geo-observation/brands/{BRAND}/summary", headers=headers,
    ).status_code == 200
    assert c.post(
        f"/api/geo-observation/brands/{BRAND}/insights", headers=headers,
    ).status_code == 200
    assert len(transport.calls) == 1


def test_retention_expiry_after_snapshot_selection_is_fenced_before_paid_call(
    private_data,
):
    transport = FakeTransport()
    deps = build_test_deps(transport=transport)
    original_run = deps.insight_engine._run_job

    def expire_then_run(conn, job_id, lease_token, fact_pack):
        barrier = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
        try:
            with barrier.cursor() as cur:
                cur.execute(
                    "UPDATE public.geo_observation_events "
                    "SET retention_until=clock_timestamp()-interval '1 second' "
                    "WHERE processing_state='promoted'"
                )
            barrier.commit()
        finally:
            barrier.close()
        return original_run(conn, job_id, lease_token, fact_pack)

    deps.insight_engine._run_job = expire_then_run
    c = TestClient(build_test_app(deps))
    response = c.post(
        f"/api/geo-observation/brands/{BRAND}/insights",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SEMANTIC_INSIGHT_UNAVAILABLE"
    assert transport.calls == []


@pytest.mark.parametrize("scope_type", ["private_brand", "public_industry"])
def test_scope_revision_lock_linearizes_read_against_withdrawal(
    seeder, db_conn, scope_type,
):
    import threading
    import time

    if scope_type == "private_brand":
        seeder.brand(BRAND, OWNER)
        event_id = seeder.observation(
            outcome="recommended", processing_state="promoted",
            owner_user_id=OWNER, brand_id=BRAND,
        )
        scope_args = {"owner_user_id": OWNER, "brand_id": BRAND}
    else:
        event_id = seeder.observation(
            outcome="recommended", processing_state="promoted",
            source_type="paid_diagnosis", owner_user_id=OWNER, brand_id=BRAND,
            industry_key="enterprise-service",
        )
        scope_args = {"industry_key": "enterprise-service"}
    A.refresh_scope(
        db_conn, scope_type=scope_type, granularity="day", day=DAY,
        computed_at=CT,
    )
    db_conn.commit()

    reader = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    product_api._begin_exact_snapshot(reader, A.DEFAULT_POLICY_BASIS_HASH)
    receipt, problems = repo.published_scope_snapshot(
        reader, policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH,
        scope_type=scope_type, granularity="day", **scope_args,
    )
    assert receipt is not None and problems == []

    committed = threading.Event()
    failure = []

    def withdraw():
        writer = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
        try:
            with writer.cursor() as cur:
                cur.execute(
                    "UPDATE public.geo_observation_events "
                    "SET processing_state='withdrawn',withdrawn_at=NOW() WHERE id=%s",
                    (event_id,),
                )
            writer.commit()
            committed.set()
        except Exception as exc:  # pragma: no cover - diagnostic path
            failure.append(exc)
        finally:
            writer.close()

    worker = threading.Thread(target=withdraw)
    worker.start()
    time.sleep(0.25)
    assert not committed.is_set(), "withdrawal committed through the published receipt lock"
    reader.rollback()
    reader.close()
    worker.join(timeout=5)
    assert not failure and committed.is_set()

    response = _client().get(
        (
            f"/api/geo-observation/brands/{BRAND}/summary"
            if scope_type == "private_brand"
            else "/api/geo-observation/industries/enterprise-service/baseline"
        ),
        headers=(
            auth_headers(OWNER, [BRAND])
            if scope_type == "private_brand"
            else auth_headers(700, [])
        ),
    )
    assert response.status_code == 503


def test_withdrawal_during_provider_call_never_returns_or_replays_summary(
    private_data, db_conn,
):
    import threading

    started = threading.Event()
    release = threading.Event()

    class BarrierTransport(FakeTransport):
        def chat(self, request_body, **kwargs):
            self.calls.append(request_body)
            started.set()
            assert release.wait(timeout=5)
            return {
                "choices": [{"message": {"content": json.dumps({
                    "summary": "REVOKED_PRIVATE_SUMMARY",
                    "evidence_refs": [],
                    "allowed_actions": ["add_evidence"],
                    "state": "ok",
                })}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

    transport = BarrierTransport()
    client = _client(transport=transport)
    result = {}

    def request_insight():
        result["response"] = client.post(
            f"/api/geo-observation/brands/{BRAND}/insights",
            headers=auth_headers(OWNER, [BRAND]),
        )

    worker = threading.Thread(target=request_insight)
    worker.start()
    assert started.wait(timeout=5)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE public.geo_observation_events "
            "SET processing_state='withdrawn',withdrawn_at=NOW() "
            "WHERE owner_user_id=%s AND brand_id=%s AND processing_state='promoted'",
            (OWNER, BRAND),
        )
    db_conn.commit()
    release.set()
    worker.join(timeout=10)
    response = result["response"]
    assert response.status_code == 503
    assert "REVOKED_PRIVATE_SUMMARY" not in response.text
    assert len(transport.calls) == 1

    retry = client.post(
        f"/api/geo-observation/brands/{BRAND}/insights",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert retry.status_code == 503
    assert len(transport.calls) == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT state,summary,paid_call_started_at FROM "
            "public.geo_observation_insight_jobs"
        )
        state, summary, paid_started = cur.fetchone()
    assert state == "failed"
    assert summary is None
    assert paid_started is not None

def test_post_provider_snapshot_sql_error_is_durable_and_never_repaid(
    private_data, db_conn, monkeypatch,
):
    original = repo.published_scope_snapshot
    calls = {"n": 0}

    def fail_second_validation(conn, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            with conn.cursor() as cur:
                cur.execute("SELECT definitely_missing_snapshot_column FROM public.geo_observation_events")
        return original(conn, **kwargs)

    monkeypatch.setattr(repo, "published_scope_snapshot", fail_second_validation)
    transport = FakeTransport()
    client = _client(transport=transport)
    response = client.post(
        f"/api/geo-observation/brands/{BRAND}/insights",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 503
    assert len(transport.calls) == 1
    assert "summary" not in response.text.lower()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT state,error_code,summary,paid_call_started_at,paid_call_unknown_at "
            "FROM public.geo_observation_insight_jobs"
        )
        state, error_code, summary, paid_started, unknown_at = cur.fetchone()
    assert state == "failed"
    assert error_code == "SNAPSHOT_REVOKED_RESULT_DISCARDED"
    assert summary is None and paid_started is not None and unknown_at is None

    retry = client.post(
        f"/api/geo-observation/brands/{BRAND}/insights",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert retry.status_code == 503
    assert len(transport.calls) == 1

def test_aggregate_read_does_not_outlive_its_exact_manifest(private_data, db_conn):
    """A control-plane ready result cannot authorize an orphan aggregate.

    Simulate the manifest changing after readiness but before the repository
    read.  The aggregate row remains in place, yet neither the product summary
    nor a paid insight may consume it.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM public.geo_observation_aggregate_refresh_manifest "
            "WHERE policy_basis_hash=%s AND scope_type='private_brand'",
            (A.DEFAULT_POLICY_BASIS_HASH,),
        )
    db_conn.commit()
    assert repo.latest_overall_aggregate(
        db_conn, scope_type="private_brand", granularity="day",
        owner_user_id=OWNER, brand_id=BRAND,
        policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH,
    ) is None

    transport = FakeTransport()
    c = _client(transport=transport)
    headers = auth_headers(OWNER, [BRAND])
    summary = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary", headers=headers,
    )
    assert summary.status_code == 503
    assert summary.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"
    insight = c.post(
        f"/api/geo-observation/brands/{BRAND}/insights", headers=headers,
    )
    assert insight.status_code == 503
    assert insight.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"
    assert transport.calls == []


def test_active_basis_cannot_become_false_ready_when_manifest_and_output_both_disappear(
    private_data, db_conn,
):
    """Empty-vs-empty is not a valid published snapshot after readiness."""
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM public.geo_observation_aggregate_refresh_manifest "
            "WHERE policy_basis_hash=%s",
            (A.DEFAULT_POLICY_BASIS_HASH,),
        )
        cur.execute(
            "DELETE FROM public.geo_observation_aggregates WHERE policy_basis_hash=%s",
            (A.DEFAULT_POLICY_BASIS_HASH,),
        )
    db_conn.commit()

    c = _client()
    response = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"


def test_target_manifest_and_outputs_cannot_disappear_behind_other_basis_receipts(
    private_data, seeder, db_conn,
):
    """A public receipt keeps the global gate green; deleting only the target
    private manifest+rows must still be caught by its independent receipt."""
    for i in range(12):
        seeder.observation(
            outcome="recommended", processing_state="promoted",
            source_type="research_round", owner_user_id=None, brand_id=None,
            user_bucket=f"other-public-u-{i}", brand_bucket=f"other-public-b-{i}",
        )
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        computed_at=CT,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM public.geo_observation_aggregate_refresh_manifest "
            "WHERE policy_basis_hash=%s AND scope_type='private_brand'",
            (A.DEFAULT_POLICY_BASIS_HASH,),
        )
        cur.execute(
            "DELETE FROM public.geo_observation_aggregates "
            "WHERE policy_basis_hash=%s AND scope_type='private_brand'",
            (A.DEFAULT_POLICY_BASIS_HASH,),
        )
    db_conn.commit()
    response = _client().get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"


def test_duplicate_receipt_scope_cell_is_unavailable(private_data, db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE public.geo_observation_aggregate_bucket_revision "
            "SET published_receipt=jsonb_set(published_receipt,'{cells}', "
            "(published_receipt->'cells') || "
            "jsonb_build_array(published_receipt->'cells'->0)) "
            "WHERE scope_type='private_brand' AND bucket_granularity='day' "
            "AND published_receipt @> %s::jsonb",
            (json.dumps({"cells": [{"owner_user_id": OWNER, "brand_id": BRAND}]}),),
        )
        assert cur.rowcount == 1
    db_conn.commit()
    response = _client().get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"

@pytest.mark.parametrize(
    "column,expression",
    [
        ("eligibility_epoch", "eligibility_epoch + 1"),
        ("promotion_sequence_watermark", "promotion_sequence_watermark + 1"),
        ("input_watermark", "input_watermark + interval '1 second'"),
    ],
)
def test_target_aggregate_lineage_mutation_is_unavailable(
    private_data, db_conn, column, expression,
):
    with db_conn.cursor() as cur:
        cur.execute(
            f"UPDATE public.geo_observation_aggregates SET {column}={expression} "
            "WHERE policy_basis_hash=%s AND scope_type='private_brand' "
            "AND owner_user_id=%s AND brand_id=%s",
            (A.DEFAULT_POLICY_BASIS_HASH, OWNER, BRAND),
        )
    db_conn.commit()
    response = _client().get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 503


def test_old_bucket_retention_expiry_does_not_block_current_snapshot(
    private_data, seeder, db_conn,
):
    old_day = date(2026, 6, 1)
    old_event = seeder.observation(
        outcome="recommended", processing_state="promoted",
        owner_user_id=OWNER, brand_id=BRAND,
        observed_at=datetime(2026, 6, 1, 8, tzinfo=timezone.utc),
        contribution_date="2026-06-01",
    )
    A.refresh_scope(
        db_conn, scope_type="private_brand", granularity="day", day=old_day,
        computed_at=CT,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE public.geo_observation_events "
            "SET retention_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (old_event,),
        )
    db_conn.commit()
    response = _client().get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 200
    assert response.json()["summary"]["valid_observations"] == 90


def test_paid_source_overdue_blocks_matching_public_industry_only(seeder, db_conn):
    for i in range(12):
        seeder.observation(
            outcome="recommended", processing_state="promoted",
            source_type="research_round", owner_user_id=None, brand_id=None,
            user_bucket=f"research-u-{i}", brand_bucket=f"research-b-{i}",
        )
    paid_event = seeder.observation(
        outcome="recommended", processing_state="promoted",
        source_type="paid_diagnosis", owner_user_id=OWNER, brand_id=BRAND,
        industry_key="enterprise-service",
    )
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        computed_at=CT,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE public.geo_observation_events "
            "SET retention_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (paid_event,),
        )
    db_conn.commit()
    response = _client().get(
        "/api/geo-observation/industries/enterprise-service/baseline",
        headers=auth_headers(700, []),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"


def test_public_retention_uses_signal_industry_before_event_fallback(seeder, db_conn):
    """Public retention uses the aggregation COALESCE(signal,event) order."""
    for i in range(12):
        seeder.observation(
            outcome="recommended", processing_state="promoted",
            source_type="research_round", owner_user_id=None, brand_id=None,
            industry_key="signal-industry",
            user_bucket=f"signal-u-{i}", brand_bucket=f"signal-b-{i}",
        )
    paid_event = seeder.observation(
        outcome="recommended", processing_state="processing",
        source_type="paid_diagnosis", owner_user_id=OWNER, brand_id=BRAND,
        industry_key="signal-industry",
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE public.geo_observation_events SET industry_key='event-industry' "
            "WHERE id=%s", (paid_event,),
        )
        cur.execute(
            "UPDATE public.geo_observation_events SET processing_state='promoted' "
            "WHERE id=%s", (paid_event,),
        )
    db_conn.commit()
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        computed_at=CT,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE public.geo_observation_events "
            "SET retention_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (paid_event,),
        )
    db_conn.commit()

    response = _client().get(
        "/api/geo-observation/industries/signal-industry/baseline",
        headers=auth_headers(700, []),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"

def test_exact_snapshot_isolation_is_transaction_local_and_pool_safe(private_data):
    from db.connection import _PooledConnection
    from psycopg2.pool import ThreadedConnectionPool

    pool = ThreadedConnectionPool(1, 1, os.environ["TEST_DATABASE_URL"])
    try:
        raw = pool.getconn()
        wrapped = _PooledConnection(raw, pool)
        product_api._begin_exact_snapshot(wrapped, A.DEFAULT_POLICY_BASIS_HASH)
        with wrapped.cursor() as cur:
            cur.execute("SHOW transaction_isolation")
            assert cur.fetchone()[0] == "repeatable read"
        wrapped.close()

        borrowed = pool.getconn()
        assert borrowed is raw
        with borrowed.cursor() as cur:
            cur.execute("SHOW transaction_isolation")
            assert cur.fetchone()[0] == "read committed"
        borrowed.rollback()
        pool.putconn(borrowed)

        raw = pool.getconn()
        wrapped = _PooledConnection(raw, pool)
        with pytest.raises(Exception):
            product_api._begin_exact_snapshot(wrapped, "f" * 64)
        wrapped.close()
        borrowed = pool.getconn()
        with borrowed.cursor() as cur:
            cur.execute("SHOW transaction_isolation")
            assert cur.fetchone()[0] == "read committed"
        borrowed.rollback()
        pool.putconn(borrowed)
    finally:
        pool.closeall()


def test_scoped_receipt_query_count_is_constant_with_unrelated_history(
    private_data, db_conn,
):
    class CountingCursor:
        def __init__(self, inner, counter):
            self.inner = inner
            self.counter = counter
        def __enter__(self):
            self.inner.__enter__()
            return self
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
        def execute(self, *args, **kwargs):
            self.counter[0] += 1
            return self.inner.execute(*args, **kwargs)
        def __getattr__(self, name):
            return getattr(self.inner, name)

    class CountingConnection:
        def __init__(self, inner):
            self.inner = inner
            self.counter = [0]
        def cursor(self, *args, **kwargs):
            return CountingCursor(self.inner.cursor(*args, **kwargs), self.counter)
        def __getattr__(self, name):
            return getattr(self.inner, name)

    with db_conn.cursor() as cur:
        for offset in range(30):
            bucket = date(2025, 1, 1) + timedelta(days=offset)
            cur.execute(
                """INSERT INTO public.geo_observation_aggregate_bucket_revision(
                       scope_type,bucket_granularity,bucket_start,epoch,dirty,published_receipt)
                     VALUES('private_brand','day',%s,0,FALSE,%s::jsonb)
                     ON CONFLICT DO NOTHING""",
                (
                    bucket,
                    json.dumps({
                        "policy_basis_hash": A.DEFAULT_POLICY_BASIS_HASH,
                        "contract_version": C.CONTRACT_VERSION,
                        "aggregation_version": C.AGGREGATION_VERSION,
                        "metric_version": C.METRIC_VERSION,
                        "cells": [{"owner_user_id": 999, "brand_id": 999,
                                   "aggregate_keys": ["x"]}],
                    }),
                ),
            )
    db_conn.commit()
    counted = CountingConnection(db_conn)
    receipt, problems = repo.published_scope_snapshot(
        counted, policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH,
        scope_type="private_brand", granularity="day",
        owner_user_id=OWNER, brand_id=BRAND,
    )
    assert receipt is not None and problems == []
    assert counted.counter[0] == 4

def test_unassigned_admin_cannot_read_customer_private_question_or_evidence(
    private_data, db_conn,
):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT event_uuid::text FROM public.geo_observation_events "
            "WHERE owner_user_id=%s AND brand_id=%s ORDER BY id LIMIT 1",
            (OWNER, BRAND),
        )
        observation_id = cur.fetchone()[0]
    c = _client()
    headers = auth_headers(1, [], admin=True)
    questions = c.get(
        f"/api/geo-observation/brands/{BRAND}/questions", headers=headers,
    )
    evidence = c.get(
        f"/api/geo-observation/brands/{BRAND}/evidence/{observation_id}",
        headers=headers,
    )
    assert questions.status_code == 403
    assert evidence.status_code == 403


# ---------------------------------------------------------------------------
# Public industry k-anonymity gate
# ---------------------------------------------------------------------------
def test_private_tenant_overdue_does_not_block_public_snapshot(seeder, db_conn):
    for i in range(12):
        seeder.observation(
            outcome="recommended", processing_state="promoted",
            source_type=("research_round" if i % 2 else "recurring_monitoring"),
            user_bucket=f"public-u-{i}", brand_bucket=f"public-b-{i}",
        )
    event_id = seeder.observation(
        outcome="recommended", processing_state="promoted",
        owner_user_id=502, brand_id=900002, industry_key="unrelated-industry",
    )
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        computed_at=CT,
    )
    A.refresh_scope(
        db_conn, scope_type="private_brand", granularity="day", day=DAY,
        computed_at=CT,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE public.geo_observation_events "
            "SET retention_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (event_id,),
        )
    db_conn.commit()
    response = _client().get(
        "/api/geo-observation/industries/enterprise-service/baseline",
        headers=auth_headers(700, []),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_product_disabled_flag_blocks_endpoints(seeder, db_conn):
    """P0: when AI-2 product_enabled=false, product endpoints must NOT serve
    (fail-closed), not return 200. Reads the flag from the real nested shape."""
    seeder.brand(BRAND, OWNER)
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=OWNER, brand_id=BRAND)
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()
    c = _client(policy={"policy_version": 5, "policy": {},
                        "effective_flags": {"product_enabled": False, "aggregation_enabled": False}})
    r_pub = c.get("/api/geo-observation/industries/enterprise-service/baseline", headers=auth_headers(700, []))
    assert r_pub.status_code == 503
    assert r_pub.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"
    r_priv = c.get(f"/api/geo-observation/brands/{BRAND}/summary", headers=auth_headers(OWNER, [BRAND]))
    assert r_priv.status_code == 503  # whole product feature behind the flag


def test_active_basis_never_falls_back_to_old_private_or_industry_rows(seeder, db_conn):
    """Current public data must not make old-basis private/industry rows eligible."""
    from services.geo_observation_analytics import repository as repo

    old_basis = "f" * 64
    seeder.brand(BRAND, OWNER)
    old_event_id = seeder.observation(
        outcome="recommended", processing_state="promoted",
        owner_user_id=OWNER, brand_id=BRAND, industry_key="old-industry",
    )
    old_config = A.AggregationConfig(policy_basis_hash=old_basis, policy_version="17")
    A.refresh_scope(
        db_conn, scope_type="private_brand", granularity="day", day=DAY,
        config=old_config, computed_at=CT,
    )
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        config=old_config, computed_at=CT,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE geo_observation_events SET processing_state='withdrawn',withdrawn_at=NOW() "
            "WHERE id=%s", (old_event_id,)
        )
    db_conn.commit()
    seeder.observation(
        outcome="recommended", processing_state="promoted",
        owner_user_id=None, brand_id=None, industry_key="current-industry",
        source_type="research_round",
    )
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        config=A.AggregationConfig(), computed_at=CT,
    )
    db_conn.commit()

    c = _client()
    response = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["valid_observations"] == 0
    assert repo.list_public_industries(
        db_conn, granularity="day",
        policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH,
    ) == ["current-industry"]


def test_product_runtime_ignores_lure_and_pg_temp_truth_tables(
    private_data, db_conn,
):
    with db_conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS lure")
        cur.execute("DROP VIEW IF EXISTS lure.geo_observation_aggregates")
        cur.execute(
            "CREATE VIEW lure.geo_observation_aggregates AS "
            "SELECT * FROM public.geo_observation_aggregates WHERE FALSE"
        )
    db_conn.commit()

    deps = build_test_deps()
    public_conn = deps.get_conn

    def lure_first_conn():
        conn = public_conn()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP VIEW geo_observation_aggregates AS "
                "SELECT * FROM public.geo_observation_aggregates WHERE FALSE"
            )
            cur.execute(
                "CREATE TEMP VIEW geo_observation_insight_jobs AS "
                "SELECT * FROM public.geo_observation_insight_jobs WHERE FALSE"
            )
            cur.execute("SET search_path=pg_temp,lure,public")
        conn.commit()
        return conn

    deps.get_conn = lure_first_conn
    c = TestClient(build_test_app(deps))
    headers = auth_headers(OWNER, [BRAND])
    summary = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary", headers=headers
    )
    assert summary.status_code == 200
    assert summary.json()["summary"]["valid_observations"] == 90
    insight = c.post(
        f"/api/geo-observation/brands/{BRAND}/insights", headers=headers
    )
    assert insight.status_code == 200
    assert insight.json()["state"] == "completed"


def test_product_bit_version_bump_keeps_same_aggregate_basis(seeder, db_conn):
    seeder.brand(BRAND, OWNER)
    seeder.observation(
        outcome="recommended", processing_state="promoted",
        owner_user_id=OWNER, brand_id=BRAND,
    )
    A.refresh_scope(
        db_conn, scope_type="private_brand", granularity="day", day=DAY,
        computed_at=CT,
    )
    db_conn.commit()
    bumped = deepcopy(DEFAULT_TEST_POLICY)
    bumped["policy_version"] = int(bumped["policy_version"]) + 1
    c = _client(policy=bumped)
    response = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 200
    assert response.json()["summary"]["valid_observations"] == 1


def test_product_enabled_without_frozen_basis_is_unavailable(seeder, db_conn):
    seeder.brand(BRAND, OWNER)
    policy_without_basis = deepcopy(DEFAULT_TEST_POLICY)
    policy_without_basis["aggregate_policy_basis"] = None
    c = _client(policy=policy_without_basis)
    response = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 503


def test_product_routes_fail_closed_when_runtime_snapshot_epoch_is_dirty(private_data):
    c = _client(aggregate_readiness=lambda _basis: {
        "status": "unavailable",
        "problems": ["eligibility epoch 落后"],
    })
    response = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"


def test_public_baseline_insufficient_when_below_kanon(seeder, db_conn):
    # only 2 valid observations, 1 user bucket -> below k-anon
    seeder.observation(outcome="recommended", processing_state="promoted", user_bucket="u1", brand_bucket="b1")
    seeder.observation(outcome="not_mentioned", processing_state="promoted", user_bucket="u1", brand_bucket="b1")
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()
    c = _client()
    r = c.get("/api/geo-observation/industries/enterprise-service/baseline",
              headers=auth_headers(700, []))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "insufficient_samples"
    assert body["baseline"] is None
    assert "sample_scope" in body  # explicit scope, no silent fallback


def test_policy_can_raise_kanon_threshold(seeder, db_conn):
    """P1#5: a raised policy k-anon threshold takes effect on public endpoints
    (not fixed at wiring time). A cell that meets the DEFAULT gate is suppressed
    when policy raises the minimum."""
    sources = ["research_round", "paid_diagnosis", "recurring_monitoring"]
    for i in range(12):
        seeder.observation(outcome="recommended", processing_state="promoted",
                           user_bucket=f"u{i}", brand_bucket=f"b{i}", source_type=sources[i % 3])
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()
    _flags = {"product_enabled": True, "aggregation_enabled": True}
    # policy min brands 3 (effective 10 via cap) -> 12 brands passes -> ok
    ok = _client(policy={"policy_version": 1, "effective_flags": _flags,
                         "policy": {"public_min_independent_brands": 3, "public_min_source_types": 2,
                                    "max_single_brand_share_bps": 1000}})
    r1 = ok.get("/api/geo-observation/industries/enterprise-service/baseline", headers=auth_headers(700, []))
    assert r1.json()["status"] == "ok"
    # policy RAISES min independent brands to 15 -> 12 < 15 -> insufficient
    strict = _client(policy={"policy_version": 2, "effective_flags": _flags,
                             "policy": {"public_min_independent_brands": 15, "public_min_source_types": 2,
                                        "max_single_brand_share_bps": 1000}})
    r2 = strict.get("/api/geo-observation/industries/enterprise-service/baseline", headers=auth_headers(700, []))
    assert r2.json()["status"] == "insufficient_samples"


def test_source_patterns_domain_level_kanon(seeder, db_conn):
    """P1#4: a domain contributed by a SINGLE client (repeated) is suppressed;
    a domain from >=3 users / >=3 brands / >=2 sources is returned."""
    dom_single = [{"domain": "single-client.example", "type": "citation"}]
    dom_broad = [{"domain": "broad.example", "type": "citation"}]
    sources = ["research_round", "paid_diagnosis", "recurring_monitoring"]
    # single client repeats its domain 3x (same user/brand) -> domain suppressed
    for i in range(3):
        seeder.observation(outcome="recommended", processing_state="promoted",
                           user_bucket="solo", brand_bucket="solo-b", source_type="research_round",
                           prompt_family_key=f"fam-s{i}", source_domains=dom_single)
    # broad domain from 9 distinct users/brands + all 3 source types (also makes
    # the OVERALL cell pass k-anon so we exercise DOMAIN-level suppression).
    for i in range(12):
        seeder.observation(outcome="recommended", processing_state="promoted",
                           user_bucket=f"u{i}", brand_bucket=f"b{i}", source_type=sources[i % 3],
                           prompt_family_key=f"fam-b{i}", source_domains=dom_broad)
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()
    c = _client()
    r = c.get("/api/geo-observation/industries/enterprise-service/source-patterns",
              headers=auth_headers(700, []))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"  # overall cell passes k-anon
    domains = {it["domain"] for it in body["patterns"]["items"]}
    assert "broad.example" in domains              # broad domain returned
    assert "single-client.example" not in domains  # single-client domain suppressed


def test_source_patterns_are_bound_to_active_aggregate_watermark(seeder, db_conn):
    """Post-aggregate promoted sources stay invisible until the basis refreshes."""
    sources = ["research_round", "paid_diagnosis", "recurring_monitoring"]
    stable = [{"domain": "stable.example", "type": "citation"}]
    new = [{"domain": "new-after-snapshot.example", "type": "citation"}]
    for i in range(12):
        seeder.observation(
            outcome="recommended", processing_state="promoted",
            source_type=sources[i % 3], source_domains=stable,
            user_bucket=f"stable-u{i}", brand_bucket=f"stable-b{i}",
            prompt_family_key=f"stable-f{i}",
            observed_at=datetime(2026, 7, 10, 8, tzinfo=timezone.utc),
        )
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        computed_at=CT,
    )
    for i in range(9):
        seeder.observation(
            outcome="recommended", processing_state="promoted",
            source_type=sources[i % 3], source_domains=new,
            user_bucket=f"new-u{i}", brand_bucket=f"new-b{i}",
            prompt_family_key=f"new-f{i}",
            # Deliberately backdated before the published observed_at watermark:
            # only the monotonic promotion sequence can keep it out of snapshot 1.
            observed_at=datetime(2026, 7, 10, 7, tzinfo=timezone.utc),
        )
    db_conn.commit()

    c = _client()
    path = "/api/geo-observation/industries/enterprise-service/source-patterns"
    before = c.get(path, headers=auth_headers(700, [])).json()
    before_domains = {item["domain"] for item in before["patterns"]["items"]}
    assert "stable.example" in before_domains
    assert "new-after-snapshot.example" not in before_domains

    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        computed_at=datetime(2026, 7, 11, 1, tzinfo=timezone.utc),
    )
    db_conn.commit()
    after = c.get(path, headers=auth_headers(700, [])).json()
    after_domains = {item["domain"] for item in after["patterns"]["items"]}
    assert "new-after-snapshot.example" in after_domains


def test_admin_health_maps_ai1_shape_and_fail_closed(seeder, db_conn):
    """P1#5: admin overview consumes AI-1's real health shape (platform_key +
    status) and enabled_by_policy from POLICY; default (unwired) deps are
    fail-closed (readiness unavailable)."""
    seeder.brand(BRAND, OWNER)
    db_conn.commit()
    # unwired policy (production default_deps returns policy_version=None) -> fail-closed
    c0 = _client(policy={"policy_version": None})
    r0 = c0.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r0.json()["readiness"] == "unavailable"  # policy_version None => not ready
    # wired AI-1 health shape (platform_key + status) + NESTED AI-2 policy platforms
    c = _client(
        platform_health=[{"platform_key": "doubao", "provider_key": "volcengine",
                          "model_key": "doubao-pro", "surface_key": "doubao_ark_api_search",
                          "status": "healthy"}],
        policy={"policy_version": 7, "effective_flags": {"product_enabled": True},
                "policy": {"platforms": [{"platform_key": "doubao", "enabled": True,
                                          "surface_key": "doubao_ark_api_search"}]}},
    )
    r = c.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r.status_code == 200
    ph = r.json()["platform_health"][0]
    assert ph["platform"] == "豆包"
    assert ph["enabled_by_policy"] is True
    assert ph["runtime_health"] == "healthy"
    assert ph["surface_key"] == "doubao_ark_api_search"  # admin sees exact surface
    assert ph["provider_key"] == "volcengine"


def test_readiness_not_ready_unless_pipeline_running(seeder, db_conn, monkeypatch):
    """P1#1/P1#3: readiness is NOT 'ready' when aggregation is off / no aggregate
    exists yet; and a DB probe failure surfaces as unavailable, not a fake 0."""
    # policy wired + product on, but aggregation OFF and no aggregate row exists
    c = _client(policy={"policy_version": 9, "policy": {},
                        "effective_flags": {"product_enabled": True, "aggregation_enabled": False}})
    r = c.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r.json()["readiness"] == "unavailable"

    # seed a real aggregate for the remaining cases
    seeder.brand(BRAND, OWNER)
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=OWNER, brand_id=BRAND, platform_key="doubao")
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()
    enabled_policy = {"policy_version": 10,
                      "effective_flags": {"product_enabled": True, "aggregation_enabled": True},
                      "policy": {"platforms": [{"platform_key": "doubao", "enabled": True,
                                                  "surface_key": "doubao_ark_api_search"}]}}

    # ISOLATES the aggregation_enabled clause: aggregate now EXISTS, so it is
    # aggregation being OFF (not a missing aggregate) that forces unavailable.
    c_off = _client(
        platform_health=[{"platform_key": "doubao", "surface_key": "doubao_ark_api_search",
                          "status": "healthy"}],
        policy={"policy_version": 11,
                "effective_flags": {"product_enabled": True, "aggregation_enabled": False},
                "policy": {"platforms": [{"platform_key": "doubao", "enabled": True,
                                            "surface_key": "doubao_ark_api_search"}]}},
    )
    r_off = c_off.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r_off.json()["readiness"] == "unavailable"

    # the happy 'ready' path: aggregation on + a real aggregate + healthy enabled
    # platform + no backlog. A RETIRED/disabled platform (kimi enabled=false)
    # reported UNAVAILABLE by AI-1 must NOT drag readiness down (enabled-scoped).
    c2 = _client(
        platform_health=[{"platform_key": "doubao", "surface_key": "doubao_ark_api_search",
                          "status": "healthy"},
                         {"platform_key": "kimi", "status": "unavailable"}],
        policy=enabled_policy,
    )
    r2 = c2.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r2.json()["readiness"] == "ready"

    # ...but an ENABLED platform reported unavailable MUST still drag readiness
    # down (guards against the fix over-scoping and hiding real outages).
    c_out = _client(
        platform_health=[{"platform_key": "doubao", "surface_key": "doubao_ark_api_search",
                          "status": "unavailable"}],
        policy=enabled_policy,
    )
    r_out = c_out.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r_out.json()["readiness"] == "unavailable"

    # FAIL-CLOSED empty enabled set: a policy that enumerates ZERO enabled
    # platforms (empty/partially-wired policy.platforms) must NEVER read 'ready',
    # even with aggregation on + aggregate present — otherwise the enabled-scoped
    # health predicate evaluates over [] and swallows a full AI-1 outage.
    c_empty = _client(
        platform_health=[{"platform_key": "doubao", "status": "unavailable"},
                         {"platform_key": "qwen", "status": "unavailable"}],
        policy={"policy_version": 12,
                "effective_flags": {"product_enabled": True, "aggregation_enabled": True},
                "policy": {"platforms": []}},
    )
    r_empty = c_empty.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r_empty.json()["readiness"] == "unavailable"

    # ...and the attention half is enabled-scoped too: a DISABLED platform (kimi)
    # reported degraded/unknown must NOT flip an otherwise-healthy product to
    # attention_required (discriminates the degraded/unknown enabled-scoping).
    c_deg = _client(
        platform_health=[{"platform_key": "doubao", "surface_key": "doubao_ark_api_search",
                          "status": "healthy"},
                         {"platform_key": "kimi", "status": "degraded"}],
        policy=enabled_policy,
    )
    r_deg = c_deg.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r_deg.json()["readiness"] == "ready"

    # DB probe failure (stuck_claim_count raises) => unavailable, never a fake 0.
    import api.geo_observation_product_api as prod_api

    def _boom(_conn):
        raise RuntimeError("simulated lease_until query failure")

    monkeypatch.setattr(prod_api.repo, "stuck_claim_count", _boom)
    c3 = _client(
        platform_health=[{"platform_key": "doubao", "surface_key": "doubao_ark_api_search",
                          "status": "healthy"}],
        policy=enabled_policy,
    )
    r3 = c3.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r3.json()["readiness"] == "unavailable"
    assert r3.json()["counts"]["stuck_claims"] == 0  # probe failed -> reported 0, but readiness unavailable


def test_stuck_claim_count_raises_on_db_error_no_swallow(db_conn):
    """P1#3: the repository probe must RAISE on a DB anomaly (schema/permission/
    connection), never swallow it to a fake 0 that masks the failure."""
    import os
    import psycopg2
    from services.geo_observation_analytics import repository as repo
    dead = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    dead.close()  # any query now raises psycopg2.InterfaceError
    with pytest.raises(Exception):
        repo.stuck_claim_count(dead)


# ---------------------------------------------------------------------------
# Readiness clause isolation: every unavailable/attention clause has ONE test
# that fires it alone from an otherwise-ready baseline (mutation-discriminating).
# ---------------------------------------------------------------------------
def _ready_policy(version: int = 20) -> dict:
    return {"policy_version": version,
            "effective_flags": {"product_enabled": True, "aggregation_enabled": True},
            "policy": {"platforms": [{"platform_key": "doubao", "enabled": True,
                                        "surface_key": "doubao_ark_api_search"}]}}


_READY_HEALTH = [{"platform_key": "doubao", "surface_key": "doubao_ark_api_search",
                  "status": "healthy"}]


def _seed_ready_aggregate(seeder, db_conn):
    """A fully-ready pipeline: brand + one promoted obs + a real private aggregate.
    With _ready_policy() + _READY_HEALTH this baseline reads 'ready'."""
    seeder.brand(BRAND, OWNER)
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=OWNER, brand_id=BRAND, platform_key="doubao")
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()


def _readiness(client) -> str:
    return client.get("/api/admin/geo-observation/overview",
                      headers=auth_headers(1, [], admin=True)).json()["readiness"]


def test_readiness_baseline_is_ready(seeder, db_conn):
    _seed_ready_aggregate(seeder, db_conn)
    assert _readiness(_client(platform_health=_READY_HEALTH, policy=_ready_policy())) == "ready"


def test_readiness_unavailable_when_policy_unwired(seeder, db_conn):
    # U1: policy_version None (unwired) is fail-closed even if all else is ready.
    _seed_ready_aggregate(seeder, db_conn)
    pol = _ready_policy()
    pol["policy_version"] = None
    assert _readiness(_client(platform_health=_READY_HEALTH, policy=pol)) == "unavailable"


def test_readiness_unavailable_when_aggregate_absent(seeder, db_conn):
    # U5: aggregation on + enabled healthy platform + probe ok, but NO aggregate
    # row exists yet -> unavailable (isolates the aggregate-existence clause).
    seeder.brand(BRAND, OWNER)  # brand only; no promoted obs / no refresh
    assert _readiness(_client(platform_health=_READY_HEALTH, policy=_ready_policy())) == "unavailable"


def test_readiness_attention_when_enabled_platform_missing_health(seeder, db_conn):
    # missing_health: doubao is enabled by policy but AI-1 reports no health for it.
    _seed_ready_aggregate(seeder, db_conn)
    assert _readiness(_client(platform_health=[], policy=_ready_policy())) == "attention_required"


def test_readiness_attention_on_pending_review_backlog(seeder, db_conn):
    _seed_ready_aggregate(seeder, db_conn)
    seeder.observation(outcome="recommended", processing_state="pending_review",
                       owner_user_id=OWNER, brand_id=BRAND, platform_key="doubao")
    db_conn.commit()
    assert _readiness(_client(platform_health=_READY_HEALTH, policy=_ready_policy())) == "attention_required"


def test_readiness_attention_on_rejected_backlog(seeder, db_conn):
    _seed_ready_aggregate(seeder, db_conn)
    seeder.observation(outcome="not_mentioned", processing_state="rejected",
                       owner_user_id=OWNER, brand_id=BRAND, platform_key="doubao")
    db_conn.commit()
    assert _readiness(_client(platform_health=_READY_HEALTH, policy=_ready_policy())) == "attention_required"


def test_readiness_attention_on_reconciler_delay(seeder, db_conn):
    # a pending event stuck in the reconciler queue older than the 300s threshold.
    _seed_ready_aggregate(seeder, db_conn)
    eid = seeder.observation(outcome="recommended", processing_state="pending",
                             owner_user_id=OWNER, brand_id=BRAND, platform_key="doubao")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE geo_observation_events SET created_at = NOW() - INTERVAL '2 hours' WHERE id=%s", (eid,))
    db_conn.commit()
    assert _readiness(_client(platform_health=_READY_HEALTH, policy=_ready_policy())) == "attention_required"


def test_readiness_attention_on_stuck_claims(seeder, db_conn):
    # a processing event leased past its lease_until (a stuck claim).
    _seed_ready_aggregate(seeder, db_conn)
    eid = seeder.observation(outcome="recommended", processing_state="processing",
                             owner_user_id=OWNER, brand_id=BRAND, platform_key="doubao")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE geo_observation_events SET lease_until = NOW() - INTERVAL '1 hour' WHERE id=%s", (eid,))
    db_conn.commit()
    assert _readiness(_client(platform_health=_READY_HEALTH, policy=_ready_policy())) == "attention_required"


# ---------------------------------------------------------------------------
# P1: readiness matches health by the EXACT selected (platform_key, surface_key)
# ---------------------------------------------------------------------------
def _deepseek_policy(surface: str, version: int = 30) -> dict:
    return {"policy_version": version,
            "effective_flags": {"product_enabled": True, "aggregation_enabled": True},
            "policy": {"platforms": [{"platform_key": "deepseek", "enabled": True,
                                      "surface_key": surface}]}}


def test_readiness_selected_surface_absent_is_not_ready(seeder, db_conn):
    # policy selects deepseek_native_with_search, but only the LEGACY surface has
    # health -> the selected surface is missing (no substitution) -> NOT ready.
    _seed_ready_aggregate(seeder, db_conn)
    health = [{"platform_key": "deepseek", "surface_key": "deepseek_dashscope_search_legacy",
               "status": "healthy"}]
    r = _readiness(_client(platform_health=health,
                           policy=_deepseek_policy("deepseek_native_with_search")))
    assert r == "attention_required"  # selected surface has no health => missing, not ready


def test_readiness_unavailable_when_selected_surface_down(seeder, db_conn):
    # the SELECTED surface itself is reported unavailable -> unavailable.
    _seed_ready_aggregate(seeder, db_conn)
    health = [{"platform_key": "deepseek", "surface_key": "deepseek_native_with_search",
               "status": "unavailable"}]
    r = _readiness(_client(platform_health=health,
                           policy=_deepseek_policy("deepseek_native_with_search")))
    assert r == "unavailable"


def test_readiness_non_selected_surface_does_not_drag(seeder, db_conn):
    # policy selects the healthy legacy surface; a NON-selected surface on the
    # same platform is unavailable -> must NOT drag readiness (still ready).
    _seed_ready_aggregate(seeder, db_conn)
    health = [{"platform_key": "deepseek", "surface_key": "deepseek_dashscope_search_legacy",
               "status": "healthy"},
              {"platform_key": "deepseek", "surface_key": "deepseek_native_with_search",
               "status": "unavailable"}]
    r = _readiness(_client(platform_health=health,
                           policy=_deepseek_policy("deepseek_dashscope_search_legacy")))
    assert r == "ready"  # non-selected surface's outage is ignored


def test_readiness_missing_surface_keys_cannot_match_to_ready(seeder, db_conn):
    """Malformed policy/health rows that both omit surface_key must not collide
    as an apparently exact match. The policy itself is invalid, so fail closed."""
    _seed_ready_aggregate(seeder, db_conn)
    policy = {"policy_version": 31,
              "effective_flags": {"product_enabled": True, "aggregation_enabled": True},
              "policy": {"platforms": [{"platform_key": "doubao", "enabled": True}]}}
    health = [{"platform_key": "doubao", "status": "healthy"}]
    assert _readiness(_client(platform_health=health, policy=policy)) == "unavailable"


def test_readiness_duplicate_enabled_platform_selection_is_unavailable(seeder, db_conn):
    _seed_ready_aggregate(seeder, db_conn)
    policy = _ready_policy(32)
    policy["policy"]["platforms"].append(
        {"platform_key": "doubao", "enabled": True, "surface_key": "other_explicit"})
    health = _READY_HEALTH + [
        {"platform_key": "doubao", "surface_key": "other_explicit", "status": "healthy"}]
    assert _readiness(_client(platform_health=health, policy=policy)) == "unavailable"


# ---------------------------------------------------------------------------
# P1: AI-1 collection control-plane readiness folded into admin readiness
# ---------------------------------------------------------------------------
def test_readiness_unavailable_when_collection_provider_not_wired(seeder, db_conn):
    # collection_readiness_provider is None (integrator did not wire AI-1) -> fail-closed.
    _seed_ready_aggregate(seeder, db_conn)
    c = _client(platform_health=_READY_HEALTH, policy=_ready_policy(), collection_readiness=None)
    assert _readiness(c) == "unavailable"


def test_readiness_unavailable_when_collection_driver_reaper_unwired(seeder, db_conn):
    # AI-1 scheduler_wiring.check_readiness() shape {ready: False, problems:[...]}.
    _seed_ready_aggregate(seeder, db_conn)
    coll = _collection(
        "unavailable", ready=False,
        problems=["sampling driver 未注入", "reservation reaper 未注入"],
    )
    c = _client(platform_health=_READY_HEALTH, policy=_ready_policy(), collection_readiness=coll)
    assert _readiness(c) == "unavailable"


def test_readiness_unavailable_when_collection_blocked(seeder, db_conn):
    # AI-1 registry.readiness() status 'blocked' (surface has no adapter) -> unavailable.
    _seed_ready_aggregate(seeder, db_conn)
    coll = _collection("blocked", ready=False, problems=["surface 无 adapter"])
    c = _client(platform_health=_READY_HEALTH, policy=_ready_policy(), collection_readiness=coll)
    assert _readiness(c) == "unavailable"


def test_readiness_attention_when_collection_substitution_pending(seeder, db_conn):
    # AI-1 registry.readiness() status 'attention_required' (current policy selects
    # an unavailable surface needing runtime substitution) -> attention, not ready.
    _seed_ready_aggregate(seeder, db_conn)
    coll = _collection(
        "attention_required", ready=False,
        problems=["deepseek: policy 选 native 运行时替换为 legacy"],
    )
    c = _client(platform_health=_READY_HEALTH, policy=_ready_policy(), collection_readiness=coll)
    assert _readiness(c) == "attention_required"


def test_readiness_unavailable_when_collection_probe_raises(seeder, db_conn):
    # a collection provider that raises must fail-closed, not be swallowed.
    _seed_ready_aggregate(seeder, db_conn)

    def _boom():
        raise RuntimeError("collection readiness probe boom")

    c = _client(platform_health=_READY_HEALTH, policy=_ready_policy(), collection_readiness=_boom)
    assert _readiness(c) == "unavailable"


def test_readiness_malformed_collection_provider_fails_closed_not_500(seeder, db_conn):
    # a provider returning a truthy NON-dict must degrade to unavailable, never
    # 500 the whole admin overview (fail-OPEN).
    _seed_ready_aggregate(seeder, db_conn)
    for bad in (["driver down"], True, 7, "oops"):
        c = _client(platform_health=_READY_HEALTH, policy=_ready_policy(),
                    collection_readiness=(lambda b=bad: b))
        r = c.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
        assert r.status_code == 200, f"non-dict provider {bad!r} 500'd the overview"
        assert r.json()["readiness"] == "unavailable"
        assert r.json()["collection_readiness"]["status"] == "unavailable"


@pytest.mark.parametrize(
    "overrides",
    [
        {"reconciler_last_success_at": None},
        {"source_watermarks": {}},
        {"source_watermarks": {"paid_diagnosis": "2026-07-20T00:00:00+00:00"}},
        {"duplicate_collection_jobs": ["ai_surface_obs_monitoring_daily"]},
        {"problems": ["仍有异常"]},
        {"policy_version": None},
    ],
)
def test_ready_collection_shape_cannot_hide_missing_terminal_truth(
    seeder, db_conn, overrides
):
    _seed_ready_aggregate(seeder, db_conn)
    coll = _collection("ready", **overrides)
    c = _client(
        platform_health=_READY_HEALTH,
        policy=_ready_policy(),
        collection_readiness=coll,
    )
    assert _readiness(c) == "unavailable"
    response = c.get(
        f"/api/geo-observation/brands/{BRAND}/summary",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert response.status_code == 503


def test_readiness_collection_non_iterable_problems_does_not_crash(seeder, db_conn):
    # a dict whose 'problems' is non-iterable must not crash; status is honored.
    _seed_ready_aggregate(seeder, db_conn)
    c = _client(platform_health=_READY_HEALTH, policy=_ready_policy(),
                collection_readiness={"status": "blocked", "problems": 500})
    r = c.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r.status_code == 200  # non-iterable problems must not 500
    assert r.json()["readiness"] == "unavailable"  # blocked -> unavailable


def test_readiness_contradictory_ready_false_downgrades_ready_status(seeder, db_conn):
    # a result whose status says 'ready' but whose ready flag is False (a wired-
    # but-not-running control plane) must fail-closed to unavailable, never 'ready'.
    _seed_ready_aggregate(seeder, db_conn)
    coll = _collection("ready", ready=False, problems=["driver 未注入"])
    c = _client(platform_health=_READY_HEALTH, policy=_ready_policy(), collection_readiness=coll)
    assert _readiness(c) == "unavailable"


def test_readiness_non_boolean_ready_field_fails_closed(seeder, db_conn):
    _seed_ready_aggregate(seeder, db_conn)
    for bad in (0, 1, "false", "true"):
        coll = _collection("ready", ready=bad)
        c = _client(platform_health=_READY_HEALTH, policy=_ready_policy(),
                    collection_readiness=coll)
        assert _readiness(c) == "unavailable", bad


def test_readiness_policy_provider_failure_fails_closed_not_500(seeder, db_conn):
    _seed_ready_aggregate(seeder, db_conn)

    def _boom():
        raise RuntimeError("policy backend unavailable")

    c = _client(platform_health=_READY_HEALTH, policy=_boom)
    response = c.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert response.status_code == 200
    assert response.json()["readiness"] == "unavailable"


def test_readiness_health_provider_failure_fails_closed_not_500(seeder, db_conn):
    _seed_ready_aggregate(seeder, db_conn)

    def _boom():
        raise RuntimeError("health backend unavailable")

    c = _client(platform_health=_boom, policy=_ready_policy())
    overview = c.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert overview.status_code == 200
    assert overview.json()["readiness"] == "unavailable"
    health = c.get("/api/admin/geo-observation/platform-health", headers=auth_headers(1, [], admin=True))
    assert health.status_code == 503
    assert health.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"


def test_collection_readiness_surfaced_in_overview(seeder, db_conn):
    _seed_ready_aggregate(seeder, db_conn)
    body = _client(platform_health=_READY_HEALTH, policy=_ready_policy()).get(
        "/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True)).json()
    assert body["collection_readiness"]["status"] == "ready"


def test_proxy_channel_note_not_user_facing_admin_sees_surface(seeder, db_conn):
    """P1#2: ordinary customers/service providers see only 'DeepSeek'; the exact
    surface/provider (incl. the 秘塔 proxy) is admin-only — enforced in the raw
    API, not just the frontend."""
    seeder.brand(BRAND, OWNER)
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=OWNER, brand_id=BRAND, platform_key="deepseek",
                       surface_key="deepseek_metaso_proxy")
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()
    c = _client()
    r = c.get(f"/api/geo-observation/brands/{BRAND}/platforms", headers=auth_headers(OWNER, [BRAND]))
    body = r.json()
    dp = next(p for p in body["items"] if p["platform_key"] == "deepseek")
    assert dp["display_name"] == "DeepSeek"
    assert dp["surface_note"] is None
    assert "秘塔" not in json.dumps(body, ensure_ascii=False)  # no proxy leak in raw API
    # admin CAN see the exact surface
    ca = _client(
        platform_health=[{"platform_key": "deepseek", "surface_key": "deepseek_metaso_proxy",
                          "provider_key": "deepseek", "status": "healthy"}],
        policy={"policy_version": 3, "effective_flags": {"product_enabled": True},
                "policy": {"platforms": [{"platform_key": "deepseek", "enabled": True}]}},
    )
    ra = ca.get("/api/admin/geo-observation/platform-health", headers=auth_headers(1, [], admin=True))
    assert ra.json()[0]["surface_key"] == "deepseek_metaso_proxy"


def test_public_baseline_ok_when_kanon_met(seeder, db_conn):
    # >=10 distinct brands (cap-feasible), >=2 source types, all bucketed
    sources = ["research_round", "paid_diagnosis", "recurring_monitoring"]
    for i in range(12):
        seeder.observation(
            outcome="recommended" if i % 2 == 0 else "not_mentioned",
            processing_state="promoted", user_bucket=f"u{i}", brand_bucket=f"b{i}",
            source_type=sources[i % 3],
        )
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()
    c = _client()
    r = c.get("/api/geo-observation/industries/enterprise-service/baseline",
              headers=auth_headers(700, []))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["baseline"]["valid_observations"] == 12
    # public DTO carries NO owner/brand/aggregate_key
    _assert_no_public_leak(body)


# ---------------------------------------------------------------------------
# Semantic insight lifecycle (fake transport) + idempotency
# ---------------------------------------------------------------------------
def test_insight_create_completes_and_is_idempotent(private_data):
    transport = FakeTransport()
    c = _client(transport=transport)
    r1 = c.post(f"/api/geo-observation/brands/{BRAND}/insights",
                headers={**auth_headers(OWNER, [BRAND]), "X-Request-Id": "req-1"})
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["state"] == "completed"
    assert body1["summary"]
    # same brand + same aggregate => same input hash => no new model call
    r2 = c.post(f"/api/geo-observation/brands/{BRAND}/insights",
                headers={**auth_headers(OWNER, [BRAND]), "X-Request-Id": "req-2"})
    assert r2.status_code == 200
    assert len(transport.calls) == 1  # idempotent: exactly one model call


def test_completed_insight_is_unreadable_after_snapshot_revision(private_data, db_conn):
    transport = FakeTransport()
    c = _client(transport=transport)
    created = c.post(
        f"/api/geo-observation/brands/{BRAND}/insights",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    assert c.get(
        f"/api/geo-observation/brands/{BRAND}/insights/{job_id}",
        headers=auth_headers(OWNER, [BRAND]),
    ).status_code == 200

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM geo_observation_events WHERE owner_user_id=%s AND brand_id=%s "
            "AND processing_state='promoted' ORDER BY id LIMIT 1",
            (OWNER, BRAND),
        )
        event_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE geo_observation_events SET processing_state='withdrawn',withdrawn_at=NOW() "
            "WHERE id=%s", (event_id,),
        )
    db_conn.commit()
    stale = c.get(
        f"/api/geo-observation/brands/{BRAND}/insights/{job_id}",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert stale.status_code == 503

    A.refresh_scope(
        db_conn, scope_type="private_brand", granularity="day", day=DAY,
        computed_at=datetime(2026, 7, 11, 1, tzinfo=timezone.utc),
    )
    still_stale = c.get(
        f"/api/geo-observation/brands/{BRAND}/insights/{job_id}",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert still_stale.status_code == 503
    replacement = c.post(
        f"/api/geo-observation/brands/{BRAND}/insights",
        headers=auth_headers(OWNER, [BRAND]),
    )
    assert replacement.status_code == 200
    assert replacement.json()["job_id"] != job_id


def test_insight_get_is_owner_brand_scoped_idor(seeder, db_conn):
    """A job created for tenant B's brand must not be readable via any brand
    tenant A owns (object-level authorization, no IDOR)."""
    # tenant B: brand 200 owner 200 with data + an insight
    seeder.brand(200, 200, name="B 品牌")
    for _ in range(5):
        seeder.observation(outcome="recommended", processing_state="promoted",
                           owner_user_id=200, brand_id=200)
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY, computed_at=CT)
    # tenant A: brand 100 owner 100 (unrelated)
    seeder.brand(100, 100, name="A 品牌")
    db_conn.commit()

    c = _client()
    r = c.post("/api/geo-observation/brands/200/insights", headers=auth_headers(200, [200]))
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    # A cannot read B's job via A's own brand 100
    r2 = c.get(f"/api/geo-observation/brands/100/insights/{job_id}", headers=auth_headers(100, [100]))
    assert r2.status_code == 503  # OBSERVATION_UNAVAILABLE — not another tenant's job
    assert r2.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"
    # A cannot read it via brand 200 either (no access to brand 200)
    r3 = c.get(f"/api/geo-observation/brands/200/insights/{job_id}", headers=auth_headers(100, [100]))
    assert r3.status_code == 403
    # the legitimate owner can read it
    r4 = c.get(f"/api/geo-observation/brands/200/insights/{job_id}", headers=auth_headers(200, [200]))
    assert r4.status_code == 200
    assert r4.json()["job_id"] == job_id


def test_insight_failure_surfaces_unavailable(private_data):
    transport = FakeTransport(fail=True)
    c = _client(transport=transport)
    r = c.post(f"/api/geo-observation/brands/{BRAND}/insights",
               headers=auth_headers(OWNER, [BRAND]))
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["code"] == "SEMANTIC_INSIGHT_UNAVAILABLE"
    assert body["detail"]["retryable"] is False


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
def test_admin_overview_requires_admin(private_data):
    c = _client()
    r = c.get("/api/admin/geo-observation/overview", headers=auth_headers(OWNER, [BRAND]))
    assert r.status_code == 403


def test_admin_overview_ok(private_data):
    c = _client(platform_health=[{"platform": "豆包", "enabled_by_policy": True, "runtime_health": "healthy"}])
    r = c.get("/api/admin/geo-observation/overview", headers=auth_headers(1, [], admin=True))
    assert r.status_code == 200
    body = r.json()
    assert "counts" in body and "readiness" in body
    assert body["platform_health"][0]["platform"] == "豆包"


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/geo-observation/model-shifts",
        "/api/admin/geo-observation/content-opportunities",
    ],
)
def test_admin_aggregate_views_fail_closed_when_runtime_snapshot_is_stale(
    private_data, path,
):
    c = _client(
        aggregate_readiness=lambda _basis: {
            "status": "unavailable",
            "problems": ["closed manifest exceeds freshness SLA"],
        }
    )
    r = c.get(path, headers=auth_headers(1, [], admin=True))
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "OBSERVATION_UNAVAILABLE"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _assert_no_leak(body):
    blob = json.dumps(body, ensure_ascii=False)
    for f in PRIVACY_FORBIDDEN_FIELDS:
        assert f'"{f}"' not in blob, f"private DTO leaked field: {f}"


def _assert_no_public_leak(body):
    blob = json.dumps(body, ensure_ascii=False)
    for f in list(PRIVACY_FORBIDDEN_FIELDS) + ["brand_id"]:
        assert f'"{f}"' not in blob, f"public DTO leaked field: {f}"
