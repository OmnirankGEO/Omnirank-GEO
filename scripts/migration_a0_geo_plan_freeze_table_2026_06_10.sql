-- [A0] geo_plan_tasks 加 freeze_table 列:存冻结句柄所在表标记('legacy'|'v35')
-- 根因:geo_plan worker 按 freeze_id(整数自增·跨 point_freezes/customer_credit_freezes 两表必撞号)
-- 调 commit_freeze/release_freeze。A0 让 worker 回传显式 freeze_table 免猜,根除撞号歧义。
-- 列可空(老任务/legacy 无标记时回落 A1+A2+A3 安全消歧)。additive·零回归。
-- ⚠️ 部署序:本 migration 必须先于代码(update_freeze_id 写该列);代码侧已对列缺失做降级兜底,
--    但仍应 migration 先行(feedback_deploy_order_db_before_code)。
BEGIN;

ALTER TABLE geo_plan_tasks
    ADD COLUMN IF NOT EXISTS freeze_table TEXT;

COMMIT;

-- 核验:
-- SELECT column_name FROM information_schema.columns
--  WHERE table_name='geo_plan_tasks' AND column_name='freeze_table';
