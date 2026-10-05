/**
 * intake.ts — 客户资料补全邀请 API 客户端 (CTO-E 2026-04-26)
 *
 * 代理端走 axios api (带 JWT)
 * 公开端走 raw fetch (无 auth)
 */

import api from '@/lib/api';

// ==================== 类型 ====================

export type IntakeTokenStatus = 'active' | 'submitted' | 'revoked' | 'expired';

export interface IntakeToken {
  id: number;
  token: string;
  intake_url: string;
  brand_id: number;
  diagnosis_id?: number | null;
  quote_id?: number | null;
  status: IntakeTokenStatus;
  ai_suggest_count: number;
  expires_at: string | null;
  submitted_at: string | null;
  revoked_at: string | null;
  revoked_reason: string | null;
  created_at: string | null;
  is_actionable: boolean;
}

export type SubmissionStatus =
  | 'pending_review'
  | 'approved'
  | 'rejected'
  | 'partially_approved';

export interface ProfileSubmission {
  id: number;
  token_id: number;
  brand_id: number;
  submitted_by_name?: string | null;
  submitted_by_phone?: string | null;
  payload_jsonb: Record<string, unknown>;
  ai_suggested_jsonb?: Record<string, unknown> | null;
  diff_jsonb?: { fields: DiffField[] } | null;
  status: SubmissionStatus;
  reviewed_by?: number | null;
  reviewed_at?: string | null;
  approved_fields_jsonb?: string[] | null;
  review_notes?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DiffField {
  form_key: string;
  label: string;
  table: 'brands' | 'client_profiles';
  column: string;
  group: 'identity' | 'business' | 'marketing' | 'market_insight';
  type: 'str' | 'json_list' | 'enum_scope';
  current_value: unknown;
  customer_value: unknown;
  ai_value: unknown;
  recommended: unknown;
  recommended_source: 'customer' | 'ai' | null;
  is_change: boolean;
  would_overwrite: boolean;
}

// 公开端 schema (客户填写页用)
export interface PublicIntakeView {
  token: string;
  expires_at: string | null;
  ai_suggest_remaining: number;
  known_basics: Record<string, unknown>;
  fields_schema: Array<{
    form_key: string;
    label: string;
    table: 'brands' | 'client_profiles';
    group: string;
    type: 'str' | 'json_list' | 'enum_scope';
    is_missing: boolean;
    current_value: unknown;
  }>;
  completeness_now: number;
  // P0-7: 分步采访 flow + 草稿恢复
  flow_steps: IntakeFlowStep[];
  draft_payload: Record<string, unknown>;
  step_index: number;
  answered_fields: string[];
  draft_updated_at: string | null;
  social_field_keys: string[];
}

export type IntakeFieldType = 'text' | 'textarea' | 'tag_list' | 'scope_radio' | 'tel';

export interface IntakeFlowStep {
  id: string;
  title: string;
  question: string;
  helper: string;
  namespace: 'profile' | 'social';
  fields: Array<{
    key: string;
    label: string;
    type: IntakeFieldType;
    placeholder: string;
    optional: boolean;
    prefilled: boolean;
    current_value: unknown;
  }>;
  all_filled: boolean;
  skipped: boolean;
  max_fields: number;
}

export interface PublicAISuggestResponse {
  drafts: Record<
    string,
    { value: string | null; confidence: 'high' | 'medium' | 'low'; needs_more_info: boolean }
  >;
  degraded: boolean;
  ai_suggest_remaining: number;
  suggestable_fields: string[];
}

// ==================== 代理端 (auth) ====================

export const intakeApi = {
  async createToken(params: {
    brand_id: number;
    diagnosis_id?: number;
    quote_id?: number;
    ttl_days?: number;
  }): Promise<IntakeToken> {
    const r = await api.post('/api/m3/intake-tokens', params);
    return r.data.token;
  },

  async listTokens(brand_id: number): Promise<IntakeToken[]> {
    const r = await api.get('/api/m3/intake-tokens', { params: { brand_id } });
    return r.data.items || [];
  },

  async revokeToken(token_id: number, reason?: string): Promise<IntakeToken> {
    const r = await api.post(`/api/m3/intake-tokens/${token_id}/revoke`, { reason });
    return r.data.token;
  },

  async listSubmissions(
    brand_id: number,
    status?: SubmissionStatus,
  ): Promise<ProfileSubmission[]> {
    const r = await api.get('/api/m3/profile-submissions', {
      params: { brand_id, status },
    });
    return r.data.items || [];
  },

  async getSubmission(submission_id: number): Promise<ProfileSubmission | null> {
    try {
      const r = await api.get(`/api/m3/profile-submissions/${submission_id}`);
      return r.data.submission || null;
    } catch {
      return null;
    }
  },

  async approveSubmission(
    submission_id: number,
    approved_form_keys: string[],
    review_notes?: string,
  ): Promise<{
    status: SubmissionStatus;
    applied: string[];
    skipped: { form_key: string; reason: string }[];
    completeness_before: number;
    completeness_after: number;
    profile_id: string | null;
    should_regenerate_report: boolean;
  }> {
    const r = await api.post(
      `/api/m3/profile-submissions/${submission_id}/approve`,
      { approved_form_keys, review_notes },
    );
    return r.data;
  },

  async rejectSubmission(
    submission_id: number,
    reason?: string,
  ): Promise<{ status: SubmissionStatus }> {
    const r = await api.post(
      `/api/m3/profile-submissions/${submission_id}/reject`,
      { reason },
    );
    return r.data;
  },
};

// ==================== 公开端 (无 auth) ====================

async function publicJsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: string }).detail || '链接无效或已失效';
    const err = new Error(detail) as Error & { status?: number; code?: string };
    err.status = res.status;
    throw err;
  }
  return data as T;
}

export const publicIntakeApi = {
  view: (token: string) =>
    publicJsonFetch<PublicIntakeView>(`/api/public/intake/${encodeURIComponent(token)}`),

  aiSuggest: (token: string, payload: Record<string, unknown>) =>
    publicJsonFetch<PublicAISuggestResponse>(
      `/api/public/intake/${encodeURIComponent(token)}/ai-suggest`,
      { method: 'POST', body: JSON.stringify({ payload }) },
    ),

  // P0-7: 保存分步采访进度
  saveStep: (
    token: string,
    body: { step_index: number; payload: Record<string, unknown>; answered_fields: string[] },
  ) =>
    publicJsonFetch<{ success: boolean; step_index: number; answered_fields: string[] }>(
      `/api/public/intake/${encodeURIComponent(token)}/step`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  submit: (
    token: string,
    body: {
      payload: Record<string, unknown>;
      ai_suggested?: Record<string, unknown>;
      submitted_by_name?: string;
      submitted_by_phone?: string;
    },
  ) =>
    publicJsonFetch<{ success: boolean; submission_id: number; message: string }>(
      `/api/public/intake/${encodeURIComponent(token)}/submit`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
};
