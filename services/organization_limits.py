"""Employee authorization limits with immutable period snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from db.connection import get_db
from services.organization_contract import IdentityContext, OrganizationError
from services.organization_service import _audit, _lock_identity


def _period_bounds(now: datetime, timezone_name: str, period_type: str, *, next_period: bool = False) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        raise OrganizationError("ORG_TIMEZONE_INVALID", "系统配置异常，请联系平台客服", http_status=503) from None
    local = now.astimezone(zone)
    if period_type == "daily":
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if next_period:
            start_local += timedelta(days=1)
        end_local = start_local + timedelta(days=1)
    elif period_type == "monthly":
        start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if next_period:
            if start_local.month == 12:
                start_local = start_local.replace(year=start_local.year + 1, month=1)
            else:
                start_local = start_local.replace(month=start_local.month + 1)
        if start_local.month == 12:
            end_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            end_local = start_local.replace(month=start_local.month + 1)
    else:
        raise OrganizationError("ORG_LIMIT_PERIOD_INVALID", "上限周期类型无效", http_status=422)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _shape(limit_kind: str, feature_code: Optional[str]) -> tuple[str, Optional[str]]:
    if limit_kind in {"daily_total", "daily_feature"}:
        period_type = "daily"
    elif limit_kind in {"monthly_total", "monthly_feature"}:
        period_type = "monthly"
    elif limit_kind == "custom":
        period_type = "custom"
    else:
        raise OrganizationError("ORG_LIMIT_KIND_INVALID", "上限类型无效", http_status=422)
    if limit_kind.endswith("_feature"):
        feature_code = str(feature_code or "").strip()
        if not feature_code:
            raise OrganizationError("ORG_LIMIT_FEATURE_REQUIRED", "按功能设置上限时必须选择功能", http_status=422)
    elif limit_kind != "custom" and feature_code:
        raise OrganizationError("ORG_LIMIT_FEATURE_FORBIDDEN", "总上限不能指定功能", http_status=422)
    return period_type, feature_code


def configure_limit(
    identity: IdentityContext,
    *,
    membership_id: int,
    limit_kind: str,
    limit_points: int,
    reason: str,
    feature_code: Optional[str] = None,
    custom_start: Optional[datetime] = None,
    custom_end: Optional[datetime] = None,
) -> dict[str, Any]:
    """Create/update an unused current period, otherwise version next period."""
    identity = identity
    period_type, feature_code = _shape(limit_kind, feature_code)
    limit_points = int(limit_points)
    if limit_points < 0:
        raise OrganizationError("ORG_LIMIT_NEGATIVE", "使用上限不能为负数", http_status=422)
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写上限设置原因", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            """
            SELECT m.*,o.billing_timezone FROM organization_memberships m
            JOIN organizations o ON o.id=m.organization_id
            WHERE m.id=%s AND m.organization_id=%s FOR UPDATE OF m,o
            """,
            (membership_id, identity.organization_id),
        )
        member = cursor.fetchone()
        if not member or member["is_owner"] or member["status"] not in {"active", "suspended"}:
            raise OrganizationError("ORG_MEMBER_INVALID", "员工不存在或当前不可设置上限", http_status=422)
        now = datetime.now(timezone.utc)
        if period_type == "custom":
            if not custom_start or not custom_end or custom_end <= custom_start or custom_end <= now:
                raise OrganizationError("ORG_LIMIT_CUSTOM_WINDOW_INVALID", "自定义上限的起止时间无效", http_status=422)
            period_start = custom_start.astimezone(timezone.utc)
            period_end = custom_end.astimezone(timezone.utc)
        else:
            period_start, period_end = _period_bounds(now, member["billing_timezone"], period_type)
        cursor.execute(
            """
            SELECT * FROM organization_spend_limits
            WHERE organization_id=%s AND membership_id=%s AND limit_kind=%s
              AND period_start=%s AND COALESCE(feature_code,'')=COALESCE(%s,'')
            FOR UPDATE
            """,
            (identity.organization_id, membership_id, limit_kind, period_start, feature_code),
        )
        existing = cursor.fetchone()
        effective = "current"
        if existing and (int(existing["reserved_points"]) or int(existing["consumed_points"]) or int(existing["refunded_points"])):
            if period_type == "custom":
                raise OrganizationError("ORG_LIMIT_IN_USE", "已开始使用的自定义上限不能修改，请新建一条", http_status=409)
            period_start, period_end = _period_bounds(now, member["billing_timezone"], period_type, next_period=True)
            effective = "next_period"
            cursor.execute(
                """
                SELECT * FROM organization_spend_limits
                WHERE organization_id=%s AND membership_id=%s AND limit_kind=%s
                  AND period_start=%s AND COALESCE(feature_code,'')=COALESCE(%s,'') FOR UPDATE
                """,
                (identity.organization_id, membership_id, limit_kind, period_start, feature_code),
            )
            existing = cursor.fetchone()
        cursor.execute(
            """
            SELECT COALESCE(MAX(policy_version),0)+1 AS next_version
            FROM organization_spend_limits
            WHERE organization_id=%s AND membership_id=%s AND limit_kind=%s
              AND COALESCE(feature_code,'')=COALESCE(%s,'')
            """,
            (identity.organization_id, membership_id, limit_kind, feature_code),
        )
        policy_version = int(cursor.fetchone()["next_version"])
        if limit_kind.endswith("_feature"):
            total_kind = "daily_total" if limit_kind.startswith("daily") else "monthly_total"
            cursor.execute(
                """
                SELECT limit_points FROM organization_spend_limits
                WHERE organization_id=%s AND membership_id=%s AND limit_kind=%s
                  AND period_start=%s AND feature_code IS NULL
                """,
                (identity.organization_id, membership_id, total_kind, period_start),
            )
            total = cursor.fetchone()
            if not total:
                raise OrganizationError("ORG_LIMIT_TOTAL_REQUIRED", "请先配置同周期的总上限", http_status=409)
            if limit_points > int(total["limit_points"]):
                raise OrganizationError("ORG_LIMIT_FEATURE_EXCEEDS_TOTAL", "功能上限不能大于同周期总上限", http_status=422)
        if existing:
            cursor.execute(
                """
                UPDATE organization_spend_limits
                SET limit_points=%s,policy_version=%s,version=version+1,updated_by_user_id=%s,
                    reason=%s,updated_at=NOW()
                WHERE id=%s RETURNING *
                """,
                (limit_points, policy_version, identity.actor_user_id, reason, existing["id"]),
            )
        else:
            cursor.execute(
                """
                INSERT INTO organization_spend_limits(
                  organization_id,membership_id,period_type,limit_kind,period_start,period_end,
                  period_timezone,feature_code,limit_points,status,policy_version,updated_by_user_id,reason
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s) RETURNING *
                """,
                (
                    identity.organization_id, membership_id, period_type, limit_kind,
                    period_start, period_end, member["billing_timezone"], feature_code,
                    limit_points, policy_version, identity.actor_user_id, reason,
                ),
            )
        row = dict(cursor.fetchone())
        row["effective"] = effective
        _audit(cursor, identity, action="limit.configure", entity_type="organization_spend_limit", entity_id=row["id"], after={key: row[key] for key in ("membership_id", "limit_kind", "feature_code", "limit_points", "period_start", "period_end", "policy_version")}, reason=reason)
        return row


def list_limits(identity: IdentityContext, *, membership_id: Optional[int] = None) -> list[dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        if not identity.is_owner:
            membership_id = identity.membership_id
        cursor.execute(
            """
            SELECT * FROM organization_spend_limits
            WHERE organization_id=%s AND (%s IS NULL OR membership_id=%s)
            ORDER BY membership_id,period_start DESC,limit_kind,feature_code NULLS FIRST,id
            """,
            (identity.organization_id, membership_id, membership_id),
        )
        return [dict(row) for row in cursor.fetchall()]


def _roll_period(cursor, *, organization_id: int, membership_id: int, kind: str, feature_code: Optional[str], timezone_name: str, now: datetime) -> Mapping[str, Any]:
    period_type = "daily" if kind.startswith("daily") else "monthly"
    start, end = _period_bounds(now, timezone_name, period_type)
    cursor.execute(
        """
        SELECT * FROM organization_spend_limits
        WHERE organization_id=%s AND membership_id=%s AND limit_kind=%s
          AND period_start=%s AND COALESCE(feature_code,'')=COALESCE(%s,'')
        FOR UPDATE
        """,
        (organization_id, membership_id, kind, start, feature_code),
    )
    current = cursor.fetchone()
    if current:
        return current
    cursor.execute(
        """
        SELECT * FROM organization_spend_limits
        WHERE organization_id=%s AND membership_id=%s AND limit_kind=%s
          AND COALESCE(feature_code,'')=COALESCE(%s,'') AND period_start<%s
        ORDER BY period_start DESC,id DESC LIMIT 1
        """,
        (organization_id, membership_id, kind, feature_code, start),
    )
    source = cursor.fetchone()
    if not source:
        raise OrganizationError("ORG_LIMIT_POLICY_MISSING", "员工使用上限尚未完整配置", http_status=409, safe_details={"limit_kind": kind, "feature_code": feature_code})
    cursor.execute(
        """
        INSERT INTO organization_spend_limits(
          organization_id,membership_id,period_type,limit_kind,period_start,period_end,
          period_timezone,feature_code,limit_points,status,policy_version,updated_by_user_id,reason
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,'automatic period rollover')
        ON CONFLICT DO NOTHING RETURNING *
        """,
        (
            organization_id,membership_id,period_type,kind,start,end,timezone_name,
            feature_code,source["limit_points"],source["policy_version"],source["updated_by_user_id"],
        ),
    )
    created = cursor.fetchone()
    if created:
        return created
    cursor.execute(
        """
        SELECT * FROM organization_spend_limits
        WHERE organization_id=%s AND membership_id=%s AND limit_kind=%s
          AND period_start=%s AND COALESCE(feature_code,'')=COALESCE(%s,'') FOR UPDATE
        """,
        (organization_id,membership_id,kind,start,feature_code),
    )
    return cursor.fetchone()


def _lock_applicable_limit_rows(
    cursor,
    identity: IdentityContext,
    *,
    feature_code: str,
) -> list[dict[str, Any]]:
    """Resolve and lock every applicable bucket after the payer wallet is locked."""
    cursor.execute("SELECT billing_timezone FROM organizations WHERE id=%s", (identity.organization_id,))
    org = cursor.fetchone()
    if not org:
        raise OrganizationError("ORG_NOT_FOUND", "团队不存在", http_status=404)
    now = datetime.now(timezone.utc)
    required = [
        _roll_period(cursor, organization_id=identity.organization_id, membership_id=identity.membership_id, kind="daily_total", feature_code=None, timezone_name=org["billing_timezone"], now=now),
        _roll_period(cursor, organization_id=identity.organization_id, membership_id=identity.membership_id, kind="monthly_total", feature_code=None, timezone_name=org["billing_timezone"], now=now),
    ]
    for kind in ("daily_feature", "monthly_feature"):
        cursor.execute(
            """
            SELECT EXISTS(SELECT 1 FROM organization_spend_limits
              WHERE organization_id=%s AND membership_id=%s AND limit_kind=%s AND feature_code=%s)
              AS configured
            """,
            (identity.organization_id, identity.membership_id, kind, feature_code),
        )
        if cursor.fetchone()["configured"]:
            required.append(_roll_period(cursor, organization_id=identity.organization_id, membership_id=identity.membership_id, kind=kind, feature_code=feature_code, timezone_name=org["billing_timezone"], now=now))
    cursor.execute(
        """
        SELECT * FROM organization_spend_limits
        WHERE organization_id=%s AND membership_id=%s AND limit_kind='custom'
          AND status='open' AND period_start<=%s AND period_end>%s
          AND (feature_code IS NULL OR feature_code=%s)
        ORDER BY id FOR UPDATE
        """,
        (identity.organization_id, identity.membership_id, now, now, feature_code),
    )
    required.extend(cursor.fetchall())
    ids = [int(row["id"]) for row in required]
    if len(ids) != len(set(ids)):
        raise OrganizationError("ORG_LIMIT_AMBIGUOUS", "员工使用上限配置重复，请联系平台客服", http_status=503)
    cursor.execute("SELECT * FROM organization_spend_limits WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (sorted(ids),))
    locked = [dict(row) for row in cursor.fetchall()]
    if {int(row["id"]) for row in locked} != set(ids):
        raise OrganizationError("ORG_LIMIT_CHANGED", "员工使用上限已变化，请重试", http_status=409, retryable=True)
    for row in locked:
        if row["status"] != "open" or not (row["period_start"] <= now < row["period_end"]):
            raise OrganizationError("ORG_LIMIT_PERIOD_CLOSED", "员工使用上限周期已关闭", http_status=409, retryable=True)
    return locked


def _bucket_available(row: Mapping[str, Any]) -> int:
    return int(row["limit_points"]) - int(row["reserved_points"]) - int(row["consumed_points"]) + int(row["refunded_points"])


def lock_applicable_limits(
    cursor,
    identity: IdentityContext,
    *,
    feature_code: str,
    reservation_points: int,
) -> list[dict[str, Any]]:
    """Resolve every applicable bucket after the payer wallet is locked."""
    if not identity.is_member:
        return []
    locked = _lock_applicable_limit_rows(cursor, identity, feature_code=feature_code)
    for row in locked:
        available = _bucket_available(row)
        if int(reservation_points) > available:
            raise OrganizationError(
                "ORG_SPEND_LIMIT_EXCEEDED",
                "本次操作超过员工使用上限，请联系团队负责人调整上限",
                http_status=402,
                safe_details={"limit_kind": row["limit_kind"], "available_points": max(available, 0)},
            )
        if row["limit_kind"] == "custom" and int(reservation_points) > int(row["limit_points"]):
            raise OrganizationError("ORG_PER_TASK_LIMIT_EXCEEDED", "本次操作超过单次上限", http_status=402)
    return locked


def lock_applicable_limits_split(
    cursor,
    identity: IdentityContext,
    *,
    feature_code: str,
    reservation_points: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Split one member reservation into within-limit and payer-overage legs.

    The within-limit leg is the largest portion that still fits every locked
    bucket (and every custom per-task cap); the remainder is returned as the
    payer overage leg and is validated against the owner payer policy by the
    caller. Buckets are locked but not mutated here.
    """
    if not identity.is_member:
        return [], int(reservation_points), 0
    locked = _lock_applicable_limit_rows(cursor, identity, feature_code=feature_code)
    within = int(reservation_points)
    for row in locked:
        within = min(within, max(_bucket_available(row), 0))
        if row["limit_kind"] == "custom":
            within = min(within, int(row["limit_points"]))
    within = max(within, 0)
    return locked, within, int(reservation_points) - within
