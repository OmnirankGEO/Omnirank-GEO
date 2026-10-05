// 营销军师控制台 · 类型 + 标签映射

export type SectionKey =
  | 'today' | 'cases' | 'campaigns' | 'levers' | 'materials' | 'effect' | 'settings'

export type CaseStatus =
  | 'draft' | 'pending' | 'approved' | 'rejected' | 'changes_requested'
  | 'executed' | 'expired' | 'cancelled'

export type RiskLevel = 'low' | 'med' | 'high'
export type OpportunityGroup = 'high_value' | 'convertible' | 'dormant' | 'offer'

export interface AudienceSpec { definition: string; count: number; segment?: string }
export interface BudgetSpec { points: number; cost_estimate_yuan: number; note?: string }

export interface MarketingCase {
  id: number
  case_no: string
  case_key: string
  rule_key: string
  fingerprint: string
  awareness_stage: string
  owner_scope: 'platform' | 'user'
  trigger_reason: string
  evidence_jsonb: Record<string, unknown>
  audience_jsonb: AudienceSpec
  expected_impact: string
  budget_cost_jsonb: BudgetSpec
  risk_level: RiskLevel
  touch_copy_jsonb: Record<string, string>
  execution_plan_jsonb: Record<string, unknown>
  rollback_plan: string
  observation_window_days: number
  skill_packs_jsonb: string[]
  status: CaseStatus
  group?: OpportunityGroup
  created_at: string
  expires_at?: string | null
}

export interface CheckItem { key: string; label: string; passed: boolean; detail: string }
export interface ChecksResult { checks: CheckItem[]; all_passed: boolean }

export interface OverviewData {
  facts: {
    pending_approvals: number
    touched_today: number
    budget_month: { cap: number; spent: number }
  }
  opportunity_cards: Record<OpportunityGroup, number>
  funnel: Record<string, number>
  self_ledger: Record<string, number>
  last_patrol: { ran_at: string; cases_opened: number; age_seconds: number } | null
  case_status_counts: Record<string, number>
  flags: Record<string, boolean>
}

export interface MarketingEvent {
  id: number
  event_type: string
  severity: 'info' | 'warn' | 'error' | 'security'
  message: string
  created_at: string
  case_id?: number | null
}

export interface Campaign {
  id: number
  campaign_code: string
  campaign_type: string
  name: string
  status: 'draft' | 'active' | 'paused' | 'ended'
  budget_cap_points: number
  spent_points: number
  is_resident: boolean
  created_at: string
}

export interface Policy { key: string; value_jsonb: { enabled?: boolean; value?: number } }

export const CASE_STATUS_LABEL: Record<CaseStatus, string> = {
  draft: '草稿', pending: '需人工审批', approved: '已通过', rejected: '已驳回',
  changes_requested: '待修改', executed: '已执行', expired: '已过期', cancelled: '已取消',
}

export const RISK_LABEL: Record<RiskLevel, string> = { low: '低风险', med: '中风险', high: '高风险' }

export const GROUP_LABEL: Record<OpportunityGroup, string> = {
  high_value: '高价值客户机会', convertible: '可提升转化客户',
  dormant: '沉睡促活客户', offer: '可推优惠客户',
}

export const FLAG_LABEL: Record<string, string> = {
  'marketing_agent.enabled': '总闸(感知巡逻 + 建议)',
  'marketing_agent.execute.enabled': '执行总开关',
  'marketing_agent.grant.enabled': '算力发放',
  'marketing_agent.notification.enabled': '触达发送',
  'marketing_agent.kill_switch': '急停(最高优先级)',
  'marketing_agent.control_group.enabled': '对照组模块',
  'marketing_agent.advisor_llm.enabled': '建议撰写 LLM',
}
