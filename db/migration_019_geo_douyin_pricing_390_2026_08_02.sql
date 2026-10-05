-- GEO 抖音图文 · 制作费调价 260 → 390,并新增「重新生成」档 260
-- Owner 2026-08-02 拍板:对齐写文章那条链的两档结构。
-- Safe to run repeatedly(ON CONFLICT DO UPDATE);生产已有 260 那行 → 走 UPDATE 语义。
--
-- 对齐依据(生产实测,不是猜的):
--   article_gen      GEO文章生成     390 / 3.00 / FALSE   ← 首次生成
--   article_rewrite  GEO文章补发/重写 260 / 3.00 / FALSE   ← 重新生成
-- 本次照抄这个两档结构:
--   geo_douyin_image_post        首次制作  390 / 3.00 / FALSE
--   geo_douyin_image_post_regen  重新生成  260 / 3.00 / FALSE
--
-- 390/3.00 同档位现有条目:article_gen / content_review / learn_viral /
--   mktg_poster_pro / single_video / video_framework —— 全是 390 + 3.00,量纲一致。
--
-- ⚠️ cost_compute 的语义我**没有验证**,只做对齐不做推断:
--    生产里它并不等于 cost_points/130(反例:article_rewrite 260/3.00、
--    report_regen 130/2.00),所以这里按 article_gen / article_rewrite 的实际取值照抄。
--    (我原先那条"cost_compute = cost_points/130"的断言是**假不变量**,本批已一并修掉。)
--
-- 🔴 本条只是【制作费】。发布费仍走发布链既有口径(成本 × markup 1.5 × 130),未改动。

INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES ('geo_douyin_image_post', 'GEO 图文制作（单条）', 390, 3.00, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET feature_name = EXCLUDED.feature_name,
    cost_points = EXCLUDED.cost_points,
    cost_compute = EXCLUDED.cost_compute,
    requires_paid_points = EXCLUDED.requires_paid_points,
    is_active = TRUE;

INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES ('geo_douyin_image_post_regen', 'GEO 图文重新生成（单条）', 260, 3.00, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET feature_name = EXCLUDED.feature_name,
    cost_points = EXCLUDED.cost_points,
    cost_compute = EXCLUDED.cost_compute,
    requires_paid_points = EXCLUDED.requires_paid_points,
    is_active = TRUE;
