"""Twice-monthly GEO article evolution audit; never auto-activates a style.

This module materializes a small decision snapshot from existing SSOTs.  It
does not create a second observation ledger, alter paid monitoring questions,
or treat Jina retrieval as citation proof.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Final

from psycopg2.extras import Json

from services.article_closed_loop_contract import feature_flags


EVOLUTION_CYCLE_VERSION: Final = "geo-article-evolution-cycle-v1.0"


def _cycle_key(today: date | None = None) -> str:
    day = today or datetime.now(timezone.utc).date()
    slot = 1 if day.day < 16 else 16
    return f"{day:%Y-%m}-{slot:02d}"


def _group_counts(cur, sql: str) -> dict[str, int]:
    cur.execute(sql)
    return {str(row["key"] or "unknown"): int(row["count"] or 0) for row in cur.fetchall()}


def load_correction_candidate_summary(cur) -> dict[str, Any]:
    """Read tenant-scoped negative signals as review candidates only."""
    if not feature_flags()["ARTICLE_FLYWHEEL_CANDIDATE_ENABLED"]:
        return {
            "source": "disabled",
            "candidate_only": True,
            "automatic_template_writeback_allowed": False,
            "counts": {},
        }
    counts = _group_counts(
        cur,
        "SELECT c.correction_type AS key, COUNT(*) AS count "
        "FROM geo_article_correction_signals c "
        "JOIN quotes q ON q.id=c.quote_id AND q.brand_id=c.brand_id "
        "JOIN brands b ON b.id=c.brand_id AND COALESCE(b.is_deleted,FALSE)=FALSE "
        "WHERE c.candidate_status='candidate' AND c.occurred_at <= CURRENT_TIMESTAMP "
        "AND c.tenant_owner_user_id=COALESCE(q.owner_user_id,b.owner_user_id) "
        "GROUP BY correction_type",
    )
    return {
        "source": "tenant_scoped_candidate_only",
        "candidate_only": True,
        "automatic_template_writeback_allowed": False,
        "counts": counts,
    }


def build_evolution_snapshot() -> dict[str, Any]:
    """Read all decision inputs once; any unavailable source stays explicit."""
    from services.article_data_health import get_article_data_health
    from db.connection import get_connection

    health = get_article_data_health()
    from services.article_expert_calibration import evaluate_gold_standard

    calibrations: dict[str, Any] = {}
    for judge_kind in ("article_review", "target_outcome"):
        try:
            calibrations[judge_kind] = evaluate_gold_standard(judge_kind)
        except Exception as exc:
            calibrations[judge_kind] = {
                "decision": "INSUFFICIENT_SAMPLES",
                "error": type(exc).__name__,
            }
    conn = get_connection()
    try:
        cur = conn.cursor()
        corpus = _group_counts(
            cur,
            "SELECT COALESCE(corpus_grade,'JC0') AS key, COUNT(*) AS count "
            "FROM geo_research_articles GROUP BY COALESCE(corpus_grade,'JC0')",
        )
        reviews = _group_counts(
            cur,
            "SELECT COALESCE(article_review_status,'legacy_unreviewed') AS key, COUNT(*) AS count "
            "FROM articles a JOIN quotes q ON q.id=a.quote_id "
            "JOIN brands b ON b.id=q.brand_id AND COALESCE(b.is_deleted,FALSE)=FALSE "
            "GROUP BY COALESCE(article_review_status,'legacy_unreviewed')",
        )
        profiles = _group_counts(
            cur,
            "SELECT COALESCE(publication_profile,'standard') AS key, COUNT(*) AS count "
            "FROM articles a JOIN quotes q ON q.id=a.quote_id "
            "JOIN brands b ON b.id=q.brand_id AND COALESCE(b.is_deleted,FALSE)=FALSE "
            "GROUP BY COALESCE(publication_profile,'standard')",
        )
        experiments = _group_counts(
            cur,
            "SELECT state AS key, COUNT(*) AS count FROM geo_article_experiments GROUP BY state",
        )
        questions = _group_counts(
            cur,
            "SELECT evolution_status AS key, COUNT(*) AS count "
            "FROM geo_research_prompts WHERE query_kind='derived_research' GROUP BY evolution_status",
        )
        correction_summary = load_correction_candidate_summary(cur)
        correction_candidates = correction_summary["counts"]
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE article_id IS NOT NULL) AS bound,
                   COUNT(*) FILTER (WHERE article_id IS NULL) AS reserved
              FROM geo_article_experiment_assignments x
              JOIN topics t ON t.id=x.topic_id
              JOIN quotes q ON q.id=t.quote_id
              JOIN brands b ON b.id=q.brand_id AND COALESCE(b.is_deleted,FALSE)=FALSE
            """
        )
        assignments = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT COALESCE(SUM(estimated_cost),0) AS tracked_llm_yuan,
                   COUNT(*) AS tracked_llm_calls
              FROM llm_call_log
             WHERE caller IN (
                    'article_writing','article_evidence_research','writing_flywheel'
                  )
               AND created_at >= date_trunc('month', CURRENT_TIMESTAMP)
               AND created_at <= CURRENT_TIMESTAMP
            """
        )
        llm_cost = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT COUNT(*) AS fetch_count, COALESCE(SUM(usage_tokens),0) AS usage_tokens
              FROM geo_research_article_fetches
             WHERE fetched_at >= date_trunc('month', CURRENT_TIMESTAMP)
               AND fetched_at <= CURRENT_TIMESTAMP
            """
        )
        fetch_cost = dict(cur.fetchone() or {})
    finally:
        conn.close()

    recommendations: list[dict[str, str]] = []
    if health.get("state") == "data_blocked":
        recommendations.append({
            "priority": "P0",
            "action": "repair_data_lineage_before_style_decision",
            "reason": "直接发布快照、监测问题或模型血缘不足，不能裁决文体效果。",
        })
    if int(corpus.get("JC3", 0)) > 0 and int(corpus.get("JC5", 0)) == 0:
        recommendations.append({
            "priority": "P1",
            "action": "collect_direct_outcome_labels_for_jina_bodies",
            "reason": "已有可分析正文但没有直接结果标签，只能研究结构，不能证明引用效果。",
        })
    pending_reviews = sum(
        int(reviews.get(key, 0))
        for key in ("blocked", "rewrite_required", "pending_human_review", "legacy_unreviewed")
    )
    correction_count = sum(correction_candidates.values())
    if correction_count:
        recommendations.append({
            "priority": "P1",
            "action": "review_agent_correction_candidates",
            "reason": f"有 {correction_count} 条代理显式编辑/纠错候选；只允许人工蒸馏，不得自动写回模板。",
        })
    if pending_reviews:
        recommendations.append({
            "priority": "P1",
            "action": "clear_article_review_queue",
            "reason": f"仍有 {pending_reviews} 篇文章待重写或人工签发。",
        })
    for judge_kind, calibration in calibrations.items():
        if calibration.get("decision") != "PASS":
            recommendations.append({
                "priority": "P1",
                "action": f"calibrate_{judge_kind}",
                "reason": f"{judge_kind} 金标准状态为 {calibration.get('decision')}，不得把专家输出当成稳定真值。",
            })
    if not recommendations:
        recommendations.append({
            "priority": "P2",
            "action": "review_preregistered_experiments_and_distilled_drafts",
            "reason": "数据面无新增阻断；继续人工审核候选，不自动改变生产配比。",
        })
    cost_summary = {
        "tracked_llm_yuan_month_to_date": float(llm_cost.get("tracked_llm_yuan") or 0),
        "tracked_llm_calls_month_to_date": int(llm_cost.get("tracked_llm_calls") or 0),
        "research_fetches_month_to_date": int(fetch_cost.get("fetch_count") or 0),
        "research_fetch_usage_tokens": int(fetch_cost.get("usage_tokens") or 0),
        "research_search_reader_unit_price_status": "tracked_in_llm_call_log_pricing_ssot",
        "customer_price_pass_through": "requires_explicit_business_decision",
        "interpretation": "写作、证据核验、搜索/阅读与评审均按实际调用日志汇总；对账仍以供应商账单为准。",
    }
    return {
        "cycle_version": EVOLUTION_CYCLE_VERSION,
        "cycle_key": _cycle_key(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "data_health": health,
        "corpus_summary": corpus,
        "review_summary": reviews,
        "expert_calibration": calibrations,
        "publication_profiles": profiles,
        "experiment_summary": experiments,
        "experiment_assignments": {
            "reserved": int(assignments.get("reserved") or 0),
            "bound": int(assignments.get("bound") or 0),
        },
        "question_candidate_summary": questions,
        "correction_candidate_summary": correction_summary,
        "cost_summary": cost_summary,
        "recommendations": recommendations,
        "automatic_activation_allowed": False,
        "paid_monitoring_questions_unchanged": True,
        "jina_body_interpretation": "JC3=可分析正文；只有 JC5 直接结果标签可进入效果证据。",
    }


def persist_evolution_cycle(*, actor_user_id: int, trigger: str = "manual") -> dict[str, Any]:
    """Persist one idempotent snapshot per half-month; remains human-decision only."""
    snapshot = build_evolution_snapshot()
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_article_evolution_runs (
                cycle_key, cycle_version, trigger_source, state, truth_level,
                data_health, corpus_summary, review_summary, experiment_summary,
                question_summary, cost_summary, recommendations, created_by, started_at, finished_at
            ) VALUES (%s,%s,%s,'review_ready',%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
            ON CONFLICT (cycle_key) DO NOTHING
            RETURNING *
            """,
            (
                snapshot["cycle_key"], EVOLUTION_CYCLE_VERSION, str(trigger or "manual")[:40],
                snapshot["data_health"].get("truth_level"),
                Json(snapshot["data_health"]), Json(snapshot["corpus_summary"]),
                Json({
                    "article_reviews": snapshot["review_summary"],
                    "publication_profiles": snapshot["publication_profiles"],
                    "expert_calibration": snapshot["expert_calibration"],
                    "agent_correction_candidates": snapshot["correction_candidate_summary"],
                }),
                Json({
                    "states": snapshot["experiment_summary"],
                    "assignments": snapshot["experiment_assignments"],
                }),
                Json(snapshot["question_candidate_summary"]),
                Json(snapshot["cost_summary"]),
                Json(snapshot["recommendations"]), int(actor_user_id or 0),
            ),
        )
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT * FROM geo_article_evolution_runs WHERE cycle_key=%s", (snapshot["cycle_key"],))
            row = cur.fetchone()
        conn.commit()
        return {"run": dict(row), "snapshot": snapshot, "idempotent": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_evolution_cycles(*, limit: int = 24) -> list[dict[str, Any]]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM geo_article_evolution_runs ORDER BY started_at DESC, id DESC LIMIT %s",
            (max(1, min(int(limit), 100)),),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def review_evolution_cycle(
    run_id: int,
    *,
    actor_user_id: int,
    decision: str,
    note: str,
) -> dict[str, Any]:
    """Human closes the cycle; approval never activates a prompt by itself."""
    if decision not in {"approved", "no_change", "rejected"}:
        raise ValueError("invalid_evolution_review_decision")
    if len(str(note or "").strip()) < 5:
        raise ValueError("review_note_required")
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_article_evolution_runs
               SET state=%s, reviewed_by=%s, reviewed_at=NOW(), review_note=%s
             WHERE id=%s AND state='review_ready'
            RETURNING *
            """,
            (decision, actor_user_id, note.strip(), run_id),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("evolution_run_not_reviewable")
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
