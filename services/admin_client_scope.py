"""Mode-aware admin adapter for legacy and organization client assignments.

``organization_brand_assignments`` remains the organization SSOT.  The
historical ``user_clients`` table is authoritative only for non-organization
accounts and is a compatibility projection for organization members.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from db.connection import get_db
from services.organization_contract import IdentityContext, stable_key
from services.organization_service import replace_member_assignments_with_cursor


ScopeKind = Literal[
    "legacy_unrestricted",
    "legacy_selected",
    "organization_assigned",
]


class AdminClientScopeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = int(http_status)
        self.retryable = bool(retryable)
        self.details = details or {}

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


def _normalized_brand_ids(values: Sequence[int]) -> list[int]:
    result: set[int] = set()
    for value in values:
        try:
            brand_id = int(value)
        except (TypeError, ValueError) as exc:
            raise AdminClientScopeError(
                "ADMIN_CLIENT_SCOPE_INVALID_BRAND",
                "客户范围包含无效品牌编号",
                http_status=422,
            ) from exc
        if brand_id <= 0:
            raise AdminClientScopeError(
                "ADMIN_CLIENT_SCOPE_INVALID_BRAND",
                "客户范围包含无效品牌编号",
                http_status=422,
            )
        result.add(brand_id)
    return sorted(result)


def _etag(snapshot: dict[str, Any]) -> str:
    evidence = {
        "user_id": snapshot["user_id"],
        "scope_kind": snapshot["scope_kind"],
        "subject_kind": snapshot["subject_kind"],
        "version": snapshot["version"],
        "permission_version": snapshot["permission_version"],
        "organization_id": snapshot.get("organization_id"),
        "membership_id": snapshot.get("membership_id"),
        "organization_authority_version": snapshot.get("organization_authority_version"),
        "brand_ids": snapshot["brand_ids"],
        "effective_brand_ids": snapshot["effective_brand_ids"],
        "stale_brand_ids": snapshot.get("stale_brand_ids", []),
    }
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "client-scope-v1-" + sha256(canonical.encode("utf-8")).hexdigest()


def _brand_rows(cursor, *, owner_user_ids: Sequence[int]) -> list[dict[str, Any]]:
    owners = sorted({int(value) for value in owner_user_ids if int(value) > 0})
    if not owners:
        return []
    cursor.execute(
        """
        SELECT id,name,owner_user_id
        FROM brands
        WHERE owner_user_id=ANY(%s)
          AND COALESCE(is_deleted,FALSE)=FALSE
          AND COALESCE(to_jsonb(brands)->>'status','active')
              NOT IN ('archived','cancelled','terminated','deleted')
        ORDER BY name,id
        """,
        (owners,),
    )
    return [
        {
            "id": int(row["id"]),
            "name": str(row.get("name") or f"品牌 #{row['id']}"),
            "owner_user_id": int(row["owner_user_id"]),
        }
        for row in cursor.fetchall()
    ]


def _live_membership(cursor, user_id: int, *, for_update: bool = False):
    suffix = " FOR UPDATE OF o,m" if for_update else ""
    cursor.execute(
        f"""
        SELECT m.id AS membership_id,m.organization_id,m.status,m.is_owner,
               m.assignment_version,m.version AS membership_version,
               o.owner_user_id,o.authority_version,o.status AS organization_status
        FROM organization_memberships m
        JOIN organizations o ON o.id=m.organization_id
        WHERE m.user_id=%s AND m.status IN ('active','suspended','leaving')
        ORDER BY m.id
        LIMIT 2{suffix}
        """,
        (int(user_id),),
    )
    rows = list(cursor.fetchall())
    if len(rows) > 1:
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_IDENTITY_AMBIGUOUS",
            "该账号存在多个现役组织身份，已拒绝修改",
            http_status=409,
        )
    return rows[0] if rows else None


def _organization_snapshot(cursor, user_id: int, membership) -> dict[str, Any]:
    organization_id = int(membership["organization_id"])
    membership_id = int(membership["membership_id"])
    principal_user_id = int(membership["owner_user_id"])
    available = _brand_rows(cursor, owner_user_ids=[principal_user_id])
    cursor.execute(
        """
        SELECT brand_id
        FROM organization_brand_assignments
        WHERE organization_id=%s AND membership_id=%s AND status='active'
        ORDER BY brand_id
        """,
        (organization_id, membership_id),
    )
    assigned = [int(row["brand_id"]) for row in cursor.fetchall()]
    cursor.execute("SELECT permission_version FROM users WHERE id=%s", (int(user_id),))
    user = cursor.fetchone()
    if not user:
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_USER_NOT_FOUND",
            "用户不存在",
            http_status=404,
        )
    is_owner = bool(membership["is_owner"])
    snapshot = {
        "success": True,
        "user_id": int(user_id),
        "scope_kind": "organization_assigned",
        "subject_kind": "organization_owner" if is_owner else "organization_member",
        "editable": not is_owner and membership["status"] == "active" and membership["organization_status"] == "active",
        "brand_ids": [] if is_owner else assigned,
        "effective_brand_ids": [row["id"] for row in available] if is_owner else assigned,
        "available_brands": available,
        "stale_brand_ids": [],
        "empty_semantics": "owner_identity_all" if is_owner else "zero_clients",
        "version": int(membership["assignment_version"]),
        "permission_version": int(user["permission_version"] or 0),
        "organization_id": organization_id,
        "membership_id": membership_id,
        "principal_user_id": principal_user_id,
        "organization_authority_version": int(membership["authority_version"]),
        "membership_status": str(membership["status"]),
    }
    snapshot["etag"] = _etag(snapshot)
    return snapshot


def _legacy_owner_ids(cursor, user_id: int) -> list[int]:
    owners = {int(user_id)}
    cursor.execute("SELECT to_regclass('customer_agent_bindings') AS relation")
    if cursor.fetchone()["relation"]:
        cursor.execute(
            """
            SELECT customer_user_id
            FROM customer_agent_bindings
            WHERE agent_user_id=%s AND COALESCE(dispute_status,'') <> 'pending'
            ORDER BY customer_user_id
            """,
            (int(user_id),),
        )
        owners.update(int(row["customer_user_id"]) for row in cursor.fetchall())
    return sorted(owners)


def _legacy_snapshot(cursor, user_id: int) -> dict[str, Any]:
    cursor.execute("SELECT permission_version FROM users WHERE id=%s", (int(user_id),))
    user = cursor.fetchone()
    if not user:
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_USER_NOT_FOUND",
            "用户不存在",
            http_status=404,
        )
    available = _brand_rows(cursor, owner_user_ids=_legacy_owner_ids(cursor, user_id))
    available_ids = {row["id"] for row in available}
    cursor.execute("SELECT brand_id FROM user_clients WHERE user_id=%s ORDER BY brand_id", (int(user_id),))
    raw = [int(row["brand_id"]) for row in cursor.fetchall()]
    selected = [brand_id for brand_id in raw if brand_id in available_ids]
    owned_ids = {
        row["id"] for row in available if int(row["owner_user_id"]) == int(user_id)
    }
    scope_kind: ScopeKind = "legacy_selected" if raw else "legacy_unrestricted"
    snapshot = {
        "success": True,
        "user_id": int(user_id),
        "scope_kind": scope_kind,
        "subject_kind": "legacy_user",
        "editable": True,
        "brand_ids": selected,
        "effective_brand_ids": sorted(owned_ids | set(selected)),
        "available_brands": available,
        "stale_brand_ids": sorted(set(raw) - available_ids),
        "empty_semantics": "legacy_owner_role_fallback" if scope_kind == "legacy_unrestricted" else "selected_plus_owned",
        "version": int(user["permission_version"] or 0),
        "permission_version": int(user["permission_version"] or 0),
        "organization_id": None,
        "membership_id": None,
        "principal_user_id": int(user_id),
        "organization_authority_version": None,
        "membership_status": None,
    }
    snapshot["etag"] = _etag(snapshot)
    return snapshot


def _snapshot(cursor, user_id: int):
    membership = _live_membership(cursor, user_id)
    if membership:
        return _organization_snapshot(cursor, user_id, membership)
    return _legacy_snapshot(cursor, user_id)


def get_admin_client_scope(user_id: int) -> dict[str, Any]:
    with get_db() as conn:
        return _snapshot(conn.cursor(), int(user_id))


def _assert_cas(
    current: dict[str, Any],
    *,
    expected_scope_kind: str,
    expected_version: int,
    expected_etag: str,
) -> None:
    if str(current["scope_kind"]) != str(expected_scope_kind):
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_MODE_CHANGED",
            "账号客户范围模式已变化，请刷新后重试",
            http_status=409,
            retryable=True,
            details={"current_scope_kind": current["scope_kind"]},
        )
    if int(current["version"]) != int(expected_version) or str(current["etag"]) != str(expected_etag):
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_VERSION_CONFLICT",
            "客户范围已被他人更新，请刷新后重试",
            http_status=409,
            retryable=True,
            details={"current_version": current["version"], "current_etag": current["etag"]},
        )


def _organization_assignment_replay(
    cursor,
    *,
    organization_id: int,
    membership_id: int,
    request_id: str,
    brand_ids: Sequence[int],
    current_brand_ids: Sequence[int],
) -> bool:
    audit_key = stable_key(
        organization_id,
        request_id,
        "assignment.admin_replace",
        "organization_membership",
        membership_id,
        0,
    )
    cursor.execute(
        "SELECT after_snapshot FROM organization_audit_events WHERE audit_event_key=%s",
        (audit_key,),
    )
    row = cursor.fetchone()
    if not row:
        return False
    snapshot = row.get("after_snapshot") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    previous = sorted(int(value) for value in snapshot.get("brand_ids", []))
    if previous != sorted(int(value) for value in brand_ids):
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_IDEMPOTENCY_CONFLICT",
            "同一请求编号不能修改不同的客户范围",
            http_status=409,
        )
    if previous != sorted(int(value) for value in current_brand_ids):
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_REPLAY_SUPERSEDED",
            "该请求曾成功，但客户范围随后已被其他请求更新",
            http_status=409,
            details={"original_brand_ids": previous},
        )
    return True


def replace_admin_client_scope(
    *,
    admin_user_id: int,
    target_user_id: int,
    scope_kind: ScopeKind,
    expected_scope_kind: ScopeKind,
    expected_version: int,
    expected_etag: str,
    brand_ids: Sequence[int],
    request_id: str,
    reason: str,
) -> dict[str, Any]:
    normalized = _normalized_brand_ids(brand_ids)
    if scope_kind == "legacy_unrestricted" and normalized:
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_SHAPE_INVALID",
            "legacy_unrestricted 模式不能携带品牌列表",
            http_status=422,
        )
    if scope_kind == "legacy_selected" and not normalized:
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_SHAPE_INVALID",
            "legacy_selected 模式至少选择一个客户",
            http_status=422,
        )
    if not str(reason or "").strip():
        raise AdminClientScopeError(
            "ADMIN_CLIENT_SCOPE_REASON_REQUIRED",
            "请填写调整原因",
            http_status=422,
        )
    with get_db() as conn:
        cursor = conn.cursor()
        membership_probe = _live_membership(cursor, int(target_user_id))
        if membership_probe:
            # Global organization lock order: organization -> membership -> user.
            cursor.execute(
                "SELECT id FROM organizations WHERE id=%s FOR UPDATE",
                (int(membership_probe["organization_id"]),),
            )
            cursor.execute(
                "SELECT id FROM organization_memberships WHERE id=%s FOR UPDATE",
                (int(membership_probe["membership_id"]),),
            )
            cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (int(target_user_id),))
            membership = _live_membership(cursor, int(target_user_id))
            if not membership or int(membership["membership_id"]) != int(membership_probe["membership_id"]):
                raise AdminClientScopeError(
                    "ADMIN_CLIENT_SCOPE_MODE_CHANGED",
                    "账号组织身份已变化，请刷新后重试",
                    http_status=409,
                    retryable=True,
                )
            current = _organization_snapshot(cursor, int(target_user_id), membership)
            if scope_kind != "organization_assigned":
                raise AdminClientScopeError(
                    "ADMIN_CLIENT_SCOPE_MODE_MISMATCH",
                    "组织账号只能使用 organization_assigned 模式",
                    http_status=409,
                )
            if _organization_assignment_replay(
                cursor,
                organization_id=int(membership["organization_id"]),
                membership_id=int(membership["membership_id"]),
                request_id=request_id,
                brand_ids=normalized,
                current_brand_ids=current["brand_ids"],
            ):
                return {**current, "replayed": True}
            _assert_cas(
                current,
                expected_scope_kind=expected_scope_kind,
                expected_version=expected_version,
                expected_etag=expected_etag,
            )
            if current["subject_kind"] == "organization_owner":
                raise AdminClientScopeError(
                    "ADMIN_CLIENT_SCOPE_OWNER_IMMUTABLE",
                    "组织老板的全客户视图来自 owner 身份，不能改为员工分配范围",
                    http_status=409,
                )
            if not current["editable"]:
                raise AdminClientScopeError(
                    "ADMIN_CLIENT_SCOPE_MEMBERSHIP_INACTIVE",
                    "该员工席位当前不可修改客户范围",
                    http_status=409,
                )
            audit_identity = IdentityContext(
                request_id=request_id,
                authenticated_user_id=int(admin_user_id),
                principal_user_id=int(membership["owner_user_id"]),
                payer_user_id=int(membership["owner_user_id"]),
                actor_kind="system",
                organization_id=int(membership["organization_id"]),
                actor_user_id=int(admin_user_id),
                requested_by_user_id=int(admin_user_id),
            )
            replace_member_assignments_with_cursor(
                cursor,
                organization_id=int(membership["organization_id"]),
                principal_user_id=int(membership["owner_user_id"]),
                membership_id=int(membership["membership_id"]),
                brand_ids=normalized,
                actor_user_id=int(admin_user_id),
                request_id=request_id,
                reason=str(reason).strip(),
                expected_assignment_version=int(expected_version),
                audit_identity=audit_identity,
                not_owned_http_status=404,
                audit_action="assignment.admin_replace",
            )
            updated_membership = _live_membership(cursor, int(target_user_id))
            return _organization_snapshot(cursor, int(target_user_id), updated_membership)

        # Legacy mutation locks the user before re-checking that no live
        # membership appeared. Invite acceptance also locks this row, so the
        # mode cannot change inside this transaction.
        cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (int(target_user_id),))
        if not cursor.fetchone():
            raise AdminClientScopeError(
                "ADMIN_CLIENT_SCOPE_USER_NOT_FOUND",
                "用户不存在",
                http_status=404,
            )
        if _live_membership(cursor, int(target_user_id)):
            raise AdminClientScopeError(
                "ADMIN_CLIENT_SCOPE_MODE_CHANGED",
                "账号已加入组织，请刷新后按组织分配模式操作",
                http_status=409,
                retryable=True,
            )
        current = _legacy_snapshot(cursor, int(target_user_id))
        _assert_cas(
            current,
            expected_scope_kind=expected_scope_kind,
            expected_version=expected_version,
            expected_etag=expected_etag,
        )
        if scope_kind == "organization_assigned":
            raise AdminClientScopeError(
                "ADMIN_CLIENT_SCOPE_MODE_MISMATCH",
                "非组织账号不能写 organization_assigned 模式",
                http_status=409,
            )
        available_ids = {int(row["id"]) for row in current["available_brands"]}
        missing = sorted(set(normalized) - available_ids)
        if missing:
            raise AdminClientScopeError(
                "ADMIN_CLIENT_SCOPE_BRAND_NOT_FOUND",
                "客户不存在或不属于该账号的合法服务范围",
                http_status=404,
            )
        cursor.execute("DELETE FROM user_clients WHERE user_id=%s", (int(target_user_id),))
        if normalized:
            cursor.executemany(
                "INSERT INTO user_clients(user_id,brand_id) VALUES (%s,%s)",
                [(int(target_user_id), brand_id) for brand_id in normalized],
            )
        cursor.execute(
            "UPDATE users SET permission_version=permission_version+1 WHERE id=%s",
            (int(target_user_id),),
        )
        return _legacy_snapshot(cursor, int(target_user_id))
