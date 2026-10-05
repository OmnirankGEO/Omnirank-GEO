-- [CTO-13.0] 回填"仿写脚本 project_id=NULL"孤儿 row
-- 2026-04-19 · 起因：老板反馈"仿写历史没有记录"
-- 根因：/api/social/rewrite 的 save_script 传 project_id=data.project_id，前端只传 profile_id
--       不传 project_id → 入库 project_id=NULL → /api/content/scripts 按 project_id 过滤读不到
-- 代码已在 server.py 两处修复（profile_id fallback 出 project_id）；本 SQL 救回被静默丢的历史

-- ============================================================
-- Step 1. 列出所有孤儿 rewrite scripts（诊断）
-- ============================================================
SELECT
    ss.id,
    ss.user_id,
    u.username,
    u.display_name,
    ss.project_id,
    ss.profile_id,
    ss.title,
    ss.source_video_url,
    ss.created_at
FROM social_scripts ss
LEFT JOIN users u ON u.user_id = ss.user_id::integer
WHERE ss.source = 'rewrite'
  AND ss.project_id IS NULL
ORDER BY ss.created_at DESC;

-- ============================================================
-- Step 2. 老板（user_id=39）最近的孤儿 → 绑到 hs5G4Vtv 对应 project
-- ============================================================
-- 先看老板 hs5G4Vtv 对应的 project_id
SELECT id, profile_id, name, industry
FROM social_projects
WHERE profile_id = 'hs5G4Vtv' AND status = 'active'
ORDER BY created_at DESC LIMIT 1;

-- 记下上面查到的 project_id（假设是 :BOSS_PROJECT_ID），执行回填：
-- ⚠️ 把 123 改成上面 SELECT 查到的 project.id
--
-- BEGIN;
-- UPDATE social_scripts
-- SET project_id = 123,     -- ← 填 hs5G4Vtv 对应的 project.id
--     profile_id = 'hs5G4Vtv'
-- WHERE source = 'rewrite'
--   AND project_id IS NULL
--   AND user_id = '39'       -- 老板（user_id 可能存为 text 格式，按实际调）
--   AND created_at > NOW() - INTERVAL '6 hours';  -- 只捞最近 6h，更早的另外判断
-- -- 验证：
-- SELECT id, title, project_id, profile_id, created_at
-- FROM social_scripts WHERE id IN ( ... );
-- COMMIT;  -- 审阅 OK 后打开
-- -- 或 ROLLBACK;

-- ============================================================
-- Step 3. 其他用户孤儿（可选）
-- ============================================================
-- 对每个 user 统计他有几个 active self profile
-- 如果只有 1 个 self profile → 可以自动回填到那个 profile 对应的 project
-- 如果有多个 → 跳过，让用户下次仿写自然生成新记录
SELECT
    ss.user_id,
    COUNT(DISTINCT ss.id) AS orphan_count,
    COUNT(DISTINCT cp.id) FILTER (WHERE b.brand_type = 'self') AS self_profile_count,
    array_agg(DISTINCT cp.id) FILTER (WHERE b.brand_type = 'self') AS self_profile_ids
FROM social_scripts ss
LEFT JOIN brands b ON b.owner_user_id = ss.user_id::integer
LEFT JOIN client_profiles cp ON cp.brand_id = b.id AND (cp.is_deleted = 0 OR cp.is_deleted IS NULL)
WHERE ss.source = 'rewrite'
  AND ss.project_id IS NULL
GROUP BY ss.user_id
ORDER BY orphan_count DESC;

-- 对 self_profile_count = 1 的 user 可批量回填；= 0 或 > 1 的人工判断
