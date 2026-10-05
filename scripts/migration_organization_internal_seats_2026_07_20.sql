-- OmniRank organization internal seats · additive migration
-- Semantic development base only (BASE_REANCHOR_REQUIRED): 1f874d769118f73dff04acdc32f396987e6f6ee9
-- Contract freeze: 5bd56f8112249ba6ac31f98a3c453ef6c41b30a2
-- PostgreSQL 16. ADDITIVE ONLY: no DROP/TRUNCATE/RENAME and no destructive backfill.

BEGIN;

-- Email invitation acceptance must bind a verification event to the exact
-- current address.  A generic sticky users.email_verified boolean is not
-- sufficient because the self-service profile can change email.
-- Production prestart does not import db.auth_db before this migration, so
-- the migration must also provide the legacy auth column it consumes.
ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_for TEXT;
UPDATE users
SET email_verified_for=lower(btrim(email))
WHERE email_verified IS TRUE AND email IS NOT NULL
  AND btrim(email)<>'' AND email_verified_for IS NULL;

CREATE TABLE IF NOT EXISTS organization_schema_migrations (
    version TEXT PRIMARY KEY,
    contract_freeze_sha TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applied','rolled_back')),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rolled_back_at TIMESTAMPTZ,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS organizations (
    id BIGSERIAL PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    creation_request_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    status TEXT NOT NULL DEFAULT 'active'
        CONSTRAINT organizations_status_valid CHECK (status IN ('active','suspended','dissolved')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    authority_version BIGINT NOT NULL DEFAULT 1 CHECK (authority_version >= 1),
    billing_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai' CHECK (btrim(billing_timezone) <> ''),
    suspended_at TIMESTAMPTZ,
    dissolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organizations_owner_unique UNIQUE (owner_user_id),
    CONSTRAINT organizations_status_times CHECK (
        (status <> 'suspended' OR suspended_at IS NOT NULL)
        AND (status <> 'dissolved' OR dissolved_at IS NOT NULL)
    )
);

ALTER TABLE IF EXISTS pricing_catalog_entries
    ADD COLUMN IF NOT EXISTS source_ref_jsonb JSONB;

CREATE TABLE IF NOT EXISTS organization_seat_entitlements (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    product_catalog_version_id BIGINT NOT NULL REFERENCES pricing_catalog_versions(id),
    product_catalog_entry_id BIGINT NOT NULL REFERENCES pricing_catalog_entries(id),
    source_sku TEXT NOT NULL CHECK (btrim(source_sku) <> ''),
    entitled_seats INTEGER NOT NULL CHECK (entitled_seats >= 0),
    extra_seat_price_snapshot JSONB,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active'
        CONSTRAINT organization_seat_entitlements_status_valid
        CHECK (status IN ('active','superseded','expired')),
    snapshot_hash TEXT NOT NULL CHECK (length(snapshot_hash) = 64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_seat_entitlements_window_valid
        CHECK (effective_to IS NULL OR effective_to > effective_from)
);
-- @index-guard ux_org_seat_entitlement_active ON organization_seat_entitlements unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_seat_entitlement_active' AND i.indrelid = to_regclass('public.organization_seat_entitlements')) THEN
        NULL;  -- 已在 public.organization_seat_entitlements 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_seat_entitlement_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_seat_entitlement_active 已存在但不在 public.organization_seat_entitlements 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_seat_entitlement_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_seat_entitlement_active ON public.organization_seat_entitlements (organization_id) WHERE status='active' AND effective_to IS NULL;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_roles (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    code TEXT NOT NULL CHECK (code ~ '^[a-z][a-z0-9_]{1,63}$'),
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    is_owner_role BOOLEAN NOT NULL DEFAULT FALSE,
    is_system_role BOOLEAN NOT NULL DEFAULT FALSE,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_roles_code_unique UNIQUE (organization_id, code),
    CONSTRAINT organization_roles_kind_valid CHECK (NOT (is_owner_role AND is_system_role))
);
-- @index-guard ux_org_owner_role ON organization_roles unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_owner_role' AND i.indrelid = to_regclass('public.organization_roles')) THEN
        NULL;  -- 已在 public.organization_roles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_owner_role' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_owner_role 已存在但不在 public.organization_roles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_owner_role' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_owner_role ON public.organization_roles (organization_id) WHERE is_owner_role;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_memberships (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role_id BIGINT NOT NULL REFERENCES organization_roles(id),
    status TEXT NOT NULL DEFAULT 'active'
        CONSTRAINT organization_memberships_status_valid
        CHECK (status IN ('active','suspended','leaving','left','removed')),
    is_owner BOOLEAN NOT NULL DEFAULT FALSE,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    capability_version BIGINT NOT NULL DEFAULT 1 CHECK (capability_version >= 1),
    assignment_version BIGINT NOT NULL DEFAULT 1 CHECK (assignment_version >= 1),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    suspended_at TIMESTAMPTZ,
    leaving_at TIMESTAMPTZ,
    left_at TIMESTAMPTZ,
    removed_at TIMESTAMPTZ,
    removed_by_user_id INTEGER REFERENCES users(id),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_memberships_owner_shape CHECK (NOT is_owner OR status IN ('active','suspended'))
);
-- @index-guard ux_org_membership_live_user ON organization_memberships unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_membership_live_user' AND i.indrelid = to_regclass('public.organization_memberships')) THEN
        NULL;  -- 已在 public.organization_memberships 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_membership_live_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_membership_live_user 已存在但不在 public.organization_memberships 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_membership_live_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_membership_live_user ON public.organization_memberships (user_id) WHERE status IN ('active','suspended','leaving');
    END IF;
END $idxguard$;
-- @index-guard ux_org_membership_owner ON organization_memberships unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_membership_owner' AND i.indrelid = to_regclass('public.organization_memberships')) THEN
        NULL;  -- 已在 public.organization_memberships 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_membership_owner' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_membership_owner 已存在但不在 public.organization_memberships 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_membership_owner' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_membership_owner ON public.organization_memberships (organization_id) WHERE is_owner;
    END IF;
END $idxguard$;
-- @index-guard idx_org_memberships_org_status ON organization_memberships plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_memberships_org_status' AND i.indrelid = to_regclass('public.organization_memberships')) THEN
        NULL;  -- 已在 public.organization_memberships 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_memberships_org_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_memberships_org_status 已存在但不在 public.organization_memberships 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_memberships_org_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_memberships_org_status ON public.organization_memberships (organization_id,status,id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_invites (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    target_kind TEXT NOT NULL CHECK (target_kind IN ('phone','email')),
    target_hmac TEXT NOT NULL CHECK (length(target_hmac) = 64),
    target_hmac_key_version TEXT NOT NULL CHECK (btrim(target_hmac_key_version) <> ''),
    delivery_ciphertext_or_reference TEXT NOT NULL CHECK (btrim(delivery_ciphertext_or_reference) <> ''),
    token_hash TEXT NOT NULL CHECK (length(token_hash) = 64),
    token_key_version TEXT NOT NULL CHECK (btrim(token_key_version) <> ''),
    role_id BIGINT NOT NULL REFERENCES organization_roles(id),
    role_version BIGINT NOT NULL CHECK (role_version >= 1),
    capability_snapshot_hash TEXT NOT NULL CHECK (length(capability_snapshot_hash) = 64),
    seat_entitlement_id BIGINT NOT NULL REFERENCES organization_seat_entitlements(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CONSTRAINT organization_invites_status_valid
        CHECK (status IN ('pending','accepted','revoked','expired')),
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_membership_id BIGINT REFERENCES organization_memberships(id),
    accepted_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    resend_count INTEGER NOT NULL DEFAULT 0 CHECK (resend_count >= 0),
    last_resend_request_id TEXT,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    request_id TEXT NOT NULL,
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_invites_token_unique UNIQUE (token_hash),
    CONSTRAINT organization_invites_request_unique UNIQUE (organization_id, request_id),
    CONSTRAINT organization_invites_expiry_valid CHECK (expires_at > created_at),
    CONSTRAINT organization_invites_terminal_shape CHECK (
        (status <> 'accepted' OR (accepted_membership_id IS NOT NULL AND accepted_at IS NOT NULL))
        AND (status <> 'revoked' OR revoked_at IS NOT NULL)
    )
);
ALTER TABLE organization_invites
    ADD COLUMN IF NOT EXISTS last_resend_request_id TEXT;
-- @index-guard ux_org_invites_resend_request ON organization_invites unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_invites_resend_request' AND i.indrelid = to_regclass('public.organization_invites')) THEN
        NULL;  -- 已在 public.organization_invites 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_invites_resend_request' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_invites_resend_request 已存在但不在 public.organization_invites 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_invites_resend_request' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_invites_resend_request ON public.organization_invites (organization_id,last_resend_request_id) WHERE last_resend_request_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard ux_org_invite_pending_target ON organization_invites unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_invite_pending_target' AND i.indrelid = to_regclass('public.organization_invites')) THEN
        NULL;  -- 已在 public.organization_invites 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_invite_pending_target' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_invite_pending_target 已存在但不在 public.organization_invites 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_invite_pending_target' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_invite_pending_target ON public.organization_invites (organization_id,target_kind,target_hmac) WHERE status='pending';
    END IF;
END $idxguard$;
-- @index-guard idx_org_invites_expiry ON organization_invites plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_invites_expiry' AND i.indrelid = to_regclass('public.organization_invites')) THEN
        NULL;  -- 已在 public.organization_invites 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_invites_expiry' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_invites_expiry 已存在但不在 public.organization_invites 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_invites_expiry' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_invites_expiry ON public.organization_invites (status,expires_at,id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_security_rate_events (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT REFERENCES organizations(id),
    action TEXT NOT NULL CHECK (btrim(action) <> ''),
    actor_user_id INTEGER REFERENCES users(id),
    target_hmac TEXT CHECK (target_hmac IS NULL OR length(target_hmac) = 64),
    source_ip_hmac TEXT CHECK (source_ip_hmac IS NULL OR length(source_ip_hmac) = 64),
    hmac_key_version TEXT NOT NULL CHECK (btrim(hmac_key_version) <> ''),
    product_catalog_version_id BIGINT NOT NULL REFERENCES pricing_catalog_versions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_security_rate_subject CHECK (
        organization_id IS NOT NULL OR actor_user_id IS NOT NULL
        OR target_hmac IS NOT NULL OR source_ip_hmac IS NOT NULL
    )
);
-- @index-guard idx_org_security_rate_organization ON organization_security_rate_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_security_rate_organization' AND i.indrelid = to_regclass('public.organization_security_rate_events')) THEN
        NULL;  -- 已在 public.organization_security_rate_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_security_rate_organization' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_security_rate_organization 已存在但不在 public.organization_security_rate_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_security_rate_organization' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_security_rate_organization ON public.organization_security_rate_events (action,organization_id,created_at DESC) WHERE organization_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_org_security_rate_actor ON organization_security_rate_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_security_rate_actor' AND i.indrelid = to_regclass('public.organization_security_rate_events')) THEN
        NULL;  -- 已在 public.organization_security_rate_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_security_rate_actor' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_security_rate_actor 已存在但不在 public.organization_security_rate_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_security_rate_actor' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_security_rate_actor ON public.organization_security_rate_events (action,actor_user_id,created_at DESC) WHERE actor_user_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_org_security_rate_target ON organization_security_rate_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_security_rate_target' AND i.indrelid = to_regclass('public.organization_security_rate_events')) THEN
        NULL;  -- 已在 public.organization_security_rate_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_security_rate_target' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_security_rate_target 已存在但不在 public.organization_security_rate_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_security_rate_target' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_security_rate_target ON public.organization_security_rate_events (action,target_hmac,created_at DESC) WHERE target_hmac IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_org_security_rate_ip ON organization_security_rate_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_security_rate_ip' AND i.indrelid = to_regclass('public.organization_security_rate_events')) THEN
        NULL;  -- 已在 public.organization_security_rate_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_security_rate_ip' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_security_rate_ip 已存在但不在 public.organization_security_rate_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_security_rate_ip' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_security_rate_ip ON public.organization_security_rate_events (action,source_ip_hmac,created_at DESC) WHERE source_ip_hmac IS NOT NULL;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_role_capabilities (
    id BIGSERIAL PRIMARY KEY,
    role_id BIGINT NOT NULL REFERENCES organization_roles(id),
    capability TEXT NOT NULL CHECK (capability ~ '^[a-z][a-z0-9_.]{2,95}$'),
    effect TEXT NOT NULL CHECK (effect IN ('allow','deny')),
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_role_capability_unique UNIQUE (role_id,capability)
);

CREATE TABLE IF NOT EXISTS organization_member_capability_overrides (
    id BIGSERIAL PRIMARY KEY,
    membership_id BIGINT NOT NULL REFERENCES organization_memberships(id),
    capability TEXT NOT NULL CHECK (capability ~ '^[a-z][a-z0-9_.]{2,95}$'),
    effect TEXT NOT NULL CHECK (effect IN ('allow','deny')),
    expected_membership_version BIGINT NOT NULL CHECK (expected_membership_version >= 1),
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_member_capability_unique UNIQUE (membership_id,capability)
);

CREATE TABLE IF NOT EXISTS organization_brand_assignments (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    membership_id BIGINT NOT NULL REFERENCES organization_memberships(id),
    brand_id INTEGER NOT NULL REFERENCES brands(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    assigned_by_user_id INTEGER NOT NULL REFERENCES users(id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    request_id TEXT NOT NULL,
    CONSTRAINT organization_brand_assignment_request_unique UNIQUE (organization_id,request_id)
);
-- @index-guard ux_org_brand_assignment_active ON organization_brand_assignments unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_brand_assignment_active' AND i.indrelid = to_regclass('public.organization_brand_assignments')) THEN
        NULL;  -- 已在 public.organization_brand_assignments 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_brand_assignment_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_brand_assignment_active 已存在但不在 public.organization_brand_assignments 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_brand_assignment_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_brand_assignment_active ON public.organization_brand_assignments (organization_id,membership_id,brand_id) WHERE status='active';
    END IF;
END $idxguard$;
-- @index-guard idx_org_brand_assignment_lookup ON organization_brand_assignments plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_brand_assignment_lookup' AND i.indrelid = to_regclass('public.organization_brand_assignments')) THEN
        NULL;  -- 已在 public.organization_brand_assignments 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_brand_assignment_lookup' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_brand_assignment_lookup 已存在但不在 public.organization_brand_assignments 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_brand_assignment_lookup' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_brand_assignment_lookup ON public.organization_brand_assignments (membership_id,status,brand_id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_artifact_shares (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    artifact_type TEXT NOT NULL CHECK (btrim(artifact_type) <> ''),
    artifact_id TEXT NOT NULL CHECK (btrim(artifact_id) <> ''),
    brand_id INTEGER REFERENCES brands(id),
    subject_membership_id BIGINT REFERENCES organization_memberships(id),
    subject_role_id BIGINT REFERENCES organization_roles(id),
    permission TEXT NOT NULL CHECK (permission IN ('read','review','edit','handoff')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    token_hash TEXT UNIQUE,
    token_key_version TEXT,
    token_purpose TEXT,
    token_expires_at TIMESTAMPTZ,
    principal_user_id INTEGER NOT NULL REFERENCES users(id),
    issued_by_user_id INTEGER REFERENCES users(id),
    whitelabel_snapshot JSONB,
    whitelabel_snapshot_hash TEXT,
    request_payload_hash TEXT,
    approval_request_id BIGINT,
    authority_version BIGINT NOT NULL CHECK (authority_version >= 1),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    request_id TEXT NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_artifact_share_subject CHECK (
        (subject_membership_id IS NOT NULL)::integer + (subject_role_id IS NOT NULL)::integer <= 1
    ),
    CONSTRAINT organization_artifact_share_request_unique UNIQUE (organization_id,request_id)
);
-- @index-guard idx_org_artifact_share_lookup ON organization_artifact_shares plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_artifact_share_lookup' AND i.indrelid = to_regclass('public.organization_artifact_shares')) THEN
        NULL;  -- 已在 public.organization_artifact_shares 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_artifact_share_lookup' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_artifact_share_lookup 已存在但不在 public.organization_artifact_shares 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_artifact_share_lookup' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_artifact_share_lookup ON public.organization_artifact_shares (organization_id,artifact_type,artifact_id,status);
    END IF;
END $idxguard$;

ALTER TABLE organization_artifact_shares ADD COLUMN IF NOT EXISTS whitelabel_snapshot_hash TEXT;
ALTER TABLE organization_artifact_shares ADD COLUMN IF NOT EXISTS request_payload_hash TEXT;
ALTER TABLE organization_artifact_shares ADD COLUMN IF NOT EXISTS approval_request_id BIGINT;

CREATE TABLE IF NOT EXISTS organization_spend_limits (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    membership_id BIGINT NOT NULL REFERENCES organization_memberships(id),
    period_type TEXT NOT NULL CHECK (period_type IN ('daily','monthly','custom')),
    limit_kind TEXT NOT NULL CHECK (limit_kind IN (
        'daily_total','monthly_total','daily_feature','monthly_feature','custom'
    )),
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    period_timezone TEXT NOT NULL CHECK (btrim(period_timezone) <> ''),
    feature_code TEXT,
    limit_points BIGINT NOT NULL CHECK (limit_points >= 0),
    reserved_points BIGINT NOT NULL DEFAULT 0 CHECK (reserved_points >= 0),
    consumed_points BIGINT NOT NULL DEFAULT 0 CHECK (consumed_points >= 0),
    refunded_points BIGINT NOT NULL DEFAULT 0 CHECK (refunded_points >= 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closing','closed')),
    policy_version BIGINT NOT NULL CHECK (policy_version >= 1),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_by_user_id INTEGER NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_spend_limit_period_valid CHECK (period_end > period_start),
    CONSTRAINT organization_spend_limit_refund_valid CHECK (refunded_points <= consumed_points),
    CONSTRAINT organization_spend_limit_capacity_valid CHECK (
        reserved_points + consumed_points - refunded_points <= limit_points
    ),
    CONSTRAINT organization_spend_limit_kind_feature_shape CHECK (
        (limit_kind IN ('daily_feature','monthly_feature') AND feature_code IS NOT NULL)
        OR (limit_kind NOT IN ('daily_feature','monthly_feature'))
    )
);
-- @index-guard ux_org_spend_limit_period ON organization_spend_limits unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_spend_limit_period' AND i.indrelid = to_regclass('public.organization_spend_limits')) THEN
        NULL;  -- 已在 public.organization_spend_limits 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_spend_limit_period' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_spend_limit_period 已存在但不在 public.organization_spend_limits 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_spend_limit_period' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_spend_limit_period ON public.organization_spend_limits ( organization_id,membership_id,limit_kind,period_start,COALESCE(feature_code,'') );
    END IF;
END $idxguard$;
-- @index-guard idx_org_spend_limit_open ON organization_spend_limits plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_spend_limit_open' AND i.indrelid = to_regclass('public.organization_spend_limits')) THEN
        NULL;  -- 已在 public.organization_spend_limits 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_spend_limit_open' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_spend_limit_open 已存在但不在 public.organization_spend_limits 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_spend_limit_open' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_spend_limit_open ON public.organization_spend_limits (membership_id,status,period_end,id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_automatic_plans (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    request_id TEXT NOT NULL,
    feature_code TEXT NOT NULL CHECK (btrim(feature_code)<>''),
    work_kind TEXT NOT NULL CHECK (btrim(work_kind)<>''),
    brand_id INTEGER REFERENCES brands(id),
    payload_hash TEXT NOT NULL CHECK (length(payload_hash)=64),
    payload_snapshot JSONB NOT NULL,
    cadence_seconds INTEGER NOT NULL CHECK (cadence_seconds>=60),
    max_occurrences INTEGER NOT NULL CHECK (max_occurrences>0),
    scheduled_occurrences INTEGER NOT NULL DEFAULT 0 CHECK (scheduled_occurrences>=0),
    settled_occurrences INTEGER NOT NULL DEFAULT 0 CHECK (settled_occurrences>=0),
    total_budget_points BIGINT NOT NULL CHECK (total_budget_points>0),
    max_occurrence_points BIGINT NOT NULL CHECK (max_occurrence_points>0),
    reserved_budget_points BIGINT NOT NULL DEFAULT 0 CHECK (reserved_budget_points>=0),
    consumed_budget_points BIGINT NOT NULL DEFAULT 0 CHECK (consumed_budget_points>=0),
    refunded_budget_points BIGINT NOT NULL DEFAULT 0 CHECK (refunded_budget_points>=0),
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    next_occurrence_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','completed','cancelled')),
    pricing_version TEXT NOT NULL,
    pricing_snapshot_hash TEXT NOT NULL CHECK (length(pricing_snapshot_hash)=64),
    approved_by_user_id INTEGER NOT NULL REFERENCES users(id),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version>=1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_automatic_plan_request_unique UNIQUE(organization_id,request_id),
    CONSTRAINT organization_automatic_plan_window_valid CHECK (ends_at>starts_at),
    CONSTRAINT organization_automatic_plan_counts_valid CHECK (
      settled_occurrences<=scheduled_occurrences AND scheduled_occurrences<=max_occurrences
    ),
    CONSTRAINT organization_automatic_plan_budget_valid CHECK (
      reserved_budget_points+consumed_budget_points-refunded_budget_points<=total_budget_points
    )
);
-- @index-guard idx_org_automatic_plans_due ON organization_automatic_plans plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_automatic_plans_due' AND i.indrelid = to_regclass('public.organization_automatic_plans')) THEN
        NULL;  -- 已在 public.organization_automatic_plans 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_automatic_plans_due' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_automatic_plans_due 已存在但不在 public.organization_automatic_plans 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_automatic_plans_due' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_automatic_plans_due ON public.organization_automatic_plans (status,next_occurrence_at,id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_plan_occurrences (
    id BIGSERIAL PRIMARY KEY,
    plan_id BIGINT NOT NULL REFERENCES organization_automatic_plans(id),
    occurrence_key TEXT NOT NULL UNIQUE,
    planned_at TIMESTAMPTZ NOT NULL,
    reserved_ceiling_points BIGINT NOT NULL CHECK (reserved_ceiling_points>0),
    actual_points BIGINT CHECK (actual_points IS NULL OR actual_points>=0),
    refunded_points BIGINT NOT NULL DEFAULT 0 CHECK (refunded_points>=0),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN (
      'scheduled','retry_wait','reserved','running','settled','released','unknown','refunded'
    )),
    charge_link_id BIGINT UNIQUE,
    reservation_attempts INTEGER NOT NULL DEFAULT 0 CHECK (reservation_attempts>=0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_plan_occurrence_amount_valid CHECK (
      actual_points IS NULL OR actual_points<=reserved_ceiling_points
    ),
    CONSTRAINT organization_plan_occurrence_refund_valid CHECK (
      actual_points IS NULL OR refunded_points<=actual_points
    )
);
-- @index-guard idx_org_plan_occurrences_status ON organization_plan_occurrences plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_plan_occurrences_status' AND i.indrelid = to_regclass('public.organization_plan_occurrences')) THEN
        NULL;  -- 已在 public.organization_plan_occurrences 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_plan_occurrences_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_plan_occurrences_status 已存在但不在 public.organization_plan_occurrences 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_plan_occurrences_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_plan_occurrences_status ON public.organization_plan_occurrences (status,next_attempt_at,planned_at,id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_charge_links (
    id BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    membership_id BIGINT REFERENCES organization_memberships(id),
    payer_user_id INTEGER NOT NULL REFERENCES users(id),
    actor_user_id INTEGER REFERENCES users(id),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('owner','member','system','legacy_owner_backfill')),
    requested_by_user_id INTEGER REFERENCES users(id),
    brand_id INTEGER REFERENCES brands(id),
    feature_code TEXT NOT NULL CHECK (btrim(feature_code) <> ''),
    task_ref TEXT,
    automatic_plan_occurrence_id BIGINT REFERENCES organization_plan_occurrences(id),
    primary_period_id BIGINT REFERENCES organization_spend_limits(id),
    applicable_spend_limit_set_hash TEXT,
    estimated_points BIGINT NOT NULL CHECK (estimated_points >= 0),
    reserved_ceiling_points BIGINT NOT NULL CHECK (reserved_ceiling_points > 0),
    actual_points BIGINT,
    refunded_points BIGINT NOT NULL DEFAULT 0 CHECK (refunded_points >= 0),
    amount_total BIGINT,
    status TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN (
        'reserved','committed','released','refund_pending','refunded','unknown'
    )),
    physical_backend TEXT NOT NULL CHECK (physical_backend='legacy_user_wallet'),
    physical_freeze_id TEXT,
    physical_split_snapshot JSONB,
    charge_tx_id TEXT,
    refund_tx_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    refund_request_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    approval_request_id BIGINT,
    approval_policy_version BIGINT,
    pricing_version TEXT NOT NULL,
    pricing_snapshot_hash TEXT NOT NULL CHECK (length(pricing_snapshot_hash)=64),
    execution_started_at TIMESTAMPTZ,
    external_side_effect_started_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    attempt_token TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_charge_amount_valid CHECK (
        actual_points IS NULL OR (actual_points >= 0 AND actual_points <= reserved_ceiling_points)
    ),
    CONSTRAINT organization_charge_refund_valid CHECK (
        actual_points IS NULL OR refunded_points <= actual_points
    ),
    CONSTRAINT organization_charge_actor_shape CHECK (
        (actor_kind='member' AND membership_id IS NOT NULL AND actor_user_id IS NOT NULL)
        OR (actor_kind='owner' AND membership_id IS NOT NULL AND actor_user_id=payer_user_id)
        OR (actor_kind='system' AND membership_id IS NULL AND actor_user_id IS NULL)
        OR actor_kind='legacy_owner_backfill'
    )
);
-- @index-guard idx_org_charge_recovery ON organization_charge_links plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_charge_recovery' AND i.indrelid = to_regclass('public.organization_charge_links')) THEN
        NULL;  -- 已在 public.organization_charge_links 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_charge_recovery' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_charge_recovery 已存在但不在 public.organization_charge_links 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_charge_recovery' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_charge_recovery ON public.organization_charge_links (status,lease_until,id);
    END IF;
END $idxguard$;
-- @index-guard idx_org_charge_member ON organization_charge_links plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_charge_member' AND i.indrelid = to_regclass('public.organization_charge_links')) THEN
        NULL;  -- 已在 public.organization_charge_links 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_charge_member' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_charge_member 已存在但不在 public.organization_charge_links 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_charge_member' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_charge_member ON public.organization_charge_links (membership_id,status,created_at DESC);
    END IF;
END $idxguard$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='organization_plan_occurrences'::regclass
          AND conname='organization_plan_occurrence_charge_fk'
    ) THEN
        ALTER TABLE organization_plan_occurrences
            ADD CONSTRAINT organization_plan_occurrence_charge_fk
            FOREIGN KEY (charge_link_id) REFERENCES organization_charge_links(id)
            NOT VALID;
    END IF;
END $$;
ALTER TABLE organization_plan_occurrences
    VALIDATE CONSTRAINT organization_plan_occurrence_charge_fk;

CREATE TABLE IF NOT EXISTS organization_charge_limit_links (
    charge_link_id BIGINT NOT NULL REFERENCES organization_charge_links(id),
    spend_limit_id BIGINT NOT NULL REFERENCES organization_spend_limits(id),
    limit_kind TEXT NOT NULL CHECK (limit_kind IN (
        'daily_total','monthly_total','daily_feature','monthly_feature','custom'
    )),
    reserved_points BIGINT NOT NULL CHECK (reserved_points > 0),
    consumed_points BIGINT NOT NULL DEFAULT 0 CHECK (consumed_points >= 0),
    refunded_points BIGINT NOT NULL DEFAULT 0 CHECK (refunded_points >= 0),
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    period_timezone TEXT NOT NULL,
    limit_policy_version BIGINT NOT NULL CHECK (limit_policy_version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (charge_link_id,spend_limit_id),
    CONSTRAINT organization_charge_limit_consumed_valid CHECK (consumed_points <= reserved_points),
    CONSTRAINT organization_charge_limit_refunded_valid CHECK (refunded_points <= consumed_points),
    CONSTRAINT organization_charge_limit_period_valid CHECK (period_end > period_start)
);

CREATE TABLE IF NOT EXISTS organization_approval_policies (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    version BIGINT NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','revoked')),
    membership_id BIGINT REFERENCES organization_memberships(id),
    role_id BIGINT REFERENCES organization_roles(id),
    action_type TEXT NOT NULL CHECK (btrim(action_type) <> ''),
    feature_code TEXT,
    public_scope TEXT,
    min_points BIGINT CHECK (min_points IS NULL OR min_points >= 0),
    max_points BIGINT CHECK (max_points IS NULL OR max_points >= 0),
    threshold_points BIGINT CHECK (threshold_points IS NULL OR threshold_points >= 0),
    always_require_approval BOOLEAN NOT NULL DEFAULT TRUE,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to TIMESTAMPTZ,
    policy_hash TEXT NOT NULL CHECK (length(policy_hash)=64),
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    approved_by_user_id INTEGER REFERENCES users(id),
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_approval_policy_window_valid CHECK (
        effective_to IS NULL OR effective_to > effective_from
    ),
    CONSTRAINT organization_approval_policy_subject_valid CHECK (
        membership_id IS NULL OR role_id IS NULL
    ),
    CONSTRAINT organization_approval_policy_amount_valid CHECK (
        min_points IS NULL OR max_points IS NULL OR max_points >= min_points
    )
);
ALTER TABLE organization_approval_policies ADD COLUMN IF NOT EXISTS role_id BIGINT REFERENCES organization_roles(id);
ALTER TABLE organization_approval_policies ADD COLUMN IF NOT EXISTS public_scope TEXT;
ALTER TABLE organization_approval_policies ADD COLUMN IF NOT EXISTS min_points BIGINT;
ALTER TABLE organization_approval_policies ADD COLUMN IF NOT EXISTS max_points BIGINT;
ALTER TABLE organization_approval_policies ADD COLUMN IF NOT EXISTS approved_by_user_id INTEGER REFERENCES users(id);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='organization_approval_policies'::regclass
          AND conname='organization_approval_policy_subject_valid' AND contype='c'
    ) THEN
        ALTER TABLE organization_approval_policies
          ADD CONSTRAINT organization_approval_policy_subject_valid
          CHECK (membership_id IS NULL OR role_id IS NULL) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='organization_approval_policies'::regclass
          AND conname='organization_approval_policy_amount_valid' AND contype='c'
    ) THEN
        ALTER TABLE organization_approval_policies
          ADD CONSTRAINT organization_approval_policy_amount_valid
          CHECK (min_points IS NULL OR max_points IS NULL OR max_points >= min_points) NOT VALID;
    END IF;
END $$;
ALTER TABLE organization_approval_policies
  VALIDATE CONSTRAINT organization_approval_policy_subject_valid;
ALTER TABLE organization_approval_policies
  VALIDATE CONSTRAINT organization_approval_policy_amount_valid;
-- @index-guard ux_org_approval_policy_active ON organization_approval_policies unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_org_approval_policy_active' AND i.indrelid = to_regclass('public.organization_approval_policies')) THEN
        NULL;  -- 已在 public.organization_approval_policies 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_org_approval_policy_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_org_approval_policy_active 已存在但不在 public.organization_approval_policies 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_org_approval_policy_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_org_approval_policy_active ON public.organization_approval_policies ( organization_id,COALESCE(membership_id,0),COALESCE(role_id,0),action_type, COALESCE(feature_code,''),COALESCE(public_scope,''),COALESCE(min_points,-1),COALESCE(max_points,-1) ) WHERE status='active' AND effective_to IS NULL;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_approval_requests (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    requested_by_membership_id BIGINT NOT NULL REFERENCES organization_memberships(id),
    requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
    action_type TEXT NOT NULL CHECK (btrim(action_type) <> ''),
    feature_code TEXT,
    brand_id INTEGER REFERENCES brands(id),
    artifact_type TEXT,
    artifact_id TEXT,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash)=64),
    payload_snapshot JSONB NOT NULL,
    estimated_points BIGINT NOT NULL DEFAULT 0 CHECK (estimated_points >= 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','approved','rejected','expired','executing','executed','failed','revoked'
    )),
    required_approver_capability TEXT NOT NULL DEFAULT 'approvals.review',
    policy_id BIGINT NOT NULL REFERENCES organization_approval_policies(id),
    policy_version BIGINT NOT NULL CHECK (policy_version >= 1),
    policy_hash TEXT NOT NULL CHECK (length(policy_hash)=64),
    capability_snapshot_hash TEXT NOT NULL CHECK (length(capability_snapshot_hash)=64),
    approved_by_user_id INTEGER REFERENCES users(id),
    approved_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    execution_request_id TEXT UNIQUE,
    reason TEXT,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    request_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_approval_request_unique UNIQUE (organization_id,request_id),
    CONSTRAINT organization_approval_no_self CHECK (
        approved_by_user_id IS NULL OR approved_by_user_id <> requested_by_user_id
    ),
    CONSTRAINT organization_approval_expiry_valid CHECK (expires_at > created_at)
);
ALTER TABLE organization_approval_requests ADD COLUMN IF NOT EXISTS feature_code TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='organization_charge_links'::regclass
          AND conname='organization_charge_approval_fk'
    ) THEN
        ALTER TABLE organization_charge_links
            ADD CONSTRAINT organization_charge_approval_fk
            FOREIGN KEY (approval_request_id) REFERENCES organization_approval_requests(id)
            NOT VALID;
    END IF;
END $$;
ALTER TABLE organization_charge_links VALIDATE CONSTRAINT organization_charge_approval_fk;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='organization_artifact_shares'::regclass
      AND conname='organization_artifact_share_approval_fk'
  ) THEN
    ALTER TABLE organization_artifact_shares
      ADD CONSTRAINT organization_artifact_share_approval_fk
      FOREIGN KEY (approval_request_id) REFERENCES organization_approval_requests(id)
      NOT VALID;
  END IF;
END $$;
ALTER TABLE organization_artifact_shares
  VALIDATE CONSTRAINT organization_artifact_share_approval_fk;

CREATE TABLE IF NOT EXISTS organization_work_outbox (
    id BIGSERIAL PRIMARY KEY,
    charge_link_id BIGINT NOT NULL UNIQUE REFERENCES organization_charge_links(id),
    execution_id TEXT NOT NULL UNIQUE,
    work_kind TEXT NOT NULL CHECK (btrim(work_kind) <> ''),
    payload_hash TEXT NOT NULL CHECK (length(payload_hash)=64),
    payload_snapshot JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','claimed','running','succeeded','failed','cancelled','unknown'
    )),
    claim_token TEXT,
    lease_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatch_deadline TIMESTAMPTZ NOT NULL,
    claimed_at TIMESTAMPTZ,
    execution_started_at TIMESTAMPTZ,
    external_side_effect_started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT organization_outbox_lease_shape CHECK (
        status NOT IN ('claimed','running') OR (claim_token IS NOT NULL AND lease_until IS NOT NULL)
    )
);
ALTER TABLE organization_work_outbox
    ADD COLUMN IF NOT EXISTS result_snapshot JSONB;
-- @index-guard idx_org_outbox_claim ON organization_work_outbox plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_outbox_claim' AND i.indrelid = to_regclass('public.organization_work_outbox')) THEN
        NULL;  -- 已在 public.organization_work_outbox 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_outbox_claim' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_outbox_claim 已存在但不在 public.organization_work_outbox 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_outbox_claim' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_outbox_claim ON public.organization_work_outbox (status,next_attempt_at,lease_until,id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS organization_audit_events (
    id BIGSERIAL PRIMARY KEY,
    audit_event_key TEXT NOT NULL UNIQUE CHECK (btrim(audit_event_key) <> ''),
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    request_id TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0 CHECK (sequence >= 0),
    event_type TEXT NOT NULL CHECK (btrim(event_type) <> ''),
    action TEXT NOT NULL CHECK (btrim(action) <> ''),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('owner','member','system','legacy_owner_backfill')),
    actor_user_id INTEGER REFERENCES users(id),
    membership_id BIGINT REFERENCES organization_memberships(id),
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash)=64),
    before_snapshot JSONB,
    after_snapshot JSONB,
    result_snapshot JSONB,
    reason TEXT,
    ip_hash TEXT,
    user_agent_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- @index-guard idx_org_audit_timeline ON organization_audit_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_audit_timeline' AND i.indrelid = to_regclass('public.organization_audit_events')) THEN
        NULL;  -- 已在 public.organization_audit_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_audit_timeline' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_audit_timeline 已存在但不在 public.organization_audit_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_audit_timeline' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_audit_timeline ON public.organization_audit_events (organization_id,created_at DESC,id DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_org_audit_actor ON organization_audit_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_org_audit_actor' AND i.indrelid = to_regclass('public.organization_audit_events')) THEN
        NULL;  -- 已在 public.organization_audit_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_org_audit_actor' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_org_audit_actor 已存在但不在 public.organization_audit_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_org_audit_actor' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_org_audit_actor ON public.organization_audit_events (actor_user_id,created_at DESC);
    END IF;
END $idxguard$;

-- Additive actor/org ownership columns on current GEO objects. Existing rows remain NULL and quarantine.
ALTER TABLE IF EXISTS client_profiles ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS client_profiles ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE IF EXISTS client_profiles ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS client_profiles ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS client_profiles ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS client_profiles ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

ALTER TABLE IF EXISTS client_materials ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS client_materials ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE IF EXISTS client_materials ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS client_materials ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS client_materials ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS client_materials ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

ALTER TABLE IF EXISTS diagnosis_records ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS diagnosis_records ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE IF EXISTS diagnosis_records ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS diagnosis_records ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS diagnosis_records ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS diagnosis_records ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

ALTER TABLE IF EXISTS quotes ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS quotes ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE IF EXISTS quotes ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS quotes ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS quotes ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS quotes ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

ALTER TABLE IF EXISTS keyword_selection_sessions ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS keyword_selection_sessions ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS keyword_selection_sessions ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS keyword_selection_sessions ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS keyword_selection_sessions ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

ALTER TABLE IF EXISTS article_generations ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS article_generations ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE IF EXISTS article_generations ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE IF EXISTS article_generations ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS article_generations ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS article_generations ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS article_generations ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

ALTER TABLE IF EXISTS publish_orders ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS publish_orders ADD COLUMN IF NOT EXISTS actor_user_id INTEGER;
ALTER TABLE IF EXISTS publish_orders ADD COLUMN IF NOT EXISTS actor_membership_id BIGINT;
ALTER TABLE IF EXISTS publish_orders ADD COLUMN IF NOT EXISTS actor_kind TEXT;
ALTER TABLE IF EXISTS publish_orders ADD COLUMN IF NOT EXISTS payer_user_id INTEGER;
ALTER TABLE IF EXISTS publish_orders ADD COLUMN IF NOT EXISTS approval_request_id BIGINT;

ALTER TABLE IF EXISTS media_publications ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS media_publications ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE IF EXISTS media_publications ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE IF EXISTS media_publications ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS media_publications ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS media_publications ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS media_publications ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

ALTER TABLE IF EXISTS monitoring_tasks ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS monitoring_tasks ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE IF EXISTS monitoring_tasks ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS monitoring_tasks ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS monitoring_tasks ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS monitoring_tasks ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

ALTER TABLE IF EXISTS monitoring_reports ADD COLUMN IF NOT EXISTS organization_id BIGINT;
ALTER TABLE IF EXISTS monitoring_reports ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE IF EXISTS monitoring_reports ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
ALTER TABLE IF EXISTS monitoring_reports ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT;
ALTER TABLE IF EXISTS monitoring_reports ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER;
ALTER TABLE IF EXISTS monitoring_reports ADD COLUMN IF NOT EXISTS artifact_visibility TEXT;

-- New organization-owned rows are constrained even while historical NULL rows
-- remain quarantined. NOT VALID avoids a blocking historical scan; PostgreSQL
-- still enforces these constraints for every new or updated row.
DO $$
DECLARE
    artifact_table TEXT;
    constraint_prefix TEXT;
BEGIN
    FOREACH artifact_table IN ARRAY ARRAY[
      'client_profiles','client_materials','diagnosis_records','quotes',
      'keyword_selection_sessions','article_generations','articles',
      'media_publications','monitoring_tasks','monitoring_reports'
    ] LOOP
      IF to_regclass(format('%I.%I', current_schema(), artifact_table)) IS NULL THEN
        CONTINUE;
      END IF;
      -- Repair partial/legacy tables before applying the isolation shape.  All
      -- columns stay nullable so old single-user rows retain their old meaning.
      EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS brand_id INTEGER', artifact_table);
      EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS organization_id BIGINT', artifact_table);
      EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER', artifact_table);
      EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT', artifact_table);
      EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS created_by_actor_kind TEXT', artifact_table);
      EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS responsible_user_id INTEGER', artifact_table);
      EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS artifact_visibility TEXT', artifact_table);
      constraint_prefix := 'org_' || left(artifact_table, 31);
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname=constraint_prefix || '_shape'
          AND conrelid=format('%I.%I',current_schema(),artifact_table)::regclass
          AND contype='c'
      ) THEN
        EXECUTE format(
          'ALTER TABLE %I ADD CONSTRAINT %I CHECK (
             organization_id IS NULL OR (
               created_by_actor_kind IN (''owner'',''member'',''system'')
               AND created_by_user_id IS NOT NULL
               AND responsible_user_id IS NOT NULL
               AND artifact_visibility IN (''private'',''team'',''external'')
               AND (created_by_actor_kind <> ''member'' OR created_by_membership_id IS NOT NULL)
             )
           ) NOT VALID',
          artifact_table, constraint_prefix || '_shape'
        );
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname=constraint_prefix || '_organization_fk'
          AND conrelid=format('%I.%I',current_schema(),artifact_table)::regclass
          AND contype='f'
      ) THEN
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (organization_id) REFERENCES organizations(id) NOT VALID', artifact_table, constraint_prefix || '_organization_fk');
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname=constraint_prefix || '_membership_fk'
          AND conrelid=format('%I.%I',current_schema(),artifact_table)::regclass
          AND contype='f'
      ) THEN
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (created_by_membership_id) REFERENCES organization_memberships(id) NOT VALID', artifact_table, constraint_prefix || '_membership_fk');
      END IF;
      EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', artifact_table, constraint_prefix || '_shape');
      EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', artifact_table, constraint_prefix || '_organization_fk');
      EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', artifact_table, constraint_prefix || '_membership_fk');
      EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I(organization_id,brand_id,created_by_membership_id)', 'idx_org_' || left(artifact_table, 42) || '_scope', artifact_table);
    END LOOP;

    IF to_regclass(format('%I.publish_orders', current_schema())) IS NOT NULL THEN
      ALTER TABLE publish_orders ADD COLUMN IF NOT EXISTS organization_id BIGINT;
      ALTER TABLE publish_orders ADD COLUMN IF NOT EXISTS actor_user_id INTEGER;
      ALTER TABLE publish_orders ADD COLUMN IF NOT EXISTS actor_membership_id BIGINT;
      ALTER TABLE publish_orders ADD COLUMN IF NOT EXISTS actor_kind TEXT;
      ALTER TABLE publish_orders ADD COLUMN IF NOT EXISTS payer_user_id INTEGER;
      ALTER TABLE publish_orders ADD COLUMN IF NOT EXISTS approval_request_id BIGINT;
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='organization_publish_actor_shape'
          AND conrelid=format('%I.publish_orders',current_schema())::regclass
          AND contype='c'
      ) THEN
        ALTER TABLE publish_orders ADD CONSTRAINT organization_publish_actor_shape CHECK (
          organization_id IS NULL OR (
            actor_kind IN ('owner','member','system') AND actor_user_id IS NOT NULL
            AND payer_user_id IS NOT NULL
            AND (actor_kind <> 'member' OR actor_membership_id IS NOT NULL)
          )
        ) NOT VALID;
      END IF;
      ALTER TABLE publish_orders VALIDATE CONSTRAINT organization_publish_actor_shape;
      -- @index-guard idx_org_publish_orders_scope ON publish_orders plain
      IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                  WHERE c.relname = 'idx_org_publish_orders_scope' AND i.indrelid = to_regclass('public.publish_orders')) THEN
          NULL;  -- 已在 public.publish_orders 上 → 幂等跳过
      ELSIF EXISTS (SELECT 1 FROM pg_class c
                     WHERE c.relname = 'idx_org_publish_orders_scope' AND c.relnamespace = 'public'::regnamespace) THEN
          RAISE EXCEPTION '[index-guard] idx_org_publish_orders_scope 已存在但不在 public.publish_orders 上(实际宿主:%)—— 拒绝静默跳过',
              (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
                 LEFT JOIN pg_index i ON i.indexrelid = c.oid
                 LEFT JOIN pg_class t ON t.oid = i.indrelid
                WHERE c.relname = 'idx_org_publish_orders_scope' AND c.relnamespace = 'public'::regnamespace)
              USING ERRCODE = 'duplicate_object';
      ELSE
          CREATE INDEX idx_org_publish_orders_scope ON public.publish_orders (organization_id,actor_membership_id,actor_user_id);
      END IF;
    END IF;
END $$;

INSERT INTO organization_schema_migrations(version,contract_freeze_sha,status,applied_at,rolled_back_at,details)
VALUES (
    'organization_internal_seats_2026_07_20_v1',
    '5bd56f8112249ba6ac31f98a3c453ef6c41b30a2',
    'applied',NOW(),NULL,
    '{"signed_base":"1f874d769118f73dff04acdc32f396987e6f6ee9"}'::jsonb
)
ON CONFLICT(version) DO UPDATE SET
    contract_freeze_sha=EXCLUDED.contract_freeze_sha,
    status='applied',
    applied_at=NOW(),
    rolled_back_at=NULL,
    details=EXCLUDED.details;

COMMIT;
