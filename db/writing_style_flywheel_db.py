"""DB helpers for writing strategy flywheel shadow layer."""

from __future__ import annotations

from db.schema_guard import add_column_if_missing, alter_column_type_if_changed
import logging
from collections import Counter
from urllib.parse import urlparse
from typing import Any

from psycopg2.extras import Json

logger = logging.getLogger("GEO-WritingStyleFlywheel")

from db.connection import get_connection, get_db
from services.media_entity_flywheel import industry_filter_values, is_all_industry_scope, normalize_industry_key
from services.article_structure_features import extract_article_structure_features
from services.writing_style_feature_extractor import extract_writing_style_features


def init_writing_style_flywheel_tables() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS writing_style_signal_events (
                id BIGSERIAL PRIMARY KEY,
                article_id BIGINT,
                source_url TEXT,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                signal_tier VARCHAR(40) NOT NULL,
                balanced_weight NUMERIC(12,6) DEFAULT 0,
                engine VARCHAR(40),
                prompt_id TEXT,
                metadata JSONB DEFAULT '{}'::jsonb,
                observed_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_writing_style_signal_industry ON writing_style_signal_events(industry_key, balanced_weight DESC)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS writing_style_feature_snapshots (
                id BIGSERIAL PRIMARY KEY,
                article_id BIGINT,
                source_url TEXT,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                style_family VARCHAR(60) NOT NULL,
                intent_type VARCHAR(60),
                content_type VARCHAR(60),
                features JSONB NOT NULL,
                feature_version VARCHAR(80) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (article_id, source_url, feature_version)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_writing_style_features_industry ON writing_style_feature_snapshots(industry_key, style_family)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS writing_strategy_versions (
                id BIGSERIAL PRIMARY KEY,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                strategy_version VARCHAR(240) NOT NULL,
                status VARCHAR(40) NOT NULL DEFAULT 'shadow',
                style_family VARCHAR(60),
                guidance TEXT,
                common_elements JSONB DEFAULT '[]'::jsonb,
                evidence_score NUMERIC(12,4),
                outcome_score NUMERIC(12,4),
                confidence NUMERIC(5,4),
                guardrails JSONB DEFAULT '[]'::jsonb,
                source_summary JSONB DEFAULT '{}'::jsonb,
                reviewed_by BIGINT,
                reviewed_at TIMESTAMPTZ,
                review_note TEXT,
                activated_at TIMESTAMPTZ,
                activated_by BIGINT,
                archived_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (industry_key, strategy_version)
            )
        """)
        add_column_if_missing(cur, "writing_strategy_versions", "review_note", "TEXT")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        add_column_if_missing(cur, "writing_strategy_versions", "activated_at", "TIMESTAMPTZ")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        add_column_if_missing(cur, "writing_strategy_versions", "activated_by", "BIGINT")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        add_column_if_missing(cur, "writing_strategy_versions", "archived_at", "TIMESTAMPTZ")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        alter_column_type_if_changed(cur, "writing_strategy_versions", "strategy_version",
                                     "VARCHAR(240)", "character varying(240)")  # [WO_285b] 已是 240 就不动
        cur.execute("CREATE INDEX IF NOT EXISTS idx_writing_strategy_versions_status ON writing_strategy_versions(status, industry_key)")
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_writing_strategy_one_active_per_industry
                ON writing_strategy_versions(industry_key)
             WHERE status = 'active'
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS writing_strategy_audit_events (
                id BIGSERIAL PRIMARY KEY,
                strategy_id BIGINT REFERENCES writing_strategy_versions(id) ON DELETE SET NULL,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                action VARCHAR(40) NOT NULL,
                actor_id BIGINT,
                from_status VARCHAR(40),
                to_status VARCHAR(40),
                note TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_writing_strategy_audit_strategy
                ON writing_strategy_audit_events(strategy_id, created_at DESC)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS writing_strategy_assignments (
                id BIGSERIAL PRIMARY KEY,
                strategy_id BIGINT NOT NULL REFERENCES writing_strategy_versions(id) ON DELETE CASCADE,
                brand_id BIGINT,
                quote_id BIGINT,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                assignment_status VARCHAR(40) DEFAULT 'shadow',
                assigned_by BIGINT,
                assigned_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_writing_strategy_assignments_brand ON writing_strategy_assignments(brand_id, quote_id)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS writing_strategy_outcome_events (
                id BIGSERIAL PRIMARY KEY,
                strategy_id BIGINT REFERENCES writing_strategy_versions(id) ON DELETE SET NULL,
                article_id BIGINT,
                brand_id BIGINT,
                quote_id BIGINT,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                publish_status VARCHAR(60),
                ai_citations_delta_30d INTEGER DEFAULT 0,
                monitoring_brand_score_delta_30d NUMERIC(8,2) DEFAULT 0,
                metadata JSONB DEFAULT '{}'::jsonb,
                observed_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        add_column_if_missing(cur, "writing_strategy_outcome_events", "industry_key", "VARCHAR(100) NOT NULL DEFAULT 'general'")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        # [FIX-4] 幂等键:new_version_id 从 metadata 提列(消费端按版本聚合用)+ measurement_window
        #   标识测量周(date_trunc('week') 粒度)。同 (version, 周, 行业) 重跑 → ON CONFLICT 去重;
        #   跨周是新测量新行。partial WHERE new_version_id IS NOT NULL:真实单篇 outcome(version NULL)
        #   不参与唯一约束,不误伤。
        add_column_if_missing(cur, "writing_strategy_outcome_events", "new_version_id", "VARCHAR(100)")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        add_column_if_missing(cur, "writing_strategy_outcome_events", "measurement_window", "DATE")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_writing_strategy_outcomes_strategy ON writing_strategy_outcome_events(strategy_id, observed_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_writing_strategy_outcomes_industry ON writing_strategy_outcome_events(industry_key, observed_at DESC)")
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_wso_version_window
                ON writing_strategy_outcome_events(new_version_id, measurement_window, industry_key)
             WHERE new_version_id IS NOT NULL
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_recommendation_predictions (
                id BIGSERIAL PRIMARY KEY,
                scope_key VARCHAR(200) NOT NULL,
                prediction_type VARCHAR(60) NOT NULL,
                predicted_payload JSONB NOT NULL,
                version VARCHAR(80) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # [FIX-4] pred 幂等:同 (scope_key, version, 周) 复用同一 pred 行(ON CONFLICT DO UPDATE
        #   保证 RETURNING id 有值)。partial WHERE measurement_window IS NOT NULL:老行(NULL)不冲突。
        add_column_if_missing(cur, "geo_recommendation_predictions", "measurement_window", "DATE")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_grp_scope_version_window
                ON geo_recommendation_predictions(scope_key, version, measurement_window)
             WHERE measurement_window IS NOT NULL
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_recommendation_outcomes (
                id BIGSERIAL PRIMARY KEY,
                prediction_id BIGINT REFERENCES geo_recommendation_predictions(id) ON DELETE SET NULL,
                scope_key VARCHAR(200) NOT NULL,
                outcome_payload JSONB NOT NULL,
                observed_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # [FIX-4] outcome 幂等:一个 prediction 一个 outcome(pred 幂等后 prediction_id 稳定复用)。
        add_column_if_missing(cur, "geo_recommendation_outcomes", "measurement_window", "DATE")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_gro_prediction
                ON geo_recommendation_outcomes(prediction_id)
             WHERE prediction_id IS NOT NULL
        """)


def upsert_style_feature_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO writing_style_feature_snapshots (
                article_id, source_url, industry_key, style_family,
                intent_type, content_type, features, feature_version, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (article_id, source_url, feature_version) DO UPDATE SET
                industry_key = EXCLUDED.industry_key,
                style_family = EXCLUDED.style_family,
                intent_type = EXCLUDED.intent_type,
                content_type = EXCLUDED.content_type,
                features = EXCLUDED.features,
                created_at = NOW()
            RETURNING *
        """, (
            snapshot.get("article_id"),
            snapshot.get("source_url") or "",
            snapshot.get("industry_key") or "general",
            snapshot.get("style_family") or "guide",
            snapshot.get("intent_type"),
            snapshot.get("content_type"),
            Json(snapshot.get("features") or snapshot),
            snapshot.get("feature_version") or "writing_style_features_v1_2026-06-12",
        ))
        return dict(cur.fetchone())


def rebuild_style_feature_snapshots_from_articles(
    industry: str,
    limit: int = 300,
    min_chars: int = 500,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Extract writing-style feature snapshots from research article bodies.

    The strategy generator intentionally reads versioned shadow snapshots, not
    raw article text.  This bridge converts June crawler originals into those
    snapshots so Phase 2 can learn from the article corpus without touching live
    writing prompts.
    """
    industry_key = normalize_industry_key(industry)
    all_scope = is_all_industry_scope(industry or industry_key)
    industry_values = [] if all_scope else industry_filter_values(industry or industry_key) or [industry_key]
    min_chars = max(0, int(min_chars or 0))
    limit = max(1, min(int(limit or 300), 1000))

    conn = get_connection()
    try:
        cur = conn.cursor()
        source_filter = "" if all_scope else "WHERE industry_key = %s"
        raw_filter = "" if all_scope else "WHERE raw.industry = ANY(%s)"
        article_filter = "" if all_scope else "AND article.primary_industry = ANY(%s)"
        params: list[Any] = []
        if not all_scope:
            params.extend([industry_key, industry_values, industry_values])
        params.extend([min_chars, limit])
        cur.execute(f"""
            WITH source_signal_by_url AS (
                SELECT source_url,
                       industry_key,
                       MAX(CASE WHEN signal_tier = 'answer_adopted' THEN 1 ELSE 0 END) AS is_adopted,
                       MAX(CASE WHEN signal_tier = 'cited_source' THEN 1 ELSE 0 END) AS is_cited,
                       MAX(CASE WHEN signal_tier = 'search_result_only' THEN 1 ELSE 0 END) AS is_search_only,
                       MAX(COALESCE(balanced_weight, 0)) AS source_weight
                  FROM geo_research_source_signals
                 {source_filter}
                 GROUP BY source_url, industry_key
            ),
            article_source_signal AS (
                SELECT citation.article_id,
                       raw.cite_url,
                       MAX(COALESCE(sig.is_adopted, 0)) AS is_adopted,
                       MAX(COALESCE(sig.is_cited, 0)) AS is_cited,
                       MAX(COALESCE(sig.is_search_only, 0)) AS is_search_only,
                       MAX(COALESCE(sig.source_weight, 0)) AS source_weight
                  FROM geo_research_article_citations citation
                  JOIN geo_research_raw raw ON raw.id = citation.raw_id
                  LEFT JOIN source_signal_by_url sig ON sig.source_url = raw.cite_url
                 {raw_filter}
                 GROUP BY citation.article_id, raw.cite_url
            ),
            article_signal AS (
                SELECT article_id,
                       SUM(is_adopted) AS adopted_count,
                       SUM(is_cited) AS cited_count,
                       SUM(is_search_only) AS search_only_count,
                       MAX(source_weight) AS source_weight
                  FROM article_source_signal
                 GROUP BY article_id
            )
            SELECT article.id, article.url, article.domain, article.title,
                   article.primary_industry, article.content_type,
                   article.intent_type, article.inline_cleaned_content,
                   article.oss_key_cleaned,
                   article.cleaned_char_count, article.review_status,
                   article.clean_status, article.total_citation_count,
                   COALESCE(article_signal.adopted_count, 0) AS adopted_count,
                   COALESCE(article_signal.cited_count, 0) AS cited_count,
                   COALESCE(article_signal.search_only_count, 0) AS search_only_count,
                   COALESCE(article_signal.source_weight, 0) AS source_weight
              FROM geo_research_articles article
              LEFT JOIN article_signal ON article_signal.article_id = article.id
             WHERE COALESCE(article.expired, FALSE) = FALSE
               {article_filter}
               AND COALESCE(article.review_status, '') <> 'rejected'
               AND COALESCE(article.clean_status, '') <> 'failed'
               AND COALESCE(
                     article.cleaned_char_count,
                     CHAR_LENGTH(COALESCE(article.inline_cleaned_content, ''))
                   ) >= %s
             ORDER BY adopted_count DESC,
                      cited_count DESC,
                      source_weight DESC,
                     article.cleaned_char_count DESC NULLS LAST,
                     article.id DESC
             LIMIT %s
        """, tuple(params))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    preview: list[dict[str, Any]] = []
    style_distribution: dict[str, int] = {}
    structure_summary: dict[str, Any] = {
        "sample_count": 0,
        "lead_answers_question": 0,
        "has_checklist": 0,
        "has_price_or_budget": 0,
        "has_risk_or_pitfall": 0,
        "conclusion_has_decision_advice": 0,
    }
    written = 0
    skipped = 0
    oss_loaded = 0  # [B1-3] 从对象存储回读正文的行数(用于诊断"written=0 静默跳过")

    for row in rows:
        content = str(row.get("cleaned_content") or row.get("inline_cleaned_content") or "")
        # [B1-3] 正文卸载到 OSS 的文章:inline_cleaned_content 为空但 cleaned_char_count>0。
        # 旧逻辑只读内联列 → 这些文章判 content='' 被静默 skip,导致 rebuild written=0、
        # 且 article_structure 采纳组被空正文稀释。此处对 OSS 行复用既有 helper 回读正文
        # (禁自造客户端;单条网络读,shadow rebuild 可接受),并写回 row 让下游特征/结构提取器同样读到。
        if len(content) < min_chars and row.get("oss_key_cleaned"):
            try:
                from services.research_monitor.oss_helper import download_markdown
                oss_body = download_markdown(row["oss_key_cleaned"]) or ""
                if oss_body:
                    content = oss_body
                    row["cleaned_content"] = oss_body
                    oss_loaded += 1
            except Exception as e:
                logger.warning(
                    "[writing-style-rebuild] OSS 正文回读失败 article_id=%s: %s",
                    row.get("id"), e,
                )
        if len(content) < min_chars:
            skipped += 1
            continue
        features = extract_writing_style_features(row)
        article_structure = features.get("article_structure")
        if not isinstance(article_structure, dict):
            article_structure = extract_article_structure_features(row)
            features["article_structure"] = article_structure
        features["industry_key"] = industry_key
        style_family = features.get("style_family") or "guide"
        style_distribution[style_family] = style_distribution.get(style_family, 0) + 1
        structure_summary["sample_count"] += 1
        for key in (
            "lead_answers_question",
            "has_checklist",
            "has_price_or_budget",
            "has_risk_or_pitfall",
            "conclusion_has_decision_advice",
        ):
            if article_structure.get(key):
                structure_summary[key] += 1
        snapshot = {
            "article_id": row.get("id"),
            "source_url": row.get("url") or "",
            "industry_key": industry_key,
            "style_family": style_family,
            "intent_type": features.get("intent_type") or row.get("intent_type"),
            "content_type": features.get("content_type") or row.get("content_type") or "article",
            "features": features,
            "feature_version": features.get("feature_version") or "writing_style_features_v1_2026-06-12",
        }
        preview.append({
            "article_id": snapshot["article_id"],
            "source_url": snapshot["source_url"],
            "title": row.get("title") or "",
            "domain": features.get("domain") or row.get("domain") or "",
            "style_family": style_family,
            "intent_type": snapshot["intent_type"],
            "content_type": snapshot["content_type"],
            "numbered_sections": features.get("numbered_sections") or 0,
            "has_faq": bool(features.get("has_faq")),
            "has_price": bool(features.get("has_price")),
            "citation_count": row.get("total_citation_count") or 0,
            "adopted_count": row.get("adopted_count") or 0,
            "cited_count": row.get("cited_count") or 0,
            "search_only_count": row.get("search_only_count") or 0,
            "source_weight": float(row.get("source_weight") or 0),
            "article_structure": {
                "lead_answers_question": bool(article_structure.get("lead_answers_question")),
                "has_checklist": bool(article_structure.get("has_checklist")),
                "has_price_or_budget": bool(article_structure.get("has_price_or_budget")),
                "has_risk_or_pitfall": bool(article_structure.get("has_risk_or_pitfall")),
                "conclusion_has_decision_advice": bool(article_structure.get("conclusion_has_decision_advice")),
                "evidence_density_score": article_structure.get("evidence_density_score") or 0,
            },
            "structure_score": features.get("structure_score") or article_structure.get("evidence_density_score") or 0,
        })
        if not dry_run:
            upsert_style_feature_snapshot(snapshot)
            written += 1

    article_structure_summary = {
        **structure_summary,
        "sample_status": "ready" if structure_summary["sample_count"] >= 30 else "observing",
        "note": "样本观察中，结构规律只作参考" if structure_summary["sample_count"] < 30 else "结构字段已随文体快照生成，仍需管理员审核",
    }

    return {
        "status": "success",
        "industry_key": industry_key,
        "industry_values": industry_values,
        "dry_run": dry_run,
        "loaded": len(rows),
        "sampled": len(rows),
        "written": written,
        "skipped": skipped,
        "oss_loaded": oss_loaded,
        "preview": preview[:50],
        "style_distribution": style_distribution,
        "article_structure_summary": article_structure_summary,
        "min_chars": min_chars,
        "source": "geo_research_articles",
        "shadow_only": True,
    }


def upsert_strategy_version(candidate: dict[str, Any]) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO writing_strategy_versions (
                industry_key, strategy_version, status, style_family,
                guidance, common_elements, evidence_score, outcome_score,
                confidence, guardrails, source_summary, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (industry_key, strategy_version) DO UPDATE SET
                status = EXCLUDED.status,
                style_family = EXCLUDED.style_family,
                guidance = EXCLUDED.guidance,
                common_elements = EXCLUDED.common_elements,
                evidence_score = EXCLUDED.evidence_score,
                outcome_score = EXCLUDED.outcome_score,
                confidence = EXCLUDED.confidence,
                guardrails = EXCLUDED.guardrails,
                source_summary = EXCLUDED.source_summary,
                created_at = NOW()
            RETURNING *
        """, (
            candidate.get("industry_key") or "general",
            candidate.get("strategy_version"),
            candidate.get("status") or "shadow",
            candidate.get("style_family"),
            candidate.get("guidance"),
            Json(candidate.get("common_elements") or []),
            candidate.get("evidence_score") or 0,
            candidate.get("outcome_score") or 0,
            candidate.get("confidence") or 0,
            Json(candidate.get("guardrails") or []),
            Json(candidate.get("source_summary") or {}),
        ))
        return dict(cur.fetchone())


def list_strategy_versions(industry_key: str = "", status: str = "shadow", limit: int = 50) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        where: list[str] = []
        params: list[Any] = []
        if industry_key:
            where.append("industry_key = %s")
            params.append(industry_key)
        if status:
            where.append("status = %s")
            params.append(status)
        params.append(limit)
        cur.execute(f"""
            SELECT *
              FROM writing_strategy_versions
             {"WHERE " + " AND ".join(where) if where else ""}
             ORDER BY confidence DESC NULLS LAST, created_at DESC
             LIMIT %s
        """, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_strategy_version(strategy_id: int) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM writing_strategy_versions WHERE id = %s", (strategy_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _insert_audit(
    cur: Any,
    strategy_id: int,
    industry_key: str,
    action: str,
    actor_id: int,
    from_status: str,
    to_status: str,
    note: str = "",
) -> None:
    cur.execute("""
        INSERT INTO writing_strategy_audit_events (strategy_id, industry_key, action, actor_id, from_status, to_status, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        strategy_id,
        industry_key or "general",
        action,
        actor_id,
        from_status or "",
        to_status or "",
        note or "",
    ))


def _set_active_within_txn(cur: Any, target: dict[str, Any], reviewer_id: int) -> dict[str, Any]:
    industry_key = target.get("industry_key") or "general"
    strategy_id = int(target["id"])
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"writing_strategy:{industry_key}",))
    cur.execute("""
        UPDATE writing_strategy_versions
           SET status = 'archived',
               archived_at = NOW()
         WHERE industry_key = %s
           AND status = 'active'
           AND id <> %s
    """, (industry_key, strategy_id))
    cur.execute("""
        UPDATE writing_strategy_versions
           SET status = 'active',
               reviewed_by = COALESCE(reviewed_by, %s),
               reviewed_at = COALESCE(reviewed_at, NOW()),
               activated_by = %s,
               activated_at = NOW(),
               archived_at = NULL
         WHERE id = %s
         RETURNING *
    """, (reviewer_id, reviewer_id, strategy_id))
    row = cur.fetchone()
    return dict(row) if row else {}


def review_strategy_version(strategy_id: int, reviewer_id: int, decision: str, note: str = "") -> dict[str, Any]:
    status = "approved" if decision == "approve" else "rejected"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM writing_strategy_versions WHERE id = %s FOR UPDATE", (strategy_id,))
        target = cur.fetchone()
        if not target:
            return {}
        target = dict(target)
        from_status = target.get("status") or ""
        cur.execute("""
            UPDATE writing_strategy_versions
               SET status = %s,
                   reviewed_by = %s,
                   reviewed_at = NOW(),
                   review_note = %s
             WHERE id = %s
             RETURNING *
        """, (status, reviewer_id, note or "", strategy_id))
        row = cur.fetchone()
        _insert_audit(
            cur,
            strategy_id=strategy_id,
            industry_key=target.get("industry_key") or "general",
            action=decision,
            actor_id=reviewer_id,
            from_status=from_status,
            to_status=status,
            note=note or "",
        )
        return dict(row) if row else {}


def activate_strategy_version(strategy_id: int, reviewer_id: int, note: str = "") -> dict[str, Any]:
    """Activate one reviewed strategy per industry.

    Activation still lives in the flywheel tables.  Production writing must opt
    in explicitly before reading active rows, so this does not auto-replace live
    writing prompts.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM writing_strategy_versions WHERE id = %s FOR UPDATE", (strategy_id,))
        target = cur.fetchone()
        if not target:
            return {}
        target = dict(target)
        industry_key = target.get("industry_key") or "general"
        if target.get("status") not in {"approved", "active"}:
            return {"error": "strategy_not_approved", "strategy": target}
        from_status = target.get("status") or ""
        row = _set_active_within_txn(cur, target, reviewer_id)
        if row:
            _insert_audit(
                cur,
                strategy_id=strategy_id,
                industry_key=industry_key,
                action="activate",
                actor_id=reviewer_id,
                from_status=from_status,
                to_status="active",
                note=note or "",
            )
        return row


def rollback_strategy_version(strategy_id: int, reviewer_id: int, note: str = "") -> dict[str, Any]:
    """Re-activate an archived strategy version with an explicit audit trail."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM writing_strategy_versions WHERE id = %s FOR UPDATE", (strategy_id,))
        target = cur.fetchone()
        if not target:
            return {}
        target = dict(target)
        if target.get("status") != "archived":
            return {"error": "strategy_not_rollbackable", "strategy": target}
        industry_key = target.get("industry_key") or "general"
        row = _set_active_within_txn(cur, target, reviewer_id)
        if row:
            _insert_audit(
                cur,
                strategy_id=strategy_id,
                industry_key=industry_key,
                action="rollback",
                actor_id=reviewer_id,
                from_status="archived",
                to_status="active",
                note=note or "",
            )
        return row


def list_strategy_audit_events(strategy_id: int, limit: int = 50) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT *
              FROM writing_strategy_audit_events
             WHERE strategy_id = %s
             ORDER BY created_at DESC, id DESC
             LIMIT %s
        """, (strategy_id, max(1, min(int(limit or 50), 200))))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_active_strategy_version(industry_key: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT *
              FROM writing_strategy_versions
             WHERE industry_key = %s
               AND status = 'active'
             ORDER BY activated_at DESC NULLS LAST, reviewed_at DESC NULLS LAST
             LIMIT 1
        """, (industry_key,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def load_structure_baseline_summary(industry_key: str, limit: int = 300) -> dict[str, Any] | None:
    """Read R6 article-structure snapshots as a non-active, reference-only baseline."""
    industry_key = normalize_industry_key(industry_key or "general")
    limit = max(30, min(int(limit or 300), 500))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT source_url, style_family, features, feature_version
              FROM writing_style_feature_snapshots
             WHERE industry_key = %s
               AND feature_version LIKE 'writing_style_features_v2%%'
             ORDER BY created_at DESC, id DESC
             LIMIT %s
        """, (industry_key, limit))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    if not rows:
        return None

    family_counter: Counter[str] = Counter()
    domain_counter: set[str] = set()
    structure_counts: Counter[str] = Counter()
    structure_keys = (
        "lead_answers_question",
        "has_checklist",
        "has_price_or_budget",
        "has_risk_or_pitfall",
        "conclusion_has_decision_advice",
    )
    for row in rows:
        family = str(row.get("style_family") or "guide")
        family_counter[family] += 1
        source_url = str(row.get("source_url") or "")
        domain = urlparse(source_url).netloc.lower()
        if domain:
            domain_counter.add(domain)
        features = row.get("features") if isinstance(row.get("features"), dict) else {}
        domain_from_features = str(features.get("domain") or "").strip().lower()
        if domain_from_features:
            domain_counter.add(domain_from_features)
        article_structure = features.get("article_structure") if isinstance(features.get("article_structure"), dict) else {}
        for key in structure_keys:
            if article_structure.get(key):
                structure_counts[key] += 1

    sample_count = len(rows)
    style_family = family_counter.most_common(1)[0][0] if family_counter else "guide"
    return {
        "industry_key": industry_key,
        "style_family": style_family,
        "sample_count": sample_count,
        "style_distribution": dict(family_counter),
        "structure_counts": dict(structure_counts),
        "source_summary": {
            "style_feature_count": sample_count,
            "search_only_control_count": sample_count,
            "sample_domain_count": len(domain_counter),
            "source_signal_count": sample_count,
        },
    }


def load_strategy_generation_inputs(industry_key: str, limit: int = 200) -> dict[str, list[dict[str, Any]]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT features
              FROM writing_style_feature_snapshots
             WHERE industry_key = %s
             ORDER BY created_at DESC
             LIMIT %s
        """, (industry_key, limit))
        style_features = [
            r["features"] if isinstance(r["features"], dict) else {}
            for r in cur.fetchall()
        ]
        cur.execute("""
            SELECT balanced_weight, signal_tier
              FROM geo_research_source_signals
             WHERE industry_key = %s
             ORDER BY balanced_weight DESC
             LIMIT %s
        """, (industry_key, limit))
        source_signals = [dict(r) for r in cur.fetchall()]
        # [FIX-4] 按版本取最新窗口一行(不跨周对同版本求和 → 消费端不线性膨胀)。
        #   DISTINCT ON(COALESCE(new_version_id, id::text)):有 version 的同版本只留最新 observed_at
        #   一行(跨周去重);version NULL 的真实单篇 outcome 用 id 各自唯一,全保留(不误伤)。
        cur.execute("""
            SELECT publish_status, ai_citations_delta_30d, monitoring_brand_score_delta_30d
              FROM (
                SELECT DISTINCT ON (COALESCE(new_version_id, id::text))
                       publish_status, ai_citations_delta_30d, monitoring_brand_score_delta_30d, observed_at
                  FROM writing_strategy_outcome_events
                 WHERE industry_key = %s
                 ORDER BY COALESCE(new_version_id, id::text), observed_at DESC
              ) latest
             ORDER BY observed_at DESC
             LIMIT %s
        """, (industry_key, limit))
        outcome_signals = [dict(r) for r in cur.fetchall()]
        return {
            "style_features": style_features,
            "source_signals": source_signals,
            "outcome_signals": outcome_signals,
        }
    finally:
        conn.close()
