/**
 * P0.6 价格 sanity banner · 发送报价前强制确认闸门
 *
 * 挂载点:StepQuotePreview.tsx / OnlineQuoteFlow.tsx · 发送按钮之前
 * 触发:总月费超行业 p90 或 城市是县级/乡镇低置信地域
 * 动作:展示警告 + 要求勾选"我已审阅" 方可发送(按钮 disabled 绑 isSendBlocked)
 *
 * CTO-15.7 2026-04-24 · 紧急生产止血(罗平县装修报价事故)
 */
import { useMemo } from 'react';
import { AlertTriangle, CheckCircle, Info } from 'lucide-react';
import { Checkbox } from '@/components/ui/checkbox';
import {
  PRICE_SANITY_THRESHOLDS,
  getIndustryMedian,
  describePriceHealth,
  describePriceDeviation,
  type PriceHealth,
} from '@/config/priceSanity';
import {
  isLowConfidenceCity,
  getCityTier,
  describeCityTier,
} from '@/config/cityTierMap';

export interface PriceSanityResult {
  health: PriceHealth;
  deviation: number;
  median: { p50: number; p90: number };
  lowConfCity: boolean;
  cityTierLabel: string;
  warnings: string[];
  isSendBlocked: (reviewed: boolean) => boolean;
}

/** Hook 式 · 组件/非组件场景都能用(如 OnlineQuoteFlow handleApprove) */
export function analyzePriceSanity(
  totalMonthly: number,
  industry: string,
  city: string,
): PriceSanityResult {
  const health = describePriceHealth(totalMonthly, industry);
  const deviation = describePriceDeviation(totalMonthly, industry);
  const median = getIndustryMedian(industry);
  const lowConfCity = isLowConfidenceCity(city);
  const tier = getCityTier(city);
  const cityTierLabel = describeCityTier(tier);

  const warnings: string[] = [];

  if (health === 'danger') {
    warnings.push(
      `总报价 ¥${totalMonthly.toLocaleString()} · 超行业 p90(¥${median.p90.toLocaleString()}) · 偏差 +${deviation}%`,
    );
  } else if (health === 'warn') {
    warnings.push(
      `总报价 ¥${totalMonthly.toLocaleString()} · 接近行业 p90(¥${median.p90.toLocaleString()}) · 偏差 ${deviation >= 0 ? '+' : ''}${deviation}%`,
    );
  }

  if (lowConfCity) {
    warnings.push(
      `${city || '未设城市'} 是${cityTierLabel}地域 · 数据稀疏低置信 · 建议参考同行业市区报价调整`,
    );
  }

  if (totalMonthly >= PRICE_SANITY_THRESHOLDS.DANGER_TOTAL_MONTHLY && health !== 'danger') {
    warnings.push(
      `总报价 ¥${totalMonthly.toLocaleString()} 超 ¥${PRICE_SANITY_THRESHOLDS.DANGER_TOTAL_MONTHLY.toLocaleString()} · 请代理二次审阅`,
    );
  }

  return {
    health,
    deviation,
    median,
    lowConfCity,
    cityTierLabel,
    warnings,
    isSendBlocked: (reviewed: boolean) => warnings.length > 0 && !reviewed,
  };
}

interface Props {
  totalMonthly: number;
  industry: string;
  city: string;
  reviewed: boolean;
  onReviewedChange: (v: boolean) => void;
}

export function PriceSanityBanner({
  totalMonthly,
  industry,
  city,
  reviewed,
  onReviewedChange,
}: Props) {
  const result = useMemo(
    () => analyzePriceSanity(totalMonthly, industry, city),
    [totalMonthly, industry, city],
  );

  // 无警告 · 绿条确认(提高代理信任)
  if (result.warnings.length === 0) {
    return (
      <div className="mb-4 rounded-lg border border-green-500/20 bg-green-500/5 p-3 flex items-center gap-2 text-sm text-green-400">
        <CheckCircle className="h-4 w-4 flex-shrink-0" />
        <span>
          报价在行业合理区间 · 中位 ¥{result.median.p50.toLocaleString()} / p90 ¥{result.median.p90.toLocaleString()}
        </span>
      </div>
    );
  }

  const isDanger = result.health === 'danger';

  return (
    <div
      className={
        isDanger
          ? 'mb-4 rounded-lg border border-red-500/40 bg-red-500/10 p-4'
          : 'mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 p-4'
      }
    >
      <div className="flex items-start gap-3">
        <AlertTriangle
          className={`h-5 w-5 flex-shrink-0 mt-0.5 ${isDanger ? 'text-red-400' : 'text-amber-400'}`}
        />
        <div className="flex-1 min-w-0">
          <div className={`font-semibold mb-2 ${isDanger ? 'text-red-400' : 'text-amber-400'}`}>
            {isDanger ? '⚠️ 报价异常 · 发送前请代理确认' : '报价偏高 · 请核对'}
          </div>
          <ul className="text-sm text-muted-foreground space-y-1.5 mb-3">
            {result.warnings.map((w, i) => (
              <li key={i} className="flex gap-1.5">
                <span>·</span>
                <span>{w}</span>
              </li>
            ))}
          </ul>
          <div className="flex items-center gap-2 text-xs text-muted-foreground/70 mb-3">
            <Info className="h-3 w-3 flex-shrink-0" />
            <span>
              行业中位 ¥{result.median.p50.toLocaleString()} / p90 ¥{result.median.p90.toLocaleString()}
              {' · '}
              城市级别 {result.cityTierLabel}
            </span>
          </div>
          <label className="flex items-start gap-2 text-sm cursor-pointer select-none">
            <Checkbox
              checked={reviewed}
              onCheckedChange={(v) => onReviewedChange(v === true)}
              className="mt-0.5"
            />
            <span className={isDanger ? 'text-red-300' : 'text-amber-300'}>
              我已审阅 {result.warnings.length} 项提示 · 确认发送该报价给客户
            </span>
          </label>
        </div>
      </div>
    </div>
  );
}
