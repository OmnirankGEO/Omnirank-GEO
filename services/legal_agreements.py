"""Canonical legal-document versions, hashes, and purchase evidence helpers."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict


USER_TERMS_VERSION = "user-v2.0"
USER_TERMS_CONTENT_HASH = "0c63376b412f1c62d5eaf7431f1ebd7365e4af2a6f3f1f8e3f2e5f2f1952715d"
PRIVACY_VERSION = "privacy-v1.0"
PRIVACY_CONTENT_HASH = "991a296c311f2ef35f18784d60b7e6041d7fe544aa544b5c6d2a0dedbfcb7768"
PURCHASE_ACCEPTANCE_MAX_AGE_MINUTES = 30
PURCHASE_ACCEPTANCE_SURFACES = frozenset({
    "customer-recharge",
    "subscription-checkout",
    "subscription-autorenew",
})


def registration_agreements() -> tuple[Dict[str, str], ...]:
    return (
        {
            "agreement_type": "user_terms",
            "agreement_version": USER_TERMS_VERSION,
            "content_hash": USER_TERMS_CONTENT_HASH,
        },
        {
            "agreement_type": "privacy",
            "agreement_version": PRIVACY_VERSION,
            "content_hash": PRIVACY_CONTENT_HASH,
        },
    )


def has_current_registration_agreements(cur, *, user_id: int) -> bool:
    """Require both canonical registration documents for passwordless login."""
    cur.execute(
        """SELECT agreement_type,agreement_version,content_hash
           FROM agreement_signatures
           WHERE user_id=%s AND (
             (agreement_type='user_terms' AND agreement_version=%s AND content_hash=%s)
             OR
             (agreement_type='privacy' AND agreement_version=%s AND content_hash=%s)
           )""",
        (
            int(user_id), USER_TERMS_VERSION, USER_TERMS_CONTENT_HASH,
            PRIVACY_VERSION, PRIVACY_CONTENT_HASH,
        ),
    )
    return {str(row["agreement_type"] if isinstance(row, dict) else row[0]) for row in cur.fetchall()} == {
        "user_terms", "privacy",
    }


def record_registration_agreement_acceptance(
    cur, *, user_id: int, ip_address: str, user_agent: str, auth_method: str,
    agreement_session_jti: str,
) -> None:
    """Persist the two current registration documents atomically and idempotently."""
    evidence = json.dumps(
        {
            "surface": "legacy-login-agreement-supplement",
            "explicit_acceptance": True,
            "auth_method": str(auth_method or "")[:20],
            "agreement_session_jti": str(agreement_session_jti or "")[:64],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for document in registration_agreements():
        cur.execute(
            """INSERT INTO agreement_signatures
               (user_id,agreement_type,agreement_version,content_hash,
                ip_address,user_agent,evidence_jsonb)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
               ON CONFLICT (user_id,agreement_type,agreement_version) DO UPDATE SET
                   content_hash=EXCLUDED.content_hash,
                   signed_at=NOW(),
                   ip_address=EXCLUDED.ip_address,
                   user_agent=EXCLUDED.user_agent,
                   evidence_jsonb=EXCLUDED.evidence_jsonb
               WHERE agreement_signatures.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                  OR agreement_signatures.evidence_jsonb->>'agreement_session_jti'
                     IS DISTINCT FROM EXCLUDED.evidence_jsonb->>'agreement_session_jti'""",
            (
                int(user_id),
                document["agreement_type"],
                document["agreement_version"],
                document["content_hash"],
                str(ip_address or "")[:45],
                str(user_agent or "")[:2000],
                evidence,
            ),
        )

    if not has_current_registration_agreements(cur, user_id=int(user_id)):
        raise RuntimeError("协议证据未完整写入，请重新确认")


def record_purchase_acceptance(
    cur, *, user_id: int, ip_address: str, user_agent: str, surface: str,
    evidence: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    normalized_surface = str(surface or "").strip().lower()
    if normalized_surface not in PURCHASE_ACCEPTANCE_SURFACES:
        raise ValueError("协议确认场景无效，请从当前购买页面重新确认")
    acceptance_id = "PAC" + uuid.uuid4().hex.upper()
    audit = {
        "explicit_checkbox": True,
        "document_displayed_before_purchase": True,
        **dict(evidence or {}),
    }
    cur.execute(
        """INSERT INTO purchase_agreement_acceptances
           (acceptance_id,user_id,agreement_type,agreement_version,content_hash,
            ip_address,user_agent,surface,evidence_jsonb)
           VALUES (%s,%s,'user_terms',%s,%s,%s,%s,%s,%s::jsonb)
           RETURNING acceptance_id,agreement_version,content_hash,accepted_at,surface""",
        (
            acceptance_id, int(user_id), USER_TERMS_VERSION, USER_TERMS_CONTENT_HASH,
            str(ip_address or "")[:200], str(user_agent or "")[:2000], normalized_surface,
            json.dumps(audit, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    row = cur.fetchone()
    return dict(row) if isinstance(row, dict) else {
        "acceptance_id": row[0], "agreement_version": row[1], "content_hash": row[2],
        "accepted_at": row[3], "surface": row[4],
    }


def validate_purchase_acceptance(
    cur, *, acceptance_id: str, user_id: int, expected_surface: str,
) -> Dict[str, Any]:
    normalized_surface = str(expected_surface or "").strip().lower()
    if normalized_surface not in PURCHASE_ACCEPTANCE_SURFACES:
        raise ValueError("购买协议校验场景无效")
    cur.execute(
        """SELECT acceptance_id,user_id,agreement_version,content_hash,accepted_at,surface
           FROM purchase_agreement_acceptances
           WHERE acceptance_id=%s AND user_id=%s
             AND agreement_type='user_terms'
             AND accepted_at >= NOW() - (%s * INTERVAL '1 minute')
           FOR SHARE""",
        (str(acceptance_id or ""), int(user_id), PURCHASE_ACCEPTANCE_MAX_AGE_MINUTES),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("购买协议确认不存在、已过期或不属于当前用户")
    item = dict(row) if isinstance(row, dict) else {
        "acceptance_id": row[0], "user_id": row[1], "agreement_version": row[2],
        "content_hash": row[3], "accepted_at": row[4], "surface": row[5],
    }
    if item["agreement_version"] != USER_TERMS_VERSION or item["content_hash"] != USER_TERMS_CONTENT_HASH:
        raise ValueError("购买协议版本已更新，请重新阅读并确认")
    if str(item.get("surface") or "").strip().lower() != normalized_surface:
        raise ValueError("购买协议确认与当前购买场景不一致，请重新阅读并确认")
    return item


def canonical_hash(value: str) -> str:
    return hashlib.sha256(value.replace("\r\n", "\n").encode("utf-8")).hexdigest()
