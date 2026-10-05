-- Migration 008 v2 回滚 · 老板 round-3 P0 拍板
-- 独立表方案 · DROP 新表 · 老 keyword_price_cache 0 触碰
-- 代码回滚不依赖 DB rollback · 老镜像 + 新 DB schema 共存安全
BEGIN;

-- 1. 关 feature flag(防回滚过程中代理继续走 LLM 路径)
UPDATE system_settings SET value='false', updated_at=CURRENT_TIMESTAMP
    WHERE key='LLM_FIRST_PRICING_ENABLED';
UPDATE system_settings SET value='[]', updated_at=CURRENT_TIMESTAMP
    WHERE key='LLM_FIRST_PRICING_AGENT_WHITELIST';

-- 2. DROP 独立 LLM cache 表(数据 + schema 都清干净)
DROP TABLE IF EXISTS keyword_price_cache_llm;

-- 注意:不动老 keyword_price_cache 表
-- 不删 system_settings 行(保留配置元数据 · 下次重开 flag 即可复用)

COMMIT;

-- 关键设计:
-- DROP 独立 LLM 表后 · 老镜像 / 老代码完全无感
-- 老 keyword_price_cache · UNIQUE(brand_name, keyword) · 始终未变 · 部署回滚安全
-- 代码 rollback 流程:
--   1. (可选)先关 flag · 0.5 秒生效
--   2. 切老镜像 · 老 ON CONFLICT(brand_name, keyword) 正常工作
--   3. (可选)DROP TABLE keyword_price_cache_llm · 释放空间
