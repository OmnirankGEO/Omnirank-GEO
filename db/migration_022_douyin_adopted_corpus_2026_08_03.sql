-- 抖音「被采纳内容」语料库(选题蒸馏器的 few-shot 来源)
--
-- 为什么要单独一张表,而不是直接查 geo_research_source_signals:
--   那张表里**没有形态字段**(只有 domain / title / adoption_rank / round_id)。
--   Review 硬要求「few-shot 优先同行业**图文帖**样本(aweme_type=68)」——
--   而"是不是图文帖"必须问抖音才知道:生产实测 1,677 条被采纳抖音条目
--   **100% 都是 `/share/video/{id}` 路径**,URL 对图文/视频**零判别力**。
--   所以这里落一张离线采集来的语料表,把 aweme_type / image_count 存下来。
--
-- 采集是**离线运维动作**(scripts/research/douyin_corpus_ingest.py,调 TIKHUB,
-- 约 ¥0.0072/条),不在任何请求链上。表空时蒸馏器**降级**(见 topic_distiller),
-- 并把降级原因如实回给前端,不假装有语料。
--
-- 幂等:CREATE TABLE IF NOT EXISTS + UNIQUE(aweme_id) 的 upsert 由写入侧负责。

CREATE TABLE IF NOT EXISTS douyin_adopted_corpus (
    id              BIGSERIAL PRIMARY KEY,
    aweme_id        VARCHAR(32)  NOT NULL UNIQUE,
    industry_key    VARCHAR(100) NOT NULL DEFAULT 'general',
    -- 抖音官方类型码:68 = 图文帖;0/4/61 等 = 视频
    aweme_type      INTEGER,
    is_image_post   BOOLEAN      NOT NULL DEFAULT FALSE,
    image_count     INTEGER      NOT NULL DEFAULT 0,
    -- 🔴 caption 是抖音的 `desc` 全文 = **标题与正文合一的一整段**。
    --    抖音没有独立标题字段(30 条逐字比对:库里的 title 与 desc 前缀
    --    吻合 81%),所以这里只存一列,不拆"标题/正文"两列去迎合我们自己的模型。
    caption         TEXT         NOT NULL,
    hashtags        JSONB        NOT NULL DEFAULT '[]'::jsonb,
    digg_count      INTEGER,
    source_url      TEXT,
    collected_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- few-shot 的取数形状就是 (行业, 是否图文帖) → 按这个建
-- @index-guard idx_douyin_corpus_industry_kind ON douyin_adopted_corpus plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_douyin_corpus_industry_kind' AND i.indrelid = to_regclass('public.douyin_adopted_corpus')) THEN
        NULL;  -- 已在 public.douyin_adopted_corpus 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_douyin_corpus_industry_kind' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_douyin_corpus_industry_kind 已存在但不在 public.douyin_adopted_corpus 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_douyin_corpus_industry_kind' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_douyin_corpus_industry_kind ON public.douyin_adopted_corpus (industry_key, is_image_post);
    END IF;
END $idxguard$;
