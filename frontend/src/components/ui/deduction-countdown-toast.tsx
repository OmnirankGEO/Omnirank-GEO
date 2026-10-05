/**
 * DeductionCountdownToast — 大额扣费倒计时确认卡片
 *
 * `useConfirmLargeDeduction` 通过 sonner `toast.custom()` 渲染本组件，替代原来
 * 「一行裸文本 + 裸文字链接」的默认 toast 样式。
 *
 * 视觉遵循 v7 设计系统：Tailwind + shadcn，无 inline style / <style> 块；
 * accent 绿（`--brand`）只出现在左侧图标底与底部进度条 —— 面积远小于 5%。
 *
 * 倒计时是真动的：组件自持 100ms tick，秒数逐秒递减 + 底部进度条线性走完。
 *
 * v1 · 2026-07-27 · 微工单 WORKORDER_UI_DEDUCTION_TOAST
 */

import { useEffect, useState } from 'react';
import { Coins } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { cn } from '@/lib/utils';

/** 进度条刷新节奏：100ms 足够顺滑，5s 内只有 50 次 re-render */
const TICK_MS = 100;

interface DeductionCountdownToastProps {
    /** 主行动作文案，如「将扣 2,730 算力写 7 篇文章」 */
    label: string;
    /** 倒计时总时长（毫秒），由 hook 传入以免两处各写一个 5000 */
    durationMs: number;
    /** 点取消 → 立即 dismiss 并置 cancelled（不执行不扣费） */
    onCancel: () => void;
    className?: string;
}

export function DeductionCountdownToast({
    label,
    durationMs,
    onCancel,
    className,
}: DeductionCountdownToastProps) {
    const [remainingMs, setRemainingMs] = useState(durationMs);

    useEffect(() => {
        // 用「起始时间戳 + 真实经过时间」而不是逐次减 TICK_MS：
        // 标签页被挂起 / 主线程繁忙时不会累积漂移，跟 hook 里的 setTimeout 保持同步。
        const startedAt = Date.now();
        const timer = window.setInterval(() => {
            const left = Math.max(0, durationMs - (Date.now() - startedAt));
            setRemainingMs(left);
            if (left <= 0) window.clearInterval(timer);
        }, TICK_MS);
        return () => window.clearInterval(timer);
    }, [durationMs]);

    const secondsLeft = Math.ceil(remainingMs / 1000);
    const percentLeft = durationMs > 0 ? (remainingMs / durationMs) * 100 : 0;

    return (
        <div
            role="alert"
            aria-live="polite"
            data-testid="deduction-countdown-toast"
            className={cn(
                'w-full overflow-hidden rounded-xl border border-border bg-popover/95 text-popover-foreground shadow-lg backdrop-blur-sm',
                className,
            )}
        >
            <div className="flex items-start gap-3 px-4 py-3">
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-brand/10 text-brand">
                    <Coins className="size-4" aria-hidden="true" />
                </span>
                <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium leading-snug break-words">{label}</p>
                    <p
                        className="mt-0.5 text-xs text-muted-foreground tabular-nums"
                        data-testid="deduction-countdown-seconds"
                    >
                        {secondsLeft} 秒后自动执行
                    </p>
                </div>
                <Button
                    variant="outline"
                    size="sm"
                    className="shrink-0"
                    onClick={onCancel}
                    data-testid="deduction-countdown-cancel"
                >
                    取消
                </Button>
            </div>
            {/* 复用 shadcn Progress（inline style 封装在设计系统组件内部，本文件零 inline style）。
              * aria-hidden：进度条只是秒数的视觉重复，读屏由上面 role=alert/aria-live 的
              * "N 秒后自动执行" 播报；且 ui/progress.tsx 未把 value 透传给 Radix Root
              * （aria-valuenow 恒 0），暴露成 progressbar 反而是错误语义。 */}
            <Progress
                value={percentLeft}
                aria-hidden="true"
                data-testid="deduction-countdown-bar"
                className="h-1 rounded-none bg-muted [&>div]:bg-brand [&>div]:transition-[transform] [&>div]:duration-100 [&>div]:ease-linear"
            />
        </div>
    );
}
