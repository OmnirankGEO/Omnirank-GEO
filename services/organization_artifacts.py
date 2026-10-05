"""Artifact ownership, query isolation, internal sharing, and public tokens."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import json
from typing import Any, Mapping, Optional

from db.connection import get_db
from services.organization_approvals import finish_approval_execution, lock_approval_for_execution
from services.organization_contract import (
    IdentityContext,
    OrganizationError,
    canonical_json,
    payload_hash,
    require_feature_flag,
    secure_compare,
)
from services.organization_crypto import derive_bearer_token, hash_bearer_token
from services.organization_service import _audit, _lock_identity


ARTIFACT_TABLES = {
    "client_profile": ("client_profiles", "id"),
    "client_material": ("client_materials", "id"),
    "diagnosis": ("diagnosis_records", "id"),
    "quote": ("quotes", "id"),
    "keyword_selection": ("keyword_selection_sessions", "id"),
    "article_generation": ("article_generations", "id"),
    "article": ("articles", "id"),
    "media_publication": ("media_publications", "id"),
    "monitoring_task": ("monitoring_tasks", "id"),
    "monitoring_report": ("monitoring_reports", "id"),
}

PUBLIC_PURPOSE_ARTIFACTS = {
    "quote": frozenset({"quote"}),
    "diagnosis_report": frozenset({"diagnosis"}),
    "monitoring_report": frozenset({"monitoring_report"}),
    "portal": frozenset({"diagnosis", "quote", "monitoring_report"}),
}


def build_public_token_payload(
    cursor,
    identity: IdentityContext,
    *,
    artifact_type: str,
    artifact_id: str,
    purpose: str,
    expires_in_seconds: int,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """Build the immutable approval/execution payload from server SSOT only."""
    if purpose not in PUBLIC_PURPOSE_ARTIFACTS:
        raise OrganizationError("ORG_PUBLIC_TOKEN_PURPOSE_INVALID", "分享类型无效，请从对应内容页发起", http_status=422)
    if artifact_type not in PUBLIC_PURPOSE_ARTIFACTS[purpose]:
        raise OrganizationError("ORG_PUBLIC_TOKEN_ARTIFACT_MISMATCH", "分享类型与内容不匹配，请从对应内容页重新发起", http_status=422)
    if int(expires_in_seconds) < 60 or int(expires_in_seconds) > 30 * 86400:
        raise OrganizationError("ORG_PUBLIC_TOKEN_EXPIRY_INVALID", "公开链接有效期无效", http_status=422)
    artifact = _lock_accessible_artifact(
        cursor,
        identity,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
    )
    whitelabel_snapshot = _resolve_principal_whitelabel_snapshot(
        cursor, identity.principal_user_id
    )
    whitelabel_hash = payload_hash(whitelabel_snapshot)
    return artifact, {
        "artifact_type": artifact_type,
        "artifact_id": str(artifact_id),
        "brand_id": artifact.get("brand_id"),
        "purpose": purpose,
        "expires_in_seconds": int(expires_in_seconds),
        "whitelabel_snapshot": whitelabel_snapshot,
        "whitelabel_snapshot_hash": whitelabel_hash,
    }


def _safe_share(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data.pop("token_hash", None)
    data.pop("request_payload_hash", None)
    return data


def _resolve_principal_whitelabel_snapshot(cursor, principal_user_id: int) -> dict[str, Any]:
    """Resolve the public brand from the payer/owner SSOT in this transaction."""
    from services.public_whitelabel import public_branding_from_record

    cursor.execute(
        """
        SELECT company_name,logo_url,slogan,contact_name,contact_phone,contact_wechat,
               contact_email,brand_color,product_name,favicon_url,whitelabel_mode,
               whitelabel_status,unlocked_by_admin
        FROM whitelabel_settings WHERE user_id=%s
        """,
        (int(principal_user_id),),
    )
    row = cursor.fetchone()
    # A missing row is a valid platform-brand snapshot. A missing table or
    # malformed schema is not swallowed: public issuance must fail closed.
    return public_branding_from_record(dict(row) if row else None, surface="customer")


def cache_partition_key(identity: IdentityContext, namespace: str, resource_key: Any) -> str:
    """Namespace cache/RAG/vector/full-text/cursor data by live authority."""
    member = identity.membership_id or 0
    return f"org:{identity.organization_id}:member:{member}:auth:{identity.authority_version}:{namespace}:{resource_key}"


def isolation_cursor(identity: IdentityContext, *, last_id: Any, secret: str) -> str:
    raw = canonical_json(
        {
            "organization_id": identity.organization_id,
            "membership_id": identity.membership_id,
            "authority_version": identity.authority_version,
            "last_id": last_id,
        }
    )
    digest = payload_hash({"secret": secret, "payload": raw})
    return base64.urlsafe_b64encode(f"{raw}.{digest}".encode("utf-8")).decode("ascii").rstrip("=")


def parse_isolation_cursor(identity: IdentityContext, *, token: str, secret: str) -> Any:
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
        raw, digest = decoded.rsplit(".", 1)
        data = json.loads(raw)
    except Exception:
        raise OrganizationError("ORG_CURSOR_INVALID", "列表已过期，请刷新", http_status=422) from None
    if not secure_compare(digest, payload_hash({"secret": secret, "payload": raw})):
        raise OrganizationError("ORG_CURSOR_INVALID", "列表已过期，请刷新", http_status=422)
    if (
        int(data.get("organization_id") or 0) != identity.organization_id
        or int(data.get("membership_id") or 0) != int(identity.membership_id or 0)
        or str(data.get("authority_version")) != identity.authority_version
    ):
        raise OrganizationError("ORG_CURSOR_AUTHORITY_CHANGED", "权限已变化，请重新加载列表", http_status=409)
    return data.get("last_id")


def artifact_access_predicate(
    identity: IdentityContext,
    *,
    alias: str = "a",
    team_capability: Optional[str] = None,
    artifact_type: Optional[str] = None,
) -> tuple[str, tuple[Any, ...]]:
    """Return a composable fail-closed SQL predicate for every read surface."""
    live_brand = f"""EXISTS (
      SELECT 1 FROM brands live_brand
      WHERE live_brand.id={alias}.brand_id
        AND live_brand.owner_user_id=%s
        AND COALESCE(live_brand.is_deleted,FALSE)=FALSE
    )"""
    if identity.is_owner:
        return (
            f"{alias}.organization_id=%s AND {live_brand}",
            (identity.organization_id, identity.principal_user_id),
        )
    team_allowed = bool(team_capability and team_capability in identity.capabilities)
    if team_allowed:
        return (
            f"""{alias}.organization_id=%s AND {live_brand}
            AND {alias}.brand_id IN (
              SELECT oba.brand_id FROM organization_brand_assignments oba
              JOIN brands b ON b.id=oba.brand_id
              WHERE oba.membership_id=%s AND oba.status='active'
                AND b.owner_user_id=%s AND COALESCE(b.is_deleted,FALSE)=FALSE
            )""",
            (
                identity.organization_id,
                identity.principal_user_id,
                identity.membership_id,
                identity.principal_user_id,
            ),
        )
    owned_or_shared = f"{alias}.created_by_membership_id=%s"
    shared_params: tuple[Any, ...] = (identity.membership_id,)
    if artifact_type:
        owned_or_shared += f""" OR EXISTS (
          SELECT 1 FROM organization_artifact_shares s
          WHERE s.organization_id=%s AND s.artifact_type=%s
            AND s.artifact_id={alias}.id::text AND s.status='active'
            AND (s.subject_membership_id=%s OR s.subject_role_id=%s)
            AND s.permission IN ('read','review','edit','handoff')
        )"""
        shared_params += (
            identity.organization_id,
            artifact_type,
            identity.membership_id,
            identity.role_id,
        )
    return (
        f"""{alias}.organization_id=%s AND {live_brand} AND ({owned_or_shared})
        AND {alias}.brand_id IN (
          SELECT oba.brand_id FROM organization_brand_assignments oba
          JOIN brands b ON b.id=oba.brand_id
          WHERE oba.membership_id=%s AND oba.status='active'
            AND b.owner_user_id=%s AND COALESCE(b.is_deleted,FALSE)=FALSE
        )""",
        (
            identity.organization_id,
            identity.principal_user_id,
            *shared_params,
            identity.membership_id,
            identity.principal_user_id,
        ),
    )


def _lock_accessible_artifact(
    cursor,
    identity: IdentityContext,
    *,
    artifact_type: str,
    artifact_id: str,
    for_update: bool = True,
) -> Mapping[str, Any]:
    """Lock the exact organization artifact before sharing or publishing it."""
    table_info = ARTIFACT_TABLES.get(artifact_type)
    if not table_info:
        raise OrganizationError("ORG_ARTIFACT_TYPE_UNKNOWN", "该类内容暂不支持团队共享，请联系平台", http_status=503)
    table, id_column = table_info
    predicate, params = artifact_access_predicate(
        identity,
        alias="a",
        team_capability="team.output_read",
        artifact_type=artifact_type,
    )
    lock_clause = "FOR UPDATE OF a" if for_update else ""
    cursor.execute(
        f"""
        SELECT a.{id_column} AS id,a.brand_id,a.created_by_user_id,
               a.created_by_membership_id,a.responsible_user_id,a.artifact_visibility
        FROM {table} a
        WHERE a.{id_column}::text=%s AND {predicate}
        {lock_clause}
        """,
        (str(artifact_id), *params),
    )
    artifact = cursor.fetchone()
    if not artifact:
        # Deliberately use one response for nonexistent and out-of-scope ids.
        raise OrganizationError("ORG_ARTIFACT_NOT_FOUND", "内容不存在或无权查看", http_status=404)
    return artifact


def require_artifact_access(
    identity: IdentityContext,
    *,
    artifact_type: str,
    artifact_id: Any,
) -> dict[str, Any]:
    """Re-resolve authority and prove object access for a live legacy handler."""
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        identity.require_active_organization()
        return dict(
            _lock_accessible_artifact(
                cursor,
                identity,
                artifact_type=artifact_type,
                artifact_id=str(artifact_id),
                for_update=False,
            )
        )


def lock_artifact_access_in_transaction(
    cursor,
    identity: IdentityContext,
    *,
    artifact_type: str,
    artifact_id: Any,
) -> dict[str, Any]:
    """Revalidate authority and lock an artifact inside its business mutation."""
    live_identity = _lock_identity(cursor, identity)
    live_identity.require_active_organization()
    return dict(
        _lock_accessible_artifact(
            cursor,
            live_identity,
            artifact_type=artifact_type,
            artifact_id=str(artifact_id),
            for_update=True,
        )
    )


def filter_accessible_artifact_rows(
    identity: IdentityContext,
    *,
    artifact_type: str,
    rows: list[Mapping[str, Any]],
    id_key: str = "id",
) -> list[dict[str, Any]]:
    """Filter a legacy list in one DB query; an empty/unparseable id never leaks."""
    table_info = ARTIFACT_TABLES.get(artifact_type)
    if not table_info:
        raise OrganizationError("ORG_ARTIFACT_TYPE_UNKNOWN", "该类内容暂不支持团队共享，请联系平台", http_status=503)
    candidate_ids = [str(row.get(id_key)) for row in rows if row.get(id_key) is not None]
    if not candidate_ids:
        return []
    table, id_column = table_info
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        predicate, params = artifact_access_predicate(
            identity,
            alias="a",
            team_capability="team.output_read",
            artifact_type=artifact_type,
        )
        cursor.execute(
            f"SELECT a.{id_column}::text AS id FROM {table} a "
            f"WHERE a.{id_column}::text=ANY(%s) AND {predicate}",
            (candidate_ids, *params),
        )
        allowed = {str(row["id"]) for row in cursor.fetchall()}
    return [dict(row) for row in rows if str(row.get(id_key)) in allowed]


def stamp_artifact(
    cursor,
    identity: IdentityContext,
    *,
    artifact_type: str,
    artifact_id: Any,
    brand_id: Optional[int],
    visibility: str = "private",
) -> None:
    """Attach immutable actor/org ownership in the legacy write transaction."""
    table_info = ARTIFACT_TABLES.get(artifact_type)
    if not table_info:
        raise OrganizationError("ORG_ARTIFACT_TYPE_UNKNOWN", "该类内容暂不支持团队共享，请联系平台", http_status=503)
    if visibility not in {"private", "team", "external"}:
        raise OrganizationError("ORG_ARTIFACT_VISIBILITY_INVALID", "内容可见范围无效", http_status=422)
    if identity.is_member:
        cursor.execute(
            """
            SELECT 1 FROM organization_brand_assignments
            WHERE organization_id=%s AND membership_id=%s AND brand_id=%s AND status='active'
            """,
            (identity.organization_id, identity.membership_id, brand_id),
        )
        if not cursor.fetchone():
            raise OrganizationError("ORG_BRAND_NOT_ASSIGNED", "该客户未分配给当前员工", http_status=403)
    # A system occurrence deliberately has no human actor_user_id or
    # membership_id.  The legacy artifact columns still require a user FK for
    # principal/responsibility attribution, so retain the owner principal in
    # those columns while created_by_actor_kind remains the authoritative actor
    # discriminator.  This must not be used for charge attribution: the charge
    # link keeps actor_user_id=NULL for system work.
    attributed_user_id = (
        identity.principal_user_id if identity.is_system else identity.actor_user_id
    )
    table, id_column = table_info
    cursor.execute(
        f"""
        UPDATE {table}
        SET brand_id=COALESCE(brand_id,%s),
            organization_id=%s,
            created_by_user_id=COALESCE(created_by_user_id,%s),
            created_by_membership_id=COALESCE(created_by_membership_id,%s),
            created_by_actor_kind=COALESCE(created_by_actor_kind,%s),
            responsible_user_id=COALESCE(responsible_user_id,%s),
            artifact_visibility=%s
        WHERE {id_column}=%s
          AND (organization_id IS NULL OR organization_id=%s)
          AND (brand_id IS NULL OR brand_id=%s)
        """,
        (
            brand_id,
            identity.organization_id,
            attributed_user_id,
            identity.membership_id,
            identity.actor_kind,
            attributed_user_id,
            visibility,
            artifact_id,
            identity.organization_id,
            brand_id,
        ),
    )
    if cursor.rowcount != 1:
        raise OrganizationError("ORG_ARTIFACT_STAMP_CONFLICT", "保存失败，请重试；若持续请联系客服", http_status=409)


def list_artifacts(
    identity: IdentityContext,
    *,
    artifact_type: str,
    brand_id: Optional[int] = None,
    search: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    table_info = ARTIFACT_TABLES.get(artifact_type)
    if not table_info:
        raise OrganizationError("ORG_ARTIFACT_TYPE_UNKNOWN", "该类内容暂不支持团队共享，请联系平台", http_status=503)
    table, id_column = table_info
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        predicate, params = artifact_access_predicate(
            identity,
            alias="a",
            team_capability="team.output_read",
            artifact_type=artifact_type,
        )
        cursor.execute(
            f"""
            SELECT a.{id_column} AS id,a.brand_id,a.created_by_user_id,a.created_by_membership_id,
                   a.responsible_user_id,a.artifact_visibility
            FROM {table} a
            WHERE {predicate} AND (%s IS NULL OR a.brand_id=%s)
              AND (%s IS NULL OR a.{id_column}::text ILIKE '%%' || %s || '%%')
            ORDER BY a.{id_column} DESC LIMIT %s
            """,
            (*params, brand_id, brand_id, search, search, max(1, min(int(limit), 200))),
        )
        return [dict(row) for row in cursor.fetchall()]


def share_artifact_internal(
    identity: IdentityContext,
    *,
    artifact_type: str,
    artifact_id: str,
    permission: str,
    subject_membership_id: Optional[int],
    subject_role_id: Optional[int],
    request_id: str,
) -> dict[str, Any]:
    if permission not in {"read", "review", "edit", "handoff"}:
        raise OrganizationError("ORG_SHARE_PERMISSION_INVALID", "共享权限无效", http_status=422)
    if bool(subject_membership_id) == bool(subject_role_id):
        raise OrganizationError("ORG_SHARE_SUBJECT_INVALID", "请选择一名员工或一个角色", http_status=422)
    request_digest = payload_hash(
        {
            "artifact_type": artifact_type,
            "artifact_id": str(artifact_id),
            "permission": permission,
            "subject_membership_id": subject_membership_id,
            "subject_role_id": subject_role_id,
        }
    )
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        if not identity.is_owner:
            identity.require("writing.share_internal")
        _lock_accessible_artifact(
            cursor,
            identity,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
        )
        cursor.execute(
            "SELECT * FROM organization_artifact_shares WHERE organization_id=%s AND request_id=%s FOR UPDATE",
            (identity.organization_id, request_id),
        )
        replay = cursor.fetchone()
        if replay:
            if (
                replay.get("request_payload_hash") != request_digest
                or int(replay.get("issued_by_user_id") or 0) != int(identity.actor_user_id or 0)
            ):
                raise OrganizationError(
                    "ORG_IDEMPOTENCY_CONFLICT",
                    "操作冲突，请刷新后重试",
                    http_status=409,
                )
            return _safe_share(replay)
        if subject_membership_id:
            cursor.execute("SELECT 1 FROM organization_memberships WHERE id=%s AND organization_id=%s AND status='active'", (subject_membership_id, identity.organization_id))
        else:
            cursor.execute("SELECT 1 FROM organization_roles WHERE id=%s AND organization_id=%s", (subject_role_id, identity.organization_id))
        if not cursor.fetchone():
            raise OrganizationError("ORG_SHARE_SUBJECT_NOT_FOUND", "共享对象不存在", http_status=404)
        cursor.execute(
            """
            INSERT INTO organization_artifact_shares(
              organization_id,artifact_type,artifact_id,subject_membership_id,subject_role_id,
              permission,status,principal_user_id,issued_by_user_id,request_payload_hash,
              authority_version,request_id
            ) VALUES (%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s) RETURNING *
            """,
            (
                identity.organization_id,artifact_type,artifact_id,subject_membership_id,
                subject_role_id,permission,identity.principal_user_id,identity.actor_user_id,
                request_digest,identity.organization_authority_version,request_id,
            ),
        )
        created = cursor.fetchone()
        _audit(cursor, identity, action="artifact.share_internal", entity_type=artifact_type, entity_id=artifact_id, after={"share_id": created["id"], "permission": permission, "subject_membership_id": subject_membership_id, "subject_role_id": subject_role_id})
        return _safe_share(created)


def issue_public_token(
    identity: IdentityContext,
    *,
    artifact_type: str,
    artifact_id: str,
    purpose: str,
    request_id: str,
    approval_request_id: Optional[int],
    expires_in_seconds: int,
) -> dict[str, Any]:
    require_feature_flag("ORGANIZATION_EXTERNAL_ACTIONS_ENABLED")
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        identity.require_active_organization()
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"organization-public-token:{identity.organization_id}:{request_id}",),
        )
        artifact, payload = build_public_token_payload(
            cursor,
            identity,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            purpose=purpose,
            expires_in_seconds=expires_in_seconds,
        )
        if not identity.is_owner:
            identity.require({
                "quote": "quote.submit_for_approval",
                "diagnosis_report": "reports.share_external",
                "monitoring_report": "reports.share_external",
                "portal": "reports.share_external",
            }[purpose])
        whitelabel_snapshot = payload["whitelabel_snapshot"]
        whitelabel_hash = payload["whitelabel_snapshot_hash"]
        request_digest = payload_hash(payload)
        action = {
            "quote": "quote.send_external",
            "diagnosis_report": "diagnosis_report.share_external",
            "monitoring_report": "monitoring_report.share_external",
            "portal": "portal.issue_external_token",
        }[purpose]
        # Replay is checked before changing an approved request to executing.
        # The deterministic HMAC token is reconstructable after a committed
        # response is lost, while its plaintext never enters PostgreSQL.
        cursor.execute(
            "SELECT * FROM organization_artifact_shares "
            "WHERE organization_id=%s AND request_id=%s FOR UPDATE",
            (identity.organization_id, request_id),
        )
        replay = cursor.fetchone()
        stable_material = f"{identity.organization_id}:{identity.actor_user_id}:{request_id}"
        if replay:
            if (
                replay.get("request_payload_hash") != request_digest
                or replay.get("artifact_type") != artifact_type
                or str(replay.get("artifact_id")) != str(artifact_id)
                or replay.get("token_purpose") != purpose
                or replay.get("approval_request_id") != approval_request_id
                or int(replay.get("issued_by_user_id") or 0) != int(identity.actor_user_id or 0)
            ):
                raise OrganizationError(
                    "ORG_IDEMPOTENCY_CONFLICT",
                    "操作冲突，请刷新后重新生成链接",
                    http_status=409,
                )
            if replay.get("status") != "active" or replay.get("token_expires_at") <= datetime.now(timezone.utc):
                raise OrganizationError(
                    "ORG_PUBLIC_TOKEN_REVOKED",
                    "该公开链接已失效，请重新生成一条新链接",
                    http_status=410,
                )
            token, digest, _ = derive_bearer_token(
                purpose=f"organization-public:{purpose}",
                stable_material=stable_material,
                key_version=replay.get("token_key_version"),
            )
            if not secure_compare(digest, replay.get("token_hash") or ""):
                raise OrganizationError(
                    "ORG_PUBLIC_TOKEN_EVIDENCE_MISMATCH",
                    "链接异常，请重新生成；若持续请联系客服",
                    http_status=503,
                )
            return {"share": _safe_share(replay), "token": token, "replayed": True}

        token, digest, key_version = derive_bearer_token(
            purpose=f"organization-public:{purpose}",
            stable_material=stable_material,
        )
        approval = lock_approval_for_execution(
            cursor,
            identity,
            approval_id=approval_request_id,
            action_type=action,
            payload=payload,
            estimated_points=0,
            feature_code=None,
            execution_id=request_id,
            public_scope=purpose,
        ) if not identity.is_owner else None
        cursor.execute(
            """
            INSERT INTO organization_artifact_shares(
              organization_id,artifact_type,artifact_id,permission,status,token_hash,token_key_version,
              token_purpose,token_expires_at,principal_user_id,issued_by_user_id,whitelabel_snapshot,
              whitelabel_snapshot_hash,request_payload_hash,approval_request_id,
              authority_version,request_id
            ) VALUES (%s,%s,%s,'read','active',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """,
            (
                identity.organization_id,artifact_type,artifact_id,digest,key_version,purpose,
                datetime.now(timezone.utc)+timedelta(seconds=expires_in_seconds),
                identity.principal_user_id,identity.actor_user_id,canonical_json(whitelabel_snapshot),
                whitelabel_hash,request_digest,int(approval["id"]) if approval else None,
                identity.organization_authority_version,request_id,
            ),
        )
        share = cursor.fetchone()
        finish_approval_execution(
            cursor,
            approval_id=int(approval["id"]) if approval else None,
            succeeded=True,
        )
        _audit(cursor, identity, action="artifact.issue_public_token", entity_type=artifact_type, entity_id=artifact_id, after={"share_id": share["id"], "purpose": purpose, "expires_at": share["token_expires_at"]})
        return {"share": _safe_share(share), "token": token, "replayed": False}


def revoke_share(identity: IdentityContext, *, share_id: int, reason: str) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写撤销原因", http_status=422)
    with get_db() as conn:
        cursor = conn.cursor()
        identity = _lock_identity(cursor, identity)
        cursor.execute("SELECT * FROM organization_artifact_shares WHERE id=%s AND organization_id=%s FOR UPDATE", (share_id, identity.organization_id))
        share = cursor.fetchone()
        if not share:
            raise OrganizationError("ORG_SHARE_NOT_FOUND", "共享记录不存在", http_status=404)
        if not identity.is_owner and int(share["issued_by_user_id"] or 0) != int(identity.actor_user_id or 0):
            raise OrganizationError("ORG_SHARE_REVOKE_FORBIDDEN", "无权撤销该共享", http_status=403)
        if share["status"] != "revoked":
            cursor.execute("UPDATE organization_artifact_shares SET status='revoked',revoked_at=NOW(),version=version+1,updated_at=NOW() WHERE id=%s RETURNING *", (share_id,))
            share = cursor.fetchone()
            cursor.execute("UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s", (identity.organization_id,))
        _audit(cursor, identity, action="artifact.revoke_share", entity_type="organization_artifact_share", entity_id=share_id, after={"status": "revoked"}, reason=reason)
        return _safe_share(share)


def validate_public_token(*, purpose: str, token: str) -> dict[str, Any]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT token_key_version FROM organization_artifact_shares
            WHERE token_purpose=%s AND token_hash IS NOT NULL
            """,
            (purpose,),
        )
        versions = [row["token_key_version"] for row in cursor.fetchall() if row["token_key_version"]]
        digests = []
        for version in versions:
            try:
                digests.append(
                    hash_bearer_token(
                        purpose=f"organization-public:{purpose}", token=token, key_version=version
                    )[0]
                )
            except OrganizationError:
                # Retiring one historic key must not make all still-valid key
                # versions unusable during rotation.
                continue
        if not digests:
            raise OrganizationError("ORG_PUBLIC_TOKEN_INVALID", "公开链接不存在或已失效", http_status=404)
        cursor.execute(
            """
            SELECT s.*,o.status AS organization_status,o.authority_version AS live_authority_version
            FROM organization_artifact_shares s JOIN organizations o ON o.id=s.organization_id
            WHERE s.token_hash=ANY(%s) AND s.token_purpose=%s FOR UPDATE OF s,o
            """,
            (digests, purpose),
        )
        share = cursor.fetchone()
        now = datetime.now(timezone.utc)
        if not share or share["status"] != "active" or share["organization_status"] != "active" or share["token_expires_at"] <= now:
            raise OrganizationError("ORG_PUBLIC_TOKEN_REVOKED", "公开链接已撤销或过期", http_status=410)
        table_info = ARTIFACT_TABLES.get(str(share["artifact_type"]))
        if not table_info:
            raise OrganizationError("ORG_PUBLIC_TOKEN_REVOKED", "公开链接已撤销或过期", http_status=410)
        table, id_column = table_info
        cursor.execute(
            f"""
            SELECT 1
            FROM {table} a
            JOIN brands b ON b.id=a.brand_id
            WHERE a.{id_column}::text=%s AND a.organization_id=%s
              AND b.owner_user_id=%s AND COALESCE(b.is_deleted,FALSE)=FALSE
            """,
            (share["artifact_id"], share["organization_id"], share["principal_user_id"]),
        )
        if not cursor.fetchone():
            raise OrganizationError("ORG_PUBLIC_TOKEN_REVOKED", "公开链接已撤销或过期", http_status=410)
        # Tokens intentionally survive unrelated authority bumps but every
        # request still checks their own status. Revocation is therefore
        # immediate and never relies on CDN TTL.
        return {
            "organization_id": int(share["organization_id"]),
            "artifact_type": share["artifact_type"],
            "artifact_id": share["artifact_id"],
            "purpose": share["token_purpose"],
            "principal_user_id": int(share["principal_user_id"]),
            "whitelabel_snapshot": share.get("whitelabel_snapshot") or {},
            "expires_at": share["token_expires_at"],
            "cache_control": "private, no-store, max-age=0",
        }
