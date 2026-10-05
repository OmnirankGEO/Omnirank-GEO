/**
 * 体检发起页顶部三步进度条(工单 §1.1)。
 *
 * 🔴 **只是位置指示,不是向导**:不拦提交、不禁用任何字段、点它也不跳步。
 *    用户随时可以先填第 3 步再回头补第 1 步 —— 这条页面本来就不是分步表单。
 *
 * 当前步由已填内容派生(`resolveLaunchStep`),纯函数,单独导出好锁。
 */
import { Check } from 'lucide-react';
import { cn } from '@/lib/utils';
import { LAUNCH_STEPS } from './launchSteps';

export { LAUNCH_STEPS, resolveLaunchStep } from './launchSteps';
export type { LaunchStepInput } from './launchSteps';

export function LaunchStepBar({ current }: { current: number }) {
    return (
        <ol
            data-testid="launch-step-bar"
            aria-label="发起流程位置"
            className="flex items-center gap-2 @md:gap-3 overflow-hidden rounded-xl border border-border bg-card px-4 py-3"
        >
            {LAUNCH_STEPS.map((step, idx) => {
                const done = step.id < current;
                const active = step.id === current;
                return (
                    <li
                        key={step.id}
                        data-testid={`launch-step-${step.id}`}
                        data-state={done ? 'done' : active ? 'active' : 'todo'}
                        aria-current={active ? 'step' : undefined}
                        className="flex min-w-0 flex-1 items-center gap-2"
                    >
                        <span
                            className={cn(
                                'flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold transition-colors',
                                active && 'bg-foreground text-background',
                                done && 'bg-brand/15 text-brand',
                                !active && !done && 'border border-border text-muted-foreground/60',
                            )}
                        >
                            {done ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : step.id}
                        </span>
                        <span
                            className={cn(
                                'truncate text-sm',
                                active ? 'font-medium text-foreground' : 'text-muted-foreground',
                            )}
                        >
                            {step.label}
                        </span>
                        {idx < LAUNCH_STEPS.length - 1 && (
                            <span
                                aria-hidden="true"
                                className={cn(
                                    'ml-1 hidden h-px min-w-4 flex-1 @sm:block',
                                    done ? 'bg-brand/40' : 'bg-border',
                                )}
                            />
                        )}
                    </li>
                );
            })}
        </ol>
    );
}
