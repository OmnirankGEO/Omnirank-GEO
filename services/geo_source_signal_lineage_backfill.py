"""§4 血缘回填:`geo_research_source_signals.article_id`(工单 B 2026-07-27)。

背景:该表生产 71403 行,`article_id` **全空**。后续做"哪篇文章被哪个引擎在哪个行业
引用过"的结构研究,只能每次现场 `JOIN ... ON url_hash` 靠技巧凑,outcome 归因也没有
稳定的落脚点。这个 job 把这条边一次性焊死,并保持幂等,新增行随夜里那一趟自动补上。

**匹配口径以生产真实唯一键为准,不以建表语句为准**(SQL 4 维核验第 1 条的教训):

    建表语句写的是 `url_hash CHAR(40) NOT NULL UNIQUE`;
    生产真实索引是 `UNIQUE (url_hash, primary_industry)`
    —— 所以同一个 URL 在不同行业下**合法地存在多行**(实测 161 组同 hash 多行)。
    按 url_hash 单键盲 JOIN 会在这些组上随机挑一篇写进血缘,把地基浇歪。

于是分两档,都只写 `article_id IS NULL` 的行(幂等的来源):

  1. `exact`    —— 按 (url_hash, industry_key) = (url_hash, primary_industry) 匹配。
                   这就是生产唯一键,命中至多一篇,零歧义。
  2. `url_only` —— 剩下的行按 url_hash 匹配,且**只认全库该 hash 只有一篇文章**的情形。
                   一个 hash 对多篇、行业又对不上 → 宁可留空,也不猜。

留空的行会在返回值里如实报数(`ambiguous`),不算失败也不假装成功。

只读 `geo_research_articles`,只写 `geo_research_source_signals.article_id` 一列。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("GEO-SourceSignalLineage")

DEFAULT_BATCH_SIZE = 5000
DEFAULT_MAX_BATCHES = 200

# 档 1:生产唯一键 (url_hash, primary_industry) —— 命中至多一篇。
_SQL_EXACT = """
    WITH target AS (
        SELECT s.id, a.id AS article_id
          FROM geo_research_source_signals s
          JOIN geo_research_articles a
            ON a.url_hash = s.url_hash
           AND a.primary_industry = s.industry_key
         WHERE s.article_id IS NULL
           AND s.url_hash IS NOT NULL
         ORDER BY s.id
         LIMIT %s
    )
    UPDATE geo_research_source_signals s
       SET article_id = t.article_id
      FROM target t
     WHERE s.id = t.id
       AND s.article_id IS NULL
"""

# 档 2:行业对不上时退到 url_hash,但只认"全库该 hash 唯一一篇"的无歧义情形。
_SQL_URL_ONLY = """
    WITH unique_hash AS (
        SELECT url_hash, MIN(id) AS article_id
          FROM geo_research_articles
         WHERE url_hash IS NOT NULL
         GROUP BY url_hash
        HAVING COUNT(*) = 1
    ),
    target AS (
        SELECT s.id, u.article_id
          FROM geo_research_source_signals s
          JOIN unique_hash u ON u.url_hash = s.url_hash
         WHERE s.article_id IS NULL
           AND s.url_hash IS NOT NULL
         ORDER BY s.id
         LIMIT %s
    )
    UPDATE geo_research_source_signals s
       SET article_id = t.article_id
      FROM target t
     WHERE s.id = t.id
       AND s.article_id IS NULL
"""

_SQL_REMAINING = """
    SELECT
        COUNT(*)                                                      AS pending,
        COUNT(*) FILTER (
            WHERE EXISTS (
                SELECT 1 FROM geo_research_articles a
                 WHERE a.url_hash = s.url_hash
            )
        )                                                             AS ambiguous
      FROM geo_research_source_signals s
     WHERE s.article_id IS NULL
"""


def _run_phase(cur, sql: str, batch_size: int, max_batches: int) -> tuple[int, int]:
    """跑一档到收敛。返回 (写入行数, 用掉的批次数)。"""
    written = 0
    batches = 0
    while batches < max_batches:
        cur.execute(sql, (batch_size,))
        affected = cur.rowcount or 0
        batches += 1
        written += affected
        if affected < batch_size:
            break
    return written, batches


def backfill_source_signal_article_ids(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """回填 article_id。幂等:重复跑只会处理仍为空的行,已写的行一行不动。

    dry_run=True 只统计不写(用于部署前先看一眼盘子有多大)。
    """
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        if dry_run:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE s.article_id IS NULL)          AS pending,
                    COUNT(*) FILTER (
                        WHERE s.article_id IS NULL AND EXISTS (
                            SELECT 1 FROM geo_research_articles a
                             WHERE a.url_hash = s.url_hash
                               AND a.primary_industry = s.industry_key
                        )
                    )                                                     AS exact_matchable
                  FROM geo_research_source_signals s
                """
            )
            row = cur.fetchone() or {}
            return {
                "dry_run": True,
                "pending": int(row.get("pending") or 0),
                "exact_matchable": int(row.get("exact_matchable") or 0),
            }

        exact_written, exact_batches = _run_phase(cur, _SQL_EXACT, batch_size, max_batches)
        url_written, url_batches = _run_phase(cur, _SQL_URL_ONLY, batch_size, max_batches)
        conn.commit()

        cur.execute(_SQL_REMAINING)
        row = cur.fetchone() or {}
        result = {
            "written": exact_written + url_written,
            "written_exact": exact_written,
            "written_url_only": url_written,
            "batches": exact_batches + url_batches,
            "pending": int(row.get("pending") or 0),
            # 仍能在文章表找到同 hash、却因行业对不上且一 hash 多篇而不敢写的行。
            # 不是错误,是**诚实的留空**:宁可没有血缘,也不要错的血缘。
            "ambiguous": int(row.get("ambiguous") or 0),
        }

    if result["batches"] >= 2 * max_batches:
        # 批次用满 = 这一趟没跑完,明晚接着跑。说出来,不要让"没跑完"看起来像"跑完了"。
        logger.warning(
            "[SourceSignalLineage] 批次上限用满,本轮未收敛(written=%s pending=%s)",
            result["written"], result["pending"],
        )
    return result
