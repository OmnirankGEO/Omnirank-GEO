-- ============================================================
-- V3.5 GEO 工具额度工厂模式 · 代理库存 + 平台代收结算
-- ============================================================
-- 日期:2026-05-26
-- 关联文档:docs/AI-CONTEXT/V35_FACTORY_INVENTORY_MODEL_v6_2026-05-26.md
-- Codex 8 轮 review · v6 终稿
--
-- 改动概览:
--   新建 9 表  · 代理库存 / 客户授权 / 流水 / 收益台账 / 提现 / SKU 模板 / 税务 / 绑定审计
--   ALTER 4 表 · feature_pricing / mhz_media / recharge_orders / users
--   新增索引 17 个
--   全幂等(IF NOT EXISTS / ADD COLUMN IF NOT EXISTS)· 可重跑
--
-- ⚠️ 红线 check:
--   - middleware/billing.py 未动(仅扩 feature_pricing 列 · 核心扣费 cost_points 不变)
--   - user_wallets / point_transactions 不动(底层积分 SSOT 保留)
--   - 不删任何已有表 · 仅新增 + ALTER ADD COLUMN
--
-- [Codex r4 P0-2 修正] 不写 BEGIN/COMMIT
--   事务由 db/migrate_v35_factory_inventory.py wrapper 统一管理
--   防 --dry-run 模式下 SQL 内嵌 COMMIT 让外层 ROLLBACK 失效
-- ============================================================


-- ====== 1. feature_pricing 扩列(出厂价 SSOT) ======

ALTER TABLE feature_pricing ADD COLUMN IF NOT EXISTS wholesale_cents INTEGER;
ALTER TABLE feature_pricing ADD COLUMN IF NOT EXISTS wholesale_points INTEGER;
ALTER TABLE feature_pricing ADD COLUMN IF NOT EXISTS platform_cost_cents INTEGER;  -- admin only · API 永不外露

-- ====== 2. mhz_media 扩列(发布中心出厂价) ======

ALTER TABLE mhz_media ADD COLUMN IF NOT EXISTS wholesale_cents INTEGER;
ALTER TABLE mhz_media ADD COLUMN IF NOT EXISTS wholesale_points INTEGER;
ALTER TABLE mhz_media ADD COLUMN IF NOT EXISTS platform_cost_cents INTEGER;

-- ====== 3. users 加冗余快查 referred_by_agent_id ======

ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_agent_id INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS agent_bound_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS idx_users_referred_agent ON users(referred_by_agent_id);

-- ====== 4. recharge_orders 扩(代理归属 + 拆账快照) ======

ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS agent_user_id INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS sku_template_id INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS factory_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS agent_revenue_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS agent_margin_before_tax_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS gateway_fee_bps INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS gateway_fee_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS settlement_service_fee_bps INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS settlement_service_fee_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS tax_rate_bps INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS tax_withholding_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS pricing_snapshot_jsonb JSONB;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS settlement_mode TEXT;
    -- 'v35_inventory_settlement' / 'v32_legacy' / 'direct'
CREATE INDEX IF NOT EXISTS idx_recharge_agent ON recharge_orders(agent_user_id);
CREATE INDEX IF NOT EXISTS idx_recharge_settlement_mode ON recharge_orders(settlement_mode);

-- ====== 5. SKU 模板(平台标准 · 代理白标但不改内核) ======

CREATE TABLE IF NOT EXISTS sku_templates (
    id SERIAL PRIMARY KEY,
    template_code TEXT UNIQUE NOT NULL,
    sku_type TEXT NOT NULL CHECK (sku_type IN ('credit_pack','scenario_pack','addon_pack')),
    default_name TEXT NOT NULL,
    default_subtitle TEXT,
    default_capability_pitch TEXT,         -- 平台 "工具能力" 描述 · 代理不可改
    points_granted INTEGER NOT NULL,
    wholesale_cents INTEGER NOT NULL,
    suggested_retail_cents INTEGER,
    recommended_use_jsonb JSONB,           -- 场景包推荐配比(仅 UI 引导)
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sku_templates_type ON sku_templates(sku_type, is_active);

-- ====== 6. 代理白标 SKU 覆盖 ======

CREATE TABLE IF NOT EXISTS agent_sku_overrides (
    id SERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    sku_template_id INTEGER NOT NULL REFERENCES sku_templates(id),
    custom_name TEXT,
    custom_subtitle TEXT,
    custom_sales_pitch TEXT,               -- 追加 · 不覆盖标准
    retail_cents INTEGER NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    margin_warning TEXT,                   -- 'allowed' / 'admin_approval_required' / 'rejected'
    admin_approved_at TIMESTAMP,
    admin_approved_by INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(agent_user_id, sku_template_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_sku_overrides_agent ON agent_sku_overrides(agent_user_id, is_active);

-- ====== 7. 代理库存钱包(paid + bonus 双轨) ======

CREATE TABLE IF NOT EXISTS agent_inventory_wallets (
    agent_user_id INTEGER PRIMARY KEY,
    paid_inventory_points INTEGER NOT NULL DEFAULT 0,
    bonus_inventory_points INTEGER NOT NULL DEFAULT 0,
    frozen_inventory_points INTEGER NOT NULL DEFAULT 0,
    total_purchased_points BIGINT NOT NULL DEFAULT 0,
    total_allocated_points BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW(),
    CHECK (paid_inventory_points >= 0),
    CHECK (bonus_inventory_points >= 0),
    CHECK (frozen_inventory_points >= 0)
);

-- ====== 8. 代理库存流水(双向审计 trace) ======

CREATE TABLE IF NOT EXISTS agent_inventory_transactions (
    id BIGSERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN (
        'purchase_prepay',           -- 预付充值进货
        'purchase_auto',             -- 客户付款触发自动补库存
        'purchase_admin_adjust',     -- 对公转账 admin 入账
        'allocate_to_customer',      -- 划拨给客户(线上)
        'allocate_to_customer_offline', -- 线下划拨
        'revoke_from_customer',      -- 撤回未消费额度
        'admin_adjust',              -- admin 调账
        'refund_clawback'            -- 退款冲销
    )),
    pool TEXT NOT NULL CHECK (pool IN ('paid','bonus')),
    points INTEGER NOT NULL,                  -- 正/负
    balance_paid_after INTEGER NOT NULL,
    balance_bonus_after INTEGER NOT NULL,
    related_customer_user_id INTEGER,
    related_order_id TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_inv_tx_agent ON agent_inventory_transactions(agent_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_inv_tx_order ON agent_inventory_transactions(related_order_id);

-- ====== 9. 客户授权额度钱包(代理授权给客户的额度 · 非平台直营) ======
--   命名严格 · 不混淆 user_wallets 平台钱包

CREATE TABLE IF NOT EXISTS customer_agent_credit_wallets (
    customer_user_id INTEGER PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,            -- 客户主代理(归属)
    tool_credit_points INTEGER NOT NULL DEFAULT 0,    -- 工具(诊断/写作/监测/报告)
    publish_credit_points INTEGER NOT NULL DEFAULT 0, -- 发布额度(paid-only)
    bonus_credit_points INTEGER NOT NULL DEFAULT 0,   -- 赠送(仅工具 · 禁发布)
    total_purchased_points BIGINT NOT NULL DEFAULT 0,
    total_consumed_points BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW(),
    CHECK (tool_credit_points >= 0),
    CHECK (publish_credit_points >= 0),
    CHECK (bonus_credit_points >= 0)
);
CREATE INDEX IF NOT EXISTS idx_customer_agent_credit_agent ON customer_agent_credit_wallets(agent_user_id);

-- ====== 10. 客户额度流水 ======

CREATE TABLE IF NOT EXISTS customer_credit_transactions (
    id BIGSERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL,
    agent_user_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('allocate','consume','refund','revoke')),
    pool TEXT NOT NULL CHECK (pool IN ('tool','publish','bonus')),
    points INTEGER NOT NULL,                  -- 正=入账 · 负=消费
    balance_tool_after INTEGER NOT NULL,
    balance_publish_after INTEGER NOT NULL,
    balance_bonus_after INTEGER NOT NULL,
    feature_code TEXT,
    related_order_id TEXT,
    source TEXT CHECK (source IN ('online_payment','offline_allocation','admin_adjust','tool_consume','refund_revoke')),
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_customer_credit_tx_customer ON customer_credit_transactions(customer_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_customer_credit_tx_order ON customer_credit_transactions(related_order_id);

-- ====== 11. 客户归属审计表(终身唯一绑定) ======

CREATE TABLE IF NOT EXISTS customer_agent_bindings (
    id SERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL UNIQUE,
    agent_user_id INTEGER NOT NULL,
    binding_source TEXT NOT NULL,                -- 'qrcode' / 'ref_link' / 'invite_code' / 'admin_manual'
    source_token TEXT,                            -- 扫的二维码 / ref code
    bound_at TIMESTAMP DEFAULT NOW(),
    dispute_status TEXT,                         -- NULL / 'pending' / 'resolved' / 'reverted'
    dispute_note TEXT,
    admin_override_user_id INTEGER,
    admin_override_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_customer_bindings_agent ON customer_agent_bindings(agent_user_id);

-- ====== 12. 代理收益台账(3 层费率 + 税务 + 4 状态) ======

CREATE TABLE IF NOT EXISTS agent_revenue_ledger (
    id BIGSERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN (
        'recharge','refund_clawback','tax_refund','adjust'
    )),
    recharge_order_id TEXT,
    customer_user_id INTEGER,
    customer_paid_cents INTEGER NOT NULL,
    factory_cents INTEGER NOT NULL,
    gateway_fee_bps INTEGER NOT NULL DEFAULT 90,
    gateway_fee_cents INTEGER NOT NULL DEFAULT 0,
    settlement_service_fee_bps INTEGER NOT NULL DEFAULT 190,
    settlement_service_fee_cents INTEGER NOT NULL DEFAULT 0,
    agent_margin_before_tax_cents INTEGER NOT NULL,
    tax_rate_bps INTEGER NOT NULL DEFAULT 600,
    tax_mode TEXT NOT NULL DEFAULT 'withheld' CHECK (tax_mode IN ('withheld','invoice_provided','exempt_manual')),
    tax_withholding_cents INTEGER NOT NULL DEFAULT 0,
    invoice_status TEXT NOT NULL DEFAULT 'none' CHECK (invoice_status IN ('none','submitted','approved','rejected','refunded')),
    invoice_id INTEGER,
    agent_settlement_cents INTEGER NOT NULL,        -- 最终应付代理款
    -- [Codex r2 P0-1 修正] status 简化 frozen/settled/cancelled
    -- 提现锁定状态从 agent_settlement_request_items 推断 · 不再写回 ledger.status
    -- 防"部分提现锁整条" + "available 双算" + "mark_paid 误标未申请部分"
    -- [Codex r2 P0-3 修正] cancelled 表示已被退款冲销 · cron settle 跳过
    status TEXT NOT NULL DEFAULT 'frozen' CHECK (status IN ('frozen','settled','cancelled')),
    frozen_at TIMESTAMP DEFAULT NOW(),
    settle_at TIMESTAMP NOT NULL,
    settled_at TIMESTAMP,
    settlement_request_id INTEGER,                  -- deprecated · 仅历史 · 当前关联用 items 表
    manual_review_required BOOLEAN DEFAULT FALSE,
    -- [Codex r2 P0-3] 反向冲销标记 · 防 cron 把已退款 ledger 重复 settle
    reversed_at TIMESTAMP,
    reversed_by_ledger_id BIGINT,
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_agent ON agent_revenue_ledger(agent_user_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_settle_due ON agent_revenue_ledger(settle_at) WHERE status='frozen';
CREATE INDEX IF NOT EXISTS idx_agent_revenue_order ON agent_revenue_ledger(recharge_order_id);
CREATE INDEX IF NOT EXISTS idx_agent_revenue_reversed ON agent_revenue_ledger(reversed_by_ledger_id) WHERE reversed_by_ledger_id IS NOT NULL;

-- ====== 13. 代理结算申请(人工对账打款) ======

CREATE TABLE IF NOT EXISTS agent_settlement_requests (
    id SERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    request_amount_cents INTEGER NOT NULL,
    bank_name TEXT NOT NULL,
    bank_account TEXT NOT NULL,
    account_holder TEXT NOT NULL,
    invoice_required BOOLEAN DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','paid','rejected')),
    admin_note TEXT,
    transfer_proof_url TEXT,
    paid_at TIMESTAMP,
    paid_by_admin_user_id INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_settlement_requests_agent ON agent_settlement_requests(agent_user_id, status);
CREATE INDEX IF NOT EXISTS idx_settlement_requests_status ON agent_settlement_requests(status);

-- ====== 14. 提现申请明细(申请 ↔ ledger 多对多) ======

CREATE TABLE IF NOT EXISTS agent_settlement_request_items (
    id BIGSERIAL PRIMARY KEY,
    settlement_request_id INTEGER NOT NULL REFERENCES agent_settlement_requests(id),
    ledger_id BIGINT NOT NULL REFERENCES agent_revenue_ledger(id),
    locked_amount_cents INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ledger_id, settlement_request_id)
);
CREATE INDEX IF NOT EXISTS idx_settlement_items_request ON agent_settlement_request_items(settlement_request_id);

-- ====== 15. 代理税务身份(配置化税率) ======

CREATE TABLE IF NOT EXISTS agent_tax_profiles (
    agent_user_id INTEGER PRIMARY KEY,
    entity_type TEXT CHECK (entity_type IN ('individual','individual_business','company','partnership')),
    default_tax_rate_bps INTEGER,
    default_tax_mode TEXT CHECK (default_tax_mode IN ('withheld','invoice_provided','exempt_manual')),
    tax_id TEXT,
    invoice_capability TEXT CHECK (invoice_capability IN ('none','general_invoice','special_invoice')),
    notes TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ====== 16. 系统设置 · 费率配置 + feature flags ======

-- 费率配置(JSONB · 启动锚定 · 实际对账回填)
INSERT INTO system_settings (key, value, description)
VALUES (
    'platform_fee_config',
    '{
        "gateway_fee_bps": {"wechat_pay": 90, "alipay": 60, "huipi": 90, "manual": 0},
        "settlement_service_fee_bps": 190,
        "collection_fee_bps_display": 280,
        "tax_withholding_bps_default": 600
    }'::jsonb::text,
    'V3.5 工厂模式 · 费率配置(配置化 · 不写死)'
) ON CONFLICT (key) DO NOTHING;

-- 旧分润 feature flag(过渡期 30 天保留)
INSERT INTO system_settings (key, value, description)
VALUES (
    'LEGACY_REFERRAL_V32_ENABLED',
    'passthrough_30day',  -- 'passthrough_30day' / 'disabled' / 'rollback_full'
    'V3.2 老分润开关 · 30 天后切 disabled · 老 commission_points 可继续提现'
) ON CONFLICT (key) DO NOTHING;

-- V3.5 工厂模式总开关
INSERT INTO system_settings (key, value, description)
VALUES (
    'V35_FACTORY_INVENTORY_ENABLED',
    'false',  -- 启动 false · DB ready 后切 true · 完整测试后切 production
    'V3.5 代理库存 + 平台代收结算模式总开关'
) ON CONFLICT (key) DO NOTHING;

-- 代理库存预警阈值
INSERT INTO system_settings (key, value, description)
VALUES (
    'agent_inventory_alert_config',
    '{"low_warning_pct": 30, "critical_pct": 10, "block_pct": 0}'::jsonb::text,
    'V3.5 代理库存预警阈值 · 30% 通知 / 10% 强提醒 / 0% 阻断购买'
) ON CONFLICT (key) DO NOTHING;

-- [Codex r4 P0-2] 不写 COMMIT · 由 Python wrapper 管事务
-- 防 dry-run 失效

-- ============================================================
-- 验证查询(post-migration · Python wrapper 用)
-- ============================================================

-- 查询 1:9 张新表
-- SELECT COUNT(*) FROM information_schema.tables
--  WHERE table_name IN (
--    'sku_templates','agent_sku_overrides','agent_inventory_wallets',
--    'agent_inventory_transactions','customer_agent_credit_wallets',
--    'customer_credit_transactions','customer_agent_bindings',
--    'agent_revenue_ledger','agent_settlement_requests',
--    'agent_settlement_request_items','agent_tax_profiles'
--  )
-- 期望:11

-- 查询 2:feature_pricing 扩列
-- SELECT COUNT(*) FROM information_schema.columns
--  WHERE table_name='feature_pricing'
--    AND column_name IN ('wholesale_cents','wholesale_points','platform_cost_cents')
-- 期望:3

-- 查询 3:recharge_orders 扩列
-- SELECT COUNT(*) FROM information_schema.columns
--  WHERE table_name='recharge_orders'
--    AND column_name IN (
--      'agent_user_id','sku_template_id','factory_cents','agent_revenue_cents',
--      'agent_margin_before_tax_cents','gateway_fee_cents','settlement_service_fee_cents',
--      'tax_withholding_cents','pricing_snapshot_jsonb','settlement_mode'
--    )
-- 期望:10

-- 查询 4:system_settings 配置
-- SELECT COUNT(*) FROM system_settings
--  WHERE key IN (
--    'platform_fee_config','LEGACY_REFERRAL_V32_ENABLED',
--    'V35_FACTORY_INVENTORY_ENABLED','agent_inventory_alert_config'
--  )
-- 期望:4
