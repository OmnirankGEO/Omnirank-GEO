"""DB helpers for GEO media entity flywheel shadow layer."""

from __future__ import annotations

from db.schema_guard import add_column_if_missing
import json
from typing import Any, Sequence

from psycopg2.extras import Json

from db.connection import get_connection, get_db
from services.media_entity_flywheel import is_all_industry_scope


def _jsonb(value: Any) -> Json:
    return Json(value, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str))


def init_media_entity_flywheel_tables() -> None:
    """Create Phase 1 media entity flywheel tables idempotently."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_industry_taxonomy (
                id BIGSERIAL PRIMARY KEY,
                industry_key VARCHAR(100) NOT NULL UNIQUE,
                display_name VARCHAR(200) NOT NULL,
                aliases JSONB DEFAULT '[]'::jsonb,
                parent_key VARCHAR(100),
                active BOOLEAN DEFAULT TRUE,
                source VARCHAR(50) DEFAULT 'system',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_industry_taxonomy_active ON geo_industry_taxonomy(active, industry_key)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_media_entities (
                id BIGSERIAL PRIMARY KEY,
                entity_key VARCHAR(80) NOT NULL UNIQUE,
                canonical_name VARCHAR(300) NOT NULL,
                domain VARCHAR(300),
                aliases JSONB DEFAULT '[]'::jsonb,
                entity_type VARCHAR(40) DEFAULT 'media_site',
                reference_status VARCHAR(40) DEFAULT 'reference_only',
                ownership_scope VARCHAR(40) DEFAULT 'unknown',
                home_url TEXT,
                tags JSONB DEFAULT '{}'::jsonb,
                confidence NUMERIC(5,4) DEFAULT 0.7000,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_entities_domain ON geo_media_entities(domain)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_entities_status ON geo_media_entities(reference_status)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_media_inventory_mappings (
                id BIGSERIAL PRIMARY KEY,
                entity_id BIGINT NOT NULL REFERENCES geo_media_entities(id) ON DELETE CASCADE,
                media_source VARCHAR(30) NOT NULL,
                inventory_id BIGINT NOT NULL,
                media_name TEXT,
                price_yuan NUMERIC(12,2),
                price_points BIGINT,
                inventory_status VARCHAR(40) DEFAULT 'active',
                match_method VARCHAR(40),
                match_confidence NUMERIC(5,4) DEFAULT 0,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (media_source, inventory_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_inventory_entity ON geo_media_inventory_mappings(entity_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_inventory_source ON geo_media_inventory_mappings(media_source, inventory_id)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_media_citation_rollups (
                id BIGSERIAL PRIMARY KEY,
                entity_key VARCHAR(80) NOT NULL,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                engine VARCHAR(40) NOT NULL DEFAULT 'all',
                signal_tier VARCHAR(40) NOT NULL,
                answer_credit NUMERIC(12,6) DEFAULT 0,
                answer_adopted_count INTEGER DEFAULT 0,
                cited_count INTEGER DEFAULT 0,
                search_only_count INTEGER DEFAULT 0,
                reference_only_count INTEGER DEFAULT 0,
                prompt_count INTEGER DEFAULT 0,
                source_count_total INTEGER DEFAULT 0,
                evidence JSONB DEFAULT '{}'::jsonb,
                version VARCHAR(80) NOT NULL,
                last_seen_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (entity_key, industry_key, engine, signal_tier, version)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_rollups_entity ON geo_media_citation_rollups(entity_key, industry_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_rollups_weight ON geo_media_citation_rollups(answer_credit DESC)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS media_entity_score_snapshots (
                id BIGSERIAL PRIMARY KEY,
                entity_id BIGINT NOT NULL REFERENCES geo_media_entities(id) ON DELETE CASCADE,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                score_version VARCHAR(80) NOT NULL,
                shadow_score NUMERIC(6,2) NOT NULL,
                evidence_score NUMERIC(6,2),
                quality_score NUMERIC(6,2),
                inventory_score NUMERIC(6,2),
                outcome_score NUMERIC(6,2),
                reference_status VARCHAR(40) DEFAULT 'reference_only',
                is_purchasable BOOLEAN DEFAULT FALSE,
                reasons JSONB DEFAULT '[]'::jsonb,
                evidence JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (entity_id, industry_key, score_version)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_media_entity_scores_industry ON media_entity_score_snapshots(industry_key, shadow_score DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_media_entity_scores_purchasable ON media_entity_score_snapshots(is_purchasable, shadow_score DESC)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_media_binding_candidates (
                id BIGSERIAL PRIMARY KEY,
                candidate_key VARCHAR(240) NOT NULL UNIQUE,
                entity_key VARCHAR(80) NOT NULL,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                media_source VARCHAR(30) NOT NULL,
                inventory_id BIGINT NOT NULL,
                media_name TEXT,
                inventory_url TEXT,
                match_method VARCHAR(40),
                match_confidence NUMERIC(5,4) DEFAULT 0,
                can_approve BOOLEAN DEFAULT FALSE,
                status VARCHAR(40) DEFAULT 'candidate',
                risk_flags JSONB DEFAULT '[]'::jsonb,
                evidence JSONB DEFAULT '{}'::jsonb,
                created_by BIGINT,
                reviewed_by BIGINT,
                review_note TEXT,
                approved_mapping_id BIGINT,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                reviewed_at TIMESTAMPTZ,
                UNIQUE (entity_key, industry_key, media_source, inventory_id)
            )
        """)
        add_column_if_missing(cur, "geo_media_binding_candidates", "active", "BOOLEAN DEFAULT TRUE")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_binding_candidates_industry ON geo_media_binding_candidates(industry_key, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_binding_candidates_entity ON geo_media_binding_candidates(entity_key, industry_key)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_media_binding_audit_events (
                id BIGSERIAL PRIMARY KEY,
                candidate_id BIGINT REFERENCES geo_media_binding_candidates(id) ON DELETE SET NULL,
                entity_key VARCHAR(80),
                industry_key VARCHAR(100) DEFAULT 'general',
                event_type VARCHAR(40) NOT NULL,
                operator_id BIGINT,
                note TEXT,
                payload JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_binding_audit_candidate ON geo_media_binding_audit_events(candidate_id, created_at DESC)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_media_takeover_policies (
                id BIGSERIAL PRIMARY KEY,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                status VARCHAR(40) NOT NULL DEFAULT 'draft',
                scope_type VARCHAR(40) NOT NULL DEFAULT 'shadow_only',
                whitelist JSONB DEFAULT '{}'::jsonb,
                gate_summary JSONB DEFAULT '{}'::jsonb,
                review_note TEXT,
                created_by BIGINT,
                reviewed_by BIGINT,
                disabled_by BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                reviewed_at TIMESTAMPTZ,
                disabled_at TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_geo_media_takeover_one_active_shadow
                ON geo_media_takeover_policies(industry_key)
             WHERE status = 'ready_shadow'
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_takeover_policy_industry ON geo_media_takeover_policies(industry_key, status, updated_at DESC)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_media_takeover_audit_events (
                id BIGSERIAL PRIMARY KEY,
                policy_id BIGINT REFERENCES geo_media_takeover_policies(id) ON DELETE SET NULL,
                industry_key VARCHAR(100) DEFAULT 'general',
                event_type VARCHAR(40) NOT NULL,
                operator_id BIGINT,
                note TEXT,
                payload JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_geo_media_takeover_audit_policy ON geo_media_takeover_audit_events(policy_id, created_at DESC)")


def upsert_media_entity(entity: dict[str, Any]) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO geo_media_entities (
                entity_key, canonical_name, domain, aliases, entity_type,
                reference_status, ownership_scope, home_url, tags, confidence,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (entity_key) DO UPDATE SET
                canonical_name = EXCLUDED.canonical_name,
                domain = EXCLUDED.domain,
                aliases = EXCLUDED.aliases,
                entity_type = EXCLUDED.entity_type,
                home_url = EXCLUDED.home_url,
                tags = EXCLUDED.tags,
                confidence = EXCLUDED.confidence,
                updated_at = NOW()
            RETURNING *
        """, (
            entity["entity_key"],
            entity["canonical_name"],
            entity.get("domain"),
            _jsonb(entity.get("aliases") or []),
            entity.get("entity_type") or "media_site",
            entity.get("reference_status") or "reference_only",
            entity.get("ownership_scope") or "unknown",
            entity.get("home_url") or "",
            _jsonb(entity.get("tags") or {}),
            entity.get("confidence") or 0.7,
        ))
        return dict(cur.fetchone())


def upsert_inventory_mapping(entity_id: int, match: dict[str, Any]) -> dict[str, Any]:
    inventory_id = int(match.get("inventory_id") or match.get("media_id") or match.get("id") or 0)
    if inventory_id <= 0:
        raise ValueError("inventory_id must be positive")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO geo_media_inventory_mappings (
                entity_id, media_source, inventory_id, media_name, price_yuan,
                price_points, inventory_status, match_method, match_confidence,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (media_source, inventory_id) DO UPDATE SET
                entity_id = EXCLUDED.entity_id,
                media_name = EXCLUDED.media_name,
                price_yuan = EXCLUDED.price_yuan,
                price_points = EXCLUDED.price_points,
                inventory_status = EXCLUDED.inventory_status,
                match_method = EXCLUDED.match_method,
                match_confidence = EXCLUDED.match_confidence,
                updated_at = NOW()
            RETURNING *
        """, (
            entity_id,
            match.get("media_source") or "media",
            inventory_id,
            match.get("media_name") or match.get("platform_name") or "",
            match.get("price_yuan") or match.get("our_price_yuan") or match.get("price"),
            match.get("price_points") or match.get("our_price_points"),
            "active" if match.get("is_purchasable", False) else "reference_only",
            match.get("match_method") or "",
            match.get("match_confidence") or 0,
        ))
        return dict(cur.fetchone())


def upsert_score_snapshot(entity_id: int, score: dict[str, Any]) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO media_entity_score_snapshots (
                entity_id, industry_key, score_version, shadow_score,
                evidence_score, quality_score, inventory_score, outcome_score,
                reference_status, is_purchasable, reasons, evidence, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (entity_id, industry_key, score_version) DO UPDATE SET
                shadow_score = EXCLUDED.shadow_score,
                evidence_score = EXCLUDED.evidence_score,
                quality_score = EXCLUDED.quality_score,
                inventory_score = EXCLUDED.inventory_score,
                outcome_score = EXCLUDED.outcome_score,
                reference_status = EXCLUDED.reference_status,
                is_purchasable = EXCLUDED.is_purchasable,
                reasons = EXCLUDED.reasons,
                evidence = EXCLUDED.evidence,
                created_at = NOW()
            RETURNING *
        """, (
            entity_id,
            score.get("industry_key") or "general",
            score.get("score_version"),
            score.get("shadow_score"),
            score.get("evidence_score"),
            score.get("quality_score"),
            score.get("inventory_score"),
            score.get("outcome_score"),
            score.get("reference_status") or "reference_only",
            bool(score.get("is_purchasable")),
            _jsonb(score.get("reasons") or []),
            _jsonb(score.get("evidence") or {}),
        ))
        return dict(cur.fetchone())


def list_shadow_media_entities(
    *,
    industry_key: str = "",
    only_purchasable: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        where = []
        params: list[Any] = []
        if industry_key and not is_all_industry_scope(industry_key):
            where.append("s.industry_key = %s")
            params.append(industry_key)
        if only_purchasable:
            where.append("s.is_purchasable = TRUE")
        params.append(limit)
        # [GEO-R2-CAN-013] 全行业(无 industry_key)快照榜:同一 entity 可能在多个不相关行业/
        # score_version 都有快照,原来无去重直接 LIMIT → 同一实体多条重复快照挤占 LIMIT 名额,
        # 且"全局"分数会退化成某个不相关行业的任意一条。这里先按 entity 用 DISTINCT ON 只保留
        # 该实体最佳的一条快照(purchasable/shadow/evidence 排序),去重后再做全局排序 + LIMIT。
        cur.execute(f"""
            SELECT * FROM (
                SELECT DISTINCT ON (e.id)
                       e.entity_key, e.canonical_name, e.domain, e.aliases, e.home_url,
                       s.industry_key, s.score_version, s.shadow_score,
                       s.evidence_score, s.quality_score, s.inventory_score,
                       s.outcome_score, s.reference_status, s.is_purchasable,
                       s.reasons, s.evidence, s.created_at
                  FROM media_entity_score_snapshots s
                  JOIN geo_media_entities e ON e.id = s.entity_id
                 {"WHERE " + " AND ".join(where) if where else ""}
                 ORDER BY e.id, s.is_purchasable DESC, s.shadow_score DESC,
                          s.evidence_score DESC, s.created_at DESC
            ) dedup
             ORDER BY dedup.is_purchasable DESC, dedup.shadow_score DESC,
                      dedup.evidence_score DESC
             LIMIT %s
        """, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_shadow_industry_keys() -> list[str]:
    """[R5] 有 shadow 快照数据的行业 key 清单(DISTINCT · 排除 ''/general)。

    供发布榜近似行业 fallback:细分长尾行业无数据时,在"确有数据的行业"里找相近者。
    小结果集(行业数量级),走 industry_key 索引,毫秒级。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT industry_key
              FROM media_entity_score_snapshots
             WHERE industry_key IS NOT NULL
               AND industry_key <> ''
               AND industry_key <> 'general'
        """)
        return [r["industry_key"] for r in cur.fetchall() if r["industry_key"]]
    finally:
        conn.close()


def get_media_entity_for_binding(entity_key: str, industry_key: str = "") -> dict[str, Any] | None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = [entity_key]
        industry_filter = ""
        if industry_key:
            industry_filter = "AND s.industry_key = %s"
            params.append(industry_key)
        cur.execute(f"""
            SELECT e.id, e.entity_key, e.canonical_name, e.domain, e.aliases,
                   e.home_url, e.tags, e.confidence,
                   s.industry_key, s.score_version, s.evidence AS score_evidence,
                   s.reasons, s.shadow_score, s.reference_status, s.is_purchasable
              FROM geo_media_entities e
              LEFT JOIN media_entity_score_snapshots s ON s.entity_id = e.id
             WHERE e.entity_key = %s
               {industry_filter}
             ORDER BY s.created_at DESC NULLS LAST
             LIMIT 1
        """, params)
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_shadow_scores_by_entity_keys(entity_keys: Sequence[str], industry_key: str = "") -> dict[str, float]:
    """[B6-1] 批量取实体最新 shadow_score(按 entity_key)→ {entity_key: shadow_score}。

    industry_key 给定则限该行业或 general 的快照;取每实体最新一条(DISTINCT ON created_at DESC)。
    """
    keys = [k for k in (entity_keys or []) if k]
    if not keys:
        return {}
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = [keys]
        ind_clause = ""
        if industry_key:
            ind_clause = "AND (s.industry_key = %s OR s.industry_key = 'general')"
            params.append(industry_key)
        cur.execute(f"""
            SELECT DISTINCT ON (e.entity_key) e.entity_key, s.shadow_score
              FROM geo_media_entities e
              JOIN media_entity_score_snapshots s ON s.entity_id = e.id
             WHERE e.entity_key = ANY(%s) {ind_clause}
             ORDER BY e.entity_key, s.created_at DESC
        """, params)
        return {
            r["entity_key"]: float(r["shadow_score"])
            for r in cur.fetchall()
            if r["shadow_score"] is not None
        }
    finally:
        conn.close()


def get_outcome_rollups_by_entity_keys(
    entity_keys: Sequence[str], industry_key: str = ""
) -> dict[str, dict[str, Any]]:
    """[E5] 批量取实体最新快照里的 evidence.outcome_rollup(自家发布效果回流)→ {entity_key: rollup}。

    数据由 services.media_outcome_sync.sync_media_entity_outcome_rollups(B5-1)写入:
    该任务只在实体有真实 published_count / citation_lift 信号时才回写 outcome_rollup,
    所以 0 发布时本函数对所有 key 都返回不到该键(→ E5 校准恒 dormant no-op)。

    只返回含非空 outcome_rollup 的实体(published_count=0 且无信号的实体不进结果),
    纯只读、fail-soft、不伪造 outcome。取每实体最新一条快照(DISTINCT ON created_at DESC)。
    """
    keys = [k for k in (entity_keys or []) if k]
    if not keys:
        return {}
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = [keys]
        ind_clause = ""
        if industry_key:
            ind_clause = "AND (s.industry_key = %s OR s.industry_key = 'general')"
            params.append(industry_key)
        cur.execute(f"""
            SELECT DISTINCT ON (e.entity_key) e.entity_key, s.evidence
              FROM geo_media_entities e
              JOIN media_entity_score_snapshots s ON s.entity_id = e.id
             WHERE e.entity_key = ANY(%s) {ind_clause}
             ORDER BY e.entity_key, s.created_at DESC
        """, params)
        result: dict[str, dict[str, Any]] = {}
        for r in cur.fetchall():
            evidence = r["evidence"] if isinstance(r["evidence"], dict) else {}
            rollup = evidence.get("outcome_rollup")
            if isinstance(rollup, dict) and rollup:
                result[r["entity_key"]] = rollup
        return result
    finally:
        conn.close()


def _search_clauses(column: str, terms: Sequence[str], params: list[Any]) -> str:
    clauses = []
    for term in terms:
        clean = str(term or "").strip()
        if len(clean) < 2:
            continue
        clauses.append(f"{column} ILIKE %s")
        params.append(f"%{clean}%")
    return " OR ".join(clauses)


def list_media_inventory_candidates(
    *,
    names: Sequence[str],
    domain: str = "",
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Load media-box inventory rows likely related to a media entity."""
    conn = get_connection()
    try:
        params_media: list[Any] = []
        params_wemedia: list[Any] = []
        media_conditions = ["is_active = TRUE"]
        wemedia_conditions = ["is_active = TRUE"]

        name_clause = _search_clauses("media_name", names, params_media)
        wemedia_name_clause = _search_clauses("toutiao_name", names, params_wemedia)
        if name_clause:
            media_conditions.append(f"({name_clause})")
        if wemedia_name_clause:
            wemedia_conditions.append(f"({wemedia_name_clause})")
        if domain:
            media_conditions.append("entrance_link ILIKE %s")
            params_media.append(f"%{domain}%")
            wemedia_conditions.append("entrance_link ILIKE %s")
            params_wemedia.append(f"%{domain}%")

        if len(media_conditions) == 1 and len(wemedia_conditions) == 1:
            return []

        cur = conn.cursor()
        cur.execute(f"""
            (
                SELECT 'mhz_media' AS media_source,
                       id AS inventory_id,
                       media_name,
                       entrance_link,
                       price,
                       our_price_yuan,
                       our_price_points,
                       area,
                       portal_media,
                       resource_type_name,
                       geo_rank_platform,
                       authority_media,
                       is_active
                  FROM mhz_media
                 WHERE is_active = TRUE
                   AND ({" OR ".join(media_conditions[1:]) if len(media_conditions) > 1 else "FALSE"})
                 ORDER BY COALESCE(our_price_yuan, price, 0) DESC NULLS LAST
                 LIMIT %s
            )
            UNION ALL
            (
                SELECT 'mhz_wemedia' AS media_source,
                       id AS inventory_id,
                       toutiao_name AS media_name,
                       entrance_link,
                       price,
                       our_price_yuan,
                       our_price_points,
                       province AS area,
                       platform AS portal_media,
                       industry AS resource_type_name,
                       geo_rank_platform,
                       authority_media,
                       is_active
                  FROM mhz_wemedia
                 WHERE is_active = TRUE
                   AND ({" OR ".join(wemedia_conditions[1:]) if len(wemedia_conditions) > 1 else "FALSE"})
                 ORDER BY COALESCE(our_price_yuan, price, 0) DESC NULLS LAST
                 LIMIT %s
            )
        """, params_media + [limit] + params_wemedia + [limit])
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def upsert_media_binding_candidate(candidate: dict[str, Any], operator_id: int | None = None) -> dict[str, Any]:
    inv = candidate.get("inventory") or {}
    entity_key = candidate.get("entity_key")
    industry_key = candidate.get("industry_key") or "general"
    media_source = inv.get("media_source") or "mhz_media"
    inventory_id = int(inv.get("inventory_id") or 0)
    with get_db() as conn:
        cur = conn.cursor()
        # [P2 deleted 复活 2026-07-03] 先探同键旧行状态:供 ON CONFLICT 条件复活 +
        #   调用方据 _revived_from_deleted 写 audit(insert_media_binding_audit_event 自开事务,
        #   不在此嵌套调用)。
        #   FOR UPDATE 锁住旧行到本事务(get_db) commit:阻塞并发 admin 改 status 与本 upsert 交错,
        #   否则「pre-SELECT 读到非 deleted → admin 删除 → ON CONFLICT 却复活并清空人工决策却不写 audit」
        #   的竞态会无声擦除人工审核痕迹(出口审核 Finding 2)。
        cur.execute(
            """
            SELECT status FROM geo_media_binding_candidates
             WHERE entity_key = %s AND industry_key = %s
               AND media_source = %s AND inventory_id = %s
             FOR UPDATE
            """,
            (entity_key, industry_key, media_source, inventory_id),
        )
        _prior = cur.fetchone()
        _prior_status = _prior["status"] if _prior else None
        cur.execute("""
            INSERT INTO geo_media_binding_candidates (
                candidate_key, entity_key, industry_key, media_source, inventory_id,
                media_name, inventory_url, match_method, match_confidence,
                can_approve, status, risk_flags, evidence, created_by,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'candidate', %s, %s, %s, NOW(), NOW())
            ON CONFLICT (entity_key, industry_key, media_source, inventory_id) DO UPDATE SET
                candidate_key = EXCLUDED.candidate_key,
                media_name = EXCLUDED.media_name,
                inventory_url = EXCLUDED.inventory_url,
                match_method = EXCLUDED.match_method,
                match_confidence = EXCLUDED.match_confidence,
                can_approve = EXCLUDED.can_approve,
                risk_flags = EXCLUDED.risk_flags,
                evidence = EXCLUDED.evidence,
                -- [P2 复活] 仅旧行 status='deleted' 才复活成 candidate 并清空人工决策痕迹;
                --   rejected / approved / candidate 一律保留原状(绝不覆盖人工决策)。
                status = CASE WHEN geo_media_binding_candidates.status = 'deleted'
                              THEN 'candidate' ELSE geo_media_binding_candidates.status END,
                active = CASE WHEN geo_media_binding_candidates.status = 'deleted'
                              THEN TRUE ELSE geo_media_binding_candidates.active END,
                reviewed_by = CASE WHEN geo_media_binding_candidates.status = 'deleted'
                                   THEN NULL ELSE geo_media_binding_candidates.reviewed_by END,
                reviewed_at = CASE WHEN geo_media_binding_candidates.status = 'deleted'
                                   THEN NULL ELSE geo_media_binding_candidates.reviewed_at END,
                review_note = CASE WHEN geo_media_binding_candidates.status = 'deleted'
                                   THEN NULL ELSE geo_media_binding_candidates.review_note END,
                approved_mapping_id = CASE WHEN geo_media_binding_candidates.status = 'deleted'
                                           THEN NULL ELSE geo_media_binding_candidates.approved_mapping_id END,
                updated_at = NOW()
            RETURNING *
        """, (
            candidate.get("candidate_key"),
            entity_key,
            industry_key,
            media_source,
            inventory_id,
            inv.get("media_name") or "",
            inv.get("url") or "",
            candidate.get("match_method") or "",
            candidate.get("match_confidence") or 0,
            bool(candidate.get("can_approve")),
            _jsonb(candidate.get("risk_flags") or []),
            _jsonb(candidate.get("evidence") or {}),
            operator_id,
        ))
        row = dict(cur.fetchone())
        # 供调用方决定是否写 revived_from_deleted audit event。旧行不存在 / 非 deleted → False。
        row["_revived_from_deleted"] = _prior_status == "deleted"
        return row


def list_media_binding_candidates(
    *,
    industry_key: str = "",
    status: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        where = ["COALESCE(active, TRUE) = TRUE"]
        params: list[Any] = []
        if industry_key and not is_all_industry_scope(industry_key):
            where.append("industry_key = %s")
            params.append(industry_key)
        if status:
            where.append("status = %s")
            params.append(status)
        else:
            # [P1-2 2026-07-03] soft-deleted 候选默认不进列表/计数(待审队列不应含已删)。
            #   显式传 status='deleted' 仍可查(运维审计用)。
            where.append("status <> 'deleted'")
        params.append(limit)
        cur.execute(f"""
            SELECT *
              FROM geo_media_binding_candidates
             {"WHERE " + " AND ".join(where) if where else ""}
             ORDER BY can_approve DESC, match_confidence DESC, updated_at DESC
             LIMIT %s
        """, params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    # [P0-1 2026-08-15] 待审行也走同一份实时判据再返回 —— 否则前端 isRecommendedBinding
    #   仍读陈旧快照,列表里那条会显示「建议通过」、点下去却 409,和一键按钮同一个病。
    #   🔴 只重算 status='candidate':approved / rejected 是人工决策的历史事实(审计资产),
    #      不能被今天的库存状态改写。
    pending = [r for r in rows if str(r.get("status") or "candidate") == "candidate"]
    if not pending:
        return rows
    revalidated = {int(r["id"]): r for r in revalidate_binding_candidate_rows(pending)}
    return [revalidated.get(int(r["id"]), r) for r in rows]


#: [P0-1 2026-08-15] 待重算候选的扫描上限。选集不再靠快照列过滤,先取结构性超集
#: (status/active,这两列没有「供应商下架」这种会背着我们变化的语义)再逐条实时重算。
#: 生产实测 status=candidate+active = 1559 行 / 25 实体 / 154 库存,离上限很远;
#: 超集条数触顶时调用方拿 truncated 诚实暴露,不假装「已全部覆盖」。
REVALIDATION_SCAN_LIMIT = 20000


def _list_candidate_rows_for_revalidation(limit: int) -> list[dict[str, Any]]:
    """[P0-1] 待重算超集:只按 status/active 取,**不碰 can_approve / risk_flags / match_confidence**。

    🔴 这三列都是写入时快照,正是死锁的来源;用它们做预过滤会让「库存重新上架」的候选
    永远回不来(出口闸的反向对照会当场抓到)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM geo_media_binding_candidates
             WHERE status = 'candidate'
               AND COALESCE(active, TRUE) = TRUE
             ORDER BY match_confidence DESC, updated_at DESC
             LIMIT %s
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def revalidate_binding_candidate_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """[P0-1 2026-08-15] 给候选行批量附上**实时**判据,覆盖陈旧快照列。

    判据本身一个字都不在这里写 —— 全部来自 `services.media_binding_candidates.evaluate_candidate_live`,
    也就是审核路径 `verify_candidate_for_approval` 调的那同一个函数。本函数只负责:
      1. 把每行需要的 entity / inventory 装配好(同一 key 只查一次);
      2. 用返回的实时 can_approve / risk_flags / match_confidence 覆盖快照列;
      3. 附加 `live_block_reason`(空串 = 实时判定可通过),供选集过滤与 UI 展示原因。

    🔴 实体与库存一律走既有的单条取数函数 `get_media_entity_for_binding` /
       `get_media_inventory_item` —— 不另写批量 SQL,否则「同一份 SQL」的保证就没了。
       去重后生产量级约 25 实体 + 154 库存,连接走池,开销可忽略。
    """
    from services.media_binding_candidates import evaluate_candidate_live

    entity_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    inventory_cache: dict[tuple[str, int], dict[str, Any] | None] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        entity_key = str(row.get("entity_key") or "")
        industry_key = str(row.get("industry_key") or "")
        media_source = str(row.get("media_source") or "mhz_media")
        try:
            inventory_id = int(row.get("inventory_id") or 0)
        except (TypeError, ValueError):
            inventory_id = 0

        ekey = (entity_key, industry_key)
        if ekey not in entity_cache:
            entity_cache[ekey] = get_media_entity_for_binding(entity_key, industry_key)
        ikey = (media_source, inventory_id)
        if ikey not in inventory_cache:
            inventory_cache[ikey] = get_media_inventory_item(media_source, inventory_id)

        verdict = evaluate_candidate_live(
            entity_cache[ekey], inventory_cache[ikey], {"inventory": row},
        )
        fresh = verdict.get("candidate") or {}
        reason = verdict.get("block_reason") or ""
        merged = dict(row)
        merged["live_block_reason"] = reason
        merged["can_approve"] = not reason
        if fresh:
            merged["risk_flags"] = fresh.get("risk_flags") or []
            merged["match_confidence"] = fresh.get("match_confidence")
        else:
            # 实体/库存整个取不到 —— 没有可信的重算结果,退化为「有风险且置信度归零」,
            # 绝不让它靠残留快照留在「建议通过」里。
            merged["risk_flags"] = [reason] if reason else []
            merged["match_confidence"] = 0
        out.append(merged)
    return out


def list_recommended_binding_candidates(
    min_confidence: float, limit: int = 2000,
) -> list[dict[str, Any]]:
    """[T6] 全库跨行业「建议通过」选集。
    min_confidence 由 API 层传入(SSOT=services.media_binding_candidates.RECOMMENDED_BINDING_MIN_CONFIDENCE),
    不接受前端传 id,避开翻页/200 上限漏项。

    [P0-1 2026-08-15] 判据改为**实时重算**:超集取 status/active → 逐条走
    `evaluate_candidate_live`(审核路径同一个函数)→ 再用 `is_recommended_binding`
    (前端 isRecommendedBinding 的同源常量口径)收口。三处判定合并成一处,
    「选集说能过、审核说不能过」的死锁在结构上不再可能出现。
    """
    return recommended_binding_candidates_scan(min_confidence, limit=limit)["items"]


def recommended_binding_candidates_scan(
    min_confidence: float, limit: int = 2000,
) -> dict[str, Any]:
    """[P0-1] 选集与计数的**唯一实现**;两个公开函数都是它的薄包装。

    返回 `{"items": [...], "scanned": n, "scan_truncated": bool}`。
    🔴 `scan_truncated` 不是装饰:重算前的超集受 `REVALIDATION_SCAN_LIMIT` 限制,
       静默截断会让计数少报而看起来像「已经清干净了」。上限碰到了就要说出来。
    """
    from services.media_binding_candidates import is_recommended_binding

    rows = _list_candidate_rows_for_revalidation(REVALIDATION_SCAN_LIMIT)
    fresh = revalidate_binding_candidate_rows(rows)
    picked = [r for r in fresh
              if not r.get("live_block_reason")
              and is_recommended_binding({**r, "match_confidence": r.get("match_confidence") or 0})
              and float(r.get("match_confidence") or 0) >= float(min_confidence)]
    return {
        "items": picked[: max(1, int(limit or 2000))],
        "scanned": len(rows),
        "scan_truncated": len(rows) >= REVALIDATION_SCAN_LIMIT,
    }


def count_recommended_binding_candidates(min_confidence: float) -> int:
    """[T6] 全库「建议通过」条数(前端一键按钮 N 来源)。

    [P0-1] 与选集共用 `recommended_binding_candidates_scan` —— 计数与选集**必须**同一份判据,
    各写一遍就是本次 bug 的原型(选集/审核各写一遍 → 按钮恒显示 4 条点不掉)。
    """
    return len(recommended_binding_candidates_scan(min_confidence, limit=REVALIDATION_SCAN_LIMIT)["items"])


def delete_inventory_mapping(mapping_id: int) -> bool:
    """[T6b 撤销] 删除一条 inventory_mapping(撤销自动/人工绑定用)。返回是否删到行。"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM geo_media_inventory_mappings WHERE id = %s", (int(mapping_id),))
        return cur.rowcount > 0


def count_other_approved_by_mapping(mapping_id: int, exclude_candidate_id: int) -> int:
    """[T6b 撤销守卫] 除本候选外,还有几个 approved 候选引用同一 mapping。
    inventory_mappings UNIQUE(media_source, inventory_id) → 两个不同实体 approve 同一库存会共享同一 mapping 行;
    撤销时若他人仍在用,则不删该行(只解绑本候选),防悬挂 approved_mapping_id(出口审核 Finding #4)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS c FROM geo_media_binding_candidates
             WHERE approved_mapping_id = %s AND status = 'approved' AND id <> %s
            """,
            (int(mapping_id), int(exclude_candidate_id)),
        )
        row = cur.fetchone()
        return int(row["c"]) if row else 0
    finally:
        conn.close()


def list_recent_auto_approved_bindings(since_days: int = 7, limit: int = 100) -> dict[str, Any]:
    """[T6b] 近 N 天自动通过绑定(event_type='自动通过绑定')的条数 + 明细,供 UI「本周自动通过 N(查看)」。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS c
              FROM geo_media_binding_audit_events
             WHERE event_type = '自动通过绑定'
               AND created_at >= NOW() - (%s || ' days')::interval
            """,
            (int(since_days),),
        )
        row = cur.fetchone()
        count = int(row["c"]) if row else 0
        cur.execute(
            """
            SELECT id, candidate_id, entity_key, industry_key, note, created_at
              FROM geo_media_binding_audit_events
             WHERE event_type = '自动通过绑定'
               AND created_at >= NOW() - (%s || ' days')::interval
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (int(since_days), int(limit)),
        )
        return {"count": count, "items": [dict(r) for r in cur.fetchall()]}
    except Exception:
        return {"count": 0, "items": []}
    finally:
        conn.close()


def list_approved_media_binding_candidates(
    *,
    industry_key: str,
    limit: int | None = 100,
    include_general: bool = False,
) -> list[dict[str, Any]]:
    """List approved, active media bindings usable by the takeover gate.

    include_general=True:精确行业**或** 'general' 都算命中(与 get_shadow_scores_by_entity_keys
    的 `industry_key = %s OR = 'general'` 口径对齐)。E1 榜/E2 排序 boost 用它——否则大量默认
    industry_key='general' 的 approved 绑定对具体行业文章恒被过滤掉,导致 boost/可投放标记 silent no-op。

    limit=None:不截断。[review fix] E1 榜/E2 boost 用本函数做**全量 membership 判定**
    (prod approved 绑定上千行量级),默认 LIMIT 100 会把置信度排位靠后的已绑定实体
    截掉 → 榜上错标「参考/待拓展」、boost 漏加(假阴性,include_general 后更易触发)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        scoped_industry = "" if is_all_industry_scope(industry_key) else industry_key
        params: list[Any] = []
        if scoped_industry and include_general:
            industry_filter = "AND (industry_key = %s OR industry_key = 'general')"
            params.append(scoped_industry)
        elif scoped_industry:
            industry_filter = "AND industry_key = %s"
            params.append(scoped_industry)
        else:
            industry_filter = ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT %s"
            params.append(int(limit))
        cur.execute(f"""
            SELECT *
              FROM geo_media_binding_candidates
             WHERE status = 'approved'
               AND can_approve = TRUE
               AND approved_mapping_id IS NOT NULL
               AND COALESCE(active, TRUE) = TRUE
               {industry_filter}
             ORDER BY match_confidence DESC, reviewed_at DESC NULLS LAST
             {limit_clause}
        """, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_media_binding_candidate(candidate_id: int) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_media_binding_candidates WHERE id = %s", (candidate_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_media_inventory_item(media_source: str, inventory_id: int) -> dict[str, Any] | None:
    if media_source == "mhz_wemedia":
        table = "mhz_wemedia"
        name_expr = "toutiao_name AS media_name"
        extra = "province AS area, platform AS portal_media, industry AS resource_type_name"
    else:
        table = "mhz_media"
        name_expr = "media_name"
        extra = "area, portal_media, resource_type_name"
        media_source = "mhz_media"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT %s AS media_source,
                   id AS inventory_id,
                   {name_expr},
                   entrance_link,
                   price,
                   our_price_yuan,
                   our_price_points,
                   {extra},
                   geo_rank_platform,
                   authority_media,
                   is_active
              FROM {table}
             WHERE id = %s
             LIMIT 1
        """, (media_source, inventory_id))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_media_binding_candidate_review(
    *,
    candidate_id: int,
    status: str,
    reviewed_by: int,
    review_note: str,
    approved_mapping_id: int | None = None,
) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE geo_media_binding_candidates
               SET status = %s,
                   reviewed_by = %s,
                   review_note = %s,
                   approved_mapping_id = %s,
                   reviewed_at = NOW(),
                   updated_at = NOW()
             WHERE id = %s
             RETURNING *
        """, (status, reviewed_by, review_note, approved_mapping_id, candidate_id))
        row = cur.fetchone()
        return dict(row) if row else {}


def insert_media_binding_audit_event(
    *,
    candidate_id: int | None,
    entity_key: str,
    industry_key: str,
    event_type: str,
    operator_id: int | None,
    note: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO geo_media_binding_audit_events (
                candidate_id, entity_key, industry_key, event_type, operator_id,
                note, payload, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING *
        """, (
            candidate_id,
            entity_key,
            industry_key or "general",
            event_type,
            operator_id,
            note,
            _jsonb(payload or {}),
        ))
        return dict(cur.fetchone())


def get_media_takeover_policy(industry_key: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT *
              FROM geo_media_takeover_policies
             WHERE industry_key = %s
             ORDER BY
                   CASE status WHEN 'ready_shadow' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                   updated_at DESC
             LIMIT 1
        """, (industry_key or "general",))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_media_takeover_policy(
    *,
    industry_key: str,
    whitelist: dict[str, Any],
    gate_summary: dict[str, Any],
    review_note: str,
    operator_id: int | None,
) -> dict[str, Any]:
    """Save one ready shadow takeover policy for an industry."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE geo_media_takeover_policies
               SET status = 'archived',
                   updated_at = NOW()
             WHERE industry_key = %s
               AND status = 'ready_shadow'
        """, (industry_key or "general",))
        cur.execute("""
            INSERT INTO geo_media_takeover_policies (
                industry_key, status, scope_type, whitelist, gate_summary,
                review_note, created_by, reviewed_by, created_at, updated_at, reviewed_at
            )
            VALUES (%s, 'ready_shadow', 'shadow_only', %s, %s, %s, %s, %s, NOW(), NOW(), NOW())
            RETURNING *
        """, (
            industry_key or "general",
            _jsonb(whitelist or {}),
            _jsonb(gate_summary or {}),
            review_note,
            operator_id,
            operator_id,
        ))
        return dict(cur.fetchone())


def disable_media_takeover_policy(
    *,
    industry_key: str,
    review_note: str,
    operator_id: int | None,
) -> dict[str, Any] | None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE geo_media_takeover_policies
               SET status = 'disabled',
                   review_note = %s,
                   disabled_by = %s,
                   disabled_at = NOW(),
                   updated_at = NOW()
             WHERE id = (
                 SELECT id
                   FROM geo_media_takeover_policies
                  WHERE industry_key = %s
                    AND status IN ('ready_shadow', 'draft')
                  ORDER BY updated_at DESC
                  LIMIT 1
             )
             RETURNING *
        """, (review_note, operator_id, industry_key or "general"))
        row = cur.fetchone()
        return dict(row) if row else None


def insert_media_takeover_audit_event(
    *,
    policy_id: int | None,
    industry_key: str,
    event_type: str,
    operator_id: int | None,
    note: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO geo_media_takeover_audit_events (
                policy_id, industry_key, event_type, operator_id, note, payload, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            RETURNING *
        """, (
            policy_id,
            industry_key or "general",
            event_type,
            operator_id,
            note,
            _jsonb(payload or {}),
        ))
        return dict(cur.fetchone())


def get_media_flywheel_coverage() -> dict[str, Any]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM geo_media_entities")
        entities = int(cur.fetchone()["cnt"] or 0)
        cur.execute("SELECT COUNT(*) AS cnt FROM geo_media_inventory_mappings")
        mappings = int(cur.fetchone()["cnt"] or 0)
        cur.execute("SELECT COUNT(*) AS cnt FROM media_entity_score_snapshots")
        scores = int(cur.fetchone()["cnt"] or 0)
        cur.execute("""
            SELECT reference_status, COUNT(*) AS cnt
              FROM media_entity_score_snapshots
             GROUP BY reference_status
        """)
        by_status = {r["reference_status"]: int(r["cnt"] or 0) for r in cur.fetchall()}

        # [B1-5] 口径统一:by_reference_status 上面按"快照行"计(同实体跨行业/跨版本多行 →
        # 4,057 口径),entities 是"实体行数"(2,718 口径),两者放同一句话会误导。
        # 这里按实体去重(每实体取最新一条快照)给出"同分母"三元组:
        # scored_entities(分母) / purchasable_entities(可投放) / by_reference_status_distinct(各状态),
        # 三者可相加解释。media_entity_score_snapshots 无 entity_key 列 → 用 entity_id FK 去重。
        cur.execute("""
            WITH latest_per_entity AS (
                SELECT DISTINCT ON (entity_id)
                       entity_id, reference_status, is_purchasable
                  FROM media_entity_score_snapshots
                 ORDER BY entity_id, created_at DESC
            )
            SELECT reference_status,
                   COUNT(*) AS cnt,
                   COUNT(*) FILTER (WHERE is_purchasable) AS purchasable_cnt
              FROM latest_per_entity
             GROUP BY reference_status
        """)
        by_status_distinct: dict[str, int] = {}
        scored_entities = 0
        purchasable_entities = 0
        for r in cur.fetchall():
            cnt = int(r["cnt"] or 0)
            by_status_distinct[r["reference_status"]] = cnt
            scored_entities += cnt
            purchasable_entities += int(r["purchasable_cnt"] or 0)

        return {
            "entities": entities,
            "inventory_mappings": mappings,
            "score_snapshots": scores,
            "by_reference_status": by_status,
            # [B1-5] 新增去重同分母口径(前端候选卡 + 媒体建议 tab 统一用这组)
            "scored_entities": scored_entities,
            "purchasable_entities": purchasable_entities,
            "by_reference_status_distinct": by_status_distinct,
        }
    finally:
        conn.close()


def _as_int(row: dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _source_signal_health(cur: Any, industry_key: str) -> dict[str, Any]:
    scoped_industry = "" if is_all_industry_scope(industry_key) else industry_key
    where = "WHERE industry_key = %s" if scoped_industry else ""
    params: list[Any] = [scoped_industry] if scoped_industry else []
    cur.execute(f"""
        SELECT COUNT(*) AS source_signal_count,
               COUNT(*) FILTER (WHERE signal_tier = 'answer_adopted') AS answer_adoption_count,
               COUNT(*) FILTER (WHERE signal_tier = 'cited_source') AS explicit_citation_count,
               COUNT(*) FILTER (WHERE signal_tier = 'search_result_only') AS search_exposure_count,
               COUNT(*) FILTER (WHERE signal_tier = 'rejected_noise') AS noise_count,
               COUNT(DISTINCT domain) AS source_domain_count
          FROM geo_research_source_signals
          {where}
    """, params)
    return dict(cur.fetchone() or {})


def _raw_health(cur: Any, industry_values: Sequence[str]) -> dict[str, Any]:
    where = "WHERE industry = ANY(%s)" if industry_values else ""
    params: list[Any] = [list(industry_values)] if industry_values else []
    cur.execute(f"""
        SELECT COUNT(*) AS raw_citation_rows,
               COUNT(*) FILTER (WHERE COALESCE(is_answer_cited, FALSE) = TRUE) AS raw_answer_cited_count
          FROM geo_research_raw
          {where}
    """, params)
    return dict(cur.fetchone() or {})


def _article_health(cur: Any, industry_values: Sequence[str]) -> dict[str, Any]:
    where = "WHERE primary_industry = ANY(%s)" if industry_values else ""
    params: list[Any] = [list(industry_values)] if industry_values else []
    cur.execute(f"""
        SELECT COUNT(*) AS article_count,
               COUNT(*) FILTER (
                   WHERE GREATEST(COALESCE(cleaned_char_count, 0), CHAR_LENGTH(COALESCE(inline_cleaned_content, ''))) >= 500
                     AND COALESCE(review_status, '') <> 'rejected'
                     AND COALESCE(clean_status, '') <> 'failed'
               ) AS article_body_count,
               COUNT(*) FILTER (
                   WHERE COALESCE(review_status, '') = 'rejected'
                      OR COALESCE(clean_status, '') = 'failed'
                      OR GREATEST(COALESCE(cleaned_char_count, 0), CHAR_LENGTH(COALESCE(inline_cleaned_content, ''))) < 500
               ) AS failed_body_count
          FROM geo_research_articles
          {where}
    """, params)
    return dict(cur.fetchone() or {})


def _citation_health(cur: Any, industry_values: Sequence[str]) -> dict[str, Any]:
    join = ""
    where = ""
    params: list[Any] = []
    if industry_values:
        join = "JOIN geo_research_articles a ON a.id = c.article_id"
        where = "WHERE a.primary_industry = ANY(%s)"
        params.append(list(industry_values))
    cur.execute(f"""
        SELECT COUNT(*) AS citation_rows,
               COUNT(*) FILTER (WHERE c.raw_id IS NULL) AS raw_id_missing
          FROM geo_research_article_citations c
          {join}
          {where}
    """, params)
    return dict(cur.fetchone() or {})


def _legacy_dedup_health(cur: Any) -> dict[str, Any]:
    cur.execute("""
        SELECT COUNT(*) AS legacy_duplicate_groups
          FROM (
              SELECT round_id, industry_id, prompt_text, platform
                FROM geo_research_round_call
               WHERE round_id LIKE 'round_legacy_%'
               GROUP BY round_id, industry_id, prompt_text, platform
              HAVING COUNT(*) > 1
          ) d
    """)
    duplicate_row = dict(cur.fetchone() or {})
    cur.execute("""
        SELECT EXISTS (
            SELECT 1
              FROM pg_indexes
             WHERE tablename = 'geo_research_round_call'
               AND indexname = 'idx_geo_research_round_call_legacy_unique'
        ) AS legacy_unique_index_exists
    """)
    index_row = dict(cur.fetchone() or {})
    return {**duplicate_row, **index_row}


def get_flywheel_data_health(
    *,
    industry_key: str = "",
    industry_values: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return read-only data health for the admin review console."""
    scoped_industry = "" if is_all_industry_scope(industry_key) else industry_key
    values = [] if is_all_industry_scope(industry_key) else [v for v in (industry_values or []) if v]
    conn = get_connection()
    try:
        cur = conn.cursor()
        source = _source_signal_health(cur, scoped_industry)
        raw = _raw_health(cur, values)
        articles = _article_health(cur, values)
        citations = _citation_health(cur, values)
        legacy = _legacy_dedup_health(cur)
    finally:
        conn.close()

    metrics = {
        "source_signal_count": _as_int(source, "source_signal_count"),
        "source_domain_count": _as_int(source, "source_domain_count"),
        "answer_adoption_count": _as_int(source, "answer_adoption_count"),
        "explicit_citation_count": _as_int(source, "explicit_citation_count"),
        "search_exposure_count": _as_int(source, "search_exposure_count"),
        "noise_count": _as_int(source, "noise_count"),
        "raw_citation_rows": _as_int(raw, "raw_citation_rows"),
        "raw_answer_cited_count": _as_int(raw, "raw_answer_cited_count"),
        "article_count": _as_int(articles, "article_count"),
        "article_body_count": _as_int(articles, "article_body_count"),
        "failed_body_count": _as_int(articles, "failed_body_count"),
        "citation_rows": _as_int(citations, "citation_rows"),
        "raw_id_missing": _as_int(citations, "raw_id_missing"),
        "legacy_duplicate_groups": _as_int(legacy, "legacy_duplicate_groups"),
        "legacy_unique_index_exists": bool(legacy.get("legacy_unique_index_exists")),
    }

    blockers: list[str] = []
    warnings: list[str] = []
    if metrics["source_signal_count"] <= 0:
        blockers.append("还没有可用于飞轮的来源证据，请先预览或保存来源证据。")
    if metrics["article_body_count"] <= 0:
        blockers.append("当前行业没有可抽取文章风格的正文，请先补录或抓取原文。")
    if metrics["raw_id_missing"] > 0:
        blockers.append("部分引用记录没有连回原始采集行，需先修复 raw_id 关联。")
    if metrics["legacy_duplicate_groups"] > 0:
        blockers.append("历史补录存在重复调用记录，需先完成去重。")
    if not metrics["legacy_unique_index_exists"]:
        blockers.append("历史补录防重复索引不存在，重跑导入可能再次翻倍。")
    if metrics["answer_adoption_count"] <= 0 and metrics["explicit_citation_count"] <= 0:
        warnings.append("暂未识别到答案采纳或明确引用，当前更多是搜索曝光参考。")
    if metrics["noise_count"] > max(10, metrics["source_signal_count"] * 0.2):
        warnings.append("低质量或被拒来源占比较高，建议先清理来源质量。")

    if blockers:
        level = "red"
        label = "需先修复数据"
        summary = blockers[0]
    elif warnings:
        level = "yellow"
        label = "可预览，谨慎启用"
        summary = warnings[0]
    else:
        level = "green"
        label = "数据可用"
        summary = "来源证据、正文、引用关联和历史去重状态都满足审核前检查。"

    return {
        "level": level,
        "label": label,
        "summary": summary,
        "reasons": blockers + warnings,
        "next_action": "先处理红色问题" if blockers else ("建议人工复核后再启用" if warnings else "可以进入候选生成与人工审核"),
        "can_preview": metrics["raw_citation_rows"] > 0 or metrics["source_signal_count"] > 0 or metrics["article_count"] > 0,
        "can_save_shadow": not blockers and metrics["source_signal_count"] > 0,
        "can_activate": not blockers and not warnings,
        "impact_scope": {
            "online_recommendation": False,
            "production_writing_prompt": False,
            "billing": False,
            "customer_visible": False,
            "admin_shadow_tables": True,
        },
        "metrics": metrics,
    }
