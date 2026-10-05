-- [P1-4 引擎定向 · 2026-08-14] quotes.target_engine —— 写作项目级目标引擎。
-- 复用 geo_douyin 发布面先例(ranking_router TARGET_ENGINE 快照+继承),文章链
-- 目标面是每项目选项:NULL/空 = 不定向(现行为)。值域由应用层
-- writing/engine_targeting.py 归一(dashscope/deepseek/kimi/doubao),库层不加
-- CHECK —— 引擎清单会随监测侧演进,往 CHECK 加允许值不是 additive(历史教训)。
-- 幂等:ADD COLUMN IF NOT EXISTS,无数据 UPDATE(prestart 每次部署重放安全)。

ALTER TABLE quotes ADD COLUMN IF NOT EXISTS target_engine VARCHAR(32) DEFAULT NULL;

COMMENT ON COLUMN quotes.target_engine IS
    'P1-4 引擎定向:写作项目级目标引擎(dashscope/deepseek/kimi/doubao;NULL=不定向)。生成时注入 topic,lineage 按篇快照。';
