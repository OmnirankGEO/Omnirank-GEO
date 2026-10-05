"""写作策略版本的 AI 评估(包② §3F)+ 绑定候选 AI 确认。

工单:docs/AI-CONTEXT/WORKORDER_AI_REVIEW_REPLACES_HUMAN_2026-08-01.md §3F

生产实测(2026-08-01):`writing_strategy_versions` **42 版本全部 status='shadow'**,
`reviewed_by` / `reviewed_at` / `activated_at` **三列全空** —— 上线至今没有任何一个
策略版本被评审过、更没有被激活过。人审这条路事实上是废的,这正是本包要接管的。

🔴 **两处必须写进代码的订正**(照工单字面执行会直接报错 / 越界):

1. **`reviewed_by` 是 BIGINT,不是 reviewer 名字列。**
   工单原话"写 `reviewed_by='ai:deepseek-chat@…'`"类型上不可能。
   AI 身份落 `geo_strategy_ai_review_reports.reviewer` + 主表 `review_note` 串;
   `reviewed_by` 按本仓既有的系统 actor 约定写 0(`persist_evolution_cycle(actor_user_id=0)` 同款)。

2. **激活默认自动做(Owner 2026-08-01 拍板)。**
   §3F 说"通过即激活",§1 范围边界(标注"越界即事故")却把平台侧规则变更
   划为"保留 AI 预核对 + 一键确认"。复审已确认这是**工单自身前后矛盾**
   (v2→v3 改写时 §3F 旧句没跟着边界改)。我首版按边界从严交了默认关,
   并把理由摆给 Owner;Owner 看过后决定打开,`STRATEGY_AI_AUTOACTIVATE` 默认 true。
   🔴 打开的是"判过之后不用再点一次",**不是"少判一道"**:
   fail-closed / 冲突项必须带 rule+evidence / 自相矛盾从严按 reject,三条一个没松。

其余四件套与包①同口径:advisory / 留痕 / fail-closed / 幂等。
渠道复用 `services.article_ai_review`(官方 api.deepseek.com),不另起。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Final

from services import article_ai_review as _air
from services.article_ai_review import AI_MODEL, DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_PATH

logger = logging.getLogger("GEO-StrategyReviewAI")

STRATEGY_PROMPT_VERSION: Final = "v1-2026-08-01"
STRATEGY_REVIEWER: Final = f"ai:{AI_MODEL}@strategy-{STRATEGY_PROMPT_VERSION}"

#: 本仓既有的系统 actor 约定(persist_evolution_cycle(actor_user_id=0) 同款)。
SYSTEM_ACTOR_ID: Final = 0

VERDICT_PASS: Final = "pass"
VERDICT_REJECT: Final = "reject"
VERDICT_NOT_CHECKED: Final = "not_checked"


def autoactivate_enabled() -> bool:
    """🔴 **默认开(Owner 2026-08-01 拍板)。**

    我原本交的是默认关,理由写在下面;Owner 看过理由后决定打开,按其裁定执行。
    保留原理由供后来人知道这个开关的分量:
      · 策略是**全站级写作规则**,一条错策略会污染此后所有文章;
      · 与"客户决定自己那篇文章发不发"不是一回事 —— 后者错了只影响一篇。

    🔴 打开的是"AI 判通过后不用再点一次",**不是"少判一道"**:
    evaluate_strategy_version 的 fail-closed、冲突项必须带 rule+evidence、
    自相矛盾从严按 reject —— 三条一个没松。AI 判不过的策略照样激活不了。
    要临时收回人工确认,把环境变量显式设成 0/false 即可。
    """
    return os.getenv("STRATEGY_AI_AUTOACTIVATE", "true").strip().lower() in {"1", "true", "yes", "on"}


_PROMPT: Final = """你在审核一条“写作策略版本”能不能被启用。它会影响此后所有文章的写法。

判两件事:
1. 与下列红线有没有冲突(有一条冲突就不能通过):
   - 不得出现《广告法》绝对化用语(第一名/最佳/唯一首选/遥遥领先等)
   - 不得自创评分体系冒充独立评价(综合评分/五星推荐/S-A-B 级)
   - 不得要求虚构数据、案例、资质、媒体背书
   - 不得把企业自述包装成独立结论
2. 有没有数据支撑(evidence_score / outcome_score / confidence 是否足以支持这条策略)

只输出 JSON:
{{"verdict":"pass|reject","summary":"一句话结论","conflicts":[{{"rule":"冲突的是哪条红线","evidence":"策略原文里的哪一句"}}]}}

🔴 conflicts 每条都必须同时给出 rule 与 evidence(原文哪一句)。
禁止输出“可能有风险”“建议再看看”这类没有落点的话。verdict=pass 时 conflicts 为空数组。

策略内容:
{payload}
"""


def _parse(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw.strip("`")
        raw = raw.removeprefix("json").strip()
    try:
        obj = json.loads(raw)
    except Exception:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            obj = json.loads(raw[start:end + 1])
        except Exception:
            return {}
    return obj if isinstance(obj, dict) else {}


def _normalize_conflicts(raw: Any) -> list[dict[str, str]]:
    """只保留**同时**给出 rule 与 evidence 的冲突项(§3D 禁笼统,同一口径)。"""
    out: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if not rule or not evidence:
            continue
        out.append({"rule": rule, "evidence": evidence})
    return out


def evaluate_strategy_version(row: dict[str, Any]) -> dict[str, Any]:
    """评一条策略版本 → {verdict, summary, conflicts}。任何失败返回 not_checked,不抛。"""
    api_key = _air._deepseek_key()
    if not api_key:
        return {"verdict": VERDICT_NOT_CHECKED, "summary": "策略评估未执行(缺少可用密钥)",
                "conflicts": []}
    payload = json.dumps({
        "industry_key": row.get("industry_key"),
        "strategy_version": row.get("strategy_version"),
        "style_family": row.get("style_family"),
        "guidance": row.get("guidance"),
        "common_elements": row.get("common_elements"),
        "guardrails": row.get("guardrails"),
        "evidence_score": str(row.get("evidence_score")),
        "outcome_score": str(row.get("outcome_score")),
        "confidence": str(row.get("confidence")),
    }, ensure_ascii=False)

    try:
        data = _air._post_chat(
            DEEPSEEK_BASE_URL + DEEPSEEK_CHAT_PATH,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            {
                "model": AI_MODEL,
                "messages": [{"role": "user", "content": _PROMPT.format(payload=payload[:16000])}],
                "temperature": 0.1,
            },
        )
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as exc:
        logger.warning("[strategy-ai] 调用失败 · fail-closed: %s", exc)
        return {"verdict": VERDICT_NOT_CHECKED, "summary": "策略评估未完成(服务暂时不可用)",
                "conflicts": []}

    parsed = _parse(content)
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in {VERDICT_PASS, VERDICT_REJECT}:
        logger.warning("[strategy-ai] 结论无法识别(%r) · fail-closed", verdict)
        return {"verdict": VERDICT_NOT_CHECKED, "summary": "策略评估结论无法解析", "conflicts": []}

    conflicts = _normalize_conflicts(parsed.get("conflicts"))
    if verdict == VERDICT_REJECT and not conflicts:
        # 说不通过却指不出冲突哪条 = 笼统结论,不作数(与 §3D 同型)
        logger.warning("[strategy-ai] reject 但无具体冲突项 · 视为未核查")
        return {"verdict": VERDICT_NOT_CHECKED, "summary": "策略评估结论缺少具体冲突项",
                "conflicts": []}
    if verdict == VERDICT_PASS and conflicts:
        # 🔴 自相矛盾(说通过却列了冲突)一律**从严**:按 reject 处理,不赌。
        logger.warning("[strategy-ai] pass 却带冲突项 · 从严按 reject 处理")
        return {"verdict": VERDICT_REJECT,
                "summary": str(parsed.get("summary") or "").strip() or "结论自相矛盾,从严处理",
                "conflicts": conflicts}

    return {
        "verdict": verdict,
        "summary": str(parsed.get("summary") or "").strip(),
        "conflicts": conflicts,
    }


_UPSERT_REPORT_SQL: Final = """
    INSERT INTO geo_strategy_ai_review_reports
        (strategy_id, reviewer, verdict, summary, conflicts, evidence)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (strategy_id, reviewer) DO UPDATE
       SET verdict = EXCLUDED.verdict,
           summary = EXCLUDED.summary,
           conflicts = EXCLUDED.conflicts,
           evidence = EXCLUDED.evidence,
           updated_at = NOW()
    RETURNING id
"""


def persist_strategy_review(cursor, strategy_id: int, verdict: dict[str, Any],
                            *, evidence: dict[str, Any] | None = None) -> int:
    from psycopg2.extras import Json

    cursor.execute(_UPSERT_REPORT_SQL, (
        int(strategy_id), STRATEGY_REVIEWER, verdict["verdict"],
        verdict.get("summary") or "",
        Json(verdict.get("conflicts") or []),
        Json(evidence or {}),
    ))
    return int(cursor.fetchone()["id"])


# ===========================================================================
# 绑定候选 AI 确认(§3F 第二件)
# ===========================================================================
#: 🔴 **必须带这个切片条件**。生产实测:`geo_media_binding_candidates` 全表 7095 行,
#: 而待确认的只有 `status='candidate'` 的 **1390** 条(1356 + 34)。
#: 不带条件会多铺 5705 条 —— 其中 5564 条是 already approved,重跑等于把已确认的再确认一遍。
BINDING_CANDIDATE_SQL: Final = """
    SELECT id, candidate_key, entity_key, industry_key, media_source,
           media_name, inventory_url, match_method, match_confidence,
           can_approve, risk_flags
      FROM geo_media_binding_candidates
     WHERE status = 'candidate'
       AND COALESCE(active, TRUE) = TRUE
     ORDER BY id
"""


def count_binding_candidates(cursor) -> int:
    cursor.execute(f"SELECT COUNT(*) AS n FROM ({BINDING_CANDIDATE_SQL}) t")
    return int(cursor.fetchone()["n"])
