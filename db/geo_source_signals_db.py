"""DB helpers for trusted source signals from GEO research monitor."""

from __future__ import annotations

from db.schema_guard import add_column_if_missing
from typing import Any

from psycopg2.extras import Json

from db.connection import get_connection, get_db
from services.media_entity_flywheel import is_all_industry_scope
from services.research_monitor.source_signal_weighting import EXPLICIT_CITED_TIERS

# [T3 口径 SSOT 2026-07-03] "明确引用" FILTER 从 EXPLICIT_CITED_TIERS 单点派生(answer_adopted+cited_source),
# 与 answer_adoption_metrics.metric_flags 的 explicit_cited 完全同源 → 证据来源表与引擎拆分对同一数。
_EXPLICIT_CITED_SQL = "signal_tier IN (%s)" % ", ".join(
    "'%s'" % tier for tier in sorted(EXPLICIT_CITED_TIERS)
)

# [B1-1] engine 归一 · 与 services/placement_service._ENGINE_NORMALIZE_SQL 及
# api/research_monitor_citations_api._ENGINE_NORMALIZE_SQL 同口径(项目惯例:小 CASE 各模块自带,
# 避免 db 层反向 import 重量级 placement_service 造成循环依赖)。
# geo_research_source_signals.engine 由 bridge 写入,可能小写/中文混存 →
# COUNT(DISTINCT engine) 会把同一引擎的别名算成多个,必须先归一。
_ENGINE_NORMALIZE_SQL = """
    CASE
        WHEN LOWER(engine) IN ('doubao', '豆包') THEN '豆包'
        WHEN LOWER(engine) IN ('kimi') THEN 'Kimi'
        WHEN LOWER(engine) IN ('deepseek') THEN 'DeepSeek'
        WHEN LOWER(engine) IN ('qwen', '千问') THEN '千问'
        ELSE engine
    END
"""


def sum_signal_weight_by_domain(industry_key: str = "") -> dict[str, float]:
    """[发布榜垂类特异性 2026-07-05] domain → SUM(balanced_weight)(只读聚合)。

    industry_key 给定(且非 all-scope)则限该行业;空 = 全行业基线。
    balanced_weight 是引用信号加权 SSOT(与评分链同源),比裸行数更能代表引用强度。
    走 idx_geo_source_signals_industry / idx_geo_source_signals_domain,万级表毫秒级。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        where = "WHERE domain IS NOT NULL AND domain <> ''"
        params: list[Any] = []
        if industry_key and not is_all_industry_scope(industry_key):
            where += " AND industry_key = %s"
            params.append(industry_key)
        cur.execute(f"""
            SELECT domain, SUM(balanced_weight) AS weight_sum
              FROM geo_research_source_signals
             {where}
             GROUP BY domain
        """, params)
        return {
            r["domain"]: float(r["weight_sum"] or 0)
            for r in cur.fetchall()
            if r["domain"]
        }
    finally:
        conn.close()


def init_geo_source_signal_tables() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_research_source_signals (
                id BIGSERIAL PRIMARY KEY,
                source_url TEXT NOT NULL,
                url_hash CHAR(40),
                domain VARCHAR(300),
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                engine VARCHAR(40) NOT NULL,
                prompt_id TEXT,
                signal_tier VARCHAR(40) NOT NULL,
                source_position INTEGER DEFAULT 0,
                total_sources_in_answer INTEGER DEFAULT 1,
                balanced_weight NUMERIC(12,6) NOT NULL DEFAULT 0,
                answer_mentioned_brand BOOLEAN DEFAULT FALSE,
                round_id VARCHAR(80),
                article_id BIGINT,
                metadata JSONB DEFAULT '{}'::jsonb,
                observed_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (source_url, industry_key, engine, prompt_id, signal_tier, round_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_source_signals_industry ON geo_research_source_signals(industry_key, balanced_weight DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_source_signals_domain ON geo_research_source_signals(domain)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_source_signals_tier ON geo_research_source_signals(signal_tier)")
        # A9 research observation lineage: each signal remains event-grain.
        for column, sql_type in [
            ("label_provenance_type", "VARCHAR(40) DEFAULT 'legacy_unknown'"),
            ("label_provenance_version", "VARCHAR(80) DEFAULT 'legacy_unknown'"),
            ("provider", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("model", "VARCHAR(128) DEFAULT 'legacy_unknown'"),
            ("model_revision", "VARCHAR(128) DEFAULT 'legacy_unknown'"),
            ("surface", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("search_mode", "VARCHAR(64) DEFAULT 'legacy_unknown'"),
            ("prompt_snapshot", "TEXT"),
            ("article_snapshot_hash", "CHAR(64)"),
            ("article_fetch_id", "BIGINT"),
            ("lineage_status", "VARCHAR(32) DEFAULT 'legacy_unknown'"),
            ("lineage_error_reason", "TEXT"),
        ]:
            add_column_if_missing(cur, "geo_research_source_signals", column, sql_type)  # [WO_285b] 列缺失才 ALTER(请求路径可达)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_source_quality_snapshots (
                id BIGSERIAL PRIMARY KEY,
                source_url TEXT NOT NULL,
                article_id BIGINT,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                quality_score NUMERIC(6,2),
                risk_tags JSONB DEFAULT '[]'::jsonb,
                usable_for_writing BOOLEAN DEFAULT FALSE,
                quality_components JSONB DEFAULT '{}'::jsonb,
                version VARCHAR(80) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (source_url, industry_key, version)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_source_quality_industry ON geo_source_quality_snapshots(industry_key, quality_score DESC)")


def upsert_source_signal(signal: dict[str, Any]) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO geo_research_source_signals (
                source_url, url_hash, domain, industry_key, engine, prompt_id,
                signal_tier, source_position, total_sources_in_answer,
                balanced_weight, answer_mentioned_brand, round_id, article_id,
                metadata, observed_at, label_provenance_type,
                label_provenance_version, provider, model, model_revision,
                surface, search_mode, prompt_snapshot, article_snapshot_hash,
                article_fetch_id, lineage_status, lineage_error_reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(),
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_url, industry_key, engine, prompt_id, signal_tier, round_id)
            DO UPDATE SET
                source_position = EXCLUDED.source_position,
                total_sources_in_answer = EXCLUDED.total_sources_in_answer,
                balanced_weight = EXCLUDED.balanced_weight,
                answer_mentioned_brand = EXCLUDED.answer_mentioned_brand,
                article_id = EXCLUDED.article_id,
                metadata = EXCLUDED.metadata,
                label_provenance_type = EXCLUDED.label_provenance_type,
                label_provenance_version = EXCLUDED.label_provenance_version,
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                model_revision = EXCLUDED.model_revision,
                surface = EXCLUDED.surface,
                search_mode = EXCLUDED.search_mode,
                prompt_snapshot = EXCLUDED.prompt_snapshot,
                article_snapshot_hash = EXCLUDED.article_snapshot_hash,
                article_fetch_id = EXCLUDED.article_fetch_id,
                lineage_status = EXCLUDED.lineage_status,
                lineage_error_reason = EXCLUDED.lineage_error_reason,
                observed_at = NOW()
            RETURNING *
        """, (
            signal.get("source_url"),
            signal.get("url_hash"),
            signal.get("domain"),
            signal.get("industry_key") or "general",
            signal.get("engine") or "",
            signal.get("prompt_id") or "",
            signal.get("signal_tier") or "crawled_reference_only",
            int(signal.get("source_position") or 0),
            int(signal.get("total_sources_in_answer") or 1),
            signal.get("balanced_weight") or 0,
            bool(signal.get("answer_mentioned_brand")),
            signal.get("round_id") or "",
            signal.get("article_id"),
            Json(signal.get("metadata") or {}),
            signal.get("label_provenance_type") or "legacy_unknown",
            signal.get("label_provenance_version") or "legacy_unknown",
            signal.get("provider") or signal.get("engine") or "legacy_unknown",
            signal.get("model") or "legacy_unknown",
            signal.get("model_revision") or "legacy_unknown",
            signal.get("surface") or "legacy_unknown",
            signal.get("search_mode") or "legacy_unknown",
            signal.get("prompt_snapshot"),
            signal.get("article_snapshot_hash"),
            signal.get("article_fetch_id"),
            signal.get("lineage_status") or "legacy_unknown",
            signal.get("lineage_error_reason"),
        ))
        return dict(cur.fetchone())


def upsert_source_quality_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO geo_source_quality_snapshots (
                source_url, article_id, industry_key, quality_score, risk_tags,
                usable_for_writing, quality_components, version, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (source_url, industry_key, version) DO UPDATE SET
                article_id = EXCLUDED.article_id,
                quality_score = EXCLUDED.quality_score,
                risk_tags = EXCLUDED.risk_tags,
                usable_for_writing = EXCLUDED.usable_for_writing,
                quality_components = EXCLUDED.quality_components,
                created_at = NOW()
            RETURNING *
        """, (
            snapshot.get("source_url"),
            snapshot.get("article_id"),
            snapshot.get("industry_key") or "general",
            snapshot.get("quality_score"),
            Json(snapshot.get("risk_tags") or []),
            bool(snapshot.get("usable_for_writing")),
            Json(snapshot.get("quality_components") or {}),
            snapshot.get("version") or "source_quality_v1_2026-06-12",
        ))
        return dict(cur.fetchone())


def list_source_signal_rollup(industry_key: str = "", limit: int = 100) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = []
        where = ""
        if industry_key and not is_all_industry_scope(industry_key):
            where = "WHERE industry_key = %s"
            params.append(industry_key)
        params.append(limit)
        cur.execute(f"""
            SELECT domain, industry_key,
                   SUM(balanced_weight)::float AS balanced_weight,
                   COUNT(*) FILTER (WHERE signal_tier = 'answer_adopted') AS answer_adopted_count,
                   COUNT(*) FILTER (WHERE signal_tier = 'cited_source') AS cited_count,
                   COUNT(*) FILTER (WHERE {_EXPLICIT_CITED_SQL}) AS explicit_cited_count,
                   COUNT(*) FILTER (WHERE signal_tier = 'search_result_only') AS search_only_count,
                   COUNT(*) FILTER (WHERE signal_tier = 'crawled_reference_only') AS reference_only_count,
                   COUNT(DISTINCT {_ENGINE_NORMALIZE_SQL}) AS engine_count,
                   COUNT(DISTINCT prompt_id) AS prompt_count,
                   MAX(observed_at) AS last_seen_at
              FROM geo_research_source_signals
              {where}
             GROUP BY domain, industry_key
             ORDER BY SUM(balanced_weight) DESC
             LIMIT %s
        """, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
