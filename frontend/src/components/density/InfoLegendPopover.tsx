/**
 * InfoLegendPopover · 图例 / 规则 / 说明默认折叠 · CTO-15.23 Density System v1
 *
 * 老板红线:PublishCenter 顶部"图例 + 权威媒体 + GEO + 6/6 + 性价比 + 低价 + 署名"
 * 6 chip 平铺挤一行 = 首屏过载
 *
 * 设计:
 * - 入口:1 个 button "ℹ️ 图例(N)" / 用户能看到入口但不被规则吃掉首屏
 * - 点击展开 popover · 显示全部图例项 + 每项说明
 * - 支持分组(如"基础徽章 / 价格警示 / 风险标识")
 * - click-outside 自动收起
 *
 * 用法:
 *   <InfoLegendPopover
 *     buttonLabel="图例"
 *     groups={[
 *       {
 *         title: '基础徽章',
 *         items: [
 *           { badge: <V/>, label: '权威媒体', description: '权威媒体认证' },
 *           { badge: <Badge>GEO</Badge>, label: 'GEO 引用', description: 'AI 搜索引擎引用过' },
 *         ],
 *       },
 *     ]}
 *   />
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Info, ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface LegendItem {
  badge: ReactNode;
  label: string;
  description?: string;
}

export interface LegendGroup {
  title?: string;
  items: LegendItem[];
}

export interface InfoLegendPopoverProps {
  /** Button 显示文案 · 默认"图例" */
  buttonLabel?: string;
  /** Button 前 icon · 默认 Info */
  icon?: ReactNode;
  /** 是否在 Button 上显示数字 "(N)" · 默认 true */
  showCount?: boolean;
  /** 图例分组 · 可只 1 组 */
  groups: LegendGroup[];
  /** Popover 位置 · 默认 below */
  placement?: 'below' | 'above';
  /** Popover 对齐 · 默认 start(左对齐 button) */
  align?: 'start' | 'end' | 'center';
  className?: string;
}

export function InfoLegendPopover({
  buttonLabel = '图例',
  icon,
  showCount = true,
  groups,
  placement = 'below',
  align = 'start',
  className,
}: InfoLegendPopoverProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const totalCount = groups.reduce((s, g) => s + g.items.length, 0);

  return (
    <div ref={ref} className={cn('relative inline-block', className)}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className={cn(
          'inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition-colors',
          open
            ? 'border-primary/40 bg-primary/5 text-foreground'
            : 'border-border bg-secondary/40 text-muted-foreground hover:bg-secondary hover:text-foreground',
        )}
        aria-expanded={open}
        aria-label={`查看${buttonLabel}`}
      >
        {icon ?? <Info className="h-3.5 w-3.5" />}
        <span>{buttonLabel}</span>
        {showCount && <span className="text-[10px] opacity-70">({totalCount})</span>}
        <ChevronDown className={cn('h-3 w-3 transition-transform', open && 'rotate-180')} />
      </button>

      {open && (
        <div
          className={cn(
            'absolute z-50 min-w-[260px] max-w-[420px] rounded-lg border border-border bg-popover p-3 shadow-lg',
            placement === 'below' ? 'top-full mt-1' : 'bottom-full mb-1',
            align === 'start' && 'left-0',
            align === 'end' && 'right-0',
            align === 'center' && 'left-1/2 -translate-x-1/2',
          )}
        >
          {groups.map((group, gi) => (
            <div key={gi} className={cn(gi > 0 && 'mt-3 pt-3 border-t border-border/50')}>
              {group.title && (
                <div className="text-info-tertiary uppercase tracking-wider font-semibold mb-2">
                  {group.title}
                </div>
              )}
              <div className="space-y-1.5">
                {group.items.map((item, i) => (
                  <div key={i} className="flex items-start gap-2 text-info-secondary">
                    <span className="shrink-0 mt-0.5">{item.badge}</span>
                    <div className="min-w-0 flex-1">
                      <div className="text-info-secondary font-medium">{item.label}</div>
                      {item.description && (
                        <div className="text-info-tertiary leading-snug mt-0.5">
                          {item.description}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
