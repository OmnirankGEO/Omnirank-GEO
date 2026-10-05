"""B5 写作策略选择:接在 A4 指派管道之上(2026-07-29)。

工单原话:"在 A4 管道之上,由 v4-flash 选文体/角度/取材,结果进 assignments,
被引回流进 outcome_events —— 真闭环第一次跑通。"

所以这里**只做一件事**:在已经把候选算清楚之后,挑一个 style_family 并给出取材角度。
候选集合、被引统计、写库、幂等键全部仍由代码负责:
  - 候选 = 六文体家族契约(`writing.article_style_contract`),模型选不出契约外的家族;
  - 每个候选的历史被引表现由代码从 `writing_strategy_outcome_events` +
    `writing_article_version_fingerprints` 算出来(这正是 A4 管道回流的那份数据 —— 闭环在此合拢);
  - 结果写进 `writing_strategy_assignments.metadata`,**不改 strategy_id 语义**
    (指派的是哪个策略版本仍由 A4 的 active 版本决定,模型无权激活版本);
  - advisory:选择只作为写作参考落账,不阻断任何生成;模型不可用 → 规则兜底选被引最好的家族。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-FlywheelStyleChoice")

POINT_KEY = "writing_strategy_selection"


def _style_families() -> list[str]:
    """六文体家族契约 = 候选全集(SSOT 在 `writing.article_style_contract`,这里不另起一套)。

    读不到时返回空 —— 上游据此跳过选择,保持既定家族不变。"""
    try:
        from writing.article_style_contract import USER_CHOICE_FAMILY_CODES

        return [str(code) for code in USER_CHOICE_FAMILY_CODES]
    except Exception as exc:
        logger.warning("[B5] 读文体家族契约失败: %s", exc)
        return []


def family_citation_stats(industry_key: str) -> dict[str, dict[str, Any]]:
    """各文体家族的历史被引表现(代码算,不让模型编)。

    数据来源就是 A4 回流出来的那张表:指派 → 文章 → 发布 → 被引。
    A4 上线前这里全是 0,属于诚实的"还没有数据",不是故障。
    """
    from db.connection import get_connection

    stats: dict[str, dict[str, Any]] = {}
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COALESCE(a.style_family, '(unknown)') AS style_family,
                       COUNT(DISTINCT a.id)                  AS assignments,
                       COALESCE(SUM(e.ai_citations_delta_30d), 0) AS citations,
                       COUNT(e.id) FILTER (WHERE e.publish_status = 'measured') AS measured_events
                  FROM writing_strategy_assignments a
                  LEFT JOIN writing_strategy_outcome_events e
                         ON e.quote_id = a.quote_id
                        AND e.industry_key = a.industry_key
                 WHERE a.industry_key = %s
                 GROUP BY COALESCE(a.style_family, '(unknown)')
                """,
                (industry_key or "general",),
            )
            for row in cur.fetchall() or []:
                assignments = int(row["assignments"] or 0)
                citations = int(row["citations"] or 0)
                stats[str(row["style_family"])] = {
                    "assignments": assignments,
                    "citations": citations,
                    "measured_events": int(row["measured_events"] or 0),
                    "citations_per_assignment": (
                        round(citations / assignments, 3) if assignments else 0.0
                    ),
                }
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[B5] 文体被引统计查询失败(按无数据处理): %s", exc)
    return stats


def _rule_choice(families: list[str], stats: dict[str, dict[str, Any]],
                 default_family: Optional[str]) -> dict[str, Any]:
    """规则兜底:挑被引密度最高的家族;完全没数据就沿用调用方的既定家族(= 改前行为)。"""
    ranked = sorted(
        (f for f in families if stats.get(f, {}).get("assignments")),
        key=lambda f: stats[f]["citations_per_assignment"],
        reverse=True,
    )
    if ranked and stats[ranked[0]]["citations"] > 0:
        return {
            "style_family": ranked[0],
            "angle": "",
            "reason": f"规则兜底:该行业里 {ranked[0]} 的历史被引密度最高",
        }
    return {
        "style_family": default_family or "",
        "angle": "",
        "reason": "规则兜底:尚无被引数据,沿用既定文体家族",
    }


def apply_writing_style_choice(
    topics: list[dict[str, Any]],
    style_choice: Optional[dict[str, Any]],
    industry: Optional[str],
    *,
    can_apply: bool = True,
) -> dict[str, Any]:
    """[工单 B 2026-07-27] B5 真接管:把选出来的文体家族落到 auto 档的 topic 上。

    改前 `choose_writing_strategy` 的结果只进 `assignments.metadata`,下游无人消费 ——
    判断点开了也是空转。这个函数就是那一档"真消费",三条边界一条都不能少:

      1. **只吃 auto 档**。前端显式选过、或 DB 持久化过文体的 topic 一律不碰
         (用户 > AI,这是产品口径不是实现细节);
      2. **只认 source=llm**。规则兜底(含判断点关闸)一律不接管 —— 于是
         "关闸 = 与改前逐字节一致"是结构性成立的,不靠测试保证;
      3. **家族必须能过 `resolve_user_choice(family, industry)`**。否则下游
         `_allocate_style_from_ratios` 会按既定口径 raise,把整批生成判死。
         飞轮无权判死一批文章:选不出合法家族就原样退回 ratio 抽签。

    [P1-5a 止血护栏 2026-08-14] 第 4 条边界 —— **数据闸**:
      `can_apply`(调用方从 structure guidance 取的数据置信位)为 False =
      本行业**没有被引数据**支撑任何文体选择。此时 LLM 的选择只是意图揣测
      (生产实测 4/10 批次、37 个 topic 被零数据选择实改文体,理由清一色
      「虽无被引但意图匹配度最高」,而 outcome_events 恒 0 → 偏差不可自纠,
      研究定稿 §12)。闸上 = 不应用、退回既有 ratio 抽签,零用户面变化(O1);
      `can_apply` 转真值后闸自然打开 —— 止血与 D6-B 回流是同一开关两侧。
      默认 True:不传 = 维持旧签名行为,生产调用方必须显式传真实值。

    原地改 `topics`(与调用点其余装配一致),返回落账用的摘要。
    """
    from db.flywheel_judgment_db import SOURCE_LLM

    if not isinstance(style_choice, dict):
        return {"family": "", "applied_topics": 0, "skip_reason": "choice_unavailable"}
    if style_choice.get("source") != SOURCE_LLM:
        return {
            "family": "", "applied_topics": 0,
            "skip_reason": f"source={style_choice.get('source')}",
        }
    if not can_apply:
        # [P1-5a] 数据闸:无被引数据时 LLM 选择不落地,topic 保持 auto → ratio 抽签。
        return {"family": "", "applied_topics": 0, "skip_reason": "no_citation_data_guard"}

    family = str(style_choice.get("style_family") or "")
    if not family:
        return {"family": "", "applied_topics": 0, "skip_reason": "empty_family"}
    try:
        from writing.style_registry import resolve_user_choice

        if not resolve_user_choice(family, industry):
            return {"family": "", "applied_topics": 0, "skip_reason": "family_not_resolvable"}
    except Exception as exc:
        # 医疗/法律 hard rule 等既定红线在这里生效:飞轮让路,不越过产品硬规则。
        logger.warning("[B5] 家族 %s 不适用于行业 %s,退回 ratio 抽签: %s", family, industry, exc)
        return {"family": "", "applied_topics": 0, "skip_reason": f"rejected:{type(exc).__name__}"}

    applied = 0
    for topic in topics:
        if topic.get("user_choice") == "auto":
            topic["user_choice"] = family
            topic["_style_from_flywheel"] = True
            applied += 1
    return {
        "family": family if applied else "",
        "applied_topics": applied,
        "skip_reason": None if applied else "no_auto_topic",
    }


def choose_writing_strategy(
    *,
    industry_key: str,
    default_family: Optional[str] = None,
    titles: Optional[list[str]] = None,
) -> dict[str, Any]:
    """给一个项目选文体家族 + 取材角度。返回 dict,永远可用(兜底保证)。

    返回:{style_family, angle, reason, source, provider, model, fallback_reason, candidates}
    """
    families = _style_families()
    if not families:
        return {
            "style_family": default_family or "", "angle": "", "reason": "文体契约不可读,不做选择",
            "source": "rule", "fallback_reason": "style_contract_unavailable",
        }

    stats = family_citation_stats(industry_key)
    from services.flywheel_judgment import judge

    candidate_rows = [
        {"style_family": f, **(stats.get(f) or {
            "assignments": 0, "citations": 0, "measured_events": 0,
            "citations_per_assignment": 0.0,
        })}
        for f in families
    ]
    prompt = (
        "你在为一个 B 端品牌的 GEO 文章项目挑写作方向。下面是本行业各文体家族的历史被引表现"
        "(系统算好的真实数字,不要改动或自行推算):\n\n"
        + json.dumps(candidate_rows, ensure_ascii=False, indent=2)
        + f"\n\n行业:{industry_key}\n"
        + ("本批标题示例:\n- " + "\n- ".join((titles or [])[:8]) + "\n" if titles else "")
        + "\n判断要求:\n"
        "1. 只能从上面的 style_family 里选一个,不要发明新家族;\n"
        "2. 有被引数据时优先看 citations_per_assignment;数据量太小(assignments 很少)时"
        "以标题意图为主,不要被个位数样本带偏;\n"
        "3. angle 用一句话说清取材角度,例如:多用可核对的行业数据;或围绕选购决策链路展开。"
        "不要写成效果承诺,不要提任何具体排名或收益数字。\n\n"
        '严格只输出 JSON:{"style_family": "...", "angle": "...", "reason": "一句话理由"}'
    )

    def _validate(payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and str(payload.get("style_family") or "") in set(families)
        )

    result = judge(
        POINT_KEY,
        prompt=prompt,
        rule_fallback=lambda: _rule_choice(families, stats, default_family),
        input_summary={
            "industry_key": industry_key,
            "candidates": len(families),
            "families_with_data": sum(1 for f in families if stats.get(f, {}).get("assignments")),
            "title_sample": len(titles or []),
        },
        validate=_validate,
    )

    payload = result.payload if isinstance(result.payload, dict) else {}
    chosen = str(payload.get("style_family") or "")
    if chosen not in set(families):
        # 兜底结果也可能是空(完全没数据),此时保持调用方既定家族,不硬塞。
        chosen = default_family or ""
    return {
        "style_family": chosen,
        "angle": str(payload.get("angle") or "")[:300],
        "reason": str(payload.get("reason") or "")[:300],
        "source": result.source,
        "provider": result.provider,
        "model": result.model,
        "fallback_reason": result.fallback_reason,
        "candidates": candidate_rows,
    }
