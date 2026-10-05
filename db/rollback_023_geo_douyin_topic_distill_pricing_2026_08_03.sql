-- 回滚 023:把「AI 一键蒸馏选题」这行价目停用。
-- 🔴 **绝不登记进 migration_manifest.py**(登记 = 上线即把本次定价撤掉)。
--
-- 用 is_active=FALSE 而不是 DELETE:`get_feature_pricing` 只认 is_active=TRUE,
-- 停用即等价于"这个 code 不存在";DELETE 会让已经引用过它的历史流水失去对照行。
--
-- 停用之后的行为(**不是恢复免费**):
--   端点在扣费上下文里读不到价目 → check_balance_only 抛 → 蒸馏返回"暂时不可用"。
--   这是刻意的:回滚一条价目的意思是"这个功能先别卖",不是"改回白送" ——
--   白送要靠不调用扣费上下文来表达,而那是代码改动,不是一条 SQL 能做到的。

UPDATE feature_pricing SET is_active = FALSE
 WHERE feature_code = 'geo_douyin_topic_distill';
