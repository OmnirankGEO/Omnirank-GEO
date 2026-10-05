"""Versioned organization product governance and verified invite onboarding.

This module never reuses ordinary registration: invite-created operators get
only a login identity, verified contact, legal evidence, membership and audit
anchors.  Wallets, brands, roles, referrals, commissions and client ownership
are deliberately absent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
import os
import re
import uuid
from typing import Any, Mapping, Optional

logger = logging.getLogger("GEO-Organization-Onboarding")

from db.connection import get_db
from db.organization_db import assert_ready
from services.legal_agreements import (
    PRIVACY_CONTENT_HASH,
    PRIVACY_VERSION,
    USER_TERMS_CONTENT_HASH,
    USER_TERMS_VERSION,
)
from services.organization_contract import (
    IdentityContext,
    OrganizationError,
    canonical_json,
    capability_snapshot_hash,
    payload_hash,
    require_feature_flag,
    secure_compare,
)
from services.organization_crypto import (
    decrypt_delivery_target,
    derive_bearer_token,
    encrypt_delivery_target,
    hash_bearer_token,
    normalize_target,
    target_matches,
)
from services.organization_membership_lifecycle import snapshot_and_clear_legacy_access


PRODUCT_CODE = "organization_internal_seats"
CATALOG_TYPE = "feature_consumption"
CATALOG_SCOPE = "ORGANIZATION_SEATS"
REQUIRED_RATE_ACTIONS = (
    "invite.create",
    "invite.resend",
    "invite.accept",
    "invite.verify",
    "invite.delivery_status",
)


#: [F-3 · Owner 裁决]操作员开户密码最短长度(唯一权威定义;API/前端都对齐它)
MIN_OPERATOR_PASSWORD_LENGTH = 6


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _require_platform_admin(cursor, actor_user_id: int) -> None:
    cursor.execute(
        """SELECT u.id,u.is_active,EXISTS(
             SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
             WHERE ur.user_id=u.id AND r.name='admin'
           ) AS is_admin
           FROM users u WHERE u.id=%s FOR UPDATE""",
        (int(actor_user_id),),
    )
    row = cursor.fetchone()
    if not row or not bool(row["is_active"]) or not bool(row["is_admin"]):
        raise OrganizationError("PLATFORM_ADMIN_REQUIRED", "仅平台管理员可执行此操作", http_status=403)


def _publication_result(cursor, row: Mapping[str, Any]) -> dict[str, Any]:
    cursor.execute(
        """SELECT v.version_code,e.source_ref_jsonb
           FROM pricing_catalog_versions v
           JOIN pricing_catalog_entries e ON e.id=%s AND e.version_id=v.id
           WHERE v.id=%s""",
        (row["product_catalog_entry_id"], row["product_catalog_version_id"]),
    )
    version = cursor.fetchone()
    metadata = _json_object(version["source_ref_jsonb"])
    return {
        "publication_version": int(row["publication_version"]),
        "product_catalog_version_id": int(row["product_catalog_version_id"]),
        "product_catalog_entry_id": int(row["product_catalog_entry_id"]),
        "version_code": str(version["version_code"]),
        "included_seats": int(row["included_seats"]),
        "extra_seat_price_cents": int(row["extra_seat_price_cents"]),
        "paid_extra_seats_enabled": bool(row["paid_extra_seats_enabled"]),
        "operational": bool(row["operational"]),
        "invite_ttl_hours": int(metadata["invite_ttl_hours"]),
        "verification_ttl_minutes": int(metadata["verification_ttl_minutes"]),
        "verification_max_attempts": int(metadata["verification_max_attempts"]),
        "config_hash": str(row["config_hash"]),
        "created_at": row["created_at"],
    }


def get_product_config(*, actor_user_id: int) -> dict[str, Any]:
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        _require_platform_admin(cursor, actor_user_id)
        cursor.execute(
            """SELECT * FROM organization_product_config_publications
               ORDER BY publication_version DESC LIMIT 1"""
        )
        row = cursor.fetchone()
        if not row:
            raise OrganizationError("ORG_PRODUCT_CONFIG_MISSING", "席位服务配置异常，请联系平台客服", http_status=503)
        return _publication_result(cursor, row)


def publish_product_config(
    *,
    actor_user_id: int,
    request_id: str,
    expected_version: int,
    included_seats: int,
    invite_ttl_hours: int,
    verification_ttl_minutes: int,
    verification_max_attempts: int,
    reason: str,
) -> dict[str, Any]:
    """Publish a free included-seat policy with CAS and durable audit receipt."""
    reason = str(reason or "").strip()
    if not reason:
        raise OrganizationError("ORG_REASON_REQUIRED", "请填写发布原因", http_status=422)
    if not 1 <= int(included_seats) <= 10000:
        raise OrganizationError("ORG_PRODUCT_CONFIG_INVALID", "基础员工席位数无效", http_status=422)
    if not 1 <= int(invite_ttl_hours) <= 24 * 30:
        raise OrganizationError("ORG_PRODUCT_CONFIG_INVALID", "邀请有效期无效", http_status=422)
    if not 5 <= int(verification_ttl_minutes) <= 60:
        raise OrganizationError("ORG_PRODUCT_CONFIG_INVALID", "验证码有效期无效", http_status=422)
    if not 3 <= int(verification_max_attempts) <= 10:
        raise OrganizationError("ORG_PRODUCT_CONFIG_INVALID", "验证码尝试次数无效", http_status=422)

    request_payload = {
        "included_seats": int(included_seats),
        "extra_seat_price_cents": 0,
        "paid_extra_seats_enabled": False,
        "invite_ttl_hours": int(invite_ttl_hours),
        "verification_ttl_minutes": int(verification_ttl_minutes),
        "verification_max_attempts": int(verification_max_attempts),
        "operational": True,
        "reason": reason,
    }
    request_digest = payload_hash(request_payload)
    rate_limits = {
        "invite.create": {"window_seconds": 3600, "organization": 100, "actor": 30, "target": 5, "ip": 100},
        "invite.resend": {"window_seconds": 3600, "organization": 100, "actor": 30, "target": 5, "ip": 100},
        "invite.accept": {"window_seconds": 3600, "organization": 100, "actor": 30, "target": 20, "ip": 100},
        "invite.verify": {"window_seconds": 3600, "organization": 100, "target": 10, "ip": 100},
        # 送达轮询独立桶：接受页每几秒轮询一次送达状态是产品化行为，
        # 若与"输错验证码"共用 invite.verify 小桶（10/小时），正常轮询
        # + 一次手滑就会把该邀请锁死 1 小时。轮询只读、不泄露额外信息，
        # 放量到 60/小时且与 verify 桶互不影响。
        "invite.delivery_status": {"window_seconds": 3600, "organization": 100, "target": 60, "ip": 100},
    }
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("organization-product-config",))
        _require_platform_admin(cursor, actor_user_id)
        cursor.execute(
            """SELECT * FROM organization_product_config_publications
               ORDER BY publication_version DESC LIMIT 1 FOR UPDATE"""
        )
        current = cursor.fetchone()
        if not current:
            raise OrganizationError("ORG_PRODUCT_CONFIG_MISSING", "席位服务配置版本缺失，请联系平台客服", http_status=503)
        cursor.execute(
            "SELECT * FROM organization_product_config_publications WHERE request_id=%s",
            (str(request_id),),
        )
        replay = cursor.fetchone()
        if replay:
            if replay["request_payload_hash"] != request_digest:
                raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "操作冲突，请刷新页面重试", http_status=409)
            return {**_publication_result(cursor, replay), "replayed": True}
        if int(current["publication_version"]) != int(expected_version):
            raise OrganizationError(
                "ORG_VERSION_CONFLICT",
                "商品配置已变化，请刷新后重试",
                http_status=409,
                retryable=True,
                safe_details={"current_version": int(current["publication_version"])},
            )
        cursor.execute(
            """SELECT COALESCE(MAX(occupied),0) AS max_occupied FROM (
                 SELECT o.id,
                   (SELECT COUNT(*) FROM organization_memberships m
                    WHERE m.organization_id=o.id AND NOT m.is_owner
                      AND m.status IN ('active','suspended','leaving')) +
                   (SELECT COUNT(*) FROM organization_invites i
                    WHERE i.organization_id=o.id AND i.status='pending' AND i.expires_at>NOW()) AS occupied
                 FROM organizations o WHERE o.status='active'
               ) q"""
        )
        max_occupied = int(cursor.fetchone()["max_occupied"] or 0)
        if max_occupied > int(included_seats):
            raise OrganizationError(
                "ORG_PRODUCT_CONFIG_BELOW_OCCUPIED",
                "基础席位数低于现有占用，已拒绝发布",
                http_status=409,
                safe_details={"minimum_included_seats": max_occupied},
            )
        next_version = int(current["publication_version"]) + 1
        version_code = f"organization-seats-v{next_version}"
        metadata = {
            "included_seats": int(included_seats),
            "extra_seat_price_cents": 0,
            "high_cost_approval_threshold_points": 0,
            "invite_ttl_hours": int(invite_ttl_hours),
            "approval_ttl_hours": 24,
            "verification_ttl_minutes": int(verification_ttl_minutes),
            "verification_max_attempts": int(verification_max_attempts),
            "operational": True,
            "paid_extra_seats_enabled": False,
            "policy_version": version_code,
            "rate_limits": rate_limits,
        }
        config_digest = payload_hash(metadata)
        cursor.execute(
            """INSERT INTO pricing_catalog_versions(
                 catalog_type,scope_key,version_code,status,reason,created_by,calc_meta_jsonb
               ) VALUES (%s,%s,%s,'draft',%s,%s,%s::jsonb) RETURNING id""",
            (CATALOG_TYPE, CATALOG_SCOPE, version_code, reason, int(actor_user_id), canonical_json({"organization_product_config_hash": config_digest})),
        )
        version_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """INSERT INTO pricing_catalog_entries(
                 version_id,product_code,base_price_cents,multiplier_bps,final_price_cents,
                 paid_points,bonus_points,cost_floor_cents,usage_example_version,source_ref_jsonb
               ) VALUES (%s,%s,0,10000,0,0,0,0,%s,%s::jsonb) RETURNING id""",
            (version_id, PRODUCT_CODE, version_code, canonical_json(metadata)),
        )
        entry_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """UPDATE pricing_catalog_versions
               SET status='archived',effective_to=NOW(),archived_at=NOW(),updated_at=NOW()
               WHERE catalog_type=%s AND scope_key=%s AND status='published' AND effective_to IS NULL""",
            (CATALOG_TYPE, CATALOG_SCOPE),
        )
        cursor.execute(
            """UPDATE pricing_catalog_versions
               SET status='published',effective_from=NOW(),published_at=NOW(),approved_by=%s,
                   approved_at=NOW(),updated_at=NOW()
               WHERE id=%s AND status='draft'""",
            (int(actor_user_id), version_id),
        )
        if cursor.rowcount != 1:
            raise OrganizationError("ORG_PRODUCT_CONFIG_PUBLISH_FAILED", "商品配置发布失败", http_status=409)
        cursor.execute(
            """UPDATE organization_seat_entitlements
               SET status='superseded',effective_to=NOW()
               WHERE status='active' AND effective_to IS NULL"""
        )
        cursor.execute(
            """INSERT INTO organization_seat_entitlements(
                 organization_id,product_catalog_version_id,product_catalog_entry_id,
                 source_sku,entitled_seats,extra_seat_price_snapshot,effective_from,status,snapshot_hash
               ) SELECT o.id,%s,%s,%s,%s,%s::jsonb,NOW(),'active',%s
                 FROM organizations o WHERE o.status<>'dissolved'""",
            (
                version_id,
                entry_id,
                PRODUCT_CODE,
                int(included_seats),
                canonical_json({"price_cents": 0, "paid_extra_seats_enabled": False, "catalog_version": version_code}),
                config_digest,
            ),
        )
        cursor.execute(
            """INSERT INTO organization_product_config_publications(
                 request_id,publication_version,previous_publication_id,
                 product_catalog_version_id,product_catalog_entry_id,included_seats,
                 extra_seat_price_cents,paid_extra_seats_enabled,operational,
                 config_hash,request_payload_hash,actor_user_id,reason
               ) VALUES (%s,%s,%s,%s,%s,%s,0,FALSE,TRUE,%s,%s,%s,%s)
               RETURNING *""",
            (
                str(request_id), next_version, int(current["id"]), version_id, entry_id,
                int(included_seats), config_digest, request_digest, int(actor_user_id), reason,
            ),
        )
        published = cursor.fetchone()
        return {**_publication_result(cursor, published), "replayed": False}


def _split_ciphertext(value: str) -> tuple[str, str]:
    try:
        version, ciphertext = str(value).split(".", 1)
    except ValueError:
        raise OrganizationError("ORG_INVITE_CIPHERTEXT_INVALID", "邀请数据无法解密", http_status=410) from None
    return version, ciphertext


def _invite_target(invite: Mapping[str, Any]) -> str:
    version, ciphertext = _split_ciphertext(str(invite["delivery_ciphertext_or_reference"]))
    target = decrypt_delivery_target(str(invite["target_kind"]), ciphertext, version)
    normalized = normalize_target(str(invite["target_kind"]), target)
    if not target_matches(
        str(invite["target_kind"]), normalized, str(invite["target_hmac"]),
        str(invite["target_hmac_key_version"]),
    ):
        raise OrganizationError("ORG_INVITE_TARGET_MISMATCH", "邀请链接与接收人不匹配，请确认打开的是发给你的链接", http_status=410)
    return normalized


def _lookup_target_user(cursor, *, target_kind: str, normalized: str, lock: bool) -> Optional[dict[str, Any]]:
    suffix = " FOR UPDATE" if lock else ""
    if target_kind == "username":
        # [WP6] 用户名式邀请:按登录用户名查既有账号(username 已归一小写)。
        cursor.execute(
            "SELECT id,is_active,phone,email FROM users WHERE username=%s" + suffix,
            (normalized,),
        )
    elif target_kind == "email":
        cursor.execute(
            "SELECT id,is_active,phone,email FROM users WHERE lower(btrim(email))=%s" + suffix,
            (normalized.casefold(),),
        )
    else:
        national = normalized[3:] if normalized.startswith("+86") else normalized
        cursor.execute(
            """SELECT id,is_active,phone,email FROM users
               WHERE regexp_replace(COALESCE(phone,''),'[[:space:]()\\-]','','g')=ANY(%s)""" + suffix,
            ([normalized, national, "00" + normalized[1:] if normalized.startswith("+") else normalized],),
        )
    rows = list(cursor.fetchall())
    if len(rows) > 1:
        raise OrganizationError("ORG_INVITEE_TARGET_AMBIGUOUS", "邀请联系方式绑定了多个账号，请联系平台客服处理", http_status=503)
    return dict(rows[0]) if rows else None


def _require_operational_config(cursor) -> dict[str, Any]:
    from services.organization_service import _load_product_config

    config = _load_product_config(cursor)
    metadata = config["metadata"]
    if not bool(metadata.get("operational")) or int(config["included_seats"]) <= 0:
        raise OrganizationError(
            "ORG_PRODUCT_CONFIG_GOVERNANCE_ONLY",
            "团队已创建，但员工席位尚未由平台管理员发布",
            http_status=409,
            safe_details={"admin_action": "publish_organization_seat_policy"},
        )
    for field in ("verification_ttl_minutes", "verification_max_attempts"):
        if not isinstance(metadata.get(field), int) or int(metadata[field]) <= 0:
            raise OrganizationError("ORG_PRODUCT_CONFIG_INVALID", "平台配置异常，请联系平台客服", http_status=503, safe_details={"field": field})
    return config


def _enforce_public_invite_rate(*, token: str, action: str, source_ip: str) -> None:
    from services.organization_service import _enforce_invite_rate, _invite_rate_subject

    subject = _invite_rate_subject(token)
    if subject:
        _enforce_invite_rate(
            action=action,
            dimensions=("organization", "target", "ip"),
            source_ip=source_ip,
            organization_id=subject[0],
            target_digest=subject[1],
        )
    else:
        _enforce_invite_rate(
            action=action,
            dimensions=("ip",),
            source_ip=source_ip,
        )


def _resolve_live_public_invite(
    cursor,
    token: str,
    *,
    for_update: bool,
    allow_accepted: bool = False,
) -> dict[str, Any]:
    from services.organization_service import _resolve_invite_by_token

    invite = _resolve_invite_by_token(cursor, token, for_update=for_update)
    if not invite:
        raise OrganizationError("ORG_INVITE_INVALID", "邀请不存在或已失效", http_status=404)
    if invite["status"] != "pending":
        if allow_accepted and invite["status"] == "accepted":
            return invite
        raise OrganizationError(f"ORG_INVITE_{str(invite['status']).upper()}", "邀请已失效", http_status=410)
    if invite["expires_at"] <= _utcnow():
        if for_update:
            cursor.execute(
                "UPDATE organization_invites SET status='expired',version=version+1,updated_at=NOW() WHERE id=%s AND status='pending'",
                (invite["id"],),
            )
        raise OrganizationError("ORG_INVITE_EXPIRED", "邀请已过期，请让团队负责人重新邀请", http_status=410)
    if invite["organization_status"] != "active":
        raise OrganizationError("ORG_INACTIVE", "团队当前不可加入", http_status=403)
    return invite


def inspect_public_invite(*, token: str, request_id: str, source_ip: str) -> dict[str, Any]:
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    require_feature_flag("ORGANIZATION_INVITE_ONBOARDING_ENABLED")
    # inspect 留在 invite.verify 桶：它是落地页一次性加载（非轮询），且与
    # verify 同属"匿名 token 持有者的邀请信息面"，共享反枚举小桶是刻意的；
    # 只有设计上的高频轮询（delivery-status）才拆到独立放量桶。
    _enforce_public_invite_rate(token=token, action="invite.verify", source_ip=source_ip)
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        invite = _resolve_live_public_invite(cursor, token, for_update=False)
        _require_operational_config(cursor)
        target = _invite_target(invite)
        existing = _lookup_target_user(cursor, target_kind=str(invite["target_kind"]), normalized=target, lock=False)
        return {
            "status": "pending",
            "organization_name": str(invite["organization_name"]),
            "role_name": str(invite["role_name"]),
            "target_kind": str(invite["target_kind"]),
            "account_mode": "sign_in" if existing else "create_operator",
            "account_available": bool(existing is None or existing.get("is_active")),
            "expires_at": invite["expires_at"],
            "agreements": {
                "user_terms_version": USER_TERMS_VERSION,
                "privacy_version": PRIVACY_VERSION,
            },
            "request_id": str(request_id),
        }


def _derived_code(derivation_id: str, *, key_version: Optional[str] = None) -> tuple[str, str, str]:
    bearer, _, version = derive_bearer_token(
        purpose="organization-invite-code",
        stable_material=str(derivation_id),
        key_version=key_version,
    )
    code = f"{int(sha256(bearer.encode('ascii')).hexdigest()[:12], 16) % 1000000:06d}"
    digest, _ = hash_bearer_token(
        purpose="organization-invite-code",
        token=code,
        key_version=version,
    )
    return code, digest, version


def _local_capture_enabled() -> bool:
    provider = os.getenv("ORGANIZATION_INVITE_DELIVERY_PROVIDER", "disabled").strip().lower()
    environment = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "")).strip().lower()
    return provider == "local_capture" and environment in {"development", "dev", "test", "local"}


# ========== 邀请送达 provider 治理 ==========
#
# 白名单：只有经过批准的正式 provider 才放行 challenge 创建与真实外发；
# disabled / 未知值一律 503，绝不假装发送。
APPROVED_DELIVERY_PROVIDERS = frozenset({"aliyun_sms"})

# outbox 发送治理：指数退避 1m/5m/15m/1h，最多 5 次尝试后 dead-letter 终态。
DELIVERY_MAX_ATTEMPTS = 5
DELIVERY_BACKOFF_SECONDS = (60, 300, 900, 3600)
DELIVERY_LEASE_SECONDS = 300

# outbox CHECK 约束没有 failed 值；终态失败 = status='cancelled' +
# last_error_code 'FAILED:<确定性错误码>'（dead-letter 语义，机读可判）。
DELIVERY_DEAD_LETTER_PREFIX = "FAILED:"


def _delivery_provider() -> str:
    return os.getenv("ORGANIZATION_INVITE_DELIVERY_PROVIDER", "disabled").strip().lower()


def create_verification_challenge(
    *, token: str, request_id: str, source_ip: str,
) -> dict[str, Any]:
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    require_feature_flag("ORGANIZATION_INVITE_ONBOARDING_ENABLED")
    _enforce_public_invite_rate(token=token, action="invite.verify", source_ip=source_ip)
    if not _local_capture_enabled() and _delivery_provider() not in APPROVED_DELIVERY_PROVIDERS:
        raise OrganizationError(
            "ORG_INVITE_DELIVERY_NOT_CONFIGURED",
            "短信验证暂不可用，请联系平台客服",
            http_status=503,
        )
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        invite = _resolve_live_public_invite(cursor, token, for_update=True)
        if str(invite["target_kind"]) == "username":
            # [WP6] 用户名式邀请没有联系方式可发验证码;它走凭证式开户(onboard-credential),
            # 绝不为其伪造 challenge。fail-closed。
            raise OrganizationError(
                "ORG_INVITE_CREDENTIAL_MODE",
                "本邀请无需验证码，直接设置密码即可开通账号", http_status=409,
            )
        if str(invite["target_kind"]) == "email" and not _local_capture_enabled():
            # 全仓没有邮件 adapter：邮箱邀请显式不可用，绝不假装发送成功。
            raise OrganizationError(
                "ORG_INVITE_EMAIL_UNAVAILABLE",
                "邮箱邀请暂不可用，请使用手机号接收邀请",
                http_status=503,
            )
        config = _require_operational_config(cursor)
        target = _invite_target(invite)
        existing = _lookup_target_user(cursor, target_kind=str(invite["target_kind"]), normalized=target, lock=True)
        if existing:
            raise OrganizationError("ORG_INVITEE_ACCOUNT_EXISTS", "该联系方式已有账号，请先登录再接受邀请", http_status=409)
        cursor.execute(
            "SELECT * FROM organization_invite_verification_challenges WHERE request_id=%s FOR UPDATE",
            (str(request_id),),
        )
        replay = cursor.fetchone()
        if replay:
            if int(replay["invite_id"]) != int(invite["id"]):
                raise OrganizationError("ORG_IDEMPOTENCY_CONFLICT", "操作冲突，请刷新页面重试", http_status=409)
            code, _, _ = _derived_code(str(replay["receipt_derivation_id"]), key_version=str(replay["code_key_version"]))
            return {
                "challenge_id": int(replay["id"]),
                "status": str(replay["status"]),
                "expires_at": replay["expires_at"],
                "delivery_queued": True,
                "replayed": True,
                **({"test_code": code} if _local_capture_enabled() else {}),
            }
        # 重新获取验证码 = 旧 challenge/旧 outbox 立即作废，只有一个活跃验证码。
        cursor.execute(
            """UPDATE organization_invite_verification_challenges
               SET status='cancelled',updated_at=NOW()
               WHERE invite_id=%s AND status IN ('pending','verified') RETURNING id""",
            (invite["id"],),
        )
        superseded_ids = [int(row["id"]) for row in cursor.fetchall()]
        if superseded_ids:
            cursor.execute(
                """UPDATE organization_invite_delivery_outbox
                   SET status='cancelled',claim_token=NULL,lease_expires_at=NULL,
                       last_error_code='CHALLENGE_SUPERSEDED',updated_at=NOW()
                   WHERE challenge_id=ANY(%s) AND status IN ('pending','retry','sending','unknown')""",
                (superseded_ids,),
            )
        derivation_id = str(uuid.uuid4())
        code, code_hash, code_version = _derived_code(derivation_id)
        expires_at = _utcnow() + timedelta(minutes=int(config["metadata"]["verification_ttl_minutes"]))
        cursor.execute(
            """INSERT INTO organization_invite_verification_challenges(
                 invite_id,invite_version,organization_id,target_kind,target_hmac,
                 product_catalog_version_id,code_hash,code_key_version,receipt_derivation_id,
                 max_attempts,expires_at,request_id
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                invite["id"], invite["version"], invite["organization_id"], invite["target_kind"],
                invite["target_hmac"], config["version_id"], code_hash, code_version, derivation_id,
                int(config["metadata"]["verification_max_attempts"]), expires_at, str(request_id),
            ),
        )
        challenge = cursor.fetchone()
        code_ciphertext, encryption_version = encrypt_delivery_target(str(invite["target_kind"]), code)
        cursor.execute(
            """INSERT INTO organization_invite_delivery_outbox(
                 invite_id,challenge_id,event_kind,target_kind,target_hmac,
                 delivery_ciphertext,payload_ciphertext,encryption_key_version,request_id
               ) VALUES (%s,%s,'verification_code',%s,%s,%s,%s,%s,%s)""",
            (
                invite["id"], challenge["id"], invite["target_kind"], invite["target_hmac"],
                invite["delivery_ciphertext_or_reference"], code_ciphertext, encryption_version,
                f"challenge:{request_id}",
            ),
        )
        return {
            "challenge_id": int(challenge["id"]),
            "status": "pending",
            "expires_at": expires_at,
            "delivery_queued": True,
            "replayed": False,
            **({"test_code": code} if _local_capture_enabled() else {}),
        }


def verify_challenge(
    *, challenge_id: int, token: str, code: str, request_id: str, source_ip: str,
) -> dict[str, Any]:
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    require_feature_flag("ORGANIZATION_INVITE_ONBOARDING_ENABLED")
    _enforce_public_invite_rate(token=token, action="invite.verify", source_ip=source_ip)
    normalized_code = str(code or "").strip()
    if not re.fullmatch(r"[0-9]{6}", normalized_code):
        raise OrganizationError("ORG_INVITE_CODE_INVALID", "请输入 6 位数字验证码", http_status=422)
    deferred_error: Optional[OrganizationError] = None
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        invite = _resolve_live_public_invite(cursor, token, for_update=True)
        _require_operational_config(cursor)
        cursor.execute(
            """SELECT * FROM organization_invite_verification_challenges
               WHERE id=%s AND invite_id=%s FOR UPDATE""",
            (int(challenge_id), invite["id"]),
        )
        challenge = cursor.fetchone()
        if not challenge:
            raise OrganizationError("ORG_INVITE_CHALLENGE_INVALID", "验证已失效，请重新获取验证码", http_status=404)
        if challenge["status"] == "verified":
            receipt, _, _ = derive_bearer_token(
                purpose="organization-invite-verification-receipt",
                stable_material=str(challenge["receipt_derivation_id"]),
                key_version=str(challenge["receipt_key_version"]),
            )
            return {"challenge_id": int(challenge_id), "status": "verified", "verification_receipt": receipt, "replayed": True}
        if challenge["status"] != "pending" or challenge["expires_at"] <= _utcnow():
            if challenge["status"] == "pending":
                cursor.execute(
                    "UPDATE organization_invite_verification_challenges SET status='expired',updated_at=NOW() WHERE id=%s",
                    (int(challenge_id),),
                )
            deferred_error = OrganizationError(
                "ORG_INVITE_CHALLENGE_EXPIRED", "验证码已失效", http_status=410,
            )
        else:
            # 送达证据闸：只有 outbox 真实 sent（回指本 challenge）后才允许核验，
            # 防止"队列里躺着就说已发送"的假成功。local_capture 属测试通道，
            # 其送达即同步可读，豁免该闸。
            delivered = True
            if not _local_capture_enabled():
                cursor.execute(
                    """SELECT 1 FROM organization_invite_delivery_outbox
                       WHERE challenge_id=%s AND status='sent' LIMIT 1""",
                    (int(challenge_id),),
                )
                delivered = cursor.fetchone() is not None
            if not delivered:
                deferred_error = OrganizationError(
                    "ORG_INVITE_CODE_NOT_DELIVERED",
                    "验证码尚未送达，请稍后重试或重新获取",
                    http_status=409,
                    retryable=True,
                )
            else:
                actual_hash, _ = hash_bearer_token(
                    purpose="organization-invite-code",
                    token=normalized_code,
                    key_version=str(challenge["code_key_version"]),
                )
                if not secure_compare(actual_hash, str(challenge["code_hash"])):
                    next_attempt = int(challenge["attempt_count"]) + 1
                    status = "locked" if next_attempt >= int(challenge["max_attempts"]) else "pending"
                    cursor.execute(
                        """UPDATE organization_invite_verification_challenges
                           SET attempt_count=%s,status=%s,updated_at=NOW() WHERE id=%s""",
                        (next_attempt, status, int(challenge_id)),
                    )
                    deferred_error = OrganizationError(
                        "ORG_INVITE_CODE_LOCKED" if status == "locked" else "ORG_INVITE_CODE_MISMATCH",
                        "验证码尝试次数已用完，请重新获取验证码" if status == "locked" else "验证码不正确",
                        http_status=429 if status == "locked" else 422,
                        safe_details={"remaining_attempts": max(int(challenge["max_attempts"]) - next_attempt, 0)},
                    )
                else:
                    receipt, receipt_hash, receipt_version = derive_bearer_token(
                        purpose="organization-invite-verification-receipt",
                        stable_material=str(challenge["receipt_derivation_id"]),
                    )
                    cursor.execute(
                        """UPDATE organization_invite_verification_challenges
                           SET status='verified',verified_at=NOW(),receipt_hash=%s,receipt_key_version=%s,
                               attempt_count=attempt_count+1,updated_at=NOW()
                           WHERE id=%s AND status='pending'""",
                        (receipt_hash, receipt_version, int(challenge_id)),
                    )
                    if cursor.rowcount != 1:
                        raise OrganizationError("ORG_INVITE_CHALLENGE_CONFLICT", "验证码已失效，请重新获取", http_status=409, retryable=True)
                    return {"challenge_id": int(challenge_id), "status": "verified", "verification_receipt": receipt, "replayed": False}
    if deferred_error is None:
        raise RuntimeError("verification challenge exited without a result")
    raise deferred_error

def _operator_session_payload(user_id: int) -> dict[str, Any]:
    """为新开户员工签发与登录一致的会话（token + user 摘要）。

    只调用 auth/jwt_utils.create_jwt 与 db.auth_db.get_user（只调不改）。
    会话签发失败绝不回滚已提交的开户；前端退化为手动登录。
    """
    try:
        from auth.jwt_utils import create_jwt
        from db.auth_db import get_user

        token = create_jwt(int(user_id))
        user = get_user(int(user_id)) if token else None
    except Exception as exc:
        logger.warning(
            "[onboard] 会话签发失败 user_id=%s error=%s",
            int(user_id), type(exc).__name__,
        )
        return {"success": True, "auto_login": False}
    if not token or not user:
        return {"success": True, "auto_login": False}
    return {"success": True, "auto_login": True, "token": token, "user": user}


def onboard_operator(
    *,
    token: str,
    challenge_id: int,
    verification_receipt: str,
    password: str,
    display_name: str,
    request_id: str,
    terms_accepted: bool,
    privacy_accepted: bool,
    terms_version: str,
    privacy_version: str,
    source_ip: str,
    user_agent: str,
) -> dict[str, Any]:
    """开户成功（含幂等重放）后直接签发 JWT，响应结构与登录一致。"""
    result = _onboard_operator_core(
        token=token,
        challenge_id=challenge_id,
        verification_receipt=verification_receipt,
        password=password,
        display_name=display_name,
        request_id=request_id,
        terms_accepted=terms_accepted,
        privacy_accepted=privacy_accepted,
        terms_version=terms_version,
        privacy_version=privacy_version,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return _finish_onboarding(result)


def onboard_operator_via_credential(
    *,
    token: str,
    password: str,
    display_name: str,
    request_id: str,
    terms_accepted: bool,
    privacy_accepted: bool,
    terms_version: str,
    privacy_version: str,
    source_ip: str,
    user_agent: str,
) -> dict[str, Any]:
    """[WP6] 用户名式邀请开户:owner 已设定登录用户名,被邀请人凭邀请 token 直接进入并
    自设初始密码,不走短信/邮箱验证码。仅对 target_kind='username' 的邀请生效(core 内
    fail-closed 校验),其余安全不变(live invite / 一次性 / 幂等 / 席位与角色版本 / 绑定
    组织·角色·代际·access policy)。成功后与登录一致直接签发 JWT。"""
    result = _onboard_operator_core(
        token=token,
        password=password,
        display_name=display_name,
        request_id=request_id,
        terms_accepted=terms_accepted,
        privacy_accepted=privacy_accepted,
        terms_version=terms_version,
        privacy_version=privacy_version,
        source_ip=source_ip,
        user_agent=user_agent,
        verification_mode="fragment",
    )
    return _finish_onboarding(result)


def _finish_onboarding(result: dict[str, Any]) -> dict[str, Any]:
    """开户**提交之后**的收尾:补钱包行 + 签发会话。两个公开入口共用。

    🔴 [#139 · 2026-09-07] 建钱包行这件事以前只有**自助注册**那条路做
       (`api/auth_api.py:549`)。组织邀请接受这条路建了 users 行却不建钱包行,
       于是 `admin_user_governance._assert_identity_ssot` 的全表扫描一命中就
       `raise`,**整个管理端用户列表拒读**(Owner 2026-09-07 报的现象)。

    🔴 必须在 `_onboard_operator_core` **返回之后**调 —— 它的 `with get_db()`
       到函数末尾才退出,`get_or_create_wallet` 自开连接:放事务里的话,
       外层一旦回滚就会留下**指向不存在用户**的钱包行(或当场撞 FK)。

    🔴 抽成一个收尾函数而不是给两个入口各打一个补丁:两处原本一字不差,
       补丁打两遍迟早漂一处,而漂了不会有任何东西报错。第三个入口以后自动带上。
    """
    user_id = int(result["user_id"])
    try:
        from db.wallet_db import get_or_create_wallet

        get_or_create_wallet(user_id)
    except Exception as exc:      # noqa: BLE001
        # 🔴 不阻断开户(人已经建出来了,回滚它比缺一行钱包更糟),
        #    但**不静默**:打 ERROR 并说清后果,别让它长得像一条普通提示。
        #    残留由列表侧的按行降级兜住(该行标 unverified + attention)。
        logger.error(
            "[#139] 开户后建钱包行失败 user=%s —— 该账号在管理端会显示为"
            "「尚未初始化钱包」,本人登录一次即自动补齐:%s", user_id, exc)
    return {**result, **_operator_session_payload(user_id)}

def _onboard_operator_core(
    *,
    token: str,
    password: str,
    display_name: str,
    request_id: str,
    terms_accepted: bool,
    privacy_accepted: bool,
    terms_version: str,
    privacy_version: str,
    source_ip: str,
    user_agent: str,
    verification_mode: str = "challenge",
    challenge_id: Optional[int] = None,
    verification_receipt: Optional[str] = None,
) -> dict[str, Any]:
    # [WP6 §7.4] 两种联系方式验证语义:
    #  - "challenge":走短信/本地捕获的一次性验证码(默认;SMS 可用时唯一路径);
    #  - "fragment":SMS 未配时的高熵一次性 fragment-token 兜底——凭邀请链接内的高熵 token
    #    (等价 out-of-band 交付的凭证)+ 自设密码开户,服务端仍绑邀请的联系方式/组织/角色/
    #    代际/access policy,用户不能改任何绑定项。fragment 路径只在 SMS 真不可用时放行。
    if verification_mode not in {"challenge", "fragment"}:
        raise OrganizationError("ORG_INVITE_VERIFICATION_MODE_INVALID", "邀请链接异常，请让团队负责人重新邀请", http_status=422)
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    require_feature_flag("ORGANIZATION_INVITE_ONBOARDING_ENABLED")
    _enforce_public_invite_rate(token=token, action="invite.accept", source_ip=source_ip)
    if not terms_accepted or not privacy_accepted or terms_version != USER_TERMS_VERSION or privacy_version != PRIVACY_VERSION:
        raise OrganizationError("ORG_AGREEMENT_REQUIRED", "请阅读并同意当前用户协议与隐私政策", http_status=422)
    display_name = str(display_name or "").strip()
    if not 1 <= len(display_name) <= 80:
        raise OrganizationError("ORG_OPERATOR_NAME_INVALID", "员工姓名不能为空且不能超过 80 字", http_status=422)
    if len(str(password or "")) < MIN_OPERATOR_PASSWORD_LENGTH or len(str(password)) > 128:
        raise OrganizationError(
            "ORG_OPERATOR_PASSWORD_INVALID",
            f"密码至少 {MIN_OPERATOR_PASSWORD_LENGTH} 位且不能超过 128 位", http_status=422,
        )
    from db.auth_db import hash_password
    from services.organization_service import (
        _apply_invite_access_policy,
        _audit,
        _cancel_invite_onboarding,
        _lock_entitlement_and_count,
    )

    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        invite = _resolve_live_public_invite(cursor, token, for_update=True, allow_accepted=True)
        if verification_mode == "challenge":
            cursor.execute(
                """SELECT * FROM organization_invite_verification_challenges
                   WHERE id=%s AND invite_id=%s FOR UPDATE""",
                (int(challenge_id), invite["id"]),
            )
            challenge = cursor.fetchone()
            if not challenge or challenge["status"] not in {"verified", "consumed"}:
                raise OrganizationError("ORG_INVITE_VERIFICATION_REQUIRED", "请先完成手机号验证", http_status=403)
            receipt_hash, _ = hash_bearer_token(
                purpose="organization-invite-verification-receipt",
                token=str(verification_receipt),
                key_version=str(challenge["receipt_key_version"]),
            )
            if not secure_compare(receipt_hash, str(challenge["receipt_hash"])):
                raise OrganizationError("ORG_INVITE_VERIFICATION_INVALID", "验证已失效，请重新验证", http_status=403)
            receipt_key_version = str(challenge["receipt_key_version"])
        else:  # fragment:owner 设定登录用户名 + 被邀请人凭 token 进入并自设初始密码
            # [WP6] fail-closed(服务端方法权威):只有 owner 明确以"用户名"方式发出的邀请
            # (target_kind='username')才允许凭 token 直进;短信/邮箱邀请必须走验证码,不能用
            # fragment 绕过联系方式验证。被邀请人不能改 owner 设定的登录名/组织/角色/代际。
            if str(invite["target_kind"]) != "username":
                raise OrganizationError(
                    "ORG_INVITE_VERIFICATION_REQUIRED",
                    "该邀请需通过短信验证码完成邀请验证", http_status=403,
                )
            challenge = None
            challenge_id = 0
            # username 邀请无 challenge/receipt 密钥;password_request_hmac 只用于本次
            # accept 幂等摘要,用 active HMAC 密钥版本即可(None → hash_bearer_token 取 active)。
            receipt_key_version = None
        target = _invite_target(invite)
        password_request_hmac, _ = hash_bearer_token(
            purpose="organization-invite-onboard-password-request",
            token=str(password),
            key_version=receipt_key_version,
        )
        accept_digest = payload_hash({
            "invite_id": int(invite["id"]),
            "challenge_id": int(challenge_id),
            "verification_mode": verification_mode,
            "target_hmac": invite["target_hmac"],
            "display_name": display_name,
            "password_request_hmac": password_request_hmac,
            "terms_version": terms_version,
            "privacy_version": privacy_version,
            "access_policy_hash": invite["access_policy_hash"],
        })
        cursor.execute(
            """SELECT * FROM organization_invite_accept_receipts
               WHERE accepted_request_id=%s FOR UPDATE""",
            (str(request_id),),
        )
        replay = cursor.fetchone()
        if replay:
            if int(replay["invite_id"]) != int(invite["id"]) or replay["accept_request_hash"] != accept_digest:
                raise OrganizationError("ORG_INVITE_IDEMPOTENCY_CONFLICT", "操作冲突，请刷新页面重试", http_status=409)
            result = _json_object(replay["accepted_result"])
            return {**result, "replayed": True}
        if (challenge is not None and challenge["status"] == "consumed") or invite["status"] != "pending":
            raise OrganizationError("ORG_INVITE_IDEMPOTENCY_CONFLICT", "邀请已被接受，请直接登录", http_status=409)
        config = _require_operational_config(cursor)
        if _lookup_target_user(cursor, target_kind=str(invite["target_kind"]), normalized=target, lock=True):
            raise OrganizationError("ORG_INVITEE_ACCOUNT_EXISTS", "该联系方式已有账号，请登录后接受邀请", http_status=409)
        cursor.execute(
            """SELECT id FROM organization_memberships
               WHERE user_id IN (SELECT id FROM users WHERE username=%s)
                 AND status IN ('active','suspended','leaving') FOR UPDATE""",
            (target,),
        )
        if cursor.fetchone():
            raise OrganizationError("ORG_USER_ALREADY_MEMBER_OTHER_ORG", "该联系方式已属于其他团队", http_status=409)
        entitlement, occupied = _lock_entitlement_and_count(cursor, int(invite["organization_id"]))
        if int(entitlement["product_catalog_version_id"]) != int(config["version_id"]):
            raise OrganizationError("ORG_SEAT_ENTITLEMENT_STALE", "团队席位配置已变化，请重试", http_status=409, retryable=True)
        if occupied > int(entitlement["entitled_seats"]):
            raise OrganizationError("ORG_SEAT_LIMIT_REACHED", "员工席位已用完，请联系团队负责人", http_status=409)
        cursor.execute(
            """SELECT capability FROM organization_role_capabilities
               WHERE role_id=%s AND effect='allow' ORDER BY capability""",
            (invite["role_id"],),
        )
        capabilities = [row["capability"] for row in cursor.fetchall()]
        if int(invite["role_version"]) != int(invite["current_role_version"]) or capability_snapshot_hash(capabilities) != invite["capability_snapshot_hash"]:
            raise OrganizationError("ORG_INVITE_ROLE_CHANGED", "邀请权限已变化，请老板重新邀请", http_status=409)
        username = target
        phone = target if invite["target_kind"] == "phone" else None
        email = target if invite["target_kind"] == "email" else None
        try:
            cursor.execute(
                """INSERT INTO users(
                     username,password_hash,display_name,is_active,permission_version,
                     must_change_password,phone,phone_verified,email,email_verified,email_verified_for
                   ) VALUES (%s,%s,%s,1,1,0,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    username, hash_password(str(password)), display_name, phone,
                    bool(phone), email, bool(email), email if email else None,
                ),
            )
        except Exception as exc:
            if getattr(exc, "pgcode", None) == "23505":
                raise OrganizationError(
                    "ORG_INVITEE_ACCOUNT_EXISTS",
                    "该联系方式已有账号，请登录后接受邀请",
                    http_status=409,
                ) from exc
            raise
        user_id = int(cursor.fetchone()["id"])
        evidence = canonical_json({
            "surface": "organization-invite-onboarding",
            "explicit_acceptance": True,
            "invite_id": int(invite["id"]),
            "challenge_id": int(challenge_id),
            "verification_mode": verification_mode,
        })
        for agreement_type, agreement_version, content_hash in (
            ("user_terms", USER_TERMS_VERSION, USER_TERMS_CONTENT_HASH),
            ("privacy", PRIVACY_VERSION, PRIVACY_CONTENT_HASH),
        ):
            cursor.execute(
                """INSERT INTO agreement_signatures(
                     user_id,agreement_type,agreement_version,content_hash,ip_address,user_agent,evidence_jsonb
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (user_id, agreement_type, agreement_version, content_hash, str(source_ip)[:45], str(user_agent)[:2000], evidence),
            )
        cursor.execute(
            """INSERT INTO organization_memberships(organization_id,user_id,role_id,status,is_owner)
               VALUES (%s,%s,%s,'active',FALSE) RETURNING *""",
            (invite["organization_id"], user_id, invite["role_id"]),
        )
        membership = cursor.fetchone()
        snapshot_and_clear_legacy_access(
            cursor,
            organization_id=int(invite["organization_id"]),
            membership_id=int(membership["id"]),
            user_id=user_id,
            dedicated_operator=True,
        )
        provisional_identity = IdentityContext(
            request_id=str(request_id), authenticated_user_id=user_id,
            principal_user_id=int(invite["owner_user_id"]), payer_user_id=int(invite["owner_user_id"]),
            actor_kind="member", organization_id=int(invite["organization_id"]),
            actor_user_id=user_id, membership_id=int(membership["id"]), role_id=int(invite["role_id"]),
            membership_version=1,
            capability_version=1,
            assignment_version=1,
            capabilities=frozenset(capabilities),
        )
        applied_policy = _apply_invite_access_policy(
            cursor,
            invite=invite,
            membership_id=int(membership["id"]),
            user_id=user_id,
            request_id=str(request_id),
            identity=provisional_identity,
        )
        cursor.execute(
            """INSERT INTO organization_operator_accounts(
                 user_id,organization_id,membership_id,invite_id,principal_user_id,
                 target_kind,target_hmac
               ) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (
                user_id, invite["organization_id"], membership["id"], invite["id"],
                invite["owner_user_id"], invite["target_kind"], invite["target_hmac"],
            ),
        )
        result = {
            "user_id": user_id,
            "login_username": username,
            "membership_id": int(membership["id"]),
            "organization_id": int(invite["organization_id"]),
            "status": "active",
            "account_origin": "organization_invite",
            "permission_version": int(applied_policy["permission_version"]),
            "access_policy_hash": invite["access_policy_hash"],
        }
        cursor.execute(
            """INSERT INTO organization_invite_accept_receipts(
                 invite_id,accepted_user_id,accepted_request_id,accept_request_hash,accepted_result
               ) VALUES (%s,%s,%s,%s,%s::jsonb)""",
            (invite["id"], user_id, str(request_id), accept_digest, canonical_json(result)),
        )
        cursor.execute(
            """UPDATE organization_invites
               SET status='accepted',accepted_membership_id=%s,accepted_at=NOW(),
                   version=version+1,updated_at=NOW() WHERE id=%s AND status='pending'""",
            (membership["id"], invite["id"]),
        )
        if cursor.rowcount != 1:
            raise OrganizationError("ORG_INVITE_IDEMPOTENCY_CONFLICT", "操作冲突，请稍后重试", http_status=409)
        if verification_mode == "challenge":
            cursor.execute(
                """UPDATE organization_invite_verification_challenges
                   SET status='consumed',consumed_at=NOW(),updated_at=NOW()
                   WHERE id=%s AND status='verified'""",
                (int(challenge_id),),
            )
            if cursor.rowcount != 1:
                raise OrganizationError("ORG_INVITE_VERIFICATION_CONFLICT", "操作冲突，请稍后重试", http_status=409)
        _cancel_invite_onboarding(cursor, [int(invite["id"])], reason="INVITE_ACCEPTED")
        cursor.execute(
            "UPDATE organizations SET authority_version=authority_version+1,updated_at=NOW() WHERE id=%s",
            (invite["organization_id"],),
        )
        identity = IdentityContext(
            request_id=str(request_id), authenticated_user_id=user_id,
            principal_user_id=int(invite["owner_user_id"]), payer_user_id=int(invite["owner_user_id"]),
            actor_kind="member", organization_id=int(invite["organization_id"]),
            actor_user_id=user_id, membership_id=int(membership["id"]), role_id=int(invite["role_id"]),
            membership_version=int(applied_policy["version"]), capability_version=int(applied_policy["capability_version"]), assignment_version=int(applied_policy["assignment_version"]),
            capabilities=frozenset(capabilities),
        )
        _audit(
            cursor, identity, action="invite.onboard", entity_type="organization_membership",
            entity_id=membership["id"],
            after={
                "invite_id": invite["id"],
                "account_origin": "organization_invite",
                "access_policy_hash": invite["access_policy_hash"],
            },
        )
        return {**result, "replayed": False}


def _delivery_tick_local_capture(*, limit: int) -> dict[str, Any]:
    """测试通道：确定性标记 sent，绝不真实外发。"""
    processed: list[int] = []
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        cursor.execute(
            """SELECT d.id,i.status AS invite_status,i.expires_at,o.status AS organization_status
               FROM organization_invite_delivery_outbox d
               JOIN organization_invites i ON i.id=d.invite_id
               JOIN organizations o ON o.id=i.organization_id
               WHERE d.status IN ('pending','retry') AND d.next_attempt_at<=NOW()
               ORDER BY d.id FOR UPDATE OF d SKIP LOCKED LIMIT %s""",
            (max(1, min(int(limit), 500)),),
        )
        rows = list(cursor.fetchall())
        for row in rows:
            if row["invite_status"] != "pending" or row["expires_at"] <= _utcnow() or row["organization_status"] != "active":
                cursor.execute(
                    """UPDATE organization_invite_delivery_outbox
                       SET status='cancelled',updated_at=NOW(),last_error_code='INVITE_NOT_LIVE'
                       WHERE id=%s""",
                    (row["id"],),
                )
            else:
                cursor.execute(
                    """UPDATE organization_invite_delivery_outbox
                       SET status='sent',sent_at=NOW(),attempt_count=attempt_count+1,
                           provider_message_reference='local-capture',updated_at=NOW()
                       WHERE id=%s""",
                    (row["id"],),
                )
            processed.append(int(row["id"]))
    return {"processed": processed, "deferred": [], "real_messages_sent": 0, "provider": "local_capture"}


def _claim_delivery_rows(*, limit: int) -> list[dict[str, Any]]:
    """单事务：回收过期 lease 为 unknown；SKIP LOCKED 认领并置 sending+lease。

    20 个 worker 并发时同一行只有一个 claim 成功，真实发送最多一次。
    """
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        # lease 过期 = 发送结果未知（可能已发出但回执丢失）→ unknown 态。
        # unknown 行仍在认领索引内，会被重新认领补发；验证码按 challenge
        # 确定性派生，补发内容与首次完全一致，重复短信无副作用。
        # R3：回收即计一次 attempt 并按既有 DELIVERY_BACKOFF_SECONDS 档排程，
        # 否则 worker 崩溃/部署窗口会让 sending→unknown→sending 无计数、
        # 无退避空转，每 300s 真实补发一条短信直到邀请过期（≤30 天）；
        # 达 DELIVERY_MAX_ATTEMPTS 直接 dead-letter 终态（与
        # _finalize_delivery 同一语义：cancelled + FAILED: 前缀）。
        backoff_case = " ".join(
            f"WHEN {index + 1} THEN {seconds}"
            for index, seconds in enumerate(DELIVERY_BACKOFF_SECONDS)
        )
        cursor.execute(
            f"""UPDATE organization_invite_delivery_outbox
               SET status='unknown',claim_token=NULL,lease_expires_at=NULL,
                   attempt_count=attempt_count+1,
                   next_attempt_at=NOW()+make_interval(secs=>CASE LEAST(attempt_count+1,{len(DELIVERY_BACKOFF_SECONDS)}) {backoff_case} END),
                   last_error_code='UNKNOWN_AFTER_LEASE_EXPIRY',updated_at=NOW()
               WHERE status='sending' AND lease_expires_at<=NOW()
                 AND attempt_count+1<%s""",
            (DELIVERY_MAX_ATTEMPTS,),
        )
        cursor.execute(
            """UPDATE organization_invite_delivery_outbox
               SET status='cancelled',claim_token=NULL,lease_expires_at=NULL,
                   attempt_count=attempt_count+1,last_error_code=%s,updated_at=NOW()
               WHERE status='sending' AND lease_expires_at<=NOW()""",
            (f"{DELIVERY_DEAD_LETTER_PREFIX}MAX_ATTEMPTS:UNKNOWN_AFTER_LEASE_EXPIRY",),
        )
        cursor.execute(
            """WITH due AS (
                 SELECT d.id
                 FROM organization_invite_delivery_outbox d
                 WHERE d.status IN ('pending','retry','unknown') AND d.next_attempt_at<=NOW()
                 ORDER BY d.id FOR UPDATE SKIP LOCKED LIMIT %s
               )
               UPDATE organization_invite_delivery_outbox d
               SET status='sending',claim_token=gen_random_uuid(),
                   lease_expires_at=NOW()+make_interval(secs=>%s),updated_at=NOW()
               FROM due WHERE d.id=due.id
               RETURNING d.*""",
            (max(1, min(int(limit), 500)), DELIVERY_LEASE_SECONDS),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        if not rows:
            return []
        cursor.execute(
            """SELECT d.id,i.status AS invite_status,i.expires_at AS invite_expires_at,
                      o.status AS organization_status,
                      c.status AS challenge_status,c.expires_at AS challenge_expires_at
               FROM organization_invite_delivery_outbox d
               JOIN organization_invites i ON i.id=d.invite_id
               JOIN organizations o ON o.id=i.organization_id
               LEFT JOIN organization_invite_verification_challenges c ON c.id=d.challenge_id
               WHERE d.id=ANY(%s)""",
            ([int(row["id"]) for row in rows],),
        )
        meta = {int(row["id"]): row for row in cursor.fetchall()}
        for row in rows:
            row.update(meta.get(int(row["id"])) or {})
        return rows


def _decrypt_outbox_phone(row: Mapping[str, Any]) -> str:
    version, ciphertext = _split_ciphertext(str(row["delivery_ciphertext"]))
    return decrypt_delivery_target(str(row["target_kind"]), ciphertext, version)


def _deliver_claimed_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """对已认领行执行一次真实发送（事务外）。只返回分类结果，不碰 DB。"""
    from auth.sms_service import send_verification_code

    if (
        row.get("invite_status") != "pending"
        or row["invite_expires_at"] <= _utcnow()
        or row.get("organization_status") != "active"
    ):
        return {"kind": "cancelled", "error_code": "INVITE_NOT_LIVE"}
    # challenge TTL 闸：验证码已过期仍外发，用户只会收到一个必定 410 的码，
    # 还白扣一条平台短信配额。直接确定性终态，绝不外发；invite_link 行
    # 没有 challenge（challenge_id NULL），天然豁免此闸。
    challenge_expires_at = row.get("challenge_expires_at")
    if challenge_expires_at is not None and challenge_expires_at <= _utcnow():
        return {"kind": "terminal", "error_code": "CHALLENGE_EXPIRED"}
    try:
        if str(row["target_kind"]) != "phone":
            # 邮箱没有 adapter：显式终态，绝不假装发送。
            return {"kind": "terminal", "error_code": "EMAIL_UNAVAILABLE"}
        phone = _decrypt_outbox_phone(row)
        if str(row["event_kind"]) == "verification_code":
            code = decrypt_delivery_target(
                str(row["target_kind"]),
                str(row["payload_ciphertext"]),
                str(row["encryption_key_version"]),
            )
            result = send_verification_code(
                phone=phone,
                caller_supplied_code=code,
                purpose="organization_invite",
                request_id=str(row["request_id"]),
            )
        else:  # invite_link：链接短信需要公开链接前缀 + 专用模板
            base_url = os.getenv("ORGANIZATION_INVITE_LINK_BASE_URL", "").strip().rstrip("/")
            link_template = os.getenv("SMS_TEMPLATE_INVITE_LINK", "").strip()
            if not base_url or not link_template:
                return {"kind": "terminal", "error_code": "INVITE_LINK_CONFIG_MISSING"}
            token = decrypt_delivery_target(
                "invite_link",
                str(row["payload_ciphertext"]),
                str(row["encryption_key_version"]),
            )
            link = f"{base_url}/organization/invite#token={token}"
            result = send_verification_code(
                phone=phone,
                caller_supplied_code=link,
                purpose="organization_invite_link",
                request_id=str(row["request_id"]),
                template_code=link_template,
                template_param=json.dumps({"url": link}, ensure_ascii=False),
            )
    except OrganizationError as exc:
        # 密钥轮换/密文损坏等确定性失败：重试不会成功
        return {"kind": "terminal", "error_code": str(exc.code)}
    except Exception as exc:
        return {"kind": "retry", "error_code": f"DELIVERY_EXCEPTION:{type(exc).__name__}"}
    if result["success"]:
        return {"kind": "sent", "receipt": str(result.get("receipt") or "")}
    if result.get("retryable"):
        return {"kind": "retry", "error_code": str(result.get("provider_code") or "SMS_SEND_FAILED")}
    return {"kind": "terminal", "error_code": str(result.get("provider_code") or "SMS_SEND_FAILED")}


def _finalize_delivery(row: Mapping[str, Any], outcome: Mapping[str, Any]) -> str:
    """按发送分类落终态/退避；claim_token 失配 = 已失去该row所有权，不覆盖。"""
    kind = str(outcome["kind"])
    with get_db() as conn:
        cursor = conn.cursor()
        if kind == "sent":
            receipt = str(outcome.get("receipt") or "")
            cursor.execute(
                """UPDATE organization_invite_delivery_outbox
                   SET status='sent',sent_at=NOW(),attempt_count=attempt_count+1,
                       provider_message_reference=%s,last_error_code=NULL,
                       claim_token=NULL,lease_expires_at=NULL,updated_at=NOW()
                   WHERE id=%s AND claim_token=%s AND status='sending'""",
                (f"aliyun_sms:{receipt}" if receipt else "aliyun_sms", row["id"], row["claim_token"]),
            )
            if cursor.rowcount != 1:
                # 已发出但失去所有权（lease 被回收/邀请被撤销）：交给 unknown 回收策略
                logger.warning("[invite-delivery] sent but ownership lost outbox_id=%s", int(row["id"]))
                return "lost"
            return "sent"
        if kind == "cancelled":
            cursor.execute(
                """UPDATE organization_invite_delivery_outbox
                   SET status='cancelled',last_error_code=%s,
                       claim_token=NULL,lease_expires_at=NULL,updated_at=NOW()
                   WHERE id=%s AND claim_token=%s AND status='sending'""",
                (str(outcome["error_code"]), row["id"], row["claim_token"]),
            )
            return "cancelled"
        attempt = int(row["attempt_count"]) + 1
        if kind == "terminal" or attempt >= DELIVERY_MAX_ATTEMPTS:
            error = str(outcome["error_code"])
            code = (
                f"{DELIVERY_DEAD_LETTER_PREFIX}{error}"
                if kind == "terminal"
                else f"{DELIVERY_DEAD_LETTER_PREFIX}MAX_ATTEMPTS:{error}"
            )
            # dead-letter 终态：CHECK 无 failed 值，cancelled + FAILED: 前缀即终态
            cursor.execute(
                """UPDATE organization_invite_delivery_outbox
                   SET status='cancelled',attempt_count=%s,last_error_code=%s,
                       claim_token=NULL,lease_expires_at=NULL,updated_at=NOW()
                   WHERE id=%s AND claim_token=%s AND status='sending'""",
                (attempt, code, row["id"], row["claim_token"]),
            )
            if (
                error == "CHALLENGE_EXPIRED"
                and cursor.rowcount == 1
                and row.get("challenge_id") is not None
            ):
                # 复用既有 challenge cancel 语义（create_verification_challenge
                # 与 _cancel_invite_onboarding 的 challenge 腿同款）：只关掉这
                # 一个过期 challenge，invite 下其他 event（invite_link）的
                # outbox 行不受影响；员工重新获取验证码会正常签发新 challenge。
                cursor.execute(
                    """UPDATE organization_invite_verification_challenges
                       SET status='cancelled',updated_at=NOW()
                       WHERE id=%s AND status IN ('pending','verified')""",
                    (int(row["challenge_id"]),),
                )
            return "failed"
        delay = DELIVERY_BACKOFF_SECONDS[min(attempt - 1, len(DELIVERY_BACKOFF_SECONDS) - 1)]
        cursor.execute(
            """UPDATE organization_invite_delivery_outbox
               SET status='retry',attempt_count=%s,next_attempt_at=NOW()+make_interval(secs=>%s),
                   last_error_code=%s,claim_token=NULL,lease_expires_at=NULL,updated_at=NOW()
               WHERE id=%s AND claim_token=%s AND status='sending'""",
            (attempt, delay, str(outcome["error_code"]), row["id"], row["claim_token"]),
        )
        return "retry"


def organization_invite_delivery_tick(*, limit: int = 50) -> dict[str, Any]:
    """Claim outbox rows and perform the real provider send for approved providers.

    local_capture marks rows sent deterministically (test-only, non-prod env).
    aliyun_sms claims rows under a lease, sends through the shared SMS
    primitive, then finalizes with receipt / exponential backoff / dead-letter.
    Unapproved providers are a no-op (challenge 创建闸已拒，存量行不再搅动)。
    """
    if _local_capture_enabled():
        return _delivery_tick_local_capture(limit=limit)
    provider = _delivery_provider()
    if provider not in APPROVED_DELIVERY_PROVIDERS:
        return {"processed": [], "deferred": [], "real_messages_sent": 0, "provider": provider}

    processed: list[int] = []
    deferred: list[int] = []
    sent = 0
    claimed = _claim_delivery_rows(limit=limit)
    for row in claimed:
        outcome = _deliver_claimed_row(row)
        final = _finalize_delivery(row, outcome)
        if final == "sent":
            sent += 1
            processed.append(int(row["id"]))
        elif final in {"retry", "lost"}:
            deferred.append(int(row["id"]))
        else:
            processed.append(int(row["id"]))
    return {
        "processed": processed,
        "deferred": deferred,
        "real_messages_sent": sent,
        "provider": provider,
    }


# ========== 送达状态派生（公开轮询 + 老板列表共用） ==========

_DELIVERY_CANCELLED_STATE = {
    "INVITE_REVOKED": "revoked",
    "INVITE_RESENT": "revoked",
    "INVITE_ACCEPTED": "revoked",
    "INVITE_NOT_LIVE": "revoked",
    "INVITE_EXPIRED": "expired",
    "CHALLENGE_SUPERSEDED": "superseded",
}


def _derive_outbox_state(row: Optional[Mapping[str, Any]]) -> tuple[str, Optional[str]]:
    """(state, failure_code)：state ∈ queued/sending/sent/failed/expired/revoked/superseded。"""
    if row is None:
        return "queued", None
    status = str(row["status"])
    if status in ("pending", "retry"):
        return "queued", None
    if status in ("sending", "unknown"):
        return "sending", None
    if status == "sent":
        return "sent", None
    error = str(row.get("last_error_code") or "")
    if error.startswith(DELIVERY_DEAD_LETTER_PREFIX):
        return "failed", error[len(DELIVERY_DEAD_LETTER_PREFIX):]
    return _DELIVERY_CANCELLED_STATE.get(error, "revoked"), (error or None)


# ========== 公开轮询面失败码归类（信息泄漏闸） ==========
#
# 阿里云/平台原始错误码（isv.*/isp.* 等）会泄漏平台短信余额、签名、模板、
# RAM 权限等配置信息，公开轮询面只返下面的归类词汇表；原文保留在
# organization_invite_delivery_outbox.last_error_code 供内部排查。

# 语义安全、可直接透传的内部码（不含平台配置信息，且对员工可行动）
_PUBLIC_SAFE_FAILURE_CODES = frozenset({
    "EMAIL_UNAVAILABLE",
    "INVITE_NOT_LIVE",
    "INVITE_REVOKED",
    "INVITE_RESENT",
    "INVITE_ACCEPTED",
    "INVITE_EXPIRED",
    "CHALLENGE_SUPERSEDED",
})

# 限流类 provider 码 → RATE_LIMITED（员工可"稍后再试"，不泄漏配额主体）
_SMS_RATE_LIMIT_PROVIDER_CODES = frozenset({
    "isv.BUSINESS_LIMIT_CONTROL",
    "isv.DAY_LIMIT_CONTROL",
    "isv.HOUR_LIMIT_CONTROL",
    "isv.MOBILE_COUNT_OVER_LIMIT",
    "Throttling",
    "Throttling.User",
})

# 手机号/参数类 provider 码 → PARAM_ERROR（联系方式本身的问题，可行动）
_SMS_PARAM_PROVIDER_CODES = frozenset({
    "isv.MOBILE_NUMBER_ILLEGAL",
    "isv.INVALID_PARAMETERS",
})


def _public_failure_code(raw: Optional[str]) -> Optional[str]:
    """把内部失败码归约为公开词汇表：RATE_LIMITED / PARAM_ERROR /
    DELIVERY_FAILED，或语义安全的内部码原样透传。

    余额不足（isv.AMOUNT_NOT_ENOUGH）、签名/模板未过审、RAM 拒绝、配置缺失、
    未知异常等一律归到 DELIVERY_FAILED，绝不回传 isv.*/isp.* 原文。
    """
    if not raw:
        return None
    code = str(raw)
    if code.startswith("MAX_ATTEMPTS:"):
        code = code[len("MAX_ATTEMPTS:"):]
    if code in _PUBLIC_SAFE_FAILURE_CODES:
        return code
    if code in _SMS_RATE_LIMIT_PROVIDER_CODES:
        return "RATE_LIMITED"
    if code in _SMS_PARAM_PROVIDER_CODES:
        return "PARAM_ERROR"
    return "DELIVERY_FAILED"


def get_challenge_delivery_status(
    *,
    challenge_id: int,
    token: str,
    request_id: str,
    source_ip: str,
) -> dict[str, Any]:
    """员工接受页轮询：challenge 对应 outbox 行的真实送达状态。"""
    require_feature_flag("ORGANIZATION_SEATS_ENABLED")
    require_feature_flag("ORGANIZATION_INVITE_ONBOARDING_ENABLED")
    # 高频只读轮询走独立放量桶，不与 verify/create 的小桶互相消耗。
    _enforce_public_invite_rate(token=token, action="invite.delivery_status", source_ip=source_ip)
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        invite = _resolve_live_public_invite(cursor, token, for_update=False, allow_accepted=True)
        cursor.execute(
            """SELECT * FROM organization_invite_verification_challenges
               WHERE id=%s AND invite_id=%s""",
            (int(challenge_id), invite["id"]),
        )
        challenge = cursor.fetchone()
        if not challenge:
            raise OrganizationError("ORG_INVITE_CHALLENGE_INVALID", "验证已失效，请重新获取验证码", http_status=404)
        cursor.execute(
            """SELECT * FROM organization_invite_delivery_outbox
               WHERE challenge_id=%s ORDER BY id DESC LIMIT 1""",
            (int(challenge_id),),
        )
        outbox = cursor.fetchone()
        state, failure_code = _derive_outbox_state(outbox)
        if state == "queued" and challenge["expires_at"] <= _utcnow():
            state, failure_code = "expired", None
        return {
            "challenge_id": int(challenge_id),
            "challenge_status": str(challenge["status"]),
            "delivery_state": state,
            # 公开面只返归类码：provider 原文（isv.*/isp.* 等）含平台短信
            # 余额/签名/模板配置信息，绝不回传；原文留在 outbox 供内部排查。
            "failure_code": _public_failure_code(failure_code),
            "sent_at": outbox["sent_at"] if outbox else None,
            "expires_at": challenge["expires_at"],
            "request_id": str(request_id),
        }


def list_invite_delivery_states(
    identity: IdentityContext,
    *,
    invite_ids: list[int],
) -> list[dict[str, Any]]:
    """老板邀请列表的送达状态列数据源（W2 挂载 InviteDeliveryStatus 用）。"""
    from services.organization_service import _lock_identity

    ids = sorted({int(value) for value in invite_ids})[:200]
    if not ids:
        return []
    with get_db() as conn:
        cursor = conn.cursor()
        assert_ready(cursor)
        _lock_identity(cursor, identity, owner_required=True)
        cursor.execute(
            """SELECT DISTINCT ON (d.invite_id,d.event_kind)
                      d.invite_id,d.event_kind,d.status,d.last_error_code,
                      d.sent_at,d.updated_at,d.attempt_count
               FROM organization_invite_delivery_outbox d
               JOIN organization_invites i ON i.id=d.invite_id
               WHERE d.invite_id=ANY(%s) AND i.organization_id=%s
               ORDER BY d.invite_id,d.event_kind,d.id DESC""",
            (ids, identity.organization_id),
        )
        states: dict[int, dict[str, Any]] = {}
        for row in cursor.fetchall():
            state, failure_code = _derive_outbox_state(row)
            entry = states.setdefault(int(row["invite_id"]), {"invite_id": int(row["invite_id"]), "deliveries": {}})
            entry["deliveries"][str(row["event_kind"])] = {
                "state": state,
                "failure_code": failure_code,
                "sent_at": row["sent_at"],
                "updated_at": row["updated_at"],
                "attempt_count": int(row["attempt_count"]),
            }
        return [states[invite_id] for invite_id in ids if invite_id in states]
