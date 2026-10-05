from __future__ import annotations

from typing import Any

PROFILE_GUARDED_TOOLS = {
    "internal_memory_query",
    "internal_profile_get",
    "internal_history_search",
    "update_profile_taboo",
    "update_profile_memory",
    "confirm_memory_conflict",
    "archive_memory_event",
    "update_profile_field",
}


def verify_tool_access(tool_name: str, args: dict[str, Any], ctx: Any) -> str | None:
    """H-P0-3 fix · profile_id 缺失即放行的安全漏洞修复.

    修前:`if not profile_id: return None` · 把 profile_id 漏掉作为合法调用
    放行 · 任何 internal_/update_ tool 可以不传 profile_id 绕过 owner_guard
    + 配合 update_profile_field allowlist 漏(H-P0-4)等 prompt injection
    可以悄悄改任意用户数据。
    修后:GUARDED_TOOLS 必须传 profile_id · 否则 permission_denied。
    """
    if tool_name not in PROFILE_GUARDED_TOOLS:
        return None
    profile_id = args.get("profile_id")
    # H-P0-3 fix · GUARDED_TOOLS 必须传 profile_id · 缺失 = 拒绝(不是放行)
    if not profile_id:
        return "permission_denied"
    if getattr(ctx, "enforce_owner_guard", True) is False:
        return None
    if getattr(ctx, "user_id", None) is None:
        return "permission_denied"
    selected_profile_id = getattr(ctx, "profile_id", None)
    if selected_profile_id and str(selected_profile_id) != str(profile_id):
        return "permission_denied"
    if not selected_profile_id:
        return "permission_denied"
    return None
