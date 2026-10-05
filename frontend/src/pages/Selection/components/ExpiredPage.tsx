import { motion } from 'framer-motion';
import { BrandFooter } from '@/components/brand/BrandDisplay';
import type { ResolvedBrand } from '@/hooks/useWhitelabel';

interface Props {
  /** v3.6 白标 · 客户页代理品牌(external_only 永远非空，loading 时 null) */
  brand: ResolvedBrand | null;
}

export function ExpiredPage({ brand }: Props) {
  return (
    <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] bg-linear-to-b from-gray-50 to-white flex flex-col items-center justify-center px-5">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="max-w-lg md:max-w-xl w-full"
      >
        <div className="relative bg-white rounded-3xl shadow-lg shadow-gray-100/50 overflow-hidden">
          {/* Decorative top */}
          <div className="h-1.5 bg-linear-to-r from-gray-300 to-gray-200" />

          <div className="absolute top-0 right-0 w-32 h-32 md:w-44 md:h-44 bg-gray-50 rounded-full -translate-y-1/2 translate-x-1/2" />

          <div className="relative px-8 py-12 md:px-12 md:py-16 text-center">
            <div className="w-16 h-16 md:w-20 md:h-20 mx-auto mb-5 rounded-2xl bg-gray-100 flex items-center justify-center">
              <span className="text-3xl md:text-4xl">⏰</span>
            </div>

            <h2 className="text-xl md:text-2xl font-bold text-gray-900 mb-2 md:mb-3">链接已失效</h2>
            <p className="text-sm md:text-base text-gray-400 leading-relaxed">
              此链接已过期<br />请联系您的专属顾问获取新的链接
            </p>

            <div className="mt-8 md:mt-10 pt-6 border-t border-gray-100">
              <p className="text-xs md:text-sm text-gray-300">如有疑问，请联系您的专属顾问</p>
            </div>
          </div>
        </div>

        {/* v3.6 白标 · 客户页显示代理品牌(external_only 不回退平台) */}
        <div className="mt-8 opacity-60">
          <BrandFooter brand={brand} />
        </div>
      </motion.div>
    </div>
  );
}
