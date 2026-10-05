"""Shared masking helpers for public-facing contact fields."""

from __future__ import annotations


def mask_phone(phone: str | None) -> str:
    """Mask a phone number as 138****5678."""
    value = phone or ""
    if len(value) < 7:
        return value
    return value[:3] + "****" + value[-4:]


def mask_wechat(wechat: str | None) -> str:
    """Mask a WeChat id while preserving enough public recognition."""
    value = wechat or ""
    if not value:
        return ""
    if len(value) <= 5:
        return value[0] + "***" + value[-1] if len(value) > 1 else "***"
    return value[:3] + "***" + value[-2:]


def mask_email(email: str | None) -> str:
    """Mask an email address as a***@example.com."""
    value = email or ""
    if "@" not in value:
        return value
    local, _, domain = value.partition("@")
    if not local:
        return value
    return f"{local[0]}***@{domain}"
