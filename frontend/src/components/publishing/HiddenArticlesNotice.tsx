import { EyeOff } from 'lucide-react';
import type { HiddenBreakdown } from '@/pages/Publishing/publishCenterScopeLogic';

interface HiddenArticlesNoticeProps {
  hidden: HiddenBreakdown;
  /** 清掉可清的过滤条件(URL ?articles= 预选 + 只看优化文章)。购物车不在此列。 */
  onShowAll?: () => void;
}

/**
 * 「当前有 N 篇未显示」提示行(2026-07-30 工单 T2)。
 *
 * 这一行比「空组也渲染标题」更要紧:标题带 (0) 只回答"这组是空的",
 * 回答不了老板真正在问的"我的文章去哪了"。上游三道过滤器
 * (URL 预选 / 只看优化 / 购物车)全都是静默的 —— 没有这行,用户永远
 * 无法自己诊断,只能来问人。
 *
 * 购物车那部分**不可**由「显示全部」清除(清了等于替用户清空购物车),
 * 所以它只计数、只解释;按钮仅在确有可清项时出现。
 */
export function HiddenArticlesNotice({ hidden, onShowAll }: HiddenArticlesNoticeProps) {
  if (!hidden || hidden.total <= 0) return null;
  const parts: string[] = [];
  if (hidden.byCart > 0) parts.push(`购物车 ${hidden.byCart} 篇`);
  if (hidden.byPreselect > 0) parts.push(`从写作大厅带入的筛选 ${hidden.byPreselect} 篇`);
  if (hidden.byOptimize > 0) parts.push(`只看优化文章 ${hidden.byOptimize} 篇`);
  const clearable = hidden.byPreselect > 0 || hidden.byOptimize > 0;

  return (
    <div
      data-testid="hidden-articles-notice"
      data-hidden-total={hidden.total}
      className="flex items-start gap-1.5 border-b border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-700 dark:text-amber-200"
    >
      <EyeOff className="mt-0.5 size-3 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">
        <span>当前有 {hidden.total} 篇未显示：{parts.join(' / ')}</span>
        {clearable && (
          <button
            type="button"
            onClick={onShowAll}
            data-testid="hidden-articles-show-all"
            className="ml-1.5 underline underline-offset-2 hover:no-underline"
          >
            显示全部
          </button>
        )}
      </div>
    </div>
  );
}
