/**
 * HeroSummary — 深墨色摘要区(报告身份 + 执行摘要)
 *
 *  - 品牌/行业/诊断时间/数据更新时间/报告口径；白标仅展示后端批准字段
 *  - GEO 综合评分(分数环,450ms 一次性绘制) + 后端等级 + 资料完整度(独立指标)
 *  - 缺失值显示 "—",绝不显示假 0；部分样本/等级封顶显式标注
 *  - 信息顺序:移动端 = 结论 → 评分/等级 → 完整度 → 样本规模(320 首屏先见核心评分);
 *    桌面端 = 左列(身份+结论)/右列(评分+完整度跨行)+左下 KPI,视觉不变
 */
import { useReducedMotion, motion } from 'framer-motion';
import { ArrowDownRight, ShieldAlert } from 'lucide-react';
import { useCountUp } from '../motion/presets';
import { LevelBadge, MetricText } from '../ui/primitives';
import type { PublicReportPresentationV1 } from '../../contract/types';
import { cn } from '@/lib/utils';

function formatDateTime(iso: string | null): string | null {
    if (!iso) return null;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

/** 分数环(SVG,一次性 450ms 绘制；reduced-motion 直接终态) */
function ScoreRing({ score, colorHex }: { score: number | null; colorHex: string | null }) {
    const reduceMotion = useReducedMotion();
    const R = 52;
    const CIRC = 2 * Math.PI * R;
    const pct = score === null ? 0 : score / 100;
    const color = colorHex ?? '#38bdf8';
    return (
        <div
            className="relative h-32 w-32 sm:h-36 sm:w-36"
            role="img"
            aria-label={score === null ? 'GEO 综合评分暂无数据' : `GEO 综合评分 ${score} 分，满分 100`}
        >
            <svg viewBox="0 0 120 120" className="h-full w-full -rotate-90">
                <circle cx="60" cy="60" r={R} fill="none" stroke="#1e293b" strokeWidth="9" />
                {score !== null && (
                    <motion.circle
                        cx="60"
                        cy="60"
                        r={R}
                        fill="none"
                        stroke={color}
                        strokeWidth="9"
                        strokeLinecap="round"
                        strokeDasharray={CIRC}
                        initial={{ strokeDashoffset: reduceMotion ? CIRC * (1 - pct) : CIRC }}
                        animate={{ strokeDashoffset: CIRC * (1 - pct) }}
                        transition={reduceMotion ? { duration: 0 } : { duration: 0.45, ease: 'easeOut' }}
                    />
                )}
            </svg>
            <div className="absolute inset-0 flex flex-col items-center justify-center">
                <span className="text-3xl font-bold tabular-nums text-white sm:text-4xl">
                    {score === null ? <span className="text-slate-500">—</span> : score}
                </span>
                <span className="text-[11px] text-slate-400">{score === null ? '暂无数据' : '/ 100'}</span>
            </div>
        </div>
    );
}

function HeroMetric({ label, value, suffix }: { label: string; value: number | null; suffix?: string }) {
    const display = useCountUp(value);
    return (
        <div>
            <p className="text-xs text-slate-400">{label}</p>
            <p className="mt-1 text-2xl font-bold tabular-nums text-white">
                <MetricText value={display} suffix={suffix} />
            </p>
        </div>
    );
}

export function HeroSummary({ report }: { report: PublicReportPresentationV1 }) {
    const { identity, summary } = report;
    const levelColor = summary.scoreLevel?.colorHex ?? null;
    const diagnosedAt = formatDateTime(identity.diagnosedAt);
    const updatedAt = formatDateTime(identity.dataUpdatedAt);
    const trustNotes: string[] = [];
    if (summary.trust.partialSample) trustNotes.push('本次为部分样本(有层级未实测)');
    if (summary.trust.levelCapped) trustNotes.push('等级已按规则封顶以避免过度承诺');
    if (summary.trust.confidence === 'low') trustNotes.push('样本量偏低，结论仅供参考');

    return (
        <header id="overview" className="bg-slate-900 text-white print:bg-white print:text-slate-900">
            <div className="mx-auto w-full max-w-[1320px] px-4 py-8 sm:px-6 sm:py-10 lg:px-10">
                <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-start">
                    {/* 身份 + 结论(移动第 1 / 桌面左上) */}
                    <div className="min-w-0">
                        <p className="text-xs font-semibold uppercase tracking-widest text-sky-400 print:text-sky-700">
                            GEO 品牌诊断报告
                        </p>
                        <h1 className="mt-2 break-words text-2xl font-bold leading-snug sm:text-3xl lg:text-4xl">
                            {identity.brandName}
                        </h1>
                        <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-400 sm:text-sm">
                            <div className="flex gap-1.5">
                                <dt className="text-slate-500">行业</dt>
                                <dd>{identity.industry ?? <span className="text-slate-500">—</span>}</dd>
                            </div>
                            <div className="flex gap-1.5">
                                <dt className="text-slate-500">诊断时间</dt>
                                <dd>{diagnosedAt ?? <span className="text-slate-500">—</span>}</dd>
                            </div>
                            <div className="flex gap-1.5">
                                <dt className="text-slate-500">数据更新</dt>
                                <dd>{updatedAt ?? <span className="text-slate-500">—</span>}</dd>
                            </div>
                            <div className="flex gap-1.5">
                                <dt className="text-slate-500">报告口径</dt>
                                <dd>{identity.reportVersionLabel}</dd>
                            </div>
                        </dl>

                        <p className="mt-5 max-w-2xl break-words text-base leading-7 text-slate-200 sm:text-lg print:text-slate-700">
                            {summary.headline ?? (
                                <span className="text-slate-500">本次诊断未形成一句话结论。</span>
                            )}
                        </p>

                        {trustNotes.length > 0 && (
                            <p className="mt-3 inline-flex items-start gap-1.5 rounded-md border border-amber-400/30 bg-amber-400/10 px-2.5 py-1.5 text-xs text-amber-200 print:border-amber-300 print:bg-amber-50 print:text-amber-800">
                                <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                                {trustNotes.join(' · ')}
                            </p>
                        )}

                        {summary.nextAction && (
                            <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-300 print:text-slate-600">
                                <span className="font-semibold text-sky-300 print:text-sky-700">下一步：</span>
                                {summary.nextAction}
                            </p>
                        )}
                    </div>

                    {/* 分数环 + 等级 + 完整度(移动第 2 / 桌面右列跨两行) */}
                    <div className="flex flex-col items-start gap-4 xl:row-span-2 xl:items-end">
                        <div className="flex items-center gap-5">
                            <ScoreRing score={summary.geoScore} colorHex={levelColor} />
                            <div className="space-y-2">
                                <p className="text-xs text-slate-400">GEO 综合评分</p>
                                {summary.scoreLevel ? (
                                    <LevelBadge label={summary.scoreLevel.label} colorHex={levelColor} size="lg" />
                                ) : (
                                    <p className="text-sm text-slate-500">等级暂无数据</p>
                                )}
                                {summary.scoreLevel?.summary && (
                                    <p className="max-w-[220px] text-xs leading-5 text-slate-400">
                                        {summary.scoreLevel.summary}
                                    </p>
                                )}
                            </div>
                        </div>

                        {/* 资料完整度(独立指标,显式切割) */}
                        <div className="w-full max-w-xs rounded-md border border-slate-800 bg-slate-800/50 p-3 print:border-slate-200 print:bg-slate-50">
                            <div className="flex items-baseline justify-between gap-2">
                                <p className="text-xs text-slate-400">资料完整度</p>
                                <p className="text-sm font-semibold tabular-nums">
                                    <MetricText value={summary.dataCompletenessScore} suffix="/100" />
                                    {summary.dataCompletenessLevel && (
                                        <span className="ml-2 text-xs font-normal text-slate-400">
                                            {summary.dataCompletenessLevel}
                                        </span>
                                    )}
                                </p>
                            </div>
                            <div
                                className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-700 print:bg-slate-200"
                                role="progressbar"
                                aria-valuemin={0}
                                aria-valuemax={100}
                                aria-valuenow={summary.dataCompletenessScore ?? undefined}
                                aria-label="资料完整度"
                            >
                                {summary.dataCompletenessScore !== null && (
                                    <div
                                        className="h-full rounded-full bg-sky-500"
                                        style={{ width: `${summary.dataCompletenessScore}%` }}
                                    />
                                )}
                            </div>
                            <p className="mt-2 text-[11px] leading-4 text-slate-500">
                                只表示本次判断依据是否充足，不是 GEO 综合评分。
                            </p>
                        </div>

                        <a
                            href="#evidence"
                            className={cn(
                                'inline-flex min-h-10 items-center gap-1.5 rounded-md border border-slate-600 px-4 text-sm font-medium text-slate-100',
                                'hover:border-slate-400 hover:bg-slate-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-400',
                                'print:hidden',
                            )}
                        >
                            查看关键证据
                            <ArrowDownRight className="h-4 w-4" aria-hidden="true" />
                        </a>
                    </div>

                    {/* 实测规模(移动第 3 / 桌面左下；派生自后端,禁止硬编码平台数) */}
                    <div className="grid grid-cols-1 gap-4 border-t border-slate-800 pt-5 print:border-slate-200 sm:max-w-md sm:grid-cols-3 xl:col-start-1">
                        <HeroMetric label="实测平台" value={summary.testedPlatformCount} suffix="个" />
                        <HeroMetric label="真实问题" value={summary.testedQuestionCount} suffix="个" />
                        <HeroMetric label="有效回答" value={summary.validAnswerCount} suffix="条" />
                    </div>
                </div>
            </div>
        </header>
    );
}
