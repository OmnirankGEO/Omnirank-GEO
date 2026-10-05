"""
intake_api — 客户资料补全邀请 API (CTO-E 2026-04-26)

代理端 (auth required, RBAC by brand):
  POST   /api/m3/intake-tokens
  GET    /api/m3/intake-tokens?brand_id=X
  POST   /api/m3/intake-tokens/{id}/revoke
  GET    /api/m3/profile-submissions?brand_id=X
  POST   /api/m3/profile-submissions/{id}/approve
  POST   /api/m3/profile-submissions/{id}/reject

公开端 (no auth, token-validated):
  GET    /api/public/intake/{token}
  POST   /api/public/intake/{token}/ai-suggest
  POST   /api/public/intake/{token}/submit

红线:
  - 客户提交不直接覆盖 client_profiles, 必须代理 approve
  - token 状态机硬拦 (revoked/expired/submitted 全部 410)
  - AI suggest 限频: token 维度 5 次 / IP 维度 20/小时
  - 公开端错误不暴露 model/API key/internal stack
  - 不绕开 brand_access RBAC
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from auth.brand_access import require_brand_access
from db import intake_db
from db.profile_db import find_profile_by_name
from db.connection import get_connection
from services import intake_diff
from services.intake_ai import generate_intake_draft, public_safe_response, AI_SUGGESTABLE_FIELDS
from services import intake_events
from services.intake_flow import (
    compute_flow_for_brand,
    split_payload_by_namespace,
    SOCIAL_FIELD_KEYS,
)
from utils.brand_completeness import compute_brand_completeness

logger = logging.getLogger("GEO-Intake-API")

# 两个独立 router · 公开 router 不挂 prefix · 代理 router 也不挂(各自完整路径)
router = APIRouter(tags=["客户资料补全邀请"])
public_router = APIRouter(tags=["客户资料补全邀请·公开"])


# ==================== 工具 ====================

def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _client_ip(request: Request) -> str:
    return (
        request.headers.get("X-Real-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


def _get_brand(brand_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM brands WHERE id = %s", (brand_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _reserve_ai_suggest_slot(token_id: int, cap: int) -> Optional[int]:
    """[GEO-R1-CAN-104] 原子占位: 单条 conditional UPDATE 在模型调用前抢名额.
    读-判-写的 check-then-increment 会让并发请求全部读到同一个未自增的计数、
    全部通过上限校验、全部触发模型调用 (超额烧钱). 这里用带谓词的原子 UPDATE
    (ai_suggest_count < cap) 一步完成"占坑", 返回自增后的计数; 已达上限时不改行,
    返回 None 让调用方直接 429. 硬失败时用 _release_ai_suggest_slot 归还.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE intake_tokens
            SET ai_suggest_count = ai_suggest_count + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND ai_suggest_count < %s
            RETURNING ai_suggest_count
            """,
            (token_id, cap),
        )
        row = cur.fetchone()
        conn.commit()
        return row["ai_suggest_count"] if row else None
    finally:
        conn.close()


def _release_ai_suggest_slot(token_id: int) -> None:
    """[GEO-R1-CAN-104] 归还名额 (硬失败不烧名额, 保持原"degraded 才计数"语义).
    best-effort · 不因归还失败影响主流程. 下限保护避免降到负数."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE intake_tokens
            SET ai_suggest_count = GREATEST(ai_suggest_count - 1, 0),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (token_id,),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _serialize_token(record: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """返给代理 (含明文 token + url)."""
    token = record.get("token")
    base_url = str(request.base_url).rstrip("/")
    intake_url = f"{base_url}/intake/{token}" if token else None
    return {
        "id": record.get("id"),
        "token": token,
        "intake_url": intake_url,
        "brand_id": record.get("brand_id"),
        "diagnosis_id": record.get("diagnosis_id"),
        "quote_id": record.get("quote_id"),
        "status": record.get("status"),
        "ai_suggest_count": record.get("ai_suggest_count", 0),
        "expires_at": record.get("expires_at").isoformat() if record.get("expires_at") else None,
        "submitted_at": record.get("submitted_at").isoformat() if record.get("submitted_at") else None,
        "revoked_at": record.get("revoked_at").isoformat() if record.get("revoked_at") else None,
        "revoked_reason": record.get("revoked_reason"),
        "created_at": record.get("created_at").isoformat() if record.get("created_at") else None,
        "is_actionable": intake_db.is_token_actionable(record) is None,
    }


def _serialize_submission(submission: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(submission)
    for k in ("created_at", "updated_at", "reviewed_at"):
        if out.get(k) and hasattr(out[k], "isoformat"):
            out[k] = out[k].isoformat()
    return out


# ==================== 限频 (公开端 AI suggest) ====================

# in-memory fallback when Redis 不可用 — {(ip, hour_bucket): [timestamps]}
_ip_window: Dict[str, List[float]] = {}
_IP_WINDOW_LIMIT = 20
_IP_WINDOW_SECS = 3600


def _check_ip_rate_limit(ip: str) -> bool:
    """每 IP 每小时 20 次. 超限返 False."""
    try:
        from cache.redis_client import get_redis
        r = get_redis()
    except Exception:
        r = None

    if r is not None:
        now = time.time()
        key = f"intake:ai-suggest:ip:{ip}"
        try:
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, 0, now - _IP_WINDOW_SECS)
            pipe.zcard(key)
            pipe.zadd(key, {str(now): now})
            pipe.expire(key, _IP_WINDOW_SECS + 60)
            results = pipe.execute()
            count = results[1]
            if count >= _IP_WINDOW_LIMIT:
                try:
                    r.zrem(key, str(now))
                except Exception:
                    pass
                return False
            return True
        except Exception as e:
            logger.warning(f"[Intake] redis rate limit failed: {e}, fallback to memory")

    # in-memory fallback
    now = time.time()
    arr = _ip_window.setdefault(ip, [])
    arr[:] = [t for t in arr if now - t < _IP_WINDOW_SECS]
    if len(arr) >= _IP_WINDOW_LIMIT:
        return False
    arr.append(now)
    return True


# ==================== 请求/响应模型 ====================

class CreateTokenRequest(BaseModel):
    brand_id: int = Field(..., gt=0)
    diagnosis_id: Optional[int] = None
    quote_id: Optional[int] = None
    ttl_days: Optional[int] = Field(None, ge=1, le=30)


class RevokeTokenRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=200)


class SubmitIntakeRequest(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)
    ai_suggested: Optional[Dict[str, Any]] = None
    submitted_by_name: Optional[str] = Field(None, max_length=80)
    submitted_by_phone: Optional[str] = Field(None, max_length=40)


class AISuggestRequest(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)


# P0-7 分步采访
class IntakeStepRequest(BaseModel):
    step_index: int = Field(..., ge=0, le=64)
    payload: Dict[str, Any] = Field(default_factory=dict)
    answered_fields: List[str] = Field(default_factory=list)


class ApproveSubmissionRequest(BaseModel):
    approved_form_keys: List[str] = Field(default_factory=list)
    review_notes: Optional[str] = Field(None, max_length=500)


class RejectSubmissionRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


# ============================================================
# 代理端 endpoints (RBAC + brand_access)
# ============================================================

@router.post("/api/m3/intake-tokens", summary="生成客户资料补全邀请链接")
async def api_create_intake_token(data: CreateTokenRequest, request: Request):
    user = _get_user(request)
    require_brand_access(request, data.brand_id)

    brand = _get_brand(data.brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="品牌不存在")

    ttl = data.ttl_days or intake_db.DEFAULT_TOKEN_TTL_DAYS
    record = intake_db.create_intake_token(
        brand_id=data.brand_id,
        inviter_user_id=int(user["user_id"]),
        diagnosis_id=data.diagnosis_id,
        quote_id=data.quote_id,
        ttl_days=ttl,
    )
    return {"success": True, "token": _serialize_token(record, request)}


@router.get("/api/m3/intake-tokens", summary="查询某品牌的全部邀请链接")
async def api_list_intake_tokens(brand_id: int, request: Request):
    require_brand_access(request, brand_id)
    rows = intake_db.list_tokens_for_brand(brand_id)
    return {"items": [_serialize_token(r, request) for r in rows]}


@router.post("/api/m3/intake-tokens/{token_id}/revoke", summary="撤销邀请链接")
async def api_revoke_intake_token(
    token_id: int, data: RevokeTokenRequest, request: Request
):
    record = intake_db.get_token_by_id(token_id)
    if not record:
        raise HTTPException(status_code=404, detail="邀请链接不存在")
    require_brand_access(request, record["brand_id"])

    if record["status"] != intake_db.TOKEN_STATUS_ACTIVE:
        raise HTTPException(status_code=400, detail=f"链接已是 {record['status']} 状态, 不能撤销")

    ok = intake_db.revoke_token(token_id, reason=data.reason)
    if not ok:
        raise HTTPException(status_code=409, detail="撤销失败, 链接状态可能已变化")

    new_record = intake_db.get_token_by_id(token_id)
    return {"success": True, "token": _serialize_token(new_record, request)}


@router.get("/api/m3/profile-submissions", summary="查询客户资料提交列表")
async def api_list_submissions(
    brand_id: int,
    request: Request,
    status: Optional[str] = None,
):
    require_brand_access(request, brand_id)
    rows = intake_db.list_submissions_for_brand(brand_id, status=status)
    return {"items": [_serialize_submission(r) for r in rows]}


@router.get("/api/m3/profile-submissions/{submission_id}", summary="单条客户资料提交")
async def api_get_submission(submission_id: int, request: Request):
    submission = intake_db.get_submission(submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="提交记录不存在")
    require_brand_access(request, submission["brand_id"])
    return {"submission": _serialize_submission(submission)}


@router.post("/api/m3/profile-submissions/{submission_id}/approve",
             summary="审核通过 (支持部分字段合并)")
async def api_approve_submission(
    submission_id: int, data: ApproveSubmissionRequest, request: Request
):
    user = _get_user(request)
    submission = intake_db.get_submission(submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="提交记录不存在")
    require_brand_access(request, submission["brand_id"])

    if submission["status"] != intake_db.SUBMISSION_STATUS_PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"该提交已被 {submission['status']} 处理, 不能再次审核",
        )

    if not data.approved_form_keys:
        raise HTTPException(status_code=400, detail="未选择任何字段")

    # 校验 form_keys 都在 FIELD_MAP 内
    invalid = [k for k in data.approved_form_keys if k not in intake_diff.FIELD_MAP]
    if invalid:
        raise HTTPException(
            status_code=400, detail=f"未知字段: {', '.join(invalid)}"
        )

    # 合并写入
    merge_result = intake_diff.merge_approved_fields(
        submission=submission,
        brand_id=submission["brand_id"],
        approved_form_keys=data.approved_form_keys,
    )

    # 决定最终 status: 全选 + 全 applied → approved; 部分 → partially_approved
    payload = submission.get("payload_jsonb") or {}
    if isinstance(payload, str):
        import json as _j
        try:
            payload = _j.loads(payload)
        except Exception:
            payload = {}
    customer_filled_keys = [
        k for k in payload.keys()
        if k in intake_diff.FIELD_MAP and payload.get(k) not in (None, "", [])
    ]
    fully = (
        set(merge_result["applied"]) >= set(customer_filled_keys)
        and not merge_result["skipped"]
    )
    final_status = (
        intake_db.SUBMISSION_STATUS_APPROVED if fully
        else intake_db.SUBMISSION_STATUS_PARTIAL
    )

    ok = intake_db.update_submission_review(
        submission_id=submission_id,
        reviewer_id=int(user["user_id"]),
        status=final_status,
        approved_fields=merge_result["applied"],
        review_notes=data.review_notes,
    )
    if not ok:
        # 状态在中间被改了
        raise HTTPException(status_code=409, detail="审核状态已被并发更新, 请刷新")

    intake_events.emit_reviewed(
        submission_id=submission_id,
        brand_id=submission["brand_id"],
        review_status=final_status,
        applied_count=len(merge_result["applied"]),
        completeness_after=merge_result["completeness_after"],
        reviewer_id=int(user["user_id"]),
    )

    return {
        "success": True,
        "status": final_status,
        "applied": merge_result["applied"],
        "skipped": merge_result["skipped"],
        "completeness_before": merge_result["completeness_before"],
        "completeness_after": merge_result["completeness_after"],
        "profile_id": merge_result["profile_id"],
        "should_regenerate_report": (
            merge_result["completeness_after"] > merge_result["completeness_before"]
        ),
    }


@router.post("/api/m3/profile-submissions/{submission_id}/reject", summary="审核拒绝")
async def api_reject_submission(
    submission_id: int, data: RejectSubmissionRequest, request: Request
):
    user = _get_user(request)
    submission = intake_db.get_submission(submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="提交记录不存在")
    require_brand_access(request, submission["brand_id"])

    if submission["status"] != intake_db.SUBMISSION_STATUS_PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"该提交已被 {submission['status']} 处理, 不能拒绝",
        )

    ok = intake_db.update_submission_review(
        submission_id=submission_id,
        reviewer_id=int(user["user_id"]),
        status=intake_db.SUBMISSION_STATUS_REJECTED,
        approved_fields=[],
        review_notes=data.reason,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="审核状态已被并发更新, 请刷新")

    intake_events.emit_reviewed(
        submission_id=submission_id,
        brand_id=submission["brand_id"],
        review_status=intake_db.SUBMISSION_STATUS_REJECTED,
        applied_count=0,
        completeness_after=0,
        reviewer_id=int(user["user_id"]),
    )

    return {"success": True, "status": intake_db.SUBMISSION_STATUS_REJECTED}


# ============================================================
# 公开端 endpoints (无登录, token 验证)
# ============================================================

def _public_token_or_410(token: str) -> Dict[str, Any]:
    """统一 token 校验. 不透露内部细节."""
    record = intake_db.get_token_by_string(token)
    reason = intake_db.is_token_actionable(record)
    if reason == "token_not_found":
        raise HTTPException(status_code=404, detail="链接不存在或已失效")
    if reason in ("revoked", "submitted", "expired"):
        # 410 Gone: 资源已不可用
        msg_map = {
            "revoked": "链接已被撤销",
            "submitted": "你已经提交过资料, 顾问正在审核",
            "expired": "链接已过期, 请联系顾问重新发送",
        }
        raise HTTPException(status_code=410, detail=msg_map[reason])
    return record


def _build_public_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """给客户看的最小字段集. 严禁暴露 inviter_user_id / 利润 / 内部成本."""
    brand = _get_brand(record["brand_id"]) or {}

    profile = None
    if brand.get("name"):
        profile = find_profile_by_name(brand["name"], brand_id=record["brand_id"])

    # P0-7: 分步采访 flow + 草稿恢复
    flow_steps = compute_flow_for_brand(brand, profile or {})
    draft = intake_db.get_intake_draft(record["id"])

    completeness = compute_brand_completeness(brand, profile or {})

    # 已知基础资料 (展示给客户当作"我们已经知道的"·让客户聚焦补充缺的)
    known_basics = {
        "brand_name": brand.get("name") if brand.get("name") and not str(brand.get("name", "")).endswith(("的创作空间_", )) else None,
        "company_name": brand.get("company_name"),
        "industry": brand.get("industry"),
        "cities": brand.get("cities"),
    }
    # profile 已知字段
    if profile:
        for k in (
            "business", "target_users", "service_scope", "selling_points",
            "core_value", "company_intro",
        ):
            v = profile.get(k)
            if v not in (None, "", []):
                known_basics[k] = v

    # 哪些字段还缺 (按 brand_completeness.missing 列表)
    missing_keys = completeness.get("missing", [])

    # FIELD_MAP 字段 schema (前端渲染表单)
    fields_schema: List[Dict[str, Any]] = []
    for key, spec in intake_diff.FIELD_MAP.items():
        target_col = spec["column"]
        is_missing = (
            target_col in missing_keys
            or (target_col == "name" and "name" in missing_keys)
        )
        fields_schema.append({
            "form_key": key,
            "label": spec["label"],
            "table": spec["table"],
            "group": spec["group"],
            "type": spec["type"],
            "is_missing": is_missing,
            "current_value": (
                brand.get(target_col) if spec["table"] == "brands"
                else (profile or {}).get(target_col)
            ),
        })

    return {
        "token": record["token"],
        "expires_at": record["expires_at"].isoformat() if record.get("expires_at") else None,
        "ai_suggest_remaining": max(
            0, intake_db.MAX_AI_SUGGEST_PER_TOKEN - int(record.get("ai_suggest_count") or 0)
        ),
        "known_basics": {k: v for k, v in known_basics.items() if v not in (None, "", [])},
        "fields_schema": fields_schema,
        "completeness_now": completeness.get("score", 0),
        # P0-7: 分步采访
        "flow_steps": flow_steps,
        "draft_payload": draft.get("draft_payload", {}),
        "step_index": draft.get("step_index", 0),
        "answered_fields": draft.get("answered_fields", []),
        "draft_updated_at": draft.get("draft_updated_at"),
        "social_field_keys": list(SOCIAL_FIELD_KEYS),
    }


@public_router.get("/api/public/intake/{token}", summary="客户打开邀请链接")
async def api_public_intake_view(token: str, request: Request):
    record = _public_token_or_410(token)

    # 静默事件 (m3_customer_events 不存在或失败都不阻塞)
    try:
        intake_events.emit_opened(
            token=token,
            brand_id=record.get("brand_id"),
            diagnosis_id=record.get("diagnosis_id"),
            quote_id=record.get("quote_id"),
            ip=_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
    except Exception:
        pass

    return _build_public_view(record)


@public_router.post("/api/public/intake/{token}/ai-suggest", summary="AI 帮整理草稿")
async def api_public_ai_suggest(
    token: str, data: AISuggestRequest, request: Request
):
    record = _public_token_or_410(token)

    # IP 维度限频 (先挡突发, 不占 token 名额)
    ip = _client_ip(request)
    if not _check_ip_rate_limit(ip):
        raise HTTPException(
            status_code=429,
            detail="操作过于频繁, 请稍后再试",
        )

    # [GEO-R1-CAN-104] token 维度限频: 原子占位取代 check-then-increment.
    # 在模型调用之前用带谓词的单条 UPDATE 抢名额, 杜绝并发全部越过上限、
    # 全部触发模型 (超额烧钱). 名额抢不到 → 直接 429.
    reserved = _reserve_ai_suggest_slot(record["id"], intake_db.MAX_AI_SUGGEST_PER_TOKEN)
    if reserved is None:
        raise HTTPException(
            status_code=429,
            detail=f"AI 帮填已达上限({intake_db.MAX_AI_SUGGEST_PER_TOKEN} 次), 请直接手动填写",
        )

    brand = _get_brand(record["brand_id"]) or {}
    brand_hint = {
        "brand_name": brand.get("name"),
        "industry": brand.get("industry"),
        "cities": brand.get("cities"),
        "company_name": brand.get("company_name"),
    }

    try:
        result = await generate_intake_draft(
            form_payload=data.payload or {},
            brand_hint=brand_hint,
        )
    except Exception as e:
        logger.warning(f"[Intake] ai-suggest unexpected: {type(e).__name__}")
        # [GEO-R1-CAN-104] 硬失败归还名额 (保持原"degraded 才计数"语义, 不烧客户名额)
        _release_ai_suggest_slot(record["id"])
        # 公开端不返内部细节
        raise HTTPException(status_code=503, detail="AI 帮填暂不可用, 请稍后重试或手动填写")

    safe = public_safe_response(result)
    safe["ai_suggest_remaining"] = max(
        0,
        intake_db.MAX_AI_SUGGEST_PER_TOKEN - reserved,
    )
    safe["suggestable_fields"] = AI_SUGGESTABLE_FIELDS
    return safe


@public_router.post("/api/public/intake/{token}/step", summary="保存分步采访草稿")
async def api_public_save_step(token: str, data: IntakeStepRequest):
    """P0-7: 客户每答完一步保存进度. 不写正式 submission · 刷新可恢复.
    红线:
      - 只允许 active token (revoked/submitted/expired 走 410)
      - 不调 AI · 不扣费 · 不暴露任何 LLM 失败
      - draft_payload merge 而非覆盖 (客户回退改单字段不丢前面)
    """
    record = _public_token_or_410(token)

    # 读现有草稿 → merge → 写回
    existing = intake_db.get_intake_draft(record["id"])
    merged_payload: Dict[str, Any] = dict(existing.get("draft_payload") or {})
    incoming = data.payload or {}
    for k, v in incoming.items():
        # 只接受 str/list/dict · 排除明显异常
        if isinstance(k, str) and len(k) < 64:
            if v is None or (isinstance(v, str) and not v.strip()):
                # 客户清空字段 → 从草稿移除
                merged_payload.pop(k, None)
            elif isinstance(v, (str, list, dict, int, float, bool)):
                merged_payload[k] = v

    # answered_fields 单调累加 + 去重
    answered = list(existing.get("answered_fields") or [])
    seen = set(answered)
    for k in data.answered_fields or []:
        if isinstance(k, str) and k and k not in seen and len(k) < 64:
            answered.append(k)
            seen.add(k)

    intake_db.save_intake_draft(
        token_id=record["id"],
        step_index=data.step_index,
        draft_payload=merged_payload,
        answered_fields=answered,
    )

    return {
        "success": True,
        "step_index": data.step_index,
        "answered_fields": answered,
    }


@public_router.post("/api/public/intake/{token}/submit", summary="客户提交资料")
async def api_public_submit(token: str, data: SubmitIntakeRequest, request: Request):
    record = _public_token_or_410(token)

    # 本期不允许同 token 二次提交
    if intake_db.has_existing_submission_for_token(record["id"]):
        raise HTTPException(
            status_code=409,
            detail="你已经提交过资料, 顾问正在审核, 如需修改请联系顾问",
        )

    payload = data.payload or {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="资料格式不正确")

    # 算 diff (落库时一并存)
    brand = _get_brand(record["brand_id"]) or {}
    profile = None
    if brand.get("name"):
        profile = find_profile_by_name(brand["name"], brand_id=record["brand_id"])

    customer_norm, _notes, _rejected = intake_diff.normalize_payload(payload)
    diff = intake_diff.build_diff(
        customer_payload_normalized=customer_norm,
        ai_suggested=data.ai_suggested or None,
        current_brand=brand,
        current_profile=profile,
    )

    try:
        submission = intake_db.insert_submission(
            token_id=record["id"],
            brand_id=record["brand_id"],
            payload=payload,  # 存原始 payload 含 notes 字段
            ai_suggested=data.ai_suggested,
            diff=diff,
            submitted_by_name=data.submitted_by_name,
            submitted_by_phone=data.submitted_by_phone,
        )
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail="你已经提交过资料, 顾问正在审核, 如需修改请联系顾问",
        )
    intake_db.mark_token_submitted(record["id"])

    try:
        intake_events.emit_submitted(
            token=token,
            brand_id=record["brand_id"],
            submission_id=submission["id"],
            diagnosis_id=record.get("diagnosis_id"),
            quote_id=record.get("quote_id"),
            ip=_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
    except Exception:
        pass

    return {
        "success": True,
        "submission_id": submission["id"],
        "message": "已提交给顾问审核, 顾问确认后会更新到诊断/方案中。",
    }
