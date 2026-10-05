-- =================================================
-- 身份模型 V3.3.1 · DB Migration 007
-- 创建日期:2026-05-12
-- 作者:CTO-15.23
-- 关联文档:docs/DECISION/身份模型V3_C端社媒_GEO邀请制_2026-05-11.md
-- 红线:.planning/phases/07-social-studio-subscription/RED_LINES.md
-- 决策锁:.planning/phases/07-social-studio-subscription/PRICING/IDENTITY_DECISIONS_LOCK.md
--
-- 幂等 · 可重复跑 · 支持「全新部署」+「V3.1/V3.2 半成品」+「V3.2.1 已部署」环境
-- 回滚:db/rollback_007_identity_v3_3_1.sql
-- =================================================

BEGIN;

-- =================================================
-- 1. referral_codes 扩展(V3.2.1 保留 · 幂等)
-- =================================================

-- V3.3.1 修订(Codex 二审):
-- 老 referral_codes 表 user_id PRIMARY KEY · 一个用户一个码 · 无 id 字段
-- V3.3.1 需要一个用户多码(L1 月配额 10)· 必须建独立 invite_codes 新表
-- 老 referral_codes 保留兼容 V3.1 流程 · 不动其 PK / 不强加 V3.3.1 字段

CREATE TABLE IF NOT EXISTS invite_codes (
    id              BIGSERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    code            VARCHAR(40) UNIQUE NOT NULL,
    code_type       VARCHAR(20) DEFAULT 'user' NOT NULL,  -- 'user' / 'agent'
    expires_at      TIMESTAMP NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE NOT NULL,
    revoked_at      TIMESTAMP,
    revoked_reason  VARCHAR(100),
    used_by_user_id INTEGER,                              -- 被消费时下游 user_id
    used_at         TIMESTAMP,
    note            TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_invite_codes_user   ON invite_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_invite_codes_type   ON invite_codes(code_type);
CREATE INDEX IF NOT EXISTS idx_invite_codes_active ON invite_codes(is_active, expires_at) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_invite_codes_used   ON invite_codes(used_by_user_id) WHERE used_by_user_id IS NOT NULL;

-- =================================================
-- 2. 邀请码月度配额表(V3.2.1 保留 · 幂等)
-- =================================================

CREATE TABLE IF NOT EXISTS referral_code_quota (
    user_id       INTEGER NOT NULL,
    yyyymm        INTEGER NOT NULL,
    codes_issued  INTEGER DEFAULT 0,
    monthly_limit INTEGER NOT NULL,
    PRIMARY KEY (user_id, yyyymm)
);

-- =================================================
-- 3. pending_bonus_records 表(V3.2.1 保留 + V3.3.1 net_cash 字段)
-- =================================================

CREATE TABLE IF NOT EXISTS pending_bonus_records (
    id                 BIGSERIAL PRIMARY KEY,
    referrer_id        INTEGER NOT NULL,
    referred_id        INTEGER NOT NULL,
    bonus_points       BIGINT NOT NULL,
    rate               DECIMAL(5,4) DEFAULT 0.15,
    status             VARCHAR(20) DEFAULT 'pending',
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    settle_at          TIMESTAMP NOT NULL,
    settled_at         TIMESTAMP,
    cancel_reason      VARCHAR(100),
    is_first_recharge  BOOLEAN DEFAULT TRUE
);

-- V3.2.1 半成品兼容:补缺失字段
ALTER TABLE pending_bonus_records ADD COLUMN IF NOT EXISTS recharge_order_id      TEXT;
ALTER TABLE pending_bonus_records ADD COLUMN IF NOT EXISTS charger_user_id        INTEGER;
ALTER TABLE pending_bonus_records ADD COLUMN IF NOT EXISTS recharge_amount_cents  INTEGER;

-- V3.3.1 新增:净现金基数(§3.1.1)
ALTER TABLE pending_bonus_records ADD COLUMN IF NOT EXISTS net_cash_revenue_yuan DECIMAL(10,2);
ALTER TABLE pending_bonus_records ADD COLUMN IF NOT EXISTS source                 VARCHAR(40) DEFAULT 'external_cash_payment';

-- V3.2.1 UNIQUE 防重复
CREATE UNIQUE INDEX IF NOT EXISTS uniq_pending_bonus_order_referrer
  ON pending_bonus_records(recharge_order_id, referrer_id)
  WHERE recharge_order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pending_bonus_settle    ON pending_bonus_records(status, settle_at);
CREATE INDEX IF NOT EXISTS idx_pending_bonus_referrer  ON pending_bonus_records(referrer_id);
CREATE INDEX IF NOT EXISTS idx_pending_bonus_order     ON pending_bonus_records(recharge_order_id);

-- =================================================
-- 4. [V3.3.1 核心] service_fee_records(取代 pending_commissions)
-- =================================================

CREATE TABLE IF NOT EXISTS service_fee_records (
    id                            BIGSERIAL PRIMARY KEY,
    user_id                       INTEGER NOT NULL,                -- L2 受益人
    source_user_id                INTEGER NOT NULL,                -- 被推荐人(产生服务费的客户)
    source_order_id               TEXT NOT NULL,                   -- 关联充值/订阅订单
    source_order_type             VARCHAR(30) NOT NULL,            -- recharge / subscription
    source_payment_method         VARCHAR(20),                     -- wechat / alipay
    source_payment_source         VARCHAR(40) DEFAULT 'external_cash_payment',  -- §3.6 防套利

    -- V3.3.1 净现金基数(§3.1.1)
    gross_amount_yuan             DECIMAL(10,2),                   -- 订单总额(参考 · 不用于计算服务费)
    refund_amount_yuan            DECIMAL(10,2) DEFAULT 0,
    media_cost_yuan               DECIMAL(10,2) DEFAULT 0,
    coupon_yuan                   DECIMAL(10,2) DEFAULT 0,
    bonus_deducted_yuan           DECIMAL(10,2) DEFAULT 0,
    granted_points_deducted_yuan  DECIMAL(10,2) DEFAULT 0,
    gateway_fee_yuan              DECIMAL(10,2) DEFAULT 0,
    net_cash_revenue_yuan         DECIMAL(10,2),                   -- 服务费基数 = gross - 所有扣项

    -- 服务费
    amount_yuan                   DECIMAL(10,2) NOT NULL,          -- net_cash × rate
    service_fee_rate              DECIMAL(5,4) DEFAULT 0.22,

    -- 状态机 8 态(§3.2)
    status                        VARCHAR(30) DEFAULT 'pending',
        -- pending / settled / converted / withdraw_requested / withdrawn / cancelled / clawback / rejected

    -- 时间戳
    created_at                    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    available_at                  TIMESTAMP NOT NULL,              -- T+3 退款期结束
    settled_at                    TIMESTAMP,
    converted_at                  TIMESTAMP,
    withdraw_requested_at         TIMESTAMP,
    withdrawn_at                  TIMESTAMP,

    -- Clawback 信息
    clawback_amount               DECIMAL(10,2) DEFAULT 0,
    clawback_reason               VARCHAR(200),

    -- 财务审核
    review_status                 VARCHAR(20),                      -- pending / approved / rejected
    reviewed_by                   INTEGER,
    reviewed_at                   TIMESTAMP,
    review_note                   TEXT,

    -- V3.3.1 UNIQUE 3 列(防回调重试 + 防未来 fee_type 撞键)
    CONSTRAINT uniq_service_fee_order_type_user UNIQUE (source_order_id, source_order_type, user_id)
);

CREATE INDEX IF NOT EXISTS idx_service_fee_status      ON service_fee_records(status, available_at);
CREATE INDEX IF NOT EXISTS idx_service_fee_user        ON service_fee_records(user_id, status);
CREATE INDEX IF NOT EXISTS idx_service_fee_source_user ON service_fee_records(source_user_id);
CREATE INDEX IF NOT EXISTS idx_service_fee_review      ON service_fee_records(review_status) WHERE review_status='pending';

-- =================================================
-- 5. [V3.3.1 新增] service_fee_settlements(财务审核流水)
-- =================================================

CREATE TABLE IF NOT EXISTS service_fee_settlements (
    id                    BIGSERIAL PRIMARY KEY,
    user_id               INTEGER NOT NULL,
    settlement_type       VARCHAR(20) NOT NULL,         -- withdrawal / conversion
    related_record_ids    BIGINT[],                     -- service_fee_records ids
    amount_yuan           DECIMAL(10,2) NOT NULL,
    status                VARCHAR(20) DEFAULT 'pending',-- pending / approved / rejected / partial / completed

    -- 提现专属
    bank_account_masked   VARCHAR(50),
    invoice_required      BOOLEAN DEFAULT FALSE,
    invoice_uploaded      BOOLEAN DEFAULT FALSE,
    invoice_url           TEXT,
    requires_dual_sign    BOOLEAN DEFAULT FALSE,
    second_signer_id      INTEGER,
    second_signed_at      TIMESTAMP,

    -- 审核
    reviewed_by           INTEGER,
    reviewed_at           TIMESTAMP,
    review_note           TEXT,

    -- 完成
    completed_at          TIMESTAMP,
    settlement_evidence   TEXT,                         -- 财务转账凭证 URL
    partial_amount_yuan   DECIMAL(10,2),                -- 部分结算金额

    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_settlements_user        ON service_fee_settlements(user_id, status);
CREATE INDEX IF NOT EXISTS idx_settlements_status      ON service_fee_settlements(status, created_at);

-- Codex 三审 P1-5:partial unique 兜底 advisory lock · 同 user 最多 1 条 pending withdrawal
CREATE UNIQUE INDEX IF NOT EXISTS uniq_settlements_one_pending_withdrawal_per_user
    ON service_fee_settlements(user_id)
    WHERE settlement_type = 'withdrawal' AND status = 'pending';

-- =================================================
-- 6. [V3.3.1 新增] service_fee_conversion_orders(积分转换订单)
-- =================================================

CREATE TABLE IF NOT EXISTS service_fee_conversion_orders (
    id                       BIGSERIAL PRIMARY KEY,
    service_fee_record_ids   BIGINT[] NOT NULL,        -- 涉及的服务费记录(可多条合并)
    user_id                  INTEGER NOT NULL,
    amount_yuan              DECIMAL(10,2) NOT NULL,   -- 转换金额
    paid_points_granted      BIGINT NOT NULL,          -- amount_yuan × 130
    bonus_points_granted     BIGINT NOT NULL,          -- paid_points × bonus_rate
    bonus_rate               DECIMAL(5,4) DEFAULT 0.20,-- 转换时锁定(配置可调 · 不溯及已生成订单)
    bonus_expires_at         TIMESTAMP NOT NULL,       -- created_at + 90 days
    status                   VARCHAR(20) DEFAULT 'completed', -- completed(不可撤销) / clawback

    created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- 大额审批(Q21 超出月度额度走人工)
    requires_review          BOOLEAN DEFAULT FALSE,
    review_status            VARCHAR(20),              -- pending / approved / rejected
    reviewed_by              INTEGER,
    reviewed_at              TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversion_user   ON service_fee_conversion_orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversion_review ON service_fee_conversion_orders(review_status) WHERE review_status='pending';

-- =================================================
-- 7. [V3.3.1 新增] service_fee_conversion_quota(月度配额)
-- =================================================

CREATE TABLE IF NOT EXISTS service_fee_conversion_quota (
    user_id      INTEGER NOT NULL,
    yyyymm       INTEGER NOT NULL,
    quota_yuan   DECIMAL(10,2) NOT NULL,         -- 当月配额(L2 等级决定)
    used_yuan    DECIMAL(10,2) DEFAULT 0,        -- 已用
    tier         VARCHAR(20) DEFAULT 'standard', -- standard / premium / strategic
    PRIMARY KEY (user_id, yyyymm)
);

CREATE INDEX IF NOT EXISTS idx_quota_user ON service_fee_conversion_quota(user_id);

-- =================================================
-- 8. [V3.2.1 保留] commission UNIQUE 索引(老表过渡期防回调重试)
-- =================================================

DO $$ BEGIN
  CREATE UNIQUE INDEX IF NOT EXISTS uniq_pending_commission_order_user_level
    ON pending_commissions(order_id, user_id, level);
EXCEPTION WHEN undefined_table THEN NULL; END $$;

DO $$ BEGIN
  CREATE UNIQUE INDEX IF NOT EXISTS uniq_referral_bonus_order_referrer
    ON referral_bonus_records(order_id, referrer_id);
EXCEPTION WHEN undefined_table THEN NULL; END $$;

-- =================================================
-- 9. [V3.2.1 保留] bonus_clawback_pending 债务表
-- =================================================

CREATE TABLE IF NOT EXISTS bonus_clawback_pending (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    recharge_order_id   TEXT NOT NULL,
    amount_due          BIGINT NOT NULL,
    amount_settled      BIGINT DEFAULT 0,
    status              VARCHAR(20) DEFAULT 'pending',  -- pending / partial_settled / settled / written_off
    reason              VARCHAR(100),
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    settled_at          TIMESTAMP,
    UNIQUE (user_id, recharge_order_id)
);

CREATE INDEX IF NOT EXISTS idx_clawback_pending_user ON bonus_clawback_pending(user_id, status);

-- =================================================
-- 10. [V3.3.1 新增] service_fee_clawback_pending 债务表(异常退款)
-- =================================================

CREATE TABLE IF NOT EXISTS service_fee_clawback_pending (
    id                BIGSERIAL PRIMARY KEY,
    user_id           INTEGER NOT NULL,
    source_order_id   TEXT NOT NULL,
    amount_due        DECIMAL(10,2) NOT NULL,
    amount_settled    DECIMAL(10,2) DEFAULT 0,
    status            VARCHAR(20) DEFAULT 'pending',  -- pending / partial_settled / settled / written_off
    reason            VARCHAR(200),
    refund_category   VARCHAR(40),                    -- legal_dispute / platform_force / fraud / chargeback
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    settled_at        TIMESTAMP,
    UNIQUE (user_id, source_order_id)
);

CREATE INDEX IF NOT EXISTS idx_sfclawback_user ON service_fee_clawback_pending(user_id, status);

-- =================================================
-- 11. [V3.3.1] 3 个新 role · L0/L1/L2 + 财务
-- =================================================
-- ⚠️ Codex 八审 P0(2026-05-12 prod --force 实证修复):
-- prod roles 表 schema:display_name TEXT NOT NULL(无 default)
-- 若 INSERT 不传 display_name 会触发 psycopg2.errors.NotNullViolation
-- 修法:显式传 display_name + ON CONFLICT (name) DO UPDATE 保 idempotent
--      (重跑能 backfill 已存在但 display_name 仍缺 / 旧名的行)

INSERT INTO roles (name, display_name, description)
  VALUES ('geo_user_basic', 'GEO 用户', 'GEO 用户(单 brand · 无 CRM/白标)')
  ON CONFLICT (name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description  = EXCLUDED.description;

INSERT INTO roles (name, display_name, description)
  VALUES ('geo_agent_full', 'GEO 代理', 'GEO 代理(完整 CRM/白标 + 服务费可见)')
  ON CONFLICT (name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description  = EXCLUDED.description;

INSERT INTO roles (name, display_name, description)
  VALUES ('finance_reviewer', '财务审核员', 'V3.3.1 财务审核员(提现/转换审核)')
  ON CONFLICT (name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description  = EXCLUDED.description;

-- =================================================
-- 12. [V3.2.1 保留] 新手任务表 + verification_required
-- =================================================

CREATE TABLE IF NOT EXISTS new_user_tasks (
    id                       BIGSERIAL PRIMARY KEY,
    user_id                  INTEGER NOT NULL,
    task_key                 VARCHAR(50) NOT NULL,
    bonus_amount             INTEGER NOT NULL,
    completed_at             TIMESTAMP,
    bonus_granted_at         TIMESTAMP,
    created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    verification_required    JSONB,
    verification_passed_at   TIMESTAMP,
    UNIQUE (user_id, task_key)
);

CREATE INDEX IF NOT EXISTS idx_new_user_tasks_user ON new_user_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_new_user_tasks_pending_verify
  ON new_user_tasks(verification_required)
  WHERE bonus_granted_at IS NULL AND verification_required IS NOT NULL;

-- =================================================
-- 13. [V3.3.1 新增] 流水 source 字段(防重复服务费触发)
-- =================================================
-- 注:实际流水表是 point_transactions(db/wallet_db.py 已部 schema)
-- V3.3.1 在此加 source 字段 · 不新建独立 wallet_transactions 表

DO $$ BEGIN
  ALTER TABLE point_transactions ADD COLUMN IF NOT EXISTS source VARCHAR(40);
EXCEPTION WHEN undefined_table THEN NULL; END $$;

DO $$ BEGIN
  CREATE INDEX IF NOT EXISTS idx_pt_source ON point_transactions(source);
EXCEPTION WHEN undefined_table THEN NULL; END $$;

-- recharge_orders: source(默认 external_cash_payment)
DO $$ BEGIN
  ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS source VARCHAR(40) DEFAULT 'external_cash_payment';
EXCEPTION WHEN undefined_table THEN NULL; END $$;

-- subscription_orders: source(默认 external_cash_payment)· V3.1 已部时才生效
DO $$ BEGIN
  ALTER TABLE subscription_orders ADD COLUMN IF NOT EXISTS source VARCHAR(40) DEFAULT 'external_cash_payment';
EXCEPTION WHEN undefined_table THEN NULL; END $$;

-- =================================================
-- 14. [V3.3.1 新增] 用户身份扩展(L0/L1/L2 + KYC)
-- =================================================

DO $$ BEGIN
  ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS is_kyc_passed BOOLEAN DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN NULL; WHEN undefined_table THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS bank_account_verified BOOLEAN DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN NULL; WHEN undefined_table THEN NULL; END $$;

-- L2 代理等级(standard / premium / strategic · 决定转换月度配额)
DO $$ BEGIN
  ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS agent_tier VARCHAR(20) DEFAULT 'standard';
EXCEPTION WHEN duplicate_column THEN NULL; WHEN undefined_table THEN NULL; END $$;

-- =================================================
-- 15. [V3.3.1 新增] feature flags 表(默认 OFF · system_settings 兜底)
-- =================================================

CREATE TABLE IF NOT EXISTS system_settings (
    key          VARCHAR(100) PRIMARY KEY,
    value        TEXT,
    value_type   VARCHAR(20) DEFAULT 'string',  -- string / int / float / bool / json
    description  TEXT,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by   INTEGER
);

-- V3.3.1 默认 feature flags(全部 OFF · 不影响生产)
INSERT INTO system_settings (key, value, value_type, description) VALUES
    ('V3_3_1_ENABLED',                          'false', 'bool',   'V3.3.1 总开关 · false 时全部新逻辑禁用'),
    ('V3_3_1_DUAL_WRITE_OLD_TABLE',             'true',  'bool',   '灰度期同时双写 pending_commissions 老表'),
    ('V3_3_1_NET_CASH_REVENUE_BASE',            'false', 'bool',   '是否启用净现金收入基数(§3.1.1)'),
    ('V3_3_1_SERVICE_FEE_CONVERSION_ENABLED',   'false', 'bool',   '服务费转积分功能'),
    ('V3_3_1_WITHDRAWAL_ENABLED',               'false', 'bool',   '人工提现申请功能'),
    ('V3_3_1_RBAC_GATE_ENABLED',                'false', 'bool',   'GEO RBAC L0/L1/L2 路由 gate'),
    ('V3_3_1_INVITE_CODE_REQUIRED',             'false', 'bool',   'L1/L2 必须邀请码(Q2 灰度)'),
    ('V3_3_1_DISABLE_INSTANT_TRIAL_BONUS',      'true',  'bool',   '禁止注册立即发 3888(Codex 四审 P1-2 · 防黑产套利 · 分阶段释放实施前必须 true)'),
    ('V3_3_1_CRON_ROLE_GATE',                   'any',   'string', 'V3.3.1 cron 注册 ROLE gate · any/primary/active · 蓝绿防双跑(Codex 四审 P1-3)'),
    ('service_fee_conversion_bonus_rate',       '0.20',  'float',  '转积分赠送比例(Q18)'),
    ('service_fee_conversion_quota_default',    '5000',  'float',  '月度转换配额 普通 L2(Q21)'),
    ('service_fee_conversion_quota_premium',    '20000', 'float',  '月度转换配额 优质 L2(Q21)'),
    ('service_fee_conversion_min_age_days',     '7',     'int',    '转换前置退款期天数(Q20)'),
    ('service_fee_withdrawal_min_amount',       '100',   'float',  '提现单笔最低金额(Q13)'),
    ('service_fee_withdrawal_max_per_week',     '1',     'int',    '提现每周最多次数(Q13)'),
    ('service_fee_withdrawal_invoice_threshold','800',   'float',  '提现发票门槛 月累计(Q15)'),
    ('service_fee_withdrawal_dual_sign_threshold','1000','float',  '提现双签门槛(Q16)'),
    ('service_fee_rate',                        '0.22',  'float',  '服务费率(Q6)'),
    ('referral_bonus_rate',                     '0.15',  'float',  '推荐奖励率(Q7)'),
    ('referral_bonus_settle_days',              '7',     'int',    'bonus T+7 settle(Q8)'),
    ('service_fee_settle_days',                 '3',     'int',    'service_fee T+3 settle(Q8)'),
    ('l1_monthly_invite_quota',                 '10',    'int',    'L1 月度邀请码配额(Q4)')
ON CONFLICT (key) DO NOTHING;

-- =================================================
-- 16. [V3.3.1 新增 · Codex 三审 P1-4] 失败补偿队列
-- =================================================
-- 充值已成功但 V3.3.1 hook 异常时 · 持久化记录 · 由 cron 定期重试 · 财务可手动复查

CREATE TABLE IF NOT EXISTS failed_service_fee_jobs (
    id              BIGSERIAL PRIMARY KEY,
    source_order_id TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL,
    order_extras    JSONB,                       -- 完整 extras 重新调用用
    error_message   TEXT,
    error_class     VARCHAR(100),
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 3,
    status          VARCHAR(20) DEFAULT 'pending', -- pending / retrying / succeeded / abandoned
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_retry_at   TIMESTAMP,
    succeeded_at    TIMESTAMP,
    UNIQUE (source_order_id)
);

CREATE INDEX IF NOT EXISTS idx_failed_sf_jobs_status ON failed_service_fee_jobs(status, retry_count);
CREATE INDEX IF NOT EXISTS idx_failed_sf_jobs_user   ON failed_service_fee_jobs(user_id);

-- =================================================
-- 17. [V3.3.1 新增] L1 推荐奖励严禁字眼校验日志
-- =================================================

CREATE TABLE IF NOT EXISTS rbac_route_audit (
    id                BIGSERIAL PRIMARY KEY,
    user_id           INTEGER,
    user_role         VARCHAR(50),                  -- L0 / L1 / L2 / admin
    method            VARCHAR(10),
    path              TEXT,
    response_code     INTEGER,
    blocked_reason    VARCHAR(100),
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rbac_audit_user_path ON rbac_route_audit(user_id, path, created_at);
CREATE INDEX IF NOT EXISTS idx_rbac_audit_blocked   ON rbac_route_audit(blocked_reason) WHERE blocked_reason IS NOT NULL;

COMMIT;

-- =================================================
-- 验证 SQL(部署后跑)· Codex 七审 P2:同步 point_transactions 表名
-- 推荐用 `python -m db.migrate_v3_3_1 --verify`(自动化版)
-- 下面手工版可用于 Deploy-CTO ssh 临时验证
-- =================================================
-- 1. 验证 V3.3.1 7 张新表(含 invite_codes / failed_service_fee_jobs)
--   SELECT COUNT(*) FROM information_schema.tables
--    WHERE table_name IN ('service_fee_records','service_fee_settlements',
--      'service_fee_conversion_orders','service_fee_clawback_pending',
--      'service_fee_conversion_quota','invite_codes','failed_service_fee_jobs');
--   应返回 7
--
-- 2a. 验证 source 字段 · 必选 2 张表(point_transactions / recharge_orders)
--   SELECT column_name FROM information_schema.columns
--    WHERE table_name IN ('point_transactions','recharge_orders')
--      AND column_name='source';
--   应返回 2
--
-- 2b. 验证 source 字段 · 可选(subscription_orders · V3.1 未部时表本身不存在)
--   SELECT column_name FROM information_schema.columns
--    WHERE table_name='subscription_orders' AND column_name='source';
--   存在则应返回 1;不存在则 0(warning · 不算失败)
--
-- 3. 验证 V3.3.1 feature flags(含 P1-2 DISABLE_INSTANT_TRIAL_BONUS + P1-3 CRON_ROLE_GATE)
--   SELECT COUNT(*) FROM system_settings WHERE key LIKE 'V3_3_1_%' OR key LIKE 'service_fee_%' OR key='referral_bonus_rate';
--   应 >= 17
--
-- 4. 验证 UNIQUE 3 列 + partial unique
--   SELECT indexname FROM pg_indexes
--    WHERE indexname IN ('uniq_service_fee_order_type_user',
--                        'uniq_settlements_one_pending_withdrawal_per_user');
--   应返回 2
--
-- 5. 验证幂等(再跑一次 · 应 0 ERROR)
