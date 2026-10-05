-- ============================================================
-- 打包商品 + 托管入口 软下架(2026-08-10 · W6)
--
-- 依据:Owner 2026-08-10 商业边界裁决
--   ①「OmniRank 不再出售面向终端客户的营销打包商品 —— 平台只提供能力与算力,
--      打包/组合/定价是服务商自己的生意」→ monitor_month_10 / rank_alert 永久下架
--   ②「托管是历史遗留功能,先隐藏,未来再开」→ managed_*_recharge 软下架(可逆)
--
-- 🔴 **只改 is_active,不删任何行**。三条理由:
--   1. 删行会被 `seed_feature_pricing()` 以 is_active=true 复活
--      (它每次进程启动都重放,INSERT 不写 is_active,吃列默认值 true —— 生产实测);
--   2. 保留历史行 = 保留审计线索(哪怕零交易);
--   3. 托管未来要重开,行删了还得重建。
--
-- 🔴 **本脚本不登记 `db/migration_manifest.py`**(Review 2026-08-10 已准此豁免)。
--    prestart 会**无条件重放 manifest 里的全部迁移**(无追踪表)——
--    把这条 UPDATE 登记进去,等于每次部署都强制把 is_active 按回 false,
--    未来 Owner 手工重开托管后,**下次部署会主动把它按回去**。
--
--    ⚠️ 不能套用"同类脚本也没登记"当理由 —— 那个说法不准确:
--    `scripts/fix_billing_2026-04-14.sql`(同表同类的一次性数据变更)**是登记了的**。
--    真正的区别在**幂等语义**:
--      · 那一类是「**旧值 → 新值的一次性转移**」—— 转移做完后重放永久空转,
--        登记进去无害,所以可以登记;
--      · 本脚本是「**强制状态**」—— 每次重放都会把 is_active 按成 false,
--        与"未来可由 Owner 重开"这个既定意图直接冲突。
--    故:**幂等语义为强制状态的脚本不进 manifest,不能套用一次性转移型的先例。**
--
-- 执行方式(Deploy 执行,不由执行方自行跑):
--   0) 备份:docker exec omnirank-db pg_dump -U geo_admin geo_agentscope > backup_YYYYMMDD_HHMM.sql
--   1) dry-run:把最下方 COMMIT 改成 ROLLBACK 跑一遍,核对影响行数 = 4
--   2) 清单过目:§A 的 SELECT 输出交 Owner/Review 确认
--   3) 真跑:COMMIT
--
-- 回滚:scripts/rollback_delist_packaged_sku_2026_08_10.sql
-- ============================================================

BEGIN;

-- ── §A 改前清单(过目用 · 期望 4 行,is_active 全为 t) ──────────
SELECT feature_code, feature_name, cost_points, is_active, updated_at
  FROM feature_pricing
 WHERE feature_code IN ('monitor_month_10', 'rank_alert',
                        'managed_campaign_recharge', 'managed_brand_recharge')
 ORDER BY feature_code;

-- ── §B 回滚快照(记 (表, 主键, 写入前值) 三元组) ────────────────
--   🔴 布尔列的反向 UPDATE **不可逆混入**:改完就分不清"本来就是 false"
--   和"这次改成 false"。所以先落快照,回滚只退"当前值仍等于本次写入值"的行。
CREATE TABLE IF NOT EXISTS delist_20260810_snapshot (
    feature_code    TEXT PRIMARY KEY,
    is_active_before BOOLEAN NOT NULL,
    captured_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO delist_20260810_snapshot (feature_code, is_active_before)
SELECT feature_code, is_active
  FROM feature_pricing
 WHERE feature_code IN ('monitor_month_10', 'rank_alert',
                        'managed_campaign_recharge', 'managed_brand_recharge')
ON CONFLICT (feature_code) DO NOTHING;

-- ── §C 软下架(期望影响 4 行) ──────────────────────────────────
UPDATE feature_pricing
   SET is_active = FALSE,
       updated_at = CURRENT_TIMESTAMP
 WHERE feature_code IN ('monitor_month_10', 'rank_alert',
                        'managed_campaign_recharge', 'managed_brand_recharge')
   AND is_active IS DISTINCT FROM FALSE;      -- 幂等:已 false 的不重复写 updated_at

-- ── §D 改后核验 ────────────────────────────────────────────────
SELECT feature_code, is_active, updated_at
  FROM feature_pricing
 WHERE feature_code IN ('monitor_month_10', 'rank_alert',
                        'managed_campaign_recharge', 'managed_brand_recharge')
 ORDER BY feature_code;

-- ── §E 🔴 反向对照:活跃监测 code 必须**仍然 active** ───────────
--   证明这条 UPDATE 没有误伤监测计费链。期望 3 行全 t。
--   (监测真实计费走 monitoring_keyword_daily / monitor_single / scheduled_monitoring,
--    与本次下架的 SKU 零交叉 —— 生产实测 monitoring_keyword_daily 766 笔仍在跑。)
SELECT feature_code, is_active
  FROM feature_pricing
 WHERE feature_code IN ('monitoring_keyword_daily', 'monitor_single', 'scheduled_monitoring')
 ORDER BY feature_code;

-- ── §F 🔴 托管存量数据零变更自证(本脚本一行都不碰它们) ─────────
--   下架前基线(2026-08-10 实测):managed_campaigns=2 · managed_actions=72 · managed 交易=2
SELECT 'managed_campaigns' AS t, count(*) FROM managed_campaigns
UNION ALL SELECT 'managed_actions', count(*) FROM managed_actions
UNION ALL SELECT 'point_txn@managed%', count(*) FROM point_transactions
  WHERE feature_code LIKE 'managed%'
ORDER BY 1;

-- dry-run 时把下面这行改成 ROLLBACK;
COMMIT;
