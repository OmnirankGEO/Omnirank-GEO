-- actor-artifact 表的 brand_id 列(publish_orders / publish_batches / diagnosis_records)
-- —— 对既有「自愈式 DDL」的补登记。与团队短代码同批,同一治理动作。
--
-- 【为什么这两列也算高危】
--   services/organization_schema_contract.py 的目录指纹除了 28 张 organization_* 表整表,
--   还覆盖 11 张 legacy actor-artifact 表上的 12 个隔离列(ACTOR_ARTIFACT_COLUMNS),
--   **brand_id 是其中第一个**。也就是说 publish_orders.brand_id / diagnosis_records.brand_id
--   一旦增减,整个 catalog fingerprint 就变 —— 与 2026-07-28 短代码事故完全同一条链路:
--     ① scripts/verify_unified_release_readiness.py 部署就绪门 fail-closed;
--     ② 🔴 server.py::_organization_schema_readiness_gate 容器启动门 fail-closed(新容器起不来)。
--
-- 【它们此前只由业务代码建,且失败静默】
--   publish_orders.brand_id / publish_batches.brand_id + idx_po_brand
--       ← db/publish_db.py::init_publish_tables(),整块包在 `except Exception: pass` 里;
--   diagnosis_records.brand_id
--       ← db/diagnosis_db.py::init_db() 里的 _safe_add_column(),该 helper 同样 `except: pass`。
--   两者都在 server.py **导入期**跑(db/diagnosis_db.py 末尾模块级 init_db();publish 在路由注册块),
--   早于 startup 事件里的指纹门,所以平时"看起来没事"。但这个"没事"依赖两个脆弱前提:
--     (1) 导入顺序不变 —— 一旦有人把它挪进懒加载路径,就是短代码事故的翻版;
--     (2) 那两个裸 except 从不吞掉真实失败 —— 一旦吞掉,列悄悄没建,指纹漂移,
--         而**异常已经被吃掉,日志里什么都看不到**,只剩一个起不来的容器。
--   ⚠️ `scripts/alter_columns.sql:264` 里虽有等价的 diagnosis_records.brand_id 语句,
--      但那个文件**没有登记在 db/migration_manifest.py**,prestart 根本不会执行它。
--
-- 【本迁移在生产是 no-op】三列在生产早已存在(生产契约变体 production_reanchor_short_code_v1
--   已把 publish_orders.brand_id 计入)。全部 IF NOT EXISTS,连跑无副作用。
--   登记的意义:让 prestart 确定性建立这三列,不再依赖导入顺序、也不再被裸 except 掩盖。
--
-- 【为什么用 IF EXISTS 守卫表】publish_orders / publish_batches / diagnosis_records 都是
--   **运行时代码建的 legacy 表**(不归任何迁移所有)。全新空库在 prestart 阶段这些表还不存在,
--   此时本迁移必须安静跳过而不是报错。这也正是遗留问题所在,见交付说明的"残留缺口"一节。
--
-- 【安全性】纯 additive:只加列/索引,不改既有列、不删、不改数据、不翻 flag。
--
-- 生产迁移一律 pin 到 public。

-- publish_orders / publish_batches —— 与 db/publish_db.py init_publish_tables() 逐字等价
ALTER TABLE IF EXISTS public.publish_orders
    ADD COLUMN IF NOT EXISTS brand_id INTEGER;

ALTER TABLE IF EXISTS public.publish_batches
    ADD COLUMN IF NOT EXISTS brand_id INTEGER;

DO $$
BEGIN
    IF to_regclass('public.publish_orders') IS NOT NULL THEN
        -- @index-guard idx_po_brand ON publish_orders plain
        IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                    WHERE c.relname = 'idx_po_brand' AND i.indrelid = to_regclass('public.publish_orders')) THEN
            NULL;  -- 已在 public.publish_orders 上 → 幂等跳过
        ELSIF EXISTS (SELECT 1 FROM pg_class c
                       WHERE c.relname = 'idx_po_brand' AND c.relnamespace = 'public'::regnamespace) THEN
            RAISE EXCEPTION '[index-guard] idx_po_brand 已存在但不在 public.publish_orders 上(实际宿主:%)—— 拒绝静默跳过',
                (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
                   LEFT JOIN pg_index i ON i.indexrelid = c.oid
                   LEFT JOIN pg_class t ON t.oid = i.indrelid
                  WHERE c.relname = 'idx_po_brand' AND c.relnamespace = 'public'::regnamespace)
                USING ERRCODE = 'duplicate_object';
        ELSE
            CREATE INDEX idx_po_brand ON public.publish_orders (brand_id) WHERE brand_id IS NOT NULL;
        END IF;
    END IF;
END
$$;

-- diagnosis_records —— 与 db/diagnosis_db.py 的
-- _safe_add_column(cursor,"diagnosis_records","brand_id","INTEGER REFERENCES brands(id)") 等价。
-- 必须同时守卫 brands:缺了它 FK 建不出来,会把整条 prestart 打挂。
DO $$
BEGIN
    IF to_regclass('public.diagnosis_records') IS NOT NULL
       AND to_regclass('public.brands') IS NOT NULL THEN
        ALTER TABLE public.diagnosis_records
            ADD COLUMN IF NOT EXISTS brand_id INTEGER REFERENCES public.brands(id);
    END IF;
END
$$;

-- fail-closed 自验:表在但列没建出来 = 指纹一定会漂,当场炸掉,别留到启动门才发现。
--
-- 🔴 这里必须先把 to_regclass 的结果存进 oid 变量再比,**不能**写成
--    `to_regclass('public.x') IS NOT NULL AND ... attrelid = 'public.x'::regclass`:
--    SQL 的 AND 不保证短路求值,表不存在时右边那个 ::regclass 转换仍会被求值并抛
--    UndefinedTable —— 于是"安静跳过"变成"迁移炸掉",全新库的 prestart 直接挂。
DO $$
DECLARE
    v_rel     oid;
    v_brands  oid;
    v_missing TEXT := '';
BEGIN
    v_rel := to_regclass('public.publish_orders');
    IF v_rel IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = v_rel AND attname = 'brand_id'
           AND attnum > 0 AND NOT attisdropped
    ) THEN
        v_missing := v_missing || ' publish_orders.brand_id';
    END IF;

    v_rel := to_regclass('public.publish_batches');
    IF v_rel IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = v_rel AND attname = 'brand_id'
           AND attnum > 0 AND NOT attisdropped
    ) THEN
        v_missing := v_missing || ' publish_batches.brand_id';
    END IF;

    v_rel := to_regclass('public.diagnosis_records');
    v_brands := to_regclass('public.brands');
    IF v_rel IS NOT NULL AND v_brands IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = v_rel AND attname = 'brand_id'
           AND attnum > 0 AND NOT attisdropped
    ) THEN
        v_missing := v_missing || ' diagnosis_records.brand_id';
    END IF;

    IF v_missing <> '' THEN
        RAISE EXCEPTION 'actor-artifact brand_id 补登记失败,缺列:%', v_missing;
    END IF;
END
$$;
