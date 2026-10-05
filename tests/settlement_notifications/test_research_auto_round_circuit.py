from datetime import datetime
from unittest.mock import patch

import psycopg2


def _ensure_gate(url: str, enabled: bool = True) -> datetime:
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS geo_research_config (
                    key TEXT PRIMARY KEY,
                    value_json JSONB NOT NULL,
                    updated_by TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                INSERT INTO geo_research_config(key,value_json,updated_by,updated_at)
                VALUES ('cron_enabled', %s::jsonb, 'test', NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value_json=EXCLUDED.value_json,
                        updated_by=EXCLUDED.updated_by,
                        updated_at=GREATEST(
                            clock_timestamp(),
                            geo_research_config.updated_at + INTERVAL '1 microsecond'
                        )
                RETURNING updated_at
                """,
                ('true' if enabled else 'false',),
            )
            token = cur.fetchone()[0]
        conn.commit()
        return token
    finally:
        conn.close()


def test_circuit_pause_and_admin_alert_commit_atomically(notification_db):
    from services.research_monitor import scheduler_setup

    token = _ensure_gate(notification_db)
    with patch.object(scheduler_setup, '_remove_auto_round_jobs_best_effort') as remove_jobs:
        result = scheduler_setup._trip_auto_round_circuit(
            round_id='round_auto_failed_1',
            status='failed_resumable',
            reason='stage3 timeout',
            expected_gate_updated_at=token,
        )

    assert result['paused'] is True
    assert result['changed'] is True
    remove_jobs.assert_called_once()

    conn = psycopg2.connect(notification_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value_json,updated_by FROM geo_research_config "
                "WHERE key='cron_enabled'"
            )
            assert cur.fetchone() == (False, 'auto_round_circuit')
            cur.execute(
                """
                SELECT recipient_user_id,event_type,business_id,terminal_state,status
                  FROM notification_outbox
                 WHERE business_id='auto-circuit:round_auto_failed_1'
                 ORDER BY recipient_user_id
                """
            )
            rows = cur.fetchall()
            assert rows == [
                (90, 'research.manual_required', 'auto-circuit:round_auto_failed_1',
                 'paused:failed_resumable', 'pending'),
                (91, 'research.manual_required', 'auto-circuit:round_auto_failed_1',
                 'paused:failed_resumable', 'pending'),
            ]
    finally:
        conn.close()


def test_stale_gate_token_cannot_pause_new_admin_rearm(notification_db):
    from services.research_monitor import scheduler_setup

    old_token = _ensure_gate(notification_db)
    new_token = _ensure_gate(notification_db)
    assert new_token >= old_token

    with patch.object(scheduler_setup, '_remove_auto_round_jobs_best_effort') as remove_jobs:
        result = scheduler_setup._trip_auto_round_circuit(
            round_id='round_old_failed',
            status='failed',
            reason='old result arrived late',
            expected_gate_updated_at=old_token,
        )

    assert result['stale_gate'] is True
    remove_jobs.assert_not_called()
    conn = psycopg2.connect(notification_db)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value_json FROM geo_research_config WHERE key='cron_enabled'")
            assert cur.fetchone()[0] is True
            cur.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE business_id='auto-circuit:round_old_failed'"
            )
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()
