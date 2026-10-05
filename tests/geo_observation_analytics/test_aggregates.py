"""Aggregate repository / refresh job — promoted-only, denominator discipline,
withdrawn exclusion, idempotency, watermark no-regress, per-brand cap."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import psycopg2
from psycopg2.extras import RealDictCursor

from services.geo_observation_analytics import aggregates as A
from services.geo_observation_analytics.aggregates import AggregationConfig

pytestmark = pytest.mark.integration

DAY = date(2026, 7, 10)
CT = datetime(2026, 7, 11, 0, 0, 0, tzinfo=timezone.utc)


def test_promoted_signal_and_contributor_inputs_are_db_immutable(seeder, db_conn):
    event_id = seeder.observation(
        outcome="recommended", processing_state="promoted",
        source_domains=[{"domain": "evidence.example", "type": "citation"}],
    )
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        computed_at=CT,
    )
    before = _fetch_overall(db_conn, "public_industry")
    for statement in (
        "UPDATE geo_observation_signals SET target_outcome='not_mentioned' WHERE event_id=%s",
        "UPDATE geo_observation_signals SET effective_weight_bps=1 WHERE event_id=%s",
        "UPDATE geo_observation_signals SET source_domains='[]'::jsonb WHERE event_id=%s",
        "DELETE FROM geo_observation_signals WHERE event_id=%s",
        "UPDATE geo_observation_contributor_buckets SET contributor_user_bucket='tampered' WHERE event_id=%s",
        "DELETE FROM geo_observation_contributor_buckets WHERE event_id=%s",
    ):
        with pytest.raises(psycopg2.errors.RaiseException):
            with db_conn.cursor() as cur:
                cur.execute(statement, (event_id,))
        db_conn.rollback()
    after = _fetch_overall(db_conn, "public_industry")
    assert after["aggregate_key"] == before["aggregate_key"]
    assert after["explicit_recommendation_rate_bps"] == 10000


def test_promotion_sequence_is_db_assigned_and_withdrawal_cannot_revive(seeder, db_conn):
    event_id = seeder.observation(
        outcome="recommended", processing_state="pending",
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE geo_observation_events SET promotion_seq=1,processing_state='promoted' "
                "WHERE id=%s", (event_id,),
            )
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE geo_observation_events SET processing_state='promoted' WHERE id=%s "
            "RETURNING promotion_seq", (event_id,),
        )
        assigned = int(cur.fetchone()[0])
    assert assigned > 0
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE geo_observation_events SET processing_state='withdrawn',withdrawn_at=NOW() "
            "WHERE id=%s", (event_id,),
        )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.RaiseException):
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE geo_observation_events SET processing_state='processing' WHERE id=%s",
                (event_id,),
            )
    db_conn.rollback()
    with pytest.raises(psycopg2.errors.RaiseException):
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE geo_observation_events SET processing_state='promoted' WHERE id=%s",
                (event_id,),
            )
    db_conn.rollback()

    promoted_id = seeder.observation(outcome="recommended", processing_state="promoted")
    with pytest.raises(psycopg2.errors.RaiseException):
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE geo_observation_events SET processing_state='processing' WHERE id=%s",
                (promoted_id,),
            )
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE geo_observation_events SET updated_at=NOW() WHERE id=%s RETURNING promotion_seq",
            (promoted_id,),
        )
        assert int(cur.fetchone()[0]) > assigned
    db_conn.commit()


def test_published_event_business_fields_and_delete_are_db_immutable(seeder, db_conn):
    event_id = seeder.observation(outcome="recommended", processing_state="promoted")
    mutations = (
        "UPDATE geo_observation_events SET platform_key='deepseek' WHERE id=%s",
        "UPDATE geo_observation_events SET surface_key='deepseek_native_no_search' WHERE id=%s",
        "UPDATE geo_observation_events SET model_revision='r2' WHERE id=%s",
        "UPDATE geo_observation_events SET search_enabled=FALSE WHERE id=%s",
        "UPDATE geo_observation_events SET source_type='research_round' WHERE id=%s",
        "UPDATE geo_observation_events SET source_subkey='rewritten' WHERE id=%s",
        "UPDATE geo_observation_events SET run_index=2 WHERE id=%s",
        "UPDATE geo_observation_events SET observed_at=observed_at-interval '1 day' WHERE id=%s",
        "UPDATE geo_observation_events SET industry_key='other' WHERE id=%s",
        "UPDATE geo_observation_events SET owner_user_id=99 WHERE id=%s",
        "UPDATE geo_observation_events SET brand_id=99 WHERE id=%s",
        "DELETE FROM geo_observation_events WHERE id=%s",
    )
    for statement in mutations:
        with pytest.raises(psycopg2.errors.RaiseException):
            with db_conn.cursor() as cur:
                cur.execute(statement, (event_id,))
        db_conn.rollback()


def test_bucket_scoped_withdrawal_preserves_unrelated_35_day_history(seeder, db_conn):
    from services.geo_observation_analytics import repository as repo

    owner, brand = 501, 900001
    current_event = None
    for offset in range(34, -1, -1):
        day = DAY - timedelta(days=offset)
        event_id = seeder.observation(
            outcome="recommended", processing_state="promoted",
            owner_user_id=owner, brand_id=brand,
            observed_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
                + timedelta(hours=8),
            contribution_date=day.isoformat(),
        )
        A.refresh_scope(
            db_conn, scope_type="private_brand", granularity="day", day=day,
            computed_at=CT,
        )
        if day == DAY:
            current_event = event_id
    for granularity in ("week", "month"):
        A.refresh_scope(
            db_conn, scope_type="private_brand", granularity=granularity, day=DAY,
            computed_at=CT,
        )
    before = repo.trend_series(
        db_conn, scope_type="private_brand", granularity="day",
        owner_user_id=owner, brand_id=brand,
        policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH, limit=60,
    )
    assert len(before) == 35

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE geo_observation_events SET processing_state='withdrawn',withdrawn_at=NOW() "
            "WHERE id=%s", (current_event,),
        )
    db_conn.commit()
    during = repo.trend_series(
        db_conn, scope_type="private_brand", granularity="day",
        owner_user_id=owner, brand_id=brand,
        policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH, limit=60,
    )
    assert len(during) == 34
    assert during[0]["bucket_start"] == DAY - timedelta(days=34)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT bucket_granularity FROM geo_observation_aggregate_bucket_revision "
            "WHERE scope_type='private_brand' AND dirty=TRUE ORDER BY bucket_granularity"
        )
        assert {row[0] for row in cur.fetchall()} == {"day", "week", "month"}

    for granularity in ("day", "week", "month"):
        A.refresh_scope(
            db_conn, scope_type="private_brand", granularity=granularity, day=DAY,
            computed_at=CT + timedelta(hours=1),
        )
    after = repo.trend_series(
        db_conn, scope_type="private_brand", granularity="day",
        owner_user_id=owner, brand_id=brand,
        policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH, limit=60,
    )
    assert len(after) == 34


@pytest.mark.parametrize("scope_type", ["private_brand", "public_industry"])
def test_targeted_refresh_cannot_mutate_published_snapshot(
    seeder, db_conn, scope_type,
):
    kwargs = (
        {"owner_user_id": 501, "brand_id": 900001}
        if scope_type == "private_brand" else {}
    )
    seeder.observation(
        outcome="not_mentioned", processing_state="promoted",
        industry_key="enterprise-service", **kwargs,
    )
    A.refresh_scope(
        db_conn, scope_type=scope_type, granularity="day", day=DAY,
        computed_at=CT,
    )
    before = _fetch_overall(db_conn, scope_type)
    seeder.observation(
        outcome="recommended", processing_state="promoted",
        industry_key="enterprise-service", **kwargs,
    )
    with pytest.raises(RuntimeError, match="禁止定向 refresh"):
        A.refresh_scope(
            db_conn, scope_type=scope_type, granularity="day", day=DAY,
            industry_key="enterprise-service", computed_at=CT,
        )
    db_conn.rollback()
    unchanged = _fetch_overall(db_conn, scope_type)
    assert unchanged["aggregate_key"] == before["aggregate_key"]
    assert unchanged["valid_observations"] == 1
    assert unchanged["presence_rate_bps"] == 0

    A.refresh_scope(
        db_conn, scope_type=scope_type, granularity="day", day=DAY,
        computed_at=datetime(2026, 7, 11, 1, tzinfo=timezone.utc),
    )
    switched = _fetch_overall(db_conn, scope_type)
    assert switched["valid_observations"] == 2
    assert switched["presence_rate_bps"] == 5000


@pytest.mark.parametrize("scope_type", ["private_brand", "public_industry"])
def test_targeted_refresh_is_rejected_before_first_manifest(
    seeder, db_conn, scope_type,
):
    kwargs = (
        {"owner_user_id": 501, "brand_id": 900001}
        if scope_type == "private_brand" else {}
    )
    seeder.observation(
        outcome="recommended", processing_state="promoted",
        industry_key="enterprise-service", **kwargs,
    )
    with pytest.raises(RuntimeError, match="禁止定向 refresh"):
        A.refresh_scope(
            db_conn, scope_type=scope_type, granularity="day", day=DAY,
            industry_key="enterprise-service", computed_at=CT,
        )
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM geo_observation_aggregates "
            "WHERE scope_type=%s AND bucket_start=%s",
            (scope_type, DAY),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT COUNT(*) FROM geo_observation_aggregate_refresh_manifest "
            "WHERE scope_type=%s AND bucket_start=%s",
            (scope_type, DAY),
        )
        assert cur.fetchone()[0] == 0


def _fetch_overall(conn, scope_type, industry_key="enterprise-service"):
    conn.commit()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT * FROM geo_observation_aggregates
               WHERE scope_type=%s AND industry_key=%s
                 AND platform_key IS NULL AND surface_key IS NULL
                 AND source_type IS NULL AND is_branded_prompt IS NULL
                 AND prompt_intent IS NULL AND search_enabled IS NULL""",
            (scope_type, industry_key),
        )
        return cur.fetchone()


def test_promoted_only_and_denominator_discipline(seeder, db_conn):
    # promoted valid
    seeder.observation(outcome="recommended", processing_state="promoted")
    seeder.observation(outcome="not_mentioned", processing_state="promoted")
    # promoted but excluded outcomes (must not enter denominator)
    seeder.observation(outcome="entity_ambiguous", processing_state="promoted")
    seeder.observation(outcome="engine_error", processing_state="promoted")
    # NOT promoted (must be ignored entirely)
    seeder.observation(outcome="recommended", processing_state="pending")
    seeder.observation(outcome="recommended", processing_state="pending_review")
    seeder.observation(outcome="recommended", processing_state="rejected")
    seeder.observation(outcome="recommended", processing_state="private_only")

    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        computed_at=CT,
    )
    row = _fetch_overall(db_conn, "public_industry")
    assert row is not None
    # only 2 valid promoted observations
    assert row["valid_observations"] == 2
    assert row["recommended_count"] == 1
    assert row["not_mentioned_count"] == 1
    assert row["presence_rate_bps"] == 5000  # 1 present / 2 valid


def test_withdrawn_never_aggregates(seeder, db_conn):
    seeder.observation(outcome="recommended", processing_state="promoted")
    # a promoted-then-withdrawn event flips state to 'withdrawn'
    seeder.observation(outcome="recommended", processing_state="withdrawn", withdrawn=True)
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    row = _fetch_overall(db_conn, "public_industry")
    assert row["valid_observations"] == 1  # withdrawn excluded


def test_idempotent_rerun_same_values_one_row(seeder, db_conn):
    for _ in range(3):
        seeder.observation(outcome="recommended", processing_state="promoted")
    seeder.observation(outcome="not_mentioned", processing_state="promoted")

    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    first = _fetch_overall(db_conn, "public_industry")
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY,
                    computed_at=datetime(2026, 7, 12, 0, 0, tzinfo=timezone.utc))
    second = _fetch_overall(db_conn, "public_industry")

    # exactly one overall row
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM geo_observation_aggregates "
            "WHERE scope_type='public_industry' AND platform_key IS NULL "
            "AND surface_key IS NULL AND source_type IS NULL "
            "AND is_branded_prompt IS NULL AND prompt_intent IS NULL "
            "AND search_enabled IS NULL"
        )
        assert cur.fetchone()[0] == 1

    # every metric column identical across reruns (drift = 0)
    ignore = {"updated_at", "computed_at", "created_at", "aggregate_id"}
    for k in first:
        if k in ignore:
            continue
        assert first[k] == second[k], f"drift on {k}: {first[k]} != {second[k]}"


def test_no_regress_by_computed_at(seeder, db_conn):
    """A later-computed recompute wins; an earlier-computed (staler-read) write
    that arrives late does NOT overwrite the fresher row."""
    for _ in range(3):
        seeder.observation(outcome="recommended", processing_state="promoted")
    obs = A.fetch_observations(db_conn, "public_industry", DAY, DAY)
    cfg = AggregationConfig()
    ct_late = datetime(2026, 7, 12, tzinfo=timezone.utc)
    ct_early = datetime(2026, 7, 11, tzinfo=timezone.utc)
    fresh = A.build_aggregate_rows(obs, scope_type="public_industry", granularity="day",
                                   bucket_start=DAY, bucket_end=DAY, config=cfg, computed_at=ct_late)
    A.upsert_aggregates(db_conn, fresh)
    assert _fetch_overall(db_conn, "public_industry")["valid_observations"] == 3
    # a stale recompute over 1 obs, computed EARLIER, must not regress the row
    stale = A.build_aggregate_rows(obs[:1], scope_type="public_industry", granularity="day",
                                   bucket_start=DAY, bucket_end=DAY, config=cfg, computed_at=ct_early)
    of = next(r for r in fresh if r["platform_key"] is None)
    os_ = next(r for r in stale if r["aggregate_key"] == of["aggregate_key"])
    A.upsert_aggregates(db_conn, [os_])
    assert _fetch_overall(db_conn, "public_industry")["valid_observations"] == 3  # not regressed


def test_withdrawal_recompute_drops_max_watermark_obs(seeder, db_conn):
    """Withdrawing the observation that carried the bucket's MAX observed_at must
    still take effect on the next recompute (the fix: guard on computed_at, not
    the data watermark)."""
    e1 = seeder.observation(outcome="recommended", processing_state="promoted",
                            observed_at=datetime(2026, 7, 10, 8, tzinfo=timezone.utc))
    e2 = seeder.observation(outcome="recommended", processing_state="promoted",
                            observed_at=datetime(2026, 7, 10, 9, tzinfo=timezone.utc))
    e3 = seeder.observation(outcome="recommended", processing_state="promoted",
                            observed_at=datetime(2026, 7, 10, 12, tzinfo=timezone.utc))  # max wm
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY,
                    computed_at=datetime(2026, 7, 11, tzinfo=timezone.utc))
    assert _fetch_overall(db_conn, "public_industry")["valid_observations"] == 3
    # withdraw the MAX-watermark event
    with db_conn.cursor() as cur:
        cur.execute("UPDATE geo_observation_events SET processing_state='withdrawn' WHERE id=%s", (e3,))
    db_conn.commit()
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY,
                    computed_at=datetime(2026, 7, 13, tzinfo=timezone.utc))
    row = _fetch_overall(db_conn, "public_industry")
    assert row["valid_observations"] == 2  # withdrawn contribution dropped


def test_all_withdrawn_cell_is_pruned(seeder, db_conn):
    e1 = seeder.observation(outcome="recommended", processing_state="promoted")
    e2 = seeder.observation(outcome="recommended", processing_state="promoted")
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    assert _fetch_overall(db_conn, "public_industry") is not None
    with db_conn.cursor() as cur:
        cur.execute("UPDATE geo_observation_events SET processing_state='withdrawn' WHERE id IN (%s,%s)", (e1, e2))
    db_conn.commit()
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY,
                    computed_at=datetime(2026, 7, 13, tzinfo=timezone.utc))
    # the fully-withdrawn cell now has ZERO rows (withdrawn contribution == 0)
    assert _fetch_overall(db_conn, "public_industry") is None
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM geo_observation_aggregates")
        assert cur.fetchone()[0] == 0


def test_null_dim_breakdown_does_not_collide_with_overall(seeder, db_conn):
    """A breakdown cohort with a NULL grouping value must not collide with the
    all-dims-NULL overall row (aggregate_key collision fix). In the REAL schema
    the only nullable grouping dimension is search_enabled (event column)."""
    seeder.observation(outcome="recommended", processing_state="promoted", search_enabled=True,
                       user_bucket="u1", brand_bucket="b1")
    seeder.observation(outcome="recommended", processing_state="promoted", search_enabled=True,
                       user_bucket="u2", brand_bucket="b2")
    # search_enabled NULL cohort observed at the bucket max time
    seeder.observation(outcome="not_mentioned", processing_state="promoted", search_enabled=None,
                       user_bucket="u3", brand_bucket="b3",
                       observed_at=datetime(2026, 7, 10, 23, tzinfo=timezone.utc))
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    row = _fetch_overall(db_conn, "public_industry")
    # the overall row counts ALL 3 valid observations (not corrupted to 1)
    assert row["valid_observations"] == 3
    # exactly ONE all-dims-NULL row exists
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM geo_observation_aggregates WHERE scope_type='public_industry' "
            "AND platform_key IS NULL AND surface_key IS NULL AND source_type IS NULL "
            "AND is_branded_prompt IS NULL AND prompt_intent IS NULL AND search_enabled IS NULL "
            "AND prompt_family_key IS NULL AND model_revision IS NULL AND search_query_theme IS NULL"
        )
        assert cur.fetchone()[0] == 1
        # the search_enabled breakdown only has the True cohort (NULL cohort skipped)
        cur.execute(
            "SELECT search_enabled, valid_observations FROM geo_observation_aggregates "
            "WHERE scope_type='public_industry' AND search_enabled IS NOT NULL "
            "AND platform_key IS NULL AND surface_key IS NULL AND source_type IS NULL "
            "AND prompt_intent IS NULL AND is_branded_prompt IS NULL"
        )
        rows = cur.fetchall()
    assert rows == [(True, 2)]


def test_model_shift_tiebreak_is_deterministic():
    """Two revisions tied on earliest observed_at must resolve deterministically
    (byte-idempotency across processes)."""
    from services.geo_observation_analytics.aggregates import Observation, _model_shift
    t = datetime(2026, 7, 10, 8, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 10, 9, tzinfo=timezone.utc)

    def obs(rev, outcome, when):
        return Observation(
            event_id=hash((rev, outcome)) & 0xFFFF, owner_user_id=None, brand_id=None,
            source_type="recurring_monitoring", industry_key="x", prompt_family_key=None,
            prompt_intent=None, is_branded_prompt=None, platform_key="doubao",
            surface_key="doubao_ark_api_search", model_revision=rev, search_enabled=True,
            outcome=outcome, position=None, competitor_count=0, citation_count=0, source_count=0,
            quality_score_bps=2000, effective_weight_bps=7000,
            observed_at=when, run_index=1, user_bucket="u", brand_bucket="b",
        )
    # rev A earliest at t; rev B and rev C both first-seen at t1 (tie)
    valid = [obs("A", "recommended", t), obs("B", "not_mentioned", t1), obs("C", "recommended", t1)]
    results = {_model_shift(valid) for _ in range(20)}
    assert len(results) == 1  # deterministic regardless of set iteration order


def test_per_brand_cap_public(db_conn):
    from services.geo_observation_analytics.aggregates import Observation, _weighted_denominator_micros
    # one brand contributes 10 obs, others 1 each
    obs = []
    for i in range(10):
        obs.append(_mk_obs(brand_bucket="dominant", weight=10000))
    for i in range(3):
        obs.append(_mk_obs(brand_bucket=f"small-{i}", weight=10000))
    capped = _weighted_denominator_micros(obs, "public_industry", cap_bps=1000)  # 10%
    raw = _weighted_denominator_micros(obs, "private_brand", cap_bps=1000)
    # private = raw sum (no cap); public capped strictly below raw
    assert raw == 13 * 10000 * 100
    assert capped < raw
    # dominant brand capped to 10% of raw total
    cap_micros = (raw * 1000) // 10000
    assert capped == cap_micros + 3 * (10000 * 100)


def test_private_scope_carries_owner_brand_public_does_not(seeder, db_conn):
    # private
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=501, brand_id=900001)
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY, computed_at=CT)
    prow = _fetch_overall(db_conn, "private_brand")
    assert prow["owner_user_id"] == 501
    assert prow["brand_id"] == 900001

    # public
    seeder.observation(outcome="recommended", processing_state="promoted")
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    pubrow = _fetch_overall(db_conn, "public_industry")
    assert pubrow["owner_user_id"] is None
    assert pubrow["brand_id"] is None


def test_platform_breakdown_rows_produced(seeder, db_conn):
    seeder.observation(outcome="recommended", processing_state="promoted", platform_key="doubao")
    seeder.observation(outcome="not_mentioned", processing_state="promoted", platform_key="qwen")
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT platform_key, valid_observations FROM geo_observation_aggregates "
            "WHERE scope_type='public_industry' AND platform_key IS NOT NULL "
            "ORDER BY platform_key"
        )
        rows = cur.fetchall()
    plats = {r[0]: r[1] for r in rows}
    assert plats == {"doubao": 1, "qwen": 1}


def test_public_aggregation_includes_client_sources(seeder, db_conn):
    """P1#2: public industry aggregation must include paid_diagnosis + monitoring
    events (which carry private owner/brand), not only owner/brand-NULL research.
    Without the fix the public cell drops all client sources."""
    # research (no owner/brand)
    seeder.observation(outcome="recommended", processing_state="promoted", source_type="research_round",
                       user_bucket="ur", brand_bucket="br")
    # paid diagnosis (KEEPS private owner/brand)
    seeder.observation(outcome="recommended", processing_state="promoted", source_type="paid_diagnosis",
                       owner_user_id=501, brand_id=900001, user_bucket="up", brand_bucket="bp")
    # monitoring (KEEPS private owner/brand)
    seeder.observation(outcome="not_mentioned", processing_state="promoted", source_type="recurring_monitoring",
                       owner_user_id=502, brand_id=900002, user_bucket="um", brand_bucket="bm")
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    row = _fetch_overall(db_conn, "public_industry")
    assert row["valid_observations"] == 3               # all three sources counted
    assert row["independent_source_types"] == 3          # research + paid + monitoring
    assert row["owner_user_id"] is None and row["brand_id"] is None  # still anonymized


def test_public_research_needs_no_client_bucket_but_client_sources_do(seeder, db_conn):
    """Cross-package contract: AI-2 intentionally creates no contributor bucket
    for public research, while paid/monitoring observations require one to pass
    the one-vote anti-abuse gate. AI-3 must preserve that distinction."""
    research_id = seeder.observation(
        outcome="recommended", processing_state="pending", source_type="research_round",
        owner_user_id=None, brand_id=None, user_bucket="research-u", brand_bucket="research-b",
    )
    paid_id = seeder.observation(
        outcome="recommended", processing_state="promoted", source_type="paid_diagnosis",
        owner_user_id=501, brand_id=900001, user_bucket="paid-u", brand_bucket="paid-b",
    )
    monitoring_id = seeder.observation(
        outcome="not_mentioned", processing_state="pending", source_type="recurring_monitoring",
        owner_user_id=502, brand_id=900002, user_bucket="monitor-u", brand_bucket="monitor-b",
    )
    with db_conn.cursor() as cur:
        # Reproduce AI-2's real write contract for research, plus a client
        # stability sample that lost the one-vote race.
        cur.execute(
            "DELETE FROM geo_observation_contributor_buckets WHERE event_id IN (%s, %s)",
            (research_id, monitoring_id),
        )
        cur.execute(
            "UPDATE geo_observation_events SET processing_state='promoted' "
            "WHERE id IN (%s,%s)", (research_id, monitoring_id),
        )
    db_conn.commit()

    observations = A.fetch_observations(db_conn, "public_industry", DAY, DAY)
    ids = {item.event_id for item in observations}

    assert research_id in ids       # legitimate public research survives
    assert paid_id in ids           # attributed customer vote survives
    assert monitoring_id not in ids  # bucketless customer sample stays excluded


def test_per_brand_cap_moves_public_presence_rate(seeder, db_conn):
    """P1#3/P1#4: the per-brand cap (water-filled to <=10% of the FINAL weighted
    denominator) must move the public displayed rate. A dominant brand's 10
    'recommended' votes are capped so weighted presence collapses from the raw
    10/19 (5263 bps) toward the 10% ceiling — the rich brand cannot dominate."""
    # dominant brand 'rich' with 10 distinct votes (distinct families => distinct
    # contributor buckets), all recommended/present
    for i in range(10):
        seeder.observation(outcome="recommended", processing_state="promoted",
                           user_bucket=f"ur{i}", brand_bucket="rich",
                           prompt_family_key=f"fam-{i}", effective_weight_bps=7000)
    # 9 other single-vote brands, not mentioned -> 10 brands total (cap feasible)
    for i in range(9):
        seeder.observation(outcome="not_mentioned", processing_state="promoted",
                           user_bucket=f"us{i}", brand_bucket=f"s{i}",
                           prompt_family_key=f"famS-{i}", effective_weight_bps=7000)
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    row = _fetch_overall(db_conn, "public_industry")
    assert row["valid_observations"] == 19
    # raw count presence would be 10/19 = 5263 bps; the cap collapses it to ~10%.
    assert row["presence_rate_bps"] <= 1200          # capped near the 10% ceiling
    assert row["presence_rate_bps"] < 5263           # far below the raw count rate


def test_zero_valid_cell_produces_no_row(seeder, db_conn):
    """P1#3: a cell with ONLY entity_ambiguous/engine_error has zero valid
    observations -> no fabricated 0% aggregate row is written."""
    seeder.observation(outcome="entity_ambiguous", processing_state="promoted")
    seeder.observation(outcome="engine_error", processing_state="promoted")
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    assert _fetch_overall(db_conn, "public_industry") is None
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM geo_observation_aggregates")
        assert cur.fetchone()[0] == 0


def test_reads_current_lineage_not_stale_version(seeder, db_conn):
    """P1#2: same date/cell written under an OLD metric_version + the current one;
    the read must return the CURRENT lineage, and trend must not double-count."""
    from services.geo_observation_analytics import contract as CC
    from services.geo_observation_analytics import repository as repo
    seeder.observation(outcome="recommended", processing_state="promoted",
                       owner_user_id=501, brand_id=900001)
    # OLD-version row (fabricated caliber), then the CURRENT-version row, same bucket
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY,
                    config=AggregationConfig(metric_version="geo-observation-metrics-OLD"),
                    computed_at=datetime(2026, 7, 11, tzinfo=timezone.utc))
    A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=DAY,
                    config=AggregationConfig(), computed_at=datetime(2026, 7, 12, tzinfo=timezone.utc))
    db_conn.commit()
    row = repo.latest_overall_aggregate(db_conn, scope_type="private_brand", granularity="day",
                                        owner_user_id=501, brand_id=900001,
                                        policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH)
    assert row["metric_version"] == CC.METRIC_VERSION  # current lineage, not OLD
    series = repo.trend_series(db_conn, scope_type="private_brand", granularity="day",
                               owner_user_id=501, brand_id=900001,
                               policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH)
    assert len(series) == 1  # not two rows for the same date
    assert series[0]["metric_version"] == CC.METRIC_VERSION


def test_industry_list_and_latest_timestamp_are_current_lineage_only(seeder, db_conn):
    from services.geo_observation_analytics import repository as repo

    old_event_id = seeder.observation(
        outcome="recommended", processing_state="promoted",
        industry_key="old-metric-industry",
    )
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        config=AggregationConfig(metric_version="geo-observation-metrics-OLD"),
        computed_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE geo_observation_events SET processing_state='withdrawn',withdrawn_at=NOW() "
            "WHERE id=%s", (old_event_id,)
        )
    db_conn.commit()
    seeder.observation(
        outcome="recommended", processing_state="promoted",
        industry_key="current-metric-industry",
    )
    current_computed_at = datetime(2026, 7, 12, tzinfo=timezone.utc)
    A.refresh_scope(
        db_conn, scope_type="public_industry", granularity="day", day=DAY,
        computed_at=current_computed_at,
    )
    db_conn.commit()

    assert repo.list_public_industries(
        db_conn, granularity="day",
        policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH,
    ) == ["current-metric-industry"]
    assert repo.latest_aggregate_computed_at(
        db_conn, policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH,
    ) == current_computed_at.isoformat()


def test_cap_water_fill_actually_converges_to_cap():
    """R4-P1: with several brands simultaneously over the cap, the water-filling
    must CONVERGE so no brand exceeds the cap of the final denominator (a fixed
    small iteration budget left dominant brands ~2x the cap)."""
    from services.geo_observation_analytics.aggregates import _cap_brand_weights
    braw = {**{f"big{i}": 50_000_000 for i in range(5)},
            **{f"tiny{i}": 1000 for i in range(6)}}  # 11 brands, 10% cap feasible
    capped = _cap_brand_weights(braw, 1000)
    total = sum(capped.values())
    assert total > 0
    max_share_bps = max(capped.values()) * 10000 // total
    assert max_share_bps <= 1001  # no brand exceeds ~10% (integer tolerance)


def test_model_shift_rows_dedup_and_version_scoped(seeder, db_conn):
    """R4-P2: /model-shifts must not surface duplicate policy-version rows for the
    same logical cell, nor off-lineage stale rows mislabeled as current."""
    from services.geo_observation_analytics import repository as repo
    # a cell with a real model shift (r1 recommended -> r2 not_mentioned)
    for i in range(3):
        seeder.observation(outcome="recommended", processing_state="promoted",
                           model_revision="r1", user_bucket=f"ua{i}", brand_bucket=f"ba{i}",
                           observed_at=datetime(2026, 7, 10, 8, tzinfo=timezone.utc))
    for i in range(3):
        seeder.observation(outcome="not_mentioned", processing_state="promoted",
                           model_revision="r2", user_bucket=f"ub{i}", brand_bucket=f"bb{i}",
                           observed_at=datetime(2026, 7, 10, 12, tzinfo=timezone.utc))
    # same bucket refreshed under two policy versions (distinct aggregate_key)
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY,
                    config=AggregationConfig(policy_version="p1"),
                    computed_at=datetime(2026, 7, 11, tzinfo=timezone.utc))
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY,
                    config=AggregationConfig(policy_version="p2"),
                    computed_at=datetime(2026, 7, 12, tzinfo=timezone.utc))
    # plus an OFF-lineage (old metric_version) row that must be excluded
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY,
                    config=AggregationConfig(metric_version="geo-observation-metrics-OLD"),
                    computed_at=datetime(2026, 7, 13, tzinfo=timezone.utc))
    db_conn.commit()
    rows = repo.model_shift_rows(db_conn, threshold_bps=2000,
                                 policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH)
    assert rows  # there is a shift
    keys = [(r["industry_key"], r["platform_key"], r["model_revision"], str(r["bucket_start"]))
            for r in rows]
    assert len(keys) == len(set(keys))  # no duplicate logical cell (policy dedup)


def test_model_shift_rows_threshold_after_dedup(seeder, db_conn):
    """R7-P2: the threshold is applied AFTER the freshest-per-cell dedup, so a
    RESOLVED cell (freshest shift below threshold) is not surfaced by a stale
    prior-policy_version row that still happens to exceed the threshold."""
    from services.geo_observation_analytics import repository as repo
    IND = "resolve-ind"
    for i in range(3):
        seeder.observation(outcome="recommended", processing_state="promoted", industry_key=IND,
                           model_revision="r1", user_bucket=f"ra{i}", brand_bucket=f"rba{i}",
                           observed_at=datetime(2026, 7, 10, 8, tzinfo=timezone.utc))
    r2_ids = [seeder.observation(outcome="not_mentioned", processing_state="promoted", industry_key=IND,
                                 model_revision="r2", user_bucket=f"rb{i}", brand_bucket=f"rbb{i}",
                                 observed_at=datetime(2026, 7, 10, 12, tzinfo=timezone.utc))
              for i in range(3)]
    # policy-A: strong r1->r2 shift
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY,
                    config=AggregationConfig(policy_version="policy-A"),
                    computed_at=datetime(2026, 7, 11, tzinfo=timezone.utc))
    # the r2 cohort is withdrawn -> the shift resolves
    with db_conn.cursor() as cur:
        cur.execute("UPDATE geo_observation_events SET processing_state='withdrawn' WHERE id = ANY(%s)", (r2_ids,))
    db_conn.commit()
    # policy-B: fresher recompute, single revision now -> shift 0 (below threshold)
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY,
                    config=AggregationConfig(policy_version="policy-B"),
                    computed_at=datetime(2026, 7, 12, tzinfo=timezone.utc))
    db_conn.commit()
    rows = repo.model_shift_rows(db_conn, threshold_bps=2000,
                                 policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH)
    # freshest caliber for the cell is 0 bps (< 2000) -> the resolved cell must
    # NOT appear via the stale policy-A row.
    assert [r for r in rows if r["industry_key"] == IND] == []


def test_model_shift_rows_keeps_granularities_distinct(seeder, db_conn):
    """R6-P2: a day and a month bucket that share a bucket_start (the 1st of the
    month) are distinct logical cells; neither may be silently dropped."""
    from services.geo_observation_analytics import repository as repo
    d1 = date(2026, 7, 1)  # 1st of month -> day & month buckets share bucket_start
    for i in range(3):
        seeder.observation(outcome="recommended", processing_state="promoted", model_revision="r1",
                           user_bucket=f"ga{i}", brand_bucket=f"gba{i}",
                           observed_at=datetime(2026, 7, 1, 8, tzinfo=timezone.utc))
    for i in range(3):
        seeder.observation(outcome="not_mentioned", processing_state="promoted", model_revision="r2",
                           user_bucket=f"gb{i}", brand_bucket=f"gbb{i}",
                           observed_at=datetime(2026, 7, 1, 12, tzinfo=timezone.utc))
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=d1, computed_at=CT)
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="month", day=d1,
                    computed_at=datetime(2026, 7, 9, tzinfo=timezone.utc))
    db_conn.commit()
    rows = repo.model_shift_rows(db_conn, threshold_bps=2000,
                                 policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH)
    grans = {r["bucket_granularity"] for r in rows if str(r["bucket_start"]) == "2026-07-01"}
    assert grans == {"day", "month"}  # both granularities distinct, neither dropped


def test_model_shift_rows_excludes_subsegment_breakdowns(seeder, db_conn):
    """R5-P2: a sub-segment breakdown (is_branded_prompt=TRUE, platform_key NULL)
    with a strong shift must NOT leak into /model-shifts labeled as the industry-
    wide (all-platforms) shift. Here the overall shift is diluted below threshold
    while the branded sub-segment swings fully; nothing should surface."""
    from services.geo_observation_analytics import repository as repo
    IND = "dilution-ind"
    early = datetime(2026, 7, 10, 8, tzinfo=timezone.utc)
    late = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
    # branded sub-segment fully swings r1(recommended) -> r2(not_mentioned)
    for i in range(3):
        seeder.observation(outcome="recommended", processing_state="promoted", industry_key=IND,
                           is_branded_prompt=True, model_revision="r1",
                           user_bucket=f"ba{i}", brand_bucket=f"bba{i}", observed_at=early)
    for i in range(3):
        seeder.observation(outcome="not_mentioned", processing_state="promoted", industry_key=IND,
                           is_branded_prompt=True, model_revision="r2",
                           user_bucket=f"bb{i}", brand_bucket=f"bbb{i}", observed_at=late)
    # large stable non-branded mass dilutes the OVERALL shift below the threshold
    for i in range(20):
        seeder.observation(outcome="recommended", processing_state="promoted", industry_key=IND,
                           is_branded_prompt=False, model_revision="r1",
                           user_bucket=f"na{i}", brand_bucket=f"nba{i}", observed_at=early)
    for i in range(20):
        seeder.observation(outcome="recommended", processing_state="promoted", industry_key=IND,
                           is_branded_prompt=False, model_revision="r2",
                           user_bucket=f"nb{i}", brand_bucket=f"nbb{i}", observed_at=late)
    A.refresh_scope(db_conn, scope_type="public_industry", granularity="day", day=DAY, computed_at=CT)
    db_conn.commit()
    rows = repo.model_shift_rows(db_conn, threshold_bps=2000,
                                 policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH)
    # the diluted overall (~1304 bps) is below threshold; the strong branded
    # sub-segment must NOT surface as an industry (platform_key None) row.
    leaked = [r for r in rows if r["industry_key"] == IND]
    assert leaked == []


def test_trend_series_returns_newest_not_oldest(seeder, db_conn):
    """trend_series must return the NEWEST `limit` buckets (ascending), so the
    view never freezes on the oldest history."""
    from services.geo_observation_analytics import repository as repo
    days = [date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10)]
    for d in days:
        seeder.observation(outcome="recommended", processing_state="promoted",
                           owner_user_id=501, brand_id=900001,
                           observed_at=datetime(d.year, d.month, d.day, 8, tzinfo=timezone.utc))
        A.refresh_scope(db_conn, scope_type="private_brand", granularity="day", day=d, computed_at=CT)
    db_conn.commit()
    series = repo.trend_series(db_conn, scope_type="private_brand", granularity="day",
                               owner_user_id=501, brand_id=900001, limit=2,
                               policy_basis_hash=A.DEFAULT_POLICY_BASIS_HASH)
    starts = [str(r["bucket_start"]) for r in series]
    assert starts == ["2026-07-09", "2026-07-10"]  # newest 2, ascending (not the oldest 2)


_EID = [0]


def _mk_obs(brand_bucket, weight):
    from services.geo_observation_analytics.aggregates import Observation
    _EID[0] += 1
    return Observation(
        event_id=_EID[0], owner_user_id=None, brand_id=None,
        source_type="recurring_monitoring", industry_key="x", prompt_family_key=None,
        prompt_intent=None, is_branded_prompt=None, platform_key="doubao",
        surface_key="doubao_ark_api_search", model_revision="r1", search_enabled=True,
        outcome="recommended", position=None, competitor_count=0, citation_count=0,
        source_count=0, quality_score_bps=2000,
        effective_weight_bps=weight, observed_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        run_index=1, user_bucket="u", brand_bucket=brand_bucket,
    )
