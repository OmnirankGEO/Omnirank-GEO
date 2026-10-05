/**
 * Onboarding Tooltip Active Store
 * Stage 1 Batch 3 (2026-05-08)
 *
 * 用途:
 *   FeatureTooltip 显示时, OnboardingChecklist 应自动收成圆球避免视觉重叠
 *
 * 实现:
 *   引用计数 + 自定义事件 · 任意 FeatureTooltip mount(shouldShow=true) 时 +1,
 *   unmount 或 shouldShow 转 false 时 -1 · 每次变化派 'onboarding:tooltip-changed' 事件
 *   订阅方 (OnboardingChecklist) 监听事件读 isAnyTooltipActive() 决定折叠
 */

let activeCount = 0;
const EVENT_NAME = 'onboarding:tooltip-changed';

export function notifyTooltipShown(): void {
    activeCount += 1;
    if (typeof window !== 'undefined') {
        window.dispatchEvent(new Event(EVENT_NAME));
    }
}

export function notifyTooltipHidden(): void {
    activeCount = Math.max(0, activeCount - 1);
    if (typeof window !== 'undefined') {
        window.dispatchEvent(new Event(EVENT_NAME));
    }
}

export function isAnyTooltipActive(): boolean {
    return activeCount > 0;
}

export const TOOLTIP_ACTIVE_EVENT = EVENT_NAME;
