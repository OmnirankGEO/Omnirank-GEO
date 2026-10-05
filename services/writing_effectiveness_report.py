"""WP12 P2-6 · Bi-weekly writing-effectiveness review report.

Master SSOT v2.4 requires one report that answers, per **文体 / 长度档 / 域 /
引擎**, how our published articles actually perform against citations — with
the north-star number stated first: *how many of our own published URLs have
ever entered an AI answer* (measured 1 / 185 when D12 was signed).

Two hard product constraints, both from the Owner:

* **只出报告，不自动改配比.**  Ratios are revenue structure; this module never
  writes ``style_ratios`` and exposes no path that could.
* **不重造 P0-3 闭环.**  It reads the same published-URL -> research-article ->
  citation lineage that ``services/writing_outcome_backfill`` already
  established (including the tombstone exclusion), and reuses the citation
  domain weights from ``services/citation_domain_weights``.

Everything is read-only over business data; the only write is the report row
itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any, Final

logger = logging.getLogger("GEO-WritingEffectivenessReport")

WRITING_EFFECTIVENESS_REPORT_VERSION: Final = "geo-writing-effectiveness-report-v1.0"

DEFAULT_WINDOW_DAYS: Final = 90
REVIEW_INTERVAL_DAYS: Final = 14

# The four engines the research flywheel collects.  Kept explicit so a report
# can show an engine with zero observations instead of silently dropping it.
KNOWN_ENGINES: Final[tuple[str, ...]] = ("kimi", "deepseek", "qwen", "doubao")

# Owner-recorded baseline (v2.4 ⑤, post-inflection detection rates).  Shown next
# to the measured value so a drop is visible without re-deriving it by hand.
ENGINE_DETECTION_BASELINE: Final[dict[str, float]] = {
    "kimi": 0.614,
    "deepseek": 0.448,
    "qwen": 0.429,
    "doubao": 0.279,
}

# Our own published URLs joined to research articles.  ``mhz_publish_orders``
# owns ``article_id``; ``mhz_publish_order_items`` only has ``order_id`` — see
# the note in writing_outcome_backfill for the defect this replaced.
_PUBLISHED_LINEAGE_CTE: Final = """
WITH published AS (
    SELECT DISTINCT o.article_id            AS article_id,
                    btrim(i.publish_url)    AS url
      FROM mhz_publish_order_items i
      JOIN mhz_publish_orders o ON o.id = i.order_id
     WHERE i.status = 'published'
       AND btrim(COALESCE(i.publish_url, '')) <> ''
       AND o.article_id IS NOT NULL
),
matched AS (
    SELECT p.article_id,
           p.url,
           gra.id                  AS research_id,
           gra.domain              AS domain,
           gra.cleaned_char_count  AS research_chars
      FROM published p
      LEFT JOIN geo_research_articles gra
             ON gra.url = p.url
            AND gra.domain_tier <> 'blacklist'
            AND gra.clean_status = 'cleaned'
)
"""


@dataclass(frozen=True)
class ReportUnavailable:
    reason: str

    def payload(self) -> dict[str, Any]:
        return {
            "version": WRITING_EFFECTIVENESS_REPORT_VERSION,
            "available": False,
            "reason": self.reason,
            "message": "暂时读不到发布/被引数据，本期不出结论，也不编造数字。",
        }


def _length_bucket_expr(column: str) -> str:
    """Buckets aligned with the length contract's avoidance band."""
    return f"""
        CASE
            WHEN COALESCE({column}, 0) < 2000 THEN 'lt_2k'
            WHEN COALESCE({column}, 0) < 6000 THEN '2k_6k'
            WHEN COALESCE({column}, 0) <= 11000 THEN '6k_11k_avoid'
            ELSE '12k_plus'
        END
    """


def _rows(cur) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def build_writing_effectiveness_report(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    industry: str = "",
) -> dict[str, Any]:
    """Compute the report.  Never raises: unavailable data yields an honest row."""
    from db.connection import get_connection

    window = max(7, min(720, int(window_days or DEFAULT_WINDOW_DAYS)))
    try:
        conn = get_connection()
    except Exception as exc:  # pragma: no cover
        return ReportUnavailable(f"db_unavailable:{type(exc).__name__}").payload()

    try:
        cur = conn.cursor()

        # --- north star -------------------------------------------------
        cur.execute(
            _PUBLISHED_LINEAGE_CTE + """
            SELECT COUNT(*)                                              AS published_urls,
                   COUNT(*) FILTER (WHERE m.research_id IS NOT NULL)     AS crawled_urls,
                   COUNT(*) FILTER (WHERE c.citations > 0)               AS cited_urls,
                   COALESCE(SUM(c.citations), 0)                         AS total_citations,
                   AVG(c.avg_rank) FILTER (WHERE c.citations > 0)        AS avg_rank_in_response
              FROM matched m
              LEFT JOIN LATERAL (
                    SELECT COUNT(*)                          AS citations,
                           AVG(NULLIF(x.rank_in_response, 0)) AS avg_rank
                      FROM geo_research_article_citations x
                     WHERE x.article_id = m.research_id
                       AND x.cited_at >= NOW() - make_interval(days => %s)
                   ) c ON TRUE
            """,
            (window,),
        )
        north = _rows(cur)
        north_row = north[0] if north else {}
        published_urls = int(north_row.get("published_urls") or 0)
        cited_urls = int(north_row.get("cited_urls") or 0)

        # --- by 文体 -----------------------------------------------------
        cur.execute(
            _PUBLISHED_LINEAGE_CTE + """
            SELECT COALESCE(NULLIF(a.style_code, ''), NULLIF(a.style, ''), 'unknown') AS style_code,
                   COUNT(*)                                        AS published_urls,
                   COUNT(*) FILTER (WHERE c.citations > 0)          AS cited_urls,
                   COALESCE(SUM(c.citations), 0)                    AS total_citations,
                   AVG(c.avg_rank) FILTER (WHERE c.citations > 0)   AS avg_rank_in_response
              FROM matched m
              JOIN articles a ON a.id = m.article_id
              LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS citations, AVG(NULLIF(x.rank_in_response, 0)) AS avg_rank
                      FROM geo_research_article_citations x
                     WHERE x.article_id = m.research_id
                       AND x.cited_at >= NOW() - make_interval(days => %s)
                   ) c ON TRUE
             GROUP BY 1
             ORDER BY total_citations DESC, published_urls DESC
            """,
            (window,),
        )
        by_style = _rows(cur)

        # --- by 长度档 ---------------------------------------------------
        cur.execute(
            _PUBLISHED_LINEAGE_CTE + f"""
            SELECT {_length_bucket_expr('COALESCE(m.research_chars, length(a.content))')} AS length_bucket,
                   COUNT(*)                                        AS published_urls,
                   COUNT(*) FILTER (WHERE c.citations > 0)          AS cited_urls,
                   COALESCE(SUM(c.citations), 0)                    AS total_citations,
                   AVG(c.avg_rank) FILTER (WHERE c.citations > 0)   AS avg_rank_in_response
              FROM matched m
              LEFT JOIN articles a ON a.id = m.article_id
              LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS citations, AVG(NULLIF(x.rank_in_response, 0)) AS avg_rank
                      FROM geo_research_article_citations x
                     WHERE x.article_id = m.research_id
                       AND x.cited_at >= NOW() - make_interval(days => %s)
                   ) c ON TRUE
             GROUP BY 1
             ORDER BY 1
            """,
            (window,),
        )
        by_length = _rows(cur)

        # --- by 域 -------------------------------------------------------
        cur.execute(
            _PUBLISHED_LINEAGE_CTE + """
            SELECT COALESCE(NULLIF(m.domain, ''), 'unmatched')      AS domain,
                   COUNT(*)                                        AS published_urls,
                   COUNT(*) FILTER (WHERE c.citations > 0)          AS cited_urls,
                   COALESCE(SUM(c.citations), 0)                    AS total_citations,
                   AVG(c.avg_rank) FILTER (WHERE c.citations > 0)   AS avg_rank_in_response
              FROM matched m
              LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS citations, AVG(NULLIF(x.rank_in_response, 0)) AS avg_rank
                      FROM geo_research_article_citations x
                     WHERE x.article_id = m.research_id
                       AND x.cited_at >= NOW() - make_interval(days => %s)
                   ) c ON TRUE
             GROUP BY 1
             ORDER BY total_citations DESC, published_urls DESC
             LIMIT 50
            """,
            (window,),
        )
        by_domain = _rows(cur)

        # --- by 引擎(v2.4 ⑤ 豆包 27.9% 是短板,必须分列不看均值) ----------
        cur.execute(
            _PUBLISHED_LINEAGE_CTE + """
            SELECT x.platform                                   AS engine,
                   COUNT(*)                                     AS citations,
                   COUNT(DISTINCT m.article_id)                 AS cited_our_articles,
                   AVG(NULLIF(x.rank_in_response, 0))           AS avg_rank_in_response
              FROM matched m
              JOIN geo_research_article_citations x
                   ON x.article_id = m.research_id
                  AND x.cited_at >= NOW() - make_interval(days => %s)
             GROUP BY 1
             ORDER BY citations DESC
            """,
            (window,),
        )
        our_by_engine = {str(r["engine"]): r for r in _rows(cur)}

        # 全语料引擎分列(不只我方文章),用于判断"是引擎变了还是我们变了"。
        industry_clause = "AND a.primary_industry = %s" if str(industry or "").strip() else ""
        corpus_params: list[Any] = [window]
        if industry_clause:
            corpus_params.append(str(industry).strip())
        cur.execute(
            f"""
            SELECT c.platform                            AS engine,
                   COUNT(*)                              AS citations,
                   COUNT(DISTINCT c.article_id)          AS cited_articles,
                   AVG(NULLIF(c.rank_in_response, 0))    AS avg_rank_in_response
              FROM geo_research_article_citations c
              JOIN geo_research_articles a ON a.id = c.article_id
             WHERE c.cited_at >= NOW() - make_interval(days => %s)
               {industry_clause}
             GROUP BY 1
             ORDER BY citations DESC
            """,
            corpus_params,
        )
        corpus_by_engine = {str(r["engine"]): r for r in _rows(cur)}
    except Exception as exc:
        logger.warning("[writing-effectiveness] report query failed: %s", str(exc)[:300])
        return ReportUnavailable(f"query_failed:{type(exc).__name__}:{str(exc)[:120]}").payload()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    engines = []
    for engine in KNOWN_ENGINES:
        ours = our_by_engine.get(engine) or {}
        corpus = corpus_by_engine.get(engine) or {}
        engines.append({
            "engine": engine,
            "our_citations": int(ours.get("citations") or 0),
            "our_cited_articles": int(ours.get("cited_our_articles") or 0),
            "our_avg_rank_in_response": _round(ours.get("avg_rank_in_response")),
            "corpus_citations": int(corpus.get("citations") or 0),
            "corpus_cited_articles": int(corpus.get("cited_articles") or 0),
            "corpus_avg_rank_in_response": _round(corpus.get("avg_rank_in_response")),
            "owner_baseline_detection_rate": ENGINE_DETECTION_BASELINE.get(engine),
        })
    # Engines observed but not in the known list still get reported.
    for engine in sorted(set(corpus_by_engine) - set(KNOWN_ENGINES)):
        corpus = corpus_by_engine[engine]
        engines.append({
            "engine": engine,
            "our_citations": int((our_by_engine.get(engine) or {}).get("citations") or 0),
            "our_cited_articles": 0,
            "our_avg_rank_in_response": None,
            "corpus_citations": int(corpus.get("citations") or 0),
            "corpus_cited_articles": int(corpus.get("cited_articles") or 0),
            "corpus_avg_rank_in_response": _round(corpus.get("avg_rank_in_response")),
            "owner_baseline_detection_rate": None,
        })

    north_star = {
        "label": "我方已发布 URL 被引数",
        "published_urls": published_urls,
        "crawled_urls": int(north_row.get("crawled_urls") or 0),
        "cited_urls": cited_urls,
        "total_citations": int(north_row.get("total_citations") or 0),
        "cited_url_ratio": round(cited_urls / published_urls, 4) if published_urls else None,
        "avg_rank_in_response": _round(north_row.get("avg_rank_in_response")),
        "owner_baseline": {"cited_urls": 1, "published_urls": 185, "as_of": "2026-07-26"},
    }

    return {
        "version": WRITING_EFFECTIVENESS_REPORT_VERSION,
        "available": True,
        "window_days": window,
        "industry": str(industry or ""),
        "north_star": north_star,
        "by_style": [_normalize_group(r, "style_code") for r in by_style],
        "by_length_bucket": [_normalize_group(r, "length_bucket") for r in by_length],
        "by_domain": [_normalize_group(r, "domain") for r in by_domain],
        "by_engine": engines,
        "governance_note": (
            "本报告只呈现观测，不自动调整任何文体配比或价格；配比涉及收入结构，"
            "由有权限的人在系统设置里决定。"
        ),
    }


def _round(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _normalize_group(row: dict[str, Any], key: str) -> dict[str, Any]:
    published = int(row.get("published_urls") or 0)
    cited = int(row.get("cited_urls") or 0)
    return {
        key: row.get(key),
        "published_urls": published,
        "cited_urls": cited,
        "total_citations": int(row.get("total_citations") or 0),
        "cited_url_ratio": round(cited / published, 4) if published else None,
        "citations_per_url": round(int(row.get("total_citations") or 0) / published, 3) if published else None,
        "avg_rank_in_response": _round(row.get("avg_rank_in_response")),
    }


# ---------------------------------------------------------------------------
# persistence (report row only — never touches ratios)
# ---------------------------------------------------------------------------
def persist_writing_effectiveness_report(report: dict[str, Any], *, report_key: str) -> dict[str, Any]:
    from db.connection import get_db
    from psycopg2.extras import Json

    if not report.get("available"):
        return {"stored": False, "reason": report.get("reason") or "report_unavailable"}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO writing_effectiveness_reports
                       (report_key, report_version, window_days, industry, payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (report_key) DO UPDATE
                   SET report_version = EXCLUDED.report_version,
                       window_days    = EXCLUDED.window_days,
                       industry       = EXCLUDED.industry,
                       payload        = EXCLUDED.payload,
                       generated_at   = NOW()
             RETURNING id
                """,
                (
                    str(report_key),
                    str(report.get("version") or WRITING_EFFECTIVENESS_REPORT_VERSION),
                    int(report.get("window_days") or DEFAULT_WINDOW_DAYS),
                    str(report.get("industry") or ""),
                    Json(report, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str)),
                ),
            )
            row = cur.fetchone()
        return {"stored": True, "id": int((row or {}).get("id") or 0), "report_key": report_key}
    except Exception as exc:
        logger.warning("[writing-effectiveness] persist failed: %s", str(exc)[:200])
        return {"stored": False, "reason": f"persist_failed:{type(exc).__name__}"}


def get_latest_writing_effectiveness_report() -> dict[str, Any] | None:
    from db.connection import get_connection

    try:
        conn = get_connection()
    except Exception:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT report_key, report_version, window_days, industry, payload, generated_at
              FROM writing_effectiveness_reports
             ORDER BY generated_at DESC
             LIMIT 1
            """
        )
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.warning("[writing-effectiveness] read failed: %s", str(exc)[:200])
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
