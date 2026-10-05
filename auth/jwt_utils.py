"""
JWT 工具模块
签发、验证、刷新 JWT Token
"""

import time
import json
import hmac
import hashlib
import base64
from typing import Optional, Dict, List
from db.auth_db import get_user, get_user_permissions, get_user_by_username, verify_password, update_last_login

# JWT 密钥（必须通过环境变量设置，否则自动生成随机密钥）
import os
import secrets
import logging as _logging

_jwt_logger = _logging.getLogger("GEO-JWT")

# JWT 密钥：强烈建议通过环境变量 JWT_SECRET 设置
# 文件兜底仅用于开发环境，生产环境必须设置环境变量
_env_secret = os.environ.get("JWT_SECRET")
if _env_secret:
    JWT_SECRET = _env_secret
    _jwt_logger.info("✅ JWT_SECRET 从环境变量加载")
else:
    _jwt_logger.warning("⚠️ JWT_SECRET 环境变量未设置！生产环境请务必配置。回退到文件存储。")
    _secret_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "jwt_secret.key")
    try:
        os.makedirs(os.path.dirname(_secret_file), exist_ok=True)
        if os.path.exists(_secret_file):
            with open(_secret_file, "r") as f:
                JWT_SECRET = f.read().strip()
            if not JWT_SECRET:
                raise ValueError("密钥文件为空")
        else:
            JWT_SECRET = secrets.token_hex(32)
            with open(_secret_file, "w") as f:
                f.write(JWT_SECRET)
            try:
                import stat
                os.chmod(_secret_file, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                _jwt_logger.warning("⚠️ 无法设置密钥文件权限（Windows 环境），请确保文件访问受限")
    except Exception as e:
        JWT_SECRET = secrets.token_hex(32)
        _jwt_logger.warning(f"⚠️ JWT_SECRET 文件操作失败({e})，使用临时密钥（重启后旧 Token 将失效）")

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 天（从 30 天缩短，降低 Token 被盗风险）


def _base64url_encode(data: bytes) -> str:
    """Base64url 编码（无填充）"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _base64url_decode(s: str) -> bytes:
    """Base64url 解码"""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _sign(header_payload: str) -> str:
    """HMAC-SHA256 签名"""
    signature = hmac.new(
        JWT_SECRET.encode("utf-8"),
        header_payload.encode("utf-8"),
        hashlib.sha256
    ).digest()
    return _base64url_encode(signature)


def create_jwt(user_id: int) -> Optional[str]:
    """
    为用户创建 JWT Token
    
    Payload 结构:
    {
        "user_id": 1,
        "username": "admin",
        "display_name": "管理员",
        "is_admin": true,
        "roles": [{"id": 1, "name": "admin", "display_name": "超级管理员"}],
        "permissions": ["writing:read", "writing:write", ...],
        "perm_version": 1,
        "iat": 1234567890,
        "exp": 1234567890
    }
    """
    user = get_user(user_id)
    if not user:
        return None

    # 禁用账户不签发新 token（防止 refresh 绕过禁用）
    if not user.get("is_active", True):
        return None

    now = int(time.time())
    payload = {
        "user_id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "is_admin": user["is_admin"],
        "roles": user["roles"],
        "permissions": user["permissions"],
        "client_brand_ids": user.get("client_brand_ids", []),
        "perm_version": user["permission_version"],
        "must_change_password": user.get("must_change_password", 0),
        "iat": now,
        "exp": now + JWT_EXPIRE_SECONDS,
    }

    # 获取团队上下文
    try:
        from db.team_db import get_user_team
        team = get_user_team(user["id"])
        if team:
            payload["team_context"] = {
                "team_id": team["id"],
                "team_role": team["my_role"],
                "brand_id": team.get("brand_id"),
            }
        else:
            payload["team_context"] = None
    except Exception:
        payload["team_context"] = None

    # 构建 JWT: header.payload.signature
    header = _base64url_encode(json.dumps(
        {"alg": JWT_ALGORITHM, "typ": "JWT"}, separators=(",", ":")
    ).encode("utf-8"))

    payload_b64 = _base64url_encode(json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8"))

    header_payload = f"{header}.{payload_b64}"
    signature = _sign(header_payload)

    return f"{header_payload}.{signature}"


def decode_jwt(token: str) -> Optional[Dict]:
    """
    解码并验证 JWT Token
    返回 payload 字典，失败返回 None
    """
    if not token:
        return None

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_payload = f"{parts[0]}.{parts[1]}"
        expected_signature = _sign(header_payload)

        # 验证签名
        if not hmac.compare_digest(parts[2], expected_signature):
            return None

        # 解码 payload
        payload_json = _base64url_decode(parts[1])
        payload = json.loads(payload_json)

        # 验证过期时间
        if payload.get("exp", 0) < int(time.time()):
            return None

        return payload
    except Exception:
        return None


def authenticate_user(username: str, password: str) -> Dict:
    """
    验证用户名/密码，成功返回 JWT + 用户信息
    
    Returns:
        {
            "success": True/False,
            "token": "jwt_string",
            "user": {...},
            "error": "error_message"
        }
    """
    # 获取用户（含密码哈希）
    user_raw = get_user_by_username(username)
    if not user_raw:
        return {"success": False, "error": "用户名或密码错误"}

    if not user_raw["is_active"]:
        return {"success": False, "error": "账户已被禁用"}

    # 验证密码
    if not verify_password(password, user_raw["password_hash"]):
        return {"success": False, "error": "用户名或密码错误"}

    # 签发 JWT
    token = create_jwt(user_raw["id"])
    if not token:
        return {"success": False, "error": "Token 生成失败"}

    # 更新最后登录时间
    update_last_login(user_raw["id"])

    # 获取完整用户信息（不含密码哈希）
    user_info = get_user(user_raw["id"])

    return {
        "success": True,
        "token": token,
        "user": user_info
    }


def refresh_jwt(user_id: int) -> Optional[str]:
    """刷新 JWT（重新读取权限，签发新 Token）"""
    return create_jwt(user_id)
