"""报价意图闸 —— 不会给客户带来推荐的词不进报价(WO-QUOTE-INTENT-GATE-2026-08-04)。

## 为什么单独成一个模块

闸的逻辑原本要散在三处:定价引擎(`tools/batch_pricing.py` 的 flat / cluster 两条路径)
与报价 API(`api/selection_api.py`)。散着写的下场在这个仓里有现成例子 ——
cluster 报价路径至今连 `should_quote` 那条闸都没接,就是因为"同一件事写两遍",
写第二遍的人不知道第一遍在哪。收在一个模块里,要审只审这一个文件。

顺带解决可测性:`api/selection_api.py` 模块级就连 DB,没有测试库时根本 import 不了;
本模块只依赖 `services.commercial_query_policy` 与 fastapi,脱库可跑真行为测试,
不用退化成"断言打在源码字符串上"的假测试。

## 判据来源

唯一商业意图引擎 `services.commercial_query_policy`,**不另造第二套**
(`tests/test_commercial_policy_single_engine_wiring.py` 守着这条铁律;
`tools/keyword_expander._check_geo_feasibility` 也已委托给同一引擎)。

2026-08-04 对涉事会话 194 的 11 个词实测 11/11 判对:
  6 个知识题("…就业率真实吗"/"…案例分享"/"…有企业合作吗"…)全部 excluded;
  「哪家靠谱」「培训班推荐」「机构推荐」「和自学哪个好」全部放行。

## 这道闸解决的是什么(读码 + 生产双证 · 别再往老闸上加补丁)

老的 `should_quote` 闸**从未通电**:
  ① `batch_pricing._filter_llm_should_quote_false` 首行 `if not llm_active: return keywords`,
     而 llm_active=llm_on;生产 `LLM_FIRST_PRICING_ENABLED=false` + 白名单 `[]` → llm_on 恒 False;
  ② 即便 llm_on 为真,它读的 `keyword_price_cache_llm` 生产**全表 0 行**。
本闸纯文本裁决,无 flag、无外部表依赖,恒生效。
"""

from __future__ import annotations

# intent_type → 给代理/客户看的人话(禁术语:不出现 knowledge/seo_fragment/policy 这些词)
POLICY_EXCLUDE_REASON_TEXT = {
    "knowledge": "知识科普类问题——AI 回答这类问题时不会推荐服务商，做了也拿不到推荐位",
    "seo_fragment": "不是真实提问的说法——几乎没有用户会这样问 AI",
    "uncertain": "看不出客户想买什么——需要先问清楚再决定要不要投",
}
POLICY_EXCLUDE_REASON_FALLBACK = "不适合投放——AI 回答这类问题时不会推荐服务商"

GATE_ACTIVE = "active"
GATE_UNAVAILABLE = "unavailable"


def describe_policy_exclusion(intent_type: str) -> str:
    """把引擎的 intent_type 翻成面向人的原因(元指令:工程术语全站翻人话)。"""
    return POLICY_EXCLUDE_REASON_TEXT.get(intent_type, POLICY_EXCLUDE_REASON_FALLBACK)


def partition_by_commercial_policy(
    keywords, brand_name: str = "", intent_hints=None, protected_keywords=None,
):
    """按唯一商业意图引擎切分「可报价」/「不进付费交付」。

    Args:
        keywords: 待报价词
        brand_name: 传给引擎 → 含品牌名的直问判 brand_direct 放行(不误剔品牌信任题)
        intent_hints: {keyword: LLM intent} · 引擎文本判商业时优先于 hint(防"哪家好"被
            误标 informational 后静默丢单 · 见 commercial_query_policy.evaluate docstring)
        protected_keywords: 无条件放行的词(品牌名 keyword)。**不能省** ——
            `batch_pricing._is_brand_keyword` 用 `_normalize_brand_name`(**删掉全部空白**)比对,
            而引擎的 brand_direct 用 `brand_name.strip() in normalize_question(text)`
            (**只并合空白不删**)。品牌名内部带空格时两者判定分叉,实测:
              brand="QZQZ 美学定制" + kw="QZQZ美学定制怎么样"
              → _is_brand_keyword=True 但引擎判 uncertain → 会被误剔。
            那正是 CTO-15.23 修过一次的 QZQZ 爆价案的同一个词形,不能再踩。

    Returns:
        (quotable, excluded, gate_status)
          excluded: [{keyword, intent_type, reason_codes, reason_text, ...}]
          gate_status: GATE_ACTIVE / GATE_UNAVAILABLE(此时 quotable=全部原词)

    降级口径(**故意不 fail-closed**,且绝不静默):
      引擎整体不可用时放行全部词并回 GATE_UNAVAILABLE,由上层显式告知
      「本次未做投放价值核验」。理由:让全站报价瘫痪的代价 > 偶尔漏一个词;
      但"漏了"必须看得见,不许装作核验过。单个词判定抛错 → 该词按可报处理(不误杀)。
    """
    if not keywords:
        return [], [], GATE_ACTIVE
    protected = set(protected_keywords or ())
    try:
        from services.commercial_query_policy import (
            evaluate as _policy_evaluate,
            excluded_from_paid_delivery as _policy_excluded,
        )
    except Exception as exc:  # 引擎不可用 → 放行 + 明示,不静默
        print(f"  [报价意图闸] ⚠️ 商业意图引擎不可用({exc})· 本次放行全部词且标记未核验")
        return list(keywords), [], GATE_UNAVAILABLE

    hints = intent_hints or {}
    quotable, excluded = [], []
    for kw in keywords:
        if kw in protected:  # 品牌名 keyword 走固定低价链路 · 不受意图闸约束
            quotable.append(kw)
            continue
        try:
            decision = _policy_evaluate(
                kw, brand_name=brand_name or None, intent_hint=hints.get(kw),
            )
            if _policy_excluded(decision):
                excluded.append({
                    "keyword": kw,
                    "intent_type": decision.intent_type,
                    "reason_codes": list(decision.reason_codes),
                    "reason_text": describe_policy_exclusion(decision.intent_type),
                    "needs_clarification": bool(decision.needs_clarification),
                    "policy_version": decision.policy_version,
                })
                continue
        except Exception as exc:  # 单词判定异常 → 不误杀
            print(f"  [报价意图闸] 「{kw}」判定异常({exc})· 按可报价处理")
        quotable.append(kw)

    if excluded:
        print(f"  [报价意图闸] 剔除 {len(excluded)}/{len(keywords)} 个不会带来推荐的词: "
              f"{[e['keyword'] for e in excluded][:3]}")
    return quotable, excluded, GATE_ACTIVE


def policy_excluded_markdown(policy_excluded, gate_status: str = GATE_ACTIVE) -> str:
    """被意图闸剔掉的词 → markdown 段。markdown 是对客交付物,不许静默缺词。

    gate_status=GATE_UNAVAILABLE 时明示「本次未做核验」,不假装核过(降级也不静默)。
    """
    if gate_status == GATE_UNAVAILABLE:
        return ("\n\n> ⚠️ 本次未能完成关键词投放价值核验(核验服务暂时不可用),"
                "上述关键词均按原样计价。建议稍后重新生成报价。\n")
    if not policy_excluded:
        return ""
    md = (f"\n\n## 已排除 · 不建议投放的 {len(policy_excluded)} 个关键词\n\n"
          "> 这些词已从报价中剔除，**不计费**。原因是用户问 AI 这类问题时，"
          "AI 的回答里不会推荐服务商——投了也拿不到推荐位。\n\n"
          "| 关键词 | 为什么不建议投 |\n|---|---|\n")
    for item in policy_excluded:
        md += f"| {item.get('keyword','')} | {item.get('reason_text','')} |\n"
    return md


def suggest_commercial_candidates(keywords_snapshot, already_quoted=None,
                                  brand_name: str = "", limit: int = 8):
    """从**本会话候选池**里捞出还没用上的商业/交易型词。

    Owner 2026-08-04 拍板「先捞候选池,不够才推新词」—— 候选池里本来就有商业词时
    零成本零等待就能给出解决动作,不必先去花积分扩词。会话 194 就是活例:
    候选池里躺着「哪家靠谱」「培训班推荐」「机构推荐」「和自学哪个好」四个商业词,
    却一个都没进定价,进去的全是知识题。

    纯读内存快照,不写库、不调 LLM、不扣积分。
    """
    if not keywords_snapshot:
        return []
    try:
        from services.commercial_query_policy import (
            evaluate as _policy_evaluate,
            excluded_from_paid_delivery as _policy_excluded,
        )
    except Exception:
        return []
    used = {str(k) for k in (already_quoted or [])}
    out = []
    for item in keywords_snapshot:
        if not isinstance(item, dict):
            continue
        text = str(item.get("keyword") or "")
        if not text or text in used:
            continue
        try:
            decision = _policy_evaluate(text, brand_name=brand_name or None)
            if _policy_excluded(decision):
                continue
        except Exception:
            continue  # 判不出来的不推荐(建议区宁缺勿滥)
        out.append({
            "id": item.get("id"),
            "keyword": text,
            "category_label": item.get("category_label", ""),
            "intent_type": decision.intent_type,
        })
        if len(out) >= limit:
            break
    return out


def attach_policy_exclusions(pricing_data, engine_output, keywords_snapshot, brand_name=""):
    """把被意图闸剔掉的词挂进 pricing_data 的**只读展示区**。

    Owner 口径(2026-08-04):「全禁进计价明细 + 单列只读展示区」——
      · 这些词绝不出现在 ``pricing_data["keywords"]``(计价明细)里 → 天然不进任何档位总价;
      · 但也不静默消失,单列 ``excluded_keywords`` 供前端渲染只读区 + 原因。
    同时挂 ``suggested_keywords``(候选池里现成的商业词),让提示能"帮人解决"而不只是告知
    (2026-08-04 铁律:提示要么帮人解决,要么不显示)。
    """
    excluded = (engine_output or {}).get("policy_excluded_keywords") or []
    gate_status = (engine_output or {}).get("policy_gate_status", GATE_ACTIVE)
    pricing_data["excluded_keywords"] = excluded
    pricing_data["policy_gate_status"] = gate_status
    if excluded:
        quoted_now = [kw.get("keyword") for kw in (pricing_data.get("keywords") or [])]
        pricing_data["suggested_keywords"] = suggest_commercial_candidates(
            keywords_snapshot,
            already_quoted=quoted_now + [e.get("keyword") for e in excluded],
            brand_name=brand_name,
        )
    return pricing_data


def build_nothing_quotable_message(excluded, suggestions) -> str:
    """整单都不适合投放时给客户/代理看的人话 + 解决动作。"""
    if suggestions:
        hint = "候选池里这几个词可以直接用：" + "、".join(
            s["keyword"] for s in suggestions[:4]
        )
    else:
        hint = "请补充一些客户会用来挑服务商的问法，例如「…哪家靠谱」「…怎么选」「…多少钱」。"
    return (
        f"这 {len(excluded)} 个词都不适合直接报价——用户问 AI 这类问题时，"
        f"AI 的回答里不会推荐服务商，投了也拿不到推荐位，所以没有出价。{hint}"
    )


def nothing_quotable_error(scored, engine_output, keywords_snapshot, brand_name=""):
    """整单都不适合投放 → 返回该抛的 HTTPException;否则返回 None。

    为什么必须单独守:既有的 503 守卫条件是
      ``if engine_output.get("unavailable_keywords") and not scored``——
    要求**断供词非空**才触发。意图闸剔空时断供词是空的 → 那条守卫不响,
    会静默写进一份 0 词 0 元的 pricing_data。0 元报价单发给客户比报错更糟。

    Owner 拍板「词数不够照常报价,只提示可补词」针对的是**还剩词**的情形;
    一个可报词都不剩时没有"报价"可言,只能给动作。
    """
    if scored or not (engine_output or {}).get("policy_excluded_keywords"):
        return None
    from fastapi import HTTPException
    excluded = engine_output["policy_excluded_keywords"]
    suggestions = suggest_commercial_candidates(
        keywords_snapshot,
        already_quoted=[e.get("keyword") for e in excluded],
        brand_name=brand_name,
    )
    return HTTPException(
        status_code=422,
        detail=build_nothing_quotable_message(excluded, suggestions),
    )
