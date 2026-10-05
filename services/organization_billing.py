"""Atomic owner-wallet + employee-limit billing and durable recovery anchor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import re
import secrets
from typing import Any, Callable, Mapping, Optional, Sequence

from fastapi import HTTPException

from db.connection import get_db
from db.wallet_db import get_feature_pricing
from middleware.billing import (
    commit_reserved_points_with_cursor,
    lock_legacy_wallet_with_cursor,
    refund_committed_points_with_cursor,
    release_reserved_points_with_cursor,
    reserve_points_with_cursor,
)
from services.organization_approvals import (
    approval_required,
    finish_approval_execution,
    lock_approval_for_execution,
)
from services.organization_contract import (
    BILLABLE_FEATURE_CAPABILITIES,
    BillingActorContext,
    EXTERNAL_ACTIONS,
    IdentityContext,
    OrganizationError,
    canonical_json,
    payload_hash,
    require_feature_flag,
    stable_key,
)
from services.organization_limits import lock_applicable_limits, lock_applicable_limits_split
from services.organization_payer_policy import _require_platform_admin, enforce_overage_caps, member_payer_consent
from services.organization_service import _audit, _lock_identity


FEATURE_CAPABILITIES = BILLABLE_FEATURE_CAPABILITIES

logger = logging.getLogger("GEO-OrganizationBilling")


def _translate_http_error(exc: HTTPException) -> OrganizationError:
    detail = exc.detail if isinstance(exc.detail, Mapping) else {"message": str(exc.detail)}
    return OrganizationError(
        str(detail.get("code") or "ORG_WALLET_REJECTED"),
        str(detail.get("message") or "老板钱包算力不足或不可用，本次操作未执行"),
        http_status=exc.status_code,
        safe_details={key: value for key, value in detail.items() if key not in {"code", "message"}},
    )


def _pricing_snapshot(pricing: Mapping[str, Any], ceiling: int) -> tuple[str, str]:
    version = f"feature_pricing:{pricing.get('updated_at') or 'unknown'}"
    digest = payload_hash(
        {
            "feature_code": pricing["feature_code"],
            "cost_points": int(pricing["cost_points"]),
            "requires_paid_points": bool(pricing.get("requires_paid_points")),
            "reserved_ceiling_points": int(ceiling),
            "updated_at": pricing.get("updated_at"),
        }
    )
    return version, digest


def _require_brand_assignment(cursor, identity: IdentityContext, brand_id: Optional[int]) -> None:
    if brand_id is None or identity.is_owner or identity.is_system:
        return
    cursor.execute(
        """
        SELECT 1 FROM organization_brand_assignments a
        JOIN brands b ON b.id=a.brand_id
        WHERE a.organization_id=%s AND a.membership_id=%s AND a.brand_id=%s
          AND a.status='active' AND b.owner_user_id=%s
          AND (b.is_deleted IS NULL OR b.is_deleted=FALSE)
        """,
        (identity.organization_id, identity.membership_id, brand_id, identity.principal_user_id),
    )
    if not cursor.fetchone():
        raise OrganizationError("ORG_BRAND_NOT_ASSIGNED", "该客户未分配给当前员工", http_status=403)


def _require_live_brand_scope(cursor, identity: IdentityContext, brand_id: Optional[int]) -> None:
    """Lock and validate the current principal/assignment brand boundary.

    Unlike the reservation-time helper, this is also mandatory for owner and
    system work.  A deleted or transferred brand must never reach a provider
    merely because the charge link was created while the brand was live.
    """
    if brand_id is None:
        return
    cursor.execute(
        """
        SELECT id,owner_user_id,COALESCE(is_deleted,FALSE) AS is_deleted
        FROM brands WHERE id=%s FOR UPDATE
        """,
        (int(brand_id),),
    )
    brand = cursor.fetchone()
    if (
        not brand
        or bool(brand["is_deleted"])
        or int(brand["owner_user_id"]) != int(identity.principal_user_id)
    ):
        raise OrganizationError(
            "ORG_BRAND_INACTIVE",
            "该客户已删除、已转移或不再属于当前老板",
            http_status=409,
        )
    if identity.is_member:
        cursor.execute(
            """
            SELECT id FROM organization_brand_assignments
            WHERE organization_id=%s AND membership_id=%s AND brand_id=%s
              AND status='active'
            FOR UPDATE
            """,
            (identity.organization_id, identity.membership_id, int(brand_id)),
        )
        if not cursor.fetchone():
            raise OrganizationError(
                "ORG_BRAND_NOT_ASSIGNED",
                "该客户已不再分配给当前员工",
                http_status=403,
            )


def _authority_generation_evidence(identity: IdentityContext) -> dict[str, int | None]:
    return {
        "organization_version": identity.organization_version,
        "organization_authority_version": identity.organization_authority_version,
        "membership_version": identity.membership_version,
        "capability_version": identity.capability_version,
        "assignment_version": identity.assignment_version,
    }


def _work_snapshot(payload: Mapping[str, Any], identity: IdentityContext) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "request_payload": dict(payload),
        "authority_generation": _authority_generation_evidence(identity),
    }


def _read_work_snapshot(cursor, charge_link_id: int, *, for_update: bool = False) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT payload_hash,payload_snapshot FROM organization_work_outbox
        WHERE charge_link_id=%s
        """ + (" FOR UPDATE" if for_update else ""),
        (int(charge_link_id),),
    )
    row = cursor.fetchone()
    if not row:
        raise OrganizationError("ORG_WORK_NOT_FOUND", "任务不存在", http_status=404)
    snapshot = row.get("payload_snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError) as exc:
            raise OrganizationError(
                "ORG_CHARGE_AUTHORITY_EVIDENCE_CORRUPT",
                "任务权限代际证据损坏",
                http_status=503,
            ) from exc
    if (
        not isinstance(snapshot, Mapping)
        or int(snapshot.get("schema_version") or 0) != 2
        or not isinstance(snapshot.get("request_payload"), Mapping)
        or not isinstance(snapshot.get("authority_generation"), Mapping)
        or row.get("payload_hash") != payload_hash(snapshot)
    ):
        raise OrganizationError(
            "ORG_CHARGE_AUTHORITY_EVIDENCE_CORRUPT",
            "任务权限代际证据损坏",
            http_status=503,
        )
    return dict(snapshot)


def _lock_live_charge_authority(
    cursor,
    evidence: Mapping[str, Any],
    authority_generation: Mapping[str, Any],
) -> IdentityContext:
    """Reconstruct live authority only from immutable charge-link evidence.

    The caller may use a stale request identity to locate the charge, but no
    request-supplied capability, payer, membership generation, or brand scope
    is trusted at either execution boundary.
    """
    actor_kind = str(evidence.get("actor_kind") or "")
    organization_id = int(evidence.get("organization_id") or 0)
    payer_user_id = int(evidence.get("payer_user_id") or 0)
    feature_code = str(evidence.get("feature_code") or "")
    capability = FEATURE_CAPABILITIES.get(feature_code)
    if capability is None:
        raise OrganizationError(
            "ORG_FEATURE_UNCLASSIFIED",
            "该功能暂不支持团队协作使用，请联系平台",
            http_status=503,
        )

    if actor_kind in {"owner", "member"}:
        actor_user_id = int(evidence.get("actor_user_id") or 0)
        membership_id = int(evidence.get("membership_id") or 0)
        if actor_user_id <= 0 or membership_id <= 0:
            raise OrganizationError(
                "ORG_CHARGE_ACTOR_CORRUPT",
                "付费记录身份代际证据损坏",
                http_status=503,
            )
        stale = IdentityContext(
            request_id=f"charge-live-authority:{evidence.get('id')}",
            authenticated_user_id=actor_user_id,
            principal_user_id=payer_user_id,
            payer_user_id=payer_user_id,
            actor_kind=actor_kind,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            membership_id=membership_id,
        )
        live = _lock_identity(cursor, stale)
        live.require_active_organization()
        if (
            live.actor_kind != actor_kind
            or int(live.organization_id) != organization_id
            or int(live.membership_id or 0) != membership_id
            or int(live.actor_user_id or 0) != actor_user_id
            or int(live.principal_user_id) != payer_user_id
            or int(live.payer_user_id) != payer_user_id
        ):
            raise OrganizationError(
                "ORG_CHARGE_AUTHORITY_CHANGED",
                "你的权限在任务创建后有变化，请重新发起",
                http_status=409,
                retryable=True,
            )
        if live.is_member:
            live.require(capability)
    elif actor_kind == "system":
        cursor.execute(
            """
            SELECT id,owner_user_id,status,version,authority_version
            FROM organizations WHERE id=%s FOR UPDATE
            """,
            (organization_id,),
        )
        organization = cursor.fetchone()
        if (
            not organization
            or organization["status"] != "active"
            or int(organization["owner_user_id"]) != payer_user_id
        ):
            raise OrganizationError(
                "ORG_SYSTEM_AUTHORITY_CHANGED",
                "团队权限在任务创建后有变化，请重新发起",
                http_status=409,
            )
        occurrence_id = int(evidence.get("automatic_plan_occurrence_id") or 0)
        if occurrence_id <= 0:
            raise OrganizationError(
                "ORG_PLAN_OCCURRENCE_MISSING",
                "自动任务缺少 occurrence 身份",
                http_status=503,
            )
        cursor.execute(
            """
            SELECT x.id,x.status AS occurrence_status,p.organization_id,
                   p.status AS plan_status,p.ends_at,p.feature_code,p.work_kind,p.brand_id
            FROM organization_plan_occurrences x
            JOIN organization_automatic_plans p ON p.id=x.plan_id
            WHERE x.id=%s
            FOR UPDATE OF x,p
            """,
            (occurrence_id,),
        )
        occurrence = cursor.fetchone()
        if (
            not occurrence
            or int(occurrence["organization_id"]) != organization_id
            or occurrence["plan_status"] != "active"
            or occurrence["ends_at"] <= datetime.now(timezone.utc)
            or occurrence["occurrence_status"] not in {"reserved", "running"}
            or occurrence["feature_code"] != feature_code
            or int(occurrence.get("brand_id") or 0) != int(evidence.get("brand_id") or 0)
        ):
            raise OrganizationError(
                "ORG_PLAN_AUTHORITY_CHANGED",
                "自动计划、occurrence 或执行范围已变化",
                http_status=409,
            )
        live = IdentityContext(
            request_id=f"charge-live-authority:{evidence.get('id')}",
            authenticated_user_id=payer_user_id,
            principal_user_id=payer_user_id,
            payer_user_id=payer_user_id,
            actor_kind="system",
            organization_id=organization_id,
            organization_status=str(organization["status"]),
            requested_by_user_id=evidence.get("requested_by_user_id"),
            organization_version=int(organization["version"]),
            organization_authority_version=int(organization["authority_version"]),
        )
    else:
        raise OrganizationError(
            "ORG_CHARGE_ACTOR_UNSUPPORTED",
            "该付费记录不能进入组织执行路径",
            http_status=503,
        )

    current_generation = _authority_generation_evidence(live)
    if any(
        (int(authority_generation.get(field)) if authority_generation.get(field) is not None else None)
        != (int(current_generation.get(field)) if current_generation.get(field) is not None else None)
        for field in current_generation
    ):
        raise OrganizationError(
            "ORG_CHARGE_AUTHORITY_CHANGED",
            "任务创建后的组织或成员权限代际已变化",
            http_status=409,
            retryable=True,
        )

    _require_live_brand_scope(cursor, live, evidence.get("brand_id"))
    return live


def _same_charge_authority_evidence(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    fields = (
        "id",
        "request_id",
        "organization_id",
        "membership_id",
        "payer_user_id",
        "actor_user_id",
        "actor_kind",
        "brand_id",
        "feature_code",
        "automatic_plan_occurrence_id",
    )
    return all(left.get(field) == right.get(field) for field in fields)


def _approval_action(
    cursor,
    identity: IdentityContext,
    *,
    requested_action: Optional[str],
    estimated_points: int,
    feature_code: str,
) -> Optional[str]:
    if identity.is_owner or identity.is_system:
        return None
    if requested_action in EXTERNAL_ACTIONS:
        return requested_action
    required, _ = approval_required(
        cursor,
        identity,
        action_type="billing.execute_high_cost",
        estimated_points=estimated_points,
        feature_code=feature_code,
    )
    return "billing.execute_high_cost" if required else None


async def reserve_charge(
    identity: IdentityContext,
    *,
    execution_id: str,
    feature_code: str,
    work_kind: str,
    payload: Mapping[str, Any],
    brand_id: Optional[int] = None,
    task_ref: Optional[str] = None,
    approval_request_id: Optional[int] = None,
    external_action: Optional[str] = None,
    dynamic_ceiling_extra_points: int = 0,
    dispatch_ttl_seconds: int = 300,
    automatic_plan_occurrence_id: Optional[int] = None,
) -> dict[str, Any]:
    """Reserve wallet, every applicable limit, approval, link, and outbox."""
    require_feature_flag("ORGANIZATION_SHARED_PAYER_ENABLED")
    feature_code = str(feature_code or "").strip()
    work_kind = str(work_kind or "").strip()
    execution_id = str(execution_id or "").strip()
    if not feature_code or not work_kind or not execution_id:
        raise OrganizationError("ORG_CHARGE_REQUEST_INVALID", "付费任务参数不完整", http_status=422)
    if dynamic_ceiling_extra_points < 0:
        raise OrganizationError("ORG_COST_CEILING_INVALID", "本次任务的算力上限无效", http_status=422)
    capability = FEATURE_CAPABILITIES.get(feature_code)
    if capability is None:
        raise OrganizationError("ORG_FEATURE_UNCLASSIFIED", "该功能暂不支持团队协作使用，请联系平台", http_status=503)
    with get_db() as conn:
        cursor = conn.cursor()
        # Contract lock #1: serialize every execution id before touching any
        # authority, approval, wallet, period, or evidence row.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-charge:{execution_id}",),
        )
        if identity.is_system:
            cursor.execute(
                """
                SELECT id,owner_user_id,status,version,authority_version
                FROM organizations WHERE id=%s FOR UPDATE
                """,
                (identity.organization_id,),
            )
            organization = cursor.fetchone()
            if not organization or organization["status"] != "active":
                raise OrganizationError("ORG_INACTIVE", "团队当前状态不可执行自动任务", http_status=403)
            if int(organization["owner_user_id"]) != int(identity.payer_user_id):
                raise OrganizationError("ORG_SYSTEM_PAYER_MISMATCH", "自动任务付款人证据不一致", http_status=503)
            identity = IdentityContext(
                request_id=identity.request_id,
                authenticated_user_id=int(organization["owner_user_id"]),
                principal_user_id=int(organization["owner_user_id"]),
                payer_user_id=int(organization["owner_user_id"]),
                actor_kind="system",
                organization_id=int(organization["id"]),
                requested_by_user_id=identity.requested_by_user_id,
                organization_version=int(organization["version"]),
                organization_authority_version=int(organization["authority_version"]),
            )
        else:
            identity = _lock_identity(cursor, identity)
            identity.require_active_organization()
        if identity.is_member:
            identity.require(capability)
        _require_brand_assignment(cursor, identity, brand_id)
        occurrence_evidence = None
        if automatic_plan_occurrence_id is not None:
            if not identity.is_system:
                raise OrganizationError(
                    "ORG_PLAN_ACTOR_INVALID",
                    "自动计划 occurrence 只能由 system actor 预留",
                    http_status=403,
                )
            cursor.execute(
                """
                SELECT x.*,p.organization_id AS plan_organization_id,
                       p.status AS plan_status,p.feature_code AS plan_feature_code,
                       p.work_kind AS plan_work_kind,p.brand_id AS plan_brand_id,
                       p.max_occurrence_points,p.ends_at
                FROM organization_plan_occurrences x
                JOIN organization_automatic_plans p ON p.id=x.plan_id
                WHERE x.id=%s FOR UPDATE OF x,p
                """,
                (automatic_plan_occurrence_id,),
            )
            occurrence_evidence = cursor.fetchone()
            if (
                not occurrence_evidence
                or int(occurrence_evidence["plan_organization_id"]) != identity.organization_id
                or occurrence_evidence["plan_feature_code"] != feature_code
                or occurrence_evidence["plan_work_kind"] != work_kind
                or int(occurrence_evidence.get("plan_brand_id") or 0) != int(brand_id or 0)
            ):
                raise OrganizationError(
                    "ORG_PLAN_OCCURRENCE_MISMATCH",
                    "自动计划 occurrence 与执行请求不一致",
                    http_status=409,
                )
        work_snapshot = _work_snapshot(payload, identity)
        # Idempotent replay runs BEFORE the payer consent gate: it is read-only
        # against frozen charge evidence and produces no new funds action
        # (the occurrence re-bind below only re-attaches the same charge a
        # system actor already created).  A client retrying the original
        # request after the owner disabled payer consent must recover the
        # original charge handle here instead of losing it to a 403.
        cursor.execute("SELECT * FROM organization_charge_links WHERE request_id=%s FOR UPDATE", (execution_id,))
        replay = cursor.fetchone()
        request_digest = payload_hash(work_snapshot)
        if replay:
            if (
                int(replay["organization_id"]) != identity.organization_id
                or int(replay["payer_user_id"]) != identity.payer_user_id
                or replay["feature_code"] != feature_code
                or replay["actor_kind"] != identity.actor_kind
                or int(replay.get("actor_user_id") or 0) != int(identity.actor_user_id or 0)
                or int(replay.get("membership_id") or 0) != int(identity.membership_id or 0)
                or int(replay.get("brand_id") or 0) != int(brand_id or 0)
                or int(replay.get("automatic_plan_occurrence_id") or 0)
                    != int(automatic_plan_occurrence_id or 0)
            ):
                raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "执行幂等键与原任务不一致", http_status=409)
            cursor.execute(
                "SELECT execution_id,work_kind FROM organization_work_outbox WHERE charge_link_id=%s FOR UPDATE",
                (replay["id"],),
            )
            outbox = cursor.fetchone()
            stored_snapshot = _read_work_snapshot(cursor, int(replay["id"]), for_update=True)
            if (
                not outbox
                or outbox["execution_id"] != execution_id
                or outbox["work_kind"] != work_kind
                # Authority generations fence the already-created execution at
                # claim/provider time, but are deliberately not part of the
                # client request equivalence check. A response-loss replay of a
                # terminal charge must remain recoverable after an unrelated
                # live-authority generation bump, while different arguments
                # under the same idempotency key still fail closed.
                or payload_hash(stored_snapshot["request_payload"]) != payload_hash(dict(payload))
            ):
                raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "执行内容与原任务不一致", http_status=409)
            if occurrence_evidence is not None:
                bound_charge_id = occurrence_evidence.get("charge_link_id")
                if bound_charge_id is not None and int(bound_charge_id) != int(replay["id"]):
                    raise OrganizationError(
                        "ORG_PLAN_OCCURRENCE_CONFLICT",
                        "本次自动执行已在处理中，请勿重复操作",
                        http_status=503,
                    )
                if bound_charge_id is None and occurrence_evidence["status"] == "scheduled":
                    cursor.execute(
                        """
                        UPDATE organization_plan_occurrences
                        SET status='reserved',charge_link_id=%s,updated_at=NOW()
                        WHERE id=%s AND status='scheduled' AND charge_link_id IS NULL
                        """,
                        (replay["id"], automatic_plan_occurrence_id),
                    )
                    if cursor.rowcount != 1:
                        raise OrganizationError(
                            "ORG_PLAN_OCCURRENCE_CONFLICT",
                            "自动计划 occurrence 绑定失败",
                            http_status=503,
                        )
            return _safe_charge(replay, replayed=True)
        # Payer consent gate — only brand-new member reservations reach it
        # (idempotent replays returned above with the original charge and must
        # not be re-fenced by a consent change that postdates them).  The
        # policy row lock lands directly after the organizations/membership
        # FOR UPDATE locks in the frozen lock order.  A missing or disabled
        # policy rejects member work before any pricing, approval, wallet
        # freeze, task evidence, or provider traffic exists.
        payer_policy = None
        if identity.is_member:
            payer_policy = member_payer_consent(cursor, identity)
        pricing = get_feature_pricing(feature_code, cursor=cursor)
        ceiling = int(pricing["cost_points"]) + int(dynamic_ceiling_extra_points)
        if ceiling <= 0:
            raise OrganizationError("ORG_COST_CEILING_UNBOUNDED", "该功能无法解析有限正数成本上限", http_status=409)
        if occurrence_evidence is not None and (
            occurrence_evidence["plan_status"] != "active"
            or occurrence_evidence["ends_at"] <= datetime.now(timezone.utc)
            or occurrence_evidence["status"] != "scheduled"
            or occurrence_evidence.get("charge_link_id") is not None
            or int(occurrence_evidence["reserved_ceiling_points"]) != ceiling
            or int(occurrence_evidence["max_occurrence_points"]) != ceiling
        ):
            raise OrganizationError(
                "ORG_PLAN_OCCURRENCE_NOT_RESERVABLE",
                "自动计划 occurrence 当前不可预留",
                http_status=409,
            )
        estimated = ceiling
        pricing_version, pricing_hash = _pricing_snapshot(pricing, ceiling)

        approval_action = _approval_action(
            cursor,
            identity,
            requested_action=external_action,
            estimated_points=estimated,
            feature_code=feature_code,
        )
        approval = None
        if approval_action:
            approval = lock_approval_for_execution(
                cursor,
                identity,
                approval_id=approval_request_id,
                action_type=approval_action,
                payload=payload,
                estimated_points=estimated,
                feature_code=feature_code,
                execution_id=execution_id,
            )
        try:
            lock_legacy_wallet_with_cursor(cursor, identity.payer_user_id)
        except HTTPException as exc:
            raise _translate_http_error(exc) from exc
        reservation_context = BillingActorContext(
            identity=identity,
            feature_code=feature_code,
            execution_id=execution_id,
            estimated_points=estimated,
            reserved_ceiling_points=ceiling,
            pricing_version=pricing_version,
            pricing_snapshot_hash=pricing_hash,
            brand_id=brand_id,
            task_ref=task_ref,
            approval_request_id=int(approval["id"]) if approval else None,
            approval_policy_version=int(approval["policy_version"]) if approval else None,
            physical_reservation_only=True,
        )
        try:
            # Physical evidence is created before period rows are locked. Any
            # subsequent cap failure rolls the caller-owned transaction back.
            physical = await reserve_points_with_cursor(
                cursor, reservation_context, reason="organization reservation"
            )
        except HTTPException as exc:
            raise _translate_http_error(exc) from exc
        within_limit_points = 0
        overage_points = 0
        if identity.is_member:
            limits, within_limit_points, overage_points = lock_applicable_limits_split(
                cursor, identity, feature_code=feature_code, reservation_points=ceiling
            )
            # Over-limit work is charged to the owner wallet only under the
            # locked owner consent and its three hard caps. Any rejection here
            # rolls the caller-owned transaction back: zero task, zero durable
            # freeze, zero provider traffic.
            enforce_overage_caps(cursor, identity, payer_policy, overage_points=overage_points)
        else:
            limits = lock_applicable_limits(
                cursor, identity, feature_code=feature_code, reservation_points=ceiling
            )
        employee_limit_snapshot = None
        if identity.is_member:
            employee_limit_snapshot = {
                "within_limit_points": within_limit_points,
                "limits": [
                    {
                        "id": int(row["id"]),
                        "limit_kind": row["limit_kind"],
                        "feature_code": row.get("feature_code"),
                        "limit_points": int(row["limit_points"]),
                        "reserved_points": int(row["reserved_points"]),
                        "consumed_points": int(row["consumed_points"]),
                        "refunded_points": int(row["refunded_points"]),
                        "period_start": str(row["period_start"]),
                        "period_end": str(row["period_end"]),
                        "period_timezone": row["period_timezone"],
                        "policy_version": int(row["policy_version"]),
                    }
                    for row in limits
                ],
            }
        context = BillingActorContext(
            identity=identity,
            feature_code=feature_code,
            execution_id=execution_id,
            estimated_points=estimated,
            reserved_ceiling_points=ceiling,
            pricing_version=pricing_version,
            pricing_snapshot_hash=pricing_hash,
            brand_id=brand_id,
            task_ref=task_ref,
            applicable_spend_limit_ids=tuple(int(row["id"]) for row in limits) if within_limit_points > 0 or not identity.is_member else (),
            approval_request_id=int(approval["id"]) if approval else None,
            approval_policy_version=int(approval["policy_version"]) if approval else None,
            payer_policy_version=int(payer_policy["policy_version"]) if identity.is_member else None,
            payer_overage_points=overage_points,
        )
        cursor.execute(
            """
            INSERT INTO organization_charge_links(
              request_id,organization_id,membership_id,payer_user_id,actor_user_id,actor_kind,
              requested_by_user_id,brand_id,feature_code,task_ref,primary_period_id,
              automatic_plan_occurrence_id,
              applicable_spend_limit_set_hash,estimated_points,reserved_ceiling_points,status,
              physical_backend,physical_freeze_id,physical_split_snapshot,approval_request_id,
              approval_policy_version,pricing_version,pricing_snapshot_hash,lease_until,
              payer_policy_version,owner_consent_snapshot,employee_limit_snapshot,
              within_limit_points,overage_points
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved',
                      'legacy_user_wallet',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (
                execution_id,
                identity.organization_id,
                identity.membership_id,
                identity.payer_user_id,
                identity.actor_user_id,
                identity.actor_kind,
                identity.requested_by_user_id,
                brand_id,
                feature_code,
                task_ref,
                int(limits[0]["id"]) if limits else None,
                automatic_plan_occurrence_id,
                payload_hash(sorted(int(row["id"]) for row in limits)) if limits else None,
                estimated,
                ceiling,
                str(physical["freeze_id"]),
                canonical_json(physical["physical_split_snapshot"]),
                int(approval["id"]) if approval else None,
                int(approval["policy_version"]) if approval else None,
                pricing_version,
                pricing_hash,
                datetime.now(timezone.utc) + timedelta(seconds=max(30, int(dispatch_ttl_seconds))),
                int(payer_policy["policy_version"]) if identity.is_member else None,
                canonical_json(payer_policy) if identity.is_member else None,
                canonical_json(employee_limit_snapshot) if employee_limit_snapshot is not None else None,
                within_limit_points if identity.is_member else 0,
                overage_points,
            ),
        )
        charge = dict(cursor.fetchone())
        for limit_row in limits:
            if within_limit_points <= 0:
                break
            cursor.execute(
                """
                UPDATE organization_spend_limits
                SET reserved_points=reserved_points+%s,version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (within_limit_points, limit_row["id"]),
            )
            cursor.execute(
                """
                INSERT INTO organization_charge_limit_links(
                  charge_link_id,spend_limit_id,limit_kind,reserved_points,period_start,
                  period_end,period_timezone,limit_policy_version
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    charge["id"],limit_row["id"],limit_row["limit_kind"],within_limit_points,
                    limit_row["period_start"],limit_row["period_end"],
                    limit_row["period_timezone"],limit_row["policy_version"],
                ),
            )
        cursor.execute(
            """
            INSERT INTO organization_work_outbox(
              charge_link_id,execution_id,work_kind,payload_hash,payload_snapshot,status,
              dispatch_deadline
            ) VALUES (%s,%s,%s,%s,%s,'pending',%s)
            """,
            (
                charge["id"],execution_id,work_kind,request_digest,
                canonical_json(work_snapshot),datetime.now(timezone.utc) + timedelta(seconds=max(30, int(dispatch_ttl_seconds))),
            ),
        )
        if occurrence_evidence is not None:
            cursor.execute(
                """
                UPDATE organization_plan_occurrences
                SET status='reserved',charge_link_id=%s,updated_at=NOW()
                WHERE id=%s AND status='scheduled' AND charge_link_id IS NULL
                """,
                (charge["id"], automatic_plan_occurrence_id),
            )
            if cursor.rowcount != 1:
                raise OrganizationError(
                    "ORG_PLAN_OCCURRENCE_CONFLICT",
                    "自动计划 occurrence 绑定失败",
                    http_status=503,
                )
        _audit(cursor, identity, action="billing.reserve", entity_type="organization_charge_link", entity_id=charge["id"], after={"feature_code": feature_code, "ceiling": ceiling, "limit_ids": list(context.applicable_spend_limit_ids), "approval_request_id": context.approval_request_id})
        return _safe_charge(charge, replayed=False)


def _safe_charge(row: Mapping[str, Any], *, replayed: bool = False) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "request_id": row["request_id"],
        "organization_id": int(row["organization_id"]),
        "membership_id": int(row["membership_id"]) if row.get("membership_id") is not None else None,
        "payer_user_id": int(row["payer_user_id"]),
        "actor_user_id": int(row["actor_user_id"]) if row.get("actor_user_id") is not None else None,
        "actor_kind": row["actor_kind"],
        "feature_code": row["feature_code"],
        "brand_id": row.get("brand_id"),
        "estimated_points": int(row["estimated_points"]),
        "reserved_ceiling_points": int(row["reserved_ceiling_points"]),
        "actual_points": int(row["actual_points"]) if row.get("actual_points") is not None else None,
        "refunded_points": int(row.get("refunded_points") or 0),
        "status": row["status"],
        "approval_request_id": row.get("approval_request_id"),
        "payer_policy_version": row.get("payer_policy_version"),
        "within_limit_points": int(row.get("within_limit_points") or 0),
        "overage_points": int(row.get("overage_points") or 0),
        "created_at": row["created_at"],
        "replayed": replayed,
    }


def _billing_context_from_link(link: Mapping[str, Any], limit_ids: tuple[int, ...]) -> BillingActorContext:
    actor_kind = link["actor_kind"]
    identity = IdentityContext(
        request_id=str(link["request_id"]),
        authenticated_user_id=int(link.get("actor_user_id") or link["payer_user_id"]),
        principal_user_id=int(link["payer_user_id"]),
        payer_user_id=int(link["payer_user_id"]),
        actor_kind=actor_kind,
        organization_id=int(link["organization_id"]),
        actor_user_id=int(link["actor_user_id"]) if link.get("actor_user_id") is not None else None,
        membership_id=int(link["membership_id"]) if link.get("membership_id") is not None else None,
        requested_by_user_id=int(link["requested_by_user_id"]) if link.get("requested_by_user_id") is not None else None,
    )
    return BillingActorContext(
        identity=identity,
        feature_code=str(link["feature_code"]),
        execution_id=str(link["request_id"]),
        estimated_points=int(link["estimated_points"]),
        reserved_ceiling_points=int(link["reserved_ceiling_points"]),
        pricing_version=str(link["pricing_version"]),
        pricing_snapshot_hash=str(link["pricing_snapshot_hash"]),
        brand_id=link.get("brand_id"),
        task_ref=link.get("task_ref"),
        applicable_spend_limit_ids=limit_ids,
        approval_request_id=link.get("approval_request_id"),
        approval_policy_version=link.get("approval_policy_version"),
        payer_policy_version=link.get("payer_policy_version"),
        payer_overage_points=int(link.get("overage_points") or 0),
    )


def _lock_charge_and_limits(cursor, charge_link_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cursor.execute("SELECT * FROM organization_charge_links WHERE id=%s", (charge_link_id,))
    preliminary = cursor.fetchone()
    if not preliminary:
        raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"organization-charge:{preliminary['request_id']}",),
    )
    cursor.execute("SELECT id FROM organizations WHERE id=%s FOR UPDATE", (preliminary["organization_id"],))
    if preliminary.get("membership_id") is not None:
        cursor.execute(
            "SELECT id FROM organization_memberships WHERE id=%s AND organization_id=%s FOR UPDATE",
            (preliminary["membership_id"], preliminary["organization_id"]),
        )
        if not cursor.fetchone():
            raise OrganizationError("ORG_CHARGE_MEMBERSHIP_MISSING", "付费记录员工证据缺失", http_status=503)
    if preliminary.get("approval_request_id") is not None:
        cursor.execute(
            "SELECT id FROM organization_approval_requests WHERE id=%s AND organization_id=%s FOR UPDATE",
            (preliminary["approval_request_id"], preliminary["organization_id"]),
        )
        if not cursor.fetchone():
            raise OrganizationError("ORG_CHARGE_APPROVAL_MISSING", "付费记录审批证据缺失", http_status=503)
    try:
        lock_legacy_wallet_with_cursor(cursor, int(preliminary["payer_user_id"]))
    except HTTPException as exc:
        raise _translate_http_error(exc) from exc
    if preliminary.get("physical_freeze_id") is not None:
        cursor.execute(
            "SELECT id FROM point_freezes WHERE id=%s AND user_id=%s FOR UPDATE",
            (int(preliminary["physical_freeze_id"]), int(preliminary["payer_user_id"])),
        )
        if not cursor.fetchone():
            raise OrganizationError("ORG_CHARGE_FREEZE_MISSING", "物理钱包预留证据缺失", http_status=503)
    cursor.execute(
        "SELECT spend_limit_id FROM organization_charge_limit_links WHERE charge_link_id=%s ORDER BY spend_limit_id",
        (charge_link_id,),
    )
    spend_limit_ids = [int(row["spend_limit_id"]) for row in cursor.fetchall()]
    if spend_limit_ids:
        cursor.execute(
            "SELECT id FROM organization_spend_limits WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
            (spend_limit_ids,),
        )
        if [int(row["id"]) for row in cursor.fetchall()] != spend_limit_ids:
            raise OrganizationError("ORG_CHARGE_LIMIT_LEG_MISSING", "员工上限证据缺失", http_status=503)
    cursor.execute("SELECT * FROM organization_charge_links WHERE id=%s FOR UPDATE", (charge_link_id,))
    link = cursor.fetchone()
    if not link or link["request_id"] != preliminary["request_id"] or int(link["payer_user_id"]) != int(preliminary["payer_user_id"]):
        raise OrganizationError("ORG_CHARGE_EVIDENCE_CHANGED", "付费记录证据已变化", http_status=503)
    cursor.execute(
        """
        SELECT j.*,l.reserved_points AS bucket_reserved,l.consumed_points AS bucket_consumed,
               l.refunded_points AS bucket_refunded,l.status AS bucket_status
        FROM organization_charge_limit_links j
        JOIN organization_spend_limits l ON l.id=j.spend_limit_id
        WHERE j.charge_link_id=%s ORDER BY j.spend_limit_id FOR UPDATE OF j
        """,
        (charge_link_id,),
    )
    limits = [dict(row) for row in cursor.fetchall()]
    if (
        link["actor_kind"] == "member"
        and not limits
        and not (link.get("payer_policy_version") and link.get("owner_consent_snapshot"))
    ):
        raise OrganizationError("ORG_CHARGE_LIMIT_LEG_MISSING", "员工付费记录缺少上限记账腿", http_status=503)
    if link["actor_kind"] == "system" and limits:
        raise OrganizationError("ORG_SYSTEM_LIMIT_LEG_FORBIDDEN", "系统任务错误关联员工上限", http_status=503)
    return dict(link), limits


def claim_live_charge(
    identity: IdentityContext,
    *,
    charge_link_id: int,
    lease_seconds: int = 1800,
) -> dict[str, Any]:
    """Claim a reservation for an in-process legacy handler.

    Claiming is deliberately *not* the external-effect boundary.  It re-locks
    live authority and grants a lease; the caller must subsequently call
    :func:`mark_external_side_effect_started` immediately before the first
    provider/business side effect.  A crash in between is therefore provably
    unstarted and can safely release the reservation.
    """
    claim_token = "live_" + secrets.token_hex(24)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM organization_charge_links WHERE id=%s",
            (int(charge_link_id),),
        )
        preliminary = cursor.fetchone()
        if not preliminary or int(preliminary["organization_id"]) != int(identity.organization_id):
            raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
        actor_matches = (
            preliminary["actor_kind"] in {"owner", "member"}
            and int(preliminary.get("actor_user_id") or 0) == int(identity.authenticated_user_id)
        ) or (
            preliminary["actor_kind"] == "system"
            and identity.is_system
            and int(preliminary.get("payer_user_id") or 0) == int(identity.payer_user_id)
        )
        if not actor_matches:
            raise OrganizationError("ORG_CHARGE_ACTOR_MISMATCH", "付费记录员工证据不一致", http_status=403)
        preliminary_snapshot = _read_work_snapshot(cursor, int(charge_link_id))
        live_identity = _lock_live_charge_authority(
            cursor,
            preliminary,
            preliminary_snapshot["authority_generation"],
        )
        cursor.execute(
            "SELECT * FROM organization_charge_links WHERE id=%s FOR UPDATE",
            (int(charge_link_id),),
        )
        link = cursor.fetchone()
        if not link or not _same_charge_authority_evidence(preliminary, link):
            raise OrganizationError("ORG_CHARGE_EVIDENCE_CHANGED", "付费记录身份范围已变化", http_status=503)
        locked_snapshot = _read_work_snapshot(cursor, int(charge_link_id), for_update=True)
        if payload_hash(locked_snapshot) != payload_hash(preliminary_snapshot):
            raise OrganizationError("ORG_CHARGE_EVIDENCE_CHANGED", "任务权限代际证据已变化", http_status=503)
        if live_identity.is_member and int(link.get("membership_id") or 0) != int(live_identity.membership_id or 0):
            raise OrganizationError("ORG_CHARGE_ACTOR_MISMATCH", "付费记录员工证据不一致", http_status=403)
        if link["status"] in {"committed", "released", "refunded"}:
            return _safe_charge(link, replayed=True)
        if link["status"] != "reserved":
            raise OrganizationError("ORG_CHARGE_NOT_RESERVED", "付费记录当前不能执行", http_status=409)
        cursor.execute(
            """
            SELECT status,claim_token,lease_until,execution_started_at,
                   external_side_effect_started_at
            FROM organization_work_outbox
            WHERE charge_link_id=%s
            FOR UPDATE
            """,
            (int(charge_link_id),),
        )
        outbox = cursor.fetchone()
        if not outbox:
            raise OrganizationError("ORG_WORK_NOT_FOUND", "任务不存在", http_status=404)
        now = datetime.now(timezone.utc)
        if (
            outbox["status"] == "claimed"
            and outbox.get("lease_until") is not None
            and outbox["lease_until"] > now
            and outbox.get("execution_started_at") is None
            and outbox.get("external_side_effect_started_at") is None
        ):
            # Never hand a second HTTP request the first request's live claim
            # token.  The caller must detach its cleanup path from this shared
            # reservation and report that the original execution is ongoing.
            data = _safe_charge(link, replayed=True)
            data["in_progress"] = True
            return data
        cursor.execute(
            """
            UPDATE organization_work_outbox
            SET status='claimed',claim_token=%s,claimed_at=COALESCE(claimed_at,NOW()),
                lease_until=NOW()+make_interval(secs=>%s),updated_at=NOW()
            WHERE charge_link_id=%s
              AND (status='pending' OR (status='claimed' AND lease_until<=NOW()))
              AND execution_started_at IS NULL AND external_side_effect_started_at IS NULL
            """,
            (claim_token, max(30, int(lease_seconds)), int(charge_link_id)),
        )
        if cursor.rowcount != 1:
            raise OrganizationError("ORG_WORK_CLAIM_CONFLICT", "任务已在处理中，请勿重复操作", http_status=409)
        cursor.execute(
            """
            UPDATE organization_charge_links
            SET attempt_token=%s,lease_until=NOW()+make_interval(secs=>%s),updated_at=NOW()
            WHERE id=%s RETURNING *
            """,
            (claim_token, max(30, int(lease_seconds)), int(charge_link_id)),
        )
        claimed = cursor.fetchone()
        data = _safe_charge(claimed)
        data["claim_token"] = claim_token
        return data


def get_charge_reconciliation_state(identity: IdentityContext, *, charge_link_id: int) -> dict[str, Any]:
    """Read a unique charge for settlement recovery without acquiring provider authority.

    A worker whose commit response was lost may have an outbox already marked
    running and therefore must not reclaim permission to call providers.  It
    still needs a tenant-safe way to replay the idempotent commit/release.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM organization_charge_links WHERE id=%s", (int(charge_link_id),))
        preliminary = cursor.fetchone()
        if not preliminary or int(preliminary["organization_id"]) != int(identity.organization_id):
            raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
        actor_matches = (
            preliminary["actor_kind"] in {"owner", "member"}
            and int(preliminary.get("actor_user_id") or 0) == int(identity.authenticated_user_id)
        ) or (
            preliminary["actor_kind"] == "system"
            and identity.is_system
            and int(preliminary.get("payer_user_id") or 0) == int(identity.payer_user_id)
        )
        if not actor_matches:
            raise OrganizationError("ORG_CHARGE_ACTOR_MISMATCH", "付费记录员工证据不一致", http_status=403)
        snapshot = _read_work_snapshot(cursor, int(charge_link_id))
        # Settlement recovery is not authority to start new business work.  A
        # revocation after an immutable charge was created must stop provider
        # POSTs, but it must not strand that charge in an unknown wallet state.
        cursor.execute("SELECT * FROM organization_charge_links WHERE id=%s FOR UPDATE", (int(charge_link_id),))
        current = cursor.fetchone()
        if not current or not _same_charge_authority_evidence(preliminary, current):
            raise OrganizationError("ORG_CHARGE_EVIDENCE_CHANGED", "付费记录身份范围已变化", http_status=503)
        locked_snapshot = _read_work_snapshot(cursor, int(charge_link_id), for_update=True)
        if payload_hash(snapshot) != payload_hash(locked_snapshot):
            raise OrganizationError("ORG_CHARGE_EVIDENCE_CHANGED", "任务权限代际证据已变化", http_status=503)
        return _safe_charge(current, replayed=True)


def _read_live_outbox_claim(cursor, charge_link_id: int):
    """返回**仍然活着的 live claim** 行,没有则 None。

    🔴 「活」= 未过期 且 token 不是 poll_ 前缀。
       两个条件缺一不可:
       · 只看 lease 未过期 ⇒ 轮询之间也互相拒绝,用户刷两次就卡死;
       · 只看前缀 ⇒ worker 真死了之后没人能接管,冻结永远悬着。
    """
    cursor.execute(
        """
        SELECT claim_token, lease_until
          FROM organization_work_outbox
         WHERE charge_link_id=%s AND status IN ('claimed','running')
           AND lease_until IS NOT NULL AND lease_until>NOW()
        """,
        (int(charge_link_id),),
    )
    row = cursor.fetchone()
    if not row:
        return None
    token = str(row.get("claim_token") or "")
    return None if token.startswith("poll_") else row


def claim_poll_only_charge(
    identity: IdentityContext,
    *,
    charge_link_id: int,
    attempt_id: int,
    provider_task_id: str,
    lease_seconds: int = 1800,
) -> dict[str, Any]:
    """Fence a recovery worker to poll/materialize one already-paid attempt.

    The immutable task id proves the provider POST already happened.  This path
    intentionally does not grant submit authority and never calls the live
    capability check used by :func:`claim_live_charge`.
    """
    task_id = str(provider_task_id or '').strip()
    if not task_id:
        raise OrganizationError("ORG_PROVIDER_TASK_REQUIRED", "轮询恢复缺少 provider task", http_status=409)
    claim_token = "poll_" + secrets.token_hex(24)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM organization_charge_links WHERE id=%s", (int(charge_link_id),))
        preliminary = cursor.fetchone()
        if not preliminary or int(preliminary["organization_id"]) != int(identity.organization_id):
            raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
        actor_matches = str(preliminary.get("actor_kind") or '') == str(identity.actor_kind) and (
            (
                str(identity.actor_kind) in {'owner', 'member'}
                and int(preliminary.get("actor_user_id") or 0) == int(identity.actor_user_id or identity.authenticated_user_id or 0)
            )
            or (
                identity.is_system
                and int(preliminary.get("payer_user_id") or 0) == int(identity.payer_user_id)
            )
        )
        if not actor_matches:
            raise OrganizationError("ORG_CHARGE_ACTOR_MISMATCH", "付费记录员工证据不一致", http_status=403)
        snapshot = _read_work_snapshot(cursor, int(charge_link_id))
        job_id = int((snapshot.get("request_payload") or {}).get("job_id") or 0)
        if job_id <= 0:
            raise OrganizationError("ORG_WORK_SNAPSHOT_INVALID", "任务快照缺少 job 锚", http_status=503)
        cursor.execute(
            """
            SELECT id FROM marketing_material_generation_attempts
            WHERE id=%s AND job_id=%s AND provider_task_id=%s
              AND submit_state='submitted' AND poll_state IN ('polling','pending','succeeded')
              AND status IN ('pending','running','succeeded')
            FOR UPDATE
            """,
            (int(attempt_id), job_id, task_id),
        )
        if not cursor.fetchone():
            raise OrganizationError("ORG_PROVIDER_ATTEMPT_CHANGED", "provider attempt 锚不一致", http_status=409)
        cursor.execute("SELECT * FROM organization_charge_links WHERE id=%s FOR UPDATE", (int(charge_link_id),))
        link = cursor.fetchone()
        locked_snapshot = _read_work_snapshot(cursor, int(charge_link_id), for_update=True)
        if not link or not _same_charge_authority_evidence(preliminary, link) or payload_hash(snapshot) != payload_hash(locked_snapshot):
            raise OrganizationError("ORG_CHARGE_EVIDENCE_CHANGED", "付费记录身份范围已变化", http_status=503)
        if str(link.get("status") or '') != 'reserved':
            if str(link.get("status") or '') in {'committed', 'released', 'refunded'}:
                return _safe_charge(link, replayed=True)
            raise OrganizationError("ORG_CHARGE_NOT_RESERVED", "付费记录当前不能轮询恢复", http_status=409)
        # 🔴🔴 [#113 D1] 活租约不可被第二 claimant 覆写。
        #
        #    生产事实(2026-09-05,795 冻结 650 算力 23h):worker 已 claim + 已调用
        #    provider,用户**停在页面上刷新**(78 秒内轮询约 35 次)⇒ 本函数把
        #    claim_token 覆写成 poll_ · lease 重置;worker 下一次 provider_guard 的
        #    `WHERE claim_token=%s` 命中 0 行 ⇒ ORG_WORK_LEASE_LOST ⇒
        #    冻结无人 commit/release,**此刻 lease 还剩 30 分钟**。
        #
        #    同一条不变量在本文件写了两处,只守了一处:
        #    `claim_live_charge` 的谓词有 `(status='pending' OR (status='claimed'
        #    AND lease_until<=NOW()))`,这里**没有** —— 一处有一处没有,而没有的那处不报错。
        #
        #    接管条件(与上面同口径):原 worker **确已死**(lease 过期),
        #    或原 token 本身就是 poll_(轮询之间互相接管无害)。
        #    活 worker 存活期间返回 in_progress,不抢它的 token。
        _live = _read_live_outbox_claim(cursor, int(charge_link_id))
        if _live is not None:
            data = _safe_charge(link, replayed=True)
            data["in_progress"] = True
            return data
        cursor.execute(
            """
            UPDATE organization_work_outbox
            SET status='running',claim_token=%s,lease_until=NOW()+make_interval(secs=>%s),
                updated_at=NOW()
            WHERE charge_link_id=%s AND status IN ('claimed','running')
              AND external_side_effect_started_at IS NOT NULL
              AND (lease_until IS NULL OR lease_until<=NOW()
                   OR LEFT(claim_token, 5) = 'poll_')
            """,
            (claim_token, max(30, int(lease_seconds)), int(charge_link_id)),
        )
        if cursor.rowcount != 1:
            raise OrganizationError("ORG_POLL_RECLAIM_CONFLICT", "已付费任务轮询权获取失败", http_status=409)
        cursor.execute(
            """
            UPDATE organization_charge_links
            SET attempt_token=%s,lease_until=NOW()+make_interval(secs=>%s),updated_at=NOW()
            WHERE id=%s AND status='reserved' RETURNING *
            """,
            (claim_token, max(30, int(lease_seconds)), int(charge_link_id)),
        )
        claimed = cursor.fetchone()
        data = _safe_charge(claimed, replayed=True)
        data["claim_token"] = claim_token
        data["poll_only"] = True
        return data


def renew_poll_only_lease(*, charge_link_id: int, claim_token: str, attempt_id: int,
                          provider_task_id: str, lease_seconds: int = 1800) -> None:
    """Renew a poll-only fence without granting a provider submit boundary."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 1 FROM organization_work_outbox o
            JOIN organization_charge_links c ON c.id=o.charge_link_id
            JOIN marketing_material_generation_attempts a
              ON a.id=%s AND a.job_id=(o.payload_snapshot #>> '{request_payload,job_id}')::bigint
            WHERE o.charge_link_id=%s AND o.claim_token=%s AND o.status='running'
              AND o.lease_until>NOW() AND c.status='reserved' AND c.attempt_token=%s
              AND a.provider_task_id=%s AND a.submit_state='submitted'
            FOR UPDATE OF o,c,a
            """,
            (int(attempt_id), int(charge_link_id), str(claim_token), str(claim_token), str(provider_task_id)),
        )
        if not cursor.fetchone():
            raise OrganizationError("ORG_WORK_LEASE_LOST", "已付费任务轮询租约已失效", http_status=409)
        lease = max(30, int(lease_seconds))
        cursor.execute(
            "UPDATE organization_work_outbox SET lease_until=GREATEST(lease_until,NOW()+make_interval(secs=>%s)),updated_at=NOW() "
            "WHERE charge_link_id=%s AND claim_token=%s",
            (lease, int(charge_link_id), str(claim_token)),
        )
        cursor.execute(
            "UPDATE organization_charge_links SET lease_until=GREATEST(lease_until,NOW()+make_interval(secs=>%s)),updated_at=NOW() "
            "WHERE id=%s AND attempt_token=%s",
            (lease, int(charge_link_id), str(claim_token)),
        )


def claim_poll_only_charge_by_anchor(*, charge_link_id: int, attempt_id: int,
                                     provider_task_id: str,
                                     lease_seconds: int = 1800) -> dict[str, Any]:
    """Internal worker entry after an audited operator resolution.

    It reconstructs only immutable tenant/actor coordinates. The delegated
    function still validates the charge, job, attempt and task-id linkage and
    grants no new provider POST authority.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM organization_charge_links WHERE id=%s", (int(charge_link_id),))
        link = cursor.fetchone()
        if not link:
            raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
        actor_user_id = int(link.get("actor_user_id") or link.get("payer_user_id") or 0)
        identity = IdentityContext(
            request_id=f"poll-reconcile:{int(charge_link_id)}:{int(attempt_id)}",
            authenticated_user_id=actor_user_id,
            principal_user_id=int(link["payer_user_id"]),
            payer_user_id=int(link["payer_user_id"]),
            actor_kind=str(link["actor_kind"]),
            organization_id=int(link["organization_id"]),
            actor_user_id=int(link.get("actor_user_id") or 0) or None,
            membership_id=int(link.get("membership_id") or 0) or None,
        )
    return claim_poll_only_charge(
        identity,
        charge_link_id=int(charge_link_id), attempt_id=int(attempt_id),
        provider_task_id=str(provider_task_id), lease_seconds=lease_seconds,
    )


def _artifact_evidence_hash(artifacts: Sequence[Mapping[str, Any]]) -> str:
    """Canonical hash of the artifact evidence stamped at settlement time."""
    canonical = sorted(
        (
            {
                "artifact_type": str(artifact.get("artifact_type") or ""),
                "artifact_id": int(artifact.get("artifact_id") or 0),
                "brand_id": (int(artifact["brand_id"]) if artifact.get("brand_id") is not None else None),
                "visibility": str(artifact.get("visibility") or "private"),
                "publish": bool(artifact.get("publish")),
            }
            for artifact in artifacts
        ),
        key=lambda item: (item["artifact_type"], item["artifact_id"]),
    )
    return payload_hash(canonical)


def _require_reconciliation_replay_evidence(
    cursor,
    *,
    charge_link_id: int,
    result_payload: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    artifact_writer: Optional[Callable[[Any], Mapping[str, Any]]],
) -> None:
    """对账恢复重放必须逐值重演首次结算的 outcome/artifact 证据。

    活跃路径 replay 由存活租约背书；对账路径没有 token，唯一防伪手段就是
    精确证据比对——outcome 快照哈希比对 outbox.result_snapshot，artifact
    证据哈希比对首次 billing.settle 审计固化的 artifact_hash（2026-07-23
    统一 R3 §六：replay 比较 outcome/artifact hash，非只比算力数）。
    """
    effective_result_payload = dict(result_payload)
    effective_artifacts = list(artifacts)
    if artifact_writer is not None:
        writer_result = dict(artifact_writer(cursor) or {})
        effective_result_payload.update(dict(writer_result.get("result_payload") or {}))
        effective_artifacts.extend(list(writer_result.get("artifacts") or ()))
    cursor.execute(
        "SELECT result_snapshot FROM organization_work_outbox WHERE charge_link_id=%s",
        (int(charge_link_id),),
    )
    row = cursor.fetchone()
    stored_snapshot = (row or {}).get("result_snapshot")
    if isinstance(stored_snapshot, str):
        stored_snapshot = json.loads(stored_snapshot)
    if payload_hash(effective_result_payload) != payload_hash(stored_snapshot or {}):
        raise OrganizationError(
            "ORG_SETTLE_OUTCOME_MISMATCH",
            "对账重放的执行结果快照与首次结算不一致",
            http_status=409,
            safe_details={"charge_link_id": int(charge_link_id)},
        )
    cursor.execute(
        """
        SELECT after_snapshot FROM organization_audit_events
        WHERE action='billing.settle' AND entity_type='organization_charge_link' AND entity_id=%s
        ORDER BY id DESC LIMIT 1
        """,
        (str(int(charge_link_id)),),
    )
    audit_row = cursor.fetchone()
    stored_after = (audit_row or {}).get("after_snapshot")
    if isinstance(stored_after, str):
        stored_after = json.loads(stored_after)
    stored_artifact_hash = (stored_after or {}).get("artifact_hash")
    if stored_artifact_hash is not None and _artifact_evidence_hash(effective_artifacts) != str(stored_artifact_hash):
        raise OrganizationError(
            "ORG_SETTLE_ARTIFACT_MISMATCH",
            "对账重放的产物证据与首次结算不一致",
            http_status=409,
            safe_details={"charge_link_id": int(charge_link_id)},
        )


# 对账恢复结算只接受内部 capability 通道自报身份（2026-07-23 统一 R3 §六）。
# 本入口从不暴露 HTTP；recovery_identity 必须是 ``<内部模块命名空间>:<通道>``
# 形态，命名空间根在下方受信集合内（platform/system 类内部通道），完整身份
# 写入 billing.settle 审计与运行日志。外部传入的任意字符串（用户 id、HTTP
# 来源、无通道裸词）一律 ORG_RECOVERY_IDENTITY_UNTRUSTED 拒绝。
_INTERNAL_RECOVERY_NAMESPACE_ROOTS = frozenset({
    "geo_factory", "services", "server", "scheduler", "scripts", "pytest",
})
_INTERNAL_RECOVERY_IDENTITY_RE = re.compile(
    r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*:[a-z0-9][a-z0-9_\-]{0,80}"
)


async def _settle_charge_core(
    *,
    charge_link_id: int,
    actual_points: int,
    result_payload: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]] = (),
    diagnosis_run_token: Optional[str] = None,
    artifact_writer: Optional[Callable[[Any], Mapping[str, Any]]] = None,
    claim_token: Optional[str] = None,
    recovery_identity: Optional[str] = None,
) -> dict[str, Any]:
    """Shared settlement core; reached only through the two public entries.

    Exactly one execution mode is legal (外部独立审核裁决 2026-07-23）：
      - 活跃执行路径：``claim_token`` 必填且必须匹配存活租约
        （:func:`settle_charge`）；
      - kill-9 恢复/对账路径：``recovery_identity`` 必填，不要求存活 token，
        但 external_side_effect_started_at 非空 + 非空 outcome 快照两道
        证据闸完全一致（:func:`settle_charge_via_reconciliation`）。
    """
    if claim_token is None and recovery_identity is None:
        raise OrganizationError(
            "ORG_SETTLE_MODE_REQUIRED",
            "结算必须显式选择活跃 claim 路径或对账恢复路径",
            http_status=500,
        )
    if claim_token is not None and recovery_identity is not None:
        raise OrganizationError(
            "ORG_SETTLE_MODE_AMBIGUOUS",
            "活跃 claim 路径与对账恢复路径不得混用",
            http_status=500,
        )
    with get_db() as conn:
        cursor = conn.cursor()
        link, limits = _lock_charge_and_limits(cursor, charge_link_id)
        if link["status"] == "committed":
            if int(link["actual_points"]) != int(actual_points):
                raise OrganizationError("ORG_SETTLE_CONFLICT", "重复结算的算力数不一致", http_status=409)
            if recovery_identity is not None:
                # 对账恢复重放（2026-07-23 统一 R3 §六）：replay 判别不能只看
                # 算力数——outcome 快照与 artifact 证据必须与首次结算逐值一致，
                # 防止有人借同一 charge 用不同 outcome 重放冒领"已提交"语义。
                _require_reconciliation_replay_evidence(
                    cursor,
                    charge_link_id=int(charge_link_id),
                    result_payload=result_payload,
                    artifacts=artifacts,
                    artifact_writer=artifact_writer,
                )
            return _safe_charge(link, replayed=True)
        if link["status"] != "reserved":
            raise OrganizationError("ORG_CHARGE_NOT_RESERVED", "付费记录当前不能结算", http_status=409)
        # 外部独立审核裁决（2026-07-23）：settle 必须绑定真实 claim/provider/
        # outcome 耐久证据。被 provider 闸拒绝、根本没执行的 reservation 只能
        # release（冻结退回），不得 settle 确认消费。
        #   (a) 调用方持有有效 claim（claim_token 匹配且 lease 未过期）；kill-9
        #       恢复/reconciliation 重放的等价证据是 outbox 的
        #       external_side_effect_started_at——该标记只能由持有有效 claim
        #       的 worker 经 mark_external_side_effect_started 耐久写入，证明
        #       真实 claim 曾存在并已越过 provider 边界；
        #   (b) organization_work_outbox.external_side_effect_started_at
        #       IS NOT NULL（真实到达 provider 边界）；
        #   (c) outcome/result 快照存在（调用方必须携带非空 result_payload，
        #       settle 将其固化为 outbox.result_snapshot）。
        cursor.execute(
            """
            SELECT status,claim_token,lease_until,external_side_effect_started_at
            FROM organization_work_outbox
            WHERE charge_link_id=%s
            """,
            (int(charge_link_id),),
        )
        outbox = cursor.fetchone()
        if not outbox or outbox.get("external_side_effect_started_at") is None:
            raise OrganizationError(
                "ORG_CHARGE_NOT_EXECUTED",
                "付费记录尚未到达外部执行边界，不能结算；未执行的预留请改用释放退回冻结",
                http_status=409,
                safe_details={"charge_link_id": int(charge_link_id), "release_entry": "release_charge"},
            )
        if claim_token is not None:
            # 活跃执行路径：settle 前原子续租（GREATEST 不缩租）并绑定真实存活
            # 租约（stale-worker 防护）。单条 UPDATE 的 WHERE 同时完成「token
            # 匹配 + 活跃态 + 租约未过期」判别与续租，rowcount!=1 即租约已失
            # （租约被第二 claim 抢走、token 伪造、租约过期、outbox 已非活跃
            # 态）——读-判-写分离会在判别后留下抢租窗口，原子 UPDATE 没有。
            settlement_lease_seconds = 1800
            cursor.execute(
                """
                UPDATE organization_work_outbox
                SET lease_until=GREATEST(lease_until,NOW()+make_interval(secs=>%s)),updated_at=NOW()
                WHERE charge_link_id=%s AND claim_token=%s
                  AND status IN ('claimed','running') AND lease_until>NOW()
                """,
                (settlement_lease_seconds, int(charge_link_id), str(claim_token)),
            )
            if cursor.rowcount != 1:
                raise OrganizationError("ORG_WORK_LEASE_LOST", "任务租约已失效，禁止当前 worker 结算", http_status=409)
            cursor.execute(
                """
                UPDATE organization_charge_links
                SET lease_until=GREATEST(lease_until,NOW()+make_interval(secs=>%s)),updated_at=NOW()
                WHERE id=%s AND status='reserved' AND attempt_token=%s AND lease_until>NOW()
                """,
                (settlement_lease_seconds, int(charge_link_id), str(claim_token)),
            )
            if cursor.rowcount != 1:
                raise OrganizationError("ORG_CHARGE_LEASE_LOST", "付费记录租约已失效，禁止当前 worker 结算", http_status=409)
        else:
            # kill-9 恢复/对账路径（settle_charge_via_reconciliation）：不要求
            # 存活 token——kill-9 后 token 已随进程死亡。等价证据是上方
            # external_side_effect_started_at 非空闸（只能由持有真实 claim 的
            # worker 经 mark_external_side_effect_started 耐久写入）+ 下方
            # outcome 快照闸；recovery_identity 由公开入口强制非空并写入审计。
            # 原子要求收紧（2026-07-23 统一 R3 §六）：
            #   (d) durable recovery 状态：outbox 必须停在 claimed/running/
            #       unknown 三种 durable 未终结态之一——pending 证明从未到达
            #       provider 边界（上方证据闸已先拒），succeeded/cancelled 是
            #       终态，终态不该再进恢复结算通道；
            #   (e) 活跃 lease 存在且未过期时拒绝：租约未死说明 worker 可能
            #       仍然活跃，对账结算会与活跃 worker 双结算；恢复方必须等
            #       租约自然死亡（kill-9 后 lease 到期）再进本入口。
            outbox_status = str(outbox.get("status") or "")
            if outbox_status not in {"claimed", "running", "unknown"}:
                raise OrganizationError(
                    "ORG_RECOVERY_STATE_NOT_DURABLE",
                    "任务未停在可恢复的 durable 状态，拒绝对账结算",
                    http_status=409,
                    safe_details={"charge_link_id": int(charge_link_id), "outbox_status": outbox_status},
                )
            outbox_lease_until = outbox.get("lease_until")
            if (
                outbox_status in {"claimed", "running"}
                and outbox_lease_until is not None
                and outbox_lease_until > datetime.now(timezone.utc)
            ):
                raise OrganizationError(
                    "ORG_RECONCILIATION_LEASE_ACTIVE",
                    "任务租约尚未过期，worker 仍可能结算，拒绝对账双结算",
                    http_status=409,
                    retryable=True,
                    safe_details={"charge_link_id": int(charge_link_id)},
                )
            logger.warning(
                "organization charge reconciliation settle: charge_link_id=%s recovery_identity=%s",
                int(charge_link_id), recovery_identity,
            )
        actual_points = int(actual_points)
        ceiling = int(link["reserved_ceiling_points"])
        if actual_points < 0 or actual_points > ceiling:
            cursor.execute("UPDATE organization_charge_links SET status='unknown',external_side_effect_started_at=COALESCE(external_side_effect_started_at,NOW()),updated_at=NOW() WHERE id=%s", (charge_link_id,))
            cursor.execute(
                """
                UPDATE organization_work_outbox
                SET status='unknown',last_error_code='ACTUAL_EXCEEDS_RESERVED_CEILING',updated_at=NOW()
                WHERE charge_link_id=%s
                """,
                (charge_link_id,),
            )
            # Persist the fail-closed quarantine before surfacing the error.
            # Billing primitives remain caller-owned and never commit/rollback.
            conn.commit()
            raise OrganizationError("ORG_ACTUAL_EXCEEDS_CEILING", "实际消耗超过本次上限，任务已暂停，请联系客服核对", http_status=409)
        context = _billing_context_from_link(link, tuple(int(row["spend_limit_id"]) for row in limits))
        effective_result_payload = dict(result_payload)
        effective_artifacts = list(artifacts)
        writer_result: dict[str, Any] = {}
        if artifact_writer is not None:
            writer_result = dict(artifact_writer(cursor) or {})
            effective_result_payload.update(dict(writer_result.get("result_payload") or {}))
            effective_artifacts.extend(list(writer_result.get("artifacts") or ()))
        if not effective_result_payload:
            # 证据 (c)：无真实 outcome 快照不得确认消费；未执行的预留走 release。
            raise OrganizationError(
                "ORG_CHARGE_OUTCOME_MISSING",
                "结算必须携带真实执行结果快照，未执行的预留请改用释放退回冻结",
                http_status=409,
                safe_details={"charge_link_id": int(charge_link_id), "release_entry": "release_charge"},
            )
        split = link.get("physical_split_snapshot")
        if isinstance(split, str):
            split = json.loads(split)
        result = await commit_reserved_points_with_cursor(
            cursor,
            context,
            freeze_id=int(link["physical_freeze_id"]),
            actual_points=actual_points,
            reserved_split=dict(split or {}),
            reason="organization task settled",
        )
        is_split = link.get("owner_consent_snapshot") is not None
        reserved_within = int(link.get("within_limit_points") or 0)
        actual_within = min(actual_points, reserved_within) if is_split else actual_points
        actual_overage = actual_points - actual_within
        for limit_row in limits:
            ceiling_for_bucket = int(limit_row["reserved_points"])
            cursor.execute(
                """
                UPDATE organization_spend_limits
                SET reserved_points=reserved_points-%s,consumed_points=consumed_points+%s,
                    version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (ceiling_for_bucket, actual_within, limit_row["spend_limit_id"]),
            )
            cursor.execute(
                """
                UPDATE organization_charge_limit_links SET consumed_points=%s,updated_at=NOW()
                WHERE charge_link_id=%s AND spend_limit_id=%s
                """,
                (actual_within, charge_link_id, limit_row["spend_limit_id"]),
            )
        cursor.execute(
            """
            UPDATE organization_charge_links SET status='committed',actual_points=%s,amount_total=%s,
              charge_tx_id=%s,within_limit_points=%s,overage_points=%s,updated_at=NOW()
            WHERE id=%s RETURNING *
            """,
            (actual_points, actual_points, str(result.get("charge_tx_id")) if result.get("charge_tx_id") else None, actual_within, actual_overage, charge_link_id),
        )
        updated = cursor.fetchone()
        cursor.execute(
            """
            UPDATE organization_work_outbox
            SET status='succeeded',result_snapshot=%s::jsonb,
                finished_at=NOW(),updated_at=NOW()
            WHERE charge_link_id=%s
            """,
            (canonical_json(effective_result_payload), charge_link_id),
        )
        if effective_artifacts:
            from services.organization_artifacts import stamp_artifact

            for artifact in effective_artifacts:
                stamp_artifact(
                    cursor,
                    context.identity,
                    artifact_type=str(artifact["artifact_type"]),
                    artifact_id=artifact["artifact_id"],
                    brand_id=artifact.get("brand_id"),
                    visibility=str(artifact.get("visibility") or "private"),
                )
                if artifact.get("artifact_type") == "diagnosis" and artifact.get("publish"):
                    cursor.execute(
                        "UPDATE diagnosis_records SET result_visibility='published' WHERE id=%s",
                        (artifact["artifact_id"],),
                    )
                    if cursor.rowcount != 1:
                        raise OrganizationError("ORG_ARTIFACT_PUBLISH_FAILED", "诊断结果保存失败，请重试", http_status=503)
        if diagnosis_run_token:
            cursor.execute(
                """
                UPDATE diagnosis_runs
                SET run_status='completed_exempt',status_changed_at=NOW(),settled_at=NOW(),
                    finished_at=NOW(),final_snapshot_jsonb=%s
                WHERE run_token=%s AND run_status='running'
                """,
                (canonical_json(effective_result_payload), diagnosis_run_token),
            )
            if cursor.rowcount != 1:
                raise OrganizationError("ORG_DIAGNOSIS_STATE_CONFLICT", "诊断任务终态写入失败", http_status=409)
        if link.get("automatic_plan_occurrence_id"):
            cursor.execute(
                """
                SELECT o.plan_id FROM organization_plan_occurrences o
                WHERE o.id=%s FOR UPDATE
                """,
                (link["automatic_plan_occurrence_id"],),
            )
            occurrence = cursor.fetchone()
            if not occurrence:
                raise OrganizationError("ORG_PLAN_OCCURRENCE_MISSING", "自动计划执行证据缺失", http_status=503)
            cursor.execute("SELECT id FROM organization_automatic_plans WHERE id=%s FOR UPDATE", (occurrence["plan_id"],))
            cursor.execute("UPDATE organization_plan_occurrences SET status='settled',actual_points=%s,updated_at=NOW() WHERE id=%s", (actual_points, link["automatic_plan_occurrence_id"]))
            cursor.execute(
                """
                UPDATE organization_automatic_plans
                SET reserved_budget_points=reserved_budget_points-%s,
                    consumed_budget_points=consumed_budget_points+%s,
                    settled_occurrences=settled_occurrences+1,
                    status=CASE WHEN settled_occurrences+1>=max_occurrences THEN 'completed' ELSE status END,
                    version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (ceiling, actual_points, occurrence["plan_id"]),
            )
        finish_approval_execution(cursor, approval_id=link.get("approval_request_id"), succeeded=True)
        identity = context.identity
        settle_audit_after: dict[str, Any] = {"actual_points": actual_points, "released_difference": ceiling - actual_points, "result_hash": payload_hash(effective_result_payload)}
        # artifact 证据哈希与 result_hash 并列固化（2026-07-23 统一 R3 §六）：
        # 对账重放按 outcome+artifact 双哈希逐值判别，非只比算力数。
        settle_audit_after["artifact_hash"] = _artifact_evidence_hash(effective_artifacts)
        if recovery_identity is not None:
            # 对账恢复结算必须能回答"谁/什么流程在恢复"（外部独立审核裁决
            # 2026-07-23）；活跃路径无此键，两种模式在审计流里一眼可辨。
            settle_audit_after["recovery"] = {
                "mode": "reconciliation",
                "recovery_identity": recovery_identity,
            }
        _audit(cursor, identity, action="billing.settle", entity_type="organization_charge_link", entity_id=charge_link_id, after=settle_audit_after)
        response = _safe_charge(updated)
        if writer_result:
            response["artifact_result"] = writer_result
        return response


async def settle_charge(
    *,
    charge_link_id: int,
    actual_points: int,
    result_payload: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]] = (),
    diagnosis_run_token: Optional[str] = None,
    artifact_writer: Optional[Callable[[Any], Mapping[str, Any]]] = None,
    claim_token: str,
) -> dict[str, Any]:
    """活跃执行路径的唯一结算入口：``claim_token`` 必填且必须匹配存活租约。

    外部独立审核裁决（2026-07-23）：活跃 worker 必须持有真实 claim
    （claim_live_charge / claim_poll_only_charge 发放的 token），stale
    worker、伪造 token、过期租约一律 ORG_WORK_LEASE_LOST。省略 claim_token
    直接 TypeError（必填关键字参数）；空串/空白按 ORG_CLAIM_TOKEN_REQUIRED
    拒绝。kill-9 恢复/对账不得走本入口，请用
    :func:`settle_charge_via_reconciliation`。
    """
    token = str(claim_token or "").strip()
    if not token:
        raise OrganizationError(
            "ORG_CLAIM_TOKEN_REQUIRED",
            "活跃执行路径结算必须携带真实存活 claim_token；kill-9 恢复/对账请改用 settle_charge_via_reconciliation",
            http_status=409,
            safe_details={"reconciliation_entry": "settle_charge_via_reconciliation"},
        )
    return await _settle_charge_core(
        charge_link_id=charge_link_id,
        actual_points=actual_points,
        result_payload=result_payload,
        artifacts=artifacts,
        diagnosis_run_token=diagnosis_run_token,
        artifact_writer=artifact_writer,
        claim_token=token,
        recovery_identity=None,
    )


async def settle_charge_via_reconciliation(
    *,
    charge_link_id: int,
    actual_points: int,
    result_payload: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]] = (),
    diagnosis_run_token: Optional[str] = None,
    artifact_writer: Optional[Callable[[Any], Mapping[str, Any]]] = None,
    recovery_identity: str,
) -> dict[str, Any]:
    """kill-9 恢复/对账专用结算入口。**活跃执行路径禁止使用。**

    与 :func:`settle_charge` 共享同一份证据闸——
    ``organization_work_outbox.external_side_effect_started_at`` 非空（真实
    到达 provider 边界的耐久证据）+ 非空 outcome 快照——但不要求存活
    claim token（kill-9 后 token 已随进程死亡）。本入口额外强制：

    - ``recovery_identity`` 必填（谁/什么流程在恢复，例如
      ``"geo_factory.reconcile_recoverable_geo_jobs:cron"``），写入
      ``billing.settle`` 审计 after 快照与运行日志；
    - 调用方必须是对账/kill-9 恢复流程。活跃 worker（持有真实 claim）走
      本入口即绕过 stale-worker 租约防护，属于违规用法。
    """
    identity = str(recovery_identity or "").strip()
    if not identity:
        raise OrganizationError(
            "ORG_RECOVERY_IDENTITY_REQUIRED",
            "对账恢复结算必须记录 recovery_identity（谁/什么流程在恢复）",
            http_status=422,
        )
    namespace_root = identity.split(":", 1)[0].split(".", 1)[0]
    if (
        not _INTERNAL_RECOVERY_IDENTITY_RE.fullmatch(identity)
        or namespace_root not in _INTERNAL_RECOVERY_NAMESPACE_ROOTS
    ):
        # 内部 capability 闸（2026-07-23 统一 R3 §六）：对账恢复等同资金
        # 终局操作，只接受 platform/system 类内部通道自报身份；身份形态与
        # 命名空间根都受信才放行，完整身份写入审计供事后追责。
        raise OrganizationError(
            "ORG_RECOVERY_IDENTITY_UNTRUSTED",
            "对账恢复身份必须来自内部受信通道（<内部模块>:<通道> 形态）",
            http_status=403,
        )
    return await _settle_charge_core(
        charge_link_id=charge_link_id,
        actual_points=actual_points,
        result_payload=result_payload,
        artifacts=artifacts,
        diagnosis_run_token=diagnosis_run_token,
        artifact_writer=artifact_writer,
        claim_token=None,
        recovery_identity=identity,
    )


def get_committed_charge_result(
    identity: IdentityContext,
    *,
    charge_link_id: int,
) -> dict[str, Any]:
    """Return a durable terminal response only to the original live actor.

    The result is stored as server-produced JSON, never as a caller-supplied
    credential. Current membership/capability/assignment and brand ownership
    are revalidated, while the historical generation snapshot remains the
    evidence for the execution that already settled.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT request_id,organization_id FROM organization_charge_links WHERE id=%s",
            (int(charge_link_id),),
        )
        preliminary = cursor.fetchone()
        if not preliminary:
            raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-charge:{preliminary['request_id']}",),
        )
        live_identity = _lock_identity(cursor, identity)
        live_identity.require_active_organization()
        cursor.execute(
            "SELECT * FROM organization_charge_links WHERE id=%s FOR UPDATE",
            (int(charge_link_id),),
        )
        link = cursor.fetchone()
        if (
            not link
            or str(link["request_id"]) != str(preliminary["request_id"])
            or int(link["organization_id"]) != int(preliminary["organization_id"])
        ):
            raise OrganizationError("ORG_CHARGE_EVIDENCE_CHANGED", "付费记录证据已变化", http_status=503)
        capability = FEATURE_CAPABILITIES.get(str(link["feature_code"]))
        if capability is None:
            raise OrganizationError("ORG_FEATURE_UNCLASSIFIED", "该功能暂不支持团队协作使用，请联系平台", http_status=503)
        if live_identity.is_member:
            live_identity.require(capability)
        if (
            int(link["organization_id"]) != int(live_identity.organization_id)
            or int(link["payer_user_id"]) != int(live_identity.payer_user_id)
            or str(link["actor_kind"]) != str(live_identity.actor_kind)
            or int(link.get("actor_user_id") or 0) != int(live_identity.actor_user_id or 0)
            or int(link.get("membership_id") or 0) != int(live_identity.membership_id or 0)
        ):
            raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
        _require_live_brand_scope(cursor, live_identity, link.get("brand_id"))
        if link["status"] != "committed":
            raise OrganizationError("ORG_CHARGE_RESULT_NOT_READY", "任务结果尚未就绪，请稍后刷新", http_status=409)
        cursor.execute(
            """
            SELECT status,result_snapshot FROM organization_work_outbox
            WHERE charge_link_id=%s FOR UPDATE
            """,
            (int(charge_link_id),),
        )
        outbox = cursor.fetchone()
        if not outbox or outbox["status"] != "succeeded":
            raise OrganizationError("ORG_CHARGE_RESULT_NOT_READY", "任务结果尚未就绪，请稍后刷新", http_status=409)
        snapshot = outbox.get("result_snapshot")
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except (TypeError, ValueError) as exc:
                raise OrganizationError("ORG_CHARGE_RESULT_CORRUPT", "任务结果证据损坏", http_status=503) from exc
        if not isinstance(snapshot, Mapping):
            raise OrganizationError("ORG_CHARGE_RESULT_CORRUPT", "任务结果证据损坏", http_status=503)
        return dict(snapshot)


def find_charge_replay(
    identity: IdentityContext,
    *,
    execution_id: str,
    feature_code: str,
    work_kind: str,
    payload: Mapping[str, Any],
    brand_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Probe a prior execution before mutable business-scope resolution.

    This lets an HTTP response-loss retry recover a committed result even if
    keywords, pricing, or other live inputs changed after the original commit.
    The client request itself is still hash-bound, and current authorization is
    always revalidated before any terminal result is returned.
    """
    execution_id = str(execution_id or "").strip()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-charge:{execution_id}",),
        )
        live_identity = _lock_identity(cursor, identity)
        live_identity.require_active_organization()
        capability = FEATURE_CAPABILITIES.get(str(feature_code))
        if capability is None:
            raise OrganizationError("ORG_FEATURE_UNCLASSIFIED", "该功能暂不支持团队协作使用，请联系平台", http_status=503)
        if live_identity.is_member:
            live_identity.require(capability)
        _require_live_brand_scope(cursor, live_identity, brand_id)
        cursor.execute(
            "SELECT * FROM organization_charge_links WHERE request_id=%s FOR UPDATE",
            (execution_id,),
        )
        link = cursor.fetchone()
        if not link:
            return None
        if (
            int(link["organization_id"]) != int(live_identity.organization_id)
            or int(link["payer_user_id"]) != int(live_identity.payer_user_id)
            or str(link["feature_code"]) != str(feature_code)
            or str(link["actor_kind"]) != str(live_identity.actor_kind)
            or int(link.get("actor_user_id") or 0) != int(live_identity.actor_user_id or 0)
            or int(link.get("membership_id") or 0) != int(live_identity.membership_id or 0)
            or int(link.get("brand_id") or 0) != int(brand_id or 0)
        ):
            raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "执行幂等键与原任务不一致", http_status=409)
        cursor.execute(
            "SELECT work_kind,status,result_snapshot FROM organization_work_outbox WHERE charge_link_id=%s FOR UPDATE",
            (int(link["id"]),),
        )
        outbox = cursor.fetchone()
        stored_snapshot = _read_work_snapshot(cursor, int(link["id"]), for_update=True)
        if (
            not outbox
            or str(outbox["work_kind"]) != str(work_kind)
            or payload_hash(stored_snapshot["request_payload"]) != payload_hash(dict(payload))
        ):
            raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "执行内容与原任务不一致", http_status=409)
        result = _safe_charge(link, replayed=True)
        if link["status"] == "committed":
            snapshot = outbox.get("result_snapshot")
            if isinstance(snapshot, str):
                try:
                    snapshot = json.loads(snapshot)
                except (TypeError, ValueError) as exc:
                    raise OrganizationError("ORG_CHARGE_RESULT_CORRUPT", "任务结果证据损坏", http_status=503) from exc
            if outbox["status"] != "succeeded" or not isinstance(snapshot, Mapping):
                raise OrganizationError("ORG_CHARGE_RESULT_CORRUPT", "任务结果证据损坏", http_status=503)
            result["result_payload"] = dict(snapshot)
        return result


def _alert_charge_went_unknown(*, charge_link_id: int, link, outbox, reason: str,
                               replayed: bool) -> None:
    """付费记录进 unknown 时开一条 critical 告警。**fail-soft 但不静默。**

    fingerprint 按 charge 精确去重:同一笔重复进 unknown 只刷新一条,
    不会把告警面刷爆;而**不同的 charge 各有一条**,不会互相盖住。
    """
    try:
        from db.ai_ops_db import upsert_alert

        frozen = (link or {}).get("physical_freeze_id")
        payload = {
            "charge_link_id": int(charge_link_id),
            "physical_freeze_id": frozen,
            "reason": reason,
            "replayed": bool(replayed),
            "job_id": ((outbox or {}).get("payload_snapshot") or {}).get("request_payload", {})
                       .get("job_id") if isinstance(outbox, dict) else None,
            "work_kind": (outbox or {}).get("work_kind") if isinstance(outbox, dict) else None,
        }
        upsert_alert(
            "org_charge_unknown",
            severity="critical",
            title="组织付费记录进入 unknown(算力冻着,无自动出口)",
            detail=("charge_link_id=%s · freeze=%s · 原因=%s · 出口只有 force_release_charge"
                    "(owner/平台 admin 手工,须 lease 过期)"
                    % (charge_link_id, frozen, reason)),
            fingerprint="org_charge_unknown:%s" % charge_link_id,
            payload=payload,
        )
    except Exception as exc:      # noqa: BLE001 —— 告警失败绝不影响资金安全动作
        logger.warning(
            "[org-billing] charge=%s 进 unknown 的告警没写成(冻结仍在,无人知道):%s",
            charge_link_id, exc)


def _quarantine_locked_charge(
    cursor,
    *,
    link: Mapping[str, Any],
    limits: Sequence[Mapping[str, Any]],
    reason: str,
    diagnosis_run_token: Optional[str] = None,
    diagnosis_session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Move an externally-started reservation to an operator-only terminal hold.

    The wallet and every member-limit leg deliberately remain reserved.  An
    exception, timeout, or lost response after the provider boundary cannot
    prove that no work was performed, so automatically releasing those legs
    would permit both a free execution and an unsafe retry.
    """
    charge_link_id = int(link["id"])
    cursor.execute(
        """
        SELECT * FROM organization_work_outbox
        WHERE charge_link_id=%s FOR UPDATE
        """,
        (charge_link_id,),
    )
    outbox = cursor.fetchone()
    externally_started = bool(
        link.get("external_side_effect_started_at")
        or (outbox and outbox.get("external_side_effect_started_at"))
    )
    if not externally_started:
        raise OrganizationError(
            "ORG_EXTERNAL_OUTCOME_NOT_AMBIGUOUS",
            "任务尚未越过外部调用边界，不能进入未知态",
            http_status=409,
        )
    replayed = link["status"] == "unknown"
    if link["status"] not in {"reserved", "unknown"}:
        raise OrganizationError(
            "ORG_CHARGE_NOT_QUARANTINABLE",
            "付费记录当前不能进入未知态",
            http_status=409,
        )
    error_code = (str(reason or "external outcome unknown").strip() or "external outcome unknown")[:120]
    if replayed:
        updated = link
    else:
        cursor.execute(
            """
            UPDATE organization_charge_links
            SET status='unknown',updated_at=NOW()
            WHERE id=%s RETURNING *
            """,
            (charge_link_id,),
        )
        updated = cursor.fetchone()
    cursor.execute(
        """
        UPDATE organization_work_outbox
        SET status='unknown',finished_at=NOW(),last_error_code=%s,updated_at=NOW()
        WHERE charge_link_id=%s
        """,
        (error_code, charge_link_id),
    )
    # 🔴🔴 [#113 D2-a] 进 unknown **必须有人知道**。
    #
    #    unknown 的唯一出口是 `force_release_charge`(owner/平台 admin 手工,须 lease 过期)。
    #    在此之前**没有任何告警** —— 生产事实:795 冻着 650 算力 23 小时,
    #    恢复链每 60 秒重试一次、每次 409、每次被 except 吞,**24h 跑了 564 次**,
    #    而没有一个人被通知。**沉默的死锁比报错的死锁难发现得多。**
    #
    #    fail-soft:告警写不进去**绝不**影响隔离本身(那是资金安全动作);
    #    但也**不静默** —— 被正确捕获的失败不留痕迹,最该诊断的就最查不到。
    #    `upsert_alert` 自开连接自 commit,不会毒化本事务
    #    (同一 cursor 上一条失败的 SQL 会让后续每一句都 InFailedSqlTransaction)。
    _alert_charge_went_unknown(
        charge_link_id=charge_link_id, link=updated or link,
        outbox=outbox, reason=error_code, replayed=replayed)
    if diagnosis_run_token:
        cursor.execute(
            """
            UPDATE diagnosis_runs
            SET run_status='settlement_manual',status_changed_at=NOW(),
                reaped_reason=%s
            WHERE run_token=%s
              AND run_status IN ('pending_freeze','running','release_pending','commit_pending')
            """,
            (error_code, diagnosis_run_token),
        )
    if diagnosis_session_id:
        cursor.execute(
            "UPDATE diagnosis_records SET result_visibility='withheld' WHERE session_id=%s",
            (diagnosis_session_id,),
        )
    if link.get("automatic_plan_occurrence_id"):
        cursor.execute(
            """
            UPDATE organization_plan_occurrences
            SET status='unknown',last_error_code=%s,updated_at=NOW()
            WHERE id=%s
            """,
            (error_code, link["automatic_plan_occurrence_id"]),
        )
    finish_approval_execution(
        cursor,
        approval_id=link.get("approval_request_id"),
        succeeded=False,
        reason="ORG_EXTERNAL_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED",
    )
    context = _billing_context_from_link(
        link,
        tuple(int(row["spend_limit_id"]) for row in limits),
    )
    if not replayed:
        _audit(
            cursor,
            context.identity,
            action="billing.quarantine_unknown",
            entity_type="organization_charge_link",
            entity_id=charge_link_id,
            after={
                "status": "unknown",
                "reserved_ceiling_points": int(link["reserved_ceiling_points"]),
                "automatic_plan_occurrence_id": link.get("automatic_plan_occurrence_id"),
            },
            reason=error_code,
        )
    return _safe_charge(updated, replayed=replayed)


async def quarantine_charge(
    *,
    charge_link_id: int,
    reason: str,
    diagnosis_run_token: Optional[str] = None,
    diagnosis_session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Durably quarantine an ambiguous external outcome without releasing funds."""
    with get_db() as conn:
        cursor = conn.cursor()
        link, limits = _lock_charge_and_limits(cursor, charge_link_id)
        return _quarantine_locked_charge(
            cursor,
            link=link,
            limits=limits,
            reason=reason,
            diagnosis_run_token=diagnosis_run_token,
            diagnosis_session_id=diagnosis_session_id,
        )


async def release_charge(
    *,
    charge_link_id: int,
    reason: str,
    diagnosis_run_token: Optional[str] = None,
    diagnosis_session_id: Optional[str] = None,
    failure_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    reason = str(reason or "").strip() or "organization task did not run"
    with get_db() as conn:
        cursor = conn.cursor()
        link, limits = _lock_charge_and_limits(cursor, charge_link_id)
        if link["status"] == "released":
            return _safe_charge(link, replayed=True)
        if link["status"] != "reserved":
            raise OrganizationError("ORG_CHARGE_NOT_RELEASABLE", "付费记录当前不能释放", http_status=409)
        if link.get("external_side_effect_started_at") is not None:
            quarantined = _quarantine_locked_charge(
                cursor,
                link=link,
                limits=limits,
                reason=reason,
                diagnosis_run_token=diagnosis_run_token,
                diagnosis_session_id=diagnosis_session_id,
            )
            # Persist the conservative terminal hold before surfacing the
            # ambiguity.  get_db would otherwise roll the quarantine back when
            # the error is raised.
            conn.commit()
            raise OrganizationError(
                "ORG_EXTERNAL_OUTCOME_UNKNOWN",
                "本次任务结果待确认，费用已冻结保护，核对完成前不会扣除，请勿重复操作",
                http_status=409,
                safe_details={"charge_link_id": charge_link_id, "status": quarantined["status"]},
            )
        context = _billing_context_from_link(link, tuple(int(row["spend_limit_id"]) for row in limits))
        result = await release_reserved_points_with_cursor(cursor, context, freeze_id=int(link["physical_freeze_id"]), reason=reason)
        if not result.get("success"):
            raise OrganizationError("ORG_PHYSICAL_RELEASE_FAILED", "物理钱包预留释放失败", http_status=503)
        for limit_row in limits:
            cursor.execute("UPDATE organization_spend_limits SET reserved_points=reserved_points-%s,version=version+1,updated_at=NOW() WHERE id=%s", (limit_row["reserved_points"], limit_row["spend_limit_id"]))
        cursor.execute("UPDATE organization_charge_links SET status='released',actual_points=0,amount_total=0,within_limit_points=0,overage_points=0,updated_at=NOW() WHERE id=%s RETURNING *", (charge_link_id,))
        updated = cursor.fetchone()
        cursor.execute("UPDATE organization_work_outbox SET status='cancelled',finished_at=NOW(),last_error_code=%s,updated_at=NOW() WHERE charge_link_id=%s", (reason[:120], charge_link_id))
        if diagnosis_run_token:
            cursor.execute(
                """
                UPDATE diagnosis_runs
                SET run_status='failed_exempt',status_changed_at=NOW(),settled_at=NOW(),
                    finished_at=NOW(),reaped_reason=%s,final_snapshot_jsonb=%s
                WHERE run_token=%s AND run_status IN ('pending_freeze','running')
                """,
                (
                    reason[:200],
                    canonical_json(dict(failure_snapshot or {})),
                    diagnosis_run_token,
                ),
            )
            if cursor.rowcount != 1:
                raise OrganizationError("ORG_DIAGNOSIS_STATE_CONFLICT", "诊断任务失败终态写入失败", http_status=409)
        if diagnosis_session_id:
            cursor.execute(
                "UPDATE diagnosis_records SET result_visibility='withheld' WHERE session_id=%s",
                (diagnosis_session_id,),
            )
        if link.get("automatic_plan_occurrence_id"):
            cursor.execute("SELECT plan_id,planned_at FROM organization_plan_occurrences WHERE id=%s FOR UPDATE", (link["automatic_plan_occurrence_id"],))
            occurrence = cursor.fetchone()
            if not occurrence:
                raise OrganizationError("ORG_PLAN_OCCURRENCE_MISSING", "自动计划执行证据缺失", http_status=503)
            cursor.execute(
                "SELECT id,status,ends_at FROM organization_automatic_plans WHERE id=%s FOR UPDATE",
                (occurrence["plan_id"],),
            )
            plan = cursor.fetchone()
            retryable_occurrence = bool(
                plan and plan["status"] == "active" and plan["ends_at"] > datetime.now(timezone.utc)
            )
            cursor.execute(
                """
                UPDATE organization_plan_occurrences
                SET status=%s,actual_points=0,
                    charge_link_id=CASE WHEN %s THEN NULL ELSE charge_link_id END,
                    next_attempt_at=CASE WHEN %s THEN NOW()+INTERVAL '60 seconds' ELSE next_attempt_at END,
                    last_error_code=%s,updated_at=NOW()
                WHERE id=%s
                """,
                (
                    "retry_wait" if retryable_occurrence else "released",
                    retryable_occurrence,
                    retryable_occurrence,
                    reason[:120],
                    link["automatic_plan_occurrence_id"],
                ),
            )
            cursor.execute(
                """
                UPDATE organization_automatic_plans
                SET reserved_budget_points=reserved_budget_points-%s,
                    scheduled_occurrences=GREATEST(0,scheduled_occurrences-1),
                    next_occurrence_at=CASE WHEN status='active'
                      THEN LEAST(next_occurrence_at,%s) ELSE next_occurrence_at END,
                    version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (link["reserved_ceiling_points"], occurrence["planned_at"], occurrence["plan_id"]),
            )
        finish_approval_execution(cursor, approval_id=link.get("approval_request_id"), succeeded=False, reason=reason)
        _audit(cursor, context.identity, action="billing.release", entity_type="organization_charge_link", entity_id=charge_link_id, after={"released_points": link["reserved_ceiling_points"]}, reason=reason)
        return _safe_charge(updated)


#: [#113 D2-b] 自动窄门要求的证据契约。三项**全部是落库读数**,不是推断。
#
#    🔴 为什么要三项而不是一项:
#      `provider_succeeded_attempts >= 1` 单独看只说明「钱花了」;
#      `materialized_attempts == 0` 单独看可以被「这个 job 根本没有 attempt 行」
#      空洞满足;`delivered_assets == 0` 是**第二条独立的否定证据**(另一张表)。
#      两条否定臂查的是不同的表,一张表被清空/改名不会让另一张一起哑掉。
#
#    🔴 刻意**不**按失败原因判定(授权撤销 / QA 硬失败 / ……)。
#      「用户到底拿到东西没有」是可以**直接查**的,不需要从失败原因去推。
#      按原因枚举的话,以后新增第三种失败原因就会悄悄落在窄门外面 ——
#      能消灭的轴就别去诊断它。
#: [#113 D2-b ②] 自动窄门要求的证据契约。四项**全部是落库读数**,不是推断。
#
#    🔴 Owner 2026-09-06 拍板放宽:**部分交付也退**。
#      依据不是新规则 —— 仓里正常路径 `_settle_geo` 对 partial 走的就是
#      `release_freeze("GEO 内容包部分成功整单免单")`。窄门只是让**恢复路径**
#      用上同一条既有规则,不是自己发明一套。
#
#    旧版是「三计数 + assets == 0」的零交付口径,与 `_settle_geo` 的 full/partial
#    是**两把尺**;现在统一成 `delivered < expected`(= not full)。
UNDELIVERED_EVIDENCE_KEYS = (
    "delivered",
    "expected",
    "provider_succeeded_attempts",
    "in_flight_attempts",
)


def _assert_undelivered_evidence(evidence: Mapping[str, Any]) -> dict[str, int]:
    """校验 D2-b 证据的**形状与取值**;不合格一律拒绝释放。

    🔴 本模块够不到 marketing_* 表(分层),所以证据由调用方采集。
       但「采集方给什么就信什么」等于没有闸 —— 因此这里做**第二道**校验:
       键必须齐、值必须是整数、且必须真的落在「供应商成功 + 未交付齐 + 无在途」那一格。
       调用方那一侧另有自己的判据;两侧都验,漂一侧会被另一侧顶住。
    """
    if not isinstance(evidence, Mapping):
        raise OrganizationError(
            "ORG_AUTO_RELEASE_EVIDENCE_INVALID", "自动释放证据格式非法", http_status=422)
    missing = [k for k in UNDELIVERED_EVIDENCE_KEYS if k not in evidence]
    if missing:
        raise OrganizationError(
            "ORG_AUTO_RELEASE_EVIDENCE_INCOMPLETE",
            "自动释放证据不完整",
            http_status=422,
            safe_details={"missing": missing},
        )
    facts: dict[str, int] = {}
    for key in UNDELIVERED_EVIDENCE_KEYS:
        raw = evidence[key]
        if isinstance(raw, bool) or not isinstance(raw, int):
            # bool 是 int 的子类:True 会被当成 1 悄悄通过。显式挡掉。
            raise OrganizationError(
                "ORG_AUTO_RELEASE_EVIDENCE_INVALID",
                "自动释放证据格式非法",
                http_status=422,
                safe_details={"field": key},
            )
        facts[key] = int(raw)
    if facts["expected"] <= 0:
        # 🔴 expected 为 0 时 `delivered < expected` 恒假、`>=` 恒真 ——
        #    两个方向都不是「保守」。显式拒绝,不让边界值决定钱的方向。
        raise OrganizationError(
            "ORG_AUTO_RELEASE_EVIDENCE_INVALID",
            "自动释放证据格式非法",
            http_status=422,
            safe_details={"field": "expected"},
        )
    if facts["provider_succeeded_attempts"] < 1:
        raise OrganizationError(
            "ORG_AUTO_RELEASE_NO_PROVIDER_SUCCESS",
            "供应商未成功,不适用自动释放窄门",
            http_status=409,
        )
    if facts["in_flight_attempts"] != 0:
        # 还有 attempt 在跑 ⇒ 供应商可能还会交付,现在退钱等于免费执行。
        raise OrganizationError(
            "ORG_AUTO_RELEASE_STILL_IN_FLIGHT",
            "仍有生成在途,不适用自动释放窄门",
            http_status=409,
            retryable=True,
            safe_details={"in_flight_attempts": facts["in_flight_attempts"]},
        )
    if facts["delivered"] >= facts["expected"]:
        # 🔴 full ⇒ 用户拿全了,该扣钱不该退。窄门只放 `not full`。
        #    (partial 与 none 都放 —— 见上方 Owner 拍板那段。)
        raise OrganizationError(
            "ORG_AUTO_RELEASE_FULLY_DELIVERED",
            "已全部交付,不适用自动释放窄门",
            http_status=409,
            safe_details={k: facts[k] for k in ("delivered", "expected")},
        )
    return facts

async def auto_release_undelivered_charge(
    *,
    charge_link_id: int,
    reason: str,
    evidence: Mapping[str, Any],
    recovery_identity: str,
) -> dict[str, Any]:
    """[#113 D2-b] unknown 且「供应商成功但零交付」⇒ 自动释放冻结。

    🔴 这是给 `_quarantine_locked_charge` 的保守口径开的**一条窄门**,
       不是放宽它。那条口径的原话是:

         "An exception, timeout, or lost response after the provider boundary
          cannot prove that no work was performed."

       它说的是**不能证明**。而本函数要求调用方拿出**能证明**的落库读数:
       供应商成功过(钱确实花了)+ 一件都没交付(两张表各出一条否定证据)。
       在这一格里「用户没拿到东西」不是假设,是查出来的。

    Owner 2026-09-06 拍板的资金语义:**用户没拿到东西不扣**,平台吸收供应商成本;
    图片结果保留在 attempt 行里供审计,不因释放而删。

    🔴 仍然保留的两道保守闸(与人工通道 `force_release_charge` 同口径):
      · `status == "unknown"` —— 别的态一律不走本路径;
      · 租约必须已过期 —— 租约还活着说明 worker 仍可能结算,
        此时释放会同时造成「免费执行」与「重复结算」。
    """
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    reason = str(reason or "").strip() or "provider succeeded but nothing was delivered"
    recovery_identity = str(recovery_identity or "").strip()
    if not recovery_identity:
        raise OrganizationError(
            "ORG_RECOVERY_IDENTITY_REQUIRED", "自动释放必须声明恢复来源", http_status=422)
    facts = _assert_undelivered_evidence(evidence)
    with get_db() as conn:
        cursor = conn.cursor()
        # 与人工通道同款:同一 charge 的并发恢复只允许一个进来。
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-auto-release:{int(charge_link_id)}",),
        )
        link, limits = _lock_charge_and_limits(cursor, charge_link_id)
        if link["status"] == "released":
            # 幂等:上一轮已经放过了。再放一次会把 spend_limits 扣成负数。
            return _safe_charge(link, replayed=True)
        if link["status"] != "unknown":
            raise OrganizationError(
                "ORG_CHARGE_NOT_AUTO_RELEASABLE",
                "仅未知隔离态的付费记录可自动释放",
                http_status=409,
                safe_details={"charge_link_id": int(charge_link_id), "status": link["status"]},
            )
        lease_until = link.get("lease_until")
        if lease_until is not None and lease_until > datetime.now(timezone.utc):
            raise OrganizationError(
                "ORG_CHARGE_LEASE_ACTIVE",
                "任务租约尚未过期,worker 仍可能结算,禁止自动释放",
                http_status=409,
                retryable=True,
                safe_details={"charge_link_id": int(charge_link_id)},
            )
        context = _billing_context_from_link(
            link, tuple(int(row["spend_limit_id"]) for row in limits))
        result = await release_reserved_points_with_cursor(
            cursor, context, freeze_id=int(link["physical_freeze_id"]), reason=reason,
        )
        if not result.get("success"):
            raise OrganizationError(
                "ORG_PHYSICAL_RELEASE_FAILED", "物理钱包预留释放失败", http_status=503)
        for limit_row in limits:
            cursor.execute(
                "UPDATE organization_spend_limits SET reserved_points=reserved_points-%s,"
                "version=version+1,updated_at=NOW() WHERE id=%s",
                (limit_row["reserved_points"], limit_row["spend_limit_id"]),
            )
        cursor.execute(
            "UPDATE organization_charge_links SET status='released',actual_points=0,"
            "amount_total=0,within_limit_points=0,overage_points=0,updated_at=NOW()"
            " WHERE id=%s RETURNING *",
            (int(charge_link_id),),
        )
        updated = cursor.fetchone()
        cursor.execute(
            "UPDATE organization_work_outbox SET status='cancelled',"
            "finished_at=COALESCE(finished_at,NOW()),last_error_code=%s,updated_at=NOW()"
            " WHERE charge_link_id=%s",
            ("AUTO_RELEASE_PROVIDER_SUCCEEDED_UNDELIVERED", int(charge_link_id)),
        )
        _recover_plan_occurrence_after_release(cursor, link=link, reason=reason)
        finish_approval_execution(
            cursor, approval_id=link.get("approval_request_id"), succeeded=False, reason=reason)
        _audit(
            cursor,
            context.identity,
            action="billing.auto_release_undelivered",
            entity_type="organization_charge_link",
            entity_id=int(charge_link_id),
            before={
                "status": "unknown",
                "reserved_ceiling_points": int(link["reserved_ceiling_points"]),
                "lease_until": (lease_until.isoformat() if lease_until else None),
            },
            after={
                "status": "released",
                "released_points": int(link["reserved_ceiling_points"]),
                # 证据进审计:三个月后有人问「凭什么自动退的」,答案在这一行。
                "evidence": facts,
                "recovery_identity": recovery_identity[:200],
                "actor_kind_note": "system_recovery",
            },
            reason=reason,
        )
        return _safe_charge(updated)


def _recover_plan_occurrence_after_release(cursor, *, link: Mapping[str, Any],
                                           reason: str) -> None:
    """自动计划腿的释放恢复(与 release_charge / force_release_charge 同一套语义)。

    🔴 这是本仓**第三份**同语义实现。前两份分别长在 `release_charge` 与
       `force_release_charge` 体内(我已逐字比对过:剥注释、归一化空白与尾逗号后
       两者语义完全相同,唯一差异是 `int()` 包裹)。

       正确的做法是把三份抽成一处。**我没有抽**,理由写在这里以免被当成疏忽:
       `force_release_charge` 的判据住在 `tests/organization_internal_seats/test_payer_policy_pg.py`,
       该包在本机跑不起来(迁移写死 `public.` 与它的 per-run schema 冲突)。
       **重构一段我无法回归验证的资金代码,比留一份重复更危险。**

       代价由 `tests/.../test_d2b_release_leg_drift.py` 的**漂移锁**承担:
       它把三份实现归一化后逐字比对,任何一份被改动都会当场变红并点名另外两份。
       这把「静默分家」换成了「响的失败」——不是消灭了轴,是给它装了报警。
    """
    occurrence_id = link.get("automatic_plan_occurrence_id")
    if not occurrence_id:
        return
    cursor.execute(
        "SELECT plan_id,planned_at FROM organization_plan_occurrences WHERE id=%s FOR UPDATE",
        (occurrence_id,),
    )
    occurrence = cursor.fetchone()
    if not occurrence:
        raise OrganizationError("ORG_PLAN_OCCURRENCE_MISSING", "自动计划执行证据缺失", http_status=503)
    cursor.execute(
        "SELECT id,status,ends_at FROM organization_automatic_plans WHERE id=%s FOR UPDATE",
        (occurrence["plan_id"],),
    )
    plan = cursor.fetchone()
    retryable_occurrence = bool(
        plan and plan["status"] == "active" and plan["ends_at"] > datetime.now(timezone.utc)
    )
    cursor.execute(
        """
        UPDATE organization_plan_occurrences
        SET status=%s,actual_points=0,
            charge_link_id=CASE WHEN %s THEN NULL ELSE charge_link_id END,
            next_attempt_at=CASE WHEN %s THEN NOW()+INTERVAL '60 seconds' ELSE next_attempt_at END,
            last_error_code=%s,updated_at=NOW()
        WHERE id=%s
        """,
        (
            "retry_wait" if retryable_occurrence else "released",
            retryable_occurrence,
            retryable_occurrence,
            reason[:120],
            occurrence_id,
        ),
    )
    cursor.execute(
        """
        UPDATE organization_automatic_plans
        SET reserved_budget_points=reserved_budget_points-%s,
            scheduled_occurrences=GREATEST(0,scheduled_occurrences-1),
            next_occurrence_at=CASE WHEN status='active'
              THEN LEAST(next_occurrence_at,%s) ELSE next_occurrence_at END,
            version=version+1,updated_at=NOW()
        WHERE id=%s
        """,
        (int(link["reserved_ceiling_points"]), occurrence["planned_at"], occurrence["plan_id"]),
    )

def _require_force_release_replay_binding(
    stored_event: Mapping[str, Any],
    *,
    actor_user_id: int,
    admin_override: bool,
    reason: str,
    source_ip: Optional[str],
) -> None:
    """Bind a force-release replay to the exact first-disposition evidence.

    audit_event_key 只钉住 (org, request_id, action, target)；同一 request_id
    被不同 actor/理由/IP 复用时必须稳定 409（2026-07-23 统一 R3 §六 P2），
    不得把首次人工处置当成后来者的幂等 replay。before-after 快照与审计
    payload_hash 一并校验，证据漂移同样拒绝。
    """
    after_snapshot = stored_event.get("after_snapshot")
    if isinstance(after_snapshot, str):
        after_snapshot = json.loads(after_snapshot)
    after_snapshot = dict(after_snapshot or {})
    before_snapshot = stored_event.get("before_snapshot")
    if isinstance(before_snapshot, str):
        before_snapshot = json.loads(before_snapshot)
    before_snapshot = dict(before_snapshot or {})
    expected_actor_kind = "platform_admin" if admin_override else "owner"
    evidence_matches = (
        str(after_snapshot.get("manual_actor_kind") or "") == expected_actor_kind
        and int(after_snapshot.get("manual_actor_user_id") or 0) == int(actor_user_id)
        and str(stored_event.get("reason") or "") == str(reason)
        and (after_snapshot.get("source_ip") or None) == (str(source_ip)[:120] if source_ip else None)
        and str(before_snapshot.get("status") or "") == "unknown"
        and str(stored_event.get("payload_hash") or "") == payload_hash(after_snapshot)
    )
    if not evidence_matches:
        raise OrganizationError(
            "ORG_FORCE_RELEASE_REPLAY_CONFLICT",
            "同一 request_id 的人工处置证据不一致，拒绝重放",
            http_status=409,
        )


async def force_release_charge(
    *,
    identity: Optional[IdentityContext],
    actor_user_id: int,
    is_platform_admin: bool = False,
    charge_link_id: int,
    reason: str,
    request_id: str,
    source_ip: Optional[str] = None,
) -> dict[str, Any]:
    """Manual terminal path for an unknown-quarantined charge (owner or platform admin).

    外部独立审核裁决（2026-07-23）：unknown 隔离 charge 始终占用日/月硬顶，
    人工解决是唯一豁免通道。本函数仅允许 ``status='unknown'`` 且 lease 已过期
    的 charge，按不可变快照 release 冻结腿（复用既有 release 资金腿公开入
    口），并写完整审计（actor/IP/request_id/reason/before/after）。
    force-settle 刻意不提供：无真实 outcome 不得确认消费。
    """
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写本次人工处置原因", http_status=422)
    request_id = str(request_id or "").strip()
    if len(request_id) < 8:
        raise OrganizationError("ORG_REQUEST_ID_INVALID", "请求幂等键无效", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-force-release:{request_id}",),
        )
        admin_override = False
        locked_identity: Optional[IdentityContext] = None
        if identity is not None:
            candidate = _lock_identity(cursor, identity)
            if candidate.is_owner:
                # 暂记 owner 候选；目标 charge 归属未锁定前不能定 owner/admin
                # 路径——平台 ADMIN 拥有自己团队（身份解析为组织 A owner）时，
                # 治理组织 B 必须在下方归口 admin_override，不得被这里的
                # owner 身份短路成跨组织 404（外部独立审核裁决 2026-07-23）。
                locked_identity = candidate
            elif is_platform_admin:
                admin_override = True
            else:
                raise OrganizationError(
                    "ORG_OWNER_REQUIRED",
                    "仅老板可处理待核对的费用",
                    http_status=403,
                )
        else:
            if not is_platform_admin:
                raise OrganizationError("ORG_MEMBERSHIP_REQUIRED", "你还没有加入任何团队", http_status=404)
            admin_override = True
        if is_platform_admin:
            # 凡声明平台管理员身份一律先做 DB 复核，再做任何目标相关判定：
            # 复核结果与 charge 是否存在/归属无关，伪造 admin 旗标的调用方
            # 看到的永远是同一个 403，无法把 403/404 差异常变成存在性预言机。
            _require_platform_admin(cursor, actor_user_id)
        link, limits = _lock_charge_and_limits(cursor, charge_link_id)
        organization_id = int(link["organization_id"])
        if locked_identity is not None and int(locked_identity.organization_id) != organization_id:
            # 显式优先级（外部独立审核裁决 2026-07-23）：
            #   1. 本人就是目标组织 owner → owner 路径（不进入本分支）；
            #   2. 平台管理员（上方已 DB 复核）且目标组织非本人 owner →
            #      admin_override 治理路径，审计 actor 语义与纯 admin 一致；
            #   3. 其余跨组织 → 404，不泄露存在性。
            if is_platform_admin:
                admin_override = True
                locked_identity = None
            else:
                raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
        audit_key = stable_key(
            organization_id, request_id, "billing.force_release", "organization_charge_link", int(charge_link_id), 0,
        )
        cursor.execute(
            """
            SELECT actor_kind,actor_user_id,reason,payload_hash,before_snapshot,after_snapshot
            FROM organization_audit_events WHERE audit_event_key=%s
            """,
            (audit_key,),
        )
        stored_event = cursor.fetchone()
        if stored_event:
            # replay 必须绑死 actor/reason/IP/target/before-after 证据哈希
            # （2026-07-23 统一 R3 §六 P2）：同一 request_id 换 actor、换理由、
            # 换来源 IP 或证据漂移，一律稳定 409，绝不把别人的处置当自己的
            # replay 放行。
            _require_force_release_replay_binding(
                stored_event,
                actor_user_id=int(actor_user_id),
                admin_override=admin_override,
                reason=reason,
                source_ip=source_ip,
            )
            return _safe_charge(link, replayed=True)
        if link["status"] != "unknown":
            raise OrganizationError(
                "ORG_CHARGE_NOT_FORCE_RELEASABLE",
                "仅未知隔离态且租约已过期的付费记录可人工释放",
                http_status=409,
                safe_details={"charge_link_id": int(charge_link_id), "status": link["status"]},
            )
        lease_until = link.get("lease_until")
        if lease_until is not None and lease_until > datetime.now(timezone.utc):
            raise OrganizationError(
                "ORG_CHARGE_LEASE_ACTIVE",
                "任务租约尚未过期，worker 仍可能结算，禁止人工释放",
                http_status=409,
                retryable=True,
                safe_details={"charge_link_id": int(charge_link_id)},
            )
        context = _billing_context_from_link(link, tuple(int(row["spend_limit_id"]) for row in limits))
        result = await release_reserved_points_with_cursor(
            cursor, context, freeze_id=int(link["physical_freeze_id"]), reason=reason,
        )
        if not result.get("success"):
            raise OrganizationError("ORG_PHYSICAL_RELEASE_FAILED", "物理钱包预留释放失败", http_status=503)
        for limit_row in limits:
            cursor.execute(
                "UPDATE organization_spend_limits SET reserved_points=reserved_points-%s,version=version+1,updated_at=NOW() WHERE id=%s",
                (limit_row["reserved_points"], limit_row["spend_limit_id"]),
            )
        cursor.execute(
            "UPDATE organization_charge_links SET status='released',actual_points=0,amount_total=0,within_limit_points=0,overage_points=0,updated_at=NOW() WHERE id=%s RETURNING *",
            (int(charge_link_id),),
        )
        updated = cursor.fetchone()
        cursor.execute(
            "UPDATE organization_work_outbox SET status='cancelled',finished_at=COALESCE(finished_at,NOW()),last_error_code=%s,updated_at=NOW() WHERE charge_link_id=%s",
            ("FORCE_RELEASE_MANUAL_RESOLUTION", int(charge_link_id)),
        )
        if link.get("automatic_plan_occurrence_id"):
            # 与普通 release 同源恢复（2026-07-23 统一 R3 §六 P2）：人工释放
            # 不得只回资金/上限记账腿——scheduled_occurrences/next_occurrence_at/
            # 预算腿必须与 release_charge 同一套恢复语义，计划可重试时
            # occurrence 回到 retry_wait 并释放 charge 锚点供计划体重排。
            cursor.execute(
                "SELECT plan_id,planned_at FROM organization_plan_occurrences WHERE id=%s FOR UPDATE",
                (link["automatic_plan_occurrence_id"],),
            )
            occurrence = cursor.fetchone()
            if not occurrence:
                raise OrganizationError("ORG_PLAN_OCCURRENCE_MISSING", "自动计划执行证据缺失", http_status=503)
            cursor.execute(
                "SELECT id,status,ends_at FROM organization_automatic_plans WHERE id=%s FOR UPDATE",
                (occurrence["plan_id"],),
            )
            plan = cursor.fetchone()
            retryable_occurrence = bool(
                plan and plan["status"] == "active" and plan["ends_at"] > datetime.now(timezone.utc)
            )
            cursor.execute(
                """
                UPDATE organization_plan_occurrences
                SET status=%s,actual_points=0,
                    charge_link_id=CASE WHEN %s THEN NULL ELSE charge_link_id END,
                    next_attempt_at=CASE WHEN %s THEN NOW()+INTERVAL '60 seconds' ELSE next_attempt_at END,
                    last_error_code=%s,updated_at=NOW()
                WHERE id=%s
                """,
                (
                    "retry_wait" if retryable_occurrence else "released",
                    retryable_occurrence,
                    retryable_occurrence,
                    reason[:120],
                    link["automatic_plan_occurrence_id"],
                ),
            )
            cursor.execute(
                """
                UPDATE organization_automatic_plans
                SET reserved_budget_points=reserved_budget_points-%s,
                    scheduled_occurrences=GREATEST(0,scheduled_occurrences-1),
                    next_occurrence_at=CASE WHEN status='active'
                      THEN LEAST(next_occurrence_at,%s) ELSE next_occurrence_at END,
                    version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (int(link["reserved_ceiling_points"]), occurrence["planned_at"], occurrence["plan_id"]),
            )
        before_snapshot = {
            "status": "unknown",
            "reserved_ceiling_points": int(link["reserved_ceiling_points"]),
            "within_limit_points": int(link.get("within_limit_points") or 0),
            "overage_points": int(link.get("overage_points") or 0),
            "automatic_plan_occurrence_id": link.get("automatic_plan_occurrence_id"),
        }
        after_snapshot = {
            "status": "released",
            "released_points": int(link["reserved_ceiling_points"]),
            "manual_actor_kind": "platform_admin" if admin_override else "owner",
            "manual_actor_user_id": int(actor_user_id),
            "source_ip": (str(source_ip)[:120] if source_ip else None),
        }
        if locked_identity is not None:
            audit_identity = IdentityContext(
                request_id=request_id,
                authenticated_user_id=locked_identity.authenticated_user_id,
                principal_user_id=locked_identity.principal_user_id,
                payer_user_id=locked_identity.payer_user_id,
                actor_kind="owner",
                organization_id=organization_id,
                actor_user_id=int(actor_user_id),
                membership_id=locked_identity.membership_id,
            )
        else:
            # organization_audit_events.actor_kind 的冻结 CHECK 不含
            # 'platform_admin'（改动需冻结 schema 轮换），平台管理员路径以
            # 'system' 落枚举列，真实 actor 身份/IP 完整记入 after 快照。
            audit_identity = IdentityContext(
                request_id=request_id,
                authenticated_user_id=int(actor_user_id),
                principal_user_id=int(actor_user_id),
                payer_user_id=int(actor_user_id),
                actor_kind="system",
                organization_id=organization_id,
            )
        _audit(
            cursor,
            audit_identity,
            action="billing.force_release",
            entity_type="organization_charge_link",
            entity_id=int(charge_link_id),
            before=before_snapshot,
            after=after_snapshot,
            reason=reason,
        )
        return _safe_charge(updated)


async def refund_charge(
    *,
    charge_link_id: int,
    cumulative_refund_target: int,
    refund_request_id: str,
    reason: str,
    expected_organization_id: Optional[int] = None,
) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写退款原因", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        link, limits = _lock_charge_and_limits(cursor, charge_link_id)
        if expected_organization_id is not None and int(link["organization_id"]) != int(expected_organization_id):
            raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
        request_ids = list(link.get("refund_request_ids") or [])
        if refund_request_id in request_ids:
            return _safe_charge(link, replayed=True)
        if link["status"] not in {"committed", "refunded"}:
            raise OrganizationError("ORG_CHARGE_NOT_REFUNDABLE", "付费记录当前不能退款", http_status=409)
        actual = int(link["actual_points"])
        target = int(cumulative_refund_target)
        if target < 0 or target > actual:
            raise OrganizationError("ORG_REFUND_TARGET_INVALID", "退款算力数无效", http_status=422)
        before = int(link["refunded_points"])
        if target < before:
            raise OrganizationError("ORG_REFUND_TARGET_REGRESSION", "退款算力数不能低于已退数额", http_status=409)
        if target == before:
            request_ids.append(refund_request_id)
            cursor.execute("UPDATE organization_charge_links SET refund_request_ids=%s,updated_at=NOW() WHERE id=%s RETURNING *", (canonical_json(request_ids), charge_link_id))
            return _safe_charge(cursor.fetchone(), replayed=True)
        if not link.get("charge_tx_id"):
            raise OrganizationError("ORG_CHARGE_LEDGER_MISSING", "扣费记录异常，已停止自动退款，请联系客服", http_status=503)
        context = _billing_context_from_link(link, tuple(int(row["spend_limit_id"]) for row in limits))
        result = await refund_committed_points_with_cursor(
            cursor,
            context,
            charge_tx_id=int(link["charge_tx_id"]),
            cumulative_refund_target=target,
            reason=reason,
        )
        if not result.get("success") or int(result.get("confirmed_refunded") or -1) != target:
            raise OrganizationError("ORG_REFUND_LEDGER_MISMATCH", "物理钱包退款证据不一致", http_status=503)
        delta = target - before
        is_split = link.get("owner_consent_snapshot") is not None
        # Refunds restore the employee-limit leg first; any remainder reduces
        # the owner-funded overage leg. The physical wallet refund already went
        # back to the exact original owner pools via charge_tx_id above.
        within_actual = int(link.get("within_limit_points") or 0)
        if is_split:
            within_refund_delta = min(target, within_actual) - min(before, within_actual)
        else:
            within_refund_delta = delta
        overage_refund_delta = delta - within_refund_delta
        for limit_row in limits:
            cursor.execute("UPDATE organization_spend_limits SET refunded_points=refunded_points+%s,version=version+1,updated_at=NOW() WHERE id=%s", (within_refund_delta, limit_row["spend_limit_id"]))
            cursor.execute("UPDATE organization_charge_limit_links SET refunded_points=refunded_points+%s,updated_at=NOW() WHERE charge_link_id=%s AND spend_limit_id=%s", (within_refund_delta, charge_link_id, limit_row["spend_limit_id"]))
        if link.get("automatic_plan_occurrence_id"):
            cursor.execute("SELECT plan_id FROM organization_plan_occurrences WHERE id=%s FOR UPDATE", (link["automatic_plan_occurrence_id"],))
            occurrence = cursor.fetchone()
            if not occurrence:
                raise OrganizationError("ORG_PLAN_OCCURRENCE_MISSING", "自动计划执行证据缺失", http_status=503)
            cursor.execute("SELECT id FROM organization_automatic_plans WHERE id=%s FOR UPDATE", (occurrence["plan_id"],))
            cursor.execute("UPDATE organization_plan_occurrences SET refunded_points=%s,status=CASE WHEN %s=actual_points THEN 'refunded' ELSE status END,updated_at=NOW() WHERE id=%s", (target, target, link["automatic_plan_occurrence_id"]))
            cursor.execute("UPDATE organization_automatic_plans SET refunded_budget_points=refunded_budget_points+%s,version=version+1,updated_at=NOW() WHERE id=%s", (delta, occurrence["plan_id"]))
        request_ids.append(refund_request_id)
        status = "refunded" if target == actual else "committed"
        refund_ids = list(link.get("refund_tx_ids") or [])
        refund_ids.append({"request_id": refund_request_id, "target": target, "confirmed": target})
        new_overage = int(link.get("overage_points") or 0) - overage_refund_delta
        if new_overage < 0:
            raise OrganizationError("ORG_REFUND_LEDGER_MISMATCH", "物理钱包退款证据不一致", http_status=503)
        cursor.execute("UPDATE organization_charge_links SET status=%s,refunded_points=%s,refund_request_ids=%s,refund_tx_ids=%s,overage_points=%s,updated_at=NOW() WHERE id=%s RETURNING *", (status, target, canonical_json(request_ids), canonical_json(refund_ids), new_overage, charge_link_id))
        updated = cursor.fetchone()
        _audit(cursor, context.identity, action="billing.refund", entity_type="organization_charge_link", entity_id=charge_link_id, after={"cumulative_target": target, "delta": delta}, reason=reason)
        return _safe_charge(updated)


def mark_external_side_effect_started(*, charge_link_id: int, claim_token: str,
                                      lease_seconds: int = 1800) -> None:
    """Recheck live authority and renew the lease at every provider boundary.

    This is intentionally repeatable.  Long multi-component packages must not
    rely on the authority snapshot or lease captured before the first image.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM organization_charge_links WHERE id=%s", (charge_link_id,))
        preliminary = cursor.fetchone()
        if not preliminary:
            raise OrganizationError("ORG_CHARGE_NOT_FOUND", "付费记录不存在", http_status=404)
        preliminary_snapshot = _read_work_snapshot(cursor, int(charge_link_id))
        _lock_live_charge_authority(
            cursor,
            preliminary,
            preliminary_snapshot["authority_generation"],
        )
        cursor.execute("SELECT * FROM organization_charge_links WHERE id=%s FOR UPDATE", (charge_link_id,))
        link = cursor.fetchone()
        if not link or not _same_charge_authority_evidence(preliminary, link):
            raise OrganizationError("ORG_CHARGE_EVIDENCE_CHANGED", "付费记录身份范围已变化", http_status=503)
        locked_snapshot = _read_work_snapshot(cursor, int(charge_link_id), for_update=True)
        if payload_hash(locked_snapshot) != payload_hash(preliminary_snapshot):
            raise OrganizationError("ORG_CHARGE_EVIDENCE_CHANGED", "任务权限代际证据已变化", http_status=503)
        if link["status"] != "reserved":
            raise OrganizationError("ORG_CHARGE_NOT_RESERVED", "付费记录当前不能开始外部调用", http_status=409)
        cursor.execute(
            """
            UPDATE organization_work_outbox
            SET execution_started_at=COALESCE(execution_started_at,NOW()),
                external_side_effect_started_at=COALESCE(external_side_effect_started_at,NOW()),
                lease_until=GREATEST(lease_until,NOW()+make_interval(secs=>%s)),status='running',updated_at=NOW()
            WHERE charge_link_id=%s AND claim_token=%s AND status IN ('claimed','running')
              AND lease_until>NOW()
            """,
            (max(30, int(lease_seconds)), charge_link_id, claim_token),
        )
        if cursor.rowcount != 1:
            raise OrganizationError("ORG_WORK_LEASE_LOST", "任务租约已失效，禁止继续外部调用", http_status=409)
        cursor.execute(
            """
            UPDATE organization_charge_links
            SET execution_started_at=COALESCE(execution_started_at,NOW()),
                external_side_effect_started_at=COALESCE(external_side_effect_started_at,NOW()),
                lease_until=GREATEST(lease_until,NOW()+make_interval(secs=>%s)),updated_at=NOW()
            WHERE id=%s AND status='reserved' AND attempt_token=%s
            """,
            (max(30, int(lease_seconds)), charge_link_id, claim_token),
        )
        if cursor.rowcount != 1:
            raise OrganizationError("ORG_CHARGE_NOT_RESERVED", "付费记录当前不能开始外部调用", http_status=409)


def renew_settlement_lease(*, charge_link_id: int, claim_token: str,
                           lease_seconds: int = 1800) -> None:
    """Keep one claimed worker authoritative through commit or release.

    Provider authority has already been checked at every network boundary.
    This guard verifies the still-live claim token and lease only, so a later
    revocation cannot strand settlement for provider work already performed.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        lease = max(30, int(lease_seconds))
        cursor.execute(
            """
            UPDATE organization_work_outbox
            SET lease_until=GREATEST(lease_until,NOW()+make_interval(secs=>%s)),updated_at=NOW()
            WHERE charge_link_id=%s AND claim_token=%s
              AND status IN ('claimed','running') AND lease_until>NOW()
            """,
            (lease, int(charge_link_id), str(claim_token)),
        )
        if cursor.rowcount != 1:
            raise OrganizationError("ORG_WORK_LEASE_LOST", "任务租约已失效，禁止当前 worker 结算", http_status=409)
        cursor.execute(
            """
            UPDATE organization_charge_links
            SET lease_until=GREATEST(lease_until,NOW()+make_interval(secs=>%s)),updated_at=NOW()
            WHERE id=%s AND status='reserved' AND attempt_token=%s AND lease_until>NOW()
            """,
            (lease, int(charge_link_id), str(claim_token)),
        )
        if cursor.rowcount != 1:
            raise OrganizationError("ORG_CHARGE_LEASE_LOST", "付费记录租约已失效，禁止当前 worker 结算", http_status=409)


def billing_consistency(cursor=None) -> dict[str, Any]:
    def inspect(cur) -> dict[str, Any]:
        cur.execute(
            """
            SELECT COUNT(*) AS count FROM organization_charge_links c
            WHERE
              -- Legacy member charges (pre payer-policy evidence) must have legs.
              (c.actor_kind='member' AND c.owner_consent_snapshot IS NULL AND NOT EXISTS(
                SELECT 1 FROM organization_charge_limit_links j WHERE j.charge_link_id=c.id))
              -- Split member charges must carry the locked policy version, and a
              -- live reservation without limit legs must carry a positive
              -- owner-consented overage leg (policy exemption evidence).
              OR (c.actor_kind='member' AND c.owner_consent_snapshot IS NOT NULL AND (
                c.payer_policy_version IS NULL
                OR (c.status='reserved' AND c.overage_points<=0 AND NOT EXISTS(
                  SELECT 1 FROM organization_charge_limit_links j WHERE j.charge_link_id=c.id))))
              -- Owner and system work never consumes the member overage leg.
              OR (c.actor_kind IN ('owner','system') AND (c.overage_points<>0 OR c.payer_policy_version IS NOT NULL))
              OR (c.actor_kind='system' AND EXISTS(
                SELECT 1 FROM organization_charge_limit_links j WHERE j.charge_link_id=c.id))
              -- Overage usage always requires frozen consent evidence.
              OR (c.overage_points>0 AND (c.payer_policy_version IS NULL OR c.owner_consent_snapshot IS NULL))
              OR (c.status='reserved' AND (c.physical_freeze_id IS NULL OR NOT EXISTS(
                SELECT 1 FROM organization_work_outbox o WHERE o.charge_link_id=c.id)))
              OR (c.status='committed' AND EXISTS(
                SELECT 1 FROM organization_charge_limit_links j
                WHERE j.charge_link_id=c.id
                  AND j.consumed_points<>CASE
                    WHEN c.owner_consent_snapshot IS NULL THEN c.actual_points
                    ELSE c.within_limit_points END))
              OR (c.status='released' AND EXISTS(
                SELECT 1 FROM organization_charge_limit_links j
                WHERE j.charge_link_id=c.id AND j.consumed_points<>0))
              OR (c.status='released' AND (c.within_limit_points<>0 OR c.overage_points<>0))
              OR EXISTS(
                SELECT 1 FROM organization_charge_limit_links j
                WHERE j.charge_link_id=c.id
                  AND j.refunded_points<>CASE
                    WHEN c.owner_consent_snapshot IS NULL THEN c.refunded_points
                    ELSE LEAST(c.refunded_points,c.within_limit_points) END)
            """
        )
        inconsistent = int(cur.fetchone()["count"])
        return {"ready": inconsistent == 0, "inconsistent_charge_links": inconsistent}
    if cursor is not None:
        return inspect(cursor)
    with get_db() as conn:
        return inspect(conn.cursor())
