"""Unified wiring for GEO observation collection, governance and analytics.

This module owns no business truth. It adapts AI-2's database policy to AI-1,
exposes truthful collection readiness to AI-3, and registers only the jobs that
have complete production inputs. Production defaults to reconciling the three
existing collector owners; the optional native sampling driver is a separate,
explicit mode and is never installed as a readiness placebo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from services.ai_surface_monitoring.migrations_stub import PostgresBudgetLedger
from services.ai_surface_monitoring.policy import ObservationPolicySnapshot
from services.ai_surface_monitoring.registry import AdapterRegistry
from services.ai_surface_monitoring.scheduler_wiring import (
    check_readiness as collection_control_readiness,
    register_observation_collection_jobs,
    set_reservation_reaper,
)
from services.ai_surface_monitoring.service import (
    CollectionService,
    configure_default_service,
)

logger = logging.getLogger("GEO-ObservationIntegration")

from services.geo_observation.collection_mode import (
    DEFAULT_COLLECTION_MODE,
    ObservationCollectionMode,
    parse_collection_mode,
)
from services.geo_observation.collection_readiness import (
    bridge_platform_smoke_problems,
    bridge_scheduler_probe,
    native_scheduler_probe,
    passive_platform_health,
    probe_bridge_database,
    registered_adapter_problems,
)


class DatabaseObservationPolicyReader:
    """Read AI-2's policy SSOT and validate the complete AI-1 contract."""

    def get_policy(self) -> ObservationPolicySnapshot:
        from services.geo_observation.policy import get_policy

        raw = get_policy()
        policy = dict(raw.get("policy") or {})
        policy["policy_version"] = str(raw["policy_version"])
        policy["feature_flags"] = dict(raw.get("effective_flags") or {})
        return ObservationPolicySnapshot.model_validate(policy)


@dataclass(frozen=True)
class CollectionRuntime:
    policy: DatabaseObservationPolicyReader
    registry: AdapterRegistry
    ledger: PostgresBudgetLedger
    service: CollectionService


@lru_cache(maxsize=1)
def collection_runtime() -> CollectionRuntime:
    """Build one process-local facade over database-backed shared state."""
    policy = DatabaseObservationPolicyReader()
    registry = AdapterRegistry(policy)
    ledger = PostgresBudgetLedger()
    service = CollectionService(registry, ledger=ledger)
    configure_default_service(service)
    set_reservation_reaper(ledger.reap_stale_reservations)
    return CollectionRuntime(policy=policy, registry=registry, ledger=ledger, service=service)


def policy_provider() -> dict:
    """AI-3 read seam; return AI-2's nested policy shape unchanged."""
    from services.geo_observation.policy import get_policy

    return get_policy()


def collection_mode_provider(raw_policy: dict | None = None) -> ObservationCollectionMode:
    """Read the database-backed collection ownership mode; unknown values fail closed."""
    raw = raw_policy if raw_policy is not None else policy_provider()
    return parse_collection_mode(raw.get("collection_mode", DEFAULT_COLLECTION_MODE.value))


_health_cache_lock = threading.Lock()
_health_cache_until = 0.0
_health_cache_rows: list[dict] = []
_health_cache_key: tuple[Any, str] | None = None


def platform_health_provider() -> list[dict]:
    """Return mode-appropriate health without duplicating provider calls.

    Bridge mode derives health from imported terminal facts. Only the future
    native mode may execute adapter health probes.
    """
    global _health_cache_until, _health_cache_rows, _health_cache_key
    now = time.monotonic()
    with _health_cache_lock:
        raw_policy = policy_provider()
        mode = collection_mode_provider(raw_policy)
        cache_key = (raw_policy.get("policy_version"), mode.value)
        if cache_key == _health_cache_key and now < _health_cache_until:
            return [dict(row) for row in _health_cache_rows]
        if mode is ObservationCollectionMode.EXISTING_COLLECTORS_RECONCILED:
            from db.connection import get_connection

            conn = get_connection()
            try:
                _health_cache_rows = passive_platform_health(conn.cursor(), raw_policy)
            finally:
                conn.close()
        else:
            # Native takeover is deliberately not shipped in this release.  A
            # persisted/forced native value must not turn a read-only health
            # request into a second provider call while existing owners run.
            _health_cache_rows = []
        _health_cache_key = cache_key
        _health_cache_until = time.monotonic() + 60.0
        return [dict(row) for row in _health_cache_rows]


def _runtime_scheduler_job_ids() -> list[str] | None:
    """Inspect the cron-local scheduler; web workers use its durable inventory."""
    # Only the elected cron process is authoritative. An unset/misspelled role,
    # prestart process or backup worker must not turn a merely constructed local
    # scheduler into evidence that production jobs are actually owned.
    if os.getenv("ROLE", "").strip().lower() != "cron":
        return None
    from api import scheduler as scheduler_module

    scheduler = getattr(scheduler_module, "_scheduler", None)
    if scheduler is None:
        return None
    return [str(job.id) for job in scheduler.get_jobs()]


_SCHEDULER_INVENTORY_KEY = "geo_observation_scheduler_inventory"


def record_scheduler_inventory(job_ids: list[str]) -> None:
    """Persist the cron leader's real in-memory job inventory after a good tick."""
    from db.connection import get_db

    raw_policy = policy_provider()
    payload = {
        "mode": collection_mode_provider(raw_policy).value,
        "policy_version": raw_policy.get("policy_version"),
        "job_ids": [str(job_id) for job_id in job_ids],
        "recorded_at": datetime.now(ZoneInfo("UTC")).isoformat(),
    }
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO system_settings(key,value,value_type,description,updated_at) "
            "VALUES (%s,%s,'json','GEO 观测 cron leader 实际 job 清单',NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,value_type='json',"
            "description=EXCLUDED.description,updated_at=NOW()",
            (_SCHEDULER_INVENTORY_KEY, json.dumps(payload, ensure_ascii=False)),
        )


def _durable_scheduler_job_ids(
    cur: Any, *, mode: ObservationCollectionMode, policy_version: Any
) -> tuple[list[str], list[str]]:
    """Read the last inventory emitted by the sole cron leader; malformed is unavailable."""
    cur.execute("SELECT value FROM system_settings WHERE key=%s", (_SCHEDULER_INVENTORY_KEY,))
    row = cur.fetchone()
    if not row:
        return [], ["cron leader 尚无耐久 scheduler inventory"]
    try:
        raw_value = row.get("value") if isinstance(row, dict) else row[0]
        payload = json.loads(raw_value)
        recorded_at = datetime.fromisoformat(str(payload["recorded_at"]))
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=ZoneInfo("UTC"))
        age = (
            datetime.now(ZoneInfo("UTC"))
            - recorded_at.astimezone(ZoneInfo("UTC"))
        ).total_seconds()
        problems: list[str] = []
        if age > 15 * 60:
            problems.append(f"scheduler inventory 已超时({int(age)}s > 900s)")
        if payload.get("mode") != mode.value:
            problems.append("scheduler inventory collection mode 与 policy 不一致")
        if payload.get("policy_version") != policy_version:
            problems.append("scheduler inventory policy_version 与当前 policy 不一致")
        job_ids = payload.get("job_ids")
        if not isinstance(job_ids, list) or any(not isinstance(item, str) for item in job_ids):
            raise ValueError("job_ids 不是字符串数组")
        return list(job_ids), problems
    except Exception as exc:
        return [], [f"scheduler inventory 不可解析: {type(exc).__name__}"]


def _empty_collection_readiness(
    *, mode: ObservationCollectionMode, policy_version: Any = None, problems: list[str]
) -> dict:
    return {
        "mode": mode.value,
        "ready": False,
        "status": "unavailable",
        "problems": problems,
        "reconciler_last_success_at": None,
        "reconciler_backlog": 0,
        "source_watermarks": {
            "paid_diagnosis": None,
            "recurring_monitoring": None,
            "research_round": None,
        },
        "duplicate_collection_jobs": [],
        "policy_version": policy_version,
    }


def collection_readiness_provider() -> dict:
    """Return the complete, mode-aware collection readiness contract."""
    mode = DEFAULT_COLLECTION_MODE
    policy_version: Any = None
    try:
        from db.connection import get_connection
        from services.geo_observation.readiness import verify_geo_observation_schema

        raw_policy = policy_provider()
        policy_version = raw_policy.get("policy_version")
        mode = collection_mode_provider(raw_policy)
        job_ids = _runtime_scheduler_job_ids()
        conn = get_connection()
        try:
            cur = conn.cursor()
            verify_geo_observation_schema(cur)
            verify_integration_schema(cur)
            scheduler_inventory_problems: list[str] = []
            if job_ids is None:
                job_ids, scheduler_inventory_problems = _durable_scheduler_job_ids(
                    cur, mode=mode, policy_version=policy_version
                )
            if mode is ObservationCollectionMode.EXISTING_COLLECTORS_RECONCILED:
                database = probe_bridge_database(cur)
                bridge_health = passive_platform_health(cur, raw_policy)
            else:
                database = None
                cur.execute(
                    "SELECT MAX(finished_at) AS t FROM sched_job_runs "
                    "WHERE job_name=%s AND status='done'",
                    ("ai_surface_obs_reconciler",),
                )
                row = cur.fetchone()
                native_last_success = row.get("t") if isinstance(row, dict) else row[0]
        finally:
            conn.close()
    except Exception as exc:
        return _empty_collection_readiness(
            mode=mode,
            policy_version=policy_version,
            problems=[f"观测采集接线不可用: {type(exc).__name__}"],
        )

    if mode is ObservationCollectionMode.EXISTING_COLLECTORS_RECONCILED:
        scheduler_probe = bridge_scheduler_probe(job_ids)
        structural = registered_adapter_problems()
        structural.extend(scheduler_inventory_problems)
        structural.extend(scheduler_probe["problems"])
        structural.extend(database["structural_problems"])
        structural.extend(bridge_platform_smoke_problems(bridge_health))
        attention = list(database["attention_problems"])
        status = "unavailable" if structural else ("attention_required" if attention else "ready")
        return {
            "mode": mode.value,
            "ready": status == "ready",
            "status": status,
            "problems": structural + attention,
            "reconciler_last_success_at": database["reconciler_last_success_at"],
            "reconciler_backlog": database["reconciler_backlog"],
            "source_watermarks": database["source_watermarks"],
            "duplicate_collection_jobs": scheduler_probe["duplicate_collection_jobs"],
            "policy_version": policy_version,
        }

    # Current production architecture is EXISTING_COLLECTORS_RECONCILED.  Do
    # not instantiate the native runtime, registry, driver or provider probes:
    # native remains a future schema value but cannot become a production mode
    # until an explicit owner-retirement/takeover package is reviewed.
    scheduler_probe = native_scheduler_probe(job_ids)
    structural = list(scheduler_inventory_problems)
    structural.extend(scheduler_probe["problems"])
    if native_last_success is None:
        structural.append("native reconciler 尚无成功终态")
    status = "unavailable"
    return {
        "mode": mode.value,
        "ready": status == "ready",
        "status": status,
        "problems": structural,
        "reconciler_last_success_at": (
            native_last_success.isoformat() if native_last_success is not None else None
        ),
        "reconciler_backlog": 0,
        "source_watermarks": {
            "paid_diagnosis": None,
            "recurring_monitoring": None,
            "research_round": None,
        },
        "duplicate_collection_jobs": scheduler_probe["duplicate_collection_jobs"],
        "policy_version": policy_version,
    }


def build_product_api_deps():
    """Create AI-3 dependencies with real AI-1/AI-2 providers."""
    from api.geo_observation_product_api import default_deps

    deps = default_deps()
    deps.platform_health_provider = platform_health_provider
    deps.policy_provider = policy_provider
    deps.collection_readiness_provider = collection_readiness_provider
    deps.aggregate_readiness_provider = aggregate_readiness_provider
    return deps


def aggregate_readiness_provider(policy_basis_hash: str) -> dict:
    """Runtime fence for the last complete immutable aggregate snapshots."""
    from datetime import timezone

    from db.connection import get_connection
    from services.geo_observation.aggregate_basis import runtime_manifest_problems
    from services.geo_observation_analytics.contract import (
        AGGREGATION_VERSION,
        CONTRACT_VERSION,
        METRIC_VERSION,
    )

    conn = get_connection()
    try:
        problems = runtime_manifest_problems(
            conn.cursor(),
            policy_basis_hash=policy_basis_hash,
            contract_version=CONTRACT_VERSION,
            aggregation_version=AGGREGATION_VERSION,
            metric_version=METRIC_VERSION,
            now=datetime.now(timezone.utc),
        )
    finally:
        conn.close()
    return {
        "status": "ready" if not problems else "unavailable",
        "problems": problems,
        "policy_basis_hash": policy_basis_hash,
    }


def _aggregation_config(raw_policy: dict):
    from services.geo_observation_analytics.aggregates import AggregationConfig
    from services.geo_observation.aggregate_basis import canonical_policy_basis

    policy = raw_policy["policy"]
    return AggregationConfig(
        policy_version=str(raw_policy["policy_version"]),
        policy_basis_hash=canonical_policy_basis(policy),
        max_single_brand_share_bps=int(policy["max_single_brand_share_bps"]),
        anomaly_numerator=int(policy["anomaly_confirmation_numerator"]),
        anomaly_denominator=int(policy["anomaly_confirmation_denominator"]),
    )


def aggregation_refresh_job(today: date | None = None) -> dict:
    """Refresh live boundaries plus every durable bucket invalidation."""
    from db.connection import get_connection
    from services.geo_observation_analytics.aggregates import bucket_bounds, refresh_scope

    raw_policy = policy_provider()
    flags = raw_policy.get("effective_flags") or {}
    if not flags.get("aggregation_enabled", False):
        return {"enabled": False, "refreshed": 0}

    # Aggregate windows are UTC; choosing a Shanghai calendar date here can
    # point at tomorrow's empty UTC bucket for eight hours every day.
    today = today or datetime.now(ZoneInfo("UTC")).date()
    days_by_granularity: dict[str, list[date]] = {}
    for granularity in ("day", "week", "month"):
        unique: dict[date, date] = {}
        for candidate in (today, today - timedelta(days=1)):
            bucket_start, _ = bucket_bounds(granularity, candidate)
            unique[bucket_start] = candidate
        days_by_granularity[granularity] = list(unique.values())

    config = _aggregation_config(raw_policy)
    conn = get_connection()
    refreshed = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT scope_type,bucket_granularity,bucket_start
                     FROM public.geo_observation_aggregate_bucket_revision
                    WHERE dirty=TRUE
                    ORDER BY bucket_start,scope_type,bucket_granularity"""
            )
            dirty_buckets = list(cur.fetchall())
        refresh_plan: set[tuple[str, str, date]] = set()
        for scope_type in ("private_brand", "public_industry"):
            for granularity, days in days_by_granularity.items():
                for candidate in days:
                    bucket_start, _ = bucket_bounds(granularity, candidate)
                    refresh_plan.add((scope_type, granularity, bucket_start))
        for row in dirty_buckets:
            scope_type = row["scope_type"] if isinstance(row, dict) else row[0]
            granularity = row["bucket_granularity"] if isinstance(row, dict) else row[1]
            bucket_start = row["bucket_start"] if isinstance(row, dict) else row[2]
            refresh_plan.add((scope_type, granularity, bucket_start))
        for scope_type, granularity, candidate in sorted(refresh_plan):
            refresh_scope(
                conn,
                scope_type=scope_type,
                granularity=granularity,
                day=candidate,
                config=config,
            )
            refreshed += 1
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return {"enabled": True, "refreshed": refreshed}


def register_geo_observation_integration_jobs(scheduler: Any) -> list[str]:
    """Register governance and aggregation jobs on the cron leader scheduler.

    Existing-collector mode registers no AI-1 collection jobs. Native remains
    fail-closed until a separately reviewed owner takeover is shipped.
    """
    from apscheduler.triggers.interval import IntervalTrigger
    from services.geo_observation.wiring import register_geo_observation_jobs
    from services.sched_claim import sched_claim

    registered = register_geo_observation_jobs(scheduler)
    mode = collection_mode_provider()
    if mode is ObservationCollectionMode.NATIVE_SAMPLING_DRIVER:
        logger.error(
            "native_sampling_driver requested but takeover is not shipped; "
            "no native collection/provider jobs were registered"
        )
    job_id = "geo_observation_aggregation_refresh"
    if not scheduler.get_job(job_id):
        scheduler.add_job(
            sched_claim(job_id, 600, reclaimable=True)(aggregation_refresh_job),
            trigger=IntervalTrigger(minutes=10),
            id=job_id,
            name="GEO 观测聚合刷新(每 10 分钟)",
            replace_existing=True,
        )
        registered.append(job_id)
    logger.info("registered unified GEO observation jobs: %s", registered)
    return registered


_REQUIRED_COLUMN_SPECS = {
    "geo_ai_surface_cost_ledger": {
        "id": ("bigint", None, False, "__sequence__"),
        "source_kind": ("character varying", 40, False, None),
        "surface_key": ("character varying", 80, False, None),
        "amount_micros": ("bigint", None, False, None),
        "call_count": ("bigint", None, False, "1"),
        "is_estimated": ("boolean", None, False, "false"),
        "request_id": ("character varying", 200, False, None),
        "round_id": ("character varying", 64, True, None),
        "recorded_at": ("timestamp with time zone", None, False, "now()"),
    },
    "geo_ai_surface_cost_reservations": {
        "token": ("character varying", 64, False, None),
        "source_kind": ("character varying", 40, False, None),
        "surface_key": ("character varying", 80, False, None),
        "amount_micros": ("bigint", None, False, None),
        "call_count": ("bigint", None, False, "1"),
        "request_id": ("character varying", 200, False, None),
        "round_id": ("character varying", 64, True, None),
        "created_at": ("timestamp with time zone", None, False, "now()"),
    },
    "geo_observation_insight_jobs": {
        "job_id": ("text", None, False, None),
        "input_hash": ("character", 64, False, None),
        "owner_user_id": ("bigint", None, False, None),
        "brand_id": ("bigint", None, False, None),
        "request_id": ("text", None, True, None),
        "state": ("text", None, False, "'pending'::text"),
        "result_state": ("text", None, True, None),
        "provider": ("text", None, False, "'deepseek'::text"),
        "model": ("text", None, False, None),
        "prompt_version": ("text", None, False, None),
        "schema_version": ("text", None, False, None),
        "summary": ("text", None, True, None),
        "evidence_refs": ("jsonb", None, False, "'[]'::jsonb"),
        "allowed_actions": ("jsonb", None, False, "'[]'::jsonb"),
        "error_code": ("text", None, True, None),
        "error_detail": ("text", None, True, None),
        "input_token": ("integer", None, True, None),
        "output_token": ("integer", None, True, None),
        "cost_micros": ("bigint", None, True, None),
        "attempts": ("integer", None, False, "0"),
        "lease_token": ("uuid", None, True, None),
        "lease_until": ("timestamp with time zone", None, True, None),
        "created_at": ("timestamp with time zone", None, False, "now()"),
        "updated_at": ("timestamp with time zone", None, False, "now()"),
        "completed_at": ("timestamp with time zone", None, True, None),
        "snapshot_id": ("character", 64, True, None),
        "policy_basis_hash": ("text", None, True, None),
        "scope_type": ("text", None, True, None),
        "bucket_granularity": ("text", None, True, None),
        "bucket_start": ("date", None, True, None),
        "bucket_epoch": ("bigint", None, True, None),
        "promotion_sequence_watermark": ("bigint", None, True, None),
        "aggregate_input_watermark": ("timestamp with time zone", None, True, None),
        "aggregate_contract_version": ("text", None, True, None),
        "aggregate_aggregation_version": ("text", None, True, None),
        "aggregate_metric_version": ("text", None, True, None),
        "paid_call_started_at": ("timestamp with time zone", None, True, None),
        "paid_call_unknown_at": ("timestamp with time zone", None, True, None),
    },
}

_REQUIRED_CONSTRAINT_SPECS = {
    ("geo_ai_surface_cost_ledger", "geo_ai_surface_cost_ledger_pkey"): (
        "p", ("id",), "PRIMARY KEY (id)",
    ),
    ("geo_ai_surface_cost_ledger", "geo_ai_surface_cost_ledger_amount_nonneg"): (
        "c", ("amount_micros", "call_count"),
        "CHECK (amount_micros >= 0 AND call_count >= 0)",
    ),
    ("geo_ai_surface_cost_ledger", "geo_ai_surface_cost_ledger_request_uniq"): (
        "u", ("request_id",), "UNIQUE (request_id)",
    ),
    ("geo_ai_surface_cost_reservations", "geo_ai_surface_cost_reservations_pkey"): (
        "p", ("token",), "PRIMARY KEY (token)",
    ),
    ("geo_ai_surface_cost_reservations", "geo_ai_surface_cost_res_amount_nonneg"): (
        "c", ("amount_micros", "call_count"),
        "CHECK (amount_micros >= 0 AND call_count >= 0)",
    ),
    ("geo_ai_surface_cost_reservations", "geo_ai_surface_cost_res_request_uniq"): (
        "u", ("request_id",), "UNIQUE (request_id)",
    ),
    ("geo_observation_insight_jobs", "geo_observation_insight_jobs_pkey"): (
        "p", ("job_id",), "PRIMARY KEY (job_id)",
    ),
    ("geo_observation_insight_jobs", "geo_obs_insight_state_chk"): (
        "c", ("state",),
        "CHECK (state = ANY (ARRAY['pending'::text, 'running'::text, "
        "'paid_call_started'::text, 'completed'::text, 'failed'::text, "
        "'result_unknown'::text]))",
    ),
    ("geo_observation_insight_jobs", "geo_obs_insight_result_state_chk"): (
        "c", ("result_state",),
        "CHECK (result_state IS NULL OR "
        "(result_state = ANY (ARRAY['ok'::text, 'insufficient'::text])))",
    ),
    ("geo_observation_insight_jobs", "geo_obs_insight_scope_snapshot_uk"): (
        "u", ("owner_user_id", "brand_id", "input_hash", "snapshot_id"),
        "UNIQUE (owner_user_id, brand_id, input_hash, snapshot_id)",
    ),
    ("geo_observation_insight_jobs", "chk_geo_obs_insight_snapshot"): (
        "c", ("snapshot_id", "state", "policy_basis_hash", "scope_type", "bucket_granularity",
              "bucket_start", "bucket_epoch", "promotion_sequence_watermark",
              "aggregate_input_watermark", "aggregate_contract_version",
              "aggregate_aggregation_version", "aggregate_metric_version"),
        "CHECK (snapshot_id IS NULL AND state = 'result_unknown'::text AND policy_basis_hash IS NULL AND scope_type IS NULL "
        "AND bucket_granularity IS NULL AND bucket_start IS NULL AND bucket_epoch IS NULL "
        "AND promotion_sequence_watermark IS NULL AND aggregate_input_watermark IS NULL "
        "AND aggregate_contract_version IS NULL AND aggregate_aggregation_version IS NULL "
        "AND aggregate_metric_version IS NULL OR snapshot_id IS NOT NULL "
        "AND snapshot_id ~ '^[0-9a-f]{64}$'::text AND policy_basis_hash IS NOT NULL "
        "AND policy_basis_hash ~ '^[0-9a-f]{64}$'::text AND scope_type IS NOT NULL "
        "AND scope_type = 'private_brand'::text AND bucket_granularity IS NOT NULL "
        "AND (bucket_granularity = ANY (ARRAY['day'::text, 'week'::text, 'month'::text])) "
        "AND bucket_start IS NOT NULL AND bucket_epoch IS NOT NULL AND bucket_epoch >= 0 "
        "AND promotion_sequence_watermark IS NOT NULL AND promotion_sequence_watermark >= 0 "
        "AND aggregate_input_watermark IS NOT NULL AND aggregate_contract_version IS NOT NULL "
        "AND length(aggregate_contract_version) > 0 AND aggregate_aggregation_version IS NOT NULL "
        "AND length(aggregate_aggregation_version) > 0 AND aggregate_metric_version IS NOT NULL "
        "AND length(aggregate_metric_version) > 0)",
    ),
}

_REQUIRED_INDEX_SPECS = {
    ("geo_ai_surface_cost_ledger", "idx_ai_surface_cost_ledger_src_time"): (
        False, ("source_kind", "recorded_at"), None,
    ),
    ("geo_ai_surface_cost_ledger", "idx_ai_surface_cost_ledger_round"): (
        False, ("round_id",), None,
    ),
    ("geo_ai_surface_cost_reservations", "idx_ai_surface_cost_res_src"): (
        False, ("source_kind",), None,
    ),
    ("geo_ai_surface_cost_reservations", "idx_ai_surface_cost_res_created"): (
        False, ("created_at",), None,
    ),
    ("geo_ai_surface_cost_reservations", "idx_ai_surface_cost_res_round"): (
        False, ("round_id",), None,
    ),
    ("geo_observation_insight_jobs", "idx_geo_obs_insight_reclaim"): (
        False, ("state", "lease_until"), None,
    ),
    ("geo_observation_insight_jobs", "idx_geo_obs_insight_paid_recovery"): (
        False, ("state", "lease_until"),
        "(state = ANY (ARRAY['pending'::text, 'running'::text, 'paid_call_started'::text, "
        "'result_unknown'::text]))",
    ),
}


def _canonical_definition(value: str) -> str:
    return "".join(str(value).lower().split())


def _default_matches(expected: str | None, actual: str | None) -> bool:
    if expected is None:
        return actual is None
    if actual is None:
        return False
    canonical_actual = _canonical_definition(actual)
    if expected == "__sequence__":
        return canonical_actual.startswith("nextval(")
    return canonical_actual == _canonical_definition(expected)


def _catalog_value(row: Any, index: int, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else row[index]


def verify_integration_schema(cur: Any) -> None:
    """Verify the complete integration schema using PostgreSQL catalog truth."""
    for table, required in _REQUIRED_COLUMN_SPECS.items():
        cur.execute(
            "SELECT column_name, data_type, character_maximum_length, is_nullable, "
            "column_default "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s",
            (table,),
        )
        have = {
            _catalog_value(row, 0, "column_name"): (
                _catalog_value(row, 1, "data_type"),
                _catalog_value(row, 2, "character_maximum_length"),
                _catalog_value(row, 3, "is_nullable") == "YES",
                _catalog_value(row, 4, "column_default"),
            )
            for row in cur.fetchall()
        }
        missing = required.keys() - have.keys()
        if missing:
            raise RuntimeError(f"{table} 缺列 {sorted(missing)}")
        mismatched = {
            name: {"expected": spec, "actual": have[name]}
            for name, spec in required.items()
            if have[name][:3] != spec[:3]
            or not _default_matches(spec[3], have[name][3])
        }
        if mismatched:
            raise RuntimeError(f"{table} 列类型/长度/可空性/默认值不符: {mismatched}")

    tables = sorted({table for table, _ in _REQUIRED_CONSTRAINT_SPECS})
    cur.execute(
        "SELECT c.conrelid::regclass::text AS table_name, c.conname, c.contype, c.convalidated, "
        "pg_get_constraintdef(c.oid, true) AS definition, "
        "ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum "
        "ORDER BY k.ord) AS columns "
        "FROM pg_constraint c WHERE c.conrelid::regclass::text = ANY(%s)",
        (tables,),
    )
    found_constraints = {
        (
            _catalog_value(row, 0, "table_name"),
            _catalog_value(row, 1, "conname"),
        ): (
            _catalog_value(row, 2, "contype"),
            bool(_catalog_value(row, 3, "convalidated")),
            tuple(_catalog_value(row, 5, "columns") or ()),
            _catalog_value(row, 4, "definition"),
        )
        for row in cur.fetchall()
    }
    constraint_errors: dict[str, Any] = {}
    for key, expected in _REQUIRED_CONSTRAINT_SPECS.items():
        actual = found_constraints.get(key)
        if actual is None:
            constraint_errors[f"{key[0]}.{key[1]}"] = "missing"
            continue
        expected_type, expected_columns, expected_definition = expected
        actual_type, validated, actual_columns, actual_definition = actual
        if (
            actual_type != expected_type
            or not validated
            or actual_columns != expected_columns
            or _canonical_definition(actual_definition)
            != _canonical_definition(expected_definition)
        ):
            constraint_errors[f"{key[0]}.{key[1]}"] = {
                "expected": expected,
                "actual": actual,
            }
    if constraint_errors:
        raise RuntimeError(f"GEO observation integration 约束不符: {constraint_errors}")

    cur.execute(
        "SELECT t.relname AS table_name, i.relname AS index_name, x.indisunique, x.indisvalid, x.indisready, "
        "ARRAY(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum "
        "ORDER BY k.ord) AS columns, pg_get_expr(x.indpred, x.indrelid) AS predicate "
        "FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid "
        "JOIN pg_class t ON t.oid=x.indrelid WHERE t.relname = ANY(%s)",
        (tables,),
    )
    found_indexes = {
        (
            _catalog_value(row, 0, "table_name"),
            _catalog_value(row, 1, "index_name"),
        ): (
            bool(_catalog_value(row, 2, "indisunique")),
            bool(_catalog_value(row, 3, "indisvalid")),
            bool(_catalog_value(row, 4, "indisready")),
            tuple(_catalog_value(row, 5, "columns") or ()),
            _catalog_value(row, 6, "predicate"),
        )
        for row in cur.fetchall()
    }
    index_errors: dict[str, Any] = {}
    for key, expected in _REQUIRED_INDEX_SPECS.items():
        actual = found_indexes.get(key)
        if actual is None:
            index_errors[f"{key[0]}.{key[1]}"] = "missing"
            continue
        expected_unique, expected_columns, expected_predicate = expected
        actual_unique, valid, ready, actual_columns, actual_predicate = actual
        if (
            actual_unique != expected_unique
            or not valid
            or not ready
            or actual_columns != expected_columns
            or _canonical_definition(actual_predicate or "")
            != _canonical_definition(expected_predicate or "")
        ):
            index_errors[f"{key[0]}.{key[1]}"] = {
                "expected": expected,
                "actual": actual,
            }
    if index_errors:
        raise RuntimeError(f"GEO observation integration 索引不符: {index_errors}")


def verify_integration_schema_on_startup() -> None:
    """Fail closed when the additive AI-1/AI-3 integration schema is partial."""
    import os
    import psycopg2

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL 未配置,跳过 GEO observation integration schema 反查")
        return
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            verify_integration_schema(cur)
    finally:
        conn.close()
