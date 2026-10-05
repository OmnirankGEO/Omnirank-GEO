"""
审计日志便捷封装
提供简化的审计接口，供各模块调用

使用方式:
    from auth.audit import audit
    audit(request, "create", "brands", entity_id=1, summary="创建品牌 XXX")
"""

from fastapi import Request
from typing import Any, Optional
from db.auth_db import create_audit_log

import logging
logger = logging.getLogger("GEO-Audit")


def audit(
    request: Request = None,
    action: str = "",
    module: str = None,
    entity_type: str = None,
    entity_id: int = None,
    summary: str = "",
    before: Any = None,
    after: Any = None,
    user_id: int = None,
    username: str = None,
):
    """
    便捷审计日志接口
    
    Args:
        request: FastAPI Request（自动从 state 提取用户和 IP）
        action: 操作类型 (create/update/delete/login/reset_password 等)
        module: 模块 ID (brands/writing/social 等)
        entity_type: 实体类型 (brand/article/user 等)
        entity_id: 实体 ID
        summary: 操作摘要
        before: 修改前的数据快照（自动 JSON 序列化，≤4KB）
        after: 修改后的数据快照
        user_id: 用户 ID（如不传 request 可直接指定）
        username: 用户名（如不传 request 可直接指定）
    """
    # 从 request 提取用户信息和 IP
    ip_address = None
    if request:
        user = getattr(request.state, "user", None)
        if user:
            user_id = user_id or user.get("user_id")
            username = username or user.get("username")
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            ip_address = forwarded.split(",")[0].strip()
        elif request.client:
            ip_address = request.client.host

    try:
        create_audit_log(
            user_id=user_id,
            username=username,
            action=action,
            module=module,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            before=before,
            after=after,
            ip_address=ip_address
        )
    except Exception as e:
        # 审计日志不应影响业务逻辑
        logger.error(f"审计日志写入失败: {e}")


def audit_delete(
    request: Request,
    module: str,
    entity_type: str,
    entity_id: int,
    entity_data: Any = None,
    summary: str = ""
):
    """
    删除操作专用审计（自动记录 before 快照）
    """
    audit(
        request=request,
        action="delete",
        module=module,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        before=entity_data
    )
