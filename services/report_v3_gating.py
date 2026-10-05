"""Feature gating helpers for public report v3."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from config.settings_manager import SystemSettings
from db.connection import get_connection


def _as_user_dict(request: Request | Any) -> dict:
    state = getattr(request, "state", None)
    user = getattr(state, "user", None) if state is not None else None
    return user if isinstance(user, dict) else {}


def resolve_report_v3_subject_user_id(
    *,
    request: Request | Any,
    diagnosis_record: dict,
    shared_by: int | None,
) -> int | None:
    """Resolve the user id used for v3 whitelist decisions."""
    if shared_by:
        return int(shared_by)

    if diagnosis_record.get("brand_owner_user_id"):
        return int(diagnosis_record["brand_owner_user_id"])

    if diagnosis_record.get("brand_id"):
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT owner_user_id FROM brands WHERE id = %s",
                (diagnosis_record["brand_id"],),
            )
            row = cursor.fetchone()
            if row and row.get("owner_user_id"):
                return int(row["owner_user_id"])
        finally:
            conn.close()

    user = _as_user_dict(request)
    if user.get("user_id"):
        return int(user["user_id"])
    return None


def is_admin_preview_request(request: Request | Any) -> bool:
    user = _as_user_dict(request)
    query_params = getattr(request, "query_params", {}) or {}
    return bool(user.get("is_admin") and query_params.get("preview_v3") == "1")


def should_render_report_v3(
    settings: SystemSettings,
    subject_user_id: int | None,
    is_admin_preview: bool,
) -> bool:
    """Single source of truth for report v3 rollout gating."""
    if is_admin_preview:
        return True
    if getattr(settings, "report_v3_enabled", False):
        return True
    whitelist = getattr(settings, "report_v3_whitelist_user_ids", []) or []
    return bool(subject_user_id and int(subject_user_id) in whitelist)
