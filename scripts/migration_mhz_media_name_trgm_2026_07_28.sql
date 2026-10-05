-- mhz_media.media_name 三元组索引 —— 修 /api/publish/media/recommend-v2 单请求 44 秒
--
-- 【病因】services/placement_service.py::_match_media 里的 _find_best 对每个候选平台
--   打一条 `media_name ILIKE '%关键词%'` 查询。传行业时平台列表来自调研数据,实测
--   建筑建材 = 433 条查询。前导通配符让既有 btree 索引 idx_mhz_media_name 完全用不上,
--   每条都 Seq Scan 52,072 行(实测 93-116 ms/条)→ 433 × 93ms ≈ 40 秒。
--
-- 【为什么是 P0 而不是"慢一点"】生产 WORKERS=1 只有一个 uvicorn 进程。该端点此前
--   还把这个同步函数直接写在 async def 里(同批已修,见 api/publish_api.py),
--   于是这一个请求执行期间**整台服务器停止响应**——同屏 effectiveness-board(自身
--   仅 357 ms)、active-task(80 ms)、notifications(78 ms)全部排队到 nginx 504。
--
-- 【实测】建索引后同参数 41,825 ms → 2,705 ms(15 倍),推荐结果逐条一致:
--   查询语句、候选集、排序公式都没动,只是不再全表扫。435 个关键词里仅 7 个是
--   ≤2 字(trigram 需 ≥3 字符,搜狐/网易/知乎这类救不了),覆盖率 98.4%。
--
-- 【安全性】纯 additive:不改任何列/约束/数据,不影响查询结果,只影响执行计划。
--   回滚 = DROP INDEX public.idx_mhz_media_name_trgm;(索引 4.7 MB,建耗时 0.6 秒)
--
-- 【已在生产手工执行】2026-07-28 由 Deploy-CTO 用 CREATE INDEX CONCURRENTLY 建过
--   (不锁表)。本脚本用 IF NOT EXISTS,生产上是 no-op;登记 manifest 是为了让
--   重建库/新环境不会静默丢掉索引、把这个 44 秒的坑原样复发。
--   这里不用 CONCURRENTLY:迁移在事务里跑,CONCURRENTLY 不允许;52k 行建索引
--   实测 0.6 秒,prestart 阶段(尚未切流量)短暂持锁可接受。
--
-- 生产迁移一律 pin 到 public。

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- @index-guard idx_mhz_media_name_trgm ON mhz_media plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_mhz_media_name_trgm' AND i.indrelid = to_regclass('public.mhz_media')) THEN
        NULL;  -- 已在 public.mhz_media 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_mhz_media_name_trgm' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_mhz_media_name_trgm 已存在但不在 public.mhz_media 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_mhz_media_name_trgm' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_mhz_media_name_trgm ON public.mhz_media USING gin (media_name gin_trgm_ops);
    END IF;
END $idxguard$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'
    ) THEN
        RAISE EXCEPTION 'pg_trgm 扩展未安装,trigram 索引不会生效';
    END IF;

    -- 🔴 必须绑 indrelid:索引名只在 schema 内唯一、**不绑表**。别的表上有同名索引时,
    --    不绑表的这条反查会判"建成了",而 mhz_media 上其实没有 —— 44 秒的坑原样复发,
    --    readiness 却报绿。
    IF NOT EXISTS (
        SELECT 1
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_index ix ON ix.indexrelid = c.oid
         WHERE n.nspname = 'public'
           AND c.relname = 'idx_mhz_media_name_trgm'
           AND c.relkind = 'i'
           AND ix.indrelid = to_regclass('public.mhz_media')
    ) THEN
        RAISE EXCEPTION 'idx_mhz_media_name_trgm 未建在 public.mhz_media 上';
    END IF;

    -- 索引必须 valid + ready,否则 planner 不会用它,现象是"迁移过了但还是 44 秒"。
    IF EXISTS (
        SELECT 1
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_index i ON i.indexrelid = c.oid
         WHERE n.nspname = 'public'
           AND c.relname = 'idx_mhz_media_name_trgm'
           AND i.indrelid = to_regclass('public.mhz_media')
           AND (NOT i.indisvalid OR NOT i.indisready)
    ) THEN
        RAISE EXCEPTION 'idx_mhz_media_name_trgm 存在但 invalid/not-ready';
    END IF;
END
$$;
