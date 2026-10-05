"""Versioned organization approval policy and execution gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from db.connection import get_db
from db.organization_db import resolve_identity
from services.organization_contract import (
    APPROVAL_ACTION_CAPABILITIES,
    APPROVAL_ACTIONS,
    BILLABLE_FEATURE_CAPABILITIES,
    EXTERNAL_ACTIONS,
    IdentityContext,
    OrganizationError,
    capability_snapshot_hash,
    payload_hash,
    require_feature_flag,
)
from services.organization_service import _audit, _load_product_config, _lock_identity


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_policy(
    cursor,
    identity: IdentityContext,
    *,
    action_type: str,
    feature_code: Optional[str],
    public_scope: Optional[str] = None,
    estimated_points: int = 0,
) -> Mapping[str, Any]:
    cursor.execute(
        """
        SELECT * FROM organization_approval_policies
        WHERE organization_id=%s AND status='active' AND effective_to IS NULL
          AND action_type=%s
          AND (membership_id=%s OR membership_id IS NULL)
          AND (role_id=%s OR role_id IS NULL)
          AND (feature_code=%s OR feature_code IS NULL)
          AND (public_scope=%s OR public_scope IS NULL)
          AND (min_points IS NULL OR min_points<=%s)
          AND (max_points IS NULL OR max_points>=%s)
        ORDER BY
          (membership_id IS NOT NULL) DESC,
          (role_id IS NOT NULL) DESC,
          (feature_code IS NOT NULL) DESC,
          (public_scope IS NOT NULL) DESC,
          always_require_approval DESC,
          threshold_points ASC NULLS LAST,
          max_points ASC NULLS LAST,
          id DESC
        FOR UPDATE
        """,
        (
            identity.organization_id,
            action_type,
            identity.membership_id,
            identity.role_id,
            feature_code,
            public_scope,
            int(estimated_points),
            int(estimated_points),
        ),
    )
    rows = list(cursor.fetchall())
    if not rows:
        raise OrganizationError("ORG_APPROVAL_POLICY_MISSING", "审批规则未配置，操作已停止，请联系团队负责人", http_status=503)
    # The ORDER BY encodes member > role > organization and exact feature /
    # public scope > wildcard.  Same-level overlap deliberately picks the
    # stricter rule, then the newest id as a stable final tiebreaker.
    return rows[0]


def approval_required(
    cursor,
    identity: IdentityContext,
    *,
    action_type: str,
    estimated_points: int = 0,
    feature_code: Optional[str] = None,
    public_scope: Optional[str] = None,
) -> tuple[bool, Mapping[str, Any]]:
    if identity.is_owner:
        return False, {}
    if action_type not in APPROVAL_ACTIONS:
        return False, {}
    policy = _lock_policy(
        cursor,
        identity,
        action_type=action_type,
        feature_code=feature_code,
        public_scope=public_scope,
        estimated_points=estimated_points,
    )
    threshold = policy.get("threshold_points")
    required = bool(policy["always_require_approval"])
    if threshold is not None and int(estimated_points) >= int(threshold):
        required = True
    return required, policy


def approval_required_with_default(
    cursor,
    identity: IdentityContext,
    *,
    action_type: str,
    default_required: bool,
    estimated_points: int = 0,
    feature_code: Optional[str] = None,
    public_scope: Optional[str] = None,
) -> tuple[bool, Mapping[str, Any]]:
    """和 `approval_required` 相同,但**组织尚未配置该动作的策略时**用调用方给的默认值。

    背景:`_lock_policy` 在查不到策略行时抛 ORG_APPROVAL_POLICY_MISSING(503)。
    那个 fail-closed 对"删除/外发/大额扣费"是对的 —— 没配规矩就别动。但对
    **发布**这类日常交付动作,老板 2026-07-25 拍板的口径是"默认员工直接发,
    要审批就打开开关";若沿用 503,存量组织的员工会直接被 503 挡死,等于交付岗
    上不了班。

    所以这里把"没配策略"这一种情况(且**仅**这一种)交给调用方决定,
    其余行为与 `approval_required` 完全一致 —— 组织一旦配了策略,就以策略为准。
    """
    try:
        return approval_required(
            cursor,
            identity,
            action_type=action_type,
            estimated_points=estimated_points,
            feature_code=feature_code,
            public_scope=public_scope,
        )
    except OrganizationError as exc:
        if getattr(exc, "code", None) != "ORG_APPROVAL_POLICY_MISSING":
            raise
        return bool(default_required), {"source": "default", "always_require_approval": bool(default_required)}


def submit_approval(
    identity: IdentityContext,
    *,
    action_type: str,
    payload: Mapping[str, Any],
    request_id: str,
    brand_id: Optional[int] = None,
    artifact_type: Optional[str] = None,
    artifact_id: Optional[str] = None,
    estimated_points: int = 0,
    feature_code: Optional[str] = None,
    public_scope: Optional[str] = None,
) -> dict[str, Any]:
    if identity.is_owner:
        raise OrganizationError("ORG_OWNER_APPROVAL_NOT_REQUIRED", "老板操作无需审批，可直接执行", http_status=422)
    if action_type not in APPROVAL_ACTIONS:
        raise OrganizationError("ORG_APPROVAL_ACTION_INVALID", "该操作不属于审批范围", http_status=422)
    submit_capability = APPROVAL_ACTION_CAPABILITIES.get(action_type)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-approval:{identity.organization_id}:{request_id}",),
        )
        identity = _lock_identity(cursor, identity)
        if submit_capability:
            identity.require(submit_capability)
        if action_type == "billing.execute_high_cost":
            feature_capability = BILLABLE_FEATURE_CAPABILITIES.get(feature_code)
            if feature_capability is None:
                raise OrganizationError("ORG_FEATURE_UNCLASSIFIED", "该功能暂不支持团队协作使用，请联系平台", http_status=503)
            identity.require(feature_capability)
        if action_type in EXTERNAL_ACTIONS:
            if "whitelabel_snapshot" in payload or "whitelabel_snapshot_hash" in payload:
                raise OrganizationError(
                    "ORG_WHITELABEL_CLIENT_CONTROL_FORBIDDEN",
                    "参数无效，请从页面按钮重新发起",
                    http_status=422,
                )
            purpose_by_action = {
                "quote.send_external": "quote",
                "diagnosis_report.share_external": "diagnosis_report",
                "monitoring_report.share_external": "monitoring_report",
                "portal.issue_external_token": "portal",
            }
            expected_purpose = purpose_by_action[action_type]
            if public_scope != expected_purpose or not artifact_type or not artifact_id:
                raise OrganizationError(
                    "ORG_PUBLIC_APPROVAL_SCOPE_INVALID",
                    "分享内容信息不完整，请从对应内容页重新发起分享",
                    http_status=422,
                )
            try:
                expires_in_seconds = int(payload.get("expires_in_seconds"))
            except (TypeError, ValueError):
                raise OrganizationError(
                    "ORG_PUBLIC_TOKEN_EXPIRY_INVALID", "公开链接有效期无效", http_status=422
                ) from None
            from services.organization_artifacts import build_public_token_payload
            artifact, payload = build_public_token_payload(
                cursor,
                identity,
                artifact_type=artifact_type,
                artifact_id=str(artifact_id),
                purpose=expected_purpose,
                expires_in_seconds=expires_in_seconds,
            )
            resolved_brand_id = artifact.get("brand_id")
            if brand_id is not None and int(brand_id) != int(resolved_brand_id or 0):
                raise OrganizationError(
                    "ORG_PUBLIC_APPROVAL_SCOPE_INVALID",
                    "要分享的内容与所选客户不一致，请重新选择",
                    http_status=422,
                )
            brand_id = int(resolved_brand_id) if resolved_brand_id is not None else None
        if brand_id is not None:
            cursor.execute(
                """
                SELECT 1 FROM organization_brand_assignments
                WHERE organization_id=%s AND membership_id=%s AND brand_id=%s AND status='active'
                """,
                (identity.organization_id, identity.membership_id, brand_id),
            )
            if not cursor.fetchone():
                raise OrganizationError("ORG_BRAND_NOT_ASSIGNED", "该客户未分配给当前员工", http_status=403)
        cursor.execute(
            "SELECT * FROM organization_approval_requests WHERE organization_id=%s AND request_id=%s FOR UPDATE",
            (identity.organization_id, request_id),
        )
        replay = cursor.fetchone()
        digest = payload_hash(payload)
        if replay:
            if replay["payload_hash"] != digest or replay["action_type"] != action_type:
                raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "操作冲突，请刷新后重新提交审批", http_status=409)
            return _safe_request(replay)
        required, policy = approval_required(
            cursor,
            identity,
            action_type=action_type,
            estimated_points=estimated_points,
            feature_code=feature_code,
            public_scope=public_scope,
        )
        if not required:
            raise OrganizationError("ORG_APPROVAL_NOT_REQUIRED", "当前策略不要求审批", http_status=422)
        config = _load_product_config(cursor)
        snapshot_hash = capability_snapshot_hash(identity.capabilities)
        cursor.execute(
            """
            INSERT INTO organization_approval_requests(
              organization_id,requested_by_membership_id,requested_by_user_id,action_type,feature_code,
              brand_id,artifact_type,artifact_id,payload_hash,payload_snapshot,estimated_points,
              status,policy_id,policy_version,policy_hash,capability_snapshot_hash,expires_at,request_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (
                identity.organization_id,
                identity.membership_id,
                identity.actor_user_id,
                action_type,
                feature_code,
                brand_id,
                artifact_type,
                artifact_id,
                digest,
                __import__("json").dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                int(estimated_points),
                policy["id"],
                policy["version"],
                policy["policy_hash"],
                snapshot_hash,
                _now() + timedelta(hours=config["approval_ttl_hours"]),
                request_id,
            ),
        )
        created = cursor.fetchone()
        _audit(cursor, identity, action="approval.submit", entity_type="organization_approval_request", entity_id=created["id"], after=_safe_request(created))
        return _safe_request(created)


def _safe_request(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "organization_id": int(row["organization_id"]),
        "requested_by_membership_id": int(row["requested_by_membership_id"]),
        "requested_by_user_id": int(row["requested_by_user_id"]),
        "action_type": row["action_type"],
        "feature_code": row.get("feature_code"),
        "brand_id": row.get("brand_id"),
        "artifact_type": row.get("artifact_type"),
        "artifact_id": row.get("artifact_id"),
        "payload_hash": row["payload_hash"],
        "estimated_points": int(row["estimated_points"]),
        "status": row["status"],
        "policy_version": int(row["policy_version"]),
        "approved_by_user_id": row.get("approved_by_user_id"),
        "expires_at": row["expires_at"],
        "version": int(row["version"]),
        "created_at": row["created_at"],
    }


def list_approvals(
    identity: IdentityContext,
    *,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        can_review = identity.is_owner or "approvals.review" in identity.capabilities
        cursor.execute(
            """
            UPDATE organization_approval_requests SET status='expired',version=version+1,updated_at=NOW()
            WHERE organization_id=%s AND status='pending' AND expires_at<=NOW()
            """,
            (identity.organization_id,),
        )
        if identity.is_owner:
            requester_filter = ""
            params: tuple[Any, ...] = (
                identity.organization_id,
                status,
                status,
                max(1, min(int(limit), 200)),
            )
        elif can_review:
            # A delegated reviewer may inspect assigned-customer requests and
            # their own requests only.  Decision-time authorization remains
            # stricter and action-specific.
            requester_filter = """
              AND (
                requested_by_membership_id=%s OR brand_id IN (
                  SELECT oba.brand_id
                  FROM organization_brand_assignments oba
                  JOIN brands b ON b.id=oba.brand_id
                  WHERE oba.membership_id=%s AND oba.status='active'
                    AND b.owner_user_id=%s AND COALESCE(b.is_deleted,FALSE)=FALSE
                )
              )
            """
            params = (
                identity.organization_id,
                status,
                status,
                identity.membership_id,
                identity.membership_id,
                identity.principal_user_id,
                max(1, min(int(limit), 200)),
            )
        else:
            requester_filter = " AND requested_by_membership_id=%s"
            params = (
                identity.organization_id,
                status,
                status,
                identity.membership_id,
                max(1, min(int(limit), 200)),
            )
        cursor.execute(
            """
            SELECT * FROM organization_approval_requests
            WHERE organization_id=%s AND (%s IS NULL OR status=%s)
            """ + requester_filter + """
            ORDER BY created_at DESC,id DESC LIMIT %s
            """,
            params,
        )
        return [_safe_request(row) for row in cursor.fetchall()]


def decide_approval(
    identity: IdentityContext,
    *,
    approval_id: int,
    decision: str,
    expected_version: int,
    reason: str,
) -> dict[str, Any]:
    if decision not in {"approve", "reject"}:
        raise OrganizationError("ORG_APPROVAL_DECISION_INVALID", "审批决定无效", http_status=422)
    reason = str(reason or "").strip()
    if decision == "reject" and not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "拒绝时必须填写原因", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        if not identity.is_owner:
            identity.require("approvals.review")
        cursor.execute(
            "SELECT * FROM organization_approval_requests WHERE id=%s AND organization_id=%s FOR UPDATE",
            (approval_id, identity.organization_id),
        )
        request = cursor.fetchone()
        if not request:
            raise OrganizationError("ORG_APPROVAL_NOT_FOUND", "审批单不存在", http_status=404)
        if int(request["requested_by_user_id"]) == int(identity.actor_user_id):
            raise OrganizationError("ORG_APPROVAL_SELF_FORBIDDEN", "员工不能审批自己的申请", http_status=403)
        if not identity.is_owner:
            scoped_capability = APPROVAL_ACTION_CAPABILITIES.get(request["action_type"])
            if request["action_type"] == "billing.execute_high_cost":
                scoped_capability = BILLABLE_FEATURE_CAPABILITIES.get(request.get("feature_code"))
                if scoped_capability is None:
                    raise OrganizationError("ORG_FEATURE_UNCLASSIFIED", "该功能暂不支持团队协作使用，请联系平台", http_status=503)
            if scoped_capability:
                identity.require(scoped_capability)
            if request.get("brand_id") is not None:
                cursor.execute(
                    """
                    SELECT 1 FROM organization_brand_assignments
                    WHERE organization_id=%s AND membership_id=%s AND brand_id=%s AND status='active'
                    """,
                    (identity.organization_id, identity.membership_id, request["brand_id"]),
                )
                if not cursor.fetchone():
                    raise OrganizationError("ORG_APPROVAL_SCOPE_DENIED", "你没有该客户的审批权限", http_status=403)
        if int(request["version"]) != int(expected_version):
            raise OrganizationError("ORG_VERSION_CONFLICT", "审批单状态已变化，请刷新", http_status=409, retryable=True)
        if request["status"] != "pending" or request["expires_at"] <= _now():
            if request["status"] == "pending":
                cursor.execute("UPDATE organization_approval_requests SET status='expired',version=version+1,updated_at=NOW() WHERE id=%s", (approval_id,))
            raise OrganizationError("ORG_APPROVAL_NOT_PENDING", "审批单已不是待审批状态", http_status=409)
        target_status = "approved" if decision == "approve" else "rejected"
        time_column = "approved_at" if decision == "approve" else "rejected_at"
        cursor.execute(
            f"""
            UPDATE organization_approval_requests
            SET status=%s,approved_by_user_id=%s,{time_column}=NOW(),reason=%s,
                version=version+1,updated_at=NOW()
            WHERE id=%s RETURNING *
            """,
            (target_status, identity.actor_user_id if decision == "approve" else None, reason or None, approval_id),
        )
        updated = cursor.fetchone()
        _audit(cursor, identity, action=f"approval.{decision}", entity_type="organization_approval_request", entity_id=approval_id, before=_safe_request(request), after=_safe_request(updated), reason=reason or None)
        return _safe_request(updated)


def revoke_approval(identity: IdentityContext, *, approval_id: int, reason: str) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写撤销原因", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        cursor.execute("SELECT * FROM organization_approval_requests WHERE id=%s AND organization_id=%s FOR UPDATE", (approval_id, identity.organization_id))
        request = cursor.fetchone()
        if not request:
            raise OrganizationError("ORG_APPROVAL_NOT_FOUND", "审批单不存在", http_status=404)
        if not identity.is_owner and int(request["requested_by_user_id"]) != int(identity.actor_user_id):
            raise OrganizationError("ORG_APPROVAL_REVOKE_FORBIDDEN", "无权撤销该审批单", http_status=403)
        if request["status"] not in {"pending", "approved"}:
            raise OrganizationError("ORG_APPROVAL_NOT_REVOCABLE", "当前审批状态不能撤销", http_status=409)
        cursor.execute("UPDATE organization_approval_requests SET status='revoked',revoked_at=NOW(),reason=%s,version=version+1,updated_at=NOW() WHERE id=%s RETURNING *", (reason, approval_id))
        updated = cursor.fetchone()
        _audit(cursor, identity, action="approval.revoke", entity_type="organization_approval_request", entity_id=approval_id, before=_safe_request(request), after=_safe_request(updated), reason=reason)
        return _safe_request(updated)


def lock_approval_for_execution(
    cursor,
    identity: IdentityContext,
    *,
    approval_id: Optional[int],
    action_type: str,
    payload: Mapping[str, Any],
    estimated_points: int,
    feature_code: Optional[str],
    execution_id: str,
    public_scope: Optional[str] = None,
) -> Optional[Mapping[str, Any]]:
    required, policy = approval_required(
        cursor,
        identity,
        action_type=action_type,
        estimated_points=estimated_points,
        feature_code=feature_code,
        public_scope=public_scope,
    )
    if not required:
        return None
    if approval_id is None:
        raise OrganizationError("ORG_APPROVAL_REQUIRED", "该操作需要审批后执行", http_status=409)
    cursor.execute("SELECT * FROM organization_approval_requests WHERE id=%s AND organization_id=%s FOR UPDATE", (approval_id, identity.organization_id))
    request = cursor.fetchone()
    if not request or request["status"] != "approved":
        raise OrganizationError("ORG_APPROVAL_NOT_APPROVED", "审批单尚未批准或已失效", http_status=409)
    if request["expires_at"] <= _now():
        raise OrganizationError("ORG_APPROVAL_EXPIRED", "审批单已过期", http_status=409)
    if int(request["requested_by_user_id"]) != int(identity.actor_user_id):
        raise OrganizationError("ORG_APPROVAL_ACTOR_MISMATCH", "只能由提交申请的员工执行", http_status=403)
    if identity.is_member and int(request["requested_by_membership_id"]) != int(identity.membership_id or 0):
        raise OrganizationError(
            "ORG_APPROVAL_MEMBERSHIP_CHANGED",
            "你的权限在申请后发生了变化，请重新提交审批",
            http_status=409,
        )
    if int(request["approved_by_user_id"]) == int(identity.actor_user_id):
        raise OrganizationError("ORG_APPROVAL_SELF_FORBIDDEN", "不能执行自己审批通过的申请", http_status=403)
    if (
        request["action_type"] != action_type
        or request.get("feature_code") != feature_code
        or request["payload_hash"] != payload_hash(payload)
    ):
        raise OrganizationError("ORG_APPROVAL_PAYLOAD_CHANGED", "执行内容与已批准内容不一致", http_status=409)
    if int(request.get("estimated_points") or 0) != int(estimated_points):
        raise OrganizationError(
            "ORG_APPROVAL_ESTIMATE_CHANGED",
            "本次消耗的算力上限与审批时不一致，请重新申请",
            http_status=409,
        )
    if int(request["policy_id"]) != int(policy["id"]) or int(request["policy_version"]) != int(policy["version"]) or request["policy_hash"] != policy["policy_hash"]:
        raise OrganizationError("ORG_APPROVAL_POLICY_CHANGED", "审批策略已变化，请重新申请", http_status=409)
    if request["capability_snapshot_hash"] != capability_snapshot_hash(identity.capabilities):
        raise OrganizationError("ORG_APPROVAL_CAPABILITY_CHANGED", "员工权限已变化，请重新申请", http_status=409)
    if request.get("execution_request_id") and request["execution_request_id"] != execution_id:
        raise OrganizationError("ORG_APPROVAL_ALREADY_USED", "该审批已被用于另一次执行，请重新申请", http_status=409)
    if action_type in EXTERNAL_ACTIONS:
        require_feature_flag("ORGANIZATION_EXTERNAL_ACTIONS_ENABLED")
    cursor.execute("UPDATE organization_approval_requests SET status='executing',execution_request_id=%s,version=version+1,updated_at=NOW() WHERE id=%s", (execution_id, approval_id))
    return request


def list_policies(identity: IdentityContext) -> list[dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            """
            SELECT * FROM organization_approval_policies
            WHERE organization_id=%s
            ORDER BY status='active' DESC,action_type,membership_id NULLS LAST,
                     role_id NULLS LAST,feature_code NULLS LAST,version DESC,id DESC
            """,
            (identity.organization_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def configure_policy(
    identity: IdentityContext,
    *,
    request_id: str,
    action_type: str,
    always_require_approval: bool,
    membership_id: Optional[int] = None,
    role_id: Optional[int] = None,
    feature_code: Optional[str] = None,
    public_scope: Optional[str] = None,
    min_points: Optional[int] = None,
    max_points: Optional[int] = None,
    threshold_points: Optional[int] = None,
    expected_version: Optional[int] = None,
    reason: str,
) -> dict[str, Any]:
    """CAS-supersede one exact policy shape and append its replacement."""
    if action_type not in APPROVAL_ACTIONS:
        raise OrganizationError("ORG_APPROVAL_ACTION_INVALID", "该操作不属于审批范围", http_status=422)
    if membership_id is not None and role_id is not None:
        raise OrganizationError("ORG_APPROVAL_POLICY_SUBJECT_INVALID", "策略只能指定员工或角色之一", http_status=422)
    for value in (min_points, max_points, threshold_points):
        if value is not None and int(value) < 0:
            raise OrganizationError("ORG_APPROVAL_POLICY_AMOUNT_INVALID", "审批算力范围无效", http_status=422)
    if min_points is not None and max_points is not None and int(max_points) < int(min_points):
        raise OrganizationError("ORG_APPROVAL_POLICY_AMOUNT_INVALID", "审批算力范围无效", http_status=422)
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写策略修改原因", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-policy:{identity.organization_id}:{request_id}",),
        )
        identity = _lock_identity(cursor, identity, owner_required=True)
        if membership_id is not None:
            cursor.execute(
                "SELECT 1 FROM organization_memberships WHERE id=%s AND organization_id=%s AND NOT is_owner",
                (membership_id, identity.organization_id),
            )
            if not cursor.fetchone():
                raise OrganizationError("ORG_MEMBER_INVALID", "所选员工不存在", http_status=422)
        if role_id is not None:
            cursor.execute(
                "SELECT 1 FROM organization_roles WHERE id=%s AND organization_id=%s AND NOT is_owner_role",
                (role_id, identity.organization_id),
            )
            if not cursor.fetchone():
                raise OrganizationError("ORG_ROLE_INVALID", "所选角色不存在", http_status=422)
        cursor.execute(
            """
            SELECT * FROM organization_approval_policies
            WHERE organization_id=%s AND status='active' AND effective_to IS NULL
              AND action_type=%s
              AND COALESCE(membership_id,0)=COALESCE(%s,0)
              AND COALESCE(role_id,0)=COALESCE(%s,0)
              AND COALESCE(feature_code,'')=COALESCE(%s,'')
              AND COALESCE(public_scope,'')=COALESCE(%s,'')
              AND COALESCE(min_points,-1)=COALESCE(%s,-1)
              AND COALESCE(max_points,-1)=COALESCE(%s,-1)
            FOR UPDATE
            """,
            (
                identity.organization_id,action_type,membership_id,role_id,feature_code,
                public_scope,min_points,max_points,
            ),
        )
        current = cursor.fetchone()
        if current and expected_version is not None and int(current["version"]) != int(expected_version):
            raise OrganizationError("ORG_VERSION_CONFLICT", "审批策略已变化，请刷新", http_status=409, retryable=True)
        if not current and expected_version is not None:
            raise OrganizationError("ORG_VERSION_CONFLICT", "审批策略不存在或已变化", http_status=409, retryable=True)
        next_version = int(current["version"]) + 1 if current else 1
        snapshot = {
            "action_type": action_type,
            "membership_id": membership_id,
            "role_id": role_id,
            "feature_code": feature_code,
            "public_scope": public_scope,
            "min_points": min_points,
            "max_points": max_points,
            "threshold_points": threshold_points,
            "always_require_approval": bool(always_require_approval),
            "version": next_version,
        }
        if current:
            cursor.execute(
                "UPDATE organization_approval_policies SET status='superseded',effective_to=NOW() WHERE id=%s",
                (current["id"],),
            )
        cursor.execute(
            """
            INSERT INTO organization_approval_policies(
              organization_id,version,status,membership_id,role_id,action_type,feature_code,
              public_scope,min_points,max_points,threshold_points,always_require_approval,
              effective_from,policy_hash,created_by_user_id,approved_by_user_id
            ) VALUES (%s,%s,'active',%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s)
            RETURNING *
            """,
            (
                identity.organization_id,next_version,membership_id,role_id,action_type,feature_code,
                public_scope,min_points,max_points,threshold_points,bool(always_require_approval),
                payload_hash(snapshot),identity.actor_user_id,identity.actor_user_id,
            ),
        )
        created = dict(cursor.fetchone())
        _audit(
            cursor,identity,action="approval_policy.configure",entity_type="organization_approval_policy",
            entity_id=created["id"],before=dict(current) if current else None,after=snapshot,reason=reason,
        )
        return created


def finish_approval_execution(cursor, *, approval_id: Optional[int], succeeded: bool, reason: Optional[str] = None) -> None:
    if approval_id is None:
        return
    cursor.execute(
        """
        UPDATE organization_approval_requests
        SET status=%s,reason=COALESCE(%s,reason),version=version+1,updated_at=NOW()
        WHERE id=%s AND status='executing'
        """,
        ("executed" if succeeded else "failed", reason, approval_id),
    )
    if cursor.rowcount != 1:
        raise OrganizationError(
            "ORG_APPROVAL_EXECUTION_STATE_LOST",
            "审批执行异常，请联系客服（ORG_APPROVAL_EXECUTION_STATE_LOST）",
            http_status=503,
        )
