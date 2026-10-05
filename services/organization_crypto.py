"""Cryptographic helpers for organization invites and public capabilities.

Plain phone numbers, email addresses, and bearer tokens never enter database
columns or logs.  Key material is injected by environment and key versions are
persisted beside ciphertext/hashes so rotation never guesses a key.
"""

from __future__ import annotations

import base64
from hashlib import sha256
import hmac
import os
import re
import secrets
from typing import Mapping, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.organization_contract import OrganizationError, secure_compare


_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _keyring(env_name: str) -> tuple[str, Mapping[str, bytes]]:
    """Read ``version:base64key`` entries and the independently selected version."""
    raw = os.getenv(env_name, "").strip()
    active = os.getenv(f"{env_name}_ACTIVE_VERSION", "").strip()
    if not raw or not active:
        raise OrganizationError(
            "ORG_CRYPTO_KEY_MISSING",
            "邀请功能暂不可用，请联系平台客服",
            http_status=503,
            safe_details={"keyring": env_name},
        )
    keys: dict[str, bytes] = {}
    for entry in raw.split(","):
        try:
            version, encoded = entry.strip().split(":", 1)
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError):
            raise OrganizationError(
                "ORG_CRYPTO_KEY_INVALID",
                "邀请功能暂不可用，请联系平台客服",
                http_status=503,
                safe_details={"keyring": env_name},
            ) from None
        if not version or len(decoded) < 32:
            raise OrganizationError(
                "ORG_CRYPTO_KEY_INVALID",
                "邀请功能暂不可用，请联系平台客服",
                http_status=503,
                safe_details={"keyring": env_name, "version": version or "missing"},
            )
        keys[version] = decoded
    if active not in keys:
        raise OrganizationError(
            "ORG_CRYPTO_ACTIVE_KEY_MISSING",
            "邀请功能暂不可用，请联系平台客服",
            http_status=503,
            safe_details={"keyring": env_name, "version": active},
        )
    return active, keys


def normalize_target(target_kind: str, value: str) -> str:
    value = str(value or "").strip()
    if target_kind == "phone":
        normalized = re.sub(r"[\s()\-]", "", value)
        if normalized.startswith("00"):
            normalized = "+" + normalized[2:]
        if re.fullmatch(r"1[0-9]{10}", normalized):
            normalized = "+86" + normalized
        if not _PHONE_RE.fullmatch(normalized):
            raise OrganizationError("ORG_INVITE_PHONE_INVALID", "手机号格式无效", http_status=422)
        return normalized
    if target_kind == "email":
        normalized = value.casefold()
        if len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
            raise OrganizationError("ORG_INVITE_EMAIL_INVALID", "邮箱格式无效", http_status=422)
        return normalized
    if target_kind == "username":
        # [WP6] owner 为用户名式邀请设定的登录用户名(即被邀请人的平台登录名)。
        # 3-32 位,小写字母/数字/下划线/点/连字符;归一为小写,防大小写重复冲突。
        normalized = value.casefold()
        if not re.fullmatch(r"[a-z0-9._-]{3,32}", normalized):
            raise OrganizationError(
                "ORG_INVITE_USERNAME_INVALID",
                "登录用户名须为 3-32 位小写字母、数字、下划线、点或连字符", http_status=422,
            )
        return normalized
    raise OrganizationError("ORG_INVITE_TARGET_KIND_INVALID", "邀请方式无效，请让团队负责人重新邀请", http_status=422)


def target_hmac(target_kind: str, normalized_target: str) -> tuple[str, str]:
    version, keys = _keyring("ORGANIZATION_INVITE_HMAC_KEYS")
    digest = hmac.new(
        keys[version],
        f"{target_kind}\x1f{normalized_target}".encode("utf-8"),
        sha256,
    ).hexdigest()
    return digest, version


def target_hmac_candidates(target_kind: str, normalized_target: str) -> list[tuple[str, str]]:
    """Return deterministic target digests for every configured rotation key."""
    _, keys = _keyring("ORGANIZATION_INVITE_HMAC_KEYS")
    return [
        (
            hmac.new(
                keys[version],
                f"{target_kind}\x1f{normalized_target}".encode("utf-8"),
                sha256,
            ).hexdigest(),
            version,
        )
        for version in sorted(keys)
    ]


def rate_subject_hmac(subject_kind: str, value: str) -> tuple[str, str]:
    """Hash a rate-limit dimension without persisting raw network identity."""
    normalized = str(value or "").strip().casefold()
    if not normalized:
        raise OrganizationError(
            "ORG_RATE_SUBJECT_MISSING",
            "网络环境异常，暂时无法完成操作，请稍后再试",
            http_status=503,
        )
    version, keys = _keyring("ORGANIZATION_INVITE_HMAC_KEYS")
    digest = hmac.new(
        keys[version],
        f"rate:{subject_kind}\x1f{normalized}".encode("utf-8"),
        sha256,
    ).hexdigest()
    return digest, version


def target_matches(
    target_kind: str,
    normalized_target: str,
    expected_digest: str,
    key_version: str,
) -> bool:
    _, keys = _keyring("ORGANIZATION_INVITE_HMAC_KEYS")
    key = keys.get(key_version)
    if key is None:
        return False
    actual = hmac.new(
        key,
        f"{target_kind}\x1f{normalized_target}".encode("utf-8"),
        sha256,
    ).hexdigest()
    return secure_compare(actual, expected_digest)


def encrypt_delivery_target(target_kind: str, normalized_target: str) -> tuple[str, str]:
    version, keys = _keyring("ORGANIZATION_INVITE_ENCRYPTION_KEYS")
    key = keys[version][:32]
    nonce = secrets.token_bytes(12)
    aad = f"organization-invite:{target_kind}:{version}".encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, normalized_target.encode("utf-8"), aad)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("="), version


def decrypt_delivery_target(target_kind: str, ciphertext: str, key_version: str) -> str:
    _, keys = _keyring("ORGANIZATION_INVITE_ENCRYPTION_KEYS")
    key = keys.get(key_version)
    if key is None:
        raise OrganizationError("ORG_INVITE_KEY_RETIRED", "邀请链接已失效，请让团队负责人重新发送", http_status=410)
    try:
        raw = base64.urlsafe_b64decode(ciphertext + "=" * (-len(ciphertext) % 4))
        aad = f"organization-invite:{target_kind}:{key_version}".encode("utf-8")
        plain = AESGCM(key[:32]).decrypt(raw[:12], raw[12:], aad).decode("utf-8")
    except Exception:
        raise OrganizationError("ORG_INVITE_CIPHERTEXT_INVALID", "邀请链接无效或不完整，请使用完整链接，或让团队负责人重发", http_status=410) from None
    return plain


def issue_bearer_token(*, purpose: str) -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    version, keys = _keyring("ORGANIZATION_TOKEN_HMAC_KEYS")
    digest = hmac.new(keys[version], f"{purpose}\x1f{token}".encode("utf-8"), sha256).hexdigest()
    return token, digest, version


def derive_bearer_token(
    *,
    purpose: str,
    stable_material: str,
    key_version: Optional[str] = None,
) -> tuple[str, str, str]:
    """Derive a replayable bearer without persisting its plaintext.

    The stable material is a server-scoped issuance identifier, never a user
    secret.  HMAC makes the resulting credential computationally opaque even
    when that identifier is known.  Persisting the key version is sufficient
    to reconstruct the same credential after a committed response is lost.
    """
    active, keys = _keyring("ORGANIZATION_TOKEN_HMAC_KEYS")
    version = key_version or active
    key = keys.get(version)
    if key is None:
        raise OrganizationError("ORG_TOKEN_KEY_RETIRED", "链接已失效，请重新获取", http_status=410)
    material = f"derive\x1f{purpose}\x1f{stable_material}".encode("utf-8")
    raw = hmac.new(key, material, sha256).digest()
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    digest = hmac.new(key, f"{purpose}\x1f{token}".encode("utf-8"), sha256).hexdigest()
    return token, digest, version


def hash_bearer_token(*, purpose: str, token: str, key_version: Optional[str] = None) -> tuple[str, str]:
    active, keys = _keyring("ORGANIZATION_TOKEN_HMAC_KEYS")
    version = key_version or active
    key = keys.get(version)
    if key is None:
        raise OrganizationError("ORG_TOKEN_KEY_RETIRED", "链接已失效，请重新获取", http_status=410)
    return hmac.new(key, f"{purpose}\x1f{token}".encode("utf-8"), sha256).hexdigest(), version
