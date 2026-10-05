/**
 * v3.3 / v3.4 GEO 全自动托管 — 前端 API 客户端
 *
 * 所有调用走 authFetch（带 JWT），错误用 toast 抛出
 */

import { authFetch } from '@/lib/api';
import type {
  EstimateResponse, Campaign, CampaignDetail,
  BrandPlanResponse, BrandDashboard, PendingReview,
  TierKey, CampaignMode,
} from './types';

const BASE = '/api/managed';

// ============================================================
// 通用 helper
// ============================================================

async function _fetch<T>(
  url: string,
  options: RequestInit = {},
): Promise<T> {
  const res = await authFetch(url, options);
  if (!res.ok) {
    let detail: any = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || body;
    } catch {
      // 忽略
    }
    const error: any = new Error(typeof detail === 'string' ? detail : (detail.message || JSON.stringify(detail)));
    error.code = typeof detail === 'object' ? detail.code : null;
    error.status = res.status;
    error.detail = detail;
    throw error;
  }
  return res.json();
}

// ============================================================
// v3.3 单词托管
// ============================================================

export interface EstimatePayload {
  keyword: string;
  city?: string;
  industry?: string;
  mode?: 'by_target_sov' | 'by_budget';
  target_sov_pct?: number;
  budget_yuan?: number;
  max_per_article_yuan?: number;
  check_frequency_per_day?: number;
}

export function estimate(payload: EstimatePayload): Promise<EstimateResponse> {
  return _fetch<EstimateResponse>(`${BASE}/estimate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export interface ConfirmRechargePayload {
  keyword: string;
  brand_id: number;
  target_sov_pct: number;
  target_display_label: string;
  tier_label: TierKey;
  selected_amount_yuan: number;
  mode: CampaignMode;
  max_per_article_yuan?: number;
  check_frequency_per_day?: number;
  estimate_quoted_at: string;
  current_plan?: Record<string, unknown>;
  agreement_consent: boolean;
  brand_voice_consent: boolean;
  agreement_version?: string;
}

export function confirmRecharge(payload: ConfirmRechargePayload): Promise<{
  success: boolean;
  campaign_id: number;
  status: string;
  current_balance_yuan: number;
  deducted_points: number;
}> {
  return _fetch(`${BASE}/confirm-recharge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function getCampaignDetail(campaignId: number): Promise<CampaignDetail> {
  return _fetch<CampaignDetail>(`${BASE}/${campaignId}`);
}

export function listCampaigns(params: {
  status?: string;
  brand_id?: number;
} = {}): Promise<{ campaigns: Campaign[]; total: number }> {
  const qs = new URLSearchParams();
  if (params.status) qs.set('status', params.status);
  if (params.brand_id) qs.set('brand_id', String(params.brand_id));
  const url = qs.toString() ? `${BASE}/?${qs}` : `${BASE}/`;
  return _fetch(url);
}

export interface AdjustPayload {
  adjustment_text?: string;
  new_target_sov_pct?: number;
  new_check_frequency_per_day?: number;
  new_max_per_article_yuan?: number;
  additional_recharge_yuan?: number;
  preferred_platforms?: string[];
}

export function adjustPlan(campaignId: number, payload: AdjustPayload): Promise<{
  new_estimate: EstimateResponse;
  diff_summary: string;
  requires_user_confirm: boolean;
}> {
  return _fetch(`${BASE}/${campaignId}/adjust`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function confirmAdjust(campaignId: number, payload: AdjustPayload): Promise<{ success: boolean }> {
  return _fetch(`${BASE}/${campaignId}/confirm-adjust`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function pauseCampaign(campaignId: number): Promise<{ success: boolean; status: string; balance_yuan: number }> {
  return _fetch(`${BASE}/${campaignId}/pause`, { method: 'POST' });
}

export function resumeCampaign(campaignId: number): Promise<{
  success?: boolean;
  status?: string;
  requires_re_estimate?: boolean;
  reason?: string;
  new_estimate?: EstimateResponse;
}> {
  return _fetch(`${BASE}/${campaignId}/resume`, { method: 'POST' });
}

export function confirmResume(campaignId: number): Promise<{ success: boolean; status: string }> {
  return _fetch(`${BASE}/${campaignId}/confirm-resume`, { method: 'POST' });
}

export function topUp(campaignId: number, amount_yuan: number): Promise<{
  success: boolean;
  status: string;
  new_balance: number;
}> {
  return _fetch(`${BASE}/${campaignId}/top-up`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ amount_yuan }),
  });
}

export function withdrawPending(campaignId: number, article_ids: number[]): Promise<{
  withdrawn_count: number;
  refunded_yuan: number;
}> {
  return _fetch(`${BASE}/${campaignId}/withdraw-pending`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ article_ids }),
  });
}

// ============================================================
// 待审队列
// ============================================================

export function getUserPendingReviews(): Promise<{ reviews: PendingReview[]; total: number }> {
  return _fetch(`${BASE}/reviews/pending`);
}

export function approveReview(reviewId: number, note?: string): Promise<{ success: boolean }> {
  return _fetch(`${BASE}/review/${reviewId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note: note || null }),
  });
}

export function rejectReview(reviewId: number, note?: string): Promise<{ success: boolean }> {
  return _fetch(`${BASE}/review/${reviewId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note: note || null }),
  });
}

// ============================================================
// v3.4 全品牌托管
// ============================================================

export interface BrandEstimatePayload {
  keywords: string[];
  brand_id: number;
  city?: string;
  industry?: string;
  uniform_target_sov_pct?: number;
  per_keyword_sov?: Record<string, number>;
  total_budget_yuan?: number;
}

export function estimateBrand(payload: BrandEstimatePayload): Promise<BrandPlanResponse> {
  return _fetch(`${BASE}/brand/estimate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export interface BrandConfirmRechargePayload {
  brand_id: number;
  final_total_yuan: number;
  raw_total_yuan: number;
  markup_factor: number;
  subcampaigns: { keyword: string; sov_pct: number; articles: number; cost_yuan: number }[];
  estimate_quoted_at: string;
  mode: CampaignMode;
  agreement_consent: boolean;
  brand_voice_consent: boolean;
  agreement_version?: string;
}

export function confirmBrandRecharge(payload: BrandConfirmRechargePayload): Promise<{
  success: boolean;
  brand_package_id: number;
  campaign_ids: number[];
  total_price_yuan: number;
}> {
  return _fetch(`${BASE}/brand/confirm-recharge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function getBrandDashboard(brandId: number): Promise<BrandDashboard> {
  return _fetch(`${BASE}/brand/${brandId}/dashboard`);
}

// ============================================================
// 物料中心
// ============================================================

export interface GeoAssetsPayload {
  company_intro?: { text?: string; file_url?: string };
  selling_points?: any;
  cases?: any[];
  competitors_real?: any[];
  real_data_points?: any;
  brand_story?: any;
  milestones?: any[];
  team_core?: any[];
  testimonials?: any[];
  service_flow?: any;
  price_packages?: any[];
  target_keywords?: string[];
  industry_position?: string;
  target_customer_profile?: any;
}

export function getGeoAssets(profileId: number): Promise<{
  profile_id: number;
  geo_assets: GeoAssetsPayload;
  completeness_score: number;
  completeness_detail: any;
  industry: string | null;
  city: string | null;
}> {
  return _fetch(`/api/profiles/${profileId}/geo-assets`);
}

export function updateGeoAssets(profileId: number, payload: GeoAssetsPayload): Promise<{
  success: boolean;
  completeness_score: number;
  completeness_detail: any;
  updated_fields: string[];
}> {
  return _fetch(`/api/profiles/${profileId}/geo-assets`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}
