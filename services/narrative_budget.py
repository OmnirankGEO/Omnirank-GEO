"""Budget helpers for report v3 narrative enrichment."""

from __future__ import annotations

from datetime import datetime

from db.connection import get_connection


def current_month_key() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def get_monthly_budget_snapshot(month_key: str | None = None) -> dict:
    key = month_key or current_month_key()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT month_key, total_yuan, enrichment_count, last_updated_at
            FROM narrative_monthly_budget
            WHERE month_key = %s
            """,
            (key,),
        )
        row = cursor.fetchone()
        if not row:
            return {"month_key": key, "total_yuan": 0.0, "enrichment_count": 0}
        return dict(row)
    finally:
        conn.close()


def add_monthly_budget_usage(amount_yuan: float, month_key: str | None = None) -> None:
    key = month_key or current_month_key()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO narrative_monthly_budget (month_key, total_yuan, enrichment_count, last_updated_at)
            VALUES (%s, %s, 1, NOW())
            ON CONFLICT (month_key) DO UPDATE SET
                total_yuan = narrative_monthly_budget.total_yuan + EXCLUDED.total_yuan,
                enrichment_count = narrative_monthly_budget.enrichment_count + 1,
                last_updated_at = NOW()
            """,
            (key, amount_yuan),
        )
        conn.commit()
    finally:
        conn.close()
