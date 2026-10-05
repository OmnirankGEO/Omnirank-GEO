-- ============================================================
-- migration_keyword_stable_2026-05-10.sql
-- ============================================================
--
-- 老板 2026-05-10 拍板 A 方案 · 加 is_stable 标志区分"刚达标 vs 稳定达标"
-- 不动 compliant_days 累积语义 · 续费/合同/服务期不受影响
--
-- 稳定达标定义: 最近 7 天内 ≥ 5 天 is_compliant=TRUE
-- 留 1 个 config 可调阈值: compliance_stable_window_days (默认 7)
-- 留 1 个 config 可调阈值: compliance_stable_threshold_days (默认 5)
--
-- 部署顺序:
--   1. 蓝绿部署前先跑本 SQL (ADD COLUMN IF NOT EXISTS · 安全 · 可重入)
--   2. 历史回填 UPDATE · 一次性 · 把已有 keyword_compliance_log 行打上 is_stable
--   3. 然后部新代码 · 后续 run_daily_compliance_check 会自动维护
--
-- 回滚: ALTER TABLE keyword_compliance_log DROP COLUMN IF EXISTS is_stable
-- ============================================================

-- 1. 加字段(可重入)
ALTER TABLE keyword_compliance_log
    ADD COLUMN IF NOT EXISTS is_stable BOOLEAN DEFAULT NULL;

COMMENT ON COLUMN keyword_compliance_log.is_stable IS
    '稳定达标标志 · 最近 7 天内 ≥ 5 天 is_compliant=TRUE 才 TRUE · 不影响 compliant_days 累积';

-- 2. 加索引(可重入) · 用于查询"该 keyword 当前是否稳定"
CREATE INDEX IF NOT EXISTS idx_kcl_keyword_stable
    ON keyword_compliance_log (keyword_id, keyword_source, quote_id, check_date, is_stable);

-- 3. 历史回填(一次性)
-- 对每条历史 record · 计算其前 7 天(含当天)达标天数 ≥ 5 即 stable
-- 仅回填 is_stable IS NULL 的行 · 防重跑覆盖正在维护的数据
UPDATE keyword_compliance_log kcl
   SET is_stable = (
       (SELECT COUNT(*) FROM keyword_compliance_log k2
          WHERE k2.keyword_id = kcl.keyword_id
            AND k2.keyword_source = kcl.keyword_source
            AND k2.quote_id = kcl.quote_id
            AND k2.check_date BETWEEN (kcl.check_date - INTERVAL '6 days') AND kcl.check_date
            AND k2.is_compliant = TRUE
       ) >= 5
   )
 WHERE is_stable IS NULL;

-- 4. 验证(对比 dev / prod 跑前后行数)
-- 回填后应:
--   * is_stable 字段全部非 NULL
--   * is_stable=TRUE 行数 <= is_compliant=TRUE 行数(stable 是 compliant 的子集)
SELECT
    COUNT(*) FILTER (WHERE is_stable IS NULL) AS still_null,
    COUNT(*) FILTER (WHERE is_stable = TRUE)  AS stable_true_count,
    COUNT(*) FILTER (WHERE is_compliant = TRUE) AS compliant_true_count,
    COUNT(*) AS total_rows
FROM keyword_compliance_log;
