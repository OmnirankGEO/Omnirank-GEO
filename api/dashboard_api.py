"""
首页数据 API
- /api/admin/dashboard — 管理员运营控制台（含5分钟缓存）
- /api/user/home-stats — 普通用户首页数据
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from services.api_costs import get_api_call_count, get_api_cost_daily, get_api_cost_summary
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-Dashboard")

router = APIRouter(tags=["首页数据"])

# 时区
CST = timezone(timedelta(hours=8))

# ========== 缓存 ==========
_admin_cache = {"data": None, "expires_at": 0}
CACHE_TTL = 300  # 5 分钟

# 积分汇率(1 元 = 130 积分 · SSOT)
POINTS_PER_YUAN = 130


# ========== 纯计算 helper(可真执行单测 · 不依赖 DB) ==========
# [v1.3 2026-05-29 老板复审 · 现金口径拍板] 把财务算术从 endpoint 抽出 ·
# 让 tests 能 import + 真跑(不再只文本扫 · 根治 v1.0 漏 P0-1 时区那类 runtime bug)

def _compute_finance_metrics(
    *,
    recharge_revenue_yuan: float,
    publish_revenue_yuan: float,
    llm_api_cost_yuan: float,
    publish_external_cost_yuan: float,
    active_7d: int,
) -> dict:
    """财务指标纯计算 · 现金口径(老板 2026-05-29 拍板)

    现金口径铁律:
      - 总收入 = 充值现金(recharge)· 发布收入【不并入】
        (代理发布扣的 cost_points 来自已充值的 paid_points · 并入会把同一笔现金算两次)
      - 总运营成本 = LLM API 费 + 发布外采(cost_yuan = 媒体原价 · 真实付给媒体的钱)
      - 毛利 = 总收入(现金) - 总运营成本
      - 毛利率 = 毛利 / 总收入
      - ARPU = 总收入 / 7 日活跃
      - publish_revenue_yuan 仅作信息字段(发布业务卡用)· 绝不进 total
    """
    total_revenue = round(recharge_revenue_yuan, 2)  # 现金 = 充值(不含发布)
    total_operating_cost = round(llm_api_cost_yuan + publish_external_cost_yuan, 2)
    profit = round(total_revenue - total_operating_cost, 2)
    profit_rate = (
        round(profit / max(total_revenue, 0.01), 2) if total_revenue > 0 else None
    )
    arpu = round(total_revenue / max(active_7d, 1), 2)
    return {
        # 旧字段保留(向后兼容已部署前端)· revenue_yuan = 充值现金(老语义)
        "revenue_yuan": round(recharge_revenue_yuan, 2),
        "cost_yuan": round(llm_api_cost_yuan, 2),
        # 收入侧
        "recharge_revenue_yuan": round(recharge_revenue_yuan, 2),
        "publish_revenue_yuan": round(publish_revenue_yuan, 2),  # 信息字段 · 不进 total
        "total_revenue_yuan": total_revenue,                      # 现金口径 = 充值
        "revenue_basis": "cash_recharge_only",                    # 显式口径标记(防回退误读)
        # 成本侧
        "llm_api_cost_yuan": round(llm_api_cost_yuan, 2),
        "publish_external_cost_yuan": round(publish_external_cost_yuan, 2),
        "total_operating_cost_yuan": total_operating_cost,
        # 利润(现金口径)
        "profit_yuan": profit,
        "profit_rate": profit_rate,
        "arpu": arpu,
    }


def _compute_cost_breakdown(
    *,
    llm_api_cost_yuan: float,
    publish_procurement_cost_yuan: float,
    monitoring_extra_cost_yuan: float = 0.0,
) -> dict:
    """[CTO-15.23 2026-05-29 Q2] 成本分项展示(不分摊)· 老板拍板 A 方案 · 扩展 v1.4 不重写

    publish_media 取【真实采购口径】mhz_synced_orders.price(媒介盒子实付价),
    ≠ v1.4 publish_external_cost_yuan(mhz_publish_order_items.cost_yuan 报价估值)·
    解决"本月成本只见 LLM ¥428、不含 ¥10,596 代发采购"的低估。
    revenue 维持 cash_recharge_only(v1.4 口径不动)· 仅 cost 侧加 breakdown。
    毛利不在此算:代发按月度合约分摊 · 列后续 phase(见 cost_margin_note)。
    """
    llm = round(float(llm_api_cost_yuan or 0), 2)
    pub = round(float(publish_procurement_cost_yuan or 0), 2)
    mon = round(float(monitoring_extra_cost_yuan or 0), 2)
    return {
        "cost_breakdown": {
            "llm_api": llm,
            "publish_media": pub,
            "monitoring_extra": mon,
        },
        "total_cost_yuan": round(llm + pub + mon, 2),
        "cost_margin_note": "毛利需合并月度合约收入(代发按月度合约分摊)· 后续 phase",
    }


def _compute_publish_card(*, publish_revenue_yuan: float, publish_margin_cost_yuan: float) -> dict:
    """发布业务卡纯计算 · 同 item 集(本月已发布)

    - revenue       = cost_points / 130(用户实付积分折现 = 我们的发布收入)
    - margin_cost   = cost_yuan(同一【已发布】item 集的媒体原价 · 跟 revenue 配对算毛利)
    - 毛利          = revenue - margin_cost = 媒体原价 × (markup - 1) ≈ 媒体价 × 0.5

    注意:这里的 cost 是【已发布】子集成本(配对收入)· 不是总运营外采成本 ·
    总运营外采成本是【全状态】口径(见 _query_publish_numbers 的 external_all)
    """
    rev = round(publish_revenue_yuan, 2)
    cost = round(publish_margin_cost_yuan, 2)
    return {"revenue_yuan": rev, "cost_yuan": cost, "profit_yuan": round(rev - cost, 2)}


def _query_publish_numbers(cur, month_start) -> tuple:
    """[v1.4 2026-05-29 Codex 复审 P1] 发布业务三口径分离

    返 (publish_revenue_yuan, publish_margin_cost_yuan, publish_external_all_yuan):
      - publish_revenue_yuan      已发布集 · SUM(cost_points)/130(用户实付 = 发布收入)
      - publish_margin_cost_yuan  已发布集 · SUM(cost_yuan)(配对收入算发布毛利)
      - publish_external_all_yuan 全状态集 · SUM(cost_yuan) WHERE created_at >= month_start
                                  (提交即占用外采 · published/rejected/failed/submitted/pending 都算)
                                  → 喂【总运营成本】· 防低估(Codex P1:只算已发布会漏未发布占用)

    根因(Codex v1.3 P1):v1.3 把"发布毛利成本(已发布)"和"总运营外采成本(全状态)"复用同一已发布口径
      → 总运营成本漏掉本月已提交但未发布/拒稿/失败的外采占用 → Dashboard 低估成本
    时区:month_start 为 aware datetime · 作 %s param · psycopg2 原生支持 · 不抛时区错
    """
    # A. 已发布集(发布卡收入 + 毛利成本 · 同 item 集同时间窗)
    cur.execute("""
        SELECT
            COALESCE(SUM(cost_points), 0) as pts,
            COALESCE(SUM(cost_yuan), 0) as yuan
        FROM mhz_publish_order_items
        WHERE status = 'published'
          AND COALESCE(published_at, created_at) >= %s
    """, (month_start,))
    a = cur.fetchone()
    publish_revenue = round(float((a["pts"] if a else 0) or 0) / POINTS_PER_YUAN, 2)
    publish_margin_cost = round(float((a["yuan"] if a else 0) or 0), 2)

    # B. 全状态集(总运营外采成本 · 提交即占用 · 不按 status 过滤)
    cur.execute("""
        SELECT COALESCE(SUM(cost_yuan), 0) as yuan
        FROM mhz_publish_order_items
        WHERE created_at >= %s
    """, (month_start,))
    b = cur.fetchone()
    publish_external_all = round(float((b["yuan"] if b else 0) or 0), 2)

    return publish_revenue, publish_margin_cost, publish_external_all


# ========== 管理员运营控制台 ==========

@router.get("/api/admin/dashboard")
async def admin_dashboard(request: Request, period: str = Query("month")):
    """管理员运营控制台数据（5 分钟缓存）

    [CTO-15.23 2026-05-10 P1 async safety]
      老: async def 内直接 _compute_admin_dashboard(period) → 内部 sync psycopg2 几十次 cursor.execute
          阻塞 event loop · admin 进 dashboard 时全站别的 request 串行排队
      新: asyncio.to_thread wrap · 释放 event loop · 别的 request 同时跑
    """
    import asyncio
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可访问")

    now = time.time()
    cache_key = f"admin_dashboard_{period}"
    if _admin_cache.get("key") == cache_key and _admin_cache["data"] and now < _admin_cache["expires_at"]:
        return _admin_cache["data"]

    data = await asyncio.to_thread(_compute_admin_dashboard, period)
    _admin_cache["data"] = data
    _admin_cache["expires_at"] = now + CACHE_TTL
    _admin_cache["key"] = cache_key
    return data


def _compute_admin_dashboard(period: str) -> dict:
    from db.connection import get_connection

    conn = get_connection()
    cur = conn.cursor()

    today = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_start = today.replace(day=1)

    try:
        # 确保 system_config 表存在
        cur.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        # ========== 用户增长 ==========
        cur.execute("SELECT COUNT(*) as cnt FROM users WHERE is_active = 1")
        total_users = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM users WHERE created_at >= %s", (today,))
        today_new = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM users WHERE created_at >= %s", (week_ago,))
        week_new = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM users WHERE last_login_at >= %s", (week_ago,))
        active_7d = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM user_wallets WHERE total_recharged > 0")
        paid_count = cur.fetchone()["cnt"]

        # 用户层级分布
        cur.execute("""
            SELECT agent_level, COUNT(*) as cnt FROM user_wallets GROUP BY agent_level
        """)
        level_dist = {"free": 0, "paid": 0, "agent_l1": 0, "agent_l2": 0}
        for r in cur.fetchall():
            lvl = r["agent_level"]
            if lvl == 0:
                level_dist["free"] = r["cnt"]
            elif lvl == 1:
                level_dist["agent_l1"] = r["cnt"]
            elif lvl == 2:
                level_dist["agent_l2"] = r["cnt"]
        level_dist["paid"] = paid_count

        users_data = {
            "total": total_users,
            "today_new": today_new,
            "week_new": week_new,
            "active_7d": active_7d,
            "active_rate": round(active_7d / max(total_users, 1), 2),
            "paid_count": paid_count,
            "paid_rate": round(paid_count / max(total_users, 1), 2),
            "level_distribution": level_dist,
        }

        # ========== 财务 ==========
        # 充值收入 = recharge_orders 累计金额
        cur.execute("""
            SELECT COALESCE(SUM(amount_cents), 0) as total
            FROM recharge_orders
            WHERE payment_status = 'paid'
              AND COALESCE(paid_at, created_at) >= %s
        """, (month_start,))
        month_recharge_revenue_cents = cur.fetchone()["total"]
        month_recharge_revenue = month_recharge_revenue_cents / 100

        # API 成本统一口径:llm_call_log + legacy token_usage + 未迁移 monitoring_token_usage.
        # 不再让顶部财务卡、API成本明细、TV 趋势各算各的。
        api_cost_summary = {"total_cost": 0, "total_calls": 0, "items": []}
        try:
            api_cost_summary = get_api_cost_summary(cur, month_start, limit=15)
            month_llm_api_cost = round(float(api_cost_summary["total_cost"]), 2)
        except Exception as e:
            conn.rollback()
            logger.warning(f"统一 API 成本查询失败: {e}")
            month_llm_api_cost = 0

        # [v1.3 列语义修正 → v1.4 Codex 复审 P1 · 三口径分离]
        # cost_yuan = 媒体原价(成本)· cost_points = 用户实付(媒体价×1.5×130 = 收入)
        # 实证:扣费 meijiehezi_api.py:252 deduct_points(extra_cost=cost_points) · markup PublishCenter.tsx:1713
        # v1.4 拆三口径(防 v1.3 把"发布毛利成本"和"总运营外采成本"复用已发布口径 → 低估总成本):
        #   month_publish_revenue        已发布集 cost_points/130(发布收入)
        #   month_publish_margin_cost    已发布集 cost_yuan(配对收入算发布毛利)
        #   month_publish_external_cost  全状态集 cost_yuan(提交即占用 · 喂总运营成本 · 不漏未发布占用)
        month_publish_revenue = 0
        month_publish_margin_cost = 0
        month_publish_external_cost = 0
        try:
            month_publish_revenue, month_publish_margin_cost, month_publish_external_cost = \
                _query_publish_numbers(cur, month_start)
        except Exception as e:
            conn.rollback()
            logger.warning(f"发布业务收入/成本查询失败 · 兜底 0: {e}")
            month_publish_revenue = 0
            month_publish_margin_cost = 0
            month_publish_external_cost = 0

        # 总运营成本 = LLM API 费 + 发布外采(全状态占用)+ (未来:复盘 / 其他)
        # v1.4:用全状态 month_publish_external_cost(不是已发布子集)· 防低估
        month_total_operating_cost = round(month_llm_api_cost + month_publish_external_cost, 2)

        # [CTO-15.23 2026-05-29 Q2] 真实代发采购成本(媒介盒子实付价)· mhz_synced_orders.price
        # ≠ 上方 month_publish_external_cost(item.cost_yuan 报价估值)· 供 cost_breakdown.publish_media 真实分项
        month_publish_procurement_cost = 0.0
        try:
            cur.execute("""
                SELECT COALESCE(SUM(price), 0) as total
                FROM mhz_synced_orders
                WHERE COALESCE(created_at, synced_at) >= %s
            """, (month_start,))
            month_publish_procurement_cost = round(float(cur.fetchone()["total"] or 0), 2)
        except Exception as e:
            conn.rollback()
            logger.warning(f"代发采购成本(mhz_synced_orders.price)查询失败 · 兜底 0: {e}")
            month_publish_procurement_cost = 0.0

        cur.execute("""
            SELECT COALESCE(SUM(ABS(amount)), 0) as total
            FROM point_transactions WHERE type = 'consume' AND created_at >= %s
        """, (month_start,))
        month_consumed = cur.fetchone()["total"]

        cur.execute("""
            SELECT COALESCE(SUM(base_points + bonus_points), 0) as total
            FROM recharge_orders
            WHERE payment_status = 'paid'
              AND COALESCE(paid_at, created_at) >= %s
        """, (month_start,))
        month_issued = cur.fetchone()["total"]

        # [v1.3 2026-05-29 老板拍现金口径(P1-2)]
        # 总收入 = 充值现金(不含发布 · 防双算:cost_points 来自已充值 paid_points)
        # profit / profit_rate / arpu 全基于充值现金 · 发布收入仅信息字段
        finance_data = _compute_finance_metrics(
            recharge_revenue_yuan=month_recharge_revenue,
            publish_revenue_yuan=month_publish_revenue,
            llm_api_cost_yuan=month_llm_api_cost,
            publish_external_cost_yuan=month_publish_external_cost,
            active_7d=active_7d,
        )
        finance_data["total_points_issued"] = month_issued
        finance_data["total_points_consumed"] = month_consumed
        # [CTO-15.23 2026-05-29 Q2] 成本分项 breakdown(扩展 v1.4 · revenue 维持 cash_recharge_only 不动)
        finance_data.update(_compute_cost_breakdown(
            llm_api_cost_yuan=month_llm_api_cost,
            publish_procurement_cost_yuan=month_publish_procurement_cost,
        ))

        # ========== 功能使用 TOP 10 ==========
        cur.execute("""
            SELECT feature_code, COUNT(*) as count, SUM(ABS(amount)) as points
            FROM point_transactions WHERE type = 'consume'
            GROUP BY feature_code ORDER BY count DESC LIMIT 10
        """)
        feature_usage = []
        for r in cur.fetchall():
            # 获取功能名称
            cur.execute("SELECT feature_name FROM feature_pricing WHERE feature_code = %s", (r["feature_code"],))
            name_row = cur.fetchone()
            feature_usage.append({
                "code": r["feature_code"],
                "name": name_row["feature_name"] if name_row else r["feature_code"],
                "count": r["count"],
                "points": r["points"] or 0,
            })

        # ========== API 成本 ==========
        api_costs = api_cost_summary.get("items", [])

        # ========== 代发业务(本月口径) ==========
        # [v1.2 P1-1 → v1.3 修列语义] publishing 卡全本月口径 + 收入/成本同 item 集
        # v1.3:发布收入 = cost_points/130(用户实付)· 外采成本 = cost_yuan(媒体原价)·
        #       二者取自同一"本月已发布"item 集(month_publish_revenue / month_publish_external_cost)
        #       → 发布毛利 = 媒体价 × 0.5 真 markup margin · 收入成本同时间窗(修 v1.2 P2 跨锚)
        # 计数:total_orders 本月新提交(created_at)· published 本月已发布(published_at)· rejected 本月被拒(created_at)
        # UI 文案全部加"本月..."前缀(防岁月静好误读)
        publishing = {
            "total_orders": 0, "published": 0, "rejected": 0,
            "revenue_yuan": 0, "cost_yuan": 0, "profit_yuan": 0,
            "media_count": 0, "session_valid": None,
            # 显式语义标记(给 UI + 文档)
            "period": "month",
            "period_start": month_start.isoformat() if hasattr(month_start, "isoformat") else str(month_start),
        }
        try:
            cur.execute("SELECT COUNT(*) as cnt FROM mhz_media WHERE is_active = TRUE")
            publishing["media_count"] = cur.fetchone()["cnt"]

            # [2026-04-30] 老表 publish_order_items 已废弃(0 数据)
            # 改读 mhz_publish_order_items(当前活跃链路)
            # v1.2 老板 P1-1:total_orders 本月新提交订单数(created_at)
            cur.execute(
                "SELECT COUNT(*) as cnt FROM mhz_publish_order_items WHERE created_at >= %s",
                (month_start,),
            )
            publishing["total_orders"] = cur.fetchone()["cnt"]

            # v1.2 published 本月发布(published_at · 包括上月创建本月发布)
            cur.execute(
                """SELECT COUNT(*) as cnt FROM mhz_publish_order_items
                   WHERE status = 'published'
                     AND COALESCE(published_at, created_at) >= %s
""",
                (month_start,),
            )
            publishing["published"] = cur.fetchone()["cnt"]

            # v1.2 rejected 本月被拒(用 created_at · rejected 没明确 rejected_at 字段)
            cur.execute(
                """SELECT COUNT(*) as cnt FROM mhz_publish_order_items
                   WHERE status = 'rejected' AND created_at >= %s""",
                (month_start,),
            )
            publishing["rejected"] = cur.fetchone()["cnt"]

            # v1.4 发布业务卡 = 同一"本月已发布"item 集(纯计算 helper · 收入/成本同时间窗)
            # 收入 = cost_points/130(month_publish_revenue)· 成本 = 已发布集 cost_yuan(month_publish_margin_cost)
            # ★ 这里用 margin_cost(已发布子集)· 不是 external_cost(全状态)· 二者口径不同(Codex P1)
            _pc = _compute_publish_card(
                publish_revenue_yuan=month_publish_revenue,
                publish_margin_cost_yuan=month_publish_margin_cost,
            )
            publishing["revenue_yuan"] = _pc["revenue_yuan"]
            publishing["cost_yuan"] = _pc["cost_yuan"]
            publishing["profit_yuan"] = _pc["profit_yuan"]
            # v1.4 顶部"发布外采成本"(全状态)也带进 publishing 供前端区分展示
            publishing["external_cost_all_yuan"] = round(month_publish_external_cost, 2)

            # Session 状态从 mhz_config 读（不做网络请求）
            try:
                cur.execute("SELECT value FROM mhz_config WHERE key = 'phpsessid'")
                row = cur.fetchone()
                publishing["session_valid"] = bool(row and row["value"])
            except Exception:
                conn.rollback()
                publishing["session_valid"] = None
        except Exception:
            pass

        # ========== 推荐排行（三榜）==========
        # 直接推荐榜
        cur.execute("""
            SELECT rl.referrer_id, u.display_name, COUNT(*) as count
            FROM referral_links rl JOIN users u ON rl.referrer_id = u.id
            WHERE rl.level = 1
            GROUP BY rl.referrer_id, u.display_name ORDER BY count DESC LIMIT 10
        """)
        direct_lb = [{"user_id": r["referrer_id"], "display_name": r["display_name"], "count": r["count"]} for r in cur.fetchall()]

        # 间接推荐榜
        cur.execute("""
            SELECT rl.referrer_id, u.display_name, COUNT(*) as count
            FROM referral_links rl JOIN users u ON rl.referrer_id = u.id
            WHERE rl.level = 2
            GROUP BY rl.referrer_id, u.display_name ORDER BY count DESC LIMIT 10
        """)
        indirect_lb = [{"user_id": r["referrer_id"], "display_name": r["display_name"], "count": r["count"]} for r in cur.fetchall()]

        # 佣金总榜
        cur.execute("""
            SELECT user_id, SUM(amount) as total
            FROM point_transactions
            WHERE type IN ('commission', 'bonus') AND description LIKE '%%佣金%%'
            GROUP BY user_id ORDER BY total DESC LIMIT 10
        """)
        commission_lb = []
        for r in cur.fetchall():
            cur.execute("SELECT display_name FROM users WHERE id = %s", (r["user_id"],))
            uname = cur.fetchone()
            commission_lb.append({
                "user_id": r["user_id"],
                "display_name": uname["display_name"] if uname else "?",
                "commission_points": r["total"],
                "commission_yuan": round(r["total"] / 130, 2) if r["total"] else 0,
            })

        cur.execute("SELECT COUNT(*) as cnt FROM referral_links")
        total_refs = cur.fetchone()["cnt"]
        cur.execute("SELECT COALESCE(MAX(level), 0) as depth FROM referral_links")
        deepest = cur.fetchone()["depth"]

        referrals_data = {
            "direct_leaderboard": direct_lb,
            "indirect_leaderboard": indirect_lb,
            "commission_leaderboard": commission_lb,
            "total_referrals": total_refs,
            "deepest_chain": deepest,
        }

        # ========== 最近活动 ==========
        activities = []
        try:
            # 最近诊断
            cur.execute("""
                SELECT dr.brand_name, dr.total_score, dr.created_at, u.display_name
                FROM diagnosis_records dr
                LEFT JOIN brands b ON dr.brand_name = b.name
                LEFT JOIN user_clients uc ON uc.brand_id = b.id
                LEFT JOIN users u ON uc.user_id = u.id
                ORDER BY dr.created_at DESC LIMIT 5
            """)
            for r in cur.fetchall():
                activities.append({
                    "type": "diagnosis",
                    "user": r.get("display_name", ""),
                    "title": f"GEO诊断: {r['brand_name']}" + (f" 评分{r['total_score']}" if r.get("total_score") else ""),
                    "time": r["created_at"].strftime("%m/%d %H:%M") if r.get("created_at") else "",
                    "link": "/history",
                })

            # 最近注册
            cur.execute("SELECT display_name, username, created_at, register_city FROM users ORDER BY created_at DESC LIMIT 3")
            for r in cur.fetchall():
                city = f" ({r['register_city']})" if r.get("register_city") else ""
                activities.append({
                    "type": "register",
                    "user": r["display_name"],
                    "title": f"新用户注册{city}",
                    "time": r["created_at"].strftime("%m/%d %H:%M") if r.get("created_at") else "",
                    "link": f"/admin/users",
                })

            # 按时间排序
            activities.sort(key=lambda x: x.get("time", ""), reverse=True)
            activities = activities[:10]
        except Exception as e:
            logger.warning(f"获取最近活动失败: {e}")

        return {
            "status": "success",
            "users": users_data,
            "finance": finance_data,
            "feature_usage": feature_usage,
            "api_costs": api_costs,
            "publishing": publishing,
            "referrals": referrals_data,
            "activities": activities,
            "cached_at": datetime.now(CST).isoformat(),
        }

    finally:
        conn.close()


# ========== 普通用户首页 ==========

@router.get("/api/user/home-stats")
async def user_home_stats(request: Request):
    """普通用户首页数据"""
    user = request.state.user
    user_id = user["user_id"]

    from db.connection import get_connection
    conn = get_connection()
    cur = conn.cursor()

    try:
        # GEO 数据
        # CTO-15.23 2026-05-25 · 老板报"9 品牌不对/有污染" · 加 3 条 WHERE 排除:
        # 1. is_deleted=TRUE 软删除客户(代理点删除后还留 brands · 不算客户数)
        # 🔴 [#116 2026-09-05] 原第 2 条「排除 is_test」**已删**:
        #    这条 SQL 的 WHERE 已经是 `uc.user_id = 本人` —— 属**自己名下**,
        #    按 #67 不隔离。此前它与 `brand_api` 的「我的客户」列表口径相反,
        #    结果是首页写 8 个客户、点进列表看到 9 个,而**没有任何东西报错**。
        #    规则取自唯一定义处 `services/brand_test_visibility`,不在这里再写一份。
        from services.brand_test_visibility import own_scope_test_clause as _own_scope_test_clause
        # 3. brand_type='self' 代理自有品牌(自有品牌不是客户 · 是代理自己测试 / 营销主体)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM brands b
            JOIN user_clients uc ON uc.brand_id = b.id
            WHERE uc.user_id = %s
              AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
              AND (b.brand_type IS NULL OR b.brand_type != 'self')
        """ + _own_scope_test_clause(), (user_id,))
        brand_count = cur.fetchone()["cnt"]

        cur.execute("""
            SELECT COUNT(*) as cnt FROM article_generations ag
            JOIN diagnosis_records dr ON ag.diagnosis_id = dr.id
            JOIN brands b ON dr.brand_name = b.name
            JOIN user_clients uc ON uc.brand_id = b.id
            WHERE uc.user_id = %s
        """, (user_id,))
        article_count = cur.fetchone()["cnt"]

        cur.execute("""
            SELECT COUNT(*) as cnt FROM mhz_publish_order_items poi
            JOIN mhz_publish_orders po ON poi.order_id = po.id
            WHERE po.user_id = %s AND poi.status = 'published'
        """, (user_id,))
        published_count = cur.fetchone()["cnt"]

        month_start = datetime.now(CST).replace(day=1, hour=0, minute=0, second=0)
        # [返工2 P1-2] published-only:排 withheld(退款/失败)+ pending(生成中)· 否则月度计数/最新分含退款诊断
        cur.execute("""
            SELECT COUNT(*) as cnt FROM diagnosis_records dr
            JOIN brands b ON dr.brand_name = b.name
            JOIN user_clients uc ON uc.brand_id = b.id
            WHERE uc.user_id = %s AND dr.created_at >= %s
              AND (dr.result_visibility IS NULL OR dr.result_visibility = 'published')
        """, (user_id, month_start))
        diagnosis_month = cur.fetchone()["cnt"]

        # 最新诊断分数 [返工2 P1-2] published-only(退款诊断不得成为展示的"最新分")
        cur.execute("""
            SELECT dr.total_score FROM diagnosis_records dr
            JOIN brands b ON dr.brand_name = b.name
            JOIN user_clients uc ON uc.brand_id = b.id
            WHERE uc.user_id = %s AND dr.total_score IS NOT NULL
              AND (dr.result_visibility IS NULL OR dr.result_visibility = 'published')
            ORDER BY dr.created_at DESC LIMIT 1
        """, (user_id,))
        score_row = cur.fetchone()
        latest_score = score_row["total_score"] if score_row else None

        # 最近发布（迁到新表）
        cur.execute("""
            SELECT poi.media_name, poi.status, poi.publish_url, poi.reject_reason,
                   po.article_title, poi.published_at, poi.submitted_at
            FROM mhz_publish_order_items poi
            JOIN mhz_publish_orders po ON poi.order_id = po.id
            WHERE po.user_id = %s
            ORDER BY COALESCE(poi.published_at, poi.submitted_at, po.created_at) DESC
            LIMIT 5
        """, (user_id,))
        recent_publishes = []
        for r in cur.fetchall():
            t = r.get("published_at") or r.get("submitted_at")
            recent_publishes.append({
                "media_name": r["media_name"],
                "title": r["article_title"],
                "status": r["status"],
                "url": r.get("publish_url", ""),
                "time": t.strftime("%m/%d %H:%M") if t else "",
            })

        # [WP7 2026-08-17] `published_count` 是**原始尝试数**,不是合同口径的
        # "已发布在线":它只数代发链、按 item 计数(同一交付单元重试会重复计)、
        # 且不理会撤稿。首页不逐 quote 起投影(那要枚举该用户全部报价),
        # 所以按规格 03 §10 允许的另一条路走 —— **保留 raw attempts,但标签明确**。
        # 想要合同口径请走 `services.publication_stage_adapters.quote_published_active`。
        from services.publication_stage_adapters import RAW_ATTEMPT_LABEL

        geo = {
            "brand_count": brand_count,
            "article_count": article_count,
            "published_count": published_count,
            "published_count_basis": "raw_attempts_mhz_chain_only",
            "published_count_label": RAW_ATTEMPT_LABEL,
            "diagnosis_count_month": diagnosis_month,
            "latest_score": latest_score,
            "recent_publishes": recent_publishes,
        }

        # 社媒数据
        social = {"topics_total": 0, "scripts_total": 0, "corpus_count": 0, "published_count": 0}
        try:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM social_topics st JOIN social_projects sp ON st.project_id = sp.id
                     JOIN user_clients uc ON uc.brand_id = sp.brand_id WHERE uc.user_id = %s) as topics,
                    (SELECT COUNT(*) FROM social_scripts ss JOIN social_projects sp ON ss.project_id = sp.id
                     JOIN user_clients uc ON uc.brand_id = sp.brand_id WHERE uc.user_id = %s) as scripts,
                    (SELECT COUNT(*) FROM social_scripts ss2 JOIN social_projects sp2 ON ss2.project_id = sp2.id
                     JOIN user_clients uc2 ON uc2.brand_id = sp2.brand_id WHERE uc2.user_id = %s AND ss2.status = 'published') as published,
                    (SELECT COUNT(*) FROM profile_corpus pc JOIN client_profiles cp ON pc.profile_id = cp.id
                     JOIN user_clients uc3 ON uc3.brand_id = cp.brand_id WHERE uc3.user_id = %s) as corpus
            """, (user_id, user_id, user_id, user_id))
            sr = cur.fetchone()
            social["topics_total"] = sr["topics"] if sr else 0
            social["scripts_total"] = sr["scripts"] if sr else 0
            social["published_count"] = sr["published"] if sr else 0
            social["corpus_count"] = sr["corpus"] if sr else 0
        except Exception:
            pass

        # 余额
        cur.execute("SELECT paid_points, bonus_points FROM user_wallets WHERE user_id = %s", (user_id,))
        w = cur.fetchone()
        balance = {
            "paid_points": w["paid_points"] if w else 0,
            "bonus_points": w["bonus_points"] if w else 0,
            "total": (w["paid_points"] + w["bonus_points"]) if w else 0,
        }

        # 最近活动
        activities = []
        try:
            cur.execute("""
                SELECT feature_code, description, created_at
                FROM point_transactions
                WHERE user_id = %s AND type = 'consume'
                ORDER BY created_at DESC LIMIT 5
            """, (user_id,))
            for r in cur.fetchall():
                activities.append({
                    "type": "usage",
                    "title": r.get("description") or r.get("feature_code", ""),
                    "time": r["created_at"].strftime("%m/%d %H:%M") if r.get("created_at") else "",
                })
        except Exception:
            pass

        # 用户昵称：优先用 self 品牌名，再用 profile 名
        nickname = None
        try:
            cur.execute("SELECT name FROM brands WHERE owner_user_id = %s AND brand_type = 'self' LIMIT 1", (user_id,))
            r = cur.fetchone()
            if r and r["name"]:
                nickname = r["name"]
            if not nickname:
                cur.execute("""
                    SELECT cp.name FROM client_profiles cp
                    JOIN user_clients uc ON cp.brand_id = uc.brand_id
                    WHERE uc.user_id = %s AND (cp.is_deleted = 0 OR cp.is_deleted IS NULL)
                    ORDER BY cp.updated_at DESC LIMIT 1
                """, (user_id,))
                r = cur.fetchone()
                if r and r["name"]:
                    nickname = r["name"]
        except Exception:
            pass

        return {
            "status": "success",
            "nickname": nickname,
            "geo": geo,
            "social": social,
            "balance": balance,
            "recent_activities": activities,
        }

    finally:
        conn.close()


# ========== 电视大屏 ==========

_tv_cache = {"data": None, "expires_at": 0}
TV_CACHE_TTL = 30  # 30 秒

# 管理员生成的大屏 token（历史方案，存 system_config · 现只读兼容 · Review-CTO D4 裁决）
TV_TOKEN_KEY = "tv_dashboard_token"

# ========== TV 大屏二次门禁（密码 / 一次性交换码 → HttpOnly 会话）==========
# [板块 D · 2026-07-22] 纯前端硬编码密码门升级为服务端校验：
#   POST /api/tv/access            访问密码 → 会话 cookie
#   POST /api/tv/access/exchange   URL token（一次性交换码或 legacy token）→ 会话 cookie
#   GET  /api/tv/access/session    会话探测（前端刷新恢复用）
#   GET  /api/tv/dashboard/auth    会话鉴权的数据接口
# 会话为 HMAC 签名的无状态 cookie（uid + 过期 + 密码指纹），多 worker 共享 JWT_SECRET 即可横向扩展。
TV_SESSION_COOKIE = "omnirank_tv_session"
TV_SESSION_TTL_SECONDS = 8 * 60 * 60
TV_PASSWORD_SALT = b"omnirank-tv-access-v1"
TV_PASSWORD_ITERATIONS = 210_000
_TV_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")

# 一次性交换码（D4：停止签发长期 URL token；旧 token 只读、只记指纹）。
# WORKERS=1 可内存降级；WORKERS>1 必须使用 Redis 原子共享状态，否则 fail-closed。
# start.sh 已保证只有 Redis readiness 成功时才真正启动多 worker；这里是端点级第二道安全门。
TV_EXCHANGE_CODE_TTL_SECONDS = 10 * 60
_TV_EXCHANGE_CODES: dict[str, dict] = {}  # sha256(code) -> {expires_at, created_by, fingerprint}

# /api/tv/access 密码失败限流（集中严审 R1 · P2-2）：按 admin_uid 计失败，
# 连续 TV_ACCESS_MAX_FAILURES 次失败 → 60s 冷却，冷却内一律 429 TV_ACCESS_RATE_LIMITED；成功登录清零。
# WORKERS>1 与交换码共用 Redis：失败计数/冷却由 Lua 原子更新，避免每 worker 阈值被横向放大。
TV_ACCESS_MAX_FAILURES = 10
TV_ACCESS_COOLDOWN_SECONDS = 60
_TV_ACCESS_FAILURES: dict[int, dict] = {}  # uid -> {count, cooldown_until}（仅单 worker 降级）
_TV_REDIS_PREFIX = "omnirank:tv:security:v1"
_TV_FAILURE_COUNTER_TTL_SECONDS = TV_SESSION_TTL_SECONDS

_TV_RECORD_FAILURE_LUA = """
local cooldown_ttl = redis.call('PTTL', KEYS[2])
if cooldown_ttl > 0 then
  return {0, cooldown_ttl}
end
local failures = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
if failures >= tonumber(ARGV[1]) then
  redis.call('DEL', KEYS[1])
  redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[2]))
  return {failures, tonumber(ARGV[2]) * 1000}
end
return {failures, 0}
"""


class TVSecurityStateUnavailable(RuntimeError):
    """多 worker 的 TV 共享安全状态不可用；调用方必须 fail-closed。"""


def _tv_worker_count() -> int:
    try:
        workers = int(str(os.getenv("WORKERS", "1")).strip())
    except (TypeError, ValueError):
        # 非法 worker 配置不应静默降级成“安全的单进程”判断。
        return 2
    return max(1, workers)


def _tv_shared_state_client():
    """多 worker 时返回共享 Redis；单 worker 返回 None 表示使用进程内降级。"""
    if _tv_worker_count() <= 1:
        return None
    try:
        from cache.redis_client import get_redis

        client = get_redis()
    except Exception as exc:
        logger.error("TV shared security state unavailable: %s", type(exc).__name__)
        raise TVSecurityStateUnavailable("TV shared security state unavailable") from exc
    if client is None:
        raise TVSecurityStateUnavailable("TV shared security state unavailable")
    return client


def _tv_exchange_redis_key(code_hash: str) -> str:
    return f"{_TV_REDIS_PREFIX}:exchange:{code_hash}"


def _tv_failure_redis_keys(user_id: int) -> tuple[str, str]:
    base = f"{_TV_REDIS_PREFIX}:access:{int(user_id)}"
    return f"{base}:failures", f"{base}:cooldown"


def _tv_request_id(request: Request) -> str:
    candidate = str(request.headers.get("X-Request-ID") or "").strip()
    return candidate if _TV_REQUEST_ID_RE.fullmatch(candidate) else uuid.uuid4().hex


def _tv_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    success: bool = False,
    extra: dict | None = None,
) -> JSONResponse:
    content = {
        "success": success,
        "status": "success" if success else "error",
        "code": code,
        "message": message,
        "request_id": request_id,
    }
    if extra:
        content.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
    )


def _tv_security_state_unavailable_response(request_id: str) -> JSONResponse:
    return _tv_response(
        status_code=503,
        code="TV_ACCESS_UNAVAILABLE",
        message="大屏访问服务暂时不可用，请稍后重试",
        request_id=request_id,
    )


def _tv_admin_id(request: Request) -> tuple[int | None, JSONResponse | None]:
    request_id = _tv_request_id(request)
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        return None, _tv_response(
            status_code=401,
            code="TV_AUTH_REQUIRED",
            message="请先登录管理员账号",
            request_id=request_id,
        )
    if not user.get("is_admin"):
        return None, _tv_response(
            status_code=403,
            code="TV_ADMIN_REQUIRED",
            message="仅管理员可访问大屏",
            request_id=request_id,
        )
    raw_user_id = user.get("user_id") or current_user_id(user)
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return None, _tv_response(
            status_code=401,
            code="TV_AUTH_REQUIRED",
            message="请重新登录管理员账号",
            request_id=request_id,
        )
    return user_id, None


def _tv_legacy_password_enabled() -> bool:
    # [集中严审 R1 · P1-1] legacy 兜底哈希显式 opt-in：默认 false。
    # 该回退仅为密码轮换窗口的显式 opt-in；下一发布周期移除。
    # 移除后未配置 TV_ACCESS_PASSWORD_HASH 一律 fail-closed。
    return os.getenv("TV_ACCESS_PASSWORD_LEGACY_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _tv_password_hash() -> str:
    # TV_ACCESS_PASSWORD_HASH 是唯一常规来源（小写 64 位 hex · PBKDF2-HMAC-SHA256 · v1 salt · 210k 轮），
    # 畸形配置直接抛错 → 503 fail-closed。
    # 未配置时默认 fail-closed（抛错 → /api/tv/access 503 TV_ACCESS_UNAVAILABLE）；
    # 仅当 TV_ACCESS_PASSWORD_LEGACY_ENABLED=true（显式 opt-in）且 legacy 哈希也由
    # secret store 注入时才回退。源码不得携带任何可验证的密码材料。
    configured = os.getenv("TV_ACCESS_PASSWORD_HASH")
    if configured is None:
        if _tv_legacy_password_enabled():
            configured = os.getenv("TV_ACCESS_PASSWORD_LEGACY_HASH")
            if configured is None:
                raise ValueError("TV_ACCESS_PASSWORD_LEGACY_HASH is not configured")
        else:
            raise ValueError("TV_ACCESS_PASSWORD_HASH is not configured")
    normalized = configured.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("TV_ACCESS_PASSWORD_HASH must be a 64-character hex digest")
    return normalized


def _verify_tv_password(password: str) -> bool:
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        TV_PASSWORD_SALT,
        TV_PASSWORD_ITERATIONS,
    ).hex()
    return hmac.compare_digest(candidate, _tv_password_hash())


def _tv_session_key() -> bytes:
    from auth.jwt_utils import JWT_SECRET

    return hmac.new(
        JWT_SECRET.encode("utf-8"),
        b"omnirank-tv-access-session-v1",
        hashlib.sha256,
    ).digest()


def _tv_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _tv_b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _create_tv_session(user_id: int, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "v": 1,
        "uid": user_id,
        "iat": issued_at,
        "exp": issued_at + TV_SESSION_TTL_SECONDS,
        "pwd": _tv_password_hash()[:16],
        "nonce": secrets.token_hex(8),
    }
    encoded = _tv_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _tv_b64encode(hmac.new(_tv_session_key(), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _validate_tv_session(token: str, user_id: int, now: int | None = None) -> bool:
    if not token or len(token) > 1024:
        return False
    try:
        encoded, signature = token.split(".", 1)
        expected = _tv_b64encode(
            hmac.new(_tv_session_key(), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return False
        payload = json.loads(_tv_b64decode(encoded))
        checked_at = int(time.time()) if now is None else int(now)
        return (
            payload.get("v") == 1
            and int(payload.get("uid")) == user_id
            and int(payload.get("iat")) <= checked_at
            and int(payload.get("exp")) > checked_at
            and payload.get("pwd") == _tv_password_hash()[:16]
        )
    except Exception:
        return False


def _require_tv_session(request: Request) -> tuple[int | None, JSONResponse | None]:
    user_id, auth_error = _tv_admin_id(request)
    if auth_error is not None or user_id is None:
        return None, auth_error
    request_id = _tv_request_id(request)
    token = request.cookies.get(TV_SESSION_COOKIE, "")
    if not token:
        return None, _tv_response(
            status_code=401,
            code="TV_ACCESS_SESSION_REQUIRED",
            message="请输入大屏访问密码",
            request_id=request_id,
        )
    if not _validate_tv_session(token, user_id):
        response = _tv_response(
            status_code=401,
            code="TV_ACCESS_SESSION_INVALID",
            message="大屏访问会话已失效，请重新验证",
            request_id=request_id,
        )
        response.delete_cookie(TV_SESSION_COOKIE, path="/api/tv")
        return None, response
    return user_id, None


def _tv_secure_cookie(request: Request) -> bool:
    forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _tv_fingerprint(secret_value: str) -> str:
    """token / 交换码指纹（sha256 前 12 位）。日志与审计只允许出现指纹，禁止明文。"""
    return hashlib.sha256(secret_value.encode("utf-8")).hexdigest()[:12]


def _tv_legacy_token_enabled() -> bool:
    # 合同 §7 flag：默认 true（一个发布窗口兼容期），下版本改 false 后 legacy 链路整体 fail-closed。
    return os.getenv("TV_LEGACY_TOKEN_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _read_legacy_tv_token() -> str | None:
    """只读 system_config.tv_dashboard_token（D4：旧 token 全程只读 · 未配置/读取失败一律 fail-closed 返回 None）。"""
    conn = None
    try:
        from db.connection import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM system_config WHERE key = %s", (TV_TOKEN_KEY,))
        row = cur.fetchone()
        return row["value"] if row and row["value"] else None
    except Exception as exc:
        logger.warning("TV legacy token read failed: %s", type(exc).__name__)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _purge_expired_tv_exchange_codes(now: float | None = None) -> None:
    checked_at = time.time() if now is None else now
    expired = [key for key, entry in _TV_EXCHANGE_CODES.items() if entry.get("expires_at", 0) <= checked_at]
    for key in expired:
        _TV_EXCHANGE_CODES.pop(key, None)


def _mint_tv_exchange_code(user_id: int, now: float | None = None) -> str:
    """签发一次性交换码（10 分钟、单次使用）。只存哈希与指纹，不落明文。"""
    issued_at = time.time() if now is None else now
    expires_at = issued_at + TV_EXCHANGE_CODE_TTL_SECONDS
    redis_client = _tv_shared_state_client()
    if redis_client is None:
        _purge_expired_tv_exchange_codes(issued_at)
        code = secrets.token_urlsafe(24)
        _TV_EXCHANGE_CODES[hashlib.sha256(code.encode("utf-8")).hexdigest()] = {
            "expires_at": expires_at,
            "created_by": user_id,
            "fingerprint": _tv_fingerprint(code),
        }
        return code

    # 极低概率随机碰撞时重试；Redis 只保存 code 哈希 key 与最小元数据。
    for _ in range(3):
        code = secrets.token_urlsafe(24)
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        payload = json.dumps(
            {
                "expires_at": expires_at,
                "created_by": int(user_id),
                "fingerprint": _tv_fingerprint(code),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        ttl = max(1, int(expires_at - time.time()) + 1)
        try:
            if redis_client.set(
                _tv_exchange_redis_key(code_hash),
                payload,
                nx=True,
                ex=ttl,
            ):
                return code
        except Exception as exc:
            logger.error("TV exchange state write failed: %s", type(exc).__name__)
            raise TVSecurityStateUnavailable("TV shared security state unavailable") from exc
    raise TVSecurityStateUnavailable("TV exchange code allocation failed")


def _consume_tv_exchange_code(code: str) -> str:
    """核销交换码（取出即作废，保证单次使用）。返回 'ok' / 'expired' / 'unknown'。"""
    key = hashlib.sha256(code.encode("utf-8")).hexdigest()
    redis_client = _tv_shared_state_client()
    if redis_client is None:
        entry = _TV_EXCHANGE_CODES.pop(key, None)  # 先取出（取即作废），再惰性清理其它过期码
        _purge_expired_tv_exchange_codes()
    else:
        try:
            encoded = redis_client.getdel(_tv_exchange_redis_key(key))
            entry = json.loads(encoded) if encoded else None
        except Exception as exc:
            logger.error("TV exchange state consume failed: %s", type(exc).__name__)
            raise TVSecurityStateUnavailable("TV shared security state unavailable") from exc
    if entry is None:
        return "unknown"
    if entry.get("expires_at", 0) <= time.time():
        return "expired"
    return "ok"


def _tv_access_throttle_key(request: Request, user_id: int) -> int:
    """限流计数 key：uid 单维度。

    [2026-07-22 R2 修复] 不再掺 X-Forwarded-For——nginx $proxy_add_x_forwarded_for
    为追加模式，第一段可被请求方伪造：轮换 XFF 即绕过限流且 _TV_ACCESS_FAILURES
    无界增长。uid 已区分 NAT 后不同管理员，ip 分量无增益。
    """
    return int(user_id)


def _tv_access_cooldown_remaining(key: int, now: float | None = None) -> int:
    """剩余冷却秒数（0 = 未在冷却中）。失败计数在触发冷却时已清零，
    冷却结束后自然进入新一轮计数窗口；累计失败中的条目不得 pop。
    [2026-07-22 R2] 惰性清理死条目（冷却已过期且计数为 0）防内存漂移。"""
    redis_client = _tv_shared_state_client()
    if redis_client is not None:
        _, cooldown_key = _tv_failure_redis_keys(key)
        try:
            ttl_ms = int(redis_client.pttl(cooldown_key))
        except Exception as exc:
            logger.error("TV access cooldown read failed: %s", type(exc).__name__)
            raise TVSecurityStateUnavailable("TV shared security state unavailable") from exc
        if ttl_ms <= 0:
            return 0
        return max(1, (ttl_ms + 999) // 1000)

    checked_at = time.time() if now is None else now
    if len(_TV_ACCESS_FAILURES) > 1000:
        for dead_key, dead in list(_TV_ACCESS_FAILURES.items()):
            if not dead.get("count") and float(dead.get("cooldown_until", 0.0)) <= checked_at:
                _TV_ACCESS_FAILURES.pop(dead_key, None)
    entry = _TV_ACCESS_FAILURES.get(key)
    if not entry:
        return 0
    remaining = float(entry.get("cooldown_until", 0.0)) - checked_at
    if remaining <= 0:
        return 0
    return max(1, int(remaining) + (1 if remaining % 1 else 0))


def _tv_access_record_failure(key: int, now: float | None = None) -> None:
    redis_client = _tv_shared_state_client()
    if redis_client is not None:
        failure_key, cooldown_key = _tv_failure_redis_keys(key)
        try:
            redis_client.eval(
                _TV_RECORD_FAILURE_LUA,
                2,
                failure_key,
                cooldown_key,
                TV_ACCESS_MAX_FAILURES,
                TV_ACCESS_COOLDOWN_SECONDS,
                _TV_FAILURE_COUNTER_TTL_SECONDS,
            )
            return
        except Exception as exc:
            logger.error("TV access failure state write failed: %s", type(exc).__name__)
            raise TVSecurityStateUnavailable("TV shared security state unavailable") from exc

    checked_at = time.time() if now is None else now
    entry = _TV_ACCESS_FAILURES.setdefault(key, {"count": 0, "cooldown_until": 0.0})
    if float(entry.get("cooldown_until", 0.0)) > checked_at:
        return  # 冷却期内的请求已在入口 429 拦截，不重复计数
    entry["count"] = int(entry.get("count", 0)) + 1
    if entry["count"] >= TV_ACCESS_MAX_FAILURES:
        entry["cooldown_until"] = checked_at + TV_ACCESS_COOLDOWN_SECONDS
        entry["count"] = 0  # 冷却结束后重新计 10 次窗口


def _tv_access_record_success(key: int) -> None:
    redis_client = _tv_shared_state_client()
    if redis_client is None:
        _TV_ACCESS_FAILURES.pop(key, None)
        return
    failure_key, cooldown_key = _tv_failure_redis_keys(key)
    try:
        redis_client.delete(failure_key, cooldown_key)
    except Exception as exc:
        logger.error("TV access success state clear failed: %s", type(exc).__name__)
        raise TVSecurityStateUnavailable("TV shared security state unavailable") from exc


def _tv_grant_session_response(request: Request, user_id: int, request_id: str) -> JSONResponse:
    session_token = _create_tv_session(user_id)
    response = _tv_response(
        status_code=200,
        code="TV_ACCESS_GRANTED",
        message="大屏访问已授权",
        request_id=request_id,
        success=True,
    )
    response.set_cookie(
        key=TV_SESSION_COOKIE,
        value=session_token,
        max_age=TV_SESSION_TTL_SECONDS,
        path="/api/tv",
        secure=_tv_secure_cookie(request),
        httponly=True,
        samesite="strict",
    )
    return response
@router.post("/api/tv/access")
async def grant_tv_access(request: Request):
    request_id = _tv_request_id(request)
    user_id, auth_error = _tv_admin_id(request)
    if auth_error is not None or user_id is None:
        return auth_error
    try:
        body = await request.json()
    except Exception:
        body = None
    password = body.get("password") if isinstance(body, dict) else None
    if not isinstance(password, str) or not password or len(password) > 128:
        return _tv_response(
            status_code=400,
            code="TV_ACCESS_INPUT_INVALID",
            message="请输入有效的大屏访问密码",
            request_id=request_id,
        )
    # 失败限流（P2-2）：冷却期内一律 429 + Retry-After，不给密码猜测任何反馈
    throttle_key = _tv_access_throttle_key(request, user_id)
    try:
        cooldown_remaining = _tv_access_cooldown_remaining(throttle_key)
    except TVSecurityStateUnavailable:
        return _tv_security_state_unavailable_response(request_id)
    if cooldown_remaining > 0:
        logger.info(
            "TV access rate limited uid=%s request_id=%s retry_after=%ss",
            user_id, request_id, cooldown_remaining,
        )
        limited = _tv_response(
            status_code=429,
            code="TV_ACCESS_RATE_LIMITED",
            message="尝试次数过多，请稍后重试",
            request_id=request_id,
        )
        limited.headers["Retry-After"] = str(cooldown_remaining)
        return limited
    try:
        password_valid = await asyncio.to_thread(_verify_tv_password, password)
    except Exception as exc:
        logger.error("TV access verifier unavailable: %s", type(exc).__name__)
        return _tv_response(
            status_code=503,
            code="TV_ACCESS_UNAVAILABLE",
            message="大屏访问服务暂时不可用，请稍后重试",
            request_id=request_id,
        )
    if not password_valid:
        try:
            _tv_access_record_failure(throttle_key)
        except TVSecurityStateUnavailable:
            return _tv_security_state_unavailable_response(request_id)
        logger.info("TV access denied (bad password) uid=%s request_id=%s", user_id, request_id)
        return _tv_response(
            status_code=403,
            code="TV_ACCESS_DENIED",
            message="访问密码不正确",
            request_id=request_id,
        )

    try:
        _tv_access_record_success(throttle_key)  # 成功登录清零失败计数
    except TVSecurityStateUnavailable:
        return _tv_security_state_unavailable_response(request_id)
    try:
        return _tv_grant_session_response(request, user_id, request_id)
    except Exception as exc:
        logger.error("TV access session creation failed: %s request_id=%s", type(exc).__name__, request_id)
        return _tv_response(
            status_code=503,
            code="TV_ACCESS_UNAVAILABLE",
            message="大屏访问服务暂时不可用，请稍后重试",
            request_id=request_id,
        )


@router.post("/api/tv/access/exchange")
async def exchange_tv_access(request: Request):
    """URL token 一次性交换（Review-CTO D4）：一次性交换码或 legacy token → HttpOnly 会话。
    交换成功后前端必须抹掉地址栏 token（重定向无 token URL）；旧 token 全程只读，日志只记指纹。"""
    request_id = _tv_request_id(request)
    user_id, auth_error = _tv_admin_id(request)
    if auth_error is not None or user_id is None:
        return auth_error
    try:
        body = await request.json()
    except Exception:
        body = None
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token or len(token) > 256:
        return _tv_response(
            status_code=400,
            code="TV_ACCESS_INPUT_INVALID",
            message="安全链接参数无效",
            request_id=request_id,
        )

    fingerprint = _tv_fingerprint(token)
    source = None
    try:
        exchange_status = _consume_tv_exchange_code(token)
    except TVSecurityStateUnavailable:
        return _tv_security_state_unavailable_response(request_id)
    if exchange_status == "ok":
        source = "exchange_code"
    elif exchange_status == "expired":
        logger.info("TV exchange denied (expired code) fp=%s uid=%s request_id=%s", fingerprint, user_id, request_id)
        return _tv_response(
            status_code=403,
            code="TV_ACCESS_DENIED",
            message="安全链接已过期，请让管理员重新生成",
            request_id=request_id,
        )
    elif _tv_legacy_token_enabled():
        legacy_token = _read_legacy_tv_token()
        if legacy_token and hmac.compare_digest(token, legacy_token):
            source = "legacy_token"
    if source is None:
        logger.info("TV exchange denied (unknown token) fp=%s uid=%s request_id=%s", fingerprint, user_id, request_id)
        return _tv_response(
            status_code=403,
            code="TV_ACCESS_DENIED",
            message="安全链接无效或已被使用，请让管理员重新生成或改用访问密码",
            request_id=request_id,
        )

    try:
        response = _tv_grant_session_response(request, user_id, request_id)
    except Exception as exc:
        logger.error("TV exchange session creation failed: %s request_id=%s", type(exc).__name__, request_id)
        return _tv_response(
            status_code=503,
            code="TV_ACCESS_UNAVAILABLE",
            message="大屏访问服务暂时不可用，请稍后重试",
            request_id=request_id,
        )
    logger.info("TV exchange granted source=%s fp=%s uid=%s request_id=%s", source, fingerprint, user_id, request_id)
    return response


@router.get("/api/tv/access/session")
async def get_tv_access_session(request: Request):
    request_id = _tv_request_id(request)
    _, session_error = _require_tv_session(request)
    if session_error is not None:
        return session_error
    return _tv_response(
        status_code=200,
        code="TV_ACCESS_SESSION_ACTIVE",
        message="大屏访问会话有效",
        request_id=request_id,
        success=True,
        extra={"authenticated": True},
    )


async def _cached_tv_dashboard_data() -> dict:
    now = time.time()
    if _tv_cache["data"] and now < _tv_cache["expires_at"]:
        return _tv_cache["data"]
    data = await asyncio.to_thread(_compute_tv_dashboard)
    _tv_cache["data"] = data
    _tv_cache["expires_at"] = now + TV_CACHE_TTL
    return data


@router.get("/api/tv/dashboard/auth")
async def authenticated_tv_dashboard(request: Request):
    request_id = _tv_request_id(request)
    _, session_error = _require_tv_session(request)
    if session_error is not None:
        return session_error
    try:
        data = dict(await _cached_tv_dashboard_data())
    except Exception as exc:
        logger.error("TV dashboard load failed: %s", type(exc).__name__)
        return _tv_response(
            status_code=503,
            code="TV_DASHBOARD_UNAVAILABLE",
            message="大屏数据暂时不可用，请稍后重试",
            request_id=request_id,
        )
    data.update({
        "success": True,
        "status": "success",
        "code": "TV_DASHBOARD_READY",
        "message": "大屏数据已加载",
        "request_id": request_id,
    })
    return JSONResponse(
        content=data,
        headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
    )


@router.get("/api/tv/dashboard")
async def tv_dashboard(token: str = Query(None), request: Request = None):
    """电视大屏数据接口（legacy URL token · 只读兼容 · D4 观察一个发布周期后移除）。

    新链路：POST /api/tv/access/exchange → GET /api/tv/dashboard/auth（HttpOnly 会话）。
    本端点不做会话交换：会话 uid 绑定要求管理员 JWT，而本端点免 JWT（中间件白名单），
    匿名会话无法通过 dashboard/auth 的管理员校验，故只保留只读数据兼容 + 响应头引导迁移。
    """
    legacy_headers = {"X-TV-Legacy-Token": "deprecated"}
    if not _tv_legacy_token_enabled():
        raise HTTPException(403, "legacy URL token 已停用，请使用一次性安全链接或访问密码", headers=legacy_headers)

    # 鉴权：检查 token
    if not token:
        raise HTTPException(403, "缺少 token 参数", headers=legacy_headers)

    valid_token = _read_legacy_tv_token()

    # [GEO-R1-CAN-106] 未配置 token 时必须 fail-closed，禁止回退到可推导常量
    # （原实现用固定字符串的 md5 前 16 位作默认，任何人可离线算出并绕过鉴权）
    if not valid_token:
        raise HTTPException(403, "大屏 token 未配置", headers=legacy_headers)

    # [GEO-R1-CAN-106] 恒定时间比较，避免时序侧信道
    if not hmac.compare_digest(str(token), str(valid_token)):
        logger.info("TV legacy dashboard denied fp=%s", _tv_fingerprint(str(token)))
        raise HTTPException(403, "token 无效", headers=legacy_headers)

    logger.info("TV legacy dashboard served (read-only compat) fp=%s", _tv_fingerprint(str(token)))
    try:
        data = await _cached_tv_dashboard_data()
    except Exception as exc:
        logger.error("TV legacy dashboard load failed: %s", type(exc).__name__)
        raise HTTPException(503, "大屏数据暂时不可用", headers=legacy_headers)
    return JSONResponse(content=data, headers=legacy_headers)


def _compute_tv_dashboard() -> dict:
    """
    电视大屏数据 — 只放决定生死的指标：
    1. 用户漏斗（注册→建档→面试→画像Lv3→付费）
    2. 内容产出（选题→脚本→发布）
    3. 营收成本（真实 feature_pricing 计算）
    4. 用户活跃（在线/操作/停留）
    5. 地理分布（真实 IP 或虚拟）
    """
    from db.connection import get_connection

    conn = get_connection()
    cur = conn.cursor()

    today = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_min_ago = datetime.now(CST) - timedelta(minutes=30)
    week_ago = today - timedelta(days=7)
    month_start = today.replace(day=1)

    try:
        cur.execute("CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.commit()

        def _safe(sql, params=None, default=None):
            """安全查询，失败返回默认值"""
            try:
                cur.execute(sql, params or ())
                return cur.fetchall() if default == 'rows' else cur.fetchone()
            except Exception:
                conn.rollback()
                return [] if default == 'rows' else (default or {})

        # ========== 1. 用户漏斗（生死指标）==========
        total_users = (_safe("SELECT COUNT(*) as c FROM users WHERE is_active=1") or {}).get("c", 0)
        today_register = (_safe("SELECT COUNT(*) as c FROM users WHERE created_at >= %s", (today,)) or {}).get("c", 0)
        online_users = (_safe("SELECT COUNT(*) as c FROM users WHERE last_login_at >= %s", (thirty_min_ago,)) or {}).get("c", 0)
        week_active = (_safe("SELECT COUNT(*) as c FROM users WHERE last_login_at >= %s", (week_ago,)) or {}).get("c", 0)

        # 建档率：有 client_profiles 的用户
        profiles_count = 0
        try:
            cur.execute("SELECT COUNT(DISTINCT uc.user_id) as c FROM user_clients uc JOIN client_profiles cp ON cp.brand_id = uc.brand_id WHERE cp.is_deleted=0")
            profiles_count = cur.fetchone()["c"]
        except Exception:
            conn.rollback()

        # 面试完成数
        interview_done = 0
        try:
            cur.execute("SELECT COUNT(*) as c FROM interview_sessions WHERE is_complete=true")
            interview_done = cur.fetchone()["c"]
        except Exception:
            conn.rollback()

        # 画像 Lv3+ 数（personality_profile 非空且有 level>=3）
        lv3_count = 0
        try:
            cur.execute("SELECT COUNT(*) as c FROM client_profiles WHERE personality_profile IS NOT NULL AND personality_profile <> '' AND personality_profile <> '{}' AND is_deleted=0")
            lv3_count = cur.fetchone()["c"]
        except Exception:
            conn.rollback()

        # 语料数
        corpus_count = 0
        try:
            cur.execute("SELECT COUNT(*) as c FROM profile_corpus")
            corpus_count = cur.fetchone()["c"]
        except Exception:
            conn.rollback()

        # 付费用户数
        paid_count = (_safe("SELECT COUNT(*) as c FROM user_wallets WHERE total_recharged > 0") or {}).get("c", 0)

        funnel = {
            "total_users": total_users,
            "today_register": today_register,
            "online_users": online_users,
            "week_active": week_active,
            "week_active_rate": round(week_active / max(total_users, 1) * 100),
            "profiles_count": profiles_count,
            "profile_rate": round(profiles_count / max(total_users, 1) * 100),
            "interview_done": interview_done,
            "interview_rate": round(interview_done / max(total_users, 1) * 100),
            "lv3_count": lv3_count,
            "lv3_rate": round(lv3_count / max(total_users, 1) * 100),
            "corpus_count": corpus_count,
            "paid_count": paid_count,
            "paid_rate": round(paid_count / max(total_users, 1) * 100),
        }

        # ========== 2. 内容产出 ==========
        topics_count = 0
        scripts_count = 0
        articles_count = 0
        published_count = 0
        try:
            cur.execute("SELECT COUNT(*) as c FROM social_topics")
            topics_count = cur.fetchone()["c"]
        except Exception:
            conn.rollback()
        try:
            cur.execute("SELECT COUNT(*) as c FROM social_scripts")
            scripts_count = cur.fetchone()["c"]
        except Exception:
            conn.rollback()
        try:
            cur.execute("SELECT COUNT(*) as c FROM article_generations")
            articles_count = cur.fetchone()["c"]
        except Exception:
            conn.rollback()
        try:
            cur.execute("SELECT COUNT(*) as c FROM mhz_publish_order_items WHERE status='published'")
            published_count = cur.fetchone()["c"]
        except Exception:
            conn.rollback()

        content = {
            "topics": topics_count,
            "scripts": scripts_count,
            "articles": articles_count,
            "published": published_count,
        }

        # ========== 3. 营收成本 ==========
        month_revenue = 0
        try:
            cur.execute("""
                SELECT COALESCE(SUM(amount_cents), 0) as r
                FROM recharge_orders
                WHERE payment_status='paid'
                  AND COALESCE(paid_at, created_at) >= %s
            """, (month_start,))
            month_revenue = cur.fetchone()["r"] / 100
        except Exception:
            conn.rollback()

        month_cost = 0
        try:
            cur.execute("""
                SELECT pt.feature_code, COUNT(*) as count, fp.cost_compute
                FROM point_transactions pt LEFT JOIN feature_pricing fp ON pt.feature_code = fp.feature_code
                WHERE pt.type = 'consume' AND pt.created_at >= %s
                GROUP BY pt.feature_code, fp.cost_compute
            """, (month_start,))
            for r in cur.fetchall():
                month_cost += float(r["cost_compute"] or 0) * r["count"]
            month_cost = round(month_cost, 2)
        except Exception:
            conn.rollback()

        today_revenue = 0
        try:
            cur.execute("""
                SELECT COALESCE(SUM(amount_cents), 0) as r
                FROM recharge_orders
                WHERE payment_status='paid'
                  AND COALESCE(paid_at, created_at) >= %s
            """, (today,))
            today_revenue = cur.fetchone()["r"] / 100
        except Exception:
            conn.rollback()

        finance = {
            "today_revenue": today_revenue,
            "month_revenue": month_revenue,
            "month_cost": month_cost,
            "month_profit": round(month_revenue - month_cost, 2),
            "profit_rate": round((month_revenue - month_cost) / max(month_revenue, 0.01) * 100) if month_revenue > 0 else 0,
        }

        # ========== 4. 用户地理分布（城市级别，支持放大）==========
        geo_distribution = []
        try:
            cur.execute("""
                SELECT register_province as province, register_city as city, COUNT(*) as count,
                       COUNT(*) FILTER (WHERE last_login_at >= CURRENT_DATE - INTERVAL '7 days') as active
                FROM users
                WHERE register_province IS NOT NULL AND register_province <> ''
                GROUP BY register_province, register_city
                ORDER BY count DESC LIMIT 50
            """)
            geo_distribution = [{"province": r["province"], "city": r.get("city") or "", "count": r["count"], "active": r["active"]} for r in cur.fetchall()]
        except Exception:
            conn.rollback()

        # ========== 5. 活跃时段热力图 ==========
        activity_heatmap = []
        try:
            cur.execute("""
                SELECT EXTRACT(DOW FROM created_at)::int as dow,
                       EXTRACT(HOUR FROM created_at)::int as hour,
                       COUNT(*) as count
                FROM point_transactions WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY dow, hour
            """)
            activity_heatmap = [{"dow": r["dow"], "hour": r["hour"], "count": r["count"]} for r in cur.fetchall()]
        except Exception:
            conn.rollback()

        # 虚拟热力图（正式上线后删除）
        if len(activity_heatmap) < 5:
            import random
            random.seed(42)
            activity_heatmap = []
            for dow in range(7):
                for hour in range(24):
                    base = 3 if dow in (0, 6) else 8
                    if 9 <= hour <= 12 or 14 <= hour <= 17: base *= 3
                    elif 19 <= hour <= 22: base *= 2
                    elif hour < 7 or hour > 23: base = 1
                    c = random.randint(0, base)
                    if c > 0: activity_heatmap.append({"dow": dow, "hour": hour, "count": c})

        # ========== 6. 营收成本趋势 ==========
        revenue_trend = []
        try:
            thirty_days_ago = today - timedelta(days=30)
            cur.execute("""
                SELECT DATE(COALESCE(paid_at, created_at)) as date, SUM(amount_cents)/100.0 as revenue
                FROM recharge_orders
                WHERE payment_status='paid'
                  AND COALESCE(paid_at, created_at) >= %s
                GROUP BY DATE(COALESCE(paid_at, created_at))
            """, (thirty_days_ago,))
            rev_map = {str(r["date"]): float(r["revenue"]) for r in cur.fetchall()}
            try:
                cost_map = get_api_cost_daily(cur, thirty_days_ago)
            except Exception as e:
                logger.warning(f"TV 成本趋势统一查询失败: {e}")
                conn.rollback()
                cost_map = {}
            for d in sorted(set(list(rev_map.keys()) + list(cost_map.keys()))):
                revenue_trend.append({"date": d, "revenue": rev_map.get(d, 0), "cost": cost_map.get(d, 0)})
        except Exception:
            conn.rollback()

        # 虚拟趋势（正式上线后删除）
        if not revenue_trend:
            import random
            random.seed(99)
            for i in range(30):
                d = (today - timedelta(days=30 - i)).strftime('%Y-%m-%d')
                revenue_trend.append({"date": d, "revenue": random.randint(0, 5000) if random.random() > 0.4 else 0, "cost": random.randint(20, 150)})

        # ========== 7. 功能使用 TOP ==========
        top_features = []
        try:
            cur.execute("""
                SELECT pt.feature_code, fp.feature_name, COUNT(*) as count
                FROM point_transactions pt LEFT JOIN feature_pricing fp ON pt.feature_code = fp.feature_code
                WHERE pt.type = 'consume' GROUP BY pt.feature_code, fp.feature_name ORDER BY count DESC LIMIT 7
            """)
            top_features = [{"code": r["feature_code"], "name": r["feature_name"] or r["feature_code"], "count": r["count"]} for r in cur.fetchall()]
        except Exception:
            conn.rollback()

        # ========== 8. 实时动态 ==========
        live_feed = []
        try:
            cur.execute("""SELECT 'diagnosis' as type, dr.brand_name as detail, dr.created_at as time, u.display_name as user_name
                FROM diagnosis_records dr LEFT JOIN brands b ON dr.brand_name=b.name LEFT JOIN user_clients uc ON uc.brand_id=b.id LEFT JOIN users u ON uc.user_id=u.id
                ORDER BY dr.created_at DESC LIMIT 5""")
            for r in cur.fetchall():
                live_feed.append({"type": "diagnosis", "user": r.get("user_name") or "系统", "detail": f"GEO诊断: {r['detail']}", "time": r["time"].strftime("%H:%M:%S") if r.get("time") else ""})
            cur.execute("SELECT display_name, register_city, created_at FROM users ORDER BY created_at DESC LIMIT 5")
            for r in cur.fetchall():
                city = r.get("register_city") or ""
                city_str = f" ({city})" if city else ""
                live_feed.append({"type": "register", "user": r["display_name"], "detail": "新用户注册" + city_str, "time": r["created_at"].strftime("%H:%M:%S") if r.get("created_at") else ""})
            cur.execute("SELECT u.display_name, ro.amount_cents, ro.created_at FROM recharge_orders ro JOIN users u ON ro.user_id=u.id WHERE ro.payment_status='paid' ORDER BY ro.created_at DESC LIMIT 3")
            for r in cur.fetchall():
                live_feed.append({"type": "recharge", "user": r["display_name"], "detail": f"充值 ¥{r['amount_cents']/100}", "time": r["created_at"].strftime("%H:%M:%S") if r.get("created_at") else ""})
            live_feed.sort(key=lambda x: x.get("time", ""), reverse=True)
            live_feed = live_feed[:15]
        except Exception:
            conn.rollback()

        # ========== 9. 性能监控 ==========
        import os
        system_stats = {"cpu_percent": 0, "mem_total_gb": 0, "mem_used_gb": 0, "mem_percent": 0, "proc_mem_mb": 0}
        try:
            # 从 /proc 读取（Docker Linux 容器）
            with open('/proc/meminfo') as f:
                meminfo = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        meminfo[parts[0].rstrip(':')] = int(parts[1])
            mem_total_kb = meminfo.get('MemTotal', 0)
            mem_avail_kb = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
            mem_used_kb = mem_total_kb - mem_avail_kb
            system_stats["mem_total_gb"] = round(mem_total_kb / (1024**2), 1)
            system_stats["mem_used_gb"] = round(mem_used_kb / (1024**2), 1)
            system_stats["mem_percent"] = round(mem_used_kb / max(mem_total_kb, 1) * 100)

            # CPU（简化：读 /proc/loadavg）
            with open('/proc/loadavg') as f:
                load1 = float(f.read().split()[0])
            cpu_count = os.cpu_count() or 1
            system_stats["cpu_percent"] = min(round(load1 / cpu_count * 100), 100)

            # 进程内存
            try:
                with open(f'/proc/{os.getpid()}/status') as f:
                    for line in f:
                        if line.startswith('VmRSS:'):
                            system_stats["proc_mem_mb"] = round(int(line.split()[1]) / 1024)
                            break
            except Exception:
                pass
            # 并发连接数：统计当前进程的 ESTABLISHED TCP 连接
            try:
                tcp_count = 0
                # /proc/net/tcp 每行一个连接，state=01 是 ESTABLISHED
                with open('/proc/net/tcp') as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 4 and parts[3] == '01':
                            tcp_count += 1
                # 也检查 tcp6
                try:
                    with open('/proc/net/tcp6') as f6:
                        for line in f6:
                            parts = line.split()
                            if len(parts) >= 4 and parts[3] == '01':
                                tcp_count += 1
                except Exception:
                    pass
                system_stats["concurrent"] = tcp_count
            except Exception:
                system_stats["concurrent"] = 0
        except Exception:
            pass  # Windows / 无 /proc 环境

        # DB 连接池状态
        db_stats = {}
        try:
            cur.execute("SELECT COUNT(*) as c FROM pg_stat_activity WHERE datname = current_database()")
            db_stats["active_connections"] = cur.fetchone()["c"]
            cur.execute("SELECT setting::int as v FROM pg_settings WHERE name = 'max_connections'")
            db_stats["max_connections"] = cur.fetchone()["v"]
        except Exception:
            conn.rollback()
            db_stats = {"active_connections": 0, "max_connections": 100}

        # API 调用统计（最近1小时）
        api_stats = {}
        try:
            one_hour_ago = datetime.now(CST) - timedelta(hours=1)
            api_stats["calls_1h"] = get_api_call_count(cur, one_hour_ago)
        except Exception:
            conn.rollback()
            api_stats["calls_1h"] = 0

        # 定时任务状态
        scheduler_info = []
        try:
            from scheduler import scheduler as _sched
            if _sched and _sched.running:
                for job in _sched.get_jobs():
                    next_run = job.next_run_time
                    scheduler_info.append({
                        "name": job.name or job.id,
                        "next_run": next_run.strftime("%H:%M:%S") if next_run else "—",
                        "status": "active",
                    })
        except Exception:
            pass

        performance = {
            "system": system_stats,
            "db": db_stats,
            "api": api_stats,
            "scheduler": scheduler_info,
        }

        # ========== 10. 代发业务 ==========
        publishing = {"total": 0, "published": 0, "rejected": 0, "pending": 0, "queued": 0, "today_published": 0, "session_valid": None}
        try:
            cur.execute("SELECT status, COUNT(*) as c FROM mhz_publish_order_items GROUP BY status")
            for r in cur.fetchall():
                publishing[r["status"]] = r["c"]
                publishing["total"] += r["c"]
            cur.execute("SELECT COUNT(*) as c FROM mhz_publish_order_items WHERE status='published' AND COALESCE(published_at, submitted_at) >= %s", (today,))
            publishing["today_published"] = cur.fetchone()["c"]
            # Session 状态
            try:
                cur.execute("SELECT value FROM mhz_config WHERE key = 'phpsessid'")
                row = cur.fetchone()
                publishing["session_valid"] = bool(row and row["value"])
            except Exception:
                conn.rollback()
        except Exception:
            conn.rollback()

        # ========== 在线用户看板（近1分钟活跃）==========
        # ⚠️ 失败必须 rollback，否则整个 tv_dashboard 查询被污染
        online_users_list = []
        try:
            # 先检查 last_active_at 字段是否存在，避免列不存在时报错污染事务
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'users' AND column_name IN ('last_active_at', 'current_path')
            """)
            _cols = {r["column_name"] for r in cur.fetchall()}
            has_last_active = "last_active_at" in _cols
            has_current_path = "current_path" in _cols

            if has_last_active:
                # 动态构建 SELECT，current_path 字段可能未建
                _cp_select = "u.current_path" if has_current_path else "NULL AS current_path"
                cur.execute(f"""
                    SELECT u.id, u.username, u.display_name, u.phone,
                           u.last_active_at, {_cp_select},
                           (SELECT json_build_object(
                                       'action', action,
                                       'module', module,
                                       'summary', summary,
                                       'entity_type', entity_type,
                                       'created_at', created_at
                                   )
                            FROM audit_logs
                            WHERE user_id = u.id
                              AND created_at > NOW() - INTERVAL '10 minutes'
                            ORDER BY created_at DESC
                            LIMIT 1) AS last_action
                    FROM users u
                    WHERE u.last_active_at > NOW() - INTERVAL '1 minute'
                      AND u.is_active = 1
                    ORDER BY u.last_active_at DESC
                    LIMIT 50
                """)
                for r in cur.fetchall():
                    phone = r.get("phone") or ""
                    phone_masked = (phone[:3] + "****" + phone[-4:]) if len(phone) >= 7 else phone
                    name = r.get("display_name") or phone_masked or r.get("username") or f"用户{r['id']}"
                    online_users_list.append({
                        "id": r["id"],
                        "name": name,
                        "phone_masked": phone_masked,
                        "current_path": r.get("current_path") or "",
                        "last_active_at": r["last_active_at"].isoformat() if r.get("last_active_at") else None,
                        "last_action": r.get("last_action"),
                    })
        except Exception as _e:
            try:
                conn.rollback()
            except Exception:
                pass
            import logging as _lg
            _lg.getLogger("GEO-TV").warning(f"查询在线用户失败(忽略): {_e}")

        return {
            "status": "success",
            "funnel": funnel,
            "content": content,
            "finance": finance,
            "geo_distribution": geo_distribution,
            "activity_heatmap": activity_heatmap,
            "revenue_trend": revenue_trend,
            "top_features": top_features,
            "live_feed": live_feed,
            "performance": performance,
            "publishing": publishing,
            "online_users": online_users_list,
            "updated_at": datetime.now(CST).isoformat(),
        }

    finally:
        conn.close()


@router.post("/api/admin/tv-token")
async def generate_tv_token(request: Request):
    """管理员签发大屏一次性安全链接（Review-CTO D4：不再写长期 token 到 system_config）。

    交换码 10 分钟内有效、单次使用；已存在的 system_config.tv_dashboard_token 保持只读兼容
    （可在 /api/tv/access/exchange 换会话），本端点不再新增/改写它。日志只记指纹不记明文。
    """
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict):
        raise HTTPException(401, "请先登录管理员账号")
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")

    raw_user_id = user.get("user_id") or current_user_id(user)
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        raise HTTPException(401, "请重新登录管理员账号")

    try:
        code = _mint_tv_exchange_code(user_id)
    except TVSecurityStateUnavailable:
        return _tv_security_state_unavailable_response(_tv_request_id(request))
    logger.info("TV exchange code minted fp=%s by_uid=%s", _tv_fingerprint(code), user_id)
    return {
        "status": "success",
        "exchange_code": code,
        "url": f"/tv?token={code}",
        "expires_in": TV_EXCHANGE_CODE_TTL_SECONDS,
        "message": "大屏安全链接已生成：10 分钟内有效、仅可使用一次。在电视浏览器打开该链接即可完成登录。",
    }
