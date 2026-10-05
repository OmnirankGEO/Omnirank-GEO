"""
research_monitor 测试包 conftest

防误连生产 + 每测试前清表 + asyncio event loop 配置。
所有 research_monitor 测试用真实 PostgreSQL 测试库,
测试库名字必须含 'test' 否则启动时 raise。
"""
import os
import asyncio
import pytest
import psycopg2
from psycopg2.extras import RealDictCursor


# ==================== 防误连生产铁律 ====================

def _validate_test_db_url():
    """启动时检查 DATABASE_URL 必须含 'test',否则禁止跑测试"""
    url = os.environ.get('DATABASE_URL', '') or os.environ.get('TEST_DATABASE_URL', '')
    if 'test' not in url.lower():
        raise RuntimeError(
            f"测试库 URL 必须含 'test' 字样防误连生产, 当前: {url[:50]}..."
        )
    return url


# ==================== fixture: pg_conn ====================

@pytest.fixture
def pg_conn(setup_test_db):
    """每个测试函数独立连接,测试结束自动关闭。

    依赖 setup_test_db(session 级) 自动建表。
    纯函数测试不申明本 fixture 就不会触发 DB 连接。
    """
    url = _validate_test_db_url()
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = False
    yield conn
    try:
        conn.rollback()
    except Exception:
        pass
    conn.close()


# ==================== fixture: clean_research_tables ====================

RESEARCH_TABLES = [
    'geo_research_review_log',
    'geo_research_article_citations',
    'geo_research_articles',
    'geo_research_round_call',
    'geo_research_round',
    'geo_research_prompts',
    'geo_research_industries',
    'geo_research_cost_log',
]


# 注意: 不要 autouse=True,只在显式申明的测试里清表。
# 否则纯函数测试(没用到 pg_conn)也会被强制连 DB。
@pytest.fixture
def clean_research_tables(pg_conn):
    """
    每个测试前后 TRUNCATE 8 张测试表(不动 config 表,因为它有默认配置不能清),
    保证 isolation。
    autouse=True 自动应用到所有 research_monitor 测试。
    """
    cur = pg_conn.cursor()
    for tbl in RESEARCH_TABLES:
        try:
            cur.execute(f"TRUNCATE TABLE {tbl} RESTART IDENTITY CASCADE")
        except psycopg2.Error:
            pg_conn.rollback()
    pg_conn.commit()
    yield
    for tbl in RESEARCH_TABLES:
        try:
            cur.execute(f"TRUNCATE TABLE {tbl} RESTART IDENTITY CASCADE")
        except psycopg2.Error:
            pg_conn.rollback()
    pg_conn.commit()


# ==================== fixture: event_loop (asyncio) ====================

@pytest.fixture(scope="session")
def event_loop():
    """session-scoped event loop, 防 pytest-asyncio 默认 fixture 冲突"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ==================== 测试库初始化(session 级别) ====================

@pytest.fixture(scope="session")
def setup_test_db():
    """
    session 启动时,在测试库上跑一次 migration_geo_research_monitor.sql,
    确保 9 张表都建好。

    跑一次后所有测试共享 schema,结束 session 后保留(下一次 session 可复用)。

    注意: 不 autouse,只通过 pg_conn fixture 链式触发。
    纯函数测试不依赖 pg_conn 就不会触发 DB 连接。
    """
    url = _validate_test_db_url()
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()

    # research monitor 依赖主系统既有的 geo_research_raw 和写作大厅 reference_articles。
    # 集成测试库是冷库,需要在应用 migration 前补最小兼容 schema。
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reference_articles (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_url TEXT,
            platform TEXT,
            industry TEXT,
            intent_type TEXT,
            analysis TEXT,
            success_proof TEXT,
            use_count INTEGER DEFAULT 0,
            success_rate REAL DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    for path in [
        'scripts/migration_geo_research_monitor.sql',
        'scripts/migration_research_monitor_review_v2.sql',
        'scripts/migration_research_monitor_v3.sql',
        'scripts/migration_research_monitor_v4.sql',
        'scripts/migration_stage3_url_budget_2026_07_16.sql',
        'scripts/migration_stage3_url_budget_industry_name_2026_07_18.sql',
    ]:
        with open(path, 'r', encoding='utf-8') as f:
            cur.execute(f.read())

    # GEO article v1.4 adds lineage fields to the existing research-monitor
    # tables.  The full migration also depends on main-app tables that this
    # focused fixture intentionally does not create, so mirror only the owned
    # research schema here instead of silently testing against the old shape.
    cur.execute("""
        ALTER TABLE geo_research_raw
            ADD COLUMN IF NOT EXISTS provider VARCHAR(64) DEFAULT 'legacy_unknown',
            ADD COLUMN IF NOT EXISTS model VARCHAR(128) DEFAULT 'legacy_unknown',
            ADD COLUMN IF NOT EXISTS model_revision VARCHAR(128) DEFAULT 'legacy_unknown',
            ADD COLUMN IF NOT EXISTS surface VARCHAR(64) DEFAULT 'legacy_unknown',
            ADD COLUMN IF NOT EXISTS search_mode VARCHAR(64) DEFAULT 'legacy_unknown',
            ADD COLUMN IF NOT EXISTS prompt_snapshot TEXT;

        ALTER TABLE geo_research_articles
            ADD COLUMN IF NOT EXISTS content_type VARCHAR(20),
            ADD COLUMN IF NOT EXISTS inline_cleaned_content TEXT,
            ADD COLUMN IF NOT EXISTS corpus_grade VARCHAR(8) DEFAULT 'JC0',
            ADD COLUMN IF NOT EXISTS canonical_body_hash CHAR(64),
            ADD COLUMN IF NOT EXISTS body_hash_algorithm VARCHAR(80),
            ADD COLUMN IF NOT EXISTS content_cluster_id VARCHAR(80),
            ADD COLUMN IF NOT EXISTS body_boundary_version VARCHAR(80),
            ADD COLUMN IF NOT EXISTS label_provenance_version VARCHAR(80);

        CREATE TABLE IF NOT EXISTS geo_research_article_fetches (
            id BIGSERIAL PRIMARY KEY,
            fetch_event_key VARCHAR(64) NOT NULL UNIQUE,
            article_id BIGINT REFERENCES geo_research_articles(id) ON DELETE SET NULL,
            source_url TEXT NOT NULL,
            normalized_url TEXT NOT NULL,
            final_url TEXT,
            url_hash CHAR(40),
            parent_fetch_id BIGINT REFERENCES geo_research_article_fetches(id) ON DELETE SET NULL,
            request_profile VARCHAR(80) NOT NULL,
            request_profile_version VARCHAR(80) NOT NULL,
            preset VARCHAR(40),
            engine VARCHAR(40),
            cache_policy VARCHAR(40),
            timeout_seconds INTEGER,
            token_budget INTEGER,
            attempt_number INTEGER NOT NULL,
            response_status VARCHAR(40) NOT NULL,
            http_status INTEGER,
            warning TEXT,
            failure_reason VARCHAR(80),
            published_time TIMESTAMPTZ,
            fetched_at TIMESTAMPTZ NOT NULL,
            latency_ms INTEGER,
            usage_tokens INTEGER,
            parser_version VARCHAR(80) NOT NULL,
            raw_object_key TEXT,
            raw_response_hash CHAR(64),
            raw_body_hash CHAR(64),
            body_object_key TEXT,
            body_hash CHAR(64),
            robots_policy VARCHAR(40),
            robots_reason TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_geo_fetches_article_time
            ON geo_research_article_fetches(article_id, fetched_at DESC);
        CREATE INDEX IF NOT EXISTS idx_geo_fetches_url_time
            ON geo_research_article_fetches(url_hash, fetched_at DESC);

        ALTER TABLE geo_research_articles
            ALTER COLUMN review_status TYPE VARCHAR(32);
        ALTER TABLE geo_research_articles
            DROP CONSTRAINT IF EXISTS geo_research_articles_review_status_check;
        ALTER TABLE geo_research_articles
            ADD CONSTRAINT geo_research_articles_review_status_check
            CHECK (review_status IN (
                'crawled', 'pending_review', 'auto_skipped', 'approved', 'rejected',
                'in_library', 'imported_to_reference'
            ));
    """)

    # P14.2 C1: 测试库强制确保 (url_hash, primary_industry) UNIQUE · 跟 prod migration 等价
    # 这样 C1 跨行业 dup INSERT 不被老 url_hash UNIQUE 吞 · 行为符合"已 migration"prod 环境
    # 实际 prod 部署: C2 migration 文件由 Deploy-CTO 在 prod 跑 · 此处仅模拟 post-migration
    # P15: 文章意图分类字段 · 跟 scripts/migration_article_intent_type.sql 等价
    cur.execute("""
        ALTER TABLE geo_research_articles
            ADD COLUMN IF NOT EXISTS intent_type VARCHAR(20),
            ADD COLUMN IF NOT EXISTS intent_confidence NUMERIC(4,3),
            ADD COLUMN IF NOT EXISTS intent_reason TEXT,
            ADD COLUMN IF NOT EXISTS intent_model VARCHAR(50),
            ADD COLUMN IF NOT EXISTS intent_classified_at TIMESTAMPTZ;
        CREATE INDEX IF NOT EXISTS idx_geo_research_articles_intent_type
            ON geo_research_articles(intent_type)
            WHERE intent_type IS NOT NULL;
    """)

    cur.execute("""
        DO $$
        BEGIN
            -- drop 旧全局 UNIQUE (若存在)
            IF EXISTS (SELECT 1 FROM pg_constraint
                        WHERE conname = 'geo_research_articles_url_hash_key') THEN
                ALTER TABLE geo_research_articles
                    DROP CONSTRAINT geo_research_articles_url_hash_key;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_constraint
                        WHERE conname = 'geo_research_articles_url_key') THEN
                ALTER TABLE geo_research_articles
                    DROP CONSTRAINT geo_research_articles_url_key;
            END IF;
            -- add 复合 UNIQUE (若不存在)
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conname = 'geo_research_articles_url_hash_industry_key') THEN
                ALTER TABLE geo_research_articles
                    ADD CONSTRAINT geo_research_articles_url_hash_industry_key
                    UNIQUE (url_hash, primary_industry);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conname = 'geo_research_articles_url_industry_key') THEN
                ALTER TABLE geo_research_articles
                    ADD CONSTRAINT geo_research_articles_url_industry_key
                    UNIQUE (url, primary_industry);
            END IF;
        END $$;
    """)
    print(
        "测试库初始化: research_monitor schema + v2/v3/v4 + "
        "GEO article v1.4 research lineage + P14.2 per-industry UNIQUE 已就绪"
    )

    conn.close()
    yield


# ==================== fixture: sample data ====================

@pytest.fixture
def sample_industry(pg_conn):
    """创建一个测试行业,返回 id"""
    cur = pg_conn.cursor()
    cur.execute("""
        INSERT INTO geo_research_industries (name, slug, sort_order, active)
        VALUES ('测试行业', 'test_industry', 1, TRUE)
        RETURNING id
    """)
    industry_id = cur.fetchone()['id']
    pg_conn.commit()
    return industry_id


@pytest.fixture
def sample_prompts(pg_conn, sample_industry):
    """创建 3 个测试 prompts,返回 id 列表"""
    cur = pg_conn.cursor()
    ids = []
    for i, text in enumerate(['测试问题 1', '测试问题 2', '测试问题 3']):
        cur.execute("""
            INSERT INTO geo_research_prompts (industry_id, prompt_text, sort_order, active)
            VALUES (%s, %s, %s, TRUE)
            RETURNING id
        """, (sample_industry, text, i))
        ids.append(cur.fetchone()['id'])
    pg_conn.commit()
    return ids


@pytest.fixture
def sample_pending_article(pg_conn):
    """创建一篇 pending_review 状态的文章,返回 id"""
    import hashlib
    cur = pg_conn.cursor()
    url = 'https://test.com/p/1'
    url_hash = hashlib.sha1(url.encode()).hexdigest()
    cur.execute("""
        INSERT INTO geo_research_articles (
            url, url_hash, domain, title, primary_industry,
            cleaned_char_count, cleanliness_score, clean_status, review_status
        ) VALUES (
            %s, %s, 'test.com', '测试文章', '测试行业',
            4500, 85, 'cleaned', 'pending_review'
        ) RETURNING id
    """, (url, url_hash))
    article_id = cur.fetchone()['id']
    pg_conn.commit()
    return article_id


@pytest.fixture
def make_pending_article(pg_conn):
    """工厂 fixture: 调一次建一篇,允许多次建多篇,url 自动唯一"""
    import hashlib
    counter = [0]

    def _make(title='测试文章', score=85, char_count=4500, industry='测试行业'):
        counter[0] += 1
        cur = pg_conn.cursor()
        url = f'https://test.com/p/{counter[0]}'
        url_hash = hashlib.sha1(url.encode()).hexdigest()
        cur.execute("""
            INSERT INTO geo_research_articles (
                url, url_hash, domain, title, primary_industry,
                cleaned_char_count, cleanliness_score, clean_status, review_status
            ) VALUES (
                %s, %s, 'test.com', %s, %s,
                %s, %s, 'cleaned', 'pending_review'
            ) RETURNING id
        """, (url, url_hash, title, industry, char_count, score))
        article_id = cur.fetchone()['id']
        pg_conn.commit()
        return article_id

    return _make
