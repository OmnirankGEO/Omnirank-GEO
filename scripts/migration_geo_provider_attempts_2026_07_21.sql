-- RFC: the existing generation-attempt row could not distinguish a POST that
-- was never sent from one whose response was lost. These additive fields are
-- the minimum durable anchor needed to prevent a second paid provider POST and
-- to resume polling by provider task id after a worker restart.
--
-- Production migrations are pinned to public. Never resolve this state machine
-- through current_schema() or an unqualified regclass: a search_path lure must
-- not make readiness look healthy.
ALTER TABLE public.marketing_material_generation_attempts
  ADD COLUMN IF NOT EXISTS component_id TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS submit_state TEXT NOT NULL DEFAULT 'not_started',
  ADD COLUMN IF NOT EXISTS poll_state TEXT NOT NULL DEFAULT 'not_started',
  ADD COLUMN IF NOT EXISTS submit_guard_token TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMP NULL,
  ADD COLUMN IF NOT EXISTS provider_result_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS materialization_state TEXT NOT NULL DEFAULT 'not_started',
  ADD COLUMN IF NOT EXISTS resolution_state TEXT NOT NULL DEFAULT 'automatic',
  ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP NULL;

DO $$ BEGIN
  ALTER TABLE public.marketing_material_generation_attempts
    ADD CONSTRAINT marketing_attempt_submit_state_ck CHECK (
      submit_state IN ('not_started','anchored','submitted','outcome_unknown','not_sent','rejected'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE public.marketing_material_generation_attempts
    ADD CONSTRAINT marketing_attempt_poll_state_ck CHECK (
      poll_state IN ('not_started','polling','pending','succeeded','failed','timeout'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE public.marketing_material_generation_attempts
    ADD CONSTRAINT marketing_attempt_materialization_state_ck CHECK (
      materialization_state IN ('not_started','pending','materialized','failed'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE public.marketing_material_generation_attempts
    ADD CONSTRAINT marketing_attempt_resolution_state_ck CHECK (
      resolution_state IN ('automatic','manual_required','manual_resolved'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- @index-guard uq_marketing_attempt_component_no ON marketing_material_generation_attempts unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_marketing_attempt_component_no' AND i.indrelid = to_regclass('public.marketing_material_generation_attempts')) THEN
        NULL;  -- 已在 public.marketing_material_generation_attempts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_marketing_attempt_component_no' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_marketing_attempt_component_no 已存在但不在 public.marketing_material_generation_attempts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_marketing_attempt_component_no' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_marketing_attempt_component_no ON public.marketing_material_generation_attempts (job_id, component_id, attempt_no) WHERE component_id <> '';
    END IF;
END $idxguard$;

-- @index-guard idx_marketing_attempt_recovery ON marketing_material_generation_attempts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_attempt_recovery' AND i.indrelid = to_regclass('public.marketing_material_generation_attempts')) THEN
        NULL;  -- 已在 public.marketing_material_generation_attempts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_attempt_recovery' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_attempt_recovery 已存在但不在 public.marketing_material_generation_attempts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_attempt_recovery' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_attempt_recovery ON public.marketing_material_generation_attempts (status, submit_state, poll_state, last_heartbeat_at) WHERE status IN ('pending','running');
    END IF;
END $idxguard$;

DO $$
DECLARE
  target_oid OID := to_regclass('public.marketing_material_generation_attempts');
  expected_oid OID;
  bad_columns INTEGER;
  bad_constraints INTEGER;
  bad_indexes INTEGER;
BEGIN
  IF target_oid IS NULL THEN
    RAISE EXCEPTION 'geo provider attempt migration target public.marketing_material_generation_attempts is missing';
  END IF;

  -- Build catalog-native expected definitions from the same table layout.
  -- Compare catalog expressions after pg_get_expr canonicalization. Raw
  -- pg_node_tree carries parser source locations, so byte comparison would
  -- reject an identical definition created by a different statement. The
  -- canonical expression still rejects CHECK(TRUE OR ...), predicate lures,
  -- reordered keys and alternate opclasses.
  EXECUTE 'DROP TABLE IF EXISTS pg_temp.geo_provider_attempt_expected CASCADE';
  EXECUTE 'CREATE TEMP TABLE geo_provider_attempt_expected '
          '(LIKE public.marketing_material_generation_attempts EXCLUDING DEFAULTS)';
  EXECUTE 'ALTER TABLE pg_temp.geo_provider_attempt_expected '
          'ALTER component_id SET DEFAULT '''', '
          'ALTER submit_state SET DEFAULT ''not_started'', '
          'ALTER poll_state SET DEFAULT ''not_started'', '
          'ALTER submit_guard_token SET DEFAULT '''', '
          'ALTER provider_result_jsonb SET DEFAULT ''{}''::jsonb, '
          'ALTER materialization_state SET DEFAULT ''not_started'', '
          'ALTER resolution_state SET DEFAULT ''automatic''';
  EXECUTE 'ALTER TABLE pg_temp.geo_provider_attempt_expected '
          'ADD CONSTRAINT geo_expected_submit_ck CHECK '
          '(submit_state IN (''not_started'',''anchored'',''submitted'',''outcome_unknown'',''not_sent'',''rejected'')), '
          'ADD CONSTRAINT geo_expected_poll_ck CHECK '
          '(poll_state IN (''not_started'',''polling'',''pending'',''succeeded'',''failed'',''timeout'')), '
          'ADD CONSTRAINT geo_expected_materialization_ck CHECK '
          '(materialization_state IN (''not_started'',''pending'',''materialized'',''failed'')), '
          'ADD CONSTRAINT geo_expected_resolution_ck CHECK '
          '(resolution_state IN (''automatic'',''manual_required'',''manual_resolved''))';
  EXECUTE 'CREATE UNIQUE INDEX geo_provider_expected_uq '
          'ON pg_temp.geo_provider_attempt_expected(job_id,component_id,attempt_no) '
          'WHERE component_id <> ''''';
  EXECUTE 'CREATE INDEX geo_provider_expected_recovery '
          'ON pg_temp.geo_provider_attempt_expected(status,submit_state,poll_state,last_heartbeat_at) '
          'WHERE status IN (''pending'',''running'')';
  expected_oid := to_regclass('pg_temp.geo_provider_attempt_expected');

  SELECT COUNT(*) INTO bad_columns
  FROM (VALUES
    ('component_id','text'::regtype::oid,TRUE),
    ('submit_state','text'::regtype::oid,TRUE),
    ('poll_state','text'::regtype::oid,TRUE),
    ('submit_guard_token','text'::regtype::oid,TRUE),
    ('last_heartbeat_at','timestamp without time zone'::regtype::oid,FALSE),
    ('provider_result_jsonb','jsonb'::regtype::oid,TRUE),
    ('materialization_state','text'::regtype::oid,TRUE),
    ('resolution_state','text'::regtype::oid,TRUE),
    ('resolved_at','timestamp without time zone'::regtype::oid,FALSE)
  ) wanted(attname,atttypid,attnotnull)
  LEFT JOIN pg_catalog.pg_attribute actual
    ON actual.attrelid=target_oid AND actual.attname=wanted.attname AND NOT actual.attisdropped
  LEFT JOIN pg_catalog.pg_attrdef actual_default
    ON actual_default.adrelid=target_oid AND actual_default.adnum=actual.attnum
  LEFT JOIN pg_catalog.pg_attribute expected
    ON expected.attrelid=expected_oid AND expected.attname=wanted.attname AND NOT expected.attisdropped
  LEFT JOIN pg_catalog.pg_attrdef expected_default
    ON expected_default.adrelid=expected_oid AND expected_default.adnum=expected.attnum
  WHERE actual.attnum IS NULL
     OR actual.atttypid<>wanted.atttypid
     OR actual.attnotnull<>wanted.attnotnull
     OR pg_catalog.pg_get_expr(actual_default.adbin,actual_default.adrelid,FALSE)
        IS DISTINCT FROM
        pg_catalog.pg_get_expr(expected_default.adbin,expected_default.adrelid,FALSE);
  IF bad_columns <> 0 THEN
    RAISE EXCEPTION 'geo provider attempt migration found missing/wrong columns or defaults';
  END IF;

  SELECT COUNT(*) INTO bad_constraints
  FROM (VALUES
    ('marketing_attempt_submit_state_ck','geo_expected_submit_ck'),
    ('marketing_attempt_poll_state_ck','geo_expected_poll_ck'),
    ('marketing_attempt_materialization_state_ck','geo_expected_materialization_ck'),
    ('marketing_attempt_resolution_state_ck','geo_expected_resolution_ck')
  ) wanted(actual_name,expected_name)
  LEFT JOIN pg_catalog.pg_constraint actual
    ON actual.conrelid=target_oid AND actual.conname=wanted.actual_name
  LEFT JOIN pg_catalog.pg_constraint expected
    ON expected.conrelid=expected_oid AND expected.conname=wanted.expected_name
  WHERE actual.oid IS NULL OR actual.contype<>'c' OR NOT actual.convalidated
     OR pg_catalog.pg_get_expr(actual.conbin,actual.conrelid,FALSE)
        IS DISTINCT FROM pg_catalog.pg_get_expr(expected.conbin,expected.conrelid,FALSE);
  IF bad_constraints <> 0 THEN
    RAISE EXCEPTION 'geo provider attempt migration found missing/weak/invalid CHECK constraints';
  END IF;

  SELECT COUNT(*) INTO bad_indexes
  FROM (VALUES
    ('uq_marketing_attempt_component_no','geo_provider_expected_uq'),
    ('idx_marketing_attempt_recovery','geo_provider_expected_recovery')
  ) wanted(actual_name,expected_name)
  LEFT JOIN pg_catalog.pg_class actual_class
    ON actual_class.oid=to_regclass(format('public.%I',wanted.actual_name))
  LEFT JOIN pg_catalog.pg_namespace actual_namespace
    ON actual_namespace.oid=actual_class.relnamespace
  LEFT JOIN pg_catalog.pg_index actual
    ON actual.indexrelid=actual_class.oid
  LEFT JOIN pg_catalog.pg_class expected_class
    ON expected_class.relname=wanted.expected_name
   AND expected_class.relnamespace=pg_my_temp_schema()
  LEFT JOIN pg_catalog.pg_index expected
    ON expected.indexrelid=expected_class.oid
  WHERE actual_class.oid IS NULL OR actual_namespace.nspname<>'public'
     OR actual_class.relkind<>'i' OR actual.indrelid<>target_oid
     OR NOT actual.indisvalid OR NOT actual.indisready OR NOT actual.indislive
     OR actual.indisunique<>expected.indisunique
     OR actual.indisprimary<>expected.indisprimary
     OR actual.indisexclusion<>expected.indisexclusion
     OR actual.indnullsnotdistinct<>expected.indnullsnotdistinct
     OR actual.indnatts<>expected.indnatts OR actual.indnkeyatts<>expected.indnkeyatts
     OR (SELECT array_agg(attribute.attname ORDER BY key_column.ordinality)
         FROM unnest(actual.indkey::smallint[]) WITH ORDINALITY AS key_column(attnum,ordinality)
         LEFT JOIN pg_catalog.pg_attribute attribute
           ON attribute.attrelid=actual.indrelid AND attribute.attnum=key_column.attnum)
        IS DISTINCT FROM
        (SELECT array_agg(attribute.attname ORDER BY key_column.ordinality)
         FROM unnest(expected.indkey::smallint[]) WITH ORDINALITY AS key_column(attnum,ordinality)
         LEFT JOIN pg_catalog.pg_attribute attribute
           ON attribute.attrelid=expected.indrelid AND attribute.attnum=key_column.attnum)
     OR actual.indcollation::text IS DISTINCT FROM expected.indcollation::text
     OR actual.indclass::text IS DISTINCT FROM expected.indclass::text
     OR actual.indoption::text IS DISTINCT FROM expected.indoption::text
     OR pg_catalog.pg_get_expr(actual.indexprs,actual.indrelid,FALSE)
        IS DISTINCT FROM pg_catalog.pg_get_expr(expected.indexprs,expected.indrelid,FALSE)
     OR pg_catalog.pg_get_expr(actual.indpred,actual.indrelid,FALSE)
        IS DISTINCT FROM pg_catalog.pg_get_expr(expected.indpred,expected.indrelid,FALSE)
     OR actual_class.relam<>expected_class.relam;
  IF bad_indexes <> 0 THEN
    RAISE EXCEPTION 'geo provider attempt migration found missing/wrong/invalid indexes';
  END IF;

  EXECUTE 'DROP TABLE pg_temp.geo_provider_attempt_expected CASCADE';
END $$;
