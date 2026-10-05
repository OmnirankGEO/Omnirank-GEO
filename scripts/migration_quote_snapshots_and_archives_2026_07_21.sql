-- Per-quote coefficient snapshots and recoverable quote/project archives.
-- PostgreSQL 16 · additive/idempotent · exact-definition fail-closed.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
SET LOCAL search_path = public, pg_catalog;

DO $$
DECLARE
    relation_name text;
    relation_kind "char";
    relation_persistence "char";
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'quotes', 'brands', 'keyword_selection_sessions', 'users',
        'organization_memberships', 'client_profiles', 'diagnosis_records'
    ] LOOP
        SELECT c.relkind, c.relpersistence
          INTO relation_kind, relation_persistence
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relname = relation_name;
        IF relation_kind IS DISTINCT FROM 'r' OR relation_persistence IS DISTINCT FROM 'p' THEN
            RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: public.% must be a permanent ordinary table', relation_name;
        END IF;
    END LOOP;
END $$;

-- Composite owner keys are required before the cross-tenant FKs are added.
-- @index-guard ux_quotes_id_brand ON quotes unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_quotes_id_brand' AND i.indrelid = to_regclass('public.quotes')) THEN
        NULL;  -- 已在 public.quotes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_quotes_id_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_quotes_id_brand 已存在但不在 public.quotes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_quotes_id_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_quotes_id_brand ON public.quotes USING btree (id, brand_id);
    END IF;
END $idxguard$;
-- @index-guard ux_kss_id_quote_brand ON keyword_selection_sessions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_kss_id_quote_brand' AND i.indrelid = to_regclass('public.keyword_selection_sessions')) THEN
        NULL;  -- 已在 public.keyword_selection_sessions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_kss_id_quote_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_kss_id_quote_brand 已存在但不在 public.keyword_selection_sessions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_kss_id_quote_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_kss_id_quote_brand ON public.keyword_selection_sessions USING btree (id, quote_id, brand_id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS public.quote_pricing_snapshots (
    id BIGSERIAL,
    quote_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    selection_session_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    actor_user_id INTEGER,
    actor_membership_id BIGINT,
    previous_coefficient NUMERIC(8,4),
    coefficient NUMERIC(8,4),
    reason TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    pricing_snapshot JSONB NOT NULL,
    clusters_snapshot JSONB,
    snapshot_hash CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Repair a safely incomplete same-name table. Wrong existing definitions are
-- rejected by the catalog contract below rather than silently coerced.
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS quote_id INTEGER NOT NULL;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS brand_id INTEGER NOT NULL;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS selection_session_id INTEGER NOT NULL;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS actor_user_id INTEGER;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS actor_membership_id BIGINT;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS previous_coefficient NUMERIC(8,4);
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS coefficient NUMERIC(8,4);
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS calculation_version TEXT NOT NULL;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS pricing_snapshot JSONB NOT NULL;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS clusters_snapshot JSONB;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS snapshot_hash CHAR(64) NOT NULL;
ALTER TABLE public.quote_pricing_snapshots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS active_pricing_snapshot_id BIGINT;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE public.keyword_selection_sessions ADD COLUMN IF NOT EXISTS active_pricing_snapshot_id BIGINT;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS status_before_archive TEXT;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS writing_status_before_archive TEXT;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS archived_by_user_id INTEGER;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS archive_reason TEXT;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS archived_with_brand_at TIMESTAMPTZ;
ALTER TABLE public.keyword_selection_sessions ADD COLUMN IF NOT EXISTS status_before_archive TEXT;
ALTER TABLE public.keyword_selection_sessions ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE public.keyword_selection_sessions ADD COLUMN IF NOT EXISTS archived_by_user_id INTEGER;
ALTER TABLE public.keyword_selection_sessions ADD COLUMN IF NOT EXISTS archived_with_brand_at TIMESTAMPTZ;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS archived_with_brand_at TIMESTAMPTZ;
ALTER TABLE public.diagnosis_records ADD COLUMN IF NOT EXISTS archived_with_brand_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_pkey') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_version_unique') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_version_unique UNIQUE (quote_id, version);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_owner_unique') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_owner_unique UNIQUE (id, quote_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_version_check') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_version_check CHECK (version > 0) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_reason_check') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_reason_check CHECK (btrim(reason) <> '') NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_calculation_version_check') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_calculation_version_check CHECK (btrim(calculation_version) <> '') NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_pricing_snapshot_check') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_pricing_snapshot_check CHECK (jsonb_typeof(pricing_snapshot) = 'object') NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_clusters_snapshot_check') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_clusters_snapshot_check CHECK (clusters_snapshot IS NULL OR jsonb_typeof(clusters_snapshot) = 'object') NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_snapshot_hash_check') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_snapshot_hash_check CHECK (snapshot_hash ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_quote_id_fkey') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_quote_id_fkey FOREIGN KEY (quote_id) REFERENCES public.quotes(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_brand_id_fkey') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES public.brands(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_selection_session_id_fkey') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_selection_session_id_fkey FOREIGN KEY (selection_session_id) REFERENCES public.keyword_selection_sessions(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_actor_user_id_fkey') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_actor_membership_id_fkey') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_actor_membership_id_fkey FOREIGN KEY (actor_membership_id) REFERENCES public.organization_memberships(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quotes'::regclass AND conname='quotes_active_pricing_snapshot_owner_fk') THEN
        ALTER TABLE public.quotes ADD CONSTRAINT quotes_active_pricing_snapshot_owner_fk FOREIGN KEY (active_pricing_snapshot_id,id) REFERENCES public.quote_pricing_snapshots(id,quote_id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.keyword_selection_sessions'::regclass AND conname='kss_active_pricing_snapshot_owner_fk') THEN
        ALTER TABLE public.keyword_selection_sessions ADD CONSTRAINT kss_active_pricing_snapshot_owner_fk FOREIGN KEY (active_pricing_snapshot_id,quote_id) REFERENCES public.quote_pricing_snapshots(id,quote_id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_quote_brand_fk') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_quote_brand_fk FOREIGN KEY (quote_id,brand_id) REFERENCES public.quotes(id,brand_id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quote_pricing_snapshots'::regclass AND conname='quote_pricing_snapshots_session_owner_fk') THEN
        ALTER TABLE public.quote_pricing_snapshots ADD CONSTRAINT quote_pricing_snapshots_session_owner_fk FOREIGN KEY (selection_session_id,quote_id,brand_id) REFERENCES public.keyword_selection_sessions(id,quote_id,brand_id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.quotes'::regclass AND conname='quotes_archived_by_user_fk') THEN
        ALTER TABLE public.quotes ADD CONSTRAINT quotes_archived_by_user_fk FOREIGN KEY (archived_by_user_id) REFERENCES public.users(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.keyword_selection_sessions'::regclass AND conname='kss_archived_by_user_fk') THEN
        ALTER TABLE public.keyword_selection_sessions ADD CONSTRAINT kss_archived_by_user_fk FOREIGN KEY (archived_by_user_id) REFERENCES public.users(id) NOT VALID;
    END IF;
END $$;

ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_version_check;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_reason_check;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_calculation_version_check;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_pricing_snapshot_check;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_clusters_snapshot_check;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_snapshot_hash_check;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_quote_id_fkey;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_brand_id_fkey;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_selection_session_id_fkey;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_actor_user_id_fkey;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_actor_membership_id_fkey;
ALTER TABLE public.quotes VALIDATE CONSTRAINT quotes_active_pricing_snapshot_owner_fk;
ALTER TABLE public.keyword_selection_sessions VALIDATE CONSTRAINT kss_active_pricing_snapshot_owner_fk;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_quote_brand_fk;
ALTER TABLE public.quote_pricing_snapshots VALIDATE CONSTRAINT quote_pricing_snapshots_session_owner_fk;
ALTER TABLE public.quotes VALIDATE CONSTRAINT quotes_archived_by_user_fk;
ALTER TABLE public.keyword_selection_sessions VALIDATE CONSTRAINT kss_archived_by_user_fk;

-- @index-guard idx_quote_pricing_snapshots_quote_created ON quote_pricing_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_quote_pricing_snapshots_quote_created' AND i.indrelid = to_regclass('public.quote_pricing_snapshots')) THEN
        NULL;  -- 已在 public.quote_pricing_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_quote_pricing_snapshots_quote_created' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_quote_pricing_snapshots_quote_created 已存在但不在 public.quote_pricing_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_quote_pricing_snapshots_quote_created' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_quote_pricing_snapshots_quote_created ON public.quote_pricing_snapshots USING btree (quote_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_quote_pricing_snapshots_brand ON quote_pricing_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_quote_pricing_snapshots_brand' AND i.indrelid = to_regclass('public.quote_pricing_snapshots')) THEN
        NULL;  -- 已在 public.quote_pricing_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_quote_pricing_snapshots_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_quote_pricing_snapshots_brand 已存在但不在 public.quote_pricing_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_quote_pricing_snapshots_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_quote_pricing_snapshots_brand ON public.quote_pricing_snapshots USING btree (brand_id, created_at DESC);
    END IF;
END $idxguard$;

-- Preserve already-issued customer links. Historical public states predate the
-- snapshot table, so freeze their exact persisted payload once during upgrade.
WITH candidate AS (
    SELECT DISTINCT ON (q.id)
           q.id AS quote_id,
           q.brand_id,
           s.id AS selection_session_id,
           s.pricing_data::jsonb AS pricing_snapshot,
           s.clusters_data::jsonb AS clusters_snapshot,
           COALESCE(
               (SELECT MAX(existing.version) + 1
                  FROM public.quote_pricing_snapshots existing
                 WHERE existing.quote_id=q.id),
               1
           ) AS next_version
      FROM public.quotes q
      JOIN public.keyword_selection_sessions s ON s.quote_id=q.id AND s.brand_id=q.brand_id
     WHERE q.deleted_at IS NULL
       AND q.active_pricing_snapshot_id IS NULL
       AND s.active_pricing_snapshot_id IS NULL
       AND s.status IN ('quoted','adding_keywords','confirmed','pending_payment','active','paid','payment_overdue')
       AND s.pricing_data IS NOT NULL
       AND jsonb_typeof(s.pricing_data::jsonb)='object'
     ORDER BY q.id, s.updated_at DESC NULLS LAST, s.id DESC
), inserted AS (
    INSERT INTO public.quote_pricing_snapshots(
        quote_id, brand_id, selection_session_id, version, actor_user_id,
        actor_membership_id, previous_coefficient, coefficient, reason,
        calculation_version, pricing_snapshot, clusters_snapshot, snapshot_hash,
        created_at
    )
    SELECT quote_id, brand_id, selection_session_id, next_version, NULL,
           NULL, NULL, NULL, '历史公开报价迁移冻结',
           'legacy-exact-price-freeze-v1', pricing_snapshot, clusters_snapshot,
           md5(jsonb_build_object('pricing_data',pricing_snapshot,'clusters_data',clusters_snapshot)::text)
             || md5('legacy:' || jsonb_build_object('pricing_data',pricing_snapshot,'clusters_data',clusters_snapshot)::text),
           NOW()
      FROM candidate
    ON CONFLICT (quote_id, version) DO NOTHING
    RETURNING id, quote_id, selection_session_id
), updated_sessions AS (
    UPDATE public.keyword_selection_sessions s
       SET active_pricing_snapshot_id=i.id, updated_at=NOW()
      FROM inserted i
     WHERE s.id=i.selection_session_id AND s.quote_id=i.quote_id
    RETURNING i.id, i.quote_id
)
UPDATE public.quotes q
   SET active_pricing_snapshot_id=u.id, updated_at=NOW()
  FROM updated_sessions u
 WHERE q.id=u.quote_id;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.quotes q
          JOIN public.keyword_selection_sessions s ON s.quote_id=q.id
         WHERE q.deleted_at IS NULL
           AND s.status IN ('quoted','adding_keywords','confirmed','pending_payment','active','paid','payment_overdue')
           AND (
               q.active_pricing_snapshot_id IS NULL
               OR s.active_pricing_snapshot_id IS NULL
               OR q.active_pricing_snapshot_id <> s.active_pricing_snapshot_id
           )
    ) THEN
        RAISE EXCEPTION 'QUOTE_SNAPSHOT_NEEDS_PROD: an existing public quote could not be frozen deterministically';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.reject_quote_pricing_snapshot_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'quote_pricing_snapshots are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_quote_pricing_snapshots_immutable ON public.quote_pricing_snapshots;
CREATE TRIGGER trg_quote_pricing_snapshots_immutable
BEFORE UPDATE OR DELETE ON public.quote_pricing_snapshots
FOR EACH ROW EXECUTE FUNCTION public.reject_quote_pricing_snapshot_mutation();

-- Exact column contract for every new object owned by this migration.
DO $$
DECLARE
    item record;
    actual_type text;
    actual_not_null boolean;
    actual_default text;
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('quote_pricing_snapshots','id','bigint',true,'serial'),
            ('quote_pricing_snapshots','quote_id','integer',true,NULL),
            ('quote_pricing_snapshots','brand_id','integer',true,NULL),
            ('quote_pricing_snapshots','selection_session_id','integer',true,NULL),
            ('quote_pricing_snapshots','version','integer',true,NULL),
            ('quote_pricing_snapshots','actor_user_id','integer',false,NULL),
            ('quote_pricing_snapshots','actor_membership_id','bigint',false,NULL),
            ('quote_pricing_snapshots','previous_coefficient','numeric(8,4)',false,NULL),
            ('quote_pricing_snapshots','coefficient','numeric(8,4)',false,NULL),
            ('quote_pricing_snapshots','reason','text',true,NULL),
            ('quote_pricing_snapshots','calculation_version','text',true,NULL),
            ('quote_pricing_snapshots','pricing_snapshot','jsonb',true,NULL),
            ('quote_pricing_snapshots','clusters_snapshot','jsonb',false,NULL),
            ('quote_pricing_snapshots','snapshot_hash','character(64)',true,NULL),
            ('quote_pricing_snapshots','created_at','timestamp with time zone',true,'now()'),
            ('quotes','active_pricing_snapshot_id','bigint',false,NULL),
            ('quotes','status_before_archive','text',false,NULL),
            ('quotes','writing_status_before_archive','text',false,NULL),
            ('quotes','archived_by_user_id','integer',false,NULL),
            ('quotes','archive_reason','text',false,NULL),
            ('quotes','archived_with_brand_at','timestamp with time zone',false,NULL),
            ('keyword_selection_sessions','active_pricing_snapshot_id','bigint',false,NULL),
            ('keyword_selection_sessions','status_before_archive','text',false,NULL),
            ('keyword_selection_sessions','archived_at','timestamp with time zone',false,NULL),
            ('keyword_selection_sessions','archived_by_user_id','integer',false,NULL),
            ('keyword_selection_sessions','archived_with_brand_at','timestamp with time zone',false,NULL),
            ('client_profiles','archived_with_brand_at','timestamp with time zone',false,NULL),
            ('diagnosis_records','archived_with_brand_at','timestamp with time zone',false,NULL)
        ) AS expected(table_name,column_name,type_name,not_null,default_kind)
    LOOP
        SELECT format_type(a.atttypid,a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin,d.adrelid,true)
          INTO actual_type, actual_not_null, actual_default
          FROM pg_attribute a
          JOIN pg_class c ON c.oid=a.attrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace
          LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
         WHERE n.nspname='public' AND c.relname=item.table_name
           AND a.attname=item.column_name AND a.attnum>0 AND NOT a.attisdropped;
        IF actual_type IS DISTINCT FROM item.type_name OR actual_not_null IS DISTINCT FROM item.not_null THEN
            RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: %.% expected type=% not_null=% got type=% not_null=%',
                item.table_name,item.column_name,item.type_name,item.not_null,actual_type,actual_not_null;
        END IF;
        IF item.default_kind IS NULL AND actual_default IS NOT NULL THEN
            RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: %.% has unexpected default %', item.table_name,item.column_name,actual_default;
        ELSIF item.default_kind='now()' AND actual_default IS DISTINCT FROM 'now()' THEN
            RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: %.% expected now() default, got %', item.table_name,item.column_name,actual_default;
        ELSIF item.default_kind='serial' AND (actual_default IS NULL OR actual_default !~ '^nextval\(''.*quote_pricing_snapshots_id_seq''::regclass\)$') THEN
            RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: %.% expected owned sequence default, got %', item.table_name,item.column_name,actual_default;
        END IF;
    END LOOP;
END $$;

-- Exact CHECK expressions; CHECK(TRUE), OR-bypasses and unvalidated decoys fail.
DO $$
DECLARE
    item record;
    actual_expression text;
    is_valid boolean;
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('quote_pricing_snapshots_version_check','version>0'),
            ('quote_pricing_snapshots_reason_check','btrimreason<>''''::text'),
            ('quote_pricing_snapshots_calculation_version_check','btrimcalculation_version<>''''::text'),
            ('quote_pricing_snapshots_pricing_snapshot_check','jsonb_typeofpricing_snapshot=''object''::text'),
            ('quote_pricing_snapshots_clusters_snapshot_check','clusters_snapshotisnullorjsonb_typeofclusters_snapshot=''object''::text'),
            ('quote_pricing_snapshots_snapshot_hash_check','snapshot_hash~''^[0-9a-f]{64}$''::text')
        ) AS expected(constraint_name,expression)
    LOOP
        SELECT regexp_replace(lower(pg_get_expr(conbin,conrelid,true)), '[[:space:]()]', '', 'g'), convalidated
          INTO actual_expression, is_valid
          FROM pg_constraint
         WHERE conrelid='public.quote_pricing_snapshots'::regclass
           AND conname=item.constraint_name AND contype='c';
        IF actual_expression IS DISTINCT FROM item.expression OR is_valid IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: check % expected %, got % valid=%',
                item.constraint_name,item.expression,actual_expression,is_valid;
        END IF;
    END LOOP;
END $$;

-- Exact PK/UNIQUE and FK identity/column/action contract.
DO $$
DECLARE
    item record;
    actual_local text[];
    actual_foreign text[];
    constraint_row pg_constraint%ROWTYPE;
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('quote_pricing_snapshots','quote_pricing_snapshots_pkey','p','id'),
            ('quote_pricing_snapshots','quote_pricing_snapshots_version_unique','u','quote_id,version'),
            ('quote_pricing_snapshots','quote_pricing_snapshots_owner_unique','u','id,quote_id')
        ) AS expected(table_name,constraint_name,kind,local_columns)
    LOOP
        SELECT * INTO constraint_row FROM pg_constraint
         WHERE conrelid=format('public.%I',item.table_name)::regclass AND conname=item.constraint_name;
        SELECT array_agg(a.attname ORDER BY key.ord)
          INTO actual_local
          FROM unnest(constraint_row.conkey) WITH ORDINALITY key(attnum,ord)
          JOIN pg_attribute a ON a.attrelid=constraint_row.conrelid AND a.attnum=key.attnum;
        IF constraint_row.contype IS DISTINCT FROM item.kind::"char"
           OR actual_local IS DISTINCT FROM string_to_array(item.local_columns,',')
           OR constraint_row.convalidated IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: constraint % is not exact', item.constraint_name;
        END IF;
    END LOOP;

    FOR item IN
        SELECT * FROM (VALUES
            ('quote_pricing_snapshots','quote_pricing_snapshots_quote_id_fkey','quote_id','quotes','id'),
            ('quote_pricing_snapshots','quote_pricing_snapshots_brand_id_fkey','brand_id','brands','id'),
            ('quote_pricing_snapshots','quote_pricing_snapshots_selection_session_id_fkey','selection_session_id','keyword_selection_sessions','id'),
            ('quote_pricing_snapshots','quote_pricing_snapshots_actor_user_id_fkey','actor_user_id','users','id'),
            ('quote_pricing_snapshots','quote_pricing_snapshots_actor_membership_id_fkey','actor_membership_id','organization_memberships','id'),
            ('quotes','quotes_active_pricing_snapshot_owner_fk','active_pricing_snapshot_id,id','quote_pricing_snapshots','id,quote_id'),
            ('keyword_selection_sessions','kss_active_pricing_snapshot_owner_fk','active_pricing_snapshot_id,quote_id','quote_pricing_snapshots','id,quote_id'),
            ('quote_pricing_snapshots','quote_pricing_snapshots_quote_brand_fk','quote_id,brand_id','quotes','id,brand_id'),
            ('quote_pricing_snapshots','quote_pricing_snapshots_session_owner_fk','selection_session_id,quote_id,brand_id','keyword_selection_sessions','id,quote_id,brand_id'),
            ('quotes','quotes_archived_by_user_fk','archived_by_user_id','users','id'),
            ('keyword_selection_sessions','kss_archived_by_user_fk','archived_by_user_id','users','id')
        ) AS expected(table_name,constraint_name,local_columns,foreign_table,foreign_columns)
    LOOP
        SELECT * INTO constraint_row FROM pg_constraint
         WHERE conrelid=format('public.%I',item.table_name)::regclass AND conname=item.constraint_name;
        SELECT array_agg(a.attname ORDER BY key.ord) INTO actual_local
          FROM unnest(constraint_row.conkey) WITH ORDINALITY key(attnum,ord)
          JOIN pg_attribute a ON a.attrelid=constraint_row.conrelid AND a.attnum=key.attnum;
        SELECT array_agg(a.attname ORDER BY key.ord) INTO actual_foreign
          FROM unnest(constraint_row.confkey) WITH ORDINALITY key(attnum,ord)
          JOIN pg_attribute a ON a.attrelid=constraint_row.confrelid AND a.attnum=key.attnum;
        IF constraint_row.contype IS DISTINCT FROM 'f'
           OR constraint_row.confrelid IS DISTINCT FROM format('public.%I',item.foreign_table)::regclass
           OR actual_local IS DISTINCT FROM string_to_array(item.local_columns,',')
           OR actual_foreign IS DISTINCT FROM string_to_array(item.foreign_columns,',')
           OR constraint_row.convalidated IS DISTINCT FROM true
           OR constraint_row.confupdtype IS DISTINCT FROM 'a'
           OR constraint_row.confdeltype IS DISTINCT FROM 'a'
           OR constraint_row.confmatchtype IS DISTINCT FROM 's'
           OR constraint_row.condeferrable OR constraint_row.condeferred THEN
            RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: foreign key % is not exact', item.constraint_name;
        END IF;
    END LOOP;
END $$;

-- Exact btree index keys/order/uniqueness; wrong same-name indexes fail closed.
DO $$
DECLARE
    item record;
    index_row record;
    actual_columns text[];
    actual_desc boolean[];
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('ux_quotes_id_brand','quotes','id,brand_id',true,ARRAY[false,false]),
            ('ux_kss_id_quote_brand','keyword_selection_sessions','id,quote_id,brand_id',true,ARRAY[false,false,false]),
            ('idx_quote_pricing_snapshots_quote_created','quote_pricing_snapshots','quote_id,created_at',false,ARRAY[false,true]),
            ('idx_quote_pricing_snapshots_brand','quote_pricing_snapshots','brand_id,created_at',false,ARRAY[false,true])
        ) AS expected(index_name,table_name,columns,is_unique,descending)
    LOOP
        SELECT i.*, c.relpersistence, am.amname
          INTO index_row
          FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          JOIN pg_index i ON i.indexrelid=c.oid
          JOIN pg_am am ON am.oid=c.relam
         WHERE n.nspname='public' AND c.relname=item.index_name;
        SELECT array_agg(a.attname ORDER BY key.ord)
          INTO actual_columns
          FROM unnest(index_row.indkey::smallint[]) WITH ORDINALITY key(attnum,ord)
          JOIN pg_attribute a ON a.attrelid=index_row.indrelid AND a.attnum=key.attnum
         WHERE key.ord <= index_row.indnkeyatts;
        SELECT array_agg((option_value & 1) = 1 ORDER BY ord)
          INTO actual_desc
          FROM unnest(index_row.indoption::smallint[]) WITH ORDINALITY options(option_value,ord)
         WHERE ord <= index_row.indnkeyatts;
        IF index_row.indrelid IS DISTINCT FROM format('public.%I',item.table_name)::regclass
           OR index_row.indisunique IS DISTINCT FROM item.is_unique
           OR index_row.indisvalid IS DISTINCT FROM true OR index_row.indisready IS DISTINCT FROM true
           OR index_row.indpred IS NOT NULL OR index_row.indexprs IS NOT NULL
           OR index_row.indnatts IS DISTINCT FROM index_row.indnkeyatts
           OR index_row.relpersistence IS DISTINCT FROM 'p' OR index_row.amname IS DISTINCT FROM 'btree'
           OR actual_columns IS DISTINCT FROM string_to_array(item.columns,',')
           OR actual_desc IS DISTINCT FROM item.descending THEN
            RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: index % is not exact', item.index_name;
        END IF;
    END LOOP;
END $$;

DO $$
DECLARE
    function_row record;
    trigger_row record;
    serial_sequence text;
    sequence_row record;
BEGIN
    SELECT p.oid, p.prosrc, p.prorettype, p.prosecdef, p.provolatile, l.lanname
      INTO function_row
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
     WHERE n.nspname='public' AND p.proname='reject_quote_pricing_snapshot_mutation' AND p.pronargs=0;
    IF function_row.prorettype IS DISTINCT FROM 'trigger'::regtype
       OR function_row.prosecdef OR function_row.provolatile IS DISTINCT FROM 'v'
       OR function_row.lanname IS DISTINCT FROM 'plpgsql'
       OR regexp_replace(lower(function_row.prosrc),'[[:space:];]','','g')
          IS DISTINCT FROM 'beginraiseexception''quote_pricing_snapshotsareimmutable''end' THEN
        RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: immutable function is not exact';
    END IF;

    SELECT t.tgfoid, t.tgtype, t.tgenabled
      INTO trigger_row
      FROM pg_trigger t
     WHERE t.tgrelid='public.quote_pricing_snapshots'::regclass
       AND t.tgname='trg_quote_pricing_snapshots_immutable' AND NOT t.tgisinternal;
    IF trigger_row.tgfoid IS DISTINCT FROM function_row.oid
       OR trigger_row.tgtype IS DISTINCT FROM 27 OR trigger_row.tgenabled IS DISTINCT FROM 'O' THEN
        RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: immutable trigger is not exact';
    END IF;

    serial_sequence := pg_get_serial_sequence('public.quote_pricing_snapshots','id');
    IF serial_sequence IS DISTINCT FROM 'public.quote_pricing_snapshots_id_seq' THEN
        RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: snapshot id sequence ownership is not exact: %', serial_sequence;
    END IF;
    SELECT s.*, c.relpersistence INTO sequence_row
      FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
     WHERE s.seqrelid='public.quote_pricing_snapshots_id_seq'::regclass;
    IF sequence_row.seqtypid IS DISTINCT FROM 'bigint'::regtype
       OR sequence_row.seqincrement IS DISTINCT FROM 1
       OR sequence_row.seqmin IS DISTINCT FROM 1
       OR sequence_row.seqmax IS DISTINCT FROM 9223372036854775807
       OR sequence_row.seqstart IS DISTINCT FROM 1
       OR sequence_row.seqcache IS DISTINCT FROM 1
       OR sequence_row.seqcycle OR sequence_row.relpersistence IS DISTINCT FROM 'p' THEN
        RAISE EXCEPTION 'QUOTE_SCHEMA_NEEDS_REPAIR: snapshot id sequence is not exact';
    END IF;
END $$;

COMMIT;
