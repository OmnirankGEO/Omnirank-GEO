-- ============================================================================
-- 031 · 媒体上架档位独立字段(mhz_media.listing_slot)
--       工单 WO_VOCAB_CONVERGENCE_2026-08-09 §3-L5 · Owner 2026-08-09 批 (c) 叠加标记
--
-- 存在的理由:「套餐系列 / 十元专区 / 最新秒杀」是**上架档位**,不是内容体裁,
--   却和「新闻资讯」「汽车网站」并排躺在 resource_type_name / category 里。
--   下游把这两列当行业用(publish_db.get_media_industries 的 DISTINCT 行业下拉、
--   distill_media_effective_pool 的 `resource_type_name AS industry`),
--   于是"十元专区"会作为一个**行业**出现在行业候选里,并参与行业匹配。
--
-- 2026-08-09 生产只读实测(mhz_media 全表 52166 行):
--   resource_type_name 侧 套餐系列 664 / 十元专区 383 / 最新秒杀 182 = 1229 行(active 1217)
--   category           侧 套餐系列 330 / 十元专区 168 / 最新秒杀  50 =  548 行(是上面 1229 的子集)
--
-- 🔴 纯 additive:只加一列 + 一个 CHECK + 一个部分索引,**不动任何既有列的值**。
--    Owner 选 (c):resource_type_name / category 原值一个字不改 → 零信息损失、零回归。
--    列可空;NULL = "这行不是价格档"(绝大多数行)。
--
-- 🔴 漏跑的后果是**响亮的**(刻意选的方向):写入侧 _upsert_media 的 INSERT 明确带
--    listing_slot 列,列不存在 → psycopg2 UndefinedColumn 抛出 → 媒体同步整批失败并告警。
--    选响亮不选静默:这条链静默失败 = 加了列却一行没落上,而"回填过了"的假象
--    比同步挂掉更难发现(本仓已有「加列 ≠ 拆完三步」的前车之鉴)。
--
-- 🔴 顺序无依赖:不引用任何其他迁移,可排在清单任何位置。幂等(IF NOT EXISTS + DO $$ 守卫)。
-- 🔴 回滚见 db/rollback_031_media_listing_slot_2026_08_09.sql(**绝不登记进 manifest**)。
-- ============================================================================

ALTER TABLE mhz_media ADD COLUMN IF NOT EXISTS listing_slot TEXT;

-- 取值域钉死在三个已知档位(+ NULL)。上游哪天新增第四个档位,这条 CHECK 会**响亮地**
-- 拦下同步 —— 那正是我们想要的:新档位必须先被人看见,再进词表,而不是又一次静默混进体裁列。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'mhz_media_listing_slot_check'
    ) THEN
        ALTER TABLE mhz_media
            ADD CONSTRAINT mhz_media_listing_slot_check
            CHECK (listing_slot IS NULL OR listing_slot IN ('套餐系列', '十元专区', '最新秒杀'));
    END IF;
END $$;

-- 部分索引:只索引非空的 ~1229 行(全表 52166 行的 2.4%),消费侧按档位过滤走它。
-- @index-guard idx_mhz_media_listing_slot ON mhz_media plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_mhz_media_listing_slot' AND i.indrelid = to_regclass('public.mhz_media')) THEN
        NULL;  -- 已在 public.mhz_media 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_mhz_media_listing_slot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_mhz_media_listing_slot 已存在但不在 public.mhz_media 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_mhz_media_listing_slot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_mhz_media_listing_slot ON public.mhz_media (listing_slot) WHERE listing_slot IS NOT NULL;
    END IF;
END $idxguard$;

COMMENT ON COLUMN mhz_media.listing_slot IS
    '上架档位(套餐系列/十元专区/最新秒杀)· NULL=普通媒体。'
    'WO_VOCAB_CONVERGENCE-2026-08-09 §3-L5:这三个值原本混在 resource_type_name/category 里被当成行业。'
    'Owner 批 (c) 叠加标记:原列值保持不变,消费侧凭本列判断"该行类目其实不是类目"。';

-- 部署后自检(部署单照抄这两条):
--   SELECT count(*) FROM information_schema.columns
--    WHERE table_name='mhz_media' AND column_name='listing_slot';           -- 期望 1
--   SELECT listing_slot, count(*) FROM mhz_media GROUP BY 1 ORDER BY 2 DESC; -- 回填前:全 NULL
