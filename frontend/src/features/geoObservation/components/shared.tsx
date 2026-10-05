/**
 * 共享展示原语（自足、可访问、主题令牌驱动）。
 * - 只用现役语义 token（bg-card/border-border/text-foreground/text-muted-foreground/bg-muted/bg-brand），
 *   自动跟随亮/暗主题，不新造设计系统。
 * - InfoTip / LongText 键盘可达、有 aria、无跑马灯。
 */

import { useId, useRef, useState, type ReactNode } from 'react';
import { Info, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

/** 卡片容器：圆角 ≤ 8px（rounded-lg），不做卡片套卡片。 */
export function Panel({
  children,
  className,
  as: Tag = 'section',
  ...rest
}: {
  children: ReactNode;
  className?: string;
  as?: 'section' | 'div' | 'article';
} & React.HTMLAttributes<HTMLElement>) {
  return (
    <Tag className={cn('rounded-lg border border-border bg-card', className)} {...rest}>
      {children}
    </Tag>
  );
}

export function SectionHeader({
  title,
  hint,
  info,
  right,
}: {
  title: ReactNode;
  hint?: ReactNode;
  info?: string;
  right?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          <h3 className="text-sm font-semibold text-foreground">{title}</h3>
          {info ? <InfoTip label={info} /> : null}
        </div>
        {hint ? <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{hint}</p> : null}
      </div>
      {right ? <div className="shrink-0">{right}</div> : null}
    </div>
  );
}

/**
 * 信息提示：技术口径/公式/版本等进入这里，不抢主叙事。
 * 键盘可聚焦（button），hover/focus 都能看到，aria-describedby 关联说明。
 */
export function InfoTip({ label, className }: { label: string; className?: string }) {
  const [open, setOpen] = useState(false);
  const [align, setAlign] = useState<'left' | 'center' | 'right'>('center');
  const id = useId();
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  // 边缘感知：靠左/靠右时改变对齐，避免打开提示时造成横向溢出。
  const computeAlign = () => {
    const el = btnRef.current;
    if (!el) return;
    const cx = el.getBoundingClientRect().left + el.offsetWidth / 2;
    const w = window.innerWidth || document.documentElement.clientWidth;
    if (cx < w / 3) setAlign('left');
    else if (cx > (w * 2) / 3) setAlign('right');
    else setAlign('center');
  };

  const show = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    computeAlign();
    setOpen(true);
  };
  const hide = () => {
    closeTimer.current = setTimeout(() => setOpen(false), 80);
  };

  const alignClass =
    align === 'left' ? 'left-0' : align === 'right' ? 'right-0' : 'left-1/2 -translate-x-1/2';

  return (
    <span className={cn('relative inline-flex', className)}>
      <button
        ref={btnRef}
        type="button"
        aria-label="查看说明"
        aria-describedby={open ? id : undefined}
        className="inline-flex h-6 w-6 -m-1 items-center justify-center rounded-full text-muted-foreground hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onClick={(e) => {
          e.stopPropagation();
          if (!open) computeAlign();
          setOpen((v) => !v);
        }}
      >
        <Info className="h-3.5 w-3.5" />
      </button>
      {open ? (
        <span
          role="tooltip"
          id={id}
          className={cn(
            'absolute top-6 z-50 w-60 max-w-[calc(100vw-1.25rem)] rounded-lg border border-border bg-popover px-3 py-2 text-xs leading-relaxed text-popover-foreground shadow-md',
            alignClass,
          )}
        >
          {label}
        </span>
      ) : null}
    </span>
  );
}

/**
 * 长文本：截断 + 展开/收起，鼠标 hover（title）/键盘/触摸都能看到全称，无跑马灯。
 * 不因文字变长撑破固定布局：折叠态用 line-clamp。
 * 全部用 <span>（display:block 由 className 提供）+ <button>，避免在 inline 上下文中
 * 产出 <span><p></p></span> 这类非法嵌套（影响 DOM 修复 / 键盘导航 / 可访问性）。
 */
export function LongText({
  text,
  clamp = 2,
  className,
}: {
  text: string;
  clamp?: 1 | 2 | 3;
  className?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const clampClass = clamp === 1 ? 'line-clamp-1' : clamp === 3 ? 'line-clamp-3' : 'line-clamp-2';
  return (
    <span className={cn('block', className)}>
      <span
        className={cn(
          'block text-sm text-foreground break-words',
          !expanded && clampClass,
        )}
        title={text}
      >
        {text}
      </span>
      {text.length > 24 ? (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
          className="mt-0.5 text-xs text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 rounded"
        >
          {expanded ? '收起' : '展开全部'}
        </button>
      ) : null}
    </span>
  );
}

/** 标签胶囊。 */
export function Pill({
  children,
  tone = 'neutral',
  className,
}: {
  children: ReactNode;
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'info' | 'muted';
  className?: string;
}) {
  const toneMap: Record<string, string> = {
    neutral: 'bg-muted text-muted-foreground border-border',
    good: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/25 dark:text-emerald-400',
    warn: 'bg-amber-500/10 text-amber-600 border-amber-500/25 dark:text-amber-400',
    bad: 'bg-red-500/10 text-red-600 border-red-500/25 dark:text-red-400',
    info: 'bg-blue-500/10 text-blue-600 border-blue-500/25 dark:text-blue-400',
    muted: 'bg-muted text-muted-foreground/70 border-border',
  };
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium',
        toneMap[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/** KPI 磁贴：数字 + 业务标签 + 可选说明。 */
export function MetricTile({
  label,
  value,
  sub,
  info,
  tone,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  info?: string;
  tone?: 'default' | 'good' | 'warn';
}) {
  const valueColor =
    tone === 'good' ? 'text-emerald-600 dark:text-emerald-400' : tone === 'warn' ? 'text-amber-600 dark:text-amber-400' : 'text-foreground';
  return (
    <div className="rounded-lg border border-border bg-card p-3.5 sm:p-4">
      <div className="flex items-center gap-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        {info ? <InfoTip label={info} /> : null}
      </div>
      <div className={cn('mt-1.5 text-2xl font-semibold tabular-nums', valueColor)}>{value}</div>
      {sub ? <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div> : null}
    </div>
  );
}

/** 行动/跳转按钮（带图标，44px 触控目标）。 */
export function ActionLink({
  children,
  onClick,
  icon: Icon,
  ariaLabel,
}: {
  children: ReactNode;
  onClick: () => void;
  icon?: typeof ChevronRight;
  ariaLabel?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      className="inline-flex min-h-[36px] items-center gap-1 rounded-lg border border-border bg-card px-3 py-2 text-xs font-medium text-foreground transition hover:bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
    >
      {children}
      {Icon ? <Icon className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
    </button>
  );
}
