// [2026-05-18 帮助中心 Phase 3] admin API 客户端

import { authFetch } from '@/lib/api'
import type {
  FAQAdminFeedback,
  FAQAdminItem,
  FAQCategory,
  FeedbackKind,
  FeedbackStatus,
  Urgency,
} from './types'

// ========== FAQ items CRUD ==========

export async function listAdminItems(): Promise<FAQAdminItem[]> {
  const res = await authFetch('/api/admin/faq/items')
  if (!res.ok) throw new Error(`列表加载失败: ${res.status}`)
  const data = (await res.json()) as { items: FAQAdminItem[] }
  return data.items
}

export async function getAdminItem(id: number): Promise<FAQAdminItem> {
  const res = await authFetch(`/api/admin/faq/items/${id}`)
  if (!res.ok) {
    if (res.status === 404) throw new Error('FAQ 不存在(可能被别人删了)')
    throw new Error(`加载失败: ${res.status}`)
  }
  return res.json()
}

export type CreateItemInput = {
  question: string
  category: FAQCategory
  answer_md: string
  sort_order: number
  is_published: boolean
}

export async function createItem(input: CreateItemInput): Promise<{ id: number }> {
  const res = await authFetch('/api/admin/faq/items', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(`新建失败: ${res.status}`)
  return res.json()
}

export async function updateItem(id: number, input: Partial<CreateItemInput>): Promise<void> {
  const res = await authFetch(`/api/admin/faq/items/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(`保存失败: ${res.status}`)
}

export async function deleteItem(id: number): Promise<void> {
  const res = await authFetch(`/api/admin/faq/items/${id}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`删除失败: ${res.status}`)
}

/** 拖拽排序 · 传按目标顺序排好的 id 数组, 后端按数组顺序赋 sort_order=10/20/30/... */
export async function reorderItems(ids: number[]): Promise<void> {
  const res = await authFetch('/api/admin/faq/items/reorder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  })
  if (!res.ok) throw new Error(`排序保存失败: ${res.status}`)
}

// ========== 小榜知识库重建 ==========
// 2026-05-25 帮助中心管理顶部加"重建小榜知识库"按钮 · 改完 FAQ / docs-data 后手动刷
export type ReindexScope = 'all' | 'faq'

export type ReindexResult = {
  ok: boolean
  scope: ReindexScope
  doc?: number
  faq?: number
  preset?: number
}

export async function rebuildXiaobangKB(scope: ReindexScope = 'all'): Promise<ReindexResult> {
  const res = await authFetch(`/api/admin/xiaobang/reindex?scope=${scope}`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error(`重建失败: ${res.status}`)
  return res.json()
}

// ========== 索引更新状态位 ==========
// [WO-A ② · 2026-08-20] 重建是 fire-and-forget:改完 FAQ 接口返 200 **不代表**索引更了。
// 失败读的是后端 ai_ops 告警(跨进程 · 部署期那次重建的失败也从这里看得见)。
export type ReindexState = 'idle' | 'running' | 'failed' | 'unknown'

export type ReindexStatus = {
  ok: boolean
  state: ReindexState
  running: { scope: string; count: number; elapsed_seconds: number }[]
  failures: { rule_key: string; fingerprint: string; title: string; detail: string }[]
  alerts_readable: boolean
}

export async function getReindexStatus(): Promise<ReindexStatus> {
  const res = await authFetch('/api/admin/xiaobang/reindex-status')
  if (!res.ok) throw new Error(`索引状态加载失败: ${res.status}`)
  return res.json()
}

// ========== 反馈 ==========

export type FeedbackFilters = {
  kind?: FeedbackKind
  status?: FeedbackStatus
  urgency?: Urgency
  limit?: number
}

export async function listFeedback(filters: FeedbackFilters = {}): Promise<FAQAdminFeedback[]> {
  const qs = new URLSearchParams()
  if (filters.kind) qs.set('kind', filters.kind)
  if (filters.status) qs.set('status', filters.status)
  if (filters.urgency) qs.set('urgency', filters.urgency)
  if (filters.limit) qs.set('limit', String(filters.limit))
  const query = qs.toString() ? `?${qs.toString()}` : ''
  const res = await authFetch(`/api/admin/faq/feedback${query}`)
  if (!res.ok) throw new Error(`反馈列表加载失败: ${res.status}`)
  const data = (await res.json()) as { items: FAQAdminFeedback[] }
  return data.items
}

export async function updateFeedback(
  id: number,
  input: { status?: FeedbackStatus; admin_note?: string },
): Promise<void> {
  const res = await authFetch(`/api/admin/faq/feedback/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(`保存失败: ${res.status}`)
}

export async function getFeedbackCounts(kind?: FeedbackKind, urgency?: Urgency): Promise<Record<FeedbackStatus, number>> {
  const qs = new URLSearchParams()
  if (kind) qs.set('kind', kind)
  if (urgency) qs.set('urgency', urgency)
  const query = qs.toString() ? `?${qs.toString()}` : ''
  const res = await authFetch(`/api/admin/faq/feedback/counts${query}`)
  if (!res.ok) throw new Error(`计数加载失败: ${res.status}`)
  return res.json()
}

// ========== AI 运维控制塔 · 反馈快捷任务 ==========
// 把一条 bug 反馈交给 AI 诊断 / Codex 修复,返回创建(或幂等命中)的任务。
export type AiOpsFeedbackTask = {
  task: { id: number; kind: string; status: string; risk_level: string }
  created: boolean
}

async function _aiOpsFeedbackAction(
  feedbackId: number,
  action: 'diagnose' | 'fix',
): Promise<AiOpsFeedbackTask> {
  const res = await authFetch(`/api/admin/ai-ops/feedback/${feedbackId}/${action}`, {
    method: 'POST',
  })
  if (!res.ok) {
    if (res.status === 404) throw new Error('反馈不存在(可能已被删除)')
    if (res.status === 400) throw new Error('只有 bug 类反馈能交给 AI 处理')
    throw new Error(`创建 AI 任务失败: ${res.status}`)
  }
  return res.json()
}

export function diagnoseFeedback(feedbackId: number): Promise<AiOpsFeedbackTask> {
  return _aiOpsFeedbackAction(feedbackId, 'diagnose')
}

export function fixFeedback(feedbackId: number): Promise<AiOpsFeedbackTask> {
  return _aiOpsFeedbackAction(feedbackId, 'fix')
}
