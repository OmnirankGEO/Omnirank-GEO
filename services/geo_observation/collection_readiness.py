"""Truthful readiness probes for the selected GEO observation collection mode.

The production default imports terminal facts from existing diagnosis,
monitoring and research owners.  These probes are read-only and never invoke a
provider, reserve budget or write the observation ledger.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from services.ai_surface_monitoring.scheduler_wiring import (
    JOB_MONITORING_DAILY,
    JOB_RECONCILER as NATIVE_RECONCILER_JOB,
    JOB_RESEARCH_TICK,
)

from .source_adapters import registered_source_adapters


RECONCILER_JOB = "geo_observation_reconciler"
AGGREGATION_JOB = "geo_observation_aggregation_refresh"
MONITORING_OWNER_JOB = "keyword_subscription_daily_monitoring"
RESEARCH_OWNER_JOBS = frozenset(
    {"research_monitor_bimonthly_round", "research_monitor_missed_recovery"}
)

RECONCILER_MAX_AGE_SECONDS = 15 * 60
RECONCILER_LOOKBACK_HOURS = 24
MAX_RECONCILER_BACKLOG = 500
MAX_REJECTED_EVENTS_24H = 100
MAX_STUCK_EVENTS = 0
PASSIVE_HEALTHY_MAX_AGE_SECONDS = 36 * 60 * 60
PASSIVE_UNAVAILABLE_AGE_SECONDS = 7 * 24 * 60 * 60

REQUIRED_SOURCE_TYPES = frozenset(
    {"paid_diagnosis", "recurring_monitoring", "research_round"}
)
REQUIRED_PRODUCT_SMOKE_PLATFORMS = frozenset({"deepseek", "yuanbao"})
FORBIDDEN_BRIDGE_COLLECTION_JOBS = frozenset(
    {JOB_MONITORING_DAILY, JOB_RESEARCH_TICK, NATIVE_RECONCILER_JOB, "daily_monitoring"}
)


def _first_value(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def _scalar(cur: Any, query: str, params: tuple = ()) -> Any:
    cur.execute(query, params)
    return _first_value(cur.fetchone())


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def registered_adapter_problems() -> list[str]:
    adapters = registered_source_adapters()
    problems: list[str] = []
    for source_type in sorted(REQUIRED_SOURCE_TYPES):
        adapter = adapters.get(source_type)
        if adapter is None or not callable(getattr(adapter, "register_terminal", None)):
            problems.append(f"{source_type} source adapter 未注册")
    unexpected = sorted(set(adapters) - REQUIRED_SOURCE_TYPES)
    if unexpected:
        problems.append(f"source adapter registry 含未知来源: {unexpected}")
    return problems


def bridge_scheduler_probe(job_ids: Iterable[str]) -> dict[str, Any]:
    """Check ownership without creating or starting a scheduler."""
    counts = Counter(str(job_id) for job_id in job_ids)
    problems: list[str] = []
    duplicates: list[str] = []

    for required in (RECONCILER_JOB, AGGREGATION_JOB, MONITORING_OWNER_JOB):
        count = counts.get(required, 0)
        if count != 1:
            problems.append(f"scheduler job {required} 期望唯一 1 个,实际 {count} 个")
        if count > 1:
            duplicates.append(required)

    for forbidden in sorted(FORBIDDEN_BRIDGE_COLLECTION_JOBS):
        if counts.get(forbidden, 0):
            duplicates.append(forbidden)
            problems.append(
                f"bridge 模式检测到第二套采集 job {forbidden};禁止重复供应商调用/扣费"
            )

    return {
        "problems": problems,
        "duplicate_collection_jobs": sorted(set(duplicates)),
    }


def native_scheduler_probe(job_ids: Iterable[str]) -> dict[str, Any]:
    """Fail closed until a separately reviewed native-owner takeover exists.

    Merely registering the AI-1 jobs is never evidence that the existing
    monitoring/research owners were retired.  Keeping both owners alive would
    duplicate provider calls and billing, so the current release does not allow
    native mode to become ready at all.
    """
    counts = Counter(str(job_id) for job_id in job_ids)
    problems: list[str] = [
        "native_sampling_driver 尚未完成现役采集 owner takeover;当前版本禁止启用"
    ]
    duplicates: list[str] = []
    for required in (JOB_MONITORING_DAILY, JOB_RESEARCH_TICK, NATIVE_RECONCILER_JOB, AGGREGATION_JOB):
        count = counts.get(required, 0)
        if count != 1:
            problems.append(f"native scheduler job {required} 期望唯一 1 个,实际 {count} 个")
        if count > 1:
            duplicates.append(required)
    for existing_owner in sorted({MONITORING_OWNER_JOB, *RESEARCH_OWNER_JOBS}):
        if counts.get(existing_owner, 0):
            duplicates.append(existing_owner)
            problems.append(
                f"native 模式仍存在现役采集 owner {existing_owner};禁止双采双扣"
            )
    return {
        "problems": problems,
        "duplicate_collection_jobs": sorted(set(duplicates)),
    }


def _capped_count(cur: Any, body: str, params: tuple, cap: int) -> int:
    query = f"SELECT COUNT(*) AS n FROM ({body} LIMIT %s) bounded_rows"
    return int(_scalar(cur, query, (*params, cap + 1)) or 0)


def probe_bridge_database(cur: Any) -> dict[str, Any]:
    """Read terminal watermarks, missing imports and tenant ownership integrity."""
    watermarks = {
        "paid_diagnosis": _iso(_scalar(
            cur,
            "SELECT MAX(finished_at) FROM diagnosis_runs "
            "WHERE run_status IN ('committed','completed_exempt')",
        )),
        "recurring_monitoring": _iso(_scalar(
            cur,
            "SELECT MAX(mr.tested_at) FROM monitoring_results mr "
            "JOIN monitoring_tasks mt ON mt.id=mr.task_id "
            "WHERE mt.status='completed' AND mt.completed_at IS NOT NULL",
        )),
        "research_round": _iso(_scalar(
            cur,
            "SELECT MAX(r.created_at) FROM geo_research_raw r "
            "JOIN geo_research_round rr ON rr.batch_id=r.batch_id WHERE rr.status='completed'",
        )),
    }

    backlog_by_source = {
        "paid_diagnosis": _capped_count(
            cur,
            "SELECT 1 FROM diagnosis_runs dr "
            "WHERE dr.run_status IN ('committed','completed_exempt') "
            "AND dr.finished_at > NOW()-make_interval(hours => %s) "
            "AND NOT EXISTS (SELECT 1 FROM public.geo_observation_events e "
            "WHERE e.source_type='paid_diagnosis' AND e.source_record_id=dr.run_token)",
            (RECONCILER_LOOKBACK_HOURS,), MAX_RECONCILER_BACKLOG,
        ),
        "recurring_monitoring": _capped_count(
            cur,
            "SELECT 1 FROM monitoring_results mr JOIN monitoring_tasks mt ON mt.id=mr.task_id "
            "WHERE mt.status='completed' AND mt.completed_at IS NOT NULL "
            "AND mr.tested_at > NOW()-make_interval(hours => %s) "
            "AND NOT EXISTS (SELECT 1 FROM public.geo_observation_events e "
            "WHERE e.source_type='recurring_monitoring' AND e.source_record_id=mr.id::text)",
            (RECONCILER_LOOKBACK_HOURS,), MAX_RECONCILER_BACKLOG,
        ),
        "research_round": _capped_count(
            cur,
            "SELECT 1 FROM geo_research_raw r JOIN geo_research_round rr ON rr.batch_id=r.batch_id "
            "WHERE rr.status='completed' "
            "AND r.created_at > NOW()-make_interval(hours => %s) "
            "AND NOT EXISTS (SELECT 1 FROM public.geo_observation_events e "
            "WHERE e.source_type='research_round' AND e.source_record_id=r.id::text)",
            (RECONCILER_LOOKBACK_HOURS,), MAX_RECONCILER_BACKLOG,
        ),
    }
    backlog = sum(backlog_by_source.values())

    last_success = _scalar(
        cur,
        "SELECT MAX(finished_at) FROM sched_job_runs "
        "WHERE job_name=%s AND status='done'",
        (RECONCILER_JOB,),
    )
    last_success_at = _iso(last_success)
    reconciler_age: Optional[int] = None
    if last_success is not None:
        now = datetime.now(timezone.utc)
        if getattr(last_success, "tzinfo", None) is None:
            last_success = last_success.replace(tzinfo=timezone.utc)
        reconciler_age = max(0, int((now - last_success.astimezone(timezone.utc)).total_seconds()))

    rejected = _capped_count(
        cur,
        "SELECT 1 FROM public.geo_observation_events WHERE processing_state='rejected' "
        "AND updated_at > NOW()-INTERVAL '24 hours'",
        (), MAX_REJECTED_EVENTS_24H,
    )
    stuck = _capped_count(
        cur,
        "SELECT 1 FROM public.geo_observation_events WHERE processing_state='processing' "
        "AND lease_until IS NOT NULL AND lease_until < NOW()",
        (), MAX_STUCK_EVENTS,
    )

    tenant_violations = _capped_count(
        cur,
        "SELECT 1 FROM public.geo_observation_events e "
        "LEFT JOIN diagnosis_runs dr ON e.source_type='paid_diagnosis' "
        "AND dr.run_token=e.source_record_id "
        "LEFT JOIN monitoring_results mr ON e.source_type='recurring_monitoring' "
        "AND mr.id::text=e.source_record_id "
        "LEFT JOIN monitoring_tasks mt ON mt.id=mr.task_id "
        "LEFT JOIN brands b ON b.id=COALESCE(dr.brand_id,mt.brand_id) "
        "WHERE (e.source_type='research_round' AND "
        "(e.owner_user_id IS NOT NULL OR e.brand_id IS NOT NULL)) "
        "OR (e.source_type='paid_diagnosis' AND (dr.run_token IS NULL "
        "OR e.owner_user_id IS DISTINCT FROM dr.owner_user_id "
        "OR e.brand_id IS DISTINCT FROM dr.brand_id)) "
        "OR (e.source_type='recurring_monitoring' AND (mr.id IS NULL OR mt.id IS NULL "
        "OR e.brand_id IS DISTINCT FROM mt.brand_id "
        "OR e.owner_user_id IS DISTINCT FROM b.owner_user_id))",
        (), 0,
    )

    structural: list[str] = []
    attention: list[str] = []
    for source_type, watermark in watermarks.items():
        if watermark is None:
            structural.append(f"{source_type} terminal source watermark 不可用")
    if last_success_at is None:
        structural.append("geo_observation_reconciler 尚无成功终态")
    elif reconciler_age is None or reconciler_age > RECONCILER_MAX_AGE_SECONDS:
        structural.append(
            f"geo_observation_reconciler 最近成功已超时({reconciler_age}s > "
            f"{RECONCILER_MAX_AGE_SECONDS}s)"
        )
    if backlog > MAX_RECONCILER_BACKLOG:
        attention.append(
            f"reconciler 待导入积压 {backlog} 超阈值 {MAX_RECONCILER_BACKLOG}"
        )
    if rejected > MAX_REJECTED_EVENTS_24H:
        attention.append(
            f"近24小时 rejected 事件 {rejected} 超阈值 {MAX_REJECTED_EVENTS_24H}"
        )
    if stuck > MAX_STUCK_EVENTS:
        attention.append(f"stuck 事件 {stuck} 超阈值 {MAX_STUCK_EVENTS}")
    if tenant_violations:
        structural.append("观测账本存在来源归属/租户映射异常")

    return {
        "reconciler_last_success_at": last_success_at,
        "reconciler_backlog": backlog,
        "backlog_by_source": backlog_by_source,
        "source_watermarks": watermarks,
        "rejected_events_24h": rejected,
        "stuck_events": stuck,
        "tenant_violations": tenant_violations,
        "structural_problems": structural,
        "attention_problems": attention,
    }


def passive_platform_health(cur: Any, raw_policy: dict) -> list[dict]:
    """Derive health from imported facts; bridge mode never actively probes a vendor."""
    cur.execute(
        "SELECT platform_key, surface_key, MAX(observed_at) AS last_observed_at "
        "FROM public.geo_observation_events "
        "WHERE processing_state NOT IN ('error','withdrawn') "
        "GROUP BY platform_key,surface_key"
    )
    observed = {
        (row["platform_key"], row["surface_key"]): row["last_observed_at"]
        for row in cur.fetchall()
    }
    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    for item in (raw_policy.get("policy") or {}).get("platforms", []):
        if not item.get("enabled") or not item.get("surface_key"):
            continue
        key = (str(item["platform_key"]), str(item["surface_key"]))
        latest = observed.get(key)
        if latest is None:
            status, error_code = "unknown", "no_terminal_observation"
        else:
            if getattr(latest, "tzinfo", None) is None:
                latest = latest.replace(tzinfo=timezone.utc)
            age = max(0, int((now - latest.astimezone(timezone.utc)).total_seconds()))
            if age <= PASSIVE_HEALTHY_MAX_AGE_SECONDS:
                status, error_code = "healthy", None
            elif age <= PASSIVE_UNAVAILABLE_AGE_SECONDS:
                status, error_code = "degraded", "terminal_observation_stale"
            else:
                status, error_code = "unavailable", "terminal_observation_expired"
        rows.append({
            "platform_key": key[0],
            "provider_key": "existing_collector",
            "model_key": "source_terminal_truth",
            "surface_key": key[1],
            "status": status,
            "checked_at": now.isoformat(),
            "latency_ms": None,
            "model_revision": None,
            "error_code": error_code,
        })
    return rows


def bridge_platform_smoke_problems(health_rows: list[dict]) -> list[str]:
    """Require recent production-equivalent terminal facts for launch-critical platforms."""
    problems: list[str] = []
    for platform_key in sorted(REQUIRED_PRODUCT_SMOKE_PLATFORMS):
        rows = [row for row in health_rows if row.get("platform_key") == platform_key]
        if not rows or not any(row.get("status") == "healthy" for row in rows):
            problems.append(f"{platform_key} 生产等价 terminal smoke 尚未通过")
    return problems
