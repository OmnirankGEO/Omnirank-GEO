-- 提现功能 DDL: 银行卡 + 提现申请 + user_wallets KYC 扩展
-- 幂等，可重复执行

-- bank_cards table
CREATE TABLE IF NOT EXISTS bank_cards (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    card_holder TEXT NOT NULL,
    bank_name TEXT NOT NULL,
    card_number_encrypted TEXT NOT NULL,
    card_number_hmac TEXT NOT NULL,
    card_number_mask TEXT NOT NULL,
    phone TEXT NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bank_cards_user ON bank_cards(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bank_cards_hmac_active ON bank_cards(card_number_hmac) WHERE status = 'active';

-- withdrawal_requests table
CREATE TABLE IF NOT EXISTS withdrawal_requests (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    bank_card_id INTEGER NOT NULL REFERENCES bank_cards(id) ON DELETE RESTRICT,
    amount_yuan NUMERIC(10,2) NOT NULL,
    fee_yuan NUMERIC(10,2) NOT NULL,
    actual_yuan NUMERIC(10,2) NOT NULL,
    points_deducted BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reject_reason TEXT,
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TIMESTAMP,
    paid_at TIMESTAMP,
    external_tx_ref TEXT,
    idempotency_key UUID NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_wr_user ON withdrawal_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_wr_status ON withdrawal_requests(status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_wr_idempotency ON withdrawal_requests(idempotency_key);

-- KYC fields for old agents (已通过代理审核但没走提现 KYC 的用户)
ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS withdrawal_kyc_verified BOOLEAN DEFAULT FALSE;
ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS withdrawal_kyc_real_name TEXT;
