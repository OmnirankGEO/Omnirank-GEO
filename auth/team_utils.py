"""
团队角色工具函数
从 JWT 的 team_context 中读取团队信息，提供便捷的角色检查
"""

from typing import Optional, Dict
from fastapi import Request, HTTPException


def get_team_context(request: Request) -> Optional[Dict]:
    """从 request.state.user 获取团队上下文"""
    user = getattr(request.state, "user", None)
    if not user:
        return None
    return user.get("team_context")


def require_team_member(request: Request) -> Dict:
    """要求当前用户在团队内，返回 user dict"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    tc = user.get("team_context")
    if not tc or not tc.get("team_id"):
        raise HTTPException(status_code=403, detail="您还未加入团队")
    return user


def require_team_leader(request: Request) -> Dict:
    """要求当前用户是团队长，返回 user dict"""
    user = require_team_member(request)
    tc = user.get("team_context", {})
    if tc.get("team_role") != "leader":
        raise HTTPException(status_code=403, detail="需要团队长权限")
    return user


def is_team_leader(request: Request) -> bool:
    """检查当前用户是否是团队长（不抛异常）"""
    tc = get_team_context(request)
    return bool(tc and tc.get("team_role") == "leader")


def get_team_id(request: Request) -> Optional[int]:
    """获取当前用户的 team_id"""
    tc = get_team_context(request)
    return tc.get("team_id") if tc else None
