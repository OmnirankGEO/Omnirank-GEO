"""Public identity helpers for advisor market.

The advisor DB keeps both public display identity and internal source identity.
Only admin views should receive source names. Runtime prompts and responses use
public names to avoid presenting the advisor as the original source person.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any, Dict, Optional


PUBLIC_IDENTITY_FLAG = "ADVISOR_PUBLIC_IDENTITY_V2_ENABLED"
PUBLIC_IDENTITY_STATUSES = {"draft", "owner_approved", "legal_approved", "blocked"}
DEFAULT_PUBLIC_NAME = "这位专家"
_SOURCE_ALIAS_SPLIT_RE = re.compile(r"[；;、|／/，,]+")


def _read(record: Any, key: str, default: Any = None) -> Any:
    if record is None:
        return default
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"\s+", " ", text)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def is_public_identity_enabled(default: bool = False) -> bool:
    """Read the rollout flag with DB-first, env fallback behavior."""
    env_default = _truthy(os.getenv(PUBLIC_IDENTITY_FLAG, str(default)))
    try:
        from db.social_preferences_db import get_admin_setting

        return bool(get_admin_setting(PUBLIC_IDENTITY_FLAG, bool, env_default))
    except Exception:
        return env_default


def public_name_from(record: Any, default: str = DEFAULT_PUBLIC_NAME) -> str:
    """Return the public display name from a row/object/string."""
    if isinstance(record, str):
        return _clean_text(record) or default
    return (
        _clean_text(_read(record, "public_name"))
        or _clean_text(_read(record, "name"))
        or default
    )


def source_name_from(record: Any, default: str = "") -> str:
    """Return the internal source name, falling back to the row name."""
    if isinstance(record, str):
        return _clean_text(record) or default
    return (
        _clean_text(_read(record, "source_name"))
        or _clean_text(_read(record, "legacy_name"))
        or _clean_text(_read(record, "original_name"))
        or _clean_text(_read(record, "name"))
        or default
    )


def source_names_from(record: Any, extra: Optional[Iterable[Any]] = None) -> list[str]:
    """Collect possible source aliases for sanitization."""
    names: list[str] = []

    def add_aliases(value: Any) -> None:
        clean = _clean_text(value)
        if not clean:
            return
        names.append(clean)
        for part in _SOURCE_ALIAS_SPLIT_RE.split(clean):
            alias = _clean_text(part)
            if alias and alias != clean:
                names.append(alias)

    for key in ("source_name", "legacy_name", "original_name"):
        add_aliases(_read(record, key))
    raw_list = _read(record, "source_names")
    if isinstance(raw_list, str):
        add_aliases(raw_list)
    elif isinstance(raw_list, Iterable):
        for item in raw_list:
            add_aliases(item)
    if extra:
        for item in extra:
            add_aliases(item)
    public = public_name_from(record, default="")
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        clean = _clean_text(name)
        if not clean or clean == public or clean in seen:
            continue
        deduped.append(clean)
        seen.add(clean)
    return deduped


def project_advisor_row(
    row: Mapping[str, Any],
    *,
    is_admin: bool = False,
    public_identity_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Project an advisor DB row for API responses.

    B strategy: DB `name` is the public display name after migration, while
    `source_name` keeps the internal source/person label. The flag can still
    expose legacy behavior by using `source_name` as `name` when disabled.
    """
    enabled = is_public_identity_enabled() if public_identity_enabled is None else bool(public_identity_enabled)
    projected = dict(row)
    public_name = public_name_from(projected)
    source_name = source_name_from(projected, default="")
    display_name = public_name if (enabled or is_admin) else (source_name or public_name)

    projected["name"] = display_name
    projected["public_name"] = public_name
    projected["identity_status"] = projected.get("identity_status") or "draft"

    if not is_admin:
        for key in ("source_name", "source_names", "legacy_name", "original_name", "identity_notes"):
            projected.pop(key, None)
        # [self-review r4 2026-05-23] P0-11 闭环:advisor_api shadow router 也要剥敏感字段
        # 老板独立 worktree 复审:server.py 加了校验但 api/advisor_api.py:260 实际先命中
        # 漏修字段:base_prompt(模型 prompt 暴露)/ api_provider / model_name(供应商泄漏)
        # / knowledge_base_path(本地路径泄漏)
        for key in ("base_prompt", "api_provider", "model_name", "knowledge_base_path"):
            projected.pop(key, None)

    return projected


def safe_identity_intro(public_name: str) -> str:
    name = _clean_text(public_name) or DEFAULT_PUBLIC_NAME
    return f"我是{name}，平台里的 AI 专家顾问。"


def build_advisor_identity_boundary(public_name_or_record: Any, source_names: Optional[Iterable[str]] = None) -> str:
    """Prompt boundary that avoids source-person claims."""
    public_name = public_name_from(public_name_or_record)
    return (
        f"身份边界：你是平台里的 AI 专家顾问，对外名称是「{public_name}」。"
        "你可以参考平台授权整理的资料、知识库和方法卡提供建议，但不能声称自己是资料来源本人，"
        "不能暗示自己拥有真人的线下经历、客户案例或版权身份。"
        f"当用户问“你是谁/你是做什么的”时，第一句必须透明说明：{safe_identity_intro(public_name)}"
        "不要提及内部来源名，也不要使用旧版来源人物介绍句式。"
    )


_NOT_AI_PHRASES = (
    "我不是AI",
    "我不是 AI",
    "我不是人工智能",
    "我不是一个AI",
    "我不是一个 AI",
)


def sanitize_advisor_identity_claims(
    text: str,
    *,
    public_name: Optional[str] = None,
    source_names: Optional[Iterable[str]] = None,
) -> str:
    """Rewrite explicit source-person claims in model output."""
    if not text:
        return text

    clean_public = _clean_text(public_name) or DEFAULT_PUBLIC_NAME
    replacement = safe_identity_intro(clean_public).rstrip("。")
    sanitized = str(text)

    legacy_intro_patterns = [
        r"我是基于[「\"]?[^」\"\n]{1,80}[」\"]?资料和方法论的\s*AI\s*专家顾问",
        r"我是基于[「\"]?[^」\"\n]{1,80}[」\"]?的资料和方法论生成的\s*AI\s*专家顾问",
        r"我是参考[「\"]?[^」\"\n]{1,80}[」\"]?资料和方法论的\s*AI\s*专家顾问",
    ]
    for pattern in legacy_intro_patterns:
        sanitized = re.sub(pattern, replacement, sanitized)

    for source_name in source_names or []:
        clean_source = _clean_text(source_name)
        if clean_source and clean_source != clean_public:
            sanitized = sanitized.replace(clean_source, clean_public)

    for phrase in _NOT_AI_PHRASES:
        sanitized = sanitized.replace(phrase, replacement)

    # Avoid source-person / offline-practice phrasing that makes the AI sound
    # like the original expert or a real service team with personal cases.
    real_experience_rewrites = [
        (
            r"全是我每天帮[^，。]{1,80}处理的核心业务",
            "这些都是我可以协助梳理的核心业务",
        ),
        (
            r"我每天帮([^，。]{1,80})(处理|解决|做|梳理)",
            r"我可以协助\1\2",
        ),
        (
            r"我的客户案例",
            "相关案例",
        ),
        (
            r"我过去处理过的",
            "常见的",
        ),
        (
            r"我们团队",
            "平台资料",
        ),
    ]
    for pattern, repl in real_experience_rewrites:
        sanitized = re.sub(pattern, repl, sanitized)

    for phrase in (f"我是{clean_public}", f"我是 {clean_public}"):
        if sanitized.lstrip().startswith(phrase):
            leading_gap = len(sanitized) - len(sanitized.lstrip())
            stripped = sanitized.lstrip()
            sanitized = sanitized[:leading_gap] + replacement + stripped[len(phrase):]
            break

    advisor_phrase = r"平台里的\s*AI\s*专家顾问"
    sanitized = re.sub(
        rf"(我是{re.escape(clean_public)}，{advisor_phrase})(?:[，,、。\s]*(?:{advisor_phrase}))+",
        r"\1",
        sanitized,
    )
    duplicate = re.escape(replacement)
    sanitized = re.sub(rf"({duplicate})([，,。]\s*)+\1", replacement, sanitized)
    return sanitized


def sanitize_advisor_message_content(content: str, advisor_row: Any, *, public_identity_enabled: bool = True) -> str:
    """Sanitize historical advisor messages for display/runtime context."""
    if not public_identity_enabled:
        return content
    return sanitize_advisor_identity_claims(
        content,
        public_name=public_name_from(advisor_row),
        source_names=source_names_from(advisor_row),
    )
