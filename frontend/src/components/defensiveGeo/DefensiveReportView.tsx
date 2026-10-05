/**
 * 防御型 GEO 五卡报告(CUR-01 的落点)—— 规格 §8.1/§8.2/§9.4。
 *
 * §8.1 客户首屏按**客户问题**组织,不平铺十张 KPI 卡。
 * 五张卡的顺序由服务端 registry 决定(MET-29 禁换序),前端不重排。
 *
 * 🔴 未签发时画什么
 * -----------------
 * `customerPresentationSigned=false` ⇒ 对客终态口径还没签,
 * 页面**照常给服务商看**,但打上「测试数据 · 不可发客户」水印(U-8),
 * 且不提供任何"发给客户"的入口。这比整页拦掉更符合现状:
 * 服务商需要看,只是还不能对外发。
 *
 * 🔴 卡片数据为空时说人话,不显示 0
 * §9.7 逐字「不显示 0 分;解释『还没有测试数据』」。
 */

import { AlertTriangle } from 'lucide-react';
import { DEFGEO_COPY } from '@/lib/defensiveGeoCopy';
import { cn } from '@/lib/utils';
import { CUSTOMER_CARDS } from '@/lib/defensiveGeoPresentation';
import { renderableActions } from '@/lib/defensiveGeoActions';

export interface PresentationCard {
    key: string;
    question: string;
    ordinal: number;
    levelKey: string;
    levelLabel: string;
    levelTone: 'positive' | 'warning' | 'critical' | 'neutral';
    state: string;
    actions: string[];
}

export interface ReportPresentation {
    diagnosisId: number;
    isV2: boolean;
    campaignMode?: string | null;
    modeUserLabel?: string | null;
    customerPresentationSigned: boolean;
    unsignedPolicies: string[];
    cards: PresentationCard[];
    projectionVersion?: string | null;
    questionPlanId?: string | null;
    questionPlanRevision?: number | null;
}

const TONE_CLASS: Record<PresentationCard['levelTone'], string> = {
    positive: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    warning: 'bg-amber-50 text-amber-700 border-amber-200',
    critical: 'bg-orange-50 text-orange-700 border-orange-200',
    neutral: 'bg-muted text-muted-foreground border-border',
};

export function DefensiveReportView({ presentation }: { presentation: ReportPresentation }) {
    const byKey = new Map(presentation.cards.map((c) => [c.key, c]));

    return (
        <div className="space-y-4">
            {!presentation.customerPresentationSigned && (
                <div
                    role="note"
                    className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3"
                >
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden />
                    <div>
                        <p className="text-[13px] font-medium text-amber-900">
                            测试数据 · 不可发客户
                        </p>
                        <p className="text-[12px] text-amber-800">
                            对客口径还在确认中,这份结果目前只供你自己查看。
                        </p>
                    </div>
                </div>
            )}

            <header className="space-y-1">
                <h2 className="text-[20px] font-semibold leading-8">
                    客户问到您时,AI 能不能认对、讲清、给出选择理由?
                </h2>
                <p className="text-[14px] leading-6 text-muted-foreground">
                    这份体检不只看“有没有提到”,还会检查 AI 认的是不是同一家公司、
                    是否愿意推荐、哪些问题仍答不上来,以及回答依据来自哪里。
                </p>
                {presentation.modeUserLabel && (
                    <p className="text-[13px] text-muted-foreground">
                        本次目标:{presentation.modeUserLabel}
                    </p>
                )}
            </header>

            {/* 🔴 [#63 2026-09-05] 一张都没有 ⇒ **不渲染五个空壳**。
                「这一项没测到」(下面每张卡自己的留白)与「整份没有五卡投影」
                处置相反:前者继续看别的卡,后者这个视图根本不适用。
                压成一档,她看到的就是五个「暂无结论」加零出口(诊断 692)。 */}
            {presentation.cards.length === 0 && (
                <p role="note" className="rounded-md border border-border bg-muted/50 p-3 text-[13px] text-muted-foreground">
                    {DEFGEO_COPY.fiveCardSummaryUnavailable}
                </p>
            )}
            {/* 顺序来自服务端 registry 的镜像常量,前端不重排(MET-29) */}
            {presentation.cards.length > 0 && (
            <div className="grid gap-3 md:grid-cols-2">
                {CUSTOMER_CARDS.map((spec) => {
                    const card = byKey.get(spec.key);
                    return (
                        <section
                            key={spec.key}
                            className="rounded-lg border border-border p-4"
                            aria-labelledby={`card-${spec.key}`}
                        >
                            <div className="flex items-start justify-between gap-2">
                                <h3 id={`card-${spec.key}`} className="text-[15px] font-semibold">
                                    {spec.question}
                                </h3>
                                <span
                                    className={cn(
                                        'shrink-0 rounded border px-2 py-0.5 text-[12px]',
                                        TONE_CLASS[card?.levelTone ?? 'neutral'],
                                    )}
                                >
                                    {card?.levelLabel ?? '暂无结论'}
                                </span>
                            </div>
                            {!card && (
                                <p className="mt-2 text-[13px] text-muted-foreground">
                                    还没有这一项的测试数据。
                                </p>
                            )}
                            {card && (
                                <>
                                    {/* §9.7:没有有效回答时说人话,**不显示 0**。
                                        state 由服务端 presentation_state_rule_v1 派生,
                                        前端不自己按数字判档(CUR-03 的教训)。 */}
                                    {card.state === 'no_conclusion' && (
                                        <p className="mt-2 text-[13px] text-muted-foreground">
                                            这一项本次没有形成有效结论 —— 不是 0 分,
                                            是还没测到足够的有效回答。
                                        </p>
                                    )}
                                    {card.state === 'partial' && (
                                        <p className="mt-2 text-[13px] text-muted-foreground">
                                            这一项只测到部分平台,结果先看着,别急着下结论。
                                        </p>
                                    )}
                                    {/* UI-37 / POR-16:每个动作都必须点得到。
                                        renderableActions 会丢弃前端没有绑定的 key,
                                        而不是画一个点不动的死按钮。 */}
                                    {(() => {
                                        const actions = renderableActions(card.actions ?? [], {
                                            diagnosisId: presentation.diagnosisId,
                                            questionPlanId: presentation.questionPlanId ?? '',
                                            questionPlanRevision:
                                                presentation.questionPlanRevision ?? '',
                                        });
                                        if (actions.length === 0) return null;
                                        return (
                                            <div className="mt-3 flex flex-wrap gap-2">
                                                {actions.map((a) => (
                                                    <a
                                                        key={`${spec.key}:${a.key}`}
                                                        href={a.route}
                                                        className={cn(
                                                            'inline-flex min-h-[44px] items-center rounded-md',
                                                            'border border-border px-3 text-[14px]',
                                                            'hover:bg-muted focus-visible:outline',
                                                            'focus-visible:outline-2',
                                                        )}
                                                    >
                                                        {a.label}
                                                    </a>
                                                ))}
                                            </div>
                                        );
                                    })()}
                                </>
                            )}
                        </section>
                    );
                })}
            </div>
            )}
        </div>
    );
}
