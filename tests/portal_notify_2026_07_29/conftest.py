"""判别锁夹具 —— 工单 2026-07-29(门户 token 续期 / 演示门户 / 通知中心)。

口径:**行为级**。所有断言都真调被测函数(renew_client_token / update_task_status /
save_report / get_notification_feed / demo transport 端点),不做源码字符串断言 ——
源码串一换皮就绕过去了(参见 [[feedback_expose_weakness_over_pretend_pass]] 教训)。

表结构按仓库里真实的 CREATE TABLE 形态建(列名/类型逐条对齐),
避免"夹具表跟真表不是一回事 → 锁全绿但生产照崩"。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras
import pytest

from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块

TEST_URL = os.environ["TEST_DATABASE_URL"]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT,
    is_active SMALLINT DEFAULT 1
);

CREATE TABLE IF NOT EXISTS roles (id SERIAL PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS user_roles (user_id INTEGER, role_id INTEGER);

-- brands: 见 _schema fixture 里的 ensure_brands_schema()（生产 SSOT 出口），不在这里手搓。

CREATE TABLE IF NOT EXISTS quotes (
    id SERIAL PRIMARY KEY,
    brand_id INTEGER,
    brand_name TEXT,
    status TEXT,
    tier TEXT,
    paid_at TIMESTAMP,
    confirmed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    service_start_date DATE,
    service_end_date DATE,
    service_months INTEGER,
    service_days INTEGER,
    organization_id BIGINT,
    created_by_membership_id BIGINT
);

CREATE TABLE IF NOT EXISTS client_access_tokens (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER NOT NULL,
    brand_name TEXT,
    token TEXT UNIQUE NOT NULL,
    is_active SMALLINT DEFAULT 1,
    expires_at DATE,
    last_access_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    module TEXT,
    entity_type TEXT,
    entity_id INTEGER,
    summary TEXT,
    before_snapshot TEXT,
    after_snapshot TEXT,
    ip_address TEXT,
    request_id TEXT,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monitoring_tasks (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER,
    client_id TEXT NOT NULL,
    brand_id INTEGER,
    task_name TEXT,
    keyword_count INTEGER DEFAULT 0,
    platform_count INTEGER DEFAULT 0,
    total_tests INTEGER DEFAULT 0,
    completed_tests INTEGER DEFAULT 0,
    concurrency INTEGER DEFAULT 10,
    test_rounds INTEGER DEFAULT 3,
    trigger_type TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    result_summary TEXT
);

CREATE TABLE IF NOT EXISTS monitoring_reports (
    id SERIAL PRIMARY KEY,
    client_id TEXT NOT NULL,
    brand_id INTEGER,
    report_type TEXT NOT NULL,
    period_start DATE,
    period_end DATE,
    summary_data TEXT,
    content TEXT,
    status TEXT DEFAULT 'draft',
    excel_path TEXT,
    pdf_path TEXT,
    version TEXT DEFAULT 'v1',
    evidence_count INTEGER DEFAULT 0,
    modules_jsonb JSONB,
    organization_id BIGINT,
    created_by_membership_id BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_notifications (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    type VARCHAR(40),
    title VARCHAR(255),
    content TEXT,
    link VARCHAR(255),
    is_read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    level VARCHAR(20) NOT NULL DEFAULT 'light',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_key TEXT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_notifications_user_event
    ON user_notifications(user_id, event_key) WHERE event_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS notification_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    business_id TEXT NOT NULL,
    terminal_state TEXT NOT NULL,
    recipient_user_id INTEGER NOT NULL,
    recipient_kind TEXT NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    route TEXT NOT NULL,
    privacy_policy TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    claimed_at TIMESTAMPTZ,
    claim_token TEXT,
    last_error TEXT,
    delivered_notification_id BIGINT,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS point_transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    point_type TEXT NOT NULL,
    amount BIGINT NOT NULL,
    balance_after BIGINT NOT NULL,
    feature_code TEXT,
    description TEXT,
    order_id TEXT,
    brand_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_notification_read_marks (
    user_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    last_read_ref BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, channel)
);
"""

TRUNCATE_TABLES = (
    "client_access_tokens", "audit_logs", "monitoring_tasks", "monitoring_reports",
    "user_notifications", "notification_outbox", "point_transactions",
    "user_notification_read_marks", "quotes", "brands", "users",
)


def _connect():
    conn = psycopg2.connect(TEST_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


@pytest.fixture(scope="session", autouse=True)
def _schema():
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    # [R5 ⑤ 2026-08-20] brands 走生产 SSOT 出口（手搓版 8 列 vs 生产 32 列）。
    # 排在 SCHEMA_SQL 之前：底下有表 REFERENCES brands(id)。
    ensure_brands_schema(cur)
    cur.execute(SCHEMA_SQL)
    conn.close()


@pytest.fixture
def db(_schema):
    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE " + ", ".join(TRUNCATE_TABLES) + " RESTART IDENTITY CASCADE")
    yield cur
    conn.close()


@pytest.fixture
def seed(db):
    """一个真实形态的客户:品牌 + 已付款 quote(服务期 180 天) + 有效门户 token。"""

    def _make(
        *, service_days: int = 180, started_days_ago: int = 29,
        token_expires_in: int = 1, token: str = "TOKENAAAA01", is_active: int = 1,
    ):
        db.execute("INSERT INTO users (username) VALUES ('agent-a') RETURNING id")
        user_id = db.fetchone()["id"]
        db.execute(
            "INSERT INTO brands (name, owner_user_id, status) VALUES ('岱林生物', %s, 'active') RETURNING id",
            (user_id,),
        )
        brand_id = db.fetchone()["id"]
        start = date.today() - timedelta(days=started_days_ago)
        # [服务期 SSOT 2026-08-06] 服务期从此是 (service_start_date, service_end_date) 一对
        #   显式列,不再由 `service_start_date + service_days` 推。旧 seed 只写起始日 + 达标
        #   天数配额,在新口径下等于"这单没有服务期" → 门户 token 一律不续期(fail-closed),
        #   T1 七条锁会整片红。这里补上真实的服务期终点,锁守的语义一条没变。
        service_end = start + timedelta(days=service_days)
        db.execute(
            """INSERT INTO quotes (brand_id, brand_name, status, paid_at,
                                   service_start_date, service_end_date, service_days)
               VALUES (%s, '岱林生物', 'paid', %s, %s, %s, %s) RETURNING id""",
            (brand_id, datetime.now() - timedelta(days=started_days_ago), start, service_end, service_days),
        )
        quote_id = db.fetchone()["id"]
        db.execute(
            """INSERT INTO client_access_tokens (quote_id, brand_name, token, is_active, expires_at)
               VALUES (%s, '岱林生物', %s, %s, %s) RETURNING id""",
            (quote_id, token, is_active, date.today() + timedelta(days=token_expires_in)),
        )
        token_id = db.fetchone()["id"]
        return {
            "user_id": user_id, "brand_id": brand_id, "quote_id": quote_id,
            "token_id": token_id, "token": token, "service_start": start,
            "service_days": service_days,
        }

    return _make
