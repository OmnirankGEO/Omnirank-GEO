import { authFetch } from '@/lib/api';
import type { RefundCashJob, RefundMethod, RefundOrderPreview, RefundWorkOrder } from './types';

const BASE = '/api/wallet/refund-work-orders';

async function readJson<T>(response: Response): Promise<T> {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(
      (typeof detail === 'object' && detail?.message)
        || (typeof detail === 'string' ? detail : '')
        || data.message
        || `请求失败 (${response.status})`,
    );
  }
  return data as T;
}

export async function listRefundCashJobs(status?: string) {
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  const response = await authFetch(`/api/admin/dealer-resale/refund-cash-jobs${query}`);
  return readJson<{ items: RefundCashJob[]; total: number; attention_count: number }>(response);
}

export async function executeRefundCashJob(cashJobId: string, reason = '') {
  const response = await authFetch(
    `/api/admin/dealer-resale/refund-cash-jobs/${encodeURIComponent(cashJobId)}/execute`,
    { method: 'POST', body: JSON.stringify({ reason }) },
  );
  return readJson<{ refund_execution: { status: string; amount_cents: number; provider: string } }>(response);
}

export interface RefundWorkOrderPayload {
  source_order_id: string;
  refund_reason_category: string;
  refund_reason_detail: string;
  refund_method: RefundMethod;
  requested_refund_cents: number;
  customer_requested_at?: string;
  agent_confirmed_at?: string;
  process_note?: string;
}

export async function fetchRefundOrderPreview(query: string) {
  const response = await authFetch(`${BASE}/orders/preview?query=${encodeURIComponent(query)}`);
  return readJson<{ success: boolean } & RefundOrderPreview>(response);
}

export async function listRefundWorkOrders(status?: string) {
  const url = status && status !== 'start'
    ? `${BASE}?status=${encodeURIComponent(status)}`
    : BASE;
  const response = await authFetch(url);
  return readJson<{ success: boolean; items: RefundWorkOrder[]; counts: Record<string, number> }>(response);
}

export async function createRefundWorkOrderDraft(payload: RefundWorkOrderPayload) {
  const response = await authFetch(`${BASE}/drafts`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  return readJson<{ success: boolean; work_order: RefundWorkOrder }>(response);
}

export async function submitRefundWorkOrder(id: number, note = '') {
  const response = await authFetch(`${BASE}/${id}/submit`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  });
  return readJson<{ success: boolean; work_order: RefundWorkOrder }>(response);
}

export async function approveRefundWorkOrder(id: number, note = '') {
  const response = await authFetch(`${BASE}/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  });
  return readJson<{ success: boolean; work_order: RefundWorkOrder }>(response);
}

export async function executeRefundWorkOrder(id: number, note = '') {
  const response = await authFetch(`${BASE}/${id}/execute`, {
    method: 'POST',
    body: JSON.stringify({ confirm_execute: true, note }),
  });
  return readJson<{ success: boolean; work_order: RefundWorkOrder; execution_result?: unknown }>(response);
}

export async function completeRefundWorkOrder(id: number, note = '') {
  const response = await authFetch(`${BASE}/${id}/complete`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  });
  return readJson<{ success: boolean; work_order: RefundWorkOrder }>(response);
}

export async function uploadRefundAttachment(id: number, file: File, evidenceType: string) {
  const form = new FormData();
  form.append('evidence_type', evidenceType);
  form.append('file', file);
  const response = await authFetch(`${BASE}/${id}/attachments`, {
    method: 'POST',
    body: form,
  });
  return readJson<{ success: boolean; work_order: RefundWorkOrder }>(response);
}
