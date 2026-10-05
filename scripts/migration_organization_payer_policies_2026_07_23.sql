-- Organization payer policies: owner-consented shared wallet SSOT (2026-07-23).
-- Additive only. Every existing organization stays fully disabled; no broad
-- UPDATE runs and no historical consent is guessed. No feature flag changes.
--
-- DDL stays search_path-relative (same convention as the frozen internal-seats
-- migration) so the signed catalog fingerprint is byte-identical between the
-- production public schema and isolated verification schemas. Fresh guards use
-- to_regclass under current_schema() and fail closed on any missing base table.

-- Fresh guard: the frozen internal-seats schema must already exist.
DO $$
BEGIN
    IF to_regclass(format('%I.%I', current_schema(), 'organizations')) IS NULL
       OR to_regclass(format('%I.%I', current_schema(), 'organization_memberships')) IS NULL
       OR to_regclass(format('%I.%I', current_schema(), 'organization_charge_links')) IS NULL
       OR to_regclass(format('%I.%I', current_schema(), 'organization_schema_migrations')) IS NULL THEN
        RAISE EXCEPTION 'organization payer policies require the organization internal seats schema';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS organization_payer_policies (
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    shared_payer_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    overage_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    per_action_limit_points BIGINT CHECK (per_action_limit_points IS NULL OR per_action_limit_points >= 0),
    daily_limit_points BIGINT CHECK (daily_limit_points IS NULL OR daily_limit_points >= 0),
    monthly_limit_points BIGINT CHECK (monthly_limit_points IS NULL OR monthly_limit_points >= 0),
    policy_version INTEGER NOT NULL DEFAULT 1 CHECK (policy_version >= 1),
    updated_by_owner_user_id INTEGER REFERENCES users(id),
    enabled_at TIMESTAMPTZ,
    disabled_at TIMESTAMPTZ,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_organization_payer_policies PRIMARY KEY (organization_id),
    CONSTRAINT organization_payer_policy_enabled_limits_required CHECK (
        (NOT shared_payer_enabled AND NOT overage_enabled)
        OR (
            per_action_limit_points IS NOT NULL
            AND daily_limit_points IS NOT NULL
            AND monthly_limit_points IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS organization_payer_policy_events (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE CHECK (btrim(event_key) <> ''),
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    request_id TEXT NOT NULL CHECK (btrim(request_id) <> ''),
    actor_user_id INTEGER REFERENCES users(id),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('owner','platform_admin','system')),
    source_ip TEXT,
    old_snapshot JSONB,
    new_snapshot JSONB NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- @index-guard idx_org_payer_policy_events_org ON organization_payer_policy_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_payer_policy_events_org' AND i.indrelid = to_regclass('public.organization_payer_policy_events')) THEN
        NULL;  -- 已在 public.organization_payer_policy_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_payer_policy_events_org' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_payer_policy_events_org 已存在但不在 public.organization_payer_policy_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_payer_policy_events_org' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_payer_policy_events_org ON public.organization_payer_policy_events (organization_id,created_at DESC,id DESC);
    END IF;
END $idxguard$;

-- Append-only audit, same pattern as monitoring_identity_decision_events.
CREATE OR REPLACE FUNCTION organization_payer_policy_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'organization payer policy events are append-only';
END;
$fn$;

DROP TRIGGER IF EXISTS trg_org_payer_policy_events_append_only ON organization_payer_policy_events;
CREATE TRIGGER trg_org_payer_policy_events_append_only
BEFORE UPDATE OR DELETE ON organization_payer_policy_events
FOR EACH ROW EXECUTE FUNCTION organization_payer_policy_events_append_only();

-- Frozen per-charge payer evidence (contract §5): policy version, owner consent
-- snapshot, employee-limit snapshot, and the within-limit / overage leg split.
ALTER TABLE organization_charge_links ADD COLUMN IF NOT EXISTS payer_policy_version INTEGER;
ALTER TABLE organization_charge_links ADD COLUMN IF NOT EXISTS owner_consent_snapshot JSONB;
ALTER TABLE organization_charge_links ADD COLUMN IF NOT EXISTS employee_limit_snapshot JSONB;
ALTER TABLE organization_charge_links ADD COLUMN IF NOT EXISTS within_limit_points BIGINT NOT NULL DEFAULT 0;
ALTER TABLE organization_charge_links ADD COLUMN IF NOT EXISTS overage_points BIGINT NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid=format('%I.%I', current_schema(), 'organization_charge_links')::regclass
          AND conname='organization_charge_payer_split_valid' AND contype='c'
    ) THEN
        ALTER TABLE organization_charge_links
            ADD CONSTRAINT organization_charge_payer_split_valid
            CHECK (
                within_limit_points >= 0 AND overage_points >= 0
                AND within_limit_points + overage_points <= reserved_ceiling_points
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid=format('%I.%I', current_schema(), 'organization_charge_links')::regclass
          AND conname='organization_charge_overage_consent_required' AND contype='c'
    ) THEN
        ALTER TABLE organization_charge_links
            ADD CONSTRAINT organization_charge_overage_consent_required
            CHECK (
                overage_points = 0
                OR (payer_policy_version IS NOT NULL AND owner_consent_snapshot IS NOT NULL)
            ) NOT VALID;
    END IF;
END $$;
ALTER TABLE organization_charge_links VALIDATE CONSTRAINT organization_charge_payer_split_valid;
ALTER TABLE organization_charge_links VALIDATE CONSTRAINT organization_charge_overage_consent_required;

-- @index-guard idx_org_charge_overage_period ON organization_charge_links plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_charge_overage_period' AND i.indrelid = to_regclass('public.organization_charge_links')) THEN
        NULL;  -- 已在 public.organization_charge_links 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_charge_overage_period' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_charge_overage_period 已存在但不在 public.organization_charge_links 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_charge_overage_period' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_charge_overage_period ON public.organization_charge_links (organization_id,created_at) WHERE overage_points > 0;
    END IF;
END $idxguard$;

INSERT INTO organization_schema_migrations(version,contract_freeze_sha,status,applied_at,details)
VALUES (
    'organization_payer_policies_2026_07_23_v1',
    '00bf26e642e63de3984af86249b122ac171641ab',
    'applied',NOW(),
    '{"base":"0f05cc8522935a986ec1309cbe8d469230c7e21f","default_disabled":true,"broad_update":false,"flags_enabled":false}'::jsonb
)
ON CONFLICT(version) DO UPDATE SET
    contract_freeze_sha=EXCLUDED.contract_freeze_sha,
    status='applied',
    applied_at=NOW(),
    rolled_back_at=NULL,
    details=EXCLUDED.details;
