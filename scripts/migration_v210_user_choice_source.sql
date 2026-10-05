-- v2.10 文章方向配比器 · 新加 topics.user_choice_source 字段
-- 用于区分 user_choice 来源(单篇手动锁定 vs 批量配比写入)· 防"单篇 > 批量"优先级被覆盖
--
-- 取值:
--   'manual'              · 单篇 dropdown 用户主动选(锁定 · 批量配比不覆盖)
--   'batch_uniform'       · 批量"统一方向"写入(v2.8 "全部:X 文体" 路径)
--   'batch_distribution'  · 批量"自定义配比"写入(v2.10 新)
--   NULL                  · 未设置 / 单篇改回 auto 清除(进入可分配池)
--
-- 兼容性:
--   ADD COLUMN IF NOT EXISTS 反向兼容 · 不影响 v2.9.3.1 prod 老 code
--   diagnosis_db.py _safe_add_column 兜底 · 防冷启动顺序差异
--
-- 关联:
--   v2.9 加 topics.user_choice VARCHAR(32) · 已部署 prod
--   v2.10 加 topics.user_choice_source VARCHAR(32) · 新本次

ALTER TABLE topics
    ADD COLUMN IF NOT EXISTS user_choice_source VARCHAR(32) DEFAULT NULL;

COMMENT ON COLUMN topics.user_choice_source IS
    'v2.10 user_choice 来源标记 · manual(单篇手动锁定 · 优先级最高)/ batch_uniform(v2.8 全部统一方向)/ batch_distribution(v2.10 自定义配比)/ NULL(未设/已清)';

-- 兜底索引(可选 · 高频查"非 manual"做配比覆盖时性能优化)
-- partial index 防 NULL 占用过多空间
CREATE INDEX IF NOT EXISTS idx_topics_user_choice_source
    ON topics(user_choice_source)
    WHERE user_choice_source IS NOT NULL;
