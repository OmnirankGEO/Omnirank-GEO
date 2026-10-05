export type RefundWorkOrderStatus =
  | 'draft'
  | 'submitted'
  | 'approved'
  | 'payout_pending'
  | 'completed'
  | 'rejected'
  | 'cancelled';

export type RefundMethod =
  | 'manual_wechat'
  | 'offline_transfer'
  | 'original_route_pending'
  | 'system_only';

export interface RefundOrderPreview {
  found: boolean;
  order: {
    id: string;
    customer_user_id?: number;
    customer_name?: string;
    customer_phone?: string;
    agent_user_id?: number;
    agent_name?: string;
    amount_cents: number;
    payment_status?: string;
    payment_method?: string;
    order_type?: string;
    paid_at?: string;
    created_at?: string;
    refund_status?: string | null;
  };
  impact: RefundImpact;
  checks: SystemCheck[];
  timeline: TimelineItem[];
}

export interface RefundImpact {
  original_amount_cents: number;
  estimated_refund_cents: number;
  used_power: number;
  refundable_power: number;
  customer_wallet_deduct_power: number;
  agent_revenue_reversal_cents: number;
  needs_manual_payout: boolean;
  risk_tips: string[];
}

export interface SystemCheck {
  key: string;
  label: string;
  status: 'pass' | 'warn' | 'fail' | string;
}

export interface TimelineItem {
  key: string;
  label: string;
  status: 'done' | 'pending' | string;
}

export interface RefundAttachment {
  id: number;
  work_order_id: number;
  file_url: string;
  file_name: string;
  file_type: 'image' | 'pdf' | string;
  evidence_type: string;
  mime_type?: string;
  file_size_bytes: number;
  uploaded_by?: number;
  uploaded_at?: string;
}

export interface RefundEvent {
  id: number;
  work_order_id: number;
  event_type: string;
  actor_user_id?: number;
  note?: string;
  payload?: Record<string, unknown>;
  created_at?: string;
}

export interface RefundWorkOrder {
  id: number;
  source_order_id: string;
  customer_user_id?: number;
  agent_user_id?: number;
  refund_reason_category: string;
  refund_reason_detail?: string;
  refund_method: RefundMethod;
  requested_refund_cents: number;
  estimated_refund_cents: number;
  refundable_power: number;
  customer_requested_at?: string;
  agent_confirmed_at?: string;
  status: RefundWorkOrderStatus;
  impact_snapshot?: RefundOrderPreview & Record<string, unknown>;
  payout_proof_url?: string;
  rejected_reason?: string;
  last_action_error?: string;
  created_at?: string;
  updated_at?: string;
  submitted_at?: string;
  reviewed_at?: string;
  executed_at?: string;
  completed_at?: string;
  attachments?: RefundAttachment[];
  events?: RefundEvent[];
}

export interface PendingEvidence {
  id: string;
  file: File;
  evidence_type: string;
  previewUrl?: string;
}

export type RefundCashJobStatus =
  | 'queued'
  | 'provider_processing'
  | 'completed'
  | 'failed'
  | 'manual_review';

export interface RefundCashJob {
  cash_job_id: string;
  case_id: string;
  source_order_id: string;
  consumer_user_id: number;
  responsible_service_user_id: number;
  amount_cents: number;
  original_payment_route: string;
  status: RefundCashJobStatus;
  attempt_count: number;
  idempotency_key: string;
  provider_refund_id?: string | null;
  last_error?: string | null;
  attention_reason?: string | null;
  created_at?: string;
  updated_at?: string;
  completed_at?: string | null;
}
