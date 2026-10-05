"""[答案实体] DB helpers for GEO research answer-entity shadow layer.

与 B1-B6 的关系(勿混淆):
- `geo_research_source_signals`(供给侧 · 来源/URL 级):AI 从哪取材/去哪发文。
- 本模块 `geo_research_answer_facts` / `geo_research_answer_entities`(结果侧 · 品牌实体级):
  AI 最终推荐了谁 + 排第几 + 为什么 + 哪些引擎推。
同一 `answer_text` 的两个正交切面,互补共存,非重复造表(见 SPEC §1/§3 RFC)。

🔴 粒度(出口严审 P1-1 修正):`geo_research_raw` 是「query × engine × cited_url 一行一条 · 共享 answer_text」
(见 api/research_monitor_citations_api.py:6)。同一答案有 N 条 citation 行。故 fact 必须是 **answer 级**:
按 `GROUP BY <engine_norm>, COALESCE(batch_id,''), MD5(answer_text)` 折叠(与既有 citations_api 同款范式),
一条答案抽一次、算一次;`raw_ids` 聚合该答案覆盖的全部 citation 行。**不是** per-raw 粒度(会 N 倍抽取+N 倍计数)。

纪律:纯 shadow · admin-only 消费 · 不进客户页 · 不改报价/发布/扣费 · DDL 全 IF NOT EXISTS ·
不 DROP/TRUNCATE/RENAME · init_*() 兜底 · 去重键 = (engine, batch_id, answer_hash)。
"""

from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

from db.connection import get_db

EXTRACTOR_VERSION = "answer_entity_v2_2026-07-03"

# [复用·逐字一致] engine 归一 · 与 db/geo_source_signals_db._ENGINE_NORMALIZE_SQL、
# services/placement_service._ENGINE_NORMALIZE_SQL、api/research_monitor_citations_api._ENGINE_NORMALIZE_SQL
# 同口径。🔴 SPEC §4.3:不得引入第 4 种不同映射 —— 本 CASE 与既有 3 份逐字一致。
_ENGINE_NORMALIZE_SQL = """
    CASE
        WHEN LOWER(engine) IN ('doubao', '豆包') THEN '豆包'
        WHEN LOWER(engine) IN ('kimi') THEN 'Kimi'
        WHEN LOWER(engine) IN ('deepseek') THEN 'DeepSeek'
        WHEN LOWER(engine) IN ('qwen', '千问') THEN '千问'
        ELSE engine
    END
"""

_LOW_CONFIDENCE_THRESHOLD = 0.4

# [GEO-R9-CAN-007] 答案身份哈希 = MD5(query ⊕ industry ⊕ answer_text)。
# 修正:原先身份键仅 MD5(answer_text),同一 answer_text 若出自两个不同 query 或 industry(同 engine/batch),
# 会被 GROUP BY 折叠 + UNIQUE(engine,batch_id,answer_hash) 二次折叠成一条 fact,MIN(query)/MIN(industry)
# 只任取一个代表 → 丢失另一 query/行业身份。把 query+industry 纳入身份哈希后,不同 query/行业的同文本答案
# 各自成一条 fact;同上下文(同 query+industry+text)仍去重。UNIQUE 约束键不变(仍是 answer_hash)→ 零 schema 迁移。
# [对抗审核订正] 仅零 *schema* 迁移,非零 *数据* 迁移:存量 facts 是旧哈希(MD5(answer_text))写入,
#   部署后 only_pending 用新哈希算 NOT EXISTS 恒真 → 存量答案全量重抽 + 旧键孤儿。
# [Deploy-CTO NO-GO finding 8 返工 2026-07-12] 已交付数据迁移(recompute-in-place · 无损 · 幂等 · 可回滚):
#   scripts/migration_answer_hash_recompute_2026_07_12.sql —— 部署后【手动】执行(先 dry-run 再迁),
#   把存量 facts 的 answer_hash 就地重算为新哈希(不删 facts/entities · 不触发全量重抽 · 不产孤儿);
#   之前被旧 dedup 折叠掉的同 answer_text 多 query/industry 变体由 only_pending 增量重抽自然补齐。
# E'\x1f'(unit separator)作分隔符,避免拼接歧义(内容基本不含此控制字符)。
# 🔴 三处取数(count_extractable_answers / fetch_answer_groups_for_extraction / get_answer_entity_health)
# 必须共用本表达式,否则身份键不一致会导致 pending 判定错乱。
_ANSWER_IDENTITY_HASH_SQL = (
    "MD5(COALESCE(query, '') || E'\\x1f' || COALESCE(industry, '') || E'\\x1f' || COALESCE(answer_text, ''))"
)


def init_research_answer_entity_tables() -> None:
    """建 facts + entities 两表(幂等)。照 geo_source_signals_db 的 init 写法。"""
    with get_db() as conn:
        cur = conn.cursor()
        # 答案事实层:一行 = 一条被解析的 AI 答案(折叠 N 条 citation raw 行)。
        # 去重键 UNIQUE(engine, batch_id, answer_hash)· batch_id 用 COALESCE 存(NULL→'')避免 NULL≠NULL 逃去重。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_research_answer_facts (
                id BIGSERIAL PRIMARY KEY,
                raw_id INTEGER NOT NULL,
                industry TEXT NOT NULL DEFAULT '',
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                query TEXT NOT NULL DEFAULT '',
                engine VARCHAR(40) NOT NULL DEFAULT '',
                batch_id VARCHAR(120) NOT NULL DEFAULT '',
                answer_hash CHAR(32) NOT NULL DEFAULT '',
                raw_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                citation_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
                answer_excerpt TEXT,
                entity_count INTEGER NOT NULL DEFAULT 0,
                quality_flag VARCHAR(20) NOT NULL DEFAULT '',
                extractor_version VARCHAR(40) NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (engine, batch_id, answer_hash)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_facts_industry ON geo_research_answer_facts(industry_key, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_facts_engine ON geo_research_answer_facts(engine)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_facts_batch ON geo_research_answer_facts(batch_id)")

        # 实体层:一行 = 一个被推荐实体。industry_key/engine 从父 fact 反规范化(读侧 GROUP BY 免 join)。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_research_answer_entities (
                id BIGSERIAL PRIMARY KEY,
                answer_fact_id BIGINT NOT NULL,
                raw_id INTEGER NOT NULL DEFAULT 0,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                engine VARCHAR(40) NOT NULL DEFAULT '',
                entity_name TEXT NOT NULL,
                entity_key VARCHAR(80) NOT NULL,
                entity_type VARCHAR(20) NOT NULL DEFAULT 'brand',
                recommendation_rank INTEGER,
                mention_rank INTEGER,
                recommendation_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
                evidence_phrases JSONB NOT NULL DEFAULT '[]'::jsonb,
                source_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
                confidence NUMERIC(5,3) NOT NULL DEFAULT 0,
                llm_model VARCHAR(80) NOT NULL DEFAULT '',
                extractor_version VARCHAR(40) NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (answer_fact_id, entity_key)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_entities_industry ON geo_research_answer_entities(industry_key, entity_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_entities_key ON geo_research_answer_entities(entity_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_entities_fact ON geo_research_answer_entities(answer_fact_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_answer_entities_engine ON geo_research_answer_entities(engine)")


def upsert_answer_fact_with_entities(fact: dict[str, Any], entities: list[dict[str, Any]]) -> dict[str, Any]:
    """幂等写一条答案事实(answer 级)+ 其实体。

    - facts 层 upsert by (engine, batch_id, answer_hash)(UNIQUE)。
    - entities 层:先删该 fact 的旧实体,再插新的(保证重抽后行数 = 本次抽取实体数,无残留)。
    返回 {fact_id, entity_written}。
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO geo_research_answer_facts (
                raw_id, industry, industry_key, query, engine, batch_id, answer_hash,
                raw_ids, citation_urls, answer_excerpt, entity_count, quality_flag,
                extractor_version, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (engine, batch_id, answer_hash) DO UPDATE SET
                raw_id = EXCLUDED.raw_id,
                industry = EXCLUDED.industry,
                industry_key = EXCLUDED.industry_key,
                query = EXCLUDED.query,
                raw_ids = EXCLUDED.raw_ids,
                citation_urls = EXCLUDED.citation_urls,
                answer_excerpt = EXCLUDED.answer_excerpt,
                entity_count = EXCLUDED.entity_count,
                quality_flag = EXCLUDED.quality_flag,
                extractor_version = EXCLUDED.extractor_version,
                updated_at = NOW()
            RETURNING id
        """, (
            int(fact.get("raw_id") or 0),
            fact.get("industry") or "",
            fact.get("industry_key") or "general",
            fact.get("query") or "",
            fact.get("engine") or "",
            fact.get("batch_id") or "",
            fact.get("answer_hash") or "",
            Json(fact.get("raw_ids") or []),
            Json(fact.get("citation_urls") or []),
            (fact.get("answer_excerpt") or "")[:600],
            len(entities),
            fact.get("quality_flag") or "",
            fact.get("extractor_version") or EXTRACTOR_VERSION,
        ))
        fact_id = int(cur.fetchone()["id"])

        cur.execute("DELETE FROM geo_research_answer_entities WHERE answer_fact_id = %s", (fact_id,))
        written = 0
        for ent in entities:
            ekey = (ent.get("entity_key") or "").strip()
            ename = (ent.get("entity_name") or "").strip()
            if not ekey or not ename:
                continue
            cur.execute("""
                INSERT INTO geo_research_answer_entities (
                    answer_fact_id, raw_id, industry_key, engine, entity_name, entity_key,
                    entity_type, recommendation_rank, mention_rank, recommendation_reasons,
                    evidence_phrases, source_urls, confidence, llm_model, extractor_version, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (answer_fact_id, entity_key) DO UPDATE SET
                    entity_name = EXCLUDED.entity_name,
                    entity_type = EXCLUDED.entity_type,
                    recommendation_rank = EXCLUDED.recommendation_rank,
                    mention_rank = EXCLUDED.mention_rank,
                    recommendation_reasons = EXCLUDED.recommendation_reasons,
                    evidence_phrases = EXCLUDED.evidence_phrases,
                    source_urls = EXCLUDED.source_urls,
                    confidence = EXCLUDED.confidence,
                    llm_model = EXCLUDED.llm_model,
                    extractor_version = EXCLUDED.extractor_version,
                    updated_at = NOW()
            """, (
                fact_id,
                int(fact.get("raw_id") or 0),
                fact.get("industry_key") or "general",
                fact.get("engine") or "",
                ename,
                ekey,
                ent.get("entity_type") or "brand",
                ent.get("recommendation_rank"),
                ent.get("mention_rank"),
                Json(ent.get("recommendation_reasons") or []),
                Json(ent.get("evidence_phrases") or []),
                Json(ent.get("source_urls") or []),
                float(ent.get("confidence") or 0),
                (ent.get("llm_model") or "")[:80],
                ent.get("extractor_version") or EXTRACTOR_VERSION,
            ))
            written += 1
        return {"fact_id": fact_id, "entity_written": written}


def _since_clause(since_days: int | None, col: str = "created_at") -> str:
    if since_days and since_days > 0:
        return f" AND {col} >= NOW() - INTERVAL '{int(since_days)} days'"
    return ""


def list_answer_entity_summary(industry_key: str = "", engine: str = "",
                               limit: int = 20, since_days: int | None = None) -> list[dict[str, Any]]:
    """行业 Top 推荐实体聚合(读侧 · GROUP BY entity_key)。

    mention_count = 该实体出现的**答案数**(因 fact 已是 answer 级,一实体一答案一行)。
    """
    where = ["1=1"]
    params: list[Any] = []
    if industry_key:
        where.append("industry_key = %s")
        params.append(industry_key)
    if engine:
        where.append("engine = %s")
        params.append(engine)
    where_sql = " AND ".join(where) + _since_clause(since_days)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT entity_key,
                   MAX(entity_name) AS entity_name,
                   MAX(entity_type) AS entity_type,
                   COUNT(*) AS mention_count,
                   COUNT(DISTINCT engine) AS engine_count,
                   ARRAY_AGG(DISTINCT engine) AS engines,
                   AVG(recommendation_rank) FILTER (WHERE recommendation_rank IS NOT NULL) AS avg_recommendation_rank,
                   AVG(confidence) AS avg_confidence
            FROM geo_research_answer_entities
            WHERE {where_sql}
            GROUP BY entity_key
            ORDER BY mention_count DESC, engine_count DESC
            LIMIT %s
        """, (*params, int(limit)))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["avg_recommendation_rank"] = round(float(d["avg_recommendation_rank"]), 2) if d["avg_recommendation_rank"] is not None else None
            d["avg_confidence"] = round(float(d["avg_confidence"] or 0), 3)
            d["engines"] = [e for e in (d.get("engines") or []) if e]
            rows.append(d)
        return rows


def list_answer_entity_examples(industry_key: str = "", engine: str = "",
                                limit: int = 20, since_days: int | None = None) -> list[dict[str, Any]]:
    """实体样例(只返短 evidence phrase + 聚合字段 · 🔴 绝不返完整 answer_text)。"""
    where = ["1=1"]
    params: list[Any] = []
    if industry_key:
        where.append("e.industry_key = %s")
        params.append(industry_key)
    if engine:
        where.append("e.engine = %s")
        params.append(engine)
    where_sql = " AND ".join(where) + _since_clause(since_days, "e.created_at")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT e.entity_name, e.entity_key, e.entity_type, e.engine,
                   e.recommendation_rank, e.mention_rank, e.recommendation_reasons,
                   e.evidence_phrases, e.source_urls, e.confidence
            FROM geo_research_answer_entities e
            WHERE {where_sql}
            ORDER BY e.confidence DESC, e.recommendation_rank ASC NULLS LAST
            LIMIT %s
        """, (*params, int(limit)))
        return [dict(r) for r in cur.fetchall()]


def list_entity_mentions(entity_key: str, industry_key: str = "",
                         limit: int = 20, since_days: int | None = None) -> list[dict[str, Any]]:
    """[E1 溯源] 按 entity_key 精确取该实体的原始 mention 行(供「AI 原话在哪轮哪引擎」溯源)。

    join 父表 geo_research_answer_facts 拿 query/batch_id/answer_excerpt(短摘要 · 🔴 绝不返完整 answer_text)。
    ORDER BY confidence DESC, recommendation_rank ASC NULLS LAST。仿 list_answer_entity_examples。
    """
    ekey = (entity_key or "").strip()
    if not ekey:
        return []
    where = ["e.entity_key = %s"]
    params: list[Any] = [ekey]
    if industry_key:
        where.append("e.industry_key = %s")
        params.append(industry_key)
    where_sql = " AND ".join(where) + _since_clause(since_days, "e.created_at")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT e.entity_name, e.entity_key, e.entity_type, e.engine,
                   e.recommendation_rank, e.mention_rank, e.recommendation_reasons,
                   e.evidence_phrases, e.source_urls, e.confidence, e.created_at,
                   f.query AS query, f.batch_id AS batch_id, f.answer_excerpt AS answer_excerpt
            FROM geo_research_answer_entities e
            LEFT JOIN geo_research_answer_facts f ON f.id = e.answer_fact_id
            WHERE {where_sql}
            ORDER BY e.confidence DESC, e.recommendation_rank ASC NULLS LAST
            LIMIT %s
        """, (*params, int(limit)))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            if d.get("created_at") is not None and hasattr(d["created_at"], "isoformat"):
                d["created_at"] = d["created_at"].isoformat()
            if d.get("confidence") is not None:
                d["confidence"] = round(float(d["confidence"]), 3)
            rows.append(d)
        return rows


def _industry_where(alias: str, industry_values: list[str] | None, params: list[Any]) -> str:
    if industry_values:
        params.append(industry_values)
        return f" AND {alias}industry = ANY(%s)"
    return ""


def get_answer_entity_health() -> dict[str, Any]:
    """只读健康度(不接管生产推荐,见 SPEC §8)。覆盖率按 **answer 级** 计。"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE answer_text IS NOT NULL AND answer_text <> '') AS with_answer
            FROM geo_research_raw
        """)
        raw = dict(cur.fetchone())
        total_raw = int(raw["total"] or 0)
        with_answer = int(raw["with_answer"] or 0)

        # answer 级:distinct (engine_norm, batch, answer_md5) 组数
        cur.execute(f"""
            SELECT COUNT(*) AS c FROM (
                SELECT 1 FROM geo_research_raw
                WHERE answer_text IS NOT NULL AND answer_text <> ''
                GROUP BY {_ENGINE_NORMALIZE_SQL}, COALESCE(batch_id, ''), {_ANSWER_IDENTITY_HASH_SQL}  -- [GEO-R9-CAN-007] 身份键含 query+industry
            ) g
        """)
        total_answers = int((cur.fetchone() or {}).get("c") or 0)

        cur.execute("SELECT COUNT(*) AS c FROM geo_research_answer_facts")
        extracted = int((cur.fetchone() or {}).get("c") or 0)

        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE confidence < %s) AS low_conf,
                   COUNT(*) FILTER (WHERE source_urls <> '[]'::jsonb) AS with_source
            FROM geo_research_answer_entities
        """, (_LOW_CONFIDENCE_THRESHOLD,))
        ent = dict(cur.fetchone())
        total_ent = int(ent["total"] or 0)

        cur.execute(f"""
            SELECT {_ENGINE_NORMALIZE_SQL} AS canonical_engine, COUNT(*) AS c
            FROM geo_research_answer_entities
            GROUP BY canonical_engine
            ORDER BY c DESC
        """)
        engine_samples = {r["canonical_engine"]: int(r["c"]) for r in cur.fetchall()}

        cur.execute("""
            SELECT MAX(updated_at) AS last_updated,
                   COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days') AS last_7d,
                   COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS last_30d
            FROM geo_research_answer_facts
        """)
        fresh = dict(cur.fetchone())

        return {
            "raw_total": total_raw,
            "raw_with_answer": with_answer,
            "answer_text_coverage": round(with_answer / total_raw, 4) if total_raw else 0.0,
            "total_answers": total_answers,
            "raw_extracted": extracted,
            "extraction_coverage": round(extracted / total_answers, 4) if total_answers else 0.0,
            "entity_total": total_ent,
            "low_confidence_ratio": round(int(ent["low_conf"] or 0) / total_ent, 4) if total_ent else 0.0,
            "entity_source_linkable_ratio": round(int(ent["with_source"] or 0) / total_ent, 4) if total_ent else 0.0,
            "engine_normalized_samples": engine_samples,
            "last_updated": fresh["last_updated"].isoformat() if fresh.get("last_updated") else None,
            "facts_last_7d": int(fresh["last_7d"] or 0),
            "facts_last_30d": int(fresh["last_30d"] or 0),
        }


def count_extractable_answers(industry_values: list[str] | None = None, limit: int = 200) -> dict[str, Any]:
    """dry_run 计数(**answer 级**):有 answer_text 的答案组数 + 未抽(无对应 fact)组数。不调 LLM。"""
    params: list[Any] = []
    ind_where = _industry_where("", industry_values, params)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            WITH groups AS (
                SELECT {_ENGINE_NORMALIZE_SQL} AS engine_norm,
                       COALESCE(batch_id, '') AS batch_key,
                       {_ANSWER_IDENTITY_HASH_SQL} AS answer_md5  -- [GEO-R9-CAN-007] 身份键含 query+industry
                FROM geo_research_raw
                WHERE answer_text IS NOT NULL AND answer_text <> ''{ind_where}
                GROUP BY engine_norm, batch_key, answer_md5
            )
            SELECT COUNT(*) AS total_answers,
                   COUNT(*) FILTER (
                       WHERE NOT EXISTS (
                           SELECT 1 FROM geo_research_answer_facts f
                           WHERE f.engine = groups.engine_norm
                             AND f.batch_id = groups.batch_key
                             AND f.answer_hash = groups.answer_md5
                       )
                   ) AS pending_extract
            FROM groups
        """, params)
        row = dict(cur.fetchone())
        return {
            "total_answers": int(row["total_answers"] or 0),
            "pending_extract": int(row["pending_extract"] or 0),
            "batch_limit": int(limit),
        }


def fetch_answer_groups_for_extraction(industry_values: list[str] | None = None,
                                       limit: int = 200, only_pending: bool = True) -> list[dict[str, Any]]:
    """取待抽的**答案组**(折叠 citation 行 · 与 citations_api 同款 answer_md5 分组)。

    only_pending=True 只取尚无对应 fact 的答案组(去重键 engine/batch/answer_hash)。
    每组返回:anchor_raw_id(MIN id)· raw_ids(全部 citation 行)· 代表 answer_text · citation_urls · answer_md5。
    """
    params: list[Any] = []
    ind_where = _industry_where("", industry_values, params)
    pending_sql = ""
    if only_pending:
        pending_sql = """
             WHERE NOT EXISTS (
                 SELECT 1 FROM geo_research_answer_facts f
                 WHERE f.engine = base.engine_norm
                   AND f.batch_id = base.batch_key
                   AND f.answer_hash = base.answer_md5
             )
        """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            WITH base AS (
                SELECT id, industry, query, cite_url, answer_text, created_at,
                       {_ENGINE_NORMALIZE_SQL} AS engine_norm,
                       COALESCE(batch_id, '') AS batch_key,
                       {_ANSWER_IDENTITY_HASH_SQL} AS answer_md5  -- [GEO-R9-CAN-007] 身份键含 query+industry
                FROM geo_research_raw
                WHERE answer_text IS NOT NULL AND answer_text <> ''{ind_where}
            )
            SELECT engine_norm AS engine, batch_key AS batch_id, answer_md5,
                   MIN(id) AS anchor_raw_id,
                   ARRAY_AGG(id ORDER BY id) AS raw_ids,
                   MIN(industry) AS industry,
                   MIN(query) AS query,
                   MIN(answer_text) AS answer_text,
                   ARRAY_REMOVE(ARRAY_AGG(DISTINCT cite_url), NULL) AS citation_urls,
                   MAX(created_at) AS created_at
            FROM base
            {pending_sql}
            GROUP BY engine_norm, batch_key, answer_md5
            ORDER BY MAX(created_at) DESC NULLS LAST
            LIMIT %s
        """, (*params, int(limit)))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["raw_ids"] = [int(x) for x in (d.get("raw_ids") or []) if x is not None]
            d["citation_urls"] = [u for u in (d.get("citation_urls") or []) if u]
            rows.append(d)
        return rows
