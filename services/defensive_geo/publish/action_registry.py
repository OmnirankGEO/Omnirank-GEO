"""发布面的 **versioned action registry**(规格 §15.7 / POR-16 / UI-36)。

判据:POR-16(「每个 recovery action 都是 versioned 判别联合,具有合法
actionRef/capability/typed target 并解析到同 authority 对象;label-only、
null target、错 domain/reason/action、无 route 的 dead CTA 全部拒绝」)、
UI-36、§19 变异 143。

═══════════════════════════════════════════════════════════════════════
🔴 为什么要一张表,而不是在端点里就地拼 dict
═══════════════════════════════════════════════════════════════════════
就地拼的写法有两个必然缺陷:

  · **漏一格不会红**。端点里 15 处 ``{"kind": ..., "capability": ...}``,
    哪一处把 capability 抄错,只有那一条路径被走到时才会被发现 ——
    而那一条路径往往正是最少被走到的错误分支;
  · **没有分母**。POR-18 要求 registry 的 keys 恰等于 route/action-union census,
    手抄的清单不算分母(§0.5.3 G-2)。

所以这里把 (kind → capability, target kind, 是否需要 target id) 写成**数据**,
:func:`build` 是唯一出口,:func:`census` 给判据当分母。

🔴 本表是**发布面子集**,不是 POR-18 要的全仓 ``domain_action_capability_registry_v1``。
   那张全集属 WP8(§17 退出条件里 POR-18 挂在 WP8)。这里刻意用同构形状,
   便于 WP8 直接合并,而不是到时候再造一遍。
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

#: 改任一行必须升版(旧前端缓存了旧 capability,版本号是唯一能对上的锚)。
ACTION_REGISTRY_VERSION = "defgeo-publish-action-registry-v1"

TargetKind = Literal[
    "publish_command", "publish_snapshot", "publish_slot", "publish_item",
    "plan_item", "article_revision", "quote", "approval", "service_projection",
    "provider_optional_scope", "owner_handoff", "page",
]


class ActionSpec(NamedTuple):
    """一条 action 的合同。四项全填 —— 缺任一项就是一个可能的 dead CTA。"""

    capability: str
    #: 🔴 **一个 kind 可以有多个合法 target kind**,但必须逐个列出。
    #:    §15.7 里 ``contact_owner`` 就有两种:``approval_rejected`` 指
    #:    ``approval``,``service_not_active`` 指 ``service_projection``。
    #:    写成单值会把其中一格判成非法(判据 2026-08-21 当场抓到过这一处),
    #:    写成"任意"又等于没有约束 —— 所以是**闭集元组**。
    target_kinds: tuple[TargetKind, ...]
    #: 该 target 是否必须带非空 id(``page`` 类目标带的是 page 名,不是 id)。
    requires_target_id: bool
    #: 这个动作会不会产生副作用。demo/只读面据此判定能不能点(POR-18 同族)。
    mutating: bool

    @property
    def target_kind(self) -> TargetKind:
        """向后兼容的单值读法 —— 只在恰有一个合法 target kind 时可用。"""
        if len(self.target_kinds) != 1:
            raise ActionRegistryError(
                f"该 action 有 {len(self.target_kinds)} 个合法 target kind "
                f"({list(self.target_kinds)}),不能用单值读法"
            )
        return self.target_kinds[0]


#: 🔴 发布面能下发的**全部** action。多一个少一个都要改这里。
_ACTIONS: dict[str, ActionSpec] = {
    # ── 等待与查看(零副作用)────────────────────────────────────────────
    "wait": ActionSpec("view_publish_status", ("publish_command",), True, False),
    "view_status": ActionSpec("view_publish_status", ("publish_command",), True, False),
    "view_existing_command": ActionSpec("view_publish_status", ("publish_command",), True, False),
    "review_new_snapshot": ActionSpec("view_publish_snapshot", ("publish_snapshot",), True, False),
    "review_adjustment_options": ActionSpec(
        "review_publish_adjustments", ("publish_snapshot",), True, False),
    "review_replacement_policy": ActionSpec(
        "review_publish_replacement", ("publish_command",), True, False),
    "verify_outcome": ActionSpec(
        "review_publication_verification", ("publish_item",), True, False),
    # ── 会动钱 / 会动对象的 ─────────────────────────────────────────────
    "confirm_publish_decision": ActionSpec(
        "confirm_publish_decision", ("publish_snapshot",), True, True),
    "override_publish_decision": ActionSpec(
        "override_publish_media", ("publish_snapshot",), True, True),
    "cancel": ActionSpec("cancel_publish_preview", ("publish_snapshot",), True, True),
    "retry_child": ActionSpec("retry_publish_child", ("publish_command",), True, True),
    "new_preview": ActionSpec("create_publish_preview", ("plan_item",), True, True),
    "repair_legal_passage": ActionSpec("repair_content", ("article_revision",), True, True),
    "new_customer_snapshot": ActionSpec("revise_quote", ("quote",), True, True),
    "reduce_provider_optional_scope": ActionSpec(
        "edit_provider_budget", ("provider_optional_scope",), True, True),
    "request_approval": ActionSpec("request_owner_approval", ("approval",), True, True),
    # ── 转人工(零副作用,但必须有可达对象)────────────────────────────
    "contact_owner": ActionSpec("contact_owner", ("approval", "service_projection"), True, False),
    "handoff_owner": ActionSpec("contact_owner", ("owner_handoff",), True, False),
    "contact_support": ActionSpec("contact_support", ("page",), False, False),
    "top_up": ActionSpec("open_wallet", ("page",), False, False),
    # ── 包E:准备工作没做完 / 通道暂时不可用时的出口 ─────────────────────
    # 🔴 §0.5.6 铁律:任何阻塞必须自带解决方案。这两格过去是**裸 500** ——
    #    500 连"下一步"这个概念都没有,是全规格最纯粹的死路。
    "view_order_progress": ActionSpec(
        "view_service_progress", ("service_projection",), True, False),
    # target = 这一格发布槽本身:她点"稍后再试"要回到的就是这一格,
    # 不是一个编出来的 id(``requires_target_id=True`` 会拒空 id,
    # 但拒不了一个假的 —— 所以 target kind 必须挑一个**真的拿得到**的对象)。
    "retry_later": ActionSpec("retry_publish_preview", ("publish_slot",), True, False),
}

#: ``page`` 类目标的合法取值。开放页名等于给 dead CTA 开后门。
_PAGES: frozenset[str] = frozenset({"wallet", "support"})


class ActionRegistryError(ValueError):
    """action 不合法。**不下发** —— 死 CTA 比没有按钮更糟。"""


def spec(kind: object) -> ActionSpec:
    if kind not in _ACTIONS:
        raise ActionRegistryError(
            f"未注册的 action kind {kind!r};合法 = {sorted(_ACTIONS)}。"
            "未注册的动作一律拒绝下发(POR-18 同族:未注册 action 即拒绝)"
        )
    return _ACTIONS[kind]  # type: ignore[index]


def build(kind: str, *, label: str, target: dict[str, Any]) -> dict[str, Any]:
    """构造一条 typed action。**唯一出口**。

    逐项验:capability 对不对、target kind 对不对、id 有没有、label 空不空。
    任一不合格 → 抛,而不是下发一个点不动的按钮。
    """
    s = spec(kind)
    if not label or not str(label).strip():
        raise ActionRegistryError(f"action {kind} 缺 label —— label-only 的反面同样是死 CTA")
    got_kind = target.get("kind")
    if got_kind not in s.target_kinds:
        raise ActionRegistryError(
            f"action {kind} 的 target.kind 只能是 {list(s.target_kinds)},实得 {got_kind!r}"
        )
    if got_kind == "page":
        if target.get("page") not in _PAGES:
            raise ActionRegistryError(
                f"action {kind} 的 page 必须是 {sorted(_PAGES)} 之一,实得 {target.get('page')!r}"
            )
        ref_part = str(target["page"])
    else:
        target_id = target.get("id")
        if s.requires_target_id and not (target_id and str(target_id).strip()):
            raise ActionRegistryError(
                f"action {kind} 的 target.id 不得为空 —— null target 的 CTA 解析不到对象"
            )
        ref_part = str(target_id)
    return {
        "kind": kind,
        "label": label,
        "actionRef": f"defgeo-publish:{kind}:{ref_part}",
        "capability": s.capability,
        "target": dict(target),
    }


def assert_registered(action: dict[str, Any]) -> None:
    """反向锁:已经构造好的 action 再验一遍(判据用)。"""
    s = spec(action.get("kind"))
    if action.get("capability") != s.capability:
        raise ActionRegistryError(
            f"action {action.get('kind')} 的 capability 应为 {s.capability},"
            f"实得 {action.get('capability')!r} —— 交换两个合法 capability 一律拒绝"
        )
    target = action.get("target") or {}
    if target.get("kind") not in s.target_kinds:
        raise ActionRegistryError(
            f"action {action.get('kind')} 的 target kind 错配:"
            f"只能是 {list(s.target_kinds)},实得 {target.get('kind')!r}"
        )


def mutating_actions() -> frozenset[str]:
    """会产生副作用的 action 集合。demo/只读面判据拿它当分母。"""
    return frozenset(k for k, v in _ACTIONS.items() if v.mutating)


def census() -> dict[str, Any]:
    return {
        "registryVersion": ACTION_REGISTRY_VERSION,
        "actionKinds": sorted(_ACTIONS),
        "pages": sorted(_PAGES),
        "rows": {
            k: {
                "capability": v.capability, "targetKinds": list(v.target_kinds),
                "requiresTargetId": v.requires_target_id, "mutating": v.mutating,
            }
            for k, v in sorted(_ACTIONS.items())
        },
        "mutating": sorted(mutating_actions()),
    }
