-- ============================================================
-- v3.7 Migration · L0 用户多 profile / 多 client-brand 数据清理
-- ============================================================
-- 作者: CTO-13.0 · 2026-04-19 · T8
-- 修订 v2: Deploy-CTO 反馈 schema 错误已修 —
--   users.agent_level → user_wallets.agent_level
--   users.is_admin    → user_roles + roles.name='admin' 判定
-- 修订 v3: Deploy-CTO 再次反馈 brands 打分字段错 —
--   原用了 profile 的字段 (business/target_users/core_keywords/competitors)
--   brands 真实字段在 db/diagnosis_db.py:116 定义：
--     industry / industry_category / company_name / cities / contact / notes
--     diagnosis_count / latest_score / brand_type / is_deleted
--   profile 打分字段不动（Deploy-CTO 确认 client_profiles 列对的）
-- ============================================================
-- 背景: CTO-15.0 commit 3909898 实施"L0 限 1 brand"后，老板要求：
--   1. L0 现有多品牌 / 多 profile 的脏数据清理（保留信息最多的，其他软删）
--   2. profile 层也加 L0 限 1 策略（本 migration 配合代码改动）
--   3. 代理 L1+ 不受影响（多品牌/多档案是代理特权）
-- ============================================================
-- L0 判定（= 非代理 AND 非 admin）:
--   L0 = LEFT JOIN user_wallets 得到 (agent_level IS NULL OR < 1)
--        AND NOT EXISTS(user_roles ur JOIN roles r ON r.id=ur.role_id WHERE name='admin')
-- ============================================================
-- 执行: docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v3_7_l0_quota_cleanup.sql
-- 回滚: 用备份表 _migration_v3_7_backup_profiles / _backup_brands 恢复
-- ============================================================

DO $$
DECLARE
    v_cleaned_profiles INT := 0;
    v_cleaned_brands INT := 0;
BEGIN
    IF EXISTS (SELECT 1 FROM _migration_markers WHERE marker = 'v3_7_l0_quota_cleanup') THEN
        RAISE NOTICE 'v3.7 L0 额度清理已执行过，跳过';
        RETURN;
    END IF;

    -- ========== 备份（仅保存被软删的 · 用于回滚） ==========
    DROP TABLE IF EXISTS _migration_v3_7_backup_profiles;
    CREATE TABLE _migration_v3_7_backup_profiles AS
    SELECT * FROM client_profiles WHERE 1=0;

    DROP TABLE IF EXISTS _migration_v3_7_backup_brands;
    CREATE TABLE _migration_v3_7_backup_brands AS
    SELECT * FROM brands WHERE 1=0;

    -- ========== 可重用 CTE：L0 用户 ID 集合 ==========
    -- 每个 Step 都用到，定义成 WITH 子句里的 CTE

    -- ========== Step 1 · L0 多 client-brand 软删 ==========
    WITH l0_users AS (
        SELECT u.id AS user_id
        FROM users u
        LEFT JOIN user_wallets uw ON uw.user_id = u.id
        WHERE (uw.agent_level IS NULL OR uw.agent_level < 1)
          AND NOT EXISTS (
            SELECT 1 FROM user_roles ur
            INNER JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = u.id AND r.name = 'admin'
          )
    ),
    brand_scores AS (
        SELECT
            b.id AS brand_id,
            b.owner_user_id,
            (
                (CASE WHEN b.industry IS NOT NULL AND b.industry != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.industry_category IS NOT NULL AND b.industry_category != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.cities IS NOT NULL AND b.cities != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.company_name IS NOT NULL AND b.company_name != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.contact IS NOT NULL AND b.contact != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.notes IS NOT NULL AND b.notes != '' THEN 1 ELSE 0 END) +
                (CASE WHEN COALESCE(b.diagnosis_count, 0) > 0 THEN 2 ELSE 0 END) +
                (CASE WHEN COALESCE(b.latest_score, 0) > 0 THEN 1 ELSE 0 END)
            ) AS score,
            b.created_at
        FROM brands b
        INNER JOIN l0_users lu ON lu.user_id = b.owner_user_id
        WHERE b.brand_type IN ('client', 'legacy')
          AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
    ),
    ranked_brands AS (
        SELECT
            brand_id,
            ROW_NUMBER() OVER (PARTITION BY owner_user_id ORDER BY score DESC, created_at ASC) AS rn
        FROM brand_scores
    )
    INSERT INTO _migration_v3_7_backup_brands
    SELECT * FROM brands
    WHERE id IN (SELECT brand_id FROM ranked_brands WHERE rn > 1);

    GET DIAGNOSTICS v_cleaned_brands = ROW_COUNT;

    -- 实际软删
    WITH l0_users AS (
        SELECT u.id AS user_id
        FROM users u
        LEFT JOIN user_wallets uw ON uw.user_id = u.id
        WHERE (uw.agent_level IS NULL OR uw.agent_level < 1)
          AND NOT EXISTS (
            SELECT 1 FROM user_roles ur
            INNER JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = u.id AND r.name = 'admin'
          )
    ),
    brand_scores AS (
        SELECT
            b.id AS brand_id,
            b.owner_user_id,
            (
                (CASE WHEN b.industry IS NOT NULL AND b.industry != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.industry_category IS NOT NULL AND b.industry_category != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.cities IS NOT NULL AND b.cities != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.company_name IS NOT NULL AND b.company_name != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.contact IS NOT NULL AND b.contact != '' THEN 1 ELSE 0 END) +
                (CASE WHEN b.notes IS NOT NULL AND b.notes != '' THEN 1 ELSE 0 END) +
                (CASE WHEN COALESCE(b.diagnosis_count, 0) > 0 THEN 2 ELSE 0 END) +
                (CASE WHEN COALESCE(b.latest_score, 0) > 0 THEN 1 ELSE 0 END)
            ) AS score,
            b.created_at
        FROM brands b
        INNER JOIN l0_users lu ON lu.user_id = b.owner_user_id
        WHERE b.brand_type IN ('client', 'legacy')
          AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
    ),
    ranked_brands AS (
        SELECT
            brand_id,
            ROW_NUMBER() OVER (PARTITION BY owner_user_id ORDER BY score DESC, created_at ASC) AS rn
        FROM brand_scores
    )
    UPDATE brands SET is_deleted = TRUE
    WHERE id IN (SELECT brand_id FROM ranked_brands WHERE rn > 1);

    -- ========== Step 2 · L0 多 profile 软删 ==========
    WITH l0_users AS (
        SELECT u.id AS user_id
        FROM users u
        LEFT JOIN user_wallets uw ON uw.user_id = u.id
        WHERE (uw.agent_level IS NULL OR uw.agent_level < 1)
          AND NOT EXISTS (
            SELECT 1 FROM user_roles ur
            INNER JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = u.id AND r.name = 'admin'
          )
    ),
    profile_scores AS (
        SELECT
            p.id AS profile_id,
            lu.user_id,
            (
                (CASE WHEN p.industry IS NOT NULL AND p.industry != '' THEN 1 ELSE 0 END) +
                (CASE WHEN p.business IS NOT NULL AND p.business != '' THEN 1 ELSE 0 END) +
                (CASE WHEN p.persona_tone IS NOT NULL AND p.persona_tone != '' THEN 1 ELSE 0 END) +
                (CASE WHEN p.persona_positioning IS NOT NULL AND p.persona_positioning != '' THEN 1 ELSE 0 END) +
                (CASE WHEN p.persona_catchphrases IS NOT NULL AND p.persona_catchphrases != '' AND p.persona_catchphrases NOT IN ('[]', '{}', 'null') THEN 1 ELSE 0 END) +
                (CASE WHEN p.structured_knowledge IS NOT NULL AND p.structured_knowledge::text != '{}' AND p.structured_knowledge::text != 'null' THEN 2 ELSE 0 END) +
                (CASE WHEN p.target_platforms IS NOT NULL AND p.target_platforms != '' AND p.target_platforms NOT IN ('[]', '{}', 'null') THEN 1 ELSE 0 END)
            ) AS score,
            p.created_at
        FROM client_profiles p
        INNER JOIN brands b ON b.id = p.brand_id
        INNER JOIN l0_users lu ON lu.user_id = b.owner_user_id
        WHERE (p.is_deleted IS NULL OR p.is_deleted = 0)
    ),
    ranked_profiles AS (
        SELECT
            profile_id,
            ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY score DESC, created_at ASC) AS rn
        FROM profile_scores
    )
    INSERT INTO _migration_v3_7_backup_profiles
    SELECT * FROM client_profiles
    WHERE id IN (SELECT profile_id FROM ranked_profiles WHERE rn > 1);

    GET DIAGNOSTICS v_cleaned_profiles = ROW_COUNT;

    -- 实际软删
    WITH l0_users AS (
        SELECT u.id AS user_id
        FROM users u
        LEFT JOIN user_wallets uw ON uw.user_id = u.id
        WHERE (uw.agent_level IS NULL OR uw.agent_level < 1)
          AND NOT EXISTS (
            SELECT 1 FROM user_roles ur
            INNER JOIN roles r ON r.id = ur.role_id
            WHERE ur.user_id = u.id AND r.name = 'admin'
          )
    ),
    profile_scores AS (
        SELECT
            p.id AS profile_id,
            lu.user_id,
            (
                (CASE WHEN p.industry IS NOT NULL AND p.industry != '' THEN 1 ELSE 0 END) +
                (CASE WHEN p.business IS NOT NULL AND p.business != '' THEN 1 ELSE 0 END) +
                (CASE WHEN p.persona_tone IS NOT NULL AND p.persona_tone != '' THEN 1 ELSE 0 END) +
                (CASE WHEN p.persona_positioning IS NOT NULL AND p.persona_positioning != '' THEN 1 ELSE 0 END) +
                (CASE WHEN p.persona_catchphrases IS NOT NULL AND p.persona_catchphrases != '' AND p.persona_catchphrases NOT IN ('[]', '{}', 'null') THEN 1 ELSE 0 END) +
                (CASE WHEN p.structured_knowledge IS NOT NULL AND p.structured_knowledge::text != '{}' AND p.structured_knowledge::text != 'null' THEN 2 ELSE 0 END) +
                (CASE WHEN p.target_platforms IS NOT NULL AND p.target_platforms != '' AND p.target_platforms NOT IN ('[]', '{}', 'null') THEN 1 ELSE 0 END)
            ) AS score,
            p.created_at
        FROM client_profiles p
        INNER JOIN brands b ON b.id = p.brand_id
        INNER JOIN l0_users lu ON lu.user_id = b.owner_user_id
        WHERE (p.is_deleted IS NULL OR p.is_deleted = 0)
    ),
    ranked_profiles AS (
        SELECT
            profile_id,
            ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY score DESC, created_at ASC) AS rn
        FROM profile_scores
    )
    UPDATE client_profiles SET is_deleted = 1
    WHERE id IN (SELECT profile_id FROM ranked_profiles WHERE rn > 1);

    -- ========== Step 3 · 记录 marker ==========
    INSERT INTO _migration_markers (marker, applied_at, note)
    VALUES (
        'v3_7_l0_quota_cleanup',
        NOW(),
        FORMAT('L0 清理: 软删 %s 个非-self 品牌 + %s 个 profile', v_cleaned_brands, v_cleaned_profiles)
    );

    RAISE NOTICE 'v3.7 L0 额度清理完成：软删 % 个品牌 + % 个 profile', v_cleaned_brands, v_cleaned_profiles;
    RAISE NOTICE '备份表：_migration_v3_7_backup_brands / _migration_v3_7_backup_profiles';
    RAISE NOTICE '回滚：UPDATE brands/client_profiles SET is_deleted=FALSE WHERE id IN (SELECT id FROM 备份表)';
END $$;
