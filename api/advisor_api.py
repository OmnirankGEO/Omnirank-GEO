"""
社媒操盘手 v3.0 - 顾问 API 路由
"""

import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from services.advisor_identity import is_public_identity_enabled, project_advisor_row

router = APIRouter(prefix="/api/advisors", tags=["顾问系统"])


def get_connection():
    """获取数据库连接"""
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


# ========== Pydantic Models ==========

class AdvisorCombination(BaseModel):
    """顾问组合配置"""
    main_advisor_id: str
    sub_advisor_id: Optional[str] = None  # None表示单顾问模式


def _require_admin(request: Request):
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    return user


def _is_admin_request(request: Request | None) -> bool:
    user = getattr(request.state, "user", None) if request else None
    return bool(user and user.get("is_admin"))


# [WO_285] 请求路径上的 DDL ensure 两条规矩(以后新增同类 ensure 也必须照做):
#   ① 进程级只跑一次:成功才置位,失败下次调用再试 —— 否则每个请求都去抢一次 ACCESS EXCLUSIVE;
#   ② 跑 DDL 的那个事务先 `SET LOCAL lock_timeout`:发车备份的 pg_dump 对每张表持 ACCESS SHARE,
#      `ALTER TABLE … ADD COLUMN IF NOT EXISTS` 即使列早已存在也要排在它后面(本机实测复现);lock_timeout=0
#      就一直等,同步 psycopg2 调用卡住事件循环,WORKERS=1 全应用停(0913AA 上线 8 条 504,WO_284 取证)。
#      拿不到锁 ⇒ 回滚、记 warning、照常往下走:列在生产早已存在,这一次没跑成无害。
#   另在 server 启动钩子里预跑一次(prewarm_advisor_schema),首个请求不再付这笔。
#   判据:tests/advisor_request_path_ddl_2026_09_23。
_DDL_LOCK_TIMEOUT = "2s"
_IDENTITY_SCHEMA_READY = False
_OWNERSHIP_SCHEMA_READY = False


def _run_ddl_with_lock_timeout(conn, statements: list, label: str) -> bool:
    """同一个事务里先 SET LOCAL lock_timeout,再逐条跑 DDL;任何失败 ⇒ 回滚、记 warning、返回 False,不抛。"""
    # SET LOCAL 只在事务块里生效;连接池给的连接一律 autocommit=False,这里兜住万一被人设成 True 的连接
    restore_autocommit = bool(getattr(conn, "autocommit", False))
    if restore_autocommit:
        conn.autocommit = False
    cursor = conn.cursor()
    try:
        cursor.execute(f"SET LOCAL lock_timeout = '{_DDL_LOCK_TIMEOUT}'")
        for sql in statements:
            cursor.execute(sql)
        conn.commit()
        return True
    except Exception as exc:  # 典型:psycopg2.errors.LockNotAvailable(发车 pg_dump 窗口)
        try:
            conn.rollback()
        except Exception:
            pass
        logging.getLogger("GEO-Advisor").warning(
            f"[WO_285] {label} 的 DDL 这次没跑成({type(exc).__name__}: {exc}),照常继续;下次调用再试")
        return False
    finally:
        if restore_autocommit:
            conn.autocommit = True


def _ensure_advisor_identity_schema(conn) -> bool:
    """Idempotently add public/source identity columns —— 进程级只跑一次(WO_285)。"""
    global _IDENTITY_SCHEMA_READY
    if _IDENTITY_SCHEMA_READY:
        return True
    statements = [
        f"ALTER TABLE advisors ADD COLUMN IF NOT EXISTS {column} {column_type}"
        for column, column_type in [
            ("public_name", "TEXT"),
            ("source_name", "TEXT"),
            ("identity_status", "TEXT DEFAULT 'draft'"),
            ("identity_notes", "TEXT DEFAULT ''"),
            ("identity_updated_at", "TIMESTAMP"),
        ]
    ]
    if _run_ddl_with_lock_timeout(conn, statements, "advisors 身份列"):
        _IDENTITY_SCHEMA_READY = True
    return _IDENTITY_SCHEMA_READY


def _ensure_advisor_conversation_ownership_schema(conn) -> bool:
    """[ADVISOR-OWNERSHIP] 幂等补 owner_user_id 归属列 + 索引。

    additive:只加可空列与索引,不动既有列、不加 NOT NULL、不加 CHECK。
    约束等回填完再单独议 —— 记忆里「往 CHECK 加允许值 ≠ additive」那条的对应做法。

    列名用 owner_user_id 而不是 user_id:与 brands.owner_user_id 同名同义,
    且避免与 JWT 载荷里的 user_id 混淆。

    进程级只跑一次:聊天是热路径,不能每条消息都往库里打两条 DDL。
    [WO_285] DDL 事务先 SET LOCAL lock_timeout;拿不到锁不再把异常抛给聊天 handler(记 warning,下次再试)。
    """
    global _OWNERSHIP_SCHEMA_READY
    if _OWNERSHIP_SCHEMA_READY:
        return True
    statements = [
        "ALTER TABLE advisor_conversations ADD COLUMN IF NOT EXISTS owner_user_id INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_advisor_conv_owner "
        "ON advisor_conversations(owner_user_id)",
    ]
    if _run_ddl_with_lock_timeout(conn, statements, "advisor_conversations 归属列"):
        _OWNERSHIP_SCHEMA_READY = True
    return _OWNERSHIP_SCHEMA_READY


def prewarm_advisor_schema() -> dict:
    """[WO_285] server 启动钩子调用:预跑两处 ensure,首个请求不再付 DDL。
    拿不到锁 ⇒ 记 warning,留给请求路径兜底(同样只跑一次、最多等 lock_timeout)。"""
    conn = get_connection()
    try:
        return {
            "identity": _ensure_advisor_identity_schema(conn),
            "ownership": _ensure_advisor_conversation_ownership_schema(conn),
        }
    finally:
        conn.close()


def _safe_json_list(val):
    import json as _json

    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = _json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


# ========== API 路由 ==========

@router.get("", summary="获取顾问列表")
async def list_advisors(request: Request, role: str = None, industry: str = None, include_empty: bool = False, admin: bool = False):
    """获取所有可用顾问，支持按角色和行业筛选"""
    from pathlib import Path

    conn = get_connection()
    try:
        _ensure_advisor_identity_schema(conn)
        cursor = conn.cursor()
        expose_admin_fields = bool(admin and _is_admin_request(request))
        public_identity_enabled = is_public_identity_enabled()

        # 动态构建查询
        sql = """
            SELECT a.id, a.name, a.avatar, a.description, a.base_prompt, a.specialty,
                   a.knowledge_base_path, a.last_kb_update, a.api_provider, a.model_name,
                   a.role, a.industries, a.tags, a.credentials, a.greeting, a.quick_questions, a.knowledge_count,
                   a.public_name, a.source_name, a.identity_status, a.identity_notes, a.identity_updated_at,
                   COALESCE(k.active_chunks, 0) AS active_chunks,
                   COALESCE(k.vectorized_chunks, 0) AS vectorized_chunks
            FROM advisors a
            LEFT JOIN (
                SELECT advisor_id,
                       COUNT(*) FILTER (WHERE is_active = true) AS active_chunks,
                       COUNT(*) FILTER (WHERE is_active = true AND embedding IS NOT NULL) AS vectorized_chunks
                FROM knowledge_chunks
                GROUP BY advisor_id
            ) k ON k.advisor_id = a.id
            WHERE COALESCE(a.is_active, 1) = 1
        """
        params = []
        if not expose_admin_fields:
            sql += " AND COALESCE(a.identity_status, 'draft') <> 'blocked'"
        if role:
            sql += " AND a.role = %s"
            params.append(role)
        if industry:
            sql += " AND a.industries::text LIKE %s"
            params.append(f"%{industry}%")
        if not include_empty:
            sql += " AND COALESCE(k.active_chunks, 0) > 0"
        sql += " ORDER BY a.role, a.name"

        cursor.execute(sql, params)
        rows = cursor.fetchall()

        # 知识库根目录
        kb_root = Path("data/knowledge/roles/advisors")

        advisors = []
        for row in rows:
            advisor_id = row["id"]

            # 统计知识库文档数量
            advisor_kb_path = kb_root / advisor_id
            document_count = 0
            if advisor_kb_path.exists():
                for f in advisor_kb_path.iterdir():
                    if f.is_file() and not f.name.endswith('_cleaned.json'):
                        document_count += 1

            advisor_payload = {
                "id": advisor_id,
                "name": row["name"],
                "public_name": row.get("public_name"),
                "source_name": row.get("source_name"),
                "identity_status": row.get("identity_status") or "draft",
                "identity_notes": row.get("identity_notes") or "",
                "identity_updated_at": row.get("identity_updated_at"),
                "avatar": row["avatar"],
                "description": row["description"],
                "base_prompt": row["base_prompt"],
                "specialty": row.get("specialty") or "",
                "knowledge_base_path": row.get("knowledge_base_path"),
                "last_kb_update": row.get("last_kb_update"),
                "api_provider": row.get("api_provider"),
                "model_name": row.get("model_name"),
                "role": row.get("role") or "expert",
                "industries": _safe_json_list(row.get("industries")),
                "tags": _safe_json_list(row.get("tags")),
                "credentials": row.get("credentials") or "",
                "greeting": row.get("greeting") or "",
                "quick_questions": _safe_json_list(row.get("quick_questions")),
                "knowledge_count": row.get("knowledge_count", 0) or row.get("active_chunks", 0),
                "active_chunks": row.get("active_chunks", 0),
                "vectorized_chunks": row.get("vectorized_chunks", 0),
                "document_count": document_count,
            }
            advisors.append(project_advisor_row(
                advisor_payload,
                is_admin=expose_admin_fields,
                public_identity_enabled=public_identity_enabled,
            ))

        return {
            "success": True,
            "count": len(advisors),
            "advisors": advisors
        }
    finally:
        conn.close()


# ========== 对话记忆 API ==========

import uuid
from datetime import datetime


class ChatRequest(BaseModel):
    """对话请求"""
    message: str
    conversation_id: Optional[str] = None  # 不传则新建会话
    context: Optional[str] = None  # 附件内容
    brand_id: Optional[int] = None  # 当前服务客户/品牌
    profile_id: Optional[str] = None  # 当前客户画像档案


class ConversationResponse(BaseModel):
    """对话会话"""
    conversation_id: str
    advisor_id: str
    title: Optional[str]
    message_count: int
    created_at: str
    updated_at: str


# ========== 默认操盘手配置（管理员） ==========

_DEFAULT_WRITER_KEY = "default_writer_id"
_DEFAULT_WRITER_FALLBACK = "xingyi"


@router.get("/config/default-writer", summary="获取默认AI操盘手")
async def get_default_writer():
    """获取当前默认AI操盘手ID"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("SELECT value FROM system_config WHERE key = %s", (_DEFAULT_WRITER_KEY,))
        row = cursor.fetchone()
        conn.commit()
        writer_id = row["value"] if row else _DEFAULT_WRITER_FALLBACK
        return {"success": True, "default_writer_id": writer_id}
    finally:
        conn.close()


class SetDefaultWriterRequest(BaseModel):
    writer_id: str


@router.put("/config/default-writer", summary="设置默认AI操盘手（管理员）")
async def set_default_writer(data: SetDefaultWriterRequest, request: Request):
    """管理员设置默认AI操盘手"""
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("""
            INSERT INTO system_config (key, value, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
        """, (_DEFAULT_WRITER_KEY, data.writer_id))
        conn.commit()
        return {"success": True, "default_writer_id": data.writer_id}
    finally:
        conn.close()
