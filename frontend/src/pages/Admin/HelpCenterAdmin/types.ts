// [2026-05-18 帮助中心 Phase 3] admin 共用类型 · 跟后端 schema 对齐

export type FAQCategory = 'billing' | 'operation' | 'data' | 'account'
export type Urgency = 'low' | 'mid' | 'high'
export type FeedbackStatus = 'pending' | 'read' | 'done' | 'closed'
export type FeedbackKind = 'faq' | 'bug'
export type FeedbackSubmitterIdentity = 'normal_user' | 'agent' | 'l2' | 'admin' | ''

export type FAQAdminItem = {
  id: number
  question: string
  answer_md: string
  category: FAQCategory
  sort_order: number
  is_published: boolean
  thumbs_up: number
  thumbs_down: number
  feedback_count: number
  my_vote: 'up' | 'down' | null
  created_at: string
  updated_at: string
}

export type FAQAdminFeedback = {
  id: number
  client_id: string
  faq_id: number | null
  message: string
  urgency: Urgency
  kind: FeedbackKind
  screenshot_url: string
  screenshot_key?: string
  ai_answer: string
  submitter_identity?: FeedbackSubmitterIdentity
  submitter_agent_level?: number
  contact: string
  user_id: number
  status: FeedbackStatus
  admin_note: string
  created_at: string
  handled_at: string | null
  handled_by: number | null
  faq_question: string | null
  user_username: string | null
  user_display_name: string | null
}

export const CATEGORY_LABEL: Record<FAQCategory, string> = {
  billing: '计费',
  operation: '操作',
  data: '数据',
  account: '账号',
}

export const URGENCY_LABEL: Record<Urgency, string> = {
  low: '不急',
  mid: '影响业务',
  high: '卡死',
}

export const STATUS_LABEL: Record<FeedbackStatus, string> = {
  pending: '待处理',
  read: '已读',
  done: '已处理',
  closed: '关闭',
}

export const FEEDBACK_KIND_LABEL: Record<FeedbackKind, string> = {
  faq: 'FAQ 反馈',
  bug: '问题反馈',
}
