"""Admin API for writing/flywheel runtime feature switches."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from writing.feature_switches import (
    FeatureSwitchConflict,
    FeatureSwitchConfirmationRequired,
    FeatureSwitchError,
    FeatureSwitchLocked,
    FeatureSwitchNotFound,
    get_feature_switch_state,
    update_feature_switch,
)
from auth.user_ctx import current_user_id


logger = logging.getLogger("GEO-Writing-Feature-Switch-API")

router = APIRouter(prefix="/api/writing/feature-switches", tags=["写作功能开关"])


class FeatureSwitchUpdateRequest(BaseModel):
    enabled: bool
    expected_config_version: int = Field(..., ge=0)
    note: str = Field("", max_length=500)
    confirm: str = Field("", max_length=100)


def _require_admin(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _error_response(exc: Exception) -> HTTPException:
    message = str(exc)
    if isinstance(exc, FeatureSwitchConflict):
        return HTTPException(status_code=409, detail={"code": "config_version_conflict", "message": message})
    if isinstance(exc, FeatureSwitchNotFound):
        return HTTPException(status_code=404, detail={"code": "feature_switch_not_found", "message": message})
    if isinstance(exc, FeatureSwitchLocked):
        return HTTPException(status_code=403, detail={"code": "feature_switch_locked", "message": message})
    if isinstance(exc, FeatureSwitchConfirmationRequired):
        return HTTPException(status_code=409, detail={"code": "confirmation_required", "message": message})
    return HTTPException(status_code=500, detail="功能开关操作失败")


def _with_runtime_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach safe runtime facts that help admins compare UI and backend state."""
    try:
        from writing.style_control import load_control_state

        control_state = load_control_state()
        active_by_style = control_state.get("active_by_style") or {}
        active_count = len([value for value in active_by_style.values() if value])
    except Exception:
        active_count = 0
    for item in payload.get("switches") or []:
        if item.get("key") == "writing_style_overrides":
            item["runtime"] = {
                "active_style_count": active_count,
                "effective_enabled": bool(item.get("enabled")) and active_count > 0,
            }
        elif item.get("key") == "r6h_customer_output":
            item["runtime"] = {"effective_enabled": False, "permanently_locked": True}
        else:
            item["runtime"] = {"effective_enabled": bool(item.get("enabled"))}
    return payload


@router.get("")
def list_writing_feature_switches(request: Request):
    """Return safe admin-only switch projections."""
    _require_admin(request)
    try:
        return _with_runtime_status(get_feature_switch_state())
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("获取写作功能开关失败: %s", exc)
        raise HTTPException(status_code=500, detail="获取写作功能开关失败") from exc


@router.put("/{switch_key}")
def update_writing_feature_switch(switch_key: str, req: FeatureSwitchUpdateRequest, request: Request):
    """Update one runtime switch with optimistic locking."""
    user = _require_admin(request)
    try:
        result = update_feature_switch(
            switch_key,
            req.enabled,
            actor_id=int(current_user_id(user) or user.get("user_id") or 0),
            expected_config_version=req.expected_config_version,
            note=req.note,
            confirm=req.confirm,
        )
        return _with_runtime_status({**get_feature_switch_state(), "updated": result.get("switch")})
    except HTTPException:
        raise
    except FeatureSwitchError as exc:
        raise _error_response(exc) from exc
    except Exception as exc:
        logger.error("更新写作功能开关失败: %s", exc)
        raise HTTPException(status_code=500, detail="更新写作功能开关失败") from exc
