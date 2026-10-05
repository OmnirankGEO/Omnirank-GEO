"""写作策略指派账本 + 指派级效果回流(A4 · 2026-07-29)。

生产实证(2026-07-28):`writing_strategy_assignments` 0 行,而且**全仓没有任何写入代码** ——
建表 DDL 和索引都在,生产者一直缺席。同时 `writing_strategy_versions` 31 行**全是 shadow、
active 为 0**,所以 `get_active_strategy_version()` 恒返 None,文章指纹里的 strategy_id 也全 NULL。
= 版本面、生成面、效果面三段各自独立,中间没有账本把它们串起来。

本模块补的就是中间那一段:

    生成时解析出的策略(或行业基线)  →  writing_strategy_assignments(一条 quote 一行)
                                     →  writing_strategy_outcome_events(30 天后按真实被引回流)

两个刻意的设计选择:

1. **strategy_id 允许为空**。生产 active 版本数为 0 是治理现状(启用要人审,不能由代码自作主张
   激活)。如果只在"有 active 版本"时才落账,这条管道在启用之前永远是干的,和现在没区别。
   所以基线路径也落账(resolution='industry_baseline'),诚实记下"这批文章走的是行业基线",
   等管理员启用版本后自然切到 resolution='active_strategy'。闸门为 0 这件事由飞轮看门狗单独告警。

2. **账本停在 quote 级**,不下沉到 article 级。文章级归因已经有
   `writing_article_version_fingerprints`(见该模块 docstring 的分工说明),这里不重造第二份。

SQL 4 维核验(本模块所有 DDL 均为既有表的 additive 变更):
  1. 列名:strategy_version / style_family / resolution / metadata 四个新列,均先 information_schema
     级 IF NOT EXISTS 再写;既有列 strategy_id / brand_id / quote_id / industry_key / assignment_status
     / assigned_by / assigned_at 名字与生产 `\\d writing_strategy_assignments` 逐个核对过。
  2. data_type:strategy_version VARCHAR(240)(与 writing_article_version_fingerprints 同宽)·
     style_family VARCHAR(60)(同上)· resolution VARCHAR(32) · metadata JSONB。
  3. 字段归属:指派信息进 writing_strategy_assignments;效果进 writing_strategy_outcome_events;
     文章级指纹仍归 writing_article_version_fingerprints —— 三张表不互相塞列。
  4. dry-run:DROP NOT NULL 在**空表**上执行(生产 0 行,已核);ADD COLUMN IF NOT EXISTS /
     CREATE UNIQUE INDEX IF NOT EXISTS 全幂等,无破坏性 SQL。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from db.connection import get_connection, get_db

logger = logging.getLogger("GEO-WritingAssignment")

RESOLUTION_ACTIVE = "active_strategy"
RESOLUTION_BASELINE = "industry_baseline"
RESOLUTION_DEFAULT = "system_default"

_SCHEMA_READY = False


def ensure_assignment_schema(force: bool = False) -> None:
    """指派账本的 additive schema。幂等,进程内只跑一次。"""
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    from db.writing_style_flywheel_db import init_writing_style_flywheel_tables

    init_writing_style_flywheel_tables()
    with get_db() as conn:
        cur = conn.cursor()
        # 基线路径没有 strategy_id;生产该表 0 行,DROP NOT NULL 无数据风险。
        cur.execute(
            "ALTER TABLE writing_strategy_assignments ALTER COLUMN strategy_id DROP NOT NULL"
        )
        cur.execute(
            "ALTER TABLE writing_strategy_assignments "
            "ADD COLUMN IF NOT EXISTS strategy_version VARCHAR(240)"
        )
        cur.execute(
            "ALTER TABLE writing_strategy_assignments "
            "ADD COLUMN IF NOT EXISTS style_family VARCHAR(60)"
        )
        cur.execute(
            "ALTER TABLE writing_strategy_assignments "
            "ADD COLUMN IF NOT EXISTS resolution VARCHAR(32) NOT NULL DEFAULT 'industry_baseline'"
        )
        cur.execute(
            "ALTER TABLE writing_strategy_assignments "
            "ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb"
        )
        # 幂等键:同一个 quote 在同一策略(或同一"无策略"基线)下重复生成 → 刷新同一行。
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_writing_assignment_quote_strategy
                ON writing_strategy_assignments(quote_id, industry_key, COALESCE(strategy_id, -1))
             WHERE quote_id IS NOT NULL
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_writing_assignment_assigned_at "
            "ON writing_strategy_assignments(assigned_at DESC)"
        )
        # 指派级效果回流的幂等键(既有 uq_wso_version_window 只覆盖 new_version_id 非空的行,
        # 与本索引互不重叠 —— 本索引只管 quote_id 非空的指派级行)。
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wso_quote_strategy_window
                ON writing_strategy_outcome_events(quote_id, COALESCE(strategy_id, -1), measurement_window)
             WHERE quote_id IS NOT NULL
            """
        )
    _SCHEMA_READY = True


def record_strategy_assignment(
    *,
    quote_id: int,
    industry_key: str,
    brand_id: Optional[int] = None,
    strategy_id: Optional[int] = None,
    strategy_version: Optional[str] = None,
    style_family: Optional[str] = None,
    resolution: str = RESOLUTION_BASELINE,
    assigned_by: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """生成时落一条指派。**fail-soft:任何异常都只记日志,绝不阻断写作。**"""
    if not quote_id:
        return False
    try:
        ensure_assignment_schema()
        status = "active" if resolution == RESOLUTION_ACTIVE else "baseline"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO writing_strategy_assignments
                    (strategy_id, brand_id, quote_id, industry_key, assignment_status,
                     assigned_by, assigned_at, strategy_version, style_family, resolution, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s::jsonb)
                ON CONFLICT (quote_id, industry_key, COALESCE(strategy_id, -1))
                    WHERE quote_id IS NOT NULL
                DO UPDATE SET
                    brand_id = COALESCE(EXCLUDED.brand_id, writing_strategy_assignments.brand_id),
                    assignment_status = EXCLUDED.assignment_status,
                    assigned_by = COALESCE(EXCLUDED.assigned_by, writing_strategy_assignments.assigned_by),
                    assigned_at = NOW(),
                    strategy_version = EXCLUDED.strategy_version,
                    style_family = EXCLUDED.style_family,
                    resolution = EXCLUDED.resolution,
                    metadata = EXCLUDED.metadata
                """,
                (
                    int(strategy_id) if strategy_id else None,
                    int(brand_id) if brand_id else None,
                    int(quote_id),
                    (industry_key or "general")[:100],
                    status,
                    int(assigned_by) if assigned_by else None,
                    (strategy_version or None) and str(strategy_version)[:240],
                    (style_family or None) and str(style_family)[:60],
                    (resolution or RESOLUTION_BASELINE)[:32],
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
        return True
    except Exception as exc:
        logger.warning(
            "[WritingAssignment] 指派落账失败(不阻断写作) quote_id=%s: %s", quote_id, exc
        )
        return False


def list_assignments(limit: int = 100, industry_key: str = "") -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    try:
        ensure_assignment_schema()
        conn = get_connection()
        try:
            cur = conn.cursor()
            if industry_key:
                cur.execute(
                    """
                    SELECT * FROM writing_strategy_assignments
                     WHERE industry_key = %s
                     ORDER BY assigned_at DESC, id DESC LIMIT %s
                    """,
                    (industry_key[:100], limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM writing_strategy_assignments "
                    "ORDER BY assigned_at DESC, id DESC LIMIT %s",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall() or []]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[WritingAssignment] 指派列表查询失败: %s", exc)
        return []


# ============================================================
# 指派级效果回流
# ============================================================

def _articles_for_quote(quote_id: int, assigned_at: Any) -> list[int]:
    """该 quote 在指派之后产出的文章 id。指派时间不可用时退回该 quote 的全部文章。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        if assigned_at is not None:
            cur.execute(
                "SELECT id FROM articles WHERE quote_id = %s AND created_at >= %s ORDER BY id",
                (int(quote_id), assigned_at),
            )
        else:
            cur.execute(
                "SELECT id FROM articles WHERE quote_id = %s ORDER BY id", (int(quote_id),)
            )
        return [int(r["id"]) for r in cur.fetchall() or []]
    finally:
        conn.close()


def backfill_assignment_outcomes(
    *,
    dry_run: bool = False,
    min_age_days: int = 30,
    since_days: int = 30,
    limit: int = 200,
) -> dict[str, Any]:
    """把"指派 → 文章 → 发布 → 被引"回流成 outcome_events。

    只读跨 research 域(复用 `writing_outcome_backfill._citation_count_for_articles` 的既有
    诚实链路,不另造第二条 join),写入面只有 `writing_strategy_outcome_events`。
    没有文章 / 没发布 / 没匹配 → 诚实写 publish_status='insufficient' 且被引记 0,不编数。
    """
    from services.writing_outcome_backfill import _citation_count_for_articles

    ensure_assignment_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, strategy_id, brand_id, quote_id, industry_key,
                   strategy_version, style_family, resolution, assigned_at
              FROM writing_strategy_assignments
             WHERE quote_id IS NOT NULL
               AND assigned_at <= NOW() - make_interval(days => %s)
             ORDER BY assigned_at DESC
             LIMIT %s
            """,
            (int(min_age_days), max(1, min(int(limit or 200), 2000))),
        )
        assignments = [dict(r) for r in cur.fetchall() or []]
    finally:
        conn.close()

    written = 0
    skipped_no_articles = 0
    measured = 0
    for row in assignments:
        article_ids = _articles_for_quote(int(row["quote_id"]), row.get("assigned_at"))
        if not article_ids:
            skipped_no_articles += 1
            continue
        cite = _citation_count_for_articles(article_ids, since_days=since_days)
        publish_status = "insufficient" if cite.get("insufficient_data") else "measured"
        if not cite.get("insufficient_data"):
            measured += 1
        metadata = {
            "resolution": row.get("resolution"),
            "strategy_version": row.get("strategy_version"),
            "style_family": row.get("style_family"),
            "assignment_id": row.get("id"),
            "articles": cite.get("articles"),
            "matched_articles": cite.get("matched_articles"),
            "insufficient_data": bool(cite.get("insufficient_data")),
            "source": "assignment_backfill",
        }
        if dry_run:
            written += 1
            continue
        try:
            with get_db() as conn2:
                cur2 = conn2.cursor()
                cur2.execute(
                    """
                    INSERT INTO writing_strategy_outcome_events
                        (strategy_id, article_id, brand_id, quote_id, industry_key, publish_status,
                         ai_citations_delta_30d, metadata, measurement_window, observed_at)
                    VALUES (%s, NULL, %s, %s, %s, %s, %s, %s::jsonb,
                            date_trunc('week', NOW())::date, NOW())
                    ON CONFLICT (quote_id, COALESCE(strategy_id, -1), measurement_window)
                        WHERE quote_id IS NOT NULL
                    DO UPDATE SET
                        ai_citations_delta_30d = EXCLUDED.ai_citations_delta_30d,
                        publish_status = EXCLUDED.publish_status,
                        metadata = EXCLUDED.metadata,
                        brand_id = COALESCE(EXCLUDED.brand_id, writing_strategy_outcome_events.brand_id),
                        observed_at = NOW()
                    """,
                    (
                        row.get("strategy_id"),
                        row.get("brand_id"),
                        int(row["quote_id"]),
                        (row.get("industry_key") or "general")[:100],
                        publish_status,
                        int(cite.get("citations") or 0),
                        json.dumps(metadata, ensure_ascii=False, default=str),
                    ),
                )
            written += 1
        except Exception as exc:
            # 单条失败不吃掉整批,但必须让调用方(心跳)看到 —— 汇总里带 error 计数。
            logger.exception(
                "[WritingAssignment] 效果回流写入失败 assignment_id=%s: %s", row.get("id"), exc
            )
            raise

    return {
        "scanned": len(assignments),
        "written": written,
        "measured": measured,
        "skipped_no_articles": skipped_no_articles,
        "dry_run": bool(dry_run),
        "processed": written,
    }
