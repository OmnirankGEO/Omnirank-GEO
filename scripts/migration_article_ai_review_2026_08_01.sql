-- =====================================================================
-- AI 审核结论留痕 · 2026-08-01 · 包①(§3E)
-- 工单:docs/AI-CONTEXT/WORKORDER_AI_REVIEW_REPLACES_HUMAN_2026-08-01.md §3E
-- =====================================================================
-- Owner 08-01 拍板"内容审核去人工化":AI 给每篇结论。
-- 🔴 **去的是人工劳动,不是审计链** —— 结论必须逐篇落库,含 reviewer / when /
--    结论摘要 / 正文 hash。
--
-- 为什么另起一张表,而不是往 geo_article_review_events 里塞:
--   · 那张表是**人审**的不可变审计链(actor_user_id NOT NULL、decision 有 CHECK、
--     被 evaluate_publication_eligibility 的签发快照三连当权威信源读)。
--     往里混 AI 行 → 要么放宽 NOT NULL、要么伪造 user_id,两条都在动人审语义;
--     而人审签发是**资金/身份之外仍保留 H0 的那一类**,不该被 AI 旁路搅动。
--   · AI 结论是 advisory、按正文 hash 幂等、可重算可覆盖;人审决策是一次性的、
--     不可变的。生命周期不同,不该同表。
--
-- 幂等口径(§3D):UNIQUE(article_id, content_hash, reviewer)
--   同一篇 + 同一份正文 + 同一个模型版本 → 只可能有一行。
--   重跑批处理时先查后跳,**不重复调模型、不重复落行**。
--   正文一改 hash 就变 → 自然产生新行,旧行留作历史(不 UPDATE、不删)。
--
-- additive 性质(逐条自证):
--   · CREATE TABLE / INDEX 均 IF NOT EXISTS → 幂等,可反复连跑
--   · 全新表,不改任何既有表形状;无 FK(不对 articles 取引用锁)
--   · rollback-forward:回退到上一版本代码后无人读写,保留即可
-- =====================================================================

CREATE TABLE IF NOT EXISTS geo_article_ai_review_conclusions (
    id             BIGSERIAL PRIMARY KEY,
    article_id     BIGINT      NOT NULL,
    reviewer       VARCHAR(80) NOT NULL,
    content_hash   CHAR(64)    NOT NULL,
    level          VARCHAR(16) NOT NULL,
    summary        TEXT,
    findings       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- @index-guard uq_article_ai_review_conclusion ON geo_article_ai_review_conclusions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_article_ai_review_conclusion' AND i.indrelid = to_regclass('public.geo_article_ai_review_conclusions')) THEN
        NULL;  -- 已在 public.geo_article_ai_review_conclusions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_article_ai_review_conclusion' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_article_ai_review_conclusion 已存在但不在 public.geo_article_ai_review_conclusions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_article_ai_review_conclusion' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_article_ai_review_conclusion ON public.geo_article_ai_review_conclusions (article_id, content_hash, reviewer);
    END IF;
END $idxguard$;

-- @index-guard idx_article_ai_review_article ON geo_article_ai_review_conclusions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_article_ai_review_article' AND i.indrelid = to_regclass('public.geo_article_ai_review_conclusions')) THEN
        NULL;  -- 已在 public.geo_article_ai_review_conclusions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_article_ai_review_article' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_article_ai_review_article 已存在但不在 public.geo_article_ai_review_conclusions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_article_ai_review_article' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_article_ai_review_article ON public.geo_article_ai_review_conclusions (article_id, created_at DESC);
    END IF;
END $idxguard$;

-- @index-guard idx_article_ai_review_level ON geo_article_ai_review_conclusions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_article_ai_review_level' AND i.indrelid = to_regclass('public.geo_article_ai_review_conclusions')) THEN
        NULL;  -- 已在 public.geo_article_ai_review_conclusions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_article_ai_review_level' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_article_ai_review_level 已存在但不在 public.geo_article_ai_review_conclusions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_article_ai_review_level' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_article_ai_review_level ON public.geo_article_ai_review_conclusions (level, created_at DESC);
    END IF;
END $idxguard$;

COMMENT ON TABLE geo_article_ai_review_conclusions IS
    'AI 内容审核结论(§3E)。advisory 性质,不参与发布门 eligible 判定;'
    '按 (article_id, content_hash, reviewer) 幂等。人审审计链仍在 geo_article_review_events。';

COMMENT ON COLUMN geo_article_ai_review_conclusions.reviewer IS
    '形如 ai:deepseek-chat@v1-2026-08-01。模型版本进串:换模型/换 prompt 版本 = 新 reviewer = 可重跑,旧结论留痕。';

COMMENT ON COLUMN geo_article_ai_review_conclusions.level IS
    'L1=自动通过 / L2=可自动修复 / L3=建议人工复核 / not_checked=AI 调用失败(fail-closed,不冒充已核查)。';

COMMENT ON COLUMN geo_article_ai_review_conclusions.findings IS
    'JSON 数组,每条必须带 trigger(具体触发原因)。§3D 明令禁止"高风险或无全文核验证据"这类笼统文案。';
