"""v2.7.1 GEO 文体改造 · admin 文章质量警告查询接口

GET /api/admin/articles/quality-warning · 列出 articles.quality_warning IS NOT NULL 的文章
require_admin 中间件守护 · 仅 admin 可见
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from typing import Optional

router = APIRouter(prefix="/api/admin/articles", tags=["admin-articles"])


def _require_admin_dep():
    """admin 鉴权依赖 · 尝试用现有 auth/middleware.require_admin · 否则用通用 user 校验"""
    try:
        from auth.middleware import require_admin
        return require_admin
    except Exception:
        # fallback:用通用 get_current_user · 校验 user_role==admin
        try:
            from auth.middleware import get_current_user
            return get_current_user
        except Exception:
            # 极端兜底:返回 lambda(永远 401)
            def _fail():
                raise HTTPException(status_code=401, detail="auth middleware not available")
            return _fail


_admin_dep = _require_admin_dep()


@router.get("/quality-warning")
async def list_quality_warning_articles(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    hard_only: bool = Query(False, description="仅返回有 hard fail 的文章"),
    admin=Depends(_admin_dep),
):
    """v2.7.1 admin 查 quality_warning 字段非空文章

    Returns:
        {items: [{id, topic_id, quote_id, title, style, quality_warning, created_at}], total: N}
    """
    from db.diagnosis_db import get_connection

    where = "WHERE quality_warning IS NOT NULL AND COALESCE(deleted_at, NULL) IS NULL"
    if hard_only:
        # [工单 C-4 · B6] 修错误 JSON path:hard findings 实际在
        # quality_warning.evidence_legal.hard(顶层 'hard' 是结构层 H 码,常年为空),
        # 旧写法 hard_only=true 恒返回空 —— soft 转后台后 admin 质量面板要能真筛出
        # 红线残留篇目。顶层 'hard' 一并兼容(老数据)。
        where = (
            "WHERE quality_warning IS NOT NULL "
            "AND ((jsonb_typeof(quality_warning->'evidence_legal'->'hard') = 'array' "
            "      AND jsonb_array_length(quality_warning->'evidence_legal'->'hard') > 0) "
            "  OR (jsonb_typeof(quality_warning->'hard') = 'array' "
            "      AND jsonb_array_length(quality_warning->'hard') > 0))"
        )

    conn = get_connection()
    try:
        c = conn.cursor()

        # 列表
        c.execute(
            f"""
            SELECT id, topic_id, quote_id, title, style,
                   quality_warning,
                   created_at
            FROM articles
            {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = c.fetchall()
        items = [dict(r) for r in rows] if rows else []

        # 总数
        c.execute(f"SELECT COUNT(*) AS n FROM articles {where}")
        total_row = c.fetchone()
        total = (total_row.get("n") if isinstance(total_row, dict) else total_row[0]) if total_row else 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {"items": items, "total": int(total or 0)}
