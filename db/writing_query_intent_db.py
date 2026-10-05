"""Query-intent labels for the writing flywheel (W1 · 语料标签层地基).

`geo_query_intent` — one row per (industry, query) carrying that query's distilled
INTENT (reusing the 8-class article-intent taxonomy, but a query-focused prompt).

Why a NEW aggregation table (not a column on geo_research_raw):
  geo_research_raw is one-row-per-citation, with the same `query` repeated across
  every citation of every answer, and NO round_id / answer_id / stable query key.
  The unit of "问题类型" is the QUERY, not the citation — classifying millions of
  citation rows would be wasteful and duplicative. So we key by (industry, query).

Shadow / label-only: no customer data, no billing. The LLM call lives in
`services.research_monitor.query_intent_classifier`; THIS module only persists.
Idempotent `init_*` is called lazily at the top of every writer/route that touches
the table (repo convention — there is no central startup migration).
"""
from __future__ import annotations

import hashlib
from typing import Any

from db.connection import get_connection, get_db

# 复用 article_intent_classifier 的 8 类 —— 标签 SSOT 在分类器,这里只是 DB CHECK 兜底副本。
QUERY_INTENT_LABELS: tuple[str, ...] = (
    "ranking",
    "tutorial",
    "long_form",
    "comparison",
    "data_report",
    "policy",
    "definition",
    "faq",
)


def query_intent_hash(industry: str, query: str) -> str:
    """稳定键:同 (industry, query) 永远同 hash,跨进程一致(md5 不随随机种子变)。

    归一与 Python 端一致(strip + industry.lower()),故检测"未分类"时全在 Python 侧
    比对 hash,不依赖 SQL md5 与 Python md5 逐字节等价(strip/编码口径差异陷阱)。
    """
    norm_industry = (industry or "").strip().lower()
    norm_query = (query or "").strip()
    raw = f"{norm_industry}\x1f{norm_query}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def init_writing_query_intent_tables() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS geo_query_intent (
                id BIGSERIAL PRIMARY KEY,
                query_hash CHAR(32) NOT NULL,
                industry TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                query_intent VARCHAR(20) NOT NULL,
                confidence NUMERIC(4,3) NOT NULL DEFAULT 0,
                reason TEXT,
                model VARCHAR(60),
                classified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_geo_query_intent_hash UNIQUE (query_hash),
                CONSTRAINT ck_geo_query_intent_label CHECK (
                    query_intent IN (
                        'ranking','tutorial','long_form','comparison',
                        'data_report','policy','definition','faq'
                    )
                )
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_geo_query_intent_industry "
            "ON geo_query_intent(industry)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_geo_query_intent_intent "
            "ON geo_query_intent(query_intent)"
        )


def upsert_query_intent(
    *,
    industry: str,
    query: str,
    query_intent: str,
    confidence: float = 0.0,
    reason: str = "",
    model: str = "",
) -> bool:
    """幂等写入一条 query 分类。返回 True=成功。重跑同 query 覆盖(刷新分类,不重复灌行)。"""
    if query_intent not in QUERY_INTENT_LABELS:
        raise ValueError(f"invalid_query_intent:{query_intent}")
    q_hash = query_intent_hash(industry, query)
    try:
        conf = max(0.0, min(1.0, float(confidence or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_query_intent
                (query_hash, industry, query, query_intent, confidence, reason, model, classified_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (query_hash) DO UPDATE SET
                query_intent = EXCLUDED.query_intent,
                confidence   = EXCLUDED.confidence,
                reason       = EXCLUDED.reason,
                model        = EXCLUDED.model,
                classified_at = NOW()
            """,
            (
                q_hash,
                (industry or "").strip(),
                (query or "").strip(),
                query_intent,
                conf,
                (reason or "")[:500],
                (model or "")[:60],
            ),
        )
    return True


def get_query_intent(industry: str, query: str) -> dict[str, Any] | None:
    init_writing_query_intent_tables()
    q_hash = query_intent_hash(industry, query)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT industry, query, query_intent, confidence, reason, model, classified_at "
            "FROM geo_query_intent WHERE query_hash = %s",
            (q_hash,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_unclassified_queries(
    *,
    industry: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """从 geo_research_raw 取尚未分类的 distinct (industry, query),高频优先。

    未分类检测全在 Python 侧比对 hash,避免 SQL md5 与 Python md5 口径不一致陷阱。
    候选池取 limit*4(高频优先),过滤掉已分类后返回前 limit 条。
    """
    init_writing_query_intent_tables()
    safe_limit = max(1, int(limit or 2000))
    candidate_cap = safe_limit * 4
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = []
        where = ["COALESCE(query, '') <> ''"]
        if industry:
            # [review fix] 行业匹配统一 LOWER(历史坑:大小写不同 → 0 目标空转)
            where.append("LOWER(industry) = LOWER(%s)")
            params.append(industry)
        params.append(candidate_cap)
        cur.execute(
            f"""
            SELECT industry, query, COUNT(*) AS freq
            FROM geo_research_raw
            WHERE {' AND '.join(where)}
            GROUP BY industry, query
            ORDER BY freq DESC
            LIMIT %s
            """,
            tuple(params),
        )
        candidates = [dict(r) for r in cur.fetchall()]
        if not candidates:
            return []
        # 已分类 hash 集合(一次性拉全,内存比对)
        cur.execute("SELECT query_hash FROM geo_query_intent")
        classified = {str(r["query_hash"]) for r in cur.fetchall()}
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in candidates:
        h = query_intent_hash(row.get("industry") or "", row.get("query") or "")
        if h in classified:
            continue
        out.append(
            {
                "industry": row.get("industry") or "",
                "query": row.get("query") or "",
                "freq": int(row.get("freq") or 0),
            }
        )
        if len(out) >= safe_limit:
            break
    return out


def get_query_intent_coverage(industry: str | None = None) -> dict[str, Any]:
    """覆盖率:已分类 distinct query 数 / geo_research_raw distinct query 总数。

    分母口径与分类目标一致(distinct (industry, query) 且 query 非空)。分子用 Python
    hash 比对交集,避免 SQL/Python md5 口径漂移误算覆盖率。
    """
    init_writing_query_intent_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = []
        where = ["COALESCE(query, '') <> ''"]
        if industry:
            # [review fix] 行业匹配统一 LOWER(历史坑:大小写不同 → 0 目标空转)
            where.append("LOWER(industry) = LOWER(%s)")
            params.append(industry)
        cur.execute(
            f"""
            SELECT industry, query
            FROM geo_research_raw
            WHERE {' AND '.join(where)}
            GROUP BY industry, query
            """,
            tuple(params),
        )
        raw_pairs = [(r.get("industry") or "", r.get("query") or "") for r in cur.fetchall()]
        cur.execute("SELECT query_hash FROM geo_query_intent")
        classified = {str(r["query_hash"]) for r in cur.fetchall()}
    finally:
        conn.close()
    total = len(raw_pairs)
    covered = sum(1 for ind, q in raw_pairs if query_intent_hash(ind, q) in classified)
    return {
        "industry": industry or "all",
        "total_queries": total,
        "classified_queries": covered,
        "coverage": round(covered / total, 4) if total else 0.0,
        "unclassified_queries": max(0, total - covered),
    }


def get_intent_distribution(industry: str | None = None) -> dict[str, int]:
    """各 intent 类型下已分类 query 数(看板/证据用)。"""
    init_writing_query_intent_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = []
        where = ["1=1"]
        if industry:
            # [review fix] 行业匹配统一 LOWER(历史坑:大小写不同 → 0 目标空转)
            where.append("LOWER(industry) = LOWER(%s)")
            params.append(industry)
        cur.execute(
            f"SELECT query_intent, COUNT(*) AS cnt FROM geo_query_intent "
            f"WHERE {' AND '.join(where)} GROUP BY query_intent",
            tuple(params),
        )
        return {str(r["query_intent"]): int(r["cnt"]) for r in cur.fetchall()}
    finally:
        conn.close()
