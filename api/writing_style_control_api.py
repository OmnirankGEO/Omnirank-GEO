"""Admin-only writing style control console API.

This router exposes the JSON-backed writing style version control plane used by
the writing settings console.  It never publishes customer output, never calls
an LLM, and returns safe projections instead of raw prompt text.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from writing.style_control import (
    StyleControlBlocked,
    StyleControlConflict,
    StyleControlNotFound,
    activate_style_version,
    align_draft_from_flywheel,
    build_flywheel_summary,
    get_evidence_chain,
    get_style_version,
    list_audit_events,
    list_style_versions,
    retire_style_version,
    rollback_style,
)
from auth.user_ctx import current_user_id


logger = logging.getLogger("GEO-Writing-Style-Control-API")

router = APIRouter(prefix="/api/writing/style-control", tags=["写作文体控制台"])


class AlignDraftRequest(BaseModel):
    style_code: str = Field(..., min_length=1, max_length=100)
    industry_key: str = Field("general", min_length=1, max_length=100)
    evidence_mode: str = Field("with_evidence", max_length=50)
    risk_level: str = Field("normal", max_length=50)
    expected_config_version: int = Field(..., ge=0)
    source_summary: dict[str, Any] | None = None
    allowed_industries: list[str] = Field(default_factory=list)
    blocked_industries: list[str] = Field(default_factory=list)


class ActivateRequest(BaseModel):
    version_id: str = Field(..., min_length=1, max_length=200)
    expected_config_version: int = Field(..., ge=0)
    note: str = Field("", max_length=1000)
    confirm: str = Field("", max_length=100)


class RollbackRequest(BaseModel):
    style_code: str = Field(..., min_length=1, max_length=100)
    expected_config_version: int = Field(..., ge=0)
    note: str = Field("", max_length=1000)


class RetireRequest(BaseModel):
    version_id: str = Field(..., min_length=1, max_length=200)
    expected_config_version: int = Field(..., ge=0)
    note: str = Field("", max_length=1000)


def _require_admin(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _error_response(exc: Exception) -> HTTPException:
    message = str(exc)
    if isinstance(exc, StyleControlConflict):
        return HTTPException(status_code=409, detail={"code": "config_version_conflict", "message": message})
    if isinstance(exc, StyleControlNotFound):
        return HTTPException(status_code=404, detail={"code": "version_not_found", "message": message})
    if isinstance(exc, StyleControlBlocked):
        return HTTPException(status_code=409, detail={"code": "style_control_blocked", "message": message})
    return HTTPException(status_code=500, detail="写作文体控制台操作失败")


def _safe_version(version: dict[str, Any]) -> dict[str, Any]:
    guard = version.get("guard_summary") or {}
    judge = version.get("judge_summary") or {}
    rollout = version.get("rollout_recommendation") or {}
    return {
        "version_id": version.get("version_id"),
        "style_code": version.get("style_code"),
        "style_name": version.get("style_name"),
        "source": version.get("source"),
        "status": version.get("status"),
        "prompt_sha256": version.get("prompt_sha256"),
        "strategy_summary": version.get("strategy_summary"),
        "guard_summary": {
            "decision": guard.get("decision"),
            "worst_action": guard.get("worst_action"),
            "finding_count": len(guard.get("findings") or []),
            "findings": guard.get("findings") or [],
        },
        "judge_summary": {
            "semantic_decision": judge.get("semantic_decision"),
            "provider_count": int(judge.get("provider_count") or 0),
            "note": judge.get("note", ""),
        },
        "created_at": version.get("created_at"),
        "created_by": version.get("created_by"),
        "activated_at": version.get("activated_at"),
        "stable_baseline": bool(version.get("stable_baseline")),
        "rollback_parent": version.get("rollback_parent"),
        "sample_count": int(version.get("sample_count") or 0),
        "control_sample_count": int(version.get("control_sample_count") or 0),
        "risk_level": version.get("risk_level"),
        "allowed_industries": version.get("allowed_industries") or [],
        "blocked_industries": version.get("blocked_industries") or [],
        "rollout_recommendation": {
            "eligibility": rollout.get("eligibility"),
            "reason": rollout.get("reason"),
            "may_activate": bool(rollout.get("may_activate")),
            "customer_output_allowed": False,
        },
    }


def _safe_state(state: dict[str, Any]) -> dict[str, Any]:
    versions = state.get("versions") or []
    by_status: dict[str, int] = {}
    for version in versions:
        status = str(version.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "schema_version": state.get("schema_version"),
        "config_version": state.get("config_version"),
        "updated_at": state.get("updated_at"),
        "active_by_style": state.get("active_by_style") or {},
        "stable_by_style": state.get("stable_by_style") or {},
        "version_count": len(versions),
        "by_status": by_status,
    }


def _safe_chain(chain: dict[str, Any]) -> dict[str, Any]:
    evidence_chain = chain.get("evidence_chain") or {}
    return {
        "config_version": chain.get("config_version"),
        "version_id": chain.get("version_id"),
        "style_code": chain.get("style_code"),
        "status": chain.get("status"),
        "evidence_chain": {
            "summary": evidence_chain.get("summary", ""),
            "why_new_is_better": evidence_chain.get("why_new_is_better") or [],
            "sample_threshold": evidence_chain.get("sample_threshold") or {},
            "evidence_mode": evidence_chain.get("evidence_mode"),
            "source_file_count": len(evidence_chain.get("source_files") or []),
        },
        "guard_summary": chain.get("guard_summary") or {},
        "judge_summary": chain.get("judge_summary") or {},
        "rollout_recommendation": {
            **(chain.get("rollout_recommendation") or {}),
            "customer_output_allowed": False,
        },
    }


def _safe_audit_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": event.get("created_at"),
        "action": event.get("action"),
        "actor_id": event.get("actor_id"),
        "style_code": event.get("style_code"),
        "version_id": event.get("version_id"),
        "from_status": event.get("from_status"),
        "to_status": event.get("to_status"),
        "previous_active_id": event.get("previous_active_id"),
        "note": event.get("note", ""),
    }


@router.get("/versions")
def writing_style_control_versions(
    request: Request,
    style_code: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None, max_length=50),
):
    """List writing style versions as safe admin projections."""

    _require_admin(request)
    try:
        payload = list_style_versions(style_code=style_code, status=status)
        return {
            "success": True,
            "data": {
                **_safe_state(payload),
                "versions": [_safe_version(v) for v in payload.get("versions") or []],
                "customer_output_allowed": False,
            },
        }
    except Exception as exc:
        logger.error("获取文体版本失败: %s", exc)
        raise _error_response(exc) from exc


@router.get("/versions/{version_id}")
def writing_style_control_version_detail(version_id: str, request: Request):
    """Read one version as a safe projection without prompt text."""

    _require_admin(request)
    try:
        payload = get_style_version(version_id)
        return {
            "success": True,
            "data": {
                "config_version": payload.get("config_version"),
                "version": _safe_version(payload.get("version") or {}),
                "customer_output_allowed": False,
            },
        }
    except Exception as exc:
        logger.error("获取文体版本详情失败: %s", exc)
        raise _error_response(exc) from exc


@router.post("/align-draft")
def writing_style_control_align_draft(req: AlignDraftRequest, request: Request):
    """Create a draft candidate from flywheel signals; never activates it."""

    user = _require_admin(request)
    try:
        result = align_draft_from_flywheel(
            style_code=req.style_code,
            industry_key=req.industry_key,
            evidence_mode=req.evidence_mode,
            risk_level=req.risk_level,
            actor_id=int(current_user_id(user) or 0),
            expected_config_version=req.expected_config_version,
            source_summary=req.source_summary,
            allowed_industries=req.allowed_industries,
            blocked_industries=req.blocked_industries,
        )
        return {
            "success": True,
            "data": {
                "version": _safe_version(result["version"]),
                "state": _safe_state(result["state"]),
                "customer_output_allowed": False,
            },
        }
    except Exception as exc:
        logger.error("一键对齐生成 draft 失败: %s", exc)
        raise _error_response(exc) from exc


@router.post("/activate")
def writing_style_control_activate(req: ActivateRequest, request: Request):
    """Activate a reviewed version by syncing it to prompt_overrides."""

    user = _require_admin(request)
    try:
        if req.confirm != "ACTIVATE_WRITING_STYLE_VERSION":
            raise StyleControlBlocked("activate_confirmation_required")
        result = activate_style_version(
            version_id=req.version_id,
            actor_id=int(current_user_id(user) or 0),
            expected_config_version=req.expected_config_version,
            note=req.note,
        )
        return {
            "success": True,
            "data": {
                "version": _safe_version(result["version"]),
                "state": _safe_state(result["state"]),
                "customer_output_allowed": False,
            },
        }
    except Exception as exc:
        logger.error("启用文体版本失败: %s", exc)
        raise _error_response(exc) from exc


@router.post("/rollback")
def writing_style_control_rollback(req: RollbackRequest, request: Request):
    """Rollback a style to its stable baseline prompt."""

    user = _require_admin(request)
    try:
        result = rollback_style(
            style_code=req.style_code,
            actor_id=int(current_user_id(user) or 0),
            expected_config_version=req.expected_config_version,
            note=req.note,
        )
        return {
            "success": True,
            "data": {
                "active_version": _safe_version(result["active_version"]),
                "state": _safe_state(result["state"]),
                "customer_output_allowed": False,
            },
        }
    except Exception as exc:
        logger.error("回退文体版本失败: %s", exc)
        raise _error_response(exc) from exc


@router.post("/retire")
def writing_style_control_retire(req: RetireRequest, request: Request):
    """Retire a non-active, non-baseline version."""

    user = _require_admin(request)
    try:
        result = retire_style_version(
            version_id=req.version_id,
            actor_id=int(current_user_id(user) or 0),
            expected_config_version=req.expected_config_version,
            note=req.note,
        )
        return {
            "success": True,
            "data": {
                "version": _safe_version(result["version"]),
                "state": _safe_state(result["state"]),
                "customer_output_allowed": False,
            },
        }
    except Exception as exc:
        logger.error("退役文体版本失败: %s", exc)
        raise _error_response(exc) from exc


@router.get("/evidence-chain/{version_id}")
def writing_style_control_evidence_chain(version_id: str, request: Request):
    """Read safe evidence-chain summary for one version."""

    _require_admin(request)
    try:
        return {"success": True, "data": _safe_chain(get_evidence_chain(version_id))}
    except Exception as exc:
        logger.error("获取文体证据链失败: %s", exc)
        raise _error_response(exc) from exc


@router.get("/audit-log")
def writing_style_control_audit_log(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    """List safe audit events for admin review."""

    _require_admin(request)
    try:
        return {
            "success": True,
            "data": {
                "events": [_safe_audit_event(e) for e in list_audit_events(limit=limit)],
                "customer_output_allowed": False,
            },
        }
    except Exception as exc:
        logger.error("获取文体审计日志失败: %s", exc)
        raise _error_response(exc) from exc


@router.get("/flywheel-summary")
def writing_style_control_flywheel_summary(
    request: Request,
    industry_key: str = Query(default="general", max_length=100),
    style_code: str = Query(default="buying_guide", max_length=100),
):
    """Read local flywheel summary used by one-click alignment."""

    _require_admin(request)
    try:
        summary = build_flywheel_summary(industry_key=industry_key, style_code=style_code)
        summary = {
            **summary,
            "source_file_count": len(summary.get("source_files") or []),
            "source_files": [],
            "customer_output_allowed": False,
        }
        return {"success": True, "data": summary}
    except Exception as exc:
        logger.error("获取飞轮摘要失败: %s", exc)
        raise _error_response(exc) from exc
