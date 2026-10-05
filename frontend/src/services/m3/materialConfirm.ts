/**
 * M3 写作资料确认 — 前端 service (CTO-F 2026-04-27)
 *
 * 包装 /api/m3/material-confirm/* 的 5 端点 · 也封装 /m/:token 公开端的 fetch
 *
 * 不动 /api/marketing-confirm/* 老入口(销售从 /m3/customer 进 · 老 MarketingTab 仍 work)
 */

import api from '@/lib/api';

// ============================================================
// 状态机类型
// ============================================================

export type MaterialConfirmStatus =
  | 'none' // 还没整理写作资料
  | 'draft' // 已整理 · 还没生成确认链
  | 'pending' // 链接已发 · 等客户动作
  | 'feedback' // 客户提了修改意见
  | 'confirmed' // 客户确认
  | 'expired' // 链接过期
  | 'revoked'; // 代理撤销

export interface MaterialsSummary {
  company_name: string;
  industry: string;
  intro_excerpt: string;
  usp_excerpt: string;
  fields_filled: string[];
  fields_missing: string[];
  filled_count: number;
  total_fields: number;
  selling_points_count: number;
  cases_count: number;
  testimonials_count: number;
}

export interface MaterialConfirmStatusResponse {
  status: MaterialConfirmStatus;
  has_session: boolean;
  token: string | null;
  token_url: string | null;
  expires_at: string | null;
  confirmed_at: string | null;
  customer_notes: string;
  materials_summary: MaterialsSummary;
  current_summary?: MaterialsSummary;
  last_session_id: number | null;
  can_generate_link: boolean;
  brand_id: number;
  brand_name: string;
}

export interface CleanedMaterials {
  company_intro: string;
  unique_value: string;
  service_area: string;
  methodology: string;
  core_selling_points: Array<{ point: string; evidence?: string }>;
  case_studies: Array<{ client?: string; background?: string; solution?: string; results?: string }>;
  testimonials: Array<{ name?: string; title?: string; company?: string; quote?: string }>;
  credentials: Array<{ type?: string; name?: string }>;
}

export interface CleanResponse {
  success: boolean;
  cleaned_materials: CleanedMaterials;
  missing_fields: string[];
  warnings: string[];
  confidence: number;
  can_generate_link: boolean;
}

export interface GenerateLinkResponse {
  success: boolean;
  token: string;
  url: string;
  expires_at?: string;
  reused: boolean;
}

// ============================================================
// API client
// ============================================================

export const materialConfirmApi = {
  /**
   * 拉品牌的写作资料确认完整状态
   */
  async getStatus(brandId: number): Promise<MaterialConfirmStatusResponse> {
    const res = await api.get<MaterialConfirmStatusResponse>(
      `/api/m3/material-confirm/status/${brandId}`,
    );
    return res.data;
  },

  /**
   * AI 整理代理粘贴/上传的客户原始资料
   * raw_text 和 notes 至少要有一个非空
   */
  async clean(payload: {
    brand_id: number;
    diagnosis_id?: number;
    raw_text?: string;
    source_url?: string;
    uploaded_file_refs?: string[];
    notes?: string;
  }): Promise<CleanResponse> {
    const res = await api.post<CleanResponse>('/api/m3/material-confirm/clean', payload);
    return res.data;
  },

  /**
   * 生成新的客户确认链
   * force_new=false 时若有 pending 复用 token + 刷新 snapshot
   */
  async generateLink(brandId: number, forceNew = false): Promise<GenerateLinkResponse> {
    const res = await api.post<GenerateLinkResponse>('/api/m3/material-confirm/generate-link', {
      brand_id: brandId,
      force_new: forceNew,
    });
    return res.data;
  },

  /**
   * 刷新 snapshot 后重发(适合 feedback 处理后)
   */
  async resend(brandId: number): Promise<GenerateLinkResponse> {
    const res = await api.post<GenerateLinkResponse>(
      `/api/m3/material-confirm/resend/${brandId}`,
    );
    return res.data;
  },

  /**
   * 撤销 session(误发 / 客户身份变更)
   */
  async revoke(sessionId: number): Promise<{ success: boolean }> {
    const res = await api.post<{ success: boolean }>(
      `/api/m3/material-confirm/revoke/${sessionId}`,
    );
    return res.data;
  },
};

// ============================================================
// 状态机展示工具
// ============================================================

export const STATUS_LABEL: Record<MaterialConfirmStatus, string> = {
  none: '还没整理写作资料',
  draft: '已整理 · 还没发链接',
  pending: '已发链接 · 等客户确认',
  feedback: '客户有修改意见',
  confirmed: '客户已确认',
  expired: '链接已过期',
  revoked: '链接已撤销',
};

export const STATUS_TONE: Record<
  MaterialConfirmStatus,
  'neutral' | 'info' | 'warn' | 'ok' | 'danger'
> = {
  none: 'neutral',
  draft: 'neutral',
  pending: 'info',
  feedback: 'warn',
  confirmed: 'ok',
  expired: 'warn',
  revoked: 'danger',
};

export function isWritingSafe(status: MaterialConfirmStatus): boolean {
  return status === 'confirmed';
}

export function buildWechatScript(brandName: string, url: string, status: MaterialConfirmStatus): string {
  const name = brandName?.trim() || '您好';
  if (status === 'feedback') {
    return (
      `${name},您之前提的修改意见我们已经处理完。\n\n` +
      `${url}\n\n` +
      `请再确认一下,我们立刻安排写作。链接 7 天有效。`
    );
  }
  return (
    `${name},这是为您整理的写作资料,请抽 2 分钟看一下。\n\n` +
    `${url}\n\n` +
    `确认无误后我们就开始为您创作 AI 搜索优化文章。链接 7 天有效。`
  );
}
