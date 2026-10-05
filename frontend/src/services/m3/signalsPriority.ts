/**
 * signalsPriority — 销售优先级事件加权 + why-now 文案 (CTO-C 2026-04-26)
 *
 * 老板红线:
 *   - 业务 stage 永远是排序主项 · 事件加权只决定"现在该不该跟"
 *   - 事件加分上限 +60 · 不能盖过业务状态本身
 *   - 失败诚实降级 (summary 拉不到 → 加权 0 · 不显示 hint)
 *   - 不自动骚扰客户 · 仅给代理推荐
 *
 * 4 条规则 (老板拍板):
 *   1. submitted_keywords + quote.status='draft' → +50 · "立即跟进收款"
 *   2. 24h 内二次打开 (report 或 quote) → +30 · "客户回看了,现在适合跟进"
 *   3. opened + saw_price + quote.status='draft' → +20 · "价格页已查看,建议发解释话术"
 *   4. portal opened + 服务期 <30 天 → +10 · "续费时机"
 *
 * 上限 +60 · 单客户最多取最高 1 条 hint 文案
 */

import type { QuoteStatus } from './types';
import { awaitConfirmedSessionToken } from '@/lib/authoritativeSession';

export interface SignalsSummary {
  per_source: Record<
    string,
    {
      counts: Record<string, number>;
      last_at: string | null;
    }
  >;
  latest_overall_at: string | null;
  opened_24h: Record<string, number>;
  reopened_24h: boolean;
}

export interface ClientSignalsContext {
  quoteStatus?: QuoteStatus | null;
  /** 服务期剩余天数 (snapshot.contract.days_to_expire) */
  daysToExpire?: number | null;
}

export interface PriorityResult {
  /** 事件加权 · 0 ~ +60 */
  bonus: number;
  /** 主推 hint 文案 · 仅一句 · 失败时为 null */
  whyNow: string | null;
  /** 命中规则 id (用于调试 / 未来 telemetry) */
  ruleId: 'submitted_draft' | 'reopened_24h' | 'saw_price_draft' | 'portal_renewal' | null;
}

const MAX_BONUS = 60;

export function computePriority(
  summary: SignalsSummary | null | undefined,
  ctx: ClientSignalsContext,
): PriorityResult {
  if (!summary) {
    return { bonus: 0, whyNow: null, ruleId: null };
  }

  const counts = (src: string, type: string): number =>
    summary.per_source?.[src]?.counts?.[type] || 0;

  const isDraft = ctx.quoteStatus === 'draft';

  // 规则 1 · 最优先 · submitted_keywords + draft
  const submittedSelection = counts('selection', 'submitted_keywords');
  if (submittedSelection > 0 && isDraft) {
    return {
      bonus: Math.min(50, MAX_BONUS),
      whyNow: '客户已提交选词 · 立即跟进收款',
      ruleId: 'submitted_draft',
    };
  }

  // 规则 2 · 24h 内二次打开 (report 或 quote 任意一个 ≥ 2)
  const opened24Report = summary.opened_24h?.public_report || 0;
  const opened24Quote = summary.opened_24h?.public_quote || 0;
  if (opened24Report >= 2 || opened24Quote >= 2) {
    return {
      bonus: Math.min(30, MAX_BONUS),
      whyNow: '客户 24 小时内回看了 · 现在适合跟进',
      ruleId: 'reopened_24h',
    };
  }

  // 规则 3 · saw_price + opened + draft
  const sawPrice = counts('public_quote', 'saw_price');
  const openedQuote = counts('public_quote', 'opened');
  if (sawPrice > 0 && openedQuote > 0 && isDraft) {
    return {
      bonus: Math.min(20, MAX_BONUS),
      whyNow: '客户已看到价格但未确认 · 建议发解释话术',
      ruleId: 'saw_price_draft',
    };
  }

  // 规则 4 · portal opened + 服务期 <30 天
  const openedPortal = counts('portal', 'opened');
  if (openedPortal > 0 && ctx.daysToExpire !== null && ctx.daysToExpire !== undefined && ctx.daysToExpire <= 30 && ctx.daysToExpire > -7) {
    return {
      bonus: Math.min(10, MAX_BONUS),
      whyNow: '客户在服务期末打开了门户 · 续费时机',
      ruleId: 'portal_renewal',
    };
  }

  return { bonus: 0, whyNow: null, ruleId: null };
}

/** 拉聚合摘要 · 失败返 null (UI 据此显示 "信号暂不可用") */
export async function fetchSignalsSummary(brandId: number): Promise<SignalsSummary | null> {
  try {
    const res = await fetch(`/api/m3/customer-events/summary?brand_id=${brandId}&days=30`, {
      credentials: 'same-origin',
      headers: await tokenHeader(),
    });
    if (!res.ok) return null;
    const data = await res.json();
    if (!data?.success) return null;
    return {
      per_source: data.per_source || {},
      latest_overall_at: data.latest_overall_at || null,
      opened_24h: data.opened_24h || {},
      reopened_24h: !!data.reopened_24h,
    };
  } catch {
    return null;
  }
}

/** 拉时间序事件 · 失败返 null (UI 据此显示"信号暂不可用") */
export interface CustomerEventRow {
  id: number;
  brand_id: number | null;
  quote_id: number | null;
  diagnosis_id: number | null;
  source: string;
  event_type: string;
  metadata: Record<string, unknown>;
  occurred_at: string;
  created_at: string;
}

export async function fetchSignalsEvents(
  brandId: number,
  opts?: { quoteId?: number; days?: number; limit?: number },
): Promise<CustomerEventRow[] | null> {
  try {
    const params = new URLSearchParams();
    params.set('brand_id', String(brandId));
    if (opts?.quoteId != null) params.set('quote_id', String(opts.quoteId));
    params.set('days', String(opts?.days ?? 30));
    params.set('limit', String(opts?.limit ?? 50));
    const res = await fetch(`/api/m3/customer-events?${params.toString()}`, {
      credentials: 'same-origin',
      headers: await tokenHeader(),
    });
    if (!res.ok) return null;
    const data = await res.json();
    if (!data?.success) return null;
    return Array.isArray(data.events) ? (data.events as CustomerEventRow[]) : [];
  } catch {
    return null;
  }
}

// [BUG-3] 改 async:会话在途时等待确认,不再把"慢"抛成异常炸掉调用方页面。
// fail-closed 不变 —— 未确认的 token 永远不会进 Authorization 头。
async function tokenHeader(): Promise<HeadersInit> {
  const token = await awaitConfirmedSessionToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
