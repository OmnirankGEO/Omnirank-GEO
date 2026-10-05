-- Continuous-monitoring product entitlement SSOT.
--
-- Yuanbao remains a paid-diagnosis-only surface. Purchased monitoring phrases
-- use a versioned, immutable product matrix; runtime preferences never grant
-- provider rights. This migration restores only the exact retired default
-- written by migration_monitoring_yuanbao_default_2026_07_20. Custom subsets,
-- orderings and platform selections are left untouched.

SET LOCAL search_path = pg_catalog, public;

CREATE TABLE IF NOT EXISTS public.monitoring_product_platform_matrices (
    version TEXT PRIMARY KEY,
    platforms TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO public.monitoring_product_platform_matrices (version, platforms)
VALUES ('monitoring-classic4-v1', 'dashscope,deepseek,kimi,doubao')
ON CONFLICT (version) DO NOTHING;

DO $$
DECLARE
    actual_platforms TEXT;
BEGIN
    SELECT platforms
      INTO actual_platforms
      FROM public.monitoring_product_platform_matrices
     WHERE version = 'monitoring-classic4-v1';
    IF actual_platforms IS DISTINCT FROM 'dashscope,deepseek,kimi,doubao' THEN
        RAISE EXCEPTION 'monitoring product matrix drift: monitoring-classic4-v1=%',
            actual_platforms;
    END IF;
END $$;

ALTER TABLE public.confirmed_keywords
    ADD COLUMN IF NOT EXISTS monitoring_product_version TEXT;

UPDATE public.confirmed_keywords
   SET monitoring_product_version = 'monitoring-classic4-v1'
 WHERE monitoring_product_version IS NULL;

ALTER TABLE public.confirmed_keywords
    ALTER COLUMN monitoring_product_version
    SET DEFAULT 'monitoring-classic4-v1';
ALTER TABLE public.confirmed_keywords
    ALTER COLUMN monitoring_product_version SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_constraint
         WHERE conname = 'fk_confirmed_keywords_monitoring_product_version'
           AND conrelid = 'public.confirmed_keywords'::pg_catalog.regclass
    ) THEN
        ALTER TABLE public.confirmed_keywords
            ADD CONSTRAINT fk_confirmed_keywords_monitoring_product_version
            FOREIGN KEY (monitoring_product_version)
            REFERENCES public.monitoring_product_platform_matrices(version);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.reject_monitoring_product_matrix_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'monitoring product matrices are append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_monitoring_product_matrix_append_only
    ON public.monitoring_product_platform_matrices;
CREATE TRIGGER trg_monitoring_product_matrix_append_only
BEFORE UPDATE OR DELETE ON public.monitoring_product_platform_matrices
FOR EACH ROW EXECUTE FUNCTION public.reject_monitoring_product_matrix_mutation();

CREATE OR REPLACE FUNCTION public.reject_confirmed_monitoring_product_rebind()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.monitoring_product_version IS DISTINCT FROM NEW.monitoring_product_version THEN
        RAISE EXCEPTION 'confirmed monitoring product version is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_confirmed_monitoring_product_immutable
    ON public.confirmed_keywords;
CREATE TRIGGER trg_confirmed_monitoring_product_immutable
BEFORE UPDATE OF monitoring_product_version ON public.confirmed_keywords
FOR EACH ROW EXECUTE FUNCTION public.reject_confirmed_monitoring_product_rebind();

-- Restore only rows whose provenance was captured before the retired migration
-- changed the classic-four default to Yuanbao. An identical value without this
-- evidence is not guessed or rewritten.
UPDATE public.client_keywords target
   SET platforms = backup.old_value
  FROM public.monitoring_platform_matrix_backup_20260720 backup
 WHERE backup.source_table = 'client_keywords'
   AND backup.source_id = target.id
   AND backup.old_value = 'dashscope,deepseek,kimi,doubao'
   AND target.platforms = 'dashscope,deepseek,doubao,yuanbao';

UPDATE public.extra_keywords target
   SET platforms = backup.old_value
  FROM public.monitoring_platform_matrix_backup_20260720 backup
 WHERE backup.source_table = 'extra_keywords'
   AND backup.source_id = target.id
   AND backup.old_value = 'dashscope,deepseek,kimi,doubao'
   AND target.platforms = 'dashscope,deepseek,doubao,yuanbao';

UPDATE public.monitoring_config target
   SET default_platforms = backup.old_value,
       updated_at = NOW()
  FROM public.monitoring_platform_matrix_backup_20260720 backup
 WHERE backup.source_table = 'monitoring_config'
   AND backup.source_id = target.id
   AND backup.old_value = 'dashscope,deepseek,kimi,doubao'
   AND target.default_platforms = 'dashscope,deepseek,doubao,yuanbao';

ALTER TABLE public.client_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,kimi,doubao';
ALTER TABLE public.extra_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,kimi,doubao';
ALTER TABLE public.monitoring_config
    ALTER COLUMN default_platforms SET DEFAULT 'dashscope,deepseek,kimi,doubao';
ALTER TABLE public.monitoring_tasks
    ALTER COLUMN platform_count SET DEFAULT 0;

DO $$
DECLARE
    matrix_oid OID;
    confirmed_oid OID := 'public.confirmed_keywords'::pg_catalog.regclass;
    matrix_function_oid OID;
    confirmed_function_oid OID;
    confirmed_version_attnum SMALLINT;
    mismatch_count INTEGER;
BEGIN
    SELECT c.oid
      INTO matrix_oid
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relname = 'monitoring_product_platform_matrices'
       AND c.relkind = 'r'
       AND c.relpersistence = 'p';
    IF matrix_oid IS NULL THEN
        RAISE EXCEPTION 'public monitoring product matrix is not an ordinary permanent table';
    END IF;

    SELECT COUNT(*)
      INTO mismatch_count
      FROM (
            SELECT a.attname,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                   a.attnotnull,
                   pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS column_default
              FROM pg_catalog.pg_attribute a
              LEFT JOIN pg_catalog.pg_attrdef d
                ON d.adrelid = a.attrelid
               AND d.adnum = a.attnum
             WHERE a.attrelid = matrix_oid
               AND a.attnum > 0
               AND NOT a.attisdropped
      ) actual
      FULL JOIN (
            VALUES
                ('version'::name, 'text'::text, TRUE, NULL::text),
                ('platforms'::name, 'text'::text, TRUE, NULL::text),
                ('created_at'::name, 'timestamp with time zone'::text, TRUE, 'now()'::text)
      ) expected(attname, data_type, attnotnull, column_default)
        USING (attname)
     WHERE actual.attname IS NULL
        OR expected.attname IS NULL
        OR actual.data_type IS DISTINCT FROM expected.data_type
        OR actual.attnotnull IS DISTINCT FROM expected.attnotnull
        OR actual.column_default IS DISTINCT FROM expected.column_default;
    IF mismatch_count <> 0 THEN
        RAISE EXCEPTION 'monitoring product matrix column contract drift';
    END IF;

    SELECT COUNT(*)
      INTO mismatch_count
      FROM pg_catalog.pg_constraint c
     WHERE c.conrelid = matrix_oid
       AND c.contype = 'p'
       AND c.convalidated
       AND c.conkey = ARRAY[(
           SELECT a.attnum
             FROM pg_catalog.pg_attribute a
            WHERE a.attrelid = matrix_oid
              AND a.attname = 'version'
              AND NOT a.attisdropped
       )]::SMALLINT[];
    IF mismatch_count <> 1 THEN
        RAISE EXCEPTION 'monitoring product matrix PK contract drift';
    END IF;

    SELECT a.attnum
      INTO confirmed_version_attnum
      FROM pg_catalog.pg_attribute a
      LEFT JOIN pg_catalog.pg_attrdef d
        ON d.adrelid = a.attrelid
       AND d.adnum = a.attnum
     WHERE a.attrelid = confirmed_oid
       AND a.attname = 'monitoring_product_version'
       AND NOT a.attisdropped
       AND pg_catalog.format_type(a.atttypid, a.atttypmod) = 'text'
       AND a.attnotnull
       AND pg_catalog.pg_get_expr(d.adbin, d.adrelid)
           = '''monitoring-classic4-v1''::text';
    IF confirmed_version_attnum IS NULL THEN
        RAISE EXCEPTION 'confirmed monitoring product column contract drift';
    END IF;

    SELECT COUNT(*)
      INTO mismatch_count
      FROM pg_catalog.pg_constraint c
     WHERE c.conrelid = confirmed_oid
       AND c.conname = 'fk_confirmed_keywords_monitoring_product_version'
       AND c.contype = 'f'
       AND c.convalidated
       AND NOT c.condeferrable
       AND NOT c.condeferred
       AND c.confupdtype = 'a'
       AND c.confdeltype = 'a'
       AND c.confmatchtype = 's'
       AND c.confrelid = matrix_oid
       AND c.conkey = ARRAY[confirmed_version_attnum]::SMALLINT[]
       AND c.confkey = ARRAY[(
           SELECT a.attnum
             FROM pg_catalog.pg_attribute a
            WHERE a.attrelid = matrix_oid
              AND a.attname = 'version'
              AND NOT a.attisdropped
       )]::SMALLINT[];
    IF mismatch_count <> 1 THEN
        RAISE EXCEPTION 'monitoring product FK contract drift';
    END IF;

    SELECT COUNT(*)
      INTO mismatch_count
      FROM (
            VALUES
                ('public.client_keywords'::pg_catalog.regclass,
                 'platforms'::name, '''dashscope,deepseek,kimi,doubao''::text'),
                ('public.extra_keywords'::pg_catalog.regclass,
                 'platforms'::name, '''dashscope,deepseek,kimi,doubao''::text'),
                ('public.monitoring_config'::pg_catalog.regclass,
                 'default_platforms'::name, '''dashscope,deepseek,kimi,doubao''::text'),
                ('public.monitoring_tasks'::pg_catalog.regclass,
                 'platform_count'::name, '0'::text)
      ) expected(relid, attname, column_default)
      LEFT JOIN pg_catalog.pg_attribute a
        ON a.attrelid = expected.relid
       AND a.attname = expected.attname
       AND NOT a.attisdropped
      LEFT JOIN pg_catalog.pg_attrdef d
        ON d.adrelid = a.attrelid
       AND d.adnum = a.attnum
     WHERE a.attname IS NULL
        OR pg_catalog.pg_get_expr(d.adbin, d.adrelid)
           IS DISTINCT FROM expected.column_default;
    IF mismatch_count <> 0 THEN
        RAISE EXCEPTION 'monitoring default contract drift';
    END IF;

    SELECT p.oid
      INTO matrix_function_oid
      FROM pg_catalog.pg_proc p
      JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
      JOIN pg_catalog.pg_language l ON l.oid = p.prolang
     WHERE n.nspname = 'public'
       AND p.proname = 'reject_monitoring_product_matrix_mutation'
       AND pg_catalog.pg_get_function_identity_arguments(p.oid) = ''
       AND l.lanname = 'plpgsql'
       AND pg_catalog.pg_get_function_result(p.oid) = 'trigger'
       AND p.prokind = 'f'
       AND p.provolatile = 'v'
       AND NOT p.prosecdef
       AND NOT p.proleakproof
       AND p.proconfig IS NULL
       AND btrim(regexp_replace(p.prosrc, '\s+', ' ', 'g'))
           = 'BEGIN RAISE EXCEPTION ''monitoring product matrices are append-only''; END;';
    IF matrix_function_oid IS NULL THEN
        RAISE EXCEPTION 'monitoring product matrix function contract drift';
    END IF;

    SELECT p.oid
      INTO confirmed_function_oid
      FROM pg_catalog.pg_proc p
      JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
      JOIN pg_catalog.pg_language l ON l.oid = p.prolang
     WHERE n.nspname = 'public'
       AND p.proname = 'reject_confirmed_monitoring_product_rebind'
       AND pg_catalog.pg_get_function_identity_arguments(p.oid) = ''
       AND l.lanname = 'plpgsql'
       AND pg_catalog.pg_get_function_result(p.oid) = 'trigger'
       AND p.prokind = 'f'
       AND p.provolatile = 'v'
       AND NOT p.prosecdef
       AND NOT p.proleakproof
       AND p.proconfig IS NULL
       AND btrim(regexp_replace(p.prosrc, '\s+', ' ', 'g'))
           = 'BEGIN IF OLD.monitoring_product_version IS DISTINCT FROM NEW.monitoring_product_version THEN RAISE EXCEPTION ''confirmed monitoring product version is immutable''; END IF; RETURN NEW; END;';
    IF confirmed_function_oid IS NULL THEN
        RAISE EXCEPTION 'confirmed monitoring product function contract drift';
    END IF;

    SELECT COUNT(*)
      INTO mismatch_count
      FROM pg_catalog.pg_trigger t
     WHERE t.tgrelid = matrix_oid
       AND t.tgname = 'trg_monitoring_product_matrix_append_only'
       AND NOT t.tgisinternal
       AND t.tgenabled = 'O'
       AND t.tgfoid = matrix_function_oid
       AND t.tgtype = 27
       AND t.tgqual IS NULL
       AND t.tgnargs = 0
       AND t.tgattr::text = '';
    IF mismatch_count <> 1 THEN
        RAISE EXCEPTION 'monitoring product matrix trigger contract drift';
    END IF;

    SELECT COUNT(*)
      INTO mismatch_count
      FROM pg_catalog.pg_trigger t
     WHERE t.tgrelid = confirmed_oid
       AND t.tgname = 'trg_confirmed_monitoring_product_immutable'
       AND NOT t.tgisinternal
       AND t.tgenabled = 'O'
       AND t.tgfoid = confirmed_function_oid
       AND t.tgtype = 19
       AND t.tgqual IS NULL
       AND t.tgnargs = 0
       AND t.tgattr::text = confirmed_version_attnum::text;
    IF mismatch_count <> 1 THEN
        RAISE EXCEPTION 'confirmed monitoring product trigger contract drift';
    END IF;

    IF current_setting('session_replication_role') IS DISTINCT FROM 'origin' THEN
        RAISE EXCEPTION 'session_replication_role must be origin';
    END IF;
END $$;
