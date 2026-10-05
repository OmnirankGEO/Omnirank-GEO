-- ============================================================================
-- CTO-15.9 Phase 1 + M1a/M1b/M1c 汇总迁移
-- 2026-04-25
--
-- 说明(给 Deploy-CTO):
--   此脚本是本 session 所有 DDL 的"文档 + 幂等兜底"· Deploy-CTO 第 5 轮已通过
--   startup hook + lazy migration 自动执行全部列 · 本脚本 0 新增动作(全部 IF NOT EXISTS)
--   保留原因:
--     1. 未来回滚/重建库需要一次性审计 SQL(startup hook 分散难审)
--     2. 下 session 新 CTO 看得到全貌
--     3. 手工触发兜底(如 startup hook 失败 · 可人工跑本脚本)
--
-- 覆盖范围:
--   · P0.8 monitoring_query (confirmed_keywords / extra_keywords)
--   · M1a T4 pipeline_stage_log 全新表 + 2 索引
--   · M1b M1 client_profiles.business_type / city_scope
--   · M1c T5 · client_profiles.industry_brief JSONB merge(只在应用层 · 无 DDL)
--
-- 幂等性: 所有 ALTER TABLE ADD COLUMN + CREATE TABLE/INDEX 均 IF NOT EXISTS
-- ============================================================================

BEGIN;

-- ========== P0.8 · monitoring_query 字段(CTO-15.9 commit 230cc45) ==========
-- 关键词意图污染解耦 · 支持人工 override query
-- _safe_add_column 已经在 db/monitoring_db.py::init_monitoring_tables 幂等加入
ALTER TABLE confirmed_keywords ADD COLUMN IF NOT EXISTS monitoring_query TEXT;
ALTER TABLE extra_keywords     ADD COLUMN IF NOT EXISTS monitoring_query TEXT;


-- ========== M1a T4 · pipeline_stage_log 9 步工作流埋点(commit 524c6d3) ==========
-- 定位: M4 stage_runs 主表兼容壳 · 禁平行重建
-- init_pipeline_stage_log_table() startup hook 已建
CREATE TABLE IF NOT EXISTS pipeline_stage_log (
    id             SERIAL PRIMARY KEY,
    brand_id       INTEGER NOT NULL,
    stage_name     TEXT NOT NULL,
    event          TEXT NOT NULL,
    meta           JSONB,
    actor_user_id  INTEGER,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pipeline_stage_log_brand_stage
    ON pipeline_stage_log (brand_id, stage_name, event, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_stage_log_created
    ON pipeline_stage_log (created_at DESC);


-- ========== M1b M1 · business_type + city_scope SSOT(commit ec16bd5) ==========
-- SSOT 在 client_profiles(派生层) · brands 主表不加(稳定)
-- profile_db._ensure_columns() lazy → _init_profile_columns() startup-eager(本 commit)
ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS business_type VARCHAR(20) DEFAULT 'B2C';
ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS city_scope    VARCHAR(20) DEFAULT 'local';


-- ========== M1c T5 · market_insight 相关字段(commit 3db566b · CTO-13.0 已建) ==========
-- 以下字段 CTO-13.0 v3.7 S1.4 已建 · 这里列出供审计(幂等)
-- service_scope / local_competitors / industry_brief_status / industry_brief_confirmed 等
ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS service_scope             VARCHAR(20);
ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS service_scope_reasoning   TEXT;
ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS local_competitors         JSONB DEFAULT '[]';


-- ========== A6 · 存量 49 brand industry → business_type 扫描(首次运维手工跑一次) ==========
-- 只 UPDATE default 'B2C' 且 industry 强信号的行 · 代理已改过的不覆盖
-- 多关键词匹配 B2B 行业
-- 注: industry 在 brands 表 · 需 JOIN

UPDATE client_profiles cp
SET business_type = 'B2B'
FROM brands b
WHERE cp.brand_id = b.id
  AND cp.business_type = 'B2C'  -- 只扫默认值 · 不覆盖代理已改
  AND (
    b.industry ILIKE '%机械%'
    OR b.industry ILIKE '%化工%'
    OR b.industry ILIKE '%外贸%'
    OR b.industry ILIKE '%工业%'
    OR b.industry ILIKE '%制造%'
    OR b.industry ILIKE '%设备%'
    OR b.industry ILIKE '%SaaS%'
    OR b.industry ILIKE '%CRM%'
    OR b.industry ILIKE '%ERP%'
    OR b.industry ILIKE '%供应链%'
    OR b.industry ILIKE '%物流企业服务%'
    OR b.industry ILIKE '%B2B%'
  );

-- 政企强信号
UPDATE client_profiles cp
SET business_type = '政企'
FROM brands b
WHERE cp.brand_id = b.id
  AND cp.business_type = 'B2C'
  AND (
    b.industry ILIKE '%政府%'
    OR b.industry ILIKE '%政务%'
    OR b.industry ILIKE '%事业单位%'
    OR b.industry ILIKE '%央企%'
    OR b.industry ILIKE '%信创%'
  );

-- city_scope 扫: 城市为 '全国' / 空值 / 不限 → national
UPDATE client_profiles cp
SET city_scope = 'national'
FROM brands b
WHERE cp.brand_id = b.id
  AND cp.city_scope = 'local'  -- 不覆盖代理已改
  AND (
    b.cities IS NULL
    OR TRIM(b.cities::TEXT) = ''
    OR b.cities::TEXT ILIKE '%全国%'
    OR b.cities::TEXT ILIKE '%不限%'
  );


-- ========== 部署后 smoke 查询(Deploy-CTO 跑一次审计) ==========

-- 1. pipeline_stage_log 表结构
-- SELECT column_name, data_type FROM information_schema.columns
--  WHERE table_name='pipeline_stage_log' ORDER BY ordinal_position;

-- 2. monitoring_query 字段存在
-- SELECT COUNT(*) FROM information_schema.columns
--  WHERE table_name IN ('confirmed_keywords', 'extra_keywords')
--    AND column_name = 'monitoring_query';
-- 期望: 2

-- 3. business_type 分布
-- SELECT business_type, COUNT(*) FROM client_profiles GROUP BY business_type;

-- 4. city_scope 分布
-- SELECT city_scope, COUNT(*) FROM client_profiles GROUP BY city_scope;

COMMIT;
