-- ============================================================
-- V3.6 · 销售定价口径修正:卖「算力库存」不卖「功能包」
-- 2026-06-06 · GEO CTO · 老板拍板(前端+后端一起根治 / 命名取「入门 · 常用 · 大客户算力包」)
--
-- 目标:
--   1) 下架 3 个按功能命名的「场景包」(scenario_pack:诊断启动包/写作发布包/监测分析包)
--      → 三端(服务商定价页 /agent/pricing · 客户购买页 /customer/recharge · admin)不再作为主销售包
--   2) 通用算力包(credit_pack)4 档默认名/副标题统一为「算力库存」口径(入门/常用/大客户/团队定制)
--
-- 红线(本脚本绝不触碰):
--   - 不改 points_granted / wholesale_cents / suggested_retail_cents(算力量纲与价格分毫不动)
--   - 不删任何行(场景包仅 is_active=FALSE 逻辑下架 · 重置 TRUE 即可回滚)
--   - 不影响历史订单(订单创建时已 snapshot SKU 名/价 · 详见 settlement_orchestrator)
--   - 不动 agent_sku_overrides(代理已自定义白标名的不受影响 · 覆盖优先)
--   - 不动 billing / 扣费 / 实际消耗逻辑
--
-- SQL 4 维核验(2026-06-06 已对照 migration_v35_factory_inventory_2026_05_26.sql:64-78):
--   · 列名:template_code / sku_type / default_name / default_subtitle / is_active / updated_at 均在 sku_templates
--   · data_type:default_name TEXT NOT NULL · default_subtitle TEXT · is_active BOOLEAN · updated_at TIMESTAMP
--   · 字段归属:sku_templates(非 agent_sku_overrides — 后者是代理白标覆盖表)
--   · dry-run:本脚本整体 BEGIN; ... COMMIT;,Deploy-CTO 可先把 COMMIT 改 ROLLBACK 跑一遍看 RAISE NOTICE
--
-- 幂等:UPDATE 按 template_code 精确命中 · 重复执行结果一致(名字已是目标值则无变化)
-- ============================================================

BEGIN;

-- 1) 下架场景包(逻辑删除 · 可回滚)
--    回滚:UPDATE sku_templates SET is_active=TRUE WHERE template_code IN ('scenario_diagnosis','scenario_writing','scenario_monitor');
UPDATE sku_templates
SET is_active = FALSE, updated_at = NOW()
WHERE template_code IN ('scenario_diagnosis', 'scenario_writing', 'scenario_monitor')
  AND sku_type = 'scenario_pack';

-- 2) 通用算力包 4 档:默认名 + 副标题 改「算力库存」口径
--    (副标题 = 老板卡片示例的「适合…」短句;详细「大概可支撑…」场景说明在前端按档位展示)
UPDATE sku_templates SET default_name = '入门算力包',   default_subtitle = '适合先试跑 1 个客户',   updated_at = NOW()
  WHERE template_code = 'credit_basic'  AND sku_type = 'credit_pack';
UPDATE sku_templates SET default_name = '常用算力包',   default_subtitle = '适合日常经营',          updated_at = NOW()
  WHERE template_code = 'credit_growth' AND sku_type = 'credit_pack';
UPDATE sku_templates SET default_name = '大客户算力包', default_subtitle = '适合多客户批量运营',     updated_at = NOW()
  WHERE template_code = 'credit_pro'    AND sku_type = 'credit_pack';
-- 团队定制档(is_active=FALSE 商务面议 · 不上架 · 仅术语统一)
UPDATE sku_templates SET default_name = '团队定制算力包', default_subtitle = '高额度商务定制',       updated_at = NOW()
  WHERE template_code = 'credit_team'   AND sku_type = 'credit_pack';

-- 3) 验证(失败即整体回滚 · 不会留下半截状态)
DO $$
DECLARE
    scen_active     INTEGER;
    credit_renamed  INTEGER;
BEGIN
    SELECT COUNT(*) INTO scen_active
      FROM sku_templates WHERE sku_type = 'scenario_pack' AND is_active = TRUE;
    SELECT COUNT(*) INTO credit_renamed
      FROM sku_templates WHERE sku_type = 'credit_pack' AND default_name LIKE '%算力包';

    RAISE NOTICE 'scenario_pack 仍 active 行数 = %  (期望 0)', scen_active;
    RAISE NOTICE 'credit_pack 已改算力包命名行数 = %  (期望 4)', credit_renamed;

    IF scen_active <> 0 THEN
        RAISE EXCEPTION '场景包下架失败 · 仍有 % 行 active', scen_active;
    END IF;
    IF credit_renamed < 4 THEN
        RAISE EXCEPTION 'credit_pack 改名不足 · 仅 % 行(期望 4)', credit_renamed;
    END IF;
END $$;

COMMIT;

-- ============================================================
-- 部署后核验(Deploy-CTO 跑一条 SELECT 肉眼确认):
--   SELECT template_code, sku_type, default_name, default_subtitle, is_active
--     FROM sku_templates ORDER BY sku_type, id;
-- 期望:
--   credit_*  → 入门/常用/大客户/团队定制 算力包 · basic/growth/pro is_active=TRUE
--   scenario_* → is_active=FALSE(3 行)
--   addon_*   → 不变(仍 active · 前端按「小额算力补充」语义展示)
-- ============================================================
