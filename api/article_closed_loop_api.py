"""Tenant-safe APIs for article delivery summaries and admin diagnostics."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from services.article_closed_loop_queries import get_admin_state, get_project_summary, list_agent_tasks
from auth.user_ctx import current_user_id


router = APIRouter(prefix="/api/writing/closed-loop", tags=["GEO文章交付闭环"])


class BlockSlotRequest(BaseModel):
    reason_code: str = Field(..., min_length=1, max_length=80)
    user_message: str = Field(..., min_length=1, max_length=500)
    owner_kind: str = Field(..., min_length=1, max_length=40)
    next_action: str = Field(..., min_length=1, max_length=500)
    target_resolution_at: datetime


class ResolveSlotRequest(BaseModel):
    resolution_evidence: str = Field(..., min_length=1, max_length=1000)


class RequeueOutboxRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


def _user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


@router.get("/projects/{quote_id}/summary")
def project_summary(quote_id: int, request: Request):
    _user(request)
    from auth.brand_access import require_quote_access

    try:
        require_quote_access(request, quote_id, allow_null=False)
    except HTTPException as exc:
        if exc.status_code in {403, 404}:
            raise HTTPException(status_code=404, detail="项目不存在")
        raise
    return get_project_summary(quote_id)


@router.get("/tasks")
def agent_tasks(request: Request, limit: int = Query(100, ge=1, le=500)):
    _user(request)
    from auth.brand_access import get_user_brand_filter

    return list_agent_tasks(get_user_brand_filter(request), limit=limit)


@router.get("/admin/state")
def admin_state(
    request: Request,
    quote_id: int | None = Query(None, ge=1),
    limit: int = Query(100, ge=1, le=500),
):
    user = _user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="资源不存在")
    from services.article_closed_loop_contract import feature_flags

    if not feature_flags()["ARTICLE_PLAN_ADMIN_DIFF_UI_ENABLED"]:
        raise HTTPException(status_code=404, detail="资源不存在")
    return get_admin_state(quote_id=quote_id, limit=limit)


@router.get("/admin/data-health")
def admin_data_health(request: Request, since_days: int = Query(180, ge=1, le=730)):
    user = _user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="资源不存在")
    from services.article_data_health import get_article_data_health

    return get_article_data_health(since_days=since_days)


@router.post("/admin/slots/{delivery_slot_key}/block")
def admin_block_slot(delivery_slot_key: str, body: BlockSlotRequest, request: Request):
    user = _user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="资源不存在")
    actor = user.get("user_id") or current_user_id(user)
    if not actor:
        raise HTTPException(status_code=409, detail="管理员身份不可用")
    from db.diagnosis_db import get_connection
    from services.article_closed_loop_metadata import SlotMetadataUnavailable, set_slot_blocked

    conn = get_connection()
    try:
        cur = conn.cursor()
        result = set_slot_blocked(
            cur,
            delivery_slot_key=delivery_slot_key,
            reason_code=body.reason_code,
            user_message=body.user_message,
            owner_kind=body.owner_kind,
            next_action=body.next_action,
            target_resolution_at=body.target_resolution_at,
            actor_user_id=int(actor),
        )
        conn.commit()
        return result
    except SlotMetadataUnavailable as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc), "message": "无法阻断该交付项"})
    finally:
        conn.close()


@router.post("/admin/slots/{delivery_slot_key}/unblock")
def admin_unblock_slot(delivery_slot_key: str, body: ResolveSlotRequest, request: Request):
    user = _user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="资源不存在")
    actor = user.get("user_id") or current_user_id(user)
    if not actor:
        raise HTTPException(status_code=409, detail="管理员身份不可用")
    from db.diagnosis_db import get_connection
    from services.article_closed_loop_metadata import SlotMetadataUnavailable, unblock_slot

    conn = get_connection()
    try:
        cur = conn.cursor()
        result = unblock_slot(
            cur,
            delivery_slot_key=delivery_slot_key,
            resolution_evidence=body.resolution_evidence,
            actor_user_id=int(actor),
        )
        conn.commit()
        return result
    except SlotMetadataUnavailable as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc), "message": "无法解除阻断"})
    finally:
        conn.close()


@router.post("/admin/outbox/{outbox_id}/requeue")
def admin_requeue_outbox(outbox_id: int, body: RequeueOutboxRequest, request: Request):
    user = _user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="资源不存在")
    actor = user.get("user_id") or current_user_id(user)
    if not actor:
        raise HTTPException(status_code=409, detail="管理员身份不可用")
    from db.diagnosis_db import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_article_plan_outbox
               SET status='pending',attempt_count=0,available_at=NOW(),claim_token=NULL,claimed_at=NULL,
                   last_error=%s,manual_resolution_status=NULL,manual_resolution_reason=%s,
                   manual_resolved_by=%s,manual_resolved_at=NOW(),updated_at=NOW()
             WHERE id=%s AND status='dead_letter' AND manual_resolution_status='required'
            RETURNING quote_id,event_kind
            """,
            (f"manual_requeue:{body.reason}"[:2000], body.reason, int(actor), int(outbox_id)),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise HTTPException(status_code=409, detail="该事件不在可人工重试状态")
        conn.commit()
        return {"requeued": True, "quote_id": int(row["quote_id"]), "event_kind": row["event_kind"]}
    finally:
        conn.close()


@router.post("/admin/canary/quotes/{quote_id}/enroll")
def admin_enroll_canary_quote(quote_id: int, request: Request):
    user = _user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="资源不存在")
    actor = user.get("user_id") or current_user_id(user)
    if not actor:
        raise HTTPException(status_code=409, detail="管理员身份不可用")
    from db.diagnosis_db import get_connection
    from services.article_closed_loop_metadata import SlotMetadataUnavailable, enroll_new_quote_canary

    conn = get_connection()
    try:
        cur = conn.cursor()
        result = enroll_new_quote_canary(cur, quote_id=int(quote_id), actor_user_id=int(actor))
        conn.commit()
        return result
    except SlotMetadataUnavailable as exc:
        conn.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": str(exc), "message": "该报价尚未达到 canary 入组条件"},
        )
    finally:
        conn.close()
