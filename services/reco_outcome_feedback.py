"""P1-5b · D6-B 推荐结果回流(2026-08-14)。

按「品牌 × 问题族 × 引擎 × 时窗」聚合两个事实源:
  - `monitoring_results`(十分类 target_outcome,经 aggregate_eligible 谓词);
  - `geo_article_citation_attributions` 账本(body_proof 的真实被引)。
并回喂三个消费点(全部 advisory,LLM 只解释、不产数):
  1. 标题缺口 → `build_title_gap_block`(P1-1:选题 prompt 新输入段);
  2. 主优势/引擎认知 → `reco_feedback_for_lineage`(D6-A lineage 预留字段
     `reco_feedback` / `engine_recognition_state` 的供给方);
  3. 媒体组合 → `brand_media_feedback`(发布推荐面:该品牌真实被引的发布域)。

🔴 红线桥限定语(工单红线 6,不许剥离):本模块的分母走三条**红线桥**——
  J4 发布桥配对率 5.0% / J8 identity 1.5% / J9 target_outcome 有效解析 1.5%
  (研究定稿 join_bridge_audit)。**所有产出只代表「已配对子样本」,不得外推
  全量**;覆盖率由 P0-3 回填拉起后,同一套聚合自然变厚 —— 机制先建全,
  数据上来自然生效。样本不足(< min_sample)时诚实返回不可用/空块,
  绝不硬凑(A1:降级 = 现行为)。

分类:O1 —— 任何查询失败都返回空/不可用,绝不阻断选题/写作/发布主链。
样本量阈值走配置(GEO_D6B_MIN_SAMPLE,默认 30 —— 工单 §四:数值可由
Owner 拍板调整,机制不动)。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Final

logger = logging.getLogger("GEO-RecoOutcomeFeedback")

D6B_VERSION: Final = "reco-feedback-v1.0"

#: 十分类 → 三档(与 services/monitoring_lineage.TARGET_OUTCOMES 同源;
#: 分档口径抄 services/customer_operation_plan.py 的既有聚合,不另起炉灶)。
#: 🔴 分母 = **八个业务分类的白名单**,不是黑名单排除:legacy 行的
#: target_outcome 是哨兵字符串 'legacy_unknown'(DDL 默认值,非 NULL),
#: 黑名单口径会把 9 万条未解析行全算进观测分母(构造用例实证)。
_RECOMMENDED_OUTCOMES: Final = ("recommended", "conditionally_recommended")
_MENTIONED_OUTCOMES: Final = ("mentioned_only", "candidate_only")
_VALID_OUTCOMES: Final = (
    "recommended", "conditionally_recommended", "candidate_only", "mentioned_only",
    "criteria_only", "refused_no_evidence", "refused_risk", "not_mentioned",
)


def min_sample() -> int:
    """样本量阈值(<该值显示「暂无足够样本」)。机制常开,数值走配置。"""
    try:
        value = int(os.getenv("GEO_D6B_MIN_SAMPLE", "30"))
    except (TypeError, ValueError):
        value = 30
    return max(1, value)


def aggregate_brand_outcomes(brand_id: int, *, window_days: int = 90) -> dict[str, Any]:
    """品牌 × 问题族 × 引擎 × 时窗聚合。纯代码算,无 LLM。

    返回::

        {"available": bool, "version", "window_days", "min_sample",
         "total_observations": int,          # 已配对子样本(见模块头红线桥限定)
         "families": [{question_family, provider, observations,
                       recommended, mentioned, not_mentioned}...],
         "citations": [{question_family, provider, publish_domain, citations}...],
         "coverage_note": str}               # 红线桥限定语,随数据下发
    """
    base: dict[str, Any] = {
        "available": False,
        "version": D6B_VERSION,
        "window_days": int(window_days),
        "min_sample": min_sample(),
        "total_observations": 0,
        "families": [],
        "citations": [],
        "coverage_note": (
            "口径:仅已配对子样本(target_outcome 有效解析/发布桥配对的行),"
            "不代表全量监测面;覆盖率随 P0-3 回填提升。"
        ),
    }
    if not brand_id:
        return base
    try:
        from db.connection import get_db
        from services.monitoring_identity_review import aggregate_eligible_sql

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT COALESCE(r.question_family, '(unknown)') AS question_family,
                       COALESCE(r.provider, '(unknown)')        AS provider,
                       COUNT(*) FILTER (WHERE r.target_outcome IN %s) AS observations,
                       COUNT(*) FILTER (WHERE r.target_outcome IN %s) AS recommended,
                       COUNT(*) FILTER (WHERE r.target_outcome IN %s) AS mentioned,
                       COUNT(*) FILTER (WHERE r.target_outcome = 'not_mentioned') AS not_mentioned
                  FROM monitoring_results r
                  JOIN monitoring_tasks t ON t.id = r.task_id
                 WHERE t.brand_id = %s
                   AND r.tested_at >= NOW() - make_interval(days => %s)
                   AND {aggregate_eligible_sql('r')}
                 GROUP BY 1, 2
                """,
                (_VALID_OUTCOMES, _RECOMMENDED_OUTCOMES, _MENTIONED_OUTCOMES,
                 int(brand_id), int(window_days)),
            )
            families = [dict(row) for row in cur.fetchall() or []]

            cur.execute(
                """
                SELECT COALESCE(question_family, '(unknown)') AS question_family,
                       COALESCE(provider, '(unknown)')        AS provider,
                       publish_domain,
                       COUNT(*) AS citations
                  FROM geo_article_citation_attributions
                 WHERE brand_id = %s
                   AND tested_at >= NOW() - make_interval(days => %s)
                   AND body_proof
                 GROUP BY 1, 2, 3
                """,
                (int(brand_id), int(window_days)),
            )
            citations = [dict(row) for row in cur.fetchall() or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[D6-B] 品牌 %s 回流聚合失败(按不可用处理): %s", brand_id, str(exc)[:200])
        return base

    total = sum(int(row.get("observations") or 0) for row in families)
    base.update({
        "available": True,
        "total_observations": total,
        "families": families,
        "citations": citations,
    })
    return base


def build_title_gap_block(brand_id: int, *, window_days: int = 90) -> str:
    """[P1-1 消费点] 问题缺口 → 选题 prompt 输入段。

    数据不足(总有效观测 < min_sample)→ 返回 ``""``,prompt 与现行为逐字一致
    (A1:缺数据按现行为降级)。缺口 = 有效观测里品牌未被提及占比高的问题族。
    段尾带「打不动退回选词」出口(08-07 七步第 4 步):缺口只是参考,
    打不动的族退回按已确认关键词选题,不逼选题硬攻。
    """
    agg = aggregate_brand_outcomes(brand_id, window_days=window_days)
    if not agg["available"] or agg["total_observations"] < agg["min_sample"]:
        return ""

    by_family: dict[str, dict[str, int]] = {}
    for row in agg["families"]:
        fam = str(row.get("question_family") or "(unknown)")
        slot = by_family.setdefault(fam, {"observations": 0, "recommended": 0, "not_mentioned": 0})
        slot["observations"] += int(row.get("observations") or 0)
        slot["recommended"] += int(row.get("recommended") or 0)
        slot["not_mentioned"] += int(row.get("not_mentioned") or 0)

    gaps = []
    wins = []
    for fam, s in by_family.items():
        if fam == "(unknown)" or s["observations"] < 5:
            continue  # 单族样本太小不下结论(只有分子的比较是空的)
        if s["recommended"] == 0 and s["not_mentioned"] * 2 >= s["observations"]:
            gaps.append((fam, s))
        elif s["recommended"] * 2 >= s["observations"]:
            wins.append((fam, s))
    if not gaps and not wins:
        return ""

    lines = [
        "## 监测缺口参考(真实监测已配对子样本 · advisory)",
        "以下按本品牌近 90 天监测的**已配对子样本**统计(不代表全量监测面),"
        "只作选题参考,不是硬指标:",
    ]
    for fam, s in sorted(gaps, key=lambda kv: -kv[1]["observations"])[:5]:
        lines.append(
            f"- 缺口族「{fam}」:{s['observations']} 次有效观测中品牌未被提及 "
            f"{s['not_mentioned']} 次、被推荐 0 次 —— 该族问题值得优先出题。"
        )
    for fam, s in sorted(wins, key=lambda kv: -kv[1]["recommended"])[:3]:
        lines.append(
            f"- 优势族「{fam}」:被推荐 {s['recommended']}/{s['observations']} —— "
            f"该族已打下,不必重复堆题。"
        )
    lines.append(
        "🔴 出口:缺口只是参考。若某缺口族与已确认关键词的业务对象不匹配"
        "(打不动),**退回按关键词本身选题**,不得为攻缺口而脱离客户确认的关键词。"
    )
    return "\n".join(lines)


def reco_feedback_for_lineage(brand_id: int, *, window_days: int = 90) -> dict[str, Any] | None:
    """[D6-A lineage 消费点] 填 `reco_feedback` / `engine_recognition_state` 预留字段。

    样本不足 → ``None``(与预留字段的既有值一致 = 行为不变,诚实"还没有数据")。
    """
    agg = aggregate_brand_outcomes(brand_id, window_days=window_days)
    if not agg["available"] or agg["total_observations"] < agg["min_sample"]:
        return None
    engine_state: dict[str, dict[str, int]] = {}
    for row in agg["families"]:
        provider = str(row.get("provider") or "(unknown)")
        slot = engine_state.setdefault(provider, {"observations": 0, "recommended": 0})
        slot["observations"] += int(row.get("observations") or 0)
        slot["recommended"] += int(row.get("recommended") or 0)
    return {
        "version": D6B_VERSION,
        "window_days": int(window_days),
        "total_observations": agg["total_observations"],
        "coverage_note": agg["coverage_note"],
        "engine_recognition_state": engine_state,
        "cited_domains": sorted({
            str(row.get("publish_domain") or "")
            for row in agg["citations"] if row.get("publish_domain")
        })[:12],
    }


def min_distinct_domains() -> int:
    """[R2-6] 多样性门槛:可用结论至少要有几个**不同发布域**(默认 2,配置可调)。"""
    try:
        value = int(os.getenv("GEO_D6B_MIN_DOMAINS", "2"))
    except (TypeError, ValueError):
        value = 2
    return max(1, value)


def min_distinct_providers() -> int:
    """[R2-6] 多样性门槛:可用结论至少要有几个**不同引擎**(默认 2,配置可调)。"""
    try:
        value = int(os.getenv("GEO_D6B_MIN_PROVIDERS", "2"))
    except (TypeError, ValueError):
        value = 2
    return max(1, value)


def brand_media_feedback(brand_id: int, *, window_days: int = 90) -> dict[str, Any]:
    """[媒体组合消费点] 该品牌真实被引的发布域(发布推荐面 advisory 区块)。

    🔴 [R2-6 2026-08-15] 可用门槛 = **min_sample + 多样性**三条同时满足:
      被引总数 ≥ min_sample(默认 30)∧ 不同发布域 ≥ 2 ∧ 不同引擎 ≥ 2。
    旧口径只判 ==0 —— 生产实证两个品牌各 4/5 次被引、各 1 个域名,照样
    「优先续投」(R2 §0① 反例)。不足 → available=False + 计数原样带回
    (前端显示「4/30,暂作观察」+ 继续积累 / 人工查看原始记录两出口),
    **绝不给续投结论**。
    """
    agg = aggregate_brand_outcomes(brand_id, window_days=window_days)
    if not agg["available"]:
        return {"available": False, "reason": "aggregation_unavailable", "version": D6B_VERSION}
    citation_total = sum(int(row.get("citations") or 0) for row in agg["citations"])
    domains: dict[str, dict[str, Any]] = {}
    provider_set: set[str] = set()
    for row in agg["citations"]:
        dom = str(row.get("publish_domain") or "")
        if not dom:
            continue
        slot = domains.setdefault(dom, {"domain": dom, "citations": 0, "providers": set()})
        slot["citations"] += int(row.get("citations") or 0)
        slot["providers"].add(str(row.get("provider") or "(unknown)"))
        provider_set.add(str(row.get("provider") or "(unknown)"))
    gate_counts = {
        "citation_total": citation_total,
        "min_sample": agg["min_sample"],
        "distinct_domains": len(domains),
        "min_distinct_domains": min_distinct_domains(),
        "distinct_providers": len(provider_set),
        "min_distinct_providers": min_distinct_providers(),
    }
    if (
        citation_total < agg["min_sample"]
        or len(domains) < min_distinct_domains()
        or len(provider_set) < min_distinct_providers()
    ):
        return {
            "available": False, "reason": "insufficient_sample", "version": D6B_VERSION,
            "window_days": int(window_days),
            "coverage_note": agg["coverage_note"],
            **gate_counts,
        }
    ranked = sorted(domains.values(), key=lambda d: -d["citations"])[:10]
    for slot in ranked:
        slot["providers"] = sorted(slot["providers"])
    return {
        "available": True,
        "version": D6B_VERSION,
        "window_days": int(window_days),
        "coverage_note": agg["coverage_note"],
        "domains": ranked,
        **gate_counts,
    }
