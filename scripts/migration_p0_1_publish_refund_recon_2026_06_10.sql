-- =====================================================================
-- P0-1 代发退款不对账 · schema migration(交 Deploy · 部署前先跑)
-- =====================================================================
-- 作者:资金链 CTO  日期:2026-06-10  分支:fix/audit-fund-2026-06-10  基线:bb83d518
-- 配套代码:
--   db/meijiehezi_db.py  — create_order 落 admin_exempt/actually_deducted + refund_for_publish_order 加 cap
--   api/meijiehezi_api.py — 两处下单把 billing_result.admin_exempt 传入 create_order
--
-- 性质:幂等(ADD COLUMN IF NOT EXISTS) · 不动现有数据 · 可安全重复跑。
-- 顺序:本 migration 先于代码蓝绿上线(新代码 INSERT 引用新列)。
--      代码 init_meijiehezi_tables() 内有同款自愈 ALTER 兜底,但显式先跑更稳(DB→代码 顺序铁律)。
--
-- 字段语义:
--   admin_exempt             : 下单是否 admin 免扣(免扣 deduct=0,失败退款须退 0,防凭空注入)
--   actually_deducted_points : 订单真实扣费积分(退款 cap = 真实扣费 - 已退;admin 免扣=0;
--                              老订单=NULL → 退款维持原行为,历史凭空靠 CLAWBACK 回收 SQL 清)
-- =====================================================================

ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS admin_exempt BOOLEAN DEFAULT FALSE;
ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS actually_deducted_points INTEGER;

-- 复核(只读):
--   \d mhz_publish_orders
--     → 应见 admin_exempt boolean default false · actually_deducted_points integer
--   SELECT COUNT(*) AS legacy_null FROM mhz_publish_orders WHERE actually_deducted_points IS NULL;
--     → 预期 = 迁移前现有订单行数(老订单全 NULL,新订单才落值)
-- =====================================================================
