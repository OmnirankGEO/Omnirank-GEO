import { useEffect } from 'react';
import { motion } from 'framer-motion';
import confetti from 'canvas-confetti';
import { BrandFooter } from '@/components/brand/BrandDisplay';
import type { ResolvedBrand } from '@/hooks/useWhitelabel';
import { describeProbability } from '@/lib/probability';

interface Props {
  /** v3.6 白标 · 客户页代理品牌(external_only 永远非空，loading 时 null) */
  brand: ResolvedBrand | null;
  brandName: string;
  tierLabel: string;
  aiProbability: string;
  keywordCount: number;
  totalArticles: number;
  totalPrice: number;
  clusterCount?: number;
  /** Phase B (CTO-15.11):激活后客户门户 token · 给"查看数据看板"按钮 */
  portalToken?: string;
}

export function ConfirmCelebration({ brand, brandName, tierLabel, aiProbability, keywordCount, totalArticles, totalPrice, clusterCount, portalToken }: Props) {
  useEffect(() => {
    const duration = 3000;
    const end = Date.now() + duration;
    const colors = ['#2563EB', '#F59E0B', '#10B981', '#8B5CF6'];

    const frame = () => {
      confetti({
        particleCount: 3,
        angle: 60,
        spread: 55,
        origin: { x: 0, y: 0.7 },
        colors,
      });
      confetti({
        particleCount: 3,
        angle: 120,
        spread: 55,
        origin: { x: 1, y: 0.7 },
        colors,
      });
      if (Date.now() < end) requestAnimationFrame(frame);
    };
    frame();
  }, []);

  return (
    <div className="min-h-[100dvh] pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] bg-linear-to-b from-blue-50 via-white to-indigo-50 flex flex-col items-center justify-center px-5 py-8">
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={{ duration: 0.6, ease: 'easeOut' }}
        className="max-w-lg md:max-w-xl w-full"
      >
        {/* Main card */}
        <div className="relative bg-white rounded-3xl shadow-xl shadow-blue-100/50 overflow-hidden">
          {/* Top decorative bar */}
          <div className="h-2 bg-linear-to-r from-blue-500 via-emerald-400 to-amber-400" />

          {/* Decorative circles */}
          <div className="absolute top-0 right-0 w-40 h-40 md:w-56 md:h-56 bg-blue-50/50 rounded-full -translate-y-1/2 translate-x-1/2" />
          <div className="absolute bottom-0 left-0 w-32 h-32 md:w-44 md:h-44 bg-indigo-50/50 rounded-full translate-y-1/2 -translate-x-1/2" />

          <div className="relative px-8 py-10 md:px-12 md:py-14 text-center">
            <motion.div
              initial={{ scale: 0 }}
              animate={{ scale: 1 }}
              transition={{ type: 'spring', stiffness: 200, delay: 0.2 }}
              className="w-20 h-20 md:w-24 md:h-24 mx-auto mb-5 md:mb-6 rounded-2xl bg-linear-to-br from-blue-500 to-indigo-600 shadow-lg shadow-blue-200 flex items-center justify-center"
            >
              <span className="text-4xl md:text-5xl">🎊</span>
            </motion.div>

            <h2 className="text-2xl md:text-3xl font-bold text-gray-900 mb-1">
              恭喜「{brandName}」
            </h2>
            <p className="text-base md:text-lg text-gray-500 mb-8 md:mb-10">正式开启 AI 曝光优化之旅！</p>

            {/* Stats grid */}
            <div className="grid grid-cols-2 gap-3 md:gap-4 mb-8 md:mb-10">
              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.3 }}
                className="bg-linear-to-br from-blue-50 to-indigo-50 rounded-2xl p-4 md:p-5"
              >
                <p className="text-xs md:text-sm text-gray-400 mb-1">服务套餐</p>
                <p className="text-sm md:text-base font-bold text-gray-900">{tierLabel}</p>
                {/* [P1-11 fix 2026-05-23] 客户 token-only 页禁裸露 AI 出现率百分比 · 翻译成自然话术 */}
                <p className="text-xs md:text-sm text-blue-600 font-medium mt-0.5">
                  {(() => {
                    const pctMatch = String(aiProbability).match(/(\d+(?:\.\d+)?)/);
                    const pct = pctMatch ? parseFloat(pctMatch[1]) : 0;
                    return describeProbability(pct, { quoteMode: true }).label;  // [P1] 客户确认页永不出90%+「几乎每次」
                  })()}
                </p>
              </motion.div>
              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.4 }}
                className="bg-linear-to-br from-emerald-50 to-green-50 rounded-2xl p-4 md:p-5"
              >
                <p className="text-xs md:text-sm text-gray-400 mb-1">
                  {clusterCount ? '主题包' : '优化关键词'}
                </p>
                <p className="text-2xl md:text-3xl font-bold text-gray-900">
                  {clusterCount || keywordCount}
                </p>
                <p className="text-xs md:text-sm text-emerald-600 font-medium mt-0.5">
                  {clusterCount ? `${clusterCount}个包 · ${keywordCount}个核心词` : '个关键词'}
                </p>
              </motion.div>
              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.5 }}
                className="bg-linear-to-br from-amber-50 to-orange-50 rounded-2xl p-4 md:p-5"
              >
                <p className="text-xs md:text-sm text-gray-400 mb-1">内容产出</p>
                <p className="text-2xl md:text-3xl font-bold text-gray-900">{totalArticles}</p>
                <p className="text-xs md:text-sm text-amber-600 font-medium mt-0.5">篇</p>
              </motion.div>
              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.6 }}
                className="bg-linear-to-br from-violet-50 to-purple-50 rounded-2xl p-4 md:p-5"
              >
                <p className="text-xs md:text-sm text-gray-400 mb-1">合计</p>
                <p className="text-xl md:text-2xl font-bold text-blue-600">¥{totalPrice.toLocaleString()}</p>
                <p className="text-xs md:text-sm text-violet-600 font-medium mt-0.5">元 · 累计达标30天</p>
              </motion.div>
            </div>

            <div className="bg-gray-50 rounded-xl md:rounded-2xl p-4 md:p-5">
              <p className="text-sm md:text-base text-gray-500 leading-relaxed">
                您的专属顾问将尽快与您联系<br />正式启动优化方案
              </p>
            </div>

            {/* Phase B (CTO-15.11 2026-04-28):激活后给客户门户入口
                老板痛点:客户付款后 /s/ 重访不知道下一步看什么
                → 给"查看数据看板"按钮 跳 /portal/:token 月报 + 排名追踪 */}
            {portalToken && (
              <div className="mt-5">
                <a
                  href={`/portal/${portalToken}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-2 px-5 py-3 rounded-xl bg-blue-600 hover:bg-blue-700 text-white text-sm md:text-base font-medium shadow-lg shadow-blue-200 transition"
                >
                  <span>查看数据看板</span>
                  <span aria-hidden>→</span>
                </a>
                <p className="text-xs text-gray-400 mt-2">
                  排名追踪 / 月报 / 投放进度 都在这里
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Footer · v3.6 白标 · 客户页显示代理品牌(external_only 不回退平台) */}
        <div className="mt-8 opacity-60">
          <BrandFooter brand={brand} />
        </div>
      </motion.div>
    </div>
  );
}
