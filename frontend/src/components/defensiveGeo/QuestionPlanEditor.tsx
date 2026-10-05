/**
 * 统一题单编辑器 —— 规格 §9.2 + CUR-06。
 *
 * CUR-06 的债务是「主问题区和高级区存在两套相似问题输入,再加防御问题会成为第三套」。
 * 所以这里是**一个**编辑器,不同来源用**标签**区分,而不是不同输入框。
 *
 * 🔴 切模式的三条规则(§9.2 逐字)
 * --------------------------------
 * - 无人工编辑 → 直接换建议题单;
 * - **有人工编辑 → 保留人工问题并询问是否切换**;
 * - **禁止静默删除用户输入**。
 *
 * 实现上把"她自己写的"与"系统/AI 建议的"分开存:换模式只重算建议那部分,
 * 人工那部分**原样保留**。这样"静默删除"在数据结构层就发生不了 ——
 * 靠 UI 提醒去避免删除,迟早会漏。
 */

import { useMemo } from 'react';
import { Plus, Trash2, User, Sparkles, Settings2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { CampaignMode } from './ModeRadioCards';
// [阶段④ · 订正八①] 侧别 → family/exposure 单源;新建题与改侧别共用。
import { sideDefaults, type ModeSide } from '@/pages/Diagnosis/launch/planSideGuard';
// [⑤b · 订正二十三③] 单条字数上限只镜像一次,来源见该模块抬头(≡ 服务端 validate_custom_questions)。
import { CUSTOM_QUESTION_MAX_CHARS, CUSTOM_QUESTION_MIN_CHARS, isQuestionTooLong, isQuestionCommittable } from '@/pages/Diagnosis/launch/ownQuestions';
import type { DraftQuestion, QuestionSource } from '@/lib/defensiveGeoApi';

const SOURCE_META: Record<QuestionSource, { label: string; icon: typeof User }> = {
    system: { label: '系统题', icon: Settings2 },
    user: { label: '你补充的', icon: User },
    ai: { label: 'AI 建议', icon: Sparkles },
};

/** 题族。对外说人话,内部 key 不上屏(§0.5.5 U-1)。 */
export const QUESTION_FAMILIES: Array<{
    key: string; label: string; side: 'defensive' | 'offensive';
}> = [
    { key: 'trust_reliability', label: '靠不靠谱', side: 'defensive' },
    { key: 'identity_scope', label: '是做什么的', side: 'defensive' },
    { key: 'fit_for_whom', label: '适合哪些人', side: 'defensive' },
    { key: 'comparison', label: '和竞品怎么选', side: 'defensive' },
    { key: 'category_choice', label: '这一类哪家好', side: 'offensive' },
    { key: 'recommendation_ask', label: '求推荐', side: 'offensive' },
];

function familyLabel(key: string): string {
    return QUESTION_FAMILIES.find((f) => f.key === key)?.label ?? '其他';
}

/* 🔴 [#233-a1] 建议题单的纯逻辑搬到 `./questionSuggest`(函数抬头的说明也一起搬走了)——
   那边零运行时依赖,判据能**真调**它而不是文本匹配。
   这里 re-export,既有 import 路径(NewDiagnosis 等)一律不用改。 */
export { suggestedQuestions, removalKey } from './questionSuggest';
export type { SuggestContext } from './questionSuggest';

interface Props {
    mode: CampaignMode;
    brandName: string;
    /** 系统/AI 建议的那部分。切模式时**只**重算这一半。 */
    suggested: DraftQuestion[];
    /** 她自己写的。切模式时**原样保留**。 */
    manual: DraftQuestion[];
    onManualChange: (next: DraftQuestion[]) => void;
    onRemoveSuggested: (index: number) => void;
    disabled?: boolean;
    /**
     * [包一阶段③ · 订正十二] 她出了自己的题、且没开「也跑 AI 出的题」⇒ AI 那批**不跑**。
     * 此时把它们折叠置灰,并把头部计数换成**实际要跑的数**。
     * 判定来自 `launch/questionRunPlan.ts` 单源,本组件不自己判三分支。
     */
    aiMuted?: boolean;
    /** 折叠态那一行人话(同样来自单源,不在本组件重打)。 */
    mutedNotice?: string;
    /**
     * 🔴 **实际要跑的题数**,由 `launch/questionRunPlan.effectiveQuestionCount` 算好传进来。
     *    本组件**不自己数** —— 屏幕上这个数与按钮上的价必须出自同一次计算,
     *    各算各的就会出现「显示 5 道、按 8 道收钱」而两处各自看起来都对。
     */
    runningCount?: number;
}

export function QuestionPlanEditor({
    mode, brandName, suggested, manual, onManualChange, onRemoveSuggested, disabled,
    aiMuted = false, mutedNotice, runningCount,
}: Props) {
    const all = useMemo(() => [...suggested, ...manual], [suggested, manual]);
    /**
     * 🔴 muted 时分组只按**她自己的题**算 —— AI 那批移到下方折叠块。
     *    不这么做的话两批仍混在同一个 family 分组里,
     *    「哪些会跑」在屏幕上就分不出来,而这正是订正十二要 UI 说清的那件事。
     */
    const running = useMemo(() => (aiMuted ? manual : all), [aiMuted, manual, all]);
    const grouped = useMemo(() => {
        const byFamily = new Map<string, DraftQuestion[]>();
        for (const q of running) {
            const list = byFamily.get(q.familyKey) ?? [];
            list.push(q);
            byFamily.set(q.familyKey, list);
        }
        return Array.from(byFamily.entries());
    }, [running]);

    /**
     * 🔴 [#143③] 把一行草稿**提交**进题单 —— 这一刻(而不是每次按键)才计数、才算价。
     *
     * 三条路都走这里:回车 / 失焦 / 点「加入」。够不够格由 `isQuestionCommittable`
     * 单点判定(与 `collectOwnQuestions` 同一个模块),不在这里另写一套长度规则。
     * 不够格就**原样留着**当草稿 —— 不清空、不报错:她可能只是想先离开去看别处。
     */
    const commitManual = (index: number) => {
        const q = manual[index];
        if (!q || !q.draft || !isQuestionCommittable(q.text)) return;
        const next = [...manual];
        next[index] = { ...q, text: q.text.trim(), draft: undefined };
        onManualChange(next);
    };

    const addManual = () => {
        const side: 'defensive' | 'offensive' =
            mode === 'offensive' ? 'offensive' : 'defensive';
        onManualChange([
            ...manual,
            {
                text: '',
                // 🔴 [#143③] 新行是**草稿**:提交(回车/失焦/点「加入」)前不计数、不算价。
                draft: true,
                modeSide: side,
                // [阶段④] family / exposure 跟着侧别走 —— 与侧别选择器共用同一份映射。
                ...sideDefaults(side),
                source: 'user',
            },
        ]);
    };

    return (
        <section className="space-y-4" aria-labelledby="question-plan-heading">
            <div className="flex items-center justify-between gap-3">
                <div>
                    <h3 id="question-plan-heading" className="text-[15px] font-semibold">
                        本次会问 AI 的问题
                    </h3>
                    <p className="text-[13px] text-muted-foreground">
                        共 <span data-testid="plan-running-count">{runningCount ?? running.length}</span> 道。启动后题单会冻结,方便下次按同样的问题再测一次。
                    </p>
                </div>
                <Button type="button" variant="outline" size="sm" onClick={addManual} disabled={disabled}>
                    <Plus className="mr-1 h-4 w-4" /> 加一道自己的问题
                </Button>
            </div>

            {/* 🔴 [阶段③ · 订正十二] AI 那批被静音时:折叠 + 置灰 + 一句人话。
                不是删掉 —— 她删光自己的题时这批要原样回来。 */}
            {aiMuted && suggested.length > 0 && (
                <details data-testid="plan-ai-muted" className="rounded-lg border border-dashed border-border opacity-60">
                    <summary className="cursor-pointer px-3 py-2 text-[13px] text-muted-foreground">
                        AI 为你出的 {suggested.length} 道题(这次不跑)
                    </summary>
                    <p data-testid="plan-ai-muted-note" className="px-3 pb-2 text-[12px] text-muted-foreground">
                        {mutedNotice}
                    </p>
                    <ul className="divide-y divide-border border-t border-border">
                        {suggested.map((q, i) => (
                            <li key={`muted-${i}-${q.text}`} className="px-3 py-2 text-[13px] text-muted-foreground line-through">
                                {q.text}
                            </li>
                        ))}
                    </ul>
                </details>
            )}

            {grouped.map(([family, items]) => (
                <div key={family} className="rounded-lg border border-border">
                    <div className="border-b border-border bg-muted/40 px-3 py-2 text-[13px] font-medium">
                        {familyLabel(family)}
                        <span className="ml-2 text-muted-foreground">{items.length} 道</span>
                    </div>
                    <ul className="divide-y divide-border">
                        {items.map((q) => {
                            const meta = SOURCE_META[q.source];
                            const Icon = meta.icon;
                            const manualIndex = manual.indexOf(q);
                            const isManual = manualIndex >= 0;
                            return (
                                <li key={`${q.source}-${q.text}-${q.familyKey}`} className="flex items-start gap-2 p-3">
                                    <span
                                        className={cn(
                                            'mt-0.5 inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[11px]',
                                            isManual
                                                ? 'bg-primary/10 text-primary'
                                                : 'bg-muted text-muted-foreground',
                                        )}
                                    >
                                        <Icon className="h-3 w-3" aria-hidden />
                                        {meta.label}
                                    </span>
                                    {/* 🔴 [WO §3.2] 侧别标签:**系统自动判定,不给编辑入口**。
                                        判错率有数据之后再考虑放开(Owner 拍板)。
                                        它同时把「hybrid 两侧都要有题」这条规则变成看得见的东西 ——
                                        原来这条规则在界面上不可见,删空了也没有任何提示。 */}
                                    <span data-testid="question-side-badge"
                                        className={cn(
                                            'mt-0.5 inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[11px] border',
                                            q.modeSide === 'defensive'
                                                ? 'border-primary/40 bg-primary/10 text-primary'
                                                : 'border-amber-400/50 bg-amber-50 text-amber-700',
                                        )}
                                        title={q.modeSide === 'defensive'
                                            ? '客户点名问你的时候,AI 会怎么说'
                                            : '客户没点名、只问「哪家好」的时候,AI 会不会提到你'}>
                                        {q.modeSide === 'defensive' ? '会问' : '会搜'}
                                    </span>
                                    {q.isFiller && (
                                        <span data-testid="question-filler-badge"
                                            className="mt-0.5 inline-flex shrink-0 items-center rounded bg-amber-100 px-1.5 py-0.5 text-[11px] text-amber-900"
                                            title="混合体检两类问题都要有,这一道是系统替你补上的;可以改也可以换成你自己的">
                                            系统补位
                                        </span>
                                    )}
                                    {isManual ? (
                                        <input
                                            className="min-h-[36px] flex-1 rounded border border-input bg-background px-2 py-1 text-[14px]"
                                            value={q.text}
                                            placeholder="客户可能会怎么问?回车加入"
                                            disabled={disabled}
                                            aria-label="你补充的问题"
                                            data-testid={q.draft ? 'manual-question-draft' : 'manual-question-committed'}
                                            onChange={(e) => {
                                                // 🔴 [#143③] 打字**只改文本**,不动 draft 标记 ——
                                                //    所以题数与算价在输入过程中一动不动。
                                                //    (改前:每敲一个字符就计入题数并打一次价格预览,
                                                //     输入「s」屏幕就说「已有 1 道问题」。)
                                                const next = [...manual];
                                                next[manualIndex] = { ...q, text: e.target.value };
                                                onManualChange(next);
                                            }}
                                            onKeyDown={(e) => {
                                                if (e.key !== 'Enter') return;
                                                e.preventDefault();
                                                commitManual(manualIndex);
                                            }}
                                            onBlur={() => commitManual(manualIndex)}
                                        />
                                    ) : (
                                        <span className="flex-1 text-[14px] leading-6">{q.text}</span>
                                    )}
                                    {/* 🔴 [⑤b · 订正二十三③] 超长在**打字时**就说,不等提交才 422。
                                        服务端 `validate_custom_questions` 对超长是 raise;
                                        前端归一化会先把它丢掉 —— 丢掉是静默的,所以必须在这里出声,
                                        否则「她写了 12 道、只跑 10 道」而屏幕上没有任何字解释那 2 道去哪了。 */}
                                    {/* 🔴 [#143③] 草稿行给一个**看得见的**提交动作。
                                        只有键盘路径(回车/失焦)的话,鼠标用户会以为已经加进去了 ——
                                        而屏幕上题数不动,她没有任何线索知道差一步。 */}
                                    {isManual && q.draft && (
                                        <button
                                            type="button"
                                            data-testid="manual-question-commit"
                                            className="shrink-0 rounded border border-input px-2 py-1 text-[12px] disabled:opacity-40"
                                            disabled={disabled || !isQuestionCommittable(q.text)}
                                            title={isQuestionCommittable(q.text)
                                                ? '加入题单(才会计入题数与价格)'
                                                : `至少 ${CUSTOM_QUESTION_MIN_CHARS} 个字才能加入`}
                                            onMouseDown={(e) => e.preventDefault()}
                                            onClick={() => commitManual(manualIndex)}
                                        >
                                            加入
                                        </button>
                                    )}
                                    {isManual && q.draft && q.text.trim().length > 0
                                        && !isQuestionCommittable(q.text) && (
                                        <span data-testid="manual-question-too-short"
                                            className="shrink-0 text-[11px] text-muted-foreground">
                                            至少 {CUSTOM_QUESTION_MIN_CHARS} 个字
                                        </span>
                                    )}
                                    {/* 🔴 [#153-A 2026-09-08] 这里原来是 `{isManual && …}` ——
                                        **把提示挡在系统/AI 题之外**。她自己写超长会标红;
                                        系统按超长品牌名拼出来的题超长则**一个字都不说**。
                                        而超长题在防守线的后果是**整单进不去**:不在提交时拦,
                                        后台派发时静默重试到退款(C 取证)。⇒ 任何来源的题都要出声。 */}
                                    {isQuestionTooLong(q.text) && (
                                        <span data-testid="question-too-long"
                                            className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-[11px] text-amber-900">
                                            超 {CUSTOM_QUESTION_MAX_CHARS} 字 · 这道不会跑
                                        </span>
                                    )}
                                    {/* 🔴 [阶段④ · 订正八①] 全面测试下,她自己加的题必须说清属哪一侧。
                                        自动收敛已退役 ⇒ 侧别不再由系统猜,由她选;
                                        改侧别时 family/exposure 跟着一起换(sideDefaults 单源),
                                        否则那道题会带着上一侧的分组值被后端归错组,而屏幕上看起来正常。 */}
                                    {isManual && mode === 'hybrid' && (
                                        <select
                                            data-testid="question-side-select"
                                            aria-label="这道题属于哪一条线"
                                            disabled={disabled}
                                            value={q.modeSide}
                                            onChange={(e) => {
                                                const side = e.target.value as ModeSide;
                                                const next = [...manual];
                                                next[manualIndex] = { ...q, modeSide: side, ...sideDefaults(side) };
                                                onManualChange(next);
                                            }}
                                            className="h-8 shrink-0 rounded border border-input bg-background px-1.5 text-[12px]"
                                        >
                                            <option value="defensive">防守</option>
                                            <option value="offensive">增长</option>
                                        </select>
                                    )}
                                    <Button
                                        type="button" variant="ghost" size="icon"
                                        aria-label="删掉这道问题" disabled={disabled}
                                        className="h-8 w-8 shrink-0"
                                        onClick={() => {
                                            if (isManual) {
                                                onManualChange(manual.filter((_, i) => i !== manualIndex));
                                            } else {
                                                onRemoveSuggested(suggested.indexOf(q));
                                            }
                                        }}
                                    >
                                        <Trash2 className="h-4 w-4" />
                                    </Button>
                                </li>
                            );
                        })}
                    </ul>
                </div>
            ))}

            {all.length === 0 && (
                <p className="rounded-lg border border-dashed border-border p-6 text-center text-[13px] text-muted-foreground">
                    还没有问题。加一道你的客户最常问的问题就能开始。
                </p>
            )}
        </section>
    );
}
