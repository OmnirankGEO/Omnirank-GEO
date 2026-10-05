/**
 * InfoBadgeRow · 图标 inline 陈列 + 各自独立 hover/tap 解释 · CTO-15.23 Density System v1
 *
 * 老板 5/22 反馈:"徽章图例" 文案生涩难懂 · button 包 popover 让用户不知道里面什么
 * 改用:图标直接陈列 + 每个有自己的 tooltip(PC hover / mobile tap)· 用户看到就懂
 *
 * 对比 InfoLegendPopover(本组件的反向):
 * - InfoLegendPopover:1 button "图例(N)" → 点开 popover 看竖排所有 badge + 说明
 * - InfoBadgeRow:N 个 badge inline 陈列 + 每个 hover/tap 看自己的 说明
 *
 * 用哪个看场景:
 * - 当 badge ≤ 6 个 + 每个 badge 视觉本身有语义(如 V / GEO / 🔥 性价比):用 InfoBadgeRow
 * - 当 badge ≥ 7 个 + label 必读才能理解(如 rules / 计费规则):用 InfoLegendPopover
 *
 * 交互:
 * - PC:mouseenter 0.15s 延迟后弹 tooltip · mouseleave 立即收
 * - Mobile:tap 切换 tooltip · 再 tap badge 自身或外部收
 * - tooltip 自动靠右/靠左(若靠边)防屏溢出 · 默认 below + start
 * - 每 badge button 自带 cursor-help · 视觉提示可交互
 *
 * 用法:
 *   <InfoBadgeRow
 *     items={[
 *       { key: 'v', badge: <span>V</span>, label: '权威媒体', description: '权威媒体认证 · 高公信力新闻源' },
 *       { key: 'geo', badge: <span>GEO</span>, label: '被 AI 引擎引用', description: 'GEO 调研观测' },
 *       ...
 *     ]}
 *   />
 */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '@/lib/utils';

export interface InfoBadgeItem {
  key: string;
  /** 预渲染 badge 节点 · 例:<span>V</span> / <GeoEngineBadge/> */
  badge: ReactNode;
  /** 简短 label · 例:"权威媒体" · tooltip 标题用 */
  label: string;
  /** 详细说明 · 例:"权威媒体认证 · 高公信力新闻源" · tooltip 正文 */
  description?: string;
}

export interface InfoBadgeRowProps {
  items: InfoBadgeItem[];
  /** 容器 gap · 默认 gap-2 */
  gap?: 'gap-1' | 'gap-1.5' | 'gap-2' | 'gap-2.5' | 'gap-3';
  className?: string;
  /** [#199] 是否在每个 chip 旁显示**可见**短标签。默认 false —— 只在需要的地方开。 */
  showLabel?: boolean;
}

/** tooltip 估算宽度 · 用于智能 align + clamp 决策 */
const TOOLTIP_W_MAX = 280;
const TOOLTIP_W_MIN = 200;
const VIEWPORT_MARGIN = 8;
const ARROW_OFFSET = 12; // 箭头距 popover 左/右边缘距离 · 跟 left-3 / right-3 对齐

type PopoverAlign = 'start' | 'center' | 'end';

interface PopoverPos {
  top: number;
  left: number;
  width: number;        // 实际 popover width(根据 viewport clamp)
  arrowLeft: number;    // 箭头距 popover 左边缘的距离 · 跟 chip center 对齐
  align: PopoverAlign;
}

/**
 * 单 badge + tooltip · 内部组件
 *
 * 老板 5/22 反馈演进:
 * - v1 居中 left-1/2 :边界 chip 溢出
 * - v2 智能 align(start/center/end):仍被 Sheet/Dialog overflow-hidden 容器 clip 切
 * - v3(本版) Portal + fixed 定位:tooltip 脱出任何 overflow-hidden 父容器 · 用 viewport 坐标
 *
 * 行为:
 * - PC:mouseenter 150ms 后弹 · mouseleave 立即收
 * - Mobile:tap toggle · click-outside / touch-outside 收
 * - 滚动/resize:自动收(防位置失效)
 * - 箭头跟随 chip center · popover 主体 viewport-clamped 防溢出
 */
function BadgePopoverItem({ item, showLabel = false }: { item: InfoBadgeItem; showLabel?: boolean }) {
  const [open, setOpen] = useState(false);
  const [touchMode, setTouchMode] = useState(false);
  const [pos, setPos] = useState<PopoverPos | null>(null);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);

  /* 关闭 popover · 清状态 */
  const close = () => {
    setOpen(false);
    setTouchMode(false);
    setPos(null);
  };

  /* tap (mobile) 弹出后 · click-outside / touchstart-outside 自动收 */
  useEffect(() => {
    if (!open || !touchMode) return;
    const handler = (e: MouseEvent | TouchEvent) => {
      const target = e.target as Node;
      const inBtn = buttonRef.current?.contains(target);
      const inPop = popoverRef.current?.contains(target);
      if (!inBtn && !inPop) close();
    };
    document.addEventListener('mousedown', handler);
    document.addEventListener('touchstart', handler);
    return () => {
      document.removeEventListener('mousedown', handler);
      document.removeEventListener('touchstart', handler);
    };
  }, [open, touchMode]);

  /* 滚动/resize · 自动收 · 防 fixed 位置变得失效 */
  useEffect(() => {
    if (!open) return;
    const handler = () => close();
    /* capture: true · 捕获任何祖先滚动(Sheet/Dialog/正文) */
    window.addEventListener('scroll', handler, true);
    window.addEventListener('resize', handler);
    return () => {
      window.removeEventListener('scroll', handler, true);
      window.removeEventListener('resize', handler);
    };
  }, [open]);

  /* unmount 清 timer */
  useEffect(() => () => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
  }, []);

  /** 用 chip viewport 位置 + viewport 宽度 · 计算 popover fixed 坐标 + clamp */
  const computePos = (): PopoverPos | null => {
    const btn = buttonRef.current;
    if (!btn || typeof window === 'undefined') return null;
    const rect = btn.getBoundingClientRect();
    const viewportW = window.innerWidth;
    const chipCenter = rect.left + rect.width / 2;

    /* 先决定 align(start/center/end)· 给箭头位置参考 */
    let align: PopoverAlign;
    const halfMax = TOOLTIP_W_MAX / 2;
    if (chipCenter - halfMax < VIEWPORT_MARGIN) align = 'start';
    else if (chipCenter + halfMax > viewportW - VIEWPORT_MARGIN) align = 'end';
    else align = 'center';

    /* popover 实际 width · viewport 内 clamp · 双兜底 min/max */
    const availableW = viewportW - VIEWPORT_MARGIN * 2;
    const width = Math.max(TOOLTIP_W_MIN, Math.min(TOOLTIP_W_MAX, availableW));

    /* 候选 left · 先按 align 计算 · 再 viewport clamp 防超界 */
    let left: number;
    if (align === 'start') left = rect.left;
    else if (align === 'end') left = rect.right - width;
    else left = chipCenter - width / 2;

    /* 双向 clamp · 即使 align 算错也保证不超 viewport */
    left = Math.max(VIEWPORT_MARGIN, Math.min(left, viewportW - width - VIEWPORT_MARGIN));

    /* 箭头永远跟 chip center · popover 主体可能因 clamp 位移 · 箭头要回正
     * arrowLeft = chipCenter - left · 即箭头距 popover 左缘多远 */
    const arrowLeftRaw = chipCenter - left;
    /* 箭头位置也 clamp · 防箭头落在 popover 圆角外(留 8px 边距) */
    const arrowLeft = Math.max(ARROW_OFFSET, Math.min(arrowLeftRaw, width - ARROW_OFFSET));

    return {
      top: rect.bottom + 6,
      left,
      width,
      arrowLeft,
      align,
    };
  };

  const openWithPos = () => {
    const p = computePos();
    if (!p) return;
    setPos(p);
    setOpen(true);
  };

  /* open 后 · popover 渲染完 · re-measure 实际 popover height/width · 仅 dev 用(可选)
   * 当前 width 已由 clamp 决定 · height 自然撑开 · 不需 re-measure */
  useLayoutEffect(() => {
    if (!open) return;
    /* 二次确认 popover 没超出右屏(若 description 极短 popover 实际窄于 width) · skip · width 已 clamp */
  }, [open]);

  const handleMouseEnter = () => {
    if (touchMode) return;
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    hoverTimer.current = setTimeout(openWithPos, 150);
  };

  const handleMouseLeave = () => {
    if (touchMode) return;
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    close();
  };

  const handleClick = () => {
    setTouchMode(true);
    if (open) {
      close();
    } else {
      openWithPos();
    }
  };

  return (
    <div
      className="relative inline-block"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      <button
        ref={buttonRef}
        type="button"
        onClick={handleClick}
        className="inline-flex items-center cursor-help focus:outline-none focus:ring-2 focus:ring-primary/40 focus:ring-offset-1 rounded"
        aria-label={`${item.label}${item.description ? '·' + item.description : ''}`}
        aria-expanded={open}
      >
        {item.badge}
        {/*
          * 🔴 [#199] 可见短标签。改前这一行只有四个光秃秃的 chip
          *    (`V` / `GEO` / `🤖6/6` / `🔥`),名字和说明**全在 hover/tap 弹层里** ——
          *    鼠标不放上去就永远不知道它们是什么,而手机上根本没有 hover。
          *    长说明仍留弹层;这里只加**短标签**,一眼能认。
          * 🔴 默认 `false`:别处的 InfoBadgeRow 形状不变(只此处开)。
          */}
        {showLabel && (
          <span className="ml-1 text-[10px] text-muted-foreground whitespace-nowrap">{item.label}</span>
        )}
      </button>

      {/* Portal 到 body · 脱出任何 overflow-hidden 父容器(Sheet/Dialog/Card)· 老板 5/22 v3 修 */}
      {open && pos && typeof document !== 'undefined' && createPortal(
        <div
          ref={popoverRef}
          role="tooltip"
          style={{
            position: 'fixed',
            top: pos.top,
            left: pos.left,
            width: pos.width,
            zIndex: 9999, // 高于绝大多数 Sheet/Dialog
          }}
          className={cn(
            'rounded-lg border border-border bg-popover px-3 py-2 shadow-lg',
            'animate-in fade-in zoom-in-95 duration-150',
          )}
          onClick={(e) => e.stopPropagation()}
        >
          {/* 箭头 · 用 inline style 精确 px · 跟 chip center 对齐 · 不被 popover clamp 拽走 */}
          <div
            style={{ left: pos.arrowLeft, marginLeft: -4 }}
            className="absolute -top-1 h-2 w-2 rotate-45 bg-popover border-l border-t border-border"
          />

          <div className="text-info-secondary font-semibold mb-0.5">{item.label}</div>
          {item.description && (
            <div className="text-info-tertiary leading-snug">{item.description}</div>
          )}
        </div>,
        document.body,
      )}
    </div>
  );
}

export function InfoBadgeRow({ items, gap = 'gap-2', className, showLabel = false }: InfoBadgeRowProps) {
  if (!items.length) return null;
  return (
    <div className={cn('inline-flex flex-wrap items-center', gap, className)}>
      {items.map((item) => (
        <BadgePopoverItem key={item.key} item={item} showLabel={showLabel} />
      ))}
    </div>
  );
}
