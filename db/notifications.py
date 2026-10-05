"""
通知中心数据库操作模块 v2.0 (brand_id 统一化)
功能：
- 创建通知
- 获取通知列表
- 标记已读
- 获取未读数量

✅ v2.0: 参数统一使用 brand_id: int 作为业务主键。
"""

from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# 数据库路径（保留用于 init 函数兼容）
DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "geo_diagnosis.db"


def get_connection():
    """获取数据库连接"""
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


# ==========================================
# 通知操作
# ==========================================

def create_notification(
    brand_id: int,
    type: str,
    title: str,
    content: str = "",
    related_id: int = None
) -> int:
    """
    创建通知

    Args:
        brand_id: 品牌ID (brands.id)
        type: 通知类型 ('report', 'alert', 'system')
        title: 通知标题
        content: 通知内容
        related_id: 关联ID (如报告ID)

    Returns:
        新建通知的ID
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO notifications (client_id, brand_id, type, title, content, related_id, is_read, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s)
            RETURNING id
        """, (brand_id, brand_id, type, title, content, related_id, datetime.now().isoformat()))

        notification_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()

        print(f"[Notifications] 创建通知: {title} (ID: {notification_id})")
        return notification_id
    finally:
        try:
            conn.close()
        except Exception: pass


def get_notifications(
    brand_id: Optional[int] = None,
    limit: int = 50,
    unread_only: bool = False
) -> List[Dict[str, Any]]:
    """
    获取通知列表

    Args:
        brand_id: 品牌ID，None表示获取所有品牌的通知（管理员模式）
        limit: 最大返回数量
        unread_only: 是否只返回未读

    Returns:
        通知列表
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if brand_id is not None:
            query = "SELECT * FROM notifications WHERE brand_id = %s"
            params: list = [brand_id]
        else:
            query = "SELECT * FROM notifications WHERE 1=1"
            params: list = []

        if unread_only:
            query += " AND is_read = 0"

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def mark_as_read(notification_id: int) -> bool:
    """
    标记通知为已读

    Args:
        notification_id: 通知ID

    Returns:
        是否成功
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE notifications SET is_read = 1 WHERE id = %s
        """, (notification_id,))

        affected = cursor.rowcount
        conn.commit()
        conn.close()

        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def mark_all_as_read(brand_id: Optional[int] = None) -> int:
    """
    标记客户所有通知为已读

    Args:
        brand_id: 品牌ID，None表示标记所有通知已读（管理员模式）

    Returns:
        更新的通知数量
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if brand_id is not None:
            cursor.execute("""
                UPDATE notifications SET is_read = 1 WHERE brand_id = %s AND is_read = 0
            """, (brand_id,))
        else:
            cursor.execute("""
                UPDATE notifications SET is_read = 1 WHERE is_read = 0
            """)

        affected = cursor.rowcount
        conn.commit()
        conn.close()

        return affected
    finally:
        try:
            conn.close()
        except Exception: pass


def get_unread_count(brand_id: Optional[int] = None) -> int:
    """
    获取未读通知数量

    Args:
        brand_id: 品牌ID，None表示获取所有品牌的未读数量（管理员模式）

    Returns:
        未读数量
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if brand_id is not None:
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM notifications WHERE brand_id = %s AND is_read = 0
            """, (brand_id,))
        else:
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM notifications WHERE is_read = 0
            """)

        count = cursor.fetchone()["cnt"]
        conn.close()

        return count
    finally:
        try:
            conn.close()
        except Exception: pass


def delete_notification(notification_id: int) -> bool:
    """
    删除通知

    Args:
        notification_id: 通知ID

    Returns:
        是否成功
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("DELETE FROM notifications WHERE id = %s", (notification_id,))

        affected = cursor.rowcount
        conn.commit()
        conn.close()

        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def get_notification_by_id(notification_id: int) -> Optional[Dict[str, Any]]:
    """
    根据ID获取通知详情

    Args:
        notification_id: 通知ID

    Returns:
        通知详情或None
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM notifications WHERE id = %s", (notification_id,))
        row = cursor.fetchone()
        conn.close()

        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass
