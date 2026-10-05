/**
 * 统计粒度分段控件（当日/本周/本月）。
 * 这是 AI-3 真实接受的 `granularity` 参数（后端据此返回不同粒度聚合），不是被忽略的假筛选。
 * 键盘可达。真实时间范围由后端响应的 window 决定，页面另行只读展示。
 */

import { cn } from '@/lib/utils';
import type { Granularity } from '../types';

const OPTIONS: { key: Granularity; label: string }[] = [
  { key: 'day', label: '当日' },
  { key: 'week', label: '本周' },
  { key: 'month', label: '本月' },
];

export function GranularityFilter({
  value,
  onChange,
}: {
  value: Granularity;
  onChange: (g: Granularity) => void;
}) {
  return (
    <div role="group" aria-label="统计粒度" className="inline-flex rounded-lg border border-border bg-card p-0.5">
      {OPTIONS.map((o) => {
        const active = o.key === value;
        return (
          <button
            key={o.key}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(o.key)}
            className={cn(
              'min-h-[32px] rounded-md px-3 py-1 text-xs font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40',
              active ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
