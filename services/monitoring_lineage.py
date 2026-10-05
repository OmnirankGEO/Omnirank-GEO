"""Monitoring lineage and target-outcome contract.

``monitoring_results`` is the only source record for ordinary paid monitoring.
This module normalizes one write payload; it never writes a parallel ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Final


LINEAGE_VERSION: Final = "monitoring-lineage-v1.0"
OUTCOME_RESOLVER_VERSION: Final = "target-outcome-v1.1-unified-ten-class"

TARGET_OUTCOMES: Final[frozenset[str]] = frozenset({
    "recommended",
    "conditionally_recommended",
    "candidate_only",
    "mentioned_only",
    "criteria_only",
    "refused_no_evidence",
    "refused_risk",
    "not_mentioned",
    "entity_ambiguous",
    "engine_error",
})

# [包F ⑥ · 2026-08-24] 引擎合同 re-export —— 真身在 services/engine_contract.py。
#
# 🔴 为什么不住在本模块:本模块 import 了 ``question_evolution``,而它引
#    ``db.connection``。任何**在事务里惰性 import** 本模块的代码,传递闭包
#    就会碰到 ``db.connection`` —— 而 import 它会触发 init_db 抢
#    ACCESS EXCLUSIVE(2026-08-10 把生产打成 503 十六分钟的自死锁形态)。
#    包F ① 的账本桥正是事务内惰性 import,所以合同必须住在一个零依赖模块里。
#    窗D 既有锁 test_bridge_import_closure_touches_no_db_module 守这一条。
#
# re-export 而不是让调用方改 import:本模块既有调用方零变化。
from services.engine_contract import (  # noqa: F401,E402
    PLATFORM_CONTRACT as _PLATFORM_CONTRACT,
    QWEN_ENGINE,
    QWEN_ENGINE_PREVIOUS_MODEL,
    QWEN_ENGINE_SWITCHED_ON,
)



@dataclass(frozen=True)
class MonitoringLineage:
    sent_question_snapshot: str
    keyword_source: str
    keyword_type: str
    question_family: str
    question_family_version: str
    keyword_resolver_status: str
    provider: str
    model: str
    #: [工单 E3-4 · P1-9b · Codex 二审] 上面那个 ``model`` **从哪来**。
    #:
    #: · ``provider_echo``    —— 调用方给了模型名(供应商回显)⇒ 可当"实际模型"用;
    #: · ``planned_fallback`` —— 调用方没给,这里回落到平台合同里的**计划值** ⇒
    #:                            它回答的是"我们打算发哪个模型",不是"平台实际用了哪个";
    #: · ``unknown``          —— 连计划值都没有。
    #:
    #: 🔴 为什么必须显式分开而不是"反正正常路径下相等":相等是**巧合不是约束**。
    #:    供应商灰度/别名路由、我们改了 ``QWEN_ENGINE["model"]`` 而请求侧发了旧值,
    #:    这两种情况下就不等。而这一列同时是保真度对账与计价的取数口 ——
    #:    把两种来源混在同一列里,等于让"实际模型"这个词证明不了它自己。
    model_source: str
    model_revision: str
    model_revision_unknown_reason: str
    surface: str
    search_mode: str
    response_status: str
    target_brand: str
    target_entity: str
    target_outcome: str
    resolver_version: str
    resolver_confidence: float
    lineage_status: str
    lineage_error_reason: str
    request_id: str
    sent_at: str

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def classify_target_outcome(
    *,
    response_status: str,
    target_brand: str,
    full_response: str,
    is_detected: bool,
    mention_type: str,
    search_citations: str,
) -> tuple[str, float]:
    """Conservative baseline for the unified ten-class outcome contract.

    This persistence boundary can prove hard states and a detected mention. It
    cannot safely infer recommendation intent from a global keyword match, so
    recommendation classes are only accepted from a future authoritative
    ``mention_type`` producer. Citations remain a separate evidence dimension.
    """
    status = str(response_status or "unknown").lower()
    brand = str(target_brand or "").strip()
    text = str(full_response or "")
    mention = str(mention_type or "none").lower()
    if status == "brand_identity_unresolved" or mention == "pending_identity":
        return "entity_ambiguous", 1.0
    if status not in {"success", "ok"}:
        return "engine_error", 1.0
    if not text.strip():
        return "refused_no_evidence", 0.9
    if not brand:
        return "entity_ambiguous", 1.0
    brand_in_answer = brand.casefold() in text.casefold()
    if not is_detected and not brand_in_answer:
        refusal_markers = ("无法推荐", "不能推荐", "不便推荐", "没有足够证据", "无法核实")
        if any(marker in text for marker in refusal_markers):
            risk_markers = ("风险", "安全", "合规", "责任")
            return (
                "refused_risk" if any(marker in text for marker in risk_markers)
                else "refused_no_evidence",
                0.75,
            )
        if any(marker in text for marker in ("选择标准", "评估标准", "核验", "怎么选")):
            return "criteria_only", 0.55
        return "not_mentioned", 0.95
    _ = search_citations  # Citation presence is not a recommendation outcome.
    if mention in {"recommended", "recommendation", "direct_recommendation"}:
        return "recommended", 0.8
    if mention in {"conditionally_recommended", "conditional_recommendation"}:
        return "conditionally_recommended", 0.75
    if mention in {"candidate", "candidate_only", "list", "listed"}:
        return "candidate_only", 0.65
    if brand_in_answer or is_detected:
        return "mentioned_only", 0.6
    return "entity_ambiguous", 0.4


#: ``model`` 那一列的三种来源。**字面值不许在别处手写** ——
#: 手写的那份改一个字母不会让任何判据变红。
MODEL_SOURCE_PROVIDER_ECHO = "provider_echo"
MODEL_SOURCE_PLANNED_FALLBACK = "planned_fallback"
MODEL_SOURCE_UNKNOWN = "unknown"

#: ``lineage_status`` 三态。
#:
#: 🔴 ``complete`` 与 ``model_unconfirmed`` 回答的是**两个不同问题**:
#:    · "血缘各列有没有如实记下来" —— 两者都是"有";
#:    · "``model`` 那一列被供应商证实了吗" —— 只有 ``complete`` 是。
#:    上一版把这两件事压进同一个 ``complete``,于是一个名叫 ``actual_model``
#:    的列被一个名叫 ``complete`` 的状态"确认"过,而两者都只是计划值。
LINEAGE_STATUS_COMPLETE = "complete"
LINEAGE_STATUS_MODEL_UNCONFIRMED = "model_unconfirmed"
LINEAGE_STATUS_EXPLICIT_UNKNOWN = "explicit_unknown"

#: 「血缘如实记录过」的状态全集。下游想问的多半是这个问题(而不是
#: "模型被证实了吗"),所以给出 SSOT,免得每个调用方各自手抄一份 IN 列表。
LINEAGE_RECORDED_STATUSES: tuple[str, ...] = (
    LINEAGE_STATUS_COMPLETE, LINEAGE_STATUS_MODEL_UNCONFIRMED,
)


def recorded_lineage_sql(alias: str = "") -> str:
    """「血缘如实记录过」的 SQL 谓词。分母取自 :data:`LINEAGE_RECORDED_STATUSES`。

    🔴 手抄 ``IN ('complete','model_unconfirmed')`` 的问题不是难看:
       将来再加一档时,漏改的那个调用方会**静默少一批样本**,
       而它的判据仍然全绿(样本变少不会让任何断言失败)。
    """
    prefix = f"{alias}." if alias else ""
    vals = ", ".join(f"'{s}'" for s in LINEAGE_RECORDED_STATUSES)
    return f"{prefix}lineage_status IN ({vals})"


def build_monitoring_lineage(
    *,
    platform: str,
    question: str,
    target_brand: str,
    full_response: str,
    response_status: str,
    is_detected: bool,
    mention_type: str,
    search_citations: str = "",
    search_mode: str = "standard",
    keyword_source: str = "unknown",
    keyword_type: str = "unknown",
    keyword_resolver_status: str = "unknown",
    request_id: str = "",
    sent_at: str = "",
    provider: str = "",
    model: str = "",
    model_source: str = "",
    model_revision: str = "",
    surface: str = "",
) -> MonitoringLineage:
    from services.question_evolution import QUESTION_FAMILY_VERSION, classify_question_family

    platform_key = str(platform or "").strip().lower()
    contract = _PLATFORM_CONTRACT.get(platform_key, {})
    provider = str(provider or contract.get("provider", "unknown"))
    # ══════════════════════════════════════════════════════════════════════
    # 谱系诚实(Codex 终审 P1-9(b) → 三审 P1-5 · 工单 V3-C C-3 **已收口**)
    # ══════════════════════════════════════════════════════════════════════
    # 下面这一段在调用方没拿到回显时回落到 `_PLATFORM_CONTRACT` 的**计划**模型名,
    # 于是账本里那一列(`defgeo_monitoring_attempts.actual_model`,经
    # `run_ledger_bridge.close_for_result(observed_model=...)` 落库)记的是
    # 「我们打算发哪个模型」,不是「平台实际拿哪个模型回答的」。
    #
    # 两者在正常路径上相等,但**相等是巧合不是约束**:
    #   · 供应商侧做灰度/别名路由(`qwen3.7-plus` → 某个具体快照)时不相等;
    #   · 我们自己改了 `QWEN_ENGINE["model"]` 而请求侧因缓存/回退发了旧模型时不相等;
    #   · 而这一列正是保真度对账与「按哪个模型计价」的取数口
    #     (`db/monitoring_db._estimate_monitoring_cost` 用同一份常量算钱)。
    # 也就是说:回落值**证明不了它自己**。所以它现在带着
    # `model_source="planned_fallback"` 与 `lineage_status="model_unconfirmed"`
    # 一起落库 —— 语义写在数据里,不写在注释里。
    #
    # ══════════════════════════════════════════════════════════════════════
    # [工单 V3-C · C-3 · Codex 三审 P1-5] 上面那段 TODO 的另一半,这次做完了。
    # ══════════════════════════════════════════════════════════════════════
    # 🔴 上一版的谓词是 ``provider_echo if str(model).strip() else
    #    planned_fallback`` —— 键在"调用方给没给 model"。而调用方
    #    (``batch_monitor._resolve_runtime_lineage``)对每个平台**恒返**一个
    #    硬编码计划模型名 ⇒ ``planned_fallback`` 一次都不会触发,
    #    生产上每一行都被标成 ``provider_echo``。
    #    也就是说:那次打标不但没把"还没拿到真回显"变成可机读,
    #    反而把它盖成了"每一行都拿到了真回显"。上面这段 TODO 注释
    #    (口径写对了)与它下面这一行(干的正相反)是同一笔 commit 出去的。
    #
    # 🔴 现在的键是**调用方的显式声明**:只有拿到供应商回显、并且
    #    显式声明 ``model_source="provider_echo"`` 时才算证实。
    #    缺省一律 ``planned_fallback`` —— 保守缺省,不给"没声明就当证实"留路。
    declared = str(model_source or "").strip().lower()
    echoed = str(model or "").strip()
    if declared == MODEL_SOURCE_PROVIDER_ECHO and echoed:
        model_source = MODEL_SOURCE_PROVIDER_ECHO
    else:
        model_source = MODEL_SOURCE_PLANNED_FALLBACK
    model = echoed or str(contract.get("model", "unknown"))
    if model == "unknown":
        model_source = MODEL_SOURCE_UNKNOWN
    model_revision = str(model_revision or "unknown")
    surface = str(surface or contract.get("surface", "unknown"))
    outcome, confidence = classify_target_outcome(
        response_status=response_status,
        target_brand=target_brand,
        full_response=full_response,
        is_detected=is_detected,
        mention_type=mention_type,
        search_citations=search_citations,
    )
    problems: list[str] = []
    if not str(question or "").strip():
        problems.append("sent_question_missing")
    if provider == "unknown":
        problems.append("provider_unknown")
    if model == "unknown":
        problems.append("model_unknown")
    if surface == "unknown":
        problems.append("surface_unknown")
    lineage_status = LINEAGE_STATUS_EXPLICIT_UNKNOWN if problems else (
        LINEAGE_STATUS_COMPLETE if model_source == MODEL_SOURCE_PROVIDER_ECHO
        else LINEAGE_STATUS_MODEL_UNCONFIRMED)
    if lineage_status == LINEAGE_STATUS_MODEL_UNCONFIRMED:
        # 🔴 写进 error_reason,让"为什么不是 complete"在库里就能读出来,
        #    不必回头读代码。
        problems.append("model_unconfirmed")
    return MonitoringLineage(
        sent_question_snapshot=str(question or "").strip(),
        keyword_source=str(keyword_source or "unknown"),
        keyword_type=str(keyword_type or "unknown"),
        question_family=classify_question_family(question),
        question_family_version=QUESTION_FAMILY_VERSION,
        keyword_resolver_status=str(keyword_resolver_status or "unknown"),
        provider=provider,
        model=model,
        model_source=model_source,
        model_revision=model_revision,
        model_revision_unknown_reason=("" if model_revision != "unknown" else "provider_not_exposed"),
        surface=surface,
        search_mode=str(search_mode or "standard"),
        response_status=str(response_status or "unknown"),
        target_brand=str(target_brand or "").strip(),
        target_entity=str(target_brand or "").strip(),
        target_outcome=outcome,
        resolver_version=OUTCOME_RESOLVER_VERSION,
        resolver_confidence=confidence,
        lineage_status=lineage_status,
        lineage_error_reason=",".join(problems),
        request_id=str(request_id or ""),
        sent_at=sent_at or datetime.now(timezone.utc).isoformat(),
    )


def normalize_lineage_payload(raw: dict[str, Any] | None, **fallback: Any) -> dict[str, Any]:
    """Accept a captured adapter payload and fill every contract field explicitly."""
    data = dict(raw or {})
    built = build_monitoring_lineage(
        platform=data.get("platform") or fallback.get("platform") or "",
        question=data.get("sent_question_snapshot") or data.get("question") or fallback.get("keyword") or "",
        target_brand=data.get("target_brand") or "",
        full_response=fallback.get("full_response") or data.get("full_response") or "",
        response_status=data.get("response_status") or data.get("status") or "success",
        is_detected=bool(fallback.get("is_detected")),
        mention_type=fallback.get("mention_type") or "none",
        search_citations=fallback.get("search_citations") or "",
        search_mode=data.get("search_mode") or "standard",
        model_source=data.get("model_source") or fallback.get("model_source") or "",
        keyword_source=data.get("keyword_source") or "unknown",
        keyword_type=data.get("keyword_type") or "unknown",
        keyword_resolver_status=data.get("keyword_resolver_status") or "unknown",
        request_id=data.get("request_id") or "",
        sent_at=data.get("sent_at") or "",
        provider=data.get("provider") or "",
        model=data.get("model") or "",
        model_revision=data.get("model_revision") or "",
        surface=data.get("surface") or "",
    ).payload()
    for key in built:
        if data.get(key) not in (None, ""):
            built[key] = data[key]
    if built.get("target_outcome") not in TARGET_OUTCOMES:
        built["target_outcome"] = "entity_ambiguous"
        built["lineage_status"] = "explicit_unknown"
        built["lineage_error_reason"] = "invalid_target_outcome"
    return built
