/**
 * P0.6 价格 sanity 配置 · 行业中位数 + 阈值
 *
 * 来源:P0.6 生产事故(罗平县装修 ¥2.6 万-6.7 万吓跑客户)
 * 策略:纯前端拦截层 · 不改 transparent_pricing/batch_pricing 算法内核
 *
 * seed 数据(2026-04-24)从存量 quotes 抽 p50/p90 · 上线 2 周后按 override 率调
 */

export type PriceHealth = 'normal' | 'warn' | 'danger';

export const PRICE_SANITY_THRESHOLDS = {
  /** 单关键词月费 > ¥1,500 触发警告 */
  WARN_SINGLE_KW: 1500,
  /** 总月费 > ¥10,000 强制确认 */
  DANGER_TOTAL_MONTHLY: 10000,
  /** 未识别行业 fallback p50 */
  UNKNOWN_INDUSTRY_P50: 4000,
  /** 未识别行业 fallback p90 */
  UNKNOWN_INDUSTRY_P90: 10000,
} as const;

/**
 * 行业月费中位数 seed(p50 = 中位 · p90 = 行业合理上限)
 * key 是行业关键词子串 · 通过 includes 匹配
 * 不在列表的行业走 UNKNOWN_INDUSTRY_P50/P90 fallback
 */
export const INDUSTRY_MEDIAN_MONTHLY: Record<string, { p50: number; p90: number }> = {
  // 生活服务
  '装修': { p50: 3500, p90: 8000 },
  '家装': { p50: 3500, p90: 8000 },
  '家居': { p50: 3800, p90: 9000 },
  '美容': { p50: 3000, p90: 7500 },
  '医美': { p50: 4500, p90: 11000 },
  '餐饮': { p50: 2800, p90: 7000 },
  '婚礼': { p50: 3200, p90: 7800 },
  '婚纱': { p50: 3200, p90: 7800 },
  '摄影': { p50: 3000, p90: 7500 },
  // 专业服务
  '法律': { p50: 4500, p90: 10000 },
  '律所': { p50: 4500, p90: 10000 },
  '会计': { p50: 4500, p90: 10000 },
  '咨询': { p50: 5000, p90: 11000 },
  '教育': { p50: 4000, p90: 9500 },
  '培训': { p50: 4000, p90: 9500 },
  '留学': { p50: 5500, p90: 13000 },
  '医疗': { p50: 5500, p90: 12000 },
  '医院': { p50: 5500, p90: 12000 },
  '口腔': { p50: 5000, p90: 11500 },
  // B2B / 工业
  '软件': { p50: 6000, p90: 14000 },
  'SaaS': { p50: 6500, p90: 15000 },
  'AI': { p50: 6500, p90: 15000 },
  '机械': { p50: 5500, p90: 12500 },
  '制造': { p50: 5500, p90: 12500 },
  '化工': { p50: 6000, p90: 13000 },
  '外贸': { p50: 5000, p90: 11000 },
  // 高价行业
  '金融': { p50: 7500, p90: 16000 },
  '保险': { p50: 6500, p90: 14000 },
  '地产': { p50: 8000, p90: 18000 },
  '房产': { p50: 8000, p90: 18000 },
  '汽车': { p50: 6000, p90: 13000 },
  // 服饰/零售
  '服饰': { p50: 3500, p90: 8500 },
  '服装': { p50: 3500, p90: 8500 },
  '电商': { p50: 4000, p90: 9500 },
};

export function getIndustryMedian(industry: string): { p50: number; p90: number } {
  if (!industry) {
    return {
      p50: PRICE_SANITY_THRESHOLDS.UNKNOWN_INDUSTRY_P50,
      p90: PRICE_SANITY_THRESHOLDS.UNKNOWN_INDUSTRY_P90,
    };
  }
  for (const [key, value] of Object.entries(INDUSTRY_MEDIAN_MONTHLY)) {
    if (industry.includes(key)) return value;
  }
  return {
    p50: PRICE_SANITY_THRESHOLDS.UNKNOWN_INDUSTRY_P50,
    p90: PRICE_SANITY_THRESHOLDS.UNKNOWN_INDUSTRY_P90,
  };
}

/**
 * 判断月费健康度 · 基于行业 p90 + 硬阈值
 * danger: 超 p90 且超 DANGER_TOTAL_MONTHLY
 * warn: 超 p90 * 0.75
 * normal: 在合理区间
 */
export function describePriceHealth(totalMonthly: number, industry: string): PriceHealth {
  const { p90 } = getIndustryMedian(industry);
  if (totalMonthly >= Math.max(PRICE_SANITY_THRESHOLDS.DANGER_TOTAL_MONTHLY, p90)) {
    return 'danger';
  }
  if (totalMonthly >= p90 * 0.75) {
    return 'warn';
  }
  return 'normal';
}

/** 偏离行业 p50 的百分比(正=高于 · 负=低于) */
export function describePriceDeviation(totalMonthly: number, industry: string): number {
  const { p50 } = getIndustryMedian(industry);
  if (p50 === 0) return 0;
  return Math.round(((totalMonthly - p50) / p50) * 100);
}

/** 单关键词价格是否偏高 */
export function isKeywordPriceHigh(price: number): boolean {
  return price >= PRICE_SANITY_THRESHOLDS.WARN_SINGLE_KW;
}
