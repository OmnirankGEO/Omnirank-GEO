-- 032 · 投放结果事实表(报价升级返工前置 · Review NO-GO 2026-08-09 §返工要求)
--
-- 🔴 编号说明:本迁移原编 029,但 029 被**三个包同时占用**
--    (gap_operation_plan / media_listing_slot / 本包),030 被 brand_star 占,
--    031 被 media_listing_slot 与词表收敛占 → 本包让到 **032**。
--    本迁移**无顺序依赖**(纯新建表、不动任何既有列),排在清单任何位置都行,
--    所以换号零风险。⚠️ 后人加迁移前请 `git log --all --diff-filter=A --name-only -- 'db/migration_*'`
--    全分支查占用,别只看主仓 ls —— 撞号在合批时才会炸。
--
-- 背景:2026-08-09 报价升级调研被 Review 判 NO-GO,五条 P0 里有四条同源 ——
--   **把不同粒度、不同可观察条件的事实混算成了一个「有效率」**:
--     · 没有发布后的固定观察窗 → 244 个已发布 URL 里有 4 个「命中」发生在发布之前;
--     · 把「URL 被检索到」直接命名为「单篇有效率」→ 全库 mention_type='recommended' 仅 309 条;
--     · 分组变量用 COUNT(*) FROM monitoring_tasks 标成「监测词数」→ 该表**没有 keyword 列**,
--       一行是一个任务批次(实测某品牌「127 词」= 127 批次 × 单批 1 词),分组方向整个是反的;
--     · 过稿率分母三选一没冻结(50.2% / 58.0% / 72.2%)。
--
-- 本迁移建两张**只存计数、不存任何比率**的事实表,让上述四件事各归各位。
--
-- 🔴 为什么是两张表而不是一张:
--   「发布成功」的粒度是**一行订单**;「URL 被检索 / 品牌被提及 / 品牌被推荐」的粒度是
--   **(已发布文章 × 关键词 × 引擎 × 窗口)**。硬塞进同一张表,发布事实会被后者的行数放大 N 倍,
--   任何 SUM 都会重复计数 —— 那正是本次返工要修的病。粒度不同的事实分表放。
--
-- 🔴 为什么表里一个比率都不存:
--   Review P0/P1 里有三条是「分母选错 / 分母没冻结 / 只有分子」。比率一旦落库就固化了一个分母选择,
--   下游再也看不见它。这两张表只存**分子和各种候选分母的计数**,任何率都在查询侧现算并显式写出分母。
--
-- 🔴 tests_observable 是第一关唯一合法的分母(不是 tests_total):
--   monitoring_results.search_citations 有 77,350 行是空串、857 行 NULL —— 那些监测**根本没记录
--   AI 检索了什么**,我们无从判断本文有没有被检索到。把它们记成「没被引」= 把「没看见」当「失败」。
--   实测窗口内可观察率仅 28.5%,且**按品牌差 14 倍**(栖舍 94.0% / 罗平皓琪 6.7%)。
--
-- 🔴 本表定位 = **第一关(URL 被检索)事实表**,只记「这篇文章的 URL 有没有进 AI 的检索结果」。
--
--   [2026-08-09 Review 订正] 上一版这里写「品牌被提及/被推荐已由 keyword_compliance_log 承担」,
--   **那句话是假的**。实测该表只有 detection_rate / target_rate / is_compliant 三个量:
--     · 第二关(被提及):`detection_rate` 是它的**日聚合率**(词 × 日),不是独立事实层
--     · 第三关(被推荐):**当前没有任何聚合层**。原始信号只在
--       `monitoring_results.mention_type='recommended'`(全库仅 309 条),没有人把它落成事实。
--   → 第二、三关需要另建「关键词 × 引擎 × 观测日」口径的表。在那之前,
--     **不许把 detection_rate 当推荐率用**。
--   它们不放进本表的理由是**粒度错配**:同品牌两篇文章的 30 天窗口重叠时,
--   一次唯一监测事件会被数成两次(Codex 2026-08-09 复审用构造用例实证:
--   两篇文章 + 同一条监测 → tests_total=2 / gate2=2 / gate3=2,而唯一事件只有 1)。
--   要看「某篇文章确实带来了某次推荐」,查 `geo_article_citation_attributions`(严格归因账本)。
--   要看品牌层面的**日出现率**,查 `keyword_compliance_log.detection_rate` ——
--   但那是率不是事实层,且**不是推荐率**。
--   本表是这两者之间的**投放侧观测层**,不越界。
--
-- 全部 IF NOT EXISTS,幂等,可重复执行。不动任何已有表的任何一列。
-- 漏跑的后果:builder 走 fail-soft 记 table_missing 返 0 行,不抛 —— 事实表是观测面,
-- 绝不能挡住发布/监测/计费主链。所以漏跑是静默的,部署后必须跑文末 §验收 两条 SQL 确认表在。

-- ══════════════════════════════════════════════════════════════════
-- 1) 投放尝试事实 —— 一行 = 一条发布订单行(全部状态,不只 published)
--    回答「发布成功」这一关,并让三种过稿率分母都能在查询侧显式选。
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS geo_publication_attempt_facts (
    id                      BIGSERIAL PRIMARY KEY,
    metric_version          TEXT        NOT NULL,
    -- 事实来源(留字段是为了日后接入非 mhz 供应商时不改表)
    publication_source      TEXT        NOT NULL,
    publication_source_id   BIGINT      NOT NULL,
    brand_id                INTEGER,
    media_id                INTEGER,
    media_name              TEXT,
    -- 只有已发布行才有 URL,因而才有域名;未发布行此列为空是正常的
    publish_domain          TEXT,
    attempt_status          TEXT        NOT NULL,
    -- 三个互斥的结果标记。分母怎么选交给查询侧,这里只陈述事实:
    --   published / (全部)                                = 口径A
    --   published / (全部 - 我方主动终止)                  = 口径B(现役 media_publish_success 口径)
    --   published / (published + rejected)                 = 口径C
    is_published            BOOLEAN     NOT NULL,
    is_rejected             BOOLEAN     NOT NULL,
    -- cancelled / withdrawn = 我方主动终止,不是媒体不给过。混进拒稿会低估过稿率。
    is_terminated_by_us     BOOLEAN     NOT NULL,
    cost_yuan               NUMERIC,
    -- 🔴 成本是否真退:本列**只记库里有没有成本数字**,不代表钱有没有退回。
    --    退款账本核验是独立任务(Review P0-5),没核完之前任何包价公式不许假设「拒稿必退」。
    has_cost_record         BOOLEAN     NOT NULL,
    submitted_at            TIMESTAMPTZ,
    published_at            TIMESTAMPTZ,
    source_created_at       TIMESTAMPTZ,
    built_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_pub_attempt_facts
        UNIQUE (metric_version, publication_source, publication_source_id)
);

-- @index-guard idx_pub_attempt_brand ON geo_publication_attempt_facts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pub_attempt_brand' AND i.indrelid = to_regclass('public.geo_publication_attempt_facts')) THEN
        NULL;  -- 已在 public.geo_publication_attempt_facts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pub_attempt_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pub_attempt_brand 已存在但不在 public.geo_publication_attempt_facts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pub_attempt_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pub_attempt_brand ON public.geo_publication_attempt_facts (brand_id, attempt_status);
    END IF;
END $idxguard$;
-- @index-guard idx_pub_attempt_domain ON geo_publication_attempt_facts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pub_attempt_domain' AND i.indrelid = to_regclass('public.geo_publication_attempt_facts')) THEN
        NULL;  -- 已在 public.geo_publication_attempt_facts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pub_attempt_domain' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pub_attempt_domain 已存在但不在 public.geo_publication_attempt_facts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pub_attempt_domain' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pub_attempt_domain ON public.geo_publication_attempt_facts (publish_domain) WHERE publish_domain IS NOT NULL;
    END IF;
END $idxguard$;

-- ══════════════════════════════════════════════════════════════════
-- 2) 投放结果事实 —— 一行 = (已发布文章 × 关键词 × 引擎 × 窗口)
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS geo_publication_outcome_facts (
    id                          BIGSERIAL PRIMARY KEY,
    metric_version              TEXT        NOT NULL,
    -- 与严格账本共用同一套 URL 归一化(services/strict_article_outcomes.URL_NORMALIZATION_VERSION),
    -- 两边口径必须一致,否则同一篇文章在两张表里会长成两个身份。
    url_normalization_version   TEXT        NOT NULL,
    publication_source          TEXT        NOT NULL,
    publication_source_id       BIGINT      NOT NULL,
    brand_id                    INTEGER     NOT NULL,
    publish_url_normalized      TEXT        NOT NULL,
    publish_domain              TEXT        NOT NULL,
    media_id                    INTEGER,
    media_name                  TEXT,
    published_at                TIMESTAMPTZ NOT NULL,
    -- 问题维度。⚠️ 这里是 monitoring_results.keyword(401 个不同值,NOT NULL),
    -- **不是**真正发给 AI 的那句问题 —— sent_question_snapshot 在 92,743 行里有 91,592 行是空的。
    -- 所以本列的语义是「客户买的那个词」,不是「客户问题」。别把它当问题级证据用。
    keyword                     TEXT        NOT NULL,
    -- 引擎维度用 platform(deepseek/doubao/dashscope/kimi,NOT NULL,四家各约 23k 行)。
    -- 不用 provider:92,743 行里 91,592 行是 'legacy_unknown'。
    platform                    TEXT        NOT NULL,
    window_days                 SMALLINT    NOT NULL,
    -- 🔴 窗口是否已走完。未走完的窗口(文章刚发)分母天然偏小,算率必须先过滤 window_complete。
    window_complete             BOOLEAN     NOT NULL,

    -- ── 计数(只存计数,不存率) ─────────────────────────────
    -- 窗口内该 (品牌,词,引擎) 的监测次数。是本行的总基数(gate1 的分母另见下一列)。
    tests_total                 INTEGER     NOT NULL,
    -- 🔴 gate1 唯一合法的分母:search_citations 能解析成数组的次数。
    tests_observable            INTEGER     NOT NULL,
    -- search_citations 存在但解析不出数组(坏 JSON / 不是数组)。单独记,不混进上面两个。
    citations_unparsable        INTEGER     NOT NULL,
    -- 本文 URL(归一化后)出现在该次监测引用列表里的次数
    gate1_url_cited             INTEGER     NOT NULL,
    -- 🔴 这里**没有**「品牌被提及/被推荐」两列。它们是「词 × 日」粒度的事实,
    -- 不放进本表的理由是**粒度错配**(不是「别处已经有了」—— 见文首订正):
    -- 同品牌两篇文章重叠同一条监测结果时,一次唯一监测事件会被数成两次
    -- (Codex 2026-08-09 复审构造用例实证)。粒度不同的事实分表放。

    -- 窗口内首次被引时间 / 被引时的最好排位(来自 search_citations 里的 rank)
    first_cited_at              TIMESTAMPTZ,
    best_citation_rank          SMALLINT,

    built_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_pub_outcome_facts UNIQUE (
        metric_version, publication_source, publication_source_id,
        keyword, platform, window_days
    ),
    -- 分子不可能大于分母。数据出错时宁可写不进去,也不要落一行自相矛盾的事实。
    CONSTRAINT ck_pub_outcome_gate1_le_observable
        CHECK (gate1_url_cited <= tests_observable),
    CONSTRAINT ck_pub_outcome_observable_le_total
        CHECK (tests_observable + citations_unparsable <= tests_total),
    CONSTRAINT ck_pub_outcome_window CHECK (window_days > 0)
);

-- @index-guard idx_pub_outcome_brand_window ON geo_publication_outcome_facts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pub_outcome_brand_window' AND i.indrelid = to_regclass('public.geo_publication_outcome_facts')) THEN
        NULL;  -- 已在 public.geo_publication_outcome_facts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pub_outcome_brand_window' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pub_outcome_brand_window 已存在但不在 public.geo_publication_outcome_facts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pub_outcome_brand_window' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pub_outcome_brand_window ON public.geo_publication_outcome_facts (brand_id, window_days) WHERE window_complete;
    END IF;
END $idxguard$;
-- @index-guard idx_pub_outcome_domain_window ON geo_publication_outcome_facts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pub_outcome_domain_window' AND i.indrelid = to_regclass('public.geo_publication_outcome_facts')) THEN
        NULL;  -- 已在 public.geo_publication_outcome_facts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pub_outcome_domain_window' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pub_outcome_domain_window 已存在但不在 public.geo_publication_outcome_facts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pub_outcome_domain_window' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pub_outcome_domain_window ON public.geo_publication_outcome_facts (publish_domain, window_days) WHERE window_complete;
    END IF;
END $idxguard$;
-- @index-guard idx_pub_outcome_source ON geo_publication_outcome_facts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pub_outcome_source' AND i.indrelid = to_regclass('public.geo_publication_outcome_facts')) THEN
        NULL;  -- 已在 public.geo_publication_outcome_facts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pub_outcome_source' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pub_outcome_source 已存在但不在 public.geo_publication_outcome_facts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pub_outcome_source' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pub_outcome_source ON public.geo_publication_outcome_facts (publication_source, publication_source_id);
    END IF;
END $idxguard$;

-- ══════════════════════════════════════════════════════════════════
-- §验收(部署后必跑,漏跑是静默的)
-- ══════════════════════════════════════════════════════════════════
--   1. 表在不在:
--      SELECT to_regclass('public.geo_publication_attempt_facts'),
--             to_regclass('public.geo_publication_outcome_facts');
--   2. 约束在不在(尤其五条 CHECK,它们是防「分子大于分母」的最后一道):
--      SELECT conname FROM pg_constraint
--       WHERE conrelid='geo_publication_outcome_facts'::regclass ORDER BY 1;
