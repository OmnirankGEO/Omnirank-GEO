"""WP2 · governance · pricing audit request_id / reason are first-class columns.

Before: ``insert_pricing_audit`` buried request_id inside the before/after JSON
blobs and the summary text; there was no queryable column, unlike the reference
``admin_user_governance_audits`` table which carries request_id + reason as
first-class columns. This locks that WP2 promotion:

- request_id and reason are written to real ``audit_logs`` columns (SELECTable);
- reason falls back to summary when not given (column never null for pricing);
- an explicit reason overrides;
- the JSON snapshots / summary remain intact (backward compatible readers).

Runs against a TEMP ``audit_logs`` table that shadows the real one, so it needs
no fixture data and touches nothing persistent.
"""
import os

import psycopg2
import pytest

from services.agent_inventory_pricing import insert_pricing_audit

_TEMP_DDL = """
CREATE TEMP TABLE audit_logs (
  id BIGSERIAL PRIMARY KEY, user_id INTEGER, username TEXT, action TEXT NOT NULL,
  module TEXT, entity_type TEXT, entity_id INTEGER, summary TEXT,
  before_snapshot TEXT, after_snapshot TEXT, ip_address TEXT,
  request_id TEXT, reason TEXT,
  created_at TIMESTAMP DEFAULT NOW()
) ON COMMIT DROP;
"""


@pytest.fixture()
def cur():
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    try:
        c = conn.cursor()
        c.execute(_TEMP_DDL)
        yield c
        conn.rollback()  # ON COMMIT DROP temp table; never persist
    finally:
        conn.close()


def _insert(cur, **over):
    kwargs = dict(
        admin_user_id=7, admin_username="root", request_id="req-abc-123",
        module="inventory_pricing", summary="下调批发系数",
        before_config={"k": 1}, after_config={"k": 2},
        previous_catalog_version="v9", catalog_version="v10", ip_address="1.2.3.4",
    )
    kwargs.update(over)
    insert_pricing_audit(cur, **kwargs)
    cur.execute("SELECT request_id, reason, summary, before_snapshot, after_snapshot FROM audit_logs ORDER BY id DESC LIMIT 1")
    return cur.fetchone()


def test_request_id_and_reason_are_first_class_columns(cur):
    request_id, reason, summary, before, after = _insert(cur)
    # first-class columns are populated (not just JSON blobs)
    assert request_id == "req-abc-123"
    assert reason == "下调批发系数"  # fell back to summary
    # backward-compatible payload preserved
    assert "req-abc-123" in summary
    assert '"request_id": "req-abc-123"' in before
    assert '"request_id": "req-abc-123"' in after


def test_explicit_reason_overrides_summary(cur):
    request_id, reason, summary, _b, _a = _insert(
        cur, reason="合规调价：季度成本上浮", summary="更新价目表"
    )
    assert reason == "合规调价：季度成本上浮"
    assert summary.startswith("更新价目表")  # summary text unchanged


def test_request_id_column_is_queryable(cur):
    _insert(cur, request_id="req-query-me")
    cur.execute("SELECT count(*) FROM audit_logs WHERE request_id = %s", ("req-query-me",))
    assert cur.fetchone()[0] == 1
