-- ============================================================
-- Channel tier incentive foundation (2026-06-28)
-- Wrapper owns transactions. Keep this SQL idempotent and transaction-free.
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_channel_tier_state (
    agent_user_id      INTEGER PRIMARY KEY,
    channel_tier       TEXT NOT NULL DEFAULT 'none'
        CHECK (channel_tier IN ('none','certified','preferred','strategic')),
    rolling_12m_yuan   NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (rolling_12m_yuan >= 0),
    is_founder         BOOLEAN NOT NULL DEFAULT FALSE,
    founder_rank       INTEGER CHECK (founder_rank IS NULL OR founder_rank >= 1),
    first_order_done   BOOLEAN NOT NULL DEFAULT FALSE,
    last_evaluated_at  TIMESTAMP,
    tier_effective_at  TIMESTAMP DEFAULT NOW(),
    created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_channel_tier_state_tier
    ON agent_channel_tier_state(channel_tier);

ALTER TABLE agent_channel_tier_state
    ADD COLUMN IF NOT EXISTS tier_override TEXT;

ALTER TABLE agent_channel_tier_state
    ADD COLUMN IF NOT EXISTS tier_override_until TIMESTAMP;

ALTER TABLE agent_channel_tier_state
    ADD COLUMN IF NOT EXISTS tier_override_by INTEGER;

ALTER TABLE agent_channel_tier_state
    ADD COLUMN IF NOT EXISTS tier_override_note TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint con
          JOIN pg_class rel ON rel.oid = con.conrelid
         WHERE rel.relname = 'agent_channel_tier_state'
           AND con.conname = 'agent_channel_tier_state_tier_override_check'
    ) THEN
        ALTER TABLE agent_channel_tier_state
            ADD CONSTRAINT agent_channel_tier_state_tier_override_check
            CHECK (tier_override IS NULL OR tier_override IN ('certified','preferred','strategic'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS agent_tier_change_log (
    id                BIGSERIAL PRIMARY KEY,
    agent_user_id     INTEGER NOT NULL,
    from_tier         TEXT
        CHECK (from_tier IS NULL OR from_tier IN ('none','certified','preferred','strategic')),
    to_tier           TEXT NOT NULL
        CHECK (to_tier IN ('none','certified','preferred','strategic')),
    direction         TEXT NOT NULL CHECK (direction IN ('upgrade','downgrade','init')),
    rolling_12m_yuan  NUMERIC(14,2) NOT NULL DEFAULT 0,
    trigger_source    TEXT NOT NULL DEFAULT 'cron'
        CHECK (trigger_source IN ('cron','purchase_event','admin_manual')),
    related_order_id  TEXT,
    idempotency_key   TEXT NOT NULL,
    note              TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tier_change_idem
    ON agent_tier_change_log(idempotency_key);

CREATE INDEX IF NOT EXISTS idx_tier_change_agent
    ON agent_tier_change_log(agent_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS founder_seats (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    used       INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
    cap        INTEGER NOT NULL DEFAULT 10 CHECK (cap >= 0),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CHECK (used <= cap)
);

INSERT INTO founder_seats (id, used, cap)
VALUES (1, 0, 10)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS bonus_grants (
    id                BIGSERIAL PRIMARY KEY,
    grant_key         TEXT NOT NULL,
    owner_type        TEXT NOT NULL CHECK (owner_type IN ('agent','customer')),
    owner_id          INTEGER NOT NULL,
    pool              TEXT NOT NULL DEFAULT 'bonus' CHECK (pool = 'bonus'),
    granted_points    BIGINT NOT NULL CHECK (granted_points > 0),
    consumed_points   BIGINT NOT NULL DEFAULT 0 CHECK (consumed_points >= 0),
    frozen_points     BIGINT NOT NULL DEFAULT 0 CHECK (frozen_points >= 0),
    grant_type        TEXT NOT NULL
        CHECK (grant_type IN ('tier_purchase','founder_first_order','allocate_from_grant','customer_order_bonus','admin_adjust')),
    tier_at_grant     TEXT,
    bonus_rate_used   NUMERIC(5,4),
    related_order_id  TEXT,
    parent_grant_id   BIGINT REFERENCES bonus_grants(id),
    status            TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','expired_frozen','renewed','fully_consumed')),
    granted_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMP NOT NULL,
    frozen_at         TIMESTAMP,
    renewed_at        TIMESTAMP,
    note              TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    CHECK (consumed_points + frozen_points <= granted_points)
);

DO $$
DECLARE
    stale_grant_type_check TEXT;
BEGIN
    SELECT con.conname
      INTO stale_grant_type_check
      FROM pg_constraint con
      JOIN pg_class rel ON rel.oid = con.conrelid
      JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
     WHERE rel.relname = 'bonus_grants'
       AND con.contype = 'c'
       AND pg_get_constraintdef(con.oid) ILIKE '%grant_type%'
       AND pg_get_constraintdef(con.oid) NOT ILIKE '%customer_order_bonus%'
     LIMIT 1;

    IF stale_grant_type_check IS NOT NULL THEN
        EXECUTE format('ALTER TABLE bonus_grants DROP CONSTRAINT %I', stale_grant_type_check);
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint con
          JOIN pg_class rel ON rel.oid = con.conrelid
         WHERE rel.relname = 'bonus_grants'
           AND con.contype = 'c'
           AND pg_get_constraintdef(con.oid) ILIKE '%grant_type%'
           AND pg_get_constraintdef(con.oid) ILIKE '%customer_order_bonus%'
           AND pg_get_constraintdef(con.oid) ILIKE '%admin_adjust%'
    ) THEN
        ALTER TABLE bonus_grants
            ADD CONSTRAINT bonus_grants_grant_type_check
            CHECK (grant_type IN (
                'tier_purchase',
                'founder_first_order',
                'allocate_from_grant',
                'customer_order_bonus',
                'admin_adjust'
            ));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_bonus_grants_key
    ON bonus_grants(grant_key);

CREATE INDEX IF NOT EXISTS idx_bonus_grants_owner_fifo
    ON bonus_grants(owner_type, owner_id, expires_at)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_bonus_grants_expiry_scan
    ON bonus_grants(expires_at)
    WHERE status = 'active';

INSERT INTO system_settings (key, value, value_type, description)
VALUES (
    'CHANNEL_TIER_ENABLED', 'false', 'string',
    '渠道激励总开关(三档自动升降、tier配货、创始席、赠送有效期FIFO);默认 false'
)
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE VIEW v_bonus_grant_reconcile AS
SELECT 'agent' AS owner_type,
       w.agent_user_id AS owner_id,
       w.bonus_inventory_points AS pool_balance,
       COALESCE(g.active_points, 0) AS grant_active_points,
       COALESCE(g.frozen_points, 0) AS grant_frozen_points
  FROM agent_inventory_wallets w
  LEFT JOIN (
        SELECT owner_id,
               SUM(granted_points - consumed_points - frozen_points)
                   FILTER (WHERE status = 'active') AS active_points,
               SUM(frozen_points) AS frozen_points
          FROM bonus_grants
         WHERE owner_type = 'agent'
         GROUP BY owner_id
  ) g ON g.owner_id = w.agent_user_id
UNION ALL
SELECT 'customer' AS owner_type,
       w.customer_user_id AS owner_id,
       w.bonus_credit_points AS pool_balance,
       COALESCE(g.active_points, 0) AS grant_active_points,
       COALESCE(g.frozen_points, 0) AS grant_frozen_points
  FROM customer_agent_credit_wallets w
  LEFT JOIN (
        SELECT owner_id,
               SUM(granted_points - consumed_points - frozen_points)
                   FILTER (WHERE status = 'active') AS active_points,
               SUM(frozen_points) AS frozen_points
          FROM bonus_grants
         WHERE owner_type = 'customer'
         GROUP BY owner_id
  ) g ON g.owner_id = w.customer_user_id;
