"""Derive evidence-bound report actions from the canonical funnel layers.

[P2-12 · 2026-07-26] 优先行动必须可执行。

修复前的文案是「围绕真实服务地区与行业决策问题，补齐适用对象、选择标准和
可核验案例」—— 客户读完不知道要做几篇、发到哪、对着哪几个词做。
Owner 要求每条带**具体动作 + 量 + 参照数据**，例如：

    针对『深圳TikTok外贸代运营哪家靠谱』等 3 个 0 命中词，发布 4-6 篇可核验内容
    到权威站点做信源建设；同类问题下 AI 实际引用的信源约 12 条，达到相近量级后
    该层命中率有望改善。

禁绝对化收益承诺（广告法绝对化用语 + feedback_solution_not_100_guarantee）：
"有望 / 参照同行 / 通常" 可以，"必然提升 X%" 不可以。
数据不足时给"先扩测再定策略"，不硬凑一个数。
"""
from __future__ import annotations

from typing import Any


_LAYER_ACTIONS: dict[str, tuple[str, str]] = {
    "brand": (
        "品牌认知",
        "核对并补齐可公开验证的品牌全称、主营业务、服务边界和真实案例，"
        "让不同页面对品牌事实保持一致。",
    ),
    "local": (
        "决策获客",
        "围绕真实服务地区与行业决策问题，补齐适用对象、选择标准和可核验案例；"
        "不使用无证据榜单或比较结论。",
    ),
    "scenario": (
        "场景转化",
        "针对客户购买短句覆盖的高意向场景，补齐方案边界、实施过程、结果证据和常见风险。",
    ),
}


# ---------------------------------------------------------------------------
# [P2-12] 量化助手
# ---------------------------------------------------------------------------

def _suggested_volume(rate: float, missed_count: int) -> str:
    """按命中率与 0 命中词数给建议篇数区间（工作量参照，不是效果承诺）。"""
    if missed_count <= 0:
        return "2-3 篇"
    if rate < 0.1:
        return f"{max(4, missed_count)}-{max(6, missed_count * 2)} 篇"
    if rate < 0.4:
        return f"{max(3, missed_count)}-{max(4, missed_count + 2)} 篇"
    return "2-3 篇"


def _quantified_action(
    short_name: str,
    fallback_action: str,
    rate: float,
    context: Any,
) -> str:
    """把抽象动作换成「动作 + 量 + 参照数据」。取不到上下文就退回原文案。"""
    ctx = context if isinstance(context, dict) else {}
    missed = [str(q).strip() for q in (ctx.get("missed_questions") or []) if str(q).strip()]
    reference = ctx.get("peer_citation_count")
    if not missed:
        return fallback_action

    sample = missed[0]
    volume = _suggested_volume(rate, len(missed))
    head = (
        f"针对「{sample}」等 {len(missed)} 个 0 命中问题，"
        f"发布 {volume}可核验内容到能被检索到的权威站点做信源建设"
    )
    if isinstance(reference, int) and reference > 0:
        tail = (
            f"；同类问题下 AI 实际引用的信源约 {reference} 条，"
            "达到相近量级后该层命中率有望改善（参照同行，不是保证）。"
        )
    else:
        tail = (
            "；本次没有采到同类问题的同行信源量，先按上述篇数做一轮，"
            "再用同一批问题复测确认方向。"
        )
    return f"{head}{tail}（{short_name}层）"


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _action_for_layer(
    layer: dict[str, Any],
    layer_context: Any = None,
) -> dict[str, Any] | None:
    key = str(layer.get("key") or "").strip().lower()
    template = _LAYER_ACTIONS.get(key)
    if template is None:
        return None

    short_name, action = template
    context = layer_context.get(key) if isinstance(layer_context, dict) else None
    detected = max(0, int(_number(layer.get("detected"))))
    total = max(0, int(_number(layer.get("total"))))
    raw_rate = _number(layer.get("rate"), -1.0)
    rate = min(1.0, max(0.0, raw_rate if raw_rate >= 0 else (detected / total if total else 0.0)))
    rate_pct = round(rate * 100, 1)

    if total <= 0:
        return {
            "priority": "P0",
            "issue": f"{short_name}层缺少有效样本",
            "action": "先确认本次平台调用可用，再按客户购买短句对应的问题补采有效回答；样本形成前不下效果结论。",
            "evidence_basis": f"{short_name}层本次有效样本为 0。",
            "weak_dimension": short_name,
            "detected": 0,
            "total": 0,
            "rate_pct": None,
        }

    # 优先级始终按命中率算。样本不足只改**动作内容**（先扩测再定策略），
    # 不下调紧急度 —— 0 命中就是 0 命中，样本少不是把它降级的理由
    # （feedback_no_self_downgrade_no_leading_defer）。
    if rate < 0.3:
        priority = "P0"
    elif rate < 0.7:
        priority = "P1"
    else:
        priority = "P2"

    if layer.get("data_sufficient") is False:
        # [P2-12] 样本太少时不硬给一个基于 2-3 条样本的策略，先说清要扩测到多少。
        suggested = max(8, total * 2)
        return {
            "priority": priority,
            "issue": f"{short_name}层命中 {detected}/{total}（样本较少）",
            "action": (
                f"先把{short_name}层的问题扩测到 {suggested} 题以上再定策略；"
                f"当前 {total} 条样本只够看方向，不足以判断该层真实命中水平。"
            ),
            "evidence_basis": (
                f"本次 {short_name}层只有 {total} 个有效样本（命中 {detected} 个），样本量不足。"
            ),
            "weak_dimension": short_name,
            "detected": detected,
            "total": total,
            "rate_pct": rate_pct,
        }

    if priority == "P2":
        action = f"保持现有事实口径，并用同一组问题定期复测{short_name}层，发现下降时再补充对应证据。"
    else:
        action = _quantified_action(short_name, action, rate, context)

    return {
        "priority": priority,
        "issue": f"{short_name}层命中 {detected}/{total}（{rate_pct:g}%）",
        "action": action,
        "evidence_basis": f"本次 AI 实测中，{short_name}层 {total} 个有效样本命中 {detected} 个。",
        "weak_dimension": short_name,
        "detected": detected,
        "total": total,
        "rate_pct": rate_pct,
    }


def derive_funnel_todos(
    funnel: Any,
    *,
    limit: int = 3,
    layer_context: Any = None,
) -> list[dict[str, Any]]:
    """Return stable actions ordered by urgency, then by weakest observed rate.

    ``layer_context``（P2-12）按层给可量化上下文，形如::

        {"local": {"missed_questions": [...], "peer_citation_count": 12}}

    取不到就退回原有抽象文案 —— 缺数据时不编数字。
    """
    if not isinstance(funnel, dict):
        return []
    layers = funnel.get("layers")
    if not isinstance(layers, list):
        return []

    actions = [
        action
        for layer in layers
        if isinstance(layer, dict)
        if (action := _action_for_layer(layer, layer_context))
    ]
    priority_order = {"P0": 0, "P1": 1, "P2": 2}
    actions.sort(
        key=lambda item: (
            priority_order.get(str(item.get("priority")), 9),
            item.get("rate_pct") is None,
            _number(item.get("rate_pct"), 101.0),
        )
    )
    return actions[: max(0, int(limit))]


def build_layer_context(ai_visibility: Any) -> dict[str, dict[str, Any]]:
    """从本次实测结果里抽出每层的可量化上下文（P2-12）。

    - ``missed_questions``：该层 0 命中的问题（给"针对「X」等 N 个词"）；
    - ``peer_citation_count``：该层回答里 AI 实际引用的**不同域名**数
      （给"同类问题下 AI 实际引用的信源约 N 条"这个参照）。

    纯读本次 detail_table，零 LLM、零 DB、零网络。取不到就返回空。
    """
    data = ai_visibility if isinstance(ai_visibility, dict) else {}
    detail_table = data.get("detail_table")
    question_types = data.get("question_types")
    if not isinstance(detail_table, list) or not isinstance(question_types, dict):
        return {}

    layer_by_stats_key = {
        "brand_awareness": "brand",
        "regional_industry": "local",
        "super_tier1": "scenario",
    }
    missed: dict[str, list[str]] = {"brand": [], "local": [], "scenario": []}
    domains: dict[str, set] = {"brand": set(), "local": set(), "scenario": set()}

    for item in detail_table:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        layer = layer_by_stats_key.get(question_types.get(question) or "")
        if not layer:
            continue
        results = item.get("results") or {}
        if not isinstance(results, dict):
            continue
        any_detected = False
        for result in results.values():
            if not isinstance(result, dict):
                continue
            if result.get("brand_detected"):
                any_detected = True
            for field in ("search_citations", "citations"):
                raw = result.get(field)
                values = list(raw.values()) if isinstance(raw, dict) else (raw or [])
                if not isinstance(values, list):
                    continue
                for citation in values:
                    if isinstance(citation, str):
                        url = citation
                    elif isinstance(citation, dict):
                        url = citation.get("url") or citation.get("link") or ""
                    else:
                        url = ""
                    if not isinstance(url, str) or "://" not in url:
                        continue
                    host = url.split("://", 1)[1].split("/", 1)[0].strip().lower()
                    if host:
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


def derive_funnel_suggestions(
    funnel: Any,
    *,
    layer_context: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return the high/medium/ongoing shape consumed by report module 6."""
    buckets: dict[str, list[dict[str, Any]]] = {
        "high_priority": [],
        "medium_priority": [],
        "ongoing": [],
    }
    bucket_by_priority = {"P0": "high_priority", "P1": "medium_priority", "P2": "ongoing"}
    for item in derive_funnel_todos(funnel, layer_context=layer_context):
        bucket = bucket_by_priority.get(str(item.get("priority")))
        if bucket:
            buckets[bucket].append(item)
    return buckets
