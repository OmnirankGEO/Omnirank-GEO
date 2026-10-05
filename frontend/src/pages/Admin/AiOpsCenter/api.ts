// AI 运维控制塔 · 前端 API 客户端 · 全部走 authFetch · 全部 admin-only 后端端点

import { authFetch } from '@/lib/api'
import type {
  AiOpsAlert,
  AiOpsApproval,
  AiOpsArtifact,
  AiOpsEvent,
  AiOpsOverview,
  AiOpsReport,
  AiOpsReportListItem,
  AiOpsTask,
  PolicyMap,
  TaskKind,
} from './types'

const BASE = '/api/admin/ai-ops'

async function _json<T>(res: Response, label: string): Promise<T> {
  if (!res.ok) {
    if (res.status === 403) throw new Error('需要管理员权限')
    if (res.status === 404) throw new Error(`${label}: 不存在`)
    let detail = ''
    try {
      const d = await res.json()
      detail = d?.detail || ''
    } catch { /* ignore */ }
    throw new Error(`${label} 失败: ${res.status}${detail ? ` · ${detail}` : ''}`)
  }
  return res.json() as Promise<T>
}

export async function getOverview(): Promise<AiOpsOverview> {
  return _json(await authFetch(`${BASE}/overview`), '总览加载')
}

export async function listTasks(filters: { status?: string; kind?: string; limit?: number } = {}): Promise<AiOpsTask[]> {
  const qs = new URLSearchParams()
  if (filters.status) qs.set('status', filters.status)
  if (filters.kind) qs.set('kind', filters.kind)
  if (filters.limit) qs.set('limit', String(filters.limit))
  const q = qs.toString() ? `?${qs}` : ''
  const data = await _json<{ items: AiOpsTask[] }>(await authFetch(`${BASE}/tasks${q}`), '任务列表')
  return data.items
}

export async function getTask(taskId: number): Promise<AiOpsTask> {
  return _json(await authFetch(`${BASE}/tasks/${taskId}`), '任务详情')
}

export async function getTaskEvents(taskId: number, afterId?: number): Promise<AiOpsEvent[]> {
  const q = afterId ? `?after_id=${afterId}` : ''
  const data = await _json<{ items: AiOpsEvent[] }>(await authFetch(`${BASE}/tasks/${taskId}/events${q}`), '事件流')
  return data.items
}

export async function getTaskArtifacts(taskId: number): Promise<AiOpsArtifact[]> {
  const data = await _json<{ items: AiOpsArtifact[] }>(await authFetch(`${BASE}/tasks/${taskId}/artifacts`), '产物')
  return data.items
}

export type CreateTaskInput = {
  kind: TaskKind
  title?: string
  instruction?: string
  priority?: string
  source_type?: string
}

export async function createTask(input: CreateTaskInput): Promise<{ task: AiOpsTask; created: boolean }> {
  return _json(await authFetch(`${BASE}/tasks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }), '创建任务')
}

export async function cancelTask(taskId: number): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/tasks/${taskId}/cancel`, { method: 'POST' }), '取消任务')
}

export type ChatCommandResult = {
  task: AiOpsTask | null   // 问数类直答时为 null(不建任务)
  created: boolean
  reply?: string           // 人话回复:创建了什么 / 接下来会发生什么 / 要不要你做什么
  report_id?: number
  report_date?: string     // 日报意图返回,前端按钮直达 /admin/ai-ops/reports/{date}
}

// v2 总管:history=最近对话(最多 20 条),后端喂给生成式回答做多轮上下文(无状态,不落库)
export type ChatHistoryItem = { role: 'user' | 'ai'; text: string }

export async function chatCommand(
  message: string,
  context: Record<string, unknown> = {},
  history: ChatHistoryItem[] = [],
): Promise<ChatCommandResult> {
  return _json(await authFetch(`${BASE}/chat-command`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, context, history }),
  }), '命令台')
}

export async function diagnoseFeedback(feedbackId: number): Promise<{ task: AiOpsTask; created: boolean }> {
  return _json(await authFetch(`${BASE}/feedback/${feedbackId}/diagnose`, { method: 'POST' }), 'AI 诊断')
}

export async function fixFeedback(feedbackId: number): Promise<{ task: AiOpsTask; created: boolean }> {
  return _json(await authFetch(`${BASE}/feedback/${feedbackId}/fix`, { method: 'POST' }), 'Codex 修复')
}

export async function listApprovals(status?: string): Promise<AiOpsApproval[]> {
  const q = status ? `?status=${status}` : ''
  const data = await _json<{ items: AiOpsApproval[] }>(await authFetch(`${BASE}/approvals${q}`), '审批队列')
  return data.items
}

export async function approveAction(approvalId: number): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/approvals/${approvalId}/approve`, { method: 'POST' }), '审批通过')
}

export async function rejectAction(approvalId: number, reason = ''): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/approvals/${approvalId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  }), '审批驳回')
}

export async function listReports(): Promise<AiOpsReportListItem[]> {
  const data = await _json<{ items: AiOpsReportListItem[] }>(await authFetch(`${BASE}/reports`), '日报列表')
  return data.items
}

export async function getReport(reportDate: string): Promise<AiOpsReport> {
  return _json(await authFetch(`${BASE}/reports/${reportDate}`), '日报')
}

export async function generateReport(): Promise<{ task: AiOpsTask; created: boolean }> {
  return _json(await authFetch(`${BASE}/reports/generate`, { method: 'POST' }), '生成日报')
}

// ===== 告警 / 巡逻(包B) =====

export async function listAlerts(status?: 'firing' | 'resolved', limit = 100):
  Promise<{ alerts: AiOpsAlert[]; firing_count: number }> {
  const qs = new URLSearchParams()
  if (status) qs.set('status', status)
  qs.set('limit', String(limit))
  return _json(await authFetch(`${BASE}/alerts?${qs}`), '告警列表')
}

export async function resolveAlert(alertId: number): Promise<{ ok: boolean; alert: AiOpsAlert }> {
  return _json(await authFetch(`${BASE}/alerts/${alertId}/resolve`, { method: 'POST' }), '恢复告警')
}

export async function runPatrol(): Promise<{ ok: boolean; firing: number; opened: number; resolved: number; duration_ms: number }> {
  return _json(await authFetch(`${BASE}/patrol/run`, { method: 'POST' }), '立即巡逻')
}

export async function getPolicies(): Promise<{ policies: PolicyMap }> {
  return _json(await authFetch(`${BASE}/policies`), '策略')
}

export async function patchPolicy(key: string, value: Record<string, unknown>): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/policies/${key}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  }), '更新策略')
}

export async function setKillSwitch(enabled: boolean): Promise<{ ok: boolean; kill_switch: boolean }> {
  return _json(await authFetch(`${BASE}/kill-switch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  }), 'Kill Switch')
}
