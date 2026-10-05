// [2026-05-18 帮助中心 Phase 2] FAQ API 客户端 · 调真后端
// 用全局 authFetch · JWT 自动注入 + 401 自动 refresh

import { authFetch } from '@/lib/api'
import type { FAQCategoryId } from './faq-data'

export type Vote = 'up' | 'down'
export type Urgency = 'low' | 'mid' | 'high'

export type FAQItemAPI = {
  id: number
  question: string
  answer_md: string
  category: FAQCategoryId
  sort_order: number
  is_published: boolean
  thumbs_up: number
  thumbs_down: number
  feedback_count: number
  /** 当前登录用户在这条 FAQ 上的投票 · null = 没投过 */
  my_vote: Vote | null
  created_at: string
  updated_at: string
}

export type VoteResult = {
  thumbs_up: number
  thumbs_down: number
  my_vote: Vote | null
}

export type FeedbackPayload = {
  client_id: string
  faq_id: number | null
  message: string
  urgency: Urgency
  contact: string
}

export async function fetchFAQItems(category?: FAQCategoryId): Promise<FAQItemAPI[]> {
  const qs = category ? `?category=${encodeURIComponent(category)}` : ''
  const res = await authFetch(`/api/faq/items${qs}`)
  if (!res.ok) throw new Error(`fetchFAQItems failed: ${res.status}`)
  const data = (await res.json()) as { items: FAQItemAPI[] }
  return data.items
}

export async function postVote(faqId: number, vote: Vote | null): Promise<VoteResult> {
  const res = await authFetch('/api/faq/vote', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ faq_id: faqId, vote }),
  })
  if (!res.ok) throw new Error(`postVote failed: ${res.status}`)
  return res.json()
}

export async function postFeedback(
  payload: FeedbackPayload,
): Promise<{ id: number; status: 'new' | 'existing' }> {
  const res = await authFetch('/api/faq/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error(`postFeedback failed: ${res.status}`)
  return res.json()
}
