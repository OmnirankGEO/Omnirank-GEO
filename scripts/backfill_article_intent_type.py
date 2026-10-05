"""Backfill geo_research_articles.intent_type with the P15 DeepSeek classifier.

默认只 dry-run, 不写数据库。确认样本输出合理后再加 --apply。
示例:
  python scripts/backfill_article_intent_type.py --limit 50
  python scripts/backfill_article_intent_type.py --apply --limit 500 --concurrency 4
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from db.connection import get_connection
from services.research_monitor.article_intent_classifier import classify_article_intent
from services.research_monitor.oss_helper import download_markdown


logger = logging.getLogger("GEO-ArticleIntentBackfill")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P15 文章意图分类回填脚本")
    parser.add_argument("--apply", action="store_true", help="真正写入数据库; 默认不写")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果, 不写数据库(默认)")
    parser.add_argument("--limit", type=int, default=100, help="最多处理多少篇未分类文章")
    parser.add_argument("--concurrency", type=int, default=4, help="DeepSeek 并发数")
    parser.add_argument("--round-id", default=None, help="只回填某个 round_id 的文章")
    parser.add_argument("--industry", default=None, help="只回填某个 primary_industry")
    return parser.parse_args()


def _fetch_articles(*, limit: int, round_id: str | None, industry: str | None) -> list[dict[str, Any]]:
    where = [
        "clean_status = 'cleaned'",
        "intent_type IS NULL",
        "(expired = FALSE OR expired IS NULL)",
    ]
    params: list[Any] = []
    if round_id:
        where.append("first_seen_round_id = %s")
        params.append(round_id)
    if industry:
        where.append("primary_industry = %s")
        params.append(industry)
    params.append(limit)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, url, domain, title, oss_key_cleaned, inline_cleaned_content
              FROM geo_research_articles
             WHERE {' AND '.join(where)}
             ORDER BY id ASC
             LIMIT %s
            """,
            params,
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def _update_article(article_id: int, result) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_research_articles
               SET intent_type = %s,
                   intent_confidence = %s,
                   intent_reason = %s,
                   intent_model = %s,
                   intent_classified_at = NOW()
             WHERE id = %s
            """,
            (
                result.intent_type,
                result.confidence,
                result.reason,
                result.model,
                article_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def _classify_one(article: dict[str, Any], *, dry_run: bool, semaphore: asyncio.Semaphore) -> str:
    async with semaphore:
        content = ""
        if article.get("oss_key_cleaned"):
            content = download_markdown(article["oss_key_cleaned"]) or ""
        if not content and article.get("inline_cleaned_content"):
            content = article["inline_cleaned_content"] or ""
        if not content:
            return f"SKIP article#{article['id']} no cleaned content"

        result = await classify_article_intent(
            title=article.get("title") or "",
            url=article.get("url") or "",
            domain=article.get("domain") or "",
            content=content,
        )
        if not dry_run:
            _update_article(article["id"], result)
        prefix = "DRY" if dry_run else "OK"
        return (
            f"{prefix} article#{article['id']} "
            f"{result.intent_type} confidence={result.confidence:.3f} reason={result.reason[:80]}"
        )


async def main() -> int:
    args = _parse_args()
    dry_run = (not args.apply) or args.dry_run
    concurrency = max(1, min(args.concurrency, 16))
    limit = max(1, args.limit)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    articles = _fetch_articles(limit=limit, round_id=args.round_id, industry=args.industry)
    logger.info(
        "待处理文章 %s 篇 · dry_run=%s · concurrency=%s",
        len(articles),
        dry_run,
        concurrency,
    )
    semaphore = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(
        *[_classify_one(a, dry_run=dry_run, semaphore=semaphore) for a in articles],
        return_exceptions=True,
    )
    ok = 0
    failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.warning("FAIL %s: %s", type(r).__name__, r)
        else:
            ok += 1
            print(r)
    logger.info("完成 ok=%s failed=%s dry_run=%s", ok, failed, dry_run)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
