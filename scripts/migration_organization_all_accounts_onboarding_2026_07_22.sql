-- Organization internal seats v3: all active owners + verified invite onboarding.
-- Additive only. No flag is enabled, no paid price is invented, and no message is sent.

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE organization_invites ADD COLUMN IF NOT EXISTS token_derivation_id UUID;
ALTER TABLE organization_invites ADD COLUMN IF NOT EXISTS access_policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE organization_invites ADD COLUMN IF NOT EXISTS access_policy_hash TEXT NOT NULL DEFAULT repeat('0',64);

ALTER TABLE organization_invites DROP CONSTRAINT IF EXISTS organization_invite_access_policy_snapshot_object;
ALTER TABLE organization_invites ADD CONSTRAINT organization_invite_access_policy_snapshot_object
  CHECK (jsonb_typeof(access_policy_snapshot)='object');
ALTER TABLE organization_invites DROP CONSTRAINT IF EXISTS organization_invite_access_policy_hash_shape;
ALTER TABLE organization_invites ADD CONSTRAINT organization_invite_access_policy_hash_shape
  CHECK (access_policy_hash ~ '^[0-9a-f]{64}$');
UPDATE organization_invites
SET access_policy_snapshot='{}'::jsonb,
    access_policy_hash='44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
WHERE access_policy_hash=repeat('0',64);
ALTER TABLE organization_invites
  ALTER COLUMN access_policy_hash
  SET DEFAULT '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a';


DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM organization_invites
    WHERE status='pending'
    GROUP BY target_kind,target_hmac HAVING COUNT(*) > 1
  ) THEN
    RAISE EXCEPTION 'organization invite migration found cross-organization pending target duplicates';
  END IF;
END $$;

-- @index-guard ux_org_invite_pending_target_global ON organization_invites unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_invite_pending_target_global' AND i.indrelid = to_regclass('public.organization_invites')) THEN
        NULL;  -- 已在 public.organization_invites 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_invite_pending_target_global' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_invite_pending_target_global 已存在但不在 public.organization_invites 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_invite_pending_target_global' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_invite_pending_target_global ON public.organization_invites (target_kind,target_hmac) WHERE status='pending';
    END IF;
END $idxguard$;
-- @index-guard ux_org_invite_token_derivation ON organization_invites unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_invite_token_derivation' AND i.indrelid = to_regclass('public.organization_invites')) THEN
        NULL;  -- 已在 public.organization_invites 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_invite_token_derivation' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_invite_token_derivation 已存在但不在 public.organization_invites 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_invite_token_derivation' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_invite_token_derivation ON public.organization_invites (token_derivation_id) WHERE token_derivation_id IS NOT NULL;
    END IF;
END $idxguard$;

-- Acceptance replay is part of the invitation state machine, not an optional
-- admin sidecar. Keep the definition byte-identical to the later governance
-- migration so either deployment order converges without widening semantics.
CREATE TABLE IF NOT EXISTS organization_invite_accept_receipts (
  invite_id BIGINT
    CONSTRAINT organization_invite_accept_receipts_pkey PRIMARY KEY
    CONSTRAINT organization_invite_accept_receipts_invite_id_fkey
    REFERENCES organization_invites(id),
  accepted_user_id INTEGER NOT NULL
    CONSTRAINT organization_invite_accept_receipts_accepted_user_id_fkey
    REFERENCES users(id),
  accepted_request_id TEXT NOT NULL
    CONSTRAINT organization_invite_accept_receipts_accepted_request_id_key UNIQUE
    CONSTRAINT organization_invite_accept_receipts_request_id_check
    CHECK (btrim(accepted_request_id) <> ''),
  accept_request_hash TEXT NOT NULL
    CONSTRAINT organization_invite_accept_receipts_hash_check
    CHECK (accept_request_hash ~ '^[0-9a-f]{64}$'),
  accepted_result JSONB NOT NULL
    CONSTRAINT organization_invite_accept_receipts_result_check
    CHECK (jsonb_typeof(accepted_result) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO organization_invite_accept_receipts(
  invite_id,accepted_user_id,accepted_request_id,accept_request_hash,accepted_result
)
SELECT i.id,m.user_id,'legacy-invite-accept-' || i.id::text,repeat('0',64),
       jsonb_build_object(
         'membership_id',i.accepted_membership_id,
         'organization_id',i.organization_id,
         'status','active'
       )
FROM organization_invites i
JOIN organization_memberships m ON m.id=i.accepted_membership_id
WHERE i.status='accepted'
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS organization_product_config_publications (
  id BIGSERIAL PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE CHECK (btrim(request_id) <> ''),
  publication_version BIGINT NOT NULL UNIQUE CHECK (publication_version >= 1),
  previous_publication_id BIGINT REFERENCES organization_product_config_publications(id),
  product_catalog_version_id BIGINT NOT NULL REFERENCES pricing_catalog_versions(id),
  product_catalog_entry_id BIGINT NOT NULL REFERENCES pricing_catalog_entries(id),
  included_seats INTEGER NOT NULL CHECK (included_seats >= 0),
  extra_seat_price_cents INTEGER NOT NULL DEFAULT 0 CHECK (extra_seat_price_cents = 0),
  paid_extra_seats_enabled BOOLEAN NOT NULL DEFAULT FALSE CHECK (paid_extra_seats_enabled = FALSE),
  operational BOOLEAN NOT NULL DEFAULT FALSE,
  config_hash TEXT NOT NULL CHECK (length(config_hash) = 64),
  request_payload_hash TEXT NOT NULL CHECK (length(request_payload_hash) = 64),
  actor_user_id INTEGER REFERENCES users(id),
  reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT organization_product_config_operational_shape CHECK (
    NOT operational OR included_seats > 0
  )
);
-- @index-guard ix_org_product_config_operational_version ON organization_product_config_publications plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ix_org_product_config_operational_version' AND i.indrelid = to_regclass('public.organization_product_config_publications')) THEN
        NULL;  -- 已在 public.organization_product_config_publications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ix_org_product_config_operational_version' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ix_org_product_config_operational_version 已存在但不在 public.organization_product_config_publications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ix_org_product_config_operational_version' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX ix_org_product_config_operational_version ON public.organization_product_config_publications (operational,publication_version DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_member_legacy_access_snapshots (
  id BIGSERIAL PRIMARY KEY,
  organization_id BIGINT NOT NULL REFERENCES organizations(id),
  membership_id BIGINT NOT NULL UNIQUE REFERENCES organization_memberships(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  scope_kind TEXT NOT NULL DEFAULT 'legacy_snapshot'
    CHECK (scope_kind IN ('legacy_snapshot','invite_operator_empty')),
  brand_ids JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(brand_ids)='array'),
  permission_version_before INTEGER NOT NULL CHECK (permission_version_before >= 0),
  snapshot_hash TEXT NOT NULL CHECK (length(snapshot_hash) = 64),
  status TEXT NOT NULL DEFAULT 'captured' CHECK (status IN ('captured','restored','retired')),
  captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  restored_at TIMESTAMPTZ,
  CONSTRAINT organization_legacy_snapshot_terminal_shape CHECK (
    (status='captured' AND restored_at IS NULL) OR
    (status IN ('restored','retired') AND restored_at IS NOT NULL)
  )
);
-- @index-guard idx_org_legacy_snapshot_user ON organization_member_legacy_access_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_legacy_snapshot_user' AND i.indrelid = to_regclass('public.organization_member_legacy_access_snapshots')) THEN
        NULL;  -- 已在 public.organization_member_legacy_access_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_legacy_snapshot_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_legacy_snapshot_user 已存在但不在 public.organization_member_legacy_access_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_legacy_snapshot_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_legacy_snapshot_user ON public.organization_member_legacy_access_snapshots (user_id,status,membership_id);
    END IF;
END $idxguard$;

-- The earlier seat release admitted existing accounts only when legacy commercial
-- history was empty. Existing live memberships therefore backfill an immutable
-- empty pre-membership scope; current organization assignment projections remain.
INSERT INTO organization_member_legacy_access_snapshots(
  organization_id,membership_id,user_id,scope_kind,brand_ids,
  permission_version_before,snapshot_hash,status
)
SELECT m.organization_id,m.id,m.user_id,'legacy_snapshot','[]'::jsonb,
       COALESCE(u.permission_version,0),
       '52ee072abd5da70ebdb7bd81c7615029f010b070e89394ad83ca89205dec2202',
       'captured'
FROM organization_memberships m
JOIN users u ON u.id=m.user_id
WHERE NOT m.is_owner AND m.status IN ('active','suspended','leaving')
ON CONFLICT(membership_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS organization_operator_accounts (
  id BIGSERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
  organization_id BIGINT NOT NULL REFERENCES organizations(id),
  membership_id BIGINT NOT NULL UNIQUE REFERENCES organization_memberships(id),
  invite_id BIGINT NOT NULL UNIQUE REFERENCES organization_invites(id),
  principal_user_id INTEGER NOT NULL REFERENCES users(id),
  target_kind TEXT NOT NULL CHECK (target_kind IN ('phone','email')),
  target_hmac TEXT NOT NULL CHECK (length(target_hmac) = 64),
  account_origin TEXT NOT NULL DEFAULT 'organization_invite'
    CHECK (account_origin='organization_invite'),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  retired_at TIMESTAMPTZ,
  retire_reason TEXT,
  CONSTRAINT organization_operator_terminal_shape CHECK (
    (status='active' AND retired_at IS NULL AND retire_reason IS NULL) OR
    (status='retired' AND retired_at IS NOT NULL AND btrim(retire_reason) <> '')
  )
);
-- @index-guard idx_org_operator_org_status ON organization_operator_accounts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_operator_org_status' AND i.indrelid = to_regclass('public.organization_operator_accounts')) THEN
        NULL;  -- 已在 public.organization_operator_accounts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_operator_org_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_operator_org_status 已存在但不在 public.organization_operator_accounts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_operator_org_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_operator_org_status ON public.organization_operator_accounts (organization_id,status,user_id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_invite_verification_challenges (
  id BIGSERIAL PRIMARY KEY,
  invite_id BIGINT NOT NULL REFERENCES organization_invites(id),
  invite_version BIGINT NOT NULL CHECK (invite_version >= 1),
  organization_id BIGINT NOT NULL REFERENCES organizations(id),
  target_kind TEXT NOT NULL CHECK (target_kind IN ('phone','email')),
  target_hmac TEXT NOT NULL CHECK (length(target_hmac) = 64),
  product_catalog_version_id BIGINT NOT NULL REFERENCES pricing_catalog_versions(id),
  code_hash TEXT NOT NULL CHECK (length(code_hash) = 64),
  code_key_version TEXT NOT NULL CHECK (btrim(code_key_version) <> ''),
  receipt_derivation_id UUID NOT NULL UNIQUE,
  receipt_hash TEXT CHECK (receipt_hash IS NULL OR length(receipt_hash) = 64),
  receipt_key_version TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','verified','expired','locked','cancelled','consumed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
  expires_at TIMESTAMPTZ NOT NULL,
  request_id TEXT NOT NULL UNIQUE CHECK (btrim(request_id) <> ''),
  verified_at TIMESTAMPTZ,
  consumed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT organization_invite_challenge_expiry CHECK (expires_at > created_at),
  CONSTRAINT organization_invite_challenge_terminal_shape CHECK (
    (status NOT IN ('verified','consumed') OR (verified_at IS NOT NULL AND receipt_hash IS NOT NULL AND receipt_key_version IS NOT NULL))
    AND (status <> 'consumed' OR consumed_at IS NOT NULL)
  ),
  CONSTRAINT organization_invite_challenge_request_unique UNIQUE (invite_id,request_id)
);
-- @index-guard idx_org_invite_challenge_expiry ON organization_invite_verification_challenges plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_invite_challenge_expiry' AND i.indrelid = to_regclass('public.organization_invite_verification_challenges')) THEN
        NULL;  -- 已在 public.organization_invite_verification_challenges 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_invite_challenge_expiry' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_invite_challenge_expiry 已存在但不在 public.organization_invite_verification_challenges 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_invite_challenge_expiry' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_invite_challenge_expiry ON public.organization_invite_verification_challenges (status,expires_at,id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_invite_delivery_outbox (
  id BIGSERIAL PRIMARY KEY,
  invite_id BIGINT NOT NULL REFERENCES organization_invites(id),
  challenge_id BIGINT REFERENCES organization_invite_verification_challenges(id),
  event_kind TEXT NOT NULL CHECK (event_kind IN ('invite_link','verification_code')),
  target_kind TEXT NOT NULL CHECK (target_kind IN ('phone','email')),
  target_hmac TEXT NOT NULL CHECK (length(target_hmac) = 64),
  delivery_ciphertext TEXT NOT NULL CHECK (btrim(delivery_ciphertext) <> ''),
  payload_ciphertext TEXT NOT NULL CHECK (btrim(payload_ciphertext) <> ''),
  encryption_key_version TEXT NOT NULL CHECK (btrim(encryption_key_version) <> ''),
  request_id TEXT NOT NULL UNIQUE CHECK (btrim(request_id) <> ''),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','sending','sent','retry','unknown','cancelled')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  provider_message_reference TEXT,
  last_error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  sent_at TIMESTAMPTZ,
  CONSTRAINT organization_invite_delivery_shape CHECK (
    (event_kind='invite_link' AND challenge_id IS NULL) OR
    (event_kind='verification_code' AND challenge_id IS NOT NULL)
  ),
  CONSTRAINT organization_invite_delivery_claim_shape CHECK (
    (status='sending' AND claim_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR
    (status<>'sending' AND claim_token IS NULL AND lease_expires_at IS NULL)
  ),
  CONSTRAINT organization_invite_delivery_sent_shape CHECK (
    status<>'sent' OR sent_at IS NOT NULL
  )
);
-- @index-guard idx_org_invite_delivery_claim ON organization_invite_delivery_outbox plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_invite_delivery_claim' AND i.indrelid = to_regclass('public.organization_invite_delivery_outbox')) THEN
        NULL;  -- 已在 public.organization_invite_delivery_outbox 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_invite_delivery_claim' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_invite_delivery_claim 已存在但不在 public.organization_invite_delivery_outbox 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_invite_delivery_claim' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_invite_delivery_claim ON public.organization_invite_delivery_outbox (status,next_attempt_at,id) WHERE status IN ('pending','retry','sending','unknown');
    END IF;
END $idxguard$;

-- Platform default launch policy. The seat count is an auditable catalog
-- seed, not a runtime constant; price and paid extras remain disabled.
-- An existing published operational zero-price policy wins. A prior
-- governance-only zero-seat fallback is archived and upgraded atomically.
DO $$
DECLARE
  version_id BIGINT;
  entry_id BIGINT;
  included INTEGER;
  extra_price INTEGER;
  paid_enabled BOOLEAN;
  is_operational BOOLEAN;
  publication_no BIGINT;
  previous_publication_id BIGINT;
  existing_publication_id BIGINT;
  old_version_id BIGINT;
  old_version_code TEXT;
  upgrade_version_code TEXT;
  publication_request_id TEXT;
  existing_config JSONB;
  catalog_created BOOLEAN := FALSE;
  catalog_upgraded BOOLEAN := FALSE;
  config JSONB := jsonb_build_object(
    'included_seats',8,
    'extra_seat_price_cents',0,
    'high_cost_approval_threshold_points',0,
    'invite_ttl_hours',72,
    'approval_ttl_hours',24,
    'verification_ttl_minutes',10,
    'verification_max_attempts',5,
    'operational',true,
    'paid_extra_seats_enabled',false,
    'policy_version','organization-basic-team-v1',
    'rate_limits',jsonb_build_object(
      'invite.create',jsonb_build_object('window_seconds',3600,'organization',100,'actor',30,'target',5,'ip',100),
      'invite.resend',jsonb_build_object('window_seconds',3600,'organization',100,'actor',30,'target',5,'ip',100),
      'invite.accept',jsonb_build_object('window_seconds',3600,'organization',100,'actor',30,'target',20,'ip',100),
      'invite.verify',jsonb_build_object('window_seconds',3600,'organization',100,'target',10,'ip',100),
      'invite.delivery_status',jsonb_build_object('window_seconds',3600,'organization',100,'target',60,'ip',100)
    )
  );
BEGIN
  SELECT v.id,e.id,v.version_code,e.source_ref_jsonb
    INTO version_id,entry_id,old_version_code,existing_config
  FROM pricing_catalog_versions v
  JOIN pricing_catalog_entries e ON e.version_id=v.id
  WHERE v.catalog_type='feature_consumption' AND v.scope_key='ORGANIZATION_SEATS'
    AND v.status='published' AND v.effective_to IS NULL
    AND e.product_code='organization_internal_seats'
  LIMIT 1
  FOR UPDATE OF v;

  IF version_id IS NOT NULL THEN
    config := existing_config;
    included := COALESCE((config->>'included_seats')::INTEGER,0);
    extra_price := COALESCE((config->>'extra_seat_price_cents')::INTEGER,0);
    paid_enabled := COALESCE((config->>'paid_extra_seats_enabled')::BOOLEAN,FALSE);
    is_operational := COALESCE((config->>'operational')::BOOLEAN,FALSE) AND included > 0;
    IF included < 0 OR extra_price <> 0 OR paid_enabled THEN
      RAISE EXCEPTION 'organization product config contains unapproved seats or price';
    END IF;
    IF is_operational
       AND NOT (COALESCE(config->'rate_limits','{}'::jsonb)
                ?& ARRAY['invite.create','invite.resend','invite.accept','invite.verify']) THEN
      RAISE EXCEPTION 'organization product config rate-limit provenance is incomplete';
    END IF;
    IF is_operational
       AND NOT (config->'rate_limits' ? 'invite.delivery_status') THEN
      -- Published catalog entries are immutable. Create a new version instead
      -- of changing the published row in place.
      old_version_id := version_id;
      upgrade_version_code := old_version_code || '-delivery-status-v1';
      config := jsonb_set(
        config,
        '{rate_limits,invite.delivery_status}',
        jsonb_build_object('window_seconds',3600,'organization',100,'target',60,'ip',100),
        TRUE
      );
      config := jsonb_set(config,'{policy_version}',to_jsonb(upgrade_version_code),TRUE);

      INSERT INTO pricing_catalog_versions(
        catalog_type,scope_key,version_code,status,effective_from,reason,
        calc_meta_jsonb,published_at,created_at,updated_at
      ) VALUES (
        'feature_consumption','ORGANIZATION_SEATS',upgrade_version_code,
        'draft',NULL,'versioned addition of invite delivery status rate limit',
        jsonb_build_object(
          'contract','owner-launch-2026-07-22',
          'operational',true,
          'paid_extras',false,
          'supersedes_version_id',old_version_id
        ),
        NULL,NOW(),NOW()
      ) RETURNING id INTO version_id;
      INSERT INTO pricing_catalog_entries(
        version_id,product_code,base_price_cents,multiplier_bps,final_price_cents,
        paid_points,bonus_points,cost_floor_cents,usage_example_version,source_ref_jsonb
      ) VALUES (
        version_id,'organization_internal_seats',0,10000,0,0,0,0,
        upgrade_version_code,config
      ) RETURNING id INTO entry_id;
      UPDATE pricing_catalog_versions
      SET status='archived',effective_to=NOW(),archived_at=NOW(),updated_at=NOW()
      WHERE id=old_version_id AND status='published' AND effective_to IS NULL;
      UPDATE pricing_catalog_versions
      SET status='published',effective_from=NOW(),published_at=NOW(),updated_at=NOW()
      WHERE id=version_id AND status='draft';
      catalog_upgraded := TRUE;
    END IF;
    IF NOT is_operational THEN
      UPDATE pricing_catalog_versions
      SET status='archived',effective_to=NOW(),archived_at=NOW(),updated_at=NOW()
      WHERE id=version_id AND status='published' AND effective_to IS NULL;
      version_id := NULL;
      entry_id := NULL;
      config := jsonb_build_object(
        'included_seats',8,
        'extra_seat_price_cents',0,
        'high_cost_approval_threshold_points',0,
        'invite_ttl_hours',72,
        'approval_ttl_hours',24,
        'verification_ttl_minutes',10,
        'verification_max_attempts',5,
        'operational',true,
        'paid_extra_seats_enabled',false,
        'policy_version','organization-basic-team-v1',
        'rate_limits',jsonb_build_object(
          'invite.create',jsonb_build_object('window_seconds',3600,'organization',100,'actor',30,'target',5,'ip',100),
          'invite.resend',jsonb_build_object('window_seconds',3600,'organization',100,'actor',30,'target',5,'ip',100),
          'invite.accept',jsonb_build_object('window_seconds',3600,'organization',100,'actor',30,'target',20,'ip',100),
          'invite.verify',jsonb_build_object('window_seconds',3600,'organization',100,'target',10,'ip',100),
          'invite.delivery_status',jsonb_build_object('window_seconds',3600,'organization',100,'target',60,'ip',100)
        )
      );
    END IF;
  END IF;

  IF version_id IS NULL THEN
    INSERT INTO pricing_catalog_versions(
      catalog_type,scope_key,version_code,status,effective_from,reason,
      calc_meta_jsonb,published_at,created_at,updated_at
    ) VALUES (
      'feature_consumption','ORGANIZATION_SEATS','organization-basic-team-v1',
      'draft',NULL,'platform default free internal team policy; paid extras disabled',
      '{"contract":"owner-launch-2026-07-22","operational":true,"paid_extras":false}'::jsonb,
      NULL,NOW(),NOW()
    ) RETURNING id INTO version_id;
    INSERT INTO pricing_catalog_entries(
      version_id,product_code,base_price_cents,multiplier_bps,final_price_cents,
      paid_points,bonus_points,cost_floor_cents,usage_example_version,source_ref_jsonb
    ) VALUES (
      version_id,'organization_internal_seats',0,10000,0,0,0,0,
      'organization-basic-team-v1',config
    ) RETURNING id INTO entry_id;
    UPDATE pricing_catalog_versions
    SET status='published',effective_from=NOW(),published_at=NOW(),updated_at=NOW()
    WHERE id=version_id AND status='draft';
    catalog_created := TRUE;
  END IF;

  included := COALESCE((config->>'included_seats')::INTEGER,0);
  extra_price := COALESCE((config->>'extra_seat_price_cents')::INTEGER,0);
  paid_enabled := COALESCE((config->>'paid_extra_seats_enabled')::BOOLEAN,FALSE);
  is_operational := COALESCE((config->>'operational')::BOOLEAN,FALSE) AND included > 0;
  IF NOT is_operational OR extra_price <> 0 OR paid_enabled THEN
    RAISE EXCEPTION 'organization launch policy must be operational, free, and disable paid extras';
  END IF;

  SELECT id INTO existing_publication_id
  FROM organization_product_config_publications
  WHERE product_catalog_version_id=version_id
    AND product_catalog_entry_id=entry_id
  ORDER BY publication_version DESC LIMIT 1;

  IF existing_publication_id IS NULL THEN
    publication_request_id := CASE
      WHEN catalog_created THEN 'builtin:organization-basic-team-v1'
      WHEN catalog_upgraded THEN 'migration:' || upgrade_version_code
      ELSE 'migration:adopt:' || old_version_code
    END;
    SELECT id INTO previous_publication_id
    FROM organization_product_config_publications
    WHERE request_id <> publication_request_id
    ORDER BY publication_version DESC LIMIT 1;
    SELECT COALESCE(MAX(publication_version),0)+1 INTO publication_no
    FROM organization_product_config_publications;

    INSERT INTO organization_product_config_publications(
      request_id,publication_version,previous_publication_id,
      product_catalog_version_id,product_catalog_entry_id,
      included_seats,extra_seat_price_cents,paid_extra_seats_enabled,operational,
      config_hash,request_payload_hash,reason
    ) VALUES (
      publication_request_id,publication_no,previous_publication_id,
      version_id,entry_id,included,0,FALSE,TRUE,
      encode(sha256(convert_to(config::TEXT,'UTF8')),'hex'),
      encode(sha256(convert_to((publication_request_id || ':' || version_id::TEXT || ':' || entry_id::TEXT),'UTF8')),'hex'),
      CASE WHEN catalog_upgraded
        THEN 'versioned immutable addition of invite delivery status rate limit'
        ELSE 'platform default free team entitlement; administrator may supersede by CAS'
      END
    ) ON CONFLICT(request_id) DO NOTHING;
  END IF;

  UPDATE organization_seat_entitlements
  SET status='superseded',effective_to=NOW()
  WHERE status='active' AND effective_to IS NULL
    AND product_catalog_version_id<>version_id;

  INSERT INTO organization_seat_entitlements(
    organization_id,product_catalog_version_id,product_catalog_entry_id,source_sku,
    entitled_seats,extra_seat_price_snapshot,effective_from,status,snapshot_hash
  )
  SELECT o.id,version_id,entry_id,'organization_internal_seats',included,
         jsonb_build_object(
           'price_cents',0,
           'paid_extra_seats_enabled',false,
           'catalog_version',COALESCE(config->>'policy_version','organization-basic-team-v1')
         ),
         NOW(),'active',encode(sha256(convert_to(config::TEXT,'UTF8')),'hex')
  FROM organizations o
  WHERE o.status<>'dissolved'
    AND NOT EXISTS (
      SELECT 1 FROM organization_seat_entitlements e
      WHERE e.organization_id=o.id AND e.status='active' AND e.effective_to IS NULL
    );
END $$;
INSERT INTO organization_schema_migrations(version,contract_freeze_sha,status,applied_at,details)
VALUES (
  'organization_internal_seats_all_accounts_2026_07_22_v3',
  '00bf26e642e63de3984af86249b122ac171641ab','applied',NOW(),
  '{"base":"dc02115cb2c2b977f5d56ea38a9dea1fe425a4a0","flags_enabled":false,"real_delivery":false}'::jsonb
)
ON CONFLICT(version) DO UPDATE SET
  contract_freeze_sha=EXCLUDED.contract_freeze_sha,status='applied',applied_at=NOW(),
  rolled_back_at=NULL,details=EXCLUDED.details;

COMMIT;
