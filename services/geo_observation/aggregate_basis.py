"""Stable aggregate policy basis and refresh-completeness checks.

``policy_version`` is an audit/CAS version and changes when the final product
display bit is enabled.  It is therefore not a safe identity for metrics.  The
basis below hashes every metric-affecting policy input while normalising only
``feature_flags.product_enabled`` to false.  Aggregation, activation and reads
all use this same identity; no caller may infer ``current_version - 1``.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping


REQUIRED_AGGREGATE_SCOPES = ("private_brand", "public_industry")
REQUIRED_AGGREGATE_GRANULARITIES = ("day", "week", "month")
# Aggregation runs every 10 minutes. Three cadences allow one delayed leader
# hand-off without serving an indefinitely stale snapshot.
RUNTIME_FRESHNESS_SLA_SECONDS = 30 * 60


def _fingerprint(items: list[object]) -> str:
    payload = json.dumps(
        items, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _snapshot_integrity_problems(
    cur: Any,
    *,
    row: Mapping[str, Any],
    policy_basis_hash: str,
    contract_version: str,
    aggregation_version: str,
    metric_version: str,
    label: str,
) -> list[str]:
    """Compare the manifest with the exact input-cell and output-key sets."""
    scope_type = str(row["scope_type"])
    start = datetime.combine(row["bucket_start"], time.min, tzinfo=timezone.utc)
    end = datetime.combine(
        row["bucket_end"] + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    if scope_type == "private_brand":
        cell_select = (
            "e.owner_user_id AS owner_user_id,e.brand_id AS brand_id,"
            "COALESCE(s.industry_key,e.industry_key) AS industry_key"
        )
        scope_predicate = "e.owner_user_id IS NOT NULL AND e.brand_id IS NOT NULL"
    else:
        cell_select = "COALESCE(s.industry_key,e.industry_key) AS industry_key"
        scope_predicate = "(e.source_type='research_round' OR b.event_id IS NOT NULL)"
    cur.execute(
        "SELECT DISTINCT " + cell_select + " FROM public.geo_observation_signals s "
        "JOIN public.geo_observation_events e ON e.id=s.event_id "
        "LEFT JOIN public.geo_observation_contributor_buckets b ON b.event_id=e.id "
        "WHERE e.processing_state='promoted' "
        "AND (e.retention_until IS NULL OR e.retention_until > NOW()) "
        "AND e.observed_at>=%s "
        "AND e.observed_at<%s AND e.promotion_seq<=%s "
        "AND s.target_outcome NOT IN ('entity_ambiguous','engine_error') AND "
        + scope_predicate,
        (start, end, int(row["promotion_sequence_watermark"])),
    )
    if scope_type == "private_brand":
        cells = sorted(
            [
                [int(item["owner_user_id"]), int(item["brand_id"]), str(item["industry_key"])]
                for item in cur.fetchall()
            ],
            key=lambda item: json.dumps(item, ensure_ascii=False),
        )
    else:
        cells = sorted(
            [[str(item["industry_key"])] for item in cur.fetchall()],
            key=lambda item: json.dumps(item, ensure_ascii=False),
        )
    cur.execute(
        "SELECT aggregate_key FROM public.geo_observation_aggregates "
        "WHERE policy_basis_hash=%s AND contract_version=%s "
        "AND aggregation_version=%s AND metric_version=%s AND scope_type=%s "
        "AND bucket_granularity=%s AND bucket_start=%s ORDER BY aggregate_key",
        (
            policy_basis_hash,
            contract_version,
            aggregation_version,
            metric_version,
            scope_type,
            row["bucket_granularity"],
            row["bucket_start"],
        ),
    )
    aggregate_keys = [str(item["aggregate_key"]) for item in cur.fetchall()]
    problems: list[str] = []
    if int(row["expected_scope_cell_count"] or 0) != len(cells):
        problems.append(f"聚合 manifest expected scope-cell count 漂移: {label}")
    if str(row["expected_scope_cell_fingerprint"]) != _fingerprint(cells):
        problems.append(f"聚合 manifest expected scope-cell fingerprint 漂移: {label}")
    if int(row["overall_cell_count"] or 0) != len(cells):
        problems.append(f"聚合 manifest overall cell 集不完整: {label}")
    if int(row["aggregate_row_count"] or 0) != len(aggregate_keys):
        problems.append(f"聚合 manifest aggregate output count 漂移: {label}")
    if str(row["aggregate_key_fingerprint"]) != _fingerprint(aggregate_keys):
        problems.append(f"聚合 manifest aggregate output fingerprint 漂移: {label}")
    return problems


def canonical_policy_basis(policy: Mapping[str, Any]) -> str:
    """Return the immutable metric-basis hash for a validated policy payload."""
    canonical = deepcopy(dict(policy))
    flags = canonical.get("feature_flags")
    if not isinstance(flags, dict):
        raise ValueError("policy.feature_flags 缺失")
    flags["product_enabled"] = False
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def current_bucket_bounds(granularity: str, today: date) -> tuple[date, date]:
    if granularity == "day":
        return today, today
    if granularity == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if granularity == "month":
        if today.month == 12:
            next_month = date(today.year + 1, 1, 1)
        else:
            next_month = date(today.year, today.month + 1, 1)
        return today.replace(day=1), next_month - timedelta(days=1)
    raise ValueError(f"unknown granularity: {granularity!r}")


def refresh_manifest_problems(
    cur: Any,
    *,
    policy_basis_hash: str,
    contract_version: str,
    aggregation_version: str,
    metric_version: str,
    today: date,
) -> list[str]:
    """Require all six current scope/granularity refresh terminal receipts."""
    cur.execute(
        "SELECT COUNT(*) AS n FROM public.geo_observation_events "
        "WHERE processing_state='promoted' AND retention_until IS NOT NULL "
        "AND retention_until<=NOW()"
    )
    overdue = int(cur.fetchone()["n"] or 0)
    if overdue:
        return [f"存在 {overdue} 条已过保留期但尚未撤回的观测"]
    cur.execute(
        """
        SELECT scope_type,bucket_granularity,bucket_start,bucket_end,
               input_watermark,eligibility_epoch,promotion_sequence_watermark,
               eligible_observation_count,expected_scope_cell_count,
               expected_scope_cell_fingerprint,aggregate_key_fingerprint,
               overall_cell_count,aggregate_row_count
          FROM public.geo_observation_aggregate_refresh_manifest
         WHERE policy_basis_hash=%s AND contract_version=%s
           AND aggregation_version=%s AND metric_version=%s
        """,
        (policy_basis_hash, contract_version, aggregation_version, metric_version),
    )
    rows = {
        (str(row["scope_type"]), str(row["bucket_granularity"]), row["bucket_start"]): row
        for row in cur.fetchall()
    }
    cur.execute(
        "SELECT scope_type,bucket_granularity,bucket_start,epoch,dirty "
        "FROM public.geo_observation_aggregate_bucket_revision"
    )
    current_revisions = {
        (str(row["scope_type"]), str(row["bucket_granularity"]), row["bucket_start"]):
            (int(row["epoch"]), bool(row["dirty"]))
        for row in cur.fetchall()
    }
    cur.execute(
        "SELECT COALESCE(MAX(promotion_seq),0) AS seq FROM public.geo_observation_events "
        "WHERE processing_state='promoted' "
        "AND (retention_until IS NULL OR retention_until>NOW())"
    )
    live_promotion_sequence = int(cur.fetchone()["seq"] or 0)
    problems: list[str] = []
    for scope_type in REQUIRED_AGGREGATE_SCOPES:
        for granularity in REQUIRED_AGGREGATE_GRANULARITIES:
            bucket_start, bucket_end = current_bucket_bounds(granularity, today)
            row = rows.get((scope_type, granularity, bucket_start))
            label = f"{scope_type}/{granularity}/{bucket_start}"
            if row is None:
                problems.append(f"聚合 refresh manifest 缺失: {label}")
                continue
            if row["bucket_end"] != bucket_end:
                problems.append(f"聚合 refresh manifest 桶边界错误: {label}")
            if row["input_watermark"] is None:
                problems.append(f"聚合 refresh manifest watermark 缺失: {label}")
            revision = current_revisions.get((scope_type, granularity, bucket_start))
            if revision is None:
                problems.append(f"聚合 refresh bucket revision 缺失: {label}")
                continue
            if revision[1]:
                problems.append(f"聚合 refresh bucket 仍为 dirty: {label}")
            if int(row["eligibility_epoch"] or 0) != revision[0]:
                problems.append(f"聚合 refresh manifest eligibility epoch 落后: {label}")
            if int(row["overall_cell_count"] or 0) <= 0:
                problems.append(f"聚合 refresh manifest 无 overall cell: {label}")
            if int(row["promotion_sequence_watermark"] or 0) != live_promotion_sequence:
                problems.append(f"聚合 refresh manifest promotion sequence 落后: {label}")
            start = datetime.combine(bucket_start, time.min, tzinfo=timezone.utc)
            end = datetime.combine(bucket_end + timedelta(days=1), time.min, tzinfo=timezone.utc)
            if scope_type == "private_brand":
                scope_predicate = (
                    "e.owner_user_id IS NOT NULL AND e.brand_id IS NOT NULL"
                )
            else:
                scope_predicate = (
                    "(e.source_type='research_round' OR b.event_id IS NOT NULL)"
                )
            cur.execute(
                "SELECT MAX(e.observed_at) AS watermark,COUNT(*) AS n "
                "FROM public.geo_observation_signals s "
                "JOIN public.geo_observation_events e ON e.id=s.event_id "
                "LEFT JOIN public.geo_observation_contributor_buckets b ON b.event_id=e.id "
                "WHERE e.processing_state='promoted' "
                "AND (e.retention_until IS NULL OR e.retention_until>NOW()) "
                "AND e.observed_at>=%s "
                "AND e.observed_at<%s AND " + scope_predicate,
                (start, end),
            )
            current = cur.fetchone()
            current_watermark = current["watermark"]
            current_count = int(current["n"] or 0)
            if row["input_watermark"] != current_watermark:
                problems.append(f"聚合 refresh manifest watermark 落后/漂移: {label}")
            if int(row["eligible_observation_count"] or 0) != current_count:
                problems.append(f"聚合 refresh manifest 输入计数落后/漂移: {label}")
            problems.extend(_snapshot_integrity_problems(
                cur,
                row=row,
                policy_basis_hash=policy_basis_hash,
                contract_version=contract_version,
                aggregation_version=aggregation_version,
                metric_version=metric_version,
                label=label,
            ))
    return problems


def runtime_manifest_problems(
    cur: Any,
    *,
    policy_basis_hash: str,
    contract_version: str,
    aggregation_version: str,
    metric_version: str,
    now: datetime,
    freshness_sla_seconds: int = RUNTIME_FRESHNESS_SLA_SECONDS,
) -> list[str]:
    """Validate the latest completed immutable snapshots without chasing head.

    Positive appends wait for the next refresh and do not interrupt reads. A
    removal/withdrawal bumps a durable eligibility epoch, immediately making
    old snapshots ineligible until a refresh absorbs that epoch. At UTC
    rollover the latest complete previous bucket remains valid inside the SLA.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    problems: list[str] = []
    # Retention is tenant/scope-specific. A single overdue customer event must
    # not turn this global six-cell control-plane probe into a whole-product
    # outage; request-local private/public fences enforce the exact input set.
    today = now.date()
    for scope_type in REQUIRED_AGGREGATE_SCOPES:
        for granularity in REQUIRED_AGGREGATE_GRANULARITIES:
            bucket_start, _ = current_bucket_bounds(granularity, today)
            cur.execute(
                """SELECT dirty FROM public.geo_observation_aggregate_bucket_revision
                    WHERE scope_type=%s AND bucket_granularity=%s AND bucket_start=%s""",
                (scope_type, granularity, bucket_start),
            )
            revision = cur.fetchone()
            if revision is not None and bool(revision["dirty"]):
                problems.append(
                    f"聚合 runtime current bucket 待刷新: {scope_type}/{granularity}/{bucket_start}"
                )
    cur.execute(
        """
        SELECT DISTINCT ON (m.scope_type,m.bucket_granularity)
               m.scope_type,m.bucket_granularity,m.bucket_start,m.bucket_end,
               m.input_watermark,m.eligibility_epoch,m.promotion_sequence_watermark,
               m.expected_scope_cell_count,m.expected_scope_cell_fingerprint,
               m.aggregate_key_fingerprint,m.overall_cell_count,m.aggregate_row_count,
               m.completed_at
          FROM public.geo_observation_aggregate_refresh_manifest m
          JOIN public.geo_observation_aggregate_bucket_revision r
            ON r.scope_type=m.scope_type
           AND r.bucket_granularity=m.bucket_granularity
           AND r.bucket_start=m.bucket_start
         WHERE m.policy_basis_hash=%s AND m.contract_version=%s
           AND m.aggregation_version=%s AND m.metric_version=%s
           AND m.eligibility_epoch=r.epoch
           AND r.dirty=FALSE AND r.published_receipt IS NOT NULL
           AND r.published_receipt->>'manifest_key'=m.manifest_key
           AND r.published_receipt->>'policy_basis_hash'=m.policy_basis_hash
           AND r.published_receipt->>'contract_version'=m.contract_version
           AND r.published_receipt->>'aggregation_version'=m.aggregation_version
           AND r.published_receipt->>'metric_version'=m.metric_version
           AND r.published_receipt->>'scope_type'=m.scope_type
           AND r.published_receipt->>'bucket_granularity'=m.bucket_granularity
           AND (r.published_receipt->>'bucket_start')::date=m.bucket_start
           AND (r.published_receipt->>'eligibility_epoch')::bigint=m.eligibility_epoch
           AND (r.published_receipt->>'promotion_sequence_watermark')::bigint=m.promotion_sequence_watermark
           AND (r.published_receipt->>'input_watermark')::timestamptz=m.input_watermark
           AND jsonb_array_length(COALESCE(r.published_receipt->'cells','[]'::jsonb))>0
           AND m.input_watermark IS NOT NULL AND m.overall_cell_count>0
           AND m.expected_scope_cell_count>0 AND m.aggregate_row_count>0
         ORDER BY m.scope_type,m.bucket_granularity,m.bucket_start DESC,m.completed_at DESC
        """,
        (policy_basis_hash, contract_version, aggregation_version, metric_version),
    )
    rows = {
        (str(row["scope_type"]), str(row["bucket_granularity"])): row
        for row in cur.fetchall()
    }
    for scope_type in REQUIRED_AGGREGATE_SCOPES:
        for granularity in REQUIRED_AGGREGATE_GRANULARITIES:
            label = f"{scope_type}/{granularity}"
            row = rows.get((scope_type, granularity))
            if row is None:
                problems.append(f"聚合 runtime manifest 缺失: {label}")
                continue
            expected_start, expected_end = current_bucket_bounds(
                granularity, row["bucket_start"]
            )
            if row["bucket_start"] != expected_start or row["bucket_end"] != expected_end:
                problems.append(f"聚合 runtime manifest 桶边界错误: {label}")
            if row["input_watermark"] is None or int(row["overall_cell_count"] or 0) <= 0:
                problems.append(f"聚合 runtime manifest 非完整终态: {label}")
            completed_at = row["completed_at"]
            if completed_at is None:
                problems.append(f"聚合 runtime manifest 完成时间缺失: {label}")
            else:
                if completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=timezone.utc)
                completed_at = completed_at.astimezone(timezone.utc)
                # The refresh job also recomputes the previous day/week/month.
                # That maintenance write must not make an old bucket look fresh
                # indefinitely.  Once a bucket is closed, its freshness clock is
                # capped at the UTC bucket boundary; only the immediately adjacent
                # rollover window can reuse it.
                bucket_closed_at = datetime.combine(
                    row["bucket_end"] + timedelta(days=1),
                    time.min,
                    tzinfo=timezone.utc,
                )
                freshness_anchor = min(completed_at, bucket_closed_at)
                age = (now - freshness_anchor).total_seconds()
                if age < -60:
                    problems.append(f"聚合 runtime manifest 完成时间在未来: {label}")
                elif age > freshness_sla_seconds:
                    problems.append(f"聚合 runtime manifest 超过 freshness SLA: {label}")
            problems.extend(_snapshot_integrity_problems(
                cur,
                row=row,
                policy_basis_hash=policy_basis_hash,
                contract_version=contract_version,
                aggregation_version=aggregation_version,
                metric_version=metric_version,
                label=label,
            ))
    return problems
