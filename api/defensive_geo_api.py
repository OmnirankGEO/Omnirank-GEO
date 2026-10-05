"""防御型 GEO v2 façade:问题计划 + 正式诊断两阶段预览/确认。

规格 §15.1-15.3 / §0.5.5 U-2 @ spec e710be6c2。

四条贯穿全文件的规矩
--------------------
1. **wire key 一律 camelCase**(§15 开头):Pydantic 显式 alias,
   不依赖任何全局 snake↔camel 转换(那种转换本仓没有)。
2. **strict schema**:请求与响应都 ``extra='forbid'``。
   响应模型也 forbid —— G-4 要的正是「response 多返回一键 → **受控失败**而非裸 500」。
3. **错误只走 allowlist 信封**(§15.8):`_safe_error` 是唯一出口。
   raw detail / SQL / host / 供应商名一律不出去。
4. **每个响应都带人话**(§0.5.5 U-2 + 本窗补充令):
   ``userLabel`` / ``publicExplanation`` / ``nextAction.label`` 非空且零内部词,
   由 ``copy_registry`` 下发,前端不得自造。

🔴 本 façade **零资金副作用**:preview 只写 preview 行,confirm 只做 CAS 标记 +
   委托现役诊断资金链。它**不**自己冻结/扣除任何算力,也**不**创建第二套
   settlement enum(§3.5)。真正的 freeze/commit/release 仍归
   ``services/diagnosis_runs.py`` 与受保护的 ``middleware/billing.py``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config.ai_engines import unknown_engines
from services.defensive_geo import plan_store
from services.defensive_geo.copy_registry import (
    COPY_REGISTRY_VERSION,
    assert_public_copy_clean,
    user_label,
)
from services.defensive_geo.funding_projection import (
    ProjectionError,
    allowed_insufficient_actions,
    validate_preview_funding,
)
from services.defensive_geo.question_plan import (
    CANONICAL_HASH_VERSION,
    PlanIdentityError,
    PlannedQuestion,
    canonical_hash,
    counts,
    new_plan_id,
    normalize_platform_keys,
    question_identity_key,
    request_content_hash,
    run_request_hash,
    validate_plan,
)
from services.defensive_geo.run_status_projection import PROJECTION_VERSION
from services.defensive_geo.typed_error_route import TypedErrorRoute

logger = logging.getLogger("GEO-DefensiveGeoAPI")

#: [返修③ P1-5] ``route_class`` 是**一处**:本 router 上现在与将来的每个端点,
#: 无论抛出什么,客户拿到的都是同一种信封。逐个 handler 包 try 会漏,而漏掉的
#: 那一处不会有判据变红。
router = APIRouter(prefix="/api/defensive-geo", tags=["防御型 GEO · v2 façade"],
                   route_class=TypedErrorRoute)

#: preview 有效期。写死在服务端 —— 客户端不得自报(自报 = 自己给自己延期)。
_PREVIEW_TTL_MINUTES = 30
_PLAN_TTL_MINUTES = 120


# ══════════════════════════════════════════════════════════════════════════
# §15.8 错误信封 —— 唯一出口
# ══════════════════════════════════════════════════════════════════════════
#: code → (HTTP, 是否可重试)。**闭表**:不在表里的 code 一律走 500 分支的
#: 受控信封,而不是把内部异常原样吐出去。
_ERROR_TABLE: dict[str, tuple[int, bool]] = {
    "VALIDATION_FAILED": (422, False),
    "NOT_FOUND": (404, False),
    "FORBIDDEN": (403, False),
    "PREVIEW_EXPIRED": (409, False),
    "SNAPSHOT_CHANGED": (409, False),
    "QUESTION_PLAN_NOT_RUNNABLE": (409, False),
    "IDEMPOTENCY_CONFLICT": (409, False),
    "APPROVAL_REQUIRED": (403, False),
    "APPROVAL_REJECTED": (403, False),
    "INSUFFICIENT_POINTS": (402, False),
    "POLICY_UNAVAILABLE": (503, True),
    # [工单 E3-4 · P1-10 · Codex 二审] 这次监测是**老链**跑的,新链的进度口径
    # 对它没有意义。409 + **不可重试**:再点一次仍然是同一次老链运行 ——
    # retryable=true 会让前端自动重试一个永远不会变的答案。
    # 🔴 与 NOT_FOUND 分开:任务是**存在**的,只是这条口径答不了它。
    #    合成 404 会让她以为客户的监测丢了。
    "MONITORING_LEGACY_RUN": (409, False),
    "INTERNAL_ERROR": (500, True),
    # [门二 G6] 平台直营账号发起 —— 平台成本账在门栈不可用时的**typed 拒绝**。
    # 403 而不是 500:这不是系统崩了,是"这个账号走不了这条路,换个账号就行",
    # 而且她手边就有可用的服务商账号。裸 500 会让她以为是系统坏了、反复重试。
    "PLATFORM_DIRECT_UNSUPPORTED": (403, False),
    # [返修③ P1-5] 没登录。过去这条在 report 路由里是手写的裸信封
    # {"code": "NOT_AUTHENTICATED"} —— 三形态里的「minimal typed」那一种。
    "NOT_AUTHENTICATED": (401, False),
    # [包F ② 2026-08-23] ``MONITORING_PROGRESS_CLOSED`` **已退役**。
    # 一期关进度入口的唯一理由是"下面的账本还没接线";包F ① 把执行链
    # 五点接上后前置条件消失,闸与它的 code 一起撤 —— 留一个再
    # 也不会被 raise 的 code 在这张表里,就是留一个"看起来还有闸"
    # 的死枚举,而下一个人会以为这条链仍然可能被关。
}

# ══════════════════════════════════════════════════════════════════════════
# [门二 G5] 每个 code 的**默认**人话 + typed 下一步
# ══════════════════════════════════════════════════════════════════════════
# 为什么做成默认表,而不是去逐个改 26 个调用点:
# 逐个改的问题是"下次新增一个 raise 又会忘" —— 那类修复的半衰期是一个星期。
# 默认表 + 构造期强制,让**裸信封在代码层就构造不出来**。
#
# 格式:code -> (reason_key, action_kind, action_target)
_ERROR_DEFAULTS: dict[str, tuple[str | None, str, dict]] = {
    "VALIDATION_FAILED": ("validation_failed", "fix_input", {"kind": "page", "page": "form"}),
    "NOT_FOUND": ("not_found", "back_to_list", {"kind": "page", "page": "history"}),
    "FORBIDDEN": ("forbidden", "contact_support", {"kind": "page", "page": "support"}),
    "PREVIEW_EXPIRED": ("preview_expired", "new_preview", {"kind": "page", "page": "diagnosis"}),
    "SNAPSHOT_CHANGED": ("snapshot_changed", "new_preview", {"kind": "page", "page": "diagnosis"}),
    "QUESTION_PLAN_NOT_RUNNABLE": (
        "question_plan_not_runnable", "review_question_plan", {"kind": "page", "page": "history"}),
    # 🔴 唯一一个**刻意没有** reason 的:见 _NEVER_SURFACED_CODES。
    "IDEMPOTENCY_CONFLICT": (None, "view_existing_command", {"kind": "page", "page": "history"}),
    "APPROVAL_REQUIRED": ("approval_required", "request_approval", {"kind": "page", "page": "approval"}),
    "APPROVAL_REJECTED": ("approval_rejected", "contact_owner", {"kind": "page", "page": "approval"}),
    "INSUFFICIENT_POINTS": ("insufficient_points", "top_up", {"kind": "page", "page": "wallet"}),
    "POLICY_UNAVAILABLE": ("policy_unavailable", "contact_support", {"kind": "page", "page": "support"}),
    # [工单 E3-4 · P1-10] 老链跑的那次监测 —— 指回她**看得到结果**的那个页面。
    "MONITORING_LEGACY_RUN": (
        "monitoring_legacy_run", "view_legacy_monitoring",
        {"kind": "page", "page": "monitoring"}),
    "INTERNAL_ERROR": ("internal_error", "retry_later", {"kind": "page", "page": "diagnosis"}),
    "PLATFORM_DIRECT_UNSUPPORTED": (
        "platform_direct_unsupported", "switch_to_agent_account", {"kind": "page", "page": "login"}),
    "NOT_AUTHENTICATED": ("not_authenticated", "sign_in", {"kind": "page", "page": "login"}),
}

#: 🔴 §0.5 L124 逐字:``IDEMPOTENCY_CONFLICT`` **由前端静默处理、永不上屏**。
#:
#: 所以它是唯一允许**没有** ``publicExplanation`` 的 code —— 但它仍然必须带
#: typed ``nextAction``(§15.8 那一行给的是「使用新请求编号,原请求可查看」,
#: 那是给前端**照做**的,不是给用户**看**的)。
#:
#: 为什么不干脆也给它一句用户句:后端一旦下发,迟早有人把它显示出来。
#: 「registry 里根本没有」比「有但请别显示」硬得多。
#: 🔴 这个集合**只许缩不许扩**:判据 `test_never_surfaced_set_stays_minimal` 钉住。
_NEVER_SURFACED_CODES = frozenset({"IDEMPOTENCY_CONFLICT"})


#: ``PlanIdentityError.code`` → 对客 reason_key。
#:
#: 🔴 认不出的 code 取到 ``None`` ⇒ 落通用 ``validation_failed`` 句。
#:    这是**故意**的向前兼容:将来有人在 ``validate_plan`` 加一条 raise 而忘了
#:    给 code,表现回到 2026-09-02 之前(通用句),不会 500 也不会空白。
#: 🔴 键必须与 ``services/defensive_geo/question_plan.py`` 各 raise 点的 code 一致;
#:    值必须在 ``copy_registry._REASON_EXPLANATION`` 里有句子 ——
#:    没有的话 ``_safe_error`` 的构造期 fail-closed 会当场炸,不会静默上屏空白。
_PLAN_REASON_BY_CODE: dict[str | None, str] = {
    "plan_empty": "plan_empty",
    "plan_hybrid_needs_both_sides": "plan_hybrid_needs_both_sides",
    "plan_side_mismatch": "plan_side_mismatch",
    "plan_question_blank": "plan_question_blank",
    "plan_identity": "plan_identity",
}


def _safe_error(
    code: str,
    *,
    reason_key: str | None = None,
    next_action: dict | None = None,
    details: dict | None = None,
) -> HTTPException:
    """构造对外错误。

    🔴 ``publicExplanation`` 从 copy_registry 取。取不到就**不带**这一格,
       而不是回落成 code 本身 —— 回落等于把 ``PREVIEW_EXPIRED`` 这种词摆到她面前。
       U-2 明确 ``IDEMPOTENCY_CONFLICT`` 永不上屏,它就是靠"registry 里没有"实现的。
    🔴 ``details`` 是**闭集**:只允许下面白名单里的键。
       开放 details 等于给 raw detail 开后门(§19 变异 156)。
    """
    status, retryable = _ERROR_TABLE.get(code, _ERROR_TABLE["INTERNAL_ERROR"])
    payload: dict[str, Any] = {"code": code, "retryable": retryable}

    # [门二 G5] 调用方没显式给,就用默认表补 —— 补完再强制。
    default_reason, default_kind, default_target = _ERROR_DEFAULTS.get(
        code, _ERROR_DEFAULTS["INTERNAL_ERROR"])
    if reason_key is None:
        reason_key = default_reason

    if reason_key:
        from services.defensive_geo.copy_registry import try_user_label

        explanation = try_user_label("reason", reason_key)
        if explanation:
            payload["publicExplanation"] = assert_public_copy_clean(
                explanation, field=f"error.{code}.publicExplanation"
            )

    if next_action is None:
        next_action = _action(default_kind, target=default_target)
    payload["nextAction"] = next_action

    # ── 构造期 fail-closed:裸信封在这里就构造不出来 ──────────────────
    # 判据当然也会打真 HTTP,但那只能覆盖判据跑到的分支。这道门覆盖**全部**分支,
    # 包括将来某个人新加的那一条 raise。
    if code not in _NEVER_SURFACED_CODES and "publicExplanation" not in payload:
        raise RuntimeError(
            f"SafeError {code} 没有 publicExplanation —— 她会看到一片空白然后去猜。"
            f"请在 copy_registry._REASON_EXPLANATION 补 {reason_key!r},"
            "或在 _ERROR_DEFAULTS 给它一个 reason_key。"
            "(只有 §0.5 L124 明令永不上屏的 code 才允许没有,见 _NEVER_SURFACED_CODES)"
        )
    if not payload.get("nextAction", {}).get("label"):
        raise RuntimeError(
            f"SafeError {code} 的 nextAction 没有可渲染的 label —— "
            "任何阻塞必须自带解决方案(§0.5.6),没有下一步的错误就是死路。"
        )

    if details:
        allowed = {"planId", "planRevision", "latestRevisionAvailable",
                   "previewId", "expectedHash", "commandId", "statusUrl"}
        bad = set(details) - allowed
        if bad:
            # 不静默丢弃 —— 静默丢弃会让「我以为带上了」和「真带上了」长得一样。
            raise RuntimeError(f"SafeError details 出现非白名单键 {sorted(bad)}")
        payload["details"] = details

    return HTTPException(status_code=status, detail=payload)


def _action(kind: str, *, target: dict) -> dict:
    """typed nextAction。label 从 registry 取,取不到直接抛 ——

    §0.5.6 铁律「任何阻塞与错误必须自带解决方案」的可执行形式:
    没有 label 的动作是死动作,她看不见按钮上写什么。
    """
    # 🔴 actionRef 由 (kind, target) **确定性**导出,不是 uuid4。
    #
    # 换掉随机值的直接原因是一条既有判据当场变红:NOT_FOUND 在门二 G5 之后开始
    # 携带 nextAction,于是「跨租户 404 与不存在 404 必须逐字同形」不再成立 ——
    # 两次响应的 actionRef 每次都不同。随机 ref 本身不泄露对象存在性,
    # 但它让**任何**同形判据永远无法成立,等于把那道防泄露的门拆了。
    #
    # 确定性还顺带让这个字段真的成为一个「ref」:同一个动作在任何地方都是同一个串,
    # 可以拿去对账/埋点。随机串谁也对不上,那只是装饰。
    # 本文件另一处(pending command 的 wait 动作)本来就是确定性的
    # (``"defgeo:wait:" + command_id[:12]``),这里与它统一。
    #
    # 不泄露:只由动作自身导出,不掺任何"对象存不存在"的信息。
    fingerprint = hashlib.sha256(
        json.dumps({"kind": kind, "target": target}, sort_keys=True,
                   ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "kind": kind,
        "label": assert_public_copy_clean(user_label("action", kind), field=f"action.{kind}"),
        "actionRef": f"defgeo:{kind}:{fingerprint}",
        "target": target,
    }


# ══════════════════════════════════════════════════════════════════════════
# 鉴权 —— 归属每请求现做
# ══════════════════════════════════════════════════════════════════════════
def _tenant(request: Request) -> int:
    user = getattr(request.state, "user", None)
    if not user or not user.get("user_id"):
        raise _safe_error("FORBIDDEN")
    return int(user["user_id"])


def _platform_direct_service_user_id() -> int | None:
    """现役平台账腿。取不到返回 ``None``(由调用方翻成 typed 拒绝,不是 500)。

    🔴 复用 ``services.commercial_service_routing.get_platform_direct_service_user_id``
       —— 那是本仓「平台账」的**现役 SSOT**(``scheduler.py`` 的
       ``_billing_uid_for_sub`` 在 ``billing_mode=='platform'`` 时走的就是它),
       窗C 的 ``publish_funding._platform_service_user_id`` 也调它。
       三条链共用一个换号谓词,不造第三种 —— 资金上「同一谓词写两处」最贵。

    🔴 [包E R2] 实现搬到 ``services.defensive_geo.payer_classification`` ——
       发布链的预算签发者在 services 层,够不到 ``api/``。这里保留同名别名,
       调用点与行为逐位不变。
    """
    from services.defensive_geo.payer_classification import platform_direct_service_user_id

    return platform_direct_service_user_id()


def _classify_payer(request: Request) -> tuple[str, str, str | None]:
    """按发起人身份定 fundingPolicy(§15.3 四格矩阵)。

    [门二 G6] 原来这里**硬编码** ``personal_wallet`` —— 于是 admin 发起时会一路
    走到个人钱包腿,撞上 billing 的 admin 免单旁路(``freeze_points`` 对
    ``is_admin`` 直接返回 ``freeze_id=None`` 且**不写任何表**),被那把零记账守卫
    拦下抛 RuntimeError → 对外是**裸 500**。她看到的是"系统坏了",
    而真相是"这个账号该走另一条钱腿"。

    分类只在**服务端**做,客户端一个字都不参与:
      · admin,或本人就是平台直营服务账号 → ``admin_platform_ledger``
        (对外 fundingState=``exempt_recorded``:用户钱包不扣,但平台账真记账);
      · 其余 → ``personal_wallet``。

    ``organization_budget`` 与 ``sponsor_platform_ledger`` 两格**不在此签发**:
    前者的腿尚未接通,后者必须绑已签 sponsor policy —— 没有 policy 就签发它,
    等于让平台白掏钱且无从追责。

    🔴 [包E R2] 判别**逻辑本身**搬到了
       ``services.defensive_geo.payer_classification.classify_payer``(纯函数)。
       理由:发布链的 ``activation_materializer`` 在 services 层够不到这里,
       于是它一直吃 ``derive()`` 的默认值 ``personal_wallet`` ——
       **同一个人在两条链上被判成两个付款方**。同一谓词不写两处。
       本函数只剩「从 ``request.state.user`` 取事实」这一件事,返回值逐位同形。
       等价判据 ``test_r2_0x`` 逐格核对搬家前后输出相同(搬家 = 行为零变化)。
    """
    from services.defensive_geo.payer_classification import classify_request_user

    got = classify_request_user(getattr(request.state, "user", None))
    return (got.funding_policy, got.principal_kind, got.sponsor_policy_ref)


def _require_brand(request: Request, brand_id: int) -> None:
    """复用现役 RBAC,不另造第二套(§0.5.1-5 同精神)。"""
    from auth.brand_access import require_brand_access

    try:
        require_brand_access(request, brand_id, allow_null=False)
    except HTTPException:
        # 🔴 跨租户与不存在返回**同形** —— 不泄露对象存在性(ACT-02)。
        raise _safe_error("NOT_FOUND") from None


# ══════════════════════════════════════════════════════════════════════════
# DTO
# ══════════════════════════════════════════════════════════════════════════
class _Strict(BaseModel):
    """请求与响应共用底座:未知字段 422,camelCase alias。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class QuestionInput(_Strict):
    text: str = Field(min_length=1, max_length=500)
    mode_side: Literal["defensive", "offensive"] = Field(alias="modeSide")
    family_key: str = Field(alias="familyKey", min_length=1, max_length=64)
    brand_exposure: Literal["named", "unnamed", "comparison"] = Field(alias="brandExposure")


class QuestionPlanPreviewRequest(_Strict):
    client_request_id: str = Field(alias="clientRequestId", min_length=1, max_length=120)
    brand_id: int = Field(alias="brandId", ge=1)
    profile_revision_id: str = Field(alias="profileRevisionId", min_length=1, max_length=120)
    mode: Literal["defensive", "offensive", "hybrid"]
    questions: list[QuestionInput] = Field(min_length=1, max_length=100)


class PlannedQuestionOut(_Strict):
    question_identity_key: str = Field(alias="questionIdentityKey")
    question_revision: int = Field(alias="questionRevision")
    global_ordinal: int = Field(alias="globalOrdinal")
    text: str
    mode_side: str = Field(alias="modeSide")
    family_key: str = Field(alias="familyKey")
    brand_exposure: str = Field(alias="brandExposure")


class QuestionPlanCounts(_Strict):
    defensive: int
    offensive: int
    total: int


#: 诊断这条线的功能编码。原来是两处字面量(算价一处、本模块别处一处),
#: 现在只此一处 —— 同一个谓词写两处必有一处没人验。
DIAGNOSIS_FEATURE_CODE = "geo_diagnosis"


class PricingRuleOut(_Strict):
    """把**计价规则的参数**发给前端,让它自己说人话。

    🔴 [2026-09-02] 为什么要有这一格:Owner 要在「一道题都还没有」时就显示
       「起步价 X · 超 N 题每题 Y」。在此之前没有任何接口给这三个数,
       前端只能写死 —— 而价目一改,写死的文案当场变成骗人。
       这里发的是**规则参数本身**,不是某次的价格:改价目或改规则,
       前端下一次拿到的就是新的,不需要跟着改代码。

    🔴 三个数都来自 SSOT,不许在本模块出现字面量:
       ``basePoints`` 走 ``_price_for_plan``(与真实算价**同一个函数**,
       所以它必然与 ``feature_pricing`` 现役行同源);
       ``freeQuestions`` / ``extraPerQuestion`` / ``ruleVersion`` 取
       ``services/diagnosis_question_pricing`` 的常量。
    """

    base_points: int = Field(alias="basePoints")
    free_questions: int = Field(alias="freeQuestions")
    extra_per_question: int = Field(alias="extraPerQuestion")
    rule_version: str = Field(alias="ruleVersion")


def _pricing_rule_out(feature_code: str = DIAGNOSIS_FEATURE_CODE) -> "PricingRuleOut":
    """规则参数的**唯一**产出口。两个 QuestionPlanResponse 构造点都调它。

    基价走 ``_base_points_for`` —— 与 ``_price_for_plan`` **同一个取数口**,
    所以必然与真实算价同源,而又**不新增 `_price_for_plan` 的调用点**
    (第三趟那把结构锁钉着它每个调用点都要传题数变量;第一版我从那里借基价,
    等于给别人的锁凭空加了一项分母,当场把合法代码判红)。
    """
    from services.diagnosis_question_pricing import (
        EXTRA_POINTS_PER_QUESTION,
        FREE_CUSTOM_QUESTIONS,
        PRICING_RULE_VERSION,
    )

    return PricingRuleOut(
        basePoints=_base_points_for(feature_code),
        freeQuestions=FREE_CUSTOM_QUESTIONS,
        extraPerQuestion=EXTRA_POINTS_PER_QUESTION,
        ruleVersion=PRICING_RULE_VERSION,
    )


class QuestionPlanResponse(_Strict):
    plan_id: str = Field(alias="planId")
    plan_revision: int = Field(alias="planRevision")
    canonical_hash: str = Field(alias="canonicalHash")
    canonical_hash_version: str = Field(alias="canonicalHashVersion")
    brand_id: int = Field(alias="brandId")
    profile_revision_id: str = Field(alias="profileRevisionId")
    mode: str
    #: 🔴 补充令:人话字段。前端不得拿 mode 枚举自己编话。
    mode_user_label: str = Field(alias="modeUserLabel")
    questions: list[PlannedQuestionOut]
    counts: QuestionPlanCounts
    expires_at: str = Field(alias="expiresAt")
    copy_registry_version: str = Field(alias="copyRegistryVersion")
    idempotent_replay: bool = Field(alias="idempotentReplay")
    #: 计价规则参数(不是本次价格)—— 前端据此说「起步价 X · 超 N 题每题 Y」。
    pricing_rule: PricingRuleOut = Field(alias="pricingRule")


class RunPreviewRequest(_Strict):
    question_plan_id: str = Field(alias="questionPlanId")
    question_plan_revision: int = Field(alias="questionPlanRevision", ge=1)
    profile_revision_id: str = Field(alias="profileRevisionId", min_length=1, max_length=120)
    platform_keys: list[str] = Field(alias="platformKeys", min_length=1, max_length=12)

    @field_validator("platform_keys", mode="before")
    @classmethod
    def _normalize_platform_keys(cls, value):
        """[返修② 2] 入参在**进模型之前**就规范成有序去重集合。

        🔴 为什么放在 ``mode="before"``:``min_length/max_length`` 是核心 schema
           约束,``mode="after"`` 会跑在它们**之后** —— 那样 ``max_length=12``
           限的是「提交了几个」而不是「有几个不同的」,重复键会先把上限占满。
           放在前面,上限才是它该有的意思:**最多 12 个不同平台**。

        🔴 为什么规范化只此一处:``planned_cells``(工作量)与幂等 hash 都从
           这一个列表派生。任一处自己再去重一遍,就必有一处没人验
           (本仓记过「同一谓词写两处 ⇒ 必有一处没人验」)。
           🔴 [P0 2026-09-02] 原文这里还写着「报价、冻结额」也从这个列表派生 ——
           **那已经不成立**:Owner 拍板改成按题计价后,平台数彻底不进计价。
           留着那句会让下一个人以为「多选一个平台会多花钱」。
        """
        return normalize_platform_keys(value)


class NextActionOut(_Strict):
    kind: str
    label: str
    action_ref: str = Field(alias="actionRef")
    target: dict


class RunPreviewResponse(_Strict):
    preview_id: str = Field(alias="previewId")
    canonical_hash: str = Field(alias="canonicalHash")
    lifecycle: str
    #: 补充令要求的三件人话
    lifecycle_user_label: str = Field(alias="lifecycleUserLabel")
    funding_policy: str = Field(alias="fundingPolicy")
    funding_policy_user_label: str = Field(alias="fundingPolicyUserLabel")
    campaign_mode: str = Field(alias="campaignMode")
    mode_user_label: str = Field(alias="modeUserLabel")
    question_plan_id: str = Field(alias="questionPlanId")
    question_plan_revision: int = Field(alias="questionPlanRevision")
    planned_cells: int = Field(alias="plannedCells")
    base_points: int = Field(alias="basePoints")
    extra_points: int = Field(alias="extraPoints")
    exact_total_points: int = Field(alias="exactTotalPoints")
    #: U-4:「本次体检消耗你的算力 X」—— 一句话,不是让她从三个数字里自己算。
    cost_user_label: str = Field(alias="costUserLabel")
    expires_at: str = Field(alias="expiresAt")
    can_confirm: bool = Field(alias="canConfirm")
    next_action: NextActionOut = Field(alias="nextAction")
    copy_registry_version: str = Field(alias="copyRegistryVersion")
    run_status_projection_version: str = Field(alias="runStatusProjectionVersion")
    idempotent_replay: bool = Field(alias="idempotentReplay")


# ══════════════════════════════════════════════════════════════════════════
# 端点
# ══════════════════════════════════════════════════════════════════════════
def _db():
    """取一条池化连接。

    🔴 用 ``get_connection()`` 而不是 ``get_db()``:后者是 ``@contextmanager``,
       ``conn = get_db()`` 拿到的是**上下文管理器对象**,再 ``.cursor()`` 直接
       AttributeError → 端点 500。第一版就是这么写的,真 HTTP 判据当场把它打红
       (纯函数判据看不见这一层 —— 这正是「真 HTTP 判据必须打真库」的价值)。
       调用方负责 ``close()``(池化模式下 close 即归还)。
    """
    from db.connection import get_connection

    return get_connection()


@router.post("/question-plans/preview", response_model=QuestionPlanResponse,
             response_model_by_alias=True)
async def create_question_plan_preview(
    body: QuestionPlanPreviewRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> QuestionPlanResponse:
    """签发一份冻结题单(§15.2)。零资金副作用。"""
    tenant = _tenant(request)
    _require_brand(request, body.brand_id)

    plan_id = new_plan_id()
    questions = tuple(
        PlannedQuestion(
            question_identity_key=question_identity_key(plan_id, i),
            question_revision=1,
            global_ordinal=i,
            text=q.text.strip(),
            mode_side=q.mode_side,
            family_key=q.family_key,
            brand_exposure=q.brand_exposure,
            origin="customer",
            classifier_version=None,
        )
        for i, q in enumerate(body.questions, start=1)
    )
    try:
        validate_plan(body.mode, questions)
    except PlanIdentityError as exc:
        # 🔴 [2026-09-02] 按规则给**具体**的一句,不再一律落通用句。
        #    以前所有规则都 reason_key=None ⇒ 她看到「这次提交的内容有一处填得不对」,
        #    不知道哪一处、也没有下一步 —— 违反「提示二选一:有明细 + 有修复动作」。
        #    分类走异常自带的 ``code``(机器可读),**不按消息文本匹配**:
        #    裸串匹配会在有人改一个字时悄悄退回通用句,而且没有判据会红。
        #    认不出的 code ⇒ reason_key=None ⇒ 仍落通用句(向前兼容:
        #    将来新加的 raise 忘了给 code,不会炸,只是回到今天的表现)。
        # 🔴 [2026-09-02 · Deploy 实证] 这条拒绝以前**一行日志都不打**
        #    (两容器 90 分钟命中 0)⇒ 运维侧完全不可诊断:线上有人被拦下,
        #    我们既不知道撞的是哪条规则,也不知道撞了多少次。
        #    只打**规则文本**(它由本仓自己写死,不含用户输入):不打 body、
        #    不打题文、不打 token —— 题文是用户内容,进日志就是泄漏面。
        logger.info("[defgeo] validate_plan 拒绝: %s", exc)
        reason = _PLAN_REASON_BY_CODE.get(getattr(exc, "code", None))
        target: dict[str, Any] = {"kind": "question_plan"}
        side = getattr(exc, "side", None)
        if side:
            # 让按钮能直接落到出问题的那一侧,而不是让她自己找。
            target["side"] = side
        raise _safe_error("VALIDATION_FAILED", reason_key=reason,
                          next_action=_action("change_plan", target=target)) from exc

    d, o, t = counts(questions)
    q_set_version = "defgeo-question-set-v1"
    chash = canonical_hash(
        brand_id=body.brand_id, profile_revision_id=body.profile_revision_id,
        mode=body.mode, question_set_version=q_set_version, questions=questions,
    )
    # 🔴 幂等键吃**请求内容**,不吃 canonical_hash ——
    #    canonical_hash 含服务端每次新生成的 plan_id 派生身份键,
    #    拿它做幂等等于永不命中(见 question_plan.request_content_hash 的说明)。
    content_hash = request_content_hash(
        brand_id=body.brand_id, profile_revision_id=body.profile_revision_id,
        mode=body.mode, question_set_version=q_set_version,
        questions=tuple(
            (q.text.strip(), q.mode_side, q.family_key, q.brand_exposure)
            for q in body.questions
        ),
    )
    expires = datetime.now(timezone.utc) + timedelta(minutes=_PLAN_TTL_MINUTES)

    conn = _db()
    try:
        with conn.cursor() as cur:
            row = plan_store.insert_plan_revision(
                    cur, plan_id=plan_id, plan_revision=1, tenant_owner_user_id=tenant,
                    brand_id=body.brand_id, profile_revision_id=body.profile_revision_id,
                    mode=body.mode, question_set_version=q_set_version, canonical_hash=chash,
                    questions=questions, defensive_count=d, offensive_count=o, total_count=t,
                    client_request_id=body.client_request_id,
                request_content_hash=content_hash, expires_at=expires,
                created_by_user_id=tenant,
            )
        conn.commit()
    except plan_store.PlanNotFound:
        conn.rollback()
        raise _safe_error("IDEMPOTENCY_CONFLICT") from None
    finally:
        conn.close()

    replayed = str(row["plan_id"]) != plan_id
    stored = row["frozen_payload"]["questions"] if replayed else [
        {
            "question_identity_key": q.question_identity_key,
            "question_revision": q.question_revision,
            "global_ordinal": q.global_ordinal,
            "text": q.text,
            "mode_side": q.mode_side,
            "family_key": q.family_key,
            "brand_exposure": q.brand_exposure,
        }
        for q in questions
    ]

    return QuestionPlanResponse(
        planId=str(row["plan_id"]),
        planRevision=row["plan_revision"],
        canonicalHash=row["canonical_hash"],
        canonicalHashVersion=CANONICAL_HASH_VERSION,
        brandId=row["brand_id"],
        profileRevisionId=row["profile_revision_id"],
        mode=row["mode"],
        modeUserLabel=assert_public_copy_clean(user_label("mode", row["mode"]), field="modeUserLabel"),
        questions=[
            PlannedQuestionOut(
                questionIdentityKey=q["question_identity_key"],
                questionRevision=q["question_revision"],
                globalOrdinal=q["global_ordinal"],
                text=q["text"], modeSide=q["mode_side"],
                familyKey=q["family_key"], brandExposure=q["brand_exposure"],
            ) for q in stored
        ],
        counts=QuestionPlanCounts(
            defensive=row["defensive_count"], offensive=row["offensive_count"],
            total=row["total_count"],
        ),
        pricingRule=_pricing_rule_out(),
        expiresAt=row["expires_at"].isoformat(),
        copyRegistryVersion=COPY_REGISTRY_VERSION,
        idempotentReplay=replayed,
    )


@router.get("/question-plans/{plan_id}", response_model=QuestionPlanResponse,
            response_model_by_alias=True)
async def get_question_plan(
    plan_id: str, revision: int, request: Request
) -> QuestionPlanResponse:
    """按 **exact revision** 恢复(REV-11)。

    ``revision`` 是**必填 query** —— 没有「不传就给 latest」这条路。
    静默升 latest 会把旧报告绑到新题单上(§19 变异 84)。
    """
    tenant = _tenant(request)
    conn = _db()
    try:
        with conn.cursor() as cur:
            row = plan_store.get_plan_exact_revision(
                cur, plan_id=plan_id, plan_revision=revision, tenant_owner_user_id=tenant)
    except plan_store.PlanNotFound:
        raise _safe_error("NOT_FOUND") from None
    finally:
        conn.close()

    return QuestionPlanResponse(
        planId=str(row["plan_id"]), planRevision=row["plan_revision"],
        canonicalHash=row["canonical_hash"], canonicalHashVersion=CANONICAL_HASH_VERSION,
        brandId=row["brand_id"], profileRevisionId=row["profile_revision_id"],
        mode=row["mode"],
        modeUserLabel=assert_public_copy_clean(user_label("mode", row["mode"]), field="modeUserLabel"),
        questions=[
            PlannedQuestionOut(
                questionIdentityKey=q["question_identity_key"],
                questionRevision=q["question_revision"], globalOrdinal=q["global_ordinal"],
                text=q["text"], modeSide=q["mode_side"], familyKey=q["family_key"],
                brandExposure=q["brand_exposure"],
            ) for q in row["frozen_payload"]["questions"]
        ],
        counts=QuestionPlanCounts(
            defensive=row["defensive_count"], offensive=row["offensive_count"],
            total=row["total_count"]),
        pricingRule=_pricing_rule_out(),
        expiresAt=row["expires_at"].isoformat(),
        copyRegistryVersion=COPY_REGISTRY_VERSION,
        idempotentReplay=False,
    )


def _preview_outcome(row) -> tuple:
    """从 preview 行**一处**派生 (can_confirm, next_action)。

    🔴 [#97 2026-09-05] 原来这两个值在**两个端点里各算一遍**,而且都算错了同一处:
    `lifecycle` 只有 open / expired / consumed 三值,三元链的 else 分支正是 **open**,
    给的却是 `wait`(「稍等,正在体检」)—— 同一份响应里 canConfirm 又是 true。
    「你可以确认」和「请等待」同时说,两档处置相反,她只能猜。

    🔴 修法是**抽成一处**而不是改两份:同一谓词写两处必有一处漂掉 ——
    这个 bug 本身就是两份各写一遍的产物。
    """
    lifecycle = row["lifecycle"]
    if lifecycle == "consumed":
        return False, _action("view_existing_command",
                              target={"kind": "diagnosis_command",
                                      "id": row["consumed_command_id"] or ""})
    if lifecycle == "expired":
        return False, _action("new_preview",
                              target={"kind": "question_plan",
                                      "id": str(row["question_plan_id"])})
    # open(待确认):能确认,出口就该是「去确认」,不是「等着」。
    return True, _action("confirm_run_preview",
                         target={"kind": "diagnosis_run", "id": str(row["preview_id"])})


@router.post("/run-previews", response_model=RunPreviewResponse, response_model_by_alias=True)
async def create_run_preview(
    body: RunPreviewRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> RunPreviewResponse:
    """签发正式诊断预览(§15.3)。**零资金副作用** —— 不冻结任何算力。"""
    tenant = _tenant(request)
    if not idempotency_key or not idempotency_key.strip():
        # 🔴 [#58 2026-09-04] 与「请求体不合法」用**不同的** reason_key 与 nextAction。
        #    改之前两者给客户端的是**一模一样**的信封(422 + validation_failed 那句),
        #    她照那句去检查自己填的内容,而这个标识是前端自动带的、根本不是她填的。
        #    reason_key 本身不进 payload,可程序化区分的是 nextAction.kind /
        #    actionRef(后者由 (kind,target) 确定性导出)—— 所以动作也必须换掉。
        raise _safe_error("VALIDATION_FAILED",
                          reason_key="idempotency_key_missing",
                          next_action=_action("refresh_and_retry",
                                              target={"kind": "page", "page": "form"}))

    conn = _db()
    try:
        with conn.cursor() as cur:
            try:
                plan = plan_store.get_plan_exact_revision(
                    cur, plan_id=body.question_plan_id,
                    plan_revision=body.question_plan_revision, tenant_owner_user_id=tenant)
            except plan_store.PlanNotFound:
                raise _safe_error("NOT_FOUND") from None

        if plan["superseded_by_revision"] is not None:
            latest = None
            with conn.cursor() as cur:
                latest = plan_store.latest_revision_number(
                    cur, plan_id=body.question_plan_id, tenant_owner_user_id=tenant)
            raise _safe_error(
                "QUESTION_PLAN_NOT_RUNNABLE",
                reason_key="question_plan_not_runnable",
                next_action=_action("review_question_plan",
                                    target={"kind": "question_plan", "id": body.question_plan_id}),
                details={"planId": body.question_plan_id,
                         "planRevision": body.question_plan_revision,
                         "latestRevisionAvailable": latest},
            )

        # [返修② 2] ``body.platform_keys`` 已由 DTO 规范化成**有序去重集合**,
        # 所以这里的 ``len`` 就是唯一平台数。**刻意不再套一层 ``set()``** ——
        # 再去重一遍会把「规范化被摘掉」这件事遮住,让变异存活(两把锁叠同一路径)。
        # [返修③ P0-2] 白名单:不在诊断执行面里的平台键一律 typed 拒绝。
        # 🔴 这一步必须在**计价之前**。原来没有白名单,传
        #    ``["totally-fake-platform"]`` 会返 200 并真按 1 个平台计价 2600 ——
        #    客户为一个不存在的检索面付了钱,而系统永远不会去跑它。
        # 🔴 清单来自 config/ai_engines,与真跑集、前端可选集**同一份**。
        _unknown = unknown_engines(body.platform_keys)
        if _unknown:
            logger.info("[defgeo] 未知平台键被拒:%s", list(_unknown))
            raise _safe_error("VALIDATION_FAILED", reason_key="platform_not_available")

        planned_cells = plan["total_count"] * len(body.platform_keys)
        if planned_cells < 1:
            raise _safe_error("VALIDATION_FAILED")

        # 定价:服务端签发,客户端不得自报(§3.5「客户端不能用 mode 替换 SKU 或自报价格」)。
        feature_code = DIAGNOSIS_FEATURE_CODE
        try:
            # [A-2] 版本由真实价目内容派生,读不出来就**不签 preview**。
            #   回落成常量正是 Codex P0-2 的形态:版本恒等 ⇒ confirm 的比对永不触发。
            catalog_version = _live_pricing_catalog_version(feature_code)
        except PricingCatalogUnreadable:
            raise _safe_error(
                "POLICY_UNAVAILABLE", reason_key="policy_unavailable",
                next_action=_action("contact_support",
                                    target={"kind": "page", "page": "support"})) from None
        # 🔴 价格**服务端签发**,客户端不得自报(§3.5)。
        #    第一版这里硬编码 0 —— 那让整条冻结链在判据里等于没跑
        #    (0 算力不产生 point_freezes 行,「freeze 增量为 0」既像对也像错,
        #     零判别力)。现在读现役 ``feature_pricing``:与真实扣费**同一张表**,
        #    预览价与冻结价同源 —— 否则就会出现本仓记过的「预览 470、冻结 390」。
        try:
            # 🔴 传**题数**不是格数:格数 = 题数 × 平台数,拿它计价就是把
            #    一次诊断按平台卖了 N 遍(Owner 2026-09-02 拍板修掉)。
            #    planned_cells 仍然保留 —— 它是**工作量**(要问多少次),
            #    只是不再是**计价口径**。
            base_points, extra_points = _price_for_plan(
                feature_code, int(plan["total_count"]))
        except Exception:
            raise _safe_error(
                "POLICY_UNAVAILABLE", reason_key="policy_unavailable",
                next_action=_action("contact_support",
                                    target={"kind": "page", "page": "support"})) from None
        exact_total = base_points + extra_points
        funding_policy, principal_kind, sponsor_ref = _classify_payer(request)
        approval_requirement = "not_required"

        try:
            validate_preview_funding(
                funding_policy=funding_policy, principal_kind=principal_kind,
                sponsor_policy_ref=sponsor_ref, approval_requirement=approval_requirement,
                base_points=base_points, extra_points=extra_points,
                exact_total_points=exact_total,
            )
        except ProjectionError:
            raise _safe_error("POLICY_UNAVAILABLE",
                              reason_key="policy_unavailable",
                              next_action=_action("contact_support",
                                                  target={"kind": "page", "page": "support"})) from None

        preview_id = plan_store.new_preview_id()
        frozen = {
            "questionPlanId": str(plan["plan_id"]),
            "questionPlanRevision": plan["plan_revision"],
            "campaignMode": plan["mode"],
            "platformKeys": sorted(body.platform_keys),
            "plannedCells": planned_cells,
        }
        req_hash = canonical_hash(
            brand_id=plan["brand_id"], profile_revision_id=body.profile_revision_id,
            mode=plan["mode"], question_set_version=plan["question_set_version"],
            questions=tuple(
                PlannedQuestion(
                    question_identity_key=q["question_identity_key"],
                    question_revision=q["question_revision"],
                    global_ordinal=q["global_ordinal"], text=q["text"],
                    mode_side=q["mode_side"], family_key=q["family_key"],
                    brand_exposure=q["brand_exposure"], origin="customer",
                    classifier_version=None,
                ) for q in plan["frozen_payload"]["questions"]
            ),
        )
        # [返修② 3] 幂等身份 = 题单内容 **+ 平台集**。
        # 两个 hash 刻意分开存:``canonical_hash`` 仍是题单本体身份(响应
        # ``canonicalHash`` 逐字节不变,旧行为不回归),``canonical_request_hash``
        # 才是幂等唯一约束吃的那一个。合成一个会让题单预览与 run 预览对同一份
        # 题单算出两个身份。
        idem_hash = run_request_hash(
            plan_content_hash=req_hash, platform_keys=body.platform_keys)
        expires = datetime.now(timezone.utc) + timedelta(minutes=_PREVIEW_TTL_MINUTES)

        with conn.cursor() as cur:
            row, replayed = plan_store.insert_run_preview(
                    cur, preview_id=preview_id, tenant_owner_user_id=tenant,
                    brand_id=plan["brand_id"], created_by_user_id=tenant,
                    question_plan_id=str(plan["plan_id"]),
                    question_plan_revision=plan["plan_revision"],
                    question_plan_hash=plan["canonical_hash"],
                    profile_revision_id=body.profile_revision_id,
                    campaign_mode=plan["mode"], frozen_payload=frozen,
                    canonical_hash=req_hash, feature_code=feature_code,
                    pricing_catalog_version=catalog_version, base_points=base_points,
                    extra_points=extra_points, exact_total_points=exact_total,
                    funding_policy=funding_policy, principal_kind=principal_kind,
                    sponsor_policy_ref=sponsor_ref,
                    approval_requirement=approval_requirement,
                    planned_cells=planned_cells, idempotency_key=idempotency_key.strip(),
                canonical_request_hash=idem_hash, expires_at=expires,
            )
        conn.commit()
    except plan_store.PlanNotFound:
        conn.rollback()
        raise _safe_error("IDEMPOTENCY_CONFLICT") from None
    finally:
        conn.close()

    lifecycle = row["lifecycle"]
    can_confirm, next_action = _preview_outcome(row)

    return RunPreviewResponse(
        previewId=str(row["preview_id"]),
        canonicalHash=row["canonical_hash"],
        lifecycle=lifecycle,
        lifecycleUserLabel=assert_public_copy_clean(
            user_label("preview_lifecycle", lifecycle), field="lifecycleUserLabel"),
        fundingPolicy=row["funding_policy"],
        fundingPolicyUserLabel=assert_public_copy_clean(
            user_label("funding_policy", row["funding_policy"]), field="fundingPolicyUserLabel"),
        campaignMode=row["campaign_mode"],
        modeUserLabel=assert_public_copy_clean(
            user_label("mode", row["campaign_mode"]), field="modeUserLabel"),
        questionPlanId=str(row["question_plan_id"]),
        questionPlanRevision=row["question_plan_revision"],
        plannedCells=row["planned_cells"],
        basePoints=row["base_points"],
        extraPoints=row["extra_points"],
        exactTotalPoints=row["exact_total_points"],
        # U-4:一句话说清这次花多少,不让她从三个数字里自己算。
        costUserLabel=assert_public_copy_clean(
            f"本次体检消耗你的算力 {row['exact_total_points']}", field="costUserLabel"),
        expiresAt=row["expires_at"].isoformat(),
        canConfirm=can_confirm,
        nextAction=NextActionOut(**{
            "kind": next_action["kind"], "label": next_action["label"],
            "actionRef": next_action["actionRef"], "target": next_action["target"],
        }),
        copyRegistryVersion=COPY_REGISTRY_VERSION,
        runStatusProjectionVersion=PROJECTION_VERSION,
        idempotentReplay=replayed,
    )


@router.get("/run-previews/{preview_id}", response_model=RunPreviewResponse,
            response_model_by_alias=True)
async def get_run_preview(preview_id: str, request: Request) -> RunPreviewResponse:
    """exact GET(§15.3):不重算、不读 latest、不延长 expiry。"""
    tenant = _tenant(request)
    conn = _db()
    try:
        with conn.cursor() as cur:
            row = plan_store.get_run_preview(
                cur, preview_id=preview_id, tenant_owner_user_id=tenant)
    except plan_store.PlanNotFound:
        raise _safe_error("NOT_FOUND") from None
    finally:
        conn.close()

    lifecycle = row["lifecycle"]
    can_confirm, next_action = _preview_outcome(row)
    return RunPreviewResponse(
        previewId=str(row["preview_id"]), canonicalHash=row["canonical_hash"],
        lifecycle=lifecycle,
        lifecycleUserLabel=assert_public_copy_clean(
            user_label("preview_lifecycle", lifecycle), field="lifecycleUserLabel"),
        fundingPolicy=row["funding_policy"],
        fundingPolicyUserLabel=assert_public_copy_clean(
            user_label("funding_policy", row["funding_policy"]), field="fundingPolicyUserLabel"),
        campaignMode=row["campaign_mode"],
        modeUserLabel=assert_public_copy_clean(
            user_label("mode", row["campaign_mode"]), field="modeUserLabel"),
        questionPlanId=str(row["question_plan_id"]),
        questionPlanRevision=row["question_plan_revision"],
        plannedCells=row["planned_cells"], basePoints=row["base_points"],
        extraPoints=row["extra_points"], exactTotalPoints=row["exact_total_points"],
        costUserLabel=assert_public_copy_clean(
            f"本次体检消耗你的算力 {row['exact_total_points']}", field="costUserLabel"),
        expiresAt=row["expires_at"].isoformat(),
        canConfirm=can_confirm,
        nextAction=NextActionOut(**{
            "kind": next_action["kind"], "label": next_action["label"],
            "actionRef": next_action["actionRef"], "target": next_action["target"],
        }),
        copyRegistryVersion=COPY_REGISTRY_VERSION,
        runStatusProjectionVersion=PROJECTION_VERSION,
        idempotentReplay=False,
    )


# ══════════════════════════════════════════════════════════════════════════
# §15.3 confirm —— 接现役 admission,一个事务里把四件事做完
# ══════════════════════════════════════════════════════════════════════════
#: 目录版本串的方案前缀。换算法必须换前缀 —— 否则新旧算法算出的短 hash
#: 有概率相等,而「版本相等」正是**放行冻结**的依据。
_PRICING_CATALOG_SCHEME = "defgeo-pricing-v2"


class PricingCatalogUnreadable(RuntimeError):
    """价目读不出来 ⇒ **不签 preview / 不放行 confirm**,而不是拿一个常量凑合。"""


def _live_pricing_catalog_version(feature_code: str = "geo_diagnosis") -> str:
    """服务端当前定价目录版本 —— 由**真实价目内容**派生。

    [A-2 · Codex P0-2 · 2026-08-25] 修的是什么
    ------------------------------------------
    这个函数原来 ``return "defgeo-pricing-shadow-v1"`` —— 一个**常量**。
    于是 confirm 里那句
    ``preview["pricing_catalog_version"] != _live_pricing_catalog_version()``
    **永远为假**:改价这件事在「确认」这一步是看不见的。
    紧接着 ``freeze_points`` 又去读**现价**
    (``middleware/billing.py:1364`` ``total_cost = pricing["cost_points"] + extra_cost``),
    而 confirm 只把 preview 里的 **extra** 带过去 —— base 用的是现价。
    可达结果:她确认 7,800、实际冻 8,200,而响应仍然显示 7,800。

    版本必须由价目**内容**决定:改价 ⇒ 必然改版本 ⇒ confirm 必然 409
    ``SNAPSHOT_CHANGED`` 并引导重新预览。

    为什么 hash 的是这几列
    ----------------------
    preview 的价格 = ``feature_pricing[feature_code].cost_points`` + 按题加价
    (见 ``_price_for_plan``;🔴 2026-09-02 之前这里是「× 格数」,Owner 拍板
    改成按题不按平台),而冻结走的是**同一张表同一行**
    (``freeze_points`` → ``get_feature_pricing``)。``requires_paid_points``
    也进 hash:它决定冻结从哪几个池扣,改它同样改变了「她确认的那件事」。

    读不出来 → 抛 ``PricingCatalogUnreadable``,由两个调用点翻成
    ``POLICY_UNAVAILABLE``。**绝不回落到常量** —— 回落等于把这个 P0 原样放回去。

    抽成函数仍然是为了可判性:判据 monkeypatch 它来造「改价」那一幕。
    而「真的改一次价会不会改版本」由打**真库**的判据单独证(monkeypatch 证不了它)。
    """
    import hashlib

    from db.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT feature_code, cost_points, requires_paid_points "
                "FROM feature_pricing WHERE feature_code=%s",
                (str(feature_code),),
            )
            row = cur.fetchone()
    except Exception as exc:                       # noqa: BLE001
        raise PricingCatalogUnreadable(
            "价目读取失败 feature=%s: %s" % (feature_code, exc)) from None
    finally:
        conn.close()

    return _pricing_catalog_version_from_row(row, feature_code)


def _pricing_catalog_version_from_row(row, feature_code: str = "geo_diagnosis") -> str:
    """[E2-2] 由**一行已经读出来的价目**派生版本串。**唯一实现,两个调用方共用。**

    抽出来不是为了好看:confirm 现在要用「自己事务里锁住的那一行」算版本,
    而 preview 仍走 `_live_pricing_catalog_version` 自己读一次。
    同一个 canonical 写两处,迟早有一处漏改一列 —— 而漏改的那一处不会让
    任何判据变红(本仓「同一谓词写两处必有一处没人验」)。
    """
    import hashlib

    if row is None:
        raise PricingCatalogUnreadable("价目里没有 %s —— 不猜价" % (feature_code,))
    # 🔴 [P0 2026-09-02] 规则版本也进 hash。
    #    上一版只吃价目**行内容** —— 于是「改规则」在版本上是隐形的:
    #    本次把按格改成按题,cost_points 一个字没动 ⇒ 版本不变 ⇒ confirm 里那句
    #    「版本变了就 409」不触发 ⇒ 上线前建的在途 preview 仍按**旧规则**冻结
    #    (它的 extra 是落库时算好的那个数)。把规则版本混进来,
    #    改规则与改价一样必然改版本、必然让在途 preview 重新预览。
    from services.diagnosis_question_pricing import PRICING_RULE_VERSION

    canonical = "|".join([
        str(row["feature_code"]),
        str(int(row["cost_points"])),
        "1" if row.get("requires_paid_points") else "0",
        str(PRICING_RULE_VERSION),
    ])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return _PRICING_CATALOG_SCHEME + ":" + digest


def _lock_pricing_row_for_confirm(cur, feature_code: str):
    """[E2-2 · Codex 二审 P1-F2] 在 **confirm 自己的事务** 里把价目那一行锁住并读出来。

    修的是什么
    ----------
    Codex 真 HTTP × PG16 复现:preview 时 `requires_paid_points=false`,
    在**版本读取之后、freeze 之前**切成 true ⇒ confirm 200,总额同样 650,
    但物理拆分从 paid=0/bonus=650 变成 bonus=0/paid=650 —— **换池不换钱**。
    两把既有保险丝都拦不住它:
      · 版本闸读的是**另一条连接**(`_live_pricing_catalog_version` 自己
        `get_connection()` 读完就关),读完之后到 freeze 之间是敞开的窗口;
      · 金额闸只比**总额**,而这一手总额压根没变。

    为什么是 FOR SHARE
    ------------------
    `freeze_points` 内部是 `get_feature_pricing(feature_code, cursor=_cursor)` ——
    走的正是 confirm 传进去的这个 cursor。所以只要在同一事务里先把这一行
    `FOR SHARE` 锁住,并发的 `UPDATE feature_pricing` 就必须等到 confirm 结束,
    freeze 那次读必然读到与算版本时**同一行**。
    用 FOR SHARE 而不是 FOR UPDATE:confirm 只是**读者**,多个 confirm 之间
    不该互相排队(FOR UPDATE 会把并发下单串行化)。它挡的是写者,这正是要挡的那个。

    🔴 没有改 `middleware/billing.py` 一个字节 —— 它是保护文件,
       而这个修法不需要动它的签名(工单:签名如需变更要停下走 Owner 白名单)。
    """
    # 🔴 [工单 V4-A · Codex fix-of-fix P2-1] `AND is_active` 不是装饰。
    #    没有它:preview 之后有人把这个商品**停用**,confirm 这一步照样把 inactive 行
    #    锁住并算出版本(版本 hash 里也没有 is_active,所以两边完全对得上),
    #    然后一路走到 active-only 的钱包价目查询处抛 ValueError,
    #    被 :1540 的兜底翻成裸 500「稍后重试」—— 而她再试一百次也不会成功。
    #    资金侧是安全的(Codex 实证 diagnosis_runs / point_freezes / outbox 皆 0),
    #    坏的是**错误形态**:现在读不到 ⇒ PricingCatalogUnreadable ⇒ 调用点翻成
    #    typed POLICY_UNAVAILABLE + 重新预览动作。
    #
    #    为什么选「锁定查询加谓词」而不是「版本 hash 纳入 is_active」:
    #    后者会让**所有在途 preview** 的版本串改变 ⇒ 上线瞬间全部 409 重来。
    #    前者只影响"商品真的被停用了"那一小撮,爆炸半径小一个数量级。
    cur.execute(
        "SELECT feature_code, cost_points, requires_paid_points "
        "FROM feature_pricing WHERE feature_code=%s AND is_active FOR SHARE",
        (str(feature_code),),
    )
    row = cur.fetchone()
    if row is None:
        raise PricingCatalogUnreadable(
            "价目里没有**在售的** %s —— 不猜价" % (feature_code,))
    return row


def _base_points_for(feature_code: str) -> int:
    """一次诊断的**基价** —— 现役 ``feature_pricing`` 那一行的 ``cost_points``。

    🔴 [2026-09-02] 为什么单独抽出来:``_pricing_rule_out`` 也要这个数。
       第一版它是调 ``_price_for_plan(feature_code, 0)`` 借来的 —— 值对,
       但那样就**多了一个 `_price_for_plan` 调用点**,而第三趟有一把结构锁
       钉着「每个调用点都必须传题数变量」(防「格数/平台数进计价」)。
       于是我的合法调用被那把锁判红:两边都对,是我给锁的分母加了一项。
       抽出共享取数口之后,``_price_for_plan`` 的调用点数**不变**,
       锁的分母不动,判据一个字都不用改。
    """
    from db.wallet_db import get_feature_pricing

    return int(get_feature_pricing(feature_code)["cost_points"])


def _price_for_plan(feature_code: str, question_count: int) -> tuple[int, int]:
    """服务端计价:返回 (base, extra)。

    单价取现役 ``feature_pricing`` —— 与真实扣费**同一张表**,保证预览价与冻结价
    同源(否则会出现本仓记过的「预览 470、冻结 390」:预览里算了、按钮上显示了,
    却没被冻住)。查不到功能编码就抛,由调用方转 ``POLICY_UNAVAILABLE`` —— 不猜价。

    🔴 [P0 · 2026-09-02 · Owner 拍板] 这里**按题不按格**。
       上一版是 ``base = 单价; extra = 单价 × (格数 - 1)``,而
       ``格数 = 题数 × 平台数`` —— Owner 手机实测 hybrid 5 题 × 4 平台 = 20 格
       ⇒ **13,000 算力**,是 legacy 一次诊断的 20 倍。admin 显示「平台承担」
       看不出来,真服务商会被真扣。
       现在:base = 一次诊断的基价;extra = **legacy 同一条**按题加价规则。
       **平台数彻底不进计价** —— 同一份题问 4 个平台仍是一次诊断。

    🔴 ``ai_optimized=False``:防御线**没有** legacy 的「AI 优化」开关
       (``ai_optimize`` 在 ``api/defensive_geo_api.py`` 与
       ``services/defensive_geo/**`` 全仓零命中,``RunPreviewRequest`` 也没这个
       字段),所以按「未开」处理。将来真加了这个开关,改的是**这一行的实参**,
       规则本体不动 —— 那正是把规则抽出去的收益。
    """
    from services.diagnosis_question_pricing import extra_points_for_questions

    unit = _base_points_for(feature_code)
    extra = extra_points_for_questions(question_count, ai_optimized=False)
    return unit, extra


def _approval_granted(preview: dict) -> bool:
    """组织审批是否已批。

    🔴 现阶段恒 False:本包**没有**接组织审批面(那是现役
    ``services/organization_billing`` 的域,属下一窗)。恒 False 的含义是
    「``approval_requirement=required`` 的 preview 一律 403 APPROVAL_REQUIRED,
    零 command 零 freeze」—— 这是**安全默认**:宁可拦住,不可放行一笔没人批过的钱。
    抽成函数同样是为了可判性:判据 monkeypatch 它来验「批准后可 confirm」那一支。
    """
    return False


def _mint_run_token() -> str:
    from services.diagnosis_runs import mint_run_token

    return mint_run_token()


def _admit_run(owner_user_id, client_request_id, brand_id, session_id, run_token,
               billing_mode, *, _cursor):
    from services.diagnosis_runs import admit_run

    return admit_run(owner_user_id, client_request_id, brand_id, session_id,
                     run_token, billing_mode, _cursor=_cursor)


async def _freeze_exact(cur, *, preview: dict, spec, tenant: int, run_token: str) -> dict:
    """在**调用方事务**里冻 exact points,返回 fundingHandle。

    🔴 personal_wallet 走真实 ``freeze_points(_cursor=cur)``,与
       ``services/geo_douyin/contract_freeze.freeze_one_item`` 同形
       (本仓既有、有三个生产调用方的模式)。admin 免单旁路返回 ``freeze_id=None``,
       本函数**拒绝**把它当成功 —— 无句柄的「成功」正是本仓 P0-6 点名的零记账形态。

    平台账两格(admin/sponsor)**不动客户钱包**:``fundingState=exempt_recorded``;
    钱由平台直营服务账号的钱包真冻(见下面那一支)。

    返回 ``(handle, freeze_row_id, split_snapshot, payer_user_id)`` —— 后三项都是
    **冻结当时才存在**的事实,必须原样落进 run 行:

      · ``freeze_row_id``  句柄。少了它 run 进不了 running(``chk_freeze_handle``)。
      · ``split_snapshot`` 三池拆分 + 权威 order(A-4 / Codex P1-4)。
        少了它,部分履约结算读不到拆分 → ``reserved_split_order_unknown``
        → ``settlement_manual``,钱长挂。order 是**冻结当时钱包扣费偏好**的产物,
        事后从冻结行推不出来 —— 这一刻不存,就永远没有了。
      · ``payer_user_id`` 冻结行到底长在谁名下(A-1 / Codex P0-1 第二层)。
        平台腿的冻结在**平台账号**的钱包里,而结算侧此前一律拿 ``owner_user_id``
        去 ``commit_freeze`` —— user 对不上就永远定位不到那一行。
    """
    from services.defensive_geo.funding_projection import validate_confirm_funding
    from services.diagnosis_runs import freeze_task_ref as _run_freeze_task_ref

    policy = preview["funding_policy"]
    #: 真实冻结行 id(仅 personal_wallet 且非 0 价时存在)。
    #: 它要回填进 ``diagnosis_runs.freeze_id`` 才能满足 ``chk_freeze_handle``
    #: 并把 run 推进 running —— 见 ``start_run_in_caller_txn``。
    #: 🔴 刻意**不**从 ``handle["ref"]`` 反解:那是对外 DTO 的 opaque 串,
    #:    拿展示字段反推内部 id 是把两个语义焊死,改一个就悄悄打断另一个。
    freeze_row_id = None
    #: [A-4] billing 返回的 ``physical_split_snapshot``(含权威 order),原样上抛。
    split_snapshot = None
    #: [A-1] 这笔冻结长在谁名下。个人腿 = 租户自己;平台腿在下面被改写成平台 UID。
    payer_user_id = int(tenant)
    #: 🔴 [A-1] task_ref 必须与 ``diagnosis_runs.freeze_task_ref`` **同一个串**。
    #:
    #: 原来这里写的是 ``"defgeo_" + run_token``,而 admit_run 给 run 行落的是
    #: ``"diag_" + run_token``(``services/diagnosis_runs.freeze_task_ref``)——
    #: 两者**不相等**。billing 的 commit/release 给了 freeze_id+user_id 时不约束
    #: task_ref,所以这个错在"能不能扣掉"上看不出来;但凡是按三元组
    #: (id + user + **task_ref**)定位冻结行的地方就全查空:
    #:   · ``_frozen_total_for_run`` → 降级交付恒 ``frozen_amount_unreadable`` 转人工;
    #:   · ``_reserved_split_for_run`` → 恒 ``reserved_split_freeze_missing`` 转人工;
    #:   · ``_verify_ledger_refund`` → 人工确认退款恒 fail-closed 拒。
    #: 冻结的 task_ref 只该有一个产出源,就是那个函数。
    _task_ref = _run_freeze_task_ref(run_token)

    if policy == "personal_wallet":
        from middleware.billing import freeze_points

        result = await freeze_points(
            int(tenant), str(preview["feature_code"]),
            task_ref=_task_ref,
            brand_id=int(preview["brand_id"]),
            extra_cost=int(preview["extra_points"]),
            reason="防御型 GEO 正式诊断",
            _cursor=cur,
        )
        result = dict(result or {})
        # 🔴 两种「没有 freeze_id」必须分开 —— 第一版把它们混成一条,判据当场打红:
        #   (a) ``admin_exempt=True``:payer 是 admin 时 billing 直接返回零句柄。
        #       那是本仓 P0-6 点名的**零记账形态**,必须拒 ——
        #       不接受无句柄的「成功」。
        #   (b) ``exact_total_points == 0``:该功能本来就不要钱,
        #       **没有冻结行是正确的**,不是缺陷。拒它等于把免费功能拦死。
        if result.get("admin_exempt"):
            raise RuntimeError(
                "payer 是管理员,冻结走了 admin 免单旁路(freeze_id=None)——"
                "那是零记账形态,不接受无句柄的「成功」"
            )
        _exact = int(preview["exact_total_points"])
        if _exact > 0 and not result.get("freeze_id"):
            raise RuntimeError(
                f"应冻 {_exact} 算力却没拿到 freeze_id —— 不接受无句柄的「成功」"
            )
        freeze_row_id = result.get("freeze_id")
        split_snapshot = result.get("physical_split_snapshot")     # [A-4]
        handle = {
            "kind": spec.handle_kind,
            # 0 价时如实标注「本次无需冻结」,而不是编一个假 ref。
            "ref": str(result["freeze_id"]) if result.get("freeze_id") else "no_freeze:zero_cost",
            "approval_ref": None,
        }
    elif policy == "organization_budget":
        raise RuntimeError("organization_budget 腿尚未接通,不得签发该 policy 的 preview")
    else:
        # ── [门二 G6] 平台成本两格:真记账,不是返一个字符串 handle ──────
        #
        # 上一版这里是 ``ref = "platform_cost:" + run_token`` —— 一个**编出来的**
        # 字符串,库里没有任何一行对应它。FIN-06 逐字要求「admin exempt 用户钱包
        # 不扣,**但平台账真实记账**」,编字符串正好是那条要求的反面。
        #
        # 本仓「平台账」的现役形态不是一张独立 ledger 表,而是**平台直营服务账号
        # 的钱包**(scheduler 的 billing_mode=='platform' 走的就是它)。
        # 所以这里换号后走同一条 freeze_points,平台钱包真的动。
        if policy == "sponsor_platform_ledger" and not (
            preview.get("sponsor_policy_ref") and str(preview["sponsor_policy_ref"]).strip()
        ):
            # 缺已签 policy 时它与 admin 格无法区分 —— 等于平台白掏钱且无从追责。
            raise RuntimeError("sponsor_platform_ledger 必须带已签 sponsorPolicyRef")

        platform_uid = _platform_direct_service_user_id()
        if platform_uid is None:
            # 🔴 门栈里没有可用的平台账 -> **typed 拒绝**,不是 500。
            #    零 command / 零 freeze:本函数抛出后 confirm 会整笔回滚。
            raise _safe_error("PLATFORM_DIRECT_UNSUPPORTED")

        from middleware.billing import freeze_points

        # [A-1] 从这一刻起,「这笔钱是平台的」是**run 行上的一个事实**,
        #   不再靠 billing_mode='exempt' 把整条结算短路掉。
        payer_user_id = int(platform_uid)
        result = dict(await freeze_points(
            platform_uid, str(preview["feature_code"]),
            task_ref=_task_ref,
            brand_id=int(preview["brand_id"]),
            extra_cost=int(preview["extra_points"]),
            reason="防御型 GEO 正式诊断(平台承担)",
            _cursor=cur,
        ) or {})

        # 🔴 零记账守卫**原样保留**(返修单点名不许削它)——
        #    只是这一格的失败形态换成了 typed 拒绝而不是裸 500。
        #    它拦的是同一件事:平台直营账号被配成了 admin,于是 billing 走免单
        #    旁路、**一张表都不写**,而对外却显示"平台已承担"。那是账面凭空消失。
        if result.get("admin_exempt"):
            logger.error(
                "[defgeo] 平台直营服务账号 %s 命中 admin 免单旁路 —— 平台成本账会是空的",
                platform_uid,
            )
            raise _safe_error("PLATFORM_DIRECT_UNSUPPORTED")
        _exact_platform = int(preview["exact_total_points"])
        if _exact_platform > 0 and not result.get("freeze_id"):
            raise RuntimeError(
                f"平台成本账应冻 {_exact_platform} 算力却没拿到 freeze_id —— "
                "「真实记账」没有发生(FIN-06),不接受无句柄的「成功」"
            )
        freeze_row_id = result.get("freeze_id")
        split_snapshot = result.get("physical_split_snapshot")     # [A-4]
        handle = {
            "kind": spec.handle_kind,
            # ref 指向**真实存在的那一行**,不再是编出来的串。
            "ref": (f"platform_freeze:{result['freeze_id']}" if result.get("freeze_id")
                    else "no_freeze:zero_cost"),
            "approval_ref": None,
        }

    # ═══ [A-2 保险丝②] 物理冻结额必须**严格等于**她确认的那个数 ═══════════
    #
    # 版本比对(保险丝①)是"事前"的:它拦的是「preview 签发之后价目被改过」。
    # 但那一条挡不住所有形态 —— 比如改价恰好发生在版本比对与 freeze_points
    # 之间的那一瞬,或者将来有人给冻结链加了别的加价项。
    # 所以这里再钉一次"事后"的:**冻了多少**与 preview 的 ``exact_total_points``
    # 逐点相等,不等就抛,confirm 的外层 except 会把整笔回滚
    # (零 command、零 freeze、零 outbox),并 typed 引导重新预览。
    #
    # 🔴 用的是 ``freeze_points`` 返回体里的 ``amount``(billing.py:1352 那一格
    #    就是 ``total_cost``),**不动 middleware/billing.py 一个字符**。
    # 🔴 平台腿同样受这条约束 —— 平台多冻也是账错,只是错在我们自己身上。
    # 🔴 0 价:``amount`` 为 0、``exact`` 也为 0,相等,天然放行(免费功能不被拦死)。
    _exact_confirmed = int(preview["exact_total_points"])
    _frozen_amount = int(result.get("amount") or 0)
    if _frozen_amount != _exact_confirmed:
        logger.error(
            "[defgeo] 冻结额与确认额不符 policy=%s 确认=%s 实冻=%s —— 整笔回滚",
            policy, _exact_confirmed, _frozen_amount,
        )
        raise _safe_error(
            "SNAPSHOT_CHANGED", reason_key="snapshot_changed",
            next_action=_action("new_preview",
                                target={"kind": "question_plan",
                                        "id": str(preview["question_plan_id"])}))

    validate_confirm_funding(
        funding_policy=policy,
        principal_kind=preview["principal_kind"],
        billing_mode_projection=spec.billing_mode_projection,
        funding_state=spec.confirm_funding_state,
        handle_kind=handle["kind"],
        handle_approval_ref=handle["approval_ref"],
        sponsor_policy_ref=preview["sponsor_policy_ref"],
    )
    if int(preview["exact_total_points"]) != int(preview["base_points"]) + int(preview["extra_points"]):
        raise RuntimeError("冻结前算术不守恒 —— 拒绝冻结")
    return handle, freeze_row_id, split_snapshot, payer_user_id


def _enqueue_confirm_outbox(cur, *, preview: dict, tenant: int, run_token: str) -> None:
    """与 ②③④ 同事务落 outbox。

    复用现役 ``services/notification_outbox.enqueue_notification_event(cursor, …)``
    —— 它本来就吃调用方游标(函数自己的 docstring:「Use the caller's cursor so
    business terminal and outbox are atomic」),不另造第二套。

    🔴 事件类型踩过一次坑,写下来免得下一个人再踩:
       第一版用的是 ``DIAGNOSIS_COMPLETED``。它渲染出来的标题是
       **「品牌体检已完成」**—— 在用户刚按下"确认"、体检一秒钟都还没跑的时候。
       仓里已经有一模一样的前车之鉴(``notification_events.py`` L58-62,R6
       2026-08-17:复用 ``MONITORING_COMPLETED`` 通告"有人替你开通了",标题
       语义完全相反)。所以这里另立 ``DIAGNOSIS_CONFIRMED`` 非终态事件。

       这个错之前一直没被判据抓到,是因为整条 confirm 成功路径当时是死的
       (``get_user`` 打废事务,见第四班 ①)——**判据全绿只是因为它们够不到这一行。**
    """
    from datetime import datetime, timezone

    from services.notification_events import NotificationEventType, RecipientKind
    from services.notification_outbox import enqueue_notification_event

    enqueue_notification_event(
        cur,
        event_type=NotificationEventType.DIAGNOSIS_CONFIRMED,
        business_id=str(run_token),
        terminal_state="confirmed",
        recipient_user_id=int(tenant),
        recipient_kind=RecipientKind.USER,
        facts={
            "business_no": "诊断-" + str(run_token),
            "status": "已开始，正在体检",
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "summary": "请在品牌体检历史中查看进度。",
        },
    )


class RunConfirmRequest(_Strict):
    expected_hash: str = Field(alias="expectedHash", min_length=64, max_length=64)


class FundingHandleOut(_Strict):
    kind: str
    ref: str
    approval_ref: Optional[str] = Field(default=None, alias="approvalRef")


class RunConfirmResponse(_Strict):
    diagnosis_command_id: str = Field(alias="diagnosisCommandId")
    run_id: str = Field(alias="runId")
    #: [门三 G8] legacy 进度端点/WS 真正校验的那把键。
    #: `runId` 是 run_token,而 `auth.session_access.authorize_session` 查的是
    #: `diagnosis_runs.session_id` —— 两者**不相等**(session_id = "defgeo_" + run_token)。
    #: 不把这把键交出去,前端只能拿 runId 去试,于是创建者查自己的进度被 403。
    progress_session_id: str = Field(alias="progressSessionId")
    status_url: str = Field(alias="statusUrl")
    idempotent_replay: bool = Field(alias="idempotentReplay")
    funding_policy: str = Field(alias="fundingPolicy")
    principal_kind: str = Field(alias="principalKind")
    billing_mode_projection: str = Field(alias="billingModeProjection")
    funding_state: str = Field(alias="fundingState")
    funding_handle: FundingHandleOut = Field(alias="fundingHandle")
    sponsor_policy_ref: Optional[str] = Field(default=None, alias="sponsorPolicyRef")
    exact_total_points: int = Field(alias="exactTotalPoints")
    run_state_user_label: str = Field(alias="runStateUserLabel")
    funding_state_user_label: str = Field(alias="fundingStateUserLabel")
    cost_user_label: str = Field(alias="costUserLabel")
    next_action: NextActionOut = Field(alias="nextAction")
    copy_registry_version: str = Field(alias="copyRegistryVersion")
    run_status_projection_version: str = Field(alias="runStatusProjectionVersion")


def _confirm_response(preview: dict, *, replayed: bool, run_token=None, handle=None):
    from services.defensive_geo.funding_projection import cell as fp_cell

    spec = fp_cell(preview["funding_policy"])
    command_id = run_token or str(preview["consumed_command_id"])
    if handle is None:
        # 重放路径:handle 归原 command 所有,这里只回形状与 ref 锚,**不重新冻结**。
        handle = {"kind": spec.handle_kind, "ref": "replay:" + command_id, "approval_ref": None}

    # [门三 G8] 归属键统一从这里导出,不在两处各拼一遍字符串。
    from services.defensive_geo.run_executor import progress_session_id

    _progress_sid = progress_session_id(command_id)

    return RunConfirmResponse(
        diagnosisCommandId=command_id,
        runId=command_id,
        progressSessionId=_progress_sid,
        # statusUrl 指向 **legacy 进度端点**(WS 同键):它才是"这次体检跑到哪了"的
        # 真相源。原来指向 preview 的 GET —— 那只回冻结快照,永远不动,
        # 前端拿它轮询会看到一个永远"待确认"的页面。
        statusUrl=f"/api/diagnosis/session/{_progress_sid}/status",
        idempotentReplay=replayed,
        fundingPolicy=preview["funding_policy"],
        principalKind=preview["principal_kind"],
        billingModeProjection=spec.billing_mode_projection,
        fundingState=spec.confirm_funding_state,
        fundingHandle=FundingHandleOut(kind=handle["kind"], ref=handle["ref"],
                                       approvalRef=handle["approval_ref"]),
        sponsorPolicyRef=preview["sponsor_policy_ref"],
        exactTotalPoints=int(preview["exact_total_points"]),
        runStateUserLabel=assert_public_copy_clean(
            user_label("run_state", "queued"), field="runStateUserLabel"),
        fundingStateUserLabel=assert_public_copy_clean(
            user_label("funding_state", spec.confirm_funding_state),
            field="fundingStateUserLabel"),
        costUserLabel=assert_public_copy_clean(
            "本次体检消耗你的算力 " + str(int(preview["exact_total_points"])),
            field="costUserLabel"),
        nextAction=NextActionOut(**{
            "kind": "wait",
            "label": assert_public_copy_clean(user_label("action", "wait"), field="action.wait"),
            "actionRef": "defgeo:wait:" + command_id[:12],
            "target": {"kind": "diagnosis_run", "id": command_id},
        }),
        copyRegistryVersion=COPY_REGISTRY_VERSION,
        runStatusProjectionVersion=PROJECTION_VERSION,
    )


@router.post("/run-previews/{preview_id}/confirm", response_model=RunConfirmResponse,
             response_model_by_alias=True)
async def confirm_run_preview(
    preview_id: str,
    body: RunConfirmRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> RunConfirmResponse:
    """消费 preview → 创建 formal command + exact freeze + outbox(§3.5)。

    **一个事务**,四件事:
      ① ``SELECT … FOR UPDATE`` 锁住 preview 并重验 expiry / hash / payer / policy;
      ② CAS 把 preview 从 open 推到 consumed(**恰一次**的物理来源);
      ③ ``diagnosis_runs.admit_run(_cursor=cur)`` 建 formal command;
      ④ ``freeze_points(_cursor=cur)`` 冻 exact points + ``enqueue_notification_event(cur,…)`` 落 outbox。

    🔴 之所以做得成「同一事务」:billing 与 outbox 本来就支持借用游标,
       而 ``admit_run`` 由本包**追加**了同形的可选 ``_cursor``(默认路径字节不变)。
       任何一步抛异常 → 整体 rollback → **零 command、零 freeze、零 outbox**。
       409 全家的判据就是打在这个不变式上。

    🔴 ``billing_mode`` 只投影成现役 ``paid|exempt``,不扩宽(§3.4 红线)。
       四格矩阵由 ``funding_projection`` 单点裁决,本函数只按格取值。
    """
    from db.connection import get_connection
    from services.defensive_geo.funding_projection import cell as fp_cell

    tenant = _tenant(request)
    if not idempotency_key or not idempotency_key.strip():
        # 🔴 [#58 2026-09-04] 与「请求体不合法」用**不同的** reason_key 与 nextAction。
        #    改之前两者给客户端的是**一模一样**的信封(422 + validation_failed 那句),
        #    她照那句去检查自己填的内容,而这个标识是前端自动带的、根本不是她填的。
        #    reason_key 本身不进 payload,可程序化区分的是 nextAction.kind /
        #    actionRef(后者由 (kind,target) 确定性导出)—— 所以动作也必须换掉。
        raise _safe_error("VALIDATION_FAILED",
                          reason_key="idempotency_key_missing",
                          next_action=_action("refresh_and_retry",
                                              target={"kind": "page", "page": "form"}))

    conn = get_connection()
    run_token = None
    handle = None
    preview = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM defgeo_diagnosis_run_previews "
                "WHERE preview_id=%s AND tenant_owner_user_id=%s FOR UPDATE",
                # [返修③ P1-1] confirm 有**自己的**内联 SQL(要 FOR UPDATE),
                # 所以 plan_store 那道守卫覆盖不到它 —— 判据当场抓到:
                # /run-previews/not-a-uuid/confirm 仍然 500。
                # 这就是「同一谓词写两处 ⇒ 必有一处没人验」的活样本;
                # 这里不再写第三份校验,直接调同一个守卫。
                (plan_store.uuid_or_absent(preview_id, "preview_id"), tenant),
            )
            row = cur.fetchone()
            if row is None:
                raise _safe_error("NOT_FOUND")
            preview = dict(row)

            # consumed:同 root 重放**原** command,绝不建第二个(§19 变异 96)
            if preview["lifecycle"] == "consumed":
                if preview["canonical_hash"] != body.expected_hash:
                    raise _safe_error(
                        "SNAPSHOT_CHANGED", reason_key="snapshot_changed",
                        next_action=_action("new_preview",
                                            target={"kind": "question_plan",
                                                    "id": str(preview["question_plan_id"])}))
                conn.rollback()
                return _confirm_response(preview, replayed=True)

            if preview["lifecycle"] != "open":
                raise _safe_error(
                    "PREVIEW_EXPIRED", reason_key="preview_expired",
                    next_action=_action("new_preview",
                                        target={"kind": "question_plan",
                                                "id": str(preview["question_plan_id"])}),
                    details={"previewId": preview_id})

            # hash 重验必须**先于**任何写:所见即所签
            if preview["canonical_hash"] != body.expected_hash:
                raise _safe_error(
                    "SNAPSHOT_CHANGED", reason_key="snapshot_changed",
                    next_action=_action("new_preview",
                                        target={"kind": "question_plan",
                                                "id": str(preview["question_plan_id"])}),
                    details={"previewId": preview_id})

            # expiry 用 **DB 的 NOW()** 判,不用应用进程时钟 ——
            # 两台机器时钟漂半分钟就会出现「一台说过期一台说没过期」。
            cur.execute("SELECT (%s < NOW()) AS expired", (preview["expires_at"],))
            if cur.fetchone()["expired"]:
                raise _safe_error(
                    "PREVIEW_EXPIRED", reason_key="preview_expired",
                    next_action=_action("new_preview",
                                        target={"kind": "question_plan",
                                                "id": str(preview["question_plan_id"])}),
                    details={"previewId": preview_id})

            # 🔴 [#62 2026-09-05] 题单在 preview 之后被改版 ⇒ 这次确认绑的是**旧题单**。
            #    创建 preview 时(:732)已经有同名的一道闸,但那道闸只看创建那一刻;
            #    这个 bug 正好长在两者之间:前端重建了题单(新 revision),
            #    confirm 仍拿着旧 preview —— 她**以为**在跑新题单,实际跑的是旧的,
            #    钱也按旧的冻。两道闸缺一不可:创建侧管「一开始就选了旧版」,
            #    这一侧管「你决定期间它被改版了」——**处境不同,文案与出口也不同**,
            #    所以 reason_key 用新的 ``question_plan_superseded``,不复用创建侧那条。
            #
            #    🔴 必须排在任何写之前:一旦 CAS 把 preview 推到 consumed,
            #    这次确认就「恰好发生过一次」了,再拒也收不回来。
            try:
                _plan_now = plan_store.get_plan_exact_revision(
                    cur, plan_id=str(preview["question_plan_id"]),
                    plan_revision=int(preview["question_plan_revision"]),
                    tenant_owner_user_id=tenant)
            except plan_store.PlanNotFound:
                # 🔴 查不到 plan 行 ⇒ **无法判定有没有被改版**,那就什么都不判。
                #    绝不能在这里返 404:本守卫的职责是「检测改版」,
                #    不是重新校验 plan 存在性 —— preview 自己已经过了 NOT_FOUND 那道闸,
                #    确认用的是 preview 冻结的那份 payload,不依赖 plan 行还在不在。
                #    把「无法判定」压成一个**具体但错误**的结论,会顶掉它后面
                #    真正该给的那个答复 —— 实测:合成 preview(引用不存在的 plan)
                #    本该走到审批闸返 403,被这里的 404 顶掉,
                #    既有判据 `test_approval_required_is_403_with_zero_side_effects` 当场红。
                _plan_now = None
            if _plan_now is not None and _plan_now["superseded_by_revision"] is not None:
                raise _safe_error(
                    "QUESTION_PLAN_NOT_RUNNABLE",
                    reason_key="question_plan_superseded",
                    next_action=_action("refresh_and_retry",
                                        target={"kind": "page", "page": "form"}),
                    details={"previewId": preview_id,
                             "planId": str(preview["question_plan_id"]),
                             "planRevision": int(preview["question_plan_revision"]),
                             "latestRevisionAvailable": plan_store.latest_revision_number(
                                 cur, plan_id=str(preview["question_plan_id"]),
                                 tenant_owner_user_id=tenant)})

            # [A-2 保险丝①] 版本比对。现在它**真的会触发**:版本由价目内容 hash 派生,
            #   改一次价必然换一个版本串(见 `_live_pricing_catalog_version`)。
            #   读不出价目 → POLICY_UNAVAILABLE(零 command 零 freeze),不放行。
            # [E2-2] 版本、价、池策略**全部来自本事务里锁住的那一行**,
            #   而 freeze 走同一个 cursor 读同一张表 —— 两次读之间不再有窗口。
            try:
                _locked_pricing = _lock_pricing_row_for_confirm(
                    cur, str(preview["feature_code"]))
                _live_version = _pricing_catalog_version_from_row(
                    _locked_pricing, str(preview["feature_code"]))
            except PricingCatalogUnreadable:
                # 🔴 [工单 V4-A · P2-1] 下一步给**重新预览**,不是「联系客服」。
                #    这一支现在的主因是「商品在她预览之后被停用了」——
                #    那件事她自己能处理(换一个/重新预览),不需要开工单等人。
                #
                # 🔴 [工单 V5-B · Codex fix-of-fix3 P2-NEW-5] reason key 也跟着换。
                #    上一轮只换了动作、没换文案,于是屏幕上同时出现
                #    「帮你转给平台客服处理」与一颗「重新发起体检」按钮 ——
                #    读到的和看到的互相打架。文案与动作必须同一次决定。
                raise _safe_error(
                    "POLICY_UNAVAILABLE",
                    reason_key="pricing_deactivated_after_preview",
                    next_action=_action("new_preview",
                                        target={"kind": "question_plan",
                                                "id": str(preview["question_plan_id"])})
                ) from None
            if preview["pricing_catalog_version"] != _live_version:
                raise _safe_error(
                    "SNAPSHOT_CHANGED", reason_key="snapshot_changed",
                    next_action=_action("new_preview",
                                        target={"kind": "question_plan",
                                                "id": str(preview["question_plan_id"])}),
                    details={"previewId": preview_id})

            if preview["approval_requirement"] == "required" and not _approval_granted(preview):
                raise _safe_error(
                    "APPROVAL_REQUIRED", reason_key="approval_required",
                    next_action=_action("request_approval",
                                        target={"kind": "approval",
                                                "id": str(preview["preview_id"])}))

            spec = fp_cell(preview["funding_policy"])
            try:
                validate_preview_funding(
                    funding_policy=preview["funding_policy"],
                    principal_kind=preview["principal_kind"],
                    sponsor_policy_ref=preview["sponsor_policy_ref"],
                    approval_requirement=preview["approval_requirement"],
                    base_points=preview["base_points"],
                    extra_points=preview["extra_points"],
                    exact_total_points=preview["exact_total_points"])
            except ProjectionError:
                raise _safe_error(
                    "POLICY_UNAVAILABLE", reason_key="policy_unavailable",
                    next_action=_action("contact_support",
                                        target={"kind": "page", "page": "support"})) from None

            run_token = _mint_run_token()
            cur.execute(
                "UPDATE defgeo_diagnosis_run_previews "
                "SET lifecycle='consumed', consumed_command_id=%s, consumed_at=NOW() "
                "WHERE preview_id=%s AND tenant_owner_user_id=%s AND lifecycle='open' "
                "RETURNING preview_id",
                (run_token, preview_id, tenant))
            if cur.fetchone() is None:
                raise _safe_error("IDEMPOTENCY_CONFLICT")

            admit = _admit_run(
                tenant, idempotency_key.strip(), int(preview["brand_id"]),
                "defgeo_" + run_token, run_token,
                spec.billing_mode_projection, _cursor=cur)
            if not admit.admitted:
                raise _safe_error("IDEMPOTENCY_CONFLICT")

            try:
                handle, freeze_row_id, split_snapshot, payer_user_id = await _freeze_exact(
                    cur, preview=preview, spec=spec, tenant=tenant, run_token=run_token)
            except HTTPException as exc:
                # 🔴 billing 用 HTTPException(402/400) 表示「余额不足 / 参数非法」。
                #    第一版没接这一层,402 **直接穿透**了 SafeError 信封 ——
                #    §15.8 要求错误只走 allowlist,而且她会看到一个没有
                #    「下一步点哪」的裸 402。真 HTTP 判据当场抓到。
                #    这里翻成 typed 信封,动作由**资金矩阵**下发:
                #    个人钱包只给充值/缩题单,组织只给追加审批/缩题单,
                #    平台账压根没有余额概念(矩阵给空,回落到联系客服)。
                if getattr(exc, "status_code", None) not in (400, 402):
                    raise
                options = allowed_insufficient_actions(preview["funding_policy"])
                raise _safe_error(
                    "INSUFFICIENT_POINTS", reason_key="insufficient_points",
                    next_action=(
                        _action(options[0], target={"kind": "page", "page": "wallet"})
                        if options else
                        _action("contact_support", target={"kind": "page", "page": "support"})
                    ),
                ) from None
            # ⑤ 同事务把 run 推进 running。少了这一腿,run 会卡在 pending_freeze:
            #    品牌被 uq_diag_active_per_brand 锁死,10 分钟后 sweeper 收尸把
            #    算力**退回**并让诊断静默死掉,而用户界面显示的是"已开始"。
            from services.diagnosis_runs import start_run_in_caller_txn

            # [A-1 / A-4] 三件**冻结当时才存在**的事实一起落列:句柄 + 三池拆分 + payer。
            #   少落 split → 部分履约结算读不到拆分 → settlement_manual,钱长挂(P1-4);
            #   少落 payer → 结算拿租户 id 去定位平台的冻结 → 永远结不掉(P0-1 第二层)。
            if not start_run_in_caller_txn(
                cur, run_token, freeze_id=freeze_row_id, freeze_backend="legacy",
                split_snapshot=split_snapshot, payer_user_id=payer_user_id,
            ):
                raise RuntimeError(
                    f"run {run_token} 无法从 pending_freeze 进 running —— "
                    "拒绝提交一个会被收尸退款的半状态"
                )

            _enqueue_confirm_outbox(cur, preview=preview, tenant=tenant, run_token=run_token)

        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except plan_store.PlanNotFound:
        # [返修③ P1-1] preview_id 形状不合法 / 不存在 / 不归你 —— 三者同形 404。
        # 🔴 这一条必须排在下面的 ``except Exception`` **之前**:兜底那条会把它
        #    翻成 INTERNAL_ERROR(500),而"链接抄错一位"被告知"系统坏了",
        #    她只会一遍遍重试。判据 test_no_uuid_route_returns_a_bare_500 当场抓到过。
        conn.rollback()
        raise _safe_error("NOT_FOUND") from None
    except Exception:
        conn.rollback()
        logger.exception("[defgeo] confirm 失败 preview=%s", preview_id)
        raise _safe_error("INTERNAL_ERROR") from None
    finally:
        conn.close()

    # ── [门八第三发现] 把「已建、还没开跑」这一格立刻写出去 ────────────────
    #
    # 五腿已经提交:run 真的存在、算力真的冻着。但 cron 执行器要下一轮
    # (生产 20s 周期)才领它,在那之前 `manager.get_task_status` 一片空,
    # `/api/diagnosis/session/{sid}/status` 回 `{"found": false}` ⇒ 进度页挂
    # 「未找到此诊断任务。可能已过期或无权查看,请返回重新发起。」
    # 她刚付完钱,而"重新发起"意味着**再冻一笔** —— 这是资金相邻的误导。
    #
    # legacy 发起路径早就用同一手(`server.py` 注释原话:「防前端 /status 拿到
    # found:False」)。这里复用**同一个写出口** `mark_session_queued`,不另拼一份 payload。
    #
    # 🔴 fail-soft:快照写失败(Redis 抖/进程角色不对)绝不许把一次**已提交、已冻结**
    #    的 confirm 变成 500 —— 那会让她以为没成功而再确认一次。
    #    写不进去的后果只是回到"前端退避重试"那条兜底路,不掉钱。
    # 🔴 只在**非重放**这一支写:重放路径上那次 run 可能已经在跑了,
    #    往回写一个 queued 会把真进度盖掉。
    try:
        from server import mark_session_queued
        from services.defensive_geo.run_executor import progress_session_id as _psid
        # driven_in_process=False:真正跑它的是 cron 进程,不是这个 web 进程。
        # 写进程内兜底会造出一条没人更新的 queued —— Redis 一失联就变成
        # 「永远正在初始化」(P2-RUNTIME-1)。这一格只写 Redis(跨进程权威)。
        mark_session_queued(_psid(run_token), driven_in_process=False)
    except Exception:
        logger.warning(
            "[defgeo] queued 快照没写进去 run=%s —— confirm 已提交,不回滚;"
            "前端退避重试兜底", run_token, exc_info=True)

    return _confirm_response(preview, replayed=False, run_token=run_token, handle=handle)
