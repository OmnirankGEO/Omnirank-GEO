-- P0.5 feature_pricing seed · quote_generate=400 积分(CTO-15.7 2026-04-24)
--
-- 老板批:
--   · 方案书生成 ¥400 积分(按钮级扣费 · 元指令 11)
--   · 扣费时机:cluster quote 成功生成后扣 · 失败不扣
--   · 价格源唯一:feature_pricing.feature_code='quote_generate'
--   · 前端用 <FeatureCostBadge code="quote_generate" />
--
-- 放 seed 而非 migration · 每次 deploy 可幂等跑

BEGIN;

-- 幂等 upsert · feature_code 唯一约束
-- [Deploy-CTO 2026-04-24 修复] 原 SQL 用 `points` 列名 · 真实 schema 是 `cost_points`
INSERT INTO feature_pricing (feature_code, feature_name, cost_points)
VALUES ('quote_generate', 'GEO 方案书生成', 400)
ON CONFLICT (feature_code)
DO UPDATE SET
    feature_name = EXCLUDED.feature_name,
    cost_points = EXCLUDED.cost_points;

COMMIT;

-- 验收:
-- SELECT feature_code, feature_name, cost_points FROM feature_pricing WHERE feature_code = 'quote_generate';
-- 期望:1 行 · cost_points=400
