/**
 * 引导气泡卡片 (TipCard) · 跟随 spotlight 目标元素定位
 * Stage 1 Batch 2.5+ (2026-05-07)
 *
 * 跟独立的 FeatureTooltip 区别:
 *   FeatureTooltip 是 wrap children 模式 (内部维护 ref)
 *   OnboardingTipCard 是外部 ref 模式 (跟 OnboardingSpotlight 共用同一个 ref)
 *
 * 用途: 给 OnboardingSpotlight 高亮区配一个跟随定位的引导气泡 ·
 * 视觉默认贴在 target 元素左上方 · 气泡内含 标题/正文/返回上一步/跳过此步
 *
 * 跟随机制 (跟 OnboardingSpotlight 同源):
 *   - 50ms x 20 次重试拿 ref (兼容 tab 切换 / 异步 mount)
 *   - resize / scroll(capture) / ResizeObserver 实时跟随
 */
import { ReactNode, RefObject, useEffect, useState } from 'react';

const CARD_WIDTH = 320;
const CARD_HEIGHT_ESTIMATE = 160; // 估值, 用于上方放不下时回退
const GAP = 12;

interface Props {
  /** 跟踪的目标元素 ref · 跟同步显示的 OnboardingSpotlight 共用 */
  targetRef: RefObject<HTMLElement | null>;
  /** 是否激活 (跟 spotlight 同步开关) */
  active: boolean;
  /** 卡片标题 (橙色) */
  title: string;
  /** 卡片正文 */
  content: ReactNode;
  /** 返回上一步回调 (可选, 不传不显示) */
  onBack?: () => void;
  /** 跳过此步回调 (可选, 不传不显示) */
  onSkip?: () => void;
  /** 跳过按钮文字, 默认 "跳过此步"; 可改成 "保存并下一步" 等更主动的措辞 */
  skipLabel?: string;
}

export function OnboardingTipCard({ targetRef, active, title, content, onBack, onSkip, skipLabel = '跳过此步' }: Props) {
  const [rect, setRect] = useState<DOMRect | null>(null);

  useEffect(() => {
    if (!active) {
      setRect(null);
      return;
    }
    let cancelled = false;
    let retries = 0;
    let retryTimer: number | null = null;
    const sync = () => {
      if (cancelled) return;
      const el = targetRef.current;
      if (el) {
        setRect(el.getBoundingClientRect());
      } else if (retries < 20) {
        retries += 1;
        retryTimer = window.setTimeout(sync, 50);
      }
    };
    sync();
    const onChange = () => {
      if (targetRef.current) setRect(targetRef.current.getBoundingClientRect());
    };
    window.addEventListener('resize', onChange);
    window.addEventListener('scroll', onChange, true);
    const ro = new ResizeObserver(onChange);
    if (targetRef.current) ro.observe(targetRef.current);
    return () => {
      cancelled = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      window.removeEventListener('resize', onChange);
      window.removeEventListener('scroll', onChange, true);
      ro.disconnect();
    };
  }, [active, targetRef]);

  if (!active || !rect) return null;

  // 计算卡片位置:贴 target 元素左上方
  // 优先放在 target 上方且左对齐;若上方空间不够则放下方;若超出右侧则贴右边界
  const vw = typeof window !== 'undefined' ? window.innerWidth : 1920;
  const vh = typeof window !== 'undefined' ? window.innerHeight : 1080;
  const fitsTop = rect.top >= CARD_HEIGHT_ESTIMATE + GAP;
  const cardTop = fitsTop
    ? rect.top - CARD_HEIGHT_ESTIMATE - GAP
    : Math.min(rect.bottom + GAP, vh - CARD_HEIGHT_ESTIMATE - 8);
  // left 对齐 target.left, 但保证不超左/右边界
  const cardLeft = Math.max(8, Math.min(rect.left, vw - CARD_WIDTH - 8));

  return (
    <div
      className="fixed z-50 p-4 bg-popover border border-amber-500/40 rounded-xl shadow-2xl"
      style={{ left: cardLeft, top: Math.max(8, cardTop), width: CARD_WIDTH }}
      role="dialog"
      aria-label={title}
    >
      <div className="text-sm font-semibold text-amber-600 dark:text-amber-400">{title}</div>
      <div className="text-xs text-muted-foreground mt-1 leading-5">{content}</div>
      {(onBack || onSkip) && (
        <div className="border-t mt-3 pt-2 flex items-center justify-between">
          {onBack ? (
            <button
              type="button"
              onClick={onBack}
              className="text-xs text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
            >
              <span aria-hidden>←</span>返回上一步
            </button>
          ) : <span />}
          {onSkip ? (
            <button
              type="button"
              onClick={onSkip}
              className="text-xs text-muted-foreground hover:text-foreground transition-colors"
            >
              {skipLabel}
            </button>
          ) : <span />}
        </div>
      )}
    </div>
  );
}

export default OnboardingTipCard;
