-- 025 · 一键蒸馏选题改异步:任务表(WO-DISTILL-TIMEOUT-ASYNC-2026-08-05)
--
-- 为什么要一张新表,而不是复用 geo_douyin_post_tasks:
--   那张表 `post_id BIGINT NOT NULL REFERENCES geo_douyin_posts(id)` ——
--   蒸馏发生在**还没有作品之前**(它的产出才是用户挑选题的依据),没有 post_id 可填。
--
-- 为什么不用进程内字典存任务:
--   ① 生产 WORKERS=1 是 .env 强制的,不是代码保证的。有人把它调回 compose 默认的 4,
--      提交打到 worker A、轮询打到 worker B → 任务"查无此单",而且是**静默**的;
--   ② 部署很频繁(台账里有 4 小时滚 4 批的记录),重启会让在飞的任务凭空消失。
--   落库两条都免疫,代价只是一张表。
--
-- 幂等:全 IF NOT EXISTS,可重复跑。不依赖任何前置迁移(不引用其它新表)。
--
-- 🔴 漏跑这条的后果是**响亮的**:提交蒸馏时 INSERT 抛 UndefinedTable →
--    端点 500 → 前端拿到人话失败。不会静默变成"提交了但永远查不到"。
--    (刻意选响亮:静默失败在这条链上意味着用户干等,比报错更糟。)
-- 🔴 rollback_025_*.sql 同样【绝不登记】。

CREATE TABLE IF NOT EXISTS geo_douyin_distill_tasks (
    id                  BIGSERIAL PRIMARY KEY,
    brand_id            BIGINT NOT NULL,
    user_id             INTEGER NOT NULL,

    status              VARCHAR(24) NOT NULL DEFAULT 'pending',
    -- pending → running → succeeded / failed
    stage               VARCHAR(32) NOT NULL DEFAULT 'queued',
    -- queued | gathering(查知识库/语料/竞品) | distilling(LLM) | done

    -- 🔴 失败原因落【顶层列】不埋 jsonb:埋进去会让面板"看起来正常"而实际整批失败
    --    (同 geo_douyin_post_tasks.error_msg 的理由,飞轮静默 14 天的同型教训)。
    error_code          VARCHAR(64),
    error_msg           TEXT,

    keywords_used       JSONB NOT NULL DEFAULT '[]'::jsonb,
    result              JSONB NOT NULL DEFAULT '{}'::jsonb,   -- DistillResult.to_dict()

    -- 审计用:这一单到底扣没扣。charge_on_success 成功走完才置 true。
    -- 🔴 它是**记录**不是**开关** —— 扣费判定权在 middleware.billing,不在这一列。
    charged             BOOLEAN NOT NULL DEFAULT FALSE,

    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 🔴 同一个客户同时只能有一个在飞的蒸馏 —— **DB 级**保证,不是靠进程内字典。
--    改异步之后这条特别要紧:请求 2 秒就返回了,进程内那把锁如果跟着请求释放,
--    用户连点两下就是两次真调用、两次 130。这个部分唯一索引让第二次 INSERT 直接失败,
--    哪怕 WORKERS 被调成 4、哪怕两个请求真并发。
-- @index-guard uq_geo_douyin_distill_inflight ON geo_douyin_distill_tasks unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_distill_inflight' AND i.indrelid = to_regclass('public.geo_douyin_distill_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_distill_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_distill_inflight' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_distill_inflight 已存在但不在 public.geo_douyin_distill_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_distill_inflight' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_distill_inflight ON public.geo_douyin_distill_tasks (brand_id) WHERE status IN ('pending', 'running');
    END IF;
END $idxguard$;

-- @index-guard idx_geo_douyin_distill_brand ON geo_douyin_distill_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_distill_brand' AND i.indrelid = to_regclass('public.geo_douyin_distill_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_distill_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_distill_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_distill_brand 已存在但不在 public.geo_douyin_distill_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_distill_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_distill_brand ON public.geo_douyin_distill_tasks (brand_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_douyin_distill_status ON geo_douyin_distill_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_distill_status' AND i.indrelid = to_regclass('public.geo_douyin_distill_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_distill_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_distill_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_distill_status 已存在但不在 public.geo_douyin_distill_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_distill_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_distill_status ON public.geo_douyin_distill_tasks (status, created_at DESC);
    END IF;
END $idxguard$;
