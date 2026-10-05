"""归因账本 —— 把已有的严格归因引擎接到落脚点上(WO_DELIVERY_FLYWHEEL_CLOSURE §2.1)。

为什么需要这个模块(2026-08-06 生产只读取证结论,详见
`docs/AI-CONTEXT/FLYWHEEL_CLOSURE_STATUS_VERDICT_2026-08-06.md`):

  `services/strict_article_outcomes.load_strict_outcomes` 早就能跑出真实归因行 ——
  生产容器内只读实跑 `event_count=2`,其中一条是 QZQZ(brand 662)的文章 1365
  于 07-30 发布、08-05 被 moonshot 引用且 `target_outcome=recommended`。
  **但它的产物从不落库**,只被一个健康报告读一眼就扔。与此同时写作飞轮的
  `writing_outcome_backfill._citation_count_for_articles` 走的是另一条路:
  `publish_url` 精确等于 `geo_research_articles.url` —— 而全站 246 个已发布 URL 里
  进那个语料池的**只有 1 个**(归一化后也只到 2),监测池里才有 13 个。
  也就是说飞轮一直在查一个几乎空的池子,`writing_strategy_outcome_events` 因此恒 0 行。

本模块只做一件事:**把引擎产物幂等落进 `geo_article_citation_attributions`**,
让下游(飞轮回写 / 渠道有效性报表 / 零行报警)有一个共同的、可复跑的事实源。

口径纪律:
  - **只写不改**。幂等键 = (monitoring_result_id, publication_source, publication_source_id),
    冲突 DO NOTHING。口径变更走 `metric_version` 新行,绝不就地改写历史行。
  - **一行都不编**。引擎报 `insufficient` / 质量闸拦下的,这里就是不写,并把
    `quality_counts` 原样带进心跳 detail —— 让"为什么没水"可见,而不是显示成"跑成功了"。
  - **research 域一行不写**;monitoring 域一行不写。本模块是纯下游消费者。
  - 缺表(迁移 027 未跑)走 fail-soft:记 `table_missing` 返回 0 行,不抛 —— 归因是观测面,
    不能挡住发布/监测/计费主链。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("GEO-AttributionLedger")

LEDGER_TABLE = "geo_article_citation_attributions"

# 同步窗口默认 90 天:生产实测可用归因窗口从 2026-07-20 才开始(在那之前
# monitoring_results.lineage_status 全是 legacy_unknown 且没有 sent_question_snapshot,
# 归因引擎必然拦下),窗口开太大只是白扫。
DEFAULT_SINCE_DAYS = 90

_INSERT_SQL = f"""
    INSERT INTO {LEDGER_TABLE} (
        metric_version, url_normalization_version,
        monitoring_result_id, publication_source, publication_source_id,
        article_id, publication_snapshot_hash,
        brand_id, identity_key, industry,
        publish_url_normalized, publish_domain,
        style_family, question, question_family, question_family_version,
        provider, model, model_revision, surface, target_outcome,
        published_at, tested_at, url_match, body_proof
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT (monitoring_result_id, publication_source, publication_source_id) DO NOTHING
"""


def _row_params(event: dict[str, Any], url_norm_version: str) -> tuple | None:
    """事件 → INSERT 参数。缺任何一个 NOT NULL 事实就诚实丢弃(记 skipped,不补默认值)。"""
    result_id = event.get("monitoring_result_id")
    source = event.get("publication_source")
    source_id = event.get("publication_source_id")
    url = event.get("publish_url_normalized")
    published_at = event.get("published_at")
    tested_at = event.get("tested_at")
    if result_id is None or not source or source_id is None:
        return None
    if not url or not published_at or not tested_at:
        return None
    return (
        event.get("metric_version"), url_norm_version,
        int(result_id), str(source), int(source_id),
        event.get("article_id"), event.get("publication_snapshot_hash"),
        event.get("brand_id"), event.get("identity_key"), event.get("industry"),
        str(url), str(event.get("publish_domain") or ""),
        event.get("style_family"), event.get("question"),
        event.get("question_family"), event.get("question_family_version"),
        event.get("provider"), event.get("model"), event.get("model_revision"),
        event.get("surface"), event.get("target_outcome"),
        published_at, tested_at, event.get("url_match") or "exact_normalized",
        bool(event.get("body_proof", True)),
    )


def sync_article_attributions(
    *, since_days: int = DEFAULT_SINCE_DAYS, dry_run: bool = False,
) -> dict[str, Any]:
    """跑一次归因同步。返回可直接进心跳 detail 的结构(含 quality_counts,别把没水藏起来)。"""
    from services.strict_article_outcomes import load_strict_outcomes

    # include_unverified_body=True:把"URL 对得上但正文无快照"的行也**记进账本**,
    # 但它们带 body_proof=False,主指标查询恒过滤掉。理由见 strict_article_outcomes
    # 里那段注释 —— 晨光富士 8 个被引 URL 全卡在这道闸上,不记 = 客户看不到唯一的真实成果;
    # 混进主指标 = 伪造证据等级。两者都不可接受,所以记而不混。
    outcome = load_strict_outcomes(since_days=since_days, include_unverified_body=True)
    events = outcome.get("events") or []
    url_norm_version = str(outcome.get("url_normalization_version") or "unknown")

    summary: dict[str, Any] = {
        "since_days": int(since_days),
        "dry_run": bool(dry_run),
        "engine_event_count": int(outcome.get("event_count") or 0),
        "proven_event_count": int(outcome.get("proven_event_count") or 0),
        "unverified_body_event_count": int(outcome.get("unverified_body_event_count") or 0),
        "eligible_monitoring_count": int(outcome.get("eligible_monitoring_count") or 0),
        "unique_publication_url_count": int(outcome.get("unique_publication_url_count") or 0),
        "quality_counts": outcome.get("quality_counts") or {},
        "written": 0,
        "already_present": 0,
        "skipped_incomplete": 0,
    }

    rows = []
    for event in events:
        params = _row_params(event, url_norm_version)
        if params is None:
            summary["skipped_incomplete"] += 1
            continue
        rows.append(params)

    if dry_run or not rows:
        summary["processed"] = summary["written"]
        return summary

    from db.connection import get_db

    try:
        with get_db() as conn:
            cur = conn.cursor()
            for params in rows:
                cur.execute(_INSERT_SQL, params)
                # rowcount==0 → ON CONFLICT DO NOTHING 命中,说明这条已经在账本里
                if int(getattr(cur, "rowcount", 0) or 0) > 0:
                    summary["written"] += 1
                else:
                    summary["already_present"] += 1
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "geo_article_citation_attributions" in message and (
            "does not exist" in message or "UndefinedTable" in message
        ):
            summary["table_missing"] = True
            logger.warning("[attribution-ledger] 账本表缺失(迁移 027 未跑),本轮记 0 行不抛")
            summary["processed"] = 0
            return summary
        raise

    summary["processed"] = summary["written"]
    logger.info(
        "[attribution-ledger] 同步完成: 引擎产出 %s · 新写 %s · 已存在 %s",
        summary["engine_event_count"], summary["written"], summary["already_present"],
    )
    return summary


def ledger_freshness() -> dict[str, Any]:
    """账本新鲜度(给 §2.1.4 零行报警用)。缺表 → available=False,由调用方决定怎么说。"""
    from db.connection import get_db

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total, MAX(created_at) AS last_written, "
                f"MAX(tested_at) AS last_tested FROM {LEDGER_TABLE}"
            )
            row = dict(cur.fetchone() or {})
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)[:200]}

    last_written = row.get("last_written")
    age_seconds: float | None = None
    if isinstance(last_written, datetime):
        stamp = last_written if last_written.tzinfo else last_written.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - stamp).total_seconds()
    return {
        "available": True,
        "total_rows": int(row.get("total") or 0),
        "last_written": last_written,
        "last_tested": row.get("last_tested"),
        "age_seconds": age_seconds,
    }


def bridge_pairing_rates() -> dict[str, Any]:
    """[P0-3 观察面 2026-08-14] 三条红线桥的配对率现值(J4/J8/J9 周报口径)。

    🔴 红线桥限定语(工单红线 6):三个比率都是**已配对子样本对全量的覆盖率**,
    基线(研究定稿 2026-08-13):J4 发布桥 5.0% / J8 identity 1.5% /
    J9 target_outcome 有效解析 1.5%。回填推进后此处数字上移;任何引用这些
    比率的下游必须带「已配对子样本」限定,不得外推全量。
    失败 → available=False(观测面,绝不抛)。
    """
    from db.connection import get_db

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM articles)                                            AS articles_total,
                  (SELECT COUNT(DISTINCT article_id) FROM mhz_publish_orders
                    WHERE article_id IS NOT NULL)                                            AS articles_with_mhz_link,
                  (SELECT COUNT(*) FROM monitoring_results)                                  AS monitoring_total,
                  (SELECT COUNT(*) FROM monitoring_results WHERE identity_brand_id IS NOT NULL) AS monitoring_identity_paired,
                  (SELECT COUNT(*) FROM monitoring_results
                    WHERE target_outcome IN ('recommended','conditionally_recommended',
                          'candidate_only','mentioned_only','criteria_only',
                          'refused_no_evidence','refused_risk','not_mentioned',
                          'entity_ambiguous','engine_error'))                            AS monitoring_outcome_resolved
                """
            )
            row = dict(cur.fetchone() or {})
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)[:200]}

    def _rate(numerator: Any, denominator: Any) -> float | None:
        n, d = int(numerator or 0), int(denominator or 0)
        return round(n / d, 4) if d else None

    return {
        "available": True,
        "note": "已配对子样本覆盖率(红线桥 J4/J8/J9),不得外推全量口径。",
        "j4_publication_bridge": {
            "paired": int(row.get("articles_with_mhz_link") or 0),
            "total": int(row.get("articles_total") or 0),
            "rate": _rate(row.get("articles_with_mhz_link"), row.get("articles_total")),
        },
        "j8_identity_bridge": {
            "paired": int(row.get("monitoring_identity_paired") or 0),
            "total": int(row.get("monitoring_total") or 0),
            "rate": _rate(row.get("monitoring_identity_paired"), row.get("monitoring_total")),
        },
        "j9_outcome_bridge": {
            "paired": int(row.get("monitoring_outcome_resolved") or 0),
            "total": int(row.get("monitoring_total") or 0),
            "rate": _rate(row.get("monitoring_outcome_resolved"), row.get("monitoring_total")),
        },
    }


def citation_counts_for_articles(article_ids: list[int], since_days: int = 30) -> dict[str, Any]:
    """给写作飞轮用的被引计数 —— 读账本(监测池口径),不读 geo_research 语料池。

    与 `writing_outcome_backfill._citation_count_for_articles` 的分工:那条查
    `geo_research_articles.url`(全站 246 个已发布 URL 只命中 1 个),本条查账本
    (监测池口径,三家 143 个 URL 命中 13 个)。调用方取两者之和的口径见该模块。
    """
    ids = [int(a) for a in (article_ids or []) if a]
    if not ids:
        return {"articles": 0, "citations": 0, "matched_articles": 0, "insufficient_data": True}

    from db.connection import get_db

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT COUNT(DISTINCT article_id) AS matched_articles,
                       COUNT(*)                   AS citations
                  FROM {LEDGER_TABLE}
                 WHERE article_id = ANY(%s)
                   AND tested_at >= NOW() - make_interval(days => %s)
                   -- 🔴 主指标只认有正文证据的行。去掉这个条件 = 把"无法自证"
                   --    的引用算进写作版本的功劳,那是伪造证据等级。
                   AND body_proof
                """,
                (ids, int(since_days)),
            )
            row = dict(cur.fetchone() or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[attribution-ledger] 被引计数失败(诚实 insufficient,不造假): %s", str(exc)[:200])
        return {"articles": len(ids), "citations": 0, "matched_articles": 0, "insufficient_data": True}

    matched = int(row.get("matched_articles") or 0)
    return {
        "articles": len(ids),
        "matched_articles": matched,
        "citations": int(row.get("citations") or 0),
        "insufficient_data": matched == 0,
    }
