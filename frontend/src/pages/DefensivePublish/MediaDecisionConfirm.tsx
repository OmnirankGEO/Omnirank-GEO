/**
 * UI-35 / §0.5.5 U-4 —— 媒体方案确认页。
 *
 * 路由:`/defensive-geo/publish/decision/:snapshotId`
 *
 * 🔴 这一页的四条硬约束(每条都对应一个真实损失方向)
 * ══════════════════════════════════════════════════════════════════════
 * ① **恢复只走 exact GET**。刷新 / 后退 / 换设备一律 `GET decision-snapshots/{id}`,
 *    **永不重 POST preview** —— preview 会签发新冻结面并把旧的 CAS 成 superseded,
 *    也就是「刷新一下」会让用户刚看的报价当场作废、价格重算。
 *
 * ② **U-4 必答时刻**。确认按钮上方必须先说清「确认后消耗你的算力 X」,
 *    确认之后必须立刻说清「已扣 / 已冻结」——那句话取服务端 `fundingStateLabel`;
 *    走取消路径则说清「没有扣除任何算力」(cancel 端点契约:零 command / 零冻结 / 零外调)。
 *
 * ③ **文案零自造**。状态词、按钮名、调整方案的理由全部用服务端下发的串;
 *    拿不到就留白 + `data-server-copy-missing`(见 `components.tsx`),不编。
 *
 * ④ **canConfirm=false ⇒ 唯一一个出口**。不摆一排按钮让用户猜,
 *    也不在阻塞态下仍然把「确认」渲染成灰色按钮(灰按钮是最典型的死路)。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
    ArrowLeft, CheckCircle2, ChevronDown, Coins, Loader2, RefreshCw, Repeat, ShieldCheck,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
    Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { PageLoading } from '@/components/ui/page-loading';
import { formatApiErrorForDisplay } from '@/lib/api';
import {
    cancelDecisionSnapshot, confirmDecisionSnapshot, fetchDecisionSnapshot,
    newIdempotencyKey, overrideDecisionSnapshot,
} from './api';
import {
    actionIsRenderable, alternativeCandidates, otherOptions, recommendedOption, serverCopy,
    type AdjustmentOption, type ConfirmResponse, type MediaCandidate, type SnapshotResponse,
    type TypedAction,
} from './contracts';
import { ErrorNotice, Field, ServerActionButton, ServerLabel } from './components';

/** 确认之后这一页只剩两种结局,两种都必须把「钱动了没有」说死(U-4)。 */
type Outcome =
    | { kind: 'confirmed'; response: ConfirmResponse }
    | { kind: 'cancelled' };

export default function MediaDecisionConfirm() {
    const { snapshotId } = useParams<{ snapshotId: string }>();
    const navigate = useNavigate();

    const [data, setData] = useState<SnapshotResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [outcome, setOutcome] = useState<Outcome | null>(null);

    // 换一家:选中的备选 + 理由 + 该次意图的幂等键(弹窗打开时生成一次)
    const [swapTarget, setSwapTarget] = useState<MediaCandidate | null>(null);
    const [swapReason, setSwapReason] = useState('');
    const [swapKey, setSwapKey] = useState('');
    const [showOtherOptions, setShowOtherOptions] = useState(false);

    // 🔴 一次用户意图一个幂等键。在这里 useState 初始化 = 进页面时生成一次,
    //    重试同一个「确认」复用同一个键;换页面才换键。
    const [confirmKey] = useState(() => newIdempotencyKey('defgeo-confirm'));
    const [cancelKey] = useState(() => newIdempotencyKey('defgeo-cancel'));

    const load = useCallback(async () => {
        if (!snapshotId) return;
        setLoading(true);
        try {
            // ① 恢复只走 exact GET。这里**没有**任何 POST preview 的分支。
            setData(await fetchDecisionSnapshot(snapshotId));
            setError(null);
        } catch (e) {
            setError(formatApiErrorForDisplay(e, '这份方案暂时打不开 · 请稍后重试'));
        } finally {
            setLoading(false);
        }
    }, [snapshotId]);

    useEffect(() => { void load(); }, [load]);

    const snapshot = data?.snapshot ?? null;
    const decision = snapshot?.decision ?? null;
    const confirmability = data?.confirmability ?? null;
    const options = data?.adjustmentOptions ?? [];
    const alternatives = useMemo(
        () => (snapshot ? alternativeCandidates(snapshot) : []), [snapshot]);

    const exactPoints = decision?.exactPoints ?? snapshot?.totalExactPoints ?? null;
    const isInsufficient = confirmability?.reasonCode === 'insufficient_points';

    const onAction = (action: TypedAction) => {
        // typed action 的落地由 target 决定,前端不按 kind 猜路由。
        const t = action.target || { kind: '' };
        if (t.kind === 'page' && t.page === 'wallet') { navigate('/wallet'); return; }
        if (t.kind === 'page' && t.page === 'support') { navigate('/help'); return; }
        if (t.kind === 'publish_command' && t.id) {
            navigate(`/defensive-geo/publish/commands/${t.id}`); return;
        }
        if (t.kind === 'publish_snapshot' && t.id && t.id !== snapshotId) {
            navigate(`/defensive-geo/publish/decision/${t.id}`); return;
        }
        if (t.kind === 'quote' && t.id) { navigate('/pricing'); return; }
        void load();
    };

    const doConfirm = async () => {
        if (!snapshotId || !snapshot) return;
        setBusy(true);
        try {
            const res = await confirmDecisionSnapshot(
                snapshotId,
                { expectedHash: snapshot.canonicalHash, expectedVersion: snapshot.snapshotVersion },
                confirmKey,
            );
            setOutcome({ kind: 'confirmed', response: res });
            setError(null);
        } catch (e) {
            setError(formatApiErrorForDisplay(e, '确认没有成功 · 请稍后重试'));
        } finally {
            setBusy(false);
        }
    };

    const doCancel = async () => {
        if (!snapshotId || !snapshot) return;
        setBusy(true);
        try {
            await cancelDecisionSnapshot(
                snapshotId,
                { expectedHash: snapshot.canonicalHash, expectedVersion: snapshot.snapshotVersion },
                cancelKey,
            );
            setOutcome({ kind: 'cancelled' });
            setError(null);
        } catch (e) {
            setError(formatApiErrorForDisplay(e, '取消没有成功 · 请稍后重试'));
        } finally {
            setBusy(false);
        }
    };

    const doSwap = async () => {
        if (!snapshotId || !snapshot || !swapTarget) return;
        setBusy(true);
        try {
            const next = await overrideDecisionSnapshot(
                snapshotId,
                {
                    expectedHash: snapshot.canonicalHash,
                    expectedVersion: snapshot.snapshotVersion,
                    selectedPublicMediaOptionId: swapTarget.publicMediaOptionId,
                    actorReason: swapReason.trim(),
                },
                swapKey,
            );
            setSwapTarget(null);
            setSwapReason('');
            // override 会签发一份 child snapshot;跟着它走,别继续停在已 superseded 的旧 id 上。
            const childId = next.snapshot?.decisionSnapshotId;
            if (childId && childId !== snapshotId) {
                navigate(`/defensive-geo/publish/decision/${childId}`, { replace: true });
            } else {
                setData(next);
            }
            setError(null);
        } catch (e) {
            setError(formatApiErrorForDisplay(e, '换一家没有成功 · 请稍后重试'));
        } finally {
            setBusy(false);
        }
    };

    if (loading && !data) return <PageLoading />;

    // ── U-4 结局面:钱动了没有,这里必须说死 ────────────────────────────────
    if (outcome) {
        return (
            <div className="container mx-auto max-w-3xl space-y-4 py-6">
                <Card>
                    <CardHeader>
                        <CardTitle className="flex items-center gap-2 text-lg">
                            <CheckCircle2 className="h-5 w-5 text-emerald-600" aria-hidden />
                            {outcome.kind === 'confirmed' ? '已确认' : '已取消'}
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        {outcome.kind === 'confirmed' ? (
                            <>
                                {/* 🔴 U-4:「已扣 / 已冻结」取服务端 fundingStateLabel,不自造 */}
                                <div
                                    className="flex items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800"
                                    data-u4-settlement-notice="confirmed"
                                >
                                    <Coins className="h-4 w-4 shrink-0" aria-hidden />
                                    <ServerLabel
                                        value={outcome.response.fundingStateLabel}
                                        missingHint="这次的算力去向后端暂未下发说明"
                                    />
                                    <span className="text-emerald-700">
                                        · 本次 {outcome.response.exactSettlementPoints} 算力
                                    </span>
                                </div>
                                <ServerActionButton
                                    action={outcome.response.nextAction}
                                    onAct={() => navigate(
                                        `/defensive-geo/publish/commands/${outcome.response.publishCommandId}`)}
                                />
                            </>
                        ) : (
                            <div
                                className="flex items-center gap-2 rounded-md border border-border bg-muted/40 p-3 text-sm"
                                data-u4-settlement-notice="cancelled"
                            >
                                <Coins className="h-4 w-4 shrink-0" aria-hidden />
                                {/* cancel 端点契约:零 command / 零冻结 / 零外调 */}
                                <span>没有扣除任何算力。这篇稿子仍然要发，之后要再选一次媒体。</span>
                            </div>
                        )}
                    </CardContent>
                </Card>
            </div>
        );
    }

    return (
        <div className="container mx-auto max-w-3xl space-y-4 py-6">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Button variant="ghost" size="sm" onClick={() => navigate(-1)}>
                        <ArrowLeft className="mr-1 h-4 w-4" aria-hidden /> 返回
                    </Button>
                    <h1 className="text-xl font-bold">确认这次要发的媒体</h1>
                </div>
                <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}>
                    <RefreshCw className="mr-1 h-4 w-4" aria-hidden /> 刷新
                </Button>
            </div>

            {error && <ErrorNotice text={error} />}

            {!data || !snapshot || !decision ? (
                <Card><CardContent className="p-6 text-sm text-muted-foreground">
                    没有拿到这份方案。请返回上一步重新发起。
                </CardContent></Card>
            ) : (
                <>
                    {/* ── 推荐媒体 ────────────────────────────────────────── */}
                    <Card>
                        <CardHeader className="pb-3">
                            <CardTitle className="flex items-center gap-2 text-base">
                                <ShieldCheck className="h-4 w-4 text-emerald-600" aria-hidden />
                                我们建议发在这里
                            </CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                                <Field label="媒体">
                                    <ServerLabel value={decision.publicMediaName} />
                                </Field>
                                <Field label="站点">
                                    <ServerLabel value={decision.publicRootDomainLabel} />
                                </Field>
                                {/* 🔴 mediaRole 是闭集枚举,后端**没有**下发它的人话译名。
                                    这里如实留白,绝不把 authority_anchor 之类画到屏幕上(U-1)。 */}
                                <Field label="这家媒体的角色">
                                    <ServerLabel
                                        value={(decision as { mediaRoleLabel?: string }).mediaRoleLabel}
                                        missingHint="媒体角色的人话说明后端暂未下发"
                                    />
                                </Field>
                            </div>

                            {decision.reasonFacts?.length > 0 && (
                                <div className="space-y-1.5">
                                    <p className="text-xs text-muted-foreground">为什么是它</p>
                                    <ul className="space-y-1">
                                        {decision.reasonFacts.map((f, i) => (
                                            <li key={`${f.kind}-${i}`} className="flex items-start gap-2 text-sm">
                                                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" aria-hidden />
                                                {/* kind 是内部枚举,只画 label */}
                                                <ServerLabel value={f.label} />
                                            </li>
                                        ))}
                                    </ul>
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    {/* ── U-4 必答时刻 + 出口 ──────────────────────────────── */}
                    <Card>
                        <CardContent className="space-y-4 p-5">
                            {confirmability?.canConfirm ? (
                                <>
                                    {/* 🔴 U-4:先说清要花多少,再让她按 */}
                                    <p
                                        className="flex items-center gap-2 text-sm font-medium"
                                        data-u4-price-notice="pre-confirm"
                                    >
                                        <Coins className="h-4 w-4 text-amber-600" aria-hidden />
                                        {exactPoints === null
                                            ? '这次要消耗多少算力，后端暂未下发。'
                                            : `确认后消耗你的算力 ${exactPoints}`}
                                    </p>
                                    <div className="flex flex-wrap gap-2">
                                        <Button onClick={() => void doConfirm()} disabled={busy || exactPoints === null}>
                                            {busy && <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />}
                                            确认，就发这家
                                        </Button>
                                        <Button variant="outline" onClick={() => void doCancel()} disabled={busy}>
                                            先不选这家媒体
                                        </Button>
                                    </div>
                                </>
                            ) : isInsufficient ? (
                                /* ── U-7:推荐的那一个默认展开,其余折叠 ── */
                                <InsufficientPointsPanel
                                    explanation={confirmability?.publicExplanation}
                                    options={options}
                                    open={showOtherOptions}
                                    onOpenChange={setShowOtherOptions}
                                    onAct={onAction}
                                />
                            ) : (
                                /* ── 其它阻塞:说明 + **唯一**一个出口 ── */
                                <div className="space-y-3" data-blocked-single-exit="true">
                                    <BlockedExplanation text={confirmability?.publicExplanation} />
                                    <ServerActionButton
                                        action={confirmability?.nextAction ?? null}
                                        onAct={onAction}
                                    />
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    {/* ── 备选媒体 ────────────────────────────────────────── */}
                    {confirmability?.canConfirm && alternatives.length > 0 && (
                        <Card>
                            <CardHeader className="pb-3">
                                <CardTitle className="text-base">也可以换一家</CardTitle>
                            </CardHeader>
                            <CardContent className="space-y-2">
                                {alternatives.map((c) => (
                                    <div
                                        key={c.publicMediaOptionId}
                                        className="flex items-center justify-between gap-3 rounded-md border border-border p-3"
                                    >
                                        <div className="min-w-0 space-y-0.5">
                                            <p className="truncate text-sm font-medium">
                                                <ServerLabel value={c.publicMediaName} />
                                            </p>
                                            <p className="truncate text-xs text-muted-foreground">
                                                <ServerLabel value={c.publicRootDomainLabel} />
                                            </p>
                                        </div>
                                        <div className="flex shrink-0 items-center gap-2">
                                            <Badge variant="outline">{c.exactPoints} 算力</Badge>
                                            <Button
                                                variant="outline"
                                                size="sm"
                                                onClick={() => {
                                                    setSwapTarget(c);
                                                    setSwapReason('');
                                                    // 每次打开弹窗 = 一次新意图 → 一个新幂等键
                                                    setSwapKey(newIdempotencyKey('defgeo-override'));
                                                }}
                                            >
                                                <Repeat className="mr-1 h-3.5 w-3.5" aria-hidden /> 换一家
                                            </Button>
                                        </div>
                                    </div>
                                ))}
                            </CardContent>
                        </Card>
                    )}
                </>
            )}

            {/* ── 换一家:理由必填(服务端 min_length=4) ─────────────────── */}
            <Dialog open={swapTarget !== null} onOpenChange={(o) => { if (!o) setSwapTarget(null); }}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>换成「{swapTarget?.publicMediaName ?? ''}」</DialogTitle>
                    </DialogHeader>
                    <div className="space-y-3">
                        <p className="text-sm text-muted-foreground">
                            换一家之后价格会按新媒体重新出一份方案，你还要再确认一次。
                        </p>
                        <div className="space-y-1.5">
                            <Label htmlFor="defgeo-swap-reason">为什么换（必填，至少 4 个字）</Label>
                            <Input
                                id="defgeo-swap-reason"
                                value={swapReason}
                                onChange={(e) => setSwapReason(e.target.value)}
                                placeholder="例如：客户指定要发在这一家"
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setSwapTarget(null)} disabled={busy}>
                            算了
                        </Button>
                        <Button onClick={() => void doSwap()} disabled={busy || swapReason.trim().length < 4}>
                            {busy && <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />}
                            换成这一家
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    );
}

/**
 * 阻塞说明。**只渲染服务端下发的 `publicExplanation`**;
 * 没下发就如实说「后端没给说明」,不从 reasonCode 反推一句话。
 */
function BlockedExplanation({ text }: { text?: string }) {
    const verdict = serverCopy(text);
    if (verdict.ok) return <p className="text-sm">{verdict.text}</p>;
    return (
        <p className="text-sm text-muted-foreground" data-server-copy-missing={verdict.why}>
            这一步现在还不能确认。具体原因后端暂未下发说明，可以点下面的按钮继续。
        </p>
    );
}

/** U-7:推荐 1 个默认展开,其余收进「其他办法」。顺序按服务端给的,不重排。 */
function InsufficientPointsPanel({
    explanation, options, open, onOpenChange, onAct,
}: {
    explanation?: string;
    options: AdjustmentOption[];
    open: boolean;
    onOpenChange: (v: boolean) => void;
    onAct: (a: TypedAction) => void;
}) {
    const primary = recommendedOption(options);
    const rest = otherOptions(options);
    return (
        <div className="space-y-4" data-u7-options-panel="insufficient_points">
            <BlockedExplanation text={explanation} />

            {primary && (
                <div className="space-y-2 rounded-md border border-amber-200 bg-amber-50/60 p-3">
                    <p className="text-sm font-medium"><ServerLabel value={primary.publicReason} /></p>
                    <p className="text-sm text-muted-foreground"><ServerLabel value={primary.publicChange} /></p>
                    {primary.publicLoss && (
                        <p className="text-xs text-amber-800"><ServerLabel value={primary.publicLoss} /></p>
                    )}
                    <ServerActionButton action={primary.nextAction} onAct={onAct} />
                </div>
            )}

            {rest.length > 0 && (
                <Collapsible open={open} onOpenChange={onOpenChange}>
                    <CollapsibleTrigger className="flex cursor-pointer items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground focus:outline-none">
                        <ChevronDown
                            className={`h-4 w-4 transition-transform ${open ? 'rotate-180' : ''}`}
                            aria-hidden
                        />
                        <span>其他办法（{rest.length}）</span>
                    </CollapsibleTrigger>
                    <CollapsibleContent className="space-y-2 pt-2">
                        {rest.map((o) => (
                            <div key={o.kind} className="space-y-2 rounded-md border border-border p-3">
                                <p className="text-sm font-medium"><ServerLabel value={o.publicReason} /></p>
                                <p className="text-sm text-muted-foreground"><ServerLabel value={o.publicChange} /></p>
                                {o.publicLoss && (
                                    <p className="text-xs text-muted-foreground"><ServerLabel value={o.publicLoss} /></p>
                                )}
                                <ServerActionButton action={o.nextAction} onAct={onAct} variant="outline" />
                            </div>
                        ))}
                    </CollapsibleContent>
                </Collapsible>
            )}
        </div>
    );
}

/** 静态闸的正样本锚点:证明「恢复走 exact GET」这条约束在源码里真的存在。 */
export const __defgeoDecisionMarkers = {
    restoresViaExactGet: true,
    neverRepostsPreview: true,
    actionIsRenderable,
} as const;
