-- ============================================================================
-- 平台算力按需铸造 · 第一步(T1 + T2 + 三道护栏)· additive & 幂等
-- 工单:docs/AI-CONTEXT/WORKORDER_ONDEMAND_MINTING_2026-07-29.md
--
-- 本迁移只做一件事:让履约计划能表达 allocation_kind='manufacturer_mint'
-- (平台厂家根跳的按需铸造供给)。
--
-- 🔴 只追加,不重写:
--    生产实测 chk_dealer_fulfillment_allocation_kind 的真实定义是
--      ((allocation_kind='existing_lot' AND source_lot_id IS NOT NULL AND source_hop_seq IS NULL)
--       OR (allocation_kind='jit_incoming' AND source_lot_id IS NULL AND source_hop_seq IS NOT NULL))
--    这里**原样保留这两支**,只追加第三支。照抄仓库里某份旧迁移的枚举会静默删掉在用分支
--    (同类事故已发生过:agent_inventory_transactions type CHECK 生产 16 值 / 旧迁移只列 9 值)。
--
-- 🔴 本迁移**不动** agent_inventory_transactions 的 type CHECK:
--    按需铸造复用已存在的 manufacturer_origin_in;
--    manufacturer_origin_out 属于第二步(T3 存量销毁),不在本批。
-- ============================================================================

BEGIN;

DO $$
DECLARE
    v_def TEXT;
    v_bad BIGINT;
BEGIN
    IF to_regclass('public.dealer_resale_fulfillment_allocations') IS NULL THEN
        RAISE NOTICE 'dealer_resale_fulfillment_allocations 不存在 · 跳过';
        RETURN;
    END IF;

    SELECT pg_get_constraintdef(oid) INTO v_def
      FROM pg_constraint
     WHERE conrelid = 'public.dealer_resale_fulfillment_allocations'::regclass
       AND conname = 'chk_dealer_fulfillment_allocation_kind';

    -- 幂等:已经包含 manufacturer_mint 就什么都不做
    IF v_def IS NOT NULL AND position('manufacturer_mint' IN v_def) > 0 THEN
        RAISE NOTICE 'chk_dealer_fulfillment_allocation_kind 已含 manufacturer_mint · 跳过';
        RETURN;
    END IF;

    -- 形状守卫:只在读到**预期的两支**时才敢重建。定义与预期不符 → 立刻失败,
    -- 由人先看清生产真实约束,绝不盲目 DROP。
    IF v_def IS NULL
       OR position('existing_lot' IN v_def) = 0
       OR position('jit_incoming' IN v_def) = 0 THEN
        RAISE EXCEPTION
            'chk_dealer_fulfillment_allocation_kind 定义不符合预期(实际=%),拒绝重建', v_def;
    END IF;

    -- 前置核验:存量行必须全部落在保留的两支里,否则重建后会当场违反
    SELECT count(*) INTO v_bad
      FROM dealer_resale_fulfillment_allocations
     WHERE NOT (
           (allocation_kind = 'existing_lot'
            AND source_lot_id IS NOT NULL AND source_hop_seq IS NULL)
        OR (allocation_kind = 'jit_incoming'
            AND source_lot_id IS NULL AND source_hop_seq IS NOT NULL)
     );
    IF v_bad > 0 THEN
        RAISE EXCEPTION '存量 % 行不满足保留分支,拒绝重建约束', v_bad;
    END IF;

    ALTER TABLE dealer_resale_fulfillment_allocations
        DROP CONSTRAINT chk_dealer_fulfillment_allocation_kind;
    ALTER TABLE dealer_resale_fulfillment_allocations
        ADD CONSTRAINT chk_dealer_fulfillment_allocation_kind CHECK (
            (allocation_kind = 'existing_lot'
             AND source_lot_id IS NOT NULL AND source_hop_seq IS NULL)
         OR (allocation_kind = 'jit_incoming'
             AND source_lot_id IS NULL AND source_hop_seq IS NOT NULL)
         OR (allocation_kind = 'manufacturer_mint'
             AND source_lot_id IS NULL AND source_hop_seq IS NULL)
        );
END $$;

-- ----------------------------------------------------------------------------
-- dealer_resale_fulfillment_hops.mint_points —— 该跳由平台按需铸造供给的算力
-- 🔴 为什么必须新开一列而不是塞进 jit_shortfall_points:
--    jit_shortfall 的语义是"本卖方没货 → 由上游那一跳补",它同时驱动
--    需求向上游传播与 jit_incoming 血缘校验。铸造没有上游,复用会把
--    "根节点铸造"和"上游补货"混成一个数,审计再也分不开,
--    且 _build_jit_fulfillment_plan 的 "厂家短缺不能由虚拟库存补足" 守卫会误伤。
-- 现有行 mint_points 默认 0,等式 existing+shortfall+mint=points 对存量行恒成立。
-- ----------------------------------------------------------------------------
ALTER TABLE IF EXISTS dealer_resale_fulfillment_hops
    ADD COLUMN IF NOT EXISTS mint_points BIGINT NOT NULL DEFAULT 0;

DO $$
DECLARE
    v_def TEXT;
    v_bad BIGINT;
BEGIN
    IF to_regclass('public.dealer_resale_fulfillment_hops') IS NULL THEN
        RETURN;
    END IF;
    SELECT pg_get_constraintdef(oid) INTO v_def
      FROM pg_constraint
     WHERE conrelid = 'public.dealer_resale_fulfillment_hops'::regclass
       AND conname = 'chk_dealer_fulfillment_hop_values';

    IF v_def IS NOT NULL AND position('mint_points' IN v_def) > 0 THEN
        RAISE NOTICE 'chk_dealer_fulfillment_hop_values 已含 mint_points · 跳过';
        RETURN;
    END IF;

    -- 形状守卫:只在读到预期的 existing+shortfall=points 等式时才敢重建
    IF v_def IS NULL
       OR position('existing_inventory_points' IN v_def) = 0
       OR position('jit_shortfall_points' IN v_def) = 0
       OR position('standard_reference_cents' IN v_def) = 0 THEN
        RAISE EXCEPTION
            'chk_dealer_fulfillment_hop_values 定义不符合预期(实际=%),拒绝重建', v_def;
    END IF;

    SELECT count(*) INTO v_bad
      FROM dealer_resale_fulfillment_hops
     WHERE NOT (
           hop_seq >= 0 AND points > 0
       AND existing_inventory_points >= 0 AND jit_shortfall_points >= 0
       AND COALESCE(mint_points, 0) >= 0
       AND existing_inventory_points + jit_shortfall_points + COALESCE(mint_points, 0) = points
       AND standard_reference_cents > 0
     );
    IF v_bad > 0 THEN
        RAISE EXCEPTION '存量 % 行不满足新等式,拒绝重建约束', v_bad;
    END IF;

    ALTER TABLE dealer_resale_fulfillment_hops
        DROP CONSTRAINT chk_dealer_fulfillment_hop_values;
    ALTER TABLE dealer_resale_fulfillment_hops
        ADD CONSTRAINT chk_dealer_fulfillment_hop_values CHECK (
            hop_seq >= 0
        AND points > 0
        AND existing_inventory_points >= 0
        AND jit_shortfall_points >= 0
        AND mint_points >= 0
        AND (existing_inventory_points + jit_shortfall_points + mint_points) = points
        AND standard_reference_cents > 0
        );
END $$;

-- 后置逐值核验:三支都必须在,少一支即失败(约束名与归属表是 schema_status 契约的一部分)
DO $$
DECLARE
    v_def TEXT;
BEGIN
    IF to_regclass('public.dealer_resale_fulfillment_allocations') IS NULL THEN
        RETURN;
    END IF;
    SELECT pg_get_constraintdef(oid) INTO v_def
      FROM pg_constraint
     WHERE conrelid = 'public.dealer_resale_fulfillment_allocations'::regclass
       AND conname = 'chk_dealer_fulfillment_allocation_kind';
    IF v_def IS NULL
       OR position('existing_lot' IN v_def) = 0
       OR position('jit_incoming' IN v_def) = 0
       OR position('manufacturer_mint' IN v_def) = 0 THEN
        RAISE EXCEPTION '后置核验失败:allocation_kind 约束缺分支(实际=%)', v_def;
    END IF;

    SELECT pg_get_constraintdef(oid) INTO v_def
      FROM pg_constraint
     WHERE conrelid = 'public.dealer_resale_fulfillment_hops'::regclass
       AND conname = 'chk_dealer_fulfillment_hop_values';
    IF v_def IS NULL OR position('mint_points' IN v_def) = 0 THEN
        RAISE EXCEPTION '后置核验失败:hop_values 约束未纳入 mint_points(实际=%)', v_def;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'dealer_resale_fulfillment_hops' AND column_name = 'mint_points'
    ) THEN
        RAISE EXCEPTION '后置核验失败:dealer_resale_fulfillment_hops.mint_points 缺列';
    END IF;
END $$;

COMMIT;
