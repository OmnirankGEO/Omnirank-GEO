import psycopg2


def test_trial_expiry_commits_permission_terminal_and_outbox_together(notification_db):
    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO trial_passes(recipient_user_id,status,expires_at)
               VALUES (10,'active',clock_timestamp() - interval '1 minute') RETURNING id"""
        )
        trial_id = cur.fetchone()[0]
    conn.commit()
    conn.close()

    from api.trial_pass_api import expire_active_trials

    assert expire_active_trials() == 1
    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM trial_passes WHERE id=%s", (trial_id,))
        assert cur.fetchone()[0] == "expired"
        cur.execute(
            """SELECT event_type,terminal_state,recipient_user_id
               FROM notification_outbox WHERE business_id=%s""",
            (str(trial_id),),
        )
        assert cur.fetchone() == ("account.trial_expired", "expired", 10)
    conn.close()

