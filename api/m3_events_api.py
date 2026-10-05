"""
M3 客户行为事件 API (CTO-C 2026-04-26 · feat/m3-customer-signals)

3 个端点:
  POST /api/m3/customer-events/public
       公开埋点 · 无 JWT · 客户公开 token 链路调用
       rate-limit 60/min/IP · 永远不写原始 IP/UA/token (服务端立即 hash)

  GET /api/m3/customer-events
       代理读取时间线 · 验 JWT + RBAC (brand 级)
       params: brand_id / quote_id / diagnosis_id / source[] / days / limit

  GET /api/m3/customer-events/summary
       聚合摘要 · 给 M3 信号时间线 + M3 今日销售页 销售优先级用
       params: brand_id / quote_id / days

老板拍板:
  - 公开端口不允许写入原始 IP/UA/token · 只 hash 后入库
  - event_key UNIQUE 索引保证幂等 · 后端不依赖前端去重
  - 7 类事件白名单(opened / dwell_30s / dwell_120s / saw_price /
                  cta_click / submitted_keywords / renewed_interest)
  - 4 类来源白名单(public_report / public_quote / selection / portal)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime
from threading import Lock
from typing import Any, Deque, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from db.connection import get_connection
from db.m3_events_db import (
    ALLOWED_EVENT_TYPES,
    ALLOWED_SOURCES,
    hash_ip,
    hash_token,
    hash_ua,
    insert_event,
    query_events,
    summary_events,
)

logger = logging.getLogger("GEO-M3-Events-API")

router = APIRouter(tags=["M3 客户行为事件"])


# ============================================================
# Rate limit (per-IP · 60/min · 内存窗口)
# ============================================================

_RL_WINDOW_SECONDS = 60
_RL_MAX_PER_WINDOW = 60
_rl_buckets: Dict[str, Deque[float]] = {}
_rl_lock = Lock()


def _client_ip(request: Request) -> str:
    """提取 client IP · 优先 X-Forwarded-For (反代场景)"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "0.0.0.0"


def _check_rate_limit(ip: str) -> bool:
    """True = 允许 / False = 拒绝"""
    now = time.time()
    with _rl_lock:
        bucket = _rl_buckets.get(ip)
        if bucket is None:
            bucket = deque()
            _rl_buckets[ip] = bucket
        # 滑窗清理
        while bucket and (now - bucket[0]) > _RL_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= _RL_MAX_PER_WINDOW:
            return False
        bucket.append(now)
        # 简单内存治理 · 桶数过多时清最旧
        if len(_rl_buckets) > 5000:
            try:
                first_ip = next(iter(_rl_buckets))
                _rl_buckets.pop(first_ip, None)
            except Exception:
                pass
    return True


# ============================================================
# 公开埋点 (POST · 无 JWT)
# ============================================================

class PublicEventRequest(BaseModel):
    """公开埋点请求 · 字段全部可选 (除 source / event_type)

    注意:
      - 不收 ip / user_agent (服务端从 header 取并 hash)
      - 收 raw_token 但服务端立即 hash 后丢弃 · 永不入库
      - metadata 任意 JSON · 但前端 SDK 不应放滚动百分比 / 鼠标轨迹 / 表单内容
    """

    source: str = Field(..., min_length=1, max_length=32)
    event_type: str = Field(..., min_length=1, max_length=48)
    event_key: Optional[str] = Field(default=None, max_length=200)
    brand_id: Optional[int] = Field(default=None, ge=0)
    quote_id: Optional[int] = Field(default=None, ge=0)
    diagnosis_id: Optional[int] = Field(default=None, ge=0)
    raw_token: Optional[str] = Field(default=None, max_length=200)
    metadata: Optional[Dict[str, Any]] = Field(default=None)


def _sanitize_metadata(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """metadata 白名单 · 防客户端塞入 PII / 表单内容 / 长字段"""
    if not isinstance(meta, dict):
        return {}
    allowed_keys = {
        "device_class",   # mobile / desktop / wechat-webview
        "viewport",       # 'sm' / 'md' / 'lg'
        "dwell_seconds",  # 30 / 120
        "stage",          # 业务 stage hint (selection 'submit' 等)
    }
    out: Dict[str, Any] = {}
    for k, v in meta.items():
        if k not in allowed_keys:
            continue
        if isinstance(v, (int, float, bool)):
            out[k] = v
        elif isinstance(v, str):
            # 限长 · 不存超过 64 字符的串(防表单内容塞入)
            out[k] = v[:64]
    return out


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_expired(value: Any) -> bool:
    """Best-effort timestamp/date expiry check for public token tables."""
    if not value:
        return False
    try:
        text = str(value).replace("Z", "+00:00")
        expiry = datetime.fromisoformat(text)
        return datetime.now(expiry.tzinfo) > expiry
    except Exception:
        # Some legacy rows use date strings; lexical YYYY-MM-DD compare is safe here.
        return str(value) < datetime.now().strftime("%Y-%m-%d")


def _matches_optional(provided: Optional[int], expected: Optional[int]) -> bool:
    return provided is None or expected is None or int(provided) == int(expected)


def _quote_brand_id(cursor, quote_id: Optional[int]) -> Optional[int]:
    if not quote_id:
        return None
    cursor.execute("SELECT brand_id FROM quotes WHERE id = %s", (quote_id,))
    row = cursor.fetchone()
    return _as_int(row.get("brand_id")) if row else None


def _resolve_public_event_context(req: PublicEventRequest) -> Tuple[Optional[Dict[str, Optional[int]]], str]:
    """
    公开埋点 canonical context 反查.

    不信任前端传入的 brand_id/quote_id/diagnosis_id:
      - public_report 以 diagnosis_id 反查 diagnosis_records.brand_id
      - public_quote 以 share_code(raw_token) 反查 agent_quotes 是否有效
      - selection 以 token 反查 keyword_selection_sessions quote_id/brand_id
      - portal 以 token 反查 client_access_tokens quote_id, 再反查 quotes.brand_id
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if req.source == "public_report":
            if req.diagnosis_id is None:
                return None, "missing_diagnosis_id"
            cursor.execute(
                "SELECT id, brand_id FROM diagnosis_records WHERE id = %s",
                (req.diagnosis_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None, "invalid_report"
            brand_id = _as_int(row.get("brand_id"))
            if not _matches_optional(req.brand_id, brand_id):
                return None, "context_mismatch"
            if req.quote_id is not None:
                return None, "context_mismatch"
            return {
                "brand_id": brand_id,
                "quote_id": None,
                "diagnosis_id": _as_int(row.get("id")),
            }, "ok"

        if req.source == "public_quote":
            if not req.raw_token:
                return None, "missing_token"
            cursor.execute(
                """
                SELECT id FROM agent_quotes
                WHERE share_code = %s AND is_active = true
                """,
                (req.raw_token,),
            )
            if not cursor.fetchone():
                return None, "invalid_token"
            if req.brand_id is not None or req.quote_id is not None or req.diagnosis_id is not None:
                return None, "context_mismatch"
            return {"brand_id": None, "quote_id": None, "diagnosis_id": None}, "ok"

        if req.source == "selection":
            if not req.raw_token:
                return None, "missing_token"
            cursor.execute(
                """
                SELECT token, quote_id, brand_id, status, expires_at
                FROM keyword_selection_sessions
                WHERE token = %s
                """,
                (req.raw_token,),
            )
            row = cursor.fetchone()
            if not row or row.get("status") == "expired":
                return None, "invalid_token"
            terminal_statuses = {
                "confirmed",
                "pending_payment",
                "active",
                "payment_overdue",
                "pricing_pending_review",
                "business_lines_submitted",
            }
            if row.get("status") not in terminal_statuses and _is_expired(row.get("expires_at")):
                return None, "invalid_token"
            quote_id = _as_int(row.get("quote_id"))
            brand_id = _as_int(row.get("brand_id"))
            if not _matches_optional(req.quote_id, quote_id) or not _matches_optional(req.brand_id, brand_id):
                return None, "context_mismatch"
            if req.diagnosis_id is not None:
                return None, "context_mismatch"
            return {"brand_id": brand_id, "quote_id": quote_id, "diagnosis_id": None}, "ok"

        if req.source == "portal":
            if not req.raw_token:
                return None, "missing_token"
            cursor.execute(
                """
                SELECT quote_id, expires_at
                FROM client_access_tokens
                WHERE token = %s AND is_active = 1
                """,
                (req.raw_token,),
            )
            row = cursor.fetchone()
            if not row or _is_expired(row.get("expires_at")):
                return None, "invalid_token"
            quote_id = _as_int(row.get("quote_id"))
            brand_id = _quote_brand_id(cursor, quote_id)
            if not _matches_optional(req.quote_id, quote_id) or not _matches_optional(req.brand_id, brand_id):
                return None, "context_mismatch"
            if req.diagnosis_id is not None:
                return None, "context_mismatch"
            return {"brand_id": brand_id, "quote_id": quote_id, "diagnosis_id": None}, "ok"

        if req.source == "material_confirm":
            # CTO-F 2026-04-27 · /m/:token 客户写作资料确认链
            #   反查 marketing_confirm_sessions.token → brand_id
            #   仅在 status 仍是 pending/feedback/confirmed 才接受 (revoked/expired 不接事件)
            if not req.raw_token:
                return None, "missing_token"
            cursor.execute(
                """
                SELECT brand_id, status, expires_at
                FROM marketing_confirm_sessions
                WHERE token = %s
                """,
                (req.raw_token,),
            )
            row = cursor.fetchone()
            if not row:
                return None, "invalid_token"
            if row.get("status") in ("revoked",):
                return None, "invalid_token"
            # pending 也允许过期后的事件 (客户开了过期链接也想知道)
            brand_id = _as_int(row.get("brand_id"))
            if not _matches_optional(req.brand_id, brand_id):
                return None, "context_mismatch"
            if req.quote_id is not None or req.diagnosis_id is not None:
                return None, "context_mismatch"
            return {"brand_id": brand_id, "quote_id": None, "diagnosis_id": None}, "ok"

        return None, "invalid_source"
    except Exception as e:
        logger.warning(f"[M3-Events] public context resolve failed: {type(e).__name__}: {e}")
        return None, "context_lookup_failed"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _sanitize_event_key(
    event_key: Optional[str],
    *,
    raw_token: Optional[str],
    token_hash: Optional[str],
) -> Optional[str]:
    """Do not persist raw public tokens inside client-generated idempotency keys."""
    if not event_key:
        return None
    safe_key = event_key.strip()
    if raw_token and token_hash and raw_token in safe_key:
        safe_key = safe_key.replace(raw_token, token_hash)
    return safe_key[:200]


@router.post("/api/m3/customer-events/public")
def post_public_event(req: PublicEventRequest, request: Request):
    """公开端埋点 · 客户公开 token 链路调用 · 无 JWT · rate-limit 60/min/IP

    永远不写原始 IP/UA/token (服务端立即 hash · raw 值不入库)
    """
    ip = _client_ip(request)
    if not _check_rate_limit(ip):
        # 静默 200 (不让客户端重试 · 不阻塞页面)
        return {"success": False, "reason": "rate_limited"}

    if req.source not in ALLOWED_SOURCES:
        return {"success": False, "reason": "invalid_source"}
    if req.event_type not in ALLOWED_EVENT_TYPES:
        return {"success": False, "reason": "invalid_event_type"}

    canonical, context_reason = _resolve_public_event_context(req)
    if not canonical:
        # 静默 200 · 不阻塞公开页, 但不写入任何未校验事件
        return {"success": False, "reason": context_reason}

    ua = request.headers.get("User-Agent")

    # ⚠ 立即 hash · 原值不留 (老板红线)
    ip_h = hash_ip(ip)
    ua_h = hash_ua(ua)
    token_h = hash_token(req.raw_token) if req.raw_token else None
    event_key = _sanitize_event_key(
        req.event_key,
        raw_token=req.raw_token,
        token_hash=token_h,
    )

    metadata_clean = _sanitize_metadata(req.metadata)

    inserted, _evid = insert_event(
        source=req.source,
        event_type=req.event_type,
        event_key=event_key,
        brand_id=canonical.get("brand_id"),
        quote_id=canonical.get("quote_id"),
        diagnosis_id=canonical.get("diagnosis_id"),
        token_hash=token_h,
        ip_hash=ip_h,
        user_agent_hash=ua_h,
        metadata=metadata_clean,
    )

    # raw_token 等已经在函数返回前丢弃 · 不再持有引用
    return {
        "success": True,
        "inserted": inserted,
        "deduped": (not inserted) and bool(event_key),
    }


# ============================================================
# 代理读 (GET · 验 JWT + RBAC)
# ============================================================

def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _check_brand_rbac(request: Request, brand_id: Optional[int]) -> None:
    """有 brand_id 时校验 brand 级 access · 无 brand_id 时仅 admin 可全局查"""
    user = _get_user(request)
    if brand_id is None:
        if not user.get("is_admin"):
            raise HTTPException(
                status_code=400,
                detail="必须提供 brand_id (非 admin 不允许全局查询)",
            )
        return
    try:
        from auth.brand_access import require_brand_access
        require_brand_access(request, brand_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[M3-Events] RBAC 校验异常 brand={brand_id}: {e}")
        raise HTTPException(status_code=403, detail="无权访问该 brand")


@router.get("/api/m3/customer-events")
def list_customer_events(
    request: Request,
    brand_id: Optional[int] = Query(default=None, ge=0),
    quote_id: Optional[int] = Query(default=None, ge=0),
    diagnosis_id: Optional[int] = Query(default=None, ge=0),
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=500),
    source: Optional[List[str]] = Query(default=None),
):
    """代理拉客户行为事件时间线"""
    _check_brand_rbac(request, brand_id)
    events = query_events(
        brand_id=brand_id,
        quote_id=quote_id,
        diagnosis_id=diagnosis_id,
        sources=source,
        days=days,
        limit=limit,
    )
    return {
        "success": True,
        "events": events,
        "total": len(events),
    }


@router.get("/api/m3/customer-events/summary")
def get_customer_events_summary(
    request: Request,
    brand_id: Optional[int] = Query(default=None, ge=0),
    quote_id: Optional[int] = Query(default=None, ge=0),
    days: int = Query(default=30, ge=1, le=365),
):
    """聚合摘要 · 给 M3 信号时间线 + M3 今日销售页 why-now 算法用

    返回:
      {
        success: true,
        per_source: { public_report: { counts: {opened: 3, ...}, last_at: ISO }, ... },
        latest_overall_at: ISO,
        opened_24h: { public_report: 2, ... },
        reopened_24h: bool,
      }
    """
    _check_brand_rbac(request, brand_id)
    summary = summary_events(brand_id=brand_id, quote_id=quote_id, days=days)
    return {"success": True, **summary}
