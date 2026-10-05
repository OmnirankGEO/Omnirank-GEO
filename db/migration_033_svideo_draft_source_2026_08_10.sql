-- ============================================================================
-- 033 · 短视频草稿记住自己的来源(WO_P1_SVIDEO_BRAND_MISATTRIBUTION_2026-08-10 修法 4)
--
-- ## 为什么必须加这一列
--
-- svideo 下单时用 `-draft_id` 当 `article_id`(负号命名空间)。而 `create_order` 里那条
-- 服务端兜底查的是 `articles a JOIN quotes q ON q.id = a.quote_id WHERE a.id = %s` ——
-- 传进去的是**负数**,`articles` 里永远没有这行 → `_brand_fallback` **恒为 None**
-- → `it.get("brand_id") or _brand_fallback` 取到的永远是**客户端传来的值**。
--
-- 也就是说:软文有一条服务端校验通道,**svideo 一条都没有**,结构性的。
-- 生产实证(2026-08-10 只读):svideo 全库 3 条,**2 条错记到别的真实客户名下**(66.7%),
-- 且两个受害方分属不同服务商。同一条草稿内部自相矛盾 ——
-- `customer_name`="深圳市晨光富士电梯"(对) 而 `brand_id`=698 美构海外仓(错)。
--
-- 草稿表**没有任何来源列**,所以服务端事后无从查起。这一列就是把"这份内容从哪来"
-- 落进数据,让服务端**不依赖客户端**也能算出权威归属。
--
-- ## 纯 additive
--
-- 只加一列(可空)+ 一条部分索引。不动任何既有列的值/默认值/约束,不加 NOT NULL,
-- 不加外键(geo 与 mhz 是两条独立管线,外键会把两边的生命周期焊死)。
-- 幂等:IF NOT EXISTS,可重复跑。
--
-- ## 🔴 漏跑的后果是**响亮的**(刻意选的方向)
--
-- 写入侧 `create_short_video_draft` 的 INSERT **显式带 geo_post_id 列** ——
-- 列不存在 → psycopg2 UndefinedColumn 抛出 → 短视频下单当场失败。
-- 不选静默:静默失败 = 列加了但一行没落上,而"以为已经能追溯了"的假象
-- 比下单挂掉更难发现(本仓「加列 ≠ 拆完三步」的前车之鉴)。
--
-- ## 🔴 存量 3 行不回填
--
-- 存量里 2 条是错记录,回填等于把错的归属"确认"下来。它们的订正是**另一件事**,
-- 需要备份 + dry-run + Owner 过目(工单 §存量处置),不由迁移代拍。
-- ============================================================================

ALTER TABLE mhz_short_video_drafts
    ADD COLUMN IF NOT EXISTS geo_post_id BIGINT;

COMMENT ON COLUMN mhz_short_video_drafts.geo_post_id IS
    '内容来源:geo_douyin_posts.id。服务端据此反查权威归属品牌,不信客户端传的 brand_id。'
    'NULL = 非创作中心来源(用户自己上传的视频),此时无权威来源可查,退回客户端值 + RBAC。';

-- 只索引有来源的那些行(NULL 占多数且从不按 NULL 查)
-- @index-guard idx_mhz_svideo_drafts_geo_post ON mhz_short_video_drafts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_mhz_svideo_drafts_geo_post' AND i.indrelid = to_regclass('public.mhz_short_video_drafts')) THEN
        NULL;  -- 已在 public.mhz_short_video_drafts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_mhz_svideo_drafts_geo_post' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_mhz_svideo_drafts_geo_post 已存在但不在 public.mhz_short_video_drafts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_mhz_svideo_drafts_geo_post' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_mhz_svideo_drafts_geo_post ON public.mhz_short_video_drafts (geo_post_id) WHERE geo_post_id IS NOT NULL;
    END IF;
END $idxguard$;

-- ============================================================================
-- §验收(部署后手工跑这两条确认)
--   1) 列在:
--      SELECT column_name, data_type, is_nullable
--        FROM information_schema.columns
--       WHERE table_name='mhz_short_video_drafts' AND column_name='geo_post_id';
--      期望:geo_post_id | bigint | YES
--   2) 索引在:
--      SELECT indexname FROM pg_indexes
--       WHERE tablename='mhz_short_video_drafts' AND indexname='idx_mhz_svideo_drafts_geo_post';
-- ============================================================================
