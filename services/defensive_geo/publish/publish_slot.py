"""发布格(publish slot)身份与**八态全序**准入(规格 §12.2 / §15.7)。

判据:MED-20 / MED-13 / FIN-03 / FIN-14 / API-04 / §19 变异 161。

═══════════════════════════════════════════════════════════════════════
🔴 为什么是「先读全序」而不是「查有没有 live command」
═══════════════════════════════════════════════════════════════════════
§12.2 逐字:「publish slot admission 必须先读该 slot 的 canonical 全序,
不能只检查'当前没有 live command'」。

差别在两个真实损失方向:

  · 只查 live:``verified_published + committed`` 的格没有 live command
    → 放行新 preview → **换个 HTTP key 就能二次发布二次扣费**;
  · 只查 live:``failed_no_effect + released + legalRuleHit`` 的格也没有 live command
    → 放行普通重试 → **绕过广告法局部修复直接重发**。

所以这里把八态写成**数据**(:data:`_ADMISSION`),每态的 admission 分支、
可下发动作、以及"允许不允许普通 preview"三项逐格钉死。
没有 ``else`` 兜底 —— 兜底会让「漏一格」和「写对了」长得一样。

🔴 slot 身份必须由**服务端从对象血缘派生**
------------------------------------------
§12.2:「服务端从对象血缘派生 scope,客户端不能通过提交 tenant/service id 选择归属」。
所以 :func:`derive_publish_slot_id` 的入参全部是服务端已冻结的对象标识,
函数签名里**没有**任何"客户端可提交"的位置。

``plan_item_key`` 只在其 quote/snapshot scope 内稳定(§12.2 原文),因此
slot 身份必须同时含 ``tenant_owner_id + service_projection_id + accepted_snapshot_id``
—— FIN-14「两 tenant/contract 使用相同 plan_item_key + article_revision_id 不碰撞」
承重就在这三项上。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import unicodedata
from typing import Any, Literal, Mapping, NamedTuple

SLOT_IDENTITY_VERSION = "defgeo-publish-slot-v1"

#: §12.2 的 command kind。v1 只有发布这一种;写成常量是为了让
#: ``publish_slot_id`` 的派生输入里那一格有名字,而不是一个魔法字符串。
COMMAND_KIND = "media_publication"

#: 🔴 八态全序(MED-20 逐字点名的八格)。**判据的分母,不手抄**。
SlotState = Literal[
    "empty",                        # 从未有过 snapshot/command
    "open_snapshot",                # 有唯一 open snapshot 待确认
    "in_flight",                    # queued / running / submitting
    "outcome_unknown",              # unknown / conflict / pending_reconciliation / quarantined
    "fulfilled",                    # verified_published + committed
    "ordinary_no_effect_released",  # rejected/failed no-effect + released,legalRuleHit=null
    "legal_no_effect_released",     # failed_no_effect + released + legalRuleHit 非空
    "retracted_committed",          # 已发布后下架,历史 settlement 保留
]
SLOT_STATES: tuple[SlotState, ...] = (
    "empty", "open_snapshot", "in_flight", "outcome_unknown", "fulfilled",
    "ordinary_no_effect_released", "legal_no_effect_released", "retracted_committed",
)

#: §15.7 ``PublishPreviewAdmissionResponse.slotAdmission`` 闭集。
SlotAdmission = Literal[
    "snapshot_ready", "existing_command", "retry_child_required",
    "legal_repair_required", "replacement_policy_required",
]


class SlotError(ValueError):
    """slot 状态不可识别或投影不自洽。**拒绝准入**,不猜。"""


class AdmissionRule(NamedTuple):
    """一格的准入合同。六项全部必填 —— 缺一格就等于给那一格开了后门。"""

    #: preview 端点该返回的 admission 分支。``None`` = 该态允许签发新 snapshot。
    admission: SlotAdmission
    #: 是否允许**普通** preview 签发一个新的 open snapshot。
    allows_ordinary_preview: bool
    #: 是否允许 confirm(只有 open_snapshot 一格为真)。
    allows_confirm: bool
    #: 是否允许 retry-child(只有普通 no-effect 一格为真)。
    allows_retry_child: bool
    #: 该态下唯一合法的 nextAction kind。
    next_action_kind: str
    #: 该 action 对应的 capability(§15.7 逐值)。
    capability: str


#: 🔴 八格,一格不多一格不少。**没有 else**。
_ADMISSION: dict[SlotState, AdmissionRule] = {
    # ── 允许普通 preview 的两格 ────────────────────────────────────────
    "empty": AdmissionRule(
        admission="snapshot_ready", allows_ordinary_preview=True,
        allows_confirm=False, allows_retry_child=False,
        next_action_kind="confirm_publish_decision", capability="confirm_publish_decision",
    ),
    # cancelled 的格与 empty 同权:§12.2「empty|snapshot_cancelled_before_command:
    # 允许普通 preview」。cancel 不减少客户合同交付量(§15.7),所以它必须能重开。
    "open_snapshot": AdmissionRule(
        admission="snapshot_ready", allows_ordinary_preview=True,
        allows_confirm=True, allows_retry_child=False,
        next_action_kind="confirm_publish_decision", capability="confirm_publish_decision",
    ),
    # ── 只能看原 command 的三格 ────────────────────────────────────────
    "in_flight": AdmissionRule(
        admission="existing_command", allows_ordinary_preview=False,
        allows_confirm=False, allows_retry_child=False,
        next_action_kind="view_existing_command", capability="view_publish_status",
    ),
    "outcome_unknown": AdmissionRule(
        admission="existing_command", allows_ordinary_preview=False,
        allows_confirm=False, allows_retry_child=False,
        next_action_kind="view_existing_command", capability="view_publish_status",
    ),
    # 🔴 fulfilled:换 HTTP key / 换 snapshot / 换 article revision 都不能再发再扣。
    #    这一格是「二次扣费」的唯一入口,所以它和 in_flight 共用 existing_command
    #    分支但**语义不同** —— slotState 字段把它们分开,前端据此说不同的话。
    "fulfilled": AdmissionRule(
        admission="existing_command", allows_ordinary_preview=False,
        allows_confirm=False, allows_retry_child=False,
        next_action_kind="view_existing_command", capability="view_publish_status",
    ),
    # ── 各自唯一出路的三格 ────────────────────────────────────────────
    "ordinary_no_effect_released": AdmissionRule(
        admission="retry_child_required", allows_ordinary_preview=False,
        allows_confirm=False, allows_retry_child=True,
        next_action_kind="retry_child", capability="retry_publish_child",
    ),
    # 🔴 法律格**不能** retry-child(MED-18 逐字)。只能先局部修复 →
    #    新 article revision + 新 decision + 重新 confirm。
    "legal_no_effect_released": AdmissionRule(
        admission="legal_repair_required", allows_ordinary_preview=False,
        allows_confirm=False, allows_retry_child=False,
        next_action_kind="repair_legal_passage", capability="repair_content",
    ),
    "retracted_committed": AdmissionRule(
        admission="replacement_policy_required", allows_ordinary_preview=False,
        allows_confirm=False, allows_retry_child=False,
        next_action_kind="review_replacement_policy", capability="review_publish_replacement",
    ),
}

#: ``lifecycle=cancelled`` 的 snapshot 不改变 slot 的可用性 —— 它回到 empty 语义。
#: 写成显式别名而不是「把 cancelled 也塞进 empty」,是为了让 store 层能如实记录
#: 「这个格曾经取消过一次」,而准入仍按 empty 处理。
CANCELLED_BEFORE_COMMAND: SlotState = "empty"


def derive_publish_slot_id(
    *,
    tenant_owner_id: object,
    service_projection_id: object,
    accepted_snapshot_id: object,
    plan_item_key: object,
    command_kind: str = COMMAND_KIND,
) -> str:
    """§12.2 的确定性 slot 身份。

    ``tenant_owner_id + service_projection_id + accepted_snapshot_id + plan_item_key
    + command_kind`` —— 五项一个不少。少任一项都会让 FIN-14 的跨租户/跨合同
    碰撞变成可能(``plan_item_key`` 本身不是全局唯一)。
    """
    parts = [
        ("tenant_owner_id", tenant_owner_id),
        ("service_projection_id", service_projection_id),
        ("accepted_snapshot_id", accepted_snapshot_id),
        ("plan_item_key", plan_item_key),
        ("command_kind", command_kind),
    ]
    framed: list[bytes] = [SLOT_IDENTITY_VERSION.encode("ascii")]
    for name, value in parts:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise SlotError(f"slot 身份的 {name} 不得为空 —— 少一项就可能跨租户碰撞(FIN-14)")
        framed.append(unicodedata.normalize("NFC", str(value)).encode("utf-8"))
    blob = b"".join(struct.pack(">Q", len(p)) + p for p in framed)
    secret = (os.environ.get("DEFGEO_PUBLIC_IDENTITY_SECRET") or "").encode("utf-8")
    if not secret:
        from auth.jwt_utils import JWT_SECRET   # 没配 env 时从本部署的 JWT 密钥派生(与 decision_snapshot 同域)
        secret = hmac.new(JWT_SECRET.encode("utf-8"), b"defgeo-public-identity/integrity", hashlib.sha256).digest()
    return "pslot_" + hmac.new(secret, blob, hashlib.sha256).hexdigest()[:40]


def derive_publish_item_request_id(publish_slot_id: str) -> str:
    """§12.2:``publish_item_request_id`` 由 slot **确定性派生**,不是浏览器随机值。

    跨 preview / 换 HTTP key / 换 snapshot version,同一交付格恒得同一个 request id。
    这正是「换 Idempotency-Key 不能顺序二发二扣」的第二道结构性防线
    (第一道是 slot 上的 partial unique)。
    """
    if not isinstance(publish_slot_id, str) or not publish_slot_id.startswith("pslot_"):
        raise SlotError(f"publish_slot_id 形态不合法:{publish_slot_id!r}")
    return "pireq_" + hashlib.sha256(publish_slot_id.encode("ascii")).hexdigest()[:40]


def rule(state: object) -> AdmissionRule:
    """取一格的准入合同。未知态**抛**,不兜底。"""
    if state not in _ADMISSION:
        raise SlotError(
            f"未知 slot 状态 {state!r};合法 = {list(SLOT_STATES)}。"
            "不设兜底:兜底会让漏一格和写对了长得一样"
        )
    return _ADMISSION[state]  # type: ignore[index]


def allows_ordinary_preview(state: object) -> bool:
    return rule(state).allows_ordinary_preview


def allows_confirm(state: object) -> bool:
    return rule(state).allows_confirm


def allows_retry_child(state: object) -> bool:
    return rule(state).allows_retry_child


def classify_command(
    *,
    canonical_publication_state: str,
    funding_state: str,
    legal_rule_hit: bool,
) -> SlotState:
    """把一条已存在 command 的 canonical 事实投影成 slot 态。

    🔴 入参故意是**三项 canonical 事实**而不是一个 ``status`` 字符串:
       §12.1「不能把浏览器自报、镜像 URL 或裸 status 当结算真值」。
       三项都从 canonical item 读,镜像/自报进不来。
    """
    from services.defensive_geo.publish import publish_settlement as _s

    direction = _s.settlement_direction(canonical_publication_state)

    if canonical_publication_state == "not_started":
        return "in_flight"
    if direction == "hold_frozen":
        return "in_flight"
    if direction == "hold_or_quarantine":
        return "outcome_unknown"
    if direction == "commit":
        return "fulfilled"
    if direction == "preserve_historical_commit":
        return "retracted_committed"
    if direction == "release":
        # 🔴 法律格与普通格的**唯一**判别位是 legalRuleHit,不是 reason 文案。
        #    按文案判会让「改一句错误提示」静默改变资金/重试出路。
        if legal_rule_hit:
            return "legal_no_effect_released"
        if funding_state != "released":
            # release 方向但钱还没退 —— 尚未收敛,仍按未知处置(不许放行新发)。
            return "outcome_unknown"
        return "ordinary_no_effect_released"
    raise SlotError(f"无法识别的 settlementDirection {direction!r}")


def admission_for(state: object) -> dict[str, Any]:
    """给端点用的 admission 投影(不含对象 id —— 那是调用方填的)。"""
    r = rule(state)
    return {
        "slotState": state,
        "slotAdmission": r.admission,
        "allowsOrdinaryPreview": r.allows_ordinary_preview,
        "allowsConfirm": r.allows_confirm,
        "allowsRetryChild": r.allows_retry_child,
        "nextActionKind": r.next_action_kind,
        "capability": r.capability,
    }


def census() -> dict[str, Any]:
    """§0.5.3 G-2 机械分母。"""
    return {
        "identityVersion": SLOT_IDENTITY_VERSION,
        "commandKind": COMMAND_KIND,
        "slotStates": list(SLOT_STATES),
        "rules": {s: admission_for(s) for s in SLOT_STATES},
        "ordinaryPreviewStates": [s for s in SLOT_STATES if _ADMISSION[s].allows_ordinary_preview],
        "confirmableStates": [s for s in SLOT_STATES if _ADMISSION[s].allows_confirm],
        "retryChildStates": [s for s in SLOT_STATES if _ADMISSION[s].allows_retry_child],
    }
