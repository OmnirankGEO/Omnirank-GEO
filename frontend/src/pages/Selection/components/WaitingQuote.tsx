import { motion } from 'framer-motion';

interface KeywordItem {
  id: number;
  keyword: string;
  category_label: string;
}

interface Props {
  selectedKeywords: KeywordItem[];
  customKeywords: string[];
  onWithdraw?: () => void;
  withdrawing: boolean;
  reviewPending?: boolean;
}

export function WaitingQuote({ selectedKeywords, customKeywords, onWithdraw, withdrawing, reviewPending }: Props) {
  const total = selectedKeywords.length + customKeywords.length;

  return (
    <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-8">
      {/* Desktop: side-by-side, Mobile: stacked */}
      <div className="flex flex-col lg:flex-row lg:gap-6">
        {/* Status card */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="relative bg-linear-to-br from-blue-50 to-indigo-50 rounded-2xl p-8 md:p-10 text-center mb-6 lg:mb-0 lg:flex-1 overflow-hidden"
        >
          <div className="absolute top-0 right-0 w-32 h-32 bg-blue-100/50 rounded-full -translate-y-1/2 translate-x-1/2" />
          <div className="absolute bottom-0 left-0 w-24 h-24 bg-indigo-100/50 rounded-full translate-y-1/2 -translate-x-1/2" />

          <div className="relative">
            <div className="w-16 h-16 md:w-20 md:h-20 mx-auto mb-4 rounded-2xl bg-white shadow-lg shadow-blue-100 flex items-center justify-center">
              <span className="text-3xl md:text-4xl">⏳</span>
            </div>
            <h3 className="text-xl md:text-2xl font-bold text-gray-900 mb-2">
              {/* Phase E (CTO-15.11):文案友好化
                  老板痛点(/s/ 重访 5 小时后):"5 小时前点的现在还在等 5 分钟?"
                  改:不再写"已提交" + "5 分钟" 死时间 · 用"已确认 + 顾问会出方案" */}
              {reviewPending ? '报价审核中' : '业务方向已确认'}
            </h3>
            <p className="text-sm md:text-base text-gray-500 leading-relaxed">
              {reviewPending
                ? <>报价方案已生成，顾问正在为您<br />审核优化价格</>
                : <>您的需求已收到 · 顾问正在为您<br />定制专属报价方案</>
              }
            </p>

            <div className="mt-4 inline-flex items-center gap-2 px-4 py-2 bg-white/80 rounded-full text-xs md:text-sm text-gray-500">
              <span className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
              {reviewPending ? '审核完成后将自动展示报价' : '完成后将通过链接通知您'}
            </div>

            {onWithdraw && (
              <div className="mt-6">
                <button
                  onClick={onWithdraw}
                  disabled={withdrawing}
                  className="text-sm text-blue-500 hover:text-blue-600 font-medium disabled:opacity-50 transition-colors"
                >
                  {withdrawing ? '修改中...' : '← 我想改一下选择'}
                </button>
              </div>
            )}
          </div>
        </motion.div>

        {/* Selected keywords list */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.15 }}
          className="bg-white rounded-2xl border border-gray-100 shadow-xs overflow-hidden lg:flex-1"
        >
          <div className="px-5 py-4 border-b border-gray-50">
            <h4 className="text-sm md:text-base font-semibold text-gray-700">已选关键词</h4>
            <p className="text-xs md:text-sm text-gray-400 mt-0.5">共 {total} 个</p>
          </div>
          <div className="divide-y divide-gray-50 max-h-[60vh] lg:max-h-[50vh] overflow-y-auto">
            {selectedKeywords.map(kw => (
              <div key={kw.id} className="flex items-center gap-3 px-5 py-3">
                <span className="w-5 h-5 rounded-full bg-emerald-50 text-emerald-500 flex items-center justify-center shrink-0">
                  <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                </span>
                <span className="text-sm md:text-base text-gray-700 flex-1">{kw.keyword}</span>
                <span className="text-xs text-gray-300 bg-gray-50 px-2 py-0.5 rounded-full">{kw.category_label}</span>
              </div>
            ))}
            {customKeywords.map((kw, i) => (
              <div key={`custom-${i}`} className="flex items-center gap-3 px-5 py-3">
                <span className="w-5 h-5 rounded-full bg-amber-50 text-amber-500 flex items-center justify-center shrink-0">
                  <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                  </svg>
                </span>
                <span className="text-sm md:text-base text-gray-700 flex-1">{kw}</span>
                <span className="text-xs text-amber-500 bg-amber-50 px-2 py-0.5 rounded-full">自定义</span>
              </div>
            ))}
          </div>
        </motion.div>
      </div>
    </div>
  );
}
