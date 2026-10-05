-- ============================================================================
-- 回滚 031 · 媒体上架档位独立字段
-- 🔴 本文件【绝不登记进 db/migration_manifest.py】—— 登记了就会被部署清单当正向迁移跑掉。
--
-- 前置:先回退代码(写入侧 _upsert_media / upsert_media / bulk_upsert_media /
--   import_mhz_media 的 INSERT 都带 listing_slot 列;列删了而代码还在 = 同步整批 500)。
--   顺序:回退代码 → 重启容器 → 才跑本文件。
--
-- 🔴 零业务残留:031 是纯 additive,resource_type_name / category 原值从未被改动过
--   (Owner 批的 (c) 叠加标记方案),所以删列不丢任何信息 —— 删掉就是回到 08-09 之前。
-- ============================================================================

DROP INDEX IF EXISTS idx_mhz_media_listing_slot;

ALTER TABLE mhz_media DROP CONSTRAINT IF EXISTS mhz_media_listing_slot_check;

ALTER TABLE mhz_media DROP COLUMN IF EXISTS listing_slot;

-- 自检:期望 0
-- SELECT count(*) FROM information_schema.columns
--  WHERE table_name='mhz_media' AND column_name='listing_slot';
