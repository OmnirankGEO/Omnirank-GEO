"""DB helpers for real answer-adoption metric snapshots."""

from __future__ import annotations

import json
from typing import Any

from psycopg2.extras import Json

from db.connection import get_connection, get_db
from services.media_entity_flywheel import is_all_industry_scope


def _jsonb(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str))


def init_answer_adoption_metric_tables() -> None:
    """Create the answer-adoption metric table idempotently."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_answer_adoption_metrics (
                id BIGSERIAL PRIMARY KEY,
                source_url TEXT NOT NULL,
                domain VARCHAR(300),
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                engine VARCHAR(40) NOT NULL DEFAULT '',
                prompt_id TEXT NOT NULL DEFAULT '',
                round_id VARCHAR(80) NOT NULL DEFAULT '',
                signal_tier VARCHAR(40) NOT NULL,
                answer_adopted BOOLEAN NOT NULL DEFAULT FALSE,
                explicit_cited BOOLEAN NOT NULL DEFAULT FALSE,
                search_exposed BOOLEAN NOT NULL DEFAULT FALSE,
                reference_only BOOLEAN NOT NULL DEFAULT FALSE,
                rejected_noise BOOLEAN NOT NULL DEFAULT FALSE,
                source_position INTEGER DEFAULT 0,
                total_sources_in_answer INTEGER DEFAULT 1,
                normalized_credit NUMERIC(12,6) NOT NULL DEFAULT 0,
                metadata JSONB DEFAULT '{}'::jsonb,
                observed_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (source_url, industry_key, engine, prompt_id, signal_tier, round_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_answer_metrics_industry ON geo_answer_adoption_metrics(industry_key, normalized_credit DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_answer_metrics_tier ON geo_answer_adoption_metrics(signal_tier)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_answer_metrics_domain ON geo_answer_adoption_metrics(domain)")


def upsert_answer_adoption_metrics(metrics: list[dict[str, Any]]) -> int:
    if not metrics:
        return 0
    with get_db() as conn:
        cur = conn.cursor()
        cur.executemany("""
            INSERT INTO geo_answer_adoption_metrics (
                source_url, domain, industry_key, engine, prompt_id, round_id,
                signal_tier, answer_adopted, explicit_cited, search_exposed,
                reference_only, rejected_noise, source_position,
                total_sources_in_answer, normalized_credit, metadata,
                observed_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), NOW())
            ON CONFLICT (source_url, industry_key, engine, prompt_id, signal_tier, round_id)
            DO UPDATE SET
                domain = EXCLUDED.domain,
                answer_adopted = EXCLUDED.answer_adopted,
                explicit_cited = EXCLUDED.explicit_cited,
                search_exposed = EXCLUDED.search_exposed,
                reference_only = EXCLUDED.reference_only,
                rejected_noise = EXCLUDED.rejected_noise,
                source_position = EXCLUDED.source_position,
                total_sources_in_answer = EXCLUDED.total_sources_in_answer,
                normalized_credit = EXCLUDED.normalized_credit,
                metadata = EXCLUDED.metadata,
                observed_at = EXCLUDED.observed_at,
                updated_at = NOW()
        """, [
            (
                item.get("source_url") or "",
                item.get("domain") or "",
                item.get("industry_key") or "general",
                item.get("engine") or "",
                item.get("prompt_id") or "",
                item.get("round_id") or "",
                item.get("signal_tier") or "crawled_reference_only",
                bool(item.get("answer_adopted")),
                bool(item.get("explicit_cited")),
                bool(item.get("search_exposed")),
                bool(item.get("reference_only")),
                bool(item.get("rejected_noise")),
                int(item.get("source_position") or 0),
                max(1, int(item.get("total_sources_in_answer") or 1)),
                item.get("normalized_credit") or 0,
                _jsonb(item.get("metadata") or {}),
                item.get("observed_at"),
            )
            for item in metrics
        ])
        return len(metrics)


def list_answer_adoption_metric_summary(industry_key: str = "", limit: int = 20) -> dict[str, Any]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        scoped_industry = "" if is_all_industry_scope(industry_key) else industry_key
        where = "WHERE industry_key = %s" if scoped_industry else ""
        params: list[Any] = [scoped_industry] if scoped_industry else []
        cur.execute(f"""
            SELECT COUNT(*) AS total_rows,
                   COUNT(DISTINCT source_url) AS unique_sources,
                   COUNT(DISTINCT engine) AS engine_count,
                   COUNT(DISTINCT prompt_id) AS prompt_count,
                   COUNT(*) FILTER (WHERE answer_adopted) AS answer_adopted_rows,
                   COUNT(*) FILTER (WHERE explicit_cited) AS explicit_cited_rows,
                   COUNT(*) FILTER (WHERE search_exposed) AS search_exposed_rows,
                   COUNT(*) FILTER (WHERE reference_only) AS reference_only_rows,
                   COUNT(*) FILTER (WHERE rejected_noise) AS rejected_noise_rows,
                   COALESCE(SUM(normalized_credit), 0)::float AS total_credit,
                   MAX(updated_at) AS last_updated_at
              FROM geo_answer_adoption_metrics
              {where}
        """, params)
        summary = dict(cur.fetchone() or {})

        total = max(1, int(summary.get("total_rows") or 0))
        summary["answer_adoption_rate"] = round(float(summary.get("answer_adopted_rows") or 0) / total, 4)
        summary["explicit_citation_rate"] = round(float(summary.get("explicit_cited_rows") or 0) / total, 4)
        summary["search_exposure_rate"] = round(float(summary.get("search_exposed_rows") or 0) / total, 4)

        by_engine_params = list(params)
        by_engine_params.append(limit)
        cur.execute(f"""
            SELECT engine,
                   COUNT(*) AS total_rows,
                   COUNT(DISTINCT source_url) AS unique_sources,
                   COUNT(DISTINCT prompt_id) AS prompt_count,
                   COUNT(*) FILTER (WHERE answer_adopted) AS answer_adopted_rows,
                   COUNT(*) FILTER (WHERE explicit_cited) AS explicit_cited_rows,
                   COUNT(*) FILTER (WHERE search_exposed) AS search_exposed_rows,
                   COALESCE(SUM(normalized_credit), 0)::float AS total_credit
              FROM geo_answer_adoption_metrics
              {where}
             GROUP BY engine
             ORDER BY COUNT(*) FILTER (WHERE answer_adopted) DESC,
                      COUNT(*) FILTER (WHERE explicit_cited) DESC,
                      COALESCE(SUM(normalized_credit), 0) DESC
             LIMIT %s
        """, by_engine_params)
        by_engine = [dict(r) for r in cur.fetchall()]

        top_params = list(params)
        top_params.append(limit)
        cur.execute(f"""
            SELECT domain,
                   MIN(source_url) AS source_url,
                   COUNT(*) AS total_rows,
                   COUNT(DISTINCT engine) AS engine_count,
                   COUNT(DISTINCT prompt_id) AS prompt_count,
                   COUNT(*) FILTER (WHERE answer_adopted) AS answer_adopted_rows,
                   COUNT(*) FILTER (WHERE explicit_cited) AS explicit_cited_rows,
                   COUNT(*) FILTER (WHERE search_exposed) AS search_exposed_rows,
                   COALESCE(SUM(normalized_credit), 0)::float AS total_credit,
                   MAX(updated_at) AS last_updated_at
              FROM geo_answer_adoption_metrics
              {where}
             GROUP BY domain
             ORDER BY COUNT(*) FILTER (WHERE answer_adopted) DESC,
                      COUNT(*) FILTER (WHERE explicit_cited) DESC,
                      COALESCE(SUM(normalized_credit), 0) DESC
             LIMIT %s
        """, top_params)
        top_sources = [dict(r) for r in cur.fetchall()]

        return {
            "summary": summary,
            "by_engine": by_engine,
            "top_sources": top_sources,
        }
    finally:
        conn.close()
