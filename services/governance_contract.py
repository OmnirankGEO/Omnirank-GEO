"""Unified user-facing alert machine contract (SSOT business-governance-master §13).

Every user-visible alert must carry a stable machine contract AND at least one
legal next-step action, so a user or admin is never trapped in a dead end
(manual §1.6 accident #8 — "红码无下一步"). This is the single reusable builder.

Origin = the GEO marketing factory five-question contract
(``api/marketing_material_api._content_package_rejections``). This generalises it
and adds the 7th field ``impact`` (what was affected), which that contract lacked.
"""
from __future__ import annotations

from typing import Any, List, Optional


# The 7 machine-contract keys (§13). ``impact`` was missing from the original
# marketing five-question contract and is added here.
CONTRACT_KEYS = (
    "code", "message", "reason", "impact", "repair_hint", "actions", "rule_version",
)


class AlertContractError(ValueError):
    """Raised when an alert would be malformed (e.g. a dead-end with no action)."""


def _normalize_actions(actions) -> List[dict]:
    out: List[dict] = []
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").strip()
        label = str(a.get("label") or "").strip()
        if not aid or not label:
            continue
        item = {"id": aid, "label": label}
        if a.get("type"):
            item["type"] = str(a["type"])
        out.append(item)
    return out


def build_alert(
    code: str,
    message: str,
    *,
    reason: str,
    impact: str,
    repair_hint: Optional[str] = None,
    actions: Optional[List[dict]] = None,
    rule_version: Optional[str] = None,
) -> dict:
    """Build one §13-compliant alert payload (7 fields + >=1 legal action).

    ``code``/``message``/``reason``/``impact`` are required — a red code with no
    explanation of why/what-it-affected is accident #8. ``actions`` must carry at
    least one legal next step (``{"id","label"[,"type"]}``); a dead-end alert is
    rejected at construction time so it can never ship.
    """
    if not str(code or "").strip():
        raise AlertContractError("alert code is required")
    if not str(message or "").strip():
        raise AlertContractError("alert message is required")
    if not str(reason or "").strip():
        raise AlertContractError("alert reason is required (why it happened)")
    if not str(impact or "").strip():
        raise AlertContractError("alert impact is required (what it affected)")
    normalized_actions = _normalize_actions(actions)
    if not normalized_actions:
        raise AlertContractError(
            "alert must offer at least one legal next-step action (§13 / accident #8)"
        )
    return {
        "code": str(code).strip(),
        "message": str(message),
        "reason": str(reason),
        "impact": str(impact),
        "repair_hint": (str(repair_hint) if repair_hint else None),
        "actions": normalized_actions,
        "rule_version": (str(rule_version) if rule_version else None),
    }


def is_alert_contract(payload: Any) -> bool:
    """True iff payload is a well-formed §13 alert (all 7 keys + >=1 action).

    Used by tests/consumers to distinguish a governed alert from a bare
    ``{"status":"error","error": str(e)}`` or a plain string (both accident #8).
    """
    if not isinstance(payload, dict):
        return False
    if any(k not in payload for k in CONTRACT_KEYS):
        return False
    return bool(payload.get("actions"))


def build_alert_safe(code: str, message: str, **kwargs) -> dict:
    """[返修 P2-6] 永不抛的合同构造:供 **except 路径** 使用。

    `build_alert` 刻意严格(缺 reason/impact/action 即抛),那是防死胡同的设计闸;
    但在异常处理里调用它,一旦参数不全就会用 AlertContractError **掩盖原始异常**,
    把真实故障吞掉。本函数捕获校验失败并回退到"最小可用合同"(带通用出口),
    保证用户面永远拿得到一个有下一步的告警,同时不遮蔽上游错误。
    """
    try:
        return build_alert(code, message, **kwargs)
    except AlertContractError:
        return {
            "code": str(code or "UNEXPECTED_ERROR").strip() or "UNEXPECTED_ERROR",
            "message": str(message or "这一步没有完成。"),
            "reason": str(kwargs.get("reason") or "出现了未预期的问题。"),
            "impact": str(kwargs.get("impact") or "本次操作没有完成。"),
            "repair_hint": str(kwargs.get("repair_hint") or "可以重试;若持续出现请联系客服。"),
            "actions": [
                {"id": "retry", "label": "重试", "type": "retry"},
                {"id": "contact_support", "label": "联系客服", "type": "contact"},
            ],
            "rule_version": kwargs.get("rule_version"),
        }
