-- 回滚 024:删掉画幅列。
-- 🔴 **绝不登记进 migration_manifest.py**(登记 = 上线即把这一列删了)。
--
-- 删列会丢掉每条内容"当初选的是哪个画幅"这个事实,不可逆。
-- 只有在确认要整体撤掉「画幅自选」这个功能时才跑;
-- 若只是想让所有人回到 3:4,更安全的做法是把前端选择器摘掉 +
-- 后端 normalize_aspect_ratio 落默认,库里那一列留着不碍事。

ALTER TABLE geo_douyin_posts DROP COLUMN IF EXISTS aspect_ratio;
