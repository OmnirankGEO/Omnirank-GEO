"""
CTO-15.23 2026-05-09 · Admin LLM 成本监控 API
6 endpoint 给老板 admin dashboard 看 LLM 调用全量(对账 Moonshot/火山/阿里云控制台)

数据源:llm_call_log 表 · 由 tools/llm_call_tracker.py 实时写入
鉴权:仅 admin role 可访问
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from services.api_costs import (
    get_api_cost_by_caller,
    get_api_cost_by_platform,
    get_api_cost_summary,
    get_api_cost_timeline,
)

logger = logging.getLogger("GEO-Admin-LLMCost")

router = APIRouter(prefix="/api/admin/llm-cost", tags=["管理员·LLM成本"])


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _date_range(days: int) -> tuple:
    """返回 (since, prev_since, prev_until) · 同比上周用"""
    until = datetime.utcnow()
    since = until - timedelta(days=days)
    prev_since = since - timedelta(days=days)
    prev_until = since
    return since, prev_since, prev_until


def _query_db(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """sync 查询 · async caller 走 asyncio.to_thread 包"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _query_cost(fn):
    """Run a services.api_costs query with one cursor."""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        return fn(cur)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 1. summary · 总调用量 / 成本 / 异常 + 同比上周 delta
# ============================================================

@router.get("/summary")
async def llm_cost_summary(request: Request, days: int = 7):
    """总调用量 · 总成本 · 异常调用数 · 同比上周 delta

    Returns:
        {
          "days": 7,
          "current": { "total_calls": N, "total_cost": ¥X, "avg_cost": ¥Y,
                        "failed_calls": M, "success_rate": 99.5 },
          "previous": { ... },
          "delta": { "calls": +15%, "cost": +8% }
        }
    """
    import asyncio as _asyncio
    _require_admin(request)
    if days < 1 or days > 90:
        raise HTTPException(400, "days 必须在 [1, 90]")

    since, prev_since, prev_until = _date_range(days)

    def _do():
        return _query_cost(
            lambda cur: (
                get_api_cost_summary(cur, since, limit=0),
                get_api_cost_summary(cur, prev_since, until=prev_until, limit=0),
            )
        )

    cur, prev = await _asyncio.to_thread(_do)
    cur_calls = int(cur.get("total_calls") or 0)
    cur_cost = float(cur.get("total_cost") or 0)
    prev_calls = int(prev.get("total_calls") or 0)
    prev_cost = float(prev.get("total_cost") or 0)
    success_rate = (
        round(100.0 * (cur_calls - int(cur.get("failed_calls") or 0)) / cur_calls, 2)
        if cur_calls > 0 else None
    )

    def _pct(cur_v: float, prev_v: float) -> Optional[float]:
        if prev_v == 0:
            return None
        return round(100.0 * (cur_v - prev_v) / prev_v, 1)

    # 2026-05-22 老板拍板:发布外采(mhz 媒体)纳入总成本 · 用 1x 真实价(用户扣 1.5x markup 不计)
    # 数据源:mhz_publish_order_items.cost_yuan(真实付给媒体平台的钱 = 1x)
    # 监测每天大量自动跑 LLM · 已在 llm_call_log 表(走 PRICING_TABLE) · 此处 LLM 总额已含
    def _do_external():
        try:
            from db.meijiehezi_db import get_publish_external_cost_statistics
            cur_pub = get_publish_external_cost_statistics(days=days)
            prev_pub = get_publish_external_cost_statistics(days=days * 2)
            # prev period = (now - 2d) 到 (now - d) · 取 days*2 减去 days · 简化估算
            prev_total = float(prev_pub.get("total_external_cost_yuan") or 0)
            cur_total = float(cur_pub.get("total_external_cost_yuan") or 0)
            prev_only = max(prev_total - cur_total, 0)
            return cur_pub, {"total_external_cost_yuan": prev_only}
        except Exception as e:
            logger.warning(f"[summary] 发布外采统计失败: {e}")
            return ({"total_external_cost_yuan": 0, "total_orders": 0, "total_items": 0}, {"total_external_cost_yuan": 0})

    ext_cur, ext_prev = await _asyncio.to_thread(_do_external)
    ext_cur_cost = float(ext_cur.get("total_external_cost_yuan") or 0)
    ext_prev_cost = float(ext_prev.get("total_external_cost_yuan") or 0)
    grand_cur = round(cur_cost + ext_cur_cost, 4)
    grand_prev = round(prev_cost + ext_prev_cost, 4)

    return {
        "days": days,
        "current": {
            "total_calls": cur_calls,
            "total_cost": round(cur_cost, 4),  # LLM-only(对账控制台用)
            "avg_cost": round(float(cur.get("avg_cost") or 0), 6),
            "failed_calls": int(cur.get("failed_calls") or 0),
            "success_rate": success_rate,
            "avg_duration_ms": int(float(cur.get("avg_duration_ms") or 0)),
            # 2026-05-22 新增:发布外采 + 总成本(LLM+发布)
            "external_publish_cost": round(ext_cur_cost, 4),
            "external_publish_orders": int(ext_cur.get("total_orders") or 0),
            "external_publish_items": int(ext_cur.get("total_items") or 0),
            "grand_total_cost": grand_cur,
        },
        "previous": {
            "total_calls": prev_calls,
            "total_cost": round(prev_cost, 4),
            "external_publish_cost": round(ext_prev_cost, 4),
            "grand_total_cost": grand_prev,
        },
        "delta": {
            "calls_pct": _pct(cur_calls, prev_calls),
            "cost_pct": _pct(cur_cost, prev_cost),
            "grand_total_pct": _pct(grand_cur, grand_prev),
        },
    }


# ============================================================
# 2. by-caller · 按调用源分组(monitoring/autofill/竞品/写文章/...)
# ============================================================

@router.get("/by-caller")
async def llm_cost_by_caller(request: Request, days: int = 7):
    """按 caller 分组(monitoring/autofill/竞品/写文章/advisor/fallback_chain/...)
    显示每类调用占比 · 让老板一眼看到哪类业务烧钱最多
    """
    import asyncio as _asyncio
    _require_admin(request)
    if days < 1 or days > 90:
        raise HTTPException(400, "days 必须在 [1, 90]")

    since, _, _ = _date_range(days)

    def _do():
        return _query_cost(lambda cur: get_api_cost_by_caller(cur, since))

    rows = await _asyncio.to_thread(_do)
    total_cost = sum(float(r.get("cost") or 0) for r in rows)
    return {
        "days": days,
        "total_cost": round(total_cost, 4),
        "items": [
            {
                "caller": r["caller"],
                "calls": int(r["calls"]),
                "cost": round(float(r["cost"] or 0), 4),
                "avg_cost": round(float(r["avg_cost"] or 0), 6),
                "failed": int(r["failed"] or 0),
                "share_pct": (
                    round(100.0 * float(r["cost"] or 0) / total_cost, 1)
                    if total_cost > 0 else 0
                ),
            }
            for r in rows
        ],
    }


# ============================================================
# 3. by-platform · 按平台分组(对账控制台用)
# ============================================================

@router.get("/by-platform")
async def llm_cost_by_platform(request: Request, days: int = 7):
    """按 platform 分组(kimi/doubao/dashscope/deepseek/metaso)
    跟 Moonshot/火山/阿里云/DeepSeek 控制台对账 误差 < 5% 是验收标准
    """
    import asyncio as _asyncio
    _require_admin(request)
    if days < 1 or days > 90:
        raise HTTPException(400, "days 必须在 [1, 90]")

    since, _, _ = _date_range(days)

    def _do():
        return _query_cost(lambda cur: get_api_cost_by_platform(cur, since))

    rows = await _asyncio.to_thread(_do)
    total_cost = sum(float(r.get("cost") or 0) for r in rows)
    return {
        "days": days,
        "total_cost": round(total_cost, 4),
        "items": [
            {
                "platform": r["platform"],
                "calls": int(r["calls"]),
                "cost": round(float(r["cost"] or 0), 4),
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "avg_duration_ms": int(float(r["avg_duration_ms"] or 0)),
                "failed": int(r["failed"] or 0),
                "share_pct": (
                    round(100.0 * float(r["cost"] or 0) / total_cost, 1)
                    if total_cost > 0 else 0
                ),
            }
            for r in rows
        ],
    }


# ============================================================
# 4. by-client · top 客户排行(LEFT JOIN brands.name + quotes.paid_amount)
# ============================================================

@router.get("/by-client")
async def llm_cost_by_client(request: Request, days: int = 7, top: int = 20):
    """top N 客户烧钱排行 · 让老板一眼看到哪些客户白嫖最严重
    JOIN brands.name + quotes.paid_amount(对比是否 paid 客户 vs draft 白嫖)
    """
    import asyncio as _asyncio
    _require_admin(request)
    if days < 1 or days > 90:
        raise HTTPException(400, "days 必须在 [1, 90]")
    if top < 1 or top > 100:
        raise HTTPException(400, "top 必须在 [1, 100]")

    since, _, _ = _date_range(days)

    def _do():
        return _query_db(
            """SELECT
                  l.brand_id,
                  COUNT(*) AS calls,
                  COALESCE(SUM(l.estimated_cost), 0) AS cost,
                  b.name AS brand_name,
                  -- 取该 brand 最近一个 quote 的状态/支付额(简化展示)
                  (SELECT q.status FROM quotes q WHERE q.brand_id = l.brand_id ORDER BY q.id DESC LIMIT 1) AS last_quote_status,
                  (SELECT q.paid_amount FROM quotes q WHERE q.brand_id = l.brand_id ORDER BY q.id DESC LIMIT 1) AS last_paid_amount
               FROM llm_call_log l
               LEFT JOIN brands b ON b.id = l.brand_id
               WHERE l.created_at >= %s AND l.brand_id IS NOT NULL
               GROUP BY l.brand_id, b.name
               ORDER BY cost DESC
               LIMIT %s""",
            (since, top),
        )

    rows = await _asyncio.to_thread(_do)
    return {
        "days": days,
        "top": top,
        "items": [
            {
                "brand_id": r["brand_id"],
                "brand_name": r.get("brand_name"),
                "calls": int(r["calls"]),
                "cost": round(float(r["cost"] or 0), 4),
                "last_quote_status": r.get("last_quote_status"),
                "last_paid_amount": float(r["last_paid_amount"] or 0) if r.get("last_paid_amount") else 0,
            }
            for r in rows
        ],
    }


# ============================================================
# 5. timeline · 时间序列(hour/day 堆叠图用)
# ============================================================

@router.get("/timeline")
async def llm_cost_timeline(
    request: Request,
    days: int = 7,
    granularity: str = "day",
):
    """时间序列堆叠图数据 · 按 platform 堆叠
    granularity: 'hour' / 'day'
    """
    import asyncio as _asyncio
    _require_admin(request)
    if days < 1 or days > 90:
        raise HTTPException(400, "days 必须在 [1, 90]")
    if granularity not in ("hour", "day"):
        raise HTTPException(400, "granularity 必须是 hour 或 day")

    since, _, _ = _date_range(days)
    trunc = "hour" if granularity == "hour" else "day"

    def _do():
        return _query_cost(lambda cur: get_api_cost_timeline(cur, since, granularity=trunc))

    rows = await _asyncio.to_thread(_do)
    return {
        "days": days,
        "granularity": granularity,
        "items": [
            {
                "bucket": r["bucket"].isoformat() if r.get("bucket") else None,
                "platform": r["platform"],
                "calls": int(r["calls"]),
                "cost": round(float(r["cost"] or 0), 4),
            }
            for r in rows
        ],
    }


# ============================================================
# 6. recent-calls · 最近 N 调用详情(可筛选)
# ============================================================

@router.get("/recent-calls")
async def llm_cost_recent_calls(
    request: Request,
    limit: int = 100,
    caller: Optional[str] = None,
    platform: Optional[str] = None,
    brand_id: Optional[int] = None,
    success: Optional[bool] = None,
):
    """最近 N 次调用详情 · 可筛选 · 用于异常排查"""
    import asyncio as _asyncio
    _require_admin(request)
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit 必须在 [1, 500]")

    where_clauses = []
    params: list = []
    if caller:
        where_clauses.append("caller = %s")
        params.append(caller)
    if platform:
        where_clauses.append("platform = %s")
        params.append(platform)
    if brand_id:
        where_clauses.append("brand_id = %s")
        params.append(brand_id)
    if success is not None:
        where_clauses.append("success = %s")
        params.append(success)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params.append(limit)

    def _do():
        return _query_db(
            f"""SELECT id, created_at, caller, platform, model,
                       input_tokens, output_tokens, cached_tokens,
                       estimated_cost, duration_ms,
                       brand_id, quote_id, user_id,
                       success, error_msg, metadata
               FROM llm_call_log
               {where_sql}
               ORDER BY id DESC
               LIMIT %s""",
            tuple(params),
        )

    rows = await _asyncio.to_thread(_do)
    return {
        "limit": limit,
        "filters": {
            "caller": caller, "platform": platform,
            "brand_id": brand_id, "success": success,
        },
        "items": [
            {
                "id": r["id"],
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                "caller": r["caller"],
                "platform": r["platform"],
                "model": r.get("model"),
                "input_tokens": int(r.get("input_tokens") or 0),
                "output_tokens": int(r.get("output_tokens") or 0),
                "cached_tokens": int(r.get("cached_tokens") or 0),
                "estimated_cost": round(float(r.get("estimated_cost") or 0), 6),
                "duration_ms": int(r.get("duration_ms") or 0),
                "brand_id": r.get("brand_id"),
                "quote_id": r.get("quote_id"),
                "user_id": r.get("user_id"),
                "success": r.get("success"),
                "error_msg": r.get("error_msg"),
                "metadata": r.get("metadata") or {},
            }
            for r in rows
        ],
    }
