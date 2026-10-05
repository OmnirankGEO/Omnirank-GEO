"""geo_observation_audit 写入(必须与状态变化同事务)。

裁定:审计写入与业务状态变化同事务;失败整体回滚(由调用方的事务边界保证)。
唯一门用 audit_event_key(禁 request_id 单列唯一,因一次请求可能产生 claim/review/promote/withdraw 多条)。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Optional

from psycopg2.extras import Json


def compute_audit_key(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()


def write_audit(
    cur,
    action: str,
    operator_type: str,
    *,
    event_id: Optional[int] = None,
    operator_id: Optional[str] = None,
    before: Optional[Any] = None,
    after: Optional[Any] = None,
    reason_codes: Optional[list] = None,
    evidence: Optional[Any] = None,
    request_id: Optional[str] = None,
    idempotency_token: Optional[str] = None,
) -> bool:
    """在给定 cursor(=调用方业务事务)上写一条审计。返回是否新插入(幂等命中返回 False)。

    idempotency:
      - 显式 idempotency_token 时:key=hash(event_id, action, request_id, token) → 幂等去重;
      - 否则若 event_id+request_id 都有:key=hash(event_id, action, request_id) → 幂等去重;
      - 否则(纯系统日志无请求上下文):附 uuid → 每次都插入(不去重)。
    """
    if operator_type not in ("system", "admin"):
        raise ValueError("operator_type 必须是 system/admin")
    if idempotency_token is not None:
        key = compute_audit_key(str(event_id or ""), action, request_id or "", idempotency_token)
    elif event_id is not None and request_id is not None:
        key = compute_audit_key(str(event_id), action, request_id)
    else:
        key = compute_audit_key(str(event_id or ""), action, request_id or "", uuid.uuid4().hex)

    cur.execute(
        """
        INSERT INTO public.geo_observation_audit
            (event_id, action, operator_type, operator_id, before_json, after_json,
             reason_codes, evidence_json, request_id, audit_event_key)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (audit_event_key) DO NOTHING
        RETURNING id
        """,
        (
            event_id,
            action,
            operator_type,
            operator_id,
            Json(before) if before is not None else None,
            Json(after) if after is not None else None,
            Json(reason_codes if reason_codes is not None else []),
            Json(evidence) if evidence is not None else None,
            request_id,
            key,
        ),
    )
    row = cur.fetchone()
    return row is not None
