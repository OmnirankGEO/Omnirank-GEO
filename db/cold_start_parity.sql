-- 由 scripts/gen_cold_start_parity.py 生成,勿手改(重新生成见该脚本抬头)。
-- 只在空库冷启动时由 scripts/prestart.py 在 manifest 之后执行;生产库一步不走。
-- 缺表 117 张收 84 张(在役读写),缺列 103 个全收,类型差 13 处里对齐 3 列(TYPE_ALIGN,证据见生成器)。

CREATE TABLE public.activity_log (
    id integer NOT NULL,
    user_id integer NOT NULL,
    team_id integer,
    action_type character varying(30) NOT NULL,
    metadata jsonb,
    created_at timestamp without time zone DEFAULT now()
);

CREATE SEQUENCE public.activity_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.activity_log_id_seq OWNED BY public.activity_log.id;

CREATE TABLE public.advisor_conversations (
    id integer NOT NULL,
    conversation_id text NOT NULL,
    advisor_id text NOT NULL,
    title text,
    message_count integer DEFAULT 0,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    owner_user_id integer
);

CREATE SEQUENCE public.advisor_conversations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.advisor_conversations_id_seq OWNED BY public.advisor_conversations.id;

CREATE TABLE public.advisor_messages (
    id integer NOT NULL,
    conversation_id text NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    context text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.advisor_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.advisor_messages_id_seq OWNED BY public.advisor_messages.id;

CREATE TABLE public.agent_channel_tier_state (
    agent_user_id integer NOT NULL,
    channel_tier text DEFAULT 'none'::text NOT NULL,
    rolling_12m_yuan numeric(14,2) DEFAULT 0 NOT NULL,
    is_founder boolean DEFAULT false NOT NULL,
    founder_rank integer,
    first_order_done boolean DEFAULT false NOT NULL,
    last_evaluated_at timestamp without time zone,
    tier_effective_at timestamp without time zone DEFAULT now(),
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    tier_override text,
    tier_override_until timestamp without time zone,
    tier_override_by integer,
    tier_override_note text,
    CONSTRAINT agent_channel_tier_state_channel_tier_check CHECK ((channel_tier = ANY (ARRAY['none'::text, 'certified'::text, 'preferred'::text, 'strategic'::text]))),
    CONSTRAINT agent_channel_tier_state_founder_rank_check CHECK (((founder_rank IS NULL) OR (founder_rank >= 1))),
    CONSTRAINT agent_channel_tier_state_rolling_12m_yuan_check CHECK ((rolling_12m_yuan >= (0)::numeric)),
    CONSTRAINT agent_channel_tier_state_tier_override_check CHECK (((tier_override IS NULL) OR (tier_override = ANY (ARRAY['certified'::text, 'preferred'::text, 'strategic'::text]))))
);

CREATE TABLE public.agent_commission_redemption_items (
    id bigint NOT NULL,
    redemption_request_id bigint NOT NULL,
    ledger_id bigint NOT NULL,
    locked_amount_cents integer NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    CONSTRAINT agent_commission_redemption_items_locked_amount_cents_check CHECK ((locked_amount_cents > 0))
);

CREATE SEQUENCE public.agent_commission_redemption_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.agent_commission_redemption_items_id_seq OWNED BY public.agent_commission_redemption_items.id;

CREATE TABLE public.agent_commission_redemption_requests (
    id bigint NOT NULL,
    agent_user_id integer NOT NULL,
    redeem_cents integer NOT NULL,
    inventory_points_granted integer NOT NULL,
    wholesale_numer integer NOT NULL,
    wholesale_denom integer NOT NULL,
    status text DEFAULT 'redeemed'::text NOT NULL,
    note text,
    created_at timestamp without time zone DEFAULT now(),
    CONSTRAINT agent_commission_redemption_requ_inventory_points_granted_check CHECK ((inventory_points_granted >= 0)),
    CONSTRAINT agent_commission_redemption_requests_redeem_cents_check CHECK ((redeem_cents > 0)),
    CONSTRAINT agent_commission_redemption_requests_status_check CHECK ((status = ANY (ARRAY['redeemed'::text, 'reversed'::text])))
);

CREATE SEQUENCE public.agent_commission_redemption_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.agent_commission_redemption_requests_id_seq OWNED BY public.agent_commission_redemption_requests.id;

CREATE TABLE public.agent_rebate_config (
    agent_user_id integer NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    rebate_rate numeric(5,4) DEFAULT 0 NOT NULL,
    max_rebate_points_per_order integer,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_max_rebate_nonneg CHECK (((max_rebate_points_per_order IS NULL) OR (max_rebate_points_per_order >= 0))),
    CONSTRAINT chk_rebate_rate_range CHECK (((rebate_rate >= (0)::numeric) AND (rebate_rate <= 1.0)))
);

CREATE TABLE public.agent_sessions (
    id character varying(32) NOT NULL,
    user_id integer NOT NULL,
    profile_id character varying(22),
    title character varying(200) DEFAULT '新对话'::character varying,
    messages jsonb DEFAULT '[]'::jsonb,
    task_summaries jsonb DEFAULT '[]'::jsonb,
    agent_type character varying(20) DEFAULT 'social'::character varying,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);

CREATE TABLE public.agent_tier_change_log (
    id bigint NOT NULL,
    agent_user_id integer NOT NULL,
    from_tier text,
    to_tier text NOT NULL,
    direction text NOT NULL,
    rolling_12m_yuan numeric(14,2) DEFAULT 0 NOT NULL,
    trigger_source text DEFAULT 'cron'::text NOT NULL,
    related_order_id text,
    idempotency_key text NOT NULL,
    note text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_tier_change_log_direction_check CHECK ((direction = ANY (ARRAY['upgrade'::text, 'downgrade'::text, 'init'::text]))),
    CONSTRAINT agent_tier_change_log_from_tier_check CHECK (((from_tier IS NULL) OR (from_tier = ANY (ARRAY['none'::text, 'certified'::text, 'preferred'::text, 'strategic'::text])))),
    CONSTRAINT agent_tier_change_log_to_tier_check CHECK ((to_tier = ANY (ARRAY['none'::text, 'certified'::text, 'preferred'::text, 'strategic'::text]))),
    CONSTRAINT agent_tier_change_log_trigger_source_check CHECK ((trigger_source = ANY (ARRAY['cron'::text, 'purchase_event'::text, 'admin_manual'::text])))
);

CREATE SEQUENCE public.agent_tier_change_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.agent_tier_change_log_id_seq OWNED BY public.agent_tier_change_log.id;

CREATE TABLE public.ai_suggestions (
    id integer NOT NULL,
    target_type character varying(20) NOT NULL,
    target_id character varying(50) NOT NULL,
    suggestion_type character varying(50) NOT NULL,
    trigger_type character varying(20),
    title character varying(300),
    content jsonb NOT NULL,
    data_sources jsonb,
    is_read boolean DEFAULT false,
    is_actionable boolean DEFAULT true,
    action_taken boolean DEFAULT false,
    created_at timestamp without time zone DEFAULT now(),
    expires_at timestamp without time zone
);

CREATE SEQUENCE public.ai_suggestions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.ai_suggestions_id_seq OWNED BY public.ai_suggestions.id;

CREATE TABLE public.bonus_clawback_pending (
    id bigint NOT NULL,
    user_id integer NOT NULL,
    recharge_order_id text NOT NULL,
    amount_due bigint NOT NULL,
    amount_settled bigint DEFAULT 0,
    status character varying(20) DEFAULT 'pending'::character varying,
    reason character varying(100),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    settled_at timestamp without time zone
);

CREATE SEQUENCE public.bonus_clawback_pending_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.bonus_clawback_pending_id_seq OWNED BY public.bonus_clawback_pending.id;

CREATE TABLE public.bonus_grants (
    id bigint NOT NULL,
    grant_key text NOT NULL,
    owner_type text NOT NULL,
    owner_id integer NOT NULL,
    pool text DEFAULT 'bonus'::text NOT NULL,
    granted_points bigint NOT NULL,
    consumed_points bigint DEFAULT 0 NOT NULL,
    frozen_points bigint DEFAULT 0 NOT NULL,
    grant_type text NOT NULL,
    tier_at_grant text,
    bonus_rate_used numeric(5,4),
    related_order_id text,
    parent_grant_id bigint,
    status text DEFAULT 'active'::text NOT NULL,
    granted_at timestamp without time zone DEFAULT now() NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    frozen_at timestamp without time zone,
    renewed_at timestamp without time zone,
    note text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT bonus_grants_check CHECK (((consumed_points + frozen_points) <= granted_points)),
    CONSTRAINT bonus_grants_consumed_points_check CHECK ((consumed_points >= 0)),
    CONSTRAINT bonus_grants_frozen_points_check CHECK ((frozen_points >= 0)),
    CONSTRAINT bonus_grants_grant_type_check CHECK ((grant_type = ANY (ARRAY['tier_purchase'::text, 'founder_first_order'::text, 'allocate_from_grant'::text, 'customer_order_bonus'::text, 'admin_adjust'::text]))),
    CONSTRAINT bonus_grants_granted_points_check CHECK ((granted_points > 0)),
    CONSTRAINT bonus_grants_owner_type_check CHECK ((owner_type = ANY (ARRAY['agent'::text, 'customer'::text]))),
    CONSTRAINT bonus_grants_pool_check CHECK ((pool = 'bonus'::text)),
    CONSTRAINT bonus_grants_status_check CHECK ((status = ANY (ARRAY['active'::text, 'expired_frozen'::text, 'renewed'::text, 'fully_consumed'::text])))
);

CREATE SEQUENCE public.bonus_grants_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.bonus_grants_id_seq OWNED BY public.bonus_grants.id;

CREATE TABLE public.brand_aliases (
    id integer NOT NULL,
    canonical_name text NOT NULL,
    alias text NOT NULL,
    brand_id integer,
    source text DEFAULT 'manual'::text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT brand_aliases_source_check CHECK ((source = ANY (ARRAY['manual'::text, 'auto_discovered'::text, 'llm_suggested'::text])))
);

CREATE SEQUENCE public.brand_aliases_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.brand_aliases_id_seq OWNED BY public.brand_aliases.id;

CREATE TABLE public.brand_merge_map (
    old_brand_id integer NOT NULL,
    new_brand_id integer NOT NULL,
    merged_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE public.content_plans (
    id character varying(22) NOT NULL,
    profile_id character varying(22) NOT NULL,
    month character varying(7) NOT NULL,
    mode character varying(20) DEFAULT 'inspiration'::character varying NOT NULL,
    stage character varying(20),
    topic_mix jsonb,
    weekly_themes jsonb,
    milestones jsonb,
    monthly_summary text,
    encouragement text,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    is_deleted integer DEFAULT 0,
    brand_id integer,
    user_id integer,
    team_id integer,
    source_project_id integer,
    monthly_goal character varying(20) DEFAULT 'growth'::character varying,
    strategy_pack jsonb
);

CREATE TABLE public.customer_credit_freezes (
    id bigint NOT NULL,
    customer_user_id integer NOT NULL,
    agent_user_id integer NOT NULL,
    feature_code text NOT NULL,
    amount_total integer NOT NULL,
    amount_tool integer DEFAULT 0 NOT NULL,
    amount_publish integer DEFAULT 0 NOT NULL,
    amount_bonus integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'frozen'::text NOT NULL,
    task_ref text,
    brand_id integer,
    reason text,
    created_at timestamp without time zone DEFAULT now(),
    committed_at timestamp without time zone,
    released_at timestamp without time zone,
    CONSTRAINT chk_ccf_pool_sum CHECK ((((amount_tool + amount_publish) + amount_bonus) = amount_total)),
    CONSTRAINT customer_credit_freezes_amount_total_check CHECK ((amount_total > 0)),
    CONSTRAINT customer_credit_freezes_status_check CHECK ((status = ANY (ARRAY['frozen'::text, 'committed'::text, 'released'::text])))
);

CREATE SEQUENCE public.customer_credit_freezes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.customer_credit_freezes_id_seq OWNED BY public.customer_credit_freezes.id;

CREATE TABLE public.domain_authority_cache (
    domain text NOT NULL,
    tier text NOT NULL,
    reason text,
    source text DEFAULT 'ai'::text,
    updated_at timestamp without time zone DEFAULT now()
);

CREATE TABLE public.draft_workspace (
    id integer NOT NULL,
    user_id integer NOT NULL,
    brand_id integer NOT NULL,
    item_type character varying(30) NOT NULL,
    item_key character varying(200) NOT NULL,
    item_data jsonb DEFAULT '{}'::jsonb,
    source character varying(20) DEFAULT 'ai_chat'::character varying,
    created_at timestamp without time zone DEFAULT now(),
    expires_at timestamp without time zone
);

CREATE SEQUENCE public.draft_workspace_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.draft_workspace_id_seq OWNED BY public.draft_workspace.id;

CREATE TABLE public.employee_capabilities (
    id integer NOT NULL,
    employee_id text NOT NULL,
    capability_type text NOT NULL,
    target_id text NOT NULL,
    target_name text,
    enabled smallint DEFAULT 1,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.employee_capabilities_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.employee_capabilities_id_seq OWNED BY public.employee_capabilities.id;

CREATE TABLE public.failed_service_fee_jobs (
    id bigint NOT NULL,
    source_order_id text NOT NULL,
    user_id integer NOT NULL,
    amount_cents integer NOT NULL,
    order_extras jsonb,
    error_message text,
    error_class character varying(100),
    retry_count integer DEFAULT 0,
    max_retries integer DEFAULT 3,
    status character varying(20) DEFAULT 'pending'::character varying,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    last_retry_at timestamp without time zone,
    succeeded_at timestamp without time zone
);

CREATE SEQUENCE public.failed_service_fee_jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.failed_service_fee_jobs_id_seq OWNED BY public.failed_service_fee_jobs.id;

CREATE TABLE public.flywheel_judgment_log (
    id bigint NOT NULL,
    point_key character varying(60) NOT NULL,
    source character varying(12) NOT NULL,
    provider character varying(40),
    model character varying(120),
    prompt_version character varying(40),
    input_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    output jsonb DEFAULT '{}'::jsonb NOT NULL,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    cost_cny numeric(12,6) DEFAULT 0 NOT NULL,
    latency_ms integer DEFAULT 0 NOT NULL,
    fallback_reason text,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.flywheel_judgment_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.flywheel_judgment_log_id_seq OWNED BY public.flywheel_judgment_log.id;

CREATE TABLE public.founder_seats (
    id integer DEFAULT 1 NOT NULL,
    used integer DEFAULT 0 NOT NULL,
    cap integer DEFAULT 10 NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT founder_seats_cap_check CHECK ((cap >= 0)),
    CONSTRAINT founder_seats_check CHECK ((used <= cap)),
    CONSTRAINT founder_seats_id_check CHECK ((id = 1)),
    CONSTRAINT founder_seats_used_check CHECK ((used >= 0))
);

CREATE TABLE public.geo_answer_adoption_metrics (
    id bigint NOT NULL,
    source_url text NOT NULL,
    domain character varying(300),
    industry_key character varying(100) DEFAULT 'general'::character varying NOT NULL,
    engine character varying(40) DEFAULT ''::character varying NOT NULL,
    prompt_id text DEFAULT ''::text NOT NULL,
    round_id character varying(80) DEFAULT ''::character varying NOT NULL,
    signal_tier character varying(40) NOT NULL,
    answer_adopted boolean DEFAULT false NOT NULL,
    explicit_cited boolean DEFAULT false NOT NULL,
    search_exposed boolean DEFAULT false NOT NULL,
    reference_only boolean DEFAULT false NOT NULL,
    rejected_noise boolean DEFAULT false NOT NULL,
    source_position integer DEFAULT 0,
    total_sources_in_answer integer DEFAULT 1,
    normalized_credit numeric(12,6) DEFAULT 0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    observed_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE SEQUENCE public.geo_answer_adoption_metrics_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.geo_answer_adoption_metrics_id_seq OWNED BY public.geo_answer_adoption_metrics.id;

CREATE TABLE public.geo_engine_weight_candidates (
    id bigint NOT NULL,
    industry text NOT NULL,
    engine text NOT NULL,
    current_weight real,
    suggested_weight real NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(20) DEFAULT 'candidate'::character varying NOT NULL,
    reviewed_by bigint,
    review_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_at timestamp with time zone
);

CREATE SEQUENCE public.geo_engine_weight_candidates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.geo_engine_weight_candidates_id_seq OWNED BY public.geo_engine_weight_candidates.id;

CREATE TABLE public.geo_query_intent (
    id bigint NOT NULL,
    query_hash character(32) NOT NULL,
    industry text DEFAULT ''::text NOT NULL,
    query text DEFAULT ''::text NOT NULL,
    query_intent character varying(20) NOT NULL,
    confidence numeric(4,3) DEFAULT 0 NOT NULL,
    reason text,
    model character varying(60),
    classified_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_geo_query_intent_label CHECK (((query_intent)::text = ANY (ARRAY[('ranking'::character varying)::text, ('tutorial'::character varying)::text, ('long_form'::character varying)::text, ('comparison'::character varying)::text, ('data_report'::character varying)::text, ('policy'::character varying)::text, ('definition'::character varying)::text, ('faq'::character varying)::text])))
);

CREATE SEQUENCE public.geo_query_intent_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.geo_query_intent_id_seq OWNED BY public.geo_query_intent.id;

CREATE TABLE public.geo_research_answer_entities (
    id bigint NOT NULL,
    answer_fact_id bigint NOT NULL,
    raw_id integer DEFAULT 0 NOT NULL,
    industry_key character varying(100) DEFAULT 'general'::character varying NOT NULL,
    engine character varying(40) DEFAULT ''::character varying NOT NULL,
    entity_name text NOT NULL,
    entity_key character varying(80) NOT NULL,
    entity_type character varying(20) DEFAULT 'brand'::character varying NOT NULL,
    recommendation_rank integer,
    mention_rank integer,
    recommendation_reasons jsonb DEFAULT '[]'::jsonb NOT NULL,
    evidence_phrases jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_urls jsonb DEFAULT '[]'::jsonb NOT NULL,
    confidence numeric(5,3) DEFAULT 0 NOT NULL,
    llm_model character varying(80) DEFAULT ''::character varying NOT NULL,
    extractor_version character varying(40) DEFAULT ''::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE SEQUENCE public.geo_research_answer_entities_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.geo_research_answer_entities_id_seq OWNED BY public.geo_research_answer_entities.id;

CREATE TABLE public.geo_research_answer_facts (
    id bigint NOT NULL,
    raw_id integer NOT NULL,
    industry text DEFAULT ''::text NOT NULL,
    industry_key character varying(100) DEFAULT 'general'::character varying NOT NULL,
    query text DEFAULT ''::text NOT NULL,
    engine character varying(40) DEFAULT ''::character varying NOT NULL,
    batch_id character varying(120) DEFAULT ''::character varying NOT NULL,
    answer_hash character(32) DEFAULT ''::bpchar NOT NULL,
    raw_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    citation_urls jsonb DEFAULT '[]'::jsonb NOT NULL,
    answer_excerpt text,
    entity_count integer DEFAULT 0 NOT NULL,
    quality_flag character varying(20) DEFAULT ''::character varying NOT NULL,
    extractor_version character varying(40) DEFAULT ''::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE SEQUENCE public.geo_research_answer_facts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.geo_research_answer_facts_id_seq OWNED BY public.geo_research_answer_facts.id;

CREATE TABLE public.imitated_articles (
    id integer NOT NULL,
    imitation_record_id integer NOT NULL,
    reference_id integer NOT NULL,
    quote_id integer NOT NULL,
    keyword text,
    title text NOT NULL,
    content text NOT NULL,
    word_count integer,
    status text DEFAULT 'draft'::text,
    revision_notes text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.imitated_articles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.imitated_articles_id_seq OWNED BY public.imitated_articles.id;

CREATE TABLE public.imitation_records (
    id integer NOT NULL,
    reference_id integer,
    quote_id integer,
    generated_count integer,
    success_count integer DEFAULT 0,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.imitation_records_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.imitation_records_id_seq OWNED BY public.imitation_records.id;

CREATE TABLE public.industry_brief_history (
    id integer NOT NULL,
    profile_id character varying(64) NOT NULL,
    version_num integer NOT NULL,
    brief_data jsonb NOT NULL,
    confirmed boolean DEFAULT false,
    partial_fields jsonb,
    source character varying(20) NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    created_by integer
);

CREATE SEQUENCE public.industry_brief_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.industry_brief_history_id_seq OWNED BY public.industry_brief_history.id;

CREATE TABLE public.industry_knowledge (
    id integer NOT NULL,
    level character varying(10) NOT NULL,
    industry character varying(100) NOT NULL,
    category character varying(100),
    knowledge jsonb NOT NULL,
    source character varying(50) DEFAULT 'auto'::character varying,
    version integer DEFAULT 1,
    generated_at timestamp without time zone DEFAULT now(),
    expires_at timestamp without time zone,
    search_count integer DEFAULT 0,
    correction_count integer DEFAULT 0
);

CREATE SEQUENCE public.industry_knowledge_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.industry_knowledge_id_seq OWNED BY public.industry_knowledge.id;

CREATE TABLE public.interview_sessions (
    session_id text NOT NULL,
    user_id integer NOT NULL,
    state_json text NOT NULL,
    is_complete boolean DEFAULT false,
    current_topic integer DEFAULT 1,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);

CREATE TABLE public.invite_codes (
    id bigint NOT NULL,
    user_id integer NOT NULL,
    code character varying(40) NOT NULL,
    code_type character varying(20) DEFAULT 'user'::character varying NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    revoked_at timestamp without time zone,
    revoked_reason character varying(100),
    used_by_user_id integer,
    used_at timestamp without time zone,
    note text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.invite_codes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.invite_codes_id_seq OWNED BY public.invite_codes.id;

CREATE TABLE public.keyword_insights (
    id integer NOT NULL,
    task_id integer NOT NULL,
    brand_id integer,
    client_id text,
    keyword text NOT NULL,
    platforms_analyzed text DEFAULT '[]'::text,
    brands_found text DEFAULT '[]'::text,
    sources_cited text DEFAULT '[]'::text,
    response_patterns text DEFAULT '{}'::text,
    client_position text DEFAULT '{}'::text,
    optimization_hints text DEFAULT '[]'::text,
    llm_model text DEFAULT 'qwen3-max'::text,
    llm_tokens integer DEFAULT 0,
    algorithm_version text DEFAULT 'v4.2'::text,
    quality_flag text DEFAULT 'auto'::text,
    feedback_note text,
    distilled_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT keyword_insights_quality_flag_check CHECK ((quality_flag = ANY (ARRAY['auto'::text, 'verified'::text, 'rejected'::text, 'degraded'::text])))
);

CREATE SEQUENCE public.keyword_insights_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.keyword_insights_id_seq OWNED BY public.keyword_insights.id;

CREATE TABLE public.m3_customer_events (
    id bigint NOT NULL,
    brand_id integer,
    quote_id integer,
    diagnosis_id integer,
    token_hash text,
    source character varying(32) NOT NULL,
    event_type character varying(48) NOT NULL,
    event_key text,
    metadata jsonb DEFAULT '{}'::jsonb,
    ip_hash text,
    user_agent_hash text,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.m3_customer_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.m3_customer_events_id_seq OWNED BY public.m3_customer_events.id;

CREATE TABLE public.marketing_confirm_sessions (
    id integer NOT NULL,
    token text NOT NULL,
    brand_id integer NOT NULL,
    status text DEFAULT 'pending'::text,
    materials_snapshot text,
    customer_notes text DEFAULT ''::text,
    confirmed_at timestamp without time zone,
    expires_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    customer_patch_json text DEFAULT ''::text
);

CREATE SEQUENCE public.marketing_confirm_sessions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.marketing_confirm_sessions_id_seq OWNED BY public.marketing_confirm_sessions.id;

CREATE TABLE public.meeting_presets (
    id integer NOT NULL,
    name text NOT NULL,
    style text,
    rounds integer DEFAULT 2,
    co_moderator_id text,
    co_moderator_name text,
    weight_boost real DEFAULT 1.5,
    auto_execute smallint DEFAULT 0,
    description text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.meeting_presets_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.meeting_presets_id_seq OWNED BY public.meeting_presets.id;

CREATE TABLE public.operating_expenses (
    id integer NOT NULL,
    period_month date NOT NULL,
    category text NOT NULL,
    amount_cents integer DEFAULT 0 NOT NULL,
    note text,
    created_by integer,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.operating_expenses_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.operating_expenses_id_seq OWNED BY public.operating_expenses.id;

CREATE TABLE public.pending_bonus_records (
    id bigint NOT NULL,
    referrer_id integer NOT NULL,
    referred_id integer NOT NULL,
    bonus_points bigint NOT NULL,
    rate numeric(5,4) DEFAULT 0.15,
    status character varying(20) DEFAULT 'pending'::character varying,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    settle_at timestamp without time zone NOT NULL,
    settled_at timestamp without time zone,
    cancel_reason character varying(100),
    is_first_recharge boolean DEFAULT true,
    recharge_order_id text,
    charger_user_id integer,
    recharge_amount_cents integer,
    net_cash_revenue_yuan numeric(10,2),
    source character varying(40) DEFAULT 'external_cash_payment'::character varying
);

CREATE SEQUENCE public.pending_bonus_records_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.pending_bonus_records_id_seq OWNED BY public.pending_bonus_records.id;

CREATE TABLE public.pipeline_stage_log (
    id integer NOT NULL,
    brand_id integer NOT NULL,
    stage_name text NOT NULL,
    event text NOT NULL,
    meta jsonb,
    actor_user_id integer,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.pipeline_stage_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.pipeline_stage_log_id_seq OWNED BY public.pipeline_stage_log.id;

CREATE TABLE public.plan_task_script_versions (
    id integer NOT NULL,
    task_id character varying(22) NOT NULL,
    script_id integer,
    title text,
    content text NOT NULL,
    change_reason text,
    created_at timestamp without time zone DEFAULT now()
);

CREATE SEQUENCE public.plan_task_script_versions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.plan_task_script_versions_id_seq OWNED BY public.plan_task_script_versions.id;

CREATE TABLE public.plan_tasks (
    id character varying(22) NOT NULL,
    plan_id character varying(22) NOT NULL,
    week integer NOT NULL,
    day_of_week integer,
    sort_order integer DEFAULT 0,
    topic_title character varying(500) NOT NULL,
    topic_category character varying(20) NOT NULL,
    script_type character varying(20),
    content_group character varying(50),
    duration_seconds integer DEFAULT 60,
    is_pinned boolean DEFAULT false,
    ai_note text,
    status character varying(20) DEFAULT 'idea'::character varying,
    script_id integer,
    planned_date date,
    published_date date,
    performance_data jsonb,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    topic_id integer,
    brief_snapshot jsonb,
    source_refs jsonb,
    assigned_role text,
    assigned_user_id integer,
    success_metric text,
    operator_note text,
    approval_status character varying(20),
    approval_note text,
    approved_at timestamp without time zone
);

CREATE TABLE public.profile_corpus (
    id integer NOT NULL,
    profile_id character varying(20) NOT NULL,
    user_id integer NOT NULL,
    team_id integer,
    source_type character varying(20),
    source_name character varying(200),
    raw_text text,
    extracted_features jsonb,
    personal_quotes jsonb,
    style_markers jsonb,
    business_insights jsonb,
    feature_density double precision,
    word_count integer,
    audio_duration integer,
    asr_status character varying(20) DEFAULT 'completed'::character varying,
    shared_to_team boolean DEFAULT false,
    created_at timestamp without time zone DEFAULT now()
);

CREATE SEQUENCE public.profile_corpus_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.profile_corpus_id_seq OWNED BY public.profile_corpus.id;

CREATE TABLE public.profile_memory_events (
    id bigint NOT NULL,
    profile_id character varying(64) NOT NULL,
    user_id integer,
    source character varying(40) NOT NULL,
    event_type character varying(40) NOT NULL,
    dimension character varying(40),
    title text NOT NULL,
    text text NOT NULL,
    raw_payload jsonb DEFAULT '{}'::jsonb,
    confidence numeric(4,3) DEFAULT 0.700,
    weight_delta integer DEFAULT 1,
    is_active boolean DEFAULT true,
    review_status character varying(20) DEFAULT 'auto'::character varying,
    reviewed_at timestamp without time zone,
    reviewed_by_user_id integer,
    notes text,
    created_at timestamp without time zone DEFAULT now(),
    canonical_concept character varying(40),
    event_fingerprint text,
    last_accessed_at timestamp without time zone,
    access_count integer DEFAULT 0,
    importance_score integer DEFAULT 5,
    CONSTRAINT chk_profile_memory_canonical_concept CHECK (((canonical_concept IS NULL) OR ((canonical_concept)::text = ANY (ARRAY[('business_identity'::character varying)::text, ('target_customer'::character varying)::text, ('offer_and_proof'::character varying)::text, ('voice_style'::character varying)::text, ('guardrails'::character varying)::text])))),
    CONSTRAINT chk_profile_memory_confidence_range CHECK (((confidence IS NULL) OR ((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))),
    CONSTRAINT chk_profile_memory_fingerprint_concept CHECK (((event_fingerprint IS NULL) OR (canonical_concept IS NOT NULL))),
    CONSTRAINT chk_profile_memory_importance_range CHECK (((importance_score IS NULL) OR ((importance_score >= 1) AND (importance_score <= 10)))),
    CONSTRAINT chk_profile_memory_review_status CHECK (((review_status IS NULL) OR ((review_status)::text = ANY (ARRAY[('auto'::character varying)::text, ('pending'::character varying)::text, ('approved'::character varying)::text, ('rejected'::character varying)::text, ('dismissed'::character varying)::text, ('processing'::character varying)::text]))))
);

CREATE SEQUENCE public.profile_memory_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.profile_memory_events_id_seq OWNED BY public.profile_memory_events.id;

CREATE TABLE public.profile_style (
    id integer NOT NULL,
    profile_id character varying(20) NOT NULL,
    user_id integer NOT NULL,
    style_profile jsonb,
    corpus_count integer DEFAULT 0,
    total_duration integer DEFAULT 0,
    top_quotes jsonb,
    business_knowledge jsonb,
    updated_at timestamp without time zone DEFAULT now()
);

CREATE SEQUENCE public.profile_style_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.profile_style_id_seq OWNED BY public.profile_style.id;

CREATE TABLE public.rbac_route_audit (
    id bigint NOT NULL,
    user_id integer,
    user_role character varying(50),
    method character varying(10),
    path text,
    response_code integer,
    blocked_reason character varying(100),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.rbac_route_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.rbac_route_audit_id_seq OWNED BY public.rbac_route_audit.id;

CREATE TABLE public.reference_articles (
    id integer NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    source_url text,
    platform text,
    industry text,
    intent_type text,
    analysis text,
    success_proof text,
    use_count integer DEFAULT 0,
    success_rate real DEFAULT 0,
    status text DEFAULT 'active'::text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    archive_reason character varying(50),
    source_article_id bigint
);

CREATE SEQUENCE public.reference_articles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.reference_articles_id_seq OWNED BY public.reference_articles.id;

CREATE TABLE public.referral_code_quota (
    user_id integer NOT NULL,
    yyyymm integer NOT NULL,
    codes_issued integer DEFAULT 0,
    monthly_limit integer NOT NULL
);

CREATE TABLE public.result_types (
    id integer NOT NULL,
    name text NOT NULL,
    icon text DEFAULT '📄'::text,
    color text DEFAULT '#6366f1'::text,
    description text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.result_types_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.result_types_id_seq OWNED BY public.result_types.id;

CREATE TABLE public.service_fee_clawback_pending (
    id bigint NOT NULL,
    user_id integer NOT NULL,
    source_order_id text NOT NULL,
    amount_due numeric(10,2) NOT NULL,
    amount_settled numeric(10,2) DEFAULT 0,
    status character varying(20) DEFAULT 'pending'::character varying,
    reason character varying(200),
    refund_category character varying(40),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    settled_at timestamp without time zone
);

CREATE SEQUENCE public.service_fee_clawback_pending_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.service_fee_clawback_pending_id_seq OWNED BY public.service_fee_clawback_pending.id;

CREATE TABLE public.service_fee_conversion_orders (
    id bigint NOT NULL,
    service_fee_record_ids bigint[] NOT NULL,
    user_id integer NOT NULL,
    amount_yuan numeric(10,2) NOT NULL,
    paid_points_granted bigint NOT NULL,
    bonus_points_granted bigint NOT NULL,
    bonus_rate numeric(5,4) DEFAULT 0.20,
    bonus_expires_at timestamp without time zone NOT NULL,
    status character varying(20) DEFAULT 'completed'::character varying,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    requires_review boolean DEFAULT false,
    review_status character varying(20),
    reviewed_by integer,
    reviewed_at timestamp without time zone
);

CREATE SEQUENCE public.service_fee_conversion_orders_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.service_fee_conversion_orders_id_seq OWNED BY public.service_fee_conversion_orders.id;

CREATE TABLE public.service_fee_conversion_quota (
    user_id integer NOT NULL,
    yyyymm integer NOT NULL,
    quota_yuan numeric(10,2) NOT NULL,
    used_yuan numeric(10,2) DEFAULT 0,
    tier character varying(20) DEFAULT 'standard'::character varying
);

CREATE TABLE public.service_fee_records (
    id bigint NOT NULL,
    user_id integer NOT NULL,
    source_user_id integer NOT NULL,
    source_order_id text NOT NULL,
    source_order_type character varying(30) NOT NULL,
    source_payment_method character varying(20),
    source_payment_source character varying(40) DEFAULT 'external_cash_payment'::character varying,
    gross_amount_yuan numeric(10,2),
    refund_amount_yuan numeric(10,2) DEFAULT 0,
    media_cost_yuan numeric(10,2) DEFAULT 0,
    coupon_yuan numeric(10,2) DEFAULT 0,
    bonus_deducted_yuan numeric(10,2) DEFAULT 0,
    granted_points_deducted_yuan numeric(10,2) DEFAULT 0,
    gateway_fee_yuan numeric(10,2) DEFAULT 0,
    net_cash_revenue_yuan numeric(10,2),
    amount_yuan numeric(10,2) NOT NULL,
    service_fee_rate numeric(5,4) DEFAULT 0.22,
    status character varying(30) DEFAULT 'pending'::character varying,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    available_at timestamp without time zone NOT NULL,
    settled_at timestamp without time zone,
    converted_at timestamp without time zone,
    withdraw_requested_at timestamp without time zone,
    withdrawn_at timestamp without time zone,
    clawback_amount numeric(10,2) DEFAULT 0,
    clawback_reason character varying(200),
    review_status character varying(20),
    reviewed_by integer,
    reviewed_at timestamp without time zone,
    review_note text
);

CREATE SEQUENCE public.service_fee_records_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.service_fee_records_id_seq OWNED BY public.service_fee_records.id;

CREATE TABLE public.service_fee_settlements (
    id bigint NOT NULL,
    user_id integer NOT NULL,
    settlement_type character varying(20) NOT NULL,
    related_record_ids bigint[],
    amount_yuan numeric(10,2) NOT NULL,
    status character varying(20) DEFAULT 'pending'::character varying,
    bank_account_masked character varying(50),
    invoice_required boolean DEFAULT false,
    invoice_uploaded boolean DEFAULT false,
    invoice_url text,
    requires_dual_sign boolean DEFAULT false,
    second_signer_id integer,
    second_signed_at timestamp without time zone,
    reviewed_by integer,
    reviewed_at timestamp without time zone,
    review_note text,
    completed_at timestamp without time zone,
    settlement_evidence text,
    partial_amount_yuan numeric(10,2),
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.service_fee_settlements_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.service_fee_settlements_id_seq OWNED BY public.service_fee_settlements.id;

CREATE TABLE public.sms_codes (
    phone character varying(20) NOT NULL,
    code character varying(10) NOT NULL,
    purpose character varying(30) DEFAULT 'login'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    expires_at double precision NOT NULL,
    created_at timestamp without time zone DEFAULT now()
);

CREATE TABLE public.social_admin_settings (
    setting_key character varying(64) NOT NULL,
    setting_value text,
    setting_type character varying(16) DEFAULT 'string'::character varying,
    description text,
    updated_by_user_id integer,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE public.social_agent_metrics (
    id bigint NOT NULL,
    turn_id text,
    user_id integer,
    profile_id text,
    model text,
    used_tools jsonb DEFAULT '[]'::jsonb NOT NULL,
    tool_call_count integer DEFAULT 0 NOT NULL,
    tool_error_count integer DEFAULT 0 NOT NULL,
    cost_points integer DEFAULT 0 NOT NULL,
    latency_ms integer DEFAULT 0 NOT NULL,
    plan_completed boolean DEFAULT false NOT NULL,
    confirmation_requested boolean DEFAULT false NOT NULL,
    confirmation_confirmed boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);

CREATE SEQUENCE public.social_agent_metrics_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_agent_metrics_id_seq OWNED BY public.social_agent_metrics.id;

CREATE TABLE public.social_agent_plans (
    plan_id character varying(36) NOT NULL,
    profile_id text NOT NULL,
    user_id integer NOT NULL,
    state_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    current_step integer DEFAULT 0,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    user_intent_summary text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT social_agent_plans_status_check CHECK (((status)::text = ANY (ARRAY[('active'::character varying)::text, ('stale'::character varying)::text, ('completed'::character varying)::text, ('cancelled'::character varying)::text, ('superseded'::character varying)::text])))
);

CREATE TABLE public.social_agent_sessions (
    session_id text NOT NULL,
    profile_id text NOT NULL,
    user_id integer,
    summary text DEFAULT ''::text NOT NULL,
    topic text DEFAULT ''::text NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    started_at timestamp with time zone DEFAULT now(),
    last_active_at timestamp with time zone DEFAULT now(),
    CONSTRAINT social_agent_sessions_status_check CHECK (((status)::text = ANY (ARRAY[('active'::character varying)::text, ('ended'::character varying)::text, ('archived'::character varying)::text])))
);

CREATE TABLE public.social_agent_tool_audit (
    id bigint NOT NULL,
    turn_id text,
    user_id integer,
    profile_id text,
    tool_name text NOT NULL,
    args_hash text NOT NULL,
    result_chars integer DEFAULT 0 NOT NULL,
    status character varying(20) NOT NULL,
    error text,
    latency_ms integer DEFAULT 0 NOT NULL,
    cost_points integer DEFAULT 0 NOT NULL,
    charged_points integer DEFAULT 0 NOT NULL,
    refunded_points integer DEFAULT 0 NOT NULL,
    billing_status character varying(30),
    billing_note text,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT social_agent_tool_audit_status_check CHECK (((status)::text = ANY (ARRAY[('ok'::character varying)::text, ('error'::character varying)::text])))
);

CREATE SEQUENCE public.social_agent_tool_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_agent_tool_audit_id_seq OWNED BY public.social_agent_tool_audit.id;

CREATE TABLE public.social_agent_turns (
    id bigint NOT NULL,
    turn_id text,
    session_id text NOT NULL,
    user_id integer,
    profile_id text,
    role character varying(20) NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    tool_calls_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    cost_points integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT social_agent_turns_role_check CHECK (((role)::text = ANY (ARRAY[('user'::character varying)::text, ('assistant'::character varying)::text, ('tool'::character varying)::text, ('system'::character varying)::text])))
);

CREATE SEQUENCE public.social_agent_turns_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_agent_turns_id_seq OWNED BY public.social_agent_turns.id;

CREATE TABLE public.social_benchmarks (
    id integer NOT NULL,
    project_id integer NOT NULL,
    platform text NOT NULL,
    account_id text NOT NULL,
    account_name text,
    follower_count integer DEFAULT 0,
    is_competitor integer DEFAULT 0,
    notes text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.social_benchmarks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_benchmarks_id_seq OWNED BY public.social_benchmarks.id;

CREATE TABLE public.social_interactions (
    id integer NOT NULL,
    publication_id integer,
    script_id integer,
    platform text,
    interaction_type text,
    user_handle text,
    user_profile_url text,
    content text,
    sentiment text,
    is_lead boolean DEFAULT false,
    lead_status text,
    raw_data jsonb DEFAULT '{}'::jsonb,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.social_interactions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_interactions_id_seq OWNED BY public.social_interactions.id;

CREATE TABLE public.social_lifecycle_events (
    id integer NOT NULL,
    event_id character varying(24) NOT NULL,
    profile_id text,
    brand_id integer,
    team_id integer,
    actor_user_id text,
    plan_id character varying(22),
    plan_task_id character varying(22),
    topic_id integer,
    script_id integer,
    publication_id integer,
    event_type text NOT NULL,
    event_source text DEFAULT 'api'::text,
    payload jsonb DEFAULT '{}'::jsonb,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.social_lifecycle_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_lifecycle_events_id_seq OWNED BY public.social_lifecycle_events.id;

CREATE TABLE public.social_materials (
    id integer NOT NULL,
    project_id integer NOT NULL,
    platform text NOT NULL,
    video_id text NOT NULL,
    video_url text,
    title text,
    author_name text,
    author_id text,
    transcript text,
    opening_type text,
    content_type text,
    structure_analysis text,
    golden_phrases text,
    stats_digg integer DEFAULT 0,
    stats_comment integer DEFAULT 0,
    stats_share integer DEFAULT 0,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    profile_id text,
    advisor_id text,
    update_source text DEFAULT 'ai_generated'::text,
    material_type text DEFAULT 'video'::text,
    brand_id integer
);

CREATE SEQUENCE public.social_materials_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_materials_id_seq OWNED BY public.social_materials.id;

CREATE TABLE public.social_personas (
    id integer NOT NULL,
    project_id integer,
    one_liner text,
    visual_style text,
    speaking_style text,
    target_audience text,
    content_pillars text,
    persona_details text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.social_personas_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_personas_id_seq OWNED BY public.social_personas.id;

CREATE TABLE public.social_projects (
    id integer NOT NULL,
    name text NOT NULL,
    industry text,
    business text,
    target_audience text,
    product_intro text,
    advisor_style text DEFAULT '小黄编导'::text,
    status text DEFAULT 'active'::text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    brand_id integer,
    profile_id text
);

CREATE SEQUENCE public.social_projects_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_projects_id_seq OWNED BY public.social_projects.id;

CREATE TABLE public.social_publication_metrics (
    id integer NOT NULL,
    publication_id integer,
    script_id integer,
    platform text,
    views integer DEFAULT 0,
    likes integer DEFAULT 0,
    comments integer DEFAULT 0,
    shares integer DEFAULT 0,
    collects integer DEFAULT 0,
    follows integer DEFAULT 0,
    dms integer DEFAULT 0,
    leads integer DEFAULT 0,
    completion_rate numeric,
    five_sec_drop_rate numeric,
    engagement_rate numeric,
    raw_data jsonb DEFAULT '{}'::jsonb,
    recorded_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.social_publication_metrics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_publication_metrics_id_seq OWNED BY public.social_publication_metrics.id;

CREATE TABLE public.social_publications (
    id integer NOT NULL,
    profile_id text,
    brand_id integer,
    team_id integer,
    plan_id character varying(22),
    plan_task_id character varying(22),
    script_id integer,
    platform text,
    publish_url text,
    platform_item_id text,
    status text DEFAULT 'published'::text,
    published_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.social_publications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_publications_id_seq OWNED BY public.social_publications.id;

CREATE TABLE public.social_script_versions (
    id integer NOT NULL,
    script_id integer NOT NULL,
    version_num integer DEFAULT 1,
    content text NOT NULL,
    change_reason text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.social_script_versions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_script_versions_id_seq OWNED BY public.social_script_versions.id;

CREATE TABLE public.social_scripts (
    id integer NOT NULL,
    project_id integer,
    topic_id integer,
    title text NOT NULL,
    script_content text NOT NULL,
    opening_type text,
    content_type text,
    word_count integer DEFAULT 0,
    source text,
    source_video_url text,
    original_transcript text,
    status text DEFAULT 'draft'::text,
    publish_date date,
    performance_data text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    profile_id text,
    advisor_id text,
    update_source text DEFAULT 'manual'::text,
    is_filmed integer DEFAULT 0,
    user_id text,
    script_structure text,
    framework_id integer,
    generation_metadata text,
    platform text,
    brand_id integer,
    original_video_data text
);

CREATE SEQUENCE public.social_scripts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_scripts_id_seq OWNED BY public.social_scripts.id;

CREATE TABLE public.social_topics (
    id integer NOT NULL,
    project_id integer NOT NULL,
    title text NOT NULL,
    topic_type text,
    content_type text,
    user_level text,
    opening_type text,
    source text,
    source_video_id text,
    status text DEFAULT 'pending'::text,
    priority integer DEFAULT 0,
    notes text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    used_at timestamp without time zone,
    profile_id text,
    advisor_id text,
    update_source text DEFAULT 'manual'::text,
    title_hash text,
    skipped_at timestamp without time zone,
    view_count integer DEFAULT 0,
    last_shown_at timestamp without time zone,
    confirmed_at timestamp without time zone,
    brand_id integer
);

CREATE SEQUENCE public.social_topics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.social_topics_id_seq OWNED BY public.social_topics.id;

CREATE TABLE public.subscription_addon_purchases (
    id integer NOT NULL,
    user_id integer NOT NULL,
    addon_id character varying(64) NOT NULL,
    subscription_id bigint NOT NULL,
    entitlement_id bigint NOT NULL,
    yuan_paid numeric(8,2) NOT NULL,
    points_deducted integer NOT NULL,
    payment_method character varying(16) DEFAULT 'points'::character varying NOT NULL,
    order_id character varying(128) NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.subscription_addon_purchases_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.subscription_addon_purchases_id_seq OWNED BY public.subscription_addon_purchases.id;

CREATE TABLE public.subscription_addons (
    id integer NOT NULL,
    addon_id character varying(64) NOT NULL,
    target_quota character varying(64) NOT NULL,
    amount integer NOT NULL,
    yuan_price numeric(8,2) NOT NULL,
    points_price integer NOT NULL,
    display_name character varying(128) NOT NULL,
    description text,
    best_for text,
    min_plan_id character varying(32),
    max_per_month integer DEFAULT 5,
    sort_order integer DEFAULT 100,
    is_active boolean DEFAULT true,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT addon_amount_positive CHECK ((amount > 0)),
    CONSTRAINT addon_yuan_positive CHECK ((yuan_price > (0)::numeric))
);

CREATE SEQUENCE public.subscription_addons_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.subscription_addons_id_seq OWNED BY public.subscription_addons.id;

CREATE TABLE public.topic_embeddings (
    id integer NOT NULL,
    topic_id integer,
    project_id integer,
    embedding text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.topic_embeddings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.topic_embeddings_id_seq OWNED BY public.topic_embeddings.id;

CREATE TABLE public.user_module_overrides (
    user_id integer NOT NULL,
    module_name character varying(50) NOT NULL,
    granted boolean DEFAULT true NOT NULL
);

CREATE TABLE public.user_social_preferences (
    user_id integer NOT NULL,
    default_guardrail character varying(20) DEFAULT 'balanced'::character varying,
    default_duration_seconds integer DEFAULT 180,
    default_research_mode character varying(10) DEFAULT 'auto'::character varying,
    default_quality_tier character varying(20) DEFAULT 'normal'::character varying,
    theme character varying(10) DEFAULT 'system'::character varying,
    notify_email boolean DEFAULT true,
    notify_inapp boolean DEFAULT true,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE public.user_tag_assignments (
    user_id integer NOT NULL,
    tag_id integer NOT NULL
);

CREATE TABLE public.user_tags (
    id integer NOT NULL,
    name text NOT NULL,
    color text DEFAULT '#6b7280'::text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE public.user_tags_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.user_tags_id_seq OWNED BY public.user_tags.id;

CREATE TABLE public.viral_frameworks (
    id integer NOT NULL,
    project_id integer,
    name text NOT NULL,
    source_type text DEFAULT 'breakdown'::text,
    source_url text,
    source_author text,
    hook_formula text,
    structure_template text,
    rhythm_pattern text,
    emotion_arc text,
    cta_patterns text,
    golden_phrases text,
    full_framework text,
    is_pinned boolean DEFAULT false,
    is_archived boolean DEFAULT false,
    use_count integer DEFAULT 0,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    scope text DEFAULT 'personal'::text,
    user_id integer,
    team_id integer
);

CREATE SEQUENCE public.viral_frameworks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.viral_frameworks_id_seq OWNED BY public.viral_frameworks.id;

CREATE TABLE public.work_results (
    id integer NOT NULL,
    title text NOT NULL,
    result_type text,
    brand_id integer,
    brand_name text,
    meeting_id text,
    assignee_id text,
    assignee_name text,
    content text,
    attachments text,
    tags text,
    keywords text,
    status text DEFAULT '待执行'::text,
    priority integer DEFAULT 2,
    due_date text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    completed_at timestamp without time zone
);

CREATE SEQUENCE public.work_results_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.work_results_id_seq OWNED BY public.work_results.id;

CREATE TABLE public.writing_article_version_fingerprints (
    id bigint NOT NULL,
    article_id bigint NOT NULL,
    style_code character varying(60),
    style_version_id character varying(200),
    prompt_sha256 character(64),
    structure_guidance_applied boolean DEFAULT false NOT NULL,
    strategy_id bigint,
    strategy_version character varying(240),
    industry_key character varying(100) DEFAULT 'general'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    style_family character varying(60)
);

CREATE SEQUENCE public.writing_article_version_fingerprints_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.writing_article_version_fingerprints_id_seq OWNED BY public.writing_article_version_fingerprints.id;

CREATE TABLE public.writing_corpus_export_audit (
    id bigint NOT NULL,
    operator_id bigint DEFAULT 0 NOT NULL,
    filters jsonb DEFAULT '{}'::jsonb NOT NULL,
    exported_count integer DEFAULT 0 NOT NULL,
    exported_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.writing_corpus_export_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.writing_corpus_export_audit_id_seq OWNED BY public.writing_corpus_export_audit.id;

CREATE TABLE public.writing_style_simulations (
    id bigint NOT NULL,
    style_code character varying(60) NOT NULL,
    industry_key character varying(100) DEFAULT 'general'::character varying NOT NULL,
    version_id character varying(200),
    topic_title text DEFAULT ''::text NOT NULL,
    demo_quote_id bigint,
    demo_brand_name text,
    current_article text DEFAULT ''::text NOT NULL,
    candidate_article text DEFAULT ''::text NOT NULL,
    current_word_count integer DEFAULT 0 NOT NULL,
    candidate_word_count integer DEFAULT 0 NOT NULL,
    structure_guidance_state character varying(24) DEFAULT 'unknown'::character varying NOT NULL,
    user_message_identical boolean,
    cost_note text,
    review_summary jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(20) DEFAULT 'generated'::character varying NOT NULL,
    error text,
    created_by bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    structure_diff jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE SEQUENCE public.writing_style_simulations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.writing_style_simulations_id_seq OWNED BY public.writing_style_simulations.id;

ALTER TABLE ONLY public.activity_log ALTER COLUMN id SET DEFAULT nextval('public.activity_log_id_seq'::regclass);

ALTER TABLE ONLY public.advisor_conversations ALTER COLUMN id SET DEFAULT nextval('public.advisor_conversations_id_seq'::regclass);

ALTER TABLE ONLY public.advisor_messages ALTER COLUMN id SET DEFAULT nextval('public.advisor_messages_id_seq'::regclass);

ALTER TABLE ONLY public.agent_commission_redemption_items ALTER COLUMN id SET DEFAULT nextval('public.agent_commission_redemption_items_id_seq'::regclass);

ALTER TABLE ONLY public.agent_commission_redemption_requests ALTER COLUMN id SET DEFAULT nextval('public.agent_commission_redemption_requests_id_seq'::regclass);

ALTER TABLE ONLY public.agent_tier_change_log ALTER COLUMN id SET DEFAULT nextval('public.agent_tier_change_log_id_seq'::regclass);

ALTER TABLE ONLY public.ai_suggestions ALTER COLUMN id SET DEFAULT nextval('public.ai_suggestions_id_seq'::regclass);

ALTER TABLE ONLY public.bonus_clawback_pending ALTER COLUMN id SET DEFAULT nextval('public.bonus_clawback_pending_id_seq'::regclass);

ALTER TABLE ONLY public.bonus_grants ALTER COLUMN id SET DEFAULT nextval('public.bonus_grants_id_seq'::regclass);

ALTER TABLE ONLY public.brand_aliases ALTER COLUMN id SET DEFAULT nextval('public.brand_aliases_id_seq'::regclass);

ALTER TABLE ONLY public.customer_credit_freezes ALTER COLUMN id SET DEFAULT nextval('public.customer_credit_freezes_id_seq'::regclass);

ALTER TABLE ONLY public.draft_workspace ALTER COLUMN id SET DEFAULT nextval('public.draft_workspace_id_seq'::regclass);

ALTER TABLE ONLY public.employee_capabilities ALTER COLUMN id SET DEFAULT nextval('public.employee_capabilities_id_seq'::regclass);

ALTER TABLE ONLY public.failed_service_fee_jobs ALTER COLUMN id SET DEFAULT nextval('public.failed_service_fee_jobs_id_seq'::regclass);

ALTER TABLE ONLY public.flywheel_judgment_log ALTER COLUMN id SET DEFAULT nextval('public.flywheel_judgment_log_id_seq'::regclass);

ALTER TABLE ONLY public.geo_answer_adoption_metrics ALTER COLUMN id SET DEFAULT nextval('public.geo_answer_adoption_metrics_id_seq'::regclass);

ALTER TABLE ONLY public.geo_engine_weight_candidates ALTER COLUMN id SET DEFAULT nextval('public.geo_engine_weight_candidates_id_seq'::regclass);

ALTER TABLE ONLY public.geo_query_intent ALTER COLUMN id SET DEFAULT nextval('public.geo_query_intent_id_seq'::regclass);

ALTER TABLE ONLY public.geo_research_answer_entities ALTER COLUMN id SET DEFAULT nextval('public.geo_research_answer_entities_id_seq'::regclass);

ALTER TABLE ONLY public.geo_research_answer_facts ALTER COLUMN id SET DEFAULT nextval('public.geo_research_answer_facts_id_seq'::regclass);

ALTER TABLE ONLY public.imitated_articles ALTER COLUMN id SET DEFAULT nextval('public.imitated_articles_id_seq'::regclass);

ALTER TABLE ONLY public.imitation_records ALTER COLUMN id SET DEFAULT nextval('public.imitation_records_id_seq'::regclass);

ALTER TABLE ONLY public.industry_brief_history ALTER COLUMN id SET DEFAULT nextval('public.industry_brief_history_id_seq'::regclass);

ALTER TABLE ONLY public.industry_knowledge ALTER COLUMN id SET DEFAULT nextval('public.industry_knowledge_id_seq'::regclass);

ALTER TABLE ONLY public.invite_codes ALTER COLUMN id SET DEFAULT nextval('public.invite_codes_id_seq'::regclass);

ALTER TABLE ONLY public.keyword_insights ALTER COLUMN id SET DEFAULT nextval('public.keyword_insights_id_seq'::regclass);

ALTER TABLE ONLY public.m3_customer_events ALTER COLUMN id SET DEFAULT nextval('public.m3_customer_events_id_seq'::regclass);

ALTER TABLE ONLY public.marketing_confirm_sessions ALTER COLUMN id SET DEFAULT nextval('public.marketing_confirm_sessions_id_seq'::regclass);

ALTER TABLE ONLY public.meeting_presets ALTER COLUMN id SET DEFAULT nextval('public.meeting_presets_id_seq'::regclass);

ALTER TABLE ONLY public.operating_expenses ALTER COLUMN id SET DEFAULT nextval('public.operating_expenses_id_seq'::regclass);

ALTER TABLE ONLY public.pending_bonus_records ALTER COLUMN id SET DEFAULT nextval('public.pending_bonus_records_id_seq'::regclass);

ALTER TABLE ONLY public.pipeline_stage_log ALTER COLUMN id SET DEFAULT nextval('public.pipeline_stage_log_id_seq'::regclass);

ALTER TABLE ONLY public.plan_task_script_versions ALTER COLUMN id SET DEFAULT nextval('public.plan_task_script_versions_id_seq'::regclass);

ALTER TABLE ONLY public.profile_corpus ALTER COLUMN id SET DEFAULT nextval('public.profile_corpus_id_seq'::regclass);

ALTER TABLE ONLY public.profile_memory_events ALTER COLUMN id SET DEFAULT nextval('public.profile_memory_events_id_seq'::regclass);

ALTER TABLE ONLY public.profile_style ALTER COLUMN id SET DEFAULT nextval('public.profile_style_id_seq'::regclass);

ALTER TABLE ONLY public.rbac_route_audit ALTER COLUMN id SET DEFAULT nextval('public.rbac_route_audit_id_seq'::regclass);

ALTER TABLE ONLY public.reference_articles ALTER COLUMN id SET DEFAULT nextval('public.reference_articles_id_seq'::regclass);

ALTER TABLE ONLY public.result_types ALTER COLUMN id SET DEFAULT nextval('public.result_types_id_seq'::regclass);

ALTER TABLE ONLY public.service_fee_clawback_pending ALTER COLUMN id SET DEFAULT nextval('public.service_fee_clawback_pending_id_seq'::regclass);

ALTER TABLE ONLY public.service_fee_conversion_orders ALTER COLUMN id SET DEFAULT nextval('public.service_fee_conversion_orders_id_seq'::regclass);

ALTER TABLE ONLY public.service_fee_records ALTER COLUMN id SET DEFAULT nextval('public.service_fee_records_id_seq'::regclass);

ALTER TABLE ONLY public.service_fee_settlements ALTER COLUMN id SET DEFAULT nextval('public.service_fee_settlements_id_seq'::regclass);

ALTER TABLE ONLY public.social_agent_metrics ALTER COLUMN id SET DEFAULT nextval('public.social_agent_metrics_id_seq'::regclass);

ALTER TABLE ONLY public.social_agent_tool_audit ALTER COLUMN id SET DEFAULT nextval('public.social_agent_tool_audit_id_seq'::regclass);

ALTER TABLE ONLY public.social_agent_turns ALTER COLUMN id SET DEFAULT nextval('public.social_agent_turns_id_seq'::regclass);

ALTER TABLE ONLY public.social_benchmarks ALTER COLUMN id SET DEFAULT nextval('public.social_benchmarks_id_seq'::regclass);

ALTER TABLE ONLY public.social_interactions ALTER COLUMN id SET DEFAULT nextval('public.social_interactions_id_seq'::regclass);

ALTER TABLE ONLY public.social_lifecycle_events ALTER COLUMN id SET DEFAULT nextval('public.social_lifecycle_events_id_seq'::regclass);

ALTER TABLE ONLY public.social_materials ALTER COLUMN id SET DEFAULT nextval('public.social_materials_id_seq'::regclass);

ALTER TABLE ONLY public.social_personas ALTER COLUMN id SET DEFAULT nextval('public.social_personas_id_seq'::regclass);

ALTER TABLE ONLY public.social_projects ALTER COLUMN id SET DEFAULT nextval('public.social_projects_id_seq'::regclass);

ALTER TABLE ONLY public.social_publication_metrics ALTER COLUMN id SET DEFAULT nextval('public.social_publication_metrics_id_seq'::regclass);

ALTER TABLE ONLY public.social_publications ALTER COLUMN id SET DEFAULT nextval('public.social_publications_id_seq'::regclass);

ALTER TABLE ONLY public.social_script_versions ALTER COLUMN id SET DEFAULT nextval('public.social_script_versions_id_seq'::regclass);

ALTER TABLE ONLY public.social_scripts ALTER COLUMN id SET DEFAULT nextval('public.social_scripts_id_seq'::regclass);

ALTER TABLE ONLY public.social_topics ALTER COLUMN id SET DEFAULT nextval('public.social_topics_id_seq'::regclass);

ALTER TABLE ONLY public.subscription_addon_purchases ALTER COLUMN id SET DEFAULT nextval('public.subscription_addon_purchases_id_seq'::regclass);

ALTER TABLE ONLY public.subscription_addons ALTER COLUMN id SET DEFAULT nextval('public.subscription_addons_id_seq'::regclass);

ALTER TABLE ONLY public.topic_embeddings ALTER COLUMN id SET DEFAULT nextval('public.topic_embeddings_id_seq'::regclass);

ALTER TABLE ONLY public.user_tags ALTER COLUMN id SET DEFAULT nextval('public.user_tags_id_seq'::regclass);

ALTER TABLE ONLY public.viral_frameworks ALTER COLUMN id SET DEFAULT nextval('public.viral_frameworks_id_seq'::regclass);

ALTER TABLE ONLY public.work_results ALTER COLUMN id SET DEFAULT nextval('public.work_results_id_seq'::regclass);

ALTER TABLE ONLY public.writing_article_version_fingerprints ALTER COLUMN id SET DEFAULT nextval('public.writing_article_version_fingerprints_id_seq'::regclass);

ALTER TABLE ONLY public.writing_corpus_export_audit ALTER COLUMN id SET DEFAULT nextval('public.writing_corpus_export_audit_id_seq'::regclass);

ALTER TABLE ONLY public.writing_style_simulations ALTER COLUMN id SET DEFAULT nextval('public.writing_style_simulations_id_seq'::regclass);

ALTER TABLE ONLY public.activity_log
    ADD CONSTRAINT activity_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.advisor_conversations
    ADD CONSTRAINT advisor_conversations_conversation_id_key UNIQUE (conversation_id);

ALTER TABLE ONLY public.advisor_conversations
    ADD CONSTRAINT advisor_conversations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.advisor_messages
    ADD CONSTRAINT advisor_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_channel_tier_state
    ADD CONSTRAINT agent_channel_tier_state_pkey PRIMARY KEY (agent_user_id);

ALTER TABLE ONLY public.agent_commission_redemption_items
    ADD CONSTRAINT agent_commission_redemption_i_ledger_id_redemption_request__key UNIQUE (ledger_id, redemption_request_id);

ALTER TABLE ONLY public.agent_commission_redemption_items
    ADD CONSTRAINT agent_commission_redemption_items_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_commission_redemption_requests
    ADD CONSTRAINT agent_commission_redemption_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_rebate_config
    ADD CONSTRAINT agent_rebate_config_pkey PRIMARY KEY (agent_user_id);

ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.agent_tier_change_log
    ADD CONSTRAINT agent_tier_change_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.ai_suggestions
    ADD CONSTRAINT ai_suggestions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.bonus_clawback_pending
    ADD CONSTRAINT bonus_clawback_pending_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.bonus_clawback_pending
    ADD CONSTRAINT bonus_clawback_pending_user_id_recharge_order_id_key UNIQUE (user_id, recharge_order_id);

ALTER TABLE ONLY public.bonus_grants
    ADD CONSTRAINT bonus_grants_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.brand_aliases
    ADD CONSTRAINT brand_aliases_canonical_name_alias_key UNIQUE (canonical_name, alias);

ALTER TABLE ONLY public.brand_aliases
    ADD CONSTRAINT brand_aliases_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.brand_merge_map
    ADD CONSTRAINT brand_merge_map_pkey PRIMARY KEY (old_brand_id);

ALTER TABLE ONLY public.content_plans
    ADD CONSTRAINT content_plans_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.customer_credit_freezes
    ADD CONSTRAINT customer_credit_freezes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.domain_authority_cache
    ADD CONSTRAINT domain_authority_cache_pkey PRIMARY KEY (domain);

ALTER TABLE ONLY public.draft_workspace
    ADD CONSTRAINT draft_workspace_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.draft_workspace
    ADD CONSTRAINT draft_workspace_user_id_brand_id_item_type_item_key_key UNIQUE (user_id, brand_id, item_type, item_key);

ALTER TABLE ONLY public.employee_capabilities
    ADD CONSTRAINT employee_capabilities_employee_id_capability_type_target_id_key UNIQUE (employee_id, capability_type, target_id);

ALTER TABLE ONLY public.employee_capabilities
    ADD CONSTRAINT employee_capabilities_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.failed_service_fee_jobs
    ADD CONSTRAINT failed_service_fee_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.failed_service_fee_jobs
    ADD CONSTRAINT failed_service_fee_jobs_source_order_id_key UNIQUE (source_order_id);

ALTER TABLE ONLY public.flywheel_judgment_log
    ADD CONSTRAINT flywheel_judgment_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.founder_seats
    ADD CONSTRAINT founder_seats_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.geo_answer_adoption_metrics
    ADD CONSTRAINT geo_answer_adoption_metrics_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.geo_answer_adoption_metrics
    ADD CONSTRAINT geo_answer_adoption_metrics_source_url_industry_key_engine__key UNIQUE (source_url, industry_key, engine, prompt_id, signal_tier, round_id);

ALTER TABLE ONLY public.geo_engine_weight_candidates
    ADD CONSTRAINT geo_engine_weight_candidates_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.geo_query_intent
    ADD CONSTRAINT geo_query_intent_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.geo_research_answer_entities
    ADD CONSTRAINT geo_research_answer_entities_answer_fact_id_entity_key_key UNIQUE (answer_fact_id, entity_key);

ALTER TABLE ONLY public.geo_research_answer_entities
    ADD CONSTRAINT geo_research_answer_entities_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.geo_research_answer_facts
    ADD CONSTRAINT geo_research_answer_facts_engine_batch_id_answer_hash_key UNIQUE (engine, batch_id, answer_hash);

ALTER TABLE ONLY public.geo_research_answer_facts
    ADD CONSTRAINT geo_research_answer_facts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.imitated_articles
    ADD CONSTRAINT imitated_articles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.imitation_records
    ADD CONSTRAINT imitation_records_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.industry_brief_history
    ADD CONSTRAINT industry_brief_history_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.industry_brief_history
    ADD CONSTRAINT industry_brief_history_profile_id_version_num_key UNIQUE (profile_id, version_num);

ALTER TABLE ONLY public.industry_knowledge
    ADD CONSTRAINT industry_knowledge_level_industry_category_key UNIQUE (level, industry, category);

ALTER TABLE ONLY public.industry_knowledge
    ADD CONSTRAINT industry_knowledge_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.interview_sessions
    ADD CONSTRAINT interview_sessions_pkey PRIMARY KEY (session_id);

ALTER TABLE ONLY public.invite_codes
    ADD CONSTRAINT invite_codes_code_key UNIQUE (code);

ALTER TABLE ONLY public.invite_codes
    ADD CONSTRAINT invite_codes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.keyword_insights
    ADD CONSTRAINT keyword_insights_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.keyword_insights
    ADD CONSTRAINT keyword_insights_task_id_keyword_key UNIQUE (task_id, keyword);

ALTER TABLE ONLY public.m3_customer_events
    ADD CONSTRAINT m3_customer_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.marketing_confirm_sessions
    ADD CONSTRAINT marketing_confirm_sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.marketing_confirm_sessions
    ADD CONSTRAINT marketing_confirm_sessions_token_key UNIQUE (token);

ALTER TABLE ONLY public.meeting_presets
    ADD CONSTRAINT meeting_presets_name_key UNIQUE (name);

ALTER TABLE ONLY public.meeting_presets
    ADD CONSTRAINT meeting_presets_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.operating_expenses
    ADD CONSTRAINT operating_expenses_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.pending_bonus_records
    ADD CONSTRAINT pending_bonus_records_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.pipeline_stage_log
    ADD CONSTRAINT pipeline_stage_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.plan_task_script_versions
    ADD CONSTRAINT plan_task_script_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.plan_tasks
    ADD CONSTRAINT plan_tasks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.profile_corpus
    ADD CONSTRAINT profile_corpus_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.profile_memory_events
    ADD CONSTRAINT profile_memory_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.profile_style
    ADD CONSTRAINT profile_style_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.profile_style
    ADD CONSTRAINT profile_style_profile_id_key UNIQUE (profile_id);

ALTER TABLE ONLY public.rbac_route_audit
    ADD CONSTRAINT rbac_route_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.reference_articles
    ADD CONSTRAINT reference_articles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.referral_code_quota
    ADD CONSTRAINT referral_code_quota_pkey PRIMARY KEY (user_id, yyyymm);

ALTER TABLE ONLY public.result_types
    ADD CONSTRAINT result_types_name_key UNIQUE (name);

ALTER TABLE ONLY public.result_types
    ADD CONSTRAINT result_types_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.service_fee_clawback_pending
    ADD CONSTRAINT service_fee_clawback_pending_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.service_fee_clawback_pending
    ADD CONSTRAINT service_fee_clawback_pending_user_id_source_order_id_key UNIQUE (user_id, source_order_id);

ALTER TABLE ONLY public.service_fee_conversion_orders
    ADD CONSTRAINT service_fee_conversion_orders_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.service_fee_conversion_quota
    ADD CONSTRAINT service_fee_conversion_quota_pkey PRIMARY KEY (user_id, yyyymm);

ALTER TABLE ONLY public.service_fee_records
    ADD CONSTRAINT service_fee_records_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.service_fee_settlements
    ADD CONSTRAINT service_fee_settlements_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sms_codes
    ADD CONSTRAINT sms_codes_pkey PRIMARY KEY (phone);

ALTER TABLE ONLY public.social_admin_settings
    ADD CONSTRAINT social_admin_settings_pkey PRIMARY KEY (setting_key);

ALTER TABLE ONLY public.social_agent_metrics
    ADD CONSTRAINT social_agent_metrics_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_agent_plans
    ADD CONSTRAINT social_agent_plans_pkey PRIMARY KEY (plan_id);

ALTER TABLE ONLY public.social_agent_sessions
    ADD CONSTRAINT social_agent_sessions_pkey PRIMARY KEY (session_id);

ALTER TABLE ONLY public.social_agent_tool_audit
    ADD CONSTRAINT social_agent_tool_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_agent_turns
    ADD CONSTRAINT social_agent_turns_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_benchmarks
    ADD CONSTRAINT social_benchmarks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_interactions
    ADD CONSTRAINT social_interactions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_lifecycle_events
    ADD CONSTRAINT social_lifecycle_events_event_id_key UNIQUE (event_id);

ALTER TABLE ONLY public.social_lifecycle_events
    ADD CONSTRAINT social_lifecycle_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_materials
    ADD CONSTRAINT social_materials_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_personas
    ADD CONSTRAINT social_personas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_personas
    ADD CONSTRAINT social_personas_project_id_key UNIQUE (project_id);

ALTER TABLE ONLY public.social_projects
    ADD CONSTRAINT social_projects_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_publication_metrics
    ADD CONSTRAINT social_publication_metrics_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_publications
    ADD CONSTRAINT social_publications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_script_versions
    ADD CONSTRAINT social_script_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_scripts
    ADD CONSTRAINT social_scripts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.social_topics
    ADD CONSTRAINT social_topics_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.subscription_addon_purchases
    ADD CONSTRAINT subscription_addon_purchases_order_id_key UNIQUE (order_id);

ALTER TABLE ONLY public.subscription_addon_purchases
    ADD CONSTRAINT subscription_addon_purchases_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.subscription_addons
    ADD CONSTRAINT subscription_addons_addon_id_key UNIQUE (addon_id);

ALTER TABLE ONLY public.subscription_addons
    ADD CONSTRAINT subscription_addons_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.topic_embeddings
    ADD CONSTRAINT topic_embeddings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.topic_embeddings
    ADD CONSTRAINT topic_embeddings_topic_id_key UNIQUE (topic_id);

ALTER TABLE ONLY public.service_fee_records
    ADD CONSTRAINT uniq_service_fee_order_type_user UNIQUE (source_order_id, source_order_type, user_id);

ALTER TABLE ONLY public.geo_query_intent
    ADD CONSTRAINT uq_geo_query_intent_hash UNIQUE (query_hash);

ALTER TABLE ONLY public.writing_article_version_fingerprints
    ADD CONSTRAINT uq_writing_fingerprint_article UNIQUE (article_id);

ALTER TABLE ONLY public.user_module_overrides
    ADD CONSTRAINT user_module_overrides_pkey PRIMARY KEY (user_id, module_name);

ALTER TABLE ONLY public.user_social_preferences
    ADD CONSTRAINT user_social_preferences_pkey PRIMARY KEY (user_id);

ALTER TABLE ONLY public.user_tag_assignments
    ADD CONSTRAINT user_tag_assignments_pkey PRIMARY KEY (user_id, tag_id);

ALTER TABLE ONLY public.user_tags
    ADD CONSTRAINT user_tags_name_key UNIQUE (name);

ALTER TABLE ONLY public.user_tags
    ADD CONSTRAINT user_tags_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.viral_frameworks
    ADD CONSTRAINT viral_frameworks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.work_results
    ADD CONSTRAINT work_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.writing_article_version_fingerprints
    ADD CONSTRAINT writing_article_version_fingerprints_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.writing_corpus_export_audit
    ADD CONSTRAINT writing_corpus_export_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.writing_style_simulations
    ADD CONSTRAINT writing_style_simulations_pkey PRIMARY KEY (id);

CREATE INDEX idx_activity_log_action ON public.activity_log USING btree (action_type);

CREATE INDEX idx_activity_log_created ON public.activity_log USING btree (created_at);

CREATE INDEX idx_activity_log_team ON public.activity_log USING btree (team_id);

CREATE INDEX idx_activity_log_team_created ON public.activity_log USING btree (team_id, created_at);

CREATE INDEX idx_activity_log_user ON public.activity_log USING btree (user_id);

CREATE INDEX idx_addon_purchases_addon_id ON public.subscription_addon_purchases USING btree (addon_id);

CREATE INDEX idx_addon_purchases_user_month ON public.subscription_addon_purchases USING btree (user_id, created_at);

CREATE INDEX idx_addons_active_sort ON public.subscription_addons USING btree (is_active, sort_order);

CREATE INDEX idx_addons_target_quota ON public.subscription_addons USING btree (target_quota);

CREATE INDEX idx_advisor_conv_owner ON public.advisor_conversations USING btree (owner_user_id);

CREATE INDEX idx_agent_sessions_user ON public.agent_sessions USING btree (user_id, updated_at DESC);

CREATE INDEX idx_answer_entities_engine ON public.geo_research_answer_entities USING btree (engine);

CREATE INDEX idx_answer_entities_fact ON public.geo_research_answer_entities USING btree (answer_fact_id);

CREATE INDEX idx_answer_entities_industry ON public.geo_research_answer_entities USING btree (industry_key, entity_key);

CREATE INDEX idx_answer_entities_key ON public.geo_research_answer_entities USING btree (entity_key);

CREATE INDEX idx_answer_facts_batch ON public.geo_research_answer_facts USING btree (batch_id);

CREATE INDEX idx_answer_facts_engine ON public.geo_research_answer_facts USING btree (engine);

CREATE INDEX idx_answer_facts_industry ON public.geo_research_answer_facts USING btree (industry_key, created_at DESC);

CREATE INDEX idx_ba_alias ON public.brand_aliases USING btree (alias);

CREATE INDEX idx_ba_canonical ON public.brand_aliases USING btree (canonical_name);

CREATE INDEX idx_bonus_grants_expiry_scan ON public.bonus_grants USING btree (expires_at) WHERE (status = 'active'::text);

CREATE INDEX idx_bonus_grants_owner_fifo ON public.bonus_grants USING btree (owner_type, owner_id, expires_at) WHERE (status = 'active'::text);

CREATE INDEX idx_brief_history_profile ON public.industry_brief_history USING btree (profile_id, version_num DESC);

CREATE INDEX idx_ccf_customer_status ON public.customer_credit_freezes USING btree (customer_user_id, status);

CREATE INDEX idx_ccf_status_created ON public.customer_credit_freezes USING btree (status, created_at);

CREATE INDEX idx_ccf_task_ref ON public.customer_credit_freezes USING btree (task_ref) WHERE (task_ref IS NOT NULL);

CREATE INDEX idx_channel_tier_state_tier ON public.agent_channel_tier_state USING btree (channel_tier);

CREATE INDEX idx_clawback_pending_user ON public.bonus_clawback_pending USING btree (user_id, status);

CREATE INDEX idx_content_plans_brand_month ON public.content_plans USING btree (brand_id, month) WHERE (brand_id IS NOT NULL);

CREATE INDEX idx_content_plans_profile_month ON public.content_plans USING btree (profile_id, month);

CREATE INDEX idx_conversations_advisor ON public.advisor_conversations USING btree (advisor_id);

CREATE INDEX idx_conversion_review ON public.service_fee_conversion_orders USING btree (review_status) WHERE ((review_status)::text = 'pending'::text);

CREATE INDEX idx_conversion_user ON public.service_fee_conversion_orders USING btree (user_id, created_at);

CREATE INDEX idx_corpus_asr ON public.profile_corpus USING btree (asr_status);

CREATE INDEX idx_corpus_export_audit_day ON public.writing_corpus_export_audit USING btree (exported_at);

CREATE INDEX idx_corpus_profile ON public.profile_corpus USING btree (profile_id);

CREATE INDEX idx_corpus_team ON public.profile_corpus USING btree (team_id);

CREATE INDEX idx_corpus_user ON public.profile_corpus USING btree (user_id);

CREATE INDEX idx_draft_workspace_user_brand ON public.draft_workspace USING btree (user_id, brand_id, item_type);

CREATE INDEX idx_failed_sf_jobs_status ON public.failed_service_fee_jobs USING btree (status, retry_count);

CREATE INDEX idx_failed_sf_jobs_user ON public.failed_service_fee_jobs USING btree (user_id);

CREATE INDEX idx_flywheel_judgment_point_time ON public.flywheel_judgment_log USING btree (point_key, created_at DESC);

CREATE INDEX idx_geo_answer_metrics_domain ON public.geo_answer_adoption_metrics USING btree (domain);

CREATE INDEX idx_geo_answer_metrics_industry ON public.geo_answer_adoption_metrics USING btree (industry_key, normalized_credit DESC);

CREATE INDEX idx_geo_answer_metrics_tier ON public.geo_answer_adoption_metrics USING btree (signal_tier);

CREATE INDEX idx_geo_query_intent_industry ON public.geo_query_intent USING btree (industry);

CREATE INDEX idx_geo_query_intent_intent ON public.geo_query_intent USING btree (query_intent);

CREATE INDEX idx_gewc_status ON public.geo_engine_weight_candidates USING btree (status, created_at DESC);

CREATE INDEX idx_ik_expires ON public.industry_knowledge USING btree (expires_at);

CREATE INDEX idx_ik_lookup ON public.industry_knowledge USING btree (level, industry, category);

CREATE INDEX idx_interview_user_active ON public.interview_sessions USING btree (user_id, is_complete);

CREATE INDEX idx_invite_codes_active ON public.invite_codes USING btree (is_active, expires_at) WHERE (is_active = true);

CREATE INDEX idx_invite_codes_type ON public.invite_codes USING btree (code_type);

CREATE INDEX idx_invite_codes_used ON public.invite_codes USING btree (used_by_user_id) WHERE (used_by_user_id IS NOT NULL);

CREATE INDEX idx_invite_codes_user ON public.invite_codes USING btree (user_id);

CREATE INDEX idx_ki_brand_id ON public.keyword_insights USING btree (brand_id);

CREATE INDEX idx_ki_distilled_at ON public.keyword_insights USING btree (distilled_at);

CREATE INDEX idx_ki_keyword ON public.keyword_insights USING btree (keyword);

CREATE INDEX idx_ki_quality ON public.keyword_insights USING btree (quality_flag);

CREATE INDEX idx_ki_task_id ON public.keyword_insights USING btree (task_id);

CREATE INDEX idx_m3_events_brand_ts ON public.m3_customer_events USING btree (brand_id, occurred_at DESC) WHERE (brand_id IS NOT NULL);

CREATE INDEX idx_m3_events_diag_ts ON public.m3_customer_events USING btree (diagnosis_id, occurred_at DESC) WHERE (diagnosis_id IS NOT NULL);

CREATE INDEX idx_m3_events_quote_ts ON public.m3_customer_events USING btree (quote_id, occurred_at DESC) WHERE (quote_id IS NOT NULL);

CREATE INDEX idx_m3_events_source_type_ts ON public.m3_customer_events USING btree (source, event_type, occurred_at DESC);

CREATE INDEX idx_m3_events_token_ts ON public.m3_customer_events USING btree (token_hash, occurred_at DESC) WHERE (token_hash IS NOT NULL);

CREATE INDEX idx_messages_conversation ON public.advisor_messages USING btree (conversation_id);

CREATE INDEX idx_opex_period ON public.operating_expenses USING btree (period_month);

CREATE INDEX idx_pending_bonus_order ON public.pending_bonus_records USING btree (recharge_order_id);

CREATE INDEX idx_pending_bonus_referrer ON public.pending_bonus_records USING btree (referrer_id);

CREATE INDEX idx_pending_bonus_settle ON public.pending_bonus_records USING btree (status, settle_at);

CREATE INDEX idx_pipeline_stage_log_brand_stage ON public.pipeline_stage_log USING btree (brand_id, stage_name, event, created_at DESC);

CREATE INDEX idx_pipeline_stage_log_created ON public.pipeline_stage_log USING btree (created_at DESC);

CREATE INDEX idx_plan_task_script_versions_task ON public.plan_task_script_versions USING btree (task_id, created_at DESC);

CREATE INDEX idx_plan_tasks_plan_id ON public.plan_tasks USING btree (plan_id);

CREATE INDEX idx_plan_tasks_script_id ON public.plan_tasks USING btree (script_id);

CREATE INDEX idx_plan_tasks_topic_id ON public.plan_tasks USING btree (topic_id) WHERE (topic_id IS NOT NULL);

CREATE INDEX idx_profile_memory_active ON public.profile_memory_events USING btree (profile_id, is_active, created_at DESC);

CREATE INDEX idx_profile_memory_concept ON public.profile_memory_events USING btree (profile_id, canonical_concept, review_status, created_at DESC);

CREATE INDEX idx_profile_memory_pending ON public.profile_memory_events USING btree (profile_id, review_status, is_active, created_at DESC, id DESC);

CREATE INDEX idx_profile_memory_profile_time ON public.profile_memory_events USING btree (profile_id, created_at DESC);

CREATE INDEX idx_profile_memory_source ON public.profile_memory_events USING btree (source, event_type, created_at DESC);

CREATE INDEX idx_profile_memory_weighted ON public.profile_memory_events USING btree (profile_id, canonical_concept, review_status, is_active, importance_score DESC, access_count DESC, created_at DESC);

CREATE INDEX idx_quota_user ON public.service_fee_conversion_quota USING btree (user_id);

CREATE INDEX idx_rbac_audit_blocked ON public.rbac_route_audit USING btree (blocked_reason) WHERE (blocked_reason IS NOT NULL);

CREATE INDEX idx_rbac_audit_user_path ON public.rbac_route_audit USING btree (user_id, path, created_at);

CREATE INDEX idx_redemption_agent ON public.agent_commission_redemption_requests USING btree (agent_user_id, status);

CREATE INDEX idx_redemption_items_request ON public.agent_commission_redemption_items USING btree (redemption_request_id);

CREATE INDEX idx_reference_articles_source_article ON public.reference_articles USING btree (source_article_id) WHERE (source_article_id IS NOT NULL);

CREATE INDEX idx_service_fee_review ON public.service_fee_records USING btree (review_status) WHERE ((review_status)::text = 'pending'::text);

CREATE INDEX idx_service_fee_source_user ON public.service_fee_records USING btree (source_user_id);

CREATE INDEX idx_service_fee_status ON public.service_fee_records USING btree (status, available_at);

CREATE INDEX idx_service_fee_user ON public.service_fee_records USING btree (user_id, status);

CREATE INDEX idx_settlements_status ON public.service_fee_settlements USING btree (status, created_at);

CREATE INDEX idx_settlements_user ON public.service_fee_settlements USING btree (user_id, status);

CREATE INDEX idx_sfclawback_user ON public.service_fee_clawback_pending USING btree (user_id, status);

CREATE INDEX idx_social_agent_metrics_created ON public.social_agent_metrics USING btree (created_at DESC);

CREATE INDEX idx_social_agent_metrics_profile_created ON public.social_agent_metrics USING btree (profile_id, created_at DESC);

CREATE INDEX idx_social_agent_metrics_turn ON public.social_agent_metrics USING btree (turn_id);

CREATE UNIQUE INDEX idx_social_agent_plans_one_active ON public.social_agent_plans USING btree (user_id, profile_id) WHERE ((status)::text = 'active'::text);

CREATE INDEX idx_social_agent_plans_profile_status ON public.social_agent_plans USING btree (profile_id, status, updated_at DESC);

CREATE INDEX idx_social_agent_sessions_profile_status ON public.social_agent_sessions USING btree (profile_id, status, last_active_at DESC);

CREATE INDEX idx_social_agent_sessions_user_active ON public.social_agent_sessions USING btree (user_id, last_active_at DESC);

CREATE INDEX idx_social_agent_tool_audit_billing_status ON public.social_agent_tool_audit USING btree (billing_status, created_at DESC);

CREATE INDEX idx_social_agent_tool_audit_profile ON public.social_agent_tool_audit USING btree (profile_id, created_at DESC);

CREATE INDEX idx_social_agent_tool_audit_tool_status ON public.social_agent_tool_audit USING btree (tool_name, status, created_at DESC);

CREATE INDEX idx_social_agent_tool_audit_turn ON public.social_agent_tool_audit USING btree (turn_id, created_at DESC);

CREATE INDEX idx_social_agent_turns_profile_created ON public.social_agent_turns USING btree (profile_id, created_at DESC);

CREATE INDEX idx_social_agent_turns_session_created ON public.social_agent_turns USING btree (session_id, created_at, id);

CREATE INDEX idx_social_agent_turns_turn ON public.social_agent_turns USING btree (turn_id);

CREATE INDEX idx_social_interactions_publication ON public.social_interactions USING btree (publication_id) WHERE (publication_id IS NOT NULL);

CREATE INDEX idx_social_lifecycle_profile_time ON public.social_lifecycle_events USING btree (profile_id, created_at DESC);

CREATE INDEX idx_social_lifecycle_script ON public.social_lifecycle_events USING btree (script_id) WHERE (script_id IS NOT NULL);

CREATE INDEX idx_social_lifecycle_task ON public.social_lifecycle_events USING btree (plan_task_id) WHERE (plan_task_id IS NOT NULL);

CREATE INDEX idx_social_materials_brand ON public.social_materials USING btree (brand_id) WHERE (brand_id IS NOT NULL);

CREATE INDEX idx_social_metrics_publication_time ON public.social_publication_metrics USING btree (publication_id, recorded_at DESC);

CREATE INDEX idx_social_projects_brand ON public.social_projects USING btree (brand_id) WHERE (brand_id IS NOT NULL);

CREATE INDEX idx_social_projects_profile ON public.social_projects USING btree (profile_id) WHERE (profile_id IS NOT NULL);

CREATE INDEX idx_social_publications_script ON public.social_publications USING btree (script_id) WHERE (script_id IS NOT NULL);

CREATE INDEX idx_social_publications_task ON public.social_publications USING btree (plan_task_id) WHERE (plan_task_id IS NOT NULL);

CREATE INDEX idx_social_scripts_brand ON public.social_scripts USING btree (brand_id) WHERE (brand_id IS NOT NULL);

CREATE INDEX idx_social_topics_brand ON public.social_topics USING btree (brand_id) WHERE (brand_id IS NOT NULL);

CREATE UNIQUE INDEX idx_social_topics_project_title_hash ON public.social_topics USING btree (project_id, title_hash);

CREATE INDEX idx_style_profile ON public.profile_style USING btree (profile_id);

CREATE INDEX idx_suggestions_created ON public.ai_suggestions USING btree (created_at);

CREATE INDEX idx_suggestions_target ON public.ai_suggestions USING btree (target_type, target_id);

CREATE INDEX idx_suggestions_type ON public.ai_suggestions USING btree (suggestion_type);

CREATE INDEX idx_tag_assignments_tag ON public.user_tag_assignments USING btree (tag_id);

CREATE INDEX idx_tier_change_agent ON public.agent_tier_change_log USING btree (agent_user_id, created_at DESC);

CREATE INDEX idx_topic_emb_project ON public.topic_embeddings USING btree (project_id);

CREATE INDEX idx_topics_project_hash ON public.social_topics USING btree (project_id, title_hash);

CREATE INDEX idx_topics_project_status ON public.social_topics USING btree (project_id, status);

CREATE INDEX idx_writing_fingerprint_sha ON public.writing_article_version_fingerprints USING btree (prompt_sha256);

CREATE INDEX idx_writing_fingerprint_style ON public.writing_article_version_fingerprints USING btree (style_code, industry_key, created_at);

CREATE INDEX idx_writing_sim_style ON public.writing_style_simulations USING btree (style_code, industry_key, created_at DESC);

CREATE INDEX idx_writing_sim_version ON public.writing_style_simulations USING btree (version_id);

CREATE UNIQUE INDEX uniq_pending_bonus_order_referrer ON public.pending_bonus_records USING btree (recharge_order_id, referrer_id) WHERE (recharge_order_id IS NOT NULL);

CREATE UNIQUE INDEX uniq_profile_memory_event_fingerprint ON public.profile_memory_events USING btree (profile_id, source, event_type, canonical_concept, event_fingerprint) WHERE (event_fingerprint IS NOT NULL);

CREATE UNIQUE INDEX uniq_settlements_one_pending_withdrawal_per_user ON public.service_fee_settlements USING btree (user_id) WHERE (((settlement_type)::text = 'withdrawal'::text) AND ((status)::text = 'pending'::text));

CREATE UNIQUE INDEX uq_bonus_grants_key ON public.bonus_grants USING btree (grant_key);

CREATE UNIQUE INDEX uq_ccf_active_task ON public.customer_credit_freezes USING btree (customer_user_id, task_ref) WHERE ((task_ref IS NOT NULL) AND (status = 'frozen'::text));

CREATE UNIQUE INDEX uq_m3_events_event_key ON public.m3_customer_events USING btree (event_key) WHERE (event_key IS NOT NULL);

CREATE UNIQUE INDEX uq_tier_change_idem ON public.agent_tier_change_log USING btree (idempotency_key);

CREATE UNIQUE INDEX ux_gewc_open ON public.geo_engine_weight_candidates USING btree (industry, engine) WHERE ((status)::text = 'candidate'::text);

ALTER TABLE ONLY public.activity_log
    ADD CONSTRAINT activity_log_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.activity_log
    ADD CONSTRAINT activity_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.advisor_messages
    ADD CONSTRAINT advisor_messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.advisor_conversations(conversation_id);

ALTER TABLE ONLY public.agent_commission_redemption_items
    ADD CONSTRAINT agent_commission_redemption_items_ledger_id_fkey FOREIGN KEY (ledger_id) REFERENCES public.agent_revenue_ledger(id);

ALTER TABLE ONLY public.agent_commission_redemption_items
    ADD CONSTRAINT agent_commission_redemption_items_redemption_request_id_fkey FOREIGN KEY (redemption_request_id) REFERENCES public.agent_commission_redemption_requests(id);

ALTER TABLE ONLY public.bonus_grants
    ADD CONSTRAINT bonus_grants_parent_grant_id_fkey FOREIGN KEY (parent_grant_id) REFERENCES public.bonus_grants(id);

ALTER TABLE ONLY public.customer_credit_freezes
    ADD CONSTRAINT customer_credit_freezes_agent_user_id_fkey FOREIGN KEY (agent_user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.customer_credit_freezes
    ADD CONSTRAINT customer_credit_freezes_customer_user_id_fkey FOREIGN KEY (customer_user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.interview_sessions
    ADD CONSTRAINT fk_interview_user FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.social_projects
    ADD CONSTRAINT fk_sp_brand FOREIGN KEY (brand_id) REFERENCES public.brands(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.social_scripts
    ADD CONSTRAINT fk_ss_project FOREIGN KEY (project_id) REFERENCES public.social_projects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.imitated_articles
    ADD CONSTRAINT imitated_articles_imitation_record_id_fkey FOREIGN KEY (imitation_record_id) REFERENCES public.imitation_records(id);

ALTER TABLE ONLY public.imitated_articles
    ADD CONSTRAINT imitated_articles_reference_id_fkey FOREIGN KEY (reference_id) REFERENCES public.reference_articles(id);

ALTER TABLE ONLY public.imitation_records
    ADD CONSTRAINT imitation_records_reference_id_fkey FOREIGN KEY (reference_id) REFERENCES public.reference_articles(id);

ALTER TABLE ONLY public.marketing_confirm_sessions
    ADD CONSTRAINT marketing_confirm_sessions_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES public.brands(id);

ALTER TABLE ONLY public.plan_task_script_versions
    ADD CONSTRAINT plan_task_script_versions_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.plan_tasks(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.plan_tasks
    ADD CONSTRAINT plan_tasks_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.content_plans(id);

ALTER TABLE ONLY public.profile_corpus
    ADD CONSTRAINT profile_corpus_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.profile_corpus
    ADD CONSTRAINT profile_corpus_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.profile_style
    ADD CONSTRAINT profile_style_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);

ALTER TABLE ONLY public.social_agent_turns
    ADD CONSTRAINT social_agent_turns_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.social_agent_sessions(session_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.social_benchmarks
    ADD CONSTRAINT social_benchmarks_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.social_projects(id);

ALTER TABLE ONLY public.social_interactions
    ADD CONSTRAINT social_interactions_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES public.social_publications(id);

ALTER TABLE ONLY public.social_materials
    ADD CONSTRAINT social_materials_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.social_projects(id);

ALTER TABLE ONLY public.social_personas
    ADD CONSTRAINT social_personas_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.social_projects(id);

ALTER TABLE ONLY public.social_projects
    ADD CONSTRAINT social_projects_brand_id_fkey FOREIGN KEY (brand_id) REFERENCES public.brands(id);

ALTER TABLE ONLY public.social_projects
    ADD CONSTRAINT social_projects_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.client_profiles(id);

ALTER TABLE ONLY public.social_publication_metrics
    ADD CONSTRAINT social_publication_metrics_publication_id_fkey FOREIGN KEY (publication_id) REFERENCES public.social_publications(id);

ALTER TABLE ONLY public.social_script_versions
    ADD CONSTRAINT social_script_versions_script_id_fkey FOREIGN KEY (script_id) REFERENCES public.social_scripts(id);

ALTER TABLE ONLY public.social_scripts
    ADD CONSTRAINT social_scripts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.social_projects(id);

ALTER TABLE ONLY public.social_scripts
    ADD CONSTRAINT social_scripts_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.social_topics(id);

ALTER TABLE ONLY public.social_scripts
    ADD CONSTRAINT social_scripts_topic_id_fkey1 FOREIGN KEY (topic_id) REFERENCES public.social_topics(id);

ALTER TABLE ONLY public.social_topics
    ADD CONSTRAINT social_topics_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.social_projects(id);

ALTER TABLE ONLY public.topic_embeddings
    ADD CONSTRAINT topic_embeddings_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.social_topics(id);

ALTER TABLE ONLY public.user_module_overrides
    ADD CONSTRAINT user_module_overrides_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_tag_assignments
    ADD CONSTRAINT user_tag_assignments_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.user_tags(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_tag_assignments
    ADD CONSTRAINT user_tag_assignments_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.viral_frameworks
    ADD CONSTRAINT viral_frameworks_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.social_projects(id);

-- ── 缺列 / 类型对齐 ──
ALTER TABLE public.advisors ADD COLUMN IF NOT EXISTS "hot_score" integer DEFAULT 0;
ALTER TABLE public.agent_settlement_requests ADD COLUMN IF NOT EXISTS "gateway_fee_cents" integer DEFAULT 0;
ALTER TABLE public.agent_settlement_requests ADD COLUMN IF NOT EXISTS "settlement_fee_cents" integer DEFAULT 0;
ALTER TABLE public.agent_settlement_requests ADD COLUMN IF NOT EXISTS "tax_cents" integer DEFAULT 0;
ALTER TABLE public.agent_settlement_requests ADD COLUMN IF NOT EXISTS "net_amount_cents" integer DEFAULT 0;
ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS "status" text DEFAULT 'draft'::text;
ALTER TABLE public.brands ADD COLUMN IF NOT EXISTS "user_id" integer;
ALTER TABLE public.client_materials ADD COLUMN IF NOT EXISTS "owner_user_id" integer;
ALTER TABLE public.client_materials ADD COLUMN IF NOT EXISTS "is_deleted" boolean DEFAULT false;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "company_intro" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "core_value" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "selling_points" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "success_cases" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "testimonials" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "story_type" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "differentiation" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "content_direction" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "business_summary" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "business_misunderstandings" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "knowledge_files" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "brand_display_names" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "visual_style" text DEFAULT ''::text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "content_pillars" text DEFAULT '[]'::text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "structured_knowledge" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "personality_profile" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "creator_type" character varying(50);
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "content_frequency" character varying(50);
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "monetization_goal" character varying(50);
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "follower_count" character varying(50);
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "profile_completeness" jsonb DEFAULT '{}'::jsonb;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "preferred_advisor_id" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "plan_mode" character varying(20) DEFAULT 'inspiration'::character varying;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "admin_insight" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "admin_insight_level" character varying(20);
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "admin_insight_updated_at" timestamp without time zone;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "share_code" character varying(20);
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "category" character varying(100);
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "industry_brief" jsonb;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "industry_brief_status" character varying(20);
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "industry_brief_started_at" timestamp without time zone;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "preferred_expert_id" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "industry_brief_confirmed" boolean DEFAULT false;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "industry_brief_confirmed_at" timestamp without time zone;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "industry_brief_partial_fields" jsonb;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "industry_brief_edits" jsonb;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "industry_brief_version" integer DEFAULT 0;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "service_scope" character varying(20);
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "service_scope_reasoning" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "local_competitors" jsonb DEFAULT '[]'::jsonb;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "brief_applied_version" integer DEFAULT 0;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "brief_applied_at" timestamp without time zone;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "business_type" character varying(20) DEFAULT 'B2C'::character varying;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "city_scope" character varying(20) DEFAULT 'local'::character varying;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "contact_phone" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "contact_wechat" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "contact_website" text;
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS "contact_address" text;
ALTER TABLE public.confirmed_keywords ADD COLUMN IF NOT EXISTS "selling_price" real DEFAULT 0;
ALTER TABLE public.confirmed_keywords ADD COLUMN IF NOT EXISTS "brand_id" integer;
ALTER TABLE public.departments ALTER COLUMN "is_active" DROP DEFAULT;
ALTER TABLE public.departments ALTER COLUMN "is_active" TYPE smallint USING ("is_active"::integer);
ALTER TABLE public.departments ALTER COLUMN "is_active" SET DEFAULT 1;
ALTER TABLE public.diagnosis_records ADD COLUMN IF NOT EXISTS "distilled_data" text;
ALTER TABLE public.diagnosis_records ADD COLUMN IF NOT EXISTS "operator_user_id" integer;
ALTER TABLE public.employee_configs ALTER COLUMN "is_active" DROP DEFAULT;
ALTER TABLE public.employee_configs ALTER COLUMN "is_active" TYPE smallint USING ("is_active"::integer);
ALTER TABLE public.employee_configs ALTER COLUMN "is_active" SET DEFAULT 1;
ALTER TABLE public.employee_meetings ALTER COLUMN "is_internal" DROP DEFAULT;
ALTER TABLE public.employee_meetings ALTER COLUMN "is_internal" TYPE smallint USING ("is_internal"::integer);
ALTER TABLE public.employee_meetings ALTER COLUMN "is_internal" SET DEFAULT 0;
ALTER TABLE public.geo_plan_tasks ADD COLUMN IF NOT EXISTS "freeze_table" text;
ALTER TABLE public.keyword_compliance_log ADD COLUMN IF NOT EXISTS "is_stable" boolean;
ALTER TABLE public.managed_campaigns ADD COLUMN IF NOT EXISTS "last_tick_claimed_at" timestamp without time zone;
ALTER TABLE public.pending_review_articles ADD COLUMN IF NOT EXISTS "autopublish_claimed_at" timestamp without time zone;
ALTER TABLE public.point_transactions ADD COLUMN IF NOT EXISTS "source" character varying(40);
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS "competitor_mode" text DEFAULT 'fictional'::text;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS "competitor_list" text DEFAULT ''::text;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS "monitoring_enabled" boolean DEFAULT false;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS "monitoring_frequency" integer DEFAULT 1;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS "monitoring_start_hour" integer DEFAULT 8;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS "monitoring_started_at" timestamp without time zone;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS "monitoring_paused_reason" character varying(200);
ALTER TABLE public.recharge_orders ADD COLUMN IF NOT EXISTS "source" character varying(40) DEFAULT 'external_cash_payment'::character varying;
ALTER TABLE public.refund_work_orders ADD COLUMN IF NOT EXISTS "agent_signoff_confirmed" boolean DEFAULT false;
ALTER TABLE public.refund_work_orders ADD COLUMN IF NOT EXISTS "agent_signoff_note" text DEFAULT ''::text;
ALTER TABLE public.refund_work_orders ADD COLUMN IF NOT EXISTS "agent_signoff_by" integer;
ALTER TABLE public.refund_work_orders ADD COLUMN IF NOT EXISTS "agent_signoff_at" timestamp without time zone;
ALTER TABLE public.topics ADD COLUMN IF NOT EXISTS "regenerate_count" integer DEFAULT 0;
ALTER TABLE public.user_wallets ADD COLUMN IF NOT EXISTS "is_kyc_passed" boolean DEFAULT false;
ALTER TABLE public.user_wallets ADD COLUMN IF NOT EXISTS "bank_account_verified" boolean DEFAULT false;
ALTER TABLE public.user_wallets ADD COLUMN IF NOT EXISTS "agent_tier" character varying(20) DEFAULT 'standard'::character varying;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "extension_authorized" boolean DEFAULT false;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "extension_authorized_at" timestamp without time zone;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "extension_authorized_by" integer;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "real_name" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "wechat_id" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "company" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "job_title" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "industry" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "city" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "bio" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "register_city" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "register_province" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "register_source" text DEFAULT 'direct'::text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "profile_completed_at" timestamp without time zone;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "gender" text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "agent_sku_markup_ratio" numeric(4,2);
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS "customer_referral_reward_points" integer DEFAULT 0;
ALTER TABLE public.writing_strategy_assignments ADD COLUMN IF NOT EXISTS "strategy_version" character varying(240);
ALTER TABLE public.writing_strategy_assignments ADD COLUMN IF NOT EXISTS "style_family" character varying(60);
ALTER TABLE public.writing_strategy_assignments ADD COLUMN IF NOT EXISTS "resolution" character varying(32) DEFAULT 'industry_baseline'::character varying;
ALTER TABLE public.writing_strategy_assignments ADD COLUMN IF NOT EXISTS "metadata" jsonb DEFAULT '{}'::jsonb;
