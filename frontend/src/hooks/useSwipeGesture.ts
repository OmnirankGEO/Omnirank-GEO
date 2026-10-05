import { useEffect } from 'react';

/** PWA standalone 模式下浏览器返回手势已禁用，可以从边缘触发 */
const isStandalone =
  typeof window !== 'undefined' &&
  (window.matchMedia('(display-mode: standalone)').matches ||
   (navigator as any).standalone === true);

function isInteractiveSwipeTarget(target: EventTarget | null) {
  if (!(target instanceof Element)) return false;
  return Boolean(target.closest(
    'input, textarea, select, button, a, label, [role="button"], [role="link"], [contenteditable="true"], [data-sidebar-swipe-ignore]',
  ));
}

/**
 * Detects left/right swipe gestures for sidebar control.
 *
 * PWA standalone 模式: 从左边缘 0-24px 触发（无浏览器返回手势冲突）
 * 普通浏览器: 从 30-80px 区域触发，避开浏览器原生 ~20px 返回手势区
 */
export function useSwipeGesture(
  onSwipeRight: () => void,
  onSwipeLeft: () => void,
) {
  useEffect(() => {
    let startX = 0;
    let startY = 0;
    let isEdgeSwipe = false;
    let isMultiTouch = false;
    let isInteractiveTarget = false;
    let observedMoveX = 0;
    let observedMoveY = 0;
    // [CTO-15.23 2026-05-21 v8] 垂直主导锁定 · 防上下 scroll 误触发 sidebar overlay
    // 老板报 v7 部署后:pinch zoom 已修 · 但上下滑动还会出现 sidebar overlay
    // 真根因:用户单指上下 scroll 时 · touchmove 过程中略有水平偏移
    // → touchend 时 deltaX = 70 / deltaY = 40 · ratio 1.75 > 1.5(原阈值)· 仍命中 swipe
    // → 触发 onSwipeRight · sidebar overlay 显示 = 滑动时色块
    let isVerticalDominant = false;

    const onTouchStart = (e: TouchEvent) => {
      // 多指 gesture(pinch zoom)· 标记本次 touch 序列不参与 swipe
      if (e.touches.length > 1) {
        isMultiTouch = true;
        isEdgeSwipe = false;
        isInteractiveTarget = false;
        observedMoveX = 0;
        observedMoveY = 0;
        isVerticalDominant = false;
        return;
      }
      isMultiTouch = false;
      isVerticalDominant = false;
      isInteractiveTarget = isInteractiveSwipeTarget(e.target);
      observedMoveX = 0;
      observedMoveY = 0;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      if (isInteractiveTarget) {
        isEdgeSwipe = false;
        return;
      }
      // PWA: 边缘 0-40px; 浏览器: 30-80px（避开原生返回手势）
      isEdgeSwipe = isStandalone ? startX < 40 : (startX > 20 && startX < 80);
    };

    const onTouchMove = (e: TouchEvent) => {
      if (isInteractiveTarget) return;
      // touchstart 后又出现多指 · 标记
      if (e.touches.length > 1) {
        isMultiTouch = true;
        isEdgeSwipe = false;
        return;
      }
      // 实时检测垂直主导 · 一旦判定为 vertical scroll · 锁定后续 touchend 不触发 swipe
      // 阈值:垂直移动 > 24px 且 > 水平移动 1.2 倍 = vertical scroll
      const curX = e.touches[0].clientX;
      const curY = e.touches[0].clientY;
      const moveX = Math.abs(curX - startX);
      const moveY = Math.abs(curY - startY);
      observedMoveX = Math.max(observedMoveX, moveX);
      observedMoveY = Math.max(observedMoveY, moveY);
      if (moveY > 24 && moveY > moveX * 1.2) {
        isVerticalDominant = true;
      }
    };

    const onTouchEnd = (e: TouchEvent) => {
      if (isInteractiveTarget) {
        isInteractiveTarget = false;
        return;
      }
      // 多指 gesture / 垂直 scroll · 跳过 swipe 计算
      if (isMultiTouch || isVerticalDominant) {
        isMultiTouch = false;
        isVerticalDominant = false;
        return;
      }
      // 文件选择器/系统相册等交互可能只有 touchstart/touchend,没有真实 touchmove。
      // 这类序列不能被当作 sidebar swipe,否则会刷出移动端遮罩容器。
      if (observedMoveX < 40 || observedMoveX <= observedMoveY * 1.5) {
        return;
      }
      const deltaX = e.changedTouches[0].clientX - startX;
      const deltaY = e.changedTouches[0].clientY - startY;

      // 严格 horizontal swipe 判定:
      // - |deltaX| > 60px (水平距离够长)
      // - |deltaX| > |deltaY| * 2.5 (水平占绝对主导 · 之前 1.5 太宽松)
      // - |deltaY| < 40px (绝对垂直偏移上限 · 防斜向滑动)
      if (
        Math.abs(deltaX) > 60 &&
        Math.abs(deltaX) > Math.abs(deltaY) * 2.5 &&
        Math.abs(deltaY) < 40
      ) {
        if (deltaX > 0 && isEdgeSwipe) onSwipeRight(); // Right swipe from edge → open
        else if (deltaX < 0) onSwipeLeft(); // Left swipe anywhere → close
      }
    };

    document.addEventListener('touchstart', onTouchStart, { passive: true });
    document.addEventListener('touchmove', onTouchMove, { passive: true });
    document.addEventListener('touchend', onTouchEnd, { passive: true });
    return () => {
      document.removeEventListener('touchstart', onTouchStart);
      document.removeEventListener('touchmove', onTouchMove);
      document.removeEventListener('touchend', onTouchEnd);
    };
  }, [onSwipeRight, onSwipeLeft]);
}
