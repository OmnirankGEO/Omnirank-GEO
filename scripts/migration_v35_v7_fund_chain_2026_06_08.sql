-- ============================================================
-- V3.5 v7 资金链根治 migration · 2026-06-08(老板拍板 A/B/C)
-- 批 1C:利润换算力(agent_commission_redemption_requests + _items + inventory type)
-- 批 2B:客户长任务冻结(customer_credit_freezes)· 见本文件【批 2B】段
-- 幂等:全部 IF NOT EXISTS / DROP CONSTRAINT IF EXISTS · 可重跑
-- 红线 0(不动 billing/auth/connection/scoring)· Deploy 跑(prod 实证字段/约束名)
-- 0 数据迁移(新表空建)
-- ============================================================

-- ============================================================
-- 批 1C · 利润换算力(老板 A:出厂价等价进货 · 即时 · 无审核 · 无 fees/税)
-- 与提现共用 settled ledger(agent_revenue_ledger)· 防双花靠 agent_revenue.get_agent_balance
-- 的 available 同时减【提现锁 agent_settlement_request_items】+【换算力锁 本表 items】(SSOT)。
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_commission_redemption_requests (
    id BIGSERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    redeem_cents INTEGER NOT NULL CHECK (redeem_cents > 0),            -- 消耗的 settled 利润(cents)
    inventory_points_granted INTEGER NOT NULL CHECK (inventory_points_granted >= 0), -- 换得 paid_inventory 算力
    wholesale_numer INTEGER NOT NULL,                                 -- 兑换时锁定折扣(审计追溯·per-agent 可变)
    wholesale_denom INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'redeemed' CHECK (status IN ('redeemed','reversed')),
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_redemption_agent ON agent_commission_redemption_requests(agent_user_id, status);

-- 换算力明细(申请 ↔ ledger 多对多 · 部分锁 · 与 agent_settlement_request_items 同构)
CREATE TABLE IF NOT EXISTS agent_commission_redemption_items (
    id BIGSERIAL PRIMARY KEY,
    redemption_request_id BIGINT NOT NULL REFERENCES agent_commission_redemption_requests(id),
    ledger_id BIGINT NOT NULL REFERENCES agent_revenue_ledger(id),
    locked_amount_cents INTEGER NOT NULL CHECK (locked_amount_cents > 0),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ledger_id, redemption_request_id)
);
CREATE INDEX IF NOT EXISTS idx_redemption_items_request ON agent_commission_redemption_items(redemption_request_id);

-- agent_inventory_transactions.type 加 'purchase_from_commission'(利润换算力进货 · 不污染 prepay 统计)
-- 现有 type CHECK 为内联未命名 · PG 默认名 = agent_inventory_transactions_type_check
-- ⚠️ Deploy 跑前 `\d agent_inventory_transactions` 核约束名(若 prod 非默认名 · 改下方 DROP 目标)
-- 新 CHECK = 旧 8 值超集 + 新值 · 现有行不违反 · 向后兼容
ALTER TABLE agent_inventory_transactions DROP CONSTRAINT IF EXISTS agent_inventory_transactions_type_check;
ALTER TABLE agent_inventory_transactions ADD CONSTRAINT agent_inventory_transactions_type_check
    CHECK (type IN (
        'purchase_prepay',
        'purchase_auto',
        'purchase_admin_adjust',
        'purchase_from_commission',
        'allocate_to_customer',
        'allocate_to_customer_offline',
        'revoke_from_customer',
        'admin_adjust',
        'refund_clawback'
    ));

-- ============================================================
-- 批 1D · 提现 fees 透明(老板 B:提现时显示「提 X / 扣 Y / 到账 Z」三段)
-- fees 从 settled margin(R-factory)扣 · 不在 calc_settlement 每笔扣(批1A 已挪走)。
-- ⚠️ 老 commission_points 提现(withdrawal_db · V3.2)废除 = 独立后续 batch · 不在本批。
-- W2 铁律(schemas/v35_w2_dto.py):gateway/settlement 分项【仅 DB 审计列(admin 对账)】·
--   Agent API 合并成 platform_fee · tax 单列(金额)· 不露任何 *_bps。
-- 旧行 DEFAULT 0(历史申请无 fee 拆分 · 1A 前 ledger 已是净额)。本批消费 net 的展示层:
--   ✅ admin 财务列表 API(admin_factory_api · 财务按 net 打款 · COALESCE NULLIF 回落 gross)已接。
--   ⏳ admin 前端 SettlementsReview 显示 net + 服务商端到账展示 = 轮2 UI 接(部署前必接 · 否则财务按 gross 打款亏 fees)。
--   读 net 的展示层对历史 net=0 行【均须 COALESCE(NULLIF(net_amount_cents,0), request_amount_cents) 回落】。
-- ============================================================
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS gateway_fee_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS settlement_fee_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS tax_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS net_amount_cents INTEGER NOT NULL DEFAULT 0;

-- ============================================================
-- 批 2A · V3.5 客户工具退费分流(customer_credit_transactions.source 白名单加 'tool_fail_refund')
-- billing.refund_points 对 V3.5 客户走 customer_credit.refund_credit(source='tool_fail_refund')。
-- ⚠️ 对抗审 P0(2026-05-30 agent_rebate 同款根因):代码写新 source 必须【同批】更新 CHECK 白名单 ·
--    否则每笔 V3.5 退费 INSERT 被 DB 拒回滚 · 客户白扣额度且 refund_points 抛异常(调用方 try/except 吞掉 = 静默失败)。
-- 幂等:DROP IF EXISTS + ADD 含全部 7 值(原 6 + tool_fail_refund · 对齐 migration_source_check_agent_rebate_2026_05_30)。
-- ⚠️ Deploy 跑前 \d customer_credit_transactions 核约束名(prod 若手动非默认名先按名 DROP)· 资金/schema 批单独验单独放行。
-- ============================================================
ALTER TABLE customer_credit_transactions
    DROP CONSTRAINT IF EXISTS customer_credit_transactions_source_check;
ALTER TABLE customer_credit_transactions
    ADD CONSTRAINT customer_credit_transactions_source_check
    CHECK (source IN (
        'online_payment',
        'offline_allocation',
        'admin_adjust',
        'tool_consume',
        'refund_revoke',
        'agent_rebate',
        'tool_fail_refund'
    ));

-- ============================================================
-- 批 2B · V3.5 客户长任务冻结链(customer_credit_freezes)
-- V3.5 客户(customer_agent_credit_wallets·三池 tool/publish/bonus·无 frozen 列)长任务
-- (诊断/监测/GEO方案)原 billing.freeze_points 对其抛 503 → 本批补冻结链。
--
-- 设计 A(与 _refund_v35_customer_credit 对称·不动 wallet schema):
--   freeze = consume_credit 真扣三池(余额即降·占位)+ 落本表记各池拆分(status='frozen')
--   commit = 翻 status='committed'(钱已扣·坐实=不退·余额不动)
--   release = refund_credit 按拆分退回三池(source='tool_fail_refund'·已在 2A 白名单·免改 source CHECK)
--   → type/source CHECK 都【不用扩】(freeze 复用 consume·release 复用 refund·见 2A 段)。
--
-- 跨表路由(billing.commit_freeze/release_freeze 加可选 user_id):
--   user_id 传且 _is_v35_customer=True → 查本表;否则查 legacy point_freezes。
--   本表与 point_freezes 物理隔离·freeze_id 撞号不跨表误命中(只在 V3.5 分支查本表)。
--   freeze_sweeper 同步扫本表 zombie 兜底(防个别调用点漏传 user_id 致钱卡冻结)。
-- 幂等 IF NOT EXISTS·0 数据迁移·Deploy 跑前 prod 核字段。
-- ============================================================
CREATE TABLE IF NOT EXISTS customer_credit_freezes (
    id BIGSERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL REFERENCES users(id),   -- [P3-3] 外键(与 point_freezes.user_id 同口径)
    agent_user_id INTEGER NOT NULL REFERENCES users(id),
    feature_code TEXT NOT NULL,
    amount_total INTEGER NOT NULL CHECK (amount_total > 0),    -- [P3-2] 占位额必 > 0(=0 提前 return free 不落表)
    amount_tool INTEGER NOT NULL DEFAULT 0,      -- freeze 时从 tool_credit 扣的占位(release 原路退)
    amount_publish INTEGER NOT NULL DEFAULT 0,   -- publish-only 长任务占位(paid-only·退回 publish 池)
    amount_bonus INTEGER NOT NULL DEFAULT 0,     -- bonus_credit 扣的占位
    status TEXT NOT NULL DEFAULT 'frozen' CHECK (status IN ('frozen','committed','released')),
    task_ref TEXT,                                -- session_id / monitor_run_* / geoplan_task_* 定位
    brand_id INTEGER,
    reason TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    committed_at TIMESTAMP,
    released_at TIMESTAMP,
    -- [P3-2] 三池占位和守恒 = 总占位(consume_credit 按 publish-only 或 bonus→tool 扣·和恒等 cost)
    CONSTRAINT chk_ccf_pool_sum CHECK (amount_tool + amount_publish + amount_bonus = amount_total)
);
CREATE INDEX IF NOT EXISTS idx_ccf_customer_status ON customer_credit_freezes(customer_user_id, status);
CREATE INDEX IF NOT EXISTS idx_ccf_task_ref ON customer_credit_freezes(task_ref) WHERE task_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ccf_status_created ON customer_credit_freezes(status, created_at);  -- sweeper zombie 扫
-- [P2-3] 同客户同 task_ref 只允许一个活跃 frozen(防重试/重投 freeze 多行 · 配合 freeze_customer_credit 幂等查)
-- committed/released 历史不受约束(可多行)· task_ref NULL 不受约束(partial index)
CREATE UNIQUE INDEX IF NOT EXISTS uq_ccf_active_task ON customer_credit_freezes(customer_user_id, task_ref)
    WHERE task_ref IS NOT NULL AND status = 'frozen';
