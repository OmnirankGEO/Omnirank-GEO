"""Backfill article bodies for answer-adopted GEO source URLs.

R6 article-structure analysis needs full article bodies plus a citation link
back to geo_research_raw.  The R5 answer-adoption bridge created source
signals, but many adopted URLs still have no body in geo_research_articles.

This script is dry-run by default.  Pass --full to crawl missing bodies with the
existing Jina crawler and write only research article/citation shadow tables.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402
from services.media_entity_flywheel import (  # noqa: E402
    industry_filter_values,
    normalize_domain,
    normalize_industry_key,
)
from services.research_monitor.content_classifier import get_content_type  # noqa: E402
from services.research_monitor.content_hash import compute_content_hash  # noqa: E402
from services.research_monitor.crawler import crawl_article, should_pre_filter_url  # noqa: E402
from services.research_monitor.domain_tiering import get_domain_tier  # noqa: E402
from services.research_monitor.url_normalizer import compute_url_hash, normalize_url  # noqa: E402


DEFAULT_LIMIT = 50000
MAX_LIMIT = 200000
DEFAULT_MIN_CHARS = 500
DEFAULT_DOMAIN_DELAY_SECONDS = 0.75
ALLOWED_PLATFORMS = {"doubao", "deepseek", "qwen", "kimi"}


@dataclass
class AdoptedCitation:
    raw_id: int
    batch_id: str
    round_id: str
    engine: str
    rank_in_response: int | None
    title: str = ""
    created_at: Any = None


@dataclass
class AdoptedUrlCandidate:
    url: str
    normalized_url: str
    url_hash: str
    domain: str
    industry_key: str
    raw_industries: set[str] = field(default_factory=set)
    title: str = ""
    citations: list[AdoptedCitation] = field(default_factory=list)


def _normalize_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if int(limit) <= 0:
        return 0
    return min(int(limit), MAX_LIMIT)


def _limit_clause(limit: int) -> tuple[str, list[Any]]:
    if limit <= 0:
        return "", []
    return "LIMIT %s", [limit]


def _body_len(row: dict[str, Any] | None) -> int:
    if not row:
        return 0
    cleaned = row.get("cleaned_char_count")
    if cleaned is not None:
        try:
            return int(cleaned or 0)
        except (TypeError, ValueError):
            return 0
    return len(str(row.get("inline_cleaned_content") or ""))


def _has_usable_body(row: dict[str, Any] | None, min_chars: int) -> bool:
    if not row:
        return False
    if row.get("review_status") == "rejected" or row.get("clean_status") == "failed":
        return False
    return _body_len(row) >= min_chars


def _safe_platform(engine: str) -> str:
    platform = (engine or "").strip().lower()
    return platform if platform in ALLOWED_PLATFORMS else "deepseek"


def _group_candidates(rows: list[dict[str, Any]]) -> list[AdoptedUrlCandidate]:
    grouped: dict[tuple[str, str], AdoptedUrlCandidate] = {}
    for row in rows:
        raw_url = str(row.get("cite_url") or row.get("source_url") or "").strip()
        if not raw_url:
            continue
        normalized = normalize_url(raw_url)
        if not normalized:
            continue
        industry_key = normalize_industry_key(row.get("industry_key") or row.get("industry") or "")
        key = (normalized, industry_key)
        candidate = grouped.get(key)
        if not candidate:
            candidate = AdoptedUrlCandidate(
                url=raw_url,
                normalized_url=normalized,
                url_hash=compute_url_hash(normalized),
                domain=normalize_domain(normalized),
                industry_key=industry_key,
                title=str(row.get("cite_title") or "")[:500],
            )
            grouped[key] = candidate
        if row.get("industry"):
            candidate.raw_industries.add(str(row.get("industry")))
        if not candidate.title and row.get("cite_title"):
            candidate.title = str(row.get("cite_title"))[:500]
        candidate.citations.append(AdoptedCitation(
            raw_id=int(row["raw_id"]),
            batch_id=str(row.get("batch_id") or ""),
            round_id=str(row.get("round_id") or ""),
            engine=_safe_platform(str(row.get("engine") or "")),
            rank_in_response=row.get("rank_in_response"),
            title=str(row.get("cite_title") or ""),
            created_at=row.get("created_at"),
        ))
    return list(grouped.values())


def load_adopted_raw_rows(industry: str = "", batch_id: str = "", limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Load raw rows that already have explicit answer-adoption evidence."""
    params: list[Any] = []
    where = """
    WHERE COALESCE(raw.cite_url, '') <> ''
      AND (COALESCE(raw.is_answer_cited, FALSE) = TRUE OR raw.adoption_rank IS NOT NULL)
      AND EXISTS (
            SELECT 1
              FROM geo_research_source_signals sig
             WHERE sig.source_url = raw.cite_url
               AND sig.signal_tier = 'answer_adopted'
      )
    """
    if industry:
        values = industry_filter_values(industry)
        normalized = normalize_industry_key(industry)
        where += " AND (raw.industry = ANY(%s) OR %s = %s)"
        params.extend([values, normalized, normalize_industry_key(industry)])
    if batch_id:
        where += " AND raw.batch_id = %s"
        params.append(batch_id)

    limit = _normalize_limit(limit)
    limit_sql, limit_params = _limit_clause(limit)
    params.extend(limit_params)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT raw.id AS raw_id,
                   raw.cite_url,
                   raw.cite_title,
                   raw.industry,
                   raw.engine,
                   raw.batch_id AS batch_id,
                   rnd.round_id AS round_id,
                   raw.cite_position AS rank_in_response,
                   raw.created_at,
                   (
                     SELECT sig.industry_key
                       FROM geo_research_source_signals sig
                      WHERE sig.source_url = raw.cite_url
                        AND sig.signal_tier = 'answer_adopted'
                      ORDER BY sig.observed_at DESC NULLS LAST, sig.id DESC
                      LIMIT 1
                   ) AS industry_key
              FROM geo_research_raw raw
              LEFT JOIN geo_research_round rnd ON rnd.batch_id = raw.batch_id
              {where}
             ORDER BY raw.created_at DESC NULLS LAST, raw.id DESC
             {limit_sql}
        """, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _select_same_industry_article(cur: Any, candidate: AdoptedUrlCandidate) -> dict[str, Any] | None:
    values = industry_filter_values(candidate.industry_key) or [candidate.industry_key]
    cur.execute("""
        SELECT id, url, url_hash, domain, title, primary_industry,
               raw_char_count, cleaned_char_count, content_hash,
               domain_tier, content_type, inline_cleaned_content,
               clean_status, review_status
          FROM geo_research_articles
         WHERE url_hash = %s
           AND primary_industry = ANY(%s)
         ORDER BY cleaned_char_count DESC NULLS LAST, id ASC
         LIMIT 1
    """, (candidate.url_hash, values))
    row = cur.fetchone()
    return dict(row) if row else None


def _select_any_article(cur: Any, candidate: AdoptedUrlCandidate) -> dict[str, Any] | None:
    cur.execute("""
        SELECT id, url, url_hash, domain, title, primary_industry,
               raw_char_count, cleaned_char_count, content_hash,
               domain_tier, content_type, inline_cleaned_content,
               clean_status, review_status
          FROM geo_research_articles
         WHERE url_hash = %s
         ORDER BY cleaned_char_count DESC NULLS LAST, id ASC
         LIMIT 1
    """, (candidate.url_hash,))
    row = cur.fetchone()
    return dict(row) if row else None


def _derive_backfill_round_id(batch_id: str) -> str:
    suffix = (batch_id or "").strip()
    if suffix.startswith("batch_round_"):
        suffix = suffix[len("batch_"):]
    suffix = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in suffix)
    if not suffix:
        suffix = "unknown"
    return f"round_r6b_{suffix}"[:50]


def _ensure_round_for_batch(cur: Any, batch_id: str, counters: Counter[str]) -> str:
    batch_id = (batch_id or "").strip()
    if not batch_id:
        return ""
    cur.execute("""
        SELECT round_id
          FROM geo_research_round
         WHERE batch_id = %s
         ORDER BY started_at DESC NULLS LAST, id DESC
         LIMIT 1
    """, (batch_id,))
    row = cur.fetchone()
    if row:
        return str(row["round_id"])

    round_id = _derive_backfill_round_id(batch_id)
    summary = {
        "source": "r6b_adopted_url_body_backfill",
        "batch_id": batch_id,
        "note": "Synthetic round for adopted URL body backfill citation FK integrity.",
    }
    cur.execute("""
        INSERT INTO geo_research_round
            (round_id, batch_id, triggered_by, status, current_stage,
             snapshot_json, summary_json, started_at, finished_at, last_heartbeat_at)
        VALUES (%s, %s, 'manual', 'completed', 'r6b_body_backfill',
                %s::jsonb, %s::jsonb, NOW(), NOW(), NOW())
        ON CONFLICT (round_id) DO NOTHING
    """, (
        round_id,
        batch_id,
        json.dumps(summary, ensure_ascii=False),
        json.dumps(summary, ensure_ascii=False),
    ))
    if cur.rowcount == 1:
        counters["created_backfill_rounds"] += 1
    cur.execute("SELECT round_id FROM geo_research_round WHERE round_id = %s", (round_id,))
    row = cur.fetchone()
    return str(row["round_id"]) if row else ""


def _ensure_candidate_round_ids(cur: Any, candidate: AdoptedUrlCandidate, counters: Counter[str]) -> None:
    for citation in candidate.citations:
        if citation.round_id:
            continue
        citation.round_id = _ensure_round_for_batch(cur, citation.batch_id, counters)


def _insert_citation_links(
    cur: Any,
    article_id: int,
    candidate: AdoptedUrlCandidate,
    counters: Counter[str],
) -> int:
    inserted = 0
    for citation in candidate.citations:
        if not citation.round_id:
            counters["skipped_missing_round"] += 1
            continue
        cur.execute("""
            SELECT 1
              FROM geo_research_article_citations
             WHERE article_id = %s
               AND raw_id = %s
             LIMIT 1
        """, (article_id, citation.raw_id))
        if cur.fetchone():
            continue
        cur.execute("""
            INSERT INTO geo_research_article_citations
                (article_id, round_id, industry_id, prompt_id,
                 raw_id, rank_in_response, platform, cited_at)
            VALUES (%s, %s, NULL, NULL, %s, %s, %s, NOW())
            ON CONFLICT DO NOTHING
        """, (
            article_id,
            citation.round_id,
            citation.raw_id,
            citation.rank_in_response,
            citation.engine,
        ))
        if cur.rowcount == 1:
            inserted += 1
    if inserted:
        cur.execute("""
            UPDATE geo_research_articles
               SET total_citation_count = total_citation_count + %s,
                   last_seen_at = NOW()
             WHERE id = %s
        """, (inserted, article_id))
    return inserted


def _copy_cross_industry_article(cur: Any, candidate: AdoptedUrlCandidate, existing: dict[str, Any]) -> int:
    first_round_id = candidate.citations[0].round_id if candidate.citations else ""
    cur.execute("""
        INSERT INTO geo_research_articles
            (url, url_hash, domain, title, primary_industry,
             raw_char_count, cleaned_char_count, content_hash,
             domain_tier, content_type, inline_cleaned_content,
             clean_status, review_status,
             is_duplicate, primary_article_id,
             total_citation_count, first_seen_round_id,
             last_seen_at, fetched_at)
        VALUES (%s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                TRUE, %s,
                0, %s,
                NOW(), NOW())
        ON CONFLICT DO NOTHING
        RETURNING id
    """, (
        existing.get("url") or candidate.normalized_url,
        candidate.url_hash,
        existing.get("domain") or candidate.domain,
        existing.get("title") or candidate.title,
        candidate.industry_key,
        existing.get("raw_char_count") or _body_len(existing),
        existing.get("cleaned_char_count") or _body_len(existing),
        existing.get("content_hash"),
        existing.get("domain_tier") or "gray",
        existing.get("content_type") or "article",
        existing.get("inline_cleaned_content") or "",
        existing.get("clean_status") or "cleaned",
        "in_library" if existing.get("review_status") != "rejected" else "crawled",
        existing["id"],
        first_round_id,
    ))
    row = cur.fetchone()
    if row:
        return int(row["id"])
    same = _select_same_industry_article(cur, candidate)
    return int(same["id"]) if same else int(existing["id"])


def _upsert_crawled_article(cur: Any, candidate: AdoptedUrlCandidate, body: str, title: str) -> tuple[int, str]:
    content_hash = compute_content_hash(body)
    char_count = len(body)
    domain = urlparse(candidate.normalized_url).netloc or candidate.domain
    content_type = get_content_type(domain, candidate.normalized_url, char_count)
    domain_tier = get_domain_tier(domain)
    first_round_id = candidate.citations[0].round_id if candidate.citations else ""
    same = _select_same_industry_article(cur, candidate)
    if same:
        cur.execute("""
            UPDATE geo_research_articles
               SET title = COALESCE(NULLIF(title, ''), %s),
                   domain = COALESCE(NULLIF(domain, ''), %s),
                   raw_char_count = %s,
                   cleaned_char_count = %s,
                   content_hash = %s,
                   domain_tier = COALESCE(NULLIF(domain_tier, ''), %s),
                   content_type = COALESCE(NULLIF(content_type, ''), %s),
                   inline_cleaned_content = %s,
                   clean_status = 'cleaned',
                   clean_model = 'r6b_jina_backfill_v1',
                   review_status = CASE
                       WHEN review_status = 'rejected' THEN review_status
                       ELSE 'in_library'
                   END,
                   last_seen_at = NOW(),
                   fetched_at = NOW()
             WHERE id = %s
        """, (
            title or candidate.title,
            domain,
            char_count,
            char_count,
            content_hash,
            domain_tier,
            content_type,
            body,
            same["id"],
        ))
        return int(same["id"]), "updated_existing_body"

    cur.execute("""
        SELECT id
          FROM geo_research_articles
         WHERE content_hash = %s
         ORDER BY id ASC
         LIMIT 1
    """, (content_hash,))
    primary = cur.fetchone()
    primary_id = int(primary["id"]) if primary else None
    cur.execute("""
        INSERT INTO geo_research_articles
            (url, url_hash, domain, title, primary_industry,
             raw_char_count, cleaned_char_count, content_hash,
             domain_tier, content_type, inline_cleaned_content,
             clean_status, clean_model, review_status,
             is_duplicate, primary_article_id,
             total_citation_count, first_seen_round_id,
             last_seen_at, fetched_at)
        VALUES (%s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                'cleaned', 'r6b_jina_backfill_v1', 'in_library',
                %s, %s,
                0, %s,
                NOW(), NOW())
        ON CONFLICT DO NOTHING
        RETURNING id
    """, (
        candidate.normalized_url,
        candidate.url_hash,
        domain,
        title or candidate.title,
        candidate.industry_key,
        char_count,
        char_count,
        content_hash,
        domain_tier,
        content_type,
        body,
        primary_id is not None,
        primary_id,
        first_round_id,
    ))
    row = cur.fetchone()
    if row:
        return int(row["id"]), "crawled_new"
    same = _select_same_industry_article(cur, candidate)
    if same:
        return int(same["id"]), "reused_same_industry"
    any_article = _select_any_article(cur, candidate)
    return (int(any_article["id"]), "reused_other_industry") if any_article else (0, "failed_conflict")


async def _throttle_domain(domain: str, last_seen: dict[str, float], delay_seconds: float) -> None:
    if delay_seconds <= 0:
        return
    now = time.monotonic()
    previous = last_seen.get(domain)
    if previous is not None:
        wait = delay_seconds - (now - previous)
        if wait > 0:
            await asyncio.sleep(wait)
    last_seen[domain] = time.monotonic()


async def backfill(
    industry: str = "",
    batch_id: str = "",
    limit: int = DEFAULT_LIMIT,
    min_chars: int = DEFAULT_MIN_CHARS,
    domain_delay_seconds: float = DEFAULT_DOMAIN_DELAY_SECONDS,
    dry_run: bool = True,
) -> dict[str, Any]:
    min_chars = max(0, int(min_chars or 0))
    limit = _normalize_limit(limit)
    rows = load_adopted_raw_rows(industry=industry, batch_id=batch_id, limit=limit)
    candidates = _group_candidates(rows)
    by_domain = Counter(candidate.domain for candidate in candidates)
    by_industry = Counter(candidate.industry_key for candidate in candidates)
    counters: Counter[str] = Counter()
    preview: list[dict[str, Any]] = []

    conn = get_connection()
    try:
        cur = conn.cursor()
        for candidate in candidates:
            same = _select_same_industry_article(cur, candidate)
            other = None if same else _select_any_article(cur, candidate)
            if _has_usable_body(same, min_chars):
                action = "reused_same_industry"
            elif _has_usable_body(other, min_chars):
                action = "copy_cross_industry"
            elif same and same.get("review_status") == "rejected":
                action = "skipped_rejected"
            else:
                action = "crawl"
            counters[action] += 1
            if len(preview) < 50:
                preview.append({
                    "url": candidate.normalized_url,
                    "domain": candidate.domain,
                    "industry_key": candidate.industry_key,
                    "raw_count": len(candidate.citations),
                    "action": action,
                    "existing_article_id": same.get("id") if same else None,
                })
    finally:
        conn.close()

    written_articles = 0
    citations_written = 0
    last_domain_seen: dict[str, float] = defaultdict(float)

    if not dry_run:
        for candidate in candidates:
            prefilter = should_pre_filter_url(candidate.normalized_url, check_robots=True)
            if prefilter:
                counters[f"skipped_prefilter_{prefilter.value}"] += 1
                continue

            conn = get_connection()
            try:
                cur = conn.cursor()
                _ensure_candidate_round_ids(cur, candidate, counters)
                same = _select_same_industry_article(cur, candidate)
                if _has_usable_body(same, min_chars):
                    citations_written += _insert_citation_links(cur, int(same["id"]), candidate, counters)
                    conn.commit()
                    counters["linked_reused_same_industry"] += 1
                    continue

                other = _select_any_article(cur, candidate)
                if _has_usable_body(other, min_chars):
                    article_id = _copy_cross_industry_article(cur, candidate, other)
                    citations_written += _insert_citation_links(cur, article_id, candidate, counters)
                    conn.commit()
                    written_articles += 1
                    counters["copied_cross_industry"] += 1
                    continue
                conn.rollback()
            finally:
                conn.close()

            await _throttle_domain(candidate.domain, last_domain_seen, domain_delay_seconds)
            result = await crawl_article(candidate.normalized_url)
            if not result.get("ok"):
                counters["skipped_jina_failed"] += 1
                continue
            body = str(result.get("content") or "")
            if len(body) < min_chars:
                counters["skipped_short"] += 1
                continue

            conn = get_connection()
            try:
                cur = conn.cursor()
                _ensure_candidate_round_ids(cur, candidate, counters)
                article_id, action = _upsert_crawled_article(
                    cur,
                    candidate,
                    body=body,
                    title=str(result.get("title") or candidate.title),
                )
                if article_id:
                    citations_written += _insert_citation_links(cur, article_id, candidate, counters)
                    conn.commit()
                    if action in {"crawled_new", "updated_existing_body"}:
                        written_articles += 1
                    counters[action] += 1
                else:
                    conn.rollback()
                    counters["failed_article_upsert"] += 1
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    return {
        "status": "success",
        "dry_run": dry_run,
        "shadow_only": True,
        "production_takeover": False,
        "source_table": "geo_research_raw + geo_research_source_signals",
        "target_table": "geo_research_articles + geo_research_article_citations",
        "loaded": len(rows),
        "candidate_urls": len(candidates),
        "would_crawl": int(counters.get("crawl", 0)),
        "would_reuse_same_industry": int(counters.get("reused_same_industry", 0)),
        "would_copy_cross_industry": int(counters.get("copy_cross_industry", 0)),
        "written_articles": written_articles,
        "citations_written": citations_written,
        "crawled_new": int(counters.get("crawled_new", 0)),
        "reused_same_industry": int(counters.get("linked_reused_same_industry", 0)),
        "copied_cross_industry": int(counters.get("copied_cross_industry", 0)),
        "updated_existing_body": int(counters.get("updated_existing_body", 0)),
        "skipped_short": int(counters.get("skipped_short", 0)),
        "skipped_jina_failed": int(counters.get("skipped_jina_failed", 0)),
        "skipped_missing_round": int(counters.get("skipped_missing_round", 0)),
        "created_backfill_rounds": int(counters.get("created_backfill_rounds", 0)),
        "by_domain": dict(by_domain.most_common(30)),
        "by_industry": dict(by_industry),
        "cap_hit": bool(limit > 0 and len(rows) >= limit),
        "limit": limit,
        "min_chars": min_chars,
        "domain_delay_seconds": domain_delay_seconds,
        "preview": preview,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--industry", default="")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="0 means no SQL LIMIT")
    parser.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS)
    parser.add_argument("--domain-delay-seconds", type=float, default=DEFAULT_DOMAIN_DELAY_SECONDS)
    parser.add_argument("--full", action="store_true", help="crawl and write; default is dry-run")
    args = parser.parse_args()
    result = asyncio.run(backfill(
        industry=args.industry,
        batch_id=args.batch_id,
        limit=args.limit,
        min_chars=args.min_chars,
        domain_delay_seconds=args.domain_delay_seconds,
        dry_run=not args.full,
    ))
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
