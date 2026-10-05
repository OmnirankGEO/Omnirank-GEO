/**
 * 正式启动面板 —— 两阶段 preview→confirm(§15.3)+ §0.5.5 U-4 两个必答时刻。
 *
 * 每屏三问在这一屏的答案:
 * - **我知道刚才发生了什么吗?** loading/empty/error/success 各有文字 + 图标,
 *   不只靠颜色;算价中显式说"正在算,还没有扣任何算力"。
 * - **我知道下一步点哪吗?** 恒一个主行动:未算价→「看看要花多少」;
 *   已算价→「确认并开始体检」;出错→服务端 nextAction.label。
 * - **我敢等吗?** 涉钱动作**前后**都有确定性反馈:点之前显示 costUserLabel,
 *   点之后显示 fundingStateUserLabel;任何失败都显式说
 *   **「没有扣除任何算力」**(§15.8 PREVIEW_EXPIRED 用户句里本就带这半句)。
 *
 * 🔴 前端不算钱。三个数字(base/extra/exact)与那句人话全部来自服务端。
 *    这里连加法都没有 —— 有加法就意味着前端有第二套算价。
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertCircle, CheckCircle2, Loader2, Wallet } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
    DefGeoError, confirmRunPreview, createRunPreview, newIdempotencyKey,
    type QuestionPlanResponse, type RunConfirmResponse, type RunPreviewResponse,
} from '@/lib/defensiveGeoApi';
import { cn } from '@/lib/utils';
import { DEFGEO_COPY } from '@/lib/defensiveGeoCopy';
import { planNextAction } from './nextActionRoute';
import { priceUnchanged } from '@/pages/DefensivePublish/deliveryTodo';
import { questionSetSignature } from '@/pages/Diagnosis/launch/planSideGuard';
import { CUSTOM_QUESTION_MAX_CHARS } from '@/pages/Diagnosis/launch/ownQuestions';
import { findTooLongQuestion, questionTooLongMessage } from '@/pages/Diagnosis/launch/questionTooLong';

interface Props {
    plan: QuestionPlanResponse | null;
    profileRevisionId: string;
    platformKeys: string[];
    onConfirmed: (result: RunConfirmResponse) => void;
    /** shadow/QA 运行:与正式按钮**视觉强区分**(§9.2 禁含糊按钮 + U-8)。 */
    shadow?: boolean;
    /** 当前草稿题数 —— 只用于常显面板的即时反馈,**不参与任何计价**。 */
    draftCount?: number;
    /**
     * 服务端下发的计价**规则**(最近一次成功 preview 缓存)。
     * 🔴 只渲染,**不做乘法** —— 总价仍只来自 run-previews。
     */
    pricingRule?: {
        basePoints: number; freeQuestions: number;
        extraPerQuestion: number; ruleVersion: string;
    } | null;
    /** 题集在上次算价之后变了 ⇒ 已算出的价对不上现在的题单(WO §3.2 第三态)。 */
    priceIsStale?: boolean;
    /**
     * 「重新算一下」的**第一步**:用当前题单重建 plan,并由页面亲自验过它绑的就是当前题单。
     * 返回新 plan ⇒ 本组件接着做第二步(给**新** plan 算价);返回 null ⇒ 失败,黄条留着。
     * 🔴 [#62] 它**不得**触碰 pricedSignature —— 签名只在算价成功后经 `onPriced` 写。
     */
    onRepriceRequested?: () => Promise<QuestionPlanResponse | null>;
    /**
     * 算价**成功**后回报「这次算的是哪一份题单」。传的是**被定价的那份 plan 自己装的题单**
     * 的签名,不是当时的 draft —— 两者在算价那一刻可能已经不同,而会真正跑的是 plan 里那份。
     */
    onPriced?: (signature: string) => void;
}

type Phase = 'idle' | 'pricing' | 'priced' | 'confirming' | 'done';

export function LaunchPanel({
    plan, profileRevisionId, platformKeys, onConfirmed, shadow, draftCount, pricingRule,
    priceIsStale, onRepriceRequested, onPriced,
}: Props) {
    const navigate = useNavigate();
    /**
     * 🔴 [#153-A] 题单里有没有超长题。文案**取自跨层注册表**
     *    (`DEFGEO_COPY.questionTooLong`,后端 registry v11 生成物),本文件不自带句子 ——
     *    前后端两份文案会漂,而漂开那天两边各自看起来都对。
     *    上限来自 `CUSTOM_QUESTION_MAX_CHARS` 单源(与服务端 validator 同解)。
     */
    const tooLongHit = findTooLongQuestion(plan?.questions || [], CUSTOM_QUESTION_MAX_CHARS);
    const tooLongMsg = tooLongHit
        ? questionTooLongMessage(DEFGEO_COPY.questionTooLong, tooLongHit, CUSTOM_QUESTION_MAX_CHARS)
        : null;
    const [phase, setPhase] = useState<Phase>('idle');
    const [preview, setPreview] = useState<RunPreviewResponse | null>(null);
    const [error, setError] = useState<DefGeoError | null>(null);
    // ── [包H · U-6 ②] preview 过期 → **自动**重建 → 价格没变时突出「直接确认」──
    // U-6 标题逐字:「她不需要『自己想起来』」。所以过期不是给她一个
    // 「请重新发起」的按钮,而是当场重算好,再告诉她数字变没变。
    // 🔴 rebuilt 只有两种非空值,分别对应两句不同的话:价格没变 / 价格变了。
    //    未知一律算「变了」(见 priceUnchanged 的保守侧)。
    const [rebuilt, setRebuilt] = useState<null | 'unchanged' | 'changed'>(null);
    // 🔴 幂等键在**整个确认过程**里保持不变。每次重试换一个 = 每次都是新命令。
    const [idempotencyKey] = useState(newIdempotencyKey);
    // 🔴 [G4] preview 与 confirm **各一把**键:两个不同命令,共用会互相顶掉;
    //    但各自跨重试不变 —— 否则抖动重试会造出第二条 preview,
    //    「她看到的那一版」与「她确认的那一版」就可能不是同一条。
    //
    // 🔴 [工单 V5-A · Codex fix-of-fix2 P2-1] 这把键有**两种**生命周期,必须分开:
    //    · **网络重试**(同一次逻辑预览,请求没发出去/没回来):键**不变**。
    //      换一把 = 同一次预览在后端变成两条命令,「她看到的那一版」就有了分身。
    //    · **用户重新发起**(她点了「重新预览」,或旧 preview 已经过期):
    //      这是**另一次**逻辑预览,必须换一把新键。沿用旧键会命中后端的幂等唯一约束
    //      `(tenant_owner_user_id, idempotency_key, canonical_request_hash)`,
    //      而 `insert_run_preview` 冲突时是 `DO NOTHING` + 回读**原来那一行** ——
    //      于是"重新预览"重放的是那条**已经过期/已经作废**的 preview,
    //      她点多少次都出不来。这就是 P2-1 里「即使改成 runPreview() 也没用」那一句。
    const [previewIdempotencyKey, setPreviewIdempotencyKey] = useState(newIdempotencyKey);

    const runPreview = async (
        comparePoints?: number | null,
        opts?: { newLogical?: boolean },
        // 🔴 [#62] 重算链的第二步要给**刚重建出来的那份** plan 算价。props 里的 `plan`
        //    在这一刻还是上一轮渲染的旧值(setState 未生效),用它就等于给旧 plan 算价 ——
        //    请求确实发了第二次,钱仍然错。所以显式传入,不靠闭包。
        planOverride?: QuestionPlanResponse | null,
    ) => {
        const usePlan = planOverride ?? plan;
        if (!usePlan) return;
        // 🔴 `newLogical` = 这是**另一次**预览,不是同一次的重试。取新键并留住它,
        //    后续的网络重试仍然复用这一把(所以是 set 进 state,不是每次现算)。
        const key = opts?.newLogical ? newIdempotencyKey() : previewIdempotencyKey;
        if (opts?.newLogical) setPreviewIdempotencyKey(key);
        setPhase('pricing'); setError(null);
        try {
            const p = await createRunPreview({
                questionPlanId: usePlan.planId,
                questionPlanRevision: usePlan.planRevision,
                profileRevisionId,
                platformKeys,
            }, key);
            setPreview(p); setPhase('priced');
            // 🔴 [#62] 签名**只在这里**写 —— 算价成功之后,且取自**被定价的那份 plan**。
            onPriced?.(questionSetSignature(usePlan.questions));
            if (comparePoints !== undefined) {
                setRebuilt(priceUnchanged(comparePoints, p.exactTotalPoints)
                    ? 'unchanged' : 'changed');
            }
        } catch (e) {
            setError(e as DefGeoError); setPhase('idle');
        }
    };

    const confirm = async () => {
        if (!preview) return;
        setPhase('confirming'); setError(null);
        try {
            // 所见即所签:传**她看到的那一版** hash,不是 latest。
            const result = await confirmRunPreview(
                preview.previewId, preview.canonicalHash, idempotencyKey);
            setPhase('done');
            onConfirmed(result);
        } catch (e) {
            const err = e as DefGeoError;
            // U-2:IDEMPOTENCY_CONFLICT **永不上屏**。它意味着同一条命令已经在跑,
            // 静默按成功处理即可 —— 弹一个错误框只会让她以为自己搞砸了。
            if (err.code === 'IDEMPOTENCY_CONFLICT') { setPhase('done'); return; }
            // [包H · U-6 ②] 过期不上错误框 —— 当场重建一份并落到新的确认态。
            // 🔴 比价用的是**她刚才看到的那个数**(preview.exactTotalPoints),
            //    不是重建后的自己跟自己比(那恒等,这句话就永远是"没变")。
            if (err.code === 'PREVIEW_EXPIRED') {
                // 🔴 旧的那条已经过期 —— 这是**新一次**逻辑预览,必须换键。
                //    沿用旧键会把那条过期的原样取回来,她就卡在这里出不去了。
                await runPreview(preview.exactTotalPoints, { newLogical: true });
                return;
            }
            setError(err); setPhase('priced');
        }
    };

    if (!plan) {
        // 🔴 [WO §3.2] 价格面板**常显**。原来这里是一句死占位
        //    「先把问题填好,这里就会显示…」—— 她还没填完就先被要求再等一轮。
        //    现在题数即时变,并说明计价方式;**但不显示任何具体价钱**:
        //    · 前端不算钱(资金铁律),总价只能来自服务端 run-previews;
        //    · 起步价/加价阈值这几个数**目前没有任何接口在算价前给出**,
        //      写死在前端等于复制一份价目表 —— 价目一改这句话就开始骗人。
        //    ⇒ 具体数字在她点一次「看看要花多少算力」之后由服务端给(basePoints /
        //      extraPoints / costUserLabel 都是它返回的)。见工单待补项。
        // 🔴 [#165 F3] 这两句原本是「**点下面的按钮**看这次要花多少算力」。
        //    问题:走到这个分支时 `!plan`,本面板**一颗按钮都没渲染** ——
        //    她眼里"下面的按钮"就是页面自己那颗「开始品牌体检 · 650 算力」,
        //    而那颗**一点就扣**。于是文案承诺的是"先看价",实际动作是"直接买"。
        //    (真人点测原话:以为点了是看价钱。)
        //
        //    🔴 改的是**文案不是行为**:元指令 2「按钮级确认扣费」的原文是
        //    「**不弹 Dialog**;按钮直接标价;大额(>=1000 分)走 toast 5s 反悔」——
        //    它禁止的正是"加一步二次确认",而 650 < 1000 连 toast 档都不到。
        //    那颗按钮本来就把价写在脸上(`launch-price-points`),已经合规;
        //    真正在骗人的是这里这句指路。工单写的「按元指令 2 加一步确认」
        //    与元指令 2 原文相反,**没有照做**,理由随交付回报。
        return (
            <aside data-testid="launch-panel" className="space-y-2 rounded-lg border border-border p-4">
                <h3 className="text-[15px] font-semibold">本次体检</h3>
                <p data-testid="launch-panel-draft-count" className="text-[13px]">
                    已有 <b>{draftCount ?? 0}</b> 道问题
                </p>
                {pricingRule ? (
                    // 🔴 数字**全部来自服务端**下发的规则,前端一个乘法都不做。
                    //    显示"规则"与显示"算出来的总价"是两件事:前者是服务端给的常量,
                    //    后者必须走 run-previews。
                    <p data-testid="launch-panel-pricing-rule" className="text-[12px] text-muted-foreground">
                        起步 <b>{pricingRule.basePoints}</b> 算力
                        (<b>{pricingRule.freeQuestions}</b> 道以内同价),
                        超出部分每题 <b>{pricingRule.extraPerQuestion}</b>。
                        题单生成后,这里显示这次实际要花多少。
                    </p>
                ) : (
                    // 还没成功 preview 过一次 ⇒ 规则拿不到(0 题时是 422 plan_empty)。
                    // 这时**不编数字**,只说计价方式。
                    <p className="text-[12px] text-muted-foreground">
                        按题数计价,题目不多时是同一个价。题单生成后,这里显示这次要花多少算力。
                    </p>
                )}
            </aside>
        );
    }

    return (
        <aside data-testid="launch-panel" className="space-y-3 rounded-lg border border-border p-4" aria-live="polite">
            <h3 className="text-[15px] font-semibold">本次体检</h3>

            <dl className="space-y-1.5 text-[13px]">
                <div className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">目标</dt>
                    {/* 人话来自服务端,前端不拿 mode 枚举编话 */}
                    <dd className="font-medium">{plan.modeUserLabel}</dd>
                </div>
                <div className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">问题</dt>
                    <dd className="font-medium">{plan.counts.total} 道</dd>
                </div>
                <div className="flex justify-between gap-2">
                    <dt className="text-muted-foreground">平台</dt>
                    <dd className="font-medium">{platformKeys.length} 个</dd>
                </div>
                {preview && (
                    <div className="flex justify-between gap-2">
                        <dt className="text-muted-foreground">要问的次数</dt>
                        <dd className="font-medium">{preview.plannedCells} 次</dd>
                    </div>
                )}
            </dl>

            {/* ── U-4 必答时刻其一:点之前就知道要花多少 ───────────────── */}
            {preview && (
                <div className={cn(
                    'flex items-start gap-2 rounded-md bg-muted/50 p-3',
                    // 🔴 [WO §3.2 第三态] 题集在算价之后变了 ⇒ 这个数**对不上现在的题单**。
                    //    置灰不是装饰:它是在说「别照这个数做决定」。
                    priceIsStale && 'opacity-45',
                )}>
                    <Wallet className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                    <div className="space-y-0.5">
                        <p className="text-[14px] font-medium">{preview.costUserLabel}</p>
                        <p className="text-[12px] text-muted-foreground">
                            {preview.fundingPolicyUserLabel}
                        </p>
                    </div>
                </div>
            )}

            {/* 🔴 [WO §3.2 第三态 · Review 2026-09-04] 题集变了 ⇒ 一行说明 + 一个能点的按钮。
                **不自动重算**:打后端会撞应用层限流,也会放大「她看到的那版 ≠ 她确认的那版」
                (幂等键分 preview / confirm 两把正是防这个)。
                置灰的数字必须配这一行 —— 只置灰不解释,她不知道该做什么。 */}
            {preview && priceIsStale && (
                <div data-testid="launch-price-stale"
                    className="rounded-md border border-amber-300 bg-amber-50 p-3 space-y-1.5">
                    <p className="text-[13px] text-amber-900">
                        题单变了,上面这个价是按改之前的题单算的。
                    </p>
                    <button type="button" data-testid="launch-reprice"
                        className="text-[12px] font-medium text-amber-900 underline underline-offset-2"
                        onClick={async () => {
                            // 有序两步:①重建 plan(页面验过绑当前题单)→ ②给**新** plan 算价。
                            // 第一步失败就停在这 —— 黄条留着,不许清。
                            const fresh = await onRepriceRequested?.();
                            if (!fresh) return;
                            await runPreview(undefined, { newLogical: true }, fresh);
                        }}>
                        重新算一下
                    </button>
                </div>
            )}

            {/* ── [包H · U-6 ②] 重建结果:先解释,再让她按 ────────────── */}
            {rebuilt && phase !== 'done' && (
                <div
                    data-testid="preview-rebuilt"
                    data-price={rebuilt}
                    role="status"
                    className="rounded-md border border-emerald-300 bg-emerald-50 p-3"
                >
                    <p className="text-[13px] leading-relaxed text-emerald-900">
                        {rebuilt === 'unchanged'
                            ? DEFGEO_COPY.previewRebuiltPriceUnchanged
                            : DEFGEO_COPY.previewRebuiltPriceChanged}
                    </p>
                </div>
            )}

            {phase === 'pricing' && (
                <p className="flex items-center gap-2 text-[13px] text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                    正在算这次要花多少算力,还没有扣除任何算力
                </p>
            )}

            {/* ── 错误:不吓人 + 带出口 + 显式说钱 ──────────────────── */}
            {error && !error.isSilent && (
                <div role="alert" className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3">
                    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden />
                    <div className="space-y-1">
                        <p className="text-[13px] text-amber-900">{error.userSentence}</p>
                        {/* 服务端的用户句里未必带这半句,兜一次 —— 涉钱失败必须显式说 */}
                        {!error.userSentence.includes('算力') && (
                            <p className="text-[12px] text-amber-800">没有扣除任何算力。</p>
                        )}
                        {/* 🔴 [工单 V5-A · P2-1] 按 **nextAction.kind** 分发,不按 code 猜。
                            改动前:非 PREVIEW_EXPIRED 一律再点一次 confirm。于是价目被停用
                            (POLICY_UNAVAILABLE)时后端明明给的是「重新预览」,按钮却
                            **确定性地**重放同一次 confirm —— 同一个 503,一万次都一样。
                            kind 是服务端说的下一步,code 只是错误的名字;拿名字猜动作
                            必然在下一个新 code 上再错一次。 */}
                        {/* 🔴 [工单 V5-B · Codex fix-of-fix3 P2-NEW-5] 三档,不是两档。
                            V5-A 只给 new_preview 画按钮、其余一律降级成文字,漏了中间那一档:
                            服务端给 `top_up` + `{page:"wallet"}` 时站内**真的有** /wallet,
                            只显示"去充值算力"五个字等于把她推回去自己找路。
                            分档规则长在 nextActionRoute.ts(纯逻辑,判据能真的执行它),
                            这里只按结果渲染 —— 组件里不许再写第二份 kind 判断。 */}
                        {error.nextActionLabel && (() => {
                            const plan = planNextAction(error.envelope.nextAction);
                            if (plan?.tier === 'in_panel') {
                                return (
                                    <Button type="button" size="sm" variant="outline"
                                            data-testid="next-action-new-preview"
                                            onClick={() => runPreview(undefined, { newLogical: true })}>
                                        {error.nextActionLabel}
                                    </Button>
                                );
                            }
                            if (plan?.tier === 'navigate') {
                                return (
                                    <Button type="button" size="sm" variant="outline"
                                            data-testid="next-action-navigate"
                                            data-href={plan.href}
                                            onClick={() => navigate(plan.href)}>
                                        {error.nextActionLabel}
                                    </Button>
                                );
                            }
                            // 🔴 第③档:站内没有落点(request_approval / request_budget_approval /
                            //    wait / change_plan / view_existing_command …)。
                            //    把服务端那句话原样显示 —— 她仍然知道下一步是什么;
                            //    但**不给一颗点了会 404 或做错事的按钮**。
                            return (
                                <p data-testid="next-action-text"
                                   className="text-[12px] text-amber-800">
                                    {error.nextActionLabel}
                                </p>
                            );
                        })()}
                    </div>
                </div>
            )}

            {phase === 'done' && (
                <p className="flex items-center gap-2 text-[13px] text-emerald-700">
                    <CheckCircle2 className="h-4 w-4" aria-hidden />
                    已开始体检,完成后会通知你
                </p>
            )}

            {/* ── 每屏恰一个主行动 ─────────────────────────────────── */}
            {/* 🔴 [#153-A] 灰按钮必须配一句人话 —— 这是本单的**全部意义**:
                改前这条链上一个字都没有(冻了算力 → 什么都没发生 → 退款)。
                句子取自跨层注册表,与后端冻结校验**同一句**。 */}
            {tooLongMsg && (
                <p role="status" data-testid="question-too-long-block"
                   className="mb-2 text-xs text-muted-foreground">
                    {tooLongMsg}
                </p>
            )}
            {phase !== 'done' && (
                preview
                    ? (
                        <Button
                            type="button" className="w-full"
                            variant={shadow ? 'outline' : 'default'}
                            // 🔴 [#62] 过期价**不许确认**。这条与 UI 形态无关:
                            //    无论最后是黄条按钮还是自动重算,「不许拿对不上的价扣钱」都成立。
                            /**
                             * 🔴 [#153-A] 有超长题 ⇒ **不许确认**,与后端冻结校验同口径。
                             *    C 取证:超长题在防守线**整单进不去**,而且不在提交时拦 ——
                             *    后台派发时静默重试到退款。也就是说点下去的结果是
                             *    「冻了算力 → 什么都没发生 → 退款」,全程屏幕上没有一个字。
                             *    ⇒ 能在这里拦住就别只提示。
                             */
                            disabled={phase === 'confirming' || !preview.canConfirm
                                || !!priceIsStale || !!tooLongHit}
                            onClick={confirm}
                        >
                            {phase === 'confirming'
                                ? (<><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在开始…</>)
                                : shadow ? '测试运行 · 不扣算力'
                                    // U-6 逐字点名的那句突出提示。价格变了就**不**说这句 ——
                                    // 说错一次她以后不会再信任何一句"没变"。
                                    : rebuilt === 'unchanged' ? DEFGEO_COPY.confirmUnchangedPrice
                                        : rebuilt === 'changed' ? DEFGEO_COPY.reviewNewPrice
                                            : '确认并开始体检'}
                        </Button>
                    )
                    : (
                        <Button
                            type="button" className="w-full" variant="outline"
                            disabled={phase === 'pricing' || plan.counts.total === 0}
                            onClick={() => runPreview()}
                        >
                            看看要花多少算力
                        </Button>
                    )
            )}

            {preview && priceIsStale && (
                <p data-testid="launch-confirm-blocked-stale"
                    className="text-[12px] text-muted-foreground">
                    先按现在的题单重新算一次价,才能开始 —— 上面这个数对不上你改过的题单。
                </p>
            )}

            {preview && !preview.canConfirm && (
                <p className="text-[12px] text-muted-foreground">
                    {preview.nextAction.label}
                </p>
            )}
        </aside>
    );
}
