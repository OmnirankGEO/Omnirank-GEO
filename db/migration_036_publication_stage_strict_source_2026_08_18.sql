-- ---------------------------------------------------------------------------
-- migration 036 · WP7 · publish_outcome_records 增加 same-source 严格归因列
--
-- 形态:additive、零 DML、可无限重放(prestart 每次部署无条件重放全部迁移)。
--
-- 为什么必须新增列而不是复用 `ai_citations_delta_30d`:
--   那一列的历史含义是「品牌 × 30 天窗口的累计检出」。把它的语义悄悄换成
--   「same-source 严格命中」会让**同一列在不同时间段代表两件事**,历史数据
--   再也无法解释。本仓 2026-08-16 刚为「同名字段在不同层是不同语义」交过学费
--   (`is_monitored` 三信源事件),不再重复。
--
-- 所以:老列保持老口径继续写,新列并列写,两个数摆在一起,谁也冒充不了谁。
-- ---------------------------------------------------------------------------

ALTER TABLE publish_outcome_records
    ADD COLUMN IF NOT EXISTS strict_same_source_citations INTEGER;
ALTER TABLE publish_outcome_records
    ADD COLUMN IF NOT EXISTS strict_scope                 TEXT;
ALTER TABLE publish_outcome_records
    ADD COLUMN IF NOT EXISTS strict_metric_version        TEXT;

-- strict_scope 只允许两种形态:`quote:<id>` 或 `unavailable`。
-- 🔴 不允许出现 `brand:<id>` —— 一旦允许,品牌级口径就会从这个口子回来。
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_por_strict_scope') THEN
        ALTER TABLE publish_outcome_records ADD CONSTRAINT ck_por_strict_scope
            CHECK (strict_scope IS NULL
                   OR strict_scope = 'unavailable'
                   OR strict_scope ~ '^quote:[0-9]+$');
    END IF;
END $$;
