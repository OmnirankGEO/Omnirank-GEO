-- ============================================================
-- [#15] 竞品数据从全局 JSON 迁移到数据库表 + user_id 隔离
-- ============================================================
-- 背景：
--   原实现 data/competitors.json 是全局单文件，所有租户共享
--   任何登录用户都能读/改/删别人的竞品数据 — P0 数据泄漏
-- 修复：
--   ① 新建 competitors 表带 user_id/brand_id 隔离
--   ② 新建 competitor_snapshots 子表
--   ③ 索引覆盖 user_id / brand_id / profile_id 三个常用过滤条件
--   ④ 软删字段（is_deleted/deleted_at），跟 Phase 3 全局软删方案对齐
-- 部署顺序：
--   1. 先跑这个 migration（创建表）
--   2. 再跑 scripts/migrate_competitor_json_to_db.py（如有 JSON 数据迁过去）
--   3. 应用层切换（部署新代码）
-- 回滚：见 migration_competitor_table_rollback.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS competitors (
    id              TEXT        PRIMARY KEY,            -- 沿用原 JSON 的 comp_xxx 格式
    user_id         INTEGER     NOT NULL,               -- ★ 拥有者，RBAC 隔离的核心
    brand_id        INTEGER,                            -- 关联品牌（可空，沿用旧数据）
    profile_id      TEXT,                               -- 关联客户档案（可空）
    name            TEXT        NOT NULL,
    platform        TEXT        NOT NULL,               -- douyin / xiaohongshu / kuaishou
    account_id      TEXT,                               -- 平台账号 ID
    account_url     TEXT,                               -- 账号链接
    notes           TEXT,
    stats           JSONB       DEFAULT '{}'::jsonb,    -- 同步后的统计数据
    last_sync       TIMESTAMP,
    created_at      TIMESTAMP   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP   NOT NULL DEFAULT NOW(),

    -- 软删字段（跟阶段 3 全局软删对齐）
    is_deleted      BOOLEAN     DEFAULT FALSE,
    deleted_at      TIMESTAMP,
    deleted_by      INTEGER,
    delete_reason   TEXT
);

CREATE INDEX IF NOT EXISTS idx_competitors_user_id     ON competitors(user_id) WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_competitors_brand_id    ON competitors(brand_id) WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_competitors_profile_id  ON competitors(profile_id) WHERE is_deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_competitors_platform    ON competitors(platform) WHERE is_deleted = FALSE;


CREATE TABLE IF NOT EXISTS competitor_snapshots (
    id                  SERIAL      PRIMARY KEY,
    competitor_id       TEXT        NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    user_id             INTEGER     NOT NULL,           -- 冗余存一份方便过滤
    snapshot_type       TEXT        DEFAULT 'content',  -- content / stats
    payload             JSONB       NOT NULL,           -- 实际快照数据（视频列表 / 粉丝数等）
    created_at          TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_competitor_snapshots_cid  ON competitor_snapshots(competitor_id);
CREATE INDEX IF NOT EXISTS idx_competitor_snapshots_uid  ON competitor_snapshots(user_id);


-- 元数据：记录这次迁移的执行时间，便于回滚定位
DO $$
BEGIN
    INSERT INTO migrations_log (name, executed_at, notes)
    VALUES (
        'migration_competitor_table',
        NOW(),
        '#15 竞品数据从全局 JSON 迁移到 DB 表 + user_id 隔离'
    )
    ON CONFLICT DO NOTHING;
EXCEPTION WHEN undefined_table THEN
    -- migrations_log 表不存在就跳过（不影响主迁移）
    NULL;
END $$;
