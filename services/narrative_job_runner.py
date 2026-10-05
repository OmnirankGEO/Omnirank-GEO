"""Persistent job queue for report v3 narrative enrichment."""

from __future__ import annotations

import logging

from config.settings_manager import load_settings
from db.connection import get_connection
from services.llm_rich_narrative import _modules_from_jsonb, enrich_diagnosis_narrative
from services.narrative_budget import get_monthly_budget_snapshot

logger = logging.getLogger("GEO-NarrativeRunner")


def enqueue_narrative_enrichment(diagnosis_id: int) -> dict:
    settings = load_settings()
    if not getattr(settings, "report_v3_auto_enrich", False):
        return {"status": "skipped", "reason": "auto_enrich_disabled"}

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT report_v2_version, report_v2_modules_jsonb
            FROM diagnosis_records
            WHERE id = %s
            """,
            (diagnosis_id,),
        )
        row = cursor.fetchone()
        if not row or row.get("report_v2_version") != "v2" or not row.get("report_v2_modules_jsonb"):
            return {"status": "skipped", "reason": "jsonb_not_ready"}
        if not _modules_from_jsonb(row.get("report_v2_modules_jsonb")):
            return {"status": "skipped", "reason": "client_report_not_ready"}
        cursor.execute(
            """
            INSERT INTO narrative_enrichment_locks (diagnosis_id, status, retries, created_at, updated_at)
            VALUES (%s, 'pending', 0, NOW(), NOW())
            ON CONFLICT (diagnosis_id) DO UPDATE SET
                status = CASE
                    WHEN narrative_enrichment_locks.status IN ('succeeded', 'succeeded_partial')
                    THEN narrative_enrichment_locks.status
                    ELSE 'pending'
                END,
                updated_at = NOW()
            """,
            (diagnosis_id,),
        )
        conn.commit()
        return {"status": "pending", "diagnosis_id": diagnosis_id}
    finally:
        conn.close()


async def run_pending_once(limit: int = 3) -> list[dict]:
    settings = load_settings()
    budget = get_monthly_budget_snapshot()
    monthly_limit = float(getattr(settings, "llm_narrative_monthly_yuan", 100.0) or 0)
    spent = float(budget.get("total_yuan") or 0)
    if monthly_limit and spent >= monthly_limit:
        return [{"status": "budget_blocked", "spent_yuan": spent, "limit_yuan": monthly_limit}]

    max_concurrent = int(getattr(settings, "llm_narrative_max_concurrent", 3) or 3)
    limit = max(1, min(int(limit or max_concurrent), max_concurrent))

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT diagnosis_id
            FROM narrative_enrichment_locks
            WHERE status = 'pending'
               OR (status = 'failed_full' AND retries < 3 AND updated_at < NOW() - INTERVAL '5 minutes')
               OR (status = 'running' AND last_heartbeat_at < NOW() - INTERVAL '10 minutes')
            ORDER BY updated_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        )
        rows = cursor.fetchall() or []
        for row in rows:
            cursor.execute(
                """
                UPDATE narrative_enrichment_locks
                SET status = 'running',
                    started_at = COALESCE(started_at, NOW()),
                    last_heartbeat_at = NOW(),
                    updated_at = NOW()
                WHERE diagnosis_id = %s
                  AND status IN ('pending', 'failed_full', 'running')
                """,
                (row["diagnosis_id"],),
            )
        conn.commit()
    finally:
        conn.close()

    results = []
    for row in rows:
        results.append(await enrich_diagnosis_narrative(int(row["diagnosis_id"])))
    return results
