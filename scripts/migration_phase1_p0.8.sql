-- P0.8 · confirmed_keywords + extra_keywords 加 monitoring_query 字段
-- CTO-15.9 2026-04-25
--
-- 背景:
--   老"{keyword}哪家好？推荐一下"模板对 B2B / 工具类 / 教育类关键词意图污染
--   P0.8 意图分类器已能识别 brand_decision / avoid_trap / info / noise + 给 rewrite_suggestions
--   需要持久化层让代理或 pipeline 把改写结果存到 monitoring_query
--
-- 字段归属(CLAUDE.md 元指令 17 SQL 4 维度 · 2026-04-25 再核验):
--   monitoring_query 属 confirmed_keywords / extra_keywords · 不属 monitoring_tasks
--   monitoring_keywords 表不存在(历史 CTO-15.7 交叉验证)
--
-- 向后兼容:
--   默认 NULL · 老数据 resolve_monitoring_query() fallback 到 build_question(keyword)
--   不改读取路径·不修改存量数据

BEGIN;

-- 幂等 · ADD COLUMN IF NOT EXISTS(Postgres 9.6+)
ALTER TABLE confirmed_keywords ADD COLUMN IF NOT EXISTS monitoring_query TEXT;
ALTER TABLE extra_keywords ADD COLUMN IF NOT EXISTS monitoring_query TEXT;

-- dry-run 核验(生产部署前 Deploy-CTO 可手工跑)
-- SELECT column_name, data_type FROM information_schema.columns
--  WHERE table_name IN ('confirmed_keywords','extra_keywords')
--    AND column_name='monitoring_query';

COMMIT;
