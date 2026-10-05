"""B1 采集选题:下一轮采哪些行业 / 问题(2026-07-29)。

修前:`get_active_plan()` 把**全部** active 行业 × 全部 active prompts 一股脑塞进快照
(生产实测 18 行业 / 409 问题),等于静态题库轮着跑,既不看历史被引产出率,
也不看客户行业分布,更不看覆盖缺口。

改后:代码先把三类**事实**算好(被引产出率 / 客户行业分布 / 距上次采集的天数),
再让 v4-flash 在这些事实上做取舍,产出本轮题目集 + 理由。

三条边界写死在这里:
  - **统计口径由代码算**(产出率、客户数、覆盖缺口),模型只排序取舍 —— 防编数;
  - **只做减法与排序,不凭空造题**:模型只能从 active 题库里挑,挑出题库外的一律丢弃;
  - **advisory**:选题结果只影响本轮采哪些,不改任何 active 标记、不写 research 域。
    模型不可用 / 超预算 / 输出不合规 → 规则兜底(见 `_rule_plan`),行为退回改前口径。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-FlywheelTopic")

POINT_KEY = "research_topic_selection"

# 一轮最多带多少个行业。规则兜底不设限(保持改前行为),只有 LLM 选题时用它约束提问规模。
DEFAULT_MAX_INDUSTRIES = 12


def _industry_facts(industry_ids: list[int]) -> dict[int, dict[str, Any]]:
    """每个行业的三类事实。全部只读,查询失败返空 dict(上游据此走兜底)。

    SQL 4 维核验(生产 `\\d` 逐列对过,2026-07-29):
      1. 列名:行业维度挂在 **`geo_research_article_citations.industry_id`** 上;
         `geo_research_articles` **没有** industry_id(只有自由文本 `primary_industry`),
         也**没有** created_at(时间列是 fetched_at / last_seen_at)。所以产出率与覆盖缺口
         都从 citations 表取,再 join articles 只为过滤 tombstone。
      2. data_type:industry_id BIGINT(与 geo_research_industries.id 同型)· cited_at TIMESTAMPTZ
      3. 字段归属:domain_tier / clean_status 属 articles;industry_id / round_id / cited_at 属 citations
      4. 只读:三条全是 SELECT,research 域一行不写
    """
    if not industry_ids:
        return {}
    from db.connection import get_connection

    facts: dict[int, dict[str, Any]] = {i: {
        "cited_articles": 0, "citations": 0, "citations_per_cited_article": 0.0,
        "rounds_touched": 0, "brands": 0, "days_since_last_citation": None,
    } for i in industry_ids}
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            # ① 历史被引产出 + 覆盖缺口(排除 tombstone / 黑名单域,口径与效果回流一致)
            cur.execute(
                """
                SELECT c.industry_id                                        AS industry_id,
                       COUNT(DISTINCT c.article_id)                         AS cited_articles,
                       COUNT(c.id)                                          AS citations,
                       COUNT(DISTINCT c.round_id)                           AS rounds_touched,
                       EXTRACT(EPOCH FROM (NOW() - MAX(c.cited_at))) / 86400.0 AS days_since
                  FROM geo_research_article_citations c
                  JOIN geo_research_articles gra
                       ON gra.id = c.article_id
                      AND gra.domain_tier <> 'blacklist'
                      AND gra.clean_status = 'cleaned'
                 WHERE c.industry_id = ANY(%s)
                 GROUP BY c.industry_id
                """,
                (industry_ids,),
            )
            for row in cur.fetchall() or []:
                key = int(row["industry_id"])
                if key not in facts:
                    continue
                cited_articles = int(row["cited_articles"] or 0)
                citations = int(row["citations"] or 0)
                facts[key].update({
                    "cited_articles": cited_articles,
                    "citations": citations,
                    "citations_per_cited_article": (
                        round(citations / cited_articles, 3) if cited_articles else 0.0
                    ),
                    "rounds_touched": int(row["rounds_touched"] or 0),
                    "days_since_last_citation": (
                        round(float(row["days_since"]), 1) if row["days_since"] is not None else None
                    ),
                })

            # ② 客户行业分布。brands.industry 是自由文本,只能按行业名近似匹配 ——
            #    这是**近似值**,prompt 里也如实这么说,不许模型当成精确客户数去推结论。
            cur.execute(
                """
                SELECT i.id AS industry_id, COUNT(b.id) AS brands
                  FROM geo_research_industries i
                  LEFT JOIN brands b
                         ON b.industry IS NOT NULL
                        AND btrim(b.industry) <> ''
                        AND (b.industry = i.name OR b.industry ILIKE '%%' || i.name || '%%')
                 WHERE i.id = ANY(%s)
                 GROUP BY i.id
                """,
                (industry_ids,),
            )
            for row in cur.fetchall() or []:
                key = int(row["industry_id"])
                if key in facts:
                    facts[key]["brands"] = int(row["brands"] or 0)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[B1] 选题事实查询失败(走兜底): %s", exc)
        return {}
    return facts


def _rule_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """规则兜底 = 改前行为:全部 active 行业照单全收,不做任何取舍。"""
    return {
        "selected_industry_ids": [int(i["id"]) for i in plan.get("industries") or []],
        "reasons": {},
        "source_note": "规则兜底:按全部 active 行业跑(与改前行为一致)",
    }


def _build_prompt(plan: dict[str, Any], facts: dict[int, dict[str, Any]], max_industries: int) -> str:
    rows = []
    for industry in plan.get("industries") or []:
        iid = int(industry["id"])
        fact = facts.get(iid) or {}
        rows.append({
            "industry_id": iid,
            "industry_name": industry.get("name"),
            "prompt_count": len(plan.get("prompts_by_industry", {}).get(iid) or []),
            "cited_articles": fact.get("cited_articles"),
            "citations": fact.get("citations"),
            "citations_per_cited_article": fact.get("citations_per_cited_article"),
            "rounds_touched": fact.get("rounds_touched"),
            "our_brands_in_industry_approx": fact.get("brands"),
            "days_since_last_citation": fact.get("days_since_last_citation"),
        })
    return (
        "你在给一个 AI 搜索可见度平台安排下一轮行业调研采集。下面是每个候选行业的真实统计"
        "(全部由系统算好,不要自己推算、换算或修改这些数字):\n\n"
        + json.dumps(rows, ensure_ascii=False, indent=2)
        + "\n\n字段口径:\n"
        "- cited_articles / citations:该行业历史上被 AI 引用过的文章数与引用次数;\n"
        "- citations_per_cited_article:被引密度,越高说明这个行业采回来的语料越容易被引用;\n"
        "- rounds_touched:该行业被采集过多少轮;\n"
        "- our_brands_in_industry_approx:我方客户数的**近似值**(按行业名模糊匹配得来,"
        "不是精确数字,只能当强弱信号用);\n"
        "- days_since_last_citation:距最近一次被引过了多少天,null = 历史上从没被引过(覆盖缺口)。\n\n"
        "请挑出本轮最值得采集的行业,最多 " + str(max_industries) + " 个。判断依据(按重要性递减):\n"
        "1. 我方客户多的行业优先;\n"
        "2. 被引密度高的行业优先(采回来的语料真的被引用);\n"
        "3. 太久没被引 / 从没采过的行业要补缺口;\n"
        "4. 采了很多轮但几乎没有引用的行业可以降位。\n\n"
        "只能从上面列出的 industry_id 里挑,不要发明新行业。\n"
        '严格只输出 JSON,格式:{"selected_industry_ids": [1,2], "reasons": {"1": "一句话理由"}}'
    )


def select_round_topics(
    plan: dict[str, Any], *, max_industries: int = DEFAULT_MAX_INDUSTRIES
) -> dict[str, Any]:
    """给一轮采集挑题。返回**过滤后的 plan**(结构与入参一致,可直接喂给 create_round_with_snapshot)。

    永远返回可用的 plan:判断点没开 / 超预算 / 模型全挂 / 输出不合规 → 原样返回全量 plan。
    """
    industries = plan.get("industries") or []
    if len(industries) <= 1:
        return plan

    industry_ids = [int(i["id"]) for i in industries]
    facts = _industry_facts(industry_ids)

    from services.flywheel_judgment import judge

    def _validate(payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and isinstance(payload.get("selected_industry_ids"), list)
            and len(payload["selected_industry_ids"]) > 0
        )

    result = judge(
        POINT_KEY,
        prompt=_build_prompt(plan, facts, max_industries),
        rule_fallback=lambda: _rule_plan(plan),
        input_summary={
            "candidate_industries": len(industries),
            "total_prompts": sum(len(v or []) for v in (plan.get("prompts_by_industry") or {}).values()),
            "max_industries": max_industries,
        },
        validate=_validate,
    )

    selected_raw = (result.payload or {}).get("selected_industry_ids") or []
    # 题库外的一律丢弃 —— 模型只准做减法,不准造题。
    allowed = set(industry_ids)
    selected: list[int] = []
    for value in selected_raw:
        try:
            iid = int(value)
        except (TypeError, ValueError):
            continue
        if iid in allowed and iid not in selected:
            selected.append(iid)
    selected = selected[:max_industries]

    if not selected:
        logger.warning("[B1] 选题结果为空或全部越界,退回全量 plan")
        return plan

    filtered_industries = [i for i in industries if int(i["id"]) in set(selected)]
    filtered_prompts = {
        iid: prompts
        for iid, prompts in (plan.get("prompts_by_industry") or {}).items()
        if int(iid) in set(selected)
    }
    if not filtered_industries or not any(filtered_prompts.values()):
        # 挑出来的行业一个 prompt 都没有 = 会把一轮跑空,宁可退回全量。
        logger.warning("[B1] 选题后无可用 prompts,退回全量 plan")
        return plan

    logger.info(
        "[B1] 采集选题(%s):%d/%d 个行业 · %s",
        result.source, len(filtered_industries), len(industries),
        [i.get("name") for i in filtered_industries],
    )
    return {
        "industries": filtered_industries,
        "prompts_by_industry": filtered_prompts,
        "topic_selection": {
            "source": result.source,
            "provider": result.provider,
            "model": result.model,
            "fallback_reason": result.fallback_reason,
            "reasons": (result.payload or {}).get("reasons") or {},
            "candidate_industry_count": len(industries),
        },
    }
