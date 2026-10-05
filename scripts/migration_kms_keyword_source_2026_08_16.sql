-- 迁移 · keyword_monitor_subscriptions 加来源维度(手动词/合同词统一 · P0-1)
-- WO_MONITORING_MANUAL_KEYWORD_PARITY_2026-08-16 · P0-1
-- Owner 2026-08-16:「手动加的此功能和合同加的一模一样,只是前面的标签一个是合同一个是手动」
--
-- 背景(生产 information_schema 实测 2026-08-16):
--   keyword_monitor_subscriptions 17 列,**没有 keyword_source**,而每日跑批写死
--     JOIN confirmed_keywords ck ON ck.id = s.keyword_id
--   ⇒ 两条后果,都必炸:
--     (1) 给手动词建了订阅也永远跑不到(JOIN 匹配不上)= 又一种"看着开、永远不跑"的孤儿;
--     (2) 两张表各自独立自增,今天侥幸零重叠(实测 extra 2–24 共 14 行 /
--         confirmed 526–3074 共 2288 行),**手动词再加约 500 个就进入合同词 id 区间**,
--         一旦重叠,手动词的订阅会 JOIN 到另一个客户的合同词上去跑、去扣。
--
-- 🔴 默认 'confirmed' 是刻意的:生产实测存量 96 行(active 16 / cancelled 78 /
--    paused_low_balance 2)**全部**能 JOIN 上 confirmed_keywords、**零**能 JOIN 上 extra_keywords
--    ⇒ 加列即正确,不需要任何 DML 回填。
--
-- 🔴 迁移零 DML:prestart 每次部署无条件重放全部迁移(无追踪表),
--    写 UPDATE 会在每次部署覆盖代理的人工操作(与 035 同一条纪律)。
--
-- 🔴 漏跑后果**响亮**(刻意):新取数 SQL 显式引用 s.keyword_source
--    → 缺列时 UndefinedColumn 当场抛出、每日取词失败。
--    不选静默兜底 —— 「以为分开了其实串词扣错客户的钱」正是本迁移要防的形态。

ALTER TABLE keyword_monitor_subscriptions
    ADD COLUMN IF NOT EXISTS keyword_source TEXT NOT NULL DEFAULT 'confirmed';

-- CHECK 只允许两个值。加 NOT VALID 再 VALIDATE 会留下"存量不合规也能过"的窗口,
-- 而存量全是 'confirmed'(上面已论证),直接加约束不会失败。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_kms_keyword_source'
          AND conrelid = 'keyword_monitor_subscriptions'::regclass
    ) THEN
        ALTER TABLE keyword_monitor_subscriptions
            ADD CONSTRAINT chk_kms_keyword_source
            CHECK (keyword_source IN ('confirmed', 'extra'));
    END IF;
END $$;

-- ── 两条唯一索引重建成带 keyword_source ─────────────────────────────────────
-- 🔴 先建新的、再删旧的:反过来做会留下一个**无任何唯一约束**的窗口期,
--    而 create_keyword_monitor_subscription 的并发兜底正是靠 UniqueViolation 工作的
--    (see db/monitoring_db.py · v1.7 2026-05-29 并发 unique 冲突兜底),
--    窗口期内并发开关会真的插出重复订阅 = 重复扣费(2026-05-11 那个 P0 的形态)。

-- 旧:UNIQUE (keyword_id) WHERE status IN ('active','paused_low_balance')
--     → 手动词与合同词 id 撞车时会互相顶掉,建不出第二条
-- @index-guard uniq_kms_keyword_source_active ON keyword_monitor_subscriptions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uniq_kms_keyword_source_active' AND i.indrelid = to_regclass('public.keyword_monitor_subscriptions')) THEN
        NULL;  -- 已在 public.keyword_monitor_subscriptions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uniq_kms_keyword_source_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uniq_kms_keyword_source_active 已存在但不在 public.keyword_monitor_subscriptions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uniq_kms_keyword_source_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uniq_kms_keyword_source_active ON public.keyword_monitor_subscriptions (keyword_id, keyword_source) WHERE status IN ('active', 'paused_low_balance');
    END IF;
END $idxguard$;

-- 旧:UNIQUE (brand_id, keyword_id, user_id, COALESCE(quote_id,-1)) WHERE status='active'
-- @index-guard idx_kms_unique_active_v2 ON keyword_monitor_subscriptions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_kms_unique_active_v2' AND i.indrelid = to_regclass('public.keyword_monitor_subscriptions')) THEN
        NULL;  -- 已在 public.keyword_monitor_subscriptions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_kms_unique_active_v2' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_kms_unique_active_v2 已存在但不在 public.keyword_monitor_subscriptions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_kms_unique_active_v2' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX idx_kms_unique_active_v2 ON public.keyword_monitor_subscriptions (brand_id, keyword_id, keyword_source, user_id, COALESCE(quote_id, -1)) WHERE status = 'active';
    END IF;
END $idxguard$;

-- @drop-index-guard uniq_kms_keyword_active ON keyword_monitor_subscriptions if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uniq_kms_keyword_active' AND i.indrelid = to_regclass('keyword_monitor_subscriptions')) THEN
        DROP INDEX IF EXISTS uniq_kms_keyword_active;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uniq_kms_keyword_active' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] uniq_kms_keyword_active 不在 keyword_monitor_subscriptions 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uniq_kms_keyword_active' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
-- @drop-index-guard idx_kms_unique_active ON keyword_monitor_subscriptions if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_kms_unique_active' AND i.indrelid = to_regclass('keyword_monitor_subscriptions')) THEN
        DROP INDEX IF EXISTS idx_kms_unique_active;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_kms_unique_active' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] idx_kms_unique_active 不在 keyword_monitor_subscriptions 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_kms_unique_active' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;

-- 查得快:两臂取数都按 (status, keyword_source) 过滤
-- @index-guard idx_kms_status_source ON keyword_monitor_subscriptions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_kms_status_source' AND i.indrelid = to_regclass('public.keyword_monitor_subscriptions')) THEN
        NULL;  -- 已在 public.keyword_monitor_subscriptions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_kms_status_source' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_kms_status_source 已存在但不在 public.keyword_monitor_subscriptions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_kms_status_source' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_kms_status_source ON public.keyword_monitor_subscriptions (status, keyword_source);
    END IF;
END $idxguard$;

-- §验收(部署后手工跑 · 不跑就不算 migrated):
--   1) 列在且默认对:
--      SELECT column_name, data_type, is_nullable, column_default
--        FROM information_schema.columns
--       WHERE table_schema='public' AND table_name='keyword_monitor_subscriptions'
--         AND column_name='keyword_source';
--      期望 keyword_source / text / NO / 'confirmed'::text
--   2) 存量全部落在 'confirmed'(期望 96 / 0):
--      SELECT keyword_source, count(*) FROM keyword_monitor_subscriptions GROUP BY 1;
--   3) 新旧索引交接干净(期望:两个 v2/source 索引在、两个旧索引不在):
--      SELECT indexname FROM pg_indexes
--       WHERE tablename='keyword_monitor_subscriptions' ORDER BY indexname;
--   4) 🔴 反向对照(证明约束真的在拦,不是恒过):
--      BEGIN;
--        INSERT INTO keyword_monitor_subscriptions (user_id, keyword_id, keyword_source, status)
--        VALUES (1, 999999, 'bogus', 'active');   -- 期望 CHECK 违规报错
--      ROLLBACK;
