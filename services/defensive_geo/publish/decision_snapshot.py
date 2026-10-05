"""媒体决策冻结快照:canonicalHash、confirmability 判别联合、七类调整出口。

规格 §11.3 / §11.4 / §15.7。判据:MED-01/02/09/10/12/13/14/17/21、
FIN-11/15、API-04、§19 变异 16/17/78/98/136/145/157/162/163/173。

═══════════════════════════════════════════════════════════════════════
🔴 冻结面 vs live 面:字节永不随时间变
═══════════════════════════════════════════════════════════════════════
§15.7 逐字:「``snapshot`` 字节与 canonicalHash 永不随时间、库存、服务态、
余额或审批变化;``lifecycle/confirmability/budgetBlockers/adjustmentOptions``
是逐请求授权重算的 live sidecar」。

所以本模块把两者做成**两个函数、两条返回路径**:

  · :func:`freeze_snapshot` → 冻结面。输入全是已冻结对象,输出 + hash 一次性定死;
  · :func:`live_sidecar` → live 面。每次请求重算,**不许回写冻结面**。

判据 MED-14 要打的正是「sidecar 改写 frozen hash」——
两个函数物理分离让那件事必须显式写一行赋值才可能发生。

═══════════════════════════════════════════════════════════════════════
🔴 canonicalHash 覆盖**有序完整 alternatives**
═══════════════════════════════════════════════════════════════════════
MED-21:「替换、追加、删减或**换序**任一候选都必须改变 hash」。
换序也算 —— 所以 alternatives 进 hash 时**保持服务端确定顺序**,不排序、不去重。
(排序会让「换序」这一发变异静默存活。)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import unicodedata
from typing import Any, Literal, Mapping, NamedTuple, Sequence

from services.defensive_geo.publish import media_identity as _mi
from services.defensive_geo.publish.body_hash import assert_body_hash

SNAPSHOT_CONTRACT_VERSION = "defgeo-publish-decision-snapshot-v1"

#: §15.7 ``PublishDecisionSnapshotResponse`` 的 lifecycle 闭集。
Lifecycle = Literal["open", "expired", "superseded", "cancelled", "consumed"]
LIFECYCLES: tuple[Lifecycle, ...] = ("open", "expired", "superseded", "cancelled", "consumed")

#: 只有 ``open`` 可以 confirm。写成集合而不是 ``== "open"`` —— 判据拿它当分母。
CONFIRMABLE_LIFECYCLES: frozenset[str] = frozenset({"open"})

SupersessionKind = Literal["new_preview", "override", "replacement"]
SUPERSESSION_KINDS: tuple[SupersessionKind, ...] = ("new_preview", "override", "replacement")

FundingPolicy = Literal[
    "personal_wallet", "organization_budget",
    "admin_platform_ledger", "sponsor_platform_ledger",
]


class SnapshotError(ValueError):
    """快照/投影不自洽 —— **不签发**。"""


# ══════════════════════════════════════════════════════════════════════════
# §15.7 冻结资金五行矩阵(注意:是**五行**不是四行 —— organization 占两行)
# ══════════════════════════════════════════════════════════════════════════
class FrozenFundingRow(NamedTuple):
    principal_kind: Literal["personal", "organization", "platform_cost_center"]
    approval_requirement: Literal["not_required", "required"]
    sponsor_ref_required: bool
    #: 该行 live approvalState 的合法集合(§15.7「open sidecar 还要与 frozen
    #: approvalRequirement 守恒」)。
    live_approval_states: frozenset[str]
    #: 该行只能收到的调整出口(§15.7「live confirmability 与每个 adjustment option
    #: 的 fundingPolicy 必须逐值等于 frozen snapshot」)。
    allowed_option_kinds: tuple[str, ...]


#: 🔴 五行,一行不多一行不少。**没有 else**。
_FROZEN_FUNDING: dict[tuple[FundingPolicy, str], FrozenFundingRow] = {
    ("personal_wallet", "not_required"): FrozenFundingRow(
        "personal", "not_required", False, frozenset({"not_required"}),
        # 个人才能收到钱包充值。给组织/平台发「去充值」= 让他替别人掏钱。
        ("top_up", "reduce_unconfirmed_scope", "reduce_customer_delivery",
         "choose_lower_cost_alternative", "cancel_no_charge"),
    ),
    ("organization_budget", "not_required"): FrozenFundingRow(
        "organization", "not_required", False, frozenset({"not_required"}),
        ("request_approval", "reduce_unconfirmed_scope", "reduce_customer_delivery",
         "choose_lower_cost_alternative", "cancel_no_charge"),
    ),
    ("organization_budget", "required"): FrozenFundingRow(
        "organization", "required", False,
        # 🔴 required 行的 live 态只能是这三个,且**只有 approved 能 confirm**。
        frozenset({"required", "approved", "rejected"}),
        ("request_approval", "reduce_unconfirmed_scope", "reduce_customer_delivery",
         "choose_lower_cost_alternative", "cancel_no_charge"),
    ),
    ("admin_platform_ledger", "not_required"): FrozenFundingRow(
        "platform_cost_center", "not_required", False, frozenset({"not_required"}),
        # 平台成本中心:不得出现个人充值或组织审批(§15.7 逐字)。
        ("choose_lower_cost_alternative", "reduce_unconfirmed_scope",
         "reduce_customer_delivery", "contact_platform_budget_owner", "cancel_no_charge"),
    ),
    ("sponsor_platform_ledger", "not_required"): FrozenFundingRow(
        "platform_cost_center", "not_required", True, frozenset({"not_required"}),
        ("choose_lower_cost_alternative", "reduce_unconfirmed_scope",
         "reduce_customer_delivery", "contact_platform_budget_owner", "cancel_no_charge"),
    ),
}

#: 七类调整出口(§0.5.5 U-7 裁定「以 §15.7 为准」,§10.3 的 5 种已被更正)。
#: ``choose_lower_cost_alternative`` 按是否改变客户承诺分两支,但 kind 只有一个。
ADJUSTMENT_OPTION_KINDS: tuple[str, ...] = (
    "top_up", "reduce_unconfirmed_scope", "reduce_customer_delivery",
    "choose_lower_cost_alternative", "request_approval",
    "contact_platform_budget_owner", "cancel_no_charge",
)

#: 🔴 §15.7:「``cancel_no_charge`` 必须**始终存在**于资金不足的 options」。
MANDATORY_INSUFFICIENT_OPTION = "cancel_no_charge"

#: 每类出口的固定语义(§15.7 逐值)。写成数据 —— 端点不许现编。
_OPTION_SEMANTICS: dict[str, dict[str, Any]] = {
    "top_up": {
        "scope": ("global", "media_publication"),
        "newCustomerSnapshotRequired": False, "reconfirmPublishDecision": True,
        "publicLossRequired": False,
        "nextActionKind": "top_up", "capability": "open_wallet",
    },
    "reduce_unconfirmed_scope": {
        "scope": ("media_publication",),
        "newCustomerSnapshotRequired": False, "reconfirmPublishDecision": True,
        "publicLossRequired": True,
        "nextActionKind": "reduce_provider_optional_scope", "capability": "edit_provider_budget",
    },
    "reduce_customer_delivery": {
        "scope": ("media_publication",),
        "newCustomerSnapshotRequired": True, "reconfirmPublishDecision": True,
        "publicLossRequired": True,
        "nextActionKind": "new_customer_snapshot", "capability": "revise_quote",
    },
    "choose_lower_cost_alternative": {
        "scope": ("media_publication",),
        # 两支:within_accepted_commitment → False / reduces_accepted_commitment → True。
        "newCustomerSnapshotRequired": None,
        "reconfirmPublishDecision": True,
        "publicLossRequired": None,
        "nextActionKind": None, "capability": None,
    },
    "request_approval": {
        "scope": ("global", "media_publication"),
        "newCustomerSnapshotRequired": False, "reconfirmPublishDecision": True,
        "publicLossRequired": False,
        "nextActionKind": "request_approval", "capability": "request_owner_approval",
    },
    "contact_platform_budget_owner": {
        "scope": ("global", "media_publication"),
        "newCustomerSnapshotRequired": False, "reconfirmPublishDecision": True,
        "publicLossRequired": False,
        "nextActionKind": "contact_support", "capability": "contact_support",
    },
    "cancel_no_charge": {
        "scope": ("media_publication",),
        "newCustomerSnapshotRequired": False, "reconfirmPublishDecision": False,
        "publicLossRequired": True,
        "nextActionKind": "cancel", "capability": "cancel_publish_preview",
    },
}

#: §15.7 ``cancel_no_charge.publicChange`` 是**字面量**,不是自由文案。
#: U-2 把它改写成人话:「取消这次选媒体(这篇稿子还没发,之后另行安排)」。
CANCEL_NO_CHARGE_PUBLIC_CHANGE = "取消这次选媒体（这篇稿子还没发，之后另行安排）"


def frozen_funding_row(funding_policy: str, approval_requirement: str) -> FrozenFundingRow:
    key = (funding_policy, approval_requirement)
    if key not in _FROZEN_FUNDING:
        raise SnapshotError(
            f"非法冻结资金格 {key};合法 = {sorted(_FROZEN_FUNDING)}。"
            "四类 payer/handle/sponsor ref 串格是变异 157 点名的形态"
        )
    return _FROZEN_FUNDING[key]  # type: ignore[index]


def validate_frozen_funding(
    *,
    funding_policy: str,
    principal_kind: str,
    approval_requirement: str,
    sponsor_policy_ref: object,
) -> FrozenFundingRow:
    """§15.7「frozen funding matrix 必须逐值为」那一段,逐项核。"""
    row = frozen_funding_row(funding_policy, approval_requirement)
    if principal_kind != row.principal_kind:
        raise SnapshotError(
            f"{funding_policy}/{approval_requirement} 的 principalKind 必须是 "
            f"{row.principal_kind},实得 {principal_kind!r}"
        )
    has_ref = bool(sponsor_policy_ref and str(sponsor_policy_ref).strip())
    if row.sponsor_ref_required != has_ref:
        raise SnapshotError(
            f"{funding_policy} 的 sponsorPolicyRef 存在性错配"
            f"(需要={row.sponsor_ref_required} 实得={has_ref})—— "
            "缺 ref 的 sponsor 与 admin 无法区分"
        )
    return row


def validate_live_approval(
    *, funding_policy: str, approval_requirement: str, approval_state: str,
) -> None:
    """open sidecar 的 approvalState 必须与 frozen approvalRequirement 守恒。"""
    row = frozen_funding_row(funding_policy, approval_requirement)
    if approval_state not in row.live_approval_states:
        raise SnapshotError(
            f"{funding_policy}/{approval_requirement} 的 live approvalState 只能是 "
            f"{sorted(row.live_approval_states)},实得 {approval_state!r} —— "
            "变异 157「required 却 live not_required+canConfirm」"
        )


def allowed_option_kinds(funding_policy: str, approval_requirement: str) -> tuple[str, ...]:
    return frozen_funding_row(funding_policy, approval_requirement).allowed_option_kinds


def assert_option_allowed(
    *, funding_policy: str, approval_requirement: str, option_kind: str,
) -> None:
    """必须不命中的那一半:出口串格当场拒。"""
    allowed = allowed_option_kinds(funding_policy, approval_requirement)
    if option_kind not in allowed:
        raise SnapshotError(
            f"{funding_policy} 不得下发 {option_kind!r};允许 = {list(allowed)}。"
            "个人/组织/平台三条钱腿的动作不许互串(§15.7)"
        )


# ══════════════════════════════════════════════════════════════════════════
# confirmability 判别联合
# ══════════════════════════════════════════════════════════════════════════
ConfirmReasonCode = Literal[
    "expired", "superseded", "insufficient_points", "approval_required",
    "approval_rejected", "inventory_unavailable", "service_not_active",
    "already_consumed", "cancelled",
]

class _ReasonSpec(NamedTuple):
    next_action_kind: str
    capability: str
    target_kind: str
    #: 该 reason 是否属于「lifecycle 已终结」——终结态**不得**再开放 confirm,
    #: 也不下发任何 blocker/option(§15.7 ``PublishDecisionInactiveSidecar``)。
    inactive: bool

#: 🔴 §15.7 ``PublishDecisionConfirmability`` 九个 reason,逐个钉死 action/capability/target。
#:    「label-only、false+null、required+not_required+confirmable 或错 reason/action
#:    均拒绝投影」——拒绝的实现就是这张表 + :func:`confirmability`。
_REASONS: dict[ConfirmReasonCode, _ReasonSpec] = {
    "expired": _ReasonSpec("new_preview", "create_publish_preview", "plan_item", True),
    "superseded": _ReasonSpec("review_new_snapshot", "view_publish_snapshot", "publish_snapshot", True),
    "cancelled": _ReasonSpec("new_preview", "create_publish_preview", "plan_item", True),
    "already_consumed": _ReasonSpec("view_existing_command", "view_publish_status", "publish_command", True),
    "insufficient_points": _ReasonSpec(
        "review_adjustment_options", "review_publish_adjustments", "publish_snapshot", False),
    "approval_required": _ReasonSpec("request_approval", "request_owner_approval", "approval", False),
    "approval_rejected": _ReasonSpec("contact_owner", "contact_owner", "approval", False),
    "inventory_unavailable": _ReasonSpec("new_preview", "create_publish_preview", "plan_item", False),
    "service_not_active": _ReasonSpec("contact_owner", "contact_owner", "service_projection", False),
}

#: lifecycle → 该 lifecycle 强制的 reason(§15.7:expired/superseded/cancelled/consumed
#: 「不能塞进 ready 分支」,也不能重新开放 confirm)。
_LIFECYCLE_REASON: dict[str, ConfirmReasonCode] = {
    "expired": "expired",
    "superseded": "superseded",
    "cancelled": "cancelled",
    "consumed": "already_consumed",
}


def reason_spec(code: object) -> _ReasonSpec:
    if code not in _REASONS:
        raise SnapshotError(f"未知 confirmability reasonCode {code!r};合法 = {sorted(_REASONS)}")
    return _REASONS[code]  # type: ignore[index]


def confirmability(
    *,
    lifecycle: str,
    funding_policy: str,
    approval_requirement: str,
    approval_state: str,
    blocked_reason: str | None,
    target_ids: Mapping[str, str],
    label_for: Any,
    explain_for: Any = None,
) -> dict[str, Any]:
    """算出 §15.7 的 ``PublishDecisionConfirmability`` 一格。

    ``label_for(kind)`` 由调用方注入(copy_registry),**本模块不自造文案** ——
    自造等于给同一个概念开第二个叫法(U-1「同一概念全站唯一叫法」)。
    """
    if lifecycle not in LIFECYCLES:
        raise SnapshotError(f"未知 lifecycle {lifecycle!r};合法 = {list(LIFECYCLES)}")

    # ① lifecycle 终结态:reason 由 lifecycle 决定,调用方给的 blocked_reason 一律不采信。
    #    (采信会让「已 consumed 的快照因为余额够而重新 canConfirm」成为可能。)
    if lifecycle in _LIFECYCLE_REASON:
        code: ConfirmReasonCode = _LIFECYCLE_REASON[lifecycle]
    elif blocked_reason is None:
        validate_live_approval(
            funding_policy=funding_policy,
            approval_requirement=approval_requirement,
            approval_state=approval_state,
        )
        if approval_requirement == "required" and approval_state != "approved":
            raise SnapshotError(
                "organization required 只有 approved 才能 canConfirm —— "
                f"实得 approvalState={approval_state!r} 却没有 blocked_reason"
            )
        return {
            "canConfirm": True,
            "fundingPolicy": funding_policy,
            "approvalState": approval_state,
            "reasonCode": None,
            "nextAction": None,
        }
    else:
        code = blocked_reason  # type: ignore[assignment]

    spec = reason_spec(code)
    target_id = target_ids.get(spec.target_kind)
    if not target_id:
        raise SnapshotError(
            f"reasonCode={code} 的 nextAction 缺 target({spec.target_kind}) —— "
            "label-only / null target 的死 CTA 一律拒绝(§19 变异 143)"
        )
    label = label_for(spec.next_action_kind)
    if not label:
        raise SnapshotError(f"nextAction {spec.next_action_kind} 没有登记文案")
    # 🔴 ``publicExplanation`` 与 nextAction 一起下发(§0.5.6:任何阻塞必须
    #    **自带解决方案**;只有按钮没有原因,她不知道为什么被拦)。
    #    译文来自 copy_registry 的 ``reason`` 表 —— 取不到就**不带这一格**,
    #    绝不从 reasonCode 反推一句话(反推 = 前端/后端自造文案,U-2 禁)。
    explanation = explain_for(code) if explain_for is not None else None
    payload: dict[str, Any] = {
        "canConfirm": False,
        "fundingPolicy": funding_policy,
        "approvalState": approval_state,
        "reasonCode": code,
    }
    if explanation:
        payload["publicExplanation"] = explanation
    payload["nextAction"] = {
        "kind": spec.next_action_kind,
        "label": label,
        "actionRef": f"publish:{spec.next_action_kind}:{target_id}",
        "capability": spec.capability,
        "target": {"kind": spec.target_kind, "id": target_id},
    }
    return payload


def assert_sidecar_consistent(
    *, confirm: Mapping[str, Any], blockers: Sequence[Any], options: Sequence[Any],
) -> None:
    """§15.7:``canConfirm=true`` 时 blockers/options 必须为空;
    ``insufficient_points`` 时两者必须非空且差额可复算。"""
    if confirm.get("canConfirm"):
        if blockers or options:
            raise SnapshotError(
                "canConfirm=true 时 blockers/options 必须为空 —— "
                "给一个能确认的方案再挂一串「你还差多少」是自相矛盾"
            )
        return
    code = confirm.get("reasonCode")
    spec = reason_spec(code)
    if spec.inactive:
        if blockers or options:
            raise SnapshotError(
                f"lifecycle 终结态({code})必须是 InactiveSidecar:blockers/options 都为空"
            )
        return
    if code == "insufficient_points":
        if not blockers or not options:
            raise SnapshotError("insufficient_points 时 blockers 与 options 都必须非空")
        if not any(o.get("kind") == MANDATORY_INSUFFICIENT_OPTION for o in options):
            raise SnapshotError(
                f"资金不足的 options 必须始终包含 {MANDATORY_INSUFFICIENT_OPTION} —— §15.7 逐字"
            )
        for b in blockers:
            required = b.get("requiredExactPoints")
            remaining = b.get("remainingPoints")
            delta = b.get("deltaPoints")
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (required, remaining, delta)):
                raise SnapshotError("budget blocker 的三项金额必须是整数")
            if delta != required - remaining:
                raise SnapshotError(
                    f"blocker 差额不可复算:{required} - {remaining} != {delta}"
                )
            if b.get("scope") not in ("global", "media_publication"):
                raise SnapshotError(f"blocker 必须明确 scope,实得 {b.get('scope')!r}")


# ══════════════════════════════════════════════════════════════════════════
# canonicalHash
# ══════════════════════════════════════════════════════════════════════════
def _utf16_sort_key(key: str) -> tuple[int, ...]:
    return tuple(ord(c) for c in key.encode("utf-16-be").decode("utf-16-be"))


def _canonical(value: Any) -> Any:
    """RFC8785 JCS 前的规范化。与 presentation/commitments 同形,**刻意不复用**:
    那是 presentation 域的 commitment,这是交易域的 hash;共用会让改一边顺手改另一边。"""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise SnapshotError("canonicalHash 不接受浮点数 —— 金额必须是整数算力")
    if isinstance(value, Mapping):
        return {unicodedata.normalize("NFC", str(k)): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        # 🔴 **不排序**。MED-21 要求换序必须改 hash。
        return [_canonical(v) for v in value]
    raise SnapshotError(f"canonicalHash 不接受 {type(value).__name__}")


def _jcs(value: Any) -> bytes:
    def enc(node: Any) -> str:
        if node is None:
            return "null"
        if node is True:
            return "true"
        if node is False:
            return "false"
        if isinstance(node, int):
            return str(node)
        if isinstance(node, str):
            out = ['"']
            for ch in node:
                if ch == '"':
                    out.append('\\"')
                elif ch == "\\":
                    out.append("\\\\")
                elif ch == "\b":
                    out.append("\\b")
                elif ch == "\f":
                    out.append("\\f")
                elif ch == "\n":
                    out.append("\\n")
                elif ch == "\r":
                    out.append("\\r")
                elif ch == "\t":
                    out.append("\\t")
                elif ord(ch) < 0x20:
                    out.append(f"\\u{ord(ch):04x}")
                else:
                    out.append(ch)
            out.append('"')
            return "".join(out)
        if isinstance(node, list):
            return "[" + ",".join(enc(v) for v in node) + "]"
        if isinstance(node, dict):
            items = sorted(node.items(), key=lambda kv: _utf16_sort_key(kv[0]))
            return "{" + ",".join(f"{enc(k)}:{enc(v)}" for k, v in items) + "}"
        raise SnapshotError(f"JCS 无法编码 {type(node).__name__}")
    return enc(_canonical(value)).encode("utf-8")


def _frame(*parts: bytes) -> bytes:
    return b"".join(struct.pack(">Q", len(p)) + p for p in parts)


def _key() -> bytes:
    raw = (os.environ.get("DEFGEO_PUBLIC_IDENTITY_SECRET") or "").encode("utf-8")
    if raw:
        return raw
    from auth.jwt_utils import JWT_SECRET   # 没配 env 时从本部署的 JWT 密钥派生(与 publish_slot 同域)
    return hmac.new(JWT_SECRET.encode("utf-8"), b"defgeo-public-identity/integrity", hashlib.sha256).digest()


#: hash 域分隔:provider 决策 hash 与 customer 确认 hash **不同域签发**(POR-19)。
_DOMAIN_PROVIDER = b"defgeo/publish-decision/provider/v1"
_DOMAIN_COMMAND = b"defgeo/publish-command/canonical/v1"


def canonical_hash(frozen: Mapping[str, Any]) -> str:
    """provider decision canonicalHash。覆盖 §15.7 点名的**全部**字段。

    :func:`freeze_snapshot` 保证传进来的 mapping 就是完整冻结面 ——
    在这里再挑字段等于给「漏一格」留位置。
    """
    return hmac.new(_key(), _frame(_DOMAIN_PROVIDER, _jcs(frozen)), hashlib.sha256).hexdigest()


def command_canonical_hash(payload: Mapping[str, Any]) -> str:
    """``commandCanonicalHash`` —— command/object/funding handle/outbox root + parent lineage。

    §15.7 逐字:「二者禁止同名或互换」。用**不同 HMAC 域**实现:
    即便有人把同一份 payload 喂给两个函数,结果也不可能相等。
    """
    return hmac.new(_key(), _frame(_DOMAIN_COMMAND, _jcs(payload)), hashlib.sha256).hexdigest()


class Candidate(NamedTuple):
    """一个公开候选。``reason_facts`` 是**冻结**的理由事实,小榜只转述不现编(MED-05)。"""

    identity: _mi.PublicMediaIdentity
    reason_facts: tuple[tuple[str, str], ...]     # (kind, label)
    exact_points: int

    def wire(self) -> dict[str, Any]:
        for kind, label in self.reason_facts:
            if kind not in _mi.REASON_KINDS:
                raise SnapshotError(f"未知 reasonFact kind {kind!r}")
            if not label or not str(label).strip():
                raise SnapshotError(f"reasonFact {kind} 缺 label")
        if isinstance(self.exact_points, bool) or not isinstance(self.exact_points, int):
            raise SnapshotError("exactPoints 必须是整数")
        if self.exact_points < 0:
            raise SnapshotError("exactPoints 不得为负")
        return {
            **self.identity.provider_dto(),
            "reasonFacts": [{"kind": k, "label": v} for k, v in self.reason_facts],
            "exactPoints": self.exact_points,
        }


def assert_option_ids_resolve(
    decision: Candidate, alternatives: Sequence[Candidate],
) -> None:
    """MED-21:同 snapshot 内每个 option id 必须**恰好解析一个** canonical candidate。

    decision 若也出现在 alternatives,必须**逐字同对象**(同 option/media/root/role/
    reasonFacts/exactPoints)。同 id 指向不同媒体/价格一律拒绝。
    """
    seen: dict[str, dict[str, Any]] = {}
    for cand in (decision, *alternatives):
        wire = cand.wire()
        oid = wire["publicMediaOptionId"]
        prev = seen.get(oid)
        if prev is None:
            seen[oid] = wire
            continue
        if prev != wire:
            raise SnapshotError(
                f"option id {oid[:12]}… 解析到两个不同候选 —— "
                "同 id 对应不同媒体、价格或私有路由一律拒绝(MED-21)"
            )


def freeze_snapshot(
    *,
    decision_snapshot_id: str,
    publish_slot_id: str,
    snapshot_version: int,
    expires_at: str,
    accepted_snapshot_id: str,
    accepted_snapshot_hash: str,
    service_projection_id: str,
    service_projection_version: int,
    plan_item_key: str,
    question_key: str,
    question_revision: int,
    article_revision_id: str,
    article_hash: str,
    pricing_catalog_version: str,
    inventory_snapshot_version: str,
    execution_budget_snapshot_id: str,
    execution_budget_version: int,
    global_budget: Mapping[str, int],
    scope_budget: Mapping[str, int],
    decision: Candidate,
    alternatives: Sequence[Candidate],
    publish_item_request_id: str,
    replacement_policy_version: str,
    funding_policy: str,
    principal_kind: str,
    approval_requirement: str,
    sponsor_policy_ref: str | None,
) -> tuple[dict[str, Any], str]:
    """签发冻结面 + canonicalHash。**一次性定死,之后只读**。

    返回 ``(frozen_wire, canonical_hash)``。
    """
    validate_frozen_funding(
        funding_policy=funding_policy,
        principal_kind=principal_kind,
        approval_requirement=approval_requirement,
        sponsor_policy_ref=sponsor_policy_ref,
    )
    assert_body_hash(article_hash, field="articleHash")
    assert_option_ids_resolve(decision, alternatives)

    for name, budget in (("globalBudget", global_budget), ("scopeBudget", scope_budget)):
        missing = {"capPoints", "reservedPoints", "committedPoints", "remainingPoints"} - set(budget)
        if missing:
            raise SnapshotError(f"{name} 缺字段 {sorted(missing)}")
        for k, v in budget.items():
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise SnapshotError(f"{name}.{k} 必须是非负整数,实得 {v!r}")
        if budget["remainingPoints"] != budget["capPoints"] - budget["reservedPoints"] - budget["committedPoints"]:
            raise SnapshotError(
                f"{name} 算术不守恒:cap - reserved - committed != remaining。"
                "逐项价格正确不代表整包守恒(§3.4)"
            )

    decision_wire = decision.wire()
    total = decision.exact_points
    frozen: dict[str, Any] = {
        "contractVersion": SNAPSHOT_CONTRACT_VERSION,
        "decisionSnapshotId": decision_snapshot_id,
        "publishSlotId": publish_slot_id,
        "snapshotVersion": int(snapshot_version),
        "expiresAt": expires_at,
        "acceptedSnapshotId": accepted_snapshot_id,
        # 🔴 accepted 客户快照的 hash 进 provider hash —— 反向**不成立**:
        #    customer confirmation hash 不含 points(FIN-15 / POR-11)。
        "acceptedSnapshotHash": accepted_snapshot_hash,
        "serviceProjectionId": service_projection_id,
        "serviceProjectionVersion": int(service_projection_version),
        "planItemKey": plan_item_key,
        "questionKey": question_key,
        "questionRevision": int(question_revision),
        "articleRevisionId": article_revision_id,
        "articleHash": article_hash,
        "pricingCatalogVersion": pricing_catalog_version,
        "inventorySnapshotVersion": inventory_snapshot_version,
        "executionBudgetSnapshotId": execution_budget_snapshot_id,
        "executionBudgetVersion": int(execution_budget_version),
        "budgetScope": "media_publication",
        "globalBudget": dict(global_budget),
        "scopeBudget": dict(scope_budget),
        "decision": {**decision_wire, "publishItemRequestId": publish_item_request_id},
        # 🔴 有序完整 alternatives 全量进 hash(MED-21:换序也要改 hash)。
        "alternatives": [c.wire() for c in alternatives],
        "totalExactPoints": total,
        "replacementPolicyVersion": replacement_policy_version,
        "fundingPolicy": funding_policy,
        "principalKind": principal_kind,
        "approvalRequirement": approval_requirement,
        "sponsorPolicyRef": sponsor_policy_ref,
    }
    _mi.assert_no_private_leak(frozen, field="frozenSnapshot")
    digest = canonical_hash(frozen)
    frozen["canonicalHash"] = digest
    return frozen, digest


def assert_exact_points_equal(
    *, frozen: Mapping[str, Any], exact_settlement_points: int, item_points: int,
) -> None:
    """§15.7:三处 exact points 必须逐值相等且非负整数(MED-09「三处 exact points 相等」)。"""
    values = {
        "snapshot.totalExactPoints": frozen.get("totalExactPoints"),
        "snapshot.decision.exactPoints": (frozen.get("decision") or {}).get("exactPoints"),
        "response.exactSettlementPoints": exact_settlement_points,
        "response.item.exactSettlementPoints": item_points,
    }
    for name, v in values.items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise SnapshotError(f"{name} 必须是非负整数,实得 {v!r}")
    distinct = set(values.values())
    if len(distinct) != 1:
        raise SnapshotError(f"三处 exact points 不相等:{values}")


def census() -> dict[str, Any]:
    return {
        "contractVersion": SNAPSHOT_CONTRACT_VERSION,
        "lifecycles": list(LIFECYCLES),
        "confirmableLifecycles": sorted(CONFIRMABLE_LIFECYCLES),
        "supersessionKinds": list(SUPERSESSION_KINDS),
        "frozenFundingRows": [
            {
                "fundingPolicy": p, "approvalRequirement": a,
                "principalKind": r.principal_kind,
                "sponsorRefRequired": r.sponsor_ref_required,
                "liveApprovalStates": sorted(r.live_approval_states),
                "allowedOptionKinds": list(r.allowed_option_kinds),
            }
            for (p, a), r in _FROZEN_FUNDING.items()
        ],
        "adjustmentOptionKinds": list(ADJUSTMENT_OPTION_KINDS),
        "mandatoryInsufficientOption": MANDATORY_INSUFFICIENT_OPTION,
        "reasonCodes": {
            k: {
                "nextActionKind": v.next_action_kind, "capability": v.capability,
                "targetKind": v.target_kind, "inactive": v.inactive,
            }
            for k, v in _REASONS.items()
        },
        "lifecycleReason": dict(_LIFECYCLE_REASON),
    }
