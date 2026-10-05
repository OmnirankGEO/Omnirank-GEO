export type QuoteTier = 'entry' | 'standard' | 'flagship';

export interface QuotePricedKeyword {
  id: number;
  super_red_ocean?: boolean;
  should_quote?: boolean;  // [任务3 2026-06-08] LLM-first:false=信息型不报价(前端显「信息型·不报价」· 不阻塞发送)
  entry?: { price?: number | null };
  standard?: { price?: number | null };
  flagship?: { price?: number | null };
}

export type QuoteKeywordStatus = 'priced' | 'needs_deep_quote' | 'needs_manual_price' | 'info_only';

export interface QuoteSelectionSummary {
  selectedCount: number;
  pricedCount: number;
  deepQuoteCount: number;
  manualPriceCount: number;
  infoOnlyCount: number;  // [任务3] 信息型不报价词数(不阻塞发送)
  totalPrice: number;
  canConfirm: boolean;
}

export function getTierPrice(keyword: QuotePricedKeyword, tier: QuoteTier): number {
  const raw = keyword[tier]?.price;
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : 0;
}

export function getQuoteKeywordStatus(keyword: QuotePricedKeyword, tier: QuoteTier): QuoteKeywordStatus {
  if (keyword.super_red_ocean) return 'needs_deep_quote';
  if (keyword.should_quote === false) return 'info_only';  // [任务3] 信息型 · 不报价(不裸 ¥0 · 不阻塞发送)
  return getTierPrice(keyword, tier) > 0 ? 'priced' : 'needs_manual_price';
}

export function isPricedKeyword(keyword: QuotePricedKeyword, tier: QuoteTier): boolean {
  return getQuoteKeywordStatus(keyword, tier) === 'priced';
}

export function summarizeQuoteSelection<T extends QuotePricedKeyword>(
  keywords: T[],
  tier: QuoteTier,
  isSelected: (keyword: T) => boolean,
): QuoteSelectionSummary {
  let selectedCount = 0;
  let pricedCount = 0;
  let deepQuoteCount = 0;
  let manualPriceCount = 0;
  let infoOnlyCount = 0;
  let totalPrice = 0;

  for (const keyword of keywords) {
    if (!isSelected(keyword)) continue;
    selectedCount += 1;
    const status = getQuoteKeywordStatus(keyword, tier);
    if (status === 'priced') {
      pricedCount += 1;
      totalPrice += getTierPrice(keyword, tier);
    } else if (status === 'needs_deep_quote') {
      deepQuoteCount += 1;
    } else if (status === 'info_only') {
      infoOnlyCount += 1;  // [任务3] 信息型不报价 · 不计入需处理 · 不阻塞发送
    } else {
      manualPriceCount += 1;
    }
  }

  return {
    selectedCount,
    pricedCount,
    deepQuoteCount,
    manualPriceCount,
    infoOnlyCount,
    totalPrice,
    // [任务3] 信息型词(info_only)不报价但不阻塞发送(计入 priced+info 即满足)· 超红海/需核价仍需处理
    canConfirm: selectedCount > 0 && (pricedCount + infoOnlyCount) === selectedCount && totalPrice > 0,
  };
}

export function quoteBlockingMessage(summary: QuoteSelectionSummary): string {
  const parts: string[] = [];
  if (summary.deepQuoteCount > 0) parts.push(`${summary.deepQuoteCount} 个需深度报价`);
  if (summary.manualPriceCount > 0) parts.push(`${summary.manualPriceCount} 个需核价`);
  if (parts.length === 0) return '';
  return `含 ${parts.join('、')}，请先移除或联系 OmniRank 平台协助处理`;
}

export function quoteSummaryText(
  summary: QuoteSelectionSummary,
  pricedPrefix = '合计',
): string {
  const blocking = quoteBlockingMessage(summary);
  if (blocking) {
    const priced = summary.pricedCount > 0 ? `已计价 ${summary.pricedCount} 个 · ¥${summary.totalPrice.toLocaleString()} · ` : '';
    return `${priced}${blocking}`;
  }
  if (summary.totalPrice > 0) return `${pricedPrefix} ¥${summary.totalPrice.toLocaleString()}`;
  return '请选择可报价的核心词';
}
