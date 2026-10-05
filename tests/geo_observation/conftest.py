"""Isolated PostgreSQL fixtures for AI-2 geo-observation governance tests (self-built throwaway DB)."""
from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg2
import pytest

from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块(不会触发 init_db)

ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
MIGRATIONS = [
    ROOT / "scripts" / "migration_geo_observation_v1_2026_07_17.sql",
    ROOT / "scripts" / "migration_geo_observation_collection_mode_2026_07_20.sql",
    ROOT / "scripts" / "migration_geo_observation_aggregate_basis_2026_07_20.sql",
    ROOT / "scripts" / "migration_monitoring_identity_review_2026_07_21.sql",
]

# HMAC key for tests (base64 of a >=16 byte string)
os.environ.setdefault("GEO_OBSERVATION_HMAC_KEY_V1", "dGVzdC1nZW8tb2JzZXJ2YXRpb24taG1hYy1rZXktMzJieXRlIQ==")


def _assert_safe() -> None:
    if not TEST_DATABASE_URL:
        raise RuntimeError("TEST_DATABASE_URL required")
    name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if "test" not in name or "prod" in name:
        raise RuntimeError(f"unsafe test db name: {name!r}")


# 最小源 schema(只含 hook/reconciler/resolver 读取的真实列名/类型)
BASE_SCHEMA = r"""
CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT, value_type TEXT, description TEXT, updated_at TIMESTAMPTZ DEFAULT NOW());
-- brands 由 db.brands_schema.ensure_brands_schema() 建(见下方 fixture) —— 不在这里手搓。
CREATE TABLE brand_aliases (
    id SERIAL PRIMARY KEY, brand_id INTEGER NOT NULL, canonical_name TEXT, alias TEXT, source TEXT
);
-- 付费诊断资金状态机(子集)
CREATE TABLE diagnosis_runs (
    run_token TEXT PRIMARY KEY, session_id TEXT, owner_user_id INTEGER, brand_id INTEGER,
    billing_mode TEXT DEFAULT 'paid', freeze_id BIGINT, freeze_backend TEXT,
    run_status TEXT NOT NULL DEFAULT 'pending_freeze', last_settlement_error TEXT,
    status_changed_at TIMESTAMPTZ DEFAULT NOW(), finished_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE diagnosis_records (
    id SERIAL PRIMARY KEY, session_id TEXT UNIQUE, brand_id INTEGER, run_token TEXT,
    result_visibility TEXT, total_score INTEGER, raw_data_json TEXT
);
CREATE TABLE diagnosis_refund_records (
    id BIGSERIAL PRIMARY KEY, run_token TEXT NOT NULL, freeze_task_ref TEXT, freeze_id BIGINT,
    freeze_backend TEXT, owner_user_id INTEGER, points BIGINT, refund_tx_ref TEXT, operator TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- 持续监测(子集)
CREATE TABLE monitoring_tasks (
    id SERIAL PRIMARY KEY, quote_id INTEGER, client_id TEXT, brand_id INTEGER, status TEXT DEFAULT 'pending',
    total_tests INTEGER, completed_tests INTEGER, created_at TIMESTAMPTZ DEFAULT NOW(), completed_at TIMESTAMPTZ
);
CREATE TABLE monitoring_results (
    id SERIAL PRIMARY KEY, task_id INTEGER, keyword_id INTEGER, keyword TEXT, platform TEXT,
    is_detected SMALLINT DEFAULT 0, mention_type TEXT DEFAULT 'none', response_snippet TEXT,
    full_response TEXT, response_status VARCHAR(32) DEFAULT 'completed', tested_at TIMESTAMPTZ DEFAULT NOW(),
    -- [防御型 GEO WP1 · CUR-09 · 2026-08-21] source_hooks.assess_monitoring_result 现在还读这三列。
    --   类型逐列**照抄生产**(不是照抄「差不多的类型」)——
    --   本仓 2026-08-09 记过「测试 schema 类型不同构照样全绿」,反过来同样成立:
    --   夹具类型写窄了,正确的生产改动会在这里红得莫名其妙。
    --   生产真值取自 db/monitoring_db.py::_safe_add_column,并在一次性 PG16 上逐列实测:
    --     search_citations       text                                    (JSON 字符串)
    --     competitors_mentioned  jsonb   DEFAULT '[]'::jsonb             (已是数组)
    --     target_outcome         varchar(40) DEFAULT 'legacy_unknown'    (哨兵默认值)
    --   🔴 DEFAULT 也照抄:'legacy_unknown' 这个哨兵正是判据要区分「源没判定」
    --      与「源判定为否」的那一格,默认值写成 NULL 会让该判据失去被测对象。
    search_citations TEXT,
    competitors_mentioned JSONB DEFAULT '[]'::jsonb,
    target_outcome VARCHAR(40) DEFAULT 'legacy_unknown'
);
CREATE TABLE confirmed_keywords (
    id SERIAL PRIMARY KEY, quote_id TEXT, keyword TEXT, monitoring_query TEXT, status TEXT DEFAULT 'confirmed'
);
CREATE TABLE extra_keywords (
    id SERIAL PRIMARY KEY, quote_id TEXT, client_id TEXT, brand_id INTEGER, keyword TEXT,
    monitoring_query TEXT, status TEXT DEFAULT 'active'
);
-- 公共调研(子集)
CREATE TABLE geo_research_round (
    id BIGSERIAL PRIMARY KEY, round_id VARCHAR(50) UNIQUE, batch_id VARCHAR(50) NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', finished_at TIMESTAMPTZ
);
CREATE TABLE geo_research_raw (
    id SERIAL PRIMARY KEY, industry TEXT, query TEXT, engine TEXT, cited_platform TEXT,
    answer_text TEXT, is_answer_cited BOOLEAN DEFAULT FALSE, adoption_rank INTEGER,
    batch_id TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE sched_job_runs (
    id BIGSERIAL PRIMARY KEY, job_name TEXT NOT NULL, scheduled_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL, finished_at TIMESTAMPTZ,
    UNIQUE(job_name, scheduled_at)
);
"""

# 每测清空(源表 + 我的表)
_SOURCE_TABLES = [
    "diagnosis_refund_records", "diagnosis_records", "diagnosis_runs",
    "monitoring_results", "monitoring_tasks", "confirmed_keywords", "extra_keywords",
    "geo_research_raw", "geo_research_round", "brand_aliases", "brands",
]
_OBS_TABLES = [
    "geo_observation_audit", "geo_observation_signals", "geo_observation_contributor_buckets",
    "geo_observation_aggregates", "geo_observation_events", "geo_observation_gold_eval", "geo_observation_policy",
    "geo_observation_aggregate_refresh_manifest",
    "geo_observation_aggregate_bucket_revision",
    "sched_job_runs",
]


def _apply_migration(conn, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    sql = re.sub(r"^\s*\\[a-zA-Z_]+.*$", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"^\s*(BEGIN|COMMIT)\s*;\s*$", "", sql, flags=re.MULTILINE | re.IGNORECASE)
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


@pytest.fixture(scope="session", autouse=True)
def isolated_postgres_schema():
    _assert_safe()
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(BASE_SCHEMA)
        # 🔴 [R5 ⑤ 2026-08-20] brands 走**生产 SSOT 出口**,夹具不再自己 author 这张表。
        #   手搓版只有 8 列(id/name/company_name/brand_display_names/industry/
        #   industry_category/owner_user_id/status),生产 32 列 —— 比生产窄的夹具
        #   会让「读到的列」和「生产真有的列」对不上,断言就成了假绿。
        #   ⚠️ 不是改跑 init_db:那条路已实测证伪(本目录 174 passed/0 failed →
        #      138 passed/34 failed,194s → 589s)。窄出口只建 brands 一张表,0.2s。
        ensure_brands_schema(cur)
    conn.commit()
    for migration in MIGRATIONS:
        _apply_migration(conn, migration)
    for migration in MIGRATIONS:  # 2× 幂等
        _apply_migration(conn, migration)
    conn.close()

    from db import connection
    connection.close_pool()
    connection.DATABASE_URL = TEST_DATABASE_URL
    yield
    connection.close_pool()


@pytest.fixture(autouse=True)
def reset_data(isolated_postgres_schema):
    # 释放上个测试泄漏的池连接(assert 失败未 close → idle-in-transaction 持 ACCESS SHARE 锁,阻塞 TRUNCATE)
    from db import connection
    connection.close_pool()
    dbname = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0]
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s "
            "AND pid <> pg_backend_pid() AND state IN ('idle in transaction','idle in transaction (aborted)')",
            (dbname,),
        )
        cur.execute("TRUNCATE " + ", ".join(_OBS_TABLES + _SOURCE_TABLES) + " RESTART IDENTITY CASCADE")
        cur.execute("SELECT COUNT(*) FROM geo_observation_policy")
        if cur.fetchone()[0] == 0:
            cur.execute("""INSERT INTO geo_observation_policy(singleton_id,policy_version,policy_json,updated_by,updated_reason)
                VALUES (1,1,%s::jsonb,'test','reset')""", (_DEFAULT_POLICY_JSON,))
        cur.execute(
            "DELETE FROM system_settings WHERE key IN "
            "('geo_observation_policy_epoch','geo_observation_scheduler_inventory')"
        )
    conn.close()
    yield


_DEFAULT_POLICY_JSON = """{
  "policy_version":"v1-test-default",
  "platforms":[
    {"platform_key":"doubao","enabled":true,"base_weight_bps":2500,"surface_key":"doubao_ark_api_search","legacy_read_only":false},
    {"platform_key":"qwen","enabled":true,"base_weight_bps":2500,"surface_key":"qwen_dashscope_search","legacy_read_only":false},
    {"platform_key":"deepseek","enabled":true,"base_weight_bps":2500,"surface_key":"deepseek_dashscope_search_legacy","legacy_read_only":false},
    {"platform_key":"yuanbao","enabled":true,"base_weight_bps":2500,"surface_key":"yuanbao_hy3_tokenhub","legacy_read_only":false},
    {"platform_key":"kimi","enabled":false,"base_weight_bps":0,"surface_key":null,"legacy_read_only":true}
  ],
  "source_base_weights_bps":{"research":10000,"paid_diagnosis":4000,"monitoring":7000},
  "sampling_budget":{"max_calls_per_round":5000,"max_calls_per_day":20000,"max_cost_micros_per_day":100000000,"max_retry_calls_per_request":2},
  "max_single_brand_share_bps":1000,"public_min_independent_brands":3,"public_min_source_types":2,
  "retention_days":365,"anomaly_confirmation_numerator":2,"anomaly_confirmation_denominator":3,
  "feature_flags":{"ingest_enabled":false,"promotion_enabled":false,"aggregation_enabled":false,"product_enabled":false}
}"""
