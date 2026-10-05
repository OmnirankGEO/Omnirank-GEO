"""自然迁移率埋点 API · CTO-15.18 PM 干预 D.10

老板 KPI 验收(2026-05-26 周一拍板):
- 新代理(注册 ≤ 30 天)自然迁移率 > 60% = 替代旧版
- 30-60% = 双轨继续
- < 30% = 重审根因

Endpoints:
- POST /api/analytics/event           写入埋点(批量)
- GET  /api/analytics/migration-report 周报告 · admin only

数据存 m3_analytics_events 表(Q19 老板加约束:partition by month 数据量大场景预防)
"""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
import json
import logging
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/analytics", tags=["分析埋点"])
logger = logging.getLogger("GEO-Analytics")


class AnalyticsEvent(BaseModel):
    event: str = Field(..., max_length=64)
    pathname: str = Field(..., max_length=512)
    timestamp: str = Field(..., max_length=64)
    user_id: Optional[int] = None
    metadata: Optional[dict] = None


class BatchEventsRequest(BaseModel):
    events: list[AnalyticsEvent] = Field(..., max_length=50)


def init_analytics_table():
    """初始化 m3_analytics_events 表 · 启动自检调用"""
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS m3_analytics_events (
                    id BIGSERIAL PRIMARY KEY,
                    event TEXT NOT NULL,
                    pathname TEXT,
                    user_id INTEGER,
                    metadata JSONB,
                    is_m3 BOOLEAN DEFAULT FALSE,
                    is_legacy BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_m3_analytics_user_created
                ON m3_analytics_events (user_id, created_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_m3_analytics_event_created
                ON m3_analytics_events (event, created_at DESC)
            """)
            # Q19 老板加约束:partition by month 数据量大场景预防
            # PG 不支持 alter to partition · 单表 + 月份索引 + 后续需要时 DBA 改 partition
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_m3_analytics_month
                ON m3_analytics_events (DATE_TRUNC('month', created_at))
            """)
            conn.commit()
            logger.info("[analytics] m3_analytics_events 表 + 索引初始化完成")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[analytics] 表初始化失败(非阻塞): {e}")


@router.post("/event", summary="批量写入埋点事件")
async def post_analytics_events(req: BatchEventsRequest, request: Request):
    """前端批量上报埋点 · 失败 graceful 不阻断业务"""
    user = getattr(request.state, "user", None)
    user_id_from_token = user.get("user_id") if user else None

    if not req.events:
        return {"success": True, "written": 0}

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        rows = []
        for ev in req.events:
            meta = ev.metadata or {}
            is_m3 = bool(meta.get("is_m3"))
            is_legacy = bool(meta.get("is_legacy"))
            rows.append((
                ev.event,
                ev.pathname,
                user_id_from_token or ev.user_id,
                json.dumps(meta, ensure_ascii=False),
                is_m3,
                is_legacy,
            ))
        # 批量 INSERT
        cur.executemany(
            """
            INSERT INTO m3_analytics_events (event, pathname, user_id, metadata, is_m3, is_legacy)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s)
            """,
            rows,
        )
        conn.commit()
        return {"success": True, "written": len(rows)}
    except Exception as e:
        logger.warning(f"[analytics] 批量写入失败: {e}")
        # graceful · 不抛 HTTP 错(避免影响前端业务)
        return {"success": False, "error": "write_fail"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


@router.get("/migration-report", summary="自然迁移率报告(admin only)")
async def migration_report(
    request: Request,
    days: int = 7,
    signup_within: int = 30,
):
    """新代理(注册 ≤ signup_within 天)在过去 days 天的 M3 vs 旧版首选率

    KPI 验收(2026-05-26 周一拍板):
    - > 60% 替代旧版 · 30-60% 继续 · < 30% 重审

    Args:
        days: 看过去 N 天数据 · 默认 7
        signup_within: 新代理定义 · 注册 ≤ N 天 · 默认 30

    Returns:
        {
          new_users: int,           # 符合条件的新代理数
          users_with_data: int,     # 有埋点数据的人数
          m3_first_users: int,      # 首选 M3 的人数(M3 PV > 旧版 PV)
          migration_rate_pct: float, # 迁移率 % (m3_first_users / users_with_data * 100)
          verdict: str,             # 'replace' / 'continue' / 'revisit'
          breakdown: list[...],     # 每个新代理的 M3 PV / 旧版 PV
        }
    """
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅 admin 可看")

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 1. 找新代理(注册 ≤ signup_within 天 · 排除 admin)
        # [CTO-15.23 2026-05-06 SQL 4 维度核验累犯 #4+5 修复]
        # 累犯 4:is_admin 是 Python 计算字段(db/auth_db.py:401) → 改 NOT EXISTS user_roles 子查询
        # 累犯 5:Python % operator 跟 psycopg2 %s 占位符混用 → 全用 f-string 展开 int(变量)
        #         (Deploy-CTO 教学:不要混用 · 也是 SQL 注入要避免的写法 · 但 int 转换安全)
        cur.execute(
            f"""
            SELECT u.id, u.username, u.display_name, u.created_at
            FROM users u
            WHERE u.created_at >= NOW() - INTERVAL '{int(signup_within)} days'
              AND NOT EXISTS (
                  SELECT 1 FROM user_roles ur
                  JOIN roles r ON r.id = ur.role_id
                  WHERE ur.user_id = u.id AND r.name = 'admin'
              )
            ORDER BY u.created_at DESC
            """
        )
        new_users = cur.fetchall() or []

        if not new_users:
            return {
                "success": True,
                "new_users": 0,
                "users_with_data": 0,
                "m3_first_users": 0,
                "migration_rate_pct": 0.0,
                "verdict": "no_data",
                "breakdown": [],
            }

        new_user_ids = [u["id"] for u in new_users]
        placeholders = ",".join(["%s"] * len(new_user_ids))

        # 2. 统计 M3 vs 旧版 PV(过去 days 天)· days 用 f-string 展开 · IN 用 %s 列表
        cur.execute(
            f"""
            SELECT user_id,
                   SUM(CASE WHEN is_m3 THEN 1 ELSE 0 END) AS m3_pv,
                   SUM(CASE WHEN is_legacy THEN 1 ELSE 0 END) AS legacy_pv
            FROM m3_analytics_events
            WHERE user_id IN ({placeholders})
              AND created_at >= NOW() - INTERVAL '{int(days)} days'
              AND event = 'page_view'
            GROUP BY user_id
            """,
            new_user_ids,
        )
        pv_rows = cur.fetchall() or []
        pv_by_user = {r["user_id"]: (int(r["m3_pv"] or 0), int(r["legacy_pv"] or 0)) for r in pv_rows}

        users_with_data = len(pv_by_user)
        m3_first_users = sum(1 for m3, leg in pv_by_user.values() if m3 > leg)
        migration_rate = (m3_first_users / users_with_data * 100) if users_with_data > 0 else 0.0

        # 老板 KPI verdict
        if migration_rate > 60:
            verdict = "replace"
        elif migration_rate >= 30:
            verdict = "continue"
        else:
            verdict = "revisit"

        breakdown = []
        for u in new_users:
            uid = u["id"]
            m3_pv, legacy_pv = pv_by_user.get(uid, (0, 0))
            breakdown.append({
                "user_id": uid,
                "username": u.get("username"),
                "display_name": u.get("display_name"),
                "signup_at": u["created_at"].isoformat() if u.get("created_at") else None,
                "m3_pv": m3_pv,
                "legacy_pv": legacy_pv,
                "m3_preferred": m3_pv > legacy_pv,
            })

        return {
            "success": True,
            "days": days,
            "signup_within": signup_within,
            "new_users": len(new_users),
            "users_with_data": users_with_data,
            "m3_first_users": m3_first_users,
            "migration_rate_pct": round(migration_rate, 1),
            "verdict": verdict,
            "verdict_threshold": {"replace": 60, "continue": 30, "revisit": 0},
            "breakdown": breakdown,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[analytics/migration-report] 失败: {e}")
        raise HTTPException(status_code=500, detail="报表失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass
