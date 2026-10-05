"""Controlled live read-only projection for demo-customer presentation access.

[D10 · SSOT v2.0 §8.2 · Owner 2026-07-25] 演示 = **真实数据的受控实时只读投影**。

演示用户打开被授权品牌的任何功能面,看到与 owner 相同的当天真实数据;唯一差别是
一切参数调节与写操作在服务端边界被拒绝。四条支柱:

1. **读 = 真实 handler**(同路由同代码同数据)。请求原样进 handler,不再由本模块
   另组一套快照视图 —— 那正是事故#6「另做缩水演示页」。
2. **租户隔离 = 唯一授权层**。演示可读范围由 ``auth.brand_access`` 承担
   (``demo_readable_brand_id``:只读方法 + 只此一个被授权品牌),越界与普通用户
   走同一条 404/403 fail-closed 路径。本模块**不另写第二套弱校验**。
   ⚠️ 这反转了 v1.x 的「demo 永不进入 require_brand_access」不变式(Owner 裁决)。
3. **出站脱敏**。§2.4 隐私硬边界(上游身份/成本/系数/portal token 明文/联系方式)
   在响应出网前统一剥除。
4. **写闸不变**。一切 mutation、参数调节、provider、任务、扣费、公开凭证签发,
   含**有副作用的 GET**(run-stream / clear-data / 导出原始数据…),在服务端边界
   拒绝并返回 §13 合同;零写入/零 provider/零资金合同原样保留。

``admin_demo_cases.safe_snapshot`` 由默认数据源**降级为 fallback**:仅用于门户
凭证这类必须冻结的面,以及真实数据源不可用时的兜底(避免整页空白/红码)。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException, Request
from psycopg2.errors import UndefinedTable
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse, Response

from db.connection import get_db
from auth.user_ctx import current_user_id


DEMO_CASE_NAMESPACE = uuid.UUID("5f961f21-3062-4b3a-a2a3-7e22e9df6a78")
logger = logging.getLogger("GEO-Demo-Access")

# This is the presentation DTO contract.  Unknown fields are dropped again at
# read time, even if a malformed/old row managed to place them in safe_snapshot.
DEMO_SNAPSHOT_FIELDS: dict[str, tuple[str, ...]] = {
    "customer_overview": (
        "brand_id", "brand_name", "industry", "industry_category", "status", "brand_type",
    ),
    "diagnosis_snapshot": (
        "id", "brand_id", "brand_name", "industry", "total_score", "level",
        "result_visibility", "web_search_score", "platform_score",
        "content_quality_score", "authority_score", "ai_visibility_score",
        "ai_citation_score", "status", "created_at", "completed_at",
    ),
    "quote_snapshots": (
        "id", "brand_id", "brand_name", "status", "created_at", "confirmed_at",
        "service_start_date", "service_end_date",
    ),
    "writing_generations": (
        "id", "brand_id", "quote_id", "status", "created_at", "completed_at", "article_count",
    ),
    "writing_articles": (
        "id", "brand_id", "quote_id", "title", "status", "created_at", "published_at",
    ),
    "publish_snapshots": (
        "id", "brand_id", "article_id", "status", "media_name", "created_at", "published_at",
    ),
    "monitoring_snapshots": (
        "id", "quote_id", "client_id", "brand_id", "status", "task_name", "platform",
        "keyword_count", "platform_count", "total_tests", "completed_tests", "trigger_type",
        "trend_synced", "created_at", "started_at", "completed_at",
    ),
    "report_snapshots": (
        "id", "client_id", "brand_id", "status", "title", "report_type", "period_start",
        "period_end", "summary_data", "content", "overall_score", "visibility_rate",
        "created_at", "completed_at", "sent_at",
    ),
    "monitoring_clients": (
        "quote_id", "brand_id", "brand_name", "industry", "status", "tier",
        "tier_name", "keyword_count", "service_days", "service_start_date",
    ),
    "monitoring_keywords": (
        "id", "quote_id", "brand_id", "keyword", "target_brand", "source",
        "difficulty", "status", "target_rate", "is_core", "cluster_id",
        "cluster_name", "lifecycle", "detection_rate", "effective_rate",
        "display_rate", "rate_change", "last_tested", "first_detected_date",
        "total_tests", "window_tests", "compliant_days", "remaining_days",
        "remaining_compliant", "service_remaining_days_natural", "service_expired",
        "service_overdue_days", "service_days", "is_compliant", "is_stable",
        "compliance_progress", "is_monitored", "monitoring_status", "archived_at",
        "archive_reason", "super_red_ocean", "competition_ratio", "note",
        "detection_details",
    ),
    "monitoring_results": (
        "id", "task_id", "keyword_id", "confirmed_keyword_id", "keyword", "platform",
        "round_number", "is_detected", "mention_type", "response_snippet", "tested_at",
        "identity_review_state", "identity_candidates", "identity_evidence_snippet",
        "identity_evidence_hash", "identity_decision_version",
    ),
    "monitoring_trends": (
        "id", "keyword_id", "keyword_source", "period_type", "period_date",
        "test_count", "detected_count", "detection_rate", "rate_change",
    ),
    "monitoring_publications": (
        "id", "quote_id", "article_id", "platform_name", "platform_url",
        "article_title", "publish_date", "created_at", "status", "status_label",
    ),
    "monitoring_logs": (
        "id", "brand_id", "action", "target_type", "target_id", "details", "created_at",
    ),
    "monitoring_archives": (
        "id", "archive_key", "brand_id", "quote_id", "created_at", "reason", "status",
    ),
    "monitoring_config": (
        "brand_id", "client_id", "default_platforms", "auto_monitor_enabled",
        "monitoring_interval_hours", "monitoring_start_hour", "service_days",
        "service_start_date", "updated_at",
    ),
    "portal_links": ("quote_id", "token_fingerprint", "expires_at"),
}

ACCESS_CONTRACT_KEYS = (
    "access_mode", "real_commercial_authority_granted", "database_writes_allowed",
    "provider_calls_allowed", "billing_allowed", "background_jobs_allowed",
    "public_token_issuance_allowed", "raw_export_allowed",
)

DEMO_DETECTION_DETAIL_FIELDS = (
    "platform", "is_detected", "mention_type", "tested_at", "citations",
    "snippet", "pending", "historical_only",
)
DEMO_CITATION_FIELDS = ("title", "url")
DEMO_LOG_DETAIL_FIELDS = (
    "keywords_count", "count", "keyword", "brand", "platforms", "source",
)
DEMO_REPORT_SUMMARY_FIELDS = (
    "detection_rate", "avg_detection_rate", "target_rate", "tier_name", "total_tests",
    "total_detected", "task_count", "total_keywords", "total_publications",
    "period_label", "date", "generated_at", "keyword_stats", "top_keywords",
    "bottom_keywords", "cluster_stats", "platform_stats",
)
DEMO_KEYWORD_STAT_FIELDS = ("keyword", "target_brand", "avg_rate", "tests_count")
DEMO_CLUSTER_STAT_FIELDS = ("cluster_name", "keyword_count", "avg_rate")
DEMO_SURFACE_DATASETS = (
    "customer_overview", "diagnosis", "quotes", "writing.generations",
    "writing.articles", "publish.publications", "monitoring.tasks", "reports",
)
DEMO_SENSITIVE_URL_KEY = re.compile(
    r"(?:^|[_-])(?:token|secret|signature|sig|key|auth|password|credential|session)(?:$|[_-])",
    re.IGNORECASE,
)


def _project_dict(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    source = dict(value) if isinstance(value, dict) else {}
    return {key: source[key] for key in fields if key in source}


def _project_list(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_project_dict(item, fields) for item in value if isinstance(item, dict)]


def _redact_demo_text(value: str) -> str:
    text = re.sub(r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", "[已脱敏邮箱]", value)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "1**********", text)
    return re.sub(
        r"(?i)\b(token|secret|api[_-]?key|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[已脱敏]",
        text,
    )


def _safe_demo_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        query = urlencode([
            (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not DEMO_SENSITIVE_URL_KEY.search(key)
        ])
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, query, ""))
    except (TypeError, ValueError):
        return None


def _dict_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _project_report_summary(value: Any) -> dict[str, Any]:
    summary = _project_dict(_dict_value(value), DEMO_REPORT_SUMMARY_FIELDS)
    for key in ("keyword_stats", "top_keywords", "bottom_keywords"):
        if key in summary:
            summary[key] = _project_list(summary.get(key), DEMO_KEYWORD_STAT_FIELDS)
    if "cluster_stats" in summary:
        summary["cluster_stats"] = _project_list(
            summary.get("cluster_stats"), DEMO_CLUSTER_STAT_FIELDS,
        )
    if "platform_stats" in summary:
        platform_stats = summary.get("platform_stats")
        summary["platform_stats"] = (
            {
                str(key)[:80]: value
                for key, value in platform_stats.items()
                if isinstance(value, (int, float, str, bool))
            }
            if isinstance(platform_stats, dict) else {}
        )
    return summary


def _project_reports(value: Any) -> list[dict[str, Any]]:
    reports = _project_list(value, DEMO_SNAPSHOT_FIELDS["report_snapshots"])
    for report in reports:
        if isinstance(report.get("title"), str):
            report["title"] = _redact_demo_text(report["title"])
        if "summary_data" in report:
            report["summary_data"] = _project_report_summary(report.get("summary_data"))
        if "content" in report:
            content = report.get("content")
            if isinstance(content, str):
                report["content"] = _redact_demo_text(content)
            else:
                report.pop("content", None)
    return reports


def _project_monitoring_logs(value: Any) -> list[dict[str, Any]]:
    logs = _project_list(value, DEMO_SNAPSHOT_FIELDS["monitoring_logs"])
    for row in logs:
        if "details" in row:
            details = _project_dict(_dict_value(row.get("details")), DEMO_LOG_DETAIL_FIELDS)
            row["details"] = json.dumps(details, ensure_ascii=False, sort_keys=True)
    return logs


def _project_monitoring_results(value: Any) -> list[dict[str, Any]]:
    rows = _project_list(value, DEMO_SNAPSHOT_FIELDS["monitoring_results"])
    for row in rows:
        for field in ("response_snippet", "identity_evidence_snippet"):
            if isinstance(row.get(field), str):
                row[field] = _redact_demo_text(row[field])
        candidates = row.get("identity_candidates")
        if "identity_candidates" in row:
            row["identity_candidates"] = [
                _redact_demo_text(item)[:160]
                for item in candidates[:20] if isinstance(item, str)
            ] if isinstance(candidates, list) else []
    return rows


def _project_monitoring_keywords(value: Any) -> list[dict[str, Any]]:
    rows = _project_list(value, DEMO_SNAPSHOT_FIELDS["monitoring_keywords"])
    for row in rows:
        if isinstance(row.get("note"), str):
            row["note"] = _redact_demo_text(row["note"])
        if "detection_details" in row:
            row["detection_details"] = _project_list(
                row.get("detection_details"), DEMO_DETECTION_DETAIL_FIELDS,
            )
            for detail in row["detection_details"]:
                if isinstance(detail.get("snippet"), str):
                    detail["snippet"] = _redact_demo_text(detail["snippet"])
                if "citations" in detail:
                    detail["citations"] = _project_list(
                        detail.get("citations"), DEMO_CITATION_FIELDS,
                    )
                    for citation in detail["citations"]:
                        if isinstance(citation.get("title"), str):
                            citation["title"] = _redact_demo_text(citation["title"])
                        if "url" in citation:
                            safe_url = _safe_demo_url(citation.get("url"))
                            if safe_url:
                                citation["url"] = safe_url
                            else:
                                citation.pop("url", None)
    return rows


def _project_monitoring_publications(value: Any) -> list[dict[str, Any]]:
    rows = _project_list(value, DEMO_SNAPSHOT_FIELDS["monitoring_publications"])
    for row in rows:
        if isinstance(row.get("article_title"), str):
            row["article_title"] = _redact_demo_text(row["article_title"])
        if "platform_url" in row:
            safe_url = _safe_demo_url(row.get("platform_url"))
            if safe_url:
                row["platform_url"] = safe_url
            else:
                row.pop("platform_url", None)
    return rows


def project_safe_snapshot(value: Any) -> dict[str, Any]:
    """Return only the immutable presentation allow-list, never extra JSON keys."""
    source = dict(value) if isinstance(value, dict) else {}
    writing = source.get("writing_snapshots")
    writing = dict(writing) if isinstance(writing, dict) else {}
    monitoring = source.get("monitoring_snapshot")
    monitoring = dict(monitoring) if isinstance(monitoring, dict) else {}
    monitoring_dataset_keys = (
        "clients", "keywords", "results", "trends", "publications", "logs",
        "archives", "config", "tasks", "reports",
    )
    declared_included = monitoring.get("included")
    monitoring_included = tuple(
        key for key in monitoring_dataset_keys
        if (
            key in declared_included
            if isinstance(declared_included, list)
            else key in monitoring
        )
    )
    declared_surface_included = source.get("included")
    if isinstance(declared_surface_included, list):
        surface_included = tuple(
            key for key in DEMO_SURFACE_DATASETS if key in declared_surface_included
        )
    else:
        surface_included = tuple(key for key, present in (
            ("customer_overview", "customer_overview" in source),
            ("diagnosis", "diagnosis_snapshot" in source),
            ("quotes", "quote_snapshots" in source),
            ("writing.generations", "generations" in writing),
            ("writing.articles", "articles" in writing),
            ("publish.publications", "publish_snapshots" in source),
            ("monitoring.tasks", "monitoring_snapshots" in source),
            ("reports", "report_snapshots" in source),
        ) if present)
    access = _project_dict(source.get("access_contract"), ACCESS_CONTRACT_KEYS)
    # Security values are constants, not trusted snapshot input.
    access.update({
        "access_mode": "demo",
        "real_commercial_authority_granted": False,
        "database_writes_allowed": False,
        "provider_calls_allowed": False,
        "billing_allowed": False,
        "background_jobs_allowed": False,
        "public_token_issuance_allowed": False,
        "raw_export_allowed": False,
    })
    return {
        "snapshot_contract": (
            "demo-customer-safe-v5"
            if source.get("snapshot_contract") == "demo-customer-safe-v5"
            else "demo-customer-safe-v4" if monitoring else "demo-customer-safe-v3"
        ),
        "watermark": "演示案例",
        "frozen_at": source.get("frozen_at"),
        "included": list(surface_included),
        "customer_overview": _project_dict(
            source.get("customer_overview"), DEMO_SNAPSHOT_FIELDS["customer_overview"],
        ),
        "diagnosis_snapshot": _project_dict(
            source.get("diagnosis_snapshot"), DEMO_SNAPSHOT_FIELDS["diagnosis_snapshot"],
        ),
        "quote_snapshots": _project_list(
            source.get("quote_snapshots"), DEMO_SNAPSHOT_FIELDS["quote_snapshots"],
        ),
        "portal_links": _project_list(
            source.get("portal_links"), DEMO_SNAPSHOT_FIELDS["portal_links"],
        ),
        "writing_snapshots": {
            "generations": _project_list(
                writing.get("generations"), DEMO_SNAPSHOT_FIELDS["writing_generations"],
            ),
            "articles": _project_list(
                writing.get("articles"), DEMO_SNAPSHOT_FIELDS["writing_articles"],
            ),
        },
        "publish_snapshots": _project_list(
            source.get("publish_snapshots"), DEMO_SNAPSHOT_FIELDS["publish_snapshots"],
        ),
        "monitoring_snapshots": _project_list(
            source.get("monitoring_snapshots"), DEMO_SNAPSHOT_FIELDS["monitoring_snapshots"],
        ),
        "report_snapshots": _project_reports(source.get("report_snapshots")),
        "monitoring_snapshot": {
            "included": list(monitoring_included),
            "clients": _project_list(
                monitoring.get("clients"), DEMO_SNAPSHOT_FIELDS["monitoring_clients"],
            ),
            "keywords": _project_monitoring_keywords(monitoring.get("keywords")),
            "results": _project_monitoring_results(monitoring.get("results")),
            "trends": _project_list(
                monitoring.get("trends"), DEMO_SNAPSHOT_FIELDS["monitoring_trends"],
            ),
            "publications": _project_monitoring_publications(monitoring.get("publications")),
            "logs": _project_monitoring_logs(monitoring.get("logs")),
            "archives": _project_list(
                monitoring.get("archives"), DEMO_SNAPSHOT_FIELDS["monitoring_archives"],
            ),
            "config": _project_list(
                monitoring.get("config"), DEMO_SNAPSHOT_FIELDS["monitoring_config"],
            ),
            "tasks": _project_list(
                monitoring.get("tasks"), DEMO_SNAPSHOT_FIELDS["monitoring_snapshots"],
            ),
            "reports": _project_reports(monitoring.get("reports")),
        },
        "access_contract": access,
    }


@dataclass(frozen=True)
class DemoAccessContext:
    access_mode: str
    viewer_user_id: int
    grant_id: int
    brand_id: int
    case_id: str
    diagnosis_id: int
    expires_at: Any
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class DemoPortalEntryResolution:
    context: DemoAccessContext
    quote_id: int
    link: dict[str, Any]


@dataclass(frozen=True)
class DemoRouteContract:
    action: str
    pattern: str
    methods: tuple[str, ...]
    disposition: str  # live | frozen_snapshot | preview | safe_handler
    surface: str
    real_mode_effects: tuple[str, ...]

    def matches(self, method: str, path: str) -> bool:
        return method in self.methods and re.search(self.pattern, path, re.IGNORECASE) is not None


class DemoSideEffectBlocked(RuntimeError):
    """Worker/provider/database terminal fence for an explicit demo context."""

    def __init__(self, action: str):
        super().__init__("demo access cannot cross a side-effect boundary")
        self.code = "DEMO_SIDE_EFFECT_BOUNDARY"
        self.action = str(action)


def assert_side_effects_allowed(access_mode: str, action: str) -> None:
    """Fail before a worker, provider, billing primitive or DB mutation starts."""
    if str(access_mode).strip().lower() == "demo":
        raise DemoSideEffectBlocked(action)


# Ordered, machine-readable route/action contract.  Credential-like GETs are
# mutations in disguise and therefore precede every snapshot surface rule.
DEMO_ROUTE_CONTRACTS: tuple[DemoRouteContract, ...] = (
    DemoRouteContract(
        "portal.existing_link.verify", r"^/api/portal/verify/?$",
        ("POST",), "safe_handler", "portal",
        ("只读校验快照中的现有门户链接",),
    ),
    DemoRouteContract(
        "portal.snapshot_entry.read", r"/api/portal/tokens(?:/by-brand)?/\d+(?:/|$)",
        ("GET", "HEAD"), "frozen_snapshot", "portal",
        ("读取真实客户门户凭证",),
    ),
    DemoRouteContract(
        "credential_or_raw_export", r"(?:share|public-link|download|raw-export|/export|\.(?:csv|xlsx|pdf))(?:/|$|\?)",
        ("GET", "HEAD"), "preview", "none",
        ("签发或读取公开凭证", "生成原始导出或公开链接", "记录分享状态"),
    ),
    # [D10 · 关键] 有副作用的 GET —— 必须排在所有只读面之前。
    #   D10 把只读默认改为"进真 handler",于是「GET 但有副作用」的路由从
    #   "未映射→404 顺带被挡"变成"会真的执行"。这是本次改造的头号风险面,
    #   所以在此显式登记为 preview(服务端拒绝),而不是依赖未映射兜底。
    #   按**路径段**匹配(前后是 / 或串尾),避免 /brands/current 这类误伤。
    #   纵深防御:即便此表漏网,终端 fence assert_side_effects_allowed() 仍会在
    #   worker/provider/billing/DB 写入真正开始前抛 DemoSideEffectBlocked。
    DemoRouteContract(
        "side_effect_get",
        r"/(?:run|run-stream|rerun|re-run|retry|start|stop|cancel|pause|resume|trigger|execute|"
        r"clear-data|restore-data|rollback|rollback-batch|reset|purge|archive|unarchive|"
        r"generate|regenerate|refresh|sync|resync|rebuild|recalculate|recalc|revalidate|"
        r"send|resend|notify|dispatch|publish-now|distill|expand|audit|stream|sse|"
        r"collect|crawl|probe|analyze|deep-analyze|enrich|autofill|backfill)"
        r"(?:/|$)",
        ("GET", "HEAD"), "preview", "none",
        ("触发后台任务或外部服务", "写入业务数据或状态", "按真实规则冻结/扣除费用"),
    ),
    DemoRouteContract(
        "customer_overview.read", r"/(?:client-context|clients?|brands?)(?:/|$)",
        ("GET", "HEAD"), "live", "customer_overview",
        ("读取客户实时资料",),
    ),
    DemoRouteContract(
        "diagnosis.read", r"/(?:diagnosis|diagnoses|diagnosis-records?)(?:/|$)",
        ("GET", "HEAD"), "live", "diagnosis",
        ("读取实时诊断",),
    ),
    DemoRouteContract(
        "quote.read", r"/(?:quotes?|pricing)(?:/|$)",
        ("GET", "HEAD"), "live", "quotes",
        ("读取或保存报价", "更新关键词与服务状态", "写入报价审计"),
    ),
    DemoRouteContract(
        "writing.read", r"/(?:writing|articles?|content)(?:/|$)",
        ("GET", "HEAD"), "live", "writing",
        ("保存写作状态", "调用内容生成服务", "扣费并创建文章"),
    ),
    DemoRouteContract(
        "publish.read", r"/(?:publish|publications?|meijiehezi)(?:/|$)",
        ("GET", "HEAD"), "live", "publish",
        ("创建发布订单", "调用外部发布服务", "冻结或扣除发布费用"),
    ),
    DemoRouteContract(
        "monitoring.read", r"/(?:monitor|monitoring|keywords?|logs)(?:/|$)",
        ("GET", "HEAD"), "live", "monitoring",
        ("保存监测配置或开关", "创建或取消调度任务", "结算监测费用"),
    ),
    DemoRouteContract(
        "report.read", r"/(?:reports?|reporting)(?:/|$)",
        ("GET", "HEAD"), "live", "reports",
        ("读取实时报告", "生成或发送报告", "签发报告凭证"),
    ),
)


def stable_demo_case_id(brand_id: int, diagnosis_id: int) -> str:
    return str(uuid.uuid5(DEMO_CASE_NAMESPACE, f"brand:{int(brand_id)}:diagnosis:{int(diagnosis_id)}"))


def _active(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "inactive", "disabled"}
    return bool(value)


def _rows(cur) -> list[dict[str, Any]]:
    return [dict(row) for row in cur.fetchall() or []]


def _live_context_query() -> str:
    return """
        SELECT g.id AS grant_id,g.brand_id,g.case_id,g.expires_at,
               c.diagnosis_id,c.safe_snapshot,u.is_active
        FROM public.admin_demo_case_grants g
        JOIN public.admin_demo_cases c ON c.case_id=g.case_id AND c.brand_id=g.brand_id
        JOIN public.brands b ON b.id=g.brand_id
        JOIN public.users u ON u.id=%s
        WHERE g.status='active' AND g.valid_from<=NOW() AND g.expires_at>NOW()
          AND g.capability='demo.customer.preview' AND c.status='active'
          AND (b.is_deleted IS NULL OR b.is_deleted=FALSE)
          AND (
            (g.grantee_kind='user' AND g.grantee_user_id=%s)
            OR
            (g.grantee_kind='organization' AND EXISTS(
              SELECT 1 FROM public.organization_memberships m
              JOIN public.organizations o ON o.id=m.organization_id
              WHERE m.organization_id=g.grantee_organization_id AND m.user_id=%s
                AND m.status='active' AND o.status='active'
            ))
          )
    """


def _context(user_id: int, row: Any) -> Optional[DemoAccessContext]:
    if not row or not _active(row.get("is_active")):
        return None
    return DemoAccessContext(
        access_mode="demo", viewer_user_id=int(user_id), grant_id=int(row["grant_id"]),
        brand_id=int(row["brand_id"]), case_id=str(row["case_id"]),
        diagnosis_id=int(row["diagnosis_id"]), expires_at=row["expires_at"],
        snapshot=project_safe_snapshot(row.get("safe_snapshot")),
    )


def resolve_demo_access(user_id: int, brand_id: int) -> Optional[DemoAccessContext]:
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                _live_context_query()
                + " AND g.brand_id=%s ORDER BY (g.grantee_kind='user') DESC,g.expires_at DESC,g.id DESC LIMIT 1",
                (int(user_id), int(user_id), int(user_id), int(brand_id)),
            )
            return _context(user_id, cur.fetchone())
    except UndefinedTable:
        return None


def _normalized_case_id(value: Any) -> Optional[str]:
    """`admin_demo_cases.case_id` / `admin_demo_case_grants.case_id` 是 **uuid 列** ——
    形状不对的串一律**不进 SQL**。

    🔴 [窗 1 微单 · 2026-08-20 · 存量] 原来把 `X-Demo-Case-ID` 请求头(**攻击者可控**)
       原样塞进 `g.case_id=%s`。非 UUID 时 psycopg2 抛 `InvalidTextRepresentation`
       (SQLSTATE 22P02),而本函数当时只 catch `UndefinedTable` ⇒ 异常穿透到中间件
       ⇒ **HTTP 500**(实测:`not-a-uuid` / 空串 / `' OR 1=1 --` 三种全 500)。
       500 不只是可用性问题:它把「这个 case 不存在」(404)和「你这串格式不对」(500)
       变成**两种可区分的回答** = 一个免费的探测 oracle。

    🔴 **返回 None,不抛**:本函数的合同就是「拿得到 context 或 None」。
       让 None 走调用方既有的 404 分支 ⇒ 与「合法但不存在」**同一种回答**,不给 oracle。
       (`auth/middleware.py` 那个调用点自己有 fail-closed 兜底,不受影响;
       API 路由那几处用 FastAPI 的 `case_id: uuid.UUID` 声明,在更前面就 422 了 ——
       所以真正没人管的入口只有这个请求头。)

    承重的是**「进 SQL 之前就拒掉」**这一步(把它摘掉 ⇒ 判据 17 条转红)。
    归一化本身**不承重**:`uuid.UUID()` 认的那些形态(带花括号 / urn: / 无短横 32 位)
    PostgreSQL 的 uuid 输入本来也认 —— 变异实测「闸在但仍送原串」判据全绿,
    正是因为两者对 PG 等价。留归一化是为了让落库/日志里的值规范,标**非承重**,
    别把它当成第二道安全闸。
    """
    try:
        return str(uuid.UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def resolve_demo_case_access(user_id: int, case_id: str) -> Optional[DemoAccessContext]:
    normalized_case_id = _normalized_case_id(case_id)
    if normalized_case_id is None:
        return None
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                _live_context_query()
                + " AND g.case_id=%s ORDER BY (g.grantee_kind='user') DESC,g.expires_at DESC,g.id DESC LIMIT 1",
                (int(user_id), int(user_id), int(user_id), normalized_case_id),
            )
            return _context(user_id, cur.fetchone())
    except UndefinedTable:
        return None


def resolve_demo_access_history(user_id: int, brand_id: int) -> Optional[DemoAccessContext]:
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT g.id AS grant_id,g.brand_id,g.case_id,g.expires_at,
                          c.diagnosis_id,c.safe_snapshot,TRUE AS is_active
                   FROM public.admin_demo_case_grants g
                   JOIN public.admin_demo_cases c ON c.case_id=g.case_id AND c.brand_id=g.brand_id
                   WHERE g.brand_id=%s AND (
                     (g.grantee_kind='user' AND g.grantee_user_id=%s)
                     OR (g.grantee_kind='organization' AND EXISTS(
                       SELECT 1 FROM public.organization_memberships m
                       WHERE m.organization_id=g.grantee_organization_id
                         AND m.user_id=%s AND m.status='active'
                     ))
                   ) ORDER BY g.updated_at DESC,g.id DESC LIMIT 1""",
                (int(brand_id), int(user_id), int(user_id)),
            )
            return _context(user_id, cur.fetchone())
    except UndefinedTable:
        return None


def list_live_demo_case_contexts(user_id: int) -> list[DemoAccessContext]:
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                _live_context_query() + " ORDER BY (g.grantee_kind='user') DESC,g.expires_at DESC,g.id DESC",
                (int(user_id), int(user_id), int(user_id)),
            )
            rows = _rows(cur)
    except UndefinedTable:
        return []
    if rows and not _active(rows[0].get("is_active")):
        return []
    by_case: dict[str, DemoAccessContext] = {}
    for row in rows:
        context = _context(user_id, row)
        if context:
            by_case.setdefault(context.case_id, context)
    return list(by_case.values())


def list_live_demo_contexts(user_id: int) -> list[DemoAccessContext]:
    by_brand: dict[int, DemoAccessContext] = {}
    for context in list_live_demo_case_contexts(user_id):
        by_brand.setdefault(context.brand_id, context)
    return list(by_brand.values())


def _request_id(request: Request) -> str:
    return str(
        getattr(request.state, "request_id", None)
        or getattr(request.state, "organization_request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )[:180]


def _source_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )


def _action_name(request: Request) -> str:
    path = re.sub(r"/[0-9a-f-]{8,}(?=/|$)", "/{id}", request.url.path, flags=re.IGNORECASE)
    path = re.sub(r"/\d+(?=/|$)", "/{id}", path)
    return f"{request.method.upper()} {path}"[:240]


def record_demo_access_event(
    context: DemoAccessContext, *, action: str, request_id: str,
    ip_address: str, blocked_reason: Optional[str] = None,
) -> None:
    """Emit a structured audit event without mutating the demo or customer database."""
    logger.info(
        "demo_access_event %s",
        json.dumps({
            "event": "demo_access",
            "grant_id": context.grant_id,
            "viewer_user_id": context.viewer_user_id,
            "brand_id": context.brand_id,
            "case_id": context.case_id,
            "action": action[:240],
            "request_id": request_id[:180],
            "source_ip_hash": hashlib.sha256(ip_address.encode("utf-8")).hexdigest()[:20],
            "blocked_reason": blocked_reason[:240] if blocked_reason else None,
        }, ensure_ascii=False, sort_keys=True),
    )


def resolve_demo_route_contract(method: str, path: str) -> Optional[DemoRouteContract]:
    normalized_method = method.upper()
    normalized_path = path.lower()
    for contract in DEMO_ROUTE_CONTRACTS:
        if contract.matches(normalized_method, normalized_path):
            return contract
    if normalized_method not in {"GET", "HEAD", "OPTIONS"}:
        return DemoRouteContract(
            "side_effect.preview", r".*", (normalized_method,), "preview", "none",
            ("保存业务变更", "按真实规则结算费用", "调用外部服务或创建后台任务"),
        )
    return None


def demo_preview_contract(
    request: Request, context: DemoAccessContext, blocked_reason: str,
    route_contract: Optional[DemoRouteContract] = None,
) -> dict[str, Any]:
    contract = route_contract or resolve_demo_route_contract(request.method, request.url.path)
    effects = list(contract.real_mode_effects) if contract else []
    # [§13 · D10] 七字段机器合同 + 至少一个合法出口。演示拒绝是**产品预期行为**,
    # 不是故障:必须说清"这是演示案例、真实模式下会发生什么、下一步能做什么",
    # 否则就是红码无下一步(事故#8)。既有传输键(saved/charged/provider_called/…)
    # 一并保留,前端与既有判别测试零改动。
    payload = {
        "code": "DEMO_ACTION_PREVIEW",
        "message": "这是演示案例，只能查看真实数据，不能修改或执行。",
        "reason": "你正在以演示身份查看该客户案例。演示可以看到与本人账号相同的实时数据，但所有参数调节和写入操作都被服务端拒绝。",
        "impact": "本次操作没有执行：没有保存任何修改，没有调用外部服务，没有创建任务，也没有产生任何费用。",
        "repair_hint": (
            "演示模式下可以自由浏览各个功能面的真实数据；"
            "如需真正执行这些操作，请使用你自己的客户，或联系该客户的负责人开通正式权限。"
        ),
        "actions": [
            {"id": "back_to_demo_read", "label": "返回继续查看演示数据", "type": "dismiss"},
            {"id": "switch_to_own_client", "label": "切换到我自己的客户", "type": "nav"},
            {"id": "contact_owner", "label": "联系该客户负责人", "type": "contact"},
        ],
        "rule_version": "demo-readonly-projection-v2",
        "access_mode": "demo",
        "action": contract.action if contract else _action_name(request),
        "route_contract": contract.action if contract else "unmapped",
        "blocked_reason": blocked_reason,
        "saved": False,
        "charged": False,
        "provider_called": False,
        "external_service_called": False,
        "job_created": False,
        "refresh_discards_local_preview": True,
        "real_mode_effects": effects,
        "request_id": _request_id(request),
    }
    if effects:
        payload["repair_hint"] = (
            f"真实模式下这个操作会：{'；'.join(effects)}。" + payload["repair_hint"]
        )
    return payload


def _portal_entry_secret() -> Optional[bytes]:
    value = os.environ.get("DEMO_PORTAL_ENTRY_SECRET") or os.environ.get("JWT_SECRET")
    if not value or len(value.encode("utf-8")) < 32:
        return None
    return value.encode("utf-8")


def portal_token_fingerprint(*, token: str, quote_id: int, brand_id: int) -> Optional[str]:
    """Return a purpose-separated HMAC; customer credentials never enter snapshots."""
    secret = _portal_entry_secret()
    candidate = str(token or "")
    if secret is None or not candidate:
        return None
    material = f"demo-portal-token-fingerprint:v1:{int(brand_id)}:{int(quote_id)}:{candidate}"
    return hmac.new(secret, material.encode("utf-8"), hashlib.sha256).hexdigest()


def _portal_entry_material(context: DemoAccessContext, quote_id: int) -> bytes:
    expires_at = context.expires_at.isoformat() if hasattr(context.expires_at, "isoformat") else str(context.expires_at)
    return (
        f"v3:{context.grant_id}:{context.case_id}:{context.brand_id}:"
        f"{context.viewer_user_id}:{int(quote_id)}:{expires_at}"
    ).encode("utf-8")


def demo_portal_entry(context: DemoAccessContext, *, quote_id: int) -> Optional[str]:
    """Return a grant/viewer/quote-bound transport handle, never a customer link."""
    secret = _portal_entry_secret()
    if secret is None:
        return None
    digest = hmac.new(secret, _portal_entry_material(context, quote_id), hashlib.sha256).digest()[:20]
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=")
    return f"D{encoded}"


def is_demo_portal_entry(value: str) -> bool:
    """Reserve the internal demo-handle namespace so it never falls through to live auth."""
    return re.fullmatch(r"D[A-Z2-7]{32}", str(value or "").strip().upper()) is not None


def _current_portal_token_row(quote_id: int, brand_id: int) -> Optional[dict[str, Any]]:
    """该 quote **当前**有效的门户 token 行。仅服务端内部使用,绝不出网。

    [工单 2026-07-29 T2] 与 `_portal_link_is_live` 的差别:那条要求冻结快照里的
    指纹仍然逐字匹配,token 一旦轮换/新签发,演示门户当天就打不开。这里改成按
    「该 quote 属于本演示案例授权的品牌」直接取当前 active token —— 仍然不跨品牌、
    不签发新 token、不返回明文给前端。
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT t.token, t.is_active, t.expires_at
                     FROM public.client_access_tokens t
                     JOIN public.quotes q ON q.id = t.quote_id
                    WHERE t.quote_id = %s AND q.brand_id = %s
                      AND COALESCE(t.is_active, 0) <> 0
                    ORDER BY t.created_at DESC, t.id DESC
                    LIMIT 1""",
                (int(quote_id), int(brand_id)),
            )
            rows = _rows(cur)
    except UndefinedTable:
        return None
    for row in rows:
        if _active(row.get("is_active")) and _not_expired(row.get("expires_at")):
            return dict(row)
    return None


def live_portal_link(context: DemoAccessContext, *, quote_id: int) -> Optional[dict[str, Any]]:
    """演示门户入口的**实时**解析(T2)。

    授权边界没有放宽:quote 必须已在本演示案例的快照客户集里(`_quote_ids`),
    品牌必须等于 grant 绑定的品牌。变的只是"用哪一版 token 派生句柄"。
    """
    try:
        target = int(quote_id)
    except (TypeError, ValueError):
        return None
    if target not in _quote_ids(context):
        return None
    row = _current_portal_token_row(target, context.brand_id)
    if not row:
        return None
    fingerprint = portal_token_fingerprint(
        token=str(row.get("token") or ""), quote_id=target, brand_id=context.brand_id,
    )
    if not fingerprint:
        return None
    return {
        "quote_id": target,
        "token_fingerprint": fingerprint,
        "expires_at": row.get("expires_at"),
    }


def resolve_portal_link(context: DemoAccessContext, *, quote_id: int) -> Optional[dict[str, Any]]:
    """先按实时口径解析,拿不到再回落到冻结快照口径(向后兼容)。"""
    return (
        live_portal_link(context, quote_id=quote_id)
        or live_snapshot_portal_link(context, quote_id=quote_id)
    )


def resolve_demo_portal_entry(entry: str) -> Optional[DemoPortalEntryResolution]:
    """Resolve in memory first; perform one live-link query only after an HMAC match."""
    if not is_demo_portal_entry(entry):
        return None
    if _portal_entry_secret() is None:
        return None
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT g.id AS grant_id,g.brand_id,g.case_id,g.expires_at,
                          c.diagnosis_id,c.safe_snapshot,
                          CASE WHEN g.grantee_kind='user' THEN gu.id ELSE gom.id END AS viewer_user_id,
                          TRUE AS is_active
                   FROM public.admin_demo_case_grants g
                   JOIN public.admin_demo_cases c ON c.case_id=g.case_id AND c.brand_id=g.brand_id
                   JOIN public.brands b ON b.id=g.brand_id
                   LEFT JOIN public.users gu ON g.grantee_kind='user' AND gu.id=g.grantee_user_id
                   LEFT JOIN public.organizations go ON g.grantee_kind='organization'
                                                       AND go.id=g.grantee_organization_id
                   LEFT JOIN public.organization_memberships gm
                              ON g.grantee_kind='organization'
                             AND gm.organization_id=g.grantee_organization_id
                             AND gm.status='active'
                   LEFT JOIN public.users gom ON gom.id=gm.user_id
                   WHERE g.status='active' AND g.valid_from<=NOW() AND g.expires_at>NOW()
                     AND g.capability='demo.customer.preview' AND c.status='active'
                     AND (b.is_deleted IS NULL OR b.is_deleted=FALSE)
                     AND ((g.grantee_kind='user' AND COALESCE(gu.is_active,0)<>0)
                          OR (g.grantee_kind='organization' AND go.status='active'
                              AND gom.id IS NOT NULL AND COALESCE(gom.is_active,0)<>0))
                   ORDER BY g.expires_at DESC,g.id DESC
                   LIMIT 5000""",
            )
            rows = _rows(cur)
    except UndefinedTable:
        return None
    candidate = str(entry).strip().upper()
    for row in rows:
        context = _context(int(row.get("viewer_user_id") or 0), row)
        if not context:
            continue
        # [工单 2026-07-29 T2] 候选 quote 从「冻结快照里的 portal_links」放宽到
        # 「本演示案例快照里的客户集」。原口径要求签发过 token 且指纹没变过,
        # 于是 token 一轮换/一新签发,演示门户就永久打不开(Owner 报的入口不可点)。
        # 授权边界不变:仍只在 grant 绑定品牌的 quote 里挑,句柄仍与 grant/观众/quote
        # 三者绑定,过期即失效。
        candidate_quotes = set(_quote_ids(context))
        for link in project_safe_snapshot(context.snapshot).get("portal_links") or []:
            try:
                candidate_quotes.add(int(link.get("quote_id")))
            except (TypeError, ValueError):
                continue
        for quote_id in sorted(candidate_quotes):
            derived = demo_portal_entry(context, quote_id=quote_id)
            if derived and hmac.compare_digest(candidate, derived):
                live_link = resolve_portal_link(context, quote_id=quote_id)
                return (
                    DemoPortalEntryResolution(context=context, quote_id=quote_id, link=live_link)
                    if live_link else None
                )
    return None


def _not_expired(value: Any) -> bool:
    if value in (None, ""):
        return True
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = datetime.fromisoformat(parsed.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = date.fromisoformat(parsed.strip())
            except ValueError:
                return False
    if isinstance(parsed, datetime):
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed > datetime.now(timezone.utc)
    if isinstance(parsed, date):
        return parsed >= datetime.now(timezone.utc).date()
    return False


def _portal_link_is_live(context: DemoAccessContext, link: dict[str, Any]) -> bool:
    """Revalidate one exact frozen credential without returning any live customer data."""
    try:
        quote_id = int(link.get("quote_id"))
        fingerprint = str(link.get("token_fingerprint") or "").lower()
    except (TypeError, ValueError):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or not _not_expired(link.get("expires_at")):
        return False
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT t.token,t.is_active,t.expires_at
                     FROM public.client_access_tokens t
                     JOIN public.quotes q ON q.id=t.quote_id
                    WHERE t.quote_id=%s AND q.brand_id=%s""",
                (quote_id, int(context.brand_id)),
            )
            rows = _rows(cur)
    except UndefinedTable:
        return False
    for row in rows:
        current = portal_token_fingerprint(
            token=str(row.get("token") or ""), quote_id=quote_id, brand_id=context.brand_id,
        )
        if (
            current
            and hmac.compare_digest(fingerprint, current)
            and _active(row.get("is_active"))
            and _not_expired(row.get("expires_at"))
        ):
            return True
    return False


def live_snapshot_portal_link(
    context: DemoAccessContext, *, quote_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Revalidate at most one exact frozen link; never discover a new token."""
    snapshot = project_safe_snapshot(context.snapshot)
    for link in snapshot.get("portal_links") or []:
        try:
            if quote_id is not None and int(link.get("quote_id")) != int(quote_id):
                continue
        except (TypeError, ValueError):
            continue
        # Safe snapshots contain one link per quote. Stop after the first exact
        # projection even if a malformed legacy snapshot duplicated it, keeping
        # public handle resolution to at most one client_access_tokens query.
        return dict(link) if _portal_link_is_live(context, link) else None
    return None


def _snapshot_meta(context: DemoAccessContext, *, missing: bool = False, fields: Iterable[str] = ()) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "access_mode": "demo",
        "data_source": "admin_demo_cases.safe_snapshot",
        "case_id": context.case_id,
        "brand_id": context.brand_id,
        "demo_watermark": "演示案例",
        "snapshot_missing": bool(missing),
    }
    missing_fields = [str(field) for field in fields]
    if missing_fields:
        payload["snapshot_missing_fields"] = missing_fields
        payload["snapshot_message"] = "快照未包含"
    return payload


def _monitoring_data(context: DemoAccessContext) -> tuple[dict[str, Any], set[str]]:
    snapshot = project_safe_snapshot(context.snapshot)
    monitoring = dict(snapshot.get("monitoring_snapshot") or {})
    return monitoring, set(monitoring.get("included") or [])


def _demo_clients(context: DemoAccessContext) -> tuple[list[dict[str, Any]], bool]:
    snapshot = project_safe_snapshot(context.snapshot)
    monitoring, included = _monitoring_data(context)
    if "clients" in included:
        return list(monitoring.get("clients") or []), False
    overview = snapshot["customer_overview"]
    clients: list[dict[str, Any]] = []
    for quote in snapshot.get("quote_snapshots") or []:
        quote_id = quote.get("id")
        if quote_id is None:
            continue
        clients.append({
            key: value for key, value in {
                "quote_id": quote_id,
                "brand_id": context.brand_id,
                "brand_name": quote.get("brand_name") or overview.get("brand_name"),
                "industry": overview.get("industry"),
                "status": quote.get("status"),
                "service_start_date": quote.get("service_start_date"),
            }.items() if value is not None
        })
    return clients, True


def _quote_ids(context: DemoAccessContext) -> set[int]:
    clients, _ = _demo_clients(context)
    values: set[int] = set()
    for item in clients:
        try:
            values.add(int(item.get("quote_id")))
        except (TypeError, ValueError):
            continue
    return values


def _requested_id(path: str, pattern: str) -> Optional[int]:
    match = re.search(pattern, path, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _query_int(query: Any, key: str) -> Optional[int]:
    try:
        raw = query.get(key) if query is not None else None
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None



def _demo_json(status_code: int, content: Any, headers: Any = None) -> JSONResponse:
    """本模块**唯一**的 `JSONResponse` 出口:内容一律先过 `jsonable_encoder`。

    ## 缺陷

    `GET /api/portal/tokens/94` 在 demo 的 `frozen_snapshot` 那支 500 x2:
    `snapshot_transport_payload()` 的 payload 里带着 DB 的 `date`
    (`trends` 里的 `period_date`、`expires_at` / `archived_at`),
    直塞 `JSONResponse(content=payload)` => `TypeError: Object of type date is
    not JSON serializable`。

    ## 为什么在**出口**编码,而不是在 `snapshot_transport_payload` 里
    ## 把 date/datetime 归一成 ISO

    归一只挡得住**我今天想到的那几种类型**。`Decimal`(金额)、`UUID`、
    `Enum` 一样不可序列化,而它们进 payload 的那天不会有任何东西提醒 ——
    表现同样是整页 500。`jsonable_encoder` 覆盖的是「不可序列化」这个**事实**,
    不是我列举出来的那几种写法。

    ## 为什么是门,不是注释

    8 个调用点全部改走这里,并配一条「本模块零裸 `JSONResponse`」的结构锁。
    只修 1748 那一处的话,其余 7 处照样是雷 —— 而且下一个新增的调用点
    仍然可以直塞,不会有任何东西变红。
    """
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(content),
        headers=headers,
    )

def snapshot_transport_payload(
    context: DemoAccessContext, *, method: str, path: str, query: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Adapt a frozen snapshot to the same endpoint DTO consumed by live pages."""
    normalized_method = method.upper()
    normalized_path = path.lower()
    if normalized_method not in {"GET", "HEAD"}:
        return 409, {
            "detail": {
                "code": "DEMO_ACTION_PREVIEW",
                "message": "演示案例为只读",
                "access_mode": "demo",
                "saved": False,
                "charged": False,
                "provider_called": False,
                "job_created": False,
                "request_id": uuid.uuid4().hex,
            },
        }

    snapshot = project_safe_snapshot(context.snapshot)
    monitoring, included = _monitoring_data(context)
    quote_ids = _quote_ids(context)
    meta = _snapshot_meta(context)

    if re.search(r"/api/portal/tokens(?:/by-brand)?/\d+/?$", normalized_path):
        quote_id = _requested_id(normalized_path, r"/tokens/(\d+)")
        brand_id = _requested_id(normalized_path, r"/by-brand/(\d+)")
        if quote_id is not None and quote_id not in quote_ids:
            return 404, {"detail": "资源不存在"}
        if brand_id is not None and brand_id != context.brand_id:
            return 404, {"detail": "资源不存在"}
        selected_quote = quote_id or (min(quote_ids) if quote_ids else None)
        if selected_quote is None:
            return 200, {
                "status": "success", "token": None,
                **_snapshot_meta(context, missing=True, fields=("portal.quote_id",)),
            }
        # [工单 2026-07-29 T2] 实时口径优先:token 轮换/新签发后演示门户仍可打开。
        # 仍然只返回内部句柄,明文 token 永不出网(§2.4 隐私硬边界不放宽)。
        link = resolve_portal_link(context, quote_id=selected_quote)
        if not link:
            return 200, {
                "status": "success", "token": None, "quote_id": selected_quote,
                **_snapshot_meta(context, missing=True, fields=("portal.existing_link",)),
            }
        internal_entry = demo_portal_entry(context, quote_id=selected_quote)
        if not internal_entry:
            return 503, {"detail": {"code": "DEMO_PORTAL_ENTRY_UNAVAILABLE", "message": "演示门户入口未配置"}}
        token_info = {
            "token": internal_entry,
            "expires_at": link.get("expires_at"),
            "is_active": True,
            "access_mode": "demo",
            "demo_transport_entry": internal_entry,
        }
        return 200, {
            "status": "success", "token": token_info, "quote_id": selected_quote,
            "brand_name": snapshot.get("customer_overview", {}).get("brand_name"), **meta,
        }

    if normalized_path.endswith("/api/monitoring/clients"):
        clients, missing = _demo_clients(context)
        payload = {"status": "success", "clients": clients, **_snapshot_meta(
            context, missing=missing, fields=("monitoring.clients",) if missing else (),
        )}
        if not missing:
            payload["count"] = len(clients)
        return 200, payload

    quote_id = _requested_id(normalized_path, r"/api/monitoring/clients/(\d+)/keywords")
    if quote_id is not None:
        if quote_id not in quote_ids:
            return 404, {"detail": "资源不存在"}
        missing = "keywords" not in included
        keywords = list(monitoring.get("keywords") or []) if not missing else []
        payload = {
            "status": "success", "keywords": keywords,
            "super_red_ocean_keywords": [row for row in keywords if row.get("super_red_ocean") is True],
            "covered_keywords": [row for row in keywords if row.get("is_core") is False],
            **_snapshot_meta(context, missing=missing, fields=("monitoring.keywords",) if missing else ()),
        }
        if not missing:
            payload["count"] = len(keywords)
        return 200, payload

    quote_id = _requested_id(normalized_path, r"/api/publications/(\d+)")
    if quote_id is not None:
        if quote_id not in quote_ids:
            return 404, {"detail": "资源不存在"}
        missing = "publications" not in included
        publications = list(monitoring.get("publications") or []) if not missing else []
        payload = {"status": "success", "publications": publications, **_snapshot_meta(
            context, missing=missing, fields=("monitoring.publications",) if missing else (),
        )}
        if not missing:
            payload["count"] = len(publications)
        return 200, payload

    if normalized_path.endswith("/api/monitoring/trend"):
        requested_quote = _query_int(query, "client_id")
        if requested_quote is not None and requested_quote not in quote_ids:
            return 404, {"detail": "资源不存在"}
        missing = "trends" not in included
        trends = [
            {"date": row.get("period_date"), "rate": row.get("detection_rate")}
            for row in monitoring.get("trends") or []
            if row.get("period_date") is not None
        ] if not missing else []
        return 200, {"status": "success", "trend": trends, **_snapshot_meta(
            context, missing=missing, fields=("monitoring.trends",) if missing else (),
        )}

    if normalized_path.endswith("/api/logs"):
        missing = "logs" not in included
        logs = list(monitoring.get("logs") or []) if not missing else []
        return 200, {"status": "success", "logs": logs, **_snapshot_meta(
            context, missing=missing, fields=("monitoring.logs",) if missing else (),
        )}

    if normalized_path.endswith("/api/monitoring/rollback/tasks") or normalized_path.endswith("/api/monitoring/tasks"):
        missing = "tasks" not in included
        tasks = list(monitoring.get("tasks") or []) if not missing else []
        payload = {"status": "success", "tasks": tasks, **_snapshot_meta(
            context, missing=missing, fields=("monitoring.tasks",) if missing else (),
        )}
        if not missing:
            payload["count"] = len(tasks)
        return 200, payload

    task_id = _requested_id(normalized_path, r"/api/monitoring/tasks/(\d+)")
    if task_id is not None:
        missing_tasks = "tasks" not in included
        tasks = list(monitoring.get("tasks") or []) if not missing_tasks else []
        task = next((row for row in tasks if int(row.get("id") or -1) == task_id), None)
        if not task:
            if missing_tasks:
                return 200, {"status": "success", "task": None, "results": [], "cells": [], **_snapshot_meta(
                    context, missing=True, fields=("monitoring.tasks",),
                )}
            return 404, {"detail": "资源不存在"}
        missing = "results" not in included
        results = [
            row for row in monitoring.get("results") or []
            if int(row.get("task_id") or -1) == task_id
        ] if not missing else []
        return 200, {"status": "success", "task": task, "results": results, "cells": [], **_snapshot_meta(
            context, missing=missing, fields=("monitoring.results",) if missing else (),
        )}

    if normalized_path.endswith("/api/monitoring/archives"):
        missing = "archives" not in included
        return 200, {"status": "success", "archives": list(monitoring.get("archives") or []), **_snapshot_meta(
            context, missing=missing, fields=("monitoring.archives",) if missing else (),
        )}

    if normalized_path.endswith("/api/monitoring/archived-keywords"):
        missing = "keywords" not in included
        archived = [row for row in monitoring.get("keywords") or [] if row.get("archived_at")]
        return 200, {"success": True, "keywords": archived, **_snapshot_meta(
            context, missing=missing, fields=("monitoring.keywords",) if missing else (),
        )}

    if normalized_path.endswith("/api/monitoring/identity-reviews"):
        missing = "results" not in included
        items = [
            row for row in monitoring.get("results") or []
            if row.get("identity_review_state") == "pending"
        ] if not missing else []
        return 200, {"items": items, **_snapshot_meta(
            context, missing=missing, fields=("monitoring.results",) if missing else (),
        )}

    config_quote = _requested_id(normalized_path, r"/api/monitoring/client/(\d+)/monitoring-config")
    if config_quote is not None:
        if config_quote not in quote_ids:
            return 404, {"detail": "资源不存在"}
        missing = "config" not in included
        config = next((row for row in monitoring.get("config") or [] if str(row.get("client_id")) == str(config_quote)), {})
        return 200, {"status": "success", **config, **_snapshot_meta(
            context, missing=missing, fields=("monitoring.config",) if missing else (),
        )}

    if normalized_path.endswith("/api/monitoring/schedule"):
        missing = "config" not in included
        config = next(iter(monitoring.get("config") or []), {})
        enabled = config.get("auto_monitor_enabled") if not missing else None
        return 200, {"status": "success", "monitoring_enabled": enabled, "jobs": [], **_snapshot_meta(
            context, missing=missing, fields=("monitoring.schedule",) if missing else (),
        )}

    if normalized_path.endswith("/api/monitoring/platform-weights"):
        return 200, {"status": "success", "weights": {}, "mau_data": {}, **_snapshot_meta(
            context, missing=True, fields=("monitoring.platform_weights",),
        )}

    insight_quote = _requested_id(normalized_path, r"/api/insights/(\d+)")
    if insight_quote is not None:
        if insight_quote not in quote_ids:
            return 404, {"detail": "资源不存在"}
        return 200, {"status": "success", "insights": [], **_snapshot_meta(
            context, missing=True, fields=("portal.insights",),
        )}

    if re.fullmatch(r"/api/reports/?", normalized_path):
        missing = "reports" not in included
        reports = list(monitoring.get("reports") or []) if not missing else []
        payload = {"status": "success", "reports": reports, "items": reports, **_snapshot_meta(
            context, missing=missing, fields=("reports",) if missing else (),
        )}
        if not missing:
            payload["count"] = len(reports)
            payload["total"] = len(reports)
        return 200, payload

    contract = resolve_demo_route_contract(normalized_method, normalized_path)
    # [Review-CTO 2026-07-26 修 · D10 回归] D10 把诊断/报价/写作等面的 disposition 从
    # "snapshot" 改成 "live" 后,本函数(冻结快照运输)对这些面一律 404 —— 于是
    # `_snapshot_fallback`(真 handler 5xx 时的兜底)对**所有 live 面失效**,SSOT v2.0 §8.2 ④
    # "快照降级为 fallback"的承诺只兑现了一半:后端一抖,演示用户看到的是"资源不存在"
    # 而不是带 snapshot_missing 标注的降级视图(§8.2 明禁整页空白/纯错误码)。
    # live 面在这里同样按快照渲染:surface 白名单与脱敏逻辑在下方共用,不放宽任何字段;
    # preview / safe_handler 仍然拒绝(它们本就不该有快照视图)。
    if not contract or contract.disposition not in ("snapshot", "live"):
        return 404, {"detail": "资源不存在"}

    surfaces: dict[str, Any] = {
        "customer_overview": {
            "brand": snapshot["customer_overview"],
            "profile": {"industry": snapshot["customer_overview"].get("industry")},
        },
        "diagnosis": {
            "record": snapshot["diagnosis_snapshot"],
            "diagnosis": snapshot["diagnosis_snapshot"],
            "records": [snapshot["diagnosis_snapshot"]] if snapshot["diagnosis_snapshot"] else [],
        },
        "quotes": {"items": snapshot["quote_snapshots"], "quotes": snapshot["quote_snapshots"]},
        "writing": {
            **snapshot["writing_snapshots"],
            "projects": snapshot["writing_snapshots"].get("generations", []),
            "items": snapshot["writing_snapshots"].get("articles", []),
        },
        "publish": {
            "items": snapshot["publish_snapshots"],
            "publications": snapshot["publish_snapshots"],
            "orders": snapshot["publish_snapshots"],
            "batches": [],
        },
        "monitoring": {"items": snapshot["monitoring_snapshots"], "tasks": snapshot["monitoring_snapshots"]},
        "reports": {"items": snapshot["report_snapshots"], "reports": snapshot["report_snapshots"]},
        "portal": {},
    }
    surface = surfaces[contract.surface]
    items = surface.get("items", []) if isinstance(surface, dict) else []
    requirements = {
        "customer_overview": ("customer_overview",),
        "diagnosis": ("diagnosis",),
        "quotes": ("quotes",),
        "writing": ("writing.generations", "writing.articles"),
        "publish": ("publish.publications",),
        "monitoring": ("monitoring.tasks",),
        "reports": ("reports",),
    }[contract.surface]
    surface_included = set(snapshot.get("included") or [])
    missing_fields = tuple(key for key in requirements if key not in surface_included)
    payload = {
        "success": True,
        "status": "success",
        "route_contract": contract.action,
        "surface": surface,
        "data": surface,
        "items": items,
        "context": surface if contract.surface == "customer_overview" else None,
        **surface,
        **_snapshot_meta(context, missing=bool(missing_fields), fields=missing_fields),
    }
    primary_items = {
        "quotes": "quotes",
        "writing": "writing.articles",
        "publish": "publish.publications",
        "monitoring": "monitoring.tasks",
        "reports": "reports",
    }.get(contract.surface)
    if primary_items is not None and primary_items in surface_included:
        payload["total"] = len(items)
    return 200, payload


# ---------------------------------------------------------------------------
# [D10] 出站脱敏 —— §2.4 隐私硬边界
# ---------------------------------------------------------------------------
# 真 handler 返回的是 owner 视角的完整 DTO,里面可能带上游身份、真实成本、渠道
# 系数、portal token 明文、联系方式。演示用户**不得**看到这些(§2.4 对普通用户
# 和服务商都是硬边界,演示身份只会更严)。这里按**键名**递归剥除。
#
# 口径与既有脱敏保持一致:`strip_private_context`(报价私有上下文)、
# `_strip_internal_pricing_fields`(词级内部字段)、`DEMO_SNAPSHOT_FIELDS`(白名单)。
# 本表是它们的**出站兜底**:真 handler 的 DTO 形态不受快照白名单约束,必须有一层
# 与路由无关的键名清洗,否则新加的字段会默认泄漏(fail-open)。
DEMO_PRIVATE_KEY_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?:^|_)(?:upstream|parent|superior)_(?:agent|user|id)", re.I),
    re.compile(r"^(?:parent|upstream)_agent(?:_id|_name)?$", re.I),
    re.compile(r"^(?:sv|ch)_(?:code|no|number)$", re.I),
    re.compile(r"^agent_(?:code|no|serial)$", re.I),
    re.compile(r"(?:^|_)cost(?:_|$)", re.I),
    re.compile(r"^(?:base|platform|purchase|procurement|wholesale)_(?:cost|price)", re.I),
    re.compile(r"multiplier_bps$|_bps$", re.I),
    re.compile(r"^(?:markup|coefficient|margin|profit|gross_profit)", re.I),
    re.compile(r"(?:^|_)(?:markup|coefficient)(?:_|$)", re.I),
    re.compile(r"(?:^|_)(?:portal_token|share_token|access_token|refresh_token)(?:_|$)", re.I),
    re.compile(r"^(?:token|secret|password|credential|api_key|signature)$", re.I),
    re.compile(r"(?:^|_)(?:secret|password|api_key|private_key)(?:_|$)", re.I),
    re.compile(r"(?:^|_)(?:phone|mobile|telephone|email|wechat|qq|contact_(?:phone|email|name))(?:_|$)", re.I),
    re.compile(r"^(?:id_card|identity_no|bank_account|bank_card)$", re.I),
)

# 明确保留的键:名字命中上面的模式,但对演示阅读体验是必要且不敏感的。
DEMO_PRIVATE_KEY_ALLOWLIST: frozenset = frozenset({
    "token_fingerprint",   # 指纹不是明文凭证,快照本就暴露它
    "cost_level",          # 定性档位(高/中/低),非金额
})


def _is_private_demo_key(key: str) -> bool:
    if key in DEMO_PRIVATE_KEY_ALLOWLIST:
        return False
    return any(pattern.search(key) for pattern in DEMO_PRIVATE_KEY_PATTERNS)


def scrub_demo_payload(value: Any, _depth: int = 0) -> Any:
    """递归剥除 §2.4 隐私硬边界键(演示出站兜底)。

    - 命中键名 → 整个值丢弃(不保留 masked 残值,避免"看得出量级"的侧信道);
    - 字符串值 → 复用 `_redact_demo_text` 洗掉正文里的邮箱/手机号/token=xxx;
    - 深度上限防御畸形/自引用结构导致的递归爆栈。
    """
    if _depth > 24:
        return value
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_private_demo_key(key):
                continue
            cleaned[key] = scrub_demo_payload(item, _depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [scrub_demo_payload(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_demo_text(value)
    return value


async def scrub_demo_response(response: Response) -> Response:
    """把真 handler 的响应过一遍出站脱敏,非 JSON 原样放行(但标演示头)。

    只处理 JSON 体。SSE/流式响应在演示上下文里属于副作用 GET,已在写闸被拒,
    不会走到这里;若将来出现只读流式面,应显式登记契约再处理。
    """
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/json" not in content_type:
        response.headers["X-Demo-Mode"] = "demo"
        response.headers["X-Demo-Data-Source"] = "live"
        return response
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
    try:
        payload = json.loads(body.decode("utf-8") or "null")
    except Exception:
        # 解析不了就**不放行原文**(可能含未脱敏字段)——fail-closed 成空对象。
        logger.warning("[D10] demo 响应非法 JSON,已 fail-closed 丢弃原文")
        payload = {}
    cleaned = scrub_demo_payload(payload)
    scrubbed = _demo_json(status_code=response.status_code, content=cleaned)
    for key, value in response.headers.items():
        if key.lower() in {"content-length", "content-type"}:
            continue
        scrubbed.headers[key] = value
    scrubbed.headers["X-Demo-Mode"] = "demo"
    scrubbed.headers["X-Demo-Data-Source"] = "live"
    scrubbed.headers["Cache-Control"] = "private, no-store, max-age=0"
    return scrubbed


async def demo_portal_live_read(
    app_instance, *, path: str, query: dict, portal_token: str,
) -> tuple:
    """[工单 2026-07-29 T2] 在同一个 ASGI app 内发一次**只读子请求**读真门户数据。

    Owner 拍板「不做脱敏预览版 —— 演示案例的门户就是真门户」。所以演示门户的数据
    不再由 `snapshot_transport_payload` 另组一套冻结视图(那正是 demo_access 模块头
    自己点名的事故#6「另做缩水演示页」),而是**同路由同代码**走真 handler:

      · 子请求带该 quote 当前有效的**真实门户 token**,与客户手上那条链路逐字相同
        —— 鉴权、RBAC、PORTAL_ALLOWED_PREFIXES 只读白名单全部照旧生效;
      · 明文 token 只存在于服务端这一次内部调用里,**不出网**;前端拿到的始终是
        与 grant/观众/quote 绑定、随授权到期即失效的 `D...` 句柄;
      · 出网前统一过 `scrub_demo_payload`(§2.4 隐私硬边界),白标脱敏照旧。

    返 (status_code, payload_or_None)。非 JSON / 解析失败一律返 None 让调用方回落快照。
    """
    from urllib.parse import urlencode

    messages: list = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        messages.append(message)

    raw_query = urlencode(query, doseq=False).encode("utf-8")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": raw_query,
        "root_path": "",
        "headers": [
            (b"host", b"internal.demo"),
            (b"authorization", f"Bearer {portal_token}".encode("utf-8")),
            (b"accept", b"application/json"),
            # 供下游识别这是演示内部只读子请求(便于日志区分),不承担任何授权语义。
            (b"x-internal-demo-portal", b"1"),
        ],
        "client": ("127.0.0.1", 0),
        "server": ("internal.demo", 80),
        "state": {},
    }
    await app_instance(scope, _receive, _send)

    status_code = 502
    body = b""
    content_type = ""
    for message in messages:
        if message.get("type") == "http.response.start":
            status_code = int(message.get("status") or 502)
            for key, value in message.get("headers") or []:
                if key.lower() == b"content-type":
                    content_type = value.decode("latin-1").lower()
        elif message.get("type") == "http.response.body":
            body += message.get("body") or b""
    if "application/json" not in content_type:
        return status_code, None
    try:
        return status_code, json.loads(body.decode("utf-8") or "null")
    except Exception:
        return status_code, None


def register_demo_access(request: Request, context: DemoAccessContext) -> None:
    request.state.demo_access_context = context
    request.state.demo_active_brand_id = context.brand_id


def register_demo_list_access(request: Request, contexts: Iterable[DemoAccessContext]) -> None:
    for context in contexts:
        record_demo_access_event(
            context, action=_action_name(request), request_id=_request_id(request),
            ip_address=_source_ip(request),
        )


def authorize_demo_request(request: Request, context: DemoAccessContext) -> None:
    """Compatibility adapter: only dedicated snapshot surfaces are authority."""
    register_demo_access(request, context)
    contract = resolve_demo_route_contract(request.method, request.url.path)
    if not contract or contract.disposition != "snapshot":
        blocked_reason = "DEMO_SIDE_EFFECT_BOUNDARY" if contract else "DEMO_ROUTE_UNMAPPED"
        record_demo_access_event(
            context, action=contract.action if contract else _action_name(request),
            request_id=_request_id(request), ip_address=_source_ip(request),
            blocked_reason=blocked_reason,
        )
        if contract and contract.disposition == "preview":
            raise HTTPException(
                status_code=409,
                detail=demo_preview_contract(request, context, blocked_reason, contract),
                headers={"X-Error-Code": "DEMO_ACTION_PREVIEW", "Cache-Control": "no-store"},
            )
        raise HTTPException(status_code=404, detail="资源不存在")


def _has_real_brand_access(request: Request, user_id: int, brand_id: int) -> bool:
    """Prove live ownership/assignment before honoring a stale demo selection."""
    user = getattr(request.state, "user", None) or {}
    if user.get("is_admin"):
        return True
    try:
        if int(brand_id) in {int(item) for item in user.get("client_brand_ids", [])}:
            return True
    except (TypeError, ValueError):
        pass
    organization_identity = getattr(request.state, "organization_identity", None)
    if organization_identity is None:
        try:
            from db.organization_db import resolve_identity
            organization_identity = resolve_identity(int(user_id), request_id=_request_id(request))
        except Exception:
            organization_identity = None
    if organization_identity is not None:
        try:
            from db.organization_db import assigned_brand_ids
            if int(brand_id) in assigned_brand_ids(organization_identity):
                return True
        except Exception:
            pass
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM public.brands
                        WHERE id=%s AND owner_user_id=%s
                          AND (is_deleted IS NULL OR is_deleted=FALSE)
                   ) AS owned""",
                (int(brand_id), int(user_id)),
            )
            return bool(cur.fetchone()["owned"])
    except Exception:
        return False


class DemoSafeResponseMiddleware(BaseHTTPMiddleware):
    """Early demo request router; ordinary handlers never receive demo-only calls."""

    async def dispatch(self, request: Request, call_next):
        selected_brand = request.headers.get("X-Demo-Brand-ID")
        selected_case = request.headers.get("X-Demo-Case-ID")
        if not selected_brand and not selected_case:
            return await call_next(request)
        if not selected_brand or not selected_case:
            return _demo_json(status_code=404, content={"detail": "资源不存在"}, headers={"Cache-Control": "no-store"})
        user = getattr(request.state, "user", None)
        if not user:
            return _demo_json(status_code=404, content={"detail": "资源不存在"}, headers={"Cache-Control": "no-store"})
        try:
            user_id = int(user.get("user_id") or current_user_id(user) or 0)
            brand_id = int(selected_brand)
        except (TypeError, ValueError):
            return _demo_json(status_code=404, content={"detail": "资源不存在"}, headers={"Cache-Control": "no-store"})
        if not user_id or brand_id <= 0:
            return _demo_json(status_code=404, content={"detail": "资源不存在"}, headers={"Cache-Control": "no-store"})
        # Real authority always wins. A stale demo selection must never downgrade
        # an owner, assigned operator, commercial assignee or administrator.
        if _has_real_brand_access(request, user_id, brand_id):
            return await call_next(request)
        context = resolve_demo_case_access(user_id, selected_case)
        if not context or context.brand_id != brand_id:
            historical = resolve_demo_access_history(user_id, brand_id)
            if historical:
                record_demo_access_event(
                    historical, action=_action_name(request), request_id=_request_id(request),
                    ip_address=_source_ip(request), blocked_reason="DEMO_GRANT_INACTIVE",
                )
            return _demo_json(status_code=404, content={"detail": "资源不存在"}, headers={"Cache-Control": "no-store"})
        register_demo_access(request, context)
        # Dedicated case handlers read only the projected snapshot and own their
        # list/detail/export/WS audit semantics.
        if request.url.path.startswith("/api/demo-cases"):
            return await call_next(request)
        contract = resolve_demo_route_contract(request.method, request.url.path)
        if not contract:
            # [D10 · SSOT v2.0 §8.2] 未映射的**只读**请求不再 404。
            #   v1.x 是白名单默认拒 → 演示用户打不开未登记的功能面(= 事故#6
            #   "另做缩水演示页")。D10 要求"任何功能面都能看到真实数据",故只读
            #   请求默认放行进真 handler;租户隔离由**唯一授权层**
            #   auth.brand_access(require_*_access / get_user_brand_filter)承担,
            #   越界照旧 404/403 fail-closed,不在此另写第二套弱校验。
            #   写方法不会走到这里:resolve_demo_route_contract 对非 GET/HEAD/OPTIONS
            #   一律返回 catch-all preview 契约。
            record_demo_access_event(
                context, action=_action_name(request), request_id=_request_id(request),
                ip_address=_source_ip(request),
            )
            return await self._live_projection(request, call_next, context, None)
        if contract.disposition == "preview":
            record_demo_access_event(
                context, action=contract.action, request_id=_request_id(request),
                ip_address=_source_ip(request), blocked_reason="DEMO_SIDE_EFFECT_BOUNDARY",
            )
            return _demo_json(
                status_code=409,
                content={"detail": demo_preview_contract(
                    request, context, "DEMO_SIDE_EFFECT_BOUNDARY", contract,
                )},
                headers={"X-Error-Code": "DEMO_ACTION_PREVIEW", "Cache-Control": "private, no-store, max-age=0"},
            )
        if contract.disposition == "safe_handler":
            record_demo_access_event(
                context, action=contract.action, request_id=_request_id(request), ip_address=_source_ip(request),
            )
            return await call_next(request)
        record_demo_access_event(
            context, action=contract.action, request_id=_request_id(request), ip_address=_source_ip(request),
        )
        if contract.disposition == "frozen_snapshot":
            # 门户凭证等**必须**冻结的面:继续只从快照投影,绝不进真 handler
            # (真 handler 会返回真实 portal token 明文 = §2.4 隐私硬边界)。
            return self._snapshot_response(context, request)
        # [D10] 其余读面(诊断/报价/写作/发布/监测/报告/客户总览)默认走真实
        # handler 的实时投影;快照降级为数据源不可用时的 fallback。
        return await self._live_projection(request, call_next, context, contract)

    def _snapshot_response(self, context: DemoAccessContext, request: Request) -> Response:
        status_code, payload = snapshot_transport_payload(
            context,
            method=request.method,
            path=request.url.path,
            query=request.query_params,
        )
        return _demo_json(
            status_code=status_code, content=payload,
            headers={
                "X-Demo-Mode": "demo",
                "X-Demo-Data-Source": "frozen_snapshot",
                "Cache-Control": "private, no-store, max-age=0",
            },
        )

    async def _live_projection(
        self, request: Request, call_next, context: DemoAccessContext,
        contract: Optional[DemoRouteContract],
    ) -> Response:
        """[D10] 受控实时只读投影:真 handler 出数 → 出站脱敏 → 异常回落快照。

        - **同路由同代码**:请求原样进真 handler,拿到的就是 owner 当天看到的数据;
        - **租户隔离**:由 auth.brand_access 唯一授权层承担(演示品牌只读白名单),
          越界 404/403 原样透出,本方法不放宽;
        - **出站脱敏**:§2.4 隐私硬边界字段统一剥除后才出网;
        - **fallback**:真 handler 5xx 或抛异常时,若该面有快照能力则回落快照,
          保证演示不出现整页空白/红码(事故#6),并标注数据源。
        """
        try:
            response = await call_next(request)
        except Exception:
            fallback = self._snapshot_fallback(context, request, contract)
            if fallback is not None:
                return fallback
            raise
        if response.status_code >= 500:
            fallback = self._snapshot_fallback(context, request, contract)
            if fallback is not None:
                return fallback
        return await scrub_demo_response(response)

    def _snapshot_fallback(
        self, context: DemoAccessContext, request: Request,
        contract: Optional[DemoRouteContract],
    ) -> Optional[Response]:
        if contract is None or contract.surface in ("", "none"):
            return None
        try:
            response = self._snapshot_response(context, request)
        except Exception:
            return None
        response.headers["X-Demo-Data-Source"] = "frozen_snapshot_fallback"
        return response


def setup_demo_access_middleware(app) -> None:
    app.add_middleware(DemoSafeResponseMiddleware)


def context_public_dict(context: DemoAccessContext) -> dict[str, Any]:
    value = asdict(context)
    value.pop("snapshot", None)
    return value
