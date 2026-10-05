-- ============================================================================
-- 037 · keyword_monitor_subscriptions 加付款方维度 billing_mode
-- [WO_MONITORING_PLATFORM_COVERED_AND_NOTIFY 2026-08-16 · §2]
--
-- 🔴 这是**加法**,不是减法。
--   2026-06-10 audit P0-4 特意把计费主体从"操作者"改成 brands.owner_user_id,
--   防的是 C 端 token-only 场景下扣费落到错的人头上。
--   **禁止**用"运行时改回认 sub.user_id"来实现平台承担 —— 那等于把审计修复倒回去。
--   正确做法:在订阅上加显式的付款方维度,默认值让老行为**逐字不变**。
--
-- 🔴 零 DML(prestart 每次部署无条件重放全部迁移):
--   存量 96 行靠 DEFAULT 'brand_owner' 落值 = **加列即正确**,不需要任何 UPDATE。
--   §5 岱林那 2 条的调整走独立幂等脚本,不写进迁移 ——
--   写进来会在每次部署把人工调整覆盖回去。
--
-- 🔴 漏跑后果响亮(刻意):scheduler 的计费主体解析显式读 billing_mode,
--   列不存在会 UndefinedColumn 当场抛出。选响亮不选静默 ——
--   「以为记平台账、实际扣了服务商」正是本单要消灭的形态。
-- ============================================================================

ALTER TABLE keyword_monitor_subscriptions
    ADD COLUMN IF NOT EXISTS billing_mode TEXT NOT NULL DEFAULT 'brand_owner';

-- CHECK 白名单:只有这两种付款方。加第三种必须回来改这里 + 改解析函数,
-- 不允许靠"传什么算什么"悄悄扩展。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_kms_billing_mode'
           AND conrelid = 'keyword_monitor_subscriptions'::regclass
    ) THEN
        ALTER TABLE keyword_monitor_subscriptions
            ADD CONSTRAINT chk_kms_billing_mode
            CHECK (billing_mode IN ('brand_owner', 'platform'));
    END IF;
END $$;

-- 「平台一共垫了多少」要能一条 SQL 回答 ⇒ 给平台承担的订阅一个可用索引。
-- @index-guard idx_kms_billing_mode ON keyword_monitor_subscriptions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_kms_billing_mode' AND i.indrelid = to_regclass('public.keyword_monitor_subscriptions')) THEN
        NULL;  -- 已在 public.keyword_monitor_subscriptions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_kms_billing_mode' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_kms_billing_mode 已存在但不在 public.keyword_monitor_subscriptions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_kms_billing_mode' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_kms_billing_mode ON public.keyword_monitor_subscriptions (billing_mode) WHERE billing_mode <> 'brand_owner';
    END IF;
END $idxguard$;
