"""Owner-consented shared payer policy SSOT, audit, and overage cap accounting.

Every member billed action reads this layer after the organization row lock.
A missing row means fully disabled: existing organizations never gain payer
consent implicitly, and only the organization owner may enable or raise it.
Platform ADMIN may only emergency-disable, never enable or raise.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Mapping, Optional

from db.connection import get_db
from services.organization_contract import (
    IdentityContext,
    OrganizationError,
    canonical_json,
    feature_flags,
    payload_hash,
    require_feature_flag,
    stable_key,
)
from services.organization_limits import _period_bounds
from services.organization_service import _audit, _lock_identity


logger = logging.getLogger("GEO-Organization-Payer-Policy")

# 外部独立审核裁决（2026-07-23，覆盖 R3 的 24h 统计豁免方向）：unknown（响应
# 未知）隔离态 charge 的资金仍冻结，统计上一旦放开员工即可再次占用上限、突破
# 老板硬上限。正确语义：unknown 始终全额计入日/月已用量，直到可靠地
# settle/release/refund 或经 force-release 人工解决（受控终态路径，见
# services.organization_billing.force_release_charge）。force-settle 不提供：
# 无真实 outcome 不得确认消费。
POLICY_LIMIT_FIELDS = (
    "per_action_limit_points",
    "daily_limit_points",
    "monthly_limit_points",
)

DISABLED_POLICY: dict[str, Any] = {
    "organization_id": None,
    "shared_payer_enabled": False,
    "overage_enabled": False,
    "per_action_limit_points": None,
    "daily_limit_points": None,
    "monthly_limit_points": None,
    "policy_version": 0,
    "updated_by_owner_user_id": None,
    "enabled_at": None,
    "disabled_at": None,
    "reason": None,
    "created_at": None,
    "updated_at": None,
}


def _row_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical consent content frozen onto every member charge link."""
    return {
        "organization_id": int(row["organization_id"]),
        "shared_payer_enabled": bool(row["shared_payer_enabled"]),
        "overage_enabled": bool(row["overage_enabled"]),
        "per_action_limit_points": _opt_int(row["per_action_limit_points"]),
        "daily_limit_points": _opt_int(row["daily_limit_points"]),
        "monthly_limit_points": _opt_int(row["monthly_limit_points"]),
        "policy_version": int(row["policy_version"]),
    }


def _opt_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def lock_payer_policy(cursor, organization_id: int) -> Optional[dict[str, Any]]:
    """Lock the policy row directly after the organizations FOR UPDATE lock."""
    cursor.execute(
        "SELECT * FROM organization_payer_policies WHERE organization_id=%s FOR UPDATE",
        (int(organization_id),),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def effective_policy(row: Optional[Mapping[str, Any]], *, organization_id: int) -> dict[str, Any]:
    if row is None:
        return {**DISABLED_POLICY, "organization_id": int(organization_id)}
    return _row_snapshot(row)


def member_payer_consent(cursor, identity: IdentityContext) -> dict[str, Any]:
    """Locked consent for a member reservation; fail closed when disabled.

    The organization row lock (``_lock_identity``) must already be held so this
    policy row lock lands directly after it in the frozen lock order.
    """
    row = lock_payer_policy(cursor, identity.organization_id)
    policy = effective_policy(row, organization_id=identity.organization_id)
    if not policy["shared_payer_enabled"]:
        raise OrganizationError(
            "ORG_SHARED_PAYER_CONSENT_MISSING",
            "老板尚未开启员工费用代付，本次付费动作已阻止",
            http_status=403,
        )
    return policy


def _overage_usage(
    cursor,
    *,
    organization_id: int,
    billing_timezone: str,
    membership_id: Optional[int] = None,
) -> dict[str, Any]:
    """Net overage usage for the current billing day/month.

    Reserved charges hold their full overage leg, settled charges their actual
    leg, released charges zero, and refunds reduce the leg, so a plain SUM over
    the frozen per-charge columns is the exact budget consumption.

    外部独立审核裁决（2026-07-23）：status='unknown' 的隔离 charge 不设任何
    时间豁免，全额计入硬顶——资金冻结期间统计口径不得放开，否则员工可再次
    占用量突破老板硬上限。解除占用的唯一路径是可靠 settle/release/refund
    或 force-release 人工解决。
    """
    now = datetime.now(timezone.utc)
    day_start, day_end = _period_bounds(now, billing_timezone, "daily")
    month_start, month_end = _period_bounds(now, billing_timezone, "monthly")

    def bucket(start: datetime, end: datetime) -> int:
        cursor.execute(
            """
            SELECT COALESCE(SUM(overage_points),0) AS used
            FROM organization_charge_links
            WHERE organization_id=%s AND created_at>=%s AND created_at<%s
              AND (%s IS NULL OR membership_id=%s)
            """,
            (int(organization_id), start, end, membership_id, membership_id),
        )
        return int(cursor.fetchone()["used"])

    return {
        "daily_overage_used_points": bucket(day_start, day_end),
        "monthly_overage_used_points": bucket(month_start, month_end),
        "daily_period_start": day_start,
        "daily_period_end": day_end,
        "monthly_period_start": month_start,
        "monthly_period_end": month_end,
    }


def enforce_overage_caps(
    cursor,
    identity: IdentityContext,
    policy: Mapping[str, Any],
    *,
    overage_points: int,
) -> None:
    """Validate one overage leg against the three owner hard caps.

    Runs inside the organization serialization lock before any provider call;
    a rejection leaves zero task, zero durable freeze and zero provider traffic.
    """
    overage_points = int(overage_points)
    if overage_points <= 0:
        return
    if not policy.get("overage_enabled"):
        raise OrganizationError(
            "ORG_OVERAGE_NOT_CONSENTED",
            "你的算力上限不足，且老板未开启超额代付，本次操作已停止",
            http_status=402,
            safe_details={"overage_points": overage_points},
        )
    per_action = _opt_int(policy.get("per_action_limit_points"))
    if per_action is None or per_action <= 0 or overage_points > per_action:
        raise OrganizationError(
            "ORG_OVERAGE_PER_ACTION_CAP_EXCEEDED",
            "本次超额代付超过老板设置的单次上限，已阻止",
            http_status=402,
            safe_details={"overage_points": overage_points, "per_action_limit_points": per_action},
        )
    cursor.execute(
        "SELECT billing_timezone FROM organizations WHERE id=%s",
        (identity.organization_id,),
    )
    organization = cursor.fetchone()
    if not organization:
        raise OrganizationError("ORG_NOT_FOUND", "组织不存在", http_status=404)
    usage = _overage_usage(
        cursor,
        organization_id=identity.organization_id,
        billing_timezone=str(organization["billing_timezone"]),
    )
    daily_cap = _opt_int(policy.get("daily_limit_points"))
    monthly_cap = _opt_int(policy.get("monthly_limit_points"))
    if daily_cap is None or daily_cap <= 0 or usage["daily_overage_used_points"] + overage_points > daily_cap:
        raise OrganizationError(
            "ORG_OVERAGE_DAILY_CAP_EXCEEDED",
            "本次超额代付超过老板设置的每日上限，已阻止",
            http_status=402,
            safe_details={
                "overage_points": overage_points,
                "daily_limit_points": daily_cap,
                "daily_overage_used_points": usage["daily_overage_used_points"],
            },
        )
    if monthly_cap is None or monthly_cap <= 0 or usage["monthly_overage_used_points"] + overage_points > monthly_cap:
        raise OrganizationError(
            "ORG_OVERAGE_MONTHLY_CAP_EXCEEDED",
            "本次超额代付超过老板设置的每月上限，已阻止",
            http_status=402,
            safe_details={
                "overage_points": overage_points,
                "monthly_limit_points": monthly_cap,
                "monthly_overage_used_points": usage["monthly_overage_used_points"],
            },
        )


def get_payer_policy(identity: IdentityContext) -> dict[str, Any]:
    """Role-shaped read: owner sees the full policy, members a redacted view."""
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        row = lock_payer_policy(cursor, identity.organization_id)
        policy = effective_policy(row, organization_id=identity.organization_id)
        cursor.execute(
            "SELECT billing_timezone FROM organizations WHERE id=%s",
            (identity.organization_id,),
        )
        organization = cursor.fetchone()
        billing_timezone = str(organization["billing_timezone"]) if organization else "Asia/Shanghai"
        flags = feature_flags()
        if identity.is_owner:
            usage = _overage_usage(
                cursor,
                organization_id=identity.organization_id,
                billing_timezone=billing_timezone,
            )
            daily_cap = policy["daily_limit_points"]
            monthly_cap = policy["monthly_limit_points"]
            usage["daily_overage_remaining_points"] = (
                None if daily_cap is None else max(daily_cap - usage["daily_overage_used_points"], 0)
            )
            usage["monthly_overage_remaining_points"] = (
                None if monthly_cap is None else max(monthly_cap - usage["monthly_overage_used_points"], 0)
            )
            full = dict(policy)
            if row is not None:
                full["enabled_at"] = row["enabled_at"]
                full["disabled_at"] = row["disabled_at"]
                full["reason"] = row["reason"]
                full["updated_at"] = row["updated_at"]
            return {
                "viewer": "owner",
                "global_shared_payer_flag": bool(flags["ORGANIZATION_SHARED_PAYER_ENABLED"]),
                "policy": full,
                "usage": usage,
            }
        my_usage = _overage_usage(
            cursor,
            organization_id=identity.organization_id,
            billing_timezone=billing_timezone,
            membership_id=identity.membership_id,
        )
        return {
            "viewer": "member",
            "global_shared_payer_flag": bool(flags["ORGANIZATION_SHARED_PAYER_ENABLED"]),
            "shared_payer_enabled": policy["shared_payer_enabled"],
            "overage_enabled": policy["overage_enabled"],
            "my_usage": my_usage,
        }


def _require_platform_admin(cursor, actor_user_id: int) -> None:
    cursor.execute(
        """SELECT u.id,u.is_active,EXISTS(
             SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
             WHERE ur.user_id=u.id AND r.name='admin'
           ) AS is_admin
           FROM users u WHERE id=%s FOR UPDATE""",
        (int(actor_user_id),),
    )
    row = cursor.fetchone()
    if not row or not bool(row["is_active"]) or not bool(row["is_admin"]):
        raise OrganizationError(
            "PLATFORM_ADMIN_REQUIRED",
            "仅平台管理员可执行紧急关闭，且不能替老板开启或提高上限",
            http_status=403,
        )


def _desired_state(
    *,
    shared_payer_enabled: bool,
    overage_enabled: bool,
    per_action_limit_points: Optional[int],
    daily_limit_points: Optional[int],
    monthly_limit_points: Optional[int],
) -> dict[str, Any]:
    desired = {
        "shared_payer_enabled": bool(shared_payer_enabled),
        "overage_enabled": bool(overage_enabled),
        "per_action_limit_points": _opt_int(per_action_limit_points),
        "daily_limit_points": _opt_int(daily_limit_points),
        "monthly_limit_points": _opt_int(monthly_limit_points),
    }
    # 负值上限前置 422：disabled 态不受"开启需正值"校验约束，负值会一路
    # 落库撞 CHECK 变 500（CHECK 仍是最后防线，这里给人话）。
    negative = [
        field for field in POLICY_LIMIT_FIELDS
        if desired[field] is not None and int(desired[field]) < 0
    ]
    if negative:
        raise OrganizationError(
            "ORG_PAYER_POLICY_LIMIT_NEGATIVE",
            "员工费用代付上限不能为负数",
            http_status=422,
            safe_details={"negative_limits": negative},
        )
    if not desired["shared_payer_enabled"] and desired["overage_enabled"]:
        # 惰性组合：member_payer_consent 先闸 shared_payer_enabled，该组合下
        # overage 永不可达，配置面只会误导老板。显式 422，不静默改写。
        raise OrganizationError(
            "ORG_PAYER_POLICY_OVERAGE_REQUIRES_SHARED",
            "请先开启共享钱包后再开启超额代付",
            http_status=422,
        )
    return desired


def put_payer_policy(
    *,
    identity: Optional[IdentityContext],
    actor_user_id: int,
    is_platform_admin: bool = False,
    organization_id: Optional[int] = None,
    shared_payer_enabled: bool,
    overage_enabled: bool,
    per_action_limit_points: Optional[int],
    daily_limit_points: Optional[int],
    monthly_limit_points: Optional[int],
    reason: str,
    expected_version: int,
    request_id: str,
    source_ip: Optional[str] = None,
) -> dict[str, Any]:
    """CAS update of the owner-consented payer policy with append-only audit."""
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写本次调整原因", http_status=422)
    request_id = str(request_id or "").strip()
    if len(request_id) < 8:
        raise OrganizationError("ORG_REQUEST_ID_INVALID", "请求无效，请刷新页面重试", http_status=422)
    expected_version = int(expected_version)
    if expected_version < 0:
        raise OrganizationError("ORG_PAYER_POLICY_VERSION_INVALID", "页面数据已过期，请刷新", http_status=422)
    desired = _desired_state(
        shared_payer_enabled=shared_payer_enabled,
        overage_enabled=overage_enabled,
        per_action_limit_points=per_action_limit_points,
        daily_limit_points=daily_limit_points,
        monthly_limit_points=monthly_limit_points,
    )
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-payer-policy:{request_id}",),
        )
        admin_override = False
        if identity is not None:
            locked_identity = _lock_identity(cursor, identity)
            target_organization_id = int(locked_identity.organization_id)
            if organization_id is not None and int(organization_id) != target_organization_id:
                # 显式优先级（外部独立审核裁决 2026-07-23）：目标组织非本人组织时——
                #   1. 平台管理员（is_platform_admin，下方 _require_platform_admin
                #      DB 复核）→ admin_override 紧急关闭路径。平台 ADMIN 拥有
                #      自己团队（identity 解析为组织 A owner）时不得被 owner
                #      身份短路成 403；
                #   2. 其余（含非管理员的老板碰别人组织）→ 403。
                # 本人就是目标组织 owner 的情形不进入本分支（organization_id 等于
                # 身份组织），自然走 owner 路径。
                if not is_platform_admin:
                    raise OrganizationError(
                        "ORG_PAYER_POLICY_SCOPE_MISMATCH",
                        "老板只能调整自己组织的员工费用代付策略",
                        http_status=403,
                    )
                admin_override = True
                # 管理员持普通成员身份（组织 A）紧急关闭目标组织 B：下游所有
                # 锁、CAS、UPSERT、events 与代际 bump 必须落在 B 上。漏掉这行
                # 赋值会让目标静默停留在 A，造成跨组织误写。
                target_organization_id = int(organization_id)
            elif not locked_identity.is_owner:
                if not is_platform_admin:
                    raise OrganizationError(
                        "ORG_OWNER_REQUIRED",
                        "仅老板可开启或调整员工费用代付，员工与组织管理员无权操作",
                        http_status=403,
                    )
                admin_override = True
        else:
            if not is_platform_admin:
                raise OrganizationError("ORG_MEMBERSHIP_REQUIRED", "你还没有加入任何团队", http_status=404)
            admin_override = True
            if organization_id is None:
                raise OrganizationError(
                    "ORG_PAYER_POLICY_TARGET_REQUIRED",
                    "平台管理员紧急关闭必须指定目标组织",
                    http_status=422,
                )
            target_organization_id = int(organization_id)
        if admin_override:
            _require_platform_admin(cursor, actor_user_id)
            cursor.execute(
                "SELECT id,owner_user_id,status FROM organizations WHERE id=%s FOR UPDATE",
                (target_organization_id,),
            )
            organization = cursor.fetchone()
            if not organization:
                raise OrganizationError("ORG_NOT_FOUND", "组织不存在", http_status=404)
            locked_identity = None
        actor_kind = "platform_admin" if admin_override else "owner"
        row = lock_payer_policy(cursor, target_organization_id)
        current = effective_policy(row, organization_id=target_organization_id)
        # Idempotent replay is checked before CAS: a response-loss retry carries
        # the original expected_version, which no longer matches the bumped row.
        event_key = stable_key("organization-payer-policy", target_organization_id, request_id)
        cursor.execute(
            "SELECT new_snapshot FROM organization_payer_policy_events WHERE event_key=%s",
            (event_key,),
        )
        replay = cursor.fetchone()
        if replay:
            stored = replay["new_snapshot"]
            if isinstance(stored, str):
                import json

                stored = json.loads(stored)
            desired_with_version = {**desired, "policy_version": expected_version + 1, "organization_id": target_organization_id}
            if payload_hash(stored) != payload_hash(desired_with_version):
                raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "操作冲突，请刷新后重试", http_status=409)
            result = dict(stored)
            result["replayed"] = True
            return result
        if int(current["policy_version"]) != expected_version:
            raise OrganizationError(
                "ORG_PAYER_POLICY_VERSION_CONFLICT",
                "员工费用代付策略已被其他人修改，请刷新后重试",
                http_status=409,
                retryable=True,
                safe_details={"current_policy_version": int(current["policy_version"])},
            )
        if admin_override:
            disabling = (
                (not desired["shared_payer_enabled"] and current["shared_payer_enabled"])
                or (not desired["overage_enabled"] and current["overage_enabled"])
            )
            raises = (
                (desired["shared_payer_enabled"] and not current["shared_payer_enabled"])
                or (desired["overage_enabled"] and not current["overage_enabled"])
                or any(
                    desired[field] is not None
                    and (current[field] is None or int(desired[field]) > int(current[field]))
                    and desired[field] != current[field]
                    for field in POLICY_LIMIT_FIELDS
                )
            )
            if raises or not disabling:
                raise OrganizationError(
                    "ORG_PAYER_POLICY_ADMIN_DISABLE_ONLY",
                    "平台管理员仅可紧急关闭员工费用代付，不能替老板开启或提高上限",
                    http_status=403,
                )
        enabling = (
            (desired["shared_payer_enabled"] and not current["shared_payer_enabled"])
            or (desired["overage_enabled"] and not current["overage_enabled"])
        )
        if desired["shared_payer_enabled"] or desired["overage_enabled"]:
            missing = [field for field in POLICY_LIMIT_FIELDS if desired[field] is None or int(desired[field]) <= 0]
            if missing:
                raise OrganizationError(
                    "ORG_PAYER_POLICY_LIMITS_REQUIRED",
                    "开启员工费用代付必须设置大于 0 的单次、每日、每月上限",
                    http_status=422,
                    safe_details={"missing_limits": missing},
                )
        if enabling and admin_override:
            raise OrganizationError(
                "ORG_PAYER_POLICY_ADMIN_DISABLE_ONLY",
                "平台管理员仅可紧急关闭员工费用代付，不能替老板开启或提高上限",
                http_status=403,
            )
        new_version = expected_version + 1
        was_enabled = current["shared_payer_enabled"] or current["overage_enabled"]
        will_enable = desired["shared_payer_enabled"] or desired["overage_enabled"]
        enabled_at = row["enabled_at"] if row is not None else None
        if will_enable and not was_enabled:
            enabled_at = datetime.now(timezone.utc)
        disabled_at = row["disabled_at"] if row is not None else None
        if not will_enable and was_enabled:
            disabled_at = datetime.now(timezone.utc)
        cursor.execute(
            """
            INSERT INTO organization_payer_policies(
              organization_id,shared_payer_enabled,overage_enabled,
              per_action_limit_points,daily_limit_points,monthly_limit_points,
              policy_version,updated_by_owner_user_id,enabled_at,disabled_at,reason
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(organization_id) DO UPDATE SET
              shared_payer_enabled=EXCLUDED.shared_payer_enabled,
              overage_enabled=EXCLUDED.overage_enabled,
              per_action_limit_points=EXCLUDED.per_action_limit_points,
              daily_limit_points=EXCLUDED.daily_limit_points,
              monthly_limit_points=EXCLUDED.monthly_limit_points,
              policy_version=EXCLUDED.policy_version,
              updated_by_owner_user_id=EXCLUDED.updated_by_owner_user_id,
              enabled_at=EXCLUDED.enabled_at,
              disabled_at=EXCLUDED.disabled_at,
              reason=EXCLUDED.reason,
              updated_at=NOW()
            """,
            (
                target_organization_id,
                desired["shared_payer_enabled"],
                desired["overage_enabled"],
                desired["per_action_limit_points"],
                desired["daily_limit_points"],
                desired["monthly_limit_points"],
                new_version,
                None if admin_override else int(actor_user_id),
                enabled_at,
                disabled_at,
                reason,
            ),
        )
        # 外部独立审核裁决（2026-07-23，覆盖 R1 的"收紧才 bump"方向）：代付
        # 策略编辑永不 bump authority_version——关闭/降额也不得中断在途：在途
        # charge 持有 reservation 时冻结的不可变 owner-consent 快照，按快照完成
        # 或退款；策略变化只影响新 reservation 的 consent 闸（reserve 路径读
        # live policy）。身份变化（成员撤权/客户撤分配）的立即阻断由既有
        # capability/assignment 活体闸承担，与 authority bump 无关。
        new_snapshot = {
            **desired,
            "policy_version": new_version,
            "organization_id": target_organization_id,
        }
        cursor.execute(
            """
            INSERT INTO organization_payer_policy_events(
              event_key,organization_id,request_id,actor_user_id,actor_kind,
              source_ip,old_snapshot,new_snapshot,reason
            ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
            """,
            (
                event_key,
                target_organization_id,
                request_id,
                int(actor_user_id),
                actor_kind,
                (str(source_ip)[:120] if source_ip else None),
                canonical_json(current if row is not None else None),
                canonical_json(new_snapshot),
                reason,
            ),
        )
        # 次级组织审计只对老板自有路径书写：locked_identity 属于"操作者所在
        # 组织"。admin_override（平台管理员紧急关闭其他组织）时 locked_identity
        # 已置 None —— 若用它写 _audit，会把目标组织 B 的 entity 错记进管理员
        # 自己组织 A 的审计流。该路径的主审计由上面的
        # organization_payer_policy_events（append-only、含 actor_kind=
        # platform_admin 与新旧快照）完整承担，因此显式跳过次级 _audit。
        if locked_identity is not None:
            _audit(
                cursor,
                locked_identity,
                action="payer_policy.update",
                entity_type="organization_payer_policy",
                entity_id=target_organization_id,
                before=current if row is not None else None,
                after=new_snapshot,
                reason=reason,
            )
        result = dict(new_snapshot)
        result["replayed"] = False
        return result
