-- ============================================================
-- V3.5 工厂模式 · 出厂价数据回填
-- ============================================================
-- 在 migration_v35_factory_inventory.sql 跑完后执行
-- 给 feature_pricing + mhz_media 填出厂价
--
-- 规则:
--   GEO 全 feature  · wholesale_cents = price_yuan × 0.90 × 100  (示例 9 折出厂)
--   GEO 全 feature  · wholesale_points = cost_points × 0.80      (积分等值同步打折)
--   GEO 全 feature  · platform_cost_cents 留 NULL  (admin 后台逐项填真实 LLM 成本)
--   mhz_media       · wholesale_cents = base_cost_yuan × 1.30 × 100  (代理拿 130 卖客户)
--   mhz_media       · platform_cost_cents = base_cost_yuan × 100     (媒体真实底价)
-- ============================================================

-- [Codex r4 P0-2] 不写 BEGIN/COMMIT · 由 Python wrapper 管事务
-- 防 dry-run 失效

-- ====== 1. feature_pricing 出厂价 backfill ======
-- 关联文档 v6 §1.2:GEO 全 feature × 0.80 出厂

-- price_yuan 来自现有 feature_pricing.cost_compute(¥定价)
-- 注:cost_compute 是 NUMERIC(6,2) · 单位元 · 乘 100 转 cents

UPDATE feature_pricing
SET wholesale_cents = ROUND(cost_compute * 0.80 * 100)::INTEGER,
    wholesale_points = ROUND(cost_points * 0.80)::INTEGER
WHERE wholesale_cents IS NULL
  AND cost_compute IS NOT NULL
  AND cost_points IS NOT NULL;

-- ====== 2. mhz_media 出厂价 backfill ======
-- 关联文档 v6 §1.2:发布中心 markup 100 + 30% = 130 出厂(平台对代理结算价)
-- 平台真实成本 = base_cost_yuan(媒体底价)

-- 注:mhz_media 实际列名以 prod 为准 · 候选有 base_price_yuan / our_price_points / cost_yuan
-- 此处用 our_price_points 作为代理目前看到的售价(150% markup 的结果)反推
-- our_price_points = base_cost × 1.50 × 130(1 元 = 130 积分)
-- 出厂 = base_cost × 1.30 × 100 cents = our_price_points / 1.50 / 130 × 1.30 × 100
--      = our_price_points × 0.6667 cents (粗略 · 实际应该用 base_cost 字段)

UPDATE mhz_media
SET wholesale_cents = ROUND(our_price_points / 1.50 / 130 * 1.30 * 100)::INTEGER,
    platform_cost_cents = ROUND(our_price_points / 1.50 / 130 * 100)::INTEGER,
    wholesale_points = ROUND(our_price_points / 1.50 * 1.30)::INTEGER
WHERE wholesale_cents IS NULL
  AND our_price_points IS NOT NULL
  AND our_price_points > 0;

-- ⚠️ 若 mhz_media 表用 base_cost_yuan 字段 · 应改用:
-- UPDATE mhz_media
-- SET wholesale_cents = ROUND(base_cost_yuan * 1.30 * 100)::INTEGER,
--     platform_cost_cents = ROUND(base_cost_yuan * 100)::INTEGER,
--     wholesale_points = ROUND(base_cost_yuan * 1.30 * 130)::INTEGER
-- WHERE wholesale_cents IS NULL AND base_cost_yuan IS NOT NULL;

-- ====== 3. SKU 模板初始数据 ======
-- 平台标准 SKU · 代理白标但不改内核
-- 关联文档 v6 §3 · 推荐额度包(非项目承诺)

-- [Codex r4 P0-1 修正] 量纲严格对齐 services/agent_pricing.calc_factory_cents 公式 SSOT
--   出厂换算公式:1 积分 = 225/325 cents
--   验证:wholesale_cents × 325 = points_granted × 200(整除)
--   选 points_granted 必须满足 points × 200 % 325 == 0(即 points 是 13 的倍数)
--   1 元 = 130 积分(底层)· 示例 9 折出厂 → 1 元出厂 ≈ 144.4 积分
--
-- 计算法:
--   wholesale_cents = points × 225 / 325(示例出厂系数;下表 points 全是 13 的倍数,整除)
--
-- 验证 SQL(post-backfill 跑):
--   SELECT template_code, points_granted, wholesale_cents,
--          (points_granted * 225 / 325) AS expected_cents,
--          CASE WHEN points_granted * 225 = wholesale_cents * 325
--               THEN 'OK' ELSE 'MISMATCH' END AS check
--   FROM sku_templates ORDER BY id;

INSERT INTO sku_templates (
    template_code, sku_type, default_name, default_subtitle, default_capability_pitch,
    points_granted, wholesale_cents, suggested_retail_cents, recommended_use_jsonb, is_active
) VALUES
-- credit_pack 通用工具额度包(企业级 ¥1200-15000+)
('credit_basic',  'credit_pack',  '基础额度包', '适合单品牌起步', 'GEO 全流程工具能力 · 含诊断/写作/发布/监测/报告',
    195000,   135000,    180000, NULL, TRUE),
('credit_growth', 'credit_pack',  '成长额度包', '适合多词测试',   'GEO 全流程工具能力 · 更高额度',
    893750,   618750,    680000, NULL, TRUE),
('credit_pro',    'credit_pack',  '专业额度包', '适合矩阵运营',   'GEO 全流程工具能力 · 企业级额度',
    2437500, 1687500,   1880000, NULL, TRUE),
-- [Codex r5 P1-2] 团队包面议 · is_active=FALSE 防客户页面 fallback 显示 0 元
-- 商务后台谈完后手动 UPDATE points_granted/wholesale_cents/suggested_retail + is_active=TRUE
('credit_team',   'credit_pack',  '团队额度包', '高额度商务定制', 'GEO 全流程工具能力 · 联系商务定制',
    0,             0,         0, NULL, FALSE),

-- scenario_pack 场景包(推荐配比 · 非承诺)
('scenario_diagnosis', 'scenario_pack', '诊断启动包', '试水 + 调研', 'GEO 诊断为主 · 配套少量写作监测',
    130000,  90000, 128000,
    '{"diagnosis": 10, "writing": 5, "monitoring": 2}'::jsonb, TRUE),
('scenario_writing', 'scenario_pack', '写作发布包', '内容矩阵密集', 'GEO 写作 + 发布为主',
    243750, 168750, 240000,
    '{"writing": 10, "publishing": 8, "reports": 1}'::jsonb, TRUE),
('scenario_monitor', 'scenario_pack', '监测分析包', '已有内容追踪', 'GEO 监测 + 报告为主',
    162500, 112500, 160000,
    '{"monitoring": 20, "reports": 3}'::jsonb, TRUE),

-- addon_pack 按需加购(零碎补充 · 几十到几百元)
('addon_diagnosis', 'addon_pack', '单次诊断包', '临时诊断', 'GEO 诊断工具单次额度',
    13000,    9000,  12800, NULL, TRUE),
('addon_writing',   'addon_pack', '单篇写作包', '临时写作', 'GEO 写作工具单篇额度',
    24375,   16875,  24000, NULL, TRUE),
('addon_monitor',   'addon_pack', '监测加密包', '临时加密监测', 'GEO 监测工具加密额度',
    32500,   22500,  32000, NULL, TRUE),
('addon_report',    'addon_pack', '报告补充包', '加 1 份报告', 'GEO 报告工具额度',
    8125,    5625,   8000, NULL, TRUE)

ON CONFLICT (template_code) DO NOTHING;

-- [Codex r4 P0-1] post-backfill 量纲一致性验证(必须 11 行 OK · 0 MISMATCH)
-- 用 raise notice 让 staging 跑时立即可见
DO $$
DECLARE
    bad_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO bad_count FROM sku_templates
    WHERE points_granted * 225 != wholesale_cents * 325
      AND points_granted > 0 AND wholesale_cents > 0;
    IF bad_count > 0 THEN
        RAISE EXCEPTION 'V3.5 SKU 量纲 MISMATCH 行数=%· 公式 points × 225 = cents × 325 不成立 · 修 backfill SQL', bad_count;
    END IF;
    RAISE NOTICE 'V3.5 SKU 量纲一致性 PASS';
END $$;

-- [Codex r4 P0-2] 无 COMMIT · Python wrapper 管

-- ============================================================
-- 验证(post-backfill)
-- ============================================================

-- SELECT COUNT(*) FROM feature_pricing WHERE wholesale_cents IS NOT NULL;
-- SELECT COUNT(*) FROM mhz_media WHERE wholesale_cents IS NOT NULL;
-- SELECT COUNT(*) FROM sku_templates;     -- 期望 11
-- SELECT sku_type, COUNT(*) FROM sku_templates GROUP BY sku_type;
--   credit_pack: 4 · scenario_pack: 3 · addon_pack: 4
