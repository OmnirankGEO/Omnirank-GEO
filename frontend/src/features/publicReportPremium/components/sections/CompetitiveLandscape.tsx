/**
 * CompetitiveLandscape — 竞争格局
 *
 * 仅在后端存在真实竞品数据时展示；无可靠分母 → 只显示次数，禁止拼百分比；
 * 差距用次数差表达，不伪造排名结论。
 */
import { Card, EmptyBlock, Section } from '../ui/primitives';
import { shouldClaimExclusion, exclusionClaimText } from '@/features/publicReportPremium/exclusionClaim';
import type { CompetitiveData, DataSection } from '../../contract/types';
import { cn } from '@/lib/utils';

export function CompetitiveLandscape({ section }: { section: DataSection<CompetitiveData> }) {
    if (section.status !== 'ready') {
        return (
            <Section
                id="competitive"
                eyebrow="对照"
                title="竞争格局"
                subtitle="与同一批问题中被共同提及的品牌对照。"
            >
                <EmptyBlock message={section.message} />
            </Section>
        );
    }

    const {
        competitors,
        ownMentionCount,
        ownRecommendCount,
        hasReliableDenominator,
        sampleScope,
        denominatorNote,
        competitorSource,
        emptyReason,
        excludedBrandDirectedCount,
    } = section.data;
    const maxMention = Math.max(ownMentionCount ?? 0, ...competitors.map((c) => c.mentionCount), 1);

    const rows = [
        ...(ownMentionCount !== null
            ? [{ name: '本品牌', mention: ownMentionCount, recommend: ownRecommendCount, own: true }]
            : []),
        ...competitors.map((c) => ({ name: c.name, mention: c.mentionCount, recommend: c.recommendCount, own: false })),
    ].sort((a, b) => b.mention - a.mention);

    return (
        <Section
            id="competitive"
            eyebrow="对照"
            title="竞争格局"
            subtitle={sampleScope ? `样本口径：${sampleScope}` : '与同一批问题中被共同提及的品牌对照。'}
        >
            <Card className="p-4 sm:p-5">
                {!hasReliableDenominator && (
                    <p className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                        {denominatorNote ?? '本次缺少可靠分母，以下仅展示被提及次数，不计算比例。'}
                    </p>
                )}
                {/* [P1-7] 同行名单来源要写清；来自监测汇总时不能让客户误读成"本次实测结果" */}
                {competitorSource === 'monitoring' && competitors.length > 0 && (
                    <p className="mb-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
                        本次实测没有汇总出同行名单，下表同行数据来自该品牌近 90 天的持续监测记录。
                    </p>
                )}
                {/* [P1-7] 名单为空时说明原因，不让"只剩本品牌一条"被读成"这个行业没有对手" */}
                {competitors.length === 0 && emptyReason && (
                    <p className="mb-4 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
                        {emptyReason}
                    </p>
                )}
                <ul className="space-y-3">
                    {rows.map((row) => (
                        <li key={row.name}>
                            <div className="flex items-baseline justify-between gap-3">
                                <p className={cn('min-w-0 break-words text-sm', row.own ? 'font-semibold text-slate-900' : 'text-slate-700')}>
                                    {row.name}
                                    {row.own && <span className="ml-1.5 text-[11px] font-normal text-sky-700">(你的品牌)</span>}
                                </p>
                                <p className="shrink-0 text-xs tabular-nums text-slate-500">
                                    被提及 <span className="font-semibold text-slate-800">{row.mention}</span> 次
                                    {row.recommend !== null && (
                                        <>
                                            {' · '}出现正面措辞 <span className="font-semibold text-slate-800">{row.recommend}</span> 次
                                        </>
                                    )}
                                </p>
                            </div>
                            <div
                                className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-slate-100"
                                role="img"
                                aria-label={`${row.name} 被提及 ${row.mention} 次`}
                            >
                                <div
                                    className={cn('h-full rounded-full', row.own ? 'bg-sky-600' : 'bg-slate-300')}
                                    style={{ width: `${Math.max(2, (row.mention / maxMention) * 100)}%` }}
                                />
                            </div>
                        </li>
                    ))}
                </ul>
                {/*
                  * 🔴 [#236-c1a] 这句排除声明原来是**无条件**写死的 —— 它陈述的是
                  *    「本代码打算排除」,不是「这次到底排没排」。真客户 #700/#726 这次
                  *    一条都没排,脚注照样对客户宣称排过。
                  *
                  * 🔴 只读后端结构化字段 `excludedBrandDirectedCount`(本次实际被排除的**回答条数**,
                  *    一道题 x 4 平台 = 4 条),**不另算**;缺省(老报告)按 0 处理 ⇒ 不显示。
                  *    不许回落成「按老样子显示」——那等于继续宣称排过。
                  *    也不读 `sampleScope` 那串口语:它把「没排」和「排了 0 条」压成同一种沉默。
                  */}
                <p className="mt-4 text-xs leading-5 text-slate-500">
                    次数含义：在本次实测问题中，该品牌被 AI 回答提及、以及其中出现正面措辞的次数。
                    {shouldClaimExclusion(excludedBrandDirectedCount) && (
                        <>{' '}{exclusionClaimText(excludedBrandDirectedCount)}</>
                    )}
                    {' '}次数受样本范围影响，不等于市场份额。
                </p>
            </Card>
        </Section>
    );
}
