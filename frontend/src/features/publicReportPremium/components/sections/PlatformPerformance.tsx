/**
 * PlatformPerformance — 平台表现
 *
 *  - 平台动态渲染(后端给几个就几个，禁止硬编码 "四大引擎")
 *  - 桌面：语义 table + 可排序列(aria-sort)；移动：卡片折叠
 *  - 每平台可展开查看关联证据；比率缺失显示 "—"(非 0)；迷你比例条辅助扫读
 */
import { useMemo, useState } from 'react';
import { ChevronDown, ArrowUpDown, ArrowUp, ArrowDown } from 'lucide-react';
import { Card, EmptyBlock, MetricText, MissingValue, Section, VerdictTag } from '../ui/primitives';
import { MotionExpand } from '../motion/presets';
import { MarkdownView } from '../markdown/MarkdownView';
import type { DataSection, EvidenceItem, PlatformPerformance as PlatformRow } from '../../contract/types';
import { cn } from '@/lib/utils';
import { platformExclusionClause } from '@/features/publicReportPremium/exclusionClaim';

type SortKey = 'validSamples' | 'detectionRatePct' | 'mentionRatePct' | 'recommendRatePct' | 'citationCount';

const COLUMNS: readonly { key: SortKey; label: string }[] = [
    { key: 'detectionRatePct', label: '识别率' },
    { key: 'mentionRatePct', label: '提及率' },
    /* 🔴 [#238 乙] 对客不再叫「推荐率」。该判定只看「确认提及之后回答里有没有正向词」,
       既不绑定推荐对象也不识别否定(实测把「目前无法推荐甲公司」判成 recommended)——
       拿它对客户说「推荐」是拿一个会认错的判定做正面承诺。与后端指标显示名同口径。
       🔴 键 `recommendRatePct` **不动**:改的是显示名,改键下游取数就断。 */
    { key: 'recommendRatePct', label: '提及率（正面措辞）' },
    { key: 'citationCount', label: '引用来源' },
];

function rateValue(row: PlatformRow, key: SortKey): number | null {
    return row[key];
}

function RateCell({ value, label, showBar = true }: { value: number | null; label?: string; showBar?: boolean }) {
    return (
        <div className="min-w-[72px]">
            <span className="text-sm font-semibold tabular-nums text-slate-800">
                {value === null ? <MissingValue label="未采集" /> : <MetricText value={value} suffix="%" />}
            </span>
            {showBar && value !== null && (
                <div aria-hidden="true" className="mt-1 h-1 w-16 overflow-hidden rounded-full bg-slate-100">
                    <div className="h-full rounded-full bg-sky-500" style={{ width: `${Math.min(100, value)}%` }} />
                </div>
            )}
            {label && <p className="mt-0.5 text-[11px] text-slate-400">{label}</p>}
        </div>
    );
}

/**
 * 引用来源单元格。
 *
 * [P0-3 ④] 「不适用」和「真实为 0」不是一回事：元宝这类**模型原生回答**表面本就
 * 不联网检索，不产生引用。旧版把它渲染成 0 和其他平台的 62/61/90 并排，读起来像
 * 「这个品牌一条信源都没有」——那是把系统能力边界说成客户的问题。
 * 后端给 citationCount=null + citationStatus 原因，这里显示 "—" 并把原因写清。
 */
function CitationCell({ row }: { row: PlatformRow }) {
    if (row.citationCount !== null) {
        return (
            <span className="text-sm font-semibold tabular-nums text-slate-800">
                <MetricText value={row.citationCount} />
            </span>
        );
    }
    const status = row.citationStatus ?? null;
    const notApplicable = Boolean(status && status.includes('不适用'));
    return (
        <span className="inline-flex flex-col gap-0.5">
            <span className="text-sm font-semibold text-slate-800">
                <MissingValue label={notApplicable ? '不适用' : '未采集'} />
            </span>
            {status && <span className="text-[11px] font-normal leading-4 text-slate-400">{status}</span>}
        </span>
    );
}

function PlatformEvidence({ items, platformName }: { items: readonly EvidenceItem[]; platformName: string }) {
    const related = items.filter((e) => e.platformName === platformName);
    if (related.length === 0) {
        return <p className="px-4 py-3 text-xs text-slate-500">该平台暂无关联证据。</p>;
    }
    return (
        <ul className="prp-expand-surface divide-y divide-slate-200 bg-slate-50">
            {related.slice(0, 5).map((e) => (
                <li key={e.rowKey} className="px-4 py-3.5" data-testid="platform-evidence-row">
                    {/* [工单 2026-08-03 ③] 问句是这一行的主语,原来 text-xs/font-medium/slate-700
                        —— 比下面的答案正文还弱,扫视时根本找不到"这题问的是什么"。
                        改 flex-wrap→items-start + 徽章 shrink-0:长问句换行时徽章仍钉在首行,
                        不会被挤到下一行去(原 flex-wrap 在窄视口正是这么错位的)。 */}
                    <div className="flex items-start gap-2">
                        <span className="mt-0.5 shrink-0">
                            <VerdictTag verdict={e.verdict} />
                        </span>
                        <h4
                            className="min-w-0 flex-1 break-words text-sm font-semibold leading-6 text-slate-900 sm:text-[15px]"
                            data-testid="evidence-question-title"
                        >
                            {e.question}
                        </h4>
                    </div>
                    {/* 与答案正文明确分隔:分隔线 + 与问句左缘对齐的缩进 */}
                    <div className="mt-2.5 border-t border-slate-200 pt-2.5" data-testid="evidence-answer-body">
                        {e.answerExcerpt ? (
                            <MarkdownView
                                markdown={e.answerExcerpt}
                                className="text-xs leading-5 [&_h1]:text-sm [&_h2]:text-sm [&_h3]:text-xs [&_p]:my-1 [&_ul]:my-1 [&_ol]:my-1"
                            />
                        ) : (
                            <p className="text-xs leading-5 text-slate-500">本次未获取到有效回答文本。</p>
                        )}
                    </div>
                </li>
            ))}
        </ul>
    );
}

export function PlatformPerformance({
    section,
    evidenceItems,
    excludedBrandDirectedCount,
}: {
    section: DataSection<readonly PlatformRow[]>;
    evidenceItems: readonly EvidenceItem[] | null;
    /**
     * [#238 甲] 本次**实际**被排除出分母的有效回答条数。与竞争格局那一节同源
     * (`competitive.data.excludedBrandDirectedCount`)—— 同一次排除,不许两处各自算:
     * 各算各的就会出现「脚注说排了 5 条、平台表说排了 8 条」而两边各自看起来都对。
     * 取不到(该节 unavailable)⇒ `null` ⇒ **不说**那句。
     */
    excludedBrandDirectedCount: number | null;
}) {
    const [sortKey, setSortKey] = useState<SortKey | null>(null);
    const [sortAsc, setSortAsc] = useState(false);
    const [expanded, setExpanded] = useState<string | null>(null);

    const rows = useMemo(() => {
        if (section.status !== 'ready') return [];
        const list = [...section.data];
        if (sortKey) {
            list.sort((a, b) => {
                const va = rateValue(a, sortKey);
                const vb = rateValue(b, sortKey);
                if (va === null && vb === null) return 0;
                if (va === null) return 1; // 缺失恒排最后
                if (vb === null) return -1;
                return sortAsc ? va - vb : vb - va;
            });
        }
        return list;
    }, [section, sortKey, sortAsc]);

    const toggleSort = (key: SortKey) => {
        if (sortKey === key) {
            if (!sortAsc) setSortAsc(true);
            else setSortKey(null); // 第三次点击恢复后端原序
        } else {
            setSortKey(key);
            setSortAsc(false); // 比率默认降序
        }
    };

    const toggleExpand = (name: string) => setExpanded((cur) => (cur === name ? null : name));

    const ariaSortFor = (key: SortKey): 'ascending' | 'descending' | 'none' =>
        sortKey === key ? (sortAsc ? 'ascending' : 'descending') : 'none';

    // [WO 2026-08-07] 全平台待确认格合计。后端 0 给 null,这里 `?? 0` 只是求和用,
    // 渲染条件是 `> 0` —— 两处都不会把"没有待确认"渲染成「0 次」。
    const pendingDisclosureTotal =
        section.status === 'ready'
            ? section.data.reduce((sum, row) => sum + (row.identityPendingSamples ?? 0), 0)
            : 0;
    // 已统计的样本数 = 真正进了分母的那些。复审要求两个数一起给:
    // 只说"另有 M 待确认"读者不知道 M 相对多大;给了分母才判得动这份报告可信度。
    const countedDisclosureTotal =
        section.status === 'ready'
            ? section.data.reduce((sum, row) => sum + (row.validSamples ?? 0), 0)
            : 0;

    return (
        <Section
            id="platforms"
            eyebrow="分平台"
            title="平台表现"
            /*
             * 🔴 [#238 甲 · P0] 这句原来是**字面量,旁边没有任何条件** —— 它陈述的是
             *    「本代码打算排除」,不是「这次到底排没排」。真客户 #700(瑞恩宠物医院)
             *    的 brand_directed_valid = **0**,一条都没排,平台表照样宣称已排除。
             *    与 #236 甲**完全同病**:同一句谎话长在两个屏上,修了一个另一个还在说。
             *
             * 🔴 复用 `exclusionClaim.ts` 的同一个判定与同一份文案,**不另写一份**:
             *    两处各自算就会出现「脚注说 5 条、平台表说 8 条」而两边各自看起来都对。
             */
            subtitle={
                '以本次实际实测的平台为准。识别率看全部问题；'
                + platformExclusionClause(excludedBrandDirectedCount)
                + '「提及率（正面措辞）」是「提及率」的一部分（前者 ≤ 后者）。“未采集”「不适用」都不等于 0。'
            }
        >
            {section.status !== 'ready' ? (
                <EmptyBlock message={section.message} />
            ) : section.data.length === 0 ? (
                <EmptyBlock message="本次未实测任何平台。" />
            ) : (
                <>
                    {/*
                      * [WO_UNKNOWN_DENOMINATOR_DISCLOSURE 2026-08-07 · Owner 边界②③]
                      *
                      * 明示:有几次回答的品牌名还要人确认。
                      *
                      * 🔴 [Owner 2026-08-07 拍板 (A)(a)] 文案说的必须是**这一层的真相**:
                      * 生产实证(179/183 份报告 · 5976 格)报告模块层 status 是中文展示
                      * 标签「未提到品牌」而不是 error → 这些格在 C 端**是进分母的、
                      * 算作未提到**,与 ai_mention_rate(排除)方向相反。
                      * 所以这里绝不能写「未计入本次出现率」—— 在这一层那是假话。
                      * 口径一个字不动(Owner 裁定),只把真相说清楚:确认后**可能上升**。
                      * 🔴 这是 C 端(链接接收方)视角 —— **只说清楚,不给确认入口**:
                      *   确认是服务商的活,不该把操作口递给收到链接的人。
                      * 🔴 N=0 整块不渲染(后端 0 给 null)—— 零待确认的报告里
                      *   不许多出任何一句废话(「提示要么帮人解决,要么不显示」)。
                      * 🔴 文案里不许出现 PENDING_IDENTITY / UNKNOWN / ai_total_tests
                      *   这类工程词。
                      */}
                    {pendingDisclosureTotal > 0 && (
                        <p
                            data-testid="identity-pending-disclosure"
                            className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900"
                        >
                            已统计 {countedDisclosureTotal} 次问答，其中{' '}
                            {pendingDisclosureTotal} 次的品牌名还需人工确认、
                            <strong className="font-semibold">当前按「未提到」计入</strong>
                            ；确认后出现率可能上升。
                        </p>
                    )}
                    {/* 桌面/平板:table(容器内可横向滚动，不撑破页面) */}
                    <div className="hidden overflow-x-auto rounded-lg border border-slate-200 bg-white md:block">
                        <table className="w-full min-w-[720px] border-collapse text-sm">
                            <caption className="sr-only">各平台实测表现，可排序</caption>
                            <thead>
                                <tr className="border-b border-slate-200 bg-slate-50 text-left">
                                    <th scope="col" className="px-4 py-3 font-semibold text-slate-700">平台</th>
                                    <th scope="col" className="px-3 py-3 font-semibold text-slate-700" aria-sort={ariaSortFor('validSamples')}>
                                        <button type="button" onClick={() => toggleSort('validSamples')} className="inline-flex items-center gap-1 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-sky-600">
                                            样本
                                            {sortKey === 'validSamples' ? (sortAsc ? <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ArrowDown className="h-3.5 w-3.5" aria-hidden="true" />) : <ArrowUpDown className="h-3.5 w-3.5 text-slate-300" aria-hidden="true" />}
                                        </button>
                                    </th>
                                    {COLUMNS.map((col) => (
                                        <th key={col.key} scope="col" className="px-3 py-3 font-semibold text-slate-700" aria-sort={ariaSortFor(col.key)}>
                                            <button type="button" onClick={() => toggleSort(col.key)} className="inline-flex items-center gap-1 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-sky-600">
                                                {col.label}
                                                {sortKey === col.key ? (sortAsc ? <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" /> : <ArrowDown className="h-3.5 w-3.5" aria-hidden="true" />) : <ArrowUpDown className="h-3.5 w-3.5 text-slate-300" aria-hidden="true" />}
                                            </button>
                                        </th>
                                    ))}
                                    <th scope="col" className="px-3 py-3 font-semibold text-slate-700">状态</th>
                                    <th scope="col" className="px-3 py-3 font-semibold text-slate-700"><span className="sr-only">展开证据</span></th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows.map((row) => {
                                    const isOpen = expanded === row.platformName;
                                    return (
                                        <PlatformTableRow
                                            key={row.platformName}
                                            row={row}
                                            isOpen={isOpen}
                                            onToggle={() => toggleExpand(row.platformName)}
                                            evidenceItems={evidenceItems}
                                        />
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>

                    {/* 手机：卡片折叠 */}
                    <ul className="grid gap-3 md:hidden">
                        {rows.map((row) => {
                            const isOpen = expanded === row.platformName;
                            return (
                                <Card key={row.platformName} as="li" className="p-4">
                                    <div className="flex items-start justify-between gap-2">
                                        <div className="min-w-0">
                                            <h3 className="break-words text-sm font-semibold text-slate-800">{row.platformName}</h3>
                                            <p className="mt-0.5 text-xs text-slate-500">
                                                样本 <MetricText value={row.validSamples} />
                                                {row.dataStatus ? ` · ${row.dataStatus}` : ''}
                                            </p>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => toggleExpand(row.platformName)}
                                            aria-expanded={isOpen}
                                            aria-label={`${expanded === row.platformName ? '收起' : '展开'} ${row.platformName} 的证据`}
                                            className="inline-flex min-h-9 min-w-9 shrink-0 items-center justify-center rounded-md border border-slate-200 text-slate-500 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-sky-600"
                                        >
                                            <ChevronDown className={cn('h-4 w-4 transition-transform motion-reduce:transition-none', isOpen && 'rotate-180')} aria-hidden="true" />
                                        </button>
                                    </div>
                                    <div className="mt-3 grid grid-cols-1 gap-3">
                                        <RateCell value={row.detectionRatePct} label="识别率" />
                                        <RateCell value={row.mentionRatePct} label="提及率" />
                                        <RateCell value={row.recommendRatePct} label="提及率（正面措辞）" />
                                        <div>
                                            <CitationCell row={row} />
                                            <p className="mt-0.5 text-[11px] text-slate-400">引用来源</p>
                                        </div>
                                    </div>
                                    <MotionExpand open={isOpen}>
                                        <div className="-mx-4 mt-3 border-t border-slate-100">
                                            <PlatformEvidence items={evidenceItems ?? []} platformName={row.platformName} />
                                        </div>
                                    </MotionExpand>
                                </Card>
                            );
                        })}
                    </ul>
                </>
            )}
        </Section>
    );
}

function PlatformTableRow({
    row,
    isOpen,
    onToggle,
    evidenceItems,
}: {
    row: PlatformRow;
    isOpen: boolean;
    onToggle: () => void;
    evidenceItems: readonly EvidenceItem[] | null;
}) {
    return (
        <>
            <tr className="border-b border-slate-100 align-top last:border-0">
                <th scope="row" className="px-4 py-3 text-left font-medium text-slate-800">
                    <span className="break-words">{row.platformName}</span>
                    {row.updatedAt && (
                        <p className="mt-0.5 text-[11px] font-normal text-slate-400">
                            更新 {new Date(row.updatedAt).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' })}
                        </p>
                    )}
                </th>
                <td className="px-3 py-3 tabular-nums text-slate-700"><MetricText value={row.validSamples} /></td>
                <td className="px-3 py-3"><RateCell value={row.detectionRatePct} /></td>
                <td className="px-3 py-3"><RateCell value={row.mentionRatePct} /></td>
                <td className="px-3 py-3"><RateCell value={row.recommendRatePct} /></td>
                <td className="px-3 py-3 tabular-nums text-slate-700">
                    <CitationCell row={row} />
                </td>
                <td className="px-3 py-3">
                    {row.dataStatus ? (
                        <span className="inline-flex rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[11px] text-slate-600">
                            {row.dataStatus}
                        </span>
                    ) : (
                        <span className="text-slate-400">—</span>
                    )}
                </td>
                <td className="px-3 py-3">
                    <button
                        type="button"
                        onClick={onToggle}
                        aria-expanded={isOpen}
                        aria-label={`${isOpen ? '收起' : '展开'} ${row.platformName} 的证据`}
                        className="inline-flex min-h-9 min-w-9 items-center justify-center rounded-md border border-slate-200 text-slate-500 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-sky-600"
                    >
                        <ChevronDown className={cn('h-4 w-4 transition-transform motion-reduce:transition-none', isOpen && 'rotate-180')} aria-hidden="true" />
                    </button>
                </td>
            </tr>
            {isOpen && (
                <tr className="prp-expand-surface border-b border-slate-200 bg-slate-50">
                    <td colSpan={8} className="p-0">
                        <PlatformEvidence items={evidenceItems ?? []} platformName={row.platformName} />
                    </td>
                </tr>
            )}
        </>
    );
}
