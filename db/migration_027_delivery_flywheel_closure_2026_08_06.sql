-- 027 · 交付闭环通电 + 品牌实体档案(WO_DELIVERY_FLYWHEEL_CLOSURE_2026-08-06 §2.1 / §2.2)
--
-- 背景(2026-08-06 生产只读取证 7 轮,详见 docs/AI-CONTEXT/FLYWHEEL_CLOSURE_STATUS_VERDICT_2026-08-06.md):
--   `services/strict_article_outcomes.load_strict_outcomes` 是一个已经能跑出真实归因行的
--   引擎(生产实跑 event_count=2,含 QZQZ brand 662 被 moonshot 引用并 recommended),
--   但它的产物**从不落库**;而写作飞轮的 outcome 回写走的是另一条查 geo_research 语料池的
--   路径,246 个已发布 URL 里那个池只命中 1 个 —— 闭环因此恒 0 行。
--   本迁移只做一件事:给那个引擎一个落脚点,外加实体归一所需的**只读映射**。
--
-- 三张表:
--   1) geo_article_citation_attributions —— 归因账本(闭环验收锚落点)
--   2) brand_identity_profiles           —— 品牌实体档案 SSOT(正式名/简称/历史名/别名白名单)
--   3) brand_identity_members            —— brand_id → 实体档案 的**只关联不删除**映射
--
-- 🔴 本迁移**不动 brands 表任何一列**,不写 parent_brand_id,不改 owner_user_id ——
--    实体归并涉及跨用户商业归属,按 Owner 2026-08-06 裁定「先建档案+映射,不动归属」。
--    映射表是纯只读旁路:删掉它,归因退回按 brand_id 严格相等,行为回到迁移前。
--
-- 🔴 漏跑的后果是**静默的**(所以写清楚):归因同步 job 对 UndefinedTable 走 fail-soft
--    记 0 行并落心跳 detail,飞轮回写退回原 geo_research 路径 = 回到修复前的样子(恒 0)。
--    刻意选静默方向:归因是观测面,绝不能让它挡住发布/监测/计费主链。
--    反过来说,漏跑不会自己冒出来,要靠部署单核 —— 部署后跑 §验收 那两条 SQL 确认表在。
--
-- 全部 IF NOT EXISTS,幂等,可重复执行;不依赖任何前置迁移(只引用 brands.id 这个自古就有的键)。

-- ══════════════════════════════════════════════════════════════════
-- 1) 归因账本
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS geo_article_citation_attributions (
    id                          BIGSERIAL PRIMARY KEY,
    -- 口径版本(引擎自报,用于日后口径变更时区分历史行,禁止就地改写老行)
    metric_version              TEXT        NOT NULL,
    url_normalization_version   TEXT        NOT NULL,
    -- 事实三元组:哪条监测结果 · 引用了哪个发布物 · 对应哪篇文章
    monitoring_result_id        BIGINT      NOT NULL,
    publication_source          TEXT        NOT NULL,
    publication_source_id       BIGINT      NOT NULL,
    article_id                  BIGINT,
    publication_snapshot_hash   CHAR(64),
    -- 归属(brand_id 来自发布侧;identity_key 是实体档案键,可为空 = 未建档)
    brand_id                    INTEGER,
    identity_key                TEXT,
    industry                    TEXT,
    -- 渠道有效性(§2.3 白名单口径的原料:被引的到底是哪个媒体位)
    publish_url_normalized      TEXT        NOT NULL,
    publish_domain              TEXT        NOT NULL,
    -- 文体与问题族(§2.5.5 「哪家好」类问题该配什么文体 的原料)
    style_family                TEXT,
    question                    TEXT,
    question_family             TEXT,
    question_family_version     TEXT,
    -- 引擎侧
    provider                    TEXT,
    model                       TEXT,
    model_revision              TEXT,
    surface                     TEXT,
    target_outcome              TEXT,
    -- 时间(published_at < tested_at 由引擎保证,这里再加一道 CHECK)
    published_at                TIMESTAMPTZ NOT NULL,
    tested_at                   TIMESTAMPTZ NOT NULL,
    url_match                   TEXT        NOT NULL,
    -- 证据等级(🔴 主指标只认 TRUE):
    --   TRUE  = 有提交正文快照,能证明"被 AI 引用的就是我们发出去的那一版";
    --   FALSE = URL 精确匹配成立,但正文无快照可自证(2026-05 前的历史发布普遍如此)。
    -- 事后拿当前 article 正文补哈希 = 伪造证据等级,禁止;所以这里用一个诚实的标志位,
    -- 让这类行**能被看见但永不并入主指标**。
    body_proof                  BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 幂等补列(若更早版本的本迁移已建过表)
ALTER TABLE public.geo_article_citation_attributions
    ADD COLUMN IF NOT EXISTS body_proof BOOLEAN NOT NULL DEFAULT TRUE;

-- 幂等键:同一条监测结果对同一个发布物只记一行(重复跑同步 job 不产生重复行)
-- @index-guard uq_geo_article_attr_result_publication ON geo_article_citation_attributions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_article_attr_result_publication' AND i.indrelid = to_regclass('public.geo_article_citation_attributions')) THEN
        NULL;  -- 已在 public.geo_article_citation_attributions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_article_attr_result_publication' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_article_attr_result_publication 已存在但不在 public.geo_article_citation_attributions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_article_attr_result_publication' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_article_attr_result_publication ON public.geo_article_citation_attributions (monitoring_result_id, publication_source, publication_source_id);
    END IF;
END $idxguard$;

-- @index-guard idx_geo_article_attr_brand_tested ON geo_article_citation_attributions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_attr_brand_tested' AND i.indrelid = to_regclass('public.geo_article_citation_attributions')) THEN
        NULL;  -- 已在 public.geo_article_citation_attributions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_attr_brand_tested' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_attr_brand_tested 已存在但不在 public.geo_article_citation_attributions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_attr_brand_tested' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_attr_brand_tested ON public.geo_article_citation_attributions (brand_id, tested_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_attr_identity_tested ON geo_article_citation_attributions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_attr_identity_tested' AND i.indrelid = to_regclass('public.geo_article_citation_attributions')) THEN
        NULL;  -- 已在 public.geo_article_citation_attributions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_attr_identity_tested' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_attr_identity_tested 已存在但不在 public.geo_article_citation_attributions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_attr_identity_tested' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_attr_identity_tested ON public.geo_article_citation_attributions (identity_key, tested_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_attr_article ON geo_article_citation_attributions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_attr_article' AND i.indrelid = to_regclass('public.geo_article_citation_attributions')) THEN
        NULL;  -- 已在 public.geo_article_citation_attributions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_attr_article' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_attr_article 已存在但不在 public.geo_article_citation_attributions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_attr_article' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_attr_article ON public.geo_article_citation_attributions (article_id);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_attr_domain ON geo_article_citation_attributions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_attr_domain' AND i.indrelid = to_regclass('public.geo_article_citation_attributions')) THEN
        NULL;  -- 已在 public.geo_article_citation_attributions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_attr_domain' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_attr_domain 已存在但不在 public.geo_article_citation_attributions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_attr_domain' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_attr_domain ON public.geo_article_citation_attributions (publish_domain, tested_at DESC);
    END IF;
END $idxguard$;
-- 主指标查询恒带 body_proof 过滤,给它一个部分索引
-- @index-guard idx_geo_article_attr_proven ON geo_article_citation_attributions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_attr_proven' AND i.indrelid = to_regclass('public.geo_article_citation_attributions')) THEN
        NULL;  -- 已在 public.geo_article_citation_attributions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_attr_proven' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_attr_proven 已存在但不在 public.geo_article_citation_attributions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_attr_proven' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_attr_proven ON public.geo_article_citation_attributions (article_id, tested_at DESC) WHERE body_proof;
    END IF;
END $idxguard$;

-- 时序不变式:被引时间必须晚于发布时间(引擎已挡 prepublication_citation,DB 兜底)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_geo_article_attr_time_order'
           AND conrelid = 'public.geo_article_citation_attributions'::regclass
    ) THEN
        ALTER TABLE public.geo_article_citation_attributions
            ADD CONSTRAINT chk_geo_article_attr_time_order
            CHECK (tested_at > published_at);
    END IF;
END $$;

COMMENT ON TABLE geo_article_citation_attributions IS
    '归因账本:我方已发布 URL 被哪个引擎在哪次监测里引用。由 services/article_attribution_ledger 同步,'
    '源头是 services/strict_article_outcomes.load_strict_outcomes(只读 monitoring_results + 四条发布链)。'
    '本表只写不改:口径变更走 metric_version 新行,不就地改写历史。';

-- ══════════════════════════════════════════════════════════════════
-- 2) 品牌实体档案 SSOT
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS brand_identity_profiles (
    identity_key        TEXT        PRIMARY KEY,
    canonical_name      TEXT        NOT NULL,   -- 正式公司名
    short_name          TEXT,                   -- 品牌简称
    historical_names    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    alias_whitelist     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    region              TEXT,
    business            TEXT,
    official_site       TEXT,
    note                TEXT,
    created_by          BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE brand_identity_profiles IS
    '品牌实体档案 SSOT(WO §2.2)。与 monitoring_identity_name_decisions 分工:那张表是'
    '「监测答案里出现的名字判给哪个 brand」的决策流水;本表是「这个实体本身叫什么」的档案。'
    '⚠️ 本表不承载商业归属;归属 SSOT 仍是 customer_agent_bindings。';

-- ══════════════════════════════════════════════════════════════════
-- 3) brand_id → 实体档案 映射(只关联不删除)
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS brand_identity_members (
    brand_id        INTEGER     PRIMARY KEY REFERENCES brands(id),
    identity_key    TEXT        NOT NULL REFERENCES brand_identity_profiles(identity_key),
    member_role     TEXT        NOT NULL DEFAULT 'duplicate',   -- 'primary' | 'duplicate'
    note            TEXT,
    created_by      BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- @index-guard idx_brand_identity_members_key ON brand_identity_members plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_brand_identity_members_key' AND i.indrelid = to_regclass('public.brand_identity_members')) THEN
        NULL;  -- 已在 public.brand_identity_members 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_brand_identity_members_key' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_brand_identity_members_key 已存在但不在 public.brand_identity_members 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_brand_identity_members_key' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_brand_identity_members_key ON public.brand_identity_members (identity_key);
    END IF;
END $idxguard$;

-- 每个实体最多一个 primary(其余都是 duplicate;归因聚合读 primary 作展示主档)
-- @index-guard uq_brand_identity_primary ON brand_identity_members unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_brand_identity_primary' AND i.indrelid = to_regclass('public.brand_identity_members')) THEN
        NULL;  -- 已在 public.brand_identity_members 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_brand_identity_primary' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_brand_identity_primary 已存在但不在 public.brand_identity_members 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_brand_identity_primary' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_brand_identity_primary ON public.brand_identity_members (identity_key) WHERE member_role = 'primary';
    END IF;
END $idxguard$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_brand_identity_member_role'
           AND conrelid = 'public.brand_identity_members'::regclass
    ) THEN
        ALTER TABLE public.brand_identity_members
            ADD CONSTRAINT chk_brand_identity_member_role
            CHECK (member_role IN ('primary', 'duplicate'));
    END IF;
END $$;

COMMENT ON TABLE brand_identity_members IS
    'brand_id → 实体档案 的只读映射(WO §2.2「归并只关联不删除」)。'
    '🔴 不删除任何 brand 行、不改 owner_user_id、不写 brands.parent_brand_id —— '
    '跨用户商业归属变更须单独出单报 Owner。删掉本表 = 归因退回按 brand_id 严格相等。';

-- ══════════════════════════════════════════════════════════════════
-- 验收(部署后手工跑这两条确认迁移真跑了)
--   SELECT to_regclass('public.geo_article_citation_attributions');   -- 非空
--   SELECT to_regclass('public.brand_identity_members');              -- 非空
-- ══════════════════════════════════════════════════════════════════
