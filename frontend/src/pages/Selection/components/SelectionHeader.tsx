import { motion } from 'framer-motion';
import { BrandLogo } from '@/components/brand/BrandDisplay';
import type { ResolvedBrand } from '@/hooks/useWhitelabel';

interface SelectionContext {
  customer_persona: {
    type: string;
    description: string;
    search_behavior: string;
    decision_journey: string;
  };
  methodology: {
    steps: string[];
    summary: string;
  };
}

interface Props {
  /** v3.6 白标 · 客户页代理品牌(external_only 永远非空，loading 时 null) */
  brand: ResolvedBrand | null;
  brandName: string;
  phase: 'selection' | 'pricing' | 'confirmed';
  context?: SelectionContext;
}

export function SelectionHeader({ brand, brandName, phase, context }: Props) {
  const subtitle = phase === 'selection'
    ? '量身定制的关键词方案'
    : phase === 'pricing'
    ? 'AI 曝光优化方案'
    : '已确认方案';

  const instruction = phase === 'selection' ? '请勾选您希望优化的关键词' : '';

  return (
    <div className="relative overflow-hidden">
      {/* Background layers */}
      <div className="absolute inset-0 bg-linear-to-b from-blue-50 via-blue-50/60 to-transparent" />
      <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[600px] h-[300px] lg:w-[900px] lg:h-[400px] bg-linear-to-b from-blue-100/40 to-transparent rounded-full blur-3xl" />
      <div className="absolute -top-20 -right-20 w-64 h-64 bg-linear-to-br from-indigo-100/30 to-transparent rounded-full blur-2xl hidden md:block" />
      <div className="absolute -top-10 -left-20 w-48 h-48 bg-linear-to-br from-cyan-100/20 to-transparent rounded-full blur-2xl hidden md:block" />

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: 'easeOut' }}
        className="relative text-center pt-10 pb-4 px-5 md:pt-14 md:pb-6 lg:pt-16 lg:pb-8"
      >
        <div className="flex justify-center mb-6 md:mb-8">
          <BrandLogo brand={brand} size="lg" className="!h-12 md:!h-14 lg:!h-16" />
        </div>
        <p className="text-sm md:text-base text-gray-400 mb-1">为</p>
        <h1 className="text-2xl md:text-3xl lg:text-4xl font-bold text-gray-900 mb-2 md:mb-3 tracking-tight">
          「{brandName}」
        </h1>
        <p className="text-base md:text-lg text-gray-600">{subtitle}</p>
        {instruction && (
          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.3 }}
            className="text-sm md:text-base text-gray-400 mt-3"
          >
            {instruction}
          </motion.p>
        )}
      </motion.div>

      {/* Methodology & Persona Section */}
      {context && phase === 'selection' && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.4, duration: 0.5 }}
          className="relative max-w-lg md:max-w-2xl lg:max-w-3xl mx-auto px-5 pb-6 md:pb-8"
        >
          {/* Persona card */}
          <div className="bg-white/80 backdrop-blur-xs rounded-2xl border border-blue-100/60 shadow-xs p-4 md:p-5 mb-3">
            <div className="flex items-start gap-3">
              <div className="w-8 h-8 md:w-9 md:h-9 rounded-xl bg-blue-50 flex items-center justify-center shrink-0 mt-0.5">
                <svg className="w-4 h-4 md:w-5 md:h-5 text-blue-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs text-blue-500 font-semibold tracking-wide uppercase mb-1">目标客户画像</p>
                <p className="text-sm md:text-[15px] text-gray-700 leading-relaxed">
                  {context.customer_persona.description}
                </p>
                <div className="flex flex-wrap gap-x-4 gap-y-1.5 mt-2.5 text-xs text-gray-500">
                  <span className="flex items-center gap-1">
                    <svg className="w-3.5 h-3.5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    {context.customer_persona.search_behavior}
                  </span>
                  <span className="flex items-center gap-1">
                    <svg className="w-3.5 h-3.5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
                    </svg>
                    {context.customer_persona.decision_journey}
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* Methodology card */}
          <div className="bg-white/60 backdrop-blur-xs rounded-2xl border border-gray-100/60 p-4 md:p-5">
            <p className="text-xs text-gray-400 font-semibold tracking-wide uppercase mb-2.5">选词方法论</p>
            <div className="space-y-2">
              {context.methodology.steps.map((step, i) => (
                <div key={i} className="flex items-start gap-2.5">
                  <span className="w-5 h-5 rounded-full bg-blue-50 text-blue-500 flex items-center justify-center shrink-0 text-[10px] font-bold mt-0.5">
                    {i + 1}
                  </span>
                  <p className="text-xs md:text-sm text-gray-600 leading-relaxed">{step}</p>
                </div>
              ))}
            </div>
            <p className="text-xs text-gray-400 mt-3 leading-relaxed border-t border-gray-100 pt-2.5">
              {context.methodology.summary}
            </p>
          </div>
        </motion.div>
      )}
    </div>
  );
}
