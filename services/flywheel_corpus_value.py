"""B2 语料价值判定(2026-07-29)。

工单原话:"23474 条引用**当前无人消费**。按客户/行业判'这批语料哪些可复用',
产出结构化标签写回,供 B3/B4 与写作取材。"

分工照旧:
  · **代码**负责选候选(哪些被引文章还没标注、被引几次、标题/域/意图是什么)、
    批次切分、写库幂等 —— 全是确定性的;
  · **v4-flash** 只回答"这篇能不能复用、复用在什么场景、价值几档"。

两条硬约束:
  1. **场景词表固定**(`REUSE_SCENARIOS` 六项),模型不许自由发挥 ——
     否则下游没法按场景检索,标签就成了摆设;
  2. **research 域一行不写**,标签落在本单新增的 `flywheel_corpus_value_labels`
     (工单红线:飞轮表写入只走本单新增路径)。

规则兜底不是"随便给一档":按被引次数与文章意图给出保守但有依据的标签(见 `_rule_label`),
所以即使 provider 全挂,这条管道照样出水,只是判断粗一些。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from db.flywheel_corpus_label_db import (
    REUSE_SCENARIOS,
    labeled_article_ids,
    upsert_corpus_labels,
)

logger = logging.getLogger("GEO-FlywheelCorpus")

POINT_KEY = "corpus_value_labeling"

DEFAULT_BATCH_SIZE = 20
DEFAULT_DAILY_LIMIT = 200

#: 文章意图 → 默认复用场景(规则兜底与 prompt 提示共用同一张表,口径不分叉)。
_INTENT_TO_SCENARIOS: dict[str, tuple[str, ...]] = {
    "ranking": ("diagnosis_question", "competitor_mapping", "buyer_decision"),
    "comparison": ("competitor_mapping", "buyer_decision"),
    "faq": ("diagnosis_question", "writing_evidence"),
    "tutorial": ("writing_evidence", "industry_background"),
    "definition": ("industry_background",),
    "data_report": ("writing_evidence", "industry_background"),
    "policy": ("industry_background",),
    "long_form": ("writing_evidence",),
}


def _fetch_candidates(industry_key: str, limit: int) -> list[dict[str, Any]]:
    """未标注的被引文章,按被引次数从高到低。

    SQL 4 维核验:行业维度取自 `geo_research_article_citations.industry_id`
    (`geo_research_articles` 无 industry_id,只有自由文本 primary_industry);
    tombstone / 黑名单域按既有口径排除;全部只读。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = []
        industry_filter = ""
        if industry_key:
            industry_filter = "AND i.slug = %s"
            params.append(industry_key)
        params.append(int(limit))
        cur.execute(
            f"""
            SELECT gra.id            AS article_id,
                   gra.title         AS title,
                   gra.domain        AS domain,
                   gra.intent_type   AS intent_type,
                   COALESCE(i.slug, '') AS industry_key,
                   COALESCE(i.name, '') AS industry_name,
                   COUNT(c.id)       AS citations
              FROM geo_research_article_citations c
              JOIN geo_research_articles gra
                   ON gra.id = c.article_id
                  AND gra.domain_tier <> 'blacklist'
                  AND gra.clean_status = 'cleaned'
              LEFT JOIN geo_research_industries i ON i.id = c.industry_id
             WHERE TRUE {industry_filter}
             GROUP BY gra.id, gra.title, gra.domain, gra.intent_type, i.slug, i.name
             ORDER BY COUNT(c.id) DESC, gra.id DESC
             LIMIT %s
            """,
            tuple(params),
        )
        return [dict(r) for r in cur.fetchall() or []]
    finally:
        conn.close()


def _rule_label(row: dict[str, Any]) -> dict[str, Any]:
    """规则兜底:被引次数定价值档,意图定场景。保守但有依据,绝不瞎标 high。"""
    citations = int(row.get("citations") or 0)
    intent = str(row.get("intent_type") or "")
    scenarios = list(_INTENT_TO_SCENARIOS.get(intent, ()))
    if citations >= 5:
        tier, reusable = "high", True
    elif citations >= 2:
        tier, reusable = "medium", True
    else:
        tier, reusable = "low", False
    if reusable and not scenarios:
        # 意图未分类(生产 14044 篇 intent_type 为空)但确实被反复引用 → 至少可作写作证据。
        scenarios = ["writing_evidence"]
    return {
        "article_id": row.get("article_id"),
        "industry_key": row.get("industry_key") or "general",
        "reusable": reusable,
        "value_tier": tier,
        "scenarios": scenarios,
        "reason": f"规则兜底:近似按被引 {citations} 次与意图 {intent or '未分类'} 判定",
        "source": "rule",
        "citations": citations,
    }


def _build_prompt(batch: list[dict[str, Any]]) -> str:
    rows = [
        {
            "article_id": r.get("article_id"),
            "title": str(r.get("title") or "")[:120],
            "domain": r.get("domain"),
            "intent_type": r.get("intent_type") or "",
            "industry": r.get("industry_name") or "",
            "ai_citations": r.get("citations"),
        }
        for r in batch
    ]
    scenario_lines = "\n".join(f"  - {k}:{v}" for k, v in REUSE_SCENARIOS.items())
    return (
        "下面是一批被 AI 搜索引擎引用过的行业文章。我们要判断它们能不能作为素材复用 ——"
        "复用去向是:给新客户做诊断时出题、挖竞品、以及写文章时当证据。\n\n"
        + json.dumps(rows, ensure_ascii=False, indent=2)
        + "\n\n可选的复用场景(**只能从这些 key 里挑**,不要自造):\n"
        + scenario_lines
        + "\n\n判断口径:\n"
        "1. ai_citations 越高说明 AI 越愿意引用它,越值得复用;\n"
        "2. 榜单/对比/问答类通常能直接喂给诊断出题和竞品挖掘;科普/政策类多作背景;\n"
        "3. 标题明显是营销软文、与行业无关、或信息量过低的,判 reusable=false;\n"
        "4. value_tier 只能是 high / medium / low,拿不准就给 low,不要充数。\n\n"
        "对每篇给一条结果。严格只输出 JSON:\n"
        '{"labels": [{"article_id": 1, "reusable": true, "value_tier": "high", '
        '"scenarios": ["diagnosis_question"], "reason": "一句话"}]}'
    )


def label_corpus_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """标一批。模型不可用/越界 → 整批走规则兜底(不做半 LLM 半规则的混合结果)。"""
    if not batch:
        return []
    from services.flywheel_judgment import judge

    def _validate(payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and isinstance(payload.get("labels"), list)
            and len(payload["labels"]) > 0
        )

    result = judge(
        POINT_KEY,
        prompt=_build_prompt(batch),
        rule_fallback=lambda: [_rule_label(r) for r in batch],
        input_summary={"batch_size": len(batch),
                       "industries": sorted({str(r.get("industry_key") or "") for r in batch})},
        validate=_validate,
    )
    if not result.from_llm:
        return list(result.payload or [])

    by_id = {int(r["article_id"]): r for r in batch}
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in (result.payload or {}).get("labels") or []:
        if not isinstance(item, dict):
            continue
        try:
            article_id = int(item.get("article_id"))
        except (TypeError, ValueError):
            continue
        row = by_id.get(article_id)
        if row is None or article_id in seen:
            continue  # 模型报了不在本批里的 id → 丢弃,不写库
        seen.add(article_id)
        scenarios = [s for s in (item.get("scenarios") or []) if s in REUSE_SCENARIOS]
        out.append({
            "article_id": article_id,
            "industry_key": row.get("industry_key") or "general",
            "reusable": bool(item.get("reusable")),
            "value_tier": str(item.get("value_tier") or "low"),
            "scenarios": scenarios,
            "reason": str(item.get("reason") or "")[:1000],
            "source": "llm",
            "model": result.model,
            "citations": int(row.get("citations") or 0),
        })

    # 模型漏判的补规则兜底 —— 候选进了批次就必须有结论,不能悄悄丢掉。
    for row in batch:
        if int(row["article_id"]) not in seen:
            out.append(_rule_label(row))
    return out


def run_corpus_value_labeling(
    *, industry_key: str = "", limit: int = DEFAULT_DAILY_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE, dry_run: bool = False,
) -> dict[str, Any]:
    """把还没标注的被引语料标一轮。返回结构给心跳层读处理量。"""
    # 多取一些再过滤已标注的,避免每次都被同一批高被引文章占满配额。
    candidates = _fetch_candidates(industry_key, limit * 3)
    if not candidates:
        return {"processed": 0, "scanned": 0, "note": "无被引语料候选"}

    already = labeled_article_ids([r["article_id"] for r in candidates])
    pending = [r for r in candidates if int(r["article_id"]) not in already][:limit]
    if not pending:
        return {"processed": 0, "scanned": len(candidates), "note": "候选均已标注"}

    labels: list[dict[str, Any]] = []
    for start in range(0, len(pending), max(1, batch_size)):
        labels.extend(label_corpus_batch(pending[start:start + max(1, batch_size)]))

    if dry_run:
        return {"processed": 0, "scanned": len(candidates), "labels": len(labels),
                "dry_run": True}

    written = upsert_corpus_labels(labels)
    by_llm = sum(1 for label in labels if label.get("source") == "llm")
    reusable = sum(1 for label in labels if label.get("reusable"))
    logger.info(
        "[B2] 语料价值标注完成:写 %s 条(模型判 %s / 可复用 %s)", written, by_llm, reusable
    )
    return {
        "processed": written, "scanned": len(candidates), "pending": len(pending),
        "by_llm": by_llm, "reusable": reusable,
    }
