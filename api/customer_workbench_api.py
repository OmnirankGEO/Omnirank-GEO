"""
V3.5 W3 · 客户工作台 API · 3 endpoints
- GET /api/customer/credit/summary    当前客户三池余额 + 最近 10 条使用 + 服务方
- GET /api/customer/credit/transactions  额度流水分页
- GET /api/customer/recharge/skus     可购买 SKU 列表(只读解析出的直属服务方显式零售目录)

W3 API 字段铁律:不返 wholesale_cents / platform_cost_cents / agent_revenue_ledger / tax_* / fee_*
"""
import logging
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Query
from db.connection import get_db
from services.commercial_service_routing import (
    RelationshipConflict,
    RelationshipError,
    public_relationship_http_error,
    resolve_commercial_relationship,
)
from schemas.public_contracts import CustomerServiceDTO
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-V35-W3-Customer-API")
router = APIRouter(prefix="/api/customer", tags=["V35-W3-客户工作台"])


def _current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    return {"user_id": user.get("user_id") or current_user_id(user), "is_admin": user.get("is_admin", False)}


def _g(row, key, default=None):
    if row is None: return default
    if isinstance(row, dict): return row.get(key, default)
    return default


def _public_credit_description(type_code: object, points: object) -> str:
    """Never pass ledger prose through; legacy descriptions may contain principals."""
    amount = int(points or 0)
    if amount > 0:
        return "OmniRank 平台额度已到账"
    if amount < 0:
        return "OmniRank 平台功能额度已使用"
    return "OmniRank 平台额度记录"


# ============================================================
# 客户额度 summary
# ============================================================

@router.get("/credit/summary")
async def customer_credit_summary(request: Request):
    """
    返回当前客户:
    - tool/publish/bonus 三池余额
    - 最近 10 条额度使用记录
    - 平台服务配置状态；不返回内部结算主体或推广关系
    """
    u = _current_user(request)
    with get_db() as conn:
        cur = conn.cursor()
        # [单账本收敛 2026-07-27] 余额改读客户自己的 user_wallets。
        # 不改的话这个页面会在迁移后【静默显示 0】—— 客户直接可见,正是这次投诉的成因。
        # 字段名沿用(前端契约不变):tool_credit_points ← paid_points(充值算力),
        # publish_credit_points 恒 0(单账本无独立发布池), bonus_credit_points ← bonus_points。
        # ⚠️ Owner 尚未裁定这个页面最终留还是撤(O-2)。这里按"留着并显示正确数字"实现 ——
        #    若最终决定下线,删页面比让客户看到错数字容易。
        cur.execute("""
            SELECT paid_points  AS tool_credit_points,
                   0            AS publish_credit_points,
                   bonus_points AS bonus_credit_points,
                   total_recharged AS total_purchased_points
            FROM user_wallets
            WHERE user_id = %s
        """, (u["user_id"],))
        w = cur.fetchone()

        try:
            relationship = resolve_commercial_relationship(cur, u["user_id"])
            service = relationship.customer_dto()
        except RelationshipConflict:
            service = CustomerServiceDTO(
                configuration_status="review_required",
                account_configured=False,
                dispute_pending=True,
            )
        except RelationshipError:
            service = CustomerServiceDTO(
                configuration_status="unavailable",
                account_configured=False,
                dispute_pending=False,
            )

        # 最近 10 条流水
        # [单账本收敛 2026-07-27] 流水同样改读 point_transactions。
        # pool 字段前端仍在用,按 point_type 映射:paid→tool / bonus→bonus。
        cur.execute("""
            SELECT type,
                   CASE WHEN point_type = 'bonus' THEN 'bonus' ELSE 'tool' END AS pool,
                   amount AS points,
                   description, created_at
            FROM point_transactions
            WHERE user_id = %s
            ORDER BY created_at DESC LIMIT 10
        """, (u["user_id"],))
        recent = []
        for row in cur.fetchall():
            d = dict(row) if isinstance(row, dict) else {}
            recent.append({
                "type": d.get("type"),
                "pool": d.get("pool"),
                "points": d.get("points"),
                "description": _public_credit_description(d.get("type"), d.get("points")),
                "created_at": d.get("created_at").isoformat() if d.get("created_at") else None,
            })

    return {
        "tool_credit_points": _g(w, "tool_credit_points", 0) or 0,
        "publish_credit_points": _g(w, "publish_credit_points", 0) or 0,
        "bonus_credit_points": _g(w, "bonus_credit_points", 0) or 0,
        "total_purchased_points": _g(w, "total_purchased_points", 0) or 0,
        "service": service.model_dump(),
        "recent_transactions": recent,
    }


# ============================================================
# 客户额度流水
# ============================================================

@router.get("/credit/transactions")
async def customer_credit_transactions(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pool: Optional[str] = Query(None, description="tool / publish / bonus"),
):
    # [单账本收敛 2026-07-27] 流水分页端点改读 point_transactions。
    # 不改的话客户流水页在迁移后会显示【空列表】—— 与余额显示 0 同源的问题。
    # pool 过滤按 point_type 映射:tool/publish → paid(单账本无独立发布池) · bonus → bonus。
    u = _current_user(request)
    where = "WHERE user_id = %s"
    params = [u["user_id"]]
    if pool in ("tool", "publish"):
        where += " AND point_type = %s"
        params.append("paid")
    elif pool == "bonus":
        where += " AND point_type = %s"
        params.append("bonus")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS c FROM point_transactions {where}", params)
        total = int(_g(cur.fetchone(), "c", 0) or 0)
        cur.execute(f"""
            SELECT type,
                   CASE WHEN point_type = 'bonus' THEN 'bonus' ELSE 'tool' END AS pool,
                   amount AS points,
                   description, created_at
            FROM point_transactions
            {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        items = []
        for row in cur.fetchall():
            d = dict(row) if isinstance(row, dict) else {}
            items.append({
                "type": d.get("type"),
                "pool": d.get("pool"),
                "points": d.get("points"),
                "description": _public_credit_description(d.get("type"), d.get("points")),
                "created_at": d.get("created_at").isoformat() if d.get("created_at") else None,
            })
    return {"items": items, "total": total}


# ============================================================
# 客户购买 SKU 列表
# ============================================================

@router.get("/recharge/skus")
async def customer_recharge_skus(request: Request):
    """Return the configured public catalog without exposing its internal owner."""
    u = _current_user(request)
    with get_db() as conn:
        cur = conn.cursor()
        try:
            relationship = resolve_commercial_relationship(cur, u["user_id"])
        except RelationshipError as exc:
            status, detail = public_relationship_http_error(exc)
            logger.warning("recharge catalog relationship unavailable type=%s", type(exc).__name__)
            raise HTTPException(status, detail=detail) from exc
        items = []

        agent_user_id = relationship.service_user_id
        cur.execute("""
            SELECT o.id AS override_id, o.retail_sku_id, o.version AS retail_sku_version,
                   o.sku_template_id, o.points_granted,
                   COALESCE(t.template_code, o.retail_sku_id) AS template_code,
                   COALESCE(t.sku_type, 'credit_pack') AS sku_type,
                   o.custom_name, o.custom_subtitle, o.custom_sales_pitch, o.custom_scene,
                   o.retail_cents AS override_retail,
                   o.is_active AS override_active, o.sort_order
            FROM agent_sku_overrides o
            LEFT JOIN sku_templates t ON t.id=o.source_template_id
            WHERE o.agent_user_id = %s
              AND o.is_active = TRUE AND o.deleted_at IS NULL
            ORDER BY o.sort_order, o.id
        """, (agent_user_id,))
        for row in cur.fetchall():
            d = dict(row) if isinstance(row, dict) else {}
            retail = d.get("override_retail") or 0
            if not retail or int(retail) <= 0:
                continue
            items.append({
                "override_id": d["override_id"],
                "retail_sku_id": d["retail_sku_id"],
                "retail_sku_version": int(d["retail_sku_version"]),
                "sku_template_id": d["sku_template_id"],
                "sku_key": d.get("template_code"),
                "category": d.get("sku_type"),
                "display_name": d.get("custom_name"),
                "subtitle": d.get("custom_subtitle"),
                "promo_text": d.get("custom_sales_pitch"),
                "scene": d.get("custom_scene"),
                "points_granted": int(d.get("points_granted", 0) or 0),
                "retail_cents": int(retail),
                "source": "configured_catalog",
            })
    return {
        "items": items,
        "service": relationship.customer_dto().model_dump(),
    }


# ============================================================
# 历史客户侧商业绑定入口已停用
# ============================================================

@router.post("/bind-agent")
async def customer_bind_agent(request: Request):
    """Legacy customer-side commercial binding is disabled.

    Referral and invitation relationships are promotional only. Commercial
    assignments are configured through the administrator workflow; an
    unassigned customer uses PLATFORM_DIRECT.
    """
    _current_user(request)
    raise HTTPException(
        410,
        detail={
            "code": "PLATFORM_CONFIGURATION_MANAGED",
            "message": "当前账户由 OmniRank 平台提供服务，无需绑定其他账户",
        },
    )
