"""
团队管理数据库模块
Phase 0: 团队隔离体系 - teams, team_members, user_notifications 表操作
"""

import random
import string
import logging
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger("GEO-TeamDB")


def get_connection():
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


def init_team_tables():
    """初始化团队相关表（幂等）"""
    import os
    conn = get_connection()
    try:
        conn.autocommit = True
        cursor = conn.cursor()
        # migration_001_teams.sql
        sql_path = os.path.join(os.path.dirname(__file__), "migration_001_teams.sql")
        with open(sql_path, "r", encoding="utf-8") as f:
            cursor.execute(f.read())
        # migration_005_activity.sql (活动追踪)
        activity_path = os.path.join(os.path.dirname(__file__), "migration_005_activity.sql")
        if os.path.exists(activity_path):
            with open(activity_path, "r", encoding="utf-8") as f:
                cursor.execute(f.read())
        # migration_007: 通知分级 level + metadata
        m007_path = os.path.join(os.path.dirname(__file__), "migration_007_notification_level.sql")
        if os.path.exists(m007_path):
            with open(m007_path, "r", encoding="utf-8") as f:
                cursor.execute(f.read())
        conn.close()
        logger.info("✅ 团队表初始化完成")
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 团队码生成
# ==========================================

def generate_team_code(region_prefix: str = "") -> str:
    """生成6位团队码，如 HD-A3X9"""
    chars = string.ascii_uppercase + string.digits
    suffix = ''.join(random.choices(chars, k=4))
    code = f"{region_prefix}-{suffix}" if region_prefix else suffix
    # 确保唯一
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM teams WHERE team_code = %s", (code,))
        if cursor.fetchone():
            conn.close()
            return generate_team_code(region_prefix)  # 冲突重试
        conn.close()
        return code
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 团队 CRUD
# ==========================================

def create_team(
    team_name: str,
    brand_id: Optional[int],
    leader_id: int,
    created_by: int,
    region: str = "",
    description: str = "",
    max_members: int = 50,
) -> Dict[str, Any]:
    """创建团队，自动生成团队码"""
    # 生成区域前缀
    prefix = ""
    if region:
        region_map = {
            "华东": "HD", "华南": "HN", "华北": "HB", "华中": "HZ",
            "西南": "XN", "西北": "XB", "东北": "DB",
        }
        prefix = region_map.get(region, region[:2].upper())

    team_code = generate_team_code(prefix)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO teams (team_name, team_code, region, description, leader_id, brand_id, max_members, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (team_name, team_code, region, description, leader_id, brand_id, max_members, created_by))
        team = dict(cursor.fetchone())

        # 自动将队长加入成员
        cursor.execute("""
            INSERT INTO team_members (team_id, user_id, role, status)
            VALUES (%s, %s, 'leader', 'active')
            ON CONFLICT (team_id, user_id) DO NOTHING
        """, (team["id"], leader_id))

        conn.commit()
        conn.close()
        logger.info(f"创建团队: {team_name} (code={team_code})")
        return team
    finally:
        try:
            conn.close()
        except Exception: pass


def get_team(team_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.*, b.name as brand_name, u.display_name as leader_name
            FROM teams t
            LEFT JOIN brands b ON t.brand_id = b.id
            LEFT JOIN users u ON t.leader_id = u.id
            WHERE t.id = %s
        """, (team_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_team_by_code(team_code: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM teams WHERE team_code = %s", (team_code,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def list_teams(brand_id: Optional[int] = None, status: str = "active") -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        query = "SELECT t.*, u.display_name as leader_name, b.name as brand_name FROM teams t LEFT JOIN users u ON t.leader_id = u.id LEFT JOIN brands b ON t.brand_id = b.id WHERE 1=1"
        params: list = []
        if brand_id is not None:
            query += " AND t.brand_id = %s"
            params.append(brand_id)
        if status:
            query += " AND t.status = %s"
            params.append(status)
        query += " ORDER BY t.created_at DESC"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def update_team(team_id: int, **kwargs) -> Optional[Dict[str, Any]]:
    allowed = {"team_name", "region", "description", "leader_id", "brand_id", "max_members", "status"}
    fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not fields:
        return get_team(team_id)

    sets = ", ".join(f"{k} = %s" for k in fields)
    vals = list(fields.values())
    vals.append(datetime.now())
    vals.append(team_id)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE teams SET {sets}, updated_at = %s WHERE id = %s RETURNING *", vals)
        row = cursor.fetchone()
        conn.commit()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def dissolve_team(team_id: int) -> bool:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE teams SET status = 'dissolved', dissolved_at = NOW(), updated_at = NOW()
            WHERE id = %s AND status = 'active'
        """, (team_id,))
        affected = cursor.rowcount
        # 移除所有成员
        if affected:
            cursor.execute("""
                UPDATE team_members SET status = 'removed' WHERE team_id = %s AND status = 'active'
            """, (team_id,))
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 团队成员
# ==========================================

def join_team(team_code: str, user_id: int, profile_id: Optional[str] = None) -> Dict[str, Any]:
    """
    通过团队码加入团队
    Returns: {"success": bool, "message": str, "team": dict|None}
    """
    team = get_team_by_code(team_code.strip().upper())
    if not team:
        return {"success": False, "message": "团队码无效", "team": None}
    if team["status"] != "active":
        return {"success": False, "message": "该团队已冻结或解散", "team": None}

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 多团队支持：不再限制同品牌只能加一个团队

        # 检查人数上限
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM team_members WHERE team_id = %s AND status = 'active'",
            (team["id"],)
        )
        count = cursor.fetchone()["cnt"]
        if count >= team["max_members"]:
            conn.close()
            return {"success": False, "message": "该团队已满员", "team": None}

        # 加入
        try:
            cursor.execute("""
                INSERT INTO team_members (team_id, user_id, profile_id, role, status)
                VALUES (%s, %s, %s, 'member', 'active')
            """, (team["id"], user_id, profile_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                return {"success": False, "message": "你已是该团队成员", "team": None}
            raise

        # 发通知给队长
        try:
            cursor.execute("SELECT display_name FROM users WHERE id = %s", (user_id,))
            user_row = cursor.fetchone()
            user_name = user_row["display_name"] if user_row else str(user_id)
            cursor.execute("""
                INSERT INTO user_notifications (user_id, type, title, content, link)
                VALUES (%s, 'team_join', %s, %s, %s)
            """, (
                team["leader_id"],
                f"{user_name} 加入了团队",
                f"{user_name} 通过团队码加入了「{team['team_name']}」",
                "/social/team"
            ))
            conn.commit()
        except Exception:
            pass  # 通知失败不影响主流程

        conn.close()
        logger.info(f"用户 {user_id} 加入团队 {team['team_name']}")
        return {"success": True, "message": "加入成功", "team": team}
    finally:
        try:
            conn.close()
        except Exception: pass


def get_team_members(team_id: int, status: str = "active") -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT tm.*, u.display_name, u.username
            FROM team_members tm
            JOIN users u ON tm.user_id = u.id
            WHERE tm.team_id = %s AND tm.status = %s
            ORDER BY tm.role = 'leader' DESC, tm.joined_at ASC
        """, (team_id, status))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def remove_member(team_id: int, user_id: int) -> bool:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 不能移除队长
        cursor.execute(
            "SELECT role FROM team_members WHERE team_id = %s AND user_id = %s AND status = 'active'",
            (team_id, user_id)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        if row["role"] == "leader":
            conn.close()
            raise ValueError("无法移除队长，请先转移队长角色")

        cursor.execute("""
            UPDATE team_members SET status = 'removed' WHERE team_id = %s AND user_id = %s
        """, (team_id, user_id))
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def transfer_member(from_team_id: int, to_team_id: int, user_id: int) -> Dict[str, Any]:
    """调拨成员到另一个团队"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 检查来源
        cursor.execute(
            "SELECT role FROM team_members WHERE team_id = %s AND user_id = %s AND status = 'active'",
            (from_team_id, user_id)
        )
        src = cursor.fetchone()
        if not src:
            conn.close()
            return {"success": False, "message": "该用户不在原团队中"}
        if src["role"] == "leader":
            conn.close()
            return {"success": False, "message": "队长不能被调拨，请先转移队长角色"}

        # 检查目标团队
        cursor.execute("SELECT * FROM teams WHERE id = %s AND status = 'active'", (to_team_id,))
        target = cursor.fetchone()
        if not target:
            conn.close()
            return {"success": False, "message": "目标团队不存在或已解散"}

        # 检查目标团队人数
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM team_members WHERE team_id = %s AND status = 'active'",
            (to_team_id,)
        )
        if cursor.fetchone()["cnt"] >= target["max_members"]:
            conn.close()
            return {"success": False, "message": "目标团队已满员"}

        # 执行调拨
        cursor.execute(
            "UPDATE team_members SET status = 'removed' WHERE team_id = %s AND user_id = %s",
            (from_team_id, user_id)
        )
        cursor.execute("""
            INSERT INTO team_members (team_id, user_id, role, status)
            VALUES (%s, %s, 'member', 'active')
            ON CONFLICT (team_id, user_id) DO UPDATE SET status = 'active', role = 'member', joined_at = NOW()
        """, (to_team_id, user_id))
        conn.commit()
        conn.close()
        return {"success": True, "message": "调拨成功"}
    finally:
        try:
            conn.close()
        except Exception: pass


def transfer_leader(team_id: int, new_leader_id: int) -> bool:
    """转移队长角色"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 验证新队长是团队成员
        cursor.execute(
            "SELECT 1 FROM team_members WHERE team_id = %s AND user_id = %s AND status = 'active'",
            (team_id, new_leader_id)
        )
        if not cursor.fetchone():
            conn.close()
            return False
        # 旧队长降为成员
        cursor.execute(
            "UPDATE team_members SET role = 'member' WHERE team_id = %s AND role = 'leader'",
            (team_id,)
        )
        # 新队长
        cursor.execute(
            "UPDATE team_members SET role = 'leader' WHERE team_id = %s AND user_id = %s",
            (team_id, new_leader_id)
        )
        cursor.execute(
            "UPDATE teams SET leader_id = %s, updated_at = NOW() WHERE id = %s",
            (new_leader_id, team_id)
        )
        conn.commit()
        conn.close()
        return True
    finally:
        try:
            conn.close()
        except Exception: pass


def get_user_team(user_id: int, brand_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """获取用户所属团队"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        query = """
            SELECT t.*, tm.role as my_role, tm.joined_at as my_joined_at
            FROM team_members tm
            JOIN teams t ON tm.team_id = t.id
            WHERE tm.user_id = %s AND tm.status = 'active' AND t.status = 'active'
        """
        params: list = [user_id]
        if brand_id is not None:
            query += " AND t.brand_id = %s"
            params.append(brand_id)
        query += " LIMIT 1"
        cursor.execute(query, params)
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception: pass


def get_user_team_id(user_id: int) -> Optional[int]:
    """从 team_members 获取当前用户的 team_id（用于数据隔离）"""
    team = get_user_team(user_id)
    return team["id"] if team else None


# ==========================================
# 用户通知 (user_notifications)
# ==========================================

def create_user_notification(
    user_id: int,
    type: str,
    title: str,
    content: str = "",
    link: str = "",
    level: str = "light",
    metadata: dict = None,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        cursor.execute("""
            INSERT INTO user_notifications (user_id, type, title, content, link, level, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (user_id, type, title, content, link, level, meta_json))
        nid = cursor.fetchone()["id"]
        conn.commit()
        conn.close()
        return nid
    finally:
        try:
            conn.close()
        except Exception: pass


def get_user_notifications(user_id: int, limit: int = 50, unread_only: bool = False) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # Public DTO allowlist: event_key is an internal idempotency key and must
        # never become a browser-visible business identifier.
        query = (
            "SELECT id, user_id, type, title, content, link, is_read, "
            "created_at, level, metadata FROM user_notifications WHERE user_id = %s"
        )
        params: list = [user_id]
        if unread_only:
            query += " AND is_read = FALSE"
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def mark_user_notification_read(notification_id: int, user_id: int) -> bool:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_notifications SET is_read = TRUE WHERE id = %s AND user_id = %s",
            (notification_id, user_id)
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def mark_all_user_notifications_read(user_id: int) -> int:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_notifications SET is_read = TRUE WHERE user_id = %s AND is_read = FALSE",
            (user_id,)
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected
    finally:
        try:
            conn.close()
        except Exception: pass


def get_user_unread_count(user_id: int) -> int:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM user_notifications WHERE user_id = %s AND is_read = FALSE",
            (user_id,)
        )
        count = cursor.fetchone()["cnt"]
        conn.close()
        return count
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# [工单 2026-07-29 T3] 通知中心 · 两类分组 + 历史分页 + 同业务单号终态去重
#
# 分两条通道:
#   system  —— user_notifications 里非资金类事件(产品/公告/业务状态)
#   billing —— **point_transactions 的只读投影**(扣费/退费/冻结释放)
#              🔴 只读:不 INSERT/UPDATE 任何资金表,不参与金额计算,
#                 已读状态落在独立的 user_notification_read_marks 水位表。
#
# 终态去重(§3.4):同一业务单号(如 MONITOR-1507)的互斥终态只留最后一条。
#   分组白名单在 services.notification_events.TERMINAL_SUPERSEDE_GROUPS —— 退款 /
#   待人工两类**故意不在组里**,永不被覆盖。
# ==========================================

NOTIFICATION_CHANNEL_SYSTEM = "system"
NOTIFICATION_CHANNEL_BILLING = "billing"

# 扣费通知取哪几种流水(工单 §3.1:每笔扣费 / 退费 / 冻结释放)
BILLING_FEED_TX_TYPES = ("consume", "refund", "release")

_BILLING_TX_PRESENTATION = {
    "consume": ("已扣费", "gentle"),
    "refund": ("费用已退回", "important"),
    "release": ("冻结算力已释放", "light"),
}


def _terminal_supersede_cte() -> tuple:
    """构造「同业务单号终态去重」的 CTE 与参数。

    event_key 形如 `{event_type}:{business_id}:{terminal_state}:{user_id}`,
    其中 event_type 自带一个点(如 `monitoring.completed`)。这里按
    (分组名, business_id) 聚合,只保留 (created_at, id) 最大的那条。

    ⚠️ 不用 `split_part(event_type,'.',1)` 猜分组 —— 那会把 `publication.refunded`
    和 `publication.completed` 归到一组,导致退款通知被后到的终态盖掉。分组一律
    走 TERMINAL_SUPERSEDE_GROUPS 显式白名单。
    """
    from services.notification_events import terminal_supersede_pairs

    pairs = terminal_supersede_pairs()
    event_types = [item[0] for item in pairs]
    group_names = [item[1] for item in pairs]
    cte = """
        terminal_map(event_type, group_name) AS (
            SELECT * FROM unnest(%s::text[], %s::text[])
        ),
        scoped AS (
            SELECT n.id, n.created_at, tm.group_name,
                   split_part(n.event_key, ':', 2) AS business_id
            FROM user_notifications n
            JOIN terminal_map tm ON tm.event_type = split_part(n.event_key, ':', 1)
            WHERE n.user_id = %s AND n.event_key IS NOT NULL
        ),
        superseded AS (
            SELECT s.id FROM scoped s
            WHERE EXISTS (
                SELECT 1 FROM scoped s2
                WHERE s2.group_name = s.group_name
                  AND s2.business_id = s.business_id
                  AND (s2.created_at, s2.id) > (s.created_at, s.id)
            )
        )
    """
    return cte, [event_types, group_names]


def _system_notification_rows(
    cursor, user_id: int, *, limit: int, offset: int, unread_only: bool,
) -> List[Dict[str, Any]]:
    cte, cte_params = _terminal_supersede_cte()
    where = ["n.user_id = %s", "n.id NOT IN (SELECT id FROM superseded)"]
    params = list(cte_params) + [user_id, user_id]
    if unread_only:
        where.append("n.is_read = FALSE")
    cursor.execute(
        f"""
        WITH {cte}
        SELECT n.id, n.type, n.title, n.content, n.link, n.is_read,
               n.created_at, n.level, n.metadata,
               split_part(n.event_key, ':', 1) AS event_type,
               split_part(n.event_key, ':', 2) AS business_id
        FROM user_notifications n
        WHERE {' AND '.join(where)}
        ORDER BY n.created_at DESC, n.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params + [limit, offset]),
    )
    return [dict(r) for r in cursor.fetchall()]


def _system_notification_count(cursor, user_id: int, *, unread_only: bool) -> int:
    cte, cte_params = _terminal_supersede_cte()
    where = ["n.user_id = %s", "n.id NOT IN (SELECT id FROM superseded)"]
    params = list(cte_params) + [user_id, user_id]
    if unread_only:
        where.append("n.is_read = FALSE")
    cursor.execute(
        f"""
        WITH {cte}
        SELECT COUNT(*) AS cnt FROM user_notifications n
        WHERE {' AND '.join(where)}
        """,
        tuple(params),
    )
    return int((cursor.fetchone() or {}).get("cnt") or 0)


def get_billing_read_mark(cursor, user_id: int) -> int:
    """扣费通知已读水位。查不到 / 表还没建 → 返 0(= 全部未读),不抛。"""
    try:
        cursor.execute(
            "SELECT last_read_ref FROM user_notification_read_marks "
            "WHERE user_id = %s AND channel = %s",
            (user_id, NOTIFICATION_CHANNEL_BILLING),
        )
        row = cursor.fetchone()
        return int((row or {}).get("last_read_ref") or 0)
    except Exception:
        return 0


def _billing_rows(
    cursor, user_id: int, *, limit: int, offset: int, unread_only: bool, watermark: int,
) -> List[Dict[str, Any]]:
    where = ["pt.user_id = %s", "pt.type = ANY(%s)"]
    params: list = [user_id, list(BILLING_FEED_TX_TYPES)]
    if unread_only:
        where.append("pt.id > %s")
        params.append(watermark)
    cursor.execute(
        f"""
        SELECT pt.id, pt.type, pt.point_type, pt.amount, pt.balance_after,
               pt.feature_code, pt.description, pt.created_at,
               b.name AS brand_name
        FROM point_transactions pt
        LEFT JOIN brands b ON pt.brand_id = b.id
        WHERE {' AND '.join(where)}
        ORDER BY pt.created_at DESC, pt.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params + [limit, offset]),
    )
    return [dict(r) for r in cursor.fetchall()]


def _billing_count(cursor, user_id: int, *, unread_only: bool, watermark: int) -> int:
    where = ["pt.user_id = %s", "pt.type = ANY(%s)"]
    params: list = [user_id, list(BILLING_FEED_TX_TYPES)]
    if unread_only:
        where.append("pt.id > %s")
        params.append(watermark)
    cursor.execute(
        f"SELECT COUNT(*) AS cnt FROM point_transactions pt WHERE {' AND '.join(where)}",
        tuple(params),
    )
    return int((cursor.fetchone() or {}).get("cnt") or 0)


def _billing_item(row: Dict[str, Any], watermark: int) -> Dict[str, Any]:
    """流水行 → 通知条目。**纯展示映射,不做任何金额计算**(工单 §3.1)。"""
    tx_type = str(row.get("type") or "")
    title, level = _BILLING_TX_PRESENTATION.get(tx_type, ("账户变动", "light"))
    amount = int(row.get("amount") or 0)
    brand = row.get("brand_name")
    parts = [f"业务单号 BILL-{row['id']}"]
    parts.append(f"算力变动 {amount:+d}")
    if row.get("balance_after") is not None:
        parts.append(f"变动后余额 {int(row['balance_after'])}")
    if brand:
        parts.append(f"关联客户 {brand}")
    if row.get("description"):
        parts.append(str(row["description"]))
    return {
        "id": f"bill:{row['id']}",
        "raw_id": int(row["id"]),
        "category": NOTIFICATION_CHANNEL_BILLING,
        "type": tx_type,
        "title": title,
        # 正文完整落库形态,前端弹窗逐字展示(工单 §3.5 锁 1)
        "content": "；".join(parts) + "。",
        "link": "/wallet",
        "level": level,
        "is_read": int(row["id"]) <= watermark,
        "created_at": row.get("created_at"),
        "business_no": f"BILL-{row['id']}",
    }


def _system_item(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"sys:{row['id']}",
        "raw_id": int(row["id"]),
        "category": NOTIFICATION_CHANNEL_SYSTEM,
        "type": row.get("type"),
        "title": row.get("title"),
        "content": row.get("content") or "",
        "link": row.get("link") or "",
        "level": row.get("level") or "light",
        "is_read": bool(row.get("is_read")),
        "created_at": row.get("created_at"),
        "business_no": (str(row.get("business_id")) or "") if row.get("business_id") else "",
        "metadata": row.get("metadata"),
    }


def get_notification_feed(
    user_id: int, *, category: str = "all", limit: int = 20, offset: int = 0,
    unread_only: bool = False,
) -> Dict[str, Any]:
    """通知中心统一 feed(历史页翻页也走这里)。

    category ∈ {all, system, billing}。all 时两路各取 limit+offset 条再合并排序 ——
    正确性优先于一次查询:两路是不同表,不做跨表 UNION 以免把资金表拖进复杂计划。
    """
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    conn = get_connection()
    try:
        cursor = conn.cursor()
        watermark = get_billing_read_mark(cursor, user_id)
        items: List[Dict[str, Any]] = []
        totals = {"system": 0, "billing": 0}
        want_system = category in ("all", NOTIFICATION_CHANNEL_SYSTEM)
        want_billing = category in ("all", NOTIFICATION_CHANNEL_BILLING)
        # 分通道请求时窗口就是 (offset, limit);合并请求时两路都取到 offset+limit
        # 再统一排序截断,保证合并后的第 N 页与单路口径一致。
        window = limit if category != "all" else offset + limit
        window_offset = offset if category != "all" else 0

        if want_system:
            totals["system"] = _system_notification_count(cursor, user_id, unread_only=unread_only)
            items.extend(
                _system_item(row) for row in _system_notification_rows(
                    cursor, user_id, limit=window, offset=window_offset, unread_only=unread_only,
                )
            )
        if want_billing:
            totals["billing"] = _billing_count(
                cursor, user_id, unread_only=unread_only, watermark=watermark,
            )
            items.extend(
                _billing_item(row, watermark) for row in _billing_rows(
                    cursor, user_id, limit=window, offset=window_offset,
                    unread_only=unread_only, watermark=watermark,
                )
            )
        conn.close()

        items.sort(key=lambda item: (item["created_at"] is not None, item["created_at"]), reverse=True)
        if category == "all":
            items = items[offset:offset + limit]
        total = (totals["system"] if want_system else 0) + (totals["billing"] if want_billing else 0)
        return {
            "items": items,
            "total": total,
            "totals": totals,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def get_notification_unread_counts(user_id: int) -> Dict[str, Any]:
    """两类各自的未读数 + 级别分布(铃铛三色仍按 system 级别着色)。"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        watermark = get_billing_read_mark(cursor, user_id)
        system_unread = _system_notification_count(cursor, user_id, unread_only=True)
        billing_unread = _billing_count(cursor, user_id, unread_only=True, watermark=watermark)
        cte, cte_params = _terminal_supersede_cte()
        cursor.execute(
            f"""
            WITH {cte}
            SELECT COALESCE(n.level, 'light') AS level, COUNT(*) AS cnt
            FROM user_notifications n
            WHERE n.user_id = %s AND n.is_read = FALSE
              AND n.id NOT IN (SELECT id FROM superseded)
            GROUP BY COALESCE(n.level, 'light')
            """,
            tuple(list(cte_params) + [user_id, user_id]),
        )
        by_level = {"silent": 0, "light": 0, "gentle": 0, "important": 0, "total": 0}
        for row in cursor.fetchall():
            by_level[row["level"]] = int(row["cnt"])
            by_level["total"] += int(row["cnt"])
        # 扣费通知按 gentle 计入铃铛显示(不抬成 important,免得每笔扣费都抖铃铛)
        by_level["gentle"] += billing_unread
        by_level["total"] += billing_unread
        conn.close()
        return {
            "system": system_unread,
            "billing": billing_unread,
            "total": system_unread + billing_unread,
            "by_level": by_level,
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def mark_feed_item_read(user_id: int, item_id: str) -> bool:
    """标记单条已读。`sys:<id>` 走 user_notifications;`bill:<id>` 只推水位。

    🔴 billing 分支只写 user_notification_read_marks(纯 UI 状态表),
       对 point_transactions / 钱包 / 冻结 **零写入**。
    """
    raw = str(item_id or "").strip()
    if raw.startswith("sys:"):
        return mark_user_notification_read(int(raw[4:]), user_id)
    if raw.startswith("bill:"):
        ref = int(raw[5:])
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_notification_read_marks (user_id, channel, last_read_ref, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, channel) DO UPDATE
                SET last_read_ref = GREATEST(user_notification_read_marks.last_read_ref, EXCLUDED.last_read_ref),
                    updated_at = NOW()
                """,
                (user_id, NOTIFICATION_CHANNEL_BILLING, ref),
            )
            conn.commit()
            conn.close()
            return True
        finally:
            try:
                conn.close()
            except Exception: pass
    # 兼容老前端:纯数字 id 按系统消息处理
    if raw.isdigit():
        return mark_user_notification_read(int(raw), user_id)
    return False


def mark_feed_all_read(user_id: int, category: str = "all") -> int:
    """全部已读(工单 §3.3.4:不做删除,要清爽用"全部已读")。"""
    affected = 0
    if category in ("all", NOTIFICATION_CHANNEL_SYSTEM):
        affected += mark_all_user_notifications_read(user_id)
    if category in ("all", NOTIFICATION_CHANNEL_BILLING):
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM point_transactions "
                "WHERE user_id = %s AND type = ANY(%s)",
                (user_id, list(BILLING_FEED_TX_TYPES)),
            )
            max_id = int((cursor.fetchone() or {}).get("max_id") or 0)
            before = get_billing_read_mark(cursor, user_id)
            cursor.execute(
                """
                INSERT INTO user_notification_read_marks (user_id, channel, last_read_ref, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, channel) DO UPDATE
                SET last_read_ref = GREATEST(user_notification_read_marks.last_read_ref, EXCLUDED.last_read_ref),
                    updated_at = NOW()
                """,
                (user_id, NOTIFICATION_CHANNEL_BILLING, max_id),
            )
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM point_transactions "
                "WHERE user_id = %s AND type = ANY(%s) AND id > %s AND id <= %s",
                (user_id, list(BILLING_FEED_TX_TYPES), before, max_id),
            )
            affected += int((cursor.fetchone() or {}).get("cnt") or 0)
            conn.commit()
            conn.close()
        finally:
            try:
                conn.close()
            except Exception: pass
    return affected


def get_user_unread_by_level(user_id: int) -> Dict[str, int]:
    """返回各级别的未读数，用于前端铃铛三色展示"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(level, 'light') as level, COUNT(*) as cnt
            FROM user_notifications
            WHERE user_id = %s AND is_read = FALSE
            GROUP BY COALESCE(level, 'light')
        """, (user_id,))
        rows = cursor.fetchall()
        conn.close()
        result = {"silent": 0, "light": 0, "gentle": 0, "important": 0, "total": 0}
        for r in rows:
            result[r["level"]] = r["cnt"]
            result["total"] += r["cnt"]
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 活动追踪 (activity_log)
# ==========================================

def log_activity(user_id: int, team_id: Optional[int], action_type: str, metadata: Optional[Dict[str, Any]] = None):
    """记录用户活动"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO activity_log (user_id, team_id, action_type, metadata)
            VALUES (%s, %s, %s, %s)
        """, (user_id, team_id, action_type, json.dumps(metadata) if metadata else None))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"活动记录失败（不影响主流程）: {e}")


def get_member_activity_stats(team_id: int, days: int = 30) -> List[Dict[str, Any]]:
    """获取团队成员活动统计（每人各类action_type的计数）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        since = datetime.now() - timedelta(days=days)
        cursor.execute("""
            SELECT
                a.user_id,
                u.display_name,
                u.username,
                a.action_type,
                COUNT(*) as action_count
            FROM activity_log a
            JOIN users u ON a.user_id = u.id
            WHERE a.team_id = %s AND a.created_at >= %s
            GROUP BY a.user_id, u.display_name, u.username, a.action_type
            ORDER BY u.display_name, a.action_type
        """, (team_id, since))
        rows = cursor.fetchall()
        conn.close()

        # 聚合为 per-member 结构
        member_map: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            uid = r["user_id"]
            if uid not in member_map:
                member_map[uid] = {
                    "user_id": uid,
                    "display_name": r["display_name"] or r["username"],
                    "actions": {},
                    "total": 0,
                }
            member_map[uid]["actions"][r["action_type"]] = r["action_count"]
            member_map[uid]["total"] += r["action_count"]

        return sorted(member_map.values(), key=lambda x: x["total"], reverse=True)
    finally:
        try:
            conn.close()
        except Exception: pass


def get_activity_heatmap(team_id: int, days: int = 84) -> List[Dict[str, Any]]:
    """获取团队每日活动计数（用于热力图，默认84天=12周）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        since = datetime.now() - timedelta(days=days)
        cursor.execute("""
            SELECT DATE(created_at) as day, COUNT(*) as count
            FROM activity_log
            WHERE team_id = %s AND created_at >= %s
            GROUP BY DATE(created_at)
            ORDER BY day
        """, (team_id, since))
        rows = cursor.fetchall()
        conn.close()
        return [{"day": str(r["day"]), "count": r["count"]} for r in rows]
    finally:
        try:
            conn.close()
        except Exception: pass


def get_team_weekly_stats(team_id: int, days: int = 7) -> Dict[str, Any]:
    """获取团队周报原始统计数据"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        since = datetime.now() - timedelta(days=days)

        stats: Dict[str, Any] = {
            "period_days": days,
            "new_scripts": 0,
            "published_scripts": 0,
            "corpus_uploads": 0,
            "reviews": 0,
            "topics_confirmed": 0,
            "active_members": 0,
            "total_actions": 0,
            "top_performer": None,
            "action_breakdown": {},
        }

        # 总操作数与各类分布
        cursor.execute("""
            SELECT action_type, COUNT(*) as cnt
            FROM activity_log
            WHERE team_id = %s AND created_at >= %s
            GROUP BY action_type
        """, (team_id, since))
        for r in cursor.fetchall():
            stats["action_breakdown"][r["action_type"]] = r["cnt"]
            stats["total_actions"] += r["cnt"]

        stats["new_scripts"] = stats["action_breakdown"].get("script_create", 0)
        stats["published_scripts"] = stats["action_breakdown"].get("script_publish", 0)
        stats["corpus_uploads"] = stats["action_breakdown"].get("corpus_upload", 0)
        stats["reviews"] = stats["action_breakdown"].get("review", 0)
        stats["topics_confirmed"] = stats["action_breakdown"].get("topic_confirm", 0)

        # 活跃成员数
        cursor.execute("""
            SELECT COUNT(DISTINCT user_id) as cnt
            FROM activity_log
            WHERE team_id = %s AND created_at >= %s
        """, (team_id, since))
        stats["active_members"] = (cursor.fetchone() or {}).get("cnt", 0)

        # 最活跃成员
        cursor.execute("""
            SELECT a.user_id, u.display_name, COUNT(*) as cnt
            FROM activity_log a
            JOIN users u ON a.user_id = u.id
            WHERE a.team_id = %s AND a.created_at >= %s
            GROUP BY a.user_id, u.display_name
            ORDER BY cnt DESC
            LIMIT 1
        """, (team_id, since))
        top_row = cursor.fetchone()
        if top_row:
            stats["top_performer"] = {
                "user_id": top_row["user_id"],
                "display_name": top_row["display_name"],
                "action_count": top_row["cnt"],
            }

        conn.close()
        return stats
    finally:
        try:
            conn.close()
        except Exception: pass


def leave_team(team_id: int, user_id: int) -> dict:
    """成员主动退出团队"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, role FROM team_members
            WHERE team_id = %s AND user_id = %s AND status = 'active'
        """, (team_id, user_id))
        member = cursor.fetchone()

        if not member:
            return {"success": False, "message": "你不在该团队中"}
        if member["role"] == "leader":
            return {"success": False, "message": "队长请先转让队长角色再退出"}

        cursor.execute("""
            UPDATE team_members SET status = 'removed'
            WHERE team_id = %s AND user_id = %s
        """, (team_id, user_id))
        conn.commit()
        return {"success": True, "message": "已退出团队"}
    finally:
        conn.close()


def get_user_teams(user_id: int) -> list:
    """获取用户加入的所有团队"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.id, t.team_name, t.team_code, t.brand_id, t.leader_id,
                   t.region, t.status, t.max_members,
                   b.name as brand_name,
                   tm.role as my_role,
                   (SELECT COUNT(*) FROM team_members WHERE team_id = t.id AND status = 'active') as member_count
            FROM team_members tm
            JOIN teams t ON tm.team_id = t.id
            LEFT JOIN brands b ON t.brand_id = b.id
            WHERE tm.user_id = %s AND tm.status = 'active' AND t.status = 'active'
            ORDER BY tm.joined_at DESC
        """, (user_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()
