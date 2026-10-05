"""Lease-based organization work claiming and conservative crash recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets
import json
from typing import Any, Optional

from db.connection import get_db
from db.wallet_db import get_feature_pricing
from services.organization_billing import (
    mark_external_side_effect_started,
    quarantine_charge,
    release_charge,
    settle_charge,
)
from services.organization_contract import IdentityContext, OrganizationError, payload_hash
from services.organization_membership_lifecycle import restore_legacy_access_or_retire_operator


def claim_work(
    *,
    worker_id: str,
    lease_seconds: int = 120,
    automatic_system_only: bool = False,
) -> Optional[dict[str, Any]]:
    token = f"{worker_id}:{secrets.token_urlsafe(18)}"
    lease_until = datetime.now(timezone.utc) + timedelta(seconds=max(30, int(lease_seconds)))
    with get_db() as conn:
        cursor = conn.cursor()
        automatic_filter = (
            "AND c.actor_kind='system' AND c.automatic_plan_occurrence_id IS NOT NULL"
            if automatic_system_only
            else ""
        )
        cursor.execute(
            f"""
            SELECT o.*,c.actor_kind,c.actor_user_id,c.membership_id,c.payer_user_id,
                   c.organization_id,c.status AS charge_status,c.reserved_ceiling_points,
                   c.automatic_plan_occurrence_id,c.brand_id
            FROM organization_work_outbox o
            JOIN organization_charge_links c ON c.id=o.charge_link_id
            WHERE c.status='reserved' AND o.dispatch_deadline>NOW() AND (
              (o.status='pending' AND o.next_attempt_at<=NOW()) OR
              (o.status='claimed' AND o.lease_until<NOW()
                AND o.execution_started_at IS NULL
                AND o.external_side_effect_started_at IS NULL)
            )
            {automatic_filter}
            ORDER BY o.next_attempt_at,o.id
            FOR UPDATE OF c,o SKIP LOCKED LIMIT 1
            """
        )
        row = cursor.fetchone()
        if not row:
            return None
        cursor.execute(
            """
            UPDATE organization_work_outbox
            SET status='claimed',claim_token=%s,lease_until=%s,claimed_at=NOW(),
                attempts=attempts+1,updated_at=NOW()
            WHERE id=%s RETURNING *
            """,
            (token, lease_until, row["id"]),
        )
        claimed = dict(cursor.fetchone())
        claimed.update(
            {
                "actor_kind": row["actor_kind"],
                "actor_user_id": row["actor_user_id"],
                "membership_id": row["membership_id"],
                "payer_user_id": row["payer_user_id"],
                "organization_id": row["organization_id"],
                "reserved_ceiling_points": row["reserved_ceiling_points"],
                "automatic_plan_occurrence_id": row["automatic_plan_occurrence_id"],
                "brand_id": row["brand_id"],
            }
        )
        return claimed


def start_work(*, outbox_id: int, claim_token: str) -> dict[str, Any]:
    with get_db() as conn:
        cursor = conn.cursor()
        # Fixed cross-service lock order: charge -> outbox.  Billing settle,
        # release, quarantine and recovery all use the same order.
        cursor.execute(
            """
            SELECT charge_link_id FROM organization_work_outbox WHERE id=%s
            """,
            (outbox_id,),
        )
        anchor = cursor.fetchone()
        if not anchor:
            raise OrganizationError("ORG_WORK_NOT_FOUND", "任务不存在", http_status=404)
        cursor.execute(
            """
            SELECT id,actor_kind,actor_user_id,membership_id,status AS charge_status
            FROM organization_charge_links WHERE id=%s FOR UPDATE
            """,
            (anchor["charge_link_id"],),
        )
        charge = cursor.fetchone()
        cursor.execute(
            "SELECT * FROM organization_work_outbox WHERE id=%s AND charge_link_id=%s FOR UPDATE",
            (outbox_id, anchor["charge_link_id"]),
        )
        outbox = cursor.fetchone()
        if not charge or not outbox:
            raise OrganizationError("ORG_WORK_NOT_FOUND", "任务不存在", http_status=404)
        row = dict(outbox)
        row.update(
            actor_kind=charge["actor_kind"],
            actor_user_id=charge["actor_user_id"],
            membership_id=charge["membership_id"],
            charge_status=charge["charge_status"],
        )
        if row["claim_token"] != claim_token or row["status"] != "claimed" or row["lease_until"] <= datetime.now(timezone.utc):
            raise OrganizationError("ORG_WORK_LEASE_LOST", "任务已超时，请重新发起", http_status=409)
        if row["charge_status"] != "reserved":
            raise OrganizationError("ORG_CHARGE_NOT_RESERVED", "任务的算力冻结已失效，请重新发起", http_status=409)
        # Actor identity is immutable. A member-originated job stays member even
        # though a system worker performs the computation.
        if row["actor_kind"] == "member" and (row["actor_user_id"] is None or row["membership_id"] is None):
            raise OrganizationError("ORG_WORK_ACTOR_CORRUPT", "员工任务身份证据损坏", http_status=503)
        if row["actor_kind"] == "system" and (row["actor_user_id"] is not None or row["membership_id"] is not None):
            raise OrganizationError("ORG_WORK_ACTOR_CORRUPT", "系统任务错误绑定员工身份", http_status=503)
        # Moving into the local worker is not yet the external-effect boundary.
        # A crash after this statement but before the provider call remains
        # provably unstarted and may be released by recovery.
        cursor.execute(
            """
            UPDATE organization_work_outbox
            SET status='running',updated_at=NOW()
            WHERE id=%s RETURNING *
            """,
            (outbox_id,),
        )
        updated = dict(cursor.fetchone())
        cursor.execute(
            """
            UPDATE organization_charge_links
            SET attempt_token=%s,lease_until=%s,updated_at=NOW()
            WHERE id=%s
            """,
            (claim_token, row["lease_until"], row["charge_link_id"]),
        )
        cursor.execute(
            """
            UPDATE organization_plan_occurrences
            SET status='running',updated_at=NOW()
            WHERE id=(
              SELECT automatic_plan_occurrence_id
              FROM organization_charge_links WHERE id=%s
            ) AND status='reserved'
            """,
            (row["charge_link_id"],),
        )
        return updated


def _prepare_automatic_monitoring(payload: dict[str, Any], brand_id: int) -> tuple[Any, int]:
    """Resolve the live scope and bounded actual price before any provider call."""
    from api.monitoring_api import MonitoringRunRequest, _resolve_brand_and_quotes
    from db.monitoring_db import brand_has_confirmed_or_paid_quote, get_keywords_for_monitoring

    requested_brand_id = payload.get("brand_id")
    if requested_brand_id is not None and int(requested_brand_id) != int(brand_id):
        raise OrganizationError("ORG_PLAN_PAYLOAD_BRAND_MISMATCH", "计划的客户信息已变化，本次执行已跳过", http_status=409)
    request = MonitoringRunRequest(
        brand_id=int(brand_id),
        keyword_keys=list(payload.get("keyword_keys") or []) or None,
        platforms=list(payload.get("platforms") or []) or None,
        concurrency=payload.get("concurrency"),
        search_mode=str(payload.get("search_mode") or "enhanced"),
    )
    resolved_brand_id, quote_ids = _resolve_brand_and_quotes(request)
    if int(resolved_brand_id or 0) != int(brand_id):
        raise OrganizationError("ORG_PLAN_BRAND_SCOPE_INVALID", "计划的客户已不在范围内，本次执行已跳过", http_status=409)
    if quote_ids:
        keywords = get_keywords_for_monitoring(
            quote_ids=quote_ids,
            keyword_keys=request.keyword_keys,
        )
    elif brand_has_confirmed_or_paid_quote(brand_id):
        keywords = []
    else:
        keywords = get_keywords_for_monitoring(
            brand_id=brand_id,
            keyword_keys=request.keyword_keys,
        )
    unique_keys = {
        (str(row.get("keyword") or ""), str(row.get("source") or ""))
        for row in keywords
        if str(row.get("keyword") or "").strip()
    }
    if not unique_keys:
        raise OrganizationError("ORG_PLAN_NO_MONITORING_KEYWORDS", "计划当前没有可监测的监测词", http_status=409)
    pricing = get_feature_pricing("monitor_single")
    if not pricing or int(pricing.get("cost_points") or 0) <= 0:
        raise OrganizationError("ORG_PRICING_MISSING", "系统价格配置异常，请联系平台客服", http_status=503)
    return request, len(unique_keys) * int(pricing["cost_points"])


async def dispatch_one_automatic_work(
    *,
    worker_id: str = "organization-automatic-worker",
) -> Optional[dict[str, Any]]:
    """Execute one durable owner-approved occurrence through its live GEO handler."""
    claimed = claim_work(
        worker_id=worker_id,
        lease_seconds=7200,
        automatic_system_only=True,
    )
    if not claimed:
        return None
    charge_link_id = int(claimed["charge_link_id"])
    claim_token = str(claimed["claim_token"])
    start_work(outbox_id=int(claimed["id"]), claim_token=claim_token)
    external_started = False
    try:
        payload_snapshot = claimed.get("payload_snapshot") or {}
        if isinstance(payload_snapshot, str):
            payload_snapshot = json.loads(payload_snapshot)
        if not isinstance(payload_snapshot, dict) or claimed.get("payload_hash") != payload_hash(payload_snapshot):
            raise OrganizationError("ORG_CHARGE_AUTHORITY_EVIDENCE_CORRUPT", "任务权限代际证据损坏", http_status=503)
        if int(payload_snapshot.get("schema_version") or 0) == 2:
            payload = payload_snapshot.get("request_payload")
            if not isinstance(payload, dict) or not isinstance(payload_snapshot.get("authority_generation"), dict):
                raise OrganizationError("ORG_CHARGE_AUTHORITY_EVIDENCE_CORRUPT", "任务权限代际证据损坏", http_status=503)
        else:
            # Read compatibility for reservations created before the authority
            # generation envelope was introduced.
            payload = payload_snapshot
        if claimed["work_kind"] != "monitoring.run":
            raise OrganizationError(
                "ORG_PLAN_WORK_UNSUPPORTED",
                "该功能暂不支持自动执行，请联系平台",
                http_status=503,
            )
        brand_id = int(claimed.get("brand_id") or payload.get("brand_id") or 0)
        if brand_id <= 0:
            raise OrganizationError("ORG_PLAN_BRAND_REQUIRED", "自动监测计划缺少客户", http_status=422)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT o.owner_user_id,o.status,o.version,o.authority_version,
                       p.approved_by_user_id,p.status AS plan_status,p.ends_at
                FROM organizations o
                JOIN organization_plan_occurrences x ON x.id=%s
                JOIN organization_automatic_plans p ON p.id=x.plan_id
                WHERE o.id=%s
                """,
                (claimed["automatic_plan_occurrence_id"], claimed["organization_id"]),
            )
            authority = cursor.fetchone()
        if (
            not authority
            or authority["status"] != "active"
            or authority["plan_status"] != "active"
            or authority["ends_at"] <= datetime.now(timezone.utc)
        ):
            raise OrganizationError("ORG_PLAN_INACTIVE", "自动计划已暂停、取消或失效", http_status=409)
        identity = IdentityContext(
            request_id=str(claimed["execution_id"]),
            authenticated_user_id=int(authority["owner_user_id"]),
            principal_user_id=int(authority["owner_user_id"]),
            payer_user_id=int(authority["owner_user_id"]),
            actor_kind="system",
            organization_id=int(claimed["organization_id"]),
            organization_status=str(authority["status"]),
            organization_version=int(authority["version"]),
            organization_authority_version=int(authority["authority_version"]),
            requested_by_user_id=int(authority["approved_by_user_id"]),
        )
        request, actual_points = _prepare_automatic_monitoring(dict(payload), brand_id)
        if actual_points > int(claimed["reserved_ceiling_points"]):
            raise OrganizationError(
                "ORG_ACTUAL_EXCEEDS_CEILING",
                "本次监测范围超出计划的单次预算，本次执行已跳过",
                http_status=409,
            )
        mark_external_side_effect_started(
            charge_link_id=charge_link_id,
            claim_token=claim_token,
        )
        external_started = True
        from api.monitoring_api import api_run_monitoring

        result = await api_run_monitoring(request, organization_identity=identity)
        if not isinstance(result, dict) or result.get("status") != "success" or not result.get("task_id"):
            raise OrganizationError("ORG_PLAN_WORK_FAILED", "本次自动监测未生成结果", http_status=503)
        settled = await settle_charge(
            charge_link_id=charge_link_id,
            actual_points=actual_points,
            result_payload={
                "task_id": int(result["task_id"]),
                "brand_id": brand_id,
                "automatic_plan_occurrence_id": int(claimed["automatic_plan_occurrence_id"]),
            },
            artifacts=(
                {
                    "artifact_type": "monitoring_task",
                    "artifact_id": int(result["task_id"]),
                    "brand_id": brand_id,
                    "visibility": "private",
                },
            ),
            claim_token=claim_token,
        )
        return {"status": "succeeded", "charge": settled, "result": result}
    except Exception as exc:
        try:
            if external_started:
                await quarantine_charge(
                    charge_link_id=charge_link_id,
                    reason="automatic external outcome unknown: " + type(exc).__name__,
                )
            else:
                await release_charge(
                    charge_link_id=charge_link_id,
                    reason="automatic work rejected before provider call: " + type(exc).__name__,
                )
        except OrganizationError as release_error:
            if release_error.code not in {
                "ORG_CHARGE_NOT_RELEASABLE",
                "ORG_CHARGE_NOT_QUARANTINABLE",
            }:
                raise
        return {
            "status": "unknown" if external_started else "retry_wait",
            "charge_link_id": charge_link_id,
            "error_code": getattr(exc, "code", type(exc).__name__),
        }


def heartbeat(*, outbox_id: int, claim_token: str, lease_seconds: int = 120) -> datetime:
    lease_until = datetime.now(timezone.utc) + timedelta(seconds=max(30, int(lease_seconds)))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE organization_work_outbox SET lease_until=GREATEST(lease_until,%s),updated_at=NOW()
            WHERE id=%s AND claim_token=%s AND status IN ('claimed','running') AND lease_until>NOW()
            """,
            (lease_until, outbox_id, claim_token),
        )
        if cursor.rowcount != 1:
            raise OrganizationError("ORG_WORK_LEASE_LOST", "任务已超时，请重新发起", http_status=409)
        # GREATEST 不缩租（2026-07-23 统一 R3 §六）：延迟心跳不得缩短既有租约。
        cursor.execute("UPDATE organization_charge_links SET lease_until=GREATEST(lease_until,%s),updated_at=NOW() WHERE id=(SELECT charge_link_id FROM organization_work_outbox WHERE id=%s)", (lease_until, outbox_id))
        return lease_until


async def recover_stale_work(*, limit: int = 100) -> dict[str, Any]:
    """Release provably unstarted work; quarantine every ambiguous window."""
    release_ids: list[int] = []
    unknown_ids: list[int] = []
    corrupted_ids: list[int] = []
    with get_db() as conn:
        cursor = conn.cursor()
        bounded_limit = max(1, min(int(limit), 500))
        # PostgreSQL forbids FOR UPDATE on the nullable side of a LEFT JOIN.
        # Lock complete c+outbox pairs with an inner join, then quarantine
        # missing-outbox corruption in a second charge-only lock pass.
        cursor.execute(
            """
            SELECT c.id AS charge_link_id,c.physical_freeze_id,c.external_side_effect_started_at AS charge_external,
                   o.id AS outbox_id,o.status,o.dispatch_deadline,o.lease_until,o.execution_started_at,
                   o.external_side_effect_started_at
            FROM organization_charge_links c JOIN organization_work_outbox o ON o.charge_link_id=c.id
            WHERE c.status='reserved' AND (
              o.dispatch_deadline<NOW() OR
              (o.status IN ('claimed','running') AND o.lease_until<NOW())
            )
            ORDER BY c.id FOR UPDATE OF c,o SKIP LOCKED LIMIT %s
            """,
            (bounded_limit,),
        )
        rows = list(cursor.fetchall())
        remaining = max(0, bounded_limit - len(rows))
        if remaining:
            cursor.execute(
                """
                SELECT c.id AS charge_link_id,c.physical_freeze_id,
                       c.external_side_effect_started_at AS charge_external,
                       NULL::BIGINT AS outbox_id,NULL::TEXT AS status,
                       NULL::TIMESTAMPTZ AS dispatch_deadline,NULL::TIMESTAMPTZ AS lease_until,
                       NULL::TIMESTAMPTZ AS execution_started_at,
                       NULL::TIMESTAMPTZ AS external_side_effect_started_at
                FROM organization_charge_links c
                WHERE c.status='reserved' AND NOT EXISTS (
                  SELECT 1 FROM organization_work_outbox o WHERE o.charge_link_id=c.id
                )
                ORDER BY c.id FOR UPDATE OF c SKIP LOCKED LIMIT %s
                """,
                (remaining,),
            )
            rows.extend(cursor.fetchall())
        for row in rows:
            charge_id = int(row["charge_link_id"])
            if row["outbox_id"] is None or not row["physical_freeze_id"]:
                cursor.execute("UPDATE organization_charge_links SET status='unknown',updated_at=NOW() WHERE id=%s", (charge_id,))
                corrupted_ids.append(charge_id)
                continue
            if row["charge_external"] is not None or row["external_side_effect_started_at"] is not None or row["execution_started_at"] is not None:
                unknown_ids.append(charge_id)
                continue
            release_ids.append(charge_id)
    failed: list[dict[str, Any]] = []
    for charge_id in unknown_ids:
        try:
            await quarantine_charge(
                charge_link_id=charge_id,
                reason="RECOVERY_AMBIGUOUS_AFTER_START",
            )
        except Exception as exc:
            corrupted_ids.append(charge_id)
            failed.append({"charge_link_id": charge_id, "error_type": type(exc).__name__})
    released: list[int] = []
    for charge_id in release_ids:
        try:
            await release_charge(charge_link_id=charge_id, reason="dispatch deadline expired before execution")
            released.append(charge_id)
        except Exception as exc:
            failed.append({"charge_link_id": charge_id, "error_type": type(exc).__name__})
    finalized_memberships: list[int] = []
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT m.id,m.organization_id FROM organization_memberships m
            WHERE m.status='leaving' AND NOT EXISTS (
              SELECT 1 FROM organization_charge_links c
              WHERE c.membership_id=m.id AND c.status IN ('reserved','refund_pending','unknown')
            )
            ORDER BY m.id FOR UPDATE OF m SKIP LOCKED LIMIT %s
            """,
            (max(1, min(int(limit), 500)),),
        )
        rows = list(cursor.fetchall())
        for row in rows:
            cursor.execute(
                """
                UPDATE organization_memberships
                SET status='left',left_at=NOW(),version=version+1,
                    capability_version=capability_version+1,assignment_version=assignment_version+1,
                    updated_at=NOW() WHERE id=%s
                """,
                (row["id"],),
            )
            lifecycle = restore_legacy_access_or_retire_operator(
                cursor,
                membership_id=int(row["id"]),
                reason="所有在途资金记录已收敛",
            )
            cursor.execute(
                "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
                (row["organization_id"],),
            )
            cursor.execute(
                """
                INSERT INTO organization_audit_events(
                  organization_id,actor_kind,action,entity_type,entity_id,reason
                ) VALUES (%s,'system','member.leave.finalize','organization_membership',%s,%s)
                """,
                (row["organization_id"], str(row["id"]), f"所有在途资金记录已收敛；legacy_access={lifecycle['disposition']}"),
            )
            finalized_memberships.append(int(row["id"]))
    return {
        "released": released,
        "unknown": unknown_ids,
        "corrupted": corrupted_ids,
        "failed": failed,
        "finalized_memberships": finalized_memberships,
    }
