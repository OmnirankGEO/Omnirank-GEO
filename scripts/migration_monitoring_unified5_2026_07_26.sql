-- P0-2 · 引擎清单双源打架的收口迁移（Owner 2026-07-26 裁决：诊断与监测统一五引擎）
--
-- 修复前：诊断 tools/ai_visibility/ai_tester.py = dashscope/deepseek/doubao/yuanbao，
--         监测 db/monitoring_db.py = dashscope/deepseek/kimi/doubao
--         → 同一客户在诊断报告与监测面板看到不同引擎、不同答案。
--
-- 本迁移是 **additive**：
--   1) 追加新商品矩阵行 monitoring-unified5-v1 = 五引擎（append-only 触发器允许 INSERT）；
--   2) 只改列默认值（影响将来新建的词/配置），
--      **不 UPDATE 任何存量 platforms / default_platforms / monitoring_product_version**。
--      已售监测按下单时持久化的矩阵继续履约（SSOT §16「已完成账本、退款和报价快照
--      不可无痕重写」）；classic4 矩阵行保留，老词外键不悬空。
--   3) 不动 monitoring_results / monitoring_reports 历史。
--
-- 回滚：把四处默认值 ALTER 回 classic4 即可（见 rollback 段注释）；新矩阵行按
-- append-only 语义保留（保留不影响任何老词，删除反而会打断新词外键）。

SET LOCAL search_path = pg_catalog, public;

-- 前置：矩阵表与 classic4 行必须已由 migration_monitoring_product_matrix_2026_07_21.sql 建立。
DO $$
DECLARE
    legacy_platforms TEXT;
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relname = 'monitoring_product_platform_matrices'
           AND c.relkind = 'r'
           AND c.relpersistence = 'p'
    ) THEN
        RAISE EXCEPTION 'monitoring product matrix table missing; run migration_monitoring_product_matrix_2026_07_21.sql first';
    END IF;

    SELECT platforms
      INTO legacy_platforms
      FROM public.monitoring_product_platform_matrices
     WHERE version = 'monitoring-classic4-v1';
    IF legacy_platforms IS DISTINCT FROM 'dashscope,deepseek,kimi,doubao' THEN
        RAISE EXCEPTION 'historical monitoring matrix missing or rewritten: monitoring-classic4-v1=%',
            legacy_platforms;
    END IF;
END $$;

INSERT INTO public.monitoring_product_platform_matrices (version, platforms)
VALUES ('monitoring-unified5-v1', 'dashscope,deepseek,doubao,kimi,yuanbao')
ON CONFLICT (version) DO NOTHING;

DO $$
DECLARE
    actual_platforms TEXT;
BEGIN
    SELECT platforms
      INTO actual_platforms
      FROM public.monitoring_product_platform_matrices
     WHERE version = 'monitoring-unified5-v1';
    IF actual_platforms IS DISTINCT FROM 'dashscope,deepseek,doubao,kimi,yuanbao' THEN
        RAISE EXCEPTION 'monitoring product matrix drift: monitoring-unified5-v1=%',
            actual_platforms;
    END IF;
END $$;

-- 新词/新配置默认走统一五引擎；存量行一律不动。
ALTER TABLE public.client_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,doubao,kimi,yuanbao';
ALTER TABLE public.extra_keywords
    ALTER COLUMN platforms SET DEFAULT 'dashscope,deepseek,doubao,kimi,yuanbao';
ALTER TABLE public.monitoring_config
    ALTER COLUMN default_platforms SET DEFAULT 'dashscope,deepseek,doubao,kimi,yuanbao';
ALTER TABLE public.confirmed_keywords
    ALTER COLUMN monitoring_product_version SET DEFAULT 'monitoring-unified5-v1';

-- 契约自检：默认值与矩阵一致，且历史行未被本迁移改写。
DO $$
DECLARE
    mismatch_count INTEGER;
    rewritten_count INTEGER;
BEGIN
    SELECT COUNT(*)
      INTO mismatch_count
      FROM (
            VALUES
                ('public.client_keywords'::pg_catalog.regclass,
                 'platforms'::name, '''dashscope,deepseek,doubao,kimi,yuanbao''::text'),
                ('public.extra_keywords'::pg_catalog.regclass,
                 'platforms'::name, '''dashscope,deepseek,doubao,kimi,yuanbao''::text'),
                ('public.monitoring_config'::pg_catalog.regclass,
                 'default_platforms'::name, '''dashscope,deepseek,doubao,kimi,yuanbao''::text'),
                ('public.confirmed_keywords'::pg_catalog.regclass,
                 'monitoring_product_version'::name, '''monitoring-unified5-v1''::text'),
                ('public.monitoring_tasks'::pg_catalog.regclass,
                 'platform_count'::name, '0'::text)
      ) expected(relid, attname, column_default)
      LEFT JOIN pg_catalog.pg_attribute a
        ON a.attrelid = expected.relid
       AND a.attname = expected.attname
       AND NOT a.attisdropped
      LEFT JOIN pg_catalog.pg_attrdef d
        ON d.adrelid = a.attrelid
       AND d.adnum = a.attnum
     WHERE a.attname IS NULL
        OR pg_catalog.pg_get_expr(d.adbin, d.adrelid)
           IS DISTINCT FROM expected.column_default;
    IF mismatch_count <> 0 THEN
        RAISE EXCEPTION 'unified5 monitoring default contract drift';
    END IF;

    -- 老词的商品版本快照必须仍指向 classic4（本迁移不得批量改绑；
    -- confirmed_keywords 上另有 trg_confirmed_monitoring_product_immutable 兜底）。
    SELECT COUNT(*)
      INTO rewritten_count
      FROM public.confirmed_keywords
     WHERE monitoring_product_version NOT IN (
               'monitoring-classic4-v1', 'monitoring-unified5-v1'
           );
    IF rewritten_count <> 0 THEN
        RAISE EXCEPTION 'unexpected monitoring_product_version values: %', rewritten_count;
    END IF;

    IF current_setting('session_replication_role') IS DISTINCT FROM 'origin' THEN
        RAISE EXCEPTION 'session_replication_role must be origin';
    END IF;
END $$;

-- rollback（人工执行，非本文件自动跑）：
--   ALTER TABLE public.client_keywords    ALTER COLUMN platforms                  SET DEFAULT 'dashscope,deepseek,kimi,doubao';
--   ALTER TABLE public.extra_keywords     ALTER COLUMN platforms                  SET DEFAULT 'dashscope,deepseek,kimi,doubao';
--   ALTER TABLE public.monitoring_config  ALTER COLUMN default_platforms          SET DEFAULT 'dashscope,deepseek,kimi,doubao';
--   ALTER TABLE public.confirmed_keywords ALTER COLUMN monitoring_product_version SET DEFAULT 'monitoring-classic4-v1';
-- 已按 unified5 建的词保持原样（它们的矩阵行仍在，不会悬空）。
