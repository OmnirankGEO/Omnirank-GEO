-- ============================================================
-- 回滚:打包商品 + 托管入口 软下架(2026-08-10 · W6)
--
-- 🔴 **不登记 manifest**(与正向脚本同理由)。
--
-- 回滚原则(资金/商品目录纪律):只退**当前值仍等于本次写入值**的行 ——
--   若有人在此之后把某个 code 又改回 true(比如 Owner 决定重开托管),
--   这条回滚**不许**把它按回去。布尔列没有这道闸就是不可逆混入。
--
-- 用法:先 dry-run(COMMIT 改 ROLLBACK),核对影响行数,再真跑。
-- ============================================================

BEGIN;

-- ── 改前清单 ───────────────────────────────────────────────────
SELECT f.feature_code, f.is_active AS now_value, s.is_active_before, s.captured_at
  FROM feature_pricing f
  JOIN delist_20260810_snapshot s ON s.feature_code = f.feature_code
 ORDER BY f.feature_code;

-- ── 逐行还原(只退没被别人改过的) ──────────────────────────────
UPDATE feature_pricing f
   SET is_active = s.is_active_before,
       updated_at = CURRENT_TIMESTAMP
  FROM delist_20260810_snapshot s
 WHERE s.feature_code = f.feature_code
   AND f.is_active IS FALSE;          -- ← 只退"当前仍是本次写入的 false"那些

-- ── 核验 ───────────────────────────────────────────────────────
SELECT feature_code, is_active FROM feature_pricing
 WHERE feature_code IN ('monitor_month_10', 'rank_alert',
                        'managed_campaign_recharge', 'managed_brand_recharge')
 ORDER BY feature_code;

-- ⚠️ 快照表**不删** —— 留着才知道当初是从什么值改过来的。
--    确实要清理时单独执行:DROP TABLE delist_20260810_snapshot;

-- dry-run 时改成 ROLLBACK;
COMMIT;
