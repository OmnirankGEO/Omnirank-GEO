/**
 * 教程聚光灯视觉层。
 *
 * 整屏 70% 黑色蒙层 + 目标元素挖洞 + amber 高亮 ring + 外发光
 *
 * 定位与重测由 FeatureTooltip 统一负责。这里不再铺点击拦截层，避免目标
 * 测量产生数像素偏差时吞掉真实按钮点击。
 */
import type { CoachTargetRect } from './coachTarget';

type SidePadding = { top?: number; right?: number; bottom?: number; left?: number };

interface SpotlightProps {
    targetRect: CoachTargetRect | null;
    active: boolean;
    padding?: number | SidePadding;
}

export function OnboardingSpotlight({ targetRect, active, padding = 6 }: SpotlightProps) {
    const pad = typeof padding === 'number'
        ? { top: padding, right: padding, bottom: padding, left: padding }
        : { top: padding.top ?? 6, right: padding.right ?? 6, bottom: padding.bottom ?? 6, left: padding.left ?? 6 };
    if (!active || !targetRect) return null;
    const rect = {
        ...targetRect,
        right: targetRect.left + targetRect.width,
        bottom: targetRect.top + targetRect.height,
    };

    // 2026-05-24 BUGFIX: subpixel rendering 缝隙 (洞下方出现白线)
    // DOMRect 坐标是 float, 4 块蒙层之间相邻边界 0.5-1px 露 body 背景色 → 白横线/竖线
    // 修法: 用 floor/ceil 配对让相邻蒙层有 1px 重叠 (洞稍微大 1px 不影响视觉)
    const holeLeft = Math.floor(rect.left - pad.left);
    const holeTop = Math.floor(rect.top - pad.top);
    const holeRight = Math.ceil(rect.right + pad.right);
    const holeBottom = Math.ceil(rect.bottom + pad.bottom);
    const holeWidth = holeRight - holeLeft;
    const holeHeight = holeBottom - holeTop;

    return (
        <>
            {/* === 视觉层 === 一个元素 + 巨型 box-shadow 一气覆盖 · 无 subpixel 缝隙 */}
            <div
                className="fixed z-40 pointer-events-none rounded-lg"
                style={{
                    left: holeLeft,
                    top: holeTop,
                    width: holeWidth,
                    height: holeHeight,
                    boxShadow: '0 0 0 9999px rgba(0, 0, 0, 0.55)',
                }}
                aria-hidden
            />
            {/* === 高亮描边 === ring 跟随 target 形状 · amber 边框 · 不拦截点击(让洞内 target 能点) */}
            <div
                className="fixed pointer-events-none ring-2 ring-amber-500 rounded-lg z-50"
                style={{
                    left: holeLeft,
                    top: holeTop,
                    width: holeWidth,
                    height: holeHeight,
                }}
                aria-hidden
            />
        </>
    );
}

export default OnboardingSpotlight;
