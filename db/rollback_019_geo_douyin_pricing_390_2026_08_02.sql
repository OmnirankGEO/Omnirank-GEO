-- Migration 019 回滚 · 制作费 390 → 退回 260,并停用「重新生成」档
--
-- 🔴 **不 DELETE 价目行**。价目表是 SSOT,删行会让历史扣费记录对不上账
--    (老订单的 feature_code 查不到定义)。回滚 = 把值改回去 + 停用新增档,
--    与 rollback_008「不删 system_settings 行」同一口径。
--
-- 回滚后状态 = migration_018 的状态(260 / 2.00 / FALSE)。
-- ⚠️ 注意 018 那版 cost_compute 是 2.00(我按 points/130 推的),
--    019 已订正为 3.00(照抄 article_gen 实际值)。这里退回 018 的原值以保持可逆。

BEGIN;

UPDATE feature_pricing
   SET cost_points = 260,
       cost_compute = 2.00,
       is_active = TRUE
 WHERE feature_code = 'geo_douyin_image_post';

-- 重新生成档是 019 新增的:停用而不删除,保住历史对账
UPDATE feature_pricing
   SET is_active = FALSE
 WHERE feature_code = 'geo_douyin_image_post_regen';

COMMIT;

-- 代码侧回滚:老镜像不认识 geo_douyin_image_post_regen 这个 feature_code,
-- 也没有「重新生成」入口 → 停用该行即可,老镜像完全无感。
