-- ============================================================
-- V3.5 W3 · 客户面 + 旧入口 + 协议签约 schema
-- 2026-05-26 · idempotent · 无 BEGIN/COMMIT(Python wrapper 管事务)
-- ============================================================
-- 边界铁律(r12):
--   ✓ W3 拥有:agent_factory_agreements 表 + idx_recharge_sku 索引
--   ❌ W3 不拥 recharge_orders.sku_template_id 列(W1 主 migration line 46 已 ADD)
--     W3 仅依赖该列 + 加索引 · rollback 不能 DROP 该列
-- ============================================================

-- 1. 协议签约表(代理 V3.5 工厂模式 v2.1)
-- [r11 改名 agent_agreements → agent_factory_agreements 避开 partner v1.1 老表]
CREATE TABLE IF NOT EXISTS agent_factory_agreements (
    id SERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unsigned','signed','rejected','expired')),
    signed_at TIMESTAMP,
    signed_ip TEXT,
    signed_ua TEXT,
    content_hash TEXT,
    rejected_at TIMESTAMP,
    rejected_reason TEXT,
    expired_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(agent_user_id, version)
);
CREATE INDEX IF NOT EXISTS idx_agent_factory_agreements_agent ON agent_factory_agreements(agent_user_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_factory_agreements_version ON agent_factory_agreements(version, status);

-- 2. W3 索引 recharge_orders.sku_template_id
-- ⚠️ 列本身由 W1 主 migration 创建(scripts/migration_v35_factory_inventory_2026_05_26.sql:46)
--    W3 仅新建索引 · 不重复 ADD COLUMN(W1 已 IF NOT EXISTS)
CREATE INDEX IF NOT EXISTS idx_recharge_sku ON recharge_orders(sku_template_id);

-- 3. 验证(失败立即 RAISE · r12 升级列级断言)
DO $$
DECLARE
    v_count INTEGER;
    v_col_count INTEGER;
BEGIN
    -- 3.1 表存在
    SELECT COUNT(*) INTO v_count FROM information_schema.tables
     WHERE table_name = 'agent_factory_agreements';
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'W3 migration FAILED: agent_factory_agreements 表未建';
    END IF;

    -- 3.2 [r12 P1] 列级断言 · 防表名同 schema 异(吸取 partner agent_agreements 撞名教训)
    --     5 个关键列:agent_user_id / status / content_hash / signed_ip / signed_ua
    SELECT COUNT(*) INTO v_col_count FROM information_schema.columns
     WHERE table_name = 'agent_factory_agreements'
       AND column_name IN ('agent_user_id','status','content_hash','signed_ip','signed_ua');
    IF v_col_count <> 5 THEN
        RAISE EXCEPTION 'W3 migration FAILED: agent_factory_agreements 5 关键列未全 · 实际 %', v_col_count;
    END IF;

    -- 3.3 status CHECK 4 状态枚举(unsigned/signed/rejected/expired)间接验证 · 写入异值会被 CHECK 拦
    --     W3 不 verify enum 内容(pg_constraint 查询复杂)· 用业务层测试覆盖

    -- 3.4 W1 资产依赖:recharge_orders.sku_template_id 必须存在(否则 idx_recharge_sku 无意义)
    SELECT COUNT(*) INTO v_count FROM information_schema.columns
     WHERE table_name = 'recharge_orders' AND column_name = 'sku_template_id';
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'W3 migration FAILED: recharge_orders.sku_template_id 缺失(应由 W1 主 migration 提供)';
    END IF;

    -- 3.5 索引存在(W3 独占)
    SELECT COUNT(*) INTO v_count FROM pg_indexes
     WHERE indexname = 'idx_recharge_sku';
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'W3 migration FAILED: idx_recharge_sku 索引未建';
    END IF;

    RAISE NOTICE 'V35 W3 migration verified OK(table + 5 cols + W1 dep + index)';
END $$;
