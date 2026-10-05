-- Migration 006: 达标后历史平滑算法
-- keyword_compliance_log 增加 effective_rate 字段
-- effective_rate = 达标前等于detection_rate，达标后等于首次达标以来的累计平均出现率

ALTER TABLE keyword_compliance_log
    ADD COLUMN IF NOT EXISTS effective_rate NUMERIC(5,1);

-- 回填：将现有记录的 effective_rate 设为 detection_rate
UPDATE keyword_compliance_log
SET effective_rate = detection_rate
WHERE effective_rate IS NULL;
