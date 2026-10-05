/**
 * DecisionFunnel — 决策漏斗(可访问 HTML/SVG 实现，非 ECharts)
 *
 * 选择理由：打印保真、零 CLS、天然响应式、屏幕阅读器可读；
 * 层数与命名完全由后端 layers 驱动(当前 SSOT 为 3 层加权漏斗)。
 * 每层：样本数 / 比例 / 与上一层流失(pp) / 普通话解释 / 不足样本提示。
 */
import { motion, useReducedMotion } from 'framer-motion';
import { ArrowDown, TrendingDown } from 'lucide-react';
import { Card, EmptyBlock, MetricText, Section } from '../ui/primitives';
import { useStaggerChild, useStaggerParent } from '../motion/presets';
import type { DataSection, DecisionFunnelData, FunnelLayer } from '../../contract/types';
import { cn } from '@/lib/utils';

/** 层序色板(按位置，不编码数值状态) */
const LAYER_ACCENTS = [
    { bar: 'bg-emerald-500', text: 'text-emerald-600', border: 'border-emerald-500' },
    { bar: 'bg-sky-500', text: 'text-sky-600', border: 'border-sky-500' },
    { bar: 'bg-amber-500', text: 'text-amber-600', border: 'border-amber-500' },
    { bar: 'bg-slate-400', text: 'text-slate-500', border: 'border-slate-400' },
] as const;

function LayerCard({ layer, index, prevRatePct }: { layer: FunnelLayer; index: number; prevRatePct: number | null }) {
    const accent = LAYER_ACCENTS[Math.min(index, LAYER_ACCENTS.length - 1)];
    const childVariants = useStaggerChild();
    const reduceMotion = useReducedMotion();
    const drop =
        prevRatePct !== null && layer.ratePct !== null
            ? Math.round((prevRatePct - layer.ratePct) * 10) / 10
            : null;
    const widthPct = layer.ratePct === null ? 0 : Math.max(2, Math.min(100, layer.ratePct));

    return (
        <motion.li variants={childVariants} className="relative flex-1 min-w-0">
            {/* 层间流失标识(桌面：卡片上方居中；移动：卡片之间) */}
            {index > 0 && (
                <div
                    className={cn(
                        'mb-2 flex items-center justify-center gap-1 text-xs font-medium',
                        drop !== null && drop > 0 ? 'text-orange-600' : 'text-slate-400',
                    )}
                    aria-label={
                        drop !== null ? `较上一层流失 ${drop} 个百分点` : '较上一层流失暂无数据'
                    }
                >
                    {index > 0 && <ArrowDown className="h-3.5 w-3.5 xl:hidden" aria-hidden="true" />}
                    {drop !== null ? (
                        <>
                            <TrendingDown className="hidden h-3.5 w-3.5 xl:block" aria-hidden="true" />
                            流失 {drop}pp
                        </>
                    ) : (
                        '流失 —'
                    )}
                </div>
            )}
            <Card className={cn('h-full border-t-2 p-4', accent.border)}>
                <div className="flex items-baseline justify-between gap-2">
                    <h3 className="text-sm font-semibold text-slate-800">{layer.label}</h3>
                    {layer.weight !== null && (
                        <span className="text-[11px] text-slate-400">权重 {layer.weight}</span>
                    )}
                </div>
                {/*
                  * [P0-5] 同一层禁止并排显示"满分"和"数据不足"。
                  * 生产实证：品牌层 4/4 → 100%，同时 total=4<5 触发"样本不足，仅供参考"，
                  * 客户读到的是"你们自己都说不可信"。
                  * 命中率高但样本少 → 大号位置改成 headlineLabel（"初步达标 · 待扩测"），
                  * 比率降为副行；样本说明统一走 sampleNote 一句话，不再另起角标。
                  */}
                {layer.provisional && layer.headlineLabel ? (
                    <>
                        <p className={cn('mt-2 text-2xl font-bold', accent.text)}>{layer.headlineLabel}</p>
                        <p className="mt-0.5 text-xs tabular-nums text-slate-500">
                            命中率 <MetricText value={layer.ratePct} suffix="%" />
                        </p>
                    </>
                ) : (
                    <p className={cn('mt-2 text-3xl font-bold tabular-nums', layer.ratePct === null ? 'text-slate-300' : accent.text)}>
                        <MetricText value={layer.ratePct} suffix="%" />
                    </p>
                )}
                <p className="mt-0.5 text-xs tabular-nums text-slate-500">
                    {layer.sampleNote
                        ? layer.sampleNote
                        : layer.detected !== null && layer.total !== null
                            ? `命中 ${layer.detected} / ${layer.total} 样本`
                            : '样本暂无数据'}
                </p>
                {/* 比例条(reduced-motion 直接终态) */}
                <div
                    className="mt-3 h-2 w-full overflow-hidden rounded-full bg-slate-100"
                    role="img"
                    aria-label={layer.ratePct === null ? '命中率暂无数据' : `命中率 ${layer.ratePct}%`}
                >
                    {layer.ratePct !== null && (
                        <motion.div
                            className={cn('h-full rounded-full', accent.bar)}
                            initial={{ width: reduceMotion ? `${widthPct}%` : 0 }}
                            whileInView={{ width: `${widthPct}%` }}
                            viewport={{ once: true, margin: '-40px' }}
                            transition={reduceMotion ? { duration: 0 } : { duration: 0.45, ease: 'easeOut' }}
                        />
                    )}
                </div>
                {layer.description && (
                    <p className="mt-3 text-xs leading-5 text-slate-600">{layer.description}</p>
                )}
                {layer.businessMeaning && (
                    <p className="mt-1.5 text-xs leading-5 text-slate-500">{layer.businessMeaning}</p>
                )}
                {/*
                  * 仅在**后端没给合并表述**时才退回旧角标（老报告兼容）。
                  * 有 sampleNote 就说明样本情况已经并进上面那一行了，这里再摆一个
                  * "样本不足"角标就又变成左右脑互搏。
                  */}
                {!layer.dataSufficient && !layer.sampleNote && (
                    <p className="mt-2 inline-flex rounded bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-700 border border-amber-200">
                        {layer.total === null ? '本层未实测' : '样本较少，建议扩测后确认'}
                    </p>
                )}
            </Card>
        </motion.li>
    );
}

export function DecisionFunnel({ section }: { section: DataSection<DecisionFunnelData> }) {
    const parentVariants = useStaggerParent();
    return (
        <Section
            id="funnel"
            eyebrow="决策路径"
            title="决策漏斗"
            subtitle="把客户问题按决策深度分层，越往后越接近成交；层数与命名以本次实测口径为准。"
        >
            {section.status !== 'ready' ? (
                <EmptyBlock message={section.message} />
            ) : (
                <>
                    <motion.ol
                        variants={parentVariants}
                        initial="hidden"
                        whileInView="show"
                        viewport={{ once: true, margin: '-60px' }}
                        className="flex flex-col gap-4 xl:flex-row xl:items-start"
                    >
                        {section.data.layers.map((layer, i) => (
                            <LayerCard
                                key={layer.key}
                                layer={layer}
                                index={i}
                                prevRatePct={i > 0 ? section.data.layers[i - 1].ratePct : null}
                            />
                        ))}
                    </motion.ol>
                    {/* 文本等价(屏幕阅读器 + 打印兜底) */}
                    <ul className="sr-only">
                        {section.data.layers.map((layer) => (
                            <li key={layer.key}>
                                {layer.label}:命中率 {layer.ratePct ?? '暂无数据'}%,
                                {layer.sampleNote ?? `样本 ${layer.detected ?? '—'}/${layer.total ?? '—'}`}
                            </li>
                        ))}
                    </ul>
                    {(section.data.trust.partialSample || section.data.trust.levelCapped) && (
                        <p className="mt-4 text-xs text-slate-500">
                            注：本次存在未实测层级，总分按有样本层级折算
                            {section.data.trust.levelCapped ? ',等级已按规则封顶以避免过度承诺' : ''}。
                        </p>
                    )}
                </>
            )}
        </Section>
    );
}
