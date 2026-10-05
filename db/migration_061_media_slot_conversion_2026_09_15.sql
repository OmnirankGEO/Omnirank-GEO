-- ============================================================================
-- 060 · 媒体桶换算表 + topics 的桶列(WO_225-c1 §8.1/§8.3)
-- ============================================================================
--
-- 这是什么
-- ----------------------------------------------------------------------------
-- 一个交付槽,按不同类别的媒体去发,**要发的条数不一样**。今天系统已经承认
-- 「一篇的成本随媒体档次变」(`tools/transparent_pricing.py:81-89` 的 MEDIA_TIER_COSTS),
-- 却仍假定「一篇 = 一篇」。本表把「一槽要几条」这个**运营口径**变成一个可版本化的数。
--
-- 🔴 它是**运营工作量口径**,不是效果承诺。
--    裁定书 v2 §1.2/§2.2/§7 三处明令:不得说「两篇覆盖内容等于一篇重点媒体」、
--    不得签发「某层媒体等于另一层几篇的效果系数」。那说的是**效果等价**;
--    本表说的是「按这类媒体发,填满一个槽通常要打几条」。两件事必须分开写,
--    所以列名是 `posts_per_slot_bps`(条/槽),不是 `equivalence` 之类。
--
-- 为什么是 bps 整数而不是小数
-- ----------------------------------------------------------------------------
-- 10000 bps = 一槽一条。比较与求和一律整数,禁 float ——
-- 08_billing §3.3 的同一条纪律:钱与额度相关的比较不许踩浮点误差。
--
-- 为什么另起一张表,而不是塞进 settings
-- ----------------------------------------------------------------------------
-- 🔴 `config/settings_manager.py` 与 `config/pricing_config.py` **都是保护文件**,
--    而且**不带版本**。这个数会随 Owner 口径变,且旧报价必须能读回**当时那一版**
--    (08_billing §3.3「调价必须创建新版本;旧目录、旧报价不得覆盖」)。
--    塞进无版本的配置里,等于让历史报价的解释随当前配置漂移。
--
-- 🔴 也不复用 `geo_article_delivery_slots`(035 已有 media_mix_bucket 列):
--    那张表是**逐槽的交付记录**,一行一个槽;本表是**系数字典**,一行一个(版本,桶)。
--    挤进一张表以后,「这一行是记录还是字典」要靠额外判别条件区分,漏写就串数据。
--
-- 顺序 / 依赖
-- ----------------------------------------------------------------------------
-- 🔴 无依赖:新建一张独立表 + 给 `topics` 加一列可空列。不加 FK、不改任何既有约束。
-- 🔴 桶名逐字复用 035 的三值(`focus_media_anchor` / `industry_platform_coverage`
--    / `douyin_doubao_only`),与 `services/quote_media_mix.py:300-302` 同名 ——
--    **不另起一套名字**:同一个概念两套词表,迟早有一处对不上而没人报错。
--
-- 🔴 本文件**零 DML**
-- ----------------------------------------------------------------------------
-- v1 的初值(anchor 10000 / coverage 50000 / douyin 50000)**不在这里播种**。
-- 迁移体内禁 DML 是本仓红线;播种由 `services/media_slot_conversion.ensure_seeded()`
-- 在首次读取时幂等写入(与 `db/social_preferences_db.ensure_schema()` 同法)。
-- 这样重放本迁移不会覆盖 Owner 之后发的新版本。
--
-- 重放安全
-- ----------------------------------------------------------------------------
-- 全部 IF NOT EXISTS / 条件建约束;重复执行是空操作。
-- ============================================================================

CREATE TABLE IF NOT EXISTS media_slot_conversion_versions (
    id                  BIGSERIAL PRIMARY KEY,
    version             INTEGER      NOT NULL,
    bucket              VARCHAR(40)  NOT NULL,
    posts_per_slot_bps  INTEGER      NOT NULL,
    effective_from      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by          TEXT,
    note                TEXT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 同一版本里一个桶只许一行 —— 两行会让「取哪一行」变成随机。
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_slot_conversion_version_bucket
    ON media_slot_conversion_versions (version, bucket);

-- 按版本倒序取「当前版」的常用路径。
CREATE INDEX IF NOT EXISTS ix_media_slot_conversion_version
    ON media_slot_conversion_versions (version DESC);

DO $$
BEGIN
    -- 桶名与 035 逐字一致
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_media_slot_conversion_bucket') THEN
        ALTER TABLE media_slot_conversion_versions
            ADD CONSTRAINT ck_media_slot_conversion_bucket
            CHECK (bucket IN ('focus_media_anchor',
                              'industry_platform_coverage',
                              'douyin_doubao_only'));
    END IF;

    -- 🔴 >= 10000:一个槽至少要一条。小于 10000 意味着「一条顶不止一个槽」,
    --    那是**效果等价**的说法,本单明令不签(见抬头)。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_media_slot_conversion_bps_floor') THEN
        ALTER TABLE media_slot_conversion_versions
            ADD CONSTRAINT ck_media_slot_conversion_bps_floor
            CHECK (posts_per_slot_bps >= 10000);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_media_slot_conversion_version_positive') THEN
        ALTER TABLE media_slot_conversion_versions
            ADD CONSTRAINT ck_media_slot_conversion_version_positive
            CHECK (version >= 1);
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- topics 的桶列(可空 = 老行为)
-- ----------------------------------------------------------------------------
-- 🔴 可空且**不回填**:存量 4428 行没有桶,NULL 一律按 10000 折算 ⇒
--    老单的容量读数逐字等于原来的 COUNT(topics)。
--    回填等于替历史上的每一条选题**猜**它当初是按哪类媒体发的 ——
--    猜出来的桶会直接改变那些单的已用额度。NULL 在这里表示
--    「建于本迁移之前,没有留下口径」,比一个编造的值诚实。
ALTER TABLE topics
    ADD COLUMN IF NOT EXISTS media_bucket VARCHAR(40);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_topics_media_bucket') THEN
        ALTER TABLE topics
            ADD CONSTRAINT ck_topics_media_bucket
            CHECK (media_bucket IS NULL OR media_bucket IN (
                'focus_media_anchor',
                'industry_platform_coverage',
                'douyin_doubao_only'));
    END IF;
END $$;
