"""Shared contract for diagnosis brand-mention cell states.

Mirrors ``services/monitoring_identity_review.py`` for the diagnosis chain:

- every question x engine cell maps to exactly one explicit state:
  ``YES`` / ``NO`` / ``PENDING_IDENTITY`` / ``PROVIDER_UNKNOWN`` / ``NOT_COLLECTED``;
- only ``YES`` / ``NO`` cells enter scoring denominators — an unresolved cell
  must never collapse into a fake 0% (the diagnosis counterpart of the
  monitoring ``AGGREGATE_ELIGIBLE_SQL`` semantics);
- ``PENDING_IDENTITY`` ("疑似提到") cells are human-reviewable and are listed
  separately as 待确认 until an operator decision lands;
- ``PROVIDER_UNKNOWN`` cells are provider/verifier failures (retry semantics,
  no reviewable candidate);
- ``NOT_COLLECTED`` cells were planned but never collected (query failure or
  a missing engine slot), so there is nothing a human could confirm.

Legacy compatibility: cells persisted before ``brand_verdict`` existed fall
back to the historical boolean (``brand_detected`` -> YES/NO).  Cells with an
explicit stored ``UNKNOWN`` verdict keep it out of every denominator and are
re-classified locally (zero provider calls) when a candidate is needed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Final, Optional

VERDICT_YES: Final = "YES"
VERDICT_NO: Final = "NO"
STATE_PENDING_IDENTITY: Final = "PENDING_IDENTITY"
STATE_PROVIDER_UNKNOWN: Final = "PROVIDER_UNKNOWN"
STATE_NOT_COLLECTED: Final = "NOT_COLLECTED"

EXPLICIT_STATES: Final = (
    VERDICT_YES,
    VERDICT_NO,
    STATE_PENDING_IDENTITY,
    STATE_PROVIDER_UNKNOWN,
    STATE_NOT_COLLECTED,
)

# Resolver reasons (BrandDecision.reason) that mean a human-reviewable
# identity candidate exists.  Aligned with BrandIdentityResolver exits.
PENDING_IDENTITY_REASONS: Final = frozenset({
    "local_evidence_requires_review",
    "ambiguous_identity_candidate",
    "invalid_matched_text",
    "identity_decision_conflict",
})

# Resolver/transport reasons that mean the provider path failed; a human can
# still read the raw answer, but the system has no candidate to offer and the
# cell follows retry semantics instead of review semantics.
PROVIDER_UNKNOWN_REASONS: Final = frozenset({
    "identity_load_failed",
    "provider_not_sent",
    "provider_result_unknown",
    "timeout",
    "verifier_exception",
    "lease_lost",
})

# Human-review terminal states persisted back into a decided cell.
REVIEW_STATE_CONFIRMED: Final = "confirmed"
REVIEW_STATE_REJECTED: Final = "rejected"

# funnel dimension buckets, aligned with workflows/diagnosis_workflow.py and
# tools/scoring/funnel_score.py FUNNEL_LAYERS stats_key values.
DIMENSION_BUCKETS: Final = ("brand_awareness", "regional_industry", "super_tier1")


def cell_answer_is_query_failure(cell: dict) -> bool:
    """Same query-failure predicate the workflow aggregation already used."""
    summary = str(cell.get("answer_summary") or "")
    return "查询失败" in summary or summary.startswith("Error")


def _local_rejudge(cell: dict, identity: Any) -> Any:
    """Deterministic local re-judgement of one raw answer; never calls a provider."""
    from services.brand_identity_resolver import BrandIdentityResolver

    answer = str(cell.get("full_response") or "")
    resolver = BrandIdentityResolver(identity)
    return resolver.resolve_local(answer)


def classify_cell_state(
    cell: Any,
    *,
    identity: Any = None,
    allow_local_rejudge: bool = True,
) -> str:
    """Map one persisted detail_table cell to the explicit five-state verdict.

    ``identity`` (a loaded BrandIdentity) enables the deterministic local
    re-judgement used for legacy UNKNOWN cells; when it is None, legacy cells
    without a stored ``detection_reason`` fail closed to PROVIDER_UNKNOWN
    rather than being silently counted.
    """
    if not isinstance(cell, dict):
        return STATE_NOT_COLLECTED

    raw_verdict = str(cell.get("brand_verdict") or "").strip().upper()
    if raw_verdict == VERDICT_YES:
        return VERDICT_YES
    if raw_verdict == VERDICT_NO:
        return VERDICT_NO
    if not raw_verdict:
        # [2026-07-22 R3 · P2] 查询失败格守卫:无 brand_verdict 的历史诊断里,
        # answer_summary 带查询失败语义的格子是"没采到"而非"未命中" —— 当 NO 进分母
        # 会压低分数。仅用 answer_summary 谓词(与下方 UNKNOWN 分支同口径),
        # 不加 full_response 非空要求,避免旧数据大面积 NOT_COLLECTED 反向通胀。
        if cell_answer_is_query_failure(cell):
            return STATE_NOT_COLLECTED
        # Legacy compat: bool-only cells keep their historical meaning.
        return VERDICT_YES if cell.get("brand_detected") else VERDICT_NO

    # Stored UNKNOWN (or any unrecognized marker): classify explicitly.
    if not str(cell.get("full_response") or "").strip() or cell_answer_is_query_failure(cell):
        return STATE_NOT_COLLECTED

    reason = str(cell.get("detection_reason") or "").strip()
    if reason in PENDING_IDENTITY_REASONS:
        return STATE_PENDING_IDENTITY
    if reason in PROVIDER_UNKNOWN_REASONS:
        return STATE_PROVIDER_UNKNOWN

    # Human-decided cells always persist YES/NO, so reaching here means the
    # cell is an unresolved UNKNOWN without a stored reason (legacy data).
    if identity is not None and allow_local_rejudge:
        try:
            decision = _local_rejudge(cell, identity)
        except Exception:
            return STATE_PROVIDER_UNKNOWN
        local_reason = str(getattr(decision, "reason", "") or "")
        if local_reason in PROVIDER_UNKNOWN_REASONS:
            return STATE_PROVIDER_UNKNOWN
        # A deterministic YES/NO on re-judge or a local ambiguous candidate
        # both mean a human-reviewable mention candidate exists.
        return STATE_PENDING_IDENTITY

    # Fresh collections always persist detection_reason; anything else fails
    # closed out of the denominator.
    return STATE_PROVIDER_UNKNOWN


def cell_candidates(
    cell: dict,
    *,
    identity: Any = None,
) -> tuple[list[str], Optional[str]]:
    """Return (candidate display names, evidence snippet) for a reviewable cell.

    Reads stored identity fields first; falls back to one deterministic local
    re-judgement for legacy cells.  Never calls a provider.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            candidates.append(text)

    for value in cell.get("identity_candidates") or []:
        _add(value)
    _add(cell.get("matched_text"))
    snippet = cell.get("identity_evidence_snippet") or None

    if not candidates and identity is not None and str(cell.get("full_response") or "").strip():
        try:
            decision = _local_rejudge(cell, identity)
        except Exception:
            decision = None
        if decision is not None:
            _add(getattr(decision, "matched_alias", None))
            snippet = snippet or getattr(decision, "evidence_snippet", None)

    # [WO 2026-08-06 §1] 近失兜底 —— 存量格没有 near_miss 字段也要能确认。
    #
    # 🔴 这一段是给**已经落库的 21 份诊断 / 90 格**用的(生产实查):它们是在近失
    # 层上线之前采的,``identity_candidates`` 恒空,``resolve_local`` 也只会再吐一次
    # UNKNOWN(matched_alias=None)—— 卡片永远零候选。而原始答案还在
    # ``full_response``、同格品牌名单还在 ``mentioned_brands``,候选是**算得出来的**,
    # 不需要重跑引擎、不需要重新扣费。
    # 🔴 仍然只补候选、不碰 verdict。
    if not candidates and identity is not None:
        candidate_pool = [
            *(cell.get("mentioned_brands") or []),
            *(cell.get("brand_variants_found") or []),
        ]
        if candidate_pool:
            try:
                from services.brand_name_near_miss import near_miss_candidates_for_identity

                for item in near_miss_candidates_for_identity(identity, candidate_pool):
                    _add(item.display)
            except Exception:
                pass

    return candidates[:5], snippet


def is_identity_pending_cell(cell: Any) -> bool:
    """该格是不是「身份待确认」—— 出现率分母把它排除掉的那一类。

    [WO_UNKNOWN_DENOMINATOR_DISCLOSURE 2026-08-07] 披露口径的**单点判据**。
    两条设计约束:

      · **与 ``classify_cell_state`` 同源**,不另立第二套判定 —— 分母怎么算的、
        披露就怎么数,否则"说的 N"和"扣掉的 N"会对不上,那比不说更坏;
      · **只读落库字段**(``allow_local_rejudge=False`` + 不传 identity):
        披露是展示层动作,不许为了数个数去读库/加载身份/跑重判。
        历史格缺 ``detection_reason`` 时 fail-closed 归 PROVIDER_UNKNOWN
        (= 不计入待确认 → 不渲染披露),宁可少说,不虚报。

    🔴 本函数**不改任何统计口径**(工单 §2 / Owner 边界①):它只回答"这一格属于
    被排除的那一类吗",分母怎么算一个字没动。
    """
    return classify_cell_state(
        cell, identity=None, allow_local_rejudge=False
    ) == STATE_PENDING_IDENTITY


def cell_near_miss(cell: dict, *, identity: Any = None) -> list[dict]:
    """该格的「疑似同品牌变体」列表(给报告/卡片展示用)。

    [WO 2026-08-06 §1.2-1] 与 ``cell_candidates`` 的分工:
      · ``cell_candidates`` 服务的是**待确认格**(PENDING_IDENTITY)的按钮;
      · 本函数服务的是**已判 NO 的格** —— 它们不进待确认队列(verdict 是明确的
        「未提到」,测量诚实不破),但原文里确实出现了近似写法,代理有权知道
        并一键确认。诊断 561 的 7 个 NO 格全在这一类:复核层理由逐条写着
        「证据中为'阿强龙虾'」,却没有任何一个界面把这句话给到代理。

    落库有 ``near_miss_candidates`` 就直接用(新采的格);没有就现算(存量格)。
    """
    stored = cell.get("near_miss_candidates")
    if isinstance(stored, list) and stored:
        return [item for item in stored if isinstance(item, dict)]
    if identity is None:
        return []
    candidate_pool = [
        *(cell.get("mentioned_brands") or []),
        *(cell.get("brand_variants_found") or []),
    ]
    if not candidate_pool:
        return []
    try:
        from services.brand_name_near_miss import near_miss_candidates_for_identity

        return [
            item.as_dict()
            for item in near_miss_candidates_for_identity(identity, candidate_pool)
        ]
    except Exception:
        return []


def build_diagnosis_cell_evidence_hash(
    *,
    brand_id: int,
    diagnosis_id: int,
    question: str,
    engine: str,
    full_response: str,
) -> str:
    """Stable evidence fingerprint for one diagnosis cell (optimistic concurrency)."""
    payload = {
        "brand_id": int(brand_id),
        "diagnosis_id": int(diagnosis_id),
        "question": str(question or ""),
        "engine": str(engine or ""),
        "response_sha256": hashlib.sha256(
            str(full_response or "").encode("utf-8")
        ).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def aggregate_dimension_stats(
    detail_table: Any,
    question_types: Any,
    *,
    classifier: Optional[Callable[[Any], str]] = None,
    brand_name: str = "",
    brand_aliases: Any = (),
    exempt_questions: Any = (),
) -> dict:
    """Aggregate detail_table into funnel buckets with PENDING semantics.

    Only YES/NO cells enter ``total``/``detected``; unresolved cells are
    counted in their own explicit buckets so reports can show 待确认 instead
    of a fake 0% when no definite sample exists.  Bucket keys stay compatible
    with ``calculate_funnel_score`` (total/detected are unchanged in shape).

    [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2] 漏斗分层完全由 ``question_types``
    的标签决定 —— 上游把一道公司题标成 super_tier1,这里就把它算进场景转化层,
    而公司题必然命中:报告 551 的场景转化层 4/8=50% 里 4 个命中**全部**来自那一道,
    该层 20/40 分是虚的。传入 ``brand_name`` 后按题面纠正归层;
    不传则行为与改前逐字一致(向后兼容,老调用方零影响)。
    """
    from services.brand_directed_question import is_brand_directed_text

    brand_name = (brand_name or "").strip()
    # [#149 2026-09-08] 逐题豁免:题单里**客户自己写的**那些题不做归层纠正
    #   (她想问什么就照问)。改前是整体豁免(传 brand_name="" 一刀切),
    #   AI 出的品牌定向题进来后那样会把它也豁免掉,顶高提及率。
    #   🔴 默认空 ⇒ 与改前**逐字节相同**,老调用方零影响。
    #   谁享豁免由 `services.diagnosis_question_origin` **一处**判定,不在这里再写一遍。
    _exempt = frozenset(exempt_questions or ())
    buckets: dict[str, dict[str, int]] = {
        key: {
            "total": 0,
            "detected": 0,
            "pending_identity": 0,
            "provider_unknown": 0,
            "not_collected": 0,
        }
        for key in DIMENSION_BUCKETS
    }
    classify = classifier or (lambda cell: classify_cell_state(cell))
    types = question_types if isinstance(question_types, dict) else {}
    for item in detail_table or []:
        if not isinstance(item, dict):
            continue
        question = item.get("question", "")
        q_type = types.get(question, "super_tier1")
        if (
            brand_name
            and q_type != "brand_awareness"
            and question not in _exempt
            and is_brand_directed_text(question, brand_name, aliases=brand_aliases or ())
        ):
            q_type = "brand_awareness"  # 文本为准:题面含品牌名 = 品牌认知层
        if q_type not in buckets:
            continue
        results = item.get("results") or {}
        if not isinstance(results, dict):
            continue
        for eng_result in results.values():
            state = classify(eng_result)
            bucket = buckets[q_type]
            if state == VERDICT_YES:
                bucket["total"] += 1
                bucket["detected"] += 1
            elif state == VERDICT_NO:
                bucket["total"] += 1
            elif state == STATE_PENDING_IDENTITY:
                bucket["pending_identity"] += 1
            elif state == STATE_PROVIDER_UNKNOWN:
                bucket["provider_unknown"] += 1
            else:
                bucket["not_collected"] += 1
    return buckets
