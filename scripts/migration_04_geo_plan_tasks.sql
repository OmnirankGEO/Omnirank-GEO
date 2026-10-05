-- ============================================================
-- Phase 4 · Migration · 新建 geo_plan_tasks 表
-- ============================================================
-- 作者: CTO-15.5 · 2026-04-20
-- 立项: .planning/phases/04-c-geo/PRD.md Section 4.1 (GEO-REQ-DB-1/2)
-- 背景: C 端 GEO 方案从同步超时降级重构成异步任务,需持久化任务状态.
-- ============================================================
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_04_geo_plan_tasks.sql
-- 验证:
--   docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d+ geo_plan_tasks"
-- 回滚:
--   DROP TABLE IF EXISTS geo_plan_tasks;
--   DELETE FROM _migration_markers WHERE marker = 'phase_04_geo_plan_tasks';
-- ============================================================

DO $$
BEGIN
    -- 兼容生产:真实表名是 _migrations (name, applied_at),不是 _migration_markers
    IF NOT EXISTS (SELECT 1 FROM _migrations WHERE name = 'phase_04_geo_plan_tasks') THEN

        -- 主表
        CREATE TABLE IF NOT EXISTS geo_plan_tasks (
            id                BIGSERIAL PRIMARY KEY,
            user_id           INTEGER     NOT NULL,
            brand_id          INTEGER     NOT NULL,
            status            VARCHAR(16) NOT NULL DEFAULT 'queued'
                              CHECK (status IN ('queued','running','done','failed','cancelled','timeout')),
            -- 启动时参数快照 (前端传来的 request body)
            params_json       JSONB       NOT NULL DEFAULT '{}'::jsonb,
            -- brand + profile 完整快照 (D13: 防 brand 中途被改/删)
            brand_snapshot    JSONB       NOT NULL DEFAULT '{}'::jsonb,
            -- 进度 (5 阶段: identify/expand/audit/cluster/pricing/done)
            progress_stage    VARCHAR(32),
            progress_percent  SMALLINT    NOT NULL DEFAULT 0 CHECK (progress_percent BETWEEN 0 AND 100),
            progress_message  TEXT,
            -- 结果 + 错误
            result_json       JSONB,
            error_code        VARCHAR(32),
            error_detail      TEXT,
            -- 时间戳
            queued_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at        TIMESTAMPTZ,
            heartbeat_at      TIMESTAMPTZ,
            done_at           TIMESTAMPTZ,
            -- 模式 + 来源
            data_mode         VARCHAR(16) NOT NULL DEFAULT 'full'
                              CHECK (data_mode IN ('full','l1l2_fallback')),
            source            VARCHAR(32) NOT NULL,
            ip_at_start       VARCHAR(64),
            -- 关联 (前端确认方案后回填)
            linked_quote_id   INTEGER,
            archived_at       TIMESTAMPTZ,
            -- 扣费 freeze 引用 (PLAN 01 Task 2.1.e 先 create 后 freeze,所以 nullable)
            freeze_id         INTEGER
        );

        -- 索引 (PRD GEO-REQ-DB-2 · 4 个)

        -- idx_user_status: /tasks 列表页 + count_running_for_user
        -- 加部分索引 WHERE archived_at IS NULL 减小索引体积 (90 天归档后不参与筛)
        CREATE INDEX IF NOT EXISTS idx_geoplan_user_status
            ON geo_plan_tasks(user_id, status) WHERE archived_at IS NULL;

        -- idx_brand_status: 同品牌去重 (D1)
        CREATE INDEX IF NOT EXISTS idx_geoplan_brand_status
            ON geo_plan_tasks(brand_id, status) WHERE status IN ('queued','running');

        -- idx_heartbeat: zombie killer 扫表 (D8)
        CREATE INDEX IF NOT EXISTS idx_geoplan_heartbeat
            ON geo_plan_tasks(heartbeat_at) WHERE status = 'running';

        -- idx_cleanup: 90 天归档任务扫表 (D15)
        CREATE INDEX IF NOT EXISTS idx_geoplan_cleanup
            ON geo_plan_tasks(done_at) WHERE done_at IS NOT NULL;

        -- 标记 migration
        INSERT INTO _migrations (name, applied_at)
        VALUES ('phase_04_geo_plan_tasks', NOW())
        ON CONFLICT (name) DO NOTHING;

        RAISE NOTICE 'Phase 4 · geo_plan_tasks 表已创建 + 4 索引 + migration 标记';
    ELSE
        RAISE NOTICE 'Phase 4 · geo_plan_tasks 已存在,跳过 (marker = phase_04_geo_plan_tasks)';
    END IF;
END $$;
