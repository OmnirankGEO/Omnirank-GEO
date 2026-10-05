// 营销军师控制台 · 类型化 API 客户端(全走 authFetch · 前缀 /api/admin/marketing)
import { authFetch } from '@/lib/api'
import type { OverviewData, MarketingCase, ChecksResult, MarketingEvent, Campaign, Policy } from './types'

const BASE = '/api/admin/marketing'

async function _json<T>(res: Response, label: string): Promise<T> {
  if (res.status === 403) throw new Error('需要管理员权限')
  if (res.status === 404) throw new Error(`${label}:未找到`)
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const detail = (data && (data.detail?.msg || data.detail || data.error)) || `${label} 失败`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return data as T
}

export async function fetchOverview(): Promise<OverviewData> {
  return _json(await authFetch(`${BASE}/overview`), '概览')
}

export async function runPatrol(): Promise<{ cases_opened: number; signals_matched: number }> {
  return _json(await authFetch(`${BASE}/patrol/run`, { method: 'POST' }), '立即巡逻')
}

export async function fetchCases(status?: string): Promise<{ cases: MarketingCase[]; status_counts: Record<string, number> }> {
  const q = status ? `?status=${status}` : ''
  return _json(await authFetch(`${BASE}/cases${q}`), '案件列表')
}

export async function fetchCase(id: number): Promise<{ case: MarketingCase; checks: ChecksResult }> {
  return _json(await authFetch(`${BASE}/cases/${id}`), '案件详情')
}

export async function approveCase(id: number, note = ''): Promise<{ ok: boolean; execution?: unknown }> {
  return _json(await authFetch(`${BASE}/cases/${id}/approve`, {
    method: 'POST', body: JSON.stringify({ note }),
  }), '批准')
}

export async function rejectCase(id: number, note = ''): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/cases/${id}/reject`, {
    method: 'POST', body: JSON.stringify({ note }),
  }), '驳回')
}

export async function requestChanges(id: number, note = ''): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/cases/${id}/request-changes`, {
    method: 'POST', body: JSON.stringify({ note }),
  }), '要求修改')
}

export async function fetchEvents(limit = 40): Promise<{ events: MarketingEvent[] }> {
  return _json(await authFetch(`${BASE}/events?limit=${limit}`), '动态')
}

export async function fetchCampaigns(): Promise<{ campaigns: Campaign[] }> {
  return _json(await authFetch(`${BASE}/campaigns`), '活动')
}

export async function activateCampaign(id: number): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/campaigns/${id}/activate`, { method: 'POST' }), '上线')
}
export async function endCampaign(id: number): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/campaigns/${id}/end`, { method: 'POST' }), '下线')
}

export async function fetchLevers(): Promise<{ recharge_ladders: unknown[]; sku_shelf: unknown[]; first_charge_double: { exists: boolean; status?: string } }> {
  return _json(await authFetch(`${BASE}/levers`), '杠杆面板')
}

export async function fetchSignalRules(): Promise<{ rules: { rule_key: string; group: string; data_status: string }[] }> {
  return _json(await authFetch(`${BASE}/signal-rules`), '信号规则')
}

export async function fetchPolicies(): Promise<{ policies: Policy[]; flag_keys: string[]; config_keys: string[] }> {
  return _json(await authFetch(`${BASE}/policies`), '策略')
}

export async function patchPolicy(key: string, value: Record<string, unknown>): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/policies/${key}`, {
    method: 'PATCH', body: JSON.stringify({ value }),
  }), '更新策略')
}

export async function setKillSwitch(enabled: boolean): Promise<{ ok: boolean }> {
  return _json(await authFetch(`${BASE}/kill-switch`, {
    method: 'POST', body: JSON.stringify({ enabled }),
  }), '急停')
}

export async function fetchEffect(): Promise<{
  funnel_4level: Record<string, number>
  kpi: Record<string, number>
  attribution: { skill_pack: string; measured: number; converted: number; conv_rate_pct: number }[]
  ai_review: { markdown?: string; summary?: string }
  control_group: { enabled: boolean; note: string }
}> {
  return _json(await authFetch(`${BASE}/effect`), '效果复盘')
}

export async function fetchWeeklyReport(): Promise<{ report: { markdown?: string } }> {
  return _json(await authFetch(`${BASE}/reports/weekly`), '周报')
}
export async function generateWeeklyReport(): Promise<{ ok: boolean; week: string }> {
  return _json(await authFetch(`${BASE}/reports/weekly/generate`, { method: 'POST' }), '生成周报')
}
