"""Frozen organization internal-seat contracts and server-resolved contexts.

This module contains no persistence.  It is the single runtime vocabulary for
organization identity, permissions, feature gates, hashes, and safe errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import json
import logging
import os
from typing import Any, Mapping, Optional, Sequence


logger = logging.getLogger("GEO-OrganizationContract")


SEMANTIC_DEVELOPMENT_BASE_SHA = "dc02115cb2c2b977f5d56ea38a9dea1fe425a4a0"
BASE_REANCHOR_REQUIRED = True
CONTRACT_FREEZE_SHA = "00bf26e642e63de3984af86249b122ac171641ab"
SCHEMA_VERSION = "organization_internal_seats_all_accounts_2026_07_22_v3"

# Shared by organization-seat governance and platform ADMIN audit surfaces.
# This is an access contract over existing authorities, not a second permission
# vocabulary: organization owners use organization.manage; platform admins use
# their existing is_admin/platform_admin.use authority.
GOVERNANCE_AUDIT_ACCESS_CONTRACT = {
    "version": "governance-audit-access-v1",
    "organization_owner": "organization.manage",
    "platform_admin": "platform_admin.use",
}


DELEGABLE_CAPABILITIES = frozenset(
    {
        "clients.read_assigned",
        "clients.profile_edit",
        "diagnosis.read_own",
        "diagnosis.run",
        "diagnosis.export",
        "quote.read_own",
        "quote.create",
        "quote.submit_for_approval",
        "quote.send_external",
        "writing.read_own",
        "writing.generate",
        "writing.review",
        "writing.share_internal",
        "publish.plan",
        "publish.submit_for_approval",
        "publish.execute",
        "monitoring.read_assigned",
        "monitoring.run",
        "monitoring.configure",
        "monitoring.retry",
        "reports.read_own",
        "reports.generate",
        "reports.export",
        "reports.share_external",
        "materials.read_assigned",
        "materials.write",
        "approvals.review",
        "team.output_read",
        "team.output_handoff",
    }
)

OWNER_ONLY_CAPABILITIES = frozenset(
    {
        "organization.manage",
        "organization.dissolve",
        "members.manage",
        "roles.manage",
        "billing.balance_view",
        "billing.manage",
        "inventory.view",
        "inventory.manage",
        "pricing.cost_and_margin_view",
        "pricing.manage",
        "settlements.manage",
        "withdrawals.manage",
        "referral.manage",
        "whitelabel.manage",
        "platform_admin.use",
    }
)

SALES_ROLE_CAPABILITIES = frozenset(
    {
        "clients.read_assigned",
        "clients.profile_edit",
        "materials.read_assigned",
        "diagnosis.read_own",
        "diagnosis.run",
        "diagnosis.export",
        "quote.read_own",
        "quote.create",
        "quote.submit_for_approval",
        "monitoring.read_assigned",
        "monitoring.run",
        "monitoring.retry",
        "reports.read_own",
    }
)

DELIVERY_ROLE_CAPABILITIES = frozenset(
    {
        "clients.read_assigned",
        "materials.read_assigned",
        "materials.write",
        "writing.read_own",
        "writing.generate",
        "writing.review",
        "writing.share_internal",
        "publish.plan",
        "publish.submit_for_approval",
        "publish.execute",
        "monitoring.read_assigned",
        "monitoring.run",
        "monitoring.retry",
        "reports.read_own",
        "reports.generate",
        "reports.export",
    }
)

READONLY_ROLE_CAPABILITIES = frozenset(
    {
        "clients.read_assigned",
        "materials.read_assigned",
        "diagnosis.read_own",
        "quote.read_own",
        "writing.read_own",
        "monitoring.read_assigned",
        "reports.read_own",
        "team.output_read",
    }
)

# Compatibility name for static callers. New organizations receive the three
# split-domain templates below; there is no all-purpose employee role.
DEFAULT_MEMBER_CAPABILITIES = frozenset(
    {
        "clients.read_assigned",
        "clients.profile_edit",
        "materials.read_assigned",
        "diagnosis.read_own",
        "diagnosis.run",
        "diagnosis.export",
        "quote.read_own",
        "quote.create",
        "quote.submit_for_approval",
        "monitoring.read_assigned",
        "monitoring.run",
        "monitoring.retry",
        "reports.read_own",
    }
)

ROLE_TEMPLATE_CAPABILITIES = {
    "sales": SALES_ROLE_CAPABILITIES,
    "delivery": DELIVERY_ROLE_CAPABILITIES,
    "readonly": READONLY_ROLE_CAPABILITIES,
}

ROLE_TEMPLATE_NAMES = {
    "sales": "销售",
    "delivery": "交付",
    "readonly": "只读协作",
}


EXTERNAL_ACTIONS = frozenset(
    {
        "quote.send_external",
        "diagnosis_report.share_external",
        "monitoring_report.share_external",
        "portal.issue_external_token",
        "publish.execute",
    }
)

APPROVAL_ACTIONS = frozenset(
    set(EXTERNAL_ACTIONS)
    | {
        "billing.execute_high_cost",
        "artifact.delete",
        "artifact.archive",
        "artifact.handoff",
    }
)

# One runtime SSOT shared by approval scope checks and the billing adapter.
BILLABLE_FEATURE_CAPABILITIES = {
    "geo_diagnosis": "diagnosis.run",
    "social_diagnosis": "diagnosis.run",
    "full_diagnosis": "diagnosis.run",
    "report_regen": "diagnosis.run",
    "quote_generate": "quote.create",
    "quote_keyword_append": "quote.create",
    "quote_keyword_replace": "quote.create",
    # 🔴 [#106b · 2026-09-06] "keyword_expand" **已删**(该 SKU 于 #106 下架,
    #    种子行于 #106b 删除)。映射指向不存在的商品 = 死代码,而这张表的
    #    两个消费方漏键都会响亮报错(approvals:171 → 503 ORG_FEATURE_UNCLASSIFIED;
    #    service:844 → 422 ORG_LIMIT_FEATURE_INVALID),对已下架商品那正是对的行为。
    "keyword_price": "quote.create",
    "brand_fill": "clients.profile_edit",
    "autofill_brand": "clients.profile_edit",
    "deep_analyze": "clients.profile_edit",
    "article_gen": "writing.generate",
    "article_rewrite": "writing.generate",
    "topic_gen": "writing.generate",
    "report_export": "reports.export",
    "monitor_single": "monitoring.run",
    "monitoring_keyword_daily": "monitoring.run",
    "scheduled_monitoring": "monitoring.run",
    # [商业边界裁决 2026-08-10] `monitor_month_10` / `rank_alert` 已下架,
    #   两条映射一并删除 —— 它们指向的商品不再存在,留着就是指向空气的死代码。
    #   恢复时连同 db/wallet_db.py 的种子行一起加回(见那里的注释)。
    "geo_research_selfserve": "diagnosis.run",
    "media_publish": "publish.execute",
    "media_proxy_publish": "publish.execute",
    # ── GEO 图文(抖音图文笔记)· 裁定 2026-08-17 P0-7「能力映射」 ──
    # 这五个 feature_code 从 2026-08-01 起就在生产扣费,却**从来没有**能力映射:
    # 组织成员走到这些操作时 route classification 拿不到 capability,
    # 按规格 02 §9 只能 handoff owner —— 也就是员工席位实际用不了图文制作。
    # 归档到 writing.generate:与 article_gen / topic_gen 同档(都是"生成内容"),
    # 交付角色本就持有,存量角色无需补授权;销售/只读角色天然拿不到(实测三档模板)。
    # 🔴 制作与投放**分属两条能力**:制作 writing.generate,投放 publish.execute。
    #    合并成一条就等于"能做就能发",违反规格 §9「写作/制作与发布分别授权」。
    "geo_douyin_image_post": "writing.generate",
    "geo_douyin_image_post_regen": "writing.generate",
    "geo_douyin_image_post_extra_card": "writing.generate",
    "geo_douyin_image_post_redraw": "writing.generate",
    "geo_douyin_topic_distill": "writing.generate",
    "mktg_moments_copy": "materials.write",
    "mktg_poster_basic": "materials.write",
    "mktg_poster_pro": "materials.write",
    "mktg_bundle_std": "materials.write",
    "mktg_bundle_pro": "materials.write",
}

APPROVAL_ACTION_CAPABILITIES = {
    "quote.send_external": "quote.submit_for_approval",
    "diagnosis_report.share_external": "reports.share_external",
    "monitoring_report.share_external": "reports.share_external",
    "portal.issue_external_token": "reports.share_external",
    "publish.execute": "publish.submit_for_approval",
    "artifact.handoff": "team.output_handoff",
    "artifact.delete": "team.output_handoff",
    "artifact.archive": "team.output_handoff",
}

LIMIT_KINDS = (
    "daily_total",
    "monthly_total",
    "daily_feature",
    "monthly_feature",
    "custom",
)


# ---------------------------------------------------------------------------
# [文案收口 2026-08-17] 计费/任务层「内部不变量」出口映射
#
# 背景(后端审计 §5,约 80 条):`organization_billing` / `organization_worker` /
# `organization_membership_lifecycle` 里的错误文案是写给 worker 进程看的
# ——「活跃执行路径结算必须携带真实存活 claim_token;kill-9 恢复/对账请改用
# settle_charge_via_reconciliation」这种句子会**一字不差**出现在员工屏幕上。
#
# 修法按工单 §3-3:**不逐条改写,一次性在出口收口**。这里只登记「哪些 code 属于
# 内部不变量」,`as_detail()` 在转 HTTP 响应的**唯一出口**把 message 换成通用人话 +
# 错误码;原文降级进日志与审计,排查能力一点不少。
#
# 判据:凡登记在此的 code,渲染到用户面的 message 必须不含 claim_token / worker /
# durable / occurrence / provider / 幂等键 / 代际 / 快照 / 租约 / 竞态 等内部词。
# 未登记的 code 一律**原样透出**(它们是审计判定 ✅/⚠️ 的可行动文案)。
_INTERNAL_BILLING_COPY = (
    "任务处理异常，费用已冻结保护，请稍后重试；若持续请联系客服并提供错误码 {code}"
)
_INTERNAL_LIFECYCLE_COPY = (
    "退出团队的处理出现异常，请稍后重试；若持续请联系客服并提供错误码 {code}"
)

INTERNAL_INVARIANT_CODES: dict[str, str] = {
    code: _INTERNAL_BILLING_COPY
    for code in (
        # organization_billing.py — 付费记录 / 结算 / 对账 / 租约 内部不变量
        "ORG_CHARGE_ACTOR_CORRUPT",
        "ORG_CHARGE_ACTOR_MISMATCH",
        "ORG_CHARGE_ACTOR_UNSUPPORTED",
        "ORG_CHARGE_APPROVAL_MISSING",
        "ORG_CHARGE_AUTHORITY_EVIDENCE_CORRUPT",
        "ORG_CHARGE_EVIDENCE_CHANGED",
        "ORG_CHARGE_FREEZE_MISSING",
        "ORG_CHARGE_LEASE_ACTIVE",
        "ORG_CHARGE_LEASE_LOST",
        "ORG_CHARGE_LIMIT_LEG_MISSING",
        "ORG_CHARGE_MEMBERSHIP_MISSING",
        "ORG_CHARGE_NOT_EXECUTED",
        "ORG_CHARGE_NOT_FORCE_RELEASABLE",
        "ORG_CHARGE_NOT_QUARANTINABLE",
        "ORG_CHARGE_NOT_RELEASABLE",
        "ORG_CHARGE_NOT_RESERVED",
        "ORG_CHARGE_OUTCOME_MISSING",
        "ORG_CHARGE_RESULT_CORRUPT",
        "ORG_CHARGE_RESULT_NOT_READY",
        "ORG_CLAIM_TOKEN_REQUIRED",
        "ORG_COST_CEILING_UNBOUNDED",
        "ORG_DIAGNOSIS_STATE_CONFLICT",
        "ORG_EXTERNAL_OUTCOME_NOT_AMBIGUOUS",
        "ORG_FORCE_RELEASE_REPLAY_CONFLICT",
        "ORG_PHYSICAL_RELEASE_FAILED",
        "ORG_PLAN_ACTOR_INVALID",
        "ORG_PLAN_OCCURRENCE_MISMATCH",
        "ORG_PLAN_OCCURRENCE_MISSING",
        "ORG_PLAN_OCCURRENCE_NOT_RESERVABLE",
        "ORG_POLL_RECLAIM_CONFLICT",
        "ORG_PROVIDER_ATTEMPT_CHANGED",
        "ORG_PROVIDER_TASK_REQUIRED",
        "ORG_RECONCILIATION_LEASE_ACTIVE",
        "ORG_RECOVERY_IDENTITY_REQUIRED",
        "ORG_RECOVERY_IDENTITY_UNTRUSTED",
        "ORG_RECOVERY_STATE_NOT_DURABLE",
        "ORG_REFUND_LEDGER_MISMATCH",
        "ORG_SETTLE_ARTIFACT_MISMATCH",
        "ORG_SETTLE_MODE_AMBIGUOUS",
        "ORG_SETTLE_MODE_REQUIRED",
        "ORG_SETTLE_OUTCOME_MISMATCH",
        "ORG_SYSTEM_LIMIT_LEG_FORBIDDEN",
        "ORG_SYSTEM_PAYER_MISMATCH",
        "ORG_WORK_ACTOR_CORRUPT",
        "ORG_WORK_LEASE_LOST",
        "ORG_WORK_SNAPSHOT_INVALID",
    )
}
INTERNAL_INVARIANT_CODES.update(
    {
        code: _INTERNAL_LIFECYCLE_COPY
        for code in (
            # organization_membership_lifecycle.py — 员工退出的内部一致性检查
            "ORG_LEGACY_SNAPSHOT_CONFLICT",
            "ORG_LEGACY_SNAPSHOT_MISSING",
            "ORG_OPERATOR_ACCOUNT_ANCHOR_MISSING",
        )
    }
)


def public_error_message(code: str, message: str) -> str:
    """Project an internal-invariant message into user-facing plain language."""
    template = INTERNAL_INVARIANT_CODES.get(code)
    return template.format(code=code) if template else message


class OrganizationError(RuntimeError):
    """Safe domain error suitable for conversion to an HTTP response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        retryable: bool = False,
        safe_details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = int(http_status)
        self.retryable = bool(retryable)
        self.safe_details = dict(safe_details or {})

    def as_detail(self, request_id: str) -> dict[str, Any]:
        public_message = public_error_message(self.code, self.message)
        if public_message != self.message:
            # 原文不进响应,但必须进日志 —— 客服/排查靠 request_id + error_code 反查。
            logger.warning(
                "[organization] internal invariant surfaced code=%s request_id=%s internal_message=%s",
                self.code, request_id, self.message,
            )
        detail: dict[str, Any] = {
            "code": self.code,
            "message": public_message,
            "request_id": request_id,
            "retryable": self.retryable,
        }
        details = dict(self.safe_details)
        if public_message != self.message:
            details["error_code"] = self.code
        if details:
            detail["details"] = details
        return detail


@dataclass(frozen=True)
class IdentityContext:
    request_id: str
    authenticated_user_id: int
    principal_user_id: int
    payer_user_id: int
    actor_kind: str
    organization_id: int
    organization_status: str = "active"
    membership_status: str = "active"
    actor_user_id: Optional[int] = None
    membership_id: Optional[int] = None
    role_id: Optional[int] = None
    membership_version: Optional[int] = None
    capability_version: Optional[int] = None
    assignment_version: Optional[int] = None
    organization_version: int = 1
    organization_authority_version: int = 1
    requested_by_user_id: Optional[int] = None
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def authority_version(self) -> str:
        return ":".join(
            str(value)
            for value in (
                self.organization_authority_version,
                self.membership_version or 0,
                self.capability_version or 0,
                self.assignment_version or 0,
            )
        )

    @property
    def is_owner(self) -> bool:
        return self.actor_kind == "owner"

    @property
    def is_member(self) -> bool:
        return self.actor_kind == "member"

    @property
    def is_system(self) -> bool:
        return self.actor_kind == "system"

    def require(self, capability: str) -> None:
        if self.is_owner:
            return
        if capability not in self.capabilities:
            raise OrganizationError(
                "ORG_CAPABILITY_DENIED",
                "你的账号没有这项权限，请找团队老板开通",
                http_status=403,
            )

    def require_any(self, capabilities) -> None:
        """[R3-P7 ④] 通用路由外层守卫用:持有其中**任意一项**即放行。

        为什么需要它:``/api/xiaobang/operations/{operation_id}/...`` 一条路径
        通向**多个** operation,各自要求不同 capability(``writing.generate`` /
        ``publish.execute``)。外层守卫只能声明一个 capability 的话,必然出现
        「能力发现把某个 operation 下发给了这个员工,而外层守卫把他 403 掉」
        —— 判据看两边都"对",用户看到的是功能不存在。

        🔴 放行**不等于**授权:精确校验在 handler 的 ``_authorize()``
        (它按该 operation 的 ``required_capability`` 再问一次)。
        外层守卫的职责是「这条路径对这个身份是否可达」,不是「这个动作是否被允许」。
        """
        wanted = tuple(str(c) for c in (capabilities or ()) if str(c))
        if self.is_owner:
            return
        if not wanted:
            raise OrganizationError(
                "ORG_CAPABILITY_DENIED",
                "你的账号没有这项权限，请找团队老板开通",
                http_status=403,
            )
        if not any(capability in self.capabilities for capability in wanted):
            raise OrganizationError(
                "ORG_CAPABILITY_DENIED",
                "你的账号没有这项权限，请找团队老板开通",
                http_status=403,
            )

    def require_active_organization(self) -> None:
        if self.organization_status != "active":
            raise OrganizationError(
                "ORG_INACTIVE",
                "团队已停用，当前仅团队负责人可处理审批、账目等善后事项",
                http_status=403,
            )


@dataclass(frozen=True)
class BillingActorContext:
    """Backward-compatible payer/actor split passed to billing primitives.

    ``None`` remains the old single-user path.  An instance always represents
    an organization action and therefore requires pre-reservation.
    """

    identity: IdentityContext
    feature_code: str
    execution_id: str
    estimated_points: int
    reserved_ceiling_points: int
    pricing_version: str
    pricing_snapshot_hash: str
    brand_id: Optional[int] = None
    task_ref: Optional[str] = None
    applicable_spend_limit_ids: tuple[int, ...] = ()
    approval_request_id: Optional[int] = None
    approval_policy_version: Optional[int] = None
    physical_reservation_only: bool = False
    # Explicit payer-policy exemption evidence for the member overage leg. A
    # member reservation whose employee-limit leg does not cover the full
    # ceiling must carry the locked policy version it was consented under;
    # pure-overage charges use this instead of spend-limit legs.
    payer_policy_version: Optional[int] = None
    payer_overage_points: int = 0

    def __post_init__(self) -> None:
        if self.reserved_ceiling_points <= 0:
            raise ValueError("reserved_ceiling_points must be positive")
        if self.estimated_points < 0:
            raise ValueError("estimated_points must be non-negative")
        if self.estimated_points > self.reserved_ceiling_points:
            raise ValueError("estimated_points exceeds reserved ceiling")
        if self.payer_overage_points < 0:
            raise ValueError("payer_overage_points must be non-negative")
        if self.payer_overage_points > self.reserved_ceiling_points:
            raise ValueError("payer_overage_points exceeds reserved ceiling")
        if self.payer_policy_version is not None:
            if not self.identity.is_member:
                raise ValueError("payer policy exemption evidence is member-only")
            if int(self.payer_policy_version) < 1:
                raise ValueError("payer_policy_version must be positive")
        has_limit_legs = bool(self.applicable_spend_limit_ids)
        has_overage_evidence = self.payer_policy_version is not None
        if self.identity.is_member and not has_limit_legs and not self.physical_reservation_only and not has_overage_evidence:
            raise ValueError("member billing requires every applicable limit id")
        if self.identity.is_member and not has_limit_legs and has_overage_evidence and self.payer_overage_points <= 0:
            raise ValueError("member billing without limit legs requires a positive overage leg")
        if self.identity.is_system and self.applicable_spend_limit_ids:
            raise ValueError("system billing cannot consume member limits")
        if self.identity.is_system and self.payer_policy_version is not None:
            raise ValueError("system billing cannot use payer policy exemption")
        if self.identity.payer_user_id != self.identity.principal_user_id:
            raise ValueError("payer must equal principal")


def require_governance_audit_access(
    *,
    identity: Optional[IdentityContext] = None,
    is_platform_admin: bool = False,
) -> str:
    """Resolve the shared audit scope from existing ADMIN/organization truth."""
    if is_platform_admin:
        return "platform"
    if identity is not None and identity.is_owner:
        identity.require("organization.manage")
        return f"organization:{identity.organization_id}"
    raise OrganizationError(
        "GOVERNANCE_AUDIT_ACCESS_DENIED",
        "仅团队负责人可查看操作记录。",
        http_status=403,
        safe_details={"contract": GOVERNANCE_AUDIT_ACCESS_CONTRACT["version"]},
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_key(*parts: Any) -> str:
    return sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def secure_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left), str(right))


def _env_bool(name: str) -> bool:
    raw = os.getenv(name, "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise OrganizationError(
        "ORG_FLAG_INVALID",
        "系统配置异常，请联系平台客服",
        http_status=503,
        safe_details={"flag": name},
    )


def feature_flags() -> dict[str, bool]:
    seats = _env_bool("ORGANIZATION_SEATS_ENABLED")
    onboarding = _env_bool("ORGANIZATION_INVITE_ONBOARDING_ENABLED")
    payer = _env_bool("ORGANIZATION_SHARED_PAYER_ENABLED")
    external = _env_bool("ORGANIZATION_EXTERNAL_ACTIONS_ENABLED")
    if onboarding and not seats:
        raise OrganizationError("ORG_FLAG_DEPENDENCY_INVALID", "该功能暂未开放，请联系平台客服", http_status=503)
    if payer and not seats:
        raise OrganizationError("ORG_FLAG_DEPENDENCY_INVALID", "该功能暂未开放，请联系平台客服", http_status=503)
    if external and not payer:
        raise OrganizationError("ORG_FLAG_DEPENDENCY_INVALID", "该功能暂未开放，请联系平台客服", http_status=503)
    return {
        "ORGANIZATION_SEATS_ENABLED": seats,
        "ORGANIZATION_INVITE_ONBOARDING_ENABLED": onboarding,
        "ORGANIZATION_SHARED_PAYER_ENABLED": payer,
        "ORGANIZATION_EXTERNAL_ACTIONS_ENABLED": external,
    }


def require_feature_flag(name: str) -> None:
    flags = feature_flags()
    if not flags.get(name, False):
        code = {
            "ORGANIZATION_SEATS_ENABLED": "ORG_SEATS_DISABLED",
            "ORGANIZATION_INVITE_ONBOARDING_ENABLED": "ORG_INVITE_ONBOARDING_DISABLED",
            "ORGANIZATION_SHARED_PAYER_ENABLED": "ORG_SHARED_PAYER_DISABLED",
            "ORGANIZATION_EXTERNAL_ACTIONS_ENABLED": "ORG_EXTERNAL_ACTIONS_DISABLED",
        }.get(name, "ORG_FLAG_DISABLED")
        raise OrganizationError(code, "团队功能暂未开放，请联系平台客服", http_status=503)


def capability_snapshot_hash(capabilities: Sequence[str]) -> str:
    return payload_hash(sorted(set(capabilities)))


def validate_capabilities(capabilities: Sequence[str]) -> frozenset[str]:
    values = frozenset(str(value).strip() for value in capabilities if str(value).strip())
    unknown = values - DELEGABLE_CAPABILITIES - OWNER_ONLY_CAPABILITIES
    if unknown:
        raise OrganizationError(
            "ORG_CAPABILITY_UNKNOWN",
            "权限列表里有无效项，请重新选择",
            http_status=422,
            safe_details={"capabilities": sorted(unknown)},
        )
    forbidden = values & OWNER_ONLY_CAPABILITIES
    if forbidden:
        raise OrganizationError(
            "ORG_OWNER_ONLY_CAPABILITY",
            "老板专属权限不能授予员工",
            http_status=422,
            safe_details={"capabilities": sorted(forbidden)},
        )
    return values
