"""缺口作战计划 · 持久化(P4)

表由 `db/migration_029_gap_operation_plan_2026_08_08.sql` 建(支撑迁移包,先行发车)。

🔴 本模块**不写** geo_article_* 任何一张表 —— 那是 services/article_delivery_plan.py
   的 closed-loop 机器(2026-08-08 生产实测四表全 0 行、ARTICLE_PLAN_* flag 全 False)。
   命名边界的完整理由见迁移文件头部。

🔴 UndefinedTable 的处置口径:**响亮**。迁移漏跑时不 fail-soft 返空 ——
   返空会让运营看到"这个客户没缺口",那是一句假结论。让它抛,由 API 层翻成
   `snapshot_unavailable` 错误合同(人话 + 重新获取按钮),只影响这个新区块。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg2.extras import Json

from db.connection import get_connection

logger = logging.getLogger("GEO-GapPlan-DB")

CHECKBACK_DAYS = (7, 14, 30)


def _jsonb(value: Any) -> Json:
    """psycopg2 的裸 Json() 用的是 json.dumps 默认参数,遇到 datetime 直接抛。

    🔴 这不是理论风险:快照 payload 里带 last_observed_at(TIMESTAMPTZ),
       2026-08-08 实测 `TypeError: Object of type datetime is not JSON serializable`。
       而且它只在**答案环境真有数据时**才触发 —— 环境为空的用例一路绿灯,
       是那条"依据抽屉不能为空"的锁把它逼出来的。
    """
    import json as _json

    return Json(value or {}, dumps=lambda v: _json.dumps(v, ensure_ascii=False, default=str))


def _rows(cur) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _row(cur) -> dict[str, Any] | None:
    r = cur.fetchone()
    return dict(r) if r else None


# ────────────────────────────────────────────────────────────────
# 快照
# ────────────────────────────────────────────────────────────────

def get_latest_snapshot(quote_id: int) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM gap_plan_snapshots
             WHERE quote_id = %s
             ORDER BY authority_generation DESC
             LIMIT 1
            """,
            (int(quote_id),),
        )
        return _row(cur)
    finally:
        conn.close()


def get_snapshot_by_data_version(quote_id: int, data_version: str) -> dict[str, Any] | None:
    """按数据版本找已有快照 —— 源事实没变就不产生新代际(A1 版本化的实质)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM gap_plan_snapshots
             WHERE quote_id = %s AND data_version = %s
             ORDER BY authority_generation DESC
             LIMIT 1
            """,
            (int(quote_id), str(data_version)),
        )
        return _row(cur)
    finally:
        conn.close()


def persist_snapshot(snapshot: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """落库一个新代际快照 + 它的计划项。

    🔴 代际分配在**同一个事务里**用 `SELECT ... FOR UPDATE` 之外的方式无法安全做,
       但 gap_plan_snapshots 上有 uq(quote_id, authority_generation) 唯一索引兜底:
       两个并发请求算出同一代际时,后到的那个 INSERT 会 UniqueViolation,
       调用侧退回读最新快照 —— 结果一致,不会出现两份同代际快照。
       选唯一索引而不是行锁:这条路径是**只读页面的加载**,不该为它锁住 quotes 行。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO gap_plan_snapshots (
                snapshot_id, quote_id, brand_id, owner_user_id,
                snapshot_version, rule_version, data_version, authority_generation,
                display_query, observed_at,
                capacity_authorized, capacity_reserved, capacity_available, capacity_source,
                summary_jsonb, payload_jsonb, generated_at
            ) VALUES (
                %(snapshot_id)s, %(quote_id)s, %(brand_id)s, %(owner_user_id)s,
                %(snapshot_version)s, %(rule_version)s, %(data_version)s, %(authority_generation)s,
                %(display_query)s, %(observed_at)s,
                %(capacity_authorized)s, %(capacity_reserved)s, %(capacity_available)s,
                %(capacity_source)s,
                %(summary_jsonb)s, %(payload_jsonb)s, NOW()
            )
            RETURNING *
            """,
            {
                **snapshot,
                "summary_jsonb": _jsonb(snapshot.get("summary_jsonb")),
                "payload_jsonb": _jsonb(snapshot.get("payload_jsonb")),
            },
        )
        stored = _row(cur)
        for item in items:
            cur.execute(
                """
                INSERT INTO gap_plan_items (
                    snapshot_id, plan_item_id, quote_id, ordinal,
                    target_question, content_form, target_platform, target_domain,
                    gap_code, access_code, allocation_code, duplicate_of_item_id,
                    rationale_jsonb, frozen_spec_jsonb
                ) VALUES (
                    %(snapshot_id)s, %(plan_item_id)s, %(quote_id)s, %(ordinal)s,
                    %(target_question)s, %(content_form)s, %(target_platform)s, %(target_domain)s,
                    %(gap_code)s, %(access_code)s, %(allocation_code)s, %(duplicate_of_item_id)s,
                    %(rationale_jsonb)s, %(frozen_spec_jsonb)s
                )
                """,
                {
                    **item,
                    "snapshot_id": stored["snapshot_id"],
                    "rationale_jsonb": _jsonb(item.get("rationale_jsonb")),
                    "frozen_spec_jsonb": _jsonb(item.get("frozen_spec_jsonb")),
                },
            )
        conn.commit()
        return stored
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_items(snapshot_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM gap_plan_items WHERE snapshot_id = %s ORDER BY ordinal",
            (str(snapshot_id),),
        )
        return _rows(cur)
    finally:
        conn.close()


def get_item(snapshot_id: str, plan_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM gap_plan_items WHERE snapshot_id = %s AND plan_item_id = %s",
            (str(snapshot_id), str(plan_item_id)),
        )
        return _row(cur)
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
# 发布物 + 回查队列
# ────────────────────────────────────────────────────────────────

#: [WP7 2026-08-17] gap 行的**降级标记**。规格 03 §10:
#:   「`gap_plan_publications` 只保留 plan/checkback metadata 与 canonical source
#:     引用;其 URL/current published/evidence bool **不再是独立真值**。」
#:
#: 🔴 为什么必须在数据上标,而不是写进文档:
#:    这张表的 `evidence_published` 是**只增不减**的布尔(CHECK 里还有单调约束),
#:    而 canonical 的"真发布有效"会因撤稿回落。谁把它当真值渲染,撤稿就永远不生效。
#:    标记跟着每一行走,下游想无视它必须显式删掉这个键 —— 那是一次可见的 diff。
GAP_ROW_AUTHORITY_NOTE: str = (
    "计划/回查元数据,非发布事实源;发布事实以 publication_stage_projection 的"
    "quote-scoped 六阶段投影为准")


def _demote(row: dict[str, Any]) -> dict[str, Any]:
    """给一行 gap 发布物盖上"非真值"戳。"""
    out = dict(row)
    out["is_canonical_source"] = False
    out["authority_note"] = GAP_ROW_AUTHORITY_NOTE
    return out


def get_publications(quote_id: int) -> dict[str, dict[str, Any]]:
    """quote 下所有计划项的发布物,按 plan_item_id 索引。

    [WP7] 每一行都带 `is_canonical_source=False`。要"这张报价真发布了几篇"
    请走 `services.publication_stage_adapters.quote_published_active`。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM gap_plan_publications WHERE quote_id = %s",
            (int(quote_id),),
        )
        return {r["plan_item_id"]: _demote(r) for r in _rows(cur)}
    finally:
        conn.close()


def canonical_stage_tuple(quote_id: int) -> dict[str, Any]:
    """gap plan 侧要展示"当前真发布"时的唯一合法取数口。

    存在的意义是**堵住回退路径**:没有这个函数,想显示发布数的人只会去读
    `evidence_published`(手边就有),于是降级标记形同虚设。
    """
    from services.publication_stage_adapters import quote_stage_tuple

    return quote_stage_tuple(int(quote_id))


def find_publication_by_idempotency_key(key: str) -> dict[str, Any] | None:
    if not key:
        return None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM gap_plan_publications WHERE idempotency_key = %s",
            (str(key),),
        )
        return _row(cur)
    finally:
        conn.close()


def register_publication(
    *,
    quote_id: int,
    plan_item_id: str,
    publication_url: str,
    publication_url_normalized: str,
    publication_domain: str,
    submitted_by: int | None,
    idempotency_key: str | None,
    published_at: datetime | None = None,
) -> dict[str, Any]:
    """登记发布链接 + 建 7/14/30 回查队列。**只点亮 evidence_published**。

    🔴 其余三格(收录/被引用/进入推荐)由 DB CHECK + 本函数的字面量共同保证不会被人工点亮:
       这里的 INSERT 根本不接受那三个参数 —— 想点亮它们只能改这段代码,
       而改了它 test_manual_link_cannot_light_beyond_published 会转红。
    """
    published_at = published_at or datetime.now(timezone.utc)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO gap_plan_publications (
                quote_id, plan_item_id, publication_url, publication_url_normalized,
                publication_domain, published_at, evidence_published,
                submitted_by, idempotency_key
            ) VALUES (%s,%s,%s,%s,%s,%s, TRUE, %s,%s)
            ON CONFLICT (quote_id, plan_item_id) DO UPDATE SET
                publication_url = EXCLUDED.publication_url,
                publication_url_normalized = EXCLUDED.publication_url_normalized,
                publication_domain = EXCLUDED.publication_domain,
                updated_at = NOW()
            RETURNING *
            """,
            (
                int(quote_id), str(plan_item_id), str(publication_url),
                str(publication_url_normalized), str(publication_domain),
                published_at, submitted_by, idempotency_key,
            ),
        )
        pub = _row(cur)
        for day in CHECKBACK_DAYS:
            cur.execute(
                """
                INSERT INTO gap_plan_checkbacks
                    (publication_id, quote_id, plan_item_id, due_day, due_at)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (publication_id, due_day) DO NOTHING
                """,
                (pub["id"], int(quote_id), str(plan_item_id), day,
                 published_at + timedelta(days=day)),
            )
        conn.commit()
        return pub
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_checkbacks(quote_id: int) -> dict[str, list[dict[str, Any]]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM gap_plan_checkbacks
             WHERE quote_id = %s
             ORDER BY plan_item_id, due_day
            """,
            (int(quote_id),),
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for row in _rows(cur):
            out.setdefault(row["plan_item_id"], []).append(row)
        return out
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
# 渠道可及性标记(A3 domain-access)
# ────────────────────────────────────────────────────────────────

def mark_domain_entry_assessment(
    *, domain: str, assessment: str, actor_user_id: int | None, note: str = ""
) -> int:
    """把「进不去」写回媒体目录,让后续所有报价都受益(而不是只改这一张卡)。

    返回受影响行数。0 行 = 媒体库里没有这个域名的条目,调用侧照常换方案。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE media_outlets
               SET entry_assessment = %s,
                   verified_at = NOW(),
                   verified_by = %s,
                   entry_note = COALESCE(NULLIF(%s,''), entry_note),
                   updated_at = CURRENT_TIMESTAMP
             WHERE LOWER(platform) = LOWER(%s) OR LOWER(name) = LOWER(%s)
            """,
            (str(assessment), actor_user_id, str(note or ""), str(domain), str(domain)),
        )
        affected = cur.rowcount
        conn.commit()
        return int(affected or 0)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_domain_entry_assessments(domains: list[str]) -> dict[str, dict[str, Any]]:
    """批量读可进入性核实结论。未核实的域名不出现在返回里(NULL ≠ 进不去)。"""
    wanted = [str(d).lower() for d in domains if d]
    if not wanted:
        return {}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT LOWER(COALESCE(platform, name)) AS key,
                   entry_assessment, entry_note, verified_at
              FROM media_outlets
             WHERE entry_assessment IS NOT NULL
               AND (LOWER(platform) = ANY(%s) OR LOWER(name) = ANY(%s))
            """,
            (wanted, wanted),
        )
        return {r["key"]: r for r in _rows(cur) if r.get("key")}
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
# 四引擎答案环境(A1 快照的事实底座)
# ────────────────────────────────────────────────────────────────

def fetch_answer_environment(
    industry_key: str, *, query_like: str = "", window_days: int = 90, limit: int = 200
) -> dict[str, Any]:
    """按行业(可选叠加问题模糊匹配)取四引擎答案环境。

    🔴 联表形态照 services/geo_douyin/ranking_source.py:143 的现役模式
       (`geo_research_answer_entities e LEFT JOIN geo_research_answer_facts f
         ON f.id = e.answer_fact_id`),不另造第二套取数路径。
       —— R3 说 geo_research_answer_facts「没有读侧 db 函数」这一条经核不准确:
       db/research_answer_entity_db.py 里读函数齐全(list_answer_entity_summary 等)。
       本函数存在的理由不是"没有别的",而是那些函数**按行业聚合**,
       而任务卡需要的是「这个问题下,四个引擎各自的候选位有多少、客户在不在里面」
       —— 那是按 (engine, fact) 的维度,现有聚合函数拿不到。

    返回:
        {
          "engines": {engine: {"answer_count": n, "candidate_count": n,
                               "last_observed_at": dt, "citation_domains": [...]}},
          "entity_keys": {entity_key: {"entity_name":..., "engines":[...]}},
          "queries": [...],
        }
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = [str(industry_key), str(window_days)]
        query_clause = ""
        if query_like:
            query_clause = " AND f.query ILIKE %s"
            params.append(f"%{query_like}%")
        params.append(int(limit))

        cur.execute(
            f"""
            SELECT e.engine,
                   e.entity_key,
                   e.entity_name,
                   e.recommendation_rank,
                   f.id            AS answer_fact_id,
                   f.query         AS query,
                   f.citation_urls AS citation_urls,
                   f.created_at    AS observed_at
              FROM geo_research_answer_entities e
              LEFT JOIN geo_research_answer_facts f ON f.id = e.answer_fact_id
             WHERE e.industry_key = %s
               AND e.entity_type = 'brand'
               AND e.created_at >= NOW() - (%s || ' days')::interval
               {query_clause}
             ORDER BY f.created_at DESC NULLS LAST, e.id
             LIMIT %s
            """,
            params,
        )
        rows = _rows(cur)
    finally:
        conn.close()

    engines: dict[str, dict[str, Any]] = {}
    entity_keys: dict[str, dict[str, Any]] = {}
    queries: set[str] = set()

    for row in rows:
        engine = (row.get("engine") or "").strip()
        if not engine:
            continue
        bucket = engines.setdefault(
            engine,
            {"answer_count": 0, "candidate_count": 0,
             "last_observed_at": None, "citation_domains": set(),
             "answer_fact_ids": set()},
        )
        fact_id = row.get("answer_fact_id")
        if fact_id is not None and fact_id not in bucket["answer_fact_ids"]:
            bucket["answer_fact_ids"].add(fact_id)
            bucket["answer_count"] += 1
        bucket["candidate_count"] += 1
        observed = row.get("observed_at")
        if observed and (bucket["last_observed_at"] is None
                         or observed > bucket["last_observed_at"]):
            bucket["last_observed_at"] = observed
        for url in (row.get("citation_urls") or []):
            domain = _domain_of(url)
            if domain:
                bucket["citation_domains"].add(domain)

        key = row.get("entity_key")
        if key:
            ent = entity_keys.setdefault(
                key, {"entity_name": row.get("entity_name") or "", "engines": set()}
            )
            ent["engines"].add(engine)
        if row.get("query"):
            queries.add(row["query"])

    for bucket in engines.values():
        bucket["citation_domains"] = sorted(bucket.pop("citation_domains"))
        bucket.pop("answer_fact_ids", None)
    for ent in entity_keys.values():
        ent["engines"] = sorted(ent["engines"])

    return {"engines": engines, "entity_keys": entity_keys, "queries": sorted(queries)}


def _domain_of(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    text = text.split("://", 1)[-1]
    host = text.split("/", 1)[0].split("?", 1)[0].strip().lower()
    return host[4:] if host.startswith("www.") else host


def list_core_keywords(quote_id: int) -> list[dict[str, Any]]:
    """报价下的关键词明细(主词优先)。

    🔴 required_articles 在这里**只作展示与排序**,不是容量口径 ——
       容量口径 SSOT = `services/article_capacity_contract`(P1),本包只消费不自算,
       出处随合同版本落库(见 services/gap_operation_plan.Capacity.source)。
       ⚠️ 本注释原先写的是「钉死在 quotes.total_articles」,那是并轨前的旧口径,
          已随容量并轨作废:`quotes.total_articles` 是脏数据不是容量
          (2026-08-08 实测 137 张有词的单里 52 张与词表对不上且全为 0)。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, keyword, monitoring_query, required_articles, is_core,
                   status, intent, funnel_stage, layer
              FROM confirmed_keywords
             WHERE quote_id = %s
             ORDER BY (is_core IS NOT TRUE), required_articles DESC NULLS LAST, id
            """,
            (int(quote_id),),
        )
        return _rows(cur)
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
# 小榜留痕(C3)
# ────────────────────────────────────────────────────────────────

def record_assistant_audit(**fields: Any) -> dict[str, Any] | None:
    """幂等留痕。重复 (assistant_request_id, snapshot_version) 只返回已有行。

    🔴 留痕失败**不得**阻断主链(合同 §9.1「不阻断主链」):本函数吞掉异常并记日志,
       返回 None。理由:留痕是观测面,让它挡住运营看任务卡是本末倒置。
       —— 但它吞的是**自己的**异常,调用方事务由 get_connection 独立连接隔离,
       不会像 2026-07 那次一样打废调用方事务。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO gap_assistant_audit (
                assistant_request_id, snapshot_id, snapshot_version, authority_generation,
                quote_id, brand_id, actor_user_id, facts_hash, action_ids, output_hash,
                operation_map_version, degraded, degrade_reason
            ) VALUES (
                %(assistant_request_id)s, %(snapshot_id)s, %(snapshot_version)s,
                %(authority_generation)s, %(quote_id)s, %(brand_id)s, %(actor_user_id)s,
                %(facts_hash)s, %(action_ids)s, %(output_hash)s,
                %(operation_map_version)s, %(degraded)s, %(degrade_reason)s
            )
            ON CONFLICT (assistant_request_id, COALESCE(snapshot_version, ''))
            DO UPDATE SET assistant_request_id = EXCLUDED.assistant_request_id
            RETURNING *
            """,
            {
                "assistant_request_id": fields.get("assistant_request_id"),
                "snapshot_id": fields.get("snapshot_id"),
                "snapshot_version": fields.get("snapshot_version"),
                "authority_generation": fields.get("authority_generation"),
                "quote_id": fields.get("quote_id"),
                "brand_id": fields.get("brand_id"),
                "actor_user_id": fields.get("actor_user_id"),
                "facts_hash": fields.get("facts_hash"),
                "action_ids": _jsonb(fields.get("action_ids") or []),
                "output_hash": fields.get("output_hash"),
                "operation_map_version": fields.get("operation_map_version"),
                "degraded": bool(fields.get("degraded", False)),
                "degrade_reason": fields.get("degrade_reason"),
            },
        )
        row = _row(cur)
        conn.commit()
        return row
    except Exception as exc:            # noqa: BLE001 — 见 docstring
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("小榜留痕失败(不阻断主链):%s", exc)
        return None
    finally:
        conn.close()
