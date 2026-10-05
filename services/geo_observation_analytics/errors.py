"""Product error envelope. Codes + user-facing messages are byte-identical to
the frozen ``frontend_race_fixture_v1.json`` ``error_cases`` (asserted by
tests/test_product_api.py). HTTPException detail is a ``{code, message}`` object
so the frontend adapter can branch on a stable code and show the safe message.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

# code -> (http_status, user_facing_message)
ERROR_SPECS: dict[str, tuple[int, str]] = {
    "FORBIDDEN": (403, "你没有查看这项数据的权限。"),
    "VERSION_CONFLICT": (409, "数据已被其他操作更新，请刷新后重试。"),
    "ENV_OVERRIDE_ACTIVE": (423, "当前设置由运行环境统一管理，不能在这里修改。"),
    "OBSERVATION_UNAVAILABLE": (503, "观测数据暂不可用，请稍后重试。"),
    "SEMANTIC_INSIGHT_UNAVAILABLE": (503, "AI 洞察暂不可用，图表和证据仍可正常查看。"),
    "INSUFFICIENT_SAMPLES": (200, "样本不足，暂不下结论。"),
}


def error_body(code: str, message: Optional[str] = None, **extra) -> dict:
    spec = ERROR_SPECS.get(code)
    msg = message if message is not None else (spec[1] if spec else code)
    body = {"code": code, "message": msg}
    body.update(extra)
    return body


def http_error(code: str, message: Optional[str] = None, **extra) -> HTTPException:
    spec = ERROR_SPECS.get(code)
    status = spec[0] if spec else 400
    return HTTPException(status_code=status, detail=error_body(code, message, **extra))
