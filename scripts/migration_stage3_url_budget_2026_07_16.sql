-- ============================================================
-- Stage 3 URL 质量预算 · 耐久断点表 + config seed
-- (Stage 3 全量调研容量 P1 返工 · 2026-07-16)
--
-- 幂等: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS + INSERT ON CONFLICT DO NOTHING。
-- 纯 additive: 新表 + geo_research_config 四键, 无既有表结构改动、无数据改写。
-- 用途:
--   - 每 round 的 Stage3 选中 URL 预算(质量排序 + 配额)持久化到本表, 不只存内存;
--   - status 字段 = 耐久断点/幂等批次(续跑只处理 status='pending' 的 URL, 已 done/
--     skipped/failed 的不重爬)。
--   - 消费侧: services/research_monitor/url_budget.py(选择算法) +
--     db/research_url_budget_db.py(CRUD) + round_runner.stage3(前置预算 + crawl 更新状态)。
-- 回滚: DROP TABLE geo_research_url_budget; DELETE FROM geo_research_config WHERE key IN (四键);
-- ============================================================

CREATE TABLE IF NOT EXISTS geo_research_url_budget (
    id                BIGSERIAL PRIMARY KEY,
    round_id          VARCHAR(50) NOT NULL,
    url_hash          CHAR(40) NOT NULL,          -- SHA1(normalized_url)
    normalized_url    TEXT NOT NULL,
    url               TEXT NOT NULL,              -- 原始 cite_url(首现)
    domain            TEXT,
    industry_id       INTEGER,
    industry_name     TEXT NOT NULL DEFAULT '',
    prompt_id         INTEGER,
    platform          VARCHAR(20),
    raw_id            BIGINT,
    rank_in_response  INTEGER,
    quality_score     DOUBLE PRECISION DEFAULT 0,
    budget_rank       INTEGER,                    -- 选中排序(1 起)
    status            VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending/done/skipped/failed
    reason            VARCHAR(40),                -- crawl 结果 reason(crawled_new/reused_existing/...)
    attempts          SMALLINT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 同 round 内一个 (url, 行业) 唯一预算项(与 article 去重口径 (url_hash, primary_industry) 对齐);
    -- 幂等持久化: 重算预算(续跑)ON CONFLICT DO NOTHING 不覆盖已有处理状态。
    CONSTRAINT uq_url_budget_round_urlhash_industry UNIQUE (round_id, url_hash, industry_name)
);

COMMENT ON TABLE geo_research_url_budget IS 'Stage3 URL 质量预算 + 耐久断点(每 round 选中集与处理状态)';

-- @index-guard idx_url_budget_round_status ON geo_research_url_budget plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_url_budget_round_status' AND i.indrelid = to_regclass('public.geo_research_url_budget')) THEN
        NULL;  -- 已在 public.geo_research_url_budget 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_url_budget_round_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_url_budget_round_status 已存在但不在 public.geo_research_url_budget 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_url_budget_round_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_url_budget_round_status ON public.geo_research_url_budget (round_id, status);
    END IF;
END $idxguard$;
-- @index-guard idx_url_budget_round_rank ON geo_research_url_budget plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_url_budget_round_rank' AND i.indrelid = to_regclass('public.geo_research_url_budget')) THEN
        NULL;  -- 已在 public.geo_research_url_budget 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_url_budget_round_rank' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_url_budget_round_rank 已存在但不在 public.geo_research_url_budget 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_url_budget_round_rank' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_url_budget_round_rank ON public.geo_research_url_budget (round_id, budget_rank);
    END IF;
END $idxguard$;

-- Earlier deployment candidates created industry_name as nullable. Normalise
-- that partial schema without deleting or guessing rows. A NULL/empty collision
-- is ambiguous and therefore aborts before any update.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM geo_research_url_budget
         WHERE industry_name IS NULL
    ) THEN
        IF EXISTS (
            SELECT 1
              FROM geo_research_url_budget
             GROUP BY round_id, url_hash, COALESCE(industry_name, '')
            HAVING COUNT(*) > 1
        ) THEN
            RAISE EXCEPTION
                'ABORT[stage3_url_budget]: NULL/empty industry_name collision requires manual review';
        END IF;
        UPDATE geo_research_url_budget
           SET industry_name = ''
         WHERE industry_name IS NULL;
    END IF;

    ALTER TABLE geo_research_url_budget
        ALTER COLUMN industry_name SET DEFAULT '',
        ALTER COLUMN industry_name SET NOT NULL;
END $$;

-- config: 预算参数(round_runner 读; 读不到/非法回落代码默认值)
INSERT INTO geo_research_config (key, value_json, description) VALUES
    ('stage3_url_budget_max', '1500',
     'Stage3 真爬 Jina 的全新 URL 总量上限(DB 命中免费项不占此额度；实际耗时以生产 canary 为准)。'
     '设为 0 = 关闭预算 = 旧全量抓取(灰度回退用·缺省若读不到亦回落 1500 非 0)'),
    ('stage3_min_urls_per_industry', '40',
     'Stage3 每行业保覆盖底线(质量排序后至少取 N)'),
    ('stage3_min_urls_per_platform', '50',
     'Stage3 每平台保覆盖底线(4 平台代表性)'),
    ('stage3_max_urls_per_domain', '30',
     'Stage3 单域名 URL 上限(去低价值重复来源)')
ON CONFLICT (key) DO NOTHING;
