"""Dedicated ADMIN governance, presentation-safe demo grants and downgrade planning.

This module deliberately contains no generic patch primitive.  Commercial
relationships, wallets, inventory and settlements remain owned by their
existing services; downgrade planning only proves that each dependency reached
an allowed terminal state before calling the existing identity transition.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

from psycopg2.extras import Json

from db.connection import get_db


class CrossTenantNotFound(Exception):
    pass


class CrossTenantConflict(Exception):
    def __init__(self, code: str, message: str, *, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class CrossTenantValidation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _row(value: Any) -> dict:
    return dict(value or {})


def _active(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "inactive", "disabled"}
    return bool(value)


def _json_default(value: Any) -> str:
    """Keep audit snapshots lossless enough to inspect and always JSON-safe."""
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    raise TypeError(f"Unsupported audit value: {type(value).__name__}")


def _audit_json(value: Optional[dict]) -> Json:
    return Json(
        value or {},
        dumps=lambda item: json.dumps(item, default=_json_default, ensure_ascii=False),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, default=_json_default, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    )


def _immutable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _audit(
    cur,
    *,
    actor_user_id: int,
    actor_username: Optional[str],
    action: str,
    subject_kind: str,
    subject_id: Optional[int],
    request_id: str,
    reason: str,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    event_hash = _immutable_hash({
        "actor_user_id": int(actor_user_id),
        "action": action,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "reason": reason.strip(),
        "before": before or {},
        "after": after or {},
    })
    cur.execute(
        """INSERT INTO admin_cross_tenant_audits(
               actor_user_id,actor_username,action,subject_kind,subject_id,
               request_id,event_hash,reason,before_snapshot,after_snapshot,ip_address
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(request_id) DO NOTHING RETURNING id""",
        (
            int(actor_user_id), actor_username, action, subject_kind, subject_id,
            request_id[:180], event_hash, reason.strip(), _audit_json(before),
            _audit_json(after), ip_address,
        ),
    )
    if cur.fetchone() is not None:
        return
    cur.execute(
        """SELECT actor_user_id,action,subject_kind,subject_id,event_hash
           FROM admin_cross_tenant_audits WHERE request_id=%s FOR SHARE""",
        (request_id[:180],),
    )
    existing = cur.fetchone()
    exact_replay = bool(
        existing
        and int(existing["actor_user_id"]) == int(actor_user_id)
        and existing["action"] == action
        and existing["subject_kind"] == subject_kind
        and existing.get("subject_id") == subject_id
        and existing["event_hash"] == event_hash
    )
    if not exact_replay:
        raise CrossTenantConflict(
            "AUDIT_REQUEST_ID_COLLISION",
            "请求编号已绑定不同治理审计；业务事务已整体拒绝",
        )


def safe_search_audit(search: Optional[str]) -> dict[str, str]:
    """Return non-reversible audit metadata; never persist a phone/search term."""
    normalized = (search or "").strip().casefold()
    if not normalized:
        return {"search_kind": "none", "search_hash": ""}
    digits = "".join(character for character in normalized if character.isdigit())
    if normalized.isdigit():
        kind = "id" if len(normalized) <= 6 else "phone"
    elif "@" in normalized:
        kind = "email"
    elif len(digits) >= 7:
        kind = "phone"
    else:
        kind = "text"
    return {"search_kind": kind, "search_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest()}


def record_admin_read(
    *, actor_user_id: int, actor_username: Optional[str], action: str,
    subject_kind: str, subject_id: Optional[int], request_id: str,
    reason: str, before: Optional[dict], after: Optional[dict],
    ip_address: Optional[str],
) -> None:
    with get_db() as conn:
        _audit(
            conn.cursor(), actor_user_id=actor_user_id,
            actor_username=actor_username, action=action,
            subject_kind=subject_kind, subject_id=subject_id,
            request_id=request_id, reason=reason, before=before, after=after,
            ip_address=ip_address,
        )


def list_cross_tenant_audits(
    *, page: int, page_size: int, subject_kind: Optional[str] = None,
    subject_id: Optional[int] = None,
) -> dict:
    page = max(1, int(page)); page_size = min(100, max(10, int(page_size)))
    where: list[str] = []
    params: list[Any] = []
    if subject_kind:
        where.append("subject_kind=%s"); params.append(subject_kind)
    if subject_id is not None:
        where.append("subject_id=%s"); params.append(int(subject_id))
    clause = "WHERE " + " AND ".join(where) if where else ""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS count FROM admin_cross_tenant_audits {clause}", params)
        total = int(cur.fetchone()["count"])
        cur.execute(
            f"""SELECT id,actor_user_id,actor_username,action,subject_kind,subject_id,
                       request_id,reason,before_snapshot,after_snapshot,ip_address,created_at
                FROM admin_cross_tenant_audits {clause}
                ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s""",
            params + [page_size, (page - 1) * page_size],
        )
        return {"success": True, "audits": [_row(row) for row in cur.fetchall() or []],
                "total": total, "page": page, "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size}


def _lock_subject_version(cur, subject_kind: str, subject_id: int, capability: str, expected: int) -> int:
    cur.execute(
        """INSERT INTO admin_governance_subject_versions(subject_kind,subject_id,capability,version)
           VALUES (%s,%s,%s,1) ON CONFLICT(subject_kind,subject_id,capability) DO NOTHING""",
        (subject_kind, int(subject_id), capability),
    )
    cur.execute(
        """SELECT version FROM admin_governance_subject_versions
           WHERE subject_kind=%s AND subject_id=%s AND capability=%s FOR UPDATE""",
        (subject_kind, int(subject_id), capability),
    )
    current = int(cur.fetchone()["version"])
    if current != int(expected):
        raise CrossTenantConflict(
            "GOVERNANCE_VERSION_CONFLICT", "治理对象已被其他管理员更新",
            details={"current_version": current, "capability": capability},
        )
    return current


def _advance_subject_version(cur, subject_kind: str, subject_id: int, capability: str, current: int) -> int:
    next_version = int(current) + 1
    cur.execute(
        """UPDATE admin_governance_subject_versions SET version=%s,updated_at=NOW()
           WHERE subject_kind=%s AND subject_id=%s AND capability=%s AND version=%s""",
        (next_version, subject_kind, int(subject_id), capability, int(current)),
    )
    if cur.rowcount != 1:
        raise CrossTenantConflict("GOVERNANCE_VERSION_CONFLICT", "治理版本更新冲突")
    return next_version


def change_account_status(
    user_id: int, *, active: bool, expected_version: int, reason: str,
    actor_user_id: int, actor_username: Optional[str], request_id: str,
    ip_address: Optional[str],
) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        # Serialize every active-admin census before locking the target.  Two
        # administrators disabling each other can no longer both observe 2.
        cur.execute("SELECT pg_advisory_xact_lock(920716,1)")
        cur.execute(
            "SELECT id,username,is_active,permission_version FROM users WHERE id=%s FOR UPDATE",
            (int(user_id),),
        )
        user = cur.fetchone()
        if not user:
            raise CrossTenantNotFound()
        if int(user_id) == int(actor_user_id) and not active:
            raise CrossTenantValidation("SELF_ACCOUNT_DISABLE", "管理员不能停用当前登录账号")
        current = _lock_subject_version(cur, "user", user_id, "account_status", expected_version)
        before_active = _active(user.get("is_active"))
        if before_active == bool(active):
            raise CrossTenantConflict("NO_CHANGE", "账号状态没有变化")
        cur.execute(
            """SELECT EXISTS(
                 SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                 WHERE ur.user_id=%s AND r.name='admin'
               ) AS is_admin""",
            (int(user_id),),
        )
        is_admin = bool(cur.fetchone()["is_admin"])
        if is_admin and not active:
            cur.execute(
                """SELECT COUNT(*) AS count FROM users u
                   WHERE COALESCE(u.is_active,0)<>0 AND EXISTS(
                     SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                     WHERE ur.user_id=u.id AND r.name='admin'
                   )"""
            )
            if int(cur.fetchone()["count"]) <= 1:
                raise CrossTenantConflict("LAST_ACTIVE_ADMIN", "不能停用最后一个可用平台管理员")
        cur.execute(
            """UPDATE users SET is_active=%s,
                      permission_version=COALESCE(permission_version,1)+1
               WHERE id=%s RETURNING permission_version""",
            (1 if active else 0, int(user_id)),
        )
        permission_version = int(cur.fetchone()["permission_version"])
        next_version = _advance_subject_version(cur, "user", user_id, "account_status", current)
        before = {"active": before_active}
        after = {"active": bool(active)}
        _audit(
            cur, actor_user_id=actor_user_id, actor_username=actor_username,
            action="account_status.change", subject_kind="user", subject_id=user_id,
            request_id=request_id, reason=reason, before=before, after=after,
            ip_address=ip_address,
        )
        return {
            "success": True, "scope": "account_status", "version": next_version,
            "request_id": request_id, "before": before, "after": after,
            "permission_version": permission_version,
        }


# ---------------------------------------------------------------------------
# Demo customer contract v2 (Owner decision 2026-07-21)
#
# Immutable brand/case ids, frozen safe snapshots, batch grants, and one
# predicate shared by list/detail/export/URL/WS.
# ---------------------------------------------------------------------------

_SNAPSHOT_TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "quotes": (
        "id", "brand_id", "brand_name", "industry", "status", "tier", "created_at",
        "confirmed_at", "service_days", "service_start_date", "service_end_date",
    ),
    "article_generations": ("id", "brand_id", "quote_id", "status", "created_at", "completed_at", "article_count"),
    "articles": ("id", "brand_id", "quote_id", "title", "status", "created_at", "published_at"),
    "media_publications": ("id", "brand_id", "article_id", "status", "media_name", "created_at", "published_at"),
    "monitoring_tasks": (
        "id", "quote_id", "client_id", "brand_id", "status", "task_name", "platform",
        "keyword_count", "platform_count", "total_tests", "completed_tests", "trigger_type",
        "trend_synced", "created_at", "started_at", "completed_at",
    ),
    "monitoring_reports": (
        "id", "client_id", "brand_id", "status", "title", "report_type", "period_start",
        "period_end", "summary_data", "content", "overall_score", "visibility_rate",
        "created_at", "completed_at", "sent_at",
    ),
}

_MONITORING_DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "confirmed_keywords": (
        "id", "quote_id", "keyword", "target_brand", "category", "status", "target_rate",
        "is_core", "cluster_id", "cluster_name", "is_monitored", "monitoring_status",
        "archived_at", "archive_reason", "super_red_ocean", "competition_ratio", "note",
    ),
    "extra_keywords": (
        "id", "quote_id", "brand_id", "keyword", "target_brand", "difficulty", "status",
        "target_rate", "is_core", "cluster_id", "cluster_name", "is_monitored",
        "monitoring_status", "archived_at", "archive_reason", "super_red_ocean",
        "competition_ratio", "note",
    ),
    "monitoring_results": (
        "id", "task_id", "keyword_id", "confirmed_keyword_id", "keyword", "platform",
        "round_number", "is_detected", "mention_type", "response_snippet", "tested_at",
        "identity_review_state", "identity_candidates", "identity_evidence_snippet",
        "identity_evidence_hash", "identity_decision_version",
    ),
    "keyword_trend_stats": (
        "id", "keyword_id", "keyword_source", "period_type", "period_date", "test_count",
        "detected_count", "detection_rate", "rate_change",
    ),
    "media_publications": (
        "id", "quote_id", "article_id", "platform_name", "platform_url", "article_title",
        "publish_date", "created_at", "status", "status_label",
    ),
    "operation_logs": (
        "id", "brand_id", "action", "target_type", "target_id", "details", "created_at",
    ),
    "monitoring_data_archives": (
        "id", "archive_key", "brand_id", "quote_id", "created_at", "reason", "status",
    ),
    "monitoring_config": (
        "brand_id", "client_id", "default_platforms", "auto_monitor_enabled",
        "monitoring_interval_hours", "monitoring_start_hour", "service_days",
        "service_start_date", "updated_at",
    ),
}
_BRAND_SNAPSHOT_FIELDS = ("id", "name", "industry", "industry_category", "status", "brand_type", "created_at")
_DIAGNOSIS_SNAPSHOT_FIELDS = (
    "id", "brand_id", "brand_name", "industry", "total_score", "level",
    "result_visibility", "web_search_score", "platform_score",
    "content_quality_score", "authority_score", "ai_visibility_score",
    "ai_citation_score", "status", "created_at", "completed_at",
)


def _derived_request_id(request_id: str, suffix: str) -> str:
    suffix = suffix.strip(":")
    return f"{request_id[: max(1, 179 - len(suffix))]}:{suffix}"[:180]


def _safe_keys(raw: Optional[dict], allowed: tuple[str, ...]) -> dict:
    source = dict(raw or {})
    return {key: source.get(key) for key in allowed if key in source}


def _table_columns(cur, table: str) -> set[str]:
    cur.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema=current_schema() AND table_name=%s""",
        (table,),
    )
    return {str(row["column_name"]) for row in cur.fetchall() or []}


def _snapshot_rows(cur, table: str, brand_id: int, allowed: tuple[str, ...]) -> list[dict]:
    columns = _table_columns(cur, table)
    if "brand_id" not in columns:
        return []
    order = "id DESC" if "id" in columns else "brand_id"
    cur.execute(
        f'SELECT to_jsonb(t) AS raw FROM "{table}" t WHERE brand_id=%s ORDER BY {order} LIMIT 50',
        (int(brand_id),),
    )
    return [_safe_keys(row.get("raw"), allowed) for row in cur.fetchall() or []]


def _snapshot_rows_by_values(
    cur, table: str, column: str, values: list[int], allowed: tuple[str, ...], *, limit: int = 500,
) -> list[dict]:
    """Freeze an allow-listed relation slice; table/column names come only from constants."""
    columns = _table_columns(cur, table)
    if column not in columns or not values:
        return []
    order = "id DESC" if "id" in columns else column
    cur.execute(
        f'SELECT to_jsonb(t) AS raw FROM "{table}" t WHERE "{column}"=ANY(%s) '
        f'ORDER BY {order} LIMIT %s',
        (sorted({int(value) for value in values}), int(limit)),
    )
    return [_safe_keys(row.get("raw"), allowed) for row in cur.fetchall() or []]


def _monitoring_snapshot(cur, *, brand_id: int, quotes: list[dict], tasks: list[dict], reports: list[dict]) -> dict:
    quote_ids = [int(row["id"]) for row in quotes if row.get("id") is not None]
    task_ids = [int(row["id"]) for row in tasks if row.get("id") is not None]
    quotes_available = "brand_id" in _table_columns(cur, "quotes")
    tasks_available = "brand_id" in _table_columns(cur, "monitoring_tasks")
    reports_available = "brand_id" in _table_columns(cur, "monitoring_reports")
    keywords_available = all(
        "quote_id" in _table_columns(cur, table)
        for table in ("confirmed_keywords", "extra_keywords")
    )

    confirmed = _snapshot_rows_by_values(
        cur, "confirmed_keywords", "quote_id", quote_ids,
        _MONITORING_DETAIL_FIELDS["confirmed_keywords"],
    ) if keywords_available else []
    extra = _snapshot_rows_by_values(
        cur, "extra_keywords", "quote_id", quote_ids,
        _MONITORING_DETAIL_FIELDS["extra_keywords"],
    ) if keywords_available else []
    keywords = [
        {**row, "source": "confirmed", "difficulty": row.get("category")}
        for row in confirmed
    ] + [{**row, "source": "extra"} for row in extra]

    results_available = tasks_available and "task_id" in _table_columns(cur, "monitoring_results")
    results = _snapshot_rows_by_values(
        cur, "monitoring_results", "task_id", task_ids,
        _MONITORING_DETAIL_FIELDS["monitoring_results"], limit=2000,
    ) if results_available else []
    latest_details: dict[tuple[str, int, str], dict] = {}
    for row in results:
        source = "confirmed" if row.get("confirmed_keyword_id") is not None else "extra"
        keyword_id = row.get("confirmed_keyword_id") if source == "confirmed" else row.get("keyword_id")
        platform = str(row.get("platform") or "")
        if keyword_id is None or not platform:
            continue
        key = (source, int(keyword_id), platform)
        if key in latest_details:
            continue
        latest_details[key] = {
            "platform": platform,
            "is_detected": bool(row.get("is_detected")),
            "mention_type": row.get("mention_type"),
            "tested_at": row.get("tested_at"),
            "snippet": row.get("response_snippet"),
        }
    for keyword in keywords:
        source = str(keyword.get("source") or "confirmed")
        keyword_id = keyword.get("id")
        keyword["detection_details"] = [
            detail for (detail_source, detail_id, _), detail in latest_details.items()
            if detail_source == source and keyword_id is not None and detail_id == int(keyword_id)
        ]

    keyword_ids = [int(row["id"]) for row in keywords if row.get("id") is not None]
    trends_available = keywords_available and "keyword_id" in _table_columns(cur, "keyword_trend_stats")
    trends = _snapshot_rows_by_values(
        cur, "keyword_trend_stats", "keyword_id", keyword_ids,
        _MONITORING_DETAIL_FIELDS["keyword_trend_stats"], limit=2000,
    ) if trends_available else []
    publications_available = quotes_available and "quote_id" in _table_columns(cur, "media_publications")
    publications = _snapshot_rows_by_values(
        cur, "media_publications", "quote_id", quote_ids,
        _MONITORING_DETAIL_FIELDS["media_publications"],
    ) if publications_available else []
    logs_available = "brand_id" in _table_columns(cur, "operation_logs")
    logs = _snapshot_rows_by_values(
        cur, "operation_logs", "brand_id", [brand_id],
        _MONITORING_DETAIL_FIELDS["operation_logs"],
    ) if logs_available else []
    archives_available = "brand_id" in _table_columns(cur, "monitoring_data_archives")
    archives = _snapshot_rows_by_values(
        cur, "monitoring_data_archives", "brand_id", [brand_id],
        _MONITORING_DETAIL_FIELDS["monitoring_data_archives"],
    ) if archives_available else []
    config_available = "brand_id" in _table_columns(cur, "monitoring_config")
    config = _snapshot_rows_by_values(
        cur, "monitoring_config", "brand_id", [brand_id],
        _MONITORING_DETAIL_FIELDS["monitoring_config"],
    ) if config_available else []

    keyword_count_by_quote: dict[int, int] = {}
    for keyword in keywords:
        quote_id = keyword.get("quote_id")
        if quote_id is not None and keyword.get("is_core") is not False and not keyword.get("super_red_ocean"):
            keyword_count_by_quote[int(quote_id)] = keyword_count_by_quote.get(int(quote_id), 0) + 1
    clients = [{
        key: value for key, value in {
            "quote_id": quote.get("id"),
            "brand_id": quote.get("brand_id"),
            "brand_name": quote.get("brand_name"),
            "industry": quote.get("industry"),
            "status": quote.get("status"),
            "tier": quote.get("tier"),
            "keyword_count": (
                keyword_count_by_quote.get(int(quote["id"]), 0)
                if keywords_available and quote.get("id") is not None else None
            ),
            "service_days": quote.get("service_days"),
            "service_start_date": quote.get("service_start_date"),
        }.items() if value is not None
    } for quote in quotes]
    datasets = (
        ("clients", clients, quotes_available),
        ("keywords", keywords, keywords_available),
        ("results", results, results_available),
        ("trends", trends, trends_available),
        ("publications", publications, publications_available),
        ("logs", logs, logs_available),
        ("archives", archives, archives_available),
        ("config", config, config_available),
        ("tasks", tasks, tasks_available),
        ("reports", reports, reports_available),
    )
    return {key: value for key, value, available in datasets if available}


def _build_safe_demo_snapshot(cur, *, brand_id: int, diagnosis_id: int) -> dict:
    cur.execute(
        """SELECT to_jsonb(b) AS raw FROM brands b
           WHERE b.id=%s AND (b.is_deleted IS NULL OR b.is_deleted=FALSE) FOR SHARE""",
        (int(brand_id),),
    )
    brand_row = cur.fetchone()
    if not brand_row:
        raise CrossTenantNotFound()
    cur.execute("SELECT to_jsonb(d) AS raw FROM diagnosis_records d WHERE d.id=%s FOR SHARE", (int(diagnosis_id),))
    diagnosis_row = cur.fetchone()
    if not diagnosis_row:
        raise CrossTenantNotFound()
    diagnosis_raw = dict(diagnosis_row.get("raw") or {})
    if int(diagnosis_raw.get("brand_id") or 0) != int(brand_id):
        raise CrossTenantValidation("DEMO_CASE_ID_MISMATCH", "案例与客户 ID 不匹配")
    if str(diagnosis_raw.get("result_visibility") or "published") != "published":
        raise CrossTenantNotFound()
    brand = _safe_keys(brand_row.get("raw"), _BRAND_SNAPSHOT_FIELDS)
    diagnosis = _safe_keys(diagnosis_raw, _DIAGNOSIS_SNAPSHOT_FIELDS)
    artifacts = {
        table: _snapshot_rows(cur, table, brand_id, fields)
        for table, fields in _SNAPSHOT_TABLE_FIELDS.items()
    }
    quote_snapshots = artifacts.pop("quotes", [])
    quote_ids = [int(row["id"]) for row in quote_snapshots if row.get("id") is not None]
    portal_links: list[dict[str, Any]] = []
    from services.demo_access import portal_token_fingerprint
    portal_columns = _table_columns(cur, "client_access_tokens")
    if quote_ids and {"quote_id", "token", "is_active", "expires_at"}.issubset(portal_columns):
        cur.execute(
            """SELECT quote_id,token,expires_at
                FROM public.client_access_tokens
                WHERE quote_id = ANY(%s) AND COALESCE(is_active,0)<>0
                ORDER BY quote_id DESC,expires_at DESC NULLS LAST,token ASC""",
            (quote_ids,),
        )
        seen_quotes: set[int] = set()
        for raw in cur.fetchall() or []:
            quote_id = int(raw["quote_id"])
            token = str(raw.get("token") or "").strip()
            fingerprint = portal_token_fingerprint(
                token=token, quote_id=quote_id, brand_id=int(brand_id),
            )
            if quote_id in seen_quotes or not fingerprint:
                continue
            seen_quotes.add(quote_id)
            portal_links.append({
                "quote_id": quote_id,
                "token_fingerprint": fingerprint,
                "expires_at": raw.get("expires_at"),
            })
    monitoring_tasks = artifacts.pop("monitoring_tasks", [])
    monitoring_reports = artifacts.pop("monitoring_reports", [])
    monitoring_snapshot = _monitoring_snapshot(
        cur,
        brand_id=brand_id,
        quotes=quote_snapshots,
        tasks=monitoring_tasks,
        reports=monitoring_reports,
    )
    included = ["customer_overview", "diagnosis"]
    included.extend(
        dataset for dataset, table in (
            ("quotes", "quotes"),
            ("writing.generations", "article_generations"),
            ("writing.articles", "articles"),
            ("publish.publications", "media_publications"),
            ("monitoring.tasks", "monitoring_tasks"),
            ("reports", "monitoring_reports"),
        )
        if "brand_id" in _table_columns(cur, table)
    )
    return {
        "snapshot_contract": "demo-customer-safe-v5",
        "watermark": "演示案例",
        "frozen_at": datetime.now(timezone.utc),
        "included": included,
        "customer_overview": {
            "brand_id": int(brand_id),
            "brand_name": brand.get("name"),
            "industry": brand.get("industry"),
            "industry_category": brand.get("industry_category"),
            "status": brand.get("status"),
            "brand_type": brand.get("brand_type"),
        },
        "diagnosis_snapshot": diagnosis,
        "quote_snapshots": quote_snapshots,
        "portal_links": portal_links,
        "writing_snapshots": {
            "generations": artifacts.pop("article_generations", []),
            "articles": artifacts.pop("articles", []),
        },
        "publish_snapshots": artifacts.pop("media_publications", []),
        "monitoring_snapshots": monitoring_tasks,
        "report_snapshots": monitoring_reports,
        "monitoring_snapshot": monitoring_snapshot,
        "access_contract": {
            "access_mode": "demo",
            "real_commercial_authority_granted": False,
            "database_writes_allowed": False,
            "provider_calls_allowed": False,
            "billing_allowed": False,
            "background_jobs_allowed": False,
            "public_token_issuance_allowed": False,
            "raw_export_allowed": False,
        },
    }


def _ensure_demo_case(
    cur, *, brand_id: int, diagnosis_id: int, case_id: str,
    actor_user_id: int, actor_username: Optional[str], reason: str,
    request_id: str, ip_address: Optional[str],
) -> dict:
    from services.demo_access import stable_demo_case_id
    expected = stable_demo_case_id(brand_id, diagnosis_id)
    if str(case_id).lower() != expected:
        raise CrossTenantValidation("DEMO_CASE_ID_MISMATCH", "case_id 与 brand_id/diagnosis_id 不匹配")
    cur.execute("SELECT * FROM admin_demo_cases WHERE case_id=%s FOR UPDATE", (expected,))
    existing = cur.fetchone()
    if existing:
        if int(existing["brand_id"]) != int(brand_id) or int(existing["diagnosis_id"]) != int(diagnosis_id):
            raise CrossTenantConflict("DEMO_CASE_ID_CONFLICT", "案例 ID 已绑定不同来源")
        if existing["status"] != "active":
            raise CrossTenantConflict("DEMO_CASE_RETIRED", "演示案例已停用")
        return dict(existing)
    from services.demo_access import project_safe_snapshot
    snapshot = project_safe_snapshot(
        _build_safe_demo_snapshot(cur, brand_id=brand_id, diagnosis_id=diagnosis_id),
    )
    case_request_id = _derived_request_id(request_id, "case")
    cur.execute(
        """INSERT INTO admin_demo_cases(
               case_id,brand_id,diagnosis_id,safe_snapshot,created_by_user_id,
               created_reason,created_request_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (
            expected, int(brand_id), int(diagnosis_id), _audit_json(snapshot),
            int(actor_user_id), reason, case_request_id,
        ),
    )
    created = dict(cur.fetchone())
    _audit(
        cur, actor_user_id=actor_user_id, actor_username=actor_username,
        action="demo_case.freeze", subject_kind="demo_case", subject_id=brand_id,
        request_id=case_request_id, reason=reason, before={},
        after={
            "case_id": expected, "brand_id": int(brand_id),
            "diagnosis_id": int(diagnosis_id), "snapshot_contract": "demo-customer-safe-v5",
        }, ip_address=ip_address,
    )
    return created


def list_demo_case_catalog(*, page: int, page_size: int, search: Optional[str]) -> dict:
    from services.demo_access import stable_demo_case_id
    page = max(1, int(page)); page_size = min(100, max(10, int(page_size)))
    where = "WHERE (b.is_deleted IS NULL OR b.is_deleted=FALSE)"
    params: list[Any] = []
    if search and search.strip():
        where += " AND (b.name ILIKE %s OR COALESCE(b.industry,'') ILIKE %s OR COALESCE(u.display_name,u.username,'') ILIKE %s OR b.id::text=%s)"
        needle = f"%{search.strip()}%"
        params.extend([needle, needle, needle, search.strip()])
    lateral = """JOIN LATERAL (
        SELECT d.id,d.created_at FROM diagnosis_records d
        WHERE d.brand_id=b.id AND COALESCE(d.result_visibility,'published')='published'
        ORDER BY d.created_at DESC,d.id DESC LIMIT 1
    ) d ON TRUE"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS count FROM brands b LEFT JOIN users u ON u.id=b.owner_user_id {lateral} {where}", params)
        total = int(cur.fetchone()["count"])
        cur.execute(
            f"""SELECT b.id AS brand_id,b.name AS brand_name,b.industry,b.owner_user_id,
                       COALESCE(u.display_name,u.username,'账号#'||b.owner_user_id::text) AS owner_label,
                       d.id AS diagnosis_id,d.created_at AS diagnosis_created_at,c.status AS case_status
                FROM brands b LEFT JOIN users u ON u.id=b.owner_user_id {lateral}
                LEFT JOIN admin_demo_cases c ON c.brand_id=b.id AND c.diagnosis_id=d.id
                {where} ORDER BY d.created_at DESC,b.id DESC LIMIT %s OFFSET %s""",
            params + [page_size, (page - 1) * page_size],
        )
        cases = []
        for raw in cur.fetchall() or []:
            row = dict(raw)
            row["case_id"] = stable_demo_case_id(row["brand_id"], row["diagnosis_id"])
            row["frozen"] = row.get("case_status") == "active"
            cases.append(row)
        return {"success": True, "cases": cases, "total": total, "page": page,
                "page_size": page_size, "total_pages": (total + page_size - 1) // page_size}


def _grant_dict(row: dict) -> dict:
    now = datetime.now(timezone.utc)
    expires_at = row["expires_at"]
    effective_status = "expired" if row["status"] == "active" and expires_at <= now else row["status"]
    snapshot = dict(row.get("safe_snapshot") or {})
    overview = dict(snapshot.get("customer_overview") or {})
    return {
        "id": int(row["id"]), "case_id": str(row["case_id"]),
        "brand_id": int(row["brand_id"]),
        "diagnosis_id": int(row.get("diagnosis_id") or snapshot.get("diagnosis_snapshot", {}).get("id") or 0),
        "case_label": overview.get("brand_name"), "industry": overview.get("industry"),
        "grantee_kind": row["grantee_kind"], "grantee_user_id": row.get("grantee_user_id"),
        "grantee_organization_id": row.get("grantee_organization_id"),
        "grantee_label": row.get("grantee_label"), "capability": row["capability"],
        "status": effective_status, "valid_from": row["valid_from"], "expires_at": expires_at,
        "version": int(row["version"]), "note": row.get("note") or "",
        "created_at": row["created_at"], "revoked_at": row.get("revoked_at"),
    }


def _expire_matching_grants(
    cur, *, grantee_kind: str, grantee_id: int, case_id: str,
    actor_user_id: int, actor_username: Optional[str], ip_address: Optional[str],
) -> None:
    column = "grantee_user_id" if grantee_kind == "user" else "grantee_organization_id"
    cur.execute(
        f"""SELECT * FROM admin_demo_case_grants WHERE grantee_kind=%s AND {column}=%s
              AND case_id=%s AND status='active' AND expires_at<=NOW() FOR UPDATE""",
        (grantee_kind, int(grantee_id), case_id),
    )
    for old in cur.fetchall() or []:
        cur.execute("UPDATE admin_demo_case_grants SET status='expired',expired_at=NOW(),version=version+1,updated_at=NOW() WHERE id=%s", (int(old["id"]),))
        _audit(
            cur, actor_user_id=actor_user_id, actor_username=actor_username,
            action="demo_grant.expire", subject_kind="demo_grant", subject_id=int(old["id"]),
            request_id=f"demo-expire:{old['id']}:{old['version']}", reason="演示授权有效期已届满",
            before={"status": "active", "version": int(old["version"])},
            after={"status": "expired", "version": int(old["version"]) + 1}, ip_address=ip_address,
        )


def create_demo_case_grants_batch(
    *, grantee_kind: str, grantee_user_id: Optional[int], grantee_organization_id: Optional[int],
    selections: list[dict], expires_at: datetime, note: str, reason: str,
    actor_user_id: int, actor_username: Optional[str], request_id: str,
    ip_address: Optional[str],
) -> dict:
    if not selections or len(selections) > 100:
        raise CrossTenantValidation("DEMO_CASE_SELECTION_REQUIRED", "请选择 1-100 个演示客户")
    grantee_id = int(grantee_user_id or grantee_organization_id or 0)
    if grantee_kind not in {"user", "organization"} or not grantee_id:
        raise CrossTenantValidation("DEMO_GRANTEE_INVALID", "授权对象无效")
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expires_at = expires_at.astimezone(timezone.utc)
    normalized_by_case: dict[str, dict[str, Any]] = {}
    for selection in selections:
        normalized = {
            "case_id": str(selection["case_id"]).lower(),
            "brand_id": int(selection["brand_id"]),
            "diagnosis_id": int(selection["diagnosis_id"]),
        }
        prior = normalized_by_case.get(normalized["case_id"])
        if prior and prior != normalized:
            raise CrossTenantValidation("DEMO_CASE_DUPLICATE_CONFLICT", "同一 case_id 绑定了不同来源")
        normalized_by_case[normalized["case_id"]] = normalized
    normalized_selections = sorted(
        normalized_by_case.values(),
        key=lambda item: (item["case_id"], item["brand_id"], item["diagnosis_id"]),
    )
    request_payload_hash = _immutable_hash({
        "grantee_kind": grantee_kind,
        "grantee_id": grantee_id,
        "capability": "demo.customer.preview",
        "selections": normalized_selections,
        "expires_at": expires_at,
        "note": note,
        "reason": reason,
    })
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"demo-grant:{grantee_kind}:{grantee_id}",))
        if grantee_kind == "user":
            cur.execute("SELECT display_name,username,is_active FROM users WHERE id=%s FOR UPDATE", (grantee_id,))
            grantee = cur.fetchone()
            if not grantee or not _active(grantee.get("is_active")):
                raise CrossTenantNotFound()
        else:
            cur.execute("SELECT name,status FROM organizations WHERE id=%s FOR UPDATE", (grantee_id,))
            grantee = cur.fetchone()
            if not grantee or grantee["status"] != "active":
                raise CrossTenantNotFound()
        created: list[dict] = []
        for index, selection in enumerate(normalized_selections):
            brand_id = int(selection["brand_id"]); diagnosis_id = int(selection["diagnosis_id"])
            case_id = str(selection["case_id"])
            item_request_id = _derived_request_id(request_id, f"grant-{index}")
            cur.execute(
                """SELECT g.*,c.diagnosis_id,c.safe_snapshot
                   FROM admin_demo_case_grants g
                   JOIN admin_demo_cases c ON c.case_id=g.case_id
                   WHERE g.created_request_id=%s""",
                (item_request_id,),
            )
            replay = cur.fetchone()
            if replay:
                if (
                    replay.get("request_payload_hash") != request_payload_hash
                    or str(replay["case_id"]) != case_id
                    or int(replay["brand_id"]) != brand_id
                    or int(replay["diagnosis_id"]) != diagnosis_id
                    or replay["grantee_kind"] != grantee_kind
                    or int(replay.get("grantee_user_id") or replay.get("grantee_organization_id") or 0) != grantee_id
                ):
                    raise CrossTenantConflict("IDEMPOTENCY_CONFLICT", "请求编号已用于不同演示授权")
                created.append(_grant_dict(dict(replay)))
                continue
            case = _ensure_demo_case(
                cur, brand_id=brand_id, diagnosis_id=diagnosis_id, case_id=case_id,
                actor_user_id=actor_user_id, actor_username=actor_username, reason=reason,
                request_id=item_request_id, ip_address=ip_address,
            )
            _expire_matching_grants(
                cur, grantee_kind=grantee_kind, grantee_id=grantee_id, case_id=case_id,
                actor_user_id=actor_user_id, actor_username=actor_username, ip_address=ip_address,
            )
            column = "grantee_user_id" if grantee_kind == "user" else "grantee_organization_id"
            cur.execute(
                f"""SELECT id FROM admin_demo_case_grants WHERE grantee_kind=%s AND {column}=%s
                      AND case_id=%s AND capability='demo.customer.preview' AND status='active' FOR UPDATE""",
                (grantee_kind, grantee_id, case_id),
            )
            if cur.fetchone():
                raise CrossTenantConflict("DEMO_GRANT_ALREADY_ACTIVE", "所选客户已有生效中的演示授权")
            cur.execute(
                f"""INSERT INTO admin_demo_case_grants(
                       grantee_kind,{column},case_id,brand_id,capability,expires_at,note,
                       request_payload_hash,created_by_user_id,created_reason,created_request_id)
                   VALUES (%s,%s,%s,%s,'demo.customer.preview',%s,%s,%s,%s,%s,%s) RETURNING *""",
                (grantee_kind, grantee_id, case_id, brand_id, expires_at, note,
                 request_payload_hash, int(actor_user_id), reason, item_request_id),
            )
            grant = dict(cur.fetchone()); grant["safe_snapshot"] = case["safe_snapshot"]
            grant["diagnosis_id"] = diagnosis_id
            _audit(
                cur, actor_user_id=actor_user_id, actor_username=actor_username,
                action="demo_grant.create", subject_kind="demo_grant", subject_id=int(grant["id"]),
                request_id=item_request_id, reason=reason, before={}, after={
                    "grantee_kind": grantee_kind, "grantee_id": grantee_id,
                    "case_id": case_id, "brand_id": brand_id, "diagnosis_id": diagnosis_id,
                    "capability": "demo.customer.preview", "expires_at": expires_at,
                    "commercial_assets_granted": False, "database_write_capability_granted": False,
                }, ip_address=ip_address,
            )
            created.append(_grant_dict(grant))
        return {"success": True, "grants": created, "count": len(created)}


def create_demo_case_grant(
    *, grantee_kind: str, grantee_user_id: Optional[int], grantee_organization_id: Optional[int],
    brand_id: int, diagnosis_id: int, case_id: str, capability: str,
    expires_at: datetime, reason: str, actor_user_id: int,
    actor_username: Optional[str], request_id: str, ip_address: Optional[str],
    note: str = "",
) -> dict:
    if capability != "demo.customer.preview":
        raise CrossTenantValidation("DEMO_CAPABILITY_INVALID", "演示客户 capability 固定为 demo.customer.preview")
    result = create_demo_case_grants_batch(
        grantee_kind=grantee_kind, grantee_user_id=grantee_user_id,
        grantee_organization_id=grantee_organization_id,
        selections=[{"brand_id": brand_id, "diagnosis_id": diagnosis_id, "case_id": case_id}],
        expires_at=expires_at, note=note, reason=reason, actor_user_id=actor_user_id,
        actor_username=actor_username, request_id=request_id, ip_address=ip_address,
    )
    return {"success": True, "grant": result["grants"][0], "replayed": False}


def list_demo_case_grants(*, page: int, page_size: int, grantee_kind: Optional[str] = None, grantee_id: Optional[int] = None) -> dict:
    page = max(1, int(page)); page_size = min(100, max(10, int(page_size)))
    where: list[str] = []; params: list[Any] = []
    if grantee_kind:
        where.append("g.grantee_kind=%s"); params.append(grantee_kind)
    if grantee_id:
        column = "g.grantee_user_id" if grantee_kind == "user" else "g.grantee_organization_id"
        where.append(f"{column}=%s"); params.append(int(grantee_id))
    clause = "WHERE " + " AND ".join(where) if where else ""
    with get_db() as conn:
        cur = conn.cursor(); cur.execute(f"SELECT COUNT(*) AS count FROM admin_demo_case_grants g {clause}", params)
        total = int(cur.fetchone()["count"])
        cur.execute(
            f"""SELECT g.*,c.diagnosis_id,c.safe_snapshot,
                       CASE WHEN g.grantee_kind='user' THEN COALESCE(u.display_name,u.username) ELSE o.name END AS grantee_label
                FROM admin_demo_case_grants g JOIN admin_demo_cases c ON c.case_id=g.case_id
                LEFT JOIN users u ON u.id=g.grantee_user_id LEFT JOIN organizations o ON o.id=g.grantee_organization_id
                {clause} ORDER BY g.created_at DESC,g.id DESC LIMIT %s OFFSET %s""",
            params + [page_size, (page - 1) * page_size],
        )
        return {"success": True, "grants": [_grant_dict(dict(row)) for row in cur.fetchall() or []],
                "total": total, "page": page, "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size}


def _revoke_demo_case_grant_locked(
    cur, grant_id: int, *, expected_version: int, reason: str,
    actor_user_id: int, actor_username: Optional[str], request_id: str,
    ip_address: Optional[str],
) -> dict:
    cur.execute(
        """SELECT g.*,c.diagnosis_id,c.safe_snapshot
           FROM admin_demo_case_grants g
           JOIN admin_demo_cases c ON c.case_id=g.case_id
           WHERE g.id=%s FOR UPDATE OF g""",
        (int(grant_id),),
    )
    grant = cur.fetchone()
    if not grant:
        raise CrossTenantNotFound()
    if grant.get("revoked_request_id") == request_id:
        return {"success": True, "grant": _grant_dict(dict(grant)), "replayed": True}
    if int(grant["version"]) != int(expected_version):
        raise CrossTenantConflict(
            "GOVERNANCE_VERSION_CONFLICT", "演示授权已被其他管理员更新",
            details={"current_version": int(grant["version"])},
        )
    if grant["status"] != "active" or grant["expires_at"] <= datetime.now(timezone.utc):
        raise CrossTenantConflict("DEMO_GRANT_NOT_ACTIVE", "演示授权已失效")
    before = {
        "status": "active", "version": int(grant["version"]),
        "expires_at": grant["expires_at"],
    }
    cur.execute(
        """UPDATE admin_demo_case_grants SET status='revoked',revoked_by_user_id=%s,
               revoked_reason=%s,revoked_request_id=%s,revoked_at=NOW(),
               version=version+1,updated_at=NOW()
           WHERE id=%s RETURNING *""",
        (int(actor_user_id), reason, request_id, int(grant_id)),
    )
    updated = dict(cur.fetchone())
    updated["safe_snapshot"] = grant["safe_snapshot"]
    updated["diagnosis_id"] = grant["diagnosis_id"]
    _audit(
        cur, actor_user_id=actor_user_id, actor_username=actor_username,
        action="demo_grant.revoke", subject_kind="demo_grant", subject_id=grant_id,
        request_id=request_id, reason=reason, before=before,
        after={
            "status": "revoked", "version": int(updated["version"]),
            "revoked_at": updated["revoked_at"],
        }, ip_address=ip_address,
    )
    return {"success": True, "grant": _grant_dict(updated), "replayed": False}


def revoke_demo_case_grant(
    grant_id: int, *, expected_version: int, reason: str, actor_user_id: int,
    actor_username: Optional[str], request_id: str, ip_address: Optional[str],
) -> dict:
    with get_db() as conn:
        return _revoke_demo_case_grant_locked(
            conn.cursor(), grant_id, expected_version=expected_version, reason=reason,
            actor_user_id=actor_user_id, actor_username=actor_username,
            request_id=request_id, ip_address=ip_address,
        )


def revoke_demo_case_grants_batch(
    *, grants: list[dict], reason: str, actor_user_id: int, actor_username: Optional[str],
    request_id: str, ip_address: Optional[str],
) -> dict:
    if not grants or len(grants) > 100:
        raise CrossTenantValidation("DEMO_GRANT_SELECTION_REQUIRED", "请选择 1-100 条授权")
    normalized = [
        (index, int(item["grant_id"]), int(item["expected_version"]))
        for index, item in enumerate(grants)
    ]
    if len({grant_id for _, grant_id, _ in normalized}) != len(normalized):
        raise CrossTenantValidation("DEMO_GRANT_DUPLICATE", "批量撤回不能包含重复授权")
    revoked_by_index: dict[int, dict] = {}
    # One transaction and deterministic lock order: a stale version in any row
    # rolls back the entire batch instead of leaving a partially revoked set.
    with get_db() as conn:
        cur = conn.cursor()
        for index, grant_id, expected_version in sorted(normalized, key=lambda item: item[1]):
            revoked_by_index[index] = _revoke_demo_case_grant_locked(
                cur, grant_id, expected_version=expected_version, reason=reason,
                actor_user_id=actor_user_id, actor_username=actor_username,
                request_id=_derived_request_id(request_id, f"revoke-{index}"),
                ip_address=ip_address,
            )["grant"]
    revoked = [revoked_by_index[index] for index in range(len(normalized))]
    return {"success": True, "grants": revoked, "count": len(revoked)}


def _case_payload(row: dict) -> dict:
    from services.demo_access import project_safe_snapshot

    snapshot = project_safe_snapshot(row.get("safe_snapshot"))
    snapshot.update({
        "case_id": str(row["case_id"]), "brand_id": int(row["brand_id"]),
        "diagnosis_id": int(row["diagnosis_id"]), "access_mode": "demo",
        "read_only": True, "presentation_preview": True,
        "demo_watermark": "演示案例",
    })
    if row.get("expires_at") is not None:
        snapshot["expires_at"] = row["expires_at"]
    return snapshot


def list_authorized_demo_cases(
    user_id: int, *, page: int, page_size: int, search: Optional[str],
    request_id: str, ip_address: Optional[str],
) -> dict:
    from services.demo_access import (
        list_live_demo_case_contexts,
        record_demo_access_event,
    )
    page = max(1, int(page)); page_size = min(100, max(10, int(page_size)))
    contexts = list_live_demo_case_contexts(int(user_id))
    if search and search.strip():
        needle = search.strip().casefold()
        contexts = [
            context for context in contexts
            if needle in str(context.snapshot.get("customer_overview", {}).get("brand_name") or "").casefold()
            or needle in str(context.snapshot.get("customer_overview", {}).get("industry") or "").casefold()
        ]
    contexts.sort(
        key=lambda context: str(context.snapshot.get("frozen_at") or ""),
        reverse=True,
    )
    total = len(contexts)
    selected = contexts[(page - 1) * page_size:page * page_size]
    cases = [_case_payload({
        "case_id": context.case_id, "brand_id": context.brand_id,
        "diagnosis_id": context.diagnosis_id, "safe_snapshot": context.snapshot,
        "expires_at": context.expires_at,
    }) for context in selected]
    for context in selected:
        record_demo_access_event(
            context, action="demo_case.list", request_id=request_id,
            ip_address=ip_address or "unknown",
        )
    return {"success": True, "cases": cases, "total": total, "page": page,
            "page_size": page_size, "total_pages": (total + page_size - 1) // page_size}


def get_authorized_demo_case(
    user_id: int, case_id: str, *, action: str, request_id: str,
    ip_address: Optional[str],
) -> dict:
    from services.demo_access import record_demo_access_event, resolve_demo_case_access
    context = resolve_demo_case_access(int(user_id), str(case_id))
    if not context:
        raise CrossTenantNotFound()
    payload = _case_payload({
        "case_id": context.case_id, "brand_id": context.brand_id,
        "diagnosis_id": context.diagnosis_id, "safe_snapshot": context.snapshot,
        "expires_at": context.expires_at,
    })
    record_demo_access_event(
        context, action=action, request_id=request_id,
        ip_address=ip_address or "unknown",
    )
    return payload


def has_authorized_demo_case(user_id: int, case_id: str) -> bool:
    from services.demo_access import resolve_demo_case_access
    return resolve_demo_case_access(int(user_id), str(case_id)) is not None


def _relation_exists(cur, table: str) -> bool:
    cur.execute(
        """SELECT c.relkind
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname='public' AND c.relname=%s""",
        (table,),
    )
    row = cur.fetchone()
    return bool(row and row.get("relkind") in {"r", "p"})


def provider_downgrade_snapshot(provider_user_id: int, *, cur=None, lock: bool = False) -> dict:
    if cur is None:
        with get_db() as conn:
            return provider_downgrade_snapshot(provider_user_id, cur=conn.cursor(), lock=lock)
    required = (
        "customer_agent_bindings", "recharge_orders", "agent_inventory_wallets",
        "channel_pricing_relationships", "customer_agent_binding_disputes", "dispute_escrow",
        "agent_revenue_ledger", "agent_settlement_requests", "agent_settlement_request_items",
        "agent_commission_redemption_requests", "agent_commission_redemption_items",
        "withdrawal_requests", "channel_revenue_ledger",
        "dealer_inventory_lots", "dealer_resale_orders", "dealer_consumer_sales",
        "dealer_resale_profit_ledger", "dealer_resale_hop_profit_ledger",
        "dealer_resale_fulfillment_plans", "dealer_resale_fulfillment_hops",
        "dealer_resale_fulfillment_allocations", "consumer_refund_cases",
        "refund_work_orders",
        "service_refund_liability_ledger", "service_refund_funding_work_orders",
        "service_refund_cash_jobs", "service_refund_reserve_accounts",
        "dealer_resale_global_settings",
    )
    missing = [name for name in required if not _relation_exists(cur, name)]
    if missing:
        return {
            "ready": False, "schema_ready": False, "missing_schema": missing,
            "categories": [], "blocker_categories": ["schema"],
        }
    provider_id = int(provider_user_id)
    suffix = " FOR UPDATE" if lock else ""

    # Every dependency is read as concrete rows.  This lets confirmation lock
    # the exact obligations and keeps row counts, points and cents in separate
    # machine-readable units instead of adding unlike values together.
    specs = (
        ("clients", "commercial_bindings", "商业绑定客户", "rows",
         "SELECT id AS key,1 AS value FROM public.customer_agent_bindings WHERE agent_user_id=%s", (provider_id,)),
        ("orders", "pending_recharge_orders", "待支付订单", "rows",
         """SELECT id AS key,1 AS value FROM public.recharge_orders WHERE payment_status='pending' AND
            ((user_id=%s AND order_type='agent_inventory_purchase') OR
             (agent_user_id=%s AND (order_type IS NULL OR order_type='customer_recharge')))""", (provider_id, provider_id)),
        ("inventory", "inventory_wallet_points", "服务商库存", "points",
         """SELECT agent_user_id AS key,COALESCE(paid_inventory_points,0)+COALESCE(bonus_inventory_points,0)+
                   COALESCE(frozen_inventory_points,0) AS value
            FROM public.agent_inventory_wallets WHERE agent_user_id=%s""", (provider_id,)),
        ("channel_relations", "active_channel_relations", "现役渠道关系", "rows",
         """SELECT id AS key,1 AS value FROM public.channel_pricing_relationships
            WHERE status='active' AND effective_to IS NULL
              AND (buyer_dealer_id=%s OR upstream_channel_account_id=%s)""", (provider_id, provider_id)),
        ("responsibilities", "binding_disputes", "待裁决客户归属争议", "rows",
         """SELECT id AS key,1 AS value FROM public.customer_agent_binding_disputes
            WHERE status='pending' AND (old_agent_user_id=%s OR new_agent_user_id=%s)""", (provider_id, provider_id)),
        ("responsibilities", "held_escrows", "在途争议托管", "rows",
         """SELECT id AS key,1 AS value FROM public.dispute_escrow
            WHERE status='held' AND (order_agent_user_id=%s OR bound_agent_user_id=%s)""", (provider_id, provider_id)),
        ("earnings", "settlement_requests", "在途结算申请", "rows",
         "SELECT id AS key,1 AS value FROM public.agent_settlement_requests WHERE agent_user_id=%s AND status IN ('pending','approved')", (provider_id,)),
        ("earnings", "withdrawal_requests", "在途提现申请", "rows",
         "SELECT id AS key,1 AS value FROM public.withdrawal_requests WHERE user_id=%s AND status IN ('pending','approved')", (provider_id,)),
        ("earnings", "channel_revenue_cents", "未结渠道收益", "cents",
         """SELECT id AS key,channel_revenue_cents AS value FROM public.channel_revenue_ledger
            WHERE channel_beneficiary_user_id=%s AND status='recorded' AND channel_revenue_cents>0""", (provider_id,)),
        ("inventory", "resale_inventory_points", "逐级转售库存", "points",
         """SELECT lot_id AS key,remaining_points+reserved_points AS value FROM public.dealer_inventory_lots
            WHERE owner_agent_user_id=%s AND (remaining_points>0 OR reserved_points>0)""", (provider_id,)),
        ("orders", "open_resale_orders", "未终态转售订单", "rows",
         """SELECT order_id AS key,1 AS value FROM public.dealer_resale_orders
            WHERE (seller_user_id=%s OR refund_responsible_user_id=%s) AND state NOT IN ('refunded','cancelled')""", (provider_id, provider_id)),
        ("orders", "open_consumer_sales", "未终态消费者订单", "rows",
         """SELECT order_id AS key,1 AS value FROM public.dealer_consumer_sales
            WHERE (seller_user_id=%s OR refund_responsible_user_id=%s) AND state NOT IN ('refunded','cancelled')""", (provider_id, provider_id)),
        ("earnings", "resale_profit_cents", "未入收益台账的逐级收益", "cents",
         """SELECT profit_id AS key,margin_cents AS value FROM public.dealer_resale_profit_ledger
            WHERE seller_user_id=%s AND (status='pending' OR (status='available' AND revenue_ledger_id IS NULL))
              AND margin_cents>0""", (provider_id,)),
        ("responsibilities", "consumer_refund_cases", "消费者退款责任", "rows",
         """SELECT case_id AS key,1 AS value FROM public.consumer_refund_cases
            WHERE responsible_service_user_id=%s AND status NOT IN ('completed','rejected')""", (provider_id,)),
        ("responsibilities", "refund_work_orders", "退款工单", "rows",
         """SELECT id AS key,1 AS value FROM public.refund_work_orders
            WHERE agent_user_id=%s AND status IN ('draft','submitted','approved','payout_pending')""", (provider_id,)),
        ("responsibilities", "refund_liabilities", "退款负债", "rows",
         "SELECT liability_id AS key,1 AS value FROM public.service_refund_liability_ledger WHERE service_user_id=%s AND status<>'closed'", (provider_id,)),
        ("responsibilities", "refund_funding_work_orders", "退款资金工单", "rows",
         "SELECT work_order_id AS key,1 AS value FROM public.service_refund_funding_work_orders WHERE service_user_id=%s AND status<>'closed'", (provider_id,)),
        ("responsibilities", "refund_cash_jobs", "退款现金任务", "rows",
         "SELECT cash_job_id AS key,1 AS value FROM public.service_refund_cash_jobs WHERE responsible_service_user_id=%s AND status<>'completed'", (provider_id,)),
        ("inventory", "refund_reserve_cents", "退款准备金", "cents",
         "SELECT service_user_id AS key,available_cents AS value FROM public.service_refund_reserve_accounts WHERE service_user_id=%s AND available_cents>0", (provider_id,)),
        ("orders", "jit_fulfillment_plans", "在途 JIT 履约计划", "rows",
         """SELECT p.plan_id AS key,1 AS value FROM public.dealer_resale_fulfillment_plans p
            WHERE p.state NOT IN ('settled','cancelled') AND (
              p.final_seller_user_id=%s OR p.final_buyer_user_id=%s OR EXISTS (
                SELECT 1 FROM public.dealer_resale_fulfillment_hops h
                WHERE h.plan_id=p.plan_id AND (h.seller_user_id=%s OR h.buyer_user_id=%s)))""",
         (provider_id, provider_id, provider_id, provider_id)),
        ("orders", "jit_fulfillment_hops", "在途 JIT 履约节点", "rows",
         """SELECT plan_id || ':' || hop_seq AS key,1 AS value FROM public.dealer_resale_fulfillment_hops
            WHERE state NOT IN ('settled','cancelled') AND (seller_user_id=%s OR buyer_user_id=%s)""", (provider_id, provider_id)),
        ("inventory", "jit_reserved_allocations", "JIT 预留分配", "rows",
         """SELECT a.allocation_id AS key,1 AS value FROM public.dealer_resale_fulfillment_allocations a
            JOIN public.dealer_resale_fulfillment_hops h ON h.plan_id=a.plan_id AND h.hop_seq=a.hop_seq
            WHERE a.status='reserved' AND (h.seller_user_id=%s OR h.buyer_user_id=%s)""", (provider_id, provider_id)),
        ("earnings", "jit_hop_payable_cents", "JIT 节点未结应付", "cents",
         """SELECT profit_id AS key,agent_payable_cents AS value FROM public.dealer_resale_hop_profit_ledger
            WHERE seller_user_id=%s AND (status='pending' OR (status='available' AND revenue_ledger_id IS NULL))
              AND agent_payable_cents>0""", (provider_id,)),
    )
    labels = {
        "clients": "客户", "orders": "订单与履约", "inventory": "库存与退款储备",
        "earnings": "收益与结算", "responsibilities": "争议与退款责任",
        "channel_relations": "渠道关系", "platform_manufacturer": "平台生产主体",
    }
    grouped: dict[str, list[dict]] = {key: [] for key in labels}
    for category, key, label, unit, sql, params in specs:
        lock_sql = suffix
        if lock and " JOIN " in sql:
            if "fulfillment_allocations a" in sql:
                lock_sql = " FOR UPDATE OF a"
            elif "fulfillment_plans p" in sql:
                lock_sql = " FOR UPDATE OF p"
        cur.execute(sql + lock_sql, params)
        rows = cur.fetchall() or []
        value = sum(int(row.get("value") or 0) for row in rows)
        grouped[category].append({"key": key, "label": label, "unit": unit, "value": value})
    if lock:
        cur.execute(
            "SELECT id FROM public.agent_revenue_ledger WHERE agent_user_id=%s ORDER BY id FOR UPDATE",
            (provider_id,),
        )
        cur.fetchall()
        cur.execute(
            """SELECT i.id FROM public.agent_settlement_request_items i
               JOIN public.agent_settlement_requests r ON r.id=i.settlement_request_id
               WHERE r.agent_user_id=%s ORDER BY i.id FOR UPDATE OF i""",
            (provider_id,),
        )
        cur.fetchall()
        cur.execute(
            """SELECT i.id FROM public.agent_commission_redemption_items i
               JOIN public.agent_commission_redemption_requests r ON r.id=i.redemption_request_id
               WHERE r.agent_user_id=%s ORDER BY i.id FOR UPDATE OF i""",
            (provider_id,),
        )
        cur.fetchall()
    from services.agent_revenue import get_agent_balance
    revenue_balance = get_agent_balance(cur, provider_id)
    grouped["earnings"].append({
        "key": "agent_revenue_cents", "label": "未结服务商收益", "unit": "cents",
        "value": sum(int(revenue_balance.get(key) or 0) for key in (
            "frozen_cents", "available_cents", "pending_payout_cents",
        )),
    })
    cur.execute(
        "SELECT platform_seller_user_id FROM public.dealer_resale_global_settings WHERE singleton_id=1" + suffix
    )
    setting = cur.fetchone() or {}
    grouped["platform_manufacturer"].append({
        "key": "platform_manufacturer", "label": "平台生产主体配置", "unit": "rows",
        "value": int(int(setting.get("platform_seller_user_id") or 0) == provider_id),
    })
    from services.admin_user_governance import is_platform_direct_service_user
    grouped["platform_manufacturer"].append({
        "key": "platform_direct_service_identity", "label": "平台直营专用服务身份", "unit": "rows",
        "value": int(is_platform_direct_service_user(provider_id)),
    })
    categories = []
    for key, label in labels.items():
        outstanding = [item for item in grouped[key] if int(item["value"]) > 0]
        categories.append({"key": key, "label": label, "resolved": not outstanding, "outstanding": outstanding})
    blockers = [item["key"] for item in categories if not item["resolved"]]
    return {"ready": not blockers, "schema_ready": True, "missing_schema": [],
            "categories": categories, "blocker_categories": blockers}


def _downgrade_next_steps(strategy: str, snapshot: dict, target_provider_user_id: Optional[int]) -> list[dict]:
    action_labels = {
        "clients": (
            f"逐个使用商业服务归属专门 API 转交给服务商 #{target_provider_user_id}"
            if strategy == "transfer_upstream" else
            "逐个使用商业服务归属专门 API 转为平台直营" if strategy == "platform_managed"
            else "逐个结束或完成客户服务关系"
        ),
        "orders": "等待待支付/转售订单进入不可变终态；本向导不代改订单",
        "inventory": "使用现有库存退回、转售或结清原语归零；本向导不没收库存",
        "earnings": "完成现有结算/提现/收益冲销流程；本向导不覆盖收益",
        "responsibilities": "完成争议、退款责任和资金工单；责任不得转嫁给平台",
        "channel_relations": "使用渠道关系专门 API 结束现役上下游关系",
        "platform_manufacturer": "先迁移平台生产主体配置；向导不会翻生产配置",
        "schema": "补齐只读依赖 schema 证明后重试；当前保持 fail-closed",
    }
    keys = snapshot.get("blocker_categories") or []
    return [{"category": key, "instruction": action_labels.get(key, "使用该领域现有原语处理后重试")}
            for key in keys]


def get_provider_downgrade_readiness(provider_user_id: int) -> dict:
    snapshot = provider_downgrade_snapshot(provider_user_id)
    return {"provider_user_id": int(provider_user_id), **snapshot,
            "next_steps": _downgrade_next_steps("settle_then_downgrade", snapshot, None)}


def _validate_downgrade_destination(
    cur, *, strategy: str, provider_user_id: int, target_provider_user_id: Optional[int],
) -> Optional[int]:
    """Lock and validate the live terminal destination used at create/confirm."""
    from services.commercial_service_routing import (
        RelationshipConflict,
        lock_commercial_provider_for_assignment,
        lock_commercial_provider_lifecycle,
        platform_direct_readiness,
    )

    if strategy == "settle_then_downgrade":
        if target_provider_user_id is not None:
            raise CrossTenantValidation("DOWNGRADE_TARGET_INVALID", "结清后降级不能指定承接服务商")
        return None
    if strategy == "transfer_upstream":
        if not target_provider_user_id or int(target_provider_user_id) == int(provider_user_id):
            raise CrossTenantValidation("DOWNGRADE_TARGET_INVALID", "转交上级必须指定其他合格服务商")
        target_id = int(target_provider_user_id)
        try:
            lock_commercial_provider_lifecycle(cur, target_id)
            lock_commercial_provider_for_assignment(cur, target_id)
        except RelationshipConflict as exc:
            raise CrossTenantConflict(
                "DOWNGRADE_TARGET_NOT_ELIGIBLE",
                "目标承接服务商当前未通过身份与价目检查",
                details={"target_provider_user_id": target_id},
            ) from exc
        return target_id
    if strategy == "platform_managed":
        try:
            readiness = platform_direct_readiness(cur=cur)
        except Exception as exc:
            raise CrossTenantConflict(
                "PLATFORM_DIRECT_NOT_READY", "平台托管服务身份当前无法完成资格校验",
            ) from exc
        service_user = readiness.get("service_user") or {}
        target_id = int(service_user.get("user_id") or 0)
        if not readiness.get("ready") or not target_id or target_id == int(provider_user_id):
            raise CrossTenantConflict(
                "PLATFORM_DIRECT_NOT_READY", "平台托管服务身份尚未就绪",
                details={"status": readiness.get("status")},
            )
        try:
            lock_commercial_provider_lifecycle(cur, target_id)
            lock_commercial_provider_for_assignment(cur, target_id)
        except RelationshipConflict as exc:
            raise CrossTenantConflict(
                "PLATFORM_DIRECT_NOT_READY", "平台托管服务身份已失效",
                details={"target_provider_user_id": target_id},
            ) from exc
        # Re-evaluate after the target row/fence is held.  Catalog and identity
        # qualification therefore come from the same transaction as confirm.
        try:
            locked_readiness = platform_direct_readiness(cur=cur)
        except Exception as exc:
            raise CrossTenantConflict(
                "PLATFORM_DIRECT_NOT_READY", "平台托管服务身份当前无法完成资格校验",
            ) from exc
        if not locked_readiness.get("ready"):
            raise CrossTenantConflict("PLATFORM_DIRECT_NOT_READY", "平台托管服务身份已失效")
        return target_id
    raise CrossTenantValidation("DOWNGRADE_STRATEGY_INVALID", "不支持的服务商降级策略")


def create_provider_downgrade_plan(
    provider_user_id: int, *, strategy: str, target_provider_user_id: Optional[int],
    expected_identity_version: int, reason: str, actor_user_id: int,
    actor_username: Optional[str], request_id: str, ip_address: Optional[str],
) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(920714,%s)", (int(provider_user_id),))
        cur.execute("SELECT * FROM admin_provider_downgrade_plans WHERE created_request_id=%s", (request_id,))
        replay = cur.fetchone()
        if replay:
            if replay["strategy"] != strategy or replay.get("target_provider_user_id") != target_provider_user_id:
                raise CrossTenantConflict("IDEMPOTENCY_CONFLICT", "请求编号已用于不同降级方案")
            return {"success": True, "plan": _row(replay), "replayed": True}
        cur.execute(
            """SELECT u.id,u.is_active,COALESCE(w.agent_level,0) AS agent_level
               FROM users u LEFT JOIN user_wallets w ON w.user_id=u.id
               WHERE u.id=%s FOR UPDATE OF u""", (int(provider_user_id),),
        )
        provider = cur.fetchone()
        if not provider or not _active(provider["is_active"]):
            raise CrossTenantNotFound()
        if int(provider["agent_level"] or 0) < 1:
            raise CrossTenantValidation("SUBJECT_NOT_PROVIDER", "该账号当前不是服务商")
        cur.execute(
            """SELECT COALESCE((SELECT version FROM admin_user_governance_versions
                   WHERE subject_user_id=%s AND scope='business_identity'),1) AS version""",
            (int(provider_user_id),),
        )
        actual_identity_version = int(cur.fetchone()["version"])
        if actual_identity_version != int(expected_identity_version):
            raise CrossTenantConflict(
                "GOVERNANCE_VERSION_CONFLICT", "服务商身份版本已变化",
                details={"current_version": actual_identity_version},
            )
        _validate_downgrade_destination(
            cur, strategy=strategy, provider_user_id=int(provider_user_id),
            target_provider_user_id=target_provider_user_id,
        )
        cur.execute(
            """SELECT id FROM admin_provider_downgrade_plans
               WHERE provider_user_id=%s AND status IN ('draft','blocked','ready') FOR UPDATE""",
            (int(provider_user_id),),
        )
        if cur.fetchone():
            raise CrossTenantConflict("DOWNGRADE_PLAN_ALREADY_ACTIVE", "该服务商已有进行中的降级方案")
        snapshot = provider_downgrade_snapshot(provider_user_id, cur=cur)
        next_steps = _downgrade_next_steps(strategy, snapshot, target_provider_user_id)
        status = "ready" if snapshot["ready"] else "blocked"
        cur.execute(
            """INSERT INTO admin_provider_downgrade_plans(
                 provider_user_id,strategy,target_provider_user_id,status,
                 dependency_snapshot,next_steps,expected_identity_version,
                 created_by_user_id,created_reason,created_request_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (int(provider_user_id), strategy, target_provider_user_id, status,
             Json(snapshot), Json(next_steps), int(expected_identity_version),
             int(actor_user_id), reason, request_id),
        )
        plan = cur.fetchone()
        _audit(
            cur, actor_user_id=actor_user_id, actor_username=actor_username,
            action="provider_downgrade.plan_create", subject_kind="provider_downgrade_plan",
            subject_id=int(plan["id"]), request_id=request_id, reason=reason,
            before={}, after={"strategy": strategy, "status": status,
                              "dependency_snapshot": snapshot,
                              "assets_transferred": False, "funds_overwritten": False},
            ip_address=ip_address,
        )
        return {"success": True, "plan": _row(plan), "replayed": False}


def get_provider_downgrade_plan(provider_user_id: int, plan_id: int) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM admin_provider_downgrade_plans WHERE id=%s AND provider_user_id=%s",
            (int(plan_id), int(provider_user_id)),
        )
        plan = cur.fetchone()
        if not plan:
            raise CrossTenantNotFound()
        snapshot = provider_downgrade_snapshot(provider_user_id, cur=cur)
        result = _row(plan)
        result["live_dependency_snapshot"] = snapshot
        result["live_next_steps"] = _downgrade_next_steps(plan["strategy"], snapshot, plan.get("target_provider_user_id"))
        return result


def confirm_provider_downgrade_plan(
    provider_user_id: int, plan_id: int, *, expected_plan_version: int,
    reason: str, actor_user_id: int, actor_username: Optional[str],
    request_id: str, ip_address: Optional[str],
) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        # Payment callbacks lock orders before the provider lifecycle fence.
        # Match that order, then keep plan, census and identity terminal write
        # in this single transaction.
        cur.execute(
            """SELECT id FROM public.recharge_orders
               WHERE payment_status='pending' AND (
                 (user_id=%s AND order_type='agent_inventory_purchase') OR
                 (agent_user_id=%s AND (order_type IS NULL OR order_type='customer_recharge'))
               ) ORDER BY id FOR UPDATE""",
            (int(provider_user_id), int(provider_user_id)),
        )
        cur.fetchall()
        from services.commercial_service_routing import lock_commercial_provider_lifecycle
        lock_commercial_provider_lifecycle(cur, int(provider_user_id))
        cur.execute(
            "SELECT * FROM public.admin_provider_downgrade_plans WHERE id=%s AND provider_user_id=%s FOR UPDATE",
            (int(plan_id), int(provider_user_id)),
        )
        plan = cur.fetchone()
        if not plan:
            raise CrossTenantNotFound()
        if plan["status"] == "completed":
            if plan.get("confirmed_request_id") != request_id:
                raise CrossTenantConflict("DOWNGRADE_PLAN_ALREADY_COMPLETED", "降级方案已由其他请求完成")
            return {"success": True, "plan": _row(plan), "replayed": True}
        if int(plan["version"]) != int(expected_plan_version):
            raise CrossTenantConflict(
                "GOVERNANCE_VERSION_CONFLICT", "降级方案已被其他管理员更新",
                details={"current_version": int(plan["version"])},
            )
        _validate_downgrade_destination(
            cur, strategy=str(plan["strategy"]), provider_user_id=int(provider_user_id),
            target_provider_user_id=plan.get("target_provider_user_id"),
        )
        snapshot = provider_downgrade_snapshot(provider_user_id, cur=cur, lock=True)
        if not snapshot["ready"]:
            next_steps = _downgrade_next_steps(plan["strategy"], snapshot, plan.get("target_provider_user_id"))
            cur.execute(
                """UPDATE admin_provider_downgrade_plans
                   SET status='blocked',dependency_snapshot=%s,next_steps=%s,
                       version=version+1,updated_at=NOW() WHERE id=%s RETURNING *""",
                (Json(snapshot), Json(next_steps), int(plan_id)),
            )
            updated = cur.fetchone()
            _audit(
                cur, actor_user_id=actor_user_id, actor_username=actor_username,
                action="provider_downgrade.confirm_blocked", subject_kind="provider_downgrade_plan",
                subject_id=plan_id, request_id=request_id, reason=reason,
                before={"status": plan["status"], "version": int(plan["version"])},
                after={"status": "blocked", "version": int(updated["version"]),
                       "dependency_snapshot": snapshot}, ip_address=ip_address,
            )
            return {"success": False, "blocked": True, "plan": _row(updated),
                    "details": snapshot}
        from services.admin_user_governance import (
            GovernanceNotFound,
            GovernanceValidationError,
            GovernanceVersionConflict,
            complete_provider_downgrade_in_transaction,
        )
        try:
            result = complete_provider_downgrade_in_transaction(
                cur, int(provider_user_id),
                expected_version=int(plan["expected_identity_version"]),
                reason=reason, operator_user_id=int(actor_user_id),
                operator_username=actor_username, request_id=request_id,
                ip_address=ip_address,
            )
        except GovernanceNotFound as exc:
            raise CrossTenantNotFound() from exc
        except GovernanceVersionConflict as exc:
            raise CrossTenantConflict(
                "PROVIDER_IDENTITY_CHANGED", "服务商身份版本已变化",
                details={"current_identity_version": exc.current_version},
            ) from exc
        except GovernanceValidationError as exc:
            raise CrossTenantConflict(exc.code, str(exc)) from exc
        cur.execute(
            """UPDATE admin_provider_downgrade_plans
               SET status='completed',dependency_snapshot=%s,next_steps='[]'::jsonb,
                   confirmed_by_user_id=%s,confirmed_reason=%s,confirmed_request_id=%s,
                   confirmed_at=NOW(),version=version+1,updated_at=NOW()
               WHERE id=%s AND provider_user_id=%s AND status<>'completed' RETURNING *""",
            (Json(snapshot), int(actor_user_id), reason, request_id,
             int(plan_id), int(provider_user_id)),
        )
        completed = cur.fetchone()
        if not completed:
            raise CrossTenantConflict("DOWNGRADE_PLAN_STATE_CHANGED", "降级方案状态已变化")
        _audit(
            cur, actor_user_id=actor_user_id, actor_username=actor_username,
            action="provider_downgrade.complete", subject_kind="provider_downgrade_plan",
            subject_id=plan_id, request_id=request_id, reason=reason,
            before={"status": plan["status"], "version": int(plan["version"])},
            after={"status": "completed", "version": int(completed["version"]),
                   "funds_overwritten": False, "inventory_confiscated": False,
                   "commercial_relationships_overwritten": False},
            ip_address=ip_address,
        )
        return {"success": True, "plan": _row(completed), "replayed": False,
                "permission_version": result["permission_version"]}
