"""Distill mhz media inventory into media_effective_pool.

The V2.3 recommendation layer treats mhz_media/mhz_wemedia as raw inventory
and writes L0/L1/L2 scoring into media_effective_pool. It intentionally does
not read media_outlets.geo_confirmed because that field is import/marketing
noise, not an outcome signal.
"""

from __future__ import annotations

import argparse
import statistics
from typing import Any

from db.connection import get_connection
from db.publish_db import count_publish_order_samples, init_publish_tables, upsert_media_effective_pool
from services.publish_recommendation import score_media_candidate


def _fetch_citation_platforms() -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                platform,
                COALESCE(industry, '') AS industry,
                MAX(citation_rate) AS citation_rate
            FROM geo_engine_stats
            WHERE platform IS NOT NULL
              AND platform != ''
              AND citation_rate IS NOT NULL
            GROUP BY platform, COALESCE(industry, '')
        """)
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _best_citation(row: dict[str, Any], platforms: list[dict[str, Any]]) -> tuple[float, str]:
    name = str(row.get("media_name") or row.get("platform_name") or "").replace(" ", "")
    best = 0.0
    industry = row.get("industry") or ""
    for item in platforms:
        platform = str(item.get("platform") or "").replace(" ", "")
        if not platform or platform not in name:
            continue
        rate = float(item.get("citation_rate") or 0)
        if rate > best:
            best = rate
            industry = item.get("industry") or industry
    return best, industry


def _fetch_raw_media() -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                'media' AS media_source,
                id AS media_id,
                media_name,
                media_name AS platform_name,
                resource_type_name AS industry,
                portal_media,
                inclusion_rate,
                pc_weight,
                m_weight,
                geo_rank,
                authority_media,
                our_price_yuan,
                our_price_points,
                price,
                price1
            FROM mhz_media
            WHERE is_active = TRUE
        """)
        media = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT
                'wemedia' AS media_source,
                id AS media_id,
                toutiao_name AS media_name,
                toutiao_name AS platform_name,
                industry,
                platform AS portal_media,
                0 AS inclusion_rate,
                0 AS pc_weight,
                0 AS m_weight,
                geo_rank,
                authority_media,
                our_price_yuan,
                our_price_points,
                price,
                price1
            FROM mhz_wemedia
            WHERE is_active = TRUE
        """)
        media.extend(dict(r) for r in cur.fetchall())
        return media
    finally:
        conn.close()


def _median_price(rows: list[dict[str, Any]], source: str) -> float:
    prices = []
    for row in rows:
        if row.get("media_source") != source:
            continue
        price = float(row.get("our_price_yuan") or row.get("price") or row.get("price1") or 0)
        if price > 0:
            prices.append(price)
    return float(statistics.median(prices)) if prices else 0.0


def distill(limit: int | None = None) -> dict[str, int]:
    init_publish_tables()
    raw_rows = _fetch_raw_media()
    if limit:
        raw_rows = raw_rows[:limit]
    platforms = _fetch_citation_platforms()
    sample_count = count_publish_order_samples()
    media_median = _median_price(raw_rows, "media")
    wemedia_median = _median_price(raw_rows, "wemedia")

    scored_rows = []
    counts = {"total": 0, "L0": 0, "L1": 0, "L2": 0, "recommendable": 0}
    for row in raw_rows:
        citation_rate, industry = _best_citation(row, platforms)
        row["citation_rate"] = citation_rate
        row["industry"] = row.get("industry") or industry
        source = row.get("media_source") or "media"
        scored = score_media_candidate(
            row,
            sample_count=sample_count,
            median_price_yuan=wemedia_median if source == "wemedia" else media_median,
            industry=row.get("industry") or "",
        )
        scored_rows.append(scored)
        tier = scored.get("tier") or "L0"
        counts["total"] += 1
        counts[tier] = counts.get(tier, 0) + 1
        if scored.get("is_recommendable"):
            counts["recommendable"] += 1

    written = upsert_media_effective_pool(scored_rows)
    counts["written"] = written
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Optional local smoke limit")
    args = parser.parse_args()
    result = distill(limit=args.limit)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
