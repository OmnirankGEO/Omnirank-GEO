from __future__ import annotations

from typing import Any

from jsonschema import Draft7Validator


def validate_tool_args(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    validator = Draft7Validator(schema.get("parameters") or {})
    errors = sorted(validator.iter_errors(args or {}), key=lambda item: list(item.path))
    if not errors:
        return None
    first = errors[0]
    path = ".".join(str(part) for part in first.path)
    prefix = f"{path}: " if path else ""
    return f"{prefix}{first.message}"
