-- [P0 代发卡单出口 2026-07-26] awaiting_sync 死胡同:给"自动挽回"计次,给"人工出口"留证。
--
-- 背景(根因):mhz 回 code=200 但无订单号 → item 落 awaiting_sync;12 小时反查不到后
-- mark_item_manual_review 只打 manual_review_required=TRUE,而两个扫描器
-- (find_awaiting_sync_items_to_check / _overdue)的 WHERE 都带 manual_review_required=FALSE
-- → 打完标记这条 item 从所有自动扫描里永久消失,唯一出口是管理员主动翻牌。
-- 线上实证:最老一条滞留 58 天;全库「曾进 awaiting_sync 且已到终态」= 0 笔。
--
-- 本迁移只加三列 + 一个部分索引,纯 additive:
--   awaiting_sync_probe_attempts —— 反查失败计次。没有它就无法表达
--     「失败 ≥3 次 且 滞留 ≥72 小时」这个开人工出口的判据(旧代码无限重试且不计次)。
--   user_exit_claim / _at         —— 用户在 §13 出口上的**声明**(未发布 / 已发布)。
--     刻意只记录不动钱:mhz 已回执"已接收",稿件可能真发出去了,自动退款 =
--     既退钱又发稿。真正退款仍只走 admin_manual_review_resolve('mark_failed'),
--     幂等键唯一为 item:{id}。
--
-- 幂等:全部 IF NOT EXISTS,可连跑。不改任何既有行的业务字段,不动钱,不翻 flag。
-- 运行时另有兜底 ALTER(db/meijiehezi_db.py init 段),但**以本清单为准** ——
-- 运行时代码会无条件读写这三列,漏登记 manifest 会让扫描器直接 UndefinedColumn。

ALTER TABLE mhz_publish_order_items
    ADD COLUMN IF NOT EXISTS awaiting_sync_probe_attempts INTEGER NOT NULL DEFAULT 0;

ALTER TABLE mhz_publish_order_items
    ADD COLUMN IF NOT EXISTS user_exit_claim VARCHAR(32);

ALTER TABLE mhz_publish_order_items
    ADD COLUMN IF NOT EXISTS user_exit_claim_at TIMESTAMP;

-- 出口候选的扫描形状:status='awaiting_sync' + 按滞留时间排序。
-- 注意与既有 idx_mhz_items_awaiting_sync 的差别:那个索引服务的是
-- "12 小时内还在自愈"的查询;本索引额外覆盖 probe_attempts,服务 72 小时出口扫描。
-- @index-guard idx_mhz_items_awaiting_sync_exit ON mhz_publish_order_items plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_mhz_items_awaiting_sync_exit' AND i.indrelid = to_regclass('public.mhz_publish_order_items')) THEN
        NULL;  -- 已在 public.mhz_publish_order_items 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_mhz_items_awaiting_sync_exit' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_mhz_items_awaiting_sync_exit 已存在但不在 public.mhz_publish_order_items 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_mhz_items_awaiting_sync_exit' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_mhz_items_awaiting_sync_exit ON public.mhz_publish_order_items (awaiting_sync_since, awaiting_sync_probe_attempts) WHERE status = 'awaiting_sync';
    END IF;
END $idxguard$;
