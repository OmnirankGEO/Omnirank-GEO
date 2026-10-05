"""
一键策划 — 内容计划数据库操作
content_plans + plan_tasks 表的建表与 CRUD
"""

import json
import logging
import shortuuid
from datetime import datetime, date
from typing import Optional, List, Dict, Any

logger = logging.getLogger("GEO-Plan")

_PLAN_EXECUTED_STATUSES = {"filmed", "ready_to_publish", "published"}


def get_connection():
    """获取数据库连接"""
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


# ========== 建表 ==========

_tables_ensured = False


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, column),
    )
    return cursor.fetchone() is not None


def _safe_add_column(cursor, table: str, column: str, col_type: str) -> None:
    if _column_exists(cursor, table, column):
        return
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except Exception:
        # 并发启动时可能已有别的 worker 加过，保持幂等。
        pass


def ensure_tables():
    """确保 content_plans + plan_tasks 表存在（仅首次调用）"""
    global _tables_ensured
    if _tables_ensured:
        return
    try:
        conn = get_connection()
        conn.autocommit = True
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS content_plans (
                id VARCHAR(22) PRIMARY KEY,
                profile_id VARCHAR(22) NOT NULL,
                month VARCHAR(7) NOT NULL,
                mode VARCHAR(20) NOT NULL DEFAULT 'inspiration',
                stage VARCHAR(20),
                topic_mix JSONB,
                weekly_themes JSONB,
                milestones JSONB,
                monthly_summary TEXT,
                encouragement TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                is_deleted INTEGER DEFAULT 0
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS plan_tasks (
                id VARCHAR(22) PRIMARY KEY,
                plan_id VARCHAR(22) NOT NULL REFERENCES content_plans(id),
                week INTEGER NOT NULL,
                day_of_week INTEGER,
                sort_order INTEGER DEFAULT 0,
                topic_title VARCHAR(500) NOT NULL,
                topic_category VARCHAR(20) NOT NULL,
                script_type VARCHAR(20),
                content_group VARCHAR(50),
                duration_seconds INTEGER DEFAULT 60,
                is_pinned BOOLEAN DEFAULT FALSE,
                ai_note TEXT,
                status VARCHAR(20) DEFAULT 'idea',
                script_id INTEGER,
                planned_date DATE,
                published_date DATE,
                performance_data JSONB,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS plan_task_script_versions (
                id SERIAL PRIMARY KEY,
                task_id VARCHAR(22) NOT NULL REFERENCES plan_tasks(id) ON DELETE CASCADE,
                script_id INTEGER,
                title TEXT,
                content TEXT NOT NULL,
                change_reason TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # v5 内容规划闭环：归属、topic/script 正式链路、执行责任与证据字段
        for col, col_def in [
            ("brand_id", "INTEGER"),
            ("user_id", "INTEGER"),
            ("team_id", "INTEGER"),
            ("source_project_id", "INTEGER"),
            ("monthly_goal", "VARCHAR(20) DEFAULT 'growth'"),
            ("strategy_pack", "JSONB"),
        ]:
            _safe_add_column(cursor, "content_plans", col, col_def)

        for col, col_def in [
            ("content_group", "VARCHAR(50)"),
            ("duration_seconds", "INTEGER DEFAULT 60"),
            ("is_pinned", "BOOLEAN DEFAULT FALSE"),
            ("ai_note", "TEXT"),
            ("published_date", "DATE"),
            ("performance_data", "JSONB"),
            ("topic_id", "INTEGER"),
            ("brief_snapshot", "JSONB"),
            ("source_refs", "JSONB"),
            ("assigned_role", "TEXT"),
            ("assigned_user_id", "INTEGER"),
            ("success_metric", "TEXT"),
            ("operator_note", "TEXT"),
            ("approval_status", "VARCHAR(20)"),
            ("approval_note", "TEXT"),
            ("approved_at", "TIMESTAMP"),
        ]:
            _safe_add_column(cursor, "plan_tasks", col, col_def)

        # 索引
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_content_plans_profile_month
            ON content_plans(profile_id, month)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_plan_tasks_plan_id
            ON plan_tasks(plan_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_plan_tasks_script_id
            ON plan_tasks(script_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_content_plans_brand_month
            ON content_plans(brand_id, month) WHERE brand_id IS NOT NULL
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_plan_tasks_topic_id
            ON plan_tasks(topic_id) WHERE topic_id IS NOT NULL
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_plan_task_script_versions_task
            ON plan_task_script_versions(task_id, created_at DESC)
        """)

        cursor.close()
        conn.close()
        logger.info("[Plan] content_plans + plan_tasks 表已就绪")
    except Exception as e:
        logger.warning(f"[Plan] 建表异常（可忽略并发竞态）: {e}")
    _tables_ensured = True


# ========== CRUD ==========


def create_plan(
    profile_id: str,
    month: str,
    mode: str = "inspiration",
    stage: str = None,
    topic_mix: dict = None,
    weekly_themes: list = None,
    milestones: list = None,
    monthly_summary: str = None,
    encouragement: str = None,
    brand_id: int = None,
    user_id: int = None,
    team_id: int = None,
    source_project_id: int = None,
    monthly_goal: str = "growth",
    strategy_pack: dict = None,
) -> Dict[str, Any]:
    """创建月度计划，返回 plan dict"""
    ensure_tables()
    plan_id = shortuuid.uuid()[:12]
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO content_plans
                (id, profile_id, month, mode, stage, topic_mix, weekly_themes,
                 milestones, monthly_summary, encouragement, brand_id, user_id,
                 team_id, source_project_id, monthly_goal, strategy_pack)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                plan_id, profile_id, month, mode, stage,
                json.dumps(topic_mix) if topic_mix else None,
                json.dumps(weekly_themes) if weekly_themes else None,
                json.dumps(milestones) if milestones else None,
                monthly_summary, encouragement, brand_id, user_id, team_id,
                source_project_id, monthly_goal,
                json.dumps(strategy_pack, ensure_ascii=False) if strategy_pack else None,
            ),
        )
        plan = dict(cursor.fetchone())
        conn.commit()
        cursor.close()
        return plan
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_plan(profile_id: str, month: str = None) -> Optional[Dict]:
    """获取某个档案的月度计划（默认当月），返回 plan dict 或 None"""
    ensure_tables()
    if not month:
        month = datetime.now().strftime("%Y-%m")
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM content_plans
            WHERE profile_id = %s AND month = %s AND is_deleted = 0
            ORDER BY created_at DESC LIMIT 1
            """,
            (profile_id, month),
        )
        row = cursor.fetchone()
        cursor.close()
        if row:
            plan = dict(row)
            _parse_plan_json(plan)
            return plan
        return None
    finally:
        conn.close()


def get_plan_by_id(plan_id: str) -> Optional[Dict]:
    """按 plan_id 获取计划"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM content_plans WHERE id = %s AND is_deleted = 0", (plan_id,))
        row = cursor.fetchone()
        cursor.close()
        if row:
            plan = dict(row)
            _parse_plan_json(plan)
            return plan
        return None
    finally:
        conn.close()


def get_plan_tasks(plan_id: str) -> List[Dict]:
    """获取计划下所有任务，按 week → sort_order 排序"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT to_regclass('public.social_scripts') AS table_name")
        social_scripts_exists = bool((cursor.fetchone() or {}).get("table_name"))
        if social_scripts_exists:
            cursor.execute(
                """
                SELECT
                    t.*,
                    s.title AS script_title,
                    s.script_content AS script_text,
                    s.word_count AS script_word_count,
                    s.status AS script_status,
                    (
                        SELECT COUNT(*)
                        FROM plan_task_script_versions v
                        WHERE v.task_id = t.id
                    ) AS script_version_count
                FROM plan_tasks t
                LEFT JOIN social_scripts s ON s.id = t.script_id
                WHERE t.plan_id = %s
                ORDER BY t.week, t.sort_order, t.day_of_week NULLS LAST
                """,
                (plan_id,),
            )
        else:
            cursor.execute(
                """
                SELECT
                    t.*,
                    (
                        SELECT COUNT(*)
                        FROM plan_task_script_versions v
                        WHERE v.task_id = t.id
                    ) AS script_version_count
                FROM plan_tasks t
                WHERE plan_id = %s
                ORDER BY week, sort_order, day_of_week NULLS LAST
                """,
                (plan_id,),
            )
        rows = cursor.fetchall()
        cursor.close()
        return [_parse_task_json(dict(r)) for r in rows]
    finally:
        conn.close()


def get_recent_profile_tasks(profile_id: str, limit: int = 24) -> List[Dict]:
    """获取某档案最近的计划任务，用于下轮规划吸收发布复盘。"""
    ensure_tables()
    safe_limit = max(1, min(int(limit or 24), 80))
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                t.*,
                p.month AS plan_month,
                p.monthly_summary AS plan_monthly_summary
            FROM plan_tasks t
            JOIN content_plans p ON p.id = t.plan_id
            WHERE p.profile_id = %s
              AND p.is_deleted = 0
            ORDER BY
              t.published_date DESC NULLS LAST,
              p.month DESC,
              t.week DESC,
              t.sort_order DESC
            LIMIT %s
            """,
            (profile_id, safe_limit),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [_parse_task_json(dict(r)) for r in rows]
    finally:
        conn.close()


def create_tasks_batch(plan_id: str, tasks: List[Dict]) -> List[Dict]:
    """批量创建任务"""
    ensure_tables()
    conn = get_connection()
    created = []
    try:
        cursor = conn.cursor()
        for t in tasks:
            task_id = shortuuid.uuid()[:12]
            cursor.execute(
                """
                INSERT INTO plan_tasks
                    (id, plan_id, week, day_of_week, sort_order,
                     topic_title, topic_category, script_type, content_group,
                     duration_seconds, is_pinned, ai_note, status, planned_date,
                     script_id, topic_id, brief_snapshot, source_refs, assigned_role,
                     assigned_user_id, success_metric)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    task_id, plan_id,
                    t.get("week", 1),
                    t.get("day_of_week"),
                    t.get("sort_order", 0),
                    t["topic_title"],
                    t.get("topic_category", "traffic"),
                    t.get("script_type"),
                    t.get("content_group"),
                    t.get("duration_seconds", 60),
                    t.get("is_pinned", False),
                    t.get("ai_note"),
                    t.get("status", "idea"),
                    t.get("planned_date"),
                    t.get("script_id"),
                    t.get("topic_id"),
                    json.dumps(t.get("brief_snapshot"), ensure_ascii=False) if t.get("brief_snapshot") else None,
                    json.dumps(t.get("source_refs"), ensure_ascii=False) if t.get("source_refs") else None,
                    t.get("assigned_role"),
                    t.get("assigned_user_id"),
                    t.get("success_metric"),
                ),
            )
            created.append(dict(cursor.fetchone()))
        conn.commit()
        cursor.close()
        return [_parse_task_json(c) for c in created]
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def add_task(plan_id: str, task: Dict) -> Dict:
    """追加单条任务"""
    return create_tasks_batch(plan_id, [task])[0]


def _fetch_task_with_script(cursor, task_id: str) -> Optional[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT
            t.*,
            s.title AS script_title,
            s.script_content AS script_text,
            s.word_count AS script_word_count,
            s.status AS script_status,
            (
                SELECT COUNT(*)
                FROM plan_task_script_versions v
                WHERE v.task_id = t.id
            ) AS script_version_count
        FROM plan_tasks t
        LEFT JOIN social_scripts s ON s.id = t.script_id
        WHERE t.id = %s
        """,
        (task_id,),
    )
    row = cursor.fetchone()
    return _parse_task_json(dict(row)) if row else None


def _backup_current_task_script(cursor, task_id: str, change_reason: str) -> None:
    task = _fetch_task_with_script(cursor, task_id)
    content = (task or {}).get("script_text")
    script_id = (task or {}).get("script_id")
    if not script_id or not content:
        return
    cursor.execute(
        """
        INSERT INTO plan_task_script_versions
            (task_id, script_id, title, content, change_reason)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            task_id,
            script_id,
            (task or {}).get("script_title") or (task or {}).get("topic_title"),
            content,
            change_reason,
        ),
    )


def update_task_status(task_id: str, status: str, script_id: int = None) -> Optional[Dict]:
    """更新任务状态（可选回写 script_id）"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if script_id is not None:
            current = _fetch_task_with_script(cursor, task_id)
            current_script_id = (current or {}).get("script_id")
            if current_script_id and str(current_script_id) != str(script_id):
                _backup_current_task_script(cursor, task_id, "新稿覆盖前自动备份")
        sets = ["status = %s", "updated_at = NOW()"]
        vals = [status]
        if script_id is not None:
            sets.append("script_id = %s")
            vals.append(script_id)
        if status == "published":
            sets.append("published_date = CURRENT_DATE")
        if status == "idea":
            # 恢复时清空关联
            sets.append("script_id = NULL")
        vals.append(task_id)
        cursor.execute(
            f"UPDATE plan_tasks SET {', '.join(sets)} WHERE id = %s RETURNING *",
            vals,
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return _parse_task_json(dict(row)) if row else None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def claim_task_writing(task_id: str, allow_rewrite: bool = False) -> Optional[Dict]:
    """把待写任务条件占用为 writing。

    默认只允许 idea 进入写稿中；需要改稿时可允许 has_script 进入 writing。
    已拍、待发、已发、已有发布复盘资产的内容不能覆盖，只能追拍或新建任务。
    """
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        allowed_statuses = ("idea", "has_script") if allow_rewrite else ("idea",)
        cursor.execute(
            """
            UPDATE plan_tasks
            SET status = 'writing', updated_at = NOW()
            WHERE id = %s
              AND COALESCE(status, 'idea') = ANY(%s)
              AND (
                    COALESCE(status, 'idea') = 'idea'
                    OR (%s = TRUE AND COALESCE(status, 'idea') = 'has_script' AND script_id IS NOT NULL)
                  )
              AND published_date IS NULL
              AND (performance_data IS NULL OR performance_data = '{}'::jsonb)
            RETURNING *
            """,
            (task_id, list(allowed_statuses), bool(allow_rewrite)),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return _parse_task_json(dict(row)) if row else None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def release_task_writing(task_id: str) -> Optional[Dict]:
    """释放尚未保存成稿的 writing 任务。

    只在当前仍是 writing 时释放：有旧 script_id 的回到 has_script，没有脚本的回到 idea。
    避免迟到的失败回调清掉别人已保存的脚本。
    """
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE plan_tasks
            SET status = CASE WHEN script_id IS NULL THEN 'idea' ELSE 'has_script' END,
                updated_at = NOW()
            WHERE id = %s
              AND status = 'writing'
            RETURNING *
            """,
            (task_id,),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return _parse_task_json(dict(row)) if row else None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_task_by_id(task_id: str) -> Optional[Dict]:
    """按 task_id 获取计划任务（权限校验和跨模块回写使用）。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        task = _fetch_task_with_script(cursor, task_id)
        cursor.close()
        return task
    finally:
        conn.close()


def get_task_script_versions(task_id: str, limit: int = 10) -> List[Dict]:
    """获取单条计划任务的脚本历史版本。"""
    ensure_tables()
    safe_limit = max(1, min(int(limit or 10), 30))
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, task_id, script_id, title, content, change_reason, created_at
            FROM plan_task_script_versions
            WHERE task_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (task_id, safe_limit),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def restore_latest_task_script_version(task_id: str) -> Optional[Dict]:
    """把计划任务恢复到最近一个脚本历史版本。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, script_id, content
            FROM plan_task_script_versions
            WHERE task_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (task_id,),
        )
        version = cursor.fetchone()
        if not version or not version.get("script_id"):
            cursor.close()
            conn.rollback()
            return None

        _backup_current_task_script(cursor, task_id, "恢复上一版前自动备份")
        cursor.execute(
            """
            UPDATE plan_tasks
            SET script_id = %s,
                status = CASE
                    WHEN COALESCE(status, 'idea') IN ('filmed', 'ready_to_publish', 'published') THEN status
                    ELSE 'has_script'
                END,
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (version["script_id"], task_id),
        )
        row = cursor.fetchone()
        task = _fetch_task_with_script(cursor, task_id) if row else None
        conn.commit()
        cursor.close()
        return task
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_task_by_script_id(script_id: int) -> Optional[Dict]:
    """按 script_id 找到关联的计划任务。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM plan_tasks
            WHERE script_id = %s
            ORDER BY updated_at DESC LIMIT 1
            """,
            (script_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        return _parse_task_json(dict(row)) if row else None
    finally:
        conn.close()


def update_task_performance(
    task_id: str,
    performance_data: Dict[str, Any],
    status: str = None,
    published_date: Any = None,
) -> Optional[Dict]:
    """回写任务发布效果，供发布复盘链路使用。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        sets = ["performance_data = %s", "updated_at = NOW()"]
        vals = [json.dumps(performance_data or {}, ensure_ascii=False, default=str)]
        if status:
            sets.append("status = %s")
            vals.append(status)
        if published_date is not None:
            sets.append("published_date = COALESCE(%s, published_date)")
            vals.append(published_date)
        elif status == "published":
            sets.append("published_date = COALESCE(published_date, CURRENT_DATE)")
        vals.append(task_id)
        cursor.execute(
            f"UPDATE plan_tasks SET {', '.join(sets)} WHERE id = %s RETURNING *",
            vals,
        )
        row = cursor.fetchone()
        task = _fetch_task_with_script(cursor, task_id) if row else None
        conn.commit()
        cursor.close()
        return task
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def update_task_brief(
    task_id: str,
    brief_snapshot: Dict[str, Any],
    source_refs: Dict[str, Any] = None,
) -> Optional[Dict]:
    """写入/刷新单条任务的 writer_brief 快照。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE plan_tasks
            SET brief_snapshot = %s,
                source_refs = COALESCE(%s, source_refs),
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (
                json.dumps(brief_snapshot or {}, ensure_ascii=False, default=str),
                json.dumps(source_refs, ensure_ascii=False, default=str) if source_refs is not None else None,
                task_id,
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return _parse_task_json(dict(row)) if row else None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def update_task_operator_note(task_id: str, operator_note: str) -> Optional[Dict]:
    """更新单条计划任务的人工拍摄备注。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE plan_tasks
            SET operator_note = %s,
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            ((operator_note or "").strip() or None, task_id),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return _parse_task_json(dict(row)) if row else None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def update_task_approval(task_id: str, approval_status: str, approval_note: str = None) -> Optional[Dict]:
    """更新计划任务的客户确认状态。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE plan_tasks
            SET approval_status = %s,
                approval_note = %s,
                approved_at = CASE WHEN %s = 'approved' THEN NOW() ELSE approved_at END,
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (
                approval_status,
                (approval_note or "").strip() or None,
                approval_status,
                task_id,
            ),
        )
        row = cursor.fetchone()
        task = _fetch_task_with_script(cursor, task_id) if row else None
        conn.commit()
        cursor.close()
        return task
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def update_task_performance_by_script_id(
    script_id: int,
    performance_data: Dict[str, Any],
    status: str = "published",
    published_date: Any = None,
) -> int:
    """根据 script_id 回写一个或多个 plan_task 的效果数据。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        sets = ["performance_data = %s", "updated_at = NOW()"]
        vals = [json.dumps(performance_data or {}, ensure_ascii=False, default=str)]
        if status:
            sets.append("status = %s")
            vals.append(status)
        if published_date is not None:
            sets.append("published_date = COALESCE(%s, published_date)")
            vals.append(published_date)
        elif status == "published":
            sets.append("published_date = COALESCE(published_date, CURRENT_DATE)")
        vals.append(script_id)
        cursor.execute(
            f"UPDATE plan_tasks SET {', '.join(sets)} WHERE script_id = %s",
            vals,
        )
        affected = cursor.rowcount
        conn.commit()
        cursor.close()
        return affected
    except Exception as e:
        conn.rollback()
        logger.warning(f"[Plan] update_task_performance_by_script_id 失败: {e}")
        return 0
    finally:
        conn.close()


def update_plan_mode(plan_id: str, mode: str) -> Optional[Dict]:
    """切换计划模式"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE content_plans SET mode = %s, updated_at = NOW()
            WHERE id = %s AND is_deleted = 0 RETURNING *
            """,
            (mode, plan_id),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        if row:
            plan = dict(row)
            _parse_plan_json(plan)
            return plan
        return None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def update_plan_core(
    plan_id: str,
    mode: str = None,
    stage: str = None,
    topic_mix: dict = None,
    weekly_themes: list = None,
    milestones: list = None,
    monthly_summary: str = None,
    encouragement: str = None,
    monthly_goal: str = None,
    strategy_pack: dict = None,
) -> Optional[Dict]:
    """更新计划主体信息，不触碰已存在任务。"""
    ensure_tables()
    updates = []
    vals = []
    mapping = {
        "mode": mode,
        "stage": stage,
        "topic_mix": json.dumps(topic_mix, ensure_ascii=False) if topic_mix is not None else None,
        "weekly_themes": json.dumps(weekly_themes, ensure_ascii=False) if weekly_themes is not None else None,
        "milestones": json.dumps(milestones, ensure_ascii=False) if milestones is not None else None,
        "monthly_summary": monthly_summary,
        "encouragement": encouragement,
        "monthly_goal": monthly_goal,
        "strategy_pack": json.dumps(strategy_pack, ensure_ascii=False) if strategy_pack is not None else None,
    }
    for col, val in mapping.items():
        if val is not None:
            updates.append(f"{col} = %s")
            vals.append(val)
    if not updates:
        return get_plan_by_id(plan_id)
    updates.append("updated_at = NOW()")
    vals.append(plan_id)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE content_plans SET {', '.join(updates)} WHERE id = %s AND is_deleted = 0 RETURNING *",
            vals,
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        if row:
            plan = dict(row)
            _parse_plan_json(plan)
            return plan
        return None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def delete_unstarted_tasks(plan_id: str) -> int:
    """删除仍处于 idea 且没有任何执行资产的任务，用于安全重排未开始内容。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM plan_tasks
            WHERE plan_id = %s
              AND COALESCE(status, 'idea') = 'idea'
              AND script_id IS NULL
              AND published_date IS NULL
              AND (performance_data IS NULL OR performance_data = '{}'::jsonb)
            """,
            (plan_id,),
        )
        affected = cursor.rowcount
        conn.commit()
        cursor.close()
        return affected
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def delete_safe_task(task_id: str) -> bool:
    """安全删除单条还没进入执行的任务。

    只允许删除 idea/skipped 且没有脚本、发布日期、复盘数据的任务。
    已成稿/已拍/已发的任务是内容资产，只能改状态，不能从数据库直接删。
    """
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM plan_tasks
            WHERE id = %s
              AND COALESCE(status, 'idea') IN ('idea', 'skipped')
              AND script_id IS NULL
              AND published_date IS NULL
              AND (performance_data IS NULL OR performance_data = '{}'::jsonb)
            """,
            (task_id,),
        )
        affected = cursor.rowcount
        conn.commit()
        cursor.close()
        return affected > 0
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def replace_unstarted_tasks(
    plan_id: str,
    plan_updates: Dict[str, Any],
    tasks: List[Dict],
) -> Dict[str, Any]:
    """在同一事务里更新计划主体、删除未开始任务、补入新任务。

    用于“只重排未开始内容”。旧的已执行任务必须原样保留；如果插入新任务失败，
    删除未开始任务和计划主体更新会一起回滚。
    """
    ensure_tables()
    conn = get_connection()
    created = []
    deleted = 0
    try:
        cursor = conn.cursor()

        updates = []
        vals = []
        mapping = {
            "mode": plan_updates.get("mode"),
            "stage": plan_updates.get("stage"),
            "topic_mix": json.dumps(plan_updates.get("topic_mix"), ensure_ascii=False)
            if plan_updates.get("topic_mix") is not None else None,
            "weekly_themes": json.dumps(plan_updates.get("weekly_themes"), ensure_ascii=False)
            if plan_updates.get("weekly_themes") is not None else None,
            "milestones": json.dumps(plan_updates.get("milestones"), ensure_ascii=False)
            if plan_updates.get("milestones") is not None else None,
            "monthly_summary": plan_updates.get("monthly_summary"),
            "encouragement": plan_updates.get("encouragement"),
            "monthly_goal": plan_updates.get("monthly_goal"),
            "strategy_pack": json.dumps(plan_updates.get("strategy_pack"), ensure_ascii=False)
            if plan_updates.get("strategy_pack") is not None else None,
        }
        for col, val in mapping.items():
            if val is not None:
                updates.append(f"{col} = %s")
                vals.append(val)
        updates.append("updated_at = NOW()")
        vals.append(plan_id)
        cursor.execute(
            f"UPDATE content_plans SET {', '.join(updates)} WHERE id = %s AND is_deleted = 0 RETURNING *",
            vals,
        )
        plan_row = cursor.fetchone()
        if not plan_row:
            raise ValueError(f"plan not found: {plan_id}")

        cursor.execute(
            """
            DELETE FROM plan_tasks
            WHERE plan_id = %s
              AND COALESCE(status, 'idea') = 'idea'
              AND script_id IS NULL
              AND published_date IS NULL
              AND (performance_data IS NULL OR performance_data = '{}'::jsonb)
            """,
            (plan_id,),
        )
        deleted = cursor.rowcount

        for t in tasks:
            task_id = shortuuid.uuid()[:12]
            cursor.execute(
                """
                INSERT INTO plan_tasks
                    (id, plan_id, week, day_of_week, sort_order,
                     topic_title, topic_category, script_type, content_group,
                     duration_seconds, is_pinned, ai_note, status, planned_date,
                     script_id, topic_id, brief_snapshot, source_refs, assigned_role,
                     assigned_user_id, success_metric)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    task_id, plan_id,
                    t.get("week", 1),
                    t.get("day_of_week"),
                    t.get("sort_order", 0),
                    t["topic_title"],
                    t.get("topic_category", "traffic"),
                    t.get("script_type"),
                    t.get("content_group"),
                    t.get("duration_seconds", 60),
                    t.get("is_pinned", False),
                    t.get("ai_note"),
                    t.get("status", "idea"),
                    t.get("planned_date"),
                    t.get("script_id"),
                    t.get("topic_id"),
                    json.dumps(t.get("brief_snapshot"), ensure_ascii=False) if t.get("brief_snapshot") else None,
                    json.dumps(t.get("source_refs"), ensure_ascii=False) if t.get("source_refs") else None,
                    t.get("assigned_role"),
                    t.get("assigned_user_id"),
                    t.get("success_metric"),
                ),
            )
            created.append(dict(cursor.fetchone()))

        conn.commit()
        cursor.close()
        plan = dict(plan_row)
        _parse_plan_json(plan)
        return {
            "plan": plan,
            "tasks": [_parse_task_json(c) for c in created],
            "deleted_unstarted_count": deleted,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_plan_stats(plan_id: str) -> Dict:
    """统计计划下各状态任务数"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*) AS total_tasks,
                COUNT(*) FILTER (WHERE status = 'published') AS published,
                COUNT(*) FILTER (WHERE status = 'ready_to_publish') AS ready_to_publish,
                COUNT(*) FILTER (WHERE status = 'has_script') AS has_script,
                COUNT(*) FILTER (WHERE status = 'filmed') AS filmed,
                COUNT(*) FILTER (WHERE status = 'idea') AS idea,
                COUNT(*) FILTER (WHERE status = 'skipped') AS skipped
            FROM plan_tasks WHERE plan_id = %s
            """,
            (plan_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        return dict(row) if row else {
            "total_tasks": 0, "published": 0, "has_script": 0,
            "filmed": 0, "ready_to_publish": 0, "idea": 0, "skipped": 0,
        }
    finally:
        conn.close()


def sync_task_by_script_id(script_id: int, new_status: str) -> None:
    """根据 script_id 同步 plan_task 状态（社媒脚本库 联动用）。

    同步只能单调推进，不能把 plan_task 的拍摄/待发/已发状态拉回去。
    """
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if new_status == "published":
            cursor.execute(
                """
                UPDATE plan_tasks
                SET status = 'published',
                    published_date = COALESCE(published_date, CURRENT_DATE),
                    updated_at = NOW()
                WHERE script_id = %s
                  AND COALESCE(status, 'idea') != 'published'
                """,
                (script_id,),
            )
        elif new_status == "filmed":
            cursor.execute(
                """
                UPDATE plan_tasks
                SET status = CASE
                        WHEN COALESCE(status, 'idea') IN ('ready_to_publish', 'published') THEN status
                        ELSE 'filmed'
                    END,
                    updated_at = NOW()
                WHERE script_id = %s
                  AND COALESCE(status, 'idea') IN ('has_script', 'filmed', 'ready_to_publish')
                """,
                (script_id,),
            )
        elif new_status == "has_script":
            cursor.execute(
                """
                UPDATE plan_tasks
                SET status = CASE
                        WHEN COALESCE(status, 'idea') IN ('filmed', 'ready_to_publish', 'published') THEN status
                        ELSE 'has_script'
                    END,
                    updated_at = NOW()
                WHERE script_id = %s
                """,
                (script_id,),
            )
        else:
            logger.info("[Plan] sync_task_by_script_id 跳过非单调状态: %s", new_status)
        conn.commit()
        cursor.close()
    except Exception as e:
        conn.rollback()
        logger.warning(f"[Plan] sync_task_by_script_id 失败: {e}")
    finally:
        conn.close()


def delete_plan(plan_id: str) -> bool:
    """软删除计划"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE content_plans SET is_deleted = 1, updated_at = NOW() WHERE id = %s",
            (plan_id,),
        )
        affected = cursor.rowcount
        conn.commit()
        cursor.close()
        return affected > 0
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


# ========== JSON 解析辅助 ==========


def _parse_plan_json(plan: Dict) -> Dict:
    """解析 plan 中的 JSONB 字段"""
    for field in ("topic_mix", "weekly_themes", "milestones", "strategy_pack"):
        val = plan.get(field)
        if isinstance(val, str):
            try:
                plan[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    return plan


def _parse_task_json(task: Dict) -> Dict:
    """解析 task 中的 JSONB 字段"""
    for field in ("performance_data", "brief_snapshot", "source_refs"):
        val = task.get(field)
        if isinstance(val, str):
            try:
                task[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    return task
