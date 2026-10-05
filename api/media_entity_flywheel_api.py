"""Admin API for GEO placement media entity flywheel shadow layer.

[A1 事件循环阻塞治理 · 2026-08-01]
生产实证(nginx access log 2026-08-02 03:22,log_format `omnirank_timed` 带 rt/urt):
本页 `/admin/geo-placement-flywheel` 一次打开并发 ~20 个 GET,同一批 16 个端点
**四次加载全部在 ±0.06s 内同时完成**(10.45s → 13.73s → 22.83s → 60.0s),
且完全无关的轻端点 `/api/wallet`、`/api/user/notifications` 一并被拖到 rt=60.0 → 504
(19 个 504 全部 urt=60.0 ≈ nginx `proxy_connect_timeout 60s`,即 uvicorn 连 accept 都做不到)。
= 单 worker(WORKERS=1 铁律)事件循环被 async handler 里的裸同步 psycopg2 调用串行占死。

修法(裁定 §A3,只读重端点):`async def` → `def`。FastAPI 对同步 handler 自动走
`run_in_threadpool`,同步 DB 调用离开事件循环,行为/返回值零变化,diff 只有一个 `async` 前缀。
handler 内部真有 await 的一律不动(仍用 `asyncio.to_thread` 包同步段,见
`writing_style_flywheel_api.py:318` [B1-6]/[V7] 样板)。

全站治理清单 + 回归锁:`scripts/scan_async_endpoint_blocking_2026_08_01.py`
(AST 走查,存量基线登记在 `tests/fixtures/async_blocking_baseline_2026_08_01.json`)。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.flywheel_rebuild_lock import run_rebuild_guarded  # [B1-6] 后台化 + 互斥守卫

from db.answer_adoption_metrics_db import (
    init_answer_adoption_metric_tables,
    list_answer_adoption_metric_summary,
)
from db.geo_source_signals_db import (
    init_geo_source_signal_tables,
    list_source_signal_rollup,
)
from db.media_entity_flywheel_db import (
    count_other_approved_by_mapping,
    count_recommended_binding_candidates,
    delete_inventory_mapping,
    disable_media_takeover_policy,
    get_media_binding_candidate,
    get_media_entity_for_binding,
    get_flywheel_data_health,
    get_media_takeover_policy,
    get_media_inventory_item,
    get_media_flywheel_coverage,
    init_media_entity_flywheel_tables,
    insert_media_binding_audit_event,
    insert_media_takeover_audit_event,
    list_approved_media_binding_candidates,
    list_media_binding_candidates,
    list_media_inventory_candidates,
    list_recent_auto_approved_bindings,
    list_shadow_media_entities,
    recommended_binding_candidates_scan,
    save_media_takeover_policy,
    update_media_binding_candidate_review,
    upsert_media_binding_candidate,
    upsert_inventory_mapping,
    upsert_media_entity,
    upsert_score_snapshot,
)
from services.media_binding_candidates import (
    RECOMMENDED_BINDING_MIN_CONFIDENCE,
    build_binding_candidates,
    is_auto_approvable,
    verify_candidate_for_approval,
)
from writing.feature_switches import is_feature_enabled
from services.media_takeover_gate import evaluate_takeover_gate
from services.media_entity_flywheel import (
    normalize_domain,
    build_media_entity_seed,
    compute_media_entity_shadow_score,
    industry_filter_values,
    is_all_industry_scope,
    match_inventory_to_entity,
    normalize_industry_key,
)
from services.flywheel_advisory import (
    ENABLE_ADVISORY_CONFIRMATION,
    ALLOWED_ADVISORY_MODES,
    get_advisory_candidates,
    get_flywheel_observability,
    set_flywheel_advisory_mode,
)
from scripts.rebuild_answer_adoption_metrics_2026_06_17 import rebuild as rebuild_answer_adoption_metrics
from scripts.rebuild_geo_source_signals_shadow import rebuild as rebuild_source_signals
from db.flywheel_bridge_db import (
    get_flywheel_bridge_health,
    list_recent_bridge_runs,
)
from db.research_answer_entity_db import list_entity_mentions
from services.media_effectiveness_board import get_industry_media_effectiveness_board
from writing.flywheel_cache import SCOPE_ENTITY_RANK, SCOPE_HEALTH, get_or_compute, make_key


router = APIRouter(prefix="/api/admin/geo-placement-flywheel", tags=["GEO投放飞轮"])


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


class MediaEntityPreviewRequest(BaseModel):
    entity: dict[str, Any] = Field(..., description="媒体实体种子")
    citation_rollup: dict[str, Any] = Field(default_factory=dict)
    inventory_matches: list[dict[str, Any]] = Field(default_factory=list)
    outcome_rollup: dict[str, Any] = Field(default_factory=dict)


class MediaEntityPersistRequest(MediaEntityPreviewRequest):
    persist_inventory: bool = True


class SourceSignalRebuildRequest(BaseModel):
    industry: str = Field("", max_length=100)
    limit: int = Field(1000, ge=1, le=10000)
    dry_run: bool = True


class MediaEntityRebuildRequest(BaseModel):
    industry: str = Field("", max_length=100)
    limit: int = Field(100, ge=1, le=500)
    dry_run: bool = True


class AnswerAdoptionMetricRebuildRequest(BaseModel):
    industry: str = Field("", max_length=100)
    limit: int = Field(50000, ge=0, le=200000)
    dry_run: bool = True


class MediaBindingCandidateRebuildRequest(BaseModel):
    industry: str = Field("", max_length=100)
    limit: int = Field(80, ge=1, le=500)
    dry_run: bool = True


class MediaBindingCandidateReviewRequest(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    review_note: str = Field(..., min_length=2, max_length=500)


class MediaBindingCandidateBatchReviewRequest(BaseModel):
    candidate_ids: list[int] = Field(..., min_length=1, max_length=200)
    decision: str = Field(..., pattern="^(approve|reject)$")
    # 批量场景允许空备注,后端自动生成"AI 高置信度匹配·批量人工确认"类默认备注
    review_note: str = Field("", max_length=500)


class EngineWeightCandidateGenerateRequest(BaseModel):
    industries: Optional[list[str]] = None  # None = 全部行业


class EngineWeightCandidateReviewRequest(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    review_note: str = Field(..., min_length=2, max_length=500)


class MediaTakeoverGatePreviewRequest(BaseModel):
    industry: str = Field("", max_length=100)
    whitelist: dict[str, Any] = Field(default_factory=dict)


class MediaTakeoverGateSaveRequest(MediaTakeoverGatePreviewRequest):
    review_note: str = Field(..., min_length=2, max_length=500)


class MediaTakeoverGateDisableRequest(BaseModel):
    industry: str = Field("", max_length=100)
    review_note: str = Field(..., min_length=2, max_length=500)


class FlywheelAdvisoryModeRequest(BaseModel):
    mode: str = Field(..., max_length=30)
    confirm: str = Field("", max_length=80)


def _ensure_valid_entity(entity: dict[str, Any]) -> None:
    if not entity.get("entity_key") or not entity.get("canonical_name"):
        raise HTTPException(status_code=400, detail="媒体实体名称或域名不能为空")


def _inventory_id(match: dict[str, Any]) -> int:
    try:
        return int(match.get("inventory_id") or match.get("media_id") or match.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _user_id(user: dict[str, Any]) -> int | None:
    try:
        return int(user.get("id") or user.get("user_id") or 0) or None
    except (TypeError, ValueError):
        return None


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _entity_from_shadow_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_key": row.get("entity_key"),
        "canonical_name": row.get("canonical_name"),
        "domain": row.get("domain") or "",
        "aliases": _json_value(row.get("aliases"), []),
        "industry_key": row.get("industry_key") or "general",
        "confidence": row.get("confidence") or 0.7,
    }


def _industry_context(industry: str | None) -> tuple[str, list[str]]:
    industry_key = normalize_industry_key(industry) if industry else ""
    if is_all_industry_scope(industry or industry_key):
        return "general", []
    industry_values = industry_filter_values(industry or industry_key) if (industry or industry_key) else []
    return industry_key or "general", industry_values


def _query_industry_key(industry: str | None) -> str:
    if not industry or is_all_industry_scope(industry):
        return ""
    industry_key = normalize_industry_key(industry)
    return "" if is_all_industry_scope(industry_key) else industry_key


@router.get("/advisory/observability")
def flywheel_advisory_observability(request: Request):
    """Read flywheel advisory observability for admin review only."""
    _require_admin(request)
    return {
        "status": "success",
        **get_flywheel_observability(),
    }


@router.get("/advisory/candidates")
def flywheel_advisory_candidates(
    request: Request,
    industry: Optional[str] = None,
    media_type: str = "",
    limit: int = 20,
):
    """List admin advisory candidates without affecting live publishing."""
    _require_admin(request)
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit 必须 1-100")
    return get_advisory_candidates(
        industry=_query_industry_key(industry),
        media_type=media_type,
        limit=limit,
    )


@router.put("/advisory-mode")
async def update_flywheel_advisory_mode(req: FlywheelAdvisoryModeRequest, request: Request):
    """Update read-only advisory mode. It never enables production takeover."""
    user = _require_admin(request)
    mode = (req.mode or "").strip()
    if mode not in ALLOWED_ADVISORY_MODES:
        raise HTTPException(status_code=400, detail="无效的飞轮顾问模式")
    if mode == "advisory" and req.confirm != ENABLE_ADVISORY_CONFIRMATION:
        raise HTTPException(status_code=409, detail="启用顾问参考需要输入确认口令")
    try:
        saved = set_flywheel_advisory_mode(mode, operator_id=_user_id(user))
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的飞轮顾问模式")
    return {
        "status": "success",
        "mode": saved,
        "shadow_only": True,
        "production_takeover": False,
    }


def _load_takeover_gate(
    *,
    industry: str | None,
    whitelist: dict[str, Any] | None,
) -> dict[str, Any]:
    industry_key, industry_values = _industry_context(industry)
    health = get_flywheel_data_health(
        industry_key=industry_key,
        industry_values=industry_values,
    )
    adoption_data = list_answer_adoption_metric_summary(
        industry_key=industry_key,
        limit=20,
    )
    approved_bindings = list_approved_media_binding_candidates(
        industry_key=industry_key,
        limit=100,
    )
    policy = get_media_takeover_policy(industry_key)
    policy_whitelist = _json_value(policy.get("whitelist"), {}) if policy else {}
    gate = evaluate_takeover_gate(
        industry_key=industry_key,
        health=health,
        approved_bindings=approved_bindings,
        adoption_summary=adoption_data.get("summary") or {},
        whitelist=whitelist if whitelist is not None else policy_whitelist,
    )
    return {
        "industry_key": industry_key,
        "health": health,
        "adoption_summary": adoption_data.get("summary") or {},
        "approved_bindings": approved_bindings,
        "policy": policy,
        "gate": gate,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.get("/source-signals/rollup")
def source_signal_rollup(request: Request, industry: Optional[str] = None, limit: int = 100):
    """List weighted source signals with fairness-normalized answer credit."""
    _require_admin(request)
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit 必须 1-1000")
    init_geo_source_signal_tables()
    rows = list_source_signal_rollup(
        industry_key=_query_industry_key(industry),
        limit=limit,
    )
    return {"status": "success", "items": rows, "shadow_only": True}


@router.post("/source-signals/rebuild")
async def rebuild_source_signal_shadow(req: SourceSignalRebuildRequest, request: Request):
    """Rebuild source signal shadow rows from research raw data.

    Dry-run is the default so an admin can inspect loaded rows before writing.
    """
    _require_admin(request)
    # [B1-6] 后台线程执行 + 互斥(防事件循环冻结 / 并发重复重建)
    return await run_rebuild_guarded(
        "source_signals_rebuild",
        rebuild_source_signals,
        industry=req.industry, limit=req.limit, dry_run=req.dry_run,
    )


@router.get("/answer-adoption/summary")
def answer_adoption_metric_summary(request: Request, industry: Optional[str] = None, limit: int = 20):
    """Read true answer-adoption metrics derived from classified source signals."""
    _require_admin(request)
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit 必须 1-100")
    init_answer_adoption_metric_tables()
    data = list_answer_adoption_metric_summary(
        industry_key=_query_industry_key(industry),
        limit=limit,
    )
    return {
        "status": "success",
        **data,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.post("/answer-adoption/rebuild")
async def rebuild_answer_adoption_metric_shadow(req: AnswerAdoptionMetricRebuildRequest, request: Request):
    """Rebuild answer-adoption metric snapshots from source signal shadow rows."""
    _require_admin(request)
    # [B1-6] 后台线程执行 + 互斥
    return await run_rebuild_guarded(
        "answer_adoption_rebuild",
        rebuild_answer_adoption_metrics,
        industry=req.industry, limit=req.limit, dry_run=req.dry_run,
    )


@router.get("/media/coverage")
def media_flywheel_coverage(request: Request):
    """Admin-only coverage summary for shadow media entity layer."""
    _require_admin(request)
    init_media_entity_flywheel_tables()
    return {"status": "success", "coverage": get_media_flywheel_coverage()}


@router.get("/health")
def flywheel_data_health(request: Request, industry: Optional[str] = None):
    """Read-only health check for the admin flywheel review console."""
    _require_admin(request)
    industry_key = normalize_industry_key(industry) if industry else ""
    if is_all_industry_scope(industry or industry_key):
        industry_values = []
    else:
        industry_values = industry_filter_values(industry or industry_key) if (industry or industry_key) else []
    # [A1][V7] 接上 `writing.flywheel_cache.SCOPE_HEALTH` —— 这个 scope 早就定义好却一直没接线
    #   (grep 全仓:此前只有 tests 引用)。生产实证:同一分钟内本页被重复加载 4 次,
    #   16 个端点每次全量重算 —— 60s TTL 把「同一份真实聚合」在跨次加载间复用,数值不变。
    #   🔴 只在这个只读 GET 上缓存:`_ensure_strategy_health_allows_activation`(策略启用闸)
    #   同样调 get_flywheel_data_health,那条路径**必须实时**,所以绝不在 db 函数层加缓存。
    #   SCOPE_HEALTH 不属于 CONTROL_DERIVED_SCOPES(既有测试已断言),写动作不需要失效它。
    health = get_or_compute(
        make_key(SCOPE_HEALTH, industry_key, ",".join(industry_values)),
        60.0,
        lambda: get_flywheel_data_health(
            industry_key=industry_key,
            industry_values=industry_values,
        ),
    )
    return {
        "status": "success",
        "health": health,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.get("/media/shadow-recommendations")
def media_shadow_recommendations(
    request: Request,
    industry: Optional[str] = None,
    only_purchasable: bool = False,
    limit: int = 50,
):
    """List shadow-ranked media entities. Does not affect live recommendation."""
    _require_admin(request)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit 必须 1-200")
    init_media_entity_flywheel_tables()
    rows = list_shadow_media_entities(
        industry_key=_query_industry_key(industry),
        only_purchasable=only_purchasable,
        limit=limit,
    )
    return {"status": "success", "items": rows, "shadow_only": True}


@router.get("/media/binding-candidates")
def media_binding_candidates(
    request: Request,
    industry: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
):
    """List admin-reviewed media binding candidates. Shadow only."""
    _require_admin(request)
    if limit < 1 or limit > 300:
        raise HTTPException(status_code=400, detail="limit 必须 1-300")
    init_media_entity_flywheel_tables()
    rows = list_media_binding_candidates(
        industry_key=_query_industry_key(industry),
        status=status or "",
        limit=limit,
    )
    return {
        "status": "success",
        "items": rows,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.post("/media/binding-candidates/rebuild")
async def rebuild_media_binding_candidates(req: MediaBindingCandidateRebuildRequest, request: Request):
    """Preview or write media-box binding candidates to shadow review table."""
    user = _require_admin(request)
    operator_id = _user_id(user)

    # [B1-6] 内联重建循环挪到后台线程 + 互斥(逐实体查库存,量大时会占事件循环)
    def _work() -> dict[str, Any]:
        init_media_entity_flywheel_tables()
        industry_key = _query_industry_key(req.industry)
        entities = list_shadow_media_entities(
            industry_key=industry_key,
            only_purchasable=False,
            limit=req.limit,
        )
        preview: list[dict[str, Any]] = []
        written = 0
        candidate_count = 0
        # [T6b] 自动通过闸门:仅 flag 开 + 真跑时对 domain_exact≥0.95 无风险可采购候选自动 approve。
        auto_approve_enabled = (not req.dry_run) and is_feature_enabled("binding_auto_approve")
        auto_approved = 0
        auto_failed = 0
        for row in entities:
            entity = _entity_from_shadow_row(row)
            names = [entity.get("canonical_name") or "", *(_json_value(entity.get("aliases"), []) or [])]
            inventory_rows = list_media_inventory_candidates(
                names=names,
                domain=normalize_domain(entity.get("domain") or ""),
                limit=40,
            )
            candidates = build_binding_candidates(entity, inventory_rows, limit=8)
            candidate_count += len(candidates)
            preview.append({
                "entity": entity,
                "candidates": candidates,
                "candidate_count": len(candidates),
            })
            if req.dry_run:
                continue
            for candidate in candidates:
                saved = upsert_media_binding_candidate(candidate, operator_id=operator_id)
                if saved.get("_revived_from_deleted"):
                    # [P2 2026-07-03] 重跑匹配命中已删除候选 → 复活为待审核(人工决策痕迹已清空)。
                    insert_media_binding_audit_event(
                        candidate_id=int(saved["id"]),
                        entity_key=candidate.get("entity_key") or "",
                        industry_key=candidate.get("industry_key") or "general",
                        event_type="revived_from_deleted",
                        operator_id=operator_id,
                        note="重跑匹配命中已删除候选，复活为待审核候选，仍需人工审核才能进入投放映射",
                        payload={"candidate_key": candidate.get("candidate_key")},
                    )
                insert_media_binding_audit_event(
                    candidate_id=int(saved["id"]),
                    entity_key=candidate.get("entity_key") or "",
                    industry_key=candidate.get("industry_key") or "general",
                    event_type="候选写入",
                    operator_id=operator_id,
                    note="写入待审核媒体绑定候选，不接管线上投放",
                    payload={"candidate": candidate},
                )
                written += 1
                # [T6b] 自动通过:仅 domain_exact≥0.95 无风险可采购(name_alias 0.86 永不自动)。
                #   走既有 _review_binding_candidate_core(实时库存校验保留,不可采购照样 409 拦);
                #   reviewed_by 列是 BIGINT 存不下字符串 → 用 0 + review_note/审计事件标 system:auto。
                #   只写后台数据,不改线上推荐(blend 已退役 + takeover 关),不越接管闸门。
                #   🔴 守卫:只对当前 DB status 仍为 candidate 的行自动通过 —— 绝不覆盖人工 REJECT(status='rejected'
                #      被 upsert ON CONFLICT 保留),也不重复 approve 已通过行(出口审核 Finding #2)。
                if (
                    auto_approve_enabled
                    and saved.get("status") == "candidate"
                    and is_auto_approvable(candidate)
                ):
                    try:
                        _review_binding_candidate_core(
                            int(saved["id"]), "approve",
                            "system:auto ≥0.95 domain_exact 自动通过(只写后台数据,不改线上推荐)",
                            operator_id=None,
                        )
                        insert_media_binding_audit_event(
                            candidate_id=int(saved["id"]),
                            entity_key=candidate.get("entity_key") or "",
                            industry_key=candidate.get("industry_key") or "general",
                            event_type="自动通过绑定",
                            operator_id=None,
                            note="system:auto 高置信 domain_exact 自动通过,仍不越 media_takeover 授权闸门",
                            payload={"reviewed_by_label": "system:auto", "auto": True,
                                     "candidate_key": candidate.get("candidate_key")},
                        )
                        auto_approved += 1
                    except HTTPException:
                        # 实时库存校验没过(409)/实体缺失等 → 不自动通过,留候选人工。
                        auto_failed += 1
                    except Exception:  # 非 HTTP 异常也不拖垮整批(出口审核 Finding #3),计入 auto_failed
                        auto_failed += 1
        if not req.dry_run:
            _invalidate_panorama_cache()  # [review fix] rebuild 新增候选/T6b 自动通过改变 panorama 待办数
        return {
            "status": "success",
            "dry_run": req.dry_run,
            "loaded": len(entities),
            "sampled": candidate_count,
            "loaded_entities": len(entities),
            "candidate_count": candidate_count,
            "written": written,
            "auto_approved": auto_approved,
            "auto_failed": auto_failed,
            "items": preview[:50],
            "shadow_only": True,
            "production_takeover": False,
        }

    return await run_rebuild_guarded("media_binding_candidates_rebuild", _work)


def _binding_failure_entry(
    candidate_id: int, error: str, row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """[P0-2 2026-08-15] 批量审核失败项的统一形状。

    原来只回 `{candidate_id, error}`,前端拿不到媒体名 —— 一键通过是**跨行业全库选集**,
    前端列表只有 300 条,靠 id 映射不回名字,于是原因只能被丢掉,变成「已通过 0 条,
    4 条未成功」这种说不出所以然的提示。这里把名字随失败一起带出去,让原因能直接显示。
    单条端点(`/review`)本来就把 detail 放在 HTTPException 里由前端 parseError 展示,不受影响。
    """
    if row is not None:
        src: dict[str, Any] = row
    else:
        # fail-soft:名字只是给人看的展示字段。查名字失败绝不能把「部分失败」升级成整批 500,
        # 也不能让批量端点凭空多一个必须可用的依赖。查不到就留空,前端退回显示 id。
        # (get_media_binding_candidate 自开连接,不在调用方事务里,这里 except 不会打废别人的事务。)
        try:
            src = get_media_binding_candidate(candidate_id) or {}
        except Exception:
            src = {}
    return {
        "candidate_id": candidate_id,
        "error": error,
        "media_name": src.get("media_name") or "",
        "entity_key": src.get("entity_key") or "",
        "industry_key": src.get("industry_key") or "",
        "media_source": src.get("media_source") or "",
    }


def _invalidate_panorama_cache() -> None:
    """[review fix] 绑定审批(单条/批量/一键/rebuild 内自动通过)会改变 panorama「人工把关」待办数:
    W7 后端进程缓存若不失效,高级 tab 点完审批后全景在 60s TTL 窗口内仍吐旧数(前端失效被后端旧缓存抵消)。
    fail-soft:缓存层不可用绝不影响审批本身。"""
    try:
        from writing.flywheel_cache import SCOPE_INSIGHT, SCOPE_PANORAMA, invalidate
        invalidate([SCOPE_PANORAMA, SCOPE_INSIGHT])
    except Exception:
        pass


def _review_binding_candidate_core(
    candidate_id: int,
    decision: str,
    note: str,
    operator_id: int | None,
) -> dict[str, Any]:
    """单条候选审核核心逻辑,单条端点与批量端点共用;HTTPException 表示该条失败。"""
    candidate = get_media_binding_candidate(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="候选绑定不存在")
    if decision == "reject":
        updated = update_media_binding_candidate_review(
            candidate_id=candidate_id,
            status="rejected",
            reviewed_by=operator_id or 0,
            review_note=note,
        )
        insert_media_binding_audit_event(
            candidate_id=candidate_id,
            entity_key=candidate.get("entity_key") or "",
            industry_key=candidate.get("industry_key") or "general",
            event_type="驳回绑定",
            operator_id=operator_id,
            note=note,
            payload={"candidate": candidate},
        )
        _invalidate_panorama_cache()  # [review fix] 审批改变 panorama 待办数,后端进程缓存须同步失效
        return {
            "status": "success",
            "candidate": updated,
            "shadow_only": True,
            "production_takeover": False,
        }

    entity = get_media_entity_for_binding(
        candidate.get("entity_key") or "",
        candidate.get("industry_key") or "",
    )
    if not entity:
        raise HTTPException(status_code=404, detail="媒体实体不存在或缺少影子评分")
    inventory = get_media_inventory_item(
        candidate.get("media_source") or "mhz_media",
        int(candidate.get("inventory_id") or 0),
    )
    if not inventory:
        raise HTTPException(status_code=404, detail="库存资源不存在")
    try:
        approved = verify_candidate_for_approval(entity, inventory, {"inventory": candidate})
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    mapping_payload = {
        **(approved.get("inventory") or {}),
        "match_method": approved.get("match_method"),
        "match_confidence": approved.get("match_confidence"),
        "is_purchasable": True,
    }
    mapping = upsert_inventory_mapping(int(entity["id"]), mapping_payload)
    score_evidence = _json_value(entity.get("score_evidence"), {}) or {}
    citation_rollup = score_evidence.get("citation_rollup") or {}
    outcome_rollup = score_evidence.get("outcome_rollup") or {}
    score = compute_media_entity_shadow_score(
        entity=entity,
        citation_rollup=citation_rollup,
        inventory_matches=[mapping_payload],
        outcome_rollup=outcome_rollup,
    )
    score_row = upsert_score_snapshot(int(entity["id"]), score)
    updated = update_media_binding_candidate_review(
        candidate_id=candidate_id,
        status="approved",
        reviewed_by=operator_id or 0,
        review_note=note,
        approved_mapping_id=int(mapping["id"]),
    )
    insert_media_binding_audit_event(
        candidate_id=candidate_id,
        entity_key=candidate.get("entity_key") or "",
        industry_key=candidate.get("industry_key") or "general",
        event_type="通过绑定",
        operator_id=operator_id,
        note=note,
        payload={"candidate": candidate, "mapping": mapping, "score": score_row},
    )
    _invalidate_panorama_cache()  # [review fix] 审批改变 panorama 待办数,后端进程缓存须同步失效
    return {
        "status": "success",
        "candidate": updated,
        "mapping": mapping,
        "score_snapshot": score_row,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.post("/media/binding-candidates/{candidate_id}/review")
async def review_media_binding_candidate(
    candidate_id: int,
    req: MediaBindingCandidateReviewRequest,
    request: Request,
):
    """Approve or reject one media binding candidate after admin review."""
    user = _require_admin(request)
    operator_id = _user_id(user)
    init_media_entity_flywheel_tables()
    note = req.review_note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="请填写审核备注")
    return _review_binding_candidate_core(candidate_id, req.decision, note, operator_id)


@router.post("/media/binding-candidates/batch-review")
async def batch_review_media_binding_candidates(
    req: MediaBindingCandidateBatchReviewRequest,
    request: Request,
):
    """批量审核媒体绑定候选。

    AI 已完成匹配和分组,人工只做整批确认;逐条容错(单条失败不拖垮整批),
    每条各自写审计事件。仍是人工触发的审核动作,不新增任何自动接管路径。
    """
    user = _require_admin(request)
    operator_id = _user_id(user)
    init_media_entity_flywheel_tables()
    ids = list(dict.fromkeys(int(x) for x in req.candidate_ids))
    note = (req.review_note or "").strip() or (
        "AI 高置信度匹配，批量人工确认通过" if req.decision == "approve" else "批量人工驳回"
    )
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for cid in ids:
        try:
            result = _review_binding_candidate_core(cid, req.decision, note, operator_id)
            succeeded.append({
                "candidate_id": cid,
                "status": (result.get("candidate") or {}).get("status"),
            })
        except HTTPException as exc:
            failed.append(_binding_failure_entry(cid, str(exc.detail)))
        except Exception as exc:  # 单条意外错误不拖垮整批,错误类型入审计返回
            failed.append(_binding_failure_entry(cid, type(exc).__name__))
    return {
        "status": "success",
        "decision": req.decision,
        "requested": len(ids),
        "succeeded": succeeded,
        "failed": failed,
        "shadow_only": True,
        "production_takeover": False,
    }


# ==================== [T6/T6b] 全行业一键通过 / 撤销绑定 ====================

class ApproveAllRecommendedRequest(BaseModel):
    note: str = Field("", max_length=500)
    limit: int = Field(5000, ge=1, le=20000)


class MediaBindingRevokeRequest(BaseModel):
    review_note: str = Field("", max_length=500)


@router.get("/media/binding-candidates/recommended-count")
def recommended_binding_candidates_count(request: Request):
    """[T6] 全库跨行业「建议通过」条数(前端一键按钮 N 来源)。admin only。"""
    _require_admin(request)
    init_media_entity_flywheel_tables()
    # [P0-1] 与一键通过的选集共用 recommended_binding_candidates_scan,判据同源;
    #   scan_truncated 透传出去,超集触顶时不假装计数是全量。
    scan = recommended_binding_candidates_scan(RECOMMENDED_BINDING_MIN_CONFIDENCE)
    count = len(scan["items"])
    return {
        "status": "success",
        "recommended_count": count,
        "scanned": scan["scanned"],
        "scan_truncated": scan["scan_truncated"],
        "min_confidence": RECOMMENDED_BINDING_MIN_CONFIDENCE,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.post("/media/binding-candidates/approve-all-recommended")
async def approve_all_recommended_binding_candidates(req: ApproveAllRecommendedRequest, request: Request):
    """[T6] 一键通过全部行业的「建议通过」候选。服务端选集(不接受前端传 id,避开翻页/上限漏项),
    与 UI「建议通过」同一标准(共享常量 RECOMMENDED_BINDING_MIN_CONFIDENCE);逐条走既有审核核心,
    单条 409/失败记 failed 不拖垮整批;后台化 + 互斥(run_rebuild_guarded)。只写后台数据,不改线上推荐/不扣费/不发布。"""
    user = _require_admin(request)
    operator_id = _user_id(user)

    def _work() -> dict[str, Any]:
        init_media_entity_flywheel_tables()
        scan = recommended_binding_candidates_scan(
            RECOMMENDED_BINDING_MIN_CONFIDENCE, limit=req.limit,
        )
        candidates = scan["items"]
        note = (req.note or "").strip() or (
            "一键通过全部行业 AI 高置信「建议通过」候选(只写后台数据,不改线上推荐/不扣费/不发布)"
        )
        approved: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for cand in candidates:
            cid = int(cand["id"])
            try:
                result = _review_binding_candidate_core(cid, "approve", note, operator_id)
                approved.append({
                    "candidate_id": cid,
                    "industry_key": cand.get("industry_key"),
                    "status": (result.get("candidate") or {}).get("status"),
                })
            except HTTPException as exc:
                failed.append(_binding_failure_entry(cid, str(exc.detail), cand))
            except Exception as exc:  # 单条意外错误不拖垮整批
                failed.append(_binding_failure_entry(cid, type(exc).__name__, cand))
        # [出口审核 Finding #1] 选集受 limit 上限,可能未取全;通过后重算剩余数,诚实暴露漏项,
        #   前端可提示「还剩 N,再点一次继续」,不做假承诺「已全部通过」。
        remaining = count_recommended_binding_candidates(RECOMMENDED_BINDING_MIN_CONFIDENCE)
        return {
            "status": "success",
            "selected": len(candidates),
            "approved": approved[:200],
            "failed": failed[:200],
            "approved_count": len(approved),
            "failed_count": len(failed),
            "truncated": len(candidates) >= req.limit,
            "scanned": scan["scanned"],
            "scan_truncated": scan["scan_truncated"],
            "remaining": remaining,
            "min_confidence": RECOMMENDED_BINDING_MIN_CONFIDENCE,
            "shadow_only": True,
            "production_takeover": False,
        }

    return await run_rebuild_guarded("binding_approve_all_recommended", _work)


@router.post("/media/binding-candidates/{candidate_id}/revoke")
async def revoke_media_binding(candidate_id: int, req: MediaBindingRevokeRequest, request: Request):
    """[T6b 撤销] 撤销一条已通过绑定(含自动通过):删除对应 inventory_mapping 行,候选置回 candidate,写审计。
    可回滚自动通过;不影响 media_takeover 授权状态。admin only。"""
    user = _require_admin(request)
    operator_id = _user_id(user)
    init_media_entity_flywheel_tables()
    candidate = get_media_binding_candidate(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="候选绑定不存在")
    if candidate.get("status") != "approved":
        raise HTTPException(status_code=409, detail="仅可撤销已通过(approved)的绑定")
    note = (req.review_note or "").strip() or "撤销绑定,候选置回待审核(不影响接管授权)"
    mapping_id = candidate.get("approved_mapping_id")
    # [Finding #4] 仅当无其它 approved 候选共享同一 mapping 时才删该行,否则只解绑本候选(防悬挂)。
    other_refs = count_other_approved_by_mapping(int(mapping_id), candidate_id) if mapping_id else 0
    deleted_mapping = bool(mapping_id) and other_refs == 0 and delete_inventory_mapping(int(mapping_id))
    updated = update_media_binding_candidate_review(
        candidate_id=candidate_id,
        status="candidate",
        reviewed_by=operator_id or 0,
        review_note=note,
        approved_mapping_id=None,
    )
    insert_media_binding_audit_event(
        candidate_id=candidate_id,
        entity_key=candidate.get("entity_key") or "",
        industry_key=candidate.get("industry_key") or "general",
        event_type="撤销绑定",
        operator_id=operator_id,
        note=note,
        payload={"revoked_mapping_id": mapping_id, "deleted_mapping": deleted_mapping},
    )
    return {
        "status": "success",
        "candidate": updated,
        "deleted_mapping": deleted_mapping,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.get("/media/binding-candidates/auto-approved-recent")
def auto_approved_bindings_recent(request: Request, days: int = 7):
    """[T6b] 近 N 天(默认 7 天)自动通过绑定条数 + 明细,供 UI「本周自动通过 N(查看)」。admin only。"""
    _require_admin(request)
    init_media_entity_flywheel_tables()
    safe_days = max(1, min(int(days or 7), 90))
    result = list_recent_auto_approved_bindings(since_days=safe_days, limit=100)
    return {"status": "success", "days": safe_days, **result, "shadow_only": True}


@router.get("/media/takeover-gate")
def media_takeover_gate(request: Request, industry: Optional[str] = None):
    """Read the shadow takeover preparation gate for one industry."""
    _require_admin(request)
    init_media_entity_flywheel_tables()
    return {
        "status": "success",
        **_load_takeover_gate(industry=industry, whitelist=None),
    }


@router.post("/media/takeover-gate/preview")
async def preview_media_takeover_gate(req: MediaTakeoverGatePreviewRequest, request: Request):
    """Preview takeover readiness. This never writes and never affects live ranking."""
    _require_admin(request)
    init_media_entity_flywheel_tables()
    return {
        "status": "success",
        "dry_run": True,
        **_load_takeover_gate(industry=req.industry, whitelist=req.whitelist),
    }


@router.post("/media/takeover-gate/save")
async def save_media_takeover_gate(req: MediaTakeoverGateSaveRequest, request: Request):
    """Save an auditable shadow takeover preparation policy after all gates pass."""
    user = _require_admin(request)
    operator_id = _user_id(user)
    init_media_entity_flywheel_tables()
    context = _load_takeover_gate(industry=req.industry, whitelist=req.whitelist)
    gate = context["gate"]
    if not gate.get("ready"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "takeover_gate_blocked",
                "message": "接管准备条件未通过",
                "blockers": gate.get("blockers") or [],
                "production_takeover": False,
            },
        )
    note = req.review_note.strip()
    policy = save_media_takeover_policy(
        industry_key=context["industry_key"],
        whitelist=req.whitelist,
        gate_summary=gate,
        review_note=note,
        operator_id=operator_id,
    )
    audit = insert_media_takeover_audit_event(
        policy_id=int(policy["id"]),
        industry_key=context["industry_key"],
        event_type="保存接管准备",
        operator_id=operator_id,
        note=note,
        payload={"gate": gate, "policy": policy},
    )
    return {
        "status": "success",
        "policy": policy,
        "audit_event": audit,
        "gate": gate,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.post("/media/takeover-gate/disable")
async def disable_media_takeover_gate(req: MediaTakeoverGateDisableRequest, request: Request):
    """Disable the shadow takeover preparation state for one industry."""
    user = _require_admin(request)
    operator_id = _user_id(user)
    init_media_entity_flywheel_tables()
    industry_key, _industry_values = _industry_context(req.industry)
    note = req.review_note.strip()
    policy = disable_media_takeover_policy(
        industry_key=industry_key,
        review_note=note,
        operator_id=operator_id,
    )
    audit = insert_media_takeover_audit_event(
        policy_id=int(policy["id"]) if policy else None,
        industry_key=industry_key,
        event_type="停用接管准备",
        operator_id=operator_id,
        note=note,
        payload={"policy": policy or {}},
    )
    return {
        "status": "success",
        "policy": policy,
        "audit_event": audit,
        "shadow_only": True,
        "production_takeover": False,
    }


@router.post("/media/rebuild-from-source-signals")
async def rebuild_media_entities_from_source_signals(req: MediaEntityRebuildRequest, request: Request):
    """Build reference media entities from trusted source signals.

    This creates/updates only flywheel shadow tables.  It never mutates media
    inventory or live placement recommendation tables.
    """
    _require_admin(request)

    # [B1-6] 内联重建循环挪到后台线程 + 互斥(最高 20 万行,原直接在 async 里跑会冻结事件循环)
    def _work() -> dict[str, Any]:
        init_geo_source_signal_tables()
        init_media_entity_flywheel_tables()
        rollups = list_source_signal_rollup(
            industry_key=normalize_industry_key(req.industry) if req.industry else "",
            limit=req.limit,
        )
        preview: list[dict[str, Any]] = []
        written = 0
        for row in rollups:
            domain = row.get("domain") or ""
            if not domain:
                continue
            entity = build_media_entity_seed({
                "name": domain,
                "domain": domain,
                "industry_key": row.get("industry_key") or req.industry or "general",
                "confidence": 0.72,
                "tags": {"source": "geo_research_source_signals"},
            })
            score = compute_media_entity_shadow_score(
                entity=entity,
                citation_rollup=row,
                inventory_matches=[],
                outcome_rollup={},
            )
            item = {"entity": entity, "score": score, "source_rollup": row}
            if req.dry_run:
                preview.append(item)
                continue
            entity_row = upsert_media_entity(entity)
            score_row = upsert_score_snapshot(int(entity_row["id"]), score)
            preview.append({"entity": entity_row, "score": score_row, "source_rollup": row})
            written += 1
        return {
            "status": "success",
            "dry_run": req.dry_run,
            "loaded": len(rollups),
            "written": written,
            "items": preview[:50],
            "shadow_only": True,
        }

    return await run_rebuild_guarded("media_entities_rebuild_from_signals", _work)


@router.post("/media/score-preview")
async def preview_media_entity_score(req: MediaEntityPreviewRequest, request: Request):
    """Preview entity normalization and shadow score without writing DB."""
    _require_admin(request)
    entity = build_media_entity_seed(req.entity)
    _ensure_valid_entity(entity)
    matched_inventory = match_inventory_to_entity(entity, req.inventory_matches)
    score = compute_media_entity_shadow_score(
        entity=entity,
        citation_rollup=req.citation_rollup,
        inventory_matches=matched_inventory,
        outcome_rollup=req.outcome_rollup,
    )
    return {
        "status": "success",
        "entity": entity,
        "matched_inventory": matched_inventory,
        "score": score,
        "shadow_only": True,
    }


@router.post("/media/score-snapshot")
async def create_media_entity_score_snapshot(req: MediaEntityPersistRequest, request: Request):
    """Persist an admin-reviewed shadow score snapshot.

    This writes only shadow tables; it does not alter mhz_media, media_effective_pool,
    or any live placement recommendation.
    """
    _require_admin(request)
    init_media_entity_flywheel_tables()
    entity = build_media_entity_seed(req.entity)
    _ensure_valid_entity(entity)
    entity_row = upsert_media_entity(entity)
    matched_inventory = match_inventory_to_entity(entity, req.inventory_matches)
    matches_to_store = matched_inventory
    if req.persist_inventory:
        for match in matches_to_store:
            if _inventory_id(match) <= 0:
                continue
            upsert_inventory_mapping(int(entity_row["id"]), match)
    score = compute_media_entity_shadow_score(
        entity=entity,
        citation_rollup=req.citation_rollup,
        inventory_matches=matches_to_store,
        outcome_rollup=req.outcome_rollup,
    )
    score_row = upsert_score_snapshot(int(entity_row["id"]), score)
    return {
        "status": "success",
        "entity": entity_row,
        "score_snapshot": score_row,
        "shadow_only": True,
    }


# ============================================================
# [B3-1] 引擎权重候选管道(shadow → 人工审核 → UPSERT geo_engine_weights)
# ============================================================
@router.get("/engine-weight-candidates")
def list_engine_weight_candidates_endpoint(request: Request, status: str = "candidate", limit: int = 100):
    """列出引擎权重候选(默认待审核)。admin only。"""
    _require_admin(request)
    from db.engine_weight_candidates_db import list_engine_weight_candidates
    items = list_engine_weight_candidates(status=status, limit=limit)
    return {"status": "success", "items": items, "shadow_only": True, "production_takeover": False}


@router.post("/engine-weight-candidates/generate")
async def generate_engine_weight_candidates_endpoint(req: EngineWeightCandidateGenerateRequest, request: Request):
    """手动触发候选生成(常规由 Stage 7 聚合后自动生成)。admin only · 后台线程 + 互斥。"""
    _require_admin(request)
    from db.engine_weight_candidates_db import generate_engine_weight_candidates
    created = await run_rebuild_guarded(
        "engine_weight_candidates_generate",
        generate_engine_weight_candidates,
        industries=req.industries,
    )
    if isinstance(created, dict):  # in_progress 守卫返回
        return created
    return {"status": "success", "created": created, "shadow_only": True, "production_takeover": False}


@router.post("/engine-weight-candidates/{candidate_id}/review")
async def review_engine_weight_candidate_endpoint(
    candidate_id: int, req: EngineWeightCandidateReviewRequest, request: Request
):
    """审核一条引擎权重候选。approve → UPSERT geo_engine_weights(生效)。admin only · 留审计。"""
    user = _require_admin(request)
    reviewer_id = _user_id(user) or 0
    from db.engine_weight_candidates_db import review_engine_weight_candidate
    result = review_engine_weight_candidate(
        candidate_id=candidate_id,
        reviewer_id=reviewer_id,
        decision=req.decision,
        note=req.review_note,
    )
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="候选不存在")
    if result.get("status") == "already_reviewed":
        raise HTTPException(status_code=409, detail=f"候选已处理: {result.get('candidate_status')}")
    # [P2-5 明示] approve 直接 UPSERT geo_engine_weights → 生产引擎权重即时生效(非纯 shadow),留痕给前端/审计。
    if (req.decision or "").lower() in ("approve", "approved"):
        result["production_effect"] = "geo_engine_weights_updated"
        result["note"] = "审核通过:已直接更新生产引擎权重 geo_engine_weights,即时生效"
    return result


# ============================================================
# [答案实体] 真实推荐图谱 shadow 层(admin-only · 结果侧品牌实体 · 读侧不返完整 answer_text)
# ============================================================
class AnswerEntityRebuildRequest(BaseModel):
    industry: str = Field("", max_length=100)
    limit: int = Field(200, ge=1, le=2000)
    dry_run: bool = True
    only_pending: bool = True


@router.get("/answer-entities/summary")
def answer_entities_summary_endpoint(request: Request, industry: str = "", engine: str = "",
                                           limit: int = 20, since_days: int = 0):
    """行业 Top 推荐实体 + 各引擎覆盖 + 平均推荐排名 + 推荐理由 Top 词。admin only · shadow。"""
    _require_admin(request)
    from collections import Counter
    from db.research_answer_entity_db import (
        init_research_answer_entity_tables, list_answer_entity_summary, list_answer_entity_examples,
    )
    from services.media_entity_flywheel import normalize_industry_key
    from services.research_monitor.answer_entity_extractor import _normalize_engine
    init_research_answer_entity_tables()
    ikey = normalize_industry_key(industry) if industry else ""
    ekey = _normalize_engine(engine) if engine else ""  # [review fix] 读侧引擎归一,与写侧口径一致(qwen→千问)
    sd = since_days if since_days > 0 else None
    entities = list_answer_entity_summary(industry_key=ikey, engine=ekey, limit=limit, since_days=sd)
    # 推荐理由 Top 词(整短语频次 · 中文不分词,取整理由串)
    sample = list_answer_entity_examples(industry_key=ikey, engine=ekey, limit=200, since_days=sd)
    reason_counter: Counter = Counter()
    for e in sample:
        for r in (e.get("recommendation_reasons") or []):
            r = str(r).strip()
            if r:
                reason_counter[r] += 1
    top_reasons = [{"reason": k, "count": v} for k, v in reason_counter.most_common(15)]
    return {
        "status": "success", "shadow_only": True, "production_takeover": False,
        "industry_key": ikey or "(all)", "engine": ekey or "(all)",
        "entities": entities, "top_recommendation_reasons": top_reasons,
    }


@router.get("/answer-entities/examples")
def answer_entities_examples_endpoint(request: Request, industry: str = "", engine: str = "",
                                            limit: int = 20, since_days: int = 0):
    """实体样例(短 evidence phrase + 聚合字段)· 🔴 绝不返完整 answer_text。admin only。"""
    _require_admin(request)
    from db.research_answer_entity_db import init_research_answer_entity_tables, list_answer_entity_examples
    from services.media_entity_flywheel import normalize_industry_key
    from services.research_monitor.answer_entity_extractor import _normalize_engine
    init_research_answer_entity_tables()
    ikey = normalize_industry_key(industry) if industry else ""
    ekey = _normalize_engine(engine) if engine else ""  # [review fix] 读侧引擎归一
    sd = since_days if since_days > 0 else None
    rows = list_answer_entity_examples(industry_key=ikey, engine=ekey, limit=limit, since_days=sd)
    return {"status": "success", "shadow_only": True, "examples": rows}


@router.get("/answer-entities/health")
def answer_entities_health_endpoint(request: Request):
    """答案实体只读健康度(覆盖率/低置信/引擎样本/新鲜度)· 不接管生产推荐。admin only。"""
    _require_admin(request)
    from db.research_answer_entity_db import init_research_answer_entity_tables, get_answer_entity_health
    init_research_answer_entity_tables()
    return {"status": "success", "shadow_only": True, "health": get_answer_entity_health()}


def _answer_entity_cost_estimate(pending: int) -> dict[str, Any]:
    """[T5] 答案实体抽取预计成本(pending 组 × 单组 qwen3.7-max 估价),人话给运营/老板看。"""
    from tools.llm_call_tracker import PRICING_TABLE
    pricing = PRICING_TABLE.get(("dashscope", "qwen3.7-max"), {"input": 0.006, "output": 0.018})
    # 单组:输入 ~3000 token(答案截 8000 字 + prompt),输出 ~800 token(max_tokens=4000 封顶)。
    per_call_cny = round((3000 / 1000) * pricing["input"] + (800 / 1000) * pricing["output"], 6)
    pending = max(0, int(pending or 0))
    total = round(per_call_cny * pending, 2)
    return {
        "pending_groups": pending,
        "estimated_llm_calls": pending,
        "cost_per_call_cny": per_call_cny,
        "estimated_cost_cny": total,
        "human_note": f"约 ¥{total},平台承担(实际按答案长度浮动;单组一次 LLM 抽取,失败重试最多 ×3)。",
        "model": "qwen3.7-max (dashscope)",
    }


@router.post("/answer-entities/rebuild")
async def answer_entities_rebuild_endpoint(req: AnswerEntityRebuildRequest, request: Request):
    """预览/抽取答案实体。dry_run(默认)=只计数不调 LLM;dry_run=false=后台 LLM 抽取。admin only。"""
    _require_admin(request)
    from db.research_answer_entity_db import init_research_answer_entity_tables
    from services.research_monitor.answer_entity_extractor import rebuild_answer_entities
    init_research_answer_entity_tables()
    if req.dry_run:
        # 只计数/预览,不调 LLM(零成本 · 安全 inline)
        result = await rebuild_answer_entities(
            industry=req.industry, limit=req.limit, dry_run=True, only_pending=req.only_pending,
        )
        # [T5] 附预计成本,老板据此授权首跑。
        result["cost_estimate"] = _answer_entity_cost_estimate(result.get("pending_extract") or 0)
        return {"status": "success", "shadow_only": True, **result}
    # 真跑 LLM:🔴 禁止 inline 全库(run_rebuild_guarded sync-only 包不住 async LLM)→ 后台任务 + extractor 内 async 互斥。
    import asyncio
    asyncio.create_task(rebuild_answer_entities(
        industry=req.industry, limit=req.limit, dry_run=False, only_pending=req.only_pending,
    ))
    return {
        "status": "accepted", "shadow_only": True, "mode": "background",
        "message": "已受理后台抽取(async 互斥防并发双成本);完成后到 summary/health 查看结果。",
        "industry": req.industry or "all", "batch_limit": req.limit,
    }


# ============================================================
# [E1] 行业媒体有效性榜(L3 答案实体主榜 + shadow/绑定/L2 附加证据 · admin-only · 纯只读零 LLM)
# ============================================================
@router.get("/media/effectiveness-board")
async def media_effectiveness_board_endpoint(request: Request, industry: str = "",
                                             weeks: int = 4, limit: int = 30):
    """E1 行业媒体有效性榜:L3 答案实体(AI 点名推荐)主榜 + 媒体有效分/可投放/L2 附加证据。

    综合有效分口径 = 点名次数/排名靠前/引擎覆盖/置信度(services.media_effectiveness_board SSOT)。
    数字全真实、零 LLM、无数据不显示、fail-soft。admin only · shadow。
    """
    _require_admin(request)
    ik = _query_industry_key(industry)
    result = await asyncio.to_thread(
        get_or_compute,
        make_key(SCOPE_ENTITY_RANK, ik, weeks, limit),
        60.0,
        lambda: get_industry_media_effectiveness_board(industry, weeks, limit),
    )
    return {"status": "success", "shadow_only": True, **result}


@router.get("/media/effectiveness-board/trace")
async def media_effectiveness_board_trace_endpoint(request: Request, entity_key: str = "",
                                                   industry: str = "", limit: int = 20, weeks: int = 4):
    """E1 溯源:某实体的原始 mention(哪引擎/哪 query/哪轮 batch/推荐理由/证据短语/来源URL)。

    🔴 只返 answer_excerpt 短摘要,绝不返完整 answer_text。admin only · shadow。
    [review fix] weeks 与榜面同窗(since_days=weeks*7),使溯源看到的证据条数与榜面 mention_count 可对账。
    """
    _require_admin(request)
    weeks = max(1, min(52, int(weeks or 4)))
    # [review fix] limit 同样钳制:负数会让 PostgreSQL LIMIT 报错 500,超大值返 MB 级 payload
    limit = max(1, min(200, int(limit or 20)))
    mentions = await asyncio.to_thread(
        list_entity_mentions, entity_key, _query_industry_key(industry), limit, weeks * 7,
    )
    return {"status": "success", "shadow_only": True, "entity_key": entity_key, "mentions": mentions}


# ==================== [P0-1] 飞轮桥接:健康度 + 手动触发 ====================

class FlywheelBridgeRunRequest(BaseModel):
    round_id: str = Field(..., min_length=1, max_length=120)
    dry_run: bool = True
    page_size: int = Field(1000, ge=50, le=5000)


@router.get("/bridge/health")
def flywheel_bridge_health_endpoint(request: Request):
    """飞轮桥接防再断健康度:各层最后更新时间 + 影子层滞后 raw 小时数 + 最近桥接状态。admin only。"""
    _require_admin(request)
    return {
        "status": "success",
        "shadow_only": True,
        "production_takeover": False,
        "health": get_flywheel_bridge_health(),
        "recent_runs": list_recent_bridge_runs(limit=20),
    }


@router.post("/bridge/run")
async def flywheel_bridge_run_endpoint(req: FlywheelBridgeRunRequest, request: Request):
    """手动触发某一 round 的下游桥接。dry_run(默认)=只预估(含答案实体 LLM 成本),不写库;
    dry_run=false=后台跑,只写 shadow/candidate 层(人工审核门保留)。admin only。"""
    _require_admin(request)
    from services.research_monitor.flywheel_bridge import run_round_bridge
    if req.dry_run:
        result = await run_round_bridge(
            req.round_id, trigger_source="manual", dry_run=True, page_size=req.page_size,
        )
        return {"status": "success", "mode": "dry_run", "shadow_only": True, "result": result}
    # 真跑:可能分页 >10000 行 → 后台任务(worker 内含并发锁 + 分页 + 每页 fail-soft)。
    import asyncio
    asyncio.create_task(run_round_bridge(
        req.round_id, trigger_source="manual", dry_run=False, page_size=req.page_size,
    ))
    return {
        "status": "accepted", "mode": "background", "shadow_only": True,
        "round_id": req.round_id,
        "message": "已受理后台桥接(仅写 shadow/candidate 层,不接管客户可见输出);到 /bridge/health 查看进度。",
    }


class SourceSignalsBackfillRequest(BaseModel):
    since_days: int = Field(0, ge=0, le=365)  # 0 = 所有已完成 round
    dry_run: bool = True
    max_rounds: int = Field(500, ge=1, le=5000)
    page_size: int = Field(1000, ge=50, le=5000)


@router.post("/source-signals/backfill")
async def source_signals_backfill_endpoint(req: SourceSignalsBackfillRequest, request: Request):
    """[T1] 全局 catch-up backfill:把已完成 round(6-17 之后断档)的 raw 增量桥进 source_signals + 刷新 adoption_metrics。
    复用飞轮桥接的 source_signals/answer_adoption stage(分页 + 幂等 upsert);互斥,单条 round 失败不拖垮整批。
    dry_run(默认)只计数不写;dry_run=false 后台跑。admin only。与常态化 round hook(flywheel_round_bridge)互补:
    catch-up 补 hook 启用前已完成的历史轮。"""
    _require_admin(request)
    from services.research_monitor.flywheel_bridge import run_source_signals_backfill
    if req.dry_run:
        result = await run_source_signals_backfill(
            since_days=req.since_days, dry_run=True, page_size=req.page_size, max_rounds=req.max_rounds,
        )
        return {"status": "success", "mode": "dry_run", "shadow_only": True, "result": result}
    import asyncio
    asyncio.create_task(run_source_signals_backfill(
        since_days=req.since_days, dry_run=False, page_size=req.page_size, max_rounds=req.max_rounds,
    ))
    return {
        "status": "accepted", "mode": "background", "shadow_only": True,
        "message": "已受理后台 backfill(逐 round 桥接 raw→source_signals→adoption,幂等,只写 shadow 层);到 /bridge/health 看新鲜度。",
    }
