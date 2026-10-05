"""GEO 统一观测 · 管理员治理后端(admin-only)。

契约(spec 02 §11):readiness/events/review-queue/review/withdraw/privacy-audit/source-health/policy(GET/PUT)。
- 每端点必 _require_admin(不能只靠 module 前缀映射:/api/admin/geo-observation/* 落 users 模块,
  持 users 权限的非 admin 会过中间件闸 → in-router 才是真闸)。
- 写接口强类型 extra='forbid';policy PUT CAS 409 / env 覆盖 423 / 非 admin 403;审计与状态同事务。
- 不在 API 内重跑付费诊断、不调 LLM(晋升由 cron worker 异步做)。
- 公共/服务商匿名聚合接口由 AI-3 实现,不在此。

集成者在 server.py 注册本 router(见 services/geo_observation/wiring.py)。
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg2.extras import Json

from db.connection import get_db
from services.geo_observation import policy as policy_mod
from services.geo_observation import readiness, repository
from services.geo_observation.audit import write_audit
from schemas.geo_observation_admin import (
    PostGoldEvaluationRequest, PutPolicyRequest, PutPromotionGovernanceRequest,
    ReviewEventRequest, WithdrawEventRequest,
)

router = APIRouter(prefix="/api/admin/geo-observation", tags=["GEO观测治理"])


# ────────────────────────────── admin RBAC + 请求上下文 ──────────────────────────────
def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _actor_id(admin: dict) -> str:
    return str(admin.get("username") or admin.get("user_id") or admin.get("id") or "admin")


# ────────────────────────────── readiness ──────────────────────────────
@router.get("/readiness")
def get_readiness(request: Request):
    _require_admin(request)
    schema_errors: list[str] = []
    try:
        with get_db() as conn:
            readiness.verify_geo_observation_schema(conn.cursor())
    except readiness.ObservationSchemaNotReady as exc:
        schema_errors = list(exc.args[0]) if exc.args else [str(exc)]
    from services.geo_observation.integration import collection_readiness_provider

    collection = collection_readiness_provider()
    errors = schema_errors + list(collection.get("problems") or [])
    return {
        "ready": not schema_errors and collection.get("status") == "ready",
        "errors": errors,
        "collection_readiness": collection,
    }


# ────────────────────────────── events ──────────────────────────────
_EVENT_PUBLIC_COLS = (
    "id, event_uuid, source_type, source_table, source_record_id, source_subkey, industry_key, "
    "platform_key, provider_key, model_key, surface_key, processing_state, rejection_codes, "
    "source_terminal_state, consent_policy_version, promotion_legal_basis, legal_hold, "
    "attempts, observed_at, created_at, updated_at, withdrawn_at"
)


@router.get("/events")
def list_events(
    request: Request,
    processing_state: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    _require_admin(request)
    where, params = [], []
    if processing_state:
        where.append("processing_state = %s")
        params.append(processing_state)
    if source_type:
        where.append("source_type = %s")
        params.append(source_type)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS n FROM public.geo_observation_events {clause}", params)
        total = cur.fetchone()["n"]
        cur.execute(
            f"SELECT {_EVENT_PUBLIC_COLS} FROM public.geo_observation_events {clause} "
            f"ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"total": total, "events": rows}


@router.get("/events/{event_id}")
def get_event(request: Request, event_id: int):
    _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {_EVENT_PUBLIC_COLS} FROM public.geo_observation_events WHERE id=%s", (event_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="event 不存在")
        cur.execute(
            "SELECT id, action, operator_type, operator_id, reason_codes, request_id, created_at "
            "FROM public.geo_observation_audit WHERE event_id=%s ORDER BY created_at DESC LIMIT 100",
            (event_id,),
        )
        audits = [dict(r) for r in cur.fetchall()]
    return {"event": dict(row), "audit": audits}


@router.get("/review-queue")
def review_queue(request: Request, limit: int = Query(50, ge=1, le=200)):
    _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_EVENT_PUBLIC_COLS} FROM public.geo_observation_events "
            f"WHERE processing_state='pending_review' ORDER BY created_at ASC LIMIT %s",
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"events": rows}


@router.post("/events/{event_id}/review")
def review_event(request: Request, event_id: int, body: ReviewEventRequest):
    admin = _require_admin(request)
    operator = _actor_id(admin)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, processing_state FROM public.geo_observation_events WHERE id=%s FOR UPDATE", (event_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="event 不存在")
        if row["processing_state"] == "processing":
            raise HTTPException(status_code=409, detail="event 正被 worker 处理,请稍后再复核")
        before = row["processing_state"]
        # withdrawn 是治理终态(退款/注销/抹除/审计判错)→ 除幂等 withdraw 外不可改状态,绝不复活进晋升管线
        if before == "withdrawn" and body.decision != "withdraw":
            raise HTTPException(status_code=409, detail="event 已撤回(治理终态),不可 requeue/改状态")
        # requeue(重新入队处理)仅允许从可重处理态(pending_review/error),不得从终态复活
        if body.decision == "requeue" and before not in ("pending_review", "error"):
            raise HTTPException(status_code=409, detail=f"requeue 仅允许从 pending_review/error(当前 {before})")

        if body.decision == "withdraw":
            changed = repository.withdraw_event(cur, event_id, reason_codes=[body.reason])
            after = "withdrawn"
        else:
            target = {"private_only": "private_only", "rejected": "rejected", "requeue": "pending"}[body.decision]
            # requeue 重排必须重置 attempts,否则被 poison 熔断器(attempts>上限)立即打回 error,永远救不回
            cur.execute(
                "UPDATE public.geo_observation_events SET processing_state=%s, lease_token=NULL, lease_until=NULL, "
                "attempts=0, updated_at=NOW() WHERE id=%s AND processing_state <> 'processing' RETURNING id",
                (target, event_id),
            )
            changed = cur.fetchone() is not None
            after = target
        if not changed:
            raise HTTPException(status_code=409, detail="event 状态已变化,复核未生效")
        write_audit(cur, f"admin_review:{body.decision}", "admin", event_id=event_id, operator_id=operator,
                    before={"processing_state": before}, after={"processing_state": after},
                    reason_codes=[body.reason], request_id=body.request_id,
                    idempotency_token=f"admin_review:{body.decision}")
    return {"event_id": event_id, "before": before, "after": after}


@router.post("/events/{event_id}/withdraw")
def withdraw_event(request: Request, event_id: int, body: WithdrawEventRequest):
    admin = _require_admin(request)
    operator = _actor_id(admin)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT processing_state FROM public.geo_observation_events WHERE id=%s FOR UPDATE", (event_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="event 不存在")
        changed = repository.withdraw_event(cur, event_id, reason_codes=[body.reason])
        if changed:
            write_audit(cur, "admin_withdraw", "admin", event_id=event_id, operator_id=operator,
                        before={"processing_state": row["processing_state"]}, after={"processing_state": "withdrawn"},
                        reason_codes=[body.reason], request_id=body.request_id, idempotency_token="admin_withdraw")
    return {"event_id": event_id, "withdrawn": True}


# ────────────────────────────── privacy-audit / source-health ──────────────────────────────
@router.get("/privacy-audit")
def privacy_audit(request: Request, action: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=500)):
    _require_admin(request)
    where, params = [], []
    if action:
        where.append("action = %s")
        params.append(action)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, event_id, action, operator_type, operator_id, reason_codes, request_id, created_at "
            f"FROM public.geo_observation_audit {clause} ORDER BY created_at DESC LIMIT %s",
            params + [limit],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"audit": rows}


@router.get("/source-health")
def source_health(request: Request):
    _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT source_type, processing_state, COUNT(*) AS n FROM public.geo_observation_events "
            "GROUP BY source_type, processing_state ORDER BY source_type, processing_state"
        )
        by_state = [dict(r) for r in cur.fetchall()]
    return {"by_source_state": by_state}


# ────────────────────────────── policy (GET / PUT CAS) ──────────────────────────────
@router.get("/policy")
def get_policy(request: Request):
    _require_admin(request)
    snap = policy_mod.get_policy()
    return {
        "policy_version": snap["policy_version"],
        "collection_mode": snap["collection_mode"],
        "policy": snap["policy"],
        "env_overrides": snap["env_overrides"],
        "effective_flags": snap["effective_flags"],
        "promotion_governance": {
            "promotion_legal_basis": snap["promotion_legal_basis"],
            "consent_policy_version": snap["consent_policy_version"],
            "outcome_gold_gate_passed": snap["outcome_gold_gate_passed"],
            "gold_dataset_version": snap["gold_dataset_version"],
            "gold_macro_f1_bps": snap["gold_macro_f1_bps"],
            "gold_sample_count": snap["gold_sample_count"],
            "gold_high_risk_false_reco": snap["gold_high_risk_false_reco"],
            "gold_report_hash": snap["gold_report_hash"],
        },
    }


@router.put("/policy/promotion-governance")
def put_promotion_governance(request: Request, body: PutPromotionGovernanceRequest):
    """法务批准晋升依据 + 同意版本(CAS)。批准后 worker 下一次决策即读新鲜值生效。

    金标准门不在此设置(见 POST /policy/gold-evaluation);extra='forbid' 会 422 拒绝残留 outcome_gold_gate_passed。
    """
    admin = _require_admin(request)
    operator = _actor_id(admin)
    try:
        with get_db() as conn:
            new_version = policy_mod.update_promotion_governance(
                conn.cursor(),
                expected_version=body.expected_policy_version,
                promotion_legal_basis=body.promotion_legal_basis,
                consent_policy_version=body.consent_policy_version,
                reason=body.reason, request_id=body.request_id, operator_id=operator,
            )
        return {"policy_version": new_version}
    except policy_mod.PolicyVersionConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "POLICY_VERSION_CONFLICT", "current_version": exc.current_version}) from exc


@router.post("/policy/gold-evaluation")
def post_gold_evaluation(request: Request, body: PostGoldEvaluationRequest):
    """提交金标准评估证据(P1-1)。服务端按契约 §601 阈值派生 outcome_gold_gate_passed,写不可变评估记录。

    调用方只提供证据(样本数/macro-F1/高风险误推荐/数据集版本/报告哈希),**无法直接置门通过**;
    证据不达阈值 → gate_passed=false(confirmed_mention 仍只入 pending_review)。
    """
    admin = _require_admin(request)
    operator = _actor_id(admin)
    try:
        with get_db() as conn:
            result = policy_mod.record_gold_evaluation(
                conn.cursor(),
                expected_version=body.expected_policy_version,
                dataset_version=body.dataset_version,
                sample_count=body.sample_count,
                macro_f1_bps=body.macro_f1_bps,
                high_risk_false_reco=body.high_risk_false_reco,
                report_hash=body.report_hash,
                reason=body.reason, request_id=body.request_id, operator_id=operator,
            )
        return result
    except policy_mod.GoldEvaluationConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "GOLD_EVALUATION_IDENTITY_CONFLICT",
            "dataset_version": exc.dataset_version, "report_hash": exc.report_hash}) from exc
    except policy_mod.PolicyVersionConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "POLICY_VERSION_CONFLICT", "current_version": exc.current_version}) from exc


@router.put("/policy")
def put_policy(request: Request, body: PutPolicyRequest):
    admin = _require_admin(request)
    operator = _actor_id(admin)
    try:
        collection_readiness = None
        if body.policy.feature_flags.product_enabled:
            from services.geo_observation.integration import collection_readiness_provider

            collection_readiness = collection_readiness_provider()
        with get_db() as conn:
            new_version = policy_mod.update_policy(
                conn.cursor(),
                expected_version=body.expected_policy_version,
                new_policy=body.policy.model_dump(mode="json"),
                reason=body.reason,
                request_id=body.request_id,
                operator_id=operator,
                collection_readiness=collection_readiness,
            )
        return {"policy_version": new_version}
    except policy_mod.PolicyVersionConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "POLICY_VERSION_CONFLICT", "current_version": exc.current_version}) from exc
    except policy_mod.PolicyEnvOverride as exc:
        raise HTTPException(status_code=423, detail={
            "code": "POLICY_ENV_OVERRIDE", "fields": exc.fields}) from exc
    except policy_mod.ProductActivationBlocked as exc:
        raise HTTPException(status_code=409, detail={
            "code": "PRODUCT_ACTIVATION_BLOCKED", "problems": exc.problems}) from exc
