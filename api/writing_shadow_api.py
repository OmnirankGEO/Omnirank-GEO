"""
Admin-only writing shadow artifact API.

This module lists local R6-H shadow-only artifacts for review. It does not
trigger generation, does not expose sidecar contents, and does not grant
customer output.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("GEO-Writing-Shadow-API")

router = APIRouter(prefix="/api/writing", tags=["写作 Shadow"])

DEFAULT_SHADOW_ARTIFACT_ROOT = (
    "qa-artifacts/r6c_20260619/r6e_shadow_harness/admin_shadow_runs"
)


def _shadow_artifact_root() -> Path:
    return Path(os.getenv("R6H_SHADOW_ARTIFACT_ROOT") or DEFAULT_SHADOW_ARTIFACT_ROOT)


@router.get("/shadow-runs")
def list_writing_shadow_runs(request: Request):
    """List admin-only shadow artifacts without exposing evidence sidecars."""

    try:
        user = getattr(request.state, "user", None)
        if not user:
            raise HTTPException(status_code=401, detail="未登录")
        if not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="需要管理员权限")

        from writing.shadow_only_injection import list_shadow_run_artifacts

        try:
            listing = list_shadow_run_artifacts(_shadow_artifact_root())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "success": True,
            "data": listing,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("获取写作 Shadow 运行失败: %s", exc)
        raise HTTPException(status_code=500, detail="获取写作 Shadow 运行失败") from exc


@router.get("/shadow-runs/{run_id}")
def get_writing_shadow_run_detail(run_id: str, request: Request):
    """Read one admin-only shadow artifact as a safe review projection."""

    try:
        user = getattr(request.state, "user", None)
        if not user:
            raise HTTPException(status_code=401, detail="未登录")
        if not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="需要管理员权限")

        from writing.shadow_only_injection import get_shadow_run_artifact_detail

        try:
            detail = get_shadow_run_artifact_detail(_shadow_artifact_root(), run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Shadow 运行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "success": True,
            "data": detail,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("获取写作 Shadow 运行详情失败: %s", exc)
        raise HTTPException(status_code=500, detail="获取写作 Shadow 运行详情失败") from exc
