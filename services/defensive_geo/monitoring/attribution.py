"""§7.5 显式来源归因 / 陈述支持 —— 两条**正交**轴。

规格 §7.5 逐字把它们拆成两个问题:

* **显式来源归因**只回答「平台明确列出了什么来源」;
* **陈述支持等级**只回答「公开资料能否支持这句话」,**不得冒充 AI 的真实来源**。

混成一条轴的后果很具体:客户会读到"AI 明确引用了你们官网",而真相是
"我们自己去搜到了官网,AI 一个字都没说来源"。§7.5 因此规定:
只有 ``explicit_citation`` 才可以写"AI 明确引用了……";其余只能写
"可找到公开资料支持"或"暂不可追溯"。

🔴 MET-11:**无 URL/body proof 不得标 explicit attribution**
------------------------------------------------------------
「平台回了一个来源字段」和「那个来源确实能对上这句话」是两件事。
本模块因此有三级,不是两级:

    eligible  → 可稳定切分、可判断有没有映射的陈述(**没有映射的也进分母**)
    mapped    → 平台**显式返回**了来源映射
    verified  → 映射的 URL 与正文 proof 与该陈述**可核验匹配**

MET-32 的 10/2/1 夹具逐字锁死了这三级的关系:

    10 eligible / 2 mapped / 1 verified  ⇒  主覆盖率 = 1/10、proof 有效率 = 1/2

注意主覆盖率的分子是 **verified(1)** 而不是 mapped(2) ——
因为 §7.5 的 ``explicit_citation`` 定义里就带着「URL/body proof 可核验匹配」,
MET-11 又明令没有 proof 不得标 explicit。:func:`attribution_metrics` 按这个算,
并在 :func:`assert_met32_fixture` 里把这三个数钉死。

⚠️ 已知不一致(已在交付单挂号,未擅自改窗B 签发件)
--------------------------------------------------
``services/defensive_geo/presentation/metric_definitions`` 里
``explicit_source_coverage`` 那一行的 numerator 写的是
``claims_with_explicit_mapping``(= 2),与 MET-32 要求的 1 不一致。
本模块**按规格 MET-32 出数**,同时把两个计数都显式下发
(:func:`attribution_metrics` 返回 ``mappedClaims`` 与 ``verifiedClaims``),
让消费方不必猜。registry 行的 numerator 名要不要改属于签发件变更
(改行必须升 definition version、slice commitment 随之改),
不由执行方擅自动 —— 见交付单「待裁定」。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

from services.defensive_geo.monitoring import denominators as _den

ATTRIBUTION_VERSION = "defgeo-attribution-v1"
CLAIM_SPLITTER_VERSION = "defgeo-claim-splitter-v1"

#: §7.5 表一:显式来源归因**只有两档**。
#: 刻意没有第三档 —— 「平台回了来源但对不上」不是一种归因状态,
#: 它就是 ``unknown``(我们无法追溯),同时 proof 有效率会掉下来。
SOURCE_ATTRIBUTION_STATES: tuple[str, ...] = ("explicit_citation", "unknown")

#: §7.5 表二:陈述支持四档。与 :mod:`defensive_projection` 的 SUPPORT_LEVELS 同源。
CLAIM_SUPPORT_LEVELS: tuple[str, ...] = (
    "corroborated", "inferred", "unsupported", "unknown",
)

#: 对客文案约束(§7.5 逐字)。判据拿它做负向锁:
#: 非 explicit_citation 的格出现"明确引用"字样即红。
EXPLICIT_ONLY_PHRASES: tuple[str, ...] = ("明确引用", "明确列出了来源")


class AttributionError(ValueError):
    """归因形态不合法。**不出数**,而不是出一个把推测说成引用的结论。"""


class ClaimAttribution(NamedTuple):
    """一条客户可见回答陈述的归因事实。"""

    claim_key: str
    evidence_cell_ref: str
    is_source_observable: bool     # 该 platform/surface 合同能否观察 source 字段
    has_explicit_mapping: bool     # 平台**显式返回**了来源映射
    has_url_proof: bool
    has_body_proof: bool
    support_level: str
    has_conflict: bool
    support_source_count: int

    @property
    def is_eligible(self) -> bool:
        """§6.3 ``source_attribution_eligible_claims`` 逐字:
        「……可稳定切分、可判断是否存在来源映射的全部回答陈述;
        **没有映射的陈述也必须进入分母**」。

        所以 eligible 只看"这一格的来源字段可不可观察",不看"有没有映射"。
        把没映射的踢出分母 = MET-32 点名的「删除 8 个无映射 claims」作弊。
        """
        return self.is_source_observable

    @property
    def is_verified_explicit(self) -> bool:
        """MET-11:URL **和** 正文 proof 都有,才算 explicit attribution。"""
        return self.has_explicit_mapping and self.has_url_proof and self.has_body_proof

    @property
    def attribution_state(self) -> str:
        return "explicit_citation" if self.is_verified_explicit else "unknown"


def assert_support_consistent(claim: ClaimAttribution) -> None:
    """MET-23 逐字的四条:各档必须有对应的支持源。"""
    if claim.support_level not in CLAIM_SUPPORT_LEVELS:
        raise AttributionError(f"未知 supportLevel {claim.support_level!r}")
    if claim.support_level == "corroborated" and claim.support_source_count < 1:
        raise AttributionError(
            f"{claim.claim_key}: corroborated 必须至少一条独立公开支持源"
            "(MET-23);零来源的「有据可查」就是编的")
    if claim.support_level == "inferred" and claim.support_source_count < 1:
        raise AttributionError(
            f"{claim.claim_key}: inferred 必须至少一条公开线索源(MET-23)")
    if claim.support_level in ("unsupported", "unknown") and claim.support_source_count:
        raise AttributionError(
            f"{claim.claim_key}: {claim.support_level} 不得带支持源 —— "
            "有源却判无支持,两条轴就对不上了(MET-23)")


def assert_no_explicit_claim_without_proof(
    claim: ClaimAttribution, customer_copy: str
) -> None:
    """§7.5 对客文案门:没有 verified proof 就不许写"明确引用"。

    这不是措辞洁癖。写了"AI 明确引用了你们官网"而 AI 其实没说来源,
    客户拿去对外讲,被人一问就穿帮 —— 那是我们制造的谎,属于我们**控制得了**
    的那一类(开发原则第 1 条),所以必须设门。
    """
    if claim.is_verified_explicit:
        return
    for phrase in EXPLICIT_ONLY_PHRASES:
        if phrase in customer_copy:
            raise AttributionError(
                f"{claim.claim_key} 没有 URL+正文 proof,对客文案却出现「{phrase}」。"
                "只有 explicit_citation 才可以说 AI 明确引用;"
                "其余只能写「可找到公开资料支持」或「暂不可追溯」(§7.5 / MET-11)。"
            )


class AttributionMetrics(NamedTuple):
    eligible_claims: int
    mapped_claims: int
    verified_claims: int
    coverage: _den.Ratio           # verified / eligible   —— MET-32 主覆盖率
    proof_rate: _den.Ratio         # verified / mapped     —— MET-32 proof 有效率


def attribution_metrics(claims: Sequence[ClaimAttribution]) -> AttributionMetrics:
    """§7.5 两条轴的两个比值。零分母 ⇒ ``value=None``,**不是 0%**。"""
    for c in claims:
        assert_support_consistent(c)

    eligible = [c for c in claims if c.is_eligible]
    mapped = [c for c in eligible if c.has_explicit_mapping]
    verified = [c for c in eligible if c.is_verified_explicit]

    # 🔴 三级必须单调:verified ⊆ mapped ⊆ eligible。
    #    不单调说明上游把"没映射但有 proof"这种不可能的组合造出来了。
    if not (len(verified) <= len(mapped) <= len(eligible)):
        raise AttributionError(
            f"归因三级不单调:verified={len(verified)} mapped={len(mapped)} "
            f"eligible={len(eligible)}(MET-33「0<=verified<=mapped<=eligible」)"
        )

    coverage = _den.ratio(
        numerator_label="claims_with_verified_explicit_attribution",
        denominator_key="source_attribution_eligible_claims",
        numerator=len(verified),
        denominator=len(eligible),
    )
    proof_rate = _den.ratio(
        numerator_label="claims_with_url_and_body_proof",
        denominator_key="explicit_mapped_claims",
        numerator=len(verified),
        denominator=len(mapped),
    )
    return AttributionMetrics(
        eligible_claims=len(eligible), mapped_claims=len(mapped),
        verified_claims=len(verified), coverage=coverage, proof_rate=proof_rate,
    )


def assert_met32_fixture(metrics: AttributionMetrics) -> None:
    """MET-32 / §20.5 的 10/2/1 夹具逐值钉死。

    判据直接调它。三个数任何一个漂了都在这里炸,而不是等到报告上
    出现一个"看起来挺合理"的百分比。
    """
    if (metrics.eligible_claims, metrics.mapped_claims, metrics.verified_claims) != (10, 2, 1):
        raise AttributionError(
            f"这不是 10/2/1 夹具:eligible={metrics.eligible_claims} "
            f"mapped={metrics.mapped_claims} verified={metrics.verified_claims}"
        )
    if (metrics.coverage.numerator, metrics.coverage.denominator) != (1, 10):
        raise AttributionError(
            f"主覆盖率应为 1/10,实得 {metrics.coverage.numerator}/"
            f"{metrics.coverage.denominator}(MET-32 逐字)"
        )
    if (metrics.proof_rate.numerator, metrics.proof_rate.denominator) != (1, 2):
        raise AttributionError(
            f"proof 有效率应为 1/2,实得 {metrics.proof_rate.numerator}/"
            f"{metrics.proof_rate.denominator}(MET-32 逐字)"
        )


def assert_no_causal_claim_without_source(
    *, has_explicit_source: bool, customer_copy: str
) -> None:
    """POR-08 / MET-11:「无显式来源时只说观察变化,**不说由发布导致**」。

    §7.5 逐字:「不得把『发布后回答变化』自动写成『由这篇文章导致』」。
    """
    if has_explicit_source:
        return
    causal = ("由这篇", "因为发布", "发布带来", "导致 AI", "使得 AI", "所以 AI 才")
    for phrase in causal:
        if phrase in customer_copy:
            raise AttributionError(
                f"没有显式来源却写了因果「{phrase}」。没有来源时只能说"
                "『观察到变化』,不能说『由发布导致』(§7.5 / POR-08)。"
            )


def census() -> dict[str, Any]:
    return {
        "attributionVersion": ATTRIBUTION_VERSION,
        "claimSplitterVersion": CLAIM_SPLITTER_VERSION,
        "sourceAttributionStates": list(SOURCE_ATTRIBUTION_STATES),
        "claimSupportLevels": list(CLAIM_SUPPORT_LEVELS),
        "explicitOnlyPhrases": list(EXPLICIT_ONLY_PHRASES),
    }
