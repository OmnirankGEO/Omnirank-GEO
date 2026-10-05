"""
V3.5 W4 · Admin 治理 API
- GET /api/admin/binding-disputes
- PATCH /api/admin/binding-disputes/{id}
- POST /api/admin/inventory-audit/run
- GET /api/admin/inventory-audit/history
"""
import logging
import uuid
from typing import Literal, Optional, List
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from db.connection import get_db
from services.commercial_binding_history import (
    append_current_version,
    close_current_version,
)
from services.commercial_service_routing import (
    RelationshipConflict,
    lock_commercial_binding_subject,
    lock_commercial_provider_lifecycle,
    lock_commercial_provider_for_assignment,
    lock_pending_commercial_orders,
)
from services.admin_user_governance import record_external_commercial_binding_change
from services.inventory_audit import run_audit, list_audit_runs
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-V35-W4-Admin-API")
router = APIRouter(prefix="/api/admin", tags=["V35-W4-Admin 治理"])


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")
    return {"user_id": user.get("user_id") or current_user_id(user)}


def _g(row, key, default=None):
    if row is None: return default
    if isinstance(row, dict): return row.get(key, default)
    return default


# ============================================================
# 绑定争议
# ============================================================

class DisputePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["keep_old", "reassign", "reject"]
    admin_decision: Optional[str] = Field(default=None, max_length=500)
    note: str = Field(min_length=2, max_length=500)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("处置原因至少需要 2 个字符")
        return value


@router.get("/binding-disputes")
async def list_disputes(
    request: Request,
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    _require_admin(request)
    where = "WHERE 1=1"
    params: list = []
    if status:
        where += " AND d.status = %s"
        params.append(status)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS c FROM customer_agent_binding_disputes d {where}", params)
        total = int(_g(cur.fetchone(), "c", 0) or 0)
        cur.execute(f"""
            SELECT d.id, d.customer_user_id, d.old_agent_user_id, d.new_agent_user_id,
                   d.binding_source, d.source_token, d.status, d.admin_decision,
                   d.note, d.created_at, d.resolved_at,
                   uc.display_name AS customer_name,
                   uo.display_name AS old_agent_name,
                   un.display_name AS new_agent_name
            FROM customer_agent_binding_disputes d
            LEFT JOIN users uc ON uc.id = d.customer_user_id
            LEFT JOIN users uo ON uo.id = d.old_agent_user_id
            LEFT JOIN users un ON un.id = d.new_agent_user_id
            {where}
            ORDER BY d.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        items = []
        for row in cur.fetchall():
            d = dict(row) if isinstance(row, dict) else {}
            d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
            d["resolved_at"] = d["resolved_at"].isoformat() if d.get("resolved_at") else None
            items.append(d)
    return {"items": items, "total": total}


@router.patch("/binding-disputes/{dispute_id}")
async def resolve_dispute(dispute_id: int, req: DisputePatchRequest, request: Request):
    """
    admin 处置 dispute:
    - keep_old:维持原 agent · customer_agent_bindings 不改 + UPDATE dispute_status=NULL + INSERT resolved_keep_old
    - reassign:改绑到 new_agent · UPDATE bindings.agent_user_id=new + dispute_status=NULL + INSERT resolved_reassign
    - reject:拒绝处理 · INSERT rejected · 历史订单 attribution 不回改 · future 生效

    历史 ledger / recharge snapshot 不回改
    """
    a = _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        # First discover only the lock subject. Do not take a dispute row lock
        # before the customer-scoped advisory lock, or writers can deadlock.
        cur.execute("""
            SELECT customer_user_id
            FROM customer_agent_binding_disputes WHERE id = %s
        """, (dispute_id,))
        subject_row = cur.fetchone()
        if not subject_row:
            raise HTTPException(404, "dispute 不存在")
        customer_id = _g(subject_row, "customer_user_id")
        from services.commercial_service_routing import lock_commercial_relationship

        lock_commercial_relationship(cur, int(customer_id))
        # Fixed lock order: advisory → binding row → dispute row.
        cur.execute(
            "SELECT id FROM customer_agent_bindings "
            "WHERE customer_user_id = %s FOR UPDATE",
            (customer_id,),
        )
        cur.fetchone()
        cur.execute("""
            SELECT id, customer_user_id, old_agent_user_id, new_agent_user_id, status
            FROM customer_agent_binding_disputes WHERE id = %s FOR UPDATE
        """, (dispute_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "dispute 不存在")
        cur_status = _g(row, "status")
        if cur_status != "pending":
            raise HTTPException(400, f"dispute 状态 {cur_status} 不可再处置")
        if int(_g(row, "customer_user_id")) != int(customer_id):
            raise HTTPException(409, "dispute 归属在并发处理中发生变化，请重试")
        old_agent = _g(row, "old_agent_user_id")
        new_agent = _g(row, "new_agent_user_id")
        request_id = str(
            getattr(request.state, "request_id", None)
            or request.headers.get("X-Request-ID")
            or uuid.uuid4().hex
        )[:128]
        selected_provider = int(new_agent if req.action == "reassign" else old_agent)
        lock_commercial_provider_lifecycle(cur, selected_provider)
        if req.action == "reassign":
            # Settlement locks an order before resolving the commercial subject.
            # Match that order here and fail before any dispute/history mutation.
            if lock_pending_commercial_orders(cur, int(customer_id)):
                raise HTTPException(409, "仍有待支付订单锁定当前服务方，请等待支付终态后再改派")
        lock_commercial_binding_subject(cur, int(customer_id))
        cur.execute(
            "SELECT COALESCE(agent_level,0) AS agent_level FROM user_wallets "
            "WHERE user_id=%s FOR UPDATE",
            (int(customer_id),),
        )
        subject_wallet = cur.fetchone()
        if not subject_wallet or int(_g(subject_wallet, "agent_level", 0) or 0) >= 1:
            raise HTTPException(422, "商业服务归属只适用于普通用户；服务商必须走渠道关系治理")
        if req.action == "reassign" and lock_pending_commercial_orders(cur, int(customer_id)):
            raise HTTPException(409, "仍有待支付订单锁定当前服务方，请等待支付终态后再改派")
        cur.execute(
            "SELECT * FROM customer_agent_bindings WHERE customer_user_id=%s FOR UPDATE",
            (int(customer_id),),
        )
        projection_before = cur.fetchone()
        if not projection_before:
            raise HTTPException(409, "商业服务关系不存在，已拒绝裁决")
        projection_before = dict(projection_before)
        if (
            int(projection_before.get("agent_user_id") or 0) != int(old_agent)
            or projection_before.get("dispute_status") != "pending"
        ):
            raise HTTPException(409, "商业服务关系已被其他操作改变，请刷新后重新核验")
        try:
            lock_commercial_provider_for_assignment(cur, selected_provider)
        except RelationshipConflict as exc:
            raise HTTPException(409, "裁决承接方未通过服务商、权限或价目范围检查") from exc
        ended_history_id = close_current_version(
            cur,
            customer_user_id=int(customer_id),
            projection=projection_before,
            operator_user_id=int(a["user_id"]),
            reason=req.note,
            request_id=request_id,
        )

        new_status = {
            "keep_old": "resolved_keep_old",
            "reassign": "resolved_reassign",
            "reject": "rejected",
        }[req.action]

        # 更新 dispute 记录
        cur.execute("""
            UPDATE customer_agent_binding_disputes
            SET status = %s, admin_decision = %s, admin_user_id = %s,
                note = %s, resolved_at = NOW()
            WHERE id = %s
        """, (new_status, req.admin_decision or req.action, a["user_id"], req.note, dispute_id))

        # 更新 customer_agent_bindings 当前状态 column
        if req.action == "reassign":
            cur.execute("""
                UPDATE customer_agent_bindings
                SET agent_user_id = %s, dispute_status = NULL, dispute_note = NULL,
                    binding_source='admin_manual',source_token=NULL,bound_at=NOW(),
                    admin_override_user_id = %s, admin_override_at = NOW()
                WHERE customer_user_id = %s
            """, (new_agent, a["user_id"], customer_id))
            # [GEO-R1-CAN-039] 同事务同步授权钱包的 agent_user_id · 否则 split-brain:
            # customer_agent_credit_wallets PK=customer_user_id 有独立 agent_user_id 列,
            # 改绑后仍留旧 agent → 历史流水的服务商归属会对不上。
            # [历史账本只读 · 2026-08-17] 该表已于 2026-07-29 停写(三池清零 · 行保留)。
            #   本 UPDATE 只同步**归属投影**(agent_user_id),不动任何金额;保留的理由是
            #   customer_credit.refund_credit(在途 v35 冻结 release 的历史通道)仍会读
            #   wallet["agent_user_id"] 写流水。客户无 wallet 行则命中 0 行(安全)。
            #   ⚠️ 原注释引用的 consume_credit 已随单账本接线删除,此处不再有消费侧依赖。
            cur.execute("""
                UPDATE customer_agent_credit_wallets
                SET agent_user_id = %s
                WHERE customer_user_id = %s
            """, (new_agent, customer_id))
        else:
            # keep_old / reject confirms the old provider as a new, administrator-
            # evidenced relationship version.  The closed history row above keeps
            # the original source/token/bound_at unchanged; the current projection
            # must point at this new manual decision so readiness/evidence queries
            # do not mistake it for the historical source fact.
            cur.execute("""
                UPDATE customer_agent_bindings
                SET dispute_status = NULL, dispute_note = NULL,
                    binding_source='admin_manual',source_token=NULL,bound_at=NOW(),
                    admin_override_user_id = %s, admin_override_at = NOW()
                WHERE customer_user_id = %s
            """, (a["user_id"], customer_id))

        cur.execute(
            "SELECT * FROM customer_agent_bindings WHERE customer_user_id=%s",
            (int(customer_id),),
        )
        projection_after = dict(cur.fetchone())
        history_id = append_current_version(
            cur,
            customer_user_id=int(customer_id),
            provider_user_id=int(projection_after["agent_user_id"]),
            source_binding_id=int(projection_after["id"]),
            operator_user_id=int(a["user_id"]),
            reason=req.note,
            request_id=request_id,
            binding_source=str(projection_after.get("binding_source") or "admin_manual"),
            source_token=projection_after.get("source_token"),
        )
        relationship_version = record_external_commercial_binding_change(
            cur,
            subject_user_id=int(customer_id),
            operator_user_id=int(a["user_id"]),
            operator_username=a.get("username"),
            request_id=request_id,
            reason=req.note,
            before={
                "commercial_provider_user_id": int(projection_before["agent_user_id"]),
                "commercial_mode": "service_provider",
                "dispute_status": projection_before.get("dispute_status"),
            },
            after={
                "commercial_provider_user_id": int(projection_after["agent_user_id"]),
                "commercial_mode": "service_provider",
                "dispute_status": projection_after.get("dispute_status"),
            },
            ip_address=request.client.host if request.client else None,
            evidence={
                "binding_id": int(projection_after["id"]),
                "dispute_id": int(dispute_id),
                "resolution_action": req.action,
                "ended_history_id": ended_history_id,
                "new_history_id": history_id,
            },
        )

        # [v6 req2 · 老板决策] 原子处置本 dispute 关联的 held escrow(与 binding 更新同事务):
        #   keep_old → 结算给原服务商 X(old_agent);reassign → 结算给新服务商 Y(new_agent);reject → 退款给客户。
        #   幂等:escrow settle/refund 仅作用于 held(重复裁决 no-op)· 客户额度在 escrow 内不可消费(已消费竞态天然防)。
        escrow_result = {"settled": 0, "refunded": 0, "skipped": 0}
        try:
            from db.dispute_escrow_db import list_held_for_dispute, settle_escrow, refund_escrow
            held = list_held_for_dispute(cur, dispute_id)   # FOR UPDATE 锁行
            for esc in held:
                if req.action == "keep_old":
                    ok = settle_escrow(cur, esc, old_agent, a["user_id"])
                    escrow_result["settled" if ok else "skipped"] += 1
                elif req.action == "reassign":
                    ok = settle_escrow(cur, esc, new_agent, a["user_id"])
                    escrow_result["settled" if ok else "skipped"] += 1
                else:  # reject
                    ok = refund_escrow(cur, esc, a["user_id"])
                    escrow_result["refunded" if ok else "skipped"] += 1
        except Exception as e:
            # escrow 处置失败必须回滚整个裁决(不能 binding 改了钱没动)→ raise 触发 with get_db 的 rollback
            logger.exception(f"[admin_dispute] dispute={dispute_id} escrow 处置失败 · 回滚裁决: {e}")
            raise HTTPException(500, f"escrow 处置失败,裁决已回滚: {e}")

        from datetime import datetime, timezone
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import (
            enqueue_admin_notification_events,
            enqueue_notification_event,
        )

        dispute_facts = {
            "business_no": f"DISPUTE-{int(dispute_id)}",
            "status": "订单核验已完成",
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": "请在账户页面查看最终业务状态。",
        }
        enqueue_notification_event(
            cur,
            event_type=NotificationEventType.DISPUTE_RESOLVED,
            business_id=str(dispute_id),
            terminal_state=new_status,
            recipient_user_id=int(customer_id),
            recipient_kind=RecipientKind.CUSTOMER,
            facts=dispute_facts,
        )
        enqueue_admin_notification_events(
            cur,
            event_type=NotificationEventType.DISPUTE_RESOLVED,
            business_id=str(dispute_id),
            terminal_state=new_status,
            facts=dispute_facts,
        )

        conn.commit()
    logger.info(
        f"[admin_dispute] admin={a['user_id']} dispute={dispute_id} action={req.action} · "
        f"customer={customer_id} old_agent={old_agent} new_agent={new_agent} · escrow={escrow_result}"
    )
    return {
        "success": True, "dispute_id": dispute_id, "new_status": new_status,
        "escrow": escrow_result, "request_id": request_id,
        "relationship_history": {"ended_id": ended_history_id, "current_id": history_id},
        "commercial_binding_version": relationship_version,
    }


# ============================================================
# 对账
# ============================================================

@router.post("/inventory-audit/run")
async def admin_audit_run(request: Request):
    """手动触发一次对账 · 写 run + diff(若 drift)

    异常路径与 cron 同规格(2026-07-29 返修):落一行 status='failed' + 告警,
    再把 500 抛给 admin —— 手动跑炸了同样不能只留一条日志。
    """
    a = _require_admin(request)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            result = run_audit(cur, triggered_by="admin_manual", triggered_by_user_id=a["user_id"])
            conn.commit()
    except Exception as e:  # noqa: BLE001
        from services.inventory_audit import record_audit_failure
        failed_run_id = record_audit_failure(e, triggered_by="admin_manual",
                                             triggered_by_user_id=a["user_id"])
        raise HTTPException(
            status_code=500,
            detail=f"对账运行失败已记录(run_id={failed_run_id}):{type(e).__name__}: {e}",
        )
    return result


@router.get("/inventory-audit/history")
async def admin_audit_history(
    request: Request,
    limit: int = Query(30, ge=1, le=200),
    drift_only: bool = Query(False),
):
    _require_admin(request)
    with get_db() as conn:
        cur = conn.cursor()
        runs = list_audit_runs(cur, limit=limit, drift_only=drift_only)
    return {"items": runs}
