"""Short-lived, login-only proof for legacy registration agreement acceptance.

The token deliberately is not a JWT. Normal authentication middleware must reject it;
only the dedicated agreement acceptance endpoint understands this format.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict

from auth.jwt_utils import JWT_SECRET
from services.legal_agreements import registration_agreements


AGREEMENT_SESSION_TTL_SECONDS = 10 * 60
_TOKEN_PREFIX = "ags1"
_TOKEN_USE = "registration_agreement_supplement"
_AUTH_METHODS = frozenset({"password", "sms"})


class AgreementSessionError(ValueError):
    """The supplement token is missing, invalid, stale, or expired."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload_b64: str) -> str:
    key = hmac.new(
        JWT_SECRET.encode("utf-8"),
        b"omnirank:registration-agreement-supplement:v1",
        hashlib.sha256,
    ).digest()
    return hmac.new(key, f"{_TOKEN_PREFIX}:{payload_b64}".encode("ascii"), hashlib.sha256).hexdigest()


def public_registration_agreements() -> list[Dict[str, str]]:
    labels = {
        "user_terms": ("用户服务协议", "/terms?embedded=true"),
        "privacy": ("隐私政策", "/privacy?embedded=true"),
    }
    return [
        {
            **document,
            "title": labels[document["agreement_type"]][0],
            "url": labels[document["agreement_type"]][1],
        }
        for document in registration_agreements()
    ]


def create_agreement_session(*, user_id: int, auth_method: str, now: int | None = None) -> Dict[str, Any]:
    normalized_method = str(auth_method or "").strip().lower()
    if normalized_method not in _AUTH_METHODS:
        raise ValueError("unsupported agreement authentication method")
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + AGREEMENT_SESSION_TTL_SECONDS
    documents = public_registration_agreements()
    payload = {
        "token_use": _TOKEN_USE,
        "user_id": int(user_id),
        "auth_method": normalized_method,
        "iat": issued_at,
        "exp": expires_at,
        "jti": secrets.token_hex(16),
        "agreements": [
            {
                "agreement_type": item["agreement_type"],
                "agreement_version": item["agreement_version"],
                "content_hash": item["content_hash"],
            }
            for item in documents
        ],
    }
    payload_b64 = _b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    token = f"{_TOKEN_PREFIX}:{payload_b64}:{_sign(payload_b64)}"
    return {
        "agreement_session_token": token,
        "expires_at": expires_at,
        "agreements": documents,
    }


def decode_agreement_session(token: str, *, now: int | None = None) -> Dict[str, Any]:
    try:
        prefix, payload_b64, signature = str(token or "").split(":", 2)
        if prefix != _TOKEN_PREFIX or not hmac.compare_digest(signature, _sign(payload_b64)):
            raise AgreementSessionError("协议确认凭证无效，请重新验证身份")
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict):
            raise AgreementSessionError("协议确认凭证无效，请重新验证身份")
    except AgreementSessionError:
        raise
    except Exception as exc:
        raise AgreementSessionError("协议确认凭证无效，请重新验证身份") from exc

    current_time = int(time.time() if now is None else now)
    if payload.get("token_use") != _TOKEN_USE:
        raise AgreementSessionError("协议确认凭证用途无效，请重新验证身份")
    if payload.get("auth_method") not in _AUTH_METHODS:
        raise AgreementSessionError("协议确认凭证来源无效，请重新验证身份")
    if not isinstance(payload.get("user_id"), int) or int(payload["user_id"]) <= 0:
        raise AgreementSessionError("协议确认凭证账号无效，请重新验证身份")
    if not isinstance(payload.get("iat"), int) or not isinstance(payload.get("exp"), int):
        raise AgreementSessionError("协议确认凭证时间无效，请重新验证身份")
    if payload["exp"] <= current_time:
        raise AgreementSessionError("协议确认已超时，请重新验证身份")
    if payload["iat"] > current_time + 60 or payload["exp"] - payload["iat"] != AGREEMENT_SESSION_TTL_SECONDS:
        raise AgreementSessionError("协议确认凭证时间无效，请重新验证身份")
    jti = payload.get("jti")
    if not isinstance(jti, str) or len(jti) != 32 or any(ch not in "0123456789abcdef" for ch in jti):
        raise AgreementSessionError("协议确认凭证编号无效，请重新验证身份")

    expected_documents = [
        {
            "agreement_type": item["agreement_type"],
            "agreement_version": item["agreement_version"],
            "content_hash": item["content_hash"],
        }
        for item in registration_agreements()
    ]
    if payload.get("agreements") != expected_documents:
        raise AgreementSessionError("协议版本已更新，请重新验证身份并阅读最新协议")
    return payload
