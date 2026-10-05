import { motion } from 'framer-motion';

interface KeywordItem {
  id: number;
  keyword: string;
  category_label: string;
  difficulty: number;
  recommendation_reason?: string;
  intent?: string;
}

interface Props {
  keyword: KeywordItem;
  selected: boolean;
  onToggle: (id: number) => void;
  onHoverStart?: (id: number) => void;
  onHoverEnd?: (id: number) => void;
  disabled?: boolean;
  priceInfo?: { price: number; articles: number } | null;
}

const difficultyConfig: Record<number, { color: string; barColor: string; label: string }> = {
  1: { color: 'text-emerald-600', barColor: 'bg-emerald-400', label: '低' },
  2: { color: 'text-emerald-600', barColor: 'bg-emerald-400', label: '较低' },
  3: { color: 'text-amber-600', barColor: 'bg-amber-400', label: '中等' },
  4: { color: 'text-orange-600', barColor: 'bg-orange-400', label: '较高' },
  5: { color: 'text-red-600', barColor: 'bg-red-400', label: '高' },
};

export function KeywordCard({ keyword, selected, onToggle, onHoverStart, onHoverEnd, disabled, priceInfo }: Props) {
  const diff = difficultyConfig[keyword.difficulty] || difficultyConfig[3];

  return (
    <motion.div
      layout
      whileTap={disabled ? undefined : { scale: 0.98 }}
      onHoverStart={() => onHoverStart?.(keyword.id)}
      onHoverEnd={() => onHoverEnd?.(keyword.id)}
      onClick={() => !disabled && onToggle(keyword.id)}
      className={`
        relative rounded-2xl p-4 md:p-5 cursor-pointer transition-all duration-200 group
        ${selected
          ? 'bg-white border-2 border-blue-500 shadow-lg shadow-blue-100/50'
          : 'bg-white border-2 border-gray-100 hover:border-blue-200 shadow-xs hover:shadow-md'
        }
        ${disabled ? 'opacity-60 cursor-not-allowed' : ''}
      `}
    >
      {/* Left indicator bar */}
      {selected && (
        <motion.div
          initial={{ scaleY: 0 }}
          animate={{ scaleY: 1 }}
          className="absolute left-0 top-3 bottom-3 w-1 bg-linear-to-b from-blue-500 to-emerald-400 rounded-r-full"
        />
      )}

      <div className="flex items-start gap-3 pl-1">
        {/* Checkbox */}
        <div className={`
          mt-0.5 w-6 h-6 rounded-lg border-2 flex items-center justify-center shrink-0 transition-all duration-200
          ${selected
            ? 'bg-blue-600 border-blue-600 shadow-md shadow-blue-200'
            : 'border-gray-300 group-hover:border-blue-400'
          }
        `}>
          {selected && (
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

        <div className="flex-1 min-w-0">
          {/* Keyword text */}
          <p className={`text-[15px] md:text-base leading-snug font-medium ${selected ? 'text-gray-900' : 'text-gray-700'}`}>
            {keyword.keyword}
          </p>

          {/* Tags row */}
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <span className="inline-flex items-center text-xs px-2.5 py-1 rounded-full bg-blue-50 text-blue-600 font-medium">
              {keyword.category_label}
            </span>

            {keyword.intent && (
              <span className={`inline-flex items-center text-xs px-2 py-0.5 rounded-full font-medium ${
                keyword.intent === 'transactional' ? 'bg-emerald-50 text-emerald-600' :
                keyword.intent === 'navigational' ? 'bg-purple-50 text-purple-600' :
                'bg-gray-50 text-gray-500'
              }`}>
                {keyword.intent === 'transactional' ? '交易型' :
                 keyword.intent === 'navigational' ? '导航型' :
                 keyword.intent === 'informational' ? '信息型' : keyword.intent}
              </span>
            )}

            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-400">竞争度</span>
              <div className="flex gap-0.5">
                {Array.from({ length: 5 }).map((_, i) => (
                  <div
                    key={i}
                    className={`w-4 h-1.5 rounded-full transition-colors ${
                      i < keyword.difficulty ? diff.barColor : 'bg-gray-200'
                    }`}
                  />
                ))}
              </div>
              <span className={`text-xs font-medium ${diff.color}`}>{diff.label}</span>
            </div>
          </div>

          {/* Recommendation reason */}
          {keyword.recommendation_reason && (
            <p className="text-xs text-gray-400 mt-2 leading-relaxed">
              💡 {keyword.recommendation_reason}
            </p>
          )}

          {/* Price info */}
          {priceInfo && (
            <div className="flex items-center gap-4 mt-2.5 pt-2.5 border-t border-gray-100">
              <span className="text-sm text-blue-600 font-semibold">¥{priceInfo.price.toLocaleString()}</span>
              <span className="text-xs text-gray-400">{priceInfo.articles}篇文章</span>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}
