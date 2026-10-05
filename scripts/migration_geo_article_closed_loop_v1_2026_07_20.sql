-- GEO article quote-to-publication compatible sidecar v1.
-- Additive only: no historical slot backfill, no commercial-state rewrite, no flag activation.

CREATE TABLE IF NOT EXISTS geo_article_plan_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_key CHARACTER(64) NOT NULL,
    event_kind CHARACTER VARYING(64) NOT NULL,
    source_kind CHARACTER VARYING(80) NOT NULL,
    source_id TEXT NOT NULL,
    source_version CHARACTER VARYING(160) NOT NULL,
    source_snapshot_hash CHARACTER(64) NOT NULL,
    owner_user_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    quote_id INTEGER NOT NULL,
    authority_snapshot JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status CHARACTER VARYING(32) NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at TIMESTAMPTZ,
    claim_token CHARACTER VARYING(80),
    last_error TEXT,
    completed_at TIMESTAMPTZ,
    manual_resolution_status CHARACTER VARYING(32),
    manual_resolution_reason TEXT,
    manual_resolved_by INTEGER,
    manual_resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS event_key CHARACTER(64);
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS event_kind CHARACTER VARYING(64);
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS source_kind CHARACTER VARYING(80);
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS source_id TEXT;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS source_version CHARACTER VARYING(160);
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS source_snapshot_hash CHARACTER(64);
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS owner_user_id INTEGER;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS quote_id INTEGER;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS authority_snapshot JSONB;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS status CHARACTER VARYING(32) DEFAULT 'pending';
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS attempt_count INTEGER DEFAULT 0;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS claim_token CHARACTER VARYING(80);
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS manual_resolution_status CHARACTER VARYING(32);
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS manual_resolution_reason TEXT;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS manual_resolved_by INTEGER;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS manual_resolved_at TIMESTAMPTZ;
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_plan_outbox ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

CREATE TABLE IF NOT EXISTS geo_article_contract_revisions (
    id BIGSERIAL PRIMARY KEY,
    revision_key CHARACTER(64) NOT NULL,
    source_event_key CHARACTER(64) NOT NULL,
    source_version CHARACTER VARYING(160) NOT NULL,
    owner_user_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    quote_id INTEGER NOT NULL,
    authority_snapshot JSONB NOT NULL,
    authority_snapshot_hash CHARACTER(64) NOT NULL,
    delivery_count INTEGER NOT NULL,
    contract_version CHARACTER VARYING(80) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS revision_key CHARACTER(64);
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS source_event_key CHARACTER(64);
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS source_version CHARACTER VARYING(160);
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS owner_user_id INTEGER;
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS quote_id INTEGER;
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS authority_snapshot JSONB;
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS authority_snapshot_hash CHARACTER(64);
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS delivery_count INTEGER;
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS contract_version CHARACTER VARYING(80);
ALTER TABLE geo_article_contract_revisions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

CREATE TABLE IF NOT EXISTS geo_article_plan_runs (
    id BIGSERIAL PRIMARY KEY,
    run_key CHARACTER(64) NOT NULL,
    contract_revision_id BIGINT NOT NULL,
    source_outbox_id BIGINT,
    owner_user_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    quote_id INTEGER NOT NULL,
    run_mode CHARACTER VARYING(24) NOT NULL,
    compiler_version CHARACTER VARYING(80) NOT NULL,
    input_snapshot JSONB NOT NULL,
    input_snapshot_hash CHARACTER(64) NOT NULL,
    output_snapshot JSONB,
    output_snapshot_hash CHARACTER(64),
    comparison_snapshot JSONB,
    status CHARACTER VARYING(24) NOT NULL DEFAULT 'pending',
    verdict CHARACTER VARYING(32) NOT NULL DEFAULT 'NOT_EVALUATED',
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS run_key CHARACTER(64);
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS contract_revision_id BIGINT;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS source_outbox_id BIGINT;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS owner_user_id INTEGER;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS quote_id INTEGER;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS run_mode CHARACTER VARYING(24);
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS compiler_version CHARACTER VARYING(80);
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS input_snapshot JSONB;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS input_snapshot_hash CHARACTER(64);
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS output_snapshot JSONB;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS output_snapshot_hash CHARACTER(64);
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS comparison_snapshot JSONB;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS status CHARACTER VARYING(24) DEFAULT 'pending';
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS verdict CHARACTER VARYING(32) DEFAULT 'NOT_EVALUATED';
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS failure_reason TEXT;
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_plan_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS geo_article_delivery_slot_events (
    id BIGSERIAL PRIMARY KEY,
    event_key CHARACTER(64) NOT NULL,
    delivery_slot_key UUID NOT NULL,
    slot_version INTEGER NOT NULL,
    event_kind CHARACTER VARYING(32) NOT NULL,
    target_state CHARACTER VARYING(24) NOT NULL,
    contract_revision_id BIGINT NOT NULL,
    plan_run_id BIGINT NOT NULL,
    owner_user_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    quote_id INTEGER NOT NULL,
    actor_user_id INTEGER,
    source_version CHARACTER VARYING(160) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS event_key CHARACTER(64);
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS delivery_slot_key UUID;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS slot_version INTEGER;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS event_kind CHARACTER VARYING(32);
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS target_state CHARACTER VARYING(24);
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS contract_revision_id BIGINT;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS plan_run_id BIGINT;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS owner_user_id INTEGER;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS quote_id INTEGER;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS actor_user_id INTEGER;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS source_version CHARACTER VARYING(160);
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS payload JSONB DEFAULT '{}'::JSONB;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;
ALTER TABLE geo_article_delivery_slot_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

CREATE TABLE IF NOT EXISTS geo_article_delivery_slots (
    delivery_slot_key UUID PRIMARY KEY,
    contract_revision_id BIGINT NOT NULL,
    contract_ordinal INTEGER NOT NULL,
    owner_user_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    quote_id INTEGER NOT NULL,
    current_state CHARACTER VARYING(24) NOT NULL,
    projection_version INTEGER NOT NULL,
    current_event_id BIGINT NOT NULL,
    last_event_key CHARACTER(64) NOT NULL,
    keyword_id INTEGER,
    topic_id INTEGER,
    article_id INTEGER,
    superseded_by_slot_key UUID,
    blocked_reason_code CHARACTER VARYING(80),
    blocked_user_message TEXT,
    owner_kind CHARACTER VARYING(40),
    next_action TEXT,
    completion_evidence TEXT,
    blocked_at TIMESTAMPTZ,
    last_reminded_at TIMESTAMPTZ,
    target_resolution_at TIMESTAMPTZ,
    escalation_status CHARACTER VARYING(32),
    resolution_evidence TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS contract_revision_id BIGINT;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS contract_ordinal INTEGER;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS owner_user_id INTEGER;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS quote_id INTEGER;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS current_state CHARACTER VARYING(24);
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS projection_version INTEGER;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS current_event_id BIGINT;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS last_event_key CHARACTER(64);
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS keyword_id INTEGER;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS topic_id INTEGER;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS article_id INTEGER;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS superseded_by_slot_key UUID;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS blocked_reason_code CHARACTER VARYING(80);
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS blocked_user_message TEXT;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS owner_kind CHARACTER VARYING(40);
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS next_action TEXT;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS completion_evidence TEXT;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMPTZ;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS last_reminded_at TIMESTAMPTZ;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS target_resolution_at TIMESTAMPTZ;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS escalation_status CHARACTER VARYING(32);
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS resolution_evidence TEXT;
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE geo_article_delivery_slots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

CREATE TABLE IF NOT EXISTS geo_article_target_question_snapshots (
    id BIGSERIAL PRIMARY KEY,
    question_key CHARACTER(64) NOT NULL,
    tenant_owner_user_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    quote_id INTEGER NOT NULL,
    delivery_slot_key UUID NOT NULL,
    question_source_type CHARACTER VARYING(40) NOT NULL,
    source_object_type CHARACTER VARYING(64),
    source_id BIGINT,
    source_text_snapshot TEXT NOT NULL,
    source_version CHARACTER VARYING(160) NOT NULL,
    source_snapshot_hash CHARACTER(64) NOT NULL,
    parent_source_object_type CHARACTER VARYING(64),
    derived_from_id BIGINT,
    resolver_version CHARACTER VARYING(80) NOT NULL,
    resolution_status CHARACTER VARYING(24) NOT NULL,
    created_by INTEGER NOT NULL,
    override_reason TEXT,
    approval_evidence_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS question_key CHARACTER(64);
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS tenant_owner_user_id INTEGER;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS quote_id INTEGER;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS delivery_slot_key UUID;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS question_source_type CHARACTER VARYING(40);
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS source_object_type CHARACTER VARYING(64);
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS source_id BIGINT;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS source_text_snapshot TEXT;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS source_version CHARACTER VARYING(160);
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS source_snapshot_hash CHARACTER(64);
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS parent_source_object_type CHARACTER VARYING(64);
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS derived_from_id BIGINT;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS resolver_version CHARACTER VARYING(80);
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS resolution_status CHARACTER VARYING(24);
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS created_by INTEGER;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS override_reason TEXT;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS approval_evidence_id TEXT;
ALTER TABLE geo_article_target_question_snapshots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

CREATE TABLE IF NOT EXISTS geo_article_correction_signals (
    id BIGSERIAL PRIMARY KEY,
    correction_key CHARACTER(64) NOT NULL,
    tenant_owner_user_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    quote_id INTEGER NOT NULL,
    delivery_slot_key UUID,
    topic_id INTEGER,
    article_id INTEGER,
    revision_key CHARACTER(64) NOT NULL,
    before_hash CHARACTER(64) NOT NULL,
    after_hash CHARACTER(64) NOT NULL,
    correction_type CHARACTER VARYING(48) NOT NULL,
    reason TEXT NOT NULL,
    actor_user_id INTEGER NOT NULL,
    source_version CHARACTER VARYING(160) NOT NULL,
    candidate_status CHARACTER VARYING(24) NOT NULL DEFAULT 'candidate',
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS correction_key CHARACTER(64);
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS tenant_owner_user_id INTEGER;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS quote_id INTEGER;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS delivery_slot_key UUID;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS topic_id INTEGER;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS article_id INTEGER;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS revision_key CHARACTER(64);
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS before_hash CHARACTER(64);
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS after_hash CHARACTER(64);
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS correction_type CHARACTER VARYING(48);
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS actor_user_id INTEGER;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS source_version CHARACTER VARYING(160);
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS candidate_status CHARACTER VARYING(24) DEFAULT 'candidate';
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;
ALTER TABLE geo_article_correction_signals ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

CREATE TABLE IF NOT EXISTS geo_article_review_shadow_events (
    id BIGSERIAL PRIMARY KEY,
    event_key CHARACTER(64) NOT NULL,
    article_id INTEGER NOT NULL,
    dispatch_source CHARACTER VARYING(80) NOT NULL,
    eligible BOOLEAN NOT NULL,
    reason CHARACTER VARYING(80) NOT NULL,
    canonical_content_hash CHARACTER(64),
    outgoing_content_hash CHARACTER(64) NOT NULL,
    evidence_manifest_hash CHARACTER(64),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS event_key CHARACTER(64);
ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS article_id INTEGER;
ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS dispatch_source CHARACTER VARYING(80);
ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS eligible BOOLEAN;
ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS reason CHARACTER VARYING(80);
ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS canonical_content_hash CHARACTER(64);
ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS outgoing_content_hash CHARACTER(64);
ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS evidence_manifest_hash CHARACTER(64);
ALTER TABLE geo_article_review_shadow_events ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ DEFAULT NOW();

-- Nullable compatibility metadata.  Existing quotes/topics/articles keep NULL and
-- therefore remain on the exact legacy path; no historical backfill is allowed.
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS article_plan_writing_mode CHARACTER VARYING(32);
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS article_plan_enrolled_at TIMESTAMPTZ;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS article_plan_contract_version CHARACTER VARYING(80);
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS article_plan_enrolled_by INTEGER;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS article_plan_enrollment_run_id BIGINT;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS delivery_slot_key UUID;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS plan_run_id BIGINT;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS target_question_snapshot_id BIGINT;
ALTER TABLE topics ADD COLUMN IF NOT EXISTS article_plan_metadata_version CHARACTER VARYING(80);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS delivery_slot_key UUID;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS article_revision_key CHARACTER(64);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS target_question_snapshot_id BIGINT;

-- Older production migrations created these nullable metadata columns with an
-- explicit DEFAULT NULL.  PostgreSQL preserves that expression in pg_attrdef,
-- while the closed-loop contract requires the canonical no-default shape.
-- Dropping the defaults changes no stored value and remains idempotent.
ALTER TABLE quotes ALTER COLUMN article_plan_writing_mode DROP DEFAULT;
ALTER TABLE quotes ALTER COLUMN article_plan_contract_version DROP DEFAULT;
ALTER TABLE topics ALTER COLUMN article_plan_metadata_version DROP DEFAULT;
ALTER TABLE articles ALTER COLUMN article_revision_key DROP DEFAULT;

-- Finish safely repairable half-created tables.  If a table already contains
-- rows without required authority/provenance values these SET NOT NULL commands
-- fail closed; the migration never fabricates business identities to make them
-- pass.  Defaults are technical lifecycle defaults only.
ALTER TABLE geo_article_plan_outbox ALTER COLUMN status SET DEFAULT 'pending';
ALTER TABLE geo_article_plan_outbox ALTER COLUMN attempt_count SET DEFAULT 0;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN available_at SET DEFAULT NOW();
ALTER TABLE geo_article_plan_outbox ALTER COLUMN observed_at SET DEFAULT NOW();
ALTER TABLE geo_article_plan_outbox ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_plan_outbox ALTER COLUMN updated_at SET DEFAULT NOW();
ALTER TABLE geo_article_plan_outbox ALTER COLUMN id SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN event_key SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN event_kind SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN source_kind SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN source_id SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN source_version SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN source_snapshot_hash SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN brand_id SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN quote_id SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN authority_snapshot SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN occurred_at SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN status SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN attempt_count SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN available_at SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE geo_article_plan_outbox ALTER COLUMN updated_at SET NOT NULL;

ALTER TABLE geo_article_contract_revisions ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_contract_revisions ALTER COLUMN id SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN revision_key SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN source_event_key SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN source_version SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN brand_id SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN quote_id SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN authority_snapshot SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN authority_snapshot_hash SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN delivery_count SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN contract_version SET NOT NULL;
ALTER TABLE geo_article_contract_revisions ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE geo_article_plan_runs ALTER COLUMN status SET DEFAULT 'pending';
ALTER TABLE geo_article_plan_runs ALTER COLUMN verdict SET DEFAULT 'NOT_EVALUATED';
ALTER TABLE geo_article_plan_runs ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_plan_runs ALTER COLUMN id SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN run_key SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN contract_revision_id SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN brand_id SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN quote_id SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN run_mode SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN compiler_version SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN input_snapshot SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN input_snapshot_hash SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN status SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN verdict SET NOT NULL;
ALTER TABLE geo_article_plan_runs ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN payload SET DEFAULT '{}'::JSONB;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN id SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN event_key SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN delivery_slot_key SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN slot_version SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN event_kind SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN target_state SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN contract_revision_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN plan_run_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN brand_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN quote_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN source_version SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN payload SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN occurred_at SET NOT NULL;
ALTER TABLE geo_article_delivery_slot_events ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE geo_article_delivery_slots ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_delivery_slots ALTER COLUMN updated_at SET DEFAULT NOW();
ALTER TABLE geo_article_delivery_slots ALTER COLUMN delivery_slot_key SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN contract_revision_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN contract_ordinal SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN owner_user_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN brand_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN quote_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN current_state SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN projection_version SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN current_event_id SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN last_event_key SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE geo_article_delivery_slots ALTER COLUMN updated_at SET NOT NULL;

ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN id SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN question_key SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN tenant_owner_user_id SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN brand_id SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN quote_id SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN delivery_slot_key SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN question_source_type SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN source_text_snapshot SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN source_version SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN source_snapshot_hash SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN resolver_version SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN resolution_status SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN created_by SET NOT NULL;
ALTER TABLE geo_article_target_question_snapshots ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE geo_article_correction_signals ALTER COLUMN candidate_status SET DEFAULT 'candidate';
ALTER TABLE geo_article_correction_signals ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE geo_article_correction_signals ALTER COLUMN id SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN correction_key SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN tenant_owner_user_id SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN brand_id SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN quote_id SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN revision_key SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN before_hash SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN after_hash SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN correction_type SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN reason SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN actor_user_id SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN source_version SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN candidate_status SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN occurred_at SET NOT NULL;
ALTER TABLE geo_article_correction_signals ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE geo_article_review_shadow_events ALTER COLUMN observed_at SET DEFAULT NOW();
ALTER TABLE geo_article_review_shadow_events ALTER COLUMN id SET NOT NULL;
ALTER TABLE geo_article_review_shadow_events ALTER COLUMN event_key SET NOT NULL;
ALTER TABLE geo_article_review_shadow_events ALTER COLUMN article_id SET NOT NULL;
ALTER TABLE geo_article_review_shadow_events ALTER COLUMN dispatch_source SET NOT NULL;
ALTER TABLE geo_article_review_shadow_events ALTER COLUMN eligible SET NOT NULL;
ALTER TABLE geo_article_review_shadow_events ALTER COLUMN reason SET NOT NULL;
ALTER TABLE geo_article_review_shadow_events ALTER COLUMN outgoing_content_hash SET NOT NULL;
ALTER TABLE geo_article_review_shadow_events ALTER COLUMN observed_at SET NOT NULL;

-- Missing constraints are safely additive.  Existing same-name constraints are
-- not trusted: the exact Python readiness contract verifies relation, columns,
-- actions and VALID state and fails closed if any object is weak or misdirected.
DO $closed_loop_constraints$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_article_plan_outbox_pkey' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_outbox ADD CONSTRAINT geo_article_plan_outbox_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_article_contract_revisions_pkey' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_contract_revisions ADD CONSTRAINT geo_article_contract_revisions_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_article_plan_runs_pkey' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_runs ADD CONSTRAINT geo_article_plan_runs_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_article_delivery_slot_events_pkey' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slot_events ADD CONSTRAINT geo_article_delivery_slot_events_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_article_delivery_slots_pkey' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT geo_article_delivery_slots_pkey PRIMARY KEY (delivery_slot_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_article_target_question_snapshots_pkey' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_target_question_snapshots ADD CONSTRAINT geo_article_target_question_snapshots_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_article_correction_signals_pkey' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_correction_signals ADD CONSTRAINT geo_article_correction_signals_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='geo_article_review_shadow_events_pkey' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_review_shadow_events ADD CONSTRAINT geo_article_review_shadow_events_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_plan_outbox_event_key' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_outbox ADD CONSTRAINT uq_geo_article_plan_outbox_event_key UNIQUE (event_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_plan_outbox_kind' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_outbox ADD CONSTRAINT ck_geo_article_plan_outbox_kind CHECK (event_kind IN ('quote_paid_standard','quote_paid_offline','quote_paid_agent_activation','zero_price_writing_project_created','contract_add_on','keyword_reassigned','publication_locked'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_plan_outbox_status' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_outbox ADD CONSTRAINT ck_geo_article_plan_outbox_status CHECK (status IN ('pending','processing','completed','dead_letter'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_plan_outbox_brand' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_outbox ADD CONSTRAINT fk_geo_article_plan_outbox_brand FOREIGN KEY (brand_id) REFERENCES brands(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_plan_outbox_quote' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_outbox ADD CONSTRAINT fk_geo_article_plan_outbox_quote FOREIGN KEY (quote_id) REFERENCES quotes(id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_contract_revision_key' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_contract_revisions ADD CONSTRAINT uq_geo_article_contract_revision_key UNIQUE (revision_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_contract_delivery_count' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_contract_revisions ADD CONSTRAINT ck_geo_article_contract_delivery_count CHECK (delivery_count >= 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_contract_brand' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_contract_revisions ADD CONSTRAINT fk_geo_article_contract_brand FOREIGN KEY (brand_id) REFERENCES brands(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_contract_quote' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_contract_revisions ADD CONSTRAINT fk_geo_article_contract_quote FOREIGN KEY (quote_id) REFERENCES quotes(id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_plan_run_key' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_runs ADD CONSTRAINT uq_geo_article_plan_run_key UNIQUE (run_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_plan_run_revision_mode' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_runs ADD CONSTRAINT uq_geo_article_plan_run_revision_mode UNIQUE (contract_revision_id, compiler_version, run_mode);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_plan_run_mode' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_runs ADD CONSTRAINT ck_geo_article_plan_run_mode CHECK (run_mode IN ('shadow','assisted','canary'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_plan_run_status' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_runs ADD CONSTRAINT ck_geo_article_plan_run_status CHECK (status IN ('pending','completed','failed'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_plan_run_verdict' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_runs ADD CONSTRAINT ck_geo_article_plan_run_verdict CHECK (verdict IN ('PASS','FAIL','INSUFFICIENT_SAMPLES','NOT_EVALUATED'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_plan_run_revision' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_runs ADD CONSTRAINT fk_geo_article_plan_run_revision FOREIGN KEY (contract_revision_id) REFERENCES geo_article_contract_revisions(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_plan_run_outbox' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_plan_runs ADD CONSTRAINT fk_geo_article_plan_run_outbox FOREIGN KEY (source_outbox_id) REFERENCES geo_article_plan_outbox(id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_slot_event_key' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slot_events ADD CONSTRAINT uq_geo_article_slot_event_key UNIQUE (event_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_slot_event_version' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slot_events ADD CONSTRAINT uq_geo_article_slot_event_version UNIQUE (delivery_slot_key, slot_version);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_slot_event_kind' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slot_events ADD CONSTRAINT ck_geo_article_slot_event_kind CHECK (event_kind IN ('created','blocked','unblocked','cancelled','superseded','reassigned','topic_linked','article_linked','publication_locked'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_slot_event_state' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slot_events ADD CONSTRAINT ck_geo_article_slot_event_state CHECK (target_state IN ('active','blocked','cancelled','superseded'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_slot_event_revision' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slot_events ADD CONSTRAINT fk_geo_article_slot_event_revision FOREIGN KEY (contract_revision_id) REFERENCES geo_article_contract_revisions(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_slot_event_run' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slot_events ADD CONSTRAINT fk_geo_article_slot_event_run FOREIGN KEY (plan_run_id) REFERENCES geo_article_plan_runs(id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_slot_revision_ordinal' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT uq_geo_article_slot_revision_ordinal UNIQUE (contract_revision_id, contract_ordinal);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_slot_current_event' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT uq_geo_article_slot_current_event UNIQUE (current_event_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_slot_state' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT ck_geo_article_slot_state CHECK (current_state IN ('active','blocked','cancelled','superseded'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_slot_blocked_fields' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT ck_geo_article_slot_blocked_fields CHECK (current_state <> 'blocked' OR (blocked_reason_code IS NOT NULL AND blocked_user_message IS NOT NULL AND owner_kind IS NOT NULL AND next_action IS NOT NULL AND blocked_at IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_slot_revision' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT fk_geo_article_slot_revision FOREIGN KEY (contract_revision_id) REFERENCES geo_article_contract_revisions(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_slot_current_event' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT fk_geo_article_slot_current_event FOREIGN KEY (current_event_id) REFERENCES geo_article_delivery_slot_events(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_slot_keyword' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT fk_geo_article_slot_keyword FOREIGN KEY (keyword_id) REFERENCES confirmed_keywords(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_slot_topic' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT fk_geo_article_slot_topic FOREIGN KEY (topic_id) REFERENCES topics(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_slot_article' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT fk_geo_article_slot_article FOREIGN KEY (article_id) REFERENCES articles(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_slot_superseded_by' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT fk_geo_article_slot_superseded_by FOREIGN KEY (superseded_by_slot_key) REFERENCES geo_article_delivery_slots(delivery_slot_key);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_target_question_key' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_target_question_snapshots ADD CONSTRAINT uq_geo_article_target_question_key UNIQUE (question_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_question_source_type' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_target_question_snapshots ADD CONSTRAINT ck_geo_article_question_source_type CHECK (question_source_type IN ('purchased_monitoring_exact','confirmed_current_proxy','extra_current_proxy','derived_content_question','operator_override'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_question_resolution' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_target_question_snapshots ADD CONSTRAINT ck_geo_article_question_resolution CHECK (resolution_status IN ('exact','proxy','ambiguous','unresolved'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_question_override' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_target_question_snapshots ADD CONSTRAINT ck_geo_article_question_override CHECK (question_source_type <> 'operator_override' OR (override_reason IS NOT NULL AND approval_evidence_id IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_target_question_slot' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_target_question_snapshots ADD CONSTRAINT fk_geo_article_target_question_slot FOREIGN KEY (delivery_slot_key) REFERENCES geo_article_delivery_slots(delivery_slot_key);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_correction_key' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_correction_signals ADD CONSTRAINT uq_geo_article_correction_key UNIQUE (correction_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_article_correction_candidate' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_correction_signals ADD CONSTRAINT ck_geo_article_correction_candidate CHECK (candidate_status IN ('candidate','reviewed','rejected'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_correction_slot' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_correction_signals ADD CONSTRAINT fk_geo_article_correction_slot FOREIGN KEY (delivery_slot_key) REFERENCES geo_article_delivery_slots(delivery_slot_key);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_geo_article_review_shadow_event_key' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_review_shadow_events ADD CONSTRAINT uq_geo_article_review_shadow_event_key UNIQUE (event_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_article_review_shadow_article' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE geo_article_review_shadow_events ADD CONSTRAINT fk_geo_article_review_shadow_article FOREIGN KEY (article_id) REFERENCES articles(id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_topics_delivery_slot' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE topics ADD CONSTRAINT fk_topics_delivery_slot FOREIGN KEY (delivery_slot_key) REFERENCES geo_article_delivery_slots(delivery_slot_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_topics_plan_run' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE topics ADD CONSTRAINT fk_topics_plan_run FOREIGN KEY (plan_run_id) REFERENCES geo_article_plan_runs(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_topics_target_question' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE topics ADD CONSTRAINT fk_topics_target_question FOREIGN KEY (target_question_snapshot_id) REFERENCES geo_article_target_question_snapshots(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_articles_delivery_slot' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE articles ADD CONSTRAINT fk_articles_delivery_slot FOREIGN KEY (delivery_slot_key) REFERENCES geo_article_delivery_slots(delivery_slot_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_articles_target_question' AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())) THEN
        ALTER TABLE articles ADD CONSTRAINT fk_articles_target_question FOREIGN KEY (target_question_snapshot_id) REFERENCES geo_article_target_question_snapshots(id);
    END IF;
END
$closed_loop_constraints$;

-- @index-guard idx_geo_article_plan_outbox_due ON geo_article_plan_outbox plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_plan_outbox_due' AND i.indrelid = to_regclass('public.geo_article_plan_outbox')) THEN
        NULL;  -- 已在 public.geo_article_plan_outbox 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_plan_outbox_due' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_plan_outbox_due 已存在但不在 public.geo_article_plan_outbox 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_plan_outbox_due' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_plan_outbox_due ON public.geo_article_plan_outbox (status, available_at, id);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_plan_outbox_quote ON geo_article_plan_outbox plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_plan_outbox_quote' AND i.indrelid = to_regclass('public.geo_article_plan_outbox')) THEN
        NULL;  -- 已在 public.geo_article_plan_outbox 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_plan_outbox_quote' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_plan_outbox_quote 已存在但不在 public.geo_article_plan_outbox 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_plan_outbox_quote' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_plan_outbox_quote ON public.geo_article_plan_outbox (quote_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_contract_quote ON geo_article_contract_revisions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_contract_quote' AND i.indrelid = to_regclass('public.geo_article_contract_revisions')) THEN
        NULL;  -- 已在 public.geo_article_contract_revisions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_contract_quote' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_contract_quote 已存在但不在 public.geo_article_contract_revisions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_contract_quote' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_contract_quote ON public.geo_article_contract_revisions (quote_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_plan_runs_quote ON geo_article_plan_runs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_plan_runs_quote' AND i.indrelid = to_regclass('public.geo_article_plan_runs')) THEN
        NULL;  -- 已在 public.geo_article_plan_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_plan_runs_quote' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_plan_runs_quote 已存在但不在 public.geo_article_plan_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_plan_runs_quote' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_plan_runs_quote ON public.geo_article_plan_runs (quote_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_slot_events_slot ON geo_article_delivery_slot_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_slot_events_slot' AND i.indrelid = to_regclass('public.geo_article_delivery_slot_events')) THEN
        NULL;  -- 已在 public.geo_article_delivery_slot_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_slot_events_slot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_slot_events_slot 已存在但不在 public.geo_article_delivery_slot_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_slot_events_slot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_slot_events_slot ON public.geo_article_delivery_slot_events (delivery_slot_key, slot_version DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_slots_quote_state ON geo_article_delivery_slots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_slots_quote_state' AND i.indrelid = to_regclass('public.geo_article_delivery_slots')) THEN
        NULL;  -- 已在 public.geo_article_delivery_slots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_slots_quote_state' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_slots_quote_state 已存在但不在 public.geo_article_delivery_slots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_slots_quote_state' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_slots_quote_state ON public.geo_article_delivery_slots (quote_id, current_state, contract_ordinal);
    END IF;
END $idxguard$;
-- @index-guard uq_geo_article_slots_topic ON geo_article_delivery_slots unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_article_slots_topic' AND i.indrelid = to_regclass('public.geo_article_delivery_slots')) THEN
        NULL;  -- 已在 public.geo_article_delivery_slots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_article_slots_topic' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_article_slots_topic 已存在但不在 public.geo_article_delivery_slots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_article_slots_topic' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_article_slots_topic ON public.geo_article_delivery_slots (topic_id) WHERE topic_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard uq_geo_article_slots_article ON geo_article_delivery_slots unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_article_slots_article' AND i.indrelid = to_regclass('public.geo_article_delivery_slots')) THEN
        NULL;  -- 已在 public.geo_article_delivery_slots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_article_slots_article' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_article_slots_article 已存在但不在 public.geo_article_delivery_slots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_article_slots_article' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_article_slots_article ON public.geo_article_delivery_slots (article_id) WHERE article_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_target_questions_quote ON geo_article_target_question_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_target_questions_quote' AND i.indrelid = to_regclass('public.geo_article_target_question_snapshots')) THEN
        NULL;  -- 已在 public.geo_article_target_question_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_target_questions_quote' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_target_questions_quote 已存在但不在 public.geo_article_target_question_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_target_questions_quote' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_target_questions_quote ON public.geo_article_target_question_snapshots (quote_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_corrections_quote ON geo_article_correction_signals plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_corrections_quote' AND i.indrelid = to_regclass('public.geo_article_correction_signals')) THEN
        NULL;  -- 已在 public.geo_article_correction_signals 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_corrections_quote' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_corrections_quote 已存在但不在 public.geo_article_correction_signals 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_corrections_quote' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_corrections_quote ON public.geo_article_correction_signals (quote_id, occurred_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_review_shadow_article_time ON geo_article_review_shadow_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_review_shadow_article_time' AND i.indrelid = to_regclass('public.geo_article_review_shadow_events')) THEN
        NULL;  -- 已在 public.geo_article_review_shadow_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_review_shadow_article_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_review_shadow_article_time 已存在但不在 public.geo_article_review_shadow_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_review_shadow_article_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_review_shadow_article_time ON public.geo_article_review_shadow_events (article_id, observed_at DESC);
    END IF;
END $idxguard$;
-- @index-guard uq_topics_delivery_slot_key ON topics unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_topics_delivery_slot_key' AND i.indrelid = to_regclass('public.topics')) THEN
        NULL;  -- 已在 public.topics 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_topics_delivery_slot_key' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_topics_delivery_slot_key 已存在但不在 public.topics 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_topics_delivery_slot_key' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_topics_delivery_slot_key ON public.topics (delivery_slot_key) WHERE delivery_slot_key IS NOT NULL;
    END IF;
END $idxguard$;
