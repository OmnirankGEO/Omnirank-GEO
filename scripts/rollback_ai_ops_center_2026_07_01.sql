-- ============================================================================
-- AI Ops Center 回滚 · 2026-07-01
-- ----------------------------------------------------------------------------
-- 仅在必须彻底回滚时手动执行(Deploy-CTO 操作),不进 _run_sql_migrations 自动路径。
-- 默认保留审计:回滚优先靠关 flag(ai_ops.enabled=false / kill_switch=true)+ 停 Worker,
-- 而不是 DROP 表。只有确认无需保留任何 AI Ops 审计数据时才跑本文件。
--
-- 依赖顺序:先删引用 ai_ops_tasks 的子表,再删主表。
-- ============================================================================

DROP TABLE IF EXISTS ai_ops_task_events CASCADE;
DROP TABLE IF EXISTS ai_ops_artifacts CASCADE;
DROP TABLE IF EXISTS ai_ops_approvals CASCADE;
DROP TABLE IF EXISTS ai_ops_reports CASCADE;
DROP TABLE IF EXISTS ai_ops_policies CASCADE;
-- P1-B 心跳表(独立 · 无 FK)· 彻底回滚必须不遗留表:
DROP TABLE IF EXISTS ai_ops_worker_heartbeats CASCADE;
-- 包B 巡逻(alerts 引用 ai_ops_tasks,先删):
DROP TABLE IF EXISTS ai_ops_alerts CASCADE;
DROP TABLE IF EXISTS ai_ops_patrol_runs CASCADE;
DROP TABLE IF EXISTS ai_ops_tasks CASCADE;
