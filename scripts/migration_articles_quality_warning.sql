-- v2.7.1 GEO 文体改造 · articles.quality_warning JSONB 字段
-- 主线 migration · Deploy-CTO 在生产 DB 跑(蓝绿启动前)
-- 兜底:db/diagnosis_db.py init_db 内 _safe_add_column 幂等防漏
-- 关联 §4.9.1 admin /api/admin/articles/quality-warning 查询

ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS quality_warning JSONB DEFAULT NULL;

COMMENT ON COLUMN articles.quality_warning IS
    'v2.7.1 半强制结构 check 重写 1 次后仍 fail 的 H/S 编号 · 格式 {"hard": ["H1","H3"], "soft": ["S2"], "checked_at": "ISO8601"} · NULL 表示全过';

-- 配套字段:style_code 单一权威(向后兼容 · 旧 style 字段保留)
ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS style_code VARCHAR(64) DEFAULT NULL;

COMMENT ON COLUMN articles.style_code IS
    'v2.7.1 单一权威 style 字段 · 优先读此字段 · 旧 style 字段仅历史 fallback';

-- 索引:admin 查质量警告文章用
CREATE INDEX IF NOT EXISTS idx_articles_quality_warning_not_null
    ON articles((quality_warning IS NOT NULL))
    WHERE quality_warning IS NOT NULL;

-- staging dry-run 验证示例:
--   BEGIN;
--     \i scripts/migration_articles_quality_warning.sql
--     \d articles
--   ROLLBACK;
