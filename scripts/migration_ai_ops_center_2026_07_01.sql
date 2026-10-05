-- ============================================================================
-- AI Ops Center (AI 运维控制塔) 迁移 · 2026-07-01
-- ----------------------------------------------------------------------------
-- 新增 6 张表:任务总线 + 事件流 + 产物 + 审批 + 日报 + 策略/Kill Switch。
--
-- 落地约定(与 server.py:_run_sql_migrations 一致):
--   - 本文件由 _run_sql_migrations() 在 autocommit 下整体 execute。
--   - 运行器会剥离外层 BEGIN/COMMIT 与 psql \ 指令,所以这里不写事务包裹、不写 \ 指令。
--   - 全部 CREATE TABLE / INDEX IF NOT EXISTS,幂等、可重复跑、向后兼容。
--
-- 载入顺序注意(重要):
--   _run_sql_migrations() 在 server.py 里先于 init_faq_tables() 执行。
--   因此本迁移在全新库跑时 faq_feedback 可能还不存在。
--   feedback_id 故意用软整数列(不加硬 FK REFERENCES faq_feedback),
--   避免"表不存在→整段迁移失败→ai_ops_* 建不出来"。归属校验在应用层做。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. ai_ops_tasks · 运维任务主表(任务总线)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_ops_tasks (
  id BIGSERIAL PRIMARY KEY,
  task_key TEXT UNIQUE,
  source_type TEXT NOT NULL DEFAULT 'manual'
    CHECK (source_type IN ('feedback','chat','schedule','alert','manual')),
  source_id TEXT NOT NULL DEFAULT '',
  source_context_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  feedback_id INTEGER NULL,  -- 软链 faq_feedback.id(见文件头载入顺序注意),归属校验在应用层
  kind TEXT NOT NULL DEFAULT 'diagnose'
    CHECK (kind IN ('diagnose','fix','report','ssh_action','code_review')),
  title TEXT NOT NULL DEFAULT '',
  instruction TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','waiting_approval','succeeded','failed','cancelled')),
  risk_level TEXT NOT NULL DEFAULT 'L0'
    CHECK (risk_level IN ('L0','L1','L2','L3','L4')),
  priority TEXT NOT NULL DEFAULT 'P3'
    CHECK (priority IN ('P0','P1','P2','P3')),
  created_by INTEGER NULL,
  assigned_worker_id TEXT NOT NULL DEFAULT '',
  codex_session_id TEXT NOT NULL DEFAULT '',
  worktree_path TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  result_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
  started_at TIMESTAMP NULL,
  finished_at TIMESTAMP NULL
);

-- @index-guard idx_ai_ops_tasks_status_priority ON ai_ops_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_tasks_status_priority' AND i.indrelid = to_regclass('public.ai_ops_tasks')) THEN
        NULL;  -- 已在 public.ai_ops_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_tasks_status_priority' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_tasks_status_priority 已存在但不在 public.ai_ops_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_tasks_status_priority' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_tasks_status_priority ON public.ai_ops_tasks (status, priority, created_at DESC);
    END IF;
END $idxguard$;

-- @index-guard idx_ai_ops_tasks_feedback ON ai_ops_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_tasks_feedback' AND i.indrelid = to_regclass('public.ai_ops_tasks')) THEN
        NULL;  -- 已在 public.ai_ops_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_tasks_feedback' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_tasks_feedback 已存在但不在 public.ai_ops_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_tasks_feedback' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_tasks_feedback ON public.ai_ops_tasks (feedback_id) WHERE feedback_id IS NOT NULL;
    END IF;
END $idxguard$;

-- @index-guard idx_ai_ops_tasks_kind_created ON ai_ops_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_tasks_kind_created' AND i.indrelid = to_regclass('public.ai_ops_tasks')) THEN
        NULL;  -- 已在 public.ai_ops_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_tasks_kind_created' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_tasks_kind_created 已存在但不在 public.ai_ops_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_tasks_kind_created' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_tasks_kind_created ON public.ai_ops_tasks (kind, created_at DESC);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 2. ai_ops_task_events · 任务事件流(前端时间线 + Codex JSONL 摘要 + 审计)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_ops_task_events (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES ai_ops_tasks(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info'
    CHECK (severity IN ('info','warn','error','security')),
  message TEXT NOT NULL DEFAULT '',
  payload_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_ai_ops_events_task ON ai_ops_task_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_events_task' AND i.indrelid = to_regclass('public.ai_ops_task_events')) THEN
        NULL;  -- 已在 public.ai_ops_task_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_events_task' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_events_task 已存在但不在 public.ai_ops_task_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_events_task' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_events_task ON public.ai_ops_task_events (task_id, created_at ASC);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 3. ai_ops_artifacts · 任务产物(context / codex 输出 / patch / 测试日志 / 日报 / 截图)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_ops_artifacts (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES ai_ops_tasks(id) ON DELETE CASCADE,
  artifact_type TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  storage_type TEXT NOT NULL DEFAULT 'db'
    CHECK (storage_type IN ('db','file','oss')),
  content_text TEXT NOT NULL DEFAULT '',
  storage_url TEXT NOT NULL DEFAULT '',
  metadata_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_ai_ops_artifacts_task_type ON ai_ops_artifacts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_artifacts_task_type' AND i.indrelid = to_regclass('public.ai_ops_artifacts')) THEN
        NULL;  -- 已在 public.ai_ops_artifacts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_artifacts_task_type' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_artifacts_task_type 已存在但不在 public.ai_ops_artifacts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_artifacts_task_type' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_artifacts_task_type ON public.ai_ops_artifacts (task_id, artifact_type, created_at DESC);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 4. ai_ops_approvals · 高危动作审批(L3/L4)· 只存计划,不存密钥
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_ops_approvals (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES ai_ops_tasks(id) ON DELETE CASCADE,
  action_type TEXT NOT NULL,
  risk_level TEXT NOT NULL DEFAULT 'L4'
    CHECK (risk_level IN ('L3','L4')),
  approval_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (approval_status IN ('pending','approved','rejected','expired','executed')),
  requested_by INTEGER NULL,
  requested_reason TEXT NOT NULL DEFAULT '',
  command_plan_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  approved_by INTEGER NULL,
  approved_at TIMESTAMP NULL,
  expires_at TIMESTAMP NULL,
  executed_at TIMESTAMP NULL,   -- SSH Runner 执行后打点(幂等 · 防每轮重复执行)
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_ai_ops_approvals_status ON ai_ops_approvals plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_approvals_status' AND i.indrelid = to_regclass('public.ai_ops_approvals')) THEN
        NULL;  -- 已在 public.ai_ops_approvals 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_approvals_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_approvals_status 已存在但不在 public.ai_ops_approvals 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_approvals_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_approvals_status ON public.ai_ops_approvals (approval_status, created_at DESC);
    END IF;
END $idxguard$;

-- @index-guard idx_ai_ops_approvals_task ON ai_ops_approvals plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_approvals_task' AND i.indrelid = to_regclass('public.ai_ops_approvals')) THEN
        NULL;  -- 已在 public.ai_ops_approvals 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_approvals_task' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_approvals_task 已存在但不在 public.ai_ops_approvals 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_approvals_task' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_approvals_task ON public.ai_ops_approvals (task_id, created_at DESC);
    END IF;
END $idxguard$;

-- 幂等补丁:若某环境跑过本文件早期版本(表已存在 · 无 executed_at · 4 态 CHECK),
-- CREATE TABLE IF NOT EXISTS 不会补列/改 CHECK,这里显式补齐到当前 5 态。
ALTER TABLE ai_ops_approvals ADD COLUMN IF NOT EXISTS executed_at TIMESTAMP NULL;
DO $$
BEGIN
  ALTER TABLE ai_ops_approvals DROP CONSTRAINT IF EXISTS ai_ops_approvals_approval_status_check;
  ALTER TABLE ai_ops_approvals ADD CONSTRAINT ai_ops_approvals_approval_status_check
    CHECK (approval_status IN ('pending','approved','rejected','expired','executed'));
END $$;

-- ---------------------------------------------------------------------------
-- 5. ai_ops_reports · 每日运维/运营日报 · (report_date, report_type) 唯一(幂等 guard)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_ops_reports (
  id BIGSERIAL PRIMARY KEY,
  report_date DATE NOT NULL,
  report_type TEXT NOT NULL DEFAULT 'daily'
    CHECK (report_type IN ('daily','weekly','manual')),
  status TEXT NOT NULL DEFAULT 'generating'
    CHECK (status IN ('generating','ready','failed')),
  markdown TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  metrics_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  action_items_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb,
  generated_by_task_id BIGINT NULL REFERENCES ai_ops_tasks(id) ON DELETE SET NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
  UNIQUE (report_date, report_type)
);

-- @index-guard idx_ai_ops_reports_date ON ai_ops_reports plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_reports_date' AND i.indrelid = to_regclass('public.ai_ops_reports')) THEN
        NULL;  -- 已在 public.ai_ops_reports 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_reports_date' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_reports_date 已存在但不在 public.ai_ops_reports 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_reports_date' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_reports_date ON public.ai_ops_reports (report_date DESC, report_type);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 6. ai_ops_policies · 权限策略与 Kill Switch(key/value)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_ops_policies (
  key TEXT PRIMARY KEY,
  value_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_by INTEGER NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- 默认策略:全部保守(整体默认关闭 · 只允许只读诊断 · 修复/SSH/Kill/命令台LLM 全关)
-- 幂等:ON CONFLICT DO NOTHING;_run_sql_migrations 每次启动重跑本文件,
-- 跑过旧版迁移的环境重启后也会自动补上后加的 key(如 ai_ops.chat_llm.enabled)。
INSERT INTO ai_ops_policies (key, value_jsonb) VALUES
  ('ai_ops.enabled',        '{"enabled": false}'::jsonb),
  ('codex.diagnose.enabled','{"enabled": true}'::jsonb),
  ('codex.fix.enabled',     '{"enabled": false}'::jsonb),
  ('ssh_runner.enabled',    '{"enabled": false}'::jsonb),
  ('ai_ops.kill_switch',    '{"enabled": false}'::jsonb),
  ('ai_ops.chat_llm.enabled','{"enabled": false}'::jsonb),
  ('ai_ops.glm_triage.enabled','{"enabled": false}'::jsonb),
  ('auto_deploy.enabled',   '{"enabled": false}'::jsonb),
  ('ai_ops.auto_create_from_feedback','{"enabled": false}'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 7. ai_ops_worker_heartbeats · Runner 心跳(P1-B)
-- ----------------------------------------------------------------------------
-- Worker 常驻循环每 30-60s upsert 一行,让控制塔知道 Runner 到底"在线 / 离线 /
-- 在线但未授权执行"。worker_id 作主键(单 Runner 一行 · 多 Runner 多行)。
-- 只存运行元数据,绝不存密钥。env AI_OPS_ENABLED=false 也照常心跳(证明在线未授权)。
-- Runner 受限 DB 角色需 GRANT SELECT, INSERT, UPDATE 本表(见 provision checklist §4)。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_ops_worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  host TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  last_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
  env_enabled BOOLEAN NOT NULL DEFAULT false,
  codex_available BOOLEAN NOT NULL DEFAULT false,
  ssh_runner_enabled BOOLEAN NOT NULL DEFAULT false,
  note TEXT NOT NULL DEFAULT ''
);

-- @index-guard idx_ai_ops_heartbeats_seen ON ai_ops_worker_heartbeats plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_heartbeats_seen' AND i.indrelid = to_regclass('public.ai_ops_worker_heartbeats')) THEN
        NULL;  -- 已在 public.ai_ops_worker_heartbeats 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_heartbeats_seen' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_heartbeats_seen 已存在但不在 public.ai_ops_worker_heartbeats 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_heartbeats_seen' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_heartbeats_seen ON public.ai_ops_worker_heartbeats (last_seen_at DESC);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 8. ai_ops_alerts + ai_ops_patrol_runs · 主动巡逻(包B · 2026-07-03)
-- ---------------------------------------------------------------------------
-- 巡逻引擎(services/ai_ops/patrol.py)每 5 分钟评估一遍库内已有真实信号
-- (Runner 心跳 / 失败任务 / 审批积压 / 队列堆积 / 日报缺失),异常写告警。
-- 去重:同一 (rule_key, fingerprint) 只保持一条 firing(部分唯一索引),
-- 恢复后置 resolved;再次异常开新行(保留历史)。
-- 巡逻本身只读评估+写本表,不碰业务表;自动立案受 kill switch + 总开关双闸。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_ops_alerts (
  id SERIAL PRIMARY KEY,
  rule_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'warn' CHECK (severity IN ('info', 'warn', 'critical')),
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'firing' CHECK (status IN ('firing', 'resolved')),
  task_id INTEGER NULL REFERENCES ai_ops_tasks(id) ON DELETE SET NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMP NULL,
  resolved_by INTEGER NULL
);

-- @index-guard uq_ai_ops_alerts_firing ON ai_ops_alerts unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_ai_ops_alerts_firing' AND i.indrelid = to_regclass('public.ai_ops_alerts')) THEN
        NULL;  -- 已在 public.ai_ops_alerts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_ai_ops_alerts_firing' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_ai_ops_alerts_firing 已存在但不在 public.ai_ops_alerts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_ai_ops_alerts_firing' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_ai_ops_alerts_firing ON public.ai_ops_alerts (rule_key, fingerprint) WHERE status = 'firing';
    END IF;
END $idxguard$;
-- @index-guard idx_ai_ops_alerts_status ON ai_ops_alerts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_alerts_status' AND i.indrelid = to_regclass('public.ai_ops_alerts')) THEN
        NULL;  -- 已在 public.ai_ops_alerts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_alerts_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_alerts_status 已存在但不在 public.ai_ops_alerts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_alerts_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_alerts_status ON public.ai_ops_alerts (status, last_seen_at DESC);
    END IF;
END $idxguard$;

-- 巡逻运行记录(保安打卡:最后巡逻时间/发现数;patrol.py 只保留最近 500 条)
CREATE TABLE IF NOT EXISTS ai_ops_patrol_runs (
  id SERIAL PRIMARY KEY,
  ran_at TIMESTAMP NOT NULL DEFAULT NOW(),
  firing_count INTEGER NOT NULL DEFAULT 0,
  opened_count INTEGER NOT NULL DEFAULT 0,
  resolved_count INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);

-- @index-guard idx_ai_ops_patrol_runs_at ON ai_ops_patrol_runs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_ops_patrol_runs_at' AND i.indrelid = to_regclass('public.ai_ops_patrol_runs')) THEN
        NULL;  -- 已在 public.ai_ops_patrol_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_ops_patrol_runs_at' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_ops_patrol_runs_at 已存在但不在 public.ai_ops_patrol_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_ops_patrol_runs_at' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_ops_patrol_runs_at ON public.ai_ops_patrol_runs (ran_at DESC);
    END IF;
END $idxguard$;

-- 巡逻开关(默认关 · 设置页可点;开启后 scheduler 每 5 分钟跑一轮)
INSERT INTO ai_ops_policies (key, value_jsonb) VALUES
  ('ai_ops.patrol.enabled', '{"enabled": false}'::jsonb)
ON CONFLICT (key) DO NOTHING;
