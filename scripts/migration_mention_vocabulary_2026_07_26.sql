-- P0-3 · 提及/推荐词表归一（Owner 2026-07-26 裁决：推荐口径放宽）
--
-- 生产实证（修复前）：monitoring_results.mention_type 全库只有
--   direct / none / llm_verified_fallback / pending_identity
-- 四个值，没有任何「推荐」档 → 报告推荐率恒 0%，而 UI 还并排展示
-- 「仅提到 / 推荐」两个标签，后者永远取不到值 = 假标签。
--
-- 本迁移只做**词表重命名**，不改任何判定结果：
--   direct                → mentioned
--   llm_verified_fallback → mentioned
-- 🔴 绝不把任何存量值升成 recommended。旧值只证明「品牌出现在回答里」，
--    不证明 AI 推荐了它；升档就是伪造业绩（Owner 明确「不伪造推荐」）。
--
-- 不动的列：is_detected（评分输入）、target_outcome、identity_review_state、
-- response_snippet、full_response、tested_at —— 判定与原文一字不改。
--
-- 幂等：WHERE 限定旧值，重复执行 0 行受影响。
-- 回滚：见文件尾注释（把 mentioned 改回 direct 会同时波及新写入的行，
--        因此回滚只在同一部署窗口内、确认无新数据时执行）。

SET LOCAL search_path = pg_catalog, public;

-- 前置：确认 mention_type 列存在且是 text 家族（不猜类型）。
DO $$
DECLARE
    col_type TEXT;
BEGIN
    SELECT pg_catalog.format_type(a.atttypid, a.atttypmod)
      INTO col_type
      FROM pg_catalog.pg_attribute a
     WHERE a.attrelid = 'public.monitoring_results'::pg_catalog.regclass
       AND a.attname = 'mention_type'
       AND NOT a.attisdropped;
    IF col_type IS NULL THEN
        RAISE EXCEPTION 'monitoring_results.mention_type is missing';
    END IF;
    IF col_type NOT IN ('text', 'character varying', 'character varying(40)', 'character varying(50)') THEN
        RAISE EXCEPTION 'unexpected monitoring_results.mention_type type: %', col_type;
    END IF;
END $$;

-- 词表归一（只重命名"已提到"这一档；不碰 none / pending_identity）
UPDATE public.monitoring_results
   SET mention_type = 'mentioned'
 WHERE mention_type IN ('direct', 'llm_verified_fallback');

-- 自检：不得残留旧值，且不得因本迁移凭空出现 recommended。
DO $$
DECLARE
    legacy_left INTEGER;
    fabricated INTEGER;
BEGIN
    SELECT COUNT(*) INTO legacy_left
      FROM public.monitoring_results
     WHERE mention_type IN ('direct', 'llm_verified_fallback');
    IF legacy_left <> 0 THEN
        RAISE EXCEPTION 'legacy mention_type rows still present: %', legacy_left;
    END IF;

    -- recommended 只能由运行时判定写入。本迁移执行完，若库里已有 recommended，
    -- 那必须是应用侧新写的行；这里只断言"本迁移没有把 is_detected=false 的行
    -- 说成被推荐"（伪造业绩的最直接形态）。
    SELECT COUNT(*) INTO fabricated
      FROM public.monitoring_results
     WHERE mention_type = 'recommended'
       AND COALESCE(is_detected, 0) = 0;
    IF fabricated <> 0 THEN
        RAISE EXCEPTION 'recommended rows without detection: %', fabricated;
    END IF;

    IF current_setting('session_replication_role') IS DISTINCT FROM 'origin' THEN
        RAISE EXCEPTION 'session_replication_role must be origin';
    END IF;
END $$;

-- rollback（人工执行 · 仅限同一部署窗口内确认无新写入时）：
--   UPDATE public.monitoring_results SET mention_type = 'direct' WHERE mention_type = 'mentioned';
-- 注意：回滚会把新写入的 mentioned 一并改成 direct。应用层
-- services/mention_vocabulary.py 的 normalize_mention_type 双向都认，
-- 因此**不回滚数据**通常更安全。
