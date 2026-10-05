"""POR-20 的十张公开 registry —— code / copy / action / route 的唯一真值。

POR-20 逐字点名这十张:``metric_status_reason_registry_v1`` /
``observation_error_registry_v1`` / ``priority_action_policy_v1`` /
``public_priority_metric_reason_registry_v1`` /
``public_priority_availability_reason_registry_v1`` /
``professional_analysis_section_registry_v1`` / ``provider_binding_policy_v1`` /
``provider_action_availability_policy_v1`` / ``provider_handoff_route_registry_v1`` /
``admission_unavailable_reason_registry_v1``。

要求:「keys 与 DTO producer/consumer/action/route union **exact**;
code-copy-primary action、intent/target/capability/effects、scope×route availability、
RBAC/idempotency 与 version/hash 逐行相等;**死/漏 row**、交换合法行或
generation 变化未失效 cache 均拒绝」。

🔴 为什么每张 registry 都长同一个形状
------------------------------------
统一成 ``{code: Row}`` 且 ``Row`` 有固定字段,是为了让
``scripts/defgeo_census/registry_census.py`` 能用 **AST** 机械枚举它们 ——
手写清单不算分母(G-1/G-2)。形状不统一,census 就得对每张表写一套解析,
那本身又变成"手写清单"的另一种形式。

🔴 「死 row」与「漏 row」是两个方向,都要拒
------------------------------------------
- **死 row**:registry 里定义了但没有任何 DTO/consumer 引用 → 说明它已经被
  忘掉了,留着会让人以为某条路径还活着;
- **漏 row**:consumer 引用了一个 registry 里没有的 code → 运行时会拿不到文案,
  最好的情况是显示 code 本身(= 内部枚举裸串上屏 = 验收红)。

两者由 census 在**两个方向**上分别求差集,见判据。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, NamedTuple

#: 十张 registry 的版本名。**顺序即 census 报告顺序**,判据拿它当分母。
REGISTRY_VERSIONS: tuple[str, ...] = (
    "metric_status_reason_registry_v1",
    "observation_error_registry_v1",
    "priority_action_policy_v1",
    "public_priority_metric_reason_registry_v1",
    "public_priority_availability_reason_registry_v1",
    "professional_analysis_section_registry_v1",
    "provider_binding_policy_v1",
    "provider_action_availability_policy_v1",
    "provider_handoff_route_registry_v1",
    "admission_unavailable_reason_registry_v1",
)


class Row(NamedTuple):
    """一行 registry。字段集合对十张表统一 —— 便于 AST census 机械枚举。"""

    code: str
    #: 对外人话。**不得**是内部枚举形态(判据会用形态检测器扫)。
    copy: str
    #: 主动作 kind;无动作的 reason 类填 ``None``。
    primary_action: str | None
    #: 该行归属的能力/路由/意图,按表语义填;无则 None。
    target: str | None
    #: 副作用集合。``()`` = 只读。
    effects: tuple[str, ...]


def _r(code: str, copy: str, action: str | None = None,
       target: str | None = None, effects: tuple[str, ...] = ()) -> Row:
    return Row(code=code, copy=copy, primary_action=action,
               target=target, effects=effects)


# ── 1. metric_status_reason_registry_v1 ────────────────────────────
METRIC_STATUS_REASON: dict[str, Row] = {r.code: r for r in (
    _r("measured", "已测得", "view_evidence"),
    _r("not_measured", "本次没测这一项", "build_comparable_retest_plan"),
    _r("not_applicable", "这一项不适用", None),
    _r("platform_error", "没能拿到平台回答", "retry_platform"),
    _r("insufficient_sample", "样本还不够,先不下结论", "build_comparable_retest_plan"),
)}

# ── 2. observation_error_registry_v1 ───────────────────────────────
OBSERVATION_ERROR: dict[str, Row] = {r.code: r for r in (
    _r("engine_error", "没能拿到平台回答", "retry_platform"),
    _r("entity_ambiguous", "没认准是哪一家", "calibrate_identity"),
    _r("policy_skipped", "按规则这次跳过了", None),
    _r("timeout", "平台这次响应太慢", "retry_platform"),
    _r("rate_limited", "平台这次限流了", "retry_platform"),
)}

# ── 3. priority_action_policy_v1 ───────────────────────────────────
PRIORITY_ACTION_POLICY: dict[str, Row] = {r.code: r for r in (
    _r("content_or_publication_repair", "补内容或补发布",
       "generate_plan", "content", ("mutation",)),
    _r("fact_collection", "补齐品牌资料",
       "ai_autofill", "fact", ("mutation", "billing")),
    _r("identity_calibration", "校准品牌身份",
       "calibrate", "identity", ("mutation",)),
    _r("comparable_retest", "按同样的问题和平台再测一次",
       "start_retest", "retest", ("mutation", "job")),
)}

# ── 4. public_priority_metric_reason_registry_v1 ───────────────────
PUBLIC_PRIORITY_METRIC_REASON: dict[str, Row] = {r.code: r for r in (
    _r("no_applicable_metric_definition", "这一项暂时没有可用的衡量口径", None),
    _r("metric_observation_unavailable", "这一项这次没能观察到", None),
)}

# ── 5. public_priority_availability_reason_registry_v1 ─────────────
#: §15.6 五格 availability(MET-19/42 逐字五格全序)+ Z-3.1 的第四出口。
PUBLIC_PRIORITY_AVAILABILITY_REASON: dict[str, Row] = {r.code: r for r in (
    _r("business_available", "这项已包含在服务里", "contact_provider"),
    _r("requires_quote", "这项需要另外报价", "contact_provider"),
    _r("included_manual_fulfillment", "这项已包含,需人工安排", "contact_provider"),
    _r("manual_quote_required", "这项需人工报价", "contact_provider"),
    _r("scope_confirmation_required", "这项是否包含需要先确认", "contact_provider"),
    _r("ai_autofill_available", "可以让 AI 联网补齐", "ai_autofill",
       "fact", ("mutation", "billing")),
)}

# ── 6. professional_analysis_section_registry_v1 ───────────────────
PROFESSIONAL_ANALYSIS_SECTION: dict[str, Row] = {r.code: r for r in (
    _r("evidence_matrix", "平台 × 问题证据矩阵", "view_evidence"),
    _r("source_visibility", "依据核查", "view_evidence"),
    _r("competitor_cooccurrence", "同场竞品", "view_evidence"),
    _r("scenario_coverage", "问法覆盖", "view_evidence"),
)}

# ── 7. provider_binding_policy_v1 ──────────────────────────────────
PROVIDER_BINDING_POLICY: dict[str, Row] = {r.code: r for r in (
    _r("view_raw_answers", "查看原始回答", "view_raw_answers", "trace"),
    _r("view_plan", "查看题单", "view_plan", "question_plan"),
    _r("generate_pdf", "生成 PDF", "generate_pdf", "pdf_variant",
       ("mutation", "job")),
    _r("demo_read_only", "演示模式·只能看不能动", None, "demo"),
)}

# ── 8. provider_action_availability_policy_v1 ──────────────────────
PROVIDER_ACTION_AVAILABILITY: dict[str, Row] = {r.code: r for r in (
    _r("enabled", "可以操作", None),
    _r("permission_denied", "你的账号没有这个权限", "contact_admin"),
    _r("revoked", "这个权限已被收回", "contact_admin"),
    _r("demo_read_only", "演示模式·只能看不能动", None),
)}

# ── 9. provider_handoff_route_registry_v1 ──────────────────────────
PROVIDER_HANDOFF_ROUTE: dict[str, Row] = {r.code: r for r in (
    _r("request_approval", "请求审批", "request_approval", "approval"),
    _r("view_approver", "查看该找谁批", "view_approver", "approval"),
    _r("top_up", "去充值", "top_up", "wallet"),
    _r("contact_support", "联系平台支持", "contact_support", "support"),
)}

# ── 10. admission_unavailable_reason_registry_v1 ───────────────────
#: 与 ``services.defensive_geo.work_admission.BlockReason`` 一一对应。
#: 判据会把两边求**双向差集** —— 这正是 POR-20 的「keys 与 DTO union exact」。
ADMISSION_UNAVAILABLE_REASON: dict[str, Row] = {r.code: r for r in (
    _r("service_not_activated", "服务还没正式开工", "view_milestone"),
    _r("execution_funding_pending", "待补算力,暂未开工", "top_up"),
    _r("approval_required", "这项支出需要先获得组织审批", "request_approval"),
    _r("scope_cap_exhausted", "这一类交付的预算额度已经用完了", "raise_scope_cap"),
    _r("capability_unavailable", "发布渠道未接入或未就绪", "skip_capability"),
)}


#: registry version → 表。**AST census 从本模块源码机械提取同一张映射**并互校,
#: 所以这里写错、漏写一张都会被 census 逮到,而不是靠人读。
ALL_REGISTRIES: dict[str, dict[str, Row]] = {
    "metric_status_reason_registry_v1": METRIC_STATUS_REASON,
    "observation_error_registry_v1": OBSERVATION_ERROR,
    "priority_action_policy_v1": PRIORITY_ACTION_POLICY,
    "public_priority_metric_reason_registry_v1": PUBLIC_PRIORITY_METRIC_REASON,
    "public_priority_availability_reason_registry_v1":
        PUBLIC_PRIORITY_AVAILABILITY_REASON,
    "professional_analysis_section_registry_v1": PROFESSIONAL_ANALYSIS_SECTION,
    "provider_binding_policy_v1": PROVIDER_BINDING_POLICY,
    "provider_action_availability_policy_v1": PROVIDER_ACTION_AVAILABILITY,
    "provider_handoff_route_registry_v1": PROVIDER_HANDOFF_ROUTE,
    "admission_unavailable_reason_registry_v1": ADMISSION_UNAVAILABLE_REASON,
}


def registry(version: str) -> dict[str, Row]:
    try:
        return ALL_REGISTRIES[version]
    except KeyError:
        raise ValueError(
            f"未知 registry {version!r};合法 = {list(REGISTRY_VERSIONS)}"
        ) from None


def generation_hash() -> str:
    """全部十张表的内容指纹 —— POR-20「generation 变化未失效 cache 拒绝」。

    任何一行的 code/copy/action/target/effects 改动都会改变它,
    调用方拿它做 cache key 就不会用到过期的 registry。
    """
    payload = {
        version: [list(row) for _, row in sorted(table.items())]
        for version, table in sorted(ALL_REGISTRIES.items())
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def all_action_kinds() -> frozenset[str]:
    """全部 registry 用到的 action kind 并集。route 表要能覆盖它。"""
    return frozenset(
        row.primary_action
        for table in ALL_REGISTRIES.values()
        for row in table.values()
        if row.primary_action
    )


def all_effects() -> frozenset[str]:
    return frozenset(
        effect
        for table in ALL_REGISTRIES.values()
        for row in table.values()
        for effect in row.effects
    )
