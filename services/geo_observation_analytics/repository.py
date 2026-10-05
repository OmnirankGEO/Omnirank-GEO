"""Strongly-typed read repository for the product API.

All reads go through :func:`db.connection.get_connection` in production; every
query is parameterized and, for eligibility freshness, re-checks the underlying
event ``processing_state='promoted'`` where per-observation data is returned
(so a just-withdrawn observation never surfaces even before the next aggregate
recompute).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from psycopg2.extras import Json, RealDictCursor

from .contract import AGGREGATION_VERSION, CONTRACT_VERSION, METRIC_VERSION


def _public_eligible_predicate(event_alias: str, bucket_alias: str) -> str:
    """One SQL SSOT for inputs that contribute to public-industry aggregates."""
    if event_alias not in {"e", "overdue"} or bucket_alias not in {"b", "ob"}:
        raise ValueError("unsupported observation SQL alias")
    return (
        f"({event_alias}.source_type='research_round' OR EXISTS ("
        f"SELECT 1 FROM public.geo_observation_contributor_buckets {bucket_alias} "
        f"WHERE {bucket_alias}.event_id={event_alias}.id))"
    )


_PUBLIC_OVERDUE_ELIGIBLE = _public_eligible_predicate("overdue", "ob")
_PUBLIC_EVENT_ELIGIBLE = _public_eligible_predicate("e", "b")

# Read only the CURRENT code lineage so an older/newer metric or aggregation
# version cannot be silently returned and mislabeled as the current caliber.
# policy_version is audit-only here. Every aggregate read is additionally bound
# to the immutable activation basis; rows from another basis never compete by
# freshness and are never used as fallback.
_VERSION_PREDICATE = (
    "contract_version=%s AND aggregation_version=%s AND metric_version=%s "
    "AND eligibility_epoch=(SELECT r.epoch "
    "FROM public.geo_observation_aggregate_bucket_revision r "
    "WHERE r.scope_type=public.geo_observation_aggregates.scope_type "
    "AND r.bucket_granularity=public.geo_observation_aggregates.bucket_granularity "
    "AND r.bucket_start=public.geo_observation_aggregates.bucket_start) "
    "AND (SELECT r.dirty "
    "FROM public.geo_observation_aggregate_bucket_revision r "
    "WHERE r.scope_type=public.geo_observation_aggregates.scope_type "
    "AND r.bucket_granularity=public.geo_observation_aggregates.bucket_granularity "
    "AND r.bucket_start=public.geo_observation_aggregates.bucket_start)=FALSE "
    "AND EXISTS (SELECT 1 "
    "FROM public.geo_observation_aggregate_refresh_manifest m "
    "WHERE m.policy_basis_hash=public.geo_observation_aggregates.policy_basis_hash "
    "AND m.contract_version=public.geo_observation_aggregates.contract_version "
    "AND m.aggregation_version=public.geo_observation_aggregates.aggregation_version "
    "AND m.metric_version=public.geo_observation_aggregates.metric_version "
    "AND m.scope_type=public.geo_observation_aggregates.scope_type "
    "AND m.bucket_granularity=public.geo_observation_aggregates.bucket_granularity "
    "AND m.bucket_start=public.geo_observation_aggregates.bucket_start "
    "AND m.eligibility_epoch=public.geo_observation_aggregates.eligibility_epoch "
    "AND m.promotion_sequence_watermark="
    "public.geo_observation_aggregates.promotion_sequence_watermark "
    "AND m.input_watermark=public.geo_observation_aggregates.input_watermark "
    "AND m.input_watermark IS NOT NULL AND m.overall_cell_count>0 "
    "AND m.expected_scope_cell_count>0 AND m.aggregate_row_count>0) "
    "AND NOT EXISTS (SELECT 1 FROM public.geo_observation_events overdue "
    "WHERE overdue.processing_state='promoted' "
    "AND overdue.retention_until IS NOT NULL "
    "AND overdue.retention_until<=clock_timestamp() "
    "AND overdue.observed_at >= (public.geo_observation_aggregates.bucket_start::timestamp AT TIME ZONE 'UTC') "
    "AND overdue.observed_at < ((public.geo_observation_aggregates.bucket_end + 1)::timestamp AT TIME ZONE 'UTC') "
    "AND overdue.promotion_seq<=public.geo_observation_aggregates.promotion_sequence_watermark AND ("
    "(public.geo_observation_aggregates.scope_type='private_brand' "
    " AND overdue.owner_user_id=public.geo_observation_aggregates.owner_user_id "
    " AND overdue.brand_id=public.geo_observation_aggregates.brand_id) OR "
    "(public.geo_observation_aggregates.scope_type='public_industry' "
    f" AND {_PUBLIC_OVERDUE_ELIGIBLE} "
    " AND (public.geo_observation_aggregates.industry_key IS NULL OR "
    "      COALESCE((SELECT os.industry_key "
    "        FROM public.geo_observation_signals os WHERE os.event_id=overdue.id),overdue.industry_key)="
    "      public.geo_observation_aggregates.industry_key))"
    "))"
)
_VERSION_PARAMS = [CONTRACT_VERSION, AGGREGATION_VERSION, METRIC_VERSION]
_BASIS_PREDICATE = "policy_basis_hash=%s"

# Overall aggregate = the row where every optional breakdown dimension is NULL.
_OVERALL_PREDICATE = (
    "platform_key IS NULL AND surface_key IS NULL AND source_type IS NULL "
    "AND is_branded_prompt IS NULL AND prompt_intent IS NULL "
    "AND search_enabled IS NULL AND prompt_family_key IS NULL "
    "AND model_revision IS NULL AND search_query_theme IS NULL"
)


def _fetchall(conn, sql: str, params: tuple) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _fetchone(conn, sql: str, params: tuple) -> Optional[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Brand display name (owner's own brand)
# ---------------------------------------------------------------------------
def brand_display_name(conn, brand_id: int) -> Optional[str]:
    row = _fetchone(
        conn,
        "SELECT name FROM brands WHERE id=%s AND (is_deleted IS NULL OR is_deleted=FALSE)",
        (brand_id,),
    )
    return row["name"] if row else None


def brand_ref(conn, brand_id: int) -> Optional[dict]:
    """Return {name, owner_user_id} for an active brand, else None."""
    return _fetchone(
        conn,
        "SELECT name, owner_user_id FROM brands "
        "WHERE id=%s AND (is_deleted IS NULL OR is_deleted=FALSE)",
        (brand_id,),
    )


def has_overdue_promoted_inputs(
    conn,
    *,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    public_only: bool = False,
    industry_key: Optional[str] = None,
    bucket_start: Optional[datetime] = None,
    bucket_end: Optional[datetime] = None,
    through_promotion_sequence: Optional[int] = None,
) -> bool:
    """Live retention fence for request-local product reads.

    ``clock_timestamp`` is intentional: PostgreSQL ``NOW()`` is frozen at the
    transaction start and would let an observation that expires mid-request
    remain visible for the rest of that request.
    """
    where = [
        "e.processing_state='promoted'", "e.retention_until IS NOT NULL",
        "e.retention_until<=clock_timestamp()",
    ]
    params: list[Any] = []
    if (bucket_start is None) != (bucket_end is None):
        raise ValueError("retention bucket requires start + end")
    if bucket_start is not None:
        where += ["e.observed_at>=%s", "e.observed_at<%s"]
        params += [bucket_start, bucket_end]
    if through_promotion_sequence is not None:
        where.append("e.promotion_seq<=%s")
        params.append(through_promotion_sequence)
    if owner_user_id is not None or brand_id is not None:
        if owner_user_id is None or brand_id is None:
            raise ValueError("private retention fence requires owner_user_id + brand_id")
        where += ["e.owner_user_id=%s", "e.brand_id=%s"]
        params += [owner_user_id, brand_id]
    elif public_only:
        where.append(_PUBLIC_EVENT_ELIGIBLE)
        if industry_key is not None:
            where.append(
                "COALESCE((SELECT s.industry_key FROM "
                "public.geo_observation_signals s WHERE s.event_id=e.id),e.industry_key)=%s"
            )
            params.append(industry_key)
    row = _fetchone(
        conn,
        "SELECT EXISTS(SELECT 1 FROM public.geo_observation_events e WHERE "
        + " AND ".join(where) + ") AS overdue",
        tuple(params),
    )
    return bool(row and row["overdue"])


def published_snapshot_integrity_problems(
    conn, *, policy_basis_hash: str,
) -> list[str]:
    """Cheap request-local invariant for the active published basis.

    Full-basis fingerprints belong to activation/readiness jobs.  A customer
    request only proves that at least one durable receipt still has its exact
    manifest.  The actual requested cell is validated by
    :func:`published_scope_snapshot` in a bounded number of scoped queries.
    """
    row = _fetchone(
        conn,
        """SELECT EXISTS(
                 SELECT 1
                   FROM public.geo_observation_aggregate_bucket_revision r
                   JOIN public.geo_observation_aggregate_refresh_manifest m
                     ON m.manifest_key=r.published_receipt->>'manifest_key'
                    AND m.scope_type=r.scope_type
                    AND m.bucket_granularity=r.bucket_granularity
                    AND m.bucket_start=r.bucket_start
                  WHERE r.dirty=FALSE
                    AND jsonb_typeof(r.published_receipt)='object'
                    AND r.published_receipt->>'policy_basis_hash'=%s
                    AND r.published_receipt->>'contract_version'=%s
                    AND r.published_receipt->>'aggregation_version'=%s
                    AND r.published_receipt->>'metric_version'=%s
                    AND m.policy_basis_hash=%s
                    AND m.contract_version=%s
                    AND m.aggregation_version=%s
                    AND m.metric_version=%s
                    AND m.eligibility_epoch=r.epoch
                    AND m.input_watermark IS NOT NULL
                    AND m.overall_cell_count>0
               ) AS ok""",
        (
            policy_basis_hash, CONTRACT_VERSION, AGGREGATION_VERSION,
            METRIC_VERSION, policy_basis_hash, CONTRACT_VERSION,
            AGGREGATION_VERSION, METRIC_VERSION,
        ),
    )
    return [] if row and bool(row["ok"]) else [
        "active basis 没有可验证的 published aggregate receipt"
    ]


def _canonical_watermark(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def published_scope_snapshot(
    conn,
    *,
    policy_basis_hash: str,
    scope_type: str,
    granularity: str,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    industry_key: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Return and verify the latest receipt that actually contains this cell.

    The receipt lives on the bucket revision independently of aggregate rows and
    the manifest.  Deleting both target output and its manifest therefore cannot
    be mistaken for an honest empty cell.  Only the requested cell is inspected;
    unrelated historical buckets do not create request-time N+1 work.
    """
    if scope_type == "private_brand":
        if owner_user_id is None or brand_id is None:
            raise ValueError("private snapshot requires owner_user_id + brand_id")
        cell_match = {"cells": [{
            "owner_user_id": owner_user_id, "brand_id": brand_id,
        }]}
    elif scope_type == "public_industry":
        if not industry_key:
            raise ValueError("public snapshot requires industry_key")
        cell_match = {"cells": [{"industry_key": industry_key}]}
    else:
        raise ValueError("unsupported observation scope")

    receipt_row = _fetchone(
        conn,
        """SELECT r.epoch,r.dirty,r.bucket_start,r.published_receipt
               FROM public.geo_observation_aggregate_bucket_revision r
              WHERE r.scope_type=%s AND r.bucket_granularity=%s
                AND jsonb_typeof(r.published_receipt)='object'
                AND r.published_receipt->>'policy_basis_hash'=%s
                AND r.published_receipt->>'contract_version'=%s
                AND r.published_receipt->>'aggregation_version'=%s
                AND r.published_receipt->>'metric_version'=%s
                AND r.published_receipt @> %s::jsonb
              ORDER BY r.bucket_start DESC
              LIMIT 1
              FOR SHARE OF r""",
        (
            scope_type, granularity, policy_basis_hash, CONTRACT_VERSION,
            AGGREGATION_VERSION, METRIC_VERSION, Json(cell_match),
        ),
    )
    if receipt_row is None:
        aggregate_where = [
            "policy_basis_hash=%s", "contract_version=%s",
            "aggregation_version=%s", "metric_version=%s", "scope_type=%s",
            "bucket_granularity=%s",
        ]
        aggregate_params: list[Any] = [
            policy_basis_hash, CONTRACT_VERSION, AGGREGATION_VERSION,
            METRIC_VERSION, scope_type, granularity,
        ]
        if scope_type == "private_brand":
            aggregate_where += ["owner_user_id=%s", "brand_id=%s"]
            aggregate_params += [owner_user_id, brand_id]
        else:
            aggregate_where += [
                "owner_user_id IS NULL", "brand_id IS NULL", "industry_key=%s",
            ]
            aggregate_params.append(industry_key)
        orphan = _fetchone(
            conn,
            "SELECT EXISTS(SELECT 1 FROM public.geo_observation_aggregates WHERE "
            + " AND ".join(aggregate_where) + ") AS exists",
            tuple(aggregate_params),
        )
        if orphan and bool(orphan["exists"]):
            return None, ["目标 aggregate 存在但 published receipt 缺失"]
        return None, []

    receipt = receipt_row.get("published_receipt")
    if not isinstance(receipt, dict):
        return None, ["published receipt 不是对象"]
    problems: list[str] = []
    if bool(receipt_row["dirty"]):
        problems.append("目标聚合 bucket revision dirty")
    if int(receipt_row["epoch"]) != int(receipt.get("eligibility_epoch", -1)):
        problems.append("目标聚合 bucket revision epoch 漂移")

    manifest = _fetchone(
        conn,
        """SELECT *
               FROM public.geo_observation_aggregate_refresh_manifest
              WHERE manifest_key=%s AND policy_basis_hash=%s
                AND contract_version=%s AND aggregation_version=%s
                AND metric_version=%s AND scope_type=%s
                AND bucket_granularity=%s AND bucket_start=%s""",
        (
            receipt.get("manifest_key"), policy_basis_hash, CONTRACT_VERSION,
            AGGREGATION_VERSION, METRIC_VERSION, scope_type, granularity,
            receipt_row["bucket_start"],
        ),
    )
    if manifest is None:
        return receipt, problems + ["目标 published manifest 缺失"]

    exact_pairs = (
        ("bucket_start", manifest["bucket_start"].isoformat()),
        ("bucket_end", manifest["bucket_end"].isoformat()),
        ("eligibility_epoch", int(manifest["eligibility_epoch"])),
        ("promotion_sequence_watermark", int(manifest["promotion_sequence_watermark"])),
    )
    for key, expected in exact_pairs:
        actual = receipt.get(key)
        if key in {"eligibility_epoch", "promotion_sequence_watermark"}:
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                pass
        if actual != expected:
            problems.append(f"published receipt {key} 与 manifest 漂移")
    if _canonical_watermark(receipt.get("input_watermark")) != _canonical_watermark(
        manifest["input_watermark"]
    ):
        problems.append("published receipt input_watermark 与 manifest 漂移")

    cells = receipt.get("cells")
    matching_cells: list[dict] = []
    if isinstance(cells, list):
        for candidate in cells:
            if not isinstance(candidate, dict):
                continue
            if scope_type == "private_brand":
                matches = (
                    candidate.get("owner_user_id") == owner_user_id
                    and candidate.get("brand_id") == brand_id
                )
            else:
                matches = candidate.get("industry_key") == industry_key
            if matches:
                matching_cells.append(candidate)
    if not matching_cells:
        return receipt, problems + ["published receipt 目标 scope-cell 缺失"]
    if len(matching_cells) != 1:
        return receipt, problems + ["published receipt 目标 scope-cell 重复/歧义"]
    cell = matching_cells[0]
    identity_where = [
        "policy_basis_hash=%s", "contract_version=%s",
        "aggregation_version=%s", "metric_version=%s", "scope_type=%s",
        "bucket_granularity=%s", "bucket_start=%s",
    ]
    identity_params: list[Any] = [
        policy_basis_hash, CONTRACT_VERSION, AGGREGATION_VERSION,
        METRIC_VERSION, scope_type, granularity, receipt_row["bucket_start"],
    ]
    if scope_type == "private_brand":
        identity_where += ["owner_user_id=%s", "brand_id=%s"]
        identity_params += [owner_user_id, brand_id]
    else:
        identity_where += ["owner_user_id IS NULL", "brand_id IS NULL", "industry_key=%s"]
        identity_params.append(industry_key)
    output_rows = _fetchall(
        conn,
        "SELECT * FROM public.geo_observation_aggregates WHERE "
        + " AND ".join(identity_where) + " ORDER BY aggregate_key",
        tuple(identity_params),
    )
    expected_keys = sorted(str(key) for key in cell.get("aggregate_keys", []))
    actual_keys = sorted(str(row["aggregate_key"]) for row in output_rows)
    if actual_keys != expected_keys:
        problems.append("目标 aggregate output 集与 published receipt 不一致")

    from .aggregates import compute_aggregate_key

    receipt_watermark = _canonical_watermark(receipt.get("input_watermark"))
    for row in output_rows:
        if int(row["eligibility_epoch"]) != int(receipt.get("eligibility_epoch", -1)):
            problems.append("目标 aggregate eligibility_epoch 漂移")
            break
        if int(row["promotion_sequence_watermark"]) != int(
            receipt.get("promotion_sequence_watermark", -1)
        ):
            problems.append("目标 aggregate promotion watermark 漂移")
            break
        if _canonical_watermark(row.get("input_watermark")) != receipt_watermark:
            problems.append("目标 aggregate input watermark 漂移")
            break
        if compute_aggregate_key(row) != str(row["aggregate_key"]):
            problems.append("目标 aggregate identity/key 漂移")
            break

    start = datetime.combine(manifest["bucket_start"], time.min, tzinfo=timezone.utc)
    end = datetime.combine(
        manifest["bucket_end"] + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    if has_overdue_promoted_inputs(
        conn,
        owner_user_id=owner_user_id,
        brand_id=brand_id,
        public_only=(scope_type == "public_industry"),
        industry_key=industry_key,
        bucket_start=start,
        bucket_end=end,
        through_promotion_sequence=int(manifest["promotion_sequence_watermark"]),
    ):
        problems.append("目标 published snapshot 含已过 retention 输入")
    return receipt, problems

def platform_surface_pairs(
    conn,
    *,
    scope_type: str,
    bucket_start: date,
    bucket_end: date,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    industry_key: Optional[str] = None,
) -> list[dict]:
    """Distinct (platform_key, surface_key) among promoted signals in the window
    — used to attach honest surface notes to per-platform product rows."""
    from datetime import time, timedelta, timezone as _tz, datetime as _dt

    start = _dt.combine(bucket_start, time.min, tzinfo=_tz.utc)
    end = _dt.combine(bucket_end + timedelta(days=1), time.min, tzinfo=_tz.utc)
    where = [
        "e.processing_state='promoted'",
        "(e.retention_until IS NULL OR e.retention_until > clock_timestamp())",
        "e.observed_at>=%s", "e.observed_at<%s",
    ]
    params: list[Any] = [start, end]
    if scope_type == "private_brand":
        where += ["e.owner_user_id=%s", "e.brand_id=%s"]
        params += [owner_user_id, brand_id]
    else:
        where.append(_PUBLIC_EVENT_ELIGIBLE)
    if industry_key is not None:
        where.append("COALESCE(s.industry_key, e.industry_key)=%s")
        params.append(industry_key)
    sql = (
        "SELECT DISTINCT COALESCE(s.platform_key, e.platform_key) AS platform_key, "
        "COALESCE(s.surface_key, e.surface_key) AS surface_key "
        "FROM public.geo_observation_signals s JOIN public.geo_observation_events e ON e.id=s.event_id "
        "WHERE " + " AND ".join(where)
    )
    return _fetchall(conn, sql, tuple(params))


def historical_platforms(
    conn,
    *,
    scope_type: str,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    industry_key: Optional[str] = None,
    platform_keys: tuple[str, ...] = ("kimi",),
    through_watermark: datetime,
    through_promotion_sequence: int,
) -> list[dict]:
    where = [
        "e.processing_state='promoted'",
        "(e.retention_until IS NULL OR e.retention_until > clock_timestamp())",
        "COALESCE(s.platform_key, e.platform_key) = ANY(%s)",
        "e.observed_at <= %s",
        "e.promotion_seq <= %s",
    ]
    params: list[Any] = [list(platform_keys), through_watermark, through_promotion_sequence]
    if scope_type == "private_brand":
        where += ["e.owner_user_id=%s", "e.brand_id=%s"]
        params += [owner_user_id, brand_id]
    else:
        where.append(_PUBLIC_EVENT_ELIGIBLE)
    if industry_key is not None:
        where.append("COALESCE(s.industry_key, e.industry_key)=%s")
        params.append(industry_key)
    sql = (
        "SELECT COALESCE(s.platform_key, e.platform_key) AS platform_key, "
        "MAX(e.observed_at) AS last_observed_at "
        "FROM public.geo_observation_signals s JOIN public.geo_observation_events e ON e.id=s.event_id "
        "WHERE " + " AND ".join(where) + " GROUP BY 1 ORDER BY 1"
    )
    return _fetchall(conn, sql, tuple(params))


def recent_observation_refs(
    conn, owner_user_id: int, brand_id: int, *, through_watermark: datetime,
    through_promotion_sequence: int,
    limit: int = 10,
) -> list[str]:
    rows = _fetchall(
        conn,
        """SELECT e.event_uuid::text AS observation_id
           FROM public.geo_observation_events e
           JOIN public.geo_observation_signals s ON s.event_id=e.id
           WHERE e.processing_state='promoted' AND e.owner_user_id=%s AND e.brand_id=%s
             AND (e.retention_until IS NULL OR e.retention_until > clock_timestamp())
             AND e.observed_at <= %s
             AND e.promotion_seq <= %s
             AND s.target_outcome NOT IN ('entity_ambiguous','engine_error')
           ORDER BY e.observed_at DESC, e.id DESC LIMIT %s""",
        (owner_user_id, brand_id, through_watermark, through_promotion_sequence, limit),
    )
    return [r["observation_id"] for r in rows]


# ---------------------------------------------------------------------------
# Overall aggregate for a scope-cell
# ---------------------------------------------------------------------------
def latest_overall_aggregate(
    conn,
    *,
    scope_type: str,
    granularity: str,
    policy_basis_hash: str,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    industry_key: Optional[str] = None,
    as_of: Optional[date] = None,
) -> Optional[dict]:
    where = ["scope_type=%s", "bucket_granularity=%s", _OVERALL_PREDICATE,
             _VERSION_PREDICATE, _BASIS_PREDICATE]
    params: list[Any] = [scope_type, granularity, *_VERSION_PARAMS, policy_basis_hash]
    if scope_type == "private_brand":
        where += ["owner_user_id=%s", "brand_id=%s"]
        params += [owner_user_id, brand_id]
    else:
        where += ["owner_user_id IS NULL", "brand_id IS NULL"]
    if industry_key is not None:
        where.append("industry_key=%s")
        params.append(industry_key)
    if as_of is not None:
        where.append("bucket_start<=%s")
        params.append(as_of)
    sql = (
        "SELECT * FROM public.geo_observation_aggregates WHERE "
        + " AND ".join(where)
        + " ORDER BY bucket_start DESC, computed_at DESC LIMIT 1"
    )
    return _fetchone(conn, sql, tuple(params))


def overall_aggregate_for_bucket(
    conn,
    *,
    scope_type: str,
    granularity: str,
    bucket_start: date,
    policy_basis_hash: str,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    industry_key: Optional[str] = None,
) -> Optional[dict]:
    where = ["scope_type=%s", "bucket_granularity=%s", "bucket_start=%s",
             _OVERALL_PREDICATE, _VERSION_PREDICATE, _BASIS_PREDICATE]
    params: list[Any] = [scope_type, granularity, bucket_start, *_VERSION_PARAMS, policy_basis_hash]
    if scope_type == "private_brand":
        where += ["owner_user_id=%s", "brand_id=%s"]
        params += [owner_user_id, brand_id]
    else:
        where += ["owner_user_id IS NULL", "brand_id IS NULL"]
    if industry_key is not None:
        where.append("industry_key=%s")
        params.append(industry_key)
    sql = ("SELECT * FROM public.geo_observation_aggregates WHERE " + " AND ".join(where)
           + " ORDER BY computed_at DESC LIMIT 1")
    return _fetchone(conn, sql, tuple(params))


def previous_overall_aggregate(
    conn,
    *,
    scope_type: str,
    granularity: str,
    before_bucket_start: date,
    policy_basis_hash: str,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    industry_key: Optional[str] = None,
) -> Optional[dict]:
    where = ["scope_type=%s", "bucket_granularity=%s", "bucket_start<%s",
             _OVERALL_PREDICATE, _VERSION_PREDICATE, _BASIS_PREDICATE]
    params: list[Any] = [scope_type, granularity, before_bucket_start,
                         *_VERSION_PARAMS, policy_basis_hash]
    if scope_type == "private_brand":
        where += ["owner_user_id=%s", "brand_id=%s"]
        params += [owner_user_id, brand_id]
    else:
        where += ["owner_user_id IS NULL", "brand_id IS NULL"]
    if industry_key is not None:
        where.append("industry_key=%s")
        params.append(industry_key)
    sql = (
        "SELECT * FROM public.geo_observation_aggregates WHERE "
        + " AND ".join(where)
        + " ORDER BY bucket_start DESC, computed_at DESC LIMIT 1"
    )
    return _fetchone(conn, sql, tuple(params))


def trend_series(
    conn,
    *,
    scope_type: str,
    granularity: str,
    policy_basis_hash: str,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    industry_key: Optional[str] = None,
    limit: int = 60,
) -> list[dict]:
    where = ["scope_type=%s", "bucket_granularity=%s", _OVERALL_PREDICATE,
             _VERSION_PREDICATE, _BASIS_PREDICATE]
    params: list[Any] = [scope_type, granularity, *_VERSION_PARAMS, policy_basis_hash]
    if scope_type == "private_brand":
        where += ["owner_user_id=%s", "brand_id=%s"]
        params += [owner_user_id, brand_id]
    else:
        where += ["owner_user_id IS NULL", "brand_id IS NULL"]
    if industry_key is not None:
        where.append("industry_key=%s")
        params.append(industry_key)
    # DISTINCT ON (bucket_start) picks the FRESHEST row per bucket (so a same-date
    # multi-version write never yields two points), take the NEWEST `limit`
    # buckets, then present ascending (series[-1] is always the most recent).
    sql = (
        "SELECT * FROM (SELECT DISTINCT ON (bucket_start) * FROM public.geo_observation_aggregates WHERE "
        + " AND ".join(where)
        + " ORDER BY bucket_start DESC, computed_at DESC LIMIT %s) t ORDER BY bucket_start ASC"
    )
    params.append(limit)
    return _fetchall(conn, sql, tuple(params))


def platform_aggregates_for_bucket(
    conn,
    *,
    scope_type: str,
    granularity: str,
    bucket_start: date,
    policy_basis_hash: str,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    industry_key: Optional[str] = None,
) -> list[dict]:
    # per-platform rows = platform_key set, all OTHER breakdown dims NULL
    where = [
        "scope_type=%s", "bucket_granularity=%s", "bucket_start=%s", _VERSION_PREDICATE,
        _BASIS_PREDICATE,
        "platform_key IS NOT NULL", "surface_key IS NULL", "source_type IS NULL",
        "is_branded_prompt IS NULL", "prompt_intent IS NULL", "search_enabled IS NULL",
        "prompt_family_key IS NULL", "model_revision IS NULL", "search_query_theme IS NULL",
    ]
    params: list[Any] = [scope_type, granularity, bucket_start,
                         *_VERSION_PARAMS, policy_basis_hash]
    if scope_type == "private_brand":
        where += ["owner_user_id=%s", "brand_id=%s"]
        params += [owner_user_id, brand_id]
    else:
        where += ["owner_user_id IS NULL", "brand_id IS NULL"]
    if industry_key is not None:
        where.append("industry_key=%s")
        params.append(industry_key)
    sql = (
        "SELECT DISTINCT ON (platform_key) * FROM public.geo_observation_aggregates WHERE "
        + " AND ".join(where)
        + " ORDER BY platform_key ASC, computed_at DESC"
    )
    return _fetchall(conn, sql, tuple(params))


# ---------------------------------------------------------------------------
# Per-observation questions (owner's own promoted data)
# ---------------------------------------------------------------------------
_QUESTIONS_SQL = """
WITH ordered AS (
    SELECT
        e.event_uuid::text        AS observation_id,
        e.id                      AS event_id,
        s.target_outcome          AS outcome,
        s.target_position         AS position,
        s.citation_count          AS citations,
        (s.citation_count > 0 OR s.quality_score_bps >= 5000) AS evidence_available,
        s.platform_key            AS platform_key,
        s.surface_key             AS surface_key,
        e.observed_at             AS observed_at,
        LAG(s.target_outcome) OVER (
            PARTITION BY COALESCE(s.prompt_family_key, '')
            ORDER BY e.observed_at, e.id
        ) AS prev_outcome
    FROM public.geo_observation_signals s
    JOIN public.geo_observation_events e ON e.id = s.event_id
    WHERE e.processing_state = 'promoted'
      AND (e.retention_until IS NULL OR e.retention_until > clock_timestamp())
      AND e.owner_user_id = %s
      AND e.brand_id = %s
      AND e.observed_at <= %s
      AND e.promotion_seq <= %s
)
SELECT observation_id, event_id, outcome, position, citations, evidence_available,
       platform_key, surface_key, observed_at,
       (prev_outcome IS NOT NULL AND prev_outcome <> outcome) AS changed
FROM ordered
ORDER BY observed_at DESC, event_id DESC
LIMIT %s OFFSET %s
"""

_QUESTIONS_COUNT_SQL = """
SELECT COUNT(*) AS n
FROM public.geo_observation_signals s
JOIN public.geo_observation_events e ON e.id = s.event_id
WHERE e.processing_state = 'promoted' AND e.owner_user_id = %s AND e.brand_id = %s
  AND (e.retention_until IS NULL OR e.retention_until > clock_timestamp())
  AND e.observed_at <= %s
  AND e.promotion_seq <= %s
"""


def count_brand_questions(
    conn, owner_user_id: int, brand_id: int, *, through_watermark,
    through_promotion_sequence: int,
) -> int:
    row = _fetchone(
        conn, _QUESTIONS_COUNT_SQL,
        (owner_user_id, brand_id, through_watermark, through_promotion_sequence),
    )
    return int(row["n"]) if row else 0


def list_brand_questions(
    conn, owner_user_id: int, brand_id: int, page: int, page_size: int,
    *, through_watermark, through_promotion_sequence: int,
) -> list[dict]:
    offset = (page - 1) * page_size
    return _fetchall(
        conn, _QUESTIONS_SQL,
        (owner_user_id, brand_id, through_watermark, through_promotion_sequence,
         page_size, offset),
    )


def get_observation_meta(
    conn, owner_user_id: int, brand_id: int, observation_id: str,
    *, through_watermark, through_promotion_sequence: int,
) -> Optional[dict]:
    """Resolve an opaque observation_id (event_uuid) to its signal meta, scoped
    to the owner's own brand — never returns another tenant's row."""
    return _fetchone(
        conn,
        """
        SELECT e.event_uuid::text AS observation_id, e.id AS event_id,
               e.source_type, e.source_table, e.source_record_id, e.source_subkey,
               s.target_outcome AS outcome, s.target_position AS position,
               s.citation_count AS citations, s.source_domains AS source_domains,
               s.platform_key AS platform_key, s.surface_key AS surface_key,
               (s.citation_count > 0 OR s.quality_score_bps >= 5000) AS evidence_available
        FROM public.geo_observation_events e
        JOIN public.geo_observation_signals s ON s.event_id = e.id
        WHERE e.processing_state='promoted'
          AND (e.retention_until IS NULL OR e.retention_until > clock_timestamp())
          AND e.owner_user_id=%s AND e.brand_id=%s
          AND e.event_uuid::text=%s
          AND e.observed_at <= %s
          AND e.promotion_seq <= %s
        """,
        (owner_user_id, brand_id, observation_id, through_watermark,
         through_promotion_sequence),
    )


# ---------------------------------------------------------------------------
# Admin overview inputs (computed from events/aggregates I own)
# ---------------------------------------------------------------------------
def event_state_counts(conn) -> dict[str, int]:
    rows = _fetchall(
        conn,
        "SELECT processing_state, COUNT(*) AS n FROM public.geo_observation_events "
        "GROUP BY processing_state",
        (),
    )
    return {r["processing_state"]: int(r["n"]) for r in rows}


def stuck_claim_count(conn) -> int:
    """Events leased past their lease_until. AI-2's real migration HAS
    lease_until, so a query failure here means a schema/permission/DB anomaly and
    MUST surface (the caller drives readiness to 'unavailable') — it is NOT
    folded into a fake 0. Raises on error; never swallows."""
    row = _fetchone(
        conn,
        "SELECT COUNT(*) AS n FROM public.geo_observation_events "
        "WHERE processing_state='processing' AND lease_until IS NOT NULL "
        "AND lease_until < NOW()",
        (),
    )
    return int(row["n"]) if row else 0


def reconciler_delay_seconds(conn) -> int:
    row = _fetchone(
        conn,
        "SELECT COALESCE(EXTRACT(EPOCH FROM (NOW() - MIN(created_at)))::bigint, 0) AS delay "
        "FROM public.geo_observation_events WHERE processing_state IN ('pending','processing')",
        (),
    )
    return int(row["delay"]) if row and row["delay"] is not None else 0


def latest_aggregate_computed_at(conn, *, policy_basis_hash: str) -> Optional[str]:
    row = _fetchone(
        conn,
        "SELECT MAX(computed_at) AS t FROM public.geo_observation_aggregates "
        "WHERE policy_basis_hash=%s AND " + _VERSION_PREDICATE,
        (policy_basis_hash, *_VERSION_PARAMS),
    )
    if row and row["t"] is not None:
        return row["t"].isoformat()
    return None


def industry_source_patterns(
    conn,
    *,
    industry_key: str,
    bucket_start: date,
    bucket_end: date,
    min_user_buckets: int,
    min_brand_buckets: int,
    min_source_types: int,
    through_watermark: datetime,
    through_promotion_sequence: int,
) -> list[dict]:
    """Aggregate normalized source domains for a public industry window, with
    PER-DOMAIN k-anonymity: a domain is only returned when it was contributed by
    at least ``min_user_buckets`` distinct users, ``min_brand_buckets`` distinct
    brands, and ``min_source_types`` distinct source types. A single client
    repeating a domain can never push it into the public API (differential
    re-identification defense). The domain object's ``type`` field (citation vs
    source) is honored per AI-2's promotion contract.
    """
    from datetime import time, timedelta, timezone as _tz, datetime as _dt

    start = _dt.combine(bucket_start, time.min, tzinfo=_tz.utc)
    end = _dt.combine(bucket_end + timedelta(days=1), time.min, tzinfo=_tz.utc)
    return _fetchall(
        conn,
        """
        SELECT dom.domain AS domain,
               COALESCE(dom."type", 'source') AS source_type,
               COUNT(*) AS appearance_count,
               COUNT(DISTINCT COALESCE(s.platform_key, e.platform_key)) AS platforms,
               COUNT(DISTINCT b.contributor_user_bucket)  AS user_buckets,
               COUNT(DISTINCT b.contributor_brand_bucket) AS brand_buckets,
               COUNT(DISTINCT e.source_type)              AS source_types
        FROM public.geo_observation_signals s
        JOIN public.geo_observation_events e ON e.id = s.event_id
        LEFT JOIN public.geo_observation_contributor_buckets b ON b.event_id = e.id
        CROSS JOIN LATERAL jsonb_to_recordset(s.source_domains)
            AS dom(domain text, "type" text)
        WHERE e.processing_state='promoted'
          AND (e.retention_until IS NULL OR e.retention_until > clock_timestamp())
          AND COALESCE(s.industry_key, e.industry_key)=%s
          AND e.observed_at>=%s AND e.observed_at<%s
          AND e.observed_at<=%s
          AND e.promotion_seq<=%s
          AND dom.domain IS NOT NULL
        GROUP BY dom.domain, COALESCE(dom."type", 'source')
        HAVING COUNT(DISTINCT b.contributor_user_bucket)  >= %s
           AND COUNT(DISTINCT b.contributor_brand_bucket) >= %s
           AND COUNT(DISTINCT e.source_type)              >= %s
        ORDER BY appearance_count DESC, domain ASC
        LIMIT 50
        """,
        (
            industry_key, start, end, through_watermark, through_promotion_sequence,
            min_user_buckets, min_brand_buckets, min_source_types,
        ),
    )


def list_public_industries(
    conn, *, granularity: str, policy_basis_hash: str, limit: int = 100
) -> list[str]:
    rows = _fetchall(
        conn,
        "SELECT DISTINCT industry_key FROM public.geo_observation_aggregates "
        "WHERE scope_type='public_industry' AND bucket_granularity=%s "
        "AND policy_basis_hash=%s AND " + _VERSION_PREDICATE + " "
        "ORDER BY industry_key LIMIT %s",
        (granularity, policy_basis_hash, *_VERSION_PARAMS, limit),
    )
    return [r["industry_key"] for r in rows]


def model_shift_rows(
    conn, *, threshold_bps: int, policy_basis_hash: str, limit: int = 50
) -> list[dict]:
    # Same lineage discipline as every other read: filter to the current
    # contract/aggregation/metric versions and keep the FRESHEST row per logical
    # model-shift cell (scope, industry, platform, model_revision, bucket) so a
    # policy CAS-bump or metric-version bump cannot surface duplicate/stale-caliber
    # rows in the admin /model-shifts view.
    return _fetchall(
        conn,
        """
        SELECT * FROM (
          SELECT DISTINCT ON (scope_type, industry_key, COALESCE(platform_key,''),
                              bucket_granularity, bucket_start)
                 industry_key, platform_key, model_revision, bucket_granularity,
                 bucket_start, model_shift_index_bps, stability_status, computed_at
          FROM public.geo_observation_aggregates
          WHERE """ + _VERSION_PREDICATE + """
            AND policy_basis_hash=%s
            -- industry-level governance view only: public_industry rows have
            -- owner/brand NULL, so (scope,industry,platform,granularity,bucket)
            -- fully identifies the logical cell (no per-tenant collapse/leak).
            AND scope_type = 'public_industry'
            -- only the OVERALL (all breakdown dims NULL) and per-PLATFORM rows;
            -- exclude sub-segment breakdowns (surface/source/branded/intent/search)
            -- which also have platform_key NULL and would otherwise be mislabeled
            -- as the industry-wide shift (same overall DISTINCT ON group).
            AND surface_key IS NULL AND source_type IS NULL
            AND is_branded_prompt IS NULL AND prompt_intent IS NULL
            AND search_enabled IS NULL AND prompt_family_key IS NULL
            AND model_revision IS NULL AND search_query_theme IS NULL
          -- bucket_granularity is part of the logical cell identity (day/week/month
          -- share a bucket_start on the 1st / Mondays); keep it in the key so a
          -- coarser-granularity shift is never silently dropped.
          ORDER BY scope_type, industry_key, COALESCE(platform_key,''),
                   bucket_granularity, bucket_start, computed_at DESC
        ) t
        -- threshold applied AFTER the freshest-per-cell dedup, so a resolved
        -- (freshest, below-threshold) cell is not represented by a stale
        -- prior-policy_version row that happens to still be above threshold.
        WHERE t.model_shift_index_bps >= %s
        ORDER BY bucket_start DESC, model_shift_index_bps DESC
        LIMIT %s
        """,
        (*_VERSION_PARAMS, policy_basis_hash, threshold_bps, limit),
    )
