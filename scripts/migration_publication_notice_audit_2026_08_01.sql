-- =====================================================================
-- 发布提示静默留痕 · 2026-08-01 · 包①(内容审核 AI 化 + 核验流融合)
-- 工单:docs/AI-CONTEXT/WORKORDER_AI_REVIEW_REPLACES_HUMAN_2026-08-01.md §3A
-- =====================================================================
-- 背景:Owner 08-01 拍板把内容类硬门(广告法/平台档/编造型证据)全部降为提示级,
-- 发不发由客户自己决定。平台因此需要一条**零打扰**的证据链:客户在"存在未处理
-- 提示"的情况下点了发布,后台留一行记录(article_id / 提示类别 / 时间 / 提示时的
-- 判定快照)。前端**零弹窗零确认**——这一行只在纠纷时被查,不在任何界面上出现。
--
-- 🔴 它不是审核记录,不参与任何判定:写失败只记 warning,绝不影响发布(见
--    services/article_review_gate.py::_record_publication_notice_audit 的 SAVEPOINT)。
--    因此漏跑本迁移的表现是"留痕恒为空",不会造成发布故障——但那正是**最该登记**
--    的一类(本仓 07-29 协议门禁 3 天零信号就是同型),故进 manifest 由 prestart 建。
--
-- additive 性质(逐条自证):
--   · CREATE TABLE IF NOT EXISTS  → 幂等,可反复连跑
--   · 全新表,不改任何既有表的形状 → 既有行的可写性零变化
--   · 无 FK(刻意):不对 articles 取引用锁,也不让归档/删除文章时反向卡住留痕
--   · 索引用 IF NOT EXISTS,非 CONCURRENTLY(新表零行,不存在长锁问题)
--   · rollback-forward:回退到上一版本代码后无人读写,保留即可
-- =====================================================================

CREATE TABLE IF NOT EXISTS geo_article_publication_notice_audits (
    id              BIGSERIAL PRIMARY KEY,
    article_id      BIGINT      NOT NULL,
    notice_classes  JSONB       NOT NULL,
    notice_count    INTEGER     NOT NULL DEFAULT 0,
    gate_snapshot   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- @index-guard idx_pub_notice_audits_article ON geo_article_publication_notice_audits plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pub_notice_audits_article' AND i.indrelid = to_regclass('public.geo_article_publication_notice_audits')) THEN
        NULL;  -- 已在 public.geo_article_publication_notice_audits 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pub_notice_audits_article' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pub_notice_audits_article 已存在但不在 public.geo_article_publication_notice_audits 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pub_notice_audits_article' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pub_notice_audits_article ON public.geo_article_publication_notice_audits (article_id, created_at DESC);
    END IF;
END $idxguard$;

-- @index-guard idx_pub_notice_audits_created ON geo_article_publication_notice_audits plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pub_notice_audits_created' AND i.indrelid = to_regclass('public.geo_article_publication_notice_audits')) THEN
        NULL;  -- 已在 public.geo_article_publication_notice_audits 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pub_notice_audits_created' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pub_notice_audits_created 已存在但不在 public.geo_article_publication_notice_audits 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pub_notice_audits_created' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pub_notice_audits_created ON public.geo_article_publication_notice_audits (created_at DESC);
    END IF;
END $idxguard$;

COMMENT ON TABLE geo_article_publication_notice_audits IS
    '带未处理内容提示发布时的静默留痕(§3A)。零弹窗、不进任何界面;'
    '用途仅为广告法等纠纷时证明"平台已尽提示义务"。写入 best-effort,不影响发布。';

COMMENT ON COLUMN geo_article_publication_notice_audits.notice_classes IS
    'JSON 数组:发布当时未处理的内容提示类别(legal_hard / platform_profile_hard / evidence_fabrication_hard)。';

COMMENT ON COLUMN geo_article_publication_notice_audits.gate_snapshot IS
    '发布当时的发布门判定快照(review_state / advisory_state / publication_h0_state / reason 等),'
    '用于事后还原"提示是什么、当时是什么状态"。';
