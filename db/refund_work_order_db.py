"""Refund work order persistence.

This module adds a work-order layer above the existing wallet refund action.
Creating or submitting a work order never performs system reconciliation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from db.connection import get_connection, get_db
from services.commercial_service_routing import lock_commercial_provider_lifecycle

logger = logging.getLogger("GEO-RefundWorkOrder-DB")

OPEN_STATUSES = ("draft", "submitted", "approved", "payout_pending")
TERMINAL_STATUSES = ("completed", "rejected", "cancelled")
SYSTEM_ONLY_METHOD = "system_only"


def _row(row: Any) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _iso(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def init_refund_work_order_tables(cursor=None):
    """Create refund work-order tables and indexes idempotently."""
    owns_connection = cursor is None
    conn = None
    if owns_connection:
        conn = get_connection()
        cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS refund_work_orders (
                id BIGSERIAL PRIMARY KEY,
                source_order_id TEXT NOT NULL,
                customer_user_id INTEGER,
                agent_user_id INTEGER,
                refund_reason_category TEXT NOT NULL DEFAULT 'other',
                refund_reason_detail TEXT DEFAULT '',
                refund_method TEXT NOT NULL DEFAULT 'manual_wechat',
                requested_refund_cents INTEGER NOT NULL DEFAULT 0,
                estimated_refund_cents INTEGER NOT NULL DEFAULT 0,
                refundable_power BIGINT NOT NULL DEFAULT 0,
                customer_requested_at TIMESTAMP,
                agent_confirmed_at TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'draft',
                impact_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_by INTEGER,
                reviewed_by INTEGER,
                payout_operator_id INTEGER,
                payout_proof_url TEXT,
                rejected_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                submitted_at TIMESTAMP,
                reviewed_at TIMESTAMP,
                executed_at TIMESTAMP,
                completed_at TIMESTAMP,
                last_action_error TEXT,
                execution_result JSONB,
                CONSTRAINT refund_work_orders_status_check CHECK (
                    status IN ('draft','submitted','approved','payout_pending','completed','rejected','cancelled')
                )
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS refund_work_order_attachments (
                id BIGSERIAL PRIMARY KEY,
                work_order_id BIGINT NOT NULL REFERENCES refund_work_orders(id) ON DELETE CASCADE,
                file_url TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                evidence_type TEXT NOT NULL DEFAULT 'other',
                mime_type TEXT,
                file_size_bytes BIGINT NOT NULL DEFAULT 0,
                uploaded_by INTEGER,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS refund_work_order_events (
                id BIGSERIAL PRIMARY KEY,
                work_order_id BIGINT NOT NULL REFERENCES refund_work_orders(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                actor_user_id INTEGER,
                note TEXT DEFAULT '',
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_refund_work_orders_status_time
            ON refund_work_orders(status, created_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_refund_work_orders_order
            ON refund_work_orders(source_order_id)
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_refund_work_orders_open_order
            ON refund_work_orders(source_order_id)
            WHERE status IN ('draft','submitted','approved','payout_pending')
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_refund_work_order_attachments_work_order
            ON refund_work_order_attachments(work_order_id, uploaded_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_refund_work_order_events_work_order
            ON refund_work_order_events(work_order_id, created_at ASC)
        """)
        if owns_connection:
            conn.commit()
        logger.info("[RefundWorkOrder] tables ready")
    finally:
        if owns_connection and conn:
            conn.close()


def _format_work_order(record: dict) -> dict:
    out = dict(record)
    for key in (
        "customer_requested_at", "agent_confirmed_at", "created_at", "updated_at",
        "submitted_at", "reviewed_at", "executed_at", "completed_at",
    ):
        if key in out:
            out[key] = _iso(out[key])
    snap = out.get("impact_snapshot")
    if isinstance(snap, str):
        try:
            out["impact_snapshot"] = json.loads(snap)
        except Exception:
            out["impact_snapshot"] = {}
    result = out.get("execution_result")
    if isinstance(result, str):
        try:
            out["execution_result"] = json.loads(result)
        except Exception:
            out["execution_result"] = None
    return out


def _format_attachment(record: dict) -> dict:
    out = dict(record)
    out["uploaded_at"] = _iso(out.get("uploaded_at"))
    return out


def _format_event(record: dict) -> dict:
    out = dict(record)
    out["created_at"] = _iso(out.get("created_at"))
    payload = out.get("payload")
    if isinstance(payload, str):
        try:
            out["payload"] = json.loads(payload)
        except Exception:
            out["payload"] = {}
    return out


def add_work_order_event(cursor, work_order_id: int, event_type: str, actor_user_id: Optional[int],
                         note: str = "", payload: Optional[dict] = None):
    cursor.execute("""
        INSERT INTO refund_work_order_events (work_order_id, event_type, actor_user_id, note, payload)
        VALUES (%s, %s, %s, %s, %s::jsonb)
    """, (work_order_id, event_type, actor_user_id, note or "", _json(payload)))


def find_order_for_refund_query(query: str) -> Optional[str]:
    """Resolve admin search text to a recharge order id."""
    q = (query or "").strip()
    if not q:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM recharge_orders WHERE id = %s LIMIT 1", (q,))
        row = cur.fetchone()
        if row:
            return _row(row)["id"]

        like = f"%{q}%"
        cur.execute("""
            SELECT ro.id
            FROM recharge_orders ro
            JOIN users u ON u.id = ro.user_id
            WHERE u.phone = %s
               OR u.username ILIKE %s
               OR u.display_name ILIKE %s
            ORDER BY ro.created_at DESC
            LIMIT 1
        """, (q, like, like))
        row = cur.fetchone()
        return _row(row).get("id") if row else None
    finally:
        conn.close()


def build_refund_order_preview(order_id: str) -> dict:
    """Build an order and impact preview without mutating state."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ro.*,
                   cu.username AS customer_username,
                   cu.display_name AS customer_name,
                   cu.phone AS customer_phone,
                   au.username AS agent_username,
                   au.display_name AS agent_name,
                   au.phone AS agent_phone
            FROM recharge_orders ro
            LEFT JOIN users cu ON cu.id = ro.user_id
            LEFT JOIN users au ON au.id = ro.agent_user_id
            WHERE ro.id = %s
        """, (order_id,))
        order = _row(cur.fetchone())
        if not order:
            return {"found": False, "message": "未找到订单"}

        amount_cents = int(order.get("amount_cents") or 0)
        base_power = int(order.get("base_points") or 0)
        bonus_power = int(order.get("bonus_points") or 0)
        total_power = max(0, base_power + bonus_power)
        paid_at = order.get("paid_at")
        refund_status = order.get("refund_status")
        payment_status = order.get("payment_status")

        consumed_since = 0
        if paid_at:
            try:
                cur.execute("""
                    SELECT COALESCE(SUM(ABS(amount)), 0)::bigint AS consumed
                    FROM point_transactions
                    WHERE user_id = %s AND type = 'consume' AND created_at > %s
                """, (order.get("user_id"), paid_at))
                consumed_since = int((_row(cur.fetchone()).get("consumed") or 0))
            except Exception as exc:
                logger.warning("[RefundWorkOrder] consume estimate failed order=%s: %s", order_id, exc)

        refundable_power = max(0, total_power - consumed_since)
        refundable_ratio = (refundable_power / total_power) if total_power else 0
        estimated_refund_cents = int(amount_cents * refundable_ratio)

        wallet_total = 0
        try:
            cur.execute("""
                SELECT paid_points, bonus_points, commission_points
                FROM user_wallets WHERE user_id = %s
            """, (order.get("user_id"),))
            wallet = _row(cur.fetchone())
            wallet_total = int(wallet.get("paid_points") or 0) + int(wallet.get("bonus_points") or 0)
        except Exception:
            wallet_total = 0

        # [历史账本只读 · 2026-08-17] 该表停写且三池清零 → 本项恒 +0,只影响下方
        # "客户余额足够扣减" 这个展示徽标,**不参与退款金额计算**
        # (退款额 = amount_cents × refundable_ratio,ratio 取自 point_transactions)。
        try:
            cur.execute("""
                SELECT COALESCE(SUM(tool_credit_points + publish_credit_points + bonus_credit_points), 0)::bigint AS credit_total
                FROM customer_agent_credit_wallets
                WHERE customer_user_id = %s
            """, (order.get("user_id"),))
            wallet_total += int((_row(cur.fetchone()).get("credit_total") or 0))
        except Exception:
            pass

        agent_reversal_cents = 0
        agent_revenue_settled = False
        try:
            cur.execute("""
                SELECT COALESCE(SUM(agent_settlement_cents), 0)::bigint AS amount_cents,
                       BOOL_OR(status = 'settled') AS has_settled
                FROM agent_revenue_ledger
                WHERE recharge_order_id = %s
            """, (order_id,))
            ledger = _row(cur.fetchone())
            agent_reversal_cents = int(ledger.get("amount_cents") or 0)
            agent_revenue_settled = bool(ledger.get("has_settled"))
        except Exception:
            # [WO_299b] 兜底分支。两处改动:
            #   ① 原来先转浮点再乘 100 取整:浮点截断,1.15 元记成 114 分 ⇒ 改为精确换分;
            #   ② 换算原来在 try 里,失败会被下面的 except 吞成 0 ⇒ 挪到 try 外,只让「查询失败」回落 0。
            # 🔴 可达性(已核,见 WO_299b 交付单):本函数的连接来自连接池(autocommit=False),上面那条查询若在 SQL 层失败,
            #    同一事务已中止,这里的查询必然以 InFailedSqlTransaction 失败 ⇒ 回落 0;随后 `SELECT now()` 也会失败,整个预览抛错。
            #    所以这条兜底在现状下拿不到值;要让它真正可用,需要给上面那条查询包保存点(本单未改,交 Review 定)。
            pc_amount_yuan = None
            try:
                cur.execute("""
                    SELECT COALESCE(SUM(amount_yuan), 0) AS amount_yuan,
                           BOOL_OR(status = 'settled') AS has_settled
                    FROM pending_commissions
                    WHERE order_id = %s
                """, (order_id,))
                pc = _row(cur.fetchone())
                pc_amount_yuan = pc.get("amount_yuan") or 0
                agent_revenue_settled = bool(pc.get("has_settled"))
            except Exception:
                agent_reversal_cents = 0
            if pc_amount_yuan is not None:
                from services.payment_amounts import yuan_to_fen

                agent_reversal_cents = yuan_to_fen(pc_amount_yuan)

        # [BUG-P3] 退款窗口用 DB 本地时间(与 paid_at 同刻度),原 utcnow(naive UTC)慢 8h
        # 致 within_window/seconds_remaining 展示偏宽松,与对外"3 自然日"不一致。
        cur.execute("SELECT now()::timestamp AS now")
        now = _row(cur.fetchone()).get("now")
        deadline = paid_at + timedelta(days=3) if paid_at else None
        within_window = bool(deadline and now <= deadline)
        checks = [
            {"key": "paid", "label": "订单已支付", "status": "pass" if payment_status == "paid" else "fail"},
            {"key": "duplicate", "label": "未重复退款", "status": "pass" if not refund_status else "fail"},
            {"key": "window", "label": "未超过退款窗口", "status": "pass" if within_window else "warn"},
            {"key": "wallet", "label": "客户余额足够扣减", "status": "pass" if wallet_total >= refundable_power else "warn"},
            {"key": "agent_settled", "label": "服务商收益结算状态", "status": "warn" if agent_revenue_settled else "pass"},
            {"key": "payout_proof", "label": "需要人工打款凭证", "status": "warn"},
        ]
        risks = []
        if payment_status != "paid":
            risks.append("订单未支付，不能进入退款审核。")
        if refund_status:
            risks.append("该订单已有退款记录，请先核对历史状态。")
        if not within_window:
            risks.append("退款窗口可能已过，需要管理员人工复核。")
        if wallet_total < refundable_power:
            risks.append("客户当前余额可能不足以扣减预计可退算力。")
        if agent_revenue_settled:
            risks.append("服务商收益可能已结算，冲账后会产生追回或人工复核。")

        impact = {
            "original_amount_cents": amount_cents,
            "estimated_refund_cents": estimated_refund_cents,
            "used_power": consumed_since,
            "refundable_power": refundable_power,
            "customer_wallet_deduct_power": refundable_power,
            "agent_revenue_reversal_cents": agent_reversal_cents,
            "needs_manual_payout": True,
            "risk_tips": risks or ["订单基础信息可核验，请继续上传凭证并提交审核。"],
        }
        return {
            "found": True,
            "order": {
                "id": order.get("id"),
                "customer_user_id": order.get("user_id"),
                "customer_name": order.get("customer_name") or order.get("customer_username") or f"客户 {order.get('user_id')}",
                "customer_phone": order.get("customer_phone"),
                "agent_user_id": order.get("agent_user_id"),
                "agent_name": order.get("agent_name") or order.get("agent_username") or "未绑定服务商",
                "amount_cents": amount_cents,
                "payment_status": payment_status,
                "payment_method": order.get("payment_method") or "未记录",
                "order_type": order.get("order_type") or "充值订单",
                "paid_at": _iso(paid_at),
                "created_at": _iso(order.get("created_at")),
                "refund_status": refund_status,
            },
            "impact": impact,
            "checks": checks,
            "timeline": default_timeline("draft"),
        }
    finally:
        conn.close()


def default_timeline(status: str) -> list[dict]:
    order = [
        ("created", "创建工单"),
        ("approved", "审核通过"),
        ("executed", "系统冲账"),
        ("payout_proof", "上传打款凭证"),
        ("completed", "标记完成"),
    ]
    reached = {
        "draft": {"created"},
        "submitted": {"created"},
        "approved": {"created", "approved"},
        "payout_pending": {"created", "approved", "executed"},
        "completed": {"created", "approved", "executed", "payout_proof", "completed"},
    }.get(status, {"created"})
    return [
        {"key": key, "label": label, "status": "done" if key in reached else "pending"}
        for key, label in order
    ]


def _lock_active_refund_provider(cur, agent_user_id: Optional[int]) -> None:
    """Keep new refund responsibility atomic with provider downgrade."""
    if agent_user_id is None:
        return
    provider_user_id = int(agent_user_id)
    lock_commercial_provider_lifecycle(cur, provider_user_id)
    cur.execute(
        """SELECT u.id, u.is_active, COALESCE(w.agent_level, 0) AS agent_level
           FROM public.users u
           LEFT JOIN public.user_wallets w ON w.user_id = u.id
           WHERE u.id = %s
           FOR UPDATE OF u""",
        (provider_user_id,),
    )
    provider = _row(cur.fetchone())
    active = provider.get("is_active")
    if (
        not provider
        or active in (0, False)
        or (isinstance(active, str) and active.strip().lower() in {"0", "false", "disabled", "inactive"})
        or int(provider.get("agent_level") or 0) < 1
    ):
        raise ValueError("服务商状态已变化，退款责任需由平台重新核验后提交")


def create_refund_work_order(payload: dict, actor_user_id: int, *, status: str = "draft") -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        _lock_active_refund_provider(cur, payload.get("agent_user_id"))
        cur.execute("""
            SELECT id, status FROM refund_work_orders
            WHERE source_order_id = %s
              AND status IN ('draft', 'submitted', 'approved', 'payout_pending')
            LIMIT 1
        """, (payload["source_order_id"],))
        existing = _row(cur.fetchone())
        if existing:
            raise ValueError(f"该订单已有未完成退款工单 #{existing['id']} ({existing['status']})")

        submitted_at = "CURRENT_TIMESTAMP" if status == "submitted" else "NULL"
        cur.execute(f"""
            INSERT INTO refund_work_orders (
                source_order_id, customer_user_id, agent_user_id,
                refund_reason_category, refund_reason_detail, refund_method,
                requested_refund_cents, estimated_refund_cents, refundable_power,
                customer_requested_at, agent_confirmed_at, status, impact_snapshot,
                created_by, submitted_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, {submitted_at})
            RETURNING *
        """, (
            payload["source_order_id"], payload.get("customer_user_id"), payload.get("agent_user_id"),
            payload.get("refund_reason_category") or "other",
            payload.get("refund_reason_detail") or "",
            payload.get("refund_method") or "manual_wechat",
            int(payload.get("requested_refund_cents") or 0),
            int(payload.get("estimated_refund_cents") or 0),
            int(payload.get("refundable_power") or 0),
            payload.get("customer_requested_at"),
            payload.get("agent_confirmed_at"),
            status,
            _json(payload.get("impact_snapshot") or {}),
            actor_user_id,
        ))
        wo = _format_work_order(_row(cur.fetchone()))
        add_work_order_event(
            cur, wo["id"], "submitted" if status == "submitted" else "created",
            actor_user_id, "提交退款审核" if status == "submitted" else "保存草稿",
        )
        return wo


def list_refund_work_orders(status: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[dict]:
    params: list[Any] = []
    where = ""
    if status and status != "all":
        where = "WHERE status = %s"
        params.append(status)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT *
            FROM refund_work_orders
            {where}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        return [_format_work_order(_row(r)) for r in cur.fetchall()]


def get_refund_work_order(work_order_id: int, *, include_children: bool = True) -> Optional[dict]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM refund_work_orders WHERE id = %s", (work_order_id,))
        row = cur.fetchone()
        if not row:
            return None
        wo = _format_work_order(_row(row))
        if include_children:
            cur.execute("""
                SELECT * FROM refund_work_order_attachments
                WHERE work_order_id = %s ORDER BY uploaded_at DESC, id DESC
            """, (work_order_id,))
            wo["attachments"] = [_format_attachment(_row(r)) for r in cur.fetchall()]
            cur.execute("""
                SELECT * FROM refund_work_order_events
                WHERE work_order_id = %s ORDER BY created_at ASC, id ASC
            """, (work_order_id,))
            wo["events"] = [_format_event(_row(r)) for r in cur.fetchall()]
        return wo


def count_refund_work_order_attachments(work_order_id: int, evidence_type: Optional[str] = None) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        if evidence_type:
            cur.execute("""
                SELECT COUNT(*) AS c FROM refund_work_order_attachments
                WHERE work_order_id = %s AND evidence_type = %s
            """, (work_order_id, evidence_type))
        else:
            cur.execute("""
                SELECT COUNT(*) AS c FROM refund_work_order_attachments WHERE work_order_id = %s
            """, (work_order_id,))
        return int(_row(cur.fetchone()).get("c") or 0)


_COMPLETABLE_REFUND_STATUSES = frozenset({"pending", "approved", "processed", "pending_review", "completed"})
MANUAL_REFUND_EVIDENCE_TYPES = ("payout_proof", "channel_refund_proof")


def has_manual_refund_evidence_cur(cur, source_order_id: str, *, work_order_id: Optional[int] = None) -> bool:
    """Return whether an admin-reviewed work order carries actual-refund evidence.

    General wallet refund entry points may only consume proof attached to an approved or later
    work order. A specific work-order completion already owns and locks its state, so it may
    scope the lookup directly by ``work_order_id``.
    """
    if work_order_id is not None:
        cur.execute(
            "SELECT 1 FROM refund_work_order_attachments a "
            "JOIN refund_work_orders w ON w.id=a.work_order_id "
            "WHERE w.id=%s AND w.source_order_id=%s "
            "AND a.evidence_type = ANY(%s) LIMIT 1",
            (work_order_id, source_order_id, list(MANUAL_REFUND_EVIDENCE_TYPES)),
        )
    else:
        cur.execute(
            "SELECT 1 FROM refund_work_order_attachments a "
            "JOIN refund_work_orders w ON w.id=a.work_order_id "
            "WHERE w.source_order_id=%s "
            "AND w.status IN ('approved','payout_pending','completed') "
            "AND a.evidence_type = ANY(%s) LIMIT 1",
            (source_order_id, list(MANUAL_REFUND_EVIDENCE_TYPES)),
        )
    return bool(cur.fetchone())


def assert_external_refund_evidence_cur(cur, source_order_id: str, refund_status: Optional[str],
                                        refund_completed_at=None, *, work_order_id: Optional[int] = None,
                                        payout_proof_url: Optional[str] = None) -> dict:
    """Fail closed when an external-refund state lacks proof that cash was returned."""
    status = (refund_status or "").strip().lower()
    has_cd_evidence = refund_completed_at is not None
    has_manual_evidence = bool(payout_proof_url)
    # A verified CD timestamp is sufficient for pending_review. Avoid touching
    # work-order tables on this path (and for unrelated statuses); older schemas
    # may not have those tables yet, while the signed callback evidence is already
    # durable on recharge_orders.
    if not has_manual_evidence and (
        status == "channel_refunding" or (status == "pending_review" and not has_cd_evidence)
    ):
        has_manual_evidence = has_manual_refund_evidence_cur(
            cur, source_order_id, work_order_id=work_order_id,
        )

    if status == "channel_refunding" and not has_manual_evidence:
        raise ValueError("渠道退款仍在途中且无实际退款凭证·禁止提前反向内账")
    if status == "pending_review" and not (has_cd_evidence or has_manual_evidence):
        raise ValueError("pending_review 缺少可信 CD 回调或实际退款凭证·拒绝假完成")
    return {"has_cd_evidence": has_cd_evidence, "has_manual_evidence": has_manual_evidence}


def assert_refund_completion_allowed_cur(cur, work_order_id: int, source_order_id: str,
                                         work_order: Optional[dict] = None) -> dict:
    """Lock and validate the order before a refund work order may become completed.

    `pending_review` is not evidence by itself. It needs either the timestamp written by a
    verified channel CD callback or an admin-uploaded refund proof. `channel_refunding`
    remains blocked unless an admin supplied verifiable proof that cash was actually returned.
    The caller owns the transaction; any failure must roll back the work-order transition too.
    """
    cur.execute(
        "SELECT id, user_id, amount_cents, refund_status, refund_completed_at "
        "FROM recharge_orders WHERE id=%s FOR UPDATE",
        (source_order_id,),
    )
    order = _row(cur.fetchone())
    if not order:
        raise ValueError("充值订单不存在·拒绝完成退款工单")

    wo = work_order or {}
    status = (order.get("refund_status") or "").strip().lower()
    assert_external_refund_evidence_cur(
        cur,
        source_order_id,
        status,
        order.get("refund_completed_at"),
        work_order_id=work_order_id,
        payout_proof_url=wo.get("payout_proof_url"),
    )

    if status != "channel_refunding" and status not in _COMPLETABLE_REFUND_STATUSES:
        raise ValueError("订单不处于可完成退款状态(未发起/已失败/被拒)·拒绝虚假完成")

    order_user_id = order.get("user_id")
    work_order_user_id = wo.get("customer_user_id")
    if work_order_user_id is not None and order_user_id is not None \
            and int(work_order_user_id) != int(order_user_id):
        raise ValueError("退款工单与订单归属不一致(customer_user_id)·拒绝完成")
    requested_cents = int(wo.get("requested_refund_cents") or 0)
    order_cents = int(order.get("amount_cents") or 0)
    if requested_cents > order_cents:
        raise ValueError("退款金额大于订单实付金额·拒绝完成")
    return order


def _transition_refund_work_order_cur(cur, work_order_id: int, status: str, actor_user_id: int,
                                      *, note: str = "", payload: Optional[dict] = None,
                                      extra_set: str = "") -> dict:
    """[v11 F3] 在【调用方 cursor / 事务】内推进工单状态 + 记事件 —— 供退款完成【单事务原子提交】复用
    (工单终态与 recharge_orders 完成、canonical 冲销一并提交,消除"工单完成但订单未完成")。"""
    status_sets = {
        "submitted": "submitted_at = COALESCE(submitted_at, CURRENT_TIMESTAMP)",
        "approved": "reviewed_at = CURRENT_TIMESTAMP, reviewed_by = %s",
        "payout_pending": "executed_at = CURRENT_TIMESTAMP",
        "completed": "completed_at = CURRENT_TIMESTAMP, payout_operator_id = COALESCE(payout_operator_id, %s)",
        "rejected": "reviewed_at = CURRENT_TIMESTAMP, reviewed_by = %s",
        "cancelled": "completed_at = CURRENT_TIMESTAMP",
    }
    params: list[Any] = []
    auto_set = status_sets.get(status, "")
    if "%s" in auto_set:
        params.append(actor_user_id)
    set_parts = ["status = %s", "updated_at = CURRENT_TIMESTAMP"]
    params.insert(0, status)
    if auto_set:
        set_parts.append(auto_set)
    if extra_set:
        set_parts.append(extra_set)
    params.append(work_order_id)
    cur.execute(f"""
        UPDATE refund_work_orders
        SET {", ".join(set_parts)}
        WHERE id = %s
        RETURNING *
    """, params)
    row = cur.fetchone()
    if not row:
        raise ValueError("退款工单不存在")
    add_work_order_event(cur, work_order_id, status, actor_user_id, note, payload)
    return _format_work_order(_row(row))


def transition_refund_work_order(work_order_id: int, status: str, actor_user_id: int,
                                 *, note: str = "", payload: Optional[dict] = None,
                                 extra_set: str = "") -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        return _transition_refund_work_order_cur(
            cur, work_order_id, status, actor_user_id,
            note=note, payload=payload, extra_set=extra_set,
        )


def attach_refund_work_order_file(work_order_id: int, file_info: dict, actor_user_id: int) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM refund_work_orders WHERE id = %s", (work_order_id,))
        wo = _row(cur.fetchone())
        if not wo:
            raise ValueError("退款工单不存在")
        if wo.get("status") == "completed":
            raise ValueError("已完成工单只能追加备注，不能再上传凭证")
        cur.execute("""
            INSERT INTO refund_work_order_attachments (
                work_order_id, file_url, file_name, file_type, evidence_type,
                mime_type, file_size_bytes, uploaded_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            work_order_id, file_info["file_url"], file_info["file_name"],
            file_info["file_type"], file_info.get("evidence_type") or "other",
            file_info.get("mime_type"), int(file_info.get("file_size_bytes") or 0),
            actor_user_id,
        ))
        attachment = _format_attachment(_row(cur.fetchone()))
        add_work_order_event(
            cur, work_order_id, "attachment_uploaded", actor_user_id,
            f"上传凭证: {attachment['file_name']}",
            {"attachment_id": attachment["id"], "evidence_type": attachment["evidence_type"]},
        )
        return attachment


def save_execution_result(work_order_id: int, actor_user_id: int, result: dict, next_status: str) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        complete_set = ""
        params: list[Any] = [next_status, _json(result)]
        if next_status == "completed":
            complete_set = ", completed_at = CURRENT_TIMESTAMP, payout_operator_id = COALESCE(payout_operator_id, %s)"
            params.append(actor_user_id)
        params.append(work_order_id)
        # [C1] CAS:仅当工单仍处 'approved' 才落执行结果 · 并发双 execute 第二个匹配 0 行被拒
        # (下游 recharge_orders FOR UPDATE + 幂等已兜真钱不双退,此处再加工单层乐观锁防状态错乱)
        cur.execute(f"""
            UPDATE refund_work_orders
            SET status = %s,
                updated_at = CURRENT_TIMESTAMP,
                executed_at = CURRENT_TIMESTAMP,
                execution_result = %s::jsonb,
                last_action_error = NULL
                {complete_set}
            WHERE id = %s AND status = 'approved'
            RETURNING *
        """, params)
        row = cur.fetchone()
        if not row:
            raise ValueError("退款工单不存在或状态已变更(非 approved · 并发重复执行已拒绝)")
        if next_status == "completed":
            # [v10 item6] "仅系统冲账不打款"自动完成→completed → canonical 冲销渠道收益(同事务·幂等·仅生效态真冲)。
            #   此路径独立于 refund_work_order_api /complete(#4)· 亦是退款真完成入口 · 不得漏冲。
            _soid = row.get("source_order_id") if isinstance(row, dict) else None
            if _soid:
                order = assert_refund_completion_allowed_cur(cur, work_order_id, _soid, _row(row))
                cur.execute("""
                    UPDATE recharge_orders
                    SET refund_status = 'completed'
                    WHERE id = %s
                      AND refund_status IS NOT DISTINCT FROM %s
                    RETURNING id
                """, (_soid, order.get("refund_status")))
                _upd = cur.fetchone()
                if _upd is None:
                    raise ValueError("退款订单状态并发变化·拒绝将工单标记完成")
                from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund
                reverse_channel_revenue_on_refund(cur, _soid)
            else:
                # source_order_id 缺失(数据异常)· 无订单可完成/无收益可冲 → 记 CRITICAL,不新增阻断(保历史行为)
                logger.critical("[refund_work_order][v11 F3] 工单 %s 无 source_order_id · 无法校验订单完成态", work_order_id)
        add_work_order_event(cur, work_order_id, "executed", actor_user_id, "系统冲账已执行", result)
        if next_status == "completed":
            add_work_order_event(cur, work_order_id, "completed", actor_user_id, "仅系统冲账不打款，工单自动完成")
        return _format_work_order(_row(row))


def save_action_error(work_order_id: int, message: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE refund_work_orders
            SET last_action_error = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (message[:500], work_order_id))


def set_payout_proof_url(work_order_id: int, file_url: str, actor_user_id: int) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE refund_work_orders
            SET payout_proof_url = %s, payout_operator_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING *
        """, (file_url, actor_user_id, work_order_id))
        row = cur.fetchone()
        if not row:
            raise ValueError("退款工单不存在")
        add_work_order_event(cur, work_order_id, "payout_proof", actor_user_id, "上传打款凭证", {"file_url": file_url})
        return _format_work_order(_row(row))


def requires_payout_proof(refund_method: str) -> bool:
    return refund_method != SYSTEM_ONLY_METHOD
