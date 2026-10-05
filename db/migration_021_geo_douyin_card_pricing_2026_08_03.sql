-- GEO 抖音图文 · 按张数计价(Owner 2026-08-03 拍板)
--   套餐默认/含 4 张;每多一张 100 算力;单张重抽由**免费**改为 100 算力。
--   少于 4 张不减价(这条是代码口径,表里不体现 —— 见 services/geo_douyin/pricing.py)。
--
-- 两条都是新增 feature_code,ON CONFLICT DO UPDATE 幂等,可反复连跑。
-- 不改动 018/019 建的两行基础价(390 / 260),那两行仍是"一组"的价。
--
-- ⚠️ cost_compute 取值说明(**不做推断,把依据和不确定都写下来**):
--    生产同量级样本里 130→1.00、390→3.00 均等于 cost_points/130(即人民币金额),
--    但存在两个反例(report_regen 130→2.00、article_rewrite 260→3.00),
--    更像调价时只改了 points 没跟着改 compute 的历史脏数据。
--    本次取 100/130 ≈ 0.77。
--    🔴 该列在 `middleware/billing.py` 里**零引用**(grep 实核),不参与任何扣费判定,
--       取值错了不会影响资金 —— 所以这里可以按最合理的推导取值,而不是留空成 0。
--
-- 🔴 计价怎么用这两行:
--    - extra_card:走 `freeze_points(..., extra_cost=张数超出部分 × 本行 cost_points)`,
--      **不新增任何资金路径** —— 冻结/提交/退款仍是原来那一套。
--    - redraw:单独一次 freeze → commit/release,是本包**唯一新增的资金路径**,
--      交付单里已单列。

INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES ('geo_douyin_image_post_extra_card', 'GEO 图文加做一张卡', 100, 0.77, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET feature_name = EXCLUDED.feature_name,
    cost_points = EXCLUDED.cost_points,
    cost_compute = EXCLUDED.cost_compute,
    requires_paid_points = EXCLUDED.requires_paid_points,
    is_active = TRUE;

INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES ('geo_douyin_image_post_redraw', 'GEO 图文单张重抽', 100, 0.77, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET feature_name = EXCLUDED.feature_name,
    cost_points = EXCLUDED.cost_points,
    cost_compute = EXCLUDED.cost_compute,
    requires_paid_points = EXCLUDED.requires_paid_points,
    is_active = TRUE;
