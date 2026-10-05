/**
 * 功能气泡 (Feature Tooltip)
 *
 * 行为:
 *   1. 仅在 isAvailable && welcome_choice ∈ {'start','later'} 时显示
 *   2. 对应 step 已完成 OR 已跳过 → 不再显示 (passed)
 *   3. 用户点 dismissFeatureTooltip(featureId) → 永久不再显示
 *   4. 启用 spotlight (默认 true) → 整屏 70% 黑蒙层 + 目标元素挖洞 + amber 高亮 ring
 *   5. 蒙层只承担视觉聚焦，不拦截页面或目标点击
 *   6. 气泡按 visualViewport 与实际卡片尺寸自动避让
 *   7. 目标缺失、隐藏、禁用或持续移动时给出重新定位和跳过出口
 *
 * 通用性: step 1-4 任意按钮指引, 只换 featureId / stepId / title / content
 */
import { ReactNode, useEffect, useRef, useState, useLayoutEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useOnboarding } from '@/context/OnboardingContext';
import { ONBOARDING_STEPS } from './OnboardingStepDefinitions';
import { OnboardingSpotlight } from './OnboardingSpotlight';
import { SkipConfirmDialog } from './SkipConfirmDialog';
import { notifyTooltipShown, notifyTooltipHidden } from './tooltipActiveStore';
import { useSandboxState } from '@/sandbox/sandboxState';
import { useScreenshotMode } from '@/sandbox/screenshotMode';
import { useIsMobile } from '@/hooks/use-mobile';
import { MobileCoachMark } from './mobile/MobileCoachMark';
import {
    coachRectVisibleFraction,
    getCoachViewport,
    measureCoachTarget,
    resolveCoachTarget,
    sameCoachRect,
    type CoachTargetRect,
} from './coachTarget';

export type FeatureTooltipSide = 'top' | 'bottom' | 'left' | 'right';

export interface FeatureTooltipProps {
    /** 唯一标识, 持久化到 dismissed_features */
    featureId: string;
    /** 关联的 step_id, 用于读 canSkip / 已完成 / 已跳过 状态 */
    stepId: string;
    /** 气泡标题 */
    title: string;
    /** 气泡正文 */
    content: string;
    /** 期望气泡方向, 默认 bottom; 实际方向会根据 viewport 自动避让 */
    side?: FeatureTooltipSide;
    /** 是否启用全屏蒙层 (默认 true, 本期统一开) */
    spotlight?: boolean;
    /** 可选: "返回上一步" 回调; 不传则不显示该按钮 */
    onBack?: () => void;
    /** 可选: 临时禁用整个引导(不显示 spotlight + tooltip), 仅渲染 children · 用于路径冲突场景 */
    disabled?: boolean;
    /** 可选: 信息型 spotlight 的"下一步/我知道了"按钮文案 · 不传不显示
     *  用于不需要用户点 children 就能推进的引导步骤 (如纯介绍性 spotlight) */
    nextLabel?: string;
    /** 可选: nextLabel 按钮的点击回调 · 通常用来推进 tutorial stage */
    onNext?: () => void;
    /** 可选: 包裹层 span 的 className · 默认 'relative inline-block'
     *  当包裹的是 block 级元素 (Card / 大区域 div 等) 时, 传 'relative block' 避免被
     *  inline-block 收缩成内容宽度, 撑满父级 */
    wrapClassName?: string;
    /** 可选: spotlight 挖洞的内边距 · 默认 6px · 可按边设置(如下拉/弹层展开时把 bottom 调大, 让洞罩住可点区域) */
    spotlightPadding?: number | { top?: number; right?: number; bottom?: number; left?: number };
    /** 可选: 所有视口都优先用 selector 定位真实目标，避免 wrapper 在 flex/sticky 布局里测错 */
    targetSelector?: string;
    /** 旧调用点兼容；新代码请使用 targetSelector */
    mobileTargetSelector?: string;
    /** 被包裹的触发元素 (按钮等) */
    children: ReactNode;
}

const TOOLTIP_WIDTH = 280;
const GAP = 12; // 气泡到 target 的间距 (px)
const VIEWPORT_MARGIN = 8;

/** 根据 target rect 选最宽裕的 side · 优先用 preferredSide, 不够位再回退 */
function pickBestSide(
    rect: CoachTargetRect,
    preferred: FeatureTooltipSide,
    cardHeight: number,
): FeatureTooltipSide {
    const viewport = getCoachViewport();
    const rectRight = rect.left + rect.width;
    const rectBottom = rect.top + rect.height;
    const spaceTop = rect.top - viewport.top;
    const spaceBottom = viewport.bottom - rectBottom;
    const spaceLeft = rect.left - viewport.left;
    const spaceRight = viewport.right - rectRight;

    const fits = {
        top: spaceTop >= cardHeight + GAP,
        bottom: spaceBottom >= cardHeight + GAP,
        left: spaceLeft >= TOOLTIP_WIDTH + GAP,
        right: spaceRight >= TOOLTIP_WIDTH + GAP,
    };

    if (fits[preferred]) return preferred;
    // 优先级: bottom → top → right → left
    const order: FeatureTooltipSide[] = ['bottom', 'top', 'right', 'left'];
    for (const s of order) {
        if (fits[s]) return s;
    }
    // 都不够位 · 取空间最大的那个 (避免 undefined)
    const all: Array<[FeatureTooltipSide, number]> = [
        ['top', spaceTop],
        ['bottom', spaceBottom],
        ['left', spaceLeft],
        ['right', spaceRight],
    ];
    all.sort((a, b) => b[1] - a[1]);
    return all[0][0];
}

/** 根据 side + target rect 计算气泡卡片 fixed 坐标 */
function computeCardStyle(
    rect: CoachTargetRect,
    side: FeatureTooltipSide,
    cardHeight: number,
): React.CSSProperties {
    const viewport = getCoachViewport();
    const width = Math.min(TOOLTIP_WIDTH, viewport.width - VIEWPORT_MARGIN * 2);
    const rectRight = rect.left + rect.width;
    const rectBottom = rect.top + rect.height;
    const clampLeft = (left: number) => Math.max(
        viewport.left + VIEWPORT_MARGIN,
        Math.min(viewport.right - width - VIEWPORT_MARGIN, left),
    );
    const clampTop = (top: number) => Math.max(
        viewport.top + VIEWPORT_MARGIN,
        Math.min(viewport.bottom - cardHeight - VIEWPORT_MARGIN, top),
    );
    switch (side) {
        case 'top': {
            return {
                position: 'fixed',
                left: clampLeft(rect.left + rect.width / 2 - width / 2),
                top: clampTop(rect.top - GAP - cardHeight),
                width,
            };
        }
        case 'left': {
            return {
                position: 'fixed',
                left: clampLeft(rect.left - GAP - width),
                top: clampTop(rect.top + rect.height / 2 - cardHeight / 2),
                width,
            };
        }
        case 'right': {
            return {
                position: 'fixed',
                left: clampLeft(rectRight + GAP),
                top: clampTop(rect.top + rect.height / 2 - cardHeight / 2),
                width,
            };
        }
        case 'bottom':
        default: {
            return {
                position: 'fixed',
                left: clampLeft(rect.left + rect.width / 2 - width / 2),
                top: clampTop(rectBottom + GAP),
                width,
            };
        }
    }
}

export function FeatureTooltip({
    featureId,
    stepId,
    title,
    content,
    side = 'bottom',
    spotlight = true,
    onBack,
    disabled = false,
    nextLabel,
    onNext,
    wrapClassName = 'relative inline-block',
    spotlightPadding = 6,
    targetSelector,
    mobileTargetSelector,
    children,
}: FeatureTooltipProps) {
    const {
        state,
        isAvailable,
        isFeatureDismissed,
        isStepCompleted,
        isStepSkipped,
        skipStep,
    } = useOnboarding();
    // Stage 1 Batch 4 (2026-05-18) · 沙盒态下绕过 welcome_choice 检查
    // 沙盒本身就是 entry point · 不需要老的"欢迎弹窗选择"前置状态
    const { isSandbox } = useSandboxState();
    // 2026-05-23 截图模式 · 所有 spotlight / tooltip 一律不弹
    const screenshotMode = useScreenshotMode();
    // [2026-05-27] 移动端走 MobileCoachMark (sheet + 挖洞) · PC 走原 spotlight + 浮动气泡
    const isMobile = useIsMobile();

    // 包裹层 ref · 用于拿 children 渲染后的 rect (放在 children 外, 不强求 children 接 ref)
    const wrapRef = useRef<HTMLSpanElement>(null);
    // [2026-05-27] 移动端用 state 同步 wrapRef.current · 让 MobileCoachMark useEffect 能在 ref attach 时重测
    // PC 端继续用 wrapRef.current(updateRect 没改)· 0 行为变化
    const [wrapEl, setWrapEl] = useState<HTMLSpanElement | null>(null);
    const cardRef = useRef<HTMLDivElement>(null);
    const setRefs = useCallback((node: HTMLSpanElement | null) => {
        (wrapRef as React.MutableRefObject<HTMLSpanElement | null>).current = node;
        setWrapEl(node);
    }, []);
    const [rect, setRect] = useState<CoachTargetRect | null>(null);
    const [cardHeight, setCardHeight] = useState(150);
    const [targetUnavailable, setTargetUnavailable] = useState(false);
    const [locateGeneration, setLocateGeneration] = useState(0);
    const [bestSide, setBestSide] = useState<FeatureTooltipSide>(side);
    const [skipDialogOpen, setSkipDialogOpen] = useState(false);
    // [2026-05-27] 移动端 sheet open 状态 · onClose 关 sheet · shouldShow 切换时重 open
    const [mobileSheetOpen, setMobileSheetOpen] = useState(false);

    const step = ONBOARDING_STEPS.find(s => s.step_id === stepId);
    const passed = isStepCompleted(stepId) || isStepSkipped(stepId);

    // 沙盒态: 教程可反复重走 · 每个气泡显隐由调用点的 tutorialStage 精确控制
    // 不能再被"步骤已完成/已跳过/已 dismiss"这类持久标记二次拦截 (否则走过一遍后重走就没引导了)
    const shouldShow =
        !disabled &&
        !screenshotMode &&  // 截图模式 · 一律不显
        isAvailable &&
        // 沙盒态 OR 老 welcome_choice 流程 (后者兼容)
        (isSandbox || state.welcome_choice === 'start' || state.welcome_choice === 'later') &&
        (isSandbox || (!isFeatureDismissed(featureId) && !passed));

    const effectiveTargetSelector = targetSelector || mobileTargetSelector;

    // 同一条测量链同时驱动 spotlight 和 tooltip，防止两层各测各的产生点击缝隙。
    const updateRect = useCallback(() => {
        const target = resolveCoachTarget(wrapRef.current, effectiveTargetSelector);
        const measured = measureCoachTarget(target);
        if (!measured || coachRectVisibleFraction(measured) < 0.35) {
            setRect(null);
            return false;
        }
        setRect(current => sameCoachRect(current, measured) ? current : measured);
        setBestSide(pickBestSide(measured, side, cardHeight));
        setTargetUnavailable(false);
        return true;
    }, [cardHeight, effectiveTargetSelector, side]);

    useLayoutEffect(() => {
        if (!shouldShow) {
            setRect(null);
            setTargetUnavailable(false);
            return;
        }
        let cancelled = false;
        let raf = 0;
        let stableFrames = 0;
        let lastRect: CoachTargetRect | null = null;
        const target = resolveCoachTarget(wrapRef.current, effectiveTargetSelector);
        if (target) {
            const measured = measureCoachTarget(target);
            if (!measured || coachRectVisibleFraction(measured) < 0.6) {
                target.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' });
            }
        }
        const tick = () => {
            if (cancelled) return;
            const currentTarget = resolveCoachTarget(wrapRef.current, effectiveTargetSelector);
            const measured = measureCoachTarget(currentTarget);
            if (sameCoachRect(lastRect, measured)) stableFrames += 1;
            else stableFrames = 0;
            lastRect = measured;
            updateRect();
            if (stableFrames < 4) raf = window.requestAnimationFrame(tick);
        };
        raf = window.requestAnimationFrame(tick);
        const unavailableTimer = window.setTimeout(() => {
            if (!cancelled && !updateRect()) setTargetUnavailable(true);
        }, 1200);
        return () => {
            cancelled = true;
            window.cancelAnimationFrame(raf);
            window.clearTimeout(unavailableTimer);
        };
    }, [effectiveTargetSelector, locateGeneration, shouldShow, updateRect]);

    useEffect(() => {
        if (!shouldShow) return;
        const remeasure = () => { updateRect(); };
        window.addEventListener('resize', remeasure);
        window.addEventListener('scroll', remeasure, true);
        window.visualViewport?.addEventListener('resize', remeasure);
        window.visualViewport?.addEventListener('scroll', remeasure);
        const ro = new ResizeObserver(updateRect);
        const target = resolveCoachTarget(wrapRef.current, effectiveTargetSelector);
        if (target) ro.observe(target);
        const mo = new MutationObserver(remeasure);
        mo.observe(document.body, {
            attributes: true,
            childList: true,
            subtree: true,
            attributeFilter: ['class', 'style', 'disabled', 'aria-disabled'],
        });
        return () => {
            window.removeEventListener('resize', remeasure);
            window.removeEventListener('scroll', remeasure, true);
            window.visualViewport?.removeEventListener('resize', remeasure);
            window.visualViewport?.removeEventListener('scroll', remeasure);
            ro.disconnect();
            mo.disconnect();
        };
    }, [effectiveTargetSelector, shouldShow, updateRect]);

    useLayoutEffect(() => {
        if (!cardRef.current) return;
        const nextHeight = Math.max(96, cardRef.current.getBoundingClientRect().height);
        if (Math.abs(nextHeight - cardHeight) > 0.5) setCardHeight(nextHeight);
    }, [cardHeight, content, rect, title]);

    // 通知全局 store · 让 OnboardingChecklist 听见自动折叠成圆球, 避免视觉重叠
    useEffect(() => {
        if (!shouldShow) return;
        notifyTooltipShown();
        return () => {
            notifyTooltipHidden();
        };
    }, [shouldShow]);

    // [2026-05-27] 移动端 sheet open 同步 shouldShow · 切 stage 时重弹
    useEffect(() => {
        setMobileSheetOpen(shouldShow);
    }, [shouldShow]);

    const handleSkipClick = () => {
        setSkipDialogOpen(true);
    };

    const handleSkipConfirm = () => {
        skipStep(stepId);
        setSkipDialogOpen(false);
    };

    // ref 本身要随时挂着 · 即使 shouldShow=false 也要让外部能正常 mount children
    // Stage 1 Batch 4 (2026-05-18) · 用 createPortal 把蒙层 + 气泡挂到 document.body
    // 防止父容器有 z-index 层叠上下文 (如 sticky bottom + z-10) 把 z-50 困住
    const portalContent = shouldShow && rect ? (
        <>
            {/* 整屏蒙层 + 目标挖洞 (仅 spotlight=true 时) */}
            {spotlight && (
                <OnboardingSpotlight
                    targetRect={rect}
                    active
                    padding={spotlightPadding}
                />
            )}

            {/* 气泡卡片 · z-50 与 ring 同层 · 略高于蒙层 z-40 */}
            <div
                ref={cardRef}
                role="dialog"
                aria-label={title}
                data-coach-card
                /*
                 * @@R@@ [#194] 把 `featureId` 挂到卡片上 —— 判据要能**归因**。
                 *    没有它,G2 只能问「屏幕上有没有引导卡」,而这一问会被**别的阶段**的卡满足:
                 *    「点这个项目卡进入」那张只由 `!selectedProject` 控制、与阶段无关,
                 *    于是我一度让 6 个 step3 阶段一起报绿,而它们各自的引导一张都没出现。
                 *    (同族:a-string-anchor-satisfied-by-an-unrelated-line)
                 */
                data-feature-id={featureId}
                style={computeCardStyle(rect, bestSide, cardHeight)}
                className="z-50 p-3 bg-popover text-popover-foreground border border-amber-500/40 rounded-lg shadow-2xl"
            >
                            <h4 className="text-sm font-semibold text-amber-600 dark:text-amber-400">
                                {title}
                            </h4>
                            <p className="text-xs text-muted-foreground mt-1 leading-5 whitespace-pre-line">
                                {content}
                            </p>
                            <div className="flex items-center justify-between mt-3 pt-2 border-t border-border gap-2">
                                {/* 左下: 返回上一步 (传了 onBack 才显示) */}
                                {onBack ? (
                                    <button
                                        type="button"
                                        onClick={onBack}
                                        className="text-xs text-muted-foreground hover:text-foreground transition-colors cursor-pointer focus:outline-none focus:underline flex items-center gap-1"
                                    >
                                        <span aria-hidden="true">←</span>返回上一步
                                    </button>
                                ) : <span />}
                                <div className="flex items-center gap-2">
                                    {/* 跳过此步 / 必做 */}
                                    {step?.canSkip ? (
                                        <button
                                            type="button"
                                            onClick={handleSkipClick}
                                            className="text-xs text-muted-foreground hover:text-foreground transition-colors cursor-pointer focus:outline-none focus:underline"
                                        >
                                            跳过此步
                                        </button>
                                    ) : (
                                        <span
                                            title="这步是后续流程的基础, 没法跳过"
                                            className="text-[10px] text-muted-foreground/60 cursor-not-allowed"
                                        >
                                            必做
                                        </span>
                                    )}
                                    {/* 下一步按钮 (信息型 spotlight · 不需要点 children 就能推进) */}
                                    {nextLabel && onNext && (
                                        <button
                                            type="button"
                                            onClick={onNext}
                                            className="text-xs font-medium px-2.5 py-1 rounded bg-amber-500 text-white hover:bg-amber-600 transition-colors focus:outline-none focus:ring-2 focus:ring-amber-500 focus:ring-offset-1"
                                        >
                                            {nextLabel}
                                        </button>
                                    )}
                                </div>
                            </div>
                        </div>
        </>
    ) : null;

    const recoveryContent = shouldShow && targetUnavailable ? (
        <div
            role="dialog"
            aria-label={`${title} · 定位恢复`}
            data-coach-recovery
            className="fixed bottom-4 left-1/2 z-[160] w-[min(24rem,calc(100vw-2rem))] -translate-x-1/2 rounded-lg border border-amber-500/40 bg-popover p-4 text-popover-foreground shadow-2xl"
        >
            <h4 className="text-sm font-semibold">{title}</h4>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
                当前按钮还没有准备好，可能正在切换页面或加载内容。你可以重新定位，也可以先跳过这一步。
            </p>
            <div className="mt-3 flex justify-end gap-2">
                <button
                    type="button"
                    onClick={() => {
                        setTargetUnavailable(false);
                        setLocateGeneration(value => value + 1);
                    }}
                    className="rounded border border-border px-3 py-1.5 text-xs hover:bg-muted"
                >
                    重新定位
                </button>
                <button
                    type="button"
                    onClick={handleSkipConfirm}
                    className="rounded bg-amber-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-600"
                >
                    跳过此步
                </button>
            </div>
        </div>
    ) : null;

    // [2026-05-27 fix] 移动端按钮跟 PC 强引导对齐:
    //   canSkip → "跳过此步" 走 SkipConfirmDialog 二次确认
    //   不 canSkip + 没 onNext → 强引导 · 不给关闭按钮(MobileCoachMark closeLabel=null)
    //                            sheet 也不可下拉关 · 用户必须点高亮目标推进
    //   有 onNext → 渲染 "下一步" 主按钮(MobileCoachMark 内部处理)
    //   降级 fallback(目标找不到) → MobileCoachMark 强制 "知道了" 防卡死(无视 closeLabel=null)
    const mobileCloseLabel: string | null = (step?.canSkip || isSandbox) ? '跳过此步' : null;
    const handleMobileClose = () => {
        if (step?.canSkip) {
            setSkipDialogOpen(true);
        }
        setMobileSheetOpen(false);
    };

    return (
        <>
            <span ref={setRefs} className={wrapClassName}>
                {children}
            </span>

            {/* 移动端走 MobileCoachMark · PC 端走原 Portal spotlight + 浮动气泡
                shouldShow=true 时只渲染其中一个 · isMobile 切换 */}
            {isMobile && shouldShow && (
                <MobileCoachMark
                    open={mobileSheetOpen}
                    targetEl={effectiveTargetSelector ? null : wrapEl}
                    targetSelector={effectiveTargetSelector}
                    title={title}
                    description={content}
                    onClose={handleMobileClose}
                    onNext={onNext}
                    nextLabel={nextLabel}
                    closeLabel={mobileCloseLabel}
                    onRetry={() => setLocateGeneration(value => value + 1)}
                    /* [2026-05-27] 透传 spotlightPadding · PC 用 {bottom:48} 包 dropdown · 移动端也需要 */
                    padding={spotlightPadding}
                />
            )}

            {/* Portal 到 document.body · 跳出任何父级 z-index 层叠上下文
                isMobile=true 时 portalContent 还是会算出来但不渲染 · 避免 PC/Mobile 双显 */}
            {!isMobile && portalContent && typeof document !== 'undefined' && createPortal(portalContent, document.body)}
            {!isMobile && recoveryContent && typeof document !== 'undefined' && createPortal(recoveryContent, document.body)}

            {/* 跳过二次确认弹窗 · 也用 portal (Dialog 内部已用 portal 但保险起见) */}
            {shouldShow && step && (
                <SkipConfirmDialog
                    open={skipDialogOpen}
                    onOpenChange={setSkipDialogOpen}
                    onConfirm={handleSkipConfirm}
                    dangerous={!!step.skipDangerous}
                    stepLabel={step.label}
                />
            )}
        </>
    );
}

export default FeatureTooltip;
