"""Diagnosis brand-cell human review API (板块 A · 2026-07-22).

契约风格对齐监测侧 ``/api/monitoring/identity-reviews``:

- ``GET  /api/diagnosis/{diagnosis_id}/brand-cells``
  列出每个 question×engine 单元格的五态 verdict、matched_text、候选名、证据摘要。
  数据全部来自 diagnosis_records.raw_data_json + 本地 resolver,**不调 LLM**。

- ``POST /api/diagnosis/{diagnosis_id}/brand-cells/decision``
  body: {question, engine, action: confirm_yes|confirm_no|add_alias,
         selected_name?, reason?, request_id, expected_version?}
  纯本地重判(provider_calls=0 / billing writes=0 / 不覆盖原始回答)→
  落共享 decision(source_kind='diagnosis')→ 安全别名持久化(同 tenant+brand)→
  单事务原子重算(dimension_stats→funnel→总分/等级→records+modules_jsonb→
  brands.latest_score)→ 返 {success, cell, aggregates, score, level}。

幂等:同 request_id 同参返既有结果;同键异参 409。
权限:require_diagnosis_access(内含 require_brand_access + 组织 artifact 闸)+
服务层活体 RBAC 复核(与监测 decide 同语义);跨租户/跨品牌 403/404。
审计:events 落 ip(取 request.client)/reason/metadata。

总闸:env ``BRAND_IDENTITY_REVIEW_ENABLED``(默认 true;false → 404,fail-closed)。
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("GEO-DiagnosisIdentityAPI")

router = APIRouter(prefix="/api/diagnosis", tags=["诊断品牌身份确认"])


def _review_enabled() -> bool:
    return os.getenv("BRAND_IDENTITY_REVIEW_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


class DiagnosisBrandCellDecisionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    engine: str = Field(min_length=1, max_length=50)
    action: Literal["confirm_yes", "confirm_no", "add_alias"]
    selected_name: Optional[str] = Field(default=None, max_length=80)
    reason: Optional[str] = Field(default=None, max_length=500)
    request_id: uuid.UUID
    expected_version: int = Field(default=0, ge=0)


def _resolve_brand_id(request: Request, diagnosis_id: int) -> tuple[int, dict]:
    """Authorize and return (brand_id, diagnosis_record); 404 on miss, 403 on RBAC."""
    from auth.brand_access import require_diagnosis_access

    record = require_diagnosis_access(request, diagnosis_id)
    brand_id = record.get("brand_id")
    if brand_id is None:
        # 无 brand 归属的残缺行:决策无处锚定,fail-closed(与 require_brand_access 同口径)
        raise HTTPException(status_code=404, detail="诊断记录未关联品牌，无法确认")
    return int(brand_id), record


def _actor_user_id(request: Request) -> int:
    user = getattr(request.state, "user", None) or {}
    raw_actor = user.get("user_id") or user.get("id")
    try:
        actor = int(raw_actor)
        if actor <= 0:
            raise ValueError
        return actor
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="当前账号无法执行确认")


def _client_ip(request: Request) -> Optional[str]:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host) if host else None


@router.get("/{diagnosis_id}/brand-cells")
def get_diagnosis_brand_cells(diagnosis_id: int, request: Request):
    """列出诊断的 question×engine 品牌判定单元格(五态 + 候选 + 证据)。"""
    if not _review_enabled():
        raise HTTPException(status_code=404, detail="功能未启用")
    from services.diagnosis_identity_decision import (
        DiagnosisIdentityReviewNotFound,
        list_brand_cells,
    )

    brand_id, _record = _resolve_brand_id(request, diagnosis_id)
    try:
        data = list_brand_cells(diagnosis_id, brand_id)
    except DiagnosisIdentityReviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "success", "data": data}


@router.post("/{diagnosis_id}/brand-cells/decision")
def decide_diagnosis_brand_cell(
    diagnosis_id: int,
    payload: DiagnosisBrandCellDecisionRequest,
    request: Request,
):
    """对单个「疑似提到」单元格落人工确认,并单事务原子重算分数。"""
    if not _review_enabled():
        raise HTTPException(status_code=404, detail="功能未启用")
    from services.diagnosis_identity_decision import (
        DiagnosisIdentityReviewConflict,
        DiagnosisIdentityReviewNotFound,
        decide_brand_cell,
    )

    brand_id, _record = _resolve_brand_id(request, diagnosis_id)
    actor_user_id = _actor_user_id(request)
    user = getattr(request.state, "user", None) or {}
    try:
        result = decide_brand_cell(
            diagnosis_id=diagnosis_id,
            brand_id=brand_id,
            actor_user_id=actor_user_id,
            question=payload.question,
            engine=payload.engine,
            action=payload.action,
            selected_name=payload.selected_name,
            reason=payload.reason,
            request_id=str(payload.request_id),
            expected_version=payload.expected_version,
            ip=_client_ip(request),
            actor_is_admin=bool(user.get("is_admin")),
        )
        return {"status": "success", **result}
    except DiagnosisIdentityReviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except DiagnosisIdentityReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        # [集中严审 R1 · P2-1] 入参/语义校验失败按契约文档统一 400（不再 422）
        raise HTTPException(status_code=400, detail=str(exc))
