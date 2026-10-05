-- ============================================================
-- v3.6 Migration · 新增 rewrite_gen feature_code
-- ============================================================
-- 作者: CTO-13.0 · 2026-04-19
-- 立项: docs/功能设计/feature_code_rewrite_gen_立项_v1.md
-- 背景: T4 (commit b6a2210) 临时复用 script_gen 给仿写扣费，副作用：
--   1. 钱包记录全显示"完整脚本生成"，无法区分写脚本 vs 仿写
--   2. 定价耦合，无法独立调价/促销
--   3. 运营统计被污染
-- 方案: 建独立 feature_code `rewrite_gen`，和 script_gen 同价(650)独立统计
-- ============================================================
-- 执行: docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v3_6_rewrite_gen.sql
-- 验证: SELECT feature_code, feature_name, cost_points FROM feature_pricing WHERE feature_code = 'rewrite_gen';
-- 回滚: DELETE FROM feature_pricing WHERE feature_code = 'rewrite_gen'; DELETE FROM _migration_markers WHERE marker = 'v3_6_rewrite_gen';
-- ============================================================
-- Deploy-CTO 2026-04-19 修正: 原 SQL 列名 (description/price_points/price_yuan) 与生产真实列名
--   (feature_name/cost_points/cost_compute) 不一致, 生产运行时 ERROR:
--   "column description of relation feature_pricing does not exist". 已对齐 db/wallet_db.py 的 seed 顺序.
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM _migration_markers WHERE marker = 'v3_6_rewrite_gen') THEN
        -- 新增扣费项（和 script_gen 同价同 paid-only 属性）
        INSERT INTO feature_pricing
            (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
        VALUES
            ('rewrite_gen', '爆款仿写', 650, 5.0, FALSE)
        ON CONFLICT (feature_code) DO NOTHING;

        -- 标记 migration
        INSERT INTO _migration_markers (marker, applied_at)
        VALUES ('v3_6_rewrite_gen', NOW());

        RAISE NOTICE 'v3.6 rewrite_gen feature_code 已加入';
    ELSE
        RAISE NOTICE 'v3.6 rewrite_gen 已存在，跳过';
    END IF;
END $$;
