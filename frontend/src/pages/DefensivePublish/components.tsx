/**
 * 三页共用的小件。核心只有一件事:**服务端文案缺失时如实留白,不编一句顶上**。
 *
 * 🔴 为什么不给一个「兜底中文」
 * ------------------------------------------------
 * 兜底中文会让「后端忘了补译」表现成「界面上有一句看着挺正常的话」——
 * 于是没有任何东西会因此变红,缺口永远发现不了。
 * 留白 + `data-server-copy-missing` 则相反:用户看到的是一个明确的「—」,
 * 判据可以数 `[data-server-copy-missing]` 的个数,后端补齐那天它自然归零。
 */

import type { ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { serverCopy, type TypedAction } from './contracts';

/** 后端没下发文案时给用户看的占位。**不是任何服务端概念的译名**,只是一个「这里空着」。 */
const MISSING_PLACEHOLDER = '—';

export function ServerLabel({
    value, className, missingHint = '这一项的说明后端暂未下发',
}: { value: unknown; className?: string; missingHint?: string }) {
    const verdict = serverCopy(value);
    if (verdict.ok) return <span className={className}>{verdict.text}</span>;
    return (
        <span
            className={cn('text-muted-foreground', className)}
            data-server-copy-missing={verdict.why}
            title={missingHint}
        >
            {MISSING_PLACEHOLDER}
        </span>
    );
}

/**
 * 唯一出口按钮。§0.5.6:任何阻塞都必须自带解决方案 ——
 * 所以 action 存在时**一定**渲染一个按钮;只是文案缺失时禁用并说明,
 * 而不是悄悄不渲染(不渲染 = 用户面前真的没有出口了,那更糟)。
 */
export function ServerActionButton({
    action, onAct, variant = 'default', className, disabled,
}: {
    action: TypedAction | null | undefined;
    onAct: (action: TypedAction) => void;
    variant?: 'default' | 'outline' | 'secondary' | 'ghost' | 'destructive' | 'link';
    className?: string;
    disabled?: boolean;
}) {
    if (!action) return null;
    const verdict = serverCopy(action.label);
    if (!verdict.ok) {
        return (
            <div className="space-y-1" data-server-copy-missing={verdict.why}>
                <Button variant={variant} className={className} disabled>
                    {MISSING_PLACEHOLDER}
                </Button>
                <p className="flex items-start gap-1 text-xs text-amber-700">
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                    <span>这一步的按钮文案后端没有下发，暂时点不了。请联系平台客服。</span>
                </p>
            </div>
        );
    }
    return (
        <Button
            variant={variant}
            className={className}
            disabled={disabled}
            onClick={() => onAct(action)}
        >
            {verdict.text}
        </Button>
    );
}

/** 一行「标题 / 值」。值缺失走 ServerLabel 的留白口径。 */
export function Field({
    label, children, className,
}: { label: string; children: ReactNode; className?: string }) {
    return (
        <div className={cn('flex flex-col gap-0.5', className)}>
            <span className="text-xs text-muted-foreground">{label}</span>
            <span className="text-sm font-medium">{children}</span>
        </div>
    );
}

/** 页面级错误条。文案统一走 `formatApiErrorForDisplay`,不在各页各写一套。 */
export function ErrorNotice({ text }: { text: string }) {
    return (
        <div
            role="alert"
            className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
        >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <span>{text}</span>
        </div>
    );
}
