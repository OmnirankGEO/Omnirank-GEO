"""Audit and optionally repair safe historical referral commercial bindings.

Default mode is read-only. Applying requires an explicit operator and only binds
an ordinary, currently unbound customer to one verified active service-provider
inviter. Conflicts, ambiguous referrals, service-provider subjects and pending
orders are always reported and skipped. Channel hierarchy is never inferred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any, Dict, List

from db.connection import get_db
from services.admin_user_governance import record_external_commercial_binding_change
from services.commercial_service_routing import (
    RelationshipConflict,
    lock_commercial_binding_subject,
    lock_commercial_provider_for_assignment,
    lock_commercial_provider_lifecycle,
    lock_pending_commercial_orders,
)
from services.customer_binding import upsert_customer_agent_binding


def _rows(cur) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT rl.referred_id AS customer_user_id,
               MIN(rl.referrer_id) AS inviter_user_id,
               MIN(rl.created_at) AS referred_at,
               COUNT(*) AS referral_count,
               cw.user_id AS wallet_user_id,
               COALESCE(cw.agent_level,0) AS customer_level,
               COALESCE(customer.is_active,1) AS customer_active,
               cab.agent_user_id AS current_provider_user_id
        FROM referral_links rl
        JOIN users customer ON customer.id=rl.referred_id
        LEFT JOIN user_wallets cw ON cw.user_id=customer.id
        LEFT JOIN customer_agent_bindings cab ON cab.customer_user_id=customer.id
        WHERE rl.level=1
        GROUP BY rl.referred_id,cw.user_id,cw.agent_level,customer.is_active,cab.agent_user_id
        ORDER BY rl.referred_id
        """
    )
    return [dict(row) for row in cur.fetchall() or []]


def audit_candidates(cur) -> List[Dict[str, Any]]:
    report: List[Dict[str, Any]] = []
    for row in _rows(cur):
        customer_id = int(row["customer_user_id"])
        inviter_id = int(row["inviter_user_id"])
        current_provider = row.get("current_provider_user_id")
        item = {
            "customer_user_id": customer_id,
            "inviter_user_id": inviter_id,
            "current_provider_user_id": (
                int(current_provider) if current_provider is not None else None
            ),
            "classification": "unknown",
        }
        if not bool(row.get("customer_active")):
            item["classification"] = "customer_inactive"
        elif row.get("wallet_user_id") is None:
            item["classification"] = "customer_identity_unavailable"
        elif int(row["referral_count"] or 0) != 1:
            item["classification"] = "ambiguous_referral"
        elif int(row.get("customer_level") or 0) >= 1:
            item["classification"] = "service_provider_requires_explicit_channel_mapping"
        elif current_provider is not None:
            item["classification"] = (
                "already_bound_to_inviter"
                if int(current_provider) == inviter_id
                else "binding_conflict"
            )
        else:
            cur.execute(
                """SELECT u.id
                   FROM users u JOIN user_wallets w ON w.user_id=u.id
                   LEFT JOIN public_account_codes pac ON pac.user_id=u.id
                   WHERE u.id=%s AND COALESCE(u.is_active,1)=1
                     AND COALESCE(w.agent_level,0)>=1
                     AND pac.service_account_code IS NOT NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                       WHERE ur.user_id=u.id AND r.name='admin'
                     )""",
                (inviter_id,),
            )
            if not cur.fetchone():
                item["classification"] = "inviter_not_eligible"
            else:
                cur.execute(
                    """SELECT COUNT(*) AS count FROM recharge_orders
                       WHERE user_id=%s AND payment_status='pending'
                         AND (order_type IS NULL OR order_type='customer_recharge')""",
                    (customer_id,),
                )
                pending = int(cur.fetchone()["count"])
                item["classification"] = (
                    "pending_order_blocked" if pending else "safe_ordinary_unbound"
                )
                item["pending_order_count"] = pending
        report.append(item)
    return report


def apply_safe_candidates(
    cur, report: List[Dict[str, Any]], *, operator_user_id: int,
    reason: str, request_prefix: str,
) -> List[Dict[str, Any]]:
    cur.execute(
        """SELECT u.id
           FROM users u
           JOIN user_roles ur ON ur.user_id=u.id
           JOIN roles r ON r.id=ur.role_id AND r.name='admin'
           WHERE u.id=%s AND COALESCE(u.is_active,1)=1
           FOR SHARE OF u,ur,r""",
        (int(operator_user_id),),
    )
    if not cur.fetchone():
        raise PermissionError("historical commercial repair requires an active administrator")

    applied: List[Dict[str, Any]] = []
    for item in report:
        if item["classification"] != "safe_ordinary_unbound":
            continue
        customer_id = int(item["customer_user_id"])
        inviter_id = int(item["inviter_user_id"])
        lock_commercial_provider_lifecycle(cur, inviter_id)
        try:
            lock_commercial_provider_for_assignment(cur, inviter_id)
        except RelationshipConflict:
            item["classification"] = "inviter_became_ineligible"
            continue
        if lock_pending_commercial_orders(cur, customer_id):
            item["classification"] = "pending_order_blocked"
            continue
        # The initial report is advisory only. Recheck the current projection
        # after acquiring the same absent-row lock as every canonical writer.
        # A historical repair must never turn a concurrent valid binding into a
        # dispute merely because its read-only audit was briefly stale.
        lock_commercial_binding_subject(cur, customer_id)
        cur.execute(
            """SELECT u.is_active, w.user_id AS wallet_user_id,
                      COALESCE(w.agent_level,0) AS customer_level
               FROM users u
               LEFT JOIN user_wallets w ON w.user_id=u.id
               WHERE u.id=%s
               FOR UPDATE OF u""",
            (customer_id,),
        )
        current_subject = cur.fetchone()
        if not current_subject or not bool(current_subject.get("is_active")):
            item["classification"] = "customer_inactive"
            continue
        if current_subject.get("wallet_user_id") is None:
            item["classification"] = "customer_identity_unavailable"
            continue
        if int(current_subject.get("customer_level") or 0) >= 1:
            item["classification"] = "service_provider_requires_explicit_channel_mapping"
            continue
        # Repeat after the absent-row fence. A quote-less legacy writer may
        # have committed a pending order while this repair waited for the
        # customer lock; historical correction must not alter its service
        # principal.
        if lock_pending_commercial_orders(cur, customer_id):
            item["classification"] = "pending_order_blocked"
            continue
        cur.execute(
            "SELECT agent_user_id FROM customer_agent_bindings "
            "WHERE customer_user_id=%s FOR UPDATE",
            (customer_id,),
        )
        existing = cur.fetchone()
        if existing:
            existing_provider = int(existing["agent_user_id"])
            item["classification"] = (
                "already_bound_to_inviter"
                if existing_provider == inviter_id
                else "binding_conflict"
            )
            continue
        immutable_identity = f"referral-links:v1:{inviter_id}:{customer_id}"
        source_token = "sha256:" + hashlib.sha256(
            immutable_identity.encode("utf-8")
        ).hexdigest()
        result = upsert_customer_agent_binding(
            cur,
            customer_id,
            inviter_id,
            "historical_referral_backfill",
            source_token,
        )
        if result["action"] != "inserted":
            item["classification"] = f"concurrent_{result['action']}"
            continue
        cur.execute(
            """SELECT id FROM customer_agent_bindings
               WHERE customer_user_id=%s AND agent_user_id=%s""",
            (customer_id, inviter_id),
        )
        binding_id = int(cur.fetchone()["id"])
        request_id = f"{request_prefix}-{customer_id}"
        version = record_external_commercial_binding_change(
            cur,
            subject_user_id=customer_id,
            operator_user_id=int(operator_user_id),
            operator_username=None,
            request_id=request_id,
            reason=reason,
            before={
                "commercial_provider_user_id": None,
                "commercial_mode": "platform_direct",
            },
            after={
                "commercial_provider_user_id": inviter_id,
                "commercial_mode": "service_provider",
            },
            ip_address="maintenance-script",
            evidence={
                "binding_id": binding_id,
                "source": "verified_referral_links",
                "historical_orders_untouched": True,
                "channel_relationship_not_inferred": True,
            },
        )
        applied.append({
            "customer_user_id": customer_id,
            "provider_user_id": inviter_id,
            "binding_id": binding_id,
            "governance_version": version,
            "request_id": request_id,
        })
    return applied


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-ordinary-safe", action="store_true")
    parser.add_argument("--operator-user-id", type=int)
    parser.add_argument("--reason", default="")
    parser.add_argument("--request-prefix", default="referral-backfill-20260719")
    args = parser.parse_args()
    if args.apply_ordinary_safe and (
        not args.operator_user_id or len(args.reason.strip()) < 2
    ):
        parser.error("apply requires --operator-user-id and a meaningful --reason")

    with get_db() as conn:
        cur = conn.cursor()
        report = audit_candidates(cur)
        applied = []
        if args.apply_ordinary_safe:
            applied = apply_safe_candidates(
                cur,
                report,
                operator_user_id=args.operator_user_id,
                reason=args.reason.strip(),
                request_prefix=args.request_prefix.strip(),
            )
        else:
            # Keep default execution observably read-only even if get_db commits.
            conn.rollback()
        print(json.dumps({"candidates": report, "applied": applied}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
