-- ============================================================
-- D2-b · 代理自定返利配置(2026-05-30 · 工厂模式)
-- ============================================================
-- 代理给自己客户设返利(如"充值送 10%")· 返利积分从代理 bonus_inventory 出(代理成本)·
-- 丰俭由人:rate=0 不返 / rate 任意(护栏 0~100%)。
-- 取代废除的平台固定 15%/28%/5%(平台不再发任何固定返利)。
-- 幂等:IF NOT EXISTS · 既有 DB / fresh DB 都安全。

CREATE TABLE IF NOT EXISTS agent_rebate_config (
    agent_user_id               INTEGER PRIMARY KEY,
    enabled                     BOOLEAN NOT NULL DEFAULT FALSE,
    rebate_rate                 NUMERIC(5,4) NOT NULL DEFAULT 0,   -- 0.1000 = 充值积分的 10% 作返利
    max_rebate_points_per_order INTEGER,                            -- 可选单笔返利上限(NULL = 不限 · 仅受库存约束)
    created_at                  TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_rebate_rate_range CHECK (rebate_rate >= 0 AND rebate_rate <= 1.0)
);

COMMENT ON TABLE agent_rebate_config IS
  'D2-b 代理自定返利规则 · 返利积分从代理 bonus_inventory 出 · 丰俭由人 · 取代废除的平台固定返利';
COMMENT ON COLUMN agent_rebate_config.rebate_rate IS
  '返利比例(0~1.0)· 返利积分 = 客户充值获得积分 × rebate_rate · 从代理 bonus_inventory 扣';

-- 验证:
-- SELECT column_name, data_type FROM information_schema.columns WHERE table_name='agent_rebate_config';
