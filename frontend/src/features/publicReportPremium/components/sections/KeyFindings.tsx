/**
 * KeyFindings — 关键发现(机会/风险/资料短板/信源短板/推荐阻断)
 * 每条：证据等级 + 支撑样本量 + 范围 + 可展开证据摘要 + 数据不足提示。
 * 推测不写成事实：文案全部来自后端，前端只做分组呈现。
 */
import { useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Card, EmptyBlock, EvidenceLevelTag, Section } from '../ui/primitives';
import { MotionExpand } from '../motion/presets';
import type { DataSection, FindingCategory, KeyFinding } from '../../contract/types';
import { cn } from '@/lib/utils';

const CATEGORY_META: Record<FindingCategory, { label: string; badge: string }> = {
    opportunity: { label: '主要机会', badge: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
    risk: { label: '主要风险', badge: 'bg-orange-50 text-orange-700 border-orange-200' },
    profile_gap: { label: '资料短板', badge: 'bg-amber-50 text-amber-700 border-amber-200' },
    source_gap: { label: '信源短板', badge: 'bg-sky-50 text-sky-700 border-sky-200' },
    recommendation_blocker: { label: '推荐阻断', badge: 'bg-rose-50 text-rose-700 border-rose-200' },
};

const CATEGORY_ORDER: readonly FindingCategory[] = [
    'opportunity',
    'risk',
    'profile_gap',
    'source_gap',
    'recommendation_blocker',
];

function FindingCard({ finding }: { finding: KeyFinding }) {
    const [open, setOpen] = useState(false);
    const meta = CATEGORY_META[finding.category];
    const expandable = Boolean(finding.detail);
    return (
        <Card className="p-4">
            <div className="flex flex-wrap items-center gap-2">
                <span className={cn('inline-flex rounded border px-1.5 py-0.5 text-[11px] font-medium', meta.badge)}>
                    {meta.label}
                </span>
                <EvidenceLevelTag level={finding.evidenceLevel} />
                {finding.sampleCount !== null && (
                    <span className="text-[11px] tabular-nums text-slate-400">{finding.sampleCount} 个样本</span>
                )}
            </div>
            <p className="mt-2 break-words text-sm leading-6 text-slate-800">{finding.text}</p>
            {finding.scope && <p className="mt-1.5 text-xs text-slate-500">范围：{finding.scope}</p>}
            {finding.insufficientNote && (
                <p className="mt-2 inline-flex rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-700">
                    {finding.insufficientNote}
                </p>
            )}
            {expandable && (
                <>
                    <button
                        type="button"
                        onClick={() => setOpen((v) => !v)}
                        aria-expanded={open}
                        className="mt-2.5 inline-flex min-h-9 items-center gap-1 rounded-md px-1 text-xs font-medium text-sky-700 hover:text-sky-900 focus-visible:outline-2 focus-visible:outline-sky-600"
                    >
                        {open ? '收起证据摘要' : '查看证据摘要'}
                        <ChevronDown className={cn('h-3.5 w-3.5 transition-transform motion-reduce:transition-none', open && 'rotate-180')} aria-hidden="true" />
                    </button>
                    <MotionExpand open={open}>
                        <p className="mt-1 break-words rounded-md bg-slate-50 p-3 text-xs leading-5 text-slate-600">
                            {finding.detail}
                        </p>
                    </MotionExpand>
                </>
            )}
        </Card>
    );
}

export function KeyFindings({ section }: { section: DataSection<readonly KeyFinding[]> }) {
    return (
        <Section
            id="findings"
            eyebrow="证据驱动"
            title="关键发现"
            subtitle="每条结论都标注证据等级与样本量；A=直接证据,B=参考证据,C=待验证线索。"
        >
            {section.status !== 'ready' ? (
                <EmptyBlock message={section.message} />
            ) : section.data.length === 0 ? (
                <EmptyBlock message="本次未形成关键发现。" />
            ) : (
                <div className="grid gap-4 xl:grid-cols-2">
                    {CATEGORY_ORDER.map((category) => {
                        const items = section.data.filter((f) => f.category === category);
                        if (items.length === 0) return null;
                        return (
                            <div key={category} className="grid content-start gap-3">
                                {items.map((finding, i) => (
                                    <FindingCard key={`${category}-${i}`} finding={finding} />
                                ))}
                            </div>
                        );
                    })}
                </div>
            )}
        </Section>
    );
}
