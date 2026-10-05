"""全层 0 命中 → 「疑似品牌识别失败」判定（P0-1 ② · 2026-07-26）。

生产实证：``brands.id=278`` 的 name 带换行与 "城市:" 标签串，品牌识别永远匹配不上，
诊断 **456 / 468 双双 0 分**，客户付费两次拿到废报告。同一家公司换成干净名字
（``brands.id=712``）后立刻得 20 分。

问题不是「分数是 0」，而是**系统把自己的识别失败当成了客户的业务结论**交付出去。

--------------------------------------------------------------------------
判定与边界
--------------------------------------------------------------------------
这里**不新增任何硬阻断**：报告照出、原始回答照存、客户照样能看。
只是把这一次的交付标成 ``suspected_identity_failure``，让报告展示"疑似识别失败 +
建议改名重测"，并允许资金侧按未履约处理（走既有 freeze/release 原语，见
``workflows/diagnosis_workflow`` 调用点）。这是 §11 的 **A1**（提示 + 定位 +
人工继续），不是 H0。

判定条件（必须同时满足，宁可漏判不可误判）：

1. 三层漏斗**都有确定样本**且**确定命中总数 = 0**
   （只有 PENDING/UNKNOWN 的情况不算 —— 那是待确认，另有出口）；
2. 有效样本量达到最低门槛（默认 ≥8），避免"就测了 2 题都没中"被误判成识别失败；
3. 至少命中一条**识别可疑信号**：
   - 品牌名本身畸形（``utils.brand_name_hygiene``）；
   - 品牌名在 AI 回答原文里**以子串形式出现过**却仍判未命中
     （这是识别器与原文打架的最强信号）；
   - PENDING_IDENTITY 占比偏高（≥20% 的格子是"疑似提到"）。

只有条件 1+2 成立、条件 3 不成立时，返回 ``zero_but_no_identity_signal``：
那是真·完全未被收录（P1-10 的"起点基线"叙事），不是识别失败。
"""

from __future__ import annotations

from typing import Any, Final

MIN_SAMPLES_FOR_SUSPICION: Final[int] = 8
PENDING_RATIO_SIGNAL: Final[float] = 0.20

VERDICT_HEALTHY: Final[str] = "ok"
VERDICT_SUSPECTED_IDENTITY_FAILURE: Final[str] = "suspected_identity_failure"
VERDICT_ZERO_NO_SIGNAL: Final[str] = "zero_but_no_identity_signal"
VERDICT_INSUFFICIENT: Final[str] = "insufficient_samples"


def _answer_texts(detail_table: Any) -> list[str]:
    texts: list[str] = []
    for item in detail_table or []:
        if not isinstance(item, dict):
            continue
        for result in (item.get("results") or {}).values():
            if not isinstance(result, dict):
                continue
            for field in ("full_response", "response", "answer_summary"):
                value = result.get(field)
                if isinstance(value, str) and value.strip():
                    texts.append(value)
                    break
    return texts


def _brand_name_appears_in_answers(brand_name: str, detail_table: Any) -> bool:
    """品牌名（或其去噪键）在回答原文里出现过，却仍被判未命中 → 识别器与原文打架。"""
    from utils.brand_name_hygiene import brand_dedupe_key, normalize_brand_name

    clean = normalize_brand_name(brand_name)
    key = brand_dedupe_key(brand_name)
    if len(clean) < 2 and len(key) < 2:
        return False
    for text in _answer_texts(detail_table):
        if clean and len(clean) >= 2 and clean in text:
            return True
        if key and len(key) >= 3 and key in brand_dedupe_key(text):
            return True
    return False


def assess_identity_suspicion(
    *,
    brand_name: str,
    dimension_stats: dict[str, Any] | None,
    detail_table: Any = None,
) -> dict[str, Any]:
    """判定本次诊断是否「疑似品牌识别失败」。

    返回机器合同（§13：必须带 reason / impact / repair_hint / actions）。
    """
    buckets = dimension_stats if isinstance(dimension_stats, dict) else {}
    total = 0
    detected = 0
    pending = 0
    unknown = 0
    for bucket in buckets.values():
        if not isinstance(bucket, dict):
            continue
        total += int(bucket.get("total", 0) or 0)
        detected += int(bucket.get("detected", 0) or 0)
        pending += int(bucket.get("pending_identity", 0) or 0)
        unknown += int(bucket.get("provider_unknown", 0) or 0)

    observed = total + pending + unknown
    base = {
        "definite_samples": total,
        "definite_detected": detected,
        "pending_identity": pending,
        "provider_unknown": unknown,
        "rule_version": "diagnosis-identity-suspicion-v1",
    }

    if detected > 0:
        return {**base, "verdict": VERDICT_HEALTHY, "suspected": False}
    if total <= 0 or observed < MIN_SAMPLES_FOR_SUSPICION:
        # 样本太少不下"识别失败"的结论，也不下"完全未被收录"的结论。
        return {**base, "verdict": VERDICT_INSUFFICIENT, "suspected": False}

    signals: list[str] = []
    from utils.brand_name_hygiene import is_malformed_brand_name, suggest_brand_name

    malformed = is_malformed_brand_name(brand_name)
    if malformed:
        signals.append("brand_name_malformed")
    if _brand_name_appears_in_answers(brand_name, detail_table):
        signals.append("brand_name_present_in_answers")
    if observed and (pending / observed) >= PENDING_RATIO_SIGNAL:
        signals.append("high_pending_identity_ratio")

    if not signals:
        return {**base, "verdict": VERDICT_ZERO_NO_SIGNAL, "suspected": False, "signals": []}

    suggestion = suggest_brand_name(brand_name) if malformed else ""
    return {
        **base,
        "verdict": VERDICT_SUSPECTED_IDENTITY_FAILURE,
        "suspected": True,
        "signals": signals,
        "suggested_brand_name": suggestion or None,
        "code": "DIAGNOSIS_SUSPECTED_IDENTITY_FAILURE",
        "message": "本次全部问题都没有识别到该品牌，疑似品牌名识别失败，结果不作为「0 分」结论交付。",
        "reason": (
            "品牌名是 AI 品牌识别的匹配键。名称畸形、或名称明明出现在回答原文里却判为未命中，"
            "都说明是识别环节没对上，而不是品牌真的没有被 AI 提到。"
        ),
        "impact": "本次不按 0 分交付；需要人工复核并用规范品牌名重测。",
        "repair_hint": (
            f"建议把品牌名改为「{suggestion}」后重测。" if suggestion
            else "建议核对并填写规范的公司/品牌全称（不要带换行或「城市:」这类标签）后重测。"
        ),
        "actions": ["查看逐格判定并人工确认", "修改品牌名后重测", "联系客服复核本次计费"],
    }


def is_suspected_identity_failure(assessment: Any) -> bool:
    return bool(isinstance(assessment, dict) and assessment.get("suspected") is True)
