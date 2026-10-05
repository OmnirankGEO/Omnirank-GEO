"""P4 支撑迁移(029)的隔离 PostgreSQL fixture。

锁死在本任务专用的一次性库上。库名硬校验形态照抄
tests/pricing_quote_wiring/conftest.py —— 那是仓库里唯一一份
「真库 + 库名逐字校验」的现成模式,不另造第二套。

🔴 这一层存在的理由:migration_029 是 additive ALTER + 建表。
   拿假库(mock cursor)测 ALTER 等于什么都没测 —— CHECK 约束、部分唯一索引、
   IF NOT EXISTS 的幂等性,三样都只有真 PG 会告诉你真话。

🔴 与 pricing_quote_wiring 的**一处刻意差异**:那套给每个进程建私有 schema,
   本套直接用 `public`。理由不是图省事 —— migration_029 与仓库里其余迁移一样
   是 **`public.` 全限定**写死的(`ALTER TABLE public.media_outlets`、
   `'public.media_outlets'::regclass`)。放进私有 schema 跑,测的就不再是
   生产将要跑的那份 SQL:第一版这么写,16 个用例全部 UndefinedTable。
   一次性容器独占,每个用例重建 public,隔离性由容器边界保证。
"""

from __future__ import annotations

import os

from pathlib import Path


import psycopg2
import psycopg2.extras
import pytest


ROOT = Path(__file__).resolve().parents[2]

EXACT_THROWAWAY_URL = (
    "postgresql://geo_admin:gaptest@127.0.0.1:55610/geo_gapplan_test"
)

_configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
if _configured != EXACT_THROWAWAY_URL:
    raise RuntimeError(
        "gap_plan_migration 测试锁死在本任务一次性库上;"
        f"期望 TEST_DATABASE_URL={EXACT_THROWAWAY_URL!r},实得 {_configured!r}。"
        "\n起库命令:docker run -d --name omnirank-gapplan-0808-pg "
        "-e POSTGRES_PASSWORD=gaptest -e POSTGRES_USER=geo_admin "
        "-e POSTGRES_DB=geo_gapplan_test -p 55610:5432 pgvector/pgvector:pg16"
    )

os.environ["DATABASE_URL"] = EXACT_THROWAWAY_URL

MIGRATION_PATH = ROOT / "db" / "migration_029_gap_operation_plan_2026_08_08.sql"

# 迁移前置:029 只对既有的 media_outlets 做加列,所以真库里必须先有那张表。
# 这份 DDL 逐字取自 db/diagnosis_db.py:1152-1175 的现役建表(生产实测 21 列一致)。
PRE_DDL = """
CREATE TABLE media_outlets (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'traditional',
    platform TEXT,
    sivp_price REAL,
    min_price REAL,
    max_price REAL,
    ai_engines_covered TEXT DEFAULT '[]',
    ai_coverage_count INTEGER DEFAULT 0,
    geo_confirmed INTEGER DEFAULT 0,
    geo_notes TEXT,
    citation_count INTEGER DEFAULT 0,
    category TEXT,
    region TEXT,
    baidu_news_source INTEGER DEFAULT 0,
    suitable_industries TEXT DEFAULT '[]',
    data_source TEXT,
    source_url TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(name, platform, media_type)
);
INSERT INTO media_outlets (name, platform, media_type, ai_coverage_count)
VALUES ('存量媒体甲', 'blog', 'traditional', 3);
"""


def _connect():
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    conn.autocommit = True
    return conn


@pytest.fixture(scope="session")
def migration_sql() -> str:
    assert MIGRATION_PATH.exists(), f"迁移文件不存在:{MIGRATION_PATH}"
    return MIGRATION_PATH.read_text(encoding="utf-8")


@pytest.fixture()
def fresh_db(migration_sql):
    """每个用例:干净 schema → 建前置表 → 跑一次迁移。返回 (conn, run_again)。"""
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute(PRE_DDL)
        cur.execute(migration_sql)

    def run_again():
        with conn.cursor() as c2:
            c2.execute(migration_sql)

    try:
        yield conn, run_again
    finally:
        conn.close()
