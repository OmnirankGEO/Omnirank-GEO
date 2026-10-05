/**
 * EvidenceMatrix — 按平台归组的逐题证据
 *
 * 报告首层只展示本次真实采集到的平台；展开平台后一次性展示该平台的全部问题、
 * 判定、完整回答与引用。历史报告中的 Kimi 仍按真实平台名保留，绝不改名冒充元宝。
 */
import { useMemo, useState } from 'react';
import { ChevronDown, Search, X } from 'lucide-react';
import {
    EmptyBlock,
    EvidenceLevelTag,
    Section,
    VerdictTag,
} from '../ui/primitives';
import { MotionExpand } from '../motion/presets';
import { MarkdownView } from '../markdown/MarkdownView';
import type {
    DataSection,
    EvidenceMatrixData,
    EvidenceItem,
    EvidenceVerdict,
} from '../../contract/types';
import { cn } from '@/lib/utils';

const PLATFORM_ORDER = ['通义千问', 'DeepSeek', '豆包', '元宝'] as const;

const VERDICT_OPTIONS: readonly { value: EvidenceVerdict; label: string }[] = [
    /* 🔴 [#233 · 2026-09-17] 这两档对客不再叫「推荐」,改叫「提及」——
       判定器只看「确认提及之后回答里有没有正向词」,不绑定推荐对象、不识别否定
       (实测把「目前无法推荐甲公司」判成 recommended)。
       说法收回到判定真正测到的东西上。语义判定本身是包二的活,本单不改。
       🔴 同一份映射在 `ui/primitives.tsx` 的 VERDICT_STYLE 里**还有一份**,两处必须一起改。 */
    { value: 'recommended', label: '提及(正面措辞)' },
    { value: 'conditionally_recommended', label: '提及(带条件措辞)' },
    { value: 'candidate', label: '列入备选' },
    { value: 'mentioned', label: '仅提到' },
    { value: 'criteria_only', label: '只给标准' },
    { value: 'refused_no_evidence', label: '证据不足拒答' },
    { value: 'refused_risk', label: '风险拒答' },
    { value: 'not_mentioned', label: '未提到' },
    { value: 'brand_confused', label: '品牌混淆' },
    { value: 'engine_error', label: '引擎异常' },
    { value: 'no_answer', label: '无有效回答' },
];

interface PlatformGroup {
    readonly platformName: string;
    readonly items: readonly EvidenceItem[];
}

function platformRank(name: string): number {
    const index = PLATFORM_ORDER.indexOf(name as (typeof PLATFORM_ORDER)[number]);
    return index >= 0 ? index : PLATFORM_ORDER.length;
}

function groupByPlatform(items: readonly EvidenceItem[]): readonly PlatformGroup[] {
    const groups = new Map<string, EvidenceItem[]>();
    items.forEach((item) => {
        const current = groups.get(item.platformName) ?? [];
        current.push(item);
        groups.set(item.platformName, current);
    });
    return Array.from(groups, ([platformName, groupedItems]) => ({
        platformName,
        items: groupedItems,
    })).sort((left, right) => (
        platformRank(left.platformName) - platformRank(right.platformName)
        || left.platformName.localeCompare(right.platformName, 'zh-CN')
    ));
}

function formatTime(iso: string | null): string {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    });
}

function DomainChips({ domains }: { domains: readonly string[] }) {
    if (domains.length === 0) return <span className="text-xs text-slate-400">本次未采集到引用域名</span>;
    return (
        <span className="flex flex-wrap gap-1">
            {domains.map((domain) => (
                <span
                    key={domain}
                    className="inline-block max-w-full break-all rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600"
                >
                    {domain}
                </span>
            ))}
        </span>
    );
}

function PlatformSummary({ items }: { items: readonly EvidenceItem[] }) {
    const validAnswers = items.filter((item) => Boolean(item.answerExcerpt)).length;
    const recommended = items.filter((item) => (
        item.verdict === 'recommended' || item.verdict === 'conditionally_recommended'
    )).length;
    return (
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
            <span>{items.length} 个问题</span>
            <span>{validAnswers} 条有效回答</span>
            <span>{recommended} 条正面措辞</span>
        </span>
    );
}

function EvidenceAnswer({ item, index }: { item: EvidenceItem; index: number }) {
    return (
        <li className="prp-expand-surface bg-slate-50 px-4 py-5 sm:px-5">
            <article aria-labelledby={`evidence-question-${item.rowKey}`}>
                <div className="flex items-start gap-3">
                    <span className="mt-0.5 inline-flex h-6 min-w-6 shrink-0 items-center justify-center rounded-full bg-white px-1 text-xs font-semibold tabular-nums text-slate-500 ring-1 ring-slate-200">
                        {index + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                        {/* [工单 2026-08-03 ③] 问句层级提一档:font-semibold→bold、窄档 text-base、
                            宽档 text-lg。它是整条证据的标题,原来与下面的徽章/时间同一视觉重量。 */}
                        <h4
                            id={`evidence-question-${item.rowKey}`}
                            className="break-words text-base font-bold leading-7 text-slate-900 sm:text-lg"
                            data-testid="evidence-question-title"
                        >
                            {item.question}
                        </h4>
                        {/* 徽章行:items-center 保证不同高度的徽章在同一基线上对齐 */}
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                            <VerdictTag verdict={item.verdict} />
                            <EvidenceLevelTag level={item.evidenceLevel} />
                            <span className="text-xs tabular-nums text-slate-500">测试时间 {formatTime(item.testedAt)}</span>
                        </div>
                    </div>
                </div>

                <div className="mt-4 border-t border-slate-200 pt-4 sm:ml-9">
                    <p className="text-[11px] font-semibold tracking-wide text-slate-500">平台原始回答</p>
                    {item.answerExcerpt ? (
                        <MarkdownView
                            markdown={item.answerExcerpt}
                            className="mt-2 text-sm leading-7 [&_h1]:text-base [&_h2]:text-base [&_h3]:text-sm [&_p]:my-2 [&_ul]:my-2 [&_ol]:my-2"
                        />
                    ) : (
                        <p className="mt-2 text-sm leading-6 text-slate-600">本次未获取到有效回答文本。</p>
                    )}
                    <div className="mt-3 flex min-w-0 flex-wrap items-start gap-x-2 gap-y-1 text-xs text-slate-500">
                        <span className="shrink-0 whitespace-nowrap">引用域名</span>
                        <DomainChips domains={item.citedDomains} />
                    </div>
                </div>
            </article>
        </li>
    );
}

function PlatformAccordion({
    group,
    open,
    onToggle,
}: {
    group: PlatformGroup;
    open: boolean;
    onToggle: () => void;
}) {
    const panelId = `evidence-platform-${group.platformName.replace(/[^\w\u4e00-\u9fff-]/g, '-')}`;
    return (
        <li className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
            <button
                type="button"
                onClick={onToggle}
                aria-expanded={open}
                aria-controls={panelId}
                aria-label={`${open ? '收起' : '展开'}${group.platformName}的全部问题与回答`}
                className="flex min-h-20 w-full touch-manipulation items-center gap-3 px-4 py-4 text-left hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-sky-600 sm:px-5"
            >
                <span className="min-w-0 flex-1">
                    <span className="block break-words text-base font-bold text-slate-900 sm:text-lg">
                        {group.platformName}
                    </span>
                    <span className="mt-1 block">
                        <PlatformSummary items={group.items} />
                    </span>
                </span>
                <span className="hidden shrink-0 text-xs font-medium text-sky-700 sm:inline">
                    {open ? '收起全部回答' : `查看 ${group.items.length} 条回答`}
                </span>
                <ChevronDown
                    className={cn('h-5 w-5 shrink-0 text-slate-500 transition-transform motion-reduce:transition-none', open && 'rotate-180')}
                    aria-hidden="true"
                />
            </button>
            <MotionExpand open={open}>
                <ol id={panelId} className="divide-y divide-slate-200 border-t border-slate-200">
                    {group.items.map((item, index) => (
                        <EvidenceAnswer key={item.rowKey} item={item} index={index} />
                    ))}
                </ol>
            </MotionExpand>
        </li>
    );
}

export function EvidenceMatrix({ section }: { section: DataSection<EvidenceMatrixData> }) {
    const [query, setQuery] = useState('');
    const [verdict, setVerdict] = useState<EvidenceVerdict | ''>('');
    const [expandedPlatforms, setExpandedPlatforms] = useState<ReadonlySet<string>>(() => new Set());

    const items = section.status === 'ready' ? section.data.items : [];
    const filtered = useMemo(() => {
        const normalizedQuery = query.trim().toLowerCase();
        return items.filter((item) => {
            if (verdict && item.verdict !== verdict) return false;
            if (!normalizedQuery) return true;
            return `${item.question} ${item.answerExcerpt ?? ''}`.toLowerCase().includes(normalizedQuery);
        });
    }, [items, query, verdict]);
    const groups = useMemo(() => groupByPlatform(filtered), [filtered]);
    const hasFilters = query.trim() !== '' || verdict !== '';

    const clearFilters = () => {
        setQuery('');
        setVerdict('');
        setExpandedPlatforms(new Set());
    };
    const togglePlatform = (platformName: string) => {
        setExpandedPlatforms((current) => {
            const next = new Set(current);
            if (next.has(platformName)) next.delete(platformName);
            else next.add(platformName);
            return next;
        });
    };

    return (
        <Section
            id="evidence"
            eyebrow="原始证据"
            title="证据矩阵"
            subtitle="先按本次实际测试平台归拢；展开任一平台，即可核对该平台的全部问题、原始回答、判定与引用。"
        >
            {section.status !== 'ready' ? (
                <EmptyBlock
                    title={section.status === 'empty' ? '暂无证据' : '暂无数据'}
                    message={section.message}
                    tone={section.status === 'unavailable' ? 'muted' : 'info'}
                />
            ) : (
                <>
                    <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center print:hidden">
                        <div className="relative min-w-0 flex-1 sm:max-w-sm">
                            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" aria-hidden="true" />
                            <input
                                type="search"
                                value={query}
                                onChange={(event) => {
                                    setQuery(event.target.value);
                                    setExpandedPlatforms(new Set());
                                }}
                                placeholder="搜索全部平台的问题或回答"
                                aria-label="搜索全部平台的问题或回答"
                                className="h-11 w-full rounded-md border border-slate-200 bg-white pl-8 pr-3 text-sm text-slate-800 placeholder:text-slate-400 focus:border-sky-500 focus:outline-2 focus:outline-sky-600"
                            />
                        </div>
                        <select
                            value={verdict}
                            onChange={(event) => {
                                setVerdict(event.target.value as EvidenceVerdict | '');
                                setExpandedPlatforms(new Set());
                            }}
                            aria-label="按判定结果筛选"
                            className="h-11 rounded-md border border-slate-200 bg-white px-2.5 text-sm text-slate-700 focus:border-sky-500 focus:outline-2 focus:outline-sky-600"
                        >
                            <option value="">全部判定</option>
                            {VERDICT_OPTIONS.map((option) => (
                                <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                        </select>
                        {hasFilters && (
                            <button
                                type="button"
                                onClick={clearFilters}
                                className="inline-flex min-h-11 items-center justify-center gap-1 rounded-md border border-slate-200 px-3 text-sm text-slate-600 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-sky-600"
                            >
                                <X className="h-3.5 w-3.5" aria-hidden="true" />
                                清除筛选
                            </button>
                        )}
                        <p className="text-xs text-slate-400 sm:ml-auto" aria-live="polite">
                            {groups.length} 个平台 · {filtered.length} / {section.data.totalCount} 条
                            {section.data.totalCount > items.length
                                ? `（当前加载前 ${items.length} 条）`
                                : ''}
                        </p>
                    </div>

                    {groups.length === 0 ? (
                        <EmptyBlock
                            title="没有符合筛选的证据"
                            message={hasFilters ? '试试放宽条件，或清除全部筛选。' : '本次没有证据记录。'}
                        />
                    ) : (
                        <ul className="grid gap-3">
                            {groups.map((group) => (
                                <PlatformAccordion
                                    key={group.platformName}
                                    group={group}
                                    open={expandedPlatforms.has(group.platformName)}
                                    onToggle={() => togglePlatform(group.platformName)}
                                />
                            ))}
                        </ul>
                    )}
                </>
            )}
        </Section>
    );
}
