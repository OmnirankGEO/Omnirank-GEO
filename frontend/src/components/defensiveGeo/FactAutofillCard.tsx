/**
 * Z-3.1 · 「AI 联网补齐」第四出口(四按钮卡)。
 *
 * §0.5.6 Z-3.1 逐字:``fact_collection`` 增加第四出口「AI 联网补齐」
 * (复用现役 ``autofill_brand`` 能力,扣算力,结果写入 manifest、**确认后生效**)。
 *
 * 四按钮形态与现役元指令「永远不中断对话」逐字一致:
 *   ✅ 就用这些资料 · 🤖 让 AI 联网帮你查 · ✏️ 我自己填 · ❌ 先不查
 *
 * 每屏三问:
 * - **发生了什么?** 查之前:说清这一步要花算力、还没扣;
 *   查之后:说清"这些是 AI 查到的,还没生效,你确认才写进去"。
 * - **点哪?** 恒有四条路,其中至少两条是免费的(自己填 / 先不查)。
 * - **敢等吗?** 涉钱那一句在**按下之前**就出现(U-4 铁律:先解释后出现)。
 *
 * 🔴 前端不算钱,也不写死价格。要花多少由服务端在她按下时按现役
 *    ``autofill_brand`` 价目实时签发;这里只负责"按之前先说这是要花钱的"。
 * 🔴 确认写回走现役 ``PUT /api/my-clients/{id}`` —— 本组件通过 ``onConfirm``
 *    把值交回调用方,不自己开第二条写入路径。
 */

import { useCallback, useState } from 'react';
import { AlertCircle, Loader2, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { formatApiErrorForDisplay } from '@/lib/api';
import { DEFGEO_COPY } from '@/lib/defensiveGeoCopy';
import { requestFactAutofill, type FactDraft } from '@/lib/defensiveGeoAssistApi';

interface Props {
    brandId: number;
    /** §15.6 RequestedFactKey。人话由服务端下发,这里只传 key。 */
    requestedFactKeys: string[];
    /** 她按下「就用这些资料」时把 draft 交回去,由调用方走现役写回路径。 */
    onConfirm: (drafts: FactDraft[]) => void;
    onCancel?: () => void;
    /** 「我自己填」的落点。默认回客户档案页。 */
    onEditMyself?: () => void;
}

export function FactAutofillCard({
    brandId, requestedFactKeys, onConfirm, onCancel, onEditMyself,
}: Props) {
    const [phase, setPhase] = useState<'idle' | 'running' | 'done'>('idle');
    const [drafts, setDrafts] = useState<FactDraft[]>([]);
    const [manualOnly, setManualOnly] = useState<{ key: string; label: string }[]>([]);
    const [explanation, setExplanation] = useState<string>(DEFGEO_COPY.brandFactsMissing);
    const [error, setError] = useState<string | null>(null);

    const run = useCallback(async () => {
        setPhase('running'); setError(null);
        try {
            const res = await requestFactAutofill(brandId, requestedFactKeys);
            setDrafts(res.drafts);
            setManualOnly(res.manualOnly);
            setExplanation(res.publicExplanation);
            setPhase('done');
        } catch (e) {
            // 涉钱失败必须把钱说死。服务端那句里通常已经带了,兜一次。
            setError(formatApiErrorForDisplay(
                e, '这次没能查到资料；没有扣除任何算力，你也可以自己填。', 'agent'));
            setPhase('idle');
        }
    }, [brandId, requestedFactKeys]);

    return (
        <section
            data-testid="fact-autofill-card"
            data-phase={phase}
            className="space-y-3 rounded-lg border border-border p-3 lg:p-4"
            aria-live="polite"
        >
            <p data-testid="fact-autofill-explanation" className="text-[13px] leading-relaxed">
                {explanation}
            </p>

            {phase === 'running' && (
                <p className="flex items-center gap-2 text-[13px] text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                    正在联网查这家公司的公开资料
                </p>
            )}

            {error && (
                <p role="alert" className="flex items-start gap-1.5 text-[13px] text-amber-700">
                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                    <span>{error}</span>
                </p>
            )}

            {phase === 'done' && drafts.length > 0 && (
                <dl data-testid="fact-autofill-drafts" className="space-y-1.5 text-[13px]">
                    {drafts.map((d) => (
                        <div key={d.factKey} className="flex flex-wrap justify-between gap-2">
                            {/* 字段名人话由服务端下发 —— 前端不把 factKey 画上屏 */}
                            <dt className="text-muted-foreground">{d.factLabel}</dt>
                            <dd className="max-w-[60%] break-words text-right font-medium">
                                {d.suggestedDisplay || '这一项没查到'}
                            </dd>
                        </div>
                    ))}
                </dl>
            )}

            {manualOnly.length > 0 && (
                <p data-testid="fact-autofill-manual-only" className="text-[12px] text-muted-foreground">
                    这几项 AI 查不到，需要你自己填：{manualOnly.map((m) => m.label).join('、')}
                </p>
            )}

            {/* ── 四按钮。免费的那两条永远在,她不会被逼着花钱 ────────── */}
            <div className="flex flex-wrap gap-2">
                {phase === 'done' && drafts.length > 0 && (
                    <Button type="button" size="sm" data-testid="fact-autofill-confirm"
                            onClick={() => onConfirm(drafts)}>
                        {DEFGEO_COPY.confirmAiFacts}
                    </Button>
                )}
                <Button
                    type="button" size="sm"
                    variant={phase === 'done' ? 'outline' : 'default'}
                    disabled={phase === 'running'}
                    data-testid="fact-autofill-run"
                    onClick={() => void run()}
                >
                    <Sparkles className="mr-1 h-3.5 w-3.5" aria-hidden />
                    {DEFGEO_COPY.aiAutofillFacts}
                </Button>
                <Button type="button" size="sm" variant="outline"
                        data-testid="fact-autofill-manual" onClick={onEditMyself}>
                    {DEFGEO_COPY.editFactsMyself}
                </Button>
                <Button type="button" size="sm" variant="ghost"
                        data-testid="fact-autofill-cancel" onClick={onCancel}>
                    {DEFGEO_COPY.cancelAiAutofill}
                </Button>
            </div>
        </section>
    );
}

export default FactAutofillCard;
