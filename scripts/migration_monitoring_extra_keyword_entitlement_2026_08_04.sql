-- R3 · 手动词(extra_keywords.platforms)存量口径清洗 —— 2026-08-04
--
-- 背景(生产实证,只读取证 2026-08-04):
--   extra_keywords id=23(brand 662 / quote 386)platforms =
--   'dashscope,deepseek,doubao,kimi,yuanbao',而它挂靠的 quote 386 下 8 条合同词
--   全是 monitoring-classic4-v1(四路)。手动词与合同词口径不一致 →
--   create_monitoring_run_cells 的 executable ⊆ entitlement 守卫炸掉整批,
--   monitoring_tasks 1543/1544 双 failed(total_tests=29 = 6×4 + 1×5 是其指纹)。
--
--   根因是 add_keyword 抄「产品默认」而不是「这个词挂靠的合同买了什么」;
--   R1 已在 db/monitoring_db.py:resolve_extra_keyword_platforms 从源头修掉。
--   本迁移只负责把**已经写歪的存量行**拉回同一口径。
--
-- 口径(与 R1 helper 及 get_keywords_for_monitoring 的 confirmed JOIN 同表同键):
--   该行 quote 下已确认词的商品版本 → monitoring_product_platform_matrices 矩阵,
--   **逐字**写回。只处理「该 quote 恰好只有一个商品版本」的确定情形;多版本(续单
--   混合)与无确认词的行一律不动,并在尾部自检里点名报出,不静默放过。
--
-- 归属核验(fail-closed):只认 quotes.id = e.quote_id AND quotes.brand_id = e.brand_id
--   的行。brand_id 与 quote_id 是两套自增序列、数值必然会撞(库里就有 brand_id
--   = quote_id = 249 的行),不核验会把别人 quote 的授权矩阵继承过来。
--
-- 🔴 本迁移**刻意不碰任何列默认值**。
--   extra_keywords.platforms / client_keywords.platforms / monitoring_config
--   .default_platforms / confirmed_keywords.monitoring_product_version 这四处默认值
--   被 migration_monitoring_unified5_2026_07_26.sql 尾部的 DO 块**逐字断言**,而
--   prestart 每次部署全量重跑清单。这里改默认值 = 下次部署那条断言必炸
--   ('unified5 monitoring default contract drift')→ prestart 失败 → 两槽都起不来。
--   要改默认值必须和那条迁移的期望值同批改,不在本包范围。
--
-- 幂等:再跑一次时所有行已等于目标值,UPDATE 影响 0 行;provenance 表 ON CONFLICT
--   DO NOTHING 保留**首次**改写前的原值(重跑不会把已修正的值当"原值"覆盖上去)。
--
-- 回滚(人工,证据驱动):
--   UPDATE extra_keywords e SET platforms = b.old_value
--     FROM public.monitoring_extra_keyword_entitlement_backup_20260804 b
--    WHERE b.source_id = e.id;

SET LOCAL search_path = pg_catalog, public;

-- 前置:矩阵表必须在,否则本迁移无从取口径(fail-closed,不猜)。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relname = 'monitoring_product_platform_matrices'
           AND c.relkind = 'r'
           AND c.relpersistence = 'p'
    ) THEN
        RAISE EXCEPTION 'monitoring product matrix table missing; run migration_monitoring_product_matrix_2026_07_21.sql first';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.monitoring_extra_keyword_entitlement_backup_20260804 (
    source_id   BIGINT      NOT NULL PRIMARY KEY,
    quote_id    INTEGER,
    brand_id    INTEGER,
    old_value   TEXT        NOT NULL,
    new_value   TEXT        NOT NULL,
    migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 全表扫描 + 改写 + 留痕,**单条 data-modifying CTE**。
--
-- 🔴 为什么不用 CREATE TEMP TABLE ... ON COMMIT DROP:scripts/prestart.py 是
--    autocommit=True 且把整个文件当**一次** cur.execute 发过去,事务边界是隐式的。
--    ON COMMIT DROP 在这种形态下的存活范围要靠"多语句 simple query = 隐式事务块"
--    这条细则来保证 —— 能成,但判定依赖太细。写成一条语句就没有这个问题,
--    而且"该行应有的口径"这段判定逻辑只存在一份(不会和自检段各写各的漂移)。
--
-- version_count > 1 的 quote 在 HAVING 处被排除,连同"无确认词/归属核不实"的行
-- 都落到尾部自检的点名报告里,不静默放过。
WITH quote_matrix AS (
    SELECT k.quote_id,
           min(m.platforms) AS platforms
      FROM public.confirmed_keywords k
      JOIN public.monitoring_product_platform_matrices m
        ON m.version = k.monitoring_product_version
     GROUP BY k.quote_id
    HAVING count(DISTINCT m.version) = 1
), target AS (
    SELECT e.id          AS source_id,
           e.quote_id    AS quote_id,
           e.brand_id    AS brand_id,
           e.platforms   AS old_value,
           qm.platforms  AS new_value
      FROM public.extra_keywords e
      JOIN public.quotes q
        ON q.id = e.quote_id
       AND q.brand_id = e.brand_id       -- 归属核验:brand 对不上一律不动
      JOIN quote_matrix qm
        ON qm.quote_id = e.quote_id
     WHERE e.platforms IS DISTINCT FROM qm.platforms
), updated AS (
    UPDATE public.extra_keywords e
       SET platforms = t.new_value
      FROM target t
     WHERE e.id = t.source_id
    RETURNING t.source_id, t.quote_id, t.brand_id, t.old_value, t.new_value
)
INSERT INTO public.monitoring_extra_keyword_entitlement_backup_20260804
    (source_id, quote_id, brand_id, old_value, new_value)
SELECT source_id, quote_id, brand_id, COALESCE(old_value, ''), new_value
  FROM updated
ON CONFLICT (source_id) DO NOTHING;

-- 尾部自检:改完之后不允许还有"能判定口径却仍然不一致"的行;
-- 判不了口径的行(多版本 / 无确认词 / 归属核不实)只报数不改,留痕给人看。
DO $$
DECLARE
    still_mismatched   INTEGER;
    unresolved_multi   INTEGER;
    unresolved_noquote INTEGER;
BEGIN
    SELECT count(*)
      INTO still_mismatched
      FROM public.extra_keywords e
      JOIN public.quotes q
        ON q.id = e.quote_id AND q.brand_id = e.brand_id
      JOIN (
            SELECT k.quote_id, min(m.platforms) AS platforms
              FROM public.confirmed_keywords k
              JOIN public.monitoring_product_platform_matrices m
                ON m.version = k.monitoring_product_version
             GROUP BY k.quote_id
            HAVING count(DISTINCT m.version) = 1
      ) qm ON qm.quote_id = e.quote_id
     WHERE e.platforms IS DISTINCT FROM qm.platforms;

    IF still_mismatched <> 0 THEN
        RAISE EXCEPTION 'extra keyword entitlement drift remains: % rows', still_mismatched;
    END IF;

    SELECT count(*)
      INTO unresolved_multi
      FROM public.extra_keywords e
     WHERE e.quote_id IN (
               SELECT k.quote_id
                 FROM public.confirmed_keywords k
                 JOIN public.monitoring_product_platform_matrices m
                   ON m.version = k.monitoring_product_version
                GROUP BY k.quote_id
               HAVING count(DISTINCT m.version) > 1
           );

    SELECT count(*)
      INTO unresolved_noquote
      FROM public.extra_keywords e
     WHERE e.quote_id IS NULL
        OR NOT EXISTS (
               SELECT 1 FROM public.quotes q
                WHERE q.id = e.quote_id AND q.brand_id = e.brand_id
           )
        OR NOT EXISTS (
               SELECT 1 FROM public.confirmed_keywords k
                WHERE k.quote_id = e.quote_id
           );

    RAISE NOTICE 'extra keyword entitlement cleanup: multi_version_left=%, unresolvable_left=%',
        unresolved_multi, unresolved_noquote;

    IF current_setting('session_replication_role') IS DISTINCT FROM 'origin' THEN
        RAISE EXCEPTION 'session_replication_role must be origin';
    END IF;
END $$;
