/**
 * V3.5 W3+W4 · 前端 API client(协议 + 客户工作台 + admin 治理)
 */

import { buildQuotedOrderBody, getGeoPricingEntryContract } from '@/lib/pricingEntryContracts';
import { authFetch } from '@/lib/api';
import { takeBootstrapAgreementProbe } from '@/lib/bootstrapAuthProbes';

const CUSTOMER_RECHARGE_PRICING_ENTRY = getGeoPricingEntryContract('customer-recharge');

interface V35ApiErrorBody {
  code?: unknown;
  detail?: unknown;
  message?: unknown;
}

export interface V35ApiError extends Error {
  code?: string;
  detail?: unknown;
  response?: { status: number; data: V35ApiErrorBody };
}

export function getV35ApiErrorCode(error: unknown): string | undefined {
  const apiError = error as V35ApiError;
  if (apiError.code) return apiError.code;
  const body = apiError.response?.data;
  if (typeof body?.code === 'string') return body.code;
  if (body?.detail && typeof body.detail === 'object') {
    const code = (body.detail as { code?: unknown }).code;
    if (typeof code === 'string') return code;
  }
  return undefined;
}

async function call<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(opts.headers as Record<string, string> | undefined),
  };
  const r = await authFetch(path, { ...opts, headers });
  if (!r.ok) {
    const raw = await r.text();
    let body: V35ApiErrorBody = {};
    try {
      const parsed = raw ? JSON.parse(raw) : {};
      body = parsed && typeof parsed === 'object'
        ? (parsed as V35ApiErrorBody)
        : { detail: parsed };
    } catch {
      body = { detail: raw || `HTTP ${r.status}` };
    }
    const detail = body.detail ?? body.message ?? `HTTP ${r.status}`;
    const detailObject = detail && typeof detail === 'object'
      ? (detail as { code?: unknown; message?: unknown })
      : null;
    const message = typeof detail === 'string'
      ? detail
      : typeof detailObject?.message === 'string'
        ? detailObject.message
        : `HTTP ${r.status}`;
    const error = new Error(message) as V35ApiError;
    const code = typeof body.code === 'string'
      ? body.code
      : typeof detailObject?.code === 'string'
        ? detailObject.code
        : undefined;
    error.code = code;
    error.detail = detail;
    error.response = { status: r.status, data: body };
    throw error;
  }
  if (r.status === 204) return undefined as unknown as T;
  return r.json();
}

// ============================================================
// W3 协议签约
// ============================================================

export const agreementApi = {
  status: (version?: string) => version
    ? call(`/api/agent/agreement/v35-status?version=${version}`)
    : takeBootstrapAgreementProbe<any>() || call('/api/agent/agreement/v35-status'),
  sign: (body: { version?: string; content_hash?: string } = {}) =>
    call('/api/agent/agreement/v35-sign', { method: 'POST', body: JSON.stringify(body) }),
  reject: (body: { version?: string; reason?: string } = {}) =>
    call('/api/agent/agreement/v35-reject', { method: 'POST', body: JSON.stringify(body) }),
};

// ============================================================
// W3 客户工作台
// ============================================================

export const customerApi = {
  acceptLegalAgreements: (body: {
    terms_accepted: boolean;
    terms_version: string;
    surface: string;
  }) => call<{
    success: true;
    acceptance: { acceptance_id: string; agreement_version: string; content_hash: string };
  }>('/api/auth/legal-agreements/accept', {
    method: 'POST',
    body: JSON.stringify(body),
  }),
  creditSummary: () => call<{
    tool_credit_points: number;
    publish_credit_points: number;
    bonus_credit_points: number;
    total_purchased_points: number;
    service: {
      service_status: 'platform_managed';
      configuration_status: 'ready' | 'review_required' | 'unavailable';
      account_configured: boolean;
      dispute_pending: boolean;
    };
    recent_transactions: Array<any>;
  }>('/api/customer/credit/summary'),

  creditTransactions: (limit = 50, offset = 0, pool?: string) => {
    const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (pool) qs.set('pool', pool);
    return call<{ items: Array<any>; total: number }>(`/api/customer/credit/transactions?${qs}`);
  },

  rechargeSKUs: () => call<{
    items: Array<{
      retail_sku_id: string;
      retail_sku_version: number;
      sku_template_id?: number | null;
      override_id?: number | null;  // [2026-06-06 1:N 白标包] 白标包行带 override_id · 无 override 的规格行为 null
      sku_key: string;
      category: string;
      display_name: string;
      subtitle?: string | null;
      promo_text?: string | null;
      scene?: string | null;
      points_granted: number;
      retail_cents: number;
      source: 'configured_catalog';
    }>;
    service: {
      service_status: 'platform_managed';
      configuration_status: 'ready' | 'review_required' | 'unavailable';
      account_configured: boolean;
      dispute_pending: boolean;
    };
  }>('/api/customer/recharge/skus'),

  // 双 SSOT 报价请求与 legacy SKU 请求严格互斥,禁止同单混入两套价格字段。
  createSKURecharge: (body: CustomerRechargeRequest) => call<{
    success: boolean;
    data: {
      order_id: string;
      amount_yuan: number;
      base_points: number;
      bonus_points: number;
      total_points: number;
      tier_label: string;
      actual_channel: string;
      code_url?: string;
      payment_url_qrcode?: string;
      payment_url_mobile?: string;
      needs_openid?: boolean;
      status: string;
    };
  }>(CUSTOMER_RECHARGE_PRICING_ENTRY.order_endpoint, {
    method: 'POST',
    body: JSON.stringify(body.price_quote_id
      ? {
          ...buildQuotedOrderBody('customer-recharge', {
            priceQuoteId: body.price_quote_id,
            paymentMethod: body.payment_method || 'wechat',
            channel: body.channel,
            idempotencyKey: body.idempotency_key,
          }),
          terms_acceptance_id: body.terms_acceptance_id,
        }
      : {
          sku_template_id: body.sku_template_id,
          ...(body.override_id != null ? { override_id: body.override_id } : {}),
          retail_sku_id: body.retail_sku_id,
          retail_sku_version: body.retail_sku_version,
          payment_method: body.payment_method || 'wechat',
          channel: body.channel || 'auto',
          terms_acceptance_id: body.terms_acceptance_id,
        }),
  }),
};

export interface LegacyCustomerRechargeRequest {
  sku_template_id?: number;
  override_id?: number;
  retail_sku_id: string;
  retail_sku_version: number;
  price_quote_id?: never;
  idempotency_key?: never;
  channel?: string;
  payment_method?: string;
  terms_acceptance_id: string;
}

export interface QuotedCustomerRechargeRequest {
  price_quote_id: string;
  idempotency_key: string;
  sku_template_id?: never;
  override_id?: never;
  retail_sku_id?: never;
  retail_sku_version?: never;
  channel?: string;
  payment_method?: string;
  terms_acceptance_id: string;
}

export type CustomerRechargeRequest = LegacyCustomerRechargeRequest | QuotedCustomerRechargeRequest;

// ============================================================
// W4 Admin 治理
// ============================================================

export const adminW4Api = {
  disputes: (status?: string, limit = 50, offset = 0) => {
    const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (status) qs.set('status', status);
    return call<{ items: Array<any>; total: number }>(`/api/admin/binding-disputes?${qs}`);
  },

  resolveDispute: (id: number, body: { action: 'keep_old' | 'reassign' | 'reject'; admin_decision?: string; note: string }) =>
    call(`/api/admin/binding-disputes/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),

  auditRun: () => call('/api/admin/inventory-audit/run', { method: 'POST' }),

  auditHistory: (limit = 30, driftOnly = false) =>
    call<{ items: Array<any> }>(`/api/admin/inventory-audit/history?limit=${limit}&drift_only=${driftOnly}`),
};

export const formatCents = (cents: number): string => `¥${(cents / 100).toFixed(2)}`;
export const formatPoints = (p: number): string => p >= 10000 ? `${(p / 10000).toFixed(2)}万` : String(p);
