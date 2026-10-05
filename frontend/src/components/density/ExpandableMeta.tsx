/**
 * ExpandableMeta · 长 remark / reason / description 默认折叠 · CTO-15.23 Density System v1
 *
 * 老板红线:OnlineQuoteFlow / DiagnosisReport / PublishCenter 把 recommendation_reason /
 * remark / description 直接 line-clamp-2 塞行内 · text-[10px] 不可读
 *
 * 设计:
 * - 默认显示前 N 字短摘要(默认 30 chars)
 * - 文本 >N 时显示"看详情" link · hover/tap 展开 popover 看全文
 * - 整体走 --info-tertiary token · 不抢主信息
 * - 可选 icon prefix · 默认带 MessageSquareText
 *
 * Phase 4.1(2026-05-22 Codex 九审修):
 * - Portal + fixed 定位 · 脱出 table 的 overflow-x-auto 等 clipping 父容器
 * - 加 PC hover 150ms 自动弹(原只 click)· 跟 InfoBadgeRow v3 同样行为
 * - 智能 align(测 trigger viewport 位置 + viewport width clamp)
 * - 移动 tap toggle · click-outside / touch-outside / scroll / resize 自动收
 *
 * 用法 1 · inline 短摘要 + 看详情(preview):
 *   <ExpandableMeta text={kw.recommendation_reason} maxChars={30} />
 *
 * 用法 2 · 自定义 trigger(在表格行末尾的 icon 按钮):
 *   <ExpandableMeta
 *     text={m.remark}
 *     mode="iconOnly"
 *     iconLabel="备注"
 *   />
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { MessageSquareText, X } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface ExpandableMetaProps {
  /** 完整文本 */
  text?: string | null;
  /** 短摘要字数上限 · 默认 30 */
  maxChars?: number;
  /** 显示模式 · inline 默认 / iconOnly 只显 icon 按钮 / preview 短摘要 + "看详情" link */
  mode?: 'inline' | 'iconOnly' | 'preview';
  /** label 前缀 · 例:"备注" "推荐理由" "原因" */
  iconLabel?: string;
  /** 自定义 icon · 默认 MessageSquareText */
  icon?: ReactNode;
  /** 空文本 fallback · 默认显示 "-" */
  emptyFallback?: ReactNode;
  /** Popover 对齐 · start=左对齐 / end=右对齐 / 不传则智能决定 · Phase 4.1 改成 hint · 实际 clamp 由组件智能 */
  align?: 'start' | 'end';
  className?: string;
}

const POPOVER_W_MAX = 420;
const POPOVER_W_MIN = 240;
const VIEWPORT_MARGIN = 8;
const HOVER_OPEN_DELAY = 150;
const HOVER_CLOSE_DELAY = 200;

type PopoverAlign = 'start' | 'center' | 'end';

interface PopoverPos {
  top: number;
  left: number;
  width: number;
}

/** 决定 align · trigger viewport 位置 + 调用方 hint · Phase 4.1 智能 clamp */
function computePopoverPos(
  triggerRect: DOMRect,
  hintAlign: 'start' | 'end' | undefined,
): PopoverPos {
  const viewportW = typeof window !== 'undefined' ? window.innerWidth : 1280;
  const availableW = viewportW - VIEWPORT_MARGIN * 2;
  const width = Math.max(POPOVER_W_MIN, Math.min(POPOVER_W_MAX, availableW));
  const triggerCenter = triggerRect.left + triggerRect.width / 2;

  /* 默认 align 判定 · 若调用方有 hint 用 hint · 否则按 trigger 位置自动选 */
  let align: PopoverAlign;
  if (hintAlign === 'end') align = 'end';
  else if (hintAlign === 'start') align = 'start';
  else {
    const halfW = width / 2;
    if (triggerCenter - halfW < VIEWPORT_MARGIN) align = 'start';
    else if (triggerCenter + halfW > viewportW - VIEWPORT_MARGIN) align = 'end';
    else align = 'center';
  }

  /* left 候选 */
  let left: number;
  if (align === 'start') left = triggerRect.left;
  else if (align === 'end') left = triggerRect.right - width;
  else left = triggerCenter - width / 2;

  /* 双向 clamp · 防 hint 跟实际不一致仍超 viewport */
  left = Math.max(VIEWPORT_MARGIN, Math.min(left, viewportW - width - VIEWPORT_MARGIN));

  return {
    top: triggerRect.bottom + 6,
    left,
    width,
  };
}

export function ExpandableMeta({
  text,
  maxChars = 30,
  mode = 'preview',
  iconLabel = '详情',
  icon,
  emptyFallback = <span className="text-info-tertiary opacity-50">-</span>,
  align,
  className,
}: ExpandableMetaProps) {
  const [open, setOpen] = useState(false);
  const [touchMode, setTouchMode] = useState(false);
  const [pos, setPos] = useState<PopoverPos | null>(null);
  const openTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const triggerRef = useRef<HTMLDivElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);

  const close = () => {
    setOpen(false);
    setTouchMode(false);
    setPos(null);
    if (openTimer.current) clearTimeout(openTimer.current);
    if (closeTimer.current) clearTimeout(closeTimer.current);
  };

  const openWithPos = () => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    /* iconOnly mode 默认 align=end · preview 默认按位置智能选 · 显式传 align 尊重调用方 */
    const hintAlign = align ?? (mode === 'iconOnly' ? 'end' : undefined);
    setPos(computePopoverPos(rect, hintAlign));
    setOpen(true);
  };

  /* tap (mobile) 弹出后 · click-outside / touchstart-outside 自动收 */
  useEffect(() => {
    if (!open || !touchMode) return;
    const handler = (e: MouseEvent | TouchEvent) => {
      const target = e.target as Node;
      const inTrigger = triggerRef.current?.contains(target);
      const inPop = popoverRef.current?.contains(target);
      if (!inTrigger && !inPop) close();
    };
    document.addEventListener('mousedown', handler);
    document.addEventListener('touchstart', handler);
    return () => {
      document.removeEventListener('mousedown', handler);
      document.removeEventListener('touchstart', handler);
    };
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [open, touchMode]);

  /* 滚动/resize · 自动收 · 防 fixed 位置变得失效 */
  useEffect(() => {
    if (!open) return;
    const handler = () => close();
    window.addEventListener('scroll', handler, true);
    window.addEventListener('resize', handler);
    return () => {
      window.removeEventListener('scroll', handler, true);
      window.removeEventListener('resize', handler);
    };
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [open]);

  /* unmount 清 timer */
  useEffect(() => () => {
    if (openTimer.current) clearTimeout(openTimer.current);
    if (closeTimer.current) clearTimeout(closeTimer.current);
  }, []);

  if (!text || !text.trim()) return <>{emptyFallback}</>;

  const isLong = text.length > maxChars;
  const summary = isLong ? text.slice(0, maxChars).trim() + '…' : text;

  /* PC hover handlers · touchMode 锁防 iOS mouseenter 误触 */
  const handleMouseEnter = () => {
    if (touchMode) return;
    if (closeTimer.current) clearTimeout(closeTimer.current);
    if (openTimer.current) clearTimeout(openTimer.current);
    openTimer.current = setTimeout(openWithPos, HOVER_OPEN_DELAY);
  };

  const handleMouseLeave = () => {
    if (touchMode) return;
    if (openTimer.current) clearTimeout(openTimer.current);
    closeTimer.current = setTimeout(() => {
      setOpen(false);
      setPos(null);
    }, HOVER_CLOSE_DELAY);
  };

  /* popover 自己 hover 时取消 close timer · 让用户从 trigger 移到 popover 上不关 */
  const handlePopoverMouseEnter = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
  };

  const handlePopoverMouseLeave = () => {
    if (touchMode) return;
    closeTimer.current = setTimeout(() => {
      setOpen(false);
      setPos(null);
    }, HOVER_CLOSE_DELAY);
  };

  const handleClick = () => {
    setTouchMode(true);
    if (openTimer.current) clearTimeout(openTimer.current);
    if (closeTimer.current) clearTimeout(closeTimer.current);
    if (open) {
      close();
    } else {
      openWithPos();
    }
  };

  // Mode: iconOnly · 只渲染一个 icon button(给表格末列用)
  if (mode === 'iconOnly') {
    return (
      <>
        <div
          ref={triggerRef}
          className={cn('relative inline-block', className)}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        >
          <button
            type="button"
            onClick={handleClick}
            className="inline-flex h-7 w-7 items-center justify-center rounded hover:bg-muted/40 text-muted-foreground hover:text-foreground transition-colors"
            aria-label={iconLabel}
            title={iconLabel}
            aria-expanded={open}
          >
            {icon ?? <MessageSquareText className="h-3.5 w-3.5" />}
          </button>
        </div>
        <PortalPopover
          open={open}
          pos={pos}
          text={text}
          iconLabel={iconLabel}
          popoverRef={popoverRef}
          onClose={close}
          onMouseEnter={handlePopoverMouseEnter}
          onMouseLeave={handlePopoverMouseLeave}
        />
      </>
    );
  }

  // Mode: preview · 短摘要 + "看详情" link
  if (mode === 'preview') {
    if (!isLong) return <span className={cn('text-info-tertiary', className)}>{text}</span>;
    return (
      <>
        <div
          ref={triggerRef}
          className={cn('inline-flex items-baseline gap-1 max-w-full relative', className)}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        >
          <span className="text-info-tertiary break-words leading-snug min-w-0">{summary}</span>
          <button
            type="button"
            onClick={handleClick}
            /* 不混用 text-info-tertiary + text-blue-400 · token 后 import 会覆盖 · 单独 blue + size override */
            className="shrink-0 text-[12px] font-medium text-blue-400 hover:text-blue-300 hover:underline whitespace-nowrap"
            aria-expanded={open}
          >
            看详情
          </button>
        </div>
        <PortalPopover
          open={open}
          pos={pos}
          text={text}
          iconLabel={iconLabel}
          popoverRef={popoverRef}
          onClose={close}
          onMouseEnter={handlePopoverMouseEnter}
          onMouseLeave={handlePopoverMouseLeave}
        />
      </>
    );
  }

  // Mode: inline · 不截断 · 完整显示(适合短文本 + 已知容器够宽)
  return <span className={cn('text-info-tertiary break-words leading-snug', className)}>{text}</span>;
}

/** Portal 渲染的 popover · 脱出 overflow-hidden 父容器 · fixed viewport 坐标
 * Phase 4.1(Codex 九审修):原 absolute 定位被 table.overflow-x-auto clip · 改 Portal + fixed 解 */
function PortalPopover({
  open,
  pos,
  text,
  iconLabel,
  popoverRef,
  onClose,
  onMouseEnter,
  onMouseLeave,
}: {
  open: boolean;
  pos: PopoverPos | null;
  text: string;
  iconLabel: string;
  popoverRef: React.RefObject<HTMLDivElement>;
  onClose: () => void;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
}) {
  if (!open || !pos || typeof document === 'undefined') return null;
  return createPortal(
    <div
      ref={popoverRef}
      role="tooltip"
      style={{
        position: 'fixed',
        top: pos.top,
        left: pos.left,
        width: pos.width,
        zIndex: 9999,
      }}
      className={cn(
        'rounded-lg border border-border bg-popover p-3 shadow-lg',
        'animate-in fade-in zoom-in-95 duration-150',
      )}
      onClick={(e) => e.stopPropagation()}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      <div className="flex items-start justify-between gap-2 mb-2">
        <span className="text-info-tertiary uppercase tracking-wider font-semibold">{iconLabel}</span>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 p-0.5 rounded hover:bg-muted/40 text-muted-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <p className="text-info-secondary leading-relaxed whitespace-pre-wrap break-words">{text}</p>
    </div>,
    document.body,
  );
}
