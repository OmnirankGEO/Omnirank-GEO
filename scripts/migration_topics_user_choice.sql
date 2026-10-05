-- v2.9 GEO 文体改造 · topics.user_choice JSONB 持久化(Codex v2.8 复审 P0)
-- 主线 migration · Deploy-CTO 在生产 DB 跑(蓝绿启动前)
-- 兜底:db/diagnosis_db.py init_db 内 _safe_add_column 幂等防漏
--
-- 闭环根因(v2.8 漏的):
-- 1. 用户在 dropdown 选"价格预算" → regenerate-titles 只更 optimized_title/article_style/status
-- 2. user_choice 只存在前端 state · 没落 DB
-- 3. 前端 loadProjectDetail reload 后 · user_choice state 丢
-- 4. start-articles 拿空 user_choices map · 后端从 DB 取 topic 默认 'auto' · 走 ratio 抽
-- 5. 结果:标题是"价格解读"风格 · 正文按 ratio 随机 · 4 层不一致

ALTER TABLE topics
    ADD COLUMN IF NOT EXISTS user_choice VARCHAR(32) DEFAULT NULL;

COMMENT ON COLUMN topics.user_choice IS
    'v2.9 用户主动选的文体(10 项 USER_CHOICE 之一 + NULL)· 持久化层 · 启动写作时后端从此字段恢复 · NULL=系统推荐(走 ratio)';

-- staging dry-run:
--   BEGIN;
--     \i scripts/migration_topics_user_choice.sql
--     \d topics
--   ROLLBACK;
