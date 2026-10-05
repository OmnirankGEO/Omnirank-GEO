/**
 * Z-3.3 · 广告法**一键修复**。
 *
 * §0.5.6 Z-3.3 逐字:「默认 LLM 按 rule_id 生成候选改写,销售确认/微调后成新
 * revision;**不得实现成纯手工文本框**」。
 *
 * ══════════════════════════════════════════════════════════════════
 * 🔴 这一屏的形状本身就是判据
 * ══════════════════════════════════════════════════════════════════
 * 一期被判"未兑现"的形态是:命中广告法 → 给一颗按钮 → 跳通用编辑器。
 * 那等于把问题原样还给她。
 *
 * 所以这里的**第一颗**按钮是「看 AI 改好的版本」,点完直接出候选句子;
 * 手工微调是**第二步**(选中一条之后才出现输入框),不是唯一出口。
 * 判据可以断言:第一颗按钮出现时,页面上**没有**可编辑输入框。
 *
 * 🔴 候选是复数。只给一条等于替她做决定,而她比我们清楚这家客户能不能这么说。
 * 🔴 「没有扣费」那半句在命中话术里就有(规格逐字),这里原样渲染服务端下发的句子,
 *    不自己改写 —— 涉钱的话改一个字都可能变成另一个承诺。
 */

import { useCallback, useState } from 'react';
import { AlertTriangle, Loader2, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { formatApiErrorForDisplay } from '@/lib/api';
import { DEFGEO_COPY } from '@/lib/defensiveGeoCopy';
import {
    fetchLegalRepairCandidates, type LegalRepairCandidate,
} from '@/lib/defensiveGeoAssistApi';

interface Props {
    articleRevisionId: string;
    ruleId: string;
    passageRef: string;
    passageExcerpt: string;
    /** 她定稿之后交回调用方 —— [工单 C-5] 调用方负责真落成新 revision。
     *  允许返回 Promise:落库是一次网络往返,调用方要能 await 它。 */
    onApply: (finalText: string) => void | Promise<void>;
}

export function LegalRepairPanel({
    articleRevisionId, ruleId, passageRef, passageExcerpt, onApply,
}: Props) {
    const [phase, setPhase] = useState<'idle' | 'loading' | 'picked' | 'listed'>('idle');
    const [candidates, setCandidates] = useState<LegalRepairCandidate[]>([]);
    // 🔴 显式标注 string:DEFGEO_COPY 是 `as const`,不标会把 state 的类型
    //    窄成那一个字面量,服务端下发的句子就赋不进来。
    const [explanation, setExplanation] = useState<string>(DEFGEO_COPY.legalRuleHitRepairable);
    const [draft, setDraft] = useState('');
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async () => {
        setPhase('loading'); setError(null);
        try {
            const res = await fetchLegalRepairCandidates({
                articleRevisionId, ruleId, passageRef, passageExcerpt,
            });
            setCandidates(res.candidates);
            setExplanation(res.publicExplanation);
            setPhase('listed');
        } catch (e) {
            setError(formatApiErrorForDisplay(
                e, '这次没能生成改写；没有扣费，你也可以自己改这一句。', 'agent'));
            setPhase('idle');
        }
    }, [articleRevisionId, ruleId, passageRef, passageExcerpt]);

    return (
        <section
            data-testid="legal-repair-panel"
            data-phase={phase}
            className="space-y-3 rounded-lg border border-amber-300 bg-amber-50/70 p-3 lg:p-4"
            aria-live="polite"
        >
            <p className="flex items-start gap-1.5 text-[13px] leading-relaxed text-amber-900">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                <span data-testid="legal-repair-explanation">{explanation}</span>
            </p>

            <blockquote
                data-testid="legal-repair-passage"
                className="rounded-md border border-amber-200 bg-white/70 p-2 text-[13px] text-foreground"
            >
                {passageExcerpt}
            </blockquote>

            {error && <p role="alert" className="text-[13px] text-amber-800">{error}</p>}

            {/* ── 第一步:**先给改好的句子**,不是给一个空输入框 ────────── */}
            {phase !== 'listed' && phase !== 'picked' && (
                <Button
                    type="button" size="sm" disabled={phase === 'loading'}
                    data-testid="legal-repair-generate" onClick={() => void load()}
                >
                    {phase === 'loading'
                        ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" aria-hidden />
                        : <Sparkles className="mr-1 h-3.5 w-3.5" aria-hidden />}
                    {DEFGEO_COPY.viewLegalRepairCandidates}
                </Button>
            )}

            {phase === 'listed' && (
                <ul data-testid="legal-repair-candidates" className="space-y-2">
                    {candidates.map((c, i) => (
                        <li key={`${i}-${c.text.slice(0, 12)}`}
                            className="rounded-md border border-border bg-background p-2.5">
                            <p className="text-[13px] leading-relaxed">{c.text}</p>
                            <p className="mt-1 text-[11px] text-muted-foreground">{c.note}</p>
                            <div className="mt-2 flex flex-wrap gap-2">
                                <Button
                                    type="button" size="sm"
                                    data-testid="legal-repair-apply"
                                    onClick={() => { void onApply(c.text); }}
                                >
                                    {DEFGEO_COPY.applyLegalRepair}
                                </Button>
                                <Button
                                    type="button" size="sm" variant="outline"
                                    data-testid="legal-repair-tweak"
                                    onClick={() => { setDraft(c.text); setPhase('picked'); }}
                                >
                                    {DEFGEO_COPY.editLegalRepair}
                                </Button>
                            </div>
                        </li>
                    ))}
                </ul>
            )}

            {/* ── 第二步:选中一条之后才出现的手工微调 ─────────────────── */}
            {phase === 'picked' && (
                <div className="space-y-2">
                    <Textarea
                        data-testid="legal-repair-editor"
                        rows={3}
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                    />
                    <div className="flex flex-wrap gap-2">
                        <Button type="button" size="sm" disabled={!draft.trim()}
                                data-testid="legal-repair-apply-edited"
                                onClick={() => { void onApply(draft.trim()); }}>
                            {DEFGEO_COPY.applyLegalRepair}
                        </Button>
                        <Button type="button" size="sm" variant="ghost"
                                onClick={() => setPhase('listed')}>
                            回到 AI 给的几条
                        </Button>
                    </div>
                </div>
            )}
        </section>
    );
}

export default LegalRepairPanel;
