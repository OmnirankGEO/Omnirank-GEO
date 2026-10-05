"""Organization, seat, invitation, role, assignment, and audit control plane.

Every mutation owns one database transaction at this service boundary.  Payer
identity, entitlement, role capabilities, assignment authority, and audit rows
are resolved and written together; HTTP payloads cannot nominate a payer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from db.connection import get_db
from db.organization_db import assert_ready, assigned_brand_ids, resolve_identity
from services.organization_contract import (
    APPROVAL_ACTIONS,
    BILLABLE_FEATURE_CAPABILITIES,
    ROLE_TEMPLATE_CAPABILITIES,
    ROLE_TEMPLATE_NAMES,
    EXTERNAL_ACTIONS,
    IdentityContext,
    OrganizationError,
    capability_snapshot_hash,
    canonical_json,
    payload_hash,
    require_governance_audit_access,
    require_feature_flag,
    stable_key,
    validate_capabilities,
)
from services.organization_crypto import (
    decrypt_delivery_target,
    derive_bearer_token,
    encrypt_delivery_target,
    hash_bearer_token,
    normalize_target,
    rate_subject_hmac,
    target_hmac,
    target_hmac_candidates,
    target_matches,
)
from services.organization_membership_lifecycle import (
    restore_legacy_access_or_retire_operator,
    rotate_permission_version,
    snapshot_and_clear_legacy_access,
)


CATALOG_TYPE = "feature_consumption"
CATALOG_SCOPE = "ORGANIZATION_SEATS"


def _rotate_permission_version_for_users(cursor, user_ids) -> int:
    """把一批用户的 users.permission_version 顶一格,让平台权限缓存立刻失效。

    [单1 · revoke 时效] 组织能力被推导成平台权限串后,它就会随 JWT / 权限缓存
    一起被缓存(perm_cache 60s + Redis 300s)。组织内部的 capability_version /
    authority_version 管不到这两层缓存,所以每次改动**授权面**都必须顺带 rotate
    平台侧的 permission_version,否则收回权限后仍有数分钟的旧权限窗口。

    返回实际 rotate 的用户数。空集合是合法的(角色下暂时没人)。
    """
    unique_ids = sorted({int(uid) for uid in (user_ids or []) if uid is not None})
    for user_id in unique_ids:
        rotate_permission_version(cursor, user_id=user_id)
    return len(unique_ids)


def _rotate_permission_version_for_role(cursor, *, role_id: int) -> int:
    """角色能力变了 → 该角色下**所有**席位的平台权限都必须失效。

    刻意不按 status 过滤:暂停/离职中的席位将来可能恢复,让它们也带上新版本号,
    避免恢复后短暂沿用旧授权。
    """
    cursor.execute(
        "SELECT user_id FROM organization_memberships WHERE role_id=%s",
        (int(role_id),),
    )
    return _rotate_permission_version_for_users(cursor, [r["user_id"] for r in cursor.fetchall()])


def _rotate_permission_version_for_membership(cursor, *, membership_id: int) -> int:
    """成员级 override 变了 → 该席位的平台权限立刻失效。"""
    cursor.execute(
        "SELECT user_id FROM organization_memberships WHERE id=%s",
        (int(membership_id),),
    )
    row = cursor.fetchone()
    return _rotate_permission_version_for_users(cursor, [row["user_id"]] if row else [])
CATALOG_PRODUCT = "organization_internal_seats"


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _positive_config(metadata: Mapping[str, Any], name: str, *, allow_zero: bool = False) -> int:
    value = metadata.get(name)
    if isinstance(value, bool):
        value = None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = -1
    if parsed < (0 if allow_zero else 1):
        raise OrganizationError(
            "ORG_PRODUCT_CONFIG_INVALID",
            "席位服务配置异常，请联系平台客服",
            http_status=503,
            safe_details={"field": name},
        )
    return parsed


def _load_product_config(cursor, *, for_update: bool = False) -> dict[str, Any]:
    cursor.execute(
        f"""
        SELECT v.id AS version_id, v.version_code, v.effective_from,
               e.id AS entry_id, e.product_code, e.final_price_cents,
               e.source_ref_jsonb
        FROM pricing_catalog_versions v
        JOIN pricing_catalog_entries e ON e.version_id=v.id
        WHERE v.catalog_type=%s AND v.scope_key=%s AND v.status='published'
          AND v.effective_to IS NULL AND e.product_code=%s
        LIMIT 1 {"FOR UPDATE OF v, e" if for_update else ""}
        """,
        (CATALOG_TYPE, CATALOG_SCOPE, CATALOG_PRODUCT),
    )
    row = cursor.fetchone()
    if not row:
        raise OrganizationError(
            "ORG_PRODUCT_CONFIG_MISSING",
            "席位服务配置异常，请联系平台客服",
            http_status=503,
        )
    metadata = _json_object(row.get("source_ref_jsonb"))
    config = {
        "version_id": int(row["version_id"]),
        "version_code": str(row["version_code"]),
        "entry_id": int(row["entry_id"]),
        "product_code": str(row["product_code"]),
        "included_seats": _positive_config(metadata, "included_seats", allow_zero=True),
        "extra_seat_price_cents": _positive_config(metadata, "extra_seat_price_cents", allow_zero=True),
        "high_cost_approval_threshold_points": _positive_config(
            metadata, "high_cost_approval_threshold_points", allow_zero=True
        ),
        "invite_ttl_hours": _positive_config(metadata, "invite_ttl_hours"),
        "approval_ttl_hours": _positive_config(metadata, "approval_ttl_hours"),
        "metadata": metadata,
    }
    config["snapshot_hash"] = payload_hash(config)
    return config


_RATE_COLUMNS = {
    "organization": "organization_id",
    "actor": "actor_user_id",
    "target": "target_hmac",
    "ip": "source_ip_hmac",
}


def _rate_policy(config: Mapping[str, Any], action: str, dimensions: Sequence[str]) -> dict[str, int]:
    policies = config.get("metadata", {}).get("rate_limits")
    policy = policies.get(action) if isinstance(policies, Mapping) else None
    if not isinstance(policy, Mapping):
        raise OrganizationError(
            "ORG_RATE_POLICY_MISSING",
            "系统配置异常，请联系平台客服（ORG_RATE_POLICY_MISSING）",
            http_status=503,
            safe_details={"action": action},
        )
    parsed = {"window_seconds": _positive_config(policy, "window_seconds")}
    if parsed["window_seconds"] > 31 * 86400:
        raise OrganizationError("ORG_RATE_POLICY_INVALID", "系统配置异常，请联系平台客服（ORG_RATE_POLICY_INVALID）", http_status=503)
    for dimension in dimensions:
        if dimension not in _RATE_COLUMNS:
            raise RuntimeError(f"unsupported organization rate dimension: {dimension}")
        parsed[dimension] = _positive_config(policy, dimension)
    return parsed


def _enforce_invite_rate(
    *,
    action: str,
    dimensions: Sequence[str],
    source_ip: str,
    organization_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    target_digest: Optional[str] = None,
) -> None:
    """Persist abuse attempts in an independent transaction, including failures."""
    ip_digest, key_version = rate_subject_hmac("source-ip", source_ip)
    values: dict[str, Any] = {
        "organization": int(organization_id) if organization_id is not None else None,
        "actor": int(actor_user_id) if actor_user_id is not None else None,
        "target": str(target_digest) if target_digest else None,
        "ip": ip_digest if "ip" in dimensions else None,
    }
    missing = [dimension for dimension in dimensions if values.get(dimension) is None]
    if missing:
        raise OrganizationError(
            "ORG_RATE_SUBJECT_MISSING",
            "网络环境异常，暂时无法完成操作，请稍后再试",
            http_status=503,
            safe_details={"dimensions": missing},
        )
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        config = _load_product_config(cursor)
        policy = _rate_policy(config, action, dimensions)
        lock_keys = sorted(
            stable_key("organization-rate", action, dimension, values[dimension])
            for dimension in dimensions
        )
        for lock_key in lock_keys:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (lock_key,))
        for dimension in dimensions:
            column = _RATE_COLUMNS[dimension]
            cursor.execute(
                f"""
                SELECT COUNT(*) AS attempts
                FROM organization_security_rate_events
                WHERE action=%s AND {column}=%s
                  AND created_at > NOW() - (%s * INTERVAL '1 second')
                """,
                (action, values[dimension], policy["window_seconds"]),
            )
            if int(cursor.fetchone()["attempts"]) >= int(policy[dimension]):
                raise OrganizationError(
                    "ORG_RATE_LIMITED",
                    "操作过于频繁，请稍后重试",
                    http_status=429,
                    retryable=True,
                    safe_details={"dimension": dimension},
                )
        cursor.execute(
            """
            INSERT INTO organization_security_rate_events(
                organization_id,action,actor_user_id,target_hmac,source_ip_hmac,
                hmac_key_version,product_catalog_version_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                values["organization"],
                action,
                values["actor"],
                values["target"],
                values["ip"],
                key_version,
                config["version_id"],
            ),
        )


def _invite_rate_subject(token: str) -> Optional[tuple[int, str]]:
    """Resolve only the opaque rate dimensions before any business row locks."""
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        cursor.execute("SELECT token_key_version FROM organization_invites WHERE status='pending'")
        digests: list[str] = []
        for candidate in cursor.fetchall():
            try:
                digest, _ = hash_bearer_token(
                    purpose="organization-invite",
                    token=token,
                    key_version=candidate["token_key_version"],
                )
            except OrganizationError:
                continue
            digests.append(digest)
        if not digests:
            return None
        cursor.execute(
            """
            SELECT organization_id,target_hmac
            FROM organization_invites
            WHERE status='pending' AND token_hash=ANY(%s)
            LIMIT 1
            """,
            (digests,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return int(row["organization_id"]), str(row["target_hmac"])


def _audit(
    cursor,
    identity: IdentityContext,
    *,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    before: Any = None,
    after: Any = None,
    result: Any = None,
    reason: Optional[str] = None,
    sequence: int = 0,
) -> None:
    audit_key = stable_key(identity.organization_id, identity.request_id, action, entity_type, entity_id, sequence)
    snapshot = after if after is not None else result if result is not None else {}
    cursor.execute(
        """
        INSERT INTO organization_audit_events(
            audit_event_key,organization_id,request_id,sequence,event_type,action,
            actor_kind,actor_user_id,membership_id,entity_type,entity_id,payload_hash,
            before_snapshot,after_snapshot,result_snapshot,reason
        ) VALUES (%s,%s,%s,%s,'mutation',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(audit_event_key) DO NOTHING
        """,
        (
            audit_key,
            identity.organization_id,
            identity.request_id,
            sequence,
            action,
            identity.actor_kind,
            identity.actor_user_id,
            identity.membership_id,
            entity_type,
            str(entity_id) if entity_id is not None else None,
            payload_hash(snapshot),
            canonical_json(before) if before is not None else None,
            canonical_json(after) if after is not None else None,
            canonical_json(result) if result is not None else None,
            reason,
        ),
    )


def _system_identity(*, organization_id: int, owner_user_id: int, request_id: str) -> IdentityContext:
    return IdentityContext(
        request_id=request_id,
        authenticated_user_id=owner_user_id,
        principal_user_id=owner_user_id,
        payer_user_id=owner_user_id,
        actor_kind="system",
        organization_id=organization_id,
        requested_by_user_id=owner_user_id,
    )


def _owner_identity(cursor, owner_user_id: int, request_id: str) -> IdentityContext:
    identity = resolve_identity(owner_user_id, request_id=request_id, cursor=cursor)
    if not identity or not identity.is_owner:
        raise OrganizationError("ORG_OWNER_REQUIRED", "仅团队负责人可执行此操作", http_status=403)
    return identity


def _lock_identity(cursor, identity: IdentityContext, *, owner_required: bool = False) -> IdentityContext:
    # Lock the authority roots before re-resolving. Resolving first and locking
    # later leaves a revoke/suspend race where stale capabilities survive.
    cursor.execute("SELECT id FROM organizations WHERE id=%s FOR UPDATE", (identity.organization_id,))
    if not cursor.fetchone():
        raise OrganizationError("ORG_IDENTITY_CHANGED", "你的团队身份或权限刚发生变化，请刷新页面", http_status=409, retryable=True)
    cursor.execute(
        """
        SELECT id FROM organization_memberships
        WHERE organization_id=%s AND user_id=%s FOR UPDATE
        """,
        (identity.organization_id, identity.authenticated_user_id),
    )
    if not cursor.fetchone():
        raise OrganizationError("ORG_IDENTITY_CHANGED", "你的团队身份或权限刚发生变化，请刷新页面", http_status=409, retryable=True)
    locked = resolve_identity(identity.authenticated_user_id, request_id=identity.request_id, cursor=cursor)
    if not locked or locked.organization_id != identity.organization_id:
        raise OrganizationError("ORG_IDENTITY_CHANGED", "你的团队身份或权限刚发生变化，请刷新页面", http_status=409, retryable=True)
    if owner_required and not locked.is_owner:
        raise OrganizationError("ORG_OWNER_REQUIRED", "仅团队负责人可执行此操作", http_status=403)
    return locked


def create_organization(*, owner_user_id: int, name: str, request_id: str) -> dict[str, Any]:
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    name = str(name or "").strip()
    if not name or len(name) > 120:
        raise OrganizationError("ORG_NAME_INVALID", "团队名称不能为空，且不能超过 120 字", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-create:{request_id}",),
        )
        assert_ready(cursor)
        cursor.execute(
            "SELECT id,owner_user_id,name,status FROM organizations WHERE creation_request_id=%s FOR UPDATE",
            (request_id,),
        )
        replay = cursor.fetchone()
        if replay:
            if (
                int(replay["owner_user_id"]) != int(owner_user_id)
                or str(replay["name"]) != name
            ):
                raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "操作冲突，请刷新页面后重试", http_status=409)
            return get_organization_overview_by_id(cursor, int(replay["id"]), int(owner_user_id))

        cursor.execute(
            """
            SELECT u.id,u.is_active
            FROM users u
            WHERE u.id=%s FOR UPDATE OF u
            """,
            (owner_user_id,),
        )
        user = cursor.fetchone()
        if not user or not bool(user["is_active"]):
            raise OrganizationError(
                "ORG_OWNER_INVALID",
                "当前账号状态异常，无法创建团队",
                http_status=422,
            )
        cursor.execute(
            "SELECT id FROM organization_memberships WHERE user_id=%s AND status IN ('active','suspended','leaving')",
            (owner_user_id,),
        )
        if cursor.fetchone():
            raise OrganizationError("ORG_USER_ALREADY_MEMBER", "你已加入团队，一个账号只能属于一个团队", http_status=409)

        config = _load_product_config(cursor, for_update=True)
        cursor.execute(
            """
            INSERT INTO organizations(owner_user_id,creation_request_id,name,status)
            VALUES (%s,%s,%s,'active') RETURNING *
            """,
            (owner_user_id, request_id, name),
        )
        organization = dict(cursor.fetchone())
        org_id = int(organization["id"])
        cursor.execute(
            """
            INSERT INTO organization_seat_entitlements(
                organization_id,product_catalog_version_id,product_catalog_entry_id,source_sku,
                entitled_seats,extra_seat_price_snapshot,effective_from,status,snapshot_hash
            ) VALUES (%s,%s,%s,%s,%s,%s,NOW(),'active',%s) RETURNING id
            """,
            (
                org_id,
                config["version_id"],
                config["entry_id"],
                config["product_code"],
                config["included_seats"],
                canonical_json({
                    "price_cents": config["extra_seat_price_cents"],
                    "catalog_version": config["version_code"],
                }),
                config["snapshot_hash"],
            ),
        )
        entitlement_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO organization_roles(
                organization_id,code,name,is_owner_role,created_by_user_id
            ) VALUES (%s,'owner','老板',TRUE,%s) RETURNING id
            """,
            (org_id, owner_user_id),
        )
        owner_role_id = int(cursor.fetchone()["id"])
        for role_code in ("sales", "delivery", "readonly"):
            cursor.execute(
                """
                INSERT INTO organization_roles(organization_id,code,name,created_by_user_id)
                VALUES (%s,%s,%s,%s) RETURNING id
                """,
                (org_id, role_code, ROLE_TEMPLATE_NAMES[role_code], owner_user_id),
            )
            template_role_id = int(cursor.fetchone()["id"])
            cursor.executemany(
                """
                INSERT INTO organization_role_capabilities(role_id,capability,effect,created_by_user_id)
                VALUES (%s,%s,'allow',%s)
                """,
                [
                    (template_role_id, capability, owner_user_id)
                    for capability in sorted(ROLE_TEMPLATE_CAPABILITIES[role_code])
                ],
            )
        cursor.execute(
            """
            INSERT INTO organization_memberships(organization_id,user_id,role_id,status,is_owner)
            VALUES (%s,%s,%s,'active',TRUE) RETURNING id
            """,
            (org_id, owner_user_id, owner_role_id),
        )
        membership_id = int(cursor.fetchone()["id"])

        policies: list[tuple[str, Optional[int], bool]] = [
            (action, None, True) for action in sorted(EXTERNAL_ACTIONS)
        ]
        policies.extend(
            (action, None, True)
            for action in sorted(APPROVAL_ACTIONS - EXTERNAL_ACTIONS - {"billing.execute_high_cost"})
        )
        policies.append(
            (
                "billing.execute_high_cost",
                config["high_cost_approval_threshold_points"],
                False,
            )
        )
        for action, threshold, always in policies:
            policy_snapshot = {
                "action_type": action,
                "threshold_points": threshold,
                "always_require_approval": always,
                "catalog_version": config["version_code"],
            }
            cursor.execute(
                """
                INSERT INTO organization_approval_policies(
                    organization_id,version,status,action_type,threshold_points,
                    always_require_approval,effective_from,policy_hash,created_by_user_id
                ) VALUES (%s,1,'active',%s,%s,%s,NOW(),%s,%s)
                """,
                (org_id, action, threshold, always, payload_hash(policy_snapshot), owner_user_id),
            )
        identity = IdentityContext(
            request_id=request_id,
            authenticated_user_id=owner_user_id,
            principal_user_id=owner_user_id,
            payer_user_id=owner_user_id,
            actor_kind="owner",
            organization_id=org_id,
            organization_status="active",
            membership_status="active",
            actor_user_id=owner_user_id,
            membership_id=membership_id,
            role_id=owner_role_id,
            membership_version=1,
            capability_version=1,
            assignment_version=1,
        )
        _audit(
            cursor,
            identity,
            action="organization.create",
            entity_type="organization",
            entity_id=org_id,
            after={
                "name": name,
                "entitlement_id": entitlement_id,
                "entitled_seats": config["included_seats"],
                "catalog_version": config["version_code"],
            },
        )
        return get_organization_overview_by_id(cursor, org_id, owner_user_id)


def get_organization_overview_by_id(cursor, organization_id: int, viewer_user_id: int) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT o.*, e.id AS entitlement_id,e.entitled_seats,e.source_sku,
               v.version_code AS product_catalog_version
        FROM organizations o
        JOIN organization_seat_entitlements e ON e.organization_id=o.id
          AND e.status='active' AND e.effective_to IS NULL
        JOIN pricing_catalog_versions v ON v.id=e.product_catalog_version_id
        WHERE o.id=%s
        """,
        (organization_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise OrganizationError("ORG_NOT_FOUND", "团队不存在", http_status=404)
    cursor.execute(
        """
        SELECT COUNT(*) FILTER (WHERE status='active' AND NOT is_owner) AS active_members,
               COUNT(*) FILTER (WHERE status='suspended' AND NOT is_owner) AS suspended_members
        FROM organization_memberships WHERE organization_id=%s
        """,
        (organization_id,),
    )
    counts = cursor.fetchone()
    cursor.execute(
        """
        SELECT COUNT(*) AS pending_invites FROM organization_invites
        WHERE organization_id=%s AND status='pending' AND expires_at>NOW()
        """,
        (organization_id,),
    )
    pending = int(cursor.fetchone()["pending_invites"])
    entitled = int(row["entitled_seats"])
    occupied = int(counts["active_members"]) + int(counts["suspended_members"]) + pending
    return {
        "id": int(row["id"]),
        "owner_user_id": int(row["owner_user_id"]),
        "name": row["name"],
        "status": row["status"],
        "version": int(row["version"]),
        "authority_version": int(row["authority_version"]),
        "entitlement": {
            "id": int(row["entitlement_id"]),
            "entitled_seats": entitled,
            "occupied_seats": occupied,
            "available_seats": max(entitled - occupied, 0),
            "product_catalog_version": row["product_catalog_version"],
            "source_sku": row["source_sku"],
        },
        "viewer_is_owner": int(viewer_user_id) == int(row["owner_user_id"]),
    }


def get_overview(identity: IdentityContext) -> dict[str, Any]:
    with get_db() as conn:
        cursor = conn.cursor()
        locked = _lock_identity(cursor, identity)
        overview = get_organization_overview_by_id(cursor, locked.organization_id, locked.authenticated_user_id)
        overview["identity"] = {
            "actor_kind": locked.actor_kind,
            "membership_id": locked.membership_id,
            "authority_version": locked.authority_version,
            "capabilities": sorted(locked.capabilities),
        }
        brand_ids = assigned_brand_ids(locked, cursor=cursor)
        overview["assigned_brand_ids"] = brand_ids
        # [C2 修复 2026-08-17] 员工端「分配客户」原来显示的是内部客户编号拼接
        # (「3、17、42」),员工完全看不出那是谁。这里补上名字。
        # 只回 id+name,不带任何客户明细 —— 这些正是该员工**已被授权**的客户。
        overview["assigned_brands"] = []
        if brand_ids:
            cursor.execute(
                "SELECT id,name FROM brands WHERE id=ANY(%s) ORDER BY id",
                (list(brand_ids),),
            )
            overview["assigned_brands"] = [
                {"id": int(row["id"]), "name": str(row["name"] or f"客户 #{row['id']}")}
                for row in cursor.fetchall()
            ]
        return overview


def _cancel_invite_onboarding(cursor, invite_ids: Sequence[int], *, reason: str) -> None:
    ids = sorted({int(value) for value in invite_ids})
    if not ids:
        return
    cursor.execute(
        """UPDATE organization_invite_verification_challenges
           SET status='cancelled',updated_at=NOW()
           WHERE invite_id=ANY(%s) AND status IN ('pending','verified')""",
        (ids,),
    )
    cursor.execute(
        """UPDATE organization_invite_delivery_outbox
           SET status='cancelled',claim_token=NULL,lease_expires_at=NULL,
               last_error_code=%s,updated_at=NOW()
           WHERE invite_id=ANY(%s) AND status IN ('pending','retry','sending','unknown')""",
        (str(reason), ids),
    )


def _expire_invites(cursor, organization_id: int) -> int:
    cursor.execute(
        """UPDATE organization_invites
           SET status='expired',updated_at=NOW(),version=version+1
           WHERE organization_id=%s AND status='pending' AND expires_at<=NOW()
           RETURNING id""",
        (organization_id,),
    )
    expired_ids = [int(row["id"]) for row in cursor.fetchall()]
    _cancel_invite_onboarding(cursor, expired_ids, reason="INVITE_EXPIRED")
    return len(expired_ids)

def _lock_entitlement_and_count(cursor, organization_id: int) -> tuple[dict[str, Any], int]:
    cursor.execute(
        """
        SELECT * FROM organization_seat_entitlements
        WHERE organization_id=%s AND status='active' AND effective_to IS NULL
        FOR UPDATE
        """,
        (organization_id,),
    )
    entitlement = cursor.fetchone()
    if not entitlement:
        raise OrganizationError("ORG_SEAT_ENTITLEMENT_MISSING", "团队席位配置异常，请联系平台客服", http_status=503)
    _expire_invites(cursor, organization_id)
    cursor.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM organization_memberships
           WHERE organization_id=%s AND NOT is_owner AND status IN ('active','suspended','leaving'))
          +
          (SELECT COUNT(*) FROM organization_invites
           WHERE organization_id=%s AND status='pending' AND expires_at>NOW()) AS occupied
        """,
        (organization_id, organization_id),
    )
    return dict(entitlement), int(cursor.fetchone()["occupied"])


def _require_invite_product_config(cursor, *, for_update: bool = False) -> dict[str, Any]:
    config = _load_product_config(cursor, for_update=for_update)
    metadata = config["metadata"]
    if not bool(metadata.get("operational")) or int(config["included_seats"]) <= 0:
        raise OrganizationError(
            "ORG_PRODUCT_CONFIG_GOVERNANCE_ONLY",
            "团队已创建，员工席位功能待平台开通，请联系平台客服",
            http_status=409,
            safe_details={"admin_action": "publish_organization_seat_policy"},
        )
    if bool(metadata.get("paid_extra_seats_enabled")) or int(config["extra_seat_price_cents"]) != 0:
        raise OrganizationError(
            "ORG_PAID_EXTRA_SEATS_NOT_APPROVED",
            "席位已用完，请先开通额外席位再邀请",
            http_status=503,
        )
    return config


def _derive_invite_token(row: Mapping[str, Any]) -> Optional[str]:
    derivation_id = row.get("token_derivation_id")
    key_version = row.get("token_key_version")
    if not derivation_id or not key_version:
        return None
    token, _, _ = derive_bearer_token(
        purpose="organization-invite",
        stable_material=str(derivation_id),
        key_version=str(key_version),
    )
    return token


# 投递队列真正能送达的渠道。与 DB 侧
# `organization_invite_delivery_outbox_target_kind_check
#  CHECK (target_kind = ANY (ARRAY['phone','email']))` 一一对应 ——
# 两边任何一边加渠道,另一边必须同步,否则又是一次 CheckViolation。
_DELIVERABLE_TARGET_KINDS = frozenset({"phone", "email"})


def _queue_invite_link_delivery(
    cursor,
    *,
    invite: Mapping[str, Any],
    token: str,
    request_id: str,
) -> bool:
    """把邀请链接排进投递队列。**不可投递的渠道直接跳过并返回 False**。

    [修复 2026-07-28] 守卫下沉到函数内部。原来只有 `create_invite` 的调用点写了
    `if target_kind != "username"`,`resend_invite` 漏了同一条 —— 于是用户名邀请
    一重发就撞 DB 的 CHECK 约束,炸成 psycopg2 异常,再被组织守卫的兜底改写成
    「组织权限校验暂不可用」,现象与真因完全对不上。
    把判断放在调用点,等于要求每个未来的调用者都记得这条;放在这里,忘不掉。
    """
    if str(invite.get("target_kind") or "") not in _DELIVERABLE_TARGET_KINDS:
        # 用户名式邀请没有可投递的联系方式:老板线下把邀请链接 + 登录名发给员工。
        return False
    payload_ciphertext, encryption_version = encrypt_delivery_target("invite_link", token)
    cursor.execute(
        """INSERT INTO organization_invite_delivery_outbox(
             invite_id,event_kind,target_kind,target_hmac,delivery_ciphertext,
             payload_ciphertext,encryption_key_version,request_id
           ) VALUES (%s,'invite_link',%s,%s,%s,%s,%s,%s)
           ON CONFLICT(request_id) DO NOTHING""",
        (
            invite["id"], invite["target_kind"], invite["target_hmac"],
            invite["delivery_ciphertext_or_reference"], payload_ciphertext,
            encryption_version, str(request_id),
        ),
    )
    return True



def _normalize_invite_access_policy(
    *,
    role_code: str,
    role_capabilities: Sequence[str],
    brand_ids: Sequence[int],
    capability_overrides: Mapping[str, str],
    artifact_scope: str,
    daily_limit_points: Optional[int],
    monthly_limit_points: Optional[int],
    feature_limits: Mapping[str, Mapping[str, int]],
    high_risk_confirmed: bool,
    high_risk_reason: Optional[str],
) -> dict[str, Any]:
    normalized_brands = sorted({int(value) for value in brand_ids if int(value) > 0})
    if len(normalized_brands) > 1000:
        raise OrganizationError("ORG_ASSIGNMENT_TOO_LARGE", "单次最多分配 1000 个客户", http_status=422)
    scope = str(artifact_scope or "own").strip()
    if scope not in {"own", "assigned_team"}:
        raise OrganizationError("ORG_ARTIFACT_SCOPE_INVALID", "“可查看内容范围”选择无效，请重新选择", http_status=422)
    overrides = {
        str(capability).strip(): str(effect).strip()
        for capability, effect in dict(capability_overrides or {}).items()
        if str(capability).strip()
    }
    if any(effect not in {"allow", "deny"} for effect in overrides.values()):
        raise OrganizationError("ORG_CAPABILITY_EFFECT_INVALID", "权限开关状态无效", http_status=422)
    validate_capabilities(list(overrides))
    # Object scope is expressed through the existing output-read capability;
    # this does not create a second object ACL vocabulary.
    overrides["team.output_read"] = "allow" if scope == "assigned_team" else "deny"
    role_values = set(validate_capabilities(role_capabilities))
    effective = (role_values | {cap for cap, effect in overrides.items() if effect == "allow"}) - {
        cap for cap, effect in overrides.items() if effect == "deny"
    }
    frozen_template = set(ROLE_TEMPLATE_CAPABILITIES.get(str(role_code), role_values))
    expansions = sorted(effective - frozen_template)
    reason = str(high_risk_reason or "").strip()
    if expansions and (not high_risk_confirmed or not reason):
        raise OrganizationError(
            "ORG_HIGH_RISK_CONFIRMATION_REQUIRED",
            "同时授予销售和交付两类权限属于高风险操作，需再次确认并填写原因",
            http_status=409,
            safe_details={"capabilities": expansions},
        )

    def optional_points(value: Optional[int], field: str) -> Optional[int]:
        if value is None:
            return None
        parsed = int(value)
        if parsed < 0:
            raise OrganizationError("ORG_LIMIT_NEGATIVE", "使用上限不能为负数", http_status=422, safe_details={"field": field})
        return parsed

    daily = optional_points(daily_limit_points, "daily_limit_points")
    monthly = optional_points(monthly_limit_points, "monthly_limit_points")
    normalized_features: dict[str, dict[str, int]] = {}
    for feature_code, raw in sorted(dict(feature_limits or {}).items()):
        feature = str(feature_code or "").strip()
        required_capability = BILLABLE_FEATURE_CAPABILITIES.get(feature)
        if not required_capability:
            raise OrganizationError(
                "ORG_LIMIT_FEATURE_INVALID", "上限设置里包含不存在的功能，请重新选择",
                http_status=422, safe_details={"feature_code": feature},
            )
        if required_capability not in effective:
            raise OrganizationError(
                "ORG_LIMIT_FEATURE_NOT_AUTHORIZED", "不能为未授权功能配置使用上限",
                http_status=422, safe_details={"feature_code": feature},
            )
        values = dict(raw or {})
        feature_daily = optional_points(values.get("daily"), f"{feature}.daily")
        feature_monthly = optional_points(values.get("monthly"), f"{feature}.monthly")
        if feature_daily is None and feature_monthly is None:
            continue
        if feature_daily is not None and daily is None:
            raise OrganizationError("ORG_LIMIT_TOTAL_REQUIRED", "请先配置每日总上限", http_status=409)
        if feature_monthly is not None and monthly is None:
            raise OrganizationError("ORG_LIMIT_TOTAL_REQUIRED", "请先配置每月总上限", http_status=409)
        if feature_daily is not None and daily is not None and feature_daily > daily:
            raise OrganizationError("ORG_LIMIT_FEATURE_EXCEEDS_TOTAL", "功能日上限不能大于每日总上限", http_status=422)
        if feature_monthly is not None and monthly is not None and feature_monthly > monthly:
            raise OrganizationError("ORG_LIMIT_FEATURE_EXCEEDS_TOTAL", "功能月上限不能大于每月总上限", http_status=422)
        normalized_features[feature] = {
            **({"daily": feature_daily} if feature_daily is not None else {}),
            **({"monthly": feature_monthly} if feature_monthly is not None else {}),
        }
    return {
        "version": "invite-access-policy-v1",
        "brand_ids": normalized_brands,
        "capability_overrides": dict(sorted(overrides.items())),
        "artifact_scope": scope,
        "daily_limit_points": daily,
        "monthly_limit_points": monthly,
        "feature_limits": normalized_features,
        "high_risk_expansions": expansions,
        "high_risk_reason": reason or None,
    }


def _validate_invite_brand_scope(
    cursor,
    *,
    principal_user_id: int,
    brand_ids: Sequence[int],
    lock: bool,
) -> None:
    normalized = sorted({int(value) for value in brand_ids})
    if not normalized:
        return
    cursor.execute(
        """
        SELECT id FROM brands
        WHERE id=ANY(%s) AND owner_user_id=%s
          AND COALESCE(is_deleted,FALSE)=FALSE
          AND COALESCE(to_jsonb(brands)->>'status','active')
              NOT IN ('archived','cancelled','terminated','deleted')
        ORDER BY id""" + (" FOR UPDATE" if lock else ""),
        (normalized, principal_user_id),
    )
    owned = {int(row["id"]) for row in cursor.fetchall()}
    missing = sorted(set(normalized) - owned)
    if missing:
        raise OrganizationError(
            "ORG_BRAND_NOT_OWNED", "客户不存在或不在你的团队名下",
            http_status=404, safe_details={"brand_ids": missing},
        )


def _invite_access_policy(invite: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _json_object(invite.get("access_policy_snapshot"))
    expected = str(invite.get("access_policy_hash") or "")
    if payload_hash(snapshot) != expected:
        raise OrganizationError(
            "ORG_INVITE_ACCESS_POLICY_TAMPERED",
            "这条邀请的权限信息已失效，请撤销后重新发一份",
            http_status=409,
        )
    return snapshot


def _apply_invite_access_policy(
    cursor,
    *,
    invite: Mapping[str, Any],
    membership_id: int,
    user_id: int,
    request_id: str,
    identity: IdentityContext,
) -> dict[str, Any]:
    policy = _invite_access_policy(invite)
    _validate_invite_brand_scope(
        cursor,
        principal_user_id=int(invite["owner_user_id"]),
        brand_ids=policy.get("brand_ids") or [],
        lock=True,
    )
    assignment = replace_member_assignments_with_cursor(
        cursor,
        organization_id=int(invite["organization_id"]),
        principal_user_id=int(invite["owner_user_id"]),
        membership_id=int(membership_id),
        brand_ids=policy.get("brand_ids") or [],
        actor_user_id=int(invite["created_by_user_id"]),
        request_id=f"{request_id}:invite-assignment",
        reason="邀请接受时落地老板预配置客户范围",
        audit_identity=identity,
        not_owned_http_status=404,
        audit_action="invite.assignment.apply",
    )
    overrides = dict(policy.get("capability_overrides") or {})
    if overrides:
        cursor.execute(
            "SELECT version FROM organization_memberships WHERE id=%s FOR UPDATE",
            (membership_id,),
        )
        expected_version = int(cursor.fetchone()["version"])
        cursor.executemany(
            """
            INSERT INTO organization_member_capability_overrides(
              membership_id,capability,effect,expected_membership_version,reason,created_by_user_id
            ) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT(membership_id,capability) DO UPDATE
            SET effect=EXCLUDED.effect,expected_membership_version=EXCLUDED.expected_membership_version,
                reason=EXCLUDED.reason,created_by_user_id=EXCLUDED.created_by_user_id,updated_at=NOW()
            """,
            [
                (
                    membership_id,
                    capability,
                    effect,
                    expected_version,
                    "邀请权限预配置" + (
                        f"：{policy['high_risk_reason']}" if policy.get("high_risk_reason") else ""
                    ),
                    int(invite["created_by_user_id"]),
                )
                for capability, effect in sorted(overrides.items())
            ],
        )
        cursor.execute(
            """
            UPDATE organization_memberships
            SET version=version+1,capability_version=capability_version+1,updated_at=NOW()
            WHERE id=%s
            """,
            (membership_id,),
        )
        cursor.execute(
            "UPDATE users SET permission_version=permission_version+1 WHERE id=%s",
            (user_id,),
        )
        cursor.execute(
            "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
            (invite["organization_id"],),
        )

    from services.organization_limits import _period_bounds

    cursor.execute(
        """SELECT o.billing_timezone FROM organizations o
           WHERE o.id=%s FOR UPDATE""",
        (invite["organization_id"],),
    )
    timezone_name = str(cursor.fetchone()["billing_timezone"])
    now = _utcnow()
    totals = (
        ("daily_total", "daily", policy.get("daily_limit_points")),
        ("monthly_total", "monthly", policy.get("monthly_limit_points")),
    )
    limit_rows: list[tuple[str, Optional[str], int]] = []
    for limit_kind, period_type, points in totals:
        if points is not None:
            limit_rows.append((limit_kind, None, int(points)))
    for feature_code, values in sorted(dict(policy.get("feature_limits") or {}).items()):
        if values.get("daily") is not None:
            limit_rows.append(("daily_feature", feature_code, int(values["daily"])))
        if values.get("monthly") is not None:
            limit_rows.append(("monthly_feature", feature_code, int(values["monthly"])))
    for limit_kind, feature_code, points in limit_rows:
        period_type = "daily" if limit_kind.startswith("daily") else "monthly"
        period_start, period_end = _period_bounds(now, timezone_name, period_type)
        cursor.execute(
            """
            INSERT INTO organization_spend_limits(
              organization_id,membership_id,period_type,limit_kind,period_start,period_end,
              period_timezone,feature_code,limit_points,status,policy_version,updated_by_user_id,reason
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',1,%s,%s)
            """,
            (
                invite["organization_id"], membership_id, period_type, limit_kind,
                period_start, period_end, timezone_name, feature_code, points,
                invite["created_by_user_id"], "邀请接受时落地老板预配置算力上限",
            ),
        )
    cursor.execute(
        """SELECT m.version,m.capability_version,m.assignment_version,u.permission_version
           FROM organization_memberships m JOIN users u ON u.id=m.user_id
           WHERE m.id=%s""",
        (membership_id,),
    )
    versions = dict(cursor.fetchone())
    _audit(
        cursor,
        identity,
        action="invite.access_policy.apply",
        entity_type="organization_membership",
        entity_id=membership_id,
        after={
            "access_policy_hash": invite["access_policy_hash"],
            "brand_ids": policy.get("brand_ids") or [],
            "artifact_scope": policy.get("artifact_scope"),
            "high_risk_expansions": policy.get("high_risk_expansions") or [],
            "limit_kinds": [row[0] for row in limit_rows],
        },
        reason=policy.get("high_risk_reason"),
    )
    return {**assignment, **versions, "access_policy_hash": invite["access_policy_hash"]}


def create_invite(
    identity: IdentityContext,
    *,
    target_kind: str,
    target: str,
    role_id: int,
    request_id: str,
    source_ip: str,
    brand_ids: Sequence[int] = (),
    capability_overrides: Mapping[str, str] = {},
    artifact_scope: str = "own",
    daily_limit_points: Optional[int] = None,
    monthly_limit_points: Optional[int] = None,
    feature_limits: Mapping[str, Mapping[str, int]] = {},
    high_risk_confirmed: bool = False,
    high_risk_reason: Optional[str] = None,
) -> dict[str, Any]:
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    normalized = normalize_target(target_kind, target)

    # [P0-B ①] 登录名式邀请:**在创建邀请这一刻**就把重名查清楚。
    #
    # 原先只做格式归一,冲突要等到员工点开链接、填完密码、INSERT users 撞唯一键
    # 才炸(ORG_INVITEE_ACCOUNT_EXISTS)。那时链接已经发出去了,员工撞一鼻子灰,
    # 团队长还不知道为什么。现在提前拦,并且**两种语义分开说**:
    #   - 已被别的团队占用 / 账号停用 → 让团队长换一个;
    #   - 已有账号且自由 → 明确告诉他"将以既有账号加入",走登录后接受邀请那条路。
    if target_kind == "username":
        from services.organization_short_code import (
            CHECK_EXISTING_ACCOUNT, CHECK_TAKEN, check_login_name,
        )

        with get_db() as _precheck_conn:
            _pc = _precheck_conn.cursor()
            _verdict = check_login_name(
                _pc, login_name=normalized, organization_id=identity.organization_id
            )
            _precheck_conn.commit()

        if _verdict["status"] == CHECK_TAKEN:
            raise OrganizationError(
                "ORG_INVITE_LOGIN_NAME_TAKEN",
                _verdict["message"],
                http_status=409,
                safe_details={"login_name": _verdict["login_name"], "reason": _verdict.get("reason")},
            )
        if _verdict["status"] == CHECK_EXISTING_ACCOUNT and not high_risk_confirmed:
            # 不是错误,是**需要团队长确认换一条路**:对方用原密码登录后接受邀请。
            raise OrganizationError(
                "ORG_INVITE_EXISTING_ACCOUNT_CONFIRM",
                _verdict["message"],
                http_status=409,
                safe_details={
                    "login_name": _verdict["login_name"],
                    "hint": _verdict.get("hint"),
                    "confirm_field": "high_risk_confirmed",
                },
            )

    digest, digest_version = target_hmac(target_kind, normalized)
    target_candidates = target_hmac_candidates(target_kind, normalized)
    target_digests = [candidate_digest for candidate_digest, _ in target_candidates]
    target_lock_digest = target_digests[0]
    ciphertext, encryption_version = encrypt_delivery_target(target_kind, normalized)
    derivation_id = str(uuid.uuid4())
    token, token_digest, token_version = derive_bearer_token(
        purpose="organization-invite", stable_material=derivation_id,
    )
    _enforce_invite_rate(
        action="invite.create",
        dimensions=("organization", "actor", "target", "ip"),
        source_ip=source_ip,
        organization_id=identity.organization_id,
        actor_user_id=identity.actor_user_id,
        target_digest=digest,
    )
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-invite:{identity.organization_id}:{request_id}",),
        )
        identity = _lock_identity(cursor, identity, owner_required=True)
        identity.require_active_organization()
        cursor.execute(
            "SELECT * FROM organization_roles WHERE id=%s AND organization_id=%s AND NOT is_owner_role FOR UPDATE",
            (role_id, identity.organization_id),
        )
        role = cursor.fetchone()
        if not role:
            raise OrganizationError("ORG_ROLE_INVALID", "员工角色不存在", http_status=422)
        cursor.execute(
            """SELECT capability FROM organization_role_capabilities
               WHERE role_id=%s AND effect='allow' ORDER BY capability""",
            (role_id,),
        )
        capabilities = [row["capability"] for row in cursor.fetchall()]
        validate_capabilities(capabilities)
        access_policy = _normalize_invite_access_policy(
            role_code=str(role["code"]),
            role_capabilities=capabilities,
            brand_ids=brand_ids,
            capability_overrides=capability_overrides,
            artifact_scope=artifact_scope,
            daily_limit_points=daily_limit_points,
            monthly_limit_points=monthly_limit_points,
            feature_limits=feature_limits,
            high_risk_confirmed=high_risk_confirmed,
            high_risk_reason=high_risk_reason,
        )
        _validate_invite_brand_scope(
            cursor,
            principal_user_id=int(identity.principal_user_id),
            brand_ids=access_policy["brand_ids"],
            lock=True,
        )
        access_policy_digest = payload_hash(access_policy)
        cursor.execute(
            """SELECT i.*,r.name AS role_name FROM organization_invites i
               JOIN organization_roles r ON r.id=i.role_id
               WHERE i.organization_id=%s AND i.request_id=%s FOR UPDATE""",
            (identity.organization_id, request_id),
        )
        replay = cursor.fetchone()
        if replay:
            if (
                replay["target_hmac"] != digest
                or int(replay["role_id"]) != int(role_id)
                or str(replay.get("access_policy_hash") or "") != access_policy_digest
            ):
                raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "操作冲突，请刷新页面后重新发送邀请", http_status=409)
            return {
                "invite": _safe_invite(replay),
                "delivery_token": _derive_invite_token(replay),
                "replayed": True,
            }


        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (stable_key("organization-invite-target", target_kind, target_lock_digest),),
        )
        cursor.execute(
            """UPDATE organization_invites
               SET status='expired',version=version+1,updated_at=NOW()
               WHERE target_kind=%s AND target_hmac=ANY(%s)
                 AND status='pending' AND expires_at<=NOW()
               RETURNING id""",
            (target_kind, target_digests),
        )
        expired_target_invites = [int(row["id"]) for row in cursor.fetchall()]
        _cancel_invite_onboarding(cursor, expired_target_invites, reason="INVITE_EXPIRED")
        cursor.execute(
            """SELECT id FROM organization_invites
               WHERE target_kind=%s AND target_hmac=ANY(%s)
                 AND status='pending' AND expires_at>NOW()
               FOR UPDATE""",
            (target_kind, target_digests),
        )
        if cursor.fetchone():
            raise OrganizationError(
                "ORG_INVITE_ALREADY_PENDING",
                "该联系方式已有待接受的邀请，可在邀请列表里重发或撤销",
                http_status=409,
            )

        config = _require_invite_product_config(cursor, for_update=True)
        entitlement, occupied = _lock_entitlement_and_count(cursor, identity.organization_id)
        if int(entitlement["product_catalog_version_id"]) != int(config["version_id"]):
            raise OrganizationError(
                "ORG_SEAT_ENTITLEMENT_STALE",
                "团队席位配置已变化，请刷新后重试",
                http_status=409,
                retryable=True,
            )
        if occupied >= int(entitlement["entitled_seats"]):
            raise OrganizationError("ORG_SEAT_LIMIT_REACHED", "员工席位已用完，请先移除闲置成员或联系平台增加席位", http_status=409)
        expires_at = _utcnow() + timedelta(hours=config["invite_ttl_hours"])
        try:
            cursor.execute(
                """INSERT INTO organization_invites(
                     organization_id,target_kind,target_hmac,target_hmac_key_version,
                     delivery_ciphertext_or_reference,token_hash,token_key_version,token_derivation_id,
                     role_id,role_version,capability_snapshot_hash,seat_entitlement_id,
                     access_policy_snapshot,access_policy_hash,status,expires_at,
                     request_id,created_by_user_id
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'pending',%s,%s,%s)
                   RETURNING *""",
                (
                    identity.organization_id, target_kind, digest, digest_version,
                    f"{encryption_version}.{ciphertext}", token_digest, token_version, derivation_id,
                    role_id, role["version"], capability_snapshot_hash(capabilities),
                    entitlement["id"], canonical_json(access_policy), access_policy_digest,
                    expires_at, request_id, identity.actor_user_id,
                ),
            )
        except Exception as exc:
            if getattr(exc, "pgcode", None) == "23505":
                raise OrganizationError("ORG_INVITE_ALREADY_PENDING", "该联系方式已有待接受的邀请，可在邀请列表里重发或撤销", http_status=409) from exc
            raise
        invite = dict(cursor.fetchone())
        # [WP6] 用户名式邀请没有可投递的联系方式(owner 线下把邀请链接+用户名给成员),
        # 不入投递队列;短信/邮箱邀请照常排队发链接。
        if target_kind != "username":
            _queue_invite_link_delivery(
                cursor,
                invite=invite,
                token=token,
                request_id=f"invite-link:create:{request_id}",
            )
        _audit(
            cursor, identity, action="invite.create", entity_type="organization_invite",
            entity_id=invite["id"],
            after={
                "target_kind": target_kind,
                "role_id": role_id,
                "expires_at": expires_at,
                "access_policy_hash": access_policy_digest,
                "brand_ids": access_policy["brand_ids"],
                "high_risk_expansions": access_policy["high_risk_expansions"],
            },
        )
        return {"invite": _safe_invite(invite), "delivery_token": token, "replayed": False}

def _invite_login_name(row: Mapping[str, Any]) -> Optional[str]:
    """用户名式邀请的**最终登录名**(如 `jcfemw-qaseat01`),供老板侧列表展示。

    [P0 修复 2026-08-17] 成员表原来只显示「用户名邀请 #16」—— `#16` 是内部邀请 ID,
    对老板毫无意义,而他真正需要的恰恰是「这条邀请对应哪个登录名」(要连同链接一起
    发给员工)。

    只对 `target_kind == 'username'` 解密:那个明文**本来就是老板自己设定的登录名**,
    不是 PII。手机号/邮箱邀请一律返回 None,继续保持密文不出库的既有边界。
    解密失败(密钥轮换等)不抛错 —— 邀请列表不能因为一条老邀请整页打不开。
    """
    if row.get("target_kind") != "username":
        return None
    reference = str(row.get("delivery_ciphertext_or_reference") or "")
    if "." not in reference:
        return None
    key_version, _, ciphertext = reference.partition(".")
    try:
        return decrypt_delivery_target("username", ciphertext, key_version)
    except Exception:
        return None


def _safe_invite(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "organization_id": int(row["organization_id"]),
        "target_kind": row["target_kind"],
        "login_name": _invite_login_name(row),
        "role_id": int(row["role_id"]),
        "status": row["status"],
        "expires_at": row["expires_at"],
        "resend_count": int(row.get("resend_count") or 0),
        "version": int(row["version"]),
        "created_at": row["created_at"],
        "access_policy_hash": row.get("access_policy_hash"),
        "access_policy": _json_object(row.get("access_policy_snapshot")),
    }


def get_invite_link(identity: IdentityContext, *, invite_id: int) -> dict[str, Any]:
    """重新取回一条待接受邀请的链接口令。**只读,不重签、不作废旧链接。**

    [P0 修复 2026-08-17] 生成/重发只把链接写进剪贴板,老板一旦丢了剪贴板
    (微信里粘错窗口、换台机器、手机上操作)就再也拿不回来 —— 只能「重发」,
    而重发会重签 token 把先前发出去的链接作废,员工那边先收到的链接直接失效。

    token 是从 `token_derivation_id` + `token_key_version` **确定性派生**的
    (见 `_derive_invite_token`),所以「查看链接」可以做成纯读:同一条邀请
    永远派生出同一个口令,不改任何状态。
    """
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        _expire_invites(cursor, identity.organization_id)
        cursor.execute(
            "SELECT * FROM organization_invites WHERE id=%s AND organization_id=%s",
            (int(invite_id), identity.organization_id),
        )
        invite = cursor.fetchone()
        if not invite:
            raise OrganizationError("ORG_INVITE_NOT_FOUND", "邀请不存在", http_status=404)
        if invite["status"] != "pending" or invite["expires_at"] <= _utcnow():
            raise OrganizationError(
                "ORG_INVITE_NOT_PENDING",
                "这条邀请已失效，请重新发一份邀请",
                http_status=409,
            )
        token = _derive_invite_token(invite)
        if not token:
            raise OrganizationError(
                "ORG_INVITE_TOKEN_UNAVAILABLE",
                "这条邀请的链接无法找回，请撤销后重新发一份邀请",
                http_status=409,
            )
        return {"invite": _safe_invite(invite), "delivery_token": token}


def list_invites(identity: IdentityContext) -> list[dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        _expire_invites(cursor, identity.organization_id)
        cursor.execute(
            """
            SELECT i.* FROM organization_invites i
            WHERE i.organization_id=%s ORDER BY i.created_at DESC,i.id DESC
            """,
            (identity.organization_id,),
        )
        return [_safe_invite(row) for row in cursor.fetchall()]


def resend_invite(
    identity: IdentityContext,
    *,
    invite_id: int,
    request_id: str,
    source_ip: str,
) -> dict[str, Any]:
    with get_db() as rate_lookup_conn:
        rate_lookup_cursor = rate_lookup_conn.cursor()
        assert_ready(rate_lookup_cursor)
        rate_lookup_cursor.execute(
            "SELECT target_hmac FROM organization_invites WHERE id=%s AND organization_id=%s",
            (invite_id, identity.organization_id),
        )
        rate_lookup = rate_lookup_cursor.fetchone()
    resend_dimensions = ("organization", "actor", "target", "ip") if rate_lookup else ("organization", "actor", "ip")
    _enforce_invite_rate(
        action="invite.resend",
        dimensions=resend_dimensions,
        source_ip=source_ip,
        organization_id=identity.organization_id,
        actor_user_id=identity.actor_user_id,
        target_digest=rate_lookup["target_hmac"] if rate_lookup else None,
    )
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            "SELECT * FROM organization_invites WHERE id=%s AND organization_id=%s FOR UPDATE",
            (invite_id, identity.organization_id),
        )
        invite = cursor.fetchone()
        if not invite:
            raise OrganizationError("ORG_INVITE_NOT_FOUND", "邀请不存在", http_status=404)
        if invite.get("last_resend_request_id") == request_id:
            return {
                "invite": _safe_invite(invite),
                "delivery_token": _derive_invite_token(invite),
                "replayed": True,
            }
        if invite["status"] != "pending" or invite["expires_at"] <= _utcnow():
            raise OrganizationError("ORG_INVITE_NOT_PENDING", "邀请已失效，不能重发", http_status=409)
        config = _require_invite_product_config(cursor, for_update=True)
        derivation_id = str(uuid.uuid4())
        token, token_digest, token_version = derive_bearer_token(
            purpose="organization-invite", stable_material=derivation_id,
        )
        cursor.execute(
            """UPDATE organization_invite_verification_challenges
               SET status='cancelled',updated_at=NOW()
               WHERE invite_id=%s AND status IN ('pending','verified')""",
            (invite_id,),
        )
        cursor.execute(
            """UPDATE organization_invite_delivery_outbox
               SET status='cancelled',claim_token=NULL,lease_expires_at=NULL,
                   last_error_code='INVITE_RESENT',updated_at=NOW()
               WHERE invite_id=%s AND status IN ('pending','retry','sending','unknown')""",
            (invite_id,),
        )
        cursor.execute(
            """UPDATE organization_invites
               SET token_hash=%s,token_key_version=%s,token_derivation_id=%s,
                   expires_at=%s,resend_count=resend_count+1,version=version+1,
                   updated_at=NOW(),last_resend_request_id=%s
               WHERE id=%s RETURNING *""",
            (
                token_digest, token_version, derivation_id,
                _utcnow() + timedelta(hours=config["invite_ttl_hours"]),
                request_id, invite_id,
            ),
        )
        updated = dict(cursor.fetchone())
        # [修复 2026-07-28 · 生产 live bug] 这里原先无条件入投递队列,而
        #   `organization_invite_delivery_outbox` 上有
        #   `CHECK (target_kind = ANY (ARRAY['phone','email']))` ——
        #   用户名式邀请的 target_kind='username' 会直接撞 CheckViolation。
        #   那是 psycopg2 异常而不是 OrganizationError,一路冒到
        #   `middleware/organization_guard.py` 的兜底 except,被伪装成
        #   「组织权限校验暂不可用」(503 retryable),所以现象是"点重发就报权限错"。
        #   创建路径(见 create_invite 的 `if target_kind != "username"`)本来就有这道
        #   守卫并写明"用户名式邀请没有可投递的联系方式",重发路径漏了同一条 —— 两处补齐。
        #   实证:生产 invite #10(username/pending)resend_count 恒 0、version 恒 1
        #   (主事务全数回滚),而 organization_security_rate_events 里 invite.resend 有 8 条
        #   (限流是独立事务,先落库)—— 正好对上"点了很多次、一次没成功"。
        if updated["target_kind"] != "username":
            _queue_invite_link_delivery(
                cursor,
                invite=updated,
                token=token,
                request_id=f"invite-link:resend:{invite_id}:{request_id}",
            )
        _audit(
            cursor, identity, action="invite.resend", entity_type="organization_invite",
            entity_id=invite_id, after=_safe_invite(updated),
        )
        return {"invite": _safe_invite(updated), "delivery_token": token, "replayed": False}

def revoke_invite(identity: IdentityContext, *, invite_id: int, reason: str) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写撤销原因", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            "SELECT * FROM organization_invites WHERE id=%s AND organization_id=%s FOR UPDATE",
            (invite_id, identity.organization_id),
        )
        invite = cursor.fetchone()
        if not invite:
            raise OrganizationError("ORG_INVITE_NOT_FOUND", "邀请不存在", http_status=404)
        if invite["status"] == "revoked":
            return _safe_invite(invite)
        if invite["status"] != "pending":
            raise OrganizationError("ORG_INVITE_NOT_PENDING", "仅待接受邀请可以撤销", http_status=409)
        cursor.execute(
            """
            UPDATE organization_invites SET status='revoked',revoked_at=NOW(),version=version+1,updated_at=NOW()
            WHERE id=%s RETURNING *
            """,
            (invite_id,),
        )
        updated = cursor.fetchone()
        _cancel_invite_onboarding(cursor, [invite_id], reason="INVITE_REVOKED")
        _audit(cursor, identity, action="invite.revoke", entity_type="organization_invite", entity_id=invite_id, before=_safe_invite(invite), after=_safe_invite(updated), reason=reason)
        return _safe_invite(updated)


def _conversion_history(cursor, user_id: int) -> list[str]:
    """Return commercial/asset/funds/public-identity history without guessing columns."""
    checks = (
        ("user_wallets", "user_id", "COALESCE(paid_points,0)<>0 OR COALESCE(bonus_points,0)<>0 OR COALESCE(commission_points,0)<>0 OR COALESCE(total_recharged,0)<>0"),
        ("point_transactions", "user_id", "TRUE"),
        ("brands", "owner_user_id", "TRUE"),
        ("payment_orders", "user_id", "TRUE"),
        ("customer_service_relationships", "customer_user_id", "TRUE"),
        ("service_contracts", "owner_user_id", "TRUE"),
        ("portal_tokens", "owner_user_id", "TRUE"),
        ("public_report_tokens", "owner_user_id", "TRUE"),
        ("withdrawal_requests", "user_id", "TRUE"),
    )
    found: list[str] = []
    for table, column, condition in checks:
        cursor.execute("SELECT to_regclass(%s) AS reg", (table,))
        if not cursor.fetchone()["reg"]:
            continue
        cursor.execute(
            """
            SELECT EXISTS(
              SELECT 1 FROM information_schema.columns
              WHERE table_schema=current_schema() AND table_name=%s AND column_name=%s
            ) AS present
            """,
            (table, column),
        )
        if not cursor.fetchone()["present"]:
            continue
        cursor.execute(f'SELECT EXISTS(SELECT 1 FROM "{table}" WHERE "{column}"=%s AND ({condition})) AS hit', (user_id,))
        if cursor.fetchone()["hit"]:
            found.append(table)
    return found


def _resolve_invite_by_token(cursor, token: str, *, for_update: bool) -> Optional[dict[str, Any]]:
    """Resolve one opaque invite without filtering terminal states.

    Inspect and accept therefore report a stable invitation state instead of
    turning an accepted/revoked token into a misleading generic miss.  The
    token itself remains the unguessable lookup capability.
    """
    cursor.execute("SELECT DISTINCT token_key_version FROM organization_invites")
    digests: list[str] = []
    for candidate in cursor.fetchall() or []:
        try:
            digest, _ = hash_bearer_token(
                purpose="organization-invite", token=token,
                key_version=candidate["token_key_version"],
            )
        except OrganizationError:
            continue
        digests.append(digest)
    if not digests:
        return None
    lock_sql = " FOR UPDATE OF i,o,r" if for_update else ""
    cursor.execute(
        """
        SELECT i.*,o.owner_user_id,o.name AS organization_name,
               o.status AS organization_status,r.name AS role_name,
               r.version AS current_role_version
        FROM organization_invites i
        JOIN organizations o ON o.id=i.organization_id
        JOIN organization_roles r ON r.id=i.role_id
        WHERE i.token_hash=ANY(%s)
        """ + lock_sql,
        (digests,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _invitee_target_status(cursor, invite: Mapping[str, Any], authenticated_user_id: int, *, lock: bool) -> dict[str, Any]:
    verification_column = "phone_verified" if invite["target_kind"] == "phone" else "email_verified"
    required_columns = [verification_column]
    if invite["target_kind"] == "email":
        required_columns.append("email_verified_for")
    cursor.execute(
        """SELECT COUNT(*)=%s AS present FROM information_schema.columns
           WHERE table_schema=current_schema() AND table_name='users' AND column_name=ANY(%s)""",
        (len(required_columns), required_columns),
    )
    if not cursor.fetchone()["present"]:
        raise OrganizationError(
            "ORG_INVITEE_VERIFICATION_UNAVAILABLE", "暂时无法向该联系方式发送验证码，请稍后再试",
            http_status=503,
        )
    verified_value_select = ",email_verified_for" if invite["target_kind"] == "email" else ""
    lock_sql = " FOR UPDATE" if lock else ""
    cursor.execute(
        f"SELECT id,phone,email,is_active,{verification_column} AS target_verified{verified_value_select} "
        f"FROM users WHERE id=%s{lock_sql}",
        (authenticated_user_id,),
    )
    user = cursor.fetchone()
    if not user or not bool(user["is_active"]):
        raise OrganizationError("ORG_INVITEE_INVALID", "当前账号已被停用，无法接受邀请", http_status=403)
    target_value = user.get(invite["target_kind"])
    if not target_value:
        raise OrganizationError("ORG_INVITEE_TARGET_MISMATCH", "当前账号未绑定邀请指定的联系方式", http_status=403)
    normalized = normalize_target(invite["target_kind"], target_value)
    verified = bool(user.get("target_verified"))
    if invite["target_kind"] == "email":
        verified_for = str(user.get("email_verified_for") or "").strip().casefold()
        verified = verified and bool(verified_for) and verified_for == normalized.casefold()
    matched = target_matches(
        invite["target_kind"], normalized, invite["target_hmac"],
        invite["target_hmac_key_version"],
    )
    if not matched:
        raise OrganizationError("ORG_INVITEE_TARGET_MISMATCH", "当前账号与邀请对象不一致", http_status=403)
    return {"user": user, "target_verified": verified, "target_matched": True}


_INVITE_BLOCKERS: dict[str, tuple[str, int]] = {
    "ORG_INVITE_ACCEPTED": ("邀请已被接受", 409),
    "ORG_INVITE_REVOKED": ("邀请已撤回", 410),
    "ORG_INVITE_EXPIRED": ("邀请已过期", 410),
    "ORG_INACTIVE": ("组织当前不可加入", 403),
    "ORG_INVITEE_TARGET_UNVERIFIED": ("请先验证邀请指定的联系方式", 403),
    "ORG_ACCOUNT_CONVERSION_FORBIDDEN": ("该账号不能转换为员工席位", 409),
    "ORG_USER_ALREADY_MEMBER_THIS_ORG": ("当前账号已经加入这个组织", 409),
    "ORG_USER_ALREADY_MEMBER_OTHER_ORG": ("当前账号已属于其他组织", 409),
    "ORG_SEAT_LIMIT_REACHED": ("员工席位已用完，请先移除闲置成员或联系平台增加席位", 409),
    "ORG_INVITE_ROLE_CHANGED": ("邀请角色已变化，请老板重新邀请", 409),
    "ORG_INVITE_CAPABILITIES_CHANGED": ("邀请权限已变化，请老板重新邀请", 409),
}


def _read_entitlement_and_count(cursor, organization_id: int) -> tuple[dict[str, Any], int]:
    cursor.execute(
        """SELECT * FROM organization_seat_entitlements
           WHERE organization_id=%s AND status='active' AND effective_to IS NULL""",
        (organization_id,),
    )
    entitlement = cursor.fetchone()
    if not entitlement:
        raise OrganizationError("ORG_SEAT_ENTITLEMENT_MISSING", "团队席位配置异常，请联系平台客服", http_status=503)
    cursor.execute(
        """SELECT
             (SELECT COUNT(*) FROM organization_memberships
              WHERE organization_id=%s AND NOT is_owner AND status IN ('active','suspended','leaving'))
             +
             (SELECT COUNT(*) FROM organization_invites
              WHERE organization_id=%s AND status='pending' AND expires_at>NOW()) AS occupied""",
        (organization_id, organization_id),
    )
    return dict(entitlement), int(cursor.fetchone()["occupied"])


def _evaluate_invite_acceptance(
    cursor, invite: Mapping[str, Any], authenticated_user_id: int, *, lock: bool,
) -> dict[str, Any]:
    """Single source of truth for inspect and accept eligibility."""
    target = _invitee_target_status(cursor, invite, authenticated_user_id, lock=lock)
    status = str(invite["status"])
    if status == "pending" and invite["expires_at"] <= _utcnow():
        status = "expired"
    blocker: Optional[str] = None
    details: dict[str, Any] = {}
    capabilities: list[str] = []
    if status != "pending":
        blocker = f"ORG_INVITE_{status.upper()}"
    elif invite["organization_status"] != "active":
        blocker = "ORG_INACTIVE"
    elif not target["target_verified"]:
        blocker = "ORG_INVITEE_TARGET_UNVERIFIED"
    if blocker is None:
        cursor.execute(
            """SELECT EXISTS(
                 SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                 WHERE ur.user_id=%s AND r.name='admin'
               ) AS is_admin""",
            (authenticated_user_id,),
        )
        if bool(cursor.fetchone()["is_admin"]):
            blocker = "ORG_ACCOUNT_CONVERSION_FORBIDDEN"
            details["history_categories"] = ["platform_admin"]
    if blocker is None:
        cursor.execute(
            """SELECT id,organization_id FROM organization_memberships
               WHERE user_id=%s AND status IN ('active','suspended','leaving')"""
            + (" FOR UPDATE" if lock else ""),
            (authenticated_user_id,),
        )
        existing = cursor.fetchone()
        if existing:
            blocker = (
                "ORG_USER_ALREADY_MEMBER_THIS_ORG"
                if int(existing["organization_id"]) == int(invite["organization_id"])
                else "ORG_USER_ALREADY_MEMBER_OTHER_ORG"
            )

    if blocker is None:
        if lock:
            entitlement, occupied = _lock_entitlement_and_count(cursor, int(invite["organization_id"]))
        else:
            entitlement, occupied = _read_entitlement_and_count(cursor, int(invite["organization_id"]))
        if occupied > int(entitlement["entitled_seats"]):
            blocker = "ORG_SEAT_LIMIT_REACHED"
            details.update({"occupied": occupied, "entitled_seats": int(entitlement["entitled_seats"])})
    if blocker is None and int(invite["role_version"]) != int(invite["current_role_version"]):
        blocker = "ORG_INVITE_ROLE_CHANGED"
    if blocker is None:
        cursor.execute(
            """SELECT capability FROM organization_role_capabilities
               WHERE role_id=%s AND effect='allow' ORDER BY capability""",
            (invite["role_id"],),
        )
        capabilities = [row["capability"] for row in cursor.fetchall()]
        if capability_snapshot_hash(capabilities) != invite["capability_snapshot_hash"]:
            blocker = "ORG_INVITE_CAPABILITIES_CHANGED"
    if blocker is None:
        _invite_access_policy(invite)
        _validate_invite_brand_scope(
            cursor,
            principal_user_id=int(invite["owner_user_id"]),
            brand_ids=_json_object(invite.get("access_policy_snapshot")).get("brand_ids") or [],
            lock=lock,
        )
    message, http_status = _INVITE_BLOCKERS.get(blocker or "", ("", 409))
    return {
        "status": status, "target": target, "can_accept": blocker is None,
        "blocker_code": blocker, "blocker_message": message,
        "blocker_http_status": http_status, "blocker_details": details,
        "capabilities": capabilities,
    }


def inspect_invite(
    *, authenticated_user_id: int, token: str, request_id: str, source_ip: str,
) -> dict[str, Any]:
    """Authenticated, account-matched invitation status for the accept page."""
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    _enforce_invite_rate(
        # Inspect is part of the same security boundary as accept and therefore
        # consumes the already-versioned accept policy instead of inventing an
        # unconfigured fallback limit.
        action="invite.accept", dimensions=("actor", "ip"), source_ip=source_ip,
        actor_user_id=authenticated_user_id,
    )
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        invite = _resolve_invite_by_token(cursor, token, for_update=False)
        if not invite:
            raise OrganizationError("ORG_INVITE_INVALID", "邀请不存在或已失效", http_status=404)
        evaluation = _evaluate_invite_acceptance(cursor, invite, authenticated_user_id, lock=False)
        target = evaluation["target"]
        return {
            "status": evaluation["status"],
            "organization_id": int(invite["organization_id"]),
            "organization_name": invite["organization_name"],
            "role_name": invite["role_name"],
            "target_kind": invite["target_kind"],
            "expires_at": invite["expires_at"],
            "account_matched": True,
            "target_verified": bool(target["target_verified"]),
            "can_accept": evaluation["can_accept"],
            "blocker_code": evaluation["blocker_code"],
            "blocker_details": evaluation["blocker_details"],
            "request_id": request_id,
        }


def accept_invite(
    *,
    authenticated_user_id: int,
    token: str,
    request_id: str,
    source_ip: str,
) -> dict[str, Any]:
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    _enforce_invite_rate(
        action="invite.accept",
        dimensions=("actor", "ip"),
        source_ip=source_ip,
        actor_user_id=authenticated_user_id,
    )
    rate_subject = _invite_rate_subject(token)
    if rate_subject:
        _enforce_invite_rate(
            action="invite.accept",
            dimensions=("organization", "target"),
            source_ip=source_ip,
            organization_id=rate_subject[0],
            target_digest=rate_subject[1],
        )
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        invite = _resolve_invite_by_token(cursor, token, for_update=True)
        if not invite:
            raise OrganizationError("ORG_INVITE_INVALID", "邀请不存在或已失效", http_status=404)
        accept_hash = payload_hash({
            "invite_id": int(invite["id"]), "authenticated_user_id": int(authenticated_user_id),
            "organization_id": int(invite["organization_id"]), "target_hmac": invite["target_hmac"],
            "access_policy_hash": invite["access_policy_hash"],
        })
        cursor.execute(
            """SELECT invite_id,accepted_user_id,accepted_request_id,
                      accept_request_hash,accepted_result
               FROM organization_invite_accept_receipts
               WHERE accepted_request_id=%s""",
            (request_id,),
        )
        request_receipt = cursor.fetchone()
        if request_receipt and int(request_receipt["invite_id"]) != int(invite["id"]):
            raise OrganizationError(
                "ORG_INVITE_IDEMPOTENCY_CONFLICT",
                "操作冲突，请刷新页面重试",
                http_status=409,
            )
        cursor.execute(
            """SELECT invite_id,accepted_user_id,accepted_request_id,
                      accept_request_hash,accepted_result
               FROM organization_invite_accept_receipts
               WHERE invite_id=%s""",
            (invite["id"],),
        )
        invite_receipt = cursor.fetchone()
        if invite["status"] == "accepted":
            if (
                invite_receipt
                and int(invite_receipt["accepted_user_id"]) == int(authenticated_user_id)
                and invite_receipt["accepted_request_id"] == request_id
                and invite_receipt["accept_request_hash"] == accept_hash
            ):
                result = _json_object(invite_receipt.get("accepted_result"))
                if result:
                    return {**result, "replayed": True}
            raise OrganizationError(
                "ORG_INVITE_IDEMPOTENCY_CONFLICT",
                "邀请已由其他账号或请求接受",
                http_status=409,
            )
        if invite_receipt:
            raise OrganizationError(
                "ORG_INVITE_IDEMPOTENCY_CONFLICT",
                "邀请状态异常，请让团队负责人重新邀请",
                http_status=409,
            )
        evaluation = _evaluate_invite_acceptance(cursor, invite, authenticated_user_id, lock=True)
        if not evaluation["can_accept"]:
            raise OrganizationError(
                evaluation["blocker_code"], evaluation["blocker_message"],
                http_status=evaluation["blocker_http_status"],
                safe_details=evaluation["blocker_details"],
            )
        capabilities = evaluation["capabilities"]
        cursor.execute(
            """
            INSERT INTO organization_memberships(organization_id,user_id,role_id,status,is_owner)
            VALUES (%s,%s,%s,'active',FALSE) RETURNING *
            """,
            (invite["organization_id"], authenticated_user_id, invite["role_id"]),
        )
        membership = dict(cursor.fetchone())
        access_snapshot = snapshot_and_clear_legacy_access(
            cursor,
            organization_id=int(invite["organization_id"]),
            membership_id=int(membership["id"]),
            user_id=int(authenticated_user_id),
            dedicated_operator=False,
        )
        provisional_identity = IdentityContext(
            request_id=request_id,
            authenticated_user_id=authenticated_user_id,
            principal_user_id=int(invite["owner_user_id"]),
            payer_user_id=int(invite["owner_user_id"]),
            actor_kind="member",
            organization_id=int(invite["organization_id"]),
            actor_user_id=authenticated_user_id,
            membership_id=int(membership["id"]),
            role_id=int(invite["role_id"]),
            membership_version=1,
            capability_version=1,
            assignment_version=1,
            capabilities=frozenset(capabilities),
        )
        applied_policy = _apply_invite_access_policy(
            cursor,
            invite=invite,
            membership_id=int(membership["id"]),
            user_id=int(authenticated_user_id),
            request_id=request_id,
            identity=provisional_identity,
        )
        accepted_result = {
            "membership_id": int(membership["id"]),
            "organization_id": int(invite["organization_id"]),
            "status": "active",
            "permission_version": int(applied_policy["permission_version"]),
            "access_policy_hash": invite["access_policy_hash"],
        }
        cursor.execute(
            """INSERT INTO organization_invite_accept_receipts(
                   invite_id,accepted_user_id,accepted_request_id,
                   accept_request_hash,accepted_result
               ) VALUES (%s,%s,%s,%s,%s)""",
            (invite["id"], int(authenticated_user_id), request_id, accept_hash,
             json.dumps(accepted_result, ensure_ascii=False)),
        )
        cursor.execute(
            """
            UPDATE organization_invites
            SET status='accepted',accepted_membership_id=%s,
                accepted_at=NOW(),version=version+1,updated_at=NOW()
            WHERE id=%s
            """,
            (membership["id"], invite["id"]),
        )
        _cancel_invite_onboarding(cursor, [int(invite["id"])], reason="INVITE_ACCEPTED")
        cursor.execute(
            "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
            (invite["organization_id"],),
        )
        identity = IdentityContext(
            request_id=request_id,
            authenticated_user_id=authenticated_user_id,
            principal_user_id=int(invite["owner_user_id"]),
            payer_user_id=int(invite["owner_user_id"]),
            actor_kind="member",
            organization_id=int(invite["organization_id"]),
            actor_user_id=authenticated_user_id,
            membership_id=int(membership["id"]),
            role_id=int(invite["role_id"]),
            membership_version=int(applied_policy["version"]),
            capability_version=int(applied_policy["capability_version"]),
            assignment_version=int(applied_policy["assignment_version"]),
            capabilities=frozenset(capabilities),
        )
        _audit(
            cursor,
            identity,
            action="invite.accept",
            entity_type="organization_membership",
            entity_id=membership["id"],
            after={
                "invite_id": invite["id"],
                "role_id": invite["role_id"],
                "access_policy_hash": invite["access_policy_hash"],
            },
        )
        return {**accepted_result, "replayed": False}


def list_roles(identity: IdentityContext) -> list[dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        member_filter = "" if identity.is_owner else " AND r.id=%s"
        params: tuple[Any, ...] = (identity.organization_id,) if identity.is_owner else (identity.organization_id, identity.role_id)
        cursor.execute(
            """
            SELECT r.*,COALESCE(jsonb_agg(c.capability ORDER BY c.capability)
              FILTER (WHERE c.effect='allow'),'[]'::jsonb) AS capabilities
            FROM organization_roles r LEFT JOIN organization_role_capabilities c ON c.role_id=r.id
            WHERE r.organization_id=%s""" + member_filter + """ GROUP BY r.id ORDER BY r.is_owner_role DESC,r.id
            """,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]



def _role_high_risk_expansions(
    *,
    role_code: str,
    capabilities: Sequence[str],
) -> list[str]:
    values = set(validate_capabilities(capabilities))
    template = ROLE_TEMPLATE_CAPABILITIES.get(str(role_code))
    if template is not None:
        return sorted(values - set(template))
    sales_only = set(ROLE_TEMPLATE_CAPABILITIES["sales"]) - set(ROLE_TEMPLATE_CAPABILITIES["delivery"])
    delivery_only = set(ROLE_TEMPLATE_CAPABILITIES["delivery"]) - set(ROLE_TEMPLATE_CAPABILITIES["sales"])
    if values & sales_only and values & delivery_only:
        return sorted((values & sales_only) | (values & delivery_only))
    return []


def _require_role_risk_confirmation(
    *,
    role_code: str,
    capabilities: Sequence[str],
    high_risk_confirmed: bool,
    high_risk_reason: Optional[str],
) -> tuple[list[str], Optional[str]]:
    expansions = _role_high_risk_expansions(role_code=role_code, capabilities=capabilities)
    reason = str(high_risk_reason or "").strip() or None
    if expansions and (not high_risk_confirmed or not reason):
        raise OrganizationError(
            "ORG_HIGH_RISK_CONFIRMATION_REQUIRED",
            "同时授予销售和交付两类权限属于高风险操作，需再次确认并填写原因",
            http_status=409,
            safe_details={"capabilities": expansions},
        )
    return expansions, reason


def create_role(
    identity: IdentityContext,
    *,
    code: str,
    name: str,
    capabilities: Sequence[str],
    high_risk_confirmed: bool = False,
    high_risk_reason: Optional[str] = None,
) -> dict[str, Any]:
    code = str(code or "").strip().lower()
    name = str(name or "").strip()
    values = validate_capabilities(capabilities)
    expansions, risk_reason = _require_role_risk_confirmation(
        role_code=code,
        capabilities=values,
        high_risk_confirmed=high_risk_confirmed,
        high_risk_reason=high_risk_reason,
    )
    if not code or not name:
        raise OrganizationError("ORG_ROLE_INVALID", "角色标识和名称不能为空", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            """
            INSERT INTO organization_roles(organization_id,code,name,created_by_user_id)
            VALUES (%s,%s,%s,%s) RETURNING *
            """,
            (identity.organization_id, code, name, identity.actor_user_id),
        )
        role = dict(cursor.fetchone())
        cursor.executemany(
            "INSERT INTO organization_role_capabilities(role_id,capability,effect,created_by_user_id) VALUES (%s,%s,'allow',%s)",
            [(role["id"], capability, identity.actor_user_id) for capability in sorted(values)],
        )
        cursor.execute("UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s", (identity.organization_id,))
        role["capabilities"] = sorted(values)
        role["high_risk_expansions"] = expansions
        _audit(
            cursor, identity, action="role.create", entity_type="organization_role",
            entity_id=role["id"], after=role, reason=risk_reason,
        )
        return role


def update_role(
    identity: IdentityContext,
    *,
    role_id: int,
    expected_version: int,
    name: str,
    capabilities: Sequence[str],
    high_risk_confirmed: bool = False,
    high_risk_reason: Optional[str] = None,
) -> dict[str, Any]:
    values = validate_capabilities(capabilities)
    name = str(name or "").strip()
    if not name:
        raise OrganizationError("ORG_ROLE_INVALID", "角色名称不能为空", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            "SELECT * FROM organization_roles WHERE id=%s AND organization_id=%s FOR UPDATE",
            (role_id, identity.organization_id),
        )
        role = cursor.fetchone()
        if not role:
            raise OrganizationError("ORG_ROLE_NOT_FOUND", "角色不存在", http_status=404)
        if role["is_owner_role"]:
            raise OrganizationError("ORG_OWNER_ROLE_IMMUTABLE", "老板角色不能委派或修改", http_status=409)
        if int(role["version"]) != int(expected_version):
            raise OrganizationError("ORG_VERSION_CONFLICT", "角色已被他人修改，请刷新", http_status=409, retryable=True)
        expansions, risk_reason = _require_role_risk_confirmation(
            role_code=str(role["code"]),
            capabilities=values,
            high_risk_confirmed=high_risk_confirmed,
            high_risk_reason=high_risk_reason,
        )
        cursor.execute("SELECT capability FROM organization_role_capabilities WHERE role_id=%s AND effect='allow'", (role_id,))
        before_caps = sorted(row["capability"] for row in cursor.fetchall())
        cursor.execute("DELETE FROM organization_role_capabilities WHERE role_id=%s", (role_id,))
        cursor.executemany(
            "INSERT INTO organization_role_capabilities(role_id,capability,effect,created_by_user_id) VALUES (%s,%s,'allow',%s)",
            [(role_id, capability, identity.actor_user_id) for capability in sorted(values)],
        )
        cursor.execute("UPDATE organization_roles SET name=%s,version=version+1,updated_at=NOW() WHERE id=%s RETURNING *", (name, role_id))
        updated = dict(cursor.fetchone())
        cursor.execute("UPDATE organization_memberships SET capability_version=capability_version+1,version=version+1,updated_at=NOW() WHERE role_id=%s", (role_id,))
        cursor.execute("UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s", (identity.organization_id,))
        # [单1 · revoke 时效] 组织能力现在会被推导成平台权限串(见
        # auth/organization_module_derivation),而平台权限串会进 JWT / 权限缓存。
        # 只 bump 组织内部版本号不足以让它失效 —— 必须同时 rotate
        # users.permission_version,否则改完角色最长仍有约 5 分钟的旧权限窗口。
        _rotate_permission_version_for_role(cursor, role_id=role_id)
        updated["capabilities"] = sorted(values)
        updated["high_risk_expansions"] = expansions
        _audit(
            cursor, identity, action="role.update", entity_type="organization_role",
            entity_id=role_id,
            before={"name": role["name"], "capabilities": before_caps},
            after=updated, reason=risk_reason,
        )
        return updated


def set_member_overrides(
    identity: IdentityContext,
    *,
    membership_id: int,
    expected_membership_version: int,
    overrides: Mapping[str, str],
    reason: str,
    high_risk_confirmed: bool = False,
    high_risk_reason: Optional[str] = None,
) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写权限变更原因", http_status=422)
    validate_capabilities(list(overrides))
    if any(effect not in {"allow", "deny"} for effect in overrides.values()):
        raise OrganizationError("ORG_OVERRIDE_INVALID", "权限设置的取值无效，请重新选择", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            "SELECT * FROM organization_memberships WHERE id=%s AND organization_id=%s FOR UPDATE",
            (membership_id, identity.organization_id),
        )
        member = cursor.fetchone()
        if not member:
            raise OrganizationError("ORG_MEMBER_NOT_FOUND", "员工不存在", http_status=404)
        if member["is_owner"]:
            raise OrganizationError("ORG_OWNER_ROLE_IMMUTABLE", "老板权限不能修改", http_status=409)
        if int(member["version"]) != int(expected_membership_version):
            raise OrganizationError("ORG_VERSION_CONFLICT", "员工权限已变化，请刷新", http_status=409, retryable=True)
        cursor.execute(
            """SELECT r.code,c.capability FROM organization_roles r
               LEFT JOIN organization_role_capabilities c
                 ON c.role_id=r.id AND c.effect='allow'
               WHERE r.id=%s""",
            (member["role_id"],),
        )
        role_rows = list(cursor.fetchall())
        role_code = str(role_rows[0]["code"]) if role_rows else ""
        role_caps = {row["capability"] for row in role_rows if row.get("capability")}
        effective = (role_caps | {cap for cap, effect in overrides.items() if effect == "allow"}) - {
            cap for cap, effect in overrides.items() if effect == "deny"
        }
        expansions, risk_reason = _require_role_risk_confirmation(
            role_code=role_code,
            capabilities=effective,
            high_risk_confirmed=high_risk_confirmed,
            high_risk_reason=high_risk_reason,
        )
        cursor.execute("DELETE FROM organization_member_capability_overrides WHERE membership_id=%s", (membership_id,))
        cursor.executemany(
            """
            INSERT INTO organization_member_capability_overrides(
              membership_id,capability,effect,expected_membership_version,reason,created_by_user_id
            ) VALUES (%s,%s,%s,%s,%s,%s)
            """,
            [(membership_id, capability, effect, expected_membership_version, reason, identity.actor_user_id) for capability, effect in sorted(overrides.items())],
        )
        cursor.execute("UPDATE organization_memberships SET version=version+1,capability_version=capability_version+1,updated_at=NOW() WHERE id=%s RETURNING version,capability_version", (membership_id,))
        version = dict(cursor.fetchone())
        cursor.execute("UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s", (identity.organization_id,))
        # [单1 · revoke 时效] 成员级 override 是 deny 的唯一来源(deny > allow 就在这层),
        # 收回授权必须立刻反映到平台权限串上,不能等缓存自然过期。
        _rotate_permission_version_for_membership(cursor, membership_id=membership_id)
        result = {
            "membership_id": membership_id,
            "overrides": dict(overrides),
            "high_risk_expansions": expansions,
            **version,
        }
        _audit(
            cursor, identity, action="member.capabilities.override",
            entity_type="organization_membership", entity_id=membership_id,
            after=result,
            reason=(f"{reason}；高风险授权原因：{risk_reason}" if risk_reason else reason),
        )
        return result


def list_members(identity: IdentityContext) -> list[dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        can_read_team = identity.is_owner or "team.output_read" in identity.capabilities
        member_filter = "" if can_read_team else " AND m.id=%s"
        params: tuple[Any, ...] = (identity.organization_id,) if can_read_team else (identity.organization_id, identity.membership_id)
        cursor.execute(
            """
            SELECT m.id,m.user_id,m.status,m.is_owner,m.version,m.capability_version,
                   m.assignment_version,m.joined_at,u.display_name,u.avatar_url,
                   -- [P0-B ④] 席位列表要显示**完整登录名**并支持一键复制:
                   -- 团队长发邀请时看到的是登录名,事后在列表里找不到就只能靠记,
                   -- 记错一位员工就登不进来。这里只暴露本团队成员自己的登录名。
                   u.username AS login_name,
                   r.id AS role_id,
                   r.name AS role_name,r.code AS role_code,
                   COALESCE(jsonb_agg(a.brand_id ORDER BY a.brand_id)
                     FILTER (WHERE a.status='active'),'[]'::jsonb) AS assigned_brand_ids
            FROM organization_memberships m JOIN users u ON u.id=m.user_id
            JOIN organization_roles r ON r.id=m.role_id
            LEFT JOIN organization_brand_assignments a ON a.membership_id=m.id
            WHERE m.organization_id=%s AND m.status<>'left' AND m.status<>'removed'""" + member_filter + """
            GROUP BY m.id,u.id,r.id ORDER BY m.is_owner DESC,m.joined_at,m.id
            """,
            params,
        )
        rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            if row["is_owner"]:
                row["effective_capabilities"] = []
                row["capability_overrides"] = {}
                continue
            cursor.execute(
                """
                SELECT capability,effect,0 AS precedence
                FROM organization_role_capabilities WHERE role_id=%s
                UNION ALL
                SELECT capability,effect,1 AS precedence
                FROM organization_member_capability_overrides WHERE membership_id=%s
                ORDER BY capability,precedence
                """,
                (row["role_id"], row["id"]),
            )
            effects: dict[str, str] = {}
            overrides: dict[str, str] = {}
            for effect_row in cursor.fetchall():
                effects[str(effect_row["capability"])] = str(effect_row["effect"])
                if int(effect_row["precedence"]) == 1:
                    overrides[str(effect_row["capability"])] = str(effect_row["effect"])
            row["effective_capabilities"] = sorted(
                capability for capability, effect in effects.items() if effect == "allow"
            )
            row["capability_overrides"] = dict(sorted(overrides.items()))
        return rows


def set_member_status(
    identity: IdentityContext,
    *,
    membership_id: int,
    action: str,
    reason: str,
) -> dict[str, Any]:
    if action not in {"suspend", "resume", "remove"}:
        raise OrganizationError("ORG_MEMBER_ACTION_INVALID", "员工状态操作无效", http_status=422)
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写操作原因", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            "SELECT * FROM organization_memberships WHERE id=%s AND organization_id=%s FOR UPDATE",
            (membership_id, identity.organization_id),
        )
        member = cursor.fetchone()
        if not member:
            raise OrganizationError("ORG_MEMBER_NOT_FOUND", "员工不存在", http_status=404)
        if member["is_owner"]:
            raise OrganizationError("ORG_OWNER_MEMBERSHIP_IMMUTABLE", "老板席位不能暂停或移除", http_status=409)
        target = {"suspend": "suspended", "resume": "active", "remove": "removed"}[action]
        valid = {"suspend": {"active"}, "resume": {"suspended"}, "remove": {"active", "suspended", "leaving"}}[action]
        if member["status"] == target:
            cursor.execute("SELECT permission_version FROM users WHERE id=%s", (member["user_id"],))
            permission = cursor.fetchone()
            return {
                "membership_id": membership_id,
                "status": target,
                "version": int(member["version"]),
                "permission_version": int(permission["permission_version"] or 0),
            }
        if member["status"] not in valid:
            raise OrganizationError("ORG_MEMBER_STATE_CONFLICT", "当前员工状态不能执行此操作", http_status=409)
        time_column = {"suspend": "suspended_at", "resume": None, "remove": "removed_at"}[action]
        assignments_revoked = 0
        if action == "remove":
            cursor.execute(
                """UPDATE organization_brand_assignments
                   SET status='revoked',revoked_at=NOW(),version=version+1,reason=%s
                   WHERE membership_id=%s AND status='active'""",
                (reason, membership_id),
            )
            assignments_revoked = int(cursor.rowcount)
        if action in {"suspend", "remove"}:
            cursor.execute("DELETE FROM user_clients WHERE user_id=%s", (member["user_id"],))
        if action == "resume":
            cursor.execute(
                """UPDATE organization_memberships
                   SET status='active',suspended_at=NULL,version=version+1,
                       capability_version=capability_version+1,
                       assignment_version=assignment_version+1,reason=%s,updated_at=NOW()
                   WHERE id=%s RETURNING *""",
                (reason, membership_id),
            )
            updated = cursor.fetchone()
            cursor.execute(
                """INSERT INTO user_clients(user_id,brand_id)
                   SELECT %s,a.brand_id FROM organization_brand_assignments a
                   WHERE a.membership_id=%s AND a.status='active'
                   ON CONFLICT(user_id,brand_id) DO NOTHING""",
                (member["user_id"], membership_id),
            )
        else:
            cursor.execute(
                f"""UPDATE organization_memberships
                    SET status=%s,{time_column}=NOW(),removed_by_user_id=%s,
                        version=version+1,capability_version=capability_version+1,
                        assignment_version=assignment_version+1,reason=%s,updated_at=NOW()
                    WHERE id=%s RETURNING *""",
                (target, identity.actor_user_id if action == "remove" else None, reason, membership_id),
            )
            updated = cursor.fetchone()
        if action == "remove":
            lifecycle = restore_legacy_access_or_retire_operator(
                cursor, membership_id=membership_id, reason=reason,
            )
            permission_version = int(lifecycle["permission_version"])
        else:
            permission_version = rotate_permission_version(cursor, user_id=int(member["user_id"]))
        cursor.execute(
            "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
            (identity.organization_id,),
        )
        result = {
            "membership_id": membership_id,
            "status": updated["status"],
            "version": int(updated["version"]),
            "permission_version": permission_version,
            "assignments_revoked": assignments_revoked,
        }
        _audit(
            cursor, identity, action=f"member.{action}", entity_type="organization_membership",
            entity_id=membership_id, before={"status": member["status"]}, after=result, reason=reason,
        )
        return result

def leave_organization(identity: IdentityContext, *, reason: str) -> dict[str, Any]:
    if identity.is_owner:
        raise OrganizationError("ORG_OWNER_CANNOT_LEAVE", "老板不能退出自己的团队", http_status=409)
    reason = str(reason or "").strip() or "员工主动退出"
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        cursor.execute(
            """UPDATE organization_brand_assignments
               SET status='revoked',revoked_at=NOW(),version=version+1,reason=%s
               WHERE membership_id=%s AND status='active'""",
            (reason, identity.membership_id),
        )
        revoked = int(cursor.rowcount)
        cursor.execute("DELETE FROM user_clients WHERE user_id=%s", (identity.actor_user_id,))
        cursor.execute(
            """SELECT COUNT(*) AS active_count FROM organization_charge_links
               WHERE membership_id=%s AND status IN ('reserved','refund_pending','unknown')""",
            (identity.membership_id,),
        )
        active_count = int(cursor.fetchone()["active_count"])
        target_status = "leaving" if active_count else "left"
        left_sql = "NULL" if active_count else "NOW()"
        cursor.execute(
            f"""UPDATE organization_memberships
                SET status=%s,leaving_at=COALESCE(leaving_at,NOW()),left_at={left_sql},
                    version=version+1,capability_version=capability_version+1,
                    assignment_version=assignment_version+1,reason=%s,updated_at=NOW()
                WHERE id=%s RETURNING version""",
            (target_status, reason, identity.membership_id),
        )
        version = int(cursor.fetchone()["version"])
        if active_count:
            permission_version = rotate_permission_version(cursor, user_id=int(identity.actor_user_id))
            disposition = "deferred"
        else:
            lifecycle = restore_legacy_access_or_retire_operator(
                cursor, membership_id=int(identity.membership_id), reason=reason,
            )
            permission_version = int(lifecycle["permission_version"])
            disposition = str(lifecycle["disposition"])
        cursor.execute(
            "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
            (identity.organization_id,),
        )
        result = {
            "membership_id": identity.membership_id,
            "status": target_status,
            "version": version,
            "permission_version": permission_version,
            "legacy_access_disposition": disposition,
            "assignments_revoked": revoked,
            "active_charges": active_count,
        }
        _audit(
            cursor, identity, action="member.leave", entity_type="organization_membership",
            entity_id=identity.membership_id, after=result, reason=reason,
        )
        return result

def replace_member_assignments_with_cursor(
    cursor,
    *,
    organization_id: int,
    principal_user_id: int,
    membership_id: int,
    brand_ids: Sequence[int],
    actor_user_id: int,
    request_id: str,
    reason: str,
    expected_assignment_version: Optional[int] = None,
    audit_identity: Optional[IdentityContext] = None,
    not_owned_http_status: int = 403,
    audit_action: str = "assignment.replace",
) -> dict[str, Any]:
    """Replace assignments inside a caller-owned organization transaction.

    The organization assignment table stays authoritative. ``user_clients``
    is updated only as a compatibility projection. Callers lock the
    organization first; this helper then locks membership and user in the
    global authority order and never commits or rolls back.
    """
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写分配原因", http_status=422)
    normalized = sorted({int(value) for value in brand_ids if int(value) > 0})
    cursor.execute(
        """
        SELECT * FROM organization_memberships
        WHERE id=%s AND organization_id=%s FOR UPDATE
        """,
        (membership_id, organization_id),
    )
    member = cursor.fetchone()
    if not member or member["is_owner"] or member["status"] != "active":
        raise OrganizationError("ORG_MEMBER_INVALID", "员工不存在或当前不可分配客户", http_status=422)
    cursor.execute("SELECT id,permission_version FROM users WHERE id=%s FOR UPDATE", (member["user_id"],))
    locked_user = cursor.fetchone()
    if not locked_user:
        raise OrganizationError("ORG_MEMBER_INVALID", "员工账号不存在", http_status=422)
    identity = audit_identity or _system_identity(
        organization_id=organization_id,
        owner_user_id=principal_user_id,
        request_id=request_id,
    )
    audit_key = stable_key(
        identity.organization_id,
        identity.request_id,
        audit_action,
        "organization_membership",
        membership_id,
        0,
    )
    cursor.execute(
        "SELECT after_snapshot FROM organization_audit_events WHERE audit_event_key=%s",
        (audit_key,),
    )
    replay = cursor.fetchone()
    if replay:
        prior = _json_object(replay.get("after_snapshot"))
        prior_ids = sorted(int(value) for value in prior.get("brand_ids", []))
        if prior_ids != normalized:
            raise OrganizationError(
                "ORG_IDEMPOTENCY_CONFLICT",
                "操作冲突，请刷新后重试",
                http_status=409,
            )
        cursor.execute(
            """
            SELECT brand_id FROM organization_brand_assignments
            WHERE membership_id=%s AND status='active' ORDER BY brand_id
            """,
            (membership_id,),
        )
        current_ids = [int(row["brand_id"]) for row in cursor.fetchall()]
        if current_ids != prior_ids:
            raise OrganizationError(
                "ORG_IDEMPOTENCY_REPLAY_SUPERSEDED",
                "分配已被更新过，请刷新查看最新客户范围",
                http_status=409,
                safe_details={"original_brand_ids": prior_ids},
            )
        cursor.execute(
            "SELECT authority_version FROM organizations WHERE id=%s",
            (organization_id,),
        )
        return {
            "membership_id": membership_id,
            "brand_ids": current_ids,
            "assignment_version": int(member["assignment_version"]),
            "version": int(member["version"]),
            "permission_version": int(locked_user["permission_version"] or 0),
            "organization_authority_version": int(cursor.fetchone()["authority_version"]),
            "replayed": True,
        }
    if expected_assignment_version is not None and int(member["assignment_version"]) != int(expected_assignment_version):
        raise OrganizationError(
            "ORG_ASSIGNMENT_VERSION_CONFLICT",
            "客户范围已被他人更新，请刷新后重试",
            http_status=409,
            retryable=True,
            safe_details={"current_version": int(member["assignment_version"])},
        )
    if normalized:
        cursor.execute(
            """
            SELECT id FROM brands
            WHERE id=ANY(%s) AND owner_user_id=%s
              AND COALESCE(is_deleted,FALSE)=FALSE
              AND COALESCE(to_jsonb(brands)->>'status','active')
                  NOT IN ('archived','cancelled','terminated','deleted')
            ORDER BY id FOR UPDATE
            """,
            (normalized, principal_user_id),
        )
        owned = {int(row["id"]) for row in cursor.fetchall()}
        missing = sorted(set(normalized) - owned)
        if missing:
            raise OrganizationError(
                "ORG_BRAND_NOT_OWNED",
                "客户不存在或不在你的团队名下",
                http_status=not_owned_http_status,
                safe_details={"brand_ids": missing},
            )
    cursor.execute(
        """
        UPDATE organization_brand_assignments
        SET status='revoked',revoked_at=NOW(),version=version+1,reason=%s
        WHERE membership_id=%s AND status='active' AND NOT (brand_id=ANY(%s))
        """,
        (reason, membership_id, normalized or [-1]),
    )
    cursor.execute(
        """
        SELECT brand_id FROM organization_brand_assignments
        WHERE membership_id=%s AND status='active'
        """,
        (membership_id,),
    )
    current = {int(row["brand_id"]) for row in cursor.fetchall()}
    for sequence, brand_id in enumerate(sorted(set(normalized) - current)):
        child_request = request_id if sequence == 0 else f"{request_id}:{sequence}"
        cursor.execute(
            """
            INSERT INTO organization_brand_assignments(
              organization_id,membership_id,brand_id,status,assigned_by_user_id,reason,request_id
            ) VALUES (%s,%s,%s,'active',%s,%s,%s)
            """,
            (organization_id, membership_id, brand_id, actor_user_id, reason, child_request),
        )
    cursor.execute("DELETE FROM user_clients WHERE user_id=%s", (member["user_id"],))
    if normalized:
        cursor.executemany(
            """
            INSERT INTO user_clients(user_id,brand_id) VALUES (%s,%s)
            ON CONFLICT(user_id,brand_id) DO NOTHING
            """,
            [(member["user_id"], brand_id) for brand_id in normalized],
        )
    cursor.execute(
        """
        UPDATE organization_memberships
        SET assignment_version=assignment_version+1,version=version+1,updated_at=NOW()
        WHERE id=%s RETURNING assignment_version,version
        """,
        (membership_id,),
    )
    versions = dict(cursor.fetchone())
    cursor.execute(
        """
        UPDATE users SET permission_version=permission_version+1
        WHERE id=%s RETURNING permission_version
        """,
        (member["user_id"],),
    )
    permission_version = int(cursor.fetchone()["permission_version"])
    cursor.execute(
        """
        UPDATE organizations
        SET authority_version=authority_version+1,updated_at=NOW()
        WHERE id=%s RETURNING authority_version
        """,
        (organization_id,),
    )
    authority_version = int(cursor.fetchone()["authority_version"])
    result = {
        "membership_id": membership_id,
        "brand_ids": normalized,
        **versions,
        "permission_version": permission_version,
        "organization_authority_version": authority_version,
        "replayed": False,
    }
    _audit(
        cursor,
        identity,
        action=audit_action,
        entity_type="organization_membership",
        entity_id=membership_id,
        after={key: value for key, value in result.items() if not key.startswith("_")},
        reason=reason,
    )
    return result


def assign_brands(
    identity: IdentityContext,
    *,
    membership_id: int,
    brand_ids: Sequence[int],
    request_id: str,
    reason: str,
) -> dict[str, Any]:
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        return replace_member_assignments_with_cursor(
            cursor,
            organization_id=int(identity.organization_id),
            principal_user_id=int(identity.principal_user_id),
            membership_id=int(membership_id),
            brand_ids=brand_ids,
            actor_user_id=int(identity.actor_user_id),
            request_id=request_id,
            reason=reason,
            audit_identity=identity,
        )

def handoff_brands(
    identity: IdentityContext,
    *,
    from_membership_id: int,
    to_membership_id: int,
    brand_ids: Sequence[int],
    request_id: str,
    reason: str,
) -> dict[str, Any]:
    if from_membership_id == to_membership_id:
        raise OrganizationError("ORG_HANDOFF_SAME_MEMBER", "交接双方不能相同", http_status=422)
    brand_set = sorted({int(value) for value in brand_ids})
    reason = str(reason or "").strip()
    if not brand_set or not reason:
        raise OrganizationError("ORG_HANDOFF_INVALID", "请选择客户并填写交接原因", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        ordered = sorted([from_membership_id, to_membership_id])
        cursor.execute("SELECT * FROM organization_memberships WHERE organization_id=%s AND id=ANY(%s) ORDER BY id FOR UPDATE", (identity.organization_id, ordered))
        members = {int(row["id"]): row for row in cursor.fetchall()}
        if set(members) != set(ordered) or members[to_membership_id]["status"] != "active":
            raise OrganizationError("ORG_HANDOFF_MEMBER_INVALID", "交接员工不存在或不可用", http_status=422)
        cursor.execute("SELECT brand_id FROM organization_brand_assignments WHERE membership_id=%s AND status='active' AND brand_id=ANY(%s) ORDER BY brand_id FOR UPDATE", (from_membership_id, brand_set))
        assigned = {int(row["brand_id"]) for row in cursor.fetchall()}
        if assigned != set(brand_set):
            raise OrganizationError("ORG_HANDOFF_ASSIGNMENT_CHANGED", "部分客户已不属于原负责人", http_status=409, retryable=True)
        cursor.execute("UPDATE organization_brand_assignments SET status='revoked',revoked_at=NOW(),version=version+1,reason=%s WHERE membership_id=%s AND status='active' AND brand_id=ANY(%s)", (reason, from_membership_id, brand_set))
        for sequence, brand_id in enumerate(brand_set):
            cursor.execute(
                """
                INSERT INTO organization_brand_assignments(organization_id,membership_id,brand_id,status,assigned_by_user_id,reason,request_id)
                VALUES (%s,%s,%s,'active',%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                (identity.organization_id, to_membership_id, brand_id, identity.actor_user_id, reason, f"{request_id}:{sequence}"),
            )
        for membership_id in ordered:
            member = members[membership_id]
            cursor.execute("DELETE FROM user_clients WHERE user_id=%s", (member["user_id"],))
            cursor.execute("INSERT INTO user_clients(user_id,brand_id) SELECT %s,brand_id FROM organization_brand_assignments WHERE membership_id=%s AND status='active' ON CONFLICT DO NOTHING", (member["user_id"], membership_id))
            cursor.execute("UPDATE organization_memberships SET assignment_version=assignment_version+1,version=version+1,updated_at=NOW() WHERE id=%s", (membership_id,))
            cursor.execute("UPDATE users SET permission_version=permission_version+1 WHERE id=%s", (member["user_id"],))
        cursor.execute("UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s", (identity.organization_id,))
        result = {"from_membership_id": from_membership_id, "to_membership_id": to_membership_id, "brand_ids": brand_set}
        _audit(cursor, identity, action="assignment.handoff", entity_type="organization_membership", entity_id=to_membership_id, after=result, reason=reason)
        return result


def cascade_revoke_brand_with_cursor(
    cursor,
    *,
    brand_id: int,
    previous_owner_user_id: int,
    request_id: str,
    reason: str = "brand ownership changed",
) -> int:
    """Revoke every live assignment in the caller-owned brand transaction."""
    cursor.execute(
        "SELECT id,owner_user_id FROM organizations WHERE owner_user_id=%s FOR UPDATE",
        (previous_owner_user_id,),
    )
    org = cursor.fetchone()
    if not org:
        return 0
    cursor.execute(
        """
        SELECT DISTINCT membership_id FROM organization_brand_assignments
        WHERE organization_id=%s AND brand_id=%s AND status='active'
        ORDER BY membership_id
        """,
        (org["id"], brand_id),
    )
    membership_ids = [int(row["membership_id"]) for row in cursor.fetchall()]
    # Global authority lock order is organization -> membership -> assignment.
    # Claim/external rechecks use the same order, so a brand delete cannot
    # deadlock with an in-flight member action while revocation is converging.
    if membership_ids:
        cursor.execute(
            """
            SELECT id FROM organization_memberships
            WHERE id=ANY(%s)
            ORDER BY id FOR UPDATE
            """,
            (membership_ids,),
        )
        cursor.fetchall()
    cursor.execute(
        """
        SELECT id,membership_id FROM organization_brand_assignments
        WHERE organization_id=%s AND brand_id=%s AND status='active'
        ORDER BY id FOR UPDATE
        """,
        (org["id"], brand_id),
    )
    rows = list(cursor.fetchall())
    memberships = sorted({int(row["membership_id"]) for row in rows})
    if rows:
        cursor.execute(
            """
            UPDATE organization_brand_assignments
            SET status='revoked',revoked_at=NOW(),version=version+1,reason=%s
            WHERE organization_id=%s AND brand_id=%s AND status='active'
            """,
            (reason, org["id"], brand_id),
        )
        cursor.execute(
            """
            UPDATE organization_memberships
            SET assignment_version=assignment_version+1,version=version+1,updated_at=NOW()
            WHERE id=ANY(%s)
            """,
            (memberships,),
        )
        cursor.execute(
            """
            DELETE FROM user_clients uc USING organization_memberships m
            WHERE m.id=ANY(%s) AND uc.user_id=m.user_id AND uc.brand_id=%s
            """,
            (memberships, brand_id),
        )
        cursor.execute(
            """
            UPDATE users u SET permission_version=u.permission_version+1
            FROM organization_memberships m
            WHERE m.id=ANY(%s) AND u.id=m.user_id
            """,
            (memberships,),
        )
    # A brand tombstone invalidates owner/system claims too. Bump the
    # organization generation even when the brand has no employee assignment,
    # so delete→restore cannot resurrect a pre-delete provider claim.
    cursor.execute(
        "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
        (org["id"],),
    )
    identity = _system_identity(
        organization_id=int(org["id"]),
        owner_user_id=previous_owner_user_id,
        request_id=request_id,
    )
    _audit(
        cursor,
        identity,
        action="assignment.brand_owner_cascade_revoke",
        entity_type="brand",
        entity_id=brand_id,
        after={"membership_ids": memberships},
        reason=reason,
    )
    return len(rows)


def cascade_revoke_brand(*, brand_id: int, previous_owner_user_id: int, request_id: str) -> int:
    """Revoke organization authority synchronously when brand ownership changes."""
    with get_db() as conn:
        return cascade_revoke_brand_with_cursor(
            conn.cursor(),
            brand_id=brand_id,
            previous_owner_user_id=previous_owner_user_id,
            request_id=request_id,
        )


def list_audit_events(identity: IdentityContext, *, limit: int = 100, before_id: Optional[int] = None) -> list[dict[str, Any]]:
    require_governance_audit_access(identity=identity)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            """
            SELECT id,request_id,event_type,action,actor_kind,actor_user_id,membership_id,
                   entity_type,entity_id,result_snapshot,reason,created_at
            FROM organization_audit_events
            WHERE organization_id=%s AND (%s IS NULL OR id<%s)
            ORDER BY id DESC LIMIT %s
            """,
            (identity.organization_id, before_id, before_id, max(1, min(int(limit), 200))),
        )
        return [dict(row) for row in cursor.fetchall()]
