import { lazy, Suspense } from 'react';
import type { FeatureTooltipProps } from './FeatureTooltip';

const FeatureTooltip = lazy(() =>
    import('./FeatureTooltip').then((module) => ({ default: module.FeatureTooltip })),
);

/**
 * Keep the ordinary route render synchronous while loading the unchanged
 * tutorial spotlight only when that spotlight is actually active.
 */
export function LazyFeatureTooltip(props: FeatureTooltipProps) {
    if (props.disabled) return <>{props.children}</>;
    return (
        <Suspense fallback={<>{props.children}</>}>
            <FeatureTooltip {...props} />
        </Suspense>
    );
}

export default LazyFeatureTooltip;
