"""Owner-approved automatic plans with frozen cadence, budget, and occurrences."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from db.connection import get_db
from db.wallet_db import get_feature_pricing
from services.organization_billing import FEATURE_CAPABILITIES, _pricing_snapshot, release_charge, reserve_charge
from services.organization_contract import IdentityContext, OrganizationError, canonical_json, payload_hash, stable_key
from services.organization_service import _audit, _lock_identity


SUPPORTED_AUTOMATIC_WORK = {("monitor_single", "monitoring.run")}


def _normalize_plan_payload(
    *,
    feature_code: str,
    work_kind: str,
    payload: Mapping[str, Any],
    brand_id: Optional[int],
) -> dict[str, Any]:
    if (feature_code, work_kind) not in SUPPORTED_AUTOMATIC_WORK:
        raise OrganizationError(
            "ORG_PLAN_WORK_UNSUPPORTED",
            "该功能暂不支持自动计划",
            http_status=422,
        )
    if brand_id is None or int(brand_id) <= 0:
        raise OrganizationError("ORG_PLAN_BRAND_REQUIRED", "自动监测计划必须选择客户", http_status=422)
    allowed = {"brand_id", "keyword_keys", "platforms", "concurrency", "search_mode"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise OrganizationError(
            "ORG_PLAN_PAYLOAD_UNCLASSIFIED",
            "计划参数无效，请检查后重试",
            http_status=422,
            safe_details={"fields": unknown},
        )
    payload_brand = payload.get("brand_id")
    if payload_brand is not None and int(payload_brand) != int(brand_id):
        raise OrganizationError("ORG_PLAN_PAYLOAD_BRAND_MISMATCH", "计划里的客户信息不一致，请重新创建", http_status=422)
    keyword_keys = payload.get("keyword_keys") or []
    platforms = payload.get("platforms") or []
    if not isinstance(keyword_keys, list) or len(keyword_keys) > 500 or not all(
        isinstance(value, str) and 1 <= len(value) <= 120 for value in keyword_keys
    ):
        raise OrganizationError("ORG_PLAN_KEYWORD_SCOPE_INVALID", "监测词范围无效", http_status=422)
    if not isinstance(platforms, list) or len(platforms) > 10 or not all(
        isinstance(value, str) and 1 <= len(value) <= 40 for value in platforms
    ):
        raise OrganizationError("ORG_PLAN_PLATFORM_SCOPE_INVALID", "所选平台无效", http_status=422)
    concurrency = payload.get("concurrency")
    if concurrency is not None and (not isinstance(concurrency, int) or isinstance(concurrency, bool) or not 1 <= concurrency <= 50):
        raise OrganizationError("ORG_PLAN_CONCURRENCY_INVALID", "计划参数无效，请检查后重试", http_status=422)
    search_mode = str(payload.get("search_mode") or "enhanced")
    if search_mode not in {"enhanced", "standard", "auto"}:
        raise OrganizationError("ORG_PLAN_SEARCH_MODE_INVALID", "所选搜索方式无效", http_status=422)
    return {
        "brand_id": int(brand_id),
        "keyword_keys": sorted(set(keyword_keys)),
        "platforms": sorted(set(platforms)),
        "concurrency": concurrency,
        "search_mode": search_mode,
    }


def create_plan(
    identity: IdentityContext,
    *,
    request_id: str,
    feature_code: str,
    work_kind: str,
    payload: Mapping[str, Any],
    cadence_seconds: int,
    max_occurrences: int,
    total_budget_points: int,
    max_occurrence_points: int,
    starts_at: datetime,
    ends_at: datetime,
    brand_id: Optional[int] = None,
) -> dict[str, Any]:
    if feature_code not in FEATURE_CAPABILITIES:
        raise OrganizationError("ORG_FEATURE_UNCLASSIFIED", "该功能暂不支持自动计划", http_status=503)
    normalized_payload = _normalize_plan_payload(
        feature_code=feature_code,
        work_kind=work_kind,
        payload=payload,
        brand_id=brand_id,
    )
    now = datetime.now(timezone.utc)
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise OrganizationError("ORG_PLAN_TIMEZONE_REQUIRED", "计划时间无效，请重新选择", http_status=422)
    starts_at = starts_at.astimezone(timezone.utc)
    ends_at = ends_at.astimezone(timezone.utc)
    if cadence_seconds < 60 or max_occurrences <= 0 or ends_at <= starts_at or ends_at <= now:
        raise OrganizationError("ORG_PLAN_SCHEDULE_INVALID", "自动计划时间或次数无效", http_status=422)
    if total_budget_points <= 0 or max_occurrence_points <= 0 or max_occurrence_points > total_budget_points:
        raise OrganizationError("ORG_PLAN_BUDGET_INVALID", "自动计划预算无效", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-plan:{identity.organization_id}:{request_id}",),
        )
        identity = _lock_identity(cursor, identity, owner_required=True)
        identity.require_active_organization()
        cursor.execute("SELECT * FROM organization_automatic_plans WHERE organization_id=%s AND request_id=%s FOR UPDATE", (identity.organization_id, request_id))
        replay = cursor.fetchone()
        digest = payload_hash(
            {
                "feature_code": feature_code,
                "work_kind": work_kind,
                "payload": normalized_payload,
                "cadence_seconds": int(cadence_seconds),
                "max_occurrences": int(max_occurrences),
                "total_budget_points": int(total_budget_points),
                "max_occurrence_points": int(max_occurrence_points),
                "starts_at": starts_at,
                "ends_at": ends_at,
                "brand_id": int(brand_id),
            }
        )
        if replay:
            if replay["payload_hash"] != digest:
                raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "操作冲突，请刷新后重试", http_status=409)
            return dict(replay)
        pricing = get_feature_pricing(feature_code, cursor=cursor)
        base_cost = int(pricing["cost_points"])
        if base_cost <= 0 or max_occurrence_points < base_cost:
            raise OrganizationError("ORG_PLAN_COST_CEILING_INVALID", "单次预算低于该功能一次执行所需的最低算力，请调高单次上限", http_status=422)
        pricing_version, pricing_hash = _pricing_snapshot(pricing, max_occurrence_points)
        if brand_id is not None:
            cursor.execute("SELECT 1 FROM brands WHERE id=%s AND owner_user_id=%s AND (is_deleted IS NULL OR is_deleted=FALSE) FOR UPDATE", (brand_id, identity.principal_user_id))
            if not cursor.fetchone():
                raise OrganizationError("ORG_BRAND_NOT_OWNED", "所选客户不在你的团队名下", http_status=403)
        first_at = max(starts_at, now)
        cursor.execute(
            """
            INSERT INTO organization_automatic_plans(
              organization_id,request_id,feature_code,work_kind,brand_id,payload_hash,payload_snapshot,
              cadence_seconds,max_occurrences,total_budget_points,max_occurrence_points,starts_at,ends_at,
              next_occurrence_at,status,pricing_version,pricing_snapshot_hash,approved_by_user_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s)
            RETURNING *
            """,
            (
                identity.organization_id,request_id,feature_code,work_kind,brand_id,digest,
                canonical_json(normalized_payload),cadence_seconds,max_occurrences,total_budget_points,
                max_occurrence_points,starts_at,ends_at,first_at,pricing_version,pricing_hash,
                identity.actor_user_id,
            ),
        )
        plan = cursor.fetchone()
        _audit(cursor, identity, action="automatic_plan.create", entity_type="organization_automatic_plan", entity_id=plan["id"], after={"feature_code": feature_code, "cadence_seconds": cadence_seconds, "max_occurrences": max_occurrences, "total_budget_points": total_budget_points, "max_occurrence_points": max_occurrence_points, "starts_at": starts_at, "ends_at": ends_at})
        return dict(plan)


def list_plans(identity: IdentityContext) -> list[dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        if not identity.is_owner:
            return []
        cursor.execute("SELECT * FROM organization_automatic_plans WHERE organization_id=%s ORDER BY created_at DESC,id DESC", (identity.organization_id,))
        return [dict(row) for row in cursor.fetchall()]


def cancel_brand_plans_for_soft_delete(
    cursor,
    *,
    brand_id: int,
    owner_user_id: int,
    request_id: str,
) -> list[int]:
    """Cancel a deleted brand's plans and expose unstarted charges to recovery.

    The caller owns the transaction that also tombstones the brand.  Physical
    wallet/limit release happens after commit through ``release_charge``; every
    selected outbox is first made immediately recoverable so a process death in
    that handoff window cannot strand a plan budget hold.
    """
    cursor.execute(
        "SELECT id,status FROM organizations WHERE owner_user_id=%s FOR UPDATE",
        (int(owner_user_id),),
    )
    organization = cursor.fetchone()
    if not organization:
        return []
    cursor.execute(
        """
        SELECT * FROM organization_automatic_plans
        WHERE organization_id=%s AND brand_id=%s AND status IN ('active','paused')
        ORDER BY id FOR UPDATE
        """,
        (organization["id"], int(brand_id)),
    )
    plans = list(cursor.fetchall())
    if not plans:
        return []
    plan_ids = [int(plan["id"]) for plan in plans]

    cursor.execute(
        """
        UPDATE organization_plan_occurrences
        SET status='released',last_error_code='BRAND_SOFT_DELETED',updated_at=NOW()
        WHERE plan_id=ANY(%s) AND status='retry_wait' AND charge_link_id IS NULL
        """,
        (plan_ids,),
    )
    cursor.execute(
        """
        SELECT id,plan_id,reserved_ceiling_points
        FROM organization_plan_occurrences
        WHERE plan_id=ANY(%s) AND status='scheduled' AND charge_link_id IS NULL
        ORDER BY plan_id,id FOR UPDATE
        """,
        (plan_ids,),
    )
    unbound_by_plan: dict[int, dict[str, int]] = {}
    for row in cursor.fetchall():
        aggregate = unbound_by_plan.setdefault(
            int(row["plan_id"]),
            {"occurrence_count": 0, "released_points": 0},
        )
        aggregate["occurrence_count"] += 1
        aggregate["released_points"] += int(row["reserved_ceiling_points"])
    if unbound_by_plan:
        cursor.execute(
            """
            UPDATE organization_plan_occurrences
            SET status='released',last_error_code='BRAND_SOFT_DELETED',updated_at=NOW()
            WHERE plan_id=ANY(%s) AND status='scheduled' AND charge_link_id IS NULL
            """,
            (plan_ids,),
        )
        for plan_id, row in unbound_by_plan.items():
            cursor.execute(
                """
                UPDATE organization_automatic_plans
                SET reserved_budget_points=GREATEST(0,reserved_budget_points-%s),
                    scheduled_occurrences=GREATEST(0,scheduled_occurrences-%s),
                    version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (int(row["released_points"]), int(row["occurrence_count"]), plan_id),
            )

    cursor.execute(
        """
        SELECT c.id
        FROM organization_plan_occurrences x
        JOIN organization_charge_links c ON c.id=x.charge_link_id
        JOIN organization_work_outbox o ON o.charge_link_id=c.id
        WHERE x.plan_id=ANY(%s) AND c.status='reserved'
          AND c.external_side_effect_started_at IS NULL
          AND o.external_side_effect_started_at IS NULL
        ORDER BY c.id FOR UPDATE OF c,o
        """,
        (plan_ids,),
    )
    release_ids = [int(row["id"]) for row in cursor.fetchall()]
    if release_ids:
        cursor.execute(
            """
            UPDATE organization_work_outbox
            SET dispatch_deadline=NOW()-INTERVAL '1 second',
                lease_until=CASE WHEN lease_until IS NULL THEN NULL ELSE NOW()-INTERVAL '1 second' END,
                last_error_code='BRAND_SOFT_DELETED',updated_at=NOW()
            WHERE charge_link_id=ANY(%s)
              AND external_side_effect_started_at IS NULL
            """,
            (release_ids,),
        )

    cursor.execute(
        """
        UPDATE organization_automatic_plans
        SET status='cancelled',version=version+1,updated_at=NOW()
        WHERE id=ANY(%s)
        """,
        (plan_ids,),
    )
    system_identity = IdentityContext(
        request_id=request_id,
        authenticated_user_id=int(owner_user_id),
        principal_user_id=int(owner_user_id),
        payer_user_id=int(owner_user_id),
        actor_kind="system",
        organization_id=int(organization["id"]),
        requested_by_user_id=int(owner_user_id),
    )
    for plan in plans:
        _audit(
            cursor,
            system_identity,
            action="automatic_plan.cancel_brand_soft_delete",
            entity_type="organization_automatic_plan",
            entity_id=plan["id"],
            before={"status": plan["status"]},
            after={"status": "cancelled", "brand_id": int(brand_id)},
            reason="brand_soft_deleted",
        )
    return release_ids


async def set_plan_status(identity: IdentityContext, *, plan_id: int, status: str, expected_version: int) -> dict[str, Any]:
    if status not in {"active", "paused", "cancelled"}:
        raise OrganizationError("ORG_PLAN_STATUS_INVALID", "自动计划状态无效", http_status=422)
    charge_ids_to_release: list[int] = []
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        if status == "active":
            identity.require_active_organization()
        cursor.execute("SELECT * FROM organization_automatic_plans WHERE id=%s AND organization_id=%s FOR UPDATE", (plan_id, identity.organization_id))
        plan = cursor.fetchone()
        if not plan:
            raise OrganizationError("ORG_PLAN_NOT_FOUND", "自动计划不存在", http_status=404)
        if int(plan["version"]) != int(expected_version):
            raise OrganizationError("ORG_VERSION_CONFLICT", "自动计划已变化，请刷新", http_status=409, retryable=True)
        if plan["status"] in {"completed", "cancelled"}:
            raise OrganizationError("ORG_PLAN_TERMINAL", "已结束的自动计划不能修改", http_status=409)
        if status == "cancelled":
            # retry_wait occurrences no longer hold budget/count, but must be
            # made terminal so a future resume/scheduler can never resurrect
            # work after the owner cancelled the plan.
            cursor.execute(
                """
                UPDATE organization_plan_occurrences
                SET status='released',last_error_code='PLAN_CANCELLED',updated_at=NOW()
                WHERE plan_id=%s AND status='retry_wait' AND charge_link_id IS NULL
                """,
                (plan_id,),
            )
            cursor.execute(
                """
                SELECT id,reserved_ceiling_points FROM organization_plan_occurrences
                WHERE plan_id=%s AND status='scheduled' AND charge_link_id IS NULL
                ORDER BY id FOR UPDATE
                """,
                (plan_id,),
            )
            unbound = list(cursor.fetchall())
            if unbound:
                ids = [int(row["id"]) for row in unbound]
                released_hold = sum(int(row["reserved_ceiling_points"]) for row in unbound)
                cursor.execute(
                    """
                    UPDATE organization_plan_occurrences
                    SET status='released',last_error_code='PLAN_CANCELLED',updated_at=NOW()
                    WHERE id=ANY(%s)
                    """,
                    (ids,),
                )
                cursor.execute(
                    """
                    UPDATE organization_automatic_plans
                    SET reserved_budget_points=reserved_budget_points-%s,
                        scheduled_occurrences=scheduled_occurrences-%s
                    WHERE id=%s
                    """,
                    (released_hold, len(unbound), plan_id),
                )
            cursor.execute(
                """
                SELECT x.charge_link_id
                FROM organization_plan_occurrences x
                JOIN organization_charge_links c ON c.id=x.charge_link_id
                JOIN organization_work_outbox o ON o.charge_link_id=c.id
                WHERE x.plan_id=%s AND x.status IN ('reserved','running')
                  AND x.charge_link_id IS NOT NULL AND c.status='reserved'
                  AND c.external_side_effect_started_at IS NULL
                  AND o.external_side_effect_started_at IS NULL
                ORDER BY x.id FOR UPDATE OF x,c,o
                """,
                (plan_id,),
            )
            charge_ids_to_release = [int(row["charge_link_id"]) for row in cursor.fetchall()]
        cursor.execute("UPDATE organization_automatic_plans SET status=%s,version=version+1,updated_at=NOW() WHERE id=%s RETURNING *", (status, plan_id))
        updated = cursor.fetchone()
        _audit(cursor, identity, action=f"automatic_plan.{status}", entity_type="organization_automatic_plan", entity_id=plan_id, before={"status": plan["status"]}, after={"status": updated["status"]})
    release_failures = []
    for charge_id in charge_ids_to_release:
        try:
            await release_charge(charge_link_id=charge_id, reason="automatic plan cancelled")
        except Exception as exc:
            release_failures.append({"charge_link_id": charge_id, "error_type": type(exc).__name__})
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM organization_automatic_plans WHERE id=%s", (plan_id,))
        result = dict(cursor.fetchone())
    if release_failures:
        result["release_pending"] = release_failures
    return result


def _claim_or_create_occurrence() -> Optional[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT o.*,p.organization_id,p.feature_code,p.work_kind,p.brand_id,p.payload_snapshot,
                   p.max_occurrence_points,p.approved_by_user_id,p.pricing_version,p.pricing_snapshot_hash
                   ,p.cadence_seconds
            FROM organization_plan_occurrences o
            JOIN organization_automatic_plans p ON p.id=o.plan_id
            WHERE o.status='retry_wait' AND o.charge_link_id IS NULL
              AND o.next_attempt_at<=NOW() AND p.status='active' AND p.ends_at>NOW()
              AND p.scheduled_occurrences<p.max_occurrences
              AND p.reserved_budget_points+p.consumed_budget_points-p.refunded_budget_points
                    +o.reserved_ceiling_points<=p.total_budget_points
            ORDER BY o.next_attempt_at,o.id FOR UPDATE OF o,p SKIP LOCKED LIMIT 1
            """
        )
        retry = cursor.fetchone()
        if retry:
            cursor.execute(
                """
                UPDATE organization_plan_occurrences
                SET status='scheduled',reservation_attempts=reservation_attempts+1,
                    last_error_code=NULL,updated_at=NOW()
                WHERE id=%s RETURNING *
                """,
                (retry["id"],),
            )
            occurrence = dict(cursor.fetchone())
            cursor.execute(
                """
                UPDATE organization_automatic_plans
                SET scheduled_occurrences=scheduled_occurrences+1,
                    reserved_budget_points=reserved_budget_points+%s,
                    next_occurrence_at=GREATEST(next_occurrence_at,%s),
                    version=version+1,updated_at=NOW()
                WHERE id=%s
                """,
                (
                    retry["reserved_ceiling_points"],
                    retry["planned_at"] + timedelta(seconds=int(retry["cadence_seconds"])),
                    retry["plan_id"],
                ),
            )
            occurrence.update({key: retry[key] for key in ("organization_id", "feature_code", "work_kind", "brand_id", "payload_snapshot", "max_occurrence_points", "approved_by_user_id", "pricing_version", "pricing_snapshot_hash")})
            return occurrence
        cursor.execute(
            """
            SELECT o.*,p.organization_id,p.feature_code,p.work_kind,p.brand_id,p.payload_snapshot,
                   p.max_occurrence_points,p.approved_by_user_id,p.pricing_version,p.pricing_snapshot_hash
            FROM organization_plan_occurrences o
            JOIN organization_automatic_plans p ON p.id=o.plan_id
            WHERE o.status='scheduled' AND o.charge_link_id IS NULL AND p.status='active'
            ORDER BY o.planned_at,o.id FOR UPDATE OF o,p SKIP LOCKED LIMIT 1
            """
        )
        existing = cursor.fetchone()
        if existing:
            return dict(existing)
        cursor.execute(
            """
            SELECT * FROM organization_automatic_plans
            WHERE status='active' AND next_occurrence_at<=NOW() AND next_occurrence_at<ends_at
              AND scheduled_occurrences<max_occurrences
              AND reserved_budget_points+consumed_budget_points-refunded_budget_points+max_occurrence_points<=total_budget_points
              AND NOT EXISTS (
                SELECT 1 FROM organization_plan_occurrences retry
                WHERE retry.plan_id=organization_automatic_plans.id
                  AND retry.status='retry_wait'
              )
            ORDER BY next_occurrence_at,id FOR UPDATE SKIP LOCKED LIMIT 1
            """
        )
        plan = cursor.fetchone()
        if not plan:
            return None
        planned_at = plan["next_occurrence_at"]
        occurrence_key = stable_key("organization-plan", plan["id"], planned_at.isoformat())
        cursor.execute(
            """
            INSERT INTO organization_plan_occurrences(
              plan_id,occurrence_key,planned_at,reserved_ceiling_points,status,reservation_attempts
            ) VALUES (%s,%s,%s,%s,'scheduled',1) ON CONFLICT(occurrence_key) DO NOTHING RETURNING *
            """,
            (plan["id"], occurrence_key, planned_at, plan["max_occurrence_points"]),
        )
        occurrence = cursor.fetchone()
        if not occurrence:
            return None
        next_at = planned_at + timedelta(seconds=int(plan["cadence_seconds"]))
        cursor.execute(
            """
            UPDATE organization_automatic_plans
            SET scheduled_occurrences=scheduled_occurrences+1,
                reserved_budget_points=reserved_budget_points+max_occurrence_points,
                next_occurrence_at=%s,
                version=version+1,updated_at=NOW()
            WHERE id=%s
            """,
            (next_at, plan["id"]),
        )
        result = dict(occurrence)
        result.update({key: plan[key] for key in ("organization_id", "feature_code", "work_kind", "brand_id", "payload_snapshot", "max_occurrence_points", "approved_by_user_id", "pricing_version", "pricing_snapshot_hash")})
        return result


def _defer_unbound_occurrence(occurrence_id: int, error_code: str) -> None:
    """Release the plan-level hold after reservation did not become durable."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT o.*,p.status AS plan_status,p.ends_at FROM organization_plan_occurrences o
            JOIN organization_automatic_plans p ON p.id=o.plan_id
            WHERE o.id=%s FOR UPDATE OF o,p
            """,
            (occurrence_id,),
        )
        row = cursor.fetchone()
        if not row or row["charge_link_id"] is not None or row["status"] != "scheduled":
            return
        retry_at = datetime.now(timezone.utc) + timedelta(
            seconds=min(3600, 30 * (2 ** min(int(row.get("reservation_attempts") or 1), 7)))
        )
        terminal = row["plan_status"] == "cancelled" or datetime.now(timezone.utc) >= row["ends_at"]
        cursor.execute(
            """
            UPDATE organization_plan_occurrences
            SET status=%s,next_attempt_at=%s,last_error_code=%s,updated_at=NOW()
            WHERE id=%s
            """,
            ("released" if terminal else "retry_wait", retry_at, error_code[:120], occurrence_id),
        )
        cursor.execute(
            """
            UPDATE organization_automatic_plans
            SET reserved_budget_points=reserved_budget_points-%s,
                scheduled_occurrences=scheduled_occurrences-1,
                next_occurrence_at=LEAST(next_occurrence_at,%s),
                version=version+1,updated_at=NOW()
            WHERE id=%s
            """,
            (row["reserved_ceiling_points"], row["planned_at"], row["plan_id"]),
        )


async def schedule_one_due_occurrence() -> Optional[dict[str, Any]]:
    occurrence = _claim_or_create_occurrence()
    if not occurrence:
        return None
    try:
        payload = occurrence["payload_snapshot"]
        if isinstance(payload, str):
            payload = __import__("json").loads(payload)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT owner_user_id,version,authority_version,status FROM organizations WHERE id=%s", (occurrence["organization_id"],))
            org = cursor.fetchone()
            if not org or org["status"] != "active":
                raise OrganizationError("ORG_INACTIVE", "团队当前状态不可执行自动计划", http_status=403)
        system_identity = IdentityContext(
            request_id=occurrence["occurrence_key"],
            authenticated_user_id=int(org["owner_user_id"]),
            principal_user_id=int(org["owner_user_id"]),
            payer_user_id=int(org["owner_user_id"]),
            actor_kind="system",
            organization_id=int(occurrence["organization_id"]),
            requested_by_user_id=int(occurrence["approved_by_user_id"]),
            organization_version=int(org["version"]),
            organization_authority_version=int(org["authority_version"]),
        )
        pricing = get_feature_pricing(occurrence["feature_code"])
        extra = int(occurrence["max_occurrence_points"]) - int(pricing["cost_points"])
        attempt_execution_id = stable_key(
            "organization-plan-attempt",
            occurrence["occurrence_key"],
            int(occurrence.get("reservation_attempts") or 1),
        )
        charge = await reserve_charge(
            system_identity,
            execution_id=attempt_execution_id,
            feature_code=occurrence["feature_code"],
            work_kind=occurrence["work_kind"],
            payload=payload,
            brand_id=occurrence["brand_id"],
            task_ref=f"plan-occurrence:{occurrence['id']}",
            dynamic_ceiling_extra_points=extra,
            automatic_plan_occurrence_id=int(occurrence["id"]),
        )
    except Exception as exc:
        _defer_unbound_occurrence(int(occurrence["id"]), type(exc).__name__)
        raise
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT x.*,c.status AS charge_status
            FROM organization_plan_occurrences x
            LEFT JOIN organization_charge_links c ON c.id=x.charge_link_id
            WHERE x.id=%s FOR UPDATE OF x
            """,
            (occurrence["id"],),
        )
        live = cursor.fetchone()
        if live["charge_link_id"] is None or int(live["charge_link_id"]) != int(charge["id"]):
            raise OrganizationError("ORG_PLAN_OCCURRENCE_CONFLICT", "本次自动执行已在处理中，请勿重复操作", http_status=503)
        if live["status"] != "reserved" or live["charge_status"] != "reserved":
            raise OrganizationError(
                "ORG_PLAN_OCCURRENCE_NOT_RESERVED",
                "本次自动执行已取消或已失效",
                http_status=409,
            )
        result = dict(live)
        result.pop("charge_status", None)
        result["charge"] = charge
        return result
