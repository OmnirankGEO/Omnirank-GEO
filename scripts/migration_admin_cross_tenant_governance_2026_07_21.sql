-- ADMIN cross-tenant governance, frozen demo cases and provider downgrade plans.
-- PostgreSQL 16, additive/idempotent, public-qualified.  No commercial, wallet,
-- inventory or historical business row is reassigned by this migration.

BEGIN;

CREATE TABLE IF NOT EXISTS public.admin_governance_subject_versions (
    subject_kind TEXT NOT NULL
        CONSTRAINT admin_governance_subject_versions_kind_check
        CHECK (subject_kind IN ('user','organization','demo_grant','provider_downgrade_plan')),
    subject_id BIGINT NOT NULL
        CONSTRAINT admin_governance_subject_versions_subject_id_check CHECK (subject_id > 0),
    capability TEXT NOT NULL
        CONSTRAINT admin_governance_subject_versions_capability_check CHECK (btrim(capability) <> ''),
    version BIGINT NOT NULL DEFAULT 1
        CONSTRAINT admin_governance_subject_versions_version_check CHECK (version >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT admin_governance_subject_versions_pkey PRIMARY KEY (subject_kind, subject_id, capability)
);

-- CREATE TABLE IF NOT EXISTS does not restore a dropped PK.  Re-running this
-- migration must repair that exact safe defect or fail on conflicting data.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='public.admin_governance_subject_versions'::regclass
          AND contype='p'
    ) THEN
        ALTER TABLE public.admin_governance_subject_versions
            ADD CONSTRAINT admin_governance_subject_versions_pkey
            PRIMARY KEY (subject_kind,subject_id,capability);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.admin_cross_tenant_audits (
    id BIGSERIAL CONSTRAINT admin_cross_tenant_audits_pkey PRIMARY KEY,
    actor_user_id INTEGER NOT NULL
        CONSTRAINT admin_cross_tenant_audits_actor_user_id_fkey REFERENCES public.users(id),
    actor_username TEXT,
    action TEXT NOT NULL
        CONSTRAINT admin_cross_tenant_audits_action_check CHECK (btrim(action) <> ''),
    subject_kind TEXT NOT NULL
        CONSTRAINT admin_cross_tenant_audits_subject_kind_check CHECK (btrim(subject_kind) <> ''),
    subject_id BIGINT,
    request_id TEXT NOT NULL
        CONSTRAINT admin_cross_tenant_audits_request_id_key UNIQUE
        CONSTRAINT admin_cross_tenant_audits_request_id_check CHECK (btrim(request_id) <> ''),
    event_hash TEXT NOT NULL
        CONSTRAINT admin_cross_tenant_audits_event_hash_check
        CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    reason TEXT NOT NULL
        CONSTRAINT admin_cross_tenant_audits_reason_check
        CHECK (char_length(btrim(reason)) BETWEEN 2 AND 500),
    before_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip_address TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.admin_cross_tenant_audits ADD COLUMN IF NOT EXISTS event_hash TEXT;
UPDATE public.admin_cross_tenant_audits
SET event_hash=repeat('0',64) WHERE event_hash IS NULL;
ALTER TABLE public.admin_cross_tenant_audits ALTER COLUMN event_hash SET NOT NULL;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='public.admin_cross_tenant_audits'::regclass
          AND conname='admin_cross_tenant_audits_event_hash_check'
    ) THEN
        ALTER TABLE public.admin_cross_tenant_audits
            ADD CONSTRAINT admin_cross_tenant_audits_event_hash_check
            CHECK (event_hash ~ '^[0-9a-f]{64}$');
    END IF;
END $$;
-- @index-guard idx_admin_cross_tenant_audits_subject ON admin_cross_tenant_audits plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_cross_tenant_audits_subject' AND i.indrelid = to_regclass('public.admin_cross_tenant_audits')) THEN
        NULL;  -- 已在 public.admin_cross_tenant_audits 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_cross_tenant_audits_subject' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_cross_tenant_audits_subject 已存在但不在 public.admin_cross_tenant_audits 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_cross_tenant_audits_subject' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_cross_tenant_audits_subject ON public.admin_cross_tenant_audits (subject_kind, subject_id, created_at DESC, id DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_admin_cross_tenant_audits_actor ON admin_cross_tenant_audits plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_cross_tenant_audits_actor' AND i.indrelid = to_regclass('public.admin_cross_tenant_audits')) THEN
        NULL;  -- 已在 public.admin_cross_tenant_audits 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_cross_tenant_audits_actor' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_cross_tenant_audits_actor 已存在但不在 public.admin_cross_tenant_audits 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_cross_tenant_audits_actor' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_cross_tenant_audits_actor ON public.admin_cross_tenant_audits (actor_user_id, created_at DESC, id DESC);
    END IF;
END $idxguard$;

-- Invitation acceptance idempotency lives in an additive sidecar.  The
-- frozen organization_invites catalog must remain byte-for-byte compatible
-- with the active f5a release while blue and green overlap.
CREATE TABLE IF NOT EXISTS public.organization_invite_accept_receipts (
    invite_id BIGINT
        CONSTRAINT organization_invite_accept_receipts_pkey PRIMARY KEY
        CONSTRAINT organization_invite_accept_receipts_invite_id_fkey
        REFERENCES public.organization_invites(id),
    accepted_user_id INTEGER NOT NULL
        CONSTRAINT organization_invite_accept_receipts_accepted_user_id_fkey
        REFERENCES public.users(id),
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

-- Existing accepted invitations predate response-loss replay.  Record their
-- exact membership/user result without mutating the frozen invitation table;
-- the synthetic request/hash intentionally cannot impersonate a live retry.
INSERT INTO public.organization_invite_accept_receipts(
    invite_id,accepted_user_id,accepted_request_id,accept_request_hash,accepted_result
)
SELECT i.id,m.user_id,'legacy-invite-accept-' || i.id::text,repeat('0',64),
       jsonb_build_object(
           'membership_id',i.accepted_membership_id,
           'organization_id',i.organization_id,
           'status','active'
       )
FROM public.organization_invites i
JOIN public.organization_memberships m ON m.id=i.accepted_membership_id
WHERE i.status='accepted'
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS public.admin_demo_cases (
    case_id UUID CONSTRAINT admin_demo_cases_pkey PRIMARY KEY,
    brand_id BIGINT NOT NULL CONSTRAINT admin_demo_cases_brand_id_check CHECK (brand_id > 0),
    diagnosis_id BIGINT NOT NULL CONSTRAINT admin_demo_cases_diagnosis_id_check CHECK (diagnosis_id > 0),
    status TEXT NOT NULL DEFAULT 'active'
        CONSTRAINT admin_demo_cases_status_check CHECK (status IN ('active','retired')),
    snapshot_version BIGINT NOT NULL DEFAULT 1
        CONSTRAINT admin_demo_cases_snapshot_version_check CHECK (snapshot_version >= 1),
    safe_snapshot JSONB NOT NULL,
    frozen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id INTEGER NOT NULL
        CONSTRAINT admin_demo_cases_created_by_user_id_fkey REFERENCES public.users(id),
    created_reason TEXT NOT NULL
        CONSTRAINT admin_demo_cases_created_reason_check
        CHECK (char_length(btrim(created_reason)) BETWEEN 2 AND 500),
    created_request_id TEXT NOT NULL CONSTRAINT admin_demo_cases_created_request_id_key UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT admin_demo_cases_brand_id_diagnosis_id_key UNIQUE (brand_id, diagnosis_id),
    CONSTRAINT admin_demo_cases_case_id_brand_id_key UNIQUE (case_id, brand_id)
);
-- @index-guard idx_admin_demo_cases_brand ON admin_demo_cases plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_demo_cases_brand' AND i.indrelid = to_regclass('public.admin_demo_cases')) THEN
        NULL;  -- 已在 public.admin_demo_cases 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_demo_cases_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_demo_cases_brand 已存在但不在 public.admin_demo_cases 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_demo_cases_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_demo_cases_brand ON public.admin_demo_cases (brand_id, status, frozen_at DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS public.admin_demo_case_grants (
    id BIGSERIAL CONSTRAINT admin_demo_case_grants_pkey PRIMARY KEY,
    grantee_kind TEXT NOT NULL
        CONSTRAINT admin_demo_case_grants_grantee_kind_check CHECK (grantee_kind IN ('user','organization')),
    grantee_user_id INTEGER
        CONSTRAINT admin_demo_case_grants_grantee_user_id_fkey REFERENCES public.users(id),
    grantee_organization_id BIGINT
        CONSTRAINT admin_demo_case_grants_grantee_organization_id_fkey REFERENCES public.organizations(id),
    case_id UUID NOT NULL,
    brand_id BIGINT NOT NULL CONSTRAINT admin_demo_case_grants_brand_id_check CHECK (brand_id > 0),
    capability TEXT NOT NULL DEFAULT 'demo.customer.preview'
        CONSTRAINT admin_demo_case_grants_capability_check CHECK (capability = 'demo.customer.preview'),
    status TEXT NOT NULL DEFAULT 'active'
        CONSTRAINT admin_demo_case_grants_status_check CHECK (status IN ('active','revoked','expired')),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    version BIGINT NOT NULL DEFAULT 1
        CONSTRAINT admin_demo_case_grants_version_check CHECK (version >= 1),
    created_by_user_id INTEGER NOT NULL
        CONSTRAINT admin_demo_case_grants_created_by_user_id_fkey REFERENCES public.users(id),
    created_reason TEXT NOT NULL
        CONSTRAINT admin_demo_case_grants_created_reason_check
        CHECK (char_length(btrim(created_reason)) BETWEEN 2 AND 500),
    note TEXT NOT NULL DEFAULT ''
        CONSTRAINT admin_demo_case_grants_note_check CHECK (char_length(note) <= 500),
    request_payload_hash TEXT NOT NULL
        CONSTRAINT admin_demo_case_grants_request_payload_hash_check
        CHECK (request_payload_hash ~ '^[0-9a-f]{64}$'),
    created_request_id TEXT NOT NULL CONSTRAINT admin_demo_case_grants_created_request_id_key UNIQUE,
    revoked_by_user_id INTEGER
        CONSTRAINT admin_demo_case_grants_revoked_by_user_id_fkey REFERENCES public.users(id),
    revoked_reason TEXT,
    revoked_request_id TEXT CONSTRAINT admin_demo_case_grants_revoked_request_id_key UNIQUE,
    revoked_at TIMESTAMPTZ,
    expired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT admin_demo_case_grants_grantee_shape_check CHECK (
        (grantee_kind='user' AND grantee_user_id IS NOT NULL AND grantee_organization_id IS NULL)
        OR
        (grantee_kind='organization' AND grantee_user_id IS NULL AND grantee_organization_id IS NOT NULL)
    ),
    CONSTRAINT admin_demo_case_grants_case_fk
        FOREIGN KEY (case_id,brand_id) REFERENCES public.admin_demo_cases(case_id,brand_id),
    CONSTRAINT admin_demo_case_grants_window_check CHECK (expires_at > valid_from),
    CONSTRAINT admin_demo_case_grants_revocation_shape_check CHECK (
        (status='active' AND revoked_at IS NULL AND expired_at IS NULL
          AND revoked_by_user_id IS NULL AND revoked_reason IS NULL)
        OR
        (status='revoked' AND revoked_at IS NOT NULL AND expired_at IS NULL
          AND revoked_by_user_id IS NOT NULL AND char_length(btrim(revoked_reason)) BETWEEN 2 AND 500)
        OR
        (status='expired' AND revoked_at IS NULL AND expired_at IS NOT NULL
          AND revoked_by_user_id IS NULL AND revoked_reason IS NULL)
    )
);
ALTER TABLE public.admin_demo_case_grants ADD COLUMN IF NOT EXISTS request_payload_hash TEXT;
UPDATE public.admin_demo_case_grants
SET request_payload_hash=repeat('0',64) WHERE request_payload_hash IS NULL;
ALTER TABLE public.admin_demo_case_grants ALTER COLUMN request_payload_hash SET NOT NULL;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='public.admin_demo_case_grants'::regclass
          AND conname='admin_demo_case_grants_request_payload_hash_check'
    ) THEN
        ALTER TABLE public.admin_demo_case_grants
            ADD CONSTRAINT admin_demo_case_grants_request_payload_hash_check
            CHECK (request_payload_hash ~ '^[0-9a-f]{64}$');
    END IF;
END $$;
-- @index-guard ux_admin_demo_case_grants_active_user ON admin_demo_case_grants unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_admin_demo_case_grants_active_user' AND i.indrelid = to_regclass('public.admin_demo_case_grants')) THEN
        NULL;  -- 已在 public.admin_demo_case_grants 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_admin_demo_case_grants_active_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_admin_demo_case_grants_active_user 已存在但不在 public.admin_demo_case_grants 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_admin_demo_case_grants_active_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_admin_demo_case_grants_active_user ON public.admin_demo_case_grants (grantee_user_id, case_id, capability) WHERE status='active' AND grantee_kind='user';
    END IF;
END $idxguard$;
-- @index-guard ux_admin_demo_case_grants_active_org ON admin_demo_case_grants unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_admin_demo_case_grants_active_org' AND i.indrelid = to_regclass('public.admin_demo_case_grants')) THEN
        NULL;  -- 已在 public.admin_demo_case_grants 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_admin_demo_case_grants_active_org' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_admin_demo_case_grants_active_org 已存在但不在 public.admin_demo_case_grants 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_admin_demo_case_grants_active_org' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_admin_demo_case_grants_active_org ON public.admin_demo_case_grants (grantee_organization_id, case_id, capability) WHERE status='active' AND grantee_kind='organization';
    END IF;
END $idxguard$;
-- @index-guard idx_admin_demo_case_grants_case ON admin_demo_case_grants plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_demo_case_grants_case' AND i.indrelid = to_regclass('public.admin_demo_case_grants')) THEN
        NULL;  -- 已在 public.admin_demo_case_grants 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_demo_case_grants_case' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_demo_case_grants_case 已存在但不在 public.admin_demo_case_grants 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_demo_case_grants_case' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_demo_case_grants_case ON public.admin_demo_case_grants (case_id, status, expires_at, id);
    END IF;
END $idxguard$;
-- @index-guard idx_admin_demo_case_grants_user_live ON admin_demo_case_grants plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_demo_case_grants_user_live' AND i.indrelid = to_regclass('public.admin_demo_case_grants')) THEN
        NULL;  -- 已在 public.admin_demo_case_grants 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_demo_case_grants_user_live' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_demo_case_grants_user_live 已存在但不在 public.admin_demo_case_grants 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_demo_case_grants_user_live' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_demo_case_grants_user_live ON public.admin_demo_case_grants (grantee_user_id, expires_at, id) WHERE status='active' AND grantee_kind='user';
    END IF;
END $idxguard$;
-- @index-guard idx_admin_demo_case_grants_org_live ON admin_demo_case_grants plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_demo_case_grants_org_live' AND i.indrelid = to_regclass('public.admin_demo_case_grants')) THEN
        NULL;  -- 已在 public.admin_demo_case_grants 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_demo_case_grants_org_live' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_demo_case_grants_org_live 已存在但不在 public.admin_demo_case_grants 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_demo_case_grants_org_live' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_demo_case_grants_org_live ON public.admin_demo_case_grants (grantee_organization_id, expires_at, id) WHERE status='active' AND grantee_kind='organization';
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS public.admin_demo_access_events (
    id BIGSERIAL CONSTRAINT admin_demo_access_events_pkey PRIMARY KEY,
    grant_id BIGINT NOT NULL
        CONSTRAINT admin_demo_access_events_grant_id_fkey REFERENCES public.admin_demo_case_grants(id),
    viewer_user_id INTEGER NOT NULL
        CONSTRAINT admin_demo_access_events_viewer_user_id_fkey REFERENCES public.users(id),
    brand_id BIGINT NOT NULL CONSTRAINT admin_demo_access_events_brand_id_check CHECK (brand_id > 0),
    case_id UUID NOT NULL,
    access_mode TEXT NOT NULL DEFAULT 'demo'
        CONSTRAINT admin_demo_access_events_access_mode_check CHECK (access_mode='demo'),
    action TEXT NOT NULL CONSTRAINT admin_demo_access_events_action_check CHECK (btrim(action) <> ''),
    request_id TEXT NOT NULL CONSTRAINT admin_demo_access_events_request_id_check CHECK (btrim(request_id) <> ''),
    ip_address TEXT,
    blocked_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT admin_demo_access_events_idempotency_key
        UNIQUE (grant_id,viewer_user_id,request_id,action),
    CONSTRAINT admin_demo_access_events_case_fk
        FOREIGN KEY (case_id,brand_id) REFERENCES public.admin_demo_cases(case_id,brand_id)
);
-- @index-guard idx_admin_demo_access_events_grant ON admin_demo_access_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_demo_access_events_grant' AND i.indrelid = to_regclass('public.admin_demo_access_events')) THEN
        NULL;  -- 已在 public.admin_demo_access_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_demo_access_events_grant' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_demo_access_events_grant 已存在但不在 public.admin_demo_access_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_demo_access_events_grant' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_demo_access_events_grant ON public.admin_demo_access_events (grant_id,created_at DESC,id DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_admin_demo_access_events_viewer ON admin_demo_access_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_demo_access_events_viewer' AND i.indrelid = to_regclass('public.admin_demo_access_events')) THEN
        NULL;  -- 已在 public.admin_demo_access_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_demo_access_events_viewer' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_demo_access_events_viewer 已存在但不在 public.admin_demo_access_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_demo_access_events_viewer' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_demo_access_events_viewer ON public.admin_demo_access_events (viewer_user_id,created_at DESC,id DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS public.admin_provider_downgrade_plans (
    id BIGSERIAL CONSTRAINT admin_provider_downgrade_plans_pkey PRIMARY KEY,
    provider_user_id INTEGER NOT NULL
        CONSTRAINT admin_provider_downgrade_plans_provider_user_id_fkey REFERENCES public.users(id),
    strategy TEXT NOT NULL
        CONSTRAINT admin_provider_downgrade_plans_strategy_check
        CHECK (strategy IN ('transfer_upstream','platform_managed','settle_then_downgrade')),
    target_provider_user_id INTEGER
        CONSTRAINT admin_provider_downgrade_plans_target_provider_user_id_fkey REFERENCES public.users(id),
    status TEXT NOT NULL DEFAULT 'draft'
        CONSTRAINT admin_provider_downgrade_plans_status_check
        CHECK (status IN ('draft','blocked','ready','completed','cancelled')),
    dependency_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    next_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    expected_identity_version BIGINT NOT NULL
        CONSTRAINT admin_provider_downgrade_plans_expected_identity_version_check
        CHECK (expected_identity_version >= 1),
    version BIGINT NOT NULL DEFAULT 1
        CONSTRAINT admin_provider_downgrade_plans_version_check CHECK (version >= 1),
    created_by_user_id INTEGER NOT NULL
        CONSTRAINT admin_provider_downgrade_plans_created_by_user_id_fkey REFERENCES public.users(id),
    created_reason TEXT NOT NULL
        CONSTRAINT admin_provider_downgrade_plans_created_reason_check
        CHECK (char_length(btrim(created_reason)) BETWEEN 2 AND 500),
    created_request_id TEXT NOT NULL CONSTRAINT admin_provider_downgrade_plans_created_request_id_key UNIQUE,
    confirmed_by_user_id INTEGER
        CONSTRAINT admin_provider_downgrade_plans_confirmed_by_user_id_fkey REFERENCES public.users(id),
    confirmed_reason TEXT,
    confirmed_request_id TEXT CONSTRAINT admin_provider_downgrade_plans_confirmed_request_id_key UNIQUE,
    confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT admin_provider_downgrade_plans_target_check CHECK (
        (strategy='transfer_upstream' AND target_provider_user_id IS NOT NULL
          AND target_provider_user_id <> provider_user_id)
        OR
        (strategy IN ('platform_managed','settle_then_downgrade') AND target_provider_user_id IS NULL)
    ),
    CONSTRAINT admin_provider_downgrade_plans_completion_shape_check CHECK (
        (status <> 'completed') OR
        (confirmed_by_user_id IS NOT NULL AND confirmed_at IS NOT NULL
          AND char_length(btrim(confirmed_reason)) BETWEEN 2 AND 500)
    )
);
-- @index-guard ux_admin_provider_downgrade_plans_live ON admin_provider_downgrade_plans unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_admin_provider_downgrade_plans_live' AND i.indrelid = to_regclass('public.admin_provider_downgrade_plans')) THEN
        NULL;  -- 已在 public.admin_provider_downgrade_plans 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_admin_provider_downgrade_plans_live' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_admin_provider_downgrade_plans_live 已存在但不在 public.admin_provider_downgrade_plans 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_admin_provider_downgrade_plans_live' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_admin_provider_downgrade_plans_live ON public.admin_provider_downgrade_plans (provider_user_id) WHERE status IN ('draft','blocked','ready');
    END IF;
END $idxguard$;
-- @index-guard idx_admin_provider_downgrade_plans_provider ON admin_provider_downgrade_plans plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_provider_downgrade_plans_provider' AND i.indrelid = to_regclass('public.admin_provider_downgrade_plans')) THEN
        NULL;  -- 已在 public.admin_provider_downgrade_plans 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_provider_downgrade_plans_provider' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_provider_downgrade_plans_provider 已存在但不在 public.admin_provider_downgrade_plans 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_provider_downgrade_plans_provider' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_provider_downgrade_plans_provider ON public.admin_provider_downgrade_plans (provider_user_id, created_at DESC, id DESC);
    END IF;
END $idxguard$;

COMMIT;
