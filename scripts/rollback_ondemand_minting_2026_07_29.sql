-- ============================================================================
-- 回滚 · 按需铸造第一步(migration_ondemand_minting_2026_07_29.sql)
--
-- 🔴 这份脚本的用途很窄,先说清它解决什么:
--   让**旧代码镜像**(生产尖 `80132d5f` 及更早)重新能通过启动守卫
--   `services.dealer_inventory_resale.schema_status()` 里
--   `STRICT_CONSTRAINT_DEFINITIONS["chk_dealer_fulfillment_hop_values"]`
--   的**定义字符串全等**比对。
--
--   按需铸造包把该期望定义从
--     ... AND (existing_inventory_points + jit_shortfall_points) = points ...
--   改成
--     ... AND mint_points >= 0 AND (existing + jit_shortfall + mint_points) = points ...
--   并用迁移同步改了库。新代码 × 迁移后库相等 → 起得来;
--   **旧代码 × 迁移后库不等 → 容器 fail-closed 起不来**,即"版本回退能力作废"。
--   本脚本把库那一侧还原,回退才成为可能。
--
-- 📌 由 Deploy-CTO 于 2026-07-30 部署当天自备并**已 dry-run 验证**:
--   显式 `BEGIN … ROLLBACK` 执行 → psql 退出码 0 + NOTICE「逐字节一致」,
--   且事后复查线上定义仍含 mint_points(证明真回滚、未误改生产)。
--   证据与背景见 docs/AI-CONTEXT/ROLLBACK_DEPENDENCY_LEDGER_2026-07-30.md
--
-- 🔴🔴 时效条件(最重要的一条):
--   一旦真发生过铸造(存在 mint_points > 0 的行),旧等式
--   existing + jit_shortfall = points 在那些行上**不成立**。
--   本脚本会在前置守卫处 RAISE 并整体回滚,**绝不删改任何数据行**。
--   那种情况下"回退到旧代码"已不可行,只能前滚修复。
--
-- 🔴 只还原这一条约束。故意保留的两样:
--   · `dealer_resale_fulfillment_hops.mint_points` 列 —— 旧代码的列契约只查"缺列",
--     多一列不拦(实测:该列在 schema_status 的期望列表里属新增,不参与 missing 判定);
--   · `chk_dealer_fulfillment_allocation_kind` 的 `manufacturer_mint` 分支 ——
--     该约束**不在** STRICT_CONSTRAINT_DEFINITIONS 里(只按名字登记,不比定义)。
--   动它们只增加风险,换不来任何东西。
--
-- 用法(单事务;先 dry-run 再真跑):
--   dry-run: { echo 'BEGIN;'; cat 本文件; echo 'ROLLBACK;'; } | psql -v ON_ERROR_STOP=1 -f -
--   真执行 : psql -v ON_ERROR_STOP=1 --single-transaction -f 本文件
--   ⚠️ `--single-transaction` 是 BEGIN/**COMMIT**,不是 dry-run。2026-07-30 差点误用。
-- ============================================================================

DO $$
DECLARE
  v_oid      OID := to_regclass('public.dealer_resale_fulfillment_hops');
  v_minted   BIGINT;
  v_bad      BIGINT;
  v_nowdef   TEXT;
BEGIN
  IF v_oid IS NULL THEN
    RAISE EXCEPTION '[rollback-minting] dealer_resale_fulfillment_hops 不存在,停手';
  END IF;

  -- 前置守卫 1:已经铸造过 → 不可回退,响亮拒绝(绝不动数据)
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='public' AND table_name='dealer_resale_fulfillment_hops'
               AND column_name='mint_points') THEN
    SELECT COUNT(*) INTO v_minted FROM dealer_resale_fulfillment_hops WHERE mint_points > 0;
    IF v_minted > 0 THEN
      RAISE EXCEPTION '[rollback-minting] 已存在 % 行 mint_points>0 · 回滚窗口已关闭 · 只能前滚修复,不得回退旧代码', v_minted;
    END IF;
  ELSE
    RAISE NOTICE '[rollback-minting] mint_points 列不存在 = 迁移尚未跑,无需回滚';
    RETURN;
  END IF;

  -- 前置守卫 2:存量行必须满足旧等式,否则重建约束会当场失败
  SELECT COUNT(*) INTO v_bad FROM dealer_resale_fulfillment_hops
   WHERE NOT (hop_seq >= 0 AND points > 0 AND existing_inventory_points >= 0
              AND jit_shortfall_points >= 0
              AND (existing_inventory_points + jit_shortfall_points) = points
              AND standard_reference_cents > 0);
  IF v_bad > 0 THEN
    RAISE EXCEPTION '[rollback-minting] 存量 % 行不满足旧等式 · 拒绝重建约束', v_bad;
  END IF;

  ALTER TABLE dealer_resale_fulfillment_hops
    DROP CONSTRAINT chk_dealer_fulfillment_hop_values;
  ALTER TABLE dealer_resale_fulfillment_hops
    ADD CONSTRAINT chk_dealer_fulfillment_hop_values CHECK (
      hop_seq >= 0 AND points > 0 AND existing_inventory_points >= 0
      AND jit_shortfall_points >= 0
      AND (existing_inventory_points + jit_shortfall_points) = points
      AND standard_reference_cents > 0
    );

  -- 后置核验:PG 归一化后必须与迁移前基线逐字节相同
  -- (基线取自 2026-07-30 迁移前生产 pg_get_constraintdef 实读)
  SELECT pg_get_constraintdef(oid) INTO v_nowdef FROM pg_constraint
   WHERE conrelid = v_oid AND conname = 'chk_dealer_fulfillment_hop_values' AND contype='c';
  IF v_nowdef IS DISTINCT FROM
     'CHECK (((hop_seq >= 0) AND (points > 0) AND (existing_inventory_points >= 0) AND (jit_shortfall_points >= 0) AND ((existing_inventory_points + jit_shortfall_points) = points) AND (standard_reference_cents > 0)))'
  THEN
    RAISE EXCEPTION '[rollback-minting] 还原后定义与迁移前基线不符 · 回滚: %', v_nowdef;
  END IF;

  RAISE NOTICE '[rollback-minting] 已还原 chk_dealer_fulfillment_hop_values 至迁移前定义(逐字节一致)';
END $$;
