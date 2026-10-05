"""Read-only per-order audit for pending retail orders missing commercial_resolution.

This script never guesses or writes a relationship/snapshot.  Deploy runs it
against a read-only production connection and attaches the JSON output to the
four manual disposition tickets before any pricing/resale flag can be enabled.
"""

from __future__ import annotations

import json
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor


def _active(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "disabled", "inactive"}
    return bool(value)


def main() -> int:
    database_url = (os.getenv("AUDIT_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        raise RuntimeError("AUDIT_DATABASE_URL/DATABASE_URL 未配置；脚本不会选择默认数据库")
    configured_platform = (os.getenv("PLATFORM_DIRECT_SERVICE_USER_ID") or "").strip()
    platform_user_id = int(configured_platform) if configured_platform.isdigit() else None
    if platform_user_id is not None and platform_user_id <= 0:
        platform_user_id = None
    conn = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    try:
        conn.set_session(readonly=True, autocommit=False)
        cur = conn.cursor()
        cur.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM information_schema.columns
                   WHERE table_schema=current_schema() AND table_name='recharge_orders'
                     AND column_name='actual_payment_channel'
               ) AS present"""
        )
        actual_channel_projection = (
            "o.actual_payment_channel" if cur.fetchone()["present"]
            else "NULL::text AS actual_payment_channel"
        )
        cur.execute(
            f"""SELECT o.id AS order_id,o.user_id,o.agent_user_id,o.amount_cents,
                      o.base_points,o.bonus_points,o.created_at,o.price_quote_id,
                      o.pricing_catalog_version,{actual_channel_projection},
                      o.pricing_snapshot_jsonb,
                      b.id AS current_binding_id,b.agent_user_id AS current_bound_service_user_id,
                      b.binding_source,b.bound_at,b.dispute_status
               FROM recharge_orders o
               LEFT JOIN customer_agent_bindings b ON b.customer_user_id=o.user_id
               WHERE o.payment_status='pending'
                 AND o.price_quote_id IS NOT NULL
                 AND COALESCE(o.pricing_snapshot_jsonb->>'quote_type','retail')='retail'
                 AND COALESCE(o.pricing_snapshot_jsonb->>'commercial_resolution','')=''
               ORDER BY o.created_at,o.id"""
        )
        rows = [dict(row) for row in cur.fetchall()]
        service_ids = {
            int(value)
            for row in rows
            for value in (row.get("agent_user_id"), row.get("current_bound_service_user_id"), platform_user_id)
            if value is not None and str(value).isdigit()
        }
        services = {}
        if service_ids:
            cur.execute(
                """SELECT u.id,u.is_active,COALESCE(w.agent_level,0) AS agent_level,
                          p.service_account_code
                   FROM users u
                   LEFT JOIN user_wallets w ON w.user_id=u.id
                   LEFT JOIN public_account_codes p ON p.user_id=u.id
                   WHERE u.id=ANY(%s)""",
                (sorted(service_ids),),
            )
            services = {int(row["id"]): dict(row) for row in cur.fetchall()}
        output = []
        for row in rows:
            snapshot = row.get("pricing_snapshot_jsonb") or {}
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            order_service = int(row["agent_user_id"]) if row.get("agent_user_id") is not None else None
            bound_service = (
                int(row["current_bound_service_user_id"])
                if row.get("current_bound_service_user_id") is not None else None
            )
            service = services.get(order_service or -1, {})
            service_ready_now = bool(
                service
                and _active(service.get("is_active"))
                and int(service.get("agent_level") or 0) >= 1
                and service.get("service_account_code")
            )
            if bound_service is not None and bound_service == order_service:
                current_fact = "CURRENT_BOUND_MATCHES_ORDER"
            elif bound_service is not None:
                current_fact = "CURRENT_BINDING_CONFLICTS_WITH_ORDER"
            elif platform_user_id is not None and platform_user_id == order_service:
                current_fact = "CURRENT_PLATFORM_CONFIG_MATCHES_ORDER"
            elif platform_user_id is None:
                current_fact = "PLATFORM_DIRECT_CONFIG_MISSING"
            else:
                current_fact = "CURRENT_NO_BINDING_AND_PLATFORM_CONFIG_CONFLICTS_WITH_ORDER"
            output.append(
                {
                    "order_id": str(row["order_id"]),
                    "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
                    "customer_user_id": int(row["user_id"]),
                    "order_service_user_id": order_service,
                    "amount_cents": int(row.get("amount_cents") or 0),
                    "points": int(row.get("base_points") or 0),
                    "bonus_points": int(row.get("bonus_points") or 0),
                    "price_quote_id": row.get("price_quote_id"),
                    "pricing_catalog_version": row.get("pricing_catalog_version"),
                    "actual_payment_channel": row.get("actual_payment_channel"),
                    "snapshot_quote_type": snapshot.get("quote_type"),
                    "snapshot_source_agent_user_id": (
                        (snapshot.get("source_ref") or {}).get("agent_user_id")
                        if isinstance(snapshot.get("source_ref"), dict)
                        else None
                    ),
                    "current_binding_id": row.get("current_binding_id"),
                    "current_bound_service_user_id": bound_service,
                    "current_binding_source": row.get("binding_source"),
                    "current_binding_bound_at": (
                        row["bound_at"].isoformat() if row.get("bound_at") else None
                    ),
                    "current_binding_dispute_status": row.get("dispute_status"),
                    "configured_platform_direct_service_user_id": platform_user_id,
                    "order_service_ready_now": service_ready_now,
                    "current_fact_only_not_backfill_authority": current_fact,
                    "disposition": "HOLD_NO_MUTATION_MANUAL_EVIDENCE_REQUIRED",
                    "required_evidence": [
                        "订单创建时的报价 source_ref 与 catalog version",
                        "订单创建时客户商业关系审计历史",
                        "订单创建时 blue/green PLATFORM_DIRECT_SERVICE_USER_ID 与 config epoch",
                        "支付是否已在外部发生但回调尚未完成",
                    ],
                    "allowed_actions_after_evidence": [
                        "证据一致时由 Deploy 为该订单单独提交处置方案",
                        "证据冲突时取消支付入口并转 dispute_hold/人工资金工单",
                    ],
                    "forbidden_actions": [
                        "按当前绑定批量补 commercial_resolution",
                        "按当前环境变量批量补 PLATFORM_DIRECT",
                        "创建或改写客户商业关系",
                    ],
                }
            )
        print(json.dumps({"count": len(output), "orders": output}, ensure_ascii=False, indent=2, default=str))
        conn.rollback()
        return 2 if output else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
