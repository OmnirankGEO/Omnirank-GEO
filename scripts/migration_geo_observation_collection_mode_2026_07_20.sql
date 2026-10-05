-- GEO observation collection ownership mode.
-- Additive only: no feature flag is changed and no source/business row is touched.

ALTER TABLE geo_observation_policy
    ADD COLUMN IF NOT EXISTS collection_mode TEXT;

DO $$
DECLARE
    actual_type TEXT;
BEGIN
    SELECT data_type INTO actual_type
      FROM information_schema.columns
     WHERE table_schema='public' AND table_name='geo_observation_policy'
       AND column_name='collection_mode';
    IF actual_type IS DISTINCT FROM 'text' THEN
        RAISE EXCEPTION 'geo_observation_policy.collection_mode 类型错误: %', actual_type;
    END IF;
END $$;

UPDATE geo_observation_policy
   SET collection_mode='existing_collectors_reconciled'
 WHERE collection_mode IS NULL;

ALTER TABLE geo_observation_policy
    ALTER COLUMN collection_mode SET DEFAULT 'existing_collectors_reconciled',
    ALTER COLUMN collection_mode SET NOT NULL;

DO $$
DECLARE
    existing_def TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid, true) INTO existing_def
      FROM pg_constraint
     WHERE conname='chk_geo_observation_collection_mode'
       AND conrelid='geo_observation_policy'::regclass;
    IF existing_def IS NULL THEN
        ALTER TABLE geo_observation_policy
            ADD CONSTRAINT chk_geo_observation_collection_mode
            CHECK (collection_mode IN (
                'existing_collectors_reconciled',
                'native_sampling_driver'
            ));
    ELSIF regexp_replace(lower(existing_def), '\s+', '', 'g')
          <> 'check(collection_mode=any(array[''existing_collectors_reconciled''::text,''native_sampling_driver''::text]))' THEN
        RAISE EXCEPTION 'chk_geo_observation_collection_mode 定义被弱化/漂移: %', existing_def;
    END IF;
END $$;

ALTER TABLE geo_observation_policy
    VALIDATE CONSTRAINT chk_geo_observation_collection_mode;

DO $$
DECLARE
    nullable TEXT;
    default_expr TEXT;
    validated BOOLEAN;
BEGIN
    SELECT is_nullable, column_default INTO nullable, default_expr
      FROM information_schema.columns
     WHERE table_schema='public' AND table_name='geo_observation_policy'
       AND column_name='collection_mode';
    IF nullable IS DISTINCT FROM 'NO' THEN
        RAISE EXCEPTION 'collection_mode 必须 NOT NULL';
    END IF;
    IF regexp_replace(COALESCE(default_expr,''), '\s+', '', 'g')
          <> '''existing_collectors_reconciled''::text' THEN
        RAISE EXCEPTION 'collection_mode 默认值错误: %', default_expr;
    END IF;
    SELECT convalidated INTO validated
      FROM pg_constraint
     WHERE conname='chk_geo_observation_collection_mode'
       AND conrelid='geo_observation_policy'::regclass;
    IF validated IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'chk_geo_observation_collection_mode 缺失或未 VALIDATE';
    END IF;
    IF EXISTS (
        SELECT 1 FROM geo_observation_policy
         WHERE collection_mode NOT IN (
             'existing_collectors_reconciled', 'native_sampling_driver'
         )
    ) THEN
        RAISE EXCEPTION 'collection_mode 存在非法值';
    END IF;
END $$;
