"""Rich narrative enrichment for public report v3.

The production path is intentionally durable: DB rows represent jobs and the
renderer falls back to existing customer modules when enrichment is not ready.
Fallback data is evidence-preserving and must never invent customer findings.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from db.connection import get_connection

logger = logging.getLogger("GEO-RichNarrative")


def _modules_from_jsonb(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = {}
    from services.report_html_renderer import get_client_report_modules

    return get_client_report_modules(value)


def classify_section_results(results: list[bool]) -> str:
    success_count = sum(1 for item in results if item)
    if success_count == len(results) and results:
        return "succeeded"
    if success_count == 0:
        return "failed_full"
    return "succeeded_partial"


def build_fallback_rich_narrative(modules: dict) -> dict | None:
    """Preserve existing customer evidence without synthesizing conclusions."""
    safe_modules = modules if isinstance(modules, dict) else {}
    # Numeric module 1 is the current V2 producer's canonical customer
    # conclusion.  Historical aliases must never override it when both exist.
    module_one = safe_modules.get("1") or safe_modules.get(1)
    module_one = module_one if isinstance(module_one, dict) else {}
    has_module_one_artifact = any(
        isinstance(module_one.get(field), str) and module_one[field].strip()
        for field in ("insight", "conclusion_text", "rendered_md")
    )
    summary = ""
    source_module = ""
    for field in ("insight", "conclusion_text"):
        candidate = module_one.get(field)
        if isinstance(candidate, str) and candidate.strip():
            summary = candidate.strip()
            source_module = "1"
            break

    summary_module = safe_modules.get("executive_summary")
    summary_module = summary_module if isinstance(summary_module, dict) else {}
    if not summary and not has_module_one_artifact:
        legacy_summary = summary_module.get("summary")
        if isinstance(legacy_summary, str) and legacy_summary.strip():
            summary = legacy_summary.strip()
            source_module = "executive_summary"

    # The legacy alias is an all-or-nothing compatibility fallback.  Once the
    # canonical numeric artifact exists, none of its stale copy may re-enter a
    # newly persisted narrative through the findings list.
    raw_findings = None if has_module_one_artifact else summary_module.get("key_findings")
    findings = []
    if isinstance(raw_findings, (list, tuple)):
        findings = [
            item.strip()
            for item in raw_findings
            if isinstance(item, str) and item.strip()
        ][:5]
    if not summary and findings:
        summary = findings[0]
        source_module = "executive_summary.key_findings"
    if not summary:
        return None
    return {
        "version": "v3_evidence_only_2026_07_19_r3",
        "source_module": source_module,
        "executive_summary": summary,
        "key_findings": findings,
        # Existing numeric customer modules remain the section-level fallback.
        "sections": {},
    }


async def enrich_diagnosis_narrative(diagnosis_id: int, force: bool = False) -> dict:
    """Durably enrich a diagnosis with rich_narrative.

    This implementation writes only an evidence-preserving fallback narrative.
    Missing customer modules fail closed and are never created from internal data.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, report_v2_version, report_v2_modules_jsonb
            FROM diagnosis_records
            WHERE id = %s
            """,
            (diagnosis_id,),
        )
        row = cursor.fetchone()
        if not row or row.get("report_v2_version") != "v2":
            return {"status": "skipped", "reason": "v2_not_ready"}
        modules_jsonb = row.get("report_v2_modules_jsonb") or {}
        if isinstance(modules_jsonb, str):
            try:
                modules_jsonb = json.loads(modules_jsonb)
            except Exception:
                modules_jsonb = {}
        modules = _modules_from_jsonb(modules_jsonb)
        from services.report_html_renderer import is_client_report_ready

        if not is_client_report_ready(modules_jsonb):
            cursor.execute(
                """
                UPDATE narrative_enrichment_locks
                SET status = 'failed_full',
                    retries = GREATEST(retries, 3),
                    error_message = 'client_report_not_ready',
                    updated_at = NOW()
                WHERE diagnosis_id = %s
                """,
                (diagnosis_id,),
            )
            conn.commit()
            return {
                "status": "failed_full",
                "diagnosis_id": diagnosis_id,
                "reason": "client_report_not_ready",
            }
        narrative = build_fallback_rich_narrative(modules)
        if narrative is None:
            cursor.execute(
                """
                INSERT INTO narrative_enrichment_locks
                    (diagnosis_id, status, retries, token_cost_yuan,
                     error_message, completed_at, updated_at)
                VALUES (%s, 'succeeded_partial', 0, 0,
                        'customer_summary_not_available', NOW(), NOW())
                ON CONFLICT (diagnosis_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    token_cost_yuan = EXCLUDED.token_cost_yuan,
                    error_message = EXCLUDED.error_message,
                    completed_at = EXCLUDED.completed_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (diagnosis_id,),
            )
            conn.commit()
            return {
                "status": "succeeded_partial",
                "diagnosis_id": diagnosis_id,
                "reason": "customer_summary_not_available",
            }
        client = modules_jsonb.setdefault("client", {})
        client_modules = client.setdefault("modules", {})
        client_modules["rich_narrative"] = narrative
        cursor.execute(
            """
            UPDATE diagnosis_records
            SET report_v2_modules_jsonb = %s
            WHERE id = %s
            """,
            (json.dumps(modules_jsonb, ensure_ascii=False), diagnosis_id),
        )
        cursor.execute(
            """
            INSERT INTO narrative_enrichment_locks
                (diagnosis_id, status, retries, token_cost_yuan, completed_at, updated_at)
            VALUES (%s, 'succeeded', 0, 0, NOW(), NOW())
            ON CONFLICT (diagnosis_id) DO UPDATE SET
                status = EXCLUDED.status,
                token_cost_yuan = EXCLUDED.token_cost_yuan,
                completed_at = EXCLUDED.completed_at,
                updated_at = EXCLUDED.updated_at
            """,
            (diagnosis_id,),
        )
        conn.commit()
        try:
            from services.narrative_budget import add_monthly_budget_usage

            add_monthly_budget_usage(0)
        except Exception:
            pass
        return {"status": "succeeded", "diagnosis_id": diagnosis_id, "token_cost_yuan": 0}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO narrative_enrichment_locks
                    (diagnosis_id, status, retries, error_message, updated_at)
                VALUES (%s, 'failed_full', 1, %s, NOW())
                ON CONFLICT (diagnosis_id) DO UPDATE SET
                    status = 'failed_full',
                    retries = narrative_enrichment_locks.retries + 1,
                    error_message = EXCLUDED.error_message,
                    updated_at = NOW()
                """,
                (diagnosis_id, str(exc)[:500]),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.warning("[rich_narrative] enrichment failed diagnosis=%s err=%s", diagnosis_id, exc)
        return {"status": "failed_full", "diagnosis_id": diagnosis_id, "error": str(exc)[:500]}
    finally:
        conn.close()
