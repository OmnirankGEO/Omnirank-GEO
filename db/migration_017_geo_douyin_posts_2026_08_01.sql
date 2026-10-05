-- GEO 抖音图文管线 v1 · 作品库 + 生产任务状态机
-- 工单 WORKORDER_GEO_VIDEO_PIPELINE_V1_2026-08-01.md §5
-- Safe to run repeatedly（全部 IF NOT EXISTS，无破坏性 DDL）。
--
-- 设计要点:
--   1. 作品库【留全量】(Owner 08-01 拍板):它同时是 用户复用底料 / 计费凭证 /
--      **豆包引用归因锚**(飞轮反查"我们发的哪条被引了"靠 published_url ↔ source_url)。
--   2. 只存元数据 + OSS 链接,不存图片二进制(生产盘 71%,硬约束)。
--   3. 软删除(deleted_at),不做物理删除。
--   4. 发布【不自建状态机】:publish_order_id / publish_item_id 外挂到既有
--      媒介盒子订单链(mhz_publish_orders / _items),本表只存引用 + 冗余快照。
--      🔴 本表不参与扣费/退款判定,资金语义仍以既有 publish 链为准。

-- ─────────────────────────────────────────────────────────────
-- 作品表
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS geo_douyin_posts (
    id                  BIGSERIAL PRIMARY KEY,

    -- 归属
    brand_id            INTEGER,
    created_by          INTEGER NOT NULL,
    industry_key        VARCHAR(64),
    city                VARCHAR(32),                 -- 城市变体矩阵的城市维(可空=不限城市)
    keyword             TEXT,                        -- 选题来源关键词

    -- 内容形态(Phase 2 视频接口位:content_type 预留 'video',v1 不实现)
    content_type        VARCHAR(16) NOT NULL DEFAULT 'image_post',

    -- 内容本体
    title               TEXT,                        -- 抖音标题(硬上限 45 字,由应用层校验)
    body_text           TEXT,                        -- 正文文案
    hashtags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    cards               JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- cards 形如 [{"idx":1,"headline":"…","sub":"…","oss_key":"…","oss_url":"…",
    --              "publish_url":"…","status":"ready|failed","error":"…"}]

    -- 产物(只存链接,不存二进制)
    oss_keys            JSONB NOT NULL DEFAULT '[]'::jsonb,
    cover_oss_key       TEXT,

    -- 生命周期
    status              VARCHAR(24) NOT NULL DEFAULT 'draft',
    -- draft → generating → ready → publishing → published / failed

    -- 发布(引用既有媒介盒子订单链,本表不自建订单/资金状态机)
    publish_order_id    BIGINT,
    publish_item_ids    JSONB NOT NULL DEFAULT '[]'::jsonb,
    publish_status      VARCHAR(24),
    published_url       TEXT,                        -- 归因锚:↔ 飞轮 source_url
    published_at        TIMESTAMPTZ,

    -- 审计
    generation_meta     JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 模型/耗时/成本留痕
    deleted_at          TIMESTAMPTZ,                          -- 软删除
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- @index-guard idx_geo_douyin_posts_brand ON geo_douyin_posts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_posts_brand' AND i.indrelid = to_regclass('public.geo_douyin_posts')) THEN
        NULL;  -- 已在 public.geo_douyin_posts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_posts_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_posts_brand 已存在但不在 public.geo_douyin_posts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_posts_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_posts_brand ON public.geo_douyin_posts (brand_id, created_at DESC) WHERE deleted_at IS NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_douyin_posts_creator ON geo_douyin_posts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_posts_creator' AND i.indrelid = to_regclass('public.geo_douyin_posts')) THEN
        NULL;  -- 已在 public.geo_douyin_posts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_posts_creator' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_posts_creator 已存在但不在 public.geo_douyin_posts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_posts_creator' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_posts_creator ON public.geo_douyin_posts (created_by, created_at DESC) WHERE deleted_at IS NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_douyin_posts_status ON geo_douyin_posts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_posts_status' AND i.indrelid = to_regclass('public.geo_douyin_posts')) THEN
        NULL;  -- 已在 public.geo_douyin_posts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_posts_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_posts_status 已存在但不在 public.geo_douyin_posts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_posts_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_posts_status ON public.geo_douyin_posts (status, created_at DESC) WHERE deleted_at IS NULL;
    END IF;
END $idxguard$;
-- 归因反查:飞轮拿 source_url 回来匹配我们发过的作品
-- @index-guard idx_geo_douyin_posts_published_url ON geo_douyin_posts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_posts_published_url' AND i.indrelid = to_regclass('public.geo_douyin_posts')) THEN
        NULL;  -- 已在 public.geo_douyin_posts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_posts_published_url' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_posts_published_url 已存在但不在 public.geo_douyin_posts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_posts_published_url' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_posts_published_url ON public.geo_douyin_posts (published_url) WHERE published_url IS NOT NULL;
    END IF;
END $idxguard$;
-- 频控:按"账号维度"的日发上限统计走 publish 链;这里只支撑"同内容多城市错开"查询
-- @index-guard idx_geo_douyin_posts_city_kw ON geo_douyin_posts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_posts_city_kw' AND i.indrelid = to_regclass('public.geo_douyin_posts')) THEN
        NULL;  -- 已在 public.geo_douyin_posts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_posts_city_kw' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_posts_city_kw 已存在但不在 public.geo_douyin_posts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_posts_city_kw' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_posts_city_kw ON public.geo_douyin_posts (brand_id, keyword, city) WHERE deleted_at IS NULL;
    END IF;
END $idxguard$;

-- ─────────────────────────────────────────────────────────────
-- 生产任务状态机(B 类异步长任务:freeze → commit/release)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS geo_douyin_post_tasks (
    id                  BIGSERIAL PRIMARY KEY,
    post_id             BIGINT NOT NULL REFERENCES geo_douyin_posts(id),
    user_id             INTEGER NOT NULL,

    task_ref            VARCHAR(64) NOT NULL,        -- 与 point_freezes.task_ref 对齐
    freeze_id           BIGINT,                      -- middleware.billing.freeze_points 返回

    status              VARCHAR(24) NOT NULL DEFAULT 'pending',
    -- pending → running → succeeded / failed / released
    stage               VARCHAR(32),                 -- copy | cards | images | upload
    progress_done       INTEGER NOT NULL DEFAULT 0,
    progress_total      INTEGER NOT NULL DEFAULT 0,

    error_msg           TEXT,
    -- 🔴 run 级错误必须落【顶层列】,不埋 jsonb 深处:埋进 jsonb 会让面板"看起来正常"
    --    而实际整批失败(飞轮 _safe_stage 静默 14 天的同型教训)。
    result_meta         JSONB NOT NULL DEFAULT '{}'::jsonb,

    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- task_ref 必须唯一:它是 freeze/commit/release 的对账键,重复会导致退错单
-- @index-guard uq_geo_douyin_post_tasks_ref ON geo_douyin_post_tasks unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_post_tasks_ref' AND i.indrelid = to_regclass('public.geo_douyin_post_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_post_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_post_tasks_ref' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_post_tasks_ref 已存在但不在 public.geo_douyin_post_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_post_tasks_ref' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_post_tasks_ref ON public.geo_douyin_post_tasks (task_ref);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_douyin_post_tasks_post ON geo_douyin_post_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_post_tasks_post' AND i.indrelid = to_regclass('public.geo_douyin_post_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_post_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_post_tasks_post' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_post_tasks_post 已存在但不在 public.geo_douyin_post_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_post_tasks_post' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_post_tasks_post ON public.geo_douyin_post_tasks (post_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_douyin_post_tasks_status ON geo_douyin_post_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_post_tasks_status' AND i.indrelid = to_regclass('public.geo_douyin_post_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_post_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_post_tasks_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_post_tasks_status 已存在但不在 public.geo_douyin_post_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_post_tasks_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_post_tasks_status ON public.geo_douyin_post_tasks (status, created_at DESC);
    END IF;
END $idxguard$;
