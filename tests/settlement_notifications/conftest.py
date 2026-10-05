import os
from pathlib import Path

import psycopg2
import pytest

from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块


ROOT = Path(__file__).resolve().parents[2]


def _test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL", "")
    lowered = url.lower()
    if not url or "test" not in lowered or not any(host in lowered for host in ("127.0.0.1", "localhost")):
        pytest.fail("settlement notification tests require a local throwaway TEST_DATABASE_URL")
    return url


@pytest.fixture(scope="session")
def notification_database_url() -> str:
    url = _test_database_url()
    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        # [R5 ⑤ 2026-08-20] brands 走生产 SSOT 出口(手搓版只有 2 列 id/owner_user_id,
        #   生产 32 列)。必须排在下面那一大块之前:monitoring_tasks REFERENCES brands(id)。
        #   🔴 顺带丢掉的是手搓版那条 `owner_user_id NOT NULL REFERENCES users(id)` ——
        #      生产实查 brands.owner_user_id 是 **integer / nullable / 无 FK**
        #      (tests/article_self_report_2026_08_19/prod_schema_2026-08-19.sql),
        #      即手搓版比生产**严**。夹具比生产严 = 另一种假绿(测到的约束生产没有)。
        ensure_brands_schema(cur)
        cur.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                password_hash TEXT,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                permission_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE user_roles (
                user_id INTEGER NOT NULL REFERENCES users(id),
                role_id INTEGER NOT NULL REFERENCES roles(id),
                PRIMARY KEY (user_id, role_id)
            );
            CREATE TABLE user_notifications (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                type VARCHAR(50) NOT NULL DEFAULT 'system',
                title VARCHAR(255) NOT NULL,
                content TEXT NOT NULL,
                link VARCHAR(500),
                is_read BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );
            CREATE TABLE recharge_orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                amount_cents INTEGER NOT NULL,
                base_points BIGINT NOT NULL,
                bonus_points BIGINT NOT NULL DEFAULT 0,
                payment_status TEXT NOT NULL,
                payment_id TEXT,
                paid_at TIMESTAMPTZ,
                order_type TEXT,
                settlement_mode TEXT
            );
            CREATE TABLE agent_settlement_requests (
                id SERIAL PRIMARY KEY,
                agent_user_id INTEGER NOT NULL REFERENCES users(id),
                request_amount_cents INTEGER NOT NULL,
                bank_name TEXT,
                bank_account TEXT,
                account_holder TEXT,
                invoice_required BOOLEAN NOT NULL DEFAULT FALSE,
                status TEXT NOT NULL DEFAULT 'pending',
                gateway_fee_cents INTEGER NOT NULL DEFAULT 0,
                settlement_fee_cents INTEGER NOT NULL DEFAULT 0,
                tax_cents INTEGER NOT NULL DEFAULT 0,
                net_amount_cents INTEGER NOT NULL DEFAULT 0,
                approved_by_user_id INTEGER,
                approved_at TIMESTAMPTZ,
                rejected_by_user_id INTEGER,
                rejected_at TIMESTAMPTZ,
                reject_reason TEXT,
                paid_by_admin_user_id INTEGER,
                paid_at TIMESTAMPTZ,
                transfer_proof_url TEXT,
                wire_transfer_no TEXT,
                admin_note TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );
            CREATE TABLE user_wallets (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                paid_points BIGINT NOT NULL DEFAULT 0,
                bonus_points BIGINT NOT NULL DEFAULT 0,
                commission_points BIGINT NOT NULL DEFAULT 0,
                frozen_points BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );
            CREATE TABLE bank_cards (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE withdrawal_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                bank_card_id INTEGER NOT NULL REFERENCES bank_cards(id),
                amount_yuan NUMERIC(10,2) NOT NULL,
                fee_yuan NUMERIC(10,2) NOT NULL,
                actual_yuan NUMERIC(10,2) NOT NULL,
                points_deducted BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reject_reason TEXT,
                reviewed_by INTEGER,
                reviewed_at TIMESTAMPTZ,
                paid_at TIMESTAMPTZ,
                external_tx_ref TEXT,
                idempotency_key UUID NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );
            CREATE TABLE point_transactions (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                type TEXT NOT NULL,
                point_type TEXT NOT NULL,
                amount BIGINT NOT NULL,
                balance_after BIGINT NOT NULL,
                feature_code TEXT,
                description TEXT,
                order_id TEXT,
                brand_id INTEGER,
                source TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );
            -- brands: 已改由 ensure_brands_schema()（生产 SSOT 出口）在本块之前建。
            CREATE TABLE monitoring_tasks (
                id SERIAL PRIMARY KEY,
                brand_id INTEGER REFERENCES brands(id),
                status TEXT NOT NULL DEFAULT 'pending',
                total_tests INTEGER NOT NULL DEFAULT 0,
                completed_tests INTEGER NOT NULL DEFAULT 0,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                result_summary JSONB,
                trigger_type TEXT NOT NULL DEFAULT 'manual'
            );
            CREATE TABLE monitoring_reports (
                id SERIAL PRIMARY KEY,
                client_id TEXT,
                brand_id INTEGER REFERENCES brands(id),
                report_type TEXT,
                period_start TEXT,
                period_end TEXT,
                summary_data JSONB,
                content TEXT,
                status TEXT,
                excel_path TEXT,
                pdf_path TEXT,
                version TEXT,
                evidence_count INTEGER,
                modules_jsonb JSONB
            );
            CREATE TABLE marketing_material_jobs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'pending',
                freeze_id TEXT,
                billing_ref TEXT,
                final_prompt TEXT,
                block_reason TEXT,
                error_summary TEXT,
                cost_points INTEGER NOT NULL DEFAULT 0,
                finished_at TIMESTAMPTZ
            );
            CREATE TABLE geo_research_round (
                round_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                finished_at TIMESTAMPTZ,
                summary_json JSONB NOT NULL DEFAULT '{}'::jsonb
            );
            CREATE TABLE ai_ops_tasks (
                id SERIAL PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                summary TEXT,
                result_jsonb JSONB,
                worktree_path TEXT,
                codex_session_id TEXT,
                assigned_worker_id TEXT,
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );
            CREATE TABLE publish_batches (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'processing'
            );
            CREATE TABLE publish_orders (
                id SERIAL PRIMARY KEY,
                batch_id TEXT REFERENCES publish_batches(id),
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE trial_passes (
                id SERIAL PRIMARY KEY,
                recipient_user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL,
                expires_at TIMESTAMPTZ,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );
            CREATE TABLE user_social_subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'active',
                grace_period_until TIMESTAMPTZ,
                last_renewal_failed_reason TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
            );
            INSERT INTO users(id) VALUES (10),(20),(90),(91);
            INSERT INTO roles(id,name) VALUES (1,'admin');
            INSERT INTO user_roles(user_id,role_id) VALUES (90,1),(91,1);
            INSERT INTO user_wallets(user_id,paid_points,commission_points) VALUES (10,5000,1000000);
            -- [R5 ⑤ 2026-08-20] 补 name:生产 brands.name 是 **NOT NULL**,
            --   手搓夹具只有 (id, owner_user_id) 两列所以从来没暴露过 —— 这一行
            --   原样打到生产会被拒。夹具比生产窄的代价,这就是一例。
            INSERT INTO brands(id,owner_user_id,name) VALUES (1,10,'结算通知测试品牌');
            INSERT INTO bank_cards(id,user_id) VALUES (1,10);
            """
        )
        migration = (ROOT / "scripts" / "migration_notification_outbox_2026_07_17.sql").read_text(
            encoding="utf-8"
        )
        cur.execute(migration)
        cur.execute(migration)
    conn.close()
    yield url


@pytest.fixture()
def notification_db(notification_database_url: str, monkeypatch):
    import db.connection as connection

    connection.close_pool()
    conn = psycopg2.connect(notification_database_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """TRUNCATE notification_outbox, user_notifications, recharge_orders,
                        agent_settlement_requests, withdrawal_requests,
                        point_transactions, monitoring_tasks, monitoring_reports,
                        marketing_material_jobs, geo_research_round, ai_ops_tasks,
                        publish_orders, publish_batches, trial_passes,
                        user_social_subscriptions RESTART IDENTITY CASCADE;
               UPDATE user_wallets SET paid_points=5000,bonus_points=0,
                       commission_points=1000000,frozen_points=0;
               UPDATE users SET permission_version=1,must_change_password=0,password_hash=NULL;
               DELETE FROM bank_cards;
               INSERT INTO bank_cards(id,user_id) VALUES (1,10)"""
        )
    conn.close()

    monkeypatch.setenv("DATABASE_URL", notification_database_url)
    monkeypatch.setattr(connection, "DATABASE_URL", notification_database_url)
    yield notification_database_url
    connection.close_pool()
