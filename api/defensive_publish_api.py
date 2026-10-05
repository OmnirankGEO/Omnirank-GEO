"""防御型 GEO v2 façade · 发布五步 + 资金全序(规格 §11.3 / §12 / §15.7)。

判据:MED-09/10/12/13/14/16/17/19/20/21、FIN-01..16、API-02/03/04、
UI-34/35/36、POR-06/07/14/16。

═══════════════════════════════════════════════════════════════════════
🔴 路由前缀为什么不是规格字面的 ``/api/publish/decision-snapshots/*``
═══════════════════════════════════════════════════════════════════════
census 2026-08-21 在代码尖实测到一处**真实碰撞**:
``api/publish_api.py:1168`` 已有

    @router.get("/decision-snapshots/{snapshot_id}")
    async def get_decision_snapshot(snapshot_id: int, request: Request)

——参数类型是 ``int``。Starlette 的路由匹配发生在**校验之前**,
``{snapshot_id}`` 的正则是 ``[^/]+``,所以:

  · 我先注册 → legacy 的 int GET 被我的 str GET 遮蔽,存量发布链当场变形;
  · legacy 先注册 → 我的 ``/decision-snapshots/preview`` 会先命中 legacy 的
    ``{snapshot_id}:int`` 然后 422,新链根本走不通。

两条都违反「防御改动不许打断存量」(Owner 2026-08-21 发车门第 1 条)。
所以本 façade 挂在 ``/api/defensive-geo/publish/**``,与窗A/窗B 同族前缀,
且与 legacy ``/api/publish/**`` **零重叠**。DTO 形状、状态码、admission 分支
逐条按 §15.7 实现 —— 变的只有 URL 前缀,且这一处偏差在交付单里显式挂号。

═══════════════════════════════════════════════════════════════════════
🔴 G-4:「response 多返回一键 → 受控失败而非裸 500」
═══════════════════════════════════════════════════════════════════════
census 实测:本仓**没有**全局 ``ResponseValidationError`` handler
(server.py 只注册了 OrganizationError 与仅对 organization 路径生效的
RequestValidationError)。所以靠 FastAPI 的 ``response_model`` 会得到裸 500。

本文件的做法:每个 handler 走 :func:`_respond` —— 在**自己的 try 里**做
``model_validate``,失败时返回 SafeError ``INTERNAL_ERROR`` + ``publicErrorRef``。
判据用一个「多塞一个键」的正样本打这条路径,拿到的必须是受控信封。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.defensive_geo.copy_registry import (
    assert_public_copy_clean,
    try_user_label,
)
from services.defensive_geo.publish import decision_snapshot as _ds
from services.defensive_geo.publish import legal_gate as _legal
from services.defensive_geo.publish import media_identity as _mi
from services.defensive_geo.publish import publish_funding as _funding
from services.defensive_geo.publish import publish_settlement as _settle
from services.defensive_geo.publish import provider_transport as _transport
from services.defensive_geo.publish import publish_slot as _slot
from services.defensive_geo.publish import settlement_review as _review
from services.defensive_geo.publish import execution_budget_policy as _budget_policy
from services.defensive_geo.publish import store as _store

logger = logging.getLogger("GEO-DefGeoPublishAPI")

router = APIRouter(prefix="/api/defensive-geo/publish", tags=["防御型 GEO · 发布 v2 façade"])

#: decision preview 有效期。服务端写死 —— 客户端自报等于自己给自己延期。
_PREVIEW_TTL_MINUTES = 30


# ══════════════════════════════════════════════════════════════════════════
# §15.8 SafeError —— 唯一出口
# ══════════════════════════════════════════════════════════════════════════
#: code → (HTTP, retryable)。**闭表**;不在表里一律走 500 受控信封。
_ERROR_TABLE: dict[str, tuple[int, bool]] = {
    "VALIDATION_ERROR": (422, False),
    "OBJECT_NOT_FOUND": (404, False),
    "FORBIDDEN": (403, False),
    "PREVIEW_EXPIRED": (409, False),
    "PREVIEW_ALREADY_CONSUMED": (409, False),
    "SNAPSHOT_CHANGED": (409, False),
    "PRICE_CHANGED": (409, False),
    "PUBLISH_DECISION_NOT_CONFIRMABLE": (409, False),
    "CUSTOMER_COMMITMENT_RECONFIRMATION_REQUIRED": (409, False),
    "IDEMPOTENCY_CONFLICT": (409, False),
    "INSUFFICIENT_POINTS": (409, False),
    "APPROVAL_REQUIRED": (403, False),
    "APPROVAL_REJECTED": (403, False),
    "LEGAL_RULE_HIT": (409, False),
    "ADMISSION_UNAVAILABLE": (503, True),
    # [终审 P0-1 2026-08-23] 入口关闭。403 而不是 501/500:
    # 501/500 会被前端与告警当成"我们坏了"(retryable 语义、进错误率),
    # 而这不是故障 —— 是这条链**本来就没开放**。403 + typed 信封让她看到
    # 一句人话,让监控看到一个确定的、非故障的拒绝。
    "PUBLISH_ENTRY_CLOSED": (403, False),
    # [包E 2026-08-24 · 终审 P1-2 的信封那一半]
    # 「这一单的 provider 执行预算还没签发」过去走 FundingError → 兜底 except
    # → 受控 500。500 说的是"我们坏了",而事实是"这一单的准备工作还差一步"。
    # 409 + retryable=True:她**什么都不用改**,等物化器跑完再点一次就行。
    "EXECUTION_BUDGET_NOT_READY": (409, True),
    # 外发通道暂时不可用。503 与 ADMISSION_UNAVAILABLE 同族,但 code 分开 ——
    # 「没档期」和「通道不通」是两件事,压成一个 code 会让运维分不清该找谁。
    #
    # 🔴 原注留档 + [B-3 2026-08-25 订正]:这里原来写「**刻意不做成 confirm
    #    前置闸**」,理由是「外发通道的登录态会过期,拿它当 confirm 的前置 =
    #    一个 cookie 过期就让整条销售动作 503」(Owner 第 0 条:一切阻塞商业
    #    行为的所谓合规行为都是在犯傻)。那个顾虑本身成立,但**它不适用于
    #    现役这个探针**:``provider_transport._mhz_readiness`` 只问
    #    「``phpsessid`` 这一行**在不在**」,不问它有没有过期 ——
    #    过期的 cookie 仍然是一个非空字符串,``ready`` 仍然是 True。
    #    ``ready=False`` 只在两种情况成立:transport 根本没登记,
    #    或者外发通道**从来没配过 / 配置读不出来**。那两种下冻钱
    #    = 冻一笔**可证不可能被派发**的钱,不是"先拦住她"。
    #    所以 confirm 侧现在按真实 readiness 走 admission
    #    (``capability_unavailable`` → ``ADMISSION_UNAVAILABLE`` 503 retryable),
    #    而"配过但这次不通"仍然走收敛路径(reconciler
    #    ``_release_never_dispatched``)—— 两条路各管各的那一格,没有合并。
    "PUBLISH_CHANNEL_UNAVAILABLE": (503, True),
    "INTERNAL_ERROR": (500, False),
}

#: details 白名单 —— 开放 details 等于给 raw detail 开后门(§19 变异 156)。
_DETAIL_KEYS = frozenset({
    "decisionSnapshotId", "expectedHash", "expectedVersion",
    "successorSnapshotId", "successorSnapshotHash", "supersessionKind",
    "publishCommandId", "statusUrl", "publishSlotId",
    "requiredExactPoints", "remainingPoints", "deltaPoints",
    "ruleId", "ruleVersion", "passageRef", "publicErrorRef",
})


def _safe_error(
    code: str, *, reason_key: str | None = None,
    next_action: dict | None = None, details: dict | None = None,
) -> HTTPException:
    status, retryable = _ERROR_TABLE.get(code, _ERROR_TABLE["INTERNAL_ERROR"])
    payload: dict[str, Any] = {"code": code, "retryable": retryable}
    if reason_key:
        explanation = try_user_label("reason", reason_key)
        if explanation:
            payload["publicExplanation"] = assert_public_copy_clean(
                explanation, field=f"error.{code}.publicExplanation",
            )
    if next_action:
        payload["nextAction"] = next_action
    if details:
        bad = set(details) - _DETAIL_KEYS
        if bad:
            raise RuntimeError(f"SafeError details 出现非白名单键 {sorted(bad)}")
        payload["details"] = details
    return HTTPException(status_code=status, detail=payload)


# ══════════════════════════════════════════════════════════════════════════
# [终审 P0-1 2026-08-23 关 → 包E 2026-08-24 重开] 发布链客户入口
# ══════════════════════════════════════════════════════════════════════════
#: 客户侧发布入口开没开。**当前 = 开**。
#:
#: ── 当初为什么关(留着,因为它是重开条件的定义)────────────────────────
#: 执行侧没开发完。census 机械枚举(排 tests 后)——
#:   · ``publish/publish_outbox.dispatch_once``  只有定义处
#:   · ``publish/store.claim_outbox``            只有定义处
#:   · ``publish/reconciler.reconcile_once``     只有定义处
#: 调度器里跟发布有关的只有只读告警(零状态推进),而 confirm 是**真冻钱**的。
#: 客户视角 = 确认发布 → 算力被冻住 → 什么都没发生 → 12 小时后钱莫名退回。
#:
#: ── 重开的前置条件,逐条对上了才翻这个常量 ──────────────────────────────
#: 当初写死的重开条件逐字是:「上面三个函数至少各有一个生产调用点,
#: 且调度器里有推进(不只是告警)」。包E 逐条兑现:
#:   · ``claim_outbox``   ← ``publish_worker.dispatch_pending``
#:   · ``dispatch_once``  ← ``publish_worker._dispatch_one_row``
#:   · ``reconcile_once`` ← ``publish_worker.reconcile_tick``
#:   · 调度器:``defgeo_publish_dispatch`` / ``defgeo_publish_reconcile`` /
#:     ``defgeo_activation_materialize`` 三条 **推进** job(见 api/scheduler.py
#:     的 register_v32_core_tasks —— 无条件段,不是 gated 的 setup_schedule);
#:   · 顺带把 P1-2(preview 必 500)与 P1-4(归属)修了 —— 见下面
#:     ``_assert_accepted_snapshot_access`` 与 ``EXECUTION_BUDGET_NOT_READY``。
#:
#: 🔴 这一条**不是**布尔开关那么简单:判据
#:    ``test_00b_entry_is_open_only_with_real_executor_wiring`` 把「常量为 True」
#:    与「三个函数各有生产调用点 + 三条 job 真的注册得上」绑在同一条判据里。
#:    也就是说:谁把执行器接线拆掉,这一条会红;谁在没有接线时把常量翻 True,
#:    同一条也会红。开关与它的前提条件从此不能各自漂移。
_CUSTOMER_PUBLISH_ENTRY_OPEN = True


def _assert_customer_publish_entry_open() -> None:
    """会冻钱的客户侧发布端点在**第一行**调它。

    放第一行是有意的:后面一行都不许跑 —— 不连库、不取 tenant、不算 slot。
    "零冻结"因此可以由阅读证明,而不是靠"应该走不到那里"。
    单点定义:三个端点共用这一个谓词,不各写一份(同一谓词写两处,
    必有一处没人验)。
    """
    if not _CUSTOMER_PUBLISH_ENTRY_OPEN:
        raise _safe_error("PUBLISH_ENTRY_CLOSED", reason_key="publish_entry_closed")


def _internal_error(ref: str) -> HTTPException:
    """G-4 的受控 500。``publicErrorRef`` 让支持侧能对上日志,
    但不返回异常类型/stack/SQL/host/供应商(POR-14)。"""
    return _safe_error("INTERNAL_ERROR", details={"publicErrorRef": ref})


def _respond(model: type[BaseModel], payload: Mapping[str, Any]) -> dict[str, Any]:
    """G-4:响应先过 strict 模型;多一个键 → **受控失败**,不是裸 500。"""
    try:
        return model.model_validate(payload).model_dump(by_alias=True)
    except ValidationError as exc:
        ref = uuid.uuid4().hex[:16]
        logger.error("[defgeo-publish] 响应契约违约 ref=%s: %s", ref, exc)
        raise _internal_error(ref) from None


# ══════════════════════════════════════════════════════════════════════════
# 鉴权 —— 归属每请求现做
# ══════════════════════════════════════════════════════════════════════════
def _tenant(request: Request) -> int:
    user = getattr(request.state, "user", None)
    if not user or not user.get("user_id"):
        raise _safe_error("FORBIDDEN")
    return int(user["user_id"])


def _is_admin(request: Request) -> bool:
    user = getattr(request.state, "user", None) or {}
    return bool(user.get("is_admin"))


def _require_admin(request: Request) -> int:
    if not _is_admin(request):
        # 与 26+ 现役 admin router 同形(census 实测:各自手写 _require_admin)。
        raise _safe_error("FORBIDDEN")
    return _tenant(request)


def _label(kind: str) -> str:
    text = try_user_label("action", kind) or _review.ACTION_LABELS.get(kind) or ""
    return assert_public_copy_clean(text, field=f"action.{kind}") if text else ""


# ══════════════════════════════════════════════════════════════════════════
# DTO —— camelCase wire,请求与响应都 extra='forbid'
# ══════════════════════════════════════════════════════════════════════════
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PublishDecisionPreviewRequest(_Strict):
    plan_item_key: str = Field(alias="planItemKey", min_length=1, max_length=200)
    article_revision_id: str = Field(alias="articleRevisionId", min_length=1, max_length=120)
    expected_article_hash: str = Field(alias="expectedArticleHash", min_length=1, max_length=120)
    # 服务端从血缘派生 scope,但需要知道是哪个已接受合同(§12.2:客户端不能
    # 通过提交 tenant/service id 选择归属 —— 这两个是**对象引用**不是归属选择,
    # 服务端仍会逐个重验它们属于本 tenant)。
    accepted_snapshot_id: int = Field(alias="acceptedSnapshotId", ge=1)
    service_projection_id: str = Field(alias="serviceProjectionId", min_length=1, max_length=120)
    brand_id: int = Field(alias="brandId", ge=1)
    #: 🔴 法律修复续链(§12.2 逐字:「法律 failed_no_effect + released + legalRuleHit:
    #: 只能先局部修复,随后以**新 article revision/decision/confirm** 且**显式绑定
    #: parent legal command**」)。
    #: 不给这个字段,法律格就是一条死路:修复 CTA 指向改稿,改完却没有任何入口
    #: 能把新稿变成新的媒体决策 —— 那正是 §0.5.6 铁律要消灭的形态。
    #: 它**不是**绕过法律门的后门:服务端会验 ① parent 确实是本 slot 的法律格、
    #: ② 新 article revision 与 parent 的**不同**(不改稿不许续);
    #: 而且外调前的法律门照样跑一次(catalog 是热加载的)。
    parent_legal_command_id: str | None = Field(
        default=None, alias="parentLegalCommandId", max_length=64)


class ConfirmBody(_Strict):
    expected_hash: str = Field(alias="expectedHash", min_length=64, max_length=64)
    expected_version: int = Field(alias="expectedVersion", ge=1)


class OverrideBody(ConfirmBody):
    selected_public_media_option_id: str = Field(
        alias="selectedPublicMediaOptionId", min_length=8, max_length=128)
    actor_reason: str = Field(alias="actorReason", min_length=4, max_length=500)


class RetryChildBody(_Strict):
    expected_status_version: int = Field(alias="expectedStatusVersion", ge=1)
    expected_command_hash: str = Field(alias="expectedCommandHash", min_length=64, max_length=64)


class AdminReviewBody(_Strict):
    action: Literal["admin_commit", "admin_release", "admin_hold"]
    reason: str | None = Field(default=None, max_length=1000)


class ProviderEvidenceBody(_Strict):
    evidence_kind: Literal["screenshot_url", "order_number", "contact_log", "other"] = Field(
        alias="evidenceKind")
    evidence_text: str = Field(alias="evidenceText", min_length=1, max_length=2000)


class _Envelope(_Strict):
    """所有 2xx 响应的外壳。``model_dump`` 之后直接返回,不再包一层。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PreviewAdmissionResponse(_Envelope):
    slot_admission: str = Field(alias="slotAdmission")
    publish_slot_id: str = Field(alias="publishSlotId")
    slot_state: str | None = Field(default=None, alias="slotState")
    snapshot_response: dict[str, Any] | None = Field(default=None, alias="snapshotResponse")
    original_command_id: str | None = Field(default=None, alias="originalCommandId")
    status_url: str | None = Field(default=None, alias="statusUrl")
    legal_rule_hit: dict[str, Any] | None = Field(default=None, alias="legalRuleHit")
    replacement_policy_ref: str | None = Field(default=None, alias="replacementPolicyRef")
    next_action: dict[str, Any] = Field(alias="nextAction")


class SnapshotResponse(_Envelope):
    lifecycle: str
    snapshot: dict[str, Any]
    confirmability: dict[str, Any]
    budget_blockers: list[dict[str, Any]] = Field(alias="budgetBlockers")
    adjustment_options: list[dict[str, Any]] = Field(alias="adjustmentOptions")
    original_command_id: str | None = Field(default=None, alias="originalCommandId")
    status_url: str | None = Field(default=None, alias="statusUrl")
    supersession_kind: str | None = Field(default=None, alias="supersessionKind")
    superseded_by_snapshot_id: str | None = Field(default=None, alias="supersededBySnapshotId")
    superseded_by_snapshot_hash: str | None = Field(default=None, alias="supersededBySnapshotHash")


class ConfirmResponse(_Envelope):
    publish_command_id: str = Field(alias="publishCommandId")
    decision_snapshot_id: str = Field(alias="decisionSnapshotId")
    decision_snapshot_hash: str = Field(alias="decisionSnapshotHash")
    command_canonical_hash: str = Field(alias="commandCanonicalHash")
    exact_settlement_points: int = Field(alias="exactSettlementPoints")
    status_url: str = Field(alias="statusUrl")
    idempotent_replay: bool = Field(alias="idempotentReplay")
    command_state: str = Field(alias="commandState")
    command_reason: str | None = Field(default=None, alias="commandReason")
    funding_policy: str = Field(alias="fundingPolicy")
    funding_state: str = Field(alias="fundingState")
    funding_state_label: str = Field(alias="fundingStateLabel")
    next_action: dict[str, Any] = Field(alias="nextAction")
    item: dict[str, Any]


class CommandStatusResponse(_Envelope):
    publish_command_id: str = Field(alias="publishCommandId")
    decision_snapshot_id: str = Field(alias="decisionSnapshotId")
    decision_snapshot_hash: str = Field(alias="decisionSnapshotHash")
    command_canonical_hash: str = Field(alias="commandCanonicalHash")
    status_version: int = Field(alias="statusVersion")
    updated_at: str = Field(alias="updatedAt")
    command_state: str = Field(alias="commandState")
    command_state_label: str = Field(alias="commandStateLabel")
    reason: str | None = None
    funding_policy: str = Field(alias="fundingPolicy")
    funding_state: str = Field(alias="fundingState")
    funding_state_label: str = Field(alias="fundingStateLabel")
    next_action: dict[str, Any] | None = Field(default=None, alias="nextAction")
    item: dict[str, Any]


class ReviewQueueResponse(_Envelope):
    entries: list[dict[str, Any]]
    total_pending: int = Field(alias="totalPending")
    stale_over_days: int = Field(alias="staleOverDays")
    action_catalog: list[dict[str, Any]] = Field(alias="actionCatalog")


class ReviewActionResponse(_Envelope):
    publish_command_id: str = Field(alias="publishCommandId")
    action: str
    funding_state_before: str = Field(alias="fundingStateBefore")
    funding_state_after: str = Field(alias="fundingStateAfter")
    funding_state_label: str = Field(alias="fundingStateLabel")
    entry_id: int = Field(alias="entryId")


# ══════════════════════════════════════════════════════════════════════════
# 共用投影
# ══════════════════════════════════════════════════════════════════════════
def _status_url(command_id: str) -> str:
    return f"/api/defensive-geo/publish/commands/{command_id}"


def _funding_label(state: str) -> str:
    """🔴 先取**发布域**译文,取不到才回落诊断域。

    顺序不能反:诊断域的 ``frozen`` 是「算力已冻结(本次**体检**预留)」——
    在媒体发布确认页上,那句话是在对刚花钱的销售说错她买的是什么。
    """
    text = try_user_label("publish_funding_state", state) \
        or try_user_label("funding_state", state) or ""
    return assert_public_copy_clean(text, field="fundingStateLabel") if text else ""


def _run_state_label(state: str) -> str:
    text = try_user_label("publish_run_state", state) \
        or try_user_label("run_state", state) or ""
    return assert_public_copy_clean(text, field="commandStateLabel") if text else ""


def _reason_explanation(reason_code: str) -> str:
    """阻塞原因的人话。取不到**返回空串**,由 confirmability 决定不带这一格 ——
    绝不从 reasonCode 反推(反推就是自造文案,U-2 禁)。"""
    text = try_user_label("reason", reason_code) or ""
    return assert_public_copy_clean(text, field="publicExplanation") if text else ""


def _media_role_label(role: str) -> str:
    """媒体角色的人话。取不到**返回空串**,由前端渲染留白 ——
    绝不回落成角色枚举本身(裸串上屏 = U-1 红)。"""
    text = try_user_label("media_role", role) or ""
    return assert_public_copy_clean(text, field="mediaRoleLabel") if text else ""


def _recovery_action(kind: str, *, target: dict[str, Any], capability: str) -> dict[str, Any]:
    """唯一的 action 出口。

    🔴 ``capability`` 仍然由调用方传,但 :func:`action_registry.build` 会用
       registry 里登记的值**覆盖**它,并在两者不一致时抛。
       为什么不干脆不传:传了才能在这里做「调用方以为的」与「registry 说的」
       的对照 —— 不传就只剩 registry 一个信源,抄错 capability 这件事
       在判据里就没有对照物了。
    """
    from services.defensive_geo.publish import action_registry as _ar

    label = _label(kind)
    if not label:
        raise RuntimeError(f"nextAction {kind} 没有登记文案 —— 死 CTA 不许上屏")
    action = _ar.build(kind, label=label, target=target)
    if capability != action["capability"]:
        raise RuntimeError(
            f"action {kind} 的 capability 调用方写 {capability!r},"
            f"registry 是 {action['capability']!r} —— 两处不一致必有一处没人验"
        )
    return action


def _snapshot_response(
    row: Mapping[str, Any],
    *,
    blockers: list[dict[str, Any]],
    options: list[dict[str, Any]],
    approval_state: str,
    blocked_reason: str | None,
) -> dict[str, Any]:
    """把一行 snapshot 投影成 §15.7 的 ``PublishDecisionSnapshotResponse``。"""
    frozen = dict(row["frozen_payload"])
    lifecycle = str(row["lifecycle"])
    if lifecycle != "open":
        blockers, options = [], []            # InactiveSidecar
    confirm = _ds.confirmability(
        lifecycle=lifecycle,
        funding_policy=str(frozen["fundingPolicy"]),
        approval_requirement=str(frozen["approvalRequirement"]),
        approval_state=approval_state,
        blocked_reason=blocked_reason,
        target_ids={
            "plan_item": str(frozen["planItemKey"]),
            "publish_snapshot": str(row["decision_snapshot_id"]),
            "publish_command": str(row.get("consumed_command_id") or ""),
            "approval": str(frozen.get("executionBudgetSnapshotId") or ""),
            "service_projection": str(frozen["serviceProjectionId"]),
        },
        label_for=_label,
        explain_for=_reason_explanation,
    )
    _ds.assert_sidecar_consistent(confirm=confirm, blockers=blockers, options=options)
    _mi.assert_no_private_leak(frozen, field="snapshot")
    return {
        "lifecycle": lifecycle,
        "snapshot": frozen,
        "confirmability": confirm,
        "budgetBlockers": blockers,
        "adjustmentOptions": options,
        "originalCommandId": row.get("consumed_command_id"),
        "statusUrl": _status_url(str(row["consumed_command_id"]))
                     if row.get("consumed_command_id") else None,
        "supersessionKind": row.get("supersession_kind"),
        "supersededBySnapshotId": row.get("superseded_by_snapshot_id"),
        "supersededBySnapshotHash": row.get("superseded_by_snapshot_hash"),
    }


def _adjustment_options(
    *, funding_policy: str, approval_requirement: str,
    blockers: list[dict[str, Any]], snapshot_id: str, quote_id: str,
) -> list[dict[str, Any]]:
    """按 payer 裁剪的调整出口(§0.5.5 U-7:服务端裁剪,前端只折叠)。

    🔴 ``cancel_no_charge`` **恒在**(§15.7 逐字)。
    """
    if not blockers:
        return []
    delta = max(int(b["deltaPoints"]) for b in blockers)
    allowed = _ds.allowed_option_kinds(funding_policy, approval_requirement)
    out: list[dict[str, Any]] = []

    def emit(kind: str, *, scope: str, public_reason: str, public_change: str,
             public_loss: str | None, next_kind: str, capability: str,
             target: dict[str, Any], new_snapshot: bool) -> None:
        _ds.assert_option_allowed(
            funding_policy=funding_policy, approval_requirement=approval_requirement,
            option_kind=kind,
        )
        out.append({
            "kind": kind, "fundingPolicy": funding_policy, "scope": scope,
            "deltaPoints": 0 if kind == "cancel_no_charge" else delta,
            "publicReason": public_reason, "publicChange": public_change,
            "publicLoss": public_loss,
            "newCustomerSnapshotRequired": new_snapshot,
            "reconfirmPublishDecision": kind != "cancel_no_charge",
            "nextAction": _recovery_action(next_kind, target=target, capability=capability),
        })

    if "top_up" in allowed:
        emit("top_up", scope="global",
             public_reason="你的算力不够这次发布",
             public_change="充值后可以直接确认，方案和价格都不会变",
             public_loss=None, next_kind="top_up", capability="open_wallet",
             target={"kind": "page", "page": "wallet"}, new_snapshot=False)
    if "request_approval" in allowed:
        emit("request_approval", scope="global",
             public_reason="这笔支出超出了团队当前可用预算",
             public_change="请负责人批准后再确认，方案不变",
             public_loss=None, next_kind="request_approval",
             capability="request_owner_approval",
             target={"kind": "approval", "id": snapshot_id}, new_snapshot=False)
    if "contact_platform_budget_owner" in allowed:
        emit("contact_platform_budget_owner", scope="global",
             public_reason="这次由平台承担费用，平台预算需要人工确认",
             public_change="我们帮你转给平台处理，方案不变",
             public_loss=None, next_kind="contact_support", capability="contact_support",
             target={"kind": "page", "page": "support"}, new_snapshot=False)
    if "reduce_customer_delivery" in allowed:
        emit("reduce_customer_delivery", scope="media_publication",
             public_reason="也可以把这一单答应客户的量改小一点",
             public_change="改小之后要重新出一份报价给客户确认",
             public_loss="客户拿到的发布篇数会减少",
             next_kind="new_customer_snapshot", capability="revise_quote",
             target={"kind": "quote", "id": quote_id}, new_snapshot=True)
    # 恒在的那一个,放最后 —— 它是「什么都不做也不会扣钱」的出口。
    emit("cancel_no_charge", scope="media_publication",
         public_reason="也可以先不选这家媒体",
         public_change=_ds.CANCEL_NO_CHARGE_PUBLIC_CHANGE,
         public_loss="这篇稿子仍然要发，之后要再选一次媒体",
         next_kind="cancel", capability="cancel_publish_preview",
         target={"kind": "publish_snapshot", "id": snapshot_id}, new_snapshot=False)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 只读:census / GET
# ══════════════════════════════════════════════════════════════════════════
@router.get("/contract-census")
async def contract_census(request: Request) -> dict[str, Any]:
    """机械分母导出(§0.5.3 G-2)。判据从这里取,不手抄。

    只读、零副作用;登录即可读(它不含任何租户数据,只含合同结构)。
    """
    _tenant(request)
    return {
        "mediaIdentity": _mi.census(),
        "slot": _slot.census(),
        "settlement": _settle.census(),
        "decisionSnapshot": _ds.census(),
        "legalGate": _legal.census(),
        "funding": _funding.census(),
        "settlementReview": _review.census(),
        "store": _store.census(),
    }


class DeliveryTodoResponse(_Envelope):
    accepted_snapshot_id: int = Field(alias="acceptedSnapshotId")
    items: list[dict[str, Any]]
    pending_count: int = Field(alias="pendingCount")
    total_pending_points: int = Field(alias="totalPendingPoints")


@router.get("/delivery-todo")
async def delivery_todo(
    request: Request,
    accepted_snapshot_id: int = Query(..., alias="acceptedSnapshotId", ge=1),
) -> dict[str, Any]:
    """§0.5.5 **U-5 交付待办清单** —— 一页看完这一单要确认的全部媒体决策。

    U-5 逐字:「一问一稿一媒体的逐项 preview→confirm 保持独立 receipt/freeze 不变,
    但 UI **必须**提供聚合页……12~50 轮确认没有聚合页 = **验收红**」。

    🔴 聚合的是**呈现**,不是资金:每一项仍然各自 confirm、各自 freeze、
       各自 receipt。这个端点只读,零副作用 —— 它不会顺手替你把 preview 都签出来。
    🔴 「预计还要消耗 X 算力」是 U-4 两个必答时刻之一的前置信息:
       销售在点第一颗按钮之前就该知道这一单总共要花多少。
    """
    tenant = _tenant(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 🔴 [包E · P1-4] 行级过滤(下面那句 WHERE s.tenant_owner_id)一直都在,
        #    缺的是**对象级**这一层:拿别人的 acceptedSnapshotId 来问,过去返回
        #    200 空列表 —— 与本族其余五处「跨租户同形 404」的口径也不一致。
        _assert_accepted_snapshot_access(request, cur, accepted_snapshot_id)
        cur.execute(
            f"""
            SELECT s.publish_slot_id, s.plan_item_key,
                   d.decision_snapshot_id, d.lifecycle, d.canonical_hash,
                   d.snapshot_version, d.expires_at, d.frozen_payload,
                   d.consumed_command_id,
                   c.command_state, c.funding_state, c.canonical_publication_state
              FROM {_store.SLOT_TABLE} s
              LEFT JOIN {_store.SNAPSHOT_TABLE} d
                     ON d.publish_slot_id = s.publish_slot_id
                    AND d.lifecycle IN ('open','consumed')
              LEFT JOIN {_store.COMMAND_TABLE} c
                     ON c.decision_snapshot_id = d.decision_snapshot_id
             WHERE s.tenant_owner_id = %s AND s.accepted_snapshot_id = %s
             ORDER BY s.plan_item_key
            """,
            (tenant, accepted_snapshot_id),
        )
        rows = _store._rows(cur)                       # noqa: SLF001 —— 同族读工具
        conn.rollback()
    except Exception:
        # 🔴 归属闸抛在事务里 —— 不 rollback 就把一条 idle-in-transaction 归还连接池。
        #    本仓记过它的代价(2026-08-10 生产自死锁那次的同族)。
        try:
            conn.rollback()
        except Exception:                                 # noqa: BLE001
            pass
        raise
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    pending = 0
    pending_points = 0
    for r in rows:
        frozen = dict(r["frozen_payload"]) if r.get("frozen_payload") else None
        points = int((frozen or {}).get("totalExactPoints") or 0)
        lifecycle = r.get("lifecycle")
        if lifecycle == "open":
            state_key, pending, pending_points = "awaiting_confirm", pending + 1, pending_points + points
        elif r.get("command_state"):
            state_key = "in_progress"
        else:
            state_key = "not_previewed"
        items.append({
            "planItemKey": str(r["plan_item_key"]),
            "publishSlotId": str(r["publish_slot_id"]),
            "decisionSnapshotId": r.get("decision_snapshot_id"),
            "lifecycle": lifecycle,
            "todoState": state_key,
            # 🔴 人话在服务端下发 —— 前端不得自造(U-1「同一概念全站唯一叫法」)。
            "todoLabel": {
                "awaiting_confirm": "等你确认媒体方案",
                "in_progress": "已确认，正在发布",
                "not_previewed": "还没选媒体",
            }[state_key],
            "exactPoints": points,
            "publicMediaName": ((frozen or {}).get("decision") or {}).get("publicMediaName"),
            "expiresAt": _iso(r.get("expires_at")) if r.get("expires_at") else None,
            "statusUrl": _status_url(str(r["consumed_command_id"]))
                         if r.get("consumed_command_id") else None,
        })
    payload = {
        "acceptedSnapshotId": accepted_snapshot_id,
        "items": items,
        "pendingCount": pending,
        "totalPendingPoints": pending_points,
    }
    _mi.assert_no_private_leak(payload, field="deliveryTodo")
    return _respond(DeliveryTodoResponse, payload)


@router.get("/decision-snapshots/{snapshot_id}")
async def get_decision_snapshot(snapshot_id: str, request: Request) -> dict[str, Any]:
    """MED-13 / UI-35:exact GET 恢复。

    **不重新推荐、不读 latest、不延长有效期**;每次重验归属;
    跨租户与对象不存在**同形 404**。
    """
    tenant = _tenant(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        row = _store.get_snapshot(
            cur, decision_snapshot_id=snapshot_id, tenant_owner_id=tenant,
        )
        if row is None:
            raise _safe_error("OBJECT_NOT_FOUND")
        payload = _snapshot_response(
            row, blockers=[], options=[], approval_state="not_required",
            blocked_reason=None if row["lifecycle"] == "open" else None,
        )
        conn.rollback()                     # 只读:显式收尾,不留未提交事务
    finally:
        conn.close()
    return _respond(SnapshotResponse, payload)


@router.get("/commands/{command_id}")
async def get_command_status(command_id: str, request: Request) -> dict[str, Any]:
    """§15.7 GET status。逐请求重验归属;raw 状态按 exhaustive adapter 投影。

    🔴 未知/冲突进 ``quarantined`` 且**资金不自动 release**。
    """
    tenant = _tenant(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        row = _store.get_command(
            cur, publish_command_id=command_id, tenant_owner_id=tenant,
        )
        if row is None:
            raise _safe_error("OBJECT_NOT_FOUND")
        payload = _command_status_payload(row)
        conn.rollback()
    finally:
        conn.close()
    return _respond(CommandStatusResponse, payload)


def _command_status_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    state = str(row["canonical_publication_state"])
    direction = _settle.settlement_direction(state)
    availability = _settle.public_availability(state)
    funding_state = str(row["funding_state"])
    command_state = str(row["command_state"])
    is_platform = _funding.is_platform_cost(str(row["funding_policy"]))
    public_url = row.get("public_url") if _settle.url_eligible(state) else None

    # 🔴 真值表先过 —— 非法组合让整个 projection 失败(§15.7 末段)。
    _settle.assert_truth_table(
        canonical_publication_state=state,
        funding_state=funding_state,
        command_state=command_state,
        availability=availability,
        public_url=public_url,
        is_platform_cost=is_platform,
    )

    legal_hit = None
    next_action: dict[str, Any] | None
    if row.get("legal_rule_id"):
        hit = _legal.LegalRuleHit(
            rule_id=str(row["legal_rule_id"]),
            rule_version=str(row["legal_rule_version"] or ""),
            passage_ref=str(row["legal_passage_ref"] or ""),
            passage_excerpt=str(row["legal_passage_excerpt"] or ""),
            article_revision_id=str(row["article_revision_id"]),
        )
        legal_hit = hit.wire()
        next_action = _legal.repair_action(hit, label=_label("repair_legal_passage"))
        _legal.assert_repair_matches(hit, next_action)
    elif direction in ("hold_frozen", "none"):
        next_action = _recovery_action(
            "wait", target={"kind": "publish_command", "id": str(row["publish_command_id"])},
            capability="view_publish_status")
    elif direction == "hold_or_quarantine":
        next_action = _recovery_action(
            "verify_outcome",
            target={"kind": "publish_item", "id": str(row["publish_item_request_id"])},
            capability="review_publication_verification")
    elif direction == "release":
        next_action = _recovery_action(
            "contact_support", target={"kind": "page", "page": "support"},
            capability="contact_support") if is_platform else _recovery_action(
            "retry_child",
            target={"kind": "publish_command", "id": str(row["publish_command_id"])},
            capability="retry_publish_child")
    elif direction == "preserve_historical_commit":
        policy = row.get("replacement_policy_ref")
        next_action = _recovery_action(
            "review_replacement_policy",
            target={"kind": "publish_command", "id": str(row["publish_command_id"])},
            capability="review_publish_replacement") if policy else _recovery_action(
            "handoff_owner",
            target={"kind": "owner_handoff", "id": str(row["publish_command_id"])},
            capability="contact_owner")
    else:                                     # commit
        next_action = None

    item: dict[str, Any] = {
        "publishItemRequestId": str(row["publish_item_request_id"]),
        "publicationSettlement": {
            "canonicalPublicationState": state,
            "settlementDirection": direction,
        },
        "availability": availability,
        "publicUrl": public_url,
        "reason": row.get("status_reason"),
        "verificationClues": [],
        "legalRuleHit": legal_hit,
        "nextAction": next_action,
    }
    return {
        "publishCommandId": str(row["publish_command_id"]),
        "decisionSnapshotId": str(row["decision_snapshot_id"]),
        "decisionSnapshotHash": str(row["decision_snapshot_hash"]),
        "commandCanonicalHash": str(row["command_canonical_hash"]),
        "statusVersion": int(row["status_version"]),
        "updatedAt": _iso(row.get("updated_at")),
        "commandState": command_state,
        "commandStateLabel": _run_state_label(command_state),
        "reason": row.get("status_reason"),
        "fundingPolicy": str(row["funding_policy"]),
        "fundingState": funding_state,
        "fundingStateLabel": _funding_label(funding_state),
        "nextAction": next_action,
        "item": item,
    }


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "")


# ══════════════════════════════════════════════════════════════════════════
# Z-1 · 资金核验队列(平台 admin)
# ══════════════════════════════════════════════════════════════════════════
@router.get("/admin/settlement-review")
async def admin_settlement_queue(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Z-1 队列。**平台 admin only**;队列是 command 上的投影,不另存状态。"""
    _require_admin(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        rows = _store.review_queue(cur, limit=limit, offset=offset)
        stale = len([r for r in rows
                     if int(r.get("pending_seconds") or 0) >= _review_stale_seconds()])
        entries = [{
            "publishCommandId": str(r["publish_command_id"]),
            "tenantOwnerId": int(r["tenant_owner_id"]),
            "brandId": int(r["brand_id"]),
            "fundingPolicy": str(r["funding_policy"]),
            "fundingState": str(r["funding_state"]),
            "fundingStateLabel": _funding_label(str(r["funding_state"])),
            "exactSettlementPoints": int(r["exact_settlement_points"]),
            "canonicalPublicationState": str(r["canonical_publication_state"]),
            "externalStartAt": _iso(r.get("external_start_at")) if r.get("external_start_at") else None,
            "providerCallCount": int(r["provider_call_count"]),
            "pendingSeconds": int(r.get("pending_seconds") or 0),
            "isStale": int(r.get("pending_seconds") or 0) >= _review_stale_seconds(),
            "reviewEntryCount": int(r.get("review_entry_count") or 0),
            "reason": r.get("status_reason"),
        } for r in rows]
        conn.rollback()
    finally:
        conn.close()
    return _respond(ReviewQueueResponse, {
        "entries": entries,
        "totalPending": len(entries),
        "staleOverDays": 7,
        "actionCatalog": [
            {"action": a, "label": _review.ACTION_LABELS[a],
             "reasonRequired": a == "admin_hold"}
            for a in _review.ADMIN_ACTIONS
        ],
    })


def _review_stale_seconds() -> int:
    from services.defensive_geo.publish.reconciler import PENDING_ALERT_SECONDS

    return int(PENDING_ALERT_SECONDS)


@router.post("/admin/settlement-review/{command_id}")
async def admin_settlement_action(
    command_id: str, body: AdminReviewBody, request: Request,
) -> dict[str, Any]:
    """Z-1 三条动作。留痕与动钱**同事务**。"""
    admin_id = _require_admin(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            outcome = await _review.apply_admin_action(
                cur, publish_command_id=command_id, action=body.action,
                admin_user_id=admin_id, reason=body.reason,
            )
        except _review.ReviewError as exc:
            conn.rollback()
            raise _safe_error("VALIDATION_ERROR", details=None) from exc
        conn.commit()
    except HTTPException:
        raise
    except Exception as exc:                              # noqa: BLE001
        conn.rollback()
        ref = uuid.uuid4().hex[:16]
        logger.error("[defgeo-publish] admin 处置失败 ref=%s: %s", ref, exc)
        raise _internal_error(ref) from None
    finally:
        conn.close()
    return _respond(ReviewActionResponse, {
        "publishCommandId": outcome.command_id,
        "action": outcome.action,
        "fundingStateBefore": outcome.funding_state_before,
        "fundingStateAfter": outcome.funding_state_after,
        "fundingStateLabel": _funding_label(outcome.funding_state_after),
        "entryId": outcome.entry_id,
    })


@router.post("/commands/{command_id}/verification-evidence")
async def submit_verification_evidence(
    command_id: str, body: ProviderEvidenceBody, request: Request,
) -> dict[str, Any]:
    """Z-1:服务商提交线下核实凭证。**零资金副作用**,凭证进同一队列。"""
    tenant = _tenant(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        try:
            entry = _review.submit_provider_evidence(
                cur, publish_command_id=command_id, tenant_owner_id=tenant,
                provider_user_id=tenant,
                evidence={"kind": body.evidence_kind, "text": body.evidence_text},
            )
        except _review.ReviewError:
            conn.rollback()
            raise _safe_error("OBJECT_NOT_FOUND") from None
        conn.commit()
    except HTTPException:
        raise
    except Exception as exc:                              # noqa: BLE001
        conn.rollback()
        ref = uuid.uuid4().hex[:16]
        logger.error("[defgeo-publish] 凭证提交失败 ref=%s: %s", ref, exc)
        raise _internal_error(ref) from None
    finally:
        conn.close()
    return _respond(ReviewActionResponse, {
        "publishCommandId": command_id,
        "action": _review.PROVIDER_ACTION,
        "fundingStateBefore": str(entry["funding_state_before"]),
        "fundingStateAfter": str(entry["funding_state_after"]),
        "fundingStateLabel": _funding_label(str(entry["funding_state_after"])),
        "entryId": int(entry["id"]),
    })


# ══════════════════════════════════════════════════════════════════════════
# cancel —— 零 command / 零 freeze / 零 outbox / 零 provider
# ══════════════════════════════════════════════════════════════════════════
@router.post("/decision-snapshots/{snapshot_id}/cancel")
async def cancel_decision_snapshot(
    snapshot_id: str, body: ConfirmBody, request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """§15.7 cancel:只对 open/unconsumed 做 CAS。

    「取消本次媒体决策(该交付项**仍待履约**)」—— 客户合同交付量不因此减少。
    """
    tenant = _tenant(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        row = _store.get_snapshot(
            cur, decision_snapshot_id=snapshot_id, tenant_owner_id=tenant,
        )
        if row is None:
            conn.rollback()
            raise _safe_error("OBJECT_NOT_FOUND")
        if str(row["canonical_hash"]) != body.expected_hash or \
                int(row["snapshot_version"]) != body.expected_version:
            conn.rollback()
            raise _safe_error("SNAPSHOT_CHANGED", reason_key="snapshot_changed", details={
                "decisionSnapshotId": snapshot_id,
                "expectedHash": str(row["canonical_hash"]),
                "expectedVersion": int(row["snapshot_version"]),
            })
        if row["lifecycle"] == "cancelled":
            conn.rollback()                    # 幂等重放:同 key 返回同 cancelled
        elif row["lifecycle"] == "consumed":
            conn.rollback()
            raise _safe_error(
                "PREVIEW_ALREADY_CONSUMED", reason_key="already_consumed",
                details={"publishCommandId": str(row["consumed_command_id"]),
                         "statusUrl": _status_url(str(row["consumed_command_id"]))},
            )
        elif row["lifecycle"] != "open":
            conn.rollback()
            raise _safe_error("PUBLISH_DECISION_NOT_CONFIRMABLE", details={
                "decisionSnapshotId": snapshot_id,
                "successorSnapshotId": row.get("superseded_by_snapshot_id"),
                "successorSnapshotHash": row.get("superseded_by_snapshot_hash"),
                "supersessionKind": row.get("supersession_kind"),
            })
        else:
            _store.lock_slot(cur, str(row["publish_slot_id"]))
            _store.cas_lifecycle(
                cur, decision_snapshot_id=snapshot_id, tenant_owner_id=tenant,
                expect="open", to="cancelled",
            )
            conn.commit()
        cur = conn.cursor()
        fresh = _store.get_snapshot(
            cur, decision_snapshot_id=snapshot_id, tenant_owner_id=tenant,
        )
        assert fresh is not None
        payload = _snapshot_response(
            fresh, blockers=[], options=[], approval_state="not_required",
            blocked_reason=None,
        )
        conn.rollback()
    except HTTPException:
        raise
    except Exception as exc:                              # noqa: BLE001
        conn.rollback()
        ref = uuid.uuid4().hex[:16]
        logger.error("[defgeo-publish] cancel 失败 ref=%s: %s", ref, exc)
        raise _internal_error(ref) from None
    finally:
        conn.close()
    return _respond(SnapshotResponse, payload)


# ══════════════════════════════════════════════════════════════════════════
# preview —— 五步的 ①②③(preview / propose / freeze)
# ══════════════════════════════════════════════════════════════════════════
def _admission_branch(
    *, slot_state: str, publish_slot_id: str, command: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """§15.7 ``PublishPreviewAdmissionResponse`` 的非 ready 分支。

    🔴 每一格都带**同 command/root 的 typed action**;不许 raw 409、
       不许返回另一个 snapshot、不许让前端猜(§15.7 逐字)。
    """
    r = _slot.rule(slot_state)
    cmd_id = str(command["publish_command_id"]) if command else ""
    payload: dict[str, Any] = {
        "slotAdmission": r.admission,
        "publishSlotId": publish_slot_id,
        "slotState": slot_state,
        "originalCommandId": cmd_id or None,
        "statusUrl": _status_url(cmd_id) if cmd_id else None,
        "snapshotResponse": None,
        "legalRuleHit": None,
        "replacementPolicyRef": None,
    }
    if slot_state == "legal_no_effect_released" and command:
        hit = _legal.LegalRuleHit(
            rule_id=str(command["legal_rule_id"]),
            rule_version=str(command["legal_rule_version"] or ""),
            passage_ref=str(command["legal_passage_ref"] or ""),
            passage_excerpt=str(command["legal_passage_excerpt"] or ""),
            article_revision_id=str(command["article_revision_id"]),
        )
        payload["legalRuleHit"] = hit.wire()
        action = _legal.repair_action(hit, label=_label("repair_legal_passage"))
        _legal.assert_repair_matches(hit, action)
        payload["nextAction"] = action
        return payload
    if slot_state == "retracted_committed" and command:
        policy = command.get("replacement_policy_ref")
        payload["replacementPolicyRef"] = policy
        payload["nextAction"] = _recovery_action(
            "review_replacement_policy",
            target={"kind": "publish_command", "id": cmd_id},
            capability="review_publish_replacement",
        ) if policy else _recovery_action(
            "handoff_owner", target={"kind": "owner_handoff", "id": cmd_id},
            capability="contact_owner",
        )
        return payload
    payload["nextAction"] = _recovery_action(
        r.next_action_kind,
        target={"kind": "publish_command", "id": cmd_id} if cmd_id
        else {"kind": "publish_slot", "id": publish_slot_id},
        capability=r.capability,
    )
    return payload


def _read_slot_state(
    cur, *, tenant: int, publish_slot_id: str,
) -> tuple[str, Mapping[str, Any] | None]:
    """§12.2:**先读该 slot 的 canonical 全序**,不是只查有没有 live command。

    差别在两个真实损失方向:``verified_published + committed`` 和
    ``legal_no_effect_released`` 两格都**没有** live command,
    只查 live 会把它们当成空格放行(→ 二次发布二次扣 / 绕过法律修复)。
    """
    latest = _store.latest_command(cur, publish_slot_id=publish_slot_id)
    if latest is not None:
        state = _slot.classify_command(
            canonical_publication_state=str(latest["canonical_publication_state"]),
            funding_state=str(latest["funding_state"]),
            legal_rule_hit=bool(latest.get("legal_rule_id")),
        )
        # 🔴 法律格的**修复续链**:§12.2 允许「以新 article revision/decision/confirm
        #    且显式绑定 parent legal command」继续。所以当法律格上已经存在一个
        #    **换了稿**的 open snapshot 时,slot 的当前态就是 open_snapshot ——
        #    否则修复完的稿子永远确认不了,法律格变成死路(§0.5.6 铁律)。
        #    收窄到「换了稿」这一条:同一份稿子重开 preview 仍然被挡在法律格里。
        if state == "legal_no_effect_released":
            opened = _store.open_snapshot(
                cur, tenant_owner_id=tenant, publish_slot_id=publish_slot_id)
            if opened is not None:
                frozen = dict(opened["frozen_payload"])
                if str(frozen.get("articleRevisionId")) != str(latest["article_revision_id"]):
                    return "open_snapshot", latest
        return state, latest
    if _store.open_snapshot(cur, tenant_owner_id=tenant, publish_slot_id=publish_slot_id):
        return "open_snapshot", None
    return "empty", None


def _legal_repair_continuation_ok(
    *, slot_state: str, latest: Mapping[str, Any] | None,
    body: PublishDecisionPreviewRequest,
) -> bool:
    """法律格续链的三个条件。任一不满足 → 走回普通 admission 分支(即拒绝)。

    ① slot 确实在法律格(``legal_no_effect_released``);
    ② 请求显式绑定了 parent legal command,且那条 command 就是本格的 latest;
    ③ **新 article revision 与 parent 的不同** —— 没改稿就没修复,
       让原稿再走一遍等于把法律门当摆设。

    🔴 这里不检查「改对了没有」:那是外调前那道门的事(catalog 热加载,
       所以必须在 external-start 前再验一次,而不是信这里的结论)。
    """
    if slot_state != "legal_no_effect_released":
        return False
    if latest is None or not body.parent_legal_command_id:
        return False
    if str(latest["publish_command_id"]) != body.parent_legal_command_id:
        return False
    if not latest.get("legal_rule_id"):
        return False
    return str(latest["article_revision_id"]) != body.article_revision_id


def _accepted_of_snapshot_id(decision_snapshot_id: str) -> int | None:
    """从冻结面里取 acceptedSnapshotId。**独立只读连接** ——
    调用它的时候外层事务已经因异常回滚,拿那个游标只会拿到 aborted 事务。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT frozen_payload FROM {_store.SNAPSHOT_TABLE} "
            f"WHERE decision_snapshot_id = %s", (decision_snapshot_id,))
        got = cur.fetchone()
        conn.rollback()
    except Exception:                                     # noqa: BLE001
        try:
            conn.rollback()
        except Exception:                                 # noqa: BLE001
            pass
        return None
    finally:
        conn.close()
    if not got:
        return None
    payload = got["frozen_payload"] if isinstance(got, Mapping) else got[0]
    if not isinstance(payload, Mapping):
        return None
    try:
        return int(payload.get("acceptedSnapshotId"))
    except (TypeError, ValueError):
        return None


def _budget_not_ready_error(exc: BaseException, *, accepted_snapshot_id: int):
    """异常 → typed 信封。**认不出来就返回 None**(让调用方走受控 500)。

    🔴 判别按**异常类型**,不按消息文本:按文本匹配会在有人改一个字的时候
       静默退回 500,而那正是这条判据要挡的形态。
    """
    from services.defensive_geo.publish.execution_budget_policy import BudgetPolicyError
    from services.defensive_geo.publish.publish_funding import BudgetExceeded, FundingError

    if isinstance(exc, BudgetExceeded):
        return None                      # 预算够不够是另一格(有 blockers 出口)
    if not isinstance(exc, (FundingError, BudgetPolicyError)):
        return None
    return _safe_error(
        "EXECUTION_BUDGET_NOT_READY",
        reason_key="execution_budget_not_ready",
        next_action=_recovery_action(
            "view_order_progress",
            target={"kind": "service_projection",
                    "id": _budget_policy.service_projection_id_for(accepted_snapshot_id)},
            capability="view_service_progress",
        ),
    )


@router.post("/decision-snapshots/preview")
async def preview_decision_snapshot(
    body: PublishDecisionPreviewRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
) -> dict[str, Any]:
    """§11.3 ①②③ + §15.7 preview。**零资金、零 outbox、零 provider**。

    [终审 P0-1 2026-08-23] 客户入口已关 —— 见 :data:`_CUSTOMER_PUBLISH_ENTRY_OPEN`。
    """
    _assert_customer_publish_entry_open()
    tenant = _tenant(request)
    from auth.brand_access import require_brand_access
    from db.connection import get_connection

    try:
        require_brand_access(request, body.brand_id, allow_null=False)
    except HTTPException:
        raise _safe_error("OBJECT_NOT_FOUND") from None

    slot_id = _slot.derive_publish_slot_id(
        tenant_owner_id=tenant,
        service_projection_id=body.service_projection_id,
        accepted_snapshot_id=body.accepted_snapshot_id,
        plan_item_key=body.plan_item_key,
    )
    request_hash = _ds.canonical_hash({
        "planItemKey": body.plan_item_key,
        "articleRevisionId": body.article_revision_id,
        "expectedArticleHash": body.expected_article_hash,
        "acceptedSnapshotId": body.accepted_snapshot_id,
        "serviceProjectionId": body.service_projection_id,
        "brandId": body.brand_id,
    })

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 🔴 [包E · P1-4] acceptedSnapshotId 的对象级归属 —— 在建 slot **之前**。
        #    放在 ensure_slot 之后等于"先按别人的合同建好格子再说",
        #    那时即使拒绝了,库里也已经多了一行绑错合同的 slot。
        _assert_accepted_snapshot_access(request, cur, body.accepted_snapshot_id)
        _store.ensure_slot(
            cur, publish_slot_id=slot_id, tenant_owner_id=tenant,
            service_projection_id=body.service_projection_id,
            accepted_snapshot_id=body.accepted_snapshot_id,
            plan_item_key=body.plan_item_key, brand_id=body.brand_id,
            publish_item_request_id=_slot.derive_publish_item_request_id(slot_id),
        )
        _store.lock_slot(cur, slot_id)
        _store.expire_stale_open(cur, tenant_owner_id=tenant, publish_slot_id=slot_id)

        slot_state, latest = _read_slot_state(cur, tenant=tenant, publish_slot_id=slot_id)
        if not _slot.allows_ordinary_preview(slot_state):
            # 🔴 唯一的例外:法律修复续链。**不是**普通 preview —— 三个条件全满足才放行。
            if not _legal_repair_continuation_ok(
                slot_state=slot_state, latest=latest, body=body,
            ):
                payload = _admission_branch(
                    slot_state=slot_state, publish_slot_id=slot_id, command=latest)
                conn.commit()
                return _respond(PreviewAdmissionResponse, payload)

        # ── 幂等:同 key + 同 canonical request → 返回原 snapshot;异 payload 409 ──
        replay = None
        for prior in _store.find_by_idempotency(
            cur, tenant_owner_id=tenant, publish_slot_id=slot_id,
            idempotency_key=idempotency_key,
        ):
            if str(prior["request_canonical_hash"]) == request_hash:
                if str(prior["lifecycle"]) == "open":
                    replay = prior
                break
            raise _safe_error("IDEMPOTENCY_CONFLICT")
        if replay is not None:
            payload = {
                "slotAdmission": "snapshot_ready", "publishSlotId": slot_id,
                "slotState": "open_snapshot",
                "snapshotResponse": _snapshot_response(
                    replay, blockers=[], options=[],
                    approval_state="not_required", blocked_reason=None),
                "originalCommandId": None, "statusUrl": None,
                "legalRuleHit": None, "replacementPolicyRef": None,
                "nextAction": _recovery_action(
                    "confirm_publish_decision",
                    target={"kind": "publish_snapshot",
                            "id": str(replay["decision_snapshot_id"])},
                    capability="confirm_publish_decision"),
            }
            conn.commit()
            return _respond(PreviewAdmissionResponse, payload)

        payload = _build_new_preview(
            cur, tenant=tenant, body=body, slot_id=slot_id,
            idempotency_key=idempotency_key, request_hash=request_hash,
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:                              # noqa: BLE001
        conn.rollback()
        # 🔴 [包E · 终审 P1-2] 「这一单的执行预算还没签发」是一个**可解释的业务态**,
        #    不是内部故障。过去它走这条兜底 → 受控 500 → 前端读成"我们坏了"。
        #    §0.5.6 铁律:任何阻塞必须自带解决方案 —— 所以给 typed code + 下一步。
        typed = _budget_not_ready_error(exc, accepted_snapshot_id=body.accepted_snapshot_id)
        if typed is not None:
            raise typed from None
        ref = uuid.uuid4().hex[:16]
        logger.error("[defgeo-publish] preview 失败 ref=%s: %s", ref, exc, exc_info=True)
        raise _internal_error(ref) from None
    finally:
        conn.close()
    return _respond(PreviewAdmissionResponse, payload)


def _build_new_preview(
    cur, *, tenant: int, body: PublishDecisionPreviewRequest,
    slot_id: str, idempotency_key: str, request_hash: str,
) -> dict[str, Any]:
    """签发一份新的冻结面 + live sidecar。**零资金副作用**。"""
    from services.defensive_geo.publish import media_proposal as _prop

    budget, _blockers = _funding.check_and_lock_budget(
        cur, tenant_owner_id=tenant, accepted_snapshot_id=body.accepted_snapshot_id,
        service_projection_id=body.service_projection_id, required_points=0,
    )
    usage = _store.budget_usage(
        cur, execution_budget_snapshot_id=str(budget["execution_budget_snapshot_id"]))
    cap_global = int(budget["global_cap_points"])
    cap_scope = int(budget["scope_cap_points"])
    reserved = int(usage["reservedPoints"])
    committed = int(usage["committedPoints"])
    remaining_scope = cap_scope - reserved - committed

    snapshot_id = "pds_" + uuid.uuid4().hex
    rows = _prop.read_candidates(cur, industry=None)
    decision, alternatives = _prop.propose(
        rows, decision_snapshot_id=snapshot_id, remaining_points=max(0, remaining_scope))

    # 🔴 accepted **客户**快照的 hash,不是 provider 预算的 hash。
    #    §3.4 逐字:「customer offer hash 与 provider budget hash 字段范围**完全分开**」。
    #    两者混用会让 FIN-15 的「提高内部预算不得改变 customer snapshot 字节」
    #    在错误的一侧全绿(预算涨了,provider hash 变了,而这里以为客户 hash 也该变)。
    accepted_hash = _accepted_snapshot_hash(cur, int(body.accepted_snapshot_id))

    approval_requirement = "required" if budget.get("approval_ref") else "not_required"
    expires_at = (datetime.now(timezone.utc)
                  + timedelta(minutes=_PREVIEW_TTL_MINUTES)).isoformat()
    frozen, digest = _ds.freeze_snapshot(
        decision_snapshot_id=snapshot_id,
        publish_slot_id=slot_id,
        snapshot_version=1,
        expires_at=expires_at,
        accepted_snapshot_id=str(body.accepted_snapshot_id),
        accepted_snapshot_hash=accepted_hash,
        service_projection_id=body.service_projection_id,
        service_projection_version=int(budget["budget_version"]),
        plan_item_key=body.plan_item_key,
        question_key=body.plan_item_key,
        question_revision=1,
        article_revision_id=body.article_revision_id,
        article_hash=body.expected_article_hash,
        pricing_catalog_version=f"mhz-media@{budget['budget_version']}",
        inventory_snapshot_version=f"inv@{budget['execution_budget_snapshot_id']}",
        execution_budget_snapshot_id=str(budget["execution_budget_snapshot_id"]),
        execution_budget_version=int(budget["budget_version"]),
        global_budget={"capPoints": cap_global, "reservedPoints": reserved,
                       "committedPoints": committed,
                       "remainingPoints": cap_global - reserved - committed},
        scope_budget={"capPoints": cap_scope, "reservedPoints": reserved,
                      "committedPoints": committed, "remainingPoints": remaining_scope},
        decision=decision,
        alternatives=alternatives,
        publish_item_request_id=_slot.derive_publish_item_request_id(slot_id),
        replacement_policy_version="defgeo-replacement-policy-v1",
        funding_policy=str(budget["funding_policy"]),
        principal_kind=_ds.frozen_funding_row(
            str(budget["funding_policy"]), approval_requirement).principal_kind,
        approval_requirement=approval_requirement,
        sponsor_policy_ref=budget.get("sponsor_policy_ref"),
    )

    # 🔴 先 CAS supersede 旧 open,再插新的 —— 顺序反了会撞 partial unique。
    _store.supersede_open(
        cur, tenant_owner_id=tenant, publish_slot_id=slot_id,
        successor_id=snapshot_id, successor_hash=digest, kind="new_preview",
    )
    row = _store.insert_snapshot(
        cur, decision_snapshot_id=snapshot_id, publish_slot_id=slot_id,
        snapshot_version=1, canonical_hash=digest, frozen_payload=frozen,
        expires_at=expires_at, idempotency_key=idempotency_key,
        request_canonical_hash=request_hash, tenant_owner_id=tenant,
    )

    need = int(decision.exact_points)
    blockers: list[dict[str, Any]] = []
    for scope_name, cap in (("global", cap_global), ("media_publication", cap_scope)):
        remaining = cap - reserved - committed
        if reserved + committed + need > cap:
            blockers.append({
                "scope": scope_name, "requiredExactPoints": need,
                "remainingPoints": remaining, "deltaPoints": need - remaining,
                "budgetSnapshotId": str(budget["execution_budget_snapshot_id"]),
                "budgetVersion": int(budget["budget_version"]),
            })
    options = _adjustment_options(
        funding_policy=str(frozen["fundingPolicy"]),
        approval_requirement=approval_requirement,
        blockers=blockers, snapshot_id=snapshot_id,
        quote_id=str(body.accepted_snapshot_id),
    )
    approval_state = "required" if approval_requirement == "required" else "not_required"
    blocked = "insufficient_points" if blockers else (
        "approval_required" if approval_state == "required" else None)
    return {
        "slotAdmission": "snapshot_ready", "publishSlotId": slot_id,
        "slotState": "open_snapshot",
        "snapshotResponse": _snapshot_response(
            row, blockers=blockers, options=options,
            approval_state=approval_state, blocked_reason=blocked),
        "originalCommandId": None, "statusUrl": None,
        "legalRuleHit": None, "replacementPolicyRef": None,
        "nextAction": _recovery_action(
            "confirm_publish_decision",
            target={"kind": "publish_snapshot", "id": snapshot_id},
            capability="confirm_publish_decision"),
    }


# ══════════════════════════════════════════════════════════════════════════
# confirm —— 五步的 ④。**窗A 留的 work_admission 出口在这里接上**
# ══════════════════════════════════════════════════════════════════════════
@router.post("/decision-snapshots/{snapshot_id}/confirm")
async def confirm_decision_snapshot(
    snapshot_id: str, body: ConfirmBody, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
) -> dict[str, Any]:
    """§15.7 confirm。**单事务**:admission → 预算 CAS → exact freeze →
    command → outbox → snapshot CAS consumed。任一步失败整体回滚(零半状态)。

    🔴 这里是 ``services/defensive_geo/work_admission.admit`` 的**第一个生产消费点**。
       窗A 把它建好但零调用者,并用
       ``test_work_admission_consumption_side_has_no_production_caller``
       钉住那个事实。本窗接上它 ⇒ 那条边界锁**按设计翻红**,
       同 commit 改写成「只许这一处调用」的正向 census 锁。

    [终审 P0-1 2026-08-23] 客户入口已关 —— 见 :data:`_CUSTOMER_PUBLISH_ENTRY_OPEN`。
    这一条是**真冻钱**的那个(``check_and_lock_budget`` → ``freeze_exact``)。

    [fix-of-fix P1-A 2026-08-23] 🔴 关闸**之前**先做一次只读幂等重放。

    P0-1 那一版把关闸放在第一行,于是**已经确认过、钱已经冻住**的那些命令
    也一并被 403 挡在门外。对她来说那是"我明明已经确认过了,现在连查都查不到" ——
    我们把一次**已经发生**的资金事件从她眼前抹掉了。关入口是为了别让新的钱
    进黑洞,不是为了把旧的钱藏起来。
    所以顺序改成:先认"这就是那一次"(只读、tenant-scoped、零副作用),
    认不出来才落 ``PUBLISH_ENTRY_CLOSED``。
    """
    tenant = _tenant(request)
    replay = _consumed_replay_or_none(
        snapshot_id=snapshot_id, tenant=tenant,
        expected_hash=body.expected_hash, expected_version=body.expected_version,
        idempotency_key=idempotency_key,
    )
    if replay is not None:
        return replay
    _assert_customer_publish_entry_open()
    from db.connection import get_connection
    from services.defensive_geo import work_admission

    conn = get_connection()
    try:
        cur = conn.cursor()
        row = _store.get_snapshot(
            cur, decision_snapshot_id=snapshot_id, tenant_owner_id=tenant)
        if row is None:
            conn.rollback()
            raise _safe_error("OBJECT_NOT_FOUND")
        slot_id = str(row["publish_slot_id"])
        _store.lock_slot(cur, slot_id)
        # 锁后重读 —— 锁之前读到的可能已经被并发改走。
        row = _store.get_snapshot(
            cur, decision_snapshot_id=snapshot_id, tenant_owner_id=tenant)
        assert row is not None
        frozen = dict(row["frozen_payload"])

        if str(row["canonical_hash"]) != body.expected_hash or \
                int(row["snapshot_version"]) != body.expected_version:
            conn.rollback()
            raise _safe_error("SNAPSHOT_CHANGED", reason_key="snapshot_changed", details={
                "decisionSnapshotId": snapshot_id,
                "expectedHash": str(row["canonical_hash"]),
                "expectedVersion": int(row["snapshot_version"]),
            })

        lifecycle = str(row["lifecycle"])
        if lifecycle == "consumed":
            # 幂等重放:同 root 返回原 command,零新增资金/任务(FIN-01)。
            cmd = _store.get_command(
                cur, publish_command_id=str(row["consumed_command_id"]),
                tenant_owner_id=tenant)
            conn.rollback()
            if cmd is None:
                raise _safe_error("OBJECT_NOT_FOUND")
            # 响应形状与 P1-A 那条**共用同一个构造器** —— 两处各拼一遍迟早会漂,
            # 而漂的后果是"关闸前后拿到的重放不是同一份东西"。
            return _replay_response(cmd, frozen)
        if lifecycle != "open":
            conn.rollback()
            raise _safe_error("PUBLISH_DECISION_NOT_CONFIRMABLE", details={
                "decisionSnapshotId": snapshot_id,
                "successorSnapshotId": row.get("superseded_by_snapshot_id"),
                "successorSnapshotHash": row.get("superseded_by_snapshot_hash"),
                "supersessionKind": row.get("supersession_kind"),
            })

        slot_state, latest = _read_slot_state(cur, tenant=tenant, publish_slot_id=slot_id)
        if not _slot.allows_confirm(slot_state):
            conn.rollback()
            raise _safe_error("PUBLISH_DECISION_NOT_CONFIRMABLE", details={
                "decisionSnapshotId": snapshot_id,
                "publishCommandId": str(latest["publish_command_id"]) if latest else None,
                "statusUrl": _status_url(str(latest["publish_command_id"])) if latest else None,
            })

        payload = await _do_confirm(
            cur, tenant=tenant, request=request, row=row, frozen=frozen,
            slot_id=slot_id, idempotency_key=idempotency_key,
            work_admission=work_admission,
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except _funding.FundingError as exc:
        conn.rollback()
        # 🔴 [包E] 「这一单还没有执行预算快照」与「你的算力不够」是两件事,
        #    过去被压成同一个 INSUFFICIENT_POINTS —— 她会去充值,充完还是不行。
        #    错误信封说错原因,比不说更贵。
        typed = _budget_not_ready_error(
            exc, accepted_snapshot_id=int(_accepted_of_snapshot_id(snapshot_id) or 0))
        if typed is not None:
            raise typed from None
        logger.warning("[defgeo-publish] confirm 资金腿拒绝:%s", exc)
        raise _safe_error("INSUFFICIENT_POINTS", reason_key="insufficient_points") from None
    except Exception as exc:                              # noqa: BLE001
        conn.rollback()
        ref = uuid.uuid4().hex[:16]
        logger.error("[defgeo-publish] confirm 失败 ref=%s: %s", ref, exc, exc_info=True)
        raise _internal_error(ref) from None
    finally:
        conn.close()
    return _respond(ConfirmResponse, payload)


async def _do_confirm(
    cur, *, tenant: int, request: Request, row: Mapping[str, Any],
    frozen: Mapping[str, Any], slot_id: str, idempotency_key: str,
    work_admission: Any,
) -> dict[str, Any]:
    exact = int(frozen["totalExactPoints"])
    funding_policy = str(frozen["fundingPolicy"])

    # ── ① work_admission(窗A 出口):里程碑 → 能力 → 审批 → scope → 余额 ──
    #    顺序由窗A 定死,这里不重排 —— 重排会让「服务没激活」被报成「余额不足」。
    budget, budget_blockers = _funding.check_and_lock_budget(
        cur, tenant_owner_id=tenant,
        accepted_snapshot_id=int(frozen["acceptedSnapshotId"]),
        service_projection_id=str(frozen["serviceProjectionId"]),
        required_points=exact,
    )
    usage = _store.budget_usage(
        cur, execution_budget_snapshot_id=str(budget["execution_budget_snapshot_id"]))
    scope_remaining = (int(budget["scope_cap_points"])
                       - int(usage["reservedPoints"]) - int(usage["committedPoints"]))
    # 🔴 里程碑必须从**库里的真事实**算,不能写死 —— 写死等于这道闸从来没拦过东西
    #    (本仓「门禁接了但接线是坏的」)。解析不出来退回 draft(拒绝 + 有出口),
    #    而不是退回 service_activated(默认放行是最贵的默认)。
    from services.defensive_geo.publish import service_milestone as _ms

    milestone = _ms.resolve(
        cur, accepted_snapshot_id=int(frozen["acceptedSnapshotId"]), tenant_owner_id=tenant,
    )
    verdict = work_admission.admit(
        work_kind="media_publication",
        milestone=milestone,
        funding_policy=funding_policy,
        balance_points=_wallet_balance(cur, tenant, funding_policy),
        required_points=exact,
        scope_remaining_points=scope_remaining,
        approval_state="required" if str(frozen["approvalRequirement"]) == "required"
                       and not budget.get("approval_ref") else "not_required",
        # 🔴 [B-3 = Codex P1-2] 这一格原来写死 ``True``。
        #    写死 = ``work_admission`` 的 ``capability_unavailable`` 那一格
        #    **从上线起一次都没有执行过**(守卫的候选集里永远不含它)——
        #    本仓反复记过的「门禁接了但接线是坏的」。
        #    现在由真实登记表回答:``readiness()`` 永不抛,自检异常也只是"没准备好"。
        capability_available=_transport.readiness().ready,
    )
    if not verdict.admitted:
        # 🔴 ACT-15 / §15.7:blocker 在 command 之前返回,**零 command / 零 freeze / 零 outbox**。
        code = {
            "service_not_activated": "PUBLISH_DECISION_NOT_CONFIRMABLE",
            "execution_funding_pending": "INSUFFICIENT_POINTS",
            "approval_required": "APPROVAL_REQUIRED",
            "scope_cap_exhausted": "INSUFFICIENT_POINTS",
            "capability_unavailable": "ADMISSION_UNAVAILABLE",
        }[str(verdict.reason)]
        raise _safe_error(code, reason_key=str(verdict.reason))
    if budget_blockers:
        raise _safe_error("INSUFFICIENT_POINTS", reason_key="insufficient_points", details={
            "requiredExactPoints": exact,
            "remainingPoints": budget_blockers[0].remaining,
            "deltaPoints": budget_blockers[0].delta,
        })

    # ── ② exact freeze ────────────────────────────────────────────────────
    command_id = "pcmd_" + uuid.uuid4().hex
    task_ref = f"defgeo_publish_{command_id}"
    handle = await _funding.freeze_exact(
        cur, funding_policy=funding_policy, exact_points=exact,
        task_ref=task_ref, brand_id=int(_brand_of(cur, slot_id)),
        actor_user_id=tenant, tenant_owner_id=tenant,
        sponsor_policy_ref=frozen.get("sponsorPolicyRef"),
        approval_ref=budget.get("approval_ref"),
    )

    # ── ③ command(同事务)────────────────────────────────────────────────
    decision = dict(frozen["decision"])
    command_hash = _ds.command_canonical_hash({
        "publishCommandId": command_id,
        "publishSlotId": slot_id,
        "decisionSnapshotHash": str(row["canonical_hash"]),
        "fundingPolicy": funding_policy,
        "exactSettlementPoints": exact,
        "parentCommandId": None,
        "commandGeneration": 1,
    })
    values: dict[str, Any] = {
        "publish_command_id": command_id,
        "publish_slot_id": slot_id,
        "decision_snapshot_id": str(row["decision_snapshot_id"]),
        "decision_snapshot_hash": str(row["canonical_hash"]),
        "command_canonical_hash": command_hash,
        "parent_command_id": None, "command_generation": 1, "lineage_kind": "root",
        "tenant_owner_id": tenant, "actor_user_id": tenant,
        "brand_id": int(_brand_of(cur, slot_id)),
        "publish_item_request_id": str(decision["publishItemRequestId"]),
        "article_revision_id": str(frozen["articleRevisionId"]),
        "article_hash": str(frozen["articleHash"]),
        "public_media_key": str(decision["publicMediaKey"]),
        "canonical_root_domain_key": str(decision["canonicalRootDomainKey"]),
        "funding_policy": funding_policy,
        "principal_kind": str(frozen["principalKind"]),
        "exact_settlement_points": exact,
        "freeze_task_ref": task_ref,
        "command_state": "queued",
        "canonical_publication_state": "not_started",
        "idempotency_key": idempotency_key,
        "request_canonical_hash": str(row["request_canonical_hash"]),
        "status_version": 1,
        **handle.as_command_columns(),
    }
    command = _store.insert_command(cur, values)

    # ── ④ outbox(同事务)──────────────────────────────────────────────────
    _store.enqueue_outbox(cur, publish_command_id=command_id, publish_slot_id=slot_id)

    # ── ⑤ snapshot CAS → consumed ─────────────────────────────────────────
    if not _store.cas_lifecycle(
        cur, decision_snapshot_id=str(row["decision_snapshot_id"]),
        tenant_owner_id=tenant, expect="open", to="consumed",
        consumed_command_id=command_id,
    ):
        raise _funding.FundingError("并发下该 snapshot 已被别的路径消费 —— 本次零副作用")

    _ds.assert_exact_points_equal(
        frozen=frozen, exact_settlement_points=exact, item_points=int(decision["exactPoints"]))
    return _confirm_payload(command, frozen, replay=False)


def _wallet_balance(cur, user_id: int, funding_policy: str) -> int:
    """余额只在**该看钱的时候**才查(ACT-04)。平台/组织腿不查个人钱包。"""
    if funding_policy != "personal_wallet":
        return 10 ** 9          # 非个人腿:余额闸不适用,由 budget cap 与审批负责
    cur.execute(
        "SELECT COALESCE(paid_points,0)+COALESCE(bonus_points,0)+COALESCE(commission_points,0) "
        "AS total FROM user_wallets WHERE user_id = %s", (user_id,))
    got = cur.fetchone()
    if got is None:
        return 0
    return int(got["total"] if isinstance(got, Mapping) else got[0])


def _accepted_snapshot_hash(cur, accepted_snapshot_id: int) -> str:
    """读**客户已接受**报价快照的指纹(现役 ``quote_pricing_snapshots.snapshot_hash``)。

    🔴 找不到时**不造一个** —— 造一个假指纹会让「这份方案绑的是哪一版客户承诺」
       永远对不上。返回全 0 的 64 位串是同样的坏(它看起来像一个真指纹)。
       所以这里抛,让 confirm/preview 走受控失败。
    """
    cur.execute(
        "SELECT snapshot_hash FROM quote_pricing_snapshots WHERE id = %s",
        (int(accepted_snapshot_id),),
    )
    got = cur.fetchone()
    if got is None:
        raise RuntimeError(
            f"accepted snapshot {accepted_snapshot_id} 不存在 —— "
            "没有客户承诺就不该有媒体决策"
        )
    value = got["snapshot_hash"] if isinstance(got, Mapping) else got[0]
    if not value or len(str(value)) != 64:
        raise RuntimeError(
            f"accepted snapshot {accepted_snapshot_id} 的 snapshot_hash 形态不合法"
        )
    return str(value)


def _assert_accepted_snapshot_access(request: Request, cur, accepted_snapshot_id: int) -> None:
    """[包E · 终审 P1-4 的真根因] ``acceptedSnapshotId`` 的**对象级归属**。

    ═══════════════════════════════════════════════════════════════════
    🔴 先证伪工单给的根因,再修真的那个
    ═══════════════════════════════════════════════════════════════════
    终审把 P1-4 记成「``GET /publish/delivery-todo`` 无归属过滤」。
    我在车头逐字复核了那条查询(``WHERE s.tenant_owner_id = %s``)——
    **行级归属过滤一直都在**,而且在初审 SHA ``842bc1388`` 上就在。
    照字面去"补一个 tenant 过滤"会补出第二个同义谓词,而真正的洞还在。

    真正缺的是**另一半**:``acceptedSnapshotId`` 从来没有被验证过属于调用方。
    · ``preview``:只验了 ``brandId``(``require_brand_access``),
      ``acceptedSnapshotId`` 直接进 ``_slot.derive_publish_slot_id`` 与
      ``_accepted_snapshot_hash``(后者 ``WHERE id = %s``,零归属条件)。
      于是甲可以用**自己的品牌** + **乙的那一版客户承诺**建出 slot 与 command:
      发布对象绑到了别人家的合同上。
    · ``delivery-todo``:行级过滤挡住了"看到别人的行",但没挡住
      "拿别人的 acceptedSnapshotId 来问" —— 返回 200 空列表,
      与本族其余五处「跨租户同形 404」的口径也不一致。

    所以归属层补在这里,一次补对两处(§14.1 对象级授权;本仓记过
    「缺资源级归属层」)。走 ``brand_id`` 是因为报价快照的归属就长在品牌上
    (``quote_pricing_snapshots.brand_id``),而 ``require_brand_access``
    是本仓现役唯一的品牌归属权威 —— 不另造第二套。

    找不到 / 不属于你,一律 ``OBJECT_NOT_FOUND``:与本族其余跨租户闸**同形**,
    不给存在性预言机(404 与 403 分开会告诉对方"这个 id 是存在的")。
    """
    from auth.brand_access import require_brand_access

    cur.execute(
        "SELECT brand_id FROM quote_pricing_snapshots WHERE id = %s",
        (int(accepted_snapshot_id),),
    )
    got = cur.fetchone()
    if got is None:
        raise _safe_error("OBJECT_NOT_FOUND")
    brand_id = got["brand_id"] if isinstance(got, Mapping) else got[0]
    try:
        require_brand_access(request, int(brand_id), allow_null=False)
    except HTTPException:
        raise _safe_error("OBJECT_NOT_FOUND") from None


def _brand_of(cur, slot_id: str) -> int:
    cur.execute("SELECT brand_id FROM defgeo_publish_slots WHERE publish_slot_id = %s", (slot_id,))
    got = cur.fetchone()
    if got is None:
        raise RuntimeError(f"slot {slot_id} 不存在")
    return int(got["brand_id"] if isinstance(got, Mapping) else got[0])


def _replay_response(command: Mapping[str, Any], frozen: Mapping[str, Any]) -> dict[str, Any]:
    """幂等重放的响应 —— **只此一处构造**(关闸前的 P1-A 与关闸内的正常流共用)。"""
    return _respond(ConfirmResponse, _confirm_payload(command, frozen, replay=True))


def _consumed_replay_or_none(
    *, snapshot_id: str, tenant: int, expected_hash: str,
    expected_version: int, idempotency_key: str,
) -> dict[str, Any] | None:
    """[fix-of-fix P1-A] 关闸**之前**的 tenant-scoped **只读**幂等重放。

    认得出"这就是已经确认过的那一次"才返回;**任何**对不上的地方都返回
    ``None``,由调用方落 ``PUBLISH_ENTRY_CLOSED``。

    🔴 四个条件缺一不可,而且都是**收紧**方向:
      ① snapshot 属于本 tenant(``get_snapshot`` 自带 tenant 谓词);
      ② ``canonical_hash`` == 她手上那一版(换 hash ⇒ 不是同一次);
      ③ ``snapshot_version`` 也要对上 —— 单子只点名了 hash,这里比单子更严:
         版本变了说明她确认的对象已经不是同一个了,宁可 403;
      ④ 原 command 的 ``idempotency_key`` == 本次请求头(换 key ⇒ 是**新命令**,
         而新命令正是关闸要挡的东西)。

    🔴 **零副作用**:不 ``lock_slot``、不写任何表、不碰 ``_funding``,
       并且 ``finally`` 里无条件 ``rollback``。判据既打行为(资金六面逐字节不变),
       也打结构(本函数体内不许出现写语句 / 资金调用)。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        row = _store.get_snapshot(
            cur, decision_snapshot_id=snapshot_id, tenant_owner_id=tenant)
        if row is None:
            return None
        if str(row["canonical_hash"]) != expected_hash:
            return None
        if int(row["snapshot_version"]) != expected_version:
            return None
        if str(row["lifecycle"]) != "consumed":
            return None
        consumed_command_id = row.get("consumed_command_id")
        if not consumed_command_id:
            return None
        cmd = _store.get_command(
            cur, publish_command_id=str(consumed_command_id), tenant_owner_id=tenant)
        if cmd is None:
            return None
        if str(cmd.get("idempotency_key") or "") != idempotency_key:
            return None
        return _replay_response(cmd, dict(row["frozen_payload"]))
    finally:
        # 只读也要显式回滚:开了事务不关,连接回池时会带着一个 idle in transaction
        # (本仓 2026-08-10 因为这个把生产打成过 503)。
        conn.rollback()
        conn.close()


def _confirm_payload(
    command: Mapping[str, Any], frozen: Mapping[str, Any], *, replay: bool,
) -> dict[str, Any]:
    decision = dict(frozen["decision"])
    identity = {k: decision[k] for k in _mi.PROVIDER_MEDIA_FIELDS}
    # 🔴 ``mediaRole`` 是内部枚举,裸串上屏 = U-1 红。服务端一并下发人话;
    #    取不到时下发空串,前端渲染留白 —— 前端不得自己翻。
    identity["mediaRoleLabel"] = _media_role_label(str(decision.get("mediaRole") or ""))
    _mi.assert_no_private_leak(identity, field="confirm.item.media")
    funding_state = str(command["funding_state"])
    return {
        "publishCommandId": str(command["publish_command_id"]),
        "decisionSnapshotId": str(command["decision_snapshot_id"]),
        "decisionSnapshotHash": str(command["decision_snapshot_hash"]),
        "commandCanonicalHash": str(command["command_canonical_hash"]),
        "exactSettlementPoints": int(command["exact_settlement_points"]),
        "statusUrl": _status_url(str(command["publish_command_id"])),
        "idempotentReplay": replay,
        "commandState": str(command["command_state"]),
        "commandReason": None,
        "fundingPolicy": str(command["funding_policy"]),
        "fundingState": funding_state,
        "fundingStateLabel": _funding_label(funding_state),
        "nextAction": _recovery_action(
            "wait", target={"kind": "publish_command",
                            "id": str(command["publish_command_id"])},
            capability="view_publish_status"),
        "item": {
            "publishItemRequestId": str(command["publish_item_request_id"]),
            "media": identity,
            "exactSettlementPoints": int(command["exact_settlement_points"]),
            "state": "queued",
            "reason": None,
            "nextAction": _recovery_action(
                "wait", target={"kind": "publish_command",
                                "id": str(command["publish_command_id"])},
                capability="view_publish_status"),
        },
    }


# ══════════════════════════════════════════════════════════════════════════
# override —— 授权服务商从**冻结的 alternatives** 里改选(MED-10 / MED-19)
# ══════════════════════════════════════════════════════════════════════════
@router.post("/decision-snapshots/{snapshot_id}/override")
async def override_decision_snapshot(
    snapshot_id: str, body: OverrideBody, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
) -> dict[str, Any]:
    """§15.7 override。

    🔴 只接受 **opaque option id + expected hash/version + actorReason**。
       不接受 supplier id、成本、理由文本以外的任何权威字段(MED-10 逐字)。
    🔴 建 child、写 parent→child lineage、把旧 snapshot CAS 成 superseded、
       写幂等结果 —— **同一事务提交**(§15.7 加粗那句)。
    """
    tenant = _tenant(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        row = _store.get_snapshot(
            cur, decision_snapshot_id=snapshot_id, tenant_owner_id=tenant)
        if row is None:
            conn.rollback()
            raise _safe_error("OBJECT_NOT_FOUND")
        slot_id = str(row["publish_slot_id"])
        _store.lock_slot(cur, slot_id)
        row = _store.get_snapshot(
            cur, decision_snapshot_id=snapshot_id, tenant_owner_id=tenant)
        assert row is not None
        frozen = dict(row["frozen_payload"])

        if str(row["canonical_hash"]) != body.expected_hash or \
                int(row["snapshot_version"]) != body.expected_version:
            conn.rollback()
            raise _safe_error("SNAPSHOT_CHANGED", reason_key="snapshot_changed", details={
                "decisionSnapshotId": snapshot_id,
                "expectedHash": str(row["canonical_hash"]),
                "expectedVersion": int(row["snapshot_version"]),
            })
        if str(row["lifecycle"]) != "open":
            conn.rollback()
            raise _safe_error("PUBLISH_DECISION_NOT_CONFIRMABLE", details={
                "decisionSnapshotId": snapshot_id,
                "successorSnapshotId": row.get("superseded_by_snapshot_id"),
                "successorSnapshotHash": row.get("superseded_by_snapshot_hash"),
                "supersessionKind": row.get("supersession_kind"),
            })

        # ── 选中的 option 必须**属于本 snapshot 的 alternatives**,恰解析一个 ──
        pool = [dict(frozen["decision"]), *[dict(a) for a in frozen["alternatives"]]]
        matches = [c for c in pool
                   if c["publicMediaOptionId"] == body.selected_public_media_option_id]
        if len(matches) != 1:
            conn.rollback()
            # 越 alternatives / 解析到多条或零条 —— 一律拒绝且零资金(MED-10)。
            raise _safe_error("VALIDATION_ERROR")
        chosen = matches[0]

        # ── commitmentImpact 由**服务端**算(客户端的 publicLoss 不具权威)──
        old_role = str(frozen["decision"]["mediaRole"])
        new_role = str(chosen["mediaRole"])
        reduces = _role_rank(new_role) < _role_rank(old_role)
        if reduces:
            # 降档 ⇒ 减少已接受交付承诺:零 child / 零 supersede / 零资金。
            conn.rollback()
            raise _safe_error(
                "CUSTOMER_COMMITMENT_RECONFIRMATION_REQUIRED",
                details={"decisionSnapshotId": snapshot_id},
                next_action=_recovery_action(
                    "new_customer_snapshot",
                    target={"kind": "quote", "id": str(frozen["acceptedSnapshotId"])},
                    capability="revise_quote"),
            )

        payload = _build_override_child(
            cur, tenant=tenant, parent=row, frozen=frozen, chosen=chosen,
            slot_id=slot_id, idempotency_key=idempotency_key,
            actor_reason=body.actor_reason,
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:                              # noqa: BLE001
        conn.rollback()
        ref = uuid.uuid4().hex[:16]
        logger.error("[defgeo-publish] override 失败 ref=%s: %s", ref, exc, exc_info=True)
        raise _internal_error(ref) from None
    finally:
        conn.close()
    return _respond(SnapshotResponse, payload)


#: 角色强弱序。**降档**的判定用它,不用价格 —— 价格低不等于降档
#: (同角色里也有便宜的),§11.4 说的是「降低媒体角色」。
_ROLE_RANK: dict[str, int] = {
    "broad_discovery": 1, "official_owned": 2, "strong_vertical": 3, "authority_anchor": 4,
}


def _role_rank(role: str) -> int:
    if role not in _ROLE_RANK:
        raise RuntimeError(f"未知 mediaRole {role!r}")
    return _ROLE_RANK[role]


def _build_override_child(
    cur, *, tenant: int, parent: Mapping[str, Any], frozen: Mapping[str, Any],
    chosen: Mapping[str, Any], slot_id: str, idempotency_key: str, actor_reason: str,
) -> dict[str, Any]:
    """建 child snapshot,并在**同一事务**里把 parent CAS 成 superseded。"""
    child_id = "pds_" + uuid.uuid4().hex
    version = int(parent["snapshot_version"]) + 1

    # 🔴 option id 绑 snapshot,所以 child 里所有候选的 option id 必须**重算**。
    #    直接沿用 parent 的 id 会让「跨 snapshot 复用 option」变成可能。
    def rebuild(entry: Mapping[str, Any], ordinal: int) -> _ds.Candidate:
        identity = _mi.PublicMediaIdentity(
            public_media_option_id=_mi.public_media_option_id(
                decision_snapshot_id=child_id,
                media_key=str(entry["publicMediaKey"]), ordinal=ordinal),
            public_media_key=str(entry["publicMediaKey"]),
            canonical_root_domain_key=str(entry["canonicalRootDomainKey"]),
            public_media_name=str(entry["publicMediaName"]),
            public_root_domain_label=str(entry["publicRootDomainLabel"]),
            media_role=str(entry["mediaRole"]),      # type: ignore[arg-type]
        )
        return _ds.Candidate(
            identity=identity,
            reason_facts=tuple((f["kind"], f["label"]) for f in entry["reasonFacts"]),
            exact_points=int(entry["exactPoints"]),
        )

    pool = [dict(frozen["decision"]), *[dict(a) for a in frozen["alternatives"]]]
    ordered = [chosen] + [c for c in pool
                          if c["publicMediaKey"] != chosen["publicMediaKey"]]
    new_decision = rebuild(ordered[0], 0)
    new_alternatives = [rebuild(c, i + 1) for i, c in enumerate(ordered[1:])]

    child_frozen, digest = _ds.freeze_snapshot(
        decision_snapshot_id=child_id,
        publish_slot_id=slot_id,
        snapshot_version=version,
        expires_at=str(parent["expires_at"]),
        accepted_snapshot_id=str(frozen["acceptedSnapshotId"]),
        accepted_snapshot_hash=str(frozen["acceptedSnapshotHash"]),
        service_projection_id=str(frozen["serviceProjectionId"]),
        service_projection_version=int(frozen["serviceProjectionVersion"]),
        plan_item_key=str(frozen["planItemKey"]),
        question_key=str(frozen["questionKey"]),
        question_revision=int(frozen["questionRevision"]),
        article_revision_id=str(frozen["articleRevisionId"]),
        article_hash=str(frozen["articleHash"]),
        pricing_catalog_version=str(frozen["pricingCatalogVersion"]),
        inventory_snapshot_version=str(frozen["inventorySnapshotVersion"]),
        execution_budget_snapshot_id=str(frozen["executionBudgetSnapshotId"]),
        execution_budget_version=int(frozen["executionBudgetVersion"]),
        global_budget=dict(frozen["globalBudget"]),
        scope_budget=dict(frozen["scopeBudget"]),
        decision=new_decision,
        alternatives=new_alternatives,
        publish_item_request_id=str(frozen["decision"]["publishItemRequestId"]),
        replacement_policy_version=str(frozen["replacementPolicyVersion"]),
        funding_policy=str(frozen["fundingPolicy"]),
        principal_kind=str(frozen["principalKind"]),
        approval_requirement=str(frozen["approvalRequirement"]),
        sponsor_policy_ref=frozen.get("sponsorPolicyRef"),
    )
    # 🔴 先 supersede parent(CAS),再插 child —— 两步同事务。
    #    并发下只有一个能把 parent 从 open 改走,失败方零副作用。
    won = _store.supersede_open(
        cur, tenant_owner_id=tenant, publish_slot_id=slot_id,
        successor_id=child_id, successor_hash=digest, kind="override",
    )
    if won is None:
        raise RuntimeError("并发下 parent 已被改走 —— 本次零副作用")
    child = _store.insert_snapshot(
        cur, decision_snapshot_id=child_id, publish_slot_id=slot_id,
        snapshot_version=version, canonical_hash=digest, frozen_payload=child_frozen,
        expires_at=str(parent["expires_at"]), idempotency_key=idempotency_key,
        request_canonical_hash=str(parent["request_canonical_hash"]),
        tenant_owner_id=tenant, parent_snapshot_id=str(parent["decision_snapshot_id"]),
    )
    logger.info("[defgeo-publish] override %s → %s reason=%r",
                parent["decision_snapshot_id"], child_id, actor_reason[:80])
    return _snapshot_response(
        child, blockers=[], options=[],
        approval_state="required" if child_frozen["approvalRequirement"] == "required"
                       else "not_required",
        blocked_reason=None,
    )


# ══════════════════════════════════════════════════════════════════════════
# retry-child —— 只从**普通** no-effect + released 建立(MED-15 / FIN-09)
# ══════════════════════════════════════════════════════════════════════════
@router.post("/commands/{command_id}/retry-child")
async def retry_child(
    command_id: str, body: RetryChildBody, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
) -> dict[str, Any]:
    """§15.7 retry-child。

    🔴 **法律 H0 命中绝不能直接 retry-child**(MED-18 逐字):
       只能先按 rule/passage 局部修复 → 新 article revision + decision + confirm。
    🔴 unknown/conflict、资金仍 frozen、已 verified/retracted、同 parent 已有 live child
       —— 五种情况全部拒绝且零副作用。

    [终审 P0-1 2026-08-23] 客户入口已关 —— 见 :data:`_CUSTOMER_PUBLISH_ENTRY_OPEN`。

    🔴 **这一条不在终审单点名的两个端点里,是我按单子自己的口径补上的**:
       单子说「其余 publish 只读端点可保留(**不冻钱**)」—— 那是判据不是名单。
       `_funding.*` 调用点机械枚举下来,会冻钱的是两条:
       ``confirm`` 与本条(L1859 ``freeze_exact``)。
       只关 confirm 的话,任何**已存在**的 command 仍能从这里再冻一笔进同一条
       没有执行器的链 —— 客户拿到的还是那个"钱冻住 → 什么都没发生 → 12 小时后退回"。
       那样入口就只是看起来关了。
    """
    _assert_customer_publish_entry_open()
    tenant = _tenant(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        parent = _store.get_command(
            cur, publish_command_id=command_id, tenant_owner_id=tenant)
        if parent is None:
            conn.rollback()
            raise _safe_error("OBJECT_NOT_FOUND")
        slot_id = str(parent["publish_slot_id"])
        _store.lock_slot(cur, slot_id)
        parent = _store.get_command(
            cur, publish_command_id=command_id, tenant_owner_id=tenant)
        assert parent is not None

        if str(parent["command_canonical_hash"]) != body.expected_command_hash or \
                int(parent["status_version"]) != body.expected_status_version:
            conn.rollback()
            raise _safe_error("SNAPSHOT_CHANGED", details={
                "publishCommandId": command_id,
                "expectedHash": str(parent["command_canonical_hash"]),
                "expectedVersion": int(parent["status_version"]),
            })

        slot_state = _slot.classify_command(
            canonical_publication_state=str(parent["canonical_publication_state"]),
            funding_state=str(parent["funding_state"]),
            legal_rule_hit=bool(parent.get("legal_rule_id")),
        )
        if not _slot.allows_retry_child(slot_state):
            conn.rollback()
            # 法律格给的是 repair,不是 retry —— admission 分支自己会算对。
            branch = _admission_branch(
                slot_state=slot_state, publish_slot_id=slot_id, command=parent)
            raise _safe_error(
                "LEGAL_RULE_HIT" if slot_state == "legal_no_effect_released"
                else "PUBLISH_DECISION_NOT_CONFIRMABLE",
                next_action=branch["nextAction"],
                details={"publishCommandId": command_id,
                         "statusUrl": _status_url(command_id)},
            )
        if _store.live_child(cur, parent_command_id=command_id) is not None:
            conn.rollback()
            raise _safe_error("IDEMPOTENCY_CONFLICT", details={
                "publishCommandId": command_id, "statusUrl": _status_url(command_id)})

        snapshot = _store.get_snapshot(
            cur, decision_snapshot_id=str(parent["decision_snapshot_id"]),
            tenant_owner_id=tenant)
        assert snapshot is not None
        frozen = dict(snapshot["frozen_payload"])
        exact = int(parent["exact_settlement_points"])

        child_id = "pcmd_" + uuid.uuid4().hex
        task_ref = f"defgeo_publish_{child_id}"
        handle = await _funding.freeze_exact(
            cur, funding_policy=str(parent["funding_policy"]), exact_points=exact,
            task_ref=task_ref, brand_id=int(parent["brand_id"]),
            actor_user_id=tenant, tenant_owner_id=tenant,
            sponsor_policy_ref=parent.get("sponsor_policy_ref"),
            approval_ref=parent.get("approval_ref"),
        )
        command_hash = _ds.command_canonical_hash({
            "publishCommandId": child_id,
            "publishSlotId": slot_id,
            "decisionSnapshotHash": str(parent["decision_snapshot_hash"]),
            "fundingPolicy": str(parent["funding_policy"]),
            "exactSettlementPoints": exact,
            "parentCommandId": command_id,
            "commandGeneration": int(parent["command_generation"]) + 1,
        })
        # 🔴 血缘逐项冻结:parent / decision / article revision+hash /
        #    public media+root identity / 原 question-plan lineage(FIN-09)。
        values: dict[str, Any] = {
            "publish_command_id": child_id,
            "publish_slot_id": slot_id,
            "decision_snapshot_id": str(parent["decision_snapshot_id"]),
            "decision_snapshot_hash": str(parent["decision_snapshot_hash"]),
            "command_canonical_hash": command_hash,
            "parent_command_id": command_id,
            "command_generation": int(parent["command_generation"]) + 1,
            "lineage_kind": "retry_child",
            "tenant_owner_id": tenant, "actor_user_id": tenant,
            "brand_id": int(parent["brand_id"]),
            "publish_item_request_id": str(parent["publish_item_request_id"]),
            "article_revision_id": str(parent["article_revision_id"]),
            "article_hash": str(parent["article_hash"]),
            "public_media_key": str(parent["public_media_key"]),
            "canonical_root_domain_key": str(parent["canonical_root_domain_key"]),
            "funding_policy": str(parent["funding_policy"]),
            "principal_kind": str(parent["principal_kind"]),
            "exact_settlement_points": exact,
            "freeze_task_ref": task_ref,
            "command_state": "queued",
            "canonical_publication_state": "not_started",
            "idempotency_key": idempotency_key,
            "request_canonical_hash": str(parent["request_canonical_hash"]),
            "status_version": 1,
            **handle.as_command_columns(),
        }
        child = _store.insert_command(cur, values)
        _store.enqueue_outbox(cur, publish_command_id=child_id, publish_slot_id=slot_id)
        payload = _confirm_payload(child, frozen, replay=False)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except _funding.FundingError:
        conn.rollback()
        raise _safe_error("INSUFFICIENT_POINTS", reason_key="insufficient_points") from None
    except Exception as exc:                              # noqa: BLE001
        conn.rollback()
        ref = uuid.uuid4().hex[:16]
        logger.error("[defgeo-publish] retry-child 失败 ref=%s: %s", ref, exc, exc_info=True)
        raise _internal_error(ref) from None
    finally:
        conn.close()
    return _respond(ConfirmResponse, payload)
