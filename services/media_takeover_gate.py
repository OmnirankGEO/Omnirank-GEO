"""Fail-closed takeover readiness gate for GEO media flywheel.

The gate is intentionally shadow/admin only.  It evaluates whether an
industry has enough evidence to prepare for a future live handoff, but it does
not connect to placement ranking or change production recommendations.
"""

from __future__ import annotations

from typing import Any


def _num(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _whitelist_has_scope(whitelist: dict[str, Any]) -> bool:
    if not isinstance(whitelist, dict):
        return False
    for key in ("customer_ids", "brand_ids", "agent_ids", "industry_keys"):
        value = whitelist.get(key)
        if isinstance(value, list) and any(str(item).strip() for item in value):
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def evaluate_takeover_gate(
    *,
    industry_key: str,
    health: dict[str, Any] | None,
    approved_bindings: list[dict[str, Any]],
    adoption_summary: dict[str, Any] | None,
    whitelist: dict[str, Any] | None,
    min_answer_adopted: int = 30,
    min_cited: int = 30,
) -> dict[str, Any]:
    """Evaluate takeover readiness without allowing accidental takeover.

    A ready result means "ready to hold a reviewed shadow policy", not "live is
    connected".  The production_takeover flag is still off by default and only
    follows the admin feature switch after all readiness gates pass.
    """
    health = health or {}
    adoption_summary = adoption_summary or {}
    whitelist = whitelist or {}
    approved_bindings = approved_bindings or []

    blockers: list[str] = []

    if health.get("level") != "green" or health.get("can_activate") is False:
        blockers.append("数据健康必须为绿色")

    binding_count = len(approved_bindings)
    if binding_count < 1:
        blockers.append("至少需要 1 个已通过审核的真实媒体资源")

    answer_adopted = _num(adoption_summary.get("answer_adopted_rows"))
    explicit_cited = _num(adoption_summary.get("explicit_cited_rows"))
    if answer_adopted < min_answer_adopted and explicit_cited < min_cited:
        blockers.append("答案采纳或明确引用样本不足")

    if not _whitelist_has_scope(whitelist):
        blockers.append("必须先限定行业、客户或品牌白名单")

    ready = not blockers
    if ready:
        next_action = "等待老板单独授权 live 接线"
        status_label = "具备接管准备条件"
    elif binding_count < 1:
        next_action = "先审核通过媒体绑定候选"
        status_label = "暂不能准备接管"
    elif not _whitelist_has_scope(whitelist):
        next_action = "补充白名单范围后再保存接管准备"
        status_label = "暂不能准备接管"
    else:
        next_action = "补齐数据健康和答案采纳证据"
        status_label = "暂不能准备接管"

    try:
        from writing.feature_switches import is_feature_enabled

        production_takeover = bool(ready and is_feature_enabled("media_takeover"))
    except Exception:
        production_takeover = False

    return {
        "industry_key": industry_key or "general",
        "ready": ready,
        "status_label": status_label,
        "blockers": blockers,
        "next_action": next_action,
        "approved_binding_count": binding_count,
        "whitelist": whitelist,
        "metrics": {
            "answer_adopted_rows": answer_adopted,
            "explicit_cited_rows": explicit_cited,
            "unique_sources": _num(adoption_summary.get("unique_sources")),
            "engine_count": _num(adoption_summary.get("engine_count")),
        },
        "production_takeover": production_takeover,
        "shadow_only": True,
    }
