-- 030 · 客户星标/置顶 + 发布订单备注(WO_CUSTOMER_FEEDBACK_4ITEMS_2026-08-09 ③②)
--
-- 客户反馈两条,合成一条 additive 迁移(都只加列,不动既有列、不动默认值、不动约束):
--
-- ③ 星标/置顶客户
--    现状:`GET /api/my-clients` 恒 `ORDER BY b.updated_at DESC NULLS LAST`,
--         而 `updated_at` 被**任何一次字段编辑**顶到最前 —— 改一下备注就窜到第一,
--         与"这个客户最近在被服务"毫无关系。代理手上 30-50 个客户时找人靠翻页。
--    加两列:`is_starred`(星标)+ `starred_at`(星标时间,用于同为星标时的稳定次序)。
--    🔴 只加列不改排序默认值 —— 排序口径在 `api/brand_api.py` 里,不在库里。
--
-- ② 发布下单备注
--    `mhz_publish_orders` 加 `order_remark`,让"确认后重发"这条链能拿回用户当初
--    填的地区备注(首次提交走请求体,重发时请求体已经不在了)。
--
-- ══════════════════════════════════════════════════════════════════
-- 🔴 漏跑的后果(两条方向不同,分别写清楚)
-- ══════════════════════════════════════════════════════════════════
--   ③ **响亮**:`api/brand_api.py` 的列表 SQL 会 `SELECT b.is_starred` 并按它排序,
--     列不在 → UndefinedColumn → /api/my-clients 直接 500 → 客户列表整页打不开。
--     刻意选响亮:客户列表是代理每天第一眼看的页面,静默降级成"星标点了没反应"
--     会被当成功能坏了反复投诉,不如当场炸给部署看。
--   ② **静默**:写入侧 `UPDATE ... SET order_remark` 包在 try 里(见 api 侧注释),
--     列不在就跳过 → 首次提交照常带备注(走请求体),只有"确认后重发"那一次拿不到,
--     退回本次修复前的样子。刻意选静默:发布主链绝不能被一个观测性列挡住。
--
-- 🔴 顺序无依赖:两条都是 `ADD COLUMN IF NOT EXISTS`,幂等,可重复执行,
--    不引用任何其它迁移建立的对象(brands / mhz_publish_orders 都是自古就有的表)。
-- 🔴 回滚见 `rollback_030_brand_star_2026_08_09.sql`(**不登记 manifest**)。

-- ══════════════════════════════════════════════════════════════════
-- ③ 客户星标
-- ══════════════════════════════════════════════════════════════════
ALTER TABLE brands ADD COLUMN IF NOT EXISTS is_starred BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE brands ADD COLUMN IF NOT EXISTS starred_at TIMESTAMP;

COMMENT ON COLUMN brands.is_starred IS
    '客户星标/置顶(代理自己按,与商业归属/计费无关)。排序口径在 api/brand_api.py:list_my_clients。';
COMMENT ON COLUMN brands.starred_at IS
    '最近一次被标星的时间。仅用于多个星标客户之间的稳定次序;取消星标时置 NULL。';

-- 星标是少数派(预期个位数/代理),部分索引比全表索引小得多,且列表 SQL 一定带 is_starred。
-- @index-guard idx_brands_starred ON brands plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_brands_starred' AND i.indrelid = to_regclass('public.brands')) THEN
        NULL;  -- 已在 public.brands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_brands_starred' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_brands_starred 已存在但不在 public.brands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_brands_starred' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_brands_starred ON public.brands (owner_user_id, starred_at DESC) WHERE is_starred = TRUE;
    END IF;
END $idxguard$;

-- ══════════════════════════════════════════════════════════════════
-- ② 发布下单备注
-- ══════════════════════════════════════════════════════════════════
ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS order_remark TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN mhz_publish_orders.order_remark IS
    '下单备注(客户反馈② · 非必选)。少数媒体不按地区分站,只能靠备注指定投放地区。'
    '首次提交走请求体,这一列是给"确认后重发"链路回读用的。';

-- ══════════════════════════════════════════════════════════════════
-- 验收(部署后跑这三条,三条都必须返 t / 非零)
-- ══════════════════════════════════════════════════════════════════
-- SELECT count(*)=2 FROM information_schema.columns
--  WHERE table_name='brands' AND column_name IN ('is_starred','starred_at');
-- SELECT count(*)=1 FROM information_schema.columns
--  WHERE table_name='mhz_publish_orders' AND column_name='order_remark';
-- SELECT to_regclass('public.idx_brands_starred') IS NOT NULL;
