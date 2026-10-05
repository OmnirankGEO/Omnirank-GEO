-- GEO article full optimization v1.4 (additive only)
-- Deployment contract: pg_dump first; dry-run with BEGIN/ROLLBACK; then apply.
-- This file intentionally contains no historical outcome/content backfill.

BEGIN;

-- Canonical ownership: topics.article_id is the current writing-hall article.
-- Existing FK semantics are authoritative.  Numeric inference is allowed only
-- for a historical database with no FK on article_id; independent sequences
-- are allowed to contain the same integer after canonical/legacy ownership has
-- already been established by an exact validated FK.
ALTER TABLE topics ADD COLUMN IF NOT EXISTS article_id INTEGER;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS legacy_article_generation_id INTEGER;

DO $topics_article_identity$
DECLARE
    fk RECORD;
    article_attnum SMALLINT;
    legacy_attnum SMALLINT;
    articles_id_attnum SMALLINT;
    generations_id_attnum SMALLINT;
    article_fk_total BIGINT;
    article_fk_canonical BIGINT;
    article_fk_legacy BIGINT;
    legacy_fk_total BIGINT;
    legacy_fk_exact BIGINT;
    article_fk_mode TEXT;
    ambiguous_count BIGINT;
    orphan_count BIGINT;
BEGIN
    IF to_regclass('articles') IS NULL OR to_regclass('article_generations') IS NULL THEN
        RAISE EXCEPTION 'GEO_V14_NEEDS_BASE_SCHEMA: articles/article_generations missing';
    END IF;

    SELECT attnum::smallint INTO article_attnum
      FROM pg_attribute
     WHERE attrelid='topics'::regclass AND attname='article_id'
       AND attnum>0 AND NOT attisdropped;
    SELECT attnum::smallint INTO legacy_attnum
      FROM pg_attribute
     WHERE attrelid='topics'::regclass AND attname='legacy_article_generation_id'
       AND attnum>0 AND NOT attisdropped;
    SELECT attnum::smallint INTO articles_id_attnum
      FROM pg_attribute
     WHERE attrelid='articles'::regclass AND attname='id'
       AND attnum>0 AND NOT attisdropped;
    SELECT attnum::smallint INTO generations_id_attnum
      FROM pg_attribute
     WHERE attrelid='article_generations'::regclass AND attname='id'
       AND attnum>0 AND NOT attisdropped;

    SELECT COUNT(*),
           COUNT(*) FILTER (
               WHERE c.conkey=ARRAY[article_attnum]::smallint[]
                 AND c.confrelid='articles'::regclass
                 AND c.confkey=ARRAY[articles_id_attnum]::smallint[]
                 AND c.convalidated
                 AND c.confmatchtype='s' AND c.confupdtype='a' AND c.confdeltype='a'
                 AND NOT c.condeferrable AND NOT c.condeferred
           ),
           COUNT(*) FILTER (
               WHERE c.conkey=ARRAY[article_attnum]::smallint[]
                 AND c.confrelid='article_generations'::regclass
                 AND c.confkey=ARRAY[generations_id_attnum]::smallint[]
                 AND c.convalidated
                 AND c.confmatchtype='s' AND c.confupdtype='a' AND c.confdeltype='a'
                 AND NOT c.condeferrable AND NOT c.condeferred
           )
      INTO article_fk_total, article_fk_canonical, article_fk_legacy
      FROM pg_constraint c
     WHERE c.conrelid='topics'::regclass
       AND c.contype='f'
       AND article_attnum=ANY(c.conkey);

    IF article_fk_total=0 THEN
        article_fk_mode := 'none';
    ELSIF article_fk_total=1 AND article_fk_canonical=1 THEN
        article_fk_mode := 'canonical';
    ELSIF article_fk_total=1 AND article_fk_legacy=1 THEN
        article_fk_mode := 'legacy';
    ELSE
        RAISE EXCEPTION
            'GEO_V14_BAD_TOPICS_FK_SEMANTICS: total=% canonical=% legacy=% (composite/other/both/wrong-action/deferred/unvalidated)',
            article_fk_total, article_fk_canonical, article_fk_legacy;
    END IF;

    IF article_fk_mode='none' THEN
        SELECT COUNT(*) INTO ambiguous_count
          FROM topics t
         WHERE t.article_id IS NOT NULL
           AND EXISTS (SELECT 1 FROM articles a WHERE a.id=t.article_id)
           AND EXISTS (SELECT 1 FROM article_generations g WHERE g.id=t.article_id);
        IF ambiguous_count > 0 THEN
            RAISE EXCEPTION 'GEO_V14_NEEDS_PROD: % topics.article_id values match both articles and article_generations without an authoritative FK', ambiguous_count;
        END IF;

        SELECT COUNT(*) INTO orphan_count
          FROM topics t
         WHERE t.article_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM articles a WHERE a.id=t.article_id)
           AND NOT EXISTS (SELECT 1 FROM article_generations g WHERE g.id=t.article_id);
        IF orphan_count > 0 THEN
            RAISE EXCEPTION 'GEO_V14_NEEDS_PROD: % orphan topics.article_id values without an authoritative FK', orphan_count;
        END IF;
    END IF;

    IF article_fk_mode IN ('none', 'legacy') AND EXISTS (
        SELECT 1
          FROM topics t
         WHERE t.article_id IS NOT NULL
           AND EXISTS (SELECT 1 FROM article_generations g WHERE g.id=t.article_id)
           AND t.legacy_article_generation_id IS NOT NULL
           AND t.legacy_article_generation_id <> t.article_id
    ) THEN
        RAISE EXCEPTION 'GEO_V14_NEEDS_PROD: conflicting legacy_article_generation_id values';
    END IF;

    IF article_fk_mode='legacy' THEN
        UPDATE topics
           SET legacy_article_generation_id=article_id,
               article_id=NULL
         WHERE article_id IS NOT NULL;
    ELSIF article_fk_mode='none' THEN
        UPDATE topics t
           SET legacy_article_generation_id=t.article_id,
               article_id=NULL
         WHERE t.article_id IS NOT NULL
           AND EXISTS (SELECT 1 FROM article_generations g WHERE g.id=t.article_id)
           AND NOT EXISTS (SELECT 1 FROM articles a WHERE a.id=t.article_id);
    END IF;

    -- Only exact authoritative FKs reach this point.  Normalize their names
    -- after the data has been interpreted under the original FK semantics.
    FOR fk IN
        SELECT c.conname
          FROM pg_constraint c
         WHERE c.conrelid='topics'::regclass
           AND c.contype='f'
           AND article_attnum=ANY(c.conkey)
    LOOP
        EXECUTE format('ALTER TABLE topics DROP CONSTRAINT %I', fk.conname);
    END LOOP;

    SELECT COUNT(*),
           COUNT(*) FILTER (
               WHERE c.conkey=ARRAY[legacy_attnum]::smallint[]
                 AND c.confrelid='article_generations'::regclass
                 AND c.confkey=ARRAY[generations_id_attnum]::smallint[]
                 AND c.convalidated
                 AND c.confmatchtype='s' AND c.confupdtype='a' AND c.confdeltype='a'
                 AND NOT c.condeferrable AND NOT c.condeferred
           )
      INTO legacy_fk_total, legacy_fk_exact
      FROM pg_constraint c
     WHERE c.conrelid='topics'::regclass
       AND c.contype='f'
       AND legacy_attnum=ANY(c.conkey);
    IF NOT (legacy_fk_total=0 OR (legacy_fk_total=1 AND legacy_fk_exact=1)) THEN
        RAISE EXCEPTION
            'GEO_V14_BAD_TOPICS_LEGACY_FK_SEMANTICS: total=% exact=%',
            legacy_fk_total, legacy_fk_exact;
    END IF;

    FOR fk IN
        SELECT c.conname
          FROM pg_constraint c
         WHERE c.conrelid='topics'::regclass
           AND c.contype='f'
           AND legacy_attnum=ANY(c.conkey)
    LOOP
        EXECUTE format('ALTER TABLE topics DROP CONSTRAINT %I', fk.conname);
    END LOOP;

    ALTER TABLE topics ADD CONSTRAINT topics_article_id_articles_fk
        FOREIGN KEY (article_id) REFERENCES articles(id);
    ALTER TABLE topics ADD CONSTRAINT topics_legacy_article_generation_fk
        FOREIGN KEY (legacy_article_generation_id) REFERENCES article_generations(id);
END
$topics_article_identity$;

ALTER TABLE articles ADD COLUMN IF NOT EXISTS style_family VARCHAR(64);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS style_contract_version VARCHAR(80);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS style_version VARCHAR(80);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS generation_request_id TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS generation_request_snapshot JSONB;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS prompt_hash CHAR(64);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS evidence_pack JSONB;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS evidence_manifest_hash CHAR(64);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS brand_fact_snapshot JSONB;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS brand_snapshot_hash CHAR(64);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_review JSONB;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_review_status VARCHAR(40) DEFAULT 'legacy_unreviewed';
ALTER TABLE articles ADD COLUMN IF NOT EXISTS publication_profile VARCHAR(64) NOT NULL DEFAULT 'standard';
ALTER TABLE articles ADD COLUMN IF NOT EXISTS platform_review JSONB;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_human_review_status VARCHAR(40);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_human_reviewed_by INTEGER;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_human_reviewed_at TIMESTAMPTZ;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_human_review_reason TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS current_content_hash CHAR(64);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS publication_snapshot JSONB;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS publication_snapshot_hash CHAR(64);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS publication_snapshot_at TIMESTAMPTZ;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS publication_snapshot_source VARCHAR(40);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS publication_snapshot_source_id BIGINT;
ALTER TABLE articles ALTER COLUMN article_review_status SET DEFAULT 'legacy_unreviewed';
UPDATE articles SET publication_profile='standard' WHERE publication_profile IS NULL;
ALTER TABLE articles ALTER COLUMN publication_profile SET DEFAULT 'standard';
ALTER TABLE articles ALTER COLUMN publication_profile SET NOT NULL;

ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS article_content_snapshot TEXT;
ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS article_content_snapshot_hash CHAR(64);
ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS article_content_snapshot_at TIMESTAMPTZ;
ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS article_content_snapshot_source VARCHAR(40);
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_title_snapshot TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_content_snapshot TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_content_snapshot_hash CHAR(64);
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_content_snapshot_at TIMESTAMPTZ;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS submitted_content_snapshot_source VARCHAR(80);

ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS submitted_title_snapshot TEXT;
ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS submitted_content_snapshot TEXT;
ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS submitted_content_snapshot_hash CHAR(64);
ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS submitted_content_snapshot_at TIMESTAMPTZ;
ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS submitted_content_snapshot_source VARCHAR(80);
ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS public_url TEXT;
ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS public_url_reported_explicitly BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE publish_records ADD COLUMN IF NOT EXISTS public_url_report_source VARCHAR(80);
UPDATE publish_records SET public_url_reported_explicitly=FALSE
 WHERE public_url_reported_explicitly IS NULL;
ALTER TABLE publish_records ALTER COLUMN public_url_reported_explicitly SET DEFAULT FALSE;
ALTER TABLE publish_records ALTER COLUMN public_url_reported_explicitly SET NOT NULL;

CREATE TABLE IF NOT EXISTS geo_article_review_events (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT NOT NULL REFERENCES articles(id),
    actor_user_id INTEGER NOT NULL,
    decision VARCHAR(40) NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reason TEXT NOT NULL,
    machine_review_status VARCHAR(40),
    machine_review_version VARCHAR(100),
    reviewed_content_hash CHAR(64),
    evidence_manifest_hash CHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE geo_article_review_events ADD COLUMN IF NOT EXISTS article_id BIGINT;
ALTER TABLE geo_article_review_events ADD COLUMN IF NOT EXISTS actor_user_id INTEGER;
ALTER TABLE geo_article_review_events ADD COLUMN IF NOT EXISTS decision VARCHAR(40);
ALTER TABLE geo_article_review_events ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE geo_article_review_events ADD COLUMN IF NOT EXISTS machine_review_status VARCHAR(40);
ALTER TABLE geo_article_review_events ADD COLUMN IF NOT EXISTS machine_review_version VARCHAR(100);
ALTER TABLE geo_article_review_events ADD COLUMN IF NOT EXISTS reviewed_content_hash CHAR(64);
ALTER TABLE geo_article_review_events ADD COLUMN IF NOT EXISTS evidence_manifest_hash CHAR(64);
ALTER TABLE geo_article_review_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_review_events ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_review_events ALTER COLUMN article_id SET NOT NULL;
ALTER TABLE geo_article_review_events ALTER COLUMN actor_user_id SET NOT NULL;
ALTER TABLE geo_article_review_events ALTER COLUMN decision SET NOT NULL;
ALTER TABLE geo_article_review_events ALTER COLUMN reason SET NOT NULL;
ALTER TABLE geo_article_review_events ALTER COLUMN created_at SET NOT NULL;
DO $review_constraints$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_review_events'::regclass AND conname='fk_geo_article_review_article') THEN
        ALTER TABLE geo_article_review_events ADD CONSTRAINT fk_geo_article_review_article
            FOREIGN KEY (article_id) REFERENCES articles(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_review_events'::regclass AND conname='ck_geo_article_review_decision') THEN
        ALTER TABLE geo_article_review_events ADD CONSTRAINT ck_geo_article_review_decision
            CHECK (decision IN ('approved', 'rejected'));
    END IF;
END
$review_constraints$;
-- @index-guard idx_geo_article_review_events_article ON geo_article_review_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_review_events_article' AND i.indrelid = to_regclass('public.geo_article_review_events')) THEN
        NULL;  -- 已在 public.geo_article_review_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_review_events_article' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_review_events_article 已存在但不在 public.geo_article_review_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_review_events_article' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_review_events_article ON public.geo_article_review_events (article_id, created_at DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS geo_article_gold_labels (
    id BIGSERIAL PRIMARY KEY,
    judge_kind VARCHAR(40) NOT NULL,
    source_id BIGINT NOT NULL,
    machine_label VARCHAR(64) NOT NULL,
    human_label VARCHAR(64) NOT NULL,
    input_snapshot JSONB NOT NULL,
    reviewer_user_id INTEGER NOT NULL,
    rationale TEXT NOT NULL,
    calibration_version VARCHAR(80) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (judge_kind, source_id, reviewer_user_id)
);
ALTER TABLE geo_article_gold_labels ADD COLUMN IF NOT EXISTS judge_kind VARCHAR(40);
ALTER TABLE geo_article_gold_labels ADD COLUMN IF NOT EXISTS source_id BIGINT;
ALTER TABLE geo_article_gold_labels ADD COLUMN IF NOT EXISTS machine_label VARCHAR(64);
ALTER TABLE geo_article_gold_labels ADD COLUMN IF NOT EXISTS human_label VARCHAR(64);
ALTER TABLE geo_article_gold_labels ADD COLUMN IF NOT EXISTS input_snapshot JSONB;
ALTER TABLE geo_article_gold_labels ADD COLUMN IF NOT EXISTS reviewer_user_id INTEGER;
ALTER TABLE geo_article_gold_labels ADD COLUMN IF NOT EXISTS rationale TEXT;
ALTER TABLE geo_article_gold_labels ADD COLUMN IF NOT EXISTS calibration_version VARCHAR(80);
ALTER TABLE geo_article_gold_labels ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_gold_labels ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_gold_labels ALTER COLUMN judge_kind SET NOT NULL;
ALTER TABLE geo_article_gold_labels ALTER COLUMN source_id SET NOT NULL;
ALTER TABLE geo_article_gold_labels ALTER COLUMN machine_label SET NOT NULL;
ALTER TABLE geo_article_gold_labels ALTER COLUMN human_label SET NOT NULL;
ALTER TABLE geo_article_gold_labels ALTER COLUMN input_snapshot SET NOT NULL;
ALTER TABLE geo_article_gold_labels ALTER COLUMN reviewer_user_id SET NOT NULL;
ALTER TABLE geo_article_gold_labels ALTER COLUMN rationale SET NOT NULL;
ALTER TABLE geo_article_gold_labels ALTER COLUMN calibration_version SET NOT NULL;
ALTER TABLE geo_article_gold_labels ALTER COLUMN created_at SET NOT NULL;
-- @index-guard idx_geo_article_gold_labels_kind ON geo_article_gold_labels plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_gold_labels_kind' AND i.indrelid = to_regclass('public.geo_article_gold_labels')) THEN
        NULL;  -- 已在 public.geo_article_gold_labels 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_gold_labels_kind' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_gold_labels_kind 已存在但不在 public.geo_article_gold_labels 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_gold_labels_kind' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_gold_labels_kind ON public.geo_article_gold_labels (judge_kind, calibration_version, source_id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS geo_article_experiments (
    id BIGSERIAL PRIMARY KEY,
    experiment_key VARCHAR(40) NOT NULL UNIQUE,
    contract_version VARCHAR(80) NOT NULL,
    style_family VARCHAR(64) NOT NULL,
    hypothesis TEXT NOT NULL,
    single_change_dimension VARCHAR(80) NOT NULL,
    primary_metric VARCHAR(120) NOT NULL,
    baseline_version_id VARCHAR(200) NOT NULL,
    candidate_version_id VARCHAR(200) NOT NULL,
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    min_arm_articles INTEGER NOT NULL DEFAULT 30,
    minimum_weeks INTEGER NOT NULL DEFAULT 4,
    frozen_config JSONB NOT NULL,
    state VARCHAR(40) NOT NULL DEFAULT 'preregistered',
    created_by INTEGER NOT NULL,
    approved_by INTEGER,
    approval_reason TEXT,
    approved_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    observation_end TIMESTAMPTZ,
    decision_by INTEGER,
    decision_reason TEXT,
    decision_snapshot JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS experiment_key VARCHAR(40);
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS contract_version VARCHAR(80);
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS style_family VARCHAR(64);
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS hypothesis TEXT;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS single_change_dimension VARCHAR(80);
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS primary_metric VARCHAR(120);
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS baseline_version_id VARCHAR(200);
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS candidate_version_id VARCHAR(200);
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS scope JSONB DEFAULT '{}'::jsonb;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS min_arm_articles INTEGER DEFAULT 30;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS minimum_weeks INTEGER DEFAULT 4;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS frozen_config JSONB;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS state VARCHAR(40) DEFAULT 'preregistered';
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS created_by INTEGER;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS approved_by INTEGER;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS approval_reason TEXT;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS observation_end TIMESTAMPTZ;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS decision_by INTEGER;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS decision_reason TEXT;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS decision_snapshot JSONB;
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_experiments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_experiments ALTER COLUMN scope SET DEFAULT '{}'::jsonb;
ALTER TABLE geo_article_experiments ALTER COLUMN min_arm_articles SET DEFAULT 30;
ALTER TABLE geo_article_experiments ALTER COLUMN minimum_weeks SET DEFAULT 4;
ALTER TABLE geo_article_experiments ALTER COLUMN state SET DEFAULT 'preregistered';
ALTER TABLE geo_article_experiments ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_experiments ALTER COLUMN updated_at SET DEFAULT NOW();
ALTER TABLE geo_article_experiments ALTER COLUMN experiment_key SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN contract_version SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN style_family SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN hypothesis SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN single_change_dimension SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN primary_metric SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN baseline_version_id SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN candidate_version_id SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN scope SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN min_arm_articles SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN minimum_weeks SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN frozen_config SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN state SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN created_by SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE geo_article_experiments ALTER COLUMN updated_at SET NOT NULL;
-- @index-guard idx_geo_article_experiments_candidate ON geo_article_experiments plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_experiments_candidate' AND i.indrelid = to_regclass('public.geo_article_experiments')) THEN
        NULL;  -- 已在 public.geo_article_experiments 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_experiments_candidate' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_experiments_candidate 已存在但不在 public.geo_article_experiments 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_experiments_candidate' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_experiments_candidate ON public.geo_article_experiments (candidate_version_id, state);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS geo_article_experiment_assignments (
    id BIGSERIAL PRIMARY KEY,
    experiment_id BIGINT NOT NULL REFERENCES geo_article_experiments(id),
    article_id BIGINT REFERENCES articles(id),
    topic_id BIGINT NOT NULL REFERENCES topics(id),
    generation_request_id TEXT,
    arm VARCHAR(20) NOT NULL CHECK (arm IN ('control','candidate')),
    article_style_version VARCHAR(200) NOT NULL,
    assignment_hash CHAR(64) NOT NULL,
    assigned_by INTEGER NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (experiment_id, topic_id),
    UNIQUE (experiment_id, article_id),
    UNIQUE (generation_request_id)
);
ALTER TABLE geo_article_experiment_assignments ADD COLUMN IF NOT EXISTS experiment_id BIGINT;
ALTER TABLE geo_article_experiment_assignments ADD COLUMN IF NOT EXISTS article_id BIGINT;
ALTER TABLE geo_article_experiment_assignments ADD COLUMN IF NOT EXISTS topic_id BIGINT;
ALTER TABLE geo_article_experiment_assignments ADD COLUMN IF NOT EXISTS generation_request_id TEXT;
ALTER TABLE geo_article_experiment_assignments ADD COLUMN IF NOT EXISTS arm VARCHAR(20);
ALTER TABLE geo_article_experiment_assignments ADD COLUMN IF NOT EXISTS article_style_version VARCHAR(200);
ALTER TABLE geo_article_experiment_assignments ADD COLUMN IF NOT EXISTS assignment_hash CHAR(64);
ALTER TABLE geo_article_experiment_assignments ADD COLUMN IF NOT EXISTS assigned_by INTEGER;
ALTER TABLE geo_article_experiment_assignments ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_experiment_assignments ALTER COLUMN assigned_at SET DEFAULT NOW();
ALTER TABLE geo_article_experiment_assignments ALTER COLUMN experiment_id SET NOT NULL;
ALTER TABLE geo_article_experiment_assignments ALTER COLUMN topic_id SET NOT NULL;
ALTER TABLE geo_article_experiment_assignments ALTER COLUMN arm SET NOT NULL;
ALTER TABLE geo_article_experiment_assignments ALTER COLUMN article_style_version SET NOT NULL;
ALTER TABLE geo_article_experiment_assignments ALTER COLUMN assignment_hash SET NOT NULL;
ALTER TABLE geo_article_experiment_assignments ALTER COLUMN assigned_by SET NOT NULL;
ALTER TABLE geo_article_experiment_assignments ALTER COLUMN assigned_at SET NOT NULL;
DO $assignment_constraints$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_experiment_assignments'::regclass AND conname='fk_geo_article_assignment_experiment') THEN
        ALTER TABLE geo_article_experiment_assignments ADD CONSTRAINT fk_geo_article_assignment_experiment
            FOREIGN KEY (experiment_id) REFERENCES geo_article_experiments(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_experiment_assignments'::regclass AND conname='fk_geo_article_assignment_article') THEN
        ALTER TABLE geo_article_experiment_assignments ADD CONSTRAINT fk_geo_article_assignment_article
            FOREIGN KEY (article_id) REFERENCES articles(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_experiment_assignments'::regclass AND conname='fk_geo_article_assignment_topic') THEN
        ALTER TABLE geo_article_experiment_assignments ADD CONSTRAINT fk_geo_article_assignment_topic
            FOREIGN KEY (topic_id) REFERENCES topics(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_experiment_assignments'::regclass AND conname='ck_geo_article_assignment_arm') THEN
        ALTER TABLE geo_article_experiment_assignments ADD CONSTRAINT ck_geo_article_assignment_arm
            CHECK (arm IN ('control', 'candidate'));
    END IF;
END
$assignment_constraints$;
-- @index-guard idx_geo_article_experiment_assignments_topic ON geo_article_experiment_assignments plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_experiment_assignments_topic' AND i.indrelid = to_regclass('public.geo_article_experiment_assignments')) THEN
        NULL;  -- 已在 public.geo_article_experiment_assignments 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_experiment_assignments_topic' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_experiment_assignments_topic 已存在但不在 public.geo_article_experiment_assignments 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_experiment_assignments_topic' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_experiment_assignments_topic ON public.geo_article_experiment_assignments (topic_id, experiment_id);
    END IF;
END $idxguard$;
-- @index-guard uq_geo_article_experiment_topic ON geo_article_experiment_assignments unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_article_experiment_topic' AND i.indrelid = to_regclass('public.geo_article_experiment_assignments')) THEN
        NULL;  -- 已在 public.geo_article_experiment_assignments 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_article_experiment_topic' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_article_experiment_topic 已存在但不在 public.geo_article_experiment_assignments 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_article_experiment_topic' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_article_experiment_topic ON public.geo_article_experiment_assignments (experiment_id, topic_id) WHERE topic_id IS NOT NULL;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS geo_article_evolution_runs (
    id BIGSERIAL PRIMARY KEY,
    cycle_key VARCHAR(16) NOT NULL UNIQUE,
    cycle_version VARCHAR(80) NOT NULL,
    trigger_source VARCHAR(40) NOT NULL,
    state VARCHAR(40) NOT NULL,
    truth_level VARCHAR(40),
    data_health JSONB NOT NULL,
    corpus_summary JSONB NOT NULL,
    review_summary JSONB NOT NULL,
    experiment_summary JSONB NOT NULL,
    question_summary JSONB NOT NULL,
    cost_summary JSONB NOT NULL,
    recommendations JSONB NOT NULL,
    created_by INTEGER NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    reviewed_by INTEGER,
    reviewed_at TIMESTAMPTZ,
    review_note TEXT
);
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS cycle_key VARCHAR(16);
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS cycle_version VARCHAR(80);
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS trigger_source VARCHAR(40);
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS state VARCHAR(40);
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS truth_level VARCHAR(40);
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS data_health JSONB;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS corpus_summary JSONB;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS review_summary JSONB;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS experiment_summary JSONB;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS question_summary JSONB;
ALTER TABLE geo_article_evolution_runs
    ADD COLUMN IF NOT EXISTS cost_summary JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS recommendations JSONB;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS created_by INTEGER;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS reviewed_by INTEGER;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;
ALTER TABLE geo_article_evolution_runs ADD COLUMN IF NOT EXISTS review_note TEXT;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN cost_summary SET DEFAULT '{}'::jsonb;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN cycle_key SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN cycle_version SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN trigger_source SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN state SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN data_health SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN corpus_summary SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN review_summary SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN experiment_summary SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN question_summary SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN cost_summary SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN recommendations SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN created_by SET NOT NULL;
ALTER TABLE geo_article_evolution_runs ALTER COLUMN started_at SET NOT NULL;
-- @index-guard idx_geo_article_evolution_runs_time ON geo_article_evolution_runs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_evolution_runs_time' AND i.indrelid = to_regclass('public.geo_article_evolution_runs')) THEN
        NULL;  -- 已在 public.geo_article_evolution_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_evolution_runs_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_evolution_runs_time 已存在但不在 public.geo_article_evolution_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_evolution_runs_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_evolution_runs_time ON public.geo_article_evolution_runs (started_at DESC);
    END IF;
END $idxguard$;

ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS sent_question_snapshot TEXT;
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS keyword_source VARCHAR(32) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS keyword_type VARCHAR(32) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS question_family VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS question_family_version VARCHAR(80) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS keyword_source_id BIGINT;
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS keyword_resolver_status VARCHAR(32) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS provider VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS model VARCHAR(128) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS model_revision VARCHAR(128) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS model_revision_unknown_reason VARCHAR(64);
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS surface VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS search_mode VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS response_status VARCHAR(32) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS target_brand_snapshot TEXT;
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS target_entity_snapshot TEXT;
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS target_outcome VARCHAR(40) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS outcome_resolver_version VARCHAR(80) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS outcome_resolver_confidence NUMERIC(5,4);
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS lineage_version VARCHAR(80) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS lineage_status VARCHAR(32) DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS lineage_error_reason TEXT;
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS provider_request_id TEXT;
ALTER TABLE monitoring_results ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;
ALTER TABLE monitoring_results ALTER COLUMN keyword_source SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN keyword_type SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN question_family SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN question_family_version SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN keyword_resolver_status SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN provider SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN model SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN model_revision SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN surface SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN search_mode SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN response_status SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN target_outcome SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN outcome_resolver_version SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN lineage_version SET DEFAULT 'legacy_unknown';
ALTER TABLE monitoring_results ALTER COLUMN lineage_status SET DEFAULT 'legacy_unknown';

ALTER TABLE geo_research_raw ADD COLUMN IF NOT EXISTS provider VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_raw ADD COLUMN IF NOT EXISTS model VARCHAR(128) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_raw ADD COLUMN IF NOT EXISTS model_revision VARCHAR(128) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_raw ADD COLUMN IF NOT EXISTS surface VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_raw ADD COLUMN IF NOT EXISTS search_mode VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_raw ADD COLUMN IF NOT EXISTS prompt_snapshot TEXT;
ALTER TABLE geo_research_raw ALTER COLUMN provider SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_raw ALTER COLUMN model SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_raw ALTER COLUMN model_revision SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_raw ALTER COLUMN surface SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_raw ALTER COLUMN search_mode SET DEFAULT 'legacy_unknown';

ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS family_key VARCHAR(100);
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS parent_prompt_id BIGINT;
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS question_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS query_kind VARCHAR(40) NOT NULL DEFAULT 'research';
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS source_type VARCHAR(40) NOT NULL DEFAULT 'manual';
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS hypothesis TEXT;
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS single_change_dimension TEXT;
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS experiment_group VARCHAR(40) NOT NULL DEFAULT 'shadow';
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS evolution_status VARCHAR(40) NOT NULL DEFAULT 'draft';
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS policy_version VARCHAR(80) NOT NULL DEFAULT 'question-evolution-v1';
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS approved_by TEXT;
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;
ALTER TABLE geo_research_prompts ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;
ALTER TABLE geo_research_prompts ALTER COLUMN question_version SET DEFAULT 1;
ALTER TABLE geo_research_prompts ALTER COLUMN query_kind SET DEFAULT 'research';
ALTER TABLE geo_research_prompts ALTER COLUMN source_type SET DEFAULT 'manual';
ALTER TABLE geo_research_prompts ALTER COLUMN experiment_group SET DEFAULT 'shadow';
ALTER TABLE geo_research_prompts ALTER COLUMN evolution_status SET DEFAULT 'draft';
ALTER TABLE geo_research_prompts ALTER COLUMN policy_version SET DEFAULT 'question-evolution-v1';
ALTER TABLE geo_research_prompts ALTER COLUMN question_version SET NOT NULL;
ALTER TABLE geo_research_prompts ALTER COLUMN query_kind SET NOT NULL;
ALTER TABLE geo_research_prompts ALTER COLUMN source_type SET NOT NULL;
ALTER TABLE geo_research_prompts ALTER COLUMN experiment_group SET NOT NULL;
ALTER TABLE geo_research_prompts ALTER COLUMN evolution_status SET NOT NULL;
ALTER TABLE geo_research_prompts ALTER COLUMN policy_version SET NOT NULL;

CREATE TABLE IF NOT EXISTS geo_question_evolution_events (
    id BIGSERIAL PRIMARY KEY,
    prompt_id BIGINT NOT NULL REFERENCES geo_research_prompts(id),
    actor_user_id INTEGER NOT NULL,
    action VARCHAR(40) NOT NULL,
    from_status VARCHAR(40),
    to_status VARCHAR(40),
    reason TEXT NOT NULL,
    policy_version VARCHAR(80) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE geo_question_evolution_events ADD COLUMN IF NOT EXISTS prompt_id BIGINT;
ALTER TABLE geo_question_evolution_events ADD COLUMN IF NOT EXISTS actor_user_id INTEGER;
ALTER TABLE geo_question_evolution_events ADD COLUMN IF NOT EXISTS action VARCHAR(40);
ALTER TABLE geo_question_evolution_events ADD COLUMN IF NOT EXISTS from_status VARCHAR(40);
ALTER TABLE geo_question_evolution_events ADD COLUMN IF NOT EXISTS to_status VARCHAR(40);
ALTER TABLE geo_question_evolution_events ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE geo_question_evolution_events ADD COLUMN IF NOT EXISTS policy_version VARCHAR(80);
ALTER TABLE geo_question_evolution_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_question_evolution_events ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_question_evolution_events ALTER COLUMN prompt_id SET NOT NULL;
ALTER TABLE geo_question_evolution_events ALTER COLUMN actor_user_id SET NOT NULL;
ALTER TABLE geo_question_evolution_events ALTER COLUMN action SET NOT NULL;
ALTER TABLE geo_question_evolution_events ALTER COLUMN reason SET NOT NULL;
ALTER TABLE geo_question_evolution_events ALTER COLUMN policy_version SET NOT NULL;
ALTER TABLE geo_question_evolution_events ALTER COLUMN created_at SET NOT NULL;
DO $question_event_constraints$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_question_evolution_events'::regclass AND conname='fk_geo_question_event_prompt') THEN
        ALTER TABLE geo_question_evolution_events ADD CONSTRAINT fk_geo_question_event_prompt
            FOREIGN KEY (prompt_id) REFERENCES geo_research_prompts(id);
    END IF;
END
$question_event_constraints$;
-- @index-guard idx_geo_question_evolution_events_prompt ON geo_question_evolution_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_question_evolution_events_prompt' AND i.indrelid = to_regclass('public.geo_question_evolution_events')) THEN
        NULL;  -- 已在 public.geo_question_evolution_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_question_evolution_events_prompt' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_question_evolution_events_prompt 已存在但不在 public.geo_question_evolution_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_question_evolution_events_prompt' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_question_evolution_events_prompt ON public.geo_question_evolution_events (prompt_id, created_at DESC);
    END IF;
END $idxguard$;

ALTER TABLE geo_research_articles ADD COLUMN IF NOT EXISTS corpus_grade VARCHAR(8) DEFAULT 'JC0';
ALTER TABLE geo_research_articles ADD COLUMN IF NOT EXISTS canonical_body_hash CHAR(64);
ALTER TABLE geo_research_articles ADD COLUMN IF NOT EXISTS body_hash_algorithm VARCHAR(80);
ALTER TABLE geo_research_articles ADD COLUMN IF NOT EXISTS content_cluster_id VARCHAR(80);
ALTER TABLE geo_research_articles ADD COLUMN IF NOT EXISTS body_boundary_version VARCHAR(80);
ALTER TABLE geo_research_articles ADD COLUMN IF NOT EXISTS label_provenance_version VARCHAR(80);
ALTER TABLE geo_research_articles ALTER COLUMN corpus_grade SET DEFAULT 'JC0';

CREATE TABLE IF NOT EXISTS geo_research_corpus_label_events (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT NOT NULL REFERENCES geo_research_articles(id),
    from_grade VARCHAR(8) NOT NULL,
    to_grade VARCHAR(8) NOT NULL,
    labeler_version VARCHAR(80) NOT NULL,
    direct_signal_count INTEGER NOT NULL,
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (article_id, labeler_version)
);
ALTER TABLE geo_research_corpus_label_events ADD COLUMN IF NOT EXISTS article_id BIGINT;
ALTER TABLE geo_research_corpus_label_events ADD COLUMN IF NOT EXISTS from_grade VARCHAR(8);
ALTER TABLE geo_research_corpus_label_events ADD COLUMN IF NOT EXISTS to_grade VARCHAR(8);
ALTER TABLE geo_research_corpus_label_events ADD COLUMN IF NOT EXISTS labeler_version VARCHAR(80);
ALTER TABLE geo_research_corpus_label_events ADD COLUMN IF NOT EXISTS direct_signal_count INTEGER;
ALTER TABLE geo_research_corpus_label_events ADD COLUMN IF NOT EXISTS evidence JSONB;
ALTER TABLE geo_research_corpus_label_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_research_corpus_label_events ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_research_corpus_label_events ALTER COLUMN article_id SET NOT NULL;
ALTER TABLE geo_research_corpus_label_events ALTER COLUMN from_grade SET NOT NULL;
ALTER TABLE geo_research_corpus_label_events ALTER COLUMN to_grade SET NOT NULL;
ALTER TABLE geo_research_corpus_label_events ALTER COLUMN labeler_version SET NOT NULL;
ALTER TABLE geo_research_corpus_label_events ALTER COLUMN direct_signal_count SET NOT NULL;
ALTER TABLE geo_research_corpus_label_events ALTER COLUMN evidence SET NOT NULL;
ALTER TABLE geo_research_corpus_label_events ALTER COLUMN created_at SET NOT NULL;
DO $corpus_event_constraints$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_research_corpus_label_events'::regclass AND conname='fk_geo_corpus_event_article') THEN
        ALTER TABLE geo_research_corpus_label_events ADD CONSTRAINT fk_geo_corpus_event_article
            FOREIGN KEY (article_id) REFERENCES geo_research_articles(id);
    END IF;
END
$corpus_event_constraints$;
-- @index-guard idx_geo_research_corpus_label_events_time ON geo_research_corpus_label_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_corpus_label_events_time' AND i.indrelid = to_regclass('public.geo_research_corpus_label_events')) THEN
        NULL;  -- 已在 public.geo_research_corpus_label_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_corpus_label_events_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_corpus_label_events_time 已存在但不在 public.geo_research_corpus_label_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_corpus_label_events_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_corpus_label_events_time ON public.geo_research_corpus_label_events (created_at DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS geo_research_article_fetches (
    id BIGSERIAL PRIMARY KEY,
    fetch_event_key VARCHAR(64) NOT NULL UNIQUE,
    article_id BIGINT REFERENCES geo_research_articles(id) ON DELETE SET NULL,
    source_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    final_url TEXT,
    url_hash CHAR(40),
    parent_fetch_id BIGINT REFERENCES geo_research_article_fetches(id) ON DELETE SET NULL,
    request_profile VARCHAR(80) NOT NULL,
    request_profile_version VARCHAR(80) NOT NULL,
    preset VARCHAR(40), engine VARCHAR(40), cache_policy VARCHAR(40),
    timeout_seconds INTEGER, token_budget INTEGER,
    attempt_number INTEGER NOT NULL,
    response_status VARCHAR(40) NOT NULL,
    http_status INTEGER, warning TEXT, failure_reason VARCHAR(80),
    published_time TIMESTAMPTZ, fetched_at TIMESTAMPTZ NOT NULL,
    latency_ms INTEGER, usage_tokens INTEGER,
    parser_version VARCHAR(80) NOT NULL,
    raw_object_key TEXT, raw_response_hash CHAR(64), raw_body_hash CHAR(64),
    body_object_key TEXT, body_hash CHAR(64),
    robots_policy VARCHAR(40), robots_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS fetch_event_key VARCHAR(64);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS article_id BIGINT;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS normalized_url TEXT;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS final_url TEXT;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS url_hash CHAR(40);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS parent_fetch_id BIGINT;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS request_profile VARCHAR(80);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS request_profile_version VARCHAR(80);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS preset VARCHAR(40);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS engine VARCHAR(40);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS cache_policy VARCHAR(40);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS token_budget INTEGER;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS attempt_number INTEGER;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS response_status VARCHAR(40);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS http_status INTEGER;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS warning TEXT;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS failure_reason VARCHAR(80);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS published_time TIMESTAMPTZ;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS latency_ms INTEGER;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS usage_tokens INTEGER;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS parser_version VARCHAR(80);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS raw_object_key TEXT;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS raw_response_hash CHAR(64);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS raw_body_hash CHAR(64);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS body_object_key TEXT;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS body_hash CHAR(64);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS robots_policy VARCHAR(40);
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS robots_reason TEXT;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;
ALTER TABLE geo_research_article_fetches ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_research_article_fetches ALTER COLUMN metadata SET DEFAULT '{}'::jsonb;
ALTER TABLE geo_research_article_fetches ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_research_article_fetches ALTER COLUMN fetch_event_key SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN source_url SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN normalized_url SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN request_profile SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN request_profile_version SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN attempt_number SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN response_status SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN fetched_at SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN parser_version SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN metadata SET NOT NULL;
ALTER TABLE geo_research_article_fetches ALTER COLUMN created_at SET NOT NULL;
-- @index-guard idx_geo_fetches_article_time ON geo_research_article_fetches plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_fetches_article_time' AND i.indrelid = to_regclass('public.geo_research_article_fetches')) THEN
        NULL;  -- 已在 public.geo_research_article_fetches 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_fetches_article_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_fetches_article_time 已存在但不在 public.geo_research_article_fetches 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_fetches_article_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_fetches_article_time ON public.geo_research_article_fetches (article_id, fetched_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_fetches_url_time ON geo_research_article_fetches plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_fetches_url_time' AND i.indrelid = to_regclass('public.geo_research_article_fetches')) THEN
        NULL;  -- 已在 public.geo_research_article_fetches 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_fetches_url_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_fetches_url_time 已存在但不在 public.geo_research_article_fetches 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_fetches_url_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_fetches_url_time ON public.geo_research_article_fetches (url_hash, fetched_at DESC);
    END IF;
END $idxguard$;

ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS label_provenance_type VARCHAR(40) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS label_provenance_version VARCHAR(80) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS provider VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS model VARCHAR(128) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS model_revision VARCHAR(128) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS surface VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS search_mode VARCHAR(64) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS prompt_snapshot TEXT;
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS article_snapshot_hash CHAR(64);
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS article_fetch_id BIGINT;
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS lineage_status VARCHAR(32) DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ADD COLUMN IF NOT EXISTS lineage_error_reason TEXT;
ALTER TABLE geo_research_source_signals ALTER COLUMN label_provenance_type SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ALTER COLUMN label_provenance_version SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ALTER COLUMN provider SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ALTER COLUMN model SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ALTER COLUMN model_revision SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ALTER COLUMN surface SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ALTER COLUMN search_mode SET DEFAULT 'legacy_unknown';
ALTER TABLE geo_research_source_signals ALTER COLUMN lineage_status SET DEFAULT 'legacy_unknown';

-- Repair the identity/PK portion of otherwise empty or partial v1.4 tables.
-- Existing conflicting id types or duplicate/null data fail loudly here.
DO $v14_identity_contract$
DECLARE
    t TEXT;
    seq_name TEXT;
    pk_name TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'geo_article_review_events', 'geo_article_gold_labels',
        'geo_article_experiments', 'geo_article_experiment_assignments',
        'geo_article_evolution_runs', 'geo_question_evolution_events',
        'geo_research_corpus_label_events', 'geo_research_article_fetches'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_attribute
             WHERE attrelid=t::regclass AND attname='id' AND NOT attisdropped
        ) THEN
            RAISE EXCEPTION 'GEO_V14_PARTIAL_TABLE_MISSING_ID: %', t;
        END IF;
        seq_name := t || '_id_seq';
        EXECUTE format('CREATE SEQUENCE IF NOT EXISTS %I', seq_name);
        EXECUTE format('ALTER SEQUENCE %I OWNED BY %I.id', seq_name, t);
        EXECUTE format('ALTER TABLE %I ALTER COLUMN id SET DEFAULT nextval(%L::regclass)', t, seq_name);
        EXECUTE format('ALTER TABLE %I ALTER COLUMN id SET NOT NULL', t);
        pk_name := t || '_pkey';
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conrelid=t::regclass AND contype='p' AND conname=pk_name
        ) THEN
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=t::regclass AND contype='p') THEN
                RAISE EXCEPTION 'GEO_V14_WRONG_PRIMARY_KEY: %', t;
            END IF;
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I PRIMARY KEY (id)', t, pk_name);
        END IF;
    END LOOP;
END
$v14_identity_contract$;

DO $v14_named_contracts$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_gold_labels'::regclass AND conname='uq_geo_article_gold_label_identity') THEN
        ALTER TABLE geo_article_gold_labels ADD CONSTRAINT uq_geo_article_gold_label_identity
            UNIQUE (judge_kind, source_id, reviewer_user_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_experiments'::regclass AND conname='uq_geo_article_experiment_key') THEN
        ALTER TABLE geo_article_experiments ADD CONSTRAINT uq_geo_article_experiment_key UNIQUE (experiment_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_experiment_assignments'::regclass AND conname='uq_geo_article_assignment_topic') THEN
        ALTER TABLE geo_article_experiment_assignments ADD CONSTRAINT uq_geo_article_assignment_topic UNIQUE (experiment_id, topic_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_experiment_assignments'::regclass AND conname='uq_geo_article_assignment_article') THEN
        ALTER TABLE geo_article_experiment_assignments ADD CONSTRAINT uq_geo_article_assignment_article UNIQUE (experiment_id, article_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_experiment_assignments'::regclass AND conname='uq_geo_article_assignment_request') THEN
        ALTER TABLE geo_article_experiment_assignments ADD CONSTRAINT uq_geo_article_assignment_request UNIQUE (generation_request_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_article_evolution_runs'::regclass AND conname='uq_geo_article_evolution_cycle') THEN
        ALTER TABLE geo_article_evolution_runs ADD CONSTRAINT uq_geo_article_evolution_cycle UNIQUE (cycle_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_research_corpus_label_events'::regclass AND conname='uq_geo_corpus_event_article_version') THEN
        ALTER TABLE geo_research_corpus_label_events ADD CONSTRAINT uq_geo_corpus_event_article_version UNIQUE (article_id, labeler_version);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_research_article_fetches'::regclass AND conname='uq_geo_fetch_event_key') THEN
        ALTER TABLE geo_research_article_fetches ADD CONSTRAINT uq_geo_fetch_event_key UNIQUE (fetch_event_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_research_article_fetches'::regclass AND conname='fk_geo_fetch_article') THEN
        ALTER TABLE geo_research_article_fetches ADD CONSTRAINT fk_geo_fetch_article
            FOREIGN KEY (article_id) REFERENCES geo_research_articles(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='geo_research_article_fetches'::regclass AND conname='fk_geo_fetch_parent') THEN
        ALTER TABLE geo_research_article_fetches ADD CONSTRAINT fk_geo_fetch_parent
            FOREIGN KEY (parent_fetch_id) REFERENCES geo_research_article_fetches(id) ON DELETE SET NULL;
    END IF;
END
$v14_named_contracts$;

-- @index-guard idx_monitoring_lineage_status ON monitoring_results plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_monitoring_lineage_status' AND i.indrelid = to_regclass('public.monitoring_results')) THEN
        NULL;  -- 已在 public.monitoring_results 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_monitoring_lineage_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_monitoring_lineage_status 已存在但不在 public.monitoring_results 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_monitoring_lineage_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_monitoring_lineage_status ON public.monitoring_results (lineage_status, tested_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_monitoring_observation_dims ON monitoring_results plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_monitoring_observation_dims' AND i.indrelid = to_regclass('public.monitoring_results')) THEN
        NULL;  -- 已在 public.monitoring_results 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_monitoring_observation_dims' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_monitoring_observation_dims 已存在但不在 public.monitoring_results 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_monitoring_observation_dims' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_monitoring_observation_dims ON public.monitoring_results (provider, model, surface, sent_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_articles_style_family ON articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_articles_style_family' AND i.indrelid = to_regclass('public.articles')) THEN
        NULL;  -- 已在 public.articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_articles_style_family' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_articles_style_family 已存在但不在 public.articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_articles_style_family' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_articles_style_family ON public.articles (style_family, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_articles_publication_snapshot ON articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_articles_publication_snapshot' AND i.indrelid = to_regclass('public.articles')) THEN
        NULL;  -- 已在 public.articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_articles_publication_snapshot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_articles_publication_snapshot 已存在但不在 public.articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_articles_publication_snapshot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_articles_publication_snapshot ON public.articles (publication_snapshot_at) WHERE publication_snapshot_at IS NOT NULL;
    END IF;
END $idxguard$;

COMMIT;
