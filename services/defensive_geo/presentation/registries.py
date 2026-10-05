"""WP3 版本化 registry —— 等级政策 / 五卡 / 呈现状态规则 + **Owner 签发闸**。

规格 §8.2(五卡)/§15.6(LevelMeta、SectionStateProjection)/
§0.5.2 R-2(纯工程 registry 由 Review-CTO 签;Owner 只签商业口径)。
判据 = MET-29 / MET-36 / MET-41 / MET-44 / MET-45。

🔴 签发闸(MET-36 / MET-45 逐字)
--------------------------------
``defensive_presentation_level_policy_v1`` / ``presentation_state_rule_v1`` /
``professional_analysis_section_registry_v1`` 三项受签发闸约束。
未签时:**只允许 fixture/shadow,不得 enrollment 对客 v2 Presentation**。

本模块把"签没签"做成**数据 + 运行时闸**,而不是注释里的一句提醒 ——
注释拦不住任何人。:func:`require_signed_for_customer` 在未签时抛,
调用方想对客下发就必须先签。

🔴 [包F ⑧ · 2026-08-24] 三项**已签**,闸随之放行
------------------------------------------------
签发人按 §0.5.2 R-2 分工(商业口径 Owner / 纯工程 registry Review-CTO),
逐项留痕见 :data:`_SIGNATURES`。放行的直接后果:
``customerPresentationSigned`` 变 true ⇒ 前端撤"测试数据·不可发客户"水印、
五卡终态呈现解锁。判据**成对**:签发前水印在 / 签发后终态在
(见 ``tests/defensive_geo_pkgf_2026_08_23/test_presentation_signoff.py``)。
"""

from __future__ import annotations

from typing import Literal, Mapping, NamedTuple

from services.defensive_geo.presentation import copy_registry

LEVEL_POLICY_VERSION = "defensive_presentation_level_policy_v1"
STATE_RULE_VERSION = "presentation_state_rule_v1"
CARD_REGISTRY_VERSION = "customer_question_card_registry_v1"
SECTION_REGISTRY_VERSION = "professional_analysis_section_registry_v1"


# ═══════════════════════════════════════════════════════════════════
# Owner 签发状态
# ═══════════════════════════════════════════════════════════════════
class SignatureState(NamedTuple):
    policy_id: str
    signed: bool
    #: 签发人与日期。未签为 None —— ACT-12 要求 H0 门能解析签发元数据。
    signed_by: str | None
    signed_at: str | None
    note: str


#: 🔴 [包F ⑧ · 2026-08-24] 三项**已签**。签发人按 §0.5.2 R-2 分工:
#:
#:    ============================  =========  ========================
#:    policy                        签发人     依据
#:    ============================  =========  ========================
#:    level_policy(等级阈值)        **Owner**  Z-6 亲裁(商业口径)
#:    state_rule(ready/partial)     Review-CTO 纯工程 registry
#:    section_registry(MET-45)      Review-CTO 纯工程 registry
#:    ============================  =========  ========================
#:
#: R-2 逐字:「纯工程 registry 由 Review-CTO 签;Owner 只签商业口径」。
#: 等级阈值是"对客承诺"⇒ Owner;ready/partial 的派生规则与
#: 每个 mode/side/surface 需要哪些指标是工程判断 ⇒ Review-CTO。
#:
#: 🔴 MET-45 **不是空签**:``professional_analysis_sections()`` 里有逐
#:    mode/side/surface 的 required/optional 真内容,且判据机械核对它
#:    与五卡→指标映射同源。空签一份不存在的 registry 就是签一份谎 ——
#:    所以内容先建、再签。
_SIGNATURES: dict[str, SignatureState] = {
    LEVEL_POLICY_VERSION: SignatureState(
        LEVEL_POLICY_VERSION, True, "owner", "2026-08-24",
        "Z-6:出现率三档(阈值为动态系数,见 presentation.level_policy)",
    ),
    STATE_RULE_VERSION: SignatureState(
        STATE_RULE_VERSION, True, "review-cto", "2026-08-24",
        "纯工程 registry(§0.5.2 R-2):derive_section_state 已实现且判据在守",
    ),
    SECTION_REGISTRY_VERSION: SignatureState(
        SECTION_REGISTRY_VERSION, True, "review-cto", "2026-08-24",
        "纯工程 registry(§0.5.2 R-2):逐 mode/side/surface required/optional "
        "由五卡→指标映射机械导出,非手抄",
    ),
}

#: 受众。customer / pdf_customer 属对客面,受签发闸约束。
Audience = Literal[
    "service_provider", "service_provider_demo",
    "customer", "customer_public_demo", "pdf_customer",
]
CUSTOMER_FACING_AUDIENCES: frozenset[str] = frozenset(
    {"customer", "customer_public_demo", "pdf_customer"}
)
ALL_AUDIENCES: tuple[Audience, ...] = (
    "service_provider", "service_provider_demo",
    "customer", "customer_public_demo", "pdf_customer",
)


class PolicyNotSigned(RuntimeError):
    """未签 policy 却要对客下发 terminal 呈现。"""


def signature(policy_id: str) -> SignatureState:
    try:
        return _SIGNATURES[policy_id]
    except KeyError:
        raise ValueError(f"未知 policy {policy_id!r};合法 = {sorted(_SIGNATURES)}") from None


def unsigned_policies() -> tuple[str, ...]:
    return tuple(sorted(p for p, s in _SIGNATURES.items() if not s.signed))


def require_signed_for_customer(audience: str) -> None:
    """对客面的 terminal 呈现闸。fixture/shadow 不受此闸约束。

    MET-36 逐字「未签 policy 不得 enrollment 对客 v2」。
    """
    if audience not in CUSTOMER_FACING_AUDIENCES:
        return
    pending = unsigned_policies()
    if pending:
        raise PolicyNotSigned(
            "以下呈现口径尚未由 Owner 签发,不得对客下发 v2 终态呈现:"
            + "、".join(pending)
            + "。fixture/shadow 仍可使用(§0.5.5 / MET-36 / MET-45)。"
        )


# ═══════════════════════════════════════════════════════════════════
# LevelMeta —— CUR-03 的服务端权威(前端不再自己算等级和颜色)
# ═══════════════════════════════════════════════════════════════════
class LevelMeta(NamedTuple):
    key: str
    label: str
    tone: str
    description_key: str
    definition_version: str


_LEVELS: dict[str, LevelMeta] = {
    "guarded": LevelMeta(
        "guarded", copy_registry.LEVEL_LABELS["guarded"], "positive",
        "level_guarded_description", LEVEL_POLICY_VERSION),
    "needs_strengthening": LevelMeta(
        "needs_strengthening", copy_registry.LEVEL_LABELS["needs_strengthening"],
        "warning", "level_needs_strengthening_description", LEVEL_POLICY_VERSION),
    "priority_fix": LevelMeta(
        "priority_fix", copy_registry.LEVEL_LABELS["priority_fix"], "critical",
        "level_priority_fix_description", LEVEL_POLICY_VERSION),
    "unknown": LevelMeta(
        "unknown", copy_registry.LEVEL_LABELS["unknown"], "neutral",
        "level_unknown_description", LEVEL_POLICY_VERSION),
}

#: §15.6 ``ScoredLevelMeta = Exclude<LevelMeta, {key:'unknown'}>``。
SCORED_LEVEL_KEYS: frozenset[str] = frozenset(_LEVELS) - {"unknown"}


def level_meta(key: str) -> LevelMeta:
    try:
        return _LEVELS[key]
    except KeyError:
        raise ValueError(
            f"未知等级 {key!r};合法 = {sorted(_LEVELS)}。"
            "不设兜底:兜底会让「漏一档」和「写对了」长得一样。"
        ) from None


def level_keys() -> tuple[str, ...]:
    return tuple(_LEVELS)


# ═══════════════════════════════════════════════════════════════════
# 五张客户结果卡(§8.2 / MET-29 / MET-41)
# ═══════════════════════════════════════════════════════════════════
class CardSpec(NamedTuple):
    key: str
    #: 客户看到的主问题。§8.2 逐字。
    question: str
    #: 组合指标(§8.2 第三列)。
    metric_keys: tuple[str, ...]
    ordinal: int


#: §15.6 ``CustomerQuestionKey``,顺序即呈现顺序(MET-29「恰各一次、删卡/重复/换序均拒绝」)。
_CARDS: tuple[CardSpec, ...] = (
    CardSpec("identity", "AI 认得我吗?",
             ("entity_identification", "brand_mention"), 1),
    CardSpec("recommendation", "AI 会推荐我吗?",
             ("explicit_recommendation", "conditional_recommendation",
              "candidate_position"), 2),
    CardSpec("scenario", "客户换种问法还能找到我吗?",
             ("awareness_coverage", "recommendation_coverage", "answer_gap"), 3),
    CardSpec("competition", "AI 把我和谁放在一起?",
             ("disambiguated_competitor_cooccurrence",), 4),
    CardSpec("evidence", "AI 的说法有依据吗?",
             ("source_consistency", "explicit_source", "attribution_level"), 5),
)

CARD_KEYS: tuple[str, ...] = tuple(c.key for c in _CARDS)


def cards() -> tuple[CardSpec, ...]:
    return _CARDS


def card(key: str) -> CardSpec:
    for spec in _CARDS:
        if spec.key == key:
            return spec
    raise ValueError(f"未知客户卡 {key!r};合法 = {list(CARD_KEYS)}")


# ═══════════════════════════════════════════════════════════════════
# presentation_state_rule_v1(§15.6 / MET-36 / MET-44)
# ═══════════════════════════════════════════════════════════════════
SectionState = Literal["ready", "partial", "no_conclusion"]

#: 每个 state 允许的下一步。**恒非空** —— §0.5.6「任何阻塞必须自带解决方案」,
#: 且 Z-2.3 明确要求 no_comparable_baseline 分支不得是 nextAction: null。
_STATE_ACTIONS: dict[str, tuple[str, ...]] = {
    "ready": ("view_evidence",),
    "partial": ("view_evidence", "retest_same_scope"),
    "no_conclusion": ("retest_same_scope", "contact_support"),
}


def derive_section_state(*, valid: int, planned: int) -> SectionState:
    """§15.6:valid=0 必为 no_conclusion;valid>0 但未全部 terminal ⇒ partial。

    MET-36 逐字「policy-skipped/ambiguous/error 使 valid>0 报告为 partial,
    valid=0 必为 no_conclusion」。
    """
    if valid <= 0:
        return "no_conclusion"
    if valid < planned:
        return "partial"
    return "ready"


def state_actions(state: str) -> tuple[str, ...]:
    try:
        actions = _STATE_ACTIONS[state]
    except KeyError:
        raise ValueError(f"未知 section state {state!r}") from None
    if not actions:                              # pragma: no cover - 结构性保证
        raise AssertionError(f"{state} 没有下一步 —— §0.5.6 不允许死路")
    return actions


# ═══════════════════════════════════════════════════════════════════
# professional_analysis_section_registry_v1(MET-45)· 包F ⑧ 真内容
# ═══════════════════════════════════════════════════════════════════
#: MET-45 逐字要求「每个 mode/side/surface 的 **required/optional 指标**」。
#:
#: 🔴 内容**机械导出**,不手抄:required = 该卡 ``CardSpec.metric_keys`` 的
#:    第一个(卡的主指标,没有它这张卡讲不成话),其余为 optional。
#:    手抄一份会在窗B 改卡时静默漂移 —— 而本仓记过
#:    「手写分母漏掉的那一项不会让任何判据变红」。
MODE_SIDES: tuple[str, ...] = ("defensive", "offensive")

#: surface = 呈现出口。三个出口共享同一份 canonical 指标集合(§9.4:
#: 受众差异只能由服务端**裁剪**,不许重算)—— 所以 required/optional
#: 逐 surface **相同**,差异只在 audience 裁剪层。
#: 🔴 这一条是有意的:如果 required 逐 surface 不同,就等于承认
#:    "网页和 PDF 算的不是同一份数",而 WP3 出口条件明令两者逐值一致。
SURFACES: tuple[str, ...] = ("web", "internal", "pdf_customer")


class SectionSpec(NamedTuple):
    mode_side: str
    surface: str
    card_key: str
    required_metrics: tuple[str, ...]
    optional_metrics: tuple[str, ...]


def professional_analysis_sections() -> tuple[SectionSpec, ...]:
    """MET-45 registry 全量。判据拿它当**分母**机械遍历。"""
    out: list[SectionSpec] = []
    for mode_side in MODE_SIDES:
        for surface in SURFACES:
            for spec in _CARDS:
                metrics = spec.metric_keys
                if not metrics:                  # pragma: no cover - 结构性保证
                    raise AssertionError(
                        f"卡 {spec.key} 没有指标 —— MET-45 无法给出 required")
                out.append(SectionSpec(
                    mode_side=mode_side, surface=surface, card_key=spec.key,
                    required_metrics=(metrics[0],),
                    optional_metrics=tuple(metrics[1:]),
                ))
    return tuple(out)


def section_spec(*, mode_side: str, surface: str, card_key: str) -> SectionSpec:
    for s in professional_analysis_sections():
        if (s.mode_side, s.surface, s.card_key) == (mode_side, surface, card_key):
            return s
    raise ValueError(
        f"未知 section({mode_side}, {surface}, {card_key})—— "
        "MET-45「未知 definitionKey 拒绝投影」,不设兜底")


def registry_census() -> Mapping[str, int]:
    """POR-20:registry 集合守恒的机械分母。"""
    return {
        "levels": len(_LEVELS),
        "cards": len(_CARDS),
        "section_states": len(_STATE_ACTIONS),
        "audiences": len(ALL_AUDIENCES),
        "signatures": len(_SIGNATURES),
        # [包F ⑧] MET-45 registry 的机械分母
        "mode_sides": len(MODE_SIDES),
        "surfaces": len(SURFACES),
        "professional_sections": len(professional_analysis_sections()),
    }
