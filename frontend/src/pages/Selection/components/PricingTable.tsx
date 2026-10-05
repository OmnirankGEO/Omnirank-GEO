import { useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { SUPER_RED_OCEAN_COPY, ZERO_PRICE_COPY } from '@/lib/wangjieTerminology';
import { getQuoteKeywordStatus, quoteBlockingMessage, summarizeQuoteSelection } from '../utils/quoteAnnotation';

interface PricingKeyword {
  id: number;
  keyword: string;
  category_label: string;
  recommendation_reason?: string;
  entry: { price: number; articles: number };
  standard: { price: number; articles: number };
  flagship: { price: number; articles: number };
  // 【§4.2】超级红海词:不出保证价 · 需单独报价 · 普通报价确认链路不成交
  super_red_ocean?: boolean;
  super_red_ocean_level?: string;
  competition_ratio?: number;
  needs_review?: boolean;
}

interface Props {
  keywords: PricingKeyword[];
  activeTier: string;
  checkedIds: Set<number>;
  onToggle: (id: number) => void;
  pendingKeywords?: string[];
}

export function PricingTable({ keywords, activeTier, checkedIds, onToggle, pendingKeywords = [] }: Props) {
  const tier = activeTier as 'entry' | 'standard' | 'flagship';
  const [confirmDialog, confirm] = useConfirmDialog();

  const quoteSummary = useMemo(() => {
    return summarizeQuoteSelection(keywords, tier, kw => checkedIds.has(kw.id));
  }, [keywords, tier, checkedIds]);
  const blockingMessage = quoteBlockingMessage(quoteSummary);

  // 【§4.2】标记超红海词前弹风险确认(仅选中时拦 · 移除不拦)
  const handleRowClick = async (kw: PricingKeyword) => {
    if (kw.super_red_ocean && !checkedIds.has(kw.id)) {
      const ok = await confirm({
        title: SUPER_RED_OCEAN_COPY.confirmTitle(kw.keyword),
        description: SUPER_RED_OCEAN_COPY.confirmBody,
        confirmLabel: SUPER_RED_OCEAN_COPY.confirmOk,
        cancelLabel: SUPER_RED_OCEAN_COPY.confirmCancel,
        danger: true,
      });
      if (!ok) return;
    }
    onToggle(kw.id);
  };

  return (
    <>
      {confirmDialog}
      <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-2">
        <div className="bg-white rounded-2xl md:rounded-3xl border border-gray-100 shadow-xs overflow-hidden">
          {/* Header */}
          <div className="px-5 md:px-6 py-3 md:py-4 bg-linear-to-r from-gray-50 to-white border-b border-gray-100">
            <div className="flex items-center justify-between">
              <h4 className="text-sm md:text-base font-semibold text-gray-700">关键词报价明细</h4>
              <span className="text-xs md:text-sm text-gray-400">{keywords.length} 个关键词</span>
            </div>
          </div>

          {/* Column headers */}
          <div className="grid grid-cols-[auto_1fr_80px] md:grid-cols-[auto_1fr_100px] gap-2 md:gap-3 px-5 md:px-6 py-2.5 md:py-3 bg-gray-50/50 text-[11px] md:text-xs text-gray-400 font-medium uppercase tracking-wider border-b border-gray-100">
            <span className="w-6" />
            <span>关键词</span>
            <span className="text-right">费用</span>
          </div>

          {/* Keyword rows */}
          <div className="divide-y divide-gray-50">
            <AnimatePresence>
              {keywords.map(kw => {
                const checked = checkedIds.has(kw.id);
                const tierData = kw[tier] || { price: 0, articles: 0 };
                const isSro = !!kw.super_red_ocean;
                const quoteStatus = getQuoteKeywordStatus(kw, tier);

                return (
                  <motion.div
                    key={kw.id}
                    layout
                    onClick={() => handleRowClick(kw)}
                    className={`
                      grid grid-cols-[auto_1fr_80px] md:grid-cols-[auto_1fr_100px] gap-2 md:gap-3 px-5 md:px-6 py-3.5 md:py-4 cursor-pointer transition-all duration-200
                      ${checked ? 'bg-white hover:bg-blue-50/30' : 'bg-gray-50/50 opacity-50 hover:opacity-70'}
                    `}
                  >
                    <div className={`
                      w-6 h-6 rounded-lg border-2 flex items-center justify-center shrink-0 transition-all duration-200 mt-0.5
                      ${checked
                        ? 'bg-blue-600 border-blue-600 shadow-xs shadow-blue-200'
                        : 'border-gray-300 hover:border-blue-400'
                      }
                    `}>
                      {checked && (
                        <motion.svg
                          initial={{ scale: 0 }}
                          animate={{ scale: 1 }}
                          className="w-3.5 h-3.5 text-white"
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                          strokeWidth={3}
                        >
                          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                        </motion.svg>
                      )}
                    </div>

                    <div className="min-w-0">
                      <p className={`text-sm md:text-base font-medium truncate ${checked ? 'text-gray-800' : 'text-gray-500'}`}>
                        {kw.keyword}
                      </p>
                      {isSro ? (
                        <span className="text-[11px] md:text-xs text-red-600 bg-red-50 border border-red-100 px-2 py-0.5 rounded-full inline-block mt-1 font-medium">
                          {SUPER_RED_OCEAN_COPY.badge}
                        </span>
                      ) : quoteStatus === 'info_only' ? (
                        <span className="text-[11px] md:text-xs text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full inline-block mt-1 font-medium">
                          信息型 · 不报价
                        </span>
                      ) : (
                        <span className="text-[11px] md:text-xs text-gray-300 bg-gray-50 px-2 py-0.5 rounded-full inline-block mt-1">
                          {kw.category_label}
                        </span>
                      )}
                      {isSro ? (
                        <p className="text-[11px] md:text-xs text-red-500/90 mt-1 leading-relaxed">
                          {SUPER_RED_OCEAN_COPY.warning}
                        </p>
                      ) : kw.recommendation_reason ? (
                        <p className="text-[11px] md:text-xs text-gray-400 mt-1 leading-relaxed">
                          💡 {kw.recommendation_reason}
                        </p>
                      ) : null}
                    </div>

                    {quoteStatus === 'needs_deep_quote' ? (
                      <p className="text-[11px] md:text-xs text-right tabular-nums self-center text-red-600 font-medium leading-tight">
                        {SUPER_RED_OCEAN_COPY.priceLabel}
                      </p>
                    ) : quoteStatus === 'info_only' ? (
                      <p className="text-[11px] md:text-xs text-right self-center text-gray-400 font-medium leading-tight">
                        信息型 · 不报价
                      </p>
                    ) : quoteStatus === 'priced' ? (
                      <p className={`text-sm md:text-base text-right tabular-nums self-center ${checked ? 'text-blue-600 font-semibold' : 'text-gray-400 line-through'}`}>
                        ¥{tierData.price.toLocaleString()}
                      </p>
                    ) : (
                      <p className="text-[11px] md:text-xs text-right self-center text-amber-600 font-medium leading-tight" title={ZERO_PRICE_COPY.hint}>
                        {ZERO_PRICE_COPY.label}
                      </p>
                    )}
                  </motion.div>
                );
              })}
            </AnimatePresence>

            {/* Pending keywords */}
            {pendingKeywords.map((kw, i) => (
              <div
                key={`pending-${i}`}
                className="grid grid-cols-[auto_1fr_80px] md:grid-cols-[auto_1fr_100px] gap-2 md:gap-3 px-5 md:px-6 py-3.5 md:py-4 bg-amber-50/30"
              >
                <div className="w-6 h-6 rounded-lg border-2 border-dashed border-amber-300 flex items-center justify-center">
                  <span className="text-[10px] text-amber-500 font-bold">?</span>
                </div>
                <div>
                  <p className="text-sm md:text-base text-gray-700 font-medium">{kw}</p>
                  <span className="text-[11px] md:text-xs text-amber-500 bg-amber-50 px-2 py-0.5 rounded-full inline-block mt-1">
                    待评估
                  </span>
                </div>
                <p className="text-sm md:text-base text-right text-gray-300 self-center">—</p>
              </div>
            ))}
          </div>

          {/* Footer totals */}
          <div className="px-5 md:px-6 py-4 md:py-5 bg-linear-to-r from-blue-50 to-indigo-50 border-t border-blue-100">
            <div className="flex items-center justify-between">
              <div>
                <span className="text-sm md:text-base text-gray-600 font-medium">
                  已选 <span className="text-blue-600 font-bold">{quoteSummary.selectedCount}</span> / {keywords.length} 词
                </span>
              </div>
              <div className="text-right">
                <p className="text-xs md:text-sm text-gray-400">合计费用 · 累计达标30天</p>
                {blockingMessage ? (
                  <>
                    {quoteSummary.totalPrice > 0 && (
                      <p className="text-base md:text-lg font-bold text-blue-600">¥{quoteSummary.totalPrice.toLocaleString()} · 已计价部分</p>
                    )}
                    <p className="text-xs md:text-sm font-medium text-amber-600 max-w-[220px]">{blockingMessage}</p>
                  </>
                ) : quoteSummary.totalPrice > 0 ? (
                  <p className="text-lg md:text-xl lg:text-2xl font-bold text-blue-600">¥{quoteSummary.totalPrice.toLocaleString()}</p>
                ) : (
                  <p className="text-sm md:text-base font-medium text-gray-400">请选择可报价的核心词</p>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
