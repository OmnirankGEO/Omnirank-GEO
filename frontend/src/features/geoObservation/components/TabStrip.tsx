/**
 * 可访问 Tab 条：role=tablist/tab/tabpanel，左右方向键切换，roving tabindex。
 * 用于服务商工作台与管理员治理台的主 Tab。
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { cn } from '@/lib/utils';

export interface TabDef<K extends string> {
  key: K;
  label: string;
  icon?: React.ComponentType<{ className?: string }>;
}

export function TabStrip<K extends string>({
  tabs,
  active,
  onChange,
  idBase,
}: {
  tabs: TabDef<K>[];
  active: K;
  onChange: (k: K) => void;
  idBase: string;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const scrollerRef = useRef<HTMLDivElement>(null);
  // 移动端可横向滚动时给出边缘渐隐提示，让用户知道还有更多 Tab（不让用户猜半个字）。
  const [edges, setEdges] = useState({ left: false, right: false });

  const updateEdges = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const left = el.scrollLeft > 2;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 2;
    setEdges((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);

  useEffect(() => {
    updateEdges();
    const el = scrollerRef.current;
    if (!el) return;
    el.addEventListener('scroll', updateEdges, { passive: true });
    window.addEventListener('resize', updateEdges);
    return () => {
      el.removeEventListener('scroll', updateEdges);
      window.removeEventListener('resize', updateEdges);
    };
  }, [updateEdges, tabs.length]);

  function onKeyDown(e: React.KeyboardEvent, index: number) {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft' && e.key !== 'Home' && e.key !== 'End') return;
    e.preventDefault();
    let next = index;
    if (e.key === 'ArrowRight') next = (index + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabs.length - 1;
    const key = tabs[next].key;
    onChange(key);
    refs.current[key]?.focus();
  }

  return (
    <div className="relative">
      <div
        ref={scrollerRef}
        role="tablist"
        aria-label="视图切换"
        className="flex gap-1 overflow-x-auto rounded-xl bg-muted p-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        {tabs.map((t, i) => {
          const selected = t.key === active;
          const Icon = t.icon;
          return (
            <button
              key={t.key}
              ref={(el) => {
                refs.current[t.key] = el;
                if (el && selected) el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
              }}
              role="tab"
              id={`${idBase}-tab-${t.key}`}
              aria-selected={selected}
              aria-controls={`${idBase}-panel-${t.key}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => onChange(t.key)}
              onKeyDown={(e) => onKeyDown(e, i)}
              className={cn(
                'inline-flex min-h-[38px] shrink-0 items-center gap-1.5 rounded-lg px-3.5 py-2 text-sm font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40',
                selected ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:bg-card/60 hover:text-foreground',
              )}
            >
              {Icon ? <Icon className="h-4 w-4" /> : null}
              {t.label}
            </button>
          );
        })}
      </div>
      {/* 边缘渐隐提示：仍可横向滚动时出现，告诉用户还有更多 Tab */}
      {edges.left ? (
        <div aria-hidden className="pointer-events-none absolute inset-y-1 left-0 w-6 rounded-l-xl bg-gradient-to-r from-muted to-transparent" />
      ) : null}
      {edges.right ? (
        <div aria-hidden className="pointer-events-none absolute inset-y-1 right-0 w-6 rounded-r-xl bg-gradient-to-l from-muted to-transparent" />
      ) : null}
    </div>
  );
}

export function TabPanel<K extends string>({
  idBase,
  tabKey,
  active,
  children,
}: {
  idBase: string;
  tabKey: K;
  active: K;
  children: ReactNode;
}) {
  if (tabKey !== active) return null;
  return (
    <div
      role="tabpanel"
      id={`${idBase}-panel-${tabKey}`}
      aria-labelledby={`${idBase}-tab-${tabKey}`}
      tabIndex={0}
      className="focus:outline-none"
    >
      {children}
    </div>
  );
}
