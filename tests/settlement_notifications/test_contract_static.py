import re
from pathlib import Path

import psycopg2


ROOT = Path(__file__).resolve().parents[2]


def test_notification_event_key_migration_is_registered():
    migration = ROOT / "scripts" / "migration_notification_outbox_2026_07_17.sql"
    assert migration.exists()
    manifest = (ROOT / "db" / "migration_manifest.py").read_text(encoding="utf-8")
    assert migration.name in manifest


def test_transactional_notification_helper_and_public_allowlist_exist():
    source = (ROOT / "services" / "notification_outbox.py").read_text(encoding="utf-8")
    assert "def enqueue_notification_event(" in source
    assert "notification_outbox" in source
    team_source = (ROOT / "db" / "team_db.py").read_text(encoding="utf-8")
    list_block = team_source[
        team_source.index("def get_user_notifications("):
        team_source.index("def mark_user_notification_read(")
    ]
    assert "SELECT *" not in list_block
    assert "event_key" not in list_block.split("query =", 1)[1]
    assert "created_at, level, metadata" in list_block


def test_mobile_header_does_not_hide_notification_bell():
    source = (ROOT / "frontend" / "src" / "components" / "layout" / "Header.tsx").read_text(
        encoding="utf-8"
    )
    assert '<NotificationBell />' in source
    assert 'hidden sm:contents">\n          <NotificationBell />' not in source


def test_user_notification_api_does_not_return_http_200_error_envelopes():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    block = source[source.index('@app.get("/api/user/notifications")'):source.index("class SendNotificationRequest")]
    assert 'return {"status": "error"' not in block


def test_old_brand_zero_withdrawal_notification_is_removed():
    source = (ROOT / "db" / "withdrawal_db.py").read_text(encoding="utf-8")
    assert "brand_id=0" not in source
    assert "from db.notifications import create_notification" not in source


def test_old_brand_notification_writers_are_monitoring_compatibility_only():
    legacy_imports = []
    raw_writers = []
    monitoring_db_writers = []
    for folder in ("api", "db", "services", "writing"):
        for path in (ROOT / folder).rglob("*.py"):
            source = path.read_text(encoding="utf-8", errors="ignore")
            if "from db.notifications import create_notification" in source:
                legacy_imports.append(path.relative_to(ROOT).as_posix())
            if path.name != "notifications.py" and "INSERT INTO notifications" in source:
                raw_writers.append(path.relative_to(ROOT).as_posix())
            if "from db.monitoring_db import create_notification" in source:
                monitoring_db_writers.append(path.relative_to(ROOT).as_posix())
    assert legacy_imports == ["api/monitoring_api.py"]
    assert raw_writers == []
    assert monitoring_db_writers == []


def test_trial_and_subscription_permission_changes_use_typed_outbox():
    # [开源 E3 · B4 · 2026-09-28] 订阅那一半随订阅产品面删除(subscription_grace_period_check 整函数删,
    #   它是 SUBSCRIPTION_RENEWAL_FAILED 的唯一发射点;通知类型本身保留给通知中心渲染存量老通知)。只剩试用这一半。
    trial = (ROOT / "api" / "trial_pass_api.py").read_text(encoding="utf-8")
    for event in (
        "TRIAL_REVIEW_REQUIRED",
        "TRIAL_ACTIVATED",
        "TRIAL_REJECTED",
        "TRIAL_EXPIRED",
    ):
        assert f"NotificationEventType.{event}" in trial
    assert "INSERT INTO notifications" not in trial


def test_critical_manual_terminals_have_typed_outbox_hooks():
    fund = (ROOT / "db" / "fund_recovery_db.py").read_text(encoding="utf-8")
    dispute = (ROOT / "api" / "admin_w4_api.py").read_text(encoding="utf-8")
    publication = (ROOT / "db" / "meijiehezi_db.py").read_text(encoding="utf-8")
    assert "NotificationEventType.FUND_RECOVERY_MANUAL_REQUIRED" in fund
    assert "NotificationEventType.DISPUTE_RESOLVED" in dispute
    assert 'event_type="publication.manual_required"' in publication


def test_outbox_is_database_durable_when_redis_is_unavailable():
    source = (ROOT / "services" / "notification_outbox.py").read_text(encoding="utf-8")
    assert "redis" not in source.lower()
    assert "INSERT INTO notification_outbox" in source
    assert "cron_should_fire" in source


def test_every_catalog_route_exists_in_active_app_router():
    from services.notification_events import CATALOG

    app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    declared = set()
    for path in re.findall(r'<Route\s+path=["\']([^"\']+)["\']', app_source):
        declared.add(path if path.startswith("/") else f"/{path}")
    catalog_routes = {
        presentation.route
        for spec in CATALOG.values()
        for presentation in spec.presentations.values()
    }
    assert catalog_routes - declared == set()


def test_migration_transaction_rollback_then_forward_twice(notification_database_url):
    migration = (ROOT / "scripts" / "migration_notification_outbox_2026_07_17.sql").read_text(
        encoding="utf-8"
    )
    conn = psycopg2.connect(notification_database_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS notification_migration_roundtrip CASCADE")
        cur.execute("CREATE SCHEMA notification_migration_roundtrip")
        cur.execute("SET search_path TO notification_migration_roundtrip")
        cur.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
        cur.execute(
            """CREATE TABLE user_notifications(
                   id BIGSERIAL PRIMARY KEY,
                   user_id INTEGER NOT NULL REFERENCES users(id),
                   type TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,
                   link TEXT,is_read BOOLEAN NOT NULL DEFAULT FALSE,
                   created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
               )"""
        )
        cur.execute("BEGIN")
        cur.execute(migration)
        cur.execute("ROLLBACK")
        cur.execute(
            "SELECT to_regclass('notification_migration_roundtrip.notification_outbox')"
        )
        assert cur.fetchone()[0] is None
        cur.execute(migration)
        cur.execute(migration)
        cur.execute(
            "SELECT to_regclass('notification_migration_roundtrip.notification_outbox')"
        )
        assert cur.fetchone()[0] == "notification_outbox"
        cur.execute("DROP SCHEMA notification_migration_roundtrip CASCADE")
    conn.close()
