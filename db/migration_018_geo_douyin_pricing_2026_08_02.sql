-- GEO 抖音图文管线 v1 · 制作费进价目表(feature_pricing = 价目表 SSOT 唯一权威源)
-- Owner 2026-08-02 拍板:**260 算力/条**(我建议的 130 被否,理由=定价过低)
-- Safe to run repeatedly(ON CONFLICT DO UPDATE)。
--
-- 口径核对(生产实测,别猜):
--   * 260 积分 ÷ 130 积分/元 = ¥2 → `cost_compute = 2.00`,与同档条目
--     (geo_plan_unlock / team_analysis / mktg_poster_basic / trial_pass_apply)完全一致;
--   * `requires_paid_points = FALSE`:生成类特性的既有惯例(report_regen / article_rewrite /
--     geo_plan_unlock 全是 FALSE),即允许用赠送积分抵扣。
--     ⚠️ 与 `media_proxy_publish`(TRUE,只扣充值积分)不同 —— 那是**发布费**,不是本条。
--
-- 🔴 本条只是【制作费】(生图 + 文案)。**发布费是另一笔**,走发布链既有口径
--    (成本 × markup 1.5 × 130),不在本表新增条目,也未被本包改动。
--
-- 成本背书(实测单价,供日后复盘毛利):
--   生图 $0.006/张(1k) × 7.2 = ¥0.0432/张,默认 5 张 ≈ ¥0.216
--   文案 deepseek-v4-flash 官方 ¥1/M in + ¥2/M out,单次 ≈ ¥0.003
--   → 直接成本 ≈ ¥0.22/条,售 ¥2 → 毛利约 9 倍
--   (wholesale_* / platform_cost_cents 三列语义未核实,故留空走默认,不写未验证的值)

INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES ('geo_douyin_image_post', 'GEO 图文制作（单条）', 260, 2.00, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET feature_name = EXCLUDED.feature_name,
    cost_points = EXCLUDED.cost_points,
    cost_compute = EXCLUDED.cost_compute,
    requires_paid_points = EXCLUDED.requires_paid_points,
    is_active = TRUE;
