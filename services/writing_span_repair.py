"""[WP9-P0-7 ② · D8] span 级"AI 仅修复此 finding 所在段"。

D8 铁律:文章层零阻断,放行权归用户——findings 只做定位标注,用户可选"AI 修复此处"或
"忽略"。本服务提供**段级**修复:

- 只重写命中所在的**那一段**,不重跑整篇 → 不产生第二次全文生成费用;
- 定位锚 = finding 的**精确命中串**(TrustFinding.matched_text)。刻意不用字符偏移:
  检测文本经 `_without_negated_rankings` 删改,偏移不可靠;命中串在原文原样存在;
- 修完**自验**:重新评估该段,仍违规则如实返回 not fixed(绝不谎报修好);
- 定位不到 / LLM 失败 → 原文一字不动 + 诚实失败原因,调用方给出口(§13 合同)。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

# 段落切分:优先空行分段;单段过长时回落到行。
_PARA_SEP = "\n\n"


def locate_paragraph(content: str, matched_text: str) -> Optional[dict[str, Any]]:
    """定位命中串所在段。返回 {start, end, paragraph}(content 上的切片边界)或 None。

    命中串出现多次时取**第一处**(修完可再次触发下一处,逐处修复语义清晰)。
    """
    text = str(content or "")
    needle = str(matched_text or "").strip()
    if not text or not needle:
        return None
    hit = text.find(needle)
    if hit < 0:
        return None
    # 向前找段首、向后找段尾(以空行为界;无空行则退化为全文单段)
    start = text.rfind(_PARA_SEP, 0, hit)
    start = 0 if start < 0 else start + len(_PARA_SEP)
    end = text.find(_PARA_SEP, hit + len(needle))
    end = len(text) if end < 0 else end
    return {"start": start, "end": end, "paragraph": text[start:end]}


def splice_paragraph(content: str, start: int, end: int, new_paragraph: str) -> str:
    """把 [start,end) 段替换为 new_paragraph,其余部分逐字不动。"""
    text = str(content or "")
    return text[:start] + str(new_paragraph or "") + text[end:]


def build_span_repair_prompt(paragraph: str, finding: dict[str, Any]) -> str:
    """只针对该段的最小修复指令(不带整篇上下文 → 输入更小、更省、不重写全文)。"""
    code = str(finding.get("code") or "")
    message = str(finding.get("message") or "")
    matched = str(finding.get("matched_text") or "")
    return (
        "请只修改下面这一段文字中的问题部分,其余内容保持原样。\n"
        f"问题类型:{code}\n"
        f"问题说明:{message}\n"
        + (f"命中片段:{matched}\n" if matched else "")
        + "修改要求:\n"
        "- 只改这一段,不要补写其它段落、不要加标题、不要输出解释;\n"
        "- 去掉违规表述,改为**有依据的相对表述**(如\"在 X 口径下表现较好\"),不得新增未提供的事实、数字或竞品;\n"
        "- 保持原段的信息量、语气与长度量级。\n"
        "只输出修改后的这一段正文:\n\n"
        f"{paragraph}"
    )


# [工单 C-3 T3 2026-07-27] 证据精度族 finding code(段级自验走 precision 评估器,
# 需要 evidence_pack/brand_fact_snapshot 上下文,由调用方经 finding dict 传入)。
_PRECISION_FAMILY_CODES = frozenset({
    "claim_missing_inline_evidence",
    "inline_evidence_claim_mismatch",
    "unknown_inline_evidence_id",
    "unverified_inline_evidence_id",
    # [返修 C1 2026-08-11] customer_fact_source_boundary_missing 判定已拆除
    # (它要求写自曝边界词,与清零链互打),从精度族清单同步移除。
    "bibliography_only_support",
    "default_positive_pressure",
    "default_negative_pressure",
    "inference_verification_action_missing",
})


def _still_violates(
    paragraph: str,
    code: str,
    evidence_pack: dict | None = None,
    brand_fact_snapshot: dict | None = None,
    full_content: str | None = None,
) -> bool:
    """自验:修完的这一段是否仍触发同一 finding(修完不谎报)。

    [工单 C-3 T3] 旧实现只查 trust **hard** —— 批量修复的对象恰恰是 soft/advisory
    (ordered_brand_candidates / unsourced_outcome_number / 精度族),旧自验对它们
    恒 False → 没修好也报修好。

    🔴 [返修 C3 2026-08-11] 评估对象从**孤立段落**改为**整篇候选文档**
    (调用方传入替换后的全文)。豁免/结构判据(一次性声明作用域、参考文献节、
    文档级血缘对齐)都是全文级 —— 孤立段落上它们恒真,导致"永远修不好"或
    诱导模型删改本不违规的内容。归段规则:
      · 带定位锚(matched_text)的 finding:锚落在**改后的这一段**里才算数
        (别的段落自己的同名问题不记在本次修复头上);
      · 不带锚的全文级存在性 finding(bibliography_only_support /
        no_verified_evidence_available 一类):按全文真值算 —— 本段的修复
        真的消除了它就绿,问题出在别处就如实红。
    未传 full_content 的旧调用退回段级评估(兼容,不静默变语义)。
    """
    scan_target = full_content if full_content is not None else paragraph

    def _hit(findings) -> bool:
        for f in findings:
            if getattr(f, "code", None) != code:
                continue
            anchor = str(getattr(f, "matched_text", "") or "")
            if not anchor:
                return True          # 全文级存在性 finding:按全文真值
            if full_content is None or anchor in paragraph:
                return True          # 锚在改后的段里 → 本段仍违规
        return False

    try:
        if code in _PRECISION_FAMILY_CODES:
            from writing.evidence_precision_policy import evaluate_evidence_precision
            assessment = evaluate_evidence_precision(
                scan_target, evidence_pack or {}, brand_fact_snapshot,
            )
            return _hit(tuple(assessment.hard) + tuple(assessment.warnings))
        from writing.evidence_first_policy import evaluate_content_trust
        trust = evaluate_content_trust("", scan_target, evidence_mode="unknown")
        return _hit(tuple(trust.hard) + tuple(trust.soft))
    except Exception:
        # 无法自验时保守认为未修好(宁可如实说没修好,也不谎报已修)
        return True


#: [span 级 AI 免费修复 2026-07-30 · §5] span 级引擎复用**同一个**判定函数做重判,
#: 不许在那边再写第二份自验口径("锁绿而实际未达成"本项目已踩过)。
still_violates = _still_violates


async def repair_finding_span(
    content: str,
    finding: dict[str, Any],
    llm_fn: Callable[[str], Awaitable[str]],
    *,
    industry: str = "",
    title: str = "",
    preserve_terms: tuple[str, ...] = (),
    target_entity: str = "",
) -> dict[str, Any]:
    """对单个 finding 做修复。

    [R5.1] target_entity = 本文品牌(主体核对用,见 span_level_repair)。

    返回 {ok, content, reason, paragraph_before, paragraph_after}。
    ok=False 时 content 恒为原文(一字不动)。

    [span 级 AI 免费修复 2026-07-30] **四条底线类 code 路由到真 span 级引擎**
    (services.span_level_repair:只给模型那一句 + 只读上下文,机械校验四项,
    除该 span 外正文逐字节相同)。其余 code 走下面既有的整段级路径,行为不变
    —— 底线类以外的定位锚常是整块(claim_missing_inline_evidence 等),硬塞进
    span 形状只会全部转人工,那是回归不是改进。
    """
    original = str(content or "")
    from services.span_level_repair import BOTTOMLINE_CATEGORIES, repair_bottomline_span

    if str(finding.get("code") or "").strip() in BOTTOMLINE_CATEGORIES:
        return await repair_bottomline_span(
            original, finding, llm_fn,
            industry=industry, title=title, preserve_terms=preserve_terms,
            target_entity=target_entity,
        )
    located = locate_paragraph(original, finding.get("matched_text") or "")
    if not located:
        return {"ok": False, "content": original, "reason": "span_not_located"}
    prompt = build_span_repair_prompt(located["paragraph"], finding)
    try:
        rewritten = await llm_fn(prompt)
    except Exception:
        return {"ok": False, "content": original, "reason": "repair_llm_failed"}
    rewritten = str(rewritten or "").strip()
    if not rewritten:
        return {"ok": False, "content": original, "reason": "repair_empty"}
    code = str(finding.get("code") or "")
    # [返修 C3] 先拼出整篇候选文档,再在**全文语境**里自验(段内孤立评估会让
    # 全文级豁免/结构判据恒真,"永远修不好")。
    candidate_doc = splice_paragraph(original, located["start"], located["end"], rewritten)
    if code and _still_violates(
        rewritten,
        code,
        finding.get("evidence_pack"),
        finding.get("brand_fact_snapshot"),
        full_content=candidate_doc,
    ):
        # 诚实:改完仍违规 → 不落盘,交还用户(可再试或忽略)
        return {"ok": False, "content": original, "reason": "still_violating"}
    return {
        "ok": True,
        "content": candidate_doc,
        "reason": "repaired",
        "paragraph_before": located["paragraph"],
        "paragraph_after": rewritten,
    }
