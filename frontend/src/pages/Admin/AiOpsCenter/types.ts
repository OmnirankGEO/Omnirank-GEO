// AI 运维控制塔 · 前端类型 · 跟后端 api/ai_ops_api.py + db/ai_ops_db.py 对齐

export type TaskKind = 'diagnose' | 'fix' | 'report' | 'ssh_action' | 'code_review'
export type TaskStatus =
  | 'queued' | 'running' | 'waiting_approval' | 'succeeded' | 'failed' | 'cancelled'
export type RiskLevel = 'L0' | 'L1' | 'L2' | 'L3' | 'L4'
export type Priority = 'P0' | 'P1' | 'P2' | 'P3'
export type EventSeverity = 'info' | 'warn' | 'error' | 'security'
export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired' | 'executed'
export type ReportType = 'daily' | 'weekly' | 'manual'

export type AiOpsTask = {
  id: number
  task_key: string | null
  source_type: string
  source_id: string
  kind: TaskKind
  title: string
  instruction: string
  status: TaskStatus
  risk_level: RiskLevel
  priority: Priority
  feedback_id: number | null
  assigned_worker_id: string
  worktree_path: string
  summary: string
  result_jsonb: Record<string, unknown>
  source_context_jsonb: Record<string, unknown>
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
}

export type AiOpsEvent = {
  id: number
  task_id: number
  event_type: string
  severity: EventSeverity
  message: string
  payload_jsonb: Record<string, unknown>
  created_at: string
}

export type AiOpsArtifact = {
  id: number
  task_id: number
  artifact_type: string
  title: string
  storage_type: string
  content_text: string
  storage_url: string
  metadata_jsonb: Record<string, unknown>
  created_at: string
}

export type AiOpsApproval = {
  id: number
  task_id: number
  action_type: string
  risk_level: 'L3' | 'L4'
  approval_status: ApprovalStatus
  requested_by: number | null
  requested_reason: string
  command_plan_jsonb: Record<string, unknown>
  approved_by: number | null
  approved_at: string | null
  expires_at: string | null
  created_at: string
}

export type AiOpsReportListItem = {
  id: number
  report_date: string
  report_type: ReportType
  status: 'generating' | 'ready' | 'failed'
  summary: string
  action_items_jsonb: string[]
  generated_by_task_id: number | null
  created_at: string
  updated_at: string
}

export type AiOpsReport = AiOpsReportListItem & {
  markdown: string
  metrics_jsonb: Record<string, unknown>
}

export type PolicyMap = Record<string, { enabled?: boolean } & Record<string, unknown>>

// Runner(执行器)心跳状态(P1-B)· 无心跳时后端返回 null → 前端显示"未接入"
export type AiOpsRunnerStatus = {
  online: boolean
  worker_id: string
  host: string
  version: string
  last_seen_at: string
  env_enabled: boolean
  codex_available: boolean
  ssh_runner_enabled: boolean
  note: string
  age_seconds: number
}

// 巡逻告警(包B)
export type AlertSeverity = 'info' | 'warn' | 'critical'
export type AiOpsAlert = {
  id: number
  rule_key: string
  fingerprint: string
  severity: AlertSeverity
  title: string
  detail: string
  status: 'firing' | 'resolved'
  task_id: number | null
  payload: Record<string, unknown>
  first_seen_at: string
  last_seen_at: string
  resolved_at: string | null
}

export type AiOpsPatrolRun = {
  id: number
  ran_at: string
  firing_count: number
  opened_count: number
  resolved_count: number
  duration_ms: number
  age_seconds: number
}

export type AiOpsOverview = {
  health: {
    kill_switch: boolean
    ai_ops_enabled: boolean
    tasks_running: number
    tasks_failed: number
    pending_approvals: number
    open_bug_feedback: number
    runner_online: boolean
    alerts_firing?: number
  }
  task_counts: Record<TaskStatus, number>
  feedback_counts: Record<string, number>
  approval_counts: Record<ApprovalStatus, number>
  latest_report: AiOpsReport | null
  policy: PolicyMap
  // Runner 真实心跳(admin-only · 无心跳/迁移未落库时为 null)
  runner?: AiOpsRunnerStatus | null
  // 巡逻状态(包B):开关 + 最后打卡(null=还没巡逻过)+ firing 告警数
  patrol?: { enabled: boolean; last_run: AiOpsPatrolRun | null; alerts_firing: number }
  // 命令台 LLM 意图分类状态(只读展示;密钥只在服务器 .env,永不下发前端)
  chat_llm?: { enabled: boolean; model: string }
}

export const RISK_LABEL: Record<RiskLevel, string> = {
  L0: 'L0 只读诊断',
  L1: 'L1 代码修复',
  L2: 'L2 生产只读',
  L3: 'L3 低风险执行',
  L4: 'L4 高危执行',
}

export const STATUS_LABEL: Record<TaskStatus, string> = {
  queued: '排队中',
  running: '运行中',
  waiting_approval: '待审批',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
}

export const KIND_LABEL: Record<TaskKind, string> = {
  diagnose: 'AI 诊断',
  fix: 'Codex 修复',
  report: '日报',
  ssh_action: '生产动作',
  code_review: '代码审查',
}
