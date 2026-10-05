"""
Dashboard增强统计模块
提供首页看板所需的各类统计数据
"""

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

from .diagnosis_db import get_connection


def get_social_stats() -> Dict[str, int]:
    """获取社媒操盘手统计"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 本月起始时间
        now = datetime.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
        stats = {
            "topics_count": 0,
            "scripts_count": 0,
            "pending_count": 0,
            "published_count": 0,
        }
    
        try:
            # 本月选题数（已确认）
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM social_topics
                WHERE status = 'confirmed' AND created_at >= %s
            """, (month_start.isoformat(),))
            row = cursor.fetchone()
            stats["topics_count"] = row["cnt"] if row else 0
        
            # 本月文案数
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM social_scripts
                WHERE created_at >= %s
            """, (month_start.isoformat(),))
            row = cursor.fetchone()
            stats["scripts_count"] = row["cnt"] if row else 0
        
            # 待发布
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM social_scripts
                WHERE status = 'pending'
            """)
            row = cursor.fetchone()
            stats["pending_count"] = row["cnt"] if row else 0
        
            # 已发布
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM social_scripts
                WHERE status = 'published'
            """)
            row = cursor.fetchone()
            stats["published_count"] = row["cnt"] if row else 0
        
        except Exception as e:
            print(f"[Dashboard] 社媒统计查询失败: {e}")
    
        conn.close()
        return stats
    finally:
        try:
            conn.close()
        except Exception: pass


def get_ai_collaboration_stats() -> Dict[str, int]:
    """获取AI协作统计"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        now = datetime.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
        stats = {
            "meetings_count": 0,
            "tasks_count": 0,
            "active_advisors": 0,
        }
    
        try:
            # 本月会议数
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM employee_meetings
                WHERE status = 'completed' AND created_at >= %s
            """, (month_start.isoformat(),))
            row = cursor.fetchone()
            stats["meetings_count"] = row["cnt"] if row else 0
        
            # 本月任务完成数
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM employee_tasks
                WHERE status = 'completed' AND created_at >= %s
            """, (month_start.isoformat(),))
            row = cursor.fetchone()
            stats["tasks_count"] = row["cnt"] if row else 0
        
            # 活跃顾问数
            cursor.execute("""
                SELECT COUNT(*) AS cnt FROM advisors
                WHERE is_active = 1
            """)
            row = cursor.fetchone()
            stats["active_advisors"] = row["cnt"] if row else 0
        
        except Exception as e:
            print(f"[Dashboard] AI协作统计查询失败: {e}")
    
        conn.close()
        return stats
    finally:
        try:
            conn.close()
        except Exception: pass


def get_knowledge_stats() -> Dict[str, int]:
    """获取知识库统计"""
    base_dir = Path(__file__).parent.parent / "data" / "knowledge"
    
    stats = {
        "client_docs": 0,
        "role_docs": 0,
    }

    try:
        # 客户知识库
        clients_path = base_dir / "clients"
        if clients_path.exists():
            stats["client_docs"] = sum(1 for _ in clients_path.rglob("*") if _.is_file())

        # 顾问知识库
        roles_path = base_dir / "roles"
        if roles_path.exists():
            stats["role_docs"] = sum(1 for _ in roles_path.rglob("*") if _.is_file())

    except Exception as e:
        print(f"[Dashboard] 知识库统计失败: {e}")

    return stats


def get_recent_activities(limit: int = 10) -> List[Dict[str, Any]]:
    """获取最近活动时间线"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        activities = []
    
        try:
            # 最近诊断
            cursor.execute("""
                SELECT id, brand_name as title, created_at 
                FROM diagnosis_records 
                ORDER BY created_at DESC LIMIT 3
            """)
            for row in cursor.fetchall():
                activities.append({
                    "type": "diagnosis",
                    "icon": "🔍",
                    "title": f"诊断了 {row['title']}",
                    "time": row["created_at"],
                    "id": row["id"],
                    "link": f"/diagnosis/report/{row['id']}",
                })
        
            # 最近文章
            cursor.execute("""
                SELECT id, title, created_at 
                FROM article_generations 
                ORDER BY created_at DESC LIMIT 3
            """)
            for row in cursor.fetchall():
                title = row["title"]
                activities.append({
                    "type": "article",
                    "icon": "📝",
                    "title": f"生成了文章《{title[:20]}...》" if len(title) > 20 else f"生成了文章《{title}》",
                    "time": row["created_at"],
                    "id": row["id"],
                    "link": f"/articles",  # 文章列表页
                })
        
            # 最近会议
            cursor.execute("""
                SELECT id, topic as title, completed_at 
                FROM employee_meetings 
                WHERE status = 'completed'
                ORDER BY completed_at DESC LIMIT 3
            """)
            for row in cursor.fetchall():
                if row["completed_at"]:  # completed_at 可能为空
                    title = row["title"]
                    activities.append({
                        "type": "meeting",
                        "icon": "🤝",
                        "title": f"完成会议《{title[:15]}...》" if len(title) > 15 else f"完成会议《{title}》",
                        "time": row["completed_at"],
                        "id": row["id"],
                        "link": f"/employees/meeting/{row['id']}",
                    })
        
            # 最近选题
            cursor.execute("""
                SELECT id, title, confirmed_at 
                FROM social_topics 
                WHERE status = 'confirmed' AND confirmed_at IS NOT NULL
                ORDER BY confirmed_at DESC LIMIT 3
            """)
            for row in cursor.fetchall():
                title = row["title"]
                activities.append({
                    "type": "topic",
                    "icon": "💡",
                    "title": f"确认选题《{title[:15]}...》" if len(title) > 15 else f"确认选题《{title}》",
                    "time": row["confirmed_at"],
                    "id": row["id"],
                    "link": f"/social/topics",  # 选题列表页
                })
        
        except Exception as e:
            print(f"[Dashboard] 活动时间线查询失败: {e}")
    
        conn.close()
    
        # 按时间排序
        def parse_time(t):
            if not t:
                return datetime.min
            try:
                return datetime.fromisoformat(t.replace('Z', '+00:00').replace(' ', 'T'))
            except:
                return datetime.min
    
        activities.sort(key=lambda x: parse_time(x.get("time", "")), reverse=True)
    
        # 转换为相对时间
        now = datetime.now()
        for activity in activities[:limit]:
            try:
                t = parse_time(activity.get("time", ""))
                if t == datetime.min:
                    activity["relative_time"] = "未知"
                else:
                    delta = now - t
                    if delta.days > 0:
                        activity["relative_time"] = f"{delta.days}天前"
                    elif delta.seconds >= 3600:
                        activity["relative_time"] = f"{delta.seconds // 3600}小时前"
                    elif delta.seconds >= 60:
                        activity["relative_time"] = f"{delta.seconds // 60}分钟前"
                    else:
                        activity["relative_time"] = "刚刚"
            except:
                activity["relative_time"] = "未知"
    
        return activities[:limit]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_enhanced_dashboard_stats() -> Dict[str, Any]:
    """获取增强版Dashboard统计数据"""
    return {
        "social_stats": get_social_stats(),
        "ai_stats": get_ai_collaboration_stats(),
        "knowledge_stats": get_knowledge_stats(),
        "recent_activities": get_recent_activities(8),
    }
