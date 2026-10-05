-- 2026-04-29 · Quote 定时监测频率升级
-- 从 monitoring_frequency=1/2/3 次每天迁移到 monitoring_interval_hours=N 小时间隔。
-- 幂等:只回填 NULL,不覆盖已经由新版 UI 保存过的自定义间隔。

ALTER TABLE quotes ADD COLUMN IF NOT EXISTS monitoring_interval_hours INTEGER;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS monitoring_last_run_at TIMESTAMP;

UPDATE quotes
SET monitoring_interval_hours = CASE monitoring_frequency
  WHEN 1 THEN 24
  WHEN 2 THEN 12
  WHEN 3 THEN 8
  ELSE 24
END
WHERE monitoring_interval_hours IS NULL;

ALTER TABLE quotes ALTER COLUMN monitoring_interval_hours SET DEFAULT 24;

