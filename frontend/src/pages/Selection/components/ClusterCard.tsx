import { useMemo, useState } from 'react';
import { motion } from 'framer-motion';
import { CoveredKeywordsFade } from './CoveredKeywordsFade';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { SUPER_RED_OCEAN_COPY, ZERO_PRICE_COPY } from '@/lib/wangjieTerminology';
import { getQuoteKeywordStatus, isPricedKeyword, quoteBlockingMessage, summarizeQuoteSelection } from '../utils/quoteAnnotation';

interface CoreKeyword {
  id: number;
  keyword: string;
  core_score: number;
  search_volume: number;
  is_selected: boolean;
  upgraded_from_covered?: boolean;
  entry?: { price: number; articles: number };
  standard?: { price: number; articles: number };
  flagship?: { price: number; articles: number };
  // 【§4.2】超级红海词:不出保证价 · 需单独报价 · 普通报价确认链路不成交
  super_red_ocean?: boolean;
  super_red_ocean_level?: string;
  competition_ratio?: number;
  needs_review?: boolean;
}

interface CoveredKeyword {
  keyword: string;
  source: string;
  upgradeable?: boolean;
  merge_reason?: string;
  variant_category?: string;
  id?: number;
  entry?: { price: number; articles: number };
  standard?: { price: number; articles: number };
  flagship?: { price: number; articles: number };
}

interface ClusterPricing {
  entry: { core_price: number; full_price: number; savings: number; core_articles: number };
  standard: { core_price: number; full_price: number; savings: number; core_articles: number };
  flagship: { core_price: number; full_price: number; savings: number; core_articles: number };
}

export interface ClusterData {
  cluster_name: string;
  business_tag: string;
  city_tag: string;
  scenario_tag: string;
  description: string;
  core_keywords: CoreKeyword[];
  covered_keywords: CoveredKeyword[];
  variant_count: number;
  core_keyword_count: number;
  covered_keyword_count: number;
  is_selected: boolean;
  pricing: ClusterPricing;
}

interface Props {
  cluster: ClusterData;
  index: number;
  activeTier: 'entry' | 'standard' | 'flagship';
  onToggleCluster: (clusterName: string) => void;
  onToggleCoreKeyword: (clusterName: string, keywordId: number) => void;
  onUpgradeCovered?: (clusterName: string, keyword: string) => void;
  onDemoteCore?: (clusterName: string, keywordId: number) => void;
}

const tierLabels: Record<string, string> = {
  entry: '入门',
  standard: '标准',
  flagship: '旗舰',
};

export function ClusterCard({
  cluster,
  index,
  activeTier,
  onToggleCluster,
  onToggleCoreKeyword,
  onUpgradeCovered,
  onDemoteCore,
}: Props) {
  const isSelected = cluster.is_selected;
  const pricing = cluster.pricing?.[activeTier];
  // [B3 2026-06-05] 全量价默认隐藏 · 主动点"查看全量价"才展开(不裸露全量价压低当前选择感知)
  const [showFullPrice, setShowFullPrice] = useState(false);
  const [confirmDialog, confirm] = useConfirmDialog();

  // Compute dynamic price & covered ratio based on selected core keywords
  const selectedCoreCount = cluster.core_keywords.filter(kw => kw.is_selected).length;
  const quoteSummary = useMemo(() => {
    return summarizeQuoteSelection(cluster.core_keywords, activeTier, kw => kw.is_selected);
  }, [cluster.core_keywords, activeTier]);
  const dynamicPrice = quoteSummary.totalPrice;
  const blockingMessage = quoteBlockingMessage(quoteSummary);

  // 【§4.2】标记超红海核心词前弹风险确认(仅选中时拦)
  const handleCoreClick = async (kw: CoreKeyword) => {
    if (kw.super_red_ocean && !kw.is_selected) {
      const ok = await confirm({
        title: SUPER_RED_OCEAN_COPY.confirmTitle(kw.keyword),
        description: SUPER_RED_OCEAN_COPY.confirmBody,
        confirmLabel: SUPER_RED_OCEAN_COPY.confirmOk,
        cancelLabel: SUPER_RED_OCEAN_COPY.confirmCancel,
        danger: true,
      });
      if (!ok) return;
    }
    onToggleCoreKeyword(cluster.cluster_name, kw.id);
  };

  const { effectiveCoveredKeywords, effectiveVariantCount } = useMemo(() => {
    const totalCorePrice = cluster.core_keywords.filter(kw => isPricedKeyword(kw, activeTier)).reduce((s, kw) => s + (kw[activeTier]?.price || 0), 0);
    const ratio = totalCorePrice > 0 ? dynamicPrice / totalCorePrice : 0;
    const count = Math.ceil(cluster.covered_keywords.length * ratio);
    return {
      effectiveCoveredKeywords: cluster.covered_keywords.slice(0, count),
      effectiveVariantCount: Math.ceil(cluster.variant_count * ratio),
    };
  }, [cluster.core_keywords, cluster.covered_keywords, cluster.variant_count, activeTier, dynamicPrice]);

  // savings = full_price - dynamic (customer-selected) price
  // [B3 2026-06-05] 加 dynamicPrice>0 守卫:0 选时不算 100%(根治立省 100% bug)· 立省%已不做主表达,仅留兜底
  const savingsAmount = pricing ? pricing.full_price - dynamicPrice : 0;
  const savingsPercent = pricing?.full_price && dynamicPrice > 0
    ? Math.round((savingsAmount / pricing.full_price) * 100)
    : 0;

  return (
    <>
    {confirmDialog}
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.08 }}
      className={`
        rounded-2xl md:rounded-3xl border-2 overflow-hidden transition-all duration-300
        ${isSelected
          ? 'bg-white border-blue-200 shadow-lg shadow-blue-50'
          : 'bg-gray-50/80 border-gray-100 opacity-60'
        }
      `}
    >
      {/* Cluster header */}
      <div
        className="flex items-center justify-between px-5 md:px-6 py-4 cursor-pointer"
        onClick={() => onToggleCluster(cluster.cluster_name)}
      >
        <div className="flex items-center gap-3 min-w-0">
          <span className="text-xl md:text-2xl">
            {index === 0 ? '🎯' : index === 1 ? '🔍' : index === 2 ? '📊' : '💡'}
          </span>
          <div className="min-w-0">
            <h3 className={`text-base md:text-lg font-bold truncate ${isSelected ? 'text-gray-900' : 'text-gray-500'}`}>
              {cluster.cluster_name}
            </h3>
            <p className="text-xs text-gray-400 mt-0.5">{cluster.description}</p>
          </div>
        </div>
        <div className={`
          px-3 py-1.5 rounded-full text-xs font-semibold transition-colors
          ${isSelected
            ? 'bg-blue-100 text-blue-700'
            : 'bg-gray-100 text-gray-400'
          }
        `}>
          {isSelected ? '已选' : '未选'}
        </div>
      </div>

      {/* Always show content — unselected clusters stay expanded so customer can reconsider */}
        <div className={`px-5 md:px-6 pb-5 ${!isSelected ? 'opacity-50' : ''}`}>
          {/* Core keywords with checkboxes */}
          <div className="mb-3">
            <p className="text-xs text-gray-500 font-medium mb-2 flex items-center gap-1.5">
              <span className="w-3.5 h-3.5 rounded bg-blue-100 text-blue-600 flex items-center justify-center text-[9px] font-bold">C</span>
              核心监控词（{selectedCoreCount}/{cluster.core_keywords.length}）
            </p>
            <div className="space-y-1">
              {cluster.core_keywords.map(kw => {
                const isSro = !!kw.super_red_ocean;
                const quoteStatus = getQuoteKeywordStatus(kw, activeTier);
                return (
                <div key={kw.id}>
                  <div
                    className={`
                      flex items-center gap-2.5 py-2 px-3 rounded-xl transition-all duration-200 group
                      ${kw.is_selected
                        ? 'bg-blue-50/50 hover:bg-blue-50'
                        : 'bg-gray-50 hover:bg-gray-100 opacity-60'
                      }
                    `}
                  >
                    <div
                      onClick={() => handleCoreClick(kw)}
                      className="flex items-center gap-2.5 flex-1 min-w-0 cursor-pointer"
                    >
                      <div className={`
                        w-5 h-5 rounded-md border-2 flex items-center justify-center shrink-0 transition-all
                        ${kw.is_selected
                          ? 'bg-blue-600 border-blue-600'
                          : 'border-gray-300 hover:border-blue-400'
                        }
                      `}>
                        {kw.is_selected && (
                          <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                          </svg>
                        )}
                      </div>
                      <span className={`text-sm truncate ${kw.is_selected ? 'text-gray-800' : 'text-gray-400'}`}>
                        {kw.keyword}
                      </span>
                      {isSro && (
                        <span className="text-[10px] text-red-600 bg-red-50 border border-red-100 px-1.5 py-0.5 rounded-full shrink-0 font-medium">
                          {SUPER_RED_OCEAN_COPY.badge}
                        </span>
                      )}
                    </div>
                    {kw.upgraded_from_covered && onDemoteCore && (
                      <button
                        onClick={(e) => { e.stopPropagation(); onDemoteCore(cluster.cluster_name, kw.id); }}
                        className="opacity-0 group-hover:opacity-100 text-[10px] text-orange-500 hover:text-orange-700 bg-orange-50 px-1.5 py-0.5 rounded transition-opacity shrink-0"
                      >
                        降为覆盖
                      </button>
                    )}
                    <span className="text-[11px] text-gray-300 tabular-nums shrink-0">
                      vol:{kw.search_volume}
                    </span>
                    {quoteStatus === 'needs_deep_quote' ? (
                      <span className="text-[11px] text-red-600 font-medium shrink-0">
                        {SUPER_RED_OCEAN_COPY.priceLabel}
                      </span>
                    ) : quoteStatus === 'priced' ? (
                      <span className={`text-sm tabular-nums font-medium shrink-0 ${
                        kw.is_selected ? 'text-blue-600' : 'text-gray-300 line-through'
                      }`}>
                        ¥{(kw[activeTier]?.price || 0).toLocaleString()}
                      </span>
                    ) : (
                      <span className="text-[11px] text-amber-600 font-medium shrink-0" title={ZERO_PRICE_COPY.hint}>
                        {ZERO_PRICE_COPY.label}
                      </span>
                    )}
                  </div>
                  {isSro && (
                    <p className="text-[11px] text-red-500/90 leading-relaxed px-3 pt-0.5 pb-1">
                      {SUPER_RED_OCEAN_COPY.warning}
                    </p>
                  )}
                </div>
                );
              })}
            </div>
          </div>

          {/* Covered keywords — proportional to selected core keywords */}
          {effectiveCoveredKeywords.length > 0 && (
            <CoveredKeywordsFade
              coveredKeywords={effectiveCoveredKeywords}
              variantCount={effectiveVariantCount}
              activeTier={activeTier}
              onUpgrade={onUpgradeCovered ? (kw) => onUpgradeCovered(cluster.cluster_name, kw) : undefined}
            />
          )}
          {effectiveCoveredKeywords.length === 0 && cluster.covered_keywords.length > 0 && (
            <p className="text-xs text-gray-300 mt-3 italic">选择核心词后展示相关搜索参考</p>
          )}

          {/* Pricing bar — [B3 2026-06-05] 立省% 不做主表达 · 显「已选 N / 可全量监控 M + 当前合计」· 全量价移 toggle */}
          <div className="mt-4 bg-linear-to-r from-blue-50 to-indigo-50 rounded-xl p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs text-gray-400 mb-0.5">{tierLabels[activeTier]}版</p>
                {blockingMessage ? (
                  <>
                    {dynamicPrice > 0 && (
                      <p className="text-lg md:text-xl font-bold text-blue-600 tabular-nums">
                        ¥{dynamicPrice.toLocaleString()}<span className="text-xs font-normal text-gray-400"> · 已计价部分</span>
                      </p>
                    )}
                    <p className="text-xs font-medium text-amber-600 mt-0.5">{blockingMessage}</p>
                  </>
                ) : dynamicPrice > 0 ? (
                  <>
                    <p className="text-lg md:text-xl font-bold text-blue-600 tabular-nums">
                      ¥{dynamicPrice.toLocaleString()}<span className="text-xs font-normal text-gray-400"> · 累计达标30天</span>
                    </p>
                    <p className="text-xs text-gray-400 mt-0.5">
                      已选 {selectedCoreCount} 个核心词 · 可全量监控 {cluster.core_keywords.length} 个
                    </p>
                  </>
                ) : (
                  <p className="text-sm font-medium text-gray-400">请选择核心词</p>
                )}
              </div>
              {/* 全量监控:默认不显全量价/差额 · 主动点才展开(不把全量价当 headline) */}
              {dynamicPrice > 0 && pricing?.full_price && cluster.core_keywords.length > selectedCoreCount && (
                <button
                  type="button"
                  onClick={() => setShowFullPrice(v => !v)}
                  className="text-right text-xs text-gray-400 hover:text-blue-500 transition-colors shrink-0"
                >
                  {showFullPrice ? (
                    <span className="tabular-nums">
                      全量监控 ¥{pricing.full_price.toLocaleString()}
                      <span className="block text-gray-300 mt-0.5">收起 ▲</span>
                    </span>
                  ) : (
                    <span>查看全量价 ›</span>
                  )}
                </button>
              )}
            </div>
          </div>
        </div>
    </motion.div>
    </>
  );
}
