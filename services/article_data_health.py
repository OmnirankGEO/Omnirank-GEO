"""Read-only Data Health Beacon for article/question/style evolution."""
from __future__ import annotations

from typing import Any, Final


DATA_HEALTH_VERSION: Final = "article-data-health-v1.0"
DESCRIPTIVE_MIN_EVENTS: Final = 30
DESCRIPTIVE_MIN_BRANDS: Final = 5
DESCRIPTIVE_MIN_INDUSTRIES: Final = 3
DESCRIPTIVE_MIN_WEEKS: Final = 4


from services.monitoring_lineage import (
    LINEAGE_STATUS_COMPLETE as _LINEAGE_COMPLETE,
)


def _recorded_lineage_sql(alias: str = "") -> str:
    """「血缘如实记录过」的谓词 —— 取自 ``monitoring_lineage`` 的 SSOT。

    🔴 [工单 V3-C · C-3] 原来这里写死 ``lineage_status = 'complete'``。
       C-3 之后 ``complete`` 只留给"模型被供应商回显证实"的行,
       计划值行是 ``model_unconfirmed`` —— 仍然是**如实记录过**的血缘。
       不改这里的话,这个面板会把"我们从来没拿到过真回显"报成
       "血缘全丢了",两件完全不同的事混成同一个红。
    """
    from services.monitoring_lineage import recorded_lineage_sql

    return recorded_lineage_sql(alias)


def _scalar(cur, sql: str, params=()) -> int:
    cur.execute(sql, params)
    row = cur.fetchone() or {}
    return int(next(iter(row.values())) or 0)


def get_article_data_health(*, since_days: int = 180) -> dict[str, Any]:
    from db.connection import get_connection
    from services.monitoring_identity_review import aggregate_eligible_sql

    conn = get_connection()
    try:
        cur = conn.cursor()
        eligible = aggregate_eligible_sql()
        total_monitoring = _scalar(
            cur,
            f"SELECT COUNT(*) AS n FROM monitoring_results "
            f"WHERE tested_at BETWEEN CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') AND CURRENT_TIMESTAMP "
            f"AND {eligible}",
            (since_days,),
        )
        complete_monitoring = _scalar(
            cur,
            f"SELECT COUNT(*) AS n FROM monitoring_results "
            f"WHERE tested_at BETWEEN CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') AND CURRENT_TIMESTAMP "
            f"AND {eligible} "
            f"AND {_recorded_lineage_sql()} "
            f"AND NULLIF(sent_question_snapshot, '') IS NOT NULL",
            (since_days,),
        )
        # 🔴 [工单 V3-C · C-3] 上面问的是"血缘有没有如实记下来";
        #    下面这个问的是另一件事:"``model`` 那一列被供应商回显证实了吗"。
        #    合成一个数会让"我们其实全是计划值"这件事在面板上消失 ——
        #    而那正是 Codex 三审 P1-5 的实况。两个数分开报。
        model_provider_confirmed = _scalar(
            cur,
            f"SELECT COUNT(*) AS n FROM monitoring_results "
            f"WHERE tested_at BETWEEN CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') AND CURRENT_TIMESTAMP "
            f"AND {eligible} "
            f"AND lineage_status = '{_LINEAGE_COMPLETE}'",
            (since_days,),
        )
        known_model = _scalar(
            cur,
            f"SELECT COUNT(*) AS n FROM monitoring_results "
            f"WHERE tested_at BETWEEN CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') AND CURRENT_TIMESTAMP "
            f"AND {eligible} "
            f"AND provider NOT IN ('unknown','legacy_unknown') "
            f"AND model NOT IN ('unknown','legacy_unknown') "
            f"AND surface NOT IN ('unknown','legacy_unknown')",
            (since_days,),
        )
        future_monitoring = _scalar(
            cur, "SELECT COUNT(*) AS n FROM monitoring_results WHERE tested_at > CURRENT_TIMESTAMP"
        )
        publication_snapshots = _scalar(
            cur,
            """
            SELECT COUNT(DISTINCT article_id) AS n FROM (
                SELECT o.article_id
                  FROM mhz_publish_order_items i JOIN mhz_publish_orders o ON o.id=i.order_id
                 WHERE i.status='published' AND i.published_at <= CURRENT_TIMESTAMP
                   AND i.submitted_content_snapshot_at IS NOT NULL
                UNION
                SELECT o.article_id
                  FROM publish_order_items i
                  JOIN publish_orders o ON o.id=i.order_id
                  JOIN articles a ON a.id=o.article_id
                 WHERE i.status='published' AND i.published_at <= CURRENT_TIMESTAMP
                   AND a.publication_snapshot_source='publish_order_items'
                   AND a.publication_snapshot_source_id=i.id
                UNION
                -- [WO 自报收口 2026-08-19] 健康度分子只认服务端核实过的发布事实
                SELECT article_id FROM publish_records
                 WHERE status='success' AND public_url_verification_state = 'verified'
                   AND created_at <= CURRENT_TIMESTAMP
                   AND submitted_content_snapshot_at IS NOT NULL
                   AND article_id IS NOT NULL
            ) s WHERE article_id IS NOT NULL
            """,
        )
        future_snapshots = _scalar(
            cur,
            "SELECT COUNT(*) AS n FROM articles WHERE publication_snapshot_at > CURRENT_TIMESTAMP",
        )
        successful_published_articles = _scalar(
            cur,
            """
            SELECT COUNT(DISTINCT article_id) AS n FROM (
                SELECT o.article_id
                  FROM mhz_publish_order_items i JOIN mhz_publish_orders o ON o.id=i.order_id
                 WHERE i.status='published' AND i.published_at <= CURRENT_TIMESTAMP
                UNION
                SELECT o.article_id
                  FROM publish_order_items i JOIN publish_orders o ON o.id=i.order_id
                 WHERE i.status='published' AND i.published_at <= CURRENT_TIMESTAMP
                UNION
                -- [WO 自报收口 2026-08-19] 同上:自报未核实的不计入"已成功发布"
                SELECT article_id FROM publish_records
                 WHERE status='success' AND public_url_verification_state = 'verified'
                   AND created_at <= CURRENT_TIMESTAMP AND article_id IS NOT NULL
            ) s WHERE article_id IS NOT NULL
            """,
        )
        manual_registration_articles = _scalar(
            cur,
            "SELECT COUNT(DISTINCT article_id) AS n FROM media_publications "
            "WHERE created_at <= CURRENT_TIMESTAMP AND article_id IS NOT NULL",
        )
        state_time_conflicts = _scalar(
            cur,
            """
            SELECT COUNT(*) AS n FROM (
                SELECT id FROM mhz_publish_order_items
                 WHERE status IN ('rejected','withdrawn','failed','cancelled') AND published_at IS NOT NULL
                UNION ALL
                SELECT id FROM publish_order_items
                 WHERE status IN ('rejected','withdrawn','failed','cancelled') AND published_at IS NOT NULL
            ) x
            """,
        )
        jc5_count = _scalar(cur, "SELECT COUNT(*) AS n FROM geo_research_articles WHERE corpus_grade='JC5'")
        review_queue = _scalar(
            cur,
            "SELECT COUNT(*) AS n FROM articles WHERE article_review_status IN "
            "('blocked','rewrite_required','pending_human_review','legacy_unreviewed')",
        )
    finally:
        conn.close()

    strict_error = None
    try:
        from services.strict_article_outcomes import load_strict_outcomes

        strict = load_strict_outcomes(since_days=since_days)
    except Exception as exc:
        strict = {"events": [], "event_count": 0, "quality_counts": {}}
        strict_error = type(exc).__name__

    events = strict.get("events") or []
    brands = {e.get("brand_id") for e in events if e.get("brand_id") is not None}
    industries = {str(e.get("industry") or "").strip() for e in events if str(e.get("industry") or "").strip()}
    weeks = {str(e.get("tested_at") or "")[:10] for e in events if e.get("tested_at")}
    # Dates are retained as a conservative distinct-day proxy.  Four calendar
    # weeks need at least 22 days of span and four distinct ISO week buckets.
    iso_weeks = set()
    from datetime import datetime
    for event in events:
        try:
            dt = datetime.fromisoformat(str(event.get("tested_at")).replace("Z", "+00:00"))
            iso = dt.isocalendar()
            iso_weeks.add((iso.year, iso.week))
        except Exception:
            pass

    lineage_rate = complete_monitoring / total_monitoring if total_monitoring else None
    known_model_rate = known_model / total_monitoring if total_monitoring else None
    registration_rate = (
        publication_snapshots / successful_published_articles
        if successful_published_articles else None
    )
    descriptive_ready = (
        len(events) >= DESCRIPTIVE_MIN_EVENTS
        and len(brands) >= DESCRIPTIVE_MIN_BRANDS
        and len(industries) >= DESCRIPTIVE_MIN_INDUSTRIES
        and len(iso_weeks) >= DESCRIPTIVE_MIN_WEEKS
    )
    blockers: list[str] = []
    if total_monitoring == 0:
        blockers.append("no_monitoring_results")
    if not events:
        blockers.append("no_strict_article_question_events")
    if lineage_rate is not None and lineage_rate < 1:
        blockers.append("monitoring_lineage_not_100_percent")
    if known_model_rate is not None and known_model_rate < 0.95:
        blockers.append("provider_model_surface_below_95_percent")
    if registration_rate is not None and registration_rate < 1:
        blockers.append("publication_snapshot_not_100_percent")
    if future_monitoring or future_snapshots:
        blockers.append("future_time_rows_present")
    if state_time_conflicts:
        blockers.append("publication_state_time_conflicts_present")
    if strict_error:
        blockers.append(f"strict_outcome_error:{strict_error}")

    if not events:
        state, truth_level = "data_blocked", ("T1" if publication_snapshots else "T0")
    elif descriptive_ready:
        state, truth_level = "observing", "T2"
    else:
        state, truth_level = "observing", "T2_insufficient_coverage"

    return {
        "version": DATA_HEALTH_VERSION,
        "state": state,
        "truth_level": truth_level,
        "decision_status": "descriptive_ready" if descriptive_ready else "INSUFFICIENT_SAMPLES",
        "style_effect_decision_allowed": False,
        "style_effect_reason": (
            "需要预登记 paired shadow/control、成熟观察窗、cluster-aware 区间和 guardrail；"
            "直接事件成熟仅允许描述，不自动证明因果。"
        ),
        "counts": {
            "monitoring_total": total_monitoring,
            "monitoring_lineage_recorded": complete_monitoring,
            "monitoring_model_provider_confirmed": model_provider_confirmed,
            "provider_model_surface_known": known_model,
            "future_monitoring": future_monitoring,
            "successful_published_articles": successful_published_articles,
            "manual_registration_articles_time_unknown": manual_registration_articles,
            "publication_snapshots": publication_snapshots,
            "future_publication_snapshots": future_snapshots,
            "publication_state_time_conflicts": state_time_conflicts,
            "strict_article_question_events": len(events),
            "brands": len(brands),
            "industries": len(industries),
            "iso_weeks": len(iso_weeks),
            "jc5_corpus": jc5_count,
            "review_queue": review_queue,
        },
        "rates": {
            "monitoring_lineage_complete": lineage_rate,
            "provider_model_surface_known": known_model_rate,
            "publication_snapshot_coverage": registration_rate,
        },
        "strict_outcome_quality": strict.get("quality_counts") or {},
        "blockers": blockers,
    }
