"""[v6 req3] 资金补偿工单 · 人工处置 API/后台入口(admin)。

- GET  /api/admin/fund-recovery            列未终结工单(pending/processing/manual)或按 status 过滤
- GET  /api/admin/fund-recovery/{id}       单工单详情
- POST /api/admin/fund-recovery/{id}/resolve  admin 确认已处理 → resolved
- POST /api/admin/fund-recovery/{id}/fail      admin 判定无需/无法补偿 → failed

不直接在此动钱(退款/结算走各自 billing 幂等接口后再来 resolve)· 仅登记人工裁决 + 审计。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-AdminFundRecovery")
router = APIRouter(prefix="/api/admin/fund-recovery", tags=["资金补偿工单"])


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")
    return {"user_id": user.get("user_id") or current_user_id(user)}


class DispositionRequest(BaseModel):
    note: Optional[str] = None


class GeoTaskDispositionRequest(BaseModel):
    # [v7 finding2 修] terminal 改【可选偏好】· 不再作为可信终态。真实终态由后端【读 freeze 真状态派生】:
    #   committed→只能 done / released→只能失败态 · 与请求矛盾则拒绝(防已扣款却标失败 / 已退款却交付)。
    terminal: Optional[str] = None   # 仅当 freeze released 时用于选失败味(failed/cancelled/timeout)· 其余被派生覆盖
    note: Optional[str] = None


def _iso(v):
    try:
        return v.isoformat() if v else None
    except Exception:
        return v


def _fmt(d: dict) -> dict:
    d = dict(d)
    for k in ("created_at", "updated_at", "resolved_at", "next_retry_at", "claimed_at"):
        if k in d:
            d[k] = _iso(d[k])
    return d


@router.get("")
async def list_orders(request: Request,
                      status: Optional[str] = Query(None, description="pending/processing/manual/resolved/failed · 空=未终结"),
                      limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    _require_admin(request)
    from db.fund_recovery_db import list_recovery_orders
    items = [_fmt(r) for r in list_recovery_orders(status=status, limit=limit, offset=offset)]
    return {"items": items, "count": len(items)}


@router.get("/{order_id}")
async def get_order(order_id: int, request: Request):
    _require_admin(request)
    from db.fund_recovery_db import get_recovery_order
    row = get_recovery_order(order_id)
    if not row:
        raise HTTPException(404, "工单不存在")
    return _fmt(row)


def _reject_geo_settle_via_generic(order_id: int):
    """[v8 P2] 禁止 source='geo_plan_settle'(settle_conflict 冲突工单)走【通用终结接口】。

    这类工单必须走 POST /{id}/settle-geo-task —— 它会【同时】把关联 geo_plan_task 落到与 freeze 一致的终态。
    走通用 resolve/fail 只关工单不动任务 → 工单 resolved 但任务永久卡 settle_conflict(两者脱钩)。
    """
    from db.fund_recovery_db import get_recovery_order
    wo = get_recovery_order(order_id)
    if wo and (wo.get("source") or "") == "geo_plan_settle":
        raise HTTPException(400, "GEO 结算冲突工单必须走 /settle-geo-task 收口(通用接口会致工单与任务脱钩)")


@router.post("/{order_id}/resolve")
async def resolve_order(order_id: int, req: DispositionRequest, request: Request):
    a = _require_admin(request)
    _reject_geo_settle_via_generic(order_id)   # [v8 P2] GEO 结算冲突工单禁走通用接口
    from db.fund_recovery_db import resolve_recovery_order
    ok = resolve_recovery_order(order_id, note=f"admin={a['user_id']} resolve: {req.note or ''}")
    if not ok:
        raise HTTPException(409, "工单不存在或已终结,无法处置")
    logger.warning(f"[admin_fund_recovery] admin={a['user_id']} 工单 {order_id} → resolved · {req.note}")
    return {"success": True, "order_id": order_id, "status": "resolved"}


@router.post("/{order_id}/fail")
async def fail_order(order_id: int, req: DispositionRequest, request: Request):
    a = _require_admin(request)
    _reject_geo_settle_via_generic(order_id)   # [v8 P2] GEO 结算冲突工单禁走通用接口
    from db.fund_recovery_db import fail_recovery_order
    ok = fail_recovery_order(order_id, note=f"admin={a['user_id']} fail: {req.note or ''}")
    if not ok:
        raise HTTPException(409, "工单不存在或已终结,无法处置")
    logger.warning(f"[admin_fund_recovery] admin={a['user_id']} 工单 {order_id} → failed · {req.note}")
    return {"success": True, "order_id": order_id, "status": "failed"}


@router.post("/{order_id}/settle-geo-task")
async def settle_geo_task(order_id: int, req: GeoTaskDispositionRequest, request: Request):
    """[v7 finding2 修] settle_conflict 任务的【人工收口出口】· 据【真实 freeze 状态】派生终态,不信任请求终态。

    source='geo_plan_settle' 的冲突工单:后端读 freeze 真状态(committed→只能 done / released→只能失败态 /
    frozen→拒绝交补偿队列 / 未定→拒绝需人工核两表),派生【唯一合法终态】+ CAS 落终态 + 对应通知,再 resolved 工单。
    —— 消除"已扣款却标失败 / 已退款却交付"的伪造资金终态,以及 settle_conflict 永久孤儿态。
    """
    a = _require_admin(request)
    if req.terminal is not None and req.terminal not in ("done", "failed", "cancelled", "timeout"):
        raise HTTPException(400, "terminal 若传须为 done/failed/cancelled/timeout(仅作偏好 · 真实终态由 freeze 派生)")
    from db.fund_recovery_db import get_recovery_order, resolve_recovery_order
    wo = get_recovery_order(order_id)
    if not wo:
        raise HTTPException(404, "工单不存在")
    if wo.get("source") != "geo_plan_settle":
        raise HTTPException(400, "该工单不是 GEO 结算冲突工单,不可用本接口")
    payload = wo.get("payload") or {}
    if isinstance(payload, str):
        import json as _json
        try:
            payload = _json.loads(payload)
        except Exception:
            payload = {}
    task_id = payload.get("task_id")
    if not task_id:
        raise HTTPException(400, "工单缺 task_id,无法收口任务")

    from services.geo_plan_settlement import resolve_settle_conflict
    res = resolve_settle_conflict(int(task_id), a["user_id"], requested_terminal=req.terminal)
    if not res.get("ok"):
        # [v8 对抗审 P2] 任务【已是终态】(幂等重跑 / 先标任务后关工单之间崩溃后重试 / 重复工单)→ 幂等收口本工单,
        #   绝不 409 把工单卡成永久 un-closeable(通用接口已被拦 · 唯一出口就是这里)。
        from db.geo_plan_tasks_db import get_task_by_id
        _t = get_task_by_id(int(task_id))
        if _t and _t.get("status") in ("done", "failed", "cancelled", "timeout"):
            ok_wo = resolve_recovery_order(order_id, note=f"admin={a['user_id']} 任务已终态 {_t.get('status')} · 幂等收口工单")
            logger.warning(f"[admin_fund_recovery] task={task_id} 已终态 {_t.get('status')} · 工单 {order_id} 幂等 resolved={ok_wo}")
            return {"success": True, "order_id": order_id, "task_id": task_id,
                    "task_status": _t.get("status"), "note": "idempotent_already_terminal"}
        # 否则:freeze 状态与请求矛盾 / freeze 未终结 / 状态无法确定 → 拒绝(不伪造资金终态)
        raise HTTPException(409, res.get("reason") or "无法收口(freeze 状态不允许)")
    terminal = res["terminal"]
    ok_wo = resolve_recovery_order(order_id, note=f"admin={a['user_id']} 收口 GEO 任务 {task_id}→{terminal}"
                                                  f"(freeze={res.get('freeze_status')}): {req.note or ''}")
    if not ok_wo:
        # [v8 对抗审 P2] 任务已落终态但工单收口失败(不静默吞)· 巡检 ensure_settle_conflict_workorders 会反向收口
        logger.error(f"[admin_fund_recovery] task={task_id} 已落终态 {terminal} 但工单 {order_id} 收口失败 · 巡检将反向收口")
    logger.warning(f"[admin_fund_recovery] admin={a['user_id']} settle_conflict 收口 task={task_id}→{terminal} "
                   f"· freeze={res.get('freeze_status')} · 工单 {order_id} resolved={ok_wo}")
    return {"success": True, "order_id": order_id, "task_id": task_id, "task_status": terminal,
            "freeze_status": res.get("freeze_status"), "workorder_resolved": ok_wo}
