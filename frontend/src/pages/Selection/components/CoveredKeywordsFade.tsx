import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';

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

interface Props {
  coveredKeywords: CoveredKeyword[];
  variantCount: number;
  activeTier?: 'entry' | 'standard' | 'flagship';
  onUpgrade?: (keyword: string) => void;
}

const VISIBLE_COUNT = 4;

export function CoveredKeywordsFade({ coveredKeywords, variantCount, activeTier = 'entry', onUpgrade }: Props) {
  const [expanded, setExpanded] = useState(false);

  const realKeywords = coveredKeywords.filter(kw => kw.source !== 'generated_variant');
  const variants = coveredKeywords.filter(kw => kw.source === 'generated_variant');

  const displayList = expanded
    ? [...realKeywords, ...variants.slice(0, 6)]
    : [...realKeywords, ...variants].slice(0, VISIBLE_COUNT);

  const totalCovered = realKeywords.length + variantCount;
  const hiddenCount = totalCovered - displayList.length;

  return (
    <div className="mt-3">
      <p className="text-xs text-gray-400 font-medium mb-2 flex items-center gap-1.5">
        <span className="w-3.5 h-3.5 rounded bg-gray-100 flex items-center justify-center text-[9px]">+</span>
        相关搜索参考 {totalCovered} 个 · 发核心词内容时顺带覆盖 · 不单独监测
      </p>

      <div className="relative">
        <div className="space-y-0.5">
          <AnimatePresence>
            {displayList.map((kw, i) => {
              const isVariant = kw.source === 'generated_variant';
              // Gradient opacity: real keywords full opacity, variants fade
              const realCount = realKeywords.length;
              const opacityIndex = isVariant ? i - realCount : -1;
              const opacity = isVariant
                ? Math.max(0.25, 1 - opacityIndex * 0.2)
                : 1;
              // [2026-06-07 老板 Option A] 同义覆盖词:原价划线 → 免费(营销「捡便宜」感);变体词无独立价不显
              const tier = !isVariant ? kw[activeTier] : undefined;
              const tierPrice = tier?.price ?? 0;

              return (
                <motion.div
                  key={kw.keyword}
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  className="flex items-center gap-2 py-1 px-2 rounded-lg group"
                  style={{ opacity: expanded ? 1 : opacity }}
                >
                  <span className="w-1 h-1 rounded-full bg-gray-300 shrink-0" />
                  <span className={`text-xs flex-1 truncate ${
                    isVariant ? 'text-gray-300' : 'text-gray-500'
                  }`}>
                    {kw.keyword}
                  </span>
                  {/* [2026-06-07 Option A] 同义覆盖词原价划线→免费;变体/无价词回退「可能覆盖」 */}
                  {tierPrice > 0 ? (
                    <span className="text-[10px] shrink-0 whitespace-nowrap">
                      <span className="text-gray-300 line-through mr-1">¥{tierPrice}</span>
                      <span className="text-green-600 font-semibold">免费</span>
                    </span>
                  ) : (
                    <span className="text-[10px] text-gray-300 font-medium shrink-0">可能覆盖</span>
                  )}
                  {kw.upgradeable && onUpgrade && (
                    <button
                      onClick={(e) => { e.stopPropagation(); onUpgrade(kw.keyword); }}
                      className="opacity-0 group-hover:opacity-100 text-[10px] text-blue-500 hover:text-blue-700 bg-blue-50 px-1.5 py-0.5 rounded transition-opacity shrink-0"
                    >
                      升为核心
                    </button>
                  )}
                </motion.div>
              );
            })}
          </AnimatePresence>
        </div>

        {/* Gradient fade mask when collapsed */}
        {!expanded && hiddenCount > 0 && (
          <div className="absolute bottom-0 left-0 right-0 h-10 bg-linear-to-t from-white to-transparent pointer-events-none" />
        )}
      </div>

      {/* Expand/collapse toggle */}
      {hiddenCount > 0 && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="mt-1 text-xs text-blue-500 hover:text-blue-700 transition-colors flex items-center gap-1"
        >
          {expanded ? (
            <>收起</>
          ) : (
            <>以及 {hiddenCount} 个长尾搜索变体 &rarr;</>
          )}
        </button>
      )}

      {expanded && hiddenCount > 0 && variantCount > variants.slice(0, 6).length && (
        <p className="mt-1 text-[10px] text-gray-300">
          ... 以及更多 {variantCount - 6} 个长尾变体词
        </p>
      )}

      {/* [2026-06-07 Option A · 合规免责] 划线免费需配免责小字防退款:顺带覆盖 · 不单独监测 · 不承诺达标 */}
      {realKeywords.length > 0 && (
        <p className="mt-1.5 text-[10px] text-gray-300 leading-snug">
          顺带覆盖 · 不单独监测 · 不承诺达标
        </p>
      )}
    </div>
  );
}
