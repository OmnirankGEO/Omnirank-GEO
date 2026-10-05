"""R-4 · RecommendedFact 呈现层(§15.6 + §0.5.2 R-4 @ spec e710be6c2)。

一期全仓**零实现** —— 这个文件是从零建的那一层。

═══════════════════════════════════════════════════════════════════
🔴 R-4 到底改了什么(一句话:冲突不再删事实)
═══════════════════════════════════════════════════════════════════
§0.5.2 R-4 逐字:

    ``accepted_brand_fact``(客户确认过的事实)在 ``hasConflict=true`` 时
    **仍然可用**,冲突信息随行下发;只拒绝非客户来源且无来源支撑的现场生成事实。
    **禁止**因外部信源冲突使客户事实从修复建议中整体消失。

这条裁定针对的是一个很具体的坏结果:客户白纸黑字确认过"我们做的是 A",
网上有一篇旧稿写成"B",于是系统判定"有冲突 → 这条事实不可信 → 整条剔除",
销售最后看到的是**一条没有任何依据的修复建议**。真相不是"没有依据",
而是"依据在,只是外面有别的说法"。所以正确做法是**带着冲突一起下发**,
让销售发之前跟客户核一句 —— 而不是把事实藏起来。

═══════════════════════════════════════════════════════════════════
🔴 那到底拒什么(拒的是"现场编",不是"有分歧")
═══════════════════════════════════════════════════════════════════
拒绝面精确到三类,一类都不多:

  1. **现场生成事实** —— 既不在冻结 manifest 里,也没有公开来源。
     这是唯一真正危险的那一类:它是模型当场编的。
  2. **私密 / 未知可见性** —— manifest 里标了 private/unknown 的条目。
     客户确认过不等于允许公开引用。
  3. **无来源的公开 claim** —— ``corroborated_public_claim`` 却给不出
     逐值闭合的 claim + source。

注意 ``hasConflict`` **不在**这三类里。把它算进拒绝面,正是 R-4 要修的病。

═══════════════════════════════════════════════════════════════════
🔴 audience-bound:ref 不许跨受众复用
═══════════════════════════════════════════════════════════════════
§15.6:「每个 RecommendedFact 必须以 **audience-bound** ``publicFactRef``
恰解析冻结 profileRevision + brand_fact_manifest 的公开事实」。

所以 ref 里带受众。判据把客户面签发的 ref 拿去 PDF 面解析必须拒 ——
不带受众的话这条判据根本没有可打的地方。
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, NamedTuple, Sequence

from services.defensive_geo.presentation import copy_registry, registries

RECOMMENDED_FACT_POLICY_VERSION = "recommended_fact_policy_v1"

#: §15.6 ``RequestedFactKey`` 闭集。分母从这里取,判据不手抄。
REQUESTED_FACT_KEYS: tuple[str, ...] = (
    "legal_name", "brand_alias", "official_website", "service_scope",
    "product_scope", "service_location", "public_credential", "public_contact_channel",
)

#: §15.6 ``PriorityActionFactBasis`` 的四个 actionIntent。
ACTION_INTENTS: tuple[str, ...] = (
    "content_or_publication_repair", "fact_collection",
    "identity_calibration", "comparable_retest",
)

#: manifest 条目里**允许**被引用为公开事实的可见性。
#: 其余(private / unknown / 缺失)一律拒 —— 见模块头拒绝面第 2 类。
PUBLIC_VISIBILITIES: frozenset[str] = frozenset({"public"})

#: manifest 条目里**允许**被引用的状态。客户确认过才算数。
ACCEPTED_STATUSES: frozenset[str] = frozenset({"accepted"})

Provenance = Literal["accepted_brand_fact", "corroborated_public_claim"]


class FactRejected(ValueError):
    """这条事实不能进修复建议。

    **抛,不是过滤掉** —— 静默丢弃会让「修复建议里少了一条」表现成
    「本来就只有两条」,没有任何东西会因此变红。
    """


class RecommendedFact(NamedTuple):
    """§15.6 ``RecommendedFact`` 的服务端形态。

    ``conflict_notice`` 是 R-4 的「冲突信息随行下发」那一半:
    ``has_conflict=True`` 时非空,``False`` 时恒 ``None``。
    """

    public_fact_ref: str
    sanitized_text: str
    provenance: Provenance
    public_claim_ref: str | None
    public_source_refs: tuple[str, ...]
    has_conflict: bool
    conflict_notice: str | None
    basis_label: str

    def wire(self) -> dict[str, Any]:
        """上 wire 的形状。**只出人话与 ref**,不带 manifest 内部字段。"""
        return {
            "publicFactRef": self.public_fact_ref,
            "sanitizedText": self.sanitized_text,
            "provenance": {
                "kind": self.provenance,
                "publicClaimRef": self.public_claim_ref,
                "publicSourceRefs": list(self.public_source_refs),
            },
            # R-4:冲突随行。前端**必须**能读到这两个字段才画得出提示。
            "hasConflict": self.has_conflict,
            "conflictNotice": self.conflict_notice,
            "basisLabel": self.basis_label,
            "factPolicyVersion": RECOMMENDED_FACT_POLICY_VERSION,
        }


def public_fact_ref(
    *, audience: str, profile_revision_id: str, fact_key: str,
) -> str:
    """audience-bound ref。**受众进 ref**,所以跨受众复用解析不出来。

    形如 ``fact:customer:rev-7:legal_name``。
    """
    if audience not in registries.ALL_AUDIENCES:
        raise FactRejected(f"未知受众 {audience!r}")
    if not str(profile_revision_id or "").strip():
        raise FactRejected("profileRevision 为空 —— 事实必须绑在冻结的那一版档案上")
    if not str(fact_key or "").strip():
        raise FactRejected("factKey 为空")
    return f"fact:{audience}:{profile_revision_id}:{fact_key}"


def parse_fact_ref(ref: str) -> tuple[str, str, str]:
    """反解 ref。判据拿它证明「这条 ref 是签给谁的」。"""
    parts = str(ref or "").split(":")
    if len(parts) != 4 or parts[0] != "fact":
        raise FactRejected(f"publicFactRef 形态不对:{ref!r}")
    return parts[1], parts[2], parts[3]


def _conflict_fields(has_conflict: bool) -> tuple[bool, str | None]:
    """R-4 的两个字段一起决定,不许分开设置。

    分开设置会长出 ``hasConflict=True`` 却没有提示语的组合 ——
    界面上就是一个孤零零的黄点,她不知道那是什么意思。
    """
    if not has_conflict:
        return False, None
    return True, copy_registry.translate("sentence", "fact_conflict_notice")


def resolve_accepted_brand_fact(
    *,
    audience: str,
    profile_revision_id: str,
    fact_key: str,
    manifest: Mapping[str, Mapping[str, Any]],
) -> RecommendedFact:
    """把 manifest 里的一条**客户确认过的**事实解析成 RecommendedFact。

    🔴 R-4 的核心就在这个函数里:``hasConflict`` 只影响随行提示,
       **不影响可用性**。想验这一点,把 manifest 里那条的 ``hasConflict``
       翻成 True,这个函数必须照样返回一条事实(而不是抛)。
    """
    entry = manifest.get(fact_key)
    if entry is None:
        # 拒绝面第 1 类:不在冻结 manifest 里 = 现场生成。
        raise FactRejected(
            f"{fact_key!r} 不在冻结的 brand_fact_manifest 里 —— "
            "现场生成的事实不得进入修复建议(§15.6)"
        )
    status = str(entry.get("status") or "")
    visibility = str(entry.get("visibility") or "")
    if status not in ACCEPTED_STATUSES:
        raise FactRejected(
            f"{fact_key!r} 的状态是 {status!r},不是客户确认过的条目"
        )
    if visibility not in PUBLIC_VISIBILITIES:
        # 拒绝面第 2 类。客户确认过 ≠ 允许对外引用。
        raise FactRejected(
            f"{fact_key!r} 可见性 {visibility!r} 不可公开引用"
        )
    text = str(entry.get("publicText") or "").strip()
    if not text:
        raise FactRejected(f"{fact_key!r} 没有可公开的文本")

    has_conflict, notice = _conflict_fields(bool(entry.get("hasConflict")))
    return RecommendedFact(
        public_fact_ref=public_fact_ref(
            audience=audience, profile_revision_id=profile_revision_id, fact_key=fact_key,
        ),
        sanitized_text=text,
        provenance="accepted_brand_fact",
        # §15.6 类型层:accepted_brand_fact 的这两项恒 null / 空。
        public_claim_ref=None,
        public_source_refs=(),
        has_conflict=has_conflict,
        conflict_notice=notice,
        basis_label=copy_registry.translate("sentence", "fact_basis_customer_confirmed"),
    )


def resolve_corroborated_public_claim(
    *,
    audience: str,
    profile_revision_id: str,
    fact_key: str,
    claim_ref: str,
    snapshot_claims: Mapping[str, Mapping[str, Any]],
) -> RecommendedFact:
    """公开 claim + 来源,**逐值闭合**同一 snapshot。

    §15.6 逐字:「``corroborated_public_claim`` 必须将同 snapshot 的
    claim/source occurrence 逐值闭合,``sanitizedText`` 与签发的公开 fact 文本相等」。

    所以这里不接受调用方自己传文本 —— 文本只能来自 claim,
    传进来的只有 ``claim_ref``。能传文本的话「逐值相等」就成了自证式的空话。
    """
    claim = snapshot_claims.get(claim_ref)
    if claim is None:
        raise FactRejected(f"claim {claim_ref!r} 不在同一 snapshot 里")
    sources = tuple(str(s) for s in (claim.get("publicSourceRefs") or ()) if str(s).strip())
    if not sources:
        # 拒绝面第 3 类:公开 claim 却没有来源。
        raise FactRejected(
            f"claim {claim_ref!r} 没有公开来源 —— 无来源的公开 claim 拒绝(§15.6)"
        )
    text = str(claim.get("publicText") or "").strip()
    if not text:
        raise FactRejected(f"claim {claim_ref!r} 没有可公开的文本")

    has_conflict, notice = _conflict_fields(bool(claim.get("hasConflict")))
    return RecommendedFact(
        public_fact_ref=public_fact_ref(
            audience=audience, profile_revision_id=profile_revision_id, fact_key=fact_key,
        ),
        sanitized_text=text,
        provenance="corroborated_public_claim",
        public_claim_ref=claim_ref,
        public_source_refs=sources,
        has_conflict=has_conflict,
        conflict_notice=notice,
        basis_label=copy_registry.translate("sentence", "fact_basis_public_corroborated"),
    )


# ══════════════════════════════════════════════════════════════════════════
# PriorityActionFactBasis —— 按 actionIntent 判别(§15.6 L3483)
# ══════════════════════════════════════════════════════════════════════════
class FactBasisInvalid(ValueError):
    """intent 与 fact basis 错配。**投影失败**,不下发一条自相矛盾的建议。"""


def assert_fact_basis(
    *,
    action_intent: str,
    recommended_facts: Sequence[RecommendedFact],
    requested_fact_keys: Sequence[str],
) -> None:
    """四个 intent 各自的形状。§15.6 逐字:

    · 内容/发布修复 ⇒ 非空 RecommendedFact 且 ``requestedFactKeys=[]``
    · 事实采集 / 身份校准 ⇒ ``recommendedFacts=[]`` + 非空 allowlisted key
    · comparable_retest ⇒ 两者皆空

    🔴 「不能伪装已有事实」这句话的可执行形态就是这个函数:
       采集类给了 recommendedFacts 就是在假装"我已经有依据了"。
    """
    if action_intent not in ACTION_INTENTS:
        raise FactBasisInvalid(
            f"未知 actionIntent {action_intent!r};合法 = {list(ACTION_INTENTS)}"
        )
    facts = list(recommended_facts)
    keys = list(requested_fact_keys)

    if action_intent == "content_or_publication_repair":
        if not facts:
            raise FactBasisInvalid("内容/发布修复必须给出非空 RecommendedFact")
        if keys:
            raise FactBasisInvalid("内容/发布修复的 requestedFactKeys 必须为空")
        return

    if action_intent == "comparable_retest":
        if facts or keys:
            raise FactBasisInvalid("comparable_retest 两者都必须为空")
        return

    # fact_collection / identity_calibration
    if facts:
        raise FactBasisInvalid(
            f"{action_intent} 不得携带 RecommendedFact —— 那是在伪装已有事实"
        )
    if not keys:
        raise FactBasisInvalid(f"{action_intent} 必须给出非空 requestedFactKeys")
    bad = [k for k in keys if k not in REQUESTED_FACT_KEYS]
    if bad:
        raise FactBasisInvalid(
            f"requestedFactKeys 越界:{bad};allowlist = {list(REQUESTED_FACT_KEYS)}"
        )


def requested_fact_labels(keys: Sequence[str]) -> list[dict[str, str]]:
    """把 key 翻成人话再上屏。查不到就抛(U-1:裸枚举上屏 = 红)。"""
    return [
        {"key": k, "label": copy_registry.translate("requested_fact", k)}
        for k in keys
    ]


def census() -> dict[str, Any]:
    """机械导出分母。判据不手抄这两个闭集。"""
    return {
        "policyVersion": RECOMMENDED_FACT_POLICY_VERSION,
        "requestedFactKeys": list(REQUESTED_FACT_KEYS),
        "actionIntents": list(ACTION_INTENTS),
        "provenanceKinds": ["accepted_brand_fact", "corroborated_public_claim"],
        "acceptedStatuses": sorted(ACCEPTED_STATUSES),
        "publicVisibilities": sorted(PUBLIC_VISIBILITIES),
        # R-4 的可机读断言:冲突**不在**拒绝面里。
        "conflictIsRejectable": False,
    }
