-- rollback_review_status_varchar32_2026-06-01.sql
-- 回滚 migration_review_status_varchar32_2026-06-01.sql
--
-- 危险:
--   如果当前有 review_status = 'imported_to_reference' 的行(21 字)
--   ALTER COLUMN TYPE VARCHAR(20) 会失败 ("value too long for type character varying(20)")
--   回滚前必须先清:
--     UPDATE geo_research_articles SET review_status='in_library'
--      WHERE review_status='imported_to_reference';
--   (这会让所有"已加入参考库"的标记丢失 · 代理端 UI 显示会回退 · 谨慎)
--
-- 用途: 仅用于"加宽是错误决定 · 需回到 20" 的极端场景 · 正常不该跑

BEGIN;

ALTER TABLE geo_research_articles
ALTER COLUMN review_status TYPE VARCHAR(20);

ALTER TABLE geo_research_review_log
ALTER COLUMN prev_review_status TYPE VARCHAR(20);

ALTER TABLE geo_research_review_log
ALTER COLUMN new_review_status TYPE VARCHAR(20);

DELETE FROM _migration_markers
 WHERE marker = 'review_status_varchar32_2026_06_01';

COMMIT;
