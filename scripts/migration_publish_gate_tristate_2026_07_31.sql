-- =====================================================================
-- 发布门三态拆分 · 2026-07-31 · P1
-- 工单:docs/AI-CONTEXT/WORKORDER_REVIEW_GATE_AND_BATCH_AUDIT_2026-07-30.md §4
-- =====================================================================
-- 本单**判定逻辑零迁移**:三态(review_state / advisory_state /
-- publication_h0_state)全部由 evaluate_publication_eligibility 在读取时派生,
-- 不新增、不改写 articles.article_review_status 的任何值。
--
-- 唯一的 schema 变更是本文件这一列:§4.3 要求人审跳过的审计落
-- operator / time / reason / before / after 五字段,而 geo_article_review_events
-- 原本只有 operator(actor_user_id) / time(created_at) / reason / after(decision),
-- **缺 before**。
--
-- additive 性质(§8 要求逐条自证):
--   · ADD COLUMN IF NOT EXISTS  → 幂等,重复执行无副作用(2x 形态)
--   · nullable、无 DEFAULT      → 不重写既有行,不取排他重写锁
--   · 无 CHECK、无 NOT NULL、无 FK → 不改变任何既有行的可写性
--     (🔴 本仓 07-30 踩过"往 CHECK 加允许值"被当成 additive 导致两槽都起不来;
--      这里刻意不碰任何约束。decision 的 CHECK 已含 'skipped',无需放宽。)
--   · rollback-forward:回退到上一版本代码后,该列无人读写,保留即可;
--     既有 INSERT 不列该列也能成功(nullable 无默认)。
-- =====================================================================

ALTER TABLE geo_article_review_events
    ADD COLUMN IF NOT EXISTS prior_human_review_status VARCHAR(40);

COMMENT ON COLUMN geo_article_review_events.prior_human_review_status IS
    '人审决策发生前的 articles.article_human_review_status(五字段留痕的 before)。'
    'NULL = 该行由本列上线前写入,或决策前本就没有人审状态。';
