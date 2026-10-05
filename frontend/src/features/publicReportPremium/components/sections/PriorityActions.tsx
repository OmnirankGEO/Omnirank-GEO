/**
 * PriorityActions — 优先行动(Top 3-5)
 * 每条：为什么做 / 对应证据(可跳证据区) / 预计影响范围 / 建议周期 / 可展开执行说明。
 * 不伪造确定性收益、天数或行业承诺 —— 文案全部来自后端。
 */
import { useState } from 'react';
import { ChevronDown, FileSearch } from 'lucide-react';
import { Card, EmptyBlock, Section } from '../ui/primitives';
import { MarkdownView } from '../markdown/MarkdownView';
import { MotionExpand } from '../motion/presets';
import type { DataSection, PriorityAction } from '../../contract/types';
import { cn } from '@/lib/utils';

const PRIORITY_STYLE: Record<string, string> = {
    P0: 'bg-orange-600 text-white',
    P1: 'bg-sky-600 text-white',
    P2: 'bg-slate-500 text-white',
};

/** 内部优先级代号 → 客户可读措辞(P0/P1/P2 不进客户面) */
const PRIORITY_DISPLAY: Record<string, string> = {
    P0: '优先处理',
    P1: '接着处理',
    P2: '持续优化',
};

function displayPriority(priorityLabel: string): string {
    return PRIORITY_DISPLAY[priorityLabel] ?? priorityLabel;
}

function ActionCard({
    action,
    index,
    evidenceRowKeys,
}: {
    action: PriorityAction;
    index: number;
    evidenceRowKeys: readonly string[];
}) {
    const [open, setOpen] = useState(false);
    const priorityClass = PRIORITY_STYLE[action.priorityLabel] ?? 'bg-slate-700 text-white';
    const linkedEvidence = action.evidenceRowKeys.filter((k) => evidenceRowKeys.includes(k));
    const expandable = Boolean(action.detailMd);

    return (
        <Card as="li" className="p-4 sm:p-5">
            <div className="flex items-start gap-3">
                <span className={cn('flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-sm font-bold', priorityClass)}>
                    {index + 1}
                </span>
                <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                        <h3 className="break-words text-base font-semibold text-slate-900">{action.title}</h3>
                        <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[11px] font-medium text-slate-500">
                            {displayPriority(action.priorityLabel)}
                        </span>
                    </div>
                    {action.why && (
                        <p className="mt-1.5 break-words text-sm leading-6 text-slate-600">{action.why}</p>
                    )}
                    <dl className="mt-3 grid gap-2 text-xs text-slate-500 sm:grid-cols-2">
                        {action.impactScope && (
                            <div className="flex gap-1.5">
                                <dt className="shrink-0 text-slate-400">影响范围</dt>
                                <dd className="break-words">{action.impactScope}</dd>
                            </div>
                        )}
                        {action.suggestedPeriod && (
                            <div className="flex gap-1.5">
                                <dt className="shrink-0 text-slate-400">建议周期</dt>
                                <dd className="break-words">{action.suggestedPeriod}</dd>
                            </div>
                        )}
                    </dl>
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                        {linkedEvidence.length > 0 && (
                            <a
                                href="#evidence"
                                className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-slate-200 px-2.5 text-xs font-medium text-slate-600 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-sky-600 print:hidden"
                            >
                                <FileSearch className="h-3.5 w-3.5" aria-hidden="true" />
                                查看对应证据({linkedEvidence.length})
                            </a>
                        )}
                        {expandable && (
                            <button
                                type="button"
                                onClick={() => setOpen((v) => !v)}
                                aria-expanded={open}
                                className="inline-flex min-h-9 items-center gap-1 rounded-md px-1 text-xs font-medium text-sky-700 hover:text-sky-900 focus-visible:outline-2 focus-visible:outline-sky-600"
                            >
                                {open ? '收起执行说明' : '展开执行说明'}
                                <ChevronDown className={cn('h-3.5 w-3.5 transition-transform motion-reduce:transition-none', open && 'rotate-180')} aria-hidden="true" />
                            </button>
                        )}
                    </div>
                    <MotionExpand open={open && Boolean(action.detailMd)}>
                        <div className="mt-2 rounded-md bg-slate-50 p-3">
                            {action.detailMd && <MarkdownView markdown={action.detailMd} className="text-sm leading-6" />}
                        </div>
                    </MotionExpand>
                </div>
            </div>
        </Card>
    );
}

export function PriorityActions({
    section,
    evidenceRowKeys,
}: {
    section: DataSection<readonly PriorityAction[]>;
    evidenceRowKeys: readonly string[];
}) {
    return (
        <Section
            id="actions"
            eyebrow="下一步"
            title="优先行动"
            subtitle="按优先级排序；每条都对应报告内的真实证据，不含收益承诺。"
        >
            {section.status !== 'ready' ? (
                <EmptyBlock message={section.message} />
            ) : section.data.length === 0 ? (
                <EmptyBlock message="本次未形成行动建议。" />
            ) : (
                <ol className="grid gap-4">
                    {section.data.slice(0, 5).map((action, i) => (
                        <ActionCard
                            key={`${action.priorityLabel}-${i}`}
                            action={action}
                            index={i}
                            evidenceRowKeys={evidenceRowKeys}
                        />
                    ))}
                </ol>
            )}
        </Section>
    );
}
