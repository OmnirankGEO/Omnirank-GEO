-- ============================================================
-- [#15] 回滚：删除竞品数据库表，回到 JSON 模式
-- ============================================================
-- 风险：drop table 会删除所有数据，回滚前必须先把表数据导出
-- 用法：
--   1. pg_dump -t competitors -t competitor_snapshots > competitor_backup.sql
--   2. psql < migration_competitor_table_rollback.sql
--   3. 部署回旧代码（用 data/competitors.json）
-- ============================================================

DROP TABLE IF EXISTS competitor_snapshots;
DROP TABLE IF EXISTS competitors;

DO $$
BEGIN
    DELETE FROM migrations_log WHERE name = 'migration_competitor_table';
EXCEPTION WHEN undefined_table THEN
    NULL;
END $$;
