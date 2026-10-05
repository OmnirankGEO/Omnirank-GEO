"""[v4 req4 · Deploy-CTO 2026-07-13] NO-GO 回归测试的可复现 schema bootstrap。

问题:此前 nogo DB 行为测试需要手工 docker exec + 一堆 init/ALTER/constraint 才能跑,别人复现不了。
方案:session 级 autouse fixture 幂等地把 throwaway PG 的完整 schema 填齐(users 必填列 + 当前 schema),
      让 `pytest tests/regression` 在 TEST_DATABASE_URL 指向空 throwaway 库时【自动 provisioning】。

完整从零 throwaway PG 启动命令另见 scripts/test_bootstrap_throwaway_pg.sh(docker run + 本 fixture 同款 schema)。
无 TEST_DATABASE_URL 时静默跳过(DB 测试各自 skip)。

[v5 req5 2026-07-13] bootstrap 会 CREATE/ALTER 表,若误指向生产库=灾难。故:
  - 只认 TEST_DATABASE_URL(禁回退 DATABASE_URL);
  - 且必须是"本机 + 库名含 test/throwaway"的安全库(_dbsafe.resolve_test_db_url 校验,不安全直接 raise)。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 让 _dbsafe 可导入
from _dbsafe import resolve_test_db_url  # noqa: E402


_CUSTOMER_CREDIT_DDL = """
CREATE TABLE IF NOT EXISTS customer_agent_credit_wallets (
    customer_user_id INTEGER PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    tool_credit_points INTEGER NOT NULL DEFAULT 0,
    publish_credit_points INTEGER NOT NULL DEFAULT 0,
    bonus_credit_points INTEGER NOT NULL DEFAULT 0,
    total_purchased_points BIGINT NOT NULL DEFAULT 0,
    total_consumed_points BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW(),
    CHECK (tool_credit_points >= 0),
    CHECK (publish_credit_points >= 0),
    CHECK (bonus_credit_points >= 0)
);
CREATE TABLE IF NOT EXISTS customer_credit_transactions (
    id BIGSERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL,
    agent_user_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('allocate','consume','refund','revoke')),
    pool TEXT NOT NULL CHECK (pool IN ('tool','publish','bonus')),
    points INTEGER NOT NULL,
    balance_tool_after INTEGER NOT NULL,
    balance_publish_after INTEGER NOT NULL,
    balance_bonus_after INTEGER NOT NULL,
    feature_code TEXT,
    related_order_id TEXT,
    source TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# customer_credit_transactions.source 白名单 = prod 现值(7 值 · migration_v35_v7 + agent_rebate)
_SOURCE_CHECK_7 = """
ALTER TABLE customer_credit_transactions DROP CONSTRAINT IF EXISTS customer_credit_transactions_source_check;
ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_source_check
  CHECK (source = ANY (ARRAY['online_payment','offline_allocation','admin_adjust','tool_consume',
                             'refund_revoke','tool_fail_refund','agent_rebate']));
"""

_GEO_PLAN_DDL = """
CREATE TABLE IF NOT EXISTS geo_plan_tasks (
    id                BIGSERIAL PRIMARY KEY,
    user_id           INTEGER     NOT NULL,
    brand_id          INTEGER     NOT NULL,
    status            VARCHAR(24) NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','running','settling','settlement_pending','refund_pending','settle_conflict',
                                        'done','failed','cancelled','timeout')),
    params_json       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    brand_snapshot    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    progress_stage    VARCHAR(32),
    progress_percent  SMALLINT    NOT NULL DEFAULT 0 CHECK (progress_percent BETWEEN 0 AND 100),
    progress_message  TEXT,
    result_json       JSONB,
    error_code        VARCHAR(32),
    error_detail      TEXT,
    queued_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ,
    heartbeat_at      TIMESTAMPTZ,
    done_at           TIMESTAMPTZ,
    data_mode         VARCHAR(16) NOT NULL DEFAULT 'full'
                      CHECK (data_mode IN ('full','l1l2_fallback')),
    source            VARCHAR(32) NOT NULL,
    ip_at_start       VARCHAR(64),
    linked_quote_id   INTEGER,
    archived_at       TIMESTAMPTZ,
    freeze_id         INTEGER,
    freeze_table      VARCHAR(40),
    pending_terminal   VARCHAR(16),
    settle_retry_count SMALLINT NOT NULL DEFAULT 0,
    settle_last_error  TEXT
);
"""

# [v5] 兼容旧 throwaway 库(上一版 geo_plan_tasks 已存在)· 幂等 ALTER 到 v5 schema
_GEO_PLAN_V5_ALTER = """
ALTER TABLE geo_plan_tasks ALTER COLUMN status TYPE VARCHAR(24);
ALTER TABLE geo_plan_tasks DROP CONSTRAINT IF EXISTS geo_plan_tasks_status_check;
ALTER TABLE geo_plan_tasks ADD CONSTRAINT geo_plan_tasks_status_check
  CHECK (status IN ('queued','running','settling','settlement_pending','refund_pending','settle_conflict',
                    'done','failed','cancelled','timeout'));
ALTER TABLE geo_plan_tasks ADD COLUMN IF NOT EXISTS pending_terminal    VARCHAR(16);
ALTER TABLE geo_plan_tasks ADD COLUMN IF NOT EXISTS settle_retry_count  SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE geo_plan_tasks ADD COLUMN IF NOT EXISTS settle_last_error   TEXT;
"""


def _bootstrap(url: str) -> None:
    import psycopg2
    os.environ["DATABASE_URL"] = url  # 让 app init 函数指向测试库

    def _exec(sql: str):
        c = psycopg2.connect(url); c.autocommit = True
        try:
            c.cursor().execute(sql)
        finally:
            c.close()

    # 1. 基础 stub(先于 app init / migration · 满足 FK 与 migration guard/insert 依赖)
    _exec("""
        CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT,
            is_admin BOOLEAN DEFAULT FALSE, permission_version INT DEFAULT 0, is_active INT DEFAULT 1);
        CREATE TABLE IF NOT EXISTS user_clients (user_id INT, brand_id INT, PRIMARY KEY(user_id, brand_id));
        CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT, description TEXT);
        CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT NOW());
        -- diagnosis_db adds article lineage columns before creating its dependent
        -- review-event tables. Production already has this table from the legacy
        -- schema; a truly fresh regression database needs the same prerequisite.
        CREATE TABLE IF NOT EXISTS articles (id BIGSERIAL PRIMARY KEY);
    """)

    # 2. app init 函数(幂等 · IF NOT EXISTS)· 各自失败不阻断(seed admin 可能因 stub 缺列失败,表已建)
    for fn_path in (
        ("db.auth_db", "init_auth_db"),        # roles / role_permissions / user_roles
        ("db.wallet_db", "init_wallet_tables"),  # user_wallets / point_transactions / feature_pricing
        ("db.diagnosis_db", "init_db"),          # diagnosis_records / brands / article_generations / geo_research_raw
        ("db.research_answer_entity_db", "init_research_answer_entity_tables"),  # answer facts/entities
    ):
        try:
            mod = __import__(fn_path[0], fromlist=[fn_path[1]])
            getattr(mod, fn_path[1])()
        except Exception as e:  # noqa: BLE001
            print(f"[nogo bootstrap] {fn_path[1]} 部分失败(可忽略,表通常已建): {e}")

    # 3. 内联 DDL(customer_credit + geo_plan_tasks · 原 SQL 迁移带 DO 块,内联避免文件解析)
    _exec(_CUSTOMER_CREDIT_DDL)
    _exec(_GEO_PLAN_DDL)
    _exec(_GEO_PLAN_V5_ALTER)   # [v5] 旧库幂等升级到 settling/*_pending 9 态 + 补偿列

    # Provider retail cost resolution is fail-closed on the canonical channel
    # graph.  Regression fixtures must model that production prerequisite instead
    # of accidentally testing a pre-channel schema and expecting a flat fallback.
    _exec("""
        CREATE TABLE IF NOT EXISTS channel_pricing_relationships (
            id BIGSERIAL PRIMARY KEY,
            buyer_dealer_id INTEGER NOT NULL,
            upstream_channel_account_id INTEGER NOT NULL,
            relationship_version TEXT NOT NULL,
            cost_multiplier_bps INTEGER NOT NULL DEFAULT 10000,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','archived')),
            effective_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            effective_to TIMESTAMPTZ,
            reason TEXT,
            approved_by INTEGER,
            created_by INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archived_at TIMESTAMPTZ,
            CHECK (buyer_dealer_id <> upstream_channel_account_id),
            CHECK (cost_multiplier_bps >= 10000)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_channel_rel_active
            ON channel_pricing_relationships(buyer_dealer_id)
            WHERE status='active' AND effective_to IS NULL;
        CREATE INDEX IF NOT EXISTS idx_channel_rel_upstream
            ON channel_pricing_relationships(upstream_channel_account_id);
        CREATE INDEX IF NOT EXISTS idx_channel_rel_buyer
            ON channel_pricing_relationships(buyer_dealer_id,status);
    """)

    # 3.1 全站通知 SSOT。状态机现在会在终态事务内写 notification_outbox，
    # 回归库必须复刻 prestart 已完成的 user_notifications + outbox schema，
    # 否则测试的是一个生产不可能存在的“代码已升级、迁移未运行”混合态。
    _exec("""
        CREATE TABLE IF NOT EXISTS user_notifications (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            type VARCHAR(30) NOT NULL,
            title VARCHAR(200),
            content TEXT,
            link VARCHAR(300),
            is_read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    _exec((Path(__file__).resolve().parents[2] / "scripts" /
           "migration_notification_outbox_2026_07_17.sql").read_text(encoding="utf-8"))

    # Billing now writes the per-charge debt-offset outbox in the same wallet
    # transaction.  A regression database without this production migration is
    # an impossible mixed-version state and makes otherwise valid refund tests
    # fail before exercising their assertions.
    _exec((Path(__file__).resolve().parents[2] / "scripts" /
           "migration_billing_deduction_idempotency_2026_07_19.sql").read_text(encoding="utf-8"))

    # 4. 当前 schema 期望的 ALTER + 约束对齐 prod
    _exec("ALTER TABLE point_transactions ADD COLUMN IF NOT EXISTS source TEXT;")
    _exec("ALTER TABLE brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;")
    _exec(_SOURCE_CHECK_7)


@pytest.fixture(scope="session", autouse=True)
def nogo_schema_bootstrap():
    """session 级:把 throwaway 库 schema 填齐(幂等)。

    [v5 req5] 只对 TEST_DATABASE_URL(禁回退 DATABASE_URL)且安全(本机+test/throwaway 库名)的库跑;
    resolve_test_db_url 在 TEST_DATABASE_URL 指向非本机/非 test 库时直接 raise(拒绝 CREATE/ALTER 打生产)。
    未设 TEST_DATABASE_URL → None → 跳过(DB 测试各自 skipif)。
    """
    url = resolve_test_db_url()  # None(未设)或安全库 URL;不安全直接 raise
    if url:
        _bootstrap(url)
    yield
