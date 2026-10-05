-- [CTO-13.0] 修复 profile.name 与 brand.name 漂移的脏数据
-- 2026-04-19 · 起因：老板在 MyIP 页看到两个"全域上榜（深圳）科技有限公司"档案
-- 根因：auto_39_172 profile.brand_id=197（朵朵出海）但 profile.name 被 AI 填充/手动改成了
-- "全域上榜..."，UI 只显示 profile.name 无法区分。根治在代码层（6 处），本脚本清脏数据。
--
-- 执行顺序：
--   Step 1. 检查 auto_39_172 内容 → 判断是空壳还是有数据
--   Step 2. 基于 Step 1 结果执行 2A（soft delete）或 2B（merge 到 hs5G4Vtv 后 soft delete）
--   Step 3. 全站扫描同类脏数据（可能还有别的 profile 漂移了）
--
-- ⚠️ 执行前备份：docker exec omnirank-db pg_dump -U geo_admin geo_agentscope > backup_profile_drift_$(date +%Y%m%d_%H%M).sql

-- ============================================================
-- Step 1. 诊断 auto_39_172 内容深度
-- ============================================================
-- 看这条 profile 有没有独特业务数据（industry / business / 结构化知识 / 人设画像）
SELECT
    cp.id,
    cp.name                              AS profile_name,
    cp.brand_id,
    b.name                               AS brand_name,
    cp.industry,
    cp.business,
    length(COALESCE(cp.company_intro, '')) AS intro_len,
    length(COALESCE(cp.selling_points, '')) AS selling_len,
    length(COALESCE(cp.success_cases, '')) AS cases_len,
    length(COALESCE(cp.structured_knowledge::text, '')) AS sk_len,
    length(COALESCE(cp.personality_profile, '')) AS pp_len,
    cp.persona_positioning,
    cp.persona_tone,
    cp.created_at,
    cp.updated_at
FROM client_profiles cp
LEFT JOIN brands b ON b.id = cp.brand_id
WHERE cp.id = 'auto_39_172';

-- ============================================================
-- Step 2A. 空壳场景（intro_len/selling_len/sk_len/pp_len 都 ≈ 0） — 推荐
-- ============================================================
-- brand 197 下已有正确的 ZX6Xo4Uf（朵朵出海），auto_39_172 是空壳错名 → soft delete
--
-- UPDATE client_profiles
-- SET is_deleted = 1, deleted_at = NOW()
-- WHERE id = 'auto_39_172' AND (is_deleted = 0 OR is_deleted IS NULL);

-- ============================================================
-- Step 2B. 有数据场景（Step 1 显示 sk_len 或 pp_len > 100） — merge 到 hs5G4Vtv
-- ============================================================
-- [CTO-13.0 · 2026-04-19 数据审计扩展]
-- 老板给的审计：auto_39_172 有 personality_profile 1581 字节（飞轮沉淀产出），
-- persona_positioning 19 字节 / persona_tone 30 字节 / industry 49 字节 / business 49 字节
-- dst hs5G4Vtv 这 5 个字段几乎全空 → 必须保全 merge，否则老板要重走 6 问深档
--
-- merge 策略：dst 字段为空时才从 src 拉，dst 有内容则保留（老板 self brand 优先）
-- 涉及 5 个关键字段：
--   1. personality_profile  — 6 问深档产出的人设画像 JSON（飞轮核心资产）
--   2. persona_positioning  — 人设定位（脚本生成 prompt 直接读）
--   3. persona_tone         — 人设口吻（脚本生成 prompt 直接读）
--   4. industry             — 行业（src 更准确："AI SaaS / 数字营销 / 品牌智能推荐"）
--   5. business             — 业务描述（src 有内容，dst 无）
-- 其他可选字段（dst 有空位就 merge）：company_intro / selling_points / success_cases / structured_knowledge

BEGIN;

-- Step 2B-1. 合并 auto_39_172 → hs5G4Vtv（dst 空位用 src 填）
UPDATE client_profiles AS dst
SET
    -- 核心：人设画像（JSONB / JSON 字段，空/'{}'/null 都算空）
    personality_profile = CASE
        WHEN dst.personality_profile IS NULL
          OR dst.personality_profile::text IN ('', '{}', 'null')
        THEN src.personality_profile ELSE dst.personality_profile END,
    persona_positioning = COALESCE(NULLIF(dst.persona_positioning, ''), src.persona_positioning),
    persona_tone        = COALESCE(NULLIF(dst.persona_tone, ''),        src.persona_tone),
    -- 业务基础
    industry            = COALESCE(NULLIF(dst.industry, ''),            src.industry),
    business            = COALESCE(NULLIF(dst.business, ''),            src.business),
    -- 可选扩展（dst 有空位才拉）
    company_intro       = COALESCE(NULLIF(dst.company_intro, ''),       src.company_intro),
    selling_points      = COALESCE(NULLIF(dst.selling_points, ''),      src.selling_points),
    success_cases       = COALESCE(NULLIF(dst.success_cases, ''),       src.success_cases),
    structured_knowledge = CASE
        WHEN dst.structured_knowledge IS NULL
          OR dst.structured_knowledge::text IN ('', '{}', 'null')
        THEN src.structured_knowledge ELSE dst.structured_knowledge END,
    updated_at = NOW()
FROM client_profiles AS src
WHERE dst.id = 'hs5G4Vtv' AND src.id = 'auto_39_172';

-- Step 2B-2. Soft delete 源
UPDATE client_profiles
SET is_deleted = 1, deleted_at = NOW()
WHERE id = 'auto_39_172';

-- Step 2B-3. 验证合并后 hs5G4Vtv 的关键字段
SELECT
    id, name, brand_id,
    length(COALESCE(personality_profile, '')) AS pp_len,
    length(COALESCE(persona_positioning, '')) AS positioning_len,
    length(COALESCE(persona_tone, ''))        AS tone_len,
    industry,
    business,
    updated_at
FROM client_profiles WHERE id = 'hs5G4Vtv';

-- 验证看到 pp_len ~= 1581 / positioning_len ~= 19 / tone_len ~= 30 / industry 和 business 非空 → 提交
-- 否则 ROLLBACK;
-- COMMIT;  -- ← 人工审阅 SELECT 结果无误后打开
ROLLBACK;  -- 默认先 rollback 避免误跑，审阅 OK 后改成 COMMIT 再执行整段

-- ============================================================
-- Step 3. 全站扫描同类脏数据（可能还有）
-- ============================================================
-- 列出所有 profile.name 和 brand.name 不一致的记录，供人工审阅
SELECT
    cp.id                                AS profile_id,
    cp.name                              AS profile_name,
    cp.brand_id,
    b.name                               AS brand_name,
    b.brand_type,
    b.owner_user_id                      AS brand_owner,
    cp.industry,
    length(COALESCE(cp.company_intro, '')) AS intro_len,
    length(COALESCE(cp.structured_knowledge::text, '')) AS sk_len,
    cp.created_at,
    cp.updated_at
FROM client_profiles cp
LEFT JOIN brands b ON b.id = cp.brand_id
WHERE (cp.is_deleted = 0 OR cp.is_deleted IS NULL)
  AND cp.brand_id IS NOT NULL
  AND cp.name IS NOT NULL
  AND b.name IS NOT NULL
  AND cp.name <> b.name
ORDER BY cp.updated_at DESC;

-- [CTO-13.0 · 2026-04-19 数据审计后的批量策略]
-- 全站查询结果已审阅，分 3 类处理：
--   A. pp_len > 100 的 "采访档案-%" → 产品默认兜底名（interview_api.py:536），**保留不动**
--      后续 P2 改 interview_api 默认用 brand.name 兜底（不在本 SQL 范围）
--   B. pp_len > 100 且 name 非"采访档案-%" → 有人设数据的脏数据，**人工审阅**，不批量
--   C. pp_len = 0 的空壳漂移 → 直接对齐 name = brand.name（无业务损失）
--
-- 本 SQL 只处理 C 类（空壳对齐），保守无损

BEGIN;

-- Step 3-1. 预览将要修改的空壳行（必看）
SELECT
    cp.id, cp.name AS old_name, b.name AS new_name,
    cp.brand_id, b.brand_type,
    length(COALESCE(cp.personality_profile, '')) AS pp_len
FROM client_profiles cp
JOIN brands b ON b.id = cp.brand_id
WHERE (cp.is_deleted = 0 OR cp.is_deleted IS NULL)
  AND cp.name <> b.name
  -- 保护：有人设数据的不动（走人工审阅）
  AND (cp.personality_profile IS NULL
       OR cp.personality_profile::text IN ('', '{}', 'null'))
  -- 保护：interview 默认名不动（产品兜底，不是 bug）
  AND cp.name NOT LIKE '采访档案-%'
  -- 保护：同 brand 下已有正确 profile 时不动（那条正确的应该保留）
  AND NOT EXISTS (
      SELECT 1 FROM client_profiles cp2
      WHERE cp2.brand_id = cp.brand_id
        AND cp2.id <> cp.id
        AND (cp2.is_deleted = 0 OR cp2.is_deleted IS NULL)
        AND cp2.name = b.name
  )
ORDER BY cp.updated_at DESC;

-- Step 3-2. 执行对齐（人工审阅 Step 3-1 结果无误后打开）
-- UPDATE client_profiles cp
-- SET name = b.name, updated_at = NOW()
-- FROM brands b
-- WHERE cp.brand_id = b.id
--   AND (cp.is_deleted = 0 OR cp.is_deleted IS NULL)
--   AND cp.name <> b.name
--   AND (cp.personality_profile IS NULL
--        OR cp.personality_profile::text IN ('', '{}', 'null'))
--   AND cp.name NOT LIKE '采访档案-%'
--   AND NOT EXISTS (
--       SELECT 1 FROM client_profiles cp2
--       WHERE cp2.brand_id = cp.brand_id
--         AND cp2.id <> cp.id
--         AND (cp2.is_deleted = 0 OR cp2.is_deleted IS NULL)
--         AND cp2.name = b.name
--   );

ROLLBACK;  -- 默认 rollback，审阅 OK 后改 COMMIT + 打开 Step 3-2 UPDATE 再执行
