-- ============================================================
-- Phase 4 · seed · feature_pricing 新增 geo_plan_l1l2_fallback
-- ============================================================
-- 作者: CTO-15.5 · 2026-04-20
-- PRD: Section 4.5 GEO-REQ-BILLING-4
-- 用途: C 端 GEO 方案 data_mode='l1l2_fallback' 时扣 130 分 (防刷公共素材池)
-- 免费模式 data_mode='full' 不扣费,不需 seed
-- ============================================================
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/seed_feature_pricing_geo_plan_l1l2.sql
-- 验证:
--   docker exec omnirank-db psql -U geo_admin geo_agentscope -c \
--     "SELECT feature_code, feature_name, cost_points FROM feature_pricing WHERE feature_code='geo_plan_l1l2_fallback';"
-- 回滚:
--   DELETE FROM feature_pricing WHERE feature_code = 'geo_plan_l1l2_fallback';
-- ============================================================
-- 列名对齐 db/wallet_db.py seed_pricing_items 顺序:
--   feature_code / feature_name / cost_points / cost_compute / requires_paid_points
-- ============================================================

INSERT INTO feature_pricing
    (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES
    ('geo_plan_l1l2_fallback', 'GEO 方案 - 行业数据兜底', 130, 1.0, FALSE)
ON CONFLICT (feature_code) DO NOTHING;
