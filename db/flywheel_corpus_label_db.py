"""B2 语料价值标签账本(2026-07-29)。

生产实证:`geo_research_article_citations` 23474 条引用 / 16103 篇被引文章 / 17 个行业,
**当前没有任何消费方**。B2 给它们打"能不能复用、复用在什么场景、价值几档"的结构化标签,
供 B3 组合决策、B4 诊断沉淀复用、以及写作取材读取。

为什么新建表而不写回 `geo_research_articles.corpus_grade`:
工单红线写明"飞轮表写入只走本单新增路径"。`corpus_grade` 是 research 域自己的分级字段
(生产全库 JC0,由 `corpus_labeler` 维护),本单从旁边加一张标签表,
research 域一行不写 —— 与既有的"只读跨界"口径一致,也让这批标签可以整表回滚。

SQL 4 维核验(建表):
  1. 列名:article_id/industry_key/reusable/value_tier/scenarios/reason/source/model/
     labeled_at 均在场;article_id 对应 `geo_research_articles.id`
  2. data_type:article_id BIGINT(生产 geo_research_articles.id 是 bigint,已核)·
     scenarios JSONB · reusable BOOLEAN · labeled_at TIMESTAMPTZ
  3. 字段归属:全部新列在本新表,不碰 geo_research_articles / citations
  4. dry-run:CREATE TABLE / INDEX IF NOT EXISTS 全幂等,无破坏性 SQL;
     **不建外键** —— research 域的 tombstone/清理动作不该被本观测表挡住
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from db.connection import get_connection, get_db

logger = logging.getLogger("GEO-FlywheelCorpus")

VALUE_TIERS = ("high", "medium", "low")

#: 复用场景固定词表 —— 模型只能从这里挑,不许自由发挥,否则下游没法按场景检索。
REUSE_SCENARIOS: dict[str, str] = {
    "diagnosis_question": "诊断出题参考",
    "competitor_mapping": "竞品图谱素材",
    "writing_evidence": "写作可引用证据",
    "industry_background": "行业背景说明",
    "pricing_reference": "价格/费用口径参考",
    "buyer_decision": "选购决策链路素材",
}

_TABLE_READY = False


def init_flywheel_corpus_label_tables(force: bool = False) -> None:
    global _TABLE_READY
    if _TABLE_READY and not force:
        return
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS flywheel_corpus_value_labels (
                id BIGSERIAL PRIMARY KEY,
                article_id BIGINT NOT NULL,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                reusable BOOLEAN NOT NULL DEFAULT FALSE,
                value_tier VARCHAR(12) NOT NULL DEFAULT 'low',
                scenarios JSONB NOT NULL DEFAULT '[]'::jsonb,
                reason TEXT NOT NULL DEFAULT '',
                source VARCHAR(12) NOT NULL DEFAULT 'rule',
                model VARCHAR(120),
                citations INTEGER NOT NULL DEFAULT 0,
                labeled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_flywheel_corpus_label UNIQUE (article_id, industry_key)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_flywheel_corpus_label_lookup "
            "ON flywheel_corpus_value_labels(industry_key, reusable, value_tier)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_flywheel_corpus_label_time "
            "ON flywheel_corpus_value_labels(labeled_at DESC)"
        )
    _TABLE_READY = True


def upsert_corpus_labels(rows: Iterable[dict[str, Any]]) -> int:
    """幂等写标签。同 (article_id, industry_key) 重跑刷新不新增行。"""
    payload = list(rows or [])
    if not payload:
        return 0
    init_flywheel_corpus_label_tables()
    written = 0
    with get_db() as conn:
        cur = conn.cursor()
        for row in payload:
            article_id = int(row.get("article_id") or 0)
            if not article_id:
                continue
            tier = str(row.get("value_tier") or "low")
            if tier not in VALUE_TIERS:
                tier = "low"
            scenarios = [s for s in (row.get("scenarios") or []) if s in REUSE_SCENARIOS]
            cur.execute(
                """
                INSERT INTO flywheel_corpus_value_labels
                    (article_id, industry_key, reusable, value_tier, scenarios,
                     reason, source, model, citations, labeled_at)
                VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,NOW())
                ON CONFLICT (article_id, industry_key) DO UPDATE SET
                    reusable = EXCLUDED.reusable,
                    value_tier = EXCLUDED.value_tier,
                    scenarios = EXCLUDED.scenarios,
                    reason = EXCLUDED.reason,
                    source = EXCLUDED.source,
                    model = EXCLUDED.model,
                    citations = EXCLUDED.citations,
                    labeled_at = NOW()
                """,
                (
                    article_id, str(row.get("industry_key") or "general")[:100],
                    bool(row.get("reusable")), tier,
                    json.dumps(scenarios, ensure_ascii=False),
                    str(row.get("reason") or "")[:1000],
                    str(row.get("source") or "rule")[:12],
                    (row.get("model") or None),
                    int(row.get("citations") or 0),
                ),
            )
            written += 1
    return written


def list_reusable_corpus(
    industry_key: str = "", *, limit: int = 50, min_tier: str = "medium",
    scenario: str = "",
) -> list[dict[str, Any]]:
    """下游取素材的唯一入口(B3 / B4 / 写作取材都走这里)。"""
    limit = max(1, min(int(limit or 50), 500))
    tiers = {"high": ("high",), "medium": ("high", "medium")}.get(min_tier, VALUE_TIERS)
    try:
        init_flywheel_corpus_label_tables()
        conn = get_connection()
        try:
            cur = conn.cursor()
            sql = [
                "SELECT * FROM flywheel_corpus_value_labels",
                "WHERE reusable = TRUE AND value_tier = ANY(%s)",
            ]
            params: list[Any] = [list(tiers)]
            if industry_key:
                sql.append("AND industry_key = %s")
                params.append(industry_key[:100])
            if scenario:
                sql.append("AND scenarios @> %s::jsonb")
                params.append(json.dumps([scenario]))
            sql.append("ORDER BY citations DESC, labeled_at DESC LIMIT %s")
            params.append(limit)
            cur.execute(" ".join(sql), tuple(params))
            return [dict(r) for r in cur.fetchall() or []]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[B2] 可复用语料查询失败: %s", exc)
        return []


def labeled_article_ids(article_ids: Iterable[int]) -> set[int]:
    ids = [int(a) for a in article_ids if a]
    if not ids:
        return set()
    try:
        init_flywheel_corpus_label_tables()
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT article_id FROM flywheel_corpus_value_labels WHERE article_id = ANY(%s)",
                (ids,),
            )
            return {int(r["article_id"]) for r in cur.fetchall() or []}
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[B2] 已标注集合查询失败: %s", exc)
        return set()


def label_coverage() -> dict[str, Any]:
    """标注覆盖率(仪表用)。"""
    try:
        init_flywheel_corpus_label_tables()
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS labeled, "
                "COUNT(*) FILTER (WHERE reusable) AS reusable, "
                "COUNT(*) FILTER (WHERE source = 'llm') AS by_llm, "
                "MAX(labeled_at) AS last_labeled_at "
                "FROM flywheel_corpus_value_labels"
            )
            row = dict(cur.fetchone() or {})
            cur.execute("SELECT COUNT(DISTINCT article_id) AS cited FROM geo_research_article_citations")
            row["cited_articles_total"] = int((cur.fetchone() or {}).get("cited") or 0)
            return row
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[B2] 覆盖率查询失败: %s", exc)
        return {}
