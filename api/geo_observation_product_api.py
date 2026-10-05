"""Read-only product API for the GEO unified observation flywheel (AI-3).

Three audiences, three privacy postures:
  * private brand   -> /api/geo-observation/brands/{brand_id}/...   (brand-owner RBAC)
  * public industry -> /api/geo-observation/industries/{key}/...    (k-anonymity gate)
  * admin           -> /api/admin/geo-observation/...               (admin RBAC)

The router is produced by :func:`build_routers(deps)` and exported ready to
register; it never edits ``server.py``, the scheduler, or the sidebar. Every
metric comes from the deterministic backend; the LLM only explains verified
facts. All non-admin responses pass a privacy leak guard before returning.

Integration note (documented seam, NOT applied here): the final integrator must
add ``("/api/geo-observation/", None)`` to ``auth/module_mapping.py``
``ROUTE_PREFIX_MAP`` so authenticated non-admin service providers can reach the
private/public routes (each endpoint still enforces brand scope / k-anonymity
itself). The admin prefix is admin-gated by the global middleware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from schemas import geo_observation_product as dto
from services.geo_observation_analytics import (
    contract as C,
    explain,
    opportunities as opp,
    privacy,
    repository as repo,
    trends,
)
from services.geo_observation_analytics.errors import http_error
from services.geo_observation_analytics.evidence import (
    EvidenceSource,
    SourceRef,
    SourceTableEvidenceResolver,
)
from auth.user_ctx import current_user_id

# reconciler backlog older than this pushes admin readiness to attention_required
_RECONCILER_DELAY_ATTENTION_SECONDS = 300

_STABILITY_EXPLANATION = {
    "stable": "结果较稳定。",
    "watch": "结果有波动，建议继续观察。",
    "insufficient": "样本不足，暂不下结论。",
    "shifted": "部分平台模型升级，前后结果不宜直接比较。",
}


# ===========================================================================
# Dependencies
# ===========================================================================
@dataclass
class ProductApiDeps:
    get_conn: Callable[[], object]
    evidence_source: EvidenceSource
    insight_engine: explain.InsightEngine
    # cross-package injected read models (AI-1 health / AI-2 policy). Default to
    # empty/degraded so the API never fabricates other packages' data.
    platform_health_provider: Callable[[], list[dict]] = field(default=lambda: [])
    policy_provider: Callable[[], dict] = field(default=lambda: {"policy_version": None})
    # AI-1 overall control-plane readiness (integrator composes it from
    # scheduler_wiring.check_readiness() + registry.readiness()). Default None =
    # NOT wired => admin readiness is fail-closed to 'unavailable' (a collection
    # pipeline that cannot report itself running must never read 'ready').
    collection_readiness_provider: Optional[Callable[[], dict]] = None
    # Active-basis manifest freshness. Production injects a PostgreSQL-backed
    # exact watermark/count check; unwired is fail-closed for product routes.
    aggregate_readiness_provider: Optional[Callable[[str], dict]] = None
    kanon: privacy.KAnonThresholds = field(default_factory=privacy.KAnonThresholds)
    default_granularity: str = "day"


def default_deps() -> ProductApiDeps:
    from db.connection import get_connection

    return ProductApiDeps(
        get_conn=get_connection,
        evidence_source=SourceTableEvidenceResolver(),
        insight_engine=explain.InsightEngine(transport=explain.OfficialDeepSeekTransport()),
    )


# ===========================================================================
# Auth helpers
# ===========================================================================
def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise http_error("FORBIDDEN")
    return user


def _require_admin(request: Request) -> dict:
    user = _get_user(request)
    if not user.get("is_admin"):
        raise http_error("FORBIDDEN")
    return user


def _check_brand_access(request: Request, brand_id: int) -> None:
    """Reuse the canonical brand-owner RBAC; normalize its 401/403 into the
    product error envelope so the whole API speaks one error dialect."""
    user = _get_user(request)  # unauthenticated -> FORBIDDEN before any brand read
    # [Review-CTO 2026-07-26 修 · SSOT §6.2/§6.3 · Owner 生产实测]
    # 原实现故意取消 admin 旁路,要求"该品牌显式分配给这个 admin 账号"才放行。
    # 但 admin 账号本来就没有 client_brand_ids 分配(那是团队分配字段)——于是
    # **管理员访问任何品牌都恒 403**,连自己平台的诊断数据都看不到。
    # SSOT §6.3 把「为普通用户准备的租户范围限制直接套在 ADMIN 上」明列为禁止的
    # 死胡同形态;§6.2 明确 ADMIN 是平台治理者、可治理任何合法业务事实。
    # 正确边界是**留痕**而非阻断:admin 跨品牌只读放行,并记审计事件。
    if user.get("is_admin"):
        try:
            from db.auth_db import create_audit_log

            create_audit_log(
                user_id=int(user.get("user_id") or current_user_id(user) or 0),
                username=user.get("username"),
                action="geo_observation.admin_cross_brand_read",
                module="geo_observation",
                entity_type="brand",
                entity_id=int(brand_id),
                summary=f"管理员跨品牌只读观测数据 brand={brand_id}",
                after={"route": str(request.url.path)},
            )
        except Exception:
            # 审计不可用不得阻断治理读取(网是留痕,不是闸)。
            pass
        return
    from auth.brand_access import require_brand_access

    try:
        require_brand_access(request, brand_id)
    except HTTPException as exc:
        if exc.status_code in (401, 403):
            raise http_error("FORBIDDEN") from exc
        raise


# ===========================================================================
# Shared builders
# ===========================================================================
def _window_dto(bucket_start, bucket_end, granularity: str) -> dto.WindowDTO:
    label = {"day": "当日", "week": "本周", "month": "本月"}.get(granularity, "近期")
    return dto.WindowDTO(label=label, start=str(bucket_start), end=str(bucket_end))


def _iso(v) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _summary_metrics(row: dict) -> dto.BrandSummaryMetricsDTO:
    return dto.BrandSummaryMetricsDTO(
        valid_observations=int(row["valid_observations"]),
        presence_rate_bps=int(row["presence_rate_bps"]),
        explicit_recommendation_rate_bps=int(row["explicit_recommendation_rate_bps"]),
        conditional_recommendation_rate_bps=int(row["conditional_recommendation_rate_bps"]),
        candidate_rate_bps=int(row["candidate_rate_bps"]),
        criteria_only_rate_bps=int(row["criteria_only_rate_bps"]),
        refusal_no_evidence_rate_bps=int(row["refusal_no_evidence_rate_bps"]),
        refusal_risk_rate_bps=int(row["refusal_risk_rate_bps"]),
        not_mentioned_rate_bps=int(row["not_mentioned_rate_bps"]),
        citation_rate_bps=int(row["citation_rate_bps"]),
        evidence_coverage_rate_bps=int(row["evidence_coverage_rate_bps"]),
        share_of_voice_bps=(None if row["share_of_voice_bps"] is None else int(row["share_of_voice_bps"])),
        stability_status=row["stability_status"],
        stability_explanation=_STABILITY_EXPLANATION.get(row["stability_status"], ""),
    )


def _outcomes(row: dict) -> list[dto.OutcomeCountDTO]:
    items = []
    for outcome in C.VALID_OUTCOMES:
        col = f"{outcome}_count"
        if col in row:
            items.append(dto.OutcomeCountDTO(outcome=outcome, count=int(row[col])))
    return items


def _next_actions(row: dict) -> list[dto.NextActionDTO]:
    actions: list[dto.NextActionDTO] = []
    refused = int(row["refused_no_evidence_count"])
    if int(row["refusal_no_evidence_rate_bps"]) >= 1500 and refused > 0:
        actions.append(dto.NextActionDTO(
            action="add_evidence", title="补充可核验的项目案例",
            reason=f"{refused} 个问题因证据不足没有给出具体推荐",
            requires_confirmation=True, may_charge=False,
        ))
    if row["stability_status"] == "shifted":
        actions.append(dto.NextActionDTO(
            action="keep_observing", title="继续观察模型升级后的结果",
            reason="当前变化可能来自模型升级，不宜立刻判断为效果下降",
            requires_confirmation=False, may_charge=False,
        ))
    if not actions:
        actions.append(dto.NextActionDTO(
            action="keep_observing", title="继续观察",
            reason="维持当前节奏并持续采集，样本积累后再判断趋势",
            requires_confirmation=False, may_charge=False,
        ))
    return actions


def _empty_summary(brand_ref: dto.BrandRefDTO, granularity: str) -> dto.BrandSummaryDTO:
    today = date.today()
    return dto.BrandSummaryDTO(
        brand=brand_ref,
        window=_window_dto(today, today, granularity),
        data_updated_at=None,
        metric_version=C.METRIC_VERSION,
        summary=dto.BrandSummaryMetricsDTO(
            valid_observations=0,
            # no fabricated 0% — every rate is null ("no data"), not 0.
            presence_rate_bps=None, explicit_recommendation_rate_bps=None,
            conditional_recommendation_rate_bps=None, candidate_rate_bps=None,
            criteria_only_rate_bps=None, refusal_no_evidence_rate_bps=None,
            refusal_risk_rate_bps=None, not_mentioned_rate_bps=None,
            citation_rate_bps=None, evidence_coverage_rate_bps=None, share_of_voice_bps=None,
            stability_status="insufficient",
            stability_explanation=_STABILITY_EXPLANATION["insufficient"],
        ),
        comparison=dto.ComparisonDTO(comparison_allowed=False, reason="insufficient_history"),
        outcomes=[],
        next_actions=[dto.NextActionDTO(
            action="keep_observing", title="继续观察",
            reason="尚无足够观测数据，持续采集后再判断", requires_confirmation=False, may_charge=False,
        )],
    )


def _brand_ref_or_forbidden(
    conn, brand_id: int, policy_basis_hash: str, *, granularity: Optional[str] = None,
) -> dict:
    _begin_exact_snapshot(conn, policy_basis_hash)
    bref = repo.brand_ref(conn, brand_id)
    if bref is None or bref["owner_user_id"] is None:
        raise http_error("FORBIDDEN")
    if granularity is not None:
        _require_live_retention(
            conn, policy_basis_hash=policy_basis_hash,
            owner_user_id=int(bref["owner_user_id"]), brand_id=brand_id,
            granularity=granularity, integrity_checked=True,
        )
    return bref


# ===========================================================================
# Router factory
# ===========================================================================
def build_routers(deps: ProductApiDeps) -> tuple[APIRouter, APIRouter]:
    product = APIRouter(prefix="/api/geo-observation", tags=["GEO 观测洞察"])
    admin = APIRouter(prefix="/api/admin/geo-observation", tags=["GEO 观测治理"])

    # ---------------- private brand ----------------
    @product.get("/brands/{brand_id}/summary", response_model=dto.BrandSummaryDTO)
    def brand_summary(request: Request, brand_id: int, granularity: str = Query("day", pattern="^(day|week|month)$")):
        _check_brand_access(request, brand_id)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            bref = _brand_ref_or_forbidden(conn, brand_id, pv.aggregate_policy_basis, granularity=granularity)
            owner = bref["owner_user_id"]
            brand_ref = dto.BrandRefDTO(brand_id=brand_id, display_name=bref["name"])
            latest = repo.latest_overall_aggregate(
                conn, scope_type="private_brand", granularity=granularity,
                owner_user_id=owner, brand_id=brand_id,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            if latest is None:
                out = _empty_summary(brand_ref, granularity)
                privacy.assert_no_private_leak(out.model_dump())
                return out
            prev = repo.previous_overall_aggregate(
                conn, scope_type="private_brand", granularity=granularity,
                before_bucket_start=latest["bucket_start"], owner_user_id=owner, brand_id=brand_id,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            cmp = trends.compare_periods(latest, prev)
            out = dto.BrandSummaryDTO(
                brand=brand_ref,
                window=_window_dto(latest["bucket_start"], latest["bucket_end"], granularity),
                data_updated_at=_iso(latest["input_watermark"]),
                metric_version=C.METRIC_VERSION,
                summary=_summary_metrics(latest),
                comparison=dto.ComparisonDTO(
                    presence_change_bps=cmp.presence_change_bps,
                    recommendation_change_bps=cmp.recommendation_change_bps,
                    comparison_allowed=cmp.comparison_allowed, reason=cmp.reason,
                ),
                outcomes=_outcomes(latest),
                next_actions=_next_actions(latest),
            )
            privacy.assert_no_private_leak(out.model_dump())
            return out
        finally:
            conn.close()

    @product.get("/brands/{brand_id}/trend", response_model=dto.BrandTrendDTO)
    def brand_trend(request: Request, brand_id: int, granularity: str = Query("day", pattern="^(day|week|month)$")):
        _check_brand_access(request, brand_id)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            bref = _brand_ref_or_forbidden(conn, brand_id, pv.aggregate_policy_basis, granularity=granularity)
            owner = bref["owner_user_id"]
            series = repo.trend_series(
                conn, scope_type="private_brand", granularity=granularity,
                owner_user_id=owner, brand_id=brand_id,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            points = trends.build_trend_points(
                series,
                numerator=C.DEFAULT_ANOMALY_CONFIRMATION_NUMERATOR,
                denominator=C.DEFAULT_ANOMALY_CONFIRMATION_DENOMINATOR,
            )
            if series:
                window = _window_dto(series[0]["bucket_start"], series[-1]["bucket_end"], granularity)
            else:
                today = date.today()
                window = _window_dto(today, today, granularity)
            out = dto.BrandTrendDTO(
                brand=dto.BrandRefDTO(brand_id=brand_id, display_name=bref["name"]),
                window=window, metric_version=C.METRIC_VERSION, granularity=granularity,
                points=[dto.TrendPointDTO(**p.__dict__) for p in points],
                comparison_note=("部分区间模型升级，跨升级点不宜直接比较。"
                                 if any(p.model_shift_marker for p in points) else None),
            )
            privacy.assert_no_private_leak(out.model_dump())
            return out
        finally:
            conn.close()

    @product.get("/brands/{brand_id}/platforms", response_model=dto.BrandPlatformsDTO)
    def brand_platforms(request: Request, brand_id: int, granularity: str = Query("day", pattern="^(day|week|month)$")):
        _check_brand_access(request, brand_id)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            bref = _brand_ref_or_forbidden(conn, brand_id, pv.aggregate_policy_basis, granularity=granularity)
            owner = bref["owner_user_id"]
            latest = repo.latest_overall_aggregate(
                conn, scope_type="private_brand", granularity=granularity,
                owner_user_id=owner, brand_id=brand_id,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            items: list[dto.PlatformItemDTO] = []
            if latest is not None:
                rows = repo.platform_aggregates_for_bucket(
                    conn, scope_type="private_brand", granularity=granularity,
                    bucket_start=latest["bucket_start"], owner_user_id=owner, brand_id=brand_id,
                    policy_basis_hash=pv.aggregate_policy_basis,
                )
                for r in rows:
                    pk = r["platform_key"]
                    if pk in C.HISTORICAL_PLATFORM_KEYS:
                        continue
                    items.append(dto.PlatformItemDTO(
                        platform_key=pk,
                        display_name=C.PLATFORM_DISPLAY_NAMES.get(pk, pk),
                        # user-facing shows only the product platform name; exact
                        # surface/provider (incl. proxy channels) is admin-only.
                        surface_note=None,
                        valid_observations=int(r["valid_observations"]),
                        presence_rate_bps=int(r["presence_rate_bps"]),
                        explicit_recommendation_rate_bps=int(r["explicit_recommendation_rate_bps"]),
                        stability_status=r["stability_status"],
                        updated_at=_iso(r["input_watermark"]),
                    ))
            hist_rows = (
                repo.historical_platforms(
                    conn, scope_type="private_brand", owner_user_id=owner,
                    brand_id=brand_id, through_watermark=latest["input_watermark"],
                    through_promotion_sequence=int(latest["promotion_sequence_watermark"]),
                )
                if latest is not None and latest.get("input_watermark") is not None
                else []
            )
            historical = [dto.HistoricalPlatformDTO(
                platform_key=h["platform_key"],
                display_name=C.PLATFORM_DISPLAY_NAMES.get(h["platform_key"], h["platform_key"]),
                status="historical", last_observed_at=_iso(h["last_observed_at"]),
            ) for h in hist_rows]
            out = dto.BrandPlatformsDTO(items=items, historical_platforms=historical)
            privacy.assert_no_private_leak(out.model_dump())
            return out
        finally:
            conn.close()

    @product.get("/brands/{brand_id}/questions", response_model=dto.QuestionsPageDTO)
    def brand_questions(
        request: Request, brand_id: int,
        page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    ):
        _check_brand_access(request, brand_id)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            bref = _brand_ref_or_forbidden(conn, brand_id, pv.aggregate_policy_basis, granularity="day")
            owner = bref["owner_user_id"]
            latest = repo.latest_overall_aggregate(
                conn, scope_type="private_brand", granularity="day",
                owner_user_id=owner, brand_id=brand_id,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            watermark = latest.get("input_watermark") if latest is not None else None
            sequence = latest.get("promotion_sequence_watermark") if latest is not None else None
            if watermark is None or sequence is None:
                return dto.QuestionsPageDTO(
                    page=page, page_size=page_size, total=0, items=[]
                )
            total = repo.count_brand_questions(
                conn, owner, brand_id, through_watermark=watermark,
                through_promotion_sequence=int(sequence),
            )
            rows = repo.list_brand_questions(
                conn, owner, brand_id, page, page_size,
                through_watermark=watermark,
                through_promotion_sequence=int(sequence),
            )
            refs = [SourceRef(
                observation_id=r["observation_id"], source_type="", source_table="",
                source_record_id="", source_subkey="",
                platform_key=r.get("platform_key"), surface_key=r.get("surface_key"),
            ) for r in rows]
            # resolve question text for this page only (owner's own data)
            texts = _resolve_question_texts(
                conn, deps, owner, brand_id, rows,
                through_watermark=watermark,
                through_promotion_sequence=int(sequence),
            )
            items = []
            for r in rows:
                t = texts.get(r["observation_id"])
                items.append(dto.QuestionItemDTO(
                    observation_id=r["observation_id"],
                    question=(t.question if t else ""),
                    outcome=r["outcome"],
                    matched_text=(t.matched_text if t else None),
                    position=r["position"],
                    citations=int(r["citations"] or 0),
                    changed=bool(r["changed"]),
                    evidence_available=bool(r["evidence_available"]) and t is not None,
                ))
            out = dto.QuestionsPageDTO(page=page, page_size=page_size, total=total, items=items)
            privacy.assert_no_private_leak(out.model_dump())
            return out
        finally:
            conn.close()

    @product.get("/brands/{brand_id}/evidence/{observation_id}", response_model=dto.EvidenceDetailDTO)
    def brand_evidence(request: Request, brand_id: int, observation_id: str):
        _check_brand_access(request, brand_id)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            bref = _brand_ref_or_forbidden(conn, brand_id, pv.aggregate_policy_basis, granularity="day")
            owner = bref["owner_user_id"]
            latest = repo.latest_overall_aggregate(
                conn, scope_type="private_brand", granularity="day",
                owner_user_id=owner, brand_id=brand_id,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            watermark = latest.get("input_watermark") if latest is not None else None
            sequence = latest.get("promotion_sequence_watermark") if latest is not None else None
            if watermark is None or sequence is None:
                raise http_error("FORBIDDEN")
            meta = repo.get_observation_meta(
                conn, owner, brand_id, observation_id,
                through_watermark=watermark,
                through_promotion_sequence=int(sequence),
            )
            if meta is None:
                raise http_error("FORBIDDEN")
            ref = SourceRef(
                observation_id=observation_id, source_type=meta["source_type"],
                source_table=meta["source_table"], source_record_id=str(meta["source_record_id"]),
                source_subkey=meta["source_subkey"], platform_key=meta["platform_key"],
                surface_key=meta["surface_key"],
            )
            resolved = deps.evidence_source.resolve_one(conn, owner, brand_id, ref)
            citations = _citation_dtos(meta.get("source_domains"))
            out = dto.EvidenceDetailDTO(
                observation_id=observation_id,
                question=(resolved.question if resolved else ""),
                outcome=meta["outcome"],
                answer_excerpt=(resolved.answer_excerpt if resolved else None),
                matched_text=(resolved.matched_text if resolved else None),
                citations=citations,
                evidence_gap=_evidence_gap(meta["outcome"]),
                next_action=(dto.EvidenceNextActionDTO(
                    action="add_evidence", title="补充项目证据", may_charge=False)
                    if meta["outcome"] in ("refused_no_evidence", "mentioned_only") else None),
                channel_disclosure=dto.ChannelDisclosureDTO(
                    platform=C.PLATFORM_DISPLAY_NAMES.get(meta["platform_key"], meta["platform_key"] or ""),
                    # user-facing shows only the product platform name; exact
                    # surface/provider (incl. proxy channels) is admin-only.
                    note=None,
                ),
            )
            privacy.assert_no_private_leak(out.model_dump())
            return out
        finally:
            conn.close()

    @product.get("/brands/{brand_id}/opportunities", response_model=dto.OpportunitiesDTO)
    def brand_opportunities(request: Request, brand_id: int, granularity: str = Query("day", pattern="^(day|week|month)$")):
        _check_brand_access(request, brand_id)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            bref = _brand_ref_or_forbidden(conn, brand_id, pv.aggregate_policy_basis, granularity=granularity)
            owner = bref["owner_user_id"]
            latest = repo.latest_overall_aggregate(
                conn, scope_type="private_brand", granularity=granularity,
                owner_user_id=owner, brand_id=brand_id,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            items = opp.derive_opportunities(latest, scope_label=f"b{brand_id}")
            out = dto.OpportunitiesDTO(items=[dto.OpportunityItemDTO(**o.__dict__) for o in items])
            privacy.assert_no_private_leak(out.model_dump())
            return out
        finally:
            conn.close()

    @product.post("/brands/{brand_id}/insights", response_model=dto.InsightJobStatusDTO)
    def create_insight(request: Request, brand_id: int, granularity: str = Query("day", pattern="^(day|week|month)$")):
        _check_brand_access(request, brand_id)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            bref = _brand_ref_or_forbidden(conn, brand_id, pv.aggregate_policy_basis, granularity=granularity)
            owner = bref["owner_user_id"]
            latest = repo.latest_overall_aggregate(
                conn, scope_type="private_brand", granularity=granularity,
                owner_user_id=owner, brand_id=brand_id,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            if latest is None:
                raise http_error("INSUFFICIENT_SAMPLES")
            fact_pack = _fact_pack_from_row(conn, owner, brand_id, latest, granularity)
            snapshot = explain.snapshot_from_aggregate(latest)
            # End the read-only REPEATABLE READ snapshot before the owner budget
            # reservation. create_job starts a fresh READ COMMITTED transaction,
            # acquires the owner lock first, then sees preceding reservations.
            conn.commit()
            import uuid as _uuid
            request_id = request.headers.get("X-Request-Id") or f"req-{_uuid.uuid4().hex[:12]}"
            try:
                job = deps.insight_engine.create_job(
                    conn, fact_pack, request_id, owner, brand_id,
                    snapshot,
                )
            except explain.InsightUnavailableError as exc:
                raise http_error("SEMANTIC_INSIGHT_UNAVAILABLE", retryable=False) from exc
            # create_job's final SELECT opens a READ COMMITTED transaction. End
            # it, then revalidate completed output in a new exact snapshot before
            # the POST response can expose the summary.
            conn.commit()
            if job.get("state") == "completed":
                _begin_exact_snapshot(conn, pv.aggregate_policy_basis)
                _require_live_retention(
                    conn, policy_basis_hash=pv.aggregate_policy_basis,
                    owner_user_id=owner, brand_id=brand_id,
                    granularity=granularity, integrity_checked=True,
                )
                current = repo.latest_overall_aggregate(
                    conn, scope_type="private_brand", granularity=granularity,
                    owner_user_id=owner, brand_id=brand_id,
                    policy_basis_hash=pv.aggregate_policy_basis,
                )
                if current is None or not explain.job_matches_snapshot(
                    job, explain.snapshot_from_aggregate(current)
                ):
                    raise http_error("OBSERVATION_UNAVAILABLE")
            out = _job_dto(job)
            return out
        finally:
            conn.close()

    @product.get("/brands/{brand_id}/insights/{job_id}", response_model=dto.InsightJobStatusDTO)
    def get_insight(request: Request, brand_id: int, job_id: str):
        _check_brand_access(request, brand_id)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            bref = _brand_ref_or_forbidden(conn, brand_id, pv.aggregate_policy_basis)
            # object-level authz: the job must belong to THIS brand's owner, so a
            # job_id from another tenant is not readable via any brand you own.
            job = deps.insight_engine.get_job(conn, job_id, bref["owner_user_id"], brand_id)
            if job is None:
                raise http_error("OBSERVATION_UNAVAILABLE")
            # ``get_job`` may commit an expired-lease self-heal.  Re-enter the
            # exact published snapshot before comparing the durable job lineage
            # with the aggregate returned to this request.
            granularity = job.get("bucket_granularity")
            _begin_exact_snapshot(conn, pv.aggregate_policy_basis)
            _require_live_retention(
                conn,
                policy_basis_hash=pv.aggregate_policy_basis,
                owner_user_id=bref["owner_user_id"],
                brand_id=brand_id, granularity=granularity,
                integrity_checked=True,
            )
            if granularity not in ("day", "week", "month"):
                raise http_error("OBSERVATION_UNAVAILABLE")
            latest = repo.latest_overall_aggregate(
                conn, scope_type="private_brand", granularity=granularity,
                owner_user_id=bref["owner_user_id"], brand_id=brand_id,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            if latest is None or not explain.job_matches_snapshot(
                job, explain.snapshot_from_aggregate(latest)
            ):
                raise http_error("OBSERVATION_UNAVAILABLE")
            return _job_dto(job)
        finally:
            conn.close()

    # ---------------- public industry ----------------
    @product.get("/industries/{industry_key}/baseline", response_model=dto.PublicBaselineResponseDTO)
    def industry_baseline(request: Request, industry_key: str, granularity: str = Query("day", pattern="^(day|week|month)$")):
        _get_user(request)  # any authenticated user
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            _require_live_retention(
                conn, policy_basis_hash=pv.aggregate_policy_basis,
                public_only=True, industry_key=industry_key,
                granularity=granularity,
            )
            kanon = _rate_cell_kanon(deps, pv)
            latest = repo.latest_overall_aggregate(
                conn, scope_type="public_industry", granularity=granularity, industry_key=industry_key,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            scope_label = f"行业「{industry_key}」· {granularity}"
            if latest is None or not privacy.meets_k_anonymity(latest, kanon):
                return dto.PublicBaselineResponseDTO(
                    status="insufficient_samples", industry_key=industry_key,
                    sample_scope=scope_label, message="样本不足，暂不下结论。",
                )
            baseline = dto.IndustryBaselineDTO(
                industry_key=industry_key,
                window=_window_dto(latest["bucket_start"], latest["bucket_end"], granularity),
                metric_version=C.METRIC_VERSION,
                valid_observations=int(latest["valid_observations"]),
                presence_rate_bps=int(latest["presence_rate_bps"]),
                explicit_recommendation_rate_bps=int(latest["explicit_recommendation_rate_bps"]),
                conditional_recommendation_rate_bps=int(latest["conditional_recommendation_rate_bps"]),
                criteria_only_rate_bps=int(latest["criteria_only_rate_bps"]),
                refusal_no_evidence_rate_bps=int(latest["refusal_no_evidence_rate_bps"]),
                refusal_risk_rate_bps=int(latest["refusal_risk_rate_bps"]),
                citation_rate_bps=int(latest["citation_rate_bps"]),
                evidence_coverage_rate_bps=int(latest["evidence_coverage_rate_bps"]),
                stability_status=latest["stability_status"],
                sample_scope=scope_label,
            )
            out = dto.PublicBaselineResponseDTO(
                status="ok", industry_key=industry_key, sample_scope=scope_label, baseline=baseline,
            )
            privacy.assert_no_public_leak(out.model_dump())
            return out
        finally:
            conn.close()

    @product.get("/industries/{industry_key}/source-patterns",
                 response_model=dto.PublicSourcePatternsResponseDTO)
    def industry_source_patterns(request: Request, industry_key: str, granularity: str = Query("day", pattern="^(day|week|month)$")):
        _get_user(request)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            _require_live_retention(
                conn, policy_basis_hash=pv.aggregate_policy_basis,
                public_only=True, industry_key=industry_key,
                granularity=granularity,
            )
            kanon = _effective_kanon(deps, pv)
            latest = repo.latest_overall_aggregate(
                conn, scope_type="public_industry", granularity=granularity, industry_key=industry_key,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            scope_label = f"行业「{industry_key}」· {granularity}"
            if (
                latest is None
                or latest.get("input_watermark") is None
                or not privacy.meets_k_anonymity(latest, kanon)
            ):
                return dto.PublicSourcePatternsResponseDTO(
                    status="insufficient_samples", industry_key=industry_key,
                    sample_scope=scope_label, message="样本不足，暂不下结论。",
                )
            rows = repo.industry_source_patterns(
                conn, industry_key=industry_key,
                bucket_start=latest["bucket_start"], bucket_end=latest["bucket_end"],
                min_user_buckets=kanon.min_independent_user_buckets,
                min_brand_buckets=kanon.min_independent_brand_buckets,
                min_source_types=kanon.min_source_types,
                through_watermark=latest["input_watermark"],
                through_promotion_sequence=int(latest["promotion_sequence_watermark"]),
            )
            patterns = dto.IndustrySourcePatternsDTO(
                industry_key=industry_key,
                window=_window_dto(latest["bucket_start"], latest["bucket_end"], granularity),
                metric_version=C.METRIC_VERSION,
                items=[dto.SourcePatternItemDTO(
                    domain=r["domain"], appearance_count=int(r["appearance_count"]),
                    platforms=int(r["platforms"]),
                    source_type=("citation" if r["source_type"] == "citation" else "source"),
                ) for r in rows],
                sample_scope=scope_label,
            )
            out = dto.PublicSourcePatternsResponseDTO(
                status="ok", industry_key=industry_key, sample_scope=scope_label, patterns=patterns,
            )
            privacy.assert_no_public_leak(out.model_dump())
            return out
        finally:
            conn.close()

    @product.get("/industries/{industry_key}/content-opportunities",
                 response_model=dto.PublicOpportunitiesResponseDTO)
    def industry_content_opportunities(request: Request, industry_key: str, granularity: str = Query("day", pattern="^(day|week|month)$")):
        _get_user(request)
        pv = _require_product_enabled(deps)
        conn = deps.get_conn()
        try:
            _require_live_retention(
                conn, policy_basis_hash=pv.aggregate_policy_basis,
                public_only=True, industry_key=industry_key,
                granularity=granularity,
            )
            kanon = _rate_cell_kanon(deps, pv)
            scope_label = f"行业「{industry_key}」· {granularity}"
            latest = repo.latest_overall_aggregate(
                conn, scope_type="public_industry", granularity=granularity, industry_key=industry_key,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            # distinguish privacy-blocked (insufficient) from genuinely empty (ok, [])
            if latest is None or not privacy.meets_k_anonymity(latest, kanon):
                return dto.PublicOpportunitiesResponseDTO(
                    status="insufficient_samples", industry_key=industry_key,
                    sample_scope=scope_label, message="样本不足，暂不下结论。", items=[],
                )
            items = opp.derive_opportunities(latest, scope_label=f"ind-{industry_key}")
            out = dto.PublicOpportunitiesResponseDTO(
                status="ok", industry_key=industry_key, sample_scope=scope_label,
                items=[dto.OpportunityItemDTO(**o.__dict__) for o in items],
            )
            privacy.assert_no_public_leak(out.model_dump())
            return out
        finally:
            conn.close()

    # ---------------- admin ----------------
    @admin.get("/overview", response_model=dto.AdminOverviewDTO)
    def admin_overview(request: Request):
        _require_admin(request)
        conn = deps.get_conn()
        try:
            counts = repo.event_state_counts(conn)
            pv = _policy_view(deps)
            health, health_provider_ok = _platform_health_view(deps)
            health_items = _admin_health_items(health, pv)
            pending = counts.get("pending_review", 0)
            rejected = counts.get("rejected", 0)
            aggregate_updated = (
                repo.latest_aggregate_computed_at(
                    conn, policy_basis_hash=pv.aggregate_policy_basis
                )
                if pv.aggregate_policy_basis else None
            )
            reconciler_delay = repo.reconciler_delay_seconds(conn)
            # DB probe: a failure is a real anomaly, NOT a fake 0 -> unavailable.
            try:
                stuck = repo.stuck_claim_count(conn)
                probe_ok = True
            except Exception:
                conn.rollback()
                stuck, probe_ok = 0, False
            # Match policy <-> health by the EXACT (platform_key, surface_key)
            # selected surface, never by platform alone. The policy selects one
            # surface per enabled platform; a non-selected surface on the same
            # platform must neither substitute for the selected one (missing) nor
            # drag readiness down.
            enabled_surfaces = _enabled_surface_set(pv)
            surface_policy_valid = _enabled_surface_policy_valid(pv)
            reported_surfaces = {_health_surface_key(h) for h in health}
            missing_health = enabled_surfaces - reported_surfaces
            # Health readiness only considers the SELECTED (platform, surface)
            # pairs. A retired/disabled surface (kimi ships enabled=false) or a
            # non-selected surface reported by AI-1 must NOT peg the product down.
            enabled_health = [h for h in health_items if h.enabled_by_policy]
            # AI-1 overall control-plane readiness (driver/reaper/registry/
            # scheduler). Unwired/exception -> unavailable; substitution pending
            # -> attention. Never lets the product read 'ready' while collection
            # is not actually runnable.
            coll = _collection_readiness(deps)
            try:
                aggregate_runtime = (
                    deps.aggregate_readiness_provider(pv.aggregate_policy_basis)
                    if deps.aggregate_readiness_provider and pv.aggregate_policy_basis
                    else {"status": "unavailable"}
                )
            except Exception:
                aggregate_runtime = {"status": "unavailable"}
            # readiness is NOT 'ready' unless the pipeline is actually running and
            # observable: policy wired + aggregation on + at least one enabled
            # surface + a real aggregate exists + DB probe ok + no unavailable
            # ENABLED surface + collection control-plane runnable. The empty-set
            # clause is FAIL-CLOSED: a policy enumerating zero enabled surfaces
            # cannot be observing anything, and without it the surface-scoped
            # health predicate would evaluate over [] and swallow a full outage.
            unavailable = (
                pv.policy_version is None
                or not pv.ingest_enabled
                or not pv.promotion_enabled
                or not pv.aggregation_enabled
                or not pv.promotion_legal_basis
                or not pv.consent_policy_version
                or not pv.outcome_gold_gate_passed
                or not enabled_surfaces
                or not surface_policy_valid
                or not health_provider_ok
                or not probe_ok
                or aggregate_updated is None
                or coll.status == "unavailable"
                or coll.policy_version != pv.policy_version
                or aggregate_runtime.get("status") != "ready"
                or any(h.runtime_health == "unavailable" for h in enabled_health)
            )
            from services.geo_observation import hmac_buckets
            unavailable = unavailable or not hmac_buckets.is_configured()
            attention = (
                pending > 0 or rejected > 0 or stuck > 0
                or bool(missing_health)
                or coll.status == "attention_required"
                or any(h.runtime_health in ("degraded", "unknown") for h in enabled_health)
                or reconciler_delay > _RECONCILER_DELAY_ATTENTION_SECONDS
            )
            readiness = "unavailable" if unavailable else ("attention_required" if attention else "ready")
            out = dto.AdminOverviewDTO(
                readiness=readiness,
                counts=dto.AdminCountsDTO(
                    pending_review=pending, private_only=counts.get("private_only", 0),
                    rejected=rejected, withdrawn=counts.get("withdrawn", 0),
                    stuck_claims=stuck,
                ),
                reconciler_delay_seconds=reconciler_delay,
                aggregate_updated_at=aggregate_updated,
                policy_version=pv.policy_version,
                platform_health=health_items,
                collection_readiness=coll,
            )
            return out
        finally:
            conn.close()

    @admin.get("/platform-health", response_model=list[dto.AdminPlatformHealthDTO])
    def admin_platform_health(request: Request):
        _require_admin(request)
        pv = _policy_view(deps)
        health, provider_ok = _platform_health_view(deps)
        if not provider_ok:
            raise http_error("OBSERVATION_UNAVAILABLE")
        return _admin_health_items(health, pv)

    @admin.get("/model-shifts", response_model=dto.AdminModelShiftsDTO)
    def admin_model_shifts(request: Request):
        _require_admin(request)
        pv = _policy_view(deps)
        if not pv.aggregate_policy_basis:
            raise http_error("OBSERVATION_UNAVAILABLE")
        _require_aggregate_runtime_ready(deps, pv.aggregate_policy_basis)
        conn = deps.get_conn()
        try:
            _begin_exact_snapshot(conn, pv.aggregate_policy_basis)
            from services.geo_observation_analytics.metrics import MODEL_SHIFT_BPS_THRESHOLD
            rows = repo.model_shift_rows(
                conn,
                threshold_bps=MODEL_SHIFT_BPS_THRESHOLD,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            for scope in {
                (str(row["industry_key"]), str(row["bucket_granularity"]))
                for row in rows
            }:
                _require_live_retention(
                    conn, policy_basis_hash=pv.aggregate_policy_basis,
                    public_only=True, industry_key=scope[0], granularity=scope[1],
                    integrity_checked=True,
                )
            return dto.AdminModelShiftsDTO(items=[dto.AdminModelShiftDTO(
                industry_key=r["industry_key"], platform_key=r["platform_key"],
                model_revision=r["model_revision"], bucket_granularity=r["bucket_granularity"],
                bucket_start=str(r["bucket_start"]),
                model_shift_index_bps=int(r["model_shift_index_bps"]),
                stability_status=r["stability_status"],
            ) for r in rows])
        finally:
            conn.close()

    @admin.get("/aggregate-diff", response_model=dto.AdminAggregateDiffDTO)
    def admin_aggregate_diff(request: Request):
        _require_admin(request)
        # shadow-vs-legacy diff requires the legacy geo_engine_stats mapping,
        # wired at integration; until then this returns an explicit empty note
        # rather than fabricating a comparison.
        return dto.AdminAggregateDiffDTO(
            items=[], note="影子对照需在集成阶段接入现役 geo_engine_stats 口径映射后启用。")

    @admin.get("/content-opportunities", response_model=dto.OpportunitiesDTO)
    def admin_content_opportunities(request: Request, granularity: str = Query("day", pattern="^(day|week|month)$")):
        _require_admin(request)
        pv = _policy_view(deps)
        if not pv.aggregate_policy_basis:
            raise http_error("OBSERVATION_UNAVAILABLE")
        _require_aggregate_runtime_ready(deps, pv.aggregate_policy_basis)
        conn = deps.get_conn()
        try:
            _begin_exact_snapshot(conn, pv.aggregate_policy_basis)
            industries = repo.list_public_industries(
                conn, granularity=granularity,
                policy_basis_hash=pv.aggregate_policy_basis,
            )
            all_items: list[dto.OpportunityItemDTO] = []
            for ind in industries:
                _require_live_retention(
                    conn, policy_basis_hash=pv.aggregate_policy_basis,
                    public_only=True, industry_key=ind, granularity=granularity,
                    integrity_checked=True,
                )
                latest = repo.latest_overall_aggregate(
                    conn, scope_type="public_industry", granularity=granularity, industry_key=ind,
                    policy_basis_hash=pv.aggregate_policy_basis,
                )
                if latest is None:
                    continue
                for o in opp.derive_opportunities(latest, scope_label=f"ind-{ind}"):
                    all_items.append(dto.OpportunityItemDTO(**o.__dict__))
            return dto.OpportunitiesDTO(items=all_items)
        finally:
            conn.close()

    return product, admin


# ===========================================================================
# Module-level helpers used by handlers
# ===========================================================================
@dataclass
class _PolicyView:
    """Normalized view of AI-2's real ``get_policy()`` return shape:
    ``{policy_version:int, policy:{platforms, thresholds, feature_flags},
    effective_flags:{...}, env_overrides:{...}}``. Everything fail-closed."""
    policy_version: Optional[int]
    min_brands: int
    min_source_types: int
    cap_bps: int
    platforms: list
    ingest_enabled: bool
    promotion_enabled: bool
    product_enabled: bool
    aggregation_enabled: bool
    promotion_legal_basis: Optional[str]
    consent_policy_version: Optional[str]
    outcome_gold_gate_passed: bool
    aggregate_policy_basis: Optional[str]
    candidate_policy_basis: Optional[str]


def _policy_view(deps: "ProductApiDeps") -> _PolicyView:
    base = deps.kanon
    unavailable = _PolicyView(
        policy_version=None,
        min_brands=base.min_independent_brand_buckets,
        min_source_types=base.min_source_types,
        cap_bps=C.DEFAULT_MAX_SINGLE_BRAND_SHARE_BPS,
        platforms=[],
        ingest_enabled=False,
        promotion_enabled=False,
        product_enabled=False,
        aggregation_enabled=False,
        promotion_legal_basis=None,
        consent_policy_version=None,
        outcome_gold_gate_passed=False,
        aggregate_policy_basis=None,
        candidate_policy_basis=None,
    )
    try:
        raw = deps.policy_provider() or {}
        if not isinstance(raw, dict):
            return unavailable
        pol = raw.get("policy") or {}
        if not isinstance(pol, dict):
            return unavailable
        flags = raw.get("effective_flags") or pol.get("feature_flags") or {}
        platforms = pol.get("platforms") or []
        if not isinstance(flags, dict) or not isinstance(platforms, list):
            return unavailable
        from services.geo_observation.aggregate_basis import canonical_policy_basis

        candidate_basis = raw.get("candidate_aggregate_policy_basis")
        if not isinstance(candidate_basis, str) or len(candidate_basis) != 64:
            candidate_basis = canonical_policy_basis(pol)

        return _PolicyView(
            policy_version=raw.get("policy_version"),
            min_brands=int(pol.get("public_min_independent_brands", base.min_independent_brand_buckets)),
            min_source_types=int(pol.get("public_min_source_types", base.min_source_types)),
            cap_bps=int(pol.get("max_single_brand_share_bps", C.DEFAULT_MAX_SINGLE_BRAND_SHARE_BPS)),
            platforms=platforms,
            ingest_enabled=bool(flags.get("ingest_enabled", False)),
            promotion_enabled=bool(flags.get("promotion_enabled", False)),
            product_enabled=bool(flags.get("product_enabled", False)),
            aggregation_enabled=bool(flags.get("aggregation_enabled", False)),
            promotion_legal_basis=raw.get("promotion_legal_basis"),
            consent_policy_version=raw.get("consent_policy_version"),
            outcome_gold_gate_passed=bool(raw.get("outcome_gold_gate_passed", False)),
            aggregate_policy_basis=raw.get("aggregate_policy_basis"),
            candidate_policy_basis=candidate_basis,
        )
    except Exception:
        return unavailable


def _require_product_enabled(deps: "ProductApiDeps") -> _PolicyView:
    """Gate every product (private + public) endpoint on the AI-2 product_enabled
    feature flag. Pre-launch (or unwired) => OBSERVATION_UNAVAILABLE, so the
    product surface is never served while the admin switch is off."""
    pv = _policy_view(deps)
    if not pv.product_enabled:
        raise http_error("OBSERVATION_UNAVAILABLE")
    if not pv.aggregation_enabled:
        raise http_error("OBSERVATION_UNAVAILABLE")
    if not pv.ingest_enabled or not pv.promotion_enabled:
        raise http_error("OBSERVATION_UNAVAILABLE")
    if not pv.aggregate_policy_basis:
        raise http_error("OBSERVATION_UNAVAILABLE")
    if pv.candidate_policy_basis != pv.aggregate_policy_basis:
        raise http_error("OBSERVATION_UNAVAILABLE")
    if (
        not pv.promotion_legal_basis
        or not pv.consent_policy_version
        or not pv.outcome_gold_gate_passed
    ):
        raise http_error("OBSERVATION_UNAVAILABLE")
    from services.geo_observation import hmac_buckets

    if not hmac_buckets.is_configured():
        raise http_error("OBSERVATION_UNAVAILABLE")
    # Runtime fence: an env override or stale admin client cannot expose the
    # workbench while its selected collection mode is not genuinely ready.
    collection = _collection_readiness(deps)
    if collection.status != "ready" or collection.policy_version != pv.policy_version:
        raise http_error("OBSERVATION_UNAVAILABLE")
    _require_aggregate_runtime_ready(deps, pv.aggregate_policy_basis)
    return pv


def _require_aggregate_runtime_ready(
    deps: "ProductApiDeps", policy_basis_hash: str
) -> None:
    """Fail closed before aggregate-backed product or governance reads.

    Product repository reads independently re-check the exact bucket revision,
    lineage and live retention predicate on their own connection.  This
    control-plane probe therefore supplies the wider six-cell/SLA gate, while
    the repository predicate closes the request-local readiness/read TOCTOU.
    """
    provider = deps.aggregate_readiness_provider
    if provider is None:
        raise http_error("OBSERVATION_UNAVAILABLE")
    try:
        aggregate_readiness = provider(policy_basis_hash)
    except Exception:
        raise http_error("OBSERVATION_UNAVAILABLE")
    if (
        not isinstance(aggregate_readiness, dict)
        or aggregate_readiness.get("status") != "ready"
    ):
        raise http_error("OBSERVATION_UNAVAILABLE")


def _begin_exact_snapshot(conn, policy_basis_hash: str) -> None:
    """Start one repeatable-read request snapshot and pin manifest integrity."""
    try:
        # Transaction-local: ``set_session`` mutates the pooled physical
        # connection and leaks REPEATABLE READ into unrelated borrowers.
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        problems = repo.published_snapshot_integrity_problems(
            conn, policy_basis_hash=policy_basis_hash,
        )
    except Exception as exc:
        conn.rollback()
        raise http_error("OBSERVATION_UNAVAILABLE") from exc
    if problems:
        conn.rollback()
        raise http_error("OBSERVATION_UNAVAILABLE")


def _require_live_retention(
    conn,
    *,
    policy_basis_hash: str,
    owner_user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    public_only: bool = False,
    industry_key: Optional[str] = None,
    granularity: Optional[str] = None,
    integrity_checked: bool = False,
) -> Optional[dict]:
    """Verify the exact published cell consumed by this request.

    This gate is bounded to one target receipt/bucket.  Normal append lag keeps
    serving the last complete snapshot, while deletion, lineage drift, dirty
    withdrawal revisions, and overdue inputs inside that snapshot fail closed.
    """
    if not integrity_checked:
        _begin_exact_snapshot(conn, policy_basis_hash)
    if granularity is None:
        return None
    scope_type = "public_industry" if public_only else "private_brand"
    try:
        receipt, problems = repo.published_scope_snapshot(
            conn,
            policy_basis_hash=policy_basis_hash,
            scope_type=scope_type,
            granularity=granularity,
            owner_user_id=owner_user_id,
            brand_id=brand_id,
            industry_key=industry_key,
        )
    except Exception as exc:
        conn.rollback()
        raise http_error("OBSERVATION_UNAVAILABLE") from exc
    if problems:
        raise http_error("OBSERVATION_UNAVAILABLE")
    return receipt

def _effective_kanon(deps: "ProductApiDeps", pv: Optional[_PolicyView] = None) -> privacy.KAnonThresholds:
    """Base k-anonymity raised by the REAL nested AI-2 policy, read on EVERY public
    request (raising the prod threshold takes effect immediately; policy may only
    RAISE). Used for cell-gating and per-domain k-anon (differential re-id)."""
    pv = pv or _policy_view(deps)
    base = deps.kanon
    return base.raised_to(privacy.KAnonThresholds(
        min_valid_observations=base.min_valid_observations,
        min_independent_user_buckets=base.min_independent_user_buckets,
        min_independent_brand_buckets=pv.min_brands,
        min_source_types=pv.min_source_types,
    ))


def _rate_cell_kanon(deps: "ProductApiDeps", pv: _PolicyView) -> privacy.KAnonThresholds:
    """k-anonymity for endpoints that SHOW weighted rates (baseline / content
    opportunities). Additionally raises the minimum brand count so the shown cell
    is always per-brand-cap FEASIBLE (>= ceil(10000/cap_bps)); otherwise a
    3-brand cell could show a single brand above the cap."""
    k = _effective_kanon(deps, pv)
    cap_feasible = (10000 + pv.cap_bps - 1) // pv.cap_bps if pv.cap_bps > 0 else k.min_independent_brand_buckets
    return k.raised_to(privacy.KAnonThresholds(
        min_valid_observations=k.min_valid_observations,
        min_independent_user_buckets=k.min_independent_user_buckets,
        min_independent_brand_buckets=cap_feasible,
        min_source_types=k.min_source_types,
    ))


def _enabled_surface_set(pv: _PolicyView) -> set:
    """The EXACT (platform_key, surface_key) pairs the policy selects — one
    selected surface per enabled platform. Readiness matches health against
    these exact pairs, never platform-only (a non-selected surface on the same
    platform is not the selected one)."""
    pairs = set()
    for p in pv.platforms:
        if not isinstance(p, dict) or not p.get("enabled"):
            continue
        platform_key = p.get("platform_key")
        surface_key = p.get("surface_key")
        if (_nonempty_key(platform_key) and _nonempty_key(surface_key)):
            pairs.add((platform_key, surface_key))
    return pairs


def _enabled_surface_policy_valid(pv: _PolicyView) -> bool:
    """An enabled platform must select exactly one concrete surface.

    Missing keys must never match a health row that also omitted its key
    (``(platform, None) == (platform, None)`` was a false-ready path). Duplicate
    enabled entries for one platform are malformed as well: the policy contract
    permits one selected surface per enabled platform.
    """
    enabled = [p for p in pv.platforms if isinstance(p, dict) and p.get("enabled")]
    if not enabled:
        return False
    platform_keys = [p.get("platform_key") for p in enabled]
    return (
        all(_nonempty_key(key) for key in platform_keys)
        and all(_nonempty_key(p.get("surface_key")) for p in enabled)
        and len(set(platform_keys)) == len(platform_keys)
    )


def _nonempty_key(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _health_surface_key(h: dict) -> tuple:
    return (h.get("platform_key") or h.get("platform"), h.get("surface_key"))


def _platform_health_view(deps: "ProductApiDeps") -> tuple[list[dict], bool]:
    """Read AI-1 health without allowing a broken seam to 500 admin APIs."""
    try:
        raw = deps.platform_health_provider() or []
        if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
            return [], False
        return raw, True
    except Exception:
        return [], False


def _admin_health_items(health: list[dict], pv: _PolicyView) -> list[dto.AdminPlatformHealthDTO]:
    """Map AI-1's real read-only health DTO (platform_key + surface_key + status)
    + AI-2 policy selection into the admin display DTO. enabled_by_policy is TRUE
    only when the health row is the policy-SELECTED (platform_key, surface_key)
    pair — a non-selected surface on an enabled platform is enabled_by_policy=False
    (so it never substitutes for, nor drags, the selected surface)."""
    enabled_surfaces = _enabled_surface_set(pv)
    out: list[dto.AdminPlatformHealthDTO] = []
    for h in health:
        pk = h.get("platform_key") or h.get("platform")
        status = h.get("status") or h.get("runtime_health") or "unknown"
        if status not in ("healthy", "degraded", "unavailable", "unknown"):
            status = "unknown"
        out.append(dto.AdminPlatformHealthDTO(
            platform=C.PLATFORM_DISPLAY_NAMES.get(pk, pk or ""),
            enabled_by_policy=_health_surface_key(h) in enabled_surfaces,
            runtime_health=status,
            # admin-only technical detail from AI-1's health DTO
            surface_key=h.get("surface_key"),
            provider_key=h.get("provider_key"),
            model_key=h.get("model_key"),
            model_revision=h.get("model_revision"),
        ))
    return out


def _collection_readiness(deps: "ProductApiDeps") -> dto.CollectionReadinessDTO:
    """Validate the complete integrator contract; partial booleans fail closed."""

    def _unavailable(problem: str) -> dto.CollectionReadinessDTO:
        return dto.CollectionReadinessDTO(
            mode="existing_collectors_reconciled",
            status="unavailable",
            problems=[problem],
            reconciler_last_success_at=None,
            reconciler_backlog=0,
            source_watermarks={
                "paid_diagnosis": None,
                "recurring_monitoring": None,
                "research_round": None,
            },
            duplicate_collection_jobs=[],
            policy_version=None,
        )

    provider = deps.collection_readiness_provider
    if provider is None:
        return _unavailable("collection_readiness_provider 未注入(采集模式 readiness 未接线)")
    try:
        raw = provider() or {}
        if not isinstance(raw, dict):
            return _unavailable(f"collection readiness 形状不可识别(非 dict: {type(raw).__name__})")
        problems_raw = raw.get("problems") or []
        if not isinstance(problems_raw, (list, tuple)):
            problems_raw = [problems_raw]
        problems = [str(p) for p in problems_raw]
        status = {"blocked": "unavailable"}.get(raw.get("status"), raw.get("status"))
        ready = raw.get("ready")
        if ready is not None and not isinstance(ready, bool):
            return _unavailable(
                f"collection readiness ready 字段不是 bool: {type(ready).__name__}"
            )
        if status not in ("ready", "attention_required", "unavailable"):
            return _unavailable(f"collection readiness status 非法: {status!r}")
        if ready is False and status == "ready":
            return _unavailable("collection readiness ready/status 自相矛盾")
        required = {
            "mode", "reconciler_last_success_at", "reconciler_backlog",
            "source_watermarks", "duplicate_collection_jobs", "policy_version",
        }
        missing = sorted(required - set(raw))
        if missing:
            return _unavailable(f"collection readiness 缺字段: {missing}")
        source_watermarks = raw["source_watermarks"]
        duplicate_jobs = raw["duplicate_collection_jobs"]
        if not isinstance(source_watermarks, dict):
            return _unavailable("collection readiness source_watermarks 不是对象")
        if not isinstance(duplicate_jobs, list) or any(
            not isinstance(job_id, str) for job_id in duplicate_jobs
        ):
            return _unavailable("collection readiness duplicate_collection_jobs 不是字符串数组")
        if status == "ready":
            if problems:
                return _unavailable("collection readiness ready 状态仍含 problems")
            if raw["policy_version"] is None or not raw["reconciler_last_success_at"]:
                return _unavailable("collection readiness ready 缺少版本或 reconciler 终态")
            if duplicate_jobs:
                return _unavailable("collection readiness ready 仍含重复采集 job")
            if raw["mode"] == "existing_collectors_reconciled":
                required_sources = {
                    "paid_diagnosis", "recurring_monitoring", "research_round"
                }
                if any(not source_watermarks.get(source) for source in required_sources):
                    return _unavailable("bridge readiness ready 缺少三类 terminal source watermark")
        return dto.CollectionReadinessDTO(
            mode=raw["mode"],
            status=status,
            problems=problems,
            reconciler_last_success_at=raw["reconciler_last_success_at"],
            reconciler_backlog=raw["reconciler_backlog"],
            source_watermarks=source_watermarks,
            duplicate_collection_jobs=duplicate_jobs,
            policy_version=raw["policy_version"],
        )
    except Exception as exc:
        return _unavailable(f"collection readiness 探针异常: {type(exc).__name__}")


def _resolve_question_texts(
    conn, deps: ProductApiDeps, owner: int, brand_id: int, rows: list[dict],
    *, through_watermark, through_promotion_sequence: int,
):
    # look up each observation's source ref, then batch-resolve via evidence source
    refs = []
    for r in rows:
        meta = repo.get_observation_meta(
            conn, owner, brand_id, r["observation_id"],
            through_watermark=through_watermark,
            through_promotion_sequence=through_promotion_sequence,
        )
        if meta is None:
            continue
        refs.append(SourceRef(
            observation_id=meta["observation_id"], source_type=meta["source_type"],
            source_table=meta["source_table"], source_record_id=str(meta["source_record_id"]),
            source_subkey=meta["source_subkey"], platform_key=meta["platform_key"],
            surface_key=meta["surface_key"],
        ))
    return deps.evidence_source.resolve_batch(conn, owner, brand_id, refs)


def _citation_dtos(source_domains) -> list[dto.CitationDTO]:
    out: list[dto.CitationDTO] = []
    if not source_domains:
        return out
    for i, d in enumerate(source_domains, start=1):
        if isinstance(d, dict) and d.get("domain"):
            stype = "citation" if d.get("type") == "citation" or d.get("stype") == "citation" else "source"
            out.append(dto.CitationDTO(domain=d["domain"], source_type=stype, rank=i))
    return out


def _evidence_gap(outcome: str) -> list[str]:
    if outcome == "refused_no_evidence":
        return ["可核验的项目案例", "项目验收数据", "客户授权的公开证明"]
    if outcome == "mentioned_only":
        return ["能形成推荐的成功案例", "第三方可核验证据"]
    return []


def _fact_pack_from_row(conn, owner: int, brand_id: int, row: dict, granularity: str) -> explain.FactPack:
    metrics_bps = {
        "presence": int(row["presence_rate_bps"]),
        "explicit_recommendation": int(row["explicit_recommendation_rate_bps"]),
        "conditional_recommendation": int(row["conditional_recommendation_rate_bps"]),
        "criteria_only": int(row["criteria_only_rate_bps"]),
        "refusal_no_evidence": int(row["refusal_no_evidence_rate_bps"]),
        "refusal_risk": int(row["refusal_risk_rate_bps"]),
        "citation": int(row["citation_rate_bps"]),
        "evidence_coverage": int(row["evidence_coverage_rate_bps"]),
    }
    outcome_counts = {o: int(row[f"{o}_count"]) for o in C.VALID_OUTCOMES if f"{o}_count" in row}
    watermark = row.get("input_watermark")
    refs = (
        repo.recent_observation_refs(
            conn, owner, brand_id, through_watermark=watermark,
            through_promotion_sequence=int(row["promotion_sequence_watermark"]), limit=10
        )
        if watermark is not None else []
    )
    return explain.build_fact_pack(
        industry_key=row["industry_key"],
        window={"start": str(row["bucket_start"]), "end": str(row["bucket_end"]),
                "label": {"day": "当日", "week": "本周", "month": "本月"}.get(granularity, "近期")},
        outcome_counts=outcome_counts, metrics_bps=metrics_bps,
        sample_size=int(row["valid_observations"]), stability_status=row["stability_status"],
        model_shift=(row["stability_status"] == "shifted"
                     or int(row["model_shift_index_bps"]) >= 2000),
        evidence_refs=refs, allowed_action_types=["add_evidence", "keep_observing", "add_content"],
    )


def _job_dto(job: dict) -> dto.InsightJobStatusDTO:
    internal_state = job["state"]
    state = "running" if internal_state == "paid_call_started" else internal_state
    progress = None
    if state == "pending":
        progress = 0
    elif state == "running":
        progress = 55
    elif state == "completed":
        progress = 100
    if state in ("failed", "result_unknown"):
        # surface as a 503 unavailable, deterministic charts remain readable
        raise http_error(
            "SEMANTIC_INSIGHT_UNAVAILABLE",
            retryable=(state == "failed" and job.get("paid_call_started_at") is None),
        )
    return dto.InsightJobStatusDTO(
        job_id=job["job_id"], state=state, request_id=job.get("request_id"),
        progress_percent=progress, summary=job.get("summary"),
        evidence_refs=(job.get("evidence_refs") if state == "completed" else None),
        allowed_actions=(job.get("allowed_actions") if state == "completed" else None),
    )
