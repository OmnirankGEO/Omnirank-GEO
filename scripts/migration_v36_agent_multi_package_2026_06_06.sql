-- ============================================================
-- V3.6 · 服务商「新增算力包」· agent_sku_overrides 1:1 → 1:N
-- 2026-06-06 · GEO CTO · 老板拍板「授权直接做全套」
--
-- 目标:让服务商基于平台规格 sku_templates 新建多个独立白标售卖包
--   (每个独立 id · 自定义包名/副标题/卖点/适合场景/售价/上下架/排序 · 成本从规格继承不可改)
--
-- 改动:
--   1) agent_sku_overrides 删除 UNIQUE(agent_user_id, sku_template_id) 1:1 约束 → 允许一个规格多个白标包
--   2) agent_sku_overrides 加 sort_order(排序)+ custom_scene(适合场景文案)
--   3) recharge_orders 加 override_id(溯源「客户买的是哪个白标包」· 向后兼容:老订单 NULL)
--
-- 红线:不改 points_granted / wholesale_cents(成本量纲)· 不改 billing / 扣费 / 算力消耗 · 不删数据
-- ⚠️ 必须与后端 services/agent_pricing.py 同批部署:删唯一约束后,旧 UPSERT 的
--    ON CONFLICT (agent_user_id, sku_template_id) 会失效报错 → 代码已改为 create/update-by-id
--
-- SQL 4 维核验:
--   · 列名:agent_sku_overrides(id/agent_user_id/sku_template_id/custom_name/custom_subtitle/
--          custom_sales_pitch/retail_cents/is_active)· recharge_orders(已有 sku_template_id/agent_user_id)
--   · data_type:sort_order INTEGER · custom_scene TEXT · override_id INTEGER
--   · 字段归属:agent_sku_overrides(白标包)/ recharge_orders(订单溯源)
--   · dry-run:整体 BEGIN…COMMIT,可改 ROLLBACK 试跑看 RAISE NOTICE
-- ============================================================

BEGIN;

-- 1) 删除 1:1 唯一约束(动态找约束名 · 兼容自动命名 agent_sku_overrides_agent_user_id_sku_template_id_key)
DO $$
DECLARE cname TEXT;
BEGIN
    SELECT conname INTO cname
      FROM pg_constraint
     WHERE conrelid = 'agent_sku_overrides'::regclass
       AND contype = 'u'
       AND pg_get_constraintdef(oid) ILIKE '%agent_user_id%sku_template_id%';
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE agent_sku_overrides DROP CONSTRAINT %I', cname);
        RAISE NOTICE '已删除 1:1 唯一约束: %', cname;
    ELSE
        RAISE NOTICE '未找到 (agent_user_id, sku_template_id) 唯一约束(可能已删 · 幂等)';
    END IF;
END $$;

-- 2) 加排序 + 适合场景字段
ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0;
ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS custom_scene TEXT;

-- 3) recharge_orders 加 override_id 溯源(向后兼容 · 老订单/直营单 NULL)
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS override_id INTEGER;

-- 4) 索引:代理白标包按 agent + 排序查
CREATE INDEX IF NOT EXISTS idx_agent_sku_overrides_agent_sort
  ON agent_sku_overrides(agent_user_id, sort_order, id);

-- 5) 验证
DO $$
DECLARE has_uniq INTEGER; has_sort INTEGER; has_scene INTEGER; has_ov INTEGER;
BEGIN
    SELECT COUNT(*) INTO has_uniq FROM pg_constraint
      WHERE conrelid='agent_sku_overrides'::regclass AND contype='u'
        AND pg_get_constraintdef(oid) ILIKE '%agent_user_id%sku_template_id%';
    SELECT COUNT(*) INTO has_sort FROM information_schema.columns
      WHERE table_name='agent_sku_overrides' AND column_name='sort_order';
    SELECT COUNT(*) INTO has_scene FROM information_schema.columns
      WHERE table_name='agent_sku_overrides' AND column_name='custom_scene';
    SELECT COUNT(*) INTO has_ov FROM information_schema.columns
      WHERE table_name='recharge_orders' AND column_name='override_id';
    RAISE NOTICE '1:1 唯一约束剩余=% (期望 0) · sort_order=% scene=% override_id=% (各期望 1)',
                 has_uniq, has_sort, has_scene, has_ov;
    IF has_uniq <> 0 OR has_sort <> 1 OR has_scene <> 1 OR has_ov <> 1 THEN
        RAISE EXCEPTION 'migration 校验失败 · 回滚';
    END IF;
END $$;

COMMIT;

-- ============================================================
-- 回滚(如需):
--   ALTER TABLE agent_sku_overrides ADD CONSTRAINT agent_sku_overrides_agent_user_id_sku_template_id_key
--     UNIQUE(agent_user_id, sku_template_id);  -- ⚠️ 仅当无重复数据时可加回
--   ALTER TABLE agent_sku_overrides DROP COLUMN sort_order, DROP COLUMN custom_scene;
--   ALTER TABLE recharge_orders DROP COLUMN override_id;
-- ============================================================
