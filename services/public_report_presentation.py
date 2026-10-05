"""Build the public report presentation payload from customer-owned V2 modules.

This module is deliberately pure: it never reads internal modules, calls an
LLM, or recalculates the canonical report score. Missing data stays missing.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from typing import Any, Iterable
from urllib.parse import urlsplit

from services.report_html_renderer import get_client_report_modules


_OUTCOME_MAP = {
    "recommended": "recommended",
    "explicit_recommendation": "recommended",
    "conditionally_recommended": "conditionally_recommended",
    "conditional_recommendation": "conditionally_recommended",
    "candidate": "candidate",
    "candidate_only": "candidate",
    "mentioned": "mentioned",
    "mentioned_only": "mentioned",
    "criteria_only": "criteria_only",
    "refused_no_evidence": "refused_no_evidence",
    "refused_risk": "refused_risk",
    "not_mentioned": "not_mentioned",
    "entity_ambiguous": "brand_confused",
    "brand_confused": "brand_confused",
    "engine_error": "engine_error",
    "no_answer": "no_answer",
}
# [P0-3 · 2026-07-26 Owner 裁决] 推荐口径放宽。
#
# 生产实证:mention_type 只落 direct / none / llm_verified_fallback / pending_identity，
# 没有任何"推荐"档，所以报告里的推荐率恒 0%，同时 UI 还并排显示"仅提到 / 推荐"
# 两个标签 —— 后者永远取不到值，等于假标签。
#
# Owner 明确"口径要放宽，不要『最优推荐』这种严苛标准"：**出现在推荐列表、
# 被列为候选、被正面描述都算推荐**。因此 candidate(vNext candidate_only,
# 即出现在候选/名单里)并入推荐集合。
# mentioned_only(仅被顺带提到、无推荐语义)不并入，否则推荐率就等于提及率、
# 两个指标失去区分度。
_RECOMMENDATION_OUTCOMES = {"recommended", "conditionally_recommended", "candidate"}

# 提及集合 = 推荐集合 ∪ 仅提到。提及率 = 该集合 / 有效样本;
# 推荐率 = 推荐集合 / 有效样本。两者是包含关系,UI 必须写清。
_MENTION_OUTCOMES = _RECOMMENDATION_OUTCOMES | {"mentioned"}

# 品牌定向题(客户直接问"XX 是做什么的")必然命中,不进竞争格局与提及率分母(P1-6)。
_BRAND_DIRECTED_LAYER_KEYS = {"brand_awareness", "brand"}
_BRAND_DIRECTED_LAYER_LABELS = {"品牌认知层"}
_ERROR_STATUSES = {
    "error", "failed", "timeout", "unavailable", "engine_error",
    "失败", "查询失败", "超时", "不可用", "引擎异常",
}
_NO_ANSWER_TEXTS = {"本引擎未返回可展示回答", "本引擎未返回可展示回答。"}
_ERROR_ANSWER_PREFIXES = ("查询失败", "请求失败", "调用失败", "error:", "error ")


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, *, limit: int = 600) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized[:limit] if normalized else None


def _markdown_text(value: Any, *, limit: int = 8000) -> str | None:
    """Keep trustworthy answer formatting without accepting raw HTML.

    The React renderer already blocks raw HTML.  Collapsing every whitespace
    character here previously destroyed Markdown lists and headings before the
    browser ever saw them.
    """

    if not isinstance(value, str):
        return None
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None
    lines = [re.sub(r"[\t ]+", " ", line).rstrip() for line in normalized.split("\n")]
    compact: list[str] = []
    blank = False
    for line in lines:
        if line:
            compact.append(line)
            blank = False
        elif not blank:
            compact.append("")
            blank = True
    return "\n".join(compact)[:limit].rstrip() or None


def _number(value: Any, *, lo: float = 0, hi: float = 100) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < lo or number > hi:
        return None
    return int(number) if number.is_integer() else round(number, 1)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = _text(value, limit=64)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            return None


def _ready(data: Any) -> dict[str, Any]:
    return {"status": "ready", "data": data}


def _empty(message: str) -> dict[str, Any]:
    return {"status": "empty", "message": message}


def _unavailable(message: str) -> dict[str, Any]:
    return {"status": "unavailable", "message": message}


def _public_platform(value: Any) -> str:
    raw = _text(value, limit=120) or ""
    key = raw.lower()
    if any(token in key for token in ("yuanbao", "hunyuan", "hy3", "元宝", "混元")):
        return "元宝"
    if any(token in key for token in ("qwen", "dashscope", "通义", "千问")):
        return "通义千问"
    if any(token in key for token in ("doubao", "volcengine", "豆包")):
        return "豆包"
    if any(token in key for token in ("deepseek", "metaso", "秘塔")):
        return "DeepSeek"
    if any(token in key for token in ("kimi", "moonshot")):
        return "Kimi"
    return "AI 搜索引擎"


def _ordered_public_platform_labels() -> list[str]:
    """引擎清单单源 → 客户可见平台名的稳定展示顺序(P0-2)。"""
    from config.ai_engines import UNIFIED_ENGINES, engine_label

    return [engine_label(engine) for engine in UNIFIED_ENGINES]


def _platform_has_no_search(public_label: str) -> bool:
    """该客户可见平台是否为无联网检索表面(引用数不适用 · P0-3 ④)。"""
    from config.ai_engines import NO_SEARCH_ENGINES, engine_label

    return any(engine_label(engine) == public_label for engine in NO_SEARCH_ENGINES)


def _answer_text(result: dict[str, Any]) -> str | None:
    for field in ("full_response", "raw_full_response", "answer", "answer_summary", "content"):
        text = _markdown_text(result.get(field))
        if text:
            return text
    return None


def _answer_is_error(answer: str | None) -> bool:
    if not answer:
        return False
    lowered = answer.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _ERROR_ANSWER_PREFIXES)


def _is_valid_answer(result: dict[str, Any], answer: str | None = None) -> bool:
    status = str(result.get("status") or "").strip().lower()
    resolved = answer if answer is not None else _answer_text(result)
    return bool(
        resolved
        and resolved.strip() not in _NO_ANSWER_TEXTS
        and status not in _ERROR_STATUSES
        and not _answer_is_error(resolved)
    )


def _is_identity_pending(result: dict[str, Any]) -> bool:
    """这一格是不是「身份待确认」(而不是引擎失败)。

    [WO_UNKNOWN_DENOMINATOR_DISCLOSURE 2026-08-07]

    🔴 判据委托 ``diagnosis_identity_review.is_identity_pending_cell`` —— 与
    出现率分母**同一个 SSOT**。不在这里另写一套"看 status/verdict 猜一猜":
    说出去的 N 和分母里扣掉的 N 必须是同一个数,对不上比不说更坏。

    取不到判据时返回 False(fail-closed):该格照旧算失败 —— 与改动前逐字一致,
    宁可少说一句,不虚报一个数。

    🔴 [返工 2026-08-07 §4②] **优先读装配层算好的布尔**。
    本函数拿到的 `result` 是**报告模块层**的格子,它经过
    `report_writer_v2._public_result_metadata` 的严格白名单 ——
    `detection_reason` / `brand_verdict` 在那里就被丢掉了,直接调
    `is_identity_pending_cell` 恒返回 False(实测:561 的 16 格在这一层数出来是 0)。
    装配层现在会算好 `identity_pending` 布尔带下来;这里读它。
    读不到就走原判据 —— **存量的 196 份报告没有这个布尔,必须还能跑**
    (它们仍旧数不出待确认,那是"不追溯"的既定口径,不是新缺陷)。
    """
    stored = result.get("identity_pending")
    if isinstance(stored, bool):
        return stored
    try:
        from services.diagnosis_identity_review import is_identity_pending_cell

        return bool(is_identity_pending_cell(result))
    except Exception:  # pragma: no cover - 依赖缺失不得改变既有归类
        return False


def _has_citation_observation(result: dict[str, Any]) -> bool:
    for field in ("search_citations", "citations"):
        if field in result and isinstance(result.get(field), (list, dict)):
            return True
    return False


def _is_brand_directed_test(test: dict[str, Any], brand_name: str | None = None) -> bool:
    """该题是否为品牌定向题(客户直接问品牌名)。

    [P1-6 · 2026-07-26] 品牌定向题必然命中,进分母会把提及率/竞争格局虚高。
    优先读机器 key ``layer_key``(新采集写入),兼容旧产物只有中文 ``layer`` 标签。

    [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2] 只读标签**守不住**:报告 551 的第二道
    公司题带着 super_tier1 标签落库,这里就把它当竞争面样本收进了提及率分母
    (通义/豆包/元宝 14.3% 全部来自那一道)。加与标签独立的文本判据,
    标签与文本打架时以文本为准。``brand_name`` 缺省时行为与改前逐字一致。
    """
    layer_key = _text(test.get("layer_key"), limit=60)
    layer_label = _text(test.get("layer"), limit=60)
    if layer_key:
        if layer_key.strip().lower() in _BRAND_DIRECTED_LAYER_KEYS:
            return True
    elif layer_label and layer_label.strip() in _BRAND_DIRECTED_LAYER_LABELS:
        return True

    if not brand_name:
        return False
    # verbatim/自定义题没有分层(layer_key 为 None 且 layer 显"自定义")→ 沿用既有豁免。
    if layer_key is None and layer_label and layer_label.strip() == "自定义":
        return False
    question = _text(test.get("question") or test.get("keyword"), limit=240) or ""
    from services.brand_directed_question import is_brand_directed_text

    return is_brand_directed_text(question, brand_name)


def _iter_results(
    raw_module: dict[str, Any],
    brand_name: str | None = None,
) -> Iterable[tuple[str, str, dict[str, Any], bool]]:
    """Yield (question, public_platform, result, is_brand_directed)."""
    for group in ("tests", "custom_tests"):
        for test in _items(raw_module.get(group)):
            if not isinstance(test, dict):
                continue
            question = _text(test.get("question") or test.get("keyword"), limit=240) or "未记录问题"
            brand_directed = _is_brand_directed_test(test, brand_name)
            results = test.get("results")
            if isinstance(results, dict):
                iterator = results.items()
            elif isinstance(results, list):
                iterator = enumerate(results)
            else:
                continue
            for engine, raw_result in iterator:
                if not isinstance(raw_result, dict):
                    continue
                platform_source = (
                    raw_result.get("platform_key")
                    or raw_result.get("engine")
                    or raw_result.get("engine_label")
                    or engine
                )
                yield question, _public_platform(platform_source), raw_result, brand_directed


def _explicit_outcome(result: dict[str, Any]) -> str | None:
    for field in ("target_outcome", "outcome", "verdict", "recommendation_outcome"):
        value = result.get(field)
        if isinstance(value, str):
            mapped = _OUTCOME_MAP.get(value.strip().lower())
            if mapped:
                return mapped
    return None


_RECOMMENDATION_INTENT_HINTS = (
    "推荐", "首选", "优选", "哪家", "哪个好", "哪些", "靠谱", "成熟",
    "值得", "比较", "对比", "排名", "排行", "供应商", "选择",
)

_PUBLIC_RECOMMENDATION_SIGNALS = (
    "推荐", "首选", "优选", "值得", "靠谱", "领先", "优质", "专业", "知名",
    "技术成熟", "方案成熟", "服务有保障", "质量有保障", "优势明显", "实力较强",
    "实力强", "表现突出", "优先考虑", "重点考虑",
)
_PUBLIC_NEGATED_RECOMMENDATION_SIGNALS = (
    "不推荐", "不能推荐", "不建议", "不值得", "不靠谱", "不成熟", "不是首选",
    "并非首选", "没有优势", "无明显优势", "没有保障", "无保障", "缺乏保障",
)
_CONTRAST_MARKERS = ("相比之下", "反之", "但是", "不过", "然而", "而", "但")


def _target_clause(segment: str, name: str) -> list[str]:
    """Extract contrast-bounded clauses around each target occurrence."""
    clauses: list[str] = []
    start = 0
    while True:
        index = segment.find(name, start)
        if index < 0:
            break
        left = 0
        right = len(segment)
        for marker in _CONTRAST_MARKERS:
            marker_index = segment.rfind(marker, 0, index)
            if marker_index >= left:
                left = marker_index + len(marker)
            marker_index = segment.find(marker, index + len(name))
            if marker_index >= 0:
                right = min(right, marker_index)
        clause = segment[left:right].strip(" ，,:：")
        if clause and clause not in clauses:
            clauses.append(clause)
        start = index + len(name)
    return clauses


def _target_recommendation_context(
    answer: str,
    *,
    brand_name: str | None,
    matched_text: Any,
    legacy_candidate: bool,
) -> str:
    """Return only text near the confirmed target, avoiding competitor spillover."""
    names = []
    for value in (matched_text, brand_name):
        name = _text(value, limit=120)
        if name and name not in names:
            names.append(name)
    windows: list[str] = []
    segments = [segment.strip() for segment in re.split(r"(?<=[。！？!?；;])|\n+", answer) if segment.strip()]
    for name in names:
        named_segments = [segment for segment in segments if name in segment]
        if named_segments:
            for segment in named_segments:
                windows.extend(_target_clause(segment, name))
            continue
        start = 0
        while True:
            index = answer.find(name, start)
            if index < 0:
                break
            windows.append(answer[max(0, index - 90):min(len(answer), index + len(name) + 140)])
            start = index + len(name)
    if windows:
        return "\n".join(windows)
    # Some historical rows retained is_recommended but not matched_text.  In
    # that explicitly observed case the answer remains the best available
    # compatibility evidence; generic brand_detected rows stay conservative.
    return answer if legacy_candidate else ""


def _has_public_recommendation_signal(value: str) -> bool:
    scrubbed = value
    for negative in _PUBLIC_NEGATED_RECOMMENDATION_SIGNALS:
        scrubbed = scrubbed.replace(negative, "")
    return any(signal in scrubbed for signal in _PUBLIC_RECOMMENDATION_SIGNALS)


def _legacy_recommendation_outcome(
    result: dict[str, Any],
    *,
    question: str,
    answer: str,
    brand_name: str | None,
) -> str:
    """Bridge old diagnosis rows into the observation outcome SSOT.

    Older reports preserved ``brand_detected`` and sometimes
    ``is_recommended`` but not ``target_outcome``.  We reuse the observation
    classifier's deterministic recommendation signals.  A generic positive
    adjective is only allowed to promote a row when the question itself asks
    for a choice/recommendation, or the old collector explicitly marked the
    target as a recommendation candidate.  This lowers the old all-or-nothing
    gate without turning ordinary company descriptions into recommendations.
    """

    from services.geo_observation.contracts import EntityState, ResponseStatus
    from services.geo_observation import privacy
    from services.geo_observation.entity_review import classify_outcome

    legacy_candidate = result.get("is_recommended") is True
    has_recommendation_intent = any(hint in question for hint in _RECOMMENDATION_INTENT_HINTS)
    target_context = _target_recommendation_context(
        answer,
        brand_name=brand_name,
        matched_text=result.get("matched_text"),
        legacy_candidate=legacy_candidate,
    )
    outcome = classify_outcome(
        ResponseStatus.answered,
        EntityState.confirmed_mention,
        True,
        target_context,
    ).value
    if (
        target_context
        and _has_public_recommendation_signal(target_context)
        and (legacy_candidate or has_recommendation_intent)
    ):
        return (
            "conditionally_recommended"
            if privacy.has_conditional_language(target_context)
            else "recommended"
        )
    if outcome in {"recommended", "conditionally_recommended"}:
        if legacy_candidate or has_recommendation_intent:
            return _OUTCOME_MAP[outcome]
        return "mentioned"
    if outcome == "candidate_only" or legacy_candidate:
        return "candidate"
    return "mentioned"


def _verdict(
    result: dict[str, Any],
    *,
    question: str,
    brand_name: str | None,
) -> tuple[str, bool]:
    status = str(result.get("status") or "").strip().lower()
    if status in _ERROR_STATUSES:
        return "engine_error", False
    answer = _answer_text(result)
    if _answer_is_error(answer):
        return "engine_error", False
    if not answer or answer.strip() in _NO_ANSWER_TEXTS:
        return "no_answer", False
    explicit = _explicit_outcome(result)
    if explicit:
        return explicit, True
    detected = result.get("brand_detected")
    if detected is True:
        return _legacy_recommendation_outcome(
            result,
            question=question,
            answer=answer,
            brand_name=brand_name,
        ), True
    if detected is False:
        return "not_mentioned", True
    # Historical rows without an explicit boolean cannot support a rate.
    return "mentioned", False


def _citation_domains(result: dict[str, Any]) -> list[str]:
    raw = result.get("search_citations") or result.get("citations") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    domains: list[str] = []
    for citation in _items(raw):
        value = citation
        if isinstance(citation, dict):
            value = citation.get("url") or citation.get("link") or citation.get("source_url")
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value.strip())
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                continue
            host = parsed.hostname.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            continue
        if host not in domains:
            domains.append(host)
    return domains[:12]


def _trust(funnel: dict[str, Any]) -> dict[str, Any]:
    meta = _obj(funnel.get("level_meta"))
    layers = [layer for layer in _items(funnel.get("layers")) if isinstance(layer, dict)]
    confidences = [layer.get("confidence") for layer in layers if _count(layer.get("total"))]
    confidence = None
    if confidences:
        confidence = "low" if "low" in confidences else ("medium" if "medium" in confidences else "high")
    partial = meta.get("partial_sample")
    if not isinstance(partial, bool):
        partial = any((_count(layer.get("total")) or 0) == 0 for layer in layers)
    capped = meta.get("level_capped") if isinstance(meta.get("level_capped"), bool) else False
    return {"partialSample": partial, "levelCapped": capped, "confidence": confidence}


def _funnel_section(root: dict[str, Any], modules: dict[str, Any], score: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    funnel = _obj(root.get("funnel")) or _obj(_obj(modules.get("2")).get("funnel"))
    layers_out: list[dict[str, Any]] = []
    for raw in _items(funnel.get("layers")):
        if not isinstance(raw, dict):
            continue
        total = _count(raw.get("total"))
        has_samples = total is not None and total > 0
        detected = _count(raw.get("detected")) if has_samples else None
        if detected is not None and total is not None and detected > total:
            detected = None
        layers_out.append({
            "key": _text(raw.get("key"), limit=50) or "unknown",
            "label": _text(raw.get("label"), limit=80) or "未命名层级",
            "description": _text(raw.get("desc") or raw.get("description"), limit=240),
            "businessMeaning": _text(raw.get("business") or raw.get("business_meaning"), limit=240),
            "detected": detected,
            "total": total if has_samples else None,
            "ratePct": _number(raw.get("rate_pct")) if has_samples else None,
            "score": _number(raw.get("score")) if has_samples else None,
            "weight": _number(raw.get("weight")),
            "dataSufficient": bool(raw.get("data_sufficient")) if has_samples else False,
            "confidence": raw.get("confidence") if raw.get("confidence") in {"high", "medium", "low"} else None,
            # [P0-5] 合并表述：同一层禁止并排显示"满分"和"数据不足"
            "sampleNote": _text(raw.get("sample_note"), limit=160),
            "provisional": bool(raw.get("provisional")) if has_samples else False,
            "headlineLabel": _text(raw.get("headline_label"), limit=40),
        })
    trust = _trust(funnel)
    if not funnel or not layers_out:
        return _unavailable("本次报告没有可展示的漏斗明细。"), trust
    return _ready({"layers": layers_out, "totalScore": _number(score), "trust": trust}), trust


def _evidence_and_platforms(
    modules: dict[str, Any],
    generated_at: Any,
    *,
    brand_name: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    raw_module = _obj(modules.get("3_raw"))
    rows = list(_iter_results(raw_module, brand_name))
    evidence_items: list[dict[str, Any]] = []
    platform_stats: dict[str, dict[str, Any]] = {}
    for question, platform, result, brand_directed in rows:
        verdict, outcome_observed = _verdict(
            result,
            question=question,
            brand_name=brand_name,
        )
        answer = _answer_text(result)
        domains = _citation_domains(result)
        stats = platform_stats.setdefault(platform, {
            "valid": 0, "detected": 0, "detection_rows": 0,
            # [P1-6] 竞争口径分母:排除品牌定向题后的有效样本
            "competitive_valid": 0, "competitive_detection_rows": 0,
            "competitive_detected": 0,
            "outcome_rows": 0, "recommended": 0, "mentioned": 0,
            "citations": 0, "citation_rows": 0, "errors": 0,
            "brand_directed_valid": 0,
            # [WO 2026-08-07] 身份待确认格(被排除出出现率分母的那一类)
            "identity_pending": 0,
        })
        # [WO 2026-08-07 · Owner (A)(a)] 「待确认」计数**独立于** valid/invalid 分支。
        #
        # 🔴 生产实证(179/183 份报告、5976 个格):报告模块层的 `status` 是**中文展示
        # 标签**(`未提到品牌`/`提到品牌`),不是 `error` —— 所以待确认格在 C 端
        # `_is_valid_answer` 判 True、**进分母、算作未提到**,与 `ai_mention_rate`
        # (排除)方向相反。我上一版把计数放进 `else:`(失败)分支,那条分支在这一层
        # 从来不执行,于是 `identityPendingSamples` 恒 null —— 明示恒不出现。
        #
        # Owner 拍板 (A)(a):**口径一个字不动**(它们仍留在分母里算未提到),
        # 只把数量如实说出来。所以计数必须独立于分支 —— 放进任何一支都会绑上
        # 那一支的前提。
        if _is_identity_pending(result):
            stats["identity_pending"] += 1

        valid = _is_valid_answer(result, answer)
        if valid:
            stats["valid"] += 1
            has_detection = isinstance(result.get("brand_detected"), bool)
            if has_detection:
                stats["detection_rows"] += 1
                if result.get("brand_detected") is True:
                    stats["detected"] += 1
            if brand_directed:
                stats["brand_directed_valid"] += 1
            else:
                # 只有非定向题进提及率/推荐率分母(P1-6)
                stats["competitive_valid"] += 1
                if has_detection:
                    stats["competitive_detection_rows"] += 1
                    if result.get("brand_detected") is True:
                        stats["competitive_detected"] += 1
                if outcome_observed:
                    stats["outcome_rows"] += 1
                    if verdict in _RECOMMENDATION_OUTCOMES:
                        stats["recommended"] += 1
                    if verdict in _MENTION_OUTCOMES:
                        stats["mentioned"] += 1
            if _has_citation_observation(result):
                stats["citation_rows"] += 1
                stats["citations"] += len(domains)
        else:
            # [WO_UNKNOWN_DENOMINATOR_DISCLOSURE 2026-08-07] 「身份待确认」不是「失败」。
            #
            # 🔴 生产实证:待确认格落库 ``status='error'``(561/553/563 三单共 18 格实查),
            # 于是 ``_is_valid_answer`` 判它无效 → 落进 errors → 客户看到的是
            # **「部分失败」**。可什么都没失败:引擎答了、答案完整,只是这家品牌的
            # 名字需要人确认一下。把"待人确认"说成"失败",既冤枉了系统,也让客户
            # 以为数据坏了 —— 而真正该说的(这几格没进出现率分母)一个字没说。
            #
            # 🔴 只改**归类与说法**,不改分母:待确认格仍然不进 ``valid`` /
            # ``competitive_valid``(Owner 边界①:分母语义一字不动)。
            # 上面已独立计过 identity_pending;这里只分「真失败」。
            # 🔴 待确认格在**落库形态**下 status='error' 会走到这里(监测侧等其它
            # 调用方仍是那个形状),那时它不该被算成失败 —— 判据保留。
            if not _is_identity_pending(result):
                stats["errors"] += 1
        tested_at = _iso(result.get("tested_at") or result.get("created_at") or generated_at)
        row_seed = f"{question}|{platform}|{tested_at or ''}|{answer or ''}"
        evidence_items.append({
            "rowKey": hashlib.sha256(row_seed.encode("utf-8")).hexdigest()[:20],
            "question": question,
            "platformName": platform,
            "verdict": verdict,
            # Failed provider payloads may contain paths, request metadata or
            # upstream error text. Public reports expose only the fixed verdict.
            "answerExcerpt": answer if valid else None,
            "citedDomains": domains if valid else [],
            "evidenceLevel": result.get("evidence_level") if result.get("evidence_level") in {"A", "B", "C"} else None,
            "testedAt": tested_at,
        })

    platform_rows: list[dict[str, Any]] = []
    for platform, stats in platform_stats.items():
        valid = stats["valid"]
        competitive_valid = stats["competitive_valid"]

        def _pct(count: int, denominator: int) -> float | None:
            return round(count / denominator * 100, 1) if denominator else None

        # 品牌识别率仍看全部题（含定向题）：它回答的是"AI 认不认识这个品牌"。
        detection_rate = (
            _pct(stats["detected"], valid) if valid and stats["detection_rows"] == valid else None
        )
        # [P1-6] 提及率/推荐率排除品牌定向题：定向题必然命中，会把竞争口径虚高。
        mention_rate = (
            _pct(stats["mentioned"], competitive_valid)
            if competitive_valid and stats["outcome_rows"] == competitive_valid
            else (
                _pct(stats["competitive_detected"], competitive_valid)
                if competitive_valid and stats["competitive_detection_rows"] == competitive_valid
                else None
            )
        )
        recommend_rate = (
            _pct(stats["recommended"], competitive_valid)
            if competitive_valid and stats["outcome_rows"] == competitive_valid
            else None
        )
        if not valid:
            data_status = "样本不足"
        elif stats["errors"]:
            data_status = "部分失败"
        elif stats["identity_pending"]:
            # [WO 2026-08-07 · Owner 边界③] 人话,禁内部术语。
            # 这一档在旧版落在「部分失败」里 —— 没有失败,是有几格等人确认。
            data_status = "部分待确认"
        elif valid < 5:
            data_status = "样本较少"
        else:
            data_status = "结果稳定"

        # [P0-3 ④ · 2026-07-26] 引用数「不适用」与「真实为 0」必须分开。
        #   元宝(yuanbao_hy3_tokenhub)是**无联网检索表面**(lineage.py
        #   default_search_enabled=False / search_provider=None；腾讯 WSA 检索是
        #   另一个表面且当前 availability="unavailable")，它本就不产生引用。
        #   旧版把它渲染成 0，和其他平台的 62/61/90 并排，读起来像"这个品牌在元宝
        #   一条信源都没有"——那是把系统的能力边界说成客户的问题。改为 null → UI 显
        #   "—"，并给出不适用原因。
        citation_not_applicable = _platform_has_no_search(platform)
        if citation_not_applicable:
            citation_count = None
            citation_status = "该平台为模型原生回答（不联网检索），引用数不适用"
        elif valid and stats["citation_rows"] == valid:
            citation_count = stats["citations"]
            citation_status = None
        else:
            citation_count = None
            citation_status = "本次未完整采集到引用来源"

        platform_rows.append({
            "platformName": platform,
            "validSamples": valid,
            "detectionRatePct": detection_rate,
            "mentionRatePct": mention_rate,
            "recommendRatePct": recommend_rate,
            "citationCount": citation_count,
            "citationStatus": citation_status,
            # 竞争口径分母（已剔除品牌定向题），让前端能诚实说明"按几条样本算"
            "competitiveSamples": competitive_valid or None,
            "brandDirectedSamples": stats["brand_directed_valid"] or None,
            # [WO 2026-08-07] 身份待确认格数 —— **不计入上面任何一个分母**。
            # 🔴 0 时给 None 而不是 0:前端据此整块不渲染(Owner 边界④ /
            # 「提示要么帮人解决,要么不显示」)。给 0 会让 `?? ` 之类的写法
            # 把它当"有值"渲染成「0 格待确认」—— 563 那种报告里多一句废话。
            "identityPendingSamples": stats["identity_pending"] or None,
            "dataStatus": data_status,
            "updatedAt": _iso(generated_at),
        })
    # [P0-2] 展示顺序取自引擎清单单源，避免第 N 处硬编码平台列表。
    canonical_order = {
        label: index for index, label in enumerate(_ordered_public_platform_labels())
    }
    # 历史/未知平台仅在真实数据存在时追加展示。
    platform_rows.sort(key=lambda item: (
        canonical_order.get(item["platformName"], len(canonical_order)),
        item["platformName"],
    ))

    if rows:
        evidence_section = _ready({"items": evidence_items[:100], "totalCount": len(evidence_items)})
        platforms_section = _ready(platform_rows)
    elif raw_module:
        evidence_section = _empty("本次没有形成可展示的逐题证据。")
        platforms_section = _empty("本次没有形成可展示的平台样本。")
    else:
        evidence_section = _unavailable("逐题实测数据通道尚未形成。")
        platforms_section = _unavailable("平台表现数据通道尚未形成。")
    return evidence_section, platforms_section, {
        "platformCount": len(platform_rows) if rows else None,
        "validAnswerCount": sum(item["validSamples"] or 0 for item in platform_rows) if rows else None,
        "questionCount": len({question for question, _, _, _ in rows}) if rows else None,
    }


_LAYER_KEY_TO_FUNNEL = {
    "brand_awareness": "brand",
    "regional_industry": "local",
    "super_tier1": "scenario",
}
_LAYER_LABEL_TO_FUNNEL = {
    "品牌认知层": "brand",
    "决策获客层": "local",
    "场景转化层": "scenario",
}


def _layer_context_from_client_raw(raw_module: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从客户产物 ``3_raw.tests`` 抽 P2-12 的量化上下文。

    客户报告拿不到内部 ``ai_visibility_data``（那是内部模块），只能读已脱敏的
    客户产物。这里复用 ``tests[].layer_key``（新采集写入）/ ``layer``（老产物中文标签），
    统计每层的 0 命中问题与被引用域名数。取不到就返回 {} —— 缺数据不编数字。
    """
    missed: dict[str, list[str]] = {"brand": [], "local": [], "scenario": []}
    domains: dict[str, set[str]] = {"brand": set(), "local": set(), "scenario": set()}
    for test in _items(raw_module.get("tests")):
        if not isinstance(test, dict):
            continue
        layer_key = _text(test.get("layer_key"), limit=60)
        layer = _LAYER_KEY_TO_FUNNEL.get((layer_key or "").strip().lower())
        if not layer:
            layer = _LAYER_LABEL_TO_FUNNEL.get((_text(test.get("layer"), limit=60) or "").strip())
        if not layer:
            continue
        question = _text(test.get("question"), limit=240)
        any_detected = False
        for result in _items(test.get("results")):
            if not isinstance(result, dict):
                continue
            if result.get("brand_detected"):
                any_detected = True
            for host in _citation_domains(result):
                domains[layer].add(host)
        if question and not any_detected:
            missed[layer].append(question)

    context: dict[str, dict[str, Any]] = {}
    for layer in ("brand", "local", "scenario"):
        entry: dict[str, Any] = {}
        if missed[layer]:
            entry["missed_questions"] = missed[layer][:5]
        if domains[layer]:
            entry["peer_citation_count"] = len(domains[layer])
        if entry:
            context[layer] = entry
    return context


def _findings_section(modules: dict[str, Any], valid_answers: int | None) -> dict[str, Any]:
    interpretation = _obj(modules.get("1_interpretation"))
    evidence = _obj(modules.get("3"))
    findings: list[dict[str, Any]] = []
    headline = _text(interpretation.get("headline"), limit=320)
    detail = _text(interpretation.get("business_translation"), limit=800)
    caveat = _text(interpretation.get("data_caveat"), limit=420)
    if headline:
        findings.append({
            "category": "opportunity",
            "text": headline,
            "evidenceLevel": "B" if (valid_answers or 0) > 0 else "C",
            "sampleCount": valid_answers,
            "scope": "本次 AI 实测",
            "detail": detail,
            "insufficientNote": caveat,
        })
    shortfall = _count(evidence.get("evidence_shortfall"))
    if shortfall:
        findings.append({
            "category": "source_gap",
            "text": "当前可核对的公开证据仍不足，部分判断需要复测确认。",
            "evidenceLevel": "C",
            "sampleCount": _count(evidence.get("evidence_total")),
            "scope": "公开信源",
            "detail": None,
            "insufficientNote": f"距当前报告的证据目标还差 {shortfall} 条。",
        })
    if findings:
        return _ready(findings)
    return _empty("本次没有形成可展示的关键发现。") if interpretation or evidence else _unavailable("关键发现数据通道尚未形成。")


def _competitive_section(
    modules: dict[str, Any], brand_name: str | None = None
) -> dict[str, Any]:
    competition = _obj(modules.get("3_competition"))
    if not competition:
        return _unavailable("竞争品牌数据通道尚未形成。")
    # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.3] 展示层的**无条件**双保险:
    #   top_brands 是落库快照,存量报告里客户自己已经在榜上(551 实证 4 次)。
    #   在这里再剔一次,存量报告刷新即消失,零数据迁移。
    #   写入侧(report_writer_v2)同样剔 —— 两道都要有:上游修好不代表旧快照会自己变。
    from services.brand_directed_question import is_client_own_brand

    competitors: list[dict[str, Any]] = []
    for item in _items(competition.get("top_brands"))[:12]:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), limit=100)
        count = _count(item.get("count"))
        if name and brand_name and is_client_own_brand(name, brand_name):
            continue
        if name and count is not None:
            competitors.append({"name": name, "mentionCount": count, "recommendCount": None})
    valid_total = _count(competition.get("valid_total"))
    # [P1-6] 分母口径:已剔除品牌定向题(客户直接问品牌名必然命中,进分母会虚高)
    excludes_directed = (
        competition.get("denominator_scope") == "excludes_brand_directed_questions"
    )
    brand_directed = _count(competition.get("brand_directed_valid")) or 0

    # 🔴 [WO_236-c1b'' · 复审实跑抓到的**第三态**] 这一支必须在
    #    `not competitors` 之前,**不论 `competitors` 是否非空**。
    #
    #    我上一版把判断挂在 `not competitors and not valid_total` 上,漏了:
    #    `report_writer_v2.py:1827` 在 `top_brands` 为空时会**回落到蒸馏飞轮**
    #    (`keyword_insights.brands_found`)。而本单恰恰让防御型的 `top_brands`
    #    从答案侧变空 ⇒ **回落必然触发** ⇒ `competitors` 非空、`valid_total == 0`
    #    ⇒ 两个空态分支都不进 ⇒ 页面 `status='ready'`、渲染 2 个竞品、
    #    样本口径写「已排除 12 条品牌定向问答」而一个样本都没有。
    #    复审实跑读出来的,不是推断;生产可达(品牌 592,`is_test=false`,真客户,
    #    41 行飞轮料 + 防御型题单)。
    #
    #    为什么不论名字从哪来都要拦:分母为零的排行本就没意义;
    #    **名字来自飞轮时更糟** —— 那是另一个时间窗、另一批问题采到的,
    #    拼进一份防御型报告等于拿历史数据冒充本次实测。
    # 🔴 用 `not valid_total` 而**不是** `valid_total == 0`:`_count()` 可以返回 `None`
    #    (键缺失 / 显式 null),而 `None == 0` 为假 ⇒ 那时会掉进下面的 ready 分支、
    #    拿零分母渲染排行。复审实跑证实这两态在生产上**够不着**
    #    (「有 brand_directed_valid 而无 valid_total」= 0 条,writer 两键同 dict 一起写),
    #    所以不是现役缺陷;这里是把「将来 writer 改了取值域」那条路一次封死。
    #    ⚠️ 别把它"优化"回 `== 0`。
    if not valid_total and brand_directed > 0:
        return _empty(
            "本次为防御型诊断,只问了贵司自己的问题,不产生同行对比。"
            "想看竞品排行,请改用「防御+增长」合并版诊断。"
        )

    if not competitors and not valid_total:
        # 🔴 [WO_237 · Owner 2026-09-17 拍板] 空态要分两种,不能压成同一句。
        #
        #    ① 一条竞争样本都没有,**但确实剔掉过品牌定向题** ⇒ 这是防御型诊断:
        #       只问了客户自己的问题,本来就不产生同行对比。要告诉他**为什么没有**
        #       以及**怎么才能有**,而不是让他以为系统坏了。
        #    ② 一条竞争样本都没有,**也没剔过任何题** ⇒ 是"我们没采到同行",
        #       沿用旧文案。
        #
        #    🔴 判据按**本次实际发生的事**算(`brand_directed > 0`),
        #       **不按 `mode == 'defensive'` 硬判** —— 那又是一个硬编码常量,
        #       和 236-c1a 修的那句「口径已排除」是同一个病。
        #       按实算还白得一样东西:hybrid 题单被客户手删光增长题时**自动同样处理**,
        #       不用再加一个分支(A 实测过这条路径真实存在)。
        #    (剔过题那一支已在上面提前返回 —— 它不能依赖 `not competitors`,理由见那段。)
        return _empty("本次没有形成可比较的竞争品牌样本。")
    reliable = valid_total is not None and valid_total > 0
    scope_parts: list[str] = []
    if reliable:
        scope_parts.append(f"{valid_total} 条有效 AI 回答")
    if excludes_directed and brand_directed:
        scope_parts.append(f"已排除 {brand_directed} 条品牌定向问答")

    # [P1-7] 名单为空的原因必须可读:是"AI 真没点名同行"还是"我们没采到"
    if competitors:
        empty_reason = None
    elif competition.get("competitor_extraction_failed"):
        empty_reason = "同行名单本次未采集成功（名单汇总环节失败），不代表该行业没有同行。"
    else:
        empty_reason = "本次 AI 回答里没有点名具体同行，多为泛化描述。"

    return _ready({
        "sampleScope": " · ".join(scope_parts) if scope_parts else None,
        # 🔴 [WO_236-c1a] 结构化交出「本次实际排除了几条品牌定向问答」。
        #    页面那句「口径已排除『直接问本品牌名』这类必然命中的问题」原来是
        #    **无条件静态文案**,与这次到底排没排无关 —— #700/#726 一条没排,
        #    它照样在真客户报告上宣称排了。
        #    前端必须按这个数渲染:**0 就不显示那句**,>0 显示时数字用这个值。
        #    (口语那串 `sampleScope` 不够用:它把"没排"和"排了0条"压成同一种沉默。)
        #
        #    语义:被排除出竞争分母的**有效回答条数**(不是题数)。
        #    🔴 **恒为 int,永不为 None。** 我第一版注释里写了「None = 没有这个口径的观测,
        #       也按不显示处理」,而给前端的契约写的是「缺省按 0」—— 同一件事两种表示,
        #       正是"两边各自绿、页面照旧骗客户"的入口:前端少写一个 null 判断就会
        #       回落到"按老样子显示"。一种表示,不留第二种。
        #
        #    `else 0` 不是冗余:`denominator_scope` 说"没排"而 `brand_directed_valid`
        #    留着旧值(>0)时,**以 scope 为准报 0** —— 绝不拿一个陈旧计数去宣称排过。
        #    这一支由判据 `test_a_stale_count_is_not_reported_as_an_exclusion` 钉住。
        "excludedBrandDirectedCount": int(brand_directed if excludes_directed else 0),
        "competitors": competitors,
        "ownMentionCount": _count(competition.get("client_detected_count")),
        "ownRecommendCount": None,
        "hasReliableDenominator": reliable,
        "denominatorNote": None if reliable else "本次没有可靠分母，仅展示出现次数。",
        "competitorSource": (
            "monitoring"
            if competition.get("competitor_source") == "flywheel_keyword_insights"
            else ("diagnosis" if competition.get("competitor_source") else None)
        ),
        "emptyReason": empty_reason,
    })


def _actions_section(modules: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    action_module = _obj(modules.get("6"))
    labels = {"P0": "优先处理", "P1": "接着处理", "P2": "持续优化"}
    actions: list[dict[str, Any]] = []
    for item in _items(action_module.get("personalized_actions")) + _items(action_module.get("todos")):
        if not isinstance(item, dict):
            continue
        title = _text(item.get("recommended_content") or item.get("action") or item.get("issue"), limit=180)
        if not title:
            continue
        priority = _text(item.get("original_priority") or item.get("priority"), limit=40) or "建议"
        actions.append({
            "priorityLabel": labels.get(priority, priority),
            "title": title,
            "why": _text(item.get("evidence_basis") or item.get("issue"), limit=360),
            "evidenceRowKeys": [],
            "impactScope": _text(item.get("weak_dimension"), limit=120),
            "suggestedPeriod": None,
            "detailMd": None,
        })
        if len(actions) >= 8:
            break
    if not actions:
        from services.report_action_recommendations import derive_funnel_todos

        score_module = _obj(modules.get("2"))
        # [P2-12] 客户报告的行动建议同样要可执行：把本次 0 命中词与
        #   AI 实际引用的信源域名数当上下文传进去（取不到就退回原抽象文案）。
        _layer_context = _layer_context_from_client_raw(_obj(modules.get("3_raw")))
        for item in derive_funnel_todos(
            score_module.get("funnel"), layer_context=_layer_context or None
        ):
            priority = _text(item.get("priority"), limit=40) or "建议"
            actions.append({
                "priorityLabel": labels.get(priority, priority),
                "title": _text(item.get("action"), limit=180),
                "why": _text(item.get("evidence_basis"), limit=360),
                "evidenceRowKeys": [],
                "impactScope": _text(item.get("weak_dimension"), limit=120),
                "suggestedPeriod": None,
                "detailMd": None,
            })
    if not actions:
        if action_module:
            return _empty("本次没有形成明确的行动建议。"), None
        return _unavailable("行动建议数据通道尚未形成。"), None
    return _ready(actions), actions[0]["title"]


def build_identity_pending_detail(
    modules_jsonb: Any,
    *,
    brand_name: Any = None,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """待确认明细(**只给已登录且归属的服务商**)。

    [WO 2026-08-07 · Owner 拍板] 门户分享页按身份出流:服务商视角能看到
    「哪几次回答、出现的相近名字是什么」并逐条确认;匿名 token 访客拿到的
    响应体里**根本不含这一段** —— 服务端裁剪,不是前端隐藏。

    🔴 本函数是**唯一**产出这段明细的地方,调用方必须先过资源级归属校验。
    它自己不做鉴权 —— 鉴权在 API 层(能拿到 request/登录态的那一层),
    这里只保证"不被调用就不存在这段数据"。
    """
    root = _obj(modules_jsonb)
    modules = get_client_report_modules(root)
    raw_module = _obj(modules.get("3_raw"))
    out: list[dict[str, Any]] = []
    for question, platform, result, _brand_directed in _iter_results(
        raw_module, _text(brand_name, limit=120)
    ):
        if not _is_identity_pending(result):
            continue
        # 相近名字:从同格 mentioned_brands 里取(装配层白名单已带),
        # 不透传任何工程串(detection_reason 之流一个字都不进)。
        candidates = [
            _text(item, limit=60)
            for item in (result.get("mentioned_brands") or [])[:5]
            if _text(item, limit=60)
        ]
        out.append({
            "question": question,
            "platformName": platform,
            # 现役确认端点按 (question, engine) 定位单元格,所以要带**原始引擎键**
            # (platformName 是给人看的展示名,定位不了)。
            "engine": _text(result.get("engine") or result.get("platform_key"), limit=40),
            # 乐观锁版本:未处理过的格是 0。端点用它防并发/重放把分母加两次。
            "decisionVersion": int(result.get("identity_decision_version") or 0),
            "similarNames": candidates,
            "answerExcerpt": _answer_text(result)[:400] or None,
        })
        if len(out) >= max(1, int(limit)):
            break
    return out


def build_public_report_presentation(
    modules_jsonb: Any,
    *,
    industry: Any,
    canonical_score: Any,
    generated_at: Any,
    keyword_count: int | None,
    brand_name: Any = None,
    viewer_can_calibrate: bool = False,
) -> dict[str, Any]:
    """Return the safe presentation contract for a ready V2 customer report.

    ``viewer_can_calibrate``:调用方**已完成资源级归属校验**(该 brand 归属该
    登录用户,或 admin)时才传 True。默认 False —— 匿名访客走的就是默认值,
    产物里连键都不会出现(裁剪靠"不构造",不靠"构造了再删")。
    """
    root = _obj(modules_jsonb)
    modules = get_client_report_modules(root)
    module_one = _obj(modules.get("1") or modules.get(1))
    interpretation = _obj(modules.get("1_interpretation"))

    funnel, trust = _funnel_section(root, modules, canonical_score)
    evidence, platforms, sampling = _evidence_and_platforms(
        modules,
        generated_at,
        brand_name=_text(brand_name, limit=120),
    )
    valid_answers = sampling["validAnswerCount"]
    findings = _findings_section(modules, valid_answers)
    competitive = _competitive_section(modules, _text(brand_name, limit=120))
    actions, next_action = _actions_section(modules)

    headline = _text(
        module_one.get("conclusion_text")
        or module_one.get("insight")
        or interpretation.get("headline"),
        limit=420,
    )
    platform_names: list[str] = []
    if platforms.get("status") == "ready":
        platform_names = [row["platformName"] for row in platforms["data"]]
    question_count = sampling["questionCount"]
    effective_question_count = question_count if question_count is not None else keyword_count

    limits = [
        "“—”表示本次没有有效测量，不等于测量结果为 0。",
        # 🔴 [#233] 这句是公开报告的说明文字,跟着显示名一起收回口径。
        "提及率(正面措辞)只统计回答里出现正面措辞的那些；仅出现品牌名称不计入。",
        "AI 回答会随模型、联网检索和采样时间变化，本报告代表本次采样窗口。",
    ]
    if trust["partialSample"]:
        limits.append("部分漏斗层没有有效样本，综合分已按评分规则处理并明确标注。")

    tested_scope = None
    if effective_question_count is not None or valid_answers is not None:
        scope_parts = []
        if effective_question_count is not None:
            scope_parts.append(f"{effective_question_count} 个问题")
        if valid_answers is not None:
            scope_parts.append(f"{valid_answers} 条有效 AI 回答")
        tested_scope = "本次实测 " + "，".join(scope_parts) + "。"

    # [WO 2026-08-07 · Owner 拍板] 服务商视角的「待确认校准」段。
    # 🔴 **裁剪靠"不构造"**:viewer_can_calibrate=False(匿名访客的默认值)时
    # 这个键根本不进 payload —— 不是构造完再删,也不是交给前端隐藏。
    calibration: dict[str, Any] | None = None
    if viewer_can_calibrate:
        items = build_identity_pending_detail(modules_jsonb, brand_name=brand_name)
        calibration = {
            # 确认走**现役服务商端那条链**,不开第二条写路径。
            "decisionEndpoint": "/api/diagnosis/{diagnosis_id}/brand-cells/decision",
            "pendingCount": len(items),
            "items": items,
        }

    payload = {
        "contractVersion": "public-report-presentation/v1",
        "identity": {"industry": _text(industry, limit=120)},
        "summary": {
            "headline": headline,
            "testedPlatformCount": sampling["platformCount"],
            # 🔴 [WO_236-c1c] 顶部那格「真实问题 N 个」以前**后端根本没发这个数** ——
            #    前端 `transport/mapDto.ts:475` 自己拿 `keywordCount`(关键词数)顶上,
            #    契约文档 `PRESENTATION_CONTRACT.md:66` 还白纸黑字为它背书
            #    (「`testedQuestionCount` 仍取现役 `keyword_count`」)。
            #    于是同一页上出现两个数:顶部「真实问题 1 个」(= 1 个关键词)
            #    与方法说明「本次实测 3 个问题」(= 真的 3 道题),而证据矩阵
            #    每平台 3 个、12 = 3×4 与后者自洽 —— 顶部那个 1 与谁都对不上。
            #    #700 真客户报告上就是这么显示的。
            #
            #    改**取值**不改标签:「真实问题」这个词在报告别处(方法说明、证据矩阵)
            #    都是按题算的,改标签会制造第二套口径。
            #    这里发的是 `sampling["questionCount"]` —— 与方法说明那句同源。
            "testedQuestionCount": question_count,
            "validAnswerCount": valid_answers,
            "trust": trust,
            "nextAction": next_action,
        },
        "funnel": funnel,
        "platforms": platforms,
        "findings": findings,
        "evidence": evidence,
        "competitive": competitive,
        "actions": actions,
        "thirtyDayPlan": _unavailable("本次诊断没有经客户确认的 30 天执行计划。"),
        "methodology": {
            "testedScope": tested_scope,
            "timeRange": f"数据更新至 {_iso(generated_at)}" if _iso(generated_at) else None,
            "platformScope": "、".join(platform_names) if platform_names else None,
            "scoreMethod": "按本次有效样本的三层漏斗加权计算；缺失层按统一评分规则处理。",
            "dataLimits": limits,
            "extraNotes": ["竞争品牌仅统计回答中实际出现的品牌名称，不把引用网页标题当作品牌。"],
        },
    }
    if calibration is not None:
        payload["calibration"] = calibration
    return payload
