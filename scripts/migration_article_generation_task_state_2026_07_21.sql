-- Article generation task projection + append-only revision audit.
-- Additive only: no historical article is rewritten, no feature flag is changed.

ALTER TABLE public.topics ADD COLUMN IF NOT EXISTS generation_request_id VARCHAR(128);
ALTER TABLE public.topics ADD COLUMN IF NOT EXISTS generation_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.topics ADD COLUMN IF NOT EXISTS generation_operation VARCHAR(32);
ALTER TABLE public.topics ADD COLUMN IF NOT EXISTS generation_error_code VARCHAR(80);
ALTER TABLE public.topics ADD COLUMN IF NOT EXISTS generation_error_message TEXT;
ALTER TABLE public.topics ADD COLUMN IF NOT EXISTS generation_retryable BOOLEAN;
ALTER TABLE public.topics ADD COLUMN IF NOT EXISTS generation_failure_phase VARCHAR(32);
ALTER TABLE public.topics ADD COLUMN IF NOT EXISTS generation_refund_status VARCHAR(32);

-- Older bootstrap code used explicit DEFAULT NULL on nullable VARCHAR fields.
-- Normalize that metadata-only equivalent before enforcing the canonical contract.
ALTER TABLE public.topics ALTER COLUMN generation_request_id DROP DEFAULT;
ALTER TABLE public.topics ALTER COLUMN generation_operation DROP DEFAULT;
ALTER TABLE public.topics ALTER COLUMN generation_error_code DROP DEFAULT;
ALTER TABLE public.topics ALTER COLUMN generation_error_message DROP DEFAULT;
ALTER TABLE public.topics ALTER COLUMN generation_retryable DROP DEFAULT;
ALTER TABLE public.topics ALTER COLUMN generation_failure_phase DROP DEFAULT;
ALTER TABLE public.topics ALTER COLUMN generation_refund_status DROP DEFAULT;

DO $article_generation_topic_columns$
DECLARE
    _mismatch TEXT;
BEGIN
    SELECT string_agg(expected.name, ', ' ORDER BY expected.name)
      INTO _mismatch
      FROM (
        VALUES
          ('generation_request_id', 'character varying(128)', FALSE, NULL),
          ('generation_revision', 'integer', TRUE, '0'),
          ('generation_operation', 'character varying(32)', FALSE, NULL),
          ('generation_error_code', 'character varying(80)', FALSE, NULL),
          ('generation_error_message', 'text', FALSE, NULL),
          ('generation_retryable', 'boolean', FALSE, NULL),
          ('generation_failure_phase', 'character varying(32)', FALSE, NULL),
          ('generation_refund_status', 'character varying(32)', FALSE, NULL)
      ) AS expected(name, sql_type, required_not_null, required_default)
      LEFT JOIN pg_attribute attr
        ON attr.attrelid='public.topics'::regclass
       AND attr.attname=expected.name
       AND attr.attnum > 0
       AND NOT attr.attisdropped
      LEFT JOIN pg_attrdef def
        ON def.adrelid=attr.attrelid AND def.adnum=attr.attnum
     WHERE attr.attname IS NULL
        OR format_type(attr.atttypid, attr.atttypmod) <> expected.sql_type
        OR attr.attnotnull <> expected.required_not_null
        OR COALESCE(pg_get_expr(def.adbin, def.adrelid), '<NULL>')
             <> COALESCE(expected.required_default, '<NULL>');
    IF _mismatch IS NOT NULL THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: public.topics columns=%', _mismatch;
    END IF;
END
$article_generation_topic_columns$;

DO $article_generation_revision_nonnegative$
DECLARE
    _constraint_def TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid, TRUE)
      INTO _constraint_def
      FROM pg_constraint
     WHERE conrelid='public.topics'::regclass
       AND conname='topics_generation_revision_nonnegative_ck'
       AND contype='c';
    IF _constraint_def IS NULL THEN
        ALTER TABLE public.topics ADD CONSTRAINT topics_generation_revision_nonnegative_ck
            CHECK (generation_revision >= 0) NOT VALID;
    ELSIF _constraint_def <> 'CHECK (generation_revision >= 0)' THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: topics_generation_revision_nonnegative_ck=%',
            _constraint_def;
    END IF;
    ALTER TABLE public.topics VALIDATE CONSTRAINT topics_generation_revision_nonnegative_ck;
END
$article_generation_revision_nonnegative$;

-- @index-guard idx_topics_generation_request ON topics plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_topics_generation_request' AND i.indrelid = to_regclass('public.topics')) THEN
        NULL;  -- 已在 public.topics 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_topics_generation_request' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_topics_generation_request 已存在但不在 public.topics 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_topics_generation_request' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_topics_generation_request ON public.topics (quote_id,generation_request_id) WHERE generation_request_id IS NOT NULL;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS public.article_generation_revision_events (
    id BIGSERIAL PRIMARY KEY,
    request_id VARCHAR(128) NOT NULL,
    quote_id INTEGER NOT NULL REFERENCES public.quotes(id),
    topic_id INTEGER NOT NULL REFERENCES public.topics(id),
    generation_revision INTEGER NOT NULL,
    event_kind VARCHAR(32) NOT NULL,
    previous_article_id INTEGER REFERENCES public.articles(id),
    previous_title TEXT,
    previous_published BOOLEAN NOT NULL DEFAULT FALSE,
    actor_user_id INTEGER,
    snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT article_generation_revision_positive_ck CHECK (generation_revision > 0),
    CONSTRAINT article_generation_revision_event_kind_ck CHECK (event_kind IN ('full_reset')),
    CONSTRAINT article_generation_revision_request_topic_uq UNIQUE (request_id,topic_id)
);

-- @index-guard idx_article_generation_revision_topic ON article_generation_revision_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_article_generation_revision_topic' AND i.indrelid = to_regclass('public.article_generation_revision_events')) THEN
        NULL;  -- 已在 public.article_generation_revision_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_article_generation_revision_topic' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_article_generation_revision_topic 已存在但不在 public.article_generation_revision_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_article_generation_revision_topic' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_article_generation_revision_topic ON public.article_generation_revision_events (topic_id,generation_revision DESC,id DESC);
    END IF;
END $idxguard$;

DO $article_generation_revision_guard_function$
DECLARE
    _definition TEXT;
BEGIN
    IF to_regprocedure('public.article_generation_revision_events_immutable_guard()') IS NULL THEN
        EXECUTE $create_guard$
            CREATE FUNCTION public.article_generation_revision_events_immutable_guard()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $guard$
            BEGIN
                RAISE EXCEPTION 'article_generation_revision_events is append-only; UPDATE/DELETE forbidden';
            END
            $guard$
        $create_guard$;
    ELSE
        SELECT regexp_replace(proc.prosrc, '\s+', '', 'g')
          INTO _definition
          FROM pg_proc proc
         WHERE proc.oid='public.article_generation_revision_events_immutable_guard()'::regprocedure
           AND proc.prorettype='trigger'::regtype
           AND proc.pronargs=0
           AND NOT proc.prosecdef
           AND proc.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql');
        IF _definition IS DISTINCT FROM
           'BEGINRAISEEXCEPTION''article_generation_revision_eventsisappend-only;UPDATE/DELETEforbidden'';END' THEN
            RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision immutable function';
        END IF;
    END IF;
END
$article_generation_revision_guard_function$;

DO $article_generation_revision_guard_trigger$
DECLARE
    _definition TEXT;
    _enabled "char";
BEGIN
    SELECT regexp_replace(pg_get_triggerdef(trg.oid, TRUE), '\s+', '', 'g'), trg.tgenabled
      INTO _definition, _enabled
      FROM pg_trigger trg
     WHERE trg.tgrelid='public.article_generation_revision_events'::regclass
       AND trg.tgname='trg_article_generation_revision_events_immutable'
       AND NOT trg.tgisinternal;
    IF _definition IS NULL THEN
        CREATE TRIGGER trg_article_generation_revision_events_immutable
        BEFORE UPDATE OR DELETE ON public.article_generation_revision_events
        FOR EACH ROW EXECUTE FUNCTION public.article_generation_revision_events_immutable_guard();
    ELSIF _definition <>
          'CREATETRIGGERtrg_article_generation_revision_events_immutableBEFOREDELETEORUPDATEONarticle_generation_revision_eventsFOREACHROWEXECUTEFUNCTIONarticle_generation_revision_events_immutable_guard()'
       OR _enabled <> 'O' THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision immutable trigger';
    END IF;
END
$article_generation_revision_guard_trigger$;

DO $article_generation_revision_contract$
DECLARE
    _relation_kind "char";
    _persistence "char";
    _mismatch TEXT;
BEGIN
    SELECT relkind, relpersistence
      INTO _relation_kind, _persistence
      FROM pg_class
     WHERE oid='public.article_generation_revision_events'::regclass;
    IF _relation_kind <> 'r' OR _persistence <> 'p' THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision event relation kind=% persistence=%',
            _relation_kind, _persistence;
    END IF;

    SELECT string_agg(expected.name, ', ' ORDER BY expected.name)
      INTO _mismatch
      FROM (
        VALUES
          ('id', 'bigint', TRUE, '<SERIAL>'),
          ('request_id', 'character varying(128)', TRUE, NULL),
          ('quote_id', 'integer', TRUE, NULL),
          ('topic_id', 'integer', TRUE, NULL),
          ('generation_revision', 'integer', TRUE, NULL),
          ('event_kind', 'character varying(32)', TRUE, NULL),
          ('previous_article_id', 'integer', FALSE, NULL),
          ('previous_title', 'text', FALSE, NULL),
          ('previous_published', 'boolean', TRUE, 'false'),
          ('actor_user_id', 'integer', FALSE, NULL),
          ('snapshot', 'jsonb', TRUE, '''{}''::jsonb'),
          ('created_at', 'timestamp with time zone', TRUE, 'now()')
      ) AS expected(name, sql_type, required_not_null, required_default)
      LEFT JOIN pg_attribute attr
        ON attr.attrelid='public.article_generation_revision_events'::regclass
       AND attr.attname=expected.name
       AND attr.attnum > 0
       AND NOT attr.attisdropped
      LEFT JOIN pg_attrdef def
        ON def.adrelid=attr.attrelid AND def.adnum=attr.attnum
     WHERE attr.attname IS NULL
        OR format_type(attr.atttypid, attr.atttypmod) <> expected.sql_type
        OR attr.attnotnull <> expected.required_not_null
        OR (
          expected.required_default <> '<SERIAL>'
          AND COALESCE(pg_get_expr(def.adbin, def.adrelid), '<NULL>')
                <> COALESCE(expected.required_default, '<NULL>')
        );
    IF _mismatch IS NOT NULL THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision event columns=%', _mismatch;
    END IF;
    IF pg_get_serial_sequence('public.article_generation_revision_events','id')
           <> 'public.article_generation_revision_events_id_seq' THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision event id sequence';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='public.article_generation_revision_events'::regclass
           AND conname='article_generation_revision_events_pkey'
           AND contype='p' AND convalidated
           AND conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.article_generation_revision_events'::regclass AND attname='id')]::smallint[]
    ) THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision event primary key';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='public.article_generation_revision_events'::regclass
           AND conname='article_generation_revision_request_topic_uq'
           AND contype='u' AND convalidated
           AND conkey=ARRAY[
             (SELECT attnum FROM pg_attribute WHERE attrelid='public.article_generation_revision_events'::regclass AND attname='request_id'),
             (SELECT attnum FROM pg_attribute WHERE attrelid='public.article_generation_revision_events'::regclass AND attname='topic_id')
           ]::smallint[]
    ) THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision request/topic unique';
    END IF;
    IF (
        SELECT COUNT(*) FROM pg_constraint
         WHERE conrelid='public.article_generation_revision_events'::regclass
           AND contype IN ('p','u')
    ) <> 2 THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: unexpected revision uniqueness';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='public.article_generation_revision_events'::regclass
           AND conname='article_generation_revision_positive_ck'
           AND contype='c' AND convalidated
           AND pg_get_constraintdef(oid, TRUE)='CHECK (generation_revision > 0)'
    ) THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: positive revision check';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='public.article_generation_revision_events'::regclass
           AND conname='article_generation_revision_event_kind_ck'
           AND contype='c' AND convalidated
           AND regexp_replace(pg_get_constraintdef(oid, TRUE), '\s+', '', 'g')
               IN ('CHECK(event_kind::text=''full_reset''::text)', 'CHECK(event_kind=''full_reset''::charactervarying)')
    ) THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision event kind check';
    END IF;
    IF (
        SELECT COUNT(*) FROM pg_constraint
         WHERE conrelid='public.article_generation_revision_events'::regclass
           AND contype='f' AND convalidated AND NOT condeferrable
           AND confdeltype='a' AND confupdtype='a' AND confmatchtype='s'
           AND (
             (conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.article_generation_revision_events'::regclass AND attname='quote_id')]::smallint[] AND confrelid='public.quotes'::regclass AND confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.quotes'::regclass AND attname='id')]::smallint[])
             OR (conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.article_generation_revision_events'::regclass AND attname='topic_id')]::smallint[] AND confrelid='public.topics'::regclass AND confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.topics'::regclass AND attname='id')]::smallint[])
             OR (conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.article_generation_revision_events'::regclass AND attname='previous_article_id')]::smallint[] AND confrelid='public.articles'::regclass AND confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.articles'::regclass AND attname='id')]::smallint[])
           )
    ) <> 3 THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision event foreign keys';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_index idx
          JOIN pg_class index_rel ON index_rel.oid=idx.indexrelid
         WHERE idx.indrelid='public.article_generation_revision_events'::regclass
           AND index_rel.relname='idx_article_generation_revision_topic'
           AND idx.indisvalid AND idx.indisready AND NOT idx.indisunique
           AND idx.indkey::text=concat_ws(' ',
             (SELECT attnum FROM pg_attribute WHERE attrelid=idx.indrelid AND attname='topic_id'),
             (SELECT attnum FROM pg_attribute WHERE attrelid=idx.indrelid AND attname='generation_revision'),
             (SELECT attnum FROM pg_attribute WHERE attrelid=idx.indrelid AND attname='id')
           )
           AND idx.indoption::text='0 3 3'
           AND idx.indpred IS NULL AND idx.indexprs IS NULL
    ) THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision topic index';
    END IF;
END
$article_generation_revision_contract$;

DO $article_generation_revision_guard_contract$
DECLARE
    _definition TEXT;
    _enabled "char";
BEGIN
    SELECT regexp_replace(proc.prosrc, '\s+', '', 'g')
      INTO _definition
      FROM pg_proc proc
     WHERE proc.oid='public.article_generation_revision_events_immutable_guard()'::regprocedure
       AND proc.prorettype='trigger'::regtype
       AND proc.pronargs=0
       AND NOT proc.prosecdef
       AND proc.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql');
    IF _definition IS DISTINCT FROM
       'BEGINRAISEEXCEPTION''article_generation_revision_eventsisappend-only;UPDATE/DELETEforbidden'';END' THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision immutable function';
    END IF;
    SELECT regexp_replace(pg_get_triggerdef(trg.oid, TRUE), '\s+', '', 'g'), trg.tgenabled
      INTO _definition, _enabled
      FROM pg_trigger trg
     WHERE trg.tgrelid='public.article_generation_revision_events'::regclass
       AND trg.tgname='trg_article_generation_revision_events_immutable'
       AND NOT trg.tgisinternal;
    IF _definition IS DISTINCT FROM
          'CREATETRIGGERtrg_article_generation_revision_events_immutableBEFOREDELETEORUPDATEONarticle_generation_revision_eventsFOREACHROWEXECUTEFUNCTIONarticle_generation_revision_events_immutable_guard()'
       OR _enabled IS DISTINCT FROM 'O' THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: revision immutable trigger';
    END IF;
END
$article_generation_revision_guard_contract$;

DO $article_generation_topic_index_contract$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_index idx
          JOIN pg_class index_rel ON index_rel.oid=idx.indexrelid
         WHERE idx.indrelid='public.topics'::regclass
           AND index_rel.relname='idx_topics_generation_request'
           AND idx.indisvalid AND idx.indisready AND NOT idx.indisunique
           AND idx.indkey::text=concat_ws(' ',
             (SELECT attnum FROM pg_attribute WHERE attrelid=idx.indrelid AND attname='quote_id'),
             (SELECT attnum FROM pg_attribute WHERE attrelid=idx.indrelid AND attname='generation_request_id')
           )
           AND idx.indoption::text='0 0'
           AND regexp_replace(pg_get_expr(idx.indpred,idx.indrelid), '\s+', '', 'g')='(generation_request_idISNOTNULL)'
           AND idx.indexprs IS NULL
    ) THEN
        RAISE EXCEPTION 'ARTICLE_GENERATION_SCHEMA_MISMATCH: topics request index';
    END IF;
END
$article_generation_topic_index_contract$;
