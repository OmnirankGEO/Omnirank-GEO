-- [span 级 AI 免费修复 2026-07-30] 端点行为锁用的建表脚本。
--
-- 🔴 列名/类型**逐列取自生产实测**:
--   docker exec omnirank-db pg_dump -U geo_admin -d geo_agentscope --schema-only
--     -t public.articles -t public.quotes -t public.topics
--     -t public.geo_article_review_events --no-owner --no-privileges --no-comments
--   (2026-07-30 · 生产尖 39a7cd6b · PG 16.13)
-- 只做两处删减,均与被测行为无关:
--   1. 删掉指向本脚本范围外表的 FK(brands/users/organizations/diagnosis_records/
--      confirmed_keywords/keyword_clusters/article_generations/
--      geo_article_delivery_slots/geo_article_target_question_snapshots/
--      geo_article_plan_runs/quote_pricing_snapshots) —— 测试不写这些列;
--   2. 删掉与被测 SQL 无关的索引。
-- CHECK 约束 org_*_shape / topics_generation_revision_nonnegative_ck **原样保留**
-- (本项目踩过"CHECK 少一个值 → 生产起不来"的坑,测试库必须带着它们跑)。

CREATE TABLE public.articles (
    id integer NOT NULL,
    topic_id integer NOT NULL,
    quote_id integer,
    title text,
    content text,
    word_count integer DEFAULT 0,
    style text,
    version integer DEFAULT 1,
    reference_article text,
    revision_note text,
    status text DEFAULT 'draft'::text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    first_published_at timestamp without time zone,
    quality_warning jsonb,
    style_code character varying(64) DEFAULT NULL::character varying,
    distilled_version integer,
    distilled_source_hash character varying(64) DEFAULT NULL::character varying,
    distilled_at timestamp without time zone,
    style_family character varying(64),
    style_contract_version character varying(80),
    style_version character varying(80),
    generation_request_id text,
    generation_request_snapshot jsonb,
    prompt_hash character(64),
    evidence_pack jsonb,
    evidence_manifest_hash character(64),
    brand_fact_snapshot jsonb,
    brand_snapshot_hash character(64),
    article_review jsonb,
    article_review_status character varying(40) DEFAULT 'legacy_unreviewed'::character varying,
    publication_profile character varying(64) DEFAULT 'standard'::character varying NOT NULL,
    platform_review jsonb,
    article_human_review_status character varying(40),
    article_human_reviewed_by integer,
    article_human_reviewed_at timestamp with time zone,
    article_human_review_reason text,
    current_content_hash character(64),
    publication_snapshot jsonb,
    publication_snapshot_hash character(64),
    publication_snapshot_at timestamp with time zone,
    publication_snapshot_source character varying(40),
    publication_snapshot_source_id bigint,
    brand_id integer,
    organization_id bigint,
    created_by_user_id integer,
    created_by_membership_id bigint,
    created_by_actor_kind text,
    responsible_user_id integer,
    artifact_visibility text,
    delivery_slot_key uuid,
    article_revision_key character(64),
    target_question_snapshot_id bigint,
    CONSTRAINT org_articles_shape CHECK (((organization_id IS NULL) OR ((created_by_actor_kind = ANY (ARRAY['owner'::text, 'member'::text, 'system'::text])) AND (created_by_user_id IS NOT NULL) AND (responsible_user_id IS NOT NULL) AND (artifact_visibility = ANY (ARRAY['private'::text, 'team'::text, 'external'::text])) AND ((created_by_actor_kind <> 'member'::text) OR (created_by_membership_id IS NOT NULL)))))
);

CREATE SEQUENCE public.articles_id_seq AS integer START WITH 1 INCREMENT BY 1
    NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.articles_id_seq OWNED BY public.articles.id;
ALTER TABLE ONLY public.articles ALTER COLUMN id SET DEFAULT nextval('public.articles_id_seq'::regclass);
ALTER TABLE ONLY public.articles ADD CONSTRAINT articles_pkey PRIMARY KEY (id);

CREATE TABLE public.quotes (
    id integer NOT NULL,
    diagnosis_id integer,
    brand_id integer,
    brand_name text,
    industry text,
    city text,
    tier text,
    target_share real,
    total_keywords integer,
    total_articles integer,
    monthly_price real,
    paid_amount real,
    status text DEFAULT 'draft'::text,
    confirmed_at timestamp without time zone,
    paid_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    markdown text,
    writing_status text DEFAULT 'pending'::text,
    distilled_data text,
    competitor_mode text DEFAULT 'fictional'::text,
    competitor_list text DEFAULT ''::text,
    service_start_date date,
    service_end_date date,
    service_months integer DEFAULT 1,
    service_status text DEFAULT 'pending'::text,
    service_days integer DEFAULT 365,
    monitoring_enabled boolean DEFAULT false,
    monitoring_frequency integer DEFAULT 1,
    monitoring_start_hour integer DEFAULT 8,
    monitoring_started_at timestamp without time zone,
    monitoring_paused_reason character varying(200),
    source_type text DEFAULT 'agent_quote'::text,
    owner_user_id integer,
    deleted_at timestamp without time zone,
    cleanup_reason text,
    last_price_adjusted_at timestamp without time zone,
    monitoring_interval_hours integer DEFAULT 24,
    monitoring_last_run_at timestamp without time zone,
    updated_at timestamp without time zone DEFAULT now(),
    distilled_version integer,
    distilled_source_hash character varying(64) DEFAULT NULL::character varying,
    distilled_at timestamp without time zone,
    organization_id bigint,
    created_by_user_id integer,
    created_by_membership_id bigint,
    created_by_actor_kind text,
    responsible_user_id integer,
    artifact_visibility text,
    article_plan_writing_mode character varying(32),
    article_plan_enrolled_at timestamp with time zone,
    article_plan_contract_version character varying(80),
    article_plan_enrolled_by integer,
    article_plan_enrollment_run_id bigint,
    active_pricing_snapshot_id bigint,
    status_before_archive text,
    writing_status_before_archive text,
    archived_by_user_id integer,
    archive_reason text,
    archived_with_brand_at timestamp with time zone,
    CONSTRAINT org_quotes_shape CHECK (((organization_id IS NULL) OR ((created_by_actor_kind = ANY (ARRAY['owner'::text, 'member'::text, 'system'::text])) AND (created_by_user_id IS NOT NULL) AND (responsible_user_id IS NOT NULL) AND (artifact_visibility = ANY (ARRAY['private'::text, 'team'::text, 'external'::text])) AND ((created_by_actor_kind <> 'member'::text) OR (created_by_membership_id IS NOT NULL)))))
);

CREATE SEQUENCE public.quotes_id_seq AS integer START WITH 1 INCREMENT BY 1
    NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.quotes_id_seq OWNED BY public.quotes.id;
ALTER TABLE ONLY public.quotes ALTER COLUMN id SET DEFAULT nextval('public.quotes_id_seq'::regclass);
ALTER TABLE ONLY public.quotes ADD CONSTRAINT quotes_pkey PRIMARY KEY (id);
CREATE UNIQUE INDEX ux_quotes_id_brand ON public.quotes USING btree (id, brand_id);

CREATE TABLE public.topics (
    id integer NOT NULL,
    keyword_id integer,
    quote_id integer,
    original_keyword text,
    optimized_title text,
    article_style text,
    article_id integer,
    status text DEFAULT 'draft'::text,
    confirmed_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    regenerate_count integer DEFAULT 0,
    completed_at timestamp without time zone,
    reviewed_at timestamp without time zone,
    cluster_id integer,
    is_optimize boolean DEFAULT false,
    fail_reason text,
    user_choice character varying(32) DEFAULT NULL::character varying,
    user_choice_source character varying(32) DEFAULT NULL::character varying,
    is_fixed boolean DEFAULT false,
    style_code character varying(64) DEFAULT NULL::character varying,
    writing_started_at timestamp without time zone,
    legacy_article_generation_id integer,
    delivery_slot_key uuid,
    plan_run_id bigint,
    target_question_snapshot_id bigint,
    article_plan_metadata_version character varying(80),
    generation_request_id character varying(128),
    generation_revision integer DEFAULT 0 NOT NULL,
    generation_operation character varying(32),
    generation_error_code character varying(80),
    generation_error_message text,
    generation_retryable boolean,
    generation_failure_phase character varying(32),
    generation_refund_status character varying(32),
    generation_legal_catalog_version character varying(64),
    CONSTRAINT topics_generation_revision_nonnegative_ck CHECK ((generation_revision >= 0))
);

CREATE SEQUENCE public.topics_id_seq AS integer START WITH 1 INCREMENT BY 1
    NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.topics_id_seq OWNED BY public.topics.id;
ALTER TABLE ONLY public.topics ALTER COLUMN id SET DEFAULT nextval('public.topics_id_seq'::regclass);
ALTER TABLE ONLY public.topics ADD CONSTRAINT topics_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.topics
    ADD CONSTRAINT topics_article_id_articles_fk FOREIGN KEY (article_id) REFERENCES public.articles(id);
ALTER TABLE ONLY public.topics
    ADD CONSTRAINT topics_quote_id_fkey FOREIGN KEY (quote_id) REFERENCES public.quotes(id);

CREATE TABLE public.geo_article_review_events (
    id bigint NOT NULL,
    article_id bigint NOT NULL,
    actor_user_id integer NOT NULL,
    decision character varying(40) NOT NULL,
    reason text NOT NULL,
    machine_review_status character varying(40),
    machine_review_version character varying(100),
    reviewed_content_hash character(64),
    evidence_manifest_hash character(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_geo_article_review_decision CHECK (((decision)::text = ANY ((ARRAY['approved'::character varying, 'rejected'::character varying, 'skipped'::character varying])::text[])))
);

CREATE SEQUENCE public.geo_article_review_events_id_seq START WITH 1 INCREMENT BY 1
    NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE public.geo_article_review_events_id_seq OWNED BY public.geo_article_review_events.id;
ALTER TABLE ONLY public.geo_article_review_events
    ALTER COLUMN id SET DEFAULT nextval('public.geo_article_review_events_id_seq'::regclass);
ALTER TABLE ONLY public.geo_article_review_events
    ADD CONSTRAINT geo_article_review_events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.geo_article_review_events
    ADD CONSTRAINT geo_article_review_events_article_id_fkey FOREIGN KEY (article_id) REFERENCES public.articles(id);
