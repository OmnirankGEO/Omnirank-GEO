"""[工单 C-4 2026-07-27] 审核自动驾驶 · hard 红线自动修复服务。

Owner 拍板:"客户又不看又不懂,叫他们审核只是增加负担。" 产品口径:
- **红线不放松**(编造数字/自创评分/绝对化用语——引擎真会惩罚),只把处理方式
  从"逐处要用户确认"改成"**机器自动修,修不好才浮出**";
- 修复复用 C-3 段级链(writing_span_repair.repair_finding_span + _still_violates
  自验):只改命中段、不重跑整篇、平台侧承担 LLM 成本(与写作续写重试同口径,
  用户 0 扣费);
- 轮数硬上限:生成期自动修复 ≤MAX_AUTO_REPAIR_ROUNDS(1),用户一键重试累计
  ≤MAX_ONE_CLICK_ROUNDS(2)——防修复循环烧钱,上限锁配变异;
- 本服务**只算不写库**(article_topic_write_census 白名单不动):落库由调用方
  (_save_article / rewrite_article / auto-repair 端点)在既有授权函数内完成。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

#: 生成期自动修复轮数上限(每轮 = 对当前全部可定位 hard 各做一次段级修复)。
MAX_AUTO_REPAIR_ROUNDS = 1
#: 用户"一键修复"累计轮数上限(跨点击累计,存 quality_warning.auto_repair.manual_rounds)。
MAX_ONE_CLICK_ROUNDS = 2


def default_repair_llm() -> Callable[[str], Awaitable[str]]:
    """段级修复的缺省 LLM(与 repair-finding 端点同参:短 prompt、平台侧成本)。"""

    async def _llm(prompt: str) -> str:
        from tools.multi_llm_caller import MultiLLMCaller

        text, _provider = await MultiLLMCaller(
            timeout=90.0, temperature=0.3
        ).call(prompt, verbose=False)
        return text

    return _llm


def _hard_findings(title: str, content: str, evidence_mode: str) -> list[dict[str, Any]]:
    from writing.evidence_first_policy import evaluate_content_trust

    trust = evaluate_content_trust(title, content, evidence_mode=evidence_mode)
    return [
        {
            "code": item.code,
            "message": item.message,
            "matched_text": item.matched_text,
            "evidence": item.evidence,
        }
        for item in trust.hard
    ]


async def autopilot_repair_hard(
    title: str,
    content: str,
    *,
    evidence_mode: str = "unknown",
    evidence_pack: Optional[dict[str, Any]] = None,
    brand_fact_snapshot: Optional[dict[str, Any]] = None,
    llm_fn: Optional[Callable[[str], Awaitable[str]]] = None,
    max_rounds: int = MAX_AUTO_REPAIR_ROUNDS,
    target_entity: str = "",
) -> dict[str, Any]:
    """对 hard findings 做 ≤max_rounds 轮段级自动修复。

    [R5.1] target_entity = 本文品牌(修复链主体核对用,空 = 旧行为)。

    每轮:重评 → 对当前全部**可定位**(有 matched_text)hard 逐个段级修复(自验
    仍违规则该处原文不动,诚实记 still_violating)→ 下一轮前整体重评。
    无 hard 或轮数用尽即停;**轮数上限是硬闸**,llm 永远修不好也只烧 max_rounds 轮。

    返回:
      {content, rounds_used, repaired: [record...], remaining: [finding...],
       records: [每次尝试的 {code, matched_text, ok, reason}...], at}
    remaining = 结束时仍在的 hard findings(含无法定位的)——一条不少,
    调用方据此决定 blocked/汇总卡。
    """
    from services.writing_span_repair import repair_finding_span

    llm = llm_fn or default_repair_llm()
    max_rounds = max(0, int(max_rounds))
    current = str(content or "")
    records: list[dict[str, Any]] = []
    repaired: list[dict[str, Any]] = []
    rounds_used = 0

    remaining = _hard_findings(title, current, evidence_mode)
    while remaining and rounds_used < max_rounds:
        rounds_used += 1
        for finding in remaining:
            if not finding.get("matched_text"):
                records.append({
                    "code": finding["code"],
                    "matched_text": "",
                    "ok": False,
                    "reason": "span_not_located",
                })
                continue
            result = await repair_finding_span(
                current,
                {
                    "code": finding["code"],
                    "message": finding["message"],
                    "matched_text": finding["matched_text"],
                    "evidence_pack": evidence_pack,
                    "brand_fact_snapshot": brand_fact_snapshot,
                },
                llm,
                target_entity=target_entity,
            )
            record = {
                "code": finding["code"],
                "matched_text": str(finding.get("matched_text") or "")[:120],
                "ok": bool(result.get("ok")),
                "reason": str(result.get("reason") or ""),
            }
            records.append(record)
            if result.get("ok"):
                current = result["content"]
                repaired.append(record)
        remaining = _hard_findings(title, current, evidence_mode)

    return {
        "content": current,
        "rounds_used": rounds_used,
        "repaired": repaired,
        "remaining": remaining,
        "records": records,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def merge_auto_repair_state(
    quality_warning: Any,
    outcome: dict[str, Any],
    *,
    trigger: str,
) -> dict[str, Any]:
    """把一次修复轮的结果并进 quality_warning.auto_repair(记录一条不少,可追溯)。

    trigger ∈ {"auto"(生成期), "one_click"(用户一键)}。
    auto/manual 轮数分开累计 —— 上限判定各查各的。
    """
    qw = quality_warning if isinstance(quality_warning, dict) else {}
    state = qw.get("auto_repair")
    if not isinstance(state, dict):
        state = {"auto_rounds": 0, "manual_rounds": 0, "records": []}
    if trigger == "auto":
        state["auto_rounds"] = int(state.get("auto_rounds") or 0) + int(outcome.get("rounds_used") or 0)
    else:
        state["manual_rounds"] = int(state.get("manual_rounds") or 0) + int(outcome.get("rounds_used") or 0)
    state["records"] = (list(state.get("records") or []) + list(outcome.get("records") or []))[-40:]
    state["repaired_count"] = int(state.get("repaired_count") or 0) + len(outcome.get("repaired") or [])
    state["remaining_count"] = len(outcome.get("remaining") or [])
    state["last_trigger"] = trigger
    state["last_at"] = outcome.get("at")
    qw["auto_repair"] = state
    return qw


def one_click_rounds_left(quality_warning: Any) -> int:
    """一键修复剩余可用轮数(跨点击累计,服务端硬闸)。"""
    qw = quality_warning if isinstance(quality_warning, dict) else {}
    state = qw.get("auto_repair") if isinstance(qw.get("auto_repair"), dict) else {}
    used = int(state.get("manual_rounds") or 0)
    return max(0, MAX_ONE_CLICK_ROUNDS - used)
