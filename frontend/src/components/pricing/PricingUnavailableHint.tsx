/**
 * #199 · 「价目读不到」时的**同一处实现**(第 11 条第 4 款:不可选必须有原因 + 出口)。
 *
 * ## 现场
 *
 * 三处逐字同形的写法(`Monitoring/index.tsx`、`Brand/BrandDetailPage.tsx` 两处):
 * 价目为 null ⇒ 按钮禁用、文案「价目不可用」,**原因只在 `title` 里**(hover 才看得到,
 * 手机上根本没有 hover),而且**没有任何出口** —— 用户只能自己猜要不要刷新页面。
 *
 * ## 改法
 *
 * 按钮文案改成「价目读取失败」(说清是"读"失败,不是"这功能没有价"),
 * 按钮**外面**一行可见原因 + 一个真的能点的「重试」。
 *
 * 🔴 **不许出现点了没反应的按钮**。`PricingContext.refresh()` 在退避窗内是
 *    **静默不发**的;所以这里读 `retryNotBefore`,窗内把出口写成「N 秒后可重试」并禁用。
 *    给一颗此刻什么都不做的「重试」,比不给更糟 —— 用户点完只会以为系统坏了。
 *
 * 🔴 一处谓词一处实现:三处共用这一个组件。原来那三份各自写一遍,
 *    改一处就会漏两处(本仓反复栽过)。
 */
import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

export interface PricingUnavailableHintProps {
    /** `usePricing().error` 的原话;没有就用兜底那句。 */
    error?: string | null;
    /** `usePricing().retryNotBefore`(ms 时间戳;0 = 不在退避窗内)。 */
    retryNotBefore?: number;
    /** `usePricing().refresh` */
    onRetry: () => void;
    className?: string;
}

/** 按钮上的文字(三处共用,判据据此钉「价目不可用」四个字不再出现)。 */
export const PRICING_UNAVAILABLE_LABEL = '价目读取失败';

export function PricingUnavailableHint({
    error, retryNotBefore = 0, onRetry, className,
}: PricingUnavailableHintProps) {
    /*
     * 🔴 退避窗要**走着变**:只在渲染那一刻算一次的话,窗过了按钮还是灰的,
     *    用户得刷新页面才发现能点了 —— 那又是一次"自己猜"。
     */
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        if (!retryNotBefore || retryNotBefore <= Date.now()) return;
        const id = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(id);
    }, [retryNotBefore]);

    const waitMs = Math.max(0, (retryNotBefore || 0) - now);
    const waiting = waitMs > 0;
    const seconds = Math.ceil(waitMs / 1000);

    return (
        <div className={cn('flex flex-wrap items-center gap-2 text-xs', className)}
            data-testid="pricing-unavailable">
            {/* 🔴 原因画在**外面**,不在 title 里:手机上没有 hover */}
            <span className="text-muted-foreground" data-testid="pricing-unavailable-reason">
                {(error && error.trim()) || '动态价目没有读到'}
            </span>
            <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-6 px-2 text-xs"
                data-testid="pricing-unavailable-retry"
                disabled={waiting}
                onClick={() => { if (!waiting) onRetry(); }}
            >
                {waiting ? null : <RefreshCw className="mr-1 size-3" aria-hidden="true" />}
                {waiting ? `${seconds} 秒后可重试` : '重试'}
            </Button>
        </div>
    );
}

export default PricingUnavailableHint;
