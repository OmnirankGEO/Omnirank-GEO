"""小榜五阶段意图协调层(规格 §9/§10)。

## 这一层负责什么、不负责什么

**负责**:把「用户说了个目标」变成一个**服务端持有的、不可变的、可恢复的**操作意图,
并记录用户是否确认过。仅此而已。

**不负责**(写进代码而不是写进文档,因为这是最容易漂的一条):

* 不写钱包、订单、任务、领域终态 —— 那些归现役领域服务;
* 不算价格 —— 报价来自现役 resolver,本层只存**快照与指纹**;
* 不做第二套权限 —— 每个阶段都由调用方(端点)按当前请求重验;
* 不复制领域状态机 —— ``domain_projection`` 只是现役任务的投影与最后观察水位。

## 三条必须在数据库层成立的性质

1. **prepare 幂等靠唯一约束,不靠先查后插**(§10.2 原文)。
   实现是 ``INSERT ... ON CONFLICT (tenant_owner_id, operation_id, prepare_request_id)
   DO NOTHING RETURNING``,与仓内先例 ``middleware/billing.py::_begin_deduction_idempotency``
   同形。同 key 同 canonical input → 同一个 intent;同 key 异 input → 409。
2. **confirm/cancel 靠 revision CAS**。``UPDATE ... WHERE intent_id=%s AND
   intent_revision=%s`` —— cancel 与 execute 并发只有一方能成功(§10.3.1)。
3. **回执单次消费**。``UNIQUE (intent_id, intent_revision)`` 让重放拿到**同一张**回执
   而不是第二张(§19.1 #6);消费用条件 UPDATE(``WHERE consumed_at IS NULL``),
   参照 ``services/geo_douyin/settlement.py`` 的条件 UPDATE 抢占先例。

## 双时钟(P0-C)

``compute_quote_expires_at`` 是价格锁的短 TTL,到期只代表**要重新报价**;
``approval_window_expires_at`` 是审批等待窗口(长、可配)。
审批是否过期只看后者 —— 用价格锁那只钟去判审批过期,会把合法审批静默拦死。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from services.xiaobang_command_contract import (
    CommandContract,
    SIDE_EFFECT_EXTERNAL,
    SIDE_EFFECT_TO_CONFIRMATION,
)

INTENT_CONTRACT_VERSION = "xiaobang-five-phase-v1"

# ── 协调态(§10.5 冻结枚举)────────────────────────────────────────────────
STATE_PREPARED = "prepared"
STATE_AWAITING_CONFIRMATION = "awaiting_confirmation"
STATE_CONFIRMED = "confirmed"
STATE_APPROVAL_PENDING = "approval_pending"
STATE_APPROVAL_REJECTED = "approval_rejected"
STATE_EXECUTABLE = "executable"
STATE_EXECUTION_LINKED = "execution_linked"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"
STATE_SUPERSEDED = "superseded"

INTENT_STATES: tuple[str, ...] = (
    STATE_PREPARED, STATE_AWAITING_CONFIRMATION, STATE_CONFIRMED,
    STATE_APPROVAL_PENDING, STATE_APPROVAL_REJECTED, STATE_EXECUTABLE,
    STATE_EXECUTION_LINKED, STATE_CANCELLED, STATE_EXPIRED, STATE_SUPERSEDED,
)

#: 🔴 `executing` **不在**枚举里。规格 §10.5 的二选一裁定:领域执行进度由
#: ``domain_projection.state`` 承载,协调层只标「有没有把执行请求关联到领域任务」。
#: 保留 executing 会形成与 domain_projection 重叠的第二条状态语义。
_FORBIDDEN_STATES = frozenset({"executing"})

#: 还没产生任何外部副作用、允许 cancel 的协调态。
CANCELLABLE_STATES = frozenset({
    STATE_PREPARED, STATE_AWAITING_CONFIRMATION, STATE_CONFIRMED,
    STATE_APPROVAL_PENDING, STATE_EXECUTABLE,
})

# ── 三条投影的冻结枚举(§10.5)────────────────────────────────────────────
#: 🔴 未知值一律落 ``unknown``,**绝不**默认 failed 或 succeeded(§19.1 #23)。
#: 为了 UI 好看把未知说成失败,等于替用户下了一个我们并不知道的结论;
#: 说成成功更糟 —— 那是伪造终态(H0 XB-H0-FAKE-TERMINAL)。
DOMAIN_STATES: tuple[str, ...] = (
    "not_started", "queued", "running", "waiting_user", "succeeded",
    "failed", "cancelled", "unknown", "reconciling",
)
SETTLEMENT_STATES: tuple[str, ...] = (
    "none", "quoted", "reserved", "committed", "released",
    "refund_pending", "refunded", "quarantined",
)
EXTERNAL_STATES: tuple[str, ...] = (
    "not_applicable", "not_started", "started", "accepted",
    "verified_succeeded", "verified_failed", "unknown", "reconciling",
)


def _freeze(value: object, allowed: tuple[str, ...], fallback: str, *, default: str) -> str:
    """把领域给的 raw state 收进冻结枚举。

    三条分支,刻意分开写:

    * **空**(None / "" / 纯空白)→ ``default``。「还没有值」不是「值我不认识」,
      压成 unknown 会让一个刚 prepare 完、根本没有领域任务的 intent 显示"未知";
    * **认识**(逐字命中)→ 原值透传;
    * **不认识**(非空但不在枚举里)→ ``fallback``。

    🔴 归一化**只到两端空白为止**。大小写不归一 —— ``"SUCCESS"`` 落 unknown 而不是
       ``succeeded``:不同领域的枚举可能只差大小写,替它猜就是伪造终态
       (H0 XB-H0-FAKE-TERMINAL)。剥两端空白是消除传输噪声,不是猜。
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return default
    return text if text in allowed else fallback


#: 稳定错误码 → HTTP 状态。冻结面(§18 WP0「冻结 v3 schema、错误码和状态映射」)。
#: 新增一个码必须同时在这里登记,否则 tests 的双向判据会红:
#:   · 代码里抛了但没登记 → 前端拿到一个没人认识的码;
#:   · 登记了但代码里没抛 → 死条目,让人以为有这条路。
ERROR_CODES: dict[str, int] = {
    "INTENT_NOT_FOUND": 404,
    "OPERATION_NOT_FOUND": 404,
    "PREPARE_REQUEST_ID_REQUIRED": 400,
    "PREPARE_REQUEST_ID_CONFLICT": 409,
    "INTENT_STATE_UNAVAILABLE": 503,
    "RECEIPT_STATE_UNAVAILABLE": 503,
    "INVALID_INTENT_STATE": 500,
    "USER_ACTION_CHALLENGE_REQUIRED": 400,
    "CONFIRMATION_NOT_APPLICABLE": 409,
    "CONFIRMATION_REQUIRED": 409,
    "CONFIRMATION_ALREADY_CONSUMED": 409,
    "CONFIRMATION_EXPIRED": 409,
    "CONFIRM_NOT_ALLOWED": 409,
    "CANCEL_NOT_ALLOWED": 409,
    "INTENT_REVISION_CONFLICT": 409,
    "OBJECT_OR_AUTHORITY_DRIFT": 409,
    "COMPUTE_QUOTE_EXPIRED": 409,
    "APPROVAL_PENDING": 409,
    "INTENT_NOT_EXECUTABLE": 409,
    "DOMAIN_ADAPTER_NOT_AVAILABLE": 409,
    # ── [WO-B ② 2026-08-20] execute 真的跑到领域之后才可能出现的码 ──
    #    双向机械核对仍然成立:抛了必须登记、登记了必须有人抛
    #    (test_frozen_contract 的分母已扩到 services/xiaobang_publish_execute.py)。
    "SETTLEMENT_ACCOUNT_UNAVAILABLE": 503,   # 平台承担账户解析不出 → fail-closed
    "APPROVAL_HANDOFF_REQUIRED": 409,        # 组织计费未就绪 → 只能交负责人,绝不回落扣 actor
    "SETTLEMENT_HANDLE_INVALID": 500,        # 冻结没产出句柄 → 响亮失败,不接受零句柄的"成功"
    "COMPUTE_QUOTE_MISSING": 409,            # 报不出价就执行 = 让用户不知道花多少地花钱
    "PUBLISH_TARGET_MISSING": 409,           # 没指定发哪一篇
    "PUBLISH_CHANNEL_NOT_SELECTED": 409,     # 没选定账号 —— 猜一个会发到别人号上
    "PUBLISH_CHANNEL_NOT_ELIGIBLE": 409,     # 账号资格/频控不满足
    # ── [窗G 2026-08-24] POR-13 七维对账 fail-closed ──
    #    合同少绑了 POR-13 的某一维时,execute 宁可受控 503 也不放行 ——
    #    "换任一维仍能消费同一张回执"正是 §19 变异 109 的形态。
    #    503 不是 409:这不是用户做错了什么,是我们这边的绑定配置需要核对。
    "CONFIRMATION_BINDING_INCOMPLETE": 503,
    "PUBLISH_CONTENT_NOT_READY": 409,        # 没有 active revision
    "PUBLISH_ARTIFACT_NOT_READY": 409,       # 那一版的素材没准备好
    "PRICE_CHANGED": 409,                    # prepare 与 execute 之间价目变了
    "LEGAL_TERM_BLOCKED": 409,               # 广告法绝对化用语
    "EXECUTION_CONFLICT": 409,               # 同一回执被另一个请求用过
}

DEFAULT_QUOTE_TTL_SECONDS = 15 * 60          # 价格锁:短
DEFAULT_APPROVAL_WINDOW_SECONDS = 7 * 86400  # 审批窗口:长(可配)
DEFAULT_RECEIPT_TTL_SECONDS = 30 * 60


class IntentError(RuntimeError):
    """带稳定 error code 的协调层错误。端点按 ``http_status`` 转 HTTPException。"""

    def __init__(self, code: str, message: str, *, http_status: int = 409,
                 next_action: str = "", detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.next_action = next_action
        self.detail = dict(detail or {})

    def as_payload(self) -> dict:
        out = {
            "error_code": self.code,
            "message": self.message,
        }
        if self.next_action:
            out["next_action"] = self.next_action
        if self.detail:
            out.update(self.detail)
        return out


class IntentNotFound(IntentError):
    """🔴 统一 404,**不回显存在性**。

    跨租户对象、已撤权、猜测 opaque ID —— 三种情况必须长得一模一样。
    只读恢复端点(GET intent / status)也不能因为"只是读"就跳过对象级授权。
    """

    def __init__(self) -> None:
        super().__init__(
            "INTENT_NOT_FOUND", "这个操作找不到或你没有权限查看。",
            http_status=404, next_action="回到小榜重新开始",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def new_opaque_id(prefix: str) -> str:
    """不可猜的公开 ID。持有它不构成授权(§9.1)。"""
    return "{0}_{1}".format(prefix, secrets.token_urlsafe(18))


# ── canonical input ───────────────────────────────────────────────────────
def canonical_input(
    *,
    operation_id: str,
    operation_version: int,
    request_schema_version: str,
    selection: Mapping[str, Any],
    allowlist: tuple[str, ...],
) -> dict:
    """按 schema allowlist **由服务端重建**入参,不接受客户端的多余字段。

    🔴 「服务端重建」不是修辞:客户端多塞的键在这里直接消失,所以它既进不了
       hash,也进不了后续任何一步。允许它进 hash 就等于允许客户端左右幂等边界。
    """
    rebuilt = {key: selection.get(key) for key in allowlist if selection.get(key) not in (None, "")}
    return {
        "operation_id": str(operation_id),
        "operation_version": int(operation_version),
        "request_schema_version": str(request_schema_version),
        "selection": rebuilt,
    }


def canonical_input_hash(payload: Mapping[str, Any]) -> str:
    return _sha256(payload)


def content_hash(payload: Any) -> str:
    """公开的 hash 口径。调用方一律用这一个,不要各自 json.dumps 一遍 ——
    两处口径不同,漂移判据就会永远为真或永远为假。"""
    return _sha256(payload)


# ── 值对象 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ActorBinding:
    """一次请求的身份/授权代际快照。每阶段由端点现取,不从 intent 行反读。"""

    actor_user_id: int
    tenant_owner_id: int
    payer_user_id: Optional[int] = None
    organization_id: Optional[int] = None
    membership_version: Optional[str] = None
    assignment_authority_version: Optional[str] = None
    approval_policy_version: Optional[str] = None
    permission_version: Optional[str] = None


@dataclass(frozen=True)
class ComputeQuote:
    amount: int
    pricing_version: str
    unit: str = "算力"
    ttl_seconds: int = DEFAULT_QUOTE_TTL_SECONDS

    @property
    def quote_hash(self) -> str:
        return _sha256({
            "amount": int(self.amount),
            "unit": self.unit,
            "pricing_version": self.pricing_version,
        })


# ── prepare ───────────────────────────────────────────────────────────────
def prepare_intent(
    cursor,
    *,
    entry_operation_id: str,
    contract: CommandContract,
    registry_version: str,
    actor: ActorBinding,
    prepare_request_id: str,
    canonical: Mapping[str, Any],
    payload_hash: str,
    object_manifest_hash: str,
    quote: Optional[ComputeQuote],
    preview: Mapping[str, Any],
    reason_facts: list,
    interaction_id: Optional[str] = None,
    approval_window_seconds: int = DEFAULT_APPROVAL_WINDOW_SECONDS,
) -> tuple[dict, bool]:
    """原子 claim。返回 ``(intent_row, created)``。

    🔴 **零业务写**的准确口径(规格 §10.2 · P1-7):不写 wallet / order / task /
       domain 表。本函数对意图协调表的这一次 upsert **不计入**业务写,但它受
       同等的幂等/原子性判据约束 —— 所以它走的是唯一约束,不是先查后插。
    """
    if not str(prepare_request_id or "").strip():
        raise IntentError("PREPARE_REQUEST_ID_REQUIRED", "缺少稳定的请求标识。",
                          http_status=400, next_action="重新发起")

    input_hash = canonical_input_hash(canonical)
    now = _now()
    quote_expires_at = (
        now + timedelta(seconds=quote.ttl_seconds) if quote is not None else None
    )
    approval_expires_at = now + timedelta(seconds=approval_window_seconds)
    intent_id = new_opaque_id("xint")
    confirmation_mode = SIDE_EFFECT_TO_CONFIRMATION[contract.side_effect]

    cursor.execute(
        """
        INSERT INTO xiaobang_operation_intents (
            intent_id, tenant_owner_id, actor_user_id, payer_user_id,
            organization_id, membership_version, assignment_authority_version,
            approval_policy_version, permission_version,
            operation_id, operation_version, registry_version,
            request_schema_version, response_schema_version,
            side_effect, confirmation_mode,
            interaction_id, prepare_request_id, canonical_input_hash,
            payload_hash, object_manifest_hash, compute_quote_hash,
            pricing_version, compute_quote_amount, compute_quote_unit,
            compute_quote_expires_at, approval_window_expires_at,
            intent_state, intent_revision, preview, reason_facts
        ) VALUES (
            %s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,
            %s,%s,%s,
            %s,%s,
            %s,%s,
            %s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,
            %s,%s,
            %s,1,%s::jsonb,%s::jsonb
        )
        ON CONFLICT (tenant_owner_id, operation_id, prepare_request_id) DO NOTHING
        RETURNING *
        """,
        (
            intent_id, int(actor.tenant_owner_id), int(actor.actor_user_id),
            actor.payer_user_id,
            actor.organization_id, actor.membership_version,
            actor.assignment_authority_version,
            actor.approval_policy_version, actor.permission_version,
            str(entry_operation_id), 1, str(registry_version),
            contract.request_schema_version, contract.response_schema_version,
            contract.side_effect, confirmation_mode,
            interaction_id, str(prepare_request_id), input_hash,
            str(payload_hash), str(object_manifest_hash),
            quote.quote_hash if quote is not None else None,
            quote.pricing_version if quote is not None else None,
            int(quote.amount) if quote is not None else None,
            quote.unit if quote is not None else "算力",
            quote_expires_at, approval_expires_at,
            STATE_PREPARED,
            json.dumps(dict(preview), ensure_ascii=False),
            json.dumps(list(reason_facts), ensure_ascii=False),
        ),
    )
    row = cursor.fetchone()
    if row is not None:
        return dict(row), True

    # 冲突 = 同一个 (tenant, operation, prepare_request_id) 已经存在。
    # 取行时加 FOR UPDATE:并发的两个 prepare 在这里排队,不会各读到旧快照。
    cursor.execute(
        """
        SELECT * FROM xiaobang_operation_intents
         WHERE tenant_owner_id=%s AND operation_id=%s AND prepare_request_id=%s
         FOR UPDATE
        """,
        (int(actor.tenant_owner_id), str(entry_operation_id), str(prepare_request_id)),
    )
    existing = cursor.fetchone()
    if existing is None:
        # 唯一键冲突了却读不到行 —— 状态未知。不猜、不重建,让调用方重试。
        raise IntentError(
            "INTENT_STATE_UNAVAILABLE", "这次准备的状态暂时确认不了,请稍后重试。",
            http_status=503, next_action="重试",
        )
    existing = dict(existing)
    if str(existing.get("canonical_input_hash") or "") != input_hash:
        raise IntentError(
            "PREPARE_REQUEST_ID_CONFLICT",
            "这个请求标识已经用在另一组选择上了,请重新准备。",
            http_status=409, next_action="重新准备",
        )
    if int(existing.get("actor_user_id") or 0) != int(actor.actor_user_id):
        # 同 key 不同人 = 猜到了别人的 prepare_request_id。不回显存在性。
        raise IntentNotFound()
    return existing, False


# ── 读取 / 恢复 ───────────────────────────────────────────────────────────
def load_intent(cursor, *, intent_id: str, actor: ActorBinding,
                for_update: bool = False) -> dict:
    """按当前身份取 intent。取不到、不属于本人、跨租户 —— 统一 404。"""
    cursor.execute(
        "SELECT * FROM xiaobang_operation_intents WHERE intent_id=%s"
        + (" FOR UPDATE" if for_update else ""),
        (str(intent_id or ""),),
    )
    row = cursor.fetchone()
    if row is None:
        raise IntentNotFound()
    row = dict(row)
    if int(row["actor_user_id"]) != int(actor.actor_user_id):
        raise IntentNotFound()
    if int(row["tenant_owner_id"]) != int(actor.tenant_owner_id):
        raise IntentNotFound()
    return row


def quote_expired(row: Mapping[str, Any], *, now: Optional[datetime] = None) -> bool:
    """价格锁是否到期。**只**代表要重新报价。"""
    expires = row.get("compute_quote_expires_at")
    return expires is not None and (now or _now()) >= expires


def approval_window_expired(row: Mapping[str, Any], *, now: Optional[datetime] = None) -> bool:
    """审批窗口是否到期。approval_pending 的失效判据用**这一只**钟。"""
    expires = row.get("approval_window_expires_at")
    return expires is not None and (now or _now()) >= expires


def drift_reason(row: Mapping[str, Any], *, actor: ActorBinding,
                 payload_hash: str, object_manifest_hash: str,
                 compute_quote_hash: Optional[str]) -> Optional[str]:
    """确认/执行前的逐项漂移复核(§10.3 十一项 bind 的复核面)。

    返回漂移原因(``None`` = 没漂)。刻意逐项返回而不是一个布尔:
    用户要能看到「是内容变了还是价格变了」,两者的下一步动作不一样。
    """
    if str(row.get("payload_hash") or "") != str(payload_hash or ""):
        return "payload_hash"
    if str(row.get("object_manifest_hash") or "") != str(object_manifest_hash or ""):
        return "object_manifest_hash"
    existing_quote = row.get("compute_quote_hash")
    if existing_quote and str(existing_quote) != str(compute_quote_hash or ""):
        return "compute_quote_hash"
    for column, value in (
        ("payer_user_id", actor.payer_user_id),
        ("organization_id", actor.organization_id),
        ("membership_version", actor.membership_version),
        ("assignment_authority_version", actor.assignment_authority_version),
        ("approval_policy_version", actor.approval_policy_version),
        ("permission_version", actor.permission_version),
    ):
        if row.get(column) != value:
            return column
    return None


# ── 状态迁移(CAS)─────────────────────────────────────────────────────────
def _cas_transition(cursor, *, intent_id: str, expected_revision: int,
                    new_state: str, extra_sets: str = "",
                    extra_params: tuple = ()) -> Optional[dict]:
    if new_state in _FORBIDDEN_STATES or new_state not in INTENT_STATES:
        raise IntentError("INVALID_INTENT_STATE", "非法的意图状态。", http_status=500)
    sql = (
        "UPDATE xiaobang_operation_intents "
        "SET intent_state=%s, intent_revision=intent_revision+1, updated_at=NOW()"
        + (", " + extra_sets if extra_sets else "")
        + " WHERE intent_id=%s AND intent_revision=%s RETURNING *"
    )
    cursor.execute(sql, (new_state, *extra_params, str(intent_id), int(expected_revision)))
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def mark_confirmed(cursor, *, intent_id: str, expected_revision: int) -> Optional[dict]:
    """``prepared|awaiting_confirmation → confirmed``(CAS)。"""
    return _cas_transition(
        cursor, intent_id=intent_id, expected_revision=expected_revision,
        new_state=STATE_CONFIRMED,
    )


def mark_awaiting_confirmation(cursor, *, intent_id: str, expected_revision: int) -> Optional[dict]:
    """``prepared → awaiting_confirmation``。

    页面就绪回调驱动。**不产生业务写,不动算力** —— 它只是把「已生成预览」
    与「已展示给用户等待点击」两个协调态分开记(§10.2 · P2-7)。
    """
    return _cas_transition(
        cursor, intent_id=intent_id, expected_revision=expected_revision,
        new_state=STATE_AWAITING_CONFIRMATION,
    )


def link_execution(cursor, *, intent_id: str, expected_revision: int,
                   execution_id: str, execution_request_id: str,
                   domain_ref: Mapping[str, Any]) -> Optional[dict]:
    """``confirmed|executable → execution_linked``(CAS)。

    🔴 [WO-B ② 2026-08-20] 协调层只记「执行请求**关联到了**哪个领域对象」,
       **不复制领域终态**:``execution_linked`` 之后这条 intent 的领域进度
       一律由 ``domain_projection`` 现算(``status_projection`` 从 ``domain_ref`` 投)。
       规格 §10.5 的二选一裁定 —— 所以枚举里没有 ``executing``。

    🔴 与 cancel 共用同一把 CAS(``intent_revision``)。于是
       「取消」与「执行」并发时**只有一方能成功**:另一方拿到零行 →
       调用方翻 409 ``INTENT_REVISION_CONFLICT``,而不是两边都以为自己赢了。

    返回 ``None`` 表示 CAS 落败(revision 已被别人推进)。
    """
    return _cas_transition(
        cursor, intent_id=intent_id, expected_revision=expected_revision,
        new_state=STATE_EXECUTION_LINKED,
        extra_sets=("execution_id=%s, execution_request_id=%s, "
                    "domain_ref=%s::jsonb, last_observed_at=NOW()"),
        extra_params=(str(execution_id), str(execution_request_id),
                      json.dumps(dict(domain_ref or {}), ensure_ascii=False)),
    )


def cancel_intent(cursor, *, intent_id: str, expected_revision: int,
                  cancel_request_id: str) -> dict:
    """CAS 撤销。cancel 与 execute 并发只有一方能成功(§10.3.1)。"""
    cursor.execute(
        "SELECT * FROM xiaobang_operation_intents WHERE intent_id=%s FOR UPDATE",
        (str(intent_id),),
    )
    row = cursor.fetchone()
    if row is None:
        raise IntentNotFound()
    row = dict(row)
    if row["intent_state"] == STATE_CANCELLED:
        # 同 cancel_request_id 重放 → 同结果;异 request_id → 仍是已取消的事实。
        return row
    if row["intent_state"] not in CANCELLABLE_STATES:
        raise IntentError(
            "CANCEL_NOT_ALLOWED",
            "这一步已经开始了,不能再普通取消。",
            http_status=409,
            next_action="查看进度",
            detail={"intent_state": row["intent_state"]},
        )
    updated = _cas_transition(
        cursor, intent_id=intent_id, expected_revision=expected_revision,
        new_state=STATE_CANCELLED,
        extra_sets="cancel_request_id=%s", extra_params=(str(cancel_request_id),),
    )
    if updated is None:
        raise IntentError(
            "INTENT_REVISION_CONFLICT", "这个操作刚刚有变化,请刷新后再试。",
            http_status=409, next_action="刷新",
        )
    return updated


# ── 确认回执 ──────────────────────────────────────────────────────────────
def issue_confirmation_receipt(
    cursor,
    *,
    intent_row: Mapping[str, Any],
    actor: ActorBinding,
    challenge: str,
    user_agent: str = "",
    ttl_seconds: int = DEFAULT_RECEIPT_TTL_SECONDS,
) -> tuple[dict, bool]:
    """签发/重放确认回执。返回 ``(receipt_row, created)``。

    🔴 同 ``(intent_id, intent_revision)`` 只允许一张回执:唯一索引兜底,
       重放拿回**同一张**而不是第二张。回执不是 bearer 凭证 —— 客户端不携带、
       不保管;execute 在事务内按 intent_id 锁定并消费它。
    """
    if not str(challenge or "").strip():
        raise IntentError(
            "USER_ACTION_CHALLENGE_REQUIRED",
            "需要在页面上真实点击一次确认。",
            http_status=400, next_action="打开核对并确认",
        )
    receipt_id = new_opaque_id("xcr")
    cursor.execute(
        """
        INSERT INTO xiaobang_confirmation_receipts (
            receipt_id, intent_id, intent_revision, actor_user_id,
            bound_payload_hash, bound_object_manifest_hash, bound_compute_quote_hash,
            challenge_hash, user_agent_hash, receipt_expires_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (intent_id, intent_revision) DO NOTHING
        RETURNING *
        """,
        (
            receipt_id, str(intent_row["intent_id"]), int(intent_row["intent_revision"]),
            int(actor.actor_user_id),
            str(intent_row["payload_hash"]), str(intent_row["object_manifest_hash"]),
            intent_row.get("compute_quote_hash"),
            _sha256(str(challenge)),
            _sha256(str(user_agent)) if user_agent else None,
            _now() + timedelta(seconds=ttl_seconds),
        ),
    )
    row = cursor.fetchone()
    if row is not None:
        return dict(row), True
    cursor.execute(
        "SELECT * FROM xiaobang_confirmation_receipts "
        " WHERE intent_id=%s AND intent_revision=%s FOR UPDATE",
        (str(intent_row["intent_id"]), int(intent_row["intent_revision"])),
    )
    existing = cursor.fetchone()
    if existing is None:
        raise IntentError(
            "RECEIPT_STATE_UNAVAILABLE", "确认状态暂时确认不了,请稍后重试。",
            http_status=503, next_action="重试",
        )
    existing = dict(existing)
    if int(existing["actor_user_id"]) != int(actor.actor_user_id):
        raise IntentNotFound()
    return existing, False


def rebind_receipt_to_revision(cursor, *, intent_id: str, from_revision: int,
                               to_revision: int) -> None:
    """把确认回执改绑到 **confirm 之后**的 revision。

    ## 🔴 这修的是一个真洞:confirm → execute 差**一个 revision**

    ``issue_confirmation_receipt`` 用的是**确认那一刻**的 ``intent_revision``(记作 N),
    紧接着 ``mark_confirmed`` 走 CAS,``intent_revision`` 变成 N+1。
    而 ``execute`` 拿到的是 N+1,它按 ``(intent_id, N+1)`` 去找回执 —— **永远找不到**。

    也就是说在本函数之前,``external`` 档**没有任何一条路能走到 execute**:
    正确确认过的请求也会拿到 ``CONFIRMATION_REQUIRED``。
    这个洞一直没被发现,是因为**从来没有一条判据把 confirm 与 execute 连起来跑**
    (execute 那一侧原来在更早的地方就 409 了,五阶段端到端从没真跑过)。

    修法选「改绑」而不是「execute 去找 N-1」:
      · 回执的语义是「用户对**这个**意图的这一版内容点了确认」——
        confirm 的 CAS 只改状态与 revision,**内容一个字没变**,
        所以把回执挪到新 revision 上是如实描述,不是放宽;
      · 反过来让 execute 去猜 N-1 是在判据里写死一个偏移量,
        以后任何一次多余的 CAS 都会把它悄悄弄错。

    唯一索引 ``(intent_id, intent_revision)`` 仍然成立:一个 revision 一张回执。
    若此后 intent 因为别的原因又推进了 revision,回执就对不上了 ——
    那正是我们要的:内容/权限变过就必须重新确认。
    """
    cursor.execute(
        """
        UPDATE xiaobang_confirmation_receipts
           SET intent_revision = %s
         WHERE intent_id = %s AND intent_revision = %s AND consumed_at IS NULL
        """,
        (int(to_revision), str(intent_id), int(from_revision)),
    )


def consume_confirmation_receipt(cursor, *, intent_id: str, intent_revision: int,
                                 execution_request_id: str) -> dict:
    """事务内单次消费。

    条件 UPDATE(``WHERE consumed_at IS NULL``)是抢占,不是「先查再改」——
    参照 ``services/geo_douyin/settlement.py`` 的条件 UPDATE 先例。
    同一个 ``execution_request_id`` 重放会拿回同一张已消费回执(不是第二次消费)。
    """
    cursor.execute(
        """
        UPDATE xiaobang_confirmation_receipts
           SET consumed_at=NOW(), consumed_by_execution_request_id=%s
         WHERE intent_id=%s AND intent_revision=%s
           AND consumed_at IS NULL
           AND receipt_expires_at > NOW()
        RETURNING *
        """,
        (str(execution_request_id), str(intent_id), int(intent_revision)),
    )
    row = cursor.fetchone()
    if row is not None:
        return dict(row)
    cursor.execute(
        "SELECT * FROM xiaobang_confirmation_receipts "
        " WHERE intent_id=%s AND intent_revision=%s",
        (str(intent_id), int(intent_revision)),
    )
    existing = cursor.fetchone()
    if existing is None:
        raise IntentError(
            "CONFIRMATION_REQUIRED", "还没有你的确认,不能执行。",
            http_status=409, next_action="打开核对并确认",
        )
    existing = dict(existing)
    if existing.get("consumed_by_execution_request_id") == str(execution_request_id):
        return existing  # 同一次执行请求的重放:返回同一结果,不重复消费。
    if existing.get("consumed_at") is not None:
        raise IntentError(
            "CONFIRMATION_ALREADY_CONSUMED", "这次确认已经用过了。",
            http_status=409, next_action="查看进度",
        )
    raise IntentError(
        "CONFIRMATION_EXPIRED", "确认已过期,请重新核对。",
        http_status=409, next_action="重新准备",
    )


# ── 状态投影(§10.5)───────────────────────────────────────────────────────
def status_projection(row: Mapping[str, Any]) -> dict:
    """把协调态 / 领域态 / 资金态 / 外部态拆成四条投影,**不压成一列**。

    🔴 unknown / reconciling 不许被压平成 failed 或 succeeded —— 为了 UI 好看
       把未知说成失败,等于替用户下了一个我们并不知道的结论。
    """
    domain = dict(row.get("domain_ref") or {})
    external_default = (
        "not_started" if row.get("side_effect") == SIDE_EFFECT_EXTERNAL
        else "not_applicable"
    )
    return {
        "intent_state": row["intent_state"],
        "intent_revision": int(row["intent_revision"]),
        "domain_projection": {
            "state": _freeze(domain.get("state"), DOMAIN_STATES,
                             "unknown", default="not_started"),
            "reference": domain.get("reference"),
        },
        "settlement_projection": {
            # 资金态认不出来时落 quarantined 而不是 none:none 是"没花过钱"的
            # 断言,而我们此刻并不知道花没花 —— 说成 none 就是替资金下结论。
            "state": _freeze(domain.get("settlement_state"), SETTLEMENT_STATES,
                             "quarantined", default="none"),
            "amount": row.get("compute_quote_amount"),
            "unit": row.get("compute_quote_unit") or "算力",
        },
        "external_projection": {
            "state": _freeze(domain.get("external_state"), EXTERNAL_STATES,
                             "unknown", default=external_default),
        },
        "last_observed_at": row.get("last_observed_at"),
    }
