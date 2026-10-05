"""P0-1: round-scoped incremental flywheel bridge worker.

After a research round is marked `status='completed'`, this worker refreshes the
downstream flywheel SHADOW / CANDIDATE layers for that round, so the pipeline no
longer depends on a manual rebuild button that nobody clicks:

    geo_research_raw
      → geo_research_source_signals        (stage 1, paginated per raw row)
      → geo_answer_adoption_metrics         (stage 2, paginated per source signal)
      → geo_media_entities                  (stage 3, per touched industry)
      → geo_media_binding_candidates        (stage 4, per touched industry)
      → geo_research_answer_facts/_entities (stage 5, LLM, flag + cost-capped)
      → writing_strategy_versions (shadow)  (stage 6, per touched industry)

Hard guarantees (see 修复指令 §四 P0-1 + 补充指令 §1/§3/§4):
- Round/batch scoped + PAGINATED: a single round with >10000 raw rows is not
  truncated (existing rebuild endpoints cap at limit<=10000 — never reused here).
- Idempotent: every write is an ON CONFLICT upsert, so re-running the same round
  updates in place (no duplicate rows).
- Retryable / fail-soft: a failing page or stage is recorded and skipped; later
  pages/stages still run.  Each upsert opens its own transaction, so one bad row
  cannot poison the rest.
- Shadow/candidate ONLY: this never approves bindings, never writes inventory
  mappings, never activates strategies, never takes over customer output.  The
  human review gate is preserved.
- answer_entities (the only LLM cost) is gated by a SEPARATE flag
  (`flywheel_answer_entity_auto`, default OFF) + per-round / per-day caps, so
  enabling the bridge does NOT by itself incur LLM cost.

The bridge itself is gated by `flywheel_round_bridge` (default OFF) at the call
site, so production behavior is byte-identical until Deploy-CTO flips it on.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections import Counter
from typing import Any, Callable

from db.connection import get_connection
from db.flywheel_bridge_db import finish_bridge_run, start_bridge_run, sweep_stale_orphan_runs
from db.geo_source_signals_db import init_geo_source_signal_tables, upsert_source_signal
from db.answer_adoption_metrics_db import (
    init_answer_adoption_metric_tables,
    upsert_answer_adoption_metrics,
)
from db.media_entity_flywheel_db import (
    init_media_entity_flywheel_tables,
    insert_media_binding_audit_event,
    list_media_inventory_candidates,
    list_shadow_media_entities,
    upsert_media_binding_candidate,
    upsert_media_entity,
    upsert_score_snapshot,
)
from db.writing_style_flywheel_db import (
    init_writing_style_flywheel_tables,
    load_strategy_generation_inputs,
    upsert_strategy_version,
)
from services.media_binding_candidates import build_binding_candidates
from services.media_entity_flywheel import (
    build_media_entity_seed,
    compute_media_entity_shadow_score,
    normalize_domain,
    normalize_industry_key,
)
from services.research_monitor.answer_adoption_metrics import build_metric_payload
from services.research_monitor.answer_entity_extractor import rebuild_answer_entities
from services.research_monitor.source_signal_classifier import classify_source_signal
from services.research_monitor.source_signal_weighting import weighted_signal_value
from services.writing_strategy_service import build_strategy_candidate
from writing.feature_switches import is_feature_enabled

logger = logging.getLogger("GEO-FlywheelBridge")

# Prevent two bridges for the same round running concurrently (retry safety).
_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = asyncio.Lock()

_DEFAULT_PAGE_SIZE = 1000
_ENTITY_ROLLUP_LIMIT = 500
_ENTITY_LIST_LIMIT = 300
_INVENTORY_LIMIT = 40
_BINDING_PER_ENTITY = 8
_STRATEGY_INPUT_LIMIT = 200


def _sha1(value: str) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def _round_batch_id(round_id: str) -> str:
    # round_runner writes geo_research_raw.batch_id as f"batch_{round_id}".
    return f"batch_{round_id}"


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(x for x in items if x))


# ---------------------------------------------------------------------------
# scoped read helpers (each opens/closes its own connection)
# ---------------------------------------------------------------------------
def _round_industries(batch_id: str) -> list[str]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT industry
              FROM geo_research_raw
             WHERE batch_id = %s AND COALESCE(industry, '') <> ''
            """,
            (batch_id,),
        )
        return [r["industry"] for r in cur.fetchall()]
    finally:
        conn.close()


def _batch_source_counts(batch_id: str) -> dict[tuple[str, str], int]:
    """Sources-per-answer for the whole batch (avoids window undercount across pages)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT engine, query, COUNT(*) AS c
              FROM geo_research_raw
             WHERE batch_id = %s AND COALESCE(cite_url, '') <> ''
             GROUP BY engine, query
            """,
            (batch_id,),
        )
        return {((r["engine"] or ""), (r["query"] or "")): int(r["c"]) for r in cur.fetchall()}
    finally:
        conn.close()


def _load_round_raw_page(batch_id: str, after_id: int, page_size: int) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT raw.id, raw.industry, raw.query, raw.engine,
                   raw.cited_platform, raw.cite_position,
                   raw.cite_position AS search_rank,
                   raw.cite_url, raw.cite_title, raw.cite_excerpt,
                   raw.answer_text, raw.is_answer_cited, raw.adoption_rank,
                   raw.batch_id, raw.created_at, raw.provider, raw.model,
                   raw.model_revision, raw.surface, raw.search_mode,
                   raw.prompt_snapshot, arc.article_id,
                   art.review_status, art.clean_status, art.cleaned_char_count,
                   art.canonical_body_hash, af.id AS article_fetch_id,
                   af.body_hash AS fetch_body_hash
              FROM geo_research_raw raw
              LEFT JOIN LATERAL (
                    SELECT citation.article_id
                      FROM geo_research_article_citations citation
                     WHERE citation.raw_id = raw.id
                     ORDER BY citation.id DESC
                     LIMIT 1
              ) arc ON TRUE
              LEFT JOIN geo_research_articles art ON art.id = arc.article_id
              LEFT JOIN LATERAL (
                    SELECT f.id, f.body_hash
                      FROM geo_research_article_fetches f
                     WHERE f.article_id=art.id AND f.response_status='success'
                     ORDER BY f.fetched_at DESC, f.id DESC
                     LIMIT 1
              -- 别名不能叫 fetch:FETCH 是 PostgreSQL 保留字(pg_get_keywords catcode='R'),
              -- 未加引号做表别名会在解析期整条 SQL 报 syntax error。见 tests/
              -- flywheel_integration/test_sql_reserved_word_alias.py。
              ) af ON TRUE
             WHERE raw.batch_id = %s
               AND COALESCE(raw.cite_url, '') <> ''
               AND raw.id > %s
             ORDER BY raw.id
             LIMIT %s
            """,
            (batch_id, after_id, page_size),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _load_round_signal_page(batch_id: str, after_id: int, page_size: int) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, source_url, domain, industry_key, engine,
                   COALESCE(prompt_id, '') AS prompt_id,
                   COALESCE(round_id, '') AS round_id,
                   signal_tier, source_position, total_sources_in_answer,
                   balanced_weight, observed_at
              FROM geo_research_source_signals
             WHERE round_id = %s AND id > %s
             ORDER BY id
             LIMIT %s
            """,
            (batch_id, after_id, page_size),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _scalar_count(sql: str, params: tuple) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(list(row.values())[0]) if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------
def _stage_source_signals(batch_id: str, page_size: int, dry_run: bool) -> dict[str, Any]:
    if not dry_run:
        init_geo_source_signal_tables()
    source_counts = _batch_source_counts(batch_id)
    before = 0 if dry_run else _scalar_count(
        "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id = %s", (batch_id,)
    )
    loaded = written = skipped = failed = pages = would_write = 0
    by_engine: Counter[str] = Counter()
    by_tier: Counter[str] = Counter()
    last_id = 0
    last_processed_raw_id: int | None = None

    while True:
        rows = _load_round_raw_page(batch_id, last_id, page_size)
        if not rows:
            break
        pages += 1
        for row in rows:
            last_id = int(row["id"])
            last_processed_raw_id = last_id
            loaded += 1
            try:
                key = ((row.get("engine") or ""), (row.get("query") or ""))
                row["total_sources_in_answer"] = source_counts.get(key, 1)
                signal = classify_source_signal(row)
                if not signal.source_url:
                    skipped += 1
                    continue
                by_tier[signal.signal_tier] += 1
                by_engine[signal.engine or "unknown"] += 1
                would_write += 1  # [Finding #5] dry_run 也计「会写多少」,预览不再误报 0
                if dry_run:
                    continue
                exact_body_lineage = bool(
                    row.get("article_id")
                    and row.get("canonical_body_hash")
                    and row.get("article_fetch_id")
                    and row.get("fetch_body_hash") == row.get("canonical_body_hash")
                    and str(row.get("provider") or "") not in {"", "unknown", "legacy_unknown"}
                    and str(row.get("model") or "") not in {"", "unknown", "legacy_unknown"}
                    and str(row.get("surface") or "") not in {"", "unknown", "legacy_unknown"}
                    and str(row.get("prompt_snapshot") or "").strip()
                )
                direct_adoption = signal.signal_tier == "answer_adopted" and exact_body_lineage
                lineage_problems = []
                if not row.get("article_id"):
                    lineage_problems.append("article_missing")
                if not row.get("canonical_body_hash"):
                    lineage_problems.append("canonical_body_hash_missing")
                if not row.get("article_fetch_id") or row.get("fetch_body_hash") != row.get("canonical_body_hash"):
                    lineage_problems.append("fetch_body_hash_mismatch")
                if str(row.get("provider") or "") in {"", "unknown", "legacy_unknown"}:
                    lineage_problems.append("provider_unknown")
                if str(row.get("model") or "") in {"", "unknown", "legacy_unknown"}:
                    lineage_problems.append("model_unknown")
                if str(row.get("surface") or "") in {"", "unknown", "legacy_unknown"}:
                    lineage_problems.append("surface_unknown")
                if not str(row.get("prompt_snapshot") or "").strip():
                    lineage_problems.append("prompt_snapshot_missing")
                upsert_source_signal({
                    "source_url": signal.source_url,
                    "url_hash": _sha1(signal.source_url),
                    "domain": normalize_domain(signal.source_url),
                    "industry_key": signal.industry_key,
                    "engine": signal.engine,
                    "prompt_id": signal.prompt_id,
                    "signal_tier": signal.signal_tier,
                    "source_position": signal.source_position,
                    "total_sources_in_answer": signal.total_sources_in_answer,
                    "balanced_weight": weighted_signal_value(signal),
                    "answer_mentioned_brand": signal.answer_mentioned_brand,
                    "round_id": row.get("batch_id") or "",
                    "article_id": row.get("article_id"),
                    "label_provenance_type": (
                        "direct_observation" if direct_adoption else "search_discovery"
                    ),
                    "label_provenance_version": "research-source-lineage-v1.0",
                    "provider": row.get("provider") or "legacy_unknown",
                    "model": row.get("model") or "legacy_unknown",
                    "model_revision": row.get("model_revision") or "unknown",
                    "surface": row.get("surface") or "legacy_unknown",
                    "search_mode": row.get("search_mode") or "legacy_unknown",
                    "prompt_snapshot": row.get("prompt_snapshot") or row.get("query"),
                    "article_snapshot_hash": row.get("canonical_body_hash"),
                    "article_fetch_id": row.get("article_fetch_id"),
                    "lineage_status": "complete" if exact_body_lineage else "explicit_unknown",
                    "lineage_error_reason": ",".join(lineage_problems),
                    "metadata": signal.metadata or {},
                })
                written += 1
            except Exception as exc:  # per-row fail-soft (own txn, no cascade)
                failed += 1
                logger.debug(f"[FlywheelBridge] source_signal row {row.get('id')} failed: {exc}")
        if len(rows) < page_size:
            break

    after = 0 if dry_run else _scalar_count(
        "SELECT COUNT(*) AS c FROM geo_research_source_signals WHERE round_id = %s", (batch_id,)
    )
    inserted = max(0, after - before)
    updated = max(0, written - inserted)
    return {
        "loaded": loaded, "written": written, "would_write": would_write,
        "inserted": inserted, "updated": updated,
        "skipped": skipped, "failed": failed, "pages": pages,
        "by_engine": dict(by_engine), "by_tier": dict(by_tier),
        "last_processed_raw_id": last_processed_raw_id,
    }


def _stage_answer_adoption(batch_id: str, page_size: int, dry_run: bool) -> dict[str, Any]:
    if not dry_run:
        init_answer_adoption_metric_tables()
    before = 0 if dry_run else _scalar_count(
        "SELECT COUNT(*) AS c FROM geo_answer_adoption_metrics WHERE round_id = %s", (batch_id,)
    )
    loaded = written = skipped = failed = pages = 0
    by_engine: Counter[str] = Counter()
    by_tier: Counter[str] = Counter()
    last_id = 0

    while True:
        rows = _load_round_signal_page(batch_id, last_id, page_size)
        if not rows:
            break
        pages += 1
        metrics: list[dict[str, Any]] = []
        for row in rows:
            last_id = int(row["id"])
            loaded += 1
            if not row.get("source_url"):
                skipped += 1
                continue
            try:
                payload = build_metric_payload(row)
                by_tier[payload["signal_tier"]] += 1
                by_engine[payload.get("engine") or "unknown"] += 1
                metrics.append(payload)
            except Exception as exc:
                failed += 1
                logger.debug(f"[FlywheelBridge] adoption metric row {row.get('id')} failed: {exc}")
        if metrics and not dry_run:
            try:
                written += upsert_answer_adoption_metrics(metrics)
            except Exception as exc:
                failed += len(metrics)
                logger.warning(f"[FlywheelBridge] adoption metric page upsert failed: {exc}")
        elif dry_run:
            written += len(metrics)
        if len(rows) < page_size:
            break

    after = 0 if dry_run else _scalar_count(
        "SELECT COUNT(*) AS c FROM geo_answer_adoption_metrics WHERE round_id = %s", (batch_id,)
    )
    inserted = max(0, after - before)
    updated = max(0, written - inserted) if not dry_run else 0
    return {
        "loaded": loaded, "written": written, "inserted": inserted, "updated": updated,
        "skipped": skipped, "failed": failed, "pages": pages,
        "by_engine": dict(by_engine), "by_tier": dict(by_tier),
    }


def _stage_media_entities(industries: list[str], dry_run: bool) -> dict[str, Any]:
    from db.geo_source_signals_db import list_source_signal_rollup
    if not dry_run:
        init_geo_source_signal_tables()
        init_media_entity_flywheel_tables()
    before = 0 if dry_run else _scalar_count("SELECT COUNT(*) AS c FROM geo_media_entities", ())
    loaded = written = skipped = failed = 0
    scope = industries or [""]  # [""] → industry-agnostic rollup (all)
    for industry in scope:
        try:
            rollups = list_source_signal_rollup(industry_key=industry, limit=_ENTITY_ROLLUP_LIMIT)
        except Exception as exc:
            failed += 1
            logger.warning(f"[FlywheelBridge] entity rollup for {industry!r} failed: {exc}")
            continue
        for row in rollups:
            loaded += 1
            domain = row.get("domain") or ""
            if not domain:
                skipped += 1
                continue
            try:
                entity = build_media_entity_seed({
                    "name": domain,
                    "domain": domain,
                    "industry_key": row.get("industry_key") or industry or "general",
                    "confidence": 0.72,
                    "tags": {"source": "geo_research_source_signals", "via": "flywheel_bridge"},
                })
                score = compute_media_entity_shadow_score(
                    entity=entity, citation_rollup=row, inventory_matches=[], outcome_rollup={},
                )
                if dry_run:
                    continue
                entity_row = upsert_media_entity(entity)
                upsert_score_snapshot(int(entity_row["id"]), score)
                written += 1
            except Exception as exc:
                failed += 1
                logger.debug(f"[FlywheelBridge] media entity {domain} failed: {exc}")
    after = 0 if dry_run else _scalar_count("SELECT COUNT(*) AS c FROM geo_media_entities", ())
    inserted = max(0, after - before)
    updated = max(0, written - inserted)
    return {
        "loaded": loaded, "written": written, "inserted": inserted, "updated": updated,
        "skipped": skipped, "failed": failed, "industries": len([s for s in scope if s]),
    }


def _entity_from_shadow_row(row: dict[str, Any]) -> dict[str, Any]:
    aliases = row.get("aliases")
    if not isinstance(aliases, list):
        aliases = []
    return {
        "entity_key": row.get("entity_key"),
        "canonical_name": row.get("canonical_name"),
        "domain": row.get("domain") or "",
        "aliases": aliases,
        "industry_key": row.get("industry_key") or "general",
        "confidence": row.get("confidence") or 0.7,
    }


def _stage_binding_candidates(industries: list[str], dry_run: bool) -> dict[str, Any]:
    if not dry_run:
        init_media_entity_flywheel_tables()
    before = 0 if dry_run else _scalar_count(
        "SELECT COUNT(*) AS c FROM geo_media_binding_candidates WHERE status <> 'deleted'", ()
    )
    loaded = written = skipped = failed = revived = 0
    scope = industries or [""]
    for industry in scope:
        try:
            entities = list_shadow_media_entities(
                industry_key=industry, only_purchasable=False, limit=_ENTITY_LIST_LIMIT,
            )
        except Exception as exc:
            failed += 1
            logger.warning(f"[FlywheelBridge] shadow entities for {industry!r} failed: {exc}")
            continue
        for erow in entities:
            loaded += 1
            entity = _entity_from_shadow_row(erow)
            try:
                names = [entity.get("canonical_name") or "", *(entity.get("aliases") or [])]
                inventory_rows = list_media_inventory_candidates(
                    names=names,
                    domain=normalize_domain(entity.get("domain") or ""),
                    limit=_INVENTORY_LIMIT,
                )
                candidates = build_binding_candidates(entity, inventory_rows, limit=_BINDING_PER_ENTITY)
                if not candidates:
                    skipped += 1
                    continue
                if dry_run:
                    written += len(candidates)
                    continue
                for candidate in candidates:
                    saved = upsert_media_binding_candidate(candidate, operator_id=None)
                    if saved.get("_revived_from_deleted"):
                        revived += 1
                        insert_media_binding_audit_event(
                            candidate_id=int(saved["id"]),
                            entity_key=candidate.get("entity_key") or "",
                            industry_key=candidate.get("industry_key") or "general",
                            event_type="revived_from_deleted",
                            operator_id=None,
                            note="调研跑批桥接重跑命中已删除候选，复活为待审核候选，仍需人工审核才能进入投放映射",
                            payload={"candidate_key": candidate.get("candidate_key"), "via": "flywheel_bridge"},
                        )
                    written += 1
            except Exception as exc:
                failed += 1
                logger.debug(f"[FlywheelBridge] binding candidate {entity.get('entity_key')} failed: {exc}")
    after = 0 if dry_run else _scalar_count(
        "SELECT COUNT(*) AS c FROM geo_media_binding_candidates WHERE status <> 'deleted'", ()
    )
    inserted = max(0, after - before)
    updated = max(0, written - inserted)
    return {
        "loaded": loaded, "written": written, "inserted": inserted, "updated": updated,
        "skipped": skipped, "failed": failed, "revived_from_deleted": revived,
    }


async def _answer_entity_pending_preview() -> dict[str, Any]:
    """Best-effort pending/cost preview — never raises (table may not exist yet)."""
    try:
        return await rebuild_answer_entities(industry="", limit=1, dry_run=True, only_pending=True)
    except Exception:
        return {}


async def _stage_answer_entities(
    industries: list[str], dry_run: bool, *, force_answer_entity: bool = False
) -> dict[str, Any]:
    """LLM answer-entity extraction — gated by flag + per-round / per-day caps.

    force_answer_entity=True (paid self-serve round): bypass the flag gate AND the
    per-round(150)/daily(1500) platform throttles. A self-serve round is a single small
    industry the agent already paid for, so the shared daily cap must not starve it; a
    sane ceiling (FLYWHEEL_ANSWER_ENTITY_SELFSERVE_CAP, default 500) still guards against
    runaway extraction. Normal flag-driven rounds keep the caps unchanged (force defaults
    False → cron/manual behavior byte-for-byte identical).
    """
    if not is_feature_enabled("flywheel_answer_entity_auto") and not force_answer_entity:
        # dry_run still previews pending + cost (no LLM); real run is skipped.
        preview = await _answer_entity_pending_preview()
        return {
            "status": "skipped", "reason": "flag_off",
            "flag": "flywheel_answer_entity_auto",
            "pending_extract": preview.get("pending_extract"),
            "total_answers": preview.get("total_answers"),
            "llm_called": False,
        }
    if force_answer_entity:
        # paid self-serve: no per-round/daily truncation — process full pending up to a
        # sane ceiling so the agent's paid round is always visible (榜不空).
        # [R#6] 抽取【本轮自己的行业】的 pending,不是全行业 backlog(industry="" 会抽别行业的 pending,
        #   对本行业榜零贡献 = 纯浪费,且绕过按 (user,industry) 的 7 天/日配额 → 击穿平台日预算)。
        #   self-serve 是单行业,industries 只有 1 个;仍点亮本行业、成本 bound 到已付费的那个行业。
        selfserve_cap = max(1, int(os.getenv("FLYWHEEL_ANSWER_ENTITY_SELFSERVE_CAP", "500")))
        forced_industry = industries[0] if industries else ""
        result = await rebuild_answer_entities(
            industry=forced_industry, limit=selfserve_cap, dry_run=dry_run, only_pending=True,
        )
        result.setdefault("status", "success")
        result["forced"] = True
        result["cap_bypassed"] = True
        result["effective_limit"] = selfserve_cap
        result["forced_industry"] = forced_industry
        return result
    per_round_cap = max(0, int(os.getenv("FLYWHEEL_ANSWER_ENTITY_PER_ROUND_CAP", "150")))
    daily_cap = max(0, int(os.getenv("FLYWHEEL_ANSWER_ENTITY_DAILY_CAP", "1500")))
    today_count = _scalar_count(
        "SELECT COUNT(*) AS c FROM geo_research_answer_facts WHERE created_at >= date_trunc('day', NOW())", ()
    )
    remaining_daily = max(0, daily_cap - today_count)
    effective_limit = min(per_round_cap, remaining_daily)
    if effective_limit <= 0:
        preview = await _answer_entity_pending_preview()
        return {
            "status": "skipped", "reason": "daily_cap_reached",
            "per_round_cap": per_round_cap, "daily_cap": daily_cap, "today_count": today_count,
            "pending_extract": preview.get("pending_extract"),
            "llm_called": False,
        }
    result = await rebuild_answer_entities(
        industry="", limit=effective_limit, dry_run=dry_run, only_pending=True,
    )
    result.setdefault("status", "success")
    result["per_round_cap"] = per_round_cap
    result["daily_cap"] = daily_cap
    result["today_count"] = today_count
    result["effective_limit"] = effective_limit
    return result


def _stage_strategy(industries: list[str], dry_run: bool) -> dict[str, Any]:
    if not dry_run:
        init_writing_style_flywheel_tables()
    written = failed = 0
    scope = [i for i in (industries or []) if i]
    generated: list[str] = []
    for industry in scope:
        try:
            inputs = load_strategy_generation_inputs(industry, limit=_STRATEGY_INPUT_LIMIT)
            candidate = build_strategy_candidate(
                industry_key=industry,
                style_features=inputs["style_features"],
                source_signals=inputs["source_signals"],
                outcome_signals=inputs["outcome_signals"],
                operator_note="由调研跑批飞轮桥接自动生成，需管理员审核后才能启用（不自动接管生产写作）",
            )
            # build_strategy_candidate always returns status='shadow' / requires_admin_review=True /
            # production_takeover=False — the bridge only persists a pending-review version, never activates.
            if dry_run:
                generated.append(candidate.get("strategy_version") or industry)
                continue
            upsert_strategy_version(candidate)
            written += 1
            generated.append(candidate.get("strategy_version") or industry)
        except Exception as exc:
            failed += 1
            logger.warning(f"[FlywheelBridge] strategy candidate for {industry!r} failed: {exc}")
    return {
        "loaded": len(scope), "written": written, "inserted": written, "updated": 0,
        "skipped": 0, "failed": failed, "generated_versions": generated[:20],
        "note": "shadow / pending_review only — never auto-activated",
    }


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
async def _safe_stage(name: str, thunk: Callable[[], Any]) -> dict[str, Any]:
    try:
        result = await thunk()
        if not isinstance(result, dict):
            result = {"result": result}
        result.setdefault("status", "success")
        return result
    except Exception as exc:
        logger.warning(f"[FlywheelBridge] stage {name} failed: {exc}")
        return {"status": "failed", "error": str(exc)}


def _aggregate_totals(stages: dict[str, dict[str, Any]]) -> dict[str, int]:
    totals = {"loaded": 0, "inserted": 0, "updated": 0, "skipped": 0, "failed": 0}
    for stage in stages.values():
        for key in totals:
            try:
                totals[key] += int(stage.get(key, 0) or 0)
            except (TypeError, ValueError):
                pass
    return totals


async def run_round_bridge(
    round_id: str,
    *,
    trigger_source: str = "round_complete",
    dry_run: bool = False,
    page_size: int | None = None,
    force_answer_entity: bool = False,
) -> dict[str, Any]:
    """Refresh all downstream shadow/candidate layers for one completed round.

    dry_run=True previews counts (incl. answer-entity pending + LLM cost) without
    writing anything.  dry_run=False writes shadow/candidate layers only.

    force_answer_entity=True (paid self-serve round) threads down to the answer-entity
    stage so it runs regardless of flywheel_answer_entity_auto and bypasses the daily cap.
    Defaults False → cron/manual bridge runs unchanged.
    """
    if not round_id:
        return {"status": "skipped", "reason": "missing_round_id"}

    batch_id = _round_batch_id(round_id)
    key = f"flywheel_bridge::{round_id}"
    async with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return {"status": "in_progress", "round_id": round_id}
        _INFLIGHT.add(key)

    run_id: int | None = None
    stages: dict[str, dict[str, Any]] = {}
    try:
        size = int(page_size or os.getenv("FLYWHEEL_BRIDGE_PAGE_SIZE", str(_DEFAULT_PAGE_SIZE)))
        size = max(50, min(size, 5000))
        industries_raw = await asyncio.to_thread(_round_industries, batch_id)
        industries = _dedup([normalize_industry_key(i) for i in industries_raw if i])

        if not dry_run:
            # [包B · 2026-08-01] 自愈式清扫:开新 run 之前,先把超时仍 `running` 的孤儿跑收成
            # `stale_orphan` 终态。进程被杀 / 容器重启会把 run 永久钉在 running,任何面板都看不见,
            # 也没有 cron 来收(裁定:不加 cron、不动 scheduler)—— 所以收口挂在"下一次桥跑"这个
            # 自然入口上,同修法先例 `api/content_api.py:11841` (/chat-attachment/list 前先 sweep)。
            #
            # 🔴 三条边界:
            #   1. **fail-open**:sweep 只是清扫,失败绝不阻断桥跑本体(桥跑才是业务);
            #   2. 只在 `not dry_run` 时跑 —— dry_run 是预览,契约上零写库,不能因为清扫而破例;
            #   3. sweep 自带时间阈值(默认 STUCK_RUN_THRESHOLD_HOURS)且只改 status/finished_at
            #      不删行,所以**本轮刚起的 run 不会被自己标死**(此刻还没 start,更不可能命中)。
            try:
                swept = await asyncio.to_thread(sweep_stale_orphan_runs, dry_run=False)
                if swept.get("swept"):
                    logger.info(
                        "[FlywheelBridge] 清扫孤儿跑 %s 条(阈值 %sh): %s",
                        swept["swept"], swept["threshold_hours"], swept["run_ids"],
                    )
            except Exception as exc:  # fail-open:清扫失败不拖垮桥跑
                logger.warning(f"[FlywheelBridge] stale_orphan 清扫失败(忽略,不阻断桥跑): {exc}")

            run_id = await asyncio.to_thread(
                start_bridge_run, round_id, batch_id,
                trigger_source=trigger_source, dry_run=dry_run,
            )

        stages["source_signals"] = await _safe_stage(
            "source_signals",
            lambda: asyncio.to_thread(_stage_source_signals, batch_id, size, dry_run),
        )
        stages["answer_adoption"] = await _safe_stage(
            "answer_adoption",
            lambda: asyncio.to_thread(_stage_answer_adoption, batch_id, size, dry_run),
        )
        stages["media_entities"] = await _safe_stage(
            "media_entities",
            lambda: asyncio.to_thread(_stage_media_entities, industries, dry_run),
        )
        stages["binding_candidates"] = await _safe_stage(
            "binding_candidates",
            lambda: asyncio.to_thread(_stage_binding_candidates, industries, dry_run),
        )
        stages["answer_entities"] = await _safe_stage(
            "answer_entities",
            lambda: _stage_answer_entities(
                industries, dry_run, force_answer_entity=force_answer_entity
            ),
        )
        stages["strategy_candidate"] = await _safe_stage(
            "strategy_candidate",
            lambda: asyncio.to_thread(_stage_strategy, industries, dry_run),
        )

        totals = _aggregate_totals(stages)
        overall = "success" if all(
            s.get("status") in ("success", "skipped") for s in stages.values()
        ) else "partial"
        last_raw = (stages.get("source_signals") or {}).get("last_processed_raw_id")

        if not dry_run and run_id is not None:
            await asyncio.to_thread(
                finish_bridge_run, run_id,
                status=overall, stages=stages, totals=totals,
                last_processed_raw_id=last_raw, error=None,
            )

        # [E1] 真跑落库后失效 E1 榜进程缓存(媒体实体/信号写库 choke point;dry_run 不写不失效)。
        # fail-soft:缓存层不可用绝不影响桥接本身(照 media_entity_flywheel_api._invalidate_panorama_cache 模式)。
        if not dry_run:
            try:
                from writing.flywheel_cache import SCOPE_ENTITY_RANK, invalidate
                invalidate([SCOPE_ENTITY_RANK])
            except Exception:
                pass

        return {
            "status": overall, "round_id": round_id, "batch_id": batch_id,
            "dry_run": dry_run, "industries": industries,
            "stages": stages, "totals": totals,
        }
    except Exception as exc:
        logger.warning(f"[FlywheelBridge] round {round_id} bridge failed: {exc}")
        if not dry_run and run_id is not None:
            try:
                await asyncio.to_thread(
                    finish_bridge_run, run_id,
                    status="failed", stages=stages,
                    totals=_aggregate_totals(stages), error=str(exc),
                )
            except Exception:
                pass
        return {"status": "failed", "round_id": round_id, "error": str(exc), "stages": stages}
    finally:
        async with _INFLIGHT_LOCK:
            _INFLIGHT.discard(key)


# ---------------------------------------------------------------------------
# [T1] global catch-up backfill (all completed rounds) — reuses the same stages
# ---------------------------------------------------------------------------
def _backfill_target_batches(since_days: int, limit: int) -> list[str]:
    """Completed rounds' batch_ids that have cited raw (optionally within a window)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = []
        window = ""
        if since_days and int(since_days) > 0:
            window = "AND raw.created_at >= NOW() - (%s || ' days')::interval"
            params.append(int(since_days))
        params.append(int(limit))
        cur.execute(
            f"""
            SELECT raw.batch_id
              FROM geo_research_raw raw
              JOIN geo_research_round rnd ON rnd.batch_id = raw.batch_id
             WHERE rnd.status = 'completed'
               AND COALESCE(raw.cite_url, '') <> ''
               {window}
             GROUP BY raw.batch_id
             ORDER BY MAX(raw.created_at) DESC
             LIMIT %s
            """,
            params,
        )
        return [r["batch_id"] for r in cur.fetchall() if r.get("batch_id")]
    finally:
        conn.close()


async def run_source_signals_backfill(
    *,
    since_days: int = 0,
    dry_run: bool = True,
    page_size: int | None = None,
    max_rounds: int = 500,
) -> dict[str, Any]:
    """[T1] Global source_signals + answer_adoption catch-up backfill for the 6-17 gap.

    Reuses the round-scoped _stage_source_signals + _stage_answer_adoption per completed
    round (paginated, idempotent ON CONFLICT upserts). Concurrency-guarded (single backfill
    at a time), per-batch fail-soft. dry_run counts without writing. This is complementary to
    the always-on flywheel_round_bridge round hook — it catches up rounds that completed
    BEFORE the hook was enabled.
    """
    key = "source_signals_backfill"
    async with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return {"status": "in_progress"}
        _INFLIGHT.add(key)
    # [Finding #6] 真跑写一条 bridge_run 审计,整批失败也可见(与 run_round_bridge 一致);dry_run 不记。
    run_id: int | None = None
    if not dry_run:
        try:
            run_id = await asyncio.to_thread(
                start_bridge_run, "(backfill)", "", trigger_source="backfill", dry_run=False,
            )
        except Exception:
            run_id = None
    try:
        size = int(page_size or os.getenv("FLYWHEEL_BRIDGE_PAGE_SIZE", str(_DEFAULT_PAGE_SIZE)))
        size = max(50, min(size, 5000))
        batch_ids = await asyncio.to_thread(_backfill_target_batches, since_days, max_rounds)
        ss_totals = {"loaded": 0, "would_write": 0, "inserted": 0, "updated": 0, "skipped": 0, "failed": 0}
        aa_totals = {"loaded": 0, "would_write": 0, "inserted": 0, "updated": 0, "skipped": 0, "failed": 0}
        per_batch: list[dict[str, Any]] = []
        for batch_id in batch_ids:
            try:
                ss = await asyncio.to_thread(_stage_source_signals, batch_id, size, dry_run)
                aa = await asyncio.to_thread(_stage_answer_adoption, batch_id, size, dry_run)
                for k in ss_totals:
                    ss_totals[k] += int(ss.get(k, 0) or 0)
                for k in aa_totals:
                    aa_totals[k] += int(aa.get(k, 0) or 0)
                per_batch.append({
                    "batch_id": batch_id,
                    "source_signals": {kk: ss.get(kk) for kk in ("loaded", "would_write", "inserted", "updated", "failed")},
                    "adoption": {kk: aa.get(kk) for kk in ("loaded", "would_write", "inserted", "updated")},
                })
            except Exception as exc:  # per-batch fail-soft
                logger.warning(f"[FlywheelBridge] backfill batch {batch_id} failed: {exc}")
                per_batch.append({"batch_id": batch_id, "error": str(exc)})
        result = {
            "status": "success",
            "dry_run": dry_run,
            "since_days": since_days,
            "batches_processed": len(batch_ids),
            "source_signals_totals": ss_totals,
            "adoption_metrics_totals": aa_totals,
            "per_batch": per_batch[:100],
            # [Finding #5] dry_run 预览:source_signals 看 would_write(会写多少);adoption_metrics 读的是
            #   已存在 source_signals,对从未桥接的 gap 轮 dry_run 下会偏低,真跑(先写 source_signals)后即准。
            "note": (
                "dry_run 预览:source_signals 的 would_write = 会写入的行数;adoption_metrics 依赖 source_signals "
                "已写入,gap 轮在 dry_run 下偏低,真跑后即准确。"
            ),
            "shadow_only": True,
            "production_takeover": False,
        }
        if run_id is not None:
            try:
                await asyncio.to_thread(
                    finish_bridge_run, run_id,
                    status="success",
                    stages={"source_signals": ss_totals, "answer_adoption": aa_totals},
                    totals=ss_totals, error=None,
                )
            except Exception:
                pass
        return result
    except Exception as exc:
        if run_id is not None:
            try:
                await asyncio.to_thread(
                    finish_bridge_run, run_id, status="failed", stages={}, totals={}, error=str(exc),
                )
            except Exception:
                pass
        raise
    finally:
        async with _INFLIGHT_LOCK:
            _INFLIGHT.discard(key)
