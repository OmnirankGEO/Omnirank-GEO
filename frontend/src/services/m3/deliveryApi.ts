/**
 * M3 交付端深度页 API helper
 *
 * 复用现有 backend 的 /api/writing /api/publications /api/monitoring /api/reports endpoint。
 * 老板原话(B 路线开工纪律):
 *   "深度页 M3 化必须复用业务逻辑 · 只换 UI · 不重写 axios api / 不重写 backend"
 *
 * 这层做:
 *   - 类型化封装(写在 types · 不在每页里 inline interface)
 *   - 跨页面共享 helper(getWritingProjects / listPublicationsByBrand 等)
 *   - 失败兜底(空数组而非 throw · UI 层走 empty state)
 */

import api from '@/lib/api';
import { authFetch } from '@/lib/api';

// ============================================================
// 写作 / 写作大厅(/api/writing/*)
// ============================================================

export interface WritingProject {
  id: number;
  brand_id: number;
  brand_name?: string;
  industry?: string;
  status: string; // 'planning' | 'researching' | 'writing' | 'reviewing' | 'completed' | ...
  created_at: string;
  updated_at?: string;
  topics_count?: number;
  articles_count?: number;
  total_articles?: number;
  pending_review?: number;
  done_count?: number;
  drafting_count?: number;
}

export async function listWritingProjects(status?: string): Promise<WritingProject[]> {
  const url = status ? `/api/writing/projects?status=${status}` : '/api/writing/projects';
  try {
    const res = await authFetch(url);
    if (!res.ok) return [];
    const data = await res.json();
    if (Array.isArray(data)) return data as WritingProject[];
    if (Array.isArray(data?.projects)) return data.projects as WritingProject[];
    if (Array.isArray(data?.data)) return data.data as WritingProject[];
    return [];
  } catch {
    return [];
  }
}

export interface ArticleSummary {
  id: number;
  title: string;
  status: string;
  word_count?: number;
  channel?: string;
  topic_id?: number;
  brand_name?: string;
  created_at?: string;
  reviewed?: boolean;
}

export async function listProjectArticles(projectId: number): Promise<ArticleSummary[]> {
  try {
    const res = await authFetch(`/api/writing/progress/${projectId}`);
    if (!res.ok) return [];
    const data = await res.json();
    if (Array.isArray(data?.articles)) return data.articles as ArticleSummary[];
    return [];
  } catch {
    return [];
  }
}

// ============================================================
// 发布 / 投放(/api/publications)
// ============================================================

export interface Publication {
  id: number;
  quote_id?: number;
  brand_id?: number;
  brand_name?: string;
  channel: string; // '知乎' / '公众号' / '百家号' / '今日头条' 等
  url?: string;
  title?: string;
  status: string; // 'draft' | 'submitted' | 'published' | 'verified' | 'rejected'
  created_at: string;
  published_at?: string;
  screenshot_url?: string;
  article_id?: number;
}

export async function listPublicationsByQuote(quoteId: number): Promise<Publication[]> {
  try {
    const res = await authFetch(`/api/publications/${quoteId}`);
    if (!res.ok) return [];
    const data = await res.json();
    if (Array.isArray(data)) return data as Publication[];
    if (Array.isArray(data?.publications)) return data.publications as Publication[];
    return [];
  } catch {
    return [];
  }
}

export async function listPublicationsAll(): Promise<Publication[]> {
  try {
    const res = await api.get<{ publications?: Publication[]; data?: Publication[] }>(
      '/api/publications/all',
    );
    return res.data?.publications || res.data?.data || [];
  } catch {
    return [];
  }
}

// ============================================================
// 监测 / 监测详情(/api/monitoring/*)
// ============================================================

export interface MonitorClient {
  quote_id: number;
  brand_id: number;
  brand_name: string;
  service_status?: string;
  service_start_date?: string;
  service_end_date?: string;
  keywords_count?: number;
  last_monitor_at?: string;
}

export async function listMonitorClients(): Promise<MonitorClient[]> {
  try {
    const res = await authFetch('/api/monitoring/clients');
    if (!res.ok) return [];
    const data = await res.json();
    if (Array.isArray(data)) return data as MonitorClient[];
    if (Array.isArray(data?.clients)) return data.clients as MonitorClient[];
    return [];
  } catch {
    return [];
  }
}

export interface MonitorTrendPoint {
  date: string;
  appear_rate?: number;
  rank_avg?: number;
  citations?: number;
  total_keywords?: number;
}

export async function getMonitorTrend(brandId: number, days = 7): Promise<MonitorTrendPoint[]> {
  try {
    const res = await authFetch(`/api/monitoring/trend?brand_id=${brandId}&days=${days}`);
    if (!res.ok) return [];
    const data = await res.json();
    if (Array.isArray(data)) return data as MonitorTrendPoint[];
    if (Array.isArray(data?.trend)) return data.trend as MonitorTrendPoint[];
    if (Array.isArray(data?.points)) return data.points as MonitorTrendPoint[];
    return [];
  } catch {
    return [];
  }
}

export interface MonitorTaskBrief {
  id: number;
  brand_id?: number;
  brand_name?: string;
  status: string;
  created_at: string;
  finished_at?: string;
  total_engines?: number;
  appear_rate?: number;
}

export async function getLatestMonitorTask(brandId: number): Promise<MonitorTaskBrief | null> {
  try {
    const res = await authFetch(`/api/monitoring/tasks?brand_id=${brandId}&limit=1`);
    if (!res.ok) return null;
    const data = await res.json();
    const task = (data?.tasks || data?.data || [])[0];
    return task ?? null;
  } catch {
    return null;
  }
}

// ============================================================
// 报告(/api/reports/*)
// ============================================================

export interface ReportSummary {
  id: number;
  brand_id?: number;
  brand_name?: string;
  type: string; // 'monthly' / 'weekly' / 'diagnosis' / ...
  period?: string;
  status: string; // 'draft' / 'ready' / 'published' / 'archived'
  created_at: string;
  updated_at?: string;
  has_portal_token?: boolean;
}

export async function listReports(brandId?: number, type?: string, limit = 50): Promise<ReportSummary[]> {
  let url = `/api/reports/all?limit=${limit}`;
  if (brandId) url += `&brand_id=${brandId}`;
  if (type) url += `&type=${type}`;
  try {
    const res = await authFetch(url);
    if (!res.ok) return [];
    const data = await res.json();
    if (Array.isArray(data)) return data as ReportSummary[];
    if (Array.isArray(data?.reports)) return data.reports as ReportSummary[];
    if (Array.isArray(data?.data)) return data.data as ReportSummary[];
    return [];
  } catch {
    return [];
  }
}

export async function getPortalTokenByBrand(brandId: number): Promise<string | null> {
  try {
    const res = await authFetch(`/api/portal/tokens/by-brand/${brandId}`);
    if (!res.ok) return null;
    const data = await res.json();
    return data?.token ?? null;
  } catch {
    return null;
  }
}

// 集中导出
export const m3DeliveryApi = {
  listWritingProjects,
  listProjectArticles,
  listPublicationsByQuote,
  listPublicationsAll,
  listMonitorClients,
  getMonitorTrend,
  getLatestMonitorTask,
  listReports,
  getPortalTokenByBrand,
};
