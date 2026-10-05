-- =====================================================================
-- 策略 AI 评估报告留痕 · 2026-08-01 · 包②(§3F)
-- 工单:docs/AI-CONTEXT/WORKORDER_AI_REVIEW_REPLACES_HUMAN_2026-08-01.md §3F
-- =====================================================================
-- §3F 要求"评估报告落库"。为什么另起一张表而不是塞进 writing_strategy_versions:
--
--   🔴 `writing_strategy_versions.reviewed_by` 是 **BIGINT**(已核 DDL)。
--      工单原话"写 reviewed_by='ai:deepseek-chat@…'"**类型上不可能** ——
--      那是个用户 id 列,不是 reviewer 名字列。硬塞会直接报错。
--      所以 AI 的身份、结论、冲突项、数据支撑全部落本表;
--      主表只按既有语义写 reviewed_at / review_note(note 里带 reviewer 串)。
--
--   · 评估是可重跑的(换 prompt 版本就该重评),主表的评审字段是一次性决策 ——
--     生命周期不同,不该同表。
--
-- 幂等:UNIQUE(strategy_id, reviewer) —— 同一策略同一模型版本只留一条最新结论
-- (ON CONFLICT DO UPDATE,保留最后一次评估;历史版本靠 reviewer 串里的版本号区分)。
--
-- additive:全新表 + IF NOT EXISTS + 无 FK(不对策略表取引用锁),可反复连跑。
-- =====================================================================

CREATE TABLE IF NOT EXISTS geo_strategy_ai_review_reports (
    id            BIGSERIAL PRIMARY KEY,
    strategy_id   BIGINT      NOT NULL,
    reviewer      VARCHAR(80) NOT NULL,
    verdict       VARCHAR(16) NOT NULL,
    summary       TEXT,
    conflicts     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    evidence      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- @index-guard uq_strategy_ai_review ON geo_strategy_ai_review_reports unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_strategy_ai_review' AND i.indrelid = to_regclass('public.geo_strategy_ai_review_reports')) THEN
        NULL;  -- 已在 public.geo_strategy_ai_review_reports 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_strategy_ai_review' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_strategy_ai_review 已存在但不在 public.geo_strategy_ai_review_reports 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_strategy_ai_review' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_strategy_ai_review ON public.geo_strategy_ai_review_reports (strategy_id, reviewer);
    END IF;
END $idxguard$;

-- @index-guard idx_strategy_ai_review_verdict ON geo_strategy_ai_review_reports plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_strategy_ai_review_verdict' AND i.indrelid = to_regclass('public.geo_strategy_ai_review_reports')) THEN
        NULL;  -- 已在 public.geo_strategy_ai_review_reports 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_strategy_ai_review_verdict' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_strategy_ai_review_verdict 已存在但不在 public.geo_strategy_ai_review_reports 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_strategy_ai_review_verdict' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_strategy_ai_review_verdict ON public.geo_strategy_ai_review_reports (verdict, created_at DESC);
    END IF;
END $idxguard$;

COMMENT ON TABLE geo_strategy_ai_review_reports IS
    '写作策略版本的 AI 评估报告(§3F)。advisory:结论不自动激活策略 —— '
    '策略是平台级规则,按 §1 边界保留「AI 预核对 + 一键确认」。';

COMMENT ON COLUMN geo_strategy_ai_review_reports.verdict IS
    'pass=无冲突且有数据支撑 / reject=有冲突或支撑不足 / not_checked=AI 调用或解析失败(fail-closed)。';

COMMENT ON COLUMN geo_strategy_ai_review_reports.conflicts IS
    'JSON 数组,每条必须带 rule(与哪条 SSOT/红线冲突)与 evidence(原文哪一句)。禁笼统。';
