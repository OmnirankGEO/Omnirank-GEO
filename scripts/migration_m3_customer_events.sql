-- M3 客户行为事件单表 (CTO-C 2026-04-26 · feat/m3-customer-signals)
--
-- 设计:
--   - 单表统一 4 公开 token 链路埋点 (public_report / public_quote / selection / portal)
--   - event_key UNIQUE 索引保证幂等 (后端 ON CONFLICT DO NOTHING)
--   - 永远不存原始 IP/UA/token · 只存 hash (上层 api 强制)
--   - 365 天保留 · scheduler 03:15 CST 自动清理
--
-- 幂等:可重复跑 · 全部 IF NOT EXISTS / OR REPLACE
-- 验收点 8 (老板拍板):migration 幂等

BEGIN;

-- 主表
CREATE TABLE IF NOT EXISTS m3_customer_events (
    id BIGSERIAL PRIMARY KEY,
    brand_id INTEGER,
    quote_id INTEGER,
    diagnosis_id INTEGER,
    token_hash TEXT,
    source VARCHAR(32) NOT NULL,
    event_type VARCHAR(48) NOT NULL,
    event_key TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    ip_hash TEXT,
    user_agent_hash TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- brand_id + occurred_at (代理工作台时间线)
CREATE INDEX IF NOT EXISTS idx_m3_events_brand_ts
    ON m3_customer_events (brand_id, occurred_at DESC)
    WHERE brand_id IS NOT NULL;

-- quote_id + occurred_at (报价跟进卡)
CREATE INDEX IF NOT EXISTS idx_m3_events_quote_ts
    ON m3_customer_events (quote_id, occurred_at DESC)
    WHERE quote_id IS NOT NULL;

-- diagnosis_id + occurred_at (公开报告页)
CREATE INDEX IF NOT EXISTS idx_m3_events_diag_ts
    ON m3_customer_events (diagnosis_id, occurred_at DESC)
    WHERE diagnosis_id IS NOT NULL;

-- token_hash + occurred_at (跨 brand 同 token 客户视角)
CREATE INDEX IF NOT EXISTS idx_m3_events_token_ts
    ON m3_customer_events (token_hash, occurred_at DESC)
    WHERE token_hash IS NOT NULL;

-- source + event_type + occurred_at (聚合统计)
CREATE INDEX IF NOT EXISTS idx_m3_events_source_type_ts
    ON m3_customer_events (source, event_type, occurred_at DESC);

-- event_key UNIQUE (幂等去重 · 部分索引允许 NULL)
CREATE UNIQUE INDEX IF NOT EXISTS uq_m3_events_event_key
    ON m3_customer_events (event_key)
    WHERE event_key IS NOT NULL;

COMMIT;

-- ============================================================
-- 验收 dry-run:
--   BEGIN;
--   INSERT INTO m3_customer_events (source, event_type, event_key, metadata)
--     VALUES ('public_report', 'opened', 'test_key_1', '{}'::jsonb);
--   INSERT INTO m3_customer_events (source, event_type, event_key, metadata)
--     VALUES ('public_report', 'opened', 'test_key_1', '{}'::jsonb)
--     ON CONFLICT (event_key) WHERE event_key IS NOT NULL DO NOTHING;
--   SELECT COUNT(*) FROM m3_customer_events WHERE event_key='test_key_1';  -- 期望 1
--   ROLLBACK;
-- ============================================================
