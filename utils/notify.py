"""
站内通知工具函数
四级通知：静默 / 轻提示 / 温和 / 重要
标题即完整信息，大部分通知不需要 content 和 link
"""

import json
import logging
from typing import Optional, List

logger = logging.getLogger("GEO-Notify")

# 通知级别常量
LEVEL_SILENT = "silent"        # 只存数据库，前端无感
LEVEL_LIGHT = "light"          # 铃铛+1（灰色数字）
LEVEL_GENTLE = "gentle"        # 铃铛+1 + 底部横幅 3秒
LEVEL_IMPORTANT = "important"  # 铃铛+1 + toast + 抖动


def notify_user(
    user_id: int,
    title: str,
    level: str = LEVEL_LIGHT,
    content: str = "",
    link: str = "",
    type: str = "system",
    suppress_on: Optional[List[str]] = None,
):
    """
    发送站内通知

    Args:
        user_id: 接收用户 ID
        title: 通知标题（一句话说清楚，不需要"查看详情"）
        level: 通知级别 (silent/light/gentle/important)
        content: 补充说明（可选，大部分时候为空）
        link: 关联跳转路径（可选，只在确实需要跳转时填）
        type: 通知类型 (system/publish/diagnosis/wallet/referral/achievement)
        suppress_on: 用户在这些页面时跳过前端提醒（仍存数据库）
    """
    try:
        from db.team_db import create_user_notification
        metadata = {
            "level": level,
        }
        if suppress_on:
            metadata["suppress_on_pages"] = suppress_on

        nid = create_user_notification(
            user_id=user_id,
            type=type,
            title=title,
            content=content,
            link=link,
            level=level,
            metadata=metadata,
        )
        logger.info(f"[Notify] [{level}] user={user_id}: {title}")
        return nid
    except Exception as e:
        logger.warning(f"[Notify] 发送通知失败（不影响业务）: {e}")
        return None


def notify_admins(
    title: str,
    level: str = LEVEL_LIGHT,
    **kwargs,
):
    """给所有管理员发通知"""
    try:
        from db.connection import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT u.id FROM users u
            LEFT JOIN user_roles ur ON u.id = ur.user_id
            LEFT JOIN roles r ON ur.role_id = r.id
            WHERE u.is_admin = TRUE OR r.name = 'admin'
        """)
        admins = cur.fetchall()
        conn.close()

        count = 0
        for a in admins:
            notify_user(a["id"], title, level, **kwargs)
            count += 1
        logger.info(f"[Notify] 管理员通知已发送给 {count} 人: {title}")
        return count
    except Exception as e:
        logger.warning(f"[Notify] 管理员通知发送失败: {e}")
        return 0
