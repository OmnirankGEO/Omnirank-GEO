"""小榜统一系统导航 + 客户运营建议装配器。

模型只负责表达；operation_id、route、target、权限、状态和数字均由服务端确定。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Mapping

from fastapi import HTTPException

from services import gap_operation_map as omap
from services.customer_operation_plan import (
    AuthorizedAssistantContext,
    PLAN_VERSION,
    build_customer_operation_plan,
    load_customer_operation_plan,
    resolve_authorized_context,
)
from services.gap_operation_labels import assert_no_internal_leak, translate_action

logger = logging.getLogger("GEO-GapAssistant")

ASSISTANT_CONTRACT_VERSION = "xiaobang-unified-assistant-v2"
_OPERATIONS_QUESTIONS = (
    "今天", "先做什么", "下一步", "为什么这样安排", "为什么安排",
    "运营建议", "这个客户", "当前客户", "待办", "优先做",
)

# 现行 P4 的动作合同仍是统一内核的一部分。写类动作绝不进入这个集合；
# OperationRegistry 只给其中登记过的导航动作补签 route/target。
_NAVIGATION_ACTIONS = frozenset({
    "go_to_signed_route", "highlight_signed_target", "explain_plan",
    "open_media_library", "open_assistant_advice", "show_adjacent_queries",
    "view_checkback_schedule", "keep_suggestion", "back_to_plan",
    "open_delivery_plan",
})
_PLAN_LANDING_ACTION = "open_delivery_plan"
_UNSET = object()


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class AssistantAnswer:
    __slots__ = (
        "headline", "reasons", "actions", "breadcrumb", "snapshot_id",
        "snapshot_version", "authority_generation", "operation_map_version",
        "degraded", "degrade_reason", "context", "plan_id", "evidence",
    )

    def __init__(
        self,
        *,
        headline: str,
        reasons: list[str],
        actions: list[dict[str, Any]],
        breadcrumb: list[str] | None = None,
        snapshot_id: str | None = None,
        snapshot_version: str | None = None,
        authority_generation: int | None = None,
        degraded: bool = False,
        degrade_reason: str = "",
        context: Mapping[str, Any] | None = None,
        plan_id: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ):
        self.headline = str(headline or "").strip()
        self.reasons = [str(r) for r in reasons if str(r or "").strip()][:3]
        self.actions = [a for a in actions if isinstance(a, dict)][:2]
        self.breadcrumb = breadcrumb or []
        self.snapshot_id = snapshot_id
        self.snapshot_version = snapshot_version
        self.authority_generation = authority_generation
        self.operation_map_version = omap.OPERATION_MAP_VERSION
        self.degraded = degraded
        self.degrade_reason = degrade_reason
        self.context = dict(context or {})
        self.plan_id = plan_id
        self.evidence = list(evidence or [])

    def as_meta(self) -> dict[str, Any]:
        out = {
            "gap_assistant": {
                "headline": self.headline,
                "reasons": self.reasons,
                "actions": self.actions,
                "breadcrumb": self.breadcrumb,
                "plan_snapshot_id": self.snapshot_id,
                "snapshot_version": self.snapshot_version,
                "authority_generation": self.authority_generation,
                "operation_map_version": self.operation_map_version,
                # [vNext 2026-08-18] 版本协商靠数据,不靠前端写死一个常量。
                # 债务 §4.1-7:前端硬编码 operation-registry-v2,后端一升版
                # 动作会被**整体过滤掉** —— 用户看到的是"小榜答了但没按钮"。
                "operation_map_compatible_versions": list(
                    omap.OPERATION_REGISTRY_COMPATIBLE_VERSIONS
                ),
                "degraded": self.degraded,
                "context": self.context,
                "plan_id": self.plan_id,
                "evidence": self.evidence,
            },
            "assistant_context": self.context,
        }
        assert_no_internal_leak(out, where="gap-assistant")
        return out

    def plain_text(self) -> str:
        lines = [self.headline]
        if self.reasons:
            lines.extend(f"- {reason}" for reason in self.reasons)
        return "\n".join(line for line in lines if line)

    def output_hash(self) -> str:
        return _hash([
            self.headline, self.reasons,
            [(a.get("action_id"), a.get("operation_id")) for a in self.actions],
        ])


def _operation_action(
    operation_id: str,
    *,
    user: Mapping[str, Any] | None,
    identity: Any,
    route_context: Mapping[str, Any] | None,
    primary: bool,
) -> dict[str, Any] | None:
    entry = omap.resolve_operation(operation_id)
    if entry is None or (
        user is not None
        and not omap.is_operation_allowed(entry, dict(user), identity=identity)
    ):
        return None
    described = omap.describe(entry, dict(route_context or {}))
    route = described.get("target_route")
    if not route:
        return None
    base = translate_action("go_to_signed_route")
    return {
        **base,
        # 保留 P4 已上线的稳定动作类型；精确入口由 operation_id 区分。
        "action_id": "go_to_signed_route",
        "operation_id": entry.operation_id,
        "label": ("去" if primary else "备选：去") + entry.display_name,
        "action_type": entry.action_type,
        "target_route": route,
        "help_target": entry.help_target,
        "registry_version": omap.OPERATION_REGISTRY_VERSION,
        "primary": primary,
        "enabled": True,
        "confirmation": False,
    }


def _fallback_action(
    *, user: Mapping[str, Any] | None, identity: Any, route_context: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    return _operation_action(
        "help_center", user=user, identity=identity, route_context=route_context, primary=True
    )


def answer_navigation(
    question: str,
    *,
    user: Mapping[str, Any] | None = None,
    identity: Any = None,
    route_context: Mapping[str, Any] | None = None,
) -> AssistantAnswer | None:
    normalized = str(question or "").replace(" ", "")
    entry = None
    if any(token in normalized for token in ("这个页面怎么用", "当前页面怎么用", "这页怎么用")):
        from urllib.parse import urlsplit

        current_path = urlsplit(str((route_context or {}).get("_current_page") or "")).path
        if current_path == "/dashboard":
            entry = omap.resolve_operation("today_workspace")
        else:
            entry = next(
                (
                    candidate for candidate in omap.all_operations()
                    if urlsplit(candidate.route_template).path == current_path
                    and candidate.help_target != "gap-plan-section"
                ),
                None,
            )
    if entry is None:
        entry = omap.match_operation(question)
    if entry is None:
        return None
    if user is not None and not omap.is_operation_allowed(entry, dict(user), identity=identity):
        fallback = _fallback_action(user=user, identity=identity, route_context=route_context)
        return AssistantAnswer(
            headline=f"当前账号没有「{entry.display_name}」入口。",
            reasons=["这个按钮只会在有对应角色和模块权限时出现。"],
            actions=[fallback] if fallback else [],
            breadcrumb=list(entry.breadcrumb),
        )
    action = _operation_action(
        entry.operation_id,
        user=user,
        identity=identity,
        route_context=route_context,
        primary=True,
    )
    if action is None:
        return AssistantAnswer(
            headline=f"「{entry.display_name}」需要先选择对应客户或任务。",
            reasons=["当前上下文不足，系统不会猜一个客户。"],
            actions=[a for a in [
                _operation_action(
                    "client_list", user=user, identity=identity,
                    route_context=route_context, primary=True,
                )
            ] if a],
            breadcrumb=list(entry.breadcrumb),
        )
    return AssistantAnswer(
        headline=f"在「{' → '.join(entry.breadcrumb)}」。",
        reasons=[],
        actions=[action],
        breadcrumb=list(entry.breadcrumb),
    )


def _with_routes(
    actions: list[dict[str, Any]],
    *,
    user: Mapping[str, Any] | None = None,
    identity: Any = None,
    route_context: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """P4 薄适配：唯一注册表补签落点，并在签发前执行同一权限判断。"""
    out: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        landing = omap.route_for_action(
            str(action.get("action_id") or ""), dict(route_context or {})
        )
        if landing:
            entry = omap.resolve_operation(str(landing.get("operation_id") or ""))
            if entry is None or (
                user is not None
                and not omap.is_operation_allowed(entry, dict(user), identity=identity)
            ):
                continue
        elif action.get("target_route"):
            # P4/模型都不能把未登记 URL 伪装成可点击动作。
            continue
        if action.get("target_route"):
            # 按问题匹配出的精确落点优先；只补缺失的注册表元数据，绝不覆盖。
            merged = {**(landing or {}), **action}
            out.append(merged)
            continue
        out.append({**action, **landing} if landing else action)
    return out


def _answer_delivery_plan(
    presented_snapshot: Mapping[str, Any],
    *,
    user: Mapping[str, Any] | None = None,
    identity: Any = None,
    route_context: Mapping[str, Any] | None = None,
) -> AssistantAnswer:
    """融合现行 P4：页面卡动作优先，统一交付计划入口作备用。"""
    summary = dict(presented_snapshot.get("summary") or {})
    capacity = dict(presented_snapshot.get("capacity") or {})
    items = list(presented_snapshot.get("items") or [])
    focus = next(
        (item for item in items if (item.get("status") or {}).get("code") == "ready_to_execute"),
        items[0] if items else None,
    )
    reasons: list[str] = []
    if summary.get("headline"):
        reasons.append(str(summary["headline"]))
    if capacity.get("explanation"):
        reasons.append(str(capacity["explanation"]))
    if focus and (focus.get("rationale") or {}).get("how"):
        reasons.append(str(focus["rationale"]["how"]))

    if focus is None:
        headline = "眼下没有值得写的新缺口，额度保留。"
        actions = [translate_action("view_checkback_schedule")]
    else:
        headline = str(summary.get("next_step") or focus.get("title") or "先核对当前交付任务。")
        card_actions = [
            action for action in (focus.get("actions") or [])
            if action.get("action_id") in _NAVIGATION_ACTIONS and action.get("enabled")
        ]
        actions = card_actions[:1] or [translate_action("explain_plan")]

    if not any(action.get("action_id") == _PLAN_LANDING_ACTION for action in actions):
        actions = actions + [translate_action(_PLAN_LANDING_ACTION)]
    signed = _with_routes(
        actions, user=user, identity=identity, route_context=route_context,
    )
    if not any(action.get("target_route") for action in signed):
        fallback = (
            _operation_action(
                "client_list", user=user, identity=identity,
                route_context=route_context, primary=True,
            )
            or _fallback_action(
                user=user, identity=identity, route_context=route_context,
            )
        )
        if fallback:
            signed.append(fallback)
    for index, action in enumerate(signed):
        action.setdefault("primary", index == 0)
    return AssistantAnswer(
        headline=headline,
        reasons=reasons,
        actions=signed,
        snapshot_id=presented_snapshot.get("snapshot_id"),
        snapshot_version=presented_snapshot.get("snapshot_version"),
        authority_generation=presented_snapshot.get("authority_generation"),
    )


def answer_operations(
    operation_plan: Mapping[str, Any],
    *,
    user: Mapping[str, Any] | None = None,
    identity: Any = None,
    route_context: Mapping[str, Any] | None = None,
) -> AssistantAnswer:
    # 兼容直接传入现役 P4 presented snapshot 的调用方与判别锁。
    if "items" in operation_plan and "context" not in operation_plan:
        return _answer_delivery_plan(
            operation_plan, user=user, identity=identity, route_context=route_context,
        )

    context = dict(operation_plan.get("context") or {})
    delivery = dict(operation_plan.get("delivery_plan") or {})
    primary_rec = dict(operation_plan.get("primary_action") or {})
    backup_rec = dict(operation_plan.get("backup_action") or {})
    if delivery.get("items") is not None:
        p4_answer = _answer_delivery_plan(
            delivery, user=user, identity=identity, route_context=route_context,
        )
        actions = p4_answer.actions
    else:
        actions = []
        primary = _operation_action(
            str(primary_rec.get("operation_id") or ""),
            user=user, identity=identity, route_context=route_context, primary=True,
        )
        if primary:
            actions.append(primary)
        backup = _operation_action(
            str(backup_rec.get("operation_id") or ""),
            user=user, identity=identity, route_context=route_context, primary=False,
        )
        if backup and backup.get("operation_id") != (primary or {}).get("operation_id"):
            actions.append(backup)

    if not context.get("has_customer"):
        headline = "先选择一个客户，我才能按真实数据安排今天的工作。"
    else:
        brand_name = str(context.get("brand_name") or "当前客户")
        headline = f"{brand_name} 今天先做：{str(primary_rec.get('reason') or '查看当前运营状态')}"
    reasons = [
        str(primary_rec.get("reason") or ""),
        str(backup_rec.get("reason") or ""),
    ]
    metrics = operation_plan.get("metrics") or {}
    writing = metrics.get("writing") or {}
    publication = metrics.get("publication") or {}
    if (
        writing.get("available", True) and publication.get("available", True)
        and writing.get("pending") is not None and writing.get("completed") is not None
        and publication.get("published") is not None
    ):
        reasons.append(
            f"当前：待写 {int(writing['pending'])}，已完成 {int(writing['completed'])}，"
            f"已发布 {int(publication['published'])}。"
        )
    elif operation_plan.get("warnings"):
        reasons.append("部分运营数据暂时无法读取，不能按 0 解读；请进入客户页重试。")
    return AssistantAnswer(
        headline=headline,
        reasons=reasons,
        actions=actions,
        snapshot_id=delivery.get("snapshot_id"),
        snapshot_version=delivery.get("snapshot_version") or PLAN_VERSION,
        authority_generation=delivery.get("authority_generation"),
        context=context,
        plan_id=operation_plan.get("plan_id"),
        evidence=list(operation_plan.get("evidence") or []),
        degraded=bool(operation_plan.get("warnings")),
        degrade_reason=";".join(
            str(w.get("message") or "") for w in (operation_plan.get("warnings") or [])
        ),
    )


def degraded_answer(
    reason: str,
    *,
    context: Mapping[str, Any] | None = None,
    user: Mapping[str, Any] | None = None,
    identity: Any = None,
) -> AssistantAnswer:
    retry = _operation_action(
        "client_list", user=user, identity=identity, route_context=context, primary=True
    )
    if retry:
        retry["label"] = "重新进入客户后重试"
    help_action = _fallback_action(user=user, identity=identity, route_context=context)
    return AssistantAnswer(
        headline="运营数据暂时无法读取，现有页面和任务不受影响。",
        reasons=["请重新进入当前客户后重试；系统不会把读取失败当成经营数据为 0。"],
        actions=[action for action in (retry, help_action) if action],
        degraded=True,
        degrade_reason=reason,
        context=context,
    )


def build_answer(
    *,
    question: str,
    context_refs: dict[str, Any] | None,
    request,
    assistant_request_id: str,
    actor_user_id: int | None,
    current_page: str = "",
    identity: Any = None,
    resolved_context: AuthorizedAssistantContext | None = None,
    operation_plan: Mapping[str, Any] | None | object = _UNSET,
    ensure_fallback: bool = False,
) -> AssistantAnswer | None:
    """所有聊天分支都先调用本入口；即使无模型也总能得到结构化动作。"""
    user = getattr(request.state, "user", None) or {}
    try:
        if resolved_context is None and operation_plan is _UNSET:
            resolved_context, operation_plan = load_customer_operation_plan(
                request, context_refs, current_page
            )
        elif resolved_context is None:
            resolved_context = resolve_authorized_context(request, context_refs, current_page)
        elif operation_plan is _UNSET:
            operation_plan = build_customer_operation_plan(resolved_context)
    except HTTPException as exc:
        # API 层会在 SSE 建立前先 resolve，因此仍返回真实 404；直接调用统一内核的
        # 旧 P4 路径则得到不含客户名/ID的降级答案，保持既有 fail-closed 合同。
        answer = degraded_answer(
            f"authorization:{exc.status_code}", user=user, identity=identity
        )
        _record(
            answer, refs={}, assistant_request_id=assistant_request_id,
            actor_user_id=actor_user_id, question=question,
        )
        return answer
    except Exception as exc:  # optional data source failure: deterministic navigation remains
        logger.warning("小榜运营计划取不到，保留确定性导航: %s", exc)
        resolved_context = resolved_context
        operation_plan = None

    route_context = resolved_context.refs() if resolved_context else dict(context_refs or {})
    if resolved_context is not None:
        route_context["_current_page"] = resolved_context.current_page
    expected_generation = (context_refs or {}).get("authority_generation")
    delivery = dict(operation_plan.get("delivery_plan") or {}) if isinstance(operation_plan, Mapping) else {}
    if (
        expected_generation is not None and delivery.get("authority_generation") is not None
        and int(expected_generation) != int(delivery["authority_generation"])
    ):
        answer = degraded_answer(
            "generation_mismatch", context=route_context, user=user, identity=identity
        )
        _record(
            answer, refs=resolved_context.refs() if resolved_context else dict(context_refs or {}),
            assistant_request_id=assistant_request_id, actor_user_id=actor_user_id,
            question=question,
        )
        return answer

    nav = answer_navigation(
        question, user=user, identity=identity, route_context=route_context
    )
    wants_operations = any(token in str(question or "") for token in _OPERATIONS_QUESTIONS)
    ops = None
    if isinstance(operation_plan, Mapping) and wants_operations:
        ops = answer_operations(
            operation_plan, user=user, identity=identity, route_context=route_context
        )
    elif operation_plan is None and wants_operations:
        ops = degraded_answer(
            "operation_plan_unavailable", context=route_context, user=user, identity=identity
        )

    if nav is not None and ops is not None:
        answer = AssistantAnswer(
            headline=ops.headline,
            reasons=ops.reasons,
            actions=(nav.actions[:1] + ops.actions)[:2],
            breadcrumb=nav.breadcrumb,
            snapshot_id=ops.snapshot_id,
            snapshot_version=ops.snapshot_version,
            authority_generation=ops.authority_generation,
            degraded=ops.degraded,
            degrade_reason=ops.degrade_reason,
            context=ops.context,
            plan_id=ops.plan_id,
            evidence=ops.evidence,
        )
    else:
        answer = nav or ops

    if answer is None and ensure_fallback:
        context_public = resolved_context.public_context() if resolved_context else {}
        action = _fallback_action(user=user, identity=identity, route_context=route_context)
        answer = AssistantAnswer(
            headline="我会按当前页面和权限帮你找现役入口。",
            reasons=["如果要运营建议，请先选择客户后问“今天先做什么”。"],
            actions=[action] if action else [],
            context=context_public,
        )
    elif answer is None:
        return None
    elif not answer.context and resolved_context is not None:
        answer.context = resolved_context.public_context()

    _record(
        answer,
        refs=resolved_context.refs() if resolved_context else dict(context_refs or {}),
        assistant_request_id=assistant_request_id,
        actor_user_id=actor_user_id,
        question=question,
    )
    return answer


def _record(
    answer: AssistantAnswer,
    *,
    refs: dict[str, Any],
    assistant_request_id: str,
    actor_user_id: int | None,
    question: str,
) -> None:
    """审计失败不阻断聊天；只记哈希和签发动作，不记正文、客户名或模型提示。"""
    try:
        from db import gap_plan_db

        gap_plan_db.record_assistant_audit(
            assistant_request_id=assistant_request_id,
            snapshot_id=answer.snapshot_id,
            snapshot_version=answer.snapshot_version or ASSISTANT_CONTRACT_VERSION,
            authority_generation=answer.authority_generation,
            quote_id=refs.get("quote_id"),
            brand_id=refs.get("brand_id"),
            actor_user_id=actor_user_id,
            facts_hash=_hash({"q": question, "refs": refs}),
            action_ids=[a.get("action_id") for a in answer.actions],
            output_hash=answer.output_hash(),
            operation_map_version=answer.operation_map_version,
            degraded=answer.degraded,
            degrade_reason=answer.degrade_reason or None,
        )
    except Exception as exc:  # observation is O1, never a business hard block
        logger.warning("小榜建议审计暂时不可用: %s", exc)
