-- 回滚 021:把按张数计价的两行停用。
-- 🔴 **绝不登记进 migration_manifest.py**(登记 = 上线即把本次改动撤掉)。
--
-- 用 is_active=FALSE 而不是 DELETE:
--   `get_feature_pricing` 只认 is_active=TRUE,停用即等价于"这个 code 不存在",
--   而 DELETE 会让已经引用过它的历史流水失去对照行。
-- 停用之后:
--   - extra_card 读不到价目 → pricing.extra_card_points 抛 PricingUnavailable
--     → 加张下单**失败**(fail-closed,不是白送)。想真回到旧行为要一并把
--     前端张数上限调回 4 或让基础价重新覆盖全部张数。
--   - redraw 读不到价目 → 重抽端点直接报"暂时不可用",不会变成免费白嫖。

UPDATE feature_pricing SET is_active = FALSE
 WHERE feature_code IN ('geo_douyin_image_post_extra_card',
                        'geo_douyin_image_post_redraw');
