-- Migration 017 回滚 · GEO 抖音图文管线 v1
-- 新增独立表方案 · 只 DROP 本包新建的两张表 · 存量表 0 触碰
--
-- 🔴 为什么这里可以 DROP:红线「禁止 DROP TABLE」针对的是**存量业务表**。
--    本文件只 DROP 本包在 migration_017 里刚建的两张新表,回滚前它们不存在,
--    也没有任何存量数据依赖 —— 与 rollback_008 的处置口径一致。
-- 🔴 顺序:先 tasks 后 posts(tasks 有指向 posts 的外键)。
--
-- ⚠️ 一旦线上已产出作品,DROP 会连作品库一起清掉(含豆包引用归因锚 published_url)。
--    真要回滚代码时,**优先只回滚镜像**:老镜像不认识这两张表,共存完全安全,
--    不需要跑本文件。本文件只用于"确认要连数据一起清掉"的场景。

BEGIN;

-- 关总闸(防回滚过程中还有请求在写)
-- 注:总闸是环境变量 GEO_DOUYIN_PIPELINE_ENABLED,不在 system_settings 里,
--     故此处无 UPDATE —— 关闸请改环境变量后重启。

DROP TABLE IF EXISTS geo_douyin_post_tasks;
DROP TABLE IF EXISTS geo_douyin_posts;

COMMIT;

-- 关键设计:
-- 两张表均为本包新建,老镜像 / 老代码完全不认识 → 代码回滚不依赖本文件。
-- 推荐回滚流程:
--   1. 环境变量 GEO_DOUYIN_PIPELINE_ENABLED=0(总闸关,0 扣费 0 生成)
--   2. 切老镜像(新表留着不碍事,作品数据保住)
--   3. 只有确认要清数据时,才跑本文件
