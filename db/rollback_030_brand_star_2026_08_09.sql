-- 回滚 030:删掉星标两列 + 发布备注列。
-- 🔴 **绝不登记进 migration_manifest.py**(登记 = 上线即把这三列删了)。
--
-- 删列会丢掉"代理标过哪些客户"与"这单当初填的地区备注"这两个事实,不可逆。
-- 只有确认要整体撤掉这两个功能时才跑。
-- 若只是想让排序回到旧样子,更安全的做法是把 api/brand_api.py 的 ORDER BY 改回
-- `b.updated_at DESC NULLS LAST` —— 库里三列留着不碍事(默认值 FALSE/'' 无副作用)。

DROP INDEX IF EXISTS idx_brands_starred;
ALTER TABLE brands DROP COLUMN IF EXISTS starred_at;
ALTER TABLE brands DROP COLUMN IF EXISTS is_starred;
ALTER TABLE mhz_publish_orders DROP COLUMN IF EXISTS order_remark;
