/**
 * BadgeOverflowGroup · Badge 预算容器 · CTO-15.23 Density System v1
 *
 * 老板红线:整站 ~1473 处 Badge / rounded-full · 一行 5+ chip 视觉噪音
 *
 * 设计:
 * - 默认常驻最多 2 彩色 badge(可配 maxVisible)
 * - 超出的进 "+N" overflow chip
 * - hover/click "+N" 显示完整 badge popover(轻量自建 · click-outside 关)
 * - 整体语义:让一行 badge 视觉重量统一
 *
 * 用法:
 *   <BadgeOverflowGroup
 *     badges={[
 *       { key: 'authority', label: '权威媒体', color: 'blue' },
 *       { key: 'geo', label: 'GEO', color: 'green' },
 *       { key: 'engine', label: '6 引擎', color: 'amber' },
 *       { key: 'pricequal', label: '性价比', color: 'red' },
 *     ]}
 *     maxVisible={2}
 *   />
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { cn } from '@/lib/utils';

export type BadgeColor = 'blue' | 'green' | 'amber' | 'red' | 'purple' | 'gray';

export interface BadgeItem {
  key: string;
  label: string;
  /** 自带预渲染节点(优先 render · 给复合 chip 如 GeoEngineBadge 用)· label 仅作 +N popover 内 fallback */
  node?: ReactNode;
  color?: BadgeColor;
  /** hover tooltip · 鼠标悬停看详细说明(常用于规则解释) */
  tooltip?: string;
  /** 主动点击行为(可选 · 默认只显示) */
  onClick?: () => void;
}

export interface BadgeOverflowGroupProps {
  badges: BadgeItem[];
  /** 常驻可见个数 · 默认 2 */
  maxVisible?: number;
  /** "+N" overflow chip 在 popover 内 layout · vertical 默认 */
  overflowDirection?: 'vertical' | 'horizontal';
  /** chip size · sm 默认 */
  size?: 'sm' | 'xs';
  className?: string;
}

const COLOR_CLASSES: Record<BadgeColor, string> = {
  blue: 'border-blue-500/30 text-blue-400 bg-blue-500/10',
  green: 'border-green-500/30 text-green-400 bg-green-500/10',
  amber: 'border-amber-500/30 text-amber-400 bg-amber-500/10',
  red: 'border-red-500/30 text-red-400 bg-red-500/10',
  purple: 'border-purple-500/30 text-purple-400 bg-purple-500/10',
  gray: 'border-border text-muted-foreground bg-muted/30',
};

export function BadgeOverflowGroup({
  badges,
  maxVisible = 2,
  overflowDirection = 'vertical',
  size = 'sm',
  className,
}: BadgeOverflowGroupProps) {
  const [popoverOpen, setPopoverOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!popoverOpen) return;
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setPopoverOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [popoverOpen]);

  if (!badges.length) return null;

  const visible = badges.slice(0, maxVisible);
  const overflow = badges.slice(maxVisible);
  const overflowCount = overflow.length;

  const sizeCls = size === 'xs'
    ? 'text-[10px] px-1 py-px'
    : 'text-[11px] px-1.5 py-0.5';

  const Pill = ({ b }: { b: BadgeItem }) => {
    /* 自带 node 优先 render(用于复合 chip 如 GeoEngineBadge / SweetSpotBadge 等已有 React 组件) */
    if (b.node) return <>{b.node}</>;
    return (
      <button
        type="button"
        onClick={b.onClick}
        disabled={!b.onClick}
        title={b.tooltip || b.label}
        className={cn(
          'inline-flex items-center rounded-full font-medium border whitespace-nowrap',
          sizeCls,
          COLOR_CLASSES[b.color ?? 'gray'],
          b.onClick ? 'cursor-pointer hover:opacity-80 transition-opacity' : 'cursor-default',
        )}
      >
        {b.label}
      </button>
    );
  };

  return (
    <div ref={containerRef} className={cn('inline-flex items-center gap-1 flex-wrap', className)}>
      {visible.map((b) => <Pill key={b.key} b={b} />)}
      {overflowCount > 0 && (
        <div className="relative">
          <button
            type="button"
            onClick={() => setPopoverOpen(!popoverOpen)}
            className={cn(
              'inline-flex items-center rounded-full font-medium border bg-secondary/60 text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors',
              sizeCls,
            )}
            aria-label={`查看更多 ${overflowCount} 个标签`}
          >
            +{overflowCount}
          </button>
          {popoverOpen && (
            <div
              className={cn(
                'absolute z-50 top-full mt-1 right-0 min-w-[140px] rounded-lg border border-border bg-popover p-2 shadow-lg',
                overflowDirection === 'vertical' ? 'flex flex-col gap-1.5' : 'flex flex-wrap gap-1',
              )}
            >
              {overflow.map((b) => (
                <div key={b.key} className="flex items-start gap-2">
                  <Pill b={b} />
                  {b.tooltip && b.tooltip !== b.label && (
                    <span className="text-info-tertiary leading-snug">{b.tooltip}</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
