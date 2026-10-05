"""
v3.6 CTO-15.2 2026-04-19 · L1/L2 公共素材矫正反哺机制

支持用户矫正 industry_knowledge 表里的 raw 素材并立即反哺到全行业。
admin 后台可一键回滚任何矫正。

启动时由 server.py 调用 init_industry_corrections_table()。
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("IndustryCorrectionsDB")


def _get_conn():
    from db.connection import get_connection
    return get_connection()


def init_industry_corrections_table():
    """幂等创建表"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS industry_knowledge_corrections (
                id SERIAL PRIMARY KEY,
                level VARCHAR(10) NOT NULL,
                industry VARCHAR(100) NOT NULL,
                category VARCHAR(100),
                field_name VARCHAR(50) NOT NULL,
                item_index INTEGER,
                old_value JSONB,
                new_value JSONB,
                action VARCHAR(20) DEFAULT 'update',
                corrector_user_id INTEGER,
                corrector_profile_id INTEGER,
                reason TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                rolled_back_at TIMESTAMP,
                rollback_by_admin_id INTEGER
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ikc_level_industry
            ON industry_knowledge_corrections (level, industry, category);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ikc_active
            ON industry_knowledge_corrections (rolled_back_at)
            WHERE rolled_back_at IS NULL;
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ikc_corrector
            ON industry_knowledge_corrections (corrector_user_id, created_at DESC);
        """)
        conn.commit()
        logger.info("[v3.6] industry_knowledge_corrections 表初始化完成")
    finally:
        conn.close()


def apply_correction_to_l1_l2(
    level: str,                 # 'industry' or 'category'
    industry: str,
    category: Optional[str],
    field_name: str,            # 如 'industry_jargon' / 'authority_sources'
    new_value: Any,             # 新值(整体替换或单条)
    action: str = "update",     # 'update' / 'add' / 'delete'
    item_index: Optional[int] = None,  # 列表型字段索引,None 表示整体替换
    corrector_user_id: Optional[int] = None,
    corrector_profile_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> dict:
    """
    矫正 industry_knowledge 表里的 raw 素材,立即反哺全行业(老板拍板:不要 N 人共识阈值)。

    Returns:
        {"success": True, "correction_id": int, "old_value": ..., "new_value": ...}
        失败抛异常
    """
    if level not in ("industry", "category"):
        raise ValueError(f"level 必须是 industry 或 category, 收到: {level}")
    if level == "industry" and category:
        category = None  # L1 不传 category
    if level == "category" and not category:
        raise ValueError("L2 矫正必须传 category")

    conn = _get_conn()
    try:
        cur = conn.cursor()

        # 读出当前 knowledge
        if category:
            cur.execute(
                "SELECT id, knowledge FROM industry_knowledge WHERE level=%s AND industry=%s AND category=%s",
                (level, industry, category)
            )
        else:
            cur.execute(
                "SELECT id, knowledge FROM industry_knowledge WHERE level=%s AND industry=%s AND category IS NULL",
                (level, industry)
            )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"L{1 if level=='industry' else 2} 公共库无该条目: {industry}/{category or '-'}")

        knowledge = row["knowledge"]
        if isinstance(knowledge, str):
            knowledge = json.loads(knowledge)

        # 计算 old_value(用于回滚 + 记录)
        if item_index is not None:
            current_field = knowledge.get(field_name) or []
            if not isinstance(current_field, list):
                raise ValueError(f"字段 {field_name} 不是列表型,不能用 item_index")
            old_value = current_field[item_index] if 0 <= item_index < len(current_field) else None
        else:
            old_value = knowledge.get(field_name)

        # 应用矫正
        if action == "update":
            if item_index is not None:
                if 0 <= item_index < len(current_field):
                    current_field[item_index] = new_value
                    knowledge[field_name] = current_field
                else:
                    raise ValueError(f"item_index {item_index} 超出范围")
            else:
                knowledge[field_name] = new_value
        elif action == "add":
            if not isinstance(knowledge.get(field_name), list):
                knowledge[field_name] = []
            knowledge[field_name].append(new_value)
        elif action == "delete":
            if item_index is not None and isinstance(knowledge.get(field_name), list):
                if 0 <= item_index < len(knowledge[field_name]):
                    old_value = knowledge[field_name].pop(item_index)
                else:
                    raise ValueError(f"item_index {item_index} 超出范围")
            else:
                raise ValueError("delete 必须传 item_index")
        else:
            raise ValueError(f"action 必须是 update/add/delete, 收到: {action}")

        # 写回 industry_knowledge
        cur.execute(
            """
            UPDATE industry_knowledge
            SET knowledge = %s,
                version = version + 1,
                correction_count = correction_count + 1,
                generated_at = NOW()
            WHERE id = %s
            """,
            (json.dumps(knowledge, ensure_ascii=False), row["id"])
        )

        # 记录矫正日志
        cur.execute(
            """
            INSERT INTO industry_knowledge_corrections
            (level, industry, category, field_name, item_index, old_value, new_value,
             action, corrector_user_id, corrector_profile_id, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                level, industry, category, field_name, item_index,
                json.dumps(old_value, ensure_ascii=False) if old_value is not None else None,
                json.dumps(new_value, ensure_ascii=False) if new_value is not None else None,
                action, corrector_user_id, corrector_profile_id, reason,
            )
        )
        correction_id = cur.fetchone()["id"]
        conn.commit()

        logger.info(
            f"[v3.6 矫正] {level}/{industry}/{category or '-'} field={field_name} "
            f"action={action} idx={item_index} by user={corrector_user_id} → correction_id={correction_id}"
        )
        return {
            "success": True,
            "correction_id": correction_id,
            "old_value": old_value,
            "new_value": new_value,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rollback_correction(correction_id: int, admin_user_id: int) -> dict:
    """
    admin 一键回滚某条矫正,恢复 industry_knowledge 该字段为 old_value。

    Returns:
        {"success": True, "restored_field": str, "restored_to": ...}
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()

        # 取出矫正记录
        cur.execute(
            "SELECT * FROM industry_knowledge_corrections WHERE id = %s",
            (correction_id,)
        )
        rec = cur.fetchone()
        if not rec:
            raise ValueError(f"矫正记录 #{correction_id} 不存在")
        if rec["rolled_back_at"]:
            raise ValueError(f"矫正记录 #{correction_id} 已回滚于 {rec['rolled_back_at']}")

        # 取当前 industry_knowledge
        category = rec["category"]
        if category:
            cur.execute(
                "SELECT id, knowledge FROM industry_knowledge WHERE level=%s AND industry=%s AND category=%s",
                (rec["level"], rec["industry"], category)
            )
        else:
            cur.execute(
                "SELECT id, knowledge FROM industry_knowledge WHERE level=%s AND industry=%s AND category IS NULL",
                (rec["level"], rec["industry"])
            )
        ik_row = cur.fetchone()
        if not ik_row:
            raise ValueError("公共库条目已不存在,无法回滚")

        knowledge = ik_row["knowledge"]
        if isinstance(knowledge, str):
            knowledge = json.loads(knowledge)

        # 恢复 old_value
        old_value = rec["old_value"]
        if isinstance(old_value, str):
            old_value = json.loads(old_value)

        if rec["item_index"] is not None:
            current_field = knowledge.get(rec["field_name"]) or []
            if not isinstance(current_field, list):
                current_field = []
            # 把当前 item 替换回 old_value(回滚 update);如果 action=add,则删除该 item;如果 action=delete,则插回
            if rec["action"] == "update":
                if 0 <= rec["item_index"] < len(current_field):
                    current_field[rec["item_index"]] = old_value
                else:
                    current_field.append(old_value)
            elif rec["action"] == "add":
                # 删除最后追加的 item
                if current_field and current_field[-1] == json.loads(rec["new_value"]) if isinstance(rec["new_value"], str) else rec["new_value"]:
                    current_field.pop()
            elif rec["action"] == "delete":
                # 插回原位置
                current_field.insert(rec["item_index"], old_value)
            knowledge[rec["field_name"]] = current_field
        else:
            knowledge[rec["field_name"]] = old_value

        # 写回 industry_knowledge
        cur.execute(
            """
            UPDATE industry_knowledge
            SET knowledge = %s,
                version = version + 1,
                generated_at = NOW()
            WHERE id = %s
            """,
            (json.dumps(knowledge, ensure_ascii=False), ik_row["id"])
        )

        # 标记矫正已回滚
        cur.execute(
            """
            UPDATE industry_knowledge_corrections
            SET rolled_back_at = NOW(), rollback_by_admin_id = %s
            WHERE id = %s
            """,
            (admin_user_id, correction_id)
        )
        conn.commit()

        logger.info(
            f"[v3.6 回滚] correction_id={correction_id} by admin={admin_user_id} "
            f"→ {rec['level']}/{rec['industry']}/{rec['category'] or '-'} field={rec['field_name']}"
        )
        return {
            "success": True,
            "correction_id": correction_id,
            "restored_field": rec["field_name"],
            "restored_to": old_value,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_corrections(
    industry: Optional[str] = None,
    category: Optional[str] = None,
    only_active: bool = True,
    limit: int = 50,
) -> list:
    """admin 查询矫正记录(过滤已回滚 + 倒序最新)"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        where_clauses = []
        params = []
        if industry:
            where_clauses.append("industry = %s")
            params.append(industry)
        if category:
            where_clauses.append("category = %s")
            params.append(category)
        if only_active:
            where_clauses.append("rolled_back_at IS NULL")
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        params.append(limit)
        cur.execute(
            f"""SELECT id, level, industry, category, field_name, item_index, action,
                       old_value, new_value, corrector_user_id, corrector_profile_id, reason,
                       created_at, rolled_back_at, rollback_by_admin_id
                FROM industry_knowledge_corrections
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT %s""",
            params
        )
        rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "level": r["level"],
                "industry": r["industry"],
                "category": r["category"],
                "field_name": r["field_name"],
                "item_index": r["item_index"],
                "action": r["action"],
                "old_value": r["old_value"],
                "new_value": r["new_value"],
                "corrector_user_id": r["corrector_user_id"],
                "corrector_profile_id": r["corrector_profile_id"],
                "reason": r["reason"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "rolled_back_at": r["rolled_back_at"].isoformat() if r["rolled_back_at"] else None,
                "rollback_by_admin_id": r["rollback_by_admin_id"],
            }
            for r in rows
        ]
    finally:
        conn.close()
