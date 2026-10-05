-- 059 · 图文选题表(WO_204 §1.1)
-- =============================================================================
-- Owner 09-13:「不是全自动,是半自动,和写作一样:默认把这几个选题写出来,
-- 用户可以修改,修改后就按照标题来进行创作。」
--
-- 现状:蒸馏器(`services/geo_douyin/topic_distiller.py`)产出的 topics[]
-- **只活在 `geo_douyin_distill_tasks.result` 这一坨 jsonb 里**,没有逐条的行。
-- 于是"用户改一条标题""这一条已经做过了"这两件事都无处落 ——
-- 改 jsonb 里的一个元素既没有并发保护,也没法给单条加状态。
--
-- 🔴 **不复用写文章的 `topics` 表**(元指令 12 板块边界):那张表挂在
--    quote_id / keyword_id 的报价体系上,带 article_style / user_choice /
--    style_code 一整套写文章的文体合同。图文的选题没有文体,却有
--    城市、卡片大纲、成品 post_id。挤进同一张表之后,两边的查询都要
--    先问"这行是哪一边的",而那个判别条件一旦有人忘写,就是跨板块串数据。
--
-- 🔴 **零 DML**:新表,不回填。历史蒸馏任务的 topics 留在 result jsonb 里,
--    不迁过来 —— 那些任务的产物早就被用户当一次性清单用掉了,
--    补进"待做"列表会让每个老客户的列表里凭空出现一堆没人要的选题。
--
-- 🔴 **幂等**:部署脚本对候选镜像跑两遍 prestart(deploy-blue-green.sh:687-689),
--    所以 CREATE 全部 IF NOT EXISTS。
--
-- SQL 4 维核验(2026-09-13 逐列实测,源 = .deploy_toolkit/prod_schema_2026-09-05.sql):
--   ① 列名:下列引用列逐列肉眼核过;
--   ② data_type:brands.id=integer · confirmed_keywords.id=integer ·
--      users.id=integer · geo_douyin_posts.id=**bigint** ·
--      geo_douyin_distill_tasks.id=**bigint** ·
--      geo_douyin_posts.city=character varying(32)。
--      本表逐列对齐上述类型 —— 写成 BIGINT 的只有那两个真 bigint 的引用;
--      brand_id 写 INTEGER 是因为它要跟 brands.id / geo_douyin_posts.brand_id
--      对得上,写 BIGINT 会在 JOIN 时被隐式转换掩盖掉类型错配;
--   ③ 字段归属:city/keyword/title 的形态取自 geo_douyin_posts(同一族),
--      不取写文章 topics(那边 keyword 叫 original_keyword);
--   ④ dry-run:见交付物,BEGIN; <本文件>; ROLLBACK; 在 PG16 上跑过。
--
-- 外键(2026-09-13 用 `pg_constraint` 实测,**不是** grep dump):
--   这一族现有 3 条外键,**全部**指向 `geo_douyin_posts(id)` ——
--     geo_douyin_post_revisions.geo_post_id
--     geo_douyin_post_tasks.post_id
--     geo_douyin_publish_artifacts.geo_post_id
--   本表的 `post_id` 是同一种关系,**照加**。
--   `brand_id` / `confirmed_keyword_id` / `distill_task_id` **不加** ——
--   `geo_douyin_posts.brand_id` 本身就没有外键,只给新表加会让本表成为
--   删品牌时唯一一处报错的地方,而那个报错看起来像本表坏了。
--
--   ⚠️ 我一开始写的理由是「这一族一个外键都没有」——**那是错的,已实测证伪**:
--      当时的 grep 把 `ALTER TABLE ONLY …` 与 `ADD CONSTRAINT … FOREIGN KEY …`
--      当成同一行找,而 pg_dump 是分两行写的,于是得到一个自信的空结果。
--      留着一个假理由比没有理由更坏,故订正。查约束一律问 `pg_constraint`。
--
-- 号段:058 是 #169 支付意向 URL · **059 归本单** · 下一空 = 060。
-- =============================================================================

CREATE TABLE IF NOT EXISTS geo_douyin_topics (
    id                   BIGSERIAL   PRIMARY KEY,

    brand_id             INTEGER     NOT NULL,
    confirmed_keyword_id INTEGER,
    keyword              TEXT,
    -- 与 geo_douyin_posts.city 同形(varchar(32))。城市可空:
    -- 全国词没有城市,而"没有城市"与"城市是空串"是两件事。
    city                 VARCHAR(32),

    title                TEXT        NOT NULL,
    angle                TEXT,
    -- 蒸馏器给的卡片大纲,制作时当提示用。形状 = ["...", "..."]。
    card_outline         JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- distilled = AI 出的;user = 人加的或人改过标题的。
    -- 🔴 改标题后置 user 是**有意的**:它是"这条还是不是 AI 原话"的唯一依据,
    --    效果复盘要按这一列分组。不置的话,人改过的题会被算进 AI 的成绩里。
    source               TEXT        NOT NULL DEFAULT 'distilled',
    status               TEXT        NOT NULL DEFAULT 'pending',

    -- 做成了才有。done 之外一律 NULL。
    post_id              BIGINT,
    distill_task_id      BIGINT,
    -- 同一个蒸馏任务里的第几条(从 0 起)。
    -- 🔴 它存在的唯一理由是**幂等**:重放同一个 task 时靠
    --    (distill_task_id, distill_index) 去重。用 title 去重不行 ——
    --    两条选题的标题**可以**合法地相同,那样第二条会被静默吞掉,
    --    而"落表条数 == 回包条数"这条判据反而会红在一个没有缺陷的地方。
    distill_index        INTEGER,

    created_by           INTEGER     NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 人改过标题的时刻。没改过就是 NULL(不是 created_at)——
    -- "从没被改过"与"创建时就被改了"要分得出来。
    edited_at            TIMESTAMPTZ,

    CONSTRAINT ck_geo_douyin_topic_source
        CHECK (source = ANY (ARRAY['distilled'::text, 'user'::text])),
    CONSTRAINT ck_geo_douyin_topic_status
        CHECK (status = ANY (ARRAY['pending'::text, 'making'::text, 'done'::text,
                                   'failed'::text, 'archived'::text])),
    -- 🔴 post_id 只在 done 上有意义。没有这一条的话,
    --    "做失败了但 post_id 还留着上一次的"会是个合法状态,
    --    而列表上看不出来 —— 点进去是别人的成品。
    CONSTRAINT ck_geo_douyin_topic_post_shape
        CHECK ((status = 'done'::text AND post_id IS NOT NULL)
               OR (status <> 'done'::text AND post_id IS NULL)),
    -- 与同族三张表同一种关系(见文件头实测)。成品被删时不让选题指向空。
    CONSTRAINT fk_geo_douyin_topic_post
        FOREIGN KEY (post_id) REFERENCES geo_douyin_posts (id)
);

-- 列表页的主查询:某品牌下按状态分 tab。
CREATE INDEX IF NOT EXISTS idx_geo_douyin_topics_brand_status
    ON geo_douyin_topics (brand_id, status, id DESC);

-- 幂等去重键。partial:手加的题没有 task,不该被这把锁管。
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_douyin_topics_distill_slot
    ON geo_douyin_topics (distill_task_id, distill_index)
    WHERE distill_task_id IS NOT NULL AND distill_index IS NOT NULL;
