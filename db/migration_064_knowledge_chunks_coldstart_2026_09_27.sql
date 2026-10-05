-- ============================================================================
-- migration_064 · 冷启动补齐 knowledge_chunks 表与 advisors 的两列(E0d · 2026-09-27)
-- ============================================================================
-- 为什么:在役的 GET /api/advisors(api/advisor_api.py::list_advisors,设置页选写手 · 品牌 AI 补全 ·
--   营销 tab)LEFT JOIN knowledge_chunks,并读 advisors.knowledge_base_path / last_kb_update。
--   新 PG 库上这张表与这两列在代码里唯一的建表 / 补列处,是随 E3 删除的
--   tools/social_operator/advisor_knowledge_pipeline.py(且只在顾问导入端点被调用时才跑);
--   另外只有手工脚本 scripts/setup_knowledge_chunks.sql、scripts/alter_columns.sql,
--   与 SQLite 时代、不在任何启动路径上的 db/migrate_v3.py。
--   ⇒ 开源版 / 灾备重建的新库上,这个在役端点直接报错(E3_DELETION_MAP §6-4)。
--
-- 🔴 对生产是空操作:每一步都「先查后建」——
--   · 表只在 to_regclass 为空时建;索引、触发器函数、触发器都只在不存在时建;
--   · 触发器函数**不用 CREATE OR REPLACE**:生产上已有的 kc_tsv_trigger 函数体与手工脚本那份不同
--     (生产版不含 keywords 那段),本迁移绝不覆盖它;新库上建的是与生产逐字相同的那份;
--   · advisors 补列先查 information_schema,真缺才 ALTER —— 不裸写 ADD COLUMN IF NOT EXISTS:
--     那句即使列已存在也会先拿 AccessExclusiveLock,每次部署都会在繁忙表上排队(WO_284 教训)。
-- 形状取自生产 schema 快照 tests/article_self_report_2026_08_19/prod_schema_2026-08-19.sql(仓内最新)
--   (id serial 主键 · chunk_id 唯一约束 · embedding vector(1024) · 10 个显式索引 · kc_tsv_update 触发器)。
--   生产上 chunk_id / tsv / advisor 有重复索引(两批历史脚本各建了一份),照抄不去重 —— 新库与生产同形。
-- 没有 pgvector 的库:embedding 退成 text、跳过 HNSW 索引(与 advisor_knowledge_pipeline 的回退一致),
--   list_advisors 只读 `embedding IS NOT NULL`,两种类型都成立。
-- advisors 表本身由 prestart 先 import db.diagnosis_db 引导建出(scripts/prestart.py),本迁移只补两列。
-- 锁:tests/e0d_knowledge_chunks_coldstart_2026_09_27
-- ============================================================================

-- 1. pgvector(可选):装不上不失败,后面按有无扩展选列类型
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN others THEN
            RAISE NOTICE '[migration_064] pgvector 不可用,knowledge_chunks.embedding 用 text: %', SQLERRM;
        END;
    END IF;
END
$do$;

-- 2. advisors 的两列(只补缺的;表不存在则跳过)
DO $do$
BEGIN
    IF to_regclass('public.advisors') IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'advisors' AND column_name = 'knowledge_base_path'
        ) THEN
            ALTER TABLE public.advisors ADD COLUMN knowledge_base_path text;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'advisors' AND column_name = 'last_kb_update'
        ) THEN
            ALTER TABLE public.advisors ADD COLUMN last_kb_update timestamp without time zone;
        END IF;
    END IF;
END
$do$;

-- 3. knowledge_chunks 表 + 索引(表已存在则整段跳过)
DO $do$
DECLARE
    has_vector boolean := EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');
BEGIN
    IF to_regclass('public.knowledge_chunks') IS NULL THEN
        EXECUTE format($ddl$
            CREATE TABLE public.knowledge_chunks (
                id serial PRIMARY KEY,
                advisor_id character varying(64) NOT NULL,
                profile_id character varying(64),
                chunk_id character varying(128) NOT NULL,
                title character varying(200) NOT NULL,
                content text NOT NULL,
                content_type character varying(32) DEFAULT 'knowledge'::character varying,
                applicable_to jsonb DEFAULT '[]'::jsonb,
                keywords jsonb DEFAULT '[]'::jsonb,
                use_when text DEFAULT ''::text,
                dont_use_when text DEFAULT ''::text,
                related_concepts jsonb DEFAULT '[]'::jsonb,
                golden_quotes jsonb DEFAULT '[]'::jsonb,
                metadata jsonb DEFAULT '{}'::jsonb,
                source_file character varying(255) DEFAULT ''::character varying,
                source_section character varying(255) DEFAULT ''::character varying,
                embedding %s,
                tsv tsvector,
                created_at timestamp without time zone DEFAULT now(),
                updated_at timestamp without time zone DEFAULT now(),
                is_active boolean DEFAULT true,
                CONSTRAINT knowledge_chunks_chunk_id_key UNIQUE (chunk_id)
            )
        $ddl$, CASE WHEN has_vector THEN 'public.vector(1024)' ELSE 'text' END);

        CREATE INDEX IF NOT EXISTS idx_kc_advisor ON public.knowledge_chunks USING btree (advisor_id, content_type, is_active);
        CREATE INDEX IF NOT EXISTS idx_kc_chunk_id ON public.knowledge_chunks USING btree (chunk_id);
        CREATE INDEX IF NOT EXISTS idx_kc_profile ON public.knowledge_chunks USING btree (profile_id) WHERE (profile_id IS NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_kc_tsv ON public.knowledge_chunks USING gin (tsv);
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_active ON public.knowledge_chunks USING btree (is_active);
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_advisor ON public.knowledge_chunks USING btree (advisor_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_chunks_chunk_id_unique ON public.knowledge_chunks USING btree (chunk_id);
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_tsv ON public.knowledge_chunks USING gin (tsv);
        CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_type ON public.knowledge_chunks USING btree (content_type);
        IF has_vector THEN
            EXECUTE 'CREATE INDEX IF NOT EXISTS idx_kc_embedding ON public.knowledge_chunks '
                    'USING hnsw (embedding public.vector_cosine_ops) WITH (m=''16'', ef_construction=''200'')';
        END IF;
    END IF;
END
$do$;

-- 4. tsv 触发器函数 + 触发器(都只在不存在时建;函数体与生产逐字相同)
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'kc_tsv_trigger'
    ) THEN
        EXECUTE $fn$
CREATE FUNCTION public.kc_tsv_trigger() RETURNS trigger
    LANGUAGE plpgsql
    AS $body$
BEGIN
    NEW.tsv :=
        setweight(to_tsvector('simple', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(NEW.content, '')), 'B');
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$body$
        $fn$;
    END IF;

    IF to_regclass('public.knowledge_chunks') IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'kc_tsv_update' AND tgrelid = 'public.knowledge_chunks'::regclass
    ) THEN
        CREATE TRIGGER kc_tsv_update BEFORE INSERT OR UPDATE ON public.knowledge_chunks
            FOR EACH ROW EXECUTE FUNCTION public.kc_tsv_trigger();
    END IF;
END
$do$;
