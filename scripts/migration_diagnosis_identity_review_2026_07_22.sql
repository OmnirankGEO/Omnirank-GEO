-- Diagnosis brand-cell human review · D6 additive generalization (2026-07-22).
--
-- Review-CTO D6 裁决:诊断人工确认复用已部署的监测人工确认 SSOT/decision/audit/别名体系,
-- 禁止第二套诊断别名真相表。monitoring_identity_decision_events 强绑 monitoring_result_id,
-- 因此只做 additive 泛化:
--   · source_kind       'monitoring'(默认,旧行)/'diagnosis'
--   · source_result_id  诊断侧事件存 diagnosis_records.id(不设 FK:诊断记录可被既有流程删除,
--                       审计事件必须存活; brand_id FK 仍是租户锚点)
--   · tenant_id         决策时 brands.owner_user_id 的反规范化快照(跨租户审计便利列;
--                       brand_id 仍是隔离主键;旧监测行 NULL=legacy)
--   · ip / reason       审计补强(决策来源 IP + 操作员填写的理由;旧监测行 NULL=legacy)
--   · result_id         DROP NOT NULL:诊断事件没有 monitoring_results 行
--
-- monitoring_identity_name_decisions 不强绑 monitoring_result_id(主键 brand_id+normalized_name),
-- 它是跨 surface 共享的别名真相表,按 D6 保持原样不动。
--
-- Additive & idempotent:全部 ADD COLUMN IF NOT EXISTS + 约束存在性检查,可连跑两次;
-- 尾部 DO 块做精确形状核验,半成品对象 fail-closed(RAISE)。
SET LOCAL search_path = pg_catalog, public;

ALTER TABLE public.monitoring_identity_decision_events
    ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'monitoring',
    ADD COLUMN IF NOT EXISTS source_result_id BIGINT,
    ADD COLUMN IF NOT EXISTS tenant_id BIGINT,
    ADD COLUMN IF NOT EXISTS ip TEXT,
    ADD COLUMN IF NOT EXISTS reason TEXT;

-- Widening only: existing monitoring rows all carry result_id; the new CHECK
-- below keeps result_id mandatory for source_kind='monitoring'.
ALTER TABLE public.monitoring_identity_decision_events
    ALTER COLUMN result_id DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conrelid = 'public.monitoring_identity_decision_events'::pg_catalog.regclass
           AND conname = 'chk_monitoring_identity_event_source_kind'
    ) THEN
        ALTER TABLE public.monitoring_identity_decision_events
            ADD CONSTRAINT chk_monitoring_identity_event_source_kind
            CHECK (source_kind = ANY (ARRAY['monitoring'::text, 'diagnosis'::text]));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conrelid = 'public.monitoring_identity_decision_events'::pg_catalog.regclass
           AND conname = 'chk_monitoring_identity_event_source'
    ) THEN
        ALTER TABLE public.monitoring_identity_decision_events
            ADD CONSTRAINT chk_monitoring_identity_event_source
            CHECK (
                (source_kind = 'monitoring'::text AND result_id IS NOT NULL)
                OR (source_kind = 'diagnosis'::text AND source_result_id IS NOT NULL)
            );
    END IF;
END $$;

-- Fail-closed contract verification: the generalized events table must have
-- exactly this shape; anything else means a half-applied or drifted object.
DO $$
DECLARE
    event_oid OID := 'public.monitoring_identity_decision_events'::pg_catalog.regclass;
    mismatch_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO mismatch_count
      FROM pg_catalog.pg_class c
     WHERE c.oid = event_oid AND c.relkind = 'r' AND c.relpersistence = 'p';
    IF mismatch_count <> 1 THEN
        RAISE EXCEPTION 'monitoring identity decision events must be an ordinary permanent public table';
    END IF;

    SELECT COUNT(*) INTO mismatch_count
      FROM pg_catalog.pg_attribute a
     WHERE a.attrelid = event_oid AND a.attnum > 0 AND NOT a.attisdropped;
    IF mismatch_count <> 18 THEN
        RAISE EXCEPTION 'monitoring identity decision event generalization has unexpected column count (% != 18)', mismatch_count;
    END IF;

    SELECT COUNT(*) INTO mismatch_count
      FROM (VALUES
        ('event_id'::name, 'bigint'::text, TRUE, NULL::text, 'd'::text),
        ('result_id'::name, 'integer'::text, FALSE, NULL::text, ''::text),
        ('brand_id'::name, 'integer'::text, TRUE, NULL::text, ''::text),
        ('action'::name, 'character varying(16)'::text, TRUE, NULL::text, ''::text),
        ('selected_name'::name, 'text'::text, TRUE, NULL::text, ''::text),
        ('normalized_name'::name, 'text'::text, TRUE, NULL::text, ''::text),
        ('evidence_hash'::name, 'character(64)'::text, TRUE, NULL::text, ''::text),
        ('result_version_before'::name, 'bigint'::text, TRUE, NULL::text, ''::text),
        ('result_version_after'::name, 'bigint'::text, TRUE, NULL::text, ''::text),
        ('actor_user_id'::name, 'bigint'::text, TRUE, NULL::text, ''::text),
        ('request_id'::name, 'uuid'::text, TRUE, NULL::text, ''::text),
        ('metadata'::name, 'jsonb'::text, TRUE, '''{}''::jsonb'::text, ''::text),
        ('decided_at'::name, 'timestamp with time zone'::text, TRUE, 'now()'::text, ''::text),
        ('source_kind'::name, 'text'::text, TRUE, '''monitoring''::text'::text, ''::text),
        ('source_result_id'::name, 'bigint'::text, FALSE, NULL::text, ''::text),
        ('tenant_id'::name, 'bigint'::text, FALSE, NULL::text, ''::text),
        ('ip'::name, 'text'::text, FALSE, NULL::text, ''::text),
        ('reason'::name, 'text'::text, FALSE, NULL::text, ''::text)
      ) expected(attname, data_type, not_null, column_default, identity_kind)
      LEFT JOIN pg_catalog.pg_attribute a
        ON a.attrelid = event_oid AND a.attname = expected.attname AND NOT a.attisdropped
      LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE a.attname IS NULL
        OR pg_catalog.format_type(a.atttypid, a.atttypmod) IS DISTINCT FROM expected.data_type
        OR a.attnotnull IS DISTINCT FROM expected.not_null
        OR pg_catalog.pg_get_expr(d.adbin, d.adrelid) IS DISTINCT FROM expected.column_default
        OR a.attidentity IS DISTINCT FROM expected.identity_kind;
    IF mismatch_count <> 0 THEN
        RAISE EXCEPTION 'monitoring identity decision event generalized column contract drift';
    END IF;

    SELECT COUNT(*) INTO mismatch_count
      FROM (VALUES
        ('chk_monitoring_identity_event_source_kind'::name,
         'CHECK (source_kind = ANY (ARRAY[''monitoring''::text, ''diagnosis''::text]))'::text),
        -- PG16 canonical:pg_get_constraintdef(oid, TRUE) 按运算符优先级重脱水,
        -- AND 组不再保留书写时的括号(16.14 实证);与 db/monitoring_db.py
        -- exact_checks 的同一条目必须保持同一份 canonical 定义。
        ('chk_monitoring_identity_event_source'::name,
         'CHECK (source_kind = ''monitoring''::text AND result_id IS NOT NULL OR source_kind = ''diagnosis''::text AND source_result_id IS NOT NULL)'::text)
      ) expected(conname, definition)
      LEFT JOIN pg_catalog.pg_constraint c
        ON c.conrelid = event_oid AND c.conname = expected.conname
     WHERE c.oid IS NULL
        OR c.contype <> 'c'
        OR c.convalidated IS NOT TRUE
        OR c.condeferrable IS TRUE
        OR c.condeferred IS TRUE
        OR pg_catalog.pg_get_constraintdef(c.oid, TRUE) IS DISTINCT FROM expected.definition;
    IF mismatch_count <> 0 THEN
        RAISE EXCEPTION 'monitoring identity decision event generalization CHECK contract drift';
    END IF;

    -- The shared alias truth table must stay in its deployed 10-column shape
    -- (D6: no second diagnosis alias store, no result binding added here).
    SELECT COUNT(*) INTO mismatch_count
      FROM pg_catalog.pg_attribute a
     WHERE a.attrelid = 'public.monitoring_identity_name_decisions'::pg_catalog.regclass
       AND a.attnum > 0 AND NOT a.attisdropped;
    IF mismatch_count <> 10 THEN
        RAISE EXCEPTION 'monitoring identity name decisions must keep its 10-column shared shape';
    END IF;
END $$;
