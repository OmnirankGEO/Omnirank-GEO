-- migration_review_status_varchar32_2026-06-01.sql
-- P14.4 E (2026-06-01) · review_status 列宽 VARCHAR(20) → VARCHAR(32)
--
-- 症状: 文章库 "加入参考库" 按钮 500 错误
--   reference_articles 插入成功 → 接着 UPDATE geo_research_articles SET review_status='imported_to_reference' 报:
--   "value too long for type character varying(20)"
--   ('imported_to_reference' 长度 21 > 列宽 20)
--
-- 根因: P05a CHECK 约束扩了 'imported_to_reference' 取值但忘了同步加宽列
-- 修法: review_status 列宽 + audit log 两列宽 → 32 · CHECK 取值不变(列内代码逻辑不动)
--
-- 跑法: docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope < scripts/migration_review_status_varchar32_2026-06-01.sql
-- 幂等: 已是 ≥32 时不报错 · 不阻塞 (PostgreSQL ALTER COLUMN TYPE 增大列宽不重写表)
-- 回滚: scripts/rollback_review_status_varchar32_2026-06-01.sql

BEGIN;

ALTER TABLE geo_research_articles
ALTER COLUMN review_status TYPE VARCHAR(32);

ALTER TABLE geo_research_review_log
ALTER COLUMN prev_review_status TYPE VARCHAR(32);

ALTER TABLE geo_research_review_log
ALTER COLUMN new_review_status TYPE VARCHAR(32);

-- 记 migration marker (跟其他 migration 同模式)
INSERT INTO _migration_markers (marker, applied_at, note)
VALUES (
    'review_status_varchar32_2026_06_01',
    NOW(),
    'P14.4 E: review_status 列宽 20→32 · 修加入参考库 500'
)
ON CONFLICT (marker) DO NOTHING;

COMMIT;

-- 校验 (跑完后期望全部满足):
--   SELECT character_maximum_length FROM information_schema.columns
--    WHERE table_name = 'geo_research_articles' AND column_name = 'review_status';   -- 期 32
--   SELECT character_maximum_length FROM information_schema.columns
--    WHERE table_name = 'geo_research_review_log'
--      AND column_name IN ('prev_review_status', 'new_review_status');                -- 都期 32
