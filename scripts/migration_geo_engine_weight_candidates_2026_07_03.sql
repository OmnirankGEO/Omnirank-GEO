-- migration_geo_engine_weight_candidates_2026_07_03.sql
-- 归属:GEO 数据飞轮融入 · 批次 B3-1(引擎权重候选管道)
-- 性质:新增 shadow/审核 staging 表 · 幂等(IF NOT EXISTS)· 纯 additive · 无破坏性
-- 由 Deploy-CTO 手动 psql 应用(项目无 .sql 自动 runner);Python 侧 init_engine_weight_candidate_tables()
--   在端点入口也会 CREATE TABLE IF NOT EXISTS 兜底(与本 .sql 同口径)。
--
-- 背景:geo_engine_weights 是"全局单向量"(engine UNIQUE · 无 industry/status 列 · prod 0 行 →
--   _get_engine_weights 长期走硬编码兜底)。它没有候选/审核生命周期列,因此权重候选需要独立 staging 表:
--   Stage 7 聚合后按各行业引用份额生成候选 → 人工审核 → 通过才 UPSERT 进 geo_engine_weights。
--
-- SQL 4 维核验:
--   列名/类型自洽(industry TEXT / engine TEXT / *_weight REAL / evidence JSONB / status VARCHAR /
--     reviewed_by BIGINT / *_at TIMESTAMPTZ);无外键(候选是软引用 engine 名)。
--   归属:新表,独立;不动 geo_engine_weights 结构。
--   幂等:CREATE TABLE/INDEX IF NOT EXISTS;再跑无副作用。
--   约束:部分唯一索引保证"同 (industry,engine) 至多一条 open 候选"(status='candidate')。

CREATE TABLE IF NOT EXISTS geo_engine_weight_candidates (
    id              BIGSERIAL PRIMARY KEY,
    industry        TEXT NOT NULL,                 -- 证据来源行业(原始中文名)
    engine          TEXT NOT NULL,                 -- canonical 引擎名(豆包/Kimi/DeepSeek/千问)
    current_weight  REAL,                          -- 生成时的现行权重(geo_engine_weights 或硬编码兜底)
    suggested_weight REAL NOT NULL,                -- 按该行业引用份额算出的建议权重(0..1)
    evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 样本量/引用份额/引用总数等证据
    status          VARCHAR(20) NOT NULL DEFAULT 'candidate',  -- candidate / approved / rejected
    reviewed_by     BIGINT,
    review_note     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at     TIMESTAMPTZ
);

-- 至多一条 open 候选 / (industry, engine)
CREATE UNIQUE INDEX IF NOT EXISTS ux_gewc_open
    ON geo_engine_weight_candidates (industry, engine)
    WHERE status = 'candidate';

CREATE INDEX IF NOT EXISTS idx_gewc_status
    ON geo_engine_weight_candidates (status, created_at DESC);
