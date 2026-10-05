"""投放结果事实表 builder(报价升级返工前置 · Review NO-GO 2026-08-09)。

## 为什么需要这个模块

2026-08-09 报价升级调研被 Review 判 NO-GO,五条 P0 里四条同源:
**把不同粒度、不同可观察条件的事实混算成了一个「有效率」**。本模块把它们拆开落成两张
只存计数的事实表(迁移 032),让每个比率在查询侧现算、且必须显式写出分母。

## 与 `article_attribution_ledger` 的分工(别搞混,两者都要)

| | 严格账本 `geo_article_citation_attributions` | 本模块 `geo_publication_outcome_facts` |
|---|---|---|
| 记什么 | **只记确认命中**(分子) | 记**全部窗口内监测**(分子 + 各种候选分母) |
| 证据强度 | 严格:要 body_proof / lineage 完整 | 宽:只要监测行在窗口内就记,证据强弱用列区分 |
| 用途 | 对客户/对外的**归因证据** | 对内的**统计底座**(算率、定价、诊断) |
| 现状 | 83 行(被 lineage_status='legacy_unknown' 卡住) | 本模块产出 |

账本回答「这篇文章确实带来了这次推荐吗」;本表回答「投这个媒体,窗口内被检索到的比例是多少」。
**账本没有分母,所以不能拿它算率**;本表只记「这篇文章的 URL 有没有进检索结果」,不做因果归因。

## 三条不许违反的口径纪律

1. **表里一个比率都不存。** 比率一旦落库就固化了一个分母选择,下游再也看不见它。
2. **gate1 的分母是 `tests_observable`,不是 `tests_total`。**
   `monitoring_results.search_citations` 有 77,350 行空串 + 857 行 NULL —— 那些监测根本没记录
   AI 检索了什么。把它们记成「没被引」= 把「没看见」当「失败」。生产实测窗口内可观察率仅 28.5%,
   且按品牌差 14 倍(栖舍 94.0% / 罗平皓琪 6.7%)。
3. 🔴 **本模块 = 第一关(URL 被检索)事实层,不记品牌被提及/被推荐。**
   理由是粒度错配:同品牌两篇文章的 30 天窗口重叠时,一次唯一监测事件会被数成两次
   (Codex 2026-08-09 构造用例实证:两篇文章 + 同一条监测 → tests_total=2 / 提及=2,唯一事件 1)。

   ⚠️ [2026-08-09 Review 订正] 上一版这里写「已由 keyword_compliance_log 承担」,**是假的**:
     · 第二关(被提及):`keyword_compliance_log.detection_rate` 只是它的**日聚合率**,不是事实层
     · 第三关(被推荐):**当前没有任何聚合层**,原始信号只在
       `monitoring_results.mention_type='recommended'`(全库仅 309 条)
   → 二、三关需另建「关键词 × 引擎 × 观测日」口径。**不许把 detection_rate 当推荐率用。**

## fail-soft 方向

缺表(迁移 032 未跑)→ 记 `table_missing` 返 0 行,不抛。事实表是观测面,不能挡住
发布/监测/计费主链。**代价是漏跑不会自己冒出来**,要靠部署单核验收 SQL。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from services.strict_article_outcomes import (
    URL_NORMALIZATION_VERSION,
    normalize_publication_url,
    parse_citation_urls,
    publication_domain,
)

logger = logging.getLogger("GEO-PublicationFacts")

ATTEMPT_TABLE: Final = "geo_publication_attempt_facts"
OUTCOME_TABLE: Final = "geo_publication_outcome_facts"

# 口径版本。**任何口径变更必须 bump 这个值并写新行**,绝不就地改写历史行 —— 与账本同纪律。
FACTS_METRIC_VERSION: Final = "publication-outcome-facts-v1.0"

DEFAULT_WINDOWS: Final = (7, 14, 30)
PUBLICATION_SOURCE: Final = "mhz_publish_order_item"

# 我方主动终止 ≠ 媒体不给过。混进拒稿会低估过稿率(Review P1-6)。
TERMINATED_BY_US: Final = frozenset({"cancelled", "withdrawn"})



def _utc(value: Any) -> datetime | None:
    """归一成 aware UTC。拿不到 → None(调用方据此跳过,不补默认值)。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def citation_rank_for(raw: Any, target_url: str) -> int | None:
    """target_url 在这条 search_citations 里的最好排位;没命中 → None。

    ⚠️ 归一化**必须**走 `normalize_publication_url` —— 与 `parse_citation_urls` 同一个函数。
    自己再写一套「差不多的」归一化,就会出现「parse 说命中、rank 说没命中」这种自相矛盾。
    """
    if raw in (None, "") or not target_url:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, list):
        return None
    best: int | None = None
    for position, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            continue
        normalized = normalize_publication_url(item.get("url") or item.get("link") or "")
        if normalized != target_url:
            continue
        # rank 字段不一定有,也不一定是数字;取不到就退回数组下标(1-based)。
        rank_raw = item.get("rank")
        try:
            rank = int(rank_raw) if rank_raw is not None else position
        except (TypeError, ValueError):
            rank = position
        if rank > 0 and (best is None or rank < best):
            best = rank
    return best


# ══════════════════════════════════════════════════════════════════
# 取数
# ══════════════════════════════════════════════════════════════════

_PUBLICATIONS_SQL: Final = """
    SELECT id, brand_id, media_id, media_name, status, publish_url,
           cost_yuan, submitted_at, published_at, created_at
      FROM mhz_publish_order_items
     ORDER BY id
"""

# 只拉「有已发布文章的品牌」的监测行,别把全库 92k 行都搬进内存。
# 🔴 必须有时间边界。窗口最大 30 天,发布区间之外的监测行对本表毫无用处 ——
# 不设边界会把这些品牌的**全部历史监测**拉进内存,随数据增长复杂度迅速上升
# (Review 2026-08-09 P2:当前未超时,但上线前要在生产克隆记录行数/耗时/内存)。
_MONITORING_SQL: Final = """
    SELECT mr.id, mt.brand_id, mr.keyword, mr.platform, mr.tested_at,
           mr.search_citations
      FROM monitoring_results mr
      JOIN monitoring_tasks mt ON mt.id = mr.task_id
     WHERE mt.brand_id = ANY(%s)
       AND mr.tested_at IS NOT NULL
       AND mr.tested_at >= %s
       AND mr.tested_at <  %s
     ORDER BY mr.tested_at
"""

_ATTEMPT_UPSERT: Final = f"""
    INSERT INTO {ATTEMPT_TABLE} (
        metric_version, publication_source, publication_source_id,
        brand_id, media_id, media_name, publish_domain, attempt_status,
        is_published, is_rejected, is_terminated_by_us,
        cost_yuan, has_cost_record, submitted_at, published_at, source_created_at
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (metric_version, publication_source, publication_source_id)
    DO UPDATE SET
        attempt_status = EXCLUDED.attempt_status,
        is_published = EXCLUDED.is_published,
        is_rejected = EXCLUDED.is_rejected,
        is_terminated_by_us = EXCLUDED.is_terminated_by_us,
        publish_domain = EXCLUDED.publish_domain,
        cost_yuan = EXCLUDED.cost_yuan,
        has_cost_record = EXCLUDED.has_cost_record,
        submitted_at = EXCLUDED.submitted_at,
        published_at = EXCLUDED.published_at,
        built_at = NOW()
"""

_OUTCOME_UPSERT: Final = f"""
    INSERT INTO {OUTCOME_TABLE} (
        metric_version, url_normalization_version,
        publication_source, publication_source_id,
        brand_id, publish_url_normalized, publish_domain, media_id, media_name,
        published_at, keyword, platform, window_days, window_complete,
        tests_total, tests_observable, citations_unparsable,
        gate1_url_cited, first_cited_at, best_citation_rank
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (metric_version, publication_source, publication_source_id,
                 keyword, platform, window_days)
    DO UPDATE SET
        window_complete = EXCLUDED.window_complete,
        tests_total = EXCLUDED.tests_total,
        tests_observable = EXCLUDED.tests_observable,
        citations_unparsable = EXCLUDED.citations_unparsable,
        gate1_url_cited = EXCLUDED.gate1_url_cited,
        first_cited_at = EXCLUDED.first_cited_at,
        best_citation_rank = EXCLUDED.best_citation_rank,
        built_at = NOW()
"""


def _attempt_row(pub: dict[str, Any]) -> tuple:
    status = str(pub.get("status") or "")
    normalized = normalize_publication_url(pub.get("publish_url") or "")
    cost = pub.get("cost_yuan")
    return (
        FACTS_METRIC_VERSION, PUBLICATION_SOURCE, int(pub["id"]),
        pub.get("brand_id"), pub.get("media_id"), pub.get("media_name"),
        publication_domain(normalized) if normalized else None,
        status,
        status == "published",
        status == "rejected",
        status in TERMINATED_BY_US,
        cost,
        cost is not None and float(cost) > 0,
        _utc(pub.get("submitted_at")), _utc(pub.get("published_at")), _utc(pub.get("created_at")),
    )


def build_publication_facts(
    *,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """重算两张事实表。幂等:同一 metric_version 重跑只刷新计数,不产生重复行。

    返回结构可直接进心跳 detail。**不把「没水」藏起来**:可观察率、被跳过的原因都带出来。
    """
    from db.connection import get_db

    clock = now or datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "metric_version": FACTS_METRIC_VERSION,
        "url_normalization_version": URL_NORMALIZATION_VERSION,
        "windows": list(windows),
        "dry_run": bool(dry_run),
        "publications_total": 0,
        "publications_published": 0,
        "publications_skipped_no_url": 0,
        "publications_skipped_no_brand": 0,
        "publications_skipped_no_published_at": 0,
        "attempt_rows": 0,
        "outcome_rows": 0,
        "monitoring_rows_scanned": 0,
        "tests_total": 0,
        "tests_observable": 0,
        "citations_unparsable": 0,
        "gate1_url_cited": 0,
        "stale_rows_deleted": 0,
    }

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(_PUBLICATIONS_SQL)
            publications = [dict(r) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table(exc):
            summary["table_missing"] = True
            return summary
        raise

    summary["publications_total"] = len(publications)

    attempt_rows = [_attempt_row(p) for p in publications]
    summary["attempt_rows"] = len(attempt_rows)

    # ── 只对已发布、有 URL、有品牌、有发布时间的文章算三关 ──────────
    eligible: list[dict[str, Any]] = []
    for pub in publications:
        if str(pub.get("status") or "") != "published":
            continue
        summary["publications_published"] += 1
        normalized = normalize_publication_url(pub.get("publish_url") or "")
        if not normalized:
            summary["publications_skipped_no_url"] += 1
            continue
        if pub.get("brand_id") is None:
            summary["publications_skipped_no_brand"] += 1
            continue
        published_at = _utc(pub.get("published_at"))
        if published_at is None:
            summary["publications_skipped_no_published_at"] += 1
            continue
        eligible.append({**pub, "_url": normalized, "_published_at": published_at})

    brand_ids = sorted({int(p["brand_id"]) for p in eligible})
    # 时间边界 = [最早发布日, 最晚发布日 + 最大窗口)。区间外的监测行对本表零贡献。
    monitoring: list[dict[str, Any]] = []
    if brand_ids:
        pub_times = [p["_published_at"] for p in eligible]
        window_lo = min(pub_times)
        window_hi = max(pub_times) + timedelta(days=max(windows))
        summary["monitoring_window_from"] = window_lo.isoformat()
        summary["monitoring_window_to"] = window_hi.isoformat()
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(_MONITORING_SQL, (brand_ids, window_lo, window_hi))
                monitoring = [dict(r) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            if _is_missing_table(exc):
                summary["table_missing"] = True
                return summary
            raise
    summary["monitoring_rows_scanned"] = len(monitoring)

    by_brand: dict[int, list[dict[str, Any]]] = {}
    for row in monitoring:
        brand_id = row.get("brand_id")
        if brand_id is None:
            continue
        by_brand.setdefault(int(brand_id), []).append(row)

    outcome_rows: list[tuple] = []
    for pub in eligible:
        rows = by_brand.get(int(pub["brand_id"]))
        if not rows:
            continue
        for window_days in windows:
            outcome_rows.extend(
                _outcome_rows_for(pub, rows, window_days, clock, summary)
            )

    summary["outcome_rows"] = len(outcome_rows)

    if dry_run:
        return summary

    # 🔴 事务内**全量重建**,不是纯 upsert。
    #    发布订单可以从 published 改成 rejected/cancelled,URL/品牌/媒体/发布时间也会被订正
    #    (现役更新入口 db/meijiehezi_db.py)。只 upsert 不删,过期战果会永远留在表里 ——
    #    一篇后来被判 rejected 的文章,它的 30 天 gate1 命中会继续被下游当成真战果。
    #    表很小(实测三窗口合计约 9k 行),整段重建比「算出该删哪些」更容易证明正确。
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {OUTCOME_TABLE} WHERE metric_version = %s",
                        (FACTS_METRIC_VERSION,))
            summary["stale_rows_deleted"] += int(getattr(cur, "rowcount", 0) or 0)
            cur.execute(f"DELETE FROM {ATTEMPT_TABLE} WHERE metric_version = %s",
                        (FACTS_METRIC_VERSION,))
            summary["stale_rows_deleted"] += int(getattr(cur, "rowcount", 0) or 0)
            for params in attempt_rows:
                cur.execute(_ATTEMPT_UPSERT, params)
            for params in outcome_rows:
                cur.execute(_OUTCOME_UPSERT, params)
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table(exc):
            summary["table_missing"] = True
            logger.warning("[publication-facts] 事实表缺失(迁移 032 未跑),本轮记 0 行不抛")
            summary["attempt_rows"] = 0
            summary["outcome_rows"] = 0
            return summary
        raise

    logger.info(
        "[publication-facts] 重算完成: attempt %s 行 · outcome %s 行 · 窗口内监测 %s 次 · 可观察 %s 次",
        summary["attempt_rows"], summary["outcome_rows"],
        summary["tests_total"], summary["tests_observable"],
    )
    return summary


def _outcome_rows_for(
    pub: dict[str, Any],
    brand_rows: list[dict[str, Any]],
    window_days: int,
    clock: datetime,
    summary: dict[str, Any],
) -> list[tuple]:
    """一篇文章 × 一个窗口 → 若干 (keyword, platform) 事实行。窗口内无监测则不产行。"""
    published_at: datetime = pub["_published_at"]
    target_url: str = pub["_url"]
    window_end = published_at + timedelta(days=window_days)
    # 🔴 窗口没走完的行,分母天然偏小。这里如实标记,由查询侧决定要不要算它。
    window_complete = clock >= window_end

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in brand_rows:
        tested_at = _utc(row.get("tested_at"))
        # 🔴 严格半开区间 [published_at, published_at + window):发布之前的命中不算这篇文章的战果。
        #    Review P0-2 实测:244 个已发布 URL 里有 4 个「命中」发生在发布之前。
        if tested_at is None or tested_at < published_at or tested_at >= window_end:
            continue
        key = (str(row.get("keyword") or ""), str(row.get("platform") or ""))
        if not key[0] or not key[1]:
            continue
        bucket = buckets.setdefault(key, {
            "tests_total": 0, "tests_observable": 0, "citations_unparsable": 0,
            "gate1": 0, "first_cited_at": None, "best_rank": None,
        })
        bucket["tests_total"] += 1

        parsed = parse_citation_urls(row.get("search_citations"))
        status = str(parsed.get("status") or "")
        if status.startswith("valid"):
            bucket["tests_observable"] += 1
            if target_url in set(parsed.get("urls") or []):
                bucket["gate1"] += 1
                if bucket["first_cited_at"] is None or tested_at < bucket["first_cited_at"]:
                    bucket["first_cited_at"] = tested_at
                rank = citation_rank_for(row.get("search_citations"), target_url)
                if rank is not None and (bucket["best_rank"] is None or rank < bucket["best_rank"]):
                    bucket["best_rank"] = rank
        elif status in {"invalid_json", "not_array"}:
            bucket["citations_unparsable"] += 1
        # status == "empty" → 既不可观察也不是坏数据,只进 tests_total。这正是第一关看不见的那部分。
        # 🔴 这里**刻意不统计**品牌被提及/被推荐:那是「词 × 日」粒度,
        #    放进文章粒度会被文章数重复加权(见模块 docstring)。

    rows: list[tuple] = []
    for (keyword, platform), b in sorted(buckets.items()):
        summary["tests_total"] += b["tests_total"]
        summary["tests_observable"] += b["tests_observable"]
        summary["citations_unparsable"] += b["citations_unparsable"]
        summary["gate1_url_cited"] += b["gate1"]
        rows.append((
            FACTS_METRIC_VERSION, URL_NORMALIZATION_VERSION,
            PUBLICATION_SOURCE, int(pub["id"]),
            int(pub["brand_id"]), target_url, publication_domain(target_url),
            pub.get("media_id"), pub.get("media_name"),
            published_at, keyword, platform, int(window_days), window_complete,
            b["tests_total"], b["tests_observable"], b["citations_unparsable"],
            b["gate1"], b["first_cited_at"], b["best_rank"],
        ))
    return rows


def _is_missing_table(exc: Exception) -> bool:
    message = str(exc)
    if "does not exist" not in message and "UndefinedTable" not in message:
        return False
    return ATTEMPT_TABLE in message or OUTCOME_TABLE in message


# ══════════════════════════════════════════════════════════════════
# 读取侧:唯一允许算率的地方
# ══════════════════════════════════════════════════════════════════
#
# 🔴 为什么读取侧要给函数而不是让下游自己写 SQL:
#    这次 NO-GO 的五条 P0 里有三条是「分母选错」。表建好了但下游随手 `gate1 / tests_total`,
#    这张表就白建了 —— 那正是「把没看见当失败」的原样复现。
#    所以把分母绑死在函数里,并让返回值**自带分母口径说明**,不给下游选错的机会。

# gate1 的分母永远是 tests_observable,不是 tests_total。
# 这不是风格选择:search_citations 没记录时我们无从判断 URL 有没有被检索到,
# 把那些监测记成「没被引」= 把「没看见」当「失败」。
_GATE_DENOMINATORS: Final = {
    "gate1_url_cited": ("tests_observable", "可观察监测次数(search_citations 有记录)"),
}

# 🔴 分母必须从**投放人口**(attempt 表)起底,再 LEFT JOIN outcome。
# 只从 outcome 表数 articles_total,会把「窗口内一次监测都没有」的文章整个漏掉 ——
# 那些文章在 outcome 表里根本不产行(_outcome_rows_for 无监测则不产出),
# 于是 articles_unobservable 恒为 0,分母悄悄变成「至少被测过一次的文章」而不是全部已发布文章。
_ARTICLE_LEVEL_SQL: Final = f"""
    WITH pop AS (
        SELECT a.publication_source, a.publication_source_id
          FROM {ATTEMPT_TABLE} a
         WHERE a.metric_version = %s
           AND a.is_published
           AND a.published_at IS NOT NULL
           AND a.published_at + make_interval(days => %s) <= NOW()
           AND (%s::int IS NULL OR a.brand_id = %s::int)
    ), per_article AS (
        SELECT p.publication_source_id,
               COALESCE(SUM(o.tests_observable), 0) AS observable,
               COALESCE(SUM(o.gate1_url_cited), 0)  AS cited
          FROM pop p
          LEFT JOIN {OUTCOME_TABLE} o
                 ON o.metric_version = %s
                AND o.publication_source = p.publication_source
                AND o.publication_source_id = p.publication_source_id
                AND o.window_days = %s
                AND o.window_complete
         GROUP BY 1
    )
    SELECT COUNT(*)                                 AS articles_total,
           COUNT(*) FILTER (WHERE observable > 0)   AS articles_observable,
           COUNT(*) FILTER (WHERE cited > 0)        AS articles_cited
      FROM per_article
"""


def article_level_gate1(
    *, window_days: int = 30, brand_id: int | None = None,
    metric_version: str = FACTS_METRIC_VERSION,
) -> dict[str, Any]:
    """文章级第一关:一篇文章在窗口内**至少被检索到一次**的比例。报价要的是这个粒度。

    ⚠️ 与「每次监测的被引率」不是一回事,差一个数量级(生产实测 9.55% vs 0.75%),
       **不许互相替代**。前者答「发 N 篇能有几篇进 AI 视野」,后者答「某次提问命中的概率」。

    ⚠️ 分母是 `articles_observable`(至少有一次可观察监测的文章),不是 `articles_total`。
       完全没有可观察监测的文章 = 我们没测过它,不是它失败了。

    ⚠️ 只算 `window_complete` 的行。窗口没走完的文章分母天然偏小。
    """
    from db.connection import get_db

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(_ARTICLE_LEVEL_SQL,
                        (metric_version, int(window_days), brand_id, brand_id,
                         metric_version, int(window_days)))
            row = dict(cur.fetchone() or {})
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table(exc):
            return {"available": False, "reason": "table_missing"}
        raise

    total = int(row.get("articles_total") or 0)
    observable = int(row.get("articles_observable") or 0)
    cited = int(row.get("articles_cited") or 0)
    return {
        "available": True,
        "metric_version": metric_version,
        "window_days": int(window_days),
        "brand_id": brand_id,
        "articles_total": total,
        "articles_observable": observable,
        "articles_cited": cited,
        "articles_unobservable": total - observable,
        # 🔴 率永远和它的分母口径一起返回,不给下游「拿到一个光秃秃的百分比」的机会
        "rate": (round(cited / observable, 4) if observable else None),
        "denominator_field": "articles_observable",
        "denominator_note": "至少有一次可观察监测的文章数(完全没测过的不进分母)",
        "gate": "gate1_url_cited",
        "gate_note": "只是「URL 进了 AI 的检索结果」,不是「品牌被推荐」,更不是「有效」",
    }
