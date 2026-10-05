-- ============================================================================
-- 营销中心(营销军师 + 物料工厂) 迁移 · 2026-07-04
-- 发单:定价/营销策略 CTO · 收单:开发 CTO
-- ----------------------------------------------------------------------------
-- 包 A · 数据与安全地基。新增营销域表 + 策略/Kill Switch + 只读视图白名单 +
-- 物料工厂计费 feature_pricing 种子(数据 INSERT · 不改 db/wallet_db.py 红线)。
--
-- 落地约定(与 server.py:_run_sql_migrations 一致 · 照抄 AI Ops 迁移范式):
--   - 本文件由 _run_sql_migrations() 在 autocommit 下"整体 execute"。
--   - 运行器会剥离外层 BEGIN/COMMIT 与 psql \ 指令 → 本文件不写事务包裹、不写 \ 指令。
--   - 全部 CREATE TABLE / INDEX IF NOT EXISTS,幂等、可重复跑、向后兼容。
--   - 整段一次 execute:某语句失败会中止其后语句。因此把"建表 → flag → 定价种子"
--     放前面(必须落地),把"视图"放最后并逐个包 DO 块 fail-soft(见文件尾),
--     这样即便某底表尚未建好(全新库载入顺序问题)也不会拖垮前面的建表。
--
-- 表隔离(总设计 §9.2):复用 AI Ops 架构模式,但**不复用其业务表**。
--   跨域引用(users/brands/recharge_orders)一律软整数列(不加硬 FK),归属校验在应用层。
--   域内引用(attempts→jobs 等)用硬 FK(同文件按序建表,安全)。
--
-- 红线(工程 §3):本迁移只新增营销域对象 + feature_pricing 数据行,
--   零改 middleware/billing.py / db/wallet_db.py / db/connection.py / auth/*。
--   "积分"字样全站零出现(对客称"算力");视图内绝不暴露 手机号明文/密码/支付凭据/成本明细。
-- ============================================================================


-- ############################################################################
-- 一、军师域 7 表(建议案件 / 审批 / 事件流 / 活动 / 发放台账 / 触达记录 / 回测)
-- ############################################################################

-- ---------------------------------------------------------------------------
-- 1. marketing_cases · 建议案件(今日军情队列 · 10 字段强 schema + expires_at 过期自动作废)
--    idempotency:case_key UNIQUE = `mkt:{rule_key}:{fingerprint}:{date}`(AI Ops 同款日期域幂等)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_cases (
  id BIGSERIAL PRIMARY KEY,
  case_key TEXT UNIQUE,                 -- 幂等键:同一 规则:目标指纹:日期 只一案
  case_no TEXT NOT NULL DEFAULT '',     -- 展示号 MKT-YYYYMMDD-NNNNN(应用层回填)
  rule_key TEXT NOT NULL DEFAULT '',    -- 命中的信号规则
  fingerprint TEXT NOT NULL DEFAULT '', -- 目标指纹(user_id / brand_id / 'aggregate')
  awareness_stage TEXT NOT NULL DEFAULT 'unknown', -- 施瓦茨觉醒 5 阶段(策划归因)
  owner_scope TEXT NOT NULL DEFAULT 'platform'
    CHECK (owner_scope IN ('platform','user')),
  -- —— 10 字段强 schema(总设计 §9.4;pydantic 在应用层强校验,自由文本直接失败)——
  trigger_reason TEXT NOT NULL DEFAULT '',          -- ① 触发原因
  evidence_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,-- ② 证据数据(查询+快照)
  audience_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,-- ③ 目标人群定义+人数
  expected_impact TEXT NOT NULL DEFAULT '',         -- ④ 预计影响(相关转化口径 · 无承诺)
  budget_cost_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb, -- ⑤ 预算:算力+真实成本估
  risk_level TEXT NOT NULL DEFAULT 'low'
    CHECK (risk_level IN ('low','med','high')),      -- ⑥ 风险等级
  touch_copy_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb, -- ⑦ 触达文案成品(分渠道)
  execution_plan_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb, -- ⑧ 执行计划
  rollback_plan TEXT NOT NULL DEFAULT '',           -- ⑨ 回滚方案(触达不可撤回须显式标注)
  observation_window_days INTEGER NOT NULL DEFAULT 7, -- ⑩ 观察窗口 N 天
  -- —— 框架标签(度量学习 §F.2:复盘按技能包标签归因相关转化)——
  skill_packs_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- —— 状态机 ——
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('draft','pending','approved','rejected','changes_requested','executed','expired','cancelled')),
  llm_model TEXT NOT NULL DEFAULT '',   -- 撰写建议所用模型(可观测)
  created_by INTEGER NULL,              -- NULL=Agent 自动巡逻产出
  approved_by INTEGER NULL,
  approved_at TIMESTAMP NULL,
  executed_at TIMESTAMP NULL,
  expires_at TIMESTAMP NULL,            -- 过期未批自动作废(防旧方案误执行)
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_marketing_cases_status ON marketing_cases plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_cases_status' AND i.indrelid = to_regclass('public.marketing_cases')) THEN
        NULL;  -- 已在 public.marketing_cases 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_cases_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_cases_status 已存在但不在 public.marketing_cases 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_cases_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_cases_status ON public.marketing_cases (status, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_marketing_cases_rule_fp ON marketing_cases plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_cases_rule_fp' AND i.indrelid = to_regclass('public.marketing_cases')) THEN
        NULL;  -- 已在 public.marketing_cases 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_cases_rule_fp' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_cases_rule_fp 已存在但不在 public.marketing_cases 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_cases_rule_fp' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_cases_rule_fp ON public.marketing_cases (rule_key, fingerprint, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_marketing_cases_expires ON marketing_cases plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_cases_expires' AND i.indrelid = to_regclass('public.marketing_cases')) THEN
        NULL;  -- 已在 public.marketing_cases 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_cases_expires' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_cases_expires 已存在但不在 public.marketing_cases 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_cases_expires' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_cases_expires ON public.marketing_cases (expires_at) WHERE status = 'pending';
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 2. marketing_approvals · 审批记录(批准/驳回/要求修改 + 五绿勾真校验快照)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_approvals (
  id BIGSERIAL PRIMARY KEY,
  case_id BIGINT NOT NULL REFERENCES marketing_cases(id) ON DELETE CASCADE,
  decision TEXT NOT NULL
    CHECK (decision IN ('approve','reject','request_changes')),
  -- 五绿勾后端真校验器结果(禁承诺/不改余额/频控预检/预算校验/回滚非空)· 装饰≠校验
  five_checks_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  checks_passed BOOLEAN NOT NULL DEFAULT FALSE,
  approver_id INTEGER NULL,
  note TEXT NOT NULL DEFAULT '',
  executed_at TIMESTAMP NULL,           -- 执行器执行打点(幂等 · 防重复执行)
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_marketing_approvals_case ON marketing_approvals plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_approvals_case' AND i.indrelid = to_regclass('public.marketing_approvals')) THEN
        NULL;  -- 已在 public.marketing_approvals 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_approvals_case' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_approvals_case 已存在但不在 public.marketing_approvals 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_approvals_case' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_approvals_case ON public.marketing_approvals (case_id, created_at DESC);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 3. marketing_events · 事件流 + 全量审计(近期营销动态时间线 + 谁批/改了什么/预算多少)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_events (
  id BIGSERIAL PRIMARY KEY,
  case_id BIGINT NULL REFERENCES marketing_cases(id) ON DELETE CASCADE,
  campaign_id BIGINT NULL,             -- 软链 marketing_campaigns(避免建表序耦合)
  event_type TEXT NOT NULL,            -- suggestion_generated / approved / executed / dry_run_executed / touch_sent / grant_applied / grant_reversed / ...
  severity TEXT NOT NULL DEFAULT 'info'
    CHECK (severity IN ('info','warn','error','security')),
  actor_id INTEGER NULL,               -- 操作人(NULL=系统/Agent)
  message TEXT NOT NULL DEFAULT '',
  payload_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_marketing_events_created ON marketing_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_events_created' AND i.indrelid = to_regclass('public.marketing_events')) THEN
        NULL;  -- 已在 public.marketing_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_events_created' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_events_created 已存在但不在 public.marketing_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_events_created' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_events_created ON public.marketing_events (created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_marketing_events_case ON marketing_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_events_case' AND i.indrelid = to_regclass('public.marketing_events')) THEN
        NULL;  -- 已在 public.marketing_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_events_case' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_events_case 已存在但不在 public.marketing_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_events_case' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_events_case ON public.marketing_events (case_id, created_at DESC) WHERE case_id IS NOT NULL;
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 4. marketing_campaigns · 营销活动(budget_cap_points NOT NULL · 首充双倍/里程碑/临时)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_campaigns (
  id BIGSERIAL PRIMARY KEY,
  case_id BIGINT NULL REFERENCES marketing_cases(id) ON DELETE SET NULL,
  campaign_code TEXT UNIQUE,           -- 内置模板 firstcharge_double / milestone_growth 幂等键
  campaign_type TEXT NOT NULL DEFAULT 'adhoc'
    CHECK (campaign_type IN ('first_charge_double','milestone','adhoc')),
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','active','paused','ended')),
  budget_cap_points BIGINT NOT NULL,   -- 预算上限(算力)· NOT NULL 强约束
  spent_points BIGINT NOT NULL DEFAULT 0,
  params_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb, -- 活动参数(bonus 比例/门槛/里程碑档等)
  is_resident BOOLEAN NOT NULL DEFAULT FALSE,      -- 常驻(首充双倍无相对时间窗)
  dry_run BOOLEAN NOT NULL DEFAULT FALSE,          -- 彩排:只写模拟事件不动真实活动
  starts_at TIMESTAMP NULL,
  ends_at TIMESTAMP NULL,
  created_by INTEGER NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_marketing_campaigns_status ON marketing_campaigns plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_campaigns_status' AND i.indrelid = to_regclass('public.marketing_campaigns')) THEN
        NULL;  -- 已在 public.marketing_campaigns 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_campaigns_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_campaigns_status 已存在但不在 public.marketing_campaigns 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_campaigns_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_campaigns_status ON public.marketing_campaigns (status, campaign_type, created_at DESC);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 5. marketing_grants · 赠送算力发放台账(pool CHECK 锁 bonus · 语义幂等键 UNIQUE)
--    抄 services/channel_tier.grant_tier_bonus 活范式:台账先落(幂等闸)→ 再入账
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_grants (
  id BIGSERIAL PRIMARY KEY,
  grant_key TEXT NOT NULL UNIQUE,      -- 语义幂等键 `mktg_firstcharge:{user_id}` 等(不带版本号)
  campaign_id BIGINT NULL REFERENCES marketing_campaigns(id) ON DELETE SET NULL,
  user_id INTEGER NOT NULL,            -- 软链 users.id
  points BIGINT NOT NULL DEFAULT 0,
  pool TEXT NOT NULL DEFAULT 'bonus'
    CHECK (pool = 'bonus'),            -- 锁死只入 bonus_points(Agent 永不碰 paid/commission 面值)
  grant_type TEXT NOT NULL DEFAULT 'marketing',
  related_order_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'applied'
    CHECK (status IN ('applied','reversed','dry_run')),
  dry_run BOOLEAN NOT NULL DEFAULT FALSE, -- flag 未点亮时的模拟发放(彩排对账)
  reversed_of BIGINT NULL,             -- 冲销指向的原 grant(负向冲销)
  metadata_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  applied_at TIMESTAMP NULL,
  reversed_at TIMESTAMP NULL
);

-- @index-guard idx_marketing_grants_user ON marketing_grants plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_grants_user' AND i.indrelid = to_regclass('public.marketing_grants')) THEN
        NULL;  -- 已在 public.marketing_grants 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_grants_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_grants_user 已存在但不在 public.marketing_grants 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_grants_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_grants_user ON public.marketing_grants (user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_marketing_grants_campaign ON marketing_grants plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_grants_campaign' AND i.indrelid = to_regclass('public.marketing_grants')) THEN
        NULL;  -- 已在 public.marketing_grants 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_grants_campaign' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_grants_campaign 已存在但不在 public.marketing_grants 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_grants_campaign' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_grants_campaign ON public.marketing_grants (campaign_id, created_at DESC);
    END IF;
END $idxguard$;
-- 单日/单批发放量对账(每小时补发 job + 三级预算上限告警用)
-- @index-guard idx_marketing_grants_applied_at ON marketing_grants plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_grants_applied_at' AND i.indrelid = to_regclass('public.marketing_grants')) THEN
        NULL;  -- 已在 public.marketing_grants 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_grants_applied_at' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_grants_applied_at 已存在但不在 public.marketing_grants 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_grants_applied_at' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_grants_applied_at ON public.marketing_grants (applied_at) WHERE status = 'applied';
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 6. marketing_touch_events · 触达发送记录(= 频控的数据源)
--    频控五条:7天≤1 / 每日全局上限 / 夜间勿扰 / 退订DND / 服务商与终端分开计数
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_touch_events (
  id BIGSERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL,            -- 软链 users.id
  channel TEXT NOT NULL DEFAULT 'station'
    CHECK (channel IN ('station','wecom','sms','wecom_service')),
  audience_segment TEXT NOT NULL DEFAULT 'end'
    CHECK (audience_segment IN ('end','provider')), -- 终端 / 服务商(分开计数分开文案池)
  case_id BIGINT NULL REFERENCES marketing_cases(id) ON DELETE SET NULL,
  campaign_id BIGINT NULL REFERENCES marketing_campaigns(id) ON DELETE SET NULL,
  content_ref TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'sent'
    CHECK (status IN ('sent','failed','suppressed','dry_run')),
  suppressed_reason TEXT NOT NULL DEFAULT '', -- freq_7d / daily_cap / night_dnd / opted_out / dnd
  dry_run BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- 频控核心索引:7 天内该用户是否已被触达(status='sent')
-- @index-guard idx_marketing_touch_user_time ON marketing_touch_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_touch_user_time' AND i.indrelid = to_regclass('public.marketing_touch_events')) THEN
        NULL;  -- 已在 public.marketing_touch_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_touch_user_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_touch_user_time 已存在但不在 public.marketing_touch_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_touch_user_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_touch_user_time ON public.marketing_touch_events (user_id, created_at DESC) WHERE status = 'sent';
    END IF;
END $idxguard$;
-- @index-guard idx_marketing_touch_created ON marketing_touch_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_touch_created' AND i.indrelid = to_regclass('public.marketing_touch_events')) THEN
        NULL;  -- 已在 public.marketing_touch_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_touch_created' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_touch_created 已存在但不在 public.marketing_touch_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_touch_created' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_touch_created ON public.marketing_touch_events (created_at DESC);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 7. marketing_measurements · 观察窗口回测(相关转化三指标 · 无对照组禁因果表述)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_measurements (
  id BIGSERIAL PRIMARY KEY,
  case_id BIGINT NULL REFERENCES marketing_cases(id) ON DELETE CASCADE,
  campaign_id BIGINT NULL REFERENCES marketing_campaigns(id) ON DELETE SET NULL,
  user_id INTEGER NULL,               -- 软链 users.id(NULL=聚合级)
  metric_type TEXT NOT NULL
    CHECK (metric_type IN ('recharge','revisit','feature_reuse')),
  window_days INTEGER NOT NULL DEFAULT 7,
  window_start TIMESTAMP NULL,
  window_end TIMESTAMP NULL,
  converted BOOLEAN NOT NULL DEFAULT FALSE, -- 触达后 N 天内是否发生(相关,不宣称因果)
  result_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_control BOOLEAN NOT NULL DEFAULT FALSE, -- 对照组标记(样本量达标前不点亮 UI)
  measured_at TIMESTAMP NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_marketing_measurements_case ON marketing_measurements plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_measurements_case' AND i.indrelid = to_regclass('public.marketing_measurements')) THEN
        NULL;  -- 已在 public.marketing_measurements 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_measurements_case' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_measurements_case 已存在但不在 public.marketing_measurements 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_measurements_case' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_measurements_case ON public.marketing_measurements (case_id, metric_type);
    END IF;
END $idxguard$;
-- 幂等:同一 case × user × metric × window 只测一次
-- @index-guard uq_marketing_measurements_unit ON marketing_measurements unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_marketing_measurements_unit' AND i.indrelid = to_regclass('public.marketing_measurements')) THEN
        NULL;  -- 已在 public.marketing_measurements 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_marketing_measurements_unit' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_marketing_measurements_unit 已存在但不在 public.marketing_measurements 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_marketing_measurements_unit' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_marketing_measurements_unit ON public.marketing_measurements (case_id, COALESCE(user_id, -1), metric_type, window_days);
    END IF;
END $idxguard$;


-- ############################################################################
-- 二、物料域 4 表(模板 / 生成任务 / 尝试重试 / 成品资产)
-- ############################################################################

-- ---------------------------------------------------------------------------
-- 8. marketing_material_templates · 场景化 prompt 骨架库(prompt_skeleton 由策划 CTO 交付)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_material_templates (
  id BIGSERIAL PRIMARY KEY,
  template_code TEXT NOT NULL UNIQUE,
  scene_type TEXT NOT NULL DEFAULT 'moments', -- moments/salon/opening/recharge_activity/knowledge_card/invitation/...
  name TEXT NOT NULL DEFAULT '',
  material_kind TEXT NOT NULL DEFAULT 'poster' -- copy(朋友圈文案) / poster(海报) / bundle(场景套装)
    CHECK (material_kind IN ('copy','poster','bundle')),
  prompt_skeleton TEXT NOT NULL DEFAULT '',   -- 英文骨架 + §2 固定尾缀(策划交付,先放占位跑通)
  default_size TEXT NOT NULL DEFAULT '3:4',
  default_resolution TEXT NOT NULL DEFAULT '1k',
  bundle_spec_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb, -- 套装多尺寸组(朋友圈图+易拉宝+邀请函)
  style_tags_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb,
  feature_code TEXT NOT NULL DEFAULT '',      -- 计费编码(→ feature_pricing)
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  sort INTEGER NOT NULL DEFAULT 100,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_marketing_templates_active ON marketing_material_templates plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_templates_active' AND i.indrelid = to_regclass('public.marketing_material_templates')) THEN
        NULL;  -- 已在 public.marketing_material_templates 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_templates_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_templates_active 已存在但不在 public.marketing_material_templates 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_templates_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_templates_active ON public.marketing_material_templates (is_active, scene_type, sort);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 9. marketing_material_jobs · 生成任务(计费锚点 · freeze 三态)
--    status: pending/generating/succeeded/failed/blocked(blocked=生成前守卫拦截·未扣费)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_material_jobs (
  id BIGSERIAL PRIMARY KEY,
  owner_scope TEXT NOT NULL DEFAULT 'user'
    CHECK (owner_scope IN ('user','platform')),
  user_id INTEGER NOT NULL,            -- 软链 users.id(freeze/commit/release 必须同一 user_id)
  brand_id INTEGER NULL,              -- 软链 brands.id
  case_id BIGINT NULL REFERENCES marketing_cases(id) ON DELETE SET NULL, -- platform 物料强制挂案件过审批
  template_id BIGINT NULL REFERENCES marketing_material_templates(id) ON DELETE SET NULL,
  material_kind TEXT NOT NULL DEFAULT 'poster'
    CHECK (material_kind IN ('copy','poster','bundle')),
  feature_code TEXT NOT NULL DEFAULT '',
  input_fields_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb, -- 标题/卖点/日期/联系方式/风格(结构化)
  final_prompt TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '3:4',
  resolution TEXT NOT NULL DEFAULT '1k',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','generating','succeeded','failed','blocked')),
  freeze_id BIGINT NULL,              -- middleware/billing.py freeze 句柄(billing 红线只调不改)
  billing_ref TEXT NOT NULL DEFAULT '', -- task_ref = `mktg_factory_{job_id}`(幂等)
  cost_points INTEGER NOT NULL DEFAULT 0,
  block_reason TEXT NOT NULL DEFAULT '', -- 生成前守卫拦截原因(违禁词/承诺词/广告法/竞品)
  error_summary TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMP NULL
);

-- @index-guard idx_marketing_jobs_user_time ON marketing_material_jobs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_jobs_user_time' AND i.indrelid = to_regclass('public.marketing_material_jobs')) THEN
        NULL;  -- 已在 public.marketing_material_jobs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_jobs_user_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_jobs_user_time 已存在但不在 public.marketing_material_jobs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_jobs_user_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_jobs_user_time ON public.marketing_material_jobs (user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_marketing_jobs_status ON marketing_material_jobs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_jobs_status' AND i.indrelid = to_regclass('public.marketing_material_jobs')) THEN
        NULL;  -- 已在 public.marketing_material_jobs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_jobs_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_jobs_status 已存在但不在 public.marketing_material_jobs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_jobs_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_jobs_status ON public.marketing_material_jobs (status, created_at DESC);
    END IF;
END $idxguard$;
-- 日上限实时 COUNT(生产 WORKERS=1 无并发竞态,不专门立计数表)
-- @index-guard idx_marketing_jobs_user_day ON marketing_material_jobs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_jobs_user_day' AND i.indrelid = to_regclass('public.marketing_material_jobs')) THEN
        NULL;  -- 已在 public.marketing_material_jobs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_jobs_user_day' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_jobs_user_day 已存在但不在 public.marketing_material_jobs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_jobs_user_day' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_jobs_user_day ON public.marketing_material_jobs (user_id, created_at);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 10. marketing_material_generation_attempts · 尝试/重试(1 job : N attempts · ≤3 · 含安全结果)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_material_generation_attempts (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES marketing_material_jobs(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL DEFAULT 1,
  provider TEXT NOT NULL DEFAULT 'apimart-gpt-image-2',
  provider_task_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','succeeded','failed')),
  provider_cost_usd NUMERIC(10,4) NOT NULL DEFAULT 0,
  safety_status TEXT NOT NULL DEFAULT 'passed'
    CHECK (safety_status IN ('passed','blocked_prompt','blocked_output')),
  safety_flags_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb,
  error_detail TEXT NOT NULL DEFAULT '',
  started_at TIMESTAMP NULL,
  finished_at TIMESTAMP NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_marketing_attempts_job ON marketing_material_generation_attempts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_attempts_job' AND i.indrelid = to_regclass('public.marketing_material_generation_attempts')) THEN
        NULL;  -- 已在 public.marketing_material_generation_attempts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_attempts_job' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_attempts_job 已存在但不在 public.marketing_material_generation_attempts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_attempts_job' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_attempts_job ON public.marketing_material_generation_attempts (job_id, attempt_no);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 11. marketing_material_assets · 成品资产(url 落自有存储 · 外发风控三态)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marketing_material_assets (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES marketing_material_jobs(id) ON DELETE CASCADE,
  asset_kind TEXT NOT NULL DEFAULT 'poster', -- poster/copy/bundle_item
  bundle_slot TEXT NOT NULL DEFAULT '',      -- 套装内槽位(moments/rollup/invitation)
  url_provider TEXT NOT NULL DEFAULT '',     -- apimart 临时图床(不做永久引用)
  url_stored TEXT NOT NULL DEFAULT '',       -- 我方存储永久引用(services/image_storage)
  thumbnail_url TEXT NOT NULL DEFAULT '',
  content_text TEXT NOT NULL DEFAULT '',     -- 文案类成品(material_kind=copy)
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  is_final BOOLEAN NOT NULL DEFAULT FALSE,   -- 用户预览自审后才落成品
  whitelabel_applied BOOLEAN NOT NULL DEFAULT FALSE,
  publish_allowed SMALLINT NOT NULL DEFAULT 1,  -- 外发风控三态(照 brand_image_assets)
  rights_confirmed SMALLINT NOT NULL DEFAULT 0,
  risk_flags_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_marketing_assets_job ON marketing_material_assets plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_assets_job' AND i.indrelid = to_regclass('public.marketing_material_assets')) THEN
        NULL;  -- 已在 public.marketing_material_assets 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_assets_job' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_assets_job 已存在但不在 public.marketing_material_assets 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_assets_job' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_assets_job ON public.marketing_material_assets (job_id, created_at DESC);
    END IF;
END $idxguard$;


-- ############################################################################
-- 三、技能包表(prompt 装配按 weight 热加载,改包不发版)
-- ############################################################################
CREATE TABLE IF NOT EXISTS marketing_skill_packs (
  id BIGSERIAL PRIMARY KEY,
  pack_code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',            -- 大师方法论中文蒸馏包(核心框架+检查清单+禁忌+示例)
  bind_signals_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb, -- 绑定哪些信号规则(按 bind_signals 匹配注入)
  weight INTEGER NOT NULL DEFAULT 100,         -- 热加载权重/排序
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- @index-guard idx_marketing_skill_packs_active ON marketing_skill_packs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_skill_packs_active' AND i.indrelid = to_regclass('public.marketing_skill_packs')) THEN
        NULL;  -- 已在 public.marketing_skill_packs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_skill_packs_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_skill_packs_active 已存在但不在 public.marketing_skill_packs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_skill_packs_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_skill_packs_active ON public.marketing_skill_packs (is_active, weight DESC);
    END IF;
END $idxguard$;


-- ############################################################################
-- 四、操作/合规支撑表(策略Kill Switch · 巡逻打卡 · 营销消息退订)
-- ############################################################################

-- 策略与 Kill Switch(key/value · 默认全 false · 复用 AI Ops 同款 {"enabled":bool} 形状)
CREATE TABLE IF NOT EXISTS marketing_policies (
  key TEXT PRIMARY KEY,
  value_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_by INTEGER NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- 五个 Kill Switch(总设计 §9.6 · 工程 §1.A.5)· 全部默认关(点亮=运营动作不是开发动作)
INSERT INTO marketing_policies (key, value_jsonb) VALUES
  ('marketing_agent.enabled',              '{"enabled": false}'::jsonb),  -- 总闸(含感知巡逻+建议)
  ('marketing_agent.execute.enabled',      '{"enabled": false}'::jsonb),  -- 一切执行
  ('marketing_agent.grant.enabled',        '{"enabled": false}'::jsonb),  -- 发放算力
  ('marketing_agent.notification.enabled', '{"enabled": false}'::jsonb),  -- 触达
  ('marketing_agent.kill_switch',          '{"enabled": false}'::jsonb),  -- 最高优先级瞬时全停
  ('marketing_agent.control_group.enabled','{"enabled": false}'::jsonb),  -- 对照组模块(样本量达标点亮)
  ('marketing_agent.advisor_llm.enabled',  '{"enabled": false}'::jsonb)   -- 建议撰写 LLM(默认关·先规则草稿)
ON CONFLICT (key) DO NOTHING;

-- 数值配置(触达全局上限/夜间勿扰/三级预算上限/单笔硬顶/日生成上限)· value_jsonb.value 存数
INSERT INTO marketing_policies (key, value_jsonb) VALUES
  ('marketing.touch.daily_global_cap',   '{"value": 500}'::jsonb),   -- 每日全局触达总上限
  ('marketing.touch.night_dnd_start',    '{"value": 21}'::jsonb),    -- 夜间勿扰起(21:00)
  ('marketing.touch.night_dnd_end',      '{"value": 9}'::jsonb),     -- 夜间勿扰止(09:00)
  ('marketing.touch.freq_days',          '{"value": 7}'::jsonb),     -- 同一用户 N 天≤1 次
  -- [返工 R6-2] 上限与内置活动自洽:首充双倍封顶 65000(¥500 档)/ 里程碑档二 39000 都必须发得出
  ('marketing.grant.cap_per_user',       '{"value": 120000}'::jsonb),-- 三级上限:单人(累计:首充65000+里程碑45500+余量)
  ('marketing.grant.cap_per_batch',      '{"value": 200000}'::jsonb),-- 三级上限:单批
  ('marketing.grant.cap_per_day',        '{"value": 300000}'::jsonb),-- 三级上限:单日
  ('marketing.grant.hard_ceiling',       '{"value": 100000}'::jsonb),-- 单笔发放硬天花板
  ('marketing.material.daily_limit',     '{"value": 20}'::jsonb)     -- 单用户日生成上限
ON CONFLICT (key) DO NOTHING;

-- 巡逻运行记录(保安打卡:最后巡逻时间/命中数;patrol 只保留最近 500 条)
CREATE TABLE IF NOT EXISTS marketing_patrol_runs (
  id BIGSERIAL PRIMARY KEY,
  ran_at TIMESTAMP NOT NULL DEFAULT NOW(),
  signals_matched INTEGER NOT NULL DEFAULT 0,
  cases_opened INTEGER NOT NULL DEFAULT 0,
  cases_suppressed INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);

-- @index-guard idx_marketing_patrol_runs_at ON marketing_patrol_runs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_marketing_patrol_runs_at' AND i.indrelid = to_regclass('public.marketing_patrol_runs')) THEN
        NULL;  -- 已在 public.marketing_patrol_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_marketing_patrol_runs_at' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_marketing_patrol_runs_at 已存在但不在 public.marketing_patrol_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_marketing_patrol_runs_at' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_marketing_patrol_runs_at ON public.marketing_patrol_runs (ran_at DESC);
    END IF;
END $idxguard$;

-- 营销消息退订/免打扰名单(§9.3 ④ · 查询层强制过滤;不 ALTER users 红线表)
CREATE TABLE IF NOT EXISTS marketing_optouts (
  user_id INTEGER PRIMARY KEY,         -- 软链 users.id
  opted_out BOOLEAN NOT NULL DEFAULT TRUE,
  source TEXT NOT NULL DEFAULT 'user', -- user(自主退订) / admin
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);


-- ############################################################################
-- 五、物料工厂计费种子(feature_pricing 数据行 · 不改 db/wallet_db.py 红线)
--    朋友圈文案 40 / 海报 260-390 / 场景套装 650-1040 算力(dev doc §D.3)
--    requires_paid_points=FALSE(AI 生成类,bonus/commission/paid 皆可扣)
--    [返工 R2] DO NOTHING:价目表(feature_pricing 表)= SSOT 元指令,运营调价/下架不被重启还原;
--    首发插入仍生效(grep 确认 mktg_* 编码全新无冲突)。
-- ############################################################################
INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES
  ('mktg_moments_copy', '营销·朋友圈文案',   40,   0.31, FALSE),
  ('mktg_poster_basic', '营销·海报(标准)', 260,  2.00, FALSE),
  ('mktg_poster_pro',   '营销·海报(高清)', 390,  3.00, FALSE),
  ('mktg_bundle_std',   '营销·场景套装(标准)', 650,  5.00, FALSE),
  ('mktg_bundle_pro',   '营销·场景套装(全套)', 1040, 8.00, FALSE)
ON CONFLICT (feature_code) DO NOTHING;


-- ############################################################################
-- 六、marketing_v_* 只读视图白名单(视图清单即白名单本体 · 视图访问即审计)
--    marketing_reader 角色只 SELECT 这些视图,绝不碰底表(手工 GRANT · 见 provision 文档)。
--    视图内已做:手机号掩码 LEFT(3)+****+RIGHT(4) / 排除密码支付凭据成本明细。
--    fail-soft:逐个 DO 块包 + 美元引号,底表缺失/列缺失时 RAISE NOTICE 跳过,不拖垮前面的建表。
-- ############################################################################

DO $do$ BEGIN
  EXECUTE $v$
    CREATE OR REPLACE VIEW marketing_v_funnel AS
    WITH reg AS (SELECT COUNT(*)::bigint AS c FROM users),
         act AS (SELECT COUNT(DISTINCT owner_user_id)::bigint AS c
                 FROM brands WHERE diagnosis_count > 0 AND owner_user_id IS NOT NULL),
         spend AS (SELECT COUNT(DISTINCT user_id)::bigint AS c
                   FROM point_transactions WHERE type = 'consume'),
         charge AS (SELECT COUNT(DISTINCT user_id)::bigint AS c
                    FROM recharge_orders WHERE payment_status = 'paid'),
         repur AS (SELECT COUNT(*)::bigint AS c FROM (
                     SELECT user_id FROM recharge_orders WHERE payment_status = 'paid'
                     GROUP BY user_id HAVING COUNT(*) >= 2) t)
    SELECT reg.c AS registered, act.c AS activated, spend.c AS first_spend,
           charge.c AS first_charge, repur.c AS repurchase,
           ROUND(100.0 * act.c    / NULLIF(reg.c,0),    2) AS activated_rate_pct,
           ROUND(100.0 * spend.c  / NULLIF(act.c,0),    2) AS spend_rate_pct,
           ROUND(100.0 * charge.c / NULLIF(reg.c,0),    2) AS charge_rate_pct,
           ROUND(100.0 * repur.c  / NULLIF(charge.c,0), 2) AS repurchase_rate_pct
    FROM reg, act, spend, charge, repur
  $v$;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function THEN
  RAISE NOTICE 'marketing_v_funnel skipped: %', SQLERRM;
END $do$;

DO $do$ BEGIN
  EXECUTE $v$
    CREATE OR REPLACE VIEW marketing_v_pending_orders AS
    SELECT ro.id AS order_id, ro.user_id, u.username,
           CASE WHEN u.phone IS NOT NULL AND length(u.phone) >= 11
                THEN LEFT(u.phone,3) || '****' || RIGHT(u.phone,4)
                ELSE NULL END AS phone_masked,  -- [返工 R6-7] <11 位不掩全泄,置 NULL
           ro.amount_cents,
           ROUND(ro.amount_cents / 100.0, 2) AS amount_yuan,
           ro.payment_method, ro.payment_status, ro.settlement_mode,
           ro.agent_user_id, ro.created_at,
           EXTRACT(EPOCH FROM (NOW() - ro.created_at))::bigint AS age_seconds
    FROM recharge_orders ro
    LEFT JOIN users u ON u.id = ro.user_id
    WHERE ro.payment_status = 'pending'
  $v$;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function THEN
  RAISE NOTICE 'marketing_v_pending_orders skipped: %', SQLERRM;
END $do$;

DO $do$ BEGIN
  EXECUTE $v$
    CREATE OR REPLACE VIEW marketing_v_wallet_dist AS
    SELECT CASE
             WHEN (paid_points + bonus_points) = 0        THEN '0'
             WHEN (paid_points + bonus_points) < 1000     THEN '1-999'
             WHEN (paid_points + bonus_points) < 5000     THEN '1000-4999'
             WHEN (paid_points + bonus_points) < 20000    THEN '5000-19999'
             ELSE '20000+'
           END AS balance_bucket,
           COUNT(*)::bigint          AS wallet_count,
           SUM(paid_points)::bigint  AS sum_paid_points,
           SUM(bonus_points)::bigint AS sum_bonus_points,
           COUNT(*) FILTER (WHERE total_recharged = 0
                              AND (paid_points + bonus_points) = 0)::bigint
                                     AS exhausted_trial_cohort
    FROM user_wallets
    GROUP BY 1
  $v$;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function THEN
  RAISE NOTICE 'marketing_v_wallet_dist skipped: %', SQLERRM;
END $do$;

DO $do$ BEGIN
  EXECUTE $v$
    CREATE OR REPLACE VIEW marketing_v_feature_usage AS
    SELECT pt.feature_code, fp.feature_name,
           COUNT(*)::bigint                    AS usage_count,
           COUNT(DISTINCT pt.user_id)::bigint  AS distinct_users,
           SUM(ABS(pt.amount))::bigint         AS points_spent
    FROM point_transactions pt
    LEFT JOIN feature_pricing fp ON fp.feature_code = pt.feature_code
    WHERE pt.type = 'consume' AND pt.feature_code IS NOT NULL
    GROUP BY pt.feature_code, fp.feature_name
  $v$;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function THEN
  RAISE NOTICE 'marketing_v_feature_usage skipped: %', SQLERRM;
END $do$;

DO $do$ BEGIN
  EXECUTE $v$
    CREATE OR REPLACE VIEW marketing_v_effect AS
    SELECT check_date, quote_id,
           COUNT(*)::bigint                              AS keywords_checked,
           COUNT(*) FILTER (WHERE is_compliant)::bigint  AS keywords_compliant,
           ROUND(AVG(detection_rate), 1)                 AS avg_detection_rate,
           ROUND(AVG(effective_rate), 1)                 AS avg_effective_rate,
           ROUND(100.0 * COUNT(*) FILTER (WHERE is_compliant)
                 / NULLIF(COUNT(*),0), 2)                AS compliant_rate_pct
    FROM keyword_compliance_log
    GROUP BY check_date, quote_id
  $v$;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function THEN
  RAISE NOTICE 'marketing_v_effect skipped: %', SQLERRM;
END $do$;

DO $do$ BEGIN
  EXECUTE $v$
    CREATE OR REPLACE VIEW marketing_v_levers AS
    SELECT st.id AS sku_template_id, st.template_code, st.sku_type,
           st.default_name, st.default_subtitle, st.points_granted,
           st.suggested_retail_cents,
           ROUND(st.suggested_retail_cents / 100.0, 2) AS suggested_retail_yuan,
           st.is_active, st.created_at
    FROM sku_templates st
    WHERE st.is_active = TRUE
  $v$;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function THEN
  RAISE NOTICE 'marketing_v_levers skipped: %', SQLERRM;
END $do$;

DO $do$ BEGIN
  EXECUTE $v$
    CREATE OR REPLACE VIEW marketing_v_brands_health AS
    SELECT CASE
             WHEN diagnosis_count = 0 THEN 'new_no_diagnosis'
             WHEN diagnosis_count = 1 THEN 'diagnosed_once'
             ELSE 'active_multi_diagnosis'
           END AS lifecycle_stage,
           COUNT(*)::bigint AS brand_count,
           COUNT(*) FILTER (WHERE updated_at < NOW() - INTERVAL '30 days')::bigint AS idle_30d,
           ROUND(AVG(latest_score), 1) AS avg_latest_score
    FROM brands
    WHERE COALESCE(is_test, FALSE) = FALSE AND deleted_at IS NULL
    GROUP BY 1
  $v$;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function THEN
  RAISE NOTICE 'marketing_v_brands_health skipped: %', SQLERRM;
END $do$;

-- 营销自身台账聚合视图(采纳率 / 回测结果 · 军师学习用 · 全在营销域自有表,不依赖底表)
CREATE OR REPLACE VIEW marketing_v_self_ledger AS
SELECT
  (SELECT COUNT(*) FROM marketing_cases)                                        AS cases_total,
  (SELECT COUNT(*) FROM marketing_cases WHERE status='pending')                 AS cases_pending,
  (SELECT COUNT(*) FROM marketing_cases WHERE status='approved')                AS cases_approved,
  (SELECT COUNT(*) FROM marketing_cases WHERE status='executed')                AS cases_executed,
  (SELECT COUNT(*) FROM marketing_cases WHERE status='rejected')                AS cases_rejected,
  (SELECT COALESCE(SUM(points),0) FROM marketing_grants WHERE status='applied') AS grants_applied_points,
  (SELECT COUNT(*) FROM marketing_touch_events WHERE status='sent')             AS touches_sent,
  (SELECT COUNT(*) FROM marketing_measurements WHERE converted)                 AS measured_conversions;

-- [返工 R6-9] 巡逻信号的 3 处裸表定向查询收进视图:让"Agent 零裸表"成为真话
DO $do$ BEGIN
  EXECUTE $v$
    CREATE OR REPLACE VIEW marketing_v_register_no_diagnosis AS
    SELECT u.id AS user_id, u.username, u.created_at,
           EXTRACT(EPOCH FROM (NOW() - u.created_at))/86400 AS age_days
    FROM users u
    WHERE u.created_at < NOW() - INTERVAL '7 days'
      AND NOT EXISTS (SELECT 1 FROM brands b
                      WHERE b.owner_user_id = u.id AND b.diagnosis_count > 0)
  $v$;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function THEN
  RAISE NOTICE 'marketing_v_register_no_diagnosis skipped: %', SQLERRM;
END $do$;

DO $do$ BEGIN
  EXECUTE $v$
    CREATE OR REPLACE VIEW marketing_v_recharge_pulse AS
    SELECT
      (SELECT COUNT(*)::int FROM recharge_orders
        WHERE payment_status = 'paid' AND paid_at > NOW() - INTERVAL '7 days') AS paid_recharges_7d,
      (SELECT EXTRACT(EPOCH FROM (NOW() - MAX(paid_at)))::bigint FROM recharge_orders
        WHERE payment_status = 'paid') AS last_recharge_age_seconds
  $v$;
EXCEPTION WHEN undefined_table OR undefined_column OR undefined_function THEN
  RAISE NOTICE 'marketing_v_recharge_pulse skipped: %', SQLERRM;
END $do$;

-- ============================================================================
-- 迁移结束。验证(Deploy-CTO 部署后必跑):
--   SELECT feature_code, cost_points FROM feature_pricing WHERE feature_code LIKE 'mktg_%' ORDER BY 1;
--     期望 mktg_bundle_pro=1040 / mktg_bundle_std=650 / mktg_moments_copy=40 / mktg_poster_basic=260 / mktg_poster_pro=390
--   SELECT key, value_jsonb FROM marketing_policies ORDER BY 1;  -- 五闸 + 配置全 seeded
--   SELECT viewname FROM pg_views WHERE viewname LIKE 'marketing_v_%';  -- 视图白名单落地
-- ============================================================================
