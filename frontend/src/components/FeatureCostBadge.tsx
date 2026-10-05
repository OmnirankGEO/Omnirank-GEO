/**
 * FeatureCostBadge — 功能扣费 Badge（动态价目表）
 *
 * 所有扣费按钮/卡片应该用本组件显示价格，而不是硬编码 "扣 X 分"。
 * 改价只改 DB feature_pricing 表，这里自动同步。
 *
 * @example 嵌入 Button 文案
 *   <Button onClick={generate}>
 *     批量生成标题 <FeatureCostBadge featureCode="article_gen" multiplier={N} />
 *   </Button>
 *
 * @example 独立 Badge
 *   <FeatureCostBadge featureCode="geo_diagnosis" prefix="启动体检" />
 *
 * v1_3 · CTO-15.1 · 2026-04-19
 */

import { usePricing } from '@/context/PricingContext';
import { cn } from '@/lib/utils';

interface Props {
  /** DB feature_pricing.feature_code — e.g. 'article_gen' / 'geo_diagnosis' */
  featureCode: string;
  /** 数量倍数（批量操作时）— 显示 total = cost * multiplier */
  multiplier?: number;
  /** 前缀文案 — e.g. "启动体检" → "启动体检 扣 650 分" */
  prefix?: string;
  /** 后缀文案 — e.g. "/篇" → "扣 390 分/篇" */
  suffix?: string;
  /** 视觉变体 */
  variant?: 'inline' | 'badge';
  className?: string;
  /** 免费时显示什么 — 默认"免费" */
  freeLabel?: string;
  /** [写作卡死根治 2026-06-08] 余额不足红态(纯展示 · 调用方用 useWallet 判断后传入 · 组件不接钱包避免 Provider 依赖) */
  insufficient?: boolean;
  /** 余额不足时尾部补充提示 — e.g. "· 余额只够 3 篇" */
  affordableHint?: string;
}

export function FeatureCostBadge({
  featureCode,
  multiplier = 1,
  prefix,
  suffix,
  variant = 'inline',
  className,
  freeLabel = '免费',
  insufficient = false,
  affordableHint,
}: Props) {
  const { getCost, loading } = usePricing();
  const unitCost = getCost(featureCode);

  // 价目表还在加载
  if (loading && unitCost == null) {
    return (
      <span className={cn('text-xs opacity-60', className)}>
        {prefix} 扣 … 算力{suffix}
      </span>
    );
  }

  // 价目表不识别该 feature_code（后端没配 / 拼写错）— 显式兜底，方便调试发现
  if (unitCost == null) {
    return (
      <span className={cn('text-xs text-orange-500', className)} title={`未配置价目：${featureCode}`}>
        {prefix ? `${prefix} ` : ''}价目待配置
      </span>
    );
  }

  const total = unitCost * multiplier;

  if (total === 0) {
    return (
      <span
        className={cn(
          variant === 'badge'
            ? 'inline-block px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-700 dark:text-emerald-300 text-[10px] font-medium'
            : 'text-xs text-emerald-600',
          className,
        )}
      >
        {prefix ? `${prefix} ` : ''}
        {freeLabel}
      </span>
    );
  }

  const unit = suffix || '';
  const countHint = multiplier > 1 ? `（${multiplier} × ${unitCost}）` : '';

  return (
    <span
      className={cn(
        variant === 'badge'
          // 余额不足 → 红 chip;充足 → 中性 chip(半透明灰、文字继承宿主按钮颜色)
          ? insufficient
            ? 'inline-block px-1.5 py-0.5 rounded bg-red-500/15 text-red-600 dark:text-red-400 text-[10px] font-medium'
            : 'inline-block px-1.5 py-0.5 rounded bg-black/5 dark:bg-white/10 text-[10px] font-medium opacity-80'
          : insufficient
            ? 'text-xs text-red-600 dark:text-red-400 font-medium'
            : 'text-xs opacity-80',
        className,
      )}
    >
      {prefix ? `${prefix} ` : ''}
      扣 {total.toLocaleString()} 算力{unit}{countHint}
      {insufficient && affordableHint ? affordableHint : ''}
    </span>
  );
}
