"""B4 诊断沉淀复用(2026-07-29)。

工单原话:"新客户进来先判'哪些既有行业语料/竞品图谱可直接用',喂给诊断出题与竞品挖掘
(**本周两处 bug 的正解:给素材而非硬编码**)。"

那两处 bug(2026-07-26/27 已修)的共同形状是:题面里直接拼行业名录原文、竞品挖掘描述错主语 ——
根子都是**没素材就硬编码**。B4 的职责就是在诊断开跑之前,把手上已经有的东西摊出来:

  · 已被 AI 引用过、且 B2 判为可复用的行业语料(`flywheel_corpus_value_labels`);
  · AI 回答里真实出现过的同行实体(`geo_research_answer_entities`,生产 21419 行,
    带 recommendation_rank / 推荐理由 / 置信度)。

代码把候选摊好,v4-flash 只做"这家新客户用得上哪些"的取舍,并给出问题种子。
产出**全部是素材(advisory)**:诊断出题与竞品挖掘拿它当参考,不是拿它当答案 ——
本模块不写任何诊断表、不改任何题面、更不阻断诊断。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-FlywheelDiagnosisReuse")

POINT_KEY = "diagnosis_reuse_matching"

DEFAULT_CORPUS_LIMIT = 20
DEFAULT_ENTITY_LIMIT = 25


def _competitor_candidates(industry_key: str, limit: int) -> list[dict[str, Any]]:
    """AI 回答里真实出现过的同行实体(只读)。

    SQL 4 维核验(生产 `\\d geo_research_answer_entities` 逐列核过):
      1. 列名:industry_key / entity_name / entity_type / recommendation_rank /
         recommendation_reasons / confidence 均在场
      2. data_type:recommendation_rank INTEGER · confidence NUMERIC ·
         recommendation_reasons JSONB · industry_key VARCHAR
      3. 字段归属:全在 geo_research_answer_entities,不 join 诊断域任何表
      4. 只读:纯 SELECT
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT entity_name,
                   MAX(entity_type)                       AS entity_type,
                   COUNT(*)                               AS mentions,
                   MIN(NULLIF(recommendation_rank, 0))    AS best_rank,
                   AVG(COALESCE(confidence, 0))           AS avg_confidence
              FROM geo_research_answer_entities
             WHERE industry_key = %s
               AND btrim(COALESCE(entity_name, '')) <> ''
             GROUP BY entity_name
             ORDER BY COUNT(*) DESC, MIN(NULLIF(recommendation_rank, 0)) NULLS LAST
             LIMIT %s
            """,
            (industry_key or "", int(limit)),
        )
        return [
            {
                "entity_name": r["entity_name"],
                "entity_type": r["entity_type"],
                "mentions": int(r["mentions"] or 0),
                "best_rank": r["best_rank"],
                "avg_confidence": round(float(r["avg_confidence"] or 0), 3),
            }
            for r in cur.fetchall() or []
        ]
    finally:
        conn.close()


def _corpus_candidates(industry_key: str, limit: int) -> list[dict[str, Any]]:
    from db.flywheel_corpus_label_db import list_reusable_corpus

    rows = list_reusable_corpus(industry_key, limit=limit, min_tier="medium")
    return [
        {
            "article_id": r.get("article_id"),
            "value_tier": r.get("value_tier"),
            "scenarios": r.get("scenarios"),
            "citations": r.get("citations"),
            "reason": r.get("reason"),
        }
        for r in rows
    ]


def _rule_material(
    brand_name: str, industry: str, corpus: list[dict[str, Any]],
    entities: list[dict[str, Any]],
) -> dict[str, Any]:
    """规则兜底:按被引/提及次数取头部,并把本品牌自己从竞品名单里剔掉。

    不造问题种子 —— 没有模型时宁可不给种子,也不硬编码一串模板问题
    (硬编码正是本周那两处 bug 的病根)。
    """
    self_names = {n for n in (brand_name or "").split() if n}
    self_names.add(str(brand_name or "").strip())
    competitors = [
        e for e in entities
        if str(e.get("entity_name") or "").strip() not in self_names
    ][:10]
    return {
        "corpus_article_ids": [c["article_id"] for c in corpus[:10]],
        "competitor_seeds": [c["entity_name"] for c in competitors],
        "question_seeds": [],
        "reason": "规则兜底:按被引次数与 AI 提及次数取头部,未做针对性筛选",
    }


def _build_prompt(
    brand_name: str, industry: str, city: str,
    corpus: list[dict[str, Any]], entities: list[dict[str, Any]],
) -> str:
    return (
        f"有一家新客户要做 AI 搜索可见度诊断:\n"
        f"- 品牌:{brand_name}\n- 行业:{industry}\n"
        + (f"- 主要市场:{city}\n" if city else "")
        + "\n我们手上已经有这些沉淀,请判断哪些对这家客户直接可用。\n\n"
        "【已被 AI 引用过、且判定可复用的行业语料】\n"
        + json.dumps(corpus, ensure_ascii=False, indent=2)
        + "\n\n【AI 回答里真实出现过的同行实体】\n"
        "(mentions = 被 AI 提及次数,best_rank = 最好的一次推荐位次,数字均为系统统计)\n"
        + json.dumps(entities, ensure_ascii=False, indent=2)
        + "\n\n请给出:\n"
        "1. corpus_article_ids:上面语料里这家客户用得上的 article_id(只能从给定列表里挑);\n"
        "2. competitor_seeds:值得作为竞品去挖的实体名(只能从给定实体里挑,"
        "**必须排除客户自己**,也要排除明显不是同行的词,比如平台名、泛称、地名);\n"
        "3. question_seeds:2-5 条这家客户的潜在买家真会去问的问题。"
        "注意问题要描述**买家在找什么**,不是描述这家公司自己;"
        "不要把行业名录、地址、公司全称原样拼进问题里;\n"
        "4. reason:一句话说明为什么这么选。\n\n"
        '严格只输出 JSON:{"corpus_article_ids": [1], "competitor_seeds": ["A"], '
        '"question_seeds": ["..."], "reason": "..."}'
    )


def suggest_reusable_material(
    *, brand_name: str, industry: str, city: str = "",
    industry_key: str = "", corpus_limit: int = DEFAULT_CORPUS_LIMIT,
    entity_limit: int = DEFAULT_ENTITY_LIMIT,
) -> dict[str, Any]:
    """给一家新客户挑可复用素材。**永远返回可用结果**,且永远是 advisory。"""
    key = industry_key or ""
    if not key:
        try:
            from services.media_entity_flywheel import normalize_industry_key

            key = normalize_industry_key(industry or "general")
        except Exception:
            key = str(industry or "general")

    try:
        corpus = _corpus_candidates(key, corpus_limit)
    except Exception as exc:
        logger.warning("[B4] 语料候选查询失败: %s", exc)
        corpus = []
    try:
        entities = _competitor_candidates(key, entity_limit)
    except Exception as exc:
        logger.warning("[B4] 竞品实体候选查询失败: %s", exc)
        entities = []

    if not corpus and not entities:
        return {
            "advisory": True, "industry_key": key, "source": "rule",
            "corpus_article_ids": [], "competitor_seeds": [], "question_seeds": [],
            "reason": "该行业尚无可复用沉淀(语料与实体都为空)",
            "candidates": {"corpus": 0, "entities": 0},
        }

    allowed_ids = {int(c["article_id"]) for c in corpus if c.get("article_id")}
    allowed_names = {str(e["entity_name"]).strip() for e in entities}

    from services.flywheel_judgment import judge

    def _validate(payload: Any) -> bool:
        return isinstance(payload, dict) and (
            isinstance(payload.get("corpus_article_ids"), list)
            or isinstance(payload.get("competitor_seeds"), list)
        )

    result = judge(
        POINT_KEY,
        prompt=_build_prompt(brand_name, industry, city, corpus, entities),
        rule_fallback=lambda: _rule_material(brand_name, industry, corpus, entities),
        input_summary={
            "industry_key": key, "brand": brand_name,
            "corpus_candidates": len(corpus), "entity_candidates": len(entities),
        },
        validate=_validate,
    )

    payload = result.payload if isinstance(result.payload, dict) else {}
    # 越界一律丢弃:模型只准从候选里挑,不准发明语料 id 或竞品名。
    picked_ids = []
    for value in payload.get("corpus_article_ids") or []:
        try:
            article_id = int(value)
        except (TypeError, ValueError):
            continue
        if article_id in allowed_ids and article_id not in picked_ids:
            picked_ids.append(article_id)

    self_name = str(brand_name or "").strip()
    picked_names = []
    for value in payload.get("competitor_seeds") or []:
        name = str(value or "").strip()
        # 兜底结果里的名字也来自候选集,这条过滤对两种来源同样成立。
        if name and name in allowed_names and name != self_name and name not in picked_names:
            picked_names.append(name)

    question_seeds = [
        str(q).strip()[:120] for q in (payload.get("question_seeds") or [])
        if str(q or "").strip()
    ][:5]

    return {
        "advisory": True,
        "industry_key": key,
        "source": result.source,
        "provider": result.provider,
        "model": result.model,
        "fallback_reason": result.fallback_reason,
        "corpus_article_ids": picked_ids,
        "competitor_seeds": picked_names,
        "question_seeds": question_seeds,
        "reason": str(payload.get("reason") or "")[:300],
        "candidates": {"corpus": len(corpus), "entities": len(entities)},
    }
