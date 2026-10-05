/**
 * U-3 · 五里程碑折成**一条**人话进度条。
 *
 * §0.5.5 U-3 逐字:「单条进度条『报价已发 → 客户已同意 → 待核单 → 已收款 → 服务中』,
 * 每态配唯一『下一步』按钮;``commercial_basis_established`` 等内部名禁上屏」。
 *
 * ══════════════════════════════════════════════════════════════════
 * 🔴 为什么这个组件到包H 才出现
 * ══════════════════════════════════════════════════════════════════
 * 后端投影(``services/defensive_geo/commercial_milestones.project``)一期就做完了,
 * 也从 ``/s/{token}`` 下发了;但前端**零消费点** ——
 * `lib/defensiveGeoPresentation.ts` 里只留了一个
 * `MILESTONE_STEPS = ['报价已发', …]` 的常量,全仓没有任何地方用它。
 *
 * 那个常量本身还是个隐患:它是**第二份**五格标签。后端哪天改了措辞,
 * 它不会跟着变,也不会有任何东西变红。所以本组件坚持画服务端下发的 `steps`,
 * 并把那个常量标成"仅供类型/兜底",不作为渲染来源。
 *
 * 每屏三问:
 * - **发生了什么?** 当前格高亮 + 一句 `userLabel`(人话,内部名永不上屏)。
 * - **点哪?** 恰一个「下一步」按钮,文案来自服务端 `nextAction.label`。
 * - **敢等吗?** 「已收款 → 服务中」之间那一格明说系统在准备开工,
 *   而不是让她盯着一个不动的进度条猜。
 */

import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';

export interface MilestoneProjection {
    milestone: string;
    userLabel: string;
    /** 1-based;`draft` 时为 0(还没进条)。 */
    stepIndex: number;
    stepTotal: number;
    /** 逐格标签,**服务端下发** —— 前端不写死这五个词。 */
    steps: string[];
    nextAction: { kind: string; label: string };
    projectionVersion: string;
}

export function CommercialProgressBar({
    view, onNext,
}: { view: MilestoneProjection | null; onNext?: (kind: string) => void }) {
    // 拿不到投影就**不画**。画一条全灰的空条比不画更糟:
    // 她会以为这一单卡在第一步了。
    if (!view || !Array.isArray(view.steps) || view.steps.length === 0) return null;

    return (
        <section
            data-testid="commercial-progress"
            data-step-index={view.stepIndex}
            className="space-y-2 rounded-lg border border-border p-3 lg:p-4"
            aria-label="这一单进行到哪了"
        >
            <ol className="flex flex-wrap items-center gap-1.5 lg:gap-2">
                {view.steps.map((label, i) => {
                    const position = i + 1;
                    const done = position < view.stepIndex;
                    const current = position === view.stepIndex;
                    return (
                        <li
                            key={label}
                            data-testid="commercial-progress-step"
                            data-state={current ? 'current' : done ? 'done' : 'todo'}
                            aria-current={current ? 'step' : undefined}
                            className={cn(
                                'rounded-full border px-2.5 py-1 text-[12px] lg:text-[13px]',
                                current && 'border-emerald-400 bg-emerald-50 font-medium text-emerald-800',
                                done && 'border-emerald-200 bg-emerald-50/50 text-emerald-700',
                                !current && !done && 'border-border text-muted-foreground',
                            )}
                        >
                            {/* 🔴 画的是服务端下发的那一份 steps,不是前端常量 */}
                            {label}
                        </li>
                    );
                })}
            </ol>

            <div className="flex flex-wrap items-center justify-between gap-2">
                {/* 人话状态词。内部枚举名(commercial_basis_established 等)永不上屏 */}
                <p data-testid="commercial-progress-label" className="text-[13px] font-medium">
                    {view.userLabel}
                </p>
                {/* U-3:每态**唯一**一个下一步 */}
                {view.nextAction?.label && (
                    <Button
                        type="button" size="sm" variant="outline"
                        data-testid="commercial-progress-next"
                        onClick={() => onNext?.(view.nextAction.kind)}
                    >
                        {view.nextAction.label}
                    </Button>
                )}
            </div>
        </section>
    );
}

export default CommercialProgressBar;
