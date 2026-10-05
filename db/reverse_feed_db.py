"""
v3.8 CTO-13.3 · 2026-04-19 rerun 反哺 + 质量门控审计表

功能：
  - 记录 rerun / correct 两类来源对 industry_knowledge 共享池的反哺详情
  - 记录 multi_ai_voter 投票结果（verdicts）+ 差异阈值过滤后的 new_items / rejected_items
  - 支持 admin 一键回滚某次反哺（把 new_items 从共享池撤回）

启动：由 server.py 调 init_reverse_feed_table() 幂等建表。
关联活文档：docs/AI-CONTEXT/PLAN_2026-04-19_rerun_reverse_feed.md
"""

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("ReverseFeedDB")


def _get_conn():
    from db.connection import get_connection
    return get_connection()


def init_reverse_feed_table():
    """幂等建表 + 索引"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS industry_knowledge_reverse_feed (
                id SERIAL PRIMARY KEY,
                source_type VARCHAR(20) NOT NULL,       -- 'ai_rerun' | 'user_correct'
                source_ref VARCHAR(80),                  -- rerun 端点 request 标识 / correct 的 correction_id
                profile_id VARCHAR(40),
                user_id INTEGER,
                level VARCHAR(10) NOT NULL,              -- 'industry' | 'category'
                industry VARCHAR(100),
                category VARCHAR(100),
                field_name VARCHAR(50) NOT NULL,
                new_items JSONB NOT NULL,                -- 投票 + 差异门控通过、实际 merge 进共享池的 delta
                rejected_items JSONB,                    -- 被投票拒绝的（含原因）
                duplicate_items JSONB,                   -- 被差异阈值过滤（重复）的
                vote_result JSONB NOT NULL,              -- {passed, vote_pass_count, vote_total, verdicts:[...]}
                diff_summary JSONB,                      -- {merged_count, rejected_count, duplicate_count}
                rolled_back_at TIMESTAMP,
                rollback_by_admin_id INTEGER,
                rollback_reason TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_rf_industry_field
            ON industry_knowledge_reverse_feed (industry, field_name, created_at DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_rf_source
            ON industry_knowledge_reverse_feed (source_type, created_at DESC);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_rf_active
            ON industry_knowledge_reverse_feed (rolled_back_at)
            WHERE rolled_back_at IS NULL;
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_rf_user
            ON industry_knowledge_reverse_feed (user_id, created_at DESC);
        """)
        conn.commit()
        logger.info("[v3.8] industry_knowledge_reverse_feed 表初始化完成")
    finally:
        conn.close()


def record_reverse_feed(
    source_type: str,                    # 'ai_rerun' | 'user_correct'
    level: str,                           # 'industry' | 'category'
    field_name: str,
    new_items: Any,
    vote_result: dict,
    source_ref: Optional[str] = None,
    profile_id: Optional[str] = None,
    user_id: Optional[int] = None,
    industry: Optional[str] = None,
    category: Optional[str] = None,
    rejected_items: Optional[Any] = None,
    duplicate_items: Optional[Any] = None,
    diff_summary: Optional[dict] = None,
) -> int:
    """写一条反哺审计记录 · 返回 id"""
    if source_type not in ("ai_rerun", "user_correct"):
        raise ValueError(f"source_type 必须是 ai_rerun/user_correct, 收到: {source_type}")
    if level not in ("industry", "category"):
        raise ValueError(f"level 必须是 industry/category, 收到: {level}")

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO industry_knowledge_reverse_feed
              (source_type, source_ref, profile_id, user_id, level,
               industry, category, field_name,
               new_items, rejected_items, duplicate_items,
               vote_result, diff_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                source_type, source_ref, profile_id, user_id, level,
                industry, category, field_name,
                json.dumps(new_items, ensure_ascii=False),
                json.dumps(rejected_items, ensure_ascii=False) if rejected_items is not None else None,
                json.dumps(duplicate_items, ensure_ascii=False) if duplicate_items is not None else None,
                json.dumps(vote_result, ensure_ascii=False, default=str),
                json.dumps(diff_summary, ensure_ascii=False) if diff_summary else None,
            )
        )
        rid = cur.fetchone()["id"]
        conn.commit()
        logger.info(
            f"[v3.8 反哺] source={source_type} level={level}/{industry}/{category or '-'} "
            f"field={field_name} merged={len(new_items) if isinstance(new_items, list) else '?'} "
            f"by user={user_id} → feed_id={rid}"
        )
        return rid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rollback_reverse_feed(feed_id: int, admin_user_id: int, reason: Optional[str] = None) -> dict:
    """
    撤回某次反哺：从 industry_knowledge.knowledge[field_name] 里删掉本次 new_items。
    仅支持 list 类字段（list_dedup / append_only）· overwrite 类字段无法精准回滚返错。
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM industry_knowledge_reverse_feed WHERE id = %s",
            (feed_id,)
        )
        rec = cur.fetchone()
        if not rec:
            raise ValueError(f"反哺记录 #{feed_id} 不存在")
        if rec["rolled_back_at"]:
            raise ValueError(f"反哺记录 #{feed_id} 已回滚于 {rec['rolled_back_at']}")

        field_name = rec["field_name"]
        level = rec["level"]
        industry = rec["industry"]
        category = rec["category"]
        new_items = rec["new_items"]
        if isinstance(new_items, str):
            new_items = json.loads(new_items)
        if not isinstance(new_items, list):
            raise ValueError(f"field={field_name} 非 list 类型，无法按 item 粒度回滚")

        # 取当前共享池条目
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
        ik_row = cur.fetchone()
        if not ik_row:
            raise ValueError(f"共享库条目 {level}/{industry}/{category or '-'} 不存在")

        knowledge = ik_row["knowledge"]
        if isinstance(knowledge, str):
            knowledge = json.loads(knowledge)

        current = knowledge.get(field_name) or []
        if not isinstance(current, list):
            raise ValueError(f"共享池 field={field_name} 当前非 list，无法回滚")

        # 从 current 里删掉 new_items 中的每个 item（按 unique_key 或整体 JSON 相等）
        from tools.knowledge_layers import LAYER_CONFIG as _LC
        unique_key = _LC.get(field_name, {}).get("unique_key")

        def _key_of(item):
            if isinstance(item, dict) and unique_key:
                return item.get(unique_key)
            try:
                return json.dumps(item, ensure_ascii=False, sort_keys=True) if not isinstance(item, str) else item
            except Exception:
                return str(item)

        to_remove_keys = {_key_of(it) for it in new_items}
        filtered = [it for it in current if _key_of(it) not in to_remove_keys]
        removed_count = len(current) - len(filtered)

        knowledge[field_name] = filtered

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

        cur.execute(
            """
            UPDATE industry_knowledge_reverse_feed
            SET rolled_back_at = NOW(),
                rollback_by_admin_id = %s,
                rollback_reason = %s
            WHERE id = %s
            """,
            (admin_user_id, reason, feed_id)
        )
        conn.commit()

        logger.info(
            f"[v3.8 反哺回滚] feed_id={feed_id} removed={removed_count}/{len(new_items)} by admin={admin_user_id}"
        )
        return {
            "success": True,
            "feed_id": feed_id,
            "removed_count": removed_count,
            "expected_count": len(new_items),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_reverse_feeds(
    industry: Optional[str] = None,
    field_name: Optional[str] = None,
    source_type: Optional[str] = None,
    only_active: bool = True,
    limit: int = 50,
) -> list:
    """admin 查询反哺记录（倒序最新）"""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        where = []
        params = []
        if industry:
            where.append("industry = %s"); params.append(industry)
        if field_name:
            where.append("field_name = %s"); params.append(field_name)
        if source_type:
            where.append("source_type = %s"); params.append(source_type)
        if only_active:
            where.append("rolled_back_at IS NULL")
        where_sql = " AND ".join(where) if where else "1=1"
        params.append(limit)
        cur.execute(
            f"""SELECT id, source_type, source_ref, profile_id, user_id, level,
                       industry, category, field_name,
                       new_items, rejected_items, duplicate_items,
                       vote_result, diff_summary,
                       rolled_back_at, rollback_by_admin_id, rollback_reason,
                       created_at
                FROM industry_knowledge_reverse_feed
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT %s""",
            params
        )
        rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "source_type": r["source_type"],
                "source_ref": r["source_ref"],
                "profile_id": r["profile_id"],
                "user_id": r["user_id"],
                "level": r["level"],
                "industry": r["industry"],
                "category": r["category"],
                "field_name": r["field_name"],
                "new_items": r["new_items"],
                "rejected_items": r["rejected_items"],
                "duplicate_items": r["duplicate_items"],
                "vote_result": r["vote_result"],
                "diff_summary": r["diff_summary"],
                "rolled_back_at": r["rolled_back_at"].isoformat() if r["rolled_back_at"] else None,
                "rollback_by_admin_id": r["rollback_by_admin_id"],
                "rollback_reason": r["rollback_reason"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    finally:
        conn.close()
