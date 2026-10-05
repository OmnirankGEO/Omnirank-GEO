-- v2.10.4 fixed company_profile slot 落地(Codex 四审 P0-1)
-- 锁定 v2.4 P0 #1:company_profile 仅由系统 fixed_count=1 自动生成 · 用户不可选(USER_CHOICE 10 项无 company)
--
-- 新加 2 字段:
--   topics.is_fixed BOOLEAN DEFAULT FALSE
--     · 标记 fixed slot(系统自动生成 · 不进配比器 configurable_count)
--   topics.style_code VARCHAR(64) DEFAULT NULL
--     · 单一权威字段(articles.style_code 已有 v2.7.1 · 此处 topics 同步)
--
-- ArticleWriter 联合判定(article_generator_service.py:382):
--   is_fixed=True AND style_code='company_profile' → company_profile slot(模板生成 · 不进 LLM)
--
-- 兼容性:
--   ADD COLUMN IF NOT EXISTS 反向兼容 · 旧数据 is_fixed=FALSE / style_code=NULL · 不影响

ALTER TABLE topics
    ADD COLUMN IF NOT EXISTS is_fixed BOOLEAN DEFAULT FALSE;

ALTER TABLE topics
    ADD COLUMN IF NOT EXISTS style_code VARCHAR(64) DEFAULT NULL;

COMMENT ON COLUMN topics.is_fixed IS
    'v2.10.4 fixed company_profile slot 标记 · TRUE=系统固定生成(不进配比器)/ FALSE=keyword-driven';
COMMENT ON COLUMN topics.style_code IS
    'v2.10.4 单一权威 style_code(跟 articles.style_code 同步)· 值域含 company_profile / price_roi / ranking_v2 等 11 项';

-- 索引:高频 fixed slot 查询(article_generator_service / compute_slot_buckets)
CREATE INDEX IF NOT EXISTS idx_topics_is_fixed
    ON topics(quote_id, is_fixed)
    WHERE is_fixed = TRUE;
