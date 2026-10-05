-- RFC: the deal-showcase ("晒成交") intake chain needs one durable, owner-scoped
-- draft row that survives worker restarts between the three input passes
-- (structured form, voice transcript, image materials + OCR) and the content
-- package request that later freezes the confirmed deal sheet. request_id is
-- the immutable idempotency anchor: a retried POST must replay the same row,
-- and a reused request id with a different payload must conflict loudly.
--
-- Production migrations are pinned to public. Never resolve this state machine
-- through current_schema() or an unqualified regclass: a search_path lure must
-- not make readiness look healthy.
CREATE TABLE IF NOT EXISTS public.marketing_deal_drafts (
  id BIGSERIAL PRIMARY KEY,
  owner_user_id BIGINT NOT NULL,
  brand_id BIGINT NULL,
  request_id TEXT NOT NULL,
  form_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  materials_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb,
  sheet_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

ALTER TABLE public.marketing_deal_drafts
  ADD COLUMN IF NOT EXISTS owner_user_id BIGINT NOT NULL,
  ADD COLUMN IF NOT EXISTS brand_id BIGINT NULL,
  ADD COLUMN IF NOT EXISTS request_id TEXT NOT NULL,
  ADD COLUMN IF NOT EXISTS form_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS materials_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS sheet_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft',
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();

DO $$ BEGIN
  ALTER TABLE public.marketing_deal_drafts
    ADD CONSTRAINT marketing_deal_drafts_status_ck CHECK (
      status IN ('draft','analyzed','redacted','confirmed','generating','completed','archived'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- request_id 的幂等锚点按属主收窄:唯一约束是 (owner_user_id, request_id),
-- 与 advisory lock 键及 get_deal_draft_by_request_id 同粒度——跨租户既不能
-- 抢占常见 request_id 造成 409 DoS,也不能拿全局唯一索引当存在性 oracle。
-- 旧版全局唯一索引若已存在则先替换(幂等:DROP IF EXISTS + CREATE)。
-- @drop-index-guard uq_marketing_deal_drafts_request ON marketing_deal_drafts if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_marketing_deal_drafts_request' AND i.indrelid = to_regclass('marketing_deal_drafts')) THEN
        DROP INDEX IF EXISTS uq_marketing_deal_drafts_request;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_marketing_deal_drafts_request' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] uq_marketing_deal_drafts_request 不在 marketing_deal_drafts 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_marketing_deal_drafts_request' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
CREATE UNIQUE INDEX uq_marketing_deal_drafts_request
  ON public.marketing_deal_drafts(owner_user_id, request_id);

-- @index-guard idx_marketing_deal_drafts_owner ON marketing_deal_drafts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_deal_drafts_owner' AND i.indrelid = to_regclass('public.marketing_deal_drafts')) THEN
        NULL;  -- 已在 public.marketing_deal_drafts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_deal_drafts_owner' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_deal_drafts_owner 已存在但不在 public.marketing_deal_drafts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_deal_drafts_owner' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_deal_drafts_owner ON public.marketing_deal_drafts (owner_user_id, id DESC);
    END IF;
END $idxguard$;

DO $$
DECLARE
  target_oid OID := to_regclass('public.marketing_deal_drafts');
  expected_oid OID;
  bad_columns INTEGER;
  bad_constraints INTEGER;
  bad_indexes INTEGER;
BEGIN
  IF target_oid IS NULL THEN
    RAISE EXCEPTION 'deal drafts migration target public.marketing_deal_drafts is missing';
  END IF;

  -- Build catalog-native expected definitions from the same table layout.
  -- Compare catalog expressions after pg_get_expr canonicalization. Raw
  -- pg_node_tree carries parser source locations, so byte comparison would
  -- reject an identical definition created by a different statement. The
  -- canonical expression still rejects CHECK(TRUE OR ...), predicate lures,
  -- reordered keys and alternate opclasses.
  EXECUTE 'DROP TABLE IF EXISTS pg_temp.marketing_deal_drafts_expected CASCADE';
  EXECUTE 'CREATE TEMP TABLE marketing_deal_drafts_expected (
    id BIGSERIAL PRIMARY KEY,
    owner_user_id BIGINT NOT NULL,
    brand_id BIGINT NULL,
    request_id TEXT NOT NULL,
    form_jsonb JSONB NOT NULL DEFAULT ''{}''::jsonb,
    materials_jsonb JSONB NOT NULL DEFAULT ''[]''::jsonb,
    sheet_jsonb JSONB NOT NULL DEFAULT ''{}''::jsonb,
    status TEXT NOT NULL DEFAULT ''draft'',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
  )';
  EXECUTE 'ALTER TABLE pg_temp.marketing_deal_drafts_expected '
          'ADD CONSTRAINT deal_expected_status_ck CHECK '
          '(status IN (''draft'',''analyzed'',''redacted'',''confirmed'',''generating'',''completed'',''archived''))';
  EXECUTE 'CREATE UNIQUE INDEX deal_expected_request_uq '
          'ON pg_temp.marketing_deal_drafts_expected(owner_user_id, request_id)';
  EXECUTE 'CREATE INDEX deal_expected_owner_idx '
          'ON pg_temp.marketing_deal_drafts_expected(owner_user_id, id DESC)';
  expected_oid := to_regclass('pg_temp.marketing_deal_drafts_expected');

  -- The BIGSERIAL id default is bound to each table's own sequence name, so
  -- the id column is verified on type/nullability only (compare_default=FALSE);
  -- every other default is compared exactly after canonicalization.
  SELECT COUNT(*) INTO bad_columns
  FROM (VALUES
    ('id','bigint'::regtype::oid,TRUE,FALSE),
    ('owner_user_id','bigint'::regtype::oid,TRUE,TRUE),
    ('brand_id','bigint'::regtype::oid,FALSE,TRUE),
    ('request_id','text'::regtype::oid,TRUE,TRUE),
    ('form_jsonb','jsonb'::regtype::oid,TRUE,TRUE),
    ('materials_jsonb','jsonb'::regtype::oid,TRUE,TRUE),
    ('sheet_jsonb','jsonb'::regtype::oid,TRUE,TRUE),
    ('status','text'::regtype::oid,TRUE,TRUE),
    ('created_at','timestamp without time zone'::regtype::oid,TRUE,TRUE),
    ('updated_at','timestamp without time zone'::regtype::oid,TRUE,TRUE)
  ) wanted(attname,atttypid,attnotnull,compare_default)
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
     OR (wanted.compare_default
         AND pg_catalog.pg_get_expr(actual_default.adbin,actual_default.adrelid,FALSE)
             IS DISTINCT FROM
             pg_catalog.pg_get_expr(expected_default.adbin,expected_default.adrelid,FALSE))
     OR (NOT wanted.compare_default AND (actual_default.adbin IS NULL) <> (expected_default.adbin IS NULL));
  IF bad_columns <> 0 THEN
    RAISE EXCEPTION 'deal drafts migration found missing/wrong columns or defaults';
  END IF;

  SELECT COUNT(*) INTO bad_constraints
  FROM (VALUES
    ('marketing_deal_drafts_status_ck','deal_expected_status_ck')
  ) wanted(actual_name,expected_name)
  LEFT JOIN pg_catalog.pg_constraint actual
    ON actual.conrelid=target_oid AND actual.conname=wanted.actual_name
  LEFT JOIN pg_catalog.pg_constraint expected
    ON expected.conrelid=expected_oid AND expected.conname=wanted.expected_name
  WHERE actual.oid IS NULL OR actual.contype<>'c' OR NOT actual.convalidated
     OR pg_catalog.pg_get_expr(actual.conbin,actual.conrelid,FALSE)
        IS DISTINCT FROM pg_catalog.pg_get_expr(expected.conbin,expected.conrelid,FALSE);
  IF bad_constraints <> 0 THEN
    RAISE EXCEPTION 'deal drafts migration found missing/weak/invalid CHECK constraints';
  END IF;

  SELECT COUNT(*) INTO bad_indexes
  FROM (VALUES
    ('marketing_deal_drafts_pkey','marketing_deal_drafts_expected_pkey'),
    ('uq_marketing_deal_drafts_request','deal_expected_request_uq'),
    ('idx_marketing_deal_drafts_owner','deal_expected_owner_idx')
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
    RAISE EXCEPTION 'deal drafts migration found missing/wrong/invalid indexes';
  END IF;

  EXECUTE 'DROP TABLE pg_temp.marketing_deal_drafts_expected CASCADE';
END $$;
