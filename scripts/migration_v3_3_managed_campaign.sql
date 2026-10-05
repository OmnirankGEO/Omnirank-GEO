-- ============================================================
-- v3.3 + v3.4 GEO 全自动托管 — 数据库迁移
-- ============================================================
-- 作者: CTO-11.0
-- 日期: 2026-04-16
-- 配套: docs/功能设计/v3.3_v3.4_GEO托管_开发文档.md §2
--
-- 执行顺序（按事务独立）：
--   Sprint 0: v3.4 复盘引擎埋点 (3 张新表 + 现有表字段补全)
--   Sprint 1: v3.3 托管核心 (managed_campaigns + managed_actions)
--   v3.4: brand_strategies (复盘策略档案)
--   feature_pricing: 加 'managed_campaign_consume' 标记
-- ============================================================

\set ON_ERROR_STOP on

BEGIN;

-- ============================================================
-- Sprint 0.1: article_publish_snapshots (发文基线)
-- ============================================================
CREATE TABLE IF NOT EXISTS article_publish_snapshots (
  id SERIAL PRIMARY KEY,
  article_id INTEGER NOT NULL,
  campaign_id INTEGER,
  brand_id INTEGER,
  keyword_id INTEGER,
  platform VARCHAR(50) NOT NULL,
  publish_url TEXT,
  publish_timestamp TIMESTAMP NOT NULL,

  -- 基线数据（发文前的状态）
  baseline_detection_rate NUMERIC(5,2),
  baseline_rank INTEGER,
  baseline_competitor_count INTEGER,

  created_at TIMESTAMP DEFAULT NOW()
);

-- @index-guard idx_snapshots_article ON article_publish_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_snapshots_article' AND i.indrelid = to_regclass('public.article_publish_snapshots')) THEN
        NULL;  -- 已在 public.article_publish_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_snapshots_article' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_snapshots_article 已存在但不在 public.article_publish_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_snapshots_article' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_snapshots_article ON public.article_publish_snapshots (article_id);
    END IF;
END $idxguard$;
-- @index-guard idx_snapshots_brand ON article_publish_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_snapshots_brand' AND i.indrelid = to_regclass('public.article_publish_snapshots')) THEN
        NULL;  -- 已在 public.article_publish_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_snapshots_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_snapshots_brand 已存在但不在 public.article_publish_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_snapshots_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_snapshots_brand ON public.article_publish_snapshots (brand_id, publish_timestamp DESC);
    END IF;
END $idxguard$;

-- ============================================================
-- Sprint 0.2: daily_ranking_snapshots (每日排名快照)
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_ranking_snapshots (
  id SERIAL PRIMARY KEY,
  brand_id INTEGER,
  keyword_id INTEGER,
  snapshot_date DATE NOT NULL,
  rank_positions JSONB,
  detection_rate NUMERIC(5,2),
  top10_days_cumulative INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT NOW(),
  UNIQUE(brand_id, keyword_id, snapshot_date)
);

-- @index-guard idx_daily_rank_brand_date ON daily_ranking_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_daily_rank_brand_date' AND i.indrelid = to_regclass('public.daily_ranking_snapshots')) THEN
        NULL;  -- 已在 public.daily_ranking_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_daily_rank_brand_date' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_daily_rank_brand_date 已存在但不在 public.daily_ranking_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_daily_rank_brand_date' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_daily_rank_brand_date ON public.daily_ranking_snapshots (brand_id, snapshot_date DESC);
    END IF;
END $idxguard$;

-- ============================================================
-- Sprint 0.3: article_daily_metrics (每日效果聚合)
-- ============================================================
CREATE TABLE IF NOT EXISTS article_daily_metrics (
  id SERIAL PRIMARY KEY,
  article_id INTEGER NOT NULL,
  metric_date DATE NOT NULL,
  platform VARCHAR(50),
  read_count INTEGER DEFAULT 0,
  read_rate_vs_baseline NUMERIC(6,2),
  engagement_rate NUMERIC(6,2),
  effectiveness_score NUMERIC(5,2),
  created_at TIMESTAMP DEFAULT NOW(),
  UNIQUE(article_id, metric_date, platform)
);

-- @index-guard idx_daily_metrics_article ON article_daily_metrics plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_daily_metrics_article' AND i.indrelid = to_regclass('public.article_daily_metrics')) THEN
        NULL;  -- 已在 public.article_daily_metrics 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_daily_metrics_article' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_daily_metrics_article 已存在但不在 public.article_daily_metrics 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_daily_metrics_article' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_daily_metrics_article ON public.article_daily_metrics (article_id, metric_date DESC);
    END IF;
END $idxguard$;

-- ============================================================
-- Sprint 0.4: 现有表补字段
-- ============================================================
-- article_generations 加效果字段（如果表不存在则跳过）
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='article_generations') THEN
    ALTER TABLE article_generations
      ADD COLUMN IF NOT EXISTS effectiveness_score NUMERIC(5,2),
      ADD COLUMN IF NOT EXISTS detection_rate_delta NUMERIC(5,2),
      ADD COLUMN IF NOT EXISTS days_stayed_top10 INTEGER DEFAULT 0;
  END IF;

  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='media_publications') THEN
    ALTER TABLE media_publications
      ADD COLUMN IF NOT EXISTS publish_timestamp TIMESTAMP,
      ADD COLUMN IF NOT EXISTS article_url TEXT,
      ADD COLUMN IF NOT EXISTS track_start_date DATE;
  END IF;
END $$;

COMMIT;

-- ============================================================
-- Sprint 1.1: managed_campaigns (托管套餐主表)
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS managed_campaigns (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  brand_id INTEGER REFERENCES brands(id),

  -- 关键词与目标
  keyword VARCHAR(100) NOT NULL,
  target_sov_pct INTEGER,                       -- 后台目标 SOV (0-50)
  target_display_label VARCHAR(50),             -- 前端话术 "经常被推荐"
  tier_label VARCHAR(20),
    -- 'entry' (15%) / 'standard' (25%) / 'flagship' (33%) / 'strong' (50%) / 'custom'

  -- 充值池（v2.1 核心：充值即消费）
  initial_recharge_yuan NUMERIC(10,2) NOT NULL,
  total_recharged_yuan NUMERIC(10,2) NOT NULL,  -- 累计充值（含加充）
  total_consumed_yuan NUMERIC(10,2) DEFAULT 0,  -- 累计消耗

  -- 执行模式
  mode VARCHAR(20) DEFAULT 'semi_auto',
    -- 'semi_auto' / 'full_auto'

  -- 单篇上限（防 AI 选超贵媒体）
  max_per_article_yuan NUMERIC(8,2) DEFAULT 200,

  -- 监测频次
  check_frequency_per_day INTEGER DEFAULT 1,
  monitoring_cost_per_check NUMERIC(6,4) DEFAULT 0.29,

  -- 报价有效期（3 天）
  estimate_quoted_at TIMESTAMP,
  estimate_valid_until TIMESTAMP,

  -- 屏蔽词检测
  consecutive_zero_detection_days INTEGER DEFAULT 0,

  -- 余额低提醒（一次性 flag）
  low_balance_warned BOOLEAN DEFAULT FALSE,

  -- 12 个月休眠转赠送
  last_active_at TIMESTAMP DEFAULT NOW(),
  dormant_warned_at TIMESTAMP,
  dormancy_converted_at TIMESTAMP,

  -- AI 当前方案
  current_plan JSONB,

  -- 通知开关
  alert_on_replenish BOOLEAN DEFAULT TRUE,

  -- 状态
  status VARCHAR(20) DEFAULT 'active',
    -- active / paused / depleted / keyword_blocked / user_cancelled / dormancy_converted

  -- 授权审计
  authorized_at TIMESTAMP DEFAULT NOW(),
  authorized_ip VARCHAR(45),
  agreement_version VARCHAR(20),

  created_at TIMESTAMP DEFAULT NOW(),
  paused_at TIMESTAMP,
  depleted_at TIMESTAMP,

  delivered_articles INTEGER DEFAULT 0,

  -- 检查约束
  CONSTRAINT check_balance_non_negative
    CHECK (total_recharged_yuan >= total_consumed_yuan),
  CONSTRAINT check_target_sov_range
    CHECK (target_sov_pct IS NULL OR (target_sov_pct >= 0 AND target_sov_pct <= 60)),
  CONSTRAINT check_freq_positive
    CHECK (check_frequency_per_day >= 1 AND check_frequency_per_day <= 6)
);

-- 索引
-- @index-guard idx_campaigns_user ON managed_campaigns plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_campaigns_user' AND i.indrelid = to_regclass('public.managed_campaigns')) THEN
        NULL;  -- 已在 public.managed_campaigns 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_campaigns_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_campaigns_user 已存在但不在 public.managed_campaigns 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_campaigns_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_campaigns_user ON public.managed_campaigns (user_id, status);
    END IF;
END $idxguard$;

-- @index-guard idx_campaigns_active ON managed_campaigns plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_campaigns_active' AND i.indrelid = to_regclass('public.managed_campaigns')) THEN
        NULL;  -- 已在 public.managed_campaigns 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_campaigns_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_campaigns_active 已存在但不在 public.managed_campaigns 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_campaigns_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_campaigns_active ON public.managed_campaigns (status) WHERE status = 'active';
    END IF;
END $idxguard$;

-- @index-guard idx_campaigns_depleted ON managed_campaigns plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_campaigns_depleted' AND i.indrelid = to_regclass('public.managed_campaigns')) THEN
        NULL;  -- 已在 public.managed_campaigns 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_campaigns_depleted' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_campaigns_depleted 已存在但不在 public.managed_campaigns 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_campaigns_depleted' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_campaigns_depleted ON public.managed_campaigns (depleted_at) WHERE status = 'depleted';
    END IF;
END $idxguard$;

-- @index-guard idx_campaigns_dormant ON managed_campaigns plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_campaigns_dormant' AND i.indrelid = to_regclass('public.managed_campaigns')) THEN
        NULL;  -- 已在 public.managed_campaigns 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_campaigns_dormant' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_campaigns_dormant 已存在但不在 public.managed_campaigns 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_campaigns_dormant' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_campaigns_dormant ON public.managed_campaigns (last_active_at);
    END IF;
END $idxguard$;

-- 唯一约束（防同品牌+同词重复 active 套餐）
-- @index-guard idx_campaigns_unique_active ON managed_campaigns unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_campaigns_unique_active' AND i.indrelid = to_regclass('public.managed_campaigns')) THEN
        NULL;  -- 已在 public.managed_campaigns 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_campaigns_unique_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_campaigns_unique_active 已存在但不在 public.managed_campaigns 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_campaigns_unique_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX idx_campaigns_unique_active ON public.managed_campaigns (brand_id, keyword) WHERE status = 'active';
    END IF;
END $idxguard$;

COMMENT ON TABLE managed_campaigns IS 'v3.3/v3.4 GEO 全自动托管套餐主表';
COMMENT ON COLUMN managed_campaigns.tier_label IS '档位标签：entry/standard/flagship/strong/custom';
COMMENT ON COLUMN managed_campaigns.target_sov_pct IS '后台目标 SOV (0-50)，前端翻译为"出现率"';
COMMENT ON COLUMN managed_campaigns.mode IS 'semi_auto (默认，AI 写完待审) 或 full_auto (杀手级)';

COMMIT;

-- ============================================================
-- Sprint 1.2: managed_actions (操作审计)
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS managed_actions (
  id SERIAL PRIMARY KEY,
  campaign_id INTEGER REFERENCES managed_campaigns(id) ON DELETE CASCADE,
  action_type VARCHAR(40),
  action_detail JSONB,
  cost_points INTEGER DEFAULT 0,
  cost_yuan NUMERIC(8,2) DEFAULT 0,
  result VARCHAR(20),
  reason TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

-- @index-guard idx_actions_campaign ON managed_actions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_actions_campaign' AND i.indrelid = to_regclass('public.managed_actions')) THEN
        NULL;  -- 已在 public.managed_actions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_actions_campaign' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_actions_campaign 已存在但不在 public.managed_actions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_actions_campaign' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_actions_campaign ON public.managed_actions (campaign_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_actions_type ON managed_actions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_actions_type' AND i.indrelid = to_regclass('public.managed_actions')) THEN
        NULL;  -- 已在 public.managed_actions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_actions_type' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_actions_type 已存在但不在 public.managed_actions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_actions_type' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_actions_type ON public.managed_actions (action_type, created_at DESC);
    END IF;
END $idxguard$;

COMMENT ON TABLE managed_actions IS '托管套餐执行动作日志（AI 视角）';

COMMIT;

-- ============================================================
-- Sprint 1.3: pending_review_articles (半自动模式待审队列)
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS pending_review_articles (
  id SERIAL PRIMARY KEY,
  campaign_id INTEGER NOT NULL REFERENCES managed_campaigns(id) ON DELETE CASCADE,
  article_id INTEGER,                            -- 关联 article_generations.id (如有)
  title TEXT NOT NULL,
  content_preview TEXT,
  full_content TEXT,
  platforms_to_publish JSONB,                    -- ["知乎","今日头条",...]
  ai_reasoning TEXT,                              -- AI 为什么写这篇
  estimated_publish_cost_yuan NUMERIC(8,2),

  status VARCHAR(20) DEFAULT 'pending',
    -- pending / approved / rejected / withdrawn / auto_published / expired

  auto_publish_at TIMESTAMP NOT NULL,             -- created_at + 24h
  reviewed_at TIMESTAMP,
  reviewed_by_user_id INTEGER,
  review_note TEXT,

  created_at TIMESTAMP DEFAULT NOW(),
  CONSTRAINT check_status_valid
    CHECK (status IN ('pending','approved','rejected','withdrawn','auto_published','expired'))
);

-- @index-guard idx_pending_review_campaign ON pending_review_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pending_review_campaign' AND i.indrelid = to_regclass('public.pending_review_articles')) THEN
        NULL;  -- 已在 public.pending_review_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pending_review_campaign' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pending_review_campaign 已存在但不在 public.pending_review_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pending_review_campaign' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pending_review_campaign ON public.pending_review_articles (campaign_id, status);
    END IF;
END $idxguard$;
-- @index-guard idx_pending_review_auto_publish ON pending_review_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pending_review_auto_publish' AND i.indrelid = to_regclass('public.pending_review_articles')) THEN
        NULL;  -- 已在 public.pending_review_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pending_review_auto_publish' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pending_review_auto_publish 已存在但不在 public.pending_review_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pending_review_auto_publish' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pending_review_auto_publish ON public.pending_review_articles (auto_publish_at) WHERE status = 'pending';
    END IF;
END $idxguard$;

COMMENT ON TABLE pending_review_articles IS '半自动模式待审文章队列，24h 不审默认自动发';

COMMIT;

-- ============================================================
-- v3.4: brand_strategies (品牌策略档案 — 护城河)
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS brand_strategies (
  id SERIAL PRIMARY KEY,
  brand_id INTEGER NOT NULL UNIQUE,

  -- 每周更新
  top_performing_styles JSONB,
  top_performing_platforms JSONB,
  effective_keywords JSONB,
  losing_patterns JSONB,

  -- 自动调整参数
  preferred_content_type VARCHAR(30),
  preferred_platform_mix JSONB,
  article_tone VARCHAR(30),

  -- 反哺 industry_knowledge L2
  shareable_patterns JSONB,
  share_consent BOOLEAN DEFAULT TRUE,           -- v2.1: 默认静默开启

  last_reviewed_at TIMESTAMP,
  review_count INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- @index-guard idx_brand_strategies_review ON brand_strategies plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_brand_strategies_review' AND i.indrelid = to_regclass('public.brand_strategies')) THEN
        NULL;  -- 已在 public.brand_strategies 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_brand_strategies_review' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_brand_strategies_review 已存在但不在 public.brand_strategies 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_brand_strategies_review' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_brand_strategies_review ON public.brand_strategies (last_reviewed_at);
    END IF;
END $idxguard$;

COMMENT ON TABLE brand_strategies IS 'v3.4 品牌专属策略档案（复盘引擎产出，3+ 个月形成迁移壁垒）';

COMMIT;

-- ============================================================
-- v3.4: brand_managed_packages (品牌套餐主记录，关联多个 managed_campaigns)
-- ============================================================
BEGIN;

CREATE TABLE IF NOT EXISTS brand_managed_packages (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  brand_id INTEGER NOT NULL REFERENCES brands(id),

  total_price_yuan NUMERIC(10,2) NOT NULL,      -- v3.4 markup 1.2x 内嵌后的售价
  raw_cost_yuan NUMERIC(10,2) NOT NULL,         -- 后台原成本
  markup_factor NUMERIC(4,2) DEFAULT 1.2,

  campaign_ids INTEGER[] NOT NULL,              -- 关联的 managed_campaigns.id 数组
  status VARCHAR(20) DEFAULT 'active',          -- active / paused / completed

  -- 复盘引擎执行记录
  last_review_at TIMESTAMP,
  next_review_at TIMESTAMP,                     -- 默认 7 天后

  authorized_at TIMESTAMP DEFAULT NOW(),
  authorized_ip VARCHAR(45),
  agreement_version VARCHAR(20),

  created_at TIMESTAMP DEFAULT NOW(),
  CONSTRAINT check_brand_pkg_status_valid
    CHECK (status IN ('active','paused','completed'))
);

-- @index-guard idx_brand_packages_brand ON brand_managed_packages plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_brand_packages_brand' AND i.indrelid = to_regclass('public.brand_managed_packages')) THEN
        NULL;  -- 已在 public.brand_managed_packages 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_brand_packages_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_brand_packages_brand 已存在但不在 public.brand_managed_packages 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_brand_packages_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_brand_packages_brand ON public.brand_managed_packages (brand_id, status);
    END IF;
END $idxguard$;
-- @index-guard idx_brand_packages_review ON brand_managed_packages plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_brand_packages_review' AND i.indrelid = to_regclass('public.brand_managed_packages')) THEN
        NULL;  -- 已在 public.brand_managed_packages 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_brand_packages_review' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_brand_packages_review 已存在但不在 public.brand_managed_packages 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_brand_packages_review' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_brand_packages_review ON public.brand_managed_packages (next_review_at) WHERE status = 'active';
    END IF;
END $idxguard$;

COMMENT ON TABLE brand_managed_packages IS 'v3.4 全品牌托管主记录（多 keyword 合并）';

COMMIT;

-- ============================================================
-- feature_pricing 加托管相关功能码（用于积分扣费日志）
-- ============================================================
BEGIN;

INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points, is_active)
VALUES
  ('managed_campaign_recharge',  'GEO 托管充值（按金额扣 paid_points）', 1, 0, TRUE, TRUE),
  ('managed_brand_recharge',     '全品牌托管充值（含 markup 服务费）', 1, 0, TRUE, TRUE)
ON CONFLICT (feature_code) DO NOTHING;

COMMIT;

-- ============================================================
-- 用户操作日志类型说明（user_action_logs 已存在，不新建表）
-- ============================================================
-- 新增 action_type 枚举（仅文档说明，DB 不强制）：
--   managed_confirm_recharge   / managed_top_up
--   managed_adjust_plan        / managed_pause / managed_resume / managed_cancel
--   managed_withdraw_pending   / managed_review_approve / managed_review_reject
--   managed_dormancy_warned_30d / managed_dormancy_warned_7d
--   managed_dormancy_converted / geo_assets_updated
--   admin_inspect              / admin_dormancy_scan

-- 完成
SELECT '✓ v3.3 + v3.4 迁移完成' AS status;
