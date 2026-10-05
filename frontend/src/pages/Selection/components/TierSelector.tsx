import { motion } from 'framer-motion';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { describeProbability } from '@/lib/probability';

interface TierInfo {
  label: string;
  ai_probability: string;
  stars: number;
  total_price: number;
  total_articles: number;
}

interface Props {
  tiers: Record<string, TierInfo>;
  activeTier: string;
  onSelect: (tier: string) => void;
  onHoverStart?: (tier: string) => void;
  onHoverEnd?: (tier: string) => void;
  dynamicPrices?: Record<string, number>;
}

const tierOrder = ['entry', 'standard', 'flagship'];

const tierIcons: Record<string, string> = {
  entry: '⚡',
  standard: '🚀',
  flagship: '👑',
};

const tierDescriptions: Record<string, string> = {
  entry: '轻量启动，快速建立 AI 曝光基础',
  standard: '最受欢迎，平衡效果与投入',
  flagship: '全面覆盖，最大化品牌 AI 曝光',
};

const tierBadges: Record<string, { text: string; gradient: string }> = {
  entry: { text: '性价比之选', gradient: 'from-emerald-400 to-teal-400' },
  standard: { text: '最受欢迎', gradient: 'from-amber-400 to-orange-400' },
  flagship: { text: '效果最佳', gradient: 'from-blue-500 to-indigo-500' },
};

export function TierSelector({ tiers, activeTier, onSelect, onHoverStart, onHoverEnd, dynamicPrices }: Props) {
  return (
    <div className="max-w-lg md:max-w-2xl lg:max-w-4xl mx-auto px-5 py-4 md:py-6">
      <h4 className="text-sm md:text-base font-semibold text-gray-700 mb-3 md:mb-4 px-1 flex items-center gap-1">
        选择服务套餐
        <HelpHint title="三档的「AI 出现率」是什么意思?">
          指用户在各家 AI 搜索引擎里搜你的关键词时, AI 的回答里<b>提到你品牌的频次</b>。
          档位越高, 我们给你发的文章越多、覆盖面越广, 出现频次也越高。
          <br />注: 这是<b>预期目标</b>(基于历史数据), 受平台算法、竞争环境影响, 不是保证值。
          <br />本报价以<b>累计达标30天</b>为本轮交付目标。超过 6 个月的长期固定价服务, 受平台算法、竞品投入、媒体资源价格变化影响, 建议按阶段复盘并重新确认方案与报价。
        </HelpHint>
      </h4>
      <div className="grid grid-cols-3 gap-3 md:gap-4 items-center py-4">
        {tierOrder.map((key, index) => {
          const tier = tiers[key];
          if (!tier) return null;
          const isActive = activeTier === key;
          const badge = tierBadges[key];

          return (
            <motion.div
              key={key}
              initial={{ opacity: 0, y: 20 }}
              animate={{
                opacity: 1,
                y: isActive ? -6 : 0,
                scale: isActive ? 1.05 : 0.97,
              }}
              transition={{ type: 'tween', duration: 0.3, ease: 'easeOut' }}
              whileTap={{ scale: 0.95 }}
              whileHover={!isActive ? { y: -3, scale: 1, transition: { duration: 0.2 } } : undefined}
              onClick={() => onSelect(key)}
              onHoverStart={() => onHoverStart?.(key)}
              onHoverEnd={() => onHoverEnd?.(key)}
              className={`
                relative rounded-2xl md:rounded-3xl cursor-pointer text-center overflow-hidden flex flex-col transition-colors duration-200
                ${isActive
                  ? key === 'standard'
                    ? 'bg-linear-to-b from-amber-50 to-orange-50 border-2 border-amber-400 shadow-xl shadow-amber-100/50'
                    : key === 'flagship'
                      ? 'bg-linear-to-b from-blue-50 to-indigo-50 border-2 border-blue-500 shadow-xl shadow-blue-100/50'
                      : 'bg-linear-to-b from-emerald-50 to-teal-50 border-2 border-emerald-400 shadow-xl shadow-emerald-100/50'
                  : 'bg-white border-2 border-gray-100 hover:border-gray-200 shadow-xs hover:shadow-lg opacity-75'
                }
              `}
            >
              {/* Badge banner */}
              <div className={`bg-linear-to-r ${badge.gradient} text-white text-[10px] md:text-xs font-bold py-1 md:py-1.5 tracking-wider`}>
                {badge.text}
              </div>

              <div className="p-3 md:p-5 lg:p-6 flex-1 flex flex-col">
                <div className="text-2xl md:text-3xl mb-2 md:mb-3">{tierIcons[key]}</div>

                <p className={`text-sm md:text-base font-bold mb-1 md:mb-2 ${isActive ? 'text-gray-900' : 'text-gray-600'}`}>
                  {tier.label}
                </p>

                {/* Description - hidden on mobile */}
                <p className="hidden md:block text-xs text-gray-400 mb-3 leading-relaxed">
                  {tierDescriptions[key]}
                </p>

                {/* AI 出现率 · r13 圆点可视化 (老板拍板 C 方案) */}
                {/* "问 N 次出现 M 次" → N 个圆点 · 前 M 个填充主色 · 视觉一眼懂比例 */}
                <div className="my-3 md:my-5">
                  {(() => {
                    const m = String(tier.ai_probability).match(/(\d+(?:\.\d+)?)/);
                    const pct = m ? parseFloat(m[1]) : 0;
                    const prob = describeProbability(pct, { quoteMode: true });  // [P1] 报价套餐卡片永不出90%+「几乎每次」
                    const fillColor = isActive
                      ? key === 'standard' ? 'bg-amber-500' : key === 'flagship' ? 'bg-blue-500' : 'bg-emerald-500'
                      : 'bg-gray-400';
                    const emptyColor = isActive
                      ? key === 'standard' ? 'bg-amber-100' : key === 'flagship' ? 'bg-blue-100' : 'bg-emerald-100'
                      : 'bg-gray-200';
                    const labelColor = isActive
                      ? key === 'standard' ? 'text-amber-700' : key === 'flagship' ? 'text-blue-700' : 'text-emerald-700'
                      : 'text-gray-600';
                    return (
                      <>
                        {/* 圆点行 · 数字行 */}
                        <div className="flex items-center justify-center gap-1.5 md:gap-2 mb-2 md:mb-3">
                          {Array.from({ length: prob.askCount }).map((_, i) => (
                            <span
                              key={i}
                              className={`block rounded-full transition-colors ${
                                i < prob.appearCount ? fillColor : emptyColor
                              } w-2.5 h-2.5 md:w-3 md:h-3 lg:w-3.5 lg:h-3.5`}
                            />
                          ))}
                        </div>
                        {/* 话术 · 中等字号 · 一行 · 重心给"出现 N 次"突出 */}
                        {prob.isUbiquitous ? (
                          <p className={`text-base md:text-lg font-bold ${labelColor} leading-snug`}>
                            几乎每次都出现
                          </p>
                        ) : prob.isMinimal ? (
                          <p className={`text-base md:text-lg font-bold ${labelColor} leading-snug`}>
                            偶尔出现
                          </p>
                        ) : (
                          <p className={`text-sm md:text-base font-semibold ${labelColor} leading-snug whitespace-nowrap`}>
                            <span className="text-gray-500 font-normal">问</span>
                            <span className={`mx-1 text-lg md:text-xl font-bold ${labelColor}`}>{prob.askCount}</span>
                            <span className="text-gray-500 font-normal">次出现</span>
                            <span className={`mx-1 text-lg md:text-xl font-bold ${labelColor}`}>{prob.appearCount}</span>
                            <span className="text-gray-500 font-normal">次</span>
                          </p>
                        )}
                        <p className={`text-[10px] md:text-xs mt-1.5 ${isActive ? 'text-gray-500' : 'text-gray-400'} tracking-wide`}>
                          目标出现率 {tier.ai_probability} · 预期 AI 曝光频次
                        </p>
                      </>
                    );
                  })()}
                </div>

                <div className="text-amber-400 text-xs md:text-sm mb-2 md:mb-3 tracking-wider">
                  {'★'.repeat(tier.stars)}
                  <span className="text-gray-200">{'★'.repeat(5 - tier.stars)}</span>
                </div>

                {/* Price section */}
                <div className={`
                  pt-2 md:pt-3 border-t mt-auto
                  ${isActive
                    ? key === 'standard' ? 'border-amber-200' : key === 'flagship' ? 'border-blue-200' : 'border-emerald-200'
                    : 'border-gray-100'
                  }
                `}>
                  {(dynamicPrices?.[key] ?? tier.total_price) > 0 ? (
                  <p className={`text-base md:text-xl lg:text-2xl font-bold ${
                    isActive
                      ? key === 'standard' ? 'text-amber-600' : key === 'flagship' ? 'text-blue-600' : 'text-emerald-600'
                      : 'text-gray-700'
                  }`}>
                    ¥{(dynamicPrices?.[key] ?? tier.total_price).toLocaleString()}
                    <span className="text-[10px] md:text-xs font-normal text-gray-400"> · 累计达标30天</span>
                  </p>
                  ) : (
                    <p className="text-base md:text-xl font-bold text-gray-400">待选择</p>
                  )}
                </div>
              </div>

              {isActive && (
                <motion.div
                  layoutId="tier-indicator"
                  className={`h-1 rounded-b-2xl md:rounded-b-3xl ${
                    key === 'standard'
                      ? 'bg-linear-to-r from-amber-400 to-orange-400'
                      : key === 'flagship'
                        ? 'bg-linear-to-r from-blue-500 to-indigo-500'
                        : 'bg-linear-to-r from-emerald-400 to-teal-400'
                  }`}
                />
              )}
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}
