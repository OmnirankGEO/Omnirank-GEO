-- Admin user identity/relationship governance · additive and idempotent.
-- Prestart-only through db/migration_manifest.py. No production data backfill.

CREATE TABLE IF NOT EXISTS admin_user_governance_versions (
    subject_user_id INTEGER NOT NULL,
    scope TEXT NOT NULL CONSTRAINT admin_user_governance_versions_scope_check CHECK (
        scope IN ('business_identity', 'commercial_binding', 'platform_access')
    ),
    version BIGINT NOT NULL DEFAULT 1
        CONSTRAINT admin_user_governance_versions_version_check CHECK (version >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (subject_user_id, scope)
);

CREATE TABLE IF NOT EXISTS admin_user_governance_audits (
    id BIGSERIAL PRIMARY KEY,
    subject_user_id INTEGER NOT NULL,
    scope TEXT NOT NULL CONSTRAINT admin_user_governance_audits_scope_check CHECK (
        scope IN ('business_identity', 'commercial_binding', 'platform_access')
    ),
    operator_user_id INTEGER NOT NULL,
    operator_username TEXT,
    request_id TEXT NOT NULL,
    reason TEXT NOT NULL CONSTRAINT admin_user_governance_audits_reason_check
        CHECK (char_length(btrim(reason)) BETWEEN 2 AND 500),
    before_snapshot JSONB NOT NULL,
    after_snapshot JSONB NOT NULL,
    evidence_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
    version_before BIGINT NOT NULL
        CONSTRAINT admin_user_governance_audits_version_before_check CHECK (version_before >= 1),
    version_after BIGINT NOT NULL
        CONSTRAINT admin_user_governance_audits_version_after_check
        CHECK (version_after = version_before + 1),
    ip_address TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customer_agent_binding_history (
    id BIGSERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL,
    provider_user_id INTEGER,
    relationship_state TEXT NOT NULL
        CONSTRAINT customer_agent_binding_history_relationship_state_check CHECK (
        relationship_state IN ('service_provider', 'platform_direct')
    ),
    source_binding_id INTEGER,
    binding_source TEXT,
    source_token TEXT,
    effective_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_to TIMESTAMPTZ,
    dispute_status TEXT,
    dispute_note TEXT,
    created_by_operator_user_id INTEGER,
    created_reason TEXT,
    created_request_id TEXT,
    ended_by_operator_user_id INTEGER,
    ended_reason TEXT,
    ended_request_id TEXT,
    evidence_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT customer_agent_binding_history_provider_state_check CHECK (
        (relationship_state='service_provider' AND provider_user_id IS NOT NULL)
        OR (relationship_state='platform_direct' AND provider_user_id IS NULL)
    ),
    CONSTRAINT customer_agent_binding_history_effective_range_check
        CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

-- Repair additive half-created tables.  Wrong types or incompatible existing data
-- are deliberately not coerced: the final four-dimensional verifier raises and
-- halts prestart rather than guessing at business evidence.
ALTER TABLE admin_user_governance_versions
    ADD COLUMN IF NOT EXISTS subject_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS scope TEXT,
    ADD COLUMN IF NOT EXISTS version BIGINT DEFAULT 1,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE admin_user_governance_versions
    ALTER COLUMN subject_user_id SET NOT NULL,
    ALTER COLUMN scope SET NOT NULL,
    ALTER COLUMN version SET DEFAULT 1,
    ALTER COLUMN version SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT NOW(),
    ALTER COLUMN updated_at SET NOT NULL;

ALTER TABLE admin_user_governance_audits
    ADD COLUMN IF NOT EXISTS id BIGSERIAL,
    ADD COLUMN IF NOT EXISTS subject_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS scope TEXT,
    ADD COLUMN IF NOT EXISTS operator_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS operator_username TEXT,
    ADD COLUMN IF NOT EXISTS request_id TEXT,
    ADD COLUMN IF NOT EXISTS reason TEXT,
    ADD COLUMN IF NOT EXISTS before_snapshot JSONB,
    ADD COLUMN IF NOT EXISTS after_snapshot JSONB,
    ADD COLUMN IF NOT EXISTS evidence_jsonb JSONB DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS version_before BIGINT,
    ADD COLUMN IF NOT EXISTS version_after BIGINT,
    ADD COLUMN IF NOT EXISTS ip_address TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE admin_user_governance_audits
    ALTER COLUMN id SET NOT NULL,
    ALTER COLUMN subject_user_id SET NOT NULL,
    ALTER COLUMN scope SET NOT NULL,
    ALTER COLUMN operator_user_id SET NOT NULL,
    ALTER COLUMN request_id SET NOT NULL,
    ALTER COLUMN reason SET NOT NULL,
    ALTER COLUMN before_snapshot SET NOT NULL,
    ALTER COLUMN after_snapshot SET NOT NULL,
    ALTER COLUMN evidence_jsonb SET DEFAULT '{}'::jsonb,
    ALTER COLUMN evidence_jsonb SET NOT NULL,
    ALTER COLUMN version_before SET NOT NULL,
    ALTER COLUMN version_after SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE customer_agent_binding_history
    ADD COLUMN IF NOT EXISTS id BIGSERIAL,
    ADD COLUMN IF NOT EXISTS customer_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS provider_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS relationship_state TEXT,
    ADD COLUMN IF NOT EXISTS source_binding_id INTEGER,
    ADD COLUMN IF NOT EXISTS binding_source TEXT,
    ADD COLUMN IF NOT EXISTS source_token TEXT,
    ADD COLUMN IF NOT EXISTS effective_from TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS effective_to TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS dispute_status TEXT,
    ADD COLUMN IF NOT EXISTS dispute_note TEXT,
    ADD COLUMN IF NOT EXISTS created_by_operator_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS created_reason TEXT,
    ADD COLUMN IF NOT EXISTS created_request_id TEXT,
    ADD COLUMN IF NOT EXISTS ended_by_operator_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS ended_reason TEXT,
    ADD COLUMN IF NOT EXISTS ended_request_id TEXT,
    ADD COLUMN IF NOT EXISTS evidence_jsonb JSONB DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE customer_agent_binding_history
    ALTER COLUMN id SET NOT NULL,
    ALTER COLUMN customer_user_id SET NOT NULL,
    ALTER COLUMN relationship_state SET NOT NULL,
    ALTER COLUMN effective_from SET DEFAULT NOW(),
    ALTER COLUMN effective_from SET NOT NULL,
    ALTER COLUMN evidence_jsonb SET DEFAULT '{}'::jsonb,
    ALTER COLUMN evidence_jsonb SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid='admin_user_governance_versions'::regclass
          AND contype='p'
    ) THEN
        ALTER TABLE admin_user_governance_versions
            ADD PRIMARY KEY (subject_user_id, scope);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid='admin_user_governance_audits'::regclass
          AND contype='p'
    ) THEN
        ALTER TABLE admin_user_governance_audits ADD PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid='customer_agent_binding_history'::regclass
          AND contype='p'
    ) THEN
        ALTER TABLE customer_agent_binding_history ADD PRIMARY KEY (id);
    END IF;
END $$;

DO $$
DECLARE
    spec RECORD;
    actual_type TEXT;
    actual_nullable TEXT;
BEGIN
    FOR spec IN SELECT * FROM (VALUES
        ('admin_user_governance_versions','subject_user_id','integer','NO'),
        ('admin_user_governance_versions','scope','text','NO'),
        ('admin_user_governance_versions','version','bigint','NO'),
        ('admin_user_governance_versions','updated_at','timestamp with time zone','NO'),
        ('admin_user_governance_audits','id','bigint','NO'),
        ('admin_user_governance_audits','subject_user_id','integer','NO'),
        ('admin_user_governance_audits','scope','text','NO'),
        ('admin_user_governance_audits','operator_user_id','integer','NO'),
        ('admin_user_governance_audits','operator_username','text','YES'),
        ('admin_user_governance_audits','request_id','text','NO'),
        ('admin_user_governance_audits','reason','text','NO'),
        ('admin_user_governance_audits','before_snapshot','jsonb','NO'),
        ('admin_user_governance_audits','after_snapshot','jsonb','NO'),
        ('admin_user_governance_audits','evidence_jsonb','jsonb','NO'),
        ('admin_user_governance_audits','version_before','bigint','NO'),
        ('admin_user_governance_audits','version_after','bigint','NO'),
        ('admin_user_governance_audits','ip_address','text','YES'),
        ('admin_user_governance_audits','created_at','timestamp with time zone','NO'),
        ('customer_agent_binding_history','id','bigint','NO'),
        ('customer_agent_binding_history','customer_user_id','integer','NO'),
        ('customer_agent_binding_history','provider_user_id','integer','YES'),
        ('customer_agent_binding_history','relationship_state','text','NO'),
        ('customer_agent_binding_history','source_binding_id','integer','YES'),
        ('customer_agent_binding_history','binding_source','text','YES'),
        ('customer_agent_binding_history','source_token','text','YES'),
        ('customer_agent_binding_history','effective_from','timestamp with time zone','NO'),
        ('customer_agent_binding_history','effective_to','timestamp with time zone','YES'),
        ('customer_agent_binding_history','dispute_status','text','YES'),
        ('customer_agent_binding_history','dispute_note','text','YES'),
        ('customer_agent_binding_history','created_by_operator_user_id','integer','YES'),
        ('customer_agent_binding_history','created_reason','text','YES'),
        ('customer_agent_binding_history','created_request_id','text','YES'),
        ('customer_agent_binding_history','ended_by_operator_user_id','integer','YES'),
        ('customer_agent_binding_history','ended_reason','text','YES'),
        ('customer_agent_binding_history','ended_request_id','text','YES'),
        ('customer_agent_binding_history','evidence_jsonb','jsonb','NO'),
        ('customer_agent_binding_history','created_at','timestamp with time zone','NO')
    ) AS expected(table_name,column_name,data_type,is_nullable)
    LOOP
        SELECT c.data_type,c.is_nullable INTO actual_type,actual_nullable
        FROM information_schema.columns c
        WHERE c.table_schema=current_schema()
          AND c.table_name=spec.table_name AND c.column_name=spec.column_name;
        IF actual_type IS DISTINCT FROM spec.data_type
           OR actual_nullable IS DISTINCT FROM spec.is_nullable THEN
            RAISE EXCEPTION 'governance schema mismatch %.% expected %/% got %/%',
                spec.table_name,spec.column_name,spec.data_type,spec.is_nullable,
                actual_type,actual_nullable;
        END IF;
    END LOOP;

    IF (SELECT COUNT(*) FROM pg_constraint
        WHERE contype='p' AND conrelid IN (
          'admin_user_governance_versions'::regclass,
          'admin_user_governance_audits'::regclass,
          'customer_agent_binding_history'::regclass
        )) <> 3 THEN
        RAISE EXCEPTION 'governance primary key set incomplete';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='admin_user_governance_versions_scope_check'
                   AND conrelid='admin_user_governance_versions'::regclass) THEN
        ALTER TABLE admin_user_governance_versions ADD CONSTRAINT admin_user_governance_versions_scope_check
            CHECK (scope IN ('business_identity','commercial_binding','platform_access'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='admin_user_governance_versions_version_check'
                   AND conrelid='admin_user_governance_versions'::regclass) THEN
        ALTER TABLE admin_user_governance_versions ADD CONSTRAINT admin_user_governance_versions_version_check
            CHECK (version >= 1);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='admin_user_governance_audits_scope_check'
                   AND conrelid='admin_user_governance_audits'::regclass) THEN
        ALTER TABLE admin_user_governance_audits ADD CONSTRAINT admin_user_governance_audits_scope_check
            CHECK (scope IN ('business_identity','commercial_binding','platform_access'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='admin_user_governance_audits_reason_check'
                   AND conrelid='admin_user_governance_audits'::regclass) THEN
        ALTER TABLE admin_user_governance_audits ADD CONSTRAINT admin_user_governance_audits_reason_check
            CHECK (char_length(btrim(reason)) BETWEEN 2 AND 500);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='admin_user_governance_audits_version_before_check'
                   AND conrelid='admin_user_governance_audits'::regclass) THEN
        ALTER TABLE admin_user_governance_audits ADD CONSTRAINT admin_user_governance_audits_version_before_check
            CHECK (version_before >= 1);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='admin_user_governance_audits_version_after_check'
                   AND conrelid='admin_user_governance_audits'::regclass) THEN
        ALTER TABLE admin_user_governance_audits ADD CONSTRAINT admin_user_governance_audits_version_after_check
            CHECK (version_after = version_before + 1);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='customer_agent_binding_history_relationship_state_check'
                   AND conrelid='customer_agent_binding_history'::regclass) THEN
        ALTER TABLE customer_agent_binding_history ADD CONSTRAINT customer_agent_binding_history_relationship_state_check
            CHECK (relationship_state IN ('service_provider','platform_direct'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='customer_agent_binding_history_provider_state_check'
                   AND conrelid='customer_agent_binding_history'::regclass) THEN
        ALTER TABLE customer_agent_binding_history ADD CONSTRAINT customer_agent_binding_history_provider_state_check
            CHECK ((relationship_state='service_provider' AND provider_user_id IS NOT NULL)
                OR (relationship_state='platform_direct' AND provider_user_id IS NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='customer_agent_binding_history_effective_range_check'
                   AND conrelid='customer_agent_binding_history'::regclass) THEN
        ALTER TABLE customer_agent_binding_history ADD CONSTRAINT customer_agent_binding_history_effective_range_check
            CHECK (effective_to IS NULL OR effective_to >= effective_from);
    END IF;
END $$;

-- @index-guard idx_admin_user_governance_audit_subject ON admin_user_governance_audits plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_user_governance_audit_subject' AND i.indrelid = to_regclass('admin_user_governance_audits')) THEN
        NULL;  -- 已在 admin_user_governance_audits 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_user_governance_audit_subject' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('admin_user_governance_audits'))) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_user_governance_audit_subject 已存在但不在 admin_user_governance_audits 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_user_governance_audit_subject' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('admin_user_governance_audits')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_user_governance_audit_subject ON admin_user_governance_audits (subject_user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_admin_user_governance_audit_operator ON admin_user_governance_audits plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_admin_user_governance_audit_operator' AND i.indrelid = to_regclass('admin_user_governance_audits')) THEN
        NULL;  -- 已在 admin_user_governance_audits 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_admin_user_governance_audit_operator' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('admin_user_governance_audits'))) THEN
        RAISE EXCEPTION '[index-guard] idx_admin_user_governance_audit_operator 已存在但不在 admin_user_governance_audits 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_admin_user_governance_audit_operator' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('admin_user_governance_audits')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_admin_user_governance_audit_operator ON admin_user_governance_audits (operator_user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard uq_admin_user_governance_audits_request_id ON admin_user_governance_audits unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_admin_user_governance_audits_request_id' AND i.indrelid = to_regclass('admin_user_governance_audits')) THEN
        NULL;  -- 已在 admin_user_governance_audits 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_admin_user_governance_audits_request_id' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('admin_user_governance_audits'))) THEN
        RAISE EXCEPTION '[index-guard] uq_admin_user_governance_audits_request_id 已存在但不在 admin_user_governance_audits 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_admin_user_governance_audits_request_id' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('admin_user_governance_audits')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_admin_user_governance_audits_request_id ON admin_user_governance_audits (request_id);
    END IF;
END $idxguard$;
-- @index-guard uq_customer_agent_binding_history_active ON customer_agent_binding_history unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_customer_agent_binding_history_active' AND i.indrelid = to_regclass('customer_agent_binding_history')) THEN
        NULL;  -- 已在 customer_agent_binding_history 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_customer_agent_binding_history_active' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('customer_agent_binding_history'))) THEN
        RAISE EXCEPTION '[index-guard] uq_customer_agent_binding_history_active 已存在但不在 customer_agent_binding_history 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_customer_agent_binding_history_active' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('customer_agent_binding_history')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_customer_agent_binding_history_active ON customer_agent_binding_history (customer_user_id) WHERE effective_to IS NULL;
    END IF;
END $idxguard$;
-- @index-guard uq_customer_agent_binding_history_created_request ON customer_agent_binding_history unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_customer_agent_binding_history_created_request' AND i.indrelid = to_regclass('customer_agent_binding_history')) THEN
        NULL;  -- 已在 customer_agent_binding_history 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_customer_agent_binding_history_created_request' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('customer_agent_binding_history'))) THEN
        RAISE EXCEPTION '[index-guard] uq_customer_agent_binding_history_created_request 已存在但不在 customer_agent_binding_history 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_customer_agent_binding_history_created_request' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('customer_agent_binding_history')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_customer_agent_binding_history_created_request ON customer_agent_binding_history (created_request_id) WHERE created_request_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_customer_agent_binding_history_timeline ON customer_agent_binding_history plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_customer_agent_binding_history_timeline' AND i.indrelid = to_regclass('customer_agent_binding_history')) THEN
        NULL;  -- 已在 customer_agent_binding_history 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_customer_agent_binding_history_timeline' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('customer_agent_binding_history'))) THEN
        RAISE EXCEPTION '[index-guard] idx_customer_agent_binding_history_timeline 已存在但不在 customer_agent_binding_history 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_customer_agent_binding_history_timeline' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('customer_agent_binding_history')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_customer_agent_binding_history_timeline ON customer_agent_binding_history (customer_user_id, effective_from DESC, id DESC);
    END IF;
END $idxguard$;

DO $$
DECLARE
    missing TEXT[] := ARRAY[]::TEXT[];
BEGIN
    IF to_regclass('admin_user_governance_versions') IS NULL THEN
        missing := array_append(missing, 'admin_user_governance_versions');
    END IF;
    IF to_regclass('admin_user_governance_audits') IS NULL THEN
        missing := array_append(missing, 'admin_user_governance_audits');
    END IF;
    IF to_regclass('customer_agent_binding_history') IS NULL THEN
        missing := array_append(missing, 'customer_agent_binding_history');
    END IF;
    IF array_length(missing, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'admin user governance migration incomplete: %', missing;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname IN (
          'admin_user_governance_versions_scope_check',
          'admin_user_governance_versions_version_check',
          'admin_user_governance_audits_scope_check',
          'admin_user_governance_audits_reason_check',
          'admin_user_governance_audits_version_before_check',
          'admin_user_governance_audits_version_after_check',
          'customer_agent_binding_history_relationship_state_check',
          'customer_agent_binding_history_provider_state_check',
          'customer_agent_binding_history_effective_range_check'
        )
          AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())
          AND convalidated IS NOT TRUE
    ) THEN
        RAISE EXCEPTION 'governance CHECK constraint not validated';
    END IF;
    IF (SELECT COUNT(*) FROM pg_constraint
        WHERE conname IN (
          'admin_user_governance_versions_scope_check',
          'admin_user_governance_versions_version_check',
          'admin_user_governance_audits_scope_check',
          'admin_user_governance_audits_reason_check',
          'admin_user_governance_audits_version_before_check',
          'admin_user_governance_audits_version_after_check',
          'customer_agent_binding_history_relationship_state_check',
          'customer_agent_binding_history_provider_state_check',
          'customer_agent_binding_history_effective_range_check'
        ) AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())
          AND convalidated IS TRUE) <> 9 THEN
        RAISE EXCEPTION 'governance CHECK constraint set incomplete';
    END IF;
    -- 🔴 (tablename, indexname) 成对判存,不能只按 indexname:索引名只在 schema 内唯一、
    --    **不绑表**。只数名字时,别的表上的 6 个同名索引也能把这条凑满 6 ——
    --    governance 的审计索引与租户唯一键其实一个都没建,而 readiness 报绿。
    IF (SELECT COUNT(*) FROM pg_indexes
        WHERE schemaname=current_schema() AND (tablename, indexname) IN (
          ('admin_user_governance_audits',  'idx_admin_user_governance_audit_subject'),
          ('admin_user_governance_audits',  'idx_admin_user_governance_audit_operator'),
          ('admin_user_governance_audits',  'uq_admin_user_governance_audits_request_id'),
          ('customer_agent_binding_history','uq_customer_agent_binding_history_active'),
          ('customer_agent_binding_history','uq_customer_agent_binding_history_created_request'),
          ('customer_agent_binding_history','idx_customer_agent_binding_history_timeline')
        )) <> 6 THEN
        RAISE EXCEPTION 'governance index/uniqueness set incomplete(或索引长在了别的表上)';
    END IF;
END $$;
