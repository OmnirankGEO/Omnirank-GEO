--    只核 information_schema.data_type 的守卫对「错长度」零判别(varchar(64) 与
--    varchar(255) 的 data_type 都是 character varying),对「错 default」更是零核验。
-- 🔴 仍然零 DML:只 SELECT + RAISE。
DO $readiness040$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('chk_defgeo_qplan_counts', 'public.defgeo_question_plans', 'CHECK (((defensive_count >= 0) AND (offensive_count >= 0) AND (total_count = (defensive_count + offensive_count))))'),
        ('chk_defgeo_qplan_mode', 'public.defgeo_question_plans', 'CHECK (((mode)::text = ANY ((ARRAY[''defensive''::character varying, ''offensive''::character varying, ''hybrid''::character varying])::text[])))'),
        ('chk_defgeo_qplan_revision_positive', 'public.defgeo_question_plans', 'CHECK ((plan_revision >= 1))'),
        ('uq_defgeo_qplan_idempotency', 'public.defgeo_question_plans', 'UNIQUE (tenant_owner_user_id, client_request_id, request_content_hash)'),
        ('uq_defgeo_qplan_revision', 'public.defgeo_question_plans', 'UNIQUE (plan_id, plan_revision)')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[040] 约束的目标表 % 不存在', r.tname;
        END IF;
        SELECT pg_get_constraintdef(oid), convalidated INTO actual, ok
          FROM pg_constraint WHERE conname = r.cname AND conrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION '[040] 约束 % 不在 % 上(可能被同名约束挡在了别的表)',
                r.cname, r.tname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[040] 约束 % 定义漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[040] 约束 % 是 NOT VALID —— 存量行从没被验过,守卫只守未来', r.cname;
        END IF;
    END LOOP;
