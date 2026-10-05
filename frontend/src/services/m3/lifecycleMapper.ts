/**
 * M3 Lifecycle Mapper
 *
 * 老板纠偏(2026-04-25):
 *   - 用真 quote.status 5 态(draft/confirmed/pending_payment/paid/archived)· 不用 'sent' 占位
 *   - sent_at 字段 quotes 表不存在 · 用 created_at + status 推断
 *   - confirmed_at / paid_at 用真值算 timer / risk
 *   - 注意:offline-mark-paid 后端会同时设 status='paid' + service_status='active'(3.5 步实情)
 *     UI 4 步独立显示 · 但每步实际触发的 endpoint 要明示
 *
 * 优先级链:
 *   续费(T-21) > 服务期激活后(stage 5/6/7/8) > 报价生命周期 > 诊断 > 询价
 */

import type {
  LifecycleStage,
  QuoteStatus,
  ServiceStatus,
  RiskLevel,
  ServiceActivationGate,
  BusinessTag,
} from './types';
import { RENEWAL_WINDOW_DAYS as RENEWAL_DAYS, STALLED_HOURS_THRESHOLD } from './config';

export interface LifecycleInput {
  brand: { id: number; brand_type?: 'self' | 'client'; created_at?: string };
  diagnosis?: { id: number; total_score: number; level: string; created_at: string } | null;
  quote?: {
    id: number;
    status: QuoteStatus;
    service_status?: ServiceStatus | null;
    service_start_date?: string | null;
    service_end_date?: string | null;
    confirmed_at?: string | null;
    paid_at?: string | null;
    amount?: number;
  } | null;
  articles?: {
    total: number;
    done: number;
    drafting?: number;
    pending?: number;
    published?: number;
  } | null;
  monitoring?: { running: boolean; has_anomaly?: boolean } | null;
  reportPublishedThisMonth?: boolean;
}

const RENEWAL_WINDOW_DAYS = RENEWAL_DAYS;

function daysUntil(dateString?: string | null): number | null {
  if (!dateString) return null;
  const target = new Date(dateString).getTime();
  if (Number.isNaN(target)) return null;
  return Math.ceil((target - Date.now()) / (1000 * 60 * 60 * 24));
}

function hoursSince(dateString?: string | null): number | null {
  if (!dateString) return null;
  const t = new Date(dateString).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.floor((Date.now() - t) / (1000 * 60 * 60)));
}

/**
 * 主推断函数
 */
export function inferStage(input: LifecycleInput): LifecycleStage {
  const { quote, articles, diagnosis, monitoring, reportPublishedThisMonth } = input;

  // 1. 续费窗(T-21 内服务到期)
  if (quote?.service_end_date) {
    const days = daysUntil(quote.service_end_date);
    if (days !== null && days >= 0 && days <= RENEWAL_WINDOW_DAYS) {
      return 9;
    }
  }

  // 老 quote 状态偶尔会停在 confirmed/pending_payment，但已有监测数据说明交付已进入监测期。
  // 旧版顶部桥接条必须以交付事实兜底，避免把已投放客户误导成“催付款”。
  if (monitoring?.running) {
    return 7;
  }

  // 2. 报价生命周期(用真 5 态推断)
  if (quote) {
    // 服务期已激活 → 进入交付阶段(stage 5-8)
    const serviceActive =
      quote.service_status === 'active' || quote.service_status === 'expiring';
    if (serviceActive || quote.status === 'paid') {
      return inferDeliveryStage({ articles, monitoring, reportPublishedThisMonth });
    }

    // 已确认 / 等付款(包括客户确认 + 代理线下确认 2 路径)
    if (quote.status === 'confirmed' || quote.status === 'pending_payment') {
      return 4;
    }

    // 草稿 / 关键词未确认
    if (quote.status === 'draft') {
      return 3;
    }

    // 归档
    if (quote.status === 'archived') {
      return 1;
    }
  }

  // 3. 有诊断但无 quote → 待报价
  if (diagnosis) {
    return 3;
  }

  // 4. 兜底 → 询价
  return 1;
}

/**
 * 服务期激活后 · 在 5/6/7/8 之间细分
 */
function inferDeliveryStage({
  articles,
  monitoring,
  reportPublishedThisMonth,
}: {
  articles?: LifecycleInput['articles'];
  monitoring?: LifecycleInput['monitoring'];
  reportPublishedThisMonth?: boolean;
}): LifecycleStage {
  if (reportPublishedThisMonth) return 8;
  const totalArticles = articles?.total ?? 0;
  const publishedArticles = articles?.published ?? 0;

  if (totalArticles === 0) return 5;
  if (publishedArticles < totalArticles) {
    return publishedArticles > 0 ? 6 : 5;
  }
  if (monitoring?.running) return 7;
  return 6;
}

/**
 * 服务期激活闸门(老板红线)
 *
 * 后端真实状态机(server.py 验证 2026-04-25):
 *   draft → confirmed (客户在 /s/:token 选词后走 /api/quotes/{id}/confirm)
 *   draft → pending_payment (代理走 /api/quotes/{id}/offline-confirm · 写 paid_amount + service_months)
 *   confirmed | pending_payment → paid (offline-mark-paid · 同时 service_status='active')
 *   paid + service-period (调整服务期起止)
 *
 * 4 步独立 audit log:
 *   - quoteConfirmed = status in (confirmed | pending_payment | paid)
 *   - paymentRecorded = status === 'paid'
 *   - servicePeriodActive = service_status === 'active' || 'expiring'
 *   - 注意:paid 与 servicePeriodActive 实际同步触发(后端合并)
 */
export function computeActivationGate(input: LifecycleInput): ServiceActivationGate {
  const q = input.quote;
  const quoteConfirmed = !!q && (
    q.status === 'confirmed' || q.status === 'pending_payment' || q.status === 'paid'
  );
  const paymentRecorded = !!q && q.status === 'paid';
  const servicePeriodActive = !!q && (
    q.service_status === 'active' || q.service_status === 'expiring'
  );

  let lockReason: string | undefined;
  if (!q) {
    lockReason = '尚未生成报价 · 先建立报价单';
  } else if (!quoteConfirmed) {
    lockReason = '报价还未确认 · 等客户在 /s/:token 完成选词 · 或代理走 offline-confirm';
  } else if (!paymentRecorded) {
    lockReason = '已确认 · 等收款(标记已收款会同时激活服务期)';
  } else if (!servicePeriodActive) {
    lockReason = '已收款但服务期状态异常 · 用调整服务期端点修正';
  }

  return {
    quoteConfirmed,
    paymentRecorded,
    servicePeriodActive,
    deliveryLocked: !servicePeriodActive,
    lockReason,
  };
}

/**
 * 推断风险等级(销售视角)
 */
export function inferRisk(input: LifecycleInput): RiskLevel {
  const q = input.quote;
  const stage = inferStage(input);

  if (stage === 9) return 'renewal';

  // stage 4 · 已确认未付款
  if (stage === 4 && q) {
    // 用 confirmed_at 算 stalled(超过 24h 未付款)
    const hours = hoursSince(q.confirmed_at);
    if (hours !== null && hours > STALLED_HOURS_THRESHOLD) return 'stalled';
    return 'ready';
  }

  // stage 3 · 等代理出报价 / 报价草稿
  if (stage === 3) return 'warming';

  return 'ok';
}

/**
 * timer 显示文本 · 用真 confirmed_at / service_end_date
 */
export function inferTimer(input: LifecycleInput): string | undefined {
  const q = input.quote;
  const stage = inferStage(input);

  if (stage === 9 && q?.service_end_date) {
    const days = daysUntil(q.service_end_date);
    if (days !== null) return `T-${Math.max(0, days)}`;
  }

  if (stage === 4 && q?.confirmed_at) {
    const hours = hoursSince(q.confirmed_at);
    if (hours !== null) return `${hours}h`;
  }

  return undefined;
}

/**
 * 业务标签 · 接口允许 UI 直接传 tag
 */
export function inferBusinessTag(
  input: LifecycleInput & { manualTag?: BusinessTag },
): BusinessTag | undefined {
  if (input.manualTag) return input.manualTag;
  const amount = input.quote?.amount ?? 0;
  if (amount >= 50000) return 'large';
  if (input.diagnosis?.total_score && input.diagnosis.total_score >= 80) return 'vip';
  return undefined;
}
