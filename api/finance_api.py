"""
财务中心 API · GEO CTO-15.23 · 2026-05-30

投资人/审计级财务中心后端 · 全只读聚合(opex CRUD 例外)· 0 改扣费逻辑(billing.py 红线)。

口径(老板 2026-05-30 拍板 D1-D6):
  D1 双口径并列 · 默认权责(accrual)· 现金(cash)并列
  D2 确认收入只认 paid_points 消费(bonus/commission 不算 · 防虚增)
  D3 代理渠道平台收入只认 agent_revenue_ledger.factory_cents(玩法B 客户钱不过平台)
  D4 OpEx 走 operating_expenses 表(admin 手录 · finance_db.py)
  D5 社媒订阅(user_social_subscriptions.price_locked_yuan)并入确认收入
  D6 退款冲减当期确认收入 + 计现金流出(point_transactions type='refund')

所有表/列经 SQL 4 维核验实证(2026-05-30):
  - llm_call_log.estimated_cost 单位=元(NUMERIC · 不 /100)
  - mhz_synced_orders.price 单位=元 · platform_cost_cents 单位=分(优先后者)
  - point_transactions.amount 消费为负(ABS)· type∈{recharge,consume,commission,bonus,refund}
  - agent_revenue_ledger 4 层 · factory_cents 为成本基准 · status∈{frozen,settled,cancelled}
  - clawback 表真名 commission_clawback_pending
鉴权:仅 admin。
"""

import asyncio
import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.api_costs import (
    get_api_cost_by_caller,
    get_api_cost_by_platform,
    get_api_cost_summary,
)
from db.finance_db import (
    list_operating_expenses,
    create_operating_expense,
    update_operating_expense,
    delete_operating_expense,
    sum_opex_by_category,
    VALID_CATEGORIES,
    CATEGORY_LABELS,
)
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-Finance")

router = APIRouter(prefix="/api/admin/finance", tags=["管理员·财务中心"])

# 会计折算率(SSOT · 1元 = 130 积分 · 故意非整数阻断心算)
POINTS_PER_YUAN = 130


# ============================================================
# 鉴权 / DB helper(照搬 admin_llm_cost_api 模式)
# ============================================================

def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _query_db(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        # 查询失败必须 rollback · 否则 aborted 连接归还池会污染下个 request
        # (_safe_scalar 故意容忍失败 · 这条路径真实可达)
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _safe_scalar(sql: str, params: tuple = ()) -> float:
    """聚合单值 · 任一表/列缺失/查询失败返回 0(只读展示绝不让整端点 500)。

    约定:SQL 必须 SELECT ... AS v。
    """
    try:
        rows = _query_db(sql, params)
        if not rows:
            return 0
        v = rows[0].get("v")
        return v if v is not None else 0
    except Exception as e:
        logger.warning(f"[finance] 聚合查询失败(返回0): {e} · sql={sql[:80]}")
        return 0


def _safe_rows(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """多行查询 · 失败返回 []。"""
    try:
        return _query_db(sql, params)
    except Exception as e:
        logger.warning(f"[finance] 多行查询失败(返回[]): {e} · sql={sql[:80]}")
        return []


def _llm_cost(since: datetime, until: datetime) -> float:
    """LLM+监测 总成本(元)· 复用 api_costs union(已含 monitoring_token_usage)。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        s = get_api_cost_summary(cur, since, until=until, limit=0)
        return float(s.get("total_cost") or 0)
    except Exception as e:
        logger.warning(f"[finance] LLM 成本查询失败(返回0): {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0.0
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def _run(fn):
    """async 包 sync DB(WORKERS=1 防阻塞 event loop)。"""
    return await asyncio.to_thread(fn)


# ============================================================
# 期间解析
# ============================================================

def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1)


def _add_months(dt: datetime, n: int) -> datetime:
    m = dt.month - 1 + n
    y = dt.year + m // 12
    m = m % 12 + 1
    return datetime(y, m, 1)


def _resolve_period(period: str, start: Optional[str], end: Optional[str]) -> Dict[str, Any]:
    """返回期间 + 上一可比期 + opex 归属月范围。

    since/until: 期间 [since, until)
    prev_since/prev_until: 上一可比期(同环比)
    opex_start/opex_end: 'YYYY-MM-01' 字符串(含端点)
    """
    # [复审修正 🔴-4] 容器 PGTZ=Asia/Shanghai · created_at/paid_at 是 naive 本地时间
    # 用 now() 匹配存储时区(utcnow 差 8h 会让月/季/年边界跨期错位)
    now = datetime.now()
    if period == "custom" and start and end:
        try:
            since = datetime.fromisoformat(start)
            until = datetime.fromisoformat(end)
        except Exception:
            raise HTTPException(400, "custom 期间 start/end 必须是 ISO 日期")
        if until <= since:
            raise HTTPException(400, "end 必须晚于 start")
        span = until - since
        prev_until = since
        prev_since = since - span
        opex_start = since.strftime("%Y-%m-01")
        opex_end = until.strftime("%Y-%m-01")
        label = f"{start[:10]} ~ {end[:10]}"
    elif period == "year":
        since = _month_start(now.year, 1)
        until = _add_months(since, 12)
        prev_since = _add_months(since, -12)
        prev_until = since
        opex_start = f"{now.year}-01-01"
        opex_end = f"{now.year}-12-01"
        label = f"{now.year} 年度"
    elif period == "quarter":
        first_month = ((now.month - 1) // 3) * 3 + 1
        since = _month_start(now.year, first_month)
        until = _add_months(since, 3)
        prev_since = _add_months(since, -3)
        prev_until = since
        opex_start = since.strftime("%Y-%m-01")
        opex_end = _add_months(since, 2).strftime("%Y-%m-01")
        q = (first_month - 1) // 3 + 1
        label = f"{now.year} Q{q}"
    else:  # month(默认)
        since = _month_start(now.year, now.month)
        until = _add_months(since, 1)
        prev_since = _add_months(since, -1)
        prev_until = since
        opex_start = since.strftime("%Y-%m-01")
        opex_end = opex_start
        label = since.strftime("%Y-%m")
    return {
        "since": since, "until": until,
        "prev_since": prev_since, "prev_until": prev_until,
        "opex_start": opex_start, "opex_end": opex_end,
        "label": label, "period": period,
    }


# ============================================================
# 收入 / 成本 核心计算(overview + pnl 共用)
# ============================================================

def _revenue_breakdown(since: datetime, until: datetime) -> Dict[str, float]:
    """确认收入(权责)明细 · 单位元。"""
    direct_pts = _safe_scalar(
        "SELECT COALESCE(SUM(ABS(amount)),0) AS v FROM point_transactions "
        "WHERE type='consume' AND point_type='paid' AND created_at >= %s AND created_at < %s",
        (since, until),
    )
    refund_pts = _safe_scalar(
        "SELECT COALESCE(SUM(ABS(amount)),0) AS v FROM point_transactions "
        "WHERE type='refund' AND point_type='paid' AND created_at >= %s AND created_at < %s",
        (since, until),
    )
    factory_cents = _safe_scalar(
        "SELECT COALESCE(SUM(factory_cents),0) AS v FROM agent_revenue_ledger "
        "WHERE created_at >= %s AND created_at < %s AND status <> 'cancelled'",
        (since, until),
    )
    sub_yuan = _safe_scalar(
        "SELECT COALESCE(SUM(price_locked_yuan),0) AS v FROM user_social_subscriptions "
        "WHERE started_at >= %s AND started_at < %s AND status <> 'refunded'",
        (since, until),
    )
    direct = float(direct_pts) / POINTS_PER_YUAN
    refund = float(refund_pts) / POINTS_PER_YUAN
    factory = float(factory_cents) / 100.0
    sub = float(sub_yuan)
    confirmed = direct + factory + sub - refund
    return {
        "direct_consume": round(direct, 2),
        "agent_factory": round(factory, 2),
        "subscription": round(sub, 2),
        "refund": round(refund, 2),
        "confirmed": round(confirmed, 2),
    }


def _cash_revenue(since: datetime, until: datetime) -> float:
    """现金口径收入(元)· 充值到账(paid_at 优先 created_at)。"""
    # [财务账单 2026-06-17] 与 /recharge-orders 列表合计共用同一 cents 聚合口径。
    return round(float(_cash_revenue_cents(since, until)) / 100.0, 2)


def _cash_revenue_cents(since: datetime, until: datetime) -> int:
    """现金口径收入(分)· 与充值账单列表复用同一 payment_status/时间口径。"""
    v = _safe_scalar(
        "SELECT COALESCE(SUM(amount_cents),0) AS v FROM recharge_orders "
        "WHERE payment_status='paid' "
        "AND COALESCE(paid_at, created_at) >= %s AND COALESCE(paid_at, created_at) < %s",
        (since, until),
    )
    return int(v or 0)


def _cash_refund(since: datetime, until: datetime) -> float:
    """现金退款流出(元)· 真实充值退款额(非积分代理值)。

    退款真实出账额以 recharge_orders.refunded_amount_cents 为准；金额由各退款案件按
    法定/协商事由与实际履行证据确定，普通消费者不固定扣 5%。微信或虎皮椒可信终态写
    refund_status='completed' + refund_completed_at，财务报表按完成时间归期。
    """
    v = _safe_scalar(
        "SELECT COALESCE(SUM(refunded_amount_cents),0) AS v FROM recharge_orders "
        "WHERE refund_status='completed' AND refund_completed_at >= %s AND refund_completed_at < %s",
        (since, until),
    )
    return round(float(v) / 100.0, 2)


# ============================================================
# 充值账单 helper(admin-only · 只读)
# ============================================================

def _clamp_recharge_limit(limit: int) -> int:
    """充值账单列表单页上限。total 仍使用筛选集精确 COUNT。"""
    try:
        n = int(limit)
    except Exception:
        n = 50
    return max(1, min(n, 200))


def _classify_recharge_path(
    order_type: Optional[str],
    settlement_mode: Optional[str],
    agent_user_id: Optional[int],
) -> str:
    """资金路径标签 · 给 admin 看,不改变任何资金状态。"""
    ot = (order_type or "").strip()
    sm = (settlement_mode or "").strip()
    if ot == "agent_inventory_purchase":
        return "服务商预付进货"
    if sm == "v32_legacy":
        return "老分润路径"
    if sm == "v35_platform_direct_settlement":
        return "平台直营客户充值"
    if ot == "customer_recharge" or sm == "v35_inventory_settlement" or agent_user_id:
        return "服务商客户充值"
    if ot in ("recharge", "wallet_recharge", ""):
        return "平台直营"
    return "未知待核"


def _recharge_role_label(agent_level: Any) -> str:
    try:
        lvl = int(agent_level or 0)
    except Exception:
        lvl = 0
    return "服务商" if lvl >= 1 else "普通用户"


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v or default)
    except Exception:
        return default


def _build_recharge_filters(
    p: Dict[str, Any],
    status: str,
    user_id: Optional[int],
    q: Optional[str],
    payment_method: Optional[str],
    order_type: Optional[str],
    refund_status: Optional[str],
) -> tuple[str, List[Any]]:
    where = [
        "COALESCE(ro.paid_at, ro.created_at) >= %s",
        "COALESCE(ro.paid_at, ro.created_at) < %s",
    ]
    params: List[Any] = [p["since"], p["until"]]
    status = (status or "paid").strip()
    if status == "refunded":
        where.append("COALESCE(ro.refund_status, '') <> ''")
    elif status != "all":
        where.append("ro.payment_status = %s")
        params.append(status)
    if user_id:
        where.append("ro.user_id = %s")
        params.append(user_id)
    if q:
        like = f"%{q.strip()}%"
        where.append("(ro.id ILIKE %s OR u.username ILIKE %s OR u.display_name ILIKE %s OR u.phone ILIKE %s)")
        params.extend([like, like, like, like])
    if payment_method:
        where.append("ro.payment_method = %s")
        params.append(payment_method)
    if order_type:
        where.append("ro.order_type = %s")
        params.append(order_type)
    if refund_status:
        where.append("COALESCE(ro.refund_status, '') = %s")
        params.append(refund_status)
    return " AND ".join(where), params


def _format_recharge_order(row: Dict[str, Any]) -> Dict[str, Any]:
    points = _int(row.get("base_points")) + _int(row.get("bonus_points"))
    agent_user_id = row.get("agent_user_id")
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "user_display_name": row.get("user_display_name") or row.get("user_username") or f"用户 #{row.get('user_id')}",
        "user_username": row.get("user_username"),
        "user_role_label": _recharge_role_label(row.get("user_agent_level")),
        "amount_cents": _int(row.get("amount_cents")),
        "points_granted": points,
        "base_points": _int(row.get("base_points")),
        "bonus_points": _int(row.get("bonus_points")),
        "payment_status": row.get("payment_status"),
        "refund_status": row.get("refund_status") or "none",
        "refunded_amount_cents": _int(row.get("refunded_amount_cents")),
        "payment_method": row.get("payment_method") or "unknown",
        "order_type": row.get("order_type") or "recharge",
        "settlement_mode": row.get("settlement_mode"),
        "path_label": _classify_recharge_path(row.get("order_type"), row.get("settlement_mode"), agent_user_id),
        "agent_user_id": agent_user_id,
        "agent_display_name": row.get("agent_display_name"),
        "service_revenue_cents": _int(row.get("service_revenue_cents")),
        "has_flow_warning": bool(
            (row.get("payment_status") == "paid" and points <= 0)
            or (
                row.get("settlement_mode") == "v35_inventory_settlement"
                and not agent_user_id
            )
        ),
        "created_at": _iso(row.get("created_at")),
        "paid_at": _iso(row.get("paid_at")),
    }


def _media_cost(since: datetime, until: datetime) -> float:
    """媒体采购成本(元)· mhz_synced_orders.price(单位元)。

    [复审修正 🔴-1] platform_cost_cents 只在 mhz_media/feature_pricing/sku_templates,
    mhz_synced_orders(实际订单表)无此列 → 原 SQL 会查不存在的列被 _safe_scalar 静默吞成 0
    导致媒体 COGS 归零、毛利虚高。改用 synced_orders 真实成本列 price(已实证)。
    """
    # [GEO-R1-CAN-081] 只计已发生成本的已完成订单(status=2 已完成)· 排除
    # 待接单(0)/发布中(1)/拒稿(-1)/撤回(-2)/退款(3):这些订单成本未发生或已冲销,
    # 原先无 status 过滤把它们全计入 COGS → 毛利虚低 + 现金媒体流出虚高。
    # (mhz_synced_orders.status 语义见 meijiehezi_api.py:1414 -2撤回/-1拒稿/0待接单/1发布中/2已完成/3退款)
    v = _safe_scalar(
        "SELECT COALESCE(SUM(price),0) AS v "
        "FROM mhz_synced_orders WHERE status = 2 "
        "AND created_at >= %s AND created_at < %s",
        (since, until),
    )
    return round(float(v), 2)



# ══ 营业利润:**一处定义,两处共用** ═══════════════════════════════════
# 🔴 [BH-014 · 2026-09-05] 改之前这个公式在本文件里写了**两遍**且不等价:
#      屏幕 GET /pnl     : gross - opex_total - gateway_fee
#      导出 GET /export  : gross - opex_total            ← 漏减渠道费
#    同一 period / 同一 basis 下,CSV 的「营业利润」恒比屏幕高出 gateway_fee,
#    而 CSV 里连「支付渠道费」这一行都没有 ⇒ 读者无法自洽核对。
#
#    不是「写公式时手滑漏打一项」:`gateway_fee` 在 export 那条链的
#    作用域里**根本不存在**(symtable 查过)—— 它从取数层就没把渠道费拿回来。
#    所以修法是「**先取再减**」,不是「加个减号」。
#
#    抽成纯函数而不是在导出侧再手写一遍:两份手写副本**正是本 bug 的成因**,
#    再写一遍等于把成因留着,下次谁改口径照样漂。


def _operating_profit(gross: float, opex_total: float, gateway_fee: float) -> float:
    """营业利润 = 毛利 − 运营费用 − **支付渠道费**。

    三个入参都是元(已 /100 且已 round 到分)。返回值再 round 一次到分。
    """
    return round(gross - opex_total - gateway_fee, 2)


def _gateway_fee_yuan(since, until) -> float:
    """区间内已支付订单的支付渠道费(元)。

    抽出来是为了让**屏幕与导出取同一份数** —— 改之前这段 SQL 也是各写各的。
    """
    return round(float(_safe_scalar(
        "SELECT COALESCE(SUM(gateway_fee_cents),0) AS v FROM recharge_orders "
        "WHERE payment_status='paid' AND COALESCE(paid_at, created_at) >= %s "
        "AND COALESCE(paid_at, created_at) < %s",
        (since, until))) / 100.0, 2)


def _cogs(since: datetime, until: datetime) -> Dict[str, float]:
    """COGS 明细(元)· LLM(含监测)+ 媒体采购。"""
    llm = round(_llm_cost(since, until), 2)
    media = _media_cost(since, until)
    return {"llm": llm, "media": media, "total": round(llm + media, 2)}


def _pct(cur_v: float, prev_v: float) -> Optional[float]:
    if not prev_v:
        return None
    return round(100.0 * (cur_v - prev_v) / prev_v, 1)


# ============================================================
# 1. GET /overview · 5 核心数 + 同环比
# ============================================================

@router.get("/overview")
async def overview(
    request: Request,
    period: str = Query("month"),
    basis: str = Query("accrual"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    _require_admin(request)
    p = _resolve_period(period, start, end)

    def _do():
        rev = _revenue_breakdown(p["since"], p["until"])
        prev_rev = _revenue_breakdown(p["prev_since"], p["prev_until"])
        cash = _cash_revenue(p["since"], p["until"])
        prev_cash = _cash_revenue(p["prev_since"], p["prev_until"])
        cogs = _cogs(p["since"], p["until"])
        prev_cogs = _cogs(p["prev_since"], p["prev_until"])

        confirmed = rev["confirmed"]
        prev_confirmed = prev_rev["confirmed"]
        # [GEO-R1-CAN-033] 现金口径收入也要冲减当期已完成退款 · 与权责口径
        # (_revenue_breakdown: confirmed = ... - refund)对称 · 否则 cash 只取充值毛额,
        # 毛利/毛利率/营业利润在 basis=cash 下虚高。refund_cash 复用于下方 net_cash。
        refund_cash = _cash_refund(p["since"], p["until"])
        prev_refund_cash = _cash_refund(p["prev_since"], p["prev_until"])
        cash_net = round(cash - refund_cash, 2)
        prev_cash_net = round(prev_cash - prev_refund_cash, 2)
        # 主口径收入(按 basis)
        revenue = confirmed if basis != "cash" else cash_net
        prev_revenue = prev_confirmed if basis != "cash" else prev_cash_net
        gross = round(revenue - cogs["total"], 2)
        prev_gross = round(prev_revenue - prev_cogs["total"], 2)

        # 递延负债(时点)= 未消费 paid_points / 130
        deferred = round(float(_safe_scalar(
            "SELECT COALESCE(SUM(paid_points),0) AS v FROM user_wallets")) / POINTS_PER_YUAN, 2)

        # 净现金(本期)= 充值流入 − 媒体采购 − 提现已付 − 退款
        cash_in = cash
        withdraw_paid = round(float(_safe_scalar(
            "SELECT COALESCE(SUM(request_amount_cents),0) AS v FROM agent_settlement_requests "
            "WHERE status='paid' AND COALESCE(paid_at, created_at) >= %s AND COALESCE(paid_at, created_at) < %s",
            (p["since"], p["until"]))) / 100.0, 2)
        # refund_cash 已在上方计算(GEO-R1-CAN-033)· 此处直接复用
        net_cash = round(cash_in - cogs["media"] - withdraw_paid - refund_cash, 2)

        return {
            "revenue": revenue, "prev_revenue": prev_revenue,
            "cogs": cogs["total"], "prev_cogs": prev_cogs["total"],
            "gross": gross, "prev_gross": prev_gross,
            "gross_margin": round(100.0 * gross / revenue, 1) if revenue and revenue > 0 else None,
            "deferred": deferred,
            "net_cash": net_cash,
            "cash_revenue": cash, "confirmed_revenue": confirmed,
            "revenue_breakdown": rev,
            "cogs_breakdown": cogs,
        }

    d = await _run(_do)
    return {
        "period": p["period"], "label": p["label"], "basis": basis,
        "cards": {
            "revenue": {"value": d["revenue"], "delta_pct": _pct(d["revenue"], d["prev_revenue"])},
            "cogs": {"value": d["cogs"], "delta_pct": _pct(d["cogs"], d["prev_cogs"])},
            "gross": {"value": d["gross"], "delta_pct": _pct(d["gross"], d["prev_gross"]), "margin": d["gross_margin"]},
            "deferred": {"value": d["deferred"]},
            "net_cash": {"value": d["net_cash"]},
        },
        "cash_revenue": d["cash_revenue"],
        "confirmed_revenue": d["confirmed_revenue"],
        "revenue_breakdown": d["revenue_breakdown"],
        "cogs_breakdown": d["cogs_breakdown"],
    }


# ============================================================
# 2. GET /pnl · 利润表 + 分功能毛利
# ============================================================

@router.get("/pnl")
async def pnl(
    request: Request,
    period: str = Query("month"),
    basis: str = Query("accrual"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    _require_admin(request)
    p = _resolve_period(period, start, end)

    def _do():
        rev = _revenue_breakdown(p["since"], p["until"])
        cash = _cash_revenue(p["since"], p["until"])
        cogs = _cogs(p["since"], p["until"])
        # [GEO-R1-CAN-033] 现金口径冲减当期已完成退款 · 与权责口径对称(cash_revenue 显示仍为到账毛额)
        revenue = rev["confirmed"] if basis != "cash" else round(cash - _cash_refund(p["since"], p["until"]), 2)
        gross = round(revenue - cogs["total"], 2)

        # OpEx(operating_expenses 按 category)
        opex_rows = sum_opex_by_category(p["opex_start"], p["opex_end"])
        opex_total = round(sum(r["amount_cents"] for r in opex_rows) / 100.0, 2)

        # 渠道费(从 recharge_orders)—— 与导出侧共用 `_gateway_fee_yuan`
        gateway_fee = _gateway_fee_yuan(p["since"], p["until"])
        op_profit = _operating_profit(gross, opex_total, gateway_fee)

        # 分功能收入(paid 消费 by feature_code)
        feat_rows = _safe_rows(
            "SELECT COALESCE(feature_code,'(未标注)') AS feature_code, "
            "COALESCE(SUM(ABS(amount)),0) AS pts, COUNT(*) AS cnt "
            "FROM point_transactions "
            "WHERE type='consume' AND point_type='paid' AND created_at >= %s AND created_at < %s "
            "GROUP BY feature_code ORDER BY pts DESC LIMIT 30",
            (p["since"], p["until"]))
        revenue_by_feature = [
            {"feature_code": r["feature_code"],
             "revenue": round(float(r["pts"] or 0) / POINTS_PER_YUAN, 2),
             "count": int(r["cnt"] or 0)}
            for r in feat_rows
        ]

        return {
            "revenue": revenue, "cash_revenue": cash,
            "cogs": cogs, "gross": gross,
            "gross_margin": round(100.0 * gross / revenue, 1) if revenue and revenue > 0 else None,
            "opex_total": opex_total,
            "opex_breakdown": [
                {"category": r["category"], "label": r["label"], "amount": round(r["amount_cents"] / 100.0, 2)}
                for r in opex_rows
            ],
            "gateway_fee": gateway_fee,
            "operating_profit": op_profit,
            "operating_margin": round(100.0 * op_profit / revenue, 1) if revenue and revenue > 0 else None,
            "revenue_breakdown": rev,
            "revenue_by_feature": revenue_by_feature,
        }

    d = await _run(_do)
    return {"period": p["period"], "label": p["label"], "basis": basis, **d}


# ============================================================
# 3. GET /liabilities · 负债与递延表
# ============================================================

@router.get("/liabilities")
async def liabilities(request: Request):
    _require_admin(request)

    def _do():
        def y(sql, params=()):
            return round(float(_safe_scalar(sql, params)) / POINTS_PER_YUAN, 2)

        deferred = y("SELECT COALESCE(SUM(paid_points),0) AS v FROM user_wallets")
        marketing = y("SELECT COALESCE(SUM(bonus_points),0) AS v FROM user_wallets")
        commission = y("SELECT COALESCE(SUM(commission_points),0) AS v FROM user_wallets")
        frozen = y("SELECT COALESCE(SUM(frozen_points),0) AS v FROM user_wallets")
        # 客户额度浮存(代理代客户持有)
        # [单账本收敛 2026-07-27] 迁移后本项归零 —— 客户的算力已并入自己的 user_wallets,
        # 会被上面的 deferred(递延收入·预收未交付)统计到。
        # 🔴 负债总额【不变】,只是从"代持"科目移到"负债"科目:客户充值算力本来就是递延收入。
        # 科目保留不删:历史期间报表仍要能算出当时的代持额,删了会追溯改写历史数据。
        # [历史账本只读 · 2026-08-17] 该表停写且三池清零 → 本项恒 0,仅作历史科目留位。
        cust_credit = y(
            "SELECT COALESCE(SUM(tool_credit_points+publish_credit_points+bonus_credit_points),0) AS v "
            "FROM customer_agent_credit_wallets")
        # 代理库存浮存(代理预付未划拨)
        agent_inv = y(
            "SELECT COALESCE(SUM(paid_inventory_points+bonus_inventory_points+frozen_inventory_points),0) AS v "
            "FROM agent_inventory_wallets")
        # 应付代理(已结算待提现)= agent_revenue_ledger settled 的应付款
        agent_settled = round(float(_safe_scalar(
            "SELECT COALESCE(SUM(agent_settlement_cents),0) AS v FROM agent_revenue_ledger WHERE status='settled'"
        )) / 100.0, 2)

        total_liabilities = round(
            deferred + marketing + commission + frozen + cust_credit + agent_inv + agent_settled, 2)
        return {
            "items": [
                {"key": "deferred_revenue", "label": "递延收入(预收未交付)", "amount": deferred, "nature": "负债"},
                {"key": "marketing", "label": "营销负债(已发赠送积分)", "amount": marketing, "nature": "负债"},
                {"key": "commission_payable", "label": "应付佣金(钱包佣金积分)", "amount": commission, "nature": "负债"},
                {"key": "agent_settled_payable", "label": "应付服务方(已结算待提现)", "amount": agent_settled, "nature": "负债"},
                {"key": "frozen", "label": "使用中冻结(长任务占位)", "amount": frozen, "nature": "占位"},
                {"key": "customer_credit_float", "label": "客户额度浮存", "amount": cust_credit, "nature": "代持"},
                {"key": "agent_inventory_float", "label": "服务方库存浮存", "amount": agent_inv, "nature": "代持"},
            ],
            "total": total_liabilities,
        }

    d = await _run(_do)
    return d


# ============================================================
# 4. GET /cashflow · 现金流量表
# ============================================================

@router.get("/cashflow")
async def cashflow(
    request: Request,
    period: str = Query("month"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    _require_admin(request)
    p = _resolve_period(period, start, end)

    def _do():
        # 流入 by payment_method
        inflow_rows = _safe_rows(
            "SELECT COALESCE(payment_method,'(未标注)') AS method, COALESCE(SUM(amount_cents),0) AS cents, COUNT(*) AS cnt "
            "FROM recharge_orders WHERE payment_status='paid' "
            "AND COALESCE(paid_at, created_at) >= %s AND COALESCE(paid_at, created_at) < %s "
            "GROUP BY payment_method ORDER BY cents DESC",
            (p["since"], p["until"]))
        inflow = [
            {"method": r["method"], "amount": round(float(r["cents"] or 0) / 100.0, 2), "count": int(r["cnt"] or 0)}
            for r in inflow_rows
        ]
        total_in = round(sum(x["amount"] for x in inflow), 2)

        media = _media_cost(p["since"], p["until"])
        withdraw = round(float(_safe_scalar(
            "SELECT COALESCE(SUM(request_amount_cents),0) AS v FROM agent_settlement_requests "
            "WHERE status='paid' AND COALESCE(paid_at, created_at) >= %s AND COALESCE(paid_at, created_at) < %s",
            (p["since"], p["until"]))) / 100.0, 2)
        refund = _cash_refund(p["since"], p["until"])
        # 🔴 [BH-014] 与 pnl / export 共用 `_gateway_fee_yuan` —— 并入前这段 SQL
        #    在本文件是**第三份逐字副本**(已证形参代入后与 helper 逐字相等)。
        gateway_fee = _gateway_fee_yuan(p["since"], p["until"])
        tax = round(float(_safe_scalar(
            "SELECT COALESCE(SUM(tax_withholding_cents),0) AS v FROM recharge_orders "
            "WHERE payment_status='paid' AND COALESCE(paid_at, created_at) >= %s AND COALESCE(paid_at, created_at) < %s",
            (p["since"], p["until"]))) / 100.0, 2)
        outflow = [
            {"key": "media", "label": "媒体采购", "amount": media},
            {"key": "withdraw", "label": "服务方提现", "amount": withdraw},
            {"key": "refund", "label": "退款", "amount": refund},
            {"key": "gateway_fee", "label": "支付渠道费", "amount": gateway_fee},
            {"key": "tax", "label": "代扣税", "amount": tax},
        ]
        total_out = round(media + withdraw + refund + gateway_fee + tax, 2)
        return {
            "inflow": inflow, "total_inflow": total_in,
            "outflow": outflow, "total_outflow": total_out,
            "net_cash_flow": round(total_in - total_out, 2),
        }

    d = await _run(_do)
    return {"period": p["period"], "label": p["label"], **d}


# ============================================================
# 5. GET /recharge-orders · 充值账单(admin-only)
# ============================================================

@router.get("/recharge-orders")
async def admin_recharge_orders(
    request: Request,
    period: str = Query("month"),
    start: Optional[str] = None,
    end: Optional[str] = None,
    status: str = Query("paid"),
    user_id: Optional[int] = None,
    q: Optional[str] = None,
    payment_method: Optional[str] = None,
    order_type: Optional[str] = None,
    refund_status: Optional[str] = None,
    limit: int = Query(50),
    offset: int = Query(0, ge=0),
):
    _require_admin(request)
    p = _resolve_period(period, start, end)
    limit = _clamp_recharge_limit(limit)

    def _do():
        where_sql, params = _build_recharge_filters(
            p, status, user_id, q, payment_method, order_type, refund_status
        )
        join_sql = """
            FROM recharge_orders ro
            LEFT JOIN users u ON u.id = ro.user_id
            LEFT JOIN user_wallets uw ON uw.user_id = ro.user_id
            LEFT JOIN users au ON au.id = ro.agent_user_id
            LEFT JOIN (
                SELECT recharge_order_id, COALESCE(SUM(agent_settlement_cents),0) AS service_revenue_cents
                FROM agent_revenue_ledger
                WHERE reversed_at IS NULL
                GROUP BY recharge_order_id
            ) ar ON ar.recharge_order_id = ro.id
        """
        total = int(_safe_scalar(
            f"SELECT COUNT(*) AS v {join_sql} WHERE {where_sql}",
            tuple(params),
        ))
        totals_rows = _safe_rows(
            f"SELECT COALESCE(SUM(ro.amount_cents),0) AS amount_cents, "
            f"COALESCE(SUM(ro.base_points + ro.bonus_points),0) AS points_granted "
            f"{join_sql} WHERE {where_sql}",
            tuple(params),
        )
        totals = totals_rows[0] if totals_rows else {}
        rows = _safe_rows(
            f"""
            SELECT ro.id, ro.user_id, u.display_name AS user_display_name, u.username AS user_username,
                   u.phone, COALESCE(uw.agent_level, 0) AS user_agent_level,
                   ro.amount_cents, ro.base_points, ro.bonus_points,
                   ro.payment_status, ro.refund_status, ro.refunded_amount_cents,
                   ro.payment_method, ro.order_type, ro.settlement_mode,
                   ro.agent_user_id, au.display_name AS agent_display_name,
                   COALESCE(ar.service_revenue_cents, 0) AS service_revenue_cents,
                   ro.created_at, ro.paid_at
            {join_sql}
            WHERE {where_sql}
            ORDER BY COALESCE(ro.paid_at, ro.created_at) DESC, ro.created_at DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params) + (limit, offset),
        )
        return {
            "total": total,
            "items": [_format_recharge_order(r) for r in rows],
            "totals": {
                "amount_cents": _int(totals.get("amount_cents")),
                "points_granted": _int(totals.get("points_granted")),
            },
            "limit": limit,
            "offset": offset,
        }

    d = await _run(_do)
    return {"period": p["period"], "label": p["label"], **d}


@router.get("/recharge-orders/{order_id}/flow")
async def admin_recharge_order_flow(order_id: str, request: Request):
    _require_admin(request)

    def _do():
        # 订单主查询必须严格失败:这里区分"订单不存在"和"SQL/schema 异常"。
        # 后续明细表仍走 _safe_rows,避免单个辅助表缺失拖垮整个只读 Drawer。
        rows = _query_db(
            """
            SELECT ro.id, ro.user_id, u.display_name AS user_display_name, u.username AS user_username,
                   COALESCE(uw.agent_level, 0) AS user_agent_level,
                   ro.amount_cents, ro.base_points, ro.bonus_points,
                   ro.payment_status, ro.refund_status, ro.refunded_amount_cents,
                   ro.payment_method, ro.order_type, ro.settlement_mode,
                   ro.agent_user_id, au.display_name AS agent_display_name,
                   ro.created_at, ro.paid_at
            FROM recharge_orders ro
            LEFT JOIN users u ON u.id = ro.user_id
            LEFT JOIN user_wallets uw ON uw.user_id = ro.user_id
            LEFT JOIN users au ON au.id = ro.agent_user_id
            WHERE ro.id = %s
            LIMIT 1
            """,
            (order_id,),
        )
        if not rows:
            raise HTTPException(status_code=404, detail="充值订单不存在")
        order = _format_recharge_order(rows[0])
        uid = order.get("user_id")
        agent_id = order.get("agent_user_id")

        point_transactions = _safe_rows(
            """
            SELECT id, user_id, type, point_type, amount, balance_after, feature_code,
                   description, order_id, brand_id, source, created_at
            FROM point_transactions
            WHERE order_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (order_id,),
        )
        # [历史账本只读 · 2026-08-17] 单订单资金流水审计视图:读的就是停写表里的历史归属与
        # 累计值(total_purchased/consumed 未随迁移清零)。审计场景要看当时的样子,不可改读现值。
        customer_credit_rows = _safe_rows(
            """
            SELECT c.customer_user_id, c.agent_user_id, u.display_name AS agent_display_name,
                   c.tool_credit_points, c.publish_credit_points, c.bonus_credit_points,
                   c.total_purchased_points, c.total_consumed_points, c.updated_at
            FROM customer_agent_credit_wallets c
            LEFT JOIN users u ON u.id = c.agent_user_id
            WHERE c.customer_user_id = %s
            LIMIT 1
            """,
            (uid,),
        )
        customer_credit_transactions = _safe_rows(
            """
            SELECT id, customer_user_id, agent_user_id, type, pool, points,
                   balance_tool_after, balance_publish_after, balance_bonus_after,
                   feature_code, related_order_id, source, description, created_at
            FROM customer_credit_transactions
            WHERE related_order_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (order_id,),
        )
        inventory_transactions = _safe_rows(
            """
            SELECT id, agent_user_id, type, pool, points, balance_paid_after,
                   balance_bonus_after, related_customer_user_id, related_order_id,
                   description, created_at
            FROM agent_inventory_transactions
            WHERE related_order_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (order_id,),
        )
        revenue_ledger = _safe_rows(
            """
            SELECT id, agent_user_id, source, recharge_order_id, customer_user_id,
                   customer_paid_cents, factory_cents, gateway_fee_cents,
                   settlement_service_fee_cents, agent_margin_before_tax_cents,
                   tax_withholding_cents, agent_settlement_cents, status, settle_at,
                   settled_at, reversed_at, reversed_by_ledger_id,
                   manual_review_required, note, created_at
            FROM agent_revenue_ledger
            WHERE recharge_order_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (order_id,),
        )
        settlement_items = _safe_rows(
            """
            SELECT i.id, i.ledger_id, i.locked_amount_cents, i.created_at,
                   r.id AS settlement_request_id, r.status AS request_status,
                   r.request_amount_cents, r.paid_at
            FROM agent_settlement_request_items i
            JOIN agent_settlement_requests r ON r.id = i.settlement_request_id
            WHERE i.ledger_id IN (
                SELECT id FROM agent_revenue_ledger WHERE recharge_order_id = %s
            )
            ORDER BY i.created_at ASC, i.id ASC
            """,
            (order_id,),
        )

        warnings: List[str] = []
        if (
            order["payment_status"] == "paid"
            and order.get("order_type") != "agent_inventory_purchase"
            and not point_transactions
        ):
            warnings.append("订单已支付,但未找到普通钱包充值流水")
        _v35_customer_modes = {
            "v35_inventory_settlement",
            "v35_platform_direct_settlement",
        }
        if order["settlement_mode"] in _v35_customer_modes and not customer_credit_transactions:
            warnings.append("V3.5 服务方客户充值未找到客户额度入账流水")
        if order["settlement_mode"] in _v35_customer_modes and agent_id and not inventory_transactions:
            warnings.append("V3.5 服务方客户充值未找到服务方库存侧流水")
        if agent_id and order["settlement_mode"] in _v35_customer_modes and not revenue_ledger:
            warnings.append("V3.5 线上充值未找到服务商收益台账")

        steps = [
            {"key": "payment", "label": "客户支付", "status": order["payment_status"],
             "amount_cents": order["amount_cents"], "at": order.get("paid_at") or order.get("created_at")},
            {"key": "wallet", "label": "普通钱包流水", "count": len(point_transactions)},
            # 🔴 [#118-10a] 原标签「服务方账户额度」两处不妥:含禁词「额度」,
            #    且它指向的 `customer_credit_transactions` 自 2026-07-27 单账本收敛
            #    (`2aef3b59b`)起已是**退役账本** —— 标签仍写得像在用。
            #    这是 admin 面(`/api/admin/finance`),不是客户面,不需 Owner 拍板文案。
            {"key": "customer_credit", "label": "客户信用账流水(已退役·历史)",
             "count": len(customer_credit_transactions)},
            {"key": "inventory", "label": "服务方库存流水", "count": len(inventory_transactions)},
            {"key": "revenue_ledger", "label": "服务商收益台账", "count": len(revenue_ledger)},
        ]

        return {
            "order": order,
            "steps": steps,
            "point_transactions": [
                {**dict(r), "created_at": _iso(r.get("created_at"))} for r in point_transactions
            ],
            "customer_credit": (
                {**dict(customer_credit_rows[0]), "updated_at": _iso(customer_credit_rows[0].get("updated_at"))}
                if customer_credit_rows else None
            ),
            "customer_credit_transactions": [
                {**dict(r), "created_at": _iso(r.get("created_at"))} for r in customer_credit_transactions
            ],
            "inventory_transactions": [
                {**dict(r), "created_at": _iso(r.get("created_at"))} for r in inventory_transactions
            ],
            "revenue_ledger": [
                {**dict(r), "created_at": _iso(r.get("created_at")),
                 "settle_at": _iso(r.get("settle_at")), "settled_at": _iso(r.get("settled_at")),
                 "reversed_at": _iso(r.get("reversed_at"))}
                for r in revenue_ledger
            ],
            "settlement_items": [
                {**dict(r), "created_at": _iso(r.get("created_at")), "paid_at": _iso(r.get("paid_at"))}
                for r in settlement_items
            ],
            "warnings": warnings,
        }

    return await _run(_do)


# ============================================================
# 6. GET /cost-center · 成本下钻
# ============================================================

@router.get("/cost-center")
async def cost_center(
    request: Request,
    period: str = Query("month"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    _require_admin(request)
    p = _resolve_period(period, start, end)

    def _do():
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            summary = get_api_cost_summary(cur, p["since"], until=p["until"], limit=30)
            by_platform = get_api_cost_by_platform(cur, p["since"], until=p["until"])
            by_caller = get_api_cost_by_caller(cur, p["since"], until=p["until"])
        except Exception as e:
            logger.warning(f"[finance] cost-center LLM 聚合失败(返回空): {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            summary, by_platform, by_caller = {}, [], []
        finally:
            try:
                conn.close()
            except Exception:
                pass

        media_total = _media_cost(p["since"], p["until"])
        # [GEO-R1-CAN-081] 分媒体明细须与 _media_cost 同口径(status=2 已完成)· 否则合计 ≠ media_total
        media_rows = _safe_rows(
            "SELECT COALESCE(media_name,'(未标注)') AS media_name, "
            "COALESCE(SUM(price),0) AS cost, COUNT(*) AS cnt "
            "FROM mhz_synced_orders WHERE status = 2 "
            "AND COALESCE(user_id, 0) >= 0 "   # [WO_301b ②c] 与 _media_cost 同口径
            "AND created_at >= %s AND created_at < %s "
            "GROUP BY media_name ORDER BY cost DESC LIMIT 20",
            (p["since"], p["until"]))
        return {
            "llm": {
                "total": round(float(summary.get("total_cost") or 0), 2),
                "calls": int(summary.get("total_calls") or 0),
                "by_model": summary.get("items") or [],
                "by_platform": [
                    {"platform": r["platform"], "cost": round(float(r["cost"] or 0), 2),
                     "calls": int(r["calls"] or 0),
                     "input_tokens": int(r.get("input_tokens") or 0),
                     "output_tokens": int(r.get("output_tokens") or 0)}
                    for r in by_platform
                ],
                "by_caller": [
                    {"caller": r["caller"], "cost": round(float(r["cost"] or 0), 2), "calls": int(r["calls"] or 0)}
                    for r in by_caller
                ],
                "unknown_caller_ratio": summary.get("unknown_caller_ratio") or 0,
            },
            "media": {
                "total": media_total,
                "by_outlet": [
                    {"media_name": r["media_name"], "cost": round(float(r["cost"] or 0), 2), "count": int(r["cnt"] or 0)}
                    for r in media_rows
                ],
            },
            "grand_total": round(float(summary.get("total_cost") or 0) + media_total, 2),
        }

    d = await _run(_do)
    return {"period": p["period"], "label": p["label"], **d}


# ============================================================
# 6. GET /agent-settlement · 代理结算(4 层透明)
# ============================================================

@router.get("/agent-settlement")
async def agent_settlement(
    request: Request,
    period: str = Query("year"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    _require_admin(request)
    p = _resolve_period(period, start, end)

    def _do():
        # 按 status 聚合 ledger 各层(分 → 元)
        ledger_rows = _safe_rows(
            "SELECT status, COUNT(*) AS cnt, "
            "COALESCE(SUM(customer_paid_cents),0) AS customer_paid, "
            "COALESCE(SUM(factory_cents),0) AS factory, "
            "COALESCE(SUM(gateway_fee_cents),0) AS gateway_fee, "
            "COALESCE(SUM(settlement_service_fee_cents),0) AS service_fee, "
            "COALESCE(SUM(agent_margin_before_tax_cents),0) AS margin_before_tax, "
            "COALESCE(SUM(tax_withholding_cents),0) AS tax, "
            "COALESCE(SUM(agent_settlement_cents),0) AS settlement "
            "FROM agent_revenue_ledger "
            "WHERE created_at >= %s AND created_at < %s "
            "GROUP BY status",
            (p["since"], p["until"]))

        def c2y(x):
            return round(float(x or 0) / 100.0, 2)

        pipeline = [
            {"status": r["status"], "count": int(r["cnt"] or 0),
             "customer_paid": c2y(r["customer_paid"]), "factory": c2y(r["factory"]),
             "gateway_fee": c2y(r["gateway_fee"]), "service_fee": c2y(r["service_fee"]),
             "margin_before_tax": c2y(r["margin_before_tax"]), "tax": c2y(r["tax"]),
             "settlement": c2y(r["settlement"])}
            for r in ledger_rows
        ]
        # 提现申请 by status
        wd_rows = _safe_rows(
            "SELECT status, COUNT(*) AS cnt, COALESCE(SUM(request_amount_cents),0) AS cents "
            "FROM agent_settlement_requests GROUP BY status")
        withdrawals = [
            {"status": r["status"], "count": int(r["cnt"] or 0), "amount": c2y(r["cents"])}
            for r in wd_rows
        ]
        # 应付代理(已结算未提现)
        payable = c2y(_safe_scalar(
            "SELECT COALESCE(SUM(agent_settlement_cents),0) AS v FROM agent_revenue_ledger WHERE status='settled'"))
        return {"pipeline": pipeline, "withdrawals": withdrawals, "payable_to_agents": payable}

    d = await _run(_do)
    return {"period": p["period"], "label": p["label"], **d}


# ============================================================
# 7. GET /reconciliation · 守恒对账 + 异常清单
# ============================================================

@router.get("/reconciliation")
async def reconciliation(request: Request):
    _require_admin(request)

    def _do():
        latest = _safe_rows(
            "SELECT id, run_at, triggered_by, has_drift, diff_paid, diff_bonus, diff_publish, "
            "agent_total_paid, agent_total_bonus, customer_total_tool, customer_total_publish, "
            "platform_consumed, historical_purchased, historical_admin_adjust, notes "
            "FROM inventory_audit_runs ORDER BY run_at DESC LIMIT 10")
        latest_runs = [
            {"id": r["id"], "run_at": r["run_at"].isoformat() if r.get("run_at") else None,
             "triggered_by": r.get("triggered_by"), "has_drift": r.get("has_drift"),
             "diff_paid": int(r.get("diff_paid") or 0), "diff_bonus": int(r.get("diff_bonus") or 0),
             "diff_publish": int(r.get("diff_publish") or 0)}
            for r in latest
        ]
        # 异常清单
        stale_freeze = int(_safe_scalar(
            "SELECT COUNT(*) AS v FROM point_freezes WHERE status='frozen' AND created_at < %s",
            (datetime.now() - timedelta(hours=48),)))
        clawback = int(_safe_scalar(
            "SELECT COUNT(*) AS v FROM commission_clawback_pending WHERE status='pending'"))
        zero_orders = int(_safe_scalar(
            "SELECT COUNT(*) AS v FROM recharge_orders WHERE payment_status='paid' AND amount_cents=0"))
        return {
            "latest_runs": latest_runs,
            "has_recent_drift": any(r["has_drift"] for r in latest_runs) if latest_runs else False,
            "anomalies": [
                {"key": "stale_freeze", "label": "超 48h 未结算冻结", "count": stale_freeze},
                {"key": "clawback_pending", "label": "未清佣金追回", "count": clawback},
                {"key": "zero_orders", "label": "0 元已付订单", "count": zero_orders},
            ],
        }

    d = await _run(_do)
    return d


# ============================================================
# 8. GET /unit-economics · 单位经济
# ============================================================

@router.get("/unit-economics")
async def unit_economics(
    request: Request,
    period: str = Query("month"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    _require_admin(request)
    p = _resolve_period(period, start, end)

    def _do():
        cash = _cash_revenue(p["since"], p["until"])
        active = int(_safe_scalar(
            "SELECT COUNT(DISTINCT user_id) AS v FROM point_transactions "
            "WHERE type='consume' AND created_at >= %s AND created_at < %s",
            (p["since"], p["until"])))
        paying = int(_safe_scalar(
            "SELECT COUNT(DISTINCT user_id) AS v FROM recharge_orders "
            "WHERE payment_status='paid' "
            "AND COALESCE(paid_at, created_at) >= %s AND COALESCE(paid_at, created_at) < %s",
            (p["since"], p["until"])))
        repeat = int(_safe_scalar(
            "SELECT COUNT(*) AS v FROM (SELECT user_id FROM recharge_orders WHERE payment_status='paid' "
            "GROUP BY user_id HAVING COUNT(*) >= 2) t"))
        return {
            "arpu": round(cash / active, 2) if active else None,
            "arppu": round(cash / paying, 2) if paying else None,
            "active_users": active,
            "paying_users": paying,
            "repeat_buyers": repeat,
            "cash_revenue": cash,
            "cac": None, "ltv": None, "ltv_cac": None,
            "placeholder_note": "CAC / LTV 待营销支出数据源接入(当前无独立营销支出归集)",
        }

    d = await _run(_do)
    return {"period": p["period"], "label": p["label"], **d}


# ============================================================
# 9. GET /ledger · 逐笔账本(分页 + 过滤)
# ============================================================

@router.get("/ledger")
async def ledger(
    request: Request,
    period: str = Query("month"),
    start: Optional[str] = None,
    end: Optional[str] = None,
    user_id: Optional[int] = None,
    brand_id: Optional[int] = None,
    feature_code: Optional[str] = None,
    txn_type: Optional[str] = None,
    point_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    _require_admin(request)
    p = _resolve_period(period, start, end)

    def _do():
        where = ["created_at >= %s", "created_at < %s"]
        params: List[Any] = [p["since"], p["until"]]
        if user_id:
            where.append("user_id = %s"); params.append(user_id)
        if brand_id:
            where.append("brand_id = %s"); params.append(brand_id)
        if feature_code:
            where.append("feature_code = %s"); params.append(feature_code)
        if txn_type:
            where.append("type = %s"); params.append(txn_type)
        if point_type:
            where.append("point_type = %s"); params.append(point_type)
        where_sql = " AND ".join(where)
        total = int(_safe_scalar(
            f"SELECT COUNT(*) AS v FROM point_transactions WHERE {where_sql}", tuple(params)))
        rows = _safe_rows(
            f"SELECT id, user_id, type, point_type, amount, balance_after, feature_code, "
            f"description, order_id, brand_id, source, created_at "
            f"FROM point_transactions WHERE {where_sql} ORDER BY id DESC LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset))
        items = [
            {"id": r["id"], "user_id": r.get("user_id"), "type": r.get("type"),
             "point_type": r.get("point_type"), "amount": int(r.get("amount") or 0),
             "balance_after": int(r.get("balance_after") or 0),
             "feature_code": r.get("feature_code"), "description": r.get("description"),
             "order_id": r.get("order_id"), "brand_id": r.get("brand_id"),
             "source": r.get("source"),
             "created_at": r["created_at"].isoformat() if r.get("created_at") else None}
            for r in rows
        ]
        return {"total": total, "items": items, "limit": limit, "offset": offset}

    d = await _run(_do)
    return {"period": p["period"], "label": p["label"], **d}


# ============================================================
# 10. GET /export · 审计包导出(CSV)
# ============================================================

@router.get("/export")
async def export_csv(
    request: Request,
    period: str = Query("month"),
    basis: str = Query("accrual"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    _require_admin(request)
    p = _resolve_period(period, start, end)

    def _do():
        rev = _revenue_breakdown(p["since"], p["until"])
        cash = _cash_revenue(p["since"], p["until"])
        cogs = _cogs(p["since"], p["until"])
        # [GEO-R1-CAN-033] 现金口径冲减当期已完成退款 · 与权责口径对称(下方"现金收入(到账)"仍列毛额)
        revenue = rev["confirmed"] if basis != "cash" else round(cash - _cash_refund(p["since"], p["until"]), 2)
        gross = round(revenue - cogs["total"], 2)
        opex_rows = sum_opex_by_category(p["opex_start"], p["opex_end"])
        opex_total = round(sum(r["amount_cents"] for r in opex_rows) / 100.0, 2)
        # 🔴 [BH-014] 补取渠道费 —— 改之前这条链**从取数层**就没拿它,
        #    所以下面的营业利润少减一项,而屏幕侧减了。
        gateway_fee = _gateway_fee_yuan(p["since"], p["until"])
        return rev, cash, cogs, revenue, gross, opex_rows, opex_total, gateway_fee

    rev, cash, cogs, revenue, gross, opex_rows, opex_total, gateway_fee = await _run(_do)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["OmniRank 财务审计包", p["label"], f"口径={basis}"])
    w.writerow([])
    w.writerow(["利润表(元)"])
    w.writerow(["确认收入(权责)", rev["confirmed"]])
    w.writerow(["  - 直营消费", rev["direct_consume"]])
    w.writerow(["  - 服务方渠道(出厂价)", rev["agent_factory"]])
    w.writerow(["  - 社媒订阅", rev["subscription"]])
    w.writerow(["  - 退款冲减", -rev["refund"]])
    w.writerow(["现金收入(到账)", cash])
    w.writerow(["COGS 合计", cogs["total"]])
    w.writerow(["  - LLM+监测", cogs["llm"]])
    w.writerow(["  - 媒体采购", cogs["media"]])
    w.writerow(["毛利", gross])
    w.writerow(["毛利率%", round(100.0 * gross / revenue, 1) if revenue and revenue > 0 else ""])
    w.writerow([])
    w.writerow(["运营成本 OpEx(元)"])
    for r in opex_rows:
        w.writerow([f"  - {r['label']}", round(r["amount_cents"] / 100.0, 2)])
    w.writerow(["OpEx 合计", opex_total])
    # 🔴 [BH-014] CSV 里此前**连「支付渠道费」这一行都没有** ⇒ 读者无法自洽核对。
    w.writerow(["支付渠道费", gateway_fee])
    w.writerow(["营业利润", _operating_profit(gross, opex_total, gateway_fee)])

    buf.seek(0)
    fname = f"finance_audit_{p['label'].replace(' ', '_').replace('~', 'to')}.csv"
    # utf-8-sig = UTF-8 + BOM · 让 Excel 正确识别中文
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ============================================================
# OpEx CRUD · operating_expenses 录入(admin)
# ============================================================

class OpexCreate(BaseModel):
    period_month: str          # 'YYYY-MM-01' 或 'YYYY-MM'
    category: str
    amount_cents: int
    note: Optional[str] = None


class OpexUpdate(BaseModel):
    period_month: Optional[str] = None
    category: Optional[str] = None
    amount_cents: Optional[int] = None
    note: Optional[str] = None


def _norm_month(m: str) -> str:
    """'2026-06' / '2026-06-01' → '2026-06-01'。"""
    m = (m or "").strip()
    if len(m) == 7:  # YYYY-MM
        return m + "-01"
    return m


@router.get("/opex")
async def opex_list(
    request: Request,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
):
    _require_admin(request)

    def _do():
        rows = list_operating_expenses(
            _norm_month(period_start) if period_start else None,
            _norm_month(period_end) if period_end else None,
        )
        return [
            {"id": r["id"],
             "period_month": r["period_month"].isoformat() if hasattr(r["period_month"], "isoformat") else str(r["period_month"]),
             "category": r["category"],
             "label": CATEGORY_LABELS.get(r["category"], r["category"]),
             "amount_cents": int(r["amount_cents"] or 0),
             "amount_yuan": round(int(r["amount_cents"] or 0) / 100.0, 2),
             "note": r.get("note")}
            for r in rows
        ]

    items = await _run(_do)
    return {
        "items": items,
        "categories": [{"value": c, "label": CATEGORY_LABELS[c]} for c in VALID_CATEGORIES],
    }


@router.post("/opex")
async def opex_create(request: Request, body: OpexCreate):
    user = _require_admin(request)

    def _do():
        return create_operating_expense(
            _norm_month(body.period_month), body.category,
            int(body.amount_cents or 0), body.note, user.get("user_id") or current_user_id(user))

    row = await _run(_do)
    return {"success": True, "item": row}


@router.put("/opex/{opex_id}")
async def opex_update(request: Request, opex_id: int, body: OpexUpdate):
    _require_admin(request)
    fields = body.model_dump(exclude_none=True)
    if "period_month" in fields:
        fields["period_month"] = _norm_month(fields["period_month"])

    def _do():
        return update_operating_expense(opex_id, fields)

    ok = await _run(_do)
    if not ok:
        raise HTTPException(404, "未找到或无可更新字段")
    return {"success": True}


@router.delete("/opex/{opex_id}")
async def opex_delete(request: Request, opex_id: int):
    _require_admin(request)

    def _do():
        return delete_operating_expense(opex_id)

    ok = await _run(_do)
    if not ok:
        raise HTTPException(404, "未找到")
    return {"success": True}
