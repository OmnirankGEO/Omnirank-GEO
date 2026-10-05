/**
 * V3.3.1 服务费 API client
 *
 * 关联:
 * - 后端 api/service_fee_api.py
 * - 决策书 §3.2 §3.3 §3.5
 */

import { authFetch } from '@/lib/api';

export interface ServiceFeeBalance {
  pending_yuan: number;
  settled_yuan: number;
  converted_yuan: number;
  withdrawn_yuan: number;
  debt_yuan: number;
}

export interface ConversionInfo {
  enabled: boolean;
  bonus_rate: number;
  min_age_days: number;
  monthly_quota_yuan: number;
  monthly_used_yuan: number;
  monthly_available_yuan: number;
}

export interface WithdrawalInfo {
  enabled: boolean;
  min_amount_yuan: number;
}

export interface ServiceFeeBalanceResponse {
  success: boolean;
  is_l2: boolean;
  agent_tier?: 'standard' | 'premium' | 'strategic';
  kyc_passed?: boolean;
  bank_account_verified?: boolean;
  data: ServiceFeeBalance;
  conversion?: ConversionInfo;
  withdrawal?: WithdrawalInfo;
}

export interface ConvertResponse {
  success: boolean;
  conversion_order_id: string;
  paid_points_granted: number;
  bonus_points_granted: number;
  bonus_rate: number;
  bonus_expires_at: string;
  used_records: number[];
  amount_yuan: number;
}

export interface WithdrawalRequestResponse {
  success: boolean;
  settlement_id: number;
  settlement_code: string;
  status: string;
  review_sla_days: string;
  invoice_required: boolean;
  requires_dual_sign: boolean;
  monthly_withdrawn_yuan: number;
  amount_yuan: number;
}

export interface ServiceFeeHistoryRow {
  id: number;
  source_user_id: number;
  source_order_id: string;
  source_order_type: string;
  gross_amount_yuan: number;
  net_cash_revenue_yuan: number;
  amount_yuan: number;
  service_fee_rate: number;
  status: string;
  created_at: string;
  available_at: string;
  settled_at?: string;
  converted_at?: string;
  withdraw_requested_at?: string;
  withdrawn_at?: string;
  clawback_amount?: number;
  clawback_reason?: string;
}

export async function fetchServiceFeeBalance(): Promise<ServiceFeeBalanceResponse> {
  const res = await authFetch('/api/service-fee/balance', { method: 'GET' });
  if (!res.ok) {
    return {
      success: false,
      is_l2: false,
      data: {
        pending_yuan: 0, settled_yuan: 0,
        converted_yuan: 0, withdrawn_yuan: 0, debt_yuan: 0,
      },
    };
  }
  return res.json();
}

export async function convertServiceFee(amount_yuan: number): Promise<ConvertResponse> {
  const res = await authFetch('/api/service-fee/convert', {
    method: 'POST',
    body: JSON.stringify({ amount_yuan }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw err.detail || err;
  }
  return res.json();
}

export async function requestLargeConversion(amount_yuan: number, reason: string = '') {
  const res = await authFetch('/api/service-fee/convert-large', {
    method: 'POST',
    body: JSON.stringify({ amount_yuan, reason }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw err.detail || err;
  }
  return res.json();
}

export async function requestWithdrawal(
  amount_yuan: number,
  bank_account_masked: string,
): Promise<WithdrawalRequestResponse> {
  const res = await authFetch('/api/service-fee/withdraw-request', {
    method: 'POST',
    body: JSON.stringify({ amount_yuan, bank_account_masked }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw err.detail || err;
  }
  return res.json();
}

export async function fetchServiceFeeHistory(
  status?: string,
  limit = 50,
  offset = 0,
): Promise<{ success: boolean; total: number; data: ServiceFeeHistoryRow[] }> {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  const res = await authFetch(`/api/service-fee/history?${params.toString()}`, { method: 'GET' });
  if (!res.ok) {
    return { success: false, total: 0, data: [] };
  }
  return res.json();
}
