"""Agent Task-Based Session 管理

每次对话保存到 agent_sessions 表。
支持任务摘要压缩 + 历史上下文加载。
"""
import json
import logging
import shortuuid
from datetime import datetime
from typing import Optional, List, Dict

logger = logging.getLogger("GEO-AgentSession")


def _get_connection():
    from db.connection import get_connection
    return get_connection()


# ========== 建表 ==========

_tables_ensured = False


def ensure_tables():
    """确保 agent_sessions 表存在"""
    global _tables_ensured
    if _tables_ensured:
        return
    try:
        conn = _get_connection()
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id VARCHAR(32) PRIMARY KEY,
                user_id INTEGER NOT NULL,
                profile_id VARCHAR(22),
                title VARCHAR(200) DEFAULT '新对话',
                messages JSONB DEFAULT '[]',
                task_summaries JSONB DEFAULT '[]',
                agent_type VARCHAR(20) DEFAULT 'social',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_sessions_user
            ON agent_sessions(user_id, updated_at DESC)
        """)
        # 意图蓄水池：零散交互沉淀（关键词询价、竞品分析等）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS draft_workspace (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                brand_id INTEGER NOT NULL,
                item_type VARCHAR(30) NOT NULL,
                item_key VARCHAR(200) NOT NULL,
                item_data JSONB DEFAULT '{}',
                source VARCHAR(20) DEFAULT 'ai_chat',
                created_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP,
                UNIQUE(user_id, brand_id, item_type, item_key)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_draft_workspace_user_brand
            ON draft_workspace(user_id, brand_id, item_type)
        """)
        cursor.close()
        conn.close()
        _tables_ensured = True
        logger.info("[AgentSession] agent_sessions + draft_workspace 表已就绪")
    except Exception as e:
        logger.warning(f"[AgentSession] 建表失败: {e}")


# ========== CRUD ==========


def get_or_create_session(session_id: str, user_id: int,
                          profile_id: str = None) -> Dict:
    """获取现有会话或创建新会话

    ⚠️ 安全修复 2026-04-17 (P0-M): 原先 SELECT 只 WHERE id=%s，任何用户知道/猜到
       别人的 session_id 就能读写他历史。现加 user_id 过滤 — 即便 session_id 冲突，
       返回的必须是当前用户所有。冲突时 insert 会因 PK 失败，故同时检测 id 已存在
       但 user_id 不匹配 → 生成带 user 前缀的隔离 id。
    """
    ensure_tables()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        # 1. 严格按 (id, user_id) 查询，跨用户命中直接视为"无匹配"
        cursor.execute(
            "SELECT * FROM agent_sessions WHERE id = %s AND user_id = %s",
            (session_id, user_id),
        )
        row = cursor.fetchone()
        if row:
            result = dict(row)
            for field in ("messages", "task_summaries"):
                if isinstance(result.get(field), str):
                    try:
                        result[field] = json.loads(result[field])
                    except (ValueError, TypeError):
                        result[field] = []
            cursor.close()
            return result

        # 2. 本用户没有该 session。先看 session_id 是否被别人占用
        cursor.execute(
            "SELECT user_id FROM agent_sessions WHERE id = %s",
            (session_id,),
        )
        conflict = cursor.fetchone()
        if conflict:
            # 跨用户冲突 → 生成带用户前缀的隔离 id，防止写入时 PK 冲突 + 互串数据
            logger.warning(
                f"[AgentSession] session_id 冲突 session_id={session_id} "
                f"owner={conflict['user_id']} requester={user_id}，生成隔离 id"
            )
            session_id = f"u{user_id}_{session_id}"

        # 3. 创建新会话
        cursor.execute(
            """INSERT INTO agent_sessions (id, user_id, profile_id)
               VALUES (%s, %s, %s) RETURNING *""",
            (session_id, user_id, profile_id)
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return dict(row) if row else {"id": session_id, "user_id": user_id,
                                       "messages": [], "task_summaries": []}
    except Exception as e:
        conn.rollback()
        logger.error(f"[AgentSession] get_or_create 失败: {e}")
        return {"id": session_id, "user_id": user_id,
                "messages": [], "task_summaries": []}
    finally:
        conn.close()


def save_turn(session_id: str, role: str, content: str,
              metadata: dict = None) -> bool:
    """保存一轮对话"""
    ensure_tables()
    turn = {
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat(),
    }
    if metadata:
        turn["metadata"] = metadata

    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE agent_sessions
               SET messages = messages || %s::jsonb,
                   updated_at = NOW()
               WHERE id = %s""",
            (json.dumps([turn], ensure_ascii=False), session_id)
        )
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"[AgentSession] save_turn 失败: {e}")
        return False
    finally:
        conn.close()


def pin_message(session_id: str, message_index: int, pinned: bool = True) -> bool:
    """标记/取消标记消息为重要（pinned 消息不会被上下文压缩丢弃）"""
    ensure_tables()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT messages FROM agent_sessions WHERE id = %s", (session_id,))
        row = cursor.fetchone()
        if not row:
            return False

        messages = row["messages"]
        if isinstance(messages, str):
            messages = json.loads(messages)

        if 0 <= message_index < len(messages):
            messages[message_index]["pinned"] = pinned
            cursor.execute(
                "UPDATE agent_sessions SET messages = %s::jsonb, updated_at = NOW() WHERE id = %s",
                (json.dumps(messages, ensure_ascii=False), session_id)
            )
            conn.commit()
            return True
        return False
    except Exception as e:
        conn.rollback()
        logger.error(f"[AgentSession] pin_message 失败: {e}")
        return False
    finally:
        conn.close()


def get_recent_context(session_id: str, max_turns: int = 15) -> str:
    """获取最近对话上下文（文本格式，注入到 Agent prompt）
    pinned 消息始终保留，不受 max_turns 限制。
    """
    ensure_tables()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT messages FROM agent_sessions WHERE id = %s", (session_id,))
        row = cursor.fetchone()
        cursor.close()
        if not row:
            return ""

        messages = row["messages"]
        if isinstance(messages, str):
            messages = json.loads(messages)

        # 分离 pinned 和最近消息，合并去重
        pinned = [m for m in messages if m.get("pinned")]
        recent = messages[-max_turns:]

        seen_timestamps = set()
        combined = []
        for m in pinned:
            ts = m.get("timestamp", "")
            if ts not in seen_timestamps:
                combined.append(m)
                seen_timestamps.add(ts)
        for m in recent:
            ts = m.get("timestamp", "")
            if ts not in seen_timestamps:
                combined.append(m)
                seen_timestamps.add(ts)

        combined.sort(key=lambda m: m.get("timestamp", ""))

        lines = []
        for m in combined:
            role = "用户" if m.get("role") == "user" else "助手"
            prefix = "📌 " if m.get("pinned") else ""
            content = m.get("content", "")
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"{prefix}{role}: {content}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"[AgentSession] get_recent_context 失败: {e}")
        return ""
    finally:
        conn.close()


def compress_task(session_id: str, summary: str) -> bool:
    """把当前任务压缩为一句摘要，追加到 task_summaries"""
    ensure_tables()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        entry = json.dumps([{
            "summary": summary,
            "timestamp": datetime.now().isoformat(),
        }], ensure_ascii=False)
        cursor.execute(
            """UPDATE agent_sessions
               SET task_summaries = task_summaries || %s::jsonb,
                   updated_at = NOW()
               WHERE id = %s""",
            (entry, session_id)
        )
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"[AgentSession] compress_task 失败: {e}")
        return False
    finally:
        conn.close()


def get_task_summaries(session_id: str, max_count: int = 10) -> List[str]:
    """获取历史任务摘要列表"""
    ensure_tables()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT task_summaries FROM agent_sessions WHERE id = %s",
                       (session_id,))
        row = cursor.fetchone()
        cursor.close()
        if not row:
            return []

        summaries = row["task_summaries"]
        if isinstance(summaries, str):
            summaries = json.loads(summaries)

        return [s.get("summary", "") for s in summaries[-max_count:]]
    except Exception as e:
        logger.error(f"[AgentSession] get_task_summaries 失败: {e}")
        return []
    finally:
        conn.close()


# ========== 智能记忆管理 ==========

# 关键决策关键词 — 包含这些词的消息自动 pin
_DECISION_KEYWORDS = [
    "确认", "同意", "就这个", "选这个", "可以", "行",
    "不要", "取消", "换一个", "不对", "搞错",
    "积分", "价格", "报价", "多少钱",
    "[选择]", "[确认]", "[取消]",
]


def auto_pin_decisions(session_id: str) -> int:
    """扫描最近消息，自动 pin 包含关键决策的消息。返回新 pin 的数量。"""
    ensure_tables()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT messages FROM agent_sessions WHERE id = %s", (session_id,))
        row = cursor.fetchone()
        if not row:
            return 0

        messages = row["messages"]
        if isinstance(messages, str):
            messages = json.loads(messages)

        pinned_count = 0
        changed = False
        # 只扫描最近 20 条（避免大量历史扫描）
        start = max(0, len(messages) - 20)
        for i in range(start, len(messages)):
            m = messages[i]
            if m.get("pinned"):
                continue
            content = m.get("content", "")
            if any(kw in content for kw in _DECISION_KEYWORDS):
                m["pinned"] = True
                pinned_count += 1
                changed = True

        if changed:
            cursor.execute(
                "UPDATE agent_sessions SET messages = %s::jsonb WHERE id = %s",
                (json.dumps(messages, ensure_ascii=False), session_id)
            )
            conn.commit()
        cursor.close()
        return pinned_count
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"[AgentSession] auto_pin_decisions 失败: {e}")
        return 0
    finally:
        conn.close()


def auto_compress_if_needed(session_id: str, threshold: int = 20) -> bool:
    """当消息数超过 threshold 时，把最老的一半压缩为摘要。
    保留 pinned 消息不压缩。返回是否执行了压缩。
    """
    ensure_tables()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT messages, task_summaries FROM agent_sessions WHERE id = %s",
                       (session_id,))
        row = cursor.fetchone()
        if not row:
            return False

        messages = row["messages"]
        if isinstance(messages, str):
            messages = json.loads(messages)

        if len(messages) < threshold:
            return False

        summaries = row["task_summaries"]
        if isinstance(summaries, str):
            try:
                summaries = json.loads(summaries)
            except (json.JSONDecodeError, TypeError):
                summaries = []

        # 分离：前半部分压缩，后半部分保留
        split_at = len(messages) // 2
        to_compress = messages[:split_at]
        to_keep = messages[split_at:]

        # 从待压缩的消息中提取摘要（不用 LLM，用规则提取关键信息）
        summary_parts = []
        for m in to_compress:
            if m.get("pinned"):
                # pinned 消息移到保留区，不压缩
                to_keep.insert(0, m)
                continue
            role = "用户" if m.get("role") == "user" else "AI"
            content = m.get("content", "").strip()
            if not content:
                continue
            # 只保留用户消息和 AI 的关键回复（含数字/确认的）
            if role == "用户" or any(kw in content for kw in ["积分", "¥", "价格", "已", "成功", "失败"]):
                short = content[:80] + "..." if len(content) > 80 else content
                summary_parts.append(f"{role}: {short}")

        if summary_parts:
            summary_text = " | ".join(summary_parts[-8:])  # 最多保留 8 条
            summaries.append({
                "summary": summary_text,
                "compressed_count": len(to_compress),
                "timestamp": datetime.now().isoformat(),
            })

        # 重新排序保留的消息
        to_keep.sort(key=lambda m: m.get("timestamp", ""))

        cursor.execute(
            """UPDATE agent_sessions
               SET messages = %s::jsonb, task_summaries = %s::jsonb, updated_at = NOW()
               WHERE id = %s""",
            (json.dumps(to_keep, ensure_ascii=False),
             json.dumps(summaries, ensure_ascii=False),
             session_id)
        )
        conn.commit()
        cursor.close()
        logger.info(f"[AgentSession] {session_id} 压缩了 {len(to_compress)} 条消息")
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"[AgentSession] auto_compress 失败: {e}")
        return False
    finally:
        conn.close()


# ========== 意图蓄水池 (draft_workspace) ==========


def save_draft(user_id: int, brand_id: int, item_type: str, item_key: str,
               item_data: dict, source: str = "ai_chat", expires_days: int = 7) -> bool:
    """保存零散交互数据到草稿区。UPSERT — 相同 key 会更新。"""
    ensure_tables()
    from datetime import timedelta
    expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO draft_workspace (user_id, brand_id, item_type, item_key, item_data, source, expires_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (user_id, brand_id, item_type, item_key)
            DO UPDATE SET item_data = EXCLUDED.item_data, source = EXCLUDED.source,
                          expires_at = EXCLUDED.expires_at, created_at = NOW()
        """, (user_id, brand_id, item_type, item_key,
              json.dumps(item_data, ensure_ascii=False), source, expires_at))
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"[DraftWorkspace] save_draft 失败: {e}")
        return False
    finally:
        conn.close()


def get_drafts(user_id: int, brand_id: int, item_type: str = None,
               include_expired: bool = False) -> List[Dict]:
    """获取草稿区数据。默认只返回未过期的。"""
    ensure_tables()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        sql = "SELECT * FROM draft_workspace WHERE user_id = %s AND brand_id = %s"
        params: list = [user_id, brand_id]
        if item_type:
            sql += " AND item_type = %s"
            params.append(item_type)
        if not include_expired:
            sql += " AND (expires_at IS NULL OR expires_at > NOW())"
        sql += " ORDER BY created_at DESC"
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("item_data"), str):
                try:
                    d["item_data"] = json.loads(d["item_data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result
    except Exception as e:
        logger.error(f"[DraftWorkspace] get_drafts 失败: {e}")
        return []
    finally:
        conn.close()


def get_draft(user_id: int, brand_id: int, item_type: str, item_key: str) -> Optional[Dict]:
    """获取单条草稿（未过期）"""
    ensure_tables()
    conn = _get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM draft_workspace
            WHERE user_id = %s AND brand_id = %s AND item_type = %s AND item_key = %s
              AND (expires_at IS NULL OR expires_at > NOW())
        """, (user_id, brand_id, item_type, item_key))
        row = cursor.fetchone()
        cursor.close()
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("item_data"), str):
            try:
                d["item_data"] = json.loads(d["item_data"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d
    except Exception as e:
        logger.error(f"[DraftWorkspace] get_draft 失败: {e}")
        return None
    finally:
        conn.close()
