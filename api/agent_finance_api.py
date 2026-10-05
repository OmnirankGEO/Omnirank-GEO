"""
代理经营财务总览 API · GEO CTO-15.23 · 2026-05-30

代理视角的经营 P&L · 复用 admin 财务中心(finance_api)的聚合 helper · 按 agent_user_id 隔离 ·
口径从"平台立场(出厂价=平台收入)"切换成"代理立场(净毛利=代理应得)"。

全只读 · 0 改扣费/算价逻辑(agent_pricing.py / billing.py 不动)。

代理经营 P&L(等式严格成立 · 见 agent_pricing.calc_settlement:178):
  客户付款总额 GMV(customer_paid_cents)
  − 进货成本/出厂价(factory_cents)
  − 平台费(gateway_fee + settlement_service_fee)
  − 代扣税(tax_withholding)
  = 我的净收益(agent_settlement_cents · 平台应付代理款)
鉴权:仅代理(_require_agent · agent_level≥1)· 严格 agent_user_id 隔离(不串别的代理)。
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Query

from api.finance_api import _resolve_period, _query_db, _run, POINTS_PER_YUAN
from api.agent_workbench_api import _require_agent
from services.agent_revenue import get_agent_balance

logger = logging.getLogger("GEO-AgentFinance")

router = APIRouter(prefix="/api/agent/finance", tags=["代理·经营财务"])


def _safe_row(sql: str, params: tuple = ()) -> Dict[str, Any]:
    """单行聚合 · 失败返回带 __unavailable__ 哨兵的 {}(只读展示绝不让端点 500 · 复用 finance_api._query_db 的 rollback 池卫生)。

    [GEO-R2-CAN-001] 区分"查询失败"与"真实为 0":成功但空账本返回 {}(下游折算为 0 是真实值),
    查询异常返回 {"__unavailable__": True}(下游据此标记该板块 degraded · 前端可显示"暂不可用"而非误报 0)。
    """
    try:
        rows = _query_db(sql, params)
        return rows[0] if rows else {}
    except Exception as e:
        logger.warning(f"[agent-finance] 查询失败(返回空): {e} · sql={sql[:80]}")
        return {"__unavailable__": True}  # [GEO-R2-CAN-001] 哨兵:失败态 ≠ 真实空账本


def _c2y(cents: Any) -> float:
    return round(float(cents or 0) / 100.0, 2)


@router.get("/overview")
async def overview(
    request: Request,
    period: str = Query("month"),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    """代理经营总览:P&L(GMV/进货/平台费/税/净收益)+ 结算池 + 库存 + 客户额度。"""
    a = _require_agent(request)
    aid = a["user_id"]
    p = _resolve_period(period, start, end)

    def _do():
        # 1. 经营 P&L(期间内 · 排除 cancelled · 严格按 agent_user_id 隔离)
        pnl = _safe_row(
            "SELECT COALESCE(SUM(customer_paid_cents),0) AS gmv, "
            "COALESCE(SUM(factory_cents),0) AS factory, "
            "COALESCE(SUM(gateway_fee_cents),0) AS gateway, "
            "COALESCE(SUM(settlement_service_fee_cents),0) AS service_fee, "
            "COALESCE(SUM(tax_withholding_cents),0) AS tax, "
            "COALESCE(SUM(agent_settlement_cents),0) AS net, "
            "COUNT(*) AS orders "
            "FROM agent_revenue_ledger "
            "WHERE agent_user_id=%s AND created_at >= %s AND created_at < %s AND status <> 'cancelled'",
            (aid, p["since"], p["until"]))

        pnl_unavailable = bool(pnl.get("__unavailable__"))  # [GEO-R2-CAN-001]
        gmv = _c2y(pnl.get("gmv"))
        factory = _c2y(pnl.get("factory"))
        platform_fee = round(_c2y(pnl.get("gateway")) + _c2y(pnl.get("service_fee")), 2)
        tax = _c2y(pnl.get("tax"))
        net_margin = _c2y(pnl.get("net"))

        # 2. 结算池(复用 SSOT · cents → 元)
        from db.connection import get_connection
        conn = get_connection()
        settlement_unavailable = False  # [GEO-R2-CAN-001]
        try:
            cur = conn.cursor()
            bal = get_agent_balance(cur, aid)
        except Exception as e:
            logger.warning(f"[agent-finance] 结算余额查询失败: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            bal = {}
            settlement_unavailable = True  # [GEO-R2-CAN-001] 失败态 ≠ 真实 0 结算池
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # 3. 库存(时点 · 积分)
        inv = _safe_row(
            "SELECT COALESCE(paid_inventory_points,0) AS paid, COALESCE(bonus_inventory_points,0) AS bonus, "
            "COALESCE(total_purchased_points,0) AS purchased, COALESCE(total_allocated_points,0) AS allocated "
            "FROM agent_inventory_wallets WHERE agent_user_id=%s",
            (aid,))

        # 4. 客户额度(时点 · 客户数 + 预存未消耗)
        # [单账本收敛 2026-07-27] 客户的算力已并入各自 user_wallets,
        # 客户名单从绑定关系取,预存量取客户 user_wallets 余额(不再读信用钱包,否则恒 0)。
        cust = _safe_row(
            "SELECT COUNT(*) AS cnt, "
            "COALESCE(SUM(COALESCE(uw.paid_points,0)+COALESCE(uw.bonus_points,0)),0) AS outstanding "
            "FROM customer_agent_bindings cab "
            "LEFT JOIN user_wallets uw ON uw.user_id = cab.customer_user_id "
            "WHERE cab.agent_user_id=%s",
            (aid,))

        # [GEO-R2-CAN-001] 每板块暴露 unavailable 布尔(查询失败 ≠ 真实 0)· 前端据此显示"暂不可用"而非误报 0
        inv_unavailable = bool(inv.get("__unavailable__"))
        cust_unavailable = bool(cust.get("__unavailable__"))

        return {
            "degraded": pnl_unavailable or settlement_unavailable or inv_unavailable or cust_unavailable,  # [GEO-R2-CAN-001]
            "pnl": {
                "gmv": gmv,
                "factory_cost": factory,
                "platform_fee": platform_fee,
                "tax": tax,
                "net_margin": net_margin,
                "orders": int(pnl.get("orders") or 0),
                "unavailable": pnl_unavailable,  # [GEO-R2-CAN-001]
            },
            "settlement": {
                "frozen": _c2y(bal.get("frozen_cents")),
                "available": _c2y(bal.get("available_cents")),
                "pending": _c2y(bal.get("pending_payout_cents")),
                "paid": _c2y(bal.get("paid_cents")),
                "unavailable": settlement_unavailable,  # [GEO-R2-CAN-001]
            },
            "inventory": {
                "paid_points": int(inv.get("paid") or 0),
                "bonus_points": int(inv.get("bonus") or 0),
                "total_purchased_points": int(inv.get("purchased") or 0),
                "total_allocated_points": int(inv.get("allocated") or 0),
                "unavailable": inv_unavailable,  # [GEO-R2-CAN-001]
            },
            "customers": {
                "count": int(cust.get("cnt") or 0),
                "credit_outstanding_points": int(cust.get("outstanding") or 0),
                "unavailable": cust_unavailable,  # [GEO-R2-CAN-001]
            },
        }

    d = await _run(_do)
    return {"period": p["period"], "label": p["label"], **d}
