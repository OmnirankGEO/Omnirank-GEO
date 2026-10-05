"""外调前的广告法 H0 门(规格 §11.1 / §11.3 第五步)。判据:MED-11 / MED-18。

═══════════════════════════════════════════════════════════════════════
🔴 这道门的位置:**写 external-start marker 之前、任何 provider 调用之前**
═══════════════════════════════════════════════════════════════════════
§11.1 逐字。位置错一格的后果是具体的:

  · 放在 external-start **之后** → 命中时钱已经在外面了,``provider call=0``
    这条判据永远绿不了(它数的是调用次数,不是意图);
  · 放在 confirm 阶段而不放在 worker → §19 变异 112 点名的形态:
    「preview/confirm 使用 catalog v1 后签发 v2,worker external-start 仍按 v1 外调」。
    catalog 是**热加载**的(``config/legal_prohibited_pack.json`` mtime 变化即生效),
    所以必须在**外调那一刻**再验一次,而不是信 confirm 时验过的结论。

═══════════════════════════════════════════════════════════════════════
🔴 未签发时**不拦**,不是「保险起见先拦着」
═══════════════════════════════════════════════════════════════════════
``services/defensive_geo/h0_rule_catalog.enforce_h0`` 是唯一入口。
它抛 ``H0RuleNotSigned`` 时本模块返回 :class:`LegalVerdict` 的 ``advisory`` 形态:
**不阻断发布**,只把命中当提示带走。

这不是放水,是 §1.1 + 00_DEV_PRINCIPLES 第 1 条的直接后果:
未经 Owner 签发的目录**无权硬拦**(该 pack 自己的 ``_doc`` 也是这个口径)。
「保险起见先拦着」正是开发原则里被点名的「Codex 式过度谨慎」——
它拦住的是我们自己的交付,拦不住任何真实法律风险。

═══════════════════════════════════════════════════════════════════════
🔴 普通质量/证据/风格 finding **不能借这道门阻断**
═══════════════════════════════════════════════════════════════════════
§11.1:「普通质量、风格、证据强弱和 A1 finding 不能借法律门阻断外调,
删除法律 H0 校验的变异必须转红,但**扩大 catalog 之外的硬门也必须转红**」。

实现形态:本模块**只**读 ``legal_context.find_absolute_violations`` 的结果,
而那个函数的词表**只**来自签发包(``absolute_terms()``)。本文件里没有任何
自己的词表、正则或阈值 —— 想扩门必须去改签发包,而那需要 Owner。
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple, Sequence

from services.defensive_geo import h0_rule_catalog

LEGAL_GATE_VERSION = "defgeo-publish-legal-gate-v1"

#: 唯一的规则 ID。派生签发登记在 ``h0_rule_catalog._DERIVED``。
RULE_ID = "defgeo.publish.legal_catalog"

#: §15.7 ``LegalRuleHitPublicDetails.repairActionKind`` 只有这一个值。
REPAIR_ACTION_KIND: Literal["repair_passage"] = "repair_passage"

Mode = Literal["blocking", "advisory"]


class LegalRuleHit(NamedTuple):
    """§15.7 ``LegalRuleHitPublicDetails`` 的服务端形态。

    ``passageRef`` 必须与 ``nextAction.target.passageRef`` **逐值相等**
    (MED-18 / §19 变异 162「legal repair id/passage 错配」)。
    """

    rule_id: str
    rule_version: str
    passage_ref: str
    passage_excerpt: str
    article_revision_id: str
    repair_action_kind: str = REPAIR_ACTION_KIND

    def wire(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "ruleVersion": self.rule_version,
            "passageRef": self.passage_ref,
            "passageExcerpt": self.passage_excerpt,
            "articleRevisionId": self.article_revision_id,
            "repairActionKind": self.repair_action_kind,
        }


class LegalVerdict(NamedTuple):
    """一次门判定。``blocked=True`` ⇒ provider call 必须为 0、资金方向 no-effect。"""

    mode: Mode
    blocked: bool
    rule_version: str | None
    hits: tuple[LegalRuleHit, ...]
    #: 未签发时的说明(给日志/交付单,不上客户屏)。
    reason: str

    @property
    def primary(self) -> LegalRuleHit | None:
        """§15.7:命中时**唯一**局部修复动作 —— 所以对外只下发第一处。"""
        return self.hits[0] if self.hits else None


def gate_mode() -> Mode:
    """当前门是硬拦还是仅提示。由**签发状态**决定,不由 flag 决定。"""
    try:
        h0_rule_catalog.enforce_h0(RULE_ID)
    except h0_rule_catalog.H0RuleNotSigned:
        return "advisory"
    return "blocking"


def _passage_ref(article_revision_id: str, start: int, end: int) -> str:
    """稳定、可定位、不含内部实现细节的段落引用。

    形如 ``rev:<article_revision_id>#chars=120-126``。前端拿它做「点这里修好这一句」
    的锚点;它同时是 ``nextAction.target.passageRef`` —— 两处必须一模一样。
    """
    return f"rev:{article_revision_id}#chars={int(start)}-{int(end)}"


def evaluate(
    *,
    article_revision_id: str,
    frozen_body: str,
) -> LegalVerdict:
    """对**冻结的** article revision 正文再验一次。

    🔴 入参是 ``frozen_body`` 不是 ``article_id``:
       从 id 现查正文会读到「当前最新」的正文,而要发的是**冻结的那一版**。
       §11.1 逐字「对即将发布的冻结 article revision 再验一次」。
    """
    mode = gate_mode()
    if mode == "advisory":
        # 未签发:仍然**扫**(结果可用于 A1 提示),但绝不阻断。
        hits = _scan(article_revision_id, frozen_body, rule_version="unsigned")
        return LegalVerdict(
            mode="advisory", blocked=False, rule_version=None, hits=hits,
            reason=(
                f"{RULE_ID} 当前未由 Owner 签发(或法律包回落内嵌兜底)—— "
                "按 §1.1 未签目录无权硬拦,本次只作提示"
            ),
        )

    rule = h0_rule_catalog.enforce_h0(RULE_ID)
    hits = _scan(article_revision_id, frozen_body, rule_version=rule.rule_version)
    return LegalVerdict(
        mode="blocking",
        blocked=bool(hits),
        rule_version=rule.rule_version,
        hits=hits,
        reason="" if hits else "未命中已签发的广告法目录",
    )


def _scan(article_revision_id: str, body: str, *, rule_version: str) -> tuple[LegalRuleHit, ...]:
    """扫描。**词表只来自签发包** —— 本文件零词表、零正则、零阈值。"""
    try:
        from services.marketing.legal_context import find_absolute_violations
    except Exception:                        # pragma: no cover - 环境缺件
        return ()
    out: list[LegalRuleHit] = []
    for hit in find_absolute_violations(body or ""):
        out.append(LegalRuleHit(
            rule_id=RULE_ID,
            rule_version=rule_version,
            passage_ref=_passage_ref(article_revision_id, hit.start, hit.end),
            passage_excerpt=hit.excerpt,
            article_revision_id=article_revision_id,
        ))
    return tuple(out)


def repair_action(hit: LegalRuleHit, *, label: str) -> dict[str, Any]:
    """§15.7 ``repair_legal_passage``。target 的 id/passageRef 与 hit **逐值相等**。"""
    return {
        "kind": "repair_legal_passage",
        "label": label,
        "actionRef": f"publish:repair_legal_passage:{hit.passage_ref}",
        "capability": "repair_content",
        "target": {
            "kind": "article_revision",
            "id": hit.article_revision_id,
            "passageRef": hit.passage_ref,
        },
    }


def assert_repair_matches(hit: LegalRuleHit, action: dict[str, Any]) -> None:
    """必须不命中的那一半:target 与 hit 错配当场拒(§19 变异 162)。"""
    target = action.get("target") or {}
    if target.get("id") != hit.article_revision_id or target.get("passageRef") != hit.passage_ref:
        raise AssertionError(
            "legal repair target 与 legalRuleHit 不逐值相等 —— "
            f"hit=({hit.article_revision_id}, {hit.passage_ref}) "
            f"action=({target.get('id')}, {target.get('passageRef')})"
        )


def census() -> dict[str, Any]:
    return {
        "gateVersion": LEGAL_GATE_VERSION,
        "ruleId": RULE_ID,
        "mode": gate_mode(),
        "repairActionKind": REPAIR_ACTION_KIND,
        "signedRuleIds": sorted(h0_rule_catalog.signed_rule_ids()),
        "derivedCandidates": sorted(h0_rule_catalog.derived_candidate_ids()),
    }


def assert_terms_come_only_from_signed_pack(terms: Sequence[str]) -> None:
    """扩门反向锁:传入的词必须**全部**来自签发包。

    MED-11「扩大 catalog 之外的硬门也必须转红」的可执行形态:
    判据把一个包外的词喂进来,这里必须抛。
    """
    from services.marketing.legal_context import absolute_terms

    signed = set(absolute_terms())
    extra = [t for t in terms if t not in signed]
    if extra:
        raise AssertionError(
            f"这些词不在 Owner 签发的目录里:{extra} —— "
            "实现方不得临时解释法律并扩大禁区(法律包 _doc 原文)"
        )
