"""Real-PostgreSQL contract tests for reconciled/native collection readiness."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from copy import deepcopy

import pytest

from db.connection import get_connection
from services.geo_observation import integration, policy, reconciler
from services.geo_observation import source_adapters
from services.geo_observation.collection_readiness import (
    AGGREGATION_JOB,
    MONITORING_OWNER_JOB,
    RECONCILER_JOB,
    native_scheduler_probe,
)
from services.ai_surface_monitoring.scheduler_wiring import (
    JOB_MONITORING_DAILY,
    JOB_RECONCILER as NATIVE_RECONCILER_JOB,
    JOB_RESEARCH_TICK,
)
from services.geo_observation.aggregate_basis import canonical_policy_basis
from services.geo_observation_analytics import aggregates as observation_aggregates

import _helpers as H


BRIDGE_JOBS = [RECONCILER_JOB, AGGREGATION_JOB, MONITORING_OWNER_JOB]
LONG_ANSWER = (
    "大昀装修在深圳拥有公开案例与可核验的施工记录，项目流程、验收节点和售后责任均有明确说明，"
    "本次回答基于真实终态结果，仅用于观测采集接线测试。"
)


def _refresh_complete_current_basis(conn) -> str:
    """Create the six real current-bucket terminals used by product activation."""
    cur = conn.cursor()
    _promote_registered_events(cur)
    conn.commit()
    snap = policy.get_policy(cur)
    basis = canonical_policy_basis(snap["policy"])
    config = observation_aggregates.AggregationConfig(
        policy_version=str(snap["policy_version"]),
        policy_basis_hash=basis,
    )
    today = datetime.now(timezone.utc).date()
    for scope_type in ("private_brand", "public_industry"):
        for granularity in ("day", "week", "month"):
            observation_aggregates.refresh_scope(
                conn,
                scope_type=scope_type,
                granularity=granularity,
                day=today,
                config=config,
            )
    return basis


def _promote_registered_events(cur) -> None:
    """Test-only deterministic promotion terminal; no provider/LLM call."""
    cur.execute(
        """
        INSERT INTO geo_observation_signals(
            event_id,industry_key,prompt_family_key,prompt_intent,is_branded_prompt,
            platform_key,provider_key,model_key,model_revision,surface_key,
            response_status,target_outcome,sentiment,competitor_count,source_domains,
            citation_count,source_count,search_query_theme_keys,quality_score_bps,
            base_weight_bps,effective_weight_bps,confidence_bps,observed_at
        )
        SELECT e.id,COALESCE(e.industry_key,'装修'),'test-family','category_recommendation',FALSE,
               e.platform_key,e.provider_key,e.model_key,e.model_revision,e.surface_key,
               'answered','recommended','neutral',0,'[]'::jsonb,0,0,'[]'::jsonb,
               9000,10000,10000,9000,e.observed_at
          FROM geo_observation_events e
         WHERE NOT EXISTS (
             SELECT 1 FROM geo_observation_signals s WHERE s.event_id=e.id
         )
        """
    )
    cur.execute(
        "UPDATE geo_observation_events SET processing_state='promoted' "
        "WHERE processing_state <> 'withdrawn'"
    )


def _seed_healthy_bridge(monkeypatch) -> dict:
    conn = get_connection()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(
        conn,
        "ready_diag",
        owner=124,
        brand=501,
        run_status="committed",
        visibility="published",
        answer=LONG_ANSWER,
    )
    H.seed_monitoring(
        conn,
        None,
        brand=501,
        owner=124,
        answer=LONG_ANSWER,
        quote_id="ready_quote",
    )
    H.seed_monitoring(
        conn,
        None,
        brand=501,
        owner=124,
        platform="yuanbao",
        answer=LONG_ANSWER,
        quote_id="ready_quote_yuanbao",
    )
    H.seed_research(conn, None, answer=LONG_ANSWER, round_id="ready_round")
    cur = conn.cursor()
    counts = reconciler.reconcile_registration(cur)
    assert all(counts[source] >= 1 for source in counts)
    cur.execute(
        "INSERT INTO sched_job_runs(job_name,scheduled_at,status,finished_at) "
        "VALUES (%s,NOW(),'done',NOW())",
        (RECONCILER_JOB,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(integration, "_runtime_scheduler_job_ids", lambda: list(BRIDGE_JOBS))
    return integration.collection_readiness_provider()


def test_bridge_ready_without_sampling_driver_or_provider_call(monkeypatch):
    monkeypatch.setattr(
        integration,
        "collection_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("bridge must not build native runtime")),
    )
    result = _seed_healthy_bridge(monkeypatch)
    assert result == {
        "mode": "existing_collectors_reconciled",
        "ready": True,
        "status": "ready",
        "problems": [],
        "reconciler_last_success_at": result["reconciler_last_success_at"],
        "reconciler_backlog": 0,
        "source_watermarks": {
            "paid_diagnosis": result["source_watermarks"]["paid_diagnosis"],
            "recurring_monitoring": result["source_watermarks"]["recurring_monitoring"],
            "research_round": result["source_watermarks"]["research_round"],
        },
        "duplicate_collection_jobs": [],
        "policy_version": 1,
    }
    assert result["reconciler_last_success_at"]
    assert all(result["source_watermarks"].values())


def test_bridge_platform_health_is_passive_terminal_truth(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    monkeypatch.setattr(
        integration,
        "collection_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("provider probe forbidden in bridge")),
    )
    monkeypatch.setattr(integration, "_health_cache_until", 0.0)
    monkeypatch.setattr(integration, "_health_cache_key", None)
    health = integration.platform_health_provider()
    deepseek = next(row for row in health if row["platform_key"] == "deepseek")
    assert deepseek["status"] == "healthy"
    assert deepseek["provider_key"] == "existing_collector"


def test_bridge_missing_required_platform_smoke_is_unavailable(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    conn.cursor().execute(
        "DELETE FROM geo_observation_events WHERE platform_key='yuanbao'"
    )
    conn.commit()
    conn.close()
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert any("yuanbao 生产等价 terminal smoke" in item for item in result["problems"])


def test_bridge_without_reconciler_job_is_unavailable(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    monkeypatch.setattr(
        integration,
        "_runtime_scheduler_job_ids",
        lambda: [AGGREGATION_JOB, MONITORING_OWNER_JOB],
    )
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert any(RECONCILER_JOB in item for item in result["problems"])


def test_web_worker_uses_fresh_cron_leader_inventory(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    integration.record_scheduler_inventory(BRIDGE_JOBS)
    monkeypatch.setattr(integration, "_runtime_scheduler_job_ids", lambda: None)
    result = integration.collection_readiness_provider()
    assert result["status"] == "ready"
    assert result["duplicate_collection_jobs"] == []


@pytest.mark.parametrize("role", ["", "web", "prestart", "backup", "typo"])
def test_only_explicit_cron_role_may_use_local_scheduler_inventory(monkeypatch, role):
    monkeypatch.setenv("ROLE", role)
    assert integration._runtime_scheduler_job_ids() is None


def test_web_worker_without_cron_inventory_is_unavailable(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    monkeypatch.setattr(integration, "_runtime_scheduler_job_ids", lambda: None)
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert any("scheduler inventory" in item for item in result["problems"])


def test_web_worker_rejects_stale_cron_inventory(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    integration.record_scheduler_inventory(BRIDGE_JOBS)
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE system_settings SET value=(value::jsonb || "
        "jsonb_build_object('recorded_at',(NOW()-INTERVAL '16 minutes')::text))::text "
        "WHERE key='geo_observation_scheduler_inventory'"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(integration, "_runtime_scheduler_job_ids", lambda: None)
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert any("scheduler inventory 已超时" in item for item in result["problems"])


def test_bridge_stale_reconciler_is_unavailable(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE sched_job_runs SET finished_at=NOW()-INTERVAL '16 minutes' "
        "WHERE job_name=%s",
        (RECONCILER_JOB,),
    )
    conn.commit()
    conn.close()
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert any("超时" in item for item in result["problems"])


def test_bridge_missing_terminal_watermark_is_unavailable(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    conn.cursor().execute("UPDATE geo_research_round SET status='failed'")
    conn.commit()
    conn.close()
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert result["source_watermarks"]["research_round"] is None
    assert any("research_round terminal source watermark" in item for item in result["problems"])


def test_bridge_backlog_over_threshold_cannot_be_ready(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    conn.cursor().execute(
        "INSERT INTO diagnosis_runs(run_token,session_id,owner_user_id,brand_id,billing_mode,"
        "run_status,status_changed_at,finished_at) "
        "SELECT 'backlog_'||n,'backlog_session_'||n,124,501,'paid','committed',NOW(),NOW() "
        "FROM generate_series(1,501) AS n"
    )
    conn.commit()
    conn.close()
    result = integration.collection_readiness_provider()
    assert result["status"] == "attention_required"
    assert result["ready"] is False
    assert result["reconciler_backlog"] > 500
    assert any("积压" in item for item in result["problems"])


def test_bridge_stuck_event_cannot_be_ready(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE geo_observation_events SET processing_state='processing', "
        "lease_token='00000000-0000-0000-0000-000000000001',"
        "lease_until=NOW()-INTERVAL '1 minute' "
        "WHERE id=(SELECT MIN(id) FROM geo_observation_events)"
    )
    conn.commit()
    conn.close()
    result = integration.collection_readiness_provider()
    assert result["status"] == "attention_required"
    assert any("stuck" in item for item in result["problems"])


def test_bridge_rejected_events_over_threshold_cannot_be_ready(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO diagnosis_runs(run_token,session_id,owner_user_id,brand_id,billing_mode,"
        "run_status,status_changed_at,finished_at) "
        "SELECT 'rejected_'||n,'rejected_session_'||n,124,501,'paid','committed',NOW(),NOW() "
        "FROM generate_series(1,101) AS n"
    )
    cur.execute(
        "INSERT INTO diagnosis_records(session_id,brand_id,run_token,result_visibility,"
        "total_score,raw_data_json) "
        "SELECT dr.session_id,dr.brand_id,dr.run_token,'published',72,seed.raw_data_json "
        "FROM diagnosis_runs dr CROSS JOIN LATERAL "
        "(SELECT raw_data_json FROM diagnosis_records WHERE run_token='ready_diag') seed "
        "WHERE dr.run_token LIKE 'rejected_%'"
    )
    reconciler.reconcile_registration(cur)
    cur.execute(
        "UPDATE geo_observation_events SET processing_state='rejected',updated_at=NOW() "
        "WHERE source_type='paid_diagnosis' AND source_record_id LIKE 'rejected_%'"
    )
    conn.commit()
    conn.close()
    result = integration.collection_readiness_provider()
    assert result["status"] == "attention_required"
    assert any("rejected" in item for item in result["problems"])


def test_bridge_missing_registered_adapter_is_unavailable(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    monkeypatch.delitem(source_adapters.SOURCE_ADAPTERS, "research_round")
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert any("research_round source adapter" in item for item in result["problems"])


def test_bridge_rejects_second_collection_job(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    monkeypatch.setattr(
        integration,
        "_runtime_scheduler_job_ids",
        lambda: BRIDGE_JOBS + ["ai_surface_obs_monitoring_daily"],
    )
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert result["duplicate_collection_jobs"] == ["ai_surface_obs_monitoring_daily"]
    assert any("重复供应商调用/扣费" in item for item in result["problems"])


def test_native_owner_coexistence_is_always_unavailable_and_calls_no_provider(monkeypatch):
    jobs = [
        JOB_MONITORING_DAILY,
        JOB_RESEARCH_TICK,
        NATIVE_RECONCILER_JOB,
        AGGREGATION_JOB,
        MONITORING_OWNER_JOB,
        "research_monitor_bimonthly_round",
        "research_monitor_missed_recovery",
    ]
    probe = native_scheduler_probe(jobs)
    assert probe["problems"]
    assert MONITORING_OWNER_JOB in probe["duplicate_collection_jobs"]
    assert "research_monitor_bimonthly_round" in probe["duplicate_collection_jobs"]

    conn = get_connection()
    conn.cursor().execute(
        "UPDATE geo_observation_policy SET collection_mode='native_sampling_driver' "
        "WHERE singleton_id=1"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(integration, "_runtime_scheduler_job_ids", lambda: jobs)
    monkeypatch.setattr(
        integration,
        "collection_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("native provider/runtime call forbidden")),
    )
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert result["ready"] is False
    assert any("takeover" in item for item in result["problems"])
    integration._health_cache_until = 0.0
    integration._health_cache_key = None
    assert integration.platform_health_provider() == []


def test_startup_readiness_rejects_weakened_aggregate_basis_schema():
    from services.geo_observation import readiness

    conn = get_connection()
    cur = conn.cursor()
    readiness.verify_geo_observation_schema(cur)
    cur.execute(
        "ALTER TABLE geo_observation_aggregate_refresh_manifest "
        "DROP CONSTRAINT chk_geo_obs_agg_manifest_scope"
    )
    cur.execute(
        "ALTER TABLE geo_observation_aggregate_refresh_manifest "
        "ADD CONSTRAINT chk_geo_obs_agg_manifest_scope CHECK (TRUE)"
    )
    with pytest.raises(readiness.ObservationSchemaNotReady):
        readiness.verify_geo_observation_schema(cur)
    conn.rollback()
    conn.close()


def test_startup_readiness_rejects_manifest_default_drift():
    from services.geo_observation import readiness

    conn = get_connection()
    cur = conn.cursor()
    readiness.verify_geo_observation_schema(cur)
    cur.execute(
        "ALTER TABLE geo_observation_aggregate_refresh_manifest "
        "ALTER COLUMN updated_at DROP DEFAULT"
    )
    with pytest.raises(readiness.ObservationSchemaNotReady):
        readiness.verify_geo_observation_schema(cur)
    conn.rollback()
    conn.close()


def test_startup_readiness_rejects_receipt_index_definition_drift():
    from services.geo_observation import readiness

    conn = get_connection()
    cur = conn.cursor()
    readiness.verify_geo_observation_schema(cur)
    cur.execute("DROP INDEX public.idx_geo_obs_bucket_revision_receipt_gin")
    cur.execute(
        "CREATE INDEX idx_geo_obs_bucket_revision_receipt_gin "
        "ON public.geo_observation_aggregate_bucket_revision(bucket_start)"
    )
    with pytest.raises(readiness.ObservationSchemaNotReady):
        readiness.verify_geo_observation_schema(cur)
    conn.rollback()
    conn.close()

def test_startup_readiness_rejects_disabled_or_rewritten_eligibility_trigger():
    from services.geo_observation import readiness

    conn = get_connection()
    cur = conn.cursor()
    readiness.verify_geo_observation_schema(cur)
    cur.execute(
        "ALTER TABLE geo_observation_events "
        "DISABLE TRIGGER trg_geo_obs_event_eligibility_epoch"
    )
    with pytest.raises(readiness.ObservationSchemaNotReady):
        readiness.verify_geo_observation_schema(cur)
    conn.rollback()

    cur.execute(
        """CREATE OR REPLACE FUNCTION geo_obs_bump_eligibility_epoch_on_event()
             RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"""
    )
    with pytest.raises(readiness.ObservationSchemaNotReady):
        readiness.verify_geo_observation_schema(cur)
    conn.rollback()
    conn.close()


def test_startup_readiness_pins_promotion_and_input_immutability_triggers():
    from services.geo_observation import readiness

    conn = get_connection()
    cur = conn.cursor()
    readiness.verify_geo_observation_schema(cur)
    cur.execute(
        "ALTER TABLE geo_observation_signals "
        "DISABLE TRIGGER trg_geo_obs_signal_promoted_immutable"
    )
    with pytest.raises(readiness.ObservationSchemaNotReady):
        readiness.verify_geo_observation_schema(cur)
    conn.rollback()
    cur.execute(
        """CREATE OR REPLACE FUNCTION geo_obs_assign_promotion_seq()
             RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"""
    )
    with pytest.raises(readiness.ObservationSchemaNotReady):
        readiness.verify_geo_observation_schema(cur)
    conn.rollback()
    conn.close()


def test_bridge_tenant_mapping_drift_is_unavailable(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE geo_observation_events SET owner_user_id=999 "
        "WHERE source_type='paid_diagnosis'"
    )
    conn.commit()
    conn.close()
    result = integration.collection_readiness_provider()
    assert result["status"] == "unavailable"
    assert any("租户映射异常" in item for item in result["problems"])


def test_readiness_timestamp_is_current_terminal_fact(monkeypatch):
    result = _seed_healthy_bridge(monkeypatch)
    parsed = datetime.fromisoformat(result["reconciler_last_success_at"])
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    assert (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() < 60


def test_product_flag_cannot_skip_signed_activation_sequence(monkeypatch):
    ready = _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    current = policy.get_policy(conn.cursor())
    attempted = deepcopy(current["policy"])
    attempted["feature_flags"] = {
        "ingest_enabled": True,
        "promotion_enabled": True,
        "aggregation_enabled": True,
        "product_enabled": True,
    }
    with pytest.raises(policy.ProductActivationBlocked) as exc:
        policy.update_policy(
            conn.cursor(),
            expected_version=current["policy_version"],
            new_policy=attempted,
            reason="禁止跳闸",
            request_id="skip-product-gates",
            operator_id="admin",
            collection_readiness=ready,
        )
    conn.rollback()
    conn.close()
    assert "promotion_legal_basis 尚未批准" in exc.value.problems
    assert "金标准门尚未通过" in exc.value.problems
    assert any("refresh manifest" in item for item in exc.value.problems)


def test_product_flag_opens_only_after_prior_committed_readiness(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    cur = conn.cursor()

    snap = policy.get_policy(cur)
    p = deepcopy(snap["policy"])
    p["feature_flags"]["ingest_enabled"] = True
    version = policy.update_policy(
        cur,
        expected_version=snap["policy_version"],
        new_policy=p,
        reason="先开采集",
        request_id="activate-ingest",
        operator_id="admin",
    )
    conn.commit()

    version = policy.update_promotion_governance(
        cur,
        expected_version=version,
        promotion_legal_basis="legal_v1_approved",
        consent_policy_version="consent_v1",
        reason="批准治理",
        request_id="activate-governance",
        operator_id="admin",
    )
    conn.commit()
    gold = policy.record_gold_evaluation(
        cur,
        expected_version=version,
        dataset_version="gold-20260720",
        sample_count=100,
        macro_f1_bps=9000,
        high_risk_false_reco=0,
        report_hash="readiness-gold-hash-20260720",
        reason="金标准达标",
        request_id="activate-gold",
        operator_id="admin",
    )
    version = gold["policy_version"]
    conn.commit()

    p = deepcopy(policy.get_policy(cur)["policy"])
    p["feature_flags"]["promotion_enabled"] = True
    version = policy.update_policy(
        cur,
        expected_version=version,
        new_policy=p,
        reason="开启晋升",
        request_id="activate-promotion",
        operator_id="admin",
    )
    conn.commit()
    p = deepcopy(policy.get_policy(cur)["policy"])
    p["feature_flags"]["aggregation_enabled"] = True
    version = policy.update_policy(
        cur,
        expected_version=version,
        new_policy=p,
        reason="开启聚合",
        request_id="activate-aggregation",
        operator_id="admin",
    )
    conn.commit()

    readiness = integration.collection_readiness_provider()
    assert readiness["status"] == "ready"
    assert readiness["policy_version"] == version
    p = deepcopy(policy.get_policy(cur)["policy"])
    p["feature_flags"]["product_enabled"] = True
    with pytest.raises(policy.ProductActivationBlocked) as stale_exc:
        policy.update_policy(
            cur,
            expected_version=version,
            new_policy=p,
            reason="旧聚合不得开产品",
            request_id="reject-stale-aggregate",
            operator_id="admin",
            collection_readiness=readiness,
        )
    conn.rollback()
    assert any("refresh manifest" in item for item in stale_exc.value.problems)

    basis = _refresh_complete_current_basis(conn)
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"
    changed = deepcopy(p)
    changed["retention_days"] = int(changed["retention_days"]) + 1
    with pytest.raises(policy.ProductActivationBlocked) as mixed_exc:
        policy.update_policy(
            cur,
            expected_version=version,
            new_policy=changed,
            reason="禁止边改口径边开产品",
            request_id="reject-mixed-policy-activation",
            operator_id="admin",
            collection_readiness=readiness,
        )
    conn.rollback()
    assert any("不得同时修改其他 policy" in item for item in mixed_exc.value.problems)

    # A positive append must not interrupt the already activated snapshot, but
    # it still invalidates the stronger first-activation proof until refresh.
    new_result_id = H.seed_monitoring(
        conn, None, brand=501, owner=124, quote_id="post-manifest-input",
        answer=LONG_ANSWER,
    )
    reconciler.reconcile_registration(cur)
    _promote_registered_events(cur)
    conn.commit()
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"
    with pytest.raises(policy.ProductActivationBlocked) as stale_watermark_exc:
        policy.update_policy(
            cur,
            expected_version=version,
            new_policy=p,
            reason="新输入未聚合不得开产品",
            request_id="reject-stale-watermark",
            operator_id="admin",
            collection_readiness=readiness,
        )
    conn.rollback()
    assert any(
        "watermark 落后" in item or "输入计数落后" in item
        for item in stale_watermark_exc.value.problems
    ), stale_watermark_exc.value.problems

    refreshed_basis = _refresh_complete_current_basis(conn)
    assert refreshed_basis == basis
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"

    final_version = policy.update_policy(
        cur,
        expected_version=version,
        new_policy=p,
        reason="最终开产品",
        request_id="activate-product",
        operator_id="admin",
        collection_readiness=readiness,
    )
    conn.commit()
    assert final_version == version + 1
    activated = policy.get_policy(cur)
    assert activated["effective_flags"]["product_enabled"] is True
    assert activated["aggregate_policy_basis"] == basis
    assert activated["candidate_aggregate_policy_basis"] == basis
    conn.close()


def test_runtime_snapshot_allows_append_but_withdrawal_epoch_blocks_until_refresh(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    basis = _refresh_complete_current_basis(conn)
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"

    # Ordinary append waits behind the immutable manifest watermark.
    H.seed_monitoring(
        conn, None, brand=501, owner=124, quote_id="runtime-append",
        answer=LONG_ANSWER,
    )
    reconciler.reconcile_registration(conn.cursor())
    _promote_registered_events(conn.cursor())
    conn.commit()
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"

    # A negative eligibility change bumps the durable epoch immediately.
    conn.cursor().execute(
        "UPDATE geo_observation_events SET processing_state='withdrawn',withdrawn_at=NOW() "
        "WHERE id=(SELECT MIN(id) FROM geo_observation_events "
        "WHERE processing_state='promoted')"
    )
    conn.commit()
    blocked = integration.aggregate_readiness_provider(basis)
    assert blocked["status"] == "unavailable"
    assert any(
        "eligibility epoch" in item or "manifest 缺失" in item
        for item in blocked["problems"]
    )

    assert _refresh_complete_current_basis(conn) == basis
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"
    conn.close()


def test_retention_expiry_withdraws_atomically_and_invalidates_snapshot(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    basis = _refresh_complete_current_basis(conn)
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"
    cur = conn.cursor()
    cur.execute(
        "UPDATE geo_observation_events SET retention_until=NOW()-interval '1 day' "
        "WHERE id=(SELECT MIN(id) FROM geo_observation_events "
        "WHERE processing_state='promoted') RETURNING id"
    )
    event_id = int(cur.fetchone()["id"])
    conn.commit()
    overdue = integration.aggregate_readiness_provider(basis)
    assert overdue["status"] == "ready"
    assert overdue["problems"] == []
    result = reconciler.reconcile_retention(cur)
    assert result["anonymized"] == 1
    conn.commit()
    cur.execute(
        "SELECT processing_state,owner_user_id,brand_id,answer_hash,prompt_fingerprint "
        "FROM geo_observation_events WHERE id=%s", (event_id,),
    )
    event = cur.fetchone()
    assert event["processing_state"] == "withdrawn"
    assert all(event[key] is None for key in (
        "owner_user_id", "brand_id", "answer_hash", "prompt_fingerprint"
    ))
    cur.execute(
        "SELECT COUNT(*) AS n FROM geo_observation_contributor_buckets WHERE event_id=%s",
        (event_id,),
    )
    assert int(cur.fetchone()["n"]) == 0
    cur.execute(
        "SELECT COUNT(*) AS n FROM geo_observation_audit "
        "WHERE event_id=%s AND action='retention_anonymize'", (event_id,),
    )
    assert int(cur.fetchone()["n"]) == 1
    blocked = integration.aggregate_readiness_provider(basis)
    assert blocked["status"] == "unavailable"
    assert any(
        "eligibility epoch" in item or "manifest 缺失" in item
        for item in blocked["problems"]
    )
    assert _refresh_complete_current_basis(conn) == basis
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"
    assert reconciler.reconcile_retention(cur)["anonymized"] == 0
    conn.commit()
    conn.close()


def test_runtime_manifest_allows_utc_rollover_and_blocks_after_sla(monkeypatch):
    from services.geo_observation.aggregate_basis import runtime_manifest_problems
    from services.geo_observation_analytics.contract import (
        AGGREGATION_VERSION, CONTRACT_VERSION, METRIC_VERSION,
    )

    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    basis = _refresh_complete_current_basis(conn)
    today = datetime.now(timezone.utc).date()
    completed = datetime.combine(today, time(23, 58), tzinfo=timezone.utc)
    conn.cursor().execute(
        "UPDATE geo_observation_aggregate_refresh_manifest SET completed_at=%s",
        (completed,),
    )
    conn.commit()
    common = {
        "policy_basis_hash": basis,
        "contract_version": CONTRACT_VERSION,
        "aggregation_version": AGGREGATION_VERSION,
        "metric_version": METRIC_VERSION,
    }
    assert runtime_manifest_problems(
        conn.cursor(), now=completed + timedelta(minutes=4), **common
    ) == []
    stale = runtime_manifest_problems(
        conn.cursor(), now=completed + timedelta(minutes=31), **common
    )
    assert stale
    assert all("freshness SLA" in item for item in stale)
    conn.close()


def test_runtime_manifest_missing_bucket_revision_is_never_epoch_zero_clean(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    basis = _refresh_complete_current_basis(conn)
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM public.geo_observation_aggregate_bucket_revision "
        "WHERE (scope_type,bucket_granularity,bucket_start)=("
        "SELECT scope_type,bucket_granularity,bucket_start "
        "FROM public.geo_observation_aggregate_refresh_manifest "
        "WHERE policy_basis_hash=%s LIMIT 1)",
        (basis,),
    )
    assert cur.rowcount == 1
    conn.commit()
    blocked = integration.aggregate_readiness_provider(basis)
    assert blocked["status"] == "unavailable"
    assert any("manifest 缺失" in item for item in blocked["problems"])
    conn.close()


def test_previous_bucket_recompute_cannot_refresh_closed_snapshot(monkeypatch):
    """Recomputing yesterday must not extend its runtime freshness at noon."""
    from services.geo_observation.aggregate_basis import runtime_manifest_problems
    from services.geo_observation_analytics.contract import (
        AGGREGATION_VERSION, CONTRACT_VERSION, METRIC_VERSION,
    )

    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    cur = conn.cursor()
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    previous_day = now.date() - timedelta(days=1)
    observed_at = now - timedelta(days=1)
    cur.execute("UPDATE geo_observation_events SET observed_at=%s", (observed_at,))
    _promote_registered_events(cur)
    conn.commit()

    snap = policy.get_policy(cur)
    basis = canonical_policy_basis(snap["policy"])
    config = observation_aggregates.AggregationConfig(
        policy_version=str(snap["policy_version"]), policy_basis_hash=basis,
    )
    for scope_type in ("private_brand", "public_industry"):
        for granularity in ("day", "week", "month"):
            observation_aggregates.refresh_scope(
                conn,
                scope_type=scope_type,
                granularity=granularity,
                day=previous_day,
                config=config,
                computed_at=now - timedelta(minutes=1),
            )

    problems = runtime_manifest_problems(
        conn.cursor(),
        policy_basis_hash=basis,
        contract_version=CONTRACT_VERSION,
        aggregation_version=AGGREGATION_VERSION,
        metric_version=METRIC_VERSION,
        now=now,
    )
    assert len(problems) == 6
    assert all("freshness SLA" in item for item in problems)
    conn.close()


def test_runtime_manifest_rejects_missing_breakdown_and_extra_lure_output(monkeypatch):
    from psycopg2 import sql

    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    basis = _refresh_complete_current_basis(conn)
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"

    cur = conn.cursor()
    cur.execute(
        "DELETE FROM geo_observation_aggregates WHERE aggregate_id=("
        "SELECT aggregate_id FROM geo_observation_aggregates "
        "WHERE policy_basis_hash=%s AND platform_key IS NOT NULL LIMIT 1)",
        (basis,),
    )
    assert cur.rowcount == 1
    conn.commit()
    missing = integration.aggregate_readiness_provider(basis)
    assert missing["status"] == "unavailable"
    assert any("aggregate output" in item for item in missing["problems"])

    _refresh_complete_current_basis(conn)
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='geo_observation_aggregates' "
        "AND column_name <> 'aggregate_id' ORDER BY ordinal_position"
    )
    columns = [row["column_name"] for row in cur.fetchall()]
    select_exprs = [
        sql.SQL("%s") if column == "aggregate_key" else sql.Identifier(column)
        for column in columns
    ]
    cur.execute(
        sql.SQL("INSERT INTO geo_observation_aggregates ({}) SELECT {} "
                "FROM geo_observation_aggregates WHERE policy_basis_hash=%s LIMIT 1").format(
            sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(select_exprs),
        ),
        tuple(["a" * 64] + [basis]),
    )
    conn.commit()
    lure = integration.aggregate_readiness_provider(basis)
    assert lure["status"] == "unavailable"
    assert any("aggregate output" in item for item in lure["problems"])
    conn.close()


def test_empty_new_buckets_do_not_hide_complete_snapshot_but_epoch_never_falls_back(monkeypatch):
    _seed_healthy_bridge(monkeypatch)
    conn = get_connection()
    basis = _refresh_complete_current_basis(conn)
    snap = policy.get_policy(conn.cursor())
    config = observation_aggregates.AggregationConfig(
        policy_version=str(snap["policy_version"]), policy_basis_hash=basis
    )
    today = datetime.now(timezone.utc).date()
    next_month = (
        today.replace(year=today.year + 1, month=1, day=1)
        if today.month == 12 else today.replace(month=today.month + 1, day=1)
    )
    empty_days = {
        "day": today + timedelta(days=1),
        "week": today + timedelta(days=7),
        "month": next_month,
    }
    completed_at = datetime.now(timezone.utc)
    for scope_type in ("private_brand", "public_industry"):
        for granularity, empty_day in empty_days.items():
            result = observation_aggregates.refresh_scope(
                conn, scope_type=scope_type, granularity=granularity,
                day=empty_day, config=config, computed_at=completed_at,
            )
            assert result["observations"] == 0
    # All six newer bucket manifests are empty; the last complete same-epoch
    # terminals remain the published snapshot inside the SLA.
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"

    conn.cursor().execute(
        "UPDATE geo_observation_events SET processing_state='withdrawn',withdrawn_at=NOW() "
        "WHERE id=(SELECT MIN(id) FROM geo_observation_events "
        "WHERE processing_state='promoted')"
    )
    conn.commit()
    blocked = integration.aggregate_readiness_provider(basis)
    assert blocked["status"] == "unavailable"
    assert any("manifest 缺失" in item for item in blocked["problems"])
    assert _refresh_complete_current_basis(conn) == basis
    assert integration.aggregate_readiness_provider(basis)["status"] == "ready"
    conn.close()
