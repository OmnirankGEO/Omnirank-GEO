-- 回滚 migration_020(GEO 抖音图文详情页四字段)
--
-- 🔴 本文件【绝不登记进 db/migration_manifest.py】—— 登记 = 每次上线都把字段删掉。
--    只在人工确认要回退时手动执行。
--
-- 🔴 回滚会丢失 redraw_count(用户已用掉的重抽次数)。
--    回退后重新前滚,所有作品的重抽额度都会归零 = 白送一轮。
--    这是**可接受**的:重抽免费,额度只防滥用不涉资金。
--    (对比:价目行绝不能删 —— 删了历史扣费的 feature_code 就对不上账。)

ALTER TABLE geo_douyin_posts DROP CONSTRAINT IF EXISTS ck_geo_douyin_posts_redraw_nonneg;

ALTER TABLE geo_douyin_posts DROP COLUMN IF EXISTS closing_stale;
ALTER TABLE geo_douyin_posts DROP COLUMN IF EXISTS contact_enabled;
ALTER TABLE geo_douyin_posts DROP COLUMN IF EXISTS style_key;
ALTER TABLE geo_douyin_posts DROP COLUMN IF EXISTS redraw_count;
