-- ============================================================================
-- migration_065 · mhz_refund_requests 加可空的 requested_by(WO_310 · 2026-09-27)
-- ============================================================================
-- 为什么:管理员代客户申请媒介盒子退款时,原代码把申请记在管理员自己名下(user_id = 管理员)⇒
--   批准后算力退进管理员钱包,付款的客户拿不到。WO_310 改成 user_id = 受益人(付款人
--   mhz_publish_orders.user_id),另记 requested_by = 提出申请的人(管理员代申请时是管理员)。
--   表上原本只有审核人 reviewed_by,没有申请人列(生产快照 08-19 核过,10 列)。
-- 🔴 可空、不回填:历史记录不处理(Owner 口径)。
-- 🔴 对生产的影响只有这一次加列:先查 information_schema,真缺才 ALTER;
--    不裸写 ADD COLUMN IF NOT EXISTS(那句即使列已存在也先拿 AccessExclusive 锁,每次部署都排队)。
-- mhz_refund_requests 由 prestart 先引导的 db.meijiehezi_db.init_mhz_tables 建出;表不存在则跳过。
-- 锁:tests/wo310_mhz_refund_payer_2026_09_27
-- ============================================================================
DO $do$
BEGIN
    IF to_regclass('public.mhz_refund_requests') IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'mhz_refund_requests' AND column_name = 'requested_by'
    ) THEN
        ALTER TABLE public.mhz_refund_requests ADD COLUMN requested_by integer;
    END IF;
END
$do$;
