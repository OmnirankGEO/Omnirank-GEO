"""WP7 #4 · governance · owner-initiated portal-token rotation is audited.

Before: admin portal-token access was audited (require_portal_token_authority →
record_admin_read), but an owner / assigned member / legacy service-provider
rotating a customer-portal token via ``generate_client_token`` left **no audit
record**. This locks the close:

- when actor context is supplied, ``generate_client_token`` writes exactly one
  ``audit_logs`` row in the SAME transaction as the rotation (atomic — a rotated
  token can never exist without its audit), with request_id/reason in the WP2
  first-class columns and action ``portal.token.generate``;
- the audit records how many prior tokens were deactivated (rotation evidence);
- with no actor context (legacy callers) behaviour is unchanged — no audit, no
  error, token still issued.

Runs against TEMP tables shadowing the real ones — no fixtures, nothing persists.
"""
import os

import psycopg2
import pytest

from db import monitoring_db


# session-lived TEMP tables (no ON COMMIT DROP — the function under test commits;
# they vanish when the connection closes at teardown).
_DDL = """
CREATE TEMP TABLE client_access_tokens (
  id BIGSERIAL PRIMARY KEY, quote_id INTEGER, brand_name TEXT, token TEXT,
  expires_at TEXT, is_active INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TEMP TABLE audit_logs (
  id BIGSERIAL PRIMARY KEY, user_id INTEGER, username TEXT, action TEXT NOT NULL,
  module TEXT, entity_type TEXT, entity_id INTEGER, summary TEXT,
  before_snapshot TEXT, after_snapshot TEXT, ip_address TEXT,
  request_id TEXT, reason TEXT, created_at TIMESTAMP DEFAULT NOW()
);
"""


class _NoCloseProxy:
    """Delegates to the real connection but makes .close() a no-op so the
    function under test (which closes after commit) can't drop our fixture conn.
    RealDictCursor is preserved because .cursor() forwards to the real conn."""
    def __init__(self, real):
        self._real = real

    def close(self):  # swallow the inner close
        pass

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def conn(monkeypatch):
    from psycopg2.extras import RealDictCursor
    c = psycopg2.connect(os.environ["TEST_DATABASE_URL"], cursor_factory=RealDictCursor)
    c.autocommit = False
    cur = c.cursor()
    cur.execute(_DDL)
    c.commit()
    monkeypatch.setattr(monitoring_db, "get_connection", lambda: _NoCloseProxy(c))
    try:
        yield c
    finally:
        c.close()


def _audits(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, username, action, module, entity_type, entity_id, "
        "summary, before_snapshot, after_snapshot, request_id, reason "
        "FROM audit_logs ORDER BY id"
    )
    return [dict(r) for r in cur.fetchall()]  # RealDictCursor rows


def test_owner_rotation_writes_one_atomic_audit(conn):
    # seed one existing active token so the rotation deactivates it (evidence=1).
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO client_access_tokens (quote_id, brand_name, token, is_active) "
        "VALUES (55, 'ACME', 'OLDTOKEN', 1)"
    )
    result = monitoring_db.generate_client_token(
        55, "ACME", 30,
        actor_user_id=901, actor_username="owner_zhang",
        request_id="req-portal-xyz", ip_address="9.9.9.9",
        authority="commercial_owner", reason="客户要求更换分享链接",
    )
    rows = _audits(conn)
    assert len(rows) == 1, "exactly one rotation audit expected"
    a = rows[0]
    assert a["action"] == "portal.token.generate"
    assert a["module"] == "portal_token"
    assert a["entity_type"] == "customer_portal_credential"
    assert a["entity_id"] == 55
    assert a["user_id"] == 901 and a["username"] == "owner_zhang"
    # WP2 first-class columns populated
    assert a["request_id"] == "req-portal-xyz"
    assert a["reason"] == "客户要求更换分享链接"
    # rotation evidence: one prior token deactivated; new token id recorded
    assert '"previous_active_tokens": 1' in a["before_snapshot"]
    assert f'"new_token_id": {result["id"]}' in a["after_snapshot"]
    # old token actually deactivated (rotation happened)
    cur.execute("SELECT is_active FROM client_access_tokens WHERE token='OLDTOKEN'")
    assert cur.fetchone()["is_active"] == 0


def test_reason_defaults_when_not_supplied(conn):
    monitoring_db.generate_client_token(
        77, "BRAND", 30, actor_user_id=902, request_id="req-2",
    )
    rows = _audits(conn)
    assert len(rows) == 1
    assert rows[0]["reason"].strip()  # never null/empty (has a default)
    assert rows[0]["request_id"] == "req-2"


def test_no_actor_context_writes_no_audit_and_still_issues_token(conn):
    result = monitoring_db.generate_client_token(88, "LEGACY", 30)
    assert result["id"] and result["token"]
    assert _audits(conn) == []  # legacy behaviour unchanged
