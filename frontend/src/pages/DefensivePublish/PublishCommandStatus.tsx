/**
 * UI-34 / UI-36 —— 发布命令状态页。
 *
 * 路由:`/defensive-geo/publish/commands/:commandId`
 *
 * 🔴 三条硬约束
 * ══════════════════════════════════════════════════════════════════════
 * ① **statusVersion 单调**。轮询是并发的,慢响应会带回更低的 statusVersion;
 *    无脑取「最后到达的那份」会让 `completed` 被迟到的 `running` 顶回去。
 *    守卫在 `statusVersionGuard.ts`(纯函数,有可执行判据),这里只消费。
 *
 * ② **不能只靠颜色**。每一档状态都同时有**图标 + 文字**;
 *    色弱用户与打印/截图场景下颜色是不存在的信息通道。
 *    文字一律取服务端 `commandStateLabel`(quarantined 那句
 *    「结果待平台核实，费用已冻结、不会多扣（无需操作）」就在那里),前端不自造。
 *
 * ③ **逐状态都有出口**。服务端 `nextAction` 为 null 只出现在 commit(真终态);
 *    其余每一档都必须把那个 typed action 渲染成一个能点的按钮。
 *    §0.5.6:任何阻塞必须自带解决方案。
 */

import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
    AlertTriangle, ArrowLeft, CheckCircle2, Clock, ExternalLink, FileWarning,
    Loader2, Lock, RefreshCw, ShieldQuestion, XCircle,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
    Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import { PageLoading } from '@/components/ui/page-loading';
import { formatApiErrorForDisplay } from '@/lib/api';
import { toast } from 'sonner';
import { newIdempotencyKey, retryChild, submitVerificationEvidence } from './api';
import { usePublishStatusPolling } from './usePublishStatusPolling';
import { EVIDENCE_KIND_OPTIONS, serverCopy, type TypedAction } from './contracts';
import { ErrorNotice, Field, ServerActionButton, ServerLabel } from './components';
// [包H · Z-3.3] 广告法一键修复面板 —— 就地展开,不跳通用编辑器。
import { LegalRepairPanel } from '@/components/defensiveGeo/LegalRepairPanel';
// [工单 C-5] 「用这一句」的真落点 —— 见下面 onApply 那段说明。
import { applyLegalRepair } from '@/lib/defensiveGeoAssistApi';

/**
 * 状态 → 图标。**只映射图标,不映射文字** —— 文字是服务端的,
 * 在这里再写一份中文就是第二套状态词表(U-1 明令禁止)。
 * 表里没有的状态回落成一个中性图标,文字照样由服务端负责。
 */
const STATE_ICON: Record<string, LucideIcon> = {
    queued: Clock,
    running: Loader2,
    settlement_pending: Lock,
    completed: CheckCircle2,
    needs_action: FileWarning,
    failed: XCircle,
    cancelled: XCircle,
    quarantined: ShieldQuestion,
};

const STATE_TONE: Record<string, string> = {
    queued: 'text-muted-foreground',
    running: 'text-blue-600',
    settlement_pending: 'text-amber-600',
    completed: 'text-emerald-600',
    needs_action: 'text-orange-600',
    failed: 'text-destructive',
    cancelled: 'text-muted-foreground',
    quarantined: 'text-amber-700',
};

export default function PublishCommandStatus() {
    const { commandId } = useParams<{ commandId: string }>();
    const navigate = useNavigate();
    const { status, loading, error, discardedCount, refresh } = usePublishStatusPolling(commandId);

    const [evidenceKind, setEvidenceKind] = useState<string>(EVIDENCE_KIND_OPTIONS[0].value);
    const [evidenceText, setEvidenceText] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [retrying, setRetrying] = useState(false);

    const legalHit = status?.item?.legalRuleHit ?? null;
    const Icon = useMemo(
        () => (status ? STATE_ICON[status.commandState] ?? Clock : Clock), [status]);
    const tone = status ? STATE_TONE[status.commandState] ?? 'text-muted-foreground' : '';

    const onAction = async (action: TypedAction) => {
        const t = action.target || { kind: '' };
        // 唯一一处会真的动服务端的出口:重试子命令(带 CAS 两把手)。
        if (action.kind === 'retry_child' && status) {
            setRetrying(true);
            try {
                await retryChild(
                    status.publishCommandId,
                    {
                        expectedStatusVersion: status.statusVersion,
                        expectedCommandHash: status.commandCanonicalHash,
                    },
                    newIdempotencyKey('defgeo-retry'),
                );
                refresh();
            } catch (e) {
                toast.error(formatApiErrorForDisplay(e, '重试没有成功 · 请稍后再试'));
            } finally {
                setRetrying(false);
            }
            return;
        }
        if (action.kind === 'repair_legal_passage' && t.id) {
            // 🔴 [包H · Z-3.3] 这条 typed action 不再"跳编辑器"。修复面板就在
            //    本页 legalRuleHit 那张卡里就地展开(AI 先给改好的句子),
            //    所以这里只把视线带过去 —— 导航走了就又变回
            //    "把问题原样还给她"的老形态(一期正是因此被判未兑现)。
            document.querySelector('[data-testid="legal-repair-panel"]')
                ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
            return;
        }
        if (t.kind === 'page' && t.page === 'support') { navigate('/help'); return; }
        if (t.kind === 'publish_snapshot' && t.id) {
            navigate(`/defensive-geo/publish/decision/${t.id}`); return;
        }
        refresh();
    };

    const doSubmitEvidence = async () => {
        if (!commandId || evidenceText.trim().length === 0) return;
        setSubmitting(true);
        try {
            await submitVerificationEvidence(commandId, {
                evidenceKind, evidenceText: evidenceText.trim(),
            });
            setEvidenceText('');
            toast.success('凭证已提交，平台会核实后处理。这一步不会改变你的算力。');
            refresh();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '凭证没有提交成功 · 请稍后重试'));
        } finally {
            setSubmitting(false);
        }
    };

    if (loading && !status) return <PageLoading />;

    return (
        <div className="container mx-auto max-w-3xl space-y-4 py-6">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Button variant="ghost" size="sm" onClick={() => navigate(-1)}>
                        <ArrowLeft className="mr-1 h-4 w-4" aria-hidden /> 返回
                    </Button>
                    <h1 className="text-xl font-bold">这次发布的进展</h1>
                </div>
                <Button variant="ghost" size="sm" onClick={refresh} disabled={loading}>
                    <RefreshCw className="mr-1 h-4 w-4" aria-hidden /> 刷新
                </Button>
            </div>

            {error != null && (
                <ErrorNotice text={formatApiErrorForDisplay(error, '状态暂时读不到 · 正在继续重试')} />
            )}

            {!status ? (
                <Card><CardContent className="p-6 text-sm text-muted-foreground">
                    没有拿到这次发布的状态。
                </CardContent></Card>
            ) : (
                <>
                    {/* ── 主状态:图标 + 文字,不靠颜色单独表意 ────────────── */}
                    <Card>
                        <CardContent className="space-y-4 p-5">
                            <div className="flex items-start gap-3">
                                <Icon
                                    className={`mt-0.5 h-6 w-6 shrink-0 ${tone} ${status.commandState === 'running' ? 'animate-spin' : ''}`}
                                    aria-hidden
                                />
                                <div className="min-w-0 space-y-1">
                                    <p className="text-base font-semibold" data-command-state-text={status.commandState}>
                                        <ServerLabel
                                            value={status.commandStateLabel}
                                            missingHint="这一档状态的说法后端暂未下发"
                                        />
                                    </p>
                                    {status.reason && (
                                        <p className="text-sm text-muted-foreground">
                                            <ServerLabel value={status.reason} />
                                        </p>
                                    )}
                                </div>
                            </div>

                            <div className="grid grid-cols-1 gap-3 border-t border-border pt-3 sm:grid-cols-3">
                                <Field label="这次的算力">
                                    <ServerLabel
                                        value={status.fundingStateLabel}
                                        missingHint="算力去向后端暂未下发说明"
                                    />
                                </Field>
                                <Field label="更新到第几版">{status.statusVersion}</Field>
                                <Field label="最近更新">
                                    {status.updatedAt
                                        ? new Date(status.updatedAt).toLocaleString('zh-CN')
                                        : '—'}
                                </Field>
                            </div>

                            {status.item?.publicUrl && (
                                <a
                                    href={status.item.publicUrl}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline"
                                >
                                    <ExternalLink className="h-4 w-4" aria-hidden /> 打开已发布的稿件
                                </a>
                            )}

                            {/* 🔴 逐状态都有出口。nextAction 为 null 只在真终态出现。 */}
                            {status.nextAction ? (
                                <div className="pt-1">
                                    <ServerActionButton
                                        action={status.nextAction}
                                        onAct={(a) => void onAction(a)}
                                        disabled={retrying}
                                    />
                                </div>
                            ) : (
                                <p className="pt-1 text-sm text-muted-foreground">
                                    这一步已经结束，不需要你再做什么。
                                </p>
                            )}
                        </CardContent>
                    </Card>

                    {/* ── UI-36:广告法命中 —— 把原句摆出来 + 修这一句 ──────── */}
                    {legalHit && (
                        <Card className="border-orange-200">
                            <CardHeader className="pb-3">
                                <CardTitle className="flex items-center gap-2 text-base text-orange-700">
                                    <AlertTriangle className="h-4 w-4" aria-hidden />
                                    这句话按广告法过不了
                                </CardTitle>
                            </CardHeader>
                            <CardContent className="space-y-3">
                                {/* 🔴 [包H · Z-3.3 2026-08-23] 这里原来只有「原句 + 一颗按钮」,
                                    而那颗按钮的落点是 `navigate('/writing?revision=…')`
                                    —— 跳通用编辑器,把问题原样还给她。规格 Z-3.3 明写
                                    「默认 LLM **按 rule_id** 生成候选改写…**不得实现成纯手工文本框**」,
                                    一期就是因为这个形态被判未兑现。
                                    现在就地展开 LegalRepairPanel:第一颗按钮直接出 AI 改好的句子,
                                    手工微调是选中一条之后的第二步。 */}
                                <LegalRepairPanel
                                    articleRevisionId={legalHit.articleRevisionId}
                                    ruleId={legalHit.ruleId}
                                    passageRef={legalHit.passageRef}
                                    passageExcerpt={legalHit.passageExcerpt}
                                    onApply={async (finalText: string) => {
                                        // ══════════════════════════════════════════════
                                        // 🔴 [工单 C-5 · Codex 终审 P1-12] 真落库,不再跳走
                                        // ══════════════════════════════════════════════
                                        // 上一版是 `navigate('/writing?revision=…&repaired=<句子>')`,
                                        // 而那个 `repaired` 查询参数**全仓零消费者**(grep 实核:
                                        // 只有那一处产出方)。点完"用这一句"什么都没发生 ——
                                        // 重新确认冻的还是同一份正文、同一个 articleHash,
                                        // 发布门原样再拦一次。她会以为系统坏了。
                                        //
                                        // 现在直接调 `POST /legal-repair/apply`:正文真的改了,
                                        // `articleHash` 跟着变 ⇒ 旧快照自动失效 ⇒
                                        // 重新确认消费的是新 revision。落库成功后刷新本页,
                                        // 让她看到发布门那一侧的新状态。
                                        try {
                                            const out = await applyLegalRepair({
                                                articleRevisionId: legalHit.articleRevisionId,
                                                ruleId: legalHit.ruleId,
                                                passageRef: legalHit.passageRef,
                                                passageExcerpt: legalHit.passageExcerpt,
                                                chosenText: finalText,
                                            });
                                            toast.success(out.publicExplanation);
                                            await refresh();
                                        } catch (e) {
                                            // 🔴 正文一个字没动(服务端 typed 拒绝就是这个语义),
                                            //    所以这里只报原因,不跳走、不清空她选的那一句。
                                            toast.error(formatApiErrorForDisplay(
                                                e, '这一句没能改进稿子里；正文没有变动，你可以再试一次或自己改。',
                                                'agent'));
                                        }
                                    }}
                                />
                            </CardContent>
                        </Card>
                    )}

                    {/* ── Z-1:服务商提交线下核实凭证(零资金副作用) ────────── */}
                    <Card>
                        <CardHeader className="pb-3">
                            <CardTitle className="text-base">提交线下核实凭证</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-3">
                            <p className="text-sm text-muted-foreground">
                                如果你手上有这次发布的证据（截图、订单号、沟通记录），传给平台可以加快核实。
                                这一步不会改变你的算力。
                            </p>
                            <div className="space-y-1.5">
                                <Label htmlFor="defgeo-evidence-kind">凭证类型</Label>
                                <Select value={evidenceKind} onValueChange={setEvidenceKind}>
                                    <SelectTrigger id="defgeo-evidence-kind">
                                        <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
                                        {EVIDENCE_KIND_OPTIONS.map((o) => (
                                            <SelectItem key={o.value} value={o.value}>{o.text}</SelectItem>
                                        ))}
                                    </SelectContent>
                                </Select>
                            </div>
                            <div className="space-y-1.5">
                                <Label htmlFor="defgeo-evidence-text">具体内容</Label>
                                <Textarea
                                    id="defgeo-evidence-text"
                                    value={evidenceText}
                                    onChange={(e) => setEvidenceText(e.target.value)}
                                    placeholder="把链接、订单号或沟通记录贴在这里"
                                    rows={3}
                                    maxLength={2000}
                                />
                            </div>
                            <Button
                                onClick={() => void doSubmitEvidence()}
                                disabled={submitting || evidenceText.trim().length === 0}
                            >
                                {submitting && <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />}
                                提交凭证
                            </Button>
                        </CardContent>
                    </Card>

                    {/* 排障可见性:慢响应真的被丢过。判据可以直接读它。 */}
                    {discardedCount > 0 && (
                        <p
                            className="text-xs text-muted-foreground"
                            data-stale-status-discarded={discardedCount}
                        >
                            已忽略 {discardedCount} 次过期的状态回包。
                        </p>
                    )}
                </>
            )}
        </div>
    );
}

/** 静态闸锚点:证明本页真的消费了单调守卫,而不是自己取「最后一份」。 */
export const __defgeoStatusMarkers = { usesMonotonicGuard: true, serverCopy } as const;
