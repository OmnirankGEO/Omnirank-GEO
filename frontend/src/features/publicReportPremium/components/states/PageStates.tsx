/**
 * states — 页面五态中的四态(ready 由 ReportPage 渲染)
 *
 *  - loading:稳定骨架(与正文布局同构，避免 CLS)
 *  - not_ready:客户报告尚未就绪(绝不展示伪报告或 0 分)
 *  - invalid_link:链接失效(与 not_ready 明确区分，无重试)
 *  - error:人话错误 + 重试；不露内部异常/数据库信息/用户 ID
 */
import { AlertTriangle, FileClock, Link2Off, RefreshCw } from 'lucide-react';

function StateShell({
    icon,
    title,
    children,
}: {
    icon: React.ReactNode;
    title: string;
    children: React.ReactNode;
}) {
    return (
        <div className="flex min-h-[70vh] flex-col items-center justify-center bg-slate-50 px-6 py-16 text-center">
            <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full border border-slate-200 bg-white">
                {icon}
            </div>
            <h1 className="text-lg font-semibold text-slate-800">{title}</h1>
            <div className="mt-2 max-w-md space-y-3 text-sm leading-6 text-slate-500">{children}</div>
        </div>
    );
}

export function LoadingSkeleton() {
    return (
        <div aria-busy="true" aria-label="报告加载中" className="min-h-screen bg-slate-50">
            {/* 与 HeroSummary 同构的骨架，防布局跳动 */}
            <div className="bg-slate-900">
                <div className="mx-auto w-full max-w-[1320px] px-4 py-8 sm:px-6 sm:py-10 lg:px-10">
                    <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_auto]">
                        <div className="space-y-4">
                            <div className="h-3 w-32 animate-pulse rounded bg-slate-700 motion-reduce:animate-none" />
                            <div className="h-9 w-3/5 animate-pulse rounded bg-slate-700 motion-reduce:animate-none" />
                            <div className="h-4 w-2/5 animate-pulse rounded bg-slate-800 motion-reduce:animate-none" />
                            <div className="h-5 w-4/5 animate-pulse rounded bg-slate-800 motion-reduce:animate-none" />
                            <div className="mt-6 flex gap-8 border-t border-slate-800 pt-5">
                                <div className="h-10 w-16 animate-pulse rounded bg-slate-800 motion-reduce:animate-none" />
                                <div className="h-10 w-16 animate-pulse rounded bg-slate-800 motion-reduce:animate-none" />
                                <div className="h-10 w-16 animate-pulse rounded bg-slate-800 motion-reduce:animate-none" />
                            </div>
                        </div>
                        <div className="hidden items-center gap-5 xl:flex">
                            <div className="h-36 w-36 animate-pulse rounded-full bg-slate-800 motion-reduce:animate-none" />
                            <div className="space-y-2">
                                <div className="h-3 w-20 animate-pulse rounded bg-slate-800 motion-reduce:animate-none" />
                                <div className="h-8 w-24 animate-pulse rounded bg-slate-700 motion-reduce:animate-none" />
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <div className="mx-auto w-full max-w-[1320px] space-y-8 px-4 py-10 sm:px-6 lg:px-10">
                {[0, 1, 2].map((i) => (
                    <div key={i} className="space-y-3">
                        <div className="h-6 w-40 animate-pulse rounded bg-slate-200 motion-reduce:animate-none" />
                        <div className="h-28 w-full animate-pulse rounded-lg bg-slate-200/70 motion-reduce:animate-none" />
                    </div>
                ))}
            </div>
        </div>
    );
}

export function NotReadyState({ message, onRetry }: { message: string; onRetry: () => void }) {
    return (
        <StateShell
            icon={<FileClock className="h-6 w-6 text-amber-500" aria-hidden="true" />}
            title="客户报告尚未就绪"
        >
            <p>{message}</p>
            <p className="text-xs text-slate-400">报告就绪后，本页会自动展示完整内容；当前不会展示任何半成品数据。</p>
            <button
                type="button"
                onClick={onRetry}
                className="inline-flex min-h-11 items-center gap-1.5 rounded-md bg-slate-900 px-5 text-sm font-medium text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600"
            >
                <RefreshCw className="h-4 w-4" aria-hidden="true" />
                刷新看看
            </button>
        </StateShell>
    );
}

export function InvalidLinkState({ message }: { message: string }) {
    return (
        <StateShell
            icon={<Link2Off className="h-6 w-6 text-slate-400" aria-hidden="true" />}
            title="链接无效或已过期"
        >
            <p>{message}</p>
            <p className="text-xs text-slate-400">这可能是链接被撤销、过期或复制不完整；刷新无法解决。</p>
        </StateShell>
    );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
    return (
        <StateShell
            icon={<AlertTriangle className="h-6 w-6 text-orange-500" aria-hidden="true" />}
            title="报告暂时打不开"
        >
            <p>{message}</p>
            <button
                type="button"
                onClick={onRetry}
                className="inline-flex min-h-11 items-center gap-1.5 rounded-md bg-slate-900 px-5 text-sm font-medium text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600"
            >
                <RefreshCw className="h-4 w-4" aria-hidden="true" />
                重新加载
            </button>
        </StateShell>
    );
}
