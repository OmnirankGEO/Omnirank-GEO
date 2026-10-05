"""Production-equivalent PostgreSQL behavior lock for FAQ feedback listing.

Run only against an explicitly supplied throwaway database.  The fixture deliberately
creates ``users`` without ``is_admin`` so the production regression is reproducible.
"""
from __future__ import annotations

import os

import psycopg2
import psycopg2.extras
import pytest


DATABASE_URL = os.getenv("QA_FAQ_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="QA_FAQ_DATABASE_URL must point to a disposable PostgreSQL database",
)


@pytest.fixture()
def faq_schema(monkeypatch):
    assert DATABASE_URL and (
        "localhost" in DATABASE_URL or "127.0.0.1" in DATABASE_URL
    ), "QA_FAQ_DATABASE_URL must be an explicitly local throwaway database"
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS faq_nogo_test CASCADE")
        cur.execute("CREATE SCHEMA faq_nogo_test")
        cur.execute("SET search_path TO faq_nogo_test")
        cur.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT)")
        cur.execute("CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
        cur.execute("CREATE TABLE user_roles (user_id INTEGER, role_id INTEGER, PRIMARY KEY(user_id, role_id))")
        cur.execute("CREATE TABLE user_wallets (user_id INTEGER PRIMARY KEY, agent_level INTEGER NOT NULL DEFAULT 0)")
        cur.execute("CREATE TABLE faq_items (id INTEGER PRIMARY KEY, question TEXT)")
        cur.execute(
            """CREATE TABLE faq_feedback (
                id INTEGER PRIMARY KEY, client_id TEXT, faq_id INTEGER, message TEXT,
                urgency TEXT, contact TEXT, kind TEXT, screenshot_url TEXT, ai_answer TEXT,
                submitter_identity TEXT NOT NULL DEFAULT '', submitter_agent_level INTEGER NOT NULL DEFAULT 0,
                user_id INTEGER, status TEXT, admin_note TEXT, created_at TIMESTAMP,
                handled_at TIMESTAMP, handled_by INTEGER
            )"""
        )
        cur.execute("INSERT INTO roles VALUES (1, 'admin'), (2, 'user')")
        cur.execute("INSERT INTO users VALUES (1, 'admin-a', '管理员'), (2, 'agent-a', '服务商'), (3, 'normal-a', '普通职员')")
        cur.execute("INSERT INTO user_roles VALUES (1, 1), (1, 2), (2, 2), (3, 2)")
        cur.execute("INSERT INTO user_wallets VALUES (2, 2), (3, 0)")
        cur.execute("INSERT INTO faq_items VALUES (10, '如何处理问题')")
        cur.execute(
            """INSERT INTO faq_feedback
               (id, client_id, faq_id, message, urgency, kind, user_id, status, created_at)
               VALUES
               (101, 'a', 10, 'admin feedback', 'high', 'bug', 1, 'pending', NOW()),
               (102, 'b', 10, 'agent feedback', 'mid', 'bug', 2, 'read', NOW() - INTERVAL '1 second'),
               (103, 'c', NULL, 'normal feedback', 'low', 'faq', 3, 'done', NOW() - INTERVAL '2 seconds'),
               (104, 'd', NULL, 'missing user', 'low', 'bug', 999, 'closed', NOW() - INTERVAL '3 seconds')"""
        )

    class SearchPathConnection:
        def __init__(self):
            self._conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
            with self._conn.cursor() as cursor:
                cursor.execute("SET search_path TO faq_nogo_test")

        def cursor(self):
            return self._conn.cursor()

        def close(self):
            self._conn.close()

    import db.faq_db as faq_db
    import db.connection as db_connection

    monkeypatch.setattr(faq_db, "_get_conn", SearchPathConnection)
    monkeypatch.setattr(db_connection, "get_connection", SearchPathConnection)
    try:
        yield faq_db
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS faq_nogo_test CASCADE")
        conn.close()


def test_list_feedback_uses_rbac_without_users_is_admin_and_does_not_duplicate(faq_schema):
    items = faq_schema.list_feedback(limit=100)
    assert [item["id"] for item in items] == [101, 102, 103, 104]
    assert len({item["id"] for item in items}) == 4
    identities = {item["id"]: item["submitter_identity"] for item in items}
    assert identities == {101: "admin", 102: "l2", 103: "normal_user", 104: "normal_user"}


@pytest.mark.parametrize(
    ("filters", "expected_ids"),
    [
        ({"status": "pending"}, [101]),
        ({"urgency": "mid"}, [102]),
        ({"kind": "faq"}, [103]),
        ({"status": "read", "urgency": "mid", "kind": "bug"}, [102]),
        ({"limit": 2}, [101, 102]),
    ],
)
def test_list_feedback_filter_combinations(faq_schema, filters, expected_ids):
    assert [item["id"] for item in faq_schema.list_feedback(**filters)] == expected_ids


def test_counts_share_kind_and_urgency_filters_with_list(faq_schema):
    assert faq_schema.get_feedback_counts(kind="bug", urgency="high") == {
        "pending": 1,
        "read": 0,
        "done": 0,
        "closed": 0,
    }


def test_admin_feedback_http_endpoint_returns_200_without_users_is_admin(faq_schema):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    import api.faq_api as faq_api

    app = FastAPI()

    @app.middleware("http")
    async def inject_admin(request: Request, call_next):
        request.state.user = {"id": 1, "is_admin": True, "agent_level": 0}
        return await call_next(request)

    app.include_router(faq_api.router)
    response = TestClient(app).get(
        "/api/admin/faq/feedback",
        params={"status": "pending", "urgency": "high", "kind": "bug", "limit": 10},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [101]


def test_bug_feedback_notification_targets_real_admin_role_without_users_is_admin(faq_schema, monkeypatch):
    import api.faq_api as faq_api
    import db.team_db as team_db

    notifications = []

    def capture_notification(user_id, **kwargs):
        notifications.append((user_id, kwargs))

    monkeypatch.setattr(team_db, "create_user_notification", capture_notification)
    faq_api._notify_admins_for_bug(101, "额度显示异常", "high", 3)

    assert [item[0] for item in notifications] == [1]
    assert notifications[0][1]["metadata"] == {
        "feedback_id": 101,
        "user_id": 3,
        "kind": "bug",
    }
