"""Read projections for the GEO article closed-loop sidecar."""
from __future__ import annotations

from typing import Any, Iterable

from services.article_closed_loop_contract import feature_flags


def _conn():
    from db.diagnosis_db import get_connection

    return get_connection()


def _scalar(row: dict[str, Any] | None, key: str) -> int:
    return int((row or {}).get(key) or 0)


def get_project_summary(quote_id: int) -> dict[str, Any]:
    flags = feature_flags()
    if not flags["ARTICLE_PLAN_READ_SUMMARY_ENABLED"]:
        return {"enabled": False, "available": False}
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT q.id,q.brand_name,q.writing_status,q.article_plan_writing_mode,
                   r.id AS revision_id,r.delivery_count
              FROM quotes q
              JOIN brands b ON b.id=q.brand_id AND COALESCE(b.is_deleted,FALSE)=FALSE
              LEFT JOIN LATERAL (
                  SELECT id,delivery_count FROM geo_article_contract_revisions
                   WHERE quote_id=q.id ORDER BY id DESC LIMIT 1
              ) r ON TRUE
             WHERE q.id=%s
            """,
            (int(quote_id),),
        )
        project = cur.fetchone()
        if not project:
            return {"enabled": True, "simple_ui_enabled": flags["ARTICLE_WRITING_SIMPLE_UI_ENABLED"], "available": False, "reason": "not_available"}
        if not project.get("revision_id"):
            return {
                "enabled": True,
                "simple_ui_enabled": flags["ARTICLE_WRITING_SIMPLE_UI_ENABLED"],
                "available": False,
                "project_status": project.get("writing_status") or "pending",
                "message": "交付计划正在准备，现有写作功能可继续使用。",
            }
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE current_state IN ('active','blocked')) AS due,
                   COUNT(*) FILTER (WHERE current_state='active') AS active,
                   COUNT(*) FILTER (WHERE current_state='blocked') AS blocked,
                   COUNT(*) FILTER (WHERE current_state='cancelled') AS cancelled,
                   COUNT(*) FILTER (WHERE current_state='superseded') AS superseded,
                   COUNT(*) FILTER (WHERE topic_id IS NOT NULL) AS titled,
                   COUNT(*) FILTER (WHERE article_id IS NOT NULL) AS generated,
                   COUNT(*) FILTER (WHERE completion_evidence IS NOT NULL) AS published
              FROM geo_article_delivery_slots WHERE quote_id=%s
            """,
            (int(quote_id),),
        )
        counts = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE a.evidence_manifest_hash IS NOT NULL) AS evidence_bound_articles,
                   COUNT(*) FILTER (WHERE a.article_review_status IN ('blocked','rewrite_required')) AS needs_review,
                   COUNT(*) AS article_count
              FROM geo_article_delivery_slots s
              JOIN articles a ON a.id=s.article_id
             WHERE s.quote_id=%s
            """,
            (int(quote_id),),
        )
        health = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT blocked_user_message,owner_kind,next_action,target_resolution_at,escalation_status
              FROM geo_article_delivery_slots
             WHERE quote_id=%s AND current_state='blocked'
             ORDER BY blocked_at,contract_ordinal LIMIT 20
            """,
            (int(quote_id),),
        )
        blockers = [
            {
                "message": row.get("blocked_user_message") or "这篇文章需要人工处理",
                "owner": row.get("owner_kind") or "待确认",
                "next_action": row.get("next_action") or "联系运营处理",
                "target_time": row.get("target_resolution_at"),
                "escalation": row.get("escalation_status"),
            }
            for row in cur.fetchall()
        ]
        blocked = _scalar(counts, "blocked")
        due = _scalar(counts, "due")
        titled = _scalar(counts, "titled")
        generated = _scalar(counts, "generated")
        if blocked:
            next_action = {"label": f"处理 {blocked} 个问题", "kind": "resolve_blockers"}
        elif titled < due:
            next_action = {"label": "生成标题", "kind": "generate_titles"}
        elif generated < due:
            next_action = {"label": "选择文章并生成正文", "kind": "generate_articles"}
        elif _scalar(health, "needs_review"):
            next_action = {"label": "处理文章审核", "kind": "review_articles"}
        else:
            next_action = {"label": "前往发布中心", "kind": "open_publish_center"}
        article_count = _scalar(health, "article_count")
        health_status = "available" if article_count else "insufficient_information"
        return {
            "enabled": True,
            "simple_ui_enabled": flags["ARTICLE_WRITING_SIMPLE_UI_ENABLED"],
            "available": True,
            "project_status": project.get("writing_status") or "pending",
            "delivery": {
                "contract_total": int(project.get("delivery_count") or 0),
                "due": due,
                "completed": generated,
                "pending": max(0, due - generated),
                "blocked": blocked,
                "published": _scalar(counts, "published"),
            },
            "health": {
                "status": health_status,
                "articles_with_evidence_record": _scalar(health, "evidence_bound_articles"),
                "articles_needing_review": _scalar(health, "needs_review"),
                "waiting_for_information": blocked,
                "message": (
                    "健康信息来自已保存的证据和审核事实。"
                    if health_status == "available"
                    else "尚无足够信息，生成正文后再显示健康情况。"
                ),
            },
            "blockers": blockers,
            "next_action": next_action,
        }
    finally:
        conn.close()


def list_agent_tasks(allowed_brand_ids: list[int] | None, *, limit: int = 100) -> dict[str, Any]:
    if not feature_flags()["ARTICLE_PLAN_READ_SUMMARY_ENABLED"]:
        return {"enabled": False, "items": []}
    conn = _conn()
    try:
        cur = conn.cursor()
        params: list[Any] = []
        brand_filter = ""
        if allowed_brand_ids is not None:
            safe_ids = [int(value) for value in allowed_brand_ids if int(value) > 0]
            if not safe_ids:
                return {"enabled": True, "items": []}
            brand_filter = " AND q.brand_id = ANY(%s)"
            params.append(safe_ids)
        params.append(max(1, min(int(limit), 500)))
        cur.execute(
            f"""
            SELECT q.id AS quote_id,q.brand_name,
                   COUNT(*) AS affected_count,
                   MIN(s.blocked_user_message) AS message,
                   MIN(s.owner_kind) AS owner_kind,
                   MIN(s.next_action) AS next_action,
                   MIN(s.target_resolution_at) AS target_resolution_at,
                   BOOL_OR(s.escalation_status='escalated') AS escalated
              FROM geo_article_delivery_slots s
              JOIN quotes q ON q.id=s.quote_id
              JOIN brands b ON b.id=s.brand_id AND COALESCE(b.is_deleted,FALSE)=FALSE
             WHERE s.current_state='blocked'{brand_filter}
             GROUP BY q.id,q.brand_name
             ORDER BY BOOL_OR(s.owner_kind='agent') DESC,
                      MIN(s.target_resolution_at) NULLS LAST,
                      BOOL_OR(s.escalation_status='escalated') DESC,
                      q.id
             LIMIT %s
            """,
            params,
        )
        return {
            "enabled": True,
            "items": [
                {
                    "quote_id": int(row["quote_id"]),
                    "project_name": row.get("brand_name") or f"项目 {row['quote_id']}",
                    "affected_articles": int(row.get("affected_count") or 0),
                    "message": row.get("message") or "需要人工处理",
                    "owner": row.get("owner_kind") or "待确认",
                    "target_time": row.get("target_resolution_at"),
                    "escalated": bool(row.get("escalated")),
                    "next_action": row.get("next_action") or "联系运营处理",
                }
                for row in cur.fetchall()
            ],
        }
    finally:
        conn.close()


def get_admin_state(*, quote_id: int | None = None, limit: int = 100) -> dict[str, Any]:
    conn = _conn()
    try:
        cur = conn.cursor()
        where = "WHERE o.quote_id=%s" if quote_id is not None else ""
        params: list[Any] = [int(quote_id)] if quote_id is not None else []
        params.append(max(1, min(int(limit), 500)))
        cur.execute(
            f"""
            SELECT o.id,o.event_kind,o.source_kind,o.source_id,o.source_version,o.quote_id,
                   o.brand_id,o.status,o.attempt_count,o.available_at,o.last_error,
                   o.manual_resolution_status,o.created_at,o.updated_at
              FROM geo_article_plan_outbox o {where}
             ORDER BY o.id DESC LIMIT %s
            """,
            params,
        )
        outbox = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT id,quote_id,run_mode,compiler_version,status,verdict,failure_reason,created_at,finished_at
              FROM geo_article_plan_runs
             WHERE (%s IS NULL OR quote_id=%s)
             ORDER BY id DESC LIMIT %s
            """,
            (quote_id, quote_id, max(1, min(int(limit), 500))),
        )
        runs = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT id,quote_id,correction_type,reason,candidate_status,occurred_at
              FROM geo_article_correction_signals
             WHERE (%s IS NULL OR quote_id=%s)
             ORDER BY id DESC LIMIT %s
            """,
            (quote_id, quote_id, max(1, min(int(limit), 500))),
        )
        corrections = [dict(row) for row in cur.fetchall()]
        return {"outbox": outbox, "runs": runs, "correction_candidates": corrections}
    finally:
        conn.close()
