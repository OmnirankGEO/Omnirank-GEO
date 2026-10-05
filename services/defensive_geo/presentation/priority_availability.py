"""Z-3.1 · PriorityAction 可用性矩阵 —— **四出口**(§15.6 + §0.5.6 Z-3.1)。

═══════════════════════════════════════════════════════════════════
🔴 Z-3.1 改了什么
═══════════════════════════════════════════════════════════════════
规格 §0.5.6 Z-3.1 逐字:

    ``fact_collection`` 解锁:删除 §15.6
    ``Exclude<PriorityActionAvailability, { availability: 'business_available' }>``
    (原 L2588-2590),增加第四出口「**AI 联网补齐**」(复用现役 ``autofill_brand``
    能力,扣算力,结果写入 manifest、确认后生效);**配套 policy 矩阵与相关判据
    按四出口同步重锚**。同一系统不得存在两套「客户资料缺失」处理哲学
    (现役元指令 = 永不中断 + AI 代填)。

改之前 ``fact_collection`` 只有三条路,而且**每一条都要等别人**:
等报价、等人工履约、等归属确认。对一个 40 岁销售来说这三条是同一件事 ——
「今天做不了」。第四出口是唯一**她当场点得下去**的:让 AI 联网查,
查完她确认,生效。

═══════════════════════════════════════════════════════════════════
🔴 为什么第四出口不是 ``business_available``
═══════════════════════════════════════════════════════════════════
``business_available`` 在 §15.6 里绑的是「内嵌 CTA 由 ProviderActionBinding
执行」那条路径,且 §15.6 明写 ``fact_collection`` 不得投 business_available
或伪装任一 mutation/billed capability。

AI 联网补齐**是**扣算力的,但它的落点是「写进 manifest、等她确认」,
不是「替她把这项服务做了」。混进 business_available 会让报告页出现
一个看起来"已经包含在服务里"的按钮,而它其实要花钱 —— 那正是
U-4「涉钱必须先解释后出现」要挡的事。所以它是**自己的一格**。
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, NamedTuple

from services.defensive_geo.presentation import copy_registry

AVAILABILITY_POLICY_VERSION = "provider_action_availability_policy_v1"

Availability = Literal[
    "business_available", "requires_quote", "manual_route",
    # 🔴 Z-3.1 第四出口。
    "ai_autofill_available",
]

#: 全部出口。判据分母从这里取 —— 手写分母漏掉的那一项不会让任何判据变红。
ALL_AVAILABILITIES: tuple[str, ...] = (
    "business_available", "requires_quote", "manual_route", "ai_autofill_available",
)

#: §15.6 三条 manual 三元组,**exact**。key = basis code。
#: 顺序 basis → manualReason.code → handoff routeKey,一个都不许换。
MANUAL_TRIPLES: Mapping[str, tuple[str, str]] = {
    "included_route_unavailable": ("included_manual_fulfillment", "manual_fulfillment"),
    "out_of_scope_quote_unavailable": ("manual_quote_required", "manual_quote"),
    "commercial_basis_unknown": ("scope_confirmation_required", "confirm_commercial_scope"),
    "commercial_basis_conflict": ("scope_confirmation_required", "confirm_commercial_scope"),
}

#: 商业归属。``included`` / ``zero_cost`` 同一侧;``unknown`` / ``conflict`` 走安全解释态。
CommercialScope = Literal["included", "zero_cost", "out_of_scope", "unknown", "conflict"]
ALL_SCOPES: tuple[str, ...] = ("included", "zero_cost", "out_of_scope", "unknown", "conflict")


class AvailabilityBasis(NamedTuple):
    """server-only 的 ``availabilityBasisRef`` 冻结的那份商业权威。

    🔴 这三个布尔**必须由调用方从真实签发状态取**,不许从公开标题或 gap 猜
    (§15.6:「不能从公开标题/gap 临时猜能力」)。
    """

    scope: CommercialScope
    #: 已签发、可执行的报价路径在不在。
    signed_quote_route: bool
    #: 该动作的执行 route/adapter 在不在(enrolled)。
    executable_route: bool
    #: 🔴 Z-3.1:AI 联网补齐这条路在不在(现役 autofill_brand 能力是否可用)。
    ai_autofill_route: bool = False


class AvailabilityVerdict(NamedTuple):
    availability: str
    #: manual 时非空,其余恒 None。
    manual_reason_code: str | None
    handoff_route_key: str | None
    basis_code: str
    user_label: str

    def wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "availability": self.availability,
            "availabilityUserLabel": self.user_label,
            "availabilityPolicyVersion": AVAILABILITY_POLICY_VERSION,
        }
        if self.availability == "manual_route":
            out["manualReason"] = {
                "code": self.manual_reason_code,
                "handoffRouteKey": self.handoff_route_key,
            }
        return out


class AvailabilityInvalid(ValueError):
    """矩阵解不出确定的一格。**投影失败**,不猜一个安全值糊过去。"""


def _manual(basis_code: str) -> AvailabilityVerdict:
    if basis_code not in MANUAL_TRIPLES:
        raise AvailabilityInvalid(f"未知 manual basis {basis_code!r}")
    reason, route = MANUAL_TRIPLES[basis_code]
    return AvailabilityVerdict(
        availability="manual_route",
        manual_reason_code=reason,
        handoff_route_key=route,
        basis_code=basis_code,
        user_label=copy_registry.translate("availability", "manual_route"),
    )


def resolve(*, action_intent: str, basis: AvailabilityBasis) -> AvailabilityVerdict:
    """确定性 scope×route 矩阵。同一入参恒同一出口 —— 没有随机、没有回退默认。

    非 ``fact_collection`` 的三个 intent 走 §15.6 原五格;
    ``fact_collection`` 走 Z-3.1 重锚后的**四出口**。
    """
    if basis.scope not in ALL_SCOPES:
        raise AvailabilityInvalid(f"未知商业归属 {basis.scope!r}")

    if action_intent == "fact_collection":
        # ── Z-3.1 四出口 ───────────────────────────────────────────
        # 🔴 第四出口排在最前:她当场点得下去的那条路优先。
        #    但它**不覆盖** unknown/conflict —— 商业归属都没弄清就先扣钱,
        #    那是把"安全解释态"换成"先收钱再说"。
        if basis.scope in ("unknown", "conflict"):
            return _manual(f"commercial_basis_{basis.scope}")
        if basis.ai_autofill_route:
            return AvailabilityVerdict(
                availability="ai_autofill_available",
                manual_reason_code=None, handoff_route_key=None,
                basis_code=f"{basis.scope}_ai_autofill_route",
                user_label=copy_registry.translate("availability", "ai_autofill_available"),
            )
        if basis.scope == "out_of_scope":
            if basis.signed_quote_route:
                return AvailabilityVerdict(
                    availability="requires_quote", manual_reason_code=None,
                    handoff_route_key=None, basis_code="out_of_scope_signed_quote_route",
                    user_label=copy_registry.translate("availability", "requires_quote"),
                )
            return _manual("out_of_scope_quote_unavailable")
        # included / zero_cost 且没有 AI 路 ⇒ 人工履约。
        # 🔴 这里**不许**落 business_available:§15.6 明写 fact_collection
        #    不得投 business_available。四出口重锚没有改这一条。
        return _manual("included_route_unavailable")

    # ── 其余三个 intent:§15.6 原五格 ──────────────────────────────
    if basis.scope in ("unknown", "conflict"):
        return _manual(f"commercial_basis_{basis.scope}")
    if basis.scope == "out_of_scope":
        if basis.signed_quote_route:
            return AvailabilityVerdict(
                availability="requires_quote", manual_reason_code=None,
                handoff_route_key=None, basis_code="out_of_scope_signed_quote_route",
                user_label=copy_registry.translate("availability", "requires_quote"),
            )
        return _manual("out_of_scope_quote_unavailable")
    if basis.executable_route:
        return AvailabilityVerdict(
            availability="business_available", manual_reason_code=None,
            handoff_route_key=None, basis_code=f"{basis.scope}_enrolled_route",
            user_label=copy_registry.translate("availability", "business_available"),
        )
    return _manual("included_route_unavailable")


def assert_fact_collection_never_business_available(verdict: AvailabilityVerdict) -> None:
    """必须不命中的那一半(§15.6 逐字)。

    Z-3.1 解锁的是**第四出口**,不是 business_available。
    把这条写成可执行断言,是因为「解锁」两个字很容易被读成「全放开」。
    """
    if verdict.availability == "business_available":
        raise AvailabilityInvalid(
            "fact_collection 不得投 business_available(§15.6);"
            "Z-3.1 增加的是第四出口 ai_autofill_available"
        )


def census() -> dict[str, Any]:
    """机械导出。**不写死"几个出口"** —— 数字由笛卡尔积当场跑出来。

    Z-3.1 说的「第四出口」指的是新增的那一种(AI 联网补齐);
    ``fact_collection`` 实际可达格数由 ``factCollectionExits`` 现算,
    我不在这里手写一个数,免得矩阵改了而注释还停在旧数上。
    """
    return {
        "policyVersion": AVAILABILITY_POLICY_VERSION,
        "availabilities": list(ALL_AVAILABILITIES),
        "scopes": list(ALL_SCOPES),
        "manualTriples": {k: list(v) for k, v in sorted(MANUAL_TRIPLES.items())},
        # Z-3.1 的可机读断言。
        #
        # 🔴 「出口」数**不能**用 availability 值去数 —— 三条 manual 共享
        #    同一个 availability 值,那样数出来是 3,而规格说的是 4。
        #    出口的身份 = (availability, manualReason.code) 这一对。
        #    分母从笛卡尔积机械跑出来,不手写。
        # 🔴 去重必须打在 (availability, manualReason.code) **这一对**上。
        #    第一版我拿 verdict 整个 NamedTuple 去重,basis_code 各不相同,
        #    于是"四出口"数出来是 8 —— 分母里混进了同一出口的不同来路。
        "factCollectionExits": sorted({
            f"{v.availability}/{v.manual_reason_code or '-'}"
            for s in ALL_SCOPES for q in (True, False) for a in (True, False)
            for v in (resolve(
                action_intent="fact_collection",
                basis=AvailabilityBasis(
                    scope=s, signed_quote_route=q,
                    executable_route=True, ai_autofill_route=a,
                ),
            ),)
        }),
        "otherIntentExits": sorted({
            f"{v.availability}/{v.manual_reason_code or '-'}"
            for s in ALL_SCOPES for q in (True, False) for r in (True, False)
            for v in (resolve(
                action_intent="content_or_publication_repair",
                basis=AvailabilityBasis(
                    scope=s, signed_quote_route=q,
                    executable_route=r, ai_autofill_route=False,
                ),
            ),)
        }),
    }
