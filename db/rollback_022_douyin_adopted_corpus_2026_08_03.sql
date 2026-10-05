-- 回滚 022:删掉抖音语料表。
-- 🔴 **绝不登记进 migration_manifest.py**(登记 = 上线即把表删了)。
--
-- 安全性:这张表是**纯离线采集的参考语料**,没有任何业务数据依赖它 ——
-- 删了之后蒸馏器走既有的降级路径(用 geo_research_source_signals 的
-- caption,形态混合),功能仍可用,只是 few-shot 质量下降并如实标注降级。
-- 采集脚本可随时重新灌回。

DROP INDEX IF EXISTS idx_douyin_corpus_industry_kind;
DROP TABLE IF EXISTS douyin_adopted_corpus;
