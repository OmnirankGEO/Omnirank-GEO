// [2026-05-18 帮助中心 Phase 2] 反馈提交 · 在线 POST + 离线 fallback
// 投票本地缓存已废弃 · my_vote 从 server 拿(my_vote 字段)
//
// 流程:
//  1. submitFeedback() 先 POST · 成功直接 return
//  2. POST 失败(网络/500)→ 落本地队列 omnirank_faq_feedback_queue
//  3. App 启动 / HelpFAQ mount 时 drainQueue() 把 pending 重发

import { postFeedback, type FeedbackPayload, type Urgency } from './faq-api-client'

export type { Urgency, Vote } from './faq-api-client'

type PendingEntry = FeedbackPayload & {
  createdAt: string
}

const QUEUE_KEY = 'omnirank_faq_feedback_queue'
const MAX_QUEUE = 100

function genClientId(): string {
  return safeRandomUUID()
}

function loadPending(): PendingEntry[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = localStorage.getItem(QUEUE_KEY)
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function savePending(queue: PendingEntry[]) {
  if (typeof window === 'undefined') return
  const trimmed = queue.length > MAX_QUEUE ? queue.slice(-MAX_QUEUE) : queue
  localStorage.setItem(QUEUE_KEY, JSON.stringify(trimmed))
}

function pushPending(entry: PendingEntry) {
  const queue = loadPending()
  queue.push(entry)
  savePending(queue)
}

function removePending(clientId: string) {
  const queue = loadPending().filter((e) => e.client_id !== clientId)
  savePending(queue)
}

export type SubmitInput = {
  faqId: number | null
  message: string
  urgency: Urgency
  contact: string
}

export type SubmitResult = {
  ok: boolean
  /** ok=true 时是后端 id · ok=false 时是 client_id */
  reference: string | number
  message: string
}

export async function submitFeedback(input: SubmitInput): Promise<SubmitResult> {
  const payload: FeedbackPayload = {
    client_id: genClientId(),
    faq_id: input.faqId,
    message: input.message,
    urgency: input.urgency,
    contact: input.contact,
  }
  try {
    const res = await postFeedback(payload)
    return {
      ok: true,
      reference: res.id,
      message: res.status === 'new' ? '已记下你的反馈' : '已收到(重复提交已合并)',
    }
  } catch (err) {
    // 离线/失败 · 落本地队列
    pushPending({ ...payload, createdAt: new Date().toISOString() })
    return {
      ok: false,
      reference: payload.client_id,
      message: '网络不通 · 已暂存, 下次打开会自动重发',
    }
  }
}

/** App 启动 / FAQ 页 mount 时调一次 · 把本地 pending 重发到后端 */
export async function drainQueue(): Promise<{ sent: number; remaining: number }> {
  const pending = loadPending()
  if (pending.length === 0) return { sent: 0, remaining: 0 }

  let sent = 0
  for (const entry of pending) {
    try {
      await postFeedback({
        client_id: entry.client_id,
        faq_id: entry.faq_id,
        message: entry.message,
        urgency: entry.urgency,
        contact: entry.contact,
      })
      removePending(entry.client_id)
      sent += 1
    } catch {
      // 留着下次再 drain · 不抛
    }
    // 限速 1 条 / 200ms · 避免突发
    await new Promise((r) => setTimeout(r, 200))
  }
  return { sent, remaining: loadPending().length }
}

import { safeRandomUUID } from '@/lib/safeRandomUUID';