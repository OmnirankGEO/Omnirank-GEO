export interface CoachTargetRect {
    top: number;
    left: number;
    width: number;
    height: number;
}

export interface CoachViewport {
    top: number;
    left: number;
    right: number;
    bottom: number;
    width: number;
    height: number;
}

export function getCoachViewport(): CoachViewport {
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft ?? 0;
    const top = viewport?.offsetTop ?? 0;
    const width = viewport?.width ?? window.innerWidth;
    const height = viewport?.height ?? window.innerHeight;
    return {
        top,
        left,
        right: left + width,
        bottom: top + height,
        width,
        height,
    };
}

export function resolveCoachTarget(
    wrapper: HTMLElement | null | undefined,
    selector?: string,
): HTMLElement | null {
    const firstUsable = (candidates: Iterable<Element>): HTMLElement | null => {
        for (const candidate of candidates) {
            if (!(candidate instanceof HTMLElement) || !isCoachTargetActionable(candidate)) continue;
            const rect = candidate.getBoundingClientRect();
            if (rect.width >= 1 && rect.height >= 1) return candidate;
        }
        return null;
    };

    if (selector) {
        try {
            const selected = firstUsable(document.querySelectorAll(selector));
            if (selected) return selected;
        } catch {
            return null;
        }
    }
    if (!wrapper) return null;
    const explicit = firstUsable(wrapper.querySelectorAll('[data-coach-target="true"]'));
    if (explicit) return explicit;
    const interactiveSelector = [
        'button:not([disabled])',
        'a[href]',
        'input:not([disabled])',
        'textarea:not([disabled])',
        'select:not([disabled])',
        '[role="button"]:not([aria-disabled="true"])',
    ].join(',');
    const interactive = wrapper.matches(interactiveSelector)
        ? firstUsable([wrapper])
        : firstUsable(wrapper.querySelectorAll(interactiveSelector));
    return interactive || (firstUsable([wrapper]) ? wrapper : null);
}

export function isCoachTargetActionable(element: HTMLElement | null): boolean {
    if (!element || !element.isConnected) return false;
    const style = window.getComputedStyle(element);
    if (
        style.display === 'none'
        || style.visibility === 'hidden'
        || style.pointerEvents === 'none'
        || Number(style.opacity) === 0
    ) {
        return false;
    }
    if (
        (element instanceof HTMLButtonElement
            || element instanceof HTMLInputElement
            || element instanceof HTMLTextAreaElement
            || element instanceof HTMLSelectElement)
        && element.disabled
    ) {
        return false;
    }
    return element.getAttribute('aria-disabled') !== 'true';
}

export function measureCoachTarget(element: HTMLElement | null): CoachTargetRect | null {
    if (!isCoachTargetActionable(element)) return null;
    const rect = element!.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return null;
    return {
        top: rect.top,
        left: rect.left,
        width: rect.width,
        height: rect.height,
    };
}

export function coachRectVisibleFraction(rect: CoachTargetRect | null): number {
    if (!rect) return 0;
    const viewport = getCoachViewport();
    const right = rect.left + rect.width;
    const bottom = rect.top + rect.height;
    const visibleWidth = Math.max(0, Math.min(right, viewport.right) - Math.max(rect.left, viewport.left));
    const visibleHeight = Math.max(0, Math.min(bottom, viewport.bottom) - Math.max(rect.top, viewport.top));
    const area = rect.width * rect.height;
    return area > 0 ? (visibleWidth * visibleHeight) / area : 0;
}

export function sameCoachRect(
    left: CoachTargetRect | null,
    right: CoachTargetRect | null,
    tolerance = 0.5,
): boolean {
    if (!left || !right) return left === right;
    return (
        Math.abs(left.top - right.top) <= tolerance
        && Math.abs(left.left - right.left) <= tolerance
        && Math.abs(left.width - right.width) <= tolerance
        && Math.abs(left.height - right.height) <= tolerance
    );
}
